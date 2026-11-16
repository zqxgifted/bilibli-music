# SPDX-License-Identifier: MIT
"""两层去重 + 基底。

    第一层：同一场演唱只留一条
        键 = (归一化歌名, 原唱日期)。同日/±7 天内同歌视为同一场。
        保留规则：本人投稿优先；无本人投稿时优先 PRIORITY_UP，再按发布最早。

    第二层：每首歌只留最新一次演唱
        新演唱**替换**旧版本（不是「已有歌永不更新」——那样会把错收的条目永久锁死）。
        KEEP_ALL_SONGS 例外：这些歌不同日期的版本全保留。

产出（全部落在 cfg.data_dir / cfg.doc_dir）：
    _基底_每首歌最新.json    第二层结果
    _下载清单.json            基底驱动（_dl_m4a.py 吃它）
    _下载清单_全量.json        第一层后的全部演唱（备份/溯源）
    <目标>唱歌合集_去重.md
    <目标>唱歌合集_每首歌只留最新版本.md
"""
from __future__ import annotations

import json
import os
import re
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from . import naming, rules

D_FULL = re.compile(r'(20\d{2})[-./](\d{1,2})[-./](\d{1,2})')
D_SHORT = re.compile(r'(?<!\d)(\d{2})[.](\d{2})[.](\d{2})(?!\d)')
D_MD = re.compile(r'(?<!\d)(\d{1,2})/(\d{1,2})(?!\d)')

COMP_RE = re.compile(r'合集|精选集|大合集|首.{0,3}(翻唱|甜歌|热门|经典|金曲)')
SPEED_RE = re.compile(r'歌回|直播回放|回放|歌切')


# ============================================================
# 日期
# ============================================================
def extract_date(title: str, pub_ts: int) -> tuple:
    """返回 (date_str, exact)。exact=True 表示日期来自标题（原唱日期）。"""
    m = D_FULL.search(title or "")
    if m:
        y, mo, d = m.groups()
        return f"{y}-{int(mo):02d}-{int(d):02d}", True
    m = D_SHORT.search(title or "")
    if m:
        y, mo, d = m.groups()
        return f"20{y}-{int(mo):02d}-{int(d):02d}", True
    m = D_MD.search(title or "")
    if m:
        yr = datetime.fromtimestamp(int(pub_ts)).year
        mo, d = m.groups()
        return f"{yr}-{int(mo):02d}-{int(d):02d}", True
    return datetime.fromtimestamp(int(pub_ts)).strftime("%Y-%m-%d"), False


def to_ts(datestr: str) -> float:
    try:
        return datetime.strptime(datestr, "%Y-%m-%d").timestamp()
    except Exception:
        return 0


def date_gap(a: str, b: str) -> float:
    return abs(to_ts(a) - to_ts(b)) / 86400.0


def fdate(ts) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts else "-"


# ============================================================
# 歌名
# ============================================================
def song_from_title(t: str, classifier: rules.Classifier) -> str:
    """从标题抽去重键。"""
    kind, n = classifier.songs.hit(t)
    if n:
        return naming.norm_song(n)
    lab = rules.title_song_label(t)
    if lab:
        return naming.norm_song(lab)[:14]
    s = re.sub(r'^[【\[][^】\]]*[】\]]', '', t or "")
    s = re.sub(r'^[^，。！？~～]{0,3}[：:]', '', s)
    return naming.norm_song(s)[:14]


def is_songbook(pages: list) -> bool:
    """多P 且分P名像歌名 → 歌回。"""
    if not pages or len(pages) < 2:
        return False
    good = 0
    for p in pages:
        pt = (p.get("part") or "").strip()
        if pt.startswith("录制-") or re.search(r'\d{6,}', pt):
            continue
        if 1 <= len(pt) <= 22:
            good += 1
    return good >= max(2, int(len(pages) * 0.6))


# ============================================================
# 载入
# ============================================================
def load_account(cfg, mid: int, name: str, classifier: rules.Classifier) -> list:
    """载入一个号的投稿，并筛出判定为 ① 的。"""
    data_dir = cfg.data_dir
    sp = data_dir / f"_subs_{mid}.json"
    if not sp.is_file():
        return []
    try:
        subs = json.loads(sp.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    sm = subs if isinstance(subs, dict) else {x["bvid"]: x for x in subs}

    pp = data_dir / f"_pages_{mid}.json"
    pages = {}
    if pp.is_file():
        try:
            pages = json.loads(pp.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pages = {}

    vp = data_dir / f"_verified_{mid}.json"
    ver = {}
    if vp.is_file():
        try:
            ver = json.loads(vp.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            ver = {}

    out = []
    for bv, sub in sm.items():
        if not sub:
            continue
        t = sub.get("title") or ""
        r = ver.get(bv, {})
        pgs0 = pages.get(bv) or []
        if isinstance(pgs0, dict):
            pgs0 = pgs0.get("parts") or []
        cat = classifier.classify(bv, t, r.get("label", "out"), r.get("conf", 0),
                                  mid == cfg.self_mid, pgs0, sub.get("duration") or 0,
                                  mid in cfg.zl_only, mid=mid)
        if bv in classifier.manual.get("force_cat1", []):
            cat = 1
        elif bv in classifier.manual.get("force_non_song", []):
            cat = 3
        if cat != 1:
            continue
        out.append({
            "bv": bv, "mid": mid, "up": name, "title": t,
            "pub_ts": int(sub.get("pubdate") or 0),
            "dur": sub.get("duration"),
            "url": sub.get("url") or f"https://www.bilibili.com/video/{bv}",
            "pages": pgs0, "is_self": (mid == cfg.self_mid),
        })
    return out


def build_units(video: dict, classifier: rules.Classifier,
                strip_prefix: str = "") -> list:
    """视频 → 演唱单元（歌回按分P 拆开）。"""
    t = video["title"]
    pub = video["pub_ts"]
    d, dex = extract_date(t, pub)
    pgs = video["pages"]
    units = []
    # 搬运/精选合集：单P 长文件、日期是上传日期，不能当演唱日期。
    # ⚠️ 必须排除「歌回合集」这类多P 歌回 —— 一旦标成 comp，
    #    same_perf 对任意两条都返回 True，会把同一首歌的所有历史版本串成一个簇。
    comp = bool(COMP_RE.search(t)) and not is_songbook(pgs)
    if is_songbook(pgs):
        for p in pgs:
            s = naming.norm_song(p.get("part") or "", strip_prefix)
            if not s:
                continue
            units.append({"song": s, "date": d, "d_exact": dex, "pub_ts": pub,
                          "bv": video["bv"], "page": p.get("page"),
                          "part": p.get("part"), "up": video["up"], "title": t,
                          "is_self": video["is_self"], "dur": p.get("duration"),
                          "comp": comp})
    else:
        s = song_from_title(t, classifier)
        # 非歌回多P只下载 P1，时长也要取 P1 的，否则校验会把 P1 当成「被截断」
        dur = video["dur"]
        if pgs and len(pgs) > 1 and pgs[0].get("duration"):
            dur = pgs[0]["duration"]
        units.append({"song": s, "date": d, "d_exact": dex, "pub_ts": pub,
                      "bv": video["bv"], "page": None, "part": "", "up": video["up"],
                      "title": t, "is_self": video["is_self"], "dur": dur,
                      "comp": comp, "label": rules.title_song_label(t)})
    return units


# ============================================================
# 去重器
# ============================================================
class Deduper:
    def __init__(self, cfg, classifier: rules.Classifier, manual: dict | None = None):
        self.cfg = cfg
        self.clf = classifier
        self.manual = manual or {}
        self.units: list = []
        self.videos: list = []
        self.clusters: list = []
        self.n_excluded = 0
        # 按 UP 的清洗规则（GUI 可配）：{UP名: {remove, extract_re, skip}}
        self._up_rules: dict = {}
        for r in (cfg.up_clean_rules or []):
            up = str(r.get("up") or "")
            if not up:
                continue
            ex = r.get("extract_regex") or ""
            self._up_rules[up] = {
                "remove": [str(x) for x in (r.get("remove") or []) if x],
                "extract_re": re.compile(ex) if ex else None,
                "skip": [re.sub(r"\s+", "", str(x))
                         for x in (r.get("skip_parts") or []) if x],
            }

    def _apply_up_rules(self, up: str, name: str) -> str:
        """对某个 UP 的歌名应用它的清洗规则。"""
        rule = self._up_rules.get(up)
        if not rule:
            return name
        s = name or ""
        for bad in rule["skip"]:
            if bad and bad in re.sub(r"\s+", "", s):
                return ""
        for rm in rule["remove"]:
            if rm:
                s = s.replace(rm, " ")
        m = rule["extract_re"]
        if m:
            mm = m.search(s)
            if mm and mm.lastindex:
                s = mm.group(1)
        return re.sub(r"\s+", " ", s).strip(" -_·、,，.。~～")

    # ---------- 主流程 ----------
    def run(self, log=print) -> dict:
        cfg, clf = self.cfg, self.clf
        self.videos = []
        for acc in cfg.accounts:
            mid = int(acc.get("mid") or 0)
            name = str(acc.get("name") or mid)
            if not mid:
                continue
            vs = load_account(cfg, mid, name, clf)
            if not vs and not (cfg.data_dir / f"_subs_{mid}.json").is_file():
                log(f"  跳过 {name}（尚未抓取）")
                continue
            self.videos += vs
            log(f"  {name}: {len(vs)} 条判定为①")

        strip_prefix = re.escape(cfg.target_name)
        self.units = []
        for v in self.videos:
            self.units += build_units(v, clf, strip_prefix)

        # ---- 人工标注：分P级排除 ----
        ex_units = set(self.manual.get("force_non_song_units", []))
        if ex_units:
            self.units = [u for u in self.units
                          if f"{u['bv']}_p{int(u.get('page') or 1)}" not in ex_units]

        # ---- 人工标注：强制去重键 ----
        mk = self.manual.get("manual_key", {})
        if mk:
            for u in self.units:
                key = f"{u['bv']}_p{int(u.get('page') or 1)}"
                if key in mk and u["song"] != mk[key]:
                    u["song"] = mk[key]

        # ---- 黑名单 ----
        before = len(self.units)
        self.units = [u for u in self.units
                      if not clf.hit_exclude(u.get("part") or "", u.get("title") or "")]
        self.n_excluded = before - len(self.units)
        log(f"黑名单排除 {self.n_excluded} 个单元"
            f"（{', '.join(cfg.exclude_kws) or '无'}）")

        # ---- 第一层去重 ----
        self._dedup_first_layer(log)
        return self._second_layer(log)

    # ---------- 第一层 ----------
    def _dedup_first_layer(self, log=print) -> None:
        cfg = self.cfg
        by_song = defaultdict(list)
        for u in self.units:
            by_song[u["song"]].append(u)

        def same_perf(a, b) -> bool:
            """是否同一场演唱。"""
            if a.get("comp") or b.get("comp"):
                return True
            if a["d_exact"] and b["d_exact"]:
                return a["date"] == b["date"]
            if not a["d_exact"] and not b["d_exact"]:
                return date_gap(a["date"], b["date"]) <= 3
            ex, nx = (a, b) if a["d_exact"] else (b, a)
            delta = (to_ts(nx["date"]) - to_ts(ex["date"])) / 86400.0
            return 0 <= delta <= 7

        self.clusters = []
        for song, us in by_song.items():
            us.sort(key=lambda x: (x["date"], x["pub_ts"]))
            clusters = []
            for u in us:
                placed = False
                for cl in clusters:
                    if any(same_perf(u, x) for x in cl):
                        cl.append(u)
                        placed = True
                        break
                if not placed:
                    clusters.append([u])
            for cl in clusters:
                self.clusters.append(cl)
                selfs = [u for u in cl if u["is_self"]]
                if selfs:
                    for u in cl:
                        u["keep"] = bool(u["is_self"])
                    for u in cl:
                        if not u["is_self"]:
                            u["dup"] = True
                else:
                    # 无本人投稿时：优先 PRIORITY_UP，同优先级再按发布时间最早
                    cl.sort(key=lambda x: (0 if x["up"] == cfg.priority_up else 1,
                                          x["pub_ts"]))
                    cl[0]["keep"] = True
                    for u in cl[1:]:
                        u["dup"] = True

        for u in self.units:
            u.setdefault("keep", False)
            u.setdefault("dup", False)

    # ---------- 第二层 ----------
    def _second_layer(self, log=print) -> dict:
        cfg, clf = self.cfg, self.clf
        self_units = [u for u in self.units if u["is_self"]]
        arch_units = [u for u in self.units if not u["is_self"]]
        arch_videos = [v for v in self.videos if not v["is_self"]]
        self_videos = [v for v in self.videos if v["is_self"]]

        keep_by_video = defaultdict(list)
        for u in arch_units:
            if u["keep"]:
                keep_by_video[u["bv"]].append(u)

        # ---- 旧的基底（用于「新增 / 替换」统计与净化）----
        old_base = {"songs": {}, "keep_all": {}}
        if cfg.base_index.is_file():
            try:
                old_base = json.loads(cfg.base_index.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                old_base = {"songs": {}, "keep_all": {}}
        base_songs = dict(old_base.get("songs") or {})
        base_ka = {k: list(v) for k, v in (old_base.get("keep_all") or {}).items()}

        unit_by_key = {(u["bv"], int(u.get("page") or 1)): u
                       for u in list(self_units) + list(arch_units)}

        def _backfill(e):
            if e.get("disp"):
                return e
            u = unit_by_key.get((e["bv"], int(e.get("page") or 1)))
            e["disp"] = (self._disp(u) if u
                         else (rules.title_song_label(e.get("title") or "") or e["name"]))
            return e

        base_songs = {k: _backfill(v) for k, v in base_songs.items()}
        base_ka = {k: [_backfill(x) for x in lst] for k, lst in base_ka.items()}

        # ---- 基底净化：移除非幸存条目，让该键重新竞争 ----
        keep_ok = {(u["bv"], int(u.get("page") or 1))
                   for u in list(self_units) + list(arch_units) if u.get("keep")}
        n_clean = 0
        for k in list(base_songs):
            e = base_songs[k]
            if (e["bv"], int(e.get("page") or 1)) not in keep_ok:
                del base_songs[k]
                n_clean += 1
        # 旧键与 manual_key 不一致 → 删除旧键重建（避免同一条目双键）
        mk_clean = self.manual.get("manual_key", {})
        for k2 in list(base_songs):
            e2 = base_songs[k2]
            uk2 = f"{e2['bv']}_p{int(e2.get('page') or 1)}"
            if uk2 in mk_clean and k2 != mk_clean[uk2]:
                del base_songs[k2]
                n_clean += 1
        # 旧名字清洗
        for k3, e3 in base_songs.items():
            nm = e3.get("name") or ""
            nn = naming._DECOR.sub('', nm).strip()
            if nn and nn != nm:
                e3["name"] = nn
                dv = e3.get("disp") or ""
                e3["disp"] = naming._DECOR.sub('', dv).strip() or nn

        # ---- 重新构建基底 ----
        ka_seen = {(x["bv"], int(x.get("page") or 1))
                   for lst in base_ka.values() for x in lst}
        buckets = defaultdict(list)
        n_ka = 0
        for u in list(self_units) + list(arch_units):
            k = u["song"]
            if not k:
                continue
            if any(x in k for x in cfg.keep_all_songs):
                if not u["keep"]:
                    continue
                e = self._entry(u)
                kk = (e["bv"], e["page"])
                if kk not in ka_seen:
                    base_ka.setdefault(k, []).append(e)
                    ka_seen.add(kk)
                    n_ka += 1
                continue
            if not u["keep"]:
                continue
            buckets[k].append(u)

        n_new = n_upd = 0
        for k, b in buckets.items():
            b.sort(key=lambda x: (x["date"] or "0000", int(x.get("pub_ts") or 0)),
                   reverse=True)
            top_date = b[0]["date"]
            same = [x for x in b if x["date"] == top_date]
            pick = next((x for x in same if x["up"] == cfg.priority_up), b[0])
            e_new = self._entry(pick)
            old = base_songs.get(k)
            if old is None:
                base_songs[k] = e_new
                n_new += 1
            elif (e_new["date"] or "0000") > (old.get("date") or "0000"):
                base_songs[k] = e_new
                n_upd += 1

        cfg.base_index.parent.mkdir(parents=True, exist_ok=True)
        cfg.base_index.write_text(json.dumps({
            "created": time.strftime("%Y-%m-%d %H:%M:%S"),
            "keep_all": list(cfg.keep_all_songs),
            "songs": base_songs,
            "keep_all_songs": base_ka,
        }, ensure_ascii=False, indent=1), encoding="utf-8")

        # ---- 基底驱动的下载清单 ----
        rows = []
        for _k, e in base_songs.items():
            rows.append((e["date"] or "0000", e))
        for _k, lst in base_ka.items():
            for e in lst:
                rows.append((e["date"] or "0000", e))
        rows.sort(key=lambda x: x[0], reverse=True)

        manifest = [{"bv": e["bv"], "page": int(e["page"]), "up": e["up"],
                     "self": int(e.get("self") or 0), "name": e["name"],
                     "dur": e.get("dur"), "date": e["date"]}
                    for _d, e in rows]
        cfg.manifest.parent.mkdir(parents=True, exist_ok=True)
        cfg.manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=1),
                                encoding="utf-8")

        full_manifest = []
        for u in list(self_units) + [x for x in arch_units if x.get("keep")]:
            full_manifest.append({
                "bv": u["bv"], "page": int(u.get("page") or 1), "up": u["up"],
                "self": 1 if u["is_self"] else 0, "name": self._dl_name(u),
                "dur": u.get("dur"), "date": u["date"]})
        full_manifest.sort(key=lambda x: (0 if x["self"] else 1, x["date"] or "",
                                          x["bv"], x["page"]))
        cfg.manifest_full.write_text(json.dumps(full_manifest, ensure_ascii=False,
                                                indent=1), encoding="utf-8")

        log(f"下载清单：{len(manifest)} 条（基底驱动）"
            f"｜全量备份 {len(full_manifest)} 条")
        log(f"二次去重：{len(rows)} 段（{len(base_songs)} 首歌 + 例外 "
            f"{sum(len(v) for v in base_ka.values())}；新增 {n_new}，替换 {n_upd}"
            f"，净化 {n_clean}）")

        self._render_docs(rows, base_songs, base_ka, self_units, arch_units,
                          self_videos, arch_videos, keep_by_video, log)
        return {"manifest": manifest, "full": full_manifest, "rows": rows,
                "new": n_new, "updated": n_upd, "cleaned": n_clean,
                "excluded": self.n_excluded,
                "songs": len(base_songs), "clusters": len(self.clusters)}

    # ---------- 命名 ----------
    def _dl_name(self, u: dict) -> str:
        """下载文件名主体（不含 BV 后缀）。按 UP 清洗规则在最后应用。"""
        md = self.manual.get("manual_disp", {})
        k = f"{u['bv']}_p{int(u.get('page') or 1)}"
        if k in md:
            return md[k]
        if u.get("page") is not None:
            base = naming.part_name(u.get("part")) or self._title_name(u.get("title"))
        else:
            base = rules.strip_emoji(u.get("title") or "")
        cleaned = self._apply_up_rules(u.get("up") or "", base)
        return cleaned or base

    def _title_name(self, title: str) -> str:
        return rules.title_song_label(title or "") or rules.strip_emoji(title or "")

    def _disp(self, u: dict) -> str:
        """文档「曲目」列。"""
        md = self.manual.get("manual_disp", {})
        k = f"{u['bv']}_p{int(u.get('page') or 1)}"
        if k in md:
            return md[k]
        if u.get("page") is not None:
            pn = naming.part_name(u.get("part"))
            if pn:
                return pn
            return rules.clean_part(u.get("part")) or rules.strip_emoji(u.get("title") or "")
        return rules.title_song_label(u.get("title") or "") or self._dl_name(u)

    def _entry(self, u: dict) -> dict:
        return {"bv": u["bv"], "page": int(u.get("page") or 1), "date": u["date"],
                "up": u["up"], "name": self._dl_name(u), "dur": u.get("dur"),
                "disp": self._disp(u), "title": rules.strip_emoji(u.get("title") or ""),
                "self": 1 if u["is_self"] else 0}

    # ---------- 文档 ----------
    def _render_docs(self, rows, base_songs, base_ka, self_units, arch_units,
                     self_videos, arch_videos, keep_by_video, log=print) -> None:
        cfg = self.cfg
        tgt = cfg.target_name
        cfg.doc_dir.mkdir(parents=True, exist_ok=True)
        cfg.list_dir.mkdir(parents=True, exist_ok=True)

        # ---- 全时期文档 ----
        md = [f"# {tgt}唱歌合集（去重版）\n"]
        md.append(f"> **生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}**"
                  f"　｜　数据源：{len(cfg.accounts)} 个 B站空间的全部投稿，"
                  f"仅收三分类中的 ①（{tgt}本人唱/合唱）。\n")
        md.append(f"> 去重规则：按「歌名 + 原唱日期」判同一场演唱；本人投稿优先；"
                  f"无本人投稿时优先保留 **{cfg.priority_up or '（未设置）'}**。")
        md.append("> 歌回（多P且分P为歌名）按每个分P拆成一首歌参与去重。\n")
        n1 = self._count_files(cfg.dir_self)
        n2 = self._count_files(cfg.dir_other)
        md.append(f"> **已下载音频**：本人 `{cfg.dir_self.name}`（{n1} 个）｜"
                  f"其他 UP `{cfg.dir_other.name}`（{n2} 个）")
        md.append("> 命名规则：`<分P名 或 精简标题>_<BV号>_p<分P号>.m4a`；"
                  "重跑下载会自动跳过已存在的文件。\n")

        md.append("## 总览")
        keep_arch = [u for u in arch_units if u["keep"]]

        def _real_name(u):
            if u["page"] is not None:
                return True
            return bool(u.get("label"))

        songs = ({u["song"] for u in self_units if _real_name(u)}
                 | {u["song"] for u in keep_arch if _real_name(u)})
        md.append(f"- **累计可听录音（去重后）**：**{len(self_units)+len(keep_arch)} 段**，"
                  f"分布在 **{len(self_videos)+len(keep_by_video)} 个视频**里")
        md.append(f"- **可确认曲目的不同歌名**：约 **{len(songs)} 首**")
        md.append(f"- ├ 一、{tgt}本人投稿：{len(self_videos)} 个视频 / {len(self_units)} 段")
        md.append(f"- └ 二、其他 UP 独有：{len(keep_by_video)} 个视频 / {len(keep_arch)} 段")
        md.append(f"- 被判为重复已剔除：{len([u for u in arch_units if u['dup']])} 段\n")

        # 一、本人投稿
        md.append(f"## 一、{tgt}本人投稿的唱歌视频 · {len(self_videos)} 个\n")
        md.append("| # | 发布日期 | 标题 | 时长 | 分P（分P名） | BV |")
        md.append("| ---: | --- | --- | ---: | --- | --- |")
        self_videos.sort(key=lambda v: v["pub_ts"], reverse=True)
        for i, v in enumerate(self_videos, 1):
            cell = rules.part_cell(v["pages"] or [], v["title"]).replace("|", "｜")
            md.append(f"| {i} | {fdate(v['pub_ts'])} | "
                      f"{rules.strip_emoji(v['title']).replace('|','｜')} | "
                      f"{rules.fmt_dur(v['dur'])} | {cell} | "
                      f"[{v['bv']}]({v['url']}) |")
        md.append("")

        # 二、其他 UP 独有
        md.append(f"## 二、其他 UP 录制的{tgt}唱歌/合唱（已去重） · "
                  f"{len(keep_by_video)} 个\n")
        md.append("> 每个视频只列出它**独有（最先发布）**的演唱；"
                  "与本人投稿或更早录制重复的已剔除。\n")
        md.append("| # | 原唱日期 | UP | 标题 | 独有曲目（含分P序号） | 时长 | BV |")
        md.append("| ---: | --- | --- | --- | --- | ---: | --- |")
        av = sorted([v for v in arch_videos if v["bv"] in keep_by_video],
                    key=lambda v: v["pub_ts"], reverse=True)
        for i, v in enumerate(av, 1):
            ks = sorted(keep_by_video[v["bv"]], key=lambda x: (x["page"] or 0))

            def _nm(u):
                pg = u.get("page")
                if pg is None:
                    return rules.title_song_label(u.get("title") or "").replace("|", "｜")
                nm = rules.clean_part(u.get("part"))
                return (f"P{pg} {nm}" if nm else f"P{pg}").replace("|", "｜")

            names = "、".join(x for x in (_nm(u) for u in ks) if x)
            cell = f"**{len(ks)} 首**：{names}" if names else f"**{len(ks)} 首**：（无曲名）"
            md.append(f"| {i} | {ks[0]['date']} | {v['up']} | "
                      f"{rules.strip_emoji(v['title']).replace('|','｜')} | {cell} | "
                      f"{rules.fmt_dur(v['dur'])} | [{v['bv']}]({v['url']}) |")
        md.append("")

        # 三、统计
        md.append("## 三、去重统计与监控建议\n")
        md.append("| UP | 录制视频 | 演唱单元 | 参与场次 | 独家场次 | 独家占比 | 重复率 | 建议 |")
        md.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |")
        up_stat = defaultdict(lambda: {"units": 0, "keep": 0, "dup": 0, "videos": 0})
        for u in arch_units:
            s = up_stat[u["up"]]
            s["units"] += 1
            if u["keep"]:
                s["keep"] += 1
            elif u["dup"]:
                s["dup"] += 1
        for v in arch_videos:
            up_stat[v["up"]]["videos"] += 1

        up_clusters = defaultdict(set)
        up_excl = defaultdict(int)
        for i, cl in enumerate(self.clusters):
            arch_ups = {u["up"] for u in cl if not u["is_self"]}
            has_self = any(u["is_self"] for u in cl)
            for up in arch_ups:
                up_clusters[up].add(i)
            if len(arch_ups) == 1 and not has_self:
                up_excl[next(iter(arch_ups))] += 1

        rows2 = []
        for up, s in up_stat.items():
            tot = s["units"]
            ratio = (s["dup"] / tot) if tot else 0
            part = len(up_clusters[up])
            ex = up_excl[up]
            exr = (ex / part) if part else 0
            if exr <= 0.30:
                adv = "**可停止监控**（独家极少）"
            elif ratio >= 0.6:
                adv = "优先级低（重复过半）"
            else:
                adv = "建议保留"
            rows2.append((up, s["videos"], tot, part, ex, exr, ratio, adv))
        rows2.sort(key=lambda x: x[5])
        for up, nv, tot, part, ex, exr, ratio, adv in rows2:
            md.append(f"| {up} | {nv} | {tot} | {part} | {ex} | {exr*100:.0f}% | "
                      f"{ratio*100:.0f}% | {adv} |")
        md.append("")
        md.append("> **独家场次** = 只有该 UP 录到、别人都没有的演唱场次。\n")

        # 歌回发布速度
        speed = defaultdict(list)
        for v in self.videos + arch_videos:
            if not SPEED_RE.search(v["title"]):
                continue
            _d, _dex = extract_date(v["title"], v["pub_ts"])
            if not _dex:
                continue
            try:
                _lag = (datetime.fromtimestamp(v["pub_ts"]).date()
                        - datetime.strptime(_d, "%Y-%m-%d").date()).days
            except Exception:
                continue
            if 0 <= _lag <= 30:
                speed[v["up"]].append(_lag)
        if speed:
            md.append("### 歌回发布速度\n")
            md.append("| UP | 可测场次 | 中位延迟 | 平均延迟 | 当天/次日发布 |")
            md.append("| --- | ---: | ---: | ---: | ---: |")
            srows = []
            for up, lags in speed.items():
                lags.sort()
                srows.append((up, len(lags), lags[len(lags)//2], sum(lags)/len(lags),
                              sum(1 for x in lags if x <= 1)/len(lags)))
            srows.sort(key=lambda x: (x[2], x[3]))
            for up, n, med, avg, r1 in srows:
                md.append(f"| {up} | {n} | {med} 天 | {avg:.1f} 天 | {r1*100:.0f}% |")
            md.append("")

        cfg.out_all.write_text("\n".join(md), encoding="utf-8")

        # ---- 主文档：每首歌只留最新版本 ----
        md2 = [f"# {tgt}唱歌合集（每首歌只留最新版本）\n"]
        md2.append(f"> **生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}**　｜　"
                   f"基底文件：`_基底_每首歌最新.json`")
        md2.append("> 规则：同一首歌（规范化歌名相同）**始终保留最新一次演唱**——"
                   "此后若有更新的演唱，自动替换旧版本。")
        if cfg.keep_all_songs:
            md2.append(f"> 例外：**{'、'.join(cfg.keep_all_songs)}** 不同日期的演唱全部保留"
                       f"（当前 {sum(len(v) for v in base_ka.values())} 条）。")
        md2.append(f"> 本文件是后续更新的**基底**：全时期版本见《{tgt}唱歌合集_去重.md》。\n")
        n_self = sum(1 for _d, e in rows if e.get("self"))
        md2.append("## 总览")
        md2.append(f"- **收录 {len(rows)} 段**（不同歌曲 {len(base_songs)} 首 + "
                   f"例外全保留 {sum(len(v) for v in base_ka.values())} 段）")
        md2.append(f"- ├ {tgt}本人：{n_self} 段")
        md2.append(f"- └ 其他 UP：{len(rows)-n_self} 段\n")
        md2.append("| # | 最近演唱日期 | UP | 曲目 | 来源标题 | 时长 | BV |")
        md2.append("| ---: | --- | --- | --- | --- | ---: | --- |")
        for i, (_d, e) in enumerate(rows, 1):
            t = e.get("title") or ""
            md2.append(f"| {i} | {e['date']} | {e['up']} | "
                       f"{str(e.get('disp') or e['name']).replace('|','｜')} | "
                       f"{t.replace('|','｜')} | {rules.fmt_dur(e.get('dur'))} | "
                       f"[{e['bv']}](https://www.bilibili.com/video/{e['bv']}) |")
        cfg.out_latest.write_text("\n".join(md2), encoding="utf-8")

    @staticmethod
    def _count_files(d: Path) -> int:
        try:
            return len([f for f in os.listdir(d) if f.endswith((".m4a", ".flac", ".mp3"))])
        except OSError:
            return 0
