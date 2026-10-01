# Stage 3 与 Stage 4 的分工

这份文档定义两个阶段各自的职责，用来回答一个具体问题：**新功能该放哪？**

分工标准不是"代码放在哪个目录"，而是**这个功能在做什么**：

> **Stage 3 决定"说不说、说什么"。Stage 4 决定"怎么说出去、能做什么"。**

## 一句话区分

| | Stage 3 | Stage 4 |
| --- | --- | --- |
| 关注点 | **对话、记忆、防护** | **功能扩展** |
| 核心问题 | 此刻该不该开口？该说什么？ | 用什么形式表达？能做哪些动作？ |
| 典型功能 | 判断回复时机、群聊注意力、人格与分寸、长期记忆、提示词注入防护 | 发表情、发图、发语音、Ark 卡片、戳一戳、群管理、QZone、调用 SnowLuma 的 action |
| 判断口径 | **拟人度、克制程度、记忆准确性、安全性** | **功能覆盖面** |

对照例子（用户明确给出的）：

- **判断回复时机 → Stage 3**（这是"说不说"）
- **发表情 → Stage 4**（这是"怎么说出去"）

## 为什么这样分

Stage 3 是**大脑**：它持有 prompt、记忆、注意力判定和防护边界。
Stage 4 是**手脚**：它给大脑接上更多表达方式和动作能力。

这个分法的好处是 **Stage 3 的改进会自动惠及 Stage 4**——因为两者共用同一个引擎和主循环
（见 `runtime.serve`）。Stage 4 只是多了通道能力，不需要重新实现对话逻辑。

反过来说：**如果一个改动需要改 prompt、记忆或注意力判定，它属于 Stage 3，
不管它表面上是为了哪个功能。** 例如"让 bot 能发表情"是 Stage 4，
但"判断什么时候该发表情而不是说话"是 Stage 3。

## 现有模块归属

### Stage 3（对话 / 记忆 / 防护）

| 模块 | 职责 |
| --- | --- |
| `base_prompt.py` | 人格设定（可信 system 前缀） |
| `stage3_runtime.py` | prompt 组装、输出协议解析、会话状态 |
| `dialogue_judge.py` | 判定 agent：只说"接不接、在聊什么"，不带人设与记忆 |
| `dialogue_compaction.py` | 对话压缩：旧消息 → 有界摘要，给回复段一个稳定前缀 |
| `stage3_main.py` | 对话引擎：触发、注意力、命令分发、决策应用 |
| `attention.py` | "是否在叫她"的判定 |
| `trigger.py` | 低频触发与消息去重 |
| `memory_*.py` | 长期记忆的存储、模型、服务、维护 Agent、视图 |
| `security.py` | 敏感请求拦截 |
| `typing_sim.py` | 说话的节奏（"怎么说"里的时间维度） |
| `state_store.py` | 短期会话状态的持久化 |
| `conversation_context.py` | 对话背景补全（只读） |
| `model_trace.py` / `metrics.py` | 观测（注入审计、脱敏指标） |
| `control.py` / `runtime_diagnostics.py` | 只读状态与诊断端口 |
| `extensions.py` | prompt 扩展端口（插件只能补充 DATA） |

### Stage 4（功能扩展）

| 模块 | 职责 |
| --- | --- |
| `capabilities.py` | **194 个 SnowLuma action 的登记与调用闸门**（`read`/`interaction`/`send`/`admin` 四种用途） |
| `data/snowluma_actions.json` | action 元数据目录 |
| `onebot_client.py` | WS **客户端**模式传输 + SnowLuma 只读 HTTP 封装 |
| `command_plugins.py` + `builtin_commands.py` | 命令插件接口（含档位与动作意图，见 `docs/ADD_A_COMMAND.md`）|
| `background_plugins.py` | **后台插件**的装配点与节拍循环（协议在 main 上，具体插件在 `stage4-plugins`）|

**以下几项在 `stage4-plugins` 分支上**（这条分支只放机制，不放 Stage 4 的功能扩展）：

| 模块 | 职责 |
| --- | --- |
| `builtin_group_commands.py` / `group_admin.py` / `group_owner.py` | 群管理与群主命令（档位 `super`）：踢 / 禁言 / 撤回 / 头衔 / 公告 / 改名片 |
| `qq_roles.py` / `join_approval.py` | 她自己角色的现查、入群审批策略 |
| `webui_panel.py` / `webui_access.py` / `webui_data.py` | **WebUI 面板**（2026-10-01 用户："面板属于 stage4 内容，本质插件"）：HTTP 与路由、本地/远程两套接入、只读数据层。它拿不到 `transport` 也拿不到引擎，只能调装配点注入的闭包 |
| `mail_client.py` / `mail_state.py` / `mail_channel.py` / `daily_report.py` / `letter_writer.py` | 邮件通道：读信回信、每日汇报 |
| `vision.py` | 识图（图片描述） |
| `operator_config.py` / `prompt_library.py` / `prompt_guard.py` / `knowledge_operator.py` / `memory_ops.py` / `runtime_flags.py` / `control_audit.py` / `provider_registry.py` | 面板的**接入面**：配置覆盖层、六套 prompt 的可编辑层、机制词扫描共用一份、知识库面板块、记忆只删不加、运行期开关、审计、供应商表。权限判定与动作执行仍在核心。**这些留在 main 上**——它们是接缝，不是功能 |

### 共用（不属于任何一侧，改动要同时服务两边）

| 模块 | 职责 |
| --- | --- |
| `runtime.py` | **与通道无关的装配与主循环**。两阶段唯一的差别是传入哪个传输层 |
| `transport.py` | 消息模型与 `QQTransport` 协议 |
| `onebot_ws.py` | 反向 WS 传输（Stage 3 现用） |
| `llm_client.py` | 模型调用与缓存计量 |
| `admin_control.py` / `memory_view.py` | 管理员与超管命令（工具性，非人格） |
| `builtin_balance_command.py` / `balance_client.py` | `/balance` 余额查询（运维命令，默认仅超管私聊，fail-closed） |
| `dev_config.py` | 配置读取 |

## 已经出现的交叉点

有一处现在就是交叉的，值得说清楚：

**`conversation_context.py` 依赖 `capabilities.py`。**

对话背景补全（Stage 3 的功能：让她知道群里聊过什么）通过 SnowLuma 的只读 HTTP 接口拿数据，
而那些接口要过 `capabilities.py` 的闸门。

这是**单向依赖**，可以接受：Stage 3 只碰 `purpose="read"` 那两个 action。
但要注意——

> **闸门本身属于 Stage 4。** Stage 3 只是恰好需要读两个只读接口。
> 如果之后 Stage 3 的功能开始要求放开更多 action，那说明设计跑偏了。

目前全代码库里只有两处调用闸门，都是 `purpose="read"`：

```
conversation_context.py:  get_group_msg_history
conversation_context.py:  get_group_member_list
```

`interaction` / `send` / `admin` 三条路径**定义了但没有任何调用点**——那是留给 Stage 4 的。

## 判断新功能归属的操作步骤

1. 问：它是在改**"说不说/说什么"**吗？
   → Stage 3。典型信号：要动 prompt、记忆、注意力判定、防护边界。
2. 问：它是在改**"怎么说出去/能做什么"**吗？
   → Stage 4。典型信号：要调 SnowLuma action、要发非纯文本、要处理新事件类型。
3. 两个都沾 → **拆开**。"判断何时发"归 Stage 3，"发出去"归 Stage 4。
4. 哪个都不沾（装配、配置、传输、观测）→ **共用模块**，改动必须同时服务两边，
   不能为某一侧开后门。

## 给 Stage 4 的已知接缝

Stage 4 接入时**不需要改动 `runtime.serve`**，只需要：

1. **加一个阶段入口**，选 `OneBotClientTransport` 而不是 `OneBotWebSocketTransport`：

   ```python
   transport = OneBotClientTransport(QQBOT_SNOWLUMA_WS_URL)
   await serve(transport, stage_label="Stage 4")
   ```

   `OneBotClientTransport` 已实现、有离线测试、接口与反向模式一致，只是还没有入口用它。

2. **补齐两个基础设施缺口**（详见下面"尚未实现"）。它们是**传输层**的事，
   发生在 `runtime` 和 `transport` 层，不是 Stage 3 的逻辑。现在补比以后补便宜，
   因为现在只有 2 个命令插件、1 个模型调用点。

## 尚未实现

### 缺口 1：出站只能发纯文本

`onebot_ws.py` 的 `_compose_message` 只有"纯文本"和"纯文本 + 引用"两种形态。
入站能解析 `[图片]` `[表情]` `[转发]` `[引用]` `@`，**出站全表达不出来**。

**这挡住了 Stage 4 的多数功能**：发表情、发图、发语音、Ark 卡片、群相册。

倾向的做法：**新增一条段消息接口，旧的纯文本路径一字不改**——风险最小。

这也直接影响命令插件：`CommandPlugin.handle()` 只能返回 `str | None`，
所以插件现在写不出"发表情"这种命令。**先补缺口 1，插件才谈得上表达形式。**
详见 `docs/ADD_A_COMMAND.md`。

### 缺口 2：非消息事件被静默丢弃

`parse_message_event` 对 `post_type != "message"` 返回 `None`，
`_handle_payload` 拿到 `None` 就丢。**notice / request 根本没有通道进来。**

**这挡住了**：戳一戳、表情回应、群成员变动、加好友 / 加群请求。

### 不属于上述缺口的事

- **命令插件的接口能力窄**（只能返回 `str`）。这个**不急着扩**——
  接口应当从第一个真实需求长出来，而不是按想象设计。等 Stage 4 第一个命令来了再定。

## 给 Stage 3 的提醒

Stage 3 阶段**不应引入任何"必须调用某个 SnowLuma action 才能完成"的功能**。
如果出现这种需求，说明它其实是 Stage 4 的功能，只是被误判了。

Stage 3 的目标是：**把对话、记忆和防护做到稳**。功能覆盖面不是它的指标。
