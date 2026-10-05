"""群管理插件：踢 / 禁言 / 撤回 / 头衔 / 公告 / 改名片。

**命令插件**（有消息进来才动）。三条命令只做"解析 + 声明意图"，
真正的权限判定、护栏与调用留在核心的 `stage3_main.execute_action`——
插件拿不到 `transport`。

## 她自己的角色：2026-10-05 起**不经过插件**

原来本插件声明 `REQUIRES = ("roles",)`，向**另一个插件**（`plugins/roles/`）要那份
"她在这个群里是什么角色"的共享查询。用户 2026-10-05 的决定是**身份/权限事实归核心**，
所以那条依赖整条撤了：这里不声明前置、不问 `registry.shared_roles()`、
也不自己造任何角色缓存（三样都是被明确否掉的）。

角色那条路现在**只有一个方向**：核心把角色来源递给执行端——
`stage3_main.execute_action()` 调 `group_owner.execute(roles=self.self_roles, …)`，
执行端只回答"核心给的这个角色够不够做这个动作"。**本插件不问、不查、不缓存**。
`plugins/roles/` 删掉之后本插件照常装、照常认命令；群主动作做不做得了
由核心那份角色来源决定（拿不到就 fail-closed，见 `group_owner.execute`）。
"""
from __future__ import annotations

from .builtin_group_commands import GroupManageCommand, GroupOwnerCommand, TitleCommand


def register(registry) -> None:
    """三条命令**无条件登记**。

    为什么这里不再有 `REQUIRES = ("roles",)`：那等于"角色事实还在插件之间传"，
    而它已经归核心。而且前置一旦装不上，`discover()` 会把本插件整个跳过——
    可这三条命令只是**解析 + 声明意图**（`ActionRequest`），登记时不需要任何角色信息。
    """

    registry.command(GroupManageCommand())
    registry.command(GroupOwnerCommand())
    registry.command(TitleCommand())
