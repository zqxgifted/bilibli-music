# B 站接口说明

只用三个接口，刻意避开容易风控的那个。

---

## 1. 投稿列表（唯一会 412 的接口）

```
GET https://api.bilibili.com/x/space/wbi/arc/search
    ?mid=<空间号> & ps=50 & pn=<页码> & order=pubdate
    & web_location=333.1007
    & dm_img_list=[] & dm_img_str=... & dm_cover_img_str=... & dm_img_inter=...
    & wts=<时间戳> & w_rid=<WBI 签名>
```

**WBI 签名**：

1. 从 `nav` 接口取 `wbi_img.img_url` / `wbi_img.sub_url` 的文件名（去掉扩展名）
   → `img_key` / `sub_key`
2. 按固定混淆表 `TAB` 把 `img_key + sub_key` 重排成 32 位字符串
3. 参数加 `wts`（秒级时间戳）→ 按 key 排序 → 过滤掉 `!'()*` → urlencode
4. `w_rid = md5(query + mixin_key)`

> ⚠️ **`nav` 即使未登录（code=-101）也会返回 `wbi_img`**。
> 空间接口的签名密钥不要求登录 —— 别被 `code` 卡住，只看有没有 `wbi_img`。

**风控**：412 / -352 / -799 是 **per-mid 限流**，不是 IP 全封。
同一时刻 A 号能抓、B 号不能。等 30–60 分钟，别连续重试。

---

## 2. 视频信息（分P）

```
GET https://api.bilibili.com/x/web-interface/view?bvid=<BV号>
```

返回 `aid` / `cid` / `pages[]`（每项含 `page` / `part` / `duration` / `cid`）。

**省额度**：只有「长视频 or 标题像合集」才请求这个接口补分P。

---

## 3. 播放地址（音轨）

```
GET https://api.bilibili.com/x/player/playurl
    ?avid=<aid> & bvid=<BV号> & cid=<分P的cid>
    & qn=127 & fnver=0 & fnval=4048 & fourk=1 & platform=web
```

用**旧版非 WBI 接口**（不用 `/wbi/playurl`），这是绕过风控的关键。

`fnval=4048` 会返回 `dash` 结构，其中 `dash.audio[]` 是音轨，
`dash.dolby` / `dash.flac` 是高音质轨，老视频走 `durl`（整段 mp4）。

### 音轨 id 与音质

| id | 名称 | 说明 |
| --- | --- | --- |
| 30251 | Hi-Res | 大会员无损（FLAC） |
| 30250 | 杜比全景声 | 大会员 |
| 30255 | 杜比音效 | 大会员 |
| 30280 | 192K | 普通 |
| 30232 | 132K | 普通 |
| 30216 | 64K | 普通 |

优先级：**Hi-Res > 杜比全景声 > 杜比音效 > 192K > 132K > 64K**。

`baseUrl` 是主地址，`backupUrl[]` 是 CDN 镜像 —— 下载失败时依次重试。

---

## 4. 搜索（发现新号）

```
GET https://api.bilibili.com/x/web-interface/wbi/search/type
    ?search_type=video & keyword=<关键词> & page=<页码> & page_size=30 & order=pubdate
    & <同样的 WBI 签名>
```

标题里的高亮标签形如 `<em class="keyword">xxx</em>`，用正则剥掉。

> ⚠️ 标题法会**低估**不带《》的号（真实项目里 Seven 实际 94 首，标题法只数到 1），
> 所以搜索结果**只用于发现新号**，不做最终判定。

---

## 5. cookies

`cookies.txt` 用 Netscape 格式，必须含一行 `SESSDATA`：

```
# Netscape HTTP Cookie File
.bilibili.com	TRUE	/	FALSE	0	SESSDATA	xxxxxxxx%2C1808406755%2Cxxxxx
```

| 场景 | 表现 |
| --- | --- |
| 没有 cookie | 能下 480P 以下；空间列表容易 412 |
| cookie 有效 | 全部正常，能取大会员音质 |
| cookie 过期 | `nav` 返回 `-101`；下载接口**反而能通**（只是没大会员音质） |

**过期可以自查**：`nav` 返回 `-101` 就是过期了。程序会自动检测并提示。

---

## 6. 限速

`RateLimiter` 全局最小间隔，默认 **1.1 秒** + 0~0.3 秒随机抖动，模拟人工节奏。

- 下载接口：`playurl` 走限速器
- 空间接口：每页之间额外 `delay + random(0, 1.5)` 秒
- 退避重试：412/429 → 6×(n+1) 秒；空间接口 → `min(45, 8×(n+1))` + 随机

**并发别贪**：`download_jobs=4` 是实测稳定上限，间隔别低于 1.1。
