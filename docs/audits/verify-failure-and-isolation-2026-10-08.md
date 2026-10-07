# 核查报告 · 失败降级 + 隔离增长

核查者：独立复核（只读源码 + 只读文件大小 + 一次 `set.pop()` 行为实测）。
被核查对象：`docs/audits/failure-degradation-2026-10-08.md`（提交 `6ca6b17`）与
`docs/audits/isolation-growth-2026-10-08.md`（提交 `82e831e`）。
核查时的树：`main`，`HEAD = a52ae55`。
**先说一句方法学上的前提**：`git diff --stat 82e831e HEAD -- src tests` **无输出**，
也就是说自 `82e831e`（含更早的 `6ca6b17`）之后 `src/` 与 `tests/` **一行没动**。
所以两份文档的 `文件:行` 可以按当前树逐条核对，不存在"行号已经漂了"的借口。

## 一句话结论

两份都**基本可信**，且都属于"行号真的核过"的那一类（不是编的）：
失败降级约 **8 成可信**（10 条里 7 条站得住、1 条证据不全、1 条最简改法不是最简、
1 条同类漏项），隔离增长约 **8.5 成可信**（10 条里 9 条站得住、
唯一实质错处是 [8] 的"`_cleanup` 里没有"说反了，它是**有** `DELETE` 的例外写法）。
**必须更正的有一处**：隔离增长 [8] 的 `_cleanup` 说明与源码不符。
**两处最该补的"漏"**：隔离增长漏了 `_follow_ups`（按会话只增不减，
与本区主结论同类）；失败降级漏了 `send_failures` / `extension_failures`
**同样"有数据、没出口"**（它把不可见性只归给第 1、7 条，弱化了"唯一交汇点"的说法）。
其余偏差都是"引用不精确"（行号差几行、引文改写、把两段不连续的代码拼成一段），
不影响结论方向。

---

## 逐条核对结果 · 一、`failure-degradation-2026-10-08.md`

### 第 1 条 判定失败不记任何计数（高）

- **文档原话**：`stage3_main.py:4514-4524` 的 `except` 只 log 后 `return JudgeVerdict(should_reply=False)`；
  `metrics.py:131-135` 的 `record_model_error` **源码零调用**（只有 `tests/test_persistence_and_metrics.py:219` 调它）；
  `snapshots.py:34-109` 无 judge/reply 失败字段；`stage3_main.py:2739-2765` 不印失败；`1130-1155` 的 `_stats` 无 `*_failed`。
- **我的核对**：
  - `stage3_main.py:4514-4524` **逐字对上**（实测，read）。`return JudgeVerdict(should_reply=False)` 就在 4524。
  - `record_model_error` 定义在 `metrics.py:131-135` **对上**。全树 `grep record_model_error`
    命中 3 处：`metrics.py:131`（定义）、`tests/test_persistence_and_metrics.py:219`（调用）、
    以及本文档自己。**源码目录 `src/` 内调用数为 0** —— 这一条成立。
    但文档措辞有两处不严谨：(a) 正文说"只有 `metrics.py:131-135` 里只有定义"，
    而摘要与"最该修的三件"直接写"**定义了却零调用**"——**测试里有 1 处调用**，
    按文档自己的"量法说明"（"全 dev/ 搜索"）应该写成"源码零调用、测试 1 处"；
    (b) 这不是纯死代码：它进 `snapshot()`（`metrics.py:154`），有测试钉住
    （`tests/test_persistence_and_metrics.py:227`）。
  - `_stats` 名单在 `1130-1155` **对上**，确实没有任何 `*_failed`。
  - `_super_status` 在 `2739-2765`（`snapshot = self.snapshot()` 起、`判定调用：` 那行止）**对上**，
    确实只印 `model_calls` / `judge_calls` / 耗时。
  - **比文档更进一步（这是我加的）**：`2739-2740` 已经把整份 `metrics` 取到手了，
    但只用了 `metrics["model_latency"]`（`2741`）。也就是说
    `metrics["model_errors"]` / `send_failures` / `extension_failures` / `memory_degraded`
    **在同一行代码里就已经可读**，只是没人读；本树上没有面板可看（`webui_panel.py` 不在本分支，
    全树 `snapshot.metrics` 只有 `2740` 一个消费者）。
- **判定：对（现象与行号）**；**措辞需要收紧**（"零调用"应写"`src/` 零调用、测试 1 处"）。
- **证据**：`stage3_main.py:4514-4524`、`stage3_main.py:1130-1155`、`stage3_main.py:2739-2765`、
  `metrics.py:131-135`、`tests/test_persistence_and_metrics.py:219`；
  命令：`Select-String -Pattern record_model_error -Path src,tests -Recurse`。

- **最小改法是否真的最小：不是最优。** 文档让加两处新管道：
  `self._stats["judge_failed"]`（键 + `EngineSnapshot` 字段 + `_super_status` 一行）
  **以及** `self.metrics.record_model_error(summary)`。
  而 `record_model_error` 那条路**已经有出口**（`metrics.snapshot()["model_errors"]`），
  所以真正最小的是两步：(1) 在 `4516` 的 `except` 里补一句 `record_model_error`；
  (2) 在 `_super_status` 的 `2740` 后面把 `metrics["model_errors"]` 印出来。
  这样**不必**动 `snapshots.py`、不必加 `_stats` 键。
  文档的连带点也点到了（它明说了要加 `EngineSnapshot` 字段），所以**没有漏**，
  只是**多绕了一道**：新增 `_stats` 键会经 `**self._stats`（`1667`）灌进 dataclass,
  而 `EngineSnapshot` 是 `frozen=True, slots=True`（`snapshots.py:33-34`），
  字段必须在 34-109 里声明——这一层它说对了。
- **严重度是否随口给**：不是。第 1 条的理由（"她突然不说话"与"你查不到"的交汇点、
  修法只加计数、不动判定语义）与证据一致，"高"给得住。

### 第 2 条 一轮没有总超时、主循环串行（高）

- **文档原话**：`runtime.py:2436-2440` / `2455-2457` 串行且无 `wait_for`；
  `llm_client.py:168` `timeout=60.0`、`186` `max_attempts`、`232-251` 重试退避、
  `265-268` timeout/transport 可重试（`socket.timeout` 走 `317`）；
  单次最坏 ≈ 181 秒；一轮最多三次 ≈ 9 分钟。
- **我的核对**：
  - `runtime.py:2436-2440` **对上**（`for index, (item, _is_command) in enumerate(ordered)` +
    `if await _handle_one(item, backlog=backlog): return True`）；`2456`
    是 `reply = await engine.handle(item)`（实测，read）。
    **但引文不诚实**：文档把 2436-2440 与 2455-2457 拼成一段连续的 `for` 循环代码块——
    它们中间隔着 `_handle_one` 的定义（2442-2474）。结论不受影响。
  - 全树 `wait_for` / `asyncio.timeout` 搜索**没有一处包住 `engine.handle`**（实测）：
    生产代码里只有 `memory_maintenance_agent.py:235`、`memory_service.py:54/93/112/166`、
    `onebot_client.py:180/214/234`、`onebot_ws.py:492/521/542`、
    `quote_learning.py:954`、`stage3_main.py:3858/4146`、`style_reviewer.py:279`。
    命中里**没有** `engine.handle` 或 `runtime.py` 的接收循环。
  - `llm_client.py:168`（`timeout: float = 60.0`）、`171`（`max_attempts: int = DEFAULT_MAX_ATTEMPTS`）、
    `19`（`DEFAULT_MAX_ATTEMPTS = 3`）、`20-21`（`0.5` / `MAX_BACKOFF_SECONDS = 8.0`）、
    `232-251`（重试 + `await asyncio.sleep(delay)`）、`265-268`（`_is_retryable`）、
    `317`（`except (urllib.error.URLError, socket.timeout, TimeoutError)` → `transport_error`）
    **全部对上**。文档把 `max_attempts` 的行号写成 `186`，实测**该行是 `self.max_attempts = max(1, int(max_attempts))`**，
    真正带 `DEFAULT_MAX_ATTEMPTS` 默认值的是 `171`——**小错，差一行**。
  - 算术重算（`270-274`：`min(8.0, 0.5 * 2**(n-1)) * (0.5 + rand/2)`，两次退避）：
    3 次尝试 × 60 秒 + 两次退避 ≤ 1.5 秒 ≈ **181.5 秒**，与文档的 181 秒一致（**算对**）。
    一轮最多三次这类调用也成立：压缩（`4385`）+ 判定（`4515`）+ 回复（`3967`），
    三次相加 ≈ 544 秒 ≈ 9 分钟，文档的"≈9 分钟"取的是"三次都最坏"的写法，
    与它自己后一句"判定 + 回复两趟 ≈ 363 秒"并列时**略含糊**，但数字本身不错。
  - `timeout=60` 是**每次尝试**的上界这点也对：`_urlopen(request, timeout=self.timeout)`（`307`）
    在 `_complete_sync` 里，被 `complete` 的循环调用（`235`）。
- **判定：对（现象、算术、"没有一处包住 engine.handle"）**；行号 186 → 171 需更正。
- **证据**：`runtime.py:2436-2440`、`2456`、`2467`；`llm_client.py:19-21/168/171/232-251/265-268/317`；
  命令：`Select-String -Pattern 'wait_for|asyncio\.timeout' -Path src -Recurse`（13 个文件、无 `engine.handle`）。
- **最小改法**：`asyncio.wait_for(engine.handle(item), timeout=TURN_TIMEOUT_SECONDS)` —— **确实最小**，
  且 `asyncio` 已在 `stage3_main` 之外也被 `runtime.py` 使用（`runtime.py:2439` 就是 `await`），
  没有漏掉的连带点。文档建议同时把 `max_attempts` 3→2、`timeout` 60→45 **属于额外的调参建议**，
  与"加总超时"是两件事，写在同一段可以，但别当成同一个改动。
- **严重度**："高"给得住——它是唯一会让**无关功能**（命令、私聊）一起失效的路径，文档理由成立。

### 第 3 条 "巡检锁"是共享布尔、会被提前释放（中）

- **文档原话**：接收路径 `3743-3757` 持锁；`tick()` 在 `4242`/`4269` **不检查** `sweeping`
  就 `begin_sweep()` / `finally end_sweep()`；`4258` 是 `_force_compact`，
  `4262-4263` `take_next_session`，`4267` `_serve_queue`；`4139-4144` 的 `focus.acquire` 会覆盖 `focus.hot`。
- **我的核对**：
  - `3743-3757` **逐字对上**（`if self.focus.sweeping: ... return None` / `begin_sweep` / `try/finally end_sweep`）。
  - `4242` `self.focus.begin_sweep()`、`4269` `self.focus.end_sweep()` **对上**（实测，read）；
    两者之间**确实没有** `if self.focus.sweeping` 判断。
  - `4255` `self.focus.release()`、`4258` `await self._force_compact(...)`、
    `4262` `if self.focus.hot is None:`、`4263` `switched = self.focus.take_next_session(moment)`、
    `4267` `deliveries.extend(await self._serve_queue(switched, moment))` **逐行对上**。
  - `4139-4144` 是注释 + `if not self.focus.is_hot(...): self.focus.acquire(...)`（实测，read）
    ——行号**对上**，而且比文档说的更准：它只在 `decision.kind is REPLY and decision.text`
    分支里执行，也就是说"覆盖 `hot`"这件事确实会发生。
- **因果链复核**：成立，且我能把链条写得更死：
  接收路径在 `3751` 进入 `_model_path` 并**一直持有** `_sweeping=True`；
  5 秒一跳的 `tick()` 在 `4242` 无条件把它设 True、在 `4269` 无条件设 False。
  于是接收路径还在模型调用里时，`_sweeping` 已经是 False，
  下一条消息在 `3744` 读到 False → 通过 → `begin_sweep()` → **两条任务同时在 `_model_path` 里**。
  另外还有一条**文档没说的**同时成立路径：`tick()` 的 `4255` `focus.release()` 与
  接收路径的 `4142` `focus.acquire()` 之间是 await 边界（`4258` 的一次模型调用），
  所以 `hot` 既可能被 `acquire` 覆盖，也可能被 `release` 清掉——两个方向都成立。
- **判定：对**（机制全部实测；"两条任务同时进 `handle()`"是**推断**，文档自己也标了推断，
  这个标注是诚实的）。
- **一处术语不严**：文档说"两条任务可以同时进 `engine.handle()`"，
  但接收路径的守卫在 `handle()` 里（`3743`），`tick()` 那条叫
  `_serve_queue` → `handle()`（`4293`），两条都进 `handle()`，说法成立；
  真正被绕过的那道门是 `3743` 的巡检守卫，措辞可以更精确，但不是错。
- **最小改法**：文档给了两档，"最小一步：`tick()` 开头加 `if self.focus.sweeping: return []`"。
  这一档**确实最小**，而且是安全的（`tick()` 的返回类型是
  `list[tuple[IncomingMessage, list[OutgoingMessage]]]`，空列表合法）。
  但要说清代价：这样会让"释放当值群"也一起被推迟到下一次 5 秒跳，
  文档没提这一点——**这是它这处建议的小缺口**（不是错）。
- **严重度**："中"给得住（它明确说了不损坏记忆、只是焦点记账与多花一次调用）。

### 第 4 条 `compaction_enabled` 是死开关（中）

- **文档原话**：全 `dev/` 只有 5 处，全是定义/说明（`operator_config.py:46`、
  `runtime_flags.py:31/99/100/144`），**没有任何消费者**；`_maybe_compact`（`4339-4385`）不看开关。
- **我的核对**：**逐处对上**。`grep compaction_enabled` 在 `src/` + `tests/` 命中：
  `operator_config.py:46`、`runtime_flags.py:31/99/100/144` —— 一共 5 处，**消费者 0 处**（实测）。
  `_maybe_compact` 在 `4339-4416`（不是 4385，函数结尾在 4416），
  函数体里**确实一次都没读开关**（实测，read 全函数）。
  附带确认"对照"也对：`vision_enabled`→`stage3_main.py:3780`、
  `ask_when_unsure`→`3811`、`data_marker_enabled`→`3817` 三处行号**完全准确**；
  `memory_enabled` 在 `memory_maintenance_agent.py:292`（未逐字读，但该文件
  `allowed_groups` 那条路读标志的写法与文档描述一致）。
- **比文档更准的一句（我加的）**：`operator_config.py:46` 给这个开关配了
  `"applies": "live"`（面板按 live 渲染）和 `"env": "QQBOT_COMPACTION"`，
  而 `runtime_flags.build_flags`（`137-155`）里 `compaction_enabled` 是**硬编码 `True`**（`144`），
  **根本不读 `QQBOT_COMPACTION`**。也就是说这个开关不只是"没人读"，
  它的**环境变量口子也是断的**——`QQBOT_COMPACTION=0` 不会有任何效果。
  这是比文档原话更强的一击。
- **判定：对**，且证据比文档更硬（命令：`grep -n compaction_enabled src tests`）。
- **最小改法**：`_maybe_compact` 开头加 `if not self._flags().compaction_enabled: return`（`119`）
  —— **确实最小**，`_flags()` 是现成的（`1191`/`1478`/`3780`/`3811`/`3817`/`4000` 都在用），
  `asyncio` 不用新增。唯一漏的连带点是上面那句：**想让它真能被拉闸，
  还得在 `build_flags` 的 `144` 把它接上 `QQBOT_COMPACTION`**（或让面板覆盖层写进去），
  否则面板关掉它、重启又变回 True。文档只做了半截。
- **严重度**："中"给得住（自救手段失效，但不会让她不说话）。

### 第 5 条 判定失败会吞掉"稍等"（中）

- **文档原话**：`dialogue_judge.py:394-395` 解析路径尊重 `must_reply`；
  `stage3_main.py:4524` 异常路径不看 `must_reply`；`3820-3852` 用
  `if not verdict.should_reply: return None`；承诺登记在 `3674`，排队路径 `4291-4305`。
- **我的核对**：
  - `dialogue_judge.py:394-395` **逐字对上**（`if must_reply: return JudgeVerdict(should_reply=True, **common)`）。
  - `stage3_main.py:4524` 是 `return JudgeVerdict(should_reply=False)`，**不看 `must_reply`** —— 对上。
  - `3820`-`3852`：`3821` `must_reply = message.message_id in self._promised_ids`、
    `3822-3824` 调用判定、`3851-3852` `if not verdict.should_reply: return None` —— 行号**完全准确**。
  - `3674` `self._promised_ids.add(message.message_id)` —— 准确。
  - `4291-4305`：`4291` `promised = message.message_id in self._promised_ids`、
    `4303-4308` `elif promised:` → `_stats["focus_declined_promised"] += 1` + `logger.warning` —— **准确**。
- **判定：对**（现象、行号、因果链全部实测）。
  唯一的细节：那条 warning 是在**排队排空那条路**上打的（`4293` 调 `handle` 之后），
  如果该群一直没轮到，warning 也不会出现——文档说"有 warning 和计数，所以查得到"，
  这句**在"消息已被消费"的前提下才成立**，应该加这个前提。
- **最小改法**：`return JudgeVerdict(should_reply=bool(must_reply))` —— **确实最小且正确**，
  口径与 `parse_judge_output` 一致。没有漏掉的连带点。
- **严重度**："中"给得住（理由写了覆盖面有限 + 已有 warning 计数）。

### 第 6 条 压缩没有熔断（中）

- **文档原话**：`4374-4391` 失败即 `break`；`mark_compacted`（4401）没跑 → 下一轮同一批；
  `MAX_COMPACT_BATCHES_PER_TURN = 6`（119-121）、`LIVE_TARGET = 500`、`COMPACT_KEEP = 50`；
  全仓库没有熔断。
- **我的核对**：
  - `4374` `for batch in batches[:MAX_COMPACT_BATCHES_PER_TURN]:` **对上**；
    `4384-4391` `try: raw = await client.complete(request) except Exception as exc: logger.error(...) break` **对上**；
    `4401` `state.mark_compacted(boundary)` **对上**。
  - 常量：`121` `MAX_COMPACT_BATCHES_PER_TURN = 6`、`116` `LIVE_TARGET = 500`、
    `118` `COMPACT_KEEP = 50` —— **行号与值全对**。
  - `compaction_pending` 在 `stage3_runtime.py:320` —— **准确**（`332` 是 `compaction_pending_all`）。
  - "全仓库没有熔断"：`grep '熔断|circuit|disabled_until'` 在 `src`/`tests` **零命中** —— 成立。
  - 文档列的"成型的重试上限"我对了其中三处：`llm_client.py:19-21`（对）、
    `outbox.py:28-32`、`memory_inbox.py:62-70`、`memory_store.py:199/1142` 未逐行核
    （`MAX_REPLAY_ATTEMPTS` 在 `memory_store.py:592` 被用，签名一致）——标**未逐字复核**。
- **一处措辞不准（不是错）**：文档说"`state.mark_compacted`（4401）没跑，
  所以 `compaction_pending` 下一轮仍返回同一批"。更准的说法是：
  `mark_compacted` **是每批成功都跑的**（`4401` 在循环内）；失败那一批 `break`
  之后，**它和它之后的批次**没被标记，所以下一轮从**同一批**重新开始
  （前面成功的批次不会重压）。结论方向（每轮白烧一次、摘要不更新）**成立**。
- **判定：对**（常量、行号、无熔断均实测）；上面那句是**精度**问题。
- **最小改法**：加一个小熔断（连续 3 次 / 冷却 300 秒）——"最小"是个**判断**，
  它没有比这更小的做法（因为 `break` 之后没有任何跨轮状态），这一档合理。
  文档自己划了边界（"**不要**给判定和回复加熔断"）——这个边界**是对的**，
  与第 5 条的口径一致（判定/回复应当继续尝试）。
- **严重度**："中"给得住。

### 第 7 条 记忆读写失败不上面板（中）

- **文档原话**：`memory_service.py:60-63` / `96-98` / `114-117`、`memory_inbox.py:73-75`
  三处 `self.metrics.failures += 1` + `logger.warning`；`stage3_main.py:2826-2828` 只印队列深度；
  对照金句 `2838-2839`、黑话 `2859-2860`、写信 `2983` 都印失败数。
- **我的核对**：
  - `memory_service.py:61`（`self.metrics.failures += 1`，在 `retrieve` 的 `except` 里）、
    `96`（`identity_for`）、`115`（`profile_for`）**逐处对上**，
    文档给的区间（60-63 / 96-98 / 114-117）**包住了这三处**。
  - `stage3_main.py:2826-2828` **逐字对上**（`if self.memory_service is not None:`
    → `memory = self.memory_service.snapshot()` → `lines.append(f"记忆：已入队 ... 条待处理")`）。
  - `2838-2839`（金句"没跑成 N 次"）**对上**；`2859-2860` / `2983` 未逐字读，标**未复核**。
  - `memory_inbox.py:73-75` 未逐字读，标**未复核**（该文件 `74` 行有 `failures += 1`，
    与我的 `grep` 命中一致）。
  - **我加的**：`memory_service.snapshot()`（`memory_service.py:156`）返回的是
    `{"enabled", "queued_events", **asdict(self.metrics)}`——**`failures` 已经躺在同一个 dict 里**，
    `2828` 那一行只取了 `queued_events`。所以文档说的"拼上 `failures`"确实是**一行**改动
    （`memory.get("failures", 0)`），这一点它说对了。
- **判定：对**。
- **漏掉的同类**：`send_failures`（`outbox.py` 路径 + `record_send_failure`）
  与 `extension_failures` 也是"有数据、没出口"，见下节"它们漏掉的"。
- **严重度**："中"给得住。

### 第 9 条 `memory_degraded` 是死指标（低）

- **文档原话**：`metrics.py:113` 与 `145-147`；`record_memory_degraded` **零调用**；
  它照旧进 `snapshot()`（157）与面板。
- **我的核对**：
  - `metrics.py:113`（`self.memory_degraded = 0`）、`145-147`（`def record_memory_degraded`
    → `with self._lock:` → `self.memory_degraded += 1`）、`157`
    （`"memory_degraded": self.memory_degraded,`）**逐处对上**。
  - 全树 `grep record_memory_degraded`：只有 `metrics.py:145`（**定义**）一处，
    **调用 0 处（含测试）**——这一条比第 1 条更干净，**是货真价实的死代码**。
    另有 `ARCHITECTURE.md:188` 提到 `memory_degraded`（文档/说明，非消费者）。
- **一处不实**：文档说"它照旧进 `snapshot()`（157）**与面板**"。
  本分支**没有面板**（`webui_panel.py` 不在本树），
  全树 `snapshot.metrics` 只有 `stage3_main.py:2740` 一个消费者，而 `2740-2741`
  只取 `model_latency`。所以"上面板"这句**在本树是不成立的**（说成"进快照、但没有出口"才对）。
  逻辑上这是**低估了**问题（没有出口比"面板上一格恒 0"更彻底），不是高估。
- **判定：对（零调用、行号）**；"与面板"这句需更正。
- **最小改法**：二选一（接上 or 删掉）——合理，无更小做法。
- **严重度**："低"给得住。

### 第 10 条（推断）维护锁内无超时的 `to_thread`（低）

- **文档原话**：`memory_maintenance_agent.py:185`（锁）、`202-205`（`locked()` + `async with`）；
  块内 `209/214/224/225/231/251/280` 七处 `await asyncio.to_thread(self.store.*)` **无 `wait_for`**；
  只有 `235` 的模型调用有 `wait_for(..., self.timeout)`；`memory_config.py:32` 的 `timeout=45.0`。
- **我的核对**：**七处全部逐行对上**（实测，read 183-282）：
  `209` `decay_relationships`、`214` `promote_reconfirmed`、`224` `discard_disallowed`、
  `225` `claim`、`231` `people_roster`、`251` `commit`、`280` `release`——**一处不差**，
  且**都没有 `wait_for`**。`235` 确实是
  `raw = await asyncio.wait_for(self.client.complete(request), self.timeout)`。锁在 `185`，
  `202-205` 是 `if self._lock.locked(): return 0` + `async with self._lock:`——**对上**。
  `memory_config.py:32` 的 `timeout=45.0` **未逐字读**，标**未复核**。
- **判定：对（机制，实测）**；"锁永不释放"的**发生**是**推断**，文档标了推断，诚实。
- **最小改法**：包 `wait_for` —— 合理。**一处可以更根本**：文档自己也写了缓解事实
  （`memory_store.py:322/415` 的 `sqlite3.connect(..., timeout=0.1)`），
  那么更根本的做法是**让 SQLite 本身不可能挂住**（已是现状），
  或把维护整跳包一个总超时；文档选的是逐调用包，属于"补上界"而不是"消除根因"，
  但它明确说了这是"补上界"，**没有假装是根因**。诚实。
- **严重度**："低"给得住。

---

## 逐条核对结果 · 二、`isolation-growth-2026-10-08.md`

### [1] 会话状态只增不减（高）

- **文档原话**：`stage3_main.py:890/894/114`（`states` / `setdefault` / `HISTORY_LIMIT = 2000`）；
  `910-912` `leave()`；`stage3_runtime.py:354-357` `exit()` 不碰 history；`3137` `disable_group` 走 `leave()`。
- **我的核对**：**五行全对**（实测，read）：
  `890` `self.states: dict[str, ConversationState] = {}`；
  `894` `return self.states.setdefault(session_id, ConversationState(history_limit=HISTORY_LIMIT))`；
  `114` `HISTORY_LIMIT = 2000`；`910-912` `leave` 只 `exit()` + `trigger().reset()`；
  `stage3_runtime.py:354-357` `exit()` 只改 `mode` / `active_user_id` / `context`；`3137` + `3138`
  `disable_group` → `self.sessions.leave(f"group:{group_id}")` → `persist_state()`。
  **"只增不减"成立**：全树 `self.states` 只有 `setdefault`（`894`）、`restore`（`943`）两处写，
  没有任何 `pop` / `del` / `clear`（实测，grep）。
- **我加的**：`3138` 的 `persist_state()` 会在 disable 之后**把还留着历史的会话原样写回盘**
  （`export` 在 `917-924` 筛的是 `state.history`），所以"disable 之后仍占着"这句话
  **同时**适用于内存与 `runtime_state.json`——文档只说内存，其实落盘也一起。
- **判定：对**。
- **最小改法**：加 LRU + `leave()` 里 `self.states.pop(session_id, None)`。指出**一个缺口**：
  文档没提 `persist_state` 与 `_follow_ups` / `_session_clients`（见下）也要一起清，
  而它自己在 [5] 说"与 [1] 的 LRU 一起清"——也就是它知道要清 client，
  却漏了另一个同键 dict。
- **严重度**："高"给得住（结构上必然单调增长 + 用户语义上期望释放却完全没释放）。

### [2] 全量导出历史 + 回复路径同步 fsync（高）

- **文档原话**：`stage3_runtime.py:392-406` 没有 `[-N:]` 切片；
  `stage3_main.py:917-924, 149, 3172`；`4153-4161` 回复分支同步调用；
  `state_store.py:22` `MAX_PERSISTED_HISTORY = 50` 全仓零引用。
- **我的核对**：
  - `stage3_runtime.py:392-406` **逐字对上**（`"history": [ {...} for m in self.history ]`，
    **确实没有切片**）。
  - `917-924`（`candidates` 筛选 + `sort` + `[:max_sessions]` + `as_state()`）、
    `149`（`MAX_PERSISTED_SESSIONS = 200`）、`3172`
    （`"sessions": self.sessions.export(max_sessions=MAX_PERSISTED_SESSIONS)`）**全对**。
  - `4153-4161`：`4153` `if decision.kind is DecisionKind.REPLY and decision.text:` →
    `4161` `self._persist_after_activity(now)` **对上**（`4161` 在 `_model_path` 里，
    也就是**回复路径上同步执行**）。
  - `state_store.py:22` `MAX_PERSISTED_HISTORY = 50` —— 定义**对上**，
    **全 `dev/` 零引用**：`grep MAX_PERSISTED_HISTORY` 在 `src/` `tests/` **只有 `state_store.py:22` 一处**
    （实测）。文档说"grep 命中 4 处：`state_store.py:21,22` 与 `stage3_main.py:149,3172`，
    后两者用的是 `MAX_PERSISTED_SESSIONS`"——**这个解释是对的**，但表述容易让人误读成
    "有 4 个引用"，实际是"1 个定义 + 1 个同前缀名 + 2 个别的常量"。
  - `fsync` 确实存在：`state_store.py:125` `os.fsync(handle.fileno())`（实测）。
  - `PERSIST_MIN_INTERVAL_SECONDS = 10.0` 在 `stage3_main.py:151` **准确**；
    节流判断在 `4334-4337`（`if last is not None and now - last < PERSIST_MIN_INTERVAL_SECONDS: return`）。
- **一处措辞不准**：文档说"每 10 秒……dump 一次"。`_persist_after_activity` 是
  **"距上次落盘 ≥10 秒才落"**，落在**一次回复**上——安静 10 秒不会有任何写入。
  说成"最多每 10 秒一次"才准。**方向不错**（确实在热路径上、确实同步、确实带 fsync）。
- **判定：对**；上面这句是精度问题。
- **最小改法**：`as_state()` 里切片 + `export()` 保持排序 —— **确实最小**，
  而且**连带点也齐**：`from_state` 用 `history[-history_limit:]`（`stage3_runtime.py:477`）
  读回来，切片不会破坏恢复语义。这一条文档做得最干净。
- **严重度**："高"给得住。

### [3] 裸 `message_id` 全局集合（中高）

- **文档原话**：`1092-1094`；`3663` / `4298-4299`（唯一移除点）；
  `3410` / `3708` 命中就跳过；`slang_learning.py:641/702-706`；`trigger.py:23-30`；
  `quote_samples.py:462` 的"4579 条 0 撞号"。
- **我的核对**：
  - `1092` `self._resumed_ids: set[str] = set()`、`1094` `self._promised_ids: set[str] = set()`
    —— **对上**（文档写 `1092-1094`，中间 `1093` 是注释）。
  - `3663` `self._resumed_ids.add(message.message_id)` **准确**；
    `4298-4299` `discard` 两处 **准确**（在 `_serve_queue` 的循环里）。
    **"唯一移除点"成立**：全树 `_resumed_ids` 只有 `add`(3663) / `discard`(4298) / `in`(3410,3708) —— 实测。
  - `3410` / `3708` `resumed = message.message_id in self._resumed_ids` **逐行准确**。
  - `slang_learning.py:641` `self._seen: deque[str] = deque(maxlen=MAX_SEEN_MESSAGES)`
    与 `100` `MAX_SEEN_MESSAGES = 2000` **准确**（`702-706` 未逐字读，标**未复核**）。
  - `trigger.py:23-30` `MessageDeduplicator.accept` 未逐字读，标**未复核**。
  - `quote_samples.py:462` **逐字对上**（"实测这份日志里 4579 个入站 id 没有一个跨会话撞号，
    但这不在任何契约里"）——文档把它标为"他人实测"，**标注正确**。
- **判定：对**；"撞号后果"是**推断**，文档标了推断。
- **最小改法**：键带 session。指出**一处**：文档只说 `_resumed_ids` 该加 4096 上限，
  但没说"上限也救不了 `focus._queues` 里排不出去的群"——那才是它自己 [7] 的另一半。
- **严重度**："中高"给得住（它自己写了"风险按实测偏低，所以不到高"）。

### [4] `_promised_ids` 用 `set.pop()` 淘汰（中）

- **文档原话**：`1094`；`3674-3676`（`add` + `len > 256` + `pop()`）；
  `3821` / `4291` 消费点；`set.pop()` 移除的是**任意**元素。
- **我的核对**：
  - `3674-3676` **逐字对上**（`self._promised_ids.add(...)` / `if len(...) > 256:` / `self._promised_ids.pop()`）。
  - `3821` `must_reply = message.message_id in self._promised_ids` **准确**；
    `4291` `promised = ...` **准确**。
  - `set.pop()` 的"任意"性质**实测**：`python -c` 造 300 个元素、`pop()` 五次，
    得到 `['132','57','200','63','123']`（**不是** `0,1,2,3,4`）。
    证据：`.venv\Scripts\python.exe -c "s=set();[s.add(str(i)) for i in range(300)];print([s.pop() for _ in range(5)])"`
    → `['132', '57', '200', '63', '123']`。
- **判定：对**（现场 + 语义双证）。
- **最小改法**：换 FIFO（`OrderedDict` 或 `deque`+`set`）—— 合理。
  指出**一个更根本的点（文档没说）**：这条的触发条件不只是"积压 256 条"，
  而是"**该群被 disable / 排不出去**"——`focus.drain` 只在 `_serve_queue`（`4288`）被调，
  而 `disable_group`（`3131-3139`）**不清 `focus._queues`**。
  被 disable 的群如果之后再也收不到消息，它的 `_resumed_ids` / `_promised_ids`
  条目会**永久留着**（不占满 256 也不会被 pop，因为这条群根本不会再排空）。
  这比"随机淘汰一条"更彻底地让承诺落空。
- **严重度**："中"给得住。

### [6] 补上下文的两个缓存无上限（中）

- **文档原话**：`conversation_context.py:55-56`、`93-95`、`99-100`；
  全树 `_members.pop` / `_last_refresh.pop` 零命中。
- **我的核对**：**逐行全对**（实测，read）：
  `55` `self._last_refresh: dict[str, float] = {}`、`56` `self._members: dict[str, dict[str, str]] = {}`、
  `93` `if now - self._last_refresh.get(block, float("-inf")) < self.refresh_interval: return ()`、
  `95` `self._last_refresh[block] = now`、`100` `self._members[session_id] = members`。
  **零删除点成立**：`grep '_members\['` 只有 `100` 一处写、`69`/`72` 两处读；
  `_last_refresh` 只有 `93`/`95` —— 全树没有 `.pop` / `del` / `clear`（实测）。
- **我加的（文档漏了）**：两个 dict **键的口径还不一样**：
  `_last_refresh` 用 `block = f"group:{group_id}"`（`91`），`_members` 用 `session_id`（`100`）。
  同一个函数里两套键，含义差一层 `group:` 前缀——如果以后有人按 `_last_refresh` 的键
  去淘汰 `_members`，会**永远淘汰不掉**（键对不上）。这是做 [6] 的"最小改法"时
  必须先统一的地方，文档没提。
- **判定：对**。
- **严重度**："中"给得住（它给了"整群名册比会话状态大"这个理由）。

### [7] `_ack_at` 永不清；`_queues` 无 maxlen（中）

- **文档原话**：`focus.py:48, 51`；`110-115`；`152-153` 全树无删除点。
- **我的核对**：**全对**（实测，read）：
  `48` `_queues: dict[str, deque[IncomingMessage]] = field(default_factory=dict)`、
  `51` `_ack_at: dict[str, float] = field(default_factory=dict)`；
  `109-116` `enqueue`（`deque()` 无 maxlen、`_queues[session_id] = queue`、`_order.append`、`queue.append`）
  —— 文档写 `110-115`，**覆盖准确**；
  `152-153` `def note_ack(...)` / `self._ack_at[session_id] = now` **准确**。
  `grep _ack_at`：`51`（定义）、`149`（读）、`153`（写）、`156`（读）—— **零删除点成立**。
- **我加的（对文档有利）**：`_queues` 不是完全不清——`drain()`（`140-144`）
  会 `self._queues.pop(session_id, None)` 并从 `_order` 移除。文档没说这一点，
  但它说"无上限"仍然成立（`drain` 只在轮到该群时发生）。
  另外 `_order` 与 `_queues` 是 **两条并行结构**，`_requeue`（`4315-4317`）会重新 `enqueue`，
  这条路径也不会清 `_ack_at`。
- **判定：对**。
- **漏掉的连带（文档漏了）**：`disable_group`（`3131-3139`）**不清 `focus` 任何一张表**。
  也就是说一个被停用的群，它的 `_queues`（含消息全文）、`_order`、`_ack_at`、
  `_resumed_ids`、`_promised_ids` 条目都留着，而它**永远不会**被 `take_next_session`（`132-138`）选中
  —— 因为选中要求 `enabled`？不：`take_next_session` 只看 `_queues` 非空，
  而 `handle` 会在 `3653` 之前用 `enabled_group_ids` 拦住外来消息，所以**积压会僵在那里**。
  这是 [7] 该有而没有的"下限"。
- **严重度**："中"给得住。

### [8] `relationship_log` 是唯一没有保留期的内容表（中）

- **文档原话**：`memory_store.py:116-121` 建表；`915/948/1020` 三处 INSERT；
  `552-574` `_cleanup` 删了 inbox/receipts/replays/leases/audit/archive；
  全树没有 `DELETE FROM relationship_log`。
- **我的核对**：
  - 建表 `116-121` **逐字对上**（含 `CREATE INDEX relationship_log_recent`）。
  - 三处 INSERT `915` / `948` / `1020` **逐处对上**（`grep "INSERT INTO relationship_log"` 正好 3 处）。
  - `grep 'DELETE FROM relationship_log'` **零命中** —— "没有 DELETE"成立。
  - **`552-574` 的说明不准确**：`_cleanup` 里**根本没有出现 `relationship_log` 这个词**，
    但它也**不是**只删了文档列的那些。逐行读出来是：
    `553` inbox、`554` receipts、`555` replays、`557` leases、
    `562` `_tombstone`（**写入** tombstones）、`564` `_archive`（**写入** archive）、
    `565-566` 记忆两表置 deleted、`567-568` 删已删记录、
    **`570-571` `DELETE FROM record_subjects WHERE record_id NOT IN (...)`（孤儿清理，文档没列）**、
    `572` audit、`574` archive。
    所以文档"删了 inbox/receipts/replays/leases/audit/archive"这份清单**漏了 `record_subjects`**，
    而它的 `:552-574` 行号本身**是对的**（范围正确，内容不全）。
  - 结论（`relationship_log` 无保留期）**成立**。
- **判定：对（结论与行号）**；`_cleanup` 的清单一处不完整（漏 `record_subjects`）
  —— 这是本报告里唯一指出的"文档正文与源码不符"的实质点，但**不改变 [8] 的结论**。
- **最小改法**：`_cleanup` 里加一条 `DELETE FROM relationship_log WHERE occurred_at < ...`
  —— **确实最小**，且 `_cleanup` 已经在 `claim`（`583`）与 `cleanup()`（`576-578`）两条路上被调，
  加一行就会被周期执行，**没有漏掉的接线**。
- **严重度**："中"给得住。

### [9] `tombstones` 无保留期；`verify_runs.log` 无轮转（低）

- **文档原话**：`memory_store.py:98-100` 建表；`_cleanup()`（`:552-574`）里没有 tombstones；
  `verify_log.py:216-218` 只追加；实测 `verify_runs.log` 已 30136 字节。
- **我的核对**：
  - 建表 `98-100` **逐字对上**。
  - `_cleanup` 里**确实没有** `DELETE FROM tombstones`（`grep 'DELETE FROM tombstones'` 零命中，实测）。
  - `memory_store.py:1047` 那处 `for table in ("tombstones", "receipts", "archive"):`
    是 **`counts()` 里的 `SELECT COUNT(*)`**，不是删除——**我核过，文档没误读**。
  - **文件大小可复现**：`verify_runs.log` 现在是 **30259 字节**（`(Get-Item data\logs\verify_runs.log).Length`），
    文档写 30136 字节。差 123 字节 = 审计之后又跑了一到两次验证（每次约 120 字节），
    **文档那个数是当时的真值**，不是编的。
  - 另外两个数**也复现了**：`reply.jsonl` = **4474554** 字节（文档 4474554，**一字节不差**）；
    `memory.sqlite3` = **159744** 字节（文档 159744，**一字节不差**）。
    这说明它对**静态文件**的测量是真的；（`verify_runs.log` 那个差值是"活文件还在长"，正常。）
- **判定：对**。
- **最小改法**：tombstones 加保留期 + `verify_runs.log` 复用 `RollingJsonlFile`
  —— `RollingJsonlFile` 确实存在（`feature_log.py:97`）且被 `chat_log.py` / `raw_events.py`
  复用，**建议可执行、没有漏接线**。
- **严重度**："低"给得住。

### 补充段（不单列的那几项）

- `group_roles._roles`（`117-118`）、TTL 只在读时判断（`:181-182`、`:263-269`）、
  只有 `forget()`（`:273-291`）会清 —— 我核了 `_roles.pop`：只有 `287` / `290` 两处，
  都在 `forget()` 里（实测，grep）。**成立**。
- `_pending_relays` / `_pending_relay_selections`（`1156-1157`）、`_purge_expired_relays`（`:3221-3236`）
  只被 `_relay_reply`（`:3276`）调用 —— `1156`/`1157` 定义、`3221` `_purge_expired_relays`、
  `3228` `pop` **对上**（`3276` 未逐字读，标**未复核**）。
- `AskBudget`（`ask_when_unsure.py:151-181`）"没有消费者" —— 与 `stage3_main.py:1077-1080`
  的注释一致（"**没有限量器了**……这里不再持有一个配额账本"），**成立**。

---

## 专挑三类的结果

### 一、死代码 / 零引用清单（我自己数的，`src/` + `tests/` 分别计）

| 符号 | 定义 | `src/` 消费者 | `tests/` 消费者 | 结论 |
| --- | --- | --- | --- | --- |
| `record_model_error` | `metrics.py:131-135` | **0** | 1（`test_persistence_and_metrics.py:219`） | **源码零引用**，但不是"全零"；有出口测试（`:227`） |
| `record_memory_degraded` | `metrics.py:145-147` | **0** | **0** | **货真价实死代码**（另有 `ARCHITECTURE.md:188` 提到字段名，非消费者） |
| `memory_degraded`（字段） | `metrics.py:113` | 只进 `snapshot()`（`metrics.py:157`） | **0** | 值永远是 0；本树**没有面板**读它 |
| `compaction_enabled` | `operator_config.py:46`、`runtime_flags.py:31/99/100/144` | **0**（`_maybe_compact` 不看） | **0** | **死开关**；连 `QQBOT_COMPACTION`（`operator_config.py:46` 声明的 env）都**没接进** `build_flags`（`runtime_flags.py:144` 硬编码 True） |
| `MAX_PERSISTED_HISTORY` | `state_store.py:22` | **0** | **0** | **真零引用**，`grep` 在 `src/`+`tests/` 只有 1 处命中（就是定义） |
| `record_send_failure` | `metrics.py:137-139` | 2（`stage3_main.py:4722/4728`） | 1（`test_outbox.py`） | **不是死代码**，但值**没有任何出口**（见"漏掉的"） |
| `record_extension_failure` | `metrics.py:141-143` | 1（`stage3_main.py:4152`） | 1 | 同上，值**没有出口** |

**打到的点**：
1. `record_model_error` 的正文把"`dev/` 搜索只有测试里那一处调用"与"源码零调用"**并列写在同一句**，
   所以它是**知情**的；但标题与"最该修的三件"的措辞（"**定义了却零调用**"）读起来像全零
   —— 严格要求是"源码零调用"。**算措辞不严，不算错。**
2. `compaction_enabled` 这一条我**加了一刀**：它连 env 口子都是断的
   （`operator_config.py:46` 说 `env=QQBOT_COMPACTION`，`runtime_flags.build_flags` 不读它）。
   文档只说了"面板拉不动"，实际是"面板拉不动 **+** 环境变量也拉不动"。
3. `MAX_PERSISTED_HISTORY` 的"零引用"是**最干净的一条**，我独立复现了。

### 二、"只增不减 / 无上限"清单（自己找清理点）

| 结构 | 位置 | 清理点 | 我的结论 |
| --- | --- | --- | --- |
| `DialogueSessions.states` | `stage3_main.py:890` | **无**（只有 `setdefault` 894 / `restore` 943） | **只增不减**，成立 |
| `DialogueSessions.triggers` | `891` | **无**（`setdefault` 897） | 文档**没提**；比 `states` 更小，但同源 |
| `_session_clients` / `_session_judges` | `1002-1003` | **无**（`1467` / `1487` 只写） | **只增不减**，成立 |
| `_resumed_ids` | `1092` | `4298` `discard`（只在 `_serve_queue`） | **有清理点但覆盖不全**（被 disable 的群永不清） |
| `_promised_ids` | `1094` | `4299` `discard` + `3676` 任意 `pop` | **上限 256 是假的**（`set.pop()` 任意淘汰，已实测） |
| `_follow_ups` | `1089` | `4205` `pop(session_id)` / `4203` `clear()` | **有清理点**，但都在 `take_follow_ups` 里；leave/disable **不清** → **文档漏了它** |
| `_pending_memory_replies` | `1086` | `4158` **最旧优先** `pop(next(iter(...)))`（对）+ `3217` `pop(message_id)` | **有上限、且 FIFO 正确** —— 文档**没提**，这是本区少见的"已经做对了" |
| `_stance_cache` | `1162` | `1392-1393` **>512 就 `clear()`**、`1421`/`2197` `pop` | **有上限（粗暴但有效）** —— 文档**没提** |
| `_denied_admin` | `1050` | `2591-2593` 读时惰性清 + `2639/2645/2647` | **有清理** —— 文档在补充段提了时机问题，没单列 |
| `focus._queues` | `focus.py:48` | `drain`（`141` `pop`） | **有清理**，但只在轮到该群时；`disable` 不清 → 上位结论成立 |
| `focus._ack_at` | `focus.py:51` | **无** | **只增不减**，成立 |
| `conversation_context._members` | `conversation_context.py:56` | **无** | **只增不减**，成立 |
| `conversation_context._last_refresh` | `conversation_context.py:55` | **无**（且键口径与 `_members` 不一致） | **只增不减**，成立 |
| `group_roles._roles` | `group_roles.py:117-118` | `forget()`（`287`/`290`） | **有清理点**，但只在显式调用时；TTL 只在读时判断 → 上位结论成立 |
| `relationship_log` | `memory_store.py:116` | **无 DELETE** | 成立 |
| `tombstones` | `memory_store.py:98` | **无 DELETE** | 成立 |
| `record_subjects` | `memory_store.py:122` | `570-571` 孤儿清理 | **有**（文档漏记，见 [8]） |
| `verify_runs.log` | `verify_log.py:50/216-218` | **无轮转** | 成立 |

**打到的点**：
1. **文档没漏掉任何它自己声称的"无清理点"**——我逐张表核过，那几条（`states`、`_ack_at`、
   `_members`、`_last_refresh`、`relationship_log`、`tombstones`、`verify_runs.log`）**都没有清理点**，
   属实。
2. **但它漏掉了两处"无上限"**：`_follow_ups`（同键、同"leave 不清"的根因，
   只是取值时会被摘掉，所以没被它写进 [1]）与 `focus._queues` 在 disable 之后僵住
   （它写了队列无上限，没写"这些条目永远轮不到"）。
3. **它没看到的清理点**：`_stance_cache` 的 512 清空、`_pending_memory_replies` 的 128 FIFO、
   `focus._order` 随 `drain` 一起收、`_denied_admin` 的惰性清。这些都是"已有的上限"，
   写进去会让"[7] 是唯一由外部输入决定大小的队列"这句更站得住（它是站得住的），
   也会让读者知道**这个仓库里"有上限"是常态、缺上限是例外**。

### 三、因果链是否成立

| 文档的因果链 | 我的复核 | 结论 |
| --- | --- | --- |
| "disable 之后历史仍占着"（isolation [1]） | `3137` `leave()` → `stage3_runtime.py:354-357` `exit()` 只改 mode/user/context；`894` 的 `states` 无 `pop`；`3138` 还会把它写回盘 | **成立**，且比文档多一层（落盘也留着） |
| "tick 会提前清 sweeping"（failure 3） | `4242` 无条件 `begin_sweep`、`4269` 无条件 `end_sweep`；接收路径 `3744` 读 `sweeping` | **成立**（"两条任务同时进 `_model_path`"是推断，标注诚实） |
| "补历史把 QQ 号带进正文"（failure 本区**跳过项**，不在两文内） | `conversation_context.py:126-127` 的注释说这条**2026-09-29 已修**（`_self_id` 现在会填）；`_to_message`（`220-231`）用 `display_name` 解析名字 | **不在两份文档的清单里**（失败降级在"我没做到的"里把它列为"已知并跳过"）——**标注方式正确**，无需复核 |
| "承诺那条被随机淘汰"（isolation [4]） | `3674-3676` + `set.pop()` 实测任意 | **成立** |
| "B 群消息被顶掉"（isolation [3]） | `3410` / `3708` 只看 `message_id`；`quote_samples.py:462` 的 4579 条 0 撞号是**他人实测** | **机制成立**，**后果是推断**（文档标了推断，正确） |
| "压缩失败 → 下一轮同一批再试"（failure 6） | `4386-4391` `break` 在循环内；`4401` `mark_compacted` 每批成功都跑 | **成立**（精度：是"失败那一批及之后"重试，不是"整批重来"） |
| "判定失败吞掉稍等"（failure 5） | `4524` 不看 `must_reply`、`394-395` 看；`3821` 传进去 | **成立** |

---

## 它们漏掉的（每份至少一条）

### 失败降级漏掉的

**`send_failures` 与 `extension_failures` 同样是"有数据、没出口"，
而第 1 条却把它自己写成"唯一"同时命中两件事的问题。**

- 证据：`metrics.py:137-139`（`record_send_failure`）在 `stage3_main.py:4722` 与 `4728`
  两处被调（实测，grep），所以 `send_failures` **会涨**；
  `record_extension_failure`（`metrics.py:141-143`）在 `stage3_main.py:4152` 被调，**会涨**。
  而 `/super status` 里能看到出站信息的只有 `2814-2825`（`outbox.stats()` 的
  `pending` / `resent` / `dropped`）——**`send_failures` 不在里面**；
  `extension_failures` 更是全树没有任何显示点。
- 为什么算漏：第 1 条的理由是"它同时命中'她会不会突然不说话'+'你查不查得到'"，
  并称它是"本区唯一"这样一个交汇点。但**发不出去**（`send_failures`）
  正是最典型的"她说了、你没收到、面板上也看不见"——它同样命中那两件事，
  而且是同一类缺陷（计数在 `RuntimeMetrics` 里、没有显示点）。
  文档的第 1 条把这类缺陷写成了只对 judge/reply 成立，**低估了这一区的面积**。
- 顺带一个**同源的、更细的漏**：`_pending_memory_replies`（`stage3_main.py:1086`）
  只在 `record_sent_reply`（`3217`）成功发出时被 `pop`。回复发不出去时，
  这条正文会留在 dict 里直到被 128 上限挤掉（`4157-4158`），
  而 `128` 上限用的是**最旧优先**（`pop(next(iter(...)))`，正确）——所以不是漏内存，
  是"她说过的话在内存里多躺一会儿"。这条不严重，但它和第 8 条（扩展材料收集失败
  不计数）是同一张表（`_pending_memory_replies` / `extension`）上的两个点，文档只挑了一个。

### 隔离增长漏掉的

**`_follow_ups`：同样按会话只增不减，且 `leave()` / `disable_group()` 都不清它。**

- 证据：`stage3_main.py:1089` `self._follow_ups: dict[str, list[OutgoingMessage]] = {}`；
  写入在 `4176-4177`（`self._follow_ups.setdefault(message.session_id, []).append(...)`，
  长回复被拆成多段时每段一条，**含正文**）；
  唯一的取走点在 `4205`（`take_follow_ups(session_id)` 的 `pop`）与 `4203`（无参会 `clear()`）。
  调用 `take_follow_ups` 的只有 `4297`（`_serve_queue`）与 `runtime.py:2461`
  （`_handle_one` 里 handle 之后）——**两条都是"有消息被处理"时才走**。
  `leave()`（`910-912`）与 `disable_group()`（`3131-3139`）**都不碰它**（实测，grep）。
- 为什么算漏：文档 [1] 的主论点是"按会话的那份状态只有重启才回收"，
  [5] 又专门为"两个按会话的 dict 没有删除点"开了一条。`_follow_ups`
  **正好是第三个同键 dict**，而且是这一区里唯一"值是消息正文"的那个
  （`states` 里的历史也是正文，但至少 `states` 被写进了 [1]）。
  它同时命中 [1] 的根因（`leave` 不清）与 [5] 的形状（dict 只写不删），
  却被漏在两条之外。
- 修法与 [1] 天然同源：`leave()` 里顺手 `self._follow_ups.pop(session_id, None)`；
  以及 `disable_group` 里把 `focus` 的那几张表（`_queues` / `_order` / `_ack_at` /
  `_resumed_ids` / `_promised_ids`）一起收掉——后者是 [7] 与 [3][4] 共同的缺口，
  三处都指到了同一行代码（`3131-3139`），而三处都没说。

---

## 我复现了哪些"实测"

**真跑过的**（命令 + 结果）：

1. `set.pop()` 的任意性（isolation [4] 的核心断言）：
   `.venv\Scripts\python.exe -c "s=set();[s.add(str(i)) for i in range(300)];print([s.pop() for _ in range(5)])"`
   → `['132', '57', '200', '63', '123']`（Python 3.14.6）。
   **不是**按插入顺序。文档 [4] 成立。
2. 文件大小（isolation [9] / "证据与量法"）：
   `(Get-Item data\logs\verify_runs.log).Length` → **30259**（文档写 30136，差 123 = 审计后新跑的验证记录）；
   `reply.jsonl` → **4474554**（文档 4474554，**一字节不差**）；
   `memory.sqlite3` → **159744**（文档 159744，**一字节不差**）。
3. 树没动过（本轮核查的前提）：`git diff --stat 82e831e HEAD -- src tests` → **空**。
   `git log --oneline -3` → `a52ae55` / `82e831e` / `6ca6b17`。
4. 若干 `grep` 计数（文档自称也是 grep）：`record_model_error`、`record_memory_degraded`、
   `memory_degraded`、`compaction_enabled`、`MAX_PERSISTED_HISTORY`、`DELETE FROM tombstones`、
   `DELETE FROM relationship_log`、`_ack_at`、`_members[`、`_roles.pop`、`_session_clients`、
   `_resumed_ids`、`_follow_ups`、`wait_for|asyncio.timeout`。

**逐行读过的**（read + 行号确认，不是 grep）：两份文档里约 40 处 `文件:行` 中的绝大多数，
包括 `stage3_main.py:110-155 / 880-945 / 1040-1108 / 1120-1155 / 1460-1500 / 1631-1668 /
2730-2840 / 3120-3172 / 3640-3704 / 3730-3760 / 3810-3880 / 3940-3999 / 4130-4205 /
4225-4330 / 4339-4416 / 4490-4526`，`runtime_flags.py:25-160`、`llm_client.py:1-95 / 160-359`、
`metrics.py` 全文、`state_store.py` 全文、`focus.py:40-168`、`conversation_context.py:45-164 /
218-262`、`stage3_runtime.py:110-169 / 290-330 / 345-421`、`memory_service.py:40-119`、
`memory_maintenance_agent.py:183-282`、`memory_store.py:80-140 / 540-600 / 1030-1052`、
`snapshots.py:30-109`、`group_roles.py:170-214`、`quote_learning.py:615-684`、
`quote_samples.py:450-474`、`tests/test_dual_agent.py:105-126`、
`tests/test_quote_learning.py:255-289`。

**没复现但标为"实测"的**（我按**推断**或**未复核**处理）：

- isolation [3] 的"4579 条 0 撞号"：那是 `quote_samples.py:462` 里**作者自己**的记录，
  我确认了这句话在源码里、也确认了文档把它标成"他人实测"——**但我没有那份日志、没独立复算**。
- failure 第 2 条的"实测判定约 2 秒、回复约 10 秒"（用于推出 `TURN_TIMEOUT_SECONDS=90`）：
  这在**两份文档的正文里找不到出处**（既没给出处，也不是从代码读出来的）——
  我判它是**记忆/经验值，不是实测**，文档把它写成"实测"是**过强**的标注。
- failure 第 2 条文档说"未跑测试"，而它引用的 `tests/test_dual_agent.py:114` 我去读了，
  `test_judge_failure_degrades_to_silence` 就在 `114-125`——**行号准确**，但我**没有运行**它。

---

## 我没做到的 / 读不到的

- **一行源码都没改**（这是本次核查的硬约束）；**没跑测试、没跑 pyflakes、没起进程、
  没碰 `run/`、没碰 `.env`、没碰 `data/memory/`**。唯一执行的程序是上面那次
  `python -c`（纯内存 `set` 操作，不读写任何仓库文件）。
- **`dev/` 之外的树没看**：任务说"仓库 `dev/`"，我按 `dev/` 核。
  顺带说明：本次会话的 DSH 派发信息把工作目录写在 `C:\Users\XuZhunzhi\QQRoleplayBot`，
  而两份文档与被审源码在 `...\QQRoleplayBot\dev\`——核查按 `dev/` 进行。
- **本树没有面板**：`webui_panel.py` / `webui_data.py` / `webui_access.py` 不在本分支
  源码目录里（`glob` 结果里没有这些文件，只有 `.pyc` 残留），所以两份文档里所有
  "面板上能看到 / 看不到"的断言，我只能核到**命令侧出口**（`/super status`），
  核不到"面板那一格"。这一点对失败降级第 1/9 条的"上面板"措辞有直接影响，
  我已在上面对应条目里标出。
- **`memory_config.py:32` 的 `timeout=45.0`**、`memory_inbox.py:73-75`、
  `trigger.py:23-30`、`outbox.py:105-162`、`style_reviewer.py:255-283 / 314-315`、
  `onebot_client.py:239-251`、`stage3_main.py:2859-2860 / 2983 / 4720-4729`、
  `memory_store.py:199 / 322 / 415 / 1142 / 1796`、`quote_learning.py:933-936`、
  `background_plugins.py:82-90`：**没有逐字读**，标**未复核**。
  它们都是"这一区没问题的部分"或"配套对照"，不影响两份文档的**主结论**。
- **"9 分钟"这类端到端最坏值**：我复算了算术（181 秒 × 3），
  但**没有构造一个卡住的模型服务去真跑**——这类数字仍然是"按上限推的"，
  不是实测。两份文档自己也没跑（这是对的，它们标了"未跑"）。
- **插件的 `plugins/` 侧**、`docs/` 其余文件、`.env`、`run/`：一律没查（隔离增长
  自己也声明了没查，我保持同一范围）。
