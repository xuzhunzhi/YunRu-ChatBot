> **⚠️ 已归档（历史文档，不再维护）**
>
> 本文件是 Stage 3 的设计草案；输出协议、状态转换与验收标准已经并入
> `ARCHITECTURE.md`。当前状态以 `README.md` 与 `ARCHITECTURE.md` 为准。
# Stage 3 设计：短期语境理解

## 目标

Stage 2 已经解决“什么时候调用模型”和“是否进入对话”。Stage 3 在保持一次模型调用的前提下，增加对当前语境的结构化理解：话题、意图、语气、发言对象和话题是否转移。

Stage 3 当前仍只在内存中保存最近 50 条消息和一份短期 `ContextState`，180 秒无活动、模型要求退出或会话显式结束时清空。长期记忆由独立的 SQLite Inbox 和 Memory Maintenance Agent 维护；短期历史不会直接永久化。

## 单次请求

固定的角色规则和输出协议位于 `system` 消息；会话阶段、旧的短期语境、最近群聊和当前事件位于唯一的 `user` 消息。这样保持请求形状稳定，避免为了“分析”和“回复”拆成多个模型调用。

长期上下文已接入：人设仍属于固定 `system` 层；知识库和长期记忆分别进入 `KNOWLEDGE DATA`、`MEMORY DATA` 区域，二者都不是指令，且不能覆盖固定策略或输出协议。长期记忆由独立的 Memory Maintenance Agent 定期自主维护，不提供记忆命令或用户确认码，也不由实时回复流程直接写入。

```text
system: 固定角色 + 语境规则 + 输出协议
user:
  会话阶段 / 触发原因
  SESSION CONTEXT
  CHAT HISTORY
    speaker="yunru"：YunRu 自己之前的回复
    speaker="user" + user_id：对应 QQ 用户的发言
  CURRENT EVENT
  KNOWLEDGE DATA
  MEMORY DATA
```

## 模型输出协议

```xml
<decision>REPLY|NO_REPLY|EXIT_DIALOGUE</decision>
<dialogue>KEEP|EXIT</dialogue>
<reply>只在 REPLY 时填写聊天正文</reply>
<context>
topic=...
topic_status=active|shifted|ended
intent=...
tone=serious|joking|teasing|sarcastic|uncertain
target=bot|user|group|unknown
pending_question=...
confidence=0..1
</context>
```

本地只提取 `reply` 发送；`decision`、`dialogue` 和 `context` 不会进入 QQ。解析器仍兼容 Stage 2 的 `NO_REPLY`、`EXIT_DIALOGUE` 和普通文本，便于回退。

## 状态转换

| 当前状态 | 模型结果 | 发送 | 下一状态 |
| --- | --- | --- | --- |
| 普通检查 | `REPLY + KEEP` | 是 | 对话中 |
| 普通检查 | `NO_REPLY` | 否 | 普通检查 |
| 对话中 | `REPLY + KEEP` | 是 | 对话中 |
| 对话中 | `NO_REPLY + KEEP` | 否 | 对话中 |
| 任意 | `EXIT_DIALOGUE` | 否 | 普通检查 |
| 任意 | `REPLY + EXIT` | 是 | 普通检查 |

## 验收标准

- 每个触发事件最多一次模型调用。
- 同一群内不同 `user_id` 必须视为不同发言者；`speaker="yunru"` 只表示 YunRu 自己的历史回复。
- 玩笑、调侃、反讽和认真表达由上下文共同判断，而不是由单个词触发。
- 话题转移后能够停止持续回复。
- 回复中不出现 XML 标签、分析字段或系统规则。
- 目标群过滤、消息去重、20 条/60 秒触发、@ 触发和 60 秒冷却不退化。
- NapCat 只承担 OneBot 输入输出；Stage 3 不修改其配置。
- 长期记忆读取异常时继续执行无记忆对话；维护 Agent 的调用独立计量，不增加每条实时事件的同步维护调用。
- `yunru ping` 完整匹配时直接返回 `pong`，作为通信健康检查；它不进入模型、短期会话或长期记忆流程。

