# Stage 3 重做方案（stage3-rebuild 分支）

起点：`main`（`09fc482`，771 用例）。

## 一、起点的实测状态

### 1.1 一个必须先说的发现

`main` 上的 `src/qq_roleplay_bot/plugins/` 里**没有任何 `.py` 文件**。

（更正：我先说那些 `__pycache__/*.pyc` 是"我误提交的"——**这是错的**。
`git ls-files` 查过，仓库里**一条 `__pycache__` 都没被跟踪**（`.gitignore` 挡住了）。
磁盘上那些 `.pyc` 是我在 `stage4-plugins` 上跑测试留下的产物，切分支不会删它们。
所以没有"索引里的垃圾要清"，第 0 步取消。）

真正的事实是：**`main` 上没有任何插件实现**。git 不跟踪空目录，所以 `plugins/`
在仓库里根本不存在——它现在之所以在磁盘上，是因为我去 `stage4-plugins` 转了一圈。

结论不变，而且更干脆：**`main` 只有"插件机制"（`command_plugins.py` /
`background_plugins.py` 两个契约模块），没有插件。**

### 1.2 核心 57 个模块里，混进来的非 Stage 3 东西

按用户判据（"删掉它，Stage 3 会不会出问题"）逐条量过，混在核心的是：

| 模块 | 它是什么 | 被谁拴住 |
| --- | --- | --- |
| `typing_sim` | 拟人停顿、分段（**出站表现**） | `stage3_main:34` 顶层 |
| `help_card` | 帮助卡片渲染（**出站渲染**） | `stage3_main` 函数内 |
| `builtin_balance_command` | `/balance`（运维查询） | `stage3_main` / `builtin_commands` 函数内 |
| `balance_client` | 余额 HTTP 客户端 | `builtin_balance_command:15` 顶层 |
| `runtime_diagnostics` | 进程/网卡/风扇采样 | `stage3_main:81` 顶层 |
| `control` | `/super` 运行时控制 | `stage3_main:65` 顶层 |
| `admin_control` | `/admin` 管理命令 | `stage3_main:80` 顶层 |
| `control_audit` | 操作审计 | `runtime:33` 顶层 |
| `provider_registry` | 供应商地址表 | `runtime:42` 顶层 |
| `qq_roles` | 她自己的群角色 | `runtime:43` 顶层 |
| `vision` | 识图 | `runtime:46` 顶层 |
| `knowledge_operator` / `embeddings` | 知识库索引（RAG） | `runtime` 函数内 |
| `model_trace` | 模型 I/O 落盘（调试） | `runtime` 函数内 |

### 1.3 归属（用户两次纠正后的口径）

| 层 | 判据 | 命令 |
| --- | --- | --- |
| **底层** | 删掉它，**服务起不来 / 管不了这台机器** | `/super restart`、`status`、`processes\|lan\|fan`、`apicheck`、授权名单与 `permit` |
| **Stage 3** | 删掉它，**对话或记忆出问题** | `/ping`、`/help`、`/admin`（开关群/清会话）、`/super memory*`、`affinity`、`profile` |
| **插件** | 删掉它，只是**少一项能力** | 群管理、入群审批、面板、邮件、识图 |

**机制与归属是两件事**：只有 Stage 4 的内容**必须**用插件机制；Stage 3 / 底层的命令
不必走插件机制（用户 2026-10-01："什么命令用插件实现，我他妈什么时候这么说过"）。

## 二、目标：把 Stage 3 收敛成"三件事 + 一个宿主"

### 2.1 模块地图（目标）

```
src/qq_roleplay_bot/
├── _host/                    宿主：引擎要用、但**自己不实现**的能力
│   ├── __init__.py           Protocol 定义 + 空实现（`NoStyler` / `NoCards` /
│   │                         `NoMachine` / `NoAudit`，都在这个文件里）
│   └── （**没有** `stubs.py`）
│
├── stage3/                   **Stage 3 本体**（对话 / 记忆 / 防护）
│   ├── dialogue/             说不说、说什么
│   │   ├── engine.py         主引擎（现在的 stage3_main 主体）
│   │   ├── judge.py          判定 agent
│   │   ├── compaction.py     压缩
│   │   ├── attention.py      注意力与回应时机
│   │   ├── focus.py          群聊专注
│   │   ├── context.py        对话背景
│   │   └── persona.py        人格装配（现 stage3_runtime）
│   ├── memory/               记什么、怎么记、边界
│   │   ├── service.py / store.py / model.py / inbox.py
│   │   ├── maintenance.py    维护 agent
│   │   ├── filters.py / config.py / ops.py / view.py / people.py
│   ├── defense/              拦什么
│   │   ├── security.py       注入 / 越界 / 敏感
│   │   └── prompt_guard.py   机制词扫描
│   └── commands/             **Stage 3 自己的**管理命令
│       ├── public.py         /ping /help
│       ├── admin.py          /admin（开关群、清会话）
│       └── super.py          /super status|memory*|affinity|profile|permit|名单
│
├── 底层（不属于 stage3，也不属于插件）
│   ├── transport.py / onebot_ws.py / onebot_client.py
│   ├── llm_client.py / api_usage.py / capabilities.py
│   ├── dev_config.py / operator_config.py / state_store.py
│   ├── control_audit.py / feature_log.py / metrics.py
│   ├── base_prompt.py / extensions.py / outbox.py
│   ├── command_plugins.py    命令插件契约（Stage 4 用）
│   ├── background_plugins.py 后台插件契约与节拍（Stage 4 用）
│   ├── plugin_host.py        发现与装配（唯一的 discover 入口）
│   └── runtime.py            装配与主循环（两阶段共用）
│
└── plugins/                  空。stage4 的东西在另一条分支
```

**为什么要动目录**：现在 57 个模块平铺在一层，"哪些是对话、哪些是记忆、哪些是底层"
只能靠文件名猜。分目录之后，`import` 路径本身就说明了层次。

### 2.2 六个注入接口（核心只说"我需要什么"）

| # | Protocol | 方法 | 谁实现 | 谁都不装时 |
| --- | --- | --- | --- | --- |
| 1 | `OutboundStyler` | `plan(text, *, backlog) -> list[Segment]` | `typing_sim`（出站表现） | 一次性发出，不分段不等待 |
| 2 | `CardRenderer` | `render(title, lines) -> bytes \| None` | `help_card` | 回纯文本 |
| 3 | `MachineProbe` | `sample() -> MachineSample` | `runtime_diagnostics` | 诊断命令答"这台机器没接诊断" |
| 4 | `AuditSink` | `record(op, payload, result, *, source, actor)` / `tail(n)` | `control_audit` | 不记审计 |
| 5 | `BalanceSource` | `query() -> Balance` | `balance_client` | 无 `/balance` 这条命令 |
| 6 | `RoleLookup` | `role(group_id) -> str` / `self_id()` | `plugins/roles` | 群主动作一律 fail-closed |
| 7 | `KnowledgeIndex` | `search(text, k) -> list[Hit]` | `knowledge_operator` | 无长期知识检索 |

（第 6 条**已经是**插件接口的样板，照它做其余六条。）

### 2.3 命令注册的两条链

| 链 | 谁登记 | 长什么样 |
| --- | --- | --- |
| **Stage 3 / 底层自己的命令** | `stage3/commands/*.py`，由核心装配 | 与现在一致 |
| **插件命令** | `plugins/<功能>/plugin.py` 调 `registry.command()` | `name` / `min_level` / `match()` / `handle()` |

两条链在 `handle()` 里汇合：**插件命令先匹配，核心命令后匹配**（特权命令不与
`/ping`、`/help` 抢）。权限判定、动作执行、护栏、审计一律在核心。

## 三、动手顺序（每步都能独立验收）

| 步 | 做什么 | 验收 |
| --- | --- | --- |
| **1** | 建 `_host/`（Protocol + 空实现），**不接线** | 测试全绿 |
| **2** | 接线：核心改调 `_host`，默认给空实现 | 测试全绿、行为不变 |
| **3** | 建 `stage3/` 分目录，把对话/记忆/防护搬进去（**只搬不改**） | 测试全绿 |
| **4** | 把 `typing_sim` / `help_card` / `balance` / `diagnostics` 改写成**实现 `_host` 的模块**，从核心装配里摘出去 | 测试全绿；摘掉后核心仍起 |
| **5** | 命令分家：`stage3/commands/` 三份 | 测试全绿 |
| **6** | 最终验收：把 `plugins/` 整个删掉，Stage 3 全绿 | `ALL_OFFLINE_TESTS_PASSED` |

**第 3 步是唯一有风险的**（动 import 路径，牵连 771 个测试）。所以它单独一步，
而且只搬不改——搬完先跑测试，绿了再往下。

## 四、这份方案里我不确定的

1. **要不要真的分 `stage3/` 目录**。好处是层次清楚；代价是 57 个模块的 import 路径全变、
   771 个测试跟着改。如果你觉得不值，就跳过第 3 步，其余照做。
2. **被摘出去的东西最终放哪**——你说"先只留接口，落点待定"。第 4 步做完之后，
   那些实现可以放 `plugins/`、放 `stage4/`、或者先留在原地只是不被核心 import。
