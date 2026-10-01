"""焦点在引擎里的行为：冷群排队 + "稍等"、到点切换、承诺必须回、私聊不受影响。

规则依据 `docs/MULTI_GROUP_FOCUS.md` 第二～四节。这里用假时钟 + 脚本化客户端，
所以每条行为都能确定性地钉住（真机验证另跑回放）。
"""
import asyncio

from qq_roleplay_bot.stage3_main import FOCUS_FAREWELL_LINE, DialogueEngine
from qq_roleplay_bot.transport import IncomingMessage, MessageTarget

A = "717151356"
B = "717151357"
TARGET_A = MessageTarget(group_id=A)
TARGET_B = MessageTarget(group_id=B)


def sid(group: str) -> str:
    """焦点按**会话 id** 记账（`group:<群号>`），不是裸群号。"""

    return f"group:{group}"

REPLY_OUTPUT = "<decision>REPLY</decision><dialogue>KEEP</dialogue><reply>收到。</reply>"
NO_REPLY_OUTPUT = "<decision>NO_REPLY</decision><dialogue>KEEP</dialogue><reply></reply>"


class Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class ScriptedJudge:
    """判定：route/related 可切换，起点永远给窗口第一条。"""

    def __init__(self, *, reply: bool = True, related: bool = True) -> None:
        self.reply = reply
        self.related = related
        self.calls = 0
        self.requests = []

    async def complete(self, request):
        self.calls += 1
        self.requests.append(request)
        if "压缩存档" in request[0]["content"]:
            # 焦点回避时会借判定这条路做压缩，返回一段摘要。
            return "早先聊了不少事。"
        route = "REPLY" if self.reply else "NO_REPLY"
        body = (
            f"<topic>话题</topic>"
            f"<topic_start>0</topic_start>"
            f"<related>{'YES' if self.related else 'NO'}</related>"
        )
        # judge'（积压消息那一版）的 prompt 里没有 route；真模型也不会吐。
        # 但这里故意吐出来，用来验证"学舌也否决不了"。
        if "上下文定位器" in request[0]["content"]:
            return body
        return f"<route>{route}</route>" + body


class ScriptedReply:
    def __init__(self, output: str = REPLY_OUTPUT) -> None:
        self.output = output
        self.calls = 0
        self.requests = []

    async def complete(self, request):
        self.calls += 1
        self.requests.append(request)
        return self.output


def build_engine(clock: Clock, judge: ScriptedJudge):
    reply = ScriptedReply()
    engine = DialogueEngine(
        reply,
        judge_client=judge,
        enabled_group_ids=frozenset({A, B}),
        group_listen=True,
        typing_sim=False,
        clock=clock.monotonic,
    )
    return engine, reply


def message(index: int, group: str = A, *, text: str = "在吗", mentioned: bool = False,
            user_id: str = "100") -> IncomingMessage:
    return IncomingMessage(
        message_id=f"m{index}", session_id=f"group:{group}", user_id=user_id, text=text,
        target=MessageTarget(group_id=group), sender_name="某人", is_bot_mentioned=mentioned,
    )


def make_hot(engine, clock: Clock, *, group: str = A, index: int = 1) -> None:
    """让她在这个群里开口，从而进入当值状态。"""

    result = asyncio.run(engine.handle(message(index, group, text="@她 在吗", mentioned=True)))
    assert result is not None, "第一步应当真的回一句"
    assert engine.focus.is_hot(sid(group)), "她开口之后这个群应当在当值"


# --- 冷群：排队与"稍等" -----------------------------------------------------


def test_hot_group_keeps_deciding_every_message_no_frequency_brake() -> None:
    """她进入话题之后，群里每说一句都要**过一遍判定**——引擎不做频率刹车。

    2026-09-28 用户明确要求："yunru 主动进入一个话题后应该一直遵循判定来回复。"
    所以这里锁死：连着几条没人叫她的群聊消息，判定被调用的次数与消息数一致，
    不存在"刚说过话就压住几条"的引擎级抑制。

    （真正限制频率的地方只有两处，都不是"压住某几句"：判定自己说 NO_REPLY，
    以及焦点层的会话级上限——连回 50 条/当值 5 分钟时收尾离开。）
    """

    clock = Clock()
    judge = ScriptedJudge()
    engine, _ = build_engine(clock, judge)
    make_hot(engine, clock)
    before = engine.snapshot().judge_calls

    for index in range(2, 6):
        clock.advance(5)
        asyncio.run(engine.handle(message(index, A, text=f"第{index}句群聊", user_id="200")))

    assert engine.snapshot().judge_calls == before + 4, "当值期间每条都要交给判定"


def test_at_in_a_cold_group_gets_an_ack_and_is_queued() -> None:
    clock = Clock()
    judge = ScriptedJudge()
    engine, reply = build_engine(clock, judge)
    make_hot(engine, clock)
    calls_before = reply.calls

    result = asyncio.run(engine.handle(message(2, B, text="@她 你看这个", mentioned=True)))

    assert result is not None, "被 @ 时要先回一句收到"
    assert result.paced is False, "稍等要立刻发，不摆拟人化节奏"
    assert engine.focus.queue_size(sid(B)) == 1
    assert reply.calls == calls_before, "冷群的消息不许现在调回复模型"
    assert engine._stats["focus_acks"] == 1
    # "稍等"要进她的历史，但不算真实回复
    history = engine.sessions.state(f"group:{B}").history
    assert history[-1].is_bot_message and history[-1].text == result.text


def test_plain_chatter_in_a_cold_group_only_becomes_background() -> None:
    clock = Clock()
    engine, reply = build_engine(clock, ScriptedJudge())
    make_hot(engine, clock)
    calls_before = reply.calls

    result = asyncio.run(engine.handle(message(2, B, text="今天天气不错")))

    assert result is None
    assert engine.focus.queue_size(sid(B)) == 0, "普通闲聊不进队列"
    assert reply.calls == calls_before
    assert engine.sessions.state(f"group:{B}").history[-1].text == "今天天气不错"


def test_mention_and_quote_are_queued_without_an_ack() -> None:
    clock = Clock()
    engine, _ = build_engine(clock, ScriptedJudge())
    make_hot(engine, clock)

    mentioned = asyncio.run(engine.handle(message(2, B, text="云茹你怎么看")))
    quoted = asyncio.run(engine.handle(
        message(3, B, text="接着说", mentioned=False)
    ))
    assert mentioned is None and quoted is None, "提及/引用不发收到语"
    assert engine.focus.queue_size(sid(B)) >= 1
    assert engine._stats["focus_acks"] == 0


def test_ack_cooldown_allows_only_one_per_window() -> None:
    clock = Clock()
    engine, _ = build_engine(clock, ScriptedJudge())
    make_hot(engine, clock)

    first = asyncio.run(engine.handle(message(2, B, text="@她 一", mentioned=True)))
    clock.advance(30)
    second = asyncio.run(engine.handle(message(3, B, text="@她 二", mentioned=True)))
    clock.advance(100)                # 累计 130 秒，过了 120 秒的冷却
    third = asyncio.run(engine.handle(message(4, B, text="@她 三", mentioned=True)))

    assert first is not None
    assert second is None, "冷却期内不再刷「稍等」"
    assert third is not None, "过了 120 秒可以再回一句"
    assert engine._stats["focus_acks"] == 2
    assert engine.focus.queue_size(sid(B)) == 3, "三条都要排队，谁都不许丢"


def test_private_chat_is_not_gated_by_the_focus() -> None:
    clock = Clock()
    judge = ScriptedJudge()
    engine, reply = build_engine(clock, judge)
    make_hot(engine, clock)
    calls_before = reply.calls
    private = IncomingMessage(
        message_id="p1", session_id="private:900", user_id="900", text="在吗",
        target=MessageTarget(user_id="900"), sender_name="某人", is_bot_mentioned=True,
    )
    engine.private_debug_user_ids = frozenset({"900"})

    result = asyncio.run(engine.handle(private))

    assert result is not None, "私聊不进焦点，照常处理"
    assert reply.calls == calls_before + 1


# --- 到点：释放与切换 -------------------------------------------------------


def test_tick_releases_after_quiet_and_serves_the_queue() -> None:
    clock = Clock()
    judge = ScriptedJudge()
    engine, reply = build_engine(clock, judge)
    make_hot(engine, clock)
    asyncio.run(engine.handle(message(2, B, text="@她 你看这个", mentioned=True)))
    calls_before = reply.calls

    clock.advance(46)          # 话题 45 秒没继续
    deliveries = asyncio.run(engine.tick(clock.monotonic()))

    assert engine.focus.is_hot(sid(B)), "该轮到 B 当值"
    assert reply.calls == calls_before + 1, "排队的那条现在要被处理"
    assert deliveries, "被排队消息的回复要交出来投递"
    source, outgoing = deliveries[-1]
    assert source.message_id == "m2"
    assert outgoing[0].paced is True, "这是真实回复，走拟人化节奏"
    assert engine.focus.queue_size(sid(B)) == 0
    assert "m2" not in engine._resumed_ids, "处理完要把标记清掉"


def test_queued_message_is_not_duplicated_in_history() -> None:
    clock = Clock()
    engine, _ = build_engine(clock, ScriptedJudge())
    make_hot(engine, clock)
    asyncio.run(engine.handle(message(2, B, text="@她 你看这个", mentioned=True)))
    clock.advance(46)
    asyncio.run(engine.tick(clock.monotonic()))

    texts = [item.text for item in engine.sessions.state(f"group:{B}").history]
    assert texts.count("@她 你看这个") == 1, f"排队消息不许在历史里出现两遍: {texts}"


def test_promised_message_uses_the_routing_only_judge() -> None:
    """积压消息走 judge'：那一版 prompt 里没有 route，判定也就否决不了。

    "要不要回"这件事在她说出"稍等"的时候就已经定了；再给判定一个否决权，
    要么得绕过它、要么得让它说违心话，都不干净。
    """

    clock = Clock()
    judge = ScriptedJudge()
    engine, reply = build_engine(clock, judge)
    make_hot(engine, clock)
    asyncio.run(engine.handle(message(2, B, text="@她 你看这个", mentioned=True)))
    calls_before = reply.calls
    # 即使模型学舌吐出 NO_REPLY，也必须按 REPLY 处理
    judge.reply = False

    clock.advance(46)
    deliveries = asyncio.run(engine.tick(clock.monotonic()))

    assert deliveries, "答应过「稍等」的消息必须给出结果"
    assert reply.calls == calls_before + 1
    judge_request = judge.requests[-1]
    assert "<route>" not in judge_request[0]["content"], "judge' 的 prompt 里不该有 route"
    assert "不需要判断要不要开口" in judge_request[0]["content"]


def test_mention_without_ack_still_uses_the_normal_judge() -> None:
    """没答应过的消息仍走普通判定（有 route），不许回就不回。"""

    clock = Clock()
    judge = ScriptedJudge()
    engine, reply = build_engine(clock, judge)
    make_hot(engine, clock)
    asyncio.run(engine.handle(message(2, B, text="云茹你怎么看")))
    calls_before = reply.calls
    judge.reply = False

    clock.advance(46)
    deliveries = asyncio.run(engine.tick(clock.monotonic()))

    assert deliveries == []
    assert reply.calls == calls_before
    assert "<route>" in judge.requests[-1][0]["content"]


def test_fifty_replies_end_with_a_farewell() -> None:
    clock = Clock()
    engine, _ = build_engine(clock, ScriptedJudge())
    make_hot(engine, clock)
    for index in range(2, 52):        # 连同第一条，一共 50 条回复
        clock.advance(1)
        result = asyncio.run(engine.handle(
            message(index, A, text=f"@她 第{index}句", mentioned=True)
        ))
        assert result is not None
    assert engine.focus.hot is not None and engine.focus.hot.replies >= 50

    clock.advance(1)
    deliveries = asyncio.run(engine.tick(clock.monotonic()))

    assert deliveries, "连回满 50 条要走，并且交代一句"
    _, outgoing = deliveries[0]
    assert outgoing[0].text == FOCUS_FAREWELL_LINE
    assert engine.focus.hot is None, "交代完就该走"


def test_duty_limit_releases_without_saying_goodbye() -> None:
    clock = Clock()
    engine, _ = build_engine(clock, ScriptedJudge())
    make_hot(engine, clock)
    for index in range(2, 10):         # 一直有人在跟她聊，避开"话题没继续"
        clock.advance(40)
        asyncio.run(engine.handle(message(index, A, text="@她 继续", mentioned=True)))

    clock.advance(5)                   # 当值超过 5 分钟，但话题刚刚还在继续
    deliveries = asyncio.run(engine.tick(clock.monotonic()))

    assert engine.focus.hot is None
    assert deliveries == [], "到点就走，不打招呼"


def test_tick_does_not_steal_the_focus_from_the_hot_group() -> None:
    """当值群还热着的时候，时钟不许把排队的人提上来。

    这条是接线测试抓出来的真 bug：原先时钟每一跳都无条件晋升队列里的下一个群，
    于是"不打断当前对话"完全失效。
    """

    clock = Clock()
    judge = ScriptedJudge()
    engine, reply = build_engine(clock, judge)
    make_hot(engine, clock)
    asyncio.run(engine.handle(message(2, B, text="@她 你看这个", mentioned=True)))
    calls_before = reply.calls

    clock.advance(10)                      # 远没到 45 秒
    deliveries = asyncio.run(engine.tick(clock.monotonic()))

    assert deliveries == []
    assert engine.focus.is_hot(sid(A)), "当值还是 A"
    assert engine.focus.queue_size(sid(B)) == 1, "B 还要继续等"
    assert reply.calls == calls_before


def test_compaction_on_release_when_someone_is_waiting() -> None:
    """有积压、真要切走时，把当值群这一段压进摘要、窗口收起来。"""

    clock = Clock()
    engine, _ = build_engine(clock, ScriptedJudge())
    make_hot(engine, clock)
    for index in range(2, 80):
        clock.advance(1)
        asyncio.run(engine.handle(message(index, A, text="@她 继续", mentioned=True)))
    asyncio.run(engine.handle(message(100, B, text="@她 在吗", mentioned=True)))

    clock.advance(46)
    asyncio.run(engine.tick(clock.monotonic()))

    state_a = engine.sessions.state(f"group:{A}")
    assert state_a.summary, "切走前应当把这一段收进摘要"
    assert len(state_a.history) <= 50, f"窗口应当收起来，实际 {len(state_a.history)}"


# --- 巡检：同一时刻只跑一个 -------------------------------------------------


def test_sweep_is_deferred_while_another_sweep_runs() -> None:
    """没有当值群时，两个群同时攒够 20 条也不许同时叫模型。"""

    clock = Clock()
    judge = ScriptedJudge()
    engine, reply = build_engine(clock, judge)
    engine.focus.begin_sweep()          # 假装另一个群正在巡检
    result = asyncio.run(engine.handle(message(1, A, text="@她 在吗", mentioned=True)))
    assert result is not None and result.paced is False, "巡检占用期间被 @ 也要先回一句稍等"
    assert engine.focus.queue_size(sid(A)) == 1, "巡检占用期间明确找她的要排队"
    assert reply.calls == 0

    engine.focus.end_sweep()
    clock.advance(46)
    deliveries = asyncio.run(engine.tick(clock.monotonic()))
    assert deliveries, "巡检空出来之后要轮到它"


# --- 观测 -------------------------------------------------------------------


def test_snapshot_exposes_focus_state_without_content() -> None:
    clock = Clock()
    engine, _ = build_engine(clock, ScriptedJudge())
    make_hot(engine, clock)
    asyncio.run(engine.handle(message(2, B, text="@她 你看这个", mentioned=True)))

    snapshot = engine.snapshot()
    assert snapshot.focus is not None
    hot = snapshot.focus["hot"]
    assert hot is not None and hot["session_id"] == sid(A)
    assert snapshot.focus["queued_total"] == 1
    assert snapshot.focus_queued == 1
    assert snapshot.focus_acks == 1
    assert "在吗" not in str(snapshot.focus)


# --- 运行装配：焦点时钟真的会把排队的回复送出去 -------------------------------


def test_runtime_ticker_delivers_the_queued_reply() -> None:
    """主循环的第二个任务（焦点时钟）必须真的把排队回复投递出去。

    离线测试里大部分地方直接调 `engine.tick()`，这里补的是**接线**：传输层没人发消息、
    通道空转的时候，时钟必须自己醒过来把该发的发掉。
    """

    from qq_roleplay_bot.runtime import _message_loop

    class FakeTransport:
        def __init__(self) -> None:
            self.sent: list[tuple[str | None, str]] = []
            self._incoming: asyncio.Queue = asyncio.Queue()
            self.closed = False

        async def start(self) -> None:
            return None

        async def close(self) -> None:
            self.closed = True
            self._incoming.put_nowait(None)

        async def receive(self):
            return await self._incoming.get()

        async def send(self, target, text: str, *, reply_to: str = "") -> None:
            self.sent.append((target.group_id, text))

        async def send_typing(self, target, notice: str = "typing") -> None:
            return None

        def push(self, item) -> None:
            self._incoming.put_nowait(item)

    clock = Clock()
    engine, _ = build_engine(clock, ScriptedJudge())

    async def main():
        transport = FakeTransport()
        transport.push(message(1, A, text="@她 在吗", mentioned=True))
        transport.push(message(2, B, text="@她 你看这个", mentioned=True))
        loop = asyncio.create_task(_message_loop(transport, engine, tick_seconds=0.01))
        for _ in range(50):                    # 让两条消息被处理完
            await asyncio.sleep(0.01)
        clock.advance(60)                      # 当值群安静下来
        for _ in range(50):                    # 等时钟把排队的回复发出去
            await asyncio.sleep(0.01)
        await transport.close()
        await asyncio.wait_for(loop, timeout=5)
        return transport

    transport = asyncio.run(main())
    texts = [text for _, text in transport.sent]
    assert any(text == "收到。" for text in texts), f"当值群的回复要发出去: {texts}"
    assert len(texts) >= 3, f"还应包括一句稍等与排队回复: {texts}"
    assert engine.focus.is_hot(sid(B)), "时钟应当把当值切到 B"
