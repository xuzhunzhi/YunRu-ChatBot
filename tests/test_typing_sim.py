"""拟人化节奏的离线覆盖：分段切分与打字停顿。

这些规则直接决定用户看到几条消息、隔多久看到，所以边界要钉死：
不能把单换行当分段、不能切出空段、停顿不能长到像卡死。
"""
import random

from qq_roleplay_bot.typing_sim import (
    FIRST_BASE_SECONDS,
    MAX_PAUSE_SECONDS,
    MAX_SEGMENTS,
    MIN_GAP_SECONDS,
    delay_plan,
    segment_delay,
    split_segments,
    total_pause,
)


# --- 分段 -------------------------------------------------------------------


def test_single_line_stays_one_segment() -> None:
    assert split_segments("我在。") == ("我在。",)


def test_blank_line_splits_into_separate_messages() -> None:
    assert split_segments("我在。\n\n继续说。") == ("我在。", "继续说。")


def test_single_newline_does_not_split() -> None:
    """单换行常只是排版，不该被当成两条消息发出去。"""

    assert split_segments("第一行\n第二行") == ("第一行\n第二行",)


def test_multiple_blank_lines_collapse() -> None:
    assert split_segments("甲\n\n\n\n乙") == ("甲", "乙")


def test_empty_and_whitespace_only_yield_nothing() -> None:
    assert split_segments("") == ()
    assert split_segments("   ") == ()
    assert split_segments("\n\n") == ()


def test_segments_are_stripped() -> None:
    assert split_segments("  甲  \n\n  乙  ") == ("甲", "乙")


def test_overflow_is_merged_not_dropped() -> None:
    """超过上限时并入最后一段——丢字比多一条消息更糟。"""

    text = "\n\n".join(f"第{i}段" for i in range(1, 6))
    parts = split_segments(text)
    assert len(parts) == MAX_SEGMENTS
    # 所有原文内容都还在。
    joined = "".join(parts)
    for i in range(1, 6):
        assert f"第{i}段" in joined


# --- 停顿 -------------------------------------------------------------------


def test_first_pause_is_longer_than_follow_ups() -> None:
    """首条要包含"读完 + 想一下"，后续几条只是打字时间。"""

    rng = random.Random(7)
    first = segment_delay("同样长度的一句话", index=0, rng=rng)
    later = segment_delay("同样长度的一句话", index=1, rng=rng)
    assert first > later
    assert first >= FIRST_BASE_SECONDS * 0.7
    assert later >= MIN_GAP_SECONDS * 0.7


def test_longer_text_waits_longer() -> None:
    rng = random.Random(11)
    short = segment_delay("嗯。", index=0, rng=rng)
    long = segment_delay("这是一句明显更长的话，需要更久才能打完。" * 2, index=0, rng=rng)
    assert long > short


def test_pause_is_capped() -> None:
    """再长的消息也不能让用户以为 Bot 挂了。"""

    huge = "字" * 5000
    assert segment_delay(huge, index=0, rng=random.Random(1)) <= MAX_PAUSE_SECONDS


def test_pause_is_never_negative() -> None:
    for index in range(4):
        assert segment_delay("", index=index, rng=random.Random(index)) >= 0.0


def test_jitter_stays_within_ratio() -> None:
    """抖动不能让停顿跑到估算值之外太远。"""

    kwargs = dict(per_char_seconds=0.1, first_base_seconds=1.0,
                  jitter_ratio=0.2, max_pause_seconds=99.0)
    values = {round(segment_delay("十个字的一句话吧", index=0, rng=random.Random(s), **kwargs), 3)
              for s in range(30)}
    # 估算值 = 1.0 + 8 * 0.1 = 1.8，抖动 ±20% => [1.44, 2.16]
    assert min(values) >= 1.44 - 0.001
    assert max(values) <= 2.16 + 0.001
    assert len(values) > 1, "抖动应当产生不同的停顿"


def test_delay_plan_length_matches_segments() -> None:
    segments = ("甲", "乙", "丙")
    plan = delay_plan(segments, rng=random.Random(3))
    assert len(plan) == len(segments)
    assert all(p > 0 for p in plan)


def test_delay_plan_is_deterministic_with_seeded_rng() -> None:
    segments = ("甲甲甲", "乙")
    first = delay_plan(segments, rng=random.Random(42))
    second = delay_plan(segments, rng=random.Random(42))
    assert first == second


def test_total_pause_sums_the_plan() -> None:
    segments = ("甲", "乙")
    rng = random.Random(5)
    plan = delay_plan(segments, rng=rng)
    rng2 = random.Random(5)
    assert total_pause(segments, rng=rng2) == round(sum(plan), 3)
