"""群成员身份事实：**某人在某群是什么角色**，以及**她自己**在那个群的角色。

由来：这个能力原来长在插件侧（`plugins/roles/`，核心这边留一份同名死代码）。
2026-10-06 用户拍板 **"行，进核心"**——理由是身份判定属于"权限判定/护栏"那一类，
按 `AGENTS.md` §2.3 **一律留在核心**；而且插件侧那一份只回答"她自己是什么角色"，
"对方是管理员还是群主"根本没得问。

## 它回答哪两件事（就这两件，不加第三件）

| 问题 | 接口 |
| --- | --- |
| **某个人**在某个群是群主 / 管理员 / 普通成员 | `await roles.role(group_id, user_id)` |
| **她自己**在某个群是什么角色 | `await roles.self_role(group_id)`（= `role(group_id, self_id)`） |

再加两个**只读**的判断题（都是上面两条的组合，不再单独查一次）：
`is_admin(group_id, user_id)`、`is_owner(group_id, user_id)`。

## 事实从哪来

只走核心既有的 action 通道：调用方注入一个 `call_action(action, params)`，
它已经过 `capabilities` 闸门、回执也解包好了（`runtime._SeamBinder.action_caller("read")`）。
**只用只读动作**：`get_login_info` 与 `get_group_member_info`，两条本来就在
`capabilities.READ_ACTIONS` 里——这里不新增任何写权限，也不持有 transport。

## 缓存与失效（照搬插件侧的做法，理由也照搬）

| 项 | 策略 | 为什么 |
| --- | --- | --- |
| 某人在某群的角色 | 内存字典 `{(group,user): (role, at)}`，TTL 900 秒 | 角色是 QQ 那边的状态，**抄进本地文件只会过期**（可以在别处被提升/降级，群主还能转让）。所以只放内存。 |
| 她自己是谁 | 只查一次并记住 | 登录信息在一次会话里不会变。 |
| 过期后 | **下一个问的人触发现查**（懒刷新），不是定时器 | 没有后台节拍要养；没人问就不查。 |
| 主动失效 | `forget(group_id, user_id="")` | 对面明确拒绝（retcode=100）通常就是"她其实不是管理员了"，那时候必须丢掉旧值而不是继续用满 TTL。 |
| `no_cache=True` | 每次查询都带上 | 2026-09-30 真机踩过：NapCat 的 `get_group_member_info` **默认走它自己的缓存**，读回来的 role 可能是变更前的——诊断那次就被它骗了。 |

## fail-closed（这条是硬的）

**拿不到事实时一律是 `unknown`，绝不猜。** 查不到、通道不支持、对面没返回 role、
没有注入 `call_action`、查询抛异常——全部归到 `ROLE_UNKNOWN`。
`is_admin` / `is_owner` 对 `unknown` 一律返回 `False`。要动群的动作因此会被挡下，
而不是"大概是管理员就放行"。

`tests/test_group_roles.py` 里有专门的用例钉这一条（含"查询抛异常"与"payload 没有 role"
两种拿不到的形状）；把 `unknown` 改成放行会让它们变红。
"""
from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

ROLE_OWNER = "owner"
ROLE_ADMIN = "admin"
ROLE_MEMBER = "member"
ROLE_UNKNOWN = "unknown"

# QQ 侧的三种角色；大小写与别名都归一到这里。认不出来的**一律 unknown**（fail-closed）。
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


#: 名册那一轮最多查几个人（第二道预算；第一道是名册本身的行数上限 `ROSTER_MAX_LINES`）。
#: 放在这里而不是类属性：它是 `prime()` 的默认参数，要在类体求值之前就存在。
MAX_PRIME_USERS = 20


def normalize_role(raw: object) -> str:
    """把 QQ 返回的 role 归一；认不出来就是 `unknown`。"""

    return _ROLE_ALIASES.get(str(raw or "").strip().casefold(), ROLE_UNKNOWN)


def _as_int(value: object) -> int | str:
    """QQ 号/群号优先按整数发出去；真不是数字就原样发（对面自己会拒）。"""

    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return str(value).strip()


class GroupRoles:
    """群成员角色事实服务。**只有只读查询**，缓存见模块 docstring。"""

    def __init__(self, call_action, *, ttl: float = 900.0, clock=time.time) -> None:
        """`call_action(action, params)`：**核心注入的、已过闸门的调用函数**。

        它必须是可调用的（`runtime._SeamBinder.action_caller("read")` 给的就是）。
        传 `None` 是允许的，那表示"这台机器没有动作通道"——此时 `enabled` 为假、
        一切查询都返回 `unknown`（fail-closed），而不是抛异常。

        **不接受裸传输层**：那条路不过 `capabilities` 闸门。插件侧那份缓存当初就是
        因为多留了一个"传输层形态"被审查者点过——"装了但走不到、走得到时又没有闸门"
        的形状不该留。这里从一开始就不认它。
        """

        if call_action is not None and not callable(call_action):
            raise TypeError(
                "GroupRoles 只接受 None（没有动作通道）或一个可调用的 "
                "`call_action(action, params)`（核心注入、已过闸门）；"
                f"**不接受裸传输层**——拿到的是 {type(call_action).__name__}")
        self.call_action = call_action
        self.ttl = max(1.0, float(ttl))
        self.clock = clock
        #: `{(group_id, user_id): (role, at)}`——**某人在某群**的角色。
        self._roles: dict[tuple[str, str], tuple[str, float]] = {}
        #: 她自己的 QQ 号（只成功查到一次就记住）。
        self._self_id = ""

    @property
    def enabled(self) -> bool:
        return self.call_action is not None

    # --- 通道 -------------------------------------------------------------

    async def _query(self, action: str, params: dict[str, object]) -> object:
        """发一次查询。**唯一的出口**（查询失败由调用方 fail-closed）。"""

        return await self.call_action(action, params)

    # --- 她是谁 -----------------------------------------------------------

    async def self_id(self) -> str:
        """她自己的 QQ 号。查不到返回空串（调用方一律按 `unknown` 处理）。"""

        if self._self_id:
            return self._self_id
        if not self.enabled:
            return ""
        try:
            data = await self._query("get_login_info", {})
        except Exception as exc:  # noqa: BLE001 - 拿不到就当没有，调用方 fail-closed
            logger.warning("group_role_login_failed category=%s", type(exc).__name__)
            return ""
        if isinstance(data, dict):
            self._self_id = str(data.get("user_id") or "").strip()
        if not self._self_id:
            # **这条日志是为了不再静默**：插件侧上一版在这里直接返回空，结果真机上只看到
            # "我在这个群里是查不到"，日志里什么都没有，定位花了很久。
            logger.warning("group_role_self_id_missing payload_type=%s", type(data).__name__)
        return self._self_id

    def cached_self_id(self) -> str:
        """**同步**取"已经查到过的"她自己的 QQ 号；没查过就是空串。

        给同步的取用点（面板的 `self_id` 接缝）用：那边不能 await，也不该为了读一个
        已知值去驱动一次网络查询。查过的值后面都会走 `self_id()` 补上。
        """

        return self._self_id

    # --- 事实 -------------------------------------------------------------

    async def role(self, group_id: str, user_id: str = "", *, refresh: bool = False) -> str:
        """`user_id` 在 `group_id` 里的角色。**查不到返回 `unknown`，不抛异常。**

        `user_id` 省略 / 传空串时回答的是**她自己**在那个群的角色（她自己的 QQ 号
        现查一次 `get_login_info`）——这就是"她自己是什么角色"那条路。
        """

        group = str(group_id or "").strip()
        if not group or not self.enabled:
            return ROLE_UNKNOWN
        user = str(user_id or "").strip() or await self.self_id()
        if not user:
            return ROLE_UNKNOWN
        key = (group, user)
        cached = self._roles.get(key)
        if cached is not None and not refresh and self.clock() - cached[1] < self.ttl:
            return cached[0]
        try:
            # **必须 `no_cache`**（2026-09-30 真机踩过）：NapCat 的 `get_group_member_info`
            # 默认走它自己的缓存，读到的 role 可能是**变更前**的。
            data = await self._query(
                "get_group_member_info",
                {"group_id": _as_int(group), "user_id": _as_int(user), "no_cache": True},
            )
        except Exception as exc:  # noqa: BLE001 - 查询失败不能让命令抛出去
            logger.warning("group_role_query_failed group=%s category=%s",
                           group, type(exc).__name__)
            return ROLE_UNKNOWN
        role = normalize_role((data or {}).get("role") if isinstance(data, dict) else "")
        if role == ROLE_UNKNOWN:
            logger.warning("group_role_unknown group=%s payload_keys=%s",
                           group, sorted(data)[:8] if isinstance(data, dict)
                           else type(data).__name__)
            return ROLE_UNKNOWN
        self._roles[key] = (role, self.clock())
        return role

    async def self_role(self, group_id: str, *, refresh: bool = False) -> str:
        """**她自己**在 `group_id` 里的角色。查不到返回 `unknown`。"""

        return await self.role(group_id, "", refresh=refresh)

    async def is_owner(self, group_id: str, user_id: str = "") -> bool:
        """是不是群主。**`unknown` 一律 `False`**（fail-closed）。"""

        return await self.role(group_id, user_id) == ROLE_OWNER

    async def at_least_admin(self, group_id: str, user_id: str = "") -> bool:
        """群主或管理员。QQ 侧这两者都能改群名片、发群公告。

        **`unknown` 一律 `False`**——这条是护栏的判据，不许猜。
        """

        return await self.role(group_id, user_id) in {ROLE_OWNER, ROLE_ADMIN}

    # --- 批量（给名册用）--------------------------------------------------

    async def prime(self, group_id: str, user_ids,
                    *, limit: int = MAX_PRIME_USERS) -> None:
        """把这一批人的角色**取到缓存里**（`role()` 之后就是纯内存读）。

        名册要在**同步**渲染里拿到角色，所以顺序是"先 prime、后渲染"：
        `prime()` 是异步的、`labels_for()` 是同步的。没被取到的（超上限、
        查询失败、认不出来）一律不在结果里——渲染方按"不知道"处理。

        `limit` 是**每轮的查询预算**：名册最多几十人，一人一次网络往返会拖慢回复，
        所以超出的这部分不查（记为 unknown）。真实群聊里 `ROSTER_MAX_LINES` 已经把
        名册本身压住了，这里是第二道。
        """

        group = str(group_id or "").strip()
        if not group or not self.enabled:
            return
        wanted = []
        for raw in user_ids:
            user = str(raw or "").strip()
            if user and user not in wanted:
                wanted.append(user)
        for user in wanted[:max(0, int(limit))]:
            await self.role(group, user)

    def labels_for(self, group_id: str, user_ids) -> dict[str, str]:
        """**同步**读缓存：`{user_id: 角色说明}`，只含**缓存里已经有的**。

        没查到的人**不出现在结果里**——调用方按"不知道"处理，绝不填一个默认值。
        """

        group = str(group_id or "").strip()
        found: dict[str, str] = {}
        for raw in user_ids:
            user = str(raw or "").strip()
            if user and user not in found:
                known = self.known_role(group, user)
                if known != ROLE_UNKNOWN:
                    found[user] = ROLE_LABELS[known]
        return found

    def known_role(self, group_id: str, user_id: str) -> str:
        """缓存里已知的角色；没有 / 过期就是 `unknown`。**不发查询。**"""

        cached = self._roles.get((str(group_id or "").strip(), str(user_id or "").strip()))
        if cached is None or self.clock() - cached[1] >= self.ttl:
            return ROLE_UNKNOWN
        return cached[0]

    # --- 失效 -------------------------------------------------------------

    def forget(self, group_id: str = "", user_id: str = "") -> None:
        """丢掉缓存。角色可能刚变过，或者刚被对面拒绝（retcode=100 就是那个信号）。

        - 两个都空：全清；
        - 只给 `group_id`：清这个群**所有人**（含她自己）；
        - 两个都给：只清这一个人在这个群的那一条。
        """

        group = str(group_id or "").strip()
        user = str(user_id or "").strip()
        if not group:
            self._roles.clear()
            return
        if user:
            self._roles.pop((group, user), None)
            return
        for key in [item for item in self._roles if item[0] == group]:
            self._roles.pop(key, None)

    def snapshot(self) -> dict[str, str]:
        """已知事实的快照 `{"group:user": role}`（诊断/测试用）。"""

        return {f"{group}:{user}": role for (group, user), (role, _) in self._roles.items()}
