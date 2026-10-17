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
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace

from qq_roleplay_bot.chat_window import ChatWindowFacts, collect_window_facts
from qq_roleplay_bot.plugins import ReportSeams
from qq_roleplay_bot.plugins.mail import daily_report as dr
from qq_roleplay_bot.plugins.mail.mail_state import (
    PENDING_REPORT_MAX_AGE,
    MailState,
    MailStateStore,
)
from qq_roleplay_bot.prompt_guard import scan_persona_text

#: 群里有人说过的一句话。**它一个字都不该出现在聚合结果里。**
SECRET = "我今晚打算去把他家那只猫偷走"

NOW = 1_800_000_000.0


#: 收件人那个键名 / 假地址。`.invalid` 是 RFC 2606 留的"永远不存在"的顶级域。
RECIPIENT_ENV = "QQBOT_MAIL_REPORT_TO"
FAKE_RECIPIENT = "someone@example.invalid"


@contextlib.contextmanager
def _recipient_configured():
    """把"这个部署配了收件人"这个前提显式摆好，跑完原样（含环境变量）还回去。

    与 `tests/test_outage_notice.py::_recipient_configured` 同法——为什么前提必须由
    测试自己配、为什么**只设环境变量**（`build_operator_mail_sender` 从 2026-10-16 起
    改成现读 `dev_config.mail_report_to()`，`MAIL_REPORT_TO` 常量不再参与）、为什么不
    改回源码默认值也不 reload，那份 docstring 写了完整理由（用户原话："改成必须由
    `.env` 给"，于是源码里没有默认收件人；本机 `.env` 也没填）。

    这个文件用它是因为 `/super mail send` 的**入口闸**就是收件人：没配的时候按设计
    **一封都不发**、日报器一次都不被调用——那会让"递的是接缝不是引擎"这条断言以
    "日报器根本没被叫"的形状红（2026-10-18 实测）。
    """

    saved_env = os.environ.get(RECIPIENT_ENV)
    os.environ[RECIPIENT_ENV] = FAKE_RECIPIENT
    try:
        yield FAKE_RECIPIENT
    finally:
        os.environ.pop(RECIPIENT_ENV, None)
        if saved_env is not None:
            os.environ[RECIPIENT_ENV] = saved_env


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


def _seams(client, *, letters=None, facts=None, persona=PERSONA_TEXT, reader=None,
           his=None) -> ReportSeams:
    """生产接缝的那一份（外加只读记忆口）。

    2026-10-12 起两处变了：`memory_service`（可写的工程件）换成只读的 `memory_read`；
    信人格与事实表也走接缝——插件不许再 `from ...base_prompt import BASE_PROMPT`。
    `persona=None` 就是"核心那边读不到"：那种情况下一封信都不该发出去。

    `his` 是 2026-10-13 加的那条（"他说过的话"）：缺省 `None` 就是"他这几天没说什么"
    ——**接缝上没有这一样时素材照样组得出来**，这条要一直成立。
    """

    return ReportSeams(
        snapshot=lambda: _Snapshot(),
        client=client,
        log_model_io=None,
        note_letter=(letters.append if letters is not None else None),
        memory_read=reader,
        facts=facts,
        letter_context=his,
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


def test_report_prompt_asks_for_a_letter_not_a_diary():
    """提示词：得写"一封信长什么样"，不只是"别像机器写的"。

    2026-10-13 用户读到一封这样的信（"这写的啥啊。。根本不像是人写的信"）：
    四段互不相干、每段结尾一句把话咽回去、拿事实表里的旧事当今天发生的事。
    上一版规矩只禁了机器腔（对仗/首先其次/比方/语气标签），一条也没说信的形状。
    这四条是那次投诉的修法——删掉任意一条，那封信的形状就会回来。
    """

    assert scan_persona_text(dr.REPORT_PROMPT) == [], "加了形状要求也不许混进机制词"
    assert "这是一封写给他的信，不是写给自己看的" in dr.REPORT_PROMPT, "信/日记这一条被删了"
    for words in ("开头要有一句是**对他说的**", "中间挑**一件事**说给他听",
                  "信里得有他", "结尾收在对他说话上"):
        assert words in dr.REPORT_PROMPT, f"{words} 这条形状要求被删了"
    assert "别从你那边起头" in dr.REPORT_PROMPT, "日记式开场（夜深了/我坐在工作台前面）得点名禁掉"
    assert "不许把那里面的旧事写成" in dr.REPORT_PROMPT, "旧事冒充今天的新事，是那封信的老毛病"
    assert "发生的新鲜事" in dr.REPORT_PROMPT
    assert "这像不像一封写给他的信" in dr.REPORT_PROMPT, "自检那一问也得问信的形状"


def test_report_prompt_tells_her_to_pick_up_on_what_he_said():
    """他说过的话进了素材，规矩里就得有对应的那一条（2026-10-13 用户："加"）。

    这一条单列，是因为它和"不要把他人的原话抄给他看"**看着像冲突**：那句说的是
    **别人**，他本人的话正好相反——要接上一句，只是不许照抄一遍。两句都得在。
    """

    assert scan_persona_text(dr.REPORT_PROMPT) == []
    assert "他这段时间说过的话" in dr.REPORT_PROMPT, "素材里那一段得在规矩里点名"
    assert "至少接上一句" in dr.REPORT_PROMPT
    assert "别把他的话抄一遍" in dr.REPORT_PROMPT
    assert "不要把他人的原话抄给他看**（说的是**别人**" in dr.REPORT_PROMPT, (
        "两句的关系要写清楚，不然她会分不清该不该提他说过的话"
    )


def test_super_mail_send_now_hands_the_seams_not_the_engine():
    """`/super mail send` 递过去的是 `ReportSeams`，**不是引擎**。

    这条以前是坏的：`run_once` 张口就 `report.snap()`，而引擎上那个方法叫
    `snapshot()`、也没有 `remember_letter`（它叫 `note_letter`）——一按就
    `AttributeError`。修法是从装配点那份接缝上取（`registry.report`）。

    前提（收件人配了）由 `_recipient_configured()` 显式摆好：`/super mail send` 的入口
    闸就是收件人，源码里已经没有默认值了。
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

    with _recipient_configured():
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


def test_materials_survive_a_snapshot_that_is_not_there():
    """快照取不到只是"数字不可信"：素材照样组得出来，一个字都不许炸。

    2026-10-13 修：`_EmptySnapshot` 少一个 `enabled_group_ids`，于是
    `ReportSeams.snapshot` 缺省——`snap()` **正常**的一种返回值就是 `None`——
    这条路上 `collect_materials()` 直接 `AttributeError`。
    "素材缺一块、信照样写"在那条路上是句空话。
    """

    from qq_roleplay_bot.plugins.mail import daily_report as dr

    # snapshot / memory_read / window_facts 全缺省：接缝上一块都没有的最坏形状
    seams = ReportSeams(
        client=_Client(),
        letter_persona=lambda: PERSONA_TEXT,
        letter_facts=lambda: FACTS_TEXT,
    )
    materials = dr.collect_materials(seams, window_hours=24.0, since=NOW - 86400.0)

    assert materials.groups == (), "没有快照就没有群可报，别猜一个"
    assert materials.counts_reliable is False, "数不出来就承认数不出来"
    assert materials.people == () and materials.relationship_changes == ()
    data = materials.as_data((), ())
    assert "（这是最近一天里的事）" in data, "缺的是数字，不是整段素材"
    assert "群里" not in data, "计数不可信时不许给「热闹还是安静」那句语气"


# --- 他说过的话（2026-10-13 用户："加，不是说了允许读取记忆吗"）----------------


class _HisContext:
    """核心那边递过来的"他说过的话"（`letter_context.LetterContext` 的形状）。"""

    def __init__(self, lines=(), *, at: float = 0.0, subject: str = "") -> None:
        self.his_lines = tuple(lines)
        self.last_letter_at = at
        self.last_letter_subject = subject


def test_the_materials_carry_what_he_said():
    """他本人的话要原样进素材，而且只问窗口这一段（别把整本日志端过来）。

    在这之前素材里一句人话都没有：实测同一份素材三封信里，有一封把《已确定的事》
    里的旧事写成了"今天"发生的新事，用户读到的那封"根本不像是人写的信"就是这么来的。
    """

    with tempfile.TemporaryDirectory() as tmp:
        store = MailStateStore(Path(tmp) / "mail_state.json")
        writer = _Writer()
        reporter = _reporter(store, _Mail())
        reporter.writer = writer
        asked: list[float] = []

        def his(since):
            asked.append(since)
            return _HisContext(["你上次说的那个问题，我想了想", "别太晚睡"],
                               at=NOW - 2 * 86400.0, subject="没别的，说件事")

        draft = asyncio.run(reporter.run_once(_seams(_Client(), his=his)))

    assert draft is not None
    data = writer.materials[0].as_data((), ())
    assert "他这段时间说过的话（从早到晚）：" in data, "这一段就是这次加的素材"
    assert "· 你上次说的那个问题，我想了想" in data and "· 别太晚睡" in data, "原样给，不改字"
    assert "你上一次写信是2 天前，主题是「没别的，说件事」。" in data, (
        "上一封信要给一句人话（几天前 + 主题），不是给一个时间戳"
    )
    assert asked == [NOW - 86400.0], "要拿窗口起点去问"


def test_a_broken_his_context_does_not_stop_the_letter():
    """读不出来只是素材缺一块：信照写，只是不提他说过什么。"""

    with tempfile.TemporaryDirectory() as tmp:
        store = MailStateStore(Path(tmp) / "mail_state.json")
        writer = _Writer()
        mail = _Mail()
        reporter = _reporter(store, mail)
        reporter.writer = writer

        def boom(since):
            raise RuntimeError("chat log locked")

        with _no_logs():
            draft = asyncio.run(reporter.run_once(_seams(_Client(), his=boom)))

    assert draft is not None and mail.sent, "这一条炸了不该把整封信拦下"
    data = writer.materials[0].as_data((), ())
    assert "他这段时间说过的话" not in data
    assert "你上一次写信是" not in data


def test_an_empty_his_context_prints_nothing():
    """接缝在、但这几天他什么都没说：素材里连标题都不该冒出来。"""

    with tempfile.TemporaryDirectory() as tmp:
        store = MailStateStore(Path(tmp) / "mail_state.json")
        writer = _Writer()
        reporter = _reporter(store, _Mail())
        reporter.writer = writer

        draft = asyncio.run(
            reporter.run_once(_seams(_Client(), his=lambda since: _HisContext()))
        )

    assert draft is not None
    data = writer.materials[0].as_data((), ())
    assert "他这段时间说过的话" not in data and "你上一次写信是" not in data


def test_the_stored_letters_are_loaded_oldest_first():
    """重启之后 `letter_history[0]` 必须是**最近那一封**。

    2026-10-13 修的：`mail_state.json` 里最后一封信在**数组末尾**（存储层是
    `append` + 只留最后几条），而 `note_letter` 是"最近在前"（新的一封插到
    `[0]`）。原先这里 `reversed(state.letters)` 会把最旧的一封灌到最前面，
    于是重启后"上一封信"指到十几天前那一封（`_letter_note` 也照着看错）。
    这条钉的是**灌进去的顺序**，与 `at` 在数组里的位置无关。
    """

    from unittest import mock

    from qq_roleplay_bot.plugins.mail import mail_client as mail_client_module
    from qq_roleplay_bot.plugins.mail import mail_state as mail_state_module
    from qq_roleplay_bot.plugins.mail import wire
    from qq_roleplay_bot.plugins.mail.mail_state import MailState

    seen: list[float] = []

    class _Store:
        def load(self):
            # 故意乱序：谁在备份里改过顺序都不该影响结果
            return MailState(letters=[
                {"at": 3.0, "subject": "最近那封"},
                {"at": 1.0, "subject": "最早那封"},
                {"at": 2.0, "subject": "中间那封"},
            ])

    class _Report:
        letter_client = staticmethod(lambda: object())

        def remember_letter(self, letter):
            seen.append(float(letter["at"]))

    # `wire` 是在函数体里 import 这两个的，所以要打在**来源模块**上
    with (
        mock.patch.object(mail_client_module, "MailClient", lambda *a, **k: object()),
        mock.patch.object(mail_state_module, "MailStateStore", _Store),
    ):
        built = wire.build_daily_report(_Report())

    assert built is not None
    assert seen == [1.0, 2.0, 3.0], "从早到晚灌，最新那封才落在最前面"


def test_a_missed_night_is_sent_when_she_comes_back_after_midnight() -> None:
    """23:00 关机、次日凌晨才开机 ⇒ **要补发**（用户 2026-10-14："昨晚云茹邮件我又没收到"）。

    `report_due` 的 docstring 里一直写着这个场景（"23:00 关机、次日 01:00 才开机补发"），
    但旧判据只看**今天**的目标时刻：跨过午夜之后，昨晚那一刻就永远不补了。
    实测代价——`data/mail_state.json` 的 `last_report_at` 停在 10-12 23:02，
    10-13 那封至今没发（`logs/mail.jsonl` 最后一条也是 10-12 23:00）。
    """
    import time as _time

    def at(year: int, month: int, day: int, hour: int, minute: int) -> float:
        return _time.mktime((year, month, day, hour, minute, 0, 0, 0, -1))

    back_at = at(2026, 10, 14, 1, 16)
    # 昨晚（10-13 23:00）那封漏了：上次成功是 10-12 23:02
    missed = MailState(last_report_at=at(2026, 10, 12, 23, 2))
    assert MailState.report_due(missed, back_at) is True, "昨晚漏了要补发"

    # 昨晚正常发过：凌晨那次开机不许再写一封
    sent_last_night = MailState(last_report_at=at(2026, 10, 13, 23, 2))
    assert MailState.report_due(sent_last_night, back_at) is False

    # 补发之后当天不重复；当天 23:00 仍照常（"每晚十一点"这条不被补发带偏）
    caught_up = MailState(last_report_at=back_at)
    assert MailState.report_due(caught_up, back_at) is False
    assert MailState.report_due(caught_up, at(2026, 10, 14, 23, 1)) is True
    # 当天 23:02 已经发过：23:30 再看也不该重复发
    assert MailState.report_due(sent_last_night, at(2026, 10, 13, 23, 30)) is False
