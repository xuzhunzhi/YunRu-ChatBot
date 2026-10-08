"""`send_at` 的 `user_id` 校验（独立审查 2026-10-09 第 6 条）。

审查实测：`send_at` 把 `person_id` **原样**塞进 `at` 段，而 `_id_value` 只试 `int()`、
失败就回原串——于是能拼出 `{"type": "at", "data": {"qq": "all"}}`，
那是 NapCat 的"@全体"语义。定级低危（`person_id` 来自事件的 `user_id`，真实适配器给的是
数字，要打得中得先能伪造事件），但一行校验就能关掉。

现在的规矩：**`user_id` 必须是数字**（ASCII 数字；空串 / `all` / `123abc` / 全角数字
都算非数字）→ **当成没有 @ 能力**：退回纯文本、**消息照发**（与"没有 `send_at` 就退回
`send`"同一条规矩）。数字则照旧拼 `[reply?][at][text]`。

这一份钉住：

1. 数字：`at` 段是真的（`qq` 是数字），正文照旧；
2. 非数字：**一个 `at` 段都没有**，只有纯文本，而且**真的发出去了**；
3. 转发层（`runtime.remind_send`）那条链上也是这个行为（`transfer` 走 `send_at`）。

突变（各自还原）：去掉那行校验 → 第 2 条红。
"""
import asyncio
import tempfile

from qq_roleplay_bot.chat_log import ChatLog
from qq_roleplay_bot.onebot_ws import OneBotWebSocketTransport
from qq_roleplay_bot.transport import MessageTarget

GROUP = "717151356"
TARGET = MessageTarget(group_id=GROUP)


def _transport(tmp: str):
    """真的 `OneBotWebSocketTransport`，只把最外那一跳（socket）换成假的。"""

    transport = OneBotWebSocketTransport(chat_log=ChatLog(tmp, enabled=True))
    sent: list[object] = []

    async def fake_call_api(action, params=None):
        sent.append((params or {}).get("message"))
        return {"status": "ok", "retcode": 0, "data": {"message_id": 1000 + len(sent)}}

    transport.call_api = fake_call_api  # type: ignore[assignment]
    return transport, sent


def _segments(sent: list) -> list[dict]:
    """把发出去的那一条摊平成消息段列表（纯文本也可能是字符串）。"""

    assert len(sent) == 1, sent
    message = sent[0]
    if isinstance(message, str):
        return [{"type": "text", "data": {"text": message}}]
    return list(message)


def test_a_numeric_user_id_gets_a_real_at_segment() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        transport, sent = _transport(tmp)
        asyncio.run(transport.send_at(TARGET, "10001", "交作业"))
        segments = _segments(sent)

    at = [seg for seg in segments if seg["type"] == "at"]
    assert len(at) == 1
    assert str(at[0]["data"]["qq"]) == "10001"
    assert "交作业" in "".join(str(seg["data"].get("text", "")) for seg in segments)


def test_a_non_numeric_user_id_degrades_to_plain_text_and_still_sends() -> None:
    """**审查第 6 条那条**：非数字不许拼进 `at` 段（`all` = @全体）。

    退化之后的规矩与"传输层没有 `send_at`"完全一致：**退回纯文本、消息照发**。
    """

    for user_id in ("all", "", "  ", "123abc", "９９", "everyone"):
        with tempfile.TemporaryDirectory() as tmp:
            transport, sent = _transport(tmp)
            asyncio.run(transport.send_at(TARGET, user_id, "交作业"))
            segments = _segments(sent)

        assert [seg for seg in segments if seg["type"] == "at"] == [], user_id
        text = "".join(str(seg["data"].get("text", "")) for seg in segments)
        assert "交作业" in text, f"{user_id}: 消息没发出去"
        assert "@" not in text, f"{user_id}: 正文里不该出现 @" 


def test_the_seam_chain_also_degrades() -> None:
    """投递接缝那条链上也一样（`runtime.remind_send` → `transport.send_at`）。"""

    from qq_roleplay_bot.remind import RemindDelivery
    from qq_roleplay_bot.runtime import _SeamBinder
    from qq_roleplay_bot.stage3_main import DialogueEngine

    class _NeverCalled:
        async def complete(self, request):
            raise AssertionError("投递这条路不该调用模型")

    with tempfile.TemporaryDirectory() as tmp:
        transport, sent = _transport(tmp)
        seam = _SeamBinder(DialogueEngine(_NeverCalled()), transport)
        result = asyncio.run(seam.remind_send(
            RemindDelivery(person_id="all", text="交作业", group_id=GROUP, channel="group")))
        segments = _segments(sent)

    # 人是谁由插件给；核心那一侧只保证"不许把 all 拼进 @ 段"，消息照旧送到群里。
    assert result.sent is True and result.reason == "ok"
    assert [seg for seg in segments if seg["type"] == "at"] == []
    assert "交作业" in "".join(str(seg["data"].get("text", "")) for seg in segments)


if __name__ == "__main__":
    import unittest

    unittest.main()
