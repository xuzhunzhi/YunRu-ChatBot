# Stage 4 作为插件接入 Stage 3 重构版本

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
   今晚那批（`alias_also` / `ALIAS_ALSO_LIMIT` / `card_from_sender` /
   `drop_summary_if_stale`）在新基底的 `src/` 里**存在且有测试**。
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
```

### 7.3 还没做的（下一步的准确清单）

1. **三份接缝没移植**（`ChatSeams` / `ReportSeams` / `UiSeams` + `runtime._SeamBinder`，
   插件线上是 **+515 行**，最敏感）。当前后果：
   * 日报**不启用**（"写信 agent 没有模型通道（接缝没给 letter_client）"）；
   * 面板缺 `execute_action` 等入口；
   * `serve` 里没补 `registry.set_loop(loop)`，面板跨线程投协程会用 fallback。
2. **群管理/群主动作的执行端**仍是本体 `stage3_main.py:1539-1547` 的 fail-closed 占位：
   那几条命令**认得出、执行不了**，回"这条分支没有群管理能力。"——
   要让它们真能用，得把动作执行那条接缝也接上。
3. **两份角色缓存**：`engine.self_roles` 还是核心的（`runtime.py:404-410`），
   roles 插件另造一份给 `registry.shared_roles()`（因为 `chat.roles_sink` 留空）。
   Stage 3 行为因此不变，但接缝移植时要把单源建立起来。
4. **插件测试**缺 5 个文件（`test_stage4_plugins` / `test_background_plugins` /
   `test_plugin_prompts` / `test_plugin_inventory` / `plugin_support`），要搬过来。
5. `_set_public_help` 没移植：`PUBLIC_HELP`（`stage3_main.py:508`）仍是静态的；
   但 `/help` 卡片走注册表 `help_lines()`，**已包含**插件帮助行。

---

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

