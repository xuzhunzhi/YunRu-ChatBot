"""焦点排队：**单个群的队列有上限**，超了丢最旧并计数（隔离增长审计 [7]）。

从前 `FocusController.enqueue` 用的是一句 `deque()`——**没有 maxlen**。而队列里存的
是**消息全文**（`IncomingMessage`），入队又完全由外部输入决定（一个群被刷屏、或者
它长期当不上值）。审计的原话是"外部刷屏能顶掉内存"。

现在每个群最多排 `queue_limit` 条（`FOCUS_QUEUE_LIMIT`，默认 50），满了丢**最旧**的，
丢掉的条数记在 `dropped` 上，并经 `focus_stats()` 进 `/super status`（"已丢最旧 N 条"）
——**丢了要看得见**，不然"她怎么没回我"永远查不出来。

这一份钉住：

1. 满了丢的是**最旧**的那条（新的留下）；
2. 跑列深度永远不超过上限；
3. 丢掉的条数有计数，而且 `/super status` 上真的印出来；
4. 没满的时候一条都不丢（默认行为不变）。

去掉上限那段（突变 L）→ 第 1、2、3 条红。
"""
import asyncio

from qq_roleplay_bot.focus import FocusController
from qq_roleplay_bot.stage3_main import DialogueEngine
from qq_roleplay_bot.transport import IncomingMessage, MessageTarget

GROUP = "717151356"
SUPER = "900000001"
SESSION = f"group:{GROUP}"


class _NeverCalled:
    async def complete(self, request):
        raise AssertionError("这些路径不该调用模型")


def message(index: int) -> IncomingMessage:
    return IncomingMessage(
        message_id=f"z{index}", session_id=SESSION, user_id="100",
        text=f"第{index}条", target=MessageTarget(group_id=GROUP), sender_name="某人",
    )


def test_the_queue_drops_the_oldest_message_when_full() -> None:
    focus = FocusController(queue_limit=3)
    for index in range(5):
        focus.enqueue(SESSION, message(index))

    assert focus.queue_size(SESSION) == 3
    assert [item.message_id for item in focus.drain(SESSION)] == ["z2", "z3", "z4"], \
        "丢的不该是刚进来的那条"
    assert focus.dropped == 2


def test_the_queue_depth_never_exceeds_the_limit() -> None:
    focus = FocusController(queue_limit=4)
    for index in range(40):
        depth = focus.enqueue(SESSION, message(index))
        assert depth <= 4, depth
    assert focus.queue_size(SESSION) == 4
    assert focus.dropped == 36


def test_a_queue_below_the_limit_drops_nothing() -> None:
    focus = FocusController(queue_limit=5)
    for index in range(5):
        focus.enqueue(SESSION, message(index))
    assert focus.dropped == 0
    assert focus.queue_size(SESSION) == 5


def test_the_drop_count_shows_up_in_the_status() -> None:
    """丢消息必须看得见：计数进 `focus_stats()`，`/super status` 上印出来。"""

    async def run() -> str:
        engine = DialogueEngine(_NeverCalled(), super_admin_user_ids=frozenset({SUPER}))
        engine.focus.queue_limit = 2
        for index in range(4):
            engine.focus.enqueue(SESSION, message(index))
        assert engine.focus_stats()["queued_dropped"] == 2
        reply = await engine.handle(IncomingMessage(
            "m:status", SESSION, SUPER, "/super status", MessageTarget(group_id=GROUP)))
        return reply.text

    text = asyncio.run(run())
    assert "排队等她：2 条" in text, text
    assert "已丢最旧 2 条" in text, text


if __name__ == "__main__":
    import unittest

    unittest.main()
