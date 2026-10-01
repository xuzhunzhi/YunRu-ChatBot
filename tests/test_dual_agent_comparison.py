"""双 agent + 压缩的设计验证。

要验证的核心性质：

1. **前缀可复用**：回复段的活窗口在两次压缩之间逐轮是追加关系，前缀不断——
   这是缓存命中的前提。前缀只在压缩那一刻断裂，且断裂次数等于压缩次数。
2. **上下文不断**：旧对话不丢，而是压成摘要一直带着。
3. **窗口有界**：活窗口在 `[COMPACT_KEEP, LIVE_TARGET]` 之间循环，不会无限增长。
"""
import asyncio

from qq_roleplay_bot.stage3_main import COMPACT_KEEP, LIVE_TARGET, DialogueEngine
from qq_roleplay_bot.transport import IncomingMessage, MessageTarget

GROUP = "717151356"
TARGET = MessageTarget(group_id=GROUP)

SUMMARY_MARK = "【摘要】"

# 跨过一次压缩所需的最小轮数：每条消息都会得到一条回复，所以历史每轮涨 2 条。
CYCLE_TURNS = LIVE_TARGET // 2 + 20
# 离阈值还远，用来观察"不压缩时前缀是否追加"。
SHORT_TURNS = LIVE_TARGET // 4
# 跨过多次压缩，用来观察周期性质而不是单次行为。
LONG_TURNS = LIVE_TARGET * 3 // 2
# 前几轮 prompt 太短，比例天然偏低（第 1 轮只有 1 条消息），统计性质时跳过。
WARMUP = 3


class Judge:
    async def complete(self, request):
        return "<route>REPLY</route><topic>话题</topic>"


class Reply:
    """回复 agent：返回固定的回复，并记录收到的请求。"""

    def __init__(self):
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        return ("<decision>REPLY</decision><dialogue>KEEP</dialogue><reply>收到。</reply>")


class Compactor(Reply):
    """同时充当压缩 client：包含压缩 prompt 特征就返回一段摘要。

    `compactions` 记的是**调用**（一轮里可能有好几批），`turns` 记的是**发生压缩的轮次**
    （同一轮多批只记一次）。前缀断裂是"每轮一次"，所以要跟 `turns` 比，不是跟调用数比。
    """

    def __init__(self):
        super().__init__()
        self.compactions = []
        self.turns = []
        self.slot = {"turn": 0}

    async def complete(self, request):
        if "压缩存档" in request[0]["content"]:
            self.compactions.append(request)
            if not self.turns or self.turns[-1] != self.slot["turn"]:
                self.turns.append(self.slot["turn"])
            return SUMMARY_MARK + "早先聊了不少事。"
        return await super().complete(request)


def build(n):
    return [IncomingMessage(
        message_id=f"x{i}", session_id=f"group:{GROUP}", user_id="100",
        text=f"@第{i}句", target=TARGET, sender_name="某人", is_bot_mentioned=True,
    ) for i in range(1, n + 1)]


def prefix_len(a, b):
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


def run(dual, n):
    """跑 n 条消息，返回 (稳定段序列, 引擎, 压缩 client 或 None)。"""

    async def main():
        reply = Reply()
        compactor = Compactor() if dual else None
        # 压缩走 judge_client（避免与对话共用同一份缓存隔离空间）。
        engine = DialogueEngine(
            reply, judge_client=(Judge() if dual else None),
        )
        if compactor is not None:
            engine.judge_client = _JudgeAndCompactor(compactor)
        for position, message in enumerate(build(n), start=1):
            if compactor is not None:
                compactor.slot["turn"] = position
            await engine.handle(message)
        return [r[1]["content"] for r in reply.requests], engine, compactor

    return asyncio.run(main())


class _JudgeAndCompactor:
    """把判定与压缩接到同一个 client 上（都走 judge 侧）。"""

    def __init__(self, inner):
        self.inner = inner

    async def complete(self, request):
        if "回应判定器" in request[0]["content"]:
            return "<route>REPLY</route><topic>话题</topic>"
        return await self.inner.complete(request)


# --- 前缀可复用 -------------------------------------------------------------


def test_prefix_is_append_only_between_compactions() -> None:
    """没跨过压缩阈值时，回复段逐轮是追加关系——前缀完整复用。

    这是缓存命中的前提：活窗口只增长、不滑动，所以上一轮的 prompt 逐字是这一轮的
    前缀。单 agent 带全量历史做不到这一点（撞上 HISTORY_LIMIT 后开始滑动）。
    """

    records, _, compactor = run(dual=True, n=SHORT_TURNS)
    assert not compactor.compactions, "这个长度不该触发压缩"
    ratios = [prefix_len(a, b) / max(len(a), 1) for a, b in zip(records, records[1:])]
    assert all(r > 0.9 for r in ratios[WARMUP:]), [f"{r:.0%}" for r in ratios]


def test_prefix_breaks_only_at_compaction() -> None:
    """前缀只在压缩那一刻断裂，且**断裂次数等于发生压缩的轮数**。

    这是"有界 prompt"与"稳定前缀"能同时成立的原因：断裂不是每轮发生，而是稀有的
    周期性事件。一旦前缀在非压缩轮也断，说明窗口又在滑动了。

    注意跟"压缩调用次数"区分：一轮里可能因为切批发出好几次压缩请求（见
    `split_compaction_batches`），但那仍然只是**一次**前缀断裂。
    """

    records, _, compactor = run(dual=True, n=LONG_TURNS)
    assert len(compactor.turns) >= 2, "需要跨过至少两次压缩才有统计意义"
    ratios = [prefix_len(a, b) / max(len(a), 1) for a, b in zip(records, records[1:])]
    breaks = [i for i, r in enumerate(ratios) if r < 0.5]
    assert len(breaks) == len(compactor.turns), (
        f"断裂 {len(breaks)} 次，发生压缩的轮数 {len(compactor.turns)}，两者应当一一对应")
    assert min(ratios) < 0.5, "压缩必然让前缀断裂（摘要与旧消息都被换掉）"
    kept = [r for i, r in enumerate(ratios) if i not in set(breaks) and i >= WARMUP]
    assert all(r > 0.9 for r in kept), f"非压缩轮的前缀不该断裂: {min(kept):.0%}"


def test_reply_prompt_stays_bounded_by_the_compaction_threshold() -> None:
    """回复段的活窗口有界：最多 LIVE_TARGET 条，压缩后回落到 COMPACT_KEEP。"""

    records, _, compactor = run(dual=True, n=LONG_TURNS)
    counts = [r.count("<message ") for r in records]
    assert max(counts) <= LIVE_TARGET + 2, f"活窗口失控: {max(counts)}"
    ratios = [prefix_len(a, b) / max(len(a), 1) for a, b in zip(records, records[1:])]
    breaks = [i for i, r in enumerate(ratios) if r < 0.5]
    assert breaks, "这个长度应当已经压缩过"
    after = [counts[i + 1] for i in breaks]
    assert max(after) <= COMPACT_KEEP + 2, f"压缩后窗口应当回落到保留条数: {after}"


def test_long_conversation_gets_compacted() -> None:
    """跨过阈值时必须发生压缩，而不是无限增长或直接丢消息。"""

    _, engine, compactor = run(dual=True, n=CYCLE_TURNS)
    assert compactor is not None
    assert compactor.compactions, "跨过阈值后应当触发压缩"
    state = engine.sessions.state(f"group:{GROUP}")
    assert state.summary.startswith(SUMMARY_MARK)
    assert state.summary_through is not None


def test_summary_is_kept_in_the_reply_prompt() -> None:
    """压缩后的摘要必须继续出现在回复段里——否则旧上下文就丢了。"""

    records, engine, _ = run(dual=True, n=CYCLE_TURNS)
    state = engine.sessions.state(f"group:{GROUP}")
    assert state.summary
    assert SUMMARY_MARK in records[-1], "摘要必须带在回复段里"
    assert "<earlier_summary>" in records[-1]


def test_compaction_is_rare_not_per_turn() -> None:
    """压缩应当稀有：只在跨过阈值时发生，之后重新累积。"""

    n = LONG_TURNS
    _, _, compactor = run(dual=True, n=n)
    assert len(compactor.compactions) < n / 10, (
        f"压缩过于频繁: {len(compactor.compactions)} 次 / {n} 轮")


def test_compaction_empties_the_window_so_it_refills() -> None:
    """压缩后窗口必须被清到很小，于是重新累积——这是"稀有"的机制保证。

    如果压缩后窗口仍然满着，下一轮会立刻再压一次（实测出现过 560 轮压 310 次）。
    """

    records, _, compactor = run(dual=True, n=LONG_TURNS)
    counts = [r.count("<message ") for r in records]
    ratios = [prefix_len(a, b) / max(len(a), 1) for a, b in zip(records, records[1:])]
    breaks = [i for i, r in enumerate(ratios) if r < 0.5]
    assert compactor.compactions and breaks
    # 每次压缩之后窗口都应该从很小的值重新往上长，而不是停在阈值附近反复触发
    for i in breaks:
        assert counts[i + 1] <= COMPACT_KEEP + 2, f"压缩后窗口没清空: {counts[i + 1]}"
        assert counts[i] > COMPACT_KEEP, "压缩前窗口应当已经攒满"
    assert max(counts) > COMPACT_KEEP * 2, "窗口应当重新累积起来"


def test_single_agent_keeps_working_without_judge() -> None:
    """单 agent 回退路径照常工作。"""

    records, engine, _ = run(dual=False, n=20)
    assert records
    assert engine.snapshot().judge_calls == 0


# --- 上下文不断 -------------------------------------------------------------


def test_summary_text_survives_compaction_sanitization() -> None:
    """摘要进 prompt 前要脱敏限长，且不能带标签。"""

    from qq_roleplay_bot.dialogue_compaction import (
        MAX_SUMMARY_CHARS,
        merge_summary,
        parse_compaction_output,
    )

    assert parse_compaction_output("<route>不该出现</route>正常内容") == "正常内容"
    assert len(parse_compaction_output("长" * 5000)) <= MAX_SUMMARY_CHARS
    assert parse_compaction_output("前\x00中\x1b后") == "前中后"

    merged = merge_summary("旧摘要", "新内容")
    assert merged.startswith("旧摘要"), "旧摘要必须逐字保留（前缀稳定性依赖它）"
    assert "新内容" in merged
    assert len(merge_summary("长" * 5000, "短")) <= MAX_SUMMARY_CHARS


def test_compaction_prompt_treats_source_as_data() -> None:
    """压缩请求必须声明原文是 DATA，不是指令。"""

    from qq_roleplay_bot.dialogue_compaction import build_compaction_messages

    request = build_compaction_messages(build(3))
    system = request[0]["content"]
    assert "DATA" in request[1]["content"]
    assert "忽略" not in system or "不要" in system
    assert "<decision>" not in system


def test_compaction_failure_keeps_history_intact() -> None:
    """压缩失败只该表现为"还没压缩"，不能丢消息。"""

    class FailingCompactor:
        async def complete(self, request):
            if "压缩存档" in request[0]["content"]:
                raise RuntimeError("compaction down")
            if "回应判定器" in request[0]["content"]:
                return "<route>REPLY</route>"
            return "<decision>REPLY</decision><reply>收到。</reply>"

    async def main():
        engine = DialogueEngine(FailingCompactor(), judge_client=FailingCompactor())
        for message in build(CYCLE_TURNS):
            await engine.handle(message)
        return engine

    engine = asyncio.run(main())
    state = engine.sessions.state(f"group:{GROUP}")
    assert state.summary == "", "压缩失败时摘要应保持为空"
    assert len(state.history) > 0, "历史不应被丢空"
