"""**通知进上下文、不进历史、因此不进记忆**（用户 2026-10-19 定的口径）。

三层必须分清（用户原话：「**上下文要留，只是不进记忆，你别搞混了**」）：

| 层 | 是什么 | 通知 |
| --- | --- | --- |
| **会话历史** `state.history` | 谁说了什么、落进状态 | **不进** |
| **上下文**（每轮拼进 prompt 的材料） | 她这一轮看得见的东西 | **要留：每轮都在** |
| **记忆库** | 记忆 agent 从**历史**提取的"她记得的事" | **不进**（历史里没有它，就提取不到） |

三轮口径按时间顺序（后一句覆盖前一句）：

1. 「哎要不还是通知吧，不要当人了」——它是**通知**：`speaker="notice"`，没有 `who`、
   不进名册、没有 `user_id`，也不是她自己的话。
2. 「这个也进，一起当通知」——**命令的回复**也走同一套。
3. 「上下文要留，只是不进记忆」——通知**不进 `state.history`**，但在**上下文里每轮
   都在**（不是"出现一次就消失"），并且**不设单独上限**（寿命跟会话状态一致）。

⚠️ 同一天中途确实有过一版"渲染一次就清空 + 最多 3 条 + 10 分钟过期"，那是**误读**
（用户随后两次纠正），已经撤掉；这里的用例钉的是**最终**口径。

这个文件逐条钉住：①不进历史 ②第一轮材料里有它 ③第二、三轮仍然有它
④记忆侧看不到它 ⑤会话状态重置时清单一并被清 ⑥脱敏与截断仍在
⑦"通知先到、真人消息后到，群背景仍要补"的回归。
"""

from __future__ import annotations

import asyncio
import re
from unittest.mock import patch

import qq_roleplay_bot.notice_text as notice_text
from qq_roleplay_bot import dev_config, runtime
from qq_roleplay_bot.command_plugins import CommandRegistry
from qq_roleplay_bot.memory_model import MemoryMaterial
from qq_roleplay_bot.stage3_main import DialogueEngine, _deliver_reply
from qq_roleplay_bot.stage3_runtime import ConversationState
from qq_roleplay_bot.transport import (
    IncomingMessage,
    MessageNotDelivered,
    MessageTarget,
)

GROUP = "717151356"
SESSION = f"group:{GROUP}"
OWNER = "900000001"


class NeverCalled:
    """命令不该触达模型：真调了就红。"""

    async def complete(self, request):
        raise AssertionError("命令不应触达模型")


class FakeTransport:
    """只记下"发出去了什么"，不发任何东西。"""

    def __init__(self, *, fail: bool = False) -> None:
        self.sent: list[tuple[MessageTarget, str]] = []
        self.fail = fail

    async def send(self, target, text, reply_to=""):
        if self.fail:
            raise MessageNotDelivered("连接不在")
        self.sent.append((target, text))


class Judge:
    """判定说"接"——不然回复那条腿不会跑，也就看不到回复材料。"""

    async def complete(self, request):
        return "<route>REPLY</route><topic>聊天</topic>"


class ReplyClient:
    """记下每一次**回复**请求（判定那几条走 `Judge`，不混在这里）。"""

    def __init__(self) -> None:
        self.calls: list[object] = []

    async def complete(self, request):
        self.calls.append(request)
        return "<decision>REPLY</decision><dialogue>KEEP</dialogue><reply>嗯。</reply>"


class _EmptyIdentity:
    """`identity_for` 的返回值：判定那一路只要一句"这个人是谁"的说明，给空串。"""

    def as_judge_note(self, *args, **kwargs) -> str:
        return ""

    def as_data(self, *args, **kwargs) -> str:
        return ""


class FakeMemory:
    """记忆侧的替身：只记下**喂进去的东西**（用来证明通知没进记忆那条路）。

    只实现"会被调到"的那几个：入站消息走 `capture`、她自己发过的话走
    `record_sent_reply`，其余读数一律返回空（这一条用例只关心前两条）。
    """

    def __init__(self) -> None:
        self.captured: list[str] = []
        self.replies: list[str] = []

    def capture(self, message) -> None:
        self.captured.append(getattr(message, "text", "") or "")

    def record_sent_reply(self, message, text) -> None:
        self.replies.append(text)

    # --- 判定/回复材料那几条读数：给空 -------------------------------------
    async def identity_for(self, message):
        return _EmptyIdentity()

    async def profile_for(self, *args, **kwargs):
        return ""

    async def retrieve(self, *args, **kwargs):
        # 记忆侧这次什么都没检索到（**不是**"通知被过滤掉了"——它根本不在检索的
        # 输入里：检索读的是历史与收件箱，通知不在那两处）。
        return MemoryMaterial()

    async def care_note(self, *args, **kwargs):
        return None

    async def own_notes(self, *args, **kwargs):
        return ()

    def mark_care_noted(self, *args, **kwargs) -> None:
        return None

    def mark_own_note_noted(self, *args, **kwargs) -> None:
        return None

    def note_own_words(self, *args, **kwargs) -> None:
        return None


def engine_for(client=None) -> DialogueEngine:
    """一个真引擎（会话表、时钟、扣子都在），模型那条腿换成替身。"""

    return DialogueEngine(client or NeverCalled())


def talkable_engine() -> tuple[DialogueEngine, ReplyClient]:
    """能真的走完"说一句话"那条路的引擎（判定 + 回复都用替身）。"""

    client = ReplyClient()
    engine = DialogueEngine(client, judge_client=Judge())
    return engine, client


def notify(group_or_user, text, *, fail: bool = False):
    """走**真接缝**发一条通知：`runtime._SeamBinder.notify`。"""

    engine = engine_for()
    transport = FakeTransport(fail=fail)
    status = asyncio.run(
        runtime._SeamBinder(engine, transport).notify(group_or_user, text)
    )
    return engine, transport, status


def message(text, *, user_id=OWNER, message_id="m1", session_id=SESSION,
            target=None, mentioned=False) -> IncomingMessage:
    return IncomingMessage(
        message_id=message_id,
        session_id=session_id,
        user_id=user_id,
        text=text,
        target=target or MessageTarget(group_id=GROUP),
        sender_name="tester",
        is_bot_mentioned=mentioned,
    )


def data_text(request) -> str:
    """请求里除 system 之外的全部正文（DATA 区）。"""

    return "\n".join(part["content"] for part in request[1:])


def system_text(request) -> str:
    return request[0]["content"]


def notices_of(engine) -> list[str]:
    state = engine.sessions.states.get(SESSION)
    return list(state.notices) if state else []


# --- 1. 接缝：发到群里，**进上下文**而不是写历史 -----------------------------


def test_the_notify_seam_keeps_the_notice_in_context_instead_of_history() -> None:
    target = MessageTarget(group_id=GROUP)
    engine, transport, status = notify(target, "群里发一条通知：今晚八点维护。")

    assert status == "sent"
    assert transport.sent and transport.sent[-1][0] == target
    # 正文**原样**发出去（进上下文的那一份才洗过）——这里本来就干净，所以逐字相同。
    assert transport.sent[-1][1] == "群里发一条通知：今晚八点维护。"

    state = engine.sessions.states[SESSION]
    assert list(state.history) == [], "通知不进历史（用户：上下文要留，只是不进记忆）"
    assert state.notices == ["群里发一条通知：今晚八点维护。"]
    # 也不落盘（清单不是 `as_state` 的字段）。
    assert all("今晚八点维护。" not in str(entry) for entry in state.as_state()["history"])


def test_a_notice_never_enters_the_roster() -> None:
    """「不要当人了」：不进名册、不占名册编号。"""

    engine, _transport, _status = notify(MessageTarget(group_id=GROUP), "维护通知。")
    state = engine.sessions.states[SESSION]
    assert state.aliases == {}, "通知不该在名册里占一个编号"
    assert state.roster_lines() == []


def test_the_legacy_single_argument_notify_still_goes_to_the_super_admins() -> None:
    """老写法 `notify(text)` 一个字都不用改：收件人仍是超管。"""

    engine = engine_for()
    transport = FakeTransport()
    with patch.object(dev_config, "APPROVE_NOTIFY_USER_IDS", ("9001", "9002")):
        status = asyncio.run(runtime._SeamBinder(engine, transport).notify("审批：有人申请进群"))

    assert status == "sent"
    assert [item[0] for item in transport.sent] == [
        MessageTarget(user_id="9001"), MessageTarget(user_id="9002"),
    ]
    for user_id in ("9001", "9002"):
        state = engine.sessions.states[f"private:{user_id}"]
        assert state.notices == ["审批：有人申请进群"]
        assert list(state.history) == []


# --- 2. "成功发出才进上下文" -------------------------------------------------


def test_a_lost_message_is_not_put_into_context() -> None:
    """确定没送出去（连补发队列都没有）→ 上下文里一个字都不该多。"""

    engine, transport, status = notify(MessageTarget(group_id=GROUP), "这条发不出去。",
                                       fail=True)
    assert status == "failed"
    assert transport.sent == []
    assert notices_of(engine) == []


def test_nothing_is_put_into_context_when_redaction_leaves_nothing() -> None:
    """整条只有一个 @ / CQ 码：洗完成空 → 不入列（上下文里多一条空通知更糟）。"""

    engine, transport, status = notify(MessageTarget(group_id=GROUP), "[CQ:at,qq=123]")
    assert status == "sent"
    assert len(transport.sent) == 1, "洗的是进上下文那一份，发出去的仍是原文"
    assert notices_of(engine) == []


def test_a_notice_to_a_private_user_outside_the_allowlist_does_not_explode() -> None:
    """没有会话状态时（私聊白名单外那种）不许炸。"""

    engine = engine_for()
    transport = FakeTransport()
    target = MessageTarget(user_id="1234567")
    status = asyncio.run(runtime._SeamBinder(engine, transport).notify(target, "私聊通知。"))
    assert status == "sent"
    assert engine.sessions.states["private:1234567"].notices == ["私聊通知。"]


def test_notify_refuses_a_bare_string_target() -> None:
    """`"800000001"` 是群号还是 QQ 号猜不出来——宁可抛，也不发错地方。"""

    engine = engine_for()
    transport = FakeTransport()
    try:
        asyncio.run(runtime._SeamBinder(engine, transport).notify("800000001", "通知"))
    except ValueError:
        pass
    else:  # pragma: no cover - 走到这里说明那道闸没了
        raise AssertionError("裸字符串目标必须被拒（群号与 QQ 号在这里分不出来）")
    assert transport.sent == []


def test_notify_accepts_a_mapping_target() -> None:
    """从订阅列表那种 JSON 里读出来的目标也能直接用。"""

    engine = engine_for()
    transport = FakeTransport()
    status = asyncio.run(
        runtime._SeamBinder(engine, transport).notify({"group_id": "800000001"}, "通知")
    )
    assert status == "sent"
    assert transport.sent[-1][0] == MessageTarget(group_id="800000001")


# --- 3. 上下文常驻：第一轮有、第二三轮仍然有 ---------------------------------


def test_the_notice_shows_up_in_the_next_turn_material_with_its_own_speaker() -> None:
    """② 第一轮材料里有它，渲染成 `speaker="notice"`（DATA 区，不是 system）。"""

    engine, client = talkable_engine()
    asyncio.run(runtime._SeamBinder(engine, FakeTransport()).notify(
        MessageTarget(group_id=GROUP), "今晚八点维护。"))

    asyncio.run(engine.handle(message("@YunRu 在吗", message_id="m1", mentioned=True)))

    assert client.calls, "这一轮应当真的走到回复模型"
    request = client.calls[0]
    assert "今晚八点维护。" not in system_text(request), "system 前缀一个字都不许被改"
    data = data_text(request)
    assert "今晚八点维护。" in data
    # 位置挨着 `<history>`：就在 `<history>` 那一段的收尾之后、DATA 信封里面。
    assert '<message speaker="notice">' in data
    assert data.index("</history>") < data.index("今晚八点维护。")
    assert data.index("今晚八点维护。") < data.index("--- UNTRUSTED CHAT DATA END ---")
    # 通知既不是她说的，也不是某个人说的：那一条上没有 who / yunru 的标法。
    notice_line = next(line for line in data.splitlines() if "今晚八点维护。" in line)
    assert 'speaker="notice"' in notice_line
    assert "who=" not in notice_line and 'speaker="yunru"' not in notice_line


def test_the_notice_is_still_in_the_second_and_third_turn() -> None:
    """③ **上下文要留**：第二、第三轮的材料里都还在（不是只出现一次）。"""

    engine, client = talkable_engine()
    asyncio.run(runtime._SeamBinder(engine, FakeTransport()).notify(
        MessageTarget(group_id=GROUP), "今晚八点维护。"))

    asyncio.run(engine.handle(message("第一句", message_id="m1", mentioned=True)))
    asyncio.run(engine.handle(message("第二句", message_id="m2")))
    asyncio.run(engine.handle(message("第三句", message_id="m3")))

    assert len(client.calls) == 3, "三轮都该走到回复模型"
    for index in range(3):
        assert "今晚八点维护。" in data_text(client.calls[index]), f"第 {index + 1} 轮应当看得见"
        assert 'speaker="notice"' in data_text(client.calls[index])


def test_the_notice_never_enters_her_history() -> None:
    """① 走完整一轮之后，历史里只有真人那条消息——通知一个字都不在。"""

    engine, _client = talkable_engine()
    asyncio.run(runtime._SeamBinder(engine, FakeTransport()).notify(
        MessageTarget(group_id=GROUP), "今晚八点维护。"))
    asyncio.run(engine.handle(message("今晚谁值班？", message_id="m1", mentioned=True)))

    state = engine.sessions.states[SESSION]
    # 历史里只有"她那一轮真的收发过的东西"：真人那条消息 + 她自己那句回复（`yunru`）。
    assert [entry.text for entry in state.history if not entry.is_bot_message] == ["今晚谁值班？"]
    assert not any(entry.is_notice for entry in state.history)
    assert "今晚八点维护。" not in "\n".join(entry.text for entry in state.history)


def test_the_memory_side_never_sees_the_notice() -> None:
    """④ 记忆提取看不到它：记忆侧拿到的是**消息**，通知从来不经过那条路。

    记忆 agent 从历史里挑"她记得的事"：入站消息走 `memory.capture(message)`、
    她自己发过的话走 `memory.record_sent_reply(...)`。通知既不是入站消息、也不是
    她的话——它只是上下文里的一行材料，所以两条路都收不到它。
    """

    memory = FakeMemory()
    engine, _client = talkable_engine()
    engine.memory_service = memory

    asyncio.run(runtime._SeamBinder(engine, FakeTransport()).notify(
        MessageTarget(group_id=GROUP), "今晚八点维护。"))
    asyncio.run(engine.handle(message("今晚谁值班？", message_id="m1", mentioned=True)))

    assert memory.captured == ["今晚谁值班？"], "喂给记忆侧的只有真人那条消息"
    assert all("今晚八点维护。" not in text for text in memory.captured)
    assert all("今晚八点维护。" not in text for text in memory.replies)
    # 历史里也没有——记忆侧就算去翻历史也翻不到。
    assert "今晚八点维护。" not in "\n".join(
        entry.text for entry in engine.sessions.states[SESSION].history)


def test_the_notice_text_is_sanitized_as_untrusted_data() -> None:
    """插件递进来的是不可信内容：CQ 码 / @ / 控制字符都不许原样落进材料里。"""

    engine, client = talkable_engine()
    asyncio.run(runtime._SeamBinder(engine, FakeTransport()).notify(
        MessageTarget(group_id=GROUP),
        "维护开始\x00\x07 [CQ:at,qq=123456789] @所有人 十分钟",
    ))
    asyncio.run(engine.handle(message("@YunRu 在吗", message_id="m1", mentioned=True)))
    data = data_text(client.calls[0])
    # 当前那句里本来就有 `@YunRu`（那是别人的话），所以只盯**通知那一行**。
    notice_line = next(line for line in data.splitlines() if "维护开始" in line)
    assert "[CQ:" not in notice_line
    assert "@" not in notice_line
    assert "\x00" not in data and "\x07" not in data
    assert "维护开始" in data and "十分钟" in data


# --- 4. 寿命：跟会话状态一致（不另设上限） ----------------------------------


def test_the_notice_list_has_no_separate_cap() -> None:
    """⑤ 用户明确否掉了"最多 N 条"（「哪来一天几十条……怎么撑爆」）：不设上限。

    这条用例钉的是"入列多少条就留多少条"，防的是有人哪天又凭手感加一个数字回来。
    """

    state = ConversationState(history_limit=50)
    for index in range(50):
        assert state.add_notice(f"通知{index}") is True
    assert len(state.notices) == 50
    assert state.notices[0] == "通知0" and state.notices[-1] == "通知49"
    assert not hasattr(state, "notices_dropped"), "没有上限就没有「丢最旧」这回事"


def test_resetting_the_session_clears_the_notice_list() -> None:
    """⑤' 寿命跟会话状态一致：会话被清空/重置时，清单一并清。"""

    engine, _client = talkable_engine()
    asyncio.run(runtime._SeamBinder(engine, FakeTransport()).notify(
        MessageTarget(group_id=GROUP), "今晚八点维护。"))
    assert engine.sessions.states[SESSION].notices == ["今晚八点维护。"]

    # `/admin clear` 与她主动结束对话走的都是这一条。
    engine.sessions.leave(SESSION)
    state = engine.sessions.states[SESSION]
    assert state.notices == [], "会话重置时通知清单一并被清"
    assert list(state.history) == []


def test_a_queued_notice_is_not_persisted() -> None:
    """不落盘：重启（`from_state`）之后不该再看见重启前的通知。"""

    state = ConversationState(history_limit=50)
    state.add_notice("维护通知。")
    restored = type(state).from_state(state.as_state(), history_limit=50)
    assert restored is not None
    assert restored.notices == []
    assert list(restored.history) == []


# --- 5. 命令的回复：同一套（进上下文 / 不写历史 / 进上下文那份脱敏） ---------


STATUS_LIKE = (
    "进程 8123456 还活着；路径 C:\\Users\\xzh\\bot\\run.py；"
    "key sk-abcdef123456；QQ 100012345；版本 3 条消息"
)


class StatusishCommand:
    """一条"输出很像 `/super status`"的命令：用来验脱敏。"""

    name = "statusish"

    def match(self, message) -> bool:
        return message.text.strip() == "/statusish"

    async def handle(self, message):
        return STATUS_LIKE

    def help_text(self) -> str:
        return ""


def _run_command() -> tuple[DialogueEngine, FakeTransport, IncomingMessage]:
    engine = DialogueEngine(NeverCalled(),
                            command_registry=CommandRegistry((StatusishCommand(),)))
    outgoing = asyncio.run(engine.handle(message("/statusish")))
    assert outgoing is not None, "这条命令必须回话"
    transport = FakeTransport()
    source = message("/statusish")
    # `_deliver_reply` 是**唯一**那条"真把它发出去"的路：上下文在这里收。
    asyncio.run(_deliver_reply(transport, engine, source, [outgoing]))
    return engine, transport, source


def test_a_command_reply_is_kept_in_context_not_history() -> None:
    engine, transport, source = _run_command()
    state = engine.sessions.states[source.session_id]
    # ① 不进历史
    assert list(state.history) == []
    # 进上下文
    assert len(state.notices) == 1
    # 发给操作者的那一份**原文没变**（该看到的还是完整输出）。
    assert transport.sent[-1][1] == STATUS_LIKE
    # 上下文那一份洗过了。
    assert state.notices[0] != STATUS_LIKE


def test_the_context_copy_of_a_command_reply_has_no_numbers_paths_or_keys() -> None:
    """⑦ 脱敏沿用：`/super status` 那类输出进上下文后没有长数字串 / 盘符路径 / `sk-`。"""

    engine, _transport, source = _run_command()
    recorded = engine.sessions.states[source.session_id].notices[0]
    assert not re.search(r"\d{6,}", recorded), recorded
    assert not re.search(r"[A-Za-z]:[\\/]", recorded), recorded
    assert "sk-" not in recorded, recorded
    assert "100012345" not in recorded
    assert "run.py" not in recorded, "路径被换成记号之后，文件名也不该留着"
    # 没被洗光的普通信息还在——否则这一份等于空的。
    assert "版本 3 条消息" in recorded


def test_a_command_reply_is_truncated_to_two_hundred_chars() -> None:
    class LongCommand(StatusishCommand):
        name = "longish"

        def match(self, message) -> bool:
            return message.text.strip() == "/longish"

        async def handle(self, message):
            return "很长" * 400

    engine = DialogueEngine(NeverCalled(),
                            command_registry=CommandRegistry((LongCommand(),)))
    outgoing = asyncio.run(engine.handle(message("/longish")))
    source = message("/longish")
    asyncio.run(_deliver_reply(FakeTransport(), engine, source, [outgoing]))
    recorded = engine.sessions.states[source.session_id].notices[0]
    assert len(recorded) <= notice_text.MAX_NOTICE_CHARS
    # 截断要看得出来（不是静默少一段）。
    assert recorded.endswith("…")


def test_a_command_reply_is_rendered_as_a_notice_not_as_her_words() -> None:
    """命令回复在材料里是 `speaker="notice"`——她没说过那句话。"""

    engine = DialogueEngine(NeverCalled(),
                            command_registry=CommandRegistry((StatusishCommand(),)))
    # 换成能走完一轮的模型替身，然后在同一个会话里说话。
    engine.judge_client = Judge()
    client = ReplyClient()
    engine.client = client

    outgoing = asyncio.run(engine.handle(message("/statusish")))
    asyncio.run(_deliver_reply(FakeTransport(), engine, message("/statusish"), [outgoing]))
    asyncio.run(engine.handle(message("第二句", message_id="m2", mentioned=True)))

    data = data_text(client.calls[0])
    assert 'speaker="notice"' in data
    assert 'speaker="yunru"' not in data


# --- 6. 群背景那条回归 + system 那句 ----------------------------------------


def test_the_first_real_message_still_gets_the_group_background_after_a_notice() -> None:
    """回归测试：通知先到，群背景照补（用户 2026-10-19 裁定的那一条）。

    > 「无会话状态时建会话 + 改 `_seed_history` 判据——确认，但补一条测试：
    >  **第一条进来的若是通知、随后第一条真人消息到达时，群背景仍然正常落进历史**。
    >  加在 `tests/test_notice_history.py` 里，写清依据（用户裁定：允许为通知建会话，
    >  但不许让它顶掉群背景）。」

    最终口径下这条退化**在结构上不存在**：通知不进 `history`，`_seed_history` 的判据
    就是原来的"历史非空"，上一轮为通知加的那个特判也撤回了。用例保留，钉的就是
    "通知不会顶掉群背景"这件事本身。
    """

    class Provider:
        def __init__(self) -> None:
            self.calls = 0

        async def collect_seed_messages(self, session_id, group_id, message):
            self.calls += 1
            return (
                IncomingMessage(
                    message_id="history:1", session_id=session_id, user_id="2002",
                    text="（背景）昨晚聊到十一点", target=MessageTarget(group_id=GROUP),
                    sender_name="小L",
                ),
            )

    provider = Provider()
    engine, client = talkable_engine()
    engine.context_provider = provider

    asyncio.run(runtime._SeamBinder(engine, FakeTransport()).notify(
        MessageTarget(group_id=GROUP), "今晚八点维护。"))
    assert list(engine.sessions.states[SESSION].history) == [], "通知不进历史"

    asyncio.run(engine.handle(message("在吗", message_id="m1", mentioned=True)))

    assert provider.calls == 1, "群背景必须照补（通知不占历史）"
    texts = [entry.text for entry in engine.sessions.states[SESSION].history]
    assert "（背景）昨晚聊到十一点" in texts
    assert "今晚八点维护。" not in texts
    assert "（背景）昨晚聊到十一点" in data_text(client.calls[0])


def test_the_system_prompt_explains_the_notice_speaker_generically() -> None:
    """system 里那句话：说明 `speaker="notice"` 是什么，且**不绑任何具体人设**。"""

    from qq_roleplay_bot.stage3_runtime import SYSTEM_PROMPT

    sentence = 'speaker="notice" 是这里发出来的通知，不是你说的，也不要当成哪个人在说话。'
    assert sentence in SYSTEM_PROMPT
    # 通用措辞：不许出现"系统/程序/机器人/播报"，也不许把某个部署的设定写进去。
    for banned in ("系统", "程序", "机器人", "播报", "基地", "反抗军"):
        assert banned not in sentence, f"这句话里不该出现「{banned}」"
    # 挨着解释 `speaker="yunru"` 的那一句（同一段、同一组标记）。
    yunru = 'speaker="yunru" 是你自己说过的话，不能算到别人头上。'
    assert yunru in SYSTEM_PROMPT
    assert SYSTEM_PROMPT.index(yunru) < SYSTEM_PROMPT.index(sentence) < SYSTEM_PROMPT.index(yunru) + 200


# --- 7. 洗正文那个函数本身 ---------------------------------------------------


def test_redaction_masks_five_classes_and_keeps_short_numbers() -> None:
    text = ("邮箱 a@b.com；key sk-abcdef123456；token: abcdef；"
            "路径 C:\\Users\\x\\a.log；QQ 100012345；版本 3 条、0.5 秒")
    cleaned = notice_text.for_history(text)
    assert "a@b.com" not in cleaned
    assert "sk-" not in cleaned
    assert "abcdef" not in cleaned
    assert "C:\\" not in cleaned
    assert "100012345" not in cleaned
    assert "版本 3 条" in cleaned and "0.5 秒" in cleaned
    assert notice_text.REDACTED in cleaned


def test_a_plain_link_survives_redaction() -> None:
    """URL 里的 `/a/b` 不是本机路径，别被"路径"那条规则吃掉。"""

    cleaned = notice_text.for_history("看 http://example.com/news/2026/a.html 这条")
    assert "http://example.com/news/2026/a.html" in cleaned


def test_the_redactor_is_idempotent_on_clean_text() -> None:
    """干净文本原样通过（否则每轮改一个字，前缀缓存全断）。"""

    text = "今晚八点维护，预计十分钟。"
    assert notice_text.for_history(text) == text
    assert notice_text.for_history(text) == notice_text.for_history(notice_text.for_history(text))
