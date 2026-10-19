"""「有人拿工具式的称呼叫她」→ 这一轮的回复材料里多一句（2026-10-20）。

## 由来（生产日志实测，群 `908696203` 2026-10-20 凌晨）

    04:32:48 Echokovo：bot你帮我看看 我睡了
             她：专利这事得你自己去翻，我不替他查。
    04:33:51 Echokovo：bot你敢不敢总结我们刚才的对话
             她：这有什么不敢的。从…卡在"热重建怎么做"上没了下文。

两次这么叫她，**她两次都顺着接了**（等于默认了这个称呼）。用户口径：
*"不接这个称呼，但也不装没听见"*（反问 / 把话头拨回去 / 只回正题、态度冷一点）。

## 用户定的形状（照做，别换方案）

1. **人格正文里一个字都不加**（那等于每轮提醒她有这么个身份，AGENTS §2.2）；
2. 纯正则判据，只看**这一轮新到的消息正文**（历史不算）；
3. 命中时往**易变段**加**一行**，与 `clarify_note` 那个口子相邻；同一轮只加一次；
   **不影响判定**（`lore` 那条已废弃，别碰）；
4. 那一行**不许出现** `bot` / `机器人` / `AI` / `程序`（提它等于又提醒一次）；
5. 计数 `reply_tool_vocative_hints` + 一条 INFO（**只有条数与 session，不记正文**）。

## 这一份钉住

1. 生产那两句原文**必须命中**；同形态的几种写法也命中；
2. 技术讨论那种日常写法**一个都不许命中**（误伤就是把每轮都加一句）；
3. 命中时材料里有那一行、没命中时没有；
4. **那一行本身不含那四个词**，并且它加进来**不增加**材料里这四个词的任何一次出现
   （"不变式"那种绝对写法在这里做不到，也不能那么写：触发它的那条消息正文里就带着
   `bot`，人格模板里本来就写着 `机器人`/`程序`/`AI`——所以钉的是"来自这条提示"这一半）；
5. 一句话里命中几处，只加**一次**；历史里出现过那种称呼**不算**；
6. 计数与日志都只有条数与 session，**没有正文**；判定那一路看不到这一行。
"""
import asyncio
import logging
import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from qq_roleplay_bot.api_usage import COUNTER_NAMES
from qq_roleplay_bot.prompt_guard import PERSONA_MECHANISM_WORDS, scan_persona_text
from qq_roleplay_bot.stage3_main import DialogueEngine
from qq_roleplay_bot.stage3_runtime import ContextState, ConversationMode, build_dialogue_messages
from qq_roleplay_bot.stance_hints import TOOL_VOCATIVE_NOTE, looks_like_tool_vocative
from qq_roleplay_bot.transport import IncomingMessage, MessageTarget

GROUP = "908696203"
TARGET = MessageTarget(group_id=GROUP)
SESSION = f"group:{GROUP}"
NOW = datetime(2026, 10, 20, 4, 32, tzinfo=timezone(timedelta(hours=8))).timestamp()
TZ = {"QQBOT_LOCAL_TZ_OFFSET_HOURS": "8"}

#: 生产原文（2026-10-20 凌晨那两句，实测她两次都顺着接了）。
REAL_ONE = "bot你帮我看看 我睡了"
REAL_TWO = "bot你敢不敢总结我们刚才的对话"

#: 用户点名要能认出来的另外几种写法（同形态）。
SAME_SHAPE = (
    "你个bot",
    "机器人是吧",
    "你们这些机器人",
    "bot，帮我看看这个",
    "AI你听我说",
    "你不是真人吧",
    "你就是个机器人",
    "叫你个机器人",
    "你是机器人吧",
    "机器人吧",
)

#: 用户点名**不许命中**的五条 + 真实日志里最像的几条（都是群里的日常技术聊法）。
NOT_HITS = (
    # 用户点名的那五条（缺一不可）
    "部署了一个机器人",
    "这个 bot 挺好用",
    "大模型跑出来的包",
    "AI 能跑不等于该往里搭工夫",
    "你把消息给他",
    # 真实日志里的近邻（2026-10-20 在 chat.jsonl 上跑判据时逐条看过）
    "扫地机器人和那种洗地机哪个能拖更干净",
    "你可以试着和你ai说一声",          # 这是"你（的）AI"，不是在叫她——踩到过
    "官方bot没法主动接话吧",
    "这玩意儿是机器人吧",              # 讲的是别人家的 bot
    "你说它是机器人吧",                # 同上：中间隔着"说它是"，不是在说她
    "你看那个机器人吧",
    "机器人吧，我看挺贵的",            # 话题标记（后面还有半句）
    "没事干 bot",
    "怎么没加verifybot，那我也不进了",
    "你的机器人伤到我了",
    "这个 robot 挺好用",
    "bot 当雌小鬼的唯一问题",
)


def _message(index: int, text: str) -> IncomingMessage:
    # `is_bot_mentioned=True`：这些用例要的是**回复那一路**的材料，判定由下面的假 client 定。
    return IncomingMessage(f"m{index}", SESSION, "100", text, TARGET,
                           sender_name="某人", is_bot_mentioned=True)


class _ScriptedJudge:
    def __init__(self, outputs: list[str] | None = None) -> None:
        self.outputs = outputs or ["<route>REPLY</route><topic>闲聊</topic>"]
        self.requests: list = []

    async def complete(self, request):
        self.requests.append(request)
        index = min(len(self.requests) - 1, len(self.outputs) - 1)
        return self.outputs[index]


class _ScriptedReply:
    def __init__(self) -> None:
        self.requests: list = []

    async def complete(self, request):
        self.requests.append(request)
        return ("<decision>REPLY</decision><dialogue>KEEP</dialogue>"
                "<reply>行。</reply>")


def _engine() -> tuple[DialogueEngine, _ScriptedJudge, _ScriptedReply]:
    from qq_roleplay_bot.runtime_flags import RuntimeFlags

    judge = _ScriptedJudge()
    reply = _ScriptedReply()
    engine = DialogueEngine(reply, judge_client=judge, enabled_group_ids=frozenset({GROUP}),
                            wall_clock=lambda: NOW)
    engine._default_flags = RuntimeFlags(review_enabled=False)
    return engine, judge, reply


def _material(request) -> str:
    """整份材料（system + user 各段）拼起来——"她看到的东西"那一个整体。"""

    return "\n".join(str(item.get("content") or "") for item in request)


def _volatile(request) -> str:
    return str(request[-1]["content"])


class _ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@contextmanager
def _capture(logger_name: str):
    logger = logging.getLogger(logger_name)
    handler = _ListHandler()
    previous = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        yield handler.records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)


def _run(engine, text: str, index: int = 1):
    with patch.dict(os.environ, TZ):
        return asyncio.run(engine.handle(_message(index, text)))


# --- 1. 正例：生产原文 + 同形态 ----------------------------------------------


def test_the_two_production_lines_hit() -> None:
    """2026-10-20 凌晨那两句，**必须**命中（用户点名）。"""

    assert looks_like_tool_vocative(REAL_ONE) is True, REAL_ONE
    assert looks_like_tool_vocative(REAL_TWO) is True, REAL_TWO


def test_the_same_shapes_elsewhere_hit() -> None:
    for text in SAME_SHAPE:
        assert looks_like_tool_vocative(text) is True, f"该命中却没命中：{text}"


def test_case_does_not_matter_for_the_english_token() -> None:
    assert looks_like_tool_vocative("BOT你敢不敢") is True


# --- 2. 反例：日常技术讨论一个都不许命中 --------------------------------------


def test_ordinary_tool_talk_does_not_hit() -> None:
    for text in NOT_HITS:
        assert looks_like_tool_vocative(text) is False, f"误伤了：{text}"


def test_empty_text_does_not_hit() -> None:
    for text in ("", "   ", None):
        assert looks_like_tool_vocative(text) is False, repr(text)


# --- 3. 注入：命中时材料里有那一行，没命中时没有 -------------------------------


def test_a_hit_puts_the_line_into_the_reply_material() -> None:
    engine, _, reply = _engine()
    assert _run(engine, REAL_ONE) is not None, "这一轮没有回复，后面的断言就没意义了"

    material = _material(reply.requests[0])
    assert TOOL_VOCATIVE_NOTE in material, "命中了却没把那一行递给她"
    # 位置：易变段（最后那条 user 消息），并且**紧挨着** `<current_event>`——与
    # `clarify_note` 那个口子同一处（用户定的"相邻"）。
    volatile = _volatile(reply.requests[0])
    tail = volatile[volatile.index(TOOL_VOCATIVE_NOTE):]
    assert tail.startswith(TOOL_VOCATIVE_NOTE)
    assert tail[len(TOOL_VOCATIVE_NOTE):].startswith("<current_event>"), tail[:200]


def test_a_non_hit_leaves_the_material_alone() -> None:
    engine, _, reply = _engine()
    assert _run(engine, "这个 bot 挺好用") is not None

    material = _material(reply.requests[0])
    assert TOOL_VOCATIVE_NOTE not in material, "没命中却加了一行"
    assert engine._stats["reply_tool_vocative_hints"] == 0


# --- 4. 不变式：那一行自己不含那四个词，加进来也不增加它们 ---------------------


def test_the_line_names_none_of_the_four_words() -> None:
    """用户口径：那一行里不许出现 `bot` / `机器人` / `AI` / `程序`。"""

    lowered = TOOL_VOCATIVE_NOTE.lower()
    for word in ("bot", "机器人", "ai", "程序"):
        assert word not in lowered, f"那一行里出现了 {word}"


def test_the_line_has_no_mechanism_words() -> None:
    """AGENTS §2.2：机制词一个都不许有（措辞会被角色吸收）。"""

    assert scan_persona_text(TOOL_VOCATIVE_NOTE) == [], \
        scan_persona_text(TOOL_VOCATIVE_NOTE)
    for word in PERSONA_MECHANISM_WORDS:
        assert word not in TOOL_VOCATIVE_NOTE, word


def test_the_line_adds_no_naming_word_to_the_material() -> None:
    """**不变式**：同一轮、同一份输入，加与不加这一行，四个词的条数完全一样。

    为什么不做"整份材料里一个都没有"那种绝对断言（用户那句话的括注已经点明口径）：
    触发它的那条消息正文里就带着 `bot`；人格模板里本来就写着 `机器人`/`程序`/`AI`。
    所以能钉、也真正要钉的是"**来自这条提示**"那一半——加进来之后这四个词
    一次都不多。用例用 `build_dialogue_messages` 直接对同一份输入做两遍。
    """

    current = _message(1, REAL_ONE)
    with_noteline = build_dialogue_messages(
        [current], current=current, mode=ConversationMode.IDLE, trigger="mention",
        context=ContextState(topic="闲聊"), clarify_note=TOOL_VOCATIVE_NOTE,
    )
    without = build_dialogue_messages(
        [current], current=current, mode=ConversationMode.IDLE, trigger="mention",
        context=ContextState(topic="闲聊"), clarify_note="",
    )
    with_text, without_text = _material(with_noteline), _material(without)
    assert TOOL_VOCATIVE_NOTE in with_text
    assert TOOL_VOCATIVE_NOTE not in without_text
    # 只多了那一行（逐字），别的一个字都没动。
    assert with_text.replace(TOOL_VOCATIVE_NOTE, "", 1) == without_text
    for word in ("bot", "机器人", "AI", "程序"):
        assert with_text.count(word) == without_text.count(word), word


# --- 5. 一次一轮一行；历史不算 ------------------------------------------------


def test_several_hits_in_one_message_still_add_only_one_line() -> None:
    engine, _, reply = _engine()
    assert _run(engine, "你个bot，机器人是吧") is not None

    volatile = _volatile(reply.requests[0])
    assert volatile.count(TOOL_VOCATIVE_NOTE) == 1, "同一轮被加了不止一次"
    assert engine._stats["reply_tool_vocative_hints"] == 1


def test_a_vocative_in_the_history_does_not_add_the_line() -> None:
    """用户口径：只对这一轮**新到的**消息正文判断，历史不算。"""

    engine, _, reply = _engine()
    assert _run(engine, REAL_ONE) is not None          # 第一轮命中
    assert engine._stats["reply_tool_vocative_hints"] == 1
    assert _run(engine, "行，睡吧", index=2) is not None   # 第二轮是普通一句

    second = _material(reply.requests[1])
    # 历史里那句"bot你帮我看看"还在（它进了**稳定段**的历史，不在易变段），但
    # **不再加那一行**——判据只看这一轮新到的那条消息。
    assert "bot你帮我看看" in second, "这条用例的前提不成立了：历史里没有那一句"
    assert TOOL_VOCATIVE_NOTE not in second
    assert engine._stats["reply_tool_vocative_hints"] == 1


# --- 6. 计数 + 日志：只有条数与 session，没有正文；判定看不见它 ----------------


def test_the_hit_is_counted_and_logged_without_the_body() -> None:
    engine, _, _ = _engine()
    with _capture("qq_roleplay_bot.stage3_main") as records:
        assert _run(engine, REAL_ONE) is not None

    assert engine._stats["reply_tool_vocative_hints"] == 1
    assert "reply_tool_vocative_hints" in COUNTER_NAMES, "计数没进账本名单（跨重启会丢）"
    assert engine.snapshot().reply_tool_vocative_hints == 1

    lines = [record.getMessage() for record in records]
    matching = [line for line in lines if "stance" in line]
    assert len(matching) == 1, lines
    line = matching[0]
    assert SESSION in line, "日志里没有 session"
    assert "bot" not in line and "机器人" not in line, "日志里带了那个称呼"
    assert REAL_ONE not in line, "日志里带了正文"


def test_a_non_hit_counts_and_logs_nothing() -> None:
    engine, _, _ = _engine()
    with _capture("qq_roleplay_bot.stage3_main") as records:
        assert _run(engine, "这个 bot 挺好用") is not None

    assert engine._stats["reply_tool_vocative_hints"] == 0
    assert [line for line in (r.getMessage() for r in records) if "stance" in line] == []


def test_the_judge_never_sees_the_line() -> None:
    """用户口径"不要影响判定"：判定那条请求里一个字都不该有。"""

    engine, judge, _ = _engine()
    assert _run(engine, REAL_ONE) is not None
    judge_text = _material(judge.requests[0])
    assert TOOL_VOCATIVE_NOTE not in judge_text
    assert "不客气的称呼" not in judge_text
