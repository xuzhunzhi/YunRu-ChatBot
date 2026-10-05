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
# 角色事实在核心（2026-10-05："行，进核心"）：常量和实现都从 `group_roles` 取，
# `plugins/roles/` 已经删掉。
from qq_roleplay_bot.group_roles import ROLE_OWNER
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
    assert engine.group_roles is not None, (
        "装配漏了核心的角色事实服务（真机踩过：命令全变'查不到'）")
    assert asyncio.run(engine.group_roles.self_role(GROUP)) == ROLE_OWNER


def test_the_core_and_the_plugins_share_one_role_cache() -> None:
    """**单一来源**：核心与插件用的是**同一个**角色事实服务。

    历史（两层都踩过）：

    - 2026-10-04 之前是两份：`runtime` 自己造一份，`roles` 插件又照核心给的
      `call_action` 造一份给 `registry.shared_roles()`；
    - 2026-10-05 用户拍板"身份/权限事实进核心"（本体 `1c5fcf3`）之后，
      `plugins/roles/` 那份**反而成了第三者**：`provide_roles` 后到者覆盖先到者，
      插件一装上就把核心那份替换掉（实测过：核心 `tests/test_group_roles.py` 四条
      当场报 `SelfRoleCache` 没有 `self_role` / `ready`）。所以那个文件夹**删掉了**。

    现在只有一份：核心造的 `engine.group_roles`，在 `discover()` **之前**经
    `registry.provide_roles(...)` 放到共享位上——插件问 `registry.shared_roles()`
    拿到的就是它，没有第二个提供者。
    """

    engine = runtime.build_engine(FakeTransport())
    shared = engine.plugin_registry.shared_roles()
    assert shared is not None, "核心没把角色事实服务放到共享位上"
    assert shared is engine.group_roles, (
        "核心与插件必须是**同一个**角色事实服务，否则'她是不是群主'就有两个真相")
    # 而且它确实在用核心注入的那个通道（走 `call_action`，不是活的 transport）
    assert asyncio.run(shared.role(GROUP)) == ROLE_OWNER


