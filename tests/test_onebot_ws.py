import asyncio

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
