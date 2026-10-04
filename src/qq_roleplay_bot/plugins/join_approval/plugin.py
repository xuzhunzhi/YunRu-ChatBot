"""入群审批插件：按 白名单 → 黑名单 → 正则 → 模型兜底 处理入群申请。

**后台插件**（按节拍去问有没有待处理的申请）。这段装配逻辑原来在
`background_plugins._join_approval_plugin()` 里，**原样搬过来**，只把函数名改成
`build`、把参数从关键字参数改成从 `registry` 取——避免搬家时改坏行为。
"""
from __future__ import annotations

import logging

from ... import dev_config
from ...background_plugins import JoinApprovalPlugin

logger = logging.getLogger(__name__)

#: 前置插件：她自己的群角色查询。装不上就不装本插件（没有它一条申请都不该处理）。
REQUIRES = ("roles",)


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
    # 角色来源由前置插件 `roles` 提供（判"她能不能批申请"）。
    registry.background(build(call_action=registry.call_action, notify=registry.notify,
                              roles=registry.shared_roles()))
