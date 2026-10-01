> **⚠️ 已归档（历史文档，不再维护）**
>
> 本文件是 2026-09-25 面向旧 Codex 会话的交接记录，其中描述的文件路径、运行入口和
> 待办事项已经被后续重构取代。当前状态请以项目根目录的 `README.md`、`ARCHITECTURE.md`
> 和 `DEFECT_VERIFICATION.md` 为准。保留本文仅用于追溯当时的决策背景。
# QQRoleplayBot 交接文档

更新时间：2026-09-25  
项目目录：`C:\Users\you`  
关联旧 Codex 会话：`01a05599-0cc8-7db0-9d32-7a65a21fbeba`

## 0. 给下一次 Codex 会话的直接指令

请先完整阅读本文件，再阅读 `README.md`、`ARCHITECTURE.md`、`STAGE3_DESIGN.md` 和当前源码。不要仅依据旧会话摘要判断项目状态。

当前应以 Stage 3 代码为准，不要把旧的 V1、Stage 2、`dev_*` 实验入口误认为默认运行版本，也不要把尚未验证的 NapCat 实机链路说成已经恢复。

本项目的稳定范围仍然是：只处理目标群 `717151356`（`717151356`），先证明 QQ → NapCat → Bot → 模型 → NapCat → QQ 的闭环；长期记忆已按 `LONG_TERM_MEMORY_PLAN.md` 接入一个独立维护 Agent，不扩展为通用多 Agent 对话架构。

## 1. 项目目标和边界

这是一个独立实现的轻量 QQ 中文语擦 Bot：

```text
QQ / NapCat
    │ OneBot v11 反向 WebSocket
    ▼
onebot_ws.py
    ▼
stage3_main.py / Stage3Engine
    │ 过滤、去重、触发、短期状态、一次模型调用
    ▼
llm_client.py
    │ OpenAI-compatible /chat/completions，非流式
    ▼
Stage3Decision
    ▼
OneBot send_msg
```

必须保持的范围：

- 只处理群 `717151356`；其他群直接忽略。
- 默认不处理私聊。只有显式配置 `PRIVATE_DEBUG_USER_IDS` 后，指定账号才可用于本地私聊调试。
- NapCat 只负责 QQ 通信，不负责 prompt、记忆、模型调度或人格逻辑。
- 模型输出只返回自然聊天正文；XML 决策字段、短期语境和安全判断不能发送到 QQ。
- V1/Stage 3 优先稳定闭环；长期记忆只使用用户确定的独立维护 Agent 方案，不引入其他多 Agent、复杂路由或全群扩展。

## 2. 从旧会话恢复的决策历史

### 2.1 评估过的 `fanlong-tongyong`

旧会话先检查了 `RagdollCat-alt/fanlong-tongyong`。该项目是基于 LLbot/青果核/OlivOS 的 QQ 群 RPG 插件集合，包含角色档案、属性、装备、货币、商店、骰子、戏录、自定义回复和后台管理。

实际代码中没有发现完整的 OpenAI/LLM 调用、模型上下文记忆或 AI 对话策略。因此它可以参考为 RPG/角色数据管理底座，但不能直接当成目标 AI 语擦 Bot。当前项目因此采用独立的 OneBot/NapCat + 模型架构，没有把旧插件套件直接并入。

### 2.2 V1 基线

最初的 V1 约定是：

- 普通消息达到 20 条，或首条消息等待 60 秒后由下一条消息触发一次模型调用。
- @机器人在不处于冷却时可立即触发模型检查。
- 1 分钟冷却期间，@不能绕过冷却。
- 每次最多提交最近 20 条消息。
- 使用 `MessageDeduplicator` 避免 OneBot 重复事件造成重复调用。
- V1 只保证“何时调用”和“发送模型正文”，不承诺模型一定应答，也不包含完整对话状态。

这部分仍保留在 `v1_main.py` 和 `tests/test_v1.py` 中，但不是当前默认入口。

### 2.3 Stage 2 / Stage 3 演进

后来项目增加了短期对话状态：

- 普通状态下低频触发；模型可以返回 `REPLY`、`NO_REPLY` 或 `EXIT_DIALOGUE`。
- 回复后进入短期对话状态，对话中后续消息逐条交给模型判断。
- 话题转移、明显不再对话或模型要求退出时，回到普通状态。
- Stage 3 在一次模型调用中同时完成语境理解、是否参与、回复生成和短期语境更新。

用户希望先完成这一条稳定闭环，再考虑 V2/Stage 4 的更复杂策略。当前已实现一个职责单一的 Memory Maintenance Agent，负责定期维护长期记忆；旧会话中其他多 Agent、不同供应商分别承担回复/路由等想法仍属于后续探索，不应自动并入。

## 3. 当前实际实现：Stage 3

### 3.1 默认运行入口

`pyproject.toml` 的脚本入口是：

```text
qq-roleplay-bot = qq_roleplay_bot.stage3_main:main
```

推荐使用：

```powershell
.\run_stage3.bat
```

Stage 2 回退入口：

```powershell
.\run_stage2.bat
```

不要同时启动 `v1_main.py`、Stage 2、Stage 3 或 `dev_*` 入口，否则会出现重复处理或 8080 端口冲突。

### 3.2 Stage 3 行为

当前 `Stage3Engine` 的关键行为：

- `TARGET_GROUP_ID = "717151356"`。
- `BATCH_SIZE = 20`。
- `COOLDOWN_SECONDS = 60.0`。
- 每个会话最多保留最近 50 条消息。
- 活跃会话 180 秒无活动后退出。
- 每个符合条件的事件最多调用模型一次。
- 任意群或私聊的完整 `yunru ping` 直接返回 `pong`，不受群启用状态和私聊白名单限制，不调用模型或记忆；重复 OneBot 事件仍会去重。
- 普通状态由 20 条/60 秒或 @触发检查。
- 活跃状态逐条判断是否回复、保持或退出。
- 消息 ID 最多去重 4096 条。
- 其他群、未授权私聊、空文本和非 message 事件不会进入模型。

### 3.3 输出协议

Stage 3 的模型输出协议位于 `stage3_runtime.py`：

```xml
<decision>REPLY|NO_REPLY|EXIT_DIALOGUE</decision>
<dialogue>KEEP|EXIT</dialogue>
<reply>仅在 REPLY 时填写聊天正文</reply>
<context>
topic=当前话题
topic_status=active|shifted|ended
intent=发言意图
tone=serious|joking|teasing|sarcastic|uncertain
target=bot|user|group|unknown
pending_question=未解决的问题，没有则填无
confidence=0到1之间的小数
</context>
```

本地解析器只把 `<reply>` 作为 QQ 正文。它仍兼容 Stage 2 的 `NO_REPLY`、`EXIT_DIALOGUE` 和普通文本回退。

### 3.4 安全边界

`security.py` 在进入模型前检查涉及本机文件、进程、端口、环境变量、路径、API Key、密码、命令执行和绕过安全规则的请求。

当前行为是：

- 普通群成员和普通管理员的敏感本机请求直接阻断，不调用模型，不发送回复。
- 即使消息自称管理员，也不会获得权限。
- `ADMIN_USER_IDS` 和 `SUPER_ADMIN_USER_IDS` 当前包含 `900000001`；管理员控制命令限定在控制群或与 Bot 的私聊中，超级管理员仅可使用固定进程诊断入口，不开放任意本机命令、文件、密钥或环境变量读取。
- 聊天内容会被当作不可信 DATA 放入明确边界中，不能改变 system prompt 或 XML 协议。
- `sanitize_chat_text` 会移除控制字符，并把单条输入限制为 4000 字符。

### 3.5 基础对话策略

固定基础策略位于 `src/qq_roleplay_bot/base_prompt.py`，由 `stage3_runtime.py` 拼接到固定 system prompt 的最前部。它要求模型按以下顺序处理每次输入：

1. 先判断是否在对 Bot 说话；群体闲聊或明确对别人说话时保持克制。
2. 再判断是否值得回复；没有新问题、只会重复原话或对方明确要求停止时可以不回复。
3. 回复通常控制在一到三句，具体、口语化、克制，不连续追问、不强行鸡汤、不凭空编造。
4. 结合前后文理解玩笑、反讽、情绪和话题转移；持续互动时保持 `KEEP`，明显结束或继续插话突兀时选择 `EXIT_DIALOGUE`。

群聊短期会话还记录 `active_user_id`：触发 YunRu 回复的用户优先获得后续对话焦点；其他用户的未 @ 消息只作为背景，不会抢焦点或刷新活跃超时。上下文中的 `speaker="yunru"` 表示 YunRu 自己已经发出的历史回复，`speaker="user" user_id="..."` 表示具体 QQ 用户；不同 `user_id` 不得混为同一人。

这部分是可信固定策略，不接受聊天、插件、知识库或额外 prompt 的覆盖；这些动态内容仍只进入 user 消息的不可信 DATA 区域。`tests/test_prompt_regression.py` 和 `tests/test_stage3.py` 会检查基础策略、发言者区分和协议边界。

## 4. 关键文件

| 文件 | 作用 | 当前判断 |
| --- | --- | --- |
| `src/qq_roleplay_bot/stage3_main.py` | Stage 3 生命周期、群过滤、会话、触发和模型调用 | 当前默认入口 |
| `src/qq_roleplay_bot/stage3_runtime.py` | Stage 3 状态、prompt、XML 解析 | 当前核心业务协议 |
| `src/qq_roleplay_bot/base_prompt.py` | 固定基础对话策略 | 可信 system prompt 层 |
| `src/qq_roleplay_bot/onebot_ws.py` | OneBot 反向 WebSocket 和有限连接等待 | 连接未建立时有限失败 |
| `LONG_TERM_MEMORY_PLAN.md` | 长期记忆范围、隔离、隐私和验收标准 | 实现约束和验收依据 |
| `src/qq_roleplay_bot/memory_model.py` | 记忆记录、事件、结构化操作和硬约束 | Agent 输出校验与 DATA 格式化 |
| `src/qq_roleplay_bot/memory_inbox.py` | 有限临时对话材料队列 | 异步捕获，保存失败不阻塞对话 |
| `src/qq_roleplay_bot/memory_store.py` | SQLite schema、事务、租约、检索和幂等 | 本地持久化实现 |
| `src/qq_roleplay_bot/memory_maintenance_agent.py` | 定期后台模型调用和结构化维护 | 独立于实时回复 |
| `src/qq_roleplay_bot/memory_service.py` | Stage 3 读取、捕获、启动和关闭 | 长期记忆生命周期 |
| `src/qq_roleplay_bot/stage2_main.py` | Stage 2 回退运行入口 | 可回退，不应并行启动 |
| `src/qq_roleplay_bot/stage2_runtime.py` | Stage 2 状态和旧输出解析 | 兼容层 |
| `src/qq_roleplay_bot/v1_main.py` | 最初 V1 低频触发实现 | 历史入口，非默认 |
| `src/qq_roleplay_bot/onebot_ws.py` | OneBot v11 入站解析、@识别、出站回执 | 核心传输层 |
| `src/qq_roleplay_bot/transport.py` | `IncomingMessage`、`MessageTarget` 等内部消息模型 | 传输和业务边界 |
| `src/qq_roleplay_bot/llm_client.py` | 标准库 HTTP 模型客户端 | 非流式 `/chat/completions` |
| `src/qq_roleplay_bot/security.py` | 敏感本机请求阻断和输入清理 | 当前安全前置层 |
| `src/qq_roleplay_bot/dev_config.py` | 本地开发配置和环境变量读取 | 不保存明文 API Key |
| `tests/test_stage3.py` | Stage 3 解析、触发、状态和群过滤测试 | 离线覆盖核心流程 |
| `tests/test_security.py` | 安全阻断测试 | 离线覆盖 |
| `tests/test_onebot_ws.py` | OneBot 消息解析测试 | 离线覆盖 |
| `src/qq_roleplay_bot/extensions.py` | 插件、知识库和额外 prompt 的受限扩展端口 | 当前 Stage 3 扩展汇聚层 |
| `src/qq_roleplay_bot/control.py` | 未来 WebUI 使用的脱敏快照和控制协议 | 当前 Stage 3 已实现业务端口，不是认证层 |
| `src/qq_roleplay_bot/admin_control.py` | 管理员命令解析、群聊启停和转告命令 | 只在控制群由管理员 QQ 生效 |
| `src/qq_roleplay_bot/runtime_diagnostics.py` | 超级管理员固定进程诊断 | 仅控制群、固定进程枚举、限长输出 |
| `tests/test_v1.py` | V1 去重、20 条、@和冷却测试 | 历史基线 |
| `REAL_WORLD_TEST_CASES.md` | 目标群实机测试顺序 | 只允许在目标群执行 |
| `NAPCAT_INTERFACE_PLAN.md` | NapCat/OneBot 接口规划 | 规划与验收依据 |

## 5. 当前配置和外部链路

### 5.1 Bot 项目模型配置

当前 `src/qq_roleplay_bot/dev_config.py` 使用：

- API Base URL：`https://api.huanyan.ltd/v1`
- 模型：`gpt-5.6-luna`
- 接口：`/chat/completions`
- `stream: false`
- 目标群：`717151356`

API Key 真实值不写入本文件；运行时通过 `QQBOT_API_KEY` 环境变量或外部密钥管理提供。不要把用户消息中的密钥复制到源码、文档、日志或提交记录，并考虑轮换曾经暴露过的开发密钥。

### 5.2 NapCat / OneBot

Bot 默认监听：

```text
127.0.0.1:8080
```

NapCat 反向 WebSocket 客户端应连接：

```text
ws://127.0.0.1:8080
```

当前设计不要求启用 NapCat HTTP API、HTTP Server、HTTP SSE 或 WebSocket Server。出站使用 OneBot `send_msg` 并等待 echo 回执。

验收时必须同时确认：

1. Bot 进程正在监听 8080；
2. NapCat 日志出现该账号的反向 WebSocket `Established`；
3. 收到目标群入站事件；
4. `send_msg` 回执成功。

只看到 `Listen` 不能证明 NapCat 已连接。

## 6. 已验证和未验证状态

### 已验证

- 旧会话阶段曾完成过 QQ → NapCat → Bot WebSocket → 模型 → NapCat → QQ 的真实 V1 闭环，用户确认收到过测试消息。
- OneBot 文本、消息段、@识别、群过滤和回执逻辑已有离线测试。
- Stage 3 结构化输出、状态转换、单次模型调用、上下文 prompt 和注入边界已有离线测试。
- 运行项目自带的 `run_offline_tests.bat` 于 2026-09-23 执行成功，输出 `ALL_OFFLINE_TESTS_PASSED`。
- 该离线入口不访问真实模型、不连接 QQ、不发送外部消息。

### 当前未验证或仅部分完成

- 当前机器此刻的 NapCat 是否已经与 8080 `Established`，尚未在本次交接前重新确认。
- 当前 huanyan.ltd API Key 是否仍有效、模型是否仍可用，未在本次重启后主动发起新的模型请求；此前已有真实闭环记录。
- Stage 3 当前版本的完整 QQ 实机测试尚未重新跑完；应按 `REAL_WORLD_TEST_CASES.md` 从 R-01 到 R-04 开始。
- NapCat 之前曾出现连接关闭：历史日志记录过 `session closed: UIN=900000002`，随后未观察到新的 Established。不能把“后端监听正常”写成“QQ 链路恢复”。
- 项目目录当前没有 Git 元数据，不能据此声称存在某个分支、提交或远程同步状态。
- 虚拟环境中没有安装 pytest；不要把 `python -m pytest` 失败写成项目测试失败。当前可用的是项目自带的无 pytest 离线入口。

## 7. 推荐下一步

按以下顺序推进，不要直接扩展范围：

1. 检查是否只有一个 `qq_roleplay_bot.stage3_main` 进程，并确认 8080 的监听归属；避免同时启动旧 V1/Stage 2/Stage 3。
2. 启动 `run_stage3.bat`，观察 `Stage 3 started`。
3. 启动或重启 NapCat，等待双方都出现连接成功证据；只看 8080 Listen 不够。
4. 只在目标群 `717151356` 执行 `REAL_WORLD_TEST_CASES.md` 的 R-01～R-04：启动、连接、@触发、实际回复。
5. 通过后再测试冷却、20 条批量、活跃对话、话题转移和重连；不要为了测试过滤器向其他群发送消息。
6. 如果模型请求失败，先记录脱敏的 HTTP 状态、错误类别、请求触发原因和耗时，不记录 API Key 或完整群聊历史。
7. 完成 Stage 3 稳定性后，继续按 `LONG_TERM_MEMORY_PLAN.md` 做长期记忆的真实 QQ 小范围验收，观察 Agent 误记、遗忘延迟、跨群误用率和维护成本。

## 8. 与 Codex 中转站问题的关系

旧 Codex 会话切换中转站后卡在 `Optimizing the conversation`，并报 `stream disconnected before completion: stream closed before response.completed`。这个问题发生在 Codex 自身使用的 Responses 流链路上，与本项目的 `llm_client.py` 无关：本项目使用的是非流式 `/chat/completions`。

旧会话无法继续时，本文件可作为迁移上下文。不要把 Codex 当前配置中的中转地址、API Key 或 Responses 设置复制到 Bot 项目；两套客户端的协议和密钥范围不同。

## 9. 修改原则

- 先查看真实源码、测试和运行日志，再判断“已完成”。
- 保持目标群白名单，不默认扩大私聊、群聊或外部数据发送范围。
- 不记录、复制或提交真实 API Key。
- 先修复 V1/Stage 3 的稳定闭环，再引入新架构。
- NapCat 连接问题必须用 `Established` 证据确认。
- 离线测试通过不等于 QQ 实机闭环通过。

### 6.1 Stage 3 安全与扩展增量

- 聊天、知识库、插件和额外 prompt 都进入 user 消息的明确 DATA 区域；XML 内容会转义，不能覆盖 system prompt、白名单或输出协议。
- 插件、知识库和额外 prompt 有数量/长度限制；扩展收集和通知有 2 秒超时，异常会降级，不阻塞 Stage 3 主循环。
- 模型最终回复会清理控制字符、内部协议标签并限制为 1000 字符后才允许出站。
- `Stage3Engine.snapshot()` 仅提供脱敏会话摘要和计数；`set_enabled()`、`leave_session()` 为未来 WebUI 的业务控制端口。WebUI 仍需自行实现认证、CSRF、访问限制和审计。
- 长期记忆已实现第一版：人设是受控可信配置，知识库是带来源的 `KNOWLEDGE DATA`，Memory Maintenance Agent 从有限临时 Inbox 中自主维护 `MEMORY DATA`。群范围记忆按 `group_id` 隔离，`user_group` 按 `group_id + user_id` 隔离，`user_global` 按 `user_id` 跨群共享；不提供记忆命令或用户确认码，不保存完整聊天记录，是否写入、更新、合并、删除或忽略完全由 Agent 判断，并提供过期、幂等、租约和数据库故障降级。离线记忆与 Stage 3 集成测试由 `tests/test_memory.py` 覆盖。
- `dev_config.py` 不再保存明文 API Key；普通 Stage 3 对话和记忆维护需要通过 `QQBOT_API_KEY` 提供凭据，模型地址和模型名可由 `QQBOT_API_BASE_URL`、`QQBOT_API_MODEL` 提供。没有凭据时仍启动 `yunru ping` 健康检查。
- `run_offline_tests.bat` 已覆盖 XML/prompt 注入、凭据/命令请求、恶意扩展、扩展超时、模型异常输出、管理员群聊控制和 WebUI 快照边界；测试不会调用真实模型或 QQ。
- 管理员控制命令只允许 `900000001` 在控制群 `717151356` 或与 Bot 的私聊中执行：`#bot enable 群号`、`#bot disable 群号`、`#bot status`、`#bot clear 群号`、`#bot help`。超级管理员可使用 `#bot super processes` 或固定自然语言进程诊断请求；该入口不调用模型、不执行任意命令、不读取密钥或文件。
- 转告命令支持 `#bot relay group 群号 内容`、`#bot relay user QQ号 内容`、`#bot relay group_name 群名 | 内容` 和 `#bot relay nickname 昵称 | 内容`；名称通过 OneBot 只读列表解析，重名时用 `#bot select 确认码 序号` 选择候选。首次只生成 120 秒有效的确认码，只有发起转告的管理员执行 `#bot confirm 确认码` 后才发送，`#bot cancel 确认码` 可取消。转告不调用模型，目标发送成功或失败都会尝试通知管理员。
- 不要因为旧会话中出现过某个想法，就把它当成当前批准的功能。

