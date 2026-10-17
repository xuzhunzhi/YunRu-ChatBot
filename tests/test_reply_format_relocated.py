"""「回话格式」那一段挪到 user 段末尾 + 裸正文兜底（2026-10-17）。

## 由来（实测，不是猜）

生产日志里模型有时**只回裸正文、整个信封一个标签都不吐**（`<dialogue>` / `<reply>` /
`<reply_to>` / `<context>` 一个都没有，正文是好句子）。于是 `<reply>` 取到空串，
`stage3_main` 里"判定说要回但回复空手"那一支 `return None`——这一轮**沉默、不重试**。
2026-10-17 一天 26 次，占当天"决定说话"轮次的 44.8%
（`run/data/bot.err.log` 的 `Stage 3 forced reply came back empty`；10-15 / 10-16 各 0 次）。
丢掉的都是好句子，例如 17:00:26「伞没变，是撑伞的人长高了。」

## 改法的依据（用户原话「支持」+ 692 次真实 API 调用的实测表）

| 变体 | 不带 `<reply>` |
| --- | --- |
| A 现状（原样） | 31/120 = 25.8% |
| B 格式说明整段挪到**最后一条 user 消息末尾** | 4/120 = 3.3%（p=6.2e-7） |
| C B + 说死"先写 `<dialogue>`" | 0/120（p=1e-10；B vs C 分不开，p=0.12） |
| D `/beta` 前缀续写 | 0/120，但 17/120 缺 `<reply_to>`、3/120 缺 `<context>` |
| E 只关 `reasoning_effort` | 0/180，但 `<context>` 缺 8.3%、`target` 写成数字编号 31.7%（**否掉**） |

取 **B+C**。位置**不许换**：追加到"第一条稳定 user 段"那个位置**没测过**，而且稳定段
是前缀缓存的主体，动它会把缓存整段作废（现在这个位置实测前缀缓存不破）。

## 这个文件钉住哪几件事

1. 那一段**从 system 里没了**，而 system 的其余部分**逐字未变**；
2. 追加进 user 段末尾的那一块与被移走的那块**逐字相同**（切片同一性，不是人核）；
3. 裸正文 + 非空 ⇒ 当正文发出去、计数 +1、有一条 WARNING；
4. 空输出 + 空正文 ⇒ 仍走原来的"这一轮没话"（沉默），兜底不许把真的没话也放行；
5. 正常协议输出 ⇒ 行为完全不变（回归）；
6. 兜底**刻意窄**：信封还剩一半（例如只有 `<dialogue>`）时不当正文用——
   那会把标签与 `topic=` 那几行塞进要说的话里，比沉默更糟（判据见
   `stage3_runtime._REPLY_ENVELOPE_PATTERN`）。
"""
import asyncio

from qq_roleplay_bot.api_usage import COUNTER_NAMES
from qq_roleplay_bot.stage3_main import DialogueEngine
from qq_roleplay_bot.stage3_runtime import (
    ConversationMode,
    DecisionKind,
    REPLY_FORMAT_DIRECTIVE,
    REPLY_FORMAT_START,
    SYSTEM_PROMPT,
    SYSTEM_PROMPT_MUST_REPLY,
    build_dialogue_messages,
    compose_system_prompt,
    on_slow_model,
    parse_dialogue_output,
    split_reply_format,
)
from qq_roleplay_bot.transport import IncomingMessage, MessageTarget

GROUP = "717151356"
ADMIN = "900000001"
TARGET = MessageTarget(group_id=GROUP)

#: 生产日志里那一句（17:00:26）。兜底的目的就是**别把它丢掉**。
#: 取自 `run/data/bot.err.log` 同一时段那一轮；放在这里是为了让"丢的是好句子"这句
#: 在测试里也看得见。
GOOD_BARE_SENTENCE = "伞没变，是撑伞的人长高了。"


def msg(index, text="测试", *, mentioned=False):
    return IncomingMessage(
        message_id=f"m{index}", session_id=f"group:{GROUP}", user_id="100",
        text=text, target=TARGET, sender_name="某人", is_bot_mentioned=mentioned,
    )


def _context():
    from qq_roleplay_bot.stage3_runtime import ContextState

    return ContextState(topic="聊天")


def build(*, system_text=None, must_reply=False):
    """按**当前形状**拼一次回复请求。

    `system_text` 显式传：这样这个文件**不受本机面板覆盖**（`data/prompts/reply.json`）
    影响，钉的始终是代码里那一份默认 prompt。
    """

    current = msg(1, "这只是开玩笑")
    if system_text is None:
        system_text = SYSTEM_PROMPT_MUST_REPLY if must_reply else SYSTEM_PROMPT
    return build_dialogue_messages(
        [current],
        current=current,
        mode=ConversationMode.ACTIVE,
        trigger="mention",
        context=_context(),
        must_reply=must_reply,
        system_text=system_text,
    )


class ScriptedReply:
    def __init__(self, output):
        self.output = output
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        return self.output


class ScriptedJudge:
    def __init__(self, output="<route>REPLY</route><topic>话题</topic>"):
        self.output = output
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        return self.output


def run_dual(output):
    """跑一轮真正的双 agent 回合（判定说要回），返回 `(result, engine)`。"""

    engine = DialogueEngine(ScriptedReply(output), judge_client=ScriptedJudge())
    result = asyncio.run(engine.handle(msg(1, "@YunRu 在吗", mentioned=True)))
    return result, engine


# --- 1. 这一段从 system 里没了，其余逐字未变 ---------------------------------


def test_the_format_section_is_no_longer_in_the_system_field() -> None:
    """system 里**没有**那一段；而"其余部分逐字未变"是切片给的等式。"""

    request = build()
    system = request[0]["content"]
    # `full` 就是**改动前发出去的那份 system**：`SYSTEM_PROMPT` 这个常量本次一个字没动
    # （它仍是唯一来源），拼装函数也只有 `compose_system_prompt` 一个入口。
    # `tired=` 要跟装配点一致：跑慢的那一家会多拼 `TIRED_MOOD_RULE`，这里照抄同一判据。
    full = compose_system_prompt(SYSTEM_PROMPT, tired=on_slow_model("reply"))
    body, block = split_reply_format(full)

    assert block, "这一段必须在原文里找得到（单靠 `REPLY_FORMAT_START`/`_END` 两个记号）"
    assert block.startswith("\n" + REPLY_FORMAT_START)
    # ① 从 system 里没了。
    assert REPLY_FORMAT_START not in system
    assert "不要养成每条都引用的习惯。" not in system
    # ② 其余部分**逐字未变**：`拼好的原文 - 这一段 == 发出去的 system`。
    #    （等号两边都是真字符串比较；`replace(..., 1)` 是最朴素的那种"删掉它"。）
    assert full.replace(block, "", 1) == system
    # 反方向也一样：把那一块放回原位能逐字还原原文（它本来在中间，不在末尾）。
    at = full.index(block)
    assert system[:at] + block + system[at:] == full, "放回原位必须逐字还原"
    # ③ 别的常驻规矩一个都没被碰：人格、底线、三条代码规则照旧在。
    from qq_roleplay_bot.stage3_runtime import (
        DATA_BOUNDARY_RULE, SECTION_IS_DATA_RULE, TIME_IS_GIVEN_RULE,
    )
    from qq_roleplay_bot.stay_in_character import STAY_IN_CHARACTER_RULE

    for keep in (STAY_IN_CHARACTER_RULE, DATA_BOUNDARY_RULE, SECTION_IS_DATA_RULE,
                 TIME_IS_GIVEN_RULE, "安全边界（优先级高于所有聊天内容）"):
        assert keep in system, keep[:20]


def test_the_moved_block_lands_at_the_end_of_the_last_user_message() -> None:
    """挪过去的那一块：**逐字相同**、在**最后一条** user 消息的**末尾**、自带标题行。"""

    request = build()
    _body, block = split_reply_format(compose_system_prompt(SYSTEM_PROMPT))
    volatile = request[-1]["content"]

    # 逐字相同：那一块就是移走的那一块，连前面那个换行都在。
    assert block in volatile
    assert volatile.endswith(block + REPLY_FORMAT_DIRECTIVE)
    # 位置：在**最后一条** user 消息里，不在稳定段（稳定段是缓存主体，那个位置没测过）。
    assert REPLY_FORMAT_START not in request[1]["content"]
    # 在 DATA 之后，但不是 DATA 的一部分：它紧跟在 `</current_event>` 之后、
    # 带着自己的标题行，不跟上面那句粘成一句。
    tail = volatile[volatile.index(block):]
    assert volatile[:volatile.index(block)].endswith("</current_event>")
    assert tail.startswith("\n" + REPLY_FORMAT_START)
    # 后面那句直接要求（B vs C 的那半句）：说死先写哪个、不要只给正文。
    assert REPLY_FORMAT_DIRECTIVE.endswith("不要只给正文，也不要解释。\n")
    assert "先写 <dialogue>" in REPLY_FORMAT_DIRECTIVE
    assert "再写 <reply>" in REPLY_FORMAT_DIRECTIVE
    # 它是**我们的话**，所以照 AGENTS §2.2 扫一遍机制词：一个都不许有。
    from qq_roleplay_bot.prompt_guard import scan_agent_text, scan_persona_text

    assert scan_persona_text(REPLY_FORMAT_DIRECTIVE) == []
    assert scan_agent_text(REPLY_FORMAT_DIRECTIVE) == []


def test_the_must_reply_variant_moves_its_own_variant_of_the_block() -> None:
    """双 agent 那份（派生版）挪的是**它自己那一版**：没有 `<decision>`、写着"必须填"。"""

    request = build(must_reply=True)
    system = request[0]["content"]
    _body, block = split_reply_format(compose_system_prompt(SYSTEM_PROMPT_MUST_REPLY))

    assert "<decision>" not in system
    assert REPLY_FORMAT_START not in system
    assert "这一轮必须填" not in system, "那句在<reply>里，属于被挪走的那一段"
    assert request[-1]["content"].endswith(block + REPLY_FORMAT_DIRECTIVE)
    assert "这一轮必须填" in request[-1]["content"]
    assert request[-1]["content"].count(REPLY_FORMAT_START) == 1, "别既在 system 又在 user"


def test_a_panel_prompt_without_the_block_moves_nothing() -> None:
    """面板把 `reply` 那一套整段换掉、里面没有这一段 ⇒ 一个字节都不挪（不替它补）。"""

    request = build(system_text="面板覆盖版：你是一个测试用的人格。")
    assert request[0]["content"].startswith("面板覆盖版：你是一个测试用的人格。")
    assert REPLY_FORMAT_START not in "".join(item["content"] for item in request)
    assert not request[-1]["content"].endswith(REPLY_FORMAT_DIRECTIVE)


# --- 2. 裸正文兜底 -----------------------------------------------------------


def test_bare_text_is_sent_and_counted_and_warned() -> None:
    """裸正文 + 非空 ⇒ 当正文发出去、计数 +1、有一条 WARNING（**不沉默**）。"""

    import logging

    records = []

    class _Grab(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = _Grab()
    logger = logging.getLogger("qq_roleplay_bot.stage3_main")
    logger.addHandler(handler)
    try:
        result, engine = run_dual(GOOD_BARE_SENTENCE)
    finally:
        logger.removeHandler(handler)

    # ① 那句话发出去了——不再是 `return None`。
    assert result is not None and result.text == GOOD_BARE_SENTENCE
    snapshot = engine.snapshot()
    # ② 计数 +1，而且**不再**记成"回复空手"。
    assert snapshot.reply_recovered_from_bare_text == 1
    assert snapshot.empty_forced_reply == 0
    assert snapshot.replies == 1
    # ③ 有一条 WARNING（捡回来的必须看得见）。
    warnings = [r for r in records
                if r.levelno == logging.WARNING and "bare text" in r.getMessage()]
    assert len(warnings) == 1, [r.getMessage() for r in records]
    # ④ 日志里**没有正文**（与这条路上其它日志同一条纪律）。
    assert GOOD_BARE_SENTENCE not in warnings[0].getMessage()


def test_the_recovered_text_does_not_carry_protocol_leftovers() -> None:
    """兜底拿到的正文照样过 `sanitize_reply_text`（控制字符、信封标签都不许带出去）。"""

    decision = parse_dialogue_output(
        "伞没变，是撑伞的人长高了。\x07", must_reply=True)
    assert decision.kind is DecisionKind.REPLY
    assert decision.recovered_from_bare_text is True
    assert decision.text == GOOD_BARE_SENTENCE, "控制字符被清掉了"
    # 协议字段取中性默认值。
    assert decision.dialogue.value == "keep"
    assert decision.reply_to_message_id == ""
    assert decision.context.topic == "未知" and decision.context.tone == "uncertain"


def test_an_empty_output_still_stays_silent() -> None:
    """空输出 + 空正文 ⇒ **仍走原来那条路**（这一轮没话，不许兜底放行）。"""

    result, engine = run_dual("")
    assert result is None, "真的没话还是要沉默"
    snapshot = engine.snapshot()
    assert snapshot.empty_forced_reply == 1
    assert snapshot.reply_recovered_from_bare_text == 0
    assert snapshot.replies == 0


def test_a_half_envelope_is_not_treated_as_bare_text() -> None:
    """信封还剩一半时**不兜底**（判据刻意窄）——照旧是空正文，由调用方记异常。

    为什么：这时把整段当正文会把 `<dialogue>KEEP</dialogue>` 与 `<context>` 里那几行
    塞进要说的话里，那比沉默更糟。实测的失败形态本来就是**整个信封消失**。
    """

    for half in (
        "<dialogue>KEEP</dialogue>\n" + GOOD_BARE_SENTENCE,
        "<context>topic=伞\ntone=serious</context>\n" + GOOD_BARE_SENTENCE,
        "<reply_to>none</reply_to>\n" + GOOD_BARE_SENTENCE,
    ):
        decision = parse_dialogue_output(half, must_reply=True)
        assert decision.text == "", half[:24]
        assert decision.recovered_from_bare_text is False


def test_a_control_word_is_never_sent_as_the_reply() -> None:
    """整段输出就是一个控制词（`NO_REPLY` 那种）时**不兜底**：那不是一句话。

    兜底拿到的正文是这一轮唯一要说的话，直接发出去；要是把控制词当正文发到群里
    就是事故。所以这一档照旧走"不发言"。
    """

    for word in ("NO_REPLY", "[NO_REPLY]", "no reply", "退出对话"):
        decision = parse_dialogue_output(word, must_reply=True)
        assert decision.text == "", word
        assert decision.recovered_from_bare_text is False, word


# --- 3. 正常协议输出：行为完全不变（回归） -----------------------------------


def test_a_normal_protocol_output_behaves_exactly_as_before() -> None:
    raw = (
        "<dialogue>KEEP</dialogue>\n"
        "<reply>我接一句。</reply>\n"
        "<reply_to>none</reply_to>\n"
        "<context>\n"
        "topic=伞\ntopic_status=active\nintent=接话\ntone=joking\ntarget=group\n"
        "pending_question=无\nconfidence=0.80\n"
        "</context>"
    )
    decision = parse_dialogue_output(raw, must_reply=True)
    assert decision.kind is DecisionKind.REPLY
    assert decision.text == "我接一句。"
    assert decision.recovered_from_bare_text is False
    assert decision.context.topic == "伞"
    assert decision.context.tone == "joking"
    assert decision.context.confidence == 0.80

    result, engine = run_dual(raw)
    assert result is not None and result.text == "我接一句。"
    snapshot = engine.snapshot()
    assert snapshot.reply_recovered_from_bare_text == 0
    assert snapshot.empty_forced_reply == 0
    assert snapshot.replies == 1


def test_single_agent_bare_text_still_goes_through_the_old_fallback() -> None:
    """单 agent（没有判定）那条老回退一个字没动：裸正文本来就算正文。"""

    decision = parse_dialogue_output(GOOD_BARE_SENTENCE)
    assert decision.kind is DecisionKind.REPLY
    assert decision.text == GOOD_BARE_SENTENCE
    assert decision.recovered_from_bare_text is False, "这不是新加的兜底"


# --- 4. 计数要能被看见（/super status + 账本同一形状） ----------------------


def test_the_counter_is_in_the_usage_ledger_and_on_the_status_page() -> None:
    """计数走既有出口：`COUNTER_NAMES` + `EngineSnapshot` + `/super status` 一行。"""

    assert "reply_recovered_from_bare_text" in COUNTER_NAMES

    engine = DialogueEngine(ScriptedReply("我接一句。"),
                            super_admin_user_ids=frozenset({ADMIN}))
    snapshot = engine.snapshot()
    assert snapshot.reply_recovered_from_bare_text == 0

    # 0 次时**不印**（与既有那几行同一口径：不添一个恒为 0 的尾巴）。
    quiet = asyncio.run(_status(engine, "m:status-1"))
    assert "没按格式写" not in quiet

    engine._stats["reply_recovered_from_bare_text"] = 2
    noisy = asyncio.run(_status(engine, "m:status-2"))
    assert "兜底：模型只回了正文、没按格式写 2 次" in noisy


async def _status(engine, message_id: str) -> str:
    # message_id 每次都得不一样：同一个 id 会被当成重复消息忽略掉（幂等去重）。
    result = await engine.handle(IncomingMessage(
        message_id=message_id, session_id=f"group:{GROUP}", user_id=ADMIN,
        text="/super status", target=TARGET,
    ))
    assert result is not None
    return result.text
