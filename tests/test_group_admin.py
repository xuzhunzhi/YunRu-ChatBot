"""群管理（`/super kick|ban|unban|mute|unmute|recall`）。

三条硬边界（用户 2026-09-30 明确："显然只有 /super 权限可以"，随后要求
"stage4 的内容都用插件实现"）：

1. **只有超管能用**——命令走插件的 `min_level="super"` 档位，判定仍在核心
   （`_plugin_level_allowed`），群管理员那套（`/admin`，按群授权）碰不到；
2. **闸门单独列名**——`capabilities.py` 里新开 `purpose="group_manage"`，只批四个 action
   （禁言/全员禁言/撤回/移出）。`set_group_leave`（退群自毁）与 `set_group_admin`（提权）
   **不在**名单里，而 `purpose="admin"` 那道闸门并没有被放宽。
3. **命令做成插件**（`builtin_group_commands.py`）：插件只解析并返回 `ActionRequest`，
   执行、权限、护栏、审计都在核心——插件拿不到 transport，也拿不到权限名单。
"""
import asyncio
import itertools

from qq_roleplay_bot.builtin_group_commands import parse_group_action
from qq_roleplay_bot.capabilities import CapabilityDenied, CapabilityRegistry
from qq_roleplay_bot.command_plugins import ActionRequest, level_of
from qq_roleplay_bot.group_admin import (DEFAULT_BAN_MINUTES, MAX_BAN_MINUTES, clamp_minutes,
                                         execute)
from qq_roleplay_bot.stage3_main import DialogueEngine, parse_super_command
from qq_roleplay_bot.transport import IncomingMessage, MessageTarget

GROUP = "717151356"
SUPER = "900000001"
ADMIN = "1111111111"
MEMBER = "242003347"


class _Transport:
    """假传输：记下每一次 call_api。"""

    def __init__(self, response=None):
        self.calls = []
        self.response = response if response is not None else {"status": "ok", "retcode": 0}

    async def call_api(self, action, params=None):
        self.calls.append((action, params))
        return self.response


class _NeverCalled:
    async def complete(self, request):
        raise AssertionError("群管理命令不该调用模型")


_SEQ = itertools.count()


def message(text, *, user_id=SUPER, group=GROUP, mentions=(), reply_to=""):
    # message_id 必须**每次不同**：引擎有去重器，同 id 的第二条会被当成重复丢弃，
    # 测试就会拿到 None（踩过一次，别再用文本拼 id）。
    return IncomingMessage(
        message_id=f"gm-{next(_SEQ)}",
        session_id=f"group:{group}",
        user_id=user_id,
        text=text,
        target=MessageTarget(group_id=group),
        mentioned_user_ids=tuple(mentions),
        reply_to_message_id=reply_to,
    )


def engine_with(transport, *, group_admins=(), super_ids=(SUPER,)):
    engine = DialogueEngine(
        _NeverCalled(),
        super_admin_user_ids=frozenset(super_ids),
        admin_user_ids=frozenset(super_ids),
    )
    engine.transport = transport
    for group in group_admins:
        engine.group_admin_ids.setdefault(group, set()).add(ADMIN)
    return engine


# --- 解析 -----------------------------------------------------------------

def test_group_commands_are_plugins_now() -> None:
    """群管理命令现在由插件认领（不再是核心的 `SuperAction`）。

    用户 2026-09-30："我之前说过 stage4 的内容都用插件实现，你好像直接加到 super 的底层里去了"。
    所以 `parse_super_command` 对这些文本**返回 None**（核心不再认识它们的字面形状），
    认领它们的是 `GroupManageCommand`，档位 `super`。
    """

    from qq_roleplay_bot.builtin_commands import build_command_registry

    registry = build_command_registry()
    plugin = registry.resolve(message("/super ban @某人 30", mentions=(MEMBER,)))
    assert plugin is not None and getattr(plugin, "name", "") == "group_manage"
    assert level_of(plugin) == "super"
    # 核心那套解析已经不认它了：这样才叫"搬出去了"
    for text in ("/super kick @某人", "/super ban @某人 30", "/super mute", "/super 撤回"):
        assert parse_super_command(text) is None, text
    # 参数抽取：@ 段不在正文里，所以 @ 全靠 mentioned_user_ids；号码则从正文认
    assert parse_group_action("/super ban 123456789 30") == ("ban", "123456789", "30")
    assert parse_group_action("/super ban 123456789") == ("ban", "123456789", "")
    assert parse_group_action("/super ban @某人 30") == ("ban", "", "30")
    assert parse_group_action("/super ban 30") == ("ban", "", "30")
    assert parse_group_action("/super kick") == ("kick", "", "")
    assert parse_group_action("/super profile @x") is None
    assert parse_group_action("/super restart") is None
    # 组合词不能变成合法命令
    assert parse_super_command("/super restart kick") is None
    # 动作后面跟了看不懂的词：仍然算群管理命令，但执行层会给"用法"提示
    # （比"解析不出来 → 掉进普通聊天让模型接一句"要好）
    assert parse_group_action("/super kick now") == ("kick", "", "")


def test_plugin_only_declares_the_intent() -> None:
    """插件返回的是意图（`ActionRequest`），不是执行结果：它手上没有 transport。"""

    from qq_roleplay_bot.builtin_group_commands import GroupManageCommand

    request = asyncio.run(GroupManageCommand().handle(
        message("/super ban @某人 30", mentions=(MEMBER,))))
    assert isinstance(request, ActionRequest)
    assert (request.group, request.kind) == ("group_admin", "ban")
    assert request.target_id == MEMBER and request.text == "30" and request.mentioned is True


def test_ban_minutes_are_clamped() -> None:
    assert clamp_minutes(None) == DEFAULT_BAN_MINUTES
    assert clamp_minutes("30") == 30
    assert clamp_minutes("0") == DEFAULT_BAN_MINUTES
    assert clamp_minutes("999999") == MAX_BAN_MINUTES
    assert clamp_minutes("abc") == DEFAULT_BAN_MINUTES


# --- 权限：只有超管 -------------------------------------------------------

def test_only_super_admin_can_use_group_management() -> None:
    async def run() -> None:
        transport = _Transport()
        engine = engine_with(transport, group_admins=(GROUP,))
        # 本群管理员发同样的命令：什么都没有（`/super` 那一层不放他进来）
        assert await engine.handle(message("/super ban @某人 10", user_id=ADMIN,
                                           mentions=(MEMBER,))) is None
        assert transport.calls == []
        # 普通成员更不用说了
        assert await engine.handle(message("/super kick @某人", user_id=MEMBER,
                                           mentions=(MEMBER,))) is None
        assert transport.calls == []
        # 超管：执行
        result = await engine.handle(message("/super ban @某人 10", mentions=(MEMBER,)))
        assert result is not None and "已禁言" in result.text
        assert transport.calls[0][0] == "set_group_ban"
        assert transport.calls[0][1] == {"group_id": int(GROUP), "user_id": int(MEMBER),
                                         "duration": 600}

    asyncio.run(run())


def test_group_management_needs_a_group() -> None:
    """超管命令是全局的，但群管理不是：私聊里没有可管的群。"""

    async def run() -> None:
        transport = _Transport()
        engine = engine_with(transport)
        private = IncomingMessage("p1", f"private:{SUPER}", SUPER, "/super mute",
                                 MessageTarget(user_id=SUPER))
        result = await engine.handle(private)
        assert result is not None and "群里" in result.text
        assert transport.calls == []

    asyncio.run(run())


# --- 目标护栏 -------------------------------------------------------------

def test_never_targets_admins_or_the_bot_itself() -> None:
    async def run() -> None:
        transport = _Transport()
        engine = engine_with(transport, group_admins=(GROUP,))
        # 对本群管理员动手 → 拒绝
        blocked = await engine.handle(message("/super kick @某人", mentions=(ADMIN,)))
        assert blocked is not None and "不动他" in blocked.text
        # 对超管自己动手 → 拒绝
        blocked = await engine.handle(message("/super ban @某人 10", mentions=(SUPER,)))
        assert blocked is not None and "不动他" in blocked.text
        assert transport.calls == []

    asyncio.run(run())


def test_kick_requires_an_at_mention() -> None:
    """手滑发个群号就把人踢了，是这条路上最典型的失误——所以踢人必须 @ 到人。"""

    async def run() -> None:
        transport = _Transport()
        engine = engine_with(transport)
        typed = await engine.handle(message(f"/super kick {MEMBER}"))
        assert typed is not None and "@" in typed.text
        assert transport.calls == []
        # 禁言允许直接写号码（可逆，且常用来处理刷屏的陌生号）
        banned = await engine.handle(message(f"/super ban {MEMBER} 5"))
        assert banned is not None and "已禁言" in banned.text
        assert transport.calls[0][1]["duration"] == 300

    asyncio.run(run())


def test_recall_needs_a_quoted_message() -> None:
    async def run() -> None:
        transport = _Transport()
        engine = engine_with(transport)
        missing = await engine.handle(message("/super recall"))
        assert missing is not None and "引用" in missing.text
        quoted = await engine.handle(message("/super recall", reply_to="42"))
        assert quoted is not None and "已撤回" in quoted.text
        assert transport.calls[0] == ("delete_msg", {"message_id": 42})

    asyncio.run(run())


def test_whole_ban_and_unban_params() -> None:
    async def run() -> None:
        transport = _Transport()
        engine = engine_with(transport)
        await engine.handle(message("/super mute"))
        await engine.handle(message("/super unmute"))
        await engine.handle(message(f"/super unban {MEMBER}"))
        assert transport.calls[0] == ("set_group_whole_ban",
                                      {"group_id": int(GROUP), "enable": True})
        assert transport.calls[1] == ("set_group_whole_ban",
                                      {"group_id": int(GROUP), "enable": False})
        assert transport.calls[2] == ("set_group_ban",
                                      {"group_id": int(GROUP), "user_id": int(MEMBER),
                                       "duration": 0})

    asyncio.run(run())


# --- 闸门与失败路径 -------------------------------------------------------

def test_gate_only_allows_the_short_list() -> None:
    registry = CapabilityRegistry()
    for name in ("set_group_ban", "set_group_whole_ban", "delete_msg", "set_group_kick"):
        registry.check(name, purpose="group_manage")
    for name in ("set_group_leave", "set_group_admin", "set_group_name", "send_group_msg"):
        try:
            registry.check(name, purpose="group_manage")
        except CapabilityDenied:
            continue
        raise AssertionError(f"{name} 不该被群管理用途放行")
    # 没有放宽原来的 admin 用途
    try:
        registry.check("set_group_kick", purpose="admin")
    except CapabilityDenied:
        pass
    else:  # pragma: no cover
        raise AssertionError("admin 用途仍然必须排除敏感写操作")


def test_execute_reports_rejections_and_failures_as_text() -> None:
    async def run() -> None:
        denied = await execute("ban", transport=_Transport(), registry=CapabilityRegistry(),
                               group_id=GROUP, actor_id=SUPER, target_id=MEMBER,
                               enabled=False)
        assert "没开" in denied
        rejected = await execute(
            "ban", transport=_Transport({"status": "failed", "retcode": 100}),
            registry=CapabilityRegistry(), group_id=GROUP, actor_id=SUPER, target_id=MEMBER)
        assert "拒绝" in rejected
        failed = await execute(
            "mute", transport=_Transport(), registry=CapabilityRegistry(),
            group_id=GROUP, actor_id=SUPER)
        assert "已开启" in failed

        class Boom:
            async def call_api(self, action, params=None):
                raise RuntimeError("连接断了")

        broken = await execute("mute", transport=Boom(), registry=CapabilityRegistry(),
                              group_id=GROUP, actor_id=SUPER)
        assert "失败" in broken

    asyncio.run(run())


def test_unknown_or_missing_target_gives_usage() -> None:
    async def run() -> None:
        usage = await execute("ban", transport=_Transport(), registry=CapabilityRegistry(),
                              group_id=GROUP, actor_id=SUPER, target_id="")
        assert "用法" in usage
        unknown = await execute("explode", transport=_Transport(),
                                registry=CapabilityRegistry(), group_id=GROUP, actor_id=SUPER)
        assert "用法" in unknown

    asyncio.run(run())
