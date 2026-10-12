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

#: 面板上的显示名（`plugins.inventory()` 读它）。
TITLE = "群管理"

#: 「群管理动作」那一页的动作清单（面板画那张表单用）。
#:
#: **从面板原样搬过来**（2026-10-11 用户点名的那处接缝）：它原来在
#: `plugins/webui/webui_panel.py` 里叫 `_ACTION_CATALOG`——那意味着"面板必须知道
#: 群管理有哪些动作"。现在反过来：**插件自己报**（`registry.provide_panel`），
#: 面板只按封闭的 kind 词表把它画出来。12 行文案一字未改。
PANEL_ACTIONS: list[dict[str, object]] = [
    {"group": "group_admin", "kind": "kick", "label": "移出群聊", "needs_target": True},
    {"group": "group_admin", "kind": "ban", "label": "禁言", "needs_target": True,
     "needs_text": "分钟数"},
    {"group": "group_admin", "kind": "unban", "label": "解除禁言", "needs_target": True},
    {"group": "group_admin", "kind": "mute", "label": "全员禁言", "needs_target": False},
    {"group": "group_admin", "kind": "unmute", "label": "解除全员禁言", "needs_target": False},
    {"group": "group_admin", "kind": "recall", "label": "撤回一条消息", "needs_target": False,
     "needs_message": True},
    {"group": "group_owner", "kind": "qqadmin", "label": "设为 QQ 群管理员",
     "needs_target": True},
    {"group": "group_owner", "kind": "unqqadmin", "label": "取消 QQ 群管理员",
     "needs_target": True},
    {"group": "group_owner", "kind": "card", "label": "改群名片", "needs_target": True,
     "needs_text": "新名片（留空=清除）"},
    {"group": "group_owner", "kind": "groupname", "label": "改群名", "needs_target": False,
     "needs_text": "新群名"},
    {"group": "group_owner", "kind": "title", "label": "设群头衔", "needs_target": True,
     "needs_text": "头衔"},
    {"group": "group_owner", "kind": "notice", "label": "发群公告", "needs_target": False,
     "needs_text": "公告正文"},
]

#: 本插件的界面描述符（`registry.provide_panel("group_admin", PANEL)`）。
#:
#: **纯数据**，顶层只有 `title` / `note` / `blocks`；这一块是 `action_form`——
#: 面板见它只画那张表单，提交仍走既有的 `POST /api/action`（已鉴权、已审计）。
#: 描述符里**不许出现任何 URL / 路径 / method / HTML 键**：面板是同源的、token 就在
#: 浏览器里，那类键等于让插件借面板去调别的写接口（键白名单在
#: `webui_panel._panel_view` / `_panel_block`，并有测试钉住）。
PANEL: dict[str, object] = {
    "title": "群管理动作",
    "note": "以云茹自己的名义执行；全部动作都要二次确认，结果会进操作记录。",
    "blocks": [{"kind": "action_form", "title": "群管理动作", "rows": list(PANEL_ACTIONS)}],
}


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
    # 本插件自己报那一页（2026-10-11 的界面接缝）：面板按 `action_form` 画表单，
    # 它**不认识本插件叫什么**，也不认识这 12 个动作分别是什么。
    registry.provide_panel("group_admin", PANEL)
    registry.command(GroupManageCommand())
    registry.command(GroupOwnerCommand())
    registry.command(TitleCommand())
