# -*- coding: utf-8 -*-
"""主题寄不出去那件事：发送前清洗 + 兜底标题（2026-10-16 用户批的"两层都做"）。

真事（`mail_state.json` 的 `sends` 里三条失败记录）：23:01 那封的主题是
『上次没寄出去的信，和那句"你别掺和"』，里面有直引号；而邮件 CLI 的主题走 **argv**，
`mail_client.clean_subject` 按最严规则卡引号这类字符 ⇒ `bad_subject`，当天三次全失败。

根因是**只有旧路洗过主题**（`daily_report.parse_report_output` 里那个 `re.sub`），
线上走的两级那条路一个字都没洗。修法两层：

1. `mail_client.sanitize_subject` + `daily_report._safe_subject`：发送前把那些字符
   **换掉**（引号配成全角引号、其余换全角），清完为空就给兜底标题——
   位置在 `clean_subject` 那道判**之前**（`_stash_pending` 与 `_send_draft`）；
2. 草稿的规矩里加一句"主题别用引号那些符号"。

这一份钉三件事（就是用户列的那三条），外加清洗函数自己的口径。
"""
from __future__ import annotations

import asyncio

from qq_roleplay_bot.plugins.mail import daily_report as dr
from qq_roleplay_bot.plugins.mail.mail_client import (
    _ARGV_FORBIDDEN,
    clean_subject,
    sanitize_subject,
)

NOW = 1_800_000_000.0
#: 回归样本：真寄不出去的那个主题。
REAL_SUBJECT = '上次没寄出去的信，和那句"你别掺和"'
BODY = "信里说的话。"


class _Store:
    """假的状态存储：记下 `record` 与 `record_letter` 都收到了什么。"""

    def __init__(self) -> None:
        self.records: list[dict] = []
        self.letters: list[dict] = []
        self.pending: list[dict] = []

    def record(self, **kwargs):
        self.records.append(kwargs)
        return None

    def record_letter(self, **kwargs):
        self.letters.append(kwargs)

    def save_pending_report(self, **kwargs):
        self.pending.append(kwargs)


class _Mail:
    """假的发送实现：**照线上那一套规则拒发**。

    它调的是真的 `clean_subject`——所以"清洗放在判之前"这件事是**被这道闸验的**，
    不是被断言摆出来的：没洗过的主题在这里就抛 `MailError(kind="bad_subject")`。
    """

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, *, to: str, subject: str, body: str) -> dict:
        clean_subject(subject)          # 与线上同一道判；它拒，就等于那天 23:01 的形状
        self.sent.append({"to": to, "subject": subject, "body": body})
        return {"message_id": "mid-1"}


class _Report:
    def __init__(self) -> None:
        self.letters: list[dict] = []

    def remember_letter(self, letter) -> None:
        self.letters.append(letter)


def _reporter(mail=None, store=None):
    return dr.DailyReporter(mail_client=mail or _Mail(), state_store=store or _Store(),
                            recipient="a@b.c", writer=None)


def _send(subject: str, *, mail=None, store=None):
    reporter = _reporter(mail, store)
    report = _Report()
    draft = dr.ReportDraft(subject=subject, body=BODY)
    got = asyncio.run(reporter._send_draft(draft, now=NOW, report=report))
    return reporter, draft, got


# --- 1. 带引号的主题：能寄出（ok:true） --------------------------------------


def test_a_subject_with_quotes_goes_out() -> None:
    """回归样本：清洗之后它**真的寄出去了**，而且引号配成了全角引号。"""

    mail, store = _Mail(), _Store()
    reporter, _draft, got = _send(REAL_SUBJECT, mail=mail, store=store)

    assert got is not None, "这一封必须发出去"
    assert mail.sent and mail.sent[0]["subject"] == '上次没寄出去的信，和那句“你别掺和”'
    assert not (_ARGV_FORBIDDEN & set(mail.sent[0]["subject"])), "一个禁字符都不许剩"
    assert reporter.sent == 1


def test_the_sent_subject_is_what_gets_recorded_and_remembered() -> None:
    """**发出去的那份 = 记下来的那份**：留档与"告诉她"都用清洗后的主题。"""

    mail, store = _Mail(), _Store()
    _reporter_obj, _draft, got = _send(REAL_SUBJECT, mail=mail, store=store)

    assert got is not None and got.subject == mail.sent[0]["subject"]
    assert store.letters and store.letters[0]["subject"] == mail.sent[0]["subject"]
    assert store.records and store.records[-1].get("ok") is True, "记的是成功，不是 bad_subject"


def test_the_pending_copy_is_washed_too() -> None:
    """发之前存的那一份（重发用）也必须是洗过的——重发是**原样再发**。"""

    store = _Store()
    reporter = _reporter(store=store)
    reporter._stash_pending(dr.ReportDraft(subject=REAL_SUBJECT, body=BODY), now=NOW)

    assert store.pending and not (_ARGV_FORBIDDEN & set(store.pending[0]["subject"]))
    assert "你别掺和" in store.pending[0]["subject"], "原意要留住"


# --- 2. 清洗后为空 ⇒ 兜底标题，且不报 bad_subject ----------------------------


def test_an_empty_subject_falls_back_and_never_reports_bad_subject() -> None:
    """只剩控制字符/空白 ⇒ 兜底标题；**不能**走到 `bad_subject` 上去。"""

    for raw in ("", "   ", "\x01\r\n"):
        mail, store = _Mail(), _Store()
        _reporter_obj, _draft, got = _send(raw, mail=mail, store=store)

        assert got is not None, f"{raw!r} 该用兜底标题寄出去"
        assert mail.sent[0]["subject"] == dr.DEFAULT_SUBJECT
        assert store.records[-1].get("ok") is True, "不是 bad_subject"


def test_the_fallback_is_the_same_constant_the_old_path_used() -> None:
    """旧路原来自己写死一个"给你的信"；现在两条路共用 `DEFAULT_SUBJECT`。

    旧路（`parse_report_output`）只在一个控制字符当主题时才会走到兜底：
    换成"删字符"的老写法，整串都是引号的那种会清空；现在引号改成全角了，
    所以能清空的只剩空白/控制字符（见 `sanitize_subject` 的口径）。
    """

    assert dr.DEFAULT_SUBJECT == "给你的信"
    draft = dr.parse_report_output("<subject>\x01</subject>\n<body>正文</body>")
    assert draft.subject == dr.DEFAULT_SUBJECT


# --- 3. 不含禁字符的主题：一字不改 -------------------------------------------


def test_a_clean_subject_is_left_byte_for_byte() -> None:
    """逐字相同——清洗**不许**碰没问题的主题（连空白都不动）。"""

    for raw in ("昨天那件事", "上次没寄出去的信，和那句你别掺和", "晚上十一点零五"):
        cleaned, changed = sanitize_subject(raw)
        assert cleaned == raw and changed == (), f"{raw!r} 不该被改"

        mail, store = _Mail(), _Store()
        _reporter_obj, _draft, got = _send(raw, mail=mail, store=store)
        assert got is not None and mail.sent[0]["subject"] == raw


# --- 4. 清洗函数自己的口径 ---------------------------------------------------


def test_the_forbidden_characters_are_mapped_not_dropped() -> None:
    """那几个字符换全角：**字符还在、意思还在**，但不再是 cmd 的元字符。"""

    cleaned, changed = sanitize_subject("进度 100% & 等待 | 完成！")

    assert cleaned == "进度 100％ ＆ 等待 ｜ 完成！"
    assert set(changed) == {"%", "&", "|"}, changed
    assert not (_ARGV_FORBIDDEN & set(cleaned))


def test_quotes_pair_up_in_order() -> None:
    """引号按出现顺序配成对：第 1 个左、第 2 个右；单引号同理。"""

    assert sanitize_subject('他说"你好"')[0] == "他说“你好”"
    assert sanitize_subject("他说'你好'")[0] == "他说‘你好’"
    assert sanitize_subject('"甲"和"乙"')[0] == "“甲”和“乙”"


def test_sanitizing_is_idempotent() -> None:
    """洗过的再洗一次：一个字都不换（所以两处调用点最多打一行日志）。"""

    once, _changed = sanitize_subject(REAL_SUBJECT)
    twice, changed2 = sanitize_subject(once)

    assert twice == once and changed2 == ()


def test_a_very_long_subject_is_still_left_to_clean_subject() -> None:
    """长度那道判**不归清洗管**：清洗只管字符，太长仍旧由 `clean_subject` 拒。

    （上游 `parse_letter` / `parse_subject` 都截到 100 字，比 `MAX_SUBJECT_CHARS=200`
    还短，所以生产上碰不到；这里只是把"清洗不越界"写清楚。）
    """

    cleaned, changed = sanitize_subject("主" * 300)

    assert cleaned == "主" * 300 and changed == ()


# --- 5. 草稿的规矩里也要说一句（第二层）--------------------------------------


def test_the_draft_rules_tell_her_not_to_use_quotes_in_the_subject() -> None:
    assert "主题那一行别用引号" in dr.REPORT_PROMPT
    assert "落款就写你自己的名字。" in dr.REPORT_PROMPT, "原来那条别被挤掉"
