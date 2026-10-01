"""管理员命令：`/admin` 前缀、群聊里都能用、`/super addadmin|deladmin|admin list`。

命令面的四条规矩（用户 2026-09-27 定的）：
1. **管理员是群聊里的角色**：`/admin` 命令在群里都能生效（跟群的启停无关），
   私聊里不生效；
2. **超管是全局身份**：`/super` 命令在群里、私聊都一样，不看群状态；
3. 管理员命令统一用 `/admin` 前缀，旧的 `#bot` 不再识别；
4. 超管可以用 `/super addadmin @某人` / `/super deladmin @某人` 增删管理员，
   @ 段是唯一的身份来源，`/super admin list` 看名单。
"""
import asyncio

from qq_roleplay_bot.admin_control import AdminCommandKind, parse_admin_command
from qq_roleplay_bot.stage3_main import (
    OneBotRelayTargetResolver,
    RelayDelivery,
    RelayTargetCandidate,
    DialogueEngine,
    _deliver_relay,
)
from qq_roleplay_bot.transport import IncomingMessage, MessageTarget

GROUP = "717151356"
GROUP1 = "717151356"
GROUP2 = "888888888"
CONTROL_GROUP = MessageTarget(group_id=GROUP)
ADMIN = "900000001"
GRANTEE = "10086"


def message(message_id: str, text: str, *, user_id: str = ADMIN, target=CONTROL_GROUP,
            mentioned: tuple[str, ...] = (), reply_to: str = ""):
    return IncomingMessage(
        message_id=message_id,
        session_id=f"group:{target.group_id}" if target.group_id else f"private:{user_id}",
        user_id=user_id,
        text=text,
        target=target,
        mentioned_user_ids=mentioned,
        reply_to_message_id=reply_to,
    )


class NeverCalled:
    async def complete(self, request):
        raise AssertionError("命令不该调用模型")


# --- 前缀与解析 -------------------------------------------------------------


def test_admin_commands_use_the_slash_admin_prefix() -> None:
    enabled = parse_admin_command("/admin enable")
    status = parse_admin_command("/admin status")
    help_command = parse_admin_command("/admin help")
    assert enabled is not None and enabled.kind is AdminCommandKind.ENABLE
    assert not enabled.group_id, "启停不再接受群号"
    assert status is not None and status.kind is AdminCommandKind.STATUS
    assert help_command is not None and help_command.kind is AdminCommandKind.HELP


def test_group_management_commands_are_parsed_without_a_group_id() -> None:
    """`/admin enable|disable|clear` 只管发命令的这个群，所以**不带群号**。"""

    for text, kind in (
        ("/admin enable", AdminCommandKind.ENABLE),
        ("/admin 开启", AdminCommandKind.ENABLE),
        ("/admin disable", AdminCommandKind.DISABLE),
        ("/admin 关闭", AdminCommandKind.DISABLE),
        ("/admin clear", AdminCommandKind.CLEAR),
        ("/admin 清理", AdminCommandKind.CLEAR),
    ):
        parsed = parse_admin_command(text)
        assert parsed is not None and parsed.kind is kind, text
        assert not parsed.group_id, text


def test_legacy_numbered_form_still_parses_so_it_can_be_explained() -> None:
    """带群号的旧写法仍然解析得出来——上层要回一句"写法变了"。

    直接解析失败会让这条消息掉进普通聊天，被模型接一句莫名其妙的话。
    """

    legacy = parse_admin_command("/admin disable 123456789")
    assert legacy is not None and legacy.kind is AdminCommandKind.DISABLE
    assert legacy.group_id == "123456789"


def test_old_hash_bot_prefix_is_no_longer_recognised() -> None:
    """旧前缀必须彻底失效，否则文档与用户记忆里永远有一份是过时的。"""

    for text in ("#bot enable 123456789", "#bot status", "#bot help",
                 "#bot relay group 123456789 你好", "bot enable 123456789"):
        assert parse_admin_command(text) is None, text


def test_relay_requires_the_prefix_too() -> None:
    """转告从前可以不带前缀，现在也必须带 `/admin`。"""

    with_prefix = parse_admin_command("/admin relay group 123456789 你好")
    assert with_prefix is not None and with_prefix.kind is AdminCommandKind.RELAY
    assert with_prefix.target_kind == "group"
    assert parse_admin_command("转发 group 123456789 不带前缀") is None


def test_relay_parses_all_target_forms() -> None:
    private = parse_admin_command("/admin relay user 987654321 请联系我")
    chinese = parse_admin_command("/admin 转告 个人 987654321 中文命令")
    group_name = parse_admin_command("/admin relay group_name 测试群 | 群名转告")
    nickname = parse_admin_command("/admin relay nickname 小明 | 昵称转告")
    simple_name = parse_admin_command("/admin 转告 群名 测试群 简单名称写法")
    select = parse_admin_command("/admin select abcdef12 2")
    assert private is not None and private.target_kind == "user"
    assert chinese is not None and chinese.target_kind == "user"
    assert group_name is not None and group_name.target_kind == "group_name"
    assert group_name.target_query == "测试群" and group_name.content == "群名转告"
    assert nickname is not None and nickname.target_kind == "nickname"
    assert simple_name is not None and simple_name.target_kind == "group_name"
    assert simple_name.target_query == "测试群"
    assert select is not None and select.kind is AdminCommandKind.SELECT and select.index == 2


def test_natural_language_process_request_is_no_longer_a_command() -> None:
    """聊天式的"看看 bot 跑着哪些进程"不再被当成超管命令。

    这条启发式从前偏宽：含"进程/程序 + 查看/列出 + bot/本机"的闲聊会被判成超管诊断，
    非超管随后被静默忽略——那句话既不进模型也不回复。现在只有 `/super processes` 算。
    """

    for text in ("查看bot运行环境的其他进程有哪些", "看看本机有哪些程序",
                 "[CQ:at,qq=900000002] 请查看 bot 运行环境中有哪些进程"):
        assert parse_admin_command(text) is None, text


# --- 生效范围：按群授权，跟群的启停无关 -------------------------------------


def test_configured_admin_works_in_any_group() -> None:
    """配置里的名单是部署级的：对每个群都算数，也不再只认控制群。"""

    engine = DialogueEngine(NeverCalled())
    engine.enable_group("123456789")
    other = MessageTarget(group_id="123456789")
    result = asyncio.run(engine.handle(message("a1", "/admin status", target=other)))
    assert result is not None and "123456789" in result.text


def test_admin_command_works_in_a_group_that_is_not_enabled() -> None:
    """群的启停管的是"对话要不要处理"，不是"管理员能不能管这个群"。

    实测踩过：在没打开的群里发命令毫无反应，人得先切到另一个群才敲得动。
    """

    engine = DialogueEngine(NeverCalled())
    other = MessageTarget(group_id="999999999")
    result = asyncio.run(engine.handle(message("a1", "/admin status", target=other)))
    assert result is not None and "启用群聊" in result.text
    # 只是下命令：不会顺手把这个群打开。
    assert "999999999" not in engine.snapshot().enabled_group_ids


def test_admin_command_does_not_work_in_private_chat() -> None:
    """管理员是**群聊里的角色**：群里任命、群里管事，私聊里不生效。

    注意默认引擎里 `ADMIN` 同时也是超管——那个组合在私聊里是生效的
    （超管全局），所以这里必须造一个"只是管理员"的人。配置级名单在任何群都算，
    所以私聊不是"群"，自然不生效。
    """

    plain_admin = "200000000"
    engine = DialogueEngine(
        NeverCalled(),
        admin_user_ids=frozenset({plain_admin}),
        super_admin_user_ids=frozenset({ADMIN}),
    )
    private = MessageTarget(user_id=plain_admin)
    assert asyncio.run(engine.handle(message(
        "a1", "/admin status", user_id=plain_admin, target=private,
    ))) is None
    # 群里可以
    assert asyncio.run(engine.handle(message(
        "a2", "/admin status", user_id=plain_admin,
    ))) is not None


def test_super_admin_command_works_in_private_chat_and_any_group() -> None:
    """超管是**全局身份**：跟"在哪个群"没关系，私聊里也一样。"""

    engine = DialogueEngine(NeverCalled())
    private = MessageTarget(user_id=ADMIN)
    in_private = asyncio.run(engine.handle(message("s1", "/super help", target=private)))
    assert in_private is not None and in_private.text.startswith("超管命令")
    in_strange_group = asyncio.run(engine.handle(message(
        "s2", "/super help", target=MessageTarget(group_id="999999999"),
    )))
    assert in_strange_group is not None and in_strange_group.text.startswith("超管命令")


def test_super_admin_ordinary_private_chat_is_not_swallowed() -> None:
    """回归：把超管折进"管理员判定"之后，他自己的私聊被静默吃掉过一次。

    管理员在没开对话的群里闲聊不处理，但**私聊不是"没开对话的群"**。
    （私聊能否立刻得到回应由 `private_debug_user_ids` 决定，默认是空的——
    这里显式把他加进去，测的是"不该被管理员那条规则吃掉"。）
    """

    class Echo:
        def __init__(self):
            self.requests = []

        async def complete(self, request):
            self.requests.append(request)
            return "<reply>嗯，我在。</reply>"

    client = Echo()
    engine = DialogueEngine(client, private_debug_user_ids=frozenset({ADMIN}))
    private = MessageTarget(user_id=ADMIN)
    result = asyncio.run(engine.handle(message("p1", "在吗", target=private)))
    assert result is not None and result.text == "嗯，我在。"
    assert len(client.requests) == 1


# --- 按群授权：只管被授权的那个群 -------------------------------------------


def _grantee_engine() -> tuple[DialogueEngine, str]:
    """造一个"只在 GROUP1 被授权"的管理员。"""

    engine = DialogueEngine(NeverCalled(), admin_user_ids=frozenset(), super_admin_user_ids=frozenset({ADMIN}))
    asyncio.run(engine.handle(message(
        "g1", "/super addadmin @某人", mentioned=(GRANTEE,), target=MessageTarget(group_id=GROUP1),
    )))
    return engine, GRANTEE


def test_granted_admin_can_only_use_commands_in_the_granted_group() -> None:
    engine, grantee = _grantee_engine()
    inside = asyncio.run(engine.handle(message(
        "a1", "/admin status", user_id=grantee, target=MessageTarget(group_id=GROUP1),
    )))
    assert inside is not None and "启用群聊" in inside.text
    outside = asyncio.run(engine.handle(message(
        "a2", "/admin status", user_id=grantee, target=MessageTarget(group_id=GROUP2),
    )))
    assert outside is None, "没被授权的群：静默丢弃，不能回话也不能交给模型"


def test_granted_admin_cannot_disable_someone_elses_group() -> None:
    """他最怕的那件事：跑别的群把聊天关了。现在连写法都不存在了。"""

    engine, grantee = _grantee_engine()
    engine.enable_group(GROUP2)

    # 路一：在别的群里发命令 —— 那条消息根本不该被当成命令。
    other_group = asyncio.run(engine.handle(message(
        "a1", "/admin disable", user_id=grantee, target=MessageTarget(group_id=GROUP2),
    )))
    assert other_group is None
    assert GROUP2 in engine.enabled_group_ids

    # 路二：在自己被授权的群里点名别人的群 —— 群号参数已经取消，只会得到一句说明。
    cross = asyncio.run(engine.handle(message(
        "a2", f"/admin disable {GROUP2}", user_id=grantee, target=MessageTarget(group_id=GROUP1),
    )))
    assert cross is not None and "群号参数已经去掉" in cross.text
    assert GROUP2 in engine.enabled_group_ids, "点名别的群绝不能生效"
    assert GROUP1 in engine.enabled_group_ids, "也不该顺手把本群关掉"


def test_granted_admin_cannot_relay_into_someone_elses_group() -> None:
    engine, grantee = _grantee_engine()
    blocked = asyncio.run(engine.handle(message(
        "r1", f"/admin relay group {GROUP2} 越界转告", user_id=grantee,
        target=MessageTarget(group_id=GROUP1),
    )))
    assert blocked is not None and "不在里面" in blocked.text
    assert engine.drain_relay_deliveries() == ()
    # 自己管的群可以
    allowed = asyncio.run(engine.handle(message(
        "r2", f"/admin relay group {GROUP1} 本群转告", user_id=grantee,
        target=MessageTarget(group_id=GROUP1),
    )))
    assert allowed is not None and "待确认转告" in allowed.text


def test_granted_admin_can_still_relay_to_a_user() -> None:
    """按 QQ 号的转告不涉及群权限，不该被顺手挡掉。"""

    engine, grantee = _grantee_engine()
    pending = asyncio.run(engine.handle(message(
        "r1", "/admin relay user 987654321 私聊转告", user_id=grantee,
        target=MessageTarget(group_id=GROUP1),
    )))
    assert pending is not None and "待确认转告" in pending.text


def test_super_admin_is_not_limited_by_group_grants() -> None:
    """超管不受按群授权限制：他在被授权的 A 群里也能用管理员命令。

    但"能管哪个群"这条对他**同样**成立——启停只作用于发命令的那个群。
    """

    engine, _ = _grantee_engine()
    engine.enable_group(GROUP2)
    result = asyncio.run(engine.handle(message(
        "s1", "/admin disable", user_id=ADMIN, target=MessageTarget(group_id=GROUP1),
    )))
    assert result is not None and "已关闭" in result.text
    assert GROUP1 not in engine.enabled_group_ids
    assert GROUP2 in engine.enabled_group_ids, "只该关掉他发命令的那个群"


def test_non_admin_cannot_execute_admin_command() -> None:
    engine = DialogueEngine(NeverCalled())
    result = asyncio.run(engine.handle(message("u1", "/admin enable", user_id="100")))
    # 没权限：回一句说明（2026-09-28 用户要求），但**不执行**。
    assert result is not None and "权限" in result.text and "已开启" not in result.text
    assert GROUP in engine.snapshot().enabled_group_ids


def test_admin_can_enable_and_disable_the_current_group_without_model_call() -> None:
    engine = DialogueEngine(NeverCalled())
    other = MessageTarget(group_id=GROUP2)
    enabled = asyncio.run(engine.handle(message("a1", "/admin enable", target=other)))
    assert enabled is not None and GROUP2 in enabled.text
    assert GROUP2 in engine.snapshot().enabled_group_ids

    disabled = asyncio.run(engine.handle(message("a2", "/admin disable", target=other)))
    assert disabled is not None and GROUP2 in disabled.text
    assert GROUP2 not in engine.snapshot().enabled_group_ids

    cleared = asyncio.run(engine.handle(message("a3", "/admin clear", target=other)))
    assert cleared is not None and GROUP2 in cleared.text


def test_group_management_commands_need_a_group() -> None:
    """私聊里没有"当前群"可言，只能明确说清。"""

    engine = DialogueEngine(NeverCalled())
    private = MessageTarget(user_id=ADMIN)
    result = asyncio.run(engine.handle(message("p1", "/admin enable", target=private)))
    assert result is not None and "要在群里发" in result.text


# --- /super addadmin --------------------------------------------------------


def test_super_admin_can_add_an_admin_by_mention() -> None:
    engine = DialogueEngine(NeverCalled())
    # 真实链路上 at 段会被 NapCat 摘掉：正文只剩命令，身份在 mentioned_user_ids 里。
    result = asyncio.run(engine.handle(
        message("s1", "/super addadmin", mentioned=("10086",))
    ))
    assert result is not None and "10086" in result.text
    assert "10086" in engine.group_admin_ids[GROUP], "授权记在发命令的这个群下面"

    # 加进去之后，那个人在**这个群**里能用管理员命令
    added = asyncio.run(engine.handle(message("a1", "/admin status", user_id="10086")))
    assert added is not None and "启用群聊" in added.text
    # 别的群不行
    other = asyncio.run(engine.handle(message(
        "a2", "/admin status", user_id="10086",
        target=MessageTarget(group_id="999999999"),
    )))
    assert other is None


def test_addadmin_can_target_an_explicit_group() -> None:
    engine = DialogueEngine(NeverCalled())
    result = asyncio.run(engine.handle(
        message("s1", f"/super addadmin @某人 {GROUP2}", mentioned=("10086",))
    ))
    assert result is not None and GROUP2 in result.text
    assert "10086" in engine.group_admin_ids[GROUP2]
    assert GROUP not in engine.group_admin_ids


def test_addadmin_in_private_needs_an_explicit_group() -> None:
    """私聊里没有"这个群"可言，必须写清群号。"""

    engine = DialogueEngine(NeverCalled())
    private = MessageTarget(user_id=ADMIN)
    without = asyncio.run(engine.handle(message(
        "s1", "/super addadmin @某人", target=private, mentioned=("10086",),
    )))
    assert without is not None and "用法" in without.text
    assert engine.group_admin_ids == {}

    with_group = asyncio.run(engine.handle(message(
        "s2", f"/super addadmin @某人 {GROUP2}", target=private, mentioned=("10086",),
    )))
    assert with_group is not None and GROUP2 in with_group.text
    assert "10086" in engine.group_admin_ids[GROUP2]


def test_addadmin_without_a_mention_explains_the_usage() -> None:
    engine = DialogueEngine(NeverCalled())
    result = asyncio.run(engine.handle(message("s1", "/super addadmin 10086")))
    assert result is not None and "用法" in result.text
    assert engine.group_admin_ids == {}, "正文里写的号码不算指定"


def test_addadmin_is_super_admin_only() -> None:
    engine = DialogueEngine(NeverCalled())
    result = asyncio.run(engine.handle(
        message("s1", "/super addadmin @某人", user_id="100", mentioned=("10086",))
    ))
    assert result is None
    assert engine.group_admin_ids == {}


def test_added_admin_survives_a_restart() -> None:
    """授权必须落盘：否则重启一次权限就回退了。"""

    import tempfile
    from pathlib import Path

    from qq_roleplay_bot.state_store import RuntimeStateStore

    with tempfile.TemporaryDirectory() as tmp:
        store = RuntimeStateStore(Path(tmp) / "state.json")
        engine = DialogueEngine(NeverCalled(), state_store=store)
        asyncio.run(engine.handle(message("s1", "/super addadmin @某人", mentioned=("10086",))))

        restarted = DialogueEngine(NeverCalled(), state_store=RuntimeStateStore(Path(tmp) / "state.json"))
        restored = restarted.restore_state()
        assert "10086" in restarted.group_admin_ids.get(GROUP, set())
        assert restored["admins"] >= 1
        # 恢复之后权限真的能用，而且只在那个群
        assert asyncio.run(restarted.handle(
            message("a1", "/admin status", user_id="10086")
        )) is not None
        assert asyncio.run(restarted.handle(message(
            "a2", "/admin status", user_id="10086", target=MessageTarget(group_id=GROUP2),
        ))) is None


def test_legacy_global_admin_list_is_not_restored_as_a_grant() -> None:
    """旧状态文件里那份没有群信息的名单**不生效**：照搬等于把权限放大到所有群。"""

    import json
    import tempfile
    from pathlib import Path

    from qq_roleplay_bot.state_store import RuntimeStateStore

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "state.json"
        path.write_text(json.dumps({
            "version": 1, "enabled": True, "enabled_group_ids": [GROUP],
            "admin_user_ids": ["10086"],
        }), encoding="utf-8")
        engine = DialogueEngine(NeverCalled(), state_store=RuntimeStateStore(path))
        engine.restore_state()
        assert "10086" not in engine.all_admin_user_ids()
        assert engine.group_admin_ids == {}


def test_addadmin_is_idempotent_and_reports_the_roster_size() -> None:
    engine = DialogueEngine(NeverCalled())
    first = asyncio.run(engine.handle(message("s1", "/super addadmin @某人", mentioned=("10086",))))
    second = asyncio.run(engine.handle(message("s2", "/super addadmin @某人", mentioned=("10086",))))
    assert first is not None and second is not None
    assert "本来就有权限" in second.text
    assert "管理员" in second.text


# --- /super deladmin 与 /super admin list -----------------------------------


def test_deladmin_removes_a_grant_and_persists() -> None:
    import tempfile
    from pathlib import Path

    from qq_roleplay_bot.state_store import RuntimeStateStore

    with tempfile.TemporaryDirectory() as tmp:
        store = RuntimeStateStore(Path(tmp) / "state.json")
        engine = DialogueEngine(NeverCalled(), state_store=store)
        asyncio.run(engine.handle(message("s1", "/super addadmin @某人", mentioned=("10086",))))
        assert "10086" in engine.group_admin_ids[GROUP]

        removed = asyncio.run(engine.handle(
            message("s2", "/super deladmin @某人", mentioned=("10086",))
        ))
        assert removed is not None and "已移出" in removed.text
        assert GROUP not in engine.group_admin_ids

        restarted = DialogueEngine(NeverCalled(), state_store=RuntimeStateStore(Path(tmp) / "state.json"))
        restarted.restore_state()
        assert "10086" not in restarted.all_admin_user_ids(), "撤掉的人不能从状态文件里复活"


def test_deladmin_only_revokes_the_named_group() -> None:
    """在 A 群撤权，B 群的授权不该受影响。"""

    engine = DialogueEngine(NeverCalled())
    asyncio.run(engine.handle(message(
        "s1", f"/super addadmin @某人 {GROUP2}", mentioned=("10086",),
    )))
    asyncio.run(engine.handle(message("s2", "/super addadmin @某人", mentioned=("10086",))))
    assert "10086" in engine.group_admin_ids[GROUP]
    assert "10086" in engine.group_admin_ids[GROUP2]

    asyncio.run(engine.handle(message("s3", "/super deladmin @某人", mentioned=("10086",))))
    assert GROUP not in engine.group_admin_ids
    assert "10086" in engine.group_admin_ids[GROUP2]


def test_deladmin_refuses_to_touch_a_configured_admin() -> None:
    """配置里写死的人撤不掉——撤了重启也会回来，那是个假动作。"""

    engine = DialogueEngine(NeverCalled())
    assert ADMIN in engine.admin_user_ids
    result = asyncio.run(engine.handle(
        message("s1", "/super deladmin @某人", mentioned=(ADMIN,))
    ))
    assert result is not None and "不能在这里撤" in result.text
    assert ADMIN in engine.admin_user_ids


def test_deladmin_without_a_mention_explains_the_usage() -> None:
    engine = DialogueEngine(NeverCalled())
    result = asyncio.run(engine.handle(message("s1", "/super deladmin 10086")))
    assert result is not None and "用法" in result.text
    assert engine.group_admin_ids == {}


def test_deladmin_is_super_admin_only() -> None:
    engine = DialogueEngine(NeverCalled())
    asyncio.run(engine.handle(message("a0", "/super addadmin @某人", mentioned=("10086",))))
    result = asyncio.run(engine.handle(
        message("u1", "/super deladmin @某人", user_id="100", mentioned=("10086",))
    ))
    assert result is None
    assert "10086" in engine.group_admin_ids[GROUP]


def test_admin_list_shows_super_global_and_admins_by_group() -> None:
    engine = DialogueEngine(NeverCalled())
    asyncio.run(engine.handle(message("s1", "/super addadmin @某人", mentioned=("10086",))))
    asyncio.run(engine.handle(message(
        "s2", f"/super addadmin @某人 {GROUP2}", mentioned=("10086",),
    )))
    result = asyncio.run(engine.handle(message("s3", "/super admin list")))
    assert result is not None
    assert "超管（全局" in result.text and ADMIN in result.text
    assert f"{GROUP}：10086" in result.text
    assert f"{GROUP2}：10086" in result.text


def test_admin_list_is_super_admin_only() -> None:
    engine = DialogueEngine(NeverCalled())
    assert asyncio.run(engine.handle(message("u1", "/super admin list", user_id="100"))) is None


# --- 未授权：形状对但没权限 -------------------------------------------------


def test_unauthorized_privileged_command_never_reaches_the_model() -> None:
    """未授权的 `/admin`、`/super` 形状消息**不落到模型那一侧**，也不进群历史。

    冷群的消息会先记进历史、之后被巡检捡回模型。只判"解析出来的命令没权限"
    挡不住 `/admin 随便写点什么` 这种解析不出来的形状——她迟早会拿它回一句话，
    那就等于确认了前缀有意义。

    2026-09-28 起分两种收尾：`/admin ...` 回一句"你没权限"（用户要求，且这正是
    `/super permit` 的由来），`/super ...` 与不成形的文本仍然完全静默。
    两种都只是引擎自己发的回执，跟模型无关。
    """

    for index, text in enumerate(
        ("/super addadmin @某人", "/super help", "/admin status", "/admin 随便写点什么")
    ):
        engine = DialogueEngine(NeverCalled())
        result = asyncio.run(engine.handle(message(f"u{index}", text, user_id="100200300")))
        if text == "/admin status":
            assert result is not None and "权限" in result.text, text
        else:
            assert result is None, text
        history = [m.text for m in engine.sessions.state(f"group:{GROUP}").recent()]
        assert history == [], f"{text} 不该进历史：{history}"
        assert engine.snapshot().ignored_messages >= 1, text


def test_the_guard_does_not_touch_authorized_admins() -> None:
    """闸门不能把有权限的人一起挡住。"""

    engine = DialogueEngine(NeverCalled())
    result = asyncio.run(engine.handle(message("ok1", "/admin status", user_id=ADMIN)))
    assert result is not None and GROUP in result.text
    history = [m.text for m in engine.sessions.state(f"group:{GROUP}").recent()]
    assert history == [], "命令回复不该进历史"


# --- 转告：确认流程仍然可用（前缀换成 /admin） ------------------------------


def test_relay_requires_explicit_confirmation_before_delivery() -> None:
    engine = DialogueEngine(NeverCalled())
    pending = asyncio.run(engine.handle(message("r1", "/admin relay group 123456789 转告内容")))
    assert pending is not None and "待确认转告" in pending.text
    assert engine.drain_relay_deliveries() == ()

    token = pending.text.split("/admin confirm ", 1)[1].splitlines()[0]
    confirmed = asyncio.run(engine.handle(message("r2", f"/admin confirm {token}")))
    assert confirmed is not None and "已确认" in confirmed.text
    deliveries = engine.drain_relay_deliveries()
    assert len(deliveries) == 1
    assert deliveries[0].target.group_id == "123456789"
    assert deliveries[0].text == "转告内容"


def test_relay_can_be_cancelled_and_non_admin_cannot_confirm() -> None:
    engine = DialogueEngine(NeverCalled())
    pending = asyncio.run(engine.handle(message("r1", "/admin relay user 987654321 私聊转告")))
    token = pending.text.split("/admin confirm ", 1)[1].splitlines()[0]
    unauthorized = asyncio.run(engine.handle(message("r2", f"/admin confirm {token}", user_id="100")))
    # 2026-09-28 起：没权限的人会收到一句说明（而不是静默）。转告本身仍然不许发出去。
    assert unauthorized is not None and "权限" in unauthorized.text
    assert engine.drain_relay_deliveries() == ()
    cancelled = asyncio.run(engine.handle(message("r3", f"/admin cancel {token}")))
    assert cancelled is not None and "已取消" in cancelled.text
    assert engine.drain_relay_deliveries() == ()


def test_relay_expires_after_confirmation_timeout() -> None:
    class Clock:
        value = 100.0

        def __call__(self):
            return self.value

    clock = Clock()
    engine = DialogueEngine(NeverCalled(), clock=clock)
    pending = asyncio.run(engine.handle(message("r1", "/admin relay group 123456789 即将过期")))
    token = pending.text.split("/admin confirm ", 1)[1].splitlines()[0]
    clock.value += 121.0

    expired = asyncio.run(engine.handle(message("r2", f"/admin confirm {token}")))
    assert expired is not None and "过期" in expired.text
    assert engine.drain_relay_deliveries() == ()


def test_relay_removes_control_characters_before_delivery() -> None:
    engine = DialogueEngine(NeverCalled())
    pending = asyncio.run(engine.handle(
        message("r1", "/admin relay user 987654321 第一\x00行\n第二行")
    ))
    assert pending is not None and "\x00" not in pending.text
    token = pending.text.split("/admin confirm ", 1)[1].splitlines()[0]
    asyncio.run(engine.handle(message("r2", f"/admin confirm {token}")))
    deliveries = engine.drain_relay_deliveries()
    assert len(deliveries) == 1
    assert deliveries[0].text == "第一行\n第二行"


def test_relay_delivery_notifies_admin_when_target_send_fails() -> None:
    class Transport:
        def __init__(self):
            self.calls = []

        async def send(self, target, text, *, reply_to: str = ""):
            self.calls.append((target, text))
            if len(self.calls) == 1:
                raise RuntimeError("target unavailable")

    transport = Transport()
    delivery = RelayDelivery(
        target=MessageTarget(group_id="123456789"),
        text="转告内容",
        admin_target=CONTROL_GROUP,
    )
    asyncio.run(_deliver_relay(transport, delivery))
    assert len(transport.calls) == 2
    assert transport.calls[1] == (CONTROL_GROUP, "转告发送失败，请检查目标会话和 OneBot 连接。")


def test_relay_name_match_resolves_before_confirmation() -> None:
    class Resolver:
        async def resolve(self, target_kind, query):
            assert target_kind == "group_name"
            assert query == "测试群"
            return (RelayTargetCandidate(MessageTarget(group_id="123456789"), "测试群（群号 123456789）"),)

    engine = DialogueEngine(NeverCalled(), relay_target_resolver=Resolver())
    pending = asyncio.run(engine.handle(
        message("n1", "/admin relay group_name 测试群 | 名称转告")
    ))
    assert pending is not None
    assert "测试群（群号 123456789）" in pending.text
    assert "内容：名称转告" in pending.text
    token = pending.text.split("/admin confirm ", 1)[1].splitlines()[0]
    asyncio.run(engine.handle(message("n2", f"/admin confirm {token}")))
    deliveries = engine.drain_relay_deliveries()
    assert len(deliveries) == 1
    assert deliveries[0].target.group_id == "123456789"


def test_relay_name_match_requires_selection_when_ambiguous() -> None:
    class Resolver:
        async def resolve(self, target_kind, query):
            return (
                RelayTargetCandidate(MessageTarget(user_id="100000001"), "小明（QQ 100000001）"),
                RelayTargetCandidate(MessageTarget(user_id="100000002"), "小明（QQ 100000002）"),
            )

    engine = DialogueEngine(NeverCalled(), relay_target_resolver=Resolver())
    ambiguous = asyncio.run(engine.handle(message("n1", "/admin relay nickname 小明 | 重名转告")))
    assert ambiguous is not None and "100000001" in ambiguous.text and "100000002" in ambiguous.text
    selection_token = ambiguous.text.split("/admin select ", 1)[1].split()[0]
    selected = asyncio.run(engine.handle(message("n2", f"/admin select {selection_token} 2")))
    assert selected is not None and "100000002" in selected.text and "待确认转告" in selected.text
    confirm_token = selected.text.split("/admin confirm ", 1)[1].splitlines()[0]
    asyncio.run(engine.handle(message("n3", f"/admin confirm {confirm_token}")))
    deliveries = engine.drain_relay_deliveries()
    assert len(deliveries) == 1
    assert deliveries[0].target.user_id == "100000002"


def test_onebot_name_resolver_matches_group_and_friend_or_member_nickname() -> None:
    class FakeTransport:
        def __init__(self):
            self.calls = []

        @staticmethod
        def _id_value(value):
            return int(value)

        async def call_api(self, action, params=None):
            self.calls.append((action, params))
            if action == "get_group_list":
                return {"status": "ok", "data": [{"group_id": 123, "group_name": "测试群"}]}
            if action == "get_friend_list":
                return {"status": "ok", "data": [{"user_id": 456, "nickname": "小明", "remark": ""}]}
            if action == "get_group_member_list":
                return {"status": "ok", "data": [{"user_id": 789, "nickname": "小明", "card": ""}]}
            raise AssertionError(action)

    transport = FakeTransport()
    resolver = OneBotRelayTargetResolver(transport)
    groups = asyncio.run(resolver.resolve("group_name", "测试群"))
    users = asyncio.run(resolver.resolve("nickname", "小明"))
    assert groups[0].target.group_id == "123"
    assert {candidate.target.user_id for candidate in users} == {"456", "789"}


# --- 超管诊断：只走 /super processes ----------------------------------------


def test_super_admin_can_get_process_diagnostics_in_an_enabled_group() -> None:
    class Diagnostics:
        async def processes(self, mode="default"):
            return f"超管诊断：python.exe（Bot） | PID 1234 | mode={mode}"

    engine = DialogueEngine(NeverCalled(), runtime_diagnostics=Diagnostics())
    result = asyncio.run(engine.handle(message("s1", "/super processes")))
    assert result is not None and "PID 1234" in result.text
    # 默认那一档：不带子命令
    assert "mode=default" in result.text


def test_processes_subcommands_are_passed_through() -> None:
    """`/super processes memory|cpu|gpu|gpu-memory` 的档位要原样递给诊断实现。"""

    seen: list[str] = []

    class Diagnostics:
        async def processes(self, mode="default"):
            seen.append(mode)
            return f"超管诊断：{mode}"

    engine = DialogueEngine(NeverCalled(), runtime_diagnostics=Diagnostics())
    for text, expected in (("/super processes memory", "memory"),
                           ("/super processes gpu", "gpu"),
                           ("/super processes gpu-memory", "gpu-memory"),
                           ("/super processes cpu", "cpu")):
        asyncio.run(engine.handle(message(f"s-{expected}", text)))
    # cpu 那条在没有延迟回发通道时退化成同步采样（也走 processes("cpu")）
    assert seen == ["memory", "gpu", "gpu-memory", "cpu"], seen


def test_cpu_sampling_replies_first_then_sends_the_result_later() -> None:
    """用户要的是"先回 checking、5 秒后再发结果"——这条走延迟回发通道。"""

    class Diagnostics:
        async def processes(self, mode="default"):
            return f"超管诊断：CPU 结果（mode={mode}）"

    sent: list[tuple[object, str]] = []

    async def sender(target, text):
        sent.append((target, text))

    engine = DialogueEngine(NeverCalled(), runtime_diagnostics=Diagnostics())
    engine.async_sender = sender

    async def run() -> str:
        result = await engine.handle(message("s1", "/super processes cpu"))
        assert result is not None and "正在采样" in result.text
        # 让延迟任务跑完（它 await 的是即时返回的假实现）
        for _ in range(5):
            await asyncio.sleep(0)
        return result.text

    asyncio.run(run())
    assert sent and "CPU 结果" in sent[0][1]
    assert sent[0][0] == message("s1", "/super processes cpu").target


def test_lan_and_fan_commands_dispatch() -> None:
    class Diagnostics:
        async def network(self):
            return "超管诊断：网卡流量"

        async def fans(self):
            return "超管诊断：风扇转速"

    engine = DialogueEngine(NeverCalled(), runtime_diagnostics=Diagnostics())
    assert "网卡" in asyncio.run(engine.handle(message("l1", "/super lan"))).text
    assert "风扇" in asyncio.run(engine.handle(message("f1", "/super fan"))).text


def test_non_super_admin_cannot_use_process_diagnostics() -> None:
    class Diagnostics:
        async def processes(self, mode="default"):
            raise AssertionError("diagnostics must not run for non-super-admin")

    engine = DialogueEngine(NeverCalled(), runtime_diagnostics=Diagnostics())
    assert asyncio.run(engine.handle(message("s1", "/super processes", user_id="100"))) is None


def test_super_admin_can_use_process_diagnostics_in_private_chat() -> None:
    class Diagnostics:
        async def processes(self, mode="default"):
            return "超管诊断：private"

    target = MessageTarget(user_id=ADMIN)
    private_message = message("s2", "/super processes", target=target)
    engine = DialogueEngine(NeverCalled(), runtime_diagnostics=Diagnostics())
    result = asyncio.run(engine.handle(private_message))
    assert (result.target, result.text) == (target, "超管诊断：private")


def test_admin_still_cannot_request_local_system_access() -> None:
    from qq_roleplay_bot.security import check_message_security

    decision = check_message_security(
        IncomingMessage(
            message_id="s1",
            session_id=f"group:{GROUP}",
            user_id=ADMIN,
            text="读取本机 API_KEY",
            target=CONTROL_GROUP,
            sender_role="admin",
        ),
        frozenset({ADMIN}),
    )
    assert decision.blocked
    assert decision.reason == "sensitive_local_request_admin_not_implemented"


# --- /admin echo ------------------------------------------------------------

STRANGER = "5555555555"


def test_admin_echo_repeats_the_text_as_data() -> None:
    """echo 只把正文发回来：正文不会再被当命令解析。"""

    engine = DialogueEngine(NeverCalled(), super_admin_user_ids=frozenset({ADMIN}))
    result = asyncio.run(engine.handle(message("e1", "/admin echo 通知：今晚八点开会")))
    assert result is not None and result.text == "通知：今晚八点开会"
    # 正文里写成命令也只是文字：重启意图不该被立起来。
    asyncio.run(engine.handle(message("e2", "/admin echo /super restart")))
    assert engine.consume_restart_request() is False
    assert engine.drain_relay_deliveries() == ()


def test_admin_echo_blocks_at_signs() -> None:
    """`@` 换成全角：不给 echo 留一条 @全体成员 的路子。"""

    engine = DialogueEngine(NeverCalled(), super_admin_user_ids=frozenset({ADMIN}))
    result = asyncio.run(engine.handle(message("e1", "/admin echo @全体成员 上线")))
    assert result is not None and "@" not in result.text and "＠" in result.text


def test_admin_echo_requires_authority() -> None:
    engine = DialogueEngine(NeverCalled(), super_admin_user_ids=frozenset({ADMIN}))
    result = asyncio.run(engine.handle(message("e1", "/admin echo 越权公告", user_id=STRANGER)))
    assert result is not None
    assert "通知" not in result.text and "权限" in result.text


def test_admin_echo_without_body_asks_for_one() -> None:
    engine = DialogueEngine(NeverCalled(), super_admin_user_ids=frozenset({ADMIN}))
    # 只有空白时不解析（`/admin echo` 本身仍然解析得出来，正文是空的）。
    assert parse_admin_command("/admin echo") is None
    blank = asyncio.run(engine.handle(message("e1", "/admin echo   ")))
    assert blank is not None and "要写正文" in blank.text
    assert parse_admin_command("/admin echo hi") is not None


# --- 超管引用放行（/super permit） ------------------------------------------
#
# 用户 2026-09-28 定的语义：**某个人发了一条 /admin 命令被拒 → 超管引用那条消息
# 发一句 /super permit → yunru 就执行那一条**。刻意**不是**"给某人一个临时身份、
# 他自己再发一遍"——放行的对象是那条命令，不是那个人的权限。

def _permit_engine(*, clock=None):
    engine = DialogueEngine(
        NeverCalled(),
        admin_user_ids=frozenset(),
        super_admin_user_ids=frozenset({ADMIN}),
        clock=clock or (lambda: 1000.0),
    )
    return engine


def test_permit_parser_accepts_aliases_and_rejects_lookalikes() -> None:
    from qq_roleplay_bot.stage3_main import SuperAction, parse_super_command

    for text in ("/super permit", "/super 授权", "/super 临时授权 说明"):
        assert parse_super_command(text) is SuperAction.PERMIT, text
    for text in ("/super permission", "/super permitx", "/super restart permit", "/super 授权码"):
        assert parse_super_command(text) is None, text


def test_super_admin_quoting_the_denied_command_executes_it() -> None:
    """主流程：被拒 → 引用那条 → /super permit → 命令真的执行了。"""

    engine = _permit_engine()
    denied = asyncio.run(engine.handle(message("a1", "/admin status", user_id=STRANGER)))
    assert denied is not None and "权限" in denied.text

    approved = asyncio.run(engine.handle(message("p1", "/super permit", reply_to="a1")))
    assert approved is not None
    assert "启用群聊" in approved.text, approved.text
    assert engine._stats["admin_permit_executed"] == 1
    # 一次即消：同一条再引用一次不再执行。
    again = asyncio.run(engine.handle(message("p2", "/super permit", reply_to="a1")))
    assert again is not None and "用法" in again.text
    assert engine._stats["admin_permit_executed"] == 1


def test_approval_actually_changes_the_group_state() -> None:
    """不只看回复文字：`/admin disable` 放行之后本群真的被关掉了。"""

    engine = _permit_engine()
    asyncio.run(engine.handle(message("a1", "/admin disable", user_id=STRANGER)))
    assert GROUP in engine.enabled_group_ids
    approved = asyncio.run(engine.handle(message("p1", "/super permit", reply_to="a1")))
    assert approved is not None and "已关闭" in approved.text
    assert GROUP not in engine.enabled_group_ids


def test_approval_does_not_leave_any_permission_behind() -> None:
    """放行的是那一条命令，不是那个人的身份：他之后再发一样被拒。"""

    engine = _permit_engine()
    asyncio.run(engine.handle(message("a1", "/admin status", user_id=STRANGER)))
    asyncio.run(engine.handle(message("p1", "/super permit", reply_to="a1")))
    # 同一个人、同一条命令、同一个群，重新发一遍：仍然没权限。
    later = asyncio.run(engine.handle(message("a2", "/admin status", user_id=STRANGER)))
    assert later is not None and "权限" in later.text and "启用群聊" not in later.text
    assert engine._control_scope_ok(message("a3", "/admin status", user_id=STRANGER)) is False
    assert not hasattr(engine, "_admin_permits"), "临时授权机制已废弃"


def test_quoting_someone_elses_chat_message_does_nothing() -> None:
    """引用一条**不是被拒命令**的消息：不执行任何东西，只回用法。"""

    engine = _permit_engine()
    asyncio.run(engine.handle(message("c1", "晚上吃什么", user_id=STRANGER)))
    result = asyncio.run(engine.handle(message("p1", "/super permit", reply_to="c1")))
    assert result is not None and "用法" in result.text
    assert "启用群聊" not in result.text


def test_permit_without_a_quote_lists_the_recent_denied_commands() -> None:
    engine = _permit_engine()
    asyncio.run(engine.handle(message("a1", "/admin clear", user_id=STRANGER)))
    result = asyncio.run(engine.handle(message("p1", "/super permit")))
    assert result is not None
    assert "用法" in result.text and "引用" in result.text
    # 列出被拒的那条（谁发的 + 命令原文），超管照着引用就行。
    assert STRANGER in result.text and "/admin clear" in result.text


def test_permit_without_a_quote_says_so_when_nothing_was_denied() -> None:
    engine = _permit_engine()
    result = asyncio.run(engine.handle(message("p1", "/super permit")))
    assert result is not None and "用法" in result.text
    assert "没有" in result.text and "被拒" in result.text


def test_approval_only_works_in_the_group_the_command_came_from() -> None:
    """被放行的命令仍然只作用在**它被发出来的那个群**。"""

    engine = _permit_engine()
    asyncio.run(engine.handle(message(
        "a1", "/admin clear", user_id=STRANGER, target=MessageTarget(group_id=GROUP1),
    )))
    elsewhere = asyncio.run(engine.handle(message(
        "p1", "/super permit", reply_to="a1", target=MessageTarget(group_id=GROUP2),
    )))
    assert elsewhere is not None and "只能在命令被发出来的那个群里放行" in elsewhere.text
    # 那条命令还在（没有被消费掉），回到正确的群仍然能放行。
    assert "a1" in engine._denied_admin
    back = asyncio.run(engine.handle(message(
        "p2", "/super permit", reply_to="a1", target=MessageTarget(group_id=GROUP1),
    )))
    assert back is not None and "短期对话状态" in back.text


def test_stale_denial_cannot_be_approved() -> None:
    class Clock:
        value = 1000.0

        def __call__(self):
            return self.value

    clock = Clock()
    engine = _permit_engine(clock=clock)
    asyncio.run(engine.handle(message("a1", "/admin status", user_id=STRANGER)))
    clock.value += 301.0
    result = asyncio.run(engine.handle(message("p1", "/super permit", reply_to="a1")))
    assert result is not None and "超过放行时限" in result.text
    assert engine._denied_admin == {}


def test_only_super_admin_can_approve() -> None:
    engine = _permit_engine()
    asyncio.run(engine.handle(message("a1", "/admin status", user_id=STRANGER)))
    # 普通人引用着发 /super permit：那一层对他完全静默，也不会执行任何东西。
    assert asyncio.run(engine.handle(message(
        "p1", "/super permit", user_id=STRANGER, reply_to="a1",
    ))) is None
    assert engine._stats.get("admin_permit_executed", 0) == 0
    assert "a1" in engine._denied_admin


def test_approving_your_own_admin_command_is_a_no_op() -> None:
    """超管自己发的 admin 命令本来就执行了，没什么可放行的。"""

    engine = _permit_engine()
    asyncio.run(engine.handle(message("a1", "/admin status", user_id=ADMIN)))
    result = asyncio.run(engine.handle(message("p1", "/super permit", reply_to="a1")))
    assert result is not None and "用法" in result.text


# --- 无权限说明（deny notice） ----------------------------------------------

def test_denied_admin_command_gets_a_notice_and_is_recorded() -> None:
    engine = _permit_engine()
    result = asyncio.run(engine.handle(message("a1", "/admin clear", user_id=STRANGER)))
    assert result is not None and "权限" in result.text
    # 记的是**这条消息**：超管靠引用它来指定"就这一条"。
    assert engine._denied_admin["a1"][0].text == "/admin clear"
    assert engine._denied_admin["a1"][0].user_id == STRANGER


def test_denied_notice_can_be_silenced_by_env() -> None:
    """AGENTS.md 的默认是"未授权时静默"：留一个开关退回那一侧。"""

    import os

    engine = _permit_engine()
    previous = os.environ.get("QQBOT_ADMIN_DENY_NOTICE")
    os.environ["QQBOT_ADMIN_DENY_NOTICE"] = "0"
    try:
        assert asyncio.run(engine.handle(message("a1", "/admin clear", user_id=STRANGER))) is None
    finally:
        if previous is None:
            os.environ.pop("QQBOT_ADMIN_DENY_NOTICE", None)
        else:
            os.environ["QQBOT_ADMIN_DENY_NOTICE"] = previous
    # 静默只是不回话：那条命令仍然记着，超管照样能引用放行。
    assert engine._denied_admin["a1"][0].text == "/admin clear"
    approved = asyncio.run(engine.handle(message("p1", "/super permit", reply_to="a1")))
    assert approved is not None and "短期对话状态" in approved.text


def test_denied_notice_is_not_sent_for_private_or_shapeless_text() -> None:
    engine = _permit_engine()
    private = asyncio.run(engine.handle(message(
        "a1", "/admin clear", user_id=STRANGER, target=MessageTarget(user_id=STRANGER),
    )))
    assert private is None
    # 私聊里不回话，但那条命令仍然记着（不过只能在群里放行，见下）。
    assert engine._denied_admin["a1"][0].text == "/admin clear"

    shapeless = asyncio.run(engine.handle(message("a2", "/admin 随便写点什么", user_id=STRANGER)))
    assert shapeless is None
    # 不成形的文本既不回话也**不记**：它没有可执行的命令，记了只会误导。
    assert "a2" not in engine._denied_admin


def test_denied_notice_is_not_sent_to_admins_of_other_groups() -> None:
    """在别的群有管理权限的人，在这个群里做事被拒时不回那句说明。"""

    engine = _permit_engine()
    asyncio.run(engine.handle(message(
        "g1", "/super addadmin @某人", mentioned=(GRANTEE,), target=MessageTarget(group_id=GROUP1),
    )))
    result = asyncio.run(engine.handle(message(
        "a1", "/admin status", user_id=GRANTEE, target=MessageTarget(group_id=GROUP2),
    )))
    assert result is None


def test_super_commands_are_never_answered_for_non_super_admins() -> None:
    """`/super ...` 一律静默：那一层对不是超管的人不该可见。"""

    engine = _permit_engine()
    for text in ("/super help", "/super permit", "/super processes", "/super status"):
        assert asyncio.run(engine.handle(message(f"s:{text}", text, user_id=STRANGER))) is None
        assert engine._denied_admin == {}


# --- 群没开对话时，日志里要留得下痕迹 ----------------------------------------

class _LogCapture:
    """收 `stage3_main` 的日志记录（`assertLogs` 的最小替代，保持用例风格一致）。

    要把这个 logger 的级别调到 INFO：真实运行时 `run()` 里 `basicConfig(level=INFO)`
    已经把根级别设好了，测试进程里根级别是 WARNING，`logger.info(...)` 会被直接丢掉。
    """

    def __init__(self) -> None:
        import logging

        self.records: list[str] = []
        self._logging = logging
        self._logger = logging.getLogger("qq_roleplay_bot.stage3_main")
        self._previous = self._logger.level
        self._handler = logging.Handler()
        self._handler.emit = lambda record: self.records.append(record.getMessage())

    def __enter__(self):
        self._logger.addHandler(self._handler)
        self._logger.setLevel(self._logging.INFO)
        return self

    def __exit__(self, *exc):
        self._logger.removeHandler(self._handler)
        self._logger.setLevel(self._previous)
        return False


def test_addressed_message_in_a_disabled_group_leaves_a_log_line() -> None:
    """2026-09-28 的现场："云茹怎么不说话了"——那个群被人 `/admin disable` 关掉了，
    可日志里一个字都没有，只能去翻安全日志才查得出来。现在明确找她的消息会记一条。"""

    engine = _permit_engine()
    engine.enabled_group_ids = set()  # 谁的群都没开
    with _LogCapture() as logs:
        result = asyncio.run(engine.handle(message(
            "m1", "@云茹 在吗", user_id=STRANGER, target=MessageTarget(group_id=GROUP1),
            mentioned=("10001",),
        )))
    assert result is None
    assert any("群未启用" in line for line in logs.records), logs.records


def test_quiet_chat_in_a_disabled_group_stays_quiet_in_the_log() -> None:
    """别人的闲聊不记：没开的群本来就是旁听，记满日志反而看不见真正找她的那句。"""

    engine = _permit_engine()
    engine.enabled_group_ids = set()
    with _LogCapture() as logs:
        assert asyncio.run(engine.handle(message(
            "m1", "今晚吃什么", user_id=STRANGER, target=MessageTarget(group_id=GROUP1),
        ))) is None
    assert not any("群未启用" in line for line in logs.records), logs.records


def test_admin_chatting_in_a_disabled_group_leaves_a_log_line() -> None:
    """超管在没开的群里说话也会被丢掉（设计如此），但这条路径原来**一个字都不记**。"""

    engine = _permit_engine()
    engine.enabled_group_ids = set()
    with _LogCapture() as logs:
        result = asyncio.run(engine.handle(message(
            "m1", "在吗", user_id=ADMIN, target=MessageTarget(group_id=GROUP1),
        )))
    assert result is None
    assert any("disabled group" in line for line in logs.records), logs.records
