"""引擎接线测试：对话背景补全如何进入 DialogueEngine。离线，不连 SnowLuma。"""
import asyncio

from qq_roleplay_bot.stage3_main import DialogueEngine
from qq_roleplay_bot.stage3_runtime import ConversationMode, build_dialogue_messages
from qq_roleplay_bot.transport import IncomingMessage, MessageTarget

GROUP = "717151356"


def _message(message_id: str, text: str = "在吗", *, mentioned: bool = True,
             user_id: str = "100") -> IncomingMessage:
    return IncomingMessage(
        message_id=message_id,
        session_id=f"group:{GROUP}",
        user_id=user_id,
        text=text,
        target=MessageTarget(group_id=GROUP),
        is_bot_mentioned=mentioned,
    )


def _history_item(seq: int, text: str, *, user_id: str = "") -> IncomingMessage:
    return IncomingMessage(
        message_id=f"history:{seq}",
        session_id=f"group:{GROUP}",
        user_id=user_id or str(200 + seq),
        text=text,
        target=MessageTarget(group_id=GROUP),
        sender_name=f"群友{seq}",
    )


class _Client:
    async def complete(self, request):
        return "<decision>NO_REPLY</decision>"


class _Provider:
    """记录调用次数，按需返回预设历史或抛错。"""

    def __init__(self, *, seeded=(), error=None, client=object()):
        self.client = client
        self._seeded = tuple(seeded)
        self._error = error
        self.calls = 0

    async def collect_seed_messages(self, session_id, group_id, current):
        self.calls += 1
        if self._error:
            raise self._error
        return self._seeded


def test_history_is_seeded_into_session_on_first_message() -> None:
    async def run() -> None:
        provider = _Provider(seeded=[_history_item(1, "前面聊的内容"), _history_item(2, "又接了一句")])
        engine = DialogueEngine(_Client(), context_provider=provider)
        await engine.handle(_message("m1"))
        state = engine.sessions.state(f"group:{GROUP}")
        texts = [m.text for m in state.recent()]
        assert "前面聊的内容" in texts
        assert "又接了一句" in texts
        assert engine.snapshot().history_seeded == 2

    asyncio.run(run())


def test_seeded_messages_get_a_roster_slot() -> None:
    """补进来的历史必须走 `state.add()`：不然整段都没有 `who`，只能靠猜谁是谁。

    2026-09-29 实测：`_seed_history` 以前直接 `state.history.append()`，绕过 `note_speaker`；
    离线复现时 seed 进去的消息渲染成 `<message index="1">`，既没有 who 也没有 speaker，
    名册里也查不到这些人。
    """

    async def run() -> None:
        provider = _Provider(seeded=[_history_item(1, "前面聊的内容", user_id="200"),
                                     _history_item(2, "又接了一句", user_id="300")])
        engine = DialogueEngine(_Client(), context_provider=provider)
        await engine.handle(_message("m1", "在吗", user_id="100"))
        state = engine.sessions.state(f"group:{GROUP}")
        assert set(state.aliases) == {"100", "200", "300"}, state.aliases
        rendered = build_dialogue_messages(
            state.topic_history(), current=state.recent()[-1],
            mode=ConversationMode.ACTIVE, trigger="mention", context=state.context,
            group_chat=True, alias_of=state.alias_of, aliases=state.aliases,
            roster=tuple(state.roster_lines()),
        )[1]["content"]
        for line in rendered.splitlines():
            if "<message " in line:
                assert 'who="' in line or 'speaker="yunru"' in line, line

    asyncio.run(run())


def test_seeding_happens_only_once_per_session() -> None:
    async def run() -> None:
        provider = _Provider(seeded=[_history_item(1, "背景")])
        engine = DialogueEngine(_Client(), context_provider=provider)
        await engine.handle(_message("m1"))
        await engine.handle(_message("m2", "第二句"))
        # 会话里已有历史，第二次不再补。
        assert provider.calls == 1

    asyncio.run(run())


def test_provider_failure_does_not_break_handling() -> None:
    async def run() -> None:
        provider = _Provider(error=RuntimeError("snowluma 不在"))
        engine = DialogueEngine(_Client(), context_provider=provider)
        reply = await engine.handle(_message("m1"))
        # 补上下文失败不产生回复，也不抛错；会话照常建立。
        assert reply is None
        assert provider.calls == 1
        assert engine.snapshot().history_seeded == 0

    asyncio.run(run())


def test_no_provider_means_no_seeding() -> None:
    async def run() -> None:
        engine = DialogueEngine(_Client())
        await engine.handle(_message("m1"))
        assert engine.snapshot().history_seeded == 0

    asyncio.run(run())


def test_seeded_history_reaches_the_model_request() -> None:
    """补进来的背景必须真的进 prompt，否则等于白拉。"""

    async def run() -> None:
        captured = {}

        class Client:
            async def complete(self, request):
                captured["user"] = request[1]["content"]
                return "<decision>NO_REPLY</decision>"

        provider = _Provider(seeded=[_history_item(1, "云茹之前说过的话")])
        engine = DialogueEngine(Client(), context_provider=provider)
        # 用 @ 强制触发一次模型调用。
        await engine.handle(_message("m1", "@YunRu 在吗"))
        assert "云茹之前说过的话" in captured.get("user", "")

    asyncio.run(run())


def test_private_session_seeding_does_not_crash() -> None:
    async def run() -> None:
        provider = _Provider(seeded=())
        engine = DialogueEngine(_Client(), context_provider=provider,
                              private_debug_user_ids=frozenset({"100"}))
        private = IncomingMessage(
            message_id="p1", session_id="private:100", user_id="100", text="你好",
            target=MessageTarget(user_id="100"),
        )
        await engine.handle(private)
        assert provider.calls >= 1

    asyncio.run(run())
