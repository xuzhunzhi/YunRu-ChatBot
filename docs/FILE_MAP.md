# 逐文件接口与调用（自动生成）

> 生成命令：`.\.venv\Scripts\python.exe data\tmp_file_apis.py`
> 原始输出：`data\tmp_file_apis.txt`（1381 行，60 个文件）
> 三列含义：**提供** = 公开的类/函数/常量；**被谁用** = 谁 import 它并取走了什么名字；
> **用到** = 它从别人那里取走了什么名字。下划线开头的不算公开接口。

---

## 一、四个接口文件（契约在这里）

### `transport` —— 引擎与传输层之间（被 18 个模块依赖，最热）
```
提供   MessageTarget            消息目标（群/私聊）
       IncomingMessage          进来的消息
       OutgoingMessage          要发出去的消息
       display_name_from_sender()   显示名（QQ 昵称优先，群名片兜底）
       MessageNotDelivered / DeliveryUncertain / DeliveryRejected   投递失败三态
       QQTransport → receive(), send(), send_typing()   传输层协议
用到   （不依赖包内任何模块）
```
被谁用（取走什么）：
`onebot_ws` `onebot_client` `outbox` `runtime` `stage3_main` `stage3_runtime`
`conversation_context` `security` `attention` `focus` `trigger` `dialogue_judge`
`dialogue_compaction` `extensions` `memory_inbox` `command_plugins`
`builtin_commands` `builtin_balance_command`

### `command_plugins` —— 插件命令契约
```
提供   CommandPlugin → match(), session_allowed(), handle(), help_text()
       CommandRegistry → resolve(), run(), dispatch(), help_lines()
       ActionRequest    插件只能"声明意图"
       ImageReply       插件只能"声明这份内容适合画成卡片"
       LEVELS, level_of()
用到   → transport: IncomingMessage, MessageTarget
```
被谁用：`builtin_commands`（`CommandRegistry`、`ImageReply`）、
`stage3_main`（`ActionRequest`、`ImageReply`、`level_of`）

### `background_plugins` —— 插件节拍契约
```
提供   BackgroundPlugin → poll_once(), close()
       run_background_plugin()      唯一的节拍循环
       build_background_plugins()   唯一的装配入口
       plugin_enabled(), FIRST_TICK_DELAY_SECONDS, MIN_INTERVAL_SECONDS
用到   （不依赖包内任何模块）
```
被谁用：`runtime`（三个都取）

### `_host` —— 引擎与"宿主能力"之间（这次新建）
```
提供   OutboundStyler → split(), delay_plan()        出站表现（分段与停顿）
       CardRenderer  → available(), render()          出站渲染（帮助卡片）
       MachineProbe  → sample()                       这台机器（进程/网卡/风扇）
       AuditSink     → record(), tail()               审计
       BalanceSource → query()                        余额
       RoleLookup    → role(), self_id()              她自己的群角色
       KnowledgeIndex→ search()                       知识检索
       KnowledgeHit, MachineSample                    值对象
       HostServices                                   装配点交给引擎的一包
       NoStyler / NoCards / NoMachine / NoAudit       空实现
用到   （不依赖包内任何模块）
```
被谁用：`_host_adapters`（`HostServices`, `MachineSample`）、`stage3_main`（`HostServices`）

---

## 二、底层（服务跑起来 + 管这台机器）

| 文件 | 提供 | 用到 |
| --- | --- | --- |
| `runtime` | `serve()` `build_engine()` `build_context_provider()` `apply_overrides()` `state_persistence_enabled()` `_USAGE_STORE` | `stage3_main`(引擎类与常量) `transport` `llm_client` `capabilities` `state_store` `api_usage` `outbox` `memory_service` `memory_ops` `memory_config` `conversation_context` `extensions` `style_reviewer` `background_plugins` `_host_adapters` `prompt_library` `operator_config` `runtime_flags` + 函数内：`control_audit` `provider_registry` `qq_roles` `vision` `knowledge_base` `knowledge_operator` |
| `dev_config` | 全部环境变量常量与 `data_dir()` `load_env_file()` | 被 11 个模块顶层依赖 |
| `llm_client` | `OpenAICompatibleClient` `LLMError` `apply_client_overrides()` | 被 `runtime` `stage3_main` `dialogue_judge` 等 |
| `capabilities` | `CapabilityRegistry` `CapabilityDenied` `Capability` `load_catalog()` + 6 组 action 名单 | （不依赖包内） |
| `onebot_ws` | `OneBotWebSocketTransport` | `transport` `dev_config` `metrics` `llm_client` |
| `onebot_client` | `SnowLumaHttpClient` `call_channel()` `unwrap_result()` | `transport` |
| `state_store` | `RuntimeStateStore` `state_path_default()` | `dev_config` `data_dir()` |
| `feature_log` | `FeatureLogs` `logs_directory()` | `dev_config` |
| `metrics` | 计数器与耗时分布 | （不依赖） |
| `control_audit` | `ControlAudit` `token_fingerprint()` `default_path()` | `dev_config` |
| `api_usage` | `ApiUsageStore` `default_path()` | `dev_config` |
| `snapshots` | `EngineSnapshot` `SessionSnapshot` | （不依赖） |
| `control` | `EngineControl` Protocol（re-export 上面两个快照） | `snapshots` |
| `operator_config` | `SETTINGS` `ConfigRejected` `shared()` `normalize()` `default_path()` | `dev_config` |
| `runtime_flags` | `build_flags()` `install()` `shared()` | （不依赖） |
| `prompt_library` | `PromptLibrary` `PromptRejected` `PROMPTS` `shared()` `install()` | 函数内：`dialogue_judge` `memory_maintenance_agent` `stage3_runtime` |
| `provider_registry` | `known()` `base_url_of()` | （不依赖） |
| `runtime_diagnostics` | `LocalRuntimeDiagnostics` `RuntimeDiagnostics` Protocol | `dev_config` |
| `media_segments` | 消息段与图片处理 | `transport` `vision` |
| `outbox` | `Outbox` 补发队列 | `transport` |

---

## 三、Stage 3（对话 / 记忆 / 防护 / 直接管它们的命令）

### 引擎与人格
| 文件 | 提供 | 用到 |
| --- | --- | --- |
| `stage3_main` (3869 行) | `DialogueEngine`（96 个方法：`handle()` `tick()` `snapshot()` `execute_action()` `enable_group()` `disable_group()` `leave_session()` `request_restart()` …）、`OutgoingMessage` 装配、`parse_super_command()` `parse_group_action()` `is_admin_help_command()` `PUBLIC_HELP` `ADMIN_HELP`、`SuperAction` 枚举、`RelayDelivery` / `PendingRelay` | 顶层：`_host` `admin_control` `attention` `builtin_commands` `dev_config` `dialogue_compaction` `dialogue_judge` `extensions` `feature_log` `focus` `llm_client` `memory_model` `memory_service` `memory_view` `metrics` `onebot_ws` `runtime_flags` `security` `snapshots` `stage3_runtime` `transport` `trigger`；函数内：`_host_adapters` `capabilities` `command_plugins` `runtime` |
| `stage3_runtime` (1205 行) | 六套 prompt 的装配（`build_*_messages`）`TLABEL` | `prompt_library` `security` `transport` `base_prompt` |
| `base_prompt` | 人格正文（`_load_base_prompt()`） | `dev_config` |
| `extensions` | `PromptSources` `PromptContext` `PromptMaterial` `KnowledgeItem` | `transport` |

### 对话判定与节奏
| 文件 | 提供 | 用到 |
| --- | --- | --- |
| `dialogue_judge` | `JudgeVerdict` `build_judge_messages()` `parse_judge_output()` | `security` `stage3_runtime` `transport` + 函数内 `prompt_library` |
| `dialogue_compaction` | `build_compaction_messages()` `merge_summary()` `parse_compaction_output()` `split_compaction_batches()` | `security` `transport` |
| `attention` | `address_reason()` `is_addressed_to_bot()` `mentions_name()` | `transport` |
| `focus` | `FocusController` | `transport` |
| `trigger` | 触发原因 | `transport` |
| `conversation_context` | `ConversationContextProvider` | `capabilities` `transport` |

### 记忆
| 文件 | 提供 | 用到 |
| --- | --- | --- |
| `memory_store` (1867 行) | 两层库 + archive + tombstones 的读写 | `security` |
| `memory_service` | `MemoryService` | `memory_config` `memory_inbox` `memory_maintenance_agent` `memory_model` `memory_store` |
| `memory_maintenance_agent` | 维护 agent（批记忆、写画像） | `memory_store` + 函数内 `prompt_library` |
| `memory_model` | `MemoryItem` `MemoryMaterial` 等数据模型 | `security` `dev_config`… |
| `memory_config` | `MemorySettings.from_environment()` | `dev_config` |
| `memory_filters` | 检索过滤 | `memory_model` |
| `memory_inbox` | 待处理消息池 | `transport` |
| `memory_ops` | `MemoryOps`（人工操作：删、调关系） | `memory_store` |
| `memory_view` | `build_overview()` `build_records_view()` | `memory_store` |

### 防护
| 文件 | 提供 | 用到 |
| --- | --- | --- |
| `security` | 注入/越界/敏感判定（被 10 个模块依赖） | `transport` |
| `prompt_guard` | `scan_text()` `scan_persona_text()` + 机制词表 | （不依赖） |
| `style_reviewer` | `StyleReviewer` | `llm_client` |

### 命令
| 文件 | 提供 | 用到 |
| --- | --- | --- |
| `admin_control` | `AdminCommandKind` `AdminCommand` `parse_admin_command()` | （**零依赖**） |
| `builtin_commands` | `HelpCommand` `PingCommand` `build_help_text()` `build_command_registry()` `default_command_plugins()` `HELP_COMMAND_PATTERN` | 函数内 `command_plugins` `builtin_balance_command` |
| `builtin_balance_command` | `BalanceCommand` `build_balance_client()` | `balance_client` `transport` `dev_config` |
| `balance_client` | `BalanceClient` `summarize()` | `dev_config` |

---

## 四、宿主适配与可插能力（`_host` 的实现）

| 文件 | 提供 | 用到 |
| --- | --- | --- |
| `_host_adapters` | `TypingStyler` `HelpCardRenderer` `LocalMachineProbe` `HostMachineProbe` `FileAuditSink` `build_host_services()` | `_host`；**函数内**：`typing_sim` `help_card` `runtime_diagnostics` `control_audit` |
| `typing_sim` | `split_segments()` `delay_plan()` `total_pause()` `segment_delay()` | （不依赖） |
| `help_card` | `render()` `available()` `clear_cache()` | （PIL 在函数内） |
| `vision` | `ImageDescriber` `replace_media_placeholder()` | `llm_client` `dev_config` |
| `qq_roles` | `SelfRoleCache` `normalize_role()` `ROLE_OWNER` 等 | `onebot_client`（函数内） |
| `knowledge_base` | 本地知识库检索 | `embeddings` |
| `knowledge_operator` | `OperatorKnowledge` `OperatorChunksStore` `open_index_readonly()` | `dev_config` `prompt_guard` |
| `embeddings` | ONNX 嵌入模型 | `dev_config` |
| `model_trace` | 模型 I/O 落盘（调试） | （不依赖） |

---

## 五、被依赖热度（改一个文件会影响谁）

| 被依赖次数 | 文件 | 含义 |
| --- | --- | --- |
| 18 | `transport` | 改它要动大半个包 |
| 10 | `security` | 防护判定的措辞/口径 |
| 6 | `memory_model` | 记忆数据模型 |
| 5 | `prompt_library` | 六套 prompt 的可编辑层 |
| 5 | `extensions` | prompt 素材类型 |
| 4 | `stage3_runtime` | 人格与 prompt 装配 |
| 3 | `onebot_client` `media_segments` `capabilities` `memory_filters` `memory_store` `memory_config` `vision` | |
| 2 | `runtime_diagnostics` `control_audit` `help_card` `_host` | |

---

## 六、唯一的双向依赖

```
runtime ──顶层 import──▶ stage3_main        （build_engine 要造引擎）
stage3_main ──函数内 import──▶ runtime      （apply_overrides 等）
```
其余所有边都是单向的，所以除了这一对，任何文件都能按"被依赖热度"自底向上单独替换。
