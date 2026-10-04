"""对话日志（**实际收发**）的离线覆盖。

由来（用户 2026-10-05）："日志属于底层设施……我的建议是**对话日志和模型日志分开**"。
起因是一次真事故——要查"她到底发出去过什么"，`reply.jsonl` 里只有模型生成的原文，
而群里实际收到的是被拆成几条发出去的分段，那几条日志里一条都没有，于是查不出来。
所以这里的判据就一句：**日志里必须是"实际收发"的那几条**，而不是模型写了什么。

必须钉住的六条：

1. 发一条文本 → 正好一条记录，正文与实发**逐字相同**；
2. 一次回复拆 3 段发出去 → 3 条，第几段/共几段与顺序都对；
3. 发送失败 → `outcome="failed"`（**不假装成功**）；
4. 收到一条 → `direction="in"`，发送者与正文正确；
5. 超过保留上限 → 旧的被裁掉，文件不会无限增长；
6. **分开**：写对话日志不会往模型日志里写，反过来也一样。

最后一组用例走的是**真实路径**：真的起一个反向 WS 传输层、真的跑 `_deliver_reply`
发三段，再拿**对面实际收到的三条**去对日志——不是"代码看起来对"。

本文件里的临时目录都在测试内部自己建（离线测试入口不用 pytest fixture）。
"""
import asyncio
import json
import os
import socket
import tempfile
from pathlib import Path
from unittest.mock import patch

from qq_roleplay_bot import dev_config
from qq_roleplay_bot.chat_log import ChatLog, chat_log_capacity
from qq_roleplay_bot.feature_log import FeatureLog, FeatureLogs
from qq_roleplay_bot.onebot_ws import OneBotWebSocketTransport
from qq_roleplay_bot.outbox import Outbox
from qq_roleplay_bot.stage3_main import DialogueEngine, _deliver_reply
from qq_roleplay_bot.transport import (
    DeliveryRejected,
    IncomingMessage,
    MessageTarget,
    MessageNotDelivered,
    OutgoingMessage,
)

GROUP = "717151356"
TARGET = MessageTarget(group_id=GROUP)


def msg(text: str = "@YunRu 在吗", *, message_id: str = "m-in-1") -> IncomingMessage:
    return IncomingMessage(
        message_id=message_id, session_id=f"group:{GROUP}", user_id="900000003",
        text=text, target=TARGET, is_bot_mentioned=True, sender_name="小明",
    )


def read_entries(directory: Path | str, name: str = "chat") -> list[dict]:
    path = Path(directory) / f"{name}.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def _transport(tmp: str, *, replies: list | None = None, error: Exception | None = None):
    """真的 `OneBotWebSocketTransport`，只把最外那一跳（socket）换成假的。

    这一层以下（分段参数 → `_send_logged` → `ChatLog` → 落盘）全是真的。
    """

    transport = OneBotWebSocketTransport(chat_log=ChatLog(tmp, enabled=True))
    sent: list[object] = []

    async def fake_call_api(action, params=None):
        sent.append((params or {}).get("message"))
        if error is not None:
            raise error
        if replies is None:
            return {"status": "ok", "retcode": 0, "data": {"message_id": 1000 + len(sent)}}
        return replies.pop(0)

    transport.call_api = fake_call_api  # type: ignore[assignment]
    return transport, sent


# --- 1. 一条文本：正好一条，逐字相同 ----------------------------------------


def test_one_text_message_is_logged_exactly_once_and_verbatim() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        async def run():
            transport, sent = _transport(tmp)
            await transport.send(TARGET, "在的，刚看到。")
            return sent

        sent = asyncio.run(run())
        entries = read_entries(tmp)
        assert len(entries) == 1
        entry = entries[0]
        # 与**实发**逐字相同（这里 `sent` 是真正交给 OneBot 的那份 payload）。
        assert sent == ["在的，刚看到。"]
        assert entry["body"] == "在的，刚看到。"
        assert entry["direction"] == "out"
        assert entry["session_id"] == f"group:{GROUP}"
        assert entry["group_id"] == GROUP
        assert entry["kind"] == "text"
        assert entry["message_id"] == "1001"  # 回执里的那个 id
        assert entry["outcome"] == "ok"
        assert isinstance(entry["at"], float) and entry["at"] > 0
        # 私聊目标记 user_id、不记 group_id。
        assert "user_id" not in entry


def test_quoted_send_records_which_message_it_replies_to() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        async def run():
            transport, _ = _transport(tmp)
            await transport.send(TARGET, "接着上面说", reply_to="42")

        asyncio.run(run())
        entry = read_entries(tmp)[0]
        assert entry["reply_to"] == "42"
        assert entry["body"] == "接着上面说"


def test_image_send_logs_a_readable_description_not_base64() -> None:
    """插件/帮助卡片发的图也要有记录，但**绝不能把 base64 写进日志**。"""

    with tempfile.TemporaryDirectory() as tmp:
        async def run():
            transport, _ = _transport(tmp)
            await transport.send_image(TARGET, b"\x89PNG" + b"x" * 2000)

        asyncio.run(run())
        entries = read_entries(tmp)
        assert len(entries) == 1
        assert entries[0]["kind"] == "image"
        assert "图片 1 张" in entries[0]["body"] and "2004 字节" in entries[0]["body"]
        raw = (Path(tmp) / "chat.jsonl").read_text(encoding="utf-8")
        assert "base64" not in raw and "iVBOR" not in raw


# --- 2. 拆三段：3 条，第几段/共几段与顺序都对 --------------------------------


def test_three_segments_are_logged_in_order_with_part_numbers() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        async def run():
            transport, sent = _transport(tmp)
            engine = DialogueEngine(object(), typing_sim=False)
            outgoing = [OutgoingMessage(TARGET, text, "", "", paced=False)
                        for text in ("第一段。", "第二段。", "第三段。")]
            await _deliver_reply(transport, engine, msg(), outgoing, outbox=Outbox())
            return sent

        sent = asyncio.run(run())
        entries = read_entries(tmp)
        assert len(entries) == 3
        assert sent == ["第一段。", "第二段。", "第三段。"]
        assert [e["body"] for e in entries] == sent
        assert [e["part"] for e in entries] == [1, 2, 3]
        assert [e["total"] for e in entries] == [3, 3, 3]
        assert [e["message_id"] for e in entries] == ["1001", "1002", "1003"]
        # origin 指回触发这一轮的那条收到的消息（便于和模型日志按会话+时间对上）。
        assert {e["origin"] for e in entries} == {"message:m-in-1"}
        assert {e["outcome"] for e in entries} == {"ok"}


def test_a_single_reply_segment_is_still_logged_as_part_one_of_one() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        async def run():
            transport, _ = _transport(tmp)
            engine = DialogueEngine(object(), typing_sim=False)
            await _deliver_reply(transport, engine, msg(),
                                 [OutgoingMessage(TARGET, "就一句。", "", "", paced=False)],
                                 outbox=Outbox())

        asyncio.run(run())
        entry = read_entries(tmp)[0]
        assert (entry["part"], entry["total"]) == (1, 1)


# --- 3. 发送失败：照实记，不假装成功 ----------------------------------------


def test_failed_send_is_logged_as_failed_and_still_raises() -> None:
    for error in (DeliveryRejected("OneBot send_msg 失败: retcode=100"),
                  MessageNotDelivered("OneBot WebSocket 尚未连接")):
        with tempfile.TemporaryDirectory() as tmp:
            async def run():
                transport, _ = _transport(tmp, error=error)
                try:
                    await transport.send(TARGET, "这句话发不出去")
                except type(error):
                    return "raised"
                return "swallowed"

            # 失败分类不能因为"要记日志"而被吞掉：Outbox 能不能补发全靠它。
            assert asyncio.run(run()) == "raised"
            entry = read_entries(tmp)[0]
            assert entry["body"] == "这句话发不出去"
            assert entry["outcome"] == "failed"
            assert entry["error"] == type(error).__name__
            assert entry["error_detail"]
            # 没有回执就没有 id：留空，**不拿 echo 或序号编一个**。
            assert entry["message_id"] == ""


def test_success_without_message_id_in_the_receipt_is_marked_as_missing() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        async def run():
            transport, _ = _transport(tmp, replies=[{"status": "ok", "retcode": 0,
                                                     "echo": "abc123", "data": {}}])
            await transport.send(TARGET, "对面没给 id")

        asyncio.run(run())
        entry = read_entries(tmp)[0]
        assert entry["outcome"] == "ok"
        assert entry["message_id"] == ""
        assert entry["message_id_missing"] is True
        # echo 是请求编号，不是消息编号——绝不能拿来冒充。
        text = (Path(tmp) / "chat.jsonl").read_text(encoding="utf-8")
        assert "abc123" not in text


# --- 4. 收到的消息 ----------------------------------------------------------


def test_incoming_message_is_logged_with_sender_and_body() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        async def run():
            transport = OneBotWebSocketTransport(chat_log=ChatLog(tmp, enabled=True))
            await transport._handle_payload(json.dumps({
                "post_type": "message", "message_type": "group", "message_id": 88,
                "user_id": 900000003, "group_id": int(GROUP), "self_id": 900000002,
                "sender": {"nickname": "小明", "card": "群里的旧名", "role": "member"},
                "message": [{"type": "text", "data": {"text": "她刚才说了什么"}}],
            }))

        asyncio.run(run())
        entries = read_entries(tmp)
        assert len(entries) == 1
        entry = entries[0]
        assert entry["direction"] == "in"
        assert entry["session_id"] == f"group:{GROUP}"
        assert entry["group_id"] == GROUP
        assert entry["user_id"] == "900000003"
        assert entry["sender_name"] == "小明"
        assert entry["sender_card"] == "群里的旧名"
        assert entry["body"] == "她刚才说了什么"
        assert entry["message_id"] == "88"
        assert entry["has_media"] is False


def test_incoming_media_is_logged_as_a_placeholder_never_as_base64() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        async def run():
            transport = OneBotWebSocketTransport(chat_log=ChatLog(tmp, enabled=True))
            await transport._handle_payload(json.dumps({
                "post_type": "message", "message_type": "group", "message_id": 89,
                "user_id": 900000003, "group_id": int(GROUP), "self_id": 900000002,
                "sender": {"nickname": "小明"},
                "message": [{"type": "image",
                             "data": {"file": "base64://iVBORw0KGgoAAAANSUhEUg"}}],
            }))

        asyncio.run(run())
        entry = read_entries(tmp)[0]
        assert entry["has_media"] is True
        assert "[图片]" in entry["body"]
        assert "iVBOR" not in json.dumps(entry, ensure_ascii=False)


def test_non_message_events_are_not_logged() -> None:
    """心跳/通知不是"收到的消息"，它进不了对话日志（也不进核心）。"""

    with tempfile.TemporaryDirectory() as tmp:
        async def run():
            transport = OneBotWebSocketTransport(chat_log=ChatLog(tmp, enabled=True))
            await transport._handle_payload(json.dumps({"post_type": "meta_event",
                                                        "meta_event_type": "heartbeat"}))

        asyncio.run(run())
        assert read_entries(tmp) == []


# --- 5. 保留期：不会无限增长 ------------------------------------------------


def test_capacity_keeps_only_the_newest_entries() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        log = ChatLog(tmp, capacity=10, enabled=True)
        for index in range(25):
            log.record_outgoing(TARGET, body=f"第{index}条")
        entries = read_entries(tmp)
        # 轮转留一倍余量（与模型日志同一个做法）：文件里是 capacity..capacity×2 条。
        assert 10 <= len(entries) <= 20
        assert entries[-1]["body"] == "第24条"
        assert "第0条" not in [entry["body"] for entry in entries]
        assert log.snapshot()["lines"] == len(entries)
        assert log.snapshot()["capacity"] == 10


def test_capacity_config_is_bounded_so_the_file_can_never_grow_without_limit() -> None:
    with patch.dict(os.environ, {"QQBOT_CHAT_LOG_MAX": "0"}):
        assert chat_log_capacity() == 10          # 0/负数被兜住，不是"不留"
    with patch.dict(os.environ, {"QQBOT_CHAT_LOG_MAX": "-5"}):
        assert chat_log_capacity() == 10
    with patch.dict(os.environ, {"QQBOT_CHAT_LOG_MAX": "不许数"}):
        assert chat_log_capacity() == dev_config.CHAT_LOG_MAX
    with patch.dict(os.environ, {"QQBOT_CHAT_LOG_MAX": "999999999999"}):
        assert chat_log_capacity() == 1_000_000
    os.environ.pop("QQBOT_CHAT_LOG_MAX", None)
    assert chat_log_capacity() == dev_config.CHAT_LOG_MAX
    # 出厂的默认量级：20000 条（理由写在 dev_config.CHAT_LOG_MAX 那一段）。
    with patch.object(dev_config, "CHAT_LOG_MAX", 20000):
        os.environ.pop("QQBOT_CHAT_LOG_MAX", None)
        assert chat_log_capacity() == 20000


def test_disabled_chat_log_writes_nothing() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        log = ChatLog(tmp, enabled=False)
        log.record_outgoing(TARGET, body="不该落盘")
        assert not (Path(tmp) / "chat.jsonl").exists()
        assert log.snapshot()["recorded"] == 0


# --- 6. 与模型日志分开：各写各的 --------------------------------------------


def test_chat_log_and_model_log_do_not_write_into_each_other() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        chat = ChatLog(tmp, enabled=True)
        feature = FeatureLog("reply", tmp, capacity=100, enabled=True)

        chat.record_outgoing(TARGET, body="只该进 chat.jsonl")
        assert [entry["body"] for entry in read_entries(tmp)] == ["只该进 chat.jsonl"]
        assert not (Path(tmp) / "reply.jsonl").exists(), "写对话日志不许碰模型日志"

        before = (Path(tmp) / "chat.jsonl").read_text(encoding="utf-8")
        feature.record(input="只该进 reply.jsonl", output="模型原文")
        assert (Path(tmp) / "reply.jsonl").exists()
        assert (Path(tmp) / "chat.jsonl").read_text(encoding="utf-8") == before, \
            "写模型日志不许碰对话日志"

        # 两边的字段形状也不一样：对话日志没有 feature/seq/system/input/output 那一套。
        entry = read_entries(tmp)[0]
        assert not {"feature", "seq", "system", "input", "output"} & set(entry)
        assert "只该进 reply.jsonl" not in before


# --- 真实路径：真的发三段，拿对面收到的去对日志 ------------------------------


class _Model:
    """只回一段固定原始输出的假模型（原文里就是三段，中间是空行）。"""

    def __init__(self, raw: str) -> None:
        self.raw = raw

    async def complete(self, request):
        return self.raw


def _free_port() -> int:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = int(probe.getsockname()[1])
    probe.close()
    return port


def test_three_segments_over_a_real_websocket_are_what_lands_in_the_chat_log() -> None:
    """**实测**：真实的反向 WS 传输层 + 假的 NapCat 客户端 + 真的 `_deliver_reply`。

    判据是"对面实际收到的三条"：日志里必须与它逐字相同、顺序一致、分段编号正确。
    同时验证这份"实际收发"的日志与模型日志（`reply.jsonl`）各写各的。
    """

    from websockets.asyncio.client import connect

    model_reply = "第一段。\n\n第二段。\n\n第三段。"
    raw = ("<decision>REPLY</decision><dialogue>KEEP</dialogue>"
           f"<reply>{model_reply}</reply>"
           "<context>intent=闲聊\ntone=平静\ntarget=bot\npending_question=无\n"
           "confidence=0.8</context>")

    with tempfile.TemporaryDirectory() as tmp:
        async def run() -> list[object]:
            transport = OneBotWebSocketTransport(
                host="127.0.0.1", port=_free_port(),
                chat_log=ChatLog(tmp, enabled=True),
                connection_timeout=5.0, send_timeout=5.0,
            )
            await transport.start()
            # 对面（假 NapCat）真正收到的消息，按到达顺序。
            received: list[object] = []

            async def fake_napcat() -> None:
                async with connect(f"ws://127.0.0.1:{transport.port}") as socket_:
                    async for raw_frame in socket_:
                        payload = json.loads(raw_frame)
                        if "echo" not in payload:            # 只处理 API 请求
                            continue
                        # 传输层还会先发一次 `set_input_status`（"正在输入"）：
                        # 它不是消息，**不该进对话日志**——这里顺手把它排掉。
                        if payload.get("action") == "send_msg":
                            received.append(payload["params"]["message"])
                        await socket_.send(json.dumps({
                            "status": "ok", "retcode": 0, "echo": payload["echo"],
                            "data": {"message_id": 900 + len(received)},
                        }))

            client_task = asyncio.create_task(fake_napcat())
            try:
                engine = DialogueEngine(
                    _Model(raw), typing_sim=False,
                    # 模型日志与对话日志写**同一个**目录：正好检验"互不串门"。
                    feature_logs=FeatureLogs(tmp, capacity=100, enabled=True),
                )
                source = msg()
                reply = await engine.handle(source)
                assert reply is not None, "假模型给的是 REPLY，这里必须有东西可发"
                outgoing = [reply, *engine.take_follow_ups(source.session_id)]
                assert len(outgoing) == 3, f"这一轮应当拆成 3 段，实际 {len(outgoing)}"
                await _deliver_reply(transport, engine, source, outgoing, outbox=Outbox())
            finally:
                client_task.cancel()
                await asyncio.gather(client_task, return_exceptions=True)
                await transport.close()
            return received

        received = asyncio.run(run())
        # 1) 对面真的收到了三条，且就是模型写的那三段（不是整段原文）。
        assert received == ["第一段。", "第二段。", "第三段。"]
        assert model_reply not in received

        # 2) 对话日志里就是那三条，逐字相同、顺序一致、编号正确。
        entries = read_entries(tmp)
        assert [entry["body"] for entry in entries] == received
        assert [entry["part"] for entry in entries] == [1, 2, 3]
        assert [entry["total"] for entry in entries] == [3, 3, 3]
        assert [entry["message_id"] for entry in entries] == ["901", "902", "903"]
        assert {entry["session_id"] for entry in entries} == {f"group:{GROUP}"}
        # **不是模型原文**：原文那一整段（带空行）在一份日志里，实发的三条在另一份。
        assert model_reply not in [entry["body"] for entry in entries]

        # 3) 模型日志仍然只记模型那一份（原文），两边形状不串。
        model_entries = read_entries(tmp, "reply")
        assert len(model_entries) == 1
        assert model_reply in model_entries[0]["output"]
        assert "direction" not in model_entries[0]
        assert all("feature" not in entry for entry in entries)
        assert all(entry["direction"] == "out" for entry in entries)


# --- 只记日志，不改行为 -----------------------------------------------------


def test_chat_log_write_failure_never_breaks_sending() -> None:
    """日志写不进去（这里用目录占住文件名）也**必须照常发出去**。"""

    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "chat.jsonl").mkdir()  # 用目录占位：读写都必然失败

        async def run():
            transport, sent = _transport(tmp)
            await transport.send(TARGET, "日志坏了也得发出去")
            return sent, transport.chat_log.write_failures

        sent, failures = asyncio.run(run())
        assert sent == ["日志坏了也得发出去"]
        assert failures >= 1
