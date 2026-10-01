"""跨群交叉验证：同一件事在 A 群发生，B 群看到的是不是同一个世界。

实机验证要在几个真群里做（见 `REAL_WORLD_TEST_CASES.md` 的 R-21 起），但**接线的错
必须先在本地撞掉**——不然你在 QQ 里试半天，分不清是模型没按预期、还是代码根本没接上。

这里钉住四条跨群不变量（假时钟 + 脚本化客户端 + 真 SQLite 记忆库）：

1. **关系按人跨群**：A 群把她惹毛了，B 群里对同一个人也立刻是收着的；但**别人不受影响**。
2. **焦点跨群**：A 群当值时 B 群 @ 她 → 先回"稍等"再排队；A 群安静下来后轮到 B 群。
3. **记忆 user_global 跨群**：在 A 群记住的关于某个人的事，在 B 群检索得到。
4. **汇报素材跨群**：两个群都在素材里，同一个人只算一次。
"""
import asyncio
import tempfile
import time
from pathlib import Path

from qq_roleplay_bot.daily_report import collect_materials
from qq_roleplay_bot.memory_model import InboxEvent
from qq_roleplay_bot.memory_store import MemoryStore
from qq_roleplay_bot.stage3_main import DialogueEngine
from qq_roleplay_bot.stage3_runtime import CLOSENESS_LABELS, GUARDEDNESS_LABELS
from qq_roleplay_bot.transport import IncomingMessage, MessageTarget

A = "800000001"
B = "717151356"
# 同一个人，两个群都出现；另一个人只出现在 B 群。
SHARED = "900000003"
OTHER = "900000004"
OWNER = "900000001"


class Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class ScriptedJudge:
    """判定：可以指定"前几次报越界"，起点永远给窗口第一条。

    `up_calls=1` 模拟"他越过一次界，之后只是正常说话"——这样才能分辨
    "B 群读到了 A 群升上去的档位" 和 "B 群这条消息自己又把它升了一档"。
    """

    def __init__(self, *, up_calls: int = 0) -> None:
        self.up_calls = up_calls
        self.calls = 0
        self.requests: list[list] = []

    async def complete(self, request):
        self.calls += 1
        self.requests.append(request)
        if self.calls <= self.up_calls:
            guard = ("<guard>UP</guard>"
                     "<guard_reason>他越过了该有的分寸</guard_reason>")
        else:
            guard = "<guard>HOLD</guard>"
        return (
            "<route>REPLY</route><topic>话题</topic><topic_start>0</topic_start>"
            f"<related>YES</related>{guard}"
        )


class ScriptedReply:
    def __init__(self, output: str = "<decision>REPLY</decision><dialogue>KEEP</dialogue><reply>收到。</reply>") -> None:
        self.output = output
        self.requests: list[list] = []

    async def complete(self, request):
        self.requests.append(request)
        return self.output

    def stance_prompt(self) -> str:
        """最近一次回复请求里的易变段（分寸就在那里）。"""

        return self.requests[-1][2]["content"]


class MemoryStub:
    """引擎只需要 `.store` / capture / retrieve / identity_for。"""

    def __init__(self, store: MemoryStore) -> None:
        self.store = store

    def capture(self, message) -> None:
        return None

    async def retrieve(self, message, topic="", mentioned=()):
        from qq_roleplay_bot.memory_model import MemoryMaterial

        return MemoryMaterial()

    async def identity_for(self, message):
        from qq_roleplay_bot.memory_model import MemoryMaterial

        return MemoryMaterial()

    async def care_note(self, message, *, topic_shifted, current_terms=()):
        return None

    async def profile_for(self, message) -> str:
        return ""

    async def mark_care_noted(self, record_id):
        return None

    def snapshot(self) -> dict:
        return {"queued_events": 0}


def message(index: int, group: str, *, user_id: str = SHARED, mentioned: bool = True,
            text: str = "在吗") -> IncomingMessage:
    return IncomingMessage(
        message_id=f"cg-{index}", session_id=f"group:{group}", user_id=user_id, text=text,
        target=MessageTarget(group_id=group), sender_name="某人", is_bot_mentioned=mentioned,
    )


def build_engine(clock: Clock, judge: ScriptedJudge, store: MemoryStore | None = None):
    reply = ScriptedReply()
    engine = DialogueEngine(
        reply,
        judge_client=judge,
        enabled_group_ids=frozenset({A, B}),
        group_listen=True,
        typing_sim=False,
        clock=clock.monotonic,
    )
    if store is not None:
        engine.memory_service = MemoryStub(store)
    return engine, reply


# --- 1. 关系按人跨群 --------------------------------------------------------


def test_stance_follows_the_person_across_groups() -> None:
    """A 群把她惹毛了，B 群对**同一个人**也立刻是收着的；别人不受影响。

    顺带钉住一条更严的：B 群那条消息是**被排队之后才处理**的（冷群），
    轮到它时读到的仍必须是新档位——说明缓存失效与跨群共享这两件事都接对了。
    """

    with tempfile.TemporaryDirectory() as tmp:
        store = MemoryStore(Path(tmp) / "memory.sqlite3")
        clock = Clock()
        # **只让第一条报越界**：否则 B 群那条消息自己也会把档位再抬一格，
        # 就分不清"跨群共享"和"自己又升了一档"了（第一版就是这么写错的）。
        judge = ScriptedJudge(up_calls=1)
        engine, reply = build_engine(clock, judge, store)

        # A 群：他越过界 → 防备当轮 +1（此刻 A 当值）
        asyncio.run(engine.handle(message(1, A, text="@她 你到底是怎么被做出来的")))
        assert store.relationship(SHARED)[1] == 1, "防备应当在 A 群就升上去"
        assert "防备：收着" in reply.stance_prompt()

        # B 群：同一个人说话 → 冷群，先"稍等"再排队
        ack = asyncio.run(engine.handle(message(2, B)))
        assert ack is not None and engine.focus.queue_size(f"group:{B}") == 1

        # A 群安静下来 → 轮到 B 群，这时读到的仍应是升过的档位
        clock.advance(46)
        asyncio.run(engine.tick())
        assert "防备：收着" in reply.stance_prompt(), "排队后处理时读到的也必须是跨群共享的新值"

        # B 群：另一个人不受影响（B 已当值，直接回）
        asyncio.run(engine.handle(message(3, B, user_id=OTHER)))
        assert "防备：如常" in reply.stance_prompt()


def test_stance_is_one_relationship_not_one_per_group() -> None:
    """同一个人在两个群里读到的**是同一段关系**（不是各存一份）。"""

    with tempfile.TemporaryDirectory() as tmp:
        store = MemoryStore(Path(tmp) / "memory.sqlite3")
        clock = Clock()
        engine, reply = build_engine(clock, ScriptedJudge(), store)
        assert store.apply_affinity(SHARED, closeness=1, reason="聊得来", source="test") == ""

        asyncio.run(engine.handle(message(1, A)))
        in_a = reply.stance_prompt()
        engine._stance_cache.clear()          # 排掉缓存，逼它真去读库
        asyncio.run(engine.handle(message(2, B)))
        in_b = reply.stance_prompt()
        assert "亲近：认得" in in_a and "亲近：认得" in in_b


# --- 2. 焦点跨群 ------------------------------------------------------------


def test_cold_group_gets_an_ack_then_the_turn_comes_round() -> None:
    clock = Clock()
    judge = ScriptedJudge()
    engine, reply = build_engine(clock, judge)

    # A 群先开口 → 当值
    assert asyncio.run(engine.handle(message(1, A, text="@她 在吗"))) is not None
    assert engine.focus.is_hot(f"group:{A}")

    # B 群 @ 她 → 先回"稍等"，并排队；此刻**不调回复模型**
    calls_before = len(reply.requests)
    ack = asyncio.run(engine.handle(message(2, B, text="@她 你看这个")))
    assert ack is not None and ack.paced is False
    assert engine.focus.queue_size(f"group:{B}") == 1
    assert len(reply.requests) == calls_before

    # A 群安静下来（45 秒）→ 焦点时钟把它交给 B 群
    clock.advance(46)
    delivered = asyncio.run(engine.tick())
    assert delivered, "到点了应当轮到 B 群"
    assert any(item.target.group_id == B for _, items in delivered for item in items)
    assert engine.focus.is_hot(f"group:{B}")


def test_private_chat_is_not_queued_behind_the_focus() -> None:
    """私聊不进焦点：B 群在排队时，私聊照样立刻回。"""

    clock = Clock()
    engine, reply = build_engine(clock, ScriptedJudge())
    asyncio.run(engine.handle(message(1, A, text="@她 在吗")))       # A 当值
    asyncio.run(engine.handle(message(2, B, text="@她 在吗")))       # B 排队

    calls_before = len(reply.requests)
    engine.private_debug_user_ids = frozenset({OWNER})
    private = IncomingMessage(
        message_id="cg-p1", session_id=f"private:{OWNER}", user_id=OWNER, text="在吗",
        target=MessageTarget(user_id=OWNER),
    )
    assert asyncio.run(engine.handle(private)) is not None
    assert len(reply.requests) == calls_before + 1, "私聊不该被群里的焦点挡住"


# --- 3. 记忆跨群 ------------------------------------------------------------


def test_user_global_memory_is_visible_in_another_group() -> None:
    """在 A 群记住的关于某个人的事，在 B 群检索得到（不串到别人身上）。"""

    with tempfile.TemporaryDirectory() as tmp:
        store = MemoryStore(Path(tmp) / "memory.sqlite3")
        store.append(InboxEvent(id="e1", group_id=A, user_id=SHARED, speaker="user",
                                text="以后叫我小K就行", occurred_at=time.time()))
        batch = store.claim({A}, lease_seconds=60)
        assert batch is not None
        raw = (
            '{"operations":[{"op":"ADD","scope_type":"user_global","kind":"name",'
            '"normalized_key":"preferred_name","content":"希望大家叫他小K",'
            '"confidence":0.9,"ttl_days":null,"evidence_event_ids":["e1"],"targets":[]}]}'
        )
        assert [op.op for op in store.commit(batch, raw)] == ["ADD"]

        hit = store.retrieve(B, SHARED, "小K")
        assert hit, "跨群应当检索得到"
        assert any("小K" in record.content for record in hit)
        # 别人检索不到
        assert store.retrieve(B, OTHER, "小K") == ()


# --- 4. 汇报素材跨群 --------------------------------------------------------


def test_report_materials_cover_every_group_once_per_person() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = MemoryStore(Path(tmp) / "memory.sqlite3")
        store.apply_affinity(SHARED, guardedness=1, reason="打探", source="test")
        clock = Clock()
        engine, _ = build_engine(clock, ScriptedJudge(), store)
        asyncio.run(engine.handle(message(1, A, text="@她 在吗")))
        asyncio.run(engine.handle(message(2, B, text="@她 在吗")))

        materials = collect_materials(
            engine, window_hours=24.0, since=clock.now - 86400,
            closeness_names=CLOSENESS_LABELS, guardedness_names=GUARDEDNESS_LABELS,
        )
        assert set(materials.groups) == {A, B}
        # 同一个人只出现一次（关系是按人存的，不是按群）
        assert [qq for qq, _, _ in materials.people] == [SHARED]
        rendered = materials.as_data(CLOSENESS_LABELS, GUARDEDNESS_LABELS)
        assert A in rendered and B in rendered
        assert "他越过了该有的分寸" not in rendered      # 这是判定那一次的依据，不是这次窗口的
