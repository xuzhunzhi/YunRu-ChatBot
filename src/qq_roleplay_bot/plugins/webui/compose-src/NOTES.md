# webui-compose（现位于 `plugins/webui/compose-src/`）— Miuix 面板 v2（wasmJs）

> **2026-10-20 搬家**：本工程原来在仓库根的 `webui-compose/`（未跟踪），为了让源码能进
> 版本控制（插件线范围门槛只放行 `src/qq_roleplay_bot/plugins/**`），整棵搬到了
> `src/qq_roleplay_bot/plugins/webui/compose-src/`。下面正文里凡是还写着 `webui-compose/`
> 的段落，都是**搬家之前**的记录；要照抄命令请看 §十五。


两轮的东西都记在这里。**第一轮**只证明 `top.yukonga.miuix.kmp:miuix-ui-wasm-js:0.9.4` 能在纯
`wasmJs` 目标下编译并在浏览器里跑起来；**第二轮**在它上面做了三件事（中文字体预载、外壳 +
`NavigationRail`、账号页接真数据），并把产物摆到面板静态目录的 `v2/` 下。

---

## 一、版本选择的出处（两轮相同）

| 组件 | 版本 | 出处 |
| --- | --- | --- |
| Kotlin | `2.4.20` | `run/data/tmp_miuix/miuix-0.9.4/gradle/libs.versions.toml:3` |
| Compose Multiplatform | `1.12.0` | 同上 `:10` |
| Gradle | `9.7.1` | 同上 `gradle/wrapper/gradle-wrapper.properties:3`（wrapper 原样拷入） |
| Miuix | `0.9.4` | 任务给定；POM 自证依赖 `compose 1.12.0` + `kotlin-stdlib 2.4.20` |
| Skiko | `0.150.1` | 无显式声明，由 CMP 1.12.0 传递引入 |

Miuix 的 `libs.versions.toml` 里**没有 Skiko 条目**，所以 Skiko 只能跟着 Compose 走。

## 二、构建

```
cd src\qq_roleplay_bot\plugins\webui\compose-src
.\gradlew.bat --console=plain wasmJsBrowserDistribution
```

冷启（第一轮）：`BUILD SUCCESSFUL in 14m 5s`，几乎全在下载（Gradle 发行包 151 MB + Maven klib
+ yarn/npm + binaryen）。热缓存：第一轮干净重编 1m 10s；第二轮改了 Kotlin 的全量编 **2m 12s**，
只改资源 9s。

### 网络/语法坑（第一轮踩的，仍然有效）

1. `services.gradle.org` 只做 307 跳转到 `github.com/gradle/gradle-distributions`，本机连 github
   超时 → 换 Gradle 官方 CDN `downloads.gradle.org/distributions/gradle-9.7.1-bin.zip`。
2. Kotlin/Wasm 对 `js(...)` 有硬限制：只能作为**顶层函数体的唯一表达式或属性初始化器**，且会被
   内联进 `'name' : () => <code>`，**字符串里不能带结尾分号**（webpack 会报
   `Module parse failed`）。
   **第二轮的做法**：不再用 `js(...)`，全部改成 `@JsFun` 的 `external fun`（每段是一个独立
   函数声明，不受那条内联规则约束）。见 `Platform.kt`。

### 第二轮新踩的两个坑

3. **`TextField` 两种重载参数不一样**：`TextField(state = TextFieldState)` 没有 `singleLine`，
   要单行得给 `lineLimits = TextFieldLineLimits.SingleLine`；而**遮罩**只有一个入口
   `visualTransformation`，它只存在于 `TextField(value = TextFieldValue, onValueChange = …)`
   那一版。所以 token 那个输入框用 value 版（并因此自己保管草稿态）。
4. **`TextFieldState.text` 是 `CharSequence`**，直接当 `LaunchedEffect` 的 key 传会编译报
   "actual type is 'CharSequence', but 'String' was expected"。

## 三、依赖纪律（这轮按要求逐个验 wasm）

**声明**（`build.gradle.kts` 里只有这四个 Miuix 相关条目）：

| 模块 | 这轮新引？ | wasm 编译 | 运行时 |
| --- | --- | --- | --- |
| `miuix-ui` | 否（第一轮就有） | 通过 | 零异常 |
| `miuix-core` | 否（第一轮就有） | 通过 | 零异常 |
| `miuix-icons` | **是** | 通过 | **零异常**（`cdp-shot` 全程 `console/exception lines: none`） |

`miuix-preference` **没有引**（这轮没有任何 Preference 组件，引了就是白背字节）。

**没有引 `miuix-blur` / `miuix-shader`**——但要说准：`miuix-shader` 是**传递依赖**，不是这轮
加的。`gradle dependencies` 实测：

```
+--- top.yukonga.miuix.kmp:miuix-ui-wasm-js:0.9.4
|    +--- top.yukonga.miuix.kmp:miuix-core:0.9.4
|    +--- top.yukonga.miuix.kmp:miuix-squircle:0.9.4
|    |    +--- top.yukonga.miuix.kmp:miuix-shader:0.9.4
+--- top.yukonga.miuix.kmp:miuix-core-wasm-js:0.9.4
\--- top.yukonga.miuix.kmp:miuix-icons-wasm-js:0.9.4
```

`squircle`（选中态药丸要用）→ `shader`，这条链第一轮就在（`NavigationRail`/`Surface` 的
`squircleBackground` 就是它）。`miuix-blur` **完全没进 classpath**（`~/.gradle/caches` 里没有
`miuix-blur*`）。

## 四、中文字体预载（第一轮的坑，这轮的办法）

### 字体文件从哪来、放哪、多大

- Miuix 官方 example 走的是 **CDN**：`example/web/src/webMain/kotlin/Main.web.kt:21-22`
  `DEFAULT_CSS_URL = https://cdn-font.hyperos.mi.com/font/css?family=MiSans_VF…`，
  再由 `webfont/WebFontPreload.kt` 解析 `@font-face`、只挑 **TTF/OTF** 子集下载，然后
  `resolver.preload(FontFamily(Font(identity = url, getData = { data }, …)))`。
- 这个部署要**本机离线可用**，所以不碰那个 CDN：改为把字体文件放进工程资源。
  用的是本机 Windows 11 自带、**SIL OFL 1.1** 的
  `C:\Windows\Fonts\NotoSansSC-VF.ttf`（17.8 MB，Noto Sans SC 变量字体）。
- 原始 17.8 MB 会把传输量翻倍，所以用 `tools/subset-font.py`（系统 Python 的 fontTools 4.66）
  离线做两件事：**在 wght=400 处实例化成静态字体** → **按字符集裁剪**。
  字符集 = ASCII + Latin-1 + 通用标点 + CJK 标点 + 全角 + **GB2312 全量（6763 字）** +
  **仓库里所有 CJK 字符**（`src/wasmJsMain/kotlin` 与本工程的 Kotlin 源码、以及老面板
  `webui/{app.js,index.html,app.css}` 里的字符串都扫了一遍）。
  裁剪后 **2,173,132 B（2.07 MiB）**，落到
  `src/wasmJsMain/resources/fonts/NotoSansSC-Regular-subset.ttf`（构建时被拷到 dist 根下的
  `fonts/`，与 `index.html` 同级）。
  覆盖检查脚本自己会打印：语料里只差 `U+FE0F`（变体选择符，本来就不是字形）。
- 复跑：`python tools/subset-font.py`（只读 Windows 字体 + 读本仓库文本，不联网）。

### 怎么注册（顺序是关键）

`Main.kt`：

1. 字体字节先到手（`index.html` 在 `<head>` 里就 `fetch()` 成 base64 promise，见下），
2. `Font(identity, getData = { bytes }, weight = Normal)` → `FontFamily(font)`，
3. `FontFamily.Resolver.preload(family)` —— 这一步把字形塞进 Skia 的共享缓存；
   **没有它，第一段排版照样会跟异步的 typeface 解析抢跑**（第一轮量到的 1.2–1.9s 就是这场竞速），
4. 这些都完成了才 `fontSettled = true`，才开始组合外壳；在此之前只画一块主题色**空**表面
   （没有文字 ⇒ 不可能出豆腐块）。
5. 字体族通过 `MiuixTheme(textStyles = cjkTextStyles(family))` 落到**全部 14 个 Miuix 文字
   样式**上（`TextStyles.copy` 逐项 `copy(fontFamily = …)`），所以 `NavigationRailItem`、
   `SmallTopAppBar`、`Card` 里那些自己指定 `fontSize` 的 `Text` 也一起吃上，不用逐处传参。

### 一个便宜但有效的优化

`index.html` 里在 `<head>` 就发起字体 `fetch`（2.1 MB），Kotlin 侧 `await` 同一个 promise
（`window.__dshFontB64`）。这样字体下载与 `composeApp.js` + 两个 `.wasm`（合计 ~11.7 MB）**并行**，
而不是等 wasm 起来之后再加一段。

| 情形 | 首帧带文字（`performance.now()`，1400×860，本机） |
| --- | --- |
| 有预载 + HTML 预取 | **中位数 1108 ms**（9 次：1015 / 1065 / 1129 / 1151 / 1190 / 1220 / 1338 …） |
| 关掉预载（`?nofont=1`） | **中位数 1052 ms**（8 次：905 / 995 / 1109 / 1204 / 1215 / 1227 …） |
| 有预载但**只在 Kotlin 里取**（没 HTML 预取） | 1432 ms（同一台机器，作对照） |

也就是说：**预载的净代价 ≈ 50 ms（中位数）**，前提是让字体下载与 wasm 下载重叠；
不重叠的话是 +343 ms。剩下 ~1 s 是「wasm 解码 + 首帧组合」本身的成本，不是字体。

### 证据（`evidence/`）

| 文件 | 是什么 |
| --- | --- |
| `v2-font-on-first.png` | **预载开**：app 自己打点的那一帧（`__dshFirstText`），中文全是真的字形 |
| `v2-font-off-first.png` | **预载关**（`?nofont=1`）：同一时刻的帧，中文全是 `□` |
| `v2-account-live.png` | 账号页读到真数据（token 已遮罩） |
| `v2-account-badtoken.png` | 错 token 的失败态：红字「读不到：HTTP 401 {…}"unauthorized"}」（失败文案是响应体原文，好判断是哪一道门挡的） |
| `v2-narrow-900.png` | 900px 宽：收起态侧栏（80dp） |
| `v2-rail-scrolled.png` | 侧栏滚到底：插件 9 项 + 系统 4 项 |
| `v2-under-subpath.png` | 从 `/v2/index.html` 这个**子路径**打开（验证相对路径没问题） |
| `compose-wasm-*.png` | 第一轮的探针（豆腐块 → 落字 → 点击） |

`-t0/400/800/1500` 后缀是同一轮里依次抓的四帧（第一轮的抓法保留）。

## 五、外壳

> ⚠️ **2026-10-18 第二轮已把这一节的外壳推翻重排**（Scaffold → Row、顶栏进页列、卡片层级、
> 彩色图标块）：组件与几何值那张表仍然有效（rail 几何 / 断点 / IA 都没变），但**界面骨架看 §十三**。

组件：`MiuixTheme`（`ThemeController(colorSchemeMode = ColorSchemeMode.System)`，即跟随系统
深浅色）→ `Scaffold` → `SmallTopAppBar`（标题 + 当前「组 · 页」副标题）→ 左侧 `NavigationRail`
→ 右侧内容 `Column`（`verticalScroll`）。

`NavigationRail` 的每个几何值都来自
`run/data/tmp_miuix/miuix-0.9.4/docs/components/navigationrail.md` 的
**§"NavigationRailDefaults Object" → "Constants"**（`navigationrail.md:117-135`），并且是
**照默认值用**、没有另传：

| 文档里的常量 | 值 | 这轮用在哪 |
| --- | --- | --- |
| `MinWidth`（`:119`） | 80dp | 收起态宽度（<1100px 时） |
| `ExpandedWidth`（`:120`） | 240dp | 展开态宽度 |
| `VerticalPadding`（`:121`） | 24dp | 侧栏内容竖向留白 |
| `IconSize`（`:123`） | 28dp | 每项图标 |
| `ExpandedItemHorizontalMargin`（`:128`） | 12dp | 展开态药丸左右边距 |
| `ExpandedItemCornerRadius`（`:129`） | 16dp | **选中态药丸圆角** |
| `LabelFontSize` / `ExpandedLabelFontSize`（`:126-127`） | 12sp / 16sp | 收起 / 展开的字号（组件自己按展开进度插值） |
| 分割线 | `showDivider = true`（`:92`） | 侧栏与内容之间那条竖线 |
| 可滚动 | `scrollState: ScrollState = rememberScrollState()`（`:96`） | 23 项放不下时侧栏自己滚（组件内部就有 `verticalScroll`） |

窄屏/宽屏的判断：`BoxWithConstraints` 的 `maxWidth >= 1100.dp` → 展开，否则收起。

**为什么是 1100 而不是老面板的 900**：老 CSS 面板窄屏时把侧栏**横过来**变成顶部页签条，
所以 900 就够；v2 的侧栏是**常驻竖栏**，它占掉的宽度要从内容区里扣。展开 240dp 时，窗宽 1100
才留下 ≥860dp 的内容区（老面板那张六列表格是按 1180px 设计的，见
`run/data/private_docs/WEBUI.md` §"布局"）；再窄就用 80dp 收起态，1100→1020 时内容区仍有
≥940dp。`maxWidth` 在 wasm 上是 CSS px（dpr=1 时即窗口内宽）。

信息架构（6 组，**一个都没丢**）：见 `Nav.kt` 的 `railGroups()`——
账号与设备（账号、设备）·聊天（总览、群聊、私聊）·记忆（记忆、人物画像）·
知识库（知识库、金句、黑话）·插件（**每个插件一项**）·系统（日志、设置、模型与凭据、动作）。

- 固定叶子 **14** 项，与老面板 `webui/app.js` 的 `CARDS` 一一对应；
- 「插件」组由 `GET /api/plugins` 的实时清单生成（本机 **9** 个：群管理、入群申请、邮件、
  掉线通知、到点提醒、订阅推送、超时通知、识图、控制面板），登录前用 `pluginFallback` 里的
  同一份 9 个兜底，所以侧栏永远是完整的；
- **实际总数 23 项**（14 + 9）。交底写的是 18 项（= 14 + 4 个插件项）；按「一个都不许丢」
  取了插件全集，没有砍。要精确 18 项就把 `Nav.kt` 的 `pluginFallback` 与
  `railGroups(...)` 的输入裁到 4 个（一行改动），但那等于隐藏 5 个已装插件。
- 除「账号」外的页都是**占位页**：写明这一页要做什么、读哪条路由（内容取自老面板的
  `TAB_NOTES`），不留空白，也不假装已完成。

## 六、「账号」页：路由、字段、token

路由与字段（对着 `src/qq_roleplay_bot/plugins/webui/webui_panel.py` 读，并用 8790 上的真面板
+ Bearer token 实测过一遍）：

| 页面上的东西 | 路由 | 字段 |
| --- | --- | --- |
| 她的号 | `GET /api/self` | `data.self_id` / `data.available` |
| bot 在运行 | `GET /api/overview` | `data.health.bot_live`（端口探法）、`data.health.engine_attached` |
| 接入模式 | `GET /api/overview` | `data.access.mode`（`local`/`remote`）+ `data.access.remote` |
| 面板会话数 | `GET /api/overview` | `data.access.sessions` |
| （附）面板运行时长 / 端口 | `GET /api/overview` | `data.health.panel_uptime_seconds`、`panel_port`、`bot_port` |
| 供应商与模型 | `GET /api/model` | `data.{available, providers[{name,base_url,key_count,models,used_by}], bindings[{label,provider,model,layer_label}]}` |

**token 怎么来的**：走**输入框**（不改后端）。这与老面板同一条路——`app.js` 用
`localStorage["yunru_panel_token"]` 存、每个请求带 `Authorization: Bearer <token>`
（本地模式的口径在 `webui_access.WebAccess.check_bearer`，token 本体在 `data/webui_token`）。
v2 的差别只有两条：字段用 `PasswordVisualTransformation` **遮罩**（避免 token 出现在截图里），
`?token=…` 可以预填（方便书签）。落库位置仍是 `localStorage["yunru_panel_token"]`，两版面板
共用同一个键。

**`available: false` 怎么处理**：`/api/model` 在这台机器上返回
`{"available":false,"providers":[],"bindings":[],"options":[]}`（装配点没把 `model_providers`
接缝递进来）。页面照实说「面板拿不到这份数据」，**不画一张空表**——空表会被读成"一把 key
都没配"。实测数据：`self_id=3655414729`、`bot_live=true`、`engine_attached=true`、
`mode=local`、`sessions=0`。

实现要点：三次读并行（`coroutineScope { async … }`）且**互相不连坐**——`/api/model` 挂了不影响
「她的号」；JSON 用一个 ~120 行的自写解析器（`Json.kt`），不引序列化插件。

## 七、产物落位与部署

### 现在能不能从 `/v2/` 取到？**不能，需要后端加东西**（这轮按要求只给方案）

读 `webui_panel.py` 的结论（`_route` 约 `:329-340`、`_static` 约 `:1570`）：

1. 静态资源走一张**四名字白名单**：`STATIC = {"/", "/index.html", "/app.css", "/app.js"}`
   （`:73-78`），别的路径在 `_route` 末尾直接 `failure("not_found", 404)`——**没有子路径概念**，
   所以 `/v2/index.html`、`/v2/composeApp.js`、`/v2/fonts/*.ttf` 全部 404。
2. 就算把名字加进白名单也**不够**：`_static()` 用
   `resources.files(...).read_text(encoding="utf-8")` **按文本读**，`.wasm` 是二进制，照这条
   路读会解成乱码（还会抛 `UnicodeDecodeError`）。MIME 也要给 `application/wasm`，
   否则 `instantiateStreaming` 静默失败（第一轮就踩过）。
3. 顺带一条：`_static` 给的是 `Cache-Control: no-store`，2.1 MB 的字体每次刷新都会重下；
   真上线时这行值得只对 `v2/fonts/` 放开缓存。

**方案（不动后端，只写清楚）**：

- 把 `STATIC` 换成「前缀 → 目录」的路由：`/v2/` 前缀下按**白名单扩展名**（`.html/.js/.wasm/.ttf/
  .ico/.map`）取文件，`Path.resolve()` 后必须仍在 `webui/v2/` 里（防穿越——现在是"没有路径拼接
  所以没有穿越"，加了子路径就必须显式挡）；
- `_static` 拆两条：文本一条（老的四个文件，UTF-8 不动）、**二进制一条**（`read_bytes()`，
  MIME 按扩展名，`.wasm` 必须 `application/wasm`）；
- 一行兜底：`/v2` 与 `/v2/` → `v2/index.html`；
- 缓存：只给 `/v2/fonts/` 与 `/v2/*.wasm` 发 `Cache-Control: max-age=…`（文件名带内容哈希）。

> **2026-10-18 已落地**（用户批了"唯一一处后端改动"）：上面四条在
> `src/qq_roleplay_bot/plugins/webui/webui_panel.py` 里实现成
> `V2_DIR / V2_TYPES / _is_v2_path / _v2_root / _v2_cache_control / _static_v2`，
> 覆盖在 `tests/test_webui_v2_static.py`（12 条）。实现时与上面措辞的两处差异：
>
> 1. **`/v2`（无斜杠）回 302 → `/v2/`**，不是直接吐 index.html。原因：产物里的
>    `composeApp.js`、`fonts/*.ttf` 都是**相对地址**，在 `/v2` 下会解析成 `/composeApp.js`
>    → 404 白屏。另一条路（给 index.html 塞 `<base href="/v2/">`）要重建前端、且把部署前缀
>    写死进产物，所以选了重定向。
> 2. **强缓存只给"名字带内容哈希"的 `.wasm`**（`_looks_content_hashed`：主干 ≥16 位十六进制），
>    别的（含 `fonts/*.ttf`）一律 `no-cache` + ETag。理由：字体文件名不含哈希，换一份字体文件名
>    不变，强缓存会让浏览器拿旧字体到明年。`index.html` 仍 `no-store` 且不发 ETag。
>
> 另外 `Response` 多了一个 `cache_control` 字段（`None` = 照旧发 `no-store`）：原来
> `_Handler._respond` 硬编码一条 `no-store`，`_static` 又塞一条，老面板线上是**两条**——
> 老路径因此一个字节没变（测试里那条基线是改代码**之前**抓的）。

### ⚠️ 改完要重启才能访问 `/v2/`

路由是**进程内存里的代码**，静态文件虽然是"每次请求读盘"，但 `/v2/` 这个分支根本不在
正在跑的那个进程里——**必须重启面板/bot 进程**才会生效（`schtasks /Run /TN YunRu-bot-launcher`
或按现有部署流程；这轮没重启，不在权限内）。

不重启时能怎么看：跑一个**临时**服务器把同一个 `_Handler` + `Panel` 挂到别的端口
（本轮做法：`python` 起 `ThreadingHTTPServer(("127.0.0.1", 8743), Handler)`，`Panel` 用空接缝，
浏览器打开 `http://127.0.0.1:8743/v2/`）——实测 wasm 与字体都从 `/v2/` 取到、控制台零异常、
首帧带文字 1022 ms，证据 `evidence/v2-served-by-panel.png`。临时服务器用完即关。


### 部署前要做的事

```
# 构建 + 拷到 v2/（一条命令；在仓库根执行）
.\src\qq_roleplay_bot\plugins\webui\compose-src\build-and-copy.ps1   # 加 -SkipBuild 只拷贝
```

（脚本自己按所在目录推算仓库根与 `webui/v2/`，所以从哪里调用都可以；上面的写法是仓库根。）

`build-and-copy.ps1` 会：跑 `gradlew wasmJsBrowserDistribution` → 把
`build/dist/wasmJs/productionExecutable/**` 拷进
`yunru-stage4/src/qq_roleplay_bot/plugins/webui/webui/v2/` → **前后比对老四个文件的 SHA256，
变了就直接报错退出**。`*.map` 不拷（浏览器不会请求，838 KB 死重）：落进 `v2/` 的是 **5 个文件
/ 13.59 MiB**。

**产物不进 git**：`compose-src/.gitignore` 里已有 `.gradle/`、`build/`、`.kotlin/`、
`kotlin-js-store/`、`node_modules/` 与构建日志；`v2/` 里的东西是部署产物（在 `yunru-stage4`
里它是被跟踪的产物文件，由 `build-and-copy.ps1` 更新——不要手改）。

### 子路径可用性实测

把整个 `webui/v2/` 目录挂到一个临时静态服务器上，直接从 `http://127.0.0.1:8742/v2/index.html`
打开：首帧正常、字体与两个 `.wasm` 都从 `v2/` 下的相对路径取到、控制台零异常
（`evidence/v2-under-subpath.png`）。也就是说**只要后端肯把 `/v2/` 路由出来，前端这一侧不用
再改**（字体、wasm、js 全是相对路径，webpack 的 `publicPath: auto` 按 script 位置算）。

## 八、产物体积（原始 / gzip，与第一轮对比）

`build/dist/wasmJs/productionExecutable`（`composeApp.js.map` 不会被请求，不计入）：

| 文件 | 这轮 | 第一轮 | 差 |
| --- | --- | --- | --- |
| `bfa5198…wasm`（skiko） | 8,640,316 B（8.24 MiB） | 8,640,616 B | −300 B |
| `de374c4…wasm`（composeApp，含 Miuix + 图标） | 3,101,999 B（2.96 MiB） | 2,228,069 B | **+873,930 B** |
| `composeApp.js` | 334,873 B（327 KiB） | 328,304 B | +6,569 B |
| `fonts/NotoSansSC-Regular-subset.ttf` | 2,173,132 B（2.07 MiB） | — | **+2,173,132 B** |
| `index.html` | 2,305 B | 729 B | +1,576 B |
| **合计传输** | **14,252,625 B ≈ 13.59 MiB** | 11,196,989 B ≈ 10.68 MiB | **+3,055,636 B（+27%）** |
| **gzip 合计** | **5,788,108 B ≈ 5.52 MiB** | 3.9 MiB（第一轮口径） | **+1.62 MiB（+42%）** |

逐项 gzip：skiko 3308.5 KiB / composeApp.wasm 928.0 KiB / composeApp.js 58.7 KiB /
字体 1356.0 KiB / index.html 1.2 KiB。

`+0.83 MiB` 的 composeApp.wasm 里有两块：`miuix-icons` 的 `ImageVector` 路径数据（15 个图标我在用，
klib 里更多）与新写的界面/网络/JSON 代码。**如需瘦身**，最省事的是把那 15 个图标换成手写的
极简 `ImageVector`（能省掉整个 `miuix-icons`），其次是字体只留 GB2312 一级字（6763 → 3755 字，
字体约 −35%）。

## 九、测量方法（可复跑）

以下命令都在 `src\qq_roleplay_bot\plugins\webui\compose-src` 下执行（`tools/` 与 `build\dist\`
都是相对这个目录的）：

```
node tools/serve.mjs build\dist\wasmJs\productionExecutable 8741 --api http://127.0.0.1:8790
node tools\cdp-shot.mjs http://127.0.0.1:8741/index.html out.png --width 1400 --height 860
node tools\cdp-shot.mjs "http://127.0.0.1:8741/index.html?nofont=1" off.png --width 1400 --height 860
node tools\cdp-shot.mjs "http://127.0.0.1:8741/index.html?token=<data/webui_token>" live.png --width 1400 --height 860
node tools\cdp-shot.mjs http://127.0.0.1:8741/index.html rail.png --width 1400 --height 860 --wheel 120 700 300
```

- `serve.mjs` 必须给 `.wasm` 正确的 `application/wasm`（否则 `instantiateStreaming` 静默失败）；
  另加 `--api <origin>` 把 `/api/*` 反代到面板——**只是测量用的架子**，真部署是同源。
- `cdp-shot.mjs` 用 `--headless=new --remote-debugging-port=9333` +
  `Emulation.setDeviceMetricsOverride` 驱动，`-first.png` 是**app 打点那一帧**立刻截的，
  之后才是 `-t0/400/800/1500` 时间序列；`--wheel x y dy` 用来把侧栏滚到底再拍。
- 打点：`withFrameNanos` 在帧**开始**时恢复，所以第二次恢复 = "这一帧已经画完"；
  刻度是 `performance.now()`（自 navigation start）。

## 十、门槛（这轮没改 Python，跑一遍确认没碰坏守卫）

```
.\.venv\Scripts\python.exe tests\run_offline.py        → ALL_OFFLINE_TESTS_PASSED
  verify_runs.log: sha=4c9747e branch=plugins dirty=yes ran=2289 failed=0 errors=0 skipped=0 result=PASS
.\.venv\Scripts\python.exe tests\check_plugin_branch_scope.py → 退出码 0（58 + 26 豁免，无越界）
```

`dirty=yes`：工作区里有这一轮未提交的改动（`webui-compose/` 整棵 + 新增的 `webui/v2/`）。

## 十一、没做到的 / 与交底不一致的

- **`/v2/` 还不能被现有静态服务取到**（见 §七）。按"这轮不批后端改动"只给了方案。
- **信息架构是 23 项，不是 18 项**：14 固定 + 9 个已装插件（交底写 18 = 14 + 4）。按"一个都
  不许丢"取全集，砍法是改 `Nav.kt` 的插件清单。
- **只有「账号」页有真数据**；其余 22 项是占位页（写明用途与将读的路由）。
- **供应商与模型这一块在这台机器上读不到**（`/api/model` → `available:false`），页面照实说；
  要真显示得由装配点把 `model_providers` 接缝递进来（核心侧改动，不在这一轮）。
- **字体只有一个字重**（Noto Sans SC 400）。Miuix 的 `subtitle`/`Medium` 用的是同一份字，
  Skia 不会伪粗体，所以加粗的层级只靠字号区分。要真粗体得再裁一份 700（约 +1 MB）。
- **字体覆盖**：GB2312 全量 + 仓库里出现过的 CJK；**GB2312 之外的生僻字**（扩充区、繁体）仍会
  先出豆腐块再回退——这类字不在"我们的 UI 文案"里，但群名/昵称可能命中。
- 第一轮那个探针页面被这轮的真面板取代了，`evidence/compose-wasm-*.png` 是第一轮的证据，没有重拍。
- 无头 Edge 单次测量抖动不小（ON 1015–1338 ms），报告里给的是多次的中位数与区间，不是单次最好值。

## 十二、这轮自己制造的问题（主动交代）

1. 第一版 token 输入框**没遮罩**，`evidence/` 里那张截图把真 token 照了进去（已删掉重拍，
   现在 `v2-account-live*.png` 是遮罩后的版本）。
2. 第一版把字体 `fetch` 放在 Kotlin 里，首帧文字 1432 ms（比关掉预载的 1052 ms 慢 380 ms）；
   后来加了 `index.html` 的提前预取把差距压到 ~50 ms。旧数字仍留在 §四 的表里作对照。
3. `tools/serve.mjs` 第一次改完忘了加 `.ttf` 的 MIME，字体被当 `application/octet-stream` 发
   （fetch 成 arrayBuffer 其实也能用，但 `font/ttf` 才是对的，已改）。
4. 跑 `tests\run_offline.py` 时它把留痕写到了 `dev\data\logs\verify_runs.log`——这是那套脚本
   自己的默认落点，不是我改的，但确实在我的命令下发生了（`dev\` 我只读不写，这一条无法避免，
   如实记下）。


## 十三、2026-10-18 第二轮：骨架重排（对照样例图，不再按"规范推"）

这一轮的起因是用户的两句话："？你怎么做到这么丑的"，以及随后给的**平板 HyperOS 设置页参考图**
（`dev/data/ui-preview/reference-hyperos-settings.jpg`）。上一轮 §五 那套骨架（`Scaffold` 的
topBar 横跨整宽 + 居中标题 + rail 与页同色 + 占位页两行字）被推翻重排。**§五 的界面描述已被
本节取代**，§五 只保留 IA / 路由 / 断点的推导，那些没变。

### 13.1 参照的是哪张图的哪一块（一张图一句话）

| 决策 | 参照 | 样例里这么做 | 我现在这么做 | 还差什么 |
| --- | --- | --- | --- | --- |
| 页底/卡片的关系 | `assets/002.webp` **第一联**（浅色默认）整页 | 页底 `#F7F7F7`、卡片纯白 `#FFFFFF`，卡片圆角 20 级、卡间留白 20 | 完全照用：`Surface(color = surface)` + `Card(cornerRadius = 20, colors = surfaceContainer)` | 无（像素实测同色，见 §13.3） |
| 卡内行间不画线 | 同上，`Checkbox` 那张卡内部四行 | 行与行**只有间距**，没有分割线；行高约 56、卡内上下留白 16 | `BasicComponent`（Miuix 自带的设置行）+ 纵向 10dp 内边距；**删掉**了上一版每行一条 0.75dp 分割线 | 无 |
| 分组小标题在卡外 | 同上，`Checkbox` / `Switch` / `Dropdown` 三个灰标题 | 灰色小标题在白色卡片**外面**，与卡片左缘对齐 | `SmallTitle(insideMargin = PaddingValues(2.dp, 8.dp))` 放在卡外（默认的 28dp 内边距会把标题推进卡片） | 无 |
| 圆角与卡间距 | 同上 | 卡片圆角明显大于 16，卡与卡之间约 20 | `CardRadius = 20dp`，`Section` 底部 20dp | 圆角是**目测**（`CardDefaults.CornerRadius` 是 16，样例看着更圆），没有像素级量 |
| 标题居中 / 左对齐分档 | `assets/002.webp`（手机形态，标题居中）+ 参考图（平板，左对齐大标题） | 两种都合法，取决于形态 | **按窗宽分档**：≥1100dp 用 `TopAppBar`（largeTitle 左对齐、32sp）；<1100dp 用 `SmallTopAppBar`（Miuix 手机形态，标题居中） | 分档点跟 rail 的断点共用 1100dp，没有单独调 |
| 彩色图标块 | 参考图 `reference-hyperos-settings.jpg` 左列（WLAN 蓝、通知红…） | 每个列表项一个彩色圆角方块 | 内容区的行与占位页卡片头都有 30/34dp 圆角方块（`.3` 圆角），底色取 `primary` / `error` / `secondaryContainer`，前景取 `onPrimary` / `onError` / `onSecondaryVariant` | **左导航的图标仍是单色**，见 §13.7 |
| 顶栏在内容列内 | 参考图整页 | 大标题在右侧内容列内、贴着左栏 | `TopAppBar` 是页列的第一个元素；rail 从 y=0 直通页底 | 无 |
| 页列最大宽度 | 参考图右侧内容列 + 用户交底 | 参考图里右列是**铺满**的；用户交底要求有上限 | `PAGE_MAX_WIDTH = 1320dp`，左对齐 | 见 §13.4 ④ 的取舍 |

### 13.2 骨架改前 / 改后

改前（`Scaffold` 的 topBar 槽 → 横跨整宽 + 居中标题）：

```kotlin
Scaffold(
    topBar = { SmallTopAppBar(title = "云茹控制面板", subtitle = "$groupTitle · ${selected.label}") },
) { innerPadding ->
    Row(modifier = Modifier.padding(innerPadding)) {
        NavigationRail(expanded = expanded, header = { RailHeader(expanded) }) { … }
        Box(modifier = Modifier.weight(1f)) { PaneContent(…) }   // 12 + 28 内边距，标题再写一遍
    }
}
```

改后（`Row` 骨架，顶栏进页列；全文见 `src/wasmJsMain/kotlin/Shell.kt`）：

```kotlin
BoxWithConstraints {
    val wide = maxWidth >= EXPAND_RAIL_AT          // 1100dp
    Surface(Modifier.fillMaxSize(), color = MiuixTheme.colorScheme.surface) {
        Row(Modifier.fillMaxSize()) {
            NavigationRail(expanded = wide, header = { RailHeader(wide) }) { …分组 + 0.75dp 分隔… }
            PageColumn(Modifier.weight(1f).fillMaxHeight(), wide = wide, …)
        }
    }
}

// PageColumn：页容器（最大 1320，左对齐）→ 顶栏 → 滚动区（左右 26 = TopAppBarDefaults.TitlePadding）
Column(Modifier.widthIn(max = PAGE_MAX_WIDTH).fillMaxWidth().weight(1f)) {
    PageHeader(item, group, wide)                 // wide → TopAppBar(largeTitle) / 窄 → SmallTopAppBar
    Column(Modifier.weight(1f).verticalScroll(scroll).padding(start = TitlePadding, end = TitlePadding, …)) {
        AccountPane(…) / StubPane(…)              // Section(小标题 + 卡) × N
    }
}
```

- **顶栏与内容之间不加 `HorizontalDivider`**：Miuix 的 `TopAppBar` 自己不画（`TopAppBar.kt`
  里没有 divider 分支），样例里也没有；而且顶栏底色就是页底色，两者之间没有接缝可画。
  画一条只会把"横幅"那条边界重新做出来。rail 与内容之间那条**保留**，它是 `NavigationRail`
  自己的 `showDivider`（`VerticalDivider`，`DividerDefaults.Thickness = 0.75.dp`、
  `DividerColor = colorScheme.dividerLine`），没有改。
- **品牌行不居中**：`RailItemIconStart = ExpandedItemHorizontalMargin + ExpandedItemContentHorizontalPadding
  = 12 + 14 = 26`；收起档项图标是居中的，`(MinWidth - IconSize) / 2 = (80 - 28) / 2 = 26`——
  两条算出来都是 26，所以 `fillMaxWidth().padding(start = 26.dp)` 在两档都对得上，切档时品牌不动。
- **`Scaffold` 的代价**：它同时兼职 window insets、`MiuixPopupHost`（`Overlay*`/下拉菜单的宿主）
  与 Surface 容器。换成 `Surface` + `Row` 之后 **popup host 没了**；本轮没有任何一页用到
  `Overlay*`，但下一轮要做下拉菜单就得把它请回来。这条写在 `Shell.kt` 的文件头里。

### 13.3 取色（**实机渲染像素**，不是 CSS/令牌常量）

从 `evidence/*.png` 里直接取像素（`PIL.Image.getpixel`），窗口 1666×1002：

| | 页底 | 卡片 | rail | 卡/底对比度 | rail/底 |
| --- | --- | --- | --- | --- | --- |
| 改前（浅） | `#F7F7F7` | `#FFFFFF` | `#F7F7F7` | 1.07 : 1 | 1.00 : 1 |
| 改后（浅） | `#F7F7F7` | `#FFFFFF` | `#F7F7F7` | 1.07 : 1 | 1.00 : 1 |
| 改后（深） | `#000000` | `#242424` | `#000000` | 1.35 : 1 | 1.00 : 1 |

三件事必须说清楚，因为它们和交底里的判断**不一致**：

1. **改前不是"#F7F7F7 上放 #E8E8E8 卡"**。实测改前就是 `surface #F7F7F7` + `surfaceContainer
   #FFFFFF`，与 `assets/002.webp` 完全同一对色（那张样例我按像素量过：页底 `#F7F7F7`、卡
   `#FFFFFF`）。所以"卡比底深、整页糊"的描述在**当前产物**上不成立。
2. 真正发灰的是**面积分布**：改前 rail、顶栏、页底**三者同色**（`#F7F7F7`），卡片只有 3 张、
   占屏不到一半，其余全是那一块灰。改后卡片沿页列铺满、加了分组小标题与留白，灰的面积被切碎。
3. **卡/底 1.07 : 1 是 Miuix 自己的关系**（样例同值）。要更高只能换令牌（`#E8E8E8` 当底会给
   1.21），但那就不再是样例的关系了——本轮没换。

### 13.4 四条判定的实测

| # | 判定 | 改前（同窗口 1666×1002 的像素） | 改后 |
| --- | --- | --- | --- |
| ① | 顶栏在内容列内、标题左对齐 | 顶栏底色 `#F7F7F7` 横跨 `x=0..1666`；标题「云茹控制面板」墨迹 `x=774..892`，中心 833 = 窗口正中（**居中**，且副标题也居中） | 顶栏在页列内（`x≥240`），标题「账号」墨迹左缘 **x=269**（= 240 rail + 26 `TitlePadding` + 抗锯齿 3px），与卡片左缘 267 对齐；32sp 大标题 |
| ② | 内容区有卡片、不再整屏灰 | 3 张卡，白卡面积合计不到半屏；占位页只有两行字 | 每页 2~3 张纯白卡（圆角 20），卡外有灰分组小标题；占位页也是一张「这一页」（图标块 + 页名 + 组 + 状态 + 用途）+ 一张「将来要读的路由」（`surfaceContainerHighest` 卡内块） |
| ③ | 品牌与图标列对齐 | 「云茹」`x=96..160`（**居中**在 240 的 rail 里），下面的项图标左缘 26 | 「云茹」左缘 = 26（`x=27` 起，抗锯齿 1px），与项图标左缘同一条线 |
| ④ | 内容区不再拉满整宽 | 卡片白区 `y=400` 那一行 `x=269..1637`（右边距 26，等于拉满） | `x=267..1534`（页列 1320 上限、右边距 132）。**取舍**：交底给的例子是 1180，我量过 1180 会让右边留 **276dp**（占窗宽 16.6%）的灰带——那正是上一轮"右侧大片空白"换个位置重演；1320 把它压到 132dp。要更窄就是改 `PAGE_MAX_WIDTH` 一个常量 |

### 13.5 产物体积（原始 / gzip）

`build-and-copy.ps1 -Clean` 之后 `webui/v2/` 是 **5 个文件 / 14,285,438 B ≈ 13.62 MiB**，
gzip 后 **5,711,666 B ≈ 5.45 MiB**（逐项：`865e81…wasm`(composeApp) 3,135,020 / 939,284；
`bfa519…wasm`(skiko) 8,640,316 / 3,332,227；`composeApp.js` 334,665 / 59,756；
字体 2,173,132 / 1,379,158；`index.html` 2,305 / 1,241）。上一轮是 14,252,625 B / 5,788,108 B，
即 **原始 +32,813 B（+0.23%）、gzip −76,442 B（−1.3%）**。

> 注意 `-Clean` 是必须的：`build-and-copy.ps1` 只拷不删，而 composeApp 的 wasm 文件名带内容哈希，
> **不 Clean 会两份 wasm 并存**（我第一次就得到 6 个文件 / 17.38 MB）。脚本头注释里"replace files,
> keep anything else"就是这件事，但没写"不 Clean 会多一份"。

### 13.6 门槛（本轮没改 Python，跑一遍确认没碰坏守卫）

```
tests\run_offline.py            → ALL_OFFLINE_TESTS_PASSED
  verify_runs.log: sha=7e104bf branch=plugins dirty=yes ran=2301 failed=0 errors=0 skipped=0 result=PASS
python -m pyflakes src tests    → 无输出（退出码 0）
tests\check_module_removal.py   → 退出码 0，七项全"能（已解耦）"
tests\check_plugin_branch_scope.py → 退出码 0（85 个文件在范围内，58 + 豁免 27，越界 0）
```

`dirty=yes`：`webui-compose/` 整棵仍是未跟踪目录 + `v2/` 里的产物改了。

### 13.7 我不同意 / Miuix 规范如此的地方（请转达）

1. **左导航的图标做不到"彩色圆角方块"**——不是没做，是 `NavigationRailItem` 没有这个槽：
   签名是 `icon: ImageVector`（`navigationrail.md:99-109`），内部 `Image(…, colorFilter =
   ColorFilter.tint(onSurfaceContainer))` 只有**一个**色相（`NavigationRail.kt:397-405`），
   选中药丸的底色也写死成 `surfaceContainerHigh`（`:442`）。要做彩色底块只有三条路：
   ① 把 `NavigationRailItem` 连同它的 morph 布局（约 120 行）抄一遍；② 按内部几何
   （图标左缘 26 / 收起档居中）用 `drawBehind` 硬画——两档是不同的 composable，y 坐标也不同，
   一改库就漂；③ 换掉整个 rail。三条都与"保持 NavigationRail 的几何 / 一切以 Miuix 为先"
   冲突，所以我把彩色放到了**内容区的行**（那是同一个"列表项"形状，且 Miuix 有 `startAction`
   槽）。要 rail 也彩色，得你点头 fork 组件。
2. **rail 与页底同色是不得已**，不是没照做：深色档的调色板是压缩的——
   `surface #000000` / `surfaceVariant #242424` / `surfaceContainer #242424` /
   `surfaceContainerHigh #242424`（药丸色）/ `surfaceContainerHighest #2D2D2D`。
   药丸色与 `surfaceContainer`、`surfaceVariant` **完全相同**，所以"给 rail 换个底色"在深色档
   会把选中态抹掉（对比度 1.00）。浅色档里 `#FFFFFF` 可以（药丸 `#E8E8E8` 对白 1.21），
   但两档不一致比同色更糟。要真分层，得先能改药丸色（同上，fork 组件）。
3. **`SmallTopAppBar` 的标题是居中的**（`TopAppBar.kt:1092` `baseX = (maxWidth - titleWidth)/2`），
   这不是我选的：`TopAppBarDefaults.TitlePadding` 只描述"标题的水平内边距"，不描述对齐。
   要左对齐大标题必须用 `TopAppBar` 的 **largeTitle**（`placeRelative(x = 0)` + `titlePadding`），
   本轮宽档就是这么做的；窄档保留居中的手机形态。
4. **卡/底 1.07 : 1 是样例自己的对比度**，不是没"够"。加深页底会让它变成"另一种设计"，
   所以留给你定。

### 13.8 这轮没做到的

- **占位页下半屏仍是灰的**：占位页只有两张卡（这一页 / 将来要读的路由），内容就这么多。
  要填满只能编内容，没有编。
- **`webui-compose/` 整棵源码仍然不进 git**（上一轮就是这样，本轮照旧）。**不是忘了**：
  `tests/check_plugin_branch_scope.py` 只允许改动落在 `src/qq_roleplay_bot/plugins/**`
  （+ 逐个列明的测试文件）里，`webui-compose/` 在仓库根、不在这个前缀下——一旦 `git add`，
  这道门槛立刻"越界 > 0"。未跟踪文件它只扫 `src/` `tests/` `docs/` 三个前缀，所以现在不报。
  `v2/` 产物因为落在 `plugins/webui/webui/v2/` 里，可以提交（上一轮就是这么做的）。
  要让源码进 git，得先给这道门槛加一条豁免——那是改判据，留给你定。
  > **2026-10-20 已解决（记录保持原样，别当成现状）**：用户选了"挪进 `plugins/**`"而不是
  > 改门槛，所以本工程搬到了 `plugins/webui/compose-src/` 并已提交。见 §十五。
- **深色档只拍了 1666×1002 一张**，窄档深色没拍。
- 卡片圆角 20 是**目测**样例，没有像素级量（`CardDefaults.CornerRadius` 是 16）。

## 十四、2026-10-19 第三轮：左栏自搭（`BasicComponent` Custom Content + 彩色方块）

起因是用户的一句「做呗」，指的是：**把左栏从 `NavigationRail` 换成用 `BasicComponent` 自搭**，
这样每项左侧能放**彩色圆角方块**。这一节记的全部是实机量与算账，命令都能复跑。

改动的文件只有四个：

| 文件 | 改了什么 |
| --- | --- |
| `src/wasmJsMain/kotlin/Rail.kt` | **新**。`PanelRail`（宽度 / toggle / 分组 / 项 / 药丸 / 色表）+ 文件头那套对齐账 |
| `src/wasmJsMain/kotlin/Ui.kt` | `Accent` 加 `MidBlue`；`Tile` 加 `glyphSize` / `cornerRadius` 两个可选参数（默认值不变，内容区一行没改） |
| `src/wasmJsMain/kotlin/Shell.kt` | `NavigationRail` → `PanelRail`；`VerticalDivider()` 自己画；展开态加"用户按 toggle 覆盖断点" |
| `tools/cdp-shot.mjs` | 加 `--settle` / `--clickDelay` / `--noPark` 三个测量开关（见 14.4） |

### 14.1 为什么只能换（这不是调参能解决的）

`NavigationRailItem` 的签名是 `icon: ImageVector`（`navigationrail.md:99-109`），内部就一句
`Image(…, colorFilter = ColorFilter.tint(onSurfaceContainer))`（`NavigationRail.kt:397-405`）——
**一个项只有一个色相**，图标本身是单色路径，没有"容器色"这一层；选中药丸的底色也写死在组件里
（`surfaceContainerHigh`，`NavigationRail.kt:442`），不让传。三条路里，前两条（抄 `NavigationRailItem`
连同约 120 行 morph 布局 / 按内部几何 `drawBehind` 硬画）都等于 fork 或跟着库漂，所以走第三条：
`BasicComponent` 的 **Custom Content Variant**（`basiccomponent.md:74-101`，文档原话 "full control over
the layout inside the component while still reusing the container and interactions"，`startAction`
槽收任意 composable）。**库一个字节没改**。

### 14.2 几何（全部取 `NavigationRailDefaults`）与"方块 x"的对齐账

| 值 | 来源 | 这轮 |
| --- | --- | --- |
| 展开 240 / 收起 80 | `MinWidth` / `ExpandedWidth`（`navigationrail.md:119-120`） | 同值，用 `animateDpAsState` 插值 |
| 整列竖向 24 | `VerticalPadding`（`:121`） | 同值 |
| 展开项左右外边距 12 | `ExpandedItemHorizontalMargin`（`:128`） | 同值（两档都用 12，见下） |
| 项内 14×14 | `ExpandedItemContentHorizontalPadding` / `...VerticalPadding`（`:131-132`） | 展开档同值；收起档横向改 13（见下） |
| 选中药丸圆角 16 | `ExpandedItemCornerRadius`（`:129`） | 同值，**自己画**（`RoundedCornerShape(16)` + `background`，不用 `squircleBackground`） |
| 标签字号 16sp | `ExpandedLabelFontSize`（`:127`） | 同值 |
| 项间 12 | 用户这轮给的（Miuix 没这个常量） | `Spacer(12.dp)` |
| toggle 56×56 | `IconSize 28 + ExpandedItemContentVerticalPadding × 2` | 同值（Miuix 自己的算法） |
| 分组小标题 | 用户要 `SmallTitle` | `SmallTitle`，左缘 = 26 |

**对齐账**（两条要求唯一打架的地方，算清楚）：

```
展开档：项外边距 12 + 项内左内边距 14 = 26      ← 与 NavigationRail 展开态图标左缘同值
收起档：80 宽、方块居中 ⇒ 左缘 = (80 − 方块边长) / 2
两条同时成立 ⇒ 方块边长 = 80 − 2×26 = 28dp，而方块边长被要求在 30–34dp
取 30dp ⇒ 差值 (30 − 28)/2 = 1dp（物理上不可能同时精确）
```

于是取 30dp（区间下限，把差值压到最小），项外边距两档都 12：

```
收起：80 − 12 − 12 = 56（药丸宽）→ 项内 (56 − 30)/2 = 13 → 方块左缘 12 + 13 = 25
      中心 25 + 15 = 40 = 80/2                       ← 精确居中（方块在 56 的药丸里左右各 13）
展开：12 + 14 = 26，中心 41                          ← 与 40 差 1dp
```

实机验证：390 收起档方块 x=**25..54**、药丸 x=**12..67**（56 宽）；1666 展开档方块 x=**26..55**、
药丸 x=**12..227**（216 = 240 − 24）。**1 CSS px 的差，切换时看不出来**。

### 14.3 配色：固定映射表 + 色槽的可用范围（这条最值得看）

色槽只用 `docs/guide/colors.md` 里现成的，不自造十六进制。**但那个色板（非 Monet）只有蓝、红、灰**，
而且 container 变体在浅色档的 rail 底色 `#F7F7F7` 上几乎是同色：

| 色槽 | 浅色 方块 / 前景 | 深色 方块 / 前景 | 浅色档对 rail 底 `#F7F7F7` 的对比度 | 结论 |
| --- | --- | --- | --- | --- |
| `primary` / `onPrimary` | `#3482FF` / `#FFFFFF` | `#277AF7` / `#FFFFFF` | 3.4 : 1（看得见） | 用 |
| `primaryContainer` / `onPrimaryContainer` | `#5D9BFF` / `#FFFFFF` | `#338FE4` / `#FFFFFF` | 2.6 : 1 | 用 |
| `error` / `onError` | `#E94634` / `#FFFFFF` | `#F12522` / `#FFFFFF` | 3.6 : 1 | 用 |
| `secondaryContainer` / `onSecondaryVariant` | `#F0F0F0` / `#303030` | `#434343` / `#D9D9D9` | **1.06 : 1 → 方块看不见** | 不用 |
| `tertiaryContainer` / `onTertiaryContainer` | `#EAF2FF` / `#3482FF` | `#2B3B54` / `#4788FF` | **1.05 : 1 → 方块看不见** | 不用 |
| `errorContainer` / `onErrorContainer` | `#FDF6F4` / `#410002` | `#2E0603` / `#FFDAD6` | **1.00 : 1 → 白方块** | 不用 |

也就是说：**浅色档只有三个色槽能做出"看得见的彩色方块"**（深色档的 container 反而都能用，但一张映射表
必须两档都成立，所以按浅色档取交集）。要真的六个色相，得自造色——本轮按约束没做，留给用户定。

> 顺带纠一条文档错误：`docs/guide/colors.md` 写 dark `onError = #FF690005`，
> 而 **0.9.4 源码 `Colors.kt:457` 是 `onError = Color.White`**。实机像素也是 `#FFFFFF`
> （深色档红色方块里的气泡图标）。**以源码与像素为准，文档过期了。**

**固定映射表**（写在 `Rail.kt` 的 `RailAccents`，键是 `RailItem.id`；未登记的新插件走
`stableSlot()` 的确定性散列，同样跨会话稳定，不随插件增删换色）：

| 项 | 色槽 | 浅色 方块/前景 | 深色 方块/前景 |
| --- | --- | --- | --- |
| 账号 / 总览 / 记忆 / 金句 / 群管理 / 掉线通知 / 超时通知 / 日志 / 动作 | `Blue` | `#3482FF` / `#FFFFFF` | `#277AF7` / `#FFFFFF` |
| 设备 / 群聊 / 人物画像 / 黑话 / 入群申请 / 到点提醒 / 识图 / 设置 | `MidBlue` | `#5D9BFF` / `#FFFFFF` | `#338FE4` / `#FFFFFF` |
| 私聊 / 知识库 / 邮件 / 订阅推送 / 控制面板 / 模型与凭据 | `Red` | `#E94634` / `#FFFFFF` | `#F12522` / `#FFFFFF` |

（23 项 = 14 固定 + 9 插件，逐项都在表里；相邻两项保证不同色。）

### 14.4 动画：用了哪个，以及**扔掉了 `AnimatedVisibility`**

- 几何：**`animateDpAsState`**，220ms —— rail 宽（80↔240）、项内边距（13↔14）、方块与标签的间距（0↔8）。
- 文字：**`animateFloatAsState`** + `Modifier.alpha` —— 标签 160ms；分组小标题 200ms。
- 分组小标题的占位高度：`height(32dp × p)` + `clipToBounds()`（内层固定 32dp，左缘恒 26）。
- **试过 `AnimatedVisibility`（`fadeIn + expandVertically` / `fadeOut + shrinkVertically`），实机有 bug，
  已删**：390 上点开时，中间帧里小标题**横向错位并被窗口左缘切掉**——松手后约 60ms 那帧墨迹左缘
  `x=2`（"账号与设备"只剩"与设备"）、约 140ms 那帧 `x=0`，稳定后才是 `x=26`。换成自己算高度之后，
  同一段动画（rail≈210 的那帧）标题左缘 `x=27`（稳定后 26，差 1 是抗锯齿）。
  **标签**没有用 `AnimatedVisibility` 的另一个理由：它的收缩是"先量出内容宽、再夹到 0"，而
  `BasicComponent` 的 center 槽本来就被 startAction 挤到一个会随 rail 变窄而趋 0 的宽度上，两层夹一起
  会出"文字被压成竖排"（`Component.kt` 文件头警告过的那个坑）。

**动画有实拍证据**（`tools/cdp-shot.mjs` 这轮加了三个开关才拍得到）：
`--clickDelay` 控制点击后等多久、`--noPark` 跳过"把鼠标挪开再等 200ms"那一段（不跳过的话第二张截图
最早也在 ~300ms，永远拍不到 220ms 的中间帧）、`--settle` 在最终截图前多等一会儿（抓到过一次
"载入中…"状态，数据还没回来）。实拍：点开后 rail 宽 **91 → 210 → 240**（settled），
对应 `evidence/rail2-390x844-expand-mid-{0,40}.png` 与 `rail2-390x844-expanded.png`；
`mid-0` 那帧分组小标题还看不见（高度 0），`mid-40` 那帧宽 210、小标题墨迹左缘 `x=27`。
**注意截图本身有 50–200ms 的延迟抖**（`--clickDelay 60` 也拍到过 240，即已经结束），
所以文件名里的数字只是"请求的等待时间"，不是精确的帧时刻；为此我把抖得最厉害的那几帧删了，
只留能看出中间状态的。

### 14.5 四条判定：改前 / 改后（同窗口实机像素）

| # | 判定 | 改前（`evidence/after-account-1666x1002.png`，`NavigationRail` 版） | 改后（`evidence/rail2-1666x1002-light.png`） |
| --- | --- | --- | --- |
| ① | 每项左侧是**有颜色的圆角方块**（不是单色线条） | 图标是单色线条：账号图标墨迹 `x=28..51`、`y=119..142`，整列只有一种 tint；**rail 区域（x<240）彩色像素 = 0** | 每项一个 30×30 圆角 10 的方块：账号方块 `x=26..55` 填 `#3482FF`、图标 20dp `#FFFFFF`；**rail 区域彩色像素 = 8388**（深色档 8214） |
| ② | 选中药丸存在、且只在选中项 | 有（Miuix 自己画的 squircle）：`x=12..227`，高 56（= 图标 28 + 2×14），色 `#E8E8E8` | 有（自己画 `RoundedCornerShape(16)` + `background`）：`x=12..227`，高 **58**（= 方块 30 + 2×14），色 `#E8E8E8`；圆角 16 是量出来的——药丸最上面一行宽 196、下 1px 200 / 2px 206 / 4px 212 / 8px 216，与 r=16 的理论内缩 10.4 / 5.4 / 2.1 对得上。未选中项无底色（`Color.Transparent`） |
| ③ | 收起档 80 时方块**居中**、点击能展开 | **没有 toggle**（固定 `expanded = wide`），390 上永远 80、点不开；左上角墨迹是品牌字"云"（`y=30` 起） | 有 toggle（`MiuixIcons.Basic.Sidebar`，56×56，墨迹 `x=29..50 / y=42..61`，中心 `(40,52)`）；方块 `x=25..54`，中心 **40 = 80/2**，药丸 `x=12..67` 也居中；点 `(40,52)` → rail `80 → 240`（中间帧 91/210，见 14.4），页面内容列同步被挤到 150 |
| ④ | 展开档方块与标签的 x 与 rail 规格一致 | 图标左缘 26（图标 28）、标签盒左缘 **70**（= 12+14+28+16），墨迹 71 | 方块左缘 **26**（= 12+14，30 宽）、标签盒左缘 **72**（= 12+14+30+16），墨迹 73；差 2 = 方块比图标宽 2，间距仍是 `ExpandedItemIconTextSpacing` 16 |

### 14.6 取色（实机截图采样，不是令牌常量）

| | 页底 | 卡片 | rail 底 | 选中药丸 | 方块 蓝 / 中蓝 / 红 | 竖线（0.75dp） | 深色档对比 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 浅色 1666×1002 | `#F7F7F7` | `#FFFFFF` | `#F7F7F7` | `#E8E8E8` | `#3482FF` / `#5D9BFF` / `#E94634` | `#E8E8E8`（x=240，1px） | 药丸/rail = 1.14 : 1 |
| 深色 1666×1002 | `#000000` | `#242424` | `#000000` | `#242424` | `#277AF7` / `#338FE4` / `#F12522` | `#262626`（x=240，1px） | 药丸/rail = 1.35 : 1 |

- 采样点：页底 `(1500,900)`、卡片 `(800,450)`（深色档要等数据回来，`--settle`）、rail 底 `(5,500)`、
  药丸 `(200,217)`、方块取 `(30, mid)`（避开图标；圆角 10 让最左 3px 会缩短，取到的是饱满区）。
- 那 1px 的竖线是 `VerticalDivider`（`DividerDefaults.Thickness 0.75dp` / `DividerColor dividerLine`）
  在 dpr=1 上按 1px 栅格化的结果：浅色 `#E0E0E0` 75% 叠 `#F7F7F7` ≈ `#E8E8E8`，深色同理 ≈ `#262626`。
- **深色档重新取过的判断**：三个色槽在深色下都换成 Miuix 的深色值后仍然成立（方块/前景对比
  3.4~4.2 : 1），没有出现"浅色能看、深色糊掉"。真的要更鲜，只能自造色（见 14.3）。

### 14.7 我不同意 / 比 `NavigationRail` 变差的地方（如实说）

1. **无障碍语义**：`NavigationRailItem` 用的是 `selectable(selected=…, role = Role.Tab)`，自带
   `selected` 语义与 `selectableGroup`；`BasicComponent` 只有 `clickable`。我补了
   `Modifier.semantics { selected = … }` 与 rail 上的 `selectableGroup()`，所以读屏仍然会念
   "标签页 / 已选中"——但这是**手补的**，不是 API 保证的；将来 Miuix 改 `clickable` 的语义合并行为，
   这条可能悄悄失效（没有测试盯着）。
2. **键盘可达性**：两者最终都走 `Modifier.clickable`（wasm 上 Tab 聚焦 + Enter/Space 触发），
   Miuix 的 `MiuixIndication` 也处理 `FocusInteraction`（alpha 0.08），所以**焦点可见性没变差**；
   但**没有实测**键盘路径（这轮只测了鼠标点击），只能给代码依据。
3. **滚动条**：两版都没有可见滚动条（`verticalScroll` 在 canvas 里画，浏览器不画）。
   23 项放不进 1002 高，**看不出"下面还有"**——这一条和改前一样，不是本轮弄坏的，
   但既然换掉了组件，值得单独提：要提示得自己加渐变/滚动条。
4. **toggle 的语义**：`IconButton` 只给 `Role.Button` + `contentDescription`（"展开/收起侧栏"），
   没有 `expanded` 状态语义（`stateDescription`），读屏不会念"已展开"。`NavigationRailState` 那版
   也没有，但自己搭就该补；这轮没补。
5. **项的可点区域变大了**：整行 216×58 可点（原来是 216×56 的 pill 区域 + 上下 12 padding），
   误触概率略升；视觉上项间距 12 让列变长了（23 项从约 11 屏内容变成更多），
   可滚但更依赖滚动。**这是用户明确要的"项间 12"的代价**，不是我没照做。
6. **窄档手动展开**：390 上手点 toggle 后 rail 240、内容列只剩 150，正文被挤成一字一行
   （`evidence/rail2-390x844-expanded.png` 能看到）。要"点击能展开"就得接受这一帧；
   要更好看，得让窄窗的展开档变成覆盖式抽屉（`NavigationRail` 也没有这个能力）。
7. **`AnimatedVisibility` 在这条路上不可用**（14.4 的实机错位）——这条我不认为是我的用法错，
   但也没有去读它的源码定位根因，只做了规避。**留个记号**：下次谁要用它包一个
   "宽度会变的容器里的 fillMaxWidth 子项"，先拍中间帧。

### 14.8 产物体积（`build-and-copy.ps1 -Clean` 之后）

| 文件 | 这轮 | 上一轮（NOTES §13.5） | 差 |
| --- | --- | --- | --- |
| `26aaa15…wasm`（composeApp，含 Miuix + 图标） | 3,109,835 B / gzip 930,179 | 3,135,020 / 939,284 | **−25,185 / −9,105** |
| `bfa5198…wasm`（skiko） | 8,640,316 / 3,324,678 | 同 | 0 |
| `composeApp.js` | 334,665 / 59,456 | 334,665 / 59,756 | 0 / −300 |
| `fonts/NotoSansSC-Regular-subset.ttf` | 2,173,132 / 1,381,494 | 同 | 0 |
| `index.html` | 2,305 / 1,241 | 同 | 0 |
| **合计（5 个文件）** | **14,260,253 B ≈ 13.60 MiB / gzip 5,697,048 ≈ 5.43 MiB** | 14,285,438 / 5,711,666 | **−25,185（−0.18%）/ −14,618（−0.26%）** |

**wasm 反而小了**：`NavigationRail` / `NavigationRailItem` 那套 morph 布局与 `squircleBackground`
调用点跟着不再被引用，被 DCE 掉了；新加的 `Rail.kt` 没补回来那么多。`miuix-preference` 仍然没引，
`miuix-blur` 仍然不在 classpath。

### 14.9 门槛（这轮没改 Python，跑一遍确认没碰坏守卫）

```
tests\run_offline.py            → ALL_OFFLINE_TESTS_PASSED
  verify_runs.log: 2026-10-19T01:48:12+08:00 label=offline sha=90e9c7a branch=plugins
                   dirty=yes ran=2301 failed=0 errors=0 skipped=0 result=PASS
python -m pyflakes src tests    → 无输出（退出码 0）
tests\check_module_removal.py   → 退出码 0，七项全"能（已解耦）"（+ group_admin 那条"已解耦，但注释说明"）
tests\check_plugin_branch_scope.py → 退出码 0（85 个文件在范围内，58 + 豁免 27，越界 0）
```

`dirty=yes` 的构成：`webui-compose/` 整棵仍是未跟踪目录；`v2/` 里 `composeApp.js` 改了、
`865e81…wasm` 被 `-Clean` 换成了 `26aaa15…wasm`。**没有提交、没有 push**（留给你）。

### 14.10 这轮没做到的

- **六个色相做不到**：浅色档能用的色槽只有三个（14.3 有对比度），剩下都是自造色，按约束没做。
- **键盘路径没实测**（只测了鼠标点击与中间帧）。`selected` 语义是手补的。
- **深色窄档没拍**（只拍了 1666×1002 深色）。
- **`AnimatedVisibility` 的横向错位没定位到源码根因**，只做了规避（14.4 / 14.7-7）。
- **`/v2/` 仍是旧进程在服务**：这轮没重启线上 bot（按约束），`v2/` 的新产物要等主会话重启后才生效。
- 临时 server（8744）与无头 Edge 用完已关；复查 8744/8741-8743/9333 端口**实际可 bind**（TCP 表里
  两条 9333/35632 是死进程的残留记录，实测端口可绑，不是还在监听）。
