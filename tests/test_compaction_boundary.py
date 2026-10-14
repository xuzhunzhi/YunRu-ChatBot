"""压缩的边界铁律：**标记为已摘要的条数，永远不能超过真的压进去的条数**。

由来（实测）：压缩原文原先按字数装、装满就 break，而删除却按原计划全删——
520 条的窗口里，470 条被标记为"已摘要"，只有 318 条真进了压缩请求，
**剩下 152 条既没进摘要、又被移出窗口**，永久消失且没有任何日志。
群里消息再长一点就会触发，所以这里把铁律钉成断言。

另一条是"标记瘦身"不能改含义：谁说的、第几条必须仍然能从渲染结果里读出来。
"""
import asyncio

from qq_roleplay_bot.dialogue_compaction import (
    COMPACT_BATCH_MESSAGES,
    MAX_SOURCE_CHARS,
    build_compaction_messages,
    split_compaction_batches,
)
from qq_roleplay_bot.stage3_main import COMPACT_KEEP, LIVE_TARGET, DialogueEngine
from qq_roleplay_bot.stage3_runtime import ConversationState, _format_history
from qq_roleplay_bot.transport import IncomingMessage, MessageTarget

GROUP = "717151356"
TARGET = MessageTarget(group_id=GROUP)
SESSION = f"group:{GROUP}"


def marker(index: int) -> str:
    """零填充的唯一标记，避免"第1句"命中"第10句"这种子串误判。"""

    return f"第{index:04d}句"


def message(index: int, *, text_len: int = 40, speaker: int = 0) -> IncomingMessage:
    """正文带唯一标记 + 指定长度：既能核对"哪条进过压缩"，又能撑爆字数预算。"""

    body = marker(index) + "字" * max(0, text_len - len(marker(index)))
    return IncomingMessage(
        message_id=f"m{index}",
        session_id=SESSION,
        user_id=f"100{speaker % 3}",
        text=body,
        target=TARGET,
        sender_name=f"某人{speaker % 3}",
    )


def build_state(count: int, *, text_len: int = 40) -> ConversationState:
    state = ConversationState(history_limit=2000)
    for index in range(count):
        state.add(message(index, text_len=text_len, speaker=index), 0.0)
    return state


class ScriptedCompactor:
    """记录每一次压缩请求的原文，返回一段固定摘要；可指定在第几批之后失败。"""

    def __init__(self, fail_after: int | None = None) -> None:
        self.requests: list[str] = []
        self.fail_after = fail_after

    async def complete(self, request):
        if self.fail_after is not None and len(self.requests) >= self.fail_after:
            raise RuntimeError("compaction down")
        self.requests.append(request[1]["content"])
        return f"第{len(self.requests)}批摘要。"


def compress(state: ConversationState, compactor: ScriptedCompactor) -> DialogueEngine:
    engine = DialogueEngine(compactor, judge_client=compactor)
    asyncio.run(engine._maybe_compact(state, session_id=SESSION))
    return engine


def covered_indexes(requests: list[str], total: int) -> set[int]:
    joined = "\n".join(requests)
    return {index for index in range(total) if marker(index) in joined}


# --- 切批本身 ---------------------------------------------------------------


def test_batches_cover_every_message_exactly_once_and_in_order() -> None:
    messages = [message(i) for i in range(300)]
    batches = split_compaction_batches(messages)
    flat = [item for batch in batches for item in batch]
    assert flat == messages, "切批必须保持顺序、不重不漏"
    assert all(len(batch) <= COMPACT_BATCH_MESSAGES for batch in batches)


def test_a_single_oversized_message_is_never_truncated_or_dropped() -> None:
    """单条超预算时宁可单独成批，也不许把它截断或跳过。"""

    huge = message(1, text_len=MAX_SOURCE_CHARS * 2)
    batches = split_compaction_batches([huge, message(2)])
    assert batches[0] == [huge], "超预算的那条应当单独成批"
    assert batches[1] == [message(2)]


def test_every_message_in_a_batch_reaches_the_request() -> None:
    batch = [message(i) for i in range(120)]
    body = build_compaction_messages(batch)[1]["content"]
    missing = [item.message_id for item in batch if marker(int(item.message_id[1:])) not in body]
    assert missing == [], f"这些条没进压缩请求: {missing}"


# --- 铁律：边界不许超过实际压到的位置 ---------------------------------------


def test_marking_never_exceeds_what_was_actually_compressed() -> None:
    """正文撑爆字数预算时，被移除的每一条都必须真的进过某次压缩请求。"""

    state = build_state(520, text_len=40)
    compactor = ScriptedCompactor()
    compress(state, compactor)

    assert state.summary_through is not None
    covered = covered_indexes(compactor.requests, 520)
    expected = set(range(state.summary_through + 1))
    assert covered == expected, (
        f"标记了 {len(expected)} 条，实际只压到 {len(covered)} 条，"
        f"漏掉 {sorted(expected - covered)[:5]}"
    )
    # 窗口里不许再留着已摘要的消息
    assert state.seq_range() is not None
    assert state.seq_range()[0] == state.summary_through + 1


def test_compaction_still_keeps_the_tail() -> None:
    """切批之后仍然保留最近 COMPACT_KEEP 条，窗口行为不变。"""

    state = build_state(520)
    compress(state, ScriptedCompactor())
    assert len(state.history) == COMPACT_KEEP
    assert state.summary
    assert state.summary_through == 520 - COMPACT_KEEP - 1


def test_batch_failure_keeps_the_rest_of_the_history() -> None:
    """某一批失败：之前压成功的保留，之后的一条都不许动（边界停在成功处）。"""

    state = build_state(520, text_len=400)   # 长正文 → 多批
    compactor = ScriptedCompactor(fail_after=1)
    compress(state, compactor)

    assert len(compactor.requests) == 1, "失败后不该继续往下压"
    assert state.summary_through is not None
    failed_boundary = state.summary_through
    assert failed_boundary < 520 - COMPACT_KEEP - 1, "只应当推进了第一批"
    assert len(state.history) == 520 - failed_boundary - 1


def test_no_compaction_below_the_threshold() -> None:
    state = build_state(LIVE_TARGET)
    compactor = ScriptedCompactor()
    compress(state, compactor)
    assert compactor.requests == []
    assert state.summary_through is None


# --- 标记瘦身：省地方但不能改含义 -------------------------------------------


def test_compact_history_still_identifies_every_speaker() -> None:
    """瘦身之后仍能读出"谁说的"：名册给出身份，who 只在换人时写一次。"""

    history = [
        message(0, speaker=0),
        message(1, speaker=0),
        message(2, speaker=1),
        message(3, speaker=2),
    ]
    state = ConversationState(history_limit=2000)
    for item in history:
        state.add(item, 0.0)
    lines = _format_history(
        history, seq_of=lambda item: history.index(item), alias_of=state.alias_of,
    ).splitlines()
    assert 'who="1"' in lines[0], "第一次出现的人必须写 who"
    # 2026-09-28 改：**每条都写 who**。原来"同一个人连着说就不重复写"省字符，
    # 代价是模型要自己把身份顺下去；群里人多消息密时一顺错就把两个人认成一个。
    assert 'who="1"' in lines[1], "同一个人的第二条也要写 who（身份不能再靠顺移）"
    assert 'who="2"' in lines[2], "换人必须重新写 who"
    assert 'who="3"' in lines[3]
    assert 'speaker="user"' not in "\n".join(lines), "用户消息不写 speaker"
    assert all(f'index="{i}"' in lines[i] for i in range(4)), "编号必须还在"
    roster = state.roster_lines()
    assert len(roster) == 3
    assert roster[0].endswith("（QQ1000）"), roster


def test_compact_history_keeps_the_mention_flag_only_when_true() -> None:
    mentioned = IncomingMessage(
        message_id="m9", session_id=SESSION, user_id="100", text="在吗",
        target=TARGET, sender_name="某人", is_bot_mentioned=True,
    )
    state = ConversationState(history_limit=2000)
    state.add(mentioned, 0.0)
    state.add(message(10), 1.0)
    rendered = _format_history(
        [mentioned, message(10)], seq_of=lambda item: 1, alias_of=state.alias_of,
    )
    assert 'mentioned="true"' in rendered
    assert 'mentioned="false"' not in rendered


def test_compact_rendering_keeps_the_bot_marker() -> None:
    bot = IncomingMessage(
        message_id="b1", session_id=SESSION, user_id="yunru", text="我在。",
        target=TARGET, sender_name="YunRu", sender_role="bot", is_bot_message=True,
    )
    state = ConversationState(history_limit=2000)
    state.add(message(0), 0.0)
    state.add(bot, 1.0)
    rendered = _format_history([message(0), bot], seq_of=lambda item: 1,
                               alias_of=state.alias_of)
    assert 'speaker="yunru"' in rendered


# --- 话题锚点 ---------------------------------------------------------------


def test_topic_start_only_moves_forward() -> None:
    """话题起点只许前进。

    2026-10-02 一度改成"往前往后都允许"，随后从 `judge.jsonl` 的 1464 条判定里
    数出：88% 原地确认、12% 前进、**0% 后退**——判定从不请求后退，
    所以"只许前进"挡掉的动作根本不发生，而放开它会白送一份缓存风险。
    改回单向。理由与实测数字写在 `stage3_runtime.advance_topic_start` 的 docstring 里。
    """

    state = build_state(20)
    assert state.advance_topic_start(5) is True
    assert state.topic_start_seq == 5
    assert state.advance_topic_start(3) is False, "不许后退"
    assert state.topic_start_seq == 5
    assert state.advance_topic_start(5) is False, "同一个编号不算变化"
    assert state.advance_topic_start(7) is True


def test_topic_history_is_clamped_to_the_anchor() -> None:
    state = build_state(20)
    assert len(state.topic_history()) == 20, "没有锚点时就是整个窗口"
    state.advance_topic_start(15)
    kept = state.topic_history()
    assert [state.seq_of(item) for item in kept] == [15, 16, 17, 18, 19]


def test_compaction_resets_the_topic_start_to_the_new_window() -> None:
    """压缩之后起点必须落到保留窗口的第一条——"压缩会切进话题"的正式说法。"""

    state = build_state(520)
    compress(state, ScriptedCompactor())
    assert state.topic_start_seq == state.seq_range()[0]
    assert state.topic_start_seq == 520 - COMPACT_KEEP


def test_aliases_are_monotonic_and_survive_persistence() -> None:
    """别名只分配一次、不重排；持久化后同一个人还是同一个编号。"""

    state = ConversationState(history_limit=2000)
    for index in range(6):
        state.add(message(index, speaker=index), 0.0)
    before = dict(state.aliases)
    assert sorted(before.values()) == [1, 2, 3]
    raw = state.as_state()
    restored = ConversationState.from_state(raw, history_limit=2000)
    assert restored is not None
    assert restored.aliases == before
    assert restored.alias_names == state.alias_names
    # 新人接着往下编号，不复用
    newcomer = IncomingMessage(
        message_id="m99", session_id=SESSION, user_id="9001", text="新人说话",
        target=TARGET, sender_name="新人",
    )
    restored.add(newcomer, 1.0)
    assert restored.aliases["9001"] == 4
