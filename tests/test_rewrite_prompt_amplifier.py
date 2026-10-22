"""打回那一条请求**不再贴回她上一版的原话**（2026-10-18 裁定的"拆放大器"）。

由来（用户 2026-10-18 交底，实测）：`_revision_request` 曾经写着

    你刚写的是：{她刚才那一版}

于是"打回重写"这件事反而把**她自己编的那一句**原样又递给她一遍。那不是提醒她改哪句，
是**给编造加码**：15:41→15:42 那两轮她越编越具体，起点正是这句话被贴回去。它也是整轮
请求里**唯一一处"她自己的正文重新进 prompt"**的地方（历史里她的正文在 DATA 段、核对时
还要排掉 `speaker="yunru"`；这一处排不掉，因为那是我们主动贴的）。

这一份钉两件事：

1. **三趟打回都不再引述**（风格审核 / 「没把握就别断言」 / 不可信短码），各自仍然
   把"为什么不行"说清楚——打回的话必须**自足**，不靠引用原文；
2. **反证**："你刚写的是"这个形状一个字都不许回来。它要是回来了，上面三条会一起变绿
   （因为她们只看"正文不出现"，而正文不出现本来就成立）——所以这条反证是必须的。

⚠️ 与短码有关的那一条还多挡一件事：旧实现要专门把短码**抹成"（那串记号）"**才敢引述；
不再引述之后这一步也不该留（`redact` 参数已删）。所以那里断言的是**连占位都没有**。
"""
import asyncio

from qq_roleplay_bot import data_boundary
from qq_roleplay_bot.ask_when_unsure import NO_ASSERT_NOTE, PAST_CLAIM_NOTE
from qq_roleplay_bot.stage3_main import DialogueEngine
from qq_roleplay_bot.transport import IncomingMessage, MessageTarget

GROUP = "717151356"
TARGET = MessageTarget(group_id=GROUP)

#: 旧版那一段的开口（"你刚写的是：…"）。**它一个字都不许再出现。**
AMPLIFIER = "你刚写的是"


def _msg(index, text="测试", *, mentioned=True):
    return IncomingMessage(
        message_id=f"m{index}", session_id=f"group:{GROUP}", user_id="100",
        text=text, target=TARGET, sender_name="某人", is_bot_mentioned=mentioned,
    )


def _marker_in_request(request) -> str:
    """从一条请求里取回当轮那个记号（回复 agent 的替身用它来"照抄"）。"""

    text = "\n".join(item["content"] for item in request if item["role"] == "user")
    for pattern in data_boundary.MARKER_SHAPE_PATTERNS:
        match = pattern.search(text or "")
        if match:
            return match.group(1)
    return ""


class ScriptedJudge:
    def __init__(self, *outputs) -> None:
        self.outputs = list(outputs) or ["<route>REPLY</route><topic>话题</topic>"]
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


class LeakingReply(ScriptedReply):
    """第一版照抄当轮记号（记号从请求里现取——它是每轮生成的），第二版干净。"""

    async def complete(self, request):
        self.requests.append(request)
        token = _marker_in_request(request)
        assert token, "请求里没有记号——这一轮的'抄记号'无从发生，测试成了恒真"
        if len(self.requests) == 1:
            body = f"行，‹{token}› 我知道了。"
        else:
            body = "行，我记下了。"
        return (f"<decision>REPLY</decision><dialogue>KEEP</dialogue>"
                f"<reply>{body}</reply>")


class ScriptedReviewer:
    def __init__(self, *results) -> None:
        self.results = list(results)
        self.calls: list = []

    def _next(self):
        index = min(len(self.calls) - 1, len(self.results) - 1)
        return self.results[index]

    async def review(self, draft, *, context=""):
        self.calls.append(draft)
        passed, reason, *_rest = self._next()
        return passed, reason

    async def review_verdict(self, draft, *, context=""):
        from qq_roleplay_bot.style_reviewer import ReviewVerdict

        self.calls.append(draft)
        result = self._next()
        return ReviewVerdict(bool(result[0]), str(result[1]))


def _engine(reply, *, reviewer=None) -> DialogueEngine:
    """装一台引擎，并把运行期开关**钉在这一台上**（别跟着进程里别的测试走）。"""

    from qq_roleplay_bot.runtime_flags import RuntimeFlags

    engine = DialogueEngine(
        reply, judge_client=ScriptedJudge(), style_reviewer=reviewer,
        enabled_group_ids=frozenset({GROUP}),
    )
    flags = RuntimeFlags(review_enabled=reviewer is not None)
    engine._flags = lambda: flags
    return engine


def _feedback(reply) -> str:
    """打回那一条 user 消息的正文（请求的最后一条）。"""

    return reply.requests[1][-1]["content"]


# --- 三趟各一条 -------------------------------------------------------------


def test_the_review_rewrite_does_not_echo_the_draft() -> None:
    """① 风格审核打回：说清"哪一处不合适"，但**不引述**她那一版。"""

    reviewer = ScriptedReviewer((False, "太冲了，像在训人"), (True, ""))
    reply = ScriptedReply("你得先看文档再问。", "行，那我换个说法。")
    engine = _engine(reply, reviewer=reviewer)

    result = asyncio.run(engine.handle(_msg(1, "@YunRu 接口怎么调")))
    assert result is not None and result.text == "行，那我换个说法。"

    feedback = _feedback(reply)
    assert "你刚写的那一句要重来" in feedback
    assert "不合适的地方：太冲了，像在训人" in feedback, "审核给的理由必须还在"
    assert "你得先看文档再问。" not in feedback, "她那一版原话被贴回去了（放大器）"
    assert AMPLIFIER not in feedback
    assert "（那串记号）" not in feedback, "不再引述正文，占位也不该出现"


def test_the_grounding_rewrite_does_not_echo_the_draft() -> None:
    """② 「没把握就别断言」打回：递 `NO_ASSERT_NOTE`，但**不引述**她那一版。"""

    judge = ScriptedJudge(
        "<route>REPLY</route><topic>综测</topic>"
        "<understood>LOST</understood><specialist>YES</specialist>"
    )
    draft = "综测报名截止到 15 号，你去教务系统提交就行。"
    reply = ScriptedReply(draft, "这个我真不清楚。")
    engine = DialogueEngine(reply, judge_client=judge,
                            enabled_group_ids=frozenset({GROUP}))

    result = asyncio.run(engine.handle(_msg(1, "@YunRu 综测报名截止了吗")))
    assert result is not None and result.text == "这个我真不清楚。"

    feedback = _feedback(reply)
    assert NO_ASSERT_NOTE.strip() in feedback, "那一趟的话术必须原样在"
    assert draft not in feedback, "她那一版原话被贴回去了（放大器）"
    assert AMPLIFIER not in feedback


def test_the_marker_rewrite_does_not_echo_the_draft_or_its_placeholder() -> None:
    """③ 不可信短码打回：只给理由，**连"（那串记号）"那个占位都不该出现**。

    旧实现是为了"敢引述"才需要抹掉短码；现在根本不引述，这一步跟着 `redact` 一起删了。
    这条用例是 `tests/test_data_boundary.py` 里那条的对应物（那边从"包含占位"改成了
    "连占位都不该出现"）。
    """

    reply = LeakingReply()
    engine = _engine(reply)

    result = asyncio.run(engine.handle(_msg(1, "@YunRu 在吗")))
    assert result is not None and result.text == "行，我记下了。"

    feedback = _feedback(reply)
    assert "你把消息里那串短码写进去了" in feedback, "打回的理由必须还在"
    assert "（那串记号）" not in feedback, "不再引述正文，占位也不该出现"
    assert "‹" not in feedback, "引述里还带着记号（等于又示范一遍）"
    assert AMPLIFIER not in feedback


# --- 反证 -------------------------------------------------------------------


def test_the_amplifier_phrase_never_comes_back() -> None:
    """**反证**：三趟打回拼出来的一条 user 消息里都没有"你刚写的是"。

    直接测"函数本身"而不是走引擎：这条要挡的是"以后谁把引述加回来"。三趟各自会绿在
    "正文不出现"上（它们只比自己的正文），只有这一条会在引述回来的那一刻红。
    """

    engine = DialogueEngine(ScriptedReply("我接一句。"),
                            judge_client=ScriptedJudge(),
                            enabled_group_ids=frozenset({GROUP}))
    request = [{"role": "system", "content": "你是云茹。"},
               {"role": "user", "content": "<message who=\"1\">在吗</message>"}]

    flights = (
        engine._revision_request(request, "太冲了，像在训人"),               # 审核
        engine._revision_request(request, "", note=NO_ASSERT_NOTE),          # 别断言
        engine._revision_request(request, "", note=PAST_CLAIM_NOTE),         # 过往断言
        engine._revision_request(                                           # 短码
            request, "你把消息里那串短码写进去了。那些短码只是记号，不要抄、也不要提。"),
    )
    for revised in flights:
        body = revised[-1]["content"]
        assert AMPLIFIER not in body, body
        assert "你刚写的那一句要重来" in body, "打回的开口还在（别把整段一起删了）"
        # 打回的话必须**自足**：要么有"不合适的地方：…"，要么带一句现成的 `note`。
        assert ("不合适的地方：" in body
                or NO_ASSERT_NOTE.strip() in body
                or PAST_CLAIM_NOTE.strip() in body), body
    # 请求本身一字不动（只在末尾追加一条）。
    assert flights[0][:-1] == request
