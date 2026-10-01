"""查询模型服务商账户余额。

为什么要单独一个客户端：这是本项目里**唯一一个读取账号资金信息**的调用，
风险等级和普通对话调用不同，必须能独立审查、独立设限（默认只在超管私聊可见）。

只用标准库：与 `llm_client.py` 一致，走 `urllib` + `asyncio.to_thread`，
不引入 http 依赖。

DeepSeek 接口规格（GET /user/balance）：
    is_available: bool
    balance_infos: [{currency, total_balance, granted_balance, topped_up_balance}]
余额字段是**字符串**，不要当数字解析。
"""
from __future__ import annotations

import asyncio
import json
import logging
import socket
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 10.0
# 余额不需要实时。缓存一小段时间，避免连续查把接口打满，
# 也避免"查一次余额"变成一条可被反复触发的出站请求。
DEFAULT_CACHE_SECONDS = 60.0


class BalanceError(RuntimeError):
    """余额查询失败。`kind` 是脱敏分类，可直接写日志。"""

    def __init__(self, message: str, *, kind: str = "unknown") -> None:
        super().__init__(message)
        self.kind = kind


class BalanceClient:
    """读取账户余额。同步实现，调用方负责丢进线程。"""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        cache_seconds: float = DEFAULT_CACHE_SECONDS,
        clock=None,
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.timeout = timeout
        self.cache_seconds = max(0.0, cache_seconds)
        self._clock = clock
        self._cached: tuple[float, dict[str, object]] | None = None

    async def fetch(self) -> dict[str, object]:
        """取余额；带缓存。出错抛 BalanceError，不返回半成品。"""

        if not self.api_key:
            raise BalanceError("未配置 API Key", kind="no_credential")
        if not self.base_url:
            raise BalanceError("未配置 API 地址", kind="no_endpoint")

        now = self._now()
        if self._cached is not None and now - self._cached[0] < self.cache_seconds:
            return self._cached[1]

        payload = await asyncio.to_thread(self._fetch_sync)
        self._cached = (now, payload)
        return payload

    def _now(self) -> float:
        if self._clock is not None:
            return self._clock()
        import time

        return time.monotonic()

    def _fetch_sync(self) -> dict[str, object]:
        request = urllib.request.Request(
            f"{self.base_url}/user/balance",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Accept": "application/json",
                # 不携带任何与本项目有关的信息，避免把调用方标识送出去。
                "User-Agent": "QQRoleplayBot/0.1",
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            # 只保留状态码：响应体可能回显凭据相关细节，不进日志。
            raise BalanceError(f"HTTP {exc.code}", kind=f"http_{exc.code}") from exc
        except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
            raise BalanceError("transport_error", kind="transport_error") from exc
        except OSError as exc:
            raise BalanceError("transport_error", kind="transport_error") from exc

        try:
            result = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise BalanceError("invalid_json", kind="invalid_json") from exc
        if not isinstance(result, dict):
            raise BalanceError("invalid_shape", kind="invalid_shape")
        return result


def summarize(payload: dict[str, object], *, cents: bool = True) -> str:
    """把余额响应渲染成一行群消息友好的文本。

    只输出币种与金额，**不输出任何凭据、账户标识或原始 JSON**。
    """

    infos = payload.get("balance_infos")
    if not isinstance(infos, list) or not infos:
        return "余额接口没有返回明细。"

    lines: list[str] = []
    available = payload.get("is_available")
    if available is False:
        lines.append("⚠ 余额不足，API 调用会失败。")

    for item in infos:
        if not isinstance(item, dict):
            continue
        currency = str(item.get("currency") or "?").upper()
        total = _money(item.get("total_balance"), cents=cents)
        granted = _money(item.get("granted_balance"), cents=cents)
        topped = _money(item.get("topped_up_balance"), cents=cents)
        lines.append(f"{currency} {total}（赠送 {granted} / 充值 {topped}）")

    return "\n".join(lines) if lines else "余额接口没有返回明细。"


def _money(value: object, *, cents: bool) -> str:
    """余额是字符串；只做展示层的轻处理，不参与任何计算。"""

    text = str(value if value is not None else "?").strip()
    if not cents:
        return text
    try:
        number = float(text)
    except ValueError:
        return text
    return f"{number:.2f}"
