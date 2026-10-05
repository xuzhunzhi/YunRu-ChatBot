"""表情回应（`group_msg_emoji_like`）落进**对话日志**的离线覆盖。

由来（2026-10-06）：用户想做"用群里的表情回应定位她说过的好句子"。第一步只到传输层：
把 NapCat 推来的这种 notice **解析成干净的一行**，与现有 in/out 记录并列写进
`data/logs/chat.jsonl`（`"kind": "reaction"`）。**不做判据**：不判金句、不做 few-shot、
不进核心、不发模型调用。

**下面的 `REAL_*` 全部是实测原文**，逐字段抄自 `run/data/logs/raw_events.jsonl`
（2026-10-06，真实 NapCat 推送），不是照文档里的样例猜的：

    行 3: group_id=1084401296 user_id=242003347 operator_id=242003347
          message_id=1852979907 message_seq=372985 likes=[{"emoji_id":"424","count":2}]
    行 5: group_id=717151356 user_id=2639157559 operator_id=2639157559
          message_id=-1269837352 message_seq=2251 likes=[{"emoji_id":"424","count":1}]
    行 6: 同上，likes=[{"emoji_id":"476","count":1}]
    行 7: 同上，user_id=operator_id=1419930873，likes=[{"emoji_id":"424","count":2}]

实测到的形状（**没有一个字是推断的**）：`post_type=notice`、`notice_type=group_msg_emoji_like`、
`sub_type=add`、`time`、`self_id`、`group_id`、`user_id`、`operator_id`、`message_id`
（**正整数与负数都出现过**）、`message_seq`、`likes`（每项 `emoji_id` 是**字符串**，有 `count`）。
4 条实测帧里 `likes` 恰好都只有一个元素——所以"一帧多个表情"**没有实测样本**，
本文件只用显式的合成输入验"每个元素各写一行"这一条行为（标明是合成的）。

必须钉住的六条：

1. 真实形状的帧喂进**真实收件口** → `chat.jsonl` 里**正好一行**干净记录，字段逐一对得上；
2. 缺字段 / 类型怪（`message_id` 是字符串、缺 `likes`、`emoji_id` 非标量…）→ **不崩**，
   按"能记的记、记不了的丢并计数"处理（理由写在 `reactions.py` 模块头部第 3 条）；
3. 别的 notice（poke / 撤回）**不进**这条记录（它们照旧走 `raw_events.jsonl`）；
4. 消息事件的 in/out 记录**与改动前逐字相同**（预期用**独立重建**的参考字典对，不是拿代码自己的
   产物对自己）；
5. 写盘失败 → 收发不受影响；开关（`QQBOT_CHAT_LOG=0`）关掉 → 一行不写；
6. 原始帧那份 `raw_events.jsonl` **照旧留着**（同一帧两份都写、互不干扰）。

用例都走**真实的传输层收件口**（`OneBotWebSocketTransport._handle_payload`），
只有"对面那一跳"（socket）是假的。
"""
import asyncio
import json
import tempfile
from pathlib import Path

from qq_roleplay_bot.chat_log import ChatLog
from qq_roleplay_bot.onebot_ws import OneBotWebSocketTransport
from qq_roleplay_bot.raw_events import RawEventLog
from qq_roleplay_bot.reactions import (
    REACTION_KIND,
    is_reaction_notice,
    parse_reaction,
)
from qq_roleplay_bot.transport import MessageTarget

GROUP = 717151356
SELF_ID = 3655414729
TARGET = MessageTarget(group_id=str(GROUP))

# --- 实测原文（逐字段抄自 run/data/logs/raw_events.jsonl，见模块头部）--------

# 行 3：另一个群、正整数 message_id、count=2。
REAL_REACTION_POSITIVE = {
    "time": 1791223346, "self_id": SELF_ID, "post_type": "notice",
    "notice_type": "group_msg_emoji_like", "sub_type": "add",
    "group_id": 1084401296, "user_id": 242003347, "operator_id": 242003347,
    "message_id": 1852979907, "message_seq": 372985,
    "likes": [{"emoji_id": "424", "count": 2}],
}

# 行 5：本群、**负数** message_id、count=1。
REAL_REACTION_NEGATIVE = {
    "time": 1791223367, "self_id": SELF_ID, "post_type": "notice",
    "notice_type": "group_msg_emoji_like", "sub_type": "add",
    "group_id": GROUP, "user_id": 2639157559, "operator_id": 2639157559,
    "message_id": -1269837352, "message_seq": 2251,
    "likes": [{"emoji_id": "424", "count": 1}],
}

# 行 6：同一条消息、另一个表情 id。
REAL_REACTION_ANOTHER_EMOJI = {
    "time": 1791223380, "self_id": SELF_ID, "post_type": "notice",
    "notice_type": "group_msg_emoji_like", "sub_type": "add",
    "group_id": GROUP, "user_id": 2639157559, "operator_id": 2639157559,
    "message_id": -1269837352, "message_seq": 2251,
    "likes": [{"emoji_id": "476", "count": 1}],
}

# 行 7：第三个人点同一条消息。
REAL_REACTION_THIRD_USER = {
    "time": 1791223402, "self_id": SELF_ID, "post_type": "notice",
    "notice_type": "group_msg_emoji_like", "sub_type": "add",
    "group_id": GROUP, "user_id": 1419930873, "operator_id": 1419930873,
    "message_id": -1269837352, "message_seq": 2251,
    "likes": [{"emoji_id": "424", "count": 2}],
}

REAL_REACTIONS = (
    REAL_REACTION_POSITIVE, REAL_REACTION_NEGATIVE,
    REAL_REACTION_ANOTHER_EMOJI, REAL_REACTION_THIRD_USER,
)

# 行 4：真实的戳一戳（**不是**表情回应，不许进 reaction 记录）。
REAL_POKE = {
    "time": 1791223351, "self_id": SELF_ID, "post_type": "notice",
    "notice_type": "notify", "sub_type": "poke", "group_id": 1084401296,
    "user_id": 2595055768, "target_id": 2843418910, "action": "戳了戳",
    "suffix": "", "action_img_url": "http://tianquan.gtimg.cn/nudgeaction/item/0/expression.jpg",
}

# 撤回：raw_events 里这一次没抓到（raw 只有 7 行，其中没有 group_recall 帧）。
# 所以这一条是**合成**的，只用来验"别的 notice 不走 reaction 那条路"，
# 字段名照 OneBot v11 的 group_recall 形状写——**不是实测**，别当实测引用。
SYNTHETIC_RECALL = {
    "time": 1759734500, "self_id": SELF_ID, "post_type": "notice",
    "notice_type": "group_recall", "group_id": GROUP,
    "user_id": 900000003, "operator_id": 900000004, "message_id": 4242,
}


def read_entries(directory: Path | str, name: str = "chat") -> list[dict]:
    path = Path(directory) / f"{name}.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def _transport(tmp: str, *, chat: bool = True, raw: bool = True):
    """真的传输层、真的三份落盘口，只有"对面那一跳"（socket）是假的。"""

    chat_log = ChatLog(tmp, enabled=chat)
    transport = OneBotWebSocketTransport(
        chat_log=chat_log,
        raw_events=RawEventLog(tmp, enabled=raw),
    )
    sent: list[object] = []

    async def fake_call_api(action, params=None):
        sent.append((params or {}).get("message"))
        return {"status": "ok", "retcode": 0, "data": {"message_id": 1000 + len(sent)}}

    transport.call_api = fake_call_api  # type: ignore[assignment]
    return transport, sent


def message_event(message_id: int = 88) -> dict:
    """与 `test_chat_log.py` / `test_raw_events.py` 同一个入站消息形状。"""

    return {
        "post_type": "message", "message_type": "group", "message_id": message_id,
        "user_id": 900000003, "group_id": GROUP, "self_id": SELF_ID,
        "sender": {"nickname": "小明", "card": "群里的旧名", "role": "member"},
        "message": [{"type": "text", "data": {"text": "她刚才说了什么"}}],
    }


def feed(tmp: str, *payloads: dict, **kwargs):
    """把若干帧按顺序喂进真实收件口，返回那个传输层。"""

    async def run():
        transport, _ = _transport(tmp, **kwargs)
        for payload in payloads:
            await transport._handle_payload(json.dumps(payload, ensure_ascii=False))
        return transport

    return asyncio.run(run())


# --- 1. 真实帧 → 正好一行干净记录，字段逐一对得上 ---------------------------


def test_a_real_reaction_frame_lands_as_one_clean_line() -> None:
    """行 3 的实测帧：`chat.jsonl` 里**正好一行** `kind="reaction"`，字段逐一对得上。"""

    with tempfile.TemporaryDirectory() as tmp:
        transport = feed(tmp, REAL_REACTION_POSITIVE)
        entries = read_entries(tmp)
        assert len(entries) == 1
        entry = entries[0]

        # 逐字段：**整套键都列出来**（多一个键、少一个键都会红）。
        expected_keys = {"at", "direction", "kind", "session_id", "group_id",
                         "message_id", "user_id", "operator_id", "emoji_id",
                         "count", "sub_type"}
        assert set(entry) == expected_keys, f"键不对: {sorted(set(entry) ^ expected_keys)}"
        assert entry["direction"] == "in"
        assert entry["kind"] == REACTION_KIND == "reaction"
        assert entry["session_id"] == "group:1084401296"
        assert entry["group_id"] == "1084401296"
        assert entry["message_id"] == "1852979907"    # 被反应的那条消息
        assert entry["user_id"] == "242003347"        # 谁点的（照帧里的原值）
        assert entry["operator_id"] == "242003347"    # 操作者（实测与 user_id 恒等，不合并）
        assert entry["emoji_id"] == "424"             # 表情 id（实测是字符串）
        assert entry["count"] == 2                    # 次数（有就记）
        assert entry["sub_type"] == "add"
        assert isinstance(entry["at"], float) and entry["at"] > 0

        # `self_id`（bot 自己）与帧里的 `time` **不进**记录：它们不被要求，也容易和 at 混。
        assert "self_id" not in entry and "time" not in entry
        # 整个序列化里没有帧里那些"本行不该出现"的值。
        raw_text = (Path(tmp) / "chat.jsonl").read_text(encoding="utf-8")
        assert str(SELF_ID) not in raw_text
        # 这一帧**没进核心**：落盘不是"收到"。
        assert transport._messages.empty()
        assert transport.reactions.recorded == 1
        assert transport.reactions.dropped == 0


def test_every_real_frame_maps_to_the_fields_we_expect() -> None:
    """四条实测帧逐条对：**负数 message_id 也是字符串**，count 取原值。"""

    with tempfile.TemporaryDirectory() as tmp:
        feed(tmp, *REAL_REACTIONS)
        entries = read_entries(tmp)
        assert len(entries) == 4
        assert [entry["message_id"] for entry in entries] == [
            "1852979907", "-1269837352", "-1269837352", "-1269837352",
        ]
        assert [entry["emoji_id"] for entry in entries] == ["424", "424", "476", "424"]
        assert [entry["count"] for entry in entries] == [2, 1, 1, 2]
        assert [entry["user_id"] for entry in entries] == [
            "242003347", "2639157559", "2639157559", "1419930873",
        ]
        # 行 5/6 是同一个人对**同一条消息**点了两个不同表情：两条独立记录，都留着。
        assert entries[1]["message_id"] == entries[2]["message_id"]
        assert entries[1]["emoji_id"] != entries[2]["emoji_id"]


def test_one_frame_with_two_likes_writes_two_lines() -> None:
    """一帧多个 `likes` 项时**每项各写一行**（合成输入：实测帧都只有一项，见模块头部）。"""

    with tempfile.TemporaryDirectory() as tmp:
        frame = dict(REAL_REACTION_NEGATIVE)
        frame["likes"] = [{"emoji_id": "424", "count": 2}, {"emoji_id": "476", "count": 1}]
        feed(tmp, frame)
        entries = read_entries(tmp)
        assert len(entries) == 2
        assert [entry["emoji_id"] for entry in entries] == ["424", "476"]
        assert [entry["count"] for entry in entries] == [2, 1]
        assert {entry["message_id"] for entry in entries} == {"-1269837352"}


def test_the_parser_is_pure_and_puts_no_timestamp_in() -> None:
    """`at` 由 `ChatLog` 统一加：解析器不猜时间（与 raw_events 那条"只加一个 at"同一套）。"""

    records = parse_reaction(REAL_REACTION_POSITIVE)
    assert len(records) == 1
    assert "at" not in records[0]
    # 判据只认这一种 notice。
    assert is_reaction_notice(REAL_REACTION_POSITIVE) is True
    assert is_reaction_notice(REAL_POKE) is False
    assert is_reaction_notice({"post_type": "message"}) is False
    assert is_reaction_notice("不是对象") is False
    assert is_reaction_notice(None) is False


# --- 2. 缺字段 / 类型怪：不崩，能记的记、记不了的丢并计数 --------------------


def test_missing_and_odd_typed_fields_never_crash_and_are_counted() -> None:
    """一个字段都不编：缺关键字段就**丢**（计数），其余形状怪异的照能记的记。"""

    with tempfile.TemporaryDirectory() as tmp:
        # (1) 缺 message_id：不知道被反应的是哪条消息 → 丢。
        no_message_id = dict(REAL_REACTION_NEGATIVE)
        no_message_id.pop("message_id")
        # (2) message_id 是空串 → 同样丢。
        empty_message_id = dict(REAL_REACTION_NEGATIVE, message_id="")
        # (3) 缺 likes → 丢。
        no_likes = dict(REAL_REACTION_NEGATIVE)
        no_likes.pop("likes")
        no_likes["message_id"] = 111
        # (4) likes 是空列表 → 丢。
        empty_likes = dict(REAL_REACTION_NEGATIVE, message_id=222, likes=[])
        # (5) emoji_id 非标量（对象）→ 这条丢，**不写 "{}" 这种假 id**。
        weird_emoji = dict(REAL_REACTION_NEGATIVE, message_id=333,
                           likes=[{"emoji_id": {"bad": "shape"}, "count": 1}])
        # (6) message_id 是**字符串**（对面换个形状）→ 照记，转成字符串。
        string_message_id = dict(REAL_REACTION_NEGATIVE, message_id="555",
                                 likes=[{"emoji_id": "424", "count": 1}])
        # (7) 缺 group_id / user_id / operator_id / sub_type / count → 照记能记的。
        bare = dict(REAL_REACTION_NEGATIVE, message_id=777,
                    likes=[{"emoji_id": "424"}])
        bare.pop("group_id")
        bare.pop("user_id")
        bare.pop("operator_id")
        bare.pop("sub_type")

        transport = feed(tmp, no_message_id, empty_message_id, no_likes, empty_likes,
                         weird_emoji, string_message_id, bare)

        entries = read_entries(tmp)
        # 只有 (6) 与 (7) 写得出来。
        assert len(entries) == 2
        assert entries[0]["message_id"] == "555"
        assert entries[0]["group_id"] == str(GROUP)
        assert entries[1]["message_id"] == "777"
        # (7) 缺的字段**干脆不写**（`ChatLog._clean` 去掉空字段），不是写成 null/0。
        for missing in ("group_id", "session_id", "user_id", "operator_id",
                        "sub_type", "count"):
            assert missing not in entries[1], f"{missing} 不该被编出来"
        assert entries[1]["emoji_id"] == "424"
        # 5 帧没能成行 → 照实计数（**不写半条**）。
        assert transport.reactions.dropped == 5
        assert transport.reactions.recorded == 2
        # 一帧都没进核心。
        assert transport._messages.empty()


def test_count_shapes_are_handled_without_inventing_a_number() -> None:
    """`count` 取得到就记原值；取不到就不写这个键（**绝不编一个 1**）。"""

    with tempfile.TemporaryDirectory() as tmp:
        frames = [
            dict(REAL_REACTION_NEGATIVE, message_id=1,
                 likes=[{"emoji_id": "424", "count": "3"}]),      # 字符串数字也收
            dict(REAL_REACTION_NEGATIVE, message_id=2,
                 likes=[{"emoji_id": "424", "count": True}]),     # bool 不是次数
            dict(REAL_REACTION_NEGATIVE, message_id=3,
                 likes=[{"emoji_id": "424", "count": "很多"}]),   # 看不懂就不写
            dict(REAL_REACTION_NEGATIVE, message_id=4,
                 likes=[{"emoji_id": "424", "count": 0}]),        # 0 不是"点了 0 次"
        ]
        feed(tmp, *frames)
        entries = read_entries(tmp)
        assert len(entries) == 4
        assert entries[0]["count"] == 3
        for entry in entries[1:]:
            assert "count" not in entry


def test_the_reaction_does_not_reach_the_core_queue() -> None:
    """它**不是消息**：不进 `_messages`、不发模型调用（这里连 socket 都没起）。"""

    with tempfile.TemporaryDirectory() as tmp:
        transport = feed(tmp, *REAL_REACTIONS)
        assert transport._messages.empty()


# --- 3. 别的 notice 不进这条记录 -------------------------------------------


def test_other_notices_never_land_in_the_reaction_record() -> None:
    """戳一戳 / 撤回照旧**只走 `raw_events.jsonl`**，一个字都不进 reaction 那条记录。"""

    with tempfile.TemporaryDirectory() as tmp:
        transport = feed(tmp, REAL_POKE, SYNTHETIC_RECALL)
        # 对话日志里一条都没有（这一帧不是消息、也不是表情回应）。
        assert read_entries(tmp) == []
        assert transport.reactions.recorded == 0
        assert transport.reactions.dropped == 0     # "不是我的帧"不算丢弃
        # 它们仍然原样留在原始事件日志里（排障底稿照旧）。
        raw = read_entries(tmp, "raw_events")
        assert [item["payload"] for item in raw] == [REAL_POKE, SYNTHETIC_RECALL]
        # 喂进别的 notice 不会影响后面真实表情回应的解析。
        feed(tmp, REAL_REACTION_POSITIVE)
        # 而真实表情回应那一帧**照旧**只进对话日志、不进 raw 那条"别的 notice"的队里。
        assert len(read_entries(tmp, "raw_events")) == 3


def test_a_reaction_frame_is_still_recorded_raw_in_full() -> None:
    """同一帧两份都写：对话日志里是干净的一行，raw 里是**原始底稿**（一个字不改）。"""

    with tempfile.TemporaryDirectory() as tmp:
        feed(tmp, REAL_REACTION_POSITIVE)
        raw = read_entries(tmp, "raw_events")
        assert len(raw) == 1
        assert raw[0]["payload"] == REAL_REACTION_POSITIVE   # 逐字段原样（含 message_seq/self_id）
        chat = read_entries(tmp)
        assert len(chat) == 1 and chat[0]["kind"] == "reaction"
        # 两份不是同一种东西：raw 里那一行没有 chat 的 kind/direction 字段。
        assert "kind" not in raw[0]["payload"] and "direction" not in raw[0]["payload"]


# --- 4. 消息事件的 in/out 记录与改动前逐字相同 -------------------------------


def test_message_in_and_out_records_are_byte_for_byte_unchanged() -> None:
    """判据：拿**独立重建**的参考记录对（不是拿代码自己的产物对自己）。

    参考字典是照 `chat_log.py` 既有形状手写的：`direction` / `session_id` / `group_id` /
    `user_id` / `sender_name` / `sender_card` / `message_id` / `reply_to` / `body` /
    `has_media`（入）与 `direction` / `session_id` / `group_id` / `user_id` / `kind` /
    `body` / `message_id` / `part` / `total` / `reply_to` / `outcome` / `error` /
    `error_detail` / `origin`（出）——**新增了 `kind="reaction"` 这条并列记录，这两条一个键都没动**。
    """

    with tempfile.TemporaryDirectory() as tmp:
        async def run():
            transport, sent = _transport(tmp)
            await transport._handle_payload(json.dumps(message_event()))
            await transport.send(TARGET, "在的，刚看到。")
            # 表情回应插在中间，也不许动这两条的形状。
            await transport._handle_payload(json.dumps(REAL_REACTION_POSITIVE,
                                                       ensure_ascii=False))
            return sent

        sent = asyncio.run(run())
        assert sent == ["在的，刚看到。"]
        entries = read_entries(tmp)
        assert len(entries) == 3
        incoming, outgoing, reaction = entries

        assert incoming == {
            "at": incoming["at"], "direction": "in", "session_id": f"group:{GROUP}",
            "group_id": str(GROUP), "user_id": "900000003", "sender_name": "小明",
            "sender_card": "群里的旧名", "message_id": "88",
            "body": "她刚才说了什么", "has_media": False,
        }
        assert outgoing == {
            "at": outgoing["at"], "direction": "out", "session_id": f"group:{GROUP}",
            "group_id": str(GROUP), "kind": "text",
            "body": "在的，刚看到。", "message_id": "1001", "outcome": "ok",
        }
        # 两处 `at` 是真的时间戳，其余逐字相同（参考字典里没有多、也没有少一个键）。
        # 空串（`reply_to` / `error` / `error_detail` / `origin`）与 `None`（`part` / `total`）
        # 是空值，**照旧**被 `_clean` 去掉——这一条没有因为新增 reaction 记录而改变。
        assert isinstance(incoming["at"], float) and isinstance(outgoing["at"], float)
        assert reaction["kind"] == "reaction"
        # 既有两条记录的落盘顺序没变（先收到的、再发出的，各自一次）。
        assert [entry["direction"] for entry in entries[:2]] == ["in", "out"]


# --- 5. 写盘失败不影响收发；开关关掉一行不写 --------------------------------


def test_reaction_write_failure_never_breaks_receiving_or_sending() -> None:
    """`chat.jsonl` 写不进去（目录占住文件名）也**必须照常收、照常发**。"""

    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "chat.jsonl").mkdir()  # 用目录占位：读写都必然失败

        async def run():
            transport, sent = _transport(tmp)
            await transport._handle_payload(json.dumps(REAL_REACTION_POSITIVE,
                                                       ensure_ascii=False))
            await transport._handle_payload(json.dumps(message_event()))
            received = await transport.receive()
            await transport.send(TARGET, "对话日志坏了也得发出去")
            return sent, received, transport

        sent, received, transport = asyncio.run(run())
        assert sent == ["对话日志坏了也得发出去"]                       # 发得出去
        assert received is not None and received.text == "她刚才说了什么"  # 收得到
        assert transport.chat_log.write_failures >= 1                  # 失败照实计数
        assert transport.reactions.recorded == 1


def test_the_chat_log_switch_turns_reactions_off_too() -> None:
    """`QQBOT_CHAT_LOG=0`（`ChatLog(enabled=False)`）时**一行都不写**——不另开一个开关。"""

    with tempfile.TemporaryDirectory() as tmp:
        transport = feed(tmp, REAL_REACTION_POSITIVE, chat=False)
        assert not (Path(tmp) / "chat.jsonl").exists()
        assert transport.chat_log.snapshot()["recorded"] == 0
        # raw 那一份不受影响（它有自己的开关）。
        assert len(read_entries(tmp, "raw_events")) == 1
        # raw 也关掉时，两份都不写，但**收件口照旧不崩**。
        with tempfile.TemporaryDirectory() as empty_dir:
            transport2 = feed(empty_dir, REAL_REACTION_POSITIVE, chat=False, raw=False)
            assert not (Path(empty_dir) / "chat.jsonl").exists()
            assert not (Path(empty_dir) / "raw_events.jsonl").exists()
            assert transport2._messages.empty()


def test_a_frame_is_still_handled_when_parsing_or_logging_blows_up() -> None:
    """解析/写盘抛异常时：**照旧不崩、raw 照旧留底**（日志坏了不能带走接收循环）。

    `reactions=None` 在构造上的含义是"用默认的那一份"（与 `raw_events=None` 同一套），
    所以这里直接把 `record` 换成一个必然抛异常的替身来验兜底那一层。
    """

    with tempfile.TemporaryDirectory() as tmp:
        async def run():
            transport, _ = _transport(tmp)
            def boom(payload):
                raise RuntimeError("解析/写盘炸了")
            transport.reactions.record = boom  # type: ignore[method-assign]
            await transport._handle_payload(json.dumps(REAL_REACTION_POSITIVE,
                                                       ensure_ascii=False))
            await transport._handle_payload(json.dumps(message_event()))
            received = await transport.receive()
            await transport.send(TARGET, "日志炸了也得能发")
            return received

        received = asyncio.run(run())
        assert received is not None and received.text == "她刚才说了什么"
        # 表情回应那一行没写出来（因为那一层炸了），但**收进去的那条与发出去的那条照旧记**，
        # 原始帧也照旧留底（消息事件按既有口径**不进** raw，所以 raw 里只有那一帧表情回应）。
        entries = read_entries(tmp)
        assert [entry["direction"] for entry in entries] == ["in", "out"]
        assert all(entry.get("kind") != "reaction" for entry in entries)
        assert [item["payload"]["notice_type"] for item in read_entries(tmp, "raw_events")] == \
            ["group_msg_emoji_like"]


# --- 6. 真实 socket：一整条反向 WS 链路上的表情回应 --------------------------


def test_a_reaction_over_a_real_websocket_lands_in_the_chat_log() -> None:
    """**实测**：真的起反向 WS、真的连一个假 NapCat 客户端，把真实帧发进去。

    这一条验的是"整条链路"（`_handle_connection` → `_handle_payload` → 落盘），
    不只是直接调 `_handle_payload`；同时顺手确认它换不来任何出站帧（零模型调用、零发送）。
    """

    import socket

    from websockets.asyncio.client import connect

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = int(probe.getsockname()[1])
    probe.close()

    with tempfile.TemporaryDirectory() as tmp:
        async def run():
            transport = OneBotWebSocketTransport(
                host="127.0.0.1", port=port,
                chat_log=ChatLog(tmp, enabled=True),
                raw_events=RawEventLog(tmp, enabled=True),
                connection_timeout=5.0, send_timeout=5.0,
            )
            await transport.start()
            sent_back: list[object] = []

            async def fake_napcat():
                async with connect(f"ws://127.0.0.1:{port}") as socket_:
                    await socket_.send(json.dumps(REAL_REACTION_POSITIVE, ensure_ascii=False))
                    await socket_.send(json.dumps(REAL_POKE, ensure_ascii=False))
                    await asyncio.sleep(0.3)
                    # 收件口只收不回：这段时间里不该有任何帧回到对面。
                    try:
                        while True:
                            sent_back.append(json.loads(await asyncio.wait_for(socket_.recv(), 0.05)))
                    except (asyncio.TimeoutError, TimeoutError):
                        pass

            task = asyncio.create_task(fake_napcat())
            try:
                # 等帧真的被处理掉（各等一次"消息循环转起来"，而不是赌一个固定时长）。
                for _ in range(20):
                    if read_entries(tmp) and len(read_entries(tmp, "raw_events")) == 2:
                        break
                    await asyncio.sleep(0.05)
                queued = transport._messages.empty()
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await transport.close()
            return transport, sent_back, queued

        transport, sent_back, queued = asyncio.run(run())
        entries = read_entries(tmp)
        assert len(entries) == 1
        assert entries[0]["kind"] == "reaction"
        assert entries[0]["emoji_id"] == "424"
        assert entries[0]["message_id"] == "1852979907"
        # 戳一戳没有进对话日志；它去了 raw。
        assert [item["payload"]["notice_type"] for item in read_entries(tmp, "raw_events")] == \
            ["group_msg_emoji_like", "notify"]
        # 零出站：收到表情回应/戳一戳都换不来一条发给对面的帧。
        assert sent_back == []
        assert queued is True
