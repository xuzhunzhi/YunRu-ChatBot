"""群主专属动作：设置群管理员 / 群名片 / 群名 / 群头衔 / 群公告。

由来（2026-09-30 用户："群主的接口应该比管理员更多来着……能适配自动审批，添加管理这些吗"，
随后点明"yunru 现在是测试群的群主了"）。实测：她（900000002）在测试群 717151356 里
`role=owner`，别处是 `member`（`qq_roles.py` 现查）。

和 `group_admin.py`（禁言/踢/撤回，只有超管、四条短名单）是**并列的两套**，
不是把那一套放宽：

- 闸门是新的 `capabilities.GROUP_OWNER_ACTIONS`（五条），`GROUP_MANAGE_ACTIONS` 没动；
- **执行前提是"她在那个群确实是群主/管理员"**——角色现查，查不到就拒绝（fail-closed），
  不靠配置里写死群号；
- 命令仍在 `/super` 下（人类一侧只有超管能发）；
- 每一次动作都记 WARNING 审计，参数名写死在常量表里。

护栏：

1. **群主专属 vs 群主/管理员**：QQ 里改群名、设管理员、发头衔只有群主能做；
   改群名片、发群公告群主和管理员都能做——按 QQ 自己的规矩来，不额外放宽。
2. **长度只挡住"明显是误操作"的量级**（群名/名片 30 字、头衔 32 字、公告 1000 字）。
   头衔**刻意不按 QQ 的 6 字去卡**（2026-10-01 用户："群头衔不要限制6个字符的长度，
   bot 设置的偶尔可以突破这个长度"）：能不能超由对面说了算。
   实测真机提交 8 字 → 对面**收下了但静默截断到 6 字**，所以设置之后会**回读一次**，
   回报里给的是真实存下来的值——不把我们没做到的事说成做到了。
3. **不能对她自己动手**（改自己的名片/头衔没意义），**不能动超管**（避免群里互相改）。
4. **退群/解散永远不在名单里**（`set_group_leave` 参数带上就是解散群）。

**修正（2026-09-30 晚，用户实测反馈"添加 qq 群管理这个没做对啊，刚刚试了不行"）**：
第 3 条里的"不能动超管"**已经从这一族里去掉**。它是从 `group_admin.py`（禁言/踢人）
抄过来的，而那一族是**对人不利**的动作，这一族全是"给人东西/设置显示"：
设管理员、给头衔、改名片、改群名、发公告——没有一条会伤到被 @ 的人。
证据来自真机日志：用户第一次试就是 @ 自己（超管），命令认出来了（
`action=owner_action`），回了一句 **11 个字**的 `这个人是超管，不动他。`，于是"不行"。
真正的前提只有一条：**她在那个群确实是群主**（角色现查）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from .capabilities import CapabilityRegistry
from .qq_roles import ROLE_ADMIN, ROLE_OWNER, ROLE_LABELS
from .transport import QQTransport

logger = logging.getLogger(__name__)

# 长度上限：**先挡一道，省一次必然失败的调用**，但只挡"明显是误操作"的量级。
#
# 头衔这里刻意**不再按 QQ 的 6 字去卡**（2026-10-01 用户："群头衔不要限制6个字符的长度，
# bot 设置的偶尔可以突破这个长度"）。理由有两条：
#   1. 6 字是 QQ 客户端的说法，实际能不能超过由对面决定，不由我们替它断言；
#   2. 我们自己挡掉的话，操作者拿到的是一句"太长了"，而不是对面**真实的**回执——
#      真被拒时 `execute` 里的 `retcode != 0` 分支会如实回"被对面拒绝（retcode=…）"，
#      那才是可信的信息。
# 所以这里只留一个防误操作的宽松上界（32），QQ 真不接受就照实回报。
MAX_GROUP_NAME = 30
MAX_GROUP_CARD = 30
MAX_SPECIAL_TITLE = 32
MAX_NOTICE = 1000


@dataclass(frozen=True, slots=True)
class OwnerActionSpec:
    """一个群主动作：命令名 → action + 需要什么参数 + 谁有资格。"""

    kind: str
    action: str
    label: str
    usage: str
    needs_target: bool = False
    needs_text: bool = False
    text_limit: int = 0
    owner_only: bool = True
    # 设/取消管理员用同一个 action，靠这个开关区分。
    enable: bool = True


SPECS: dict[str, OwnerActionSpec] = {
    "qqadmin": OwnerActionSpec("qqadmin", "set_group_admin", "设置群管理员",
                               "/super qqadmin @某人", needs_target=True),
    "unqqadmin": OwnerActionSpec("unqqadmin", "set_group_admin", "取消群管理员",
                                 "/super unqqadmin @某人", needs_target=True, enable=False),
    "card": OwnerActionSpec("card", "set_group_card", "改群名片",
                            "/super card @某人 新名片（留空=清除）", needs_target=True,
                            needs_text=True, text_limit=MAX_GROUP_CARD, owner_only=False),
    "groupname": OwnerActionSpec("groupname", "set_group_name", "改群名",
                                 "/super groupname 新群名", needs_text=True,
                                 text_limit=MAX_GROUP_NAME),
    "title": OwnerActionSpec("title", "set_group_special_title", "设置群头衔",
                             "/super title @某人 头衔；也可以自己发 /title 头衔",
                             needs_target=True, needs_text=True, text_limit=MAX_SPECIAL_TITLE),
    "notice": OwnerActionSpec("notice", "_send_group_notice", "发群公告",
                              "/super notice 公告正文", needs_text=True,
                              text_limit=MAX_NOTICE, owner_only=False),
}

# 给 `/super help` 与状态看的一行人话。
USAGE_LINE = "、".join(f"/super {kind}" for kind in SPECS)


class OwnerActionRefused(Exception):
    """被护栏挡下。`message` 是可以直接回给超管的人话。"""


def clamp_text(spec: OwnerActionSpec, text: str) -> str:
    """公告允许换行，别的都拍成一行；超长直接拒绝（不截断——改群名改半个更糟）。"""

    value = str(text or "")
    if spec.kind == "notice":
        value = value.strip()
    else:
        value = " ".join(value.split())
    if spec.text_limit and len(value) > spec.text_limit:
        raise OwnerActionRefused(f"{spec.label}太长了：最多 {spec.text_limit} 字（现在 {len(value)} 字）。")
    return value


def build_params(spec: OwnerActionSpec, *, group_id: str, target_id: str, text: str) -> dict[str, object]:
    """按常量表拼参数。参数名写死在这里，调用方只能给值。"""

    if spec.action == "set_group_admin":
        return {"group_id": _as_int(group_id), "user_id": _as_int(target_id), "enable": spec.enable}
    if spec.action == "set_group_card":
        return {"group_id": _as_int(group_id), "user_id": _as_int(target_id), "card": text}
    if spec.action == "set_group_name":
        return {"group_id": _as_int(group_id), "group_name": text}
    if spec.action == "set_group_special_title":
        return {"group_id": _as_int(group_id), "user_id": _as_int(target_id),
                "special_title": text}
    if spec.action == "_send_group_notice":
        return {"group_id": _as_int(group_id), "content": text}
    raise OwnerActionRefused("这个动作没有定义参数，已拒绝。")  # pragma: no cover - 常量表兜底


def _as_int(value: object) -> int | str:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return str(value).strip()


async def execute(
    kind: str,
    *,
    transport: QQTransport,
    roles,
    registry: CapabilityRegistry | None,
    group_id: str | None,
    actor_id: str,
    target_id: str = "",
    text: str = "",
    mentioned: bool = False,
    enabled: bool = True,
) -> str:
    """执行一个群主动作，返回给群里看的一句话。**任何异常都变成人话回复。**

    **没有 `protected_ids`**（与 `group_admin.py` 的差别，见模块头"修正"一段）：
    这一族动作不会伤到被 @ 的人，把超管/管理员排除在外只会让"给我自己设个管理员"
    这种最自然的用法直接失败。
    """

    spec = SPECS.get(str(kind))
    if spec is None:
        return f"用法：{USAGE_LINE}（详见 /super help）"
    if not enabled:
        return "群主功能没开（QQBOT_GROUP_OWNER=0）。"
    if not group_id:
        return "这些动作要在**群里**发（私聊里没有可管的群）。"
    if spec.needs_target:
        if not target_id:
            return f"用法：{spec.usage}"
        if not mentioned and spec.kind != "card" and str(target_id) != str(actor_id):
            # 和群管理同一条经验：手滑发个数字就改到别人头上。所以**改别人必须 @**；
            # 改自己不用（2026-09-30 用户："头衔……自己给自己申请"）。
            return f"{spec.label}请用 @ 指定人（改自己可以直接发）。"
    if spec.needs_text and not str(text or "").strip() and spec.kind != "card":
        return f"用法：{spec.usage}"

    try:
        value = clamp_text(spec, text)
        params = build_params(spec, group_id=group_id, target_id=target_id, text=value)
    except OwnerActionRefused as exc:
        return str(exc)

    # 角色现查：不是群主（或该动作允许的管理员）就不做，并说清她在这个群是什么身份。
    if roles is None:
        # 没接角色查询（装配漏了）——**要出声**，否则看起来只是"她查不到自己"。
        logger.warning("owner_action_no_role_source action=%s group=%s", spec.action, group_id)
    role = await roles.role(group_id) if roles is not None else ""
    allowed = {ROLE_OWNER} if spec.owner_only else {ROLE_OWNER, ROLE_ADMIN}
    if role not in allowed:
        who = ROLE_LABELS.get(role, "查不到")
        need = "群主" if spec.owner_only else "群主或管理员"
        logger.warning("owner_action_refused action=%s group=%s role=%s", spec.action, group_id, role or "unknown")
        return f"我在这个群里是{who}，{spec.label}需要{need}权限，做不了。"

    if registry is not None:
        from .capabilities import CapabilityDenied as _Denied

        try:
            registry.check(spec.action, purpose="group_owner")
        except _Denied:
            logger.warning("owner_action_denied action=%s", spec.action)
            return "这个动作没有授权，已拒绝。"

    logger.warning("owner_action actor=%s group=%s action=%s target=%s",
                   actor_id, group_id, spec.action, target_id or "-")
    call_api = getattr(transport, "call_api", None)
    if not callable(call_api):
        return "这个通道不支持群主动作。"
    try:
        response = await call_api(spec.action, params)
    except Exception as exc:  # noqa: BLE001 - 失败要给一句人话，不是 traceback
        logger.warning("owner_action_failed action=%s category=%s", spec.action, type(exc).__name__)
        return f"{spec.label}失败（{type(exc).__name__}）。"
    if isinstance(response, dict):
        status = str(response.get("status") or "")
        retcode = response.get("retcode")
        if status and status != "ok" or (isinstance(retcode, int) and retcode != 0):
            logger.warning("owner_action_rejected action=%s retcode=%s", spec.action, retcode)
            if retcode == 100:
                # 100 = 权限不足或对象不对，最常见的原因就是她其实不是群主了。
                roles.forget(group_id)
            return f"{spec.label}被对面拒绝（retcode={retcode}）。"
    if spec.action == "set_group_admin":
        return f"已{'设置' if spec.enable else '取消'} {target_id} 的群管理员。"
    if spec.action == "set_group_card":
        return f"已把 {target_id} 的群名片改成「{value}」。" if value else f"已清除 {target_id} 的群名片。"
    if spec.action == "set_group_name":
        return f"群名已改成「{value}」。"
    if spec.action == "set_group_special_title":
        # **回读一次**再回报：QQ 会**静默截断**超长头衔。
        # 实测（2026-10-01）：提交"这是七个字的头衔"（8 字）返回成功，
        # 现查回来是"这是七个字的"（6 字）——只回报"已设置"等于把我们没做到的事
        # 说成做到了，而操作者没有任何线索。回读拿不到就退回原来的说法（不撒谎、
        # 也不因为一次读失败就报错）。
        actual = await _read_back_title(transport, group_id=group_id, target_id=target_id)
        if actual and actual != value:
            logger.warning("owner_action_title_truncated requested=%s actual=%s",
                           len(value), len(actual))
            return (f"{target_id} 的群头衔现在是「{actual}」"
                    f"（我提交的是 {len(value)} 字，对面只留了 {len(actual)} 字）。")
        return f"已给 {target_id} 设置群头衔「{value}」。"
    return "群公告已发出。"


async def _read_back_title(transport, *, group_id: str, target_id: str) -> str:
    """现查一次群头衔。拿不到就返回空串（调用方退回原话）。

    `no_cache=True` 是必须的：2026-09-30 踩过一次——不带它时拿到的是缓存里的旧值，
    于是"头衔其实写进去了"被误判成"没生效"。
    """

    call_api = getattr(transport, "call_api", None)
    if not callable(call_api):
        return ""
    try:
        response = await call_api(
            "get_group_member_info",
            {"group_id": _as_int(group_id), "user_id": _as_int(target_id), "no_cache": True},
        )
    except Exception:  # noqa: BLE001 - 只是核对，失败不该把成功说成失败
        return ""
    data = response.get("data") if isinstance(response, dict) else None
    if data is None and isinstance(response, dict):
        # 有的通道直接回 `data` 本身（HTTP 那条路拆过一层）。
        data = response
    if not isinstance(data, dict):
        return ""
    return str(data.get("title") or "")
