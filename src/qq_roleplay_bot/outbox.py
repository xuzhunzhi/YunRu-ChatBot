"""没送出去的消息先放这儿，等能发了再补上（用户 2026-09-28 要的）。

**为什么要有这一层**：在那之前，`_deliver_reply` 的失败分支只做两件事——记一条
`send_failures` 指标、打一行异常日志。消息本身**丢了**：NapCat 没连上、WS 刚断、
等连接超时，她说过的话就永远没到群里，而且她自己的短期语境里也没有那句
（只有 `combined` 里成功的那几段会进语境），所以下一条她会以为刚才什么也没说。

**只补发"确定没送出去"的那一类**（`MessageNotDelivered`）：连接不在、等连接超时、
写入之前就断了。另外两类一律不补：
`DeliveryUncertain`（发出去了但没回执——补发可能让同一句话在群里出现两遍）、
`DeliveryRejected`（OneBot 明确回了 retcode != 0，例如被禁言、不是好友——对面不要）。

**三道上限**（都不能省）：条数 `max_items`、年龄 `max_age_seconds`、单条尝试次数
`max_attempts`。没有年龄上限的话，断线一小时后回来会把她一小时的回复连着喷出来，
那比丢消息更像事故。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, replace

from .transport import MessageNotDelivered, MessageTarget

logger = logging.getLogger(__name__)

# 队列上限：超过就丢**最旧的**（新的更贴近当下）。断线期间她一般也就攒下几条。
OUTBOX_MAX_ITEMS = 20
# 一条消息最多等这么久（秒）。过了就不发了——迟到十分钟的回复比不回更奇怪。
OUTBOX_MAX_AGE_SECONDS = 600.0
# 同一条最多尝试几次（每次尝试都是"连接在但没送出去"）。
OUTBOX_MAX_ATTEMPTS = 3


@dataclass(frozen=True, slots=True)
class OutboundItem:
    """一条等着补发的消息。字段与 `OutgoingMessage` 对齐（不含 paced/typing）。"""

    target: MessageTarget
    text: str
    reply_to_message_id: str = ""
    queued_at: float = 0.0
    attempts: int = 0
    # 补发成功之后要额外通知谁。转告用它：管理员确认过的那条终于发出去了，
    # 得有人告诉他一声——否则他只知道"排队中"，后面就再没下文了。
    note_target: MessageTarget | None = None
    note_text: str = ""


class Outbox:
    """出站队列：`enqueue` 收，`flush` 在连接回来时按顺序补发。"""

    def __init__(
        self,
        *,
        clock=time.time,
        max_items: int = OUTBOX_MAX_ITEMS,
        max_age_seconds: float = OUTBOX_MAX_AGE_SECONDS,
        max_attempts: int = OUTBOX_MAX_ATTEMPTS,
    ) -> None:
        self.clock = clock
        self.max_items = max(1, int(max_items))
        self.max_age_seconds = float(max_age_seconds)
        self.max_attempts = max(1, int(max_attempts))
        self._items: list[OutboundItem] = []
        self.queued = 0
        self.resent = 0
        self.dropped = 0

    def __len__(self) -> int:
        return len(self._items)

    def pending(self) -> tuple[OutboundItem, ...]:
        return tuple(self._items)

    def enqueue(
        self,
        target: MessageTarget,
        text: str,
        *,
        reply_to: str = "",
        note_target: MessageTarget | None = None,
        note_text: str = "",
    ) -> bool:
        """收下一条没发出去的消息。返回 False 表示队列满了、这条被丢掉。"""

        if not text:
            return False
        self._items.append(
            OutboundItem(target=target, text=text, reply_to_message_id=reply_to,
                         queued_at=self.clock(),
                         note_target=note_target, note_text=note_text)
        )
        self.queued += 1
        if len(self._items) > self.max_items:
            dropped = self._items.pop(0)  # 丢最旧的
            self.dropped += 1
            logger.warning(
                "outbox full, dropping oldest: group=%s length=%s",
                dropped.target.group_id or dropped.target.user_id, len(dropped.text),
            )
            return False
        return True

    def _expired(self, item: OutboundItem, now: float) -> str:
        if now - item.queued_at > self.max_age_seconds:
            return "too_old"
        if item.attempts >= self.max_attempts:
            return "too_many_attempts"
        return ""

    async def flush(self, transport) -> tuple[int, int]:
        """连接回来时补发。返回 `(补发成功数, 丢弃数)`。

        连接不在就直接返回（**不再等** `connection_timeout`：那个 15 秒会把这个
        5 秒一跳的时钟顶住）。某条又是"没送出去"就停下——按顺序来，下一跳再说。
        """

        if not self._items:
            return 0, 0
        if not bool(getattr(transport, "connected", True)):
            return 0, 0
        now = self.clock()
        keep: list[OutboundItem] = []
        resent = 0
        dropped = 0
        for index, item in enumerate(self._items):
            reason = self._expired(item, now)
            if reason:
                dropped += 1
                logger.warning(
                    "outbox dropped (%s): group=%s length=%s",
                    reason, item.target.group_id or item.target.user_id, len(item.text),
                )
                continue
            try:
                await transport.send(item.target, item.text, reply_to=item.reply_to_message_id)
            except MessageNotDelivered as exc:
                # 连接又没了：这条和后面的一条都别试了，留到下一跳，顺序不能乱。
                keep.append(replace(item, attempts=item.attempts + 1))
                keep.extend(self._items[index + 1:])
                logger.info("outbox resend postponed: %s", exc)
                break
            except Exception:
                dropped += 1
                logger.exception("outbox resend failed; dropping message")
                continue
            resent += 1
            logger.info(
                "outbox resent: group=%s length=%s attempts=%s",
                item.target.group_id or item.target.user_id, len(item.text), item.attempts + 1,
            )
            if item.note_target is not None and item.note_text:
                # 通知**不再入队**：它只是"补发成功"的回执，套娃下去没完。
                try:
                    await transport.send(item.note_target, item.note_text)
                except Exception:  # noqa: BLE001
                    logger.warning("outbox note failed; continuing", exc_info=True)
        self._items = keep
        self.resent += resent
        self.dropped += dropped
        return resent, dropped

    def stats(self) -> dict[str, int]:
        return {"pending": len(self._items), "queued": self.queued,
                "resent": self.resent, "dropped": self.dropped}
