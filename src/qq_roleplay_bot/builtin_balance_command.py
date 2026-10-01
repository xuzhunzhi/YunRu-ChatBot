"""账户余额查询命令。

**风险等级与普通命令不同**：它读取的是账号资金信息。设计取舍如下：

1. **默认只在超管私聊可用。** `session_allowed` 只声明会话范围，身份由核心判定；
   这里把"允许的 QQ 号"在构造时注入，插件自己不持有任何全局权限表。
2. **不调模型、不写记忆。** 走命令路径，回复不进对话上下文，也不进 inbox。
3. **默认只开给超管私聊是刻意的**：群里有十几号人，余额没必要让所有人看见。
   要放开就显式传 group_id。
"""
from __future__ import annotations

import logging

from .balance_client import BalanceClient, BalanceError, summarize
from .transport import IncomingMessage, MessageTarget

logger = logging.getLogger(__name__)

BALANCE_COMMAND = "/balance"
FALLBACK_HINT = "要改成别处可用，需显式配置（默认仅超管私聊）。"


class BalanceCommand:
    """查询模型服务商账户余额。"""

    name = "balance"

    def __init__(
        self,
        client: BalanceClient | None = None,
        *,
        allowed_user_ids: frozenset[str] | tuple[str, ...] = frozenset(),
        allowed_group_ids: frozenset[str] | tuple[str, ...] = frozenset(),
    ) -> None:
        self.client = client
        self.allowed_user_ids = frozenset(str(x) for x in allowed_user_ids)
        self.allowed_group_ids = frozenset(str(x) for x in allowed_group_ids)

    def match(self, message) -> bool:
        text = getattr(message, "text", "")
        return isinstance(text, str) and text.strip().casefold() in {
            BALANCE_COMMAND, "/余额", "/yu_e", "yunru balance",
        }

    def session_allowed(self, target: MessageTarget) -> bool:
        """只有被明确允许的会话能查余额。

        默认（两个集合都空）=> 任何会话都不允许。fail-closed：
        配置漏了就查不到，而不是泄漏给所有人。
        """

        if target.group_id is not None:
            return target.group_id in self.allowed_group_ids
        if target.user_id is not None:
            return target.user_id in self.allowed_user_ids
        return False

    async def handle(self, message: IncomingMessage) -> str | None:
        if self.client is None:
            return f"余额查询未配置。{FALLBACK_HINT}"
        try:
            payload = await self.client.fetch()
        except BalanceError as exc:
            # kind 是脱敏分类，可以安全写日志；正文不进日志。
            logger.warning("balance_query_failed kind=%s", exc.kind)
            return f"查余额失败（{exc.kind}），稍后再试。"
        except Exception as exc:  # noqa: BLE001 - 意外异常不能让消息处理炸掉
            logger.exception("balance_query_unexpected")
            return f"查余额失败（{type(exc).__name__}），稍后再试。"
        return summarize(payload)

    def help_text(self) -> str:
        # 不写进公开帮助：这是运维命令，不该出现在群里的 /help 里。
        return ""


def build_balance_client(base_url: str, api_key: str, **kwargs) -> BalanceClient:
    """便于装配层一行构造。"""

    return BalanceClient(base_url, api_key, **kwargs)


__all__ = ["BALANCE_COMMAND", "BalanceCommand", "build_balance_client"]
