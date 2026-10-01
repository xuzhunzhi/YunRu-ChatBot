"""OneBot v11 WebSocket 客户端传输。

与 `onebot_ws.py` 的区别：那是**反向 WS 服务端**（Bot 监听，SnowLuma 作为
wsClient 连进来），这是**客户端**（Bot 主动连 SnowLuma 的 wsServer）。

两者共用同一套上层接口（`receive` / `send` / `call_api`），因此 DialogueEngine
不需要知道自己跑在哪种连接方式上。客户端模式的意义是：不占用本地监听端口，
可以和反向模式同时运行，也可以直接调用 SnowLuma 的 HTTP 接口。
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from urllib.parse import urlencode

from .onebot_ws import TYPING_NOTICE_TIMEOUT_SECONDS, parse_message_event
from .transport import (
    DeliveryRejected,
    DeliveryUncertain,
    IncomingMessage,
    MessageNotDelivered,
    MessageTarget,
)

logger = logging.getLogger(__name__)

DEFAULT_REQUEST_TIMEOUT = 15.0
DEFAULT_RECONNECT_SECONDS = 5.0


def unwrap_result(payload: object) -> object:
    """把两种 OneBot 通道的返回统一成 **`data` 本身**。

    两条通道的返回形状不一样，踩过一次（2026-09-30 真机）：

    - `SnowLumaHttpClient.call` 直接返回 `data`；
    - WS 传输层的 `call_api` 返回**整个回执** `{"status","retcode","data",...}`。

    角色查询按 HTTP 的形状写（`payload.get("role")`），跑到 WS 通道上就永远是空，
    于是她被判成"我在这个群里是查不到"，群主命令全部做不了。
    判据：是 dict、带 `data`，且带 `status`/`retcode`/`echo` 之一（回执的特征）。
    """

    if isinstance(payload, dict) and "data" in payload and (
            "status" in payload or "retcode" in payload or "echo" in payload):
        return payload["data"]
    return payload


async def call_channel(client: object, action: str, params: dict[str, object] | None = None) -> object:
    """在任意一种通道上发一次查询/调用，并统一返回形状。

    `call`（HTTP 客户端）与 `call_api`（两种传输层）签名一致，所以这里按存在性选。
    两者都没有时抛 `RuntimeError`，由调用方决定要不要降级——**不要静默返回空**：
    这一路上静默过一次，表现成"她查不到自己的角色"，查日志还什么都没有。
    """

    for name in ("call", "call_api"):
        method = getattr(client, name, None)
        if callable(method):
            return unwrap_result(await method(action, params or {}))
    raise RuntimeError("这个通道不支持 OneBot 调用（既没有 call 也没有 call_api）")
MAX_RECONNECT_SECONDS = 60.0


class OneBotClientTransport:
    """主动连接 OneBot v11 WebSocket 服务端的客户端。

    - 事件帧转成 `IncomingMessage` 交给主循环；
    - Action 帧通过 `echo` 关联回 `call_api()` 的等待者；
    - 断线后按指数退避自动重连，`receive()` 在彻底关闭前不会返回 None。
    """

    def __init__(
        self,
        url: str,
        access_token: str = "",
        *,
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
        reconnect_seconds: float = DEFAULT_RECONNECT_SECONDS,
    ) -> None:
        self.url = url
        self.access_token = access_token
        self.request_timeout = request_timeout
        self.reconnect_seconds = max(0.1, reconnect_seconds)
        self._messages: asyncio.Queue[IncomingMessage | None] = asyncio.Queue()
        self._pending: dict[str, asyncio.Future[dict[str, object]]] = {}
        self._socket = None
        self._closed = False
        self._connected = asyncio.Event()
        self._runner: asyncio.Task | None = None
        self.connected_once = False

    # --- 生命周期 ---------------------------------------------------------

    async def start(self) -> None:
        """启动后台重连循环；立刻返回，不等待首次连接成功。"""

        if self._runner is not None:
            return
        self._closed = False
        self._runner = asyncio.create_task(self._run_forever(), name="onebot-client")

    async def close(self) -> None:
        self._closed = True
        if self._runner is not None:
            self._runner.cancel()
            try:
                await self._runner
            except asyncio.CancelledError:
                pass
            self._runner = None
        await self._teardown_socket()
        self._connected.clear()
        self._fail_pending(DeliveryUncertain("OneBot WebSocket 已关闭"))
        # 唤醒正在等待的主循环。
        self._messages.put_nowait(None)

    async def __aenter__(self) -> OneBotClientTransport:
        await self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    # --- 上层接口 ---------------------------------------------------------

    async def receive(self) -> IncomingMessage | None:
        """返回下一条消息；返回 None 表示传输层已关闭。"""

        message = await self._messages.get()
        if message is None and not self._closed:
            # 防御性还原：只有 close() 会放哨兵。
            self._messages.put_nowait(None)
        return message

    def drain_pending(self) -> list[IncomingMessage]:
        """把已经排队的消息一次取出（给主循环把命令提到前面用，见 `onebot_ws` 同名方法）。"""

        items: list[IncomingMessage] = []
        while True:
            try:
                item = self._messages.get_nowait()
            except asyncio.QueueEmpty:
                break
            if item is None:
                self._messages.put_nowait(None)
                break
            items.append(item)
        return items

    @property
    def connected(self) -> bool:
        """对面（NapCat 的 WS 服务）当前连着没有。`Outbox` 靠它决定要不要补发。"""

        return self._socket is not None and self._connected.is_set()

    async def send(self, target: MessageTarget, text: str, *, reply_to: str = "") -> None:
        if not text:
            return
        params: dict[str, object] = {"message": _compose_message(text, reply_to)}
        if target.group_id is not None:
            params.update(message_type="group", group_id=_id_value(target.group_id))
            await self.call_api("send_group_msg", params)
            return
        params.update(message_type="private", user_id=_id_value(target.user_id or ""))
        await self.call_api("send_private_msg", params)

    async def send_typing(self, target: MessageTarget, notice: str = "typing") -> None:
        """广播一次"正在输入"状态；纯装饰，失败一律吞掉。"""

        params: dict[str, object] = {"event_type": 1 if notice == "typing" else 0}
        if target.group_id is not None:
            params.update(message_type="group", group_id=_id_value(target.group_id))
        else:
            params.update(message_type="private", user_id=_id_value(target.user_id or ""))
        try:
            await asyncio.wait_for(
                self.call_api("set_input_status", params),
                timeout=TYPING_NOTICE_TIMEOUT_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001 - 状态提示失败不能影响正文
            logger.debug("set_input_status 未生效: %s", type(exc).__name__)

    async def call_api(self, action: str, params: dict[str, object] | None = None) -> dict[str, object]:
        """调用 OneBot API 并等待 echo 回执。

        与反向 WS 那条通道同一套分类（见 `transport.py`）：没送出去 →
        `MessageNotDelivered`（`Outbox` 会补发）；没回执 → `DeliveryUncertain`（不补发）；
        `retcode != 0` → `DeliveryRejected`（对面拒收，不补发）。
        """

        if not action or any(character.isspace() for character in action):
            raise ValueError("OneBot action 无效")
        await self._wait_connected()
        socket = self._socket
        if socket is None:
            raise MessageNotDelivered("OneBot WebSocket 尚未连接")

        echo = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, object]] = loop.create_future()
        self._pending[echo] = future
        try:
            try:
                await socket.send(json.dumps({"action": action, "params": params or {}, "echo": echo}))
            except Exception as exc:  # noqa: BLE001 - 写入就失败了，对面不可能收到
                raise MessageNotDelivered(
                    f"OneBot 写入失败：{type(exc).__name__}"
                ) from exc
            try:
                result = await asyncio.wait_for(future, timeout=self.request_timeout)
            except asyncio.TimeoutError as exc:
                raise DeliveryUncertain("OneBot 已发出但未收到回执") from exc
            if result.get("status") not in (None, "ok") or result.get("retcode") not in (None, 0):
                raise DeliveryRejected(f"OneBot {action} 失败: {result}")
            return result
        finally:
            self._pending.pop(echo, None)

    async def call_api_data(self, action: str, params: dict[str, object] | None = None) -> object:
        """调用 API 并只返回 `data` 字段，便于查询类调用。"""

        return (await self.call_api(action, params)).get("data")

    # --- 重连与收发 -------------------------------------------------------

    async def _wait_connected(self) -> None:
        if self._socket is not None:
            return
        try:
            await asyncio.wait_for(self._connected.wait(), timeout=self.request_timeout)
        except asyncio.TimeoutError as exc:
            raise MessageNotDelivered("OneBot WebSocket 连接等待超时") from exc

    async def _run_forever(self) -> None:
        delay = self.reconnect_seconds
        while not self._closed:
            try:
                await self._connect_and_listen()
                delay = self.reconnect_seconds
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("onebot_client_disconnected category=%s", type(exc).__name__)
            if self._closed:
                break
            await asyncio.sleep(delay)
            delay = min(MAX_RECONNECT_SECONDS, delay * 2)

    async def _connect_and_listen(self) -> None:
        from websockets.asyncio.client import connect as ws_connect

        headers = {"Authorization": f"Bearer {self.access_token}"} if self.access_token else {}
        logger.info("OneBot 客户端连接 %s", self.url)
        async with ws_connect(self.url, additional_headers=headers, open_timeout=self.request_timeout) as socket:
            self._socket = socket
            self._connected.set()
            self.connected_once = True
            logger.info("OneBot 客户端已连接")
            try:
                async for raw in socket:
                    await self._handle_frame(raw)
            finally:
                if self._socket is socket:
                    self._socket = None
                    self._connected.clear()
                    # 在途请求：不知道对面收到没有，按"不确定"抛，不自动重发。
                    self._fail_pending(DeliveryUncertain("OneBot WebSocket 已断开"))

    async def _handle_frame(self, raw: str | bytes) -> None:
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            logger.warning("忽略无法解析的 OneBot 数据")
            return
        if not isinstance(payload, dict):
            return

        echo = payload.get("echo")
        if isinstance(echo, str) and echo in self._pending:
            future = self._pending[echo]
            if not future.done():
                future.set_result(payload)
            return

        # 心跳、生命周期等 meta 事件不进业务队列。
        if payload.get("post_type") == "meta_event":
            return
        message = parse_message_event(payload)
        if message is not None:
            await self._messages.put(message)

    async def _teardown_socket(self) -> None:
        socket = self._socket
        self._socket = None
        if socket is not None:
            try:
                await socket.close()
            except Exception:
                logger.debug("关闭 OneBot 客户端连接时出错", exc_info=True)

    def _fail_pending(self, error: Exception) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)
        self._pending.clear()


def _compose_message(text: str, reply_to: str) -> str | list[dict[str, object]]:
    """引用回复时使用消息段；否则保持纯文本。"""

    if not reply_to:
        return text
    return [
        {"type": "reply", "data": {"id": reply_to}},
        {"type": "text", "data": {"text": text}},
    ]


def _id_value(value: str) -> int | str:
    try:
        return int(value)
    except ValueError:
        return value


class SnowLumaHttpClient:
    """SnowLuma OneBot v11 HTTP Server 的只读查询封装。

    用于拿 WebSocket 事件流里没有的信息：群消息历史、群成员名单、陌生人资料等。
    全部走标准库，不新增依赖；失败只抛异常，由调用方决定是否降级。
    """

    def __init__(self, base_url: str, access_token: str = "", *, timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.access_token = access_token
        self.timeout = timeout

    async def call(self, action: str, params: dict[str, object] | None = None) -> object:
        import urllib.error
        import urllib.request

        def sync_call() -> object:
            payload = json.dumps({"action": action, "params": params or {}, "echo": "http"}).encode()
            headers = {"Content-Type": "application/json"}
            if self.access_token:
                headers["Authorization"] = f"Bearer {self.access_token}"
            request = urllib.request.Request(
                f"{self.base_url}/{action}", data=payload, method="POST", headers=headers
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    result = json.loads(response.read().decode("utf-8", "replace"))
            except urllib.error.HTTPError as exc:
                raise RuntimeError(f"SnowLuma HTTP {action} 失败: status={exc.code}") from exc
            except OSError as exc:
                raise RuntimeError(f"SnowLuma HTTP {action} 失败: {type(exc).__name__}") from exc
            if not isinstance(result, dict):
                raise RuntimeError(f"SnowLuma HTTP {action} 返回格式不正确")
            if result.get("status") not in (None, "ok") or result.get("retcode") not in (None, 0):
                raise RuntimeError(f"SnowLuma HTTP {action} 失败: retcode={result.get('retcode')}")
            return result.get("data")

        return await asyncio.to_thread(sync_call)

    async def group_message_history(
        self, group_id: str, *, count: int = 20, message_seq: int | None = None
    ) -> tuple[dict[str, object], ...]:
        """拉取群消息历史。Bot 只看得见自己启动后的事件流，这是补上下文的关键接口。"""

        params: dict[str, object] = {"group_id": _id_value(group_id), "count": max(1, min(200, count))}
        if message_seq is not None:
            params["message_seq"] = message_seq
        data = await self.call("get_group_msg_history", params)
        if not isinstance(data, dict):
            return ()
        messages = data.get("messages")
        if not isinstance(messages, list):
            return ()
        return tuple(item for item in messages if isinstance(item, dict))

    async def group_members(self, group_id: str) -> tuple[dict[str, object], ...]:
        data = await self.call("get_group_member_list", {"group_id": _id_value(group_id)})
        if not isinstance(data, list):
            return ()
        return tuple(item for item in data if isinstance(item, dict))

    async def group_detail(self, group_id: str) -> dict[str, object]:
        data = await self.call("get_group_detail_info", {"group_id": _id_value(group_id)})
        return data if isinstance(data, dict) else {}

    @staticmethod
    def build_url(base_url: str, path: str = "/", token: str = "") -> str:
        """拼接带 access_token 的 URL，供 wsServer 之类的场景使用。"""

        if not token:
            return base_url.rstrip("/") + path
        separator = "&" if "?" in path else "?"
        return f"{base_url.rstrip('/')}{path}{separator}{urlencode({'access_token': token})}"
