import asyncio
import json
import logging
from contextlib import contextmanager

from qq_roleplay_bot.chat_log import ChatLog
from qq_roleplay_bot.onebot_ws import (
    MENTION_ONLY_TEXT,
    OneBotWebSocketTransport,
    bot_is_mentioned,
    extract_text,
    parse_message_event,
)


def test_extract_text_from_segments() -> None:
    assert extract_text([
        {"type": "at", "data": {"qq": "123"}},
        {"type": "text", "data": {"text": "你好"}},
    ]) == "你好"


def test_parse_group_message() -> None:
    message = parse_message_event({
        "post_type": "message",
        "message_type": "group",
        "message_id": 42,
        "user_id": 100,
        "group_id": 200,
        "message": "你好",
        "sender": {"nickname": "小明", "card": "群昵称", "role": "member"},
    })
    assert message is not None
    assert message.session_id == "group:200"
    assert message.target.group_id == "200"
    assert message.text == "你好"
    # 显示名用 QQ 昵称，不用群名片（群名片每个群一份、还会随时改）。
    assert message.sender_name == "小明"
    assert message.is_bot_message is False


def test_ignore_non_message_event() -> None:
    assert parse_message_event({"post_type": "meta_event"}) is None


def test_call_api_times_out_when_onebot_never_connects() -> None:
    async def run() -> None:
        transport = OneBotWebSocketTransport(connection_timeout=0.01)
        try:
            await transport.call_api("get_status")
        except RuntimeError as exc:
            assert "连接等待超时" in str(exc)
        else:
            raise AssertionError("未连接时应在有限时间内失败")

    asyncio.run(run())


def test_mention_only_message_is_kept_instead_of_dropped() -> None:
    """只发 @ 不说话也是明确呼叫，不能在解析层丢掉。"""

    message = parse_message_event({
        "post_type": "message",
        "message_type": "group",
        "message_id": 7,
        "user_id": 100,
        "group_id": 200,
        "self_id": 999,
        "message": [{"type": "at", "data": {"qq": "999"}}],
    })
    assert message is not None
    assert message.is_bot_mentioned is True
    assert message.text == MENTION_ONLY_TEXT


def test_empty_message_without_mention_is_ignored() -> None:
    assert parse_message_event({
        "post_type": "message",
        "message_type": "group",
        "message_id": 8,
        "user_id": 100,
        "group_id": 200,
        "self_id": 999,
        "message": "   ",
    }) is None


def test_mention_matching_is_exact_in_both_message_shapes() -> None:
    # 段形态本来就精确。
    assert bot_is_mentioned([{"type": "at", "data": {"qq": "999"}}], "999") is True
    assert bot_is_mentioned([{"type": "at", "data": {"qq": "9999"}}], "999") is False
    # 串形态曾用子串包含，导致 qq=999 命中 qq=9999。
    assert bot_is_mentioned("[CQ:at,qq=999] 你好", "999") is True
    assert bot_is_mentioned("[CQ:at,qq=9999] 你好", "999") is False
    assert bot_is_mentioned("[CQ:at,qq=99900] 你好", "999") is False
    assert bot_is_mentioned("没有艾特", "999") is False


def test_close_wakes_up_receive() -> None:
    """close() 必须让 receive() 返回 None，否则断连后主循环永久挂死。"""

    async def run() -> None:
        transport = OneBotWebSocketTransport()
        pending = asyncio.create_task(transport.receive())
        await asyncio.sleep(0)
        await transport.close()
        assert await asyncio.wait_for(pending, timeout=1.0) is None

    asyncio.run(run())


def test_media_only_message_is_kept_with_placeholder_markers() -> None:
    """只发一张图也要进入上下文，并且不下载任何资源。"""

    message = parse_message_event({
        "post_type": "message",
        "message_type": "group",
        "message_id": 11,
        "user_id": 100,
        "group_id": 200,
        "self_id": 999,
        "message": [{"type": "image", "data": {"file": "x.jpg", "url": "http://example.invalid/x.jpg"}}],
    })
    assert message is not None
    assert message.has_media is True
    assert "[图片]" in message.text


def test_text_and_media_are_combined_without_losing_text() -> None:
    message = parse_message_event({
        "post_type": "message",
        "message_type": "group",
        "message_id": 12,
        "user_id": 100,
        "group_id": 200,
        "self_id": 999,
        "message": [
            {"type": "text", "data": {"text": "你看这个"}},
            {"type": "image", "data": {"file": "x.jpg"}},
            {"type": "face", "data": {"id": "1"}},
        ],
    })
    assert message is not None
    assert message.text.startswith("你看这个")
    assert "[图片]" in message.text and "[表情]" in message.text
    assert message.has_media is True


def test_media_markers_are_bounded() -> None:
    segments = [{"type": "image", "data": {}} for _ in range(50)]
    message = parse_message_event({
        "post_type": "message", "message_type": "group", "message_id": 13,
        "user_id": 100, "group_id": 200, "self_id": 999, "message": segments,
    })
    assert message is not None
    assert message.text.count("[图片]") == 8


def test_reply_segment_is_recorded_from_both_shapes() -> None:
    from qq_roleplay_bot.onebot_ws import extract_reply_target

    assert extract_reply_target([{"type": "reply", "data": {"id": "42"}}]) == "42"
    assert extract_reply_target([{"type": "reply", "data": {"id": 42}}]) == "42"
    assert extract_reply_target("[CQ:reply,id=77] 你好") == "77"
    assert extract_reply_target([{"type": "text", "data": {"text": "hi"}}]) == ""

    message = parse_message_event({
        "post_type": "message", "message_type": "group", "message_id": 14,
        "user_id": 100, "group_id": 200, "self_id": 999,
        "message": [{"type": "reply", "data": {"id": "42"}}, {"type": "text", "data": {"text": "接着上面说"}}],
    })
    assert message is not None
    assert message.reply_to_message_id == "42"


def test_image_urls_are_kept_for_vision() -> None:
    """图片地址只在"取得到"时留下来（给识图用）：http(s) 或 data URL。

    NapCat 的 `file` 常常只是 `a.jpg` 这种文件名，交给模型只会白跑一次调用，
    所以那种一律不收。别的媒体（表情/语音）也不进这里。
    """

    from qq_roleplay_bot.onebot_ws import extract_image_urls

    assert extract_image_urls([
        {"type": "image", "data": {"url": "https://cdn.example.com/a.png", "file": "a.jpg"}},
        {"type": "image", "data": {"file": "b.jpg"}},
        {"type": "image", "data": {"url": "data:image/png;base64,AAAA"}},
        {"type": "face", "data": {"url": "https://cdn.example.com/face.png"}},
    ]) == ("https://cdn.example.com/a.png", "data:image/png;base64,AAAA")

    message = parse_message_event({
        "post_type": "message", "message_type": "group", "message_id": 15,
        "user_id": 100, "group_id": 200, "self_id": 999,
        "message": [{"type": "text", "data": {"text": "看这个"}},
                    {"type": "image", "data": {"url": "https://cdn.example.com/c.png"}}],
    })
    assert message is not None
    assert message.media_urls == ("https://cdn.example.com/c.png",)


def test_send_uses_plain_text_without_quote_and_segments_with_quote() -> None:
    from qq_roleplay_bot.onebot_ws import OneBotWebSocketTransport
    from qq_roleplay_bot.transport import MessageTarget

    async def run() -> None:
        transport = OneBotWebSocketTransport()
        sent: list[dict] = []

        async def fake_call_api(action, params=None):
            sent.append({"action": action, "params": params})
            return {"status": "ok", "retcode": 0}

        transport.call_api = fake_call_api  # type: ignore[assignment]
        target = MessageTarget(group_id="717151356")
        await transport.send(target, "普通回复")
        await transport.send(target, "引用回复", reply_to="42")

        assert sent[0]["params"]["message"] == "普通回复"
        assert sent[1]["params"]["message"] == [
            {"type": "reply", "data": {"id": "42"}},
            {"type": "text", "data": {"text": "引用回复"}},
        ]

    asyncio.run(run())


# --- 私聊没发出去时，给操作者一句人话（2026-10-08）-----------------------------
#
# 由来：给**非好友**发私聊会被对面拒收，回执是一句劝告式的话
# （实测原话"发送失败，请先添加对方为好友"；群临时会话那条是 `no such temp session`，
# 见 `docs/research/snowluma-api-2026-10-08.md`）。改动前操作者在日志里只看得到
# "Stage 3 reply send failed" 加一串回执原文，看不出下一步该做什么。
#
# 这里钉两件事：**那句话在**（去掉说明 → 红），以及**发送行为一个字节都没变**
# （还是那一类异常、还是一帧请求、不重试）。

#: 2026-10-08 实测的那条拒收回执（服务端原话，本机抓到的 `retcode=16`）。
_FRIEND_REFUSAL: dict[str, object] = {
    "status": "failed",
    "retcode": 16,
    "data": None,
    "message": "发送失败，请先添加对方为好友",
    "wording": "发送失败，请先添加对方为好友",
}

#: 群临时会话那道本地门拒收时的原话形状（同一份调研文档）。
_TEMP_SESSION_REFUSAL: dict[str, object] = {
    "status": "failed",
    "retcode": 1,
    "data": None,
    "message": "cannot send to user 2468 in group 717151356: no such temp session",
    "wording": "no such temp session",
}

#: 这两句说明里**不许出现**的词：机制口径（`prompt_guard` 那份词表）+ 操作者视角那条要求。
_FORBIDDEN_IN_NOTE = (
    "协议", "接口", "API", "OneBot", "校验", "令牌", "系统",
)


class _RefusingConnection:
    """假对面：收到一帧就回一条**拒收回执**（echo 配对照旧，走的是真 `_handle_payload`）。"""

    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload
        self.requests: list[dict] = []
        self.on_frame = None

    async def send(self, raw: str) -> None:
        request = json.loads(raw)
        self.requests.append(request)
        assert self.on_frame is not None
        await self.on_frame(json.dumps({**self.payload, "echo": request.get("echo")}))


@contextmanager
def _ws_logs():
    """抓 `qq_roleplay_bot` 这条包的日志行（与 `test_disconnect_notice` 同法）。

    抓**包**那一层、不是 `...onebot_ws` 那一层：这句说明跟客户端那条通道**共用一份**，
    落在 `qq_roleplay_bot.transport` 上（见 `transport.note_private_send_refusal`）。
    """

    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append
    logger = logging.getLogger("qq_roleplay_bot")
    previous_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.WARNING)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)


def _refused_send(payload: dict[str, object], target):
    """走真路发一条（`send` → `call_api` → 拒收分类）。

    返回三元组：（抛出来的异常或 None，实际发出去的帧，这次抓到的日志行）。
    """

    from qq_roleplay_bot.onebot_ws import OneBotWebSocketTransport

    async def run():
        transport = OneBotWebSocketTransport(chat_log=ChatLog(enabled=False))
        connection = _RefusingConnection(payload)
        connection.on_frame = transport._handle_payload
        transport._connection = connection
        transport._connection_ready.set()
        try:
            await transport.send(target, "在吗")
        except Exception as exc:  # noqa: BLE001 - 用例要看的就是这个异常
            return exc, connection.requests
        return None, connection.requests

    with _ws_logs() as records:
        failure, requests = asyncio.run(run())
    return failure, requests, [record.getMessage() for record in records]


def test_a_private_reply_refused_by_the_friend_gate_explains_itself() -> None:
    """非好友私聊被拒 → 日志里出现一句人话（WARNING），**发送行为不变**。"""

    from qq_roleplay_bot.transport import DeliveryRejected, MessageTarget

    failure, requests, lines = _refused_send(_FRIEND_REFUSAL, MessageTarget(user_id="2468"))

    # 1. 说明在（把 `note_private_send_refusal(...)` 那一行去掉 → 这里立刻红）。
    assert any("这条私聊没发出去" in line for line in lines), lines
    assert any("对方还不是好友" in line for line in lines), lines
    assert any("云茹" in line and "加好友" in line for line in lines), lines
    assert any("2468" in line for line in lines), f"要能看出是哪条私聊：{lines}"

    # 2. 只是说明：异常分类与回执原文照旧。
    assert isinstance(failure, DeliveryRejected), failure
    assert "请先添加对方为好友" in str(failure)

    # 3. 发送行为不变：还是那一帧、原参数、**没有第二次尝试**（不重发）。
    assert len(requests) == 1, requests
    assert requests[0]["action"] == "send_msg"
    assert requests[0]["params"]["message_type"] == "private"
    assert requests[0]["params"]["user_id"] == 2468
    assert requests[0]["params"]["message"] == "在吗"


def test_the_same_refusal_in_a_group_gets_no_extra_line() -> None:
    """群消息撞不出这两类原因：**不许多打**一句（多打只是噪音）。"""

    from qq_roleplay_bot.transport import DeliveryRejected, MessageTarget

    failure, requests, lines = _refused_send(_FRIEND_REFUSAL, MessageTarget(group_id="717151356"))

    assert isinstance(failure, DeliveryRejected)
    assert len(requests) == 1
    assert not [line for line in lines if "这条私聊没发出去" in line], lines


def test_the_missing_temp_session_refusal_also_gets_a_human_note() -> None:
    """没有临时会话时给的是**另一句**人话（不是上面那句复制过来的）。"""

    from qq_roleplay_bot.transport import private_send_refusal_note

    note = private_send_refusal_note(
        f"OneBot send_msg 失败: {_TEMP_SESSION_REFUSAL}"
    )

    assert "在这个群里跟云茹说过话" in note, note
    assert "对方还不是好友" not in note, "两种情况给的说法不该混"
    # 认不出来的拒收（例如被禁言）照旧只留原文，不编话。
    assert private_send_refusal_note("OneBot send_msg 失败: {'retcode': 100, 'message': '被禁言'}") == ""


def test_both_notes_carry_no_mechanism_words() -> None:
    """两句说明都零机制词（口径见 `AGENTS.md` §2.2 与这份派活的硬要求）。"""

    from qq_roleplay_bot.prompt_guard import scan_persona_text
    from qq_roleplay_bot.transport import private_send_refusal_note

    notes = [
        private_send_refusal_note(f"OneBot send_msg 失败: {_FRIEND_REFUSAL}"),
        private_send_refusal_note(f"OneBot send_msg 失败: {_TEMP_SESSION_REFUSAL}"),
    ]

    for note in notes:
        assert note, "两条用例都要真的取到说明"
        assert scan_persona_text(note) == [], f"{note} 命中了人格机制词"
        for word in _FORBIDDEN_IN_NOTE:
            assert word not in note, f"{note} 里出现了 {word}"

