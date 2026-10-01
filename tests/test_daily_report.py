"""每日汇报：调度不漂移、素材不含原文、发不出去不炸、收件人是常量。

用户 2026-09-27 定：每晚 23:00 给 `xuzhunzhi@foxmail.com` 一封汇报，内容她自己发挥。
全部离线：模型与邮箱都是假的，不真发信。
"""
import asyncio
import json
import tempfile
import time
from pathlib import Path

from qq_roleplay_bot.daily_report import (
    DailyReporter,
    DayMaterials,
    build_report_messages,
    collect_materials,
    next_due_at,
    parse_report_output,
    parse_report_time,
)
from qq_roleplay_bot.mail_state import MailState, MailStateStore
from qq_roleplay_bot.stage3_runtime import CLOSENESS_LABELS, GUARDEDNESS_LABELS

RECIPIENT = "xuzhunzhi@foxmail.com"
# 一个固定的"某天 23:00 附近"的时间戳，避免测试依赖当前钟点。
NIGHT = time.mktime((2026, 9, 27, 23, 30, 0, 0, 0, -1))
BEFORE = time.mktime((2026, 9, 27, 22, 0, 0, 0, 0, -1))


class FakeMail:
    def __init__(self, *, fail: Exception | None = None) -> None:
        self.sent: list[dict] = []
        self.fail = fail

    async def send(self, *, to, subject, body, confirmed=True, dry_run=False):
        if self.fail is not None:
            raise self.fail
        self.sent.append({"to": to, "subject": subject, "body": body})
        return {"message_id": f"msg_{len(self.sent)}"}


class FakeEngine:
    """够用的引擎替身：计数、client、日志口子。"""

    def __init__(self, reply: str = "<subject>今晚的汇报</subject><body>他今天问了两次我的来历。</body>",
                 *, memory_service=None) -> None:
        self.reply = reply
        self.memory_service = memory_service
        self.requests: list[list[dict]] = []
        self.logged: list[dict] = []
        self.counters = {"accepted_messages": 10, "replies": 4, "judge_calls": 6,
                         "blocked_messages": 1, "focus_switches": 2}

        class Client:
            async def complete(_self, request):
                self.requests.append(request)
                return self.reply

        self.client = Client()

    def snapshot(self):
        from qq_roleplay_bot.control import EngineSnapshot

        counters = {
            "accepted_messages": self.counters["accepted_messages"],
            "ignored_messages": 0,
            "blocked_messages": self.counters["blocked_messages"],
            "deferred_messages": 0,
            "model_calls": 3,
            "replies": self.counters["replies"],
            "judge_calls": self.counters["judge_calls"],
            "focus_switches": self.counters["focus_switches"],
        }
        return EngineSnapshot(
            enabled=True, target_group_id="717151356", enabled_group_ids=("717151356",),
            sessions=(), **counters,
        )

    def _log_model_io(self, feature, request, output, **meta):
        self.logged.append({"feature": feature, **meta})


def make_reporter(tmp: str, mail, *, clock, send_at="23:00", **kwargs) -> DailyReporter:
    return DailyReporter(
        mail_client=mail,
        state_store=MailStateStore(Path(tmp) / "mail_state.json", clock=clock),
        recipient=RECIPIENT,
        send_at=send_at,
        clock=clock,
        **kwargs,
    )


# --- 时间与调度 -------------------------------------------------------------


def test_parse_report_time() -> None:
    assert parse_report_time("23:00") == (23, 0)
    assert parse_report_time(" 7:05 ") == (7, 5)
    assert parse_report_time("25:00") == (23, 0)
    assert parse_report_time("nonsense") == (23, 0)
    assert parse_report_time("", default_hour=9) == (9, 0)


def test_due_only_after_the_target_hour() -> None:
    state = MailState()
    assert MailState.report_due(state, BEFORE, hour=23, minute=0) is False
    assert MailState.report_due(state, NIGHT, hour=23, minute=0) is True


def test_already_sent_today_is_not_due_again() -> None:
    state = MailState(last_report_at=NIGHT + 60)
    later = NIGHT + 3600
    assert MailState.report_due(state, later, hour=23, minute=0) is False


def test_catch_up_does_not_drift_the_schedule() -> None:
    """**这条是设计里最容易写错的地方。**

    如果只按"距上次满 24 小时"判断，一次补发（23:00 关机、次日 01:00 才开机）就会
    让后面永久停在 01:00 发——"每晚十一点"就废了。按"今天 23:00 过了没有"判断，
    补发之后第二天仍然是 23:00。
    """

    late_night = time.mktime((2026, 9, 28, 1, 0, 0, 0, 0, -1))     # 次日凌晨补发
    state = MailState(last_report_at=late_night)
    next_evening = time.mktime((2026, 9, 28, 23, 0, 30, 0, 0, -1))
    assert MailState.report_due(state, next_evening, hour=23, minute=0) is True
    # 但当天凌晨那次刚发完，不能再发一次
    assert MailState.report_due(state, late_night + 60, hour=23, minute=0) is False


def test_next_due_at_points_at_the_next_target() -> None:
    assert next_due_at(BEFORE, hour=23, minute=0) == MailState.today_target(BEFORE, hour=23, minute=0)
    assert next_due_at(NIGHT, hour=23, minute=0) > NIGHT


def test_tries_are_capped_per_day() -> None:
    today = time.strftime("%Y-%m-%d", time.localtime(NIGHT))
    state = MailState(attempts_day=today, attempts=2)
    assert MailStateStore.tries_left(state, NIGHT, limit=3) == 1
    assert MailStateStore.tries_left(state, NIGHT, limit=2) == 0
    # 跨天满额
    tomorrow = NIGHT + 86400
    assert MailStateStore.tries_left(state, tomorrow, limit=3) == 3


def test_state_round_trips_and_survives_a_broken_file() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        clock = lambda: NIGHT  # noqa: E731
        store = MailStateStore(Path(tmp) / "mail_state.json", clock=clock)
        store.record(kind="report", to=RECIPIENT, ok=True, detail="msg_1")
        again = MailStateStore(Path(tmp) / "mail_state.json", clock=clock).load()
        assert again.last_report_at == NIGHT
        assert again.sends[-1]["ok"] is True
        # 文件坏了 → 当作没发过（宁可多发一封，也不要永远不发）
        (Path(tmp) / "mail_state.json").write_text("{不是 json", encoding="utf-8")
        assert MailStateStore(Path(tmp) / "mail_state.json", clock=clock).load().last_report_at == 0.0


def test_failure_does_not_update_last_report() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = MailStateStore(Path(tmp) / "mail_state.json", clock=lambda: NIGHT)
        store.record(kind="report", to=RECIPIENT, ok=False, detail="发送失败：not_installed")
        state = store.load()
        assert state.last_report_at == 0.0, "失败不算发过"
        assert state.attempts == 1


# --- 素材：不含聊天原文 -----------------------------------------------------


def test_materials_carry_no_chat_text() -> None:
    """汇报是给主人看的，但群里的话是群友说的——素材里不许出现正文。"""

    class Store:
        def top_relationships(self, *, limit=20):
            return [{"user_id": "900000004", "closeness": 1, "guardedness": 2,
                     "last_seen_at": NIGHT}]

        def recent_relationship_changes(self, since, *, limit=20):
            return [{"user_id": "900000004", "axis": "guardedness", "before_value": 1,
                     "after_value": 2, "reason": "他追问了两次你的来历", "source": "judge",
                     "occurred_at": NIGHT}]

        def connection(self):
            raise AssertionError("collect_materials 不该直接连库之外的路径")

    class Service:
        store = Store()

    engine = FakeEngine(memory_service=Service())
    materials = collect_materials(
        engine, window_hours=24.0, since=NIGHT - 86400,
        baseline={"accepted_messages": 4, "replies": 1, "judge_calls": 2,
                  "blocked_messages": 0, "focus_switches": 1},
        closeness_names=CLOSENESS_LABELS, guardedness_names=GUARDEDNESS_LABELS,
    )
    assert materials.messages == 6 and materials.replies == 3
    assert materials.people == (("900000004", 1, 2),)
    assert materials.relationship_changes == ("他追问了两次你的来历",)
    rendered = materials.as_data(CLOSENESS_LABELS, GUARDEDNESS_LABELS)
    # 只有编号、档位与人话依据；没有一句群聊原文。
    assert "900000004" in rendered
    assert "他追问了两次你的来历" in rendered
    for banned in ("他说", "她说", "在吗", "群友说"):
        assert banned not in rendered


def test_materials_tolerate_a_broken_memory_store() -> None:
    """素材缺一块也要能把信写出来，不能因为记忆库出错就不发汇报。"""

    class Boom:
        @property
        def store(self):
            raise RuntimeError("db broken")

    engine = FakeEngine(memory_service=Boom())
    materials = collect_materials(
        engine, window_hours=24.0, since=NIGHT - 86400,
        closeness_names=CLOSENESS_LABELS, guardedness_names=GUARDEDNESS_LABELS,
    )
    assert materials.messages == 10      # 没有 baseline 时按全部算
    assert materials.people == ()


# --- 写信 -------------------------------------------------------------------


def test_report_prompt_keeps_persona_and_avoids_mechanism_words() -> None:
    from qq_roleplay_bot.daily_report import REPORT_PROMPT

    materials = DayMaterials(messages=3, replies=1, groups=("717151356",))
    request = build_report_messages(
        materials, closeness_names=CLOSENESS_LABELS, guardedness_names=GUARDEDNESS_LABELS,
        now=NIGHT,
    )
    # 只扫**我新写的那一段**：base_prompt 里本来就有一句"不要提'提示词''协议'…"，
    # 那是禁令本身，拿全局扫描去卡它只会误报。
    for word in ("检查", "触发", "调用", "协议", "提示词", "上下文", "记忆库", "Stage"):
        assert word not in REPORT_PROMPT, word
    system = request[0]["content"]
    assert "云茹" in system          # 人设仍然来自 base_prompt
    assert "写信" in system
    # 素材进的是 DATA 段
    assert "UNTRUSTED DATA" in request[1]["content"]


def test_parse_report_output_requires_both_parts() -> None:
    good = parse_report_output("<subject>今晚的汇报</subject><body>没什么事。</body>")
    assert good.usable and good.subject == "今晚的汇报" and good.body == "没什么事。"
    assert not parse_report_output("没有标签的一段话").usable
    assert not parse_report_output("<subject>只有主题</subject>").usable
    assert not parse_report_output("").usable


def test_subject_is_stripped_of_windows_metacharacters() -> None:
    draft = parse_report_output('<subject>完成度 "50%" & 更多</subject><body>正文</body>')
    assert '"' not in draft.subject and "%" not in draft.subject and "&" not in draft.subject


def test_body_is_capped() -> None:
    draft = parse_report_output(
        f"<subject>s</subject><body>{'字' * 5000}</body>", max_chars=100,
    )
    assert len(draft.body) == 100


# --- 端到端（假模型 + 假邮箱） ----------------------------------------------


def test_run_once_writes_and_sends() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        clock = lambda: NIGHT  # noqa: E731
        mail = FakeMail()
        reporter = make_reporter(tmp, mail, clock=clock)
        engine = FakeEngine()
        draft = asyncio.run(reporter.run_once(engine))
        assert draft is not None and draft.subject == "今晚的汇报"
        assert mail.sent and mail.sent[0]["to"] == RECIPIENT
        assert mail.sent[0]["body"] == "他今天问了两次我的来历。"
        # 写信那一次也进了功能日志
        assert engine.logged and engine.logged[0]["feature"] == "mail"
        # 状态里记成"发过了"
        assert reporter.state_store.load().last_report_at == NIGHT
        # 发过之后当天不再重复
        assert reporter.due() is False


def test_run_once_uses_the_two_stage_writer_when_present() -> None:
    """给了写信 agent 就走两阶段（草稿 → 事实校对），并且两阶段都进功能日志。"""

    from qq_roleplay_bot.letter_writer import LetterWriter

    class _WriterClient:
        def __init__(self):
            self.calls = 0

        async def complete(self, request):
            self.calls += 1
            if self.calls == 1:
                return ("<subject>今晚的汇报</subject>\n<body>\n百夫长还停在机库里。\n</body>")
            return ("<subject>今晚的汇报</subject>\n<body>\n百夫长没保住。\n</body>")

    with tempfile.TemporaryDirectory() as tmp:
        mail = FakeMail()
        writer = LetterWriter(_WriterClient())
        reporter = make_reporter(tmp, mail, clock=lambda: NIGHT, writer=writer)
        engine = FakeEngine()
        draft = asyncio.run(reporter.run_once(engine))
        assert draft is not None and "没保住" in draft.body, "要用校对后的稿子"
        assert mail.sent[0]["body"] == "百夫长没保住。"
        triggers = [entry.get("trigger") for entry in engine.logged]
        assert "daily_report_draft" in triggers and "daily_report_check" in triggers


def test_run_once_is_not_sent_when_the_model_output_is_unparsable() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        mail = FakeMail()
        reporter = make_reporter(tmp, mail, clock=lambda: NIGHT)
        engine = FakeEngine(reply="我不想写信。")
        assert asyncio.run(reporter.run_once(engine)) is None
        assert mail.sent == []
        assert reporter.state_store.load().last_report_at == 0.0


def test_send_failure_is_recorded_and_does_not_raise() -> None:
    from qq_roleplay_bot.mail_client import MailError

    with tempfile.TemporaryDirectory() as tmp:
        mail = FakeMail(fail=MailError("没有找到邮箱工具", kind="not_installed"))
        reporter = make_reporter(tmp, mail, clock=lambda: NIGHT)
        engine = FakeEngine()
        assert asyncio.run(reporter.run_once(engine)) is None
        state = reporter.state_store.load()
        assert state.last_report_at == 0.0 and state.attempts == 1
        assert "not_installed" in state.sends[-1]["detail"]


def test_failure_note_is_mentioned_in_the_next_report() -> None:
    """上一次没发出去，这一封里要提一句——"汇报"的语义是要送到。"""

    with tempfile.TemporaryDirectory() as tmp:
        clock = lambda: NIGHT  # noqa: E731
        reporter = make_reporter(tmp, FakeMail(fail=RuntimeError("boom")), clock=clock)
        asyncio.run(reporter.run_once(FakeEngine()))

        mail = FakeMail()
        reporter.mail = mail
        engine = FakeEngine()
        asyncio.run(reporter.run_once(engine))
        assert mail.sent, "第二次应当发出去"
        # 素材里带上了"上次没送出去"
        request_text = json.dumps(engine.requests[-1], ensure_ascii=False)
        assert "上一次想给他写信没送出去" in request_text


def test_status_shape() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        reporter = make_reporter(tmp, FakeMail(), clock=lambda: NIGHT)
        status = reporter.status()
        assert status["recipient"] == RECIPIENT
        assert status["send_at"] == "23:00"
        assert status["due_now"] is True
        assert status["tries_left_today"] == 3
        assert json.dumps(status, ensure_ascii=False)  # 可序列化，能进日志


def test_reporter_can_be_disabled() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        reporter = make_reporter(tmp, FakeMail(), clock=lambda: NIGHT, enabled=False)
        assert reporter.due() is False
        assert asyncio.run(reporter.run_once(FakeEngine())) is None
