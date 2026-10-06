"""金句学习 agent 的离线覆盖（2026-10-06 第三批）。

用户口径（原话，判据）：
*"富哥之家群刚刚提出爆的金句都是优质 rl 训练轨迹…可以通过给消息点的表情来定位"*、
*"只挑三条这个我不理解，我认为金句更重要的是**说话风格和上下文语境**，需要**调用一个
agent 来专门学习**"*、*"注意贴表情不是所有都是金句，比如贴**祝（猪的谐音）就是不赞同或者
bot 回复不恰当**"*。

要钉住的五类（与验收逐条对应）：

1. **正负分流**：她的话 + 一个**负向**表情（拿真实语境里的「祝」当用例）→ 归**反面样本**；
   正向表情 → 正样本。**方向由学习 agent 从语境推**（这一份用可脚本化的假 client 验，
   真模型那一趟见 `data/quote/` 的产物）；
2. **表情含义表**：能从语境推出方向、**可被操作者覆盖**（覆盖后按覆盖的走）；
3. **笔记**：机制词零命中、空样本 → 不产出空笔记（**连模型都不调**）、
   笔记里不含可被逐字照抄的长句；
4. **注入**：按语境挑 ≤N 条、**关掉开关 → 请求与改动前逐字相同**、样本为空 → 不注入；
5. **定期任务的失败 → 不影响对话与收发**。

**实测数据状况**（写在这里免得被误读成"没测"）：`run/data/logs/chat.jsonl` 那 51 行真实
表情回应里，**没有一行**点在她自己的消息上（详见 `quote_samples.py` 模块头部），
所以本文件的样本是**照真实字段形状合成**的（emoji_id 用的是真实出现过的那几个：76 / 387 /
277 / 128516 / 49 / 476），而不是把真实日志当样本——真实日志那一次跑出来的是**空样本**。
"""
import asyncio
import json
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from qq_roleplay_bot.prompt_guard import scan_persona_text
from qq_roleplay_bot.quote_learning import (
    IMPERATIVE_WORDS,
    MAX_NOTE_CHARS,
    SENSE_NEGATIVE,
    SENSE_POSITIVE,
    SENSE_UNCLEAR,
    QuoteLearningAgent,
    QuoteProfile,
    QuoteStore,
    build_quote_agent,
    inject_block,
    learning_enabled,
    parse_learning_output,
)
from qq_roleplay_bot.quote_samples import build_samples, collect_samples, new_samples
from qq_roleplay_bot.stage3_main import DialogueEngine
from qq_roleplay_bot.stage3_runtime import (
    ContextState,
    ConversationMode,
    build_dialogue_messages,
)
from qq_roleplay_bot.transport import IncomingMessage, MessageTarget

GROUP = "1084401296"
OTHER_GROUP = "908696203"
TARGET = MessageTarget(group_id=GROUP)
HER_MESSAGE_ID = "-1345481846"

#: 「祝 / 猪」那一类。真实日志里 277 这个 id 出现过（被同一个人连点在她附近的消息上），
#: 但**它究竟是不是那个谐音表情没有实测证据**——这里只把它当"一个负向表情"的**用例**，
#: 方向由（脚本化的）学习 agent 从语境里给出，代码里没有任何硬编码。
NEGATIVE_EMOJI = "277"
POSITIVE_EMOJI = "76"

#: 真实的上下文片段（逐字抄自 `run/data/logs/chat.jsonl` 的入站记录，只截了正文）。
REAL_CHAT_TAIL = [
    {"at": 1791263373.0, "direction": "in", "kind": "text", "session_id": f"group:{GROUP}",
     "group_id": GROUP, "user_id": "3955067055", "sender_name": "witelb",
     "message_id": "-238671200", "body": "但是这个字幕好像只能识别，不能翻译"},
    {"at": 1791263380.0, "direction": "in", "kind": "text", "session_id": f"group:{GROUP}",
     "group_id": GROUP, "user_id": "3955067056", "sender_name": "云边孤雁丶水上浮萍",
     "message_id": "-238671201", "body": "今天就要回去写模式识别与机器学习了！"},
]

HER_LINE = "识别和翻译是两套，WIN11那个只做了前半段。"
HER_ROW = {
    "at": 1791263400.0, "direction": "out", "session_id": f"group:{GROUP}",
    "group_id": GROUP, "kind": "text", "body": HER_LINE,
    "message_id": HER_MESSAGE_ID, "part": 1, "total": 1,
    "origin": "message:-238671201",
}


def _reaction(emoji_id: str, *, user_id="3955067057", target=HER_MESSAGE_ID,
              group=GROUP, at=1791263500.0, count=1) -> dict:
    return {"at": at, "direction": "in", "kind": "reaction", "session_id": f"group:{group}",
            "group_id": group, "user_id": user_id, "operator_id": user_id,
            "message_id": target, "emoji_id": emoji_id, "count": count, "sub_type": "add"}


def _write_log(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
                    encoding="utf-8")


def _quote_rows(*, emoji_id=NEGATIVE_EMOJI, after: list[dict] | None = None,
                her_row: dict | None = None) -> list[dict]:
    """一份合成日志：她在真实上下文里说了一句话，然后有人给它贴了一个表情。"""

    rows = [dict(row) for row in REAL_CHAT_TAIL]
    rows.append(dict(her_row or HER_ROW))
    rows.extend(after or [])
    rows.append(_reaction(emoji_id))
    return rows


class ScriptedLearner:
    """脚本化的学习 agent：返回固定 JSON（最后一条一直重复），并记下每一次请求。"""

    def __init__(self, *outputs, error: Exception | None = None) -> None:
        self.outputs = list(outputs)
        self.error = error
        self.requests: list = []

    async def complete(self, request):
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        index = min(len(self.requests) - 1, len(self.outputs) - 1)
        return self.outputs[index]


class ScriptedJudge:
    def __init__(self, *outputs) -> None:
        self.outputs = list(outputs) or ["<route>REPLY</route><topic>闲聊</topic>"]
        self.requests: list = []

    async def complete(self, request):
        self.requests.append(request)
        index = min(len(self.requests) - 1, len(self.outputs) - 1)
        return self.outputs[index]


class ScriptedReply:
    def __init__(self, *texts) -> None:
        self.texts = list(texts) or ["我接一句。"]
        self.requests: list = []

    async def complete(self, request):
        self.requests.append(request)
        index = min(len(self.requests) - 1, len(self.texts) - 1)
        return (f"<decision>REPLY</decision><dialogue>KEEP</dialogue>"
                f"<reply>{self.texts[index]}</reply>")


def msg(index, text="测试", *, mentioned=True, user_id="100"):
    return IncomingMessage(
        message_id=f"m{index}", session_id=f"group:{GROUP}", user_id=user_id,
        text=text, target=TARGET, sender_name="某人", is_bot_mentioned=mentioned,
    )


def _agent(tmp: str, learner, *, store: QuoteStore | None = None, logs: str | None = None,
           groups=(GROUP,)):
    """一个跑在临时目录里的 agent（日志目录也隔离，绝不碰真实 `data/`）。"""

    chat = Path(tmp) / "chat.jsonl"
    reply = Path(tmp) / "reply.jsonl"
    return QuoteLearningAgent(
        learner, store or QuoteStore(Path(tmp) / "profile.json"),
        chat_log_path=chat, reply_log_path=reply if reply.exists() else None,
        allowed_groups=groups, clock=lambda: 1791263600.0,
        log_path=Path(logs or tmp) / "quote.jsonl",
    )


# --- 1. 正负分流：她的话 + 表情 → 样本；方向由 agent 从语境推 -------------------


def test_a_reaction_on_her_line_becomes_a_sample_with_her_context() -> None:
    """配对只认 `message_id`：点在她那句话上的表情 → 一条样本，**上下文一起带上**。"""

    rows = _quote_rows(emoji_id=NEGATIVE_EMOJI)
    selection = build_samples(rows, group_ids=(GROUP,))
    assert selection.reactions == 1
    assert selection.matched_reactions == 1
    assert selection.unmatched_reactions == 0
    assert len(selection.samples) == 1
    sample = selection.samples[0]
    assert sample.body == HER_LINE
    assert sample.emoji_ids() == (NEGATIVE_EMOJI,)
    # 她开口之前群里在说什么（真实那两句）**逐字**在样本里。
    assert [item["body"] for item in sample.context_before] == [
        "但是这个字幕好像只能识别，不能翻译", "今天就要回去写模式识别与机器学习了！",
    ]
    assert sample.topic == "" and sample.intent == ""   # 没有 reply.jsonl 就不硬凑


def test_a_reaction_on_someone_elses_message_is_never_a_sample() -> None:
    """**实测的形状**：表情点在**别人**的消息上 → 一条样本都不产出（不按时间邻近去猜）。"""

    rows = [dict(row) for row in REAL_CHAT_TAIL]
    rows.append(dict(HER_ROW))
    rows.append(_reaction(NEGATIVE_EMOJI, target="-238671200"))  # 点在"witelb"那句上
    selection = build_samples(rows, group_ids=(GROUP,))
    assert selection.samples == ()
    assert selection.matched_reactions == 0
    assert selection.unmatched_reactions == 1


def test_the_agent_moves_a_negative_reaction_to_the_negative_side() -> None:
    """**验收 1**：负向表情（拿「祝/猪」当用例，带真实语境）→ 归**反面样本**。

    判据不是代码里的字面表，而是 agent 看完语境给出的 `sense`；这一条验的是
    "样本 + 上下文确实递到了 agent 手里，而且它的判定被原样存下来"。
    """

    output = json.dumps({
        "reaction_meanings": [{
            "group_id": GROUP, "emoji_id": NEGATIVE_EMOJI, "sense": "negative",
            "note": "这是不赞同，不是夸", "quote": "只做了前半段",
        }],
        "style_notes": [{"when": "被人指出说得不对", "note": "补一句就收"}],
        "context_notes": [],
    }, ensure_ascii=False)

    with tempfile.TemporaryDirectory() as tmp:
        learner = ScriptedLearner(output)
        agent = _agent(tmp, learner)
        _write_log(Path(tmp) / "chat.jsonl", _quote_rows(emoji_id=NEGATIVE_EMOJI))
        assert asyncio.run(agent.run_once()) == 1
        # 样本连**上下文**一起递进去了（不是只递那句话）。
        body = json.dumps(learner.requests[0], ensure_ascii=False)
        assert "但是这个字幕好像只能识别，不能翻译" in body
        assert HER_LINE in body
        assert NEGATIVE_EMOJI in body
        # 方向存下来了：反面。
        assert agent.store.effective_sense(GROUP, NEGATIVE_EMOJI) == SENSE_NEGATIVE
        assert agent.store.effective_note(GROUP, NEGATIVE_EMOJI) == "这是不赞同，不是夸"


def test_the_agent_moves_a_positive_reaction_to_the_positive_side() -> None:
    """正向表情 → 正样本（同一个 agent、同一套代码，**只有语境不同**）。"""

    output = json.dumps({
        "reaction_meanings": [{
            "group_id": GROUP, "emoji_id": POSITIVE_EMOJI, "sense": "positive",
            "note": "这是在捧她", "quote": "识别和翻译是两套",
        }],
        "style_notes": [{"when": "被夸", "note": "少来，别贫"}],
        "context_notes": [{"when": "被问事实", "note": "直接给结论，不铺垫"}],
    }, ensure_ascii=False)

    with tempfile.TemporaryDirectory() as tmp:
        agent = _agent(tmp, ScriptedLearner(output))
        _write_log(Path(tmp) / "chat.jsonl", _quote_rows(emoji_id=POSITIVE_EMOJI))
        assert asyncio.run(agent.run_once()) == 2
        assert agent.store.effective_sense(GROUP, POSITIVE_EMOJI) == SENSE_POSITIVE
        assert {item["when"] for item in agent.store.notes} == {"被夸", "被问事实"}


def test_the_same_emoji_can_mean_different_things_in_different_groups() -> None:
    """同一份代码、同一个 emoji：**按群分开存**（含义表是"按群维护"的那一份）。

    两件事分开验：**(a)** 两个群的样本都递到了 agent 手里（指纹里带群号，一个群
    学过的批次不会让另一个群的那条被当成"学过了"）；**(b)** 同一轮里给两个群判出
    不同方向时，两份都原样存下来、各归各的群。
    """

    one = json.dumps({"reaction_meanings": [], "style_notes": [],
                      "context_notes": [{"when": "有人问她一个技术问题", "note": "直接给结论"}]},
                     ensure_ascii=False)

    with tempfile.TemporaryDirectory() as tmp:
        store = QuoteStore(Path(tmp) / "profile.json")
        rows = _quote_rows(emoji_id=POSITIVE_EMOJI)
        # 另一个群里也有"她的一句话 + 同一个表情"。
        rows.append(dict(HER_ROW, group_id=OTHER_GROUP, session_id=f"group:{OTHER_GROUP}",
                         message_id="-999"))
        rows.append(_reaction(POSITIVE_EMOJI, target="-999", group=OTHER_GROUP))
        learner = ScriptedLearner(one)
        agent = _agent(tmp, learner, store=store, groups=(GROUP, OTHER_GROUP))
        _write_log(Path(tmp) / "chat.jsonl", rows)
        asyncio.run(agent.run_once())
        sent = json.dumps(learner.requests, ensure_ascii=False)
        assert GROUP in sent and OTHER_GROUP in sent, "两个群的样本都该递进去"
        seen = "\n".join(store.seen)
        assert GROUP in seen and OTHER_GROUP in seen, "指纹里必须带群号"

        # (b) 同一轮给两个群判出不同方向。
        two = json.dumps({
            "reaction_meanings": [
                {"group_id": GROUP, "emoji_id": POSITIVE_EMOJI, "sense": "positive", "note": "捧场"},
                {"group_id": OTHER_GROUP, "emoji_id": POSITIVE_EMOJI, "sense": "negative",
                 "note": "这群里是哄"},
            ],
            "style_notes": [], "context_notes": [],
        }, ensure_ascii=False)
        parsed = parse_learning_output(two, source_text=HER_LINE)
        store.apply_run(notes=[], meanings=parsed["meanings"], seen=[])
        assert store.effective_sense(GROUP, POSITIVE_EMOJI) == SENSE_POSITIVE
        assert store.effective_sense(OTHER_GROUP, POSITIVE_EMOJI) == SENSE_NEGATIVE
        assert store.effective_note(OTHER_GROUP, POSITIVE_EMOJI) == "这群里是哄"
        # 一份文件里两个群各一行（"按群维护"不是"一个全局表"）。
        raw = json.loads((Path(tmp) / "profile.json").read_text(encoding="utf-8"))
        assert set(raw["meanings"]) == {GROUP, OTHER_GROUP}


# --- 2. 表情含义表：可被操作者覆盖 -------------------------------------------


def test_the_operator_override_wins_over_what_the_agent_learned() -> None:
    """**验收 2**：覆盖一旦写下就压过模型判的；撤掉后又回到模型那一版。"""

    learned = json.dumps({
        "reaction_meanings": [{"group_id": GROUP, "emoji_id": NEGATIVE_EMOJI,
                               "sense": "positive", "note": "它以为是夸"}],
        "style_notes": [], "context_notes": [],
    }, ensure_ascii=False)

    with tempfile.TemporaryDirectory() as tmp:
        agent = _agent(tmp, ScriptedLearner(learned))
        _write_log(Path(tmp) / "chat.jsonl", _quote_rows(emoji_id=NEGATIVE_EMOJI))
        asyncio.run(agent.run_once())
        store = agent.store
        assert store.effective_sense(GROUP, NEGATIVE_EMOJI) == SENSE_POSITIVE

        # 操作者改一个字 → 立刻按改的走（不需要重启、不需要重跑）。
        assert store.override_sense(GROUP, NEGATIVE_EMOJI, "不赞成", "这是在损她") == SENSE_NEGATIVE
        assert store.effective_sense(GROUP, NEGATIVE_EMOJI) == SENSE_NEGATIVE
        assert store.effective_note(GROUP, NEGATIVE_EMOJI) == "这是在损她"
        # 覆盖写进了盘（下一轮回复重新读文件时读到的就是它）。
        store.reload(force=True)
        assert store.effective_sense(GROUP, NEGATIVE_EMOJI) == SENSE_NEGATIVE

        # 撤掉 → 回到模型判的那一版（"改错了能改回来"）。
        store.clear_override(GROUP, NEGATIVE_EMOJI)
        store.reload(force=True)
        assert store.effective_sense(GROUP, NEGATIVE_EMOJI) == SENSE_POSITIVE


def test_an_overridden_emoji_changes_the_material_that_goes_into_the_prompt() -> None:
    """覆盖**真的改到材料**：同一个 emoji，改前/改后给的那句话不一样。"""

    with tempfile.TemporaryDirectory() as tmp:
        store = QuoteStore(Path(tmp) / "profile.json")
        store.apply_run(notes=[], meanings={GROUP: {
            NEGATIVE_EMOJI: {"sense": "positive", "note": "它以为是夸"}}}, seen=[])
        assert "捧" in store.note_for_emoji(GROUP, NEGATIVE_EMOJI)
        store.override_sense(GROUP, NEGATIVE_EMOJI, "不赞成", "在损她")
        assert "不对路" in store.note_for_emoji(GROUP, NEGATIVE_EMOJI)


def test_an_unclear_meaning_produces_no_material() -> None:
    """看不出方向的**不写材料**（宁可不注入，也不误导她）。"""

    with tempfile.TemporaryDirectory() as tmp:
        store = QuoteStore(Path(tmp) / "profile.json")
        store.apply_run(notes=[], meanings={GROUP: {
            POSITIVE_EMOJI: {"sense": SENSE_UNCLEAR, "note": "说不清"}}}, seen=[])
        assert store.note_for_emoji(GROUP, POSITIVE_EMOJI) == ""


def test_the_operator_command_syntax_is_parsed_without_touching_the_core_tables() -> None:
    """`/super quote <emoji> <方向> [说明]` 的解析：认得出、撤得掉、别的形状不误吃。"""

    from qq_roleplay_bot.stage3_main import SuperAction, _parse_quote_override, parse_super_command

    assert parse_super_command("/super quote") is SuperAction.QUOTE
    assert parse_super_command("/super 金句") is SuperAction.QUOTE
    assert _parse_quote_override("/super quote 277 不赞成 在损她") == ("277", "不赞成", "在损她")
    assert _parse_quote_override("/super quote 277 auto") == ("277", "auto", "")
    assert _parse_quote_override("/super quote") is None
    assert _parse_quote_override("/super status") is None
    # 空方向 / 空 emoji 一律不算改。
    assert _parse_quote_override("/super quote  不赞成") is None
    # 它**不再是**通用 topic 命令（`/super quote` 不许被当成 `/super memory` 那种）。
    assert parse_super_command("/super restart quote") is None


# --- 3. 笔记的护栏 -----------------------------------------------------------


def test_notes_never_carry_mechanism_words() -> None:
    """**验收 3**：含机制词的笔记一条都进不来（`prompt_guard` 全表零命中）。"""

    output = json.dumps({
        "reaction_meanings": [],
        "style_notes": [
            {"when": "被夸", "note": "少来，别贫"},
            {"when": "被夸", "note": "这是检查过的句式"},        # 机制词 → 丢
            {"when": "被夸", "note": "按上下文挑一条"},          # 机制词 → 丢
        ],
        "context_notes": [{"when": "冷场", "note": "就这么看着，不说话"}],
    }, ensure_ascii=False)

    with tempfile.TemporaryDirectory() as tmp:
        agent = _agent(tmp, ScriptedLearner(output))
        _write_log(Path(tmp) / "chat.jsonl", _quote_rows(emoji_id=POSITIVE_EMOJI))
        asyncio.run(agent.run_once())
        notes = [item["note"] for item in agent.store.notes]
        assert "少来，别贫" in notes
        assert "就这么看着，不说话" in notes
        for note in notes:
            assert scan_persona_text(note) == [], f"机制词漏进来了：{note}"
        # 整个文件里也不许有机制词（连 `when` 一起）。
        raw = (Path(tmp) / "profile.json").read_text(encoding="utf-8")
        for word in ("检查", "触发", "调用", "协议", "提示词", "上下文", "记忆库", "Stage"):
            assert word not in raw, f"{word} 不该出现在学出来的东西里"


def test_an_empty_sample_set_does_not_produce_an_empty_note_and_costs_nothing() -> None:
    """**验收 3**：样本为空 → **连模型都不调**，一个字节都不写。"""

    with tempfile.TemporaryDirectory() as tmp:
        learner = ScriptedLearner("{}")
        agent = _agent(tmp, learner)
        # 日志里只有别人在说话（**实测形状**：51 个表情全点在别人头上）。
        _write_log(Path(tmp) / "chat.jsonl", [dict(row) for row in REAL_CHAT_TAIL])
        assert asyncio.run(agent.run_once()) == 0
        assert learner.requests == [], "没有样本时不许调模型（那是白花钱）"
        assert agent.store.notes == []
        assert not (Path(tmp) / "profile.json").exists()


def test_a_note_that_is_a_slice_of_her_own_line_is_dropped() -> None:
    """**验收 3**：与样本原句逐字重合的笔记**丢掉**（注入的是风格，不是让她背的句子）。"""

    output = json.dumps({
        "reaction_meanings": [],
        "style_notes": [
            {"when": "被问事实", "note": "WIN11那个只做了前半段"},     # 原句的一截 → 丢
            {"when": "被问事实", "note": "先给结论，再补一句为什么"},  # 风格 → 留
        ],
        "context_notes": [],
    }, ensure_ascii=False)

    with tempfile.TemporaryDirectory() as tmp:
        agent = _agent(tmp, ScriptedLearner(output))
        _write_log(Path(tmp) / "chat.jsonl", _quote_rows(emoji_id=POSITIVE_EMOJI))
        assert asyncio.run(agent.run_once()) == 1
        notes = [item["note"] for item in agent.store.notes]
        assert notes == ["先给结论，再补一句为什么"]
        # 短口吻（几个字）是放行的：那是说法，不是句子。
        assert "少来，别贫" not in notes
        assert "WIN11那个只做了前半段" not in json.dumps(agent.store.notes, ensure_ascii=False)


def test_over_long_and_imperative_notes_are_dropped() -> None:
    """太长（不成其为笔记）与命令式（变成指令）的一律丢。"""

    parsed = parse_learning_output(json.dumps({
        "reaction_meanings": [],
        "style_notes": [
            {"when": "任何时候", "note": "你" * MAX_NOTE_CHARS + "长"},
            {"when": "任何时候", "note": "你必须每次都这么回"},
            {"when": "任何时候", "note": "她就这么接话"},
        ],
        "context_notes": [],
    }, ensure_ascii=False))
    notes = [item["note"] for item in parsed["notes"]]
    assert notes == ["她就这么接话"]
    assert parsed["dropped"]["note_long"] == 1
    assert parsed["dropped"]["note_word"] >= 1


def test_the_request_carries_the_guard_rules_and_never_a_hardcoded_emoji_table() -> None:
    """system 那一份里**没有**任何"哪个表情是正、哪个是负"的表（方向必须从语境推）。

    判据是**真实的 emoji_id 一个都不在**：代码里只要出现 `"277" → negative` 这种字面映射，
    这条就红。方向词本身（`positive` / `negative`）必须有——那是**要它去填的取值**，
    不是替它填好的结论。
    """

    from qq_roleplay_bot.quote_learning import LEARNING_SYSTEM_PROMPT

    for emoji_id in ("277", "76", "387", "128516", "49", "476", "424"):
        assert emoji_id not in LEARNING_SYSTEM_PROMPT, f"不许硬编码表情含义：{emoji_id}"
    assert "positive" in LEARNING_SYSTEM_PROMPT and "negative" in LEARNING_SYSTEM_PROMPT
    # 它必须点明"要结合谁贴的、她说了什么、贴完群里怎么接"。
    assert "谁贴的" in LEARNING_SYSTEM_PROMPT and "群里怎么接" in LEARNING_SYSTEM_PROMPT


# --- 4. 注入 ----------------------------------------------------------------


def test_injection_picks_at_most_n_by_context_and_is_descriptive_material() -> None:
    """**验收 4**：按语境挑 ≤N 条；措辞是"她以前这么说过"，不是命令。

    这里用的 `when` 是**照她真实那句话的场合写的**（"有人问她一个技术问题"），
    不是分析报告腔——笔记本来就是"什么场合说什么"，场合对不上就不该给它。
    """

    with tempfile.TemporaryDirectory() as tmp:
        store = QuoteStore(Path(tmp) / "profile.json")
        # 8 条同场合的笔记（验上限）＋ 一条别场合的（不该被挑中）。
        notes = [{"kind": "context", "when": "有人说字幕翻译的事", "note": f"直接给结论{i}"}
                 for i in range(8)]
        notes.append({"kind": "context", "when": "有人在说她不爱听的话", "note": "不接，就当没看见"})
        store.apply_run(notes=notes, meanings={}, seen=[])
        profile = QuoteProfile(store)
        picked = profile.notes_for(GROUP, "这个字幕只能识别不能翻译，是真的吗", "字幕翻译")
        assert len(picked) == 5, "同场合的笔记按上限给，不多给"
        assert all("直接给结论" in item["note"] for item in picked), "别的场合那条不该进来"
        block = inject_block(picked)
        assert block.startswith("--- 她以前遇到这种时候 ---")
        assert "直接给结论" in block
        for word in IMPERATIVE_WORDS:
            assert word not in block, f"材料里出现了命令式词：{word}"
        # 它明确写着"不是要你照着念的话"。
        assert "不是要你照着念的话" in block


def test_a_note_from_another_occasion_is_not_injected() -> None:
    """**验收 4**：场合对不上就不给（"有人在说她不爱听的话"vs 眼前这句问技术问题）。"""

    with tempfile.TemporaryDirectory() as tmp:
        store = QuoteStore(Path(tmp) / "profile.json")
        store.apply_run(notes=[{"kind": "context", "when": "有人在说她不爱听的话",
                                "note": "不接，就当没看见"}], meanings={}, seen=[])
        assert QuoteProfile(store).notes_for(GROUP, "这个字幕只能识别不能翻译，是真的吗",
                                             "字幕翻译") == []


def test_the_note_picker_falls_back_to_single_characters_when_wording_differs() -> None:
    """措辞不完全一样、但**场合沾边**时退一步给 1~2 条（主判据一条都没有时的兜底）。

    这里 `when` 是"她被人问了一个事实"、眼前这句是"……是真的吗"——共享的字只有
    「是」「真的」那几个，词组一个都不共享，正是这条兜底要处理的情形。
    """

    with tempfile.TemporaryDirectory() as tmp:
        store = QuoteStore(Path(tmp) / "profile.json")
        store.apply_run(notes=[{"kind": "style", "when": "她被人问了一个事实", "note": "先给结论"},
                               {"kind": "style", "when": "她说真的吗的时候", "note": "别绕"},
                               {"kind": "style", "when": "她在说别的事", "note": "第三句"}],
                        meanings={}, seen=[])
        picked = QuoteProfile(store).notes_for(GROUP, "这个字幕只能识别不能翻译，是真的吗",
                                               "字幕翻译")
        from qq_roleplay_bot.quote_learning import NOTE_CHAR_FALLBACK_MAX

        assert 0 < len(picked) <= NOTE_CHAR_FALLBACK_MAX
        assert all(item["note"] != "第三句" for item in picked), "不沾边的那条不该被兜进来"


def test_injection_respects_the_character_budget() -> None:
    """材料再长也不会把 prompt 撑大（有字符预算）。"""

    with tempfile.TemporaryDirectory() as tmp:
        store = QuoteStore(Path(tmp) / "profile.json")
        store.apply_run(notes=[{"kind": "style", "when": "被问事实的时候",
                                "note": "先给结论再说原因，别绕"} for _ in range(5)],
                        meanings={}, seen=[])
        block = inject_block(QuoteProfile(store).notes_for(GROUP, "被问事实", "被问事实"))
        from qq_roleplay_bot.quote_learning import INJECT_CHAR_BUDGET

        assert len(block) < INJECT_CHAR_BUDGET + 120


def test_no_notes_means_no_block_at_all() -> None:
    """**验收 4**：样本为空（或没沾边的）→ **不注入**（不写空壳）。"""

    with tempfile.TemporaryDirectory() as tmp:
        profile = QuoteProfile(QuoteStore(Path(tmp) / "profile.json"))
        assert profile.notes_for(GROUP, "随便说点什么", "闲聊") == []
        assert inject_block([]) == ""
        # 有笔记但一句都不沾边时也不注入。
        store = QuoteStore(Path(tmp) / "profile.json")
        store.apply_run(notes=[{"kind": "style", "when": "被夸的时候", "note": "少来"}],
                        meanings={}, seen=[])
        assert QuoteProfile(store).notes_for(GROUP, "今天天气不错", "天气") == []


def test_turning_the_switch_off_makes_the_request_byte_for_byte_identical() -> None:
    """**验收 4（核心）**：关掉开关 → 请求与改动前**逐字相同**。

    做法是**引擎那条真路**：同一句话跑两遍，一遍开着（有笔记）、一遍关掉；
    关掉那一次的两次请求必须与"根本没有这份材料"时完全一致（拿字符串直接比）。
    """

    current = msg(1, "这个字幕只能识别不能翻译，是真的吗")
    history = [current]

    def build(style_material: str):
        return build_dialogue_messages(
            history, current=current, mode=ConversationMode.ACTIVE, trigger="threshold",
            context=ContextState(topic="实时字幕翻译"), style_material=style_material,
        )

    baseline = build("")
    with tempfile.TemporaryDirectory() as tmp:
        store = QuoteStore(Path(tmp) / "profile.json")
        store.apply_run(notes=[{"kind": "context", "when": "有人说字幕翻译的事",
                                "note": "先给结论，再补一句为什么"}], meanings={}, seen=[])
        profile = QuoteProfile(store)
        reply = ScriptedReply("识别和翻译是两套。")
        judge = ScriptedJudge("<route>REPLY</route><topic>字幕翻译</topic>")
        engine = DialogueEngine(reply, judge_client=judge, group_listen=True,
                                enabled_group_ids=frozenset({GROUP}),
                                quote_profile=profile)
        # `_style_material` 只读 `state.context.topic`：给一个最小的替身就够，
        # 不必为了这一条去跑一整轮对话。
        state = SimpleNamespace(context=ContextState(topic="字幕翻译"))
        on_request = build(engine._style_material(current, state))
        assert on_request != baseline, "开着的时候必须真的多了那几条材料（否则这条测试没意义）"
        assert "先给结论，再补一句为什么" in json.dumps(on_request, ensure_ascii=False)

        # 关掉开关：拿**同一份**材料去问引擎，它必须什么都不给。
        from qq_roleplay_bot import runtime_flags

        flags = runtime_flags.shared()
        runtime_flags.install(runtime_flags.RuntimeFlags(quote_enabled=False))
        try:
            off_request = build(engine._style_material(current, state))
        finally:
            runtime_flags.install(flags if flags is not None else runtime_flags.RuntimeFlags())
    assert off_request == baseline, "关掉开关后请求必须与改动前逐字相同"


def test_the_engine_injects_the_material_into_the_data_section_only() -> None:
    """材料进的是 **user 段的 DATA 区**：system 前缀一个字都不动（§2.1）。"""

    with tempfile.TemporaryDirectory() as tmp:
        store = QuoteStore(Path(tmp) / "profile.json")
        store.apply_run(notes=[{"kind": "context", "when": "有人在说字幕翻译的事",
                                "note": "先给结论，再补一句为什么"}], meanings={}, seen=[])
        profile = QuoteProfile(store)
        reply = ScriptedReply("识别和翻译是两套。")
        judge = ScriptedJudge("<route>REPLY</route><topic>字幕翻译</topic>")
        engine = DialogueEngine(reply, judge_client=judge, group_listen=True,
                                enabled_group_ids=frozenset({GROUP}),
                                quote_profile=profile)
        from qq_roleplay_bot import runtime_flags

        flags = runtime_flags.shared()
        runtime_flags.install(runtime_flags.RuntimeFlags(quote_enabled=True))
        try:
            result = asyncio.run(engine.handle(msg(1, "这个字幕只能识别不能翻译，是吗")))
        finally:
            runtime_flags.install(flags) if flags is not None else runtime_flags.install(
                runtime_flags.RuntimeFlags())
        assert result is not None
        request = reply.requests[0]
        system = request[0]
        assert system["role"] == "system"
        assert "先给结论" not in system["content"], "材料绝不许进 system"
        assert "她以前遇到这种时候" not in system["content"]
        # 它在一个 user 段里，而且在那一段的 DATA 区（"你记得的旧事"之后）。
        users = [item for item in request if item["role"] == "user"]
        assert len(users) >= 2
        volatile = users[-1]["content"]
        assert "她以前遇到这种时候" in volatile
        assert volatile.index("你记得的旧事 结束") < volatile.index("她以前遇到这种时候")
        assert volatile.index("她以前遇到这种时候") < volatile.index("<current_event>")


def test_a_broken_material_file_never_affects_the_reply() -> None:
    """**验收 5**：材料读不开（文件坏 / 写不进去）→ 照样收、照样回，只少一份材料。"""

    with tempfile.TemporaryDirectory() as tmp:
        # 用目录占住文件名：读会 OSError、写也会失败。
        (Path(tmp) / "profile.json").mkdir()
        store = QuoteStore(Path(tmp) / "profile.json")
        assert store.last_error != "" or store.notes == []
        reply = ScriptedReply("识别和翻译是两套。")
        judge = ScriptedJudge("<route>REPLY</route><topic>实时字幕翻译</topic>")
        engine = DialogueEngine(reply, judge_client=judge, group_listen=True,
                                enabled_group_ids=frozenset({GROUP}),
                                quote_profile=QuoteProfile(store))
        result = asyncio.run(engine.handle(msg(1, "这个字幕只能识别不能翻译，是吗")))
        assert result is not None and result.text == "识别和翻译是两套。"
        assert len(reply.requests) == 1
        assert "她以前遇到这种时候" not in json.dumps(reply.requests[0], ensure_ascii=False)


# --- 5. 定期任务：失败不影响对话，也不重复烧钱 --------------------------------


def test_a_failed_learning_run_is_only_counted_and_never_raises() -> None:
    """模型调用炸了 → 只记一笔（`failures`），**不抛给任何人**。"""

    with tempfile.TemporaryDirectory() as tmp:
        agent = _agent(tmp, ScriptedLearner(error=RuntimeError("模型挂了")))
        _write_log(Path(tmp) / "chat.jsonl", _quote_rows(emoji_id=POSITIVE_EMOJI))
        assert asyncio.run(agent.run_once()) == 0
        assert agent.failures == 1
        assert agent.store.notes == []


def test_the_unparsable_output_does_not_write_notes_but_marks_the_batch_read() -> None:
    """模型输出读不出来 → 不写笔记；但**同一批不再反复送去烧钱**（幂等那本账）。"""

    with tempfile.TemporaryDirectory() as tmp:
        agent = _agent(tmp, ScriptedLearner("我看不懂，随便说两句"))
        _write_log(Path(tmp) / "chat.jsonl", _quote_rows(emoji_id=POSITIVE_EMOJI))
        assert asyncio.run(agent.run_once()) == 0
        assert agent.store.notes == []
        assert agent.store.seen, "学过的批次要记账，否则每轮都重复烧钱"
        assert asyncio.run(agent.run_once()) == 0
        assert len(agent.client.requests) == 1, "同一批不该再学第二遍"


def test_a_new_reaction_on_an_already_seen_line_is_learned_again() -> None:
    """老样本**又被贴了一个新表情**（新信号）→ 再学一次；没有新信号就不学。"""

    with tempfile.TemporaryDirectory() as tmp:
        output = json.dumps({"reaction_meanings": [], "style_notes": [],
                             "context_notes": [{"when": "被问事实", "note": "先给结论"}]},
                            ensure_ascii=False)
        learner = ScriptedLearner(output)
        agent = _agent(tmp, learner)
        rows = _quote_rows(emoji_id=POSITIVE_EMOJI)
        _write_log(Path(tmp) / "chat.jsonl", rows)
        assert asyncio.run(agent.run_once()) == 1
        assert asyncio.run(agent.run_once()) == 0     # 一模一样 → 不学
        assert len(learner.requests) == 1
        rows.append(_reaction(NEGATIVE_EMOJI))        # 又来一个表情
        _write_log(Path(tmp) / "chat.jsonl", rows)
        assert asyncio.run(agent.run_once()) == 1
        assert len(learner.requests) == 2


def test_new_samples_ignores_what_was_already_learned() -> None:
    """`new_samples` 的判据：`sample_id` + 表情指纹（只记 id 会漏掉"又贴了一个"）。"""

    rows = _quote_rows(emoji_id=POSITIVE_EMOJI)
    selection = build_samples(rows, group_ids=(GROUP,))
    seen: set[str] = set()
    assert len(new_samples(selection, seen)) == 1
    from qq_roleplay_bot.quote_samples import fingerprint_of

    seen.add(fingerprint_of(selection.samples[0]))
    assert new_samples(selection, seen) == ()
    rows.append(_reaction(NEGATIVE_EMOJI))
    assert len(new_samples(build_samples(rows, group_ids=(GROUP,)), seen)) == 1


def test_one_failing_tick_does_not_stop_the_periodic_task() -> None:
    """节拍里一轮炸了：记一笔、**继续等下一次**（不把任务带走）。"""

    with tempfile.TemporaryDirectory() as tmp:
        agent = _agent(tmp, ScriptedLearner(error=RuntimeError("模型挂了")))
        _write_log(Path(tmp) / "chat.jsonl", _quote_rows(emoji_id=POSITIVE_EMOJI))

        async def run():
            task = asyncio.create_task(agent.run(0.05))
            await asyncio.sleep(0.12)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        asyncio.run(run())
        assert agent.failures >= 1, "一轮失败之后必须还有下一轮"
        assert agent._lock.locked() is False


def test_the_agent_never_touches_the_conversation_path() -> None:
    """**它只在定期那一趟里调模型**：一次对话（`engine.handle`）里零次模型调用。"""

    with tempfile.TemporaryDirectory() as tmp:
        learner = ScriptedLearner("{}")
        agent = _agent(tmp, learner)
        _write_log(Path(tmp) / "chat.jsonl", _quote_rows(emoji_id=POSITIVE_EMOJI))
        reply = ScriptedReply("在。")
        judge = ScriptedJudge("<route>REPLY</route><topic>闲聊</topic>")
        engine = DialogueEngine(reply, judge_client=judge, group_listen=True,
                                enabled_group_ids=frozenset({GROUP}),
                                quote_profile=QuoteProfile(agent.store))
        asyncio.run(engine.handle(msg(1, "在吗")))
        assert learner.requests == [], "热路径上一次模型调用都不许有"
        assert len(reply.requests) == 1


def test_the_switch_defaults_to_on_and_turns_off_from_the_environment() -> None:
    """**开关**：`QQBOT_QUOTE_LEARN` 没设时按配置的默认（开）；写 0 才关（用户要的形状）。"""

    from qq_roleplay_bot import dev_config

    with patch.dict(os.environ, {"QQBOT_QUOTE_LEARN": "1"}):
        assert learning_enabled() is True
    with patch.dict(os.environ, {"QQBOT_QUOTE_LEARN": "0"}):
        assert learning_enabled() is False
    env = dict(os.environ)
    env.pop("QQBOT_QUOTE_LEARN", None)
    with patch.dict(os.environ, env, clear=True):
        assert learning_enabled() is bool(dev_config.QUOTE_LEARN_ENABLED)
    # 关掉开关时**装配**也不装（`build_quote_agent` 返回 None）——省一次模型调用。
    with patch.dict(os.environ, {"QQBOT_QUOTE_LEARN": "0"}):
        assert build_quote_agent(object()) is None
    with patch.dict(os.environ, {"QQBOT_QUOTE_LEARN": "1"}):
        assert build_quote_agent(None) is None, "没有 client 也不装"


def test_the_engine_replies_normally_when_there_is_no_material_at_all() -> None:
    """没有金句学习（装配那一步返回 None）时，回复路径**什么都不多写**。"""

    current = msg(1, "在吗")
    reply = ScriptedReply("在。")
    judge = ScriptedJudge("<route>REPLY</route><topic>闲聊</topic>")
    engine = DialogueEngine(reply, judge_client=judge, group_listen=True,
                            enabled_group_ids=frozenset({GROUP}))
    assert engine.quote_profile is None
    assert engine._style_material(current, SimpleNamespace(context=ContextState())) == ""
    result = asyncio.run(engine.handle(current))
    assert result is not None and result.text == "在。"
    system_text = json.dumps(reply.requests[0][0], ensure_ascii=False)
    assert "她以前遇到这种时候" not in system_text


def test_the_material_is_wired_into_the_volatile_section_in_the_source() -> None:
    """**源码顺序**也算判据：`style_material` 只能出现在易变段（user 段）里。

    与 `tests/test_quote_injection_shape.py` 那组是**互补**的：那组在干净进程里验
    "组装出来的请求长什么样"（所以"改成进 system 段"那种突变它会红）；
    这一条直接在源码上量一次顺序——同一个进程里改不了已加载的常量，
    但源码那一步永远读得到（改错了这一条也会红）。
    """

    import inspect

    from qq_roleplay_bot import stage3_runtime

    source = inspect.getsource(stage3_runtime.build_dialogue_messages)
    material_at = source.index("+ style_material")
    volatile_at = source.index("volatile_content = (")
    system_at = source.index('{"role": "system"')
    assert volatile_at < material_at < source.index("return [", material_at)
    assert material_at < system_at, "材料必须写在易变段里，不能进 system 那一份"
    assert "style_material" in inspect.signature(
        stage3_runtime.build_dialogue_messages).parameters


# --- 与真实日志的对照（只读，不改任何东西）-----------------------------------


def test_the_real_chat_log_yields_the_samples_it_actually_has() -> None:
    """**实测留档**：拿真实 `run/data/logs/chat.jsonl` 跑一次选取。

    这一条**不假设**里面有样本——真实数据当时**一条都没有**（51 个表情全点在别人头上）。
    它钉的是"配对判据没有被放宽"：`matched + unmatched == reactions`，
    而且每一条样本的 `message_id` 都真的能在日志里找到**她**那条消息。
    """

    real = Path(__file__).resolve().parents[2] / "run" / "data" / "logs" / "chat.jsonl"
    if not real.exists():
        return  # 没有那份日志（干净 clone）：这条只在这台机器上有意义
    selection = collect_samples(real)
    assert selection.matched_reactions + selection.unmatched_reactions == selection.reactions
    rows = [json.loads(line) for line in real.read_text(encoding="utf-8").splitlines()
            if line.strip()]
    her_ids = {
        str(row.get("message_id"))
        for row in rows
        if row.get("direction") == "out" and row.get("kind") != "reaction"
    }
    for sample in selection.samples:
        assert sample.reactions, "样本必须带着表情"
        assert sample.body, "样本必须是她说过的话"
        assert sample.message_id in her_ids, "样本必须是她自己那条消息"
