"""双 agent 结构（判定 + 回复）的接线测试。

分工：判定 agent 决定"要不要接、从哪条开始"，回复 agent 只说怎么回。
`judge_client` 为 None 时退回单 agent —— 这条回退路径也要保住，否则出问题无法降级。
"""
import asyncio

from qq_roleplay_bot.stage3_main import LIVE_TARGET, DialogueEngine
from qq_roleplay_bot.stage3_runtime import ConversationMode
from qq_roleplay_bot.transport import IncomingMessage, MessageTarget

GROUP = "717151356"
TARGET = MessageTarget(group_id=GROUP)


def msg(index, text="测试", *, mentioned=False, user_id="100"):
    return IncomingMessage(
        message_id=f"m{index}", session_id=f"group:{GROUP}", user_id=user_id,
        text=text, target=TARGET, sender_name="某人", is_bot_mentioned=mentioned,
    )


class ScriptedJudge:
    """记录收到的判定请求，返回脚本化的判定输出。"""

    def __init__(self, output="<route>REPLY</route><from>none</from><topic>话题</topic>"):
        self.output = output
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        return self.output


class ScriptedReply:
    def __init__(self, output="<decision>REPLY</decision><dialogue>KEEP</dialogue>"
                             "<reply>我接一句。</reply>"):
        self.output = output
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        return self.output


def test_no_judge_client_falls_back_to_single_agent() -> None:
    """不传判定 client 时退回单 agent：回复调用照常发生。"""

    reply = ScriptedReply()
    engine = DialogueEngine(reply)

    result = asyncio.run(engine.handle(msg(1, "@YunRu 在吗", mentioned=True)))
    assert result is not None and result.text == "我接一句。"
    assert len(reply.requests) == 1, "单 agent 下应当只有一次调用"
    assert engine.snapshot().judge_calls == 0


def test_judge_no_reply_skips_the_reply_call_entirely() -> None:
    """判定说 NO_REPLY 时，回复调用必须完全不发生——这是双 agent 的主要收益。"""

    judge = ScriptedJudge("<route>NO_REPLY</route><from>none</from>")
    reply = ScriptedReply()
    engine = DialogueEngine(reply, judge_client=judge)

    result = asyncio.run(engine.handle(msg(1, "@YunRu 在吗", mentioned=True)))
    assert result is None
    assert len(judge.requests) == 1
    assert reply.requests == [], "判定 NO_REPLY 后不应再调回复 agent"
    snapshot = engine.snapshot()
    assert snapshot.judge_calls == 1
    assert snapshot.model_calls == 0


def test_judge_reply_then_reply_agent_runs() -> None:
    judge = ScriptedJudge("<route>REPLY</route><from>none</from><topic>甲</topic>")
    reply = ScriptedReply()
    engine = DialogueEngine(reply, judge_client=judge)

    result = asyncio.run(engine.handle(msg(1, "@YunRu 在吗", mentioned=True)))
    assert result is not None and result.text == "我接一句。"
    assert len(judge.requests) == 1
    assert len(reply.requests) == 1


def test_judge_sees_seq_and_not_index() -> None:
    judge = ScriptedJudge()
    engine = DialogueEngine(ScriptedReply(), judge_client=judge)
    # 用 @ 触发，避免落到"攒够 20 条才检查"的阈值路径而被 defer。
    asyncio.run(engine.handle(msg(1, "@YunRu 第一句", mentioned=True)))
    asyncio.run(engine.handle(msg(2, "@YunRu 第二句", mentioned=True)))

    user = judge.requests[1][1]["content"]
    assert 'seq="0"' in user
    assert "index=" not in user, "判定不应看到会滑动的 index"


def test_reply_agent_receives_the_live_window_not_a_fixed_tail() -> None:
    """回复段带的是"摘要之后累积的全部消息"，条数上限就是压缩阈值。

    不是"最近 N 条"——那等于固定滑动窗口，前缀每轮都断。
    """

    judge = ScriptedJudge()
    reply = ScriptedReply()
    engine = DialogueEngine(reply, judge_client=judge)

    for i in range(1, 8):
        asyncio.run(engine.handle(msg(i, f"@YunRu 第{i}句", mentioned=True)))
    stable = reply.requests[-1][1]["content"]
    assert "第7句" in stable
    assert stable.count("<message ") <= LIVE_TARGET


def test_judge_failure_degrades_to_silence() -> None:
    """判定调用失败必须回落为不出声，而不是让回复段缺语境硬说。"""

    class Exploding:
        async def complete(self, request):
            raise RuntimeError("judge down")

    reply = ScriptedReply()
    engine = DialogueEngine(reply, judge_client=Exploding())
    result = asyncio.run(engine.handle(msg(1, "@YunRu 在吗", mentioned=True)))
    assert result is None
    assert reply.requests == []


def test_unparseable_judge_output_degrades_to_silence() -> None:
    judge = ScriptedJudge("我随便说点什么，没有标签")
    reply = ScriptedReply()
    engine = DialogueEngine(reply, judge_client=judge)
    result = asyncio.run(engine.handle(msg(1, "@YunRu 在吗", mentioned=True)))
    assert result is None
    assert reply.requests == []


def test_judge_latency_is_measured_separately() -> None:
    judge = ScriptedJudge()
    engine = DialogueEngine(ScriptedReply(), judge_client=judge)
    asyncio.run(engine.handle(msg(1, "@YunRu 在", mentioned=True)))
    metrics = engine.snapshot().metrics or {}
    assert (metrics.get("judge_latency") or {}).get("count") == 1
    assert (metrics.get("model_latency") or {}).get("count") == 1


def test_judge_prompt_does_not_leak_persona() -> None:
    """判定请求里不该有人设内容——它不需要，也不该拿到。"""

    judge = ScriptedJudge()
    engine = DialogueEngine(ScriptedReply(), judge_client=judge)
    asyncio.run(engine.handle(msg(1, "@YunRu 在", mentioned=True)))
    system = judge.requests[0][0]["content"]
    assert "心灵终结" not in system
    assert "<decision>" not in system


def test_command_path_skips_both_agents() -> None:
    """命令不调模型，双 agent 下也不该调判定。"""

    judge = ScriptedJudge()
    reply = ScriptedReply()
    engine = DialogueEngine(reply, judge_client=judge)
    result = asyncio.run(engine.handle(msg(1, "/yunru ping")))
    assert result is not None and result.text == "pong"
    assert judge.requests == []
    assert reply.requests == []


def test_dual_agent_mode_enum_untouched() -> None:
    """加判定不该影响会话模式语义。"""

    judge = ScriptedJudge()
    reply = ScriptedReply()
    engine = DialogueEngine(reply, judge_client=judge)
    asyncio.run(engine.handle(msg(1, "@YunRu 在吗", mentioned=True)))
    state = engine.sessions.state(f"group:{GROUP}")
    assert state.mode is ConversationMode.ACTIVE


# --- 回复 agent 没有否决权 ---------------------------------------------------


def test_reply_prompt_drops_the_decision_tag_in_dual_agent_mode() -> None:
    """双 agent 下，回复 prompt 里不该有 `<decision>`——它没有选择权。

    单 agent 模式（没有判定）必须保留：那一次调用确实要同时决定说不说。
    """

    judge = ScriptedJudge()
    reply = ScriptedReply()
    dual = DialogueEngine(reply, judge_client=judge)
    asyncio.run(dual.handle(msg(1, "@YunRu 在吗", mentioned=True)))
    system = reply.requests[-1][0]["content"]
    assert "<decision>" not in system
    assert "这一轮必须填" in system
    assert "不要空着不答" in system, "得有一句明确的话，告诉她这轮不能说空"

    single_reply = ScriptedReply()
    single = DialogueEngine(single_reply)
    asyncio.run(single.handle(msg(1, "@YunRu 在吗", mentioned=True)))
    single_system = single_reply.requests[-1][0]["content"]
    assert "<decision>REPLY|NO_REPLY|EXIT_DIALOGUE</decision>" in single_system


def test_reply_agent_cannot_veto_a_reply_decision() -> None:
    """判定说要回之后，回复 agent 即使学舌吐出 NO_REPLY，也得把正文交出来。"""

    judge = ScriptedJudge("<route>REPLY</route><topic>甲</topic>")
    reply = ScriptedReply(
        "<decision>NO_REPLY</decision><dialogue>KEEP</dialogue><reply>我还是说一句。</reply>"
    )
    engine = DialogueEngine(reply, judge_client=judge)
    result = asyncio.run(engine.handle(msg(1, "@YunRu 在吗", mentioned=True)))
    assert result is not None and result.text == "我还是说一句。"
    assert engine.snapshot().empty_forced_reply == 0


def test_empty_reply_after_a_reply_decision_is_an_anomaly() -> None:
    """空手而归记成异常，并且不许悄悄变成一次沉默。"""

    judge = ScriptedJudge("<route>REPLY</route><topic>甲</topic>")
    reply = ScriptedReply("<decision>NO_REPLY</decision><dialogue>KEEP</dialogue><reply></reply>")
    engine = DialogueEngine(reply, judge_client=judge)
    result = asyncio.run(engine.handle(msg(1, "@YunRu 在吗", mentioned=True)))

    assert result is None
    snapshot = engine.snapshot()
    assert snapshot.empty_forced_reply == 1, "这是模型抽风，不是一种正常结果"
    # 没说话就不该进入"正在交谈"，也不该生成记忆证据。
    state = engine.sessions.state(f"group:{GROUP}")
    assert state.mode is ConversationMode.IDLE


def test_cache_totals_cover_every_session_client() -> None:
    """按会话分 client 之后，缓存统计必须合计所有 client。

    由来：`/super status` 原先只读兜底那个 client，而它一次调用都没跑过，
    于是线上会显示"命中率 0%"——一个会让人误判前缀稳定性的观测缺陷。
    """

    class Counter:
        def __init__(self, hit: int, miss: int, calls: int) -> None:
            self._stats = (hit, miss, calls)

        def cache_stats(self) -> dict[str, object]:
            hit, miss, calls = self._stats
            return {"calls": calls, "hit_tokens": hit, "miss_tokens": miss, "hit_rate": 0.0}

    engine = DialogueEngine(Counter(0, 0, 0), client_factory=lambda sid: Counter(100, 20, 3))
    engine._client_for("group:a")
    engine._client_for("group:b")
    engine._client_for("group:a")     # 同一个会话复用同一个 client
    totals = engine.cache_totals()
    assert {key: totals[key] for key in ("calls", "hit_tokens", "miss_tokens", "hit_rate")} == {
        "calls": 6, "hit_tokens": 200, "miss_tokens": 40, "hit_rate": 0.8333,
    }
    # 按 key 分开算：这条只贡献在"回复"那一栏，判定与记忆各算各的。
    report = engine.cache_report()
    assert report["dialogue"]["calls"] == 6
    assert report["judge"]["calls"] == 0
    assert report["memory"]["calls"] == 0
