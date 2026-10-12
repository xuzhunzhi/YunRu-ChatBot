"""每天这封信这条线：她"这段时间到底经历了什么"（2026-10-11 用户说的甲/乙/丙/丁）。

要治的病（用户原话：*"还有就是mail agent的幻觉很严重啊"*）：

- 素材里的"你收到过 N 条消息"取自引擎的**进程内计数**，进程一重启就归零，
  于是她连着好几天的信都写"两个群里没人开口"；同一时期群里每天上千条。
- 窗口长度直接印成"最近 192 小时"，她读不懂，就自己换算成"这几天""九天"。
- 写好的信发不出去就没了，第二天从零重写一封（同一段日子被写两遍）。

用户同一天还纠了一次方向：*"我认为没有必要让云茹强调自己看了多少消息回了多少消息啊，
我只是想看到云茹自己想的一些想法或者碎碎念或者什么看法，不要搞的跟实习生写日报一样"*
——所以素材里**一个数目都不印**（热闹/安静也只给一句语气），她写的是自己的想法。

所以这个文件钉五件事：

| 谁 | 钉什么 |
| --- | --- |
| 丙 | `chat_window` 只**数**不抄（字段就那九个，正文一个字都出不去） |
| 甲 | 写好了没送出去的信留着，下次**只重发不重写** |
| 乙 | 素材按真实间隔说人话；数不出来就什么都不说，**不许把 0 当事实** |
| 丁 | 写信提示词要的是她的念头、明禁报数目，且没有机制词 |
| — | 素材里不出现"多少条消息、回了多少、哪个群最热闹"这类日报口径 |

**这条读口是用户 2026-10-11 明确授权的只读例外**（原话：*"读取为啥不给读取，
只读又不影响"*）：允许日报从聊天日志里**数**出发过什么。范围只有计数——
没有正文、没有原话、没有昵称，也没有记忆库与知识库的入口。
"""
import asyncio
import contextlib
import json
import logging
import tempfile
from pathlib import Path
from types import SimpleNamespace

from qq_roleplay_bot.chat_window import ChatWindowFacts, collect_window_facts
from qq_roleplay_bot.plugins import ReportSeams
from qq_roleplay_bot.plugins.mail import daily_report as dr
from qq_roleplay_bot.plugins.mail.mail_state import (
    PENDING_REPORT_MAX_AGE,
    MailStateStore,
)
from qq_roleplay_bot.prompt_guard import scan_persona_text

#: 群里有人说过的一句话。**它一个字都不该出现在聚合结果里。**
SECRET = "我今晚打算去把他家那只猫偷走"

NOW = 1_800_000_000.0


@contextlib.contextmanager
def _no_logs():
    """把日志静音：接缝炸掉时它会打一条 warning + 堆栈（那是给运维看的）。

    测试要断言的是"它不抛、返回 None"，不是看它打印了什么；不清音的话
    正常的一次全绿跑也会吐一屏 traceback，掩盖真正的失败。
    """

    logging.disable(logging.CRITICAL)
    try:
        yield
    finally:
        logging.disable(logging.NOTSET)


def _write_log(path: Path, rows: list[dict]) -> Path:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    return path


def _rows() -> list[dict]:
    """一份小日志：三条进来的、一条她回的（切成两段）、一次表情回应。"""

    return [
        {"at": NOW - 600, "direction": "in", "session_id": "group:1", "group_id": "1",
         "user_id": "1001", "message_id": "a1", "body": SECRET, "has_media": False},
        {"at": NOW - 500, "direction": "out", "session_id": "group:1", "group_id": "1",
         "user_id": "1001", "message_id": "b1", "part": 1, "total": 2, "body": "嗯"},
        {"at": NOW - 490, "direction": "out", "session_id": "group:1", "group_id": "1",
         "user_id": "1001", "message_id": "b1", "part": 2, "total": 2, "body": "知道了"},
        {"at": NOW - 400, "direction": "out", "session_id": "group:1", "group_id": "1",
         "user_id": "1001", "kind": "reaction", "message_id": "c1", "body": "[表情]"},
        {"at": NOW - 300, "direction": "in", "session_id": "group:1", "group_id": "1",
         "user_id": "1002", "message_id": "a2", "reply_to": "b1", "body": "接着说说",
         "has_media": True},
        {"at": NOW - 200, "direction": "in", "session_id": "private:1003",
         "user_id": "1003", "message_id": "a3", "body": "云茹在吗"},
    ]


# --- 丙：窗口聚合只数不抄 -----------------------------------------------------


def test_window_facts_counts_messages_replies_and_calls():
    """条数口径：多段消息算一次开口、表情回应不算开口、私聊也算人来过话。"""

    with tempfile.TemporaryDirectory() as tmp:
        log = _write_log(Path(tmp) / "chat.jsonl", _rows())
        facts = collect_window_facts(log, since=NOW - 3600)

    assert facts is not None, "日志在，聚合就该给一份事实"
    assert facts.incoming == 3, "进来的三条：群里两条、私聊一条"
    assert facts.hers == 1, "她那条消息切成两段，只算开口一次；表情回应不算开口"
    assert facts.addressed == 2, "一条是接着她的话说的，一条叫了她的名字"
    assert facts.media == 1, "带图的只有一条（表情回应是发出去的，不是'收到的'）"
    assert facts.groups == (("group:1", 2),), "私聊不算群；群只报会话与条数"
    assert {qq for qq, _ in facts.people} == {"1001", "1002", "1003"}
    assert facts.since == NOW - 3600, "窗口起点原样带回来，好核对数的是哪一段"


def test_window_facts_never_carry_any_text():
    """**正文一个字都不出去**：这是"只读计数"这条授权的边界。"""

    with tempfile.TemporaryDirectory() as tmp:
        log = _write_log(Path(tmp) / "chat.jsonl", _rows())
        facts = collect_window_facts(log, since=NOW - 3600)

    assert facts is not None
    flat = " ".join(
        str(value) for value in (
            facts.since, facts.incoming, facts.hers, facts.addressed, facts.media,
            facts.groups, facts.people, facts.covers_window,
        )
    )
    assert SECRET not in flat, "别人说的话不许跟着计数一起出来"
    assert "接着说说" not in flat, "连一句短话也不许出来"
    assert set(ChatWindowFacts.__dataclass_fields__) == {
        "since", "incoming", "hers", "addressed", "media", "groups", "people",
        "covers_window",
    }, "字段清单被改动了：加字段前先读 chat_window 开头那段边界与这条测试"


def test_window_facts_says_when_the_window_is_incomplete():
    """轮转掉的部分数不出来：那就标"读不全"，**不能假装只有这么多**。"""

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "chat.jsonl"
        _write_log(path, [row for row in _rows() if row["at"] >= NOW - 500])
        partial = collect_window_facts(path, since=NOW - 3600)
        _write_log(path, _rows() + [{"at": NOW - 4000, "direction": "in",
                                     "session_id": "group:1", "user_id": "1001",
                                     "message_id": "a0", "body": "早"}])
        whole = collect_window_facts(path, since=NOW - 3600)
        missing = collect_window_facts(Path(tmp) / "nope.jsonl", since=NOW - 3600)

    assert partial is not None and partial.covers_window is False, (
        "最早一条比窗口起点晚 = 这段读不全（可能是轮转掉的）"
    )
    assert whole is not None and whole.covers_window is True, "最早一条早于窗口起点 = 读得全"
    assert missing is None, "日志不在就是 None，交给调用方按'说不清'处理"


# --- 甲：写好了没发出去的那封信 ----------------------------------------------


class _Snapshot:
    accepted_messages = 11
    replies = 3
    judge_calls = 5
    blocked_messages = 1
    focus_switches = 2
    enabled_group_ids = ("1",)


class _Client:
    """假的模型通道。`boom` 用来证明"重发那封"根本没叫模型。"""

    def __init__(self, raw: str = "", boom: bool = False) -> None:
        self.raw = raw
        self.boom = boom
        self.calls = 0

    async def complete(self, request):
        self.calls += 1
        if self.boom:
            raise AssertionError("这条路上不该再叫一次模型：重发那封不该重写")
        return self.raw


class _Mail:
    """假的发信口。`fail` 为真时抛错（模拟令牌过期/断网）。"""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.sent: list[dict] = []

    async def send(self, *, to, subject, body):
        if self.fail:
            raise RuntimeError("SMTP 连不上")
        self.sent.append({"to": to, "subject": subject, "body": body})
        return {"message_id": f"m{len(self.sent)}"}


def _reporter(store: MailStateStore, mail, *, now: float = NOW):
    return dr.DailyReporter(
        mail_client=mail,
        state_store=store,
        recipient="owner@example.com",
        send_at="23:00",
        enabled=True,
        max_chars=1500,
        max_tries=3,
        writer=None,
        clock=lambda: now,
    )


PERSONA_TEXT = "（测试用的信人格正文）"
FACTS_TEXT = "（测试用的事实表：她读过仪表盘）"


def _seams(client, *, letters=None, facts=None, persona=PERSONA_TEXT, reader=None) -> ReportSeams:
    """生产接缝的那一份（外加只读记忆口）。

    2026-10-12 起两处变了：`memory_service`（可写的工程件）换成只读的 `memory_read`；
    信人格与事实表也走接缝——插件不许再 `from ...base_prompt import BASE_PROMPT`。
    `persona=None` 就是"核心那边读不到"：那种情况下一封信都不该发出去。
    """

    return ReportSeams(
        snapshot=lambda: _Snapshot(),
        client=client,
        log_model_io=None,
        note_letter=(letters.append if letters is not None else None),
        memory_read=reader,
        facts=facts,
        letter_persona=(lambda: persona),
        letter_facts=lambda: FACTS_TEXT,
    )


RAW = "<subject>今天</subject>\n<body>群里今天挺热闹，我跟几个人说了话。</body>"


def test_a_letter_that_failed_to_send_is_resent_not_rewritten():
    """甲：当晚没送出去的那封留着，第二天**原样**再送一次。"""

    with tempfile.TemporaryDirectory() as tmp:
        store = MailStateStore(Path(tmp) / "mail_state.json")
        broken = _Mail(fail=True)
        client = _Client(RAW)
        letters: list[dict] = []
        reporter = _reporter(store, broken)

        first = asyncio.run(reporter.run_once(_seams(client, letters=letters)))

        assert first is None, "发失败了就该返回 None"
        pending = store.load().pending_report
        assert pending.get("body") == "群里今天挺热闹，我跟几个人说了话。", (
            "发之前就要把它存下来：发信那一刻断电也不该把信弄丢"
        )
        status = reporter.status()
        assert status["pending_report"].get("subject") == "今天", (
            "超管问状态时要能看到'有一封写好了还没寄出去'"
        )

        # 第二天：网络好了。**不叫模型**，直接把这封送出去。
        working = _Mail()
        retry = _reporter(store, working)
        second = asyncio.run(retry.run_once(_seams(client, letters=letters)))

    assert client.calls == 1, "第一次写了一封；重发那次不该再叫模型"
    assert second is not None and second.subject == "今天"
    assert second.body == "群里今天挺热闹，我跟几个人说了话。", "原样送出去，一个字都不改"
    assert working.sent[0]["subject"] == "今天"
    assert store.load().pending_report == {}, "送出去了就该把待发的清掉"
    assert letters and letters[-1]["body"] == second.body, "发出去的信要记一笔给她自己"


def test_a_stale_pending_letter_is_dropped_and_rewritten():
    """过期（超过三天）就不再补发：它写的那个日子已经过去了。"""

    with tempfile.TemporaryDirectory() as tmp:
        store = MailStateStore(Path(tmp) / "mail_state.json")
        store.save_pending_report(
            day="2026-10-01", subject="很久以前", body="那是很久以前的事了。",
            now=NOW - PENDING_REPORT_MAX_AGE - 10,
        )
        mail = _Mail()
        client = _Client(RAW)
        reporter = _reporter(store, mail)

        draft = asyncio.run(reporter.run_once(_seams(client)))
        left = store.load().pending_report

    assert client.calls == 1, "过期的待发信要丢掉，重新按新的一天写"
    assert left == {}, "新的那封发出去了，状态里就不该再留待发"
    assert draft is not None and draft.subject == "今天"
    assert mail.sent[0]["body"] == "群里今天挺热闹，我跟几个人说了话。"


# --- 乙：素材说人话，数不出来就别报数目 --------------------------------------


def test_window_label_talks_about_the_real_gap():
    """窗口长度说人话：一天以内是"最近一天"，更久先说清隔了多少天。"""

    assert dr.window_label(24.0) == "这是最近一天里的事"
    assert dr.window_label(30.0) == "这是最近一天里的事", "一天半还算一天，别写成 30 小时"
    assert dr.window_label(31.0) == "距你上一次写信已经 2 天，下面是这中间的事"
    assert dr.window_label(192.0) == "距你上一次写信已经 8 天，下面是这中间的事"


def test_materials_never_print_how_much_she_did():
    """素材里不印数目：用户 2026-10-11——*"不要搞的跟实习生写日报一样"*。

    群里热闹还是安静只给一句话的语气；数不出来就干脆什么都没说（不猜、也不报 0）。
    """

    busy = dr.DayMaterials(
        window_hours=24.0, messages=310, replies=21, addressed=7, media=12,
        group_counts=(("group:1", 300),), counts_reliable=True,
        last_failure="发送失败：token_expired",
    ).as_data((), ())
    assert busy.startswith("（这是最近一天里的事）"), "第一行先说这是哪一段的事"
    assert "这段时间群里一直挺热闹。" in busy, "该给的只是一个语气，不是一个数"
    for forbidden in ("你收到过", "开口回了", "条消息", "最热闹", "个群", "专门叫你的"):
        assert forbidden not in busy, f"素材里不许出现「{forbidden}」——那是日报口径"
    assert "上一次想给他写信没寄出去。这件事也要提一句。" in busy, "上次没寄出去要提一句"
    assert "token_expired" not in busy, "失败原因是机器字眼，不许进她的信"

    quiet = dr.DayMaterials(window_hours=24.0, messages=3, counts_reliable=True).as_data((), ())
    assert "这段时间群里挺安静的。" in quiet

    unknown = dr.DayMaterials(
        window_hours=100.0, messages=0, counts_reliable=False,
    ).as_data((), ())
    assert unknown.startswith("（距你上一次写信已经 4 天"), "读不全的那段也要说清隔了多久"
    for word in ("热闹", "安静", "记不清", "0 条"):
        assert word not in unknown, "读不全就别下判断，也别把 0 当事实"


def test_the_blocked_line_carries_no_count():
    """"有人提过我没接的事"只说是哪一类：那个次数来自进程内计数。"""

    data = dr.DayMaterials(
        window_hours=24.0, counts_reliable=True,
        blocked={"有人提过我没接的事": 4}, focus_switches=7,
    ).as_data((), ())
    assert "有人提过我没接的事。" in data, "键本身就是人话，别再套一层引子"
    assert "4 次" not in data
    assert "换过几次对象" not in data, "忙不忙的话也不进素材——她要写的是自己的想法"


# --- "上次没寄出去"这句话什么时候才该出现（2026-10-12 修） ----------------------


class _Writer:
    """假的写信口（两阶段那条路）：把收到的素材留下来给测试看。"""

    enabled = True

    def __init__(self) -> None:
        self.materials: list = []
        self.seen: dict = {}
        self.last_skip = ""

    async def write(self, materials, *, persona="", facts="", closeness_names=(),
                    guardedness_names=(), lookup=None, now=None):
        self.materials.append(materials)
        self.seen = {"persona": persona, "facts": facts, "lookup": lookup}
        return "今天", "群里今天挺热闹，我跟几个人说了话。"


def test_only_the_latest_report_send_decides_whether_she_mentions_it():
    """一次失败不该让后面每一封信都提"上次没寄出去"。

    真实数据（`run/data/mail_state.json`）：10-10 夜里连着三条
    `发送失败：credential_lock_unavailable`，10-11 那封其实寄成了，可 10-11、10-12
    两天的素材里都有那句"这件事也要提一句"——旧算法翻最近 5 条、见到失败就写，
    根本不看最新那条是不是成功。用户看到的就是连着两天同一件事（"这叽里咕噜说啥呢"）。
    """

    failed = {"kind": "report", "ok": False, "detail": "发送失败：credential_lock_unavailable"}
    timed_out = {"kind": "report", "ok": False, "detail": "发送失败：timeout"}
    sent = {"kind": "report", "ok": True, "detail": "ok"}

    assert dr.last_failure_note([]) == "", "一封都没寄过，没什么可提的"
    assert dr.last_failure_note(None) == ""
    assert dr.last_failure_note([failed, failed, failed, sent]) == "", (
        "最新那封寄成了：更早的失败不该再让这封信提'上次没寄出去'"
    )
    assert dr.last_failure_note([sent, failed, failed, timed_out]) == "发送失败：timeout", (
        "最新那封没寄成：这句话要出来，而且报的是最新那一次"
    )
    assert dr.last_failure_note([sent, failed, {"kind": "manual", "ok": False}]) == (
        "发送失败：credential_lock_unavailable"
    ), "只看 report 这条线：别的记录不能替汇报作证"
    assert dr.last_failure_note([{"kind": "manual", "ok": True}]) == "", "没有 report 记录就当没提过"
    assert dr.last_failure_note([sent, failed, "不是字典"]) == "发送失败：credential_lock_unavailable", (
        "状态文件里混进怪东西也不该炸"
    )


def test_a_sent_letter_does_not_tell_her_to_mention_the_old_one():
    """接上：上一封寄成了，素材里就不许出现那句话（走两阶段那条路验一遍）。"""

    with tempfile.TemporaryDirectory() as tmp:
        store = MailStateStore(Path(tmp) / "mail_state.json")
        for _ in range(3):
            store.record(kind="report", to="owner@example.com", ok=False,
                         detail="发送失败：credential_lock_unavailable")
        store.record(kind="report", to="owner@example.com", ok=True, detail="ok")

        writer = _Writer()
        reporter = _reporter(store, _Mail())
        reporter.writer = writer
        draft = asyncio.run(reporter.run_once(_seams(_Client(), letters=[])))

    assert writer.materials, "两阶段那条路该把素材交给写信口"
    assert writer.materials[0].last_failure == "", "上一封寄成了：别再提'上次没寄出去'"
    assert "上一次想给他写信没寄出去" not in writer.materials[0].as_data((), ())
    assert draft is not None and draft.subject == "今天"


def test_a_letter_that_really_failed_still_gets_mentioned():
    """反过来也要钉住：上一封真没寄出去，这句话必须出来。"""

    with tempfile.TemporaryDirectory() as tmp:
        store = MailStateStore(Path(tmp) / "mail_state.json")
        store.record(kind="report", to="owner@example.com", ok=True, detail="ok")
        store.record(kind="report", to="owner@example.com", ok=False,
                     detail="发送失败：credential_lock_unavailable")

        writer = _Writer()
        reporter = _reporter(store, _Mail())
        reporter.writer = writer
        asyncio.run(reporter.run_once(_seams(_Client(), letters=[])))

    data = writer.materials[0].as_data((), ())
    assert "上一次想给他写信没寄出去。这件事也要提一句。" in data
    assert "credential_lock_unavailable" not in data, "失败原因是机器字眼，不进她的信"


# --- 接缝与提示词 -------------------------------------------------------------


def test_report_seams_window_facts_is_a_closed_gate():
    """没有接缝 / 数不出来 / 抛异常 —— 一律 None，**绝不拿引擎计数补位**。"""

    assert ReportSeams().window_facts(0.0) is None, "没给就是没有，不许猜别的形状"

    def boom(since):
        raise RuntimeError("数不出来")

    # 这条路上接缝会打一条 warning + 堆栈（那是给运维看的）；这里断言的是"它不抛"。
    with _no_logs():
        assert ReportSeams(facts=boom).window_facts(0.0) is None, "聚合炸了只当说不清，不抛"

    sentinel = object()
    assert ReportSeams(facts=lambda since: sentinel).window_facts(0.0) is sentinel
    assert ReportSeams(facts="不是函数").window_facts(0.0) is None


def test_report_prompt_asks_for_her_own_thoughts():
    """提示词：要她自己的想法（机制词一个都不许有），不要工作量。"""

    assert scan_persona_text(dr.REPORT_PROMPT) == [], "prompt_guard 机制词表过一遍"
    assert "你脑子里在转的那些东西" in dr.REPORT_PROMPT, "主题得是她的想法，不是她干了什么"
    for words in ("别报数目", "别照着它念", "不要把他人的原话抄给他看",
                  "不是……而是……", "落款就写你自己的名字"):
        assert words in dr.REPORT_PROMPT, f"{words} 这条规矩被删了"


def test_super_mail_send_now_hands_the_seams_not_the_engine():
    """`/super mail send` 递过去的是 `ReportSeams`，**不是引擎**。

    这条以前是坏的：`run_once` 张口就 `report.snap()`，而引擎上那个方法叫
    `snapshot()`、也没有 `remember_letter`（它叫 `note_letter`）——一按就
    `AttributeError`。修法是从装配点那份接缝上取（`registry.report`）。
    """

    from qq_roleplay_bot.stage3_main import DialogueEngine

    class _Reporter:
        def __init__(self):
            self.seen = None

        async def run_once(self, report, *, force=False):
            self.seen = report
            return SimpleNamespace(subject="今天", body="说了几句话。")

    class _Registry:
        report = ReportSeams()

    class _Engine:
        _report_seams_for_mail = DialogueEngine._report_seams_for_mail

    fake = _Engine()
    fake.daily_reporter = _Reporter()
    fake.plugin_registry = _Registry()

    text = asyncio.run(DialogueEngine._mail_send_now(fake))

    assert fake.daily_reporter.seen is _Registry.report, "递的必须是接缝，不是引擎自己"
    assert "已寄出：今天" in text
    assert not hasattr(DialogueEngine, "snap"), (
        "引擎没有 snap：别为了让旧写法能用就给它加一个——那等于让插件又拿到引擎"
    )


# --- 人格由核心现读、今晚写不写她说了算（2026-10-12 用户裁定） -----------------


class _QuietWriter:
    """她说今晚不写：两个空串 + `last_skip` 里放原因（生产的 `LetterWriter` 就是这样）。"""

    enabled = True

    def __init__(self, reason: str = "今晚没什么想说的") -> None:
        self.last_skip = reason

    async def write(self, materials, *, persona="", facts="", closeness_names=(),
                    guardedness_names=(), lookup=None, now=None):
        return "", ""


def test_the_letter_uses_the_persona_the_core_handed_over():
    """写信用的人格与事实表**由核心按次递进来**（不是插件自己找、也不是聊天那份）。"""

    with tempfile.TemporaryDirectory() as tmp:
        store = MailStateStore(Path(tmp) / "mail_state.json")
        mail = _Mail()
        writer = _Writer()
        reporter = _reporter(store, mail)
        reporter.writer = writer

        draft = asyncio.run(reporter.run_once(_seams(_Client(RAW))))

    assert writer.seen["persona"] == PERSONA_TEXT, "人格要原样交到写信口手里"
    assert writer.seen["facts"] == FACTS_TEXT
    assert writer.seen["lookup"] is None, "接缝没给记忆口就别自己造一个"
    assert draft is not None and draft.subject == "今天"
    assert mail.sent and mail.sent[0]["body"] == "群里今天挺热闹，我跟几个人说了话。"


def test_a_missing_persona_means_no_letter_at_all():
    """"核心那边读不到真人格" ⇒ 不写、不发、也不叫模型，只记一句原因。

    用户 2026-10-12 原话：*"凭啥这玩意能直接复用人格prompt啊"*——所以这里**不许回落**
    到模板或聊天人格：宁可今晚没有信。
    """

    with tempfile.TemporaryDirectory() as tmp:
        store = MailStateStore(Path(tmp) / "mail_state.json")
        mail = _Mail()
        client = _Client(RAW)
        reporter = _reporter(store, mail)

        with _no_logs():
            draft = asyncio.run(reporter.run_once(_seams(client, persona=None)))
        state = store.load()

    assert draft is None and mail.sent == [], "读不到人格就不写信"
    assert client.calls == 0, "连模型都不用叫"
    assert state.sends[-1]["ok"] is True, "这不是发送失败"
    assert "信人格读不到" in state.sends[-1]["detail"]


def test_the_old_single_agent_path_also_needs_a_real_persona():
    """单 agent 老路（没有 writer 时）同样只在有真人格时才发信。"""

    with tempfile.TemporaryDirectory() as tmp:
        store = MailStateStore(Path(tmp) / "mail_state.json")
        mail = _Mail()
        client = _Client(RAW)
        reporter = _reporter(store, mail)  # writer=None：走旧路

        with _no_logs():
            draft = asyncio.run(reporter.run_once(_seams(client, persona=None)))
        state = store.load()

    assert draft is None and mail.sent == [] and client.calls == 0
    assert state.sends[-1]["ok"] is True, "这不是发送失败"
    assert "信人格读不到" in state.sends[-1]["detail"]


def test_a_skipped_night_is_recorded_but_not_a_send_failure():
    """她自己说不写：不发信，但要把"今天问过了"记下来。

    记成 `ok=False` 会有三个后果（`MailStateStore.record` 只认 `ok and kind=="report"`）：
    `due()` 一直为真、ticker 整晚每隔一分钟再问一次；`attempts`（重试额度）被
    "她不想写"用光；下一晚的信里冒出"上一次想给他写信没寄出去"——那晚根本没写。
    """

    with tempfile.TemporaryDirectory() as tmp:
        # 存状态那份自己也有一个时钟（`record()` 用它记 `last_report_at`），
        # 不跟着 `_reporter` 的假时钟走的话，"今天问过了"这句就没法对表。
        store = MailStateStore(Path(tmp) / "mail_state.json", clock=lambda: NOW)
        mail = _Mail()
        reporter = _reporter(store, mail)
        reporter.writer = _QuietWriter("今晚没什么想说的")

        first = asyncio.run(reporter.run_once(_seams(_Client(RAW))))
        state = store.load()
        due_after = reporter.due()
        again = asyncio.run(reporter.run_once(_seams(_Client(RAW))))

    assert first is None and again is None, "没有信，就没有草稿交出来"
    assert mail.sent == [], "她说今晚不写，就一封信都不发"
    assert state.last_report_at == NOW, "今天这一次问过了"
    assert due_after is False, "记下来之后就不该再算到点（否则整晚重复问）"
    assert state.attempts == 0, "不该被「她不想写」用掉重试额度"
    assert state.sends[-1]["ok"] is True, "不写不是发送失败"
    assert "今晚没写" in state.sends[-1]["detail"]
    assert "今晚没什么想说的" in state.sends[-1]["detail"]
    assert dr.last_failure_note(state.sends) == "", (
        "下一晚的信里不许出现「上次没寄出去」——那晚根本没写"
    )


# --- 素材里"跟谁说过话 / 改了看法"来自只读记忆口（2026-10-13 补） ----------------


class _Reader:
    """只读记忆口的假件——形状照生产那份 `memory_readonly.MemoryRead`。

    真件的方法与字段是拿**生产库**核过的（`run/data/memory/memory.sqlite3`）：
    `people(since)` → `user_id / closeness / guardedness / last_seen_at`，
    `changes(since)` → `… / reason / occurred_at`。
    """

    def __init__(self, *, boom: bool = False) -> None:
        self.boom = boom
        self.asked: list[float] = []

    def people(self, since=0.0, *, limit=20):
        self.asked.append(float(since))
        if self.boom:
            raise RuntimeError("库打不开")
        return ({"user_id": "242003347", "closeness": 2, "guardedness": 0},)

    def changes(self, since, *, limit=20):
        if self.boom:
            raise RuntimeError("库打不开")
        return ({"reason": "他当面贬损羞辱"},)


def test_the_materials_take_people_and_changes_from_the_readonly_reader():
    """打过交道的人要用记忆口那份（带亲近/防备档），不是聊天日志里那串光号码。"""

    from qq_roleplay_bot.stage3_runtime import CLOSENESS_LABELS, GUARDEDNESS_LABELS

    with tempfile.TemporaryDirectory() as tmp:
        store = MailStateStore(Path(tmp) / "mail_state.json")
        reader = _Reader()
        writer = _Writer()
        reporter = _reporter(store, _Mail())
        reporter.writer = writer
        draft = asyncio.run(reporter.run_once(_seams(_Client(), reader=reader)))

    assert draft is not None, "记忆口只是素材的一半，缺了也不该把信拦下"
    data = writer.materials[0].as_data(CLOSENESS_LABELS, GUARDEDNESS_LABELS)
    assert "242003347（亲近 " in data and "防备 " in data, "档位要跟着人一起给她"
    assert "有几个人让你改变了态度，原因是：他当面贬损羞辱。" in data
    assert reader.asked == [NOW - 86400.0], "只读口要拿到窗口起点，才能只翻这段日子"


def test_a_broken_reader_does_not_stop_the_letter():
    """「库打不开」只是素材缺一块：信照写，只是不提跟谁说过话。"""

    with tempfile.TemporaryDirectory() as tmp:
        store = MailStateStore(Path(tmp) / "mail_state.json")
        writer = _Writer()
        mail = _Mail()
        reporter = _reporter(store, mail)
        reporter.writer = writer
        with _no_logs():
            draft = asyncio.run(
                reporter.run_once(_seams(_Client(), reader=_Reader(boom=True)))
            )

    assert draft is not None and mail.sent, "记忆口炸了不该把整封信拦下"
    data = writer.materials[0].as_data((), ())
    assert "跟你说过话的人" not in data
    assert "改变了态度" not in data
