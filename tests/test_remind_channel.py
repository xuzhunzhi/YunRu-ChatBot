"""提醒的**频道**那一步：核心不发言，只给她一句；选择从判定里收（2026-10-09 实测那条"乱"）。

用户实测（富哥之家那一晚）：**她自己**在群里问"到时候群里喊你还是私你？""顺便，邮箱给我一个…"，
**同时**插件那条罐头问话也发进了群（"…你想我怎么叫你？1 在本群里 @ 你 2 私聊你 3 寄到你的邮箱
回我一句「/提醒 换 2」"）——**同一件事两个声音在群里说**，这就是"乱"。

定的形状（照邮箱那一步已经做对的那套抄）：

1. **频道这一步不许核心/插件直接发言**：核心只在**易变段**给她一句（`clarify_note` 那个口子，
   与邮箱那一步同一个），三个选项写进那句话里，让她**用自己的话**问；
2. **选择从判定里收**：判定每轮都读聊天，`<remind>` 的**第三段**就是他选的怎么收
   （1 群里 @ / 2 私聊 / 3 邮箱，没说就空着）——**零额外模型调用**；
   （为什么不另开一个 `<remind_channel>` 标签：判定 prompt 的大小有测试钉着，
   另开一个标签会顶破 `SYSTEM_PROMPT × 0.4` 那条线。挤进同一个字段是"最小"的那种加法。）
3. **值不齐就不交给插件**：频道没定、或者选了邮箱却还没邮箱 → 只记待办、让她问；
   ——插件拿到的永远是**一条装好的请求**，它于是**没有理由再发那条罐头问话**。

这一份钉住：

1. 判定给的第三段真的按 `group` / `private` / `email` 递到插件；
2. 没有第三段（或认不出）时：**一个发送都没有**、插件也没被调用，而她的易变段里
   多了那句问话（三个选项都在里面）；
3. `1`（群里 @）不需要邮箱，直接交；`3`（邮箱）先问邮箱、拿到才交；
4. 判定的那段说明没把 prompt 撑破（发出去的那份仍 `< SYSTEM_PROMPT × 0.4`）；
5. 映射就是投递那条 `RemindDelivery.channel` 的同一套词。

突变（各自还原）：把它改回"核心/插件直接发言"（没有频道时用 transport 发一条罐头问话）
→ 第 2 条红；把判定的第三段去掉（不认 `|2`）→ 第 1 条红。
"""
import asyncio
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from qq_roleplay_bot.dialogue_judge import JUDGE_SYSTEM_PROMPT, compose_judge_system_prompt
from qq_roleplay_bot.remind import RemindEmailBook, RemindRequest, remind_channel
from qq_roleplay_bot.stage3_main import DialogueEngine
from qq_roleplay_bot.stage3_runtime import SYSTEM_PROMPT
from qq_roleplay_bot.transport import IncomingMessage, MessageTarget

GROUP = "717151356"
TARGET = MessageTarget(group_id=GROUP)
SESSION = f"group:{GROUP}"
NOW = datetime(2026, 10, 8, 22, 12, tzinfo=timezone(timedelta(hours=8))).timestamp()
TZ = {"QQBOT_LOCAL_TZ_OFFSET_HOURS": "8"}
#: 罐头问话里那几处标记——测试用它证明"核心一个字都没往群里发"。
CANNED_MARKERS = ("想我怎么叫你", "在本群里 @ 你", "回我一句", "/提醒 换")


class _ScriptedJudge:
    def __init__(self, outputs: list[str]) -> None:
        self.outputs = outputs
        self.requests: list = []

    async def complete(self, request):
        self.requests.append(request)
        index = min(len(self.requests) - 1, len(self.outputs) - 1)
        return self.outputs[index]


class _ScriptedReply:
    def __init__(self) -> None:
        self.requests: list = []

    async def complete(self, request):
        self.requests.append(request)
        return ("<decision>REPLY</decision><dialogue>KEEP</dialogue>"
                "<reply>行，我先记着。</reply>")


class _RecordingTransport:
    """只记下"核心真的往群里发了什么"。"""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, target, text, *, reply_to=""):
        self.sent.append(text)

    async def send_segmented(self, target, text, *, reply_to="", part=None, total=None,
                             origin=""):
        self.sent.append(text)


def _message(index: int, text: str = "@YunRu 明天8点提醒我交作业") -> IncomingMessage:
    return IncomingMessage(f"m{index}", SESSION, "100", text, TARGET,
                           sender_name="某人", is_bot_mentioned=True)


def _engine(handed, outputs: list[str]):
    from qq_roleplay_bot.runtime_flags import RuntimeFlags

    judge = _ScriptedJudge(outputs)
    reply = _ScriptedReply()
    transport = _RecordingTransport()
    engine = DialogueEngine(reply, judge_client=judge, enabled_group_ids=frozenset({GROUP}),
                            remind=handed.append, wall_clock=lambda: NOW)
    engine.transport = transport
    engine._default_flags = RuntimeFlags(review_enabled=False)
    return engine, judge, reply, transport


# --- 1 / 3. 第三段真的递到了插件 ---------------------------------------------


def test_the_channel_from_the_judge_reaches_the_plugin() -> None:
    for digit, word in (("1", "group"), ("2", "private")):
        handed: list[RemindRequest] = []
        engine, _, _, _ = _engine(
            handed, [f"<route>REPLY</route><topic>提醒</topic><remind>明天8点|交作业|{digit}</remind>"])
        with patch.dict(os.environ, TZ):
            asyncio.run(engine.handle(_message(1)))

        assert len(handed) == 1, digit
        assert handed[0].channel == word, (digit, handed[0].channel)
        assert handed[0].email == "", "不是邮箱那条路就不该带邮箱"


def test_the_group_channel_needs_no_email_and_asks_nothing() -> None:
    handed: list[RemindRequest] = []
    engine, _, reply, _ = _engine(
        handed, ["<route>REPLY</route><topic>提醒</topic><remind>明天8点|交作业|1</remind>"])
    with patch.dict(os.environ, TZ):
        asyncio.run(engine.handle(_message(1)))

    assert len(handed) == 1
    assert "还没问清" not in reply.requests[0][-1]["content"]


# --- 2. 没定频道：她自己问、核心一个字都不发 ---------------------------------


def test_without_a_channel_she_asks_and_nothing_is_sent() -> None:
    handed: list[RemindRequest] = []
    engine, _, reply, transport = _engine(
        handed, ["<route>REPLY</route><topic>提醒</topic><remind>明天8点|交作业</remind>"])
    with patch.dict(os.environ, TZ):
        asyncio.run(engine.handle(_message(1)))

    # 插件没被调用：它于是没有理由再发那条罐头问话。
    assert handed == [], "频道还没定就把请求交给插件了"
    # 核心**没有**直接发言：发出去的每一条都是她自己的话。
    for text in transport.sent:
        for marker in CANNED_MARKERS:
            assert marker not in text, f"核心/插件直接发了罐头问话：{text}"
    # 该问的问法交给她了：三个选项都在她那句话里。
    volatile = reply.requests[0][-1]["content"]
    for piece in ("群里 @ 他", "私聊他", "寄到他邮箱"):
        assert piece in volatile, piece


def test_an_unknown_third_segment_counts_as_not_chosen() -> None:
    handed: list[RemindRequest] = []
    engine, _, _, _ = _engine(
        handed, ["<route>REPLY</route><topic>提醒</topic><remind>明天8点|交作业|9</remind>"])
    with patch.dict(os.environ, TZ):
        asyncio.run(engine.handle(_message(1)))

    assert handed == []
    assert engine._remind_asks["100"].what == "交作业|9", "认不出的第三段算在事情里"
    assert engine._remind_asks["100"].channel == ""


def test_no_ask_line_for_somebody_else() -> None:
    """她欠**甲**一个答复时，乙说话那一轮不该冒出"她欠甲"的那句话。"""

    handed: list[RemindRequest] = []
    engine, _, reply, _ = _engine(handed, [
        "<route>REPLY</route><topic>提醒</topic><remind>明天8点|交作业</remind>",
        "<route>REPLY</route><topic>闲聊</topic>",
    ])
    other = IncomingMessage("m9", SESSION, "200", "@YunRu 在吗", TARGET,
                            sender_name="另一个人", is_bot_mentioned=True)
    with patch.dict(os.environ, TZ):
        asyncio.run(engine.handle(_message(1)))          # 100 欠一个答复
        assert "100" in engine._remind_asks
        asyncio.run(engine.handle(other))                # 200 说话

    assert "还没问清" not in reply.requests[-1][-1]["content"]


# --- 3. 邮箱那条路：先问邮箱、拿到才交 ---------------------------------------


def test_the_email_channel_asks_for_the_email_then_hands_over() -> None:
    handed: list[RemindRequest] = []
    engine, _, reply, _ = _engine(handed, [
        "<route>REPLY</route><topic>提醒</topic><remind>明天8点|交作业|3</remind>",
        "<route>REPLY</route><topic>邮箱</topic>",
    ])
    engine._remind_emails = RemindEmailBook(persist=False)
    with patch.dict(os.environ, TZ):
        asyncio.run(engine.handle(_message(1)))
        assert handed == [], "邮箱还没有就交了"
        assert "邮箱" in reply.requests[0][-1]["content"]
        asyncio.run(engine.handle(_message(2, "我的邮箱是 lele@example.com")))

    assert len(handed) == 1
    assert handed[0].channel == "email"
    assert handed[0].email == "lele@example.com"


# --- 4 / 5. 判定那段说明与共用词表 -------------------------------------------


def test_the_prompt_carries_the_third_segment_without_breaking_the_size() -> None:
    assert "<remind>时间|事情|怎么收" in JUDGE_SYSTEM_PROMPT
    assert "1 群里 @ 他、2 私聊、" in JUDGE_SYSTEM_PROMPT
    assembled = compose_judge_system_prompt(JUDGE_SYSTEM_PROMPT)
    assert len(assembled) < len(SYSTEM_PROMPT) * 0.4, len(assembled)


def test_the_mapping_is_the_shared_vocabulary() -> None:
    assert remind_channel("1") == "group"
    assert remind_channel("2") == "private"
    assert remind_channel("3") == "email"
    assert remind_channel("1 群里 @ 他") == "group"
    for raw in ("", "9", "邮箱", "abc"):
        assert remind_channel(raw) == "", raw


if __name__ == "__main__":
    import unittest

    unittest.main()
