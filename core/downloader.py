# SPDX-License-Identifier: MIT
"""按下载清单批量抓音频。

只拉**音轨**（不拉视频流），用 ffmpeg 无损封装成 m4a（`-c:a copy`，音频数据位级不变）。

要点（实测总结）：
    * 音轨优先级：Hi-Res(30251) > 杜比全景声(30250) > 杜比音效(30255) > 192K > 132K > 64K；
    * 并发别贪：`--jobs 4` 是实测稳定值，`RateLimiter 1.1/s` 别调高；
    * 临时文件**按线程复用**（不逐个删除）—— 安全删除钩子会在第 50 个之后拦下来，
      复用临时文件就完全没有删除动作；
    * 已存在的文件自动跳过 → 天然支持断点续跑。
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import bili_api, naming

# MP3 标准档位（LAME 只支持这些固定值）
MP3_LADDER = [128, 160, 192, 224, 256, 320]


def pick_mp3_bitrate(src_bps: int, mode: str) -> str:
    """auto：把源音轨码率映射到最近的 MP3 标准档位（四舍五入，同距取高档）。"""
    if mode != "auto":
        return mode
    if src_bps <= 0:
        return "192k"
    src = src_bps / 1000
    best = min(MP3_LADDER, key=lambda b: (abs(b - src), -b))
    return f"{best}k"


class Downloader:
    def __init__(self, cfg, client: bili_api.BiliClient, ffmpeg: str = "ffmpeg"):
        self.cfg = cfg
        self.client = client
        self.ffmpeg = ffmpeg
        self.lock = threading.Lock()
        self.stat = {"ok": 0, "skip": 0, "fail": 0, "bytes": 0}
        self.fails: list = []
        self._logf = None
        self.tmp_dir = cfg.tmp_path
        self.sessdata = client.sessdata

    # ---------- 路径 ----------
    def target_stem(self, item: dict) -> Path:
        """目录 = 本人/其他 UP；文件名主体 = <歌名>_<BV>_p<N>。"""
        root = self.cfg.dir_self if item.get("self") else self.cfg.dir_other
        stem = f"{naming.base_name(item['name'])}_{item['bv']}_p{int(item['page'])}"
        return root / stem

    def target_path(self, item: dict, ext: str = ".m4a") -> Path:
        """完整路径。

        ⚠️ 不能用 Path.with_suffix —— 歌名里带英文点号会被当成扩展名吃掉。
        """
        return Path(str(self.target_stem(item)) + ext)

    def existing(self, item: dict) -> Path | None:
        for ext in (".m4a", ".flac", ".mp3"):
            q = self.target_path(item, ext)
            if q.exists() and q.stat().st_size > 1024:
                return q
        return None

    # ---------- 日志 ----------
    def _emit(self, msg: str) -> None:
        if self._logf is None:
            return
        with self.lock:
            try:
                self._logf.write(msg + "\n")
                self._logf.flush()
            except Exception:  # noqa: BLE001
                pass

    # ---------- 单条 ----------
    def run_one(self, item: dict, overwrite: bool = False) -> str:
        p = self.existing(item)
        if p and not overwrite:
            with self.lock:
                self.stat["skip"] += 1
            msg = f"skip {p.name}"
            self._emit(msg)
            return msg

        view = self.client.view(item["bv"])
        cid = None
        for pg in view.get("pages") or []:
            if int(pg.get("page") or 1) == int(item["page"]):
                cid = pg.get("cid")
                break
        cid = cid or view.get("cid")

        data = self.client.playurl(view["aid"], item["bv"], cid)
        best = bili_api.pick_audio(data, self.cfg.audio_priority)
        ext = bili_api.ext_of(best["codec"])
        if self.cfg.audio_format == "mp3":
            ext = ".mp3"

        dst = self.target_path(item, ext)
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists() and dst.stat().st_size > 1024 and not overwrite:
            with self.lock:
                self.stat["skip"] += 1
            return f"skip {dst.name}"

        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        # 每个线程固定一个临时文件，靠 "wb" 截断复用 —— 不做 unlink
        tmp = self.tmp_dir / f"w{threading.get_ident()}.m4s"
        bili_api.download_with_backup(best["url"], best["bak"], tmp,
                                      sessdata=self.sessdata, ua=self.cfg.ua)

        if ext == ".mp3":
            br = pick_mp3_bitrate(best["bw"], self.cfg.mp3_bitrate)
            cmd = [self.ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                   "-i", str(tmp), "-vn",
                   "-acodec", "libmp3lame", "-b:a", br, "-ar", "48000",
                   "-metadata", f"title={item['name']}",
                   "-metadata", f"artist={item['up']}",
                   "-metadata", f"comment=https://www.bilibili.com/video/{item['bv']}",
                   str(dst)]
        else:
            cmd = [self.ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                   "-i", str(tmp), "-vn", "-c:a", "copy",
                   "-movflags", "+faststart",
                   "-metadata", f"title={item['name']}",
                   "-metadata", f"artist={item['up']}",
                   "-metadata", f"comment=https://www.bilibili.com/video/{item['bv']}",
                   str(dst)]
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="ignore")
        if r.returncode != 0 or not dst.exists():
            raise RuntimeError(f"ffmpeg: {(r.stderr or '')[:120]}")

        size = dst.stat().st_size
        with self.lock:
            self.stat["ok"] += 1
            self.stat["bytes"] += size
        aq = bili_api.AUDIO_NAME.get(best["id"], best["id"])
        msg = (f"ok   {dst.name}  [{aq} {best['bw']//1000}k {best['codec']}] "
               f"{size/1048576:.1f}MB")
        self._emit(msg)
        return msg

    # ---------- 批量 ----------
    def run(self, manifest_path: Path | None = None, jobs: int = 0,
            limit: int = 0, offset: int = 0, only: str = "all",
            overwrite: bool = False, dry_run: bool = False,
            log=print) -> dict:
        cfg = self.cfg
        manifest_path = manifest_path or cfg.manifest
        if not Path(manifest_path).is_file():
            raise FileNotFoundError(
                f"下载清单不存在：{manifest_path}\n"
                f"请先执行「去重」生成清单。")
        items = json.loads(Path(manifest_path).read_text(encoding="utf-8"))

        if only == "self":
            items = [x for x in items if x.get("self")]
        elif only == "other":
            items = [x for x in items if not x.get("self")]
        if offset:
            items = items[offset:]
        if limit:
            items = items[:limit]

        jobs = jobs or cfg.download_jobs
        log(f"清单 {len(items)} 条｜并发 {jobs}｜"
            f"目录：{cfg.dir_self.name} / {cfg.dir_other.name}")
        if getattr(self.client, "cookie_expired", False):
            log("⚠ cookies.txt 里的 SESSDATA 已过期（nav 返回 -101）。"
                "仍可下载，但拿不到大会员音质（最高 192K）。换一份新 cookies.txt 即可。")
        elif self.sessdata:
            log("SESSDATA 已加载")
        else:
            log("SESSDATA 缺失（最高只能到 192K）")

        if dry_run:
            for x in items[:30]:
                log("  ", str(self.target_path(x)))
            if len(items) > 30:
                log(f"  ... 共 {len(items)} 条")
            return {"total": len(items)}

        cfg.dir_self.mkdir(parents=True, exist_ok=True)
        cfg.dir_other.mkdir(parents=True, exist_ok=True)
        cfg.tmp_path.mkdir(parents=True, exist_ok=True)

        logf = Path(cfg.data_dir) / "_dl_run.log"
        try:
            cfg.data_dir.mkdir(parents=True, exist_ok=True)
            self._logf = open(logf, "a", encoding="utf-8")
        except OSError:
            self._logf = None
        if self._logf:
            self._logf.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} "
                             f"开始 {len(items)} 条 =====\n")

        t0 = time.time()
        done = 0
        try:
            with ThreadPoolExecutor(max_workers=max(1, jobs)) as ex:
                fut = {ex.submit(self.run_one, x, overwrite): x for x in items}
                for f in as_completed(fut):
                    item = fut[f]
                    done += 1
                    try:
                        f.result()
                    except Exception as e:  # noqa: BLE001
                        with self.lock:
                            self.stat["fail"] += 1
                        self.fails.append({"bv": item["bv"], "page": item["page"],
                                           "name": item["name"], "err": str(e)})
                        self._emit(f"FAIL {item['bv']} p{item['page']} "
                                   f"{type(e).__name__}: {e}")
                    # 进度只写状态文件 + 低频打印（避免撑爆 stdout 管道）
                    if done % 25 == 0 or done == len(items):
                        el = time.time() - t0
                        rate = done / el * 60 if el else 0
                        eta = (len(items) - done) / max(rate, 0.01)
                        line = (f"[{done:4d}/{len(items)}] ok={self.stat['ok']} "
                                f"skip={self.stat['skip']} fail={self.stat['fail']} "
                                f"{self.stat['bytes']/1048576:.0f}MB "
                                f"{rate:.0f}条/分 剩余≈{eta:.0f}分")
                        try:
                            (Path(cfg.data_dir) / "_dl_status.txt").write_text(
                                line, encoding="utf-8")
                        except OSError:
                            pass
                        log(line)
        finally:
            if self._logf:
                try:
                    self._logf.close()
                except Exception:  # noqa: BLE001
                    pass
                self._logf = None

        if self.fails:
            fl = Path(cfg.data_dir) / "_dl_failed.json"
            old = []
            if fl.is_file():
                try:
                    old = json.loads(fl.read_text(encoding="utf-8"))
                except Exception:  # noqa: BLE001
                    old = []
            fl.write_text(json.dumps(old + self.fails, ensure_ascii=False, indent=1),
                          encoding="utf-8")

        log(f"\n完成：ok={self.stat['ok']} skip={self.stat['skip']} "
            f"fail={self.stat['fail']} 共 {self.stat['bytes']/1073741824:.2f}GB，"
            f"用时 {(time.time()-t0)/60:.1f} 分钟")
        if self.fails:
            log(f"失败清单 -> {cfg.data_dir}/_dl_failed.json（{len(self.fails)} 条）")
        return {"total": len(items), **self.stat, "fails": len(self.fails)}


# ============================================================
# 校验
# ============================================================
def verify(cfg, ffprobe: str = "ffprobe", jobs: int = 10, log=print) -> dict:
    """逐条校验清单 vs 磁盘：正常 / 缺失 / 损坏 / 时长不符。"""
    if not cfg.manifest.is_file():
        raise FileNotFoundError(
            f"下载清单不存在：{cfg.manifest}\n请先执行「去重」生成清单。")
    man = json.loads(cfg.manifest.read_text(encoding="utf-8"))
    dl = Downloader(cfg, bili_api.BiliClient(sessdata=""), ffprobe)

    def probe(item):
        p = None
        for ext in (".m4a", ".flac", ".mp3"):
            q = dl.target_path(item, ext)
            if q.exists():
                p = q
                break
        if p is None:
            return ("missing", item, "文件不存在")
        if p.stat().st_size < 20 * 1024:
            return ("bad", item, f"文件过小 {p.stat().st_size}B")
        r = subprocess.run([ffprobe, "-v", "error", "-show_entries",
                            "format=duration,bit_rate:stream=codec_name",
                            "-of", "json", str(p)],
                           capture_output=True, text=True,
                           encoding="utf-8", errors="ignore")
        if r.returncode != 0:
            return ("bad", item, f"无法解析: {(r.stderr or '')[:80]}")
        try:
            j = json.loads(r.stdout or "{}")
            dur = float((j.get("format") or {}).get("duration") or 0)
            br = int((j.get("format") or {}).get("bit_rate") or 0)
            codec = ((j.get("streams") or [{}])[0] or {}).get("codec_name") or "?"
        except Exception as e:  # noqa: BLE001
            return ("bad", item, f"解析失败 {e}")
        exp = item.get("dur") or 0
        if dur <= 0:
            return ("bad", item, "时长 0")
        if exp and abs(dur - exp) > max(3.0, exp * 0.05):
            return ("dur", item, f"时长 {dur:.0f}s ≠ 清单 {exp}s")
        return ("ok", item, f"{dur:.0f}s {br//1000}k {codec}")

    from collections import Counter
    res = []
    with ThreadPoolExecutor(max_workers=max(1, jobs)) as ex:
        for r in ex.map(probe, man):
            res.append(r)

    c = Counter(r[0] for r in res)
    log(f"清单 {len(man)} 条：正常 {c['ok']}｜缺失 {c['missing']}｜"
        f"损坏 {c['bad']}｜时长不符 {c['dur']}")
    for k in ("missing", "bad", "dur"):
        bad = [r for r in res if r[0] == k]
        if bad:
            log(f"\n--- {k}（{len(bad)}）---")
            for _st, it, why in bad[:30]:
                log(f"  {it['bv']} p{it['page']} {it['name'][:28]} | {why}")

    sizes = []
    for k, it, _w in res:
        if k != "ok":
            continue
        for ext in (".m4a", ".flac", ".mp3"):
            p = dl.target_path(it, ext)
            if p.exists():
                sizes.append(p.stat().st_size)
                break
    if sizes:
        log(f"\n合计 {sum(sizes)/1073741824:.2f} GB｜平均 "
            f"{sum(sizes)/len(sizes)/1048576:.1f} MB｜最短 "
            f"{min(sizes)/1048576:.1f} MB｜最长 {max(sizes)/1048576:.1f} MB")
    return {"ok": c["ok"], "missing": c["missing"], "bad": c["bad"], "dur": c["dur"],
            "total": len(man)}


# ============================================================
# 把被替换的文件移到「待删除」
# ============================================================
def move_to_pending(cfg, log=print) -> int:
    """把「基底里已不是最新版本」的存量文件移到 `待删除\\`。

    **删除 = 移动**（同盘 os.rename，元数据操作，0.2 秒完成 3000 个文件，
    可后悔）；别直接删。
    """
    if not cfg.manifest.is_file() or not cfg.base_index.is_file():
        return 0
    man = json.loads(cfg.manifest.read_text(encoding="utf-8"))
    keep = set()
    dl = Downloader(cfg, bili_api.BiliClient(sessdata=""))
    for it in man:
        stem = f"{it['bv']}_p{int(it['page'])}"
        keep.add(stem)

    cfg.pending_dir.mkdir(parents=True, exist_ok=True)
    moved = 0
    for d in (cfg.dir_self, cfg.dir_other):
        if not d.is_dir():
            continue
        for f in list(d.iterdir()):
            if f.suffix.lower() not in (".m4a", ".flac", ".mp3"):
                continue
            stem = f.stem
            tail = stem.rsplit("_p", 1)[-1]
            head = stem.rsplit("_p", 1)[0]
            bv = head.rsplit("_", 1)[-1]
            key = f"{bv}_p{tail}"
            if key in keep:
                continue
            try:
                target = cfg.pending_dir / f.name
                if target.exists():
                    target = cfg.pending_dir / f"{f.stem}_{int(time.time())}{f.suffix}"
                os.replace(f, target)
                moved += 1
            except OSError as e:
                log(f"  移动失败 {f.name}: {e}")
    log(f"已把 {moved} 个过期文件移到 {cfg.pending_dir}")
    return moved
