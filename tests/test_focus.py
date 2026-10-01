"""焦点控制器的离线覆盖：三条释放路径、排队先到先得、收到语冷却、巡检串行。

规则依据是 `docs/MULTI_GROUP_FOCUS.md` 第二～四节。这里不碰模型、不碰 transport，
所以每条规则都能单独钉死。
"""
from qq_roleplay_bot.focus import FocusController
from qq_roleplay_bot.transport import IncomingMessage, MessageTarget

TARGET = MessageTarget(group_id="717151356")


def msg(index: int, text: str = "测试") -> IncomingMessage:
    return IncomingMessage(
        message_id=f"m{index}", session_id="group:717151356", user_id="100",
        text=text, target=TARGET, sender_name="某人",
    )


# --- 三条释放路径 -----------------------------------------------------------


def test_quiet_release_after_topic_stops() -> None:
    """话题没继续超过 45 秒 → 静默释放。"""

    focus = FocusController()
    focus.acquire("group:a", now=1000.0)
    assert focus.release_reason(1044.0) is None, "44 秒还不该走"
    assert focus.release_reason(1046.0) == "quiet"


def test_related_messages_keep_the_focus() -> None:
    """有人接着这条线说话（她没被叫到也算）→ 计时重置，焦点留着。"""

    focus = FocusController()
    focus.acquire("group:a", now=1000.0)
    for step in range(1, 6):
        focus.note_related(1000.0 + step * 40)
    assert focus.release_reason(1210.0) is None, "一直在同一条线上，不该走"
    assert focus.release_reason(1245.1) == "quiet"


def test_reply_limit_releases_with_a_farewell_reason() -> None:
    """连回 50 条 → 走，而且理由是 `replies`（要打一声招呼）。"""

    focus = FocusController()
    focus.acquire("group:a", now=1000.0)
    for _ in range(50):
        focus.note_reply()
        focus.note_related(1010.0)
    assert focus.release_reason(1011.0) == "replies"


def test_duty_limit_releases_silently() -> None:
    """当值满 5 分钟 → 走，理由是 `duty`（静默，不打招呼）。"""

    focus = FocusController()
    focus.acquire("group:a", now=1000.0)
    for step in range(1, 8):          # 一直有人在跟她聊
        focus.note_related(1000.0 + step * 40)
    assert focus.release_reason(1300.1) == "duty"
    assert focus.release_reason(1400.0) != "replies", "不是连回满 50 条的情形"


def test_quiet_release_does_not_fire_while_nobody_is_on_duty() -> None:
    assert FocusController().release_reason(9999.0) is None


# --- 队列 -------------------------------------------------------------------


def test_queue_keeps_arrival_order_and_drains_per_session() -> None:
    focus = FocusController()
    focus.enqueue("group:b", msg(1))
    focus.enqueue("group:a", msg(2))
    focus.enqueue("group:b", msg(3))
    assert focus.queued_sessions() == ["group:b", "group:a"], "先到先得"
    assert focus.queue_size("group:b") == 2
    drained = focus.drain("group:b")
    assert [item.message_id for item in drained] == ["m1", "m3"], "同一会话内部按到达顺序"
    assert focus.queued_sessions() == ["group:a"]
    assert focus.has_backlog() is True
    focus.drain("group:a")
    assert focus.has_backlog() is False


def test_take_next_session_acquires_the_earliest_waiter() -> None:
    focus = FocusController()
    focus.enqueue("group:c", msg(1))
    focus.enqueue("group:b", msg(2))
    assert focus.take_next_session(now=500.0) == "group:c"
    assert focus.is_hot("group:c")
    assert focus.hot is not None and focus.hot.replies == 0


def test_take_next_session_returns_none_without_backlog() -> None:
    focus = FocusController()
    assert focus.take_next_session(now=1.0) is None
    assert focus.hot is None


def test_drain_clears_the_waiting_order() -> None:
    """排空之后这个会话不该再占着"先到先得"的位置。"""

    focus = FocusController()
    focus.enqueue("group:b", msg(1))
    focus.drain("group:b")
    focus.enqueue("group:a", msg(2))
    focus.enqueue("group:b", msg(3))
    assert focus.queued_sessions() == ["group:a", "group:b"]


# --- 收到语冷却 -------------------------------------------------------------


def test_ack_cooldown_allows_one_per_window() -> None:
    focus = FocusController()
    assert focus.ack_allowed("group:b", 100.0) is True
    focus.note_ack("group:b", 100.0)
    assert focus.ack_allowed("group:b", 219.0) is False
    assert focus.ack_allowed("group:b", 220.0) is True
    assert focus.ack_allowed("group:c", 101.0) is True, "冷却按会话分开"


# --- 巡检串行 ---------------------------------------------------------------


def test_sweep_is_serialized() -> None:
    focus = FocusController()
    assert focus.sweeping is False
    focus.begin_sweep()
    assert focus.sweeping is True
    focus.end_sweep()
    assert focus.sweeping is False


# --- 观测 -------------------------------------------------------------------


def test_hot_stats_report_duty_time() -> None:
    focus = FocusController()
    focus.acquire("group:a", now=100.0)
    focus.note_reply()
    focus.note_related(120.0)
    stats = focus.hot_stats(now=180.0)
    assert stats == {
        "session_id": "group:a",
        "seconds_on_duty": 80.0,
        "since_related": 60.0,
        "replies": 1,
    }
    focus.release()
    assert focus.hot_stats(now=200.0) is None
