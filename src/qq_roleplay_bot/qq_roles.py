"""她自己在每个群的 QQ 角色（群主 / 管理员 / 普通成员）。

由来（2026-09-30 用户："我的意思是 yunru 现在是测试群的群主了"）：群主能做的事比
管理员多，而在此之前**她连自己是什么角色都不知道**——`sender_role` 采集的是**别人**的
角色，她自己那一份从来没查过。

实测（2026-09-30，`get_group_member_info` 逐个群读）：QQ 900000002「YunRu」在
717151356「Yunru Bot testing」里是 `owner`，其余 6 个群都是 `member`。

设计取舍：

1. **现查，不落盘**：角色是 QQ 那边的状态，抄进本地状态文件只会过期（她可以在别处
   被提升/降级，群主还能转让）。所以只放内存，带 TTL（默认 15 分钟）。
2. **读不到就是 `unknown`，一律不放行**（fail-closed）：查询失败、通道不支持、
   对面没返回 role，都不猜"大概是群主"。要动群的命令会因此被挡下并说明原因。
3. **只读动作**：`get_group_member_info` 本来就在 `READ_ACTIONS` 里，这里不新增任何写权限。
"""
from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

ROLE_OWNER = "owner"
ROLE_ADMIN = "admin"
ROLE_MEMBER = "member"
ROLE_UNKNOWN = "unknown"

# QQ 侧的三种角色；大小写与别名都归一到这里。
_ROLE_ALIASES = {
    "owner": ROLE_OWNER, "群主": ROLE_OWNER, "创建者": ROLE_OWNER,
    "admin": ROLE_ADMIN, "administrator": ROLE_ADMIN, "管理": ROLE_ADMIN,
    "管理员": ROLE_ADMIN,
    "member": ROLE_MEMBER, "成员": ROLE_MEMBER, "普通成员": ROLE_MEMBER,
}

# 角色说明（回话用；出站是纯文本，不用 Markdown）。
ROLE_LABELS = {
    ROLE_OWNER: "群主",
    ROLE_ADMIN: "管理员",
    ROLE_MEMBER: "普通成员",
    ROLE_UNKNOWN: "查不到",
}


def normalize_role(raw: object) -> str:
    """把 QQ 返回的 role 归一；认不出来就是 `unknown`。"""

    return _ROLE_ALIASES.get(str(raw or "").strip().casefold(), ROLE_UNKNOWN)


class SelfRoleCache:
    """按群缓存"她自己是什么角色"。"""

    def __init__(self, client, *, ttl: float = 900.0, clock=time.time) -> None:
        """`client` 可以是 HTTP 客户端（`call`）或传输层（`call_api`），返回形状由
        `onebot_client.call_channel` 统一——这里踩过一次"只认 call"的坑：
        生产传的是 WS 传输层，于是每次查询都在 `getattr(client, "call")` 处静默失败，
        她被判成"查不到角色"，日志里连一条记录都没有。"""

        self.client = client
        self.ttl = max(1.0, float(ttl))
        self.clock = clock
        self._roles: dict[str, tuple[str, float]] = {}
        self._self_id = ""

    @property
    def enabled(self) -> bool:
        return self.client is not None

    async def self_id(self) -> str:
        """她自己的 QQ 号。只查一次并记住（登录信息在一次会话里不会变）。"""

        if self._self_id:
            return self._self_id
        if not self.enabled:
            return ""
        try:
            from .onebot_client import call_channel

            data = await call_channel(self.client, "get_login_info", {})
        except Exception as exc:  # noqa: BLE001 - 拿不到就当没有，调用方 fail-closed
            logger.warning("self_role_login_failed category=%s", type(exc).__name__)
            return ""
        if isinstance(data, dict):
            self._self_id = str(data.get("user_id") or "").strip()
        if not self._self_id:
            # **这条日志是为了不再静默**：上一版在这里直接返回空，结果真机上只看到
            # "我在这个群里是查不到"，日志里什么都没有，定位花了很久。
            logger.warning("self_role_self_id_missing payload_type=%s", type(data).__name__)
        return self._self_id

    async def role(self, group_id: str, *, refresh: bool = False) -> str:
        """她在 `group_id` 的角色。查不到返回 `unknown`（不抛异常）。"""

        key = str(group_id or "").strip()
        if not key or not self.enabled:
            return ROLE_UNKNOWN
        cached = self._roles.get(key)
        if cached is not None and not refresh and self.clock() - cached[1] < self.ttl:
            return cached[0]
        user_id = await self.self_id()
        if not user_id:
            return ROLE_UNKNOWN
        try:
            from .onebot_client import call_channel

            # **必须 `no_cache`**（2026-09-30 真机踩过）：NapCat 的 `get_group_member_info`
            # 默认走缓存，读到的 role 可能是**变更前**的。诊断那次就是被它骗了——
            # 头衔其实已经写进去了，缓存读回来却是空的、角色还是 member，于是误判成"没生效"。
            data = await call_channel(
                self.client, "get_group_member_info",
                {"group_id": _as_int(key), "user_id": _as_int(user_id), "no_cache": True},
            )
        except Exception as exc:  # noqa: BLE001 - 查询失败不能让命令抛出去
            logger.warning("self_role_query_failed group=%s category=%s", key, type(exc).__name__)
            return ROLE_UNKNOWN
        role = normalize_role((data or {}).get("role") if isinstance(data, dict) else "")
        if role == ROLE_UNKNOWN:
            logger.warning("self_role_unknown group=%s payload_keys=%s",
                           key, sorted(data)[:8] if isinstance(data, dict) else type(data).__name__)
            return ROLE_UNKNOWN
        self._roles[key] = (role, self.clock())
        return role

    async def is_owner(self, group_id: str) -> bool:
        return await self.role(group_id) == ROLE_OWNER

    async def at_least_admin(self, group_id: str) -> bool:
        """群主或管理员。QQ 侧这两者都能改群名片、发群公告。"""

        return await self.role(group_id) in {ROLE_OWNER, ROLE_ADMIN}

    def forget(self, group_id: str = "") -> None:
        """丢掉缓存（角色可能刚变过，或者刚被对面拒绝）。"""

        if group_id:
            self._roles.pop(str(group_id), None)
        else:
            self._roles.clear()

    def snapshot(self) -> dict[str, str]:
        return {group: role for group, (role, _) in self._roles.items()}


def _as_int(value: object) -> int | str:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return str(value).strip()
