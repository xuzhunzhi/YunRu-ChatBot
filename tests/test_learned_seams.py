"""**"她学来的东西"那条 UI 接缝**（金句 + 黑话）的离线覆盖。

由来（2026-10-06 用户口径）：*"金句我看应该划到**知识库**里"*、*"还有**黑话**"*——
面板要能看、能改这两份数据，但按项目规矩**只能调装配点注入的闭包**
（`AGENTS.md` §2.3：插件不许碰对话、记忆、知识库那类能力，接口要从装配点长出来）。

这个文件钉四类：

1. **函数集合本身**（`LearnedSeams` 的字段名逐个钉住）——多一个"记忆入口"就红；
2. **只有函数、没有引擎**：每一项都是可调用的，而且闭包/绑定方法上拿不到引擎
   （`__self__` 是只带 token 的 `_SeamBinder`）；
3. **读写真的落到那两份 store 上**（金句复用 `quote_learning`、黑话用 `slang_learning`，
   **接缝里不存第二份数据**），改一个字下一轮就读到；
4. **闭包没注入时核心照跑**（`LearnedSeams()` 全是 None、引擎没有那两份数据时，
   读侧返回空结构、对话一个字不变），以及**这一轮黑话不参与她的回复**（本次不做检索/注入）。

另有 **`quote_evidence`（证据读口，2026-10-06 加）** 那一组：用户原话
*"金句，你连原文和上下文都不贴出来怎么审核"*。它钉的是证据本身——
她的**原文**、**复数条**上下文、谁贴的什么表情、以及"结论落到了哪条笔记上"，
对着**落盘的那份 `profile.json`** 造一条真数据来对（不是只对着内存里的对象比）。
"""
import asyncio
import json
import os
import tempfile
from dataclasses import fields
from pathlib import Path
from unittest.mock import patch

from qq_roleplay_bot import runtime
from qq_roleplay_bot.plugins import LearnedSeams, UiSeams
from qq_roleplay_bot.quote_learning import QuoteProfile, QuoteStore, note_id
from qq_roleplay_bot.slang_learning import SlangStore
from qq_roleplay_bot.stage3_main import DialogueEngine
from qq_roleplay_bot.transport import IncomingMessage, MessageTarget

GROUP = "1084401296"
EMOJI = "277"

#: 这份契约**逐字**钉住：字段名就是接口，界面只能调这些。
EXPECTED_FIELDS = (
    "quote_view", "quote_evidence", "quote_correct", "quote_note_enabled",
    "slang_list", "slang_update", "slang_delete", "slang_mark_wrong",
)
#: 按名字判"有没有夹带"的那几个词：出现任何一个就说明这条缝把记忆或对话放进来了。
BANNED_IN_NAMES = ("memory", "deliver", "send", "chat", "notify", "prompt", "engine",
                   "record", "inbox", "profile", "recall")


class _NeverCalled:
    async def complete(self, request):
        raise AssertionError("这条命令不该调模型")


class _FakeTransport:
    """够 `build_engine` 起得来的最小传输层（照 `check_module_removal` 那一份）。"""

    async def call_api(self, action, params=None):
        return {"status": "ok", "retcode": 0,
                "data": {"role": "member", "user_id": "9", "nickname": "n"}}

    async def send(self, target, text, **kwargs):
        return None

    async def start(self):
        return None


class ScriptedReply:
    def __init__(self, text="在。") -> None:
        self.text = text
        self.requests: list = []

    async def complete(self, request):
        self.requests.append(request)
        return (f"<decision>REPLY</decision><dialogue>KEEP</dialogue>"
                f"<reply>{self.text}</reply>")


class ScriptedJudge:
    async def complete(self, request):
        return "<route>REPLY</route>"


def _message(text="在吗") -> IncomingMessage:
    return IncomingMessage(
        message_id=f"m:{text}", session_id=f"group:{GROUP}", user_id="100", text=text,
        target=MessageTarget(group_id=GROUP), sender_name="某人", is_bot_mentioned=True,
    )


def _assembled(tmp: str):
    """走**真的装配点**：`runtime.build_engine()` → `registry.ui.learned`。

    黑话那份落盘位置指到临时目录（`QQBOT_SLANG_PROFILE`），免得测试碰开发树里的 `data/`。
    """

    with patch.dict(os.environ, {"QQBOT_SLANG_PROFILE": str(Path(tmp) / "slang.json")}):
        engine = runtime.build_engine(_FakeTransport())
    return engine, engine.plugin_registry.ui.learned


def _quote_store(tmp: str) -> QuoteStore:
    store = QuoteStore(Path(tmp) / "quote.json")
    store.apply_run(
        notes=[{"kind": "style", "when": "被问事实", "note": "先给结论"}],
        meanings={GROUP: {EMOJI: {"sense": "negative", "note": "这是在损她"}}},
        seen=[],
    )
    return store


# --- 1. 函数集合：契约本身 ---------------------------------------------------


def test_the_function_set_is_pinned_exactly() -> None:
    """字段名逐个钉住：多一个、少一个、改名字都要红（这就是"函数集合"那条判据）。"""

    assert tuple(item.name for item in fields(LearnedSeams)) == EXPECTED_FIELDS
    assert all(getattr(LearnedSeams(), name) is None for name in EXPECTED_FIELDS), \
        "缺省必须是 None：没注入时干净地什么都没有"
    # UI 接缝上有这一项（面板从 `registry.ui.learned` 取）。
    assert "learned" in {item.name for item in fields(UiSeams)}


def test_the_seam_carries_no_memory_or_dialogue_entry() -> None:
    """**按名字**判有没有夹带记忆/对话入口——这是"接缝扩成也暴露记忆入口"那条突变的判据。

    扫的是**当前真实的字段**（不是上面那份清单）：谁把 `memory_ops` 那类东西加进来，
    哪怕顺手也改了 `EXPECTED_FIELDS`，这一条照样红。
    """

    actual = [item.name for item in fields(LearnedSeams)]
    for name in sorted(set(EXPECTED_FIELDS) | set(actual)):
        folded = name.casefold()
        for banned in BANNED_IN_NAMES:
            assert banned not in folded, f"{name} 看着像夹带了 {banned}（记忆/对话入口）"
    assert set(actual) == set(EXPECTED_FIELDS), "字段集合就是契约，多一个少一个都不行"
    with tempfile.TemporaryDirectory() as tmp:
        _, seam = _assembled(tmp)
        for name in EXPECTED_FIELDS:
            function = getattr(seam, name)
            assert callable(function), name
            # **闭包上拿不到引擎**：绑定方法的 owner 是只带一个不透明 token 的 `_SeamBinder`。
            owner = getattr(function, "__self__", None)
            assert type(owner).__name__ == "_SeamBinder", name
            assert type(owner).__slots__ == ("token",), name
            assert isinstance(owner.token, str) and owner.token, name


def test_the_seam_is_injected_at_the_assembly_point() -> None:
    """注入点在 `runtime.build_engine`（面板侧一个字都不用改，照 `UiSeams` 既有形状取用）。"""

    with tempfile.TemporaryDirectory() as tmp:
        engine, seam = _assembled(tmp)
        assert isinstance(seam, LearnedSeams)
        assert engine.plugin_registry.ui.learned is seam
        assert all(callable(getattr(seam, name)) for name in EXPECTED_FIELDS)


# --- 2. 金句：读 / 改（复用 quote_learning 的读写口）------------------------


def test_quote_read_and_edit_go_through_the_quote_store() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        engine, seam = _assembled(tmp)
        store = _quote_store(tmp)
        engine.quote_profile = QuoteProfile(store)      # 装配点接上（`serve` 里就是这么接的）

        view = seam.quote_view(GROUP)
        assert view["group_id"] == GROUP
        meaning = view["meanings"][0]
        assert meaning["emoji_id"] == EMOJI
        assert meaning["sense"] == "negative" and meaning["manual"] is False
        assert meaning["note"] == "这是在损她"
        note = view["notes"][0]
        assert note["when"] == "被问事实" and note["note"] == "先给结论"
        assert note["enabled"] is True and len(note["id"]) == 12
        assert view["path"] == str(store.path)

        # 改：纠正一个表情的方向（写的就是那份 JSON，下一轮回复读到的就是它）。
        fixed = seam.quote_correct(GROUP, EMOJI, "赞成", "其实是在夸她")
        assert fixed == {"group_id": GROUP, "emoji_id": EMOJI, "sense": "positive",
                         "cleared": False, "written": True}
        assert store.effective_sense(GROUP, EMOJI) == "positive"
        assert seam.quote_view(GROUP)["meanings"][0]["manual"] is True
        # 撤：`auto` 那一档与 `/super quote <emoji> auto` 共用一份词表。
        cleared = seam.quote_correct(GROUP, EMOJI, "auto")
        assert cleared["cleared"] is True and cleared["sense"] == "negative"
        assert seam.quote_view(GROUP)["meanings"][0]["manual"] is False

        # 改：停用某条笔记 → **读侧立刻不再给这条材料**。
        off = seam.quote_note_enabled(note["id"], False)
        assert off == {"id": note["id"], "enabled": False, "written": True}
        assert seam.quote_view(GROUP)["notes"][0]["enabled"] is False
        assert QuoteProfile(store).notes_for(GROUP, "被问事实", "被问事实") == []
        again = seam.quote_note_enabled(note["id"], True)
        assert again["enabled"] is True
        assert QuoteProfile(store).notes_for(GROUP, "被问事实", "被问事实") != []

        # 取不到东西时返回空结构，**不抛**（面板不该因为"还没学出来"崩掉）。
        assert seam.quote_view("") == {}
        assert seam.quote_note_enabled("", False) == {}
        assert seam.quote_correct(GROUP, "", "赞成") == {}


def test_a_note_disabled_in_one_place_is_disabled_everywhere() -> None:
    """id 是**内容指纹**：命令、面板、读侧三处算出来必须是同一个（否则停用静默失效）。"""

    with tempfile.TemporaryDirectory() as tmp:
        engine, seam = _assembled(tmp)
        store = _quote_store(tmp)
        engine.quote_profile = QuoteProfile(store)
        key = seam.quote_view(GROUP)["notes"][0]["id"]
        assert store.set_note_enabled(key, False) is True
        store.reload(force=True)
        assert store.note_disabled(key) is True
        assert seam.quote_view(GROUP)["notes"][0]["enabled"] is False


# --- 3. 黑话：读 / 改 -------------------------------------------------------


def test_slang_read_and_edit_go_through_the_slang_store() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        engine, seam = _assembled(tmp)
        store = SlangStore(Path(tmp) / "slang_entries.json")
        engine.slang_library = store

        assert seam.slang_list() == []
        # 改（词不存在 → 新建一条：面板的"手工加一个词"走同一条路）。
        created = seam.slang_update(GROUP, "电赛", "电子设计竞赛")
        assert created["word"] == "电赛" and created["definition"] == "电子设计竞赛"
        rows = seam.slang_list(GROUP)
        assert [item["word"] for item in rows] == ["电赛"]
        # 改（改释义 → 记一次修订）。
        revised = seam.slang_update(GROUP, "电赛", "电子设计大赛")
        assert revised["definition"] == "电子设计大赛"
        assert revised["revisions"][0]["from"] == "电子设计竞赛"
        # 标错 / 取消。
        assert seam.slang_mark_wrong(GROUP, "电赛", True)["wrong"] is True
        assert seam.slang_mark_wrong(GROUP, "电赛", False)["wrong"] is False
        # 护栏在这里也是护栏：含机制词的释义**从面板也塞不进来**。
        assert seam.slang_update(GROUP, "电赛", "上下文里的东西") == {}
        assert seam.slang_update(GROUP, "记忆库", "存东西的地方") == {}
        # 删。
        assert seam.slang_delete(GROUP, "电赛") is True
        assert seam.slang_list() == []
        assert seam.slang_delete(GROUP, "电赛") is False
        assert seam.slang_mark_wrong("", "电赛") == {} and seam.slang_delete("", "") is False


# --- 4. 没有注入时：核心照跑、回复一个字不变 --------------------------------


def test_the_core_runs_with_no_seam_injected() -> None:
    """缺省 `LearnedSeams()`（全 None）与"引擎上没有那两份数据"时：读空、对话照旧。"""

    assert all(getattr(LearnedSeams(), name) is None for name in EXPECTED_FIELDS)
    engine = DialogueEngine(_NeverCalled(), group_listen=True,
                            enabled_group_ids=frozenset({GROUP}))
    seam = runtime._SeamBinder(engine).learned_seams()
    assert seam.quote_view(GROUP) == {}
    assert seam.quote_evidence(GROUP) == {}
    assert seam.quote_correct(GROUP, EMOJI, "赞成") == {}
    assert seam.quote_note_enabled("x", False) == {}
    assert seam.slang_list() == []
    assert seam.slang_update(GROUP, "电赛", "电子设计竞赛") == {}
    assert seam.slang_delete(GROUP, "电赛") is False
    assert seam.slang_mark_wrong(GROUP, "电赛") == {}
    # 核心照跑：没有材料时她照常回一句（模型是替身，这里是"引擎不因为接缝缺东西而变样"）。
    reply = ScriptedReply("在。")
    engine2 = DialogueEngine(reply, judge_client=ScriptedJudge(), group_listen=True,
                             enabled_group_ids=frozenset({GROUP}), typing_sim=False)
    result = asyncio.run(engine2.handle(_message()))
    assert result is not None and result.text == "在。"


def test_the_slang_entries_are_not_injected_into_her_reply_this_round() -> None:
    """**本次不做检索/注入**：有词条与没词条，她的请求**逐字相同**。"""

    def request_with(entries: dict[str, str]) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            store = SlangStore(Path(tmp) / "entries.json")
            for word, definition in entries.items():
                store.apply_explanation(GROUP, word, definition, by_user_id="9", at=1.0)
            reply = ScriptedReply("在。")
            engine = DialogueEngine(reply, judge_client=ScriptedJudge(), group_listen=True,
                                    enabled_group_ids=frozenset({GROUP}), typing_sim=False,
                                    slang_library=store)
            asyncio.run(engine.handle(_message()))
            return json.dumps(reply.requests[0], ensure_ascii=False)

    baseline = request_with({})
    with_entries = request_with({"电赛": "电子设计竞赛", "综测": "综合测评"})
    assert with_entries == baseline, "这一轮词条不许进她的回复（等用户看过效果再说）"


def test_the_slang_store_is_reachable_from_the_command_path() -> None:
    """同一条数据：装配点给的面板接缝与 `/super slang` 读的是**同一份**（不许各存一份）。"""

    with tempfile.TemporaryDirectory() as tmp:
        env = {"QQBOT_SLANG_PROFILE": str(Path(tmp) / "slang.json")}
        with patch.dict(os.environ, env):
            engine = runtime.build_engine(_FakeTransport())
        seam = engine.plugin_registry.ui.learned
        assert seam.slang_update(GROUP, "电赛", "电子设计竞赛") != {}
        assert engine.slang_library.find(GROUP, "电赛")["definition"] == "电子设计竞赛"
        assert "电赛" in engine._slang_reply(_message("/super slang"))


# --- 5. `quote_evidence`：证据读口（2026-10-06 加）---------------------------
#
# 判据来自用户原话：*"金句，你连原文和上下文都不贴出来怎么审核"*。
# 所以这一组只钉一件事：**证据在不在、对不对得上**。

#: 她那条金句（证据的主体）。
HER_ID = "1615997201"
HER_BODY = "插混跑山最舒服的应该是在坡上还能顺手回收一点。"
#: 学习 agent 从这句话里摘的那一句（**照抄**，见 `LEARNING_SYSTEM_PROMPT` 里对 `quote` 的要求）。
#: 它是"结论 ↔ 笔记"那座桥：`quote` 与笔记原文连着 5 个字重合，就算落在那条上。
QUOTE = "坡上还能顺手回收"
#: 它落到了哪条笔记上（`quote` 是这条 `note` 里的一段）。
LINKED_WHEN = "聊到跑山"
LINKED_NOTE = "顺着话讲坡上还能顺手回收"
#: 另一条笔记：与上面那个 `quote` 对不上——用它钉"没落到任何笔记"那一档。
ORPHAN_WHEN = "被问事实"
ORPHAN_NOTE = "先给结论再补一句"


def _chat_row(**row) -> str:
    return json.dumps(row, ensure_ascii=False)


def _real_chat_log(tmp: str) -> Path:
    """照 `chat.jsonl` 的**真实字段形状**造一条：入站、她的话、表情、群里的反应。

    字段名逐字照抄真实日志（`direction` / `kind` / `message_id` / `sender_name` /
    `sender_card` / `sub_type` / `count`），不是这里编的——这样这一组测的
    才是"真数据进来会被读成什么"。
    """

    path = Path(tmp) / "chat.jsonl"
    rows = [
        # 她开口之前：三条入站消息（**复数条**，上下文那一档就钉在这上面）。
        {"at": 100.0, "direction": "in", "session_id": f"group:{GROUP}", "group_id": GROUP,
         "user_id": "1001", "sender_name": "甲", "sender_card": "甲的群名片",
         "message_id": "m-1", "body": "今天跑山吗"},
        {"at": 101.0, "direction": "in", "session_id": f"group:{GROUP}", "group_id": GROUP,
         "user_id": "1002", "sender_name": "乙", "sender_card": "乙",
         "message_id": "m-2", "body": "坡上那段是不是很费电"},
        {"at": 102.0, "direction": "in", "session_id": f"group:{GROUP}", "group_id": GROUP,
         "user_id": "1003", "sender_name": "丙", "sender_card": "丙",
         "message_id": "m-3", "body": "反正我不敢开上去"},
        # 她的话（被点了表情的那条）。
        {"at": 103.0, "direction": "out", "session_id": f"group:{GROUP}", "group_id": GROUP,
         "kind": "text", "message_id": HER_ID, "part": 1, "total": 1, "outcome": "ok",
         "origin": "message:m-3", "body": HER_BODY},
        # 表情证据：谁贴的、什么表情、第几次。
        # （真实日志的表情行**没有** `sender_name`——名字那一档照实走 `user_id`。）
        {"at": 104.0, "direction": "in", "kind": "reaction", "session_id": f"group:{GROUP}",
         "group_id": GROUP, "message_id": HER_ID, "user_id": "1002", "operator_id": "1002",
         "sender_name": "乙", "emoji_id": "76", "count": 2, "sub_type": "add"},
        {"at": 105.0, "direction": "in", "kind": "reaction", "session_id": f"group:{GROUP}",
         "group_id": GROUP, "message_id": HER_ID, "user_id": "1003", "operator_id": "1003",
         "sender_name": "丙", "emoji_id": "277", "count": 1, "sub_type": "add"},
        # 她说完之后群里怎么接的（两条）。
        {"at": 106.0, "direction": "in", "session_id": f"group:{GROUP}", "group_id": GROUP,
         "user_id": "1001", "sender_name": "甲", "sender_card": "甲的群名片",
         "message_id": "m-4", "body": "有点道理"},
        {"at": 107.0, "direction": "in", "session_id": f"group:{GROUP}", "group_id": GROUP,
         "user_id": "1002", "sender_name": "乙", "sender_card": "乙",
         "message_id": "m-5", "body": "下次试试"},
        # 别人的消息被点表情：**不该进证据**（配对只认她那条）。
        {"at": 108.0, "direction": "in", "session_id": f"group:{GROUP}", "group_id": GROUP,
         "user_id": "1003", "sender_name": "丙", "sender_card": "丙",
         "message_id": "m-6", "body": "我明天也去"},
        {"at": 109.0, "direction": "in", "kind": "reaction", "session_id": f"group:{GROUP}",
         "group_id": GROUP, "message_id": "m-6", "user_id": "1001", "operator_id": "1001",
         "emoji_id": "76", "count": 1, "sub_type": "add"},
    ]
    path.write_text("\n".join(_chat_row(**row) for row in rows) + "\n", encoding="utf-8")
    return path


def _evidence_profile(tmp: str) -> QuoteStore:
    """落一份**真的** `profile.json`：含义表里那条 `quote` 与一条笔记逐字对得上。"""

    store = QuoteStore(Path(tmp) / "quote.json")
    store.apply_run(
        notes=[{"kind": "style", "when": LINKED_WHEN, "note": LINKED_NOTE},
               {"kind": "context", "when": ORPHAN_WHEN, "note": ORPHAN_NOTE}],
        meanings={GROUP: {"76": {"sense": "positive", "note": "这话说得好",
                                 "quote": QUOTE}}},
        seen=[],
    )
    return store


def _engine_with_evidence(tmp: str):
    """装一台带证据读口的最小引擎：`chat_log_path` 是**这次运行真正在写的那一份**。

    `chat.jsonl` 已经在了就**不重写**——测试会先往那份日志里补几条（分页那一组），
    再装引擎；这里要是无条件重写，补的那几条就被冲掉了。
    """

    store = _evidence_profile(tmp)
    engine = DialogueEngine(_NeverCalled(), group_listen=True,
                            enabled_group_ids=frozenset({GROUP}))
    engine.quote_profile = QuoteProfile(store)
    chat_log = Path(tmp) / "chat.jsonl"

    class _Agent:
        chat_log_path = chat_log if chat_log.exists() else _real_chat_log(tmp)

    engine.quote_agent = _Agent()
    return engine, runtime._SeamBinder(engine).learned_seams(), store


def test_the_evidence_reader_returns_her_words_context_reactions_and_notes() -> None:
    """证据读口：**原文 + 消息 id / 时间 / 群 + 复数条上下文 + 表情 + 关联笔记**逐个钉住。"""

    with tempfile.TemporaryDirectory() as tmp:
        _, seam, store = _engine_with_evidence(tmp)
        view = seam.quote_evidence(GROUP)

        assert view["group_id"] == GROUP and view["total"] == 1
        assert view["offset"] == 0 and view["limit"] == 10
        assert view["error"] == "" and view["path"] == str(store.path)
        assert len(view["evidence"]) == 1
        row = view["evidence"][0]

        # ① 她的原文（**不截断**）：这一条正是用户要的"贴原文"。
        assert row["message_id"] == HER_ID
        assert row["group_id"] == GROUP
        assert row["at"] == 103.0
        assert row["她说的"] == HER_BODY
        assert row["sample_id"] == f"{GROUP}:{HER_ID}"
        assert row["当时的话题"] == "" and row["对方想干什么"] == ""  # 没给 reply.jsonl

        # ② 上下文：**复数条**，而且写清是谁说的。这一条钉死"不许退化成一句说明"。
        assert row["她开口之前群里在说"] == [
            "甲（甲的群名片）：今天跑山吗",
            "乙：坡上那段是不是很费电",
            "丙：反正我不敢开上去",
        ]
        assert len(row["她开口之前群里在说"]) >= 2, "上下文必须是复数条"
        # 上下文按 id **回日志补**（不是拿样本里那份截断的），所以 `丙` 那条也在。
        assert row["她说完之后群里接的"] == ["甲（甲的群名片）：有点道理", "乙：下次试试",
                                             "丙：我明天也去"]
        assert len(row["她说完之后群里接的"]) >= 2, "她说完之后也得是复数条"

        # ③ 表情证据：谁贴的（user_id）、什么表情、第几次。
        assert row["被点了"] == [
            {"emoji_id": "76", "by": "1002", "count": 2, "sub_type": "add"},
            {"emoji_id": "277", "by": "1003", "count": 1, "sub_type": "add"},
        ]

        # ④ 学习 agent 据此得出的结论：正/负/不明 + 它落到了哪条笔记上。
        assert row["meanings"] == [{
            "emoji_id": "76", "sense": "positive", "sense_label": "赞成（好句子）",
            "note": "这话说得好", "quote": QUOTE, "manual": False,
        }]
        # `quote` 与 `LINKED_NOTE` 连着 5 字重合 → 落在**那一条**上（id 是内容指纹）。
        # 277 没学过：**也在列表里，但空着**（如实标"没落到任何笔记"，不硬凑）。
        assert row["notes"] == [
            {"emoji_id": "76", "id": note_id(LINKED_WHEN, LINKED_NOTE),
             "note": LINKED_NOTE, "manual": False},
            {"emoji_id": "277", "id": "", "note": "", "manual": False},
        ]
        assert row["notes"][0]["id"] == seam.quote_view(GROUP)["notes"][0]["id"]


def test_the_evidence_matches_the_profile_file_on_disk() -> None:
    """**对着落盘文件**造一条真数据：证据里的关联笔记与文件里那条逐字对得上。"""

    with tempfile.TemporaryDirectory() as tmp:
        _, seam, store = _engine_with_evidence(tmp)
        saved = json.loads(Path(store.path).read_text(encoding="utf-8"))
        linked = [item for item in saved["notes"] if item["note"] == LINKED_NOTE]
        assert len(linked) == 1, "文件里应当只有一条是那条笔记"
        row = seam.quote_evidence(GROUP)["evidence"][0]
        assert row["notes"][0]["note"] == linked[0]["note"]
        assert row["notes"][0]["id"] == note_id(linked[0]["when"], linked[0]["note"])
        # 含义表那一侧也是文件里的那一份（含 `quote`）。
        assert saved["meanings"][GROUP]["76"]["quote"] == QUOTE
        assert row["meanings"][0]["sense"] == saved["meanings"][GROUP]["76"]["sense"]
        # 这条关联**不是靠猜**：`quote` 确实是笔记原文里的一段（5 字以上）。
        assert QUOTE in linked[0]["note"]


def test_a_quote_that_lands_on_no_note_says_so() -> None:
    """对不上就**如实标"没落到任何笔记"**（空 id / 空 note），不硬凑一条。"""

    with tempfile.TemporaryDirectory() as tmp:
        _, seam, store = _engine_with_evidence(tmp)
        # 换一条与两份笔记都不沾边的 `quote`。
        store.apply_run(notes=list(store.notes),
                        meanings={GROUP: {"76": {"sense": "negative", "note": "在损她",
                                                 "quote": "这句话跟两条笔记都不像"}}},
                        seen=[])
        row = seam.quote_evidence(GROUP)["evidence"][0]
        assert row["meanings"][0]["sense"] == "negative"
        assert row["notes"][0] == {"emoji_id": "76", "id": "", "note": "", "manual": False}


def test_the_evidence_is_paged_and_empty_cases_do_not_raise() -> None:
    """分页（默认最近 N 条 + 可指定 offset）+ 取不到就回空结构，**一律不抛**。"""

    with tempfile.TemporaryDirectory() as tmp:
        path = _real_chat_log(tmp)
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        # 再补两条她的话（各带一个表情），用来翻页。
        for index, body in enumerate(("第二句金句。", "第三句金句。")):
            mid = f"her-{index}"
            rows.append({"at": 200.0 + index, "direction": "out", "kind": "text",
                         "session_id": f"group:{GROUP}", "group_id": GROUP, "message_id": mid,
                         "body": body})
            rows.append({"at": 300.0 + index, "direction": "in", "kind": "reaction",
                         "session_id": f"group:{GROUP}", "group_id": GROUP, "message_id": mid,
                         "user_id": "1001", "emoji_id": "76", "count": 1, "sub_type": "add"})
        path.write_text("\n".join(_chat_row(**row) for row in rows) + "\n", encoding="utf-8")

        engine, seam, _ = _engine_with_evidence(tmp)
        engine.quote_agent.chat_log_path = path
        first = seam.quote_evidence(GROUP, 1, 0)
        assert first["total"] == 3 and first["limit"] == 1
        assert len(first["evidence"]) == 1
        second = seam.quote_evidence(GROUP, 1, 1)
        assert len(second["evidence"]) == 1
        assert second["evidence"][0]["message_id"] != first["evidence"][0]["message_id"]
        # 上限被夹住（`MAX_EVIDENCE_LIMIT`），不会"一次全吐"。
        assert seam.quote_evidence(GROUP, 10_000)["limit"] == 50
        # 群号为空：空结构（与 `quote_view("")` 同一口径）。
        assert seam.quote_evidence("", 1) == {}

        # 取不到：群号为空 / 群号不认识 / offset 越过末尾 / 日志根本不在。
        assert seam.quote_evidence("") == {}
        assert seam.quote_evidence("999")["total"] == 0
        assert seam.quote_evidence("999")["evidence"] == []
        assert seam.quote_evidence(GROUP, 5, 999)["evidence"] == []
        engine.quote_agent.chat_log_path = Path(tmp) / "没有这份日志.jsonl"
        missing = seam.quote_evidence(GROUP)
        assert missing["evidence"] == [] and missing["error"] == "missing"
        # 没有 quote_profile（引擎上那份数据都没装上）：空结构、不抛。
        engine.quote_profile = None
        assert seam.quote_evidence(GROUP) == {}


def test_the_evidence_reader_is_read_only_and_gives_no_memory_or_dialogue() -> None:
    """边界：证据读口**只读**——调它一次，盘上那两份文件一个字节都没变。"""

    with tempfile.TemporaryDirectory() as tmp:
        _, seam, store = _engine_with_evidence(tmp)
        profile = Path(store.path)
        before = profile.read_bytes()
        chat = seam.quote_evidence(GROUP)
        assert chat["evidence"], "这一条该有证据"
        assert profile.read_bytes() == before, "读证据不许写盘"
        # 它也不返回任何"能改"的东西：没有写口、没有记忆、没有对话入口。
        assert set(chat) == {"group_id", "total", "offset", "limit", "evidence",
                             "error", "path"}
        # 这一条**有结论**（emoji 76 学过），所以"为什么没有结论"不该出现。
        assert set(chat["evidence"][0]) == {
            "message_id", "group_id", "at", "她说的", "她开口之前群里在说",
            "她说完之后群里接的", "被点了", "meanings", "notes", "sample_id",
            "当时的话题", "对方想干什么",
        }
        # 拿不到结论时才**如实说为什么**：把含义表腾空，那一栏就出现。
        store.clear_override(GROUP, "76")
        store.meanings = {}
        store.save()
        bare = seam.quote_evidence(GROUP)["evidence"][0]
        assert bare["meanings"] == []
        assert bare["为什么没有结论"] == "这个群还没有学过（表情含义表是空的）"
        assert "为什么没有结论" in bare
