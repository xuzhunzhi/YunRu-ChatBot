"""短期会话状态**有上限、按最久不活跃淘汰**，且淘汰时把按会话 client 一起丢掉。

来源：隔离增长审计第 1 条与第 5 条（以核查文档为准）。修之前：

- `DialogueSessions.states` 只有 `setdefault` 与"从盘恢复"两处写入，**没有任何删除点**；
  `/admin disable`、`/super clear` 只把 mode 置回去（`leave()`），历史留在内存里，
  真正的回收只发生在进程重启；
- `_session_clients` / `_session_judges` 是只写不删的强引用，各自持一份按会话隔离的
  缓存空间——会话状态就算回收了，这两个字典也把它拽着。

现在：上限 `QQBOT_SESSION_CACHE_MAX_SESSIONS`（默认 200，夹 10..2000，**调用时现读**），
超了就淘汰**最久不活跃**的那个会话（`last_activity_at`），并把它的两个 client 摘掉；
淘汰数进 `EngineSnapshot.sessions_evicted`，`/super status` 的"短期会话"那行会印出来。

**这一份同时是那条禁令的守卫**：`/admin disable` 只等于"不回复"——它不许清会话历史，
更不许被当成淘汰手段（用户 2026-10-08 定死）。淘汰只看"上限 + 最久不活跃"。

突变验证（各自还原）：去掉上限判断 → 第 1、2 条红；淘汰时不删 client → 第 3 条红；
把淘汰改成"停用群就清它的会话状态" → 第 4 条红。
"""
import asyncio
import os
from unittest.mock import patch

from qq_roleplay_bot.stage3_main import DialogueEngine
from qq_roleplay_bot.transport import IncomingMessage, MessageTarget

GROUP = "717151356"
SUPER = "900000001"
LIMIT = 10          # `QQBOT_SESSION_CACHE_MAX_SESSIONS` 的下界就是 10


class _NeverCalled:
    async def complete(self, request):
        raise AssertionError("这些路径不该调用模型")


class _FakeClient:
    def __init__(self, session_id: str = "") -> None:
        self.session_id = session_id


def _sid(index: int) -> str:
    return f"group:{700000000 + index}"


def _engine() -> DialogueEngine:
    return DialogueEngine(
        _NeverCalled(),
        judge_client=_NeverCalled(),
        client_factory=_FakeClient,
        judge_client_factory=_FakeClient,
    )


def _message(index: int) -> IncomingMessage:
    return IncomingMessage(
        message_id=f"s{index}", session_id=_sid(index), user_id="100",
        text="在吗", target=MessageTarget(group_id=_sid(index).split(":")[-1]),
        sender_name="某人",
    )


# --- 1. 上限与 LRU -----------------------------------------------------------


def test_the_session_map_never_exceeds_the_limit() -> None:
    engine = _engine()
    with patch.dict(os.environ, {"QQBOT_SESSION_CACHE_MAX_SESSIONS": str(LIMIT)}):
        for index in range(LIMIT + 5):
            engine.sessions.state(_sid(index)).last_activity_at = 100.0 + index
            assert len(engine.sessions.states) <= LIMIT, len(engine.sessions.states)
        assert len(engine.sessions.states) == LIMIT
        assert engine.sessions.evicted == 5


def test_the_least_recently_active_session_is_evicted() -> None:
    """"最久不活跃"是按 `last_activity_at`，不是按插入顺序。"""

    engine = _engine()
    with patch.dict(os.environ, {"QQBOT_SESSION_CACHE_MAX_SESSIONS": str(LIMIT)}):
        for index in range(LIMIT):
            engine.sessions.state(_sid(index)).last_activity_at = 100.0 + index
        # 0 号（本来最旧）刚活跃过 → 时间戳最新；1 号成了最旧的。
        engine.sessions.state(_sid(0)).last_activity_at = 999.0
        engine.sessions.state(_sid(LIMIT)).last_activity_at = 1000.0

        assert _sid(1) not in engine.sessions.states, "该淘汰的是最久不活跃的 1 号"
        assert _sid(0) in engine.sessions.states, "刚活跃过的被丢了"
        assert _sid(LIMIT) in engine.sessions.states, "刚进来的被丢了"
        assert engine.sessions.evicted == 1


def test_a_brand_new_session_is_not_evicted_by_itself() -> None:
    """新建的状态 `last_activity_at` 还是 None——它不能把自己淘汰掉。"""

    engine = _engine()
    with patch.dict(os.environ, {"QQBOT_SESSION_CACHE_MAX_SESSIONS": str(LIMIT)}):
        for index in range(LIMIT):
            engine.sessions.state(_sid(index))          # 全是 None（从没活跃过）
        state = engine.sessions.state(_sid(LIMIT))
        assert engine.sessions.states[_sid(LIMIT)] is state, "调用方拿到一个已经不在表里的 state"
        assert len(engine.sessions.states) == LIMIT


# --- 2. 淘汰要跟 client 联动 -------------------------------------------------


def test_the_evicted_session_loses_its_clients() -> None:
    engine = _engine()
    with patch.dict(os.environ, {"QQBOT_SESSION_CACHE_MAX_SESSIONS": str(LIMIT)}):
        for index in range(LIMIT):
            engine.sessions.state(_sid(index))       # 状态先建出来（全 None → 0 号最旧）
            engine._client_for(_sid(index))
            engine._judge_for(_sid(index))
        assert len(engine._session_clients) == LIMIT
        assert len(engine._session_judges) == LIMIT
        assert len(engine.sessions.states) == LIMIT

        engine.sessions.state(_sid(LIMIT))            # 触发淘汰（全 None → 淘汰 0 号）

        assert _sid(0) not in engine._session_clients, "淘汰了会话却留着它的 client"
        assert _sid(0) not in engine._session_judges
        assert len(engine._session_clients) == LIMIT - 1
        assert _sid(1) in engine._session_clients, "别的会话的 client 不该被牵连"


# --- 3. 那条禁令：disable / clear 都不许当淘汰手段 ---------------------------


def test_disabling_a_group_does_not_evict_its_session() -> None:
    """`/admin disable` 只等于"不回复"：历史照记，状态不许被清（用户 2026-10-08 定死）。"""

    engine = DialogueEngine(
        _NeverCalled(), enabled_group_ids=frozenset({GROUP}),
        judge_client=_NeverCalled(), client_factory=_FakeClient,
        judge_client_factory=_FakeClient)
    session_id = f"group:{GROUP}"
    state = engine.sessions.state(session_id)
    state.add(_message(0), 100.0)
    engine._client_for(session_id)

    assert engine.disable_group(GROUP) is True

    assert session_id in engine.sessions.states, "停用群把会话状态清掉了"
    assert len(engine.sessions.states[session_id].history) == 1, "停用群把历史清掉了"
    assert session_id in engine._session_clients, "停用群把 client 也清了（那是淘汰该做的事）"
    assert engine.sessions.evicted == 0, "停用群被算成了一次淘汰"


def test_clearing_a_session_does_not_evict_it() -> None:
    """`/super clear`（`leave_session`）同理：只重置 mode 与触发器。"""

    engine = _engine()
    session_id = _sid(0)
    state = engine.sessions.state(session_id)
    state.add(_message(0), 100.0)

    assert engine.leave_session(session_id) is True

    assert session_id in engine.sessions.states
    assert len(engine.sessions.states[session_id].history) == 1
    assert engine.sessions.evicted == 0


def test_the_status_line_shows_the_eviction_count() -> None:
    engine = _engine()
    with patch.dict(os.environ, {"QQBOT_SESSION_CACHE_MAX_SESSIONS": str(LIMIT)}):
        for index in range(LIMIT + 1):
            engine.sessions.state(_sid(index)).last_activity_at = 100.0 + index
        assert engine.snapshot().sessions_evicted == 1


def test_the_status_text_prints_the_eviction_count() -> None:
    """淘汰必须**看得见**：`/super status` 的"短期会话"那行要印出来。"""

    async def run() -> str:
        engine = DialogueEngine(_NeverCalled(), super_admin_user_ids=frozenset({SUPER}))
        with patch.dict(os.environ, {"QQBOT_SESSION_CACHE_MAX_SESSIONS": str(LIMIT)}):
            for index in range(LIMIT + 1):
                engine.sessions.state(_sid(index)).last_activity_at = 100.0 + index
        reply = await engine.handle(IncomingMessage(
            "m:status", f"group:{GROUP}", SUPER, "/super status",
            MessageTarget(group_id=GROUP)))
        return reply.text

    text = asyncio.run(run())
    assert "短期会话：" in text, text
    assert "已淘汰：1" in text, text


# --- 4. 配置 -----------------------------------------------------------------


def test_the_limit_is_read_when_called_and_clamped() -> None:
    from qq_roleplay_bot import dev_config

    with patch.dict(os.environ):
        os.environ.pop("QQBOT_SESSION_CACHE_MAX_SESSIONS", None)
        assert dev_config.session_cache_max_sessions() == 200
    with patch.dict(os.environ, {"QQBOT_SESSION_CACHE_MAX_SESSIONS": "50"}):
        assert dev_config.session_cache_max_sessions() == 50
    for text, expected in (("", 200), ("不是数", 200), ("1", 10), ("99999", 2000)):
        with patch.dict(os.environ, {"QQBOT_SESSION_CACHE_MAX_SESSIONS": text}):
            assert dev_config.session_cache_max_sessions() == expected, text


if __name__ == "__main__":
    import unittest

    unittest.main()
