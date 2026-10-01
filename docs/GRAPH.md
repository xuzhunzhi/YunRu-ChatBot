# 结构图（Mermaid，随代码重新生成）

> 生成命令：`.\\.venv\\Scripts\\python.exe data\\tmp_mermaid.py`
> 节点与边都是从代码里读出来的，**不是手画的**——所以图不会和代码撒谎。
>
> 约定：**实线 `-->` = 顶层 import**（模块一加载就依赖，删掉就起不来）。
> **函数内 import**（可插：删掉只是少一项能力）**不画线**，每个图后面用表格列出——
> 虚线一多，图会绕成一团乱麻，反而看不清主干。
> 颜色：蓝=底层，绿=Stage 3，橙=宿主/可插。

## 一、主干调用链（谁调谁）

```mermaid
flowchart TD
    n__host["_host"]
    n__host_adapters["_host_adapters"]
    n_attention["attention"]
    n_background_plugins["background_plugins"]
    n_capabilities["capabilities"]
    n_command_plugins["command_plugins"]
    n_conversation_context["conversation_context"]
    n_dev_config["dev_config"]
    n_dialogue_compaction["dialogue_compaction"]
    n_dialogue_judge["dialogue_judge"]
    n_extensions["extensions"]
    n_focus["focus"]
    n_llm_client["llm_client"]
    n_memory_maintenance_agent["memory_maintenance_agent"]
    n_memory_model["memory_model"]
    n_memory_service["memory_service"]
    n_memory_store["memory_store"]
    n_onebot_ws["onebot_ws"]
    n_outbox["outbox"]
    n_prompt_library["prompt_library"]
    n_runtime["runtime"]
    n_security["security"]
    n_stage3_main["stage3_main"]
    n_stage3_runtime["stage3_runtime"]
    n_state_store["state_store"]
    n_transport["transport"]
    n_trigger["trigger"]

    n__host_adapters --> n__host
    n_attention --> n_transport
    n_command_plugins --> n_transport
    n_conversation_context --> n_capabilities
    n_conversation_context --> n_transport
    n_dialogue_compaction --> n_security
    n_dialogue_compaction --> n_transport
    n_dialogue_judge --> n_security
    n_dialogue_judge --> n_stage3_runtime
    n_dialogue_judge --> n_transport
    n_extensions --> n_security
    n_extensions --> n_stage3_runtime
    n_extensions --> n_transport
    n_focus --> n_transport
    n_memory_maintenance_agent --> n_memory_model
    n_memory_maintenance_agent --> n_memory_store
    n_memory_model --> n_security
    n_memory_service --> n_memory_maintenance_agent
    n_memory_service --> n_memory_model
    n_memory_service --> n_memory_store
    n_memory_store --> n_memory_model
    n_memory_store --> n_security
    n_onebot_ws --> n_transport
    n_outbox --> n_transport
    n_runtime --> n__host_adapters
    n_runtime --> n_background_plugins
    n_runtime --> n_capabilities
    n_runtime --> n_conversation_context
    n_runtime --> n_extensions
    n_runtime --> n_llm_client
    n_runtime --> n_memory_service
    n_runtime --> n_outbox
    n_runtime --> n_stage3_main
    n_runtime --> n_state_store
    n_runtime --> n_transport
    n_security --> n_transport
    n_stage3_main --> n__host
    n_stage3_main --> n_attention
    n_stage3_main --> n_dev_config
    n_stage3_main --> n_dialogue_compaction
    n_stage3_main --> n_dialogue_judge
    n_stage3_main --> n_extensions
    n_stage3_main --> n_focus
    n_stage3_main --> n_llm_client
    n_stage3_main --> n_memory_model
    n_stage3_main --> n_memory_service
    n_stage3_main --> n_onebot_ws
    n_stage3_main --> n_security
    n_stage3_main --> n_stage3_runtime
    n_stage3_main --> n_transport
    n_stage3_main --> n_trigger
    n_stage3_runtime --> n_extensions
    n_stage3_runtime --> n_memory_model
    n_stage3_runtime --> n_prompt_library
    n_stage3_runtime --> n_security
    n_stage3_runtime --> n_transport
    n_trigger --> n_transport

classDef bottom fill:#dbe9f8,stroke:#1f5691,color:#123
classDef stage3 fill:#def2de,stroke:#246a34,color:#132
classDef host fill:#fce9d2,stroke:#96540f,color:#321
```

| 模块 | 函数内 import（可插：删掉只是少一项能力） |
| --- | --- |
| `dialogue_judge` | `prompt_library` |
| `memory_maintenance_agent` | `prompt_library` |
| `prompt_library` | `dialogue_judge`, `memory_maintenance_agent`, `stage3_runtime` |
| `stage3_main` | `_host_adapters`, `capabilities`, `command_plugins`, `runtime` |

## 二、记忆子系统

```mermaid
flowchart TD
    n_dev_config["dev_config"]
    n_extensions["extensions"]
    n_memory_config["memory_config"]
    n_memory_filters["memory_filters"]
    n_memory_inbox["memory_inbox"]
    n_memory_maintenance_agent["memory_maintenance_agent"]
    n_memory_model["memory_model"]
    n_memory_ops["memory_ops"]
    n_memory_service["memory_service"]
    n_memory_store["memory_store"]
    n_memory_view["memory_view"]
    n_prompt_library["prompt_library"]
    n_security["security"]
    n_stage3_main["stage3_main"]
    n_transport["transport"]

    n_extensions --> n_security
    n_extensions --> n_transport
    n_memory_inbox --> n_memory_model
    n_memory_inbox --> n_memory_store
    n_memory_inbox --> n_security
    n_memory_inbox --> n_transport
    n_memory_maintenance_agent --> n_memory_model
    n_memory_maintenance_agent --> n_memory_store
    n_memory_model --> n_security
    n_memory_service --> n_memory_config
    n_memory_service --> n_memory_inbox
    n_memory_service --> n_memory_maintenance_agent
    n_memory_service --> n_memory_model
    n_memory_service --> n_memory_store
    n_memory_store --> n_memory_filters
    n_memory_store --> n_memory_model
    n_memory_store --> n_security
    n_security --> n_transport
    n_stage3_main --> n_dev_config
    n_stage3_main --> n_extensions
    n_stage3_main --> n_memory_model
    n_stage3_main --> n_memory_service
    n_stage3_main --> n_memory_view
    n_stage3_main --> n_security
    n_stage3_main --> n_transport

classDef bottom fill:#dbe9f8,stroke:#1f5691,color:#123
classDef stage3 fill:#def2de,stroke:#246a34,color:#132
classDef host fill:#fce9d2,stroke:#96540f,color:#321
```

| 模块 | 函数内 import（可插：删掉只是少一项能力） |
| --- | --- |
| `memory_maintenance_agent` | `prompt_library` |
| `memory_store` | `memory_config` |
| `prompt_library` | `memory_maintenance_agent` |

## 三、整体：谁依赖谁（全部模块）

> 这张图密（59 个节点），只用来看"有没有不该有的边"（比如 Stage 3 指向插件的）。

```mermaid
flowchart TD
    subgraph bottom["底层：服务与这台机器"]
        n_api_usage["api_usage"]
        n_background_plugins["background_plugins"]
        n_capabilities["capabilities"]
        n_command_plugins["command_plugins"]
        n_control["control"]
        n_control_audit["control_audit"]
        n_dev_config["dev_config"]
        n_feature_log["feature_log"]
        n_llm_client["llm_client"]
        n_media_segments["media_segments"]
        n_metrics["metrics"]
        n_model_trace["model_trace"]
        n_onebot_client["onebot_client"]
        n_onebot_ws["onebot_ws"]
        n_operator_config["operator_config"]
        n_outbox["outbox"]
        n_provider_registry["provider_registry"]
        n_runtime["runtime"]
        n_runtime_diagnostics["runtime_diagnostics"]
        n_runtime_flags["runtime_flags"]
        n_snapshots["snapshots"]
        n_state_store["state_store"]
        n_transport["transport"]
    end
    subgraph stage3["Stage 3：对话 / 记忆 / 防护 / 直接管它们的命令"]
        n_admin_control["admin_control"]
        n_attention["attention"]
        n_balance_client["balance_client"]
        n_base_prompt["base_prompt"]
        n_builtin_balance_command["builtin_balance_command"]
        n_builtin_commands["builtin_commands"]
        n_conversation_context["conversation_context"]
        n_dialogue_compaction["dialogue_compaction"]
        n_dialogue_judge["dialogue_judge"]
        n_extensions["extensions"]
        n_focus["focus"]
        n_memory_config["memory_config"]
        n_memory_filters["memory_filters"]
        n_memory_inbox["memory_inbox"]
        n_memory_maintenance_agent["memory_maintenance_agent"]
        n_memory_model["memory_model"]
        n_memory_ops["memory_ops"]
        n_memory_service["memory_service"]
        n_memory_store["memory_store"]
        n_memory_view["memory_view"]
        n_prompt_guard["prompt_guard"]
        n_security["security"]
        n_stage3_main["stage3_main"]
        n_stage3_runtime["stage3_runtime"]
        n_style_reviewer["style_reviewer"]
        n_trigger["trigger"]
    end
    subgraph host["宿主与可插能力"]
        n__host["_host"]
        n__host_adapters["_host_adapters"]
        n_embeddings["embeddings"]
        n_help_card["help_card"]
        n_knowledge_base["knowledge_base"]
        n_knowledge_operator["knowledge_operator"]
        n_prompt_library["prompt_library"]
        n_qq_roles["qq_roles"]
        n_typing_sim["typing_sim"]
        n_vision["vision"]
    end

    n__host_adapters --> n__host
    n_attention --> n_transport
    n_builtin_balance_command --> n_balance_client
    n_builtin_balance_command --> n_transport
    n_builtin_commands --> n_transport
    n_command_plugins --> n_transport
    n_control --> n_snapshots
    n_conversation_context --> n_capabilities
    n_conversation_context --> n_media_segments
    n_conversation_context --> n_onebot_client
    n_conversation_context --> n_transport
    n_dialogue_compaction --> n_memory_filters
    n_dialogue_compaction --> n_security
    n_dialogue_compaction --> n_transport
    n_dialogue_judge --> n_security
    n_dialogue_judge --> n_stage3_runtime
    n_dialogue_judge --> n_transport
    n_extensions --> n_security
    n_extensions --> n_stage3_runtime
    n_extensions --> n_transport
    n_focus --> n_transport
    n_knowledge_base --> n_extensions
    n_knowledge_base --> n_security
    n_knowledge_operator --> n_extensions
    n_knowledge_operator --> n_prompt_guard
    n_memory_inbox --> n_memory_model
    n_memory_inbox --> n_memory_store
    n_memory_inbox --> n_security
    n_memory_inbox --> n_transport
    n_memory_maintenance_agent --> n_memory_model
    n_memory_maintenance_agent --> n_memory_store
    n_memory_model --> n_security
    n_memory_service --> n_memory_config
    n_memory_service --> n_memory_inbox
    n_memory_service --> n_memory_maintenance_agent
    n_memory_service --> n_memory_model
    n_memory_service --> n_memory_store
    n_memory_store --> n_memory_filters
    n_memory_store --> n_memory_model
    n_memory_store --> n_security
    n_onebot_client --> n_onebot_ws
    n_onebot_client --> n_transport
    n_onebot_ws --> n_media_segments
    n_onebot_ws --> n_transport
    n_outbox --> n_transport
    n_prompt_library --> n_prompt_guard
    n_runtime --> n__host_adapters
    n_runtime --> n_api_usage
    n_runtime --> n_background_plugins
    n_runtime --> n_capabilities
    n_runtime --> n_conversation_context
    n_runtime --> n_extensions
    n_runtime --> n_llm_client
    n_runtime --> n_memory_config
    n_runtime --> n_memory_ops
    n_runtime --> n_memory_service
    n_runtime --> n_onebot_client
    n_runtime --> n_outbox
    n_runtime --> n_stage3_main
    n_runtime --> n_state_store
    n_runtime --> n_style_reviewer
    n_runtime --> n_transport
    n_runtime_diagnostics --> n_security
    n_security --> n_transport
    n_stage3_main --> n__host
    n_stage3_main --> n_admin_control
    n_stage3_main --> n_attention
    n_stage3_main --> n_builtin_commands
    n_stage3_main --> n_dev_config
    n_stage3_main --> n_dialogue_compaction
    n_stage3_main --> n_dialogue_judge
    n_stage3_main --> n_extensions
    n_stage3_main --> n_feature_log
    n_stage3_main --> n_focus
    n_stage3_main --> n_llm_client
    n_stage3_main --> n_memory_model
    n_stage3_main --> n_memory_service
    n_stage3_main --> n_memory_view
    n_stage3_main --> n_metrics
    n_stage3_main --> n_onebot_ws
    n_stage3_main --> n_runtime_diagnostics
    n_stage3_main --> n_security
    n_stage3_main --> n_snapshots
    n_stage3_main --> n_stage3_runtime
    n_stage3_main --> n_transport
    n_stage3_main --> n_trigger
    n_stage3_runtime --> n_base_prompt
    n_stage3_runtime --> n_extensions
    n_stage3_runtime --> n_memory_filters
    n_stage3_runtime --> n_memory_model
    n_stage3_runtime --> n_prompt_library
    n_stage3_runtime --> n_security
    n_stage3_runtime --> n_transport
    n_trigger --> n_transport
    n_vision --> n_media_segments

classDef bottom fill:#dbe9f8,stroke:#1f5691,color:#123
classDef stage3 fill:#def2de,stroke:#246a34,color:#132
classDef host fill:#fce9d2,stroke:#96540f,color:#321
```

| 模块 | 函数内 import（可插：删掉只是少一项能力） |
| --- | --- |
| `_host_adapters` | `control_audit`, `help_card`, `runtime_diagnostics`, `typing_sim` |
| `builtin_commands` | `builtin_balance_command`, `command_plugins` |
| `dialogue_judge` | `prompt_library` |
| `knowledge_operator` | `knowledge_base` |
| `memory_maintenance_agent` | `feature_log`, `prompt_library` |
| `memory_store` | `memory_config` |
| `prompt_library` | `base_prompt`, `dialogue_judge`, `memory_maintenance_agent`, `stage3_runtime`, `style_reviewer`, `vision` |
| `qq_roles` | `onebot_client` |
| `runtime` | `control_audit`, `knowledge_base`, `knowledge_operator`, `provider_registry`, `qq_roles`, `vision` |
| `stage3_main` | `_host_adapters`, `balance_client`, `builtin_balance_command`, `capabilities`, `command_plugins`, `help_card`, `runtime`, `vision` |
| `style_reviewer` | `prompt_library` |
| `vision` | `prompt_library` |

## 四、四个契约（接口）

```mermaid
classDiagram
    class QQTransport {
        <<interface>>
        +receive()
        +send(target, text)
        +send_typing(target, notice)
    }
    class CommandPlugin {
        <<interface>>
        +name
        +min_level
        +match(message)
        +handle(message)
        +help_text()
    }
    class BackgroundPlugin {
        <<interface>>
        +name
        +interval_seconds
        +poll_once()
        +close()
    }
    class OutboundStyler {
        <<interface>>
        +split(text, limit)
        +delay_plan(segments)
    }
    class CardRenderer {
        <<interface>>
        +available
        +render(title, text)
    }
    class MachineProbe {
        <<interface>>
        +sample()
    }
    class AuditSink {
        <<interface>>
        +record(action, detail)
        +tail(count)
    }
    class BalanceSource {
        <<interface>>
        +query()
    }
    class RoleLookup {
        <<interface>>
        +role(group_id)
        +self_id()
    }
    class KnowledgeIndex {
        <<interface>>
        +search(query, limit)
    }

    class DialogueEngine
    class PluginRegistry
    class HostServices

    DialogueEngine ..> QQTransport : 收发
    DialogueEngine ..> HostServices : 宿主能力
    PluginRegistry o-- CommandPlugin
    PluginRegistry o-- BackgroundPlugin
    PluginRegistry o-- RoleLookup : 前置插件提供
    HostServices o-- OutboundStyler
    HostServices o-- CardRenderer
    HostServices o-- MachineProbe
    HostServices o-- AuditSink
    HostServices o-- BalanceSource
    HostServices o-- RoleLookup
    HostServices o-- KnowledgeIndex
```

## 五、一条消息的路径（时序）

```mermaid
sequenceDiagram
    participant N as NapCat
    participant T as transport(onebot_ws)
    participant R as runtime.serve
    participant E as DialogueEngine
    participant P as commands(插件+核心命令)
    participant S as security
    participant J as dialogue_judge
    participant M as memory_service
    participant L as llm_client
    participant O as outbox

    N->>T: WS 推一条消息
    T->>R: IncomingMessage
    R->>R: 去重 / 批量 / 焦点排队
    R->>E: handle(message)
    E->>P: resolve(message)
    alt 是命令
        P-->>E: 插件或核心命令
        E->>E: 判档位 public/admin/super（fail-closed）
        E-->>R: 回复（paced=False）
    else 是聊天
        E->>S: 注入 / 越界 / 敏感判定
        E->>M: 检索长期记忆
        M-->>E: 记忆素材（不可信 DATA）
        E->>J: 要不要回（判定 agent）
        J-->>E: REPLY / SILENT
        E->>L: 生成回复
        L-->>E: 文本
        E->>E: 分段 + 拟人停顿（_host.styler）
        E-->>R: OutgoingMessage
    end
    R->>T: send()（失败进 outbox 补发）
    T->>N: 发出去
```

---

## 渲染好的图（PNG，直接点开就能看）

| 图 | 文件 |
| --- | --- |
| 主干调用链（27 个模块） | [`docs/graph/spine.png`](graph/spine.png) |
| 记忆子系统（15 个模块） | [`docs/graph/memory.png`](graph/memory.png) |
| 整体依赖（59 个模块） | [`docs/graph/overall.png`](graph/overall.png) |
| 四个契约与接口 | [`docs/graph/interfaces.png`](graph/interfaces.png) |
| 一条消息的路径（时序） | [`docs/graph/sequence.png`](graph/sequence.png) |

> 为什么用 PNG 而不是 SVG：mermaid 渲染的 SVG 里文字是 `<foreignObject>`（HTML 文本），
> 很多查看器不画它，打开就是"有框没字"。PNG 是像素，哪儿都能看。
> 需要矢量图时自己渲染：`npx --yes @mermaid-js/mermaid-cli@11 -i x.mmd -o x.svg`（用浏览器看）。

重渲染：

```powershell
.\\.venv\\Scripts\\python.exe data\\tmp_mermaid.py     # 重新生成 docs/GRAPH.md
```
