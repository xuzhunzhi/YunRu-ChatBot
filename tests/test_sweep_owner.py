"""巡检锁是**持有者标记**，不是共享布尔（失败降级审计 3）。

修之前：`tick()`（5 秒一跳）不检查有没有人占着就 `begin_sweep()`，`finally` 里又
无条件 `end_sweep()`——于是接收路径还在模型调用里时，它的守卫被 tick **提前清掉**，
紧随其后的新消息就认为自己可以巡检：两条任务同时进 `handle()`，`focus.hot` 也会被
半路改写（审计第 3 条的机制部分，以及核查者补的那条"两个方向都成立"）。

现在 `begin_sweep(owner)` 返回**有没有拿到**、`end_sweep(owner)` 只清自己那份；
`tick()` 拿不到就**跳过这一跳**。

这一份钉住：

1. 接收路径占着时，tick 跳过这一跳：不服务队列、**不把守卫清掉**；
2. 守卫还回去之后，那一跳照常轮到排队的人（跳过只是跳过，不是丢）；
3. 拿不到的人不会覆盖持有者；旧持有者晚到的 `end_sweep` 也清不掉新持有者。

突变（各自还原）：`tick()` 回到无条件 `begin_sweep()` / `end_sweep()` → 第 1 条红。
"""
import asyncio

from qq_roleplay_bot.stage3_main import DialogueEngine
from qq_roleplay_bot.transport import IncomingMessage, MessageTarget

GROUP = "717151356"
OTHER = "1084401296"
SESSION = f"group:{GROUP}"
OTHER_SESSION = f"group:{OTHER}"


class _ScriptedJudge:
    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, request):
        self.calls += 1
        return "<route>REPLY</route><topic>话题</topic>"


class _ScriptedReply:
    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, request):
        self.calls += 1
        return ("<decision>REPLY</decision><dialogue>KEEP</dialogue>"
                "<reply>我接一句。</reply>")


def _message(index: int, *, group_id: str = OTHER) -> IncomingMessage:
    return IncomingMessage(
        message_id=f"w{index}", session_id=f"group:{group_id}", user_id="100",
        text="@YunRu 在吗", target=MessageTarget(group_id=group_id),
        sender_name="某人", is_bot_mentioned=True,
    )


def _engine() -> tuple[DialogueEngine, _ScriptedJudge, _ScriptedReply]:
    judge, reply = _ScriptedJudge(), _ScriptedReply()
    engine = DialogueEngine(
        reply, judge_client=judge, enabled_group_ids=frozenset({GROUP, OTHER}))
    return engine, judge, reply


# --- 1. 拿不到就跳过这一跳 ---------------------------------------------------


def test_the_tick_skips_the_beat_while_the_receive_path_holds_the_guard() -> None:
    engine, judge, _ = _engine()
    engine.focus.enqueue(OTHER_SESSION, _message(1))
    owner = f"recv:{SESSION}"
    assert engine.focus.begin_sweep(owner) is True

    deliveries = asyncio.run(engine.tick(100.0))

    assert deliveries == []
    assert engine.focus.sweeping is True, "tick 把接收路径正持有的守卫清掉了"
    assert engine.focus.sweep_owner == owner
    assert engine.focus.queue_size(OTHER_SESSION) == 1, "tick 抢着把排队消息处理了"
    assert engine._stats["focus_switches"] == 0
    assert engine.focus.hot is None, "tick 在被占着的时候还是把谁提上当值了"
    assert judge.calls == 0

    # 还回去之后，那一跳照常——跳过只是跳过，不是丢。
    assert engine.focus.end_sweep(owner) is True
    deliveries = asyncio.run(engine.tick(101.0))
    assert deliveries, "守卫空出来之后该轮到排队的人了"
    assert judge.calls == 1


# --- 2. 持有者语义 -----------------------------------------------------------


def test_the_holder_is_not_overwritten_and_only_it_can_release() -> None:
    from qq_roleplay_bot.focus import FocusController

    focus = FocusController()
    assert focus.begin_sweep("recv:group:A") is True
    assert focus.begin_sweep("tick") is False, "拿不到的人仍然把持有者换掉了"
    assert focus.sweep_owner == "recv:group:A"
    assert focus.end_sweep("tick") is False, "不是持有者却把守卫清了"
    assert focus.sweeping is True
    assert focus.end_sweep("recv:group:A") is True
    assert focus.sweeping is False


def test_a_late_release_from_an_old_owner_does_not_clear_a_new_holder() -> None:
    """旧持有者晚到的 `end_sweep` 不许清掉**新**持有者（这正是共享布尔做不到的）。"""

    from qq_roleplay_bot.focus import FocusController

    focus = FocusController()
    focus.begin_sweep("recv:group:A")
    focus.end_sweep("recv:group:A")
    focus.begin_sweep("tick")
    assert focus.end_sweep("recv:group:A") is False
    assert focus.sweeping is True and focus.sweep_owner == "tick"


if __name__ == "__main__":
    import unittest

    unittest.main()
