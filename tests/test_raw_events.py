"""原始入站事件日志（**没被处理**的那些事件）的离线覆盖。

由来（2026-10-06）：用户想"用群里的表情回应定位她说过的好句子"，但**没人知道 NapCat
到底推不推这种事件**——传输层只认 `post_type=message`，别的帧被直接丢掉、一点痕迹不留，
于是"没记"和"没发生"从日志里分不出来。这一份把那些事件**原样**记下来，只为看清真实形状。

必须钉住的六条：

1. 一个非消息事件（`notice` / `request` / 非心跳的 `meta_event`）喂进收件口 →
   **原样落盘一行**：字段一个不少、一个不改、不截断；
2. **心跳不落盘**（`meta_event/heartbeat` 每几十秒一条，是噪音），别的 meta 事件照记；
3. 关掉开关 → 一行都不写；
4. 现有行为不变：消息事件照常处理、`chat.jsonl` 照常写，且**不会**串到 raw 那边；
5. 落盘失败（目录占住文件名）→ **不影响收发**（照 `ChatLog` 那条的测法）；
6. 容量有上限、按条滚动（复用 `RollingJsonlFile`，没有第二套轮转）。

这里的所有用例都走**真实的传输层收件口**（`OneBotWebSocketTransport._handle_payload`），
只有"对面那一跳"是假的；要测日志对象本身的用例直接建 `RawEventLog`。
"""
import asyncio
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from qq_roleplay_bot import dev_config
from qq_roleplay_bot.chat_log import ChatLog
from qq_roleplay_bot.onebot_ws import OneBotWebSocketTransport
from qq_roleplay_bot.raw_events import (
    RawEventLog,
    is_heartbeat,
    raw_events_capacity,
    raw_events_enabled,
    should_record,
)
from qq_roleplay_bot.transport import MessageTarget

GROUP = 717151356
SELF_ID = 900000002
TARGET = MessageTarget(group_id=str(GROUP))

# 一个**通用的**通知事件（形状取自 OneBot v11 的 notify 类，不是实测的 NapCat 抓包）。
NOTICE = {
    "post_type": "notice", "notice_type": "group_card",
    "time": 1759734000, "self_id": SELF_ID, "group_id": GROUP,
    "user_id": 900000003, "card_old": "旧名片", "card_new": "新名片",
    "nested": {"list": [1, 2, {"deep": "浅"}], "null": None},
    "empty": "", "zero": 0, "false": False, "unicode": "“引号”与换行\n第二行",
}

# 一条**猜的**表情回应通知：字段名只是为了让用例读起来像那么回事。
# 这一条要验的不是"形状对不对"（那正是还不知道的事），而是"**不按 notice_type 过滤**"——
# 不管 NapCat 把它叫 `group_msg_emoji_like` 还是别的什么，只要推过来就原样落盘。
REACTION_GUESS = {
    "post_type": "notice", "notice_type": "group_msg_emoji_like",
    "time": 1759734123, "self_id": SELF_ID, "group_id": GROUP,
    "user_id": 900000003, "message_id": 4242, "is_add": True,
    "likes": [{"emoji_id": "128512", "emoji_type": 1, "count": 1}],
}


def read_entries(directory: Path | str, name: str = "raw_events") -> list[dict]:
    path = Path(directory) / f"{name}.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def _transport(tmp: str, *, raw: bool = True):
    """真的传输层、真的两份日志，只有"对面那一跳"（socket）是假的。"""

    transport = OneBotWebSocketTransport(
        chat_log=ChatLog(tmp, enabled=True),
        raw_events=RawEventLog(tmp, enabled=raw),
    )
    sent: list[object] = []

    async def fake_call_api(action, params=None):
        sent.append((params or {}).get("message"))
        return {"status": "ok", "retcode": 0, "data": {"message_id": 1000 + len(sent)}}

    transport.call_api = fake_call_api  # type: ignore[assignment]
    return transport, sent


def message_event(message_id: int = 88) -> dict:
    return {
        "post_type": "message", "message_type": "group", "message_id": message_id,
        "user_id": 900000003, "group_id": GROUP, "self_id": SELF_ID,
        "sender": {"nickname": "小明", "role": "member"},
        "message": [{"type": "text", "data": {"text": "她刚才说了什么"}}],
    }


# --- 1. 非消息事件：原样落盘一行 --------------------------------------------


def test_a_notice_event_lands_verbatim_and_never_reaches_the_core() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        async def run():
            transport, _ = _transport(tmp)
            await transport._handle_payload(json.dumps(NOTICE, ensure_ascii=False))
            return transport

        transport = asyncio.run(run())
        entries = read_entries(tmp)
        assert len(entries) == 1
        entry = entries[0]
        # 只加了 `at`，payload 原封不动（逐字段、逐值、含空串/0/False/None/嵌套）。
        assert set(entry) == {"at", "payload"}
        assert entry["payload"] == NOTICE
        assert isinstance(entry["at"], float) and entry["at"] > 0
        # 空值也**没有**被"清理"掉（`ChatLog._clean` 那套不适合这一份：形状要原样）。
        assert entry["payload"]["empty"] == ""
        assert entry["payload"]["zero"] == 0
        assert entry["payload"]["false"] is False
        assert entry["payload"]["nested"]["null"] is None
        # 更没有被截断：整段 Unicode 与换行都在。
        assert entry["payload"]["unicode"] == NOTICE["unicode"]
        # **没进核心**：raw 里有一行 ≠ 她收到了这个事件。
        assert transport._messages.empty()
        # 也不许串进对话日志。
        assert not (Path(tmp) / "chat.jsonl").exists()


def test_a_reaction_notice_is_recorded_whatever_it_is_called() -> None:
    """判据是"没被处理就记"，与 `notice_type` 叫什么无关——这正是要查清的事。"""

    with tempfile.TemporaryDirectory() as tmp:
        async def run():
            transport, _ = _transport(tmp)
            await transport._handle_payload(json.dumps(REACTION_GUESS, ensure_ascii=False))

        asyncio.run(run())
        entry = read_entries(tmp)[0]
        assert entry["payload"] == REACTION_GUESS
        assert entry["payload"]["notice_type"] == "group_msg_emoji_like"


def test_a_request_event_is_recorded() -> None:
    request = {"post_type": "request", "request_type": "group",
               "time": 1759734200, "self_id": SELF_ID, "group_id": GROUP,
               "user_id": 900000004, "comment": "验证留言", "flag": "flag-1"}
    with tempfile.TemporaryDirectory() as tmp:
        async def run():
            transport, _ = _transport(tmp)
            await transport._handle_payload(json.dumps(request, ensure_ascii=False))

        asyncio.run(run())
        assert read_entries(tmp)[0]["payload"] == request


def test_payload_fields_are_never_trimmed() -> None:
    """`ChatLog` 那条"单字段截断"的口径**不该**套到这里：要看清形状就不能先裁。"""

    huge = "长" * 30000
    payload = {"post_type": "notice", "notice_type": "group_upload",
               "file": {"name": "很大的名字.jsonl", "blob": huge}}
    with tempfile.TemporaryDirectory() as tmp:
        log = RawEventLog(tmp, capacity=10, enabled=True)
        assert log.record(payload) is True

        entry = read_entries(tmp)[0]
        assert len(entry["payload"]["file"]["blob"]) == 30000
        assert entry["payload"] == payload


def test_a_late_receipt_is_recorded_too_because_it_used_to_vanish() -> None:
    """配对超时的回执（`echo` 不在 `_pending` 里）以前也是一声不响丢掉的。"""

    late = {"status": "ok", "retcode": 0, "echo": "早就超时了", "data": {"message_id": 7}}
    with tempfile.TemporaryDirectory() as tmp:
        async def run():
            transport, _ = _transport(tmp)
            await transport._handle_payload(json.dumps(late, ensure_ascii=False))

        asyncio.run(run())
        assert read_entries(tmp)[0]["payload"] == late


# --- 2. 心跳不落盘 ----------------------------------------------------------


def test_heartbeat_is_not_recorded_but_other_meta_events_are() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        async def run():
            transport, _ = _transport(tmp)
            await transport._handle_payload(json.dumps(
                {"post_type": "meta_event", "meta_event_type": "heartbeat",
                 "time": 1759734300, "self_id": SELF_ID, "status": {"online": True},
                 "interval": 30000}))
            # 心跳被排掉之后，别的 meta 事件照记（上下线也是"被丢掉的事件"）。
            await transport._handle_payload(json.dumps(
                {"post_type": "meta_event", "meta_event_type": "lifecycle",
                 "sub_type": "connect", "time": 1759734301, "self_id": SELF_ID}))

        asyncio.run(run())
        entries = read_entries(tmp)
        assert len(entries) == 1
        assert entries[0]["payload"]["meta_event_type"] == "lifecycle"


def test_heartbeat_detection_is_exactly_heartbeat() -> None:
    assert is_heartbeat({"post_type": "meta_event", "meta_event_type": "heartbeat"}) is True
    assert is_heartbeat({"post_type": "meta_event", "meta_event_type": "lifecycle"}) is False
    assert is_heartbeat({"post_type": "notice", "notice_type": "heartbeat"}) is False
    assert is_heartbeat({"post_type": "meta_event"}) is False
    assert is_heartbeat("心跳") is False
    # 判据一句话：心跳不记，别的对象都记；不是对象（字符串/None/列表）本来就不该落盘。
    assert should_record({"post_type": "notice"}) is True
    assert should_record({"post_type": "meta_event", "meta_event_type": "heartbeat"}) is False
    assert should_record("不是对象") is False
    assert should_record(None) is False


# --- 3. 关掉开关：一行都不写 ------------------------------------------------


def test_disabled_raw_event_log_writes_nothing() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        log = RawEventLog(tmp, enabled=False)
        assert log.record(NOTICE) is False
        assert not (Path(tmp) / "raw_events.jsonl").exists()
        assert log.snapshot()["recorded"] == 0


def test_one_switch_turns_raw_events_off_and_on_by_default() -> None:
    # `run_offline.py` 会把这个变量置 0（套件里到处在建传输层），所以默认值那一段
    # 用 `dev_config` 的出厂值来验——与 `test_chat_log.py` 对 `CHAT_LOG_MAX` 的测法同一条。
    with patch.dict(os.environ, {"QQBOT_RAW_EVENTS": ""}), \
            patch.object(dev_config, "RAW_EVENTS_ENABLED", True):
        assert raw_events_enabled() is True          # 出厂默认：开
    for off in ("0", "false", "no", "off", " OFF "):
        with patch.dict(os.environ, {"QQBOT_RAW_EVENTS": off}):
            assert raw_events_enabled() is False     # 一键关掉
    with patch.dict(os.environ, {"QQBOT_RAW_EVENTS": "1"}):
        assert raw_events_enabled() is True
    # 开关关掉时，连着喂通知事件也不该落盘。
    with tempfile.TemporaryDirectory() as tmp:
        async def run():
            transport = OneBotWebSocketTransport(
                chat_log=ChatLog(tmp, enabled=True),
                raw_events=RawEventLog(tmp, enabled=False),
            )
            await transport._handle_payload(json.dumps(NOTICE, ensure_ascii=False))

        asyncio.run(run())
        assert read_entries(tmp) == []


# --- 4. 现有行为不变：消息照旧走对话日志 ------------------------------------


def test_message_events_still_go_to_the_chat_log_only() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        async def run():
            transport, _ = _transport(tmp)
            await transport._handle_payload(json.dumps(message_event()))
            chat_after_message = (Path(tmp) / "chat.jsonl").read_text(encoding="utf-8")
            await transport._handle_payload(json.dumps(NOTICE, ensure_ascii=False))
            return transport, chat_after_message

        transport, chat_after_message = asyncio.run(run())
        # 消息：照常进核心队列、照常写 `chat.jsonl`（既有行为一个字没变）。
        assert transport._messages.qsize() == 1
        chat = read_entries(tmp, "chat")
        assert len(chat) == 1 and chat[0]["direction"] == "in"
        assert chat[0]["body"] == "她刚才说了什么"
        # 而且它**没有**被 raw 记一份：raw 里只有那条通知。
        raw = read_entries(tmp)
        assert len(raw) == 1 and raw[0]["payload"] == NOTICE
        # 收通知不会往对话日志里写一个字。
        assert (Path(tmp) / "chat.jsonl").read_text(encoding="utf-8") == chat_after_message


def test_a_message_event_that_the_core_drops_is_still_recorded_raw() -> None:
    """解析失败的消息事件同样是"被丢掉的事件"——正是最难查的一种。"""

    broken = {"post_type": "message", "message_type": "group", "self_id": SELF_ID,
              "message": [{"type": "text", "data": {"text": "没有 user_id"}}]}
    with tempfile.TemporaryDirectory() as tmp:
        async def run():
            transport, _ = _transport(tmp)
            await transport._handle_payload(json.dumps(broken, ensure_ascii=False))
            return transport

        transport = asyncio.run(run())
        assert transport._messages.empty()
        assert read_entries(tmp)[0]["payload"] == broken


# --- 5. 落盘失败不影响收发 --------------------------------------------------


def test_raw_event_write_failure_never_breaks_receiving_or_sending() -> None:
    """raw 日志写不进去（这里用目录占住文件名）也**必须照常收、照常发**。"""

    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "raw_events.jsonl").mkdir()  # 用目录占位：读写都必然失败

        async def run():
            transport, sent = _transport(tmp)
            await transport._handle_payload(json.dumps(NOTICE, ensure_ascii=False))
            await transport._handle_payload(json.dumps(message_event()))
            received = await transport.receive()
            await transport.send(TARGET, "raw 日志坏了也得发出去")
            # 心跳照旧不落盘（失败计数也不该被它推高）。
            failures_before = transport.raw_events.write_failures
            await transport._handle_payload(json.dumps(
                {"post_type": "meta_event", "meta_event_type": "heartbeat"}))
            return sent, received, failures_before, transport.raw_events.write_failures

        sent, received, failures_before, failures_after = asyncio.run(run())
        assert sent == ["raw 日志坏了也得发出去"]          # 发得出去
        assert received is not None and received.text == "她刚才说了什么"  # 收得到
        assert failures_after >= 1                        # 失败照实计数
        assert failures_after == failures_before          # 心跳不落盘 → 不产生失败
        # 对话日志照旧：一条收到的 + 一条发出的。
        chat = read_entries(tmp, "chat")
        assert [entry["direction"] for entry in chat] == ["in", "out"]


# --- 6. 容量：不会无限增长，用的是同一套滚动 --------------------------------


def test_capacity_config_is_bounded_so_the_file_can_never_grow_without_limit() -> None:
    with patch.dict(os.environ, {"QQBOT_RAW_EVENTS_MAX": "0"}):
        assert raw_events_capacity() == 10          # 0/负数被兜住，不是"不留"
    with patch.dict(os.environ, {"QQBOT_RAW_EVENTS_MAX": "-5"}):
        assert raw_events_capacity() == 10
    with patch.dict(os.environ, {"QQBOT_RAW_EVENTS_MAX": "不许数"}):
        assert raw_events_capacity() == dev_config.RAW_EVENTS_MAX
    with patch.dict(os.environ, {"QQBOT_RAW_EVENTS_MAX": "999999999999"}):
        assert raw_events_capacity() == 1_000_000
    os.environ.pop("QQBOT_RAW_EVENTS_MAX", None)
    assert raw_events_capacity() == dev_config.RAW_EVENTS_MAX
    # 出厂的默认量级：5000 条（理由写在 dev_config.RAW_EVENTS_MAX 那一段）。
    with patch.object(dev_config, "RAW_EVENTS_MAX", 5000), \
            patch.dict(os.environ, {"QQBOT_RAW_EVENTS_MAX": ""}):
        assert raw_events_capacity() == 5000


def test_capacity_keeps_only_the_newest_events() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        log = RawEventLog(tmp, capacity=10, enabled=True)
        for index in range(25):
            log.record({"post_type": "notice", "notice_type": "poke", "seq": index})
        entries = read_entries(tmp)
        # 轮转留一倍余量（与模型日志、对话日志同一个做法）。
        assert 10 <= len(entries) <= 20
        assert entries[-1]["payload"]["seq"] == 24
        assert 0 not in [entry["payload"]["seq"] for entry in entries]
        assert log.snapshot()["lines"] == len(entries)
        assert log.snapshot()["capacity"] == 10
