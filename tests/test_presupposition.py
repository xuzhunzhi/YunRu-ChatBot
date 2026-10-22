"""「预设型编造」：她没说"我做过"，但把一件有经历门槛的事当成前提讲出来。

真实案例（用户报告，`run/data/logs/reply.jsonl` 2026-10-20 17:37:33 / seq=184）：

    六级这事，我妈当年也问。后来不问了，是我先不跟她提。

那一轮材料的 `input` 里「六级」出现 **0 次**。现有的「过往断言」那一关一个字都不命中：
它认的是「我/俺 + ……过」与「我/俺 + 当年」，这一句的主语是「**我妈**」；而且就算命中，
「六级这事」只有 4 个字，低于 `PAST_CLAIM_MIN_CHUNK = 6`，照样放行。

所以这一份钉三件事：

1. 判据本身（纯字符串）：回复里**同一句**有表里的词 + 锚点（我/我妈/当年/后来……），
    且材料里那个词 0 次 → 命中；材料里有 → 不命中；`四级杆` / `六级地震` / `六级风`
    这类**修饰专业名词**的不命中；**没有锚点**的泛泛议论（"六级挺难的"）也不命中；
2. 接线：命中后走既有那条打回通道、话术就是 `PAST_CLAIM_NOTE`（不新增文案）；
3. 边界：重写后仍不合格**照发**（回复不丢）；与 `has_past_claim` 同时命中时
   **一轮只重写一次**（共用额度）。
"""
import asyncio
import unittest

from qq_roleplay_bot.ask_when_unsure import (
    EXPERIENCE_GATE_WORDS,
    PAST_CLAIM_NOTE,
    _PRESUPPOSITION_ANCHORS,
    has_experiential_presupposition,
    has_past_claim,
)
from qq_roleplay_bot.stage3_main import DialogueEngine
from qq_roleplay_bot.transport import IncomingMessage, MessageTarget

#: 用户报告的真实原话（`reply.jsonl` 2026-10-20 17:37:33 / seq=184），一字不改。
REAL_PRESUPPOSITION = "六级这事，我妈当年也问。后来不问了，是我先不跟她提。"

#: 真实一例的材料：那一轮 `input` 里「六级」出现 0 次（这里只留语境，不抄整段）。
MATERIAL_WITHOUT_LEVEL_SIX = (
    '<message who="7" index="34843"><text>老师怎么还没来</text></message>\n'
    '<message who="12" index="34847"><text>看不懂…</text></message>'
)

#: 材料里**有**那个词（有人问"六级过了吗"）→ 不命中（有依据就该照说）。
MATERIAL_WITH_LEVEL_SIX = (
    '<message who="12" index="34860"><text>你六级过了吗</text></message>'
)

#: 歧义反例：词在修饰专业名词/自然现象，不是在讲她的经历。
AMBIGUOUS = (
    "四级杆是底线了",
    "四极杆得先清洗",
    "六级地震，房子都在晃",
    "今天六级风，别出门",
    "四级火警，整栋楼都撤了",
    "六级台阶，走上来有点喘",
    "六级楼梯，扶着点",
)

#: 同一批反例**带上锚点**：加了锚点这一条之后，上面那批没有锚点会"顺手通过"，
#: 于是歧义判据就没被真正测到。这里每条都带锚点，只有"修饰专业名词"那一条能挡住它们。
AMBIGUOUS_WITH_ANCHOR = (
    "我家那台四级杆是底线了",
    "我当年洗过四级杆",
    "我妈说今天六级风，别出门",
    "那时候六级地震，房子都在晃",
    "当时四级火警，整栋楼都撤了",
    "我以前爬过六级台阶",
    "我小时候数过六级楼梯",
)


def test_the_real_case_hits() -> None:
    """**真实案例必须命中**：回复里有「六级」，材料里 0 次。"""

    assert has_experiential_presupposition(REAL_PRESUPPOSITION, MATERIAL_WITHOUT_LEVEL_SIX) is True
    # 同时印证交接文件里那句"现有的关拦不住"：句式判据对这句返回 False。
    assert has_past_claim(REAL_PRESUPPOSITION) is False


def test_material_that_mentions_the_word_passes() -> None:
    """材料里**有**那个词 → 不命中（有人问"六级过了吗"，她顺着接就不算编）。"""

    assert has_experiential_presupposition(REAL_PRESUPPOSITION, MATERIAL_WITH_LEVEL_SIX) is False


def test_ambiguous_compounds_do_not_hit() -> None:
    """**歧义反例**：`四级杆`（质谱部件）、`六级地震`、`六级风` 这类一个都不许命中。

    这些词是名词的一部分，谁都能说，不构成"她经历过"的前提。
    """

    for text in AMBIGUOUS:
        assert has_experiential_presupposition(text, "") is False, text
    for text in AMBIGUOUS_WITH_ANCHOR:
        assert has_experiential_presupposition(text, "") is False, text


def test_a_clear_mention_next_to_an_ambiguous_one_still_hits() -> None:
    """同一句里一个歧义、一个不是：**只要有一个不歧义**就算她讲了那件事。"""

    assert has_experiential_presupposition("四级杆是底线了，六级这事我妈也问", "") is True


#: 方案一的核心反例：词在，但**同一句里没有锚点**——这是泛泛议论，不是在讲她的经历。
#: 用户 2026-10-20 的口径：命中词所在句还必须同时出现锚点，这样只留真的那几条。
NO_ANCHOR = (
    "六级挺难的",
    "六级成绩出来了",
    "公司都这样",
    "他四级没过",
    "大家都说高考难",
    "宿舍太吵了",
    "面试真够磨人的",
)


def test_a_word_without_an_anchor_does_not_hit() -> None:
    """没有锚点的顺口一提 → 不命中（第一版只看词时，这些全被误伤过）。"""

    for text in NO_ANCHOR:
        assert has_experiential_presupposition(text, "") is False, text


def test_the_anchor_must_be_in_the_same_sentence() -> None:
    """锚点在**另一句**不算：跨句的「我」和「六级」不是同一件事。"""

    assert has_experiential_presupposition("六级真难。后来我先睡了", "") is False
    assert has_experiential_presupposition("六级真难！我困了", "") is False


def test_every_anchor_in_the_table_is_live() -> None:
    """锚点表里每一个都真的生效（防止扩词时忘了接线）。"""

    for anchor in _PRESUPPOSITION_ANCHORS:
        assert has_experiential_presupposition(f"六级这事{anchor}也问过", ""), anchor


def test_every_word_in_the_table_is_live() -> None:
    """表里每个词都真的在判据里生效（防止加词时忘了接线）。"""

    for word in EXPERIENCE_GATE_WORDS:
        assert has_experiential_presupposition(f"{word}这事我早就想说了", ""), word


def test_none_and_empty_are_safe() -> None:
    assert has_experiential_presupposition(None, "") is False
    assert has_experiential_presupposition("", "") is False


# --- 接线（`stage3_main._model_path` 里那一关）-------------------------------

GROUP = "717151356"
TARGET = MessageTarget(group_id=GROUP)


def _msg(index, text="测试", *, mentioned=False, user_id="100"):
    return IncomingMessage(
        message_id=f"m{index}", session_id=f"group:{GROUP}", user_id=user_id,
        text=text, target=TARGET, sender_name="某人", is_bot_mentioned=mentioned,
    )


class ScriptedJudge:
    """脚本化的判定：一次一条输出（最后一条一直重复）。"""

    def __init__(self, *outputs) -> None:
        self.outputs = list(outputs) or ["<route>REPLY</route><topic>话题</topic>"]
        self.requests: list = []

    async def complete(self, request):
        self.requests.append(request)
        index = min(len(self.requests) - 1, len(self.outputs) - 1)
        return self.outputs[index]


class ScriptedReply:
    """脚本化的回复 agent：一次一条输出（最后一条一直重复）。"""

    def __init__(self, *texts, prefix="<decision>REPLY</decision><dialogue>KEEP</dialogue>") -> None:
        self.texts = list(texts) or ["我接一句。"]
        self.prefix = prefix
        self.requests: list = []

    async def complete(self, request):
        self.requests.append(request)
        index = min(len(self.requests) - 1, len(self.texts) - 1)
        return f"{self.prefix}<reply>{self.texts[index]}</reply>"


class FailingRewriteReply(ScriptedReply):
    """第一版正常吐；**第二次**调用（重写那一趟）直接抛——模拟重写失败。"""

    async def complete(self, request):
        if self.requests:
            self.requests.append(request)
            raise RuntimeError("重写那一趟挂了")
        return await super().complete(request)


def _engine(reply, **flag_values) -> DialogueEngine:
    """装一台引擎，并把运行期开关钉在这一台上（默认全开）。"""

    from qq_roleplay_bot.runtime_flags import RuntimeFlags

    engine = DialogueEngine(
        reply, judge_client=ScriptedJudge(),
        enabled_group_ids=frozenset({GROUP}),
    )
    flags = RuntimeFlags(**flag_values)
    engine._flags = lambda: flags
    return engine


HEDGED = "说不准，好像问过，我记不清了。"


def test_the_gate_rewrites_once_with_the_existing_wording() -> None:
    """命中 → 走既有重写通道，递的话**就是** `PAST_CLAIM_NOTE`（没有新文案）。"""

    reply = ScriptedReply(REAL_PRESUPPOSITION, HEDGED)
    engine = _engine(reply)

    result = asyncio.run(engine.handle(_msg(1, "@YunRu 我妈老问我成绩", mentioned=True)))

    assert result is not None and result.text == HEDGED, "发出的是重写的那一版"
    assert len(reply.requests) == 2, "没有打回重写（或者重写了不止一次）"
    feedback = reply.requests[1][-1]["content"]
    assert "你刚写的那一句要重来" in feedback
    assert "意思照旧" in feedback, "话术要求换说法，不是删掉"
    assert "换成没那么确定的说法" in feedback
    assert PAST_CLAIM_NOTE.strip() in feedback, "复用的应当是现成那条话术"
    # **放大器已拆**：她那一版原话不许贴回来。
    assert REAL_PRESUPPOSITION not in feedback
    # 常驻 system 一个字都没变。
    assert reply.requests[1][0]["content"] == reply.requests[0][0]["content"]
    snapshot = engine.snapshot()
    assert snapshot.reply_presupposition_rewrites == 1
    assert snapshot.reply_past_claim_rewrites == 0


def test_material_with_the_word_does_not_rewrite() -> None:
    """材料里有「六级」→ 不打回（有依据就该照说）。"""

    reply = ScriptedReply(REAL_PRESUPPOSITION)
    engine = _engine(reply)

    result = asyncio.run(engine.handle(_msg(1, "@YunRu 你六级过了吗", mentioned=True)))

    assert result is not None and result.text == REAL_PRESUPPOSITION
    assert len(reply.requests) == 1, "材料里明明有那个词，却被打回了"
    assert engine.snapshot().reply_presupposition_rewrites == 0


def test_the_gate_never_drops_the_reply() -> None:
    """**因这一关丢的回复 = 0**：重写仍不合格 → 照发；重写失败 → 发第一版。"""

    # ① 重写那一版还是同一句（仍不合格）→ 照发，不能变成空。
    reply = ScriptedReply(REAL_PRESUPPOSITION)
    engine = _engine(reply)
    result = asyncio.run(engine.handle(_msg(1, "@YunRu 我妈老问我成绩", mentioned=True)))
    assert result is not None and result.text == REAL_PRESUPPOSITION
    assert engine.snapshot().reply_presupposition_rewrites == 1
    assert engine.snapshot().replies == 1, "这一条必须真的发出去"

    # ② 重写那一趟抛异常 → 发第一版，绝不留空。
    reply = FailingRewriteReply(REAL_PRESUPPOSITION)
    engine = _engine(reply)
    result = asyncio.run(engine.handle(_msg(1, "@YunRu 我妈老问我成绩", mentioned=True)))
    assert result is not None and result.text == REAL_PRESUPPOSITION
    assert engine.snapshot().replies == 1


def test_both_gates_hit_but_only_one_rewrite_happens() -> None:
    """与 `has_past_claim` 同时命中 → **一轮只重写一次**（额度共用）。

    「我当年考六级，我妈也问」同时满足：形状二「我当年」+ 经历门槛词「六级」。
    打回只发生一次（回复 agent 共被调两次：第一版 + 一次重写），
    计数落在先说话的那一关（过往断言）上，预设那一关不再要第二次。
    """

    draft = "我当年考六级，我妈也问"
    assert has_past_claim(draft) is True
    assert has_experiential_presupposition(draft, MATERIAL_WITHOUT_LEVEL_SIX) is True

    reply = ScriptedReply(draft, HEDGED)
    engine = _engine(reply)
    result = asyncio.run(engine.handle(_msg(1, "@YunRu 我妈老问我成绩", mentioned=True)))

    assert result is not None and result.text == HEDGED
    assert len(reply.requests) == 2, "一轮里重写了不止一次"
    snapshot = engine.snapshot()
    assert snapshot.reply_past_claim_rewrites == 1
    assert snapshot.reply_presupposition_rewrites == 0, "额度共用了，这一关不该再记一次"


def test_the_gate_can_be_turned_off_by_the_same_switch() -> None:
    """面板关掉 `ask_when_unsure` → 这一条也不再生效。"""

    reply = ScriptedReply(REAL_PRESUPPOSITION)
    engine = _engine(reply, ask_when_unsure=False)

    result = asyncio.run(engine.handle(_msg(1, "@YunRu 我妈老问我成绩", mentioned=True)))

    assert result is not None and result.text == REAL_PRESUPPOSITION
    assert len(reply.requests) == 1, "关掉之后不该再打回"
    assert engine.snapshot().reply_presupposition_rewrites == 0


if __name__ == "__main__":  # pragma: no cover - 手动跑
    unittest.main()
