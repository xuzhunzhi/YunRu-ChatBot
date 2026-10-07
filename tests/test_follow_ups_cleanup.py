"""待续发的分段消息要跟着会话一起被丢掉（隔离增长审计：`_follow_ups` 漏掉的那条）。

`_follow_ups` 与 `states` 同键（按会话），而且只在"有消息被处理"时才被取走
（`take_follow_ups`，`_serve_queue` 与 `runtime._handle_one` 两处）。`leave()` 与
`disable_group()` 都不清它——被停用或被 `/admin clear` 掉的会话，它的正文（长回复
拆出来的那几段）会一直躺在内存里，直到进程重启；核查者把它点成"同键第三个 dict、
值是消息正文"。

这一份钉住：

1. 清理会话（`/admin clear` 那条路）→ 那个会话的待发段没了；
2. 停用群（`/admin disable`）→ 同上；
3. **别的会话的待发段一根不动**（防"顺手清全部"这种更糟的写法）；
4. **停用群不碰会话历史与记忆**——用户 2026-10-08 定死了 `/admin disable` 只等于
   "不回复"，上下文与记忆照记。这一条是防"拿清空会话状态当解法"。

去掉那两处清理（突变）→ 第 1、2 条必红。
"""
from qq_roleplay_bot.stage3_main import DialogueEngine
from qq_roleplay_bot.transport import IncomingMessage, MessageTarget, OutgoingMessage

GROUP = "717151356"
OTHER = "1084401296"
SESSION = f"group:{GROUP}"
OTHER_SESSION = f"group:{OTHER}"
TARGET = MessageTarget(group_id=GROUP)


class _NeverCalled:
    async def complete(self, request):
        raise AssertionError("这些路径不该调用模型")


def message(index: int, session_id: str = SESSION) -> IncomingMessage:
    return IncomingMessage(
        message_id=f"f{index}", session_id=session_id, user_id="100",
        text="在吗", target=TARGET, sender_name="某人",
    )


def segment(session_id: str, text: str = "续发的一段") -> OutgoingMessage:
    return OutgoingMessage(MessageTarget(group_id=session_id.split(":")[-1]), text)


def _engine(*, enabled: frozenset[str] = frozenset({GROUP, OTHER})) -> DialogueEngine:
    return DialogueEngine(_NeverCalled(), enabled_group_ids=enabled)


# --- 1 / 2. 两处清理 ---------------------------------------------------------


def test_clearing_a_session_drops_its_pending_segments() -> None:
    engine = _engine()
    engine._follow_ups[SESSION] = [segment(SESSION), segment(SESSION, "第二段")]
    engine.leave_session(SESSION)
    assert engine.take_follow_ups(SESSION) == []


def test_disabling_a_group_drops_its_pending_segments() -> None:
    engine = _engine()
    engine._follow_ups[SESSION] = [segment(SESSION)]
    assert engine.disable_group(GROUP) is True
    assert engine.take_follow_ups(SESSION) == []


# --- 3. 不许顺手清别的会话 ---------------------------------------------------


def test_other_sessions_keep_their_pending_segments() -> None:
    engine = _engine()
    engine._follow_ups[SESSION] = [segment(SESSION)]
    engine._follow_ups[OTHER_SESSION] = [segment(OTHER_SESSION, "别人那段的正文")]

    engine.leave_session(SESSION)
    assert [item.text for item in engine.take_follow_ups(OTHER_SESSION)] == ["别人那段的正文"]

    engine._follow_ups[SESSION] = [segment(SESSION)]
    engine._follow_ups[OTHER_SESSION] = [segment(OTHER_SESSION, "别人那段的正文")]
    engine.disable_group(GROUP)
    assert [item.text for item in engine.take_follow_ups(OTHER_SESSION)] == ["别人那段的正文"]


# --- 4. 停用群不碰历史与记忆 -------------------------------------------------


def test_disabling_a_group_keeps_its_history() -> None:
    """`/admin disable` 只等于"不回复"：短期历史照留（用户 2026-10-08 定死的）。"""

    engine = _engine()
    state = engine.sessions.state(SESSION)
    state.add(message(1), 0.0)
    assert len(state.history) == 1

    memory_before = set(engine.memory_group_ids)
    assert engine.disable_group(GROUP) is True
    assert len(engine.sessions.state(SESSION).history) == 1, "停用群把历史清掉了"
    assert set(engine.memory_group_ids) == memory_before, "停用群动了记忆名单"


def test_clearing_a_session_keeps_its_history() -> None:
    engine = _engine()
    state = engine.sessions.state(SESSION)
    state.add(message(1), 0.0)
    engine.leave_session(SESSION)
    assert len(engine.sessions.state(SESSION).history) == 1


if __name__ == "__main__":
    import unittest

    unittest.main()
