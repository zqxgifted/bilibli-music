# SPDX-License-Identifier: MIT
"""B 站接口访问层。

只用标准库（urllib），不引入 requests —— 打包 exe 时体积小、无依赖。

三个接口链路：
    1. x/web-interface/view?bvid=          取 aid / cid / 分P
    2. x/player/playurl?...&fnval=4048     取音轨（旧版非 WBI 接口，绕过风控）
    3. x/space/wbi/arc/search              空间投稿列表（**唯一会 412 的接口**）

风控要点（实测总结）：
    * 空间列表接口按 mid 限流，412 / -352 / -799 是 per-mid 限流，不是 IP 全封；
    * 下载接口（view / playurl / CDN）**不受**空间接口风控影响；
    * SESSDATA 过期时 nav 返回 -101，此时**降级为无 cookie 模式**反而能通。
"""
from __future__ import annotations

import hashlib
import http.cookiejar
import json
import os
import random
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# WBI 签名用的固定混淆表
TAB = [46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49, 33, 9, 42, 19, 29,
       28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4, 22,
       25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52]

# 浏览器指纹（空间接口要求）
FP = {
    "dm_img_list": "[]",
    "dm_img_str": "V2ViR0wgMS4wIChPcGVuR0wgRVMgMi4wIENocm9taXVtKQ",
    "dm_cover_img_str": ("QU5HTEUgKE5WSURJQSwgTlZJRElBIEdlRm9yY2UgUlRYIDQwNjAgTGFwdG9wIEdQVSBEaXJlY3QzRDEx"
                         "IHZzXzVfMCBwc181XzApLCBvciBzaW1pbGFy"),
    "dm_img_inter": '{"ds":[],"wh":[0,0,0],"of":[0,0,0]}',
}

# 音频档位优先级（数字越小越优先）
AUDIO_PRIORITY = {
    30251: 0,   # Hi-Res 无损
    30250: 1,   # 杜比全景声
    30255: 2,   # 杜比音效
    30280: 3,   # 192K
    30232: 4,   # 132K
    30216: 5,   # 64K
}
AUDIO_NAME = {30251: "Hi-Res", 30250: "杜比全景声", 30255: "杜比音效",
              30280: "192K", 30232: "132K", 30216: "64K", 0: "durl"}

# 用户可选的音质档
AUDIO_CHOICES = {
    "highest": None,     # None = 按 AUDIO_PRIORITY 自动挑最好的
    "hires": 30251,
    "dolby": 30250,
    "192k": 30280,
    "132k": 30232,
    "64k": 30216,
}

# 弹幕 protobuf 里抠中文串用：中文起头的连续可打印串
_CJK_RUN = re.compile(
    r'[一-鿿][一-鿿　-〿0-9A-Za-z_，。、！？!?…·\-：:,.《》（）()\[\] ]{1,60}')


class RateLimiter:
    """全局最小间隔，模拟人工节奏，避免 API 突发。"""

    def __init__(self, min_interval: float = 1.1):
        self.min_interval = min_interval
        self.lock = threading.Lock()
        self.next_at = 0.0

    def wait(self) -> None:
        if self.min_interval <= 0:
            return
        with self.lock:
            now = time.monotonic()
            if now < self.next_at:
                time.sleep(self.next_at - now)
                now = time.monotonic()
            self.next_at = now + self.min_interval + random.uniform(0, 0.3)


def _mk(o: list) -> str:
    out = ""
    for i in TAB:
        out += o[i]
    return out[:32]


def wbi_sign(params: dict, img_key: str, sub_key: str) -> dict:
    """给空间接口参数做 WBI 签名。"""
    mixin = _mk(list(img_key + sub_key))
    p = dict(params)
    p["wts"] = round(time.time())
    p = dict(sorted(p.items()))
    p = {k: ''.join(c for c in str(v) if c not in "!'()*") for k, v in p.items()}
    q = urllib.parse.urlencode(p)
    p["w_rid"] = hashlib.md5((q + mixin).encode()).hexdigest()
    return p


def load_sessdata(cookie_path) -> str:
    """从 Netscape 格式的 cookies 文件里读 SESSDATA。"""
    try:
        p = Path(cookie_path)
        if not p.is_file():
            return ""
        return parse_sessdata(p.read_text(encoding="utf-8", errors="ignore"))
    except OSError:
        return ""


def parse_sessdata(text: str) -> str:
    """从任意形式的 cookie 文本里抠出 SESSDATA。

    兼容三种粘贴来源（用户在浏览器里复制的格式不统一）：

    1. Netscape 格式（7 字段 Tab 分隔）：
       ``.bilibili.com\\tTRUE\\t/\\tFALSE\\t0\\tSESSDATA\\t<值>``
    2. 浏览器 document.cookie / DevTools 的原始串：
       ``buvid3=xxx; SESSDATA=yyy%2Czzz; bili_jct=www``
    3. 只粘了值本身（最长、含 %2C 的那一段）
    """
    if not text:
        return ""
    text = text.strip()

    # 形态 1：Netscape（含 Tab）
    if "\t" in text:
        for line in text.splitlines():
            if line.startswith("#") or not line.strip():
                continue
            parts = line.split("\t")
            if len(parts) >= 7 and parts[5] == "SESSDATA":
                v = parts[6].strip()
                if v:
                    return v

    # 形态 2：key=value; 串
    for m in re.finditer(r'(?:^|[;\s,])SESSDATA\s*=\s*([^;\s,"]+)', text):
        v = m.group(1).strip().strip('"\'')
        if v:
            return v

    # 形态 3：直接粘值（不含 = / Tab / ; 且长度足够）
    if "\t" not in text and "=" not in text and ";" not in text:
        v = (text.splitlines() or [""])[0].strip()
        if len(v) >= 40:
            return v
    return ""


def build_netscape(sessdata: str) -> str:
    """把 SESSDATA 值组装成 Netscape 格式文本（写 cookies.txt 用）。"""
    return ("# Netscape HTTP Cookie File\n"
            "# 由 bilimusic 写入。至少需要 SESSDATA 这一行。\n\n"
            + ".bilibili.com\tTRUE\t/\tFALSE\t0\tSESSDATA\t"
            + sessdata.strip() + "\n")


class BiliClient:
    """带 cookie 的 B 站会话。

    - `sessdata` 为空 → 无登录模式（仍可下 480P 以下，空间接口反而不容易 412）
    - `refresh()` 重新建立 cookie jar 并取 WBI 密钥
    """

    def __init__(self, sessdata: str = "", ua: str = UA, interval: float = 1.1):
        self.sessdata = sessdata or ""
        self.ua = ua
        self.limiter = RateLimiter(interval)
        self.cj = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cj))
        self.opener.addheaders = [("User-Agent", ua),
                                  ("Referer", "https://www.bilibili.com/")]
        self.ck = ""
        self.img_key = ""
        self.sub_key = ""
        self._view_cache: dict = {}
        self._view_lock = threading.Lock()
        self.cookie_expired = False
        self.refresh()

    # ---------- 会话 ----------
    def refresh(self) -> bool:
        """刷新 cookie 与 WBI 密钥。返回是否成功拿到密钥。

        ⚠️ SESSDATA 过期时 nav 返回 -101。此时**不要**丢掉 cookie ——
        下载接口（view/playurl/CDN）在无 cookie 时反而能通，只是拿不到
        大会员音质。这里只标记 `cookie_expired`，让上层给出明确提示。
        """
        self.cj = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cj))
        self.opener.addheaders = [("User-Agent", self.ua),
                                  ("Referer", "https://www.bilibili.com/")]
        try:
            self.opener.open("https://www.bilibili.com/", timeout=25).read()
        except Exception:
            pass
        jar = "; ".join(f"{c.name}={c.value}" for c in self.cj)
        self.ck = (jar + ("; " if jar else "") + f"SESSDATA={self.sessdata}") \
            if self.sessdata else jar
        self.cookie_expired = False
        try:
            nav = self.get("https://api.bilibili.com/x/web-interface/nav",
                           ref="https://www.bilibili.com/")
            # ⚠️ nav 即使未登录（code=-101）也会带 wbi_img —— 空间接口的签名密钥
            #    并不要求登录，所以**只要有 wbi_img 就取**，别被 code 卡住。
            wb = (nav.get("data") or {}).get("wbi_img") or {}
            if wb.get("img_url") and wb.get("sub_url"):
                self.img_key = wb["img_url"].split("/")[-1].split(".")[0]
                self.sub_key = wb["sub_url"].split("/")[-1].split(".")[0]
            if nav.get("code") == 0:
                return True
            if nav.get("code") == -101 and self.sessdata:
                # 账号未登录 = SESSDATA 过期；标记但保留 cookie
                self.cookie_expired = True
        except Exception:
            pass
        return bool(self.img_key)

    def get(self, url: str, ref: str = "https://www.bilibili.com/") -> dict:
        """GET 一个 JSON 接口。"""
        req = urllib.request.Request(url, headers={
            "User-Agent": self.ua,
            "Referer": ref,
            "Cookie": self.ck,
            "Origin": "https://space.bilibili.com",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
        })
        with self.opener.open(req, timeout=25) as r:
            return json.loads(r.read())

    # ---------- 账号状态 ----------
    def nav_info(self) -> dict:
        """查当前登录状态。返回 {logged_in, uname, mid, vip_type, vip_status, code}。

        这是「配置页显示已登录/未登录」的数据来源。
        """
        out = {"logged_in": False, "uname": "", "mid": 0,
               "vip_type": 0, "vip_status": 0, "code": None, "message": ""}
        try:
            j = self.get("https://api.bilibili.com/x/web-interface/nav",
                         ref="https://www.bilibili.com/")
            out["code"] = j.get("code")
            out["message"] = j.get("message") or ""
            d = j.get("data") or {}
            out["logged_in"] = bool(d.get("isLogin"))
            out["uname"] = str(d.get("uname") or "")
            out["mid"] = int(d.get("mid") or 0)
            out["vip_type"] = int(d.get("vipType") or 0)
            out["vip_status"] = int(d.get("vipStatus") or 0)
        except Exception as e:  # noqa: BLE001
            out["message"] = f"{type(e).__name__}: {e}"
        return out

    # ---------- 投稿列表 ----------
    def list_page(self, mid: int, pn: int, ps: int = 50):
        """取空间投稿的一页。返回 (vlist, total, ok)。

        ok=False 表示这一页确实没抓到（区别于翻到底）。
        """
        for attempt in range(7):
            try:
                p = {"mid": mid, "ps": ps, "pn": pn, "order": "pubdate",
                     "web_location": "333.1007", **FP}
                q = wbi_sign(p, self.img_key, self.sub_key)
                d = self.get(
                    "https://api.bilibili.com/x/space/wbi/arc/search?"
                    + urllib.parse.urlencode(q),
                    ref=f"https://space.bilibili.com/{mid}/video")
                if d.get("code") == 0:
                    vl = d["data"]["list"]["vlist"]
                    return vl, d["data"]["page"]["count"], True
            except Exception:
                pass
            # 退避：8s → 17s → 26s …（封顶 45s）
            time.sleep(min(45, 8 * (attempt + 1)) + random.random() * 6)
            if attempt in (1, 3, 5):
                self.refresh()
                time.sleep(12)
        return [], 0, False

    def search_page(self, keyword: str, pn: int, page_size: int = 30):
        """搜索视频（同页码）。返回 (result, ok)。"""
        try:
            p = {"search_type": "video", "keyword": keyword, "page": pn,
                 "page_size": page_size, "order": "pubdate", **FP}
            q = wbi_sign(p, self.img_key, self.sub_key)
            d = self.get(
                "https://api.bilibili.com/x/web-interface/wbi/search/type?"
                + urllib.parse.urlencode(q),
                ref="https://search.bilibili.com/all?keyword="
                    + urllib.parse.quote(keyword))
            if d.get("code") == 0:
                return (d.get("data") or {}).get("result") or [], True
            return [], False
        except Exception:
            return [], False

    # ---------- 评论 / 弹幕（给「校验」用） ----------
    def comments(self, aid: int, max_pages: int = 1) -> list:
        """取视频评论（含前 3 条子评论）。"""
        msgs: list = []
        for pn in range(1, max_pages + 1):
            try:
                d = self.get(
                    f"https://api.bilibili.com/x/v2/reply?type=1&oid={aid}"
                    f"&pn={pn}&sort=2",
                    ref=f"https://www.bilibili.com/video/av{aid}")
                if d.get("code") != 0:
                    break
                for r in (d.get("data", {}).get("replies") or []):
                    m = (r.get("content") or {}).get("message") or ""
                    if m:
                        msgs.append(m)
                    for sub in (r.get("replies") or [])[:3]:
                        sm = (sub.get("content") or {}).get("message") or ""
                        if sm:
                            msgs.append(sm)
            except Exception:  # noqa: BLE001
                break
        return msgs

    def danmaku(self, cid: int) -> list:
        """取第一段弹幕（protobuf 二进制里抠中文串）。

        seg.so 返回的是 gzip + protobuf，弹幕文本以 UTF-8 裸串夹在里面，
        直接按「中文起头的连续串」正则抠出来即可，不必引入 protobuf 依赖。
        """
        try:
            req = urllib.request.Request(
                f"https://api.bilibili.com/x/v2/dm/web/seg.so"
                f"?type=1&oid={cid}&segment_index=1",
                headers={"User-Agent": self.ua,
                         "Referer": "https://www.bilibili.com/",
                         "Cookie": self.ck})
            with self.opener.open(req, timeout=25) as r:
                seg = r.read()
            if seg[:2] == b"\x1f\x8b":
                import gzip
                seg = gzip.decompress(seg)
            txt = seg.decode("utf-8", "ignore")
            out, seen = [], set()
            for m in _CJK_RUN.findall(txt):
                m = m.strip()
                if m and m not in seen:
                    seen.add(m)
                    out.append(m)
            return out
        except Exception:  # noqa: BLE001
            return []

    # ---------- 视频信息 ----------
    def view(self, bvid: str, use_cache: bool = True) -> dict:
        """x/web-interface/view —— 取 aid / cid / 分P 列表。"""
        with self._view_lock:
            if use_cache and bvid in self._view_cache:
                return self._view_cache[bvid]
        j = self.get(f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}",
                     ref=f"https://www.bilibili.com/video/{bvid}")
        if j.get("code") != 0:
            raise RuntimeError(f"view 失败 {bvid}: {j.get('code')} {j.get('message')}")
        data = j["data"]
        with self._view_lock:
            self._view_cache[bvid] = data
        return data

    def playurl(self, aid: int, bvid: str, cid: int) -> dict:
        """x/player/playurl —— 取音轨地址（旧版非 WBI 接口）。"""
        self.limiter.wait()
        url = (f"https://api.bilibili.com/x/player/playurl?avid={aid}&bvid={bvid}"
               f"&cid={cid}&qn=127&type=&otype=json&fnver=0&fnval=4048"
               f"&fourk=1&platform=web")
        j = self.get(url, ref=f"https://www.bilibili.com/video/{bvid}")
        if j.get("code") != 0:
            raise RuntimeError(
                f"playurl 失败 {bvid} cid={cid}: {j.get('code')} {j.get('message')}")
        return j["data"]


# ============================================================
# 音轨挑选
# ============================================================
def audio_candidates(data: dict, priority: str = "highest") -> list:
    """把 dash.dolby / dash.flac / dash.audio / durl 摊平成候选列表，按优先级排序。

    priority:
        highest  最高音质（Hi-Res > 杜比 > 192K > 132K > 64K）
        hires    只要 Hi-Res
        dolby    只要杜比
        192k/132k/64k  指定档位
    """
    dash = data.get("dash") or {}
    out: list = []

    def _push(a: dict, force_id: int | None = None):
        if not a:
            return
        url = a.get("baseUrl") or a.get("base_url")
        if not url:
            return
        out.append({
            "id": force_id or a.get("id") or 0,
            "url": url,
            "bak": list(a.get("backupUrl") or a.get("backup_url") or []),
            "bw": int(a.get("bandwidth") or 0),
            "codec": (a.get("codecs") or a.get("codecid") or ""),
        })

    dol = dash.get("dolby") or {}
    for a in (dol.get("audio") or []):
        _push(a)
    _push(dash.get("flac") or {})
    for a in (dash.get("audio") or []):
        _push(a)
    if not out:
        for d in (data.get("durl") or []):
            _push({"baseUrl": d.get("url"), "backup_url": d.get("backup_url"),
                   "bandwidth": 0, "codecs": "mp4a"}, 0)

    want = AUDIO_CHOICES.get(priority, None)
    if want is not None:
        filtered = [x for x in out if x["id"] == want]
        if filtered:
            out = filtered
    out.sort(key=lambda x: (AUDIO_PRIORITY.get(x["id"], 9), -x["bw"]))
    return out


def pick_audio(data: dict, priority: str = "highest") -> dict:
    """挑一个音轨，返回候选字典。"""
    cands = audio_candidates(data, priority)
    if not cands:
        raise RuntimeError("既无 dash.audio 也无 durl")
    return cands[0]


def ext_of(codec: str) -> str:
    """FLAC 装不进 m4a，会落成 .flac。"""
    c = (codec or "").lower()
    if "flac" in c:
        return ".flac"
    return ".m4a"


# ============================================================
# CDN 下载
# ============================================================
def http_download(url: str, dest, referer: str = "https://www.bilibili.com",
                  sessdata: str = "", ua: str = UA, timeout: int = 60) -> int:
    """下载一个文件到 dest（覆盖写）。返回字节数。"""
    req = urllib.request.Request(url, headers={
        "User-Agent": ua,
        "Referer": referer,
        "Origin": "https://www.bilibili.com",
        "Cookie": f"SESSDATA={sessdata}" if sessdata else "",
    })
    total = 0
    with urllib.request.urlopen(req, timeout=timeout) as r, open(dest, "wb") as f:
        while True:
            chunk = r.read(256 * 1024)
            if not chunk:
                break
            f.write(chunk)
            total += len(chunk)
    return total


def download_with_backup(url: str, bak: list, dest, sessdata: str = "",
                         ua: str = UA) -> int:
    """依次尝试主 URL 和备用 CDN 镜像。"""
    err = None
    for u in [url] + list(bak or [])[:3]:
        if not u:
            continue
        try:
            n = http_download(u, dest, sessdata=sessdata, ua=ua)
            if n > 1024:
                return n
        except Exception as e:  # noqa: BLE001
            err = e
    raise RuntimeError(f"CDN 下载失败: {err}")


# ============================================================
# 辅助
# ============================================================
def len2sec(s) -> int:
    """'12:34' / '1:02:03' → 秒。"""
    try:
        parts = [int(x) for x in str(s).split(":")]
    except Exception:
        return 0
    n = 0
    for p in parts:
        n = n * 60 + p
    return n


def clean_html(s: str) -> str:
    """去掉搜索结果标题里的 <em class="keyword"> 高亮标签。"""
    return re.sub(r"</?em[^>]*>", "", s or "")
