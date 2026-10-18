"""**发出去的通知要进她的历史，而且记成"通知"，不是"某个人"**。

用户 2026-10-19 定下的口径（原话，别自己改语义）：

- 「发送通知这个，包括命令的回复，能不能通过规则让云茹识别成另一个人在说话」
  → 紧接着改口：「哎要不还是通知吧，不要当人了」。
  所以历史里那一条是 `speaker="notice"`：**既不是她说的**（`speaker="yunru"`），
  **也不是哪个人说的**（没有 `who`、不进名册、没有 `user_id`）。
- 「这个也进，一起当通知」：**命令的回复**也记进历史，跟后台插件发的通知同一套。

另外两条是同一次交底里的硬要求：

- 通知正文是**不可信 DATA**（来自插件与命令输出），渲染照旧 sanitize + 中和伪造记号；
- 命令回复**进历史的那一份**要脱敏（QQ 号/群号、盘符路径、`sk-` 凭据、邮箱、长数字串）
  并截断 200 字，而**发给操作者的那一份原文不许变**（`AGENTS.md` §2.1「不记录敏感内容」）。

这个文件把这些逐条钉住，包括"没有会话状态时不许炸"（私聊白名单外那种）。
"""

from __future__ import annotations

import asyncio
import re
from unittest.mock import patch

import qq_roleplay_bot.notice_text as notice_text
from qq_roleplay_bot import dev_config, runtime
from qq_roleplay_bot.command_plugins import CommandRegistry
from qq_roleplay_bot.stage3_main import (
    DialogueEngine,
    _deliver_reply,
)
from qq_roleplay_bot.stage3_runtime import (
    ConversationMode,
    _format_history,
    message_index_of,
)
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


def engine_for(transport=None, *, client=None) -> DialogueEngine:
    """一个真引擎（会话表、时钟、扣子都在），模型那条腿换成替身。"""

    return DialogueEngine(client or NeverCalled())


def notify(group_or_user, text, *, fail: bool = False):
    """走**真接缝**发一条通知：`runtime._SeamBinder.notify`。

    这样测的是"核心统一发送 + 记历史"这条生产路径，不是把 `record_notice` 叫一遍。
    """

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


# --- 1. 接缝：发到群里，并且真记进了那个会话的历史 ---------------------------


def test_the_notify_seam_records_a_notice_in_the_target_session() -> None:
    target = MessageTarget(group_id=GROUP)
    engine, transport, status = notify(target, "群里发一条通知：今晚八点维护。")

    assert status == "sent"
    assert transport.sent and transport.sent[-1][0] == target
    # 正文**原样**发出去（历史里那份才洗过）——这里本来就干净，所以逐字相同。
    assert transport.sent[-1][1] == "群里发一条通知：今晚八点维护。"

    state = engine.sessions.states[SESSION]
    last = state.history[-1]
    assert last.is_notice is True
    assert last.is_bot_message is False, "通知不是她自己说的话"
    assert last.user_id == "", "通知没有'谁'"
    assert last.text == "群里发一条通知：今晚八点维护。"


def test_a_notice_never_enters_the_roster() -> None:
    """「不要当人了」：不进名册、不占名册编号。"""

    engine, _transport, _status = notify(MessageTarget(group_id=GROUP), "维护通知。")
    state = engine.sessions.states[SESSION]
    assert state.aliases == {}, "通知不该在名册里占一个编号"
    assert state.roster_lines() == []
    assert state.alias_of(state.history[-1]) is None


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
    # 每个收件人的私聊会话里各记一条。
    for user_id in ("9001", "9002"):
        last = engine.sessions.states[f"private:{user_id}"].history[-1]
        assert last.is_notice is True


# --- 2. "成功发出才记" -------------------------------------------------------


def test_a_lost_message_is_not_recorded_in_her_history() -> None:
    """确定没送出去（连补发队列都没有）→ 历史里一个字都不该多。"""

    engine, transport, status = notify(MessageTarget(group_id=GROUP), "这条发不出去。",
                                       fail=True)
    assert status == "failed"
    assert transport.sent == []
    assert SESSION not in engine.sessions.states or not engine.sessions.states[SESSION].history


def test_nothing_is_recorded_when_redaction_leaves_nothing() -> None:
    """整条只有一个 @ / CQ 码：洗完成空 → 不记（历史里多一条空通知更糟）。"""

    engine, transport, status = notify(MessageTarget(group_id=GROUP), "[CQ:at,qq=123]")
    assert status == "sent"
    assert len(transport.sent) == 1, "洗的是历史那一份，发出去的仍是原文"
    assert SESSION not in engine.sessions.states or not engine.sessions.states[SESSION].history


def test_a_notice_to_a_private_user_outside_the_allowlist_does_not_explode() -> None:
    """没有会话状态时（私聊白名单外那种）不许炸——记不记是次要的。"""

    engine = engine_for()
    transport = FakeTransport()
    target = MessageTarget(user_id="1234567")
    status = asyncio.run(runtime._SeamBinder(engine, transport).notify(target, "私聊通知。"))
    assert status == "sent"
    last = engine.sessions.states["private:1234567"].history[-1]
    assert last.is_notice is True and last.session_id == "private:1234567"


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


# --- 3. 渲染：`speaker="notice"`，正文仍按 DATA 处理 ------------------------


def test_the_notice_is_rendered_with_its_own_speaker() -> None:
    engine, _transport, _status = notify(MessageTarget(group_id=GROUP), "维护通知。")
    state = engine.sessions.states[SESSION]
    rendered = _format_history(state.history)
    assert 'speaker="notice"' in rendered
    assert 'speaker="yunru"' not in rendered
    assert "who=" not in rendered, "通知不是人，不该有 who"
    assert "index=" not in rendered, "通知不编号，也不该被 <reply_to> 指到"


def test_the_notice_text_is_sanitized_as_untrusted_data() -> None:
    """插件递进来的是不可信内容：CQ 码 / @ / 控制字符都不许原样落到历史里。"""

    engine, transport, _status = notify(
        MessageTarget(group_id=GROUP),
        "维护开始\x00\x07 [CQ:at,qq=123456789] @所有人 十分钟",
    )
    # 发出去的是原文（协议记号由传输层那一侧自己处理）。
    assert "[CQ:at" in transport.sent[-1][1]
    recorded = engine.sessions.states[SESSION].history[-1].text
    assert "[CQ:" not in recorded
    assert "@" not in recorded
    assert "\x00" not in recorded and "\x07" not in recorded
    assert "维护开始" in recorded and "十分钟" in recorded


def test_the_notice_lands_in_the_DATA_section_not_the_system_prompt() -> None:
    """回复材料里它在 DATA 区（稳定段），不在 system 前缀里。"""

    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[object] = []

        async def complete(self, request):
            self.calls.append(request)
            return "<decision>REPLY</decision><reply>知道了。</reply>"

    client = FakeClient()
    engine = engine_for(client=client)
    asyncio.run(runtime._SeamBinder(engine, FakeTransport()).notify(
        MessageTarget(group_id=GROUP), "今晚八点维护。"))

    asyncio.run(engine.handle(message("@YunRu 在吗", mentioned=True)))
    request = client.calls[0]
    system_prompt = request[0]["content"]
    data = "\n".join(part["content"] for part in request[1:])

    assert "今晚八点维护。" not in system_prompt
    assert "今晚八点维护。" in data
    # 落在 `<history>` 那一段里（不是别的旁路材料）。
    inside_history = data.split("<history>")[1].split("</history>")[0]
    assert 'speaker="notice"' in inside_history
    assert "今晚八点维护。" in inside_history


def test_a_reply_to_cannot_point_at_a_notice() -> None:
    """通知不参与编号：`<reply_to>` 指到它没有意义。"""

    engine, _transport, _status = notify(MessageTarget(group_id=GROUP), "维护通知。")
    state = engine.sessions.states[SESSION]
    state.add(message("@YunRu 在吗", message_id="m1"), 1.0)
    mapping = message_index_of(state.history, state.history[-1])
    # 通知（第 0 条）不编号，真人那条拿到位置编号 1。
    assert set(mapping) == {"1", "current"}, mapping


# --- 4. 命令的回复：同一套（发出去之后 / 记成通知 / 进历史那份脱敏） ---------


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


def _run_command(text=" /statusish") -> tuple[DialogueEngine, FakeTransport, IncomingMessage]:
    engine = DialogueEngine(NeverCalled(),
                            command_registry=CommandRegistry((StatusishCommand(),)))
    outgoing = asyncio.run(engine.handle(message(text.strip())))
    assert outgoing is not None, "这条命令必须回话"
    transport = FakeTransport()
    source = message(text.strip())
    # `_deliver_reply` 是**唯一**那条"真把它发出去"的路：历史在这里记。
    asyncio.run(_deliver_reply(transport, engine, source, [outgoing]))
    return engine, transport, source


def test_a_command_reply_is_recorded_as_a_notice_after_it_is_sent() -> None:
    engine, transport, source = _run_command()
    state = engine.sessions.states[source.session_id]
    last = state.history[-1]
    assert last.is_notice is True
    assert last.is_bot_message is False
    assert last.user_id == ""
    # 发给操作者的那一份**原文没变**（该看到的还是完整输出）。
    assert transport.sent[-1][1] == STATUS_LIKE
    # 进历史的那一份洗过了。
    assert last.text != STATUS_LIKE


def test_a_command_reply_does_not_become_her_own_words() -> None:
    """命令回复不许渲染成 `speaker="yunru"`——她没说过那句话。"""

    engine, _transport, source = _run_command()
    rendered = _format_history(engine.sessions.states[source.session_id].history)
    assert 'speaker="notice"' in rendered
    assert 'speaker="yunru"' not in rendered


def test_the_command_reply_history_copy_has_no_numbers_paths_or_keys() -> None:
    """`/super status` 那类输出进历史后：不含长数字串 / 盘符路径 / `sk-` 片段。"""

    engine, _transport, source = _run_command()
    recorded = engine.sessions.states[source.session_id].history[-1].text
    assert not re.search(r"\d{6,}", recorded), recorded
    assert not re.search(r"[A-Za-z]:[\\/]", recorded), recorded
    assert "sk-" not in recorded, recorded
    assert "100012345" not in recorded
    assert "run.py" not in recorded, "路径被换成记号之后，文件名也不该留着"
    # 没被洗光的普通信息还在——否则这条历史等于空的。
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
    recorded = engine.sessions.states[source.session_id].history[-1].text
    assert len(recorded) <= notice_text.MAX_NOTICE_CHARS
    # 截断要看得出来（不是静默少一段）。
    assert recorded.endswith("…")


# --- 5. 持久化 / 群背景 / prompt 里那句话 -----------------------------------


def test_a_notice_survives_a_state_round_trip() -> None:
    """重启之后"她看过那条通知"这件事还在（空 `user_id` 不许被当成坏记录丢掉）。"""

    engine, _transport, _status = notify(MessageTarget(group_id=GROUP), "维护通知。")
    state = engine.sessions.states[SESSION]
    restored = type(state).from_state(state.as_state(), history_limit=50)
    assert restored is not None
    last = restored.history[-1]
    assert last.is_notice is True
    assert last.user_id == ""
    assert last.text == "维护通知。"


def test_a_notice_does_not_stop_the_group_background_from_being_seeded() -> None:
    """"历史非空"不许被通知顶掉：群背景只在**还没人说过话**时才补。"""

    class Provider:
        def __init__(self) -> None:
            self.calls = 0

        async def collect_seed_messages(self, session_id, group_id, message):
            self.calls += 1
            return []

    provider = Provider()
    engine = DialogueEngine(NeverCalled())
    engine.context_provider = provider
    state = engine.sessions.state(SESSION)
    state.record_notice(SESSION, MessageTarget(group_id=GROUP), "维护通知。", 10.0)

    asyncio.run(engine._seed_history(message("第一条真人消息", message_id="m9"), state))
    assert provider.calls == 1, "只有通知的历史仍然算'还没人说过话'，背景照补"


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


# --- 6. 洗正文那个函数本身 ---------------------------------------------------


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


def test_the_history_speaker_constant_is_documented() -> None:
    """`speaker="notice"` 这个词只在一处生成（渲染那一处），别在别处再拼一遍。"""

    engine, _transport, _status = notify(MessageTarget(group_id=GROUP), "维护通知。")
    state = engine.sessions.states[SESSION]
    state.mode = ConversationMode.ACTIVE
    rendered = _format_history(state.history)
    assert rendered.count('speaker="notice"') == 1
