# SPDX-License-Identifier: MIT
"""编排层：把核心步骤串成「用户看得懂」的流程。

GUI 和命令行都只调这里的函数，不直接碰底层模块。

五个步骤：
    ① 抓取    fetch     抓投稿列表（全量 / 增量）
    ② 判定    classify  三分类 + 每个 UP 一份清单 md
    ③ 去重    dedup     两层去重 → 基底 → 下载清单 → 两份合集文档
    ④ 下载    download  按基底清单下载音频（断点续跑）
    ⑤ 校验    verify    缺失 / 损坏 / 时长不符
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path

from . import bili_api, config as cfg_mod, dedup, downloader, fetcher, naming, rules


class Pipeline:
    """把 config + client + 各步骤组装起来。"""

    def __init__(self, cfg, log=print):
        self.cfg = cfg
        self.log = log
        self._client = None
        self._classifier = None
        self._manual = None
        # 启动即应用标题净化策略（naming/rules 里的开关）
        naming.set_sanitize(getattr(cfg, "sanitize", None))

    # ---------- 惰性构造 ----------
    @property
    def client(self) -> bili_api.BiliClient:
        if self._client is None:
            self.cfg.ensure_dirs()
            sess = self._find_sessdata()
            self._client = bili_api.BiliClient(sess, self.cfg.ua, self.cfg.api_interval)
        return self._client

    def _find_sessdata(self) -> str:
        """按优先级找 cookies 文件：

            1. 数据目录下的 cookies.txt      （每个目标一套 cookie，推荐）
            2. 程序目录下的 cookies.txt      （全局共用）
            3. config/user.json 里的 cookies_file 指定路径
        """
        p = self._find_sessdata_path()
        if p:
            try:
                return bili_api.load_sessdata(p)
            except OSError:
                return ""
        return ""

    def _find_sessdata_path(self):
        """返回第一个存在的 cookies 文件路径；都没有则返回 None。"""
        candidates = []
        user = self.cfg.raw.get("cookies_file") if self.cfg.raw else None
        if user:
            candidates.append(Path(user))
        candidates += [
            self.cfg.base_dir / self.cfg.cookies_file,
            cfg_mod.project_root() / self.cfg.cookies_file,
            cfg_mod.project_root() / "cookies.txt",
        ]
        for p in candidates:
            try:
                if p and Path(p).is_file() and bili_api.load_sessdata(p):
                    return Path(p)
            except OSError:
                continue
        return None

    @property
    def manual(self) -> dict:
        if self._manual is None:
            p = cfg_mod.project_root() / "manual_labels.json"
            if p.is_file():
                try:
                    self._manual = json.loads(p.read_text(encoding="utf-8-sig"))
                except (OSError, json.JSONDecodeError):
                    self._manual = {}
            else:
                self._manual = {}
        return self._manual

    @property
    def classifier(self) -> rules.Classifier:
        if self._classifier is None:
            sd = rules.SongDict.from_file(self.cfg.songs_file)
            self._classifier = rules.Classifier(self.cfg, self.manual, sd)
        return self._classifier

    # ---------- ① 抓取 ----------
    def step_fetch(self, incremental: bool = True, all_pages: bool = False,
                   min_dur: int = 480, only: list | None = None) -> dict:
        cfg = self.cfg
        if not cfg.is_configured():
            raise RuntimeError(
                "还没配置目标与账号。\n"
                "请到「配置」页填写：目标名、目标别名正则、要跟踪的 UP（mid + 名字）。")
        cfg.ensure_dirs()
        f = fetcher.Fetcher(cfg, self.client)
        targets = cfg.accounts
        if only:
            mids = {int(x) for x in only}
            targets = [a for a in cfg.accounts if int(a.get("mid") or 0) in mids]
        results = []
        for acc in targets:
            mid = int(acc.get("mid") or 0)
            name = str(acc.get("name") or mid)
            if not mid:
                continue
            self.log(f"── 抓取 {name}（mid={mid}）──")
            try:
                r = f.run(mid, name, incremental=incremental, all_pages=all_pages,
                          min_dur=min_dur, log=self.log)
                results.append(r)
            except Exception as e:  # noqa: BLE001
                self.log(f"  {name} 失败：{type(e).__name__}: {e}")
                results.append({"mid": mid, "name": name, "ok": False, "err": str(e)})
        return {"accounts": results}

    # ---------- ② 判定 ----------
    def step_classify(self) -> dict:
        cfg = self.cfg
        clf = self.classifier
        if not cfg.accounts:
            raise RuntimeError("还没有配置要跟踪的 UP。")
        cfg.ensure_dirs()
        cfg.list_dir.mkdir(parents=True, exist_ok=True)
        totals = [0, 0, 0]
        per_account = []
        for acc in cfg.accounts:
            mid = int(acc.get("mid") or 0)
            name = str(acc.get("name") or mid)
            if not mid:
                continue
            sp = cfg.data_dir / f"_subs_{mid}.json"
            if not sp.is_file():
                self.log(f"{name:22s} 跳过（尚未抓取 _subs_{mid}.json）")
                continue
            try:
                a, b, c = self._gen_account(mid, name, clf)
            except Exception as e:  # noqa: BLE001
                self.log(f"{name:22s} 失败：{type(e).__name__}: {e}")
                continue
            totals[0] += a
            totals[1] += b
            totals[2] += c
            per_account.append({"mid": mid, "name": name, "c1": a, "c2": b, "c3": c})
        self.log(f"\n合计：① {totals[0]} ／ ② {totals[1]} ／ ③ {totals[2]} ／ "
                 f"总 {sum(totals)}")
        return {"c1": totals[0], "c2": totals[1], "c3": totals[2],
                "per_account": per_account}

    def _gen_account(self, mid: int, name: str, clf: rules.Classifier):
        cfg = self.cfg
        subs = json.loads((cfg.data_dir / f"_subs_{mid}.json")
                          .read_text(encoding="utf-8"))
        sm = subs if isinstance(subs, dict) else {x["bvid"]: x for x in subs}
        pp = cfg.data_dir / f"_pages_{mid}.json"
        pages = {}
        if pp.is_file():
            try:
                pages = json.loads(pp.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pages = {}
        vp = cfg.data_dir / f"_verified_{mid}.json"
        ver = {}
        if vp.is_file():
            try:
                ver = json.loads(vp.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                ver = {}
        is_self = (mid == cfg.self_mid)

        entries = []
        for bv, sub in sm.items():
            if not sub:
                continue
            rec = ver.get(bv, {})
            t = sub.get("title", "")
            parts = pages.get(bv) or []
            if isinstance(parts, dict):
                parts = parts.get("parts") or []
            c = clf.classify(bv, t, rec.get("label", "out"), rec.get("conf", 0),
                             is_self, parts, sub.get("duration") or 0,
                             mid in cfg.zl_only, mid=mid)
            if bv in clf.manual.get("force_cat1", []):
                c = 1
            elif bv in clf.manual.get("force_non_song", []):
                c = 3
            entries.append({
                "bv": bv, "cls": c, "title": t,
                "date": rules.fmt_date(sub.get("pubdate")),
                "dur": sub.get("duration"),
                "url": sub.get("url") or f"https://www.bilibili.com/video/{bv}",
                "parts": parts})

        entries.sort(key=lambda e: (e["date"] or "0000"), reverse=True)
        c1 = [e for e in entries if e["cls"] == 1]
        c2 = [e for e in entries if e["cls"] == 2]
        c3 = [e for e in entries if e["cls"] == 3]
        c3_rel = [e for e in c3 if is_self or clf.alias.search(e["title"])]
        c3_irr = [e for e in c3 if not (is_self or clf.alias.search(e["title"]))]

        tgt = cfg.target_name
        md = [f"# {name} 投稿全量清单（B站空间 {mid}）\n"]
        qpath = cfg.data_dir / f"_lastquery_{mid}.json"
        qtime = "-"
        if qpath.is_file():
            try:
                qtime = json.loads(qpath.read_text(encoding="utf-8")).get("last_query") or "-"
            except (OSError, json.JSONDecodeError):
                pass
        latest = max((e["date"] for e in entries if e.get("date")), default="-")
        md.append(f"> **最后查询时间：{qtime}** ｜ 共 **{len(entries)}** 条投稿 ｜ "
                  f"最新一条投稿：{latest}")
        md.append(f"> 下次更新（增量）：`抓取` 加「增量」选项，然后重跑「判定」。\n")
        md.append(f"## 总览")
        md.append(f"- **① {tgt}本人唱 / 与他人合唱**：{len(c1)} 条")
        md.append(f"- **② 疑似有{tgt}唱**：{len(c2)} 条")
        md.append(f"- **③ 与唱歌无关（与{tgt}有关）**：{len(c3_rel)} 条")
        md.append(f"- 另有 {len(c3_irr)} 条明确与{tgt}无关的投稿，未列出\n")
        md.append("## 分类说明")
        md.append(f"- **①**：{tgt}独唱、{tgt}版翻唱、{tgt}与他人合唱，以及整场多P歌回。")
        md.append(f"- **②**：标题有歌名但无法确认{tgt}是否在唱，需抽听确认。")
        md.append(f"- **③**：{tgt}的杂谈/直播切片、角色向视频等。\n")

        md.append(f"## ① {tgt}本人唱 / 合唱 · {len(c1)} 条\n")
        md.append(self._render_table(c1))
        md.append("\n### 分P 明细（点击展开）\n")
        md.append(self._render_details(c1))
        md.append(f"\n## ② 疑似有{tgt}唱 · {len(c2)} 条\n")
        md.append(self._render_table(c2))
        md.append("\n### 分P 明细（点击展开）\n")
        md.append(self._render_details(c2))
        md.append(f"\n## ③ 与唱歌无关（但与{tgt}有关）· {len(c3_rel)} 条\n")
        md.append(self._render_table(c3_rel))
        if c3_irr:
            md.append(f"\n---\n\n> **已略去 {len(c3_irr)} 条明确与{tgt}无关的投稿**。"
                      f"原始数据仍在 `_subs_{mid}.json`。")

        fn = cfg.list_dir / f"{name}_投稿全量清单.md"
        fn.write_text("\n".join(md), encoding="utf-8")
        self.log(f"{name:22s} ①{len(c1):4d}  ②{len(c2):4d}  "
                 f"③{len(c3_rel):4d}（略去无关 {len(c3_irr):4d}）  合计{len(entries):4d}")
        return len(c1), len(c2), len(c3)

    @staticmethod
    def _render_table(entries: list) -> str:
        lines = ["| # | 日期 | 标题 | 时长 | 分P（分P名） | BV |",
                 "| ---: | --- | --- | ---: | --- | --- |"]
        for i, e in enumerate(entries, 1):
            title = rules.strip_emoji(e["title"]).replace("|", "｜")
            parts = e["parts"] or []
            cell = (rules.part_cell(parts, e["title"]) if len(parts) <= 9
                    else f"{len(parts)}P（见下方明细）")
            cell = cell.replace("|", "｜")
            lines.append(f"| {i} | {e['date']} | {title} | {rules.fmt_dur(e['dur'])} | "
                         f"{cell} | [{e['bv']}]({e['url']}) |")
        return "\n".join(lines)

    @staticmethod
    def _render_details(entries: list) -> str:
        blocks = []
        for i, e in enumerate(entries, 1):
            np = len(e["parts"]) if e["parts"] else 1
            title = rules.strip_emoji(e["title"])
            head = (f"<details><summary><b>{i}.</b> {title}（共{np}P · "
                    f"{rules.fmt_dur(e['dur'])}）｜ <a href=\"{e['url']}\">BV</a></summary>\n\n")
            body = f"> 大标题：{title}\n"
            if e["parts"]:
                for p in e["parts"]:
                    pg = p.get("page")
                    raw = (p.get("part") or "").strip()
                    nm = rules.clean_part(raw)
                    if len(e["parts"]) == 1 and (not nm or rules.same_title(raw, e["title"])):
                        body += f"> 1P：与主标题相同（{rules.fmt_dur(p.get('duration'))}）\n"
                    else:
                        body += (f"> {pg}P：{nm or f'（第 {pg}P，无标题）'}"
                                 f"（{rules.fmt_dur(p.get('duration'))}）\n")
            else:
                body += f"> 1P：与主标题相同（{rules.fmt_dur(e['dur'])}）\n"
            blocks.append(head + body + "</details>")
        return "\n".join(blocks)

    # ---------- ③ 去重 ----------
    def step_dedup(self) -> dict:
        cfg = self.cfg
        if not list(cfg.data_dir.glob("_subs_*.json")):
            raise RuntimeError(
                f"没有找到任何投稿缓存（{cfg.data_dir}/_subs_*.json）。\n"
                f"请先执行「抓取」。")
        d = dedup.Deduper(cfg, self.classifier, self.manual)
        return d.run(log=self.log)

    # ---------- ④ 下载 ----------
    def step_download(self, jobs: int = 0, limit: int = 0, offset: int = 0,
                      only: str = "all", overwrite: bool = False,
                      dry_run: bool = False,
                      manifest_path: Path | None = None) -> dict:
        cfg = self.cfg
        ffmpeg = self._resolve_ffmpeg()
        d = downloader.Downloader(cfg, self.client, ffmpeg)
        return d.run(manifest_path=manifest_path, jobs=jobs, limit=limit,
                     offset=offset, only=only,
                     overwrite=overwrite or cfg.overwrite, dry_run=dry_run,
                     log=self.log)

    # ---------- ⑤ 校验 ----------
    def step_verify(self) -> dict:
        return downloader.verify(self.cfg, self._resolve_ffprobe(), log=self.log)

    # ---------- 辅助 ----------
    def _resolve_ffmpeg(self) -> str:
        return self.cfg.ffmpeg or "ffmpeg"

    def _resolve_ffprobe(self) -> str:
        return self.cfg.ffprobe or "ffprobe"

    # ---------- 账号状态（配置页「已登录/未登录」） ----------
    def account_status(self) -> dict:
        """实时查 nav，返回登录状态。"""
        info = self.client.nav_info()
        info["cookie_expired"] = getattr(self.client, "cookie_expired", False)
        info["sessdata_len"] = len(self.client.sessdata)
        info["vip_names"] = {0: "普通", 1: "大会员", 2: "年费大会员"}
        return info

    def save_sessdata(self, pasted: str) -> str:
        """接受用户粘贴的任意格式 cookie，解析出 SESSDATA 写进 cookies.txt。

        返回解析出的值；解析失败抛 ValueError。
        """
        sess = bili_api.parse_sessdata(pasted)
        if not sess:
            raise ValueError(
                "没能从粘贴的内容里找到 SESSDATA。\n"
                "支持三种格式：Netscape 整行 / SESSDATA=xxx 键值对 / 直接粘贴值本身。")
        targets = [self.cfg.base_dir / self.cfg.cookies_file,
                   cfg_mod.project_root() / "cookies.txt"]
        written = []
        for p in targets:
            try:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(bili_api.build_netscape(sess), encoding="utf-8")
                written.append(str(p))
            except OSError:
                continue
        if not written:
            raise ValueError("cookies.txt 写入失败（检查目录权限）")
        # 重建 client，让新 cookie 立即生效
        self._client = None
        self._classifier = None
        return sess

    # ---------- 弹幕/评论校验 ----------
    def step_verify_comments(self, is_archive: bool = False,
                             only_new: bool = True) -> dict:
        """用弹幕+评论校验每条投稿（写 _verified_{mid}.json，判定器会读它）。"""
        from . import verify as verify_mod
        v = verify_mod.Verifier(self.cfg, self.client)
        results = []
        for acc in self.cfg.accounts:
            mid = int(acc.get("mid") or 0)
            name = str(acc.get("name") or mid)
            if not mid:
                continue
            self.log(f"── 校验 {name}（mid={mid}）──")
            try:
                r = v.run(mid, name,
                          is_archive=bool(is_archive or mid != self.cfg.self_mid),
                          only_new=only_new, log=self.log)
                results.append(r)
            except Exception as e:  # noqa: BLE001
                self.log(f"  {name} 失败：{e}")
                results.append({"mid": mid, "name": name, "err": str(e)})
        return {"accounts": results}

    # ---------- 歌名词典构建 ----------
    def step_build_songs(self) -> dict:
        """从分P名抽取真实歌名，扩充 _songs.txt。"""
        if not list(self.cfg.data_dir.glob("_pages_*.json")):
            raise RuntimeError("还没有分P缓存，请先「抓取」。")
        from . import fetcher as fetcher_mod
        r = fetcher_mod.build_songs_dict(self.cfg, log=self.log)
        # 词典变了 → 判定器要重建
        self._classifier = None
        return r

    def step_scan(self, pages: int = 22) -> list:
        """扫描：还有谁在切目标。"""
        rows = fetcher.scan_ups(self.cfg, self.client, pages=pages, log=self.log)
        out = self.cfg.data_dir / "_scan_ups_rank.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
        self.log("\n===== 未跟踪 UP 主，按近30天投稿数排序（前 40）=====")
        k = 0
        for r in rows:
            if r["tracked"]:
                continue
            k += 1
            if k > 40:
                break
            self.log(f"{k:2d}. {r['name'][:16]:16s} mid={r['mid']:12} "
                     f"30天={r['d30']:3d} 90天={r['d90']:3d} "
                     f"样本={r['n']:3d} 最新={r['latest_s']}")
        return rows

    def step_pending(self) -> int:
        """把过期文件移到「待删除」。"""
        return downloader.move_to_pending(self.cfg, log=self.log)


# ============================================================
# 日期辅助（rules 里没有，放这里方便 GUI 用）
# ============================================================
def fmt_date(ts) -> str:
    if not ts:
        return "-"
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d")
    except Exception:
        return "-"
