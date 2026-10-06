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
from qq_roleplay_bot.quote_learning import QuoteProfile, QuoteStore
from qq_roleplay_bot.slang_learning import SlangStore
from qq_roleplay_bot.stage3_main import DialogueEngine
from qq_roleplay_bot.transport import IncomingMessage, MessageTarget

GROUP = "1084401296"
EMOJI = "277"

#: 这份契约**逐字**钉住：字段名就是接口，界面只能调这些。
EXPECTED_FIELDS = (
    "quote_view", "quote_correct", "quote_note_enabled",
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
