# Stage 4 作为插件接入 Stage 3 重构版本

> ⚠️ **2026-10-22 文档盘点**：本文是 2026-10-16 的快照。**机制部分（插件协议 / 装配 /
> 发现 / 范围门槛）仍然有效；清单类内容已过期**：文中示例只列 5 个插件
> （`group_admin` / `join_approval` / `mail` / `roles` / `webui`），其中 `roles` **已不存在**；
> 2026-10-22 生产实测是 **9 个**（另含 `vision` / `remind` / `rss` / `outage_notice` /
> `timeout_alert`）。当前清单以 `README.md` 插件表与 `AGENTS.md` §7.1 为准。

> 用户 2026-10-04 原话（这是本文件存在的理由，也是判据）：
>
> *"我要的是，stage4作为插件接入stage3的重构版本，我不管你怎么操作，反正是插件，
> 插件是不需要本体做出改动的，本体在不动接口的情况下改动也不会影响插件，望周知"*

## 一、方向（我一开始搞反了）

**错的读法**（我原先的）：把 `stage4-plugins` 这条线当成"要部署的版本"，
于是去数它落后 stage3 多少提交 → 结论是"接入会回退 23 个提交"→ 卡住。

**对的读法**：

```
本体（host） = Stage 3 重构版 = stage3-core-fixes / main
              ——**带全部最新修复**（含 2026-10-03 的别称 / 群名片 / roster 那批）
     ↑ 插上去，**插件原样，不动插件**
插件（plugin）= group_admin / join_approval / mail / roles / webui
              ——都在 stage4-plugins 上
```

所以要做的是**把插件（和插件接口）搬到本体上来**，而不是把本体的旧版本搬去插件那边。

## 二、现状盘点（2026-10-04 实测，不是推断）

| | 本体 `stage3-core-fixes`（`3e0d863`） | 插件线 `stage4-plugins`（`3992819`） |
| --- | --- | --- |
| `command_plugins.py`（命令插件接口） | **有** | 有 |
| `background_plugins.py`（后台插件接口） | **有** | 有 |
| `plugins/`（发现 + 注册表 + 接缝） | **无** | **有** |
| 5 个插件目录 | 无 | **有** |
| 今晚的 Stage 3 修复 | **有** | **无** |

也就是说：**接口的一半本来就在本体上**（命令/后台那两条），缺的是
`plugins/` 这一层——"扫目录 + 注册表 + 三个接缝对象（`chat` / `report` / `ui`）
+ `REQUIRES` 依赖顺序 + `ENABLED` 开关 + `inventory()` 清单"。

## 三、要搬什么（分三类，别混）

### 3.0 实测缺口（2026-10-04，在分支 `stage3-plugin-host` 上真跑出来的）

把插件包**原样**搬到本体上之后，逐个 import 插件的 `plugin.py`：

```
group_admin    OK        ← 已经能直接插上
roles          OK        ← 同上（它还是别人的前置）
webui          OK        ← 同上（面板）
join_approval  ImportError: 本体缺 JoinApprovalPlugin
mail           ImportError: 本体缺 DailyReportPlugin
```

一查就明白：**`MailChannelPlugin` / `DailyReportPlugin` / `JoinApprovalPlugin`
三个类的实现至今还躺在核心文件 `background_plugins.py` 里**；插件只是
`from ...background_plugins import JoinApprovalPlugin` 把它们捞出来用。

> **这就是"插件需要本体为它改动"的典型**：插件的实现还住在本体里，于是"插上插件"
> 必须先把本体那个文件换成插件线的版本。按用户 2026-10-04 的规矩，正确修法是
> **把实现搬进插件目录**让插件自包含；本体只留框架
> （`BackgroundPlugin` 协议 / `plugin_enabled` / `build_background_plugins` / 节拍）。

本体侧的装配签名也不同（接口要对上的另一处）：

```python
# 本体（stage3 线）：
def build_background_plugins(engine, *, call_action=None, notify=None, roles=None, loop=None)
# 插件线（stage4）：
def build_background_plugins(engine)      # 只从 registry.backgrounds 里拿已装好的那半
```

### 3.1 原样搬：`src/qq_roleplay_bot/plugins/` 整个包
   （`__init__.py` 的发现/注册表/接缝定义 + `group_admin/` `join_approval/`
   `mail/` `roles/` `webui/`）。
   **验收：`git diff stage4-plugins -- src/qq_roleplay_bot/plugins/` 必须是空的**
   ——插件一个字节都不改，否则就违反了"插件不需要本体改动"。

2. **本体的**接口层**（要逐块移植，只加接口、不改 Stage 3 行为）**：
   * `runtime.py`：`_SeamBinder`、`ChatSeams` / `ReportSeams` / `UiSeams` 的构造、
     `attach_plugins(registry, engine.commands)` 的调用点、
     `build_background_plugins(engine)` 改走 `registry.backgrounds`。
   * `stage3_main.py`：装配点（把 `registry` 造出来、把 `commands` 交出去）、
     面板的 `install_async_runner` / 节拍看护。
   * 这些是"接口"，属于本体该长的东西；**但每一块都要能证明它没改 Stage 3 的行为**
     （见第四节判据 1）。

3. **插件的测试也一起搬**：`tests/test_stage4_plugins.py`、`test_background_plugins.py`、
   `test_command_plugins.py`、`test_plugin_prompts.py`、`plugin_support.py`、
   `test_plugin_inventory.py`。插件的测试是插件的一部分。

## 四、验收判据（硬，缺一不可）

1. **Stage 3 行为一行都不回退**：`stage3-core-fixes` 的 23 个提交全部在祖先里；
   今晚那批（`alias_also` / `ALIAS_ALSO_LIMIT` / `card_from_sender`）在新基底的
   `src/` 里**存在且有测试**。
   > **2026-10-15 更正——这条判据里原来还有第四个名字 `drop_summary_if_stale`，
   > 方向已经反了**：它当时要求"在 `src` 里**存在且有测试**"，而现在
   > `ConversationState.drop_summary_if_stale()` **已经被删掉**（用户 2026-10-15 原话
   > *"我还要说几次，不切话题"*：判定不再回报话题起点，那条"起点跑到 `summary_through`
   > 之后就把摘要丢掉"的判据无从计算，函数与调用点一起删）。
   > 所以现在的判据是**反向**的：它**必须不在** `src/` 里——
   > `tests/test_topic_window.py` 的
   > `test_the_removed_topic_anchor_api_stays_removed` 逐个钉着
   > `topic_history` / `advance_topic_start` / `drop_summary_if_stale` 三个名字
   > **不许回来**（回来就等于历史又多了一个裁切入口）。
   > 前三个名字照旧"存在且有测试"；`drop_summary_if_stale` 这一项按上面的反向判据读。
2. **插件零改动**：`git diff stage4-plugins -- src/qq_roleplay_bot/plugins/` 为空。
3. **模块摘除判据**：`tests/check_module_removal.py` 退出码 0
   （"删掉插件，核心照样 `build_engine`"）——这条是"插件没拴住本体"的机器证明。
4. **全套件绿 + pyflakes 干净**（报数用 `data/logs/verify_runs.log` 的 `ran=`）。
5. **接口稳定性**：记下这次移植动了 `UiSeams` / `PluginRegistry` 的哪些字段；
   以后动接口要当破坏性变更处理（同步改插件 + 跑插件测试）。

## 五、步骤

1. 从 `stage3-core-fixes` 开新分支（**不叫 stage4**，它是"本体 + 插件接口"）：
   `git switch -c stage3-plugin-host stage3-core-fixes`
2. `git checkout stage4-plugins -- src/qq_roleplay_bot/plugins/`（第 3.1 类，原样）
3. 逐块移植第 3.2 类的接口层（`runtime.py` / `stage3_main.py`），每块跑一次套件
4. 搬测试（3.3），跑判据 1–4
5. 全绿后再谈"接到 `run`"——**停/重启运行中的 Bot 之前先问用户**

## 六、接 `run` 的注意（等第 4 节全过再说）

* `run/` 跑的是 Stage 3，`.env` 与 `data/`（含记忆库、720MB）都是真的；
  `data/`、`backups/`、`docs/yunru-source/`、`.env` **不在 git 里**，删了不可逆。
* `deploy_to_run.py`（本体上就有）负责"只增不减"的拷贝与保护检查。
* **停/重启 `run` 的进程必须先问用户**（AGENTS §五）。

---

## 七、装载点：实况与更正（2026-10-04）

### 7.1 更正：我先前这一节写错了

我原来在这里写"**本体的命令插件层是半成品**：`CommandRegistry` / `default_command_plugins()`
没有任何地方调用，`DialogueEngine.commands` **不存在**"——**这是错的**，被实测证伪：

```
stage3_main.py:508   PUBLIC_HELP = build_help_text(build_command_registry().help_lines())
stage3_main.py:846   self.commands = command_registry if command_registry is not None \
                                      else build_command_registry()
```

本体**本来就有**命令分发路径（`handle()` 在 `stage3_main.py:2678` 匹配 → `:2683` 判档位
→ `:2703` 执行 → `:2706/:2711` 分派），`DialogueEngine.commands` 一直在。
**我错在 grep 的名字**：我搜 `CommandRegistry(` 与 `default_command_plugins`，
漏了真正的调用者 `build_command_registry()`——于是把"没搜到"当成了"不存在"。
（`default_command_plugins()` 确实是死代码，但那不代表命令层是半成品。）

### 7.2 真正缺的是什么（`d768c95` 已补）

| 缺的东西 | 后果 | 现在 |
| --- | --- | --- |
| `CommandRegistry.add()` | `PluginRegistry.command()` 调它，缺了 `attach_plugins` 直接抛 | 已加 |
| `build_engine` 里的装配 | 插件从来没人 `discover()` | 已加 |
| `engine.plugin_registry` | `build_background_plugins(engine)` 永远返回空 | 已挂 |

装配后的实测（探针跑出来的）：

```
registry.loaded   = ('roles', 'group_admin', 'join_approval', 'mail', 'webui')   ← 5 个全装上
engine.commands   = ['ping', 'help', 'balance', 'group_manage', 'group_owner', 'title']
registry.backgrounds = ['join-approval', 'mail-channel']
tests/run_offline.py       817 全绿、0 跳过（与改动前基线相同）
tests/check_module_removal.py  7 个可插能力全"能拔掉而核心照跑"
                              （⚠️ 那是**当时**的快照：2026-10-06 `qq_roles` 进核心后
                               `DEFAULT_MODULES` 是**六项**，那条线的当前口径见
                               `AGENTS.md` §三那段"为什么是六项、不是七项"）
```

### 7.3 更正：这一节原来列的"没做"**大部分已经做完了**（2026-10-05）

原文在这里列了 5 条"还没做"，其中 1/2/3/5 现在**都不成立了**（文档不能撒谎，逐条更正）：

| 原来写的 | 实际（实测） |
| --- | --- |
| ① 三份接缝没移植 | **已移植**（_SeamBinder / ChatSeams / ReportSeams / UiSeams / set_loop），日报、面板、prompt 汇聚口都启用 |
| ② 群管理/群主动作"认得出、执行不了" | **执行端是通的**：真装配路径下 12 条写动作逐条真的发出去了（payload 见 	ests/test_group_action_execution.py）。那句 fail-closed 占位（stage3_main.py:1600-1603，文案已是"这条部署没有群管理能力。"）**只剩"装配错了"才会出现** |
| ③ 两份角色缓存 | **已单源**：ngine.self_roles is registry.shared_roles() 恒真 |
| ⑤ _set_public_help 没移植 | **已移植** |

**仍然成立的一条**：④ 插件测试缺的那几个文件——已随接缝移植一起搬入。

**新记的两件事（2026-10-05）**：

* ⚠️ **备胎会掩盖断线**：把 uild_engine 里接 
egistry.action_caller 那行去掉，
  **只有新加的那 2 条测试会红**，既有的 	est_stage4_plugins.py 17 条全绿——因为那条退路
  会静默兜住"装配漏接"。**动装配点时别只跑既有测试。**
* **两条待用户拍**（实测发现，未改）：/super ban @某人 99999 里的 5 位数字**先被当成 QQ 号**，
  分钟数落回默认 600（夹不到 30 天）；xecute_action **不过档位闸门**（面板路径传普通
  ctor_id 照样真发 set_group_ban）——面板鉴权在 webui_access.py，这可能是**有意**的，
  但与 docstring 的说法不一致，要么加一层调用方身份，要么把 docstring 写实。

## 八、以后要做：WebUI 内置「官方插件市场」（2026-10-05 用户提出，**先记着，不做**）

> 用户原话：*"后面看看能不能在webui内置官方插件市场（也就是我做的插件），
> 从对应分支的对应文件夹拉取安装这样"*（同轮：*"再说，记到文档里去，这个是后面要做的事情"*）

**想法**：面板里有一页"插件市场"，列出官方的（也就是用户自己做的）插件，
点一下就从**对应分支的对应文件夹**拉下来装上——不用手动往仓库里拷目录。

**必须先解决的一个冲突**（这是它现在还不能做的原因）：

| | |
| --- | --- |
| 硬规矩 | 面板**只写 data/**，**不许动 src/**（AGENTS §2.3） |
| 现状 | 插件住在 src/qq_roleplay_bot/plugins/ 里 |
| 冲突 | "面板自己安装插件" = **往 src/ 写文件**，直接违规；而且会和 git 打架 |

**两条路，推荐方案一：**

1. **（推荐）插件支持从 data/plugins/ 加载**：发现时扫**两个根**——
   src/qq_roleplay_bot/plugins/（预装、随代码走）+ data/plugins/（装进来的）。
   于是：安装 = 往 data/ 写 ✓、卸载 = 删目录 ✓、src/ 始终干净 ✓、
   而且"预装 vs 装的"天然分得清（正好接上"预装的不给卸"那条）。
2. 面板调外部脚本去写 src/ —— **破规矩**，不建议。

**拉取来源**（等做的时候确认）：stage4-plugins 分支的
src/qq_roleplay_bot/plugins/<名字>/ 整个文件夹（GitHub raw / API 都行）。
装之前要校验：plugin.py 存在且能 import、REQUIRES 能满足、ENABLED 语义、以及**来源可信**
（"官方"= 只从我们自己的分支拉，不做任意 URL，否则就是给面板开了个任意代码执行的口子）。

**做之前的前置**：先把分支理顺（见下），否则"对应分支"这句话没有可靠落点。

### 2026-10-16 补：插件 = 各自的仓库，市场 = 一份索引（**只记需求，不做**）

> 用户原话（逐字）：
> *"正常的插件是单独的仓库，一个插件一个仓库，插件市场负责收录这些仓库"*
> *"我搞这个分支本意是分支里面只放插件当插件市场的"*
> *"不急，这个记到需求里"*

**上面 2026-10-05 那一段原文一字不动**：它说的"面板里一页插件市场、从对应分支的对应文件夹
拉取安装"照旧成立。这条补的是**市场收录什么**与**插件住在哪**这两件事。

#### 三层模型（插件 / 市场 / 宿主各是什么）

| 层 | 是什么 | 不做什么 |
| --- | --- | --- |
| **插件** | **各自一个仓库**（一个插件一个仓库）。代码、版本、Issue 都跟着它自己那个仓库走 | 不再"所有插件挤在宿主仓库的 `plugins/` 一个目录里" |
| **市场** | **一份索引**：只收录**仓库地址 + 元数据**（名字 / 版本 / 说明 / 作者 / 仓库 URL），**不放插件代码** | 不托管代码、不做打包分发 |
| **宿主** | 从索引里**装**：按仓库地址取下来 → 落 `data/plugins/<名字>/` → **装完比对身份** | 不信任索引里的声明：装上来的东西与索引对不上就**安装失败** |

别人的做法（我们自己那份调研，2026-10-08，逐条带来源）：
`docs/research/plugin-ecosystem-2026-10-08.md`

- **AstrBot**：市场就是一份 JSON 索引（根是 `$meta.{schema_version,name,version,homepage,repository}`），
  每条**必填 `author/name/version/repo/desc`**，`repo` **只许 GitHub HTTPS**；
  校验 = **装完读插件自己的 `metadata.yaml`**，`author/name/version` 与索引记录**必须一致，
  不一致安装失败**（同文件 `:39`）。"装完比对身份"这条就是抄它的。
- **Koishi**：索引是 registry 的 `index.json`（默认 `https://registry.koishi.chat/index.json`），
  源是 npm；**无签名**，只有 `verified`（实测真 85/4731）与 `insecure`（实测真 113/4731）
  两个布尔（`:37`）。"索引是一份 JSON"这条与我们同形。
- **MaiBot**：索引是 `plugins.json`（`docs/PLUGIN_MARKET_SURVEY.md:4` 实测：48 KB / 386 条，
  字段只有 `id` + `repositoryUrl`）——**最小的一份索引**，正好就是"只收录仓库地址"这个形状。

#### 我们缺什么（**以现在的代码为准**；§八 里那几格"现状"已经过期，见下）

先说**已经做掉的**（2026-10-08 当天就落地了）：

- **`data/plugins/` 第二个加载根**：`plugins/__init__.py` 的 `mount_data_plugins()`（`:1530`），
  装配点 `runtime.py:700`；发现口径见 `plugin_dirs()`（`:1472`，不用 `pkgutil`——它看不见
  没有 `__init__.py` 的目录）。测试：`tests/test_plugin_install_root.py`（提交 `e12c687` / `df9bbd7`）。
- **清单字段（三个可选声明）**：`VERSION` / `TITLE` / `HOST_API`（`plugins/__init__.py:26-34`），
  宿主接口版本 `API_VERSION = 1`（`:145`），`HOST_API` 对不上就**拒载**（`:1460`）。
- **`inventory()` 不再是恒 True / 恒 False**：`builtin` 与 `removable` 按**加载根**算
  （`:1596-1599`：`removable = source == "installed" and name != "webui"`；
  `webui` 即使从 `data/plugins/` 来也**不给卸**）。

> ⚠️ **更正**：`docs/research/plugin-ecosystem-2026-10-08.md:37` 与 `:89` 那两格写的
> "`data/plugins/` 只在 STAGE4 §8 里规划过，**没做**"、"`inventory()` 里 `builtin` 恒 True、
> `removable` 恒 False（`:1100-1105`）"**已经过期**——同一份调研的收尾提交（`e12c687`）
> 就把这两件事做了，行号也变了。那段是**当天的记录**，不改；**以代码为准**。

**真缺的**（按"装一次"要经过的顺序）：

| 缺什么 | 落在哪 | 备注 |
| --- | --- | --- |
| **一份索引** | 新文件（如 `data/plugin_index.json`，未跟踪）+ 一个只读口 | 每条 = 仓库地址 + 元数据（`name/version/desc/author/repo`）。**索引只放地址与元数据，不放代码** |
| **装 / 卸 / 更新** | `plugins/__init__.py` 旁边的新模块（取仓库 → 校验 → 落 `data/plugins/<名字>/`；卸 = 删那个目录） | 现在**一点都没有**：`:1587-1588` 写着"这一版**没有**实现'装'和'卸'本身（没有网络下载、没有删目录的代码）" |
| **装完的身份比对** | 同上，装完那一步 | 拿索引里的 `name/version`（+ `author`）与装上来那份插件的**声明**逐项比，不一致就**删掉临时目录、安装失败** |
| **面板一页 + 接缝** | 插件树那条分支（`plugins/webui/`） | 面板不许自己下载 / 校验 / 落盘，只调核心给的窄接缝。**面板装插件不许往 `src/` 写**（AGENTS §2.3） |
| **启用 / 停用的落点** | `data/plugins_state.json`（未跟踪） | 现在 `ENABLED` 是**模块级常量**（`plugins/__init__.py:24`），运行期改不了 |

#### 已定边界（**不改上面那条冲突**，只把范围划死）

- **v1 只装自家人做的插件**：索引由我们自己维护，v1 里 `author` 只有我们自己。
  **不抄**社区提交 / 审核队列 / 评分 / 下载量——那几个是"市场的产物"，不是机制
  （依据：`docs/research/plugin-ecosystem-2026-10-08.md` 的"明确不要抄的"那一节）。
- **面板装插件不许往 `src/` 写**：与 §八 那张冲突表、AGENTS §2.3 是**同一条**，一个字不改；
  落地方式就是 §八 的"推荐方案一"（装 = 往 `data/plugins/` 写）——而这一条**已经落成**。
- **不做任意 URL / 任意 git / zip 上传 / 自动 `pip install`**：来源只许我们自己那份索引里
  登记过的仓库。

#### 落地顺序（**主会话建议、待用户确认**）

1. **先做市场机制**（索引 + `data/plugins/` 的 列 / 装 / 卸 + 装完身份比对）。
   理由：这一层做完，"装一个插件"才第一次存在；而且它**不碰现有插件**——现在能跑的
   那些一个字节都不动。
   > `data/plugins/` 这个落点**已经在了**，所以第 1 步实际要补的是**索引 + 装卸代码 + 身份比对**。
2. **再一个一个把插件拆成独立仓库**（一个插件一个仓库）。
   理由：拆仓库是**搬迁**（代码换位置、发布方式换、宿主侧的引用跟着换），而**现在这台整机
   是能跑的**——搬迁会打断它。所以放在市场机制之后、一个一个来。
   与 §九"分支怎么分"并存：那条说的是**当下**插件往哪条分支放，这里说的是**目标形态**
   （每个插件自己的仓库）；两件事不冲突，但都**待用户点头**才动。

> ⚠️ 用户 2026-10-16 明确说了"**不急**"：这一段是**需求记录**，不是开工令。

## 九、待定：分支怎么分（用户 2026-10-05 定过口径，细节待确认）

> 用户原话：*"插件我说过，丢到分支上去，stage3推到main，不要给搞混了"*

口径明确：**Stage 3 进 main；插件进插件分支**。
但当前用于生产的 stage3-plugin-host **两边混在一起**（Stage 3 的不懂就问/风格审核/对话日志/装配点
+ plugins/** 整个包 + plugins/mail/wire.py 的修复），所以**不能直接推 main**。

**拟定的拆法**（等用户点头再动手）：

1. 从 stage3-core-fixes 拉干净分支，把今晚的 **Stage-3 提交**迁过去 → 跑套件全绿 → 推 main；
2. plugins/** 整个包 + 插件侧的修复 → **插件分支**（stage4-plugins）；
3. plugins/__init__.py（发现 / 注册表 / 接缝**定义**）算**本体**——它是接口，插件只是用它。

⚠️ **在做完这个拆分之前，不要再往主线加"插件侧"的改动**（包括把 ision.py 挪成
plugins/vision/ 那种），否则混得更厉害。

