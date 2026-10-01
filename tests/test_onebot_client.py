"""OneBot 客户端传输与 SnowLuma HTTP 封装的离线测试。

全部用假 socket，不依赖 SnowLuma 在运行。真实连通性另由人工实测确认。
"""
import asyncio
import contextlib
import json
from unittest.mock import patch

from qq_roleplay_bot.onebot_client import (
    OneBotClientTransport,
    SnowLumaHttpClient,
    _compose_message,
    _id_value,
)
from qq_roleplay_bot.transport import MessageTarget


class _FakeSocket:
    """最小 websocket 替身：可迭代收帧，可记录发帧并按需回执。"""

    def __init__(self, frames=None, *, auto_echo=True):
        self._frames: asyncio.Queue[str | None] = asyncio.Queue()
        self.sent: list[dict[str, object]] = []
        self.closed = False
        self.auto_echo = auto_echo
        for frame in frames or ():
            self._frames.put_nowait(frame if isinstance(frame, str) else json.dumps(frame))

    async def push(self, payload: dict[str, object]) -> None:
        await self._frames.put(json.dumps(payload))

    async def send(self, raw: str) -> None:
        payload = json.loads(raw)
        self.sent.append(payload)
        if self.auto_echo:
            # 立刻回一条成功回执，模拟 SnowLuma 的同步响应。
            await self._frames.put(json.dumps(
                {"status": "ok", "retcode": 0, "echo": payload.get("echo"), "data": {"ok": True}}
            ))

    def __aiter__(self):
        return self

    async def __anext__(self):
        frame = await self._frames.get()
        if frame is None:
            raise StopAsyncIteration
        return frame

    async def close(self) -> None:
        self.closed = True
        await self._frames.put(None)


@contextlib.asynccontextmanager
async def _fake_connect(socket, **kwargs):
    yield socket


async def _start_with_socket(transport: OneBotClientTransport, socket: _FakeSocket):
    """启动传输并等它真正连上假 socket。"""

    def fake_connect(url, **kwargs):
        return _fake_connect(socket, **kwargs)

    with patch("websockets.asyncio.client.connect", fake_connect):
        await transport.start()
        for _ in range(200):
            if transport.connected_once:
                break
            await asyncio.sleep(0.005)
    return transport


# --- 出站消息形状 ---------------------------------------------------------

def test_compose_message_plain_and_quoted() -> None:
    assert _compose_message("你好", "") == "你好"
    assert _compose_message("你好", "42") == [
        {"type": "reply", "data": {"id": "42"}},
        {"type": "text", "data": {"text": "你好"}},
    ]


def test_id_value_prefers_int() -> None:
    assert _id_value("717151356") == 717151356
    assert _id_value("abc") == "abc"


def test_send_uses_group_and_private_actions() -> None:
    async def run() -> None:
        socket = _FakeSocket()
        transport = OneBotClientTransport("ws://127.0.0.1:3001")
        await _start_with_socket(transport, socket)
        try:
            await transport.send(MessageTarget(group_id="717151356"), "群消息")
            await transport.send(MessageTarget(user_id="100"), "私聊消息", reply_to="9")
        finally:
            await transport.close()

        group_call, private_call = socket.sent
        assert group_call["action"] == "send_group_msg"
        assert group_call["params"]["message"] == "群消息"
        assert group_call["params"]["group_id"] == 717151356
        assert private_call["action"] == "send_private_msg"
        assert private_call["params"]["user_id"] == 100
        assert private_call["params"]["message"][0] == {"type": "reply", "data": {"id": "9"}}

    asyncio.run(run())


def test_empty_text_is_not_sent() -> None:
    async def run() -> None:
        socket = _FakeSocket()
        transport = OneBotClientTransport("ws://127.0.0.1:3001")
        await _start_with_socket(transport, socket)
        try:
            await transport.send(MessageTarget(group_id="1"), "")
        finally:
            await transport.close()
        assert socket.sent == []

    asyncio.run(run())


# --- 事件与回执路由 -------------------------------------------------------

def test_message_event_reaches_receive() -> None:
    async def run() -> None:
        socket = _FakeSocket()
        transport = OneBotClientTransport("ws://127.0.0.1:3001")
        await _start_with_socket(transport, socket)
        try:
            await socket.push({
                "post_type": "message", "message_type": "group", "group_id": 717151356,
                "user_id": 100, "message_id": 5, "self_id": 900000002, "message": "你好",
            })
            message = await asyncio.wait_for(transport.receive(), timeout=1.0)
            assert message is not None
            assert message.text == "你好"
            assert message.session_id == "group:717151356"
        finally:
            await transport.close()

    asyncio.run(run())


def test_meta_events_are_not_queued_as_messages() -> None:
    async def run() -> None:
        socket = _FakeSocket(frames=[{"post_type": "meta_event", "meta_event_type": "heartbeat"}])
        transport = OneBotClientTransport("ws://127.0.0.1:3001")
        await _start_with_socket(transport, socket)
        try:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(transport.receive(), timeout=0.3)
                raise AssertionError("meta 事件不应进入业务队列")
        finally:
            await transport.close()

    asyncio.run(run())


def test_unparsable_frame_is_ignored() -> None:
    async def run() -> None:
        socket = _FakeSocket()
        await socket._frames.put("not json at all")
        transport = OneBotClientTransport("ws://127.0.0.1:3001")
        await _start_with_socket(transport, socket)
        try:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(transport.receive(), timeout=0.3)
                raise AssertionError("坏帧不应进入队列")
        finally:
            await transport.close()

    asyncio.run(run())


def test_call_api_raises_on_error_retcode() -> None:
    async def run() -> None:
        socket = _FakeSocket(auto_echo=False)
        transport = OneBotClientTransport("ws://127.0.0.1:3001", request_timeout=1.0)
        await _start_with_socket(transport, socket)
        try:
            async def failing_send(raw):
                payload = json.loads(raw)
                socket.sent.append(payload)
                await socket._frames.put(json.dumps(
                    {"status": "failed", "retcode": 100, "echo": payload.get("echo")}
                ))
            socket.send = failing_send  # type: ignore[assignment]
            try:
                await transport.call_api("get_msg", {"message_id": 1})
            except RuntimeError as exc:
                assert "get_msg" in str(exc)
            else:
                raise AssertionError("retcode 非 0 时应当抛错")
        finally:
            await transport.close()

    asyncio.run(run())


def test_call_api_rejects_invalid_action() -> None:
    async def run() -> None:
        transport = OneBotClientTransport("ws://127.0.0.1:3001")
        for bad in ("", "get msg", "a b"):
            try:
                await transport.call_api(bad)
            except ValueError:
                pass
            else:
                raise AssertionError(f"{bad!r} 应当被拒绝")
        await transport.close()

    asyncio.run(run())


# --- 关闭语义 -------------------------------------------------------------

def test_close_wakes_up_receive() -> None:
    async def run() -> None:
        socket = _FakeSocket()
        transport = OneBotClientTransport("ws://127.0.0.1:3001")
        await _start_with_socket(transport, socket)
        pending = asyncio.create_task(transport.receive())
        await asyncio.sleep(0)
        await transport.close()
        assert await asyncio.wait_for(pending, timeout=1.0) is None

    asyncio.run(run())


def test_connect_failure_does_not_crash_and_retries() -> None:
    async def run() -> None:
        attempts = {"n": 0}

        def failing_connect(url, **kwargs):
            attempts["n"] += 1
            raise OSError("connection refused")

        transport = OneBotClientTransport("ws://127.0.0.1:3001", reconnect_seconds=0.05)
        with patch("websockets.asyncio.client.connect", failing_connect):
            await transport.start()
            await asyncio.sleep(0.4)
            await transport.close()
        # 首次 + 至少一次退避重连，且进程没有崩。
        assert attempts["n"] >= 2, attempts

    asyncio.run(run())


def test_call_api_times_out_when_never_connected() -> None:
    async def run() -> None:
        def failing_connect(url, **kwargs):
            raise OSError("refused")

        transport = OneBotClientTransport("ws://127.0.0.1:3001", request_timeout=0.2)
        with patch("websockets.asyncio.client.connect", failing_connect):
            await transport.start()
            try:
                await transport.call_api("get_status")
            except RuntimeError as exc:
                assert "超时" in str(exc) or "尚未连接" in str(exc)
            else:
                raise AssertionError("未连接时应当在有限时间内失败")
            finally:
                await transport.close()

    asyncio.run(run())


# --- 断线后重连 -----------------------------------------------------------

def test_pending_call_fails_fast_when_socket_drops() -> None:
    async def run() -> None:
        socket = _FakeSocket(auto_echo=False)
        transport = OneBotClientTransport("ws://127.0.0.1:3001", request_timeout=5.0)
        await _start_with_socket(transport, socket)
        try:
            pending = asyncio.create_task(transport.call_api("get_status"))
            await asyncio.sleep(0.05)
            await socket.close()  # 模拟对端断开
            try:
                await asyncio.wait_for(pending, timeout=1.0)
            except RuntimeError as exc:
                assert "断开" in str(exc) or "关闭" in str(exc)
            else:
                raise AssertionError("断线时挂起的请求应当立刻失败，而不是等满超时")
        finally:
            await transport.close()

    asyncio.run(run())


# --- SnowLuma HTTP 封装 ---------------------------------------------------

def test_http_client_returns_data_field() -> None:
    async def run() -> None:
        captured = {}

        class _Response:
            def __init__(self, body): self._body = body
            def __enter__(self): return self
            def __exit__(self, *exc): return False
            def read(self): return json.dumps(self._body).encode()

        def fake_urlopen(request, timeout=None):
            captured["url"] = request.full_url
            captured["body"] = json.loads(request.data)
            captured["auth"] = request.get_header("Authorization")
            return _Response({"status": "ok", "retcode": 0,
                              "data": {"messages": [{"message_id": 1, "user_id": 2}]}})

        client = SnowLumaHttpClient("http://127.0.0.1:3000", "tok")
        with patch("urllib.request.urlopen", fake_urlopen):
            history = await client.group_message_history("717151356", count=5)

        assert captured["url"] == "http://127.0.0.1:3000/get_group_msg_history"
        assert captured["body"]["params"]["group_id"] == 717151356
        assert captured["body"]["params"]["count"] == 5
        assert captured["auth"] == "Bearer tok"
        assert len(history) == 1

    asyncio.run(run())


def test_http_client_raises_on_bad_retcode() -> None:
    async def run() -> None:
        class _Response:
            def __enter__(self): return self
            def __exit__(self, *exc): return False
            def read(self): return json.dumps({"status": "failed", "retcode": 100}).encode()

        client = SnowLumaHttpClient("http://127.0.0.1:3000")
        with patch("urllib.request.urlopen", lambda request, timeout=None: _Response()):
            try:
                await client.group_members("717151356")
            except RuntimeError as exc:
                assert "retcode" in str(exc)
            else:
                raise AssertionError("retcode 非 0 时应当抛错")

    asyncio.run(run())


def test_http_client_count_is_clamped() -> None:
    async def run() -> None:
        captured = {}

        class _Response:
            def __enter__(self): return self
            def __exit__(self, *exc): return False
            def read(self): return json.dumps({"status": "ok", "retcode": 0, "data": {"messages": []}}).encode()

        def fake_urlopen(request, timeout=None):
            captured["body"] = json.loads(request.data)
            return _Response()

        client = SnowLumaHttpClient("http://127.0.0.1:3000")
        with patch("urllib.request.urlopen", fake_urlopen):
            await client.group_message_history("1", count=9999)
        assert captured["body"]["params"]["count"] == 200

    asyncio.run(run())


def test_build_url_appends_token() -> None:
    assert SnowLumaHttpClient.build_url("http://127.0.0.1:3001", "/") == "http://127.0.0.1:3001/"
    assert "access_token=abc" in SnowLumaHttpClient.build_url("http://127.0.0.1:3001", "/", "abc")
