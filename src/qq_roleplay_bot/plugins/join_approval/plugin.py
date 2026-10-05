"""入群审批插件：按 白名单 → 黑名单 → 正则 → 模型兜底 处理入群申请。

**后台插件**（按节拍去问有没有待处理的申请）。这段装配逻辑原来在
`background_plugins._join_approval_plugin()` 里，**原样搬过来**，只把函数名改成
`build`、把参数从关键字参数改成从 `registry` 取——避免搬家时改坏行为。

## 角色来源：2026-10-05 起由**核心**放到共享位上

原来本插件声明 `REQUIRES = ("roles",)`，向**另一个插件**（`plugins/roles/`）要
"她在这个群里是什么角色"。用户 2026-10-05 的决定是**身份/权限事实归核心**
（本体 `1c5fcf3`：新增核心的 `group_roles.py`，`runtime.build_engine` 在 `discover()`
**之前**就 `registry.provide_roles(engine.group_roles)`），所以那条依赖撤了：
角色事实由核心放进**既有那个共享位**，本插件问 `registry.shared_roles()` 就行——
**不自己查、不自己缓存、也不管是谁放上来的**。

**为什么 `_RoleSource` 要延后到真正用它的那一刻再取**（这是这次最容易踩的一脚）：
`discover()` 按名字序装插件，`join_approval` 排在 `roles` **前面**。以前靠 `REQUIRES`
把顺序钉死；依赖撤掉之后，若在 `register()` 那一刻取一次，在本体那份落地之前会拿到
`None` → 插件静默不装（真机形态："审批好像没开"）。延后之后，装配期与运行期解耦：
来源什么时候出现都不影响装配，而且**核心现在是在 `discover()` 之前放好的**，
正常情况下第一轮就取得到。

拿不到角色来源时一律 **fail-closed**：`_RoleSource` 返回空角色，
`JoinApprovalPoller._may_approve` 于是不批任何一条，并且**第一次**遇到会喊一声
（不是每轮都喊）。
"""
from __future__ import annotations

import logging

from ... import dev_config
from .background import JoinApprovalPlugin

logger = logging.getLogger(__name__)


class _RoleSource:
    """"她在某个群里是什么角色"的来源：**共享位上那一份**（现在由核心放上来）。

    只依赖形状里那一个问题 `role(group_id)`——使用者
    （`JoinApprovalPoller._may_approve`）只问这一个。核心那份
    （`group_roles.GroupRoles`）有更多方法，但本插件不碰：它只需要"她是群主/管理员吗"
    这一条答案，动词越少越好。
    """

    __slots__ = ("_registry", "_warned")

    def __init__(self, registry) -> None:
        self._registry = registry
        self._warned = False

    def current(self) -> object | None:
        """此刻共享位上那一份；没人放上来就是 `None`（装配顺序不再被假定）。"""

        return self._registry.shared_roles()

    async def role(self, group_id: str, **kwargs: object) -> str:
        """她在 `group_id` 的角色；没有来源时返回空串（fail-closed，不猜）。"""

        source = self.current()
        if source is None:
            if not self._warned:      # 只喊一次：每轮都喊会把日志淹掉
                self._warned = True
                logger.warning(
                    "join_approval_no_role_source：共享位上没有角色来源"
                    "（核心那份没放上来、plugins/roles/ 也不在），"
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
    # 角色来源走**共享位**（`registry.shared_roles()`，现在由核心放上来），
    # 并且延后到真正用它的那一刻取——见本模块 docstring 与 `_RoleSource`。
    registry.background(build(call_action=registry.call_action, notify=registry.notify,
                              roles=_RoleSource(registry)))
