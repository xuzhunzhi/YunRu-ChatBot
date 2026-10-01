"""端到端集成：从真实 OneBot 事件到真实出站 payload。

与其余测试的差别：这里不 mock 引擎内部，而是把 OneBot 事件喂给真实传输层，
经真实的 DialogueEngine，再由真实传输层组装出站消息。覆盖"媒体标记 + 引用回复
+ 持久化"三者串起来的实际形状。
"""
import asyncio
import atexit
import json
import os
import shutil
import uuid
from pathlib import Path

from qq_roleplay_bot.onebot_ws import OneBotWebSocketTransport
from qq_roleplay_bot.stage3_main import DialogueEngine
from qq_roleplay_bot.state_store import RuntimeStateStore
from qq_roleplay_bot.transport import MessageTarget

GROUP = "717151356"
# 会话级临时根目录，进程退出时整体回收；带 pid 与 uuid 以免并发运行互相干扰。
_SCRATCH = (
    Path(__file__).resolve().parents[1] / ".tmp_test_run"
    / f"e2e-{os.getpid()}-{uuid.uuid4().hex[:8]}"
)
atexit.register(shutil.rmtree, _SCRATCH, ignore_errors=True)


def _scratch_dir(name: str) -> Path:
    target = _SCRATCH / name
    shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True, exist_ok=True)
    return target


def _event(message_id: str, message, *, user_id: int = 100, self_id: int = 999, mentioned: bool = True):
    return {
        "post_type": "message",
        "message_type": "group",
        "message_id": message_id,
        "user_id": user_id,
        "group_id": int(GROUP),
        "self_id": self_id,
        "message": message,
        "sender": {"nickname": "小明", "role": "member"},
    }


def _private_event(message_id: str, message, *, user_id: int = 100, self_id: int = 999):
    """私聊事件。私聊里"只发一张图"是明确给她看东西，行为与群里不同。"""

    return {
        "post_type": "message",
        "message_type": "private",
        "message_id": message_id,
        "user_id": user_id,
        "self_id": self_id,
        "message": message,
        "sender": {"nickname": "小明", "role": "member"},
    }


class _Client:
    """按顺序返回预设回复，并记录收到的请求。"""

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.requests: list[list[dict[str, str]]] = []

    async def complete(self, request):
        self.requests.append(request)
        return self.replies.pop(0) if self.replies else "<decision>NO_REPLY</decision>"


class _RecordingTransport(OneBotWebSocketTransport):
    """真实传输层 + 拦截 call_api，记录最终出站 payload。"""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.outbound: list[dict[str, object]] = []

    async def call_api(self, action, params=None):
        self.outbound.append({"action": action, "params": params or {}})
        return {"status": "ok", "retcode": 0}


def _run_engine(engine: DialogueEngine, event: dict) -> None:
    """把一条 OneBot 事件按 run() 的路径走完：解析 → 引擎 → 发送。"""

    async def scenario() -> None:
        from qq_roleplay_bot.onebot_ws import parse_message_event

        message = parse_message_event(event)
        assert message is not None, "事件应当在解析层通过"
        reply = await engine.handle(message)
        if reply is not None:
            await engine_transport.send(reply.target, reply.text, reply_to=reply.reply_to_message_id)

    engine_transport = engine._test_transport  # type: ignore[attr-defined]
    asyncio.run(scenario())


def _engine_with_transport(client, **kwargs):
    transport = _RecordingTransport()
    engine = DialogueEngine(client, **kwargs)
    engine._test_transport = transport  # type: ignore[attr-defined]
    return engine, transport


def _send_payloads(transport: _RecordingTransport) -> list[object]:
    return [item["params"]["message"] for item in transport.outbound if item["action"] == "send_msg"]


# --- 文本链路 -------------------------------------------------------------

def test_plain_reply_is_sent_as_plain_text() -> None:
    client = _Client("<decision>REPLY</decision><reply_to>none</reply_to><reply>你好呀</reply>")
    engine, transport = _engine_with_transport(client)
    _run_engine(engine, _event("1", [{"type": "at", "data": {"qq": "999"}},
                                     {"type": "text", "data": {"text": "在吗"}}]))
    assert _send_payloads(transport) == ["你好呀"]


def test_reply_to_current_quotes_the_incoming_message() -> None:
    client = _Client("<decision>REPLY</decision><reply_to>current</reply_to><reply>接你的话</reply>")
    engine, transport = _engine_with_transport(client)
    _run_engine(engine, _event("42", [{"type": "at", "data": {"qq": "999"}},
                                      {"type": "text", "data": {"text": "在吗"}}]))
    assert _send_payloads(transport) == [[
        {"type": "reply", "data": {"id": "42"}},
        {"type": "text", "data": {"text": "接你的话"}},
    ]]


def test_model_cannot_quote_a_message_outside_the_request() -> None:
    """模型给出上下文中不存在的索引时必须降级为不引用。"""

    client = _Client("<decision>REPLY</decision><reply_to>999</reply_to><reply>正常回复</reply>")
    engine, transport = _engine_with_transport(client)
    _run_engine(engine, _event("7", [{"type": "at", "data": {"qq": "999"}},
                                     {"type": "text", "data": {"text": "在吗"}}]))
    assert _send_payloads(transport) == ["正常回复"]


def test_media_only_message_in_private_reaches_model_and_can_be_answered() -> None:
    """私聊里只发一张图 = 明确给她看东西 → 强制触发，立刻能回。"""

    client = _Client("<decision>REPLY</decision><reply>这张图不错</reply>")
    engine, transport = _engine_with_transport(client, private_debug_user_ids=frozenset({"100"}))
    _run_engine(engine, _private_event("8", [
        {"type": "image", "data": {"file": "a.jpg", "url": "http://example.invalid/a.jpg"}},
    ]))
    assert _send_payloads(transport) == ["这张图不错"]
    # 模型确实看到了媒体占位标记，且请求里没有任何图片 URL。
    assert "[图片]" in client.requests[0][1]["content"]
    assert "example.invalid" not in client.requests[0][1]["content"]


def test_group_image_without_being_addressed_is_only_background() -> None:
    """群里别人发的图**不再**强制叫判定（2026-09-27 修正）。

    原来的行为是"只要带媒体就强制触发"，于是一天里 140 次判定（占全部调用的 41%）
    都花在别人发的图上——而她**看不见图**，那 140 次只能答"不接"。
    现在它只作为背景进入历史，跟着 20 条的批量路径走。
    """

    client = _Client("<decision>REPLY</decision><reply>这张图不错</reply>")
    engine, transport = _engine_with_transport(client)
    _run_engine(engine, _event("8", [{"type": "image", "data": {"file": "a.jpg"}}]))
    assert _send_payloads(transport) == [], "群里别人发的图不该立刻触发回复"
    assert client.requests == [], "不该为它调模型"
    # 但它进了历史（她之后能"看到"当时有人发过图）
    history = [m.text for m in engine.sessions.state(f"group:{GROUP}").recent()]
    assert any("[图片]" in text for text in history)


def test_group_image_addressed_to_her_still_triggers() -> None:
    """@ 她 + 一张图：这是明确给她的，照旧强制触发。"""

    client = _Client("<decision>REPLY</decision><reply>看到了</reply>")
    engine, transport = _engine_with_transport(client)
    _run_engine(engine, _event("9", [
        {"type": "at", "data": {"qq": "999"}},
        {"type": "image", "data": {"file": "a.jpg"}},
    ]))
    assert _send_payloads(transport) == ["看到了"]
    assert "[图片]" in client.requests[0][1]["content"]


def test_media_from_other_participant_still_gets_attention() -> None:
    """活跃对话中，别人发的图片也要能拉回注意力。"""

    client = _Client("<decision>REPLY</decision><reply>先回你</reply>",
                     "<decision>REPLY</decision><reply>看到你的图了</reply>")
    engine, transport = _engine_with_transport(client)
    _run_engine(engine, _event("20", [{"type": "at", "data": {"qq": "999"}},
                                      {"type": "text", "data": {"text": "你好"}}], user_id=100))
    _run_engine(engine, _event("21", [{"type": "image", "data": {"file": "b.jpg"}}], user_id=200))
    assert _send_payloads(transport) == ["先回你", "看到你的图了"]


def test_mention_only_message_reaches_model() -> None:
    client = _Client("<decision>REPLY</decision><reply>我在</reply>")
    engine, transport = _engine_with_transport(client)
    _run_engine(engine, _event("9", [{"type": "at", "data": {"qq": "999"}}]))
    assert _send_payloads(transport) == ["我在"]


# --- 与记忆/持久化的交互 --------------------------------------------------

def test_bot_reply_is_recorded_into_short_term_history_after_send() -> None:
    client = _Client("<decision>REPLY</decision><reply>第一句</reply>")
    engine, _ = _engine_with_transport(client)
    _run_engine(engine, _event("10", [{"type": "at", "data": {"qq": "999"}},
                                      {"type": "text", "data": {"text": "你好"}}]))
    history = [m.text for m in engine.sessions.state(f"group:{GROUP}").recent()]
    assert history == ["你好", "第一句"]


def test_ping_does_not_enter_short_term_history() -> None:
    client = _Client()
    engine, transport = _engine_with_transport(client)
    _run_engine(engine, _event("11", [{"type": "text", "data": {"text": "/yunru ping"}}]))
    assert _send_payloads(transport) == ["pong"]
    assert client.requests == []


def test_persisted_state_round_trips_through_real_engine() -> None:
    directory = _scratch_dir("roundtrip")
    path = directory / "runtime_state.json"

    client = _Client("<decision>REPLY</decision><reply>记住了</reply>")
    engine, _ = _engine_with_transport(client, state_store=RuntimeStateStore(path))
    _run_engine(engine, _event("12", [{"type": "at", "data": {"qq": "999"}},
                                      {"type": "text", "data": {"text": "你好"}}]))
    assert engine.persist_state() is True

    restored_engine = DialogueEngine(_Client(), state_store=RuntimeStateStore(path))
    restored_engine.restore_state()
    state = restored_engine.sessions.state(f"group:{GROUP}")
    assert [m.text for m in state.recent()] == ["你好", "记住了"]

    # 落盘内容不含凭据，也不含媒体 URL。
    body = json.loads(path.read_text(encoding="utf-8"))
    assert "api_key" not in json.dumps(body, ensure_ascii=False).lower()


def test_disabled_switch_survives_restart() -> None:
    directory = _scratch_dir("disabled")
    path = directory / "runtime_state.json"

    first = DialogueEngine(_Client(), state_store=RuntimeStateStore(path))
    first.set_enabled(False)
    first.persist_state()

    second = DialogueEngine(_Client(), state_store=RuntimeStateStore(path))
    second.restore_state()
    assert second.enabled is False
    # 关闭状态下除 ping 外不应处理任何消息。
    result = asyncio.run(second.handle(_message("13", "你好")))
    assert result is None


def _message(message_id: str, text: str):
    from qq_roleplay_bot.transport import IncomingMessage

    return IncomingMessage(
        message_id=message_id,
        session_id=f"group:{GROUP}",
        user_id="100",
        text=text,
        target=MessageTarget(group_id=GROUP),
        is_bot_mentioned=True,
    )
