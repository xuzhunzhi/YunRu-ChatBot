"""引擎的只读状态模型（快照）。

**为什么单独一个文件**：这两个 dataclass 是**引擎自己的形状**——`DialogueEngine.snapshot()`
返回的就是它，`/super status` 与面板都读它。它们原先放在 `control.py` 里，
于是 `stage3_main` 必须 import `control`，而 `control` 的名字听起来像"控制面插件"，
看起来就像核心被插件拴住了。

实际上它俩零依赖（只有 `dataclasses`），谁 import 谁都不成环。放在这里之后：

- `stage3_main` → `snapshots`（引擎的形状）
- `control.py` → `snapshots`（控制端口引用同一份形状）

**不含 API Key、不含聊天正文**——它是要给控制面与日志看的。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SessionSnapshot:
    """供控制台使用的脱敏会话摘要，不包含聊天历史。"""

    session_id: str
    mode: str
    history_size: int
    topic: str
    topic_status: str
    # 会话是否已持久化到本地状态库（重启后是否仍会保留）。
    persisted: bool = False


@dataclass(frozen=True, slots=True)
class EngineSnapshot:
    """引擎的只读状态模型；不包含 API Key 或完整聊天内容。"""

    enabled: bool
    target_group_id: str
    enabled_group_ids: tuple[str, ...]
    sessions: tuple[SessionSnapshot, ...]
    accepted_messages: int
    ignored_messages: int
    blocked_messages: int
    deferred_messages: int
    model_calls: int
    replies: int
    history_seeded: int = 0
    # 出站消息被拆成的总段数；与 replies 之比反映"分几句说"的实际情况。
    reply_segments: int = 0
    # 判定 agent 的调用次数。双 agent 结构下与 model_calls 分开计：
    # model_calls 是回复调用，judge_calls 是判定调用。
    judge_calls: int = 0
    # 对话压缩次数。攒满保留上限时触发，把旧对话压成摘要。
    compaction_calls: int = 0
    # 多群焦点的观测计数：排进队列、回了多少"稍等"、切换了几次当值群、
    # 被排队消息最后真的回了多少条、以及答应过"稍等"却没给出结果的条数（异常，应为 0）。
    focus_queued: int = 0
    focus_acks: int = 0
    focus_switches: int = 0
    focus_queued_replies: int = 0
    focus_declined_promised: int = 0
    # 判定说"要回"、回复 agent 却空手而归的次数。双 agent 下它**没有否决权**，
    # 所以这是模型抽风的异常计数（应当为 0），不是一种正常结果。
    empty_forced_reply: int = 0
    # 判定/回复这两条路的模型调用失败次数（2026-10-08）。失败时她**照样不出声**
    # （那是刻意的 fail-closed），所以这两个数就是"她突然不说话了"的出口：
    # `/super status` 会印出来。只有计数，没有正文。
    judge_failed: int = 0
    reply_failed: int = 0
    # 一轮对话超过**总预算**的次数（2026-10-08 失败降级审计 2）：主循环串行，没有
    # 上界时模型"拖着不回"会把整个接收循环卡住（连 ping / 管理命令 / 私聊都进不来）。
    # 超时 = 这一条没有回复，但**消息仍在历史里**——只有计数，没有正文。
    turn_timeouts: int = 0
    # 关系：判定报了几次"这一句越界"（当轮升防备），以及其中被机械上限拒了几次。
    # 前者是"她当场变冷"的次数，后者应当很少（上限是防模型抽风的）。
    guard_raised: int = 0
    guard_rejected: int = 0
    # 「有人打探她怎么做出来的，所以这一轮不接」的次数（2026-10-09）。
    # 它**不写关系轴**（探机制不是针对她本人的冒犯），只是"这一轮为什么没开口"的出口。
    guard_probe_held: int = 0
    # 「判定抄回来的那一句原话，核不过这一轮的输入」的次数（2026-10-16）。
    # 核不过就**不存证据**（只记数、不记内容）——它应当是 0 或极少：常红说明模型在
    # 改写原话，证据链断了。`evidence_stored` 是真正落进旁表的次数，两个数一起看。
    quote_unverified: int = 0
    evidence_stored: int = 0
    # 风格审核（**只判不改**，2026-10-05）打回、要求重写的条数。
    # `reply_review_rejected_final` 是"重写后仍被拒、照重写的那一版发出"的条数
    # （它不该是常态：常态是第一次就过，或者重写一次就过）。没配审核时三者恒为 0。
    # `reply_review_rewrite_declined`（2026-10-11）是"她把原来那句**原样写回来**"的条数：
    # 那等于她说"这句我不改"，照第一版发。风格这一趟才可能发生，另两趟不传这个退路。
    reply_reviewed: int = 0
    reply_review_rejected_final: int = 0
    reply_review_rewrite_declined: int = 0
    # 审核自己的运行计数（2026-10-05 追加：**把拦截率露出来**，靠数字判松紧，不靠感觉）。
    # `review_calls` 是它判断过的草稿数（含机器判的空回复），`review_blocked` 是判不过的次数，
    # `review_unrecognized` 是"重试一次之后仍读不出判定"的次数（那种情况按通过处理），
    # `review_failed` 是审核那一趟自己挂掉/超时的次数。**长期 0 拦截 = 审核没在工作；
    # 拦截率很高 = 可能过紧。** 没配审核时全为 0。
    review_calls: int = 0
    review_blocked: int = 0
    review_unrecognized: int = 0
    review_failed: int = 0
    # 识图成功把 `[图片]` 换成描述的条数（没配识图时恒为 0）。
    media_described: int = 0
    # 「没把握就别断言」（旧名「不懂就问」，2026-10-05 晚改口径）。
    # `clarify_quiet` 是"没被叫到、没懂、又是具体话题，所以没插话"的次数；
    # `reply_grounding_rewrites` 是"被叫到、话里有具体断言、知识库/记忆/语境三处
    # 都没有根据，于是被打回重写一次"的次数。关掉 `ask_when_unsure` 开关时两者恒为 0。
    # 只有计数，没有正文。
    clarify_quiet: int = 0
    reply_grounding_rewrites: int = 0
    # 「有人拿工具式的称呼在叫她」（2026-10-20）：这一轮往易变段递了那一句的**条数**。
    # 它是**分母**（这种叫法有多常见），不是错误计数——长期为 0 才说明判据没在工作。
    # 只有计数，**不记正文、也不记那个称呼**。
    reply_tool_vocative_hints: int = 0
    # 「她把过往说成事实」（2026-10-18 交底、2026-10-19 接线；同一个
    # `ask_when_unsure` 开关下，关掉它这三个数恒为 0）：
    # `reply_past_claim_hits` 是判据命中的次数（**分母**：含"最长片段太短、按口径放行"
    # 的那几例——那些不核，但"她在讲自己的过去"这件事已经发生了）；
    # `reply_past_claim_rewrites` 是材料里对不上、**打回重写一次**的次数；
    # `reply_past_claim_unfixed` 是重写之后**仍对不上、照发**的次数——这一关**绝不丢回复**
    # （与 `data_marker_drops` 那一关刻意不同），所以它非零只说明"这一句没救回来"，
    # 不代表有一句话没发出去。只有计数，**不记正文**。
    reply_past_claim_hits: int = 0
    reply_past_claim_rewrites: int = 0
    reply_past_claim_unfixed: int = 0
    # 「预设型编造」（2026-10-20 加；同一个 `ask_when_unsure` 开关下，关掉它恒为 0）：
    # 她把一件有"经历门槛"的事（六级 / 高考 / 宿舍 / 面试……）当成**已经发生过**
    # 摆了出来，而这一轮材料里那个词出现 **0 次** → 打回重写一次的次数。
    # 它与 `reply_past_claim_rewrites` **共用每轮一次**的重写额度，所以两者不会同时加。
    # 只有计数，**不记正文、也不记是哪个词**。
    reply_presupposition_rewrites: int = 0
    # 不可信数据的短码（`data_boundary`，2026-10-08）：`data_marker_retries` 是
    # "回复里抄了当轮短码、被打回重写一次"的次数；`data_marker_drops` 是"重写又抄、
    # 于是这一条没发出去"的次数（fail-closed）。只有计数，**不含短码、不含正文**。
    data_marker_retries: int = 0
    data_marker_drops: int = 0
    # 出站正文里剥掉的**孤立标签片段**个数（2026-10-09）：模型吐了半截协议 XML，
    # 收尾标签（`</parameter>` 那种）留在正文里，会在送出去之前被剥掉并记在这里。
    # 只有计数：**不记正文**，日志里也只有标签名。
    reply_tags_scrubbed: int = 0
    # 裸正文兜底（2026-10-17）：模型**整个信封都没吐**（`<dialogue>`/`<reply>`/
    # `<reply_to>`/`<context>` 一个都没有）、只回了一句好正文，于是那段正文被当正文
    # 发了出去。它顶着的是"判定说要回但回复空手 → 这一轮沉默"那条路
    # （2026-10-17 一天 26 次、占当天"决定说话"轮次的 44.8%）。
    # 与 `empty_forced_reply` 是同一件事的两头：兜底生效时那个数就不涨了。
    # **长期非零不是好消息**——模型还在漏信封，只是这次没把话丢掉。只有计数，没有正文。
    reply_recovered_from_bare_text: int = 0
    # 当前当值状态：热群、当值多久、排队深度。只有数字与群号，没有正文。
    focus: dict[str, object] | None = None
    # 判定采样（2026-10-10）的观测面：各会话当前档位 / 攒批条数 / 攒了几条 /
    # 跳过几次，加上全部会话累计跳过的总次数。只有数字与会话 id，没有正文。
    judge_sampling: dict[str, object] | None = None
    memory: dict[str, object] | None = None
    # 脱敏运行指标：固定类别的计数与耗时分布，不含正文、QQ 号或凭据。
    metrics: dict[str, object] | None = None
    # 各功能的输入输出日志状态（判定/回复/记忆/规则）：只有计数、行数、字节数与
    # 最后几条的**预览**，正文只在 data/logs/ 的文件里。
    model_trace: dict[str, object] | None = None
    # 本次进程启动的时间与内存占用（字节）。给 `/super status` 用。
    #
    # **两个起始点，各管一摊**（2026-10-08 安全与时间口径审计 B-02）：
    # `started_at` 是墙上时刻，只用来印"开始于"；`started_monotonic` 是单调读数
    # （与引擎注入的 `clock` 同一个钟），只用来算"已运行多久"。拿 `started_at` 去减
    # 当前墙上时刻算时长，会被 NTP 校时/改表拨动——最坏被夹成"已运行 0 秒"。
    started_at: float = 0.0
    started_monotonic: float = 0.0
    # 被"会话数上限"淘汰掉的短期会话数（2026-10-08 隔离增长审计 [1]）。它是
    # "内存里的会话为什么少了"的唯一出口：只有计数，没有正文、没有群号。
    sessions_evicted: int = 0
    memory_bytes: int = 0
    memory_peak_bytes: int = 0
    persistence_enabled: bool = False
