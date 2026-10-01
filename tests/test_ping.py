"""公开命令：ping 与 /help。

ping 只保留两种写法（用户 2026-09-27 定的）：
`/yunru ping` 与「@ 她 + ping」。从前那套裸 ping / /ping / !ping / ping? / yunru 在吗
全部去掉——它们既容易误吞聊天里的 ping，也让命令面看起来比实际复杂。
"""
import asyncio

from qq_roleplay_bot.builtin_commands import is_ping_command
from qq_roleplay_bot.stage3_main import (
    ADMIN_HELP,
    PING_REPLY,
    PUBLIC_HELP,
    DialogueEngine,
    is_help_command,
    looks_like_ping_command,
)
from qq_roleplay_bot.transport import IncomingMessage, MessageTarget


class NeverCalled:
    async def complete(self, request):
        raise AssertionError("ping/help must not call the model")


def message(message_id, text, target, *, user_id="100", is_bot_message=False,
            mentioned=False):
    return IncomingMessage(
        message_id=message_id,
        session_id=f"group:{target.group_id}" if target.group_id else f"private:{target.user_id}",
        user_id=user_id,
        text=text,
        target=target,
        is_bot_message=is_bot_message,
        is_bot_mentioned=mentioned,
    )


# --- ping 的两种写法 --------------------------------------------------------


def test_slash_yunru_ping_is_accepted() -> None:
    assert is_ping_command(message("1", "/yunru ping", MessageTarget(group_id="1")))
    assert is_ping_command(message("2", "  /YunRu   Ping  ", MessageTarget(group_id="1")))


def test_mention_with_ping_is_accepted() -> None:
    """`@YunRu ping`：NapCat 会把 at 段摘掉，所以看 is_bot_mentioned。"""

    assert is_ping_command(message("1", "ping", MessageTarget(group_id="1"), mentioned=True))
    assert is_ping_command(message("2", "  Ping  ", MessageTarget(group_id="1"), mentioned=True))
    # 只是提到她、并没有说 ping，那不是命令。
    assert not is_ping_command(message("3", "ping 服务器", MessageTarget(group_id="1"), mentioned=True))


def test_every_other_ping_spelling_is_gone() -> None:
    """旧的"容错写法"必须全部失效，否则命令面又变成一团。"""

    for text in ("yunru ping", "/ping", "!ping", "#ping", "PING?", "ping。",
                 "yunru 在吗", "云茹 ping", "yunru ping!", "ping"):
        assert not is_ping_command(message("x", text, MessageTarget(group_id="1"))), text


def test_ping_like_detection_only_for_diagnostics() -> None:
    """`looks_like_ping_command` 只用于日志提示，不参与匹配。"""

    assert looks_like_ping_command("yunru ping 一下")
    assert looks_like_ping_command("/ping 服务器")
    assert not looks_like_ping_command("你好")


def test_slash_ping_reaches_any_group_even_when_not_enabled() -> None:
    target = MessageTarget(group_id="999999999")
    result = asyncio.run(DialogueEngine(NeverCalled()).handle(message("g1", "/yunru ping", target)))
    assert (result.target, result.text) == (target, PING_REPLY)


def test_private_ping_does_not_require_private_debug_allowlist() -> None:
    target = MessageTarget(user_id="987654321")
    result = asyncio.run(DialogueEngine(NeverCalled()).handle(message("p1", "/yunru ping", target)))
    assert (result.target, result.text) == (target, "pong")


def test_ping_still_works_when_stage3_is_disabled_but_not_for_bot_messages() -> None:
    target = MessageTarget(group_id="717151356")
    engine = DialogueEngine(NeverCalled())
    engine.set_enabled(False)
    disabled = asyncio.run(engine.handle(message("disabled", "/yunru ping", target)))
    assert (disabled.target, disabled.text) == (target, "pong")
    assert asyncio.run(engine.handle(
        message("bot", "/yunru ping", target, is_bot_message=True)
    )) is None


def test_duplicate_ping_is_suppressed() -> None:
    target = MessageTarget(group_id="999999999")
    engine = DialogueEngine(NeverCalled())
    first = asyncio.run(engine.handle(message("same", "/yunru ping", target)))
    assert (first.target, first.text) == (target, "pong")
    assert asyncio.run(engine.handle(message("same", "/yunru ping", target))) is None


# --- /help -----------------------------------------------------------------


def test_help_matching() -> None:
    """`yunru` 加不加、斜杠加不加，都算同一个帮助命令。

    用户 2026-09-28：「我总记不得到底要不要加 yunru」——`/yunru help` 从前**不匹配**，
    会掉进普通聊天让她回一句无关的话，这是最坏的一种"记错就失灵"。
    """

    for text in ("/help", "/HELP", "  /help  ", "#help", "/ yunru",
                 "/yunru", "#yunru", "yunru help", "yunru 帮助", "YUNRU HELP",
                 "/yunru help", "/yunru 帮助", "#yunru help", "# yunru  帮助"):
        assert is_help_command(text), text
    # 底线：裸 `help` / `yunru` 不是命令（"help me"、"请帮助我" 是聊天）。
    for text in ("help", "yunru", "/help me", "/helps", "请帮助我",
                 "yunru helps", "云茹 help", "/yunru help me", "/yunru ping"):
        assert not is_help_command(text), text


def test_help_works_in_any_session_and_off_enabled_group() -> None:
    """帮助是公开命令：未启用群、私聊、disabled 状态下都应可用。"""

    engine = DialogueEngine(NeverCalled())
    engine.set_enabled(False)

    other_group = MessageTarget(group_id="999999999")
    result = asyncio.run(engine.handle(message("h1", "/help", other_group)))
    assert (result.target, result.text) == (other_group, PUBLIC_HELP)

    private = MessageTarget(user_id="987654321")
    result = asyncio.run(engine.handle(message("h2", "/help", private)))
    assert (result.target, result.text) == (private, PUBLIC_HELP)


def test_help_does_not_call_model_and_is_deduplicated() -> None:
    target = MessageTarget(group_id="717151356")
    engine = DialogueEngine(NeverCalled())
    first = asyncio.run(engine.handle(message("h", "/help", target)))
    assert first.text == PUBLIC_HELP
    assert asyncio.run(engine.handle(message("h", "/help", target))) is None


def test_admin_help_lists_the_slash_admin_commands() -> None:
    target = MessageTarget(group_id="717151356")
    engine = DialogueEngine(NeverCalled())
    result = asyncio.run(engine.handle(message("p", "/admin help", target, user_id="900000001")))
    assert result.text == ADMIN_HELP
    # 说明里必须真的包含命令本身，否则用户照着做会发现命令不存在。
    for token in ("/admin enable", "/admin status", "/admin confirm"):
        assert token in ADMIN_HELP, token
    # 旧前缀不许再出现在帮助里。
    assert "#bot" not in ADMIN_HELP
    assert "/yunru ping" in PUBLIC_HELP
    # 公开帮助不能泄漏管理员命令清单。
    assert "/admin enable" not in PUBLIC_HELP


def test_every_help_alias_returns_the_same_public_help() -> None:
    """写错的代价要小：这些写法**都**该拿到同一份公开帮助（而不是被当成聊天）。"""

    engine = DialogueEngine(NeverCalled())
    target = MessageTarget(group_id="717151356")
    for index, text in enumerate(("/help", "/yunru", "yunru help", "/yunru help",
                                  "#yunru 帮助", "/ yunru", "YUNRU HELP")):
        result = asyncio.run(engine.handle(message(f"alias{index}", text, target)))
        assert result is not None, text
        assert result.text == PUBLIC_HELP, text


def test_public_help_points_at_the_admin_menu_and_the_permit_route() -> None:
    """用户 2026-09-28：yunru help 里要写明"普通成员能调出 /admin help 菜单"，
    以及"要用菜单里的某一条，就让超管 /super permit 放行一次"。"""

    assert "/admin help" in PUBLIC_HELP
    assert "/super permit" in PUBLIC_HELP
    assert "谁都能看" in PUBLIC_HELP
    # 只点名入口，不把菜单本身搬进公开帮助。
    for token in ("/admin enable", "/admin relay", "/admin echo"):
        assert token not in PUBLIC_HELP, token
