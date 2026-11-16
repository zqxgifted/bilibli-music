# SPDX-License-Identifier: MIT
"""判定规则引擎（标题 → 是不是目标在唱歌）。

这是整个工具最核心、也最容易出错的部分。三类判定：

    ① 目标本人唱 / 与他人合唱    → 收进库
    ② 疑似有目标唱              → 需要人工确认
    ③ 与唱歌无关                → 不收

本模块**不含任何具体目标的名字**，全部走 config 注入的正则与词表。
判定逻辑由真实投稿（9000+ 条）实战打磨而来，改动前请先看 docs/RULES.md。
"""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from functools import lru_cache

from . import naming

# ============================================================
# 通用词表（与目标无关，换人可整段带走）
# ============================================================

# 开头【XXX】：若是「人」的名字（不是活动/游戏/格式标签），且全标题不含目标名 → 别人的独唱
HEAD_BRACKET = re.compile(r'^[【\[]([^】\]]{1,14})[】\]]')
NON_PERSON = re.compile(
    r'翻唱|合唱|大合唱|十人|众歌姬|模型展示|Live2D|艾尔登|法环|仙剑|原神|星穹|崩坏|英雄联盟|王者|'
    r'拜年|祭|新春|生日|周年|纪念|出征|企划|联动|预告|全程配音|手描き|[Ii]b$|4K|HD|BLG|鹅鸭杀|'
    r'开箱|货币战争|荒岛|深夜纯享|震撼内幕|独家捕捉|八千播客|CICF|直播|回放|切片|修复|画质|'
    r'中字|字幕|完结|合集|教程|测评|纯享|恶搞|整活|^\d{3,4}[Pp]|^P\d|^\d+[Kk]$|^\d{6,8}$')

# 「连唱/串烧/系列歌」等强唱歌信号
# ⚠️ 别删：少了它，「注视着你连唱20分钟…」这类会被判成 ③（应为 ①）
SING_EXTRA = re.compile(
    r'串烧|连唱|情歌|歌单|金曲|神曲|单曲|新曲|歌力|镇站之歌|热唱|歌って|'
    r'这首歌|这首|整首|全曲|'
    # 「王菲经典四连」「十连唱」「十六连唱！」= 连着唱 N 首
    r'[一二三四五六七八九十两\d]{1,3}连(?=[唱歌曲！!，,。、\s]|$)|'
    # 「某某情歌王第三弹」「第二弹」= 歌曲系列第 N 期
    r'第[一二三四五六七八九十\d]{1,3}[弹季]')

# 「纯他人」名单：标题含这些人名且不含目标名 → ③。
#
# ⚠️ 这是**目标专属**内容，由 config.other_names 注入。
#    默认留空 = 不做这层判断（换目标时保持空即可，需要时再填）。
#    例：填 `某某歌手|某组合` → 标题出现这些名字而没出现目标名，判 ③。

# 群体企划/多人合作（不确定目标是否参与 → ②）
GROUP = re.compile(r'UP合唱|出征曲|拜年祭|十人|众歌姬|这群人|合白|BML|全体合唱|大合唱')

# 引号里这些词说明是节目/游戏/活动/梗，不是歌名
NOT_SONG_Q = re.compile(
    r'对决|挑战|大赛|比赛|烧烤|美食|游戏|综艺|节目|采访|广告|直播|教程|测评|攻略|'
    r'梗|用语|词汇|说法|口头禅|视频|片段|合集|歌单|演唱会|音乐会|舞台|MV|动画|电影|电视剧|'
    r'下一个是谁|神人最多|一集|第.{1,3}集|次元干杯|'
    r'会议|小记|工作|启动|加油|助威|上班|下班|打卡|展示|模型')

# 唱歌动作
SING_WORD = re.compile(r'唱|翻唱|合唱|cover|Cover|COVER|歌って|歌った|歌|曲')

# 强非唱歌（游戏/直播/反应/谈话/跳舞等）
# ⚠️ 不要放「干杯/游戏/主播/老板/比赛」这类会出现在普通词/歌词里的词
#    —— 曾因「干杯」误杀《干杯》、因「游戏」误杀《万有引力》《是丘比特安排的游戏》
HARD_NON = re.compile(
    r'实机|演示|试玩|测评|攻略|通关|开箱|游戏实况|实况|直播回放|直播录像|录像|录播|录屏|'
    r'变声器|AI读|读小说|笑声大全|杂谈|聊天|日常|vlog|Vlog|reaction|采访|访谈|预告片|宣传片|'
    r'跳舞|跳《|舞蹈|擦边|福利|联机|开黑|上分|解说|复盘|整蛊|装机|美食|做饭|深夜开播|'
    r'无声|鹅叫|鬼畜|SEE|人类迷惑行为|大赏|'
    r'家访|探店|影视飓风|潜入|电竞|键盘|鼠标|职业哥|航天|'
    r'满命|草神|抽卡|探班|幕后|花絮|发布会|直播切片|ASMR|'
    r'有声小说|有声书|听书|评书|广播剧|相声|无损音质|后台播放|原版 ?MV|MV整理|全集MV|'
    r'官摄|打歌舞台|KTV|伴奏|铃声|车载|HIFI|Hifi|分集播放|'
    r'演唱会|音乐会|歌友会|签售|门票')

# 线下活动 / vlog（只在「标题没有《歌名》」时才生效）
VLOG_NON = re.compile(
    r'漫展|车模|爆改|夺冠|工作会议|小记|打卡|旅行|吃播|探店|'
    r'次元干杯|好多人啊|第一次去|全记录|花絮|'
    r'助威|第一次吃|定制cos服|模型展示')

# 反应类（Re / See / 反应 / 跳舞）
# 「看《X》」只在**分句开头**且不是「看着/看完/看到/看看」时才算反应，
# 否则会误杀大量歌词式标题
REACT = re.compile(
    r'Re(?=[一-龥])|Re[：:《]|re《|reaction|反应|锐评|吐槽|[Ss]ee《|[Ss]ee[“「]|跳《|跳舞|追剧|'
    r'(?:^|[】\]]|[，,。！!？?])\s*看(?![着完了到见看])[^《「“]{0,12}?[《「“]')

# 唱歌动作 / 弱标记
PERSONAL = re.compile(
    r'试唱|挑战唱|我唱|唱了|唱起|献唱|翻唱|弹唱|清唱|开嗓|首唱|接唱|原创曲|'
    r'唱一下|唱成什么样|献歌|歌った|を歌った|戏腔|小唱|合唱|演绎|唱rap|'
    r'唱轻涟|唱《|唱「|给粉丝写了首歌|这样唱|能唱|唱得|唱成|吉他手|歌回|歌切|想唱|街唱')

# 本人号里「时长像单曲但其实不是歌」的词
SELF_TALK = re.compile(
    r'变声器|声卡|跳舞|杂谈|抽卡|开箱|家访|vlog|Vlog|访谈|采访|测评|读评论|回私信|'
    r'模型展示|Live2D|定制cos服|助威|第一次吃')

# 「听/看 + 人名 + 《歌名》」= 听别人唱
# ⚠️ 不能写成一条裸正则 —— 「听完这首《X》」「听某某的《X》」都是本人翻唱。
#    必须二次过滤（排除功能词与目标本人），见 hit_listen_other()。
LISTEN_RE = re.compile(r"(?:听|看)([一-鿿]{2,6})(?:唱|翻唱|演唱)?[《「“]")
LISTEN_BLOCK = (
    "的", "我", "你", "他", "她", "它", "这", "那", "边", "再", "来", "去",
    "完", "着", "到", "说", "见", "懂", "起", "过", "了", "得", "出", "成",
    "不", "好", "想", "首", "遍", "一", "万人", "热歌", "女声", "合集",
    "安利", "身边", "古风", "日语", "钢琴", "伤感", "歌曲", "小曲", "灯光",
    "兔娘", "对象", "大学", "同事", "同学", "老板", "朋友", "新主线", "翻唱版",
)
CHORUS_HINTS = ("合唱",)

# 表情/装饰符号
EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U00002B00-\U00002BFF"
    "\U0000FE00-\U0000FE0F"
    "\U0000200B-\U0000200F"
    "\U000020E3"
    "]+", flags=re.UNICODE)

# 装饰性特殊符号
SPECIAL = re.compile(
    "["
    "\u2190-\u21FF"     # 箭头
    "\u25A0-\u25FF"     # 几何图形
    "\u2B00-\u2BFF"     # 杂项数学 A
    "\u2260-\u226F"     # 数学运算符
    "\uFF61-\uFF9F"     # 半角片假名（颜文字）
    "\u0400-\u04FF"     # 西里尔
    "\u3100-\u312F"     # 注音
    "\uFF5E"            # 全角 ＾
    "]+")
_PAREN = re.compile(r'\([^()（）]{0,30}\)')

# ⚡歌名⚡ 形式（如 ⚡雨爱⚡）—— 多为歌回切片，视为唱歌
SPARK = re.compile(r'⚡[^⚡]{1,30}⚡')

# 分P名像视频标题不像歌名
PART_TITLE = re.compile(
    r'[？?]|怎么|为什么|如何|是什么|体验|攻略|教程|盘点|'
    r'第.{1,3}[期集]|合集|上集|下集|预告|花絮|reaction|Re[：:]')

# 分P名里的垃圾词
PARTS_BAD = re.compile(
    r'聊天|杂谈|开场|等待|抽奖|回放|录播|录屏|休息|广告|恰饭|读|看|说|聊|谢|'
    r'歌单|预告|封面|测试|调试|设备|弹幕')

# 无意义的分P名（日期/编号/录制串/工程名…）
JUNK_PART = re.compile(
    r'^\s*$'
    r'|^录制[-_ ]'
    r'|^20\d{6}'
    r'|^\d{10,}$'
    r'|^20\d{2}[-./年]\d{1,2}([-./月]\d{1,2})?$'
    r'|^\d{1,2}[:：]\d{2}([:：]\d{2})?$'
    r'|^[Pp]?\d{1,3}$'
    r'|^第?\d+[Pp集话期]$'
    r'|^[-_=·.\s]+$'
    r'|^未命名|^无题|^新分P|^新建'
    r'|定稿\d*$|_comp|_Mux|_Trim|_调色|_final$|[Ff]inal$|工程|草稿|初稿|^序列'
    r'|^4[Kk]$|^\d{3,4}[Pp]$|^1080|^720|合拍\d*$|\d+[Kk]\d*$'
    r'|[（(]\d{1,2}[）)]$'
    r'|^投稿$|^成片$|^正片$|^终版$|^输出$|^导出$|^测试$')

# 引号/⚡/【】里的内容
QUOTE_RE = re.compile(r'《([^》]{1,40})》|「([^」]{1,40})」|“([^”]{1,40})”|⚡([^⚡]{1,40})⚡|【([^】]{1,30})】')

# 「唱XXX」抽取
_SING_TAKE = re.compile(
    r'[唱高吼](?:了|起|完|过|着)?(?:首|一首|一曲|一下)?\s*'
    r'([^\s，。！？、,.!?…~～【】\[\]()（）《》“”\'"｜|]{2,12})')
_TAKE_STOP = {'得', '的', '这', '那', '我', '你', '他', '她', '歌', '曲', '起来', '完', '过', '着',
              '什么', '这么', '那么', '一下', '一遍', '出来', '回来', '上去', '不了', '了',
              '不知道', '知道', '完整', '完整版', '清唱', '翻唱', '现场', '一首', '这首歌'}
_TAKE_BAD = re.compile(r'唱|歌|翻|视频|表现力|主播|知道|什么|这么|那么|出来|一遍|怎么样|好听')
_TAKE_HEAD = re.compile(r'^(得|的|着|完|过|了|出|起|再|又|首|个|些|是|在)+')
_TAKE_SPLIT = re.compile(r'要去|再去|然后|接着|之后|还有|顺便|感觉|整个人|结果|但是|可是|所以')
_TAKE_TAIL = re.compile(r'(了|的|啊|呀|吧|呢|吗|哦|喔|咯|啦|嘛)+$')
_VER_SUFFIX = re.compile(r'(完整版|清唱版|纯享版|现场版|填词版|重制版|翻唱版|完整|清唱|版)$')
_LEAD_ACT = re.compile(r'^\s*(街唱|现场|直播|弹唱|翻唱)\s*[\-−–—－·:：、\s]*')
_LEAD_PUNCT = ' -−–—－·:：、_｜|·，,'
_ZHI = re.compile(r'^.{1,10}(?:之|：|:)(.{2,15})$')
_SENT_PUNCT = re.compile(r'[，,。！!？?…~～；;：:]')
_SENT = re.compile(r'得|了|吧|呢|吗|啊|我|你|他|她|直播|观众|突破|唱|翻|知道|什么|怎么|为什么|如何|这|那')

# 歌名词典里剔除的垃圾（歌名不会带终止标点）
JUNK_SONG = re.compile(r'[？！?!。，,、~～…｜|]')


# ============================================================
# 基础清洗
# ============================================================
def clear_fancy(s: str) -> str:
    """把 Unicode 数学字母（𝐍𝐈𝐆𝐇𝐓、𝑺𝒊𝒏𝒄𝒆𝒓𝒆𝒍𝒚）还原成普通 ASCII。"""
    out = []
    for ch in s or "":
        o = ord(ch)
        if (0x1D400 <= o <= 0x1D7FF) or (0x2100 <= o <= 0x214F) or \
           (0xFF21 <= o <= 0xFF3A) or (0xFF41 <= o <= 0xFF5A) or (0xFF10 <= o <= 0xFF19):
            n = unicodedata.normalize("NFKC", ch)
            if n and all(ord(c) < 128 for c in n):
                out.append(n)
                continue
        out.append(ch)
    return "".join(out)


def clean_symbols(s: str) -> str:
    """把装饰符号换成空格；⟪⟫/［］转正式括号。"""
    if not s:
        return s
    s = s.replace("⟪", "《").replace("⟫", "》").replace("［", "【").replace("］", "】")
    s = _PAREN.sub(lambda m: " " if SPECIAL.search(m.group(0)) else m.group(0), s)
    s = SPECIAL.sub(" ", s)
    return s


def strip_emoji(s: str) -> str:
    """去 emoji / 特殊符号 / 花体字母，压空白。

    受 naming.SANITIZE 开关控制（默认全开 = 历史行为）。
    """
    if not s:
        return s
    san = naming.SANITIZE
    if san.get("normalize_fancy", True):
        s = clear_fancy(s)
    if san.get("strip_special", True):
        s = clean_symbols(s)
    if san.get("strip_emoji", True):
        s = EMOJI_RE.sub(" ", s)
    extra = san.get("extra_strip_chars") or ""
    if extra:
        s = s.translate({ord(c): " " for c in extra})
    return re.sub(r"\s+", " ", s).strip()


def norm(s: str) -> str:
    """归一化：去所有空白（含零宽字符）+ 还原花体字母。"""
    return re.sub(r"[\s\u200b-\u200f\ufe00-\ufeff]+", "",
                  clear_fancy(s or ""))


def flat(s: str) -> str:
    """去空白/标点 + 还原花体字母，用于「是否同一个标题」比较。"""
    return re.sub(
        r"[\s\u3000\u200b-\u200f\ufeff\u00b7,，。.！!？?~～\-—_"
        r"【】\[\]「」“”\u0022\u0027()（）：:]",
        "", clear_fancy(s or ""))


def same_title(a: str, b: str) -> bool:
    """两个标题是否「一样或基本一样」（去空白标点后相等/互相包含/相似度≥0.72）。"""
    x, y = flat(a), flat(b)
    if not x or not y:
        return False
    if x == y or x in y or y in x:
        return True
    from difflib import SequenceMatcher
    return SequenceMatcher(None, x, y).ratio() >= 0.72


def clean_part(p: str) -> str:
    """分P名 → 可显示的文字；无意义（空/日期/编号/录制串）返回 ''。"""
    s = strip_emoji(p or '').strip()
    if not s or JUNK_PART.search(s):
        return ""
    return s


def bare_title(t: str) -> str:
    """去掉【标签】/日期/序号后的标题主体。"""
    s = re.sub(r'^(?:\s*[【\[][^】\]]{0,24}[】\]])+', '', t or '')
    s = re.sub(r'[【\[（(]\s*20\d{2}[^】\]）)]*[】\]）)]', ' ', s)
    s = re.sub(r'^\s*\d+\s*[.、．]\s*', '', s)
    return s.strip(' -_·、,，.。~～｜|')


# ============================================================
# 歌名相关
# ============================================================
def song_quotes(t: str) -> list:
    """标题里真正像歌名的引号内容（剔除活动/节目/梗类）。"""
    out = []
    t = t or ''
    for m in re.finditer(r'[《「“]([^》」”]{1,60})[》」”]', t):
        s = m.group(1).strip()
        if not s or NOT_SONG_Q.search(s):
            continue
        # 引号后紧跟「用语/意思/这个词」→ 是在引用词语，不是歌名
        if re.match(r'\s*(用语|意思|这个|这种|词|话|说法)', t[m.end():]):
            continue
        out.append(s)
    return out


def quoted_norm(t: str) -> str:
    """所有引号/⚡/【】里的内容归一化后拼起来。"""
    out = []
    for m in QUOTE_RE.finditer(t or ""):
        for g in m.groups():
            if g:
                out.append(norm(g))
    return "".join(out)


def songbook(parts: list, t: str = "", min_parts: int = 4) -> bool:
    """多P 视频是否为一整场歌回（分P≈每首歌）。"""
    if not parts or len(parts) < max(2, min_parts):
        return False
    if HARD_NON.search(t):
        return False
    good = bad = 0
    for p in parts:
        pt = (p.get("part") or "").strip()
        if not pt or re.search(r'\d{6,}', pt):
            continue
        if PARTS_BAD.search(pt):
            bad += 1
            continue
        # 分P名像「视频标题」而不是「歌名」
        if PART_TITLE.search(pt):
            bad += 1
            continue
        if 1 <= len(pt) <= 24:
            good += 1
    return good >= max(3, int(len(parts) * 0.55)) and bad <= len(parts) * 0.20


def is_singing(t: str, dur: int = 0, song_dict=None) -> bool:
    """标题是否为唱歌类。

    判据：引号内歌名 / 唱歌动词 / ⚡歌名⚡ / 已知歌名 / 强唱歌信号（SING_EXTRA）。
    排除：游戏 / 直播 / 反应 / 跳舞。
    """
    hit = song_dict.hit(t)[0] if song_dict is not None else None
    if not (song_quotes(t) or SING_WORD.search(t) or SPARK.search(t) or hit
            or SING_EXTRA.search(t)):
        return False
    if HARD_NON.search(t) and not PERSONAL.search(t):
        return False
    if REACT.search(t) and not PERSONAL.search(t):
        return False
    return True


# ---- 「唱XXX」抽取 ----
def _clean_cand(s: str, split: bool = False) -> str:
    s = _TAKE_HEAD.sub('', s or '')
    if split:
        s = _TAKE_SPLIT.split(s)[0]
    for _ in range(3):
        s2 = _VER_SUFFIX.sub('', s)
        s2 = _TAKE_TAIL.sub('', s2)
        if s2 == s:
            break
        s = s2
    return s.strip().lstrip(_LEAD_PUNCT).strip()


def _zhi(s: str) -> str:
    m = _ZHI.match(s or '')
    return m.group(1).strip() if m else s


def title_song_label(t: str) -> str:
    """单P 视频：从标题里尽力取出「歌名/曲目标识」。取不到返回 ''。

    优先级：《歌名》 > 唱XXX > 整条标题即歌名 > 标题首段。
    """
    q = song_quotes(t or '')
    if q:
        return q[0].strip()
    for m in _SING_TAKE.finditer(t or ''):
        cand = _clean_cand(_zhi(m.group(1)), split=True)
        cand = _clean_cand(cand)
        if 2 <= len(cand) <= 12 and cand not in _TAKE_STOP and not _TAKE_BAD.search(cand):
            return cand
    b = bare_title(t or '')
    b = _LEAD_ACT.sub('', b).lstrip(_LEAD_PUNCT)
    # ① 整条标题就是歌名（去括号补充后，不能带句读标点）
    whole = re.sub(r'[（(][^）)]{0,20}[）)]$', '', b).strip()
    whole = _clean_cand(_zhi(whole))
    if 2 <= len(whole) <= 20 and not _SENT_PUNCT.search(whole) and not HARD_NON.search(whole):
        return whole
    # ② 只取第一句：短、不像一句话
    #    「N连唱」「第N弹」这类系列名放宽（SING_EXTRA 命中即算）
    first = _clean_cand(_zhi(re.split(r'[，,。！!？?…~～]', b)[0].strip()))
    if 2 <= len(first) <= 14 and (SING_EXTRA.search(first) or not _SENT.search(first)):
        return first
    return ""


# ============================================================
# 歌名词典
# ============================================================
class SongDict:
    """歌名词典（从《》/⚡ 抽取 + 人工补充）。

    用途：判定「标题裸歌名 = 在唱」。
    """

    def __init__(self, songs: list):
        normed = []
        for s in songs:
            n = norm(s)
            if not n or len(n) > 14 or JUNK_SONG.search(n):
                continue
            normed.append(n)
        # 必须带 s 作次要键：只按长度排时同长度顺序取决于 set 哈希顺序，
        # 会导致结果在不同进程里不一样
        self.songs = sorted(set(normed), key=lambda s: (-len(s), s))
        self.songs_long = [s for s in self.songs if len(s) >= 3]
        self._set = set(self.songs)

    @classmethod
    def from_file(cls, path) -> "SongDict":
        try:
            with open(path, encoding="utf-8") as f:
                return cls([ln.strip() for ln in f if ln.strip()])
        except OSError:
            return cls([])

    def hit(self, t: str):
        """返回 (kind, name)。

        kind: quote=引号/⚡内的歌名（强）| exact=整条标题即歌名 | bare=裸歌名 | None=无
        """
        tn = norm(t)
        q = quoted_norm(t)
        if q:
            for n in self.songs:
                if n in q:
                    return ("quote", n)
        b = norm(bare_title(t))
        if b and b in self._set:
            return ("exact", b)
        if tn:
            for n in self.songs_long:
                if n in tn:
                    return ("bare", n)
        return (None, None)


# ============================================================
# 判定器
# ============================================================
class Classifier:
    """按标题判定「目标本人在唱吗」。

    所有目标相关的词表/正则都从 config 注入，本类不含任何具体人名。
    """

    def __init__(self, cfg, manual: dict | None = None, song_dict: SongDict | None = None):
        self.cfg = cfg
        self.manual = manual or {}
        self.songs = song_dict or SongDict([])
        self.alias = cfg.alias_re
        self.strong = re.compile(cfg.strong_sign_re)
        if cfg.partner_shorthands:
            self.strong = re.compile(
                cfg.strong_sign_re + "|" + "|".join(cfg.partner_shorthands))
        self.force_c1 = set(cfg.force_cat1)
        self.force_c2 = set(cfg.force_cat2)
        self.force_c3 = set(cfg.force_cat3)
        self.nonsong_bv = set(cfg.nonsong_bv)
        self.compilation_mids = set(cfg.compilation_mids)
        self.include_kws = list(cfg.include_kws)
        self.include_prefix = list(cfg.include_prefix)
        self.exclude_regex = [re.compile(x) for x in cfg.exclude_regex_generic
                              if isinstance(x, str) and x]
        # 「纯他人」名单：目标专属（换目标必须清空重写）
        self.other_name = re.compile(cfg.other_names) if cfg.other_names else None
        # 已人工核实的标签名（默认 "verified" = 确认是目标本唱）
        self.verified_label = cfg.verified_label or "verified"
        self._self_alias = re.compile(
            "|".join(re.escape(a) for a in _alias_variants(cfg.target_aliases, cfg.target_name)))

    # ---------- 白名单 ----------
    def hit_include(self, text: str) -> bool:
        """白名单匹配：子串忽略空格；前缀先剥掉开头符号/emoji。"""
        if not text:
            return False
        nz = re.sub(r'\s+', '', text)
        if any(re.sub(r'\s+', '', k) in nz for k in self.include_kws):
            return True
        if not self.include_prefix:
            return False
        s = re.sub(r'^[^0-9A-Za-z一-鿿぀-ヿ가-힯]+', '', text.strip())
        return any(s.startswith(k) for k in self.include_prefix)

    # ---------- 黑名单 ----------
    def hit_exclude(self, part: str, title: str) -> bool:
        """黑名单判定（由 dedup 调用）。

        作用范围（**铁律**）：
          - 多P 歌回只判**分P名**；
          - 单P 才判标题；
          - 合集总标题永远不参与判定。
        """
        # 白名单优先
        if self.hit_include(part or "") or self.hit_include(title or ""):
            return False
        # 通用正则层（看视频/反应类，如 SEE《某PV》）
        if self.exclude_regex:
            for txt in (part or "", title or ""):
                if txt and any(r.search(txt) for r in self.exclude_regex):
                    return True
        # 「听/看 + 人名 + 《歌名》」= 听别人唱
        if self.hit_listen_other(part or "") or self.hit_listen_other(title or ""):
            return True
        # 「除非合唱」层：命中即排除，但含「合唱」豁免
        pair = f"{part or ''} {title or ''}"
        if self.cfg.exclude_unless_chorus_kws:
            nz = re.sub(r'\s+', '', pair)
            if any(re.sub(r'\s+', '', k) in nz for k in self.cfg.exclude_unless_chorus_kws):
                if not any(h in pair for h in CHORUS_HINTS):
                    return True
        strip = lambda x: re.sub(r'^[^0-9A-Za-z一-鿿぀-ヿ가-힯]+', '', x.strip())
        nz = lambda x: re.sub(r'\s+', '', x)
        if part:
            if any(nz(k) in nz(part) for k in self.cfg.exclude_kws):
                return True
            return any(strip(part).startswith(k) for k in self.cfg.exclude_prefix)
        t = title or ""
        if any(nz(k) in nz(t) for k in self.cfg.exclude_kws):
            return True
        t2 = re.sub(r'^[【\[][^】\]]*[】\]]', '', t).strip()
        # 两种形态都判：①剥符号后的原始标题 ②去掉号主前缀后的标题
        return any(x.startswith(k) for x in (strip(t), strip(t2))
                   for k in self.cfg.exclude_prefix)

    def hit_listen_other(self, txt: str) -> bool:
        """「听/看 + 人名 + 《歌名》」= 听别人唱。返回 True 表示应排除。"""
        if not txt:
            return False
        for m in LISTEN_RE.finditer(txt):
            name = m.group(1)
            if self._self_alias.search(name):    # 听目标自己的《X》→ 本人唱
                continue
            if any(b in name for b in LISTEN_BLOCK):
                continue                          # 听完这首/听着最甜的 → 不是人名
            return True
        return False

    # ---------- 核心判定 ----------
    def other_person_head(self, t: str) -> bool:
        """开头【XXX】是别人名字 → 别人的独唱。"""
        m = HEAD_BRACKET.match(t or "")
        if not m:
            return False
        k = m.group(1).strip()
        if not k or self.alias.search(k) or NON_PERSON.search(k):
            return False
        return not self.alias.search(t)

    def classify(self, bv: str, t: str, label: str = "out", conf: float = 0,
                 is_self: bool = False, parts: list | None = None,
                 dur: int = 0, zl_only: bool = True, mid: int = 0) -> int:
        """返回 1 / 2 / 3。"""
        parts = parts or []
        dur = int(dur or 0)

        # ① 白名单：标题或任一分P名命中 → 强制 ①（最高优先级）
        if self.include_kws or self.include_prefix:
            cands = [t] + [(p.get("part") or "") if isinstance(p, dict) else str(p)
                           for p in parts]
            for c in cands:
                if self.hit_include(c):
                    return 1
        # ② 人工强制
        if bv in self.force_c1:
            return 1
        if bv in self.force_c3:
            return 3
        if bv in self.force_c2:
            return 2
        # ③ 人工标注文件里的 force_*
        if bv in self.manual.get("force_cat1", []):
            return 1
        if bv in self.manual.get("force_non_song", []):
            return 3
        # ④ 人工核实「确认不是唱歌」（看别人唱/陪看剧/游戏实况/生活活动/宣传片/声音合集）
        if bv in self.nonsong_bv:
            return 3
        # ⑤ 搬运/合集号：整号排除
        if mid in self.compilation_mids:
            return 3
        # ⑥ 综合音乐号 / 搬运号：标题没出现目标 → 一律 ③
        #    必须先于 songbook 判定，否则「100 首华语老歌合集」会被当成歌回
        if not is_self and not zl_only and not self.alias.search(t):
            return 3
        # ⑦ 开头署名是别人 → 别人的独唱
        if self.other_person_head(t):
            return 3
        # ⑧ 多P 歌回 → 整场都算 ①
        if songbook(parts, t, self.cfg.songbook_min_parts):
            return 1
        # ⑨ 强非唱歌
        if HARD_NON.search(t) and not PERSONAL.search(t):
            return 3
        # ⑩ 线下活动/vlog（标题写了《歌名》就不算）
        if VLOG_NON.search(re.sub(r'\s+', '', t)) and not song_quotes(t) \
                and not PERSONAL.search(t):
            return 3
        # ⑩ 纯他人（标题含他人名、不含目标名）
        if self.other_name is not None and self.other_name.search(t) \
                and not self.alias.search(t):
            return 3

        if is_self:
            # ---- 本人号：强信号 → 群体企划 → 时长闸门 ----
            sq = song_quotes(t)
            if sq or self.strong.search(t) or PERSONAL.search(t) or SPARK.search(t):
                return 1
            # 「歌ってみた/替え歌」且没有目标署名 → 早年搬运的他人翻唱
            if re.search(r'歌ってみた|替え歌', t) and not self.alias.search(t):
                return 3
            if label == self.verified_label and conf >= 0.6:
                return 1
            # 只认「引号内歌名/整条标题即歌名」；裸歌名太容易误命中
            kind, _ = self.songs.hit(t)
            if kind in ("quote", "exact") or SING_EXTRA.search(t):
                return 1
            if GROUP.search(t):
                return 1 if (sq or self.cfg.self_song_min_sec <= dur <= self.cfg.self_song_max_sec) else 2
            if self.cfg.self_song_min_sec <= dur <= self.cfg.self_song_max_sec \
                    and not SELF_TALK.search(t):
                return 1
            if dur <= self.cfg.self_review_max_sec and not SELF_TALK.search(t):
                return 2
            return 3

        # ---- 二创/归档号：先看像不像唱歌，再看有没有目标名 ----
        if not is_singing(t, dur, self.songs):
            return 3
        if self.alias.search(t):
            kind, _ = self.songs.hit(t)
            if (kind == "quote" or self.strong.search(t) or PERSONAL.search(t)
                    or song_quotes(t) or SPARK.search(t)):
                return 1
            # 标题里出现已知歌名 + 唱/歌字样 → 本唱
            if kind == "bare" and SING_WORD.search(t):
                return 1
            return 2
        # 没有目标名但已校验为本唱
        if label == self.verified_label and conf >= 0.6:
            return 1
        if label == "unknown":
            return 1 if re.search(r'歌切|歌回', t) else 2
        if self.songs.hit(t)[0]:
            return 1
        return 2


def _alias_variants(aliases: str, target_name: str) -> list:
    """从别名正则里提取「用于 _self_alias 判断」的候选词。

    正则里的 `|` 分支就是各个别名，取出来做字面匹配更安全
    （`_self_alias` 只用来判断「听XXX」里的 XXX 是不是目标本人）。
    """
    out: list = []
    for piece in re.split(r'\|', aliases or ""):
        p = piece.strip()
        if not p:
            continue
        # 只取纯字面（没有正则元字符）的分支
        if re.fullmatch(r'[一-鿿぀-ヿ가-힯A-Za-z0-9]+', p):
            out.append(p)
    if target_name and target_name not in out:
        out.append(target_name)
    return out or [target_name or "x"]


# ============================================================
# 分P 显示
# ============================================================
def part_cell(parts: list, title: str) -> str:
    """「分P」列：多P 逐个列分P名；单P 且分P名≈主标题 → 只写 1P。"""
    if not parts:
        return "1P"
    if len(parts) == 1:
        raw = (parts[0].get("part") or "").strip()
        pn = clean_part(raw)
        if not pn or same_title(raw, title):
            return "1P"
        return f"1P：{pn}"
    out = []
    for i, p in enumerate(parts, 1):
        pn = clean_part(p.get("part"))
        out.append(pn if pn else f"{i}P")
    return f"{len(parts)}P：" + "、".join(out)


def fmt_dur(sec) -> str:
    if not sec:
        return "-"
    sec = int(sec)
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def fmt_date(ts) -> str:
    """时间戳 → YYYY-MM-DD。"""
    if not ts:
        return "-"
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d")
    except Exception:
        return "-"

