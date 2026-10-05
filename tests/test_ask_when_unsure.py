"""「没把握就别断言」的守卫（2026-10-04 起叫"不懂就问"，2026-10-05 晚改口径）。

用户原话：*"别做不懂就问"* · *"先改掉**不懂装懂硬插话**"*
→ **不要**做"没懂就问她一句"（那是上一版，被否掉了）；
要的是"**没根据的时候，要么不出声，要么别断言**"。

设计在 `docs/STAGE3_PENDING_DESIGNS.md` §①（那份写的是旧口径，2026-10-05 的更正见本
文件与那份文档末尾的「第三次改动」）。要守住的东西有五类：

1. **没被叫到 + 没懂 + 具体话题 → 不出声**（`decide_grounding`，确定性）；
2. **被叫到 + 没懂 + 话里有具体断言 + 知识库/记忆/语境三处都没有根据 → 打回重写一次**
   （`stage3_main._model_path` 里那一关；重写复用"审核只打回、同一轮重写一次"那条路）；
3. **只有她自己的角度、没有具体断言 → 放行**（不许把正常聊天掐死）；
4. **有根据（知识库/记忆/语境命中）→ 一个字都不拦**；
5. **这条规矩不进常驻 system**（2026-10-05 的教训）：要么由核心确定性拦，要么只在
   那一轮的易变段出现。`UNSURE_ASK_RULE` / `ASK_TURN_NOTE` / `HOLD_TURN_NOTE`
   三份旧措辞仍然留在 `ask_when_unsure.py` 里当历史与文字来源，但**没有任何调用点**。

第 5 条是 2026-10-05 白天改出来的：`UNSURE_ASK_RULE` 从 2026-10-04 起被**无条件**拼进
常驻 system（`compose_system_prompt`），system 从 **7061 → 7378 字**，多出来的正是那段
"他说的是一件**具体的事**——某个说法、某个行当里的规矩、某个数字……"。
用户报的"她的回复风格变得很奇怪、很 ai"里，这是被定位到的原因之一：
**那段措辞教她"分析话题"**（AGENTS §2.2：prompt 里的措辞会被角色吸收）。
所以现在钉三件事：**system 里没有它**、**引擎这一侧不再给她任何现场提示**、
**关掉开关时与从前逐字相同**。

不回归那条也要有：判定没给新信号（老版 prompt、解析失败）时，她必须跟从前一模一样。
"""
import asyncio
import os
from pathlib import Path
from unittest.mock import patch

from qq_roleplay_bot.ask_when_unsure import (
    ASK_TURN_NOTE,
    HOLD_TURN_NOTE,
    NO_ASSERT_NOTE,
    UNSURE_ASK_RULE,
    AskBudget,
    context_is_explicit,
    decide_grounding,
    has_specific_claim,
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
    resolve_system_prompt,
)
from qq_roleplay_bot.stay_in_character import STAY_IN_CHARACTER_RULE
from qq_roleplay_bot.transport import IncomingMessage, MessageTarget

GROUP = "717151356"
TARGET = MessageTarget(group_id=GROUP)

#: 旧版"以问回应"那两份现场提示里的一句话。**现在它们一个字都不该出现在请求里**：
#: "问"这条路已经拆掉（`clarify_note` 恒为空串）。
ASK_MARKER = "别急着答"
HOLD_MARKER = "别装懂、别下结论、别编细节"
#: 新那一条（打回重写时递的那句话）里的一句。
NO_ASSERT_MARKER = "别断言你不知道的"


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
    """脚本化的回复 agent：一次一条输出（最后一条会一直重复）。"""

    def __init__(self, *texts, prefix="<decision>REPLY</decision><dialogue>KEEP</dialogue>") -> None:
        self.texts = list(texts) or ["我接一句。"]
        self.prefix = prefix
        self.requests: list = []

    async def complete(self, request):
        self.requests.append(request)
        index = min(len(self.requests) - 1, len(self.texts) - 1)
        return f"{self.prefix}<reply>{self.texts[index]}</reply>"


class ScriptedReviewer:
    """脚本化的风格审核：`(passed, reason)` 一次一条，最后一对一直重复。"""

    def __init__(self, *results) -> None:
        self.results = list(results) or [(True, "")]
        self.calls: list = []

    async def review(self, draft, *, context=""):
        self.calls.append(draft)
        index = min(len(self.calls) - 1, len(self.results) - 1)
        return self.results[index]

    def snapshot(self) -> dict[str, int]:
        return {"calls": len(self.calls)}


class FakeKnowledge:
    """一个总说"命中"的知识面（用来验"有根据时照旧答"）。"""

    def __init__(self, hit: bool) -> None:
        self.hit = hit

    async def search(self, query, *, limit):
        if not self.hit:
            return []
        return [KnowledgeItem(source="测试", title="资料", content="这是一条背景资料。")]


class _NoIdentity:
    """`identity_for` 的返回值：她对这个人的称呼与边界（这里一律空）。"""

    def as_judge_note(self, *args, **kwargs) -> str:
        return ""


class FakeMemory:
    """一份总说"有记录"的记忆面（三种根据里的第二种）。

    只要引擎在这条路上会用到的几个方法：`capture`（入口就调）、`retrieve`（第二种根据）、
    `profile_for` / `care_note` / `mark_care_noted`。别的（写回、维护）这条路不碰。
    """

    def __init__(self, hit: bool) -> None:
        self.hit = hit

    def capture(self, message) -> None:
        return None

    def snapshot(self) -> dict[str, object]:
        return {}

    async def retrieve(self, message, topic, *, mentioned=()):
        from qq_roleplay_bot.memory_model import MemoryMaterial, MemoryRecord

        if not self.hit:
            return MemoryMaterial()
        record = MemoryRecord(
            id="r1", scope_type="user_group", scope_key=f"{GROUP}:{message.user_id}",
            subject_user_id=message.user_id, kind="fact", normalized_key="电赛",
            content="他提过这件事。", confidence=0.9, created_at=1.0, updated_at=1.0,
        )
        return MemoryMaterial(records=(record,))

    async def profile_for(self, message):
        return ""

    async def identity_for(self, message):
        return _NoIdentity()

    async def care_note(self, message, *, topic_shifted, current_terms=()):
        return None

    async def mark_care_noted(self, record_id):
        return None


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

    这条是**不回归的关键**：解析坏掉绝不能变成"她突然开始拦自己"。
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

    for text in (UNSURE_ASK_RULE, ASK_TURN_NOTE, HOLD_TURN_NOTE, NO_ASSERT_NOTE):
        assert scan_persona_text(text) == [], scan_persona_text(text)
        for word in PERSONA_MECHANISM_WORDS:
            assert word not in text, (word, text[:40])


def test_the_rule_forbids_faking_it_and_making_things_up() -> None:
    """三条禁令必须在（设计 §① 的第 3 条）：复述当懂了 / 说"我知道"/ **编细节**。

    最后一条是这套东西的要点：她**不许编**——数字、流程、型号都不能凭空补，
    否则只是把"不懂装懂"换个地方犯。
    """

    assert "换个说法复述一遍当成懂了" in UNSURE_ASK_RULE
    assert "这个我知道" in UNSURE_ASK_RULE
    assert "不许编" in UNSURE_ASK_RULE
    for shape in ("数字", "步骤", "型号", "人名"):
        assert shape in UNSURE_ASK_RULE, shape
    # **真正会递到她眼前的那一句**（打回重写时）也要有"别编"那一层意思，
    # 而且要**明确否掉"问她一句"**——那条路用户否掉了。
    assert NO_ASSERT_MARKER in NO_ASSERT_NOTE
    assert "不清楚" in NO_ASSERT_NOTE
    assert "别拿反问绕过去" in NO_ASSERT_NOTE


# --- 确定性结论（纯函数）----------------------------------------------------


def test_grounding_rules_are_deterministic() -> None:
    """把规矩摆成一张表：唯一允许沉默的是"没被叫到、又没把握也没根据"。"""

    unsure_specialist = JudgeVerdict(should_reply=True, understood="lost", specialist=True)
    clear_specialist = JudgeVerdict(should_reply=True, understood="clear", specialist=True)
    clear_daily = JudgeVerdict(should_reply=True, understood="clear", specialist=False)
    base = dict(has_knowledge=False, has_memory=False, context_explicit=False)

    # 日常闲聊、她说得清 → 照旧（连"要当心"那一层都不开）
    normal = decide_grounding(clear_daily, called=False, **base)
    assert (normal.speak, normal.rewrite) == (True, False)

    # 没被叫到 + 没懂 + 具体话题 → **不插话**（唯一的沉默）
    quiet = decide_grounding(unsure_specialist, called=False, **base)
    assert quiet.speak is False and quiet.rewrite is False
    assert quiet.unclear is True and quiet.grounded is False

    # 被叫到 + 没懂 → **必须回应**（"被叫到没有否决权"的原意保留），
    # 但之后要看一眼草稿：她一断言就打回重写一次。
    called = decide_grounding(unsure_specialist, called=True, **base)
    assert (called.speak, called.rewrite) == (True, True)

    # 具体专业话题 + 三样根据都没有 + 没被叫到 → 不出声；被叫到 → 开口 + 盯草稿
    unsupported = decide_grounding(clear_specialist, called=False, **base)
    assert unsupported.speak is False
    assert decide_grounding(clear_specialist, called=True, **base).rewrite is True
    # 「没懂」这一档**在取资料之前**就已经决定了（引擎里的第一道，见
    # `test_quiet_when_not_called_unsure_and_specialist`）——那时候还不知道有没有根据，
    # 而"没懂"要花的钱是白花的（这一轮根本不出声），所以不为了它去翻资料。
    # 这里只用"具体话题"那半边把**三样根据**逐个钉一遍：有根据 = 一个字都不拦。
    for grounds in (dict(has_knowledge=True, has_memory=False, context_explicit=False),
                    dict(has_knowledge=False, has_memory=True, context_explicit=False),
                    dict(has_knowledge=False, has_memory=False, context_explicit=True)):
        verdict = clear_specialist
        assert decide_grounding(verdict, called=False, **grounds).speak is True
        assert decide_grounding(verdict, called=True, **grounds).rewrite is False
    # 有根据时连"没懂"也不打回：手里有材料就照说，别把正常回答掐掉
    assert decide_grounding(unsure_specialist, called=True, has_knowledge=True,
                            has_memory=False, context_explicit=False).rewrite is False


def test_specific_claim_detector_only_catches_specific_claims() -> None:
    """「话里有没有具体断言」**只认具体的东西**：数字 / 型号 / 流程。

    判错的那一侧是"少打回一次"，不是"凭空打回正常聊天"——所以这几条边界都要钉住：
    承认不知道的、反问的、只说感受的，一律不算断言。
    """

    # 具体断言（该打回的那一类）
    for text in ("综测报名截止到 15 号了，你去教务系统看看",
                 "电赛的板子用 TI 的 F28379D，先报名再交材料",
                 "谷歌手机那个是 Tensor G4 芯片",
                 "一共三步：先报名，然后交材料，最后答辩"):
        assert has_specific_claim(text) is True, text
    # 不是断言（该放行的那一类）
    for text in ("这个我没跟上，你说的是哪个",
                 "行，等你",
                 "我今天有点累，说不动了",
                 "那个型号我也记不清了，可能是 A1234 吧",
                 "你说的是哪个电赛？",
                 "在吗"):
        assert has_specific_claim(text) is False, text
    assert has_specific_claim("") is False
    assert has_specific_claim(None) is False


def test_context_is_explicit_only_accepts_narrow_clues() -> None:
    """「语境明确」宁可不明确：判不准就往"没有根据"那一边倒。"""

    current = msg(3, "这个综测到底是个啥")
    assert context_is_explicit(current, [current]) is False
    # 引用/回复了眼前某一条 → 有前文可依
    quoted = msg(4, "那这个呢", reply_to_message_id="m3")
    assert context_is_explicit(quoted, [current, quoted]) is True
    # 对方正在解释这件事
    assert context_is_explicit(msg(5, "综测就是综合测评的意思"), []) is True


def test_ask_budget_limits_per_topic_and_per_window() -> None:
    """`AskBudget` 仍然是个能用的工具类（它现在**没有消费者**，但语义不许漂）。

    它的来历是旧版"以问回应"：怕她每句都问。那条路拆掉之后，兜底是"重写一次"，
    上限由那一次重写本身保证，所以引擎不再问它——但它自己的语义照旧钉住。
    """

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
    """常驻 system（内置默认）里**没有**那段规矩（新旧两份都不许有），长度也钉住。"""

    from qq_roleplay_bot.prompt_library import PromptLibrary

    assert UNSURE_ASK_RULE not in SYSTEM_PROMPT
    assert UNSURE_ASK_RULE not in SYSTEM_PROMPT_MUST_REPLY
    # 新版那句"别断言你不知道的"同样不许常驻
    assert NO_ASSERT_NOTE not in SYSTEM_PROMPT
    assert NO_ASSERT_NOTE not in SYSTEM_PROMPT_MUST_REPLY
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
        assert NO_ASSERT_NOTE not in compose_system_prompt(persona)
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
        # 3) 旧版那两份现场提示还在文件里（当历史与文字来源），但引擎不再拼它们
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


# --- 引擎路径（判定 → 路由 → 打回重写）---------------------------------------


def test_quiet_when_not_called_unsure_and_specialist() -> None:
    """**验收 1**：没被叫到 + 没懂 + 专业话题 → **不出声**（断言不产生回复）。

    这里是"没懂"那一档（在取资料**之前**就返回，也不白花钱），
    "具体话题没有根据"那一档见下面那条。
    """

    judge = ScriptedJudge(
        # 第一句 `@` 她：正常接（顺手把这个群带成"当值"，这是焦点门的需要，不是这条规则）
        "<route>REPLY</route><topic>你好</topic><understood>CLEAR</understood>",
        "<route>REPLY</route><topic>综测</topic>"
        "<understood>UNSURE</understood><specialist>YES</specialist>",
    )
    reply = ScriptedReply("在。", "综测我懂，不就是那个吗。")
    engine = DialogueEngine(reply, judge_client=judge, group_listen=True)

    assert asyncio.run(engine.handle(msg(1, "@YunRu 在吗", mentioned=True))) is not None
    result = asyncio.run(engine.handle(msg(2, "综测那个事我搞定了", user_id="200")))

    assert result is None, "没被叫到又没懂时，不许出声"
    assert len(reply.requests) == 1, "没出声那一轮不该调回复 agent"
    assert engine.snapshot().clarify_quiet == 1


def test_specialist_topic_without_grounds_does_not_interrupt() -> None:
    """**验收 1（第二种）**：她"觉得自己懂"（CLEAR），但具体话题 + 三处都没有根据
    + 没被叫到 → 照样**不出声**（这一档要等资料取完才知道）。"""

    judge = ScriptedJudge(
        "<route>REPLY</route><topic>你好</topic><understood>CLEAR</understood>",
        "<route>REPLY</route><topic>电赛</topic>"
        "<understood>CLEAR</understood><specialist>YES</specialist>",
    )
    reply = ScriptedReply("在。", "电赛的板子我买好了。")
    engine = DialogueEngine(reply, judge_client=judge, group_listen=True)

    assert asyncio.run(engine.handle(msg(1, "@YunRu 在吗", mentioned=True))) is not None
    result = asyncio.run(engine.handle(msg(2, "电赛的板子我买好了", user_id="200")))

    assert result is None, "没有根据的具体话题，不许主动断言（那就别出声）"
    assert len(reply.requests) == 1
    assert engine.snapshot().clarify_quiet == 1
    assert engine.snapshot().reply_grounding_rewrites == 0


def test_called_unsure_with_assertion_is_pushed_back_and_rewritten() -> None:
    """**验收 2**：被叫到 + 没懂 + 无根据 + 话里有具体断言 → **打回并重写一次**。

    这是这一版的**兜底**（上一版缺的就是它）：确定性拦，不靠她自觉。
    """

    judge = ScriptedJudge(
        "<route>REPLY</route><topic>综测</topic>"
        "<understood>LOST</understood><specialist>YES</specialist>"
    )
    reply = ScriptedReply(
        "综测报名截止到 15 号，你去教务系统提交就行。",   # 第一版：具体断言，没有根据
        "这个我真不清楚，你那边是哪个学校的规定？",       # 重写版：承认不知道
    )
    engine = DialogueEngine(reply, judge_client=judge)

    result = asyncio.run(engine.handle(msg(1, "@YunRu 综测报名截止了吗", mentioned=True)))

    assert result is not None, "被叫到时必须回应（沉默只留给没被叫到）"
    assert len(reply.requests) == 2, "打回之后要在同一轮里重写一次（只一次）"
    # 重写那一趟带着"别断言你不知道的"那句话；**不是**"问她一句"
    assert NO_ASSERT_MARKER in _volatile(reply.requests[1])
    assert "别拿反问绕过去" in _volatile(reply.requests[1])
    assert result.text == "这个我真不清楚，你那边是哪个学校的规定？", "发出的是重写的那一版"
    # **这一层一个字都没进常驻 system**：前后两次 system 逐字相同
    assert reply.requests[0][0]["content"] == reply.requests[1][0]["content"]
    assert NO_ASSERT_MARKER not in reply.requests[1][0]["content"]
    assert UNSURE_ASK_RULE not in reply.requests[1][0]["content"]
    snapshot = engine.snapshot()
    assert snapshot.reply_grounding_rewrites == 1
    assert snapshot.clarify_quiet == 0
    # **旧版那两句现场提示一个都不许出现**（"问"这条路已经拆掉）
    assert ASK_MARKER not in _volatile(reply.requests[0])
    assert HOLD_MARKER not in _volatile(reply.requests[0])


def test_called_unsure_without_assertion_is_let_through() -> None:
    """**验收 3**：被叫到 + 没懂 + 无根据，但她**只说自己的角度、不断言** → **放行**。

    别把人掐死：她说"这个我没跟上"本来就是对的行为，不该再打回一次。
    """

    judge = ScriptedJudge(
        "<route>REPLY</route><topic>综测</topic>"
        "<understood>LOST</understood><specialist>YES</specialist>"
    )
    reply = ScriptedReply("这个我没跟上，不敢乱说。")
    engine = DialogueEngine(reply, judge_client=judge)

    result = asyncio.run(engine.handle(msg(1, "@YunRu 综测是啥", mentioned=True)))

    assert result is not None
    assert len(reply.requests) == 1, "没有具体断言就不许打回"
    assert result.text == "这个我没跟上，不敢乱说。"
    assert engine.snapshot().reply_grounding_rewrites == 0


def test_each_of_the_three_grounds_keeps_the_normal_answer() -> None:
    """**验收 4**：有根据（知识库 / 记忆 / 语境命中）→ **不触发任何拦截**。

    三种根据各测一次，三次都必须是"照旧答、不打回"。
    """

    judge_output = (
        "<route>REPLY</route><topic>电赛</topic>"
        "<understood>LOST</understood><specialist>YES</specialist>"
    )
    assertion = "电赛报名截止到 15 号，你去教务系统提交就行。"

    # ① 知识库命中
    reply = ScriptedReply(assertion)
    engine = DialogueEngine(reply, judge_client=ScriptedJudge(judge_output),
                            prompt_sources=PromptSources(knowledge_base=FakeKnowledge(True)))
    result = asyncio.run(engine.handle(msg(1, "@YunRu 电赛报名截止了吗", mentioned=True)))
    assert result is not None and result.text == assertion
    assert len(reply.requests) == 1
    assert engine.snapshot().reply_grounding_rewrites == 0

    # ② 记忆命中
    reply = ScriptedReply(assertion)
    engine = DialogueEngine(reply, judge_client=ScriptedJudge(judge_output),
                            memory_service=FakeMemory(True))
    result = asyncio.run(engine.handle(msg(1, "@YunRu 电赛报名截止了吗", mentioned=True)))
    assert result is not None and result.text == assertion
    assert len(reply.requests) == 1
    assert engine.snapshot().reply_grounding_rewrites == 0

    # ③ 语境明确（对方正在解释这件事——"……的意思是……"这一种，判据只认这几种写法）
    reply = ScriptedReply(assertion)
    engine = DialogueEngine(reply, judge_client=ScriptedJudge(judge_output))
    result = asyncio.run(engine.handle(
        msg(1, "@YunRu 电赛的意思是电子设计竞赛，报名截止了吗", mentioned=True)))
    assert result is not None and result.text == assertion
    assert len(reply.requests) == 1
    assert engine.snapshot().reply_grounding_rewrites == 0


def test_the_pushed_back_version_keeps_the_system_prompt_untouched() -> None:
    """**验收 5**：打回重写时，她的**常驻 system 一个字都没变**，新规矩只在这一轮的
    易变段（`_revision_request` 追加的那条 user 消息）里出现。"""

    judge = ScriptedJudge(
        "<route>REPLY</route><topic>谷歌手机</topic>"
        "<understood>LOST</understood><specialist>YES</specialist>"
    )
    reply = ScriptedReply("谷歌手机是 Tensor G4 芯片。", "这个型号我说不准。")
    engine = DialogueEngine(reply, judge_client=judge)

    asyncio.run(engine.handle(msg(1, "@YunRu 谷歌手机用的什么芯片", mentioned=True)))

    assert len(reply.requests) == 2
    first, second = reply.requests
    assert first[0]["role"] == "system" and second[0]["role"] == "system"
    assert first[0]["content"] == second[0]["content"], "system 段必须逐字相同"
    # 常驻 system 那一份**逐字**是"这一台机器上的人格 + 那条既有的禁令"：
    # 拿组装函数自己算一份来比——多一个字、少一个字都会红。
    # （不用 `BASE_PROMPT` 常量比：生产上人格是整段替换的，这里跟着本机解析走。）
    expected_system = compose_system_prompt(resolve_system_prompt(must_reply=True))
    assert first[0]["content"] == expected_system, "常驻 system 不是原来那一份了"
    assert expected_system.endswith(STAY_IN_CHARACTER_RULE), "尾部那条禁令既有的"
    for text in (UNSURE_ASK_RULE, ASK_TURN_NOTE, HOLD_TURN_NOTE, NO_ASSERT_NOTE):
        assert text not in first[0]["content"]
        assert text not in second[0]["content"]
    # **prompt_guard 扫机制词**：新增的那句话要过这一关（人格/回复口径，不是 agent 口径）
    assert scan_persona_text(NO_ASSERT_NOTE) == []
    assert NO_ASSERT_MARKER not in first[-1]["content"], "第一版请求里还没有这句话"
    assert NO_ASSERT_MARKER in second[-1]["content"], "它只在她被打回的那一轮出现"


def test_a_normal_topic_still_answers_as_before() -> None:
    """**验收 6**：判定没给新信号（老版 prompt）时，正常话题**照旧**回答。

    这是"逐字相同"那一档：没标签 = clear/不专业 = 两条规则都不生效。
    """

    judge = ScriptedJudge("<route>REPLY</route><from>none</from><topic>话题</topic>")
    reply = ScriptedReply("我接一句。")
    engine = DialogueEngine(reply, judge_client=judge)

    result = asyncio.run(engine.handle(msg(1, "@YunRu 在吗", mentioned=True)))

    assert result is not None and result.text == "我接一句。"
    assert len(reply.requests) == 1, "没标签时一次都不许多余"
    note = _volatile(reply.requests[0])
    assert ASK_MARKER not in note and HOLD_MARKER not in note
    assert NO_ASSERT_MARKER not in note
    snapshot = engine.snapshot()
    assert snapshot.clarify_quiet == 0 and snapshot.reply_grounding_rewrites == 0


def test_the_rules_can_be_turned_off() -> None:
    """面板关掉它 → 退回从前：两条规则都不生效（连"不出声"也不生效）。

    这里改的是**这一台引擎自己的替身开关**（`_default_flags`），不 install 到进程上——
    否则这条测试会污染同进程里别的测试的开关状态。
    """

    from qq_roleplay_bot.runtime_flags import RuntimeFlags

    judge_output = (
        "<route>REPLY</route><topic>综测</topic>"
        "<understood>LOST</understood><specialist>YES</specialist>"
    )
    # ① 关掉之后：没被叫到也照常开口（判定说 REPLY 就 REPLY）
    warming = ScriptedJudge("<route>REPLY</route><topic>你好</topic>", judge_output)
    reply = ScriptedReply("在。", "我接一句。")
    engine = DialogueEngine(reply, judge_client=warming, group_listen=True)
    engine._default_flags = RuntimeFlags(ask_when_unsure=False)
    assert asyncio.run(engine.handle(msg(1, "@YunRu 在吗", mentioned=True))) is not None
    result = asyncio.run(engine.handle(msg(2, "综测那个事我搞定了", user_id="200")))
    assert result is not None, "关掉之后不许再因为'没懂'而不出声"
    assert engine.snapshot().clarify_quiet == 0

    # ② 关掉之后：被叫到、写了具体断言也不再打回
    reply = ScriptedReply("综测报名截止到 15 号。")
    engine = DialogueEngine(reply, judge_client=ScriptedJudge(judge_output))
    engine._default_flags = RuntimeFlags(ask_when_unsure=False)
    result = asyncio.run(engine.handle(msg(1, "@YunRu 综测是啥", mentioned=True)))
    assert result is not None and result.text == "综测报名截止到 15 号。"
    assert len(reply.requests) == 1
    assert engine.snapshot().reply_grounding_rewrites == 0


# --- 与风格审核共处：**重写机会只有一次** ------------------------------------


def test_review_and_grounding_share_one_rewrite_chance() -> None:
    """审核已经打回重写过一次 → 不再因为"有具体断言"重写第二次（最多一次）。

    两条路**共用**同一次重写，这是"不新增机器"的具体含义。
    """

    from qq_roleplay_bot.runtime_flags import RuntimeFlags

    judge = ScriptedJudge(
        "<route>REPLY</route><topic>综测</topic>"
        "<understood>LOST</understood><specialist>YES</specialist>"
    )
    reviewer = ScriptedReviewer((False, "太冲了"), (True, ""))
    reply = ScriptedReply("综测报名截止到 15 号。", "行，那我不说了。")
    engine = DialogueEngine(reply, judge_client=judge, style_reviewer=reviewer)
    engine._default_flags = RuntimeFlags(review_enabled=True)

    result = asyncio.run(engine.handle(msg(1, "@YunRu 综测是啥", mentioned=True)))

    assert result is not None
    # 审核打回 → 重写一次；重写那一版**没有**具体断言（"行，那我不说了"），
    # 所以"没把握就别断言"这一关不会再去要第二次重写。
    assert len(reply.requests) == 2, "总共只许重写一次"
    assert len(reviewer.calls) == 2, "初稿一遍 + 重写版一遍（重写版没有再被打回）"
    assert engine.snapshot().reply_grounding_rewrites == 0, "审核已经用掉那次机会"
    assert engine.snapshot().reply_reviewed == 1
