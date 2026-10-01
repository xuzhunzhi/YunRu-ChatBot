# Stage 3 的模块形状（stage3-rebuild 分支）

这份是**施工图**：改完长什么样、每个文件管什么、命令怎么被认领。
`docs/STAGE3_REBUILD.md` 讲为什么与分几步，这里讲最终形状。

## 一、起点的问题（实测）

`src/qq_roleplay_bot/stage3_main.py` 一个文件 **3827 行**：

| 对象 | 规模 |
| --- | --- |
| `DialogueEngine` | **96 个方法 / 2884 行** |
| `handle()` | **358 行**（一条消息进来之后的全部判断与分发） |
| `_model_path()` | 250 行 |
| `__init__()` | 161 行 |
| `/super` 实现 | `_super_reply` 58 + `_super_status` 96 + `_super_apicheck` 66 + `_super_memory` 23 + `_profile_block` 40 + `_add_admin` 38 + `_del_admin` 47 + `_admin_list` 22 ≈ **390 行** |
| `/admin` 实现 | `_run_admin_command` 32 + `_admin_command_reply` 31 + `_admin_echo` 13 + `_denied_admin_reply` 25 ≈ **101 行** |
| 命令解析 | `SuperAction` 枚举 + `parse_super_command` 约 **100 行** |

它同时装着：对话编排、判定、压缩、注意力、记忆调用、`/super` 全部子命令、
`/admin` 全部子命令、动作执行、护栏、审计、状态快照。**96 个方法里没有一个能单独读懂。**

## 二、目标形状

```
src/qq_roleplay_bot/
├── _host/                        宿主接口（已建）
│   └── __init__.py               7 个 Protocol + HostServices + 空实现
├── _host_adapters.py             现有模块 → _host 接口的薄壳（已建）
│
├── stage3/                       **Stage 3 本体**
│   ├── __init__.py               对外只导出 engine / commands 两样
│   │
│   ├── commands.py               **命令认领与分发**（从 DialogueEngine 里搬出来）
│   │      认领顺序：核心命令 → 命令插件 → 都不是就是聊天
│   │      它只问"这条消息是谁的命令"，**不执行动作**
│   │
│   ├── super_commands.py         `/super` 全部子命令
│   │      status / apicheck / processes / lan / fan / restart
│   │      memory* / affinity* / profile
│   │      addadmin / deladmin / adminlist / permit / mail
│   │
│   ├── admin_commands.py         `/admin` 全部子命令（开关群、清会话、echo）
│   │
│   ├── engine.py                 `DialogueEngine`：**只管对话**
│   │      一条消息进来 → 判要不要回 → 怎么回 → 交给出站
│   │      /super 与 /admin 的实现**不在这里**
│   │
│   ├── judge.py / compaction.py / attention.py / focus.py / context.py   对话
│   ├── memory/                   记忆（8 个模块，见 STAGE3_REBUILD.md 2.1）
│   └── defense/                  security / prompt_guard
│
├── 底层（不属于 stage3）
│   └── transport / llm_client / dev_config / capabilities / state_store /
│       feature_log / control_audit / metrics / command_plugins /
│       background_plugins / runtime
└── plugins/                      空（stage4 在另一条分支）
```

## 三、命令怎么被认领（这一步的关键）

现在：`handle()` 里 358 行的 `if/elif`，每加一条命令就多一段。

目标：**命令自己声明"我长什么样"与"我要什么权限"，`commands.py` 只做匹配与分发。**

```python
@dataclass(frozen=True, slots=True)
class CommandClaim:
    """一条 Stage 3 / 底层的命令。"""
    name: str                      # 唯一名，用于日志与去重
    level: str                     # "public" | "admin" | "super"
    match: Callable[[IncomingMessage], bool]
    run: Callable[[IncomingMessage], Awaitable[str | None]]
```

| 链 | 谁登记 | 什么时候匹配 |
| --- | --- | --- |
| 核心命令 | `stage3/commands.py` 自己（`/ping`、`/help`） | 第一优先 |
| Stage 3 管理命令 | `super_commands.py` / `admin_commands.py` | 第二优先 |
| 插件命令（stage4） | `plugins/<功能>/plugin.py` 调 `registry.command()` | 第三优先 |
| 都不是 | —— | 交给对话 |

**权限判定仍然在核心**：`commands.py` 拿到 `claim.level` 之后自己去问
"这个人是不是超管 / 这个群的管理员"，**命令实现拿不到名单**。

## 四、`DialogueEngine` 对外只留这些（命令层要用到的）

命令实现要用引擎的状态，但**不该直接点它的 96 个方法**。设一层窄接口：

| 接口 | 方法 | 谁用 |
| --- | --- | --- |
| `ChatControl` | `enable_group` / `disable_group` / `leave_session` / `snapshot` | `/admin`、`/super status` |
| `AdminRoster` | `add_admin` / `del_admin` / `admin_list` / `group_admins` | `/super *admin*` |
| `MemoryAdmin` | `memory_report` / `memory_records` / `memory_archive` | `/super memory*` |
| `AffinityAdmin` | `affinity_of` / `reset_affinity` / `profile_block` | `/super affinity`、`/super profile` |
| `MachineOps` | `processes` / `network` / `fans` / `request_restart` | `/super processes\|lan\|fan\|restart` |
| `UsageReport` | `usage_snapshot` / `apicheck_lines` | `/super apicheck` |
| `HostServices` | （已建） | 出站、卡片、审计、余额、角色、检索 |

**实现方式**：`DialogueEngine` 实现前六个 Protocol（方法名可以现成，只是**声明出来**），
命令实现只拿得到这六张窄接口，拿不到引擎本身。

## 五、验收（每一步都要满足）

```powershell
# 1. 离线测试全绿
.\.venv\Scripts\python.exe tests\run_offline.py      # 期望 ALL_OFFLINE_TESTS_PASSED
# 2. 静态检查干净
.\.venv\Scripts\python.exe -m pyflakes src tests
# 3. 可插能力确实可插（把模块改名，核心仍能 import）
.\.venv\Scripts\python.exe data\tmp_unplug.py typing_sim help_card vision qq_roles
```

## 六、施工顺序（一次做到位，不来回改两遍）

| 步 | 做什么 | 状态 |
| --- | --- | --- |
| A | `_host` + 适配器 + 接线 | **已完成**（提交 `3612eaa` / `429c563`） |
| B | 把顶层拴缚改成函数内 import 或注入 | **已完成**（提交 `90116fa`） |
| C | 抽 `commands.py` | **实测后判定：不做，理由见第七节** |
| D | 抽 `super_commands.py` + `admin_commands.py` | **同上** |
| E | 建 `stage3/` 目录，把对话/记忆/防护搬进去 | 未做 |

## 七、C / D 为什么不做了（实测依据）

原计划把 `/super` 与 `/admin` 的实现从 `DialogueEngine` 里抽成独立模块。实测之后判定**不该抽**：

- 那一族 **24 个方法共 717 行**；
- 从命令入口做可达性分析：**79 / 96 个方法可达**——命令层与对话主路径是**同一个连通块**；
- 它们直接用到约 **40 个引擎内部状态**（`snapshot` / `memory_service` / `group_admin_ids` /
  `admin_user_ids` / `_stats` / `_denied_admin` / `persist_state` / `_flags` …）。

也就是说：**命令不是"能被抠出去的模块"，抠它等于搬走大半个类**。硬抽出来只有两种结局——
要么传整个 `Engine`（等于没解耦），要么定义 40 个字段的接口对象（等于把类换个名字）。

而且按判据它们**本来就该在这里**：`/admin`（开关群、清会话）直接管理聊天、
`/super memory*` 直接管理记忆——那是 Stage 3 的活。

**真正的坑不是"命令在引擎里"，是"96 个方法挤在一个 3800 行文件里"。**
那个问题的解法是**分文件**（E 步），不是把互相调用的方法硬拆到两个模块。

## 八、已经达到的状态（实测）

`stage3_main` 与 `runtime` 的**顶层** import 里，已经没有"按判据只是少一项能力"的模块：

```
stage3_main 顶层:  _host admin_control attention builtin_commands dev_config
                   dialogue_compaction dialogue_judge extensions feature_log focus
                   llm_client memory_model memory_service memory_view metrics
                   onebot_ws runtime_flags security snapshots stage3_runtime
                   transport trigger
runtime 顶层:      _host_adapters api_usage background_plugins capabilities
                   conversation_context dev_config extensions llm_client
                   memory_config memory_ops memory_service onebot_client
                   operator_config outbox prompt_library runtime_flags stage3_main
                   state_store style_reviewer transport
```

剩下的全是名副其实的地基，加两处**合法依赖**：`admin_control`（`/admin` 的解析器，
自身零依赖）与 `_host` / `_host_adapters`（这次建的宿主接口）。

**可插能力实测**（`data/tmp_unplug.py`：模块改名成 `.bak`，看核心还能不能 import）：

| 模块 | 结果 |
| --- | --- |
| `typing_sim` / `help_card` / `vision` / `qq_roles` | 能（出站表现、渲染、识图、角色查询） |
| `control_audit` / `provider_registry` / `runtime_diagnostics` / `control` | 能 |
| `knowledge_operator` / `embeddings` | 能 |

