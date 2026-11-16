# SPDX-License-Identifier: MIT
"""文件名 / 歌名清洗。

三条铁律（都踩过坑）：
  1. Windows 文件名不能含 ``\\ / : * ? " < > |``；
  2. **不能用 Path.with_suffix** —— 歌名里带英文点号（如「右肩の蝶(レンver.)」）
     会被当成扩展名吃掉；
  3. 分P名可能是「001.某某-爱啦啦」「录制-883263-20260912-200835-501」
     这种无意义串，必须先判「有没有意义」再决定用不用。
"""
from __future__ import annotations

import re

# Windows 非法字符
_BAD = re.compile(r'[\\/:*?"<>|\r\n\t]')

# 【…】标签（最多 26 字）
_TAG = re.compile(r'[【\[［][^】\]］]{0,26}[】\]］]')

# 开头的标点/emoji（只剥开头，结尾的语气符如 ~ 要保留）
_LEAD_PUNCT = r'^[\s·,，。.、~～\-—_｜|！!？?：:；;]+'

# 修饰括号：（完整版）（纯享版）（某某视角）（cover）…
_DECOR = re.compile(
    r'[（(]\s*(?:\S{0,8}视角|完整版?|纯享版?|现场版?|官方版?|字幕版?|'
    r'无画面版?|剪辑版?|cover|Cover|翻唱版?)\s*[）)]',
    re.IGNORECASE,
)

# 无意义的分P名（不是歌名）
_NOISE_PART = re.compile(
    r'^(?:(?:p|P|part|Part)?\s*\d+|正片|纯享版?|完整版?|现场版?|原片|本片|'
    r'花絮|预告|上集|下集|全集|合集|无画面|字幕版?|vocal[：:].*)$',
    re.IGNORECASE,
)

# 序号前缀：'001.某某-爱啦啦' / '12、青鸟'
_SEQ = re.compile(r'^\s*\d{1,4}\s*[.、．,，)）\-_ ]\s*')


def safe_name(s: str, limit: int = 90) -> str:
    """通用安全文件名（非法字符替换成空格）。"""
    s = _BAD.sub(" ", str(s or ""))
    s = re.sub(r"\s+", " ", s).strip(" .")
    return s[:limit].rstrip(" .") or "untitled"


# ============================================================
# 净化策略开关（GUI 配置页可改；默认 = 历史行为，改完要重跑去重才生效）
# ============================================================
SANITIZE = {
    "strip_emoji": True,          # 去 emoji
    "normalize_fancy": True,      # 花体字母还原
    "strip_special": True,        # 去装饰符号
    "strip_bracket_tags": True,   # 去【…】标签
    "strip_series_no": True,      # 去 #N 系列号
    "strip_lead_punct": True,     # 去开头标点
    "keep_trailing_tone": True,   # 保留结尾语气符
    "extra_strip_chars": "",      # 额外要删的字符
}


def set_sanitize(opts: dict) -> None:
    """由 GUI / pipeline 在启动时注入净化策略。"""
    if opts:
        for k in SANITIZE:
            if k in opts:
                SANITIZE[k] = opts[k]


def get_sanitize() -> dict:
    return dict(SANITIZE)


def base_name(raw: str, limit: int = 60) -> str:
    """标题 → 干净的文件名主体。

    处理顺序（顺序很重要，不能换）：
      1. 去【…】标签 / emoji / 特殊符号
      2. 去 #N 系列号（保护「心拍数#0822」这种歌名本身带 # 的）
      3. 压空白
      4. 只剥**开头**标点（结尾的 ~ 是原标题语气符，保留）

    每一步都受 SANITIZE 开关控制（默认全开 = 历史行为）。
    """
    san = SANITIZE
    s = _TAG.sub(" ", raw or "") if san["strip_bracket_tags"] else (raw or "")
    s = _BAD.sub(" ", s)
    if san["extra_strip_chars"]:
        s = s.translate({ord(c): " " for c in san["extra_strip_chars"]})
    # 「#2 《园游会》…」→ 去掉 #N；注意「心拍数#0822」不受影响（# 后没跟空格）
    if san["strip_series_no"]:
        s = re.sub(r'#\d+(?=\s)', ' ', s)
    s = re.sub(r"\s+", " ", s).strip(" .-_")

    # 例外判定要在「去开头标点」**之前**做：
    # 去标签后若开头就是标点（如「【某某/直播翻唱】【就是爱X】，爱着X~」→
    # 「，爱着X~」），说明紧跟着的标签其实是标题的一部分，
    # 退回「只去第一个标签、其余标签保留文字」。
    if san["strip_lead_punct"] and (
            not s or re.match(r"^[\s·,，。.、~～\-—_｜|！!？?：:；;]", s)):
        t = re.sub(r"^\s*[【\[][^】\]]*[】\]]", "", (raw or ""), count=1)
        if san["strip_bracket_tags"]:
            t = re.sub(r"[【\[]([^】\]]*)[】\]]", r"\1", t)
        t = _BAD.sub(" ", t)
        if san["strip_series_no"]:
            t = re.sub(r'#\d+(?=\s)', ' ', t)
        t = re.sub(r"\s+", " ", t)
        t = re.sub(rf"{_LEAD_PUNCT}", "", t).strip(" .-_")
        if len(t) >= 2:
            s = t

    if san["strip_lead_punct"]:
        s = re.sub(_LEAD_PUNCT, "", s)
    if not san["keep_trailing_tone"]:
        s = re.sub(r'[~～!！?？。\.]+$', '', s)
    s = s[:limit].rstrip(" .-_")
    return s or "未命名"


def part_name(part: str) -> str:
    """分P名 → 歌名；无意义时返回空串（调用方回退总标题）。"""
    from .rules import clean_part  # 延迟导入，避免循环依赖
    x = clean_part(part or "")
    x = _DECOR.sub('', x).strip()
    if not x or _NOISE_PART.match(x):
        return ""
    return x


def norm_song(s: str, strip_target_prefix: str = "") -> str:
    """歌名 → 归一化去重键。

    顺序很重要：
      1. 先 `rules.norm()` —— 去空白/零宽字符/emoji 变体选择符，还原花体字母；
      2. 再去序号前缀（「001.某某-爱啦啦」「12、青鸟」）；
      3. 去修饰括号、去「纯享版/完整版/cut/cover」等后缀；
      4. 去标点、转小写。

    ⚠️ 第 1 步不能省：少了它，「❤️萌妹」与「❤萌妹」会算成两首歌，
       去重时互相顶替，磁盘上就会多出一堆重复文件。
    """
    from .rules import norm  # 延迟导入，避免循环依赖
    s = norm(s or '')
    s = _SEQ.sub('', s)
    if strip_target_prefix:
        s = re.sub(rf'^(?:{strip_target_prefix})\s*[-－—:：]\s*', '', s)
    s = re.sub(r'[（(][^）)]{0,20}[）)]', '', s)
    s = re.sub(r'(纯享版|完整版|现场版|翻唱版|DJ版|dj版|版|片段|cut|Cut|CUT|cover|Cover)$',
               '', s)
    s = re.sub(r'[《》「」“”⚡!！?？。，,、~～\-—_|｜]', '', s)
    s = re.sub(r'\s+', '', s)
    return s.strip().lower()


def target_stem(root, name: str, bv: str, page: int, ext: str) -> str:
    """完整文件名：``<歌名>_<BV号>_p<分P号><扩展名>``。

    返回**完整字符串**而不是 Path，因为歌名里带点号时 with_suffix 会出错。
    """
    stem = f"{base_name(name)}_{bv}_p{int(page)}"
    return f"{root}{stem}{ext}"
