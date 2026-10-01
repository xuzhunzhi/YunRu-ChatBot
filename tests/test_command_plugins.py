"""命令插件接口的离线覆盖。

抽插件的目的是：**加一个新的普通用户命令，不需要改 `stage3_main.py`**。
所以测试重点不是内置命令本身，而是接口是否真的支持"外部注册"，
以及边界是否挡住了不该给插件的权力。
"""
import asyncio

from qq_roleplay_bot.builtin_commands import (
    HelpCommand,
    PingCommand,
    build_command_registry,
    build_help_text,
)
from qq_roleplay_bot.command_plugins import CommandRegistry
from qq_roleplay_bot.stage3_main import PUBLIC_HELP, DialogueEngine
from qq_roleplay_bot.transport import IncomingMessage, MessageTarget

GROUP = "717151356"
OWNER = "900000001"


class NeverCalled:
    """任何走到模型的调用都算失败——命令不该触达模型。"""

    async def complete(self, request):
        raise AssertionError("命令不应触达模型")


def msg(text, *, user_id=OWNER, target=None, message_id="m1"):
    target = target or MessageTarget(group_id=GROUP)
    return IncomingMessage(
        message_id=message_id, session_id="group:717151356", user_id=user_id,
        text=text, target=target, sender_name="tester",
    )


def private(user_id=OWNER, text="x", message_id="p1"):
    return IncomingMessage(
        message_id=message_id, session_id=f"private:{user_id}", user_id=user_id,
        text=text, target=MessageTarget(user_id=user_id), sender_name="tester",
    )


# --- 接口本身 ---------------------------------------------------------------


class EchoCommand:
    """一个外部命令插件：不碰核心代码就能注册进来。"""

    name = "echo"

    def match(self, message) -> bool:
        return message.text.strip() == "/echo"

    async def handle(self, message):
        return "回声"

    def help_text(self) -> str:
        return "其他\n· /echo → 回一声。"


def test_external_plugin_works_without_touching_the_core() -> None:
    registry = build_command_registry((EchoCommand(),))
    engine = DialogueEngine(NeverCalled(), command_registry=registry)
    result = asyncio.run(engine.handle(msg("/echo")))
    assert result is not None and result.text == "回声"


def test_external_plugin_shows_up_in_help_automatically() -> None:
    """帮助由插件自述汇总，不需要改写死的文案。"""

    registry = build_command_registry((EchoCommand(),))
    engine = DialogueEngine(NeverCalled(), command_registry=registry)
    result = asyncio.run(engine.handle(msg("/help")))
    assert result is not None
    assert "/echo" in result.text
    # 原有的两段仍在。
    assert "连通测试" in result.text
    assert "管理与超管命令" in result.text


def test_plugin_help_lines_are_inserted_before_admin_section() -> None:
    text = build_help_text(("其他\n· /echo → 回一声。",))
    assert text.index("/echo") < text.index("管理与超管命令")


# --- 边界：插件不能绕过核心 -------------------------------------------------


def test_plugin_without_session_allowed_defaults_to_public() -> None:
    """未声明 session_allowed 的插件按公开命令处理（任何会话可用）。"""

    registry = CommandRegistry((EchoCommand(),))
    engine = DialogueEngine(NeverCalled(), command_registry=registry)
    other = MessageTarget(group_id="999999999")
    result = asyncio.run(engine.handle(msg("/echo", target=other)))
    assert result is not None and result.text == "回声"


def test_plugin_can_restrict_its_session() -> None:
    class GroupOnly:
        name = "group_only"

        def match(self, message):
            return message.text.strip() == "/grouponly"

        def session_allowed(self, target):
            return target.group_id == GROUP

        async def handle(self, message):
            return "仅本群"

        def help_text(self):
            return ""

    registry = CommandRegistry((GroupOnly(),))
    engine = DialogueEngine(NeverCalled(), command_registry=registry)

    allowed = asyncio.run(engine.handle(msg("/grouponly", message_id="a1")))
    assert allowed is not None and allowed.text == "仅本群"

    # 不满足会话条件时静默回落：既不回复，也不报错。
    denied = asyncio.run(engine.handle(
        msg("/grouponly", target=MessageTarget(group_id="999999999"), message_id="a2")
    ))
    assert denied is None


def test_failing_plugin_does_not_break_message_handling() -> None:
    """坏插件不能吃掉消息，也不能让整条流程炸掉。"""

    class Exploding:
        name = "boom"

        def match(self, message):
            return message.text.strip() == "/boom"

        async def handle(self, message):
            raise RuntimeError("plugin failure")

        def help_text(self):
            return ""

    registry = CommandRegistry((Exploding(),))
    engine = DialogueEngine(NeverCalled(), command_registry=registry)
    assert asyncio.run(engine.handle(msg("/boom"))) is None


def test_plugin_match_exception_is_treated_as_no_match() -> None:
    class BadMatch:
        name = "bad_match"

        def match(self, message):
            raise RuntimeError("match failure")

        async def handle(self, message):
            return "不该到这里"

        def help_text(self):
            return ""

    registry = CommandRegistry((BadMatch(),))
    engine = DialogueEngine(NeverCalled(), command_registry=registry)
    # 匹配抛异常 = 不认领；这条消息不属于任何命令，会落到普通对话（这里就是模型）。
    assert registry.plugins == (registry.plugins[0],)  # 结构未被破坏
    assert asyncio.run(engine.commands.dispatch(msg("/bad_match"))) is None


def test_plugin_returns_none_releases_the_message() -> None:
    """插件认领但选择不处理时，应当回落而不是发空消息。"""

    class Decline:
        name = "decline"

        def match(self, message):
            return message.text.strip() == "/decline"

        async def handle(self, message):
            return None

        def help_text(self):
            return ""

    registry = CommandRegistry((Decline(),))
    assert asyncio.run(registry.dispatch(msg("/decline"))) is None


def test_new_command_plugin_reply_is_not_paced_by_default() -> None:
    """新加的插件命令不该需要"记得"写 paced=False。

    由来：`OutgoingMessage.paced` 默认原本是 True，漏设就会让命令白等一秒多，
    而漏设是静默的——`/admin help` 真的漏过。现在默认是 False（工具性响应），
    只有角色回复需要显式写 True。
    """

    class Fresh:
        name = "fresh"

        def match(self, message):
            return message.text.strip() == "/fresh"

        async def handle(self, message):
            return "新命令"

        def help_text(self):
            return "其他\n· /fresh → 新命令。"

    registry = build_command_registry((Fresh(),))
    engine = DialogueEngine(NeverCalled(), command_registry=registry)
    result = asyncio.run(engine.handle(msg("/fresh")))
    assert result is not None and result.text == "新命令"
    assert result.paced is False


def test_command_replies_are_not_paced() -> None:
    """命令回复必须立刻送出，不参与拟人化停顿。

    这条不变量曾经漏过：`/admin help` 和 `/super help` 不走插件路径，
    改完插件后它们的 paced 仍是 True，会白等一秒多。这里把所有命令一起锁住。
    """

    cases = [
        ("/yunru ping", "c1", OWNER),
        ("/help", "c2", OWNER),
        ("/admin help", "c3", OWNER),
        ("/admin status", "c4", OWNER),
        ("/super help", "c5", OWNER),
        ("/super status", "c6", OWNER),
    ]
    for text, message_id, user_id in cases:
        engine = DialogueEngine(NeverCalled())
        result = asyncio.run(engine.handle(msg(text, user_id=user_id, message_id=message_id)))
        assert result is not None, text
        assert result.paced is False, f"{text} 不应参与拟人化节奏"


def test_model_replies_are_paced() -> None:
    """对照：模型生成的聊天回复必须参与节奏，否则拟人化就失效了。"""

    class Replying:
        async def complete(self, request):
            return "<decision>REPLY</decision><dialogue>KEEP</dialogue><reply>嗯。</reply>"

    engine = DialogueEngine(Replying())
    result = asyncio.run(engine.handle(msg("云茹在吗", message_id="r1")))
    assert result is not None and result.paced is True


# --- 内置命令行为与旧实现一致 -----------------------------------------------


def test_ping_is_available_in_any_session_and_when_disabled() -> None:
    engine = DialogueEngine(NeverCalled())
    engine.set_enabled(False)
    for target, mid in (
        (MessageTarget(group_id="999999999"), "g1"),
        (MessageTarget(user_id="987654321"), "p1"),
    ):
        result = asyncio.run(engine.handle(msg("/yunru ping", target=target, message_id=mid)))
        assert result is not None and result.text == "pong"


def test_help_is_available_in_any_session_and_when_disabled() -> None:
    engine = DialogueEngine(NeverCalled())
    engine.set_enabled(False)
    for target, mid in (
        (MessageTarget(group_id="999999999"), "g1"),
        (MessageTarget(user_id="987654321"), "p1"),
    ):
        result = asyncio.run(engine.handle(msg("/help", target=target, message_id=mid)))
        assert result is not None and result.text == PUBLIC_HELP


def test_commands_are_deduplicated() -> None:
    engine = DialogueEngine(NeverCalled())
    first = asyncio.run(engine.handle(msg("/yunru ping", message_id="same")))
    assert first is not None
    assert asyncio.run(engine.handle(msg("/yunru ping", message_id="same"))) is None


def test_help_aliases_all_resolve() -> None:
    engine = DialogueEngine(NeverCalled())
    for index, text in enumerate(("/help", "/yunru", "yunru help", "yunru 帮助")):
        result = asyncio.run(engine.handle(msg(text, message_id=f"a{index}")))
        assert result is not None, text
        assert result.text == PUBLIC_HELP, text


def test_ping_plugin_help_text_is_listed() -> None:
    registry = build_command_registry()
    lines = registry.help_lines()
    assert any("yunru ping" in line for line in lines)


def test_help_command_does_not_list_itself() -> None:
    """帮助本身的用法在头段，不该在命令清单里重复。"""

    assert HelpCommand().help_text() == ""
    assert PingCommand().help_text() != ""
