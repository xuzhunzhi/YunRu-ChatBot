# 隔离与增长审计 · 2026-10-08

> 2026-10-08 已修：落盘切片（[2]，提交 9f92e0a；默认上限 50 → 200 并可调，`QQBOT_PERSISTED_HISTORY`）/ 承诺 FIFO（[4]，提交 db056ca）；另注：本文 [2] 的"最小改法"**原文写法有误**——只切片不前移 `dropped` 会清空 `live_history()`（见 verify 文档）。

范围：`dev/`（分支 `main`）的 `src/qq_roleplay_bot/` 与 `tests/`。
分区：**隔离与增长**（跨会话/跨群/跨人串台 + 无界增长）。
方式：只读静态审计（read / grep / glob / 目录体积），未跑测试、未启动进程、未碰 `run/`、未改一行源码。
已知并跳过：`_entries_for` 的 `full_bodies` 只按 `message_id` 取正文（跨会话撞号）、跨会话上下文污染（已修）、面板直接开库、检索 2 字滑窗、disable 静默删待处理。

## 一句话结论

内存与落盘各有**一条必然单调增长的主线**（会话状态不释放、全量导出历史同步 fsync），
外加一批**按裸 `message_id` 全局存、没有会话维度**的集合——撞号时会让 B 群的消息被 A 群顶掉；
其余增长点都缺上限或缺淘汰，但没有一条是"马上会炸"。

## 这一区最该修的三件

1. **短期会话状态永不释放**：`/admin disable`、`/super clear` 只 `exit()` 不清历史，单会话上限 2000 条，`states` 只增不减，真正的清理只发生在进程重启。
2. **落盘把整段历史全量导出**：`as_state()` 不截断（最多 2000 条 × 200 会话），每 10 秒在**回复路径上同步 fsync** 一次 JSON；`state_store.py:22 MAX_PERSISTED_HISTORY = 50` 全仓零引用，像丢掉的截断。
3. **一批"裸 message_id 全局集合"**：`_resumed_ids` 无上限、`_promised_ids` 用 `set.pop()` 任意淘汰、slang `_seen` 与 `MessageDeduplicator` 无会话维度——A 群同号会把 B 群的消息顶掉（作者自己实测 4579 条没撞号，但无契约）。

## 10 条清单

### [1] 会话状态只增不减（内存随群数单调涨，关群也不释放）

**现象**：每个开过对话的会话（群、私聊白名单、邮件渠道）在内存里永久留一份会话状态；
`/admin disable`、`/super clear` 之后仍然留着；只有进程重启才回收。

**证据**：

```python
# stage3_main.py:890, 894, 114
self.states: dict[str, ConversationState] = {}
return self.states.setdefault(session_id, ConversationState(history_limit=HISTORY_LIMIT))
HISTORY_LIMIT = 2000

# stage3_main.py:910-912
def leave(self, session_id: str) -> None:
    self.state(session_id).exit()
    self.trigger(session_id).reset()

# stage3_runtime.py:354-357  —— exit() 一行都不碰 history
def exit(self) -> None:
    self.mode = ConversationMode.IDLE
    self.active_user_id = None
    self.context = ContextState()

# stage3_main.py:3137  —— disable_group 走的也是 leave()
self.sessions.leave(f"group:{group_id}")
```

**影响**：内存。每个开过的群/私聊/渠道会话各留最多 2000 条消息加别名表，disable/clear 之后仍占着，
只有重启回收（重启还顺手截到 200 会话 × 50 条）。`dev/data` 里没有生产规模的 `runtime_state.json`，
所以"涨到多少"是**推断**：2000 × 会话数 × 单条文本，1000 群量级即几百 MB。

**最小改法**：给 `DialogueSessions` 加会话数上限 + LRU 淘汰；`leave()` 里真正 `self.states.pop(session_id, None)`
（用户语义上就是"清掉这个群的短期对话"）。

**可调项与默认值**：`sessions_max=200`（与 `MAX_PERSISTED_SESSIONS` 对齐）；`HISTORY_LIMIT` 保持 2000 也行，
因为压缩阈值 `LIVE_TARGET=500` 先触发。

**严重度：高**。理由：这是唯一"结构上必然单调增长，且用户操作明确期望它释放却完全没用"的地方。

### [2] 持久化全量导出历史 + 事件循环上同步 fsync

**现象**：落盘把每个会话的**整段历史**（不截断）写进 `runtime_state.json`，最多 200 个会话；
写入发生在回复路径上、同步、带 `fsync`。

**证据**：

```python
# stage3_runtime.py:392-406  —— 没有 [-N:] 切片
"history": [
    {"message_id": m.message_id, "session_id": m.session_id, "user_id": m.user_id,
     "text": m.text, ...}
    for m in self.history
],

# stage3_main.py:917-924, 149, 3172
candidates = [(session_id, state) for session_id, state in self.states.items() if state.history]
candidates.sort(key=lambda item: item[1].last_activity_at or 0.0, reverse=True)
for session_id, state in candidates[:max_sessions]: ...
MAX_PERSISTED_SESSIONS = 200
"sessions": self.sessions.export(max_sessions=MAX_PERSISTED_SESSIONS),

# stage3_main.py:4153-4161  —— 回复分支里同步调用
if decision.kind is DecisionKind.REPLY and decision.text:
    self._persist_after_activity(now)

# state_store.py:22  —— grep 全 dev 树只有这一处定义、零引用（实测）
MAX_PERSISTED_HISTORY = 50
```

**影响**：磁盘与热路径。每 10 秒（`PERSIST_MIN_INTERVAL_SECONDS = 10.0`，`stage3_main.py:151`）
把最多 200 个会话的整段历史（各可达 2000 条、含正文）dump 成一份 JSON 并 `fsync`，
这份 JSON 还要先在内存里整体拼出来——回复路径被同步阻塞；排查时 `runtime_state.json` 也会大到不好读。

**最小改法**：`as_state()` 里 `self.history[-MAX_PERSISTED_HISTORY:]`（把那个 50 用起来）；
`export()` 保持现在的 last_activity 排序即可。

**可调项与默认值**：`MAX_PERSISTED_HISTORY=50`、`MAX_PERSISTED_SESSIONS=200`、
`PERSIST_MIN_INTERVAL_SECONDS` 10 → 30。

**严重度：高**。理由：与 [1] 同源，但这条在**热路径 + 落盘**上，修起来最小（一行切片）、收益最大。

### [3] 多张集合/缓存按裸 `message_id` 全局存，没有会话维度

**现象**：`_resumed_ids`、`_promised_ids`、slang 的 `_seen`、`MessageDeduplicator` 都是按原始
`message_id` 记的，键里不带会话；撞号时一个群的消息会被另一个群顶掉。

**证据**：

```python
# stage3_main.py:1092-1094
self._resumed_ids: set[str] = set()
self._promised_ids: set[str] = set()

# stage3_main.py:3663 / 4298-4299  —— 加在焦点排队时，唯一移除点是排空那条路
self._resumed_ids.add(message.message_id)
self._resumed_ids.discard(message.message_id)
self._promised_ids.discard(message.message_id)

# stage3_main.py:3410, 3708  —— 命中就跳过去重与 state.add()
resumed = message.message_id in self._resumed_ids

# slang_learning.py:641, 702-706
self._seen: deque[str] = deque(maxlen=MAX_SEEN_MESSAGES)
message_id = _text(getattr(message, "message_id", ""))
if message_id:
    if message_id in self._seen:
        return 0
    self._seen.append(message_id)

# trigger.py:23-30  —— MessageDeduplicator 同样只按 message_id
def accept(self, message_id: str) -> bool:
    if message_id in self.seen:
        return False
```

作者自己的实测写在 `quote_samples.py:462`：
「本模块**不假设**它跨会话唯一（实测这份日志里 4579 个入站 id 没有一个跨会话撞号…）」。

**影响**：撞号时——B 群那条消息被当成"已记过历史/已去重"，**不进 B 群历史**；
或 slang 观察者直接静默丢弃 B 群的解释句；`_resumed_ids` 自己也没有上限（只在排空时清，
队列积压期间会一直加）。撞号概率按他们自己的实测是低，但**没有契约保证**。

**最小改法**：键带上会话，`f"{session_id}:{message_id}"`；slang 那边用 `f"{group_id}:{message_id}"`。

**可调项与默认值**：`_resumed_ids` 加 4096 上限（可复用 `MessageDeduplicator` 的 deque 淘汰法）；
slang `MAX_SEEN_MESSAGES=2000` 保持。

**严重度：中高**。理由："她会不会丢掉别的群的话"这一类里最像的一条；风险按实测偏低，所以不到高。

### [4] `_promised_ids` 用 `set.pop()` 做淘汰：丢的是任意一条，不是最旧的

**现象**：承诺"必回"的消息 id 存在 `set` 里，超上限时用 `set.pop()` 淘汰——
Python 的 `set.pop()` 移除的是**任意**元素，不是最早加入的那个。

**证据**：

```python
# stage3_main.py:1094
self._promised_ids: set[str] = set()

# stage3_main.py:3674-3676
self._promised_ids.add(message.message_id)
if len(self._promised_ids) > 256:
    self._promised_ids.pop()

# stage3_main.py:3821, 4291  —— 消费点：判定不许否决
must_reply = message.message_id in self._promised_ids
```

**影响**：积压超过 256 条时，被丢掉的可能是刚刚"回过稍等"的那一条——判定就重新获得否决权，
她答应过"回头答你"却静默了；事后只有一条 `focus_declined_promised` 的 warning 能对。
触发条件正是 [1][2] 那种"只在重启才清"的结构造出来的积压。

**最小改法**：换成 FIFO 结构（`OrderedDict`，或 `deque` + `set` 双结构，淘汰最早加入的）。

**可调项与默认值**：上限 256 保持。

**严重度：中**。理由：不是必然发生，但后果是承诺落空，且现场只有一条 warning。

### [5] 每会话一个模型 client，字典强引用，永不回收

**现象**：按会话新建的对话 client 与判定 client 存在两个普通 `dict` 里，只有加、没有删。

**证据**：

```python
# stage3_main.py:1002-1003
self._session_clients: dict[str, object] = {}
self._session_judges: dict[str, object] = {}

# stage3_main.py:1464-1467  —— 判定侧 :1484-1487 同样
client = self._session_clients.get(session_id)
if client is None:
    client = self.client_factory(session_id)
    self._session_clients[session_id] = client

# runtime.py:1593-1602  —— 每会话真的新建一个对象
def _dialogue_client_factory(session_id: str) -> OpenAICompatibleClient:
    return OpenAICompatibleClient(base_url, api_key, model,
        user_id=dev_config.session_user_id(dev_config.DIALOGUE_USER_ID, session_id), ...)

# llm_client.py:86  —— 那份登记表是弱引用，这两个 dict 是强引用
_CLIENTS: "weakref.WeakSet[OpenAICompatibleClient]" = weakref.WeakSet()
```

**影响**：进入过对话的每个会话各留两个 client（各带 `usage_totals` / `last_usage` 等小状态），
数量与会话数同阶、无上限；`_all_model_clients()`（`:1508-1518`）每次遍历整张表，面板热更也走它。

**最小改法**：与 [1] 的 LRU 一起清 `_session_clients/_session_judges`。

**可调项与默认值**：跟随 `sessions_max=200`；若改成 `WeakValueDictionary` 需确认调用期间不被回收，建议走显式淘汰。

**严重度：中**。理由：单个对象小，但和大对象是同一个"不释放"的根因。

### [6] 补上下文的两个缓存无上限、无淘汰

**现象**：群成员名表与"上次补历史时间"两张表只写不删。

**证据**：

```python
# conversation_context.py:55-56
self._last_refresh: dict[str, float] = {}
self._members: dict[str, dict[str, str]] = {}

# conversation_context.py:93-95
if now - self._last_refresh.get(block, float("-inf")) < self.refresh_interval:
    return ()
self._last_refresh[block] = now

# conversation_context.py:99-100  —— 整群成员表（几百到几千条 号→名）
if members is not None:
    self._members[session_id] = members
```

全树 `_members.pop` / `_last_refresh.pop` 均 0 命中（实测）。

**影响**：每个用过的群留下一份**整群成员表**加一条时间戳，永不清；`/admin disable` 过的群也留着。
几千个群量级时这是独立于 [1] 的第二条内存增长线。

**最小改法**：给 `_members` 加上限 + 按 `_last_refresh` 时间淘汰；`_last_refresh` 可只保留"当前启用群"。

**可调项与默认值**：`members_cache_max=200`。

**严重度：中**。理由：独立于 [1] 的增长源，且每项体积比会话状态大（整群名册）。

### [7] 焦点冷却表 `_ack_at` 永不清；`_queues` 的 deque 无 maxlen

**现象**：`"稍等"` 的冷却表每个会话留一条、永不删；焦点积压队列是裸 `deque()`，没有条数上限。

**证据**：

```python
# focus.py:48, 51
_queues: dict[str, deque[IncomingMessage]] = field(default_factory=dict)
_ack_at: dict[str, float] = field(default_factory=dict)

# focus.py:110-115  —— 没有条数闸门
queue = self._queues.get(session_id)
if queue is None:
    queue = deque()
    self._queues[session_id] = queue
    self._order.append(session_id)
queue.append(message)

# focus.py:152-153  —— 全树无删除点
def note_ack(self, session_id: str, now: float) -> None:
    self._ack_at[session_id] = now
```

**影响**：`_ack_at` 每项极小但永久（每个被 @ 过的群一条）；`_queues` 是这一区唯一
"由外部刷屏速度决定大小"的内存结构——热群刷屏时它没有天花板（`total_queued()` 只用于展示，不是闸门）。

**最小改法**：`enqueue` 加上限、超限丢最旧并计数；`_ack_at` 顺手清过期项（照 `_recent_denied_admins` 的做法）。

**可调项与默认值**：`focus_queue_max=50`。

**严重度：中**。理由：唯一一个没有上限约束、且大小由外部输入决定的队列。

### [8] `relationship_log` 是唯一一张没有保留期的内容表

**现象**：好感度/防备的每次变动都往 `relationship_log` 追加一行（含模型写的 `reason` 正文），
清理函数里唯独没有它。

**证据**：

```python
# memory_store.py:116-121
CREATE TABLE IF NOT EXISTS relationship_log (
    user_id TEXT NOT NULL, axis TEXT NOT NULL, before_value INTEGER NOT NULL,
    after_value INTEGER NOT NULL, reason TEXT NOT NULL, source TEXT NOT NULL,
    occurred_at REAL NOT NULL);

# memory_store.py:915 / 948 / 1020
"INSERT INTO relationship_log VALUES (?,?,?,?,?,?,?)",

# memory_store.py:552-574  —— _cleanup 删了 inbox/receipts/replays/leases/audit/archive
db.execute("DELETE FROM audit WHERE created_at<?", (now - 30 * DAY,))
db.execute("DELETE FROM archive WHERE deleted_at<?", (now - ARCHIVE_RETENTION_DAYS * DAY,))
```

全树没有 `DELETE FROM relationship_log`（只有 `:1796` 别名合并时的 `UPDATE`）。

**影响**：落盘只增不减，`reason` 是"他说了什么于是被防备"的长期留痕；
与其它表 7 天 / 30 天的保留口径不一致。

**最小改法**：`_cleanup` 里加 `DELETE FROM relationship_log WHERE occurred_at < now - N*DAY`。

**可调项与默认值**：`RELATIONSHIP_LOG_RETENTION_DAYS=90`。

**严重度：中**。理由：落盘单调增长 + 保留策略不一致；单行小，所以不是高。

### [9] `tombstones` 无保留期；`verify_runs.log` 无轮转

**现象**：墓碑表只增不删；验证留痕日志是全项目唯一"无限追加"的落盘文件。

**证据**：

```python
# memory_store.py:98-100
CREATE TABLE IF NOT EXISTS tombstones (
    scope_type TEXT NOT NULL, scope_key TEXT NOT NULL, normalized_key TEXT NOT NULL,
    deleted_at REAL NOT NULL, PRIMARY KEY(scope_type,scope_key,normalized_key));
# _cleanup()（:552-574）里没有 tombstones

# verify_log.py:216-218  —— 只追加，没有 capacity / trim
summary = directory / SUMMARY_NAME
with summary.open("a", encoding="utf-8", newline="\n") as handle:
    handle.write(line + "\n")
```

实测：`dev/data/logs/verify_runs.log` 已 30136 字节。

**影响**：`tombstones` 每个"被删过的记忆键"留一行（键里含 `scope_key`，形如 `群:人`），
永久累积、重启也不清——不过每行只有几个短串，量级小。
`verify_runs.log` 是一行一次验证、增长极慢，但它是唯一没有上限的落盘文件（其它 JSONL 都走 `RollingJsonlFile`）。

**最小改法**：`tombstones` 加 `deleted_at` 保留期；`verify_runs.log` 复用 `feature_log.RollingJsonlFile`。

**可调项与默认值**：`tombstones` 保留 180 天（早于 archive 的 30 天口径但不要永久）；日志 `capacity=500`。

**严重度：低**。理由：增长慢、单行小；列出来是因为"有没有哪个是无限追加"这条问题的答案是"有这一个"。

### [10] 金句笔记是全局的，`notes_for(group_id, …)` 的群号参数完全没用（需用户裁决）

**现象**：读侧的金句笔记函数签名带 `group_id`，但函数体里一次都没用它；同一份文件里
表情含义表却是按群存的——同一份数据两种口径。

**证据**：

```python
# quote_learning.py:622 —— 签名带 group_id
def notes_for(self, group_id: str, text: str = "", topic: str = "",
              limit: int | None = None) -> list[dict[str, str]]:
    ...
    # :642-649 —— 候选来自全量笔记，循环里没有 group_id
    for item in self.store.notes:
        note = _clean_note(item.get("note"))
        when = _clean_when(item.get("when"))
        if note and when and note_id(...) not in disabled:
            candidates.append((when, note))

# quote_learning.py:445 —— 含义表是按群的
entry = (self.meanings.get(str(group_id)) or {}).get(str(emoji_id)) or {}

# stage3_main.py:1196-1200 —— 调用点（注释写的是"按眼前这句的语境挑"）
notes = profile.notes_for(
    message.target.group_id or "", message.text or "", state.context.topic or "")
```

`tests/test_quote_learning.py:263-281` 明确要求"两个群的样本都递到 agent 手里"，
所以**推断这是设计**（说话风格跨群共享），不是漏写。

**影响**：A 群金句学出来的 `when/note` 会作为 DATA 进 B 群的回复 prompt。
是否算"把 A 群的东西端给 B 群"，取决于用户怎么定义金句的归属；代码现在按
"风格是她自己的"处理，但签名长得像按群。

**最小改法**：要么删掉这个没用的参数（避免后人误读成"已按群隔离"），
要么给笔记加 `group_id` 字段并按群过滤（只在用户认定金句属于群时才做）。

**可调项与默认值**：若按群过滤，`MAX_NOTES_PER_REPLY=5` 保持不变。

**严重度：低**。理由：内容是她自己的口吻习惯、不含群内事实；列出来是因为它是本次唯一
"看上去按群、实际全局"的读侧。

## 补充（不单列的那几项）

- **`group_roles._roles`**（`group_roles.py:117-118`）：`{(group_id, user_id): (role, clock)}`，
  TTL 只在**读**时判断（`:181-182`、`:263-269`），过期条目永不删除，也没有条数上限；
  只有 `forget()`（`:273-291`）会清。增长 ≈ 查询过的 (群, 人) 对，量级低于 [6]。
- **`_pending_relays` / `_pending_relay_selections`**（`stage3_main.py:1156-1157`）：
  过期清理 `_purge_expired_relays`（`:3221-3236`）只被 `_relay_reply`（`:3276`）调用——
  管理员不再发 `/admin relay` 就一直留着（每条含转告正文）。量级取决于管理员操作。
- **`AskBudget`**（`ask_when_unsure.py:151-181`）：`_asked` / `_times` 有 512 上限的粗暴清空
  （`:175-177`），且当前**没有消费者**（模块头与类 docstring 都写明"2026-10-05 晚起它没有消费者了"），可忽略。

## 证据与量法（实测 / 推断分开标）

**实测（这次真的看到了）**：

- 本文件里所有 `文件:行` 与代码片段：逐处 `read` 或 `grep` 核过。
- `MAX_PERSISTED_HISTORY` 在全 `dev/` 树只有 `state_store.py:22` 一处**定义**、零引用（grep 命中 4 处：
  `state_store.py:21,22` 与 `stage3_main.py:149,3172`，后两者用的是 `MAX_PERSISTED_SESSIONS`）。
- `_ack_at` / `_members` / `_last_refresh` 无任何删除点；`_roles.pop` 只出现在 `forget()` 里（grep）。
- `relationship_log` 无 `DELETE`（grep 的 15 处命中全是建表 / INSERT / UPDATE / SELECT）。
- `dev/data/logs/verify_runs.log` = 30136 字节；`dev/data/logs/reply.jsonl` = 4474554 字节；
  `dev/data/memory/memory.sqlite3` = 159744 字节（目录体积，只读）。
- 代码里"撞号"这件事的**他人实测**（不是我跑的）：`quote_samples.py:462` 记 4579 个入站 id 没有跨会话撞号。

**推断（没实测，按代码上限估的）**：

- [1][5][6][7] 的"涨到多少 MB"：按代码上限（2000 条/会话、200 会话导出、成员表规模）
  乘会话数的估算，不是实测占用；`dev/data` 里没有生产规模的 `runtime_state.json` 可量。
- [3][4] 的失败场景（B 群消息被顶掉、承诺那条被随机淘汰）：由代码路径推出的可能后果，
  没有构造真实撞号数据复现；撞号概率按仓库自己的实测（4579 条 0 撞号）判为低，但无契约。
- [10] 属于设计还是缺陷：由 `tests/test_quote_learning.py:263-281` 反推为"设计"，
  是判断，不是用户口径。

**自证边界**：全程只读（read / grep / glob / `Get-ChildItem`），**未执行测试、未启动进程、未写任何源码**；
本文件是这次审计唯一新增的文件。结论只覆盖"隔离与增长"这一分区，别的分区（面板、知识库、
插件接缝、prompt 边界等）没有查。

## 我没做到的

- **没跑任何测试，也没上真机**：所有结论都是静态读码，`verify_runs.log` 里没有本次的凭证。
- **没读生产数据**：没碰 `run/`、没读生产 `memory.sqlite3` / `runtime_state.json`，
  所以"现在实际占了多少内存 / 落盘多少"一个数都没有，全是结构判断。
- **没复现撞号**：`message_id` 跨会话撞号只有仓库自己 4579 条的实测记录，我没有独立验证。
- **没验证修复**：每条"最小改法"都没试改过，也不知道会不会踩到既有测试的断言
  （例如 `MAX_PERSISTED_HISTORY` 那个 50 有没有测试钉住别的语义）。
- **没覆盖别的分区**：只看了 `src/qq_roleplay_bot/` 与 `tests/`，插件侧（`plugins/`）、
  面板分支、`docs/`、`.env`、`run/` 一律没查。
- **条数取舍**：这一区里可能还有更小的点没列（本区上限 10 条），
  例如 `stage3_main.py:1050 _denied_admin` 与 `:1158 _relay_deliveries` 只列了清理时机问题、没单列。
