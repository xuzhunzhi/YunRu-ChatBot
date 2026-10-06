"""黑话词条的离线覆盖（2026-10-06，用户口径：*"金句我看应该划到**知识库**里"*、*"还有**黑话**"*）。

要钉住的六类：

1. **被动捕获**：群里有人解释一个词 → 记成词条（词 / 释义 / **谁解释的** / 什么时候 / 哪个群）；
2. **普通聊天一个字都不记**（含"这就是对的"这类看着像解释的句子、问句、命令）；
3. **重复解释按规则合并、改口记修订**（规则写在 `slang_learning.apply_explanation`）；
4. **她不许因此发问**：走真的 `runtime._message_loop`（收那一侧），断言这一轮
   一个字都没发出去、回复模型也没被叫——捕获是**纯旁路**；
5. **机制词零命中**（词条文件里一个都不许有）与**落盘往返一致**；
6. **写盘失败不影响对话**（把那份 JSON 的位置用目录占住）。

**实测数据状况**：这台机器的真实 `run/data/logs/chat.jsonl` 里没有"解释黑话"的样本
（这是新功能，日志窗口里没有过），所以本文件的语料是**照用户举的三种句式合成**的
（"XX 就是 OO"／"XX 是 OO 的意思"／"在我们这儿 X 叫 Y"），另加真实群里会出现的
普通闲聊当**负样本**。漏与误报的实测结论写在 `slang_learning` 的模块说明里。
"""
import asyncio
import inspect
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from qq_roleplay_bot.prompt_guard import scan_persona_text
from qq_roleplay_bot.quote_learning import QuoteProfile, QuoteStore, note_id
from qq_roleplay_bot.slang_learning import (
    MAX_DEFINITION_CHARS,
    SlangStore,
    SlangWatcher,
    build_watcher,
    detect_explanations,
    entry_id,
    learning_enabled,
)
from qq_roleplay_bot.stage3_main import (
    DialogueEngine,
    SuperAction,
    _parse_quote_override,
    _parse_slang_edit,
    parse_super_command,
)
from qq_roleplay_bot.transport import IncomingMessage, MessageTarget

GROUP = "1084401296"
OTHER_GROUP = "908696203"


def msg(text: str, *, index: int = 1, group: str = GROUP, user_id: str = "3955067055",
        name: str = "witelb", mentioned: bool = False) -> IncomingMessage:
    return IncomingMessage(
        message_id=f"m{index}", session_id=f"group:{group}", user_id=user_id, text=text,
        target=MessageTarget(group_id=group), sender_name=name, is_bot_mentioned=mentioned,
    )


def private(text: str, *, index: int = 1, user_id: str = "3955067055") -> IncomingMessage:
    return IncomingMessage(
        message_id=f"p{index}", session_id=f"private:{user_id}", user_id=user_id, text=text,
        target=MessageTarget(user_id=user_id), sender_name="witelb",
    )


class ScriptedReply:
    """回复 agent 的替身：**记下每一次请求**（"她有没有被叫"就靠它判）。"""

    def __init__(self, *texts) -> None:
        self.texts = list(texts) or ["我接一句。"]
        self.requests: list = []

    async def complete(self, request):
        self.requests.append(request)
        index = min(len(self.requests) - 1, len(self.texts) - 1)
        return (f"<decision>REPLY</decision><dialogue>KEEP</dialogue>"
                f"<reply>{self.texts[index]}</reply>")


class ScriptedJudge:
    """判定 agent 的替身：默认**不出声**（这一轮本来就不该她说话）。"""

    def __init__(self, *outputs) -> None:
        self.outputs = list(outputs) or ["<route>NO_REPLY</route>"]
        self.requests: list = []

    async def complete(self, request):
        self.requests.append(request)
        index = min(len(self.requests) - 1, len(self.outputs) - 1)
        return self.outputs[index]


class QueueTransport:
    """最小假传输层：喂几条消息、然后关掉；**记下有没有东西发出去**。"""

    def __init__(self, items) -> None:
        self.items = list(items)
        self.sent: list[str] = []

    async def receive(self):
        return self.items.pop(0) if self.items else None

    async def send(self, target, text, *, reply_to=""):
        self.sent.append(text)

    async def start(self):
        return None

    async def close(self):
        return None


def _drain(transport, engine) -> None:
    """走真的收消息循环（`runtime._message_loop`），直到假传输层说"没有下一条"。"""

    from qq_roleplay_bot.runtime import _message_loop

    asyncio.run(_message_loop(transport, engine, tick_seconds=5.0))


def _engine(store, watcher, *, judge=None, reply=None, group_listen=True):
    engine = DialogueEngine(
        reply or ScriptedReply(), judge_client=judge or ScriptedJudge(),
        group_listen=group_listen, enabled_group_ids=frozenset({GROUP}),
        typing_sim=False,
    )
    engine.slang_library = store
    engine.slang_watcher = watcher
    return engine


# --- 1. 判解释句（确定性、纯函数）-------------------------------------------


def test_the_three_shapes_the_user_named_are_recognized() -> None:
    """用户举的三种句式都要认出来（词与释义都取对）。"""

    assert [(d["word"], d["definition"]) for d in
            detect_explanations("在我们这儿，电赛就是电子设计竞赛的意思")] == \
        [("电赛", "电子设计竞赛")]
    assert [(d["word"], d["definition"]) for d in
            detect_explanations("综测就是综合测评")] == [("综测", "综合测评")]
    assert [(d["word"], d["definition"]) for d in
            detect_explanations("我们这儿电赛叫电子设计竞赛")] == [("电赛", "电子设计竞赛")]
    # 一句话里两个词各记一条。
    both = detect_explanations("电赛就是电子设计竞赛，综测就是综合测评")
    assert sorted(item["word"] for item in both) == ["电赛", "综测"]
    # 更具体的形状先命中：尾巴"的意思"不会被并进释义。
    assert detect_explanations("Ciallo 是「你好」的意思") == [{
        "word": "Ciallo", "definition": "你好", "shape": "A是B的意思",
        "sentence": "Ciallo 是「你好」的意思",
    }]


def test_ordinary_chat_is_never_mistaken_for_an_explanation() -> None:
    """**误报是这里最贵的错**：这些句子一条都不许被记。"""

    for text in (
        "这个字幕好像只能识别，不能翻译",          # 真实日志里的普通聊天
        "今天就要回去写模式识别与机器学习了！",
        "他说的就是对的",                          # "就是"前面是整段子句，不是词
        "这个就是那个",
        "这不就是摆烂吗",                          # 问句
        "这是真的吗？",
        "你懂我意思吧",
        "/super slang",                           # 命令
        "#help",
        "",
    ):
        assert detect_explanations(text) == [], text


# --- 2. 被动捕获：词条长什么样 ----------------------------------------------


def test_a_heard_explanation_becomes_an_entry_with_who_when_and_where() -> None:
    """验收：词 / 释义 / **谁解释的** / 什么时候 / 哪个群，五样都在。"""

    with tempfile.TemporaryDirectory() as tmp:
        store = SlangStore(Path(tmp) / "entries.json")
        watcher = SlangWatcher(store, groups=lambda: {GROUP}, clock=lambda: 1791263600.0,
                               log_path=Path(tmp) / "slang.jsonl")
        assert watcher.observe(
            msg("在我们这儿，电赛就是电子设计竞赛的意思", user_id="3955067055", name="witelb")) == 1
        assert watcher.observe(msg("综测就是综合测评", index=2, user_id="2", name="云边")) == 1
        entries = {item["word"]: item for item in store.entries_for(GROUP)}
        assert set(entries) == {"电赛", "综测"}
        one = entries["电赛"]
        assert one["definition"] == "电子设计竞赛"
        assert one["group_id"] == GROUP
        assert one["id"] == entry_id(GROUP, "电赛")
        assert one["first_seen"] == one["last_seen"] == 1791263600.0
        assert one["times"] == 1 and one["status"] == "ok"
        who = one["explainers"][0]
        assert who["user_id"] == "3955067055" and who["name"] == "witelb"
        assert who["at"] == 1791263600.0
        assert who["quote"] == "电赛就是电子设计竞赛的意思"      # **原句证据**
        # 落盘了（下一轮/面板/命令都从文件读）。
        saved = json.loads((Path(tmp) / "entries.json").read_text(encoding="utf-8"))
        assert [item["word"] for item in saved["entries"]] == ["电赛", "综测"]


def test_she_never_asks_and_the_conversation_is_untouched() -> None:
    """**硬要求**：捕获走真的主循环，但它自己**一个字都不发**。

    这一轮里有一条是**冲她来的**（被 @），对话路径会正常回一句；另外两条只是群里的
    解释与闲聊（她本来就不出声）。判据就是**发出去的东西恰好等于对话自己的那一句**——
    捕获要是顺手发了一句"这是什么意思"，这里立刻多出一条。
    """

    with tempfile.TemporaryDirectory() as tmp:
        store = SlangStore(Path(tmp) / "entries.json")
        watcher = SlangWatcher(store, groups=lambda: {GROUP},
                               log_path=Path(tmp) / "slang.jsonl")
        reply, judge = ScriptedReply("我接一句。"), ScriptedJudge("<route>REPLY</route>")
        engine = _engine(store, watcher, judge=judge, reply=reply)
        transport = QueueTransport([
            msg("在我们这儿，电赛就是电子设计竞赛的意思", index=1),
            msg("这个字幕好像只能识别，不能翻译", index=2),
            msg("小灯就是新人的意思", index=3, user_id="2", name="云边", mentioned=True),
        ])
        _drain(transport, engine)
        assert transport.sent == ["我接一句。"], \
            "发出去的东西必须**只有对话自己那一句**（捕获不许发问、不许插话）"
        assert len(reply.requests) == 1, "只有冲她来的那一条会调回复模型"
        entries = {item["word"] for item in store.entries_for(GROUP)}
        assert entries == {"电赛", "小灯"}, "两句解释都要记下来，普通聊天不许记"


def test_the_watcher_has_nothing_to_speak_with() -> None:
    """**结构判据**：这个对象上没有任何"能发东西"的东西，也没有 async 方法。

    突变验证用的就是这条：把捕获改成"她也主动发问"必须**先给它一样能发消息的东西**
    （transport / notify / client），那时这条立刻红。
    """

    with tempfile.TemporaryDirectory() as tmp:
        watcher = SlangWatcher(SlangStore(Path(tmp) / "entries.json"), groups=lambda: {GROUP})
        fields = set(vars(watcher))
        for banned in ("notify", "transport", "client", "send", "call_action", "engine",
                       "sender", "loop", "reply"):
            assert banned not in fields, f"观察者上有 {banned}＝它就能自己发东西了"
        assert not any(
            inspect.iscoroutinefunction(getattr(watcher, name))
            for name in dir(watcher) if not name.startswith("__")
        ), "捕获不该是异步的：它是收那一侧的旁路，不是一条对话通道"


def test_only_groups_she_is_listening_to_are_captured() -> None:
    """只记**她正在听**的群；私聊不是"群里听到"，关掉的群也不记。"""

    with tempfile.TemporaryDirectory() as tmp:
        store = SlangStore(Path(tmp) / "entries.json")
        watcher = SlangWatcher(store, groups=lambda: {GROUP}, log_path=Path(tmp) / "slang.jsonl")
        assert watcher.observe(msg("电赛就是电子设计竞赛", group=OTHER_GROUP)) == 0
        assert watcher.observe(private("电赛就是电子设计竞赛")) == 0
        assert store.entries_for() == []
        # 同一个群、但同一条消息交两次手（焦点排队会再交一次）→ 只记一遍。
        first = msg("电赛就是电子设计竞赛", index=7)
        assert watcher.observe(first) == 1
        assert watcher.observe(first) == 0
        assert store.find(GROUP, "电赛")["times"] == 1


# --- 3. 合并与改口 ----------------------------------------------------------


def test_a_repeat_merges_and_a_changed_definition_records_a_revision() -> None:
    """同一词重复解释 → 合并（记次数与说话人）；改口 → 记一次修订，标错被推翻。"""

    with tempfile.TemporaryDirectory() as tmp:
        store = SlangStore(Path(tmp) / "entries.json")
        assert store.apply_explanation(GROUP, "电赛", "电子设计竞赛",
                                       by_user_id="1", by_name="A", at=100.0) == "new"
        assert store.apply_explanation(GROUP, "电赛", "电子设计竞赛",
                                       by_user_id="2", by_name="B", at=200.0) == "merged"
        entry = store.find(GROUP, "电赛")
        assert entry["times"] == 2 and entry["last_seen"] == 200.0
        assert [row["user_id"] for row in entry["explainers"]] == ["1", "2"]
        assert entry["revisions"] == [], "同一个说法不算改口"

        # 同一个人把同一个说法再说一遍：只长计数，不重复记说话人。
        store.apply_explanation(GROUP, "电赛", "电子设计竞赛",
                                by_user_id="2", by_name="B", at=300.0)
        entry = store.find(GROUP, "电赛")
        assert entry["times"] == 3 and len(entry["explainers"]) == 2
        assert entry["explainers"][-1]["count"] == 2

        # 改口：旧的那条进修订，新释义生效。
        assert store.apply_explanation(GROUP, "电赛", "电子设计大赛",
                                       by_user_id="2", by_name="B", at=400.0,
                                       quote="不是电子设计大赛，是电子设计竞赛") == "revised"
        entry = store.find(GROUP, "电赛")
        assert entry["definition"] == "电子设计大赛"
        assert entry["revisions"] == [{
            "at": 400.0, "by": "2", "from": "电子设计竞赛", "to": "电子设计大赛",
            "quote": "不是电子设计大赛，是电子设计竞赛",
        }]

        # 标错之后，**重复同一个说法不能把它洗白**；新的释义可以。
        assert store.mark_wrong(GROUP, "电赛", True) is True
        store.apply_explanation(GROUP, "电赛", "电子设计大赛", by_user_id="3", at=500.0)
        assert store.find(GROUP, "电赛")["status"] == "wrong"
        store.apply_explanation(GROUP, "电赛", "电子设计竞赛", by_user_id="3", at=600.0)
        entry = store.find(GROUP, "电赛")
        assert entry["status"] == "ok" and entry["definition"] == "电子设计竞赛"
        assert [row["to"] for row in entry["revisions"]] == ["电子设计大赛", "电子设计竞赛"]


def test_the_same_word_in_two_groups_is_two_entries() -> None:
    """黑话是**按群**的：同一个词在两个群各是一条，互不覆盖。"""

    with tempfile.TemporaryDirectory() as tmp:
        store = SlangStore(Path(tmp) / "entries.json")
        store.apply_explanation(GROUP, "小灯", "新人", by_user_id="1", at=100.0)
        store.apply_explanation(OTHER_GROUP, "小灯", "灯泡", by_user_id="2", at=200.0)
        assert store.find(GROUP, "小灯")["definition"] == "新人"
        assert store.find(OTHER_GROUP, "小灯")["definition"] == "灯泡"
        assert [item["group_id"] for item in store.entries_for(GROUP)] == [GROUP]
        assert len(store.entries_for()) == 2


# --- 4. 护栏：机制词、落盘往返、写盘失败 ------------------------------------


def test_mechanism_words_never_reach_the_entry_file() -> None:
    """**硬要求**：释义/词里出现机制词 → 整条不记；原句里有 → 只丢原句。

    最后一条断言是这份数据的判据：**整份文件**过 `prompt_guard` 零命中——
    它以后可能被注入，所以文件里不许留机制词（释义与"谁说的"照旧留下）。
    """

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "entries.json"
        store = SlangStore(path, clock=lambda: 1791263600.0)
        # 释义里有机制词 → 整条丢掉。
        assert store.apply_explanation(GROUP, "电赛", "上下文里的东西", by_user_id="1") == ""
        # 词本身是机制词 → 整条丢掉。
        assert store.apply_explanation(GROUP, "记忆库", "存东西的地方", by_user_id="1") == ""
        assert store.entries_for() == [] and store.dropped == 2
        # 原句里有机制词、但释义干净 → 词条留下，**原句不存**（审计链只少一环）。
        store.apply_explanation(GROUP, "电赛", "电子设计竞赛", by_user_id="1",
                                quote="别管上下文了，电赛就是电子设计竞赛")
        entry = store.find(GROUP, "电赛")
        assert entry["definition"] == "电子设计竞赛"
        assert entry["explainers"][0]["quote"] == ""
        assert entry["explainers"][0]["evidence_dropped"] == 1
        assert store.evidence_dropped == 1
        # 真的落盘之后，整份文件零命中。
        store.apply_explanation(GROUP, "综测", "综合测评", by_user_id="2")
        raw = path.read_text(encoding="utf-8")
        for word in ("检查", "触发", "调用", "协议", "提示词", "上下文", "记忆库", "Stage"):
            assert word not in raw, f"{word} 不该出现在词条文件里"
        assert scan_persona_text(raw) == []


def test_entries_round_trip_through_the_file() -> None:
    """落盘往返一致：重新读一遍文件，拿到的与内存里那一份逐字相同。"""

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "entries.json"
        store = SlangStore(path, clock=lambda: 1791263600.0)
        store.apply_explanation(GROUP, "电赛", "电子设计竞赛", by_user_id="1", by_name="A",
                                quote="电赛就是电子设计竞赛")
        store.apply_explanation(GROUP, "电赛", "电子设计大赛", by_user_id="2", at=1791263700.0)
        store.mark_wrong(OTHER_GROUP, "x", True)     # 不存在的词条：什么都不该发生
        fresh = SlangStore(path)
        assert fresh.entries_for() == store.entries_for()
        assert fresh.groups() == [GROUP]
        assert fresh.snapshot()["entries"] == 1


def test_a_write_failure_never_touches_the_conversation() -> None:
    """**硬要求**：那份 JSON 写不进去（这里用目录占住文件名）→ 收、回、记，一切照旧。"""

    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "entries.json"
        target.mkdir()                                # 占位：读 OSError、写也失败
        store = SlangStore(target)
        watcher = SlangWatcher(store, groups=lambda: {GROUP},
                               log_path=Path(tmp) / "slang.jsonl")
        reply, judge = ScriptedReply("在。"), ScriptedJudge("<route>REPLY</route>")
        engine = _engine(store, watcher, judge=judge, reply=reply)
        transport = QueueTransport([
            msg("电赛就是电子设计竞赛", index=1),
            msg("在吗", index=2, mentioned=True),
        ])
        _drain(transport, engine)
        assert transport.sent == ["在。"], "写盘坏了也照常说话"
        assert reply.requests, "该回的那一条还是要回"
        assert store.last_error != "", "写不进去要**如实记一笔**，不能假装成功"
        assert watcher.failures == 0, "观察者自己不该炸（写盘失败是 store 的事）"


# --- 5. `/super slang`：列 + 改 + 删 + 标错 + 证据 ----------------------------


def _super(text: str, *, group: str = GROUP) -> IncomingMessage:
    return IncomingMessage(message_id=f"s:{text}", session_id=f"group:{group}", user_id="1",
                           text=text, target=MessageTarget(group_id=group), sender_name="超管")


def test_the_command_shape_is_parsed_and_never_eats_other_commands() -> None:
    for text in ("/super slang", "/super 黑话", "/super 词条"):
        assert parse_super_command(text) is SuperAction.SLANG, text
    assert parse_super_command("/super restart slang") is None
    assert _parse_slang_edit("/super slang") == ("list", "", "")
    assert _parse_slang_edit("/super slang all") == ("all", "", "")
    assert _parse_slang_edit("/super slang why 电赛") == ("why", "电赛", "")
    assert _parse_slang_edit("/super slang set 电赛 电子设计竞赛") == ("set", "电赛", "电子设计竞赛")
    assert _parse_slang_edit("/super slang 电赛 电子设计竞赛") == ("set", "电赛", "电子设计竞赛")
    assert _parse_slang_edit("/super slang 电赛") == ("why", "电赛", "")
    assert _parse_slang_edit("/super slang del 电赛") == ("del", "电赛", "")
    assert _parse_slang_edit("/super slang wrong 电赛") == ("wrong", "电赛", "")
    assert _parse_slang_edit("/super slang ok 电赛") == ("ok", "电赛", "")


def test_the_command_lists_edits_deletes_and_shows_evidence() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = SlangStore(Path(tmp) / "entries.json", clock=lambda: 1791263600.0)
        engine = DialogueEngine(None, super_admin_user_ids=frozenset({"1"}),
                                slang_library=store)
        store.apply_explanation(GROUP, "电赛", "电子设计竞赛", by_user_id="9", by_name="witelb",
                                quote="电赛就是电子设计竞赛的意思", at=1791263600.0)
        store.apply_explanation(OTHER_GROUP, "小灯", "新人", by_user_id="8", by_name="云边")

        listed = engine._slang_reply(_super("/super slang"))
        assert "电赛 = 电子设计竞赛" in listed
        assert "witelb" in listed, "列表里要看得见**谁解释的**"
        assert "小灯" not in listed, "不带 all 时只列本群"
        assert "综测" not in listed

        everything = engine._slang_reply(_super("/super slang all"))
        assert "电赛" in everything and "小灯" in everything

        why = engine._slang_reply(_super("/super slang why 电赛"))
        assert "电赛就是电子设计竞赛的意思" in why, "证据（原句）要能看到"

        changed = engine._slang_reply(_super("/super slang set 电赛 电子设计大赛"))
        assert "已改" in changed
        entry = store.find(GROUP, "电赛")
        assert entry["definition"] == "电子设计大赛"
        assert entry["revisions"] and entry["revisions"][0]["from"] == "电子设计竞赛"

        wrong = engine._slang_reply(_super("/super slang wrong 电赛"))
        assert "已标错" in wrong and store.find(GROUP, "电赛")["status"] == "wrong"
        assert "已取消标错" in engine._slang_reply(_super("/super slang ok 电赛"))
        assert store.find(GROUP, "电赛")["status"] == "ok"

        assert "已删掉" in engine._slang_reply(_super("/super slang del 电赛"))
        assert store.find(GROUP, "电赛") is None
        # 删不存在的、改不合规的：如实说，不静默当成功。
        assert "没有" in engine._slang_reply(_super("/super slang del 电赛"))
        rejected = engine._slang_reply(_super("/super slang set 电赛 上下文里的东西"))
        assert "没法记" in rejected
        # 空库也说得出来。
        empty = DialogueEngine(None, super_admin_user_ids=frozenset({"1"}),
                               slang_library=SlangStore(Path(tmp) / "other.json"))
        assert "还没有词条" in empty._slang_reply(_super("/super slang"))
        # **没接上**时如实降级（不是抛异常）。
        bare = DialogueEngine(None, super_admin_user_ids=frozenset({"1"}))
        assert "没有接入黑话" in bare._slang_reply(_super("/super slang"))


def test_the_command_is_super_admin_only() -> None:
    """档位由核心判：非超管发 `/super slang` 一律静默（连"存在"都不暴露）。"""

    with tempfile.TemporaryDirectory() as tmp:
        store = SlangStore(Path(tmp) / "entries.json")
        engine = DialogueEngine(None, super_admin_user_ids=frozenset({"1"}),
                                slang_library=store, group_listen=True,
                                enabled_group_ids=frozenset({GROUP}), typing_sim=False)
        store.apply_explanation(GROUP, "电赛", "电子设计竞赛", by_user_id="9")
        assert asyncio.run(engine.handle(msg("/super slang", user_id="999"))) is None
        result = asyncio.run(engine.handle(_super("/super slang")))
        assert result is not None and "电赛 = 电子设计竞赛" in result.text


# --- 6. 开关与"金句那条笔记的停用"（同一个接缝要用到的那件事）-----------------


def test_the_switch_turns_the_capture_off_without_losing_what_was_learned() -> None:
    """`QQBOT_SLANG_LEARN=0` → **不再记新的**；但那份库照样在（面板/命令还看得见）。"""

    with tempfile.TemporaryDirectory() as tmp:
        with patch.dict(os.environ, {"QQBOT_SLANG_LEARN": "0"}):
            assert learning_enabled() is False
            with patch("qq_roleplay_bot.slang_learning.profile_path",
                       return_value=Path(tmp) / "entries.json"):
                store, watcher = build_watcher()
        assert watcher is None, "关掉＝不收新的"
        assert isinstance(store, SlangStore), "但库要给出来（关掉不是把数据删了）"
        with patch.dict(os.environ, {"QQBOT_SLANG_LEARN": "1"}):
            assert learning_enabled() is True
            with patch("qq_roleplay_bot.slang_learning.profile_path",
                       return_value=Path(tmp) / "entries.json"):
                _, on = build_watcher()
        assert isinstance(on, SlangWatcher)


def test_a_quote_note_can_be_disabled_and_restored_from_the_command() -> None:
    """`/super quote note <id> off|on`：**停用一条笔记**（面板接缝用的是同一个 store 口）。"""

    with tempfile.TemporaryDirectory() as tmp:
        store = QuoteStore(Path(tmp) / "profile.json")
        store.apply_run(notes=[{"kind": "style", "when": "被问事实", "note": "先给结论"}],
                        meanings={}, seen=[])
        engine = DialogueEngine(None, super_admin_user_ids=frozenset({"1"}),
                                quote_profile=QuoteProfile(store))
        key = note_id("被问事实", "先给结论")
        assert key in engine._quote_reply(_super("/super quote"))

        off = engine._quote_reply(_super(f"/super quote note {key} off"))
        assert "已停用" in off
        assert store.note_disabled(key) is True
        assert QuoteProfile(store).notes_for(GROUP, "被问事实", "被问事实") == [], \
            "停用之后不该再给这条材料"

        on = engine._quote_reply(_super(f"/super quote note {key} on"))
        assert "已恢复" in on
        assert QuoteProfile(store).notes_for(GROUP, "被问事实", "被问事实") != []

        unknown = engine._quote_reply(_super("/super quote note deadbeef off"))
        assert "没有找到" in unknown
        # `note` 这个形状**不许**被当成"改一个表情的方向"（否则会写进覆盖表一个假表情）。
        assert _parse_quote_override("/super quote note off") is None
        assert _parse_quote_override("/super quote 277 不赞成 在损她") == ("277", "不赞成", "在损她")


def test_an_operator_edit_is_recorded_as_such_not_as_what_someone_said() -> None:
    """`explainers` 里分得清"听来的"与"操作者改的"：列表不许把操作者写成"他说"。"""

    with tempfile.TemporaryDirectory() as tmp:
        store = SlangStore(Path(tmp) / "entries.json")
        engine = DialogueEngine(None, super_admin_user_ids=frozenset({"1"}),
                                slang_library=store)
        store.apply_explanation(GROUP, "电赛", "电子设计竞赛", by_user_id="9", by_name="witelb",
                                at=1.0)
        assert store.find(GROUP, "电赛")["explainers"][0]["source"] == "heard"
        engine._slang_reply(_super("/super slang set 电赛 电子设计大赛"))
        assert store.find(GROUP, "电赛")["explainers"][-1]["source"] == "edited"
        listed = engine._slang_reply(_super("/super slang"))
        assert "操作者改于" in listed and "witelb 说于" not in listed
        why = engine._slang_reply(_super("/super slang why 电赛"))
        assert "操作者改的" in why and "witelb 说的" in why


def test_the_definition_budget_is_enforced() -> None:
    """释义太长不成其为"释义"（上限在模块常量里，不是随手一个数）。"""

    with tempfile.TemporaryDirectory() as tmp:
        store = SlangStore(Path(tmp) / "entries.json")
        assert store.apply_explanation(GROUP, "电赛", "很" * (MAX_DEFINITION_CHARS + 1)) == ""
        assert store.entries_for() == []
