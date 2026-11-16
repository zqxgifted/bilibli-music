# SPDX-License-Identifier: MIT
"""抓取投稿列表。

全量翻页（首次）/ 增量（之后 `--inc`），并顺带补分P 信息。

风控策略（实测总结）：
    * 空间接口 412 / -352 / -799 是 **per-mid 限流**，等 30–60 分钟再跑那个号；
    * 单页失败不中断整轮，后面几轮只补失败的页；
    * 只有「长视频 or 标题像合集」才请求 view 补分P（省风控额度）。
"""
from __future__ import annotations

import json
import os
import random
import re
import time
from pathlib import Path

from . import bili_api

# 标题像「多P合集」的，无论时长都补分P
COLL = re.compile(
    r'合集|歌回|精选|串烧|联唱|歌单|全曲|整场|精选集|medley|串烧版|大合集|连唱', re.I)


class Fetcher:
    def __init__(self, cfg, client: bili_api.BiliClient):
        self.cfg = cfg
        self.client = client
        self.data_dir = cfg.data_dir

    # ---------- 持久化 ----------
    def _subs_path(self, mid: int) -> Path:
        return self.data_dir / f"_subs_{mid}.json"

    def _pages_path(self, mid: int) -> Path:
        return self.data_dir / f"_pages_{mid}.json"

    def _lastquery_path(self, mid: int) -> Path:
        return self.data_dir / f"_lastquery_{mid}.json"

    def _verified_path(self, mid: int) -> Path:
        return self.data_dir / f"_verified_{mid}.json"

    def load_subs(self, mid: int) -> list:
        p = self._subs_path(mid)
        if not p.is_file():
            return []
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        return data if isinstance(data, list) else list(data.values())

    def load_pages(self, mid: int) -> dict:
        p = self._pages_path(mid)
        if not p.is_file():
            return {}
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    # ---------- 抓取 ----------
    def fetch_list(self, mid: int, log=print) -> list:
        """全量翻页抓取。"""
        total, seen, out = None, set(), []
        maxpn, failed = 1, set()
        rounds = 0
        delay = max(1.5, self.cfg.api_interval)
        while rounds < 4:
            rounds += 1
            pn = 1
            while pn <= maxpn:
                if rounds > 1 and pn not in failed:
                    pn += 1
                    continue
                vl, cnt, ok = self.client.list_page(mid, pn)
                if ok and total is None and cnt:
                    total = cnt
                    maxpn = max(1, (total + 49) // 50)
                    log(f"  总投稿 {total}（{maxpn} 页）")
                if ok:
                    new = [v for v in vl if v["bvid"] not in seen]
                    for v in new:
                        seen.add(v["bvid"])
                    out += new
                    failed.discard(pn)
                    log(f"  第{pn}/{maxpn}页 +{len(new)} → {len(out)}/{total or '?'}")
                else:
                    failed.add(pn)
                    log(f"  !! 第{pn}页放弃（留待下一轮）")
                pn += 1
                time.sleep(delay + random.random() * 1.5)
            if total and len(out) >= total:
                break
            if not failed:
                break
            log(f"  —— 第{rounds}轮结束 {len(out)}/{total or '?'}，"
                f"剩余失败页 {sorted(failed)}，重试 ——")
            self.client.refresh()
            time.sleep(15)
        if total and len(out) < total:
            log(f"  !! 警告：只拿到 {len(out)}/{total} 条，仍缺 {total - len(out)} 条")
        return out

    def fetch_since(self, mid: int, since: int, log=print) -> list:
        """增量：从最新一页往回翻，翻到出现「不晚于 since」的投稿就停。"""
        out, pn = [], 1
        delay = max(1.5, self.cfg.api_interval)
        while pn <= 400:
            vl, cnt, ok = self.client.list_page(mid, pn)
            if not ok:
                log(f"  !! 第{pn}页抓取失败，增量中止（下次再试）")
                break
            if not vl:
                break
            out += [v for v in vl if (v.get("created") or 0) > since]
            if min((v.get("created") or 0) for v in vl) <= since:
                break
            pn += 1
            time.sleep(delay + random.random() * 1.5)
        return out

    # ---------- 落盘 ----------
    def _to_sub(self, v: dict, name: str) -> dict:
        return {
            "bvid": v["bvid"],
            "title": v.get("title") or "",
            "owner": v.get("author") or name,
            "duration": bili_api.len2sec(v.get("length")),
            "pubdate": v.get("created") or 0,
            "videos": 1,
            "url": f"https://www.bilibili.com/video/{v['bvid']}",
        }

    def run(self, mid: int, name: str, incremental: bool = False,
            all_pages: bool = False, min_dur: int = 480, log=print) -> dict:
        """抓一个号的全部数据，返回统计。"""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        sf = self._subs_path(mid)
        qf = self._lastquery_path(mid)

        old_subs, since = [], 0
        if incremental and sf.is_file() and qf.is_file():
            try:
                old_subs = self.load_subs(mid)
                since = int(json.loads(qf.read_text(encoding="utf-8"))
                            .get("max_pubdate") or 0)
            except Exception:
                old_subs, since = [], 0

        if since:
            log(f"[{name}] 增量更新：只取晚于 "
                f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(since))} 的投稿…")
            vl = self.fetch_since(mid, since, log)
            log(f"  新增 {len(vl)} 条")
            merged = {s["bvid"]: s for s in old_subs}
            for v in vl:
                merged[v["bvid"]] = self._to_sub(v, name)
            subs = sorted(merged.values(), key=lambda s: -s["pubdate"])
            sf.write_text(json.dumps(subs, ensure_ascii=False), encoding="utf-8")
            log(f"  合并后共 {len(subs)} 条")
        else:
            log(f"[{name}] 拉取投稿列表…")
            vl = self.fetch_list(mid, log)
            if not vl:
                log("  列表为空，退出")
                return {"mid": mid, "name": name, "count": 0, "ok": False}
            subs = [self._to_sub(v, name) for v in vl]
            subs.sort(key=lambda s: -s["pubdate"])
            sf.write_text(json.dumps(subs, ensure_ascii=False), encoding="utf-8")
            log(f"  已写 _subs_{mid}.json（{len(subs)} 条）")

        # ---- 补分P ----
        pages = self.load_pages(mid)
        pf = self._pages_path(mid)

        def need(s):
            if all_pages:
                return True
            return (s["duration"] >= min_dur or s["duration"] == 0
                    or bool(COLL.search(s["title"])))

        todo = [s["bvid"] for s in subs if s["bvid"] not in pages and need(s)]
        skipped = [s["bvid"] for s in subs if s["bvid"] not in pages and not need(s)]
        for bv in skipped:
            pages[bv] = []
        log(f"  需补分P {len(todo)} 个（跳过 {len(skipped)} 个短投稿）")

        for i, bv in enumerate(todo, 1):
            ok = False
            for attempt in range(4):
                try:
                    d = self.client.get(
                        f"https://api.bilibili.com/x/web-interface/view?bvid={bv}",
                        ref=f"https://www.bilibili.com/video/{bv}")
                    if d.get("code") == 0:
                        x = d["data"]
                        pages[bv] = [
                            {"page": p.get("page"), "part": p.get("part"),
                             "duration": p.get("duration"), "cid": p.get("cid")}
                            for p in (x.get("pages") or [])
                        ]
                        for s in subs:
                            if s["bvid"] == bv:
                                s["duration"] = x.get("duration") or s["duration"]
                                s["videos"] = x.get("videos") or 1
                                s["title"] = x.get("title") or s["title"]
                        ok = True
                        break
                    if d.get("code") in (-404, 62002):
                        pages[bv] = []
                        ok = True
                        break
                except Exception:
                    pass
                time.sleep(2 + attempt * 3)
            if not ok:
                pages[bv] = []
            if i % 50 == 0:
                sf.write_text(json.dumps(subs, ensure_ascii=False), encoding="utf-8")
                pf.write_text(json.dumps(pages, ensure_ascii=False), encoding="utf-8")
                log(f"    分P {i}/{len(todo)}")
            time.sleep(0.25)

        pf.write_text(json.dumps(pages, ensure_ascii=False), encoding="utf-8")
        sf.write_text(json.dumps(subs, ensure_ascii=False), encoding="utf-8")

        vp = self._verified_path(mid)
        if not vp.is_file():
            vp.write_text("{}", encoding="utf-8")

        meta = {"mid": mid, "name": name,
                "last_query": time.strftime("%Y-%m-%d %H:%M:%S"),
                "count": len(subs),
                "max_pubdate": max((s["pubdate"] for s in subs), default=0)}
        qf.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
        multi = sum(1 for v in pages.values() if v and len(v) >= 2)
        log(f"完成：{name} {len(subs)} 条（多P {multi} 个），最后查询 {meta['last_query']}")
        return {"mid": mid, "name": name, "count": len(subs), "multi": multi, "ok": True}


# ============================================================
# 歌名词典构建（从多P歌回分P名抽真实歌名）
# 从各号多P歌回的分P标题里抽取真实歌名，并入 _songs.txt。
# ============================================================
# 分P 中不属于歌名的（串场/聊天/设备/致谢等）
BAD_PART = re.compile(
    r'聊天|杂谈|开场|等待|抽奖|回放|录播|录屏|休息|广告|恰饭|读|看|说|聊|谢|歌单|预告|封面|'
    r'测试|调试|设备|弹幕|欢迎|来了|拜拜|晚安|下播|上播|闲聊|唠嗑|连麦|互动|唱见介绍|'
    r'^\d+$|^P\d+$|^第.{1,3}[首段]|未知|待定|无题|纯音乐|伴奏|BGM|bgm')
SKIP_VIDEO = re.compile(
    r'游戏|实况|通关|攻略|解说|配音|只狼|逆转|烟火|抽卡|原神|崩坏|星穹'
    r'|vlog|Vlog|直播录像|歌单|杂谈|ASMR')
_DATE_TAG = re.compile(r'[【\[（(]?\s*20\d{2}[-_./]?\d{2}[-_./]?\d{2}[^】\]）)]*[】\]）)]?')
_NUM_PREFIX = re.compile(r'^\s*\d+\s*[.、．,，)）:：]\s*')
_P_PREFIX = re.compile(r'^[Pp]\d+\s*[:：.、]\s*')
_TAIL_TAG = re.compile(r'[【\[].{0,20}[】\]]$')
_BAD_OLD = re.compile(r'第.{0,3}[集期季部]|大赛|比赛|合集|教程|[Vv]log|集$|期$')


def _clean_part_for_dict(p: str) -> str:
    s = (p or "").strip()
    s = _DATE_TAG.sub(" ", s)
    s = _NUM_PREFIX.sub("", s)
    s = _P_PREFIX.sub("", s)
    s = _TAIL_TAG.sub("", s)
    return s.strip(" -_·、,，.。~～")


def _part_ok(s: str) -> bool:
    if not s or not (1 <= len(s) <= 24) or re.search(r'[？?]', s):
        return False
    if BAD_PART.search(s):
        return False
    if re.search(r'\d{6,}', s):
        return False
    if re.search(r'是谁|哪起|为什么|怎么办|多少钱|哪一[个起]', s):
        return False
    if s.count('，') + s.count('、') + s.count(',') > 2:   # 一整串歌名列表
        return False
    return True


def build_songs_dict(cfg, mids: list | None = None, log=print) -> dict:
    """扫所有已抓取号的分P名，扩充 `_songs.txt`（自动备份旧词典）。"""
    from collections import Counter
    cnt = Counter()
    targets = mids or cfg.account_mids()
    for mid in targets:
        pf = cfg.data_dir / f"_pages_{mid}.json"
        sf = cfg.data_dir / f"_subs_{mid}.json"
        if not pf.is_file() or not sf.is_file():
            continue
        pages = json.loads(pf.read_text(encoding="utf-8"))
        subs = {x["bvid"]: x for x in json.loads(sf.read_text(encoding="utf-8"))}
        for bv, ps in pages.items():
            if SKIP_VIDEO.search((subs.get(bv) or {}).get("title") or ""):
                continue
            if isinstance(ps, dict):
                ps = ps.get("parts") or []
            if not ps or len(ps) < 3:
                continue
            for p in ps:
                s = _clean_part_for_dict(p.get("part"))
                if _part_ok(s):
                    cnt[s] += 1

    songs_f = cfg.songs_file
    old = []
    if songs_f.is_file():
        old = [l.strip() for l in songs_f.read_text(encoding="utf-8").splitlines()
               if l.strip()]
        try:
            bak = songs_f.with_name(songs_f.name + ".bak")
            bak.write_text(songs_f.read_text(encoding="utf-8"), encoding="utf-8")
        except OSError:
            pass

    keep_old = [s for s in old if not _BAD_OLD.search(s)]
    new = sorted(set(keep_old) | set(cnt))
    songs_f.parent.mkdir(parents=True, exist_ok=True)
    songs_f.write_text("\n".join(new), encoding="utf-8")
    log(f"歌名词典：原 {len(old)}（清理后 {len(keep_old)}）"
        f" → 新增候选 {len(set(cnt) - set(old))} → 合计 {len(new)}")
    top = ", ".join(s for s, _ in cnt.most_common(10))
    log(f"高频分P歌名 Top10：{top}")
    return {"total": len(new), "added": len(set(cnt) - set(old)),
            "cleaned": len(old) - len(keep_old)}


# ============================================================
# 扫描：还有谁在切目标
# ============================================================
def scan_ups(cfg, client: bili_api.BiliClient, pages: int = 22, log=print) -> list:
    """用搜索接口找「还有谁在切目标」。

    ⚠️ 标题法会**低估**不带《》的号，所以只用于**发现新号**，不做最终判定。
    """
    from collections import defaultdict
    tracked = {int(a.get("mid") or 0): str(a.get("name") or "")
               for a in cfg.accounts}
    name = cfg.target_name
    keywords = [name, f"{name} 翻唱", f"{name} 歌回", f"{name} 切片", f"{name} 翻唱合集"]
    seen: dict = {}
    for kw in keywords:
        got = 0
        for pn in range(1, pages + 1):
            res, ok = client.search_page(kw, pn)
            if not ok:
                log(f"[{kw}] pn={pn} 抓取失败，等待后重试")
                time.sleep(8)
                client.refresh()
                continue
            if not res:
                break
            for r in res:
                bv = r.get("bvid")
                if not bv or bv in seen:
                    continue
                seen[bv] = {
                    "bv": bv, "mid": r.get("mid"), "author": r.get("author"),
                    "title": bili_api.clean_html(r.get("title")),
                    "pubdate": r.get("pubdate") or 0,
                    "duration": r.get("duration") or "", "kw": kw,
                    "play": r.get("play") or 0,
                }
                got += 1
            log(f"[{kw}] pn={pn} +{len(res)} (总 {len(seen)})")
            time.sleep(1.6)
        log(f"=== {kw} 完成，新增 {got} ===")

    by = defaultdict(list)
    for v in seen.values():
        by[(v["mid"], v["author"])].append(v)
    now = time.time()
    rows = []
    for (mid, nm), lst in by.items():
        lst.sort(key=lambda x: -x["pubdate"])
        latest = lst[0]["pubdate"]
        d30 = sum(1 for x in lst if now - x["pubdate"] < 30 * 86400)
        d90 = sum(1 for x in lst if now - x["pubdate"] < 90 * 86400)
        rows.append({"mid": mid, "name": nm, "n": len(lst), "d30": d30, "d90": d90,
                     "latest": latest,
                     "latest_s": time.strftime("%Y-%m-%d", time.localtime(latest)),
                     "tracked": mid in tracked,
                     "samples": [x["title"][:34] for x in lst[:2]]})
    rows.sort(key=lambda r: (-r["d30"], -r["d90"], -r["n"]))
    return rows
