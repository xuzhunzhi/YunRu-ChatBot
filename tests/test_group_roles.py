"""核心的群成员角色事实服务（`group_roles.GroupRoles`）。

## 这个文件钉什么

2026-10-06 用户拍板**"行，进核心"**：身份与权限事实原来长在插件侧
（`plugins/roles/`，只回答"她自己是什么角色"），现在归核心，并且要多回答一件事
——**对方是管理员还是群主**。

所以这里守三组东西：

1. **两个问题答得对**：某人在某群的角色、她自己在那里的角色；
2. **fail-closed**：拿不到事实时一律 `unknown` / `False`，**绝不猜成管理员**（这组是
   本文件的重头，见 `test_*_fails_closed`）；
3. **缓存与失效**：TTL 内不重复查、`refresh` 与 `forget` 能立刻作废、
   批量 `prime` 有每轮预算。
"""
import asyncio
import sys

from qq_roleplay_bot import runtime
from qq_roleplay_bot.group_roles import (
    MAX_PRIME_USERS,
    ROLE_ADMIN,
    ROLE_LABELS,
    ROLE_MEMBER,
    ROLE_OWNER,
    ROLE_UNKNOWN,
    GroupRoles,
    normalize_role,
)

GROUP = "717151356"
OTHER_GROUP = "800000001"
ME = "900000002"
SOMEONE = "900000007"


class _FakeActions:
    """一个假的"核心注入的 call_action"：记下每一次调用，按脚本回答。"""

    def __init__(self, *, role=ROLE_MEMBER, self_id=ME, explode=None) -> None:
        self.role = role
        self.self_id = self_id
        self.explode = explode or set()
        self.calls: list[tuple[str, dict]] = []

    async def __call__(self, action, params=None):
        params = dict(params or {})
        self.calls.append((action, params))
        if action in self.explode:
            raise RuntimeError(f"对面不干了：{action}")
        if action == "get_login_info":
            return {"user_id": self.self_id, "nickname": "YunRu"}
        if action == "get_group_member_info":
            if self.role is None:
                # 对面回了 ok，但**没有 role 这个字段**——"拿不到事实"的一种形状。
                return {"user_id": params.get("user_id"), "nickname": "某人"}
            return {"user_id": params.get("user_id"), "role": self.role}
        return {}


def _roles(actions: _FakeActions, **kwargs) -> GroupRoles:
    return GroupRoles(actions, **kwargs)


# --- 1. 两个问题 ---------------------------------------------------------

def test_it_answers_someone_elses_role_in_a_group() -> None:
    """**某人在某群是什么角色**——这是插件侧那份根本没有的那一半。"""

    actions = _FakeActions(role=ROLE_ADMIN)
    roles = _roles(actions)

    assert asyncio.run(roles.role(GROUP, SOMEONE)) == ROLE_ADMIN
    action, params = actions.calls[-1]
    assert action == "get_group_member_info"
    assert params["group_id"] == int(GROUP) and params["user_id"] == int(SOMEONE)


def test_it_answers_her_own_role_in_a_group() -> None:
    """**她自己**在那个群的角色：不传 `user_id` 时问的是她自己。"""

    actions = _FakeActions(role=ROLE_OWNER)
    roles = _roles(actions)

    assert asyncio.run(roles.role(GROUP)) == ROLE_OWNER
    # 她的 QQ 号是现查 `get_login_info` 拿到的，不是谁传进来的。
    assert ("get_login_info", {}) in actions.calls
    assert actions.calls[-1][1]["user_id"] == int(ME)


def test_the_self_lookup_happens_once_and_is_also_readable_synchronously() -> None:
    """登录信息只查一次；而且**同步**读得到（面板那条接缝不能 await）。"""

    actions = _FakeActions(role=ROLE_OWNER)
    roles = _roles(actions)

    assert roles.cached_self_id() == "", "还没查过就是空串，不编一个"
    asyncio.run(roles.self_role(GROUP))
    assert roles.cached_self_id() == ME
    asyncio.run(roles.self_role(OTHER_GROUP))
    logins = [c for c in actions.calls if c[0] == "get_login_info"]
    assert len(logins) == 1, f"登录信息查了 {len(logins)} 次"


def test_only_read_actions_are_ever_used() -> None:
    """只用只读动作——两条都必须在 `capabilities.READ_ACTIONS` 里。"""

    from qq_roleplay_bot.capabilities import READ_ACTIONS

    actions = _FakeActions(role=ROLE_ADMIN)
    roles = _roles(actions)
    asyncio.run(roles.role(GROUP, SOMEONE))   # 问别人：只用 get_group_member_info
    asyncio.run(roles.self_role(GROUP))       # 问她自己的角色：还要 get_login_info
    used = {action for action, _ in actions.calls}
    assert used == {"get_login_info", "get_group_member_info"}
    assert used <= READ_ACTIONS, f"用了不在只读名单里的动作：{used - READ_ACTIONS}"


def test_it_refuses_a_bare_transport() -> None:
    """**不接受裸传输层**：那条路不过 `capabilities` 闸门。"""

    class _Transport:
        async def call_api(self, action, params=None):  # pragma: no cover - 不该被调到
            raise AssertionError("裸传输层不该能构造出角色服务")

    try:
        GroupRoles(_Transport())
    except TypeError:
        return
    raise AssertionError("裸传输层居然构造成功了——那条路不过能力闸门")


def test_role_names_are_normalised_and_unknown_stays_unknown() -> None:
    assert normalize_role("OWNER") == ROLE_OWNER
    assert normalize_role(" admin ") == ROLE_ADMIN
    assert normalize_role("群主") == ROLE_OWNER
    assert normalize_role("管理员") == ROLE_ADMIN
    assert normalize_role("member") == ROLE_MEMBER
    # 认不出来的一律 unknown——**不许**落到任何一个有权力的档上。
    assert normalize_role("superuser") == ROLE_UNKNOWN
    assert normalize_role("") == ROLE_UNKNOWN
    assert normalize_role(None) == ROLE_UNKNOWN


def test_the_roster_and_the_role_service_speak_the_same_words() -> None:
    """名册那两张角色词表与这里的必须逐个同义（抄写会漂，所以核一遍）。

    `stage3_runtime` 是核心常驻模块，**不能** import 这个可拔掉的模块，
    所以它抄了一份字面量；这条用例就是那次抄写的守卫。
    """

    from qq_roleplay_bot import stage3_runtime

    assert stage3_runtime.ROLE_LABELS_FOR_ROSTER == ROLE_LABELS_FOR_ROSTER_EXPECTED
    assert stage3_runtime.ROSTER_ROLE_MEMBER_LABEL == ROLE_LABELS[ROLE_MEMBER]
    for role, label in stage3_runtime.ROLE_LABELS_FOR_ROSTER.items():
        assert ROLE_LABELS[role] == label, f"{role} 两边叫法不一样"


#: 与 `group_roles.ROLE_LABELS` 同义的完整表（名册那份只用到前三个）。
ROLE_LABELS_FOR_ROSTER_EXPECTED = {
    ROLE_OWNER: "群主",
    ROLE_ADMIN: "管理员",
    ROLE_MEMBER: "普通成员",
}


# --- 2. fail-closed（这一组是重点）---------------------------------------

def test_a_query_that_explodes_fails_closed() -> None:
    """查询抛异常 → `unknown` / `False`。**绝不猜成管理员。**"""

    actions = _FakeActions(role=ROLE_OWNER, explode={"get_group_member_info"})
    roles = _roles(actions)

    assert asyncio.run(roles.role(GROUP, SOMEONE)) == ROLE_UNKNOWN
    assert asyncio.run(roles.at_least_admin(GROUP, SOMEONE)) is False
    assert asyncio.run(roles.is_owner(GROUP, SOMEONE)) is False


def test_a_payload_without_a_role_fails_closed() -> None:
    """对面回了 ok 但**没有 role 字段** → `unknown` / `False`。"""

    actions = _FakeActions(role=None)
    roles = _roles(actions)

    assert asyncio.run(roles.role(GROUP, SOMEONE)) == ROLE_UNKNOWN
    assert asyncio.run(roles.at_least_admin(GROUP, SOMEONE)) is False


def test_a_garbage_role_value_fails_closed() -> None:
    """`role` 是个认不出来的值 → `unknown` / `False`（不是"大概是成员"）。"""

    actions = _FakeActions(role="superuser")
    roles = _roles(actions)

    assert asyncio.run(roles.role(GROUP, SOMEONE)) == ROLE_UNKNOWN
    assert asyncio.run(roles.is_owner(GROUP, SOMEONE)) is False


def test_the_login_lookup_failing_fails_closed() -> None:
    """连"她自己是谁"都拿不到 → 她自己的角色也只能是 `unknown`。"""

    actions = _FakeActions(role=ROLE_OWNER, explode={"get_login_info"})
    roles = _roles(actions)

    assert asyncio.run(roles.self_role(GROUP)) == ROLE_UNKNOWN
    assert asyncio.run(roles.is_owner(GROUP)) is False


def test_no_action_channel_at_all_fails_closed() -> None:
    """没有通道（`None`）时**不是抛异常**，而是"一律不知道"。"""

    roles = GroupRoles(None)

    assert roles.enabled is False
    assert asyncio.run(roles.role(GROUP, SOMEONE)) == ROLE_UNKNOWN
    assert asyncio.run(roles.self_role(GROUP)) == ROLE_UNKNOWN
    assert asyncio.run(roles.at_least_admin(GROUP, SOMEONE)) is False
    assert roles.cached_self_id() == ""


def test_an_empty_group_id_fails_closed_and_does_not_call_out() -> None:
    """群号是空的就根本别发查询——发出去只会白挨一次失败。"""

    actions = _FakeActions(role=ROLE_OWNER)
    roles = _roles(actions)

    assert asyncio.run(roles.role("", SOMEONE)) == ROLE_UNKNOWN
    assert actions.calls == []


def test_a_failed_query_is_not_cached_as_a_fact() -> None:
    """**失败不留痕**：拿不到就没缓存，下一次照样会去问，而不是把"查不到"钉住。"""

    actions = _FakeActions(role=ROLE_OWNER, explode={"get_group_member_info"})
    roles = _roles(actions)

    asyncio.run(roles.role(GROUP, SOMEONE))
    actions.explode.clear()
    # 同一个人再问一次：应当真的再查一次，然后拿到事实。
    assert asyncio.run(roles.role(GROUP, SOMEONE)) == ROLE_OWNER


# --- 3. 缓存与失效 -------------------------------------------------------

def test_a_fresh_fact_is_not_queried_twice() -> None:
    now = [1000.0]
    actions = _FakeActions(role=ROLE_ADMIN)
    roles = _roles(actions, ttl=900.0, clock=lambda: now[0])

    assert asyncio.run(roles.role(GROUP, SOMEONE)) == ROLE_ADMIN
    assert asyncio.run(roles.role(GROUP, SOMEONE)) == ROLE_ADMIN
    member_calls = [c for c in actions.calls if c[0] == "get_group_member_info"]
    assert len(member_calls) == 1, f"TTL 内查了 {len(member_calls)} 次"


def test_a_stale_fact_is_queried_again() -> None:
    now = [1000.0]
    actions = _FakeActions(role=ROLE_ADMIN)
    roles = _roles(actions, ttl=900.0, clock=lambda: now[0])

    asyncio.run(roles.role(GROUP, SOMEONE))
    now[0] += 901.0                       # 过 TTL
    actions.role = ROLE_OWNER             # 对面那边升成群主了
    assert asyncio.run(roles.role(GROUP, SOMEONE)) == ROLE_OWNER, "过期必须重取，不能一直用旧值"


def test_refresh_forces_a_new_lookup() -> None:
    actions = _FakeActions(role=ROLE_MEMBER)
    roles = _roles(actions)

    assert asyncio.run(roles.role(GROUP, SOMEONE)) == ROLE_MEMBER
    actions.role = ROLE_OWNER
    assert asyncio.run(roles.role(GROUP, SOMEONE, refresh=True)) == ROLE_OWNER


def test_forget_drops_one_person_one_group_or_everything() -> None:
    actions = _FakeActions(role=ROLE_ADMIN)
    roles = _roles(actions)

    asyncio.run(roles.role(GROUP, SOMEONE))
    asyncio.run(roles.role(OTHER_GROUP, SOMEONE))
    assert set(roles.snapshot()) == {f"{GROUP}:{SOMEONE}", f"{OTHER_GROUP}:{SOMEONE}"}

    roles.forget(GROUP, SOMEONE)                       # 只清这一个人的这一个群
    assert set(roles.snapshot()) == {f"{OTHER_GROUP}:{SOMEONE}"}

    roles.forget(GROUP)                                # 清这个群（这里已经没有了）
    assert set(roles.snapshot()) == {f"{OTHER_GROUP}:{SOMEONE}"}

    roles.forget()                                     # 全清
    assert roles.snapshot() == {}


def test_no_cache_is_always_requested() -> None:
    """**必须带 `no_cache`**（2026-09-30 真机被 NapCat 自己的缓存骗过）。"""

    actions = _FakeActions(role=ROLE_ADMIN)
    asyncio.run(_roles(actions).role(GROUP, SOMEONE))
    _, params = actions.calls[-1]
    assert params.get("no_cache") is True


def test_prime_fills_the_cache_for_a_batch_and_has_a_budget() -> None:
    """批量取事实：`prime` 之后 `known_role` / `labels_for` 是**纯内存**读。"""

    actions = _FakeActions(role=ROLE_ADMIN)
    roles = _roles(actions)
    people = [f"9000000{i:02d}" for i in range(5)]

    assert roles.known_role(GROUP, people[0]) == ROLE_UNKNOWN, "prime 之前是不知道"
    asyncio.run(roles.prime(GROUP, people))
    for user in people:
        assert roles.known_role(GROUP, user) == ROLE_ADMIN
    assert roles.labels_for(GROUP, people) == {u: ROLE_LABELS[ROLE_ADMIN] for u in people}

    before = len([c for c in actions.calls if c[0] == "get_group_member_info"])
    asyncio.run(roles.prime(GROUP, people))
    after = len([c for c in actions.calls if c[0] == "get_group_member_info"])
    assert after == before, "缓存还新鲜时 prime 不该重复发查询"

    # 预算：超过 limit 的那部分**不查**（记为 unknown），而不是把这一轮拖成 N 次往返。
    big = [f"8000000{i:02d}" for i in range(MAX_PRIME_USERS + 3)]
    asyncio.run(roles.prime(GROUP, big, limit=2))
    assert roles.known_role(GROUP, big[0]) == ROLE_ADMIN
    assert roles.known_role(GROUP, big[1]) == ROLE_ADMIN
    assert roles.known_role(GROUP, big[2]) == ROLE_UNKNOWN


def test_labels_for_never_invents_a_role_for_unknown_people() -> None:
    """没取到事实的人**不出现在结果里**——调用方按"不知道"处理，不填默认值。"""

    roles = _roles(_FakeActions(role=ROLE_MEMBER))
    assert roles.labels_for(GROUP, ["123", "456"]) == {}
    asyncio.run(roles.prime(GROUP, ["123"]))
    labels = roles.labels_for(GROUP, ["123", "456"])
    assert labels == {"123": ROLE_LABELS[ROLE_MEMBER]}, "456 没查过，不许出现"


# --- 4. 接进核心（装配）--------------------------------------------------

class _FakeTransport:
    """够 `build_engine` 装配的最小传输层；`role` 由脚本决定。"""

    def __init__(self, *, role=ROLE_OWNER, explode=()) -> None:
        self.role = role
        self.explode = set(explode)
        self.calls: list[tuple[str, dict]] = []

    async def call_api(self, action, params=None):
        params = dict(params or {})
        self.calls.append((action, params))
        if action in self.explode:
            raise RuntimeError(f"对面不干了：{action}")
        if action == "get_login_info":
            return {"status": "ok", "retcode": 0,
                    "data": {"user_id": ME, "nickname": "YunRu"}}
        if action == "get_group_member_info":
            data = {"user_id": params.get("user_id")}
            if self.role is not None:
                data["role"] = self.role
            return {"status": "ok", "retcode": 0, "data": data}
        return {"status": "ok", "retcode": 0, "data": {}}

    async def send(self, target, text, **kwargs):  # pragma: no cover - 装配不需要
        return None

    async def start(self):  # pragma: no cover
        return None


def test_build_engine_gives_plugins_one_shared_role_source() -> None:
    """插件问"某人是不是管理员"时，拿到的**就是核心这一份**（`shared_roles`）。

    这是"插件不自己取"那条要求的落点：接缝上只有一个对象，且它答得出
    **别人**的角色（插件侧原来那份只答得出她自己的）。
    """

    engine = runtime.build_engine(_FakeTransport(role=ROLE_ADMIN))
    shared = engine.plugin_registry.shared_roles()

    assert shared is engine.group_roles, "共享位与引擎上必须是同一个对象（单源）"
    assert asyncio.run(shared.at_least_admin(GROUP, SOMEONE)) is True
    assert asyncio.run(shared.role(GROUP, SOMEONE)) == ROLE_ADMIN


def test_the_engine_reports_her_id_after_she_is_looked_up() -> None:
    """面板那条**同步**接缝（`ui.self_id`）在查过她自己之后要拿得到号。

    落点是 `_LazyGroupRoles.cached_self_id()`：懒构造出来的服务必须把"查到过的
    她自己"透出去——否则面板永远读到空串（那条接缝不能 await）。
    """

    engine = runtime.build_engine(_FakeTransport(role=ROLE_OWNER))
    read_self = engine.plugin_registry.ui.self_id

    assert read_self() == "", "还没查过就是空串"
    assert asyncio.run(engine.group_roles.self_role(GROUP)) == ROLE_OWNER
    assert read_self() == ME, "查过之后同步接缝要拿得到"
    assert isinstance(read_self(), str), "**不能**把协程对象漏出去"


def test_the_engines_role_source_fails_closed_when_the_channel_is_broken() -> None:
    """通道坏了（对面每次查询都抛）时，引擎上那一份也必须 fail-closed。"""

    engine = runtime.build_engine(_FakeTransport(explode={"get_group_member_info"}))
    shared = engine.plugin_registry.shared_roles()

    assert asyncio.run(shared.role(GROUP, SOMEONE)) == ROLE_UNKNOWN
    assert asyncio.run(shared.at_least_admin(GROUP, SOMEONE)) is False
    assert asyncio.run(shared.is_owner(GROUP, SOMEONE)) is False


def test_the_lazy_wrapper_degrades_when_the_module_is_missing() -> None:
    """模块不在时:一律 fail-closed，且**不抛异常**。

    这条是判据"删掉 `group_roles` 核心照样起得来"在**行为上**的对应物
    （`tests/check_module_removal.py` 查的是装配那一头）。

    做法与 `test_optional_capabilities` 同一条路子：把模块在 `sys.modules` 里置成
    `None`，CPython 随后对它的 import 就抛 `ImportError`。**不碰磁盘、跑完自动还原。**
    """

    engine = runtime.build_engine(_FakeTransport(role=ROLE_OWNER))
    lazy = engine.group_roles
    assert lazy.ready is None, "懒构造：还没人用过它，就不该已经造出来"

    saved = sys.modules.get("qq_roleplay_bot.group_roles")
    sys.modules["qq_roleplay_bot.group_roles"] = None
    try:
        assert asyncio.run(lazy.role(GROUP, SOMEONE)) == ROLE_UNKNOWN
        assert asyncio.run(lazy.self_role(GROUP)) == ROLE_UNKNOWN
        assert asyncio.run(lazy.at_least_admin(GROUP, SOMEONE)) is False
        assert asyncio.run(lazy.is_owner(GROUP, SOMEONE)) is False
        assert lazy.cached_self_id() == ""
        assert lazy.labels_for(GROUP, [SOMEONE]) == {}
        assert lazy.known_role(GROUP, SOMEONE) == ROLE_UNKNOWN
        lazy.forget(GROUP)                                  # 不该炸
    finally:
        if saved is None:
            sys.modules.pop("qq_roleplay_bot.group_roles", None)
        else:
            sys.modules["qq_roleplay_bot.group_roles"] = saved
