"""表情名字表（`face_names.py`）的离线覆盖。

由来（2026-10-11 用户）：*"你好歹去查询表情的名字给丢到提示词里吧"*。

要钉住六类：

1. **名字从哪来**：`q_sid` / `q_cid` / `em_code` 三栏都能查；目录是**契约那个形状**
   （`{"packs":[{"pack_name","emojis":[…]}]}`），形状不对就返回空——**不许照自己想的形状读**；
2. **码点那一档**：`12951 → ㊗`、`10060 → ❌` 收；`234 → ê`（字母）**不收**（实测它就是查不到）；
3. **读侧不联网**：查名字只读本地那份 JSON；刷新要过**只读能力闸门**（`capabilities.py`）；
4. **失败保留旧表**：拉不到就什么都不写；
5. **样本只多一个字段**：有名字才有 `名字`，没有就与改动前逐字相同；
6. **写死的那一条**（`㊗` = 不赞同）压过模型判的、但**压不过人工覆盖**。
"""
import asyncio
import json
import tempfile
from pathlib import Path

from qq_roleplay_bot import face_names
from qq_roleplay_bot.quote_learning import (
    KNOWN_EMOJI_SENSES,
    SENSE_NEGATIVE,
    SENSE_OTHER,
    SENSE_POSITIVE,
    QuoteStore,
)
from qq_roleplay_bot.quote_samples import QuoteReaction

GROUP = "1084401296"

#: 照 `fetch_sys_faces` 的**契约形状**写的一小份目录（三个 id 分别命中三栏）。
CATALOG = {
    "packs": [
        {"pack_name": "小黄脸表情", "emojis": [
            {"q_sid": "76", "q_des": "/赞", "q_cid": None, "em_code": "179"},
            {"q_sid": "387", "q_des": "/太好笑", "q_cid": None, "em_code": "10387"},
        ]},
        {"pack_name": "emoji 表情", "emojis": [
            {"q_sid": "😄", "q_des": "/高兴", "q_cid": 128516, "em_code": "400826"},
        ]},
    ]
}


def _write_cache(tmp: str, *, faces=()) -> Path:
    path = Path(tmp) / "faces.json"
    path.write_text(json.dumps({
        "version": 1, "updated_at": 1.0, "count": len(faces),
        "faces": [dict(item) for item in faces],
    }, ensure_ascii=False), encoding="utf-8")
    return path


class FakeClient:
    """只实现 `call(action, params)` 的替身；记下每次动作名。"""

    def __init__(self, payload=None, error: Exception | None = None) -> None:
        self.payload = payload
        self.error = error
        self.actions: list[str] = []

    async def call(self, action, params=None):
        self.actions.append(action)
        if self.error is not None:
            raise self.error
        return self.payload


class FakeGate:
    def __init__(self, allowed=True) -> None:
        self.allowed = allowed
        self.checked: list[str] = []

    def check(self, name, *, purpose="read"):
        self.checked.append(f"{name}:{purpose}")
        if not self.allowed:
            raise PermissionError("denied")


def _table(items=(), tmp=None) -> face_names.FaceNames:
    """一份**离线**的表：直接喂目录条目（不走网络、不碰真实 `data/`）。"""
    path = Path(tmp) / "faces.json"
    table = face_names.FaceNames(path)
    assert table.save(tuple(items))
    return table


# --- 1. 三栏 + 契约形状 -------------------------------------------------------


def test_the_catalog_is_read_in_the_shape_the_action_table_documents() -> None:
    """契约写的是 `{"packs":[{"pack_name","emojis":[…]}]}`；照它读，别的形状读不出来。"""

    items = face_names.flatten(CATALOG)
    assert [item["name"] for item in items] == ["赞", "太好笑", "高兴"]
    assert items[0] == {"q_sid": "76", "q_cid": "", "em_code": "179",
                        "name": "赞", "pack": "小黄脸表情"}
    # 认不出的形状 → 空（**不猜**：宁可"没有名字"，也不照一个自己想的形状瞎读）。
    assert face_names.flatten({"data": CATALOG}) == ()
    assert face_names.flatten([CATALOG]) == ()
    assert face_names.flatten(None) == ()


def test_a_name_can_be_found_by_any_of_the_three_columns() -> None:
    """`q_sid=76` → 赞、`em_code=179` → 赞、`q_cid=128516` → 高兴（三栏都建了索引）。"""

    with tempfile.TemporaryDirectory() as tmp:
        table = _table(face_names.flatten(CATALOG), tmp=tmp)
        assert table.label_for("76") == "赞"
        assert table.name_how("76") == "q_sid"
        assert table.label_for("179") == "赞"
        assert table.name_how("179") == "em_code"
        assert table.label_for("128516") == "高兴"
        assert table.name_how("128516") == "q_cid"
        assert table.label_for("387") == "太好笑"
        # 前导斜杠去掉了（QQ 那边叫 `/赞`，她看的是 `赞`）。
        assert "/" not in table.label_for("76")


# --- 2. 码点那一档 -----------------------------------------------------------


def test_a_unicode_codepoint_id_becomes_the_character_itself() -> None:
    """`12951` 与 `10060` 是**码点**（实测 QQ 对 Unicode 表情就是这么报的）。

    它们不在目录里（QQ 的目录没收录这两个），但 `chr()` 是算出来的、不是猜的：
    同一形状在目录里有 5 个 `q_cid` 命中当证据（见 `face_names.py` 的表）。
    """

    assert face_names.unicode_name("12951") == "㊗"
    assert face_names.unicode_name("10060") == "❌"
    assert face_names.unicode_name("128516") == "😄"


def test_a_number_that_is_not_a_symbol_is_not_a_name() -> None:
    """`234` 实测**查不到**：`chr(234) == "ê"` 是字母，不是表情 → 如实返回空。"""

    assert face_names.unicode_name("234") == ""
    assert face_names.unicode_name("65") == ""       # "A"
    assert face_names.unicode_name("abc") == ""
    assert face_names.unicode_name("") == ""
    assert face_names.unicode_name("999999999") == ""
    with tempfile.TemporaryDirectory() as tmp:
        table = face_names.FaceNames(Path(tmp) / "faces.json")
        assert table.label_for("234") == ""
        assert table.label_for("12951") == "㊗"        # 不需要目录也查得到
        assert table.name_how("234") == ""


# --- 3. 刷新：过闸门、只写本地 -------------------------------------------------


def test_refresh_reads_the_catalog_through_the_read_only_gate() -> None:
    """刷新用**仓库里登记的那个动作**，并且先过只读闸门（`capabilities.py`）。"""

    with tempfile.TemporaryDirectory() as tmp:
        client = FakeClient(CATALOG)
        gate = FakeGate()
        table = face_names.FaceNames(Path(tmp) / "faces.json")
        written = asyncio.run(face_names.refresh(client, table, registry=gate))
        assert written == 3
        assert client.actions == ["fetch_sys_faces"], "动作名从契约里来，不是我试出来的"
        assert gate.checked == ["fetch_sys_faces:read"]
        assert table.label_for("76") == "赞"
        assert json.loads((Path(tmp) / "faces.json").read_text(encoding="utf-8"))["count"] == 3


def test_a_denied_or_failed_refresh_writes_nothing() -> None:
    """闸门不放行 / 对面报错 → **一条都不写**，旧表照旧用（失败只记一笔）。"""

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "faces.json"
        table = _table(face_names.flatten(CATALOG)[:1], tmp=tmp)
        assert table.label_for("76") == "赞"
        # 让它**真的旧**（否则 `refresh` 会在闸门之前就返回 0，这条测试就白验了）。
        table.updated_at = 0.0

        denied = FakeClient(CATALOG)
        gate = FakeGate(allowed=False)
        assert asyncio.run(face_names.refresh(denied, table, registry=gate)) == 0
        assert gate.checked == ["fetch_sys_faces:read"], "先过闸门"
        assert denied.actions == [], "闸门不放行就不许发请求"
        broken = FakeClient(error=RuntimeError("连不上"))
        assert asyncio.run(face_names.refresh(broken, table, registry=FakeGate())) == 0
        # 旧表还在（失败不擦掉它）。
        table.reload(force=True)
        assert table.label_for("76") == "赞"
        assert path.exists()


def test_refresh_is_skipped_while_the_table_is_fresh() -> None:
    """没到 TTL 就不拉：`stale()` 为假时**一个请求都不发**（它不是每次学习都打接口）。"""

    with tempfile.TemporaryDirectory() as tmp:
        client = FakeClient(CATALOG)
        table = face_names.FaceNames(Path(tmp) / "faces.json", clock=lambda: 1000.0)
        assert table.stale() is True, "空表就是旧"
        assert asyncio.run(face_names.refresh(client, table, registry=FakeGate())) == 3
        table.updated_at = 1000.0
        assert table.stale(ttl=86400.0) is False
        assert asyncio.run(face_names.refresh(client, table, registry=FakeGate())) == 0
        assert client.actions == ["fetch_sys_faces"], "只该拉一次"


# --- 4. 样本只多一个字段 -------------------------------------------------------


def test_a_learning_sample_gives_the_agent_the_name_not_the_number() -> None:
    """**规则那一步在代码里做完**：给学习 agent 的样本只有名字（`表情`），没有编号。

    三种情形都要对：有名字 → 只给名字；查不到名字（`234`）→ 只给编号（那时它是唯一
    能指认它的东西）；**这一批里两个编号重名**（`西瓜` = `89` 和 🍉）→ 那两个退回编号。
    """

    from qq_roleplay_bot.quote_samples import BatchNames

    with tempfile.TemporaryDirectory() as tmp:
        table = _table(face_names.flatten(CATALOG), tmp=tmp)
        face_names.install(table)
        try:
            names = BatchNames(
                by_id={"76": "赞", "128516": "高兴", "89": "西瓜", "128055": "西瓜"},
                by_name={"赞": "76", "高兴": "128516"},
                ambiguous=frozenset({"西瓜"}),
            )
            learned = QuoteReaction(emoji_id="76", user_id="1").as_data(
                for_learning=True, names=names)
            assert learned == {"表情": "赞", "by": "1"}, "有名字就只给名字"
            assert "emoji_id" not in learned and "名字" not in learned
            # 查不到名字 → 编号（这是唯一能指认它的东西）。
            assert QuoteReaction(emoji_id="234", user_id="1").as_data(
                for_learning=True, names=names) == {"emoji_id": "234", "by": "1"}
            # 这一批里重名 → 退回编号（不猜是哪一个）。
            assert QuoteReaction(emoji_id="89", user_id="1").as_data(
                for_learning=True, names=names) == {"emoji_id": "89", "by": "1"}
            # 不重名的那个照旧只给名字。
            assert QuoteReaction(emoji_id="128516", user_id="1").as_data(
                for_learning=True, names=names) == {"表情": "高兴", "by": "1"}
            # 名字 → 编号（解析它回话时用的就是这一份）。
            assert names.resolve("赞") == "76"
            assert names.resolve("西瓜") == "", "重名的翻不回去（那时样本给的就是编号）"
            assert names.resolve("它编的") == ""
        finally:
            face_names.install(None)


def test_a_sample_carries_the_name_only_when_it_is_known() -> None:
    """有名字才有 `名字` 那一项；查不到就**没有这个键**（与改动前逐字相同）。"""

    with tempfile.TemporaryDirectory() as tmp:
        table = _table(face_names.flatten(CATALOG), tmp=tmp)
        face_names.install(table)
        try:
            known = QuoteReaction(emoji_id="76", user_id="1", count=2)
            assert known.as_data() == {"emoji_id": "76", "名字": "赞", "by": "1", "count": 2}
            point = QuoteReaction(emoji_id="12951", user_id="1")
            assert point.as_data()["名字"] == "㊗"
            unknown = QuoteReaction(emoji_id="234", user_id="1", sub_type="add")
            assert unknown.as_data() == {"emoji_id": "234", "by": "1", "sub_type": "add"}
        finally:
            face_names.install(None)


def test_without_any_table_the_sample_is_byte_for_byte_the_old_one() -> None:
    """**没有名字表时**：样本里的字段与改动前一个不差（这条是"关掉＝没改"的判据）。"""

    with tempfile.TemporaryDirectory() as tmp:
        face_names.install(face_names.FaceNames(Path(tmp) / "faces.json"))
        try:
            assert QuoteReaction(emoji_id="76", user_id="9").as_data() == {
                "emoji_id": "76", "by": "9"}
        finally:
            face_names.install(None)


# --- 5. 写死的那一条（㊗ = 不赞同）-------------------------------------------


def test_the_written_in_emoji_is_negative_even_when_the_model_said_otherwise() -> None:
    """`㊗` 的旧数据被模型判成 `other` → **照样按不赞同算**（不用重跑、不用改文件）。"""

    with tempfile.TemporaryDirectory() as tmp:
        store = QuoteStore(Path(tmp) / "profile.json")
        store.apply_run(notes=[], meanings={GROUP: {
            "12951": {"sense": SENSE_OTHER, "note": "说不清是夸还是逗"}}}, seen=[])
        assert store.learned_sense(GROUP, "12951")[0] == SENSE_OTHER, "文件里那一条没被改写"
        assert store.effective_sense(GROUP, "12951") == SENSE_NEGATIVE
        assert store.sense_source(GROUP, "12951") == "写死"
        assert store.effective_note(GROUP, "12951") == KNOWN_EMOJI_SENSES["12951"][1]
        # 进材料的那个称呼是**名字**，不是那串数字（她看不懂 12951）。
        material = store.note_for_emoji(GROUP, "12951")
        assert "12951" not in material and "㊗" in material and "不对路" in material


def test_an_operator_override_still_wins_over_the_written_in_one() -> None:
    """人工覆盖压过写死的那条（他哪天改口，一条命令就能改掉）。"""

    with tempfile.TemporaryDirectory() as tmp:
        store = QuoteStore(Path(tmp) / "profile.json")
        assert store.effective_sense(GROUP, "12951") == SENSE_NEGATIVE
        store.override_sense(GROUP, "12951", "赞成", "他说这个群是捧场")
        assert store.effective_sense(GROUP, "12951") == SENSE_POSITIVE
        assert store.sense_source(GROUP, "12951") == "人工"
        store.clear_override(GROUP, "12951")
        assert store.effective_sense(GROUP, "12951") == SENSE_NEGATIVE


def test_only_one_emoji_has_a_written_in_sense() -> None:
    """**只有一个先验**：`12951`。别的 id 一律由模型从语境推（想加先得用户点名）。"""

    assert set(KNOWN_EMOJI_SENSES) == {"12951"}
    assert KNOWN_EMOJI_SENSES["12951"][0] == SENSE_NEGATIVE
