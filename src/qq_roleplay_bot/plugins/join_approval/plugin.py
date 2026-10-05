"""入群审批插件：按 白名单 → 黑名单 → 正则 → 模型兜底 处理入群申请。

**后台插件**（按节拍去问有没有待处理的申请）。这段装配逻辑原来在
`background_plugins._join_approval_plugin()` 里，**原样搬过来**，只把函数名改成
`build`、把参数从关键字参数改成从 `registry` 取——避免搬家时改坏行为。

## 角色来源：2026-10-05 起**先问核心**

原来本插件声明 `REQUIRES = ("roles",)`，向**另一个插件**（`plugins/roles/`）要
"她在这个群里是什么角色"。用户 2026-10-05 的决定是**身份/权限事实归核心**，
所以那条依赖撤了：核心把角色来源注入到 `registry.roles` 上（`PluginRegistry`
本来就有这个槽位，`tests/plugin_support.plugin_registry(roles=…)` 也是这么给的），
本插件**只认核心那一份**，不自己查、不自己缓存。

**过渡（要删的那一半）**：本体那边还在做"角色进核心"，此刻 `registry.roles` 仍是
`None`，所以 `_RoleSource` 会退回 `registry.shared_roles()`（`plugins/roles/` 插件
放上来的那一份）。为什么必须**延后到真正用它的那一刻**再取、而不是在 `register()`
里取一次：`discover()` 按名字序装插件，`join_approval` 排在 `roles` **前面**，
注册那一刻两份来源都还是空的——以前靠 `REQUIRES` 把顺序钉死，那条依赖撤掉之后
顺序不再由我们控制。核心那边落地、`plugins/roles/` 删掉之后，
`_RoleSource.current()` 里那一行退回**要删掉**（`tests/test_background_plugins.py`
里那条"核心给了就只认核心的"钉着这个方向）。

拿不到角色来源时不装或不做都由**核心那边**决定得更早：这里一律 **fail-closed**——
`_RoleSource` 返回空角色，`JoinApprovalPoller._may_approve` 于是不批任何一条，
并且**第一次**遇到这种情况会喊一声（不是每轮都喊）。
"""
from __future__ import annotations

import logging

from ... import dev_config
from .background import JoinApprovalPlugin

logger = logging.getLogger(__name__)


class _RoleSource:
    """"她在某个群里是什么角色"的来源：**核心优先**，过渡期才看插件那一份。

    形状与 `plugins/roles/roles.py::SelfRoleCache` 一样只有 `role(group_id)`，
    因为使用者（`JoinApprovalPoller._may_approve`）只问这一个问题。
    """

    __slots__ = ("_registry", "_warned")

    def __init__(self, registry) -> None:
        self._registry = registry
        self._warned = False

    def current(self) -> object | None:
        """此刻该用哪一份：核心注入的优先；核心那份不在时才是过渡那一份。"""

        core = getattr(self._registry, "roles", None)
        if core is not None:
            return core
        return self._registry.shared_roles()

    async def role(self, group_id: str, **kwargs: object) -> str:
        """她在 `group_id` 的角色；没有来源时返回空串（fail-closed，不猜）。"""

        source = self.current()
        if source is None:
            if not self._warned:      # 只喊一次：每轮都喊会把日志淹掉
                self._warned = True
                logger.warning(
                    "join_approval_no_role_source：没有任何角色来源"
                    "（核心还没注入 registry.roles、plugins/roles/ 也不在），"
                    "从这一轮起一条申请都不会被批（fail-closed）")
            return ""
        return await source.role(group_id, **kwargs)  # type: ignore[attr-defined]


def build(*, call_action, notify, roles):
    """装配入群审批：策略（配置）＋轮询器＋节拍。不满足条件返回 None。"""

    if not dev_config.AUTO_APPROVE_JOIN:
        logger.info("入群申请自动审批未启用（QQBOT_AUTO_APPROVE_JOIN=0）")
        return None
    if roles is None:
        # **没有角色来源就不装这个插件**（fail-closed 并且**出声**）：
        # 拿不到"她在那个群是什么角色"时，审批会一条都不处理——以前这是静默的，
        # 真机上表现为"审批好像没开"，查起来很难（2026-09-30 踩过）。
        logger.warning("入群申请自动审批没有角色查询能力（engine.self_roles 为空），本次不启用")
        return None
    from .join_approval import JoinApprovalPolicy, JoinApprovalPoller

    policy = JoinApprovalPolicy(
        whitelist=dev_config.APPROVE_WHITELIST,
        blacklist=dev_config.APPROVE_BLACKLIST,
        pattern=dev_config.APPROVE_PATTERN,
        reject_reason=dev_config.APPROVE_REJECT_REASON,
    )
    poller = JoinApprovalPoller(
        call_action=call_action,
        notify=notify,
        policy=policy,
        roles=roles,
        enabled=True,
        max_per_tick=dev_config.APPROVE_MAX_PER_TICK,
    )
    logger.info("入群申请自动审批已就绪：每 %s 秒看一次；判据 %s",
                int(dev_config.APPROVE_POLL_SECONDS), policy.explain)
    return JoinApprovalPlugin(poller, interval_seconds=dev_config.APPROVE_POLL_SECONDS)


def register(registry) -> None:
    # 角色来源**先问核心**（`registry.roles`），延后到真正用它的那一刻取——
    # 见本模块 docstring 的"角色来源"与 `_RoleSource`。
    registry.background(build(call_action=registry.call_action, notify=registry.notify,
                              roles=_RoleSource(registry)))
