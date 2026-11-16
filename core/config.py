# SPDX-License-Identifier: MIT
"""分层配置（Python 侧）。

优先级（高 -> 低）：
    1. 环境变量 BILI_CONFIG 指向的 JSON 文件
    2. config/user.json      用户私有（.gitignore）
    3. config/defaults.json  通用默认（进仓库）
    4. 内置默认值 _DEFAULTS

所有字段都有合理缺省值，**不会**因为没配就抛堆栈；缺关键项时在 GUI 里给出中文提示。

不含任何真实账号、设备号、本机绝对路径或会话信息。
"""
from __future__ import annotations

import json
import os
import re
import secrets
import sys
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# 明显是占位符的值一律视为「未配置」
_PLACEHOLDERS = {"", "<MID>", "REPLACE_WITH_MID", "YOUR_MID", "xxx", "示例目标"}

# ---------------------------------------------------------------- 内置默认值
_DEFAULTS: dict = {
    # ---- 目标 ----
    "target_name": "示例目标",
    "target_aliases": "示例目标",
    # ---- 路径（相对工作区；可写绝对路径）----
    "base_dir": "./workspace",
    # ---- 账号 ----
    "self_mid": 0,
    "accounts": [],            # [{"mid": 123, "name": "UP名"}, ...]
    "zl_only": [],            # 「标题是歌名=目标在唱」的频道 mid
    "priority_up": "",        # 同日多 UP 发同一场时优先保留谁
    "keep_all_songs": [],     # 例外：这些歌不同日期版本全保留
    "compilation_mids": [],   # 纯搬运合集号（整号排除）
    # ---- 黑名单 / 白名单 ----
    "exclude_kws_generic": ["整活"],
    "exclude_regex_generic": [
        r"(?i)(?<![A-Za-z0-9])see(?![-‐‑–—]?saw\b)(?!\s+you\b)(?=\s*[^A-Za-z\s])",
        r"(?i)(?<![A-Za-z0-9])see(?=\s+[A-Za-z][A-Za-z0-9]*[^\x00-\x7f])",
    ],
    "exclude_prefix_generic": ["手描"],
    "exclude_kws_project": [],
    "exclude_prefix_project": [],
    "exclude_unless_chorus_kws": [],   # 命中即排除，但含「合唱」豁免
    "include_kws": [],         # 白名单：子串（忽略空格）
    "include_prefix": [],      # 白名单：前缀（不带【】）
    "strong_sign_regex": "",   # 「目标在唱」的强署名正则（留空则按名字拼）
    "partner_shorthands": [],  # 合作对象圈内简称
    "other_names": "",         # 「纯他人」名单（正则）；标题含这些人名且不含目标 → ③
    "verified_label": "verified",  # _verified_*.json 里代表「确认是目标本唱」的标签名
    # ---- 人工强制判定（BV 号）----
    "force_cat1": [],
    "force_cat2": [],
    "force_cat3": [],
    "nonsong_bv": [],          # 人工核实「确认不是唱歌」的 BV 号
    # ---- 弹幕/评论校验（core/verify.py 用；留空则按别名自动拼保守正则）----
    "verify_mark_regex": "",      # 目标显式署名为主唱
    "verify_collab_regex": "",    # 合作/feat 署名
    "verify_sing_regex": "",      # 「目标+唱歌语境」（评论/弹幕）
    "verify_group_regex": "",     # 群体企划（命中→他人）
    "verify_role_regex": "",      # 角色向演唱（命中→他人）
    "verify_named_others": "",    # 他人唱见名单（正则，命中→他人）
    # ---- 标题净化（只影响文件名/显示名，不影响判定）----
    "sanitize": {
        "strip_emoji": True,          # 去 emoji（💃🎄🔥…）
        "normalize_fancy": True,      # 花体字母还原（𝐍𝐈𝐆𝐇𝐓→NIGHT）
        "strip_special": True,        # 去装饰符号（箭头/几何/颜文字/全角＾…）
        "strip_bracket_tags": True,   # 去【…】标签
        "strip_series_no": True,      # 去 #N 系列号（保护 心拍数#0822）
        "strip_lead_punct": True,     # 去开头标点（结尾语气符按 keep 选项）
        "keep_trailing_tone": True,   # 保留结尾 ~ 等语气符
        "extra_strip_chars": "",      # 额外要删的字符（如 ❤♥💃🎄🎁🔥◆💌🙏⚠️）
    },
    # ---- 按 UP 的清洗规则（正则表达式.txt 的「谁家的标题长什么样」）----
    # 形如 [{"up": "某UP名", "remove": ["😭", "【某某】"],
    #        "extract_regex": "歌回(.*?)-", "skip_parts": ["二次元专场", …]}]
    # remove: 文件名里直接删掉的字符串
    # extract_regex: 分P名匹配后取第 1 个捕获组当歌名（取不到则原样）
    # skip_parts: 分P名含这些词的不当歌处理（子串，忽略空格）
    "up_clean_rules": [],
    # ---- 判定阈值 ----
    "self_song_min_sec": 60,
    "self_song_max_sec": 330,
    "self_review_max_sec": 640,
    "songbook_min_parts": 4,
    # ---- 下载 ----
    "audio_priority": "highest",   # highest | hires | dolby | 192k | 132k | 64k
    "audio_format": "m4a",         # m4a | flac | mp3
    "mp3_bitrate": "auto",
    "download_jobs": 3,
    "api_interval": 1.1,           # API 最小间隔秒数
    "overwrite": False,
    # ---- 网络 ----
    "ua": DEFAULT_UA,
    "cookies_file": "cookies.txt",
    "ffmpeg": "ffmpeg",
    "ffprobe": "ffprobe",
    "tmp_dir": "",
}

_ENV_KEYS = {
    "target_name": "BILI_TARGET_NAME",
    "base_dir": "BILI_BASE_DIR",
    "self_mid": "BILI_SELF_MID",
    "priority_up": "BILI_PRIORITY_UP",
    "download_jobs": "BILI_DOWNLOAD_JOBS",
    "api_interval": "BILI_API_INTERVAL",
    "ffmpeg": "BILI_FFMPEG",
    "ffprobe": "BILI_FFPROBE",
    "ua": "BILI_UA",
}

_INT_KEYS = {
    "self_mid", "download_jobs", "self_song_min_sec", "self_song_max_sec",
    "self_review_max_sec", "songbook_min_parts",
}

_FLOAT_KEYS = {"api_interval"}

_LIST_KEYS = {
    "accounts", "zl_only", "keep_all_songs", "compilation_mids",
    "exclude_kws_generic", "exclude_regex_generic", "exclude_prefix_generic",
    "exclude_kws_project", "exclude_prefix_project", "exclude_unless_chorus_kws",
    "include_kws", "include_prefix", "partner_shorthands",
    "force_cat1", "force_cat2", "force_cat3", "nonsong_bv",
    "up_clean_rules",
}

_STR_KEYS = {
    "verify_mark_regex", "verify_collab_regex", "verify_sing_regex",
    "verify_group_regex", "verify_role_regex", "verify_named_others",
}
_BOOL_KEYS = {"overwrite"}


@dataclass
class Config:
    """运行时配置（已合并完毕）。"""

    target_name: str = "示例目标"
    target_aliases: str = "示例目标"
    base_dir: Path = Path("workspace")
    self_mid: int = 0
    accounts: list = field(default_factory=list)      # [{"mid": int, "name": str}]
    zl_only: list = field(default_factory=list)
    priority_up: str = ""
    keep_all_songs: list = field(default_factory=list)
    compilation_mids: list = field(default_factory=list)

    exclude_kws_generic: list = field(default_factory=list)
    exclude_regex_generic: list = field(default_factory=list)
    exclude_prefix_generic: list = field(default_factory=list)
    exclude_kws_project: list = field(default_factory=list)
    exclude_prefix_project: list = field(default_factory=list)
    exclude_unless_chorus_kws: list = field(default_factory=list)
    include_kws: list = field(default_factory=list)
    include_prefix: list = field(default_factory=list)
    strong_sign_regex: str = ""
    partner_shorthands: list = field(default_factory=list)
    other_names: str = ""
    verified_label: str = "verified"
    force_cat1: list = field(default_factory=list)
    force_cat2: list = field(default_factory=list)
    force_cat3: list = field(default_factory=list)
    nonsong_bv: list = field(default_factory=list)

    verify_mark_regex: str = ""
    verify_collab_regex: str = ""
    verify_sing_regex: str = ""
    verify_group_regex: str = ""
    verify_role_regex: str = ""
    verify_named_others: str = ""

    sanitize: dict = field(default_factory=lambda: dict(_DEFAULTS["sanitize"]))
    up_clean_rules: list = field(default_factory=list)

    self_song_min_sec: int = 60
    self_song_max_sec: int = 330
    self_review_max_sec: int = 640
    songbook_min_parts: int = 4

    audio_priority: str = "highest"
    audio_format: str = "m4a"
    mp3_bitrate: str = "auto"
    download_jobs: int = 3
    api_interval: float = 1.1
    overwrite: bool = False

    ua: str = DEFAULT_UA
    cookies_file: str = "cookies.txt"
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    tmp_dir: str = ""

    raw: dict = field(default_factory=dict, repr=False)

    # ---------------- 派生属性 ----------------
    @property
    def data_dir(self) -> Path:
        """缓存与清单目录。"""
        return self.base_dir / "_脚本与数据"

    @property
    def doc_dir(self) -> Path:
        """输出文档目录。"""
        return self.base_dir / "_文档"

    @property
    def list_dir(self) -> Path:
        """每个 UP 一份的三分类清单。"""
        return self.doc_dir / "清单"

    @property
    def dir_self(self) -> Path:
        """本人投稿的音频。"""
        return self.base_dir / f"_BILI{self.target_name}"

    @property
    def dir_other(self) -> Path:
        """二创 UP 的音频。"""
        return self.base_dir / f"_BILI{self.target_name}up歌切"

    @property
    def pending_dir(self) -> Path:
        """被替换/多余文件的暂存区（删除 = 移动到这里）。"""
        return self.base_dir / "待删除"

    @property
    def tmp_path(self) -> Path:
        """下载临时目录（.m4s 分片）。"""
        if self.tmp_dir:
            return Path(self.tmp_dir)
        return self.base_dir / "_tmp"

    @property
    def out_all(self) -> Path:
        """全时期版本文档。"""
        return self.doc_dir / f"{self.target_name}唱歌合集_去重.md"

    @property
    def out_latest(self) -> Path:
        """每首歌只留最新版本（主文档）。"""
        return self.doc_dir / f"{self.target_name}唱歌合集_每首歌只留最新版本.md"

    @property
    def manifest(self) -> Path:
        """基底驱动的下载清单。"""
        return self.data_dir / "_下载清单.json"

    @property
    def manifest_full(self) -> Path:
        """第一层去重后的全部演唱（备份/溯源）。"""
        return self.data_dir / "_下载清单_全量.json"

    @property
    def base_index(self) -> Path:
        """第二层去重结果（每首歌保留哪一条）。"""
        return self.data_dir / "_基底_每首歌最新.json"

    @property
    def songs_file(self) -> Path:
        """歌名词典。"""
        return self.data_dir / "_songs.txt"

    @property
    def alias_re(self) -> re.Pattern:
        """目标别名正则。"""
        return re.compile(self.target_aliases or self.target_name)

    @property
    def exclude_kws(self) -> list:
        """通用层 + 专属层关键词。"""
        return list(self.exclude_kws_generic) + list(self.exclude_kws_project)

    @property
    def exclude_prefix(self) -> list:
        """通用层 + 专属层前缀。"""
        return list(self.exclude_prefix_generic) + list(self.exclude_prefix_project)

    @property
    def strong_sign_re(self) -> str:
        """「目标在唱」的强署名正则；留空则按别名拼一个保守的。"""
        if self.strong_sign_regex:
            return self.strong_sign_regex
        aliases = self.target_aliases or self.target_name
        return rf"{aliases}翻唱|{aliases}版"

    def account_mids(self) -> list:
        return [int(a.get("mid") or 0) for a in self.accounts if a.get("mid")]

    def account_names(self) -> list:
        return [str(a.get("name") or "") for a in self.accounts]

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.doc_dir, self.list_dir,
                  self.dir_self, self.dir_other, self.pending_dir, self.tmp_path):
            d.mkdir(parents=True, exist_ok=True)

    def headers(self) -> dict:
        return {
            "User-Agent": self.ua,
            "Referer": "https://www.bilibili.com",
            "Origin": "https://www.bilibili.com",
            "Accept": "application/json, text/plain, */*",
        }

    def is_configured(self) -> bool:
        """是否已经配好了目标与账号（GUI 用它决定要不要提示用户）。"""
        return bool(self.accounts) and self.target_name not in _PLACEHOLDERS


# ---------------------------------------------------------------- 读取
def project_root() -> Path:
    """项目根目录（core/ 的上一级）。"""
    return Path(__file__).resolve().parent.parent


def _read_one(p: Path) -> dict:
    """读单个 JSON 配置，失败返回空字典。"""
    if not p.is_file():
        return {}
    try:
        # utf-8-sig 兼容 Windows 工具生成的带 BOM 文件
        data = json.loads(p.read_text(encoding="utf-8-sig"))
        return {k: v for k, v in data.items() if not k.startswith("_")} \
            if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as e:
        print(f"[config] 无法读取 {p}: {e}", file=sys.stderr)
        return {}


def _read_file_cfg() -> dict:
    """按优先级合并配置文件。"""
    root = project_root()
    merged: dict = {}
    merged.update(_read_one(root / "config" / "defaults.json"))
    merged.update(_read_one(root / "config" / "user.json"))

    env_path = os.environ.get("BILI_CONFIG", "")
    if env_path:
        merged.update(_read_one(Path(env_path)))
    return merged


def _as_list(v) -> list:
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        return list(v)
    if isinstance(v, str):
        # 允许 "a, b, c" 或 "a\nb\nc" 两种写法
        parts = [x.strip() for x in re.split(r"[,\n]", v)]
        return [x for x in parts if x]
    return [v]


def _as_int(v, default: int) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _as_float(v, default: float) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _normalize_accounts(v) -> list:
    """accounts 支持两种写法：
        [{"mid": 123, "name": "UP"}, ...]
        [[123, "UP"], ...]
        "123:UP,456:UP2"
    """
    out: list = []
    for item in _as_list(v):
        if isinstance(item, dict):
            mid = _as_int(item.get("mid") or item.get("id"), 0)
            name = str(item.get("name") or item.get("title") or mid)
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            mid = _as_int(item[0], 0)
            name = str(item[1])
        elif isinstance(item, str) and ":" in item:
            head, _, tail = item.partition(":")
            mid = _as_int(head, 0)
            name = tail.strip()
        else:
            mid = _as_int(item, 0)
            name = str(mid)
        if mid:
            out.append({"mid": mid, "name": name})
    return out


def _normalize_int_list(v) -> list:
    return [_as_int(x, 0) for x in _as_list(v) if _as_int(x, 0)]


def load(root: Path | None = None) -> Config:
    """加载并合并出最终配置。"""
    root = root or project_root()
    file_cfg = _read_file_cfg()
    merged = dict(_DEFAULTS)
    merged.update({k: v for k, v in file_cfg.items() if v is not None})

    # 环境变量覆盖
    for key, env in _ENV_KEYS.items():
        val = os.environ.get(env)
        if val is not None and val != "":
            merged[key] = val

    for key in _INT_KEYS:
        merged[key] = _as_int(merged.get(key), _DEFAULTS[key])
    for key in _FLOAT_KEYS:
        merged[key] = _as_float(merged.get(key), _DEFAULTS[key])
    for key in _LIST_KEYS:
        merged[key] = _as_list(merged.get(key))
    for key in _BOOL_KEYS:
        merged[key] = bool(merged.get(key))
    for key in _STR_KEYS:
        merged[key] = str(merged.get(key) or "")

    # sanitize：允许 user.json 只写部分键，其余用默认值补齐
    san = dict(_DEFAULTS["sanitize"])
    if isinstance(merged.get("sanitize"), dict):
        san.update(merged["sanitize"])
    merged["sanitize"] = san

    merged["accounts"] = _normalize_accounts(merged.get("accounts"))
    merged["zl_only"] = _normalize_int_list(merged.get("zl_only"))
    merged["compilation_mids"] = _normalize_int_list(merged.get("compilation_mids"))

    base = str(merged.get("base_dir") or "").strip()
    if not base or base in {".", ".."}:
        base = str(root / "workspace")
    base_path = Path(base).expanduser()
    if not base_path.is_absolute():
        base_path = (root / base_path).resolve()

    target_name = str(merged.get("target_name") or "").strip() or "示例目标"
    aliases = str(merged.get("target_aliases") or "").strip() or target_name

    return Config(
        target_name=target_name,
        target_aliases=aliases,
        base_dir=base_path,
        self_mid=merged["self_mid"],
        accounts=merged["accounts"],
        zl_only=merged["zl_only"],
        priority_up=str(merged.get("priority_up") or ""),
        keep_all_songs=[str(x) for x in merged["keep_all_songs"]],
        compilation_mids=merged["compilation_mids"],

        exclude_kws_generic=[str(x) for x in merged["exclude_kws_generic"]],
        exclude_regex_generic=[str(x) for x in merged["exclude_regex_generic"]],
        exclude_prefix_generic=[str(x) for x in merged["exclude_prefix_generic"]],
        exclude_kws_project=[str(x) for x in merged["exclude_kws_project"]],
        exclude_prefix_project=[str(x) for x in merged["exclude_prefix_project"]],
        exclude_unless_chorus_kws=[str(x) for x in merged["exclude_unless_chorus_kws"]],
        include_kws=[str(x) for x in merged["include_kws"]],
        include_prefix=[str(x) for x in merged["include_prefix"]],
        strong_sign_regex=str(merged.get("strong_sign_regex") or ""),
        partner_shorthands=[str(x) for x in merged["partner_shorthands"]],
        other_names=str(merged.get("other_names") or ""),
        verified_label=str(merged.get("verified_label") or "verified"),
        force_cat1=[str(x) for x in merged["force_cat1"]],
        force_cat2=[str(x) for x in merged["force_cat2"]],
        force_cat3=[str(x) for x in merged["force_cat3"]],
        nonsong_bv=[str(x) for x in merged["nonsong_bv"]],
        verify_mark_regex=merged["verify_mark_regex"],
        verify_collab_regex=merged["verify_collab_regex"],
        verify_sing_regex=merged["verify_sing_regex"],
        verify_group_regex=merged["verify_group_regex"],
        verify_role_regex=merged["verify_role_regex"],
        verify_named_others=merged["verify_named_others"],
        sanitize=merged["sanitize"],
        up_clean_rules=merged["up_clean_rules"],

        self_song_min_sec=merged["self_song_min_sec"],
        self_song_max_sec=merged["self_song_max_sec"],
        self_review_max_sec=merged["self_review_max_sec"],
        songbook_min_parts=merged["songbook_min_parts"],

        audio_priority=str(merged.get("audio_priority") or "highest"),
        audio_format=str(merged.get("audio_format") or "m4a"),
        mp3_bitrate=str(merged.get("mp3_bitrate") or "auto"),
        download_jobs=merged["download_jobs"],
        api_interval=merged["api_interval"],
        overwrite=merged["overwrite"],

        ua=str(merged.get("ua") or DEFAULT_UA),
        cookies_file=str(merged.get("cookies_file") or "cookies.txt"),
        ffmpeg=str(merged.get("ffmpeg") or "ffmpeg"),
        ffprobe=str(merged.get("ffprobe") or "ffprobe"),
        tmp_dir=str(merged.get("tmp_dir") or ""),
        raw=file_cfg,
    )


def save_user(cfg: Config, root: Path | None = None) -> Path:
    """把可编辑字段写回 config/user.json（私有，不进仓库）。"""
    root = root or project_root()
    p = root / "config" / "user.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "_comment": "本文件是用户私有配置，不要提交到 GitHub。",
        "target_name": cfg.target_name,
        "target_aliases": cfg.target_aliases,
        "base_dir": str(cfg.base_dir),
        "self_mid": cfg.self_mid,
        "accounts": cfg.accounts,
        "zl_only": cfg.zl_only,
        "priority_up": cfg.priority_up,
        "keep_all_songs": cfg.keep_all_songs,
        "compilation_mids": cfg.compilation_mids,
        "exclude_kws_generic": cfg.exclude_kws_generic,
        "exclude_regex_generic": cfg.exclude_regex_generic,
        "exclude_prefix_generic": cfg.exclude_prefix_generic,
        "exclude_kws_project": cfg.exclude_kws_project,
        "exclude_prefix_project": cfg.exclude_prefix_project,
        "exclude_unless_chorus_kws": cfg.exclude_unless_chorus_kws,
        "include_kws": cfg.include_kws,
        "include_prefix": cfg.include_prefix,
        "strong_sign_regex": cfg.strong_sign_regex,
        "partner_shorthands": cfg.partner_shorthands,
        "other_names": cfg.other_names,
        "verified_label": cfg.verified_label,
        "force_cat1": cfg.force_cat1,
        "force_cat2": cfg.force_cat2,
        "force_cat3": cfg.force_cat3,
        "nonsong_bv": cfg.nonsong_bv,
        "verify_mark_regex": cfg.verify_mark_regex,
        "verify_collab_regex": cfg.verify_collab_regex,
        "verify_sing_regex": cfg.verify_sing_regex,
        "verify_group_regex": cfg.verify_group_regex,
        "verify_role_regex": cfg.verify_role_regex,
        "verify_named_others": cfg.verify_named_others,
        "sanitize": cfg.sanitize,
        "up_clean_rules": cfg.up_clean_rules,
        "self_song_min_sec": cfg.self_song_min_sec,
        "self_song_max_sec": cfg.self_song_max_sec,
        "self_review_max_sec": cfg.self_review_max_sec,
        "songbook_min_parts": cfg.songbook_min_parts,
        "audio_priority": cfg.audio_priority,
        "audio_format": cfg.audio_format,
        "mp3_bitrate": cfg.mp3_bitrate,
        "download_jobs": cfg.download_jobs,
        "api_interval": cfg.api_interval,
        "overwrite": cfg.overwrite,
        "cookies_file": cfg.cookies_file,
        "ffmpeg": cfg.ffmpeg,
        "ffprobe": cfg.ffprobe,
        "tmp_dir": cfg.tmp_dir,
    }
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


if __name__ == "__main__":
    c = load()
    print(json.dumps({
        "target_name": c.target_name,
        "base_dir": str(c.base_dir),
        "data_dir": str(c.data_dir),
        "doc_dir": str(c.doc_dir),
        "dir_self": str(c.dir_self),
        "dir_other": str(c.dir_other),
        "accounts": c.accounts,
        "self_mid": c.self_mid,
        "configured": c.is_configured(),
    }, ensure_ascii=False, indent=2))
