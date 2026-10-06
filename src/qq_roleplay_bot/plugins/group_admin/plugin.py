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
from .group_admin import execute as _execute_group_admin
from .group_owner import execute as _execute_group_owner


def build_group_admin(usage_store=None):
    """把群管理动作的执行函数交给核心（**工厂**，形状同 `registry.vision`）。

    `usage_store`（核心的用量账本）**用不上**：执行函数只拼参数、调核心造的已过闸门的
    `call`，自己不发模型请求。工厂照收它是接缝的形状要求——`runtime._build_group_actions`
    先把账本递给工厂（`factory(usage_store)`）。执行函数本身**一个字没改**。
    """

    return _execute_group_admin


def build_group_owner(usage_store=None):
    """把群主动作的执行函数交给核心（同上，`usage_store` 同样用不上）。"""

    return _execute_group_owner


def register(registry) -> None:
    """三条命令**无条件登记**。

    为什么这里不再有 `REQUIRES = ("roles",)`：那等于"角色事实还在插件之间传"，
    而它已经归核心。而且前置一旦装不上，`discover()` 会把本插件整个跳过——
    可这三条命令只是**解析 + 声明意图**（`ActionRequest`），登记时不需要任何角色信息。

    **两个执行函数经注册表交出去**（2026-10-06）：核心原来在
    `stage3_main.execute_action` 里直接 `from .plugins.group_admin.group_admin import
    execute`——那是**按插件模块名**找函数，把本文件夹改个名字，`discover()` 照样说装上
    了，而命令会**静默**回一句"这条部署没有群管理能力"。现在核心只按
    `ActionRequest.group` 取（`registry.group_action` / `runtime._build_group_actions`），
    **不认识本插件的模块名**。分组名与核心那张表一致：`"group_admin"` / `"group_owner"`。
    """

    registry.provide_group_action("group_admin", build_group_admin)
    registry.provide_group_action("group_owner", build_group_owner)
    registry.command(GroupManageCommand())
    registry.command(GroupOwnerCommand())
    registry.command(TitleCommand())
