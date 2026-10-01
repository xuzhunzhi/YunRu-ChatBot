"""装配层的接线测试：**真装配**（`runtime.build_engine`）出来东西能不能用。

由来（2026-09-30 用户："又查不到自己身份了"）：这块角色查询原来由 `_start_join_approval`
顺手创建，后来那个函数搬进 `background_plugins.py` 时**只搬了用法、没搬创建**，
`engine.self_roles` 于是永远是 `None` —— 群主命令全回"我在这个群里是查不到"，
入群审批因为拿不到角色**静默地一条都不处理**。

单元测试抓不到这类问题：每个模块各自都对，坏在装配。所以这里直接走
`build_engine(transport)`，用假传输层看它到底装上了什么、命令发出去调了什么。
"""
import asyncio

from qq_roleplay_bot import runtime
from qq_roleplay_bot.background_plugins import build_background_plugins
from qq_roleplay_bot.qq_roles import ROLE_MEMBER, ROLE_OWNER
from qq_roleplay_bot.transport import IncomingMessage, MessageTarget

GROUP = "717151356"
ME = "900000001"
TARGET = "900000005"


class FakeTransport:
    """假传输层：回答登录信息与成员角色（`role` 可指定），其余记下来。"""

    def __init__(self, role: str = ROLE_OWNER, login: str = "900000002") -> None:
        self.role = role
        self.login = login
        self.calls: list[tuple[str, dict]] = []

    async def call_api(self, action, params=None):
        self.calls.append((action, dict(params or {})))
        if action == "get_login_info":
            return {"status": "ok", "retcode": 0, "data": {"user_id": self.login}}
        if action == "get_group_member_info":
            return {"status": "ok", "retcode": 0, "data": {"role": self.role}}
        return {"status": "ok", "retcode": 0, "data": None}

    async def send(self, target, text, *, reply_to=""):
        return None


def message(text: str, *, mentions=(), user_id: str = ME) -> IncomingMessage:
    return IncomingMessage(message_id=f"m:{text}", session_id=f"group:{GROUP}", user_id=user_id,
                           text=text, target=MessageTarget(group_id=GROUP),
                           mentioned_user_ids=tuple(mentions))


def test_build_engine_wires_the_role_cache() -> None:
    """真装配必须把"她自己的角色"装上——这是群主命令与入群审批的共同前提。"""

    transport = FakeTransport()
    engine = runtime.build_engine(transport)
    assert engine.self_roles is not None, "装配漏了 SelfRoleCache（真机踩过：命令全变'查不到'）"
    assert asyncio.run(engine.self_roles.role(GROUP)) == ROLE_OWNER


def test_assembled_engine_can_actually_title_and_appoint() -> None:
    """从真装配出来的引擎，命令要一路走到 action。"""

    transport = FakeTransport()
    engine = runtime.build_engine(transport)

    result = asyncio.run(engine.handle(message("/super qqadmin", mentions=(TARGET,))))
    assert result is not None and "已设置" in result.text
    assert [name for name, _ in transport.calls] == [
        "get_login_info", "get_group_member_info", "set_group_admin"]
    assert transport.calls[-1][1] == {"group_id": int(GROUP), "user_id": int(TARGET),
                                      "enable": True}


def test_assembled_engine_says_so_when_she_is_not_the_owner() -> None:
    transport = FakeTransport(role=ROLE_MEMBER)
    engine = runtime.build_engine(transport)
    result = asyncio.run(engine.handle(message("/title 龙王")))
    assert result is not None and "做不了" in result.text
    assert all(name != "set_group_special_title" for name, _ in transport.calls)


def test_approval_plugin_refuses_to_load_without_a_role_source() -> None:
    """没有角色来源就不装审批插件（fail-closed，而且**出声**）。

    以前这里是静默的：插件照装，`_may_approve` 永远 False，一条申请都不处理。
    """

    from qq_roleplay_bot import dev_config

    original = dev_config.AUTO_APPROVE_JOIN
    dev_config.AUTO_APPROVE_JOIN = True
    try:
        with_roles = build_background_plugins(_Engine(), call_action=lambda *a: None,
                                              notify=lambda *a: None, roles=object())
        without_roles = build_background_plugins(_Engine(), call_action=lambda *a: None,
                                                 notify=lambda *a: None, roles=None)
    finally:
        dev_config.AUTO_APPROVE_JOIN = original
    assert any(plugin.name == "join-approval" for plugin in with_roles)
    assert all(plugin.name != "join-approval" for plugin in without_roles)


class _Engine:
    """装配点只用到 `daily_reporter` 与 `note_letter`，这里给出最小形状。"""

    daily_reporter = None

    def note_letter(self, letter):  # pragma: no cover - 只有装了日报才会用到
        return None
