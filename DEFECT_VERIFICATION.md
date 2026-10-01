# 缺陷核实与修复记录

本文记录对 Stage 3 与长期记忆子系统的一次系统性缺陷核实，以及随后实施的修复。
核实方式是逐条读取源码取证，并用一次性脚本实际运行验证；脚本已在核实后删除。

## 状态总览

| 编号 | 问题 | 核实结论 | 修复 |
| --- | --- | --- | --- |
| D1 | `receive()` 永不返回 `None`，断连后主循环永久挂死 | 实测确认 | 已修 |
| D2 | 纯 @ 消息在解析层被整条丢弃 | 实测确认 | 已修 |
| D3 | @ 误判 | 仅字符串消息形态触发 | 已修 |
| D4 | 冷却压制强制触发；`reset()` 不清冷却 | 实测确认 | 已修 |
| D5 | 记忆检索把类型权重放在相关性之前 | 实测确认 | 已修 |
| D6 | 未选中证据在 commit 后被永久删除 | 确认（原为设计取舍） | 已修（一次重放） |
| D7 | ADD 可用空证据绕过 | **误报，撤回** | 无需修复 |
| D8 | `capture()` 时机过早 | 确认（设计取舍） | 保留现状 |

修复后新增回归用例，全部纳入 `tests/run_offline.py`。

## 追加：媒体消息与端到端链路

D1–D6 修完后，为新增功能补的端到端测试（`tests/test_integration_chain.py`，把真实 OneBot 事件经真实传输层与引擎走到真实出站 payload）又暴露出两个问题，已一并修复：

1. **只发媒体不触发检查**：用户主动发一张图/一个表情，因为既没有 @ 也没到 20 条阈值，消息只会停在缓冲区，Bot 完全没反应。已把"只发媒体"提升为与 @ 同级的强制触发，触发原因记为 `media`（单独计入指标）。
2. **活跃对话中被媒体插话抢焦点**：同伴发言的"不抢焦点"判断只看 @，别人发图片同样会被当作背景丢弃。已把 `has_media` 一并纳入判断。

同时补上 `set_enabled()` 的持久化：此前通过控制端口关闭 Bot 只在内存生效，进程重启会回到启用状态，与群启停名单的行为不一致。

---

## D1 `receive()` 永不返回 `None`，断连后主循环永久挂死

**原因** `onebot_ws.py` 的 `receive()` 直接 `await self._messages.get()`，队列只有 `put`、没有任何哨兵值；`close()` 只取消 server、清连接、fail pending，从不唤醒等待者。`transport.py` 的协议却声明"连接关闭返回 `None`"，三处 `None` 分支都是死代码。

**影响** 断连时主循环完全无感知，不重连、不报错、不退出，日志停在 `OneBot 已断开`，只能强制结束进程。

**修复** `close()` 置 `_closed` 并向队列放入 `None` 哨兵唤醒消费者；`receive()` 把哨兵翻译成 `None` 返回，并在非关闭状态下防御性还原哨兵。

**回归用例** `test_close_wakes_up_receive`。

---

## D2 纯 @ 消息被整条丢弃

**原因** `extract_text` 只收集 `text` 段，`at` 段被丢弃，于是纯 @ 的文本为空，`parse_message_event` 的 `if not user_id or not text.strip(): return None` 直接丢弃事件。

**影响** 用户只发一个 `@YunRu`（最典型的"叫你一下"）时，消息不进去重、不进 `handle()`、不触发模型，Bot 完全无反应；`force=is_bot_mentioned` 永远没有机会执行。

**修复** 新增 `MENTION_ONLY_TEXT` 占位文本：有 @ 而无文字时用占位文本保留"被呼叫"这一事件，无 @ 且无文字的空白消息仍然忽略。

**回归用例** `test_mention_only_message_is_kept_instead_of_dropped`、`test_empty_message_without_mention_is_ignored`。

---

## D3 @ 误判（严重性下调后修复）

**原因** `bot_is_mentioned` 的段形态用 `str(qq) == self_id` 精确比较（正确），字符串形态却用子串 `f"qq={self_id}" in message`。

**影响** 当前 NapCat 配置为 `message` 段数组，走精确分支，因此实机不复现；只有切换为 CQ 码字符串形态时，`qq=999` 会被 `qq=9999`、`qq=99900` 误命中，导致 Bot 在没被叫时抢话。

**修复** 字符串形态改用 `_CQ_AT_PATTERN` 提取 QQ 号后精确比较。

**回归用例** `test_mention_matching_is_exact_in_both_message_shapes`。

---

## D4 冷却压制强制触发，且 `reset()` 不清冷却

**原因** `IdleTrigger.add()` 先判 `_in_cooldown` 再看 `force`，所以被 @ 也要等冷却；`reset()` 只清 `_buffer` 与 `_first_message_at`，不清 `_last_trigger_at`，而 `reset()` 会被显式退出对话、管理员关群、清理会话调用。

**影响** 两条：(1) 60 秒内的第二次 @ 被静默记为 `deferred_messages`，与"被叫就该应一声"的直觉冲突；(2) 模型判定退出对话后再 @ Bot，仍会被上一次触发的冷却挡住最长 60 秒。

**修复** `force=True` 时跳过冷却判断，冷却只约束阈值触发；`reset()` 同时清空 `_last_trigger_at`。

**回归用例** `test_forced_trigger_bypasses_cooldown`、`test_cooldown_still_limits_threshold_trigger`、`test_reset_clears_cooldown`。

---

## D5 记忆检索排序把类型权重放在相关性之前

**原因** `memory_store.retrieve` 的打分元组是 `(kind in {"name","boundary"}, relevant, confidence, updated_at)`。Python 元组按位比较，第一位是布尔值，因此只要 `kind` 是 `name`/`boundary` 就无条件压过所有 `preference`/`group_fact`/`topic_summary`，`relevant` 只在同类型桶内生效。

**影响** 一条与当前查询完全无关的 `name` 记录恒居第一，且只有 5 个名额。实测两个截然不同的查询得到完全相同的排序。

**修复**

- 打分元组改为 `(relevant, kind, confidence, updated_at)`，相关性优先，类型降为同相关度内的次要权重。
- 新增 `_query_terms`：英文/数字取长度 ≥2 的词元，中文取 1~2 字滑窗加整段，并过滤 `STOPWORDS` 虚词，降低单字噪声。

**回归用例** `test_relevance_outranks_record_kind`。

**已知遗留** 每个命名空间仍先 `ORDER BY updated_at DESC LIMIT 100` 再打分，不是全局 Top-5；`last_used_at` 已回写但尚未参与淘汰。

---

## D6 未选中证据在 commit 后被永久删除

**原因** 一个批次最多 `claim` 40 条事件，而模型每轮最多输出 5 个操作，未达上限，因此"未引用"是常态；`commit` 却无条件删除整批 inbox 行（仅写入 `receipts` 作为已处理标记）。

**影响** 模型某一轮误判，或第一次调用超时导致整批 `release`，那批聊天内容不可逆丢失，没有兜底。

**修复** 新增 `replays` 表与 `MAX_REPLAY_ATTEMPTS = 2`：

- 同批中只要有任一操作引用了证据，说明模型确实读到了这批事件；未被引用的事件写入 `replays` 获得一次重放机会。
- 整批只有 `IGNORE`（无任何证据引用）是模型的显式忽略，直接归档为已读，不重放。
- 重放达到上限后才写入回执并离开 inbox；已被引用的事件立即归档。
- `_settle_batch_events` 只删除已归档的事件，并解除未离开事件的租约，避免它们挂在已删除的租约上。

**回归用例** `test_uncited_evidence_gets_one_replay_then_settles`、`test_cited_evidence_is_not_replayed`。

---

## D7 撤回：ADD 无法用空证据绕过

早期报告称 `memory_model.py` 只在证据非空时校验，因此 `ADD` 可带空证据通过。实际读取源码后确认：

```python
if evidence and not any(events[e].speaker == "user" for e in evidence):
    fail()
...
if (op == "ADD" and (targets or not evidence)) or ...:
    fail()
```

两条合起来已强制 `ADD` 必须带非空且含 `speaker == "user"` 的证据。实测空证据与 yunru-only 证据都被 `invalid_operation` 拒绝。**该条为误报，证据校验是完整的，不需要修。**

---

## D8 `capture()` 时机（保留现状）

**原因** `stage3_main.py` 在会话焦点判定和触发阈值判定**之前**调用 `memory_service.capture(message)`，且后续的 `return None` 不会撤销已入队事件。

**影响** 被 deferred 的插话和未达阈值的普通消息都会进入 inbox，被维护 Agent 用于推断长期记忆。群里刷屏与无关闲聊会成为记忆素材，只受队列深度和敏感词过滤限制。

**决策** 经确认，**保留背景语境**：记忆素材的完整性优先于降低信噪比，代价由维护 Agent 的自主判断承担。此项不视为缺陷。

**已做的缓解** inbox 去重键加入 `session_id`，避免不同会话的相同 `message_id` 互相顶掉；inbox 增加按群配额，避免单个活跃大群吃满全局配额。

---

## 核实过程中额外发现并修复的问题

这几条不在最初的缺陷清单里，是在修复与回归过程中实测暴露的：

1. **inbox 全局配额**：原先只有 10000 行全局上限，一个活跃大群可以吃满，导致其他群 `append` 全部静默失败。改为按群 `MAX_INBOX_EVENTS_PER_GROUP = 2000` 加全局 `MAX_INBOX_EVENTS = 10000` 双限。回归用例 `test_inbox_quota_is_per_group`。
2. **`participants.reviewed_at` 不刷新**：`INSERT OR IGNORE INTO participants` 在行已存在时不更新 `reviewed_at`，使长期只产生 `IGNORE` 的会话被周期性反复复查。改为 `ON CONFLICT ... DO UPDATE SET reviewed_at=MAX(...)`。
3. **inbox 去重键缺少会话维度**：原键为 `group:user_id:message_id:speaker`，私聊场景下同一 QQ 的相同 `message_id` 可能互相顶掉。改为包含 `session_id`。
4. **`retrieve` 的查询拼接触发限长截断**：原先把 `message.text + " " + topic` 直接交给只取前 1000 字符的检索词提取，超长消息会把 topic 整段挤掉。改为各自先限长再加权拼接。
5. **`MemoryService.close()` 的裸 `except TimeoutError`**：在部分运行时不会捕获 `asyncio.TimeoutError`，改为同时捕获两者并记录待冲刷队列长度。

---

## 追加：双 agent、对话压缩与记忆归档

D1–D8 之后的工作（判定/回复拆分、对话压缩、记忆归档）又暴露出这些问题，已一并处理：

1. **固定尾部窗口是错的设计**（自造问题）。回复段一度实现为"最近 `REPLY_TAIL` 条"，并声称"prompt 有界、前缀稳定"。实测相反：尾部每轮滑掉一条，前缀每轮断裂，受控对比里缓存命中率与单 agent 相同（43.3% → 40.4%）。改成"摘要之后累积的活窗口"后，同一测量下非压缩轮的平均前缀复用率为 98.2%（该次为缩短周期用 `LIVE_TARGET=40, COMPACT_KEEP=10` 测量）；按真实常量 `LIVE_TARGET=500, COMPACT_KEEP=50` 复测 1500 轮：6 次压缩、6 次断裂、非压缩轮平均复用率 99.8%。回归用例在 `tests/test_dual_agent_comparison.py`。另外发现自己的 `_format_history` 用位置编号 `index` 也会破坏前缀，改为在有 `seq` 时按绝对序号编号。
2. **压缩抖动**：压缩后窗口仍然满着，下一轮立刻再压一次——实测 560 轮触发 310 次压缩，摘要每轮都变。改为压缩成功后把已摘要的消息移出窗口（`mark_compacted`），窗口重新累积。
3. **判定输出被 prompt 里的示例回声覆盖**：`_extract` 原先取**第一个**标签匹配，模型复述示例时会取到示例而不是真实判断。改为取最后一个匹配。
4. **判定输入被重复转义**：判定 agent 收到的是已经转义过的文本，转义字符进了它的视野。改为先 `unescape` 再统一转义一次。
5. **记忆库版本迁移是死代码**：`SCHEMA_VERSION` 升到 2 后，判断写成 `version not in {0, SCHEMA_VERSION}`，于是 v1 库仍被当作"版本不符"整体重建，归档表根本不会被迁移到。改为 `{0, 1, SCHEMA_VERSION}`。
6. **零相关检索仍注入兜底记忆**：查询词存在但库里没有任何命中时，`retrieve` 会退回按类型权重排序的记录——实测"我今天有点累"检索到 `group_reply_trigger_rules` 这类系统规则记忆，等于每轮提醒她是程序。改为无命中时返回空。

另有一处**非代码问题**：离线测试会读取项目 `.env`，而 `.env` 里为排查注入问题开着 `QQBOT_DEBUG_MODEL_IO=1`，于是跑一次测试会把上千条合成 prompt（含 system prompt 全文）追加进 `data/traces/model_trace.jsonl`，把真实追踪淹掉。已在 `tests/run_offline.py` 里把该开关显式置 0。

---

## 验证方式

```powershell
.\run_offline_tests.bat
```

当前结果：**396 个用例全部通过**，输出 `ALL_OFFLINE_TESTS_PASSED`。测试全程离线：不访问真实模型、不连接 QQ、不发送外部消息。

测试的临时目录位于项目内的 `.tmp_test_run/`，每个测试模块使用带 pid 与 uuid 的独立子目录，并在进程退出时通过 `atexit` 整体回收；运行结束后除一个空目录外不留任何残留。历史遗留的 ACL 锁定目录已清理完毕，对应的 `cleanup_locked_temp.ps1` 已删除。

代码静态检查（`python -m pyflakes src tests archive/qq_roleplay_bot_legacy`）返回 0 问题；顺带修掉了归档 `main.py` 里 `logger` 未定义（会抛 `NameError`）以及断连后 `continue` 变成忙等待的两个问题。
