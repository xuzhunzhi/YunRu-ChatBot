"""提醒那条**真实形状**：`what` 尾巴上的 `|` 不许漏出去，@ 必须是**真的 at 段**。

用户实测（原话："提醒成功发送了，但是格式有问题"）——群里收到的：

    @2639157559 到点了：给dsh发插件反馈|

两处：

1. **`what` 尾巴多一个 `|`**：判定那个字段是 `时间|事情|怎么收` 三段，**第三段空着**时
   模型写成 `5分钟后|给dsh发插件反馈|`，老写法把末尾那个 `|` 一起当成了"事情"
   （`reminders.jsonl` 里存的正是 `"what": "给dsh发插件反馈|"`）。现在：**逐段 trim、
   丢掉尾部空段、`what` 末尾的分隔符也剥掉**（中间的 `|` 是他原话里真有的，保留）。
2. **@ 到底是不是真的**：段类型不会是猜的——这一份**直接断言发出去的消息里
   有 `{"type": "at", "data": {"qq": <数字>}}` 段**（不是断言文本里有 `@`——
   那正是这次没抓出来的原因）。证据链：接缝 `remind_send` → 真
   `OneBotWebSocketTransport.send_at` → 消息段。

突变（各自还原）：① 让尾部 `|` 残留回来 → 第 1 条红；② 把真 @ 退回纯文本
（`remind_send` 一律用 `transport.send`）→ 第 2 条红。
"""
import asyncio
import tempfile

from qq_roleplay_bot.chat_log import ChatLog
from qq_roleplay_bot.dialogue_judge import parse_judge_output
from qq_roleplay_bot.onebot_ws import OneBotWebSocketTransport
from qq_roleplay_bot.remind import RemindDelivery
from qq_roleplay_bot.runtime import _SeamBinder
from qq_roleplay_bot.stage3_main import DialogueEngine
from qq_roleplay_bot.transport import MessageTarget

GROUP = "717151356"
PERSON = "2639157559"
TARGET = MessageTarget(group_id=GROUP)


class _NeverCalled:
    async def complete(self, request):
        raise AssertionError("投递这条路不该调用模型")


def _real_transport(tmp: str):
    """真的 `OneBotWebSocketTransport`，只把最外那一跳（socket）换成假的。"""

    transport = OneBotWebSocketTransport(chat_log=ChatLog(tmp, enabled=True))
    sent: list[object] = []

    async def fake_call_api(action, params=None):
        sent.append((params or {}).get("message"))
        return {"status": "ok", "retcode": 0, "data": {"message_id": 1000 + len(sent)}}

    transport.call_api = fake_call_api  # type: ignore[assignment]
    return transport, sent


def _segments(sent: list) -> list[dict]:
    assert len(sent) == 1, sent
    message = sent[0]
    assert isinstance(message, list), f"发出去的不是消息段列表：{message!r}"
    return message


# --- 1. 用户这条真实形状：第三段空着 -----------------------------------------


def test_the_user_shape_keeps_no_trailing_separator() -> None:
    """`<remind>5分钟后|给dsh发插件反馈|</remind>` —— 第三段空着。"""

    verdict = parse_judge_output(
        "<route>REPLY</route><topic>提醒</topic>"
        "<remind>5分钟后|给dsh发插件反馈|</remind>")
    assert verdict.remind is not None
    assert verdict.remind.what == "给dsh发插件反馈", verdict.remind.what
    assert verdict.remind.time_text == "5分钟后"
    assert verdict.remind.channel == ""


def test_separator_residue_is_dropped_everywhere() -> None:
    cases = {
        "<remind>5分钟后|给dsh发插件反馈|</remind>": "给dsh发插件反馈",
        "<remind>5分钟后|给dsh发插件反馈| </remind>": "给dsh发插件反馈",
        "<remind>5分钟后|给dsh发插件反馈||</remind>": "给dsh发插件反馈",
        "<remind>|5分钟后|给dsh发插件反馈|</remind>": "给dsh发插件反馈",
        "<remind>5分钟后| 给dsh发插件反馈 </remind>": "给dsh发插件反馈",
        "<remind>5分钟后|给dsh发插件反馈|1|</remind>": "给dsh发插件反馈",
    }
    for raw, expected in cases.items():
        verdict = parse_judge_output(raw)
        assert verdict.remind is not None, raw
        assert verdict.remind.what == expected, (raw, verdict.remind.what)
        assert "|" not in verdict.remind.what, raw


def test_a_pipe_inside_his_own_words_is_kept() -> None:
    """他原话里真有 `|`（中间那种）要保留——只有**末尾**的分隔符算残留。"""

    verdict = parse_judge_output("<remind>明天8点|交作业|顺手带伞</remind>")
    assert verdict.remind is not None
    assert verdict.remind.what == "交作业|顺手带伞"
    assert verdict.remind.channel == ""


def test_a_malformed_remind_is_still_dropped() -> None:
    for raw in ("<remind>5分钟后|</remind>", "<remind>|事情|</remind>",
                "<remind>5分钟后||1</remind>"):
        assert parse_judge_output(raw).remind is None, raw


# --- 2. 真 @：断言**段类型** -------------------------------------------------


def test_the_group_delivery_really_carries_an_at_segment() -> None:
    """证据：接缝 → 真传输层，发出去的消息段里有一个 `at` 段（qq 是数字）。"""

    with tempfile.TemporaryDirectory() as tmp:
        transport, sent = _real_transport(tmp)
        seam = _SeamBinder(DialogueEngine(_NeverCalled()), transport)
        result = asyncio.run(seam.remind_send(
            RemindDelivery(person_id=PERSON, text="到点了：给dsh发插件反馈",
                           group_id=GROUP, channel="group")))
        segments = _segments(sent)

    assert result.sent is True and result.reason == "ok"
    at_segments = [seg for seg in segments if seg["type"] == "at"]
    assert len(at_segments) == 1, f"没有真的 @ 段：{segments}"
    assert str(at_segments[0]["data"]["qq"]) == PERSON
    assert "到点了：给dsh发插件反馈" in "".join(
        str(seg["data"].get("text", "")) for seg in segments)
    # 而且它是**第一段**（在正文之前）——@ 得先出来才叫叫人。
    assert segments[0]["type"] == "at", segments


if __name__ == "__main__":
    import unittest

    unittest.main()
