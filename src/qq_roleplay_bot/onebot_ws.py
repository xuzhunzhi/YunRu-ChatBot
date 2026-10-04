from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import uuid
from urllib.parse import parse_qs, urlparse

from websockets.asyncio.server import ServerConnection, Server, serve

from .chat_log import ChatLog
from .media_segments import image_refs, media_markers
from .transport import (
    DeliveryRejected,
    DeliveryUncertain,
    IncomingMessage,
    MessageNotDelivered,
    MessageTarget,
    display_name_from_sender,
    card_from_sender,
)

logger = logging.getLogger(__name__)

# 会话只带 @、不带任何文字时使用的占位文本，用于保留“被叫到”这一事件本身。
MENTION_ONLY_TEXT = "（@YunRu）"
# 只包含媒体、没有文字时使用的占位文本。
MEDIA_ONLY_TEXT = "（发送了一条媒体消息）"
_CQ_AT_PATTERN = re.compile(r"\[CQ:at,[^\]]*?\bqq=(\d+)[^\]]*\]", re.IGNORECASE)
_CQ_REPLY_PATTERN = re.compile(r"\[CQ:reply,[^\]]*?\bid=([^,\]]+)[^\]]*\]", re.IGNORECASE)
_MAX_MEDIA_MARKERS = 8
# "正在输入"是装饰性的，等太久只会拖慢正文，所以给一个很短的上限。
TYPING_NOTICE_TIMEOUT_SECONDS = 1.0


def extract_text(message: object) -> str:
    """从 OneBot 的字符串或消息段列表中提取纯文本（不含媒体占位）。"""
    if isinstance(message, str):
        # 串形态里可能夹带 CQ 码；@ 与 reply 由各自的解析函数处理，这里只去标记。
        return re.sub(r"\[CQ:[^\]]*\]", "", message)

    if not isinstance(message, list):
        return ""

    parts: list[str] = []
    for segment in message:
        if not isinstance(segment, dict):
            continue
        if segment.get("type") == "text":
            data = segment.get("data")
            if isinstance(data, dict) and isinstance(data.get("text"), str):
                parts.append(data["text"])
    return "".join(parts)


def extract_media_markers(message: object, *, limit: int = _MAX_MEDIA_MARKERS) -> tuple[str, ...]:
    """把非文本媒体段转成有限数量的占位描述，供模型感知"发的是图片/表情包/语音"。

    判据在 `media_segments`：**表情包也是 `image` 段**，要看段里的字段才知道
    （见那个模块头部那张实测表）。只做本地标记，不下载、不上传、不请求任何外部资源。
    """

    return media_markers(message, limit=limit)


def extract_image_urls(message: object, *, limit: int = _MAX_MEDIA_MARKERS) -> tuple[str, ...]:
    """取出图片段的地址（给识图用）。

    只取 URL，**不下载、不上传**：真正去取图的是模型厂商那一侧（它自己按 URL 拉），
    或者调用方给的是 data URL。取不到地址就返回空元组，占位符照旧留在正文里。
    """

    return tuple(dict.fromkeys(
        url for _, url in image_refs(message, limit=limit) if url
    ))


def extract_media_kinds(message: object, *, limit: int = _MAX_MEDIA_MARKERS) -> tuple[str, ...]:
    """与 `extract_image_urls` **一一对应**的身份：`sticker` 或 `image`。

    识图要靠它知道该按"表情包"还是按"照片"去描述。
    """

    return tuple(kind for kind, url in image_refs(message, limit=limit) if url)


def extract_reply_target(message: object) -> str:
    """提取本条消息引用的消息 ID；段形态与 CQ 码串形态都支持。"""

    if isinstance(message, list):
        for segment in message:
            if not isinstance(segment, dict) or segment.get("type") != "reply":
                continue
            data = segment.get("data")
            if isinstance(data, dict):
                value = data.get("id")
                if isinstance(value, (str, int)) and str(value).strip():
                    return str(value).strip()
        return ""
    if isinstance(message, str):
        match = _CQ_REPLY_PATTERN.search(message)
        return match.group(1).strip() if match else ""
    return ""


def bot_is_mentioned(message: object, self_id: str) -> bool:
    """判断本条消息是否 @ 了机器人；段形态与 CQ 码串形态都必须按 QQ 号精确匹配。"""

    if not self_id:
        return False
    if isinstance(message, list):
        for segment in message:
            if not isinstance(segment, dict) or segment.get("type") != "at":
                continue
            data = segment.get("data")
            if isinstance(data, dict) and str(data.get("qq", "")) == self_id:
                return True
    elif isinstance(message, str):
        # 不能用子串包含：self_id="123" 会被 [CQ:at,qq=1234] 误命中。
        return any(qq == self_id for qq in _CQ_AT_PATTERN.findall(message))
    return False


def extract_mentions(message: object, self_id: str = "") -> tuple[str, ...]:
    """本条消息 @ 到的**其他人**（去掉机器人自己），按出现顺序、去重。

    命令要拿它当参数：`/super addadmin @某人` 靠的就是这里。
    段形态与 CQ 码串形态都支持。
    """

    found: list[str] = []
    if isinstance(message, list):
        for segment in message:
            if not isinstance(segment, dict) or segment.get("type") != "at":
                continue
            data = segment.get("data")
            if isinstance(data, dict):
                value = str(data.get("qq", "")).strip()
                if value:
                    found.append(value)
    elif isinstance(message, str):
        found.extend(qq.strip() for qq in _CQ_AT_PATTERN.findall(message) if qq.strip())
    return tuple(dict.fromkeys(qq for qq in found if qq and qq != self_id))


def parse_message_event(event: dict[str, object]) -> IncomingMessage | None:
    """把 OneBot message 事件转换为内部消息。

    - 只有 @ 没有文字的消息同样有效：它是一次明确的呼叫，用 MENTION_ONLY_TEXT
      占位以便进入后续触发与回复流程，而不是在解析层被丢弃。
    - 图片、表情等媒体段会转成占位描述拼进 text（本地标记，不下载资源），
      因此“只发一张图”也能进入上下文；只有 @ 或只有媒体都不会被丢弃。
    """

    if event.get("post_type") != "message":
        return None

    message_type = event.get("message_type")
    user_id = str(event.get("user_id", ""))
    raw_message = event.get("message")
    text = extract_text(raw_message)
    media = extract_media_markers(raw_message)
    is_bot_mentioned = bot_is_mentioned(raw_message, str(event.get("self_id", "")))
    if not user_id:
        return None
    has_media = bool(media)
    if not text.strip():
        if not is_bot_mentioned and not has_media:
            return None
        text = MENTION_ONLY_TEXT if is_bot_mentioned else MEDIA_ONLY_TEXT
    if media:
        text = f"{text}{''.join(media)}" if text.strip() else "".join(media)

    if message_type == "group":
        group_id = str(event.get("group_id", ""))
        if not group_id:
            return None
        target = MessageTarget(group_id=group_id)
        session_id = f"group:{group_id}"
    elif message_type == "private":
        target = MessageTarget(user_id=user_id)
        session_id = f"private:{user_id}"
    else:
        return None

    sender = event.get("sender")
    sender_role = "unknown"
    if isinstance(sender, dict) and isinstance(sender.get("role"), str):
        sender_role = sender["role"].lower()
    # 显示名统一用 QQ 昵称（群名片跨群不一致、还会随时改）——口径见
    # `transport.display_name_from_sender` 的说明。
    sender_name = display_name_from_sender(sender)
    # 群名片单独带一份：它是**别称**，群里人用它指代这个人（@、转述）。
    sender_card = card_from_sender(sender)

    message_id = str(event.get("message_id") or uuid.uuid4().hex)
    return IncomingMessage(
        message_id=message_id,
        session_id=session_id,
        user_id=user_id,
        text=text,
        target=target,
        is_bot_mentioned=is_bot_mentioned,
        sender_role=sender_role,
        sender_name=sender_name,
        sender_card=sender_card,
        is_bot_message=user_id == str(event.get("self_id", "")),
        reply_to_message_id=extract_reply_target(raw_message),
        mentioned_user_ids=extract_mentions(raw_message, str(event.get("self_id", ""))),
        has_media=has_media,
        media_urls=extract_image_urls(raw_message),
        media_kinds=extract_media_kinds(raw_message),
    )


def receipt_message_id(result: object) -> str:
    """从 OneBot 的回执里取 `message_id`；取不到就返回空串。

    **不猜、也不拿 echo 冒充**：`echo` 是这次请求的编号（请求-回执配对用），
    不是群里那条消息的编号。真拿它当 message_id 写进对话日志，等于给每条记录
    编一个查不到对应消息的假 id——那比留空更坏。
    """

    if not isinstance(result, dict):
        return ""
    for holder in (result.get("data"), result):
        if not isinstance(holder, dict):
            continue
        value = holder.get("message_id")
        if isinstance(value, (str, int)) and str(value).strip():
            return str(value).strip()
    return ""


class OneBotWebSocketTransport:
    """OneBot v11 反向 WebSocket 的最小输入输出实现。"""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8080,
        access_token: str = "",
        send_timeout: float = 15.0,
        connection_timeout: float = 15.0,
        chat_log: ChatLog | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.access_token = access_token
        self.send_timeout = send_timeout
        self.connection_timeout = connection_timeout
        # **对话日志**（实际收发）：默认就挂上——它是底层设施，不是可选装饰
        # （见 `chat_log.py` 头部）。收发两头都在这一层记，所以插件发的图/文、
        # 以及所有收到的消息都跑不掉。`QQBOT_CHAT_LOG=0` 可整体关掉（那时它是空操作）。
        self.chat_log = chat_log if chat_log is not None else ChatLog()
        # None 是被 close() 放入的唤醒哨兵，不是消息。
        self._messages: asyncio.Queue[IncomingMessage | None] = asyncio.Queue()
        self._closed = False
        self._connection: ServerConnection | None = None
        self._server: Server | None = None
        self._connection_ready = asyncio.Event()
        self._pending: dict[str, asyncio.Future[dict[str, object]]] = {}

    async def start(self) -> None:
        self._server = await serve(self._handle_connection, self.host, self.port)
        logger.info("OneBot 反向 WebSocket 监听于 ws://%s:%s", self.host, self.port)

    async def close(self) -> None:
        self._closed = True
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        self._connection = None
        self._connection_ready.clear()
        self._fail_pending(DeliveryUncertain("OneBot WebSocket 已关闭"))
        # 必须唤醒正在 receive() 等待的主循环，否则断连后它会永久挂死。
        self._messages.put_nowait(None)

    @property
    def connected(self) -> bool:
        """对面（NapCat）当前连着没有。`Outbox` 靠它决定要不要试着补发。"""

        return self._connection is not None and self._connection_ready.is_set()

    async def receive(self) -> IncomingMessage | None:
        """返回下一条消息；返回 None 表示传输层已关闭，调用方应结束循环。"""

        message = await self._messages.get()
        if message is not None:
            return message
        if not self._closed:
            # 只可能被显式 close() 放入；防御性还原，避免误判为关闭。
            self._messages.put_nowait(None)
        return None

    def drain_pending(self) -> list[IncomingMessage]:
        """把**已经排队**的消息一次取出来（保持到达顺序，`None` 哨兵留在队里）。

        给主循环排序用（2026-09-30）：命令只要几毫秒，而对话每条要「判定 + 拟人停顿」
        好几秒；串行处理时一条命令会排在几十条对话后面（实测真机等过一分多钟）。
        取出来之后由 `runtime._message_loop` 把命令提到前面处理——
        **顺序本身没变**（同一批里各自的相对顺序照旧），变的只是"哪几条先跑"。
        """

        items: list[IncomingMessage] = []
        while True:
            try:
                item = self._messages.get_nowait()
            except asyncio.QueueEmpty:
                break
            if item is None:
                # 关闭哨兵必须留在队列里：主循环下一次 receive() 还要靠它退出。
                self._messages.put_nowait(None)
                break
            items.append(item)
        return items

    async def send(self, target: MessageTarget, text: str, *, reply_to: str = "") -> None:
        await self._send_text(target, text, reply_to=reply_to)

    async def send_segmented(self, target: MessageTarget, text: str, *, reply_to: str = "",
                             part: int | None = None, total: int | None = None,
                             origin: str = "") -> None:
        """与 `send` 相同，额外把"第几段 / 共几段 / 从哪一轮来"带进对话日志。

        这三个事实**只有调用方知道**（传输层看到的是一条独立的消息），所以由调用方
        传进来，不由传输层猜；拿不到就留空（`part=None` 时日志里不写这两个键）。
        """

        await self._send_text(target, text, reply_to=reply_to,
                              part=part, total=total, origin=origin)

    async def _send_text(self, target: MessageTarget, text: str, *, reply_to: str = "",
                         part: int | None = None, total: int | None = None,
                         origin: str = "") -> None:
        if not text:
            # 空正文本来就没发出去，日志里也不该出现"发了条空的"。
            return
        params: dict[str, object] = {"message": self._compose_message(text, reply_to)}
        if target.group_id is not None:
            params.update(message_type="group", group_id=self._id_value(target.group_id))
        else:
            params.update(message_type="private", user_id=self._id_value(target.user_id or ""))
        await self._send_logged("send_msg", params, target, body=text, kind="text",
                                reply_to=reply_to, part=part, total=total, origin=origin)

    async def send_image(self, target: MessageTarget, png: bytes, *, reply_to: str = "") -> None:
        """发一张图（帮助卡片用）。`png` 是本地字节，走 **base64** 消息段。

        为什么用 `base64://` 而不是 `file://`：`file://` 要对面按路径去读文件，
        路径转义、相对/绝对、跨机器都会出问题；base64 把内容直接放进消息里，
        代价只是体积涨三分之一（帮助卡片一两百 KB，本机 WS 上无所谓）。

        对话日志里**只记一句人能读的描述**（"图片 1 张、多少字节"），
        **绝不记 base64**——否则一条日志几十万字符，翻都翻不动。
        """

        if not png:
            return
        segments: list[dict[str, object]] = []
        if reply_to:
            segments.append({"type": "reply", "data": {"id": reply_to}})
        segments.append({
            "type": "image",
            "data": {"file": "base64://" + base64.b64encode(png).decode("ascii")},
        })
        params: dict[str, object] = {"message": segments}
        if target.group_id is not None:
            params.update(message_type="group", group_id=self._id_value(target.group_id))
        else:
            params.update(message_type="private", user_id=self._id_value(target.user_id or ""))
        await self._send_logged("send_msg", params, target,
                                body=f"（图片 1 张，{len(png)} 字节）", kind="image",
                                reply_to=reply_to, part=None, total=None, origin="")

    async def _send_logged(self, action: str, params: dict[str, object], target: MessageTarget,
                           *, body: str, kind: str, reply_to: str, part: int | None,
                           total: int | None, origin: str) -> None:
        """调一次发送 action，并把**实际发出去的那一条**落进对话日志。

        落点在传输层是有意的：只有这里看得见"真正出了门的那一条"。引擎只知道模型
        写了什么，而群里收到的是被拆开后的每一段；插件发的图/文更是根本不经过引擎。

        顺序也是刻意的：**先发送、再记日志**，失败照原样抛给调用方
        （`Outbox` 的"能不能安全重发"分类靠它）。记日志本身只会往边上记一笔，
        绝不会改变这次发送的结果（`ChatLog` 写盘失败只计数、不抛）。
        """

        try:
            result = await self.call_api(action, params)
        except Exception as exc:  # noqa: BLE001 - 记完这一笔再原样抛出，失败分类不能变
            self._note_chat(target, body=body, kind=kind, reply_to=reply_to, part=part,
                            total=total, origin=origin, outcome="failed",
                            error=type(exc).__name__, error_detail=str(exc))
            raise
        self._note_chat(target, body=body, kind=kind, reply_to=reply_to, part=part,
                        total=total, origin=origin, outcome="ok",
                        message_id=receipt_message_id(result))

    def _note_chat(self, target: MessageTarget, **fields: object) -> None:
        """把一条出站记进对话日志。日志自身出问题只告警，**绝不打断发送**。"""

        log = self.chat_log
        if log is None:
            return
        try:
            log.record_outgoing(target, **fields)  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001 - 日志坏了不能让消息发不出去
            logger.warning("chat_log_out_failed", exc_info=True)

    def _note_chat_in(self, message: IncomingMessage) -> None:
        """把一条收到的消息记进对话日志（同样是"只记日志"）。"""

        log = self.chat_log
        if log is None:
            return
        try:
            log.record_incoming(message)
        except Exception:  # noqa: BLE001
            logger.warning("chat_log_in_failed", exc_info=True)

    async def send_typing(self, target: MessageTarget, notice: str = "typing") -> None:
        """广播一次"正在输入"状态（OneBot v11 `set_input_status`）。

        纯装饰性：不支持该 action 的实现会超时或报错，这里一律吞掉并记 debug。
        QQ 端本来也不会长显这个状态，所以它不影响正文送达，绝不能反过来
        拖慢或打断发送流程。
        """

        params: dict[str, object] = {"event_type": 1 if notice == "typing" else 0}
        if target.group_id is not None:
            params.update(message_type="group", group_id=self._id_value(target.group_id))
        else:
            params.update(message_type="private", user_id=self._id_value(target.user_id or ""))
        try:
            await asyncio.wait_for(
                self.call_api("set_input_status", params),
                timeout=TYPING_NOTICE_TIMEOUT_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001 - 状态提示失败不能影响正文
            logger.debug("set_input_status 未生效: %s", type(exc).__name__)

    @staticmethod
    def _compose_message(text: str, reply_to: str) -> str | list[dict[str, object]]:
        """引用回复时使用消息段；否则保持纯文本，避免改变既有出站形状。"""

        if not reply_to:
            return text
        return [
            {"type": "reply", "data": {"id": reply_to}},
            {"type": "text", "data": {"text": text}},
        ]

    async def call_api(self, action: str, params: dict[str, object] | None = None) -> dict[str, object]:
        """调用 OneBot API 并等待 echo 回执。仅用于只读查询和出站发送。

        失败**按"能不能安全重发"分类**（见 `transport.py` 那三个异常）：
        没送出去的抛 `MessageNotDelivered`，`Outbox` 会留着等连接回来自动补发；
        "发出去了但没回执"抛 `DeliveryUncertain`，**不重发**（避免同一句话出现两遍）。
        """

        if not action or any(character.isspace() for character in action):
            raise ValueError("OneBot action 无效")
        try:
            await asyncio.wait_for(
                self._connection_ready.wait(),
                timeout=self.connection_timeout,
            )
        except asyncio.TimeoutError as exc:
            raise MessageNotDelivered("OneBot WebSocket 连接等待超时") from exc
        connection = self._connection
        if connection is None:
            raise MessageNotDelivered("OneBot WebSocket 尚未连接")

        echo = uuid.uuid4().hex
        future: asyncio.Future[dict[str, object]] = asyncio.get_running_loop().create_future()
        self._pending[echo] = future
        try:
            try:
                await connection.send(json.dumps({"action": action, "params": params or {}, "echo": echo}))
            except Exception as exc:  # noqa: BLE001 - 写入就失败了，对面不可能收到
                raise MessageNotDelivered(
                    f"OneBot 写入失败：{type(exc).__name__}"
                ) from exc
            try:
                result = await asyncio.wait_for(future, timeout=self.send_timeout)
            except asyncio.TimeoutError as exc:
                raise DeliveryUncertain("OneBot 已发出但未收到回执") from exc
            if result.get("status") not in (None, "ok") or result.get("retcode") not in (None, 0):
                raise DeliveryRejected(f"OneBot {action} 失败: {result}")
            return result
        finally:
            self._pending.pop(echo, None)

    async def _handle_connection(self, connection: ServerConnection) -> None:
        if not self._authorized(connection):
            await connection.close(code=1008, reason="Unauthorized")
            return

        old_connection = self._connection
        if old_connection is not None:
            await old_connection.close(code=1012, reason="Replaced by a new connection")

        self._connection = connection
        self._connection_ready.set()
        logger.info("OneBot 已连接")
        try:
            async for raw in connection:
                await self._handle_payload(raw)
        except Exception:
            logger.exception("OneBot WebSocket 连接异常")
        finally:
            if self._connection is connection:
                self._connection = None
                self._connection_ready.clear()
                # 在途请求：**不知道对面收到没有**（字节已经写出去了），所以按"不确定"
                # 抛——`Outbox` 不会重发它，免得同一句话在群里出现两遍。
                self._fail_pending(DeliveryUncertain("OneBot WebSocket 已断开"))
            logger.info("OneBot 已断开")

    async def _handle_payload(self, raw: str | bytes) -> None:
        try:
            payload = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
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

        message = parse_message_event(payload)
        if message is not None:
            # **收到的消息在这里落对话日志**：这是"真正从 QQ 进来"的那一步，
            # 再往上是判定与回复。只记能解析成消息的事件（心跳/通知不记）。
            self._note_chat_in(message)
            await self._messages.put(message)

    def _authorized(self, connection: ServerConnection) -> bool:
        if not self.access_token:
            return True
        headers = getattr(getattr(connection, "request", None), "headers", {})
        authorization = headers.get("Authorization", "")
        if authorization == f"Bearer {self.access_token}":
            return True
        path = getattr(getattr(connection, "request", None), "path", "")
        query_token = parse_qs(urlparse(path).query).get("access_token", [""])[0]
        return query_token == self.access_token

    @staticmethod
    def _id_value(value: str) -> int | str:
        try:
            return int(value)
        except ValueError:
            return value

    def _fail_pending(self, error: Exception) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)
        self._pending.clear()
