"""群管理动作：禁言 / 解禁 / 全员禁言 / 撤回一条 / 移出群聊。

由来（2026-09-30 用户："优先接入群管理功能"，随后明确"显然只有 /super 权限可以"）。

这是一个**故意做窄**的模块，四条护栏缺一不可：

1. **只有超管能碰**。命令挂在 `/super` 下（`/super ban` 等），而 `/super` 那一层的
   权限判定只管一个名单、且是全局的——群管理员那套（`/admin`，按群授权）**碰不到这里**。
2. **闸门单独列名**。`capabilities.py` 里新开了 `purpose="group_manage"`，只批四个
   action（禁言/全员禁言/撤回/移出）。`purpose="admin"` 那道闸门按设计排除全部敏感写操作，
   这里**没有**放宽它，只是多了一条独立通道；`set_group_leave`（退群自毁）与
   `set_group_admin`（给/撤真管理员＝提权）**不在名单里**。
3. **目标有硬限制**：不能对超管/管理员动手（免得群里互踢），不能对机器人自己动手；
   **移出群聊必须 @ 到人**（手滑发个群号就踢人，是这条路上最典型的失误）；
   禁言分钟数有上下限（1 ~ 43200，即 30 天）。
4. **全程审计**：每一次动作都记一条 WARNING（谁、在哪个群、对谁、做什么、结果），
   动作名与参数都来自本模块的常量表，不接受调用方拼字符串。

作用范围**只有发命令的那个群**：超管命令是全局的，但群管理不是——不接受群号参数，
也就没有"敲个号去管别的群"这条路。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from ...plugins import ActionDenied

logger = logging.getLogger(__name__)

# 禁言时长边界：QQ 侧上限是 30 天，下限 1 分钟；默认 10 分钟。
MIN_BAN_MINUTES = 1
MAX_BAN_MINUTES = 43200
DEFAULT_BAN_MINUTES = 10


@dataclass(frozen=True, slots=True)
class GroupActionSpec:
    """一个群管理动作：命令名 → SnowLuma action + 参数构造 + 人话说明。"""

    kind: str
    action: str
    label: str
    needs_target: bool
    needs_message: bool = False
    quote_only: bool = False


SPECS: dict[str, GroupActionSpec] = {
    "kick": GroupActionSpec("kick", "set_group_kick", "移出群聊", needs_target=True),
    "ban": GroupActionSpec("ban", "set_group_ban", "禁言", needs_target=True),
    "unban": GroupActionSpec("unban", "set_group_ban", "解除禁言", needs_target=True),
    "mute": GroupActionSpec("mute", "set_group_whole_ban", "全员禁言", needs_target=False),
    "unmute": GroupActionSpec("unmute", "set_group_whole_ban", "解除全员禁言", needs_target=False),
    "recall": GroupActionSpec("recall", "delete_msg", "撤回消息",
                              needs_target=False, needs_message=True, quote_only=True),
}


class GroupActionRefused(Exception):
    """这次动作被护栏挡下。`message` 是可以直接回给超管的人话。"""


def clamp_minutes(raw: object) -> int:
    """禁言分钟数：不填给默认值，越界就夹到边界（不是报错——超管说 99999 分钟，
    意思是"尽量久"，夹到 30 天比回一句"参数不合法"更有用）。"""

    if raw in (None, "", "0"):
        return DEFAULT_BAN_MINUTES
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_BAN_MINUTES
    return max(MIN_BAN_MINUTES, min(MAX_BAN_MINUTES, value))


def build_params(spec: GroupActionSpec, *, group_id: str, target_id: str,
                 minutes: object, message_id: str) -> dict[str, object]:
    """按常量表拼参数。**参数名写死在这里**，调用方只能给值，不能改形状。"""

    if spec.kind == "recall":
        return {"message_id": _as_int(message_id)}
    if spec.action == "set_group_ban":
        if spec.kind == "unban":
            return {"group_id": _as_int(group_id), "user_id": _as_int(target_id), "duration": 0}
        return {"group_id": _as_int(group_id), "user_id": _as_int(target_id),
                "duration": clamp_minutes(minutes) * 60}
    if spec.action == "set_group_whole_ban":
        return {"group_id": _as_int(group_id), "enable": spec.kind == "mute"}
    if spec.action == "set_group_kick":
        return {"group_id": _as_int(group_id), "user_id": _as_int(target_id)}
    raise GroupActionRefused("这个动作没有定义参数，已拒绝。")  # pragma: no cover - 常量表兜底


def _as_int(value: object) -> int | str:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return str(value).strip()


async def execute(
    kind: str,
    *,
    call,
    group_id: str | None,
    actor_id: str,
    target_id: str = "",
    minutes: object = None,
    message_id: str = "",
    mentioned: bool = False,
    protected_ids: frozenset[str] = frozenset(),
    enabled: bool = True,
) -> str:
    """执行一个群管理动作，返回给群里看的一句话。**任何异常都变成人话回复。**

    ## 只收一个 `call`，不收 transport（2026-10-01 适配）

    `call(action, params) -> response` 是核心造的**已过闸门**的调用函数
    （`runtime._SeamBinder.action_caller("group_manage")`）。它原来的签名是
    `transport=...` + `registry=...`——也就是**插件目录里的函数拿到了活的传输层**
    并自己调 `transport.call_api`，而 `stage3_main` 的 docstring 还写着
    "插件从头到尾没拿到 transport"。那句话是假的，现在改成真的：

    - 闸门（`capabilities`，`purpose="group_manage"`）在核心那条闭包里，
      插件这边**没有** `registry.check` 可调，也就绕不过去；
    - 拒绝以 `ActionDenied` 抛出来（接缝模块的异常类型，不是核心的
      `CapabilityDenied`）——插件不该认识 `capabilities.py`。

    护栏**仍然留在这里**（要 @ 到人、不动超管、禁言分钟数夹紧）：它们是"这条命令
    怎么用才对"的规则，跟着命令走。
    """

    spec = SPECS.get(str(kind))
    if spec is None:
        return "用法：/super kick|ban|unban|mute|unmute|recall（详见 /super help）"
    if not enabled:
        return "群管理功能没开（QQBOT_GROUP_MANAGE=0）。"
    if not group_id:
        # 超管命令是全局的，但群管理不是：私聊里没有"这个群"可管。
        return "群管理要在**群里**发（私聊里没有可管的群）。"
    if spec.needs_target:
        if not target_id:
            extra = " [分钟数]" if spec.kind == "ban" else ""
            return f"用法：/super {spec.kind} @某人{extra}"
        if spec.kind == "kick" and not mentioned:
            # 移出群聊必须 @ 到人：手滑发个群号就踢人，是这条路上最典型的失误。
            return "移出群聊请用 @ 指定人（不接受直接写群号）。"
        if str(target_id) in protected_ids:
            return "这个人是超管或管理员，不动他。"
    if spec.needs_message and not message_id:
        return f"用法：/super {spec.kind}（**引用**要撤回的那条消息再发这句）"

    params = build_params(spec, group_id=group_id, target_id=target_id,
                          minutes=minutes, message_id=message_id)
    logger.warning(
        "group_action actor=%s group=%s action=%s target=%s params=%s",
        actor_id, group_id, spec.action, target_id or "-",
        {key: value for key, value in params.items() if key != "message_id"},
    )
    if not callable(call):
        return "这个通道不支持群管理动作。"
    try:
        response = await call(spec.action, params)
    except ActionDenied:
        logger.warning("group_action_denied action=%s", spec.action)
        return "这个动作没有授权，已拒绝。"
    except Exception as exc:  # noqa: BLE001 - 失败要给超管一句人话，不是 traceback
        logger.warning("group_action_failed action=%s category=%s", spec.action, type(exc).__name__)
        return f"{spec.label}失败（{type(exc).__name__}）。"
    if isinstance(response, dict):
        status = str(response.get("status") or "")
        retcode = response.get("retcode")
        if status and status != "ok" or (isinstance(retcode, int) and retcode != 0):
            logger.warning("group_action_rejected action=%s retcode=%s", spec.action, retcode)
            return f"{spec.label}被对面拒绝（retcode={retcode}）。"
    if spec.kind == "ban":
        return f"已禁言 {target_id}：{clamp_minutes(minutes)} 分钟。"
    if spec.kind == "unban":
        return f"已解除 {target_id} 的禁言。"
    if spec.kind == "kick":
        return f"已把 {target_id} 移出本群。"
    if spec.kind == "recall":
        return "已撤回那条消息。"
    return f"已{'开启' if spec.kind == 'mute' else '关闭'}全员禁言。"
