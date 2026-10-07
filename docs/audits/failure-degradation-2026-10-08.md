# 失败与降级审计 · 2026-10-08

> 2026-10-08 已修：失败计数与出口（[1][7][8][9]，提交 3ea9a48——判定/回复的模型调用失败接上 `record_model_error` + `_stats` + `/super status` 一行；记忆那行拼上失败数；扩展收集失败补计；`memory_degraded` 取值恒 0，**按审计给的第二条路删掉**）。

范围：`dev/src/qq_roleplay_bot/` 与 `dev/tests/`，分支 main。
方法：只读。未改源码、未跑测试、未碰进程、未碰 `run\`。
标注规则：**实测** = 读到的文件与行号；**推断** = 由代码结构推出、未在运行中复现。

## 一句话结论

这一区的失败路径**基本都留了日志**，但**判定与回复这两条最要命的失败只在日志里**——面板与计数上一个字都看不到；同时一轮对话**没有总超时**，而主循环是全局串行，所以模型一卡住，整个 bot（含 ping 与管理命令）跟着停摆最长约 9 分钟，而 `/super status` 上看起来一切正常。

## 这一区最该修的三件

1. **判定/回复失败在计数与面板上完全不可见**——`metrics.record_model_error` 定义了却零调用，`_stats` 里没有失败键，`/super status` 也不印失败行。她"突然不说话"时你只能去 grep 进程日志和 `data/logs/judge.jsonl`。这是本区唯一同时命中"她会不会突然不说话 + 你查不查得到"的问题。
2. **一轮对话没有总超时，而主循环是全局串行**——单次 `complete()` 最坏约 181 秒（60 秒 × 3 次 + 退避），一轮里最多三次（压缩 + 判定 + 回复）。模型卡住时整个 bot（含 ping、管理命令、私聊）一起停，最坏约 9 分钟。
3. **"同一时刻只跑一个巡检"这道锁不成立**——它是个共享布尔，`tick()` 无条件 `begin_sweep()`/`end_sweep()`，会把接收路径正在持有的守卫**提前清掉**，于是两条任务可以同时进 `engine.handle()`，并且 `focus.hot` 会被半路改写。

## 10 条清单

### 1. 判定失败回落沉默，但引擎不记任何失败计数（高）

**现象**：判定那次模型调用超时/报错时，她沉默（刻意 fail-closed），但引擎只写日志，不记任何计数，面板上看不出来。

**证据**：`stage3_main.py:4514-4524`（实测）

```
try:
    raw = await self._judge_for(message.session_id).complete(request)
except Exception as exc:
    summary = exc.safe_summary() if isinstance(exc, LLMError) else type(exc).__name__
    self._log_model_io("judge", request, "", ..., error=summary)
    logger.error("Stage 3 judge call failed: ... kind=%s ...", summary)
    return JudgeVerdict(should_reply=False)
```

配套实测：`metrics.py:131-135` 里 `record_model_error(kind)` 只有定义；全 `dev/` 搜索只有 `tests/test_persistence_and_metrics.py:219` 调它 → **源码零调用**。`snapshots.py:34-109`（`EngineSnapshot`）没有 judge/reply 失败字段；`stage3_main.py:2739-2765` 的 `/super status` 只印 `model_calls` / `judge_calls` / 耗时，不印失败。`stage3_main.py:1130-1155` 的 `_stats` 键里也没有任何 `*_failed`。判定失败**有**日志（进程日志 ERROR + `data/logs/judge.jsonl` 带 `error=`），**没有**计数、面板行或告警。

**影响**：判定一挂，她就在群里彻底沉默（这是刻意的 fail-closed，`tests/test_dual_agent.py:114` 把"沉默"钉住了，但没钉"看得见"）。你翻 `/super status` 看到的是"判定调用 N 次"，与成功时长得一模一样——**查不到原因**。

**最小改法**：在 4516 的 `except` 里补 `self.metrics.record_model_error(summary)` 与 `self._stats["judge_failed"] += 1`（键加进 1130 那份名单）；`snapshots.EngineSnapshot` 加 `judge_failed: int = 0`，`_super_status` 仿 `empty_forced_reply`（2800-2801）加一行"异常：判定挂掉 N 次"。回复那一路同改（`stage3_main.py:3968-3981`，同样的 `except` 只 log 后 `raise`）。

**可调项与默认值**：不需要调参；建议阈值——`judge_failed` 连续 ≥3 就在 status 里升级为"异常"行。

**严重度：高**——理由：它是"她突然不说话"与"你查不到"两件事的唯一交汇点，且修法只加计数、不动判定语义，代价最低。

### 2. 一轮对话没有总超时，主循环串行 → 模型卡住 = 全 bot 停摆（高）

**现象**：模型服务端不是拒绝而是"拖着不回"时，整条收消息流程停住，最坏约 9 分钟，期间新消息（含命令、私聊）一条都进不来。

**证据**：`runtime.py:2436-2440` 与 `2455-2457`（实测）

```
for index, (item, _is_command) in enumerate(ordered):
    backlog = index < len(ordered) - 1
    if await _handle_one(item, backlog=backlog):   # 串行，无并发
        return True
...
reply = await engine.handle(item)                  # 没有 wait_for，没有总预算
```

`llm_client.py:168`（`timeout: float = 60.0`）、`186`（`max_attempts`），`232-251` 重试与退避；`265-268` 判定 timeout/transport 错误**可重试**（`socket.timeout` 走 L317 的 `transport_error`）。单次 `complete()` 最坏 ≈ 3 × 60 秒 + 退避（`_backoff_delay` 两次实测上限约 1.5 秒）≈ **181 秒**；一轮里最多三次这种调用（`_maybe_compact` 失败一批 `break`（4386-4391）+ 判定 + 回复）≈ **9 分钟**。判定 + 回复两趟就是 ≈ 363 秒。

**影响**：模型服务端"挂着不回"（不是拒绝，是拖）时，`receive_loop` 卡在这里，**新消息（含 ping、`/admin`、`/super restart`、私聊）一条都进不来**；`focus_loop`、`outbox_loop` 还在跑，所以日志看起来"进程活着"。这是最像"服务卡住"的一条。

**最小改法**：给 `_handle_one` 里的 `engine.handle` 包一层总预算：`await asyncio.wait_for(engine.handle(item), timeout=TURN_TIMEOUT_SECONDS)`，超时按 `logger.error` + `_stats["turn_timeouts"] += 1` 收尾并返回 None（超时会取消任务，回复就是没有——与现在的异常路径同结果，但有了上界）。

**可调项与默认值**：`TURN_TIMEOUT_SECONDS` 建议 **90**（实测判定约 2 秒、回复约 10 秒，90 秒已是 9 倍余量）；同时把回复/判定 client 的 `max_attempts` 从 3 降到 **2**、`timeout` 从 60 降到 **45**（单次上界 181 → 91 秒）。

**严重度：高**——理由：它是"服务会不会卡住"的直接答案，也是唯一会让**无关功能（命令、私聊）一起失效**的路径。

### 3. "巡检锁"是个共享布尔，会被另一条任务提前释放（中）

**现象**：设计写着"同一时刻只允许一个群在巡检"，但这条守卫由两条任务共用、任一方结束就清掉，因此并发的 `engine.handle()` 是可能的。

**证据**：`stage3_main.py:3743-3757`（接收路径，实测）

```
if group_chat and not allowed_private and not resumed and self.focus.hot is None:
    if self.focus.sweeping:
        self._stats["deferred_messages"] += 1
        return None
    self.focus.begin_sweep()
    swept = True
try:
    return await self._model_path(...)          # 长时间持锁（含模型调用）
finally:
    if swept:
        self.focus.end_sweep()
```

`stage3_main.py:4242` / `4269`（tick 路径，实测）：`tick()` **不检查** `sweeping` 就 `self.focus.begin_sweep()`，`finally: self.focus.end_sweep()`；中间 `4258` 是 `_force_compact`（模型调用），`4262-4263` 在 `hot is None` 时 `take_next_session` → `4267` `_serve_queue` → `await self.handle(...)`。
结果（**推断**，机制是实测的）：接收路径还卡在 `_model_path` 里时，5 秒一跳的 tick 只要跑一次就会把 `sweeping` 清成 False，于是紧随其后的新消息认为自己可以巡检 → 两条任务并发进 `engine.handle()`；并且接收路径收尾时 `4139-4144` 的 `self.focus.acquire(...)` 会**覆盖** tick 刚设好的 `focus.hot`。

**影响**：设计明确写着"同一个会话的并发处理是必须避免的"（4240-4241 注释），而这条守卫挡不住。触发要"有别的群在排队 + 当值群正好在调模型"，多群使用下不罕见。后果是焦点记账串掉、多花一次模型调用（各会话 state 分开，所以不是记忆损坏）。没有任何计数能发现它（`focus_stats()` 里的 `sweeping` 只是当前值）。

**最小改法**：把布尔换成**持有者标记**：`begin_sweep(owner)` 返回是否成功、`end_sweep(owner)` 只清自己那份（或用 `asyncio.Lock` + `locked()`），`tick()` 改为"拿不到就跳过这一跳"。最小一步：`tick()` 开头加 `if self.focus.sweeping: return []`。

**可调项与默认值**：无。

**严重度：中**——理由：后果限在焦点记账与并发调用（会话状态按会话分开），不损坏记忆；但它是这一区唯一自称"锁"的东西，而它不成立，所以要显式写下来。

### 4. `compaction_enabled` 是死开关，面板上标着"live"（中）

**现象**：面板有一个"对话压缩"开关，说明写着关掉就不再压缩；但它没有任何消费者，拉不动。

**证据**：`runtime_flags.py:31` 与 `99-100`、`operator_config.py:46`（实测）

```
"compaction_enabled": "对话压缩。关掉＝长对话不再压成摘要，靠活窗口滚动。",
...
@property
def compaction_enabled(self) -> bool:
    return self.get("compaction_enabled")
```

全 `dev/` 搜索 `compaction_enabled` 只有 **5 处，全是定义/说明**（`operator_config.py:46`、`runtime_flags.py:31/99/100/144`），**没有任何消费者**。对照：`vision_enabled` 在 `stage3_main.py:3780`、`ask_when_unsure` 在 `3811`、`data_marker_enabled` 在 `3817`、`memory_enabled` 在 `memory_maintenance_agent.py:292` 都被读了。`_maybe_compact`（`stage3_main.py:4339-4385`）从头到尾不看开关。

**影响**：压缩模型开始退化（摘要越写越差、或一直超时烧钱）时，面板上那道"拉闸"拉不动——这是这一区本该有的自救手段。**查得到、修得掉的那种"卡"**。

**最小改法**：`_maybe_compact` 开头加 `if not self._flags().compaction_enabled: return`（`flags` 已经是现成的，`_flags()` 在 1500）。

**可调项与默认值**：默认 True，行为不变。

**严重度：中**——理由：不影响她说不说话，但它是"出事时唯一能拉的那道闸"，而它是假的；修法两行。

### 5. 判定调用失败会吞掉"她答应过的稍等"（中）

**现象**：她回过"稍等"、人家在等，此时判定那次调用超时/报错，那句承诺就永远不兑现。

**证据**：`dialogue_judge.py:394-395`（实测，解析路径尊重 must_reply）

```
if must_reply:
    return JudgeVerdict(should_reply=True, **common)
```

对照 `stage3_main.py:4524`：**异常路径**直接 `return JudgeVerdict(should_reply=False)`，**不看 `must_reply`**。而 `3820-3852` 只用 `if not verdict.should_reply: return None` 就返回了。承诺登记在 `3674`（`self._promised_ids.add(...)`），排队路径 `4291-4305` 只记 `focus_declined_promised` + 一条 warning。

**影响**：她回过"稍等"、人家在等，这时判定那次调用**超时/报错**（不是输出没解析——解析那条路是 fail-open 的），那句承诺就永远不兑现。有 warning 和计数，所以"查得到"，但**她不说话**。

**最小改法**：`_judge` 的 `except` 分支改成 `return JudgeVerdict(should_reply=bool(must_reply))`——"已经答应过"应当优先于"判定这次没答上来"，与 `parse_judge_output` 同一条口径。

**可调项与默认值**：无。

**严重度：中**——理由：这是"她主动承诺过却失约"，比单纯不接话更伤，但触发条件是判定调用失败 + 该条曾被 promise，覆盖面有限，且已有 warning 计数。

### 6. 压缩没有熔断：失败后每一轮都拿同一批重试一次（中）

**现象**：压缩失败不再压，但下一轮还会拿同一批再试一次，没有退避也没有熔断。

**证据**：`stage3_main.py:4374-4391`（实测）

```
for batch in batches[:MAX_COMPACT_BATCHES_PER_TURN]:
    ...
    try:
        raw = await client.complete(request)
    except Exception as exc:
        logger.error("对话压缩失败，这一段历史保持原样: session=%s kind=%s", session_id, summary)
        break
```

`break` 只结束这一轮；`state.mark_compacted`（4401）没跑，所以 `compaction_pending`（`stage3_runtime.py:320`）下一轮仍返回同一批 → 每一条新消息都先白烧一次压缩调用。`MAX_COMPACT_BATCHES_PER_TURN = 6`（119-121）、`LIVE_TARGET = 500`、`COMPACT_KEEP = 50`。全仓库**没有熔断**（搜 `熔断`/`circuit`/`disabled_until` 零命中）。成型的重试上限只在：`llm_client.py:19-21/232-251`（3 次、指数退避+抖动、只重试 transport 与 408/425/429/5xx）、`outbox.py:28-32`（3 次 / 600 秒 / 20 条）、`memory_inbox.py:62-70`（3 次、0.2/0.4 秒退避）、`onebot_client.py:239-251`（重连 5 秒 → 60 秒封顶）、`memory_store.py:199/1142`（`MAX_REPLAY_ATTEMPTS = 2`）。这些**都有退避**，是好的。

**影响**：长对话 + 压缩模型一直挂 = 每条消息白烧一次调用、`compaction_calls` 一直涨但摘要永不更新（她的长对话上下文静默退化）。日志里是 ERROR，面板上没有（见第 1 条）。

**最小改法**：在引擎上加一个小熔断：连续 3 次压缩失败 → 5 分钟内不再尝试压缩（`self._compaction_breaker_until`），并把跳过记 `_stats["compaction_skipped"]`。

**可调项与默认值**：建议 `连续 3 次 / 冷却 300 秒`。**不要**给判定和回复加熔断——那两条路的正确行为是继续尝试（失败只等于这一句没答）。

**严重度：中**——理由：白烧调用 + 上下文静默退化是慢性的，但不会让她突然不说话，也不会卡服务。

### 7. 记忆写入/检索失败不上面板（中）

**现象**：记忆读写失败的计数存在于记忆服务内部，但 `/super status` 只印队列深度，不印失败数。

**证据**：`memory_service.py:60-63` / `96-98` / `114-117`、`memory_inbox.py:73-75`（实测，三处都 `self.metrics.failures += 1` + `logger.warning`），而 `stage3_main.py:2826-2828` 只印队列深度：

```
if self.memory_service is not None:
    memory = self.memory_service.snapshot()
    lines.append(f"记忆：已入队 {memory.get('queued_events', 0)} 条待处理")
```

对照同一函数里金句（`2838-2839` "没跑成 N 次"）、黑话（`2859-2860` "没记成 N 次"）、写信（`2983` "失败 N 次"）**都印了失败数**。

**影响**：记忆库读不到 / 写不进去时，她不提以前的事、或者你说过的话没记住——`/super status` 上"记忆：已入队 0 条"看起来完全正常。**查不到**。

**最小改法**：2828 那一行拼上 `failures`（和 `memory_degraded` 若接上），与金句那行同一口径。

**可调项与默认值**：无。

**严重度：中**——理由：它决定"她忘了"这件事能不能被看见，但失败本身不会让她不说话。

### 8. 扩展材料收集失败不计入 `extension_failures`（低）

**现象**：知识库/插件材料拿不到时只写日志，不进 `extension_failures` 指标（只有"事后通知"那一步才计）。

**证据**：`stage3_main.py:3857-3864` 与 `4145-4152`（实测）

```
except Exception:
    logger.exception("Stage 3 extension collection failed; continuing without extension data")
    prompt_material = PromptMaterial()          # ← 没有 record_extension_failure()
...
except Exception:
    logger.exception("Stage 3 extension notification failed")
    self.metrics.record_extension_failure()     # ← 只有这一处计
```

**影响**：知识库/插件材料整段时间拿不到（"她变笨了"）时，`extension_failures` 仍是 0，会把"退化了"读成"没事"。低，因为只影响知识注入、不影响她说不说。

**最小改法**：3863 之后补 `self.metrics.record_extension_failure()`。

**可调项与默认值**：无。

**严重度：低**——理由：只影响知识注入质量，不影响回复是否发出。

### 9. `memory_degraded` 是死指标（低）

**现象**：`RuntimeMetrics.memory_degraded` 有定义、进快照、上面板，但源码里没有任何地方写它。

**证据**：`metrics.py:113` 与 `145-147`（实测）；全 `dev/` 搜索 `record_memory_degraded` 只有这一处定义，**零调用**；它照旧进 `snapshot()`（157）与面板。

**影响**：不影响她说话，但面板上一格永远 0 会让人以为"记忆从没降级过"。与第 4 条同类：**声明的可观测面没有消费者**。

**最小改法**：要么在 `memory_service.py:60/95/114`（或 `memory_inbox.py:73`）顺手调它，要么删掉这个字段——不要留一个永远为 0 的数。

**可调项与默认值**：无。

**严重度：低**——理由：纯粹是误导性的零值，无功能后果。

### 10. 记忆维护的串行锁内有无超时的 `to_thread`，锁理论上可永不释放（低，推断）

**现象**：记忆维护那条后台通道持锁时调用七处无超时的线程内 SQLite；其中任一处真挂住，锁就再也不释放，维护永久停止且不再有日志。

**证据**：`memory_maintenance_agent.py:185`（`self._lock = asyncio.Lock()`）、`202-205`（`if self._lock.locked(): return 0` + `async with self._lock:`）与块内 `209/214/224/225/231/251/280`：七处 `await asyncio.to_thread(self.store.*)` **都没有 `wait_for`**；只有 `235` 的模型调用有上界 `asyncio.wait_for(self.client.complete(request), self.timeout)`（`timeout=45.0`，`memory_config.py:32`）。

**影响（推断）**：某次 `to_thread` 里的 SQLite 调用真挂住（不是抛错）时，`asyncio.Lock` 永不释放，`run()` 每一跳都命中 `if self._lock.locked(): return 0`，于是**记忆维护永久停止且不再有任何日志**——只有"记忆不再更新"。缓解事实：`memory_store.py:322/415` 用 `sqlite3.connect(..., timeout=0.1)`，正常情况是抛错而不是挂住，所以给低。

**最小改法**：把 `claim` / `commit` / `release` 也包上界，如 `await asyncio.wait_for(asyncio.to_thread(...), MEMORY_DB_TIMEOUT)`，超时按失败走既有 `except`（`272-275`，它 `break` 到下一跳）。锁就有了上界。

**可调项与默认值**：`MEMORY_DB_TIMEOUT` 建议 **10** 秒（租约是 `self.timeout + 30`，不会被误伤）。

**严重度：低**——理由：需要"线程内调用真挂住"这种少见情形；但它是"锁可能永不释放"的唯一落点，且一旦发生完全无痕，所以不删。

## 这一区没问题的部分

（照留，防重复劳动。）

- **审核那条路是这一区做得最好的**：`style_reviewer.py:275-283` 超时/报错 `failed += 1` 后按**通过**处理、`255-268` 输出认不出**重试一次**再放行、`314-315` 计数进快照，`stage3_main.py:2802-2813` 把 `review_failed` / 未识别数都印出来。有超时（`REVIEW_TIMEOUT_SECONDS = 8.0`）、有上限、有明确降级。
- **发送那条路有分类、有补发、有计数**：`onebot_ws.py:520-549` 三分类（`MessageNotDelivered` / `DeliveryUncertain` / `DeliveryRejected`），`outbox.py:105-162` 三道上限 + `flush` 顺序保证，`stage3_main.py:4720-4729` 两分支都 `record_send_failure()` + log。
- **记忆检索有超时**：`memory_service.py:54-55/93-94/112-113` 都是 `wait_for(..., 0.3)`，`close()` 里 `inbox.flush` 也 `wait_for(3.0)`（166-168）。
- **重连有指数退避**：`onebot_client.py:239-251`（5 秒 → 60 秒封顶）。
- **没有"无限重试"**：`memory_maintenance_agent.py:272-275` 失败即 `break`，`quote_learning.py:933-936` 失败即 `break`，`background_plugins.py:82-90` 每跳一次、失败只记日志。
- **没有拿锁不放的异常路径**：全仓库只有 6 个锁（`metrics.py:80/107`、`memory_maintenance_agent.py:185`、`memory_store.py:307`、`quote_learning.py:847`、`runtime_flags.py:56`），全部走 `with` / `async with`，异常必释放。
- **关键路径的 `except` 基本都留痕**：脚本扫了全树的单行 `except: <body>`，没有"裸 `pass` 无日志"的关键路径；剩下的无声兜底都是配置解析默认值（`dev_config.py:276/283`、`memory_config.py:13/21`、`feature_log.py:82` 等），无害。

## 证据与量法

**实测**（读到的文件与行号，未改动、未运行）：
- 第 1、2、4、5、6、7、8、9 条的全部证据。
- 第 3 条的机制部分：`stage3_main.py:3743-3757`（接收路径 begin/end）、`4242`/`4269`（tick 无条件 begin/end）、`4262-4267`（tick 里 `take_next_session` → `_serve_queue` → `handle`）、`4139-4144`（收尾 `focus.acquire`）、`runtime.py:2436-2440`（接收循环串行）。
- 第 10 条的机制部分：`memory_maintenance_agent.py:185/202-205/209/214/224/225/231/251/280/235`。

**推断**（未在运行中复现，需补离线用例确认）：
- 第 3 条的**发生**：需要"有别的群在排队 + 当值群正好在调模型"才撞上；建议动手前补一个"两条任务同时进 `handle()`"的离线用例来确认，不要只凭读码。
- 第 10 条的**发生**：需要线程内调用真挂住（而不是抛错）；缓解事实已写在该条内。

**量法说明**：所有"零调用"/"没有消费者"的结论都用全 `dev/` 目录的文本搜索得出（例如 `record_model_error`、`record_memory_degraded`、`compaction_enabled`）；所有"没有总超时"的结论用 `wait_for` / `timeout` 的全目录搜索得出（命中的只有扩展收集 2 秒、扩展通知 2 秒、审核 8 秒、记忆检索 0.3 秒、压缩租约 45 秒、传输层 15 秒，**没有一处包住 `engine.handle`**）。

**时间上界的算法**：`llm_client.py:168` 的 `timeout = 60.0` 是**每次尝试**的上界；`186` 的 `max_attempts` 默认 3；`270-274` 的退避是 `min(8.0, 0.5 × 2^(attempt-1)) × (0.5 + rand/2)`，两次合计不超过约 1.5 秒。故单次 `complete()` 最坏 ≈ 181 秒。一轮里会走这条路的调用最多三次：压缩（失败一批即 `break`）、判定、回复。

## 我没做到的

- 未跑任何测试、未跑 pyflakes、未起进程、未改源码、未碰 `run\`。
- 第 3 条与第 10 条的**发生**是推断，不是实测（机制部分是实测）。
- 只审了"失败与降级"这一个分区；已知并按要求跳过的几项（`dropped` 不可见、链接消息被正则误判、disable 静默删待处理、时间用 monotonic、检索 2 字滑窗、令牌边界只覆盖回复段）**没有**在本文重复。
- 判定失败的"沉默"本身是刻意设计，且被 `tests/test_dual_agent.py:114` 钉住——本文建议改的是"看得见"，**不是**"要不要沉默"。
