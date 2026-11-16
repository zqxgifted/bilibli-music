# SPDX-License-Identifier: MIT
"""用「标题 + 弹幕 + 评论」校验每条投稿是否是目标本唱。

规则透明、可解释，输出证据（ev）。

三类标签（写入 `_verified_{mid}.json`，判定器 `rules.Classifier` 会读它）：

    <verified_label>  = 目标本唱（独唱/合唱，合唱也算）
    other             = 全别人唱（搬运/他人歌ってみた）→ 排除
    unknown           = 信号不足 → 需人工核对
    out               = 非唱歌类视频（标题无唱歌信号）

⚠️ 所有正则里的「目标名」都从 config 注入（target_aliases / target_name），
   本模块不写死任何人名：目标名从 config 注入，换目标改配置即可。
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

# ============================================================
# 通用正则（与目标无关的部分）
# ============================================================
SING_TITLE = re.compile(
    r'唱|翻唱|歌|cover|Cover|COVER|合唱|声乐|声|曲|麦|演奏|主题曲|插曲|单曲'
    r'|歌って|唄|ボカロ|Vocaloid|ost|OST|翻')
UTA_RE = re.compile(r'歌ってみた|歌って|替え歌')
ORIGINAL = re.compile(
    r'原唱|原版|原视频|搬运|转自|出自|翻唱自|转载|原曲|原唱版|原唱:')
OTHER_SINGER_TITLE = re.compile(
    r'【([^】]{1,15}?)(翻唱|弹唱|唱|歌|cover|Cover|COVER|唄|唱见)】')
OTHER_SINGER_TEXT = re.compile(
    r'(原唱[是为:：]?\s*([一-龥A-Za-z]{2,10})|([一-龥]{2,8})\s*原唱'
    r'|这是\s*([一-龥]{2,8})\s*唱的|(\w{2,12})\s*唱的版本'
    r'|(搬运\s*(自|的)?\s*([一-龥A-Za-z]{2,12})))')
SOFT_SING = re.compile(
    r'翻唱|女声版|女声翻唱|男声版|献歌|献唱|接唱|纯享版|唱rap|激唱|连唱|合唱|'
    r'唱了|小唱|清唱|开嗓|演绎|弹唱|翻奏|翻自|唱见|唱.{0,4}?《')
P1_RE = re.compile(r'(我|咱)(也)?(小)?(唱|合唱|清唱|开嗓|翻唱|献唱|吟唱|哼唱)')
P2_RE = re.compile(
    r'(我|咱).{0,8}?(唱了|小唱一首|再唱|唱起|唱首|唱了一首|合唱了|合唱一|唱了一)')
SONG_RE = re.compile(
    r'(唱|小唱|清唱|开嗓唱|翻唱|吟唱|哼唱|合唱).{0,3}?(《|了|一首|首|起《)')


class Verifier:
    """弹幕/评论校验器。

    目标专属的正则（署名、他人唱见名、角色名、群体企划）从 config 注入：

        verify_mark_regex      目标显式署名为主唱（正则）
        verify_collab_regex    合作/feat 署名（正则）
        verify_sing_regex      「目标+唱歌语境」（正则）
        verify_group_regex     群体企划（命中→他人）
        verify_role_regex      角色向演唱（命中→他人）
        verify_named_others    他人唱见名单（正则）
    """

    def __init__(self, cfg, client):
        self.cfg = cfg
        self.client = client
        aliases = cfg.target_aliases or cfg.target_name
        name = re.escape(cfg.target_name)

        # 默认按别名拼保守正则；config 里给了就用 config 的
        self.mark = re.compile(
            cfg.verify_mark_regex
            or rf'【{aliases}】|{aliases}翻唱|{aliases}·|{aliases}&|{aliases}本人'
               rf'|{aliases}独唱|{aliases}主唱|feat\.?\s*{aliases}'
               rf'|{aliases}\s*feat|{aliases}\s*[×x]|[×x]\s*{aliases}'
               rf'|{aliases}\s*合作|{aliases}\s*演唱')
        self.collab = re.compile(
            cfg.verify_collab_regex
            or rf'feat\.?\s*{aliases}|{aliases}\s*feat|{aliases}\s*[×x]\s*\S'
               rf'|[×x]\s*{aliases}|{aliases}\s*合作|{aliases}\s*演唱'
               rf'|{aliases}\s*vocal|{aliases}\s*/\s*\S', re.I)
        self.sing_ctx = re.compile(
            cfg.verify_sing_regex
            or rf'{aliases}\s*(唱|翻唱|声|版本|cover|Cover|这版|嗓|音色|清唱'
               rf'|开嗓|再唱|小唱|哼|演绎|现场)|{aliases}的(歌|声|翻唱)')
        self.name_re = re.compile(aliases)
        self.group_re = re.compile(cfg.verify_group_regex) if cfg.verify_group_regex else None
        self.role_re = re.compile(cfg.verify_role_regex) if cfg.verify_role_regex else None
        self.named_others = re.compile(cfg.verify_named_others) if cfg.verify_named_others else None
        self.p1 = re.compile(P1_RE.pattern.replace('咱)', f'咱|{name})'))
        self.p2 = re.compile(P2_RE.pattern.replace('咱)', f'咱|{name})'))
        self.label = cfg.verified_label or "verified"

    # ---------- 单条判定 ----------
    def title_judge(self, t: str) -> str:
        """返回 'self' / 'other' / 'ambiguous'。"""
        if self.mark.search(t) or self.collab.search(t):
            return "self"
        if UTA_RE.search(t) or (self.named_others and self.named_others.search(t)):
            return "other"
        if self.group_re and self.group_re.search(t):
            return "other"
        if self.role_re and self.role_re.search(t):
            return "other"
        if self.p1.search(t) or self.p2.search(t):
            return "self"
        if SONG_RE.search(t):
            return "self"
        return "ambiguous"

    def verify(self, title: str, comments: list, danmaku: list,
               is_archive: bool) -> tuple:
        """返回 (label, conf, evidence)。"""
        if not SING_TITLE.search(title):
            return "out", 0.0, "非唱歌类视频(标题无唱歌信号)"
        text = title + " " + " ".join(comments[:15]) + " " + " ".join(danmaku[:60])
        tj = self.title_judge(title)
        other_hit = OTHER_SINGER_TEXT.search(text)
        if tj == "self":
            if other_hit and ORIGINAL.search(text) and not self.sing_ctx.search(text):
                return "other", 0.6, f"标题似{self.cfg.target_name}但评论指名他人原唱: {other_hit.group(0)}"
            return self.label, 0.9, "标题判定为本唱"
        if tj == "other":
            return "other", 0.85, "标题为角色/他人翻唱(非本唱)"
        # ambiguous：用弹幕/评论补判
        if other_hit and ORIGINAL.search(text):
            return "other", 0.6, f"评论指名他人原唱: {other_hit.group(0)}"
        if self.sing_ctx.search(text):
            return self.label, 0.7, "评论/弹幕提及目标在唱/目标声"
        if is_archive:
            if self.name_re.search(text):
                return self.label, 0.6, "归档号唱歌候选且评论/弹幕提及目标"
            if SOFT_SING.search(title):
                return self.label, 0.5, "归档号唱歌候选且标题含明显唱歌标记"
            return "unknown", 0.45, "归档号唱歌候选，无明确信号，需核对"
        if SOFT_SING.search(title):
            return self.label, 0.5, "标题含明显唱歌标记(翻唱/女声版/献歌/接唱等)"
        return "unknown", 0.4, "标题与评论均无明确信号，需核对"

    # ---------- 批量 ----------
    def run(self, mid: int, name: str, is_archive: bool = False,
            only_new: bool = True, comment_pages: int = 1,
            log=print) -> dict:
        """校验一个号的投稿，写 `_verified_{mid}.json`。

        默认只处理「还没校验过」的条目（增量友好）；弹幕/评论抓取有 API 成本。
        """
        data_dir = self.cfg.data_dir
        sp = data_dir / f"_subs_{mid}.json"
        if not sp.is_file():
            raise FileNotFoundError(f"没有投稿缓存 {sp}，请先「抓取」")
        subs = json.loads(sp.read_text(encoding="utf-8"))
        sm = subs if isinstance(subs, dict) else {x["bvid"]: x for x in subs}

        vp = data_dir / f"_verified_{mid}.json"
        ver = {}
        if vp.is_file():
            try:
                ver = json.loads(vp.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                ver = {}
        dmp = data_dir / f"_dmcm_{mid}.json"
        dm = {}
        if dmp.is_file():
            try:
                dm = json.loads(dmp.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                dm = {}

        todo = [bv for bv in sm
                if not (only_new and bv in ver and (dm.get(bv) or {}).get("done"))]
        log(f"[{name}] 待校验 {len(todo)} 条（已校验 {len(ver)} 条）")

        cnt = {self.label: 0, "other": 0, "unknown": 0, "out": 0}
        n_done = 0
        for i, bv in enumerate(todo, 1):
            rec = sm[bv] or {}
            title = rec.get("title") or ""
            try:
                v = self.client.view(bv)
                aid, cid = v.get("aid"), None
                pages = v.get("pages") or [{}]
                cid = pages[0].get("cid")
                cs = self.client.comments(aid, comment_pages) if aid else []
                ds = self.client.danmaku(cid) if cid else []
                dm[bv] = {"aid": aid, "cid": cid, "comments": cs,
                          "danmaku": ds, "done": True}
                label, conf, ev = self.verify(title, cs, ds, is_archive)
                ver[bv] = {"label": label, "conf": conf, "ev": ev, "title": title,
                           "n_cmt": len(cs), "n_dm": len(ds)}
                cnt[label] = cnt.get(label, 0) + 1
                n_done += 1
                if i % 20 == 0 or i == len(todo):
                    log(f"  {i}/{len(todo)} 条（最近: {bv} [{label}]）")
                    # 定期落盘，中断不丢进度
                    _dump(vp, ver)
                    _dump(dmp, dm)
            except Exception as e:  # noqa: BLE001
                log(f"  !! {bv} 失败: {type(e).__name__}: {e}")
            time.sleep(max(0.5, self.cfg.api_interval))

        _dump(vp, ver)
        _dump(dmp, dm)
        log(f"[{name}] 校验完成：{self.label} {cnt.get(self.label,0)}"
            f" / other {cnt.get('other',0)} / unknown {cnt.get('unknown',0)}"
            f" / out {cnt.get('out',0)} → _verified_{mid}.json")
        return {"mid": mid, "name": name, "done": n_done, "stats": cnt}


def _dump(p: Path, data) -> None:
    try:
        p.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                     encoding="utf-8")
    except OSError:
        pass
