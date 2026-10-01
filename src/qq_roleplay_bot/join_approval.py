"""入群申请自动审批：黑名单 → 白名单 → 正则 → 兜底。

由来（2026-09-30 用户："能适配自动审批……"，随后明确"自动审批做正则，还有白名单和黑名单"）。
她能审批的前提是**她自己是群主**（实测：测试群 717151356 里她 role=owner）。

为什么走**轮询**而不是事件：`get_group_system_msg`（待处理申请，带规范 `flag`）
本来就在只读白名单里，而 OneBot 的 `post_type=request` 事件在当前传输层是被直接丢掉的
（`parse_message_event` 只认 message）。轮询不用改传输层、不新增协议面，代价是最多等
一个轮询周期（默认 60 秒）——对一个测试群完全够用。要"秒级"再走事件通道。

**它是个插件**（2026-09-30 用户："入群审批也是插件，属于 stage4 内容"）：节拍与装配在
`background_plugins.py`，插件只做策略；要用外部能力时走核心注入的
`call_action` / `notify`，**自己拿不到 transport**。

判据优先级**写死**，不做"命中多个取哪个"的猜测：

| 顺序 | 判据 | 结果 |
| --- | --- | --- |
| 1 | `QQBOT_APPROVE_BLACKLIST`（QQ 号） | **拒绝**（附一句原因给申请人） |
| 2 | `QQBOT_APPROVE_WHITELIST`（QQ 号） | 通过 |
| 3 | `QQBOT_APPROVE_PATTERN`（正则，匹配"验证留言 + 昵称"） | 通过 |
| 4 | 以上都没命中 | **不表态**（`hold`，留给超管看） |

白名单赢不了黑名单（黑名单是"明确不要的人"），兜底是 hold 而不是 approve 是**故意的**：
没配规则就什么都不批，符合 fail-closed；想"除了黑名单全批"就把正则写成 `(?s).*`。

`sub_type` 只处理 `add`（别人申请进群）。`invite`（有人邀请**她**进新群）**不自动批**——
那等于自动进陌生群，只记日志并通知超管（要不要接是人的决定）。
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass

from .qq_roles import ROLE_ADMIN, ROLE_OWNER

logger = logging.getLogger(__name__)

APPROVE = "approve"
REJECT = "reject"
HOLD = "hold"

# 只处理"申请进群"；邀请她进别的群是另一件事（见模块头）。
JOIN_SUB_TYPE = "add"
INVITE_SUB_TYPE = "invite"

# 验证留言可能很长，日志与正则都只吃前这么多字符。
MAX_COMMENT_CHARS = 200
# 同一条申请在内存里记多久（秒）：防止 `checked` 没及时翻转时被重复处理。
SEEN_TTL_SECONDS = 3600.0


@dataclass(frozen=True, slots=True)
class Decision:
    """一条申请该怎么处理，以及是**哪条规则**定的。"""

    action: str
    rule: str


@dataclass(frozen=True, slots=True)
class PendingJoin:
    """`get_group_system_msg` 里一条待处理的入群申请。"""

    flag: str
    group_id: str
    user_id: str
    nickname: str
    comment: str
    invitor_id: str = ""
    sub_type: str = JOIN_SUB_TYPE

    @property
    def is_join(self) -> bool:
        return self.sub_type == JOIN_SUB_TYPE


def _text(value: object, limit: int = MAX_COMMENT_CHARS) -> str:
    return " ".join(str(value or "").split())[:limit]


def parse_pending(data: object) -> tuple[PendingJoin, ...]:
    """把 `get_group_system_msg` 的返回转成待处理申请。

    只在**有 flag** 时才收：没有 flag 就没法处理（`set_group_add_request` 认它）。
    `checked` 为真表示已经处理过，跳过。
    """

    if not isinstance(data, list):
        return ()
    items: list[PendingJoin] = []
    for raw in data:
        if not isinstance(raw, dict):
            continue
        if raw.get("checked"):
            continue
        flag = str(raw.get("flag") or "").strip()
        group_id = str(raw.get("group_id") or "").strip()
        user_id = str(raw.get("requester_uin") or raw.get("user_id") or "").strip()
        if not flag or not group_id or not user_id:
            continue
        items.append(PendingJoin(
            flag=flag,
            group_id=group_id,
            user_id=user_id,
            nickname=_text(raw.get("requester_nick") or raw.get("nickname"), 60),
            comment=_text(raw.get("message") or raw.get("comment")),
            invitor_id=str(raw.get("invitor_uin") or "").strip(),
            # 群系统消息里的都是"申请进群"；显式给了 sub_type 就照它。
            sub_type=str(raw.get("sub_type") or JOIN_SUB_TYPE).strip().casefold(),
        ))
    return tuple(items)


class JoinApprovalPolicy:
    """白名单 / 黑名单 / 正则 / 兜底。纯函数，无 IO，方便测。"""

    def __init__(self, *, whitelist=(), blacklist=(), pattern: str = "",
                 reject_reason: str = "", default: str = HOLD) -> None:
        self.whitelist = frozenset(str(item).strip() for item in whitelist if str(item).strip())
        self.blacklist = frozenset(str(item).strip() for item in blacklist if str(item).strip())
        self.reject_reason = str(reject_reason or "").strip() or "本群暂不接受这次申请。"
        self.default = default if default in {APPROVE, REJECT, HOLD} else HOLD
        self.pattern_source = str(pattern or "").strip()
        self.pattern: re.Pattern[str] | None = None
        if self.pattern_source:
            try:
                self.pattern = re.compile(self.pattern_source)
            except re.error as exc:
                # 配置写错**不能**变成"全都不批"以外的行为：日志里说清楚，按没配处理。
                logger.warning("approve_pattern_invalid error=%s", type(exc).__name__)

    @property
    def explain(self) -> str:
        """给 `/super help` 或状态看的一行人话。"""

        if not self.pattern_source and not self.whitelist and not self.blacklist:
            return "未配置（谁都不会被自动批准，只记日志并通知超管）"
        parts = []
        if self.blacklist:
            parts.append(f"黑名单 {len(self.blacklist)} 个")
        if self.whitelist:
            parts.append(f"白名单 {len(self.whitelist)} 个")
        if self.pattern_source:
            parts.append(f"正则 {self.pattern_source!r}")
        parts.append(f"其余：{'批准' if self.default == APPROVE else '拒绝' if self.default == REJECT else '不表态'}")
        return "；".join(parts)

    def decide(self, request: PendingJoin) -> Decision:
        if request.user_id in self.blacklist:
            return Decision(REJECT, "blacklist")
        if request.user_id in self.whitelist:
            return Decision(APPROVE, "whitelist")
        if self.pattern is not None:
            haystack = f"{request.comment}\n{request.nickname}".strip()
            if self.pattern.search(haystack):
                return Decision(APPROVE, "pattern")
        return Decision(self.default, "default")


class JoinApprovalPoller:
    """一下一下地跑：读待处理申请 → 按策略处理 → 记审计。

    它**不持有 transport**（2026-09-30 用户："入群审批也是插件"）：要用到外部能力时走核心
    注入的两个窄接缝——

    - `call_action(action, params)`：核心先过 `capabilities` 闸门，再调对面，回执已拆成 `data`；
      失败抛异常。所以插件不需要知道协议形状，也绕不开闸门。
    - `notify(target, text)`：核心统一发送（入群队列里等着的那些通知走它）。

    插件自己负责的是**策略那一半**：黑名单/白名单/正则、别重复处理、每轮上限。
    """

    def __init__(self, *, call_action, policy: JoinApprovalPolicy, roles,
                 notify=None, enabled: bool = True,
                 max_per_tick: int = 5, clock=time.time) -> None:
        self.call_action = call_action
        self.notify_fn = notify
        self.policy = policy
        self.roles = roles
        self.enabled = bool(enabled)
        self.max_per_tick = max(1, int(max_per_tick))
        self.clock = clock
        self._seen: dict[str, float] = {}
        self.stats = {"polls": 0, "approved": 0, "rejected": 0, "held": 0,
                      "invites": 0, "failed": 0, "notified": 0}

    async def tick(self) -> dict[str, int]:
        """跑一轮。返回本轮各自处理了几条（给日志与测试看）。"""

        if not self.enabled or not callable(self.call_action):
            return {}
        self.stats["polls"] += 1
        self._expire()
        try:
            data = await self.call_action("get_group_system_msg", {})
        except Exception as exc:  # noqa: BLE001 - 读不到就下一轮再说
            self.stats["failed"] += 1
            logger.warning("join_approval_query_failed category=%s", type(exc).__name__)
            return {}
        result = {"approved": 0, "rejected": 0, "held": 0, "invites": 0}
        handled = 0
        for request in parse_pending(data):
            if handled >= self.max_per_tick:
                logger.info("join_approval_deferred remaining=1+")
                break
            if request.flag in self._seen:
                continue
            if not request.is_join:
                # 有人邀请她去新群：**不自动接**，只记一笔并通知（要不要进群是人决定的）。
                self._seen[request.flag] = self.clock()
                self.stats["invites"] += 1
                result["invites"] += 1
                logger.warning("join_invite_seen group=%s invitor=%s", request.group_id, request.invitor_id or "-")
                await self._notify(f"有人邀请我进群 {request.group_id}（邀请人 {request.invitor_id or '未知'}），"
                                   f"这一条我不自动处理，你看着办。")
                continue
            if not await self._may_approve(request.group_id):
                continue
            decision = self.policy.decide(request)
            ok = await self._apply(request, decision)
            if not ok:
                continue
            handled += 1
            self._seen[request.flag] = self.clock()
            if decision.action == APPROVE:
                self.stats["approved"] += 1
                result["approved"] += 1
            elif decision.action == REJECT:
                self.stats["rejected"] += 1
                result["rejected"] += 1
            else:
                self.stats["held"] += 1
                result["held"] += 1
                await self._notify(
                    f"群里有人申请加入 {request.group_id}：{request.nickname}（{request.user_id}）"
                    f"留言「{request.comment or '无'}」，我按规则没有自动批，你看着办。"
                )
        if handled:
            logger.info("join_approval_tick approved=%s rejected=%s held=%s",
                        result["approved"], result["rejected"], result["held"])
        return result

    async def _may_approve(self, group_id: str) -> bool:
        """只有她在这个群是群主/管理员才处理（管理员也能批入群）。"""

        role = await self.roles.role(group_id) if self.roles is not None else ""
        if role in {ROLE_OWNER, ROLE_ADMIN}:
            return True
        logger.info("join_approval_skipped group=%s role=%s", group_id, role or "unknown")
        return False

    async def _apply(self, request: PendingJoin, decision: Decision) -> bool:
        if decision.action == HOLD:
            return True
        params: dict[str, object] = {
            "flag": request.flag,
            "sub_type": request.sub_type,
            "approve": decision.action == APPROVE,
        }
        if decision.action == REJECT:
            params["reason"] = self.policy.reject_reason
        # 每一次处理都留审计：谁、哪个群、按哪条规则、结果（`group_admin` 同一套做法）。
        logger.warning("join_request_decided group=%s user=%s nick=%s comment=%r rule=%s approved=%s",
                       request.group_id, request.user_id, request.nickname, request.comment,
                       decision.rule, decision.action == APPROVE)
        try:
            # 闸门（`purpose="join_approval"`）在核心注入的 `call_action` 里，
            # 不在插件里——插件连"能不能批"都不判，它只判"该不该批"。
            await self.call_action("set_group_add_request", params)
        except Exception as exc:  # noqa: BLE001 - 一条失败不影响别的申请
            self.stats["failed"] += 1
            logger.warning("join_approval_failed group=%s category=%s", request.group_id, type(exc).__name__)
            return False
        return True

    async def _notify(self, text: str) -> None:
        if not callable(self.notify_fn):
            return
        try:
            await self.notify_fn(text)
            self.stats["notified"] += 1
        except Exception as exc:  # noqa: BLE001 - 通知失败不影响审批本身
            logger.warning("join_approval_notify_failed category=%s", type(exc).__name__)

    def _expire(self) -> None:
        now = self.clock()
        for flag, at in list(self._seen.items()):
            if now - at > SEEN_TTL_SECONDS:
                self._seen.pop(flag, None)

    def snapshot(self) -> dict[str, object]:
        return {**self.stats, "seen": len(self._seen), "policy": self.policy.explain}
