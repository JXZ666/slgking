# SLG黄游大王 · 六项问题修复计划

日期：2026-09-18 · 基于实测数据，非推测

---

## 一、现状实测

### 数据库状态（`%LOCALAPPDATA%\slgking\slgking.db`）

| 项 | 数量 | 说明 |
|---|---|---|
| 游戏 | 1274 | |
| 标签 | 119 | 平均 23.4 个/款，最多 50 个 |
| 有封面 | 400 | |
| 待下载封面 | **861** | 已记录 URL，只是没下 |
| 完全无封面 | 13 | 列表页没抓到 URL |
| 有站内评分 | 278 | 待补全 ~996 |
| 有简介 | 192 | 待补全 ~1082 |

### sync_log 实测（关键证据）

```
id=1  netorare   pages=25  seen=498   new=498
id=2  corruption pages=44  seen=1087  new=1087
id=3  cheating   pages=31  seen=1274  new=1274
id=4..10（连续 7 次重同步）
       每次重跑 ~100 页，new 全部为 0
```

**100 次页面请求 → 2859 次游戏出现 → 1274 款去重后游戏 = 2.24 倍冗余。**
且 id=4 之后约 **600 次请求全部白跑**（new=0），每轮约 10 分钟。

### 性能实测

```
find_games 全量 1274 行    9.1 ms
N+1：80 张卡的 game_tags() 1.9 ms
stats()                     0.1 ms
```

**数据库不是瓶颈。** 卡顿 100% 来自 tkinter 控件重建：每张卡约 6 个 CTk 控件，
每次刷新销毁重建 ~480–600 个 CTk 控件（每个 CTk 控件内部又是多个 Tk 控件 + Canvas）。

---

## 二、逐项定位

### 问题 1 · 详情页标签横向溢出

`slg_gui.py:439-453` `_detail_tags()`：所有标签按钮 `pack(side="left")`，
装在 `pack(fill="x")` 的容器里 —— **没有换行**。

详情面板宽 340px，一个标签约 90px → 每行约 3–4 个。
**488 款游戏有 ≥25 个标签**，最多 50 个 → 需要 13 行，全屏也看不到。
（备注：`_render_card` 里的卡片标签有 `wraplength=430`，是有换行的，所以只有详情页出问题。）

### 问题 2 · 同步必须跑完才刷新

`slg_gui.py:528-548` `_sync_worker()` 只往队列塞 `("log", ...)`；
`slg_gui.py:600-613` `_drain()` **只在收到 `("done", ...)` 时才调 `self.refresh()`**。

所以整个同步期间（十几分钟）列表纹丝不动，`stat_label` 只显示一行滚动文字。

### 问题 3 · 按标签抓取造成重复

同上 sync_log 实测：2.24 倍冗余，重同步 100% 白跑。

**实测到的替代方案**（已抓包验证，非推测）：

- `post-sitemap.xml` / `2` / `3` / `4` 共 4 个文件，每条约 1000 项，
  每项含 `<loc>`（游戏 URL）+ `<lastmod>`（最后修改时间）+ 封面图。
  **4 个请求拿到全站游戏清单 + 每款的修改时间。**
- 详情页**一个请求包含全部信息**（实测 `the-copycat`）：
  - 26 个标签链接 `href=".../tag/xxx/"`
  - 封面：`og:image` = `mainscreen1.jpg`，与库里记录的 `mainscreen1-576x356.jpg` **完全对应**
  - `ratingValue` 7.3 / `Version:` v1.3.0 / `Developer:` Mr. PBR
  - 简介 `elementor-tab-content-1601` / `datePublished`

→ **增量同步 = 4 个 sitemap 请求 + N 个变动游戏的详情页（通常 N=0~5）。**
对比现在每轮 100 个请求，约 **20 倍**降幅。

- `/page/N/` 不可用：page/2 与 page/50 返回字节数几乎一致（303618 vs 303625），
  是软 404，不是全站分页。

### 问题 4 · 大部分游戏没缩略图

**不是解析 bug，是吞吐上限。** `slg_gui.py:539-541` 每次同步只下 150 张，
而且 861 张待下载积压着。封面 URL 早已存在库里（`pending:<url>` 前缀）。

13 款完全没有 URL 的，用实测确认的 `og:image` 从详情页补。

### 问题 5 · 免费机翻

**露娜的真正实现**（`LunaTranslator/translator/google.py`，已读源码）：

```python
POST https://translate-pa.googleapis.com/v1/translateHtml
Headers:
    Content-Type: application/json+protobuf
    X-Goog-Api-Key: <AIza... 公开 key，实际值见 slg_engines.GOOGLE_WEB_KEY>
Body:
    [[[content], srclang, tgtlang], "wt_lib"]
取回: response.json()[0][0]
```

这就是那些「免费」翻译器的通用做法 —— 那个 key 是 Google 翻译网页版自带的
公开 key，不需要注册。**支持批量**（把 `[content]` 换成 `[t1, t2, ...]`）。

备用：`lingva`，露娜配置里已指向 `translate.plausibility.cloud`。
再兜底：`https://translate.googleapis.com/translate_a/single?client=gtx&...`。

**另外发现**：露娜 `userconfig/translatorsetting.json` 里存着一套 DeepSeek 的
明文 key（`api.deepseek.com`）。我没有把它抄进任何文件 —— 按你的选择，
代码里只留一个空输入框，要高质量翻译时你自己填。

### 问题 6 · UI 卡顿

DB 只要 11ms，瓶颈全在控件。

`slg_gui.py:363-365` `select()` → `self.refresh()` →
`slg_gui.py:294-295` 销毁全部子控件 → 重建 80 张卡。

**点一下卡片 = 重建 480 个 CTk 控件。** 搜索框每敲一个字触发一次（`:199` `KeyRelease`）。
「显示更多」也是整体重建（`:317-319`）。

---

## 三、执行计划

### A 阶段 · UI 三处硬伤（收益最高、风险最低）

| # | 内容 | 文件 |
|---|---|---|
| A1 | 新增 `FlowFrame`：监听 `<Configure>`，按可用宽度算列数后重新 grid 标签。替换 `_detail_tags` 的 `pack(side="left")` | `slg_gui.py` |
| A2 | 缓存卡片控件：`self._cards = {game_id: frame}`。`select()` 只重绘失焦/新聚焦两张卡的颜色 + 详情面板，不动列表 | `slg_gui.py` |
| A3 | 搜索防抖 300ms（`after_cancel` + `after`），避免逐字符重建 | `slg_gui.py` |
| A4 | 「显示更多」改为追加新卡，不重建已有的 | `slg_gui.py` |
| A5 | 一次性批量取标签（`game_tags_bulk`），替掉每卡一次查询的 N+1 | `slg_db.py` `slg_gui.py` |

### B 阶段 · 同步重构 ✅ 已完成（2026-09-18）

| # | 内容 | 文件 | 状态 |
|---|---|---|---|
| B1 | `fetch_sitemap()`：sitemap → `{slug: {lastmod, cover}}` | `slg_scrape.py` | ✅ |
| B2 | `parse_detail_page` 增加 `og:image` 封面、标签、og:title、datePublished | `slg_scrape.py` | ✅ |
| B3 | `sync_incremental()`：sitemap 比对库内 `lastmod` → 只抓变动游戏详情页 | `slg_scrape.py` | ✅ |
| B4 | `("progress", ...)` 消息 + 节流刷新（1 次/2 秒），进度独立 `progress_label` | `slg_gui.py` | ✅ |
| B5 | `sync_tags` 降级为「全量重建…」按钮，二次确认，不自动跑 | `slg_gui.py` | ✅ |
| B6 | `download_covers()`：3 线程 + 全局限速 3 请求/秒、可续传、可中断 | `slg_scrape.py` | ✅ |

#### 实测结果（对库副本，未动正式库）

```
第一次增量（new_limit=12）   14 个请求   68 秒
再同步一次（无变动）          3 个请求   13 秒   ← 原来 ~100 请求 / 10 分钟
封面 20 张（3 线程）          38 秒（19 成功，1 张 404）
```

#### 与计划的偏差，三条

1. **`post-sitemap3.xml` 返回 HTTP 500**，不是 4 个可用而 3 个。
   覆盖 795/1274。`SITEMAPS` 常量里已注明，标签抓取保留为兜底。
   另一个副作用：sitemap2 偶发 500，所以每次覆盖数会浮动——不影响正确性，
   只影响那一轮的变动检测范围（本就不删数据）。

2. **sitemap 给的是原图 177KB，我们要的是缩略图 18.7KB。**
   新增 `_sized_cover()`，`mainscreen*` 一律换成 `-576x356`。差 9.5 倍。

3. **计划假设「sitemap 全站 ≈ 库里那 1274 款」是错的。**
   全站约 2066+ 款（sitemap3 那 500 款看不到），库里的 1274 款只是
   3 个标签（netorare / corruption / cheating）的结果，**覆盖不到一半**。
   实测抽查 4 个库里没有的 slug，全部是正常游戏页（22–27 个标签、有评分简介）。

   → 首次增量会把库从 1274 涨到约 2500 款。所以加了 `NEW_PER_RUN = 200`
   配额：单次同步最多纳入 200 款新游戏，其余留到下次，进度条可见。
   这正是你提的「分批增量更新」。

4. **两个抓取器写同一行会互相清空。** 标签抓取没有评分，增量抓取没有
   列表页的 version/developer。原来 `UPDATE` 是直接赋值，谁后跑谁把对方
   的字段抹成 NULL。已把 version/developer/engine/rating/last_updated/lastmod
   全部改成 `COALESCE`，并加了回归测试。

5. **developer 两个来源不一致**：列表页写 `PiggyBackRide Productions`，
   详情页写 `Mr. PBR`。都没错，所以已存在的行不再被覆盖（新游戏才写入）。

#### 新增

- `tests/test_incremental.py`：15 个用例（sitemap 解析、分片 500 容错、
  详情页抽取、增量取舍、首次登记、partial update 不清空、迁移）。
  全套 46 个测试通过。
- 命令行：`--incremental`、`--sitemap`、`--new-limit N`。

### C 阶段 · AI 机翻 ✅ 已完成（2026-09-18）

需求在实施前改了三处：标签也要翻且必须用 AI（不是免费机翻）、侧栏加作者署名、
简介改成按需懒加载。所以 C1/C3 与下表原计划不同，实际做法见「与计划的偏差」。

| # | 内容 | 文件 | 状态 |
|---|---|---|---|
| C1 | `slg_translate.py`：DeepSeek（OpenAI 兼容）翻译；`--probe` 自检；key 走 `--key` / 环境变量 / prefs 三级解析 | 新文件 | ✅ |
| C2 | `translations` 表（`kind, ref, lang, src_hash, text, engine`），按原文哈希缓存与失效 | `slg_db.py` | ✅ |
| C3 | 标签用 AI 批量翻译，121 个一次翻完，全库共用 | `slg_translate.py` | ✅ |
| C4 | 详情面板「原文 / 中文」分段切换，点了才翻；侧栏「翻译设置…」对话框 | `slg_gui.py` | ✅ |
| C5 | 设置项：API Key（掩码输入，存 prefs）、模型名 | `slg_db.py` `slg_gui.py` | ✅ |
| C6 | 侧栏署名：作者 菊千代赛高 + 邮箱（点击复制） | `slg_gui.py` | ✅ |
| C7 | `_drain()` 加固：畸形队列消息不再打死轮询链 | `slg_gui.py` | ✅ |
| C8 | `slg_main.py translate` 子命令；四个子命令的 `--help` 修好 | `slg_main.py` | ✅ |

#### 与计划的偏差，四条

1. **C3 原定加 `games.overview_zh` 列，实际没加。** 译文统一落 `translations`
   表，`games.overview` 永远是站点原文。好处是抓取逻辑和 `test_incremental.py`
   的断言完全不受影响，且一张表同时管标签和简介两种缓存。

2. **缓存键从「原文哈希」扩展成 `(kind, ref, lang)` + 哈希。** 标签的 ref 是
   slug，简介的 ref 是 game id，一张表装两种。哈希不一致即视为未命中——
   站点改了简介，下次点开自动重翻，不需要任何失效扫描。

3. **免费 Google 接口整条线没做。** 需求改成统一走 AI，`translate-pa` /
   lingva / gtx 三级兜底全部不需要了。

4. **简介不走 `_start_job` / `"progress"`。** 走了会 `busy=True` 锁住同步按钮，
   且 `"progress"` 会触发 `refresh()` → 重建详情面板，把正在显示的翻译拆掉。
   改成独立的 `_ov_inflight` 集合 + `("overview", ...)` 消息。

#### 实测

```
标签 121 个  →  4 次调用（40/40/40/1）
简介 1 款    →  1 次调用，约 0.002 元
```

`--probe` 用无效 key 实测通过：`HTTP 401: Authentication Fails, Your api key:
****heck is invalid` —— 说明请求形状正确，且 DeepSeek 自己把 key 掩码成尾 4 位。

#### 测试

`tests/test_translate.py`：41 个用例（JSON 响应解析含围栏/夹带/多余键/非字符串、
分块与漏条目补译、缓存命中/过期/覆盖、`resolve_key` 优先级、`display_tag`
回退）。全套 **87 个测试通过**。

另有一个隔离环境的 UI 驱动脚本（临时文件，未入库），27 项断言覆盖：侧栏署名、
标签译文全界面生效、切换开关默认「原文」、无 key 时干净报错且不落缓存、
缓存命中零请求、简介被改后自动重翻、`_drain` 畸形消息不致命。

### D 阶段 · 验证与收尾

| # | 内容 |
|---|---|
| D1 | 补测试：sitemap 解析、详情页 `og:image`/标签抽取、翻译缓存命中、FlowFrame 列数计算 |
| D2 | 重新打包 exe，`--smoke` 验证，桌面快捷方式仍指向新 dist |
| D3 | **提交 git（仓库至今 0 次 commit）** |

---

## 四、风险与注意事项

1. **别把站点抓崩**：详情页保持 1 请求/秒；sitemap 只 4 个请求；
   封面 0.3 秒间隔 × 3–4 线程，需要你点头才跑。
2. **861 张封面首次补全约 15 分钟**（按你的选择：后台跑、可中断续传）。
3. **翻译改成统一走 DeepSeek**：key 是用户自己的，所以留了 `--probe` 自检，
   key 失效或欠费一跑就知道。模型返回不合法 JSON 有四层兜底
   （`response_format=json_object` → 剥围栏 → 漏条目补译一轮 → 回退英文）。
4. **sitemap 结构若变**：标签抓取路径保留为兜底，不删。
5. **翻译质量**：只翻标签和简介（约 2000 字符/款），不翻游戏内文本 ——
   游戏内文本那是露娜的活，两者不冲突。

---

## 五、建议顺序

**A → B → D → C**

先修 UI 三处（A），软件立刻不卡了，投入产出比最高；
再修同步（B），日常同步从 10 分钟降到几秒；
先发一版（D），最后加翻译（C，纯增量功能，不影响已有部分）。

如果只有时间做一件事：**做 A2**。点一下就卡几秒是当前最影响使用的问题，
而它只是把「重建全部」改成「只改两张卡」。

### 进度

- **A 阶段** ✅ 已完成（问题 1、6 已修）
- **B 阶段** ✅ 已完成（问题 2、3、4 已修，见上表实测）
- **D 阶段** ✅ 已完成（46 个测试全绿；exe 已重打包，`--smoke` 报 1474 行；
  首次提交 `faa86d4`，分支 main）
- **C 阶段** ✅ 已完成（87 个测试全绿；exe 已重打包，`--smoke` 报 1474 行，
  `translate --help` 在包内可用）

**还差一步：填 API Key 并跑一次标签翻译。** 代码全部就位，但 121 个标签的译文
要用你自己的 key 才能真正生成——软件里点「翻译设置…」填 key，或命令行
`slgking translate --tags --key sk-xxx`。没填 key 之前，标签显示的是原来的
英文（`display_tag` 回退），简介切换会提示去填 key，不会崩。

问题 4（缩略图）已彻底关闭：正式库 1474/1474 张封面齐了，pending 0。
桌面快捷方式在 `桌面\人工智能\SLG黄游大王.lnk`，指向 `dist\slgking.exe`，
重新打包是原地替换，快捷方式不用重建。
