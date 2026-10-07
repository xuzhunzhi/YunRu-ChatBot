# 别人的插件生态 · 能复刻到什么程度

> 只读调研（2026-10-08）。外部事实都标了来源链接；我们这侧的行是**实测**（写在文件与行号上）。
> 网络实测：`registry.koishi.chat/index.json`、`registry.nonebot.dev/plugins.json` 两份索引已落盘读取，
> 计数都是本地数出来的，不是转述。

## 一句话结论（能复刻 / 只能复刻一部分 / 不建议）

**能复刻一部分，而且小**：`data/plugins/` 加载根 + 只从自家分支"装/卸" + 面板一页，改动落在
1 个加载根、1 个装配点、1 页面板；它们那套**中间件/事件钩子、给模型工具、自动装 pip 依赖、
运行时热重载、把整个宿主对象交给插件**一律不能抄——每一条都撞我们的硬边界（插件只产出内容或意图，
权限判定/执行/护栏/审计在核心）。

## 我挑了谁、为什么

| 挑了 | 为什么是它（回答哪几问） |
| --- | --- |
| **Koishi** | 生态最成熟：注册表 4731 条、服务级依赖（`inject`）、**多实例机制**、生命周期与副作用回收、面板里装卸（第 1/2/3/4/5/7 问最全）。[发布插件](https://koishi.chat/zh-CN/guide/develop/publish.html)、[服务与依赖](https://koishi.chat/zh-CN/guide/plugin/service.html)、[生命周期](https://koishi.chat/zh-CN/guide/plugin/lifecycle.html) |
| **NoneBot2** | Python、同语言：商店 942 条 + **NoneFlow 自动加载审核**、`require()` 依赖、装失败只记日志不带走别人（第 2/3/7 问）。[插件信息](https://nonebot.dev/docs/2.4.3/advanced/plugin-info)、[发布插件](https://nonebot.dev/docs/2.4.3/developer/plugin-publishing) |
| **AstrBot** | 形态最像我们：**预装/保留插件**、面板安装、`metadata.yaml` 清单、宿主版本范围、失败插件列表（第 1/2/3/7 问）。[插件市场 JSON 规范](https://docs.astrbot.app/dev/plugin-market/2026-06-27.html)、[发布插件](https://docs.astrbot.app/dev/star/plugin-publish.html) |
| 没选 MaiBot | 本仓已实测过（[PLUGIN_MARKET_SURVEY.md](../PLUGIN_MARKET_SURVEY.md)），重复没有新信息 |

## 机制对照表

### 1. 清单 / 元数据

| | 它们怎么做（附来源） | 我们现状（实测） | 差距 · 值不值得抄 |
| --- | --- | --- | --- |
| Koishi | `package.json`：`name` 必须 `koishi-plugin-*`（或 `@scope/koishi-plugin-*`、官方 `@koishijs/plugin-*`）、`version` 走 semver、`contributors`/`license`/`homepage`/`repository`/`keywords`，**必填** `peerDependencies.koishi`；另加 `koishi` 字段：`description`（多语言）、`service.{required,optional,implements}`、`preview`、`hidden`（[publish](https://koishi.chat/zh-CN/guide/develop/publish.html)） | 目录名即插件名；`plugin.py` 模块级 `register(registry)`、`TITLE`、`ENABLED`、`REQUIRES`；`inventory()` 返回 9 键（`name/title/note/builtin/enabled/loaded/requires/removable/panel`，`src/qq_roleplay_bot/plugins/__init__.py:1043`）。**无版本、无作者、无宿主 API 版本** | 缺"版本 + 宿主 API 范围"——装进来的插件必须能**拒装**。抄这两个；展示类字段按需 |
| NoneBot | 包名 `nonebot-plugin-*`（PyPI）、模块名 `nonebot_plugin_*`；代码里 `__plugin_meta__ = PluginMetadata(name, description, usage, type, homepage, config, supported_adapters, extra)`，**版本不在 metadata 里**而在打包元数据（[plugin-info](https://nonebot.dev/docs/2.4.3/advanced/plugin-info)）。商店每条字段实测：`module_name / project_link / name / desc / author / homepage / tags / is_official / type / supported_adapters / valid / time / version / skip_test`（942 条，`type` 里 `application` 677、`library` 61） | 同上 | 抄"发布必填项少而硬"（type/homepage/config）这条思路，不抄它的双份清单 |
| AstrBot | `metadata.yaml`：`name`（唯一标识）、`display_name`、`desc`、`version`（semver）、`author`、`repo`，可选 `short_desc`/`astrbot_version`/`support_platforms`/`social_link`/`tags`，目录里 `logo.png`（[publish](https://docs.astrbot.app/dev/star/plugin-publish.html)） | 同上 | 字段名可直接借用（`name/version/author/astrbot_version`）；**抄** |

### 2. 安装 / 更新 / 卸载 · 索引 · 校验

| | 它们怎么做（附来源） | 我们现状（实测） | 差距 · 值不值得抄 |
| --- | --- | --- | --- |
| Koishi | 源 = npm registry，可换（`registry.endpoint`，国内常指 npmmirror）；索引默认 `https://registry.koishi.chat/index.json`（`search.endpoint`，[market 插件文档](https://koishi.chat/zh-CN/plugins/console/market.html)）。实测每条形如 `{package:{name,version,maintainers,links,dist-tags,date,publisher}, shortname, downloads, verified, manifest{public,description,locales,service}, category, createdAt, updatedAt, rating, score, installSize, publishSize}`；**无签名**；两个布尔标记：`verified`（实测真 85/4731）、`insecure`（实测真 113/4731）。文档明确"不对非官方插件的安全性做任何保证"、安装"不安全"插件不受官方支持（[market 使用](https://koishi.chat/zh-CN/manual/usage/market.html)）。装卸都在面板「插件市场 / 依赖管理」里做（同页） | **没有安装这回事**。插件住在 `plugins/` 包里，`discover()` 扫 `plugins.__path__`；`inventory()` 里 `builtin` 恒 True、`removable` 恒 False（`plugins/__init__.py:1100-1105`）。`data/plugins/` 只在 [STAGE4 §8](../STAGE4_AS_PLUGINS.md) 里规划过，没做 | 缺索引、安装、更新、卸载。抄"**安装后身份比对**"和"**保留插件不给卸**"；不抄"任意源" |
| NoneBot | 源 = PyPI：`nb plugin install <name>` 按索引的 `project_link` 装，并写进项目的 `[tool.nonebot] plugins`；`nb plugin list/search/update/uninstall`（[商店](https://nonebot.dev/docs/2.4.3/tutorial/store)）。索引 `https://registry.nonebot.dev/plugins.json`（942 条）。审核：提交 Issue → **NoneFlow Bot 自动加载测试**（实测 `valid=false` 328 条、`skip_test=true` 1 条）→ 维护者与志愿者看代码 → 合进商店（[publishing](https://nonebot.dev/docs/2.4.3/developer/plugin-publishing)）；**无签名** | 同上 | 抄"**上架前先自动加载一次**"（我们已有 `check_module_removal.py` 的机器门槛，再补一条"装进来的插件能不能装起来"）；审核队列不抄 |
| AstrBot | 三种源：仓库 URL（`install_plugin(repo_url, proxy, ignore_version_check, download_url)`）、**zip 上传**（`install_plugin_from_file`）、市场记录（`repo` 或 `download_url`）（[star_manager.py](https://github.com/AstrBotDevs/AstrBot/blob/master/astrbot/core/star/star_manager.py)）。索引 = 市场 JSON：根 `$meta.{schema_version,name,version,homepage,repository}`，每条必填 `author/name/version/repo/desc`，可选 `download_url/display_name/tags/category/astrbot_version/stars/download_count`；`repo` 只许 GitHub HTTPS（[规范](https://docs.astrbot.app/dev/plugin-market/2026-06-27.html)）。校验 = 装完读 `metadata.yaml`，`author/name/version` 与记录**必须一致**，不一致安装失败；发布走 AstrBot Cloud，zip ≤ 16MB（[publish](https://docs.astrbot.app/dev/star/plugin-publish.html)）；**无签名** | 同上 | 抄"**只信任固定来源 + 装完比对身份 + 卸载保留插件被拒**"（`uninstall_plugin` 里 `plugin.reserved` 直接抛，同源实测）；zip 上传与任意 repo 不抄 |

### 3. 依赖与版本约束

| | 它们怎么做（附来源） | 我们现状（实测） | 差距 · 值不值得抄 |
| --- | --- | --- | --- |
| Koishi | **服务级依赖**：`export const inject = ['database']` 或 `{required, optional}`；依赖服务为假值时插件体**不执行**，服务变动时**回滚再重装**（[service](https://koishi.chat/zh-CN/guide/plugin/service.html)）。冲突模型是"服务一对多、包一对一"：`inject` 管服务，`peerDependencies` 管 npm 包；宿主版本卡在 `peerDependencies.koishi`（发市场必填） | `REQUIRES = ("roles",)`：`discover()` 先递归装前置，前置装不上就**跳过自己**并记 warning，环依赖报错跳过（`plugins/__init__.py:951-1011`）。依赖是**目录名**，没有版本区间；宿主没有版本号可比 | 我们的方向对（依赖用声明，不因此进核心）。抄 Koishi 的"必需/可选"两档吗？**先不抄**——我们没有可选依赖的真实需求；宿主版本卡口要抄，前提是先给宿主定一个 API 版本常量 |
| NoneBot | `require("nonebot_plugin_apscheduler")` 声明插件依赖，加载时优先装依赖；拿不到就 `RuntimeError`（[requiring](https://nonebot.dev/docs/2.4.3/advanced/requiring)）。宿主版本写在打包依赖 `nonebot2>=2.x` 里，发布规范要求必须含 `nonebot2`、**禁止 `==` 锁死**；没有独立"API 兼容版本"字段 | 同上 | 抄"依赖声明 + 启动时先装依赖"这条我们已有；"禁止锁死版本"适合写进我们的插件规范 |
| AstrBot | `astrbot_version: ">=4.16,<5"`，PEP 440（代码里 `SpecifierSet` 校验，`star_manager.py:674`）；不满足 → **阻止加载**并提示，面板可"无视警告继续安装"（`ignore_version_check`，同文件 `install_plugin`）。第三方库依赖 = 插件目录 `requirements.txt`，安装/加载时**自动 pip install**（[插件指南](https://docs.astrbot.app/dev/star/plugin.html)） | 同上 | 抄宿主版本范围（PEP 440 现成可用，不用自己发明）；**自动 pip install 不抄**（往我们环境装东西 + 供应链） |

### 4. 多实例（同一插件跑多份，各自配置）

| | 它们怎么做（附来源） | 我们现状（实测） | 差距 · 值不值得抄 |
| --- | --- | --- | --- |
| Koishi | 配置键后缀别名：`dialogue:xxx` 表示同一个插件的另一份配置（[配置文件](https://koishi.chat/zh-CN/guide/develop/config.html)）；能不能**同时跑**由插件自己声明：`export const reusable = true`（类形式 `static reusable = true`）。不可重用的插件只能有一份在跑，其余只能备着；作者也能用 `fork` 事件让同一插件共享状态、多份实例（[生命周期](https://koishi.chat/zh-CN/guide/plugin/lifecycle.html)）。另一种"多实例"= 起多个 Koishi 应用（[多份配置](https://koishi.chat/zh-CN/manual/recipe/multiple.html)） | **单例**。`plugins/_REGISTRY` 与 `_LAST_LOADED` 是进程级一份；`registry.command/background` 登记项不带实例键；`ENABLED` 是模块级常量，运行期改不了 | **暂不抄**。多实例要先在注册表里加"实例 id"，是接口变更；我们还没有"同一能力两份配置"的真实需求（AGENTS §一.2：不为想象中的需求建接口）。真要做时，抄 Koishi 的**声明式 reusable**（插件自己说能不能多份）比自己猜好 |
| NoneBot | 未查到多实例机制：一个插件模块就是一份，状态挂在模块/`Plugin` 上。要分场景只能靠插件自己写配置（**推断**，非实测） | 同上 | 同上 |
| AstrBot | 未查到同插件多实例；但有**会话级启停**：`SessionPluginManager.is_plugin_enabled_for_session(session_id, plugin_name)` 读该会话的 `enabled_plugins`/`disabled_plugins`，都没配就默认启用（[session_plugin_manager.py](https://github.com/AstrBotDevs/AstrBot/blob/master/astrbot/core/star/session_plugin_manager.py)） | 同上 | "按会话开关"比"多实例"轻，但仍要动接口；`ENABLED` 先保持模块级 |

### 5. 隔离与安全

| | 它们怎么做（附来源） | 我们现状（实测） | 差距 · 值不值得抄 |
| --- | --- | --- | --- |
| Koishi | 同进程、**无沙箱**：文档只说"不保证安全"+「不安全」标识（见第 2 行来源）。插件拿到整个 `ctx`：能加中间件、命令、事件监听、替换服务 | 同进程，但契约上**插件拿不到 `engine`、拿不到 `transport`、拿不到权限名单**；只能调逐个函数列明的窄接缝（`ChatSeams`/`ReportSeams`/`UiSeams`/`LearnedSeams`/`LinkSeams`）。`plugins/__init__.py:46-50` 自己写明"进程内隔离不是安全边界，只是约定；真正的边界是核心三件事：身份由核心盖章、渠道注入的话拒特权命令、动作执行与权限判定在核心" | 这条**我们比它们严**。不抄"把宿主对象交出去"；只抄"把不可信/不安全**显式标记在清单里**"（Koishi 的 `insecure`）供面板展示 |
| NoneBot | 同进程；靠 **Import Hook** 跟踪插件（插件不能在 NoneBot 跟踪前被 import，[requiring](https://nonebot.dev/docs/2.4.3/advanced/requiring)）。插件拿到整个 `nonebot`：适配器、驱动器、事件响应器都能改 | 同上 | 同上；Import Hook 那条与我们的 `check_module_removal.py` 用 meta-path 拦截是同一类技术，可互相借鉴 |
| AstrBot | 同进程；插件 `__init__(context: Context)` 拿到 `Context`（LLM、平台、会话、DB 都在里面）；装插件会**执行 `requirements.txt` 的 pip 安装**（第 3 行来源）。文档里的"Agent 沙箱环境"是给电脑能力用的，**不是插件隔离**（推断） | 同上 | 同上 |

### 6. 开发体验

| | 它们怎么做（附来源） | 我们现状（实测） | 差距 · 值不值得抄 |
| --- | --- | --- | --- |
| Koishi | 模板项目 + 工作区开发；**子插件独立热重载**（"不影响一个模块运行的情况下修改另一个模块的代码"，[认识插件](https://koishi.chat/zh-CN/guide/plugin/)）；Schema 驱动配置表单；i18n locales；`ctx.console.addEntry()` 让插件自己往面板加页 | 没有脚手架、没有热重载、插件不能自己加面板页。有 `inventory()` 给面板列插件（含"导入失败"两行）、有 4 个插件测试文件（`tests/test_command_plugins.py`、`test_plugin_inventory.py`、`test_plugin_prompts.py`、`test_plugins_do_not_touch_dialogue.py`） | 抄脚手架 + "上架前自动加载检查"；**热重载不抄**（见下节"不要抄"）；i18n 我们没有多语言需求 |
| NoneBot | 三个社区模板（PDM/uv/Poetry，PEP 621 自动化发布）；`nb plugin list/search/install/update/uninstall`；NoneFlow 自动加载测试；`PluginMetadata.usage` 直接当机器人帮助用 | 同上 | 抄"新插件 = `cp -r` 一个模板目录"，我们把 `plugins/mail/` 当模板即可，不用造工具 |
| AstrBot | helloworld 模板；WebUI 插件页有**重载按钮**；`watchfiles` 自动热重载（`_watch_plugins_changes` / `_handle_file_changes`，`star_manager.py:225-278`）；每个插件独立 logger 等级；插件自带 Pages / i18n / skills | 同上 | 抄"插件页要有重载/失败的可见入口"（我们 `inventory()` 已有数据，缺的是面板页）；自动热重载不抄 |

### 7. 失败与降级

| | 它们怎么做（附来源） | 我们现状（实测） | 差距 · 值不值得抄 |
| --- | --- | --- | --- |
| Koishi | 停用 = `fork.dispose()`；服务移除时依赖它的插件**回滚**，服务回来再重装；副作用要作者自己 `ctx.on('dispose')` 清（[生命周期](https://koishi.chat/zh-CN/guide/plugin/lifecycle.html)） | `discover()` 对每个插件 `try/except`：导入失败 `logger.exception` 并计入 `failed`，**不带走别的**；`register()` 抛错同样只废自己；缺依赖跳过并记 warning（`plugins/__init__.py:966-1011`） | "坏插件不带走宿主"我们已有。"运行中停用并回收副作用"我们没有（没有 dispose 语义，只有启动时装配）；值不值得抄：**等真实需求**，它必然要动接口 |
| NoneBot | 装不上**只记日志、跳过它**，其余插件继续（`PluginManager.load_plugin` 的 `except Exception` + `logger.error`，[manager.py](https://github.com/nonebot/nonebot2/blob/master/nonebot/plugin/manager.py) 实测 L196-199）；`require` 拿不到依赖直接 `RuntimeError` | 同上 | 已对齐 |
| AstrBot | 失败插件进"加载失败"记录并在面板列出来（`_build_failed_plugin_record` / `reload_failed_plugin`，`star_manager.py:821/990`），可单独重载；装失败清理 staging 目录（`_cleanup_failed_plugin_install:1484`）；卸载先 `_terminate_plugin()`，不干净只告警（`:1704-1712`） | `inventory()` 已把"导入失败"的插件列出来（两种"没有 plugin.py"分开处理，`:1075-1094`） | 抄"失败必须看得见"（已有）+"失败安装不留垃圾"（我们做安装时要写进判据） |

## 最小可复刻方案（v1）

**目标**：能在面板上从**自家分支**装/卸插件到 `data/plugins/`，src 一根不动；插件的接口一个字不改。

1. **加载根（改 `src/qq_roleplay_bot/plugins/__init__.py`）**：加一个 `data_plugins_root()`，把 `<data>/plugins`
   追加进 `__path__`（目录不存在就是空操作）。`discover()`/`inventory()` **不用改扫描逻辑**——
   它们已经扫整个 `__path__`（`pkgutil.iter_modules(__path__)`，`_plugin_file()` 的 docstring 明说
   测试就往 `__path__` 里插临时目录，`:1030-1040`）。`inventory()` 里 `builtin` 改成"在 src 根下"、
   `removable` 改成"在 data 根下"（现在是恒 False，`:1100-1105`）。
2. **装配点（改 `src/qq_roleplay_bot/runtime.py`）**：在 `PluginRegistry(...)` 造出来**之前**
   （现约 L674-687）调一次根装配，之后照旧 `attach_plugins(registry, engine.commands)`。
   `build_engine` 仍是唯装配入口，别处不许自己装。
3. **清单字段（v1 只加 3 个，都是"装进来的插件"必需，不是预造插座）**：
   `VERSION`（semver）、`HOST_API`（对 `plugins.API_VERSION`，新增 1 个常量）、
   `SOURCE`（`builtin` 或 `installed:<branch>@<sha>`，由加载根算出来，插件不用自己写）。
4. **安装来源只一条**：我们自己的仓库、`plugins` 分支、`src/qq_roleplay_bot/plugins/<name>/`
   （[STAGE4 §8](../STAGE4_AS_PLUGINS.md) 的口径）。用 GitHub contents API（`?ref=<branch>`）逐文件取，
   先落临时目录。**不做任意 URL、不做 npm/PyPI、不做 zip 上传**。
5. **校验（顺序执行，任一失败删临时目录）**：`plugin.py` 在 + 目录名 = 插件名 + `HOST_API` 匹配
   + `VERSION` 是 semver + `REQUIRES` 都在 + **在子进程里 import 一次、用假 registry 调一遍 `register()`**
   （面板进程内**不许 exec 新代码**；装完比对身份这条抄自 AstrBot）。
6. **卸载**：只删 `data/plugins/<name>`；src 根下（预装）一律拒卸，`webui` 永远拒卸
   （沿用现有 `removable=False` 语义 + AstrBot 的 `reserved` 同源做法）。
7. **面板（`plugins/webui/`，在插件分支那份）**：新增一页「插件」，只调新接缝
   `UiSeams.plugins`（list / install / remove / enable / disable 五个函数，逐个列明）。
   权限判定、下载、校验、落盘全在核心；面板读 `inventory()`、写 `data/plugins_state.json`
   （启用/停用只能写 `data/`，不许动 `src/`——AGENTS §2.3）。
8. **门槛/测试（钉死，缺一不可）**：
   - `tests/check_module_removal.py` 退出码 0，且 `CORE_TO_PLUGIN_COUPLINGS`（`:299`）**不许新增条目**；
   - 新 `tests/test_plugin_install_root.py`：临时目录当第二根，钉 4 件事——被发现、`REQUIRES` 生效、
     坏插件仍然列进 `inventory()`、卸载只删 data 根；
   - `tests/test_plugins_do_not_touch_dialogue.py` 继续绿；
   - "**删掉插件核心照跑**"每一步都要能过（不是只在第一步过）。

## 明确不要抄的

| 不抄 | 为什么 |
| --- | --- |
| 中间件 / 事件钩子 / `inject` 服务（Koishi）、`@Tool` 给 LLM、prompt 注入（NoneBot/AstrBot） | 插件一旦能改"她说什么、要不要接话"，就是 Stage 3 的地盘；AGENTS §2.3 明令禁止（`PromptPlugin.build_prompt`、`build_judge_hint`、`registry.chat` 都在禁列） |
| 任意 URL / 任意 git / 任意 npm 包安装 | 等于给面板开一个任意代码执行的口子。它们敢这么做是因为信任模型是"用户自己负责"（Koishi 文档原文就是"不保证安全"），我们不行 |
| 自动 `pip install requirements.txt`（AstrBot） | 往我们环境装东西 + 供应链入口；我们的插件只允许用核心已有的能力 |
| 运行时热重载（AstrBot 的 watchfiles、Koishi 子插件热重载） | 运行中的 bot 换代码 = 不可审计、也不可复现；我们的口径是"重启装配"，`verify_runs.log` 那套留痕才有意义 |
| 把宿主对象交给插件（`ctx` / `Context` / `engine`） | 我们**唯一**不这么干，这是我们的安全边界本身 |
| "社区提交 + 审核队列 + 评分/下载量" | 我们没有审核人力，也没有这个需求；v1 只装自家分支（Koishi 的 `rating`/`downloads` 是市场产物，不是机制） |

## 分几步做、每步判据

| 步 | 做什么 | 判据 |
| --- | --- | --- |
| 1 | 加载根 + `inventory()` 的 source/removable（方案 1-3） | `check_module_removal.py` 0；旧测试全绿；新测试钉住第二根；**把 `data/plugins/` 整个删掉，一切照旧** |
| 2 | 安装/卸载数据面（方案 4-6，不含面板） | 新测试在临时 data 根上跑通：装成功、坏包拒装且不留目录、卸预装被拒；写盘只在 `data/` 下 |
| 3 | 面板一页 + 接缝（方案 7，在插件分支） | 面板里没有下载/校验逻辑；没有 token 时 fail-closed；`plugins/webui` 仍不给卸 |
| 每步都要 | 跑完就留痕 | `tests/run_offline.py` 全绿 + `pyflakes src tests` 干净 + `tests/check_module_removal.py` 0（数用 `data/logs/verify_runs.log` 的 `ran=` 核） |

## 我没查到 / 不确证的

- Koishi 是否支持"从 GitHub 装非 npm 插件"：只确认了 npm registry 与可换的 `search.endpoint`/`registry.endpoint`。
- Koishi 的 `verified` 是谁、按什么标准给的（注册表里只是一个布尔）。
- NoneBot / AstrBot 的"同插件多实例"：文档与源码都没找到，上面写的是**未查到**，不是"不存在"；AstrBot 只查到会话级启停。
- AstrBot 有没有插件签名/校验和：市场规范只要求 `metadata.yaml` 的 `author/name/version` 三项身份比对，没看到签名。
- NoneBot 索引里 `valid=false`（实测 328/942）的确切含义：**推断**是 NoneFlow 自动加载测试没过，没有逐条核。
- 我们这侧（2026-10-08 **已实测**，结论与预期相反）：`pkgutil.iter_modules` **看不见**没有
  `__init__.py` 的目录（第二个根数出 0 个插件），而 `_plugin_file()` 6/6 命中、`importlib.import_module` 成立。
  发现器因此换成 `plugin_dirs()`（自己列目录，只看 `<根>/<名字>/plugin.py`），两个根同一个口径。
