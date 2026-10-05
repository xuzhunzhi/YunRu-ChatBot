"""群管理插件：踢 / 禁言 / 撤回 / 头衔 / 公告 / 改名片。

**命令插件**（有消息进来才动）。三条命令只做"解析 + 声明意图"，
真正的权限判定、护栏与调用留在核心的 `stage3_main.execute_action_request`——
插件拿不到 `transport`。

它还负责**创建"她自己的角色"查询**（`_shared/roles.py`）：判"她是不是群主"才能
做群主动作。核心不再替它造这个（`runtime` 里留空位），因为删掉本插件后 Stage 3
照样说话——按判据它不是底层。插件造好之后**登记回核心**（`register_roles`），
执行端要用它。
"""
from __future__ import annotations

from .builtin_group_commands import GroupManageCommand, GroupOwnerCommand, TitleCommand

#: 前置插件：她自己的群角色查询（判"她是不是群主"）。装不上就不装本插件——
#: 没有角色来源时群主动作一条都做不了，半挂着不如明说。
REQUIRES = ("roles",)


def register(registry) -> None:
    # 角色查询由前置插件 `roles` 提供，这里只**取用**（`register()` 阶段它是就绪的：
    # 发现机制保证 `REQUIRES` 先装）。它内部走核心的 `call_action`，不持有 transport。
    if registry.shared_roles() is None:  # pragma: no cover - 前置没装时本插件会先被跳过
        raise RuntimeError("group_admin 需要前置插件 roles")

    registry.command(GroupManageCommand())
    registry.command(GroupOwnerCommand())
    registry.command(TitleCommand())
