"""Stage 4 的命令走插件：档位声明 + 动作意图，判定与执行留在核心。

由来（2026-09-30 用户）："我之前说过 stage4 的内容都用插件实现，你好像直接加到 super 的
底层里去了"。原来群管理与群主命令的解析/分发写在核心 `stage3_main.py` 里
（`SUPER_GROUP_PATTERN` + `_group_action_reply` 那一路）。现在：

- **插件**（`builtin_group_commands.py`）只做两件事：认出命令、把文字解析成 `ActionRequest`；
- **核心**判档位（`min_level`）、查她是不是群主、走 `capabilities` 闸门、记审计、真调 action；
- 插件**拿不到 `transport`、拿不到权限名单**——所以这一族命令在插件里"看起来什么都能做"，
  实际上核心会拦。

这个文件钉的就是这三条边界，以及"搬出去之后核心不再认识那些字面形状"。
"""
import asyncio

from qq_roleplay_bot.builtin_commands import build_command_registry
from qq_roleplay_bot.command_plugins import ActionRequest, level_of
from qq_roleplay_bot.qq_roles import ROLE_MEMBER, ROLE_OWNER, SelfRoleCache
from qq_roleplay_bot.stage3_main import DialogueEngine, parse_super_command
from qq_roleplay_bot.transport import IncomingMessage, MessageTarget

GROUP = "717151356"
SUPER = "900000001"
ADMIN = "1111111111"
MEMBER = "242003347"


class NeverCalled:
    """命令路径不该碰模型；碰了就直接炸，测试立刻发现。"""

    async def complete(self, request):  # pragma: no cover
        raise AssertionError("命令不该调用模型")


class FakeTransport:
    def __init__(self, response=None):
        self.calls: list[tuple[str, dict]] = []
        self.response = response if response is not None else {"status": "ok", "retcode": 0}

    async def call_api(self, action, params=None):
        self.calls.append((action, dict(params or {})))
        return self.response


class RoleClient:
    def __init__(self, role: str):
        self.role = role

    async def call(self, action, params=None):
        if action == "get_login_info":
            return {"user_id": "900000002"}
        if action == "get_group_member_info":
            return {"role": self.role}
        return {}


def message(text: str, *, user_id: str = SUPER, mentions=()) -> IncomingMessage:
    return IncomingMessage(
        message_id=f"m:{text}:{user_id}:{mentions}", session_id=f"group:{GROUP}", user_id=user_id,
        text=text, target=MessageTarget(group_id=GROUP), mentioned_user_ids=tuple(mentions),
    )


def engine_with(transport, *, role: str = ROLE_OWNER) -> DialogueEngine:
    engine = DialogueEngine(NeverCalled(), super_admin_user_ids=frozenset({SUPER}),
                            admin_user_ids=frozenset({SUPER}))
    engine.transport = transport
    engine.self_roles = SelfRoleCache(RoleClient(role))
    return engine


# --- 插件认领 + 核心不再认识 -------------------------------------------------

def test_stage4_commands_are_plugins_with_the_super_level() -> None:
    registry = build_command_registry()
    for text, name in (("/super kick @某人", "group_manage"),
                       ("/super ban @某人 30", "group_manage"),
                       ("/super recall", "group_manage"),
                       ("/super qqadmin", "group_owner"),
                       ("/super notice 今晚维护", "group_owner"),
                       ("/super groupname 新名字", "group_owner")):
        plugin = registry.resolve(message(text, mentions=(MEMBER,)))
        assert plugin is not None, text
        assert getattr(plugin, "name", "") == name, text
        assert level_of(plugin) == "super", text


def test_core_no_longer_knows_those_commands() -> None:
    """搬出去的证据：核心的 `parse_super_command` 对它们返回 None。

    否则就是"两边都认"，将来一定会漂移。
    """

    for text in ("/super kick @某人", "/super ban @某人 30", "/super qqadmin",
                 "/super notice 今晚维护", "/super groupname 新名字"):
        assert parse_super_command(text) is None, text
    # 仍然属于核心的 `/super` 命令没被误伤
    assert parse_super_command("/super status") is not None
    assert parse_super_command("/super restart") is not None


def test_unknown_level_falls_back_to_public() -> None:
    class Odd:
        min_level = "root"

    class Bare:
        pass

    assert level_of(Odd()) == "public"
    assert level_of(Bare()) == "public"
    assert level_of(object()) == "public"


# --- 档位判定在核心 ---------------------------------------------------------

def test_non_super_gets_nothing_not_even_a_hint() -> None:
    """群管理员/普通成员发 `/super ...`：不回复、不报错、不调 action、不交给模型。"""

    async def run() -> None:
        for user in (ADMIN, MEMBER):
            transport = FakeTransport()
            engine = engine_with(transport)
            assert await engine.handle(message("/super ban @某人 10", user_id=user,
                                              mentions=(MEMBER,))) is None
            assert transport.calls == []
    asyncio.run(run())


def test_super_admin_goes_through_the_core_to_the_action() -> None:
    async def run() -> None:
        transport = FakeTransport()
        engine = engine_with(transport)
        result = await engine.handle(message("/super ban @某人 10", mentions=(MEMBER,)))
        assert result is not None and "已禁言" in result.text
        assert transport.calls == [("set_group_ban", {"group_id": int(GROUP),
                                                     "user_id": int(MEMBER), "duration": 600})]
    asyncio.run(run())


def test_owner_action_checks_her_role_in_the_core() -> None:
    """她不是群主时，插件照样"声明了意图"，但核心会拦下来。"""

    async def run() -> None:
        transport = FakeTransport()
        engine = engine_with(transport, role=ROLE_MEMBER)
        result = await engine.handle(message("/super qqadmin", mentions=(MEMBER,)))
        assert result is not None and "做不了" in result.text
        assert transport.calls == []

        transport = FakeTransport()
        engine = engine_with(transport, role=ROLE_OWNER)
        result = await engine.handle(message("/super qqadmin", mentions=(MEMBER,)))
        assert result is not None and "已设置" in result.text
        assert transport.calls == [("set_group_admin", {"group_id": int(GROUP),
                                                       "user_id": int(MEMBER), "enable": True})]
    asyncio.run(run())


def test_unknown_action_group_is_refused_by_the_core() -> None:
    async def run() -> None:
        transport = FakeTransport()
        engine = engine_with(transport)
        result = await engine._plugin_action_reply(
            ActionRequest(group="没有这条通道", kind="kick"), message("/super kick"))
        assert "没有对应的执行通道" in result.text
        assert transport.calls == []
    asyncio.run(run())


# --- 插件拿不到东西 ---------------------------------------------------------

def test_plugins_hold_no_transport_and_no_permission_lists() -> None:
    registry = build_command_registry()
    for plugin in registry.plugins:
        for forbidden in ("transport", "admin_user_ids", "super_admin_user_ids",
                          "group_admin_ids", "self_roles", "capabilities"):
            assert not hasattr(plugin, forbidden), f"{getattr(plugin, 'name', '?')}.{forbidden}"


def test_plugin_handle_has_no_side_effects() -> None:
    """`handle()` 只产出意图：不调 action、不改任何状态。"""

    registry = build_command_registry()
    plugin = registry.resolve(message("/super kick @某人", mentions=(MEMBER,)))
    request = asyncio.run(plugin.handle(message("/super kick @某人", mentions=(MEMBER,))))
    assert isinstance(request, ActionRequest)
    assert (request.group, request.kind, request.target_id) == ("group_admin", "kick", MEMBER)
    assert request.mentioned is True


# --- 头衔：公开的"自己给自己设"（2026-09-30 用户要求）------------------------

def test_title_is_a_public_command_for_yourself() -> None:
    """`/title 龙王` 不需要任何权限，目标写死成发起人自己。

    用户原话："头衔那个不用 /super 权限，改成 title+头衔 即可，自己给自己申请"。
    """

    from qq_roleplay_bot.builtin_group_commands import parse_title_command

    registry = build_command_registry()
    plugin = registry.resolve(message("/title 龙王", user_id=MEMBER))
    assert plugin is not None and getattr(plugin, "name", "") == "title"
    assert level_of(plugin) == "public", "头衔不该要权限"
    request = asyncio.run(plugin.handle(message("/title 龙王", user_id=MEMBER)))
    assert isinstance(request, ActionRequest)
    assert (request.group, request.kind) == ("group_owner", "title")
    assert request.target_id == MEMBER, "只能改自己"
    assert request.text == "龙王" and request.mentioned is False

    # 三种写法都认；裸 `title 龙王` 与 `/titles` 不认（免得吞掉日常聊天）
    assert parse_title_command("/title 龙王") == "龙王"
    assert parse_title_command("#title 龙王") == "龙王"
    assert parse_title_command("/yunru title 龙王") == "龙王"
    assert parse_title_command("title 龙王") is None
    assert parse_title_command("/titles 龙王") is None
    assert parse_title_command("今天的 title 很有意思") is None
    assert parse_title_command("/super title @某人 龙王") is None
    # 不给头衔正文 → 回一句用法，不发动作
    assert "用法" in asyncio.run(plugin.handle(message("/title", user_id=MEMBER)))


def test_multi_line_owner_text_is_recognised() -> None:
    """群公告正文是多行的，正则必须跨得过换行。

    真机踩过（2026-09-30 用户："发公告发不出去"）：`.*?` 默认不匹配换行，
    于是 `/super notice 第一行\\n第二行` **没被插件认领**，掉进对话路径又被安全过滤拦下，
    而那条拦截的回复是空串——用户看到的是"发不出去，还一个字都不回"。
    """

    from qq_roleplay_bot.builtin_group_commands import GroupOwnerCommand, parse_owner_action

    text = "/super notice 第一行正文\n第二行正文\n\n第四行（空行）"
    assert parse_owner_action(text) == ("notice", "第一行正文\n第二行正文\n\n第四行（空行）")
    plugin = GroupOwnerCommand()
    message_ = message(text)
    assert plugin.match(message_) is True
    request = asyncio.run(plugin.handle(message_))
    assert isinstance(request, ActionRequest)
    assert request.kind == "notice" and "\n" in request.text


def test_multi_line_notice_reaches_the_action() -> None:
    """正文里的换行要原样送进 action（群公告支持多行）。"""

    async def run():
        transport = FakeTransport()
        engine = engine_with(transport, role=ROLE_OWNER)
        return transport, await engine.handle(
            message("/super notice 第一行\n第二行", user_id=SUPER))

    transport, result = asyncio.run(run())
    assert result is not None and "已发出" in result.text
    assert transport.calls == [("_send_group_notice",
                               {"group_id": int(GROUP), "content": "第一行\n第二行"})]


# --- 认不出来的命令不能"消失" ------------------------------------------------

def test_unrecognised_super_command_gets_a_usage_reply() -> None:
    """有权限、但命令打错/不存在 → 回一句用法。

    以前这种文本会继续往下走：先进安全过滤，命中就**静默**丢掉（回复是空串），
    用户那头表现成"命令发出去什么都没有"（真机 2026-09-30 就是这么报的）。
    命令前缀就是命令——认不出来也要明确收尾，不进模型、不进安全判定。
    """

    async def run(text: str, user_id: str = SUPER):
        transport = FakeTransport()
        engine = engine_with(transport)
        return engine, transport, await engine.handle(message(text, user_id=user_id))

    # 超管：打错的 /super 命令 → 用法提示（注意 `/super notice` 少了正文是**认出来**的，
    # 那种回的是那条命令自己的用法；这里要的是"根本不存在/打错"的那种）
    engine, transport, result = asyncio.run(run("/super 随便写点什么"))
    assert result is not None and "没认出来" in result.text and "/super help" in result.text
    assert transport.calls == []
    assert engine.snapshot().blocked_messages == 0, "命令不该被安全过滤算进阻断"

    # 管理员：打错的 /admin 命令 → 也是用法提示
    engine2 = engine_with(FakeTransport())
    engine2.group_admin_ids.setdefault(GROUP, set()).add(ADMIN)
    result = asyncio.run(engine2.handle(message("/admin 不存在的动作", user_id=ADMIN)))
    assert result is not None and "/admin help" in result.text

    # 不是超管的人发 /super ... 仍然完全静默（不暴露那一层）
    engine3, transport3, silent = asyncio.run(run("/super 不存在的命令", user_id=MEMBER))
    assert silent is None and transport3.calls == []


def test_sensitive_words_do_not_swallow_a_command() -> None:
    """命令正文里出现"本机/文件/数据"这类词，不该被安全过滤吃掉。

    这条是真机故障的完整复现：同样的词放在**聊天**里应该拦，
    放在 `/super notice` 后面（合法命令）必须照发。
    """

    async def run(text: str, user_id: str = SUPER):
        transport = FakeTransport()
        engine = engine_with(transport, role=ROLE_OWNER)
        return engine, transport, await engine.handle(message(text, user_id=user_id))

    notice = "/super notice 明天凌晨维护：本机数据会短暂不可用，请查看群文件里的说明。"
    engine, transport, result = asyncio.run(run(notice))
    assert result is not None and "已发出" in result.text
    assert transport.calls and transport.calls[0][0] == "_send_group_notice"
    assert engine.snapshot().blocked_messages == 0


def test_title_executes_only_where_she_is_owner() -> None:
    """能不能真改，取决于**她在那个群是不是群主**——不是群主就回一句"做不了"。"""

    async def run(role: str):
        transport = FakeTransport()
        engine = engine_with(transport, role=role)
        result = await engine.handle(message("/title 龙王", user_id=MEMBER))
        return transport, result

    transport, result = asyncio.run(run(ROLE_OWNER))
    assert result is not None and "已给" in result.text
    # 第一条是设置本身；之后会**回读一次**核对对面到底存了什么（QQ 会静默截断头衔）
    assert transport.calls[0] == ("set_group_special_title",
                                  {"group_id": int(GROUP), "user_id": int(MEMBER),
                                   "special_title": "龙王"})

    transport, result = asyncio.run(run(ROLE_MEMBER))
    assert result is not None and "做不了" in result.text
    assert transport.calls == []
