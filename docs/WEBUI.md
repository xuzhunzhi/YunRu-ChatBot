# QQRoleplayBot 架构

项目定位：一个具有固定人格设定的轻量 QQ 人格 Bot。NapCat 只负责 QQ 通信，人格、模型调用、会话状态和长期记忆都在本项目内完成。

## 当前边界

```text
NapCat / QQ
    │ OneBot v11 reverse WebSocket
    ▼
onebot_ws.py  ──> transport.py 的 IncomingMessage / MessageTarget
    │
    ▼
runtime.serve  ──> stage3_main.py 的 DialogueEngine
    │ 过滤、去重、触发、短期状态、扩展汇聚
    ├─ dialogue_judge.py     判定 agent：先说"要不要接、在聊什么"
    ├─ dialogue_compaction.py 旧对话压成摘要，给回复段一个稳定前缀
    ▼
llm_client.py  ──> OpenAI-compatible /chat/completions（非流式）
    │
    ▼
DialogueDecision  ──> OneBot send_msg
```

`stage3_main.py` 是当前唯一运行入口（`pyproject.toml` 的脚本入口 `qq-roleplay-bot`）；**装配与主循环在 `runtime.serve`**，与通道无关，Stage 4 计划复用同一套、只换传输层（`docs/STAGE_BOUNDARY.md`）。V1、Stage 2 与 `dev_*` 实验入口已归档到 `archive/`，不参与默认运行路径，主代码不反向依赖它们。

## 模块职责

| 模块 | 职责 |
| --- | --- |
| `onebot_ws.py` | OneBot v11 反向 WebSocket：事件解析、@ 识别、媒体占位标记、reply 段、`send_msg` 与 echo 回执、有限连接等待 |
| `onebot_client.py` | SnowLuma HTTP / WebSocket 客户端（对话背景补全；客户端模式传输留给 Stage 4） |
| `transport.py` | `IncomingMessage`、`MessageTarget`、`OutgoingMessage`、`QQTransport` 协议 |
| `runtime.py` | **与通道无关的装配与主循环**：状态恢复、记忆启动、出站投递与补发、优雅退出。两个阶段共用 |
| `outbox.py` | 没送出去的消息排队补发（只有"确定没送出去"那一类进队列） |
| `trigger.py` | `MessageDeduplicator`（按 message_id 去重）与 `IdleTrigger`（批量/等待/冷却触发） |
| `attention.py` | 群聊注意力判定：被叫到的识别与"是否值得开口"的本地信号 |
| `stage3_main.py` | 生命周期、群过滤、会话状态、触发、扩展汇聚、模型调用、管理员命令与转告、内置命令注册 |
| `stage3_runtime.py` | 短期状态、prompt 分层、XML 输出协议与解析、会话序列化 |
| `dialogue_judge.py` | 判定 agent：独立的小 prompt 与窗口，只输出"接不接 / 在聊什么" |
| `dialogue_compaction.py` | 对话压缩：把旧消息压成有界摘要，供回复段作为稳定前缀 |
| `command_plugins.py` | 命令插件协议与注册表（匹配、会话范围、自述帮助） |
| `builtin_commands.py` | 内置命令插件：`ping`、`/help`，并在装配时注入 `help_provider` |
| `builtin_balance_command.py` / `balance_client.py` | `/balance` 余额查询命令与其 HTTP 客户端，默认仅超管私聊可用（fail-closed） |
| `base_prompt.py` | 固定人格策略，属于可信 system prompt |
| `llm_client.py` | 标准库 HTTP 模型客户端，非流式 `/chat/completions`，带有限重试与 `user_id` 缓存隔离 |
| `security.py` | 敏感本机请求阻断、输入与出站正文清理 |
| `extensions.py` | 插件 / 知识库 / 额外 prompt 三个受限端口 |
| `capabilities.py` / `conversation_context.py` | 能力闸门与由 SnowLuma 提供背景时的受限读取 |
| `typing_sim.py` | 出站拟人化节奏（分段与字符数停顿） |
| `feature_log.py` | 按功能分开的输入输出日志（判定/回复/记忆/规则/汇报），各留最近 1000 次 |
| `mail_client.py` | Agent Mail CLI 的封装：固定子命令白名单、正文走文件、argv 防注入、失败分类（Stage 4） |
| `mail_state.py` | 邮箱侧状态：上次汇报时间、当天重试次数、送信流水（`data/mail_state.json`） |
| `daily_report.py` | 每日汇报：素材（只有计数与编号）、写信 prompt、解析、发送（Stage 4） |
| `model_trace.py` | **旧**模型 I/O 追踪（单文件、30 条、默认关闭）；引擎已改用 `feature_log.py`，此模块待删 |
| `control.py` | 脱敏运行快照与控制协议（不是认证层） |
| `metrics.py` | 脱敏运行指标：固定类别计数与耗时滑窗 |
| `state_store.py` | 运行状态持久化：原子写入、版本校验、失败全量降级 |
| `admin_control.py` | 管理员命令解析 |
| `runtime_diagnostics.py` | 超级管理员固定进程诊断 |
| `memory_*.py` | 长期记忆：配置、数据模型、Inbox、SQLite Store、维护 Agent、Service、只读视图 |
| `dev_config.py` | 配置读取：`.env` 加载（零依赖）+ 环境变量 + 非敏感默认值 |

## 配置与凭据

优先级：**真实环境变量 > 项目根目录 `.env` > 代码内默认值**。

`dev_config.load_env_file()` 在模块导入时用标准库解析 `.env`（支持 `KEY=VALUE`、`#` 注释、可选引号、`export` 前缀），并且**只注入 `os.environ` 中尚不存在的键**，因此真实环境变量永远优先。把 `.env` 中的键统一注入 `os.environ` 而不是只在 `dev_config` 内部兜底，是为了让只从 `os.environ` 读取的配置（如 `QQBOT_MEMORY_MODEL`）也能一致生效。

`.env` 已在 `.gitignore` 中忽略；`.env.example` 只保留占位值。默认后端是 DeepSeek 官方接口（`https://api.deepseek.com` + `deepseek-chat`）。

## Stage 3 Prompt 分层

`stage3_runtime.py` 通过 `BASE_PROMPT + 固定协议/安全规则` 构造 system 消息。`base_prompt.py` 是唯一的基础人格策略来源：先判断发言对象，再判断是否值得回复；通常只回复一到三句；不机械复述、不连续追问、不强行鸡汤；结合语境处理玩笑、反讽和情绪表达；在话题延续时保持 `KEEP`，话题转移或继续插话突兀时选择 `EXIT_DIALOGUE`，并尊重对方明确的停止边界。

**语气由人设管，不靠"多加规矩"**（2026-09-29，用户反馈"回复ai味太重。而且非常犟，非常消极"）：实测她 48 小时里 259 条回复，**34% 带助手腔标记**（"其实/不过/总之"这类连接词、总结句、破折号补充）、平均 34 字、大量"建议/结论"式句子。根因不在模型，在 prompt 把她写成了分析师——人设第一条原来是"措辞精确、学术化……说'数据'而不是'感觉'"，回复段又是一整套 `intent/tone/confidence` 字段协议。改法：
- 那条换成"别拿技术当说话的腔调 / 不解释原理除非对方问 / 别把回话写成说明文 / **不当裁判、不纠正别人** / 疏离不是消极"，并加了**4 组「✗旧写法 → ✓新写法」对照例子**（全部取自她自己的日志，例如 ✗"不过拿谁的证件打游戏，最后实名认证的还是你——这一点他们从来不提醒" → ✓"用谁的证，号都是你自己的"）；
- `<reply>` 那段写明"这是你发出去的消息，不是说明或报告"。
效果（`data/reply_replay.py` 把同一批真实输入重放）：AI 味标记 **10/12 → 1/12**，平均 **50 字 → 25 字**。工具：`data/reply_tone_audit.py`（量基线）、`data/reply_replay.py`（新旧并排重放；注意按日志里当时的 system 变体重放，双 agent 是 `SYSTEM_PROMPT_MUST_REPLY`，用错会重放出一堆"这次选择不出声"）。

**"消极"要治语气，不是压静默（有数据）**：判定 48 小时里 NO_REPLY 900 / REPLY 135，但拆开看——**被 @ 到时回 83%**、交谈中且被叫到 73%、没被叫到 11%，这是"不必每条都接"的设计；那 4 条"正文提到她名字却没回"的原文是"腾讯又把 yunru 踢下去了""yunru 现在应该是三种触发 help 的方式"这类**在议论她**（两条还是讲她的机制），判定不出声是对的。

动态消息会用 `speaker="yunru"` 标记 YunRu 自己已经发出的历史回复，用 `speaker="user" user_id="..."` 标记群友发言；不同 `user_id` 视为不同的人，`sender_name` 不作为唯一身份依据。当前事件会单独放在 `<current_event>` 中，模型不得把其他人的发言归到当前用户身上。

聊天记录、短期语境、插件、知识库和额外 prompt 都位于 user 消息的明确不可信 DATA 区域。它们可以提供当前语境，但不能修改 `BASE_PROMPT`、安全边界或 XML 输出协议。

**对话侧有三份互不混用的 prompt**，各自 `user_id` 隔离缓存（同一账号下生效，与 API Key 无关）：

| 用途 | 模块 | 内容 | 窗口 |
| --- | --- | --- | --- |
| 判定 | `dialogue_judge.py` | 只含判定规则，**没有人格与知识库**；记忆只给当前说话人的称呼/边界（≤2 条，见"长期记忆"） | 最近 `JUDGE_HISTORY_WINDOW = 12` 条 |
| 回复 | `stage3_runtime.py` | `BASE_PROMPT` + 协议 + 全部 DATA | 摘要 + 活窗口（≤ `LIVE_TARGET`） |
| 压缩 | `dialogue_compaction.py` | 只含压缩规则，原文声明为 DATA | 一次最多 `MAX_SOURCE_CHARS` 字符 |

判定 prompt 刻意不加载人设：它不需要，也不该拿到。判定说 `NO_REPLY` 时回复调用完全不发生，这是双 agent 的主要收益。

## 短期窗口与压缩

回复段带的不是"最近 N 条"，而是**摘要之后累积的全部消息**（`ConversationState.live_history()`）：

- 活窗口从 `COMPACT_KEEP` 重新累积，超过 `LIVE_TARGET` 时触发一次压缩；压缩成功后窗口回落到保留条数并重新累积。
- 消息带**绝对序号** `seq = dropped + 位置`（`seq_of()`），不随窗口变化漂移；历史上用的位置编号 `index` 会漂移，因此只在没有 `seq` 可用时才回退。
- 摘要作为 `<earlier_summary>` 前缀一直带着，所以旧上下文不断；两次压缩之间前缀逐字稳定，缓存才会命中。
- 压缩失败只表现为"这段历史还没被压缩"：摘要不变、消息不丢，下一轮再试。
- `HISTORY_LIMIT = 2000` 只是 deque 的安全上限，不是压缩阈值。

## 输出协议

回复 agent：

```xml
<decision>REPLY|NO_REPLY|EXIT_DIALOGUE</decision>
<dialogue>KEEP|EXIT</dialogue>
<reply>仅在 REPLY 时填写聊天正文</reply>
<reply_to>current | none | 上下文里出现过的消息序号</reply_to>
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

判定 agent（`dialogue_judge.py`，解析后得到 `JudgeVerdict`）：

```xml
<route>REPLY|NO_REPLY</route>
<topic>正在聊的话题，一句话</topic>
<topic_start>这段谈话从哪一条开始（编号）</topic_start>
<related>YES|NO</related>
```

**分工是硬的：判定决定"要不要回"，回复 agent 只决定"怎么说"。** 所以双 agent 模式下
回复用的 prompt（`SYSTEM_PROMPT_MUST_REPLY`）里**没有 `<decision>`**——它不是"引擎绕过
回复段的否决"，而是结构上就没有那个选项；`<reply>` 必须填，空手而归记成
`empty_forced_reply` 异常。单 agent 模式（未配判定）保留 `<decision>`，因为那一次调用
确实要自己决定说不说。积压消息（已经回过"稍等"的）走 judge'：一份没有 `<route>` 的
路由专用 prompt，只回答话题起点与"还在不在同一条线上"。

本地解析器只把 `<reply>` 作为 QQ 正文，并兼容旧的 `NO_REPLY`、`EXIT_DIALOGUE` 和普通文本回退。

`<reply_to>` 走的是**索引映射**而不是真实 message_id：`message_index_of()` 把历史里每条用户消息编号（`0` 起）并额外接受 `current`，模型只能在这个集合里取值。解析阶段先校验，`stage3_main` 再映射回真实 message_id 交给 `OutgoingMessage`。越界或未知的引用一律降级为不引用，因此模型无法凭空引用不存在的消息，也无法把任意文本变成出站参数。

回复带引用时，出站 `send_msg` 使用消息段 `[reply, text]`；不引用时保持纯文本，不改变既有出站形状。

## 媒体消息

`onebot_ws.extract_media_markers()` 把 `image`/`face`/`record`/`video`/`file`/`json`/`xml`/`forward` 等非文本段转成 `[图片]` 这类**本地占位标记**，最多 8 个，拼接到 text 之后。不下载、不上传、不请求任何外部资源，只让模型知道"对方发的不是文字"。

只包含媒体、没有文字的消息不会被丢弃：它用 `MEDIA_ONLY_TEXT` 占位进入正常的触发与回复流程，与纯 @ 消息的处理一致。

## 触发与会话

- `BATCH_SIZE = 20`、`COOLDOWN_SECONDS = 60.0`、`ACTIVE_TIMEOUT_SECONDS = 180.0`。
- 短期窗口：`LIVE_TARGET = 500`（压缩阈值）、`COMPACT_KEEP = 50`（压缩后保留）、`HISTORY_LIMIT = 2000`（deque 安全上限）。见"短期窗口与压缩"。
- 普通状态：20 条消息或首条等待 60 秒触发一次检查。
- **强制触发（被 @、只发媒体、私聊调试）绕过冷却**：冷却只约束非强制的批量触发。否则会出现"被叫到却因为上一轮触发的冷却而完全沉默"。
- 只发媒体的消息与 @ 同级：用户主动给 Bot 看一张图/一个表情，不应该像群聊噪声那样等够 20 条；触发原因记为 `media`，单独计入指标。
- `IdleTrigger.reset()` 同时清空缓冲和解绑冷却；显式退出对话、管理员关群或清理会话都会调用它，避免退出后残留最长一个冷却周期。
- 只发一个 @、没有文字的消息不会被丢弃，而是用占位文本进入触发与回复流程。
- 活跃对话会绑定 `active_user_id`：其他用户的未 @ 消息只作为背景，不抢焦点也不刷新活跃超时。
- 消息 ID 最多去重 4096 条。

## 运行状态持久化

`state_store.py` 用标准库 JSON 保存三类状态：群启停名单、`enabled` 开关、短期会话（会话阶段、绑定用户、短期语境、最近消息、绝对序号基准 `dropped`、压缩摘要 `summary` 与 `summary_through`）。特点：

- **原子写入**：先写同目录临时文件、`fsync`、再 `os.replace`，避免半写文件。
- **版本校验**：文件缺失、损坏、版本不符、权限不足都整体降级为默认值，只记录异常类别。
- **只保存运行状态**：不含 API Key、模型请求体或记忆库内容；单文件最多 200 个会话。
- **写入节流**：会话活跃时最快每 10 秒写一次，并在关闭时补写一次；群启停等显式操作立即写入。
- 路径由 `QQBOT_STATE_FILE` 或 `QQBOT_DATA_DIR` 决定；`QQBOT_STATE_PERSIST=0` 关闭持久化。
- 持久化**默认启用**。离线测试入口 `tests/run_offline.py` 会设置 `QQBOT_STATE_PERSIST=0` 并把 `QQBOT_STATE_FILE` 指向 `.tmp_test_run/`，因此测试不会覆盖真实的 `data/runtime_state.json`（这一点由 `tests/test_persistence_and_metrics.py` 的隔离用例守着）。它同时把 `QQBOT_DEBUG_MODEL_IO` 置 0：`.env` 里为排查注入问题开着模型追踪，测试不该把上千条合成 prompt 写进 `data/traces/`。

恢复时单条会话损坏只跳过该条，不影响其它会话。摘要与序号基准一起恢复，因此重启不会让缓存前缀白白断掉。

## 脱敏运行指标

`metrics.py` 只记录**固定类别**与耗时，不记录会话正文、QQ 号或凭据：

- `triggers`：`mention` / `media` / `threshold` / `active_message` / `private_debug` / `unknown`；
- `decisions`：`reply` / `no_reply` / `exit`；
- `model_errors`：来自 `LLMError.kind` 或异常类名的短标识；
- `send_failures`、`extension_failures`、`memory_degraded`；
- `model_latency` / `judge_latency` / `compaction_latency` / `send_latency`：固定长度滑窗的 count / avg / max / p50 / p95。

指标通过 `EngineSnapshot.metrics` 暴露，供未来 WebUI 显示；快照里另有 `model_calls`、`judge_calls`、`compaction_calls`、`reply_segments` 四个计数，便于分辨调用类型。

## 模型客户端重试

`llm_client.py` 对**限流（429）、服务端错误（5xx）和传输层抖动**做有限重试（默认 3 次，指数退避加抖动，上限 8 秒）。重试在客户端内部完成，因此一条业务消息在客户端看来仍然是一次逻辑调用；双 agent 结构下一条消息最多两次逻辑调用（判定 + 回复），压缩是偶发的第三次。

客户端支持 `user_id` 参数（`[a-zA-Z0-9\-_]+`，≤512 字符）：它把 KVCache 与调度按业务实体隔开，**在同一个账号内生效，与 API Key 无关**。对话、判定、记忆维护因此各用独立 `user_id`（`QQBOT_DIALOGUE_USER_ID` / `QQBOT_JUDGE_USER_ID` / `QQBOT_MEMORY_USER_ID`）。

不重试的情况：4xx（除 429）、响应格式错误、空回复——同样的请求不会因此变好。

实现细节：一次同步请求失败时先抛出内部的 `ModelRequestError`，携带**结构化**的状态码与已读出的响应体，`complete()` 再据此分类。这样既避免从字符串反解状态码，也避免重试时重复读取同一个 `HTTPError` 的响应流（流只能读一次，否则 body 会变成空串，丢掉唯一的排障线索）。

`LLMError` 带上 `kind` 与有限长度的 `detail`，两者都经过脱敏，可以安全写入日志；`DialogueEngine` 在模型失败时记录会话、触发原因、耗时与 `safe_summary()`。

## OneBot 传输

- 反向 WebSocket 监听 `127.0.0.1:8080`（`ONEBOT_WS_HOST` / `ONEBOT_WS_PORT`），`send_timeout` 与 `connection_timeout` 默认 15 秒。
- `call_api()` 通过 `echo` 关联回执；未连接时在有限时间内失败，不会无限挂起。
- `receive()` 在传输层关闭时返回 `None`，主循环据此结束。`close()` 会唤醒正在等待的 `receive()`，避免断连后主循环永久挂死。
- @ 识别在段形态和 CQ 码串形态下都按 QQ 号**精确匹配**，不会把 `qq=1234` 误判为 `qq=123`。
- 传输层不做去重，重复事件由 `MessageDeduplicator` 处理。
- `ONEBOT_ACCESS_TOKEN` 为空时不做鉴权校验（默认监听本机回环地址）。
- **投递失败分三类**（`transport.py`，2026-09-28）：`MessageNotDelivered`（连接不在、
  等连接超时、写入前就断——**确定没送出去**）、`DeliveryUncertain`（字节已写出但没收到
  回执，包括连接在途中断时 `_fail_pending` 抛的那一个）、`DeliveryRejected`（`retcode != 0`，
  对面不要）。分类不是洁癖：**只有第一类能安全重发**。在此之前三者都是一个笼统异常，
  "能不能重发"只能靠猜。
- **补发队列 `outbox.py`**：`_deliver_reply` 遇到 `MessageNotDelivered` 就把这条排进
  `Outbox`，runtime 的补发时钟每 `OUTBOX_TICK_SECONDS = 5` 秒试着补一次，连接一回来
  就按原顺序发出。三道上限都不能省：条数 20（满了丢最旧的）、年龄 600 秒、单条 3 次
  尝试——**断线一小时回来把一小时的回复连着喷出来，比丢消息更像事故**。第一批里第一段
  确定发不出去之后，剩下的段**不再逐条去撞 15 秒的连接等待**，直接排队（它们必然同样失败）。
  `flush` 先看 `transport.connected`（两条通道都实现了这个属性），连接没回来就直接返回，
  不去阻塞那个 5 秒的时钟。**转告与超管诊断的延迟回发也接进来了**（`_send_or_queue`）：
  转告排队时带一张便条（`note_target` / `note_text`），补发成功之后自动回发起管理员一句
  "转告已发送"——通知本身不再入队，否则套娃没完。**唯一没盖到的是重启**：队列只在内存里。
- **语境要按"她说过"记，不是按"发成功了"记**：排队中的那几段也会进 `record_sent_reply`，
  否则连接一断她的短期语境里就没有那句话，下一轮会当成什么都没说过——重复比迟到更难看。
  `send_failures` 指标计的是**真的试过几次**（排队的那几段不算）。

## 安全边界

`security.py` 在进入模型前检查涉及本机文件、进程、端口、环境变量、路径、API Key、密码、命令执行和绕过安全规则的请求。

- 普通群成员和普通管理员的敏感本机请求直接阻断，不调用模型；当前 `BLOCKED_REPLY` 为空串，属于静默拦截。
- 即使消息自称管理员，也不会获得权限。
- `ADMIN_USER_IDS` 和 `SUPER_ADMIN_USER_IDS` 当前包含 `900000001`；两层不是"权限大小"而是**适用范围**不同：**超管是全局身份**（`_is_super_admin_control` 只看 QQ 号，群里私聊都一样），**管理员是按群授权的群聊角色**（`_control_scope_ok` 要求"这个群里授过他"，私聊一律不生效）。`_control_scope_ok` **没有例外**：超管放行走的是另一条路（引用那条命令 + `/super permit` → 直接执行），不往权限判定里塞临时身份。
- 管理员授权落在 `group_admin_ids: {群号 → {QQ号}}` 上：`/super addadmin @某人 [群号]` 在哪个群发的就记在哪个群（不写群号＝当前群，私聊里必须写群号）。配置里的 `ADMIN_USER_IDS` 是部署级名单，不受按群限制。名单改动**立即落盘**（`state_store` 的 `group_admins`），配置里那份删不掉（删了重启也会回来）。
- **启停与清理不接受群号**：`/admin enable|disable|clear` 一律作用于**发命令的那个群**（`_admin_command_reply` 用 `message.target.group_id`）。旧写法（带群号）解析得出来但只回一句"群号参数已经去掉了"——既不让它掉进普通聊天被模型接一句，也不存在"点名别人的群"这条路。**超管也一样，不提供"超管可以用群号"的退路**（2026-09-27 明确否掉）：启停就是只管当前群。仍然需要目标判断的只有**往别的群发东西**：按群号/群名的转告要过 `_admin_may_manage_group`，目标群必须在他的授权范围里，越界**明确回一句**（对方本来就是管理员，静默只会让他以为 bot 卡了）。按 QQ 号的转告不涉及群权限，不受此限。
- 旧状态文件里那份没有群信息的 `admin_user_ids` **不恢复**（照搬等于把权限放大到所有群），只记一条 warning。
- **`/admin echo <文本>` 只发送文本，不解释它**：解析只在上层做一次，发出去的正文不会再被当成命令（`/admin echo /super restart` 只会把那句话发出来）。正文当成 DATA 处理：`sanitize_chat_text` 限长 200、去掉控制字符，`@` 换成全角 `＠` 以免被借去 @全体成员。它的输出**不进群历史**（和所有命令回复一样），所以 echo 也不会变成"她自己说过的话"。
- **好感度（关系）不进系统前缀、也不参与安全判定**：两个轴（亲近 0..3、防备 0..3）存在记忆库的 `relationships` 表，按 QQ 号跨群共享，渲染成 `--- 你与这个人 ---` 一段进**回复 prompt 的易变段**（放稳定段会让缓存前缀按人分叉）。
- 写入只有**一个路径** `MemoryStore._apply_affinity`，两个调用者按变化速度分工：**亲近 ← 维护 agent**（10 分钟一批，可升可降）；**防备 ← 判定 agent**（当轮，只升不降，`may_lower_guard=False`）+ 维护 agent（降要慢，7 天最多一档）。上限是机械的：单次 ±1、滚动 24 小时净变化 ±2、防备升快降慢、衰减（30 天无互动）。
- **当轮变冷的时序**是这条功能的全部要点：`_model_path` 里先读判定 → `_apply_guard_signal` 落库并失效缓存 → **再**拼回复 prompt。所以让她变冷的那句话，回复当场就是冷的。判定的 `guard_reason` 只进审计与 `/super affinity`，**不进回复 prompt**（她只该知道"现在该收着"）。
- **地板（代码保证，不是 prompt 承诺）**：被 @ / 私聊 / 被引用时判定走 `must_reply`（连 route 都不输出），好感度在那条路径上没有否决权；低好感只能让回复更短、更冷、**慢最多 12 秒**。空回复仍然计入 `empty_forced_reply` 异常。
- 聊天正文永远改不动关系值（只有两个 agent 的结构化输出能写）；高亲近也不解锁本机/凭据类请求——安全判定在模型之前，根本不看关系。
- 超级管理员仅可使用固定进程诊断入口（`/super processes`，不做自然语言匹配），不开放任意本机命令、文件、密钥或环境变量读取。
- **`/super restart` 是固定路径、无参数的重启**：引擎只立一个意图标记（`consume_restart_request`），由 `runtime` 在**回复发出去之后**执行——先落盘、关传输层（8080 得先松开，否则新进程撞端口），再 `os.execv` 换进程。它不接参数、不拼命令、不经过 shell；`-m` 启动的按模块名重启（`restart_argv`），不写死 stage3，两个阶段共用同一套。端到端演练见 `data/dryrun_restart_test.py`（另一个端口、另一个数据目录，不碰线上实例）。
- 聊天内容会被当作不可信 DATA 放入明确边界中，不能改变 system prompt 或 XML 协议。
- `sanitize_chat_text` 移除控制字符并限制单条输入；`sanitize_reply_text` 清理协议标签并把出站正文限制为 1000 字符。
- 公开命令只有两个，都在任何过滤之前处理、都不调用模型也不读写记忆：健康检查 `/yunru ping`（另一种是 @ 她 + `ping`；**裸 `ping`、`/ping`、`!ping`、`ping?`、`yunru 在吗` 都不再匹配**）与 `/help`。帮助的别名是**闭包式**的（2026-09-28，用户"总记不得要不要加 yunru"）：`/help`、`#help`、`/ yunru`、`/yunru`、`yunru help`、`/yunru help`、`#yunru 帮助`、`YUNRU HELP` 全算，判据是 `HELP_COMMAND_PATTERN`（斜杠与 `yunru` 都可选，但**裸 `help` 不算命令**，否则会把"help me""请帮助我"这类聊天吞掉）。两者都只经过消息去重，因此重复事件不会重复回复。
- 命令插件同样受会话闸门约束：`/balance` 是插件，但默认只允许超管私聊（`session_allowed` 对未配置的会话一律返回 False，属 fail-closed），也不出现在公开帮助里。
- 疑似 ping 但未匹配的消息会记 INFO 日志（`ping-like message not matched` / `ignored by session filter`），用于区分"消息没收到"和"收到了但被当成普通聊天"。
- **没权限的管理/超管形状消息不落到模型**（`privileged_command_level`）：只要以 `/admin`、`/super`、`#admin`、`#super` 开头而调用者在这一层没权限，消息就**不进历史、不进模型**。按层分别判——管理员发 `/super ...` 同样算没权限。理由：冷群消息会进历史并被 20 条巡检捡回模型，交给模型回一句话同样等于确认了这个前缀存在。2026-09-28 起收尾分两种：`/admin ...` **回一句"你没权限"**（用户明确要求，见下条；`/admin help` 更早被菜单分支接走，人人可看），`/super ...` 与不成形的文本（`/admin 随便写点什么`）仍然完全静默；私聊里也静默（管理员命令在私聊本来就不生效）。
- **无权限说明与"引用即执行"放行**（`_denied_admin_reply` / `_permit_reply` / `_run_admin_command`）：引擎把被拒的 admin 命令**整条消息**在**内存**里记 5 分钟（`_denied_admin`，键是 `message_id`——引用给的正是消息 id；不写库、不写日志正文）。超管**引用**那条消息发 `/super permit` → 引擎按原样执行那一条，回复就是命令自己的结果，执行完立即从表里删掉（一次即消）。三条硬性质：① 放行的对象是**那条命令**，不是那个人的身份——不产生任何可复用权限，`_control_scope_ok` 里没有例外；② 普通路径与放行路径共用 `_run_admin_command`，唯一差别是 `check_scope=False`（越界与否由超管自己看过原文决定），因此两者行为不会漂移；③ 放行不跨群（必须在命令被发出来的那个群里），过期就得重发。与 AGENTS.md 的"未授权时静默回落"有张力，`QQBOT_ADMIN_DENY_NOTICE=0` 可退回静默（记录与放行不受影响）。
- `/admin help` 是**公开菜单**（用户 2026-09-28 要求"普通成员可以呼叫 /admin help 调起 help 菜单"）：任何人发 `/admin help` / `/admin` / `/admin 帮助` 都拿到 `ADMIN_HELP`，没权限的人额外拼上 `ADMIN_HELP_GUEST_NOTE`（写明"执行仍需权限 + 把命令发出来再请超管引用放行"）。它仍然是引擎直接回复——不进历史、不调模型、不授予任何权限。`/super help` 不在这一条里，只给超管。公开帮助（`builtin_commands.HELP_FOOTER`）同步点名了这两个入口，但**不搬菜单本身**（`/admin enable` 之类仍不出现在 `/help` 里）。旧的 `#bot` 前缀已彻底移除，避免两份命令清单漂移。

## 扩展边界

`extensions.py` 提供三个可替换端口：

- `Stage3Plugin`：为当前调用提供有限长度的补充 DATA，并可观察决定结果；不能修改安全判断、会话状态、回复目标或出站消息。
- `KnowledgeBase`：按当前消息检索有限条 `KnowledgeItem`；知识内容与聊天内容一样只放入不可信 DATA。
- `ExtraPromptProvider`：提供额外 prompt 材料；不能覆盖固定 system prompt。

三者均有数量/长度上限；`stage3_main.py` 对 `collect` 与 `notify` 各施加 2 秒整体超时，异常会降级为空扩展材料，不阻塞主循环。注意收集是串行的：单个插件卡住会连带跳过知识库与额外 prompt。

## 长期记忆

- `memory_inbox.py`：有界异步捕获队列（512），按 `sha256(group:session:user_id:message_id:speaker)` 幂等；敏感或超长内容直接丢弃。
- `memory_store.py`：本地 SQLite（WAL），租约、幂等提交、墓碑删除、按群 inbox 配额、重放表与保留期清理；另有四张**只加不改旧数据**的表（schema v4）：`relationships` / `relationship_log`（好感度）与 `record_subjects` / `people`（记忆↔人的对应、本群见过的名字，见 `docs/MEMORY_PEOPLE.md`）。
- `memory_maintenance_agent.py`：独立后台模型调用，单实例锁，每轮最多 `max_batches` 批，异常立即退避。
- `memory_service.py`：Stage 3 读取（0.3 秒超时，失败降级为空记忆）、捕获、发送确认与生命周期。

记忆作用域：`user_global` 按 QQ 号跨群共享，`user_group` 按 `group_id + user_id` 隔离，`group` 按 `group_id` 隔离。Agent 自主决定 ADD/UPDATE/MERGE/DELETE/IGNORE，但不能修改人格、知识库、安全规则或 system prompt。

**写入的三道机械兜底**（都不依赖模型自觉，2026-09-27 体检后补齐）：

- **近重复**：同 scope 内词元重合 ≥ `NEAR_DUPLICATE_RATIO`（0.4，阈值是量出来的）→ 不新增；
- **每日配额**：每群滚动 24 小时最多新增 `DEFAULT_DAILY_ADD_LIMIT`（8）条，之后只允许改已有事实；
- **关于系统自身的直接拒绝**：`SELF_REFERENCE_RE` 命中即拒（`rejected_self_reference`，理由进 audit）。
  真库里曾有 6 条"看机器人反驳""该条在 prompt 中被列为不愿提及内容""修机器人上下文问题"
  "测试过用显示替换…"活着——这类内容每轮被检索回来等于反复提醒她自己是程序（AGENTS.md §2.2）。
  正则**不含"云茹"**：本群记忆大量是"某人对云茹的设定感兴趣"，按名字拦一次会砍掉 19/50 条。

**TTL 收口**：`ttl_days` 由维护 agent 填，存储层按 `TTL_CAP_DAYS` 夹上限（preference ≤365、
group_fact ≤180、name/boundary ≤3650），并让带时间限定的处境（`STATE_MARKERS`：最近/目前/
打算/这周…，`STATE_KINDS` 三类）一律 ≤ `STATE_TTL_DAYS`（30）——哪怕模型填了 null。
由来：真库 22 条带过期的活跃记忆从 +30 天铺到 +3648 天，"有工学椅挺好睡""最近觉得消费
有点高"拿到 1 年甚至 10 年。**注意记录的生命周期与 `MemorySettings.retention_days`（7 天）
无关**，那个只用于 inbox/receipts/replays。

**检索（`MemoryStore.retrieve`）**：

- 候选 = 本人（`user_group` + `user_global`）+ 本群（`group`）+ 被提到的人在本群学到的记录（见下）；同 key 的群内记录盖住全局；
- **本人的 `name`/`boundary` 常驻**：不靠词命中，每轮都在（"我记得你是谁、你不喜欢什么"
  不该取决于这句话里有没有同一个词），最多 2 条，排在按词命中的记录之后；
- 其余按词命中数排序（本人记录优先、再看 confidence/updated_at），总共最多 5 条；
  一个词都没命中就不注入（旧实现用"最近更新的几条"兜底，等于把无关记忆塞进她嘴里）；
- `WEAK_TERMS` 里的关系词/填充词不参与命中。**这不是按频率砍**：实测误命中的是
  「群友」（全库 2 条）、「是否」（2 条）、「评价」（2 条）——频率低但零话题信息，
  而正确的命中靠的是「喜欢」（全库 12 条）。按频率砍会砍掉对的、留下错的。

**两级记忆（v5，用户 2026-09-28 决定）**：`records` 拆成 `memory_short`（中短期）与
`memory_long`（长期）两张表。**所有语句按表名参数化**（`TABLES` / `TIERS`），隔离、删除、
归档、TTL 夹取都只有一份实现——抄两遍必然漂移。

- **进**：新事实默认落中短期（默认 14 天，群事实 30 天），`status` 类 7–14 天且**永不强化**；
- **强化 → 长期**：称呼/边界写入时直接进；维护 agent 标 `durable` 直接进；
  同一条事实被**后来的对话再确认过一次**（`revision>=2`）→ 写入时进长期，
  历史遗留由 `promote_reconfirmed()` 在每轮维护里机械搬一遍（不问模型、不花钱）；
- **弱化 → 到期**：没被强化的中短期记录 TTL 到期即进 `archive`（30 天快照，路径不变）；
- **检索**：命中数 → 说话人 → **长期优先** → 置信度 → 时间；本人称呼/边界仍是常驻项，
  按设计排在按词命中的记录之后；
- **归档与墓碑与层级无关**：`archive` 按 record_id、`tombstones` 按事实键，两边共用。

**「顺带一提」（善意的追加提醒）**：`status` 类事实（正在发烧、明天有考试）由维护 agent 写，
只活在中短期。引擎在**这一轮本来就要开口**、且**判定说话题已离开那件事**（`related=NO`）
或那条状态已 ≥1 天时，把它作为一行提示放进回复段的易变段，让回复 agent 自己决定提不提、
怎么说；发出后由 `MemoryStore.mark_reminded` 记下——**一条状态只提醒一次**。
（`memory_service.care_note` / `mark_care_noted`；提示文案里写死了"一句就够、不追问、不重复"。）



- 每条记录都有关联人：`records.subject_user_id` 管隔离，`record_subjects(record_id,user_id,group_id)`
  管归属与召回，其中 `group_id` 记的是**这条事实在哪个群学到的**；
- 维护 agent 可以给 ADD/UPDATE/MERGE 一个可选的 `subjects`（QQ 号，只能从随批次给的
  `people` 名册里照抄）；存储层核对不上就丢掉，`name`/`boundary` 机械地只挂归属人，
  `group` 作用域不挂关联人（本群公共事实人人都看得到）；
- 检索候选 = 说话人命名空间 ∪ 本群 `group` ∪ **本条消息提到的人**（@ 或引用回复的目标，
  `DialogueEngine._mentioned_people`）在**本群学到**的记录。跨群不借：在 A 群知道的事，
  不会因为 B 群有人提到他就被说出来。不在正文里做人名匹配（昵称会改、会重复）；
- 渲染成 `<memory scope="…" subject="QQ" about="乐乐">正文</memory>`：`about` 是名字，
  查不到才退回号码，名字来自 `people`（本群消息的 `sender_name` 顺手记下）。


**判定 agent 的最小记忆**：判定 prompt 有"必须远小于人格 prompt"的硬约束，塞不下整块记忆，
所以只把**当前说话人的称呼与边界**（≤2 条）经 `MemoryMaterial.as_judge_note` 放进 user 段的
DATA 区（`identity` 参数）。它不进 system 前缀——那是易变内容，进去会让缓存前缀按人分叉。

单批最多 40 条事件而模型每轮最多 5 个操作，因此未被引用的事件会写入重放表获得一次重放机会；模型显式 `IGNORE`（整批无证据引用）的批次直接归档为已读，不重放。重放事件在收尾时会解除租约，避免永远挂在已删除的租约上。

## 谁在跟谁说话（prompt 里的身份标记）

两个 agent 看到的是**同一套**标记，2026-09-28 补齐（用户报"把群里不同的人认作一个人"）：

- `who="2"` → `<people>` 名册里的编号。**每条用户消息都写 who**（原来"同一个人连着说
  就不重复写"省字符，代价是模型得自己把身份顺下去，一顺错就并成一个人）；
- `at="2,yunru"` → 这一条 @ 到了谁（`yunru` 是她自己）。`at` 段以前在解析层就被丢掉，
  群里"@了别人"的消息递到模型手里和普通消息一模一样；
- `reply_to="3/1830"` → 引用的是"3 的第 1830 条"。以前渲染的是 OneBot 原始消息 ID
  （`reply_to="-839993309"`），而历史里按 seq 编号——模型根本对不上，
  于是"引用别人的消息"和"回复你"在它眼里一样；
- `main_partner=2` 给名册编号而不是 QQ 号（号码要二次查表，容易认错人）。

**话题锚不发霉**：判定每轮都读到 `<topic>` 与 `<related>`，所以
`DialogueEngine._refresh_context_from_verdict` 用它刷新 `state.context.topic`；
判定说 `related=NO`（这一句不在原来那条线上）时，`pending_question` 一律作废。
由来：回复段自报的 `topic_status` 35 轮里只有 1 次是 `shifted`，而引擎当时只信回复段、
且只在 shifted/ended 才清未了问题——实测"体温量了吗"在群里已经聊到"明天吃什么"之后又问了四轮。

## 知识库（世界观语料）

`knowledge_base.py` 实现 `extensions.KnowledgeBase`，语料是 `docs/yunru-source/`（**原文一字不改**）。

- **收录白名单**：`02-核心设定`、`03-官方设定补充`、`05-同人系列原文`；
- **排除清单**（写在 `EXCLUDED_DIRS`，不靠模型自觉）：`01-Bot人设`（那就是我们自己的
  system prompt，喂进去等于让她读自己的指令）、`09-废弃与趣闻`（废弃设定，人格里已排除
  早期口径）、`07-知乎参考`（对她的批判与版本演变分析）、`06-密文与链接`（解密元信息）、
  `04-制作组背景`（戏外幕后）；
- **切块**：按 markdown 标题切，超长按 `MAX_CHUNK_CHARS` 断开；导出页噪声（署名、
  "编辑于 …"、"收录于文集"）丢弃；每块带 `source`/`title`/`lines` 便于回到原文；
- **构建笔记不进 prompt**（2026-09-28）：语料里混着写给我们自己的段落——标题写着
  `对 Bot 的影响`／`用于 Bot 人设构建`／`可用于 Bot 对话的特征`，正文写着
  `是构建人设的最高优先级参考`／`| 要素 | 内容 | 对人设的启示 |`，甚至整段
  "云茹 bot 应体现以下核心性格维度"。三类问题同时成立：把机制词喂进 prompt、
  在 DATA 里下指令、绕过 `base_prompt.py` 这个人设唯一来源。**处理分两级**：
  `_PERSONA_NOTE_RE`（人设/性格分析/构建）的段落**整段丢**（实测 36 段），
  其余段落**逐行**剔掉带机制词的行、标题里的批注片段也剔掉（`_clean_heading`，实测 29 行）。
  **为什么不是整段丢**：用户指出某条"被囚禁地 ≠ 住处"的纠正时顺带发现的——那一节里
  除了性格分析，还夹着**事实**（她现在在哪、哪些地方属于过去）。整段丢等于把
  "她现在在哪"的唯一来源一起扔了。`is_meta_note()` 仍在**检索**侧生效，挡旧索引。
  排除后 24 题基准不变（官方优先 top-1/3/5 仍 100%）；
- **改知识库或人设之前，先看事实表**（2026-09-28 加）：语料的权威顺序（官方原文 >
  考证整理 > 同人叙事）、她现在的处境与关键关系、以及**不许当事实的东西**都在那一页。
  由来：我按零散片段写检索词表、把某个"过去的地方"当成"住处"，被用户当场纠正；
  用户随后要求"多了解全貌再维护知识库"。词表、过滤规则、人设改动都对照它写，
  改动后跑 `data/knowledge_tool.py eval`（24 题）与 `data/knowledge_self_eval.py`
  （自问／换词／无关／**事实核对**四组，事实核对应当 100%）。
  **事实表本身不进公开仓库**（系从第三方素材摘出），本地放 `data/private_docs/YUNRU_FACTS.md`；
- **查询扩展按事实写**（`QUERY_EXPANSIONS`，2026-09-28）：语料里她的处境是
  几个专有地名与阵营名，群里问的是"你现在住在哪／在忙什么"，词面一个都撞不上。
  所以人工列同义词：**"住/在哪"那一组**扩到"她现在的位置"那几个词；
  **一个属于过去的地名**扩到"囚禁/被囚/实验室"（**用户当场纠正过："那是囚禁地，不是住处"**，
  测试 `test_relocation_question_expands_to_alaska_not_bamiyan` 锁死这条）；
  `家人`／`多大`／`怕`／`造` 各一组。命中扩展词的块在排序里单独占一档
  （`_expansion_hits`），否则扩展只是"多召回一些"，答不对题的泛泛条目还是排前面。
  实测：自问 20 条里官方口径命中 50% → **85%**、top-1 提到她 60% → **90%**；
  换词组（词面故意撞不上的说法）期望关键词 top-1 **80%** / top-5 **90%**；
- **候选池要给官方口径留位置**（2026-09-28，踩过）：`_match` 只按总 bm25 取前 N 条时，
  885 KB 同人会把 48 KB 官方设定挤出**候选池**——排序键再对也救不回来
  （实测「心灵控制是什么」掉出 top-3）。现在总候选之外**单独再取一档 authority=3 的候选**；
- **资料一律当"过去的事"**（2026-09-28，用户报的问题："总是把过去的事情当成现在正在发生的，
  比如说自己住在地下设施、在给军队造武器"）：检索回来的本来就是很多年前的经历与别人的记述，
  所以 `KNOWLEDGE_TIME_NOTE` 会拼在 `参考资料` 段最前面，说明这些发生在过去、不是眼下的处境、
  要提就用「那时候」；只在**真的有知识条目**时才拼（没有资料时是噪音）。
  人设正文里对"过去与现在"有专门一段（现在在哪、哪些属于过去），两边必须一致。
  规则必须落在可信前缀里，不能只出现在资料段。**第一版修错过方向**：当时写的
  "不再属于任何部队，也不在任何设施里"把她现在的处境抹掉了，用户当场纠正后删掉，
  `tests/test_prompt_regression.py` 里锁死"不许回来"；
- **检索**：FTS5 + **trigram** 分词（`unicode61` 会把整段中文当成一个词元）。查询侧
  按 **3 字滑窗**切词——曾经把整句当一个词，FTS5 当短语匹配，实测 0 召回；两字问句
  （"云茹"）走 LIKE，且正文与标题都查；
- **官方优先**：`authority` 列按目录给权重，排序键是
  `(authority DESC, 提到她 DESC, bm25/命中数)`（2026-09-28 加的中间那档）。
  基准实测（24 题）：官方优先 top-1/top-3 = 100%，纯相关度 top-1 96% 且 top-1 落在官方口径的
  从 20/24 掉到 16/24——885 KB 同人会淹没 48 KB 官方设定；
- **"问她自己"要给她自己的资料**（2026-09-28，用户报的问题："总是把过去的事情当成现在正在
  发生的，比如说自己住在地下设施，又如说自己在给军队造武器"）：原来的召回会端上**别人在别处
  的场景**（加尔各答撤退、1985 哈萨克斯坦）。三处改动，实测 `data/knowledge_self_eval.py`
  20 条自问：提到她 55%→**100%**、top-1 提到她 20%→**100%**、召回为空 5→**0**；24 题基准不退。
  1. **2 字窗口进 LIKE 兜底**：`_query_tokens` 除了 3 字滑窗（FTS 用）再切 2 字窗口，
     短问句的内容词常常就是两个字（"家人""喜欢"）；`_like` 从"所有词都要命中"改成
     **OR + 命中数打分**——原来 AND 起来要求"你有/有家/家人"同时出现，实测 0 条；
  2. **同级里先给提到她的**：`_rank_key` 在权威度**之内**、bm25 之前插一档 `_mentions_her`
     （正文或标题路径里有"云茹"）。刻意不放到权威度之前——那会让同人叙事把官方设定顶掉，
     正是当初加 `authority` 要修的问题；
  3. **兜底她的档案**：问的是她（`SELF_QUERY_RE`：你/您/云茹/自己）却一条都没提到她时
     （"你是谁""你怕什么"这种极短问句），补 2 条官方口径里提到她的块。它是保底不是主力。
- **向量检索这条路试过了，暂时不上**（2026-09-28）：`embeddings.py`（本地 ONNX，延迟导入，
  缺库就 `is_available()==False`）+ `vectors` 表 + `search(..., embedder=…)` 都在仓库里，
  但 **`runtime.build_knowledge_base()` 不传 embedder**，所以线上是纯词面。理由是三张实测：
  `bge-small-zh`(24M int8) 查询 20ms 但相关/无关余弦**完全重叠**（两边都 0.44~0.65）；
  `bge-large-zh`(326M int8) 相关 0.427~0.502 vs 无关 0.388~0.450 **仍然重叠**，CPU 编 734 块
  要 **66 分钟**；`bge-reranker-base`(279M int8，交叉编码器) 分数**有区分度**
  （世界观 +2.7~+3.2，无关 ≤+0.5），但 **17~24 秒/次查询**——只能在显卡上跑。
  融合用的是 RRF（两路分数不同量纲，不能直接比大小），`_rank_key` 里"提到她"那一档
  仍在名次之前（降级过一版，词面自问从 100% 掉到 60%，已改回）。
- **要不要翻资料由判定决定**：判定输出 `<lore>YES|NO</lore>`（"这一句在问她的世界吗"），
  引擎只在 YES 时调 `knowledge_base.search`（`PromptSources.collect(knowledge_enabled=…)`）。
  词面门槛做不到这件事——实测"任一词命中"召回 100% 但 79 条真实闲聊误召回 60%，
  收到"≥2 个词"召回只剩 54%；语义门 92% / 0%（`data/lore_gate_eval.py`）。
  解析不出 `lore` 时按 YES 处理（退化成"门不存在"，而不是再也翻不到资料）；
- **端口是 `async def search`，必须适配**（2026-09-28 修的静默失效）：`KnowledgeIndex.search`
  是同步函数，而 `extensions.KnowledgeBase` 与 `PromptSources.collect` 走的是 `await`。
  直接返回索引对象时 `await` 一个 list 抛 `TypeError: 'list' object can't be awaited`，
  被 `collect` 里的 `except Exception` 吞掉 → **知识库在生产里一次都没生效过**
  （`data/bot.err.log` 里 4 次 `Stage 3 knowledge lookup failed`）。离线基准绕过了这一层
  （直接调 `KnowledgeIndex.search`），所以 24 题全绿而线上是空的。现在 `runtime` 返回
  `AsyncKnowledgeBase(KnowledgeIndex(path))`，`tests/test_knowledge_base.py` 里有一条测试
  拿 `runtime.build_knowledge_base()` 的返回值去 `await`，专锁这个接口；
- **边界**：只经 `PromptSources` 进 user 段的 `KNOWLEDGE DATA`，单条 ≤3000 字、每轮 ≤5 条；
  索引缺失/查询异常一律降级为空，不影响对话；**没有条目时不写 BEGIN/END 空壳**（门开了
  却召回不到是常态，那两行只是噪声）；
- **构建是离线的**（`data/knowledge_tool.py build`），运行时不解析语料——启动路径上不该有
  1 MB 文本的解析。删掉索引文件即等于关掉知识库（`runtime.build_knowledge_base`）。



多个群同时开着的时候，**同一时刻只有一段对话在当值**（`focus.py` 的 `FocusController`）。
规则与理由见 `docs/MULTI_GROUP_FOCUS.md`，这里只说实现落点：

- **门控在引擎里**：冷群的消息到达即进该群历史（只作背景、不调模型）；"明确找她"的三类
  （@ / 提及 / 引用）进队列，@ 还会先回一句随机的"稍等"（同群 120 秒冷却，进历史、
  不进长期记忆、不算连回计数）。
- **释放与切换在时钟里**：`DialogueEngine.tick()` 由 `runtime._message_loop` 的第二个任务
  每 5 秒调一次。三条释放路径——话题 45 秒没继续（判定顺带返回 `<related>`）、连回满 50 条
  （交代一句再走）、当值满 5 分钟（静默走）。**只有真的要切走时才把当值群这一段压进摘要**。
  时钟只在**没有当值群**时才轮到队列里的下一个。
- **排队消息不重复进历史**：它们到达时已经记过、也已经过消息去重；轮到时用
  `_resumed_ids` 标记跳过这两步，再走一次正常处理。
- **答应过的必须给结果**：回过"稍等"的消息，判定不许否决（`_promised_ids`）。
- **不做频率刹车**（2026-09-28 用户决定）：她进入话题后每条都过判定，引擎不压任何一句；
  想调频率只能调判定依据。回归测试 `test_hot_group_keeps_deciding_every_message_no_frequency_brake`。
- **话题锚**：判定每轮返回当前话题起点 `seq`（只许前进），回复段与判定段都只带
  起点之后的消息；压缩后启用新起点。说话人用 `<people>` 名册 + `who` 短编号。
- **缓存隔离**：每个会话一个 `user_id`（`dev_config.session_user_id`），
  对话与判定各一套；记忆维护保持全局，跨群共享不受影响。

## 未来 WebUI 控制端口

`control.py` 定义了 `Stage3Control`、`EngineSnapshot` 和 `SessionSnapshot`。`DialogueEngine` 已实现读取不含 API Key 和完整聊天历史的脱敏运行快照、启停消息处理、清除指定短期会话，以及接收/忽略/阻断/延迟/模型调用/判定调用/压缩调用/回复统计。

这只是业务控制端口，不是 Web 服务或认证层。未来 WebUI 仍必须自行实现认证、CSRF 防护、访问来源限制和敏感操作审计。

## 观测：日志、对账、概览

三者分工固定，别互相混：

| 用途 | 入口 | 内容 |
| --- | --- | --- |
| 事后查"她当时看到/回了什么" | `data/logs/{judge,reply,memory,security,mail}.jsonl` | 每个功能各一份，各留最近 1000 次完整输入输出（`feature_log.py`） |
| 对账（成本） | `/super apicheck` | **按 key** 的命中率（回复/判定/记忆三条独立口径）+ 账户余额；主口径是**从开始使用到现在**（跨重启），本次启动的数字只附最后一行 |
| 概览（现在正常吗） | `/super status` | 本次重启后的运行时长、内存占用、计数与日志大小（另有一行**全时累计**）；**不含命中率与余额** |

- **跨重启的账本**（`api_usage.py`，2026-09-30 用户要求："默认展示从开始使用到现在的，而不是重启后的"）：以前这些数字只活在进程内存里（`OpenAICompatibleClient` 的用量、`engine._stats`），一重启就归零。现在落一份 `data/api_usage.json`，两块内容两种写法：**API 用量**每次调用累加（带节流落盘）；**引擎计数**存的是"之前几轮进程的累计"（baseline），展示时 `baseline + 本次`——这样不必去改引擎里每一处 `_stats[...] += 1`，结账点只有 `persist_state()` 一处。`QQBOT_STATE_PERSIST=0`（测试与干跑）时纯内存，不落盘。

- `feature_log.py`：正文只落盘，内存里只有计数与 20 条预览——1000 条 × 4 个功能 × 几十 KB 全放内存会上百 MB。文件留 `capacity..capacity*2` 行，超过就重写成最近 `capacity` 条（顺序读一遍，内存里只留尾部若干行）。`request_parts()` 会把**所有** user 段收进来：回复请求是 system / 稳定段 / 易变段三段，旧实现只记第一段。
- **命中率必须按 key 分开**：判定与回复的 prompt 形状完全不同，合成一个数既看不出谁在退化，也会掩盖"记忆的 key 一次都没命中"。`cache_report()` 分 dialogue / judge / memory 三栏，靠 `engine.memory_client` 这个引用把记忆维护的用量摘出来。
- **一场交谈结束（`<dialogue>EXIT</dialogue>`）时自动对一次账**并记日志：这既不像每轮那样吵，也不像定时那样可能永远等不到。
- 内存数字用标准库取（Windows `GetProcessMemoryInfo`、Linux `/proc/self/status`）。**ctypes 必须声明 argtypes/restype**：不声明时 HANDLE 被当 32 位传，函数返回 FALSE，界面上永远显示 `—`。

## Stage 4：每日汇报（唯一一条已实现的 Stage 4 路径）

- **触发者是时钟，不是她**：`runtime._report_loop` 每分钟看一眼 `DailyReporter.due()`，
  到点就写一封。它**不复制主循环**，和焦点时钟、维护时钟并列。
- **判据是"今天 23:00 过了没有 + 此后没发过"**，不是"距上次满 24 小时"：后者一旦补发
  （23:00 关机、次日 01:00 开机）就会永久漂到 01:00，"每晚十一点"就废了。
  素材窗口则是"距上次汇报到现在"，所以 23:00–24:00 那段不漏也不重。
- **收件人是配置里的常量**，不从对话/记忆/邮件内容里取——它连"地址从哪来"这个问题都没有。地址本身也**不进她的 prompt**（`REPORT_PROMPT` 只说"写给他、会送到他邮箱里、他不会在信里回你"）：她说不出一个自己不需要知道的地址，也就不会被问出来。
- **她记得自己写过什么**（2026-09-28 修）：写信是独立一次调用，信原本不进她的对话侧，于是她"不记得自己邮件发了什么"。现在发成功之后信留一份在 `mail_state.json` 的 `letters`（`MAX_LETTERS=5`），runtime 启动时用 `engine.note_letter()` 灌回引擎。`_letter_note()` 决定这一轮递不递给她：**收信人本人**（部署级管理员 ∪ 超管，刻意不用 `all_admin_user_ids()`——按群授权的人不是收信人）且（刚寄出 ≤ `LETTER_FRESH_HOURS`=36h，或他问起信/邮件且 ≤ `LETTER_RECALL_DAYS`=30 天）。渲染在 `stage3_runtime._letter_block()`（易变段、DATA、只留 600 字），并写明"他没提就别主动搬出来、别在群里念"。信的时间戳是**墙上时间**，所以判年龄用 `time.time()`，不能拿引擎的单调 `self.clock` 去减。
- **素材只有客观事件**（计数、QQ 号、档位、判定给的人话依据）。**不喂聊天原文**：
  汇报是给主人看的，但群里的话是群友说的。
- 人设仍来自 `base_prompt.py`（可信前缀），素材进 DATA；`REPORT_PROMPT` 过机制词扫描。
- 发送走 `mail_client.py`：正文写文件（argv 里只有收件人与主题）、主题按最严规则
  卡掉 cmd 元字符（Windows 上 `.cmd` 会再过一遍 cmd 解析）、失败分类而不是抛裸异常。
- **没装 CLI / 没授权 → 整条路径静默关闭**，对话照常。这是"随时能迁移"的关键性质。

## 当前审计发现（未修复）

- `onebot_ws.py` 的 `_messages` 是无界队列，且传输层无背压；极端情况下依赖上层 4096 条去重窗口。
- `llm_client.py` 没有连接复用，每次调用都新建 TCP/TLS 连接；重试已实现。
- 普通消息默认不传 `max_tokens`，输出长度依赖服务端默认值（记忆维护调用已显式限制为 4096）。
- `admin_control.py` 的 `relay` 别名在命令正则中不可达，`/admin relay 123456789`（缺内容）会解析为普通聊天。
- 放行窗口（`_denied_admin`）只存在**内存**里：重启即失效，也不会跨进程生效；被拒的命令只留 5 分钟，超管没在那之前引用就没了（得让对方重发）。命令原文（可能含转告正文）也在这 5 分钟里短暂留在内存，不写库、不写日志正文。
- 她的信只留在 `data/mail_state.json`（最近 5 封），**不进记忆库**：没有 TTL、不参与检索、也不进 `/super memory`。早于该功能的历史汇报要用 `data/backfill_letters.py` 从 `data/logs/mail.jsonl` 补；**旧版本进程**（不认识 `letters`）在 23:00 发信时会把这一字段整段写掉，重启后重跑一次那个脚本即可。
- **知识检索仍是词面匹配，不是语义检索**：2026-09-28 那一轮把它修到"问她自己时不再端别人的场景"（见上），但**换个说法问同一件事**（"你住在哪"↔"你的住处"）仍然只能靠词面撞上，问句越长越容易只命中虚词。真正的解法是语义检索（embedding）或让模型挑，不是继续调阈值；`data/knowledge_self_eval.py`（20 条自问）与 `data/knowledge_tool.py eval`（24 题）是这两件事的度量。
- `runtime_diagnostics.py` 用 UTF-8 解码中文 Windows 的 `tasklist` 输出，进程名可能乱码；28 行输出常被 1000 字符上限截断。
- `security.py` 的敏感请求判定要求 `sender_role in {"admin","owner"}`，私聊事件通常没有 role，会把私聊管理员判为普通用户。
- 长期记忆打分仍受每命名空间 `LIMIT 100` 的候选窗口限制，不是全局 Top-5；`last_used_at` 字段暂未参与淘汰。
- 记忆检索是**词面子串匹配**，不是语义检索：42 次真实回复里只有 6 次命中过词面重叠的记忆（命中还常靠"喜欢""群友"这种词）。要提高召回得换 embedding 或让模型选，不是调阈值能解决的。
- 已有记录不会被新策略回溯处理：`TTL_CAP_DAYS` 只作用于之后写入的记忆，历史记录要改得靠 `data/purge_memory_records.py` / 人工（演练脚本 `data/ttl_dryrun.py`）。
- 持久化保存的是**明文 JSON**：它只包含运行状态而非凭据，但如果机器上有其他本地用户，应把数据目录的访问权限收紧到只有运行账号可读。

## 后续扩展顺序

1. 按 `LONG_TERM_MEMORY_PLAN.md` 做真实 QQ 小范围验收，重点观察 Agent 误记、遗忘延迟、跨群误用率和维护成本。
2. 实现 WebUI 前先补认证、CSRF、访问范围和审计设计。
3. 落实 `DEFECT_VERIFICATION.md` 中标注"需要决策"的取舍项。
4. 观察媒体占位标记的实际效果，再决定是否接入真正的图像理解能力。
5. 为 `llm_client` 增加连接复用与按错误类别细分的超时策略。
