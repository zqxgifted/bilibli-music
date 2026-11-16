# 配置说明

## 优先级（高 → 低）

```
1. 环境变量 BILI_CONFIG 指向的 JSON 文件
2. config/user.json        用户私有（.gitignore，不进仓库）
3. config/defaults.json    通用默认（进仓库）
4. 内置默认值
```

环境变量覆盖：`BILI_TARGET_NAME`、`BILI_BASE_DIR`、`BILI_SELF_MID`、
`BILI_PRIORITY_UP`、`BILI_DOWNLOAD_JOBS`、`BILI_API_INTERVAL`、
`BILI_FFMPEG`、`BILI_FFPROBE`、`BILI_UA`。

## 文件位置

| 文件 | 进仓库？ | 说明 |
| --- | --- | --- |
| `config/defaults.json` | ✅ | 通用默认，不含任何真实账号 |
| `config/user.json` | ❌ | 用户私有，GUI「保存配置」写这里 |
| `config/user.example.json` | ✅ | 填写模板 |
| `manual_labels.json` | ❌ | 人工标注（改它会直接影响去重结果） |
| `cookies.txt` | ❌ | B 站 SESSDATA |

## 配置项

### 目标

| 键 | 类型 | 说明 |
| --- | --- | --- |
| `target_name` | str | 目标显示名 |
| `target_aliases` | str | 目标别名**正则**（判定「标题是否提到目标」用） |
| `base_dir` | str | 数据根目录。相对路径 = 程序目录 |
| `self_mid` | int | 目标本人 mid；没有本人号填 0 |
| `accounts` | list | `[{"mid": 123, "name": "UP名"}, ...]` |
| `zl_only` | list[int] | 「标题是歌名=目标在唱」的频道 mid |
| `priority_up` | str | 同日多 UP 发同一场时优先保留谁 |
| `keep_all_songs` | list[str] | 例外：这些歌不同日期版本全留 |
| `compilation_mids` | list[int] | 纯搬运合集号（整号排除） |

### 判定规则

| 键 | 类型 | 说明 |
| --- | --- | --- |
| `exclude_kws_generic` | list[str] | **通用**黑名单关键词（换目标也成立，可整段带走） |
| `exclude_kws_project` | list[str] | **专属**黑名单关键词（人名/粉丝团/梗/综艺） |
| `exclude_prefix_generic` | list[str] | 通用黑名单前缀（先剥开头符号再比，**不要带【】**） |
| `exclude_prefix_project` | list[str] | 专属黑名单前缀 |
| `exclude_regex_generic` | list[str] | 通用黑名单正则（看视频/反应类） |
| `exclude_unless_chorus_kws` | list[str] | 命中即排除，但标题含「合唱」则豁免 |
| `include_kws` | list[str] | **白名单**关键词：命中强制收录 |
| `include_prefix` | list[str] | 白名单前缀（**不要带【】**） |
| `strong_sign_regex` | str | 「目标在唱」的强署名正则；留空则按别名拼 |
| `partner_shorthands` | list[str] | 合作对象圈内简称（命中即视为合唱） |
| `other_names` | str | 「纯他人」名单（正则）；标题含这些人名且不含目标 → ③ |
| `force_cat1/2/3` | list[str] | 人工强制判定（BV 号） |
| `nonsong_bv` | list[str] | 人工核实「确认不是唱歌」的 BV 号（整支视频排除） |
| `verified_label` | str | `_verified_*.json` 里代表「确认是目标本唱」的标签名（默认 `verified`） |

### 标题净化（`sanitize`，GUI「标题净化」页）

**只影响文件名/显示名，不影响判定**。默认全开 = 历史行为。

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `strip_emoji` | true | 去 emoji（💃🎄🔥⚡…） |
| `normalize_fancy` | true | 花体字母还原（𝐍𝐈𝐆𝐇𝐓 → NIGHT） |
| `strip_special` | true | 去装饰符号（箭头/几何/颜文字/全角＾） |
| `strip_bracket_tags` | true | 去【…】标签 |
| `strip_series_no` | true | 去 #N 系列号（保护「心拍数#0822」） |
| `strip_lead_punct` | true | 去开头标点 |
| `keep_trailing_tone` | true | 保留结尾语气符（~～） |
| `extra_strip_chars` | "" | 额外要删的字符（逐个字符匹配） |

> ⚠️ 净化会影响去重键（歌名归一化）—— 改完保存后要**重跑「③ 去重」**才生效；
> 已下载文件不会自动改名。

### 按 UP 的清洗规则（`up_clean_rules`，GUI「高级规则」页）

对应原来 `正则表达式.txt` 里「谁家的标题长什么样」那套。

```json
"up_clean_rules": [
  {"up": "某UP名",
   "remove": ["😭", "【目标名】"],
   "extract_regex": "歌回(.*?)-",
   "skip_parts": ["二次元专场", "loser"]}
]
```

| 键 | 作用 |
| --- | --- |
| `remove` | 该 UP 的歌名里直接删掉的字符串 |
| `extract_regex` | 分P名匹配后取**第 1 个捕获组**当歌名 |
| `skip_parts` | 分P名含这些词的不当歌处理（子串，忽略空格） |

GUI 里写成一行：`UP名 | 删除串(用;分隔) | 提取正则 | 跳过词(用;分隔)`。

### 弹幕/评论校验正则（GUI「高级规则」页；留空=按目标名自动拼）

| 键 | 作用 |
| --- | --- |
| `verify_mark_regex` | 目标显式署名为主唱 |
| `verify_collab_regex` | 合作/feat 署名 |
| `verify_sing_regex` | 「目标+唱歌语境」（评论/弹幕里） |
| `verify_group_regex` | 群体企划（命中→他人） |
| `verify_role_regex` | 角色向演唱（命中→他人） |
| `verify_named_others` | 他人唱见名单（命中→他人） |

供「⑥ 弹幕/评论校验」写 `_verified_{mid}.json`，判定器再读它。

### 阈值

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `self_song_min_sec` | 60 | 本人号「单曲时长」下界 |
| `self_song_max_sec` | 330 | 本人号「单曲时长」上界 |
| `self_review_max_sec` | 640 | 超过这个时长且无强信号 → ②（待人工） |
| `songbook_min_parts` | 4 | 多P 视频至少几个分P 才算「歌回」 |

### 下载

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `audio_priority` | `highest` | `highest` / `hires` / `dolby` / `192k` / `132k` / `64k` |
| `audio_format` | `m4a` | `m4a`（无损封装）/ `mp3`（重编码） |
| `mp3_bitrate` | `auto` | `auto` = 按源音轨就近取 MP3 标准档位 |
| `download_jobs` | 3 | 并发数（实测稳定上限 4） |
| `api_interval` | 1.1 | API 最小间隔秒数 |
| `overwrite` | false | 已存在的文件是否重下 |

### 环境

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `ua` | Chrome 131 UA | 请求 UA |
| `cookies_file` | `cookies.txt` | cookie 文件名（在数据目录和程序目录各找一次） |
| `ffmpeg` / `ffprobe` | `ffmpeg` / `ffprobe` | 留空则自动探测 |
| `tmp_dir` | 空 | 下载临时目录；留空 = `<数据目录>/_tmp` |

## cookies.txt（B站登录凭证）

GUI「配置」页顶部的 **B站账号** 卡片可以直接管理，不必手工编辑文件。

| 场景 | 程序显示 | 影响 |
| --- | --- | --- |
| 有效 SESSDATA | `● 已登录：用户名（mid｜大会员）` | 抓取稳定，可取 Hi-Res/杜比 |
| SESSDATA 过期 | `● 未登录：SESSDATA 已过期…` | 下载仍可用（最高 192K），抓取容易 412 |
| 没有 cookies.txt | `● 未登录：没有 cookies.txt…` | 同上 |

**更新方式**：浏览器 F12 → Application → Cookie → 复制 `SESSDATA` 的值 →
粘进「粘贴新 SESSDATA」框 → 点「保存并验证」。
程序接受三种粘贴格式（Netscape 整行 / `SESSDATA=xxx;` 串 / 直接粘值），
自动解析并写入 `cookies.txt`（数据目录与程序目录都会写）。

查找顺序：`cookies_file` 指定路径 → `<数据目录>/cookies.txt` →
`<程序目录>/cookies.txt`。

> ⚠️ cookies.txt 等同登录凭证，**不要提交到 git、不要发给别人**。
> 发布包里不含它（`.gitignore` 与打包脚本都已排除）。

## 人工标注 `manual_labels.json`

**规则管大多数，人工标注管个例。** 判错的条目写这里，**别去改判定规则**。

| 字段 | 粒度 | 用途 |
| --- | --- | --- |
| `force_cat1` | BV | 强制判 ①（标题看不出但确认真唱） |
| `force_non_song` | BV | 强制判 ③（杂谈/整活/他人视角，**整支视频**排除） |
| `force_non_song_units` | `BVxxx_pN` | 强制判 ③（**分P级**，同视频里个别他人分P） |
| `manual_key` | `BVxxx_pN` | 强制去重键（同曲异写归并） |
| `manual_disp` | `BVxxx_pN` | 强制显示名/文件名（分P名不是歌名时用） |

> ⚠️ `manual_key` **必须分P级**。BV 级会把同视频其他分P一起改键，一次误伤 3 首歌。
