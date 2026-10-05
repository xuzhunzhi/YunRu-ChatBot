"""名册里的**身份**：谁是这个群的管理员/群主，以及她自己是什么角色。

## 由来（2026-10-06）

用户问 *"role 你认为有必要进核心吗，也就是说 yunru 要不要能识别到对方是管理员还是
群主还是群成员，以及自己的身份"*，随后拍板 **"行，进核心"**。

在这一次之前，`<people>` 名册上**只有名字与 QQ 号**：她分不出群里谁说话算数。
事实现在由核心给（`group_roles.GroupRoles`），名册只是把它渲染出来。

## 这个文件钉什么

1. **有身份的人才标**（群主/管理员），普通成员不标——省 token、也让"有身份的那几个"
   在名册里显眼；
2. **她自己**那两行也进名册（`0=云茹（…）`、`-=云茹（…）`）；
3. **不知道就不写**：拿不到事实时名册与从前**逐字相同**——绝不填一个默认角色
   （fail-closed 在渲染这一头的落点）；
4. **渲染是同步的**、事实是事先异步取进缓存的（`build_roster_lines` 的顺序）。
"""
import asyncio

from qq_roleplay_bot.group_roles import (
    ROLE_ADMIN,
    ROLE_MEMBER,
    ROLE_OWNER,
    ROLE_UNKNOWN,
    GroupRoles,
)
from qq_roleplay_bot.stage3_runtime import (
    SELF_ALIAS,
    SELF_GROUP_ALIAS,
    ConversationState,
    build_roster_lines,
    format_self_roles,
)
from qq_roleplay_bot.transport import IncomingMessage, MessageTarget

GROUP = "717151356"
TARGET = MessageTarget(group_id=GROUP)
SESSION = f"group:{GROUP}"
ME = "900000002"
BOSS = "900000001"
MOD = "900000003"
PLAIN = "900000004"


class _FakeActions:
    """一个假的 `call_action`：按 **QQ 号** 回答角色。"""

    def __init__(self, roles: dict, *, self_id: str = ME) -> None:
        self.roles = dict(roles)
        self.self_id = self_id
        self.calls: list[tuple[str, dict]] = []

    async def __call__(self, action, params=None):
        params = dict(params or {})
        self.calls.append((action, params))
        if action == "get_login_info":
            return {"user_id": self.self_id}
        user = str(params.get("user_id") or "")
        data = {"user_id": user}
        if user in self.roles:
            data["role"] = self.roles[user]
        return data


def _state(*people: tuple[str, str]) -> ConversationState:
    state = ConversationState(history_limit=2000)
    for index, (user_id, name) in enumerate(people):
        state.add(IncomingMessage(
            message_id=f"m{index}", session_id=SESSION, user_id=user_id,
            text=f"第{index}句", target=TARGET, sender_name=name,
        ), float(index))
    return state


def _line(lines: list[str], alias: int) -> str:
    for line in lines:
        if line.startswith(f"{alias}="):
            return line
    return ""


def test_the_owner_and_an_admin_are_marked_and_a_plain_member_is_not() -> None:
    """**有身份的人才标**：群主与管理员带角色词，普通成员那一行与从前一样。"""

    actions = _FakeActions({BOSS: ROLE_OWNER, MOD: ROLE_ADMIN, PLAIN: ROLE_MEMBER})
    state = _state((BOSS, "老板"), (MOD, "管理"), (PLAIN, "路人"))

    lines = asyncio.run(build_roster_lines(GroupRoles(actions), GROUP, state))

    assert "群主" in _line(lines, 1), lines
    assert "管理员" in _line(lines, 2), lines
    plain = _line(lines, 3)
    assert "普通成员" not in plain, f"普通成员不该标（省 token）：{plain}"
    assert plain == f"3=路人（QQ{PLAIN}）", plain


def test_her_own_role_is_in_the_roster() -> None:
    """**她自己**的身份也要在名册里：`0=` 是"她这个人"，`-=` 是"她在这个群"。"""

    actions = _FakeActions({ME: ROLE_OWNER})
    state = _state((PLAIN, "路人"))

    lines = asyncio.run(build_roster_lines(GroupRoles(actions), GROUP, state))

    assert lines[0].startswith(f"{SELF_ALIAS}=云茹（群主）"), lines
    assert any(line.startswith(f"{SELF_GROUP_ALIAS}=云茹（群主）") for line in lines), lines


def test_a_group_of_plain_members_gets_no_role_tags_at_all() -> None:
    """全是普通成员时：**别人**那几行一个角色词都不写。

    她自己的那两行照写（知道"我在这个群里就是个普通成员"本身就是她要的那件事），
    但"普通成员"不该出现在**别人**的行上。
    """

    actions = _FakeActions({PLAIN: ROLE_MEMBER, ME: ROLE_MEMBER})
    state = _state((PLAIN, "路人"))

    lines = asyncio.run(build_roster_lines(GroupRoles(actions), GROUP, state))

    assert _line(lines, 1) == f"1=路人（QQ{PLAIN}）", lines
    assert "管理员" not in "".join(lines) and "群主" not in "".join(lines), lines
    assert lines[0].startswith(f"{SELF_ALIAS}=云茹（"), lines


def test_an_unknown_role_is_never_rendered_as_a_default() -> None:
    """**拿不到事实就不写**——名册与从前逐字相同，绝不填一个默认角色。"""

    actions = _FakeActions({})                       # 对面什么 role 都不给
    state = _state((BOSS, "老板"))

    lines = asyncio.run(build_roster_lines(GroupRoles(actions), GROUP, state))

    assert lines == [f"1=老板（QQ{BOSS}）"], lines
    for word in ("群主", "管理员", "普通成员", "查不到"):
        assert word not in "".join(lines), word


def test_a_broken_channel_leaves_the_roster_exactly_as_before() -> None:
    """通道整个炸掉时，名册**一个字都不多**（取不到角色不该耽误这一条回复）。"""

    class _Boom:
        async def __call__(self, action, params=None):
            raise RuntimeError("对面不干了")

    state = _state((BOSS, "老板"), (PLAIN, "路人"))
    lines = asyncio.run(build_roster_lines(GroupRoles(_Boom()), GROUP, state))
    assert lines == [f"1=老板（QQ{BOSS}）", f"2=路人（QQ{PLAIN}）"], lines


def test_without_a_role_service_the_roster_is_unchanged() -> None:
    """没有角色服务（`None`，测试与离线渲染的常态）时，名册与从前逐字相同。"""

    state = _state((BOSS, "老板"))
    lines = asyncio.run(build_roster_lines(None, GROUP, state))
    assert lines == [f"1=老板（QQ{BOSS}）"], lines
    # 直接调（不经过 `build_roster_lines`）时也是一个字都不加。
    assert state.roster_lines() == [f"1=老板（QQ{BOSS}）"]


def test_private_chat_gets_no_self_role_lines() -> None:
    """私聊没有群，就没有"她在这个群是什么角色"这回事。"""

    actions = _FakeActions({ME: ROLE_OWNER})
    state = ConversationState(history_limit=2000)
    state.add(IncomingMessage(message_id="m0", session_id="private:1", user_id=PLAIN,
                              text="在吗", target=MessageTarget(user_id=PLAIN),
                              sender_name="路人"), 0.0)

    lines = asyncio.run(build_roster_lines(GroupRoles(actions), None, state))
    assert lines == [f"1=路人（QQ{PLAIN}）"], lines


def test_roles_are_fetched_once_and_then_read_from_cache() -> None:
    """渲染在读缓存：连续两次渲染**第二次一次查询都不发**。

    第一次是 2 次（她自己那份 + 名册上那一个人那份），第二次是 0 次——
    所以两轮下来总数仍是 2。这条正是"缓存 + TTL"在渲染路径上的落点。
    """

    actions = _FakeActions({MOD: ROLE_ADMIN, ME: ROLE_MEMBER})
    state = _state((MOD, "管理"))
    roles = GroupRoles(actions)

    def member_queries() -> int:
        return len([c for c in actions.calls if c[0] == "get_group_member_info"])

    asyncio.run(build_roster_lines(roles, GROUP, state))
    after_first = member_queries()
    asyncio.run(build_roster_lines(roles, GROUP, state))
    after_second = member_queries()
    assert after_first == 2, f"第一轮应当是 2 次（她自己 + 名册上那个人）：{after_first}"
    assert after_second == 2, f"第二轮一次都不该发，总数应当还是 2：{after_second}"


def test_her_own_role_is_only_queried_once_per_group() -> None:
    """她自己那一份也走缓存：同群再渲染一次不该重新问。"""

    actions = _FakeActions({ME: ROLE_OWNER})
    state = _state((PLAIN, "路人"))
    roles = GroupRoles(actions)

    asyncio.run(build_roster_lines(roles, GROUP, state))
    asyncio.run(build_roster_lines(roles, GROUP, state))
    mine = [c for c in actions.calls
            if c[0] == "get_group_member_info" and str(c[1].get("user_id")) == ME]
    assert len(mine) == 1, f"问了 {len(mine)} 次"


def test_the_self_role_lines_are_escaped_like_everything_else() -> None:
    """角色词也是外部数据（对面返回的），进名册前照样转义。"""

    lines = format_self_roles(self_label="<b>冒充</b>")
    assert "<b>" not in lines[0], lines
    assert "&lt;b&gt;" in lines[0], lines


def test_format_self_roles_writes_nothing_when_nothing_is_known() -> None:
    """**不知道就不写**：空串与内部值 `unknown` 都不许渲染成一行。"""

    assert format_self_roles() == []
    assert format_self_roles(self_label=ROLE_UNKNOWN) == []
    assert format_self_roles(self_label=ROLE_UNKNOWN, group_label=ROLE_UNKNOWN) == []
