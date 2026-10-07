"""停用一个群：把那个群从**焦点排队**里摘掉（隔离增长审计 [7] / [3][4] 的共同缺口）。

修之前 `disable_group` 只做两件事（移出启停名单、`sessions.leave`），**不碰 `focus`
任何一张表**。而被停用的群**永远**不会被 `take_next_session` 轮到——它的新消息在
`handle()` 里就被"群未启用"拦下、不再入队。于是：队列里的消息全文、`_order` 条目、
`_ack_at`、"排队过"那两条记账（`_resumed_ids` / `_promised_ids`）全部僵在那里，
直到进程重启。核查者的话是"比随机淘汰更彻底地让承诺落空"。

这一份钉住：

1. 停用 → 该群的队列、`_order`、`_ack_at`、以及那几条消息的 `_resumed_ids` /
   `_promised_ids` 条目全清；
2. **别的群的排队与记账一根不动**；
3. 会话历史照留（用户 2026-10-08 定死：`/admin disable` 只等于"不回复"）；
4. **`/admin clear`（`leave_session`）保留队列**——这是我这次的选择：那条路上那些
   消息还是"欠着的回复"（群还开着、轮到了就该答），清掉等于静默吞掉用户消息。
   被停用的群不同：它**永远**轮不到，留着才是纯泄漏。

去掉 `disable_group` 里那次 `_forget_scheduled`（突变）→ 第 1 条必红。
"""
from qq_roleplay_bot.stage3_main import DialogueEngine
from qq_roleplay_bot.transport import IncomingMessage, MessageTarget

GROUP = "717151356"
OTHER = "1084401296"
SESSION = f"group:{GROUP}"
OTHER_SESSION = f"group:{OTHER}"


class _NeverCalled:
    async def complete(self, request):
        raise AssertionError("这些路径不该调用模型")


def message(index: int, *, group_id: str = GROUP) -> IncomingMessage:
    return IncomingMessage(
        message_id=f"q{index}", session_id=f"group:{group_id}", user_id="100",
        text="在吗", target=MessageTarget(group_id=group_id), sender_name="某人",
    )


def _engine() -> DialogueEngine:
    return DialogueEngine(_NeverCalled(), enabled_group_ids=frozenset({GROUP, OTHER}))


def _queue_up(engine: DialogueEngine, item: IncomingMessage) -> None:
    """把一条消息摆成"正在排队等当值"的样子（含那两处记账）。"""

    engine.focus.enqueue(item.session_id, item)
    engine.focus.note_ack(item.session_id, 10.0)
    engine._resumed_ids.add(item.message_id)
    engine._promised_ids[item.message_id] = None


# --- 1. 停用就摘干净 ---------------------------------------------------------


def test_disabling_a_group_clears_its_focus_entries() -> None:
    engine = _engine()
    item = message(1)
    _queue_up(engine, item)
    assert engine.focus.queue_size(SESSION) == 1

    assert engine.disable_group(GROUP) is True

    assert engine.focus.queue_size(SESSION) == 0, "停用之后队列还在"
    assert engine.focus.queued_sessions() == [], "_order 里还留着它"
    assert engine.focus.last_ack_at(SESSION) is None, "稍等冷却没清"
    assert item.message_id not in engine._resumed_ids, "排队记账没清"
    assert item.message_id not in engine._promised_ids, "承诺记账没清"


def test_disabling_a_group_clears_a_second_queued_message_too() -> None:
    engine = _engine()
    first, second = message(1), message(2)
    _queue_up(engine, first)
    _queue_up(engine, second)

    engine.disable_group(GROUP)

    assert engine.focus.total_queued() == 0
    for item in (first, second):
        assert item.message_id not in engine._resumed_ids
        assert item.message_id not in engine._promised_ids


# --- 2. 不许波及别的群 -------------------------------------------------------


def test_disabling_one_group_leaves_the_other_queue_alone() -> None:
    engine = _engine()
    mine, theirs = message(1), message(2, group_id=OTHER)
    _queue_up(engine, mine)
    _queue_up(engine, theirs)

    engine.disable_group(GROUP)

    assert engine.focus.queue_size(OTHER_SESSION) == 1, "顺手清了别人的队列"
    assert engine.focus.queued_sessions() == [OTHER_SESSION]
    assert engine.focus.last_ack_at(OTHER_SESSION) == 10.0
    assert theirs.message_id in engine._resumed_ids
    assert theirs.message_id in engine._promised_ids


# --- 3. 历史照留 -------------------------------------------------------------


def test_disabling_a_group_keeps_its_short_term_history() -> None:
    engine = _engine()
    queued = message(1)
    _queue_up(engine, queued)
    state = engine.sessions.state(SESSION)
    state.add(queued, 0.0)

    engine.disable_group(GROUP)

    assert len(engine.sessions.state(SESSION).history) == 1, "停用群把历史清了"


# --- 4. 我的判断：`/admin clear` 不吃掉欠着的回复 ----------------------------


def test_clearing_a_session_keeps_its_queue() -> None:
    """"清短期状态"清的是历史与语境；排队那些人还在等她答。

    所以这条路**只**丢待发分段（见 `tests/test_follow_ups_cleanup.py`），
    队列留给焦点时钟轮到时照常处理。
    """

    engine = _engine()
    item = message(1)
    _queue_up(engine, item)

    engine.leave_session(SESSION)

    assert engine.focus.queue_size(SESSION) == 1
    assert item.message_id in engine._resumed_ids


if __name__ == "__main__":
    import unittest

    unittest.main()
