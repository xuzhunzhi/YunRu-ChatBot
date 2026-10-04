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
