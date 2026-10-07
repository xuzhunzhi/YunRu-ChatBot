# SnowLuma 接口清单调研 · 2026-10-08

只读调研：未发送任何消息，未 POST 任何接口，未改动任何源码/配置。带"实测"的行都读过对应文件或发过 GET。

## 一句话结论（能不能给非好友发私聊：能/不能/有条件）

**不能直接发，只有一条"有条件"的路。** 不带 `group_id` 的 `send_private_msg` 走好友私聊，被 QQ 服务端拒绝（报错串"请先添加对方为好友"在 SnowLuma 全部代码与 native 二进制里都搜不到，实测：仅存在于服务端返回）；带 `group_id` 走群临时会话，但 SnowLuma **本地另有一道硬门**：只有机器人此前**收到过**该用户在该群的临时会话消息，才允许发送，否则本地直接抛 `no such temp session`。你三次失败都是撞在这道门上，不是参数写错。

## 它的动作清单（来源：本机打包产物 / WebUI 文档 / 官方文档 —— 标清来源）

本机产物（实测）：
- `C:\ProgramData\NapCatQQ Desktop\components\SnowLuma\config-ClrCrmAk.js` 是权威来源：`index.mjs` 首行 `import { ... m as ACTION_REGISTRY ... }` 即指向它。内含 `ACTION_GROUPS`（12 分类）+ `defineAction` 134 条 + `groupAction` 55 条 = **189 个可执行动作**。
- 该目录**没有** `docs\`、没有 openapi.json、没有 README；只有 `EULA.md` / `PRIVACY.md` / `package.json`。`index.mjs` 里 `send_private_msg` 只出现 1 次（动作调用处）。
- 12 个分类：信息 / 消息 / 好友 / 群信息 / 群管理 / 群文件 / 请求 / 扩展 / 群相册 / 空间 / 系统表情 / 流式接口。另有 2 个隐藏别名 `.handle_quick_operation`、`.handle_quick_operation_async`。

WebUI `http://127.0.0.1:5099`（实测 GET）：
- 首页是 SPA；`/docs`、`/api-docs`、`/openapi.json`、`/swagger` **全部返回同一个 index.html（3938 字节）**，没有 Swagger 页，全产物也搜不到 `openapi`/`swagger`/`接口文档` 字样。
- 真正的清单端点是 `GET /api/debug/actions` → `{actions: collectActionDocs(), categories: collectCategories()}`（见 `server-D--Qu9qq.js`）。**实测未登录返回 401**，故本次未取到正文；它读的就是上面那份注册表，数据等价。

官方文档（联网，实测可达，与本地清单逐条吻合）：
- https://snowluma.github.io/zh/docs/api —— 同样的 12 分类，`扩展` 分类 115 个动作；例：`send_packet` 摘要"发送原始 SSO 包（cmd + hex data）"与本地完全一致。单动作页形如 `/zh/docs/api/message/send_private_msg`。

## 发消息相关的动作逐个说明（名字 · 参数 · 限制 · 来源）

参数一律取自本地 `config-ClrCrmAk.js` 的 `params` 定义（实测），非官方文档页面（其请求体 schema 是前端渲染的，静态 HTML 无参数）。

| 动作 | 参数 | 限制 | 来源 |
| --- | --- | --- | --- |
| `send_private_msg` | `user_id`, `message`, `group_id`（可选, int>=0）, `auto_escape` | 动作体：`tempGroupId = group_id>0 ? group_id : undefined`。无 group_id = 好友私聊；有 group_id 必须命中本地临时会话表 | 本地动作体 |
| `send_msg` | `message`, `message_type`, `group_id`, `user_id`, `auto_escape` | `message_type="private"` 且同时给 user_id+group_id 才走临时会话，否则按 group_id 走群 | 本地动作体 |
| `upload_private_file` | `user_id`, `file`, `name`（默认 ""）, `upload_file`（默认 true） | 内部 `uploadPrivate` 后调 `sendPrivateMessage(user_id, …, 无 group_id)` → 同样走好友私聊路径 | 本地 `group-file.ts` |
| `send_private_forward_msg` | `user_id`, `messages`, `message` | 好友私聊路径 | 本地 |
| `send_forward_msg` | `messages`, `message`, `message_type`, `group_id`, `user_id` | 按 message_type 路由，同上规则 | 本地 |
| `send_custom_face` | `emoji_id`, `group_id`, `user_id`, `reply_to` | group_id / user_id 二选一 | 本地 |
| `send_packet`（别名 `.send_packet`） | `cmd`（非空）, `data`（hex 偶长）, `rsp`（默认 true） | 扩展分类；直接发原始 SSO 包，**绕过**本地临时会话门 | 本地 + 官方文档 |

临时会话门的完整机制（实测，`index.mjs`）：
- `TempSessionStore` 是**纯内存** `Map`，键 `` `${userUin}:${groupUin}` ``，TTL 7 天（10080 分钟）；`record()` 在 groupId<=0 时不写。
- 只由接收路径写入：`handleTempMessage` 收到群临时消息时 `tempSessions.record(senderUin, groupId)`。
- 发送路径校验：`isTempReply = tempGroupId !== undefined && tempSessions.has(userId, tempGroupId)`，为假即抛 `cannot send to user X in group Y: no such temp session`。
- ctx 构造处 `tempSessions: new TempSessionStore()` → **进程重启即清空**，历史收到过的也不作数。
- 走通后内部路由：`bridge.apis.message.sendGroupTempMessage` 用 `grpTmp { groupUin, toUid }` 组包发送。
- 相关群设置：`set_group_member_permissions` 有参数 `allow_member_temporary_session`（"是否允许成员发起临时会话"，本地+官方文档）。该群若关闭，对方根本无法开临时会话 → 机器人永远收不到 → 永远回不了。

## 给非好友发消息：有哪些可能的路（按可行性排 ✓ 标注风险 ✗）

1. **等对方先开口（唯一正常路径）**：让对方在该群点开机器人发一条临时会话消息；之后 7 天内、同一 (user, group) 组合可用 `send_private_msg{user_id, message, group_id}` 回复。风险：无。前提：该群 `allow_member_temporary_session` 开启，且重启后需对方再次开口。
2. **先加好友**：放弃"非好友"前提，用 `send_private_msg{user_id, message}`。风险：无，但不满足需求。
3. **用 `send_packet` 手搓原始 SSO 包**：动作真实存在且绕过本地门，理论上可自行构造 grpTmp 发消息请求。风险最高：属未文档化协议行为，包格式出错可能异常/掉线，且可能违反 QQ 协议导致封号。**本次未调用。**
4. **换用互动类动作**（`send_like` / `friend_poke` / `set_input_status`）：对非好友预计同样不成立或无效——**推断，未实测**。

结论：没有"给非好友私聊"的专用动作；也没有"发群临时会话"的专用动作（只有 `send_private_msg`/`send_msg` 带 `group_id` 这一种形态，`sendGroupTempMessage` 只是内部 bridge 函数名，不是可调动作名）。

## 我没查到 / 不确证的

- `get_supported_actions`：全产物（含 native `.node`/`.dll`）搜不到该字符串，**本版没有这个动作**。你拿到的空数组不来自本地注册表（未注册动作应回 `retcode` 未知动作错误）。不确定你当时打到了哪个端点。
- `GET /api/debug/actions` 的实际返回正文未取到（401，未登录；按要求不做登录/POST）。
- `upload_private_file` 发给非好友是否必然失败：只确认它最终走 `sendPrivateMessage`（无 group_id）这条好友路径；服务端真实行为**未实测**（未发任何消息）。
- `send_packet` 能否成功伪造群临时会话：**未实测**，仅确认动作存在、参数形状、且绕过本地门。
- 报错串"请先添加对方为好友"的确切出处：SnowLuma 的 js 分包与 3 个 native 二进制均无此串（实测），故推断来自 QQ 服务端；未反编译协议层确认。
- native 二进制里是否还有其它未在 js 中出现的动作名：只做了特定字符串检索，未做完整符号提取。
