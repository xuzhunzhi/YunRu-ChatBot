"""「不懂就问」的守卫（2026-10-04 用户："问问题那个也可以上线了"）。

设计在 `docs/STAGE3_PENDING_DESIGNS.md` §①。要守住的东西有四类：

1. **没懂 + 被叫到 → 以"问"回应**（不是断言），而**没懂 + 没被叫到 → 不插话**；
2. **具体专业话题没有根据时不许主动断言**（知识库命中 / 记忆命中 / 语境明确，
   三样都没有）——它是**确定性**的，模型"觉得自己懂"绕不过去；
3. **问的次数有上限**（同一话题一次、一段时间内几次），否则她会每句都问；
4. **这条规矩不进常驻 system**（2026-10-05 更正）：它只在判定说"这一句你没跟上"的
   那一轮作为**现场提示**进易变段（`ASK_TURN_NOTE` / `HOLD_TURN_NOTE`）。
   （常驻 system 末尾现在拼的是**另一条禁令**——"有人当面谈这东西怎么做出来的，她不接"，
   见 `tests/test_stay_in_character.py`；那一条不是这段规矩。）

第 4 条是这次改出来的：`UNSURE_ASK_RULE` 从 2026-10-04 起被**无条件**拼进常驻
system（`compose_system_prompt`），system 从 **7061 → 7378 字**，多出来的正是那段
"他说的是一件**具体的事**——某个说法、某个行当里的规矩、某个数字……"。
用户报的"她的回复风格变得很奇怪、很 ai"里，这是被定位到的原因之一：
**那段措辞教她"分析话题"**（AGENTS §2.2：prompt 里的措辞会被角色吸收），
她的 `intent` 于是从"接住吐槽，顺口一句"变成"接话，给出判断"。
所以现在钉两件事：**system 里没有它**、**"关掉开关时与从前逐字相同"**。

不回归那条也要有：判定没给新信号（老版 prompt、解析失败）时，她必须跟从前一模一样。
"""
import asyncio
import os
from pathlib import Path
from unittest.mock import patch

from qq_roleplay_bot.ask_when_unsure import (
    ASK_TURN_NOTE,
    HOLD_TURN_NOTE,
    UNSURE_ASK_RULE,
    AskBudget,
    context_is_explicit,
    decide_grounding,
)
from qq_roleplay_bot.base_prompt import _load_base_prompt
from qq_roleplay_bot.dialogue_judge import (
    JUDGE_ROUTING_PROMPT,
    JUDGE_SYSTEM_PROMPT,
    JudgeVerdict,
    parse_judge_output,
)
from qq_roleplay_bot.extensions import KnowledgeItem, PromptSources
from qq_roleplay_bot.prompt_guard import PERSONA_MECHANISM_WORDS, scan_persona_text
from qq_roleplay_bot.stage3_main import DialogueEngine
from qq_roleplay_bot.stage3_runtime import (
    ContextState,
    ConversationMode,
    SYSTEM_PROMPT,
    SYSTEM_PROMPT_MUST_REPLY,
    build_dialogue_messages,
    compose_system_prompt,
)
from qq_roleplay_bot.stay_in_character import STAY_IN_CHARACTER_RULE
from qq_roleplay_bot.transport import IncomingMessage, MessageTarget

GROUP = "717151356"
TARGET = MessageTarget(group_id=GROUP)

ASK_MARKER = "别急着答"          # ASK_TURN_NOTE 里的一句
HOLD_MARKER = "别装懂、别下结论、别编细节"   # HOLD_TURN_NOTE 里的一句


def msg(index, text="测试", *, mentioned=False, user_id="100", reply_to_message_id=""):
    return IncomingMessage(
        message_id=f"m{index}", session_id=f"group:{GROUP}", user_id=user_id,
        text=text, target=TARGET, sender_name="某人",
        is_bot_mentioned=mentioned, reply_to_message_id=reply_to_message_id,
    )


class ScriptedJudge:
    """脚本化的判定：一次一条输出。"""

    def __init__(self, *outputs) -> None:
        self.outputs = list(outputs) or [
            "<route>REPLY</route><topic>话题</topic>"
        ]
        self.requests: list = []

    async def complete(self, request):
        self.requests.append(request)
        index = min(len(self.requests) - 1, len(self.outputs) - 1)
        return self.outputs[index]


class ScriptedReply:
    def __init__(self, text="我接一句。") -> None:
        self.output = (f"<decision>REPLY</decision><dialogue>KEEP</dialogue>"
                       f"<reply>{text}</reply>")
        self.requests: list = []

    async def complete(self, request):
        self.requests.append(request)
        return self.output


class FakeKnowledge:
    """一个总说"命中"的知识面（用来验"有根据时照旧答"）。"""

    def __init__(self, hit: bool) -> None:
        self.hit = hit

    async def search(self, query, *, limit):
        if not self.hit:
            return []
        return [KnowledgeItem(source="测试", title="资料", content="这是一条背景资料。")]


def _volatile(request) -> str:
    return request[-1]["content"]


# --- 判定：信号与默认值 -----------------------------------------------------


def test_judge_prompt_asks_for_the_two_signals() -> None:
    """两个信号由判定那趟"便宜调用"顺带给出——**不多花一次调用**。"""

    for prompt in (JUDGE_SYSTEM_PROMPT, JUDGE_ROUTING_PROMPT):
        assert "<understood>CLEAR|UNSURE|LOST</understood>" in prompt
        assert "<specialist>YES|NO</specialist>" in prompt
    assert "看明白" in JUDGE_SYSTEM_PROMPT


def test_missing_signals_keep_the_old_behaviour() -> None:
    """判定没给新标签（老版 prompt / 模型漏输出）时，一律按"懂、日常话题"处理。

    这条是**不回归的关键**：解析坏掉绝不能变成"她突然每句都问"。
    """

    for raw in ("<route>REPLY</route><topic>甲</topic>",
                "<route>REPLY</route><understood>???</understood><specialist>???</specialist>",
                "<route>NO_REPLY</route>"):
        verdict = parse_judge_output(raw)
        assert verdict.understood == "clear", raw
        assert verdict.specialist is False, raw
        assert verdict.unsure is False, raw
    unsure = parse_judge_output("<route>REPLY</route><understood>UNSURE</understood>")
    assert unsure.unsure is True


def test_the_written_rule_has_no_mechanism_words() -> None:
    """写进她 prompt 的这几段不许有机制词（AGENTS §2.2）。"""

    for text in (UNSURE_ASK_RULE, ASK_TURN_NOTE, HOLD_TURN_NOTE):
        assert scan_persona_text(text) == [], scan_persona_text(text)
        for word in PERSONA_MECHANISM_WORDS:
            assert word not in text, (word, text[:40])


def test_the_rule_forbids_faking_it_and_making_things_up() -> None:
    """三条禁令必须在（设计 §① 的第 3 条）：复述当懂了 / 说"我知道"/ **编细节**。

    最后一条是这套东西的要点：她"问"的时候也**不许编**——数字、流程、型号都不能凭空补，
    否则只是把"不懂装懂"换个地方犯。
    """

    assert "换个说法复述一遍当成懂了" in UNSURE_ASK_RULE
    assert "这个我知道" in UNSURE_ASK_RULE
    assert "不许编" in UNSURE_ASK_RULE
    for shape in ("数字", "步骤", "型号", "人名"):
        assert shape in UNSURE_ASK_RULE, shape
    # 现场提示里也要有"别编"那一句（她真正看的是这一轮拼出来的那一份）
    assert "别编细节" in ASK_TURN_NOTE
    assert "别编细节" in HOLD_TURN_NOTE


# --- 确定性结论（纯函数）----------------------------------------------------


def test_grounding_rules_are_deterministic() -> None:
    """把四条规矩摆成一张表：唯一允许沉默的是"没被叫到 + 没把握"。"""

    unsure = JudgeVerdict(should_reply=True, understood="lost", specialist=False)
    clear_specialist = JudgeVerdict(should_reply=True, understood="clear", specialist=True)
    clear_daily = JudgeVerdict(should_reply=True, understood="clear", specialist=False)
    base = dict(has_knowledge=False, has_memory=False, context_explicit=False)

    # 日常闲聊、她说得清 → 照旧（连提示都不拼）
    normal = decide_grounding(clear_daily, called=False, ask_allowed=True, **base)
    assert (normal.speak, normal.ask, normal.care) == (True, False, False)
    assert normal.turn_note == ""

    # 没被叫到 + 没把握 → 不插话（唯一的沉默）
    quiet = decide_grounding(unsure, called=False, ask_allowed=True, **base)
    assert quiet.speak is False and quiet.turn_note == ""

    # 被叫到 + 没把握 → **必须回应**，但以"问"回应（"被叫到没有否决权"的原意保留）
    asked = decide_grounding(unsure, called=True, ask_allowed=True, **base)
    assert (asked.speak, asked.ask) == (True, True)
    assert asked.turn_note == ASK_TURN_NOTE

    # 具体专业话题 + 三样根据都没有 + 没被叫到 → 不主动断言，也就不出声
    unsupported = decide_grounding(clear_specialist, called=False, ask_allowed=True, **base)
    assert unsupported.speak is False
    # ……被叫到时以问回应
    assert decide_grounding(clear_specialist, called=True, ask_allowed=True, **base).ask is True
    # ……**有**一处根据（这里是知识库命中）时照旧断言
    grounded = decide_grounding(clear_specialist, called=False, ask_allowed=True,
                                has_knowledge=True, has_memory=False, context_explicit=False)
    assert (grounded.speak, grounded.ask, grounded.care) == (True, False, False)

    # 问的次数用完 → 仍然必须回应（被叫到），但不许断言、只用自己的角度
    spent = decide_grounding(unsure, called=True, ask_allowed=False, **base)
    assert (spent.speak, spent.ask, spent.care) == (True, False, True)
    assert spent.turn_note == HOLD_TURN_NOTE


def test_context_is_explicit_only_accepts_narrow_clues() -> None:
    """「语境明确」宁可不明确：判不准就往"去问"那一边倒。"""

    current = msg(3, "这个综测到底是个啥")
    assert context_is_explicit(current, [current]) is False
    # 引用/回复了眼前某一条 → 有前文可依
    quoted = msg(4, "那这个呢", reply_to_message_id="m3")
    assert context_is_explicit(quoted, [current, quoted]) is True
    # 对方正在解释这件事
    assert context_is_explicit(msg(5, "综测就是综合测评的意思"), []) is True


def test_ask_budget_limits_per_topic_and_per_window() -> None:
    """同一话题最多一次；窗口内最多几次；窗口过去之后另一个话题还能问。"""

    budget = AskBudget(max_per_topic=1, max_per_window=3, window_seconds=600.0)
    assert budget.allows("group:1", "综测", 0.0) is True
    budget.note("group:1", "综测", 0.0)
    assert budget.allows("group:1", "综测", 1.0) is False, "同一话题最多一次"
    assert budget.allows("group:1", "别的 话题", 2.0) is True, "空白要归一"
    budget.note("group:1", "别的 话题", 2.0)
    budget.note("group:1", "第三个话题", 3.0)
    assert budget.allows("group:1", "第四个话题", 4.0) is False, "窗口内最多 3 次"
    assert budget.allows("group:2", "第四个话题", 4.0) is True, "窗口按会话算"
    assert budget.allows("group:1", "第四个话题", 601.0) is True, "窗口滑走之后又能问"
    # 关掉那条规矩时一次都不许问
    off = AskBudget(max_per_topic=0)
    assert off.allows("group:1", "综测", 0.0) is False


# --- 那条规矩**不在**常驻 system 里（2026-10-05）------------------------------

#: 撤掉那段规矩之后 `SYSTEM_PROMPT` 的**逐字长度**（本树跑的是模板人格）。
#: 生产上真人格那份是 **7061**（改之前是 7378 = 7061 + 317 字的规矩）。
#: 这是个"逐字"级别的钉子：**谁再往常驻 system 里加/减东西，这里都会红**。
#: 合理地改了人格或回话格式时，请一并更新这个数字（并说明为什么）。
SYSTEM_PROMPT_CHARS = 6187


def test_the_resident_system_prompt_carries_no_unsure_rule() -> None:
    """常驻 system（内置默认）里**没有**那段规矩，长度也回到撤掉它那一档。"""

    from qq_roleplay_bot.prompt_library import PromptLibrary

    assert UNSURE_ASK_RULE not in SYSTEM_PROMPT
    assert UNSURE_ASK_RULE not in SYSTEM_PROMPT_MUST_REPLY
    # 面板"查看当前 prompt"看到的就是这一份（内置默认）——两处必须一致。
    assert PromptLibrary.builtin("reply") == SYSTEM_PROMPT
    assert UNSURE_ASK_RULE not in PromptLibrary.builtin("reply")
    assert len(SYSTEM_PROMPT) == SYSTEM_PROMPT_CHARS, (
        f"常驻 system 的长度变了（{len(SYSTEM_PROMPT)} ≠ {SYSTEM_PROMPT_CHARS}）："
        "是不是又把某段规矩拼回了常驻 system？")


def test_the_rule_is_not_in_the_system_prompt_even_with_a_replaced_persona() -> None:
    """**把 BASE_PROMPT 换成一份极简假人格，常驻 system 里也不许出现这条规矩**。

    生产上人格是整段替换的（`data/private_docs/base_prompt.REAL.py`，找到就整段返回，
    见 `base_prompt.py:249-251`），所以这条走那条真路：用 `QQBOT_BASE_PROMPT_FILE`
    指一份假人格 → 读出来 → 组装请求 → 规矩**不在**、人格**在**。
    """

    import tempfile

    fake = "你是一个住在群聊里的角色。这一份是测试用的极简人格，里面没有任何规矩。"
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "base_prompt.FAKE.py"
        path.write_text(f'BASE_PROMPT = """{fake}"""\n', encoding="utf-8")
        with patch.dict(os.environ, {"QQBOT_BASE_PROMPT_FILE": str(path)}):
            persona = _load_base_prompt()
        assert persona.strip() == fake, "假人格没被读进来，这条测试就没意义了"

        # 1) 装配那一层只拼**代码层的禁令**（2026-10-05 起是 `STAY_IN_CHARACTER_RULE`）：
        #    「不懂就问」那段一个字都不许从这里进来。
        assert compose_system_prompt(persona).endswith(STAY_IN_CHARACTER_RULE)
        assert compose_system_prompt(persona) == persona + STAY_IN_CHARACTER_RULE
        assert UNSURE_ASK_RULE not in compose_system_prompt(persona)
        # 2) **生产的组装路径**：面板/人格给回来的 system 文本是"裸人格"，
        #    组装出来的请求里也不许多出这段规矩，而人格本身要一字不少（禁令拼在它后面）。
        current = msg(1, "随便聊点什么")
        with patch("qq_roleplay_bot.stage3_runtime.resolve_system_prompt",
                   lambda **kwargs: persona):
            request = build_dialogue_messages(
                [current], current=current, mode=ConversationMode.IDLE,
                trigger="threshold", context=ContextState(topic="闲聊"),
            )
        assert UNSURE_ASK_RULE not in request[0]["content"]
        assert request[0]["content"] == persona + STAY_IN_CHARACTER_RULE, "人格本身不能被改动"
        # 3) 现场提示那一路仍在（它才是这条规矩现在的去处）
        assert "别急着答" in ASK_TURN_NOTE


def test_a_panel_prompt_is_not_edited_by_the_assembly_step() -> None:
    """面板保存的覆盖版同样**只多出那一条常驻禁令**：装配那一步不改操作者写的字。"""

    current = msg(1, "随便聊点什么")
    panel = "面板改过的一份 prompt，里面没有不懂就问这条。"
    with patch("qq_roleplay_bot.stage3_runtime.resolve_system_prompt",
               lambda **kwargs: panel):
        request = build_dialogue_messages(
            [current], current=current, mode=ConversationMode.IDLE,
            trigger="threshold", context=ContextState(topic="闲聊"),
        )
    assert request[0]["content"] == panel + STAY_IN_CHARACTER_RULE
    assert UNSURE_ASK_RULE not in request[0]["content"]
    # 装配层不替操作者改已存的文本：面板那份里若已经带了旧版留下的那段规矩，
    # 它既不会被删掉、也不会被再拼一遍（幂等）——那种遗留要不要清，由面板上
    # "恢复默认/重存"决定。常驻禁令那一小段同理，已有的不重复拼。
    assert compose_system_prompt(panel + UNSURE_ASK_RULE) == (
        panel + UNSURE_ASK_RULE + STAY_IN_CHARACTER_RULE)
    assert compose_system_prompt(panel + STAY_IN_CHARACTER_RULE) == panel + STAY_IN_CHARACTER_RULE


# --- 引擎路径（判定 → 路由 → prompt）----------------------------------------


def test_unsure_and_called_asks_instead_of_asserting() -> None:
    judge = ScriptedJudge(
        "<route>REPLY</route><topic>综测</topic>"
        "<understood>LOST</understood><specialist>YES</specialist>"
    )
    reply = ScriptedReply("综测？你先说说你那个是啥。")
    engine = DialogueEngine(reply, judge_client=judge)

    result = asyncio.run(engine.handle(msg(1, "@YunRu 综测是啥", mentioned=True)))

    assert result is not None, "被叫到时必须回应"
    assert len(reply.requests) == 1
    note = _volatile(reply.requests[0])
    assert ASK_MARKER in note, "被叫到但没懂时，这一轮要走'问'"
    assert HOLD_MARKER not in note
    # 规矩的措辞只在**这一轮的易变段**里，常驻 system 里没有它（2026-10-05）
    assert UNSURE_ASK_RULE not in reply.requests[0][0]["content"], "规矩不该常驻 system"
    assert engine.snapshot().clarify_asked == 1


def test_specialist_topic_without_grounds_does_not_assert() -> None:
    """**具体的专业话题 + 三样根据都没有** → 也不许断言（她"觉得自己懂"不算数）。"""

    judge = ScriptedJudge(
        "<route>REPLY</route><topic>电赛</topic>"
        "<understood>CLEAR</understood><specialist>YES</specialist>"
    )
    reply = ScriptedReply("电赛？你说的是哪个？")
    engine = DialogueEngine(reply, judge_client=judge)

    result = asyncio.run(engine.handle(msg(1, "@YunRu 电赛报名截止了吗", mentioned=True)))

    assert result is not None
    assert ASK_MARKER in _volatile(reply.requests[0])
    assert engine.snapshot().clarify_asked == 1


def test_grounds_present_keeps_the_normal_answer() -> None:
    """**有**一处根据时照旧断言：不许把"不懂就问"变成"什么都不敢答"。"""

    judge = ScriptedJudge(
        "<route>REPLY</route><topic>电赛</topic>"
        "<understood>CLEAR</understood><specialist>YES</specialist>"
    )
    reply = ScriptedReply("我看过那份通知。")
    engine = DialogueEngine(reply, judge_client=judge,
                            prompt_sources=PromptSources(knowledge_base=FakeKnowledge(True)))

    result = asyncio.run(engine.handle(msg(1, "@YunRu 电赛报名截止了吗", mentioned=True)))

    assert result is not None
    note = _volatile(reply.requests[0])
    assert ASK_MARKER not in note and HOLD_MARKER not in note
    assert engine.snapshot().clarify_asked == 0


def test_unsure_without_being_called_stays_quiet() -> None:
    """**没被叫到 + 没懂 → 不插话**（她不该为了刷存在感硬接一句）。

    这条规则在引擎里有**两道**，所以两种都测：
    * 没懂 → 在取资料之前就返回（便宜的那道）；
    * 具体专业话题没有根据 → 要等资料取完才知道（贵的那道，见第三个消息）。
    """

    judge = ScriptedJudge(
        "<route>REPLY</route><topic>你好</topic><understood>CLEAR</understood>",
        "<route>REPLY</route><topic>综测</topic>"
        "<understood>UNSURE</understood><specialist>YES</specialist>",
        "<route>REPLY</route><topic>电赛</topic>"
        "<understood>CLEAR</understood><specialist>YES</specialist>",
    )
    reply = ScriptedReply("在。")
    engine = DialogueEngine(reply, judge_client=judge, group_listen=True)

    # 第一句 @ 她：正常接（同时把会话带进"正在交谈"）
    assert asyncio.run(engine.handle(msg(1, "@YunRu 在吗", mentioned=True))) is not None
    assert len(reply.requests) == 1

    # 第二句是别人在群里自说自话，没人叫她 → 判定虽然说 REPLY，她也不许插话
    quiet = asyncio.run(engine.handle(msg(2, "综测那个事我搞定了", user_id="200")))
    assert quiet is None, "没被叫到又没把握时，不许出声"

    # 第三句同上，但这回她"觉得自己懂"——**没有根据的具体专业话题照样不许主动接**
    quiet_again = asyncio.run(engine.handle(msg(3, "电赛的板子我买好了", user_id="200")))
    assert quiet_again is None, "没有根据的具体话题，不许主动断言"

    assert len(reply.requests) == 1, "后两轮都不该再调回复"
    assert len(judge.requests) == 3, "判定照样跑（它是便宜的，也是话题锚点的来源）"
    assert engine.snapshot().clarify_quiet == 2
    assert engine.snapshot().clarify_asked == 0


def test_asking_is_rate_limited() -> None:
    """同一话题最多问一次：第二次还问就是唠叨，改成只说她自己的角度。"""

    judge = ScriptedJudge(
        "<route>REPLY</route><topic>综测</topic>"
        "<understood>LOST</understood><specialist>YES</specialist>"
    )
    reply = ScriptedReply("综测？你说的是啥？")
    engine = DialogueEngine(reply, judge_client=judge)

    first = asyncio.run(engine.handle(msg(1, "@YunRu 综测是啥", mentioned=True)))
    second = asyncio.run(engine.handle(msg(2, "@YunRu 那综测呢", mentioned=True)))

    assert first is not None and second is not None, "被叫到两次都要回应"
    assert ASK_MARKER in _volatile(reply.requests[0])
    assert ASK_MARKER not in _volatile(reply.requests[1]), "同一话题不许问第二遍"
    assert HOLD_MARKER in _volatile(reply.requests[1])
    snapshot = engine.snapshot()
    assert snapshot.clarify_asked == 1
    assert snapshot.clarify_quiet == 0


def test_a_normal_topic_still_answers_as_before() -> None:
    """**不回归**：判定没给新信号（老版 prompt）时，正常话题照旧回答。"""

    judge = ScriptedJudge("<route>REPLY</route><from>none</from><topic>话题</topic>")
    reply = ScriptedReply("我接一句。")
    engine = DialogueEngine(reply, judge_client=judge)

    result = asyncio.run(engine.handle(msg(1, "@YunRu 在吗", mentioned=True)))

    assert result is not None and result.text == "我接一句。"
    note = _volatile(reply.requests[0])
    assert ASK_MARKER not in note and HOLD_MARKER not in note
    snapshot = engine.snapshot()
    assert snapshot.clarify_asked == 0 and snapshot.clarify_quiet == 0


def test_the_rule_can_be_turned_off() -> None:
    """面板关掉它 → 退回从前：被叫到就直接答，问不问不再由核心定。

    这里改的是**这一台引擎自己的替身开关**（`_default_flags`），不 install 到进程上——
    否则这条测试会污染同进程里别的测试的开关状态。
    """

    from qq_roleplay_bot.runtime_flags import RuntimeFlags

    judge = ScriptedJudge(
        "<route>REPLY</route><topic>综测</topic>"
        "<understood>LOST</understood><specialist>YES</specialist>"
    )
    reply = ScriptedReply("我接一句。")
    engine = DialogueEngine(reply, judge_client=judge)
    engine._default_flags = RuntimeFlags(ask_when_unsure=False)

    result = asyncio.run(engine.handle(msg(1, "@YunRu 综测是啥", mentioned=True)))

    assert result is not None
    assert ASK_MARKER not in _volatile(reply.requests[0])
    assert engine.snapshot().clarify_asked == 0
