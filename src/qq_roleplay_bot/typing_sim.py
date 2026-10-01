"""把一条回复切成"像人连着发的几条"，并给出每次发送前的停顿。

为什么需要它：模型一次只吐一段正文，框架抓到一个 OutgoingMessage 就立刻发出去。
结果是无论内容长短都在 1 秒左右秒回，而且永远只有一条——像公告不像聊天。

这里只做两件事，都不碰模型：
1. 按**空行**把正文切成若干条独立消息（单换行不算断开）；
2. 按字符数估算打字时间，给出每条消息发送前应该停顿多久。

延迟参数是刻意偏保守的：宁可略慢，也不要快到显假。真实的人读一句话、
想一下、再打出十几个字，通常在两秒以上。
"""
from __future__ import annotations

import random
import re

# 一条回复最多拆成几条。超过这个数就不是"分几句说"，而是在刷屏。
MAX_SEGMENTS = 3

# 首次回应前的基础停顿：对应"读完对方的话 + 想一下"。
FIRST_BASE_SECONDS = 1.4
# 每条消息按字符数增加的打字时间。
PER_CHAR_SECONDS = 0.075
# 两条消息之间的最小间隔，避免看起来像同时发出。
MIN_GAP_SECONDS = 0.7
# 单次停顿上限：再长就会让人以为 Bot 卡死了。
MAX_PAUSE_SECONDS = 6.0
# 抖动比例，让每次停顿不完全一样。
JITTER_RATIO = 0.22

# 只按空行切分：作者用空行表示"这是下一句"，单换行往往只是排版。
_SEGMENT_SPLIT = re.compile(r"\n\s*\n+")


def split_segments(text: str, *, limit: int = MAX_SEGMENTS) -> tuple[str, ...]:
    """把回复正文切成要分次发出的段落。

    空行是唯一的分隔信号。切不出多段时原样返回单段。
    """

    if not text or not text.strip():
        return ()
    parts = [part.strip() for part in _SEGMENT_SPLIT.split(text)]
    parts = [part for part in parts if part]
    if not parts:
        return ()
    if len(parts) <= limit:
        return tuple(parts)
    # 超出的部分并入最后一段，而不是丢弃——丢字比刷屏更糟。
    head = parts[: limit - 1]
    tail = "\n\n".join(parts[limit - 1:])
    return tuple(head + [tail])


def segment_delay(
    text: str,
    *,
    index: int,
    rng: random.Random | None = None,
    per_char_seconds: float = PER_CHAR_SECONDS,
    first_base_seconds: float = FIRST_BASE_SECONDS,
    min_gap_seconds: float = MIN_GAP_SECONDS,
    max_pause_seconds: float = MAX_PAUSE_SECONDS,
    jitter_ratio: float = JITTER_RATIO,
) -> float:
    """第 index 段（从 0 开始）发送前应停顿的秒数。"""

    source = rng or random
    base = first_base_seconds if index == 0 else min_gap_seconds
    estimate = base + len(text) * per_char_seconds
    jitter = source.uniform(-jitter_ratio, jitter_ratio) * estimate
    return round(max(0.0, min(max_pause_seconds, estimate + jitter)), 3)


def delay_plan(
    segments: tuple[str, ...] | list[str],
    *,
    rng: random.Random | None = None,
    **kwargs: float,
) -> tuple[float, ...]:
    """给出每一段对应的停顿；长度与 segments 一致。"""

    return tuple(
        segment_delay(text, index=index, rng=rng, **kwargs)
        for index, text in enumerate(segments)
    )


def total_pause(segments: tuple[str, ...] | list[str], *, rng: random.Random | None = None) -> float:
    """整条回复的总停顿，用于日志与上限判断。"""

    return round(sum(delay_plan(segments, rng=rng)), 3)
