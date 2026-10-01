> **⚠️ 已归档（历史文档，不再维护）**
>
> 本文件描述的是重构前的 NapCat 接口规划；其中提到的已使用接口与连接可靠性要求
> 已经并入 `ARCHITECTURE.md`。当前状态以 `README.md` 与 `ARCHITECTURE.md` 为准。
# NapCat / OneBot 接口规划（仅规划，不落地）

## 当前实际配置

当前 NapCat 使用 OneBot v11 反向 WebSocket 客户端：

- 地址：`ws://127.0.0.1:8080`
- 消息格式：`array`
- `reportSelfMessage=false`
- 开启自动启动和约 5 秒重连
- 当前没有启用 HTTP API、HTTP Server、HTTP SSE 或 WebSocket Server

本文件不修改 NapCat 配置，也不把当前 token 写入项目或文档。

## 已使用接口

### 入站：群消息事件

核心字段：`post_type=message`、`message_type=group`、`group_id`、`user_id`、`message_id`、`message`、`self_id`。

Bot 侧只提取 `text` 消息段，并识别 `at` 段是否提到了自身。非目标群、私聊、空文本和非 message 事件在业务层忽略。

### 出站：`send_msg`

Bot 发送：`action=send_msg`，参数包含 `message_type=group`、`group_id` 和纯文本 `message`，使用 `echo` 等待 OneBot 回执。发送异常不能终止主循环。

## 建议的未来能力

按风险和收益分阶段加入，不在 Stage 3 直接实现：

| 能力 | OneBot 方向 | 用途 | 前置条件 |
| --- | --- | --- | --- |
| 回复指定消息 | `send_msg` 的 reply 消息段 | 让机器人明确接哪条话 | 内部消息模型增加 reply/message_id |
| 获取群基础信息 | `get_group_info` | 显示名、人数等弱上下文 | 明确隐私范围并做缓存 |
| 获取成员信息 | `get_group_member_info` | 区分昵称与 QQ 号 | 只在需要时请求，避免每条消息调用 |
| 主动撤回 | `delete_msg` | 清理错误回复 | 必须有显式开关和权限保护 |
| 群管理动作 | `set_group_ban` 等 | 管理功能 | 与语擦核心完全隔离，默认禁用 |
| 图片/表情 | `message` array 段 | 丰富输入输出 | 先扩展内部消息段模型，不直接拼字符串 |

## 连接可靠性

验收不能只看本地 8080 处于 Listen。必须同时确认：

1. Bot 进程正在监听 `127.0.0.1:8080`；
2. NapCat 日志出现对应账号的反向 WebSocket `Established`；
3. 收到一条目标群入站事件；
4. `send_msg` 回执成功。

重连、心跳、token 校验和单连接替换属于传输层；不要把这些逻辑塞进人格 prompt 或 Stage 3 会话状态。

## 不建议的接口扩展

- 不启用 HTTP、HTTP SSE 和 WebSocket Server 来重复提供同一条消息通道。
- 不让 NapCat 承担 prompt、记忆或模型调度。
- 不在模型输出中生成 OneBot action；模型只返回本地协议，业务层负责发送。

