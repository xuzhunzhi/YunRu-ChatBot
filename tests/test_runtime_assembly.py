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
from qq_roleplay_bot.plugins.roles.roles import ROLE_OWNER
from qq_roleplay_bot.transport import IncomingMessage, MessageTarget

GROUP = "717151356"
ME = "900000001"
TARGET = "900000007"


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


def test_the_core_and_the_plugins_share_one_role_cache() -> None:
    """**单一来源**（2026-10-04 对齐）：核心与插件用的是**同一个**角色查询对象。

    改之前是两份：`runtime` 自己 `SelfRoleCache(transport, ...)`，
    `roles` 插件又照核心给的 `call_action` 造一份给 `registry.shared_roles()`。
    两份带各自 TTL 的缓存意味着"她在这个群里是不是群主"有**两个答案**，
    而群主动作读核心那份、入群审批读插件那份（真机踩过的形状：
    一边说能批、另一边说查不到）。现在只有一份：插件造、经
    `registry.provide_roles()` → `chat.roles_sink()` 填回 `engine.self_roles`。
    """

    engine = runtime.build_engine(FakeTransport())
    shared = engine.plugin_registry.shared_roles()
    assert shared is not None, "roles 插件没把角色查询放上注册表"
    assert shared is engine.self_roles, (
        "核心与插件必须是**同一个**角色查询对象，否则'她是不是群主'就有两个真相")
    # 而且它确实在用核心注入的那个通道（走 `call_action`，不是活的 transport）
    assert asyncio.run(shared.role(GROUP)) == ROLE_OWNER


