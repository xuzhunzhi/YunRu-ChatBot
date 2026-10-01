"""Stage 3 上下文补全的离线测试：不依赖 SnowLuma 在线。"""
import asyncio

from qq_roleplay_bot.capabilities import CapabilityDenied, CapabilityRegistry
from qq_roleplay_bot.conversation_context import (
    HISTORY_PREFIX,
    ConversationContextProvider,
    segments_to_text,
)
from qq_roleplay_bot.transport import IncomingMessage, MessageTarget, display_name_from_sender

GROUP = "717151356"


def test_display_name_prefers_qq_nickname_and_falls_back() -> None:
    """取名口径（2026-10-01 用户）：QQ 昵称优先，缺失才退回群名片。

    真机实测（群 800000001）：59 人里 42 人名片与昵称不同，并且有人隔几条消息
    就把名片从"桓珩"改成"洹桁"——名片按群一份、还会变，不能当身份用。
    """

    # 两者都有 → 用昵称。
    assert display_name_from_sender({"nickname": "宋秋湙", "card": "在不在不在"}) == "宋秋湙"
    # 没有昵称 → 退回名片（别把说话人显示成空）。
    assert display_name_from_sender({"nickname": "", "card": "只有名片"}) == "只有名片"
    assert display_name_from_sender({"card": "只有名片"}) == "只有名片"
    # 空白要当成没有，不能显示成一串空格。
    assert display_name_from_sender({"nickname": "   ", "card": " 名片 "}) == "名片"
    # 都没有 → 调用方给的兜底名。
    assert display_name_from_sender({"nickname": " "}, fallback="某人") == "某人"
    assert display_name_from_sender(None, fallback="某人") == "某人"
    assert display_name_from_sender("不是字典") == ""


def _message(message_id: str = "cur", text: str = "在吗") -> IncomingMessage:
    return IncomingMessage(
        message_id=message_id,
        session_id=f"group:{GROUP}",
        user_id="100",
        text=text,
        target=MessageTarget(group_id=GROUP),
        is_bot_mentioned=True,
    )


def _history_item(seq: int, user_id: str, text: str, *, nickname: str = "") -> dict:
    return {
        "message_seq": seq,
        "message_id": seq * 10,
        "user_id": user_id,
        "sender": {"nickname": nickname or f"用户{user_id}", "role": "member"},
        "message": [{"type": "text", "data": {"text": text}}],
    }


class _FakeClient:
    def __init__(self, *, members=None, history=None, members_error=None, history_error=None):
        self._members = members if members is not None else [
            {"user_id": 100, "nickname": "小明", "card": "群名片小明"},
            {"user_id": 200, "nickname": "小红", "card": ""},
            {"user_id": 900000002, "nickname": "YunRu", "card": ""},
        ]
        self._history = history if history is not None else [
            _history_item(1, "100", "前面说的话"),
            _history_item(2, "200", "别人接了一句"),
        ]
        self._members_error = members_error
        self._history_error = history_error
        self.member_calls = 0
        self.history_calls = 0

    async def group_members(self, group_id):
        self.member_calls += 1
        if self._members_error:
            raise self._members_error
        return tuple(self._members)

    async def group_message_history(self, group_id, *, count=20, message_seq=None):
        self.history_calls += 1
        if self._history_error:
            raise self._history_error
        return tuple(self._history)


# --- 分段转文本 -----------------------------------------------------------

def test_segments_to_text_handles_text_at_and_media() -> None:
    assert segments_to_text([{"type": "text", "data": {"text": "你好"}}]) == "你好"
    assert segments_to_text([
        {"type": "at", "data": {"qq": "999"}},
        {"type": "text", "data": {"text": " 在吗"}},
    ]) == "@999 在吗"
    assert segments_to_text([{"type": "image", "data": {}}]) == "[图片]"
    assert segments_to_text([{"type": "reply", "data": {"id": "1"}},
                             {"type": "text", "data": {"text": "嗯"}}]) == "[回复]嗯"
    assert segments_to_text("纯字符串") == "纯字符串"
    assert segments_to_text(None) == ""
    assert segments_to_text([{"type": "unknown_xyz"}]) == ""


# --- 名字解析 -------------------------------------------------------------

def test_member_nickname_preferred_over_card() -> None:
    """显示名用 QQ 昵称，不用群名片（2026-10-01 用户定的口径）。

    群名片每个群一份、还会随时改，拿它当"这个人叫什么"会让跨群显示漂移；
    QQ 昵称是账号级的。群名片没被丢掉，"改群名片"命令照旧用它。
    """

    async def run() -> None:
        provider = ConversationContextProvider(_FakeClient(), refresh_interval=0)
        await provider.collect_seed_messages(f"group:{GROUP}", GROUP, _message())
        # 100 有群名片"群名片小明"，但显示名该是他的 QQ 昵称。
        assert provider.display_name(f"group:{GROUP}", "100") == "小明"
        assert provider.display_name(f"group:{GROUP}", "200") == "小红"
        assert provider.display_name(f"group:{GROUP}", "yunru") == "YunRu"
        # 未知用户回退到调用方给的名字。
        assert provider.display_name(f"group:{GROUP}", "999", "备用名") == "备用名"
        assert provider.display_name(f"group:{GROUP}", "999") == ""

    asyncio.run(run())


def test_member_without_nickname_falls_back_to_card() -> None:
    """昵称缺失时才退回群名片：宁可显示个会变的名片，也别把说话人显示成空。"""

    async def run() -> None:
        client = _FakeClient(members=[{"user_id": 300, "nickname": "", "card": "只有名片"}])
        provider = ConversationContextProvider(client, refresh_interval=0)
        await provider.collect_seed_messages(f"group:{GROUP}", GROUP, _message())
        assert provider.display_name(f"group:{GROUP}", "300") == "只有名片"

    asyncio.run(run())


def test_unknown_member_falls_back_to_message_nickname() -> None:
    async def run() -> None:
        client = _FakeClient(members=[])  # 拿不到成员名单
        provider = ConversationContextProvider(client, refresh_interval=0)
        seeded = await provider.collect_seed_messages(f"group:{GROUP}", GROUP, _message())
        # 名字回退到历史里 sender 的显示名（QQ 昵称优先）。
        assert any(m.sender_name for m in seeded)

    asyncio.run(run())


# --- 补全行为 -------------------------------------------------------------

def test_seed_messages_exclude_current_and_are_prefixed() -> None:
    async def run() -> None:
        client = _FakeClient(history=[
            _history_item(1, "100", "第一句"),
            _history_item(2, "200", "第二句"),
        ])
        provider = ConversationContextProvider(client, refresh_interval=0)
        seeded = await provider.collect_seed_messages(f"group:{GROUP}", GROUP, _message("cur"))
        assert [m.text for m in seeded] == ["第一句", "第二句"]
        assert all(m.message_id.startswith(HISTORY_PREFIX) for m in seeded)
        # 历史消息不应被标成"提到了机器人"。
        assert all(m.is_bot_mentioned is False for m in seeded)

    asyncio.run(run())


def test_current_message_is_not_duplicated_from_history() -> None:
    async def run() -> None:
        client = _FakeClient(history=[_history_item(1, "100", "就是这句")])
        provider = ConversationContextProvider(client, refresh_interval=0)
        # 让 history 的 message_id 与当前消息一致。
        current = _message("history:1")
        seeded = await provider.collect_seed_messages(f"group:{GROUP}", GROUP, current)
        assert seeded == ()

    asyncio.run(run())


def test_history_limit_is_respected() -> None:
    async def run() -> None:
        history = [_history_item(i, "100", f"第{i}句") for i in range(1, 21)]
        provider = ConversationContextProvider(_FakeClient(history=history), history_count=5,
                                               refresh_interval=0)
        seeded = await provider.collect_seed_messages(f"group:{GROUP}", GROUP, _message())
        assert len(seeded) == 5
        assert seeded[-1].text == "第20句"

    asyncio.run(run())


def test_media_only_history_entries_are_skipped() -> None:
    async def run() -> None:
        client = _FakeClient(history=[
            {"message_seq": 1, "user_id": "200", "sender": {"nickname": "小红"},
             "message": [{"type": "image", "data": {}}]},
            _history_item(2, "100", "有文字"),
        ])
        provider = ConversationContextProvider(client, refresh_interval=0)
        seeded = await provider.collect_seed_messages(f"group:{GROUP}", GROUP, _message())
        assert [m.text for m in seeded] == ["有文字"]

    asyncio.run(run())


def test_bot_history_is_labelled_as_yunru() -> None:
    async def run() -> None:
        provider = ConversationContextProvider(_FakeClient(), refresh_interval=0)
        provider._self_id = "900000002"
        client = _FakeClient(history=[_history_item(1, "900000002", "我之前说过的话")])
        provider.client = client
        seeded = await provider.collect_seed_messages(f"group:{GROUP}", GROUP, _message())
        assert len(seeded) == 1
        assert seeded[0].is_bot_message is True
        assert seeded[0].user_id == "yunru"
        assert seeded[0].sender_name == "YunRu"

    asyncio.run(run())


# --- 限流与降级 -----------------------------------------------------------

def test_refresh_is_throttled_per_group() -> None:
    async def run() -> None:
        client = _FakeClient()
        provider = ConversationContextProvider(client, refresh_interval=100.0)
        await provider.collect_seed_messages(f"group:{GROUP}", GROUP, _message())
        first = client.history_calls
        await provider.collect_seed_messages(f"group:{GROUP}", GROUP, _message("cur2"))
        # 距离上次刷新不足间隔，不再打接口。
        assert client.history_calls == first

    asyncio.run(run())


def test_client_failure_degrades_to_empty() -> None:
    async def run() -> None:
        provider = ConversationContextProvider(
            _FakeClient(history_error=RuntimeError("boom")), refresh_interval=0
        )
        seeded = await provider.collect_seed_messages(f"group:{GROUP}", GROUP, _message())
        assert seeded == ()
        assert provider.stats["refreshes"] == 1

    asyncio.run(run())


def test_members_failure_still_uses_history() -> None:
    async def run() -> None:
        provider = ConversationContextProvider(
            _FakeClient(members_error=RuntimeError("boom")), refresh_interval=0
        )
        seeded = await provider.collect_seed_messages(f"group:{GROUP}", GROUP, _message())
        assert len(seeded) == 2
        assert provider.cached_member_count(f"group:{GROUP}") == 0

    asyncio.run(run())


def test_no_client_means_no_op() -> None:
    async def run() -> None:
        provider = ConversationContextProvider(None)
        assert await provider.collect_seed_messages(f"group:{GROUP}", GROUP, _message()) == ()
        assert await provider.fetch_history(GROUP) == ()

    asyncio.run(run())


def test_private_session_has_no_group_history() -> None:
    async def run() -> None:
        client = _FakeClient()
        provider = ConversationContextProvider(client, refresh_interval=0)
        assert await provider.collect_seed_messages("private:100", None, _message()) == ()
        assert client.history_calls == 0

    asyncio.run(run())


def test_capability_gate_blocks_history_action() -> None:
    """若 read 白名单被收紧，补上下文必须安全降级而不是抛到主循环。"""

    async def run() -> None:
        registry = CapabilityRegistry()
        client = _FakeClient()
        provider = ConversationContextProvider(client, registry=registry, refresh_interval=0)
        # 直接把 client 换成会抛 CapabilityDenied 的假实现。
        async def denied(*args, **kwargs):
            raise CapabilityDenied("denied")

        client.group_message_history = denied  # type: ignore[assignment]
        seeded = await provider.collect_seed_messages(f"group:{GROUP}", GROUP, _message())
        assert seeded == ()

    asyncio.run(run())


def test_fetch_history_uses_capability_check() -> None:
    async def run() -> None:
        provider = ConversationContextProvider(_FakeClient(), refresh_interval=0)
        history = await provider.fetch_history(GROUP, count=3)
        assert len(history) == 2

    asyncio.run(run())
