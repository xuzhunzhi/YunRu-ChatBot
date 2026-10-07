"""金句上下文**不许跨会话**——用户 2026-10-06 报的那个缺陷（第四批）。

用户原话（判据）：*"你没有区分群聊。。。你把**不同群聊的上下文**都塞到金句的上下文里了"*。

## 那是个什么病

金句证据里那两串上下文（`她开口之前群里在说` / `她说完之后群里接的`）原来是**按日志位置**
抓的：往上/往下数若干条日志行，只要是入站消息就收。日志是**所有会话共用一份**
（`data/logs/chat.jsonl`），于是别群的消息就混进了她的语境。实测
`run/data/logs/chat.jsonl`（5201 行、7 个会话、A 群与 B 群消息交错）：
不过滤时会串进来 **38 行**别群的消息（其中 32 行明确属于主群 `1084401296`）。

## 这一份钉住什么

1. **交错的多群日志**（A/B/A/B）→ A 群那条证据的上下文里**不含 B 的任何一行**；
   学习输入（`build_samples`）与审核证据（`collect_evidence`）**两条路都要**——
   原来那两处各写了一份取上下文的代码，是同一个病；
2. **条数不够就如实少给**：窗口里同一会话只有 1 条，就给 1 条，
   **绝不**拿别群的话补位（`DEFAULT_CONTEXT_BEFORE` 要 6 条也不补）；
3. **私聊（非群会话）同样不串**：`private:` 与 `group:` 是两个会话，
   双向都不许混（私聊那行的 `group_id` 是空的，只按群号判就会把它并进"空群号"那一堆）；
4. **突变验证**：把会话过滤去掉（改回"按位置抓前后 N 条"）→ 上面那些断言**必须变红**；
5. 取上下文只有**一处实现**（`quote_samples.context_around`），两个读口都调它。

**这一份不用 pytest**（本仓库的套件是纯 stdlib 的 `unittest` + 裸函数，
`tests/run_offline.py` 直接收集它们）：临时目录走 `tempfile`，打桩走
`unittest.mock.patch`。
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from qq_roleplay_bot import quote_samples  # noqa: E402
from qq_roleplay_bot.quote_samples import (  # noqa: E402
    DEFAULT_CONTEXT_BEFORE,
    build_samples,
    collect_evidence,
)

GROUP_A = "1001"
GROUP_B = "2002"
PRIVATE = "private:3003"
A_SESSION = f"group:{GROUP_A}"
B_SESSION = f"group:{GROUP_B}"
HER_ID = "-900001"
OTHER_HER_ID = "-900002"
EMOJI = "76"


def _incoming(session: str, group: str, message_id: str, body: str,
              at: float = 1.0, who: str = "甲") -> dict:
    row = {"at": at, "direction": "in", "session_id": session, "user_id": "9",
           "sender_name": who, "message_id": message_id, "body": body,
           "has_media": False}
    if group:
        row["group_id"] = group
    return row


def _her(session: str, group: str, message_id: str, body: str, at: float = 5.0) -> dict:
    row = {"at": at, "direction": "out", "session_id": session, "user_id": "1",
           "sender_name": "云茹", "kind": "text", "message_id": message_id,
           "body": body, "part": 1, "total": 1}
    if group:
        row["group_id"] = group
    return row


def _reaction(session: str, group: str, target: str, at: float = 6.0) -> dict:
    row = {"at": at, "direction": "in", "kind": "reaction", "session_id": session,
           "user_id": "9", "operator_id": "9", "message_id": target,
           "emoji_id": EMOJI, "count": 1, "sub_type": "add"}
    if group:
        row["group_id"] = group
    return row


def _interleaved(*, before_b: int = 6, after_b: int = 2) -> list[dict]:
    """**交错的多群日志**（A/B/A/B…）：B 群的消息紧紧围着她 A 群那条。

    "按日志位置抓"的话，B 那些行会被当成本条的上下文；按会话过滤的话一行都不该有。
    """

    rows: list[dict] = []
    for index in range(before_b):
        rows.append(_incoming(B_SESSION, GROUP_B, f"b{index}", f"B群的话{index}",
                              at=1.0 + index, who="B君"))
    rows.append(_her(A_SESSION, GROUP_A, HER_ID, "她说的那句话。", at=5.0))
    for index in range(after_b):
        rows.append(_incoming(B_SESSION, GROUP_B, f"ba{index}", f"B群接的{index}",
                              at=6.0 + index, who="B君"))
    rows.append(_reaction(A_SESSION, GROUP_A, HER_ID))
    return rows


def _write_log(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
                    encoding="utf-8")


def _bodies(items) -> list[str]:
    return [item["body"] for item in items]


# --- 1. 交错的多群日志：A 的证据里不许有 B 的任何一行 ---------------------------


def test_build_samples_context_never_crosses_sessions() -> None:
    """**学习输入**：A/B/A/B 交错 → A 那条样本的上下文里一行 B 都没有。"""

    selection = build_samples(_interleaved())
    assert len(selection.samples) == 1
    sample = selection.samples[0]
    assert sample.group_id == GROUP_A
    # B 群的话**一条都不许出现**（不管在她之前还是之后）。
    assert "B群的话" not in " ".join(_bodies(sample.context_before))
    assert "B群接的" not in " ".join(_bodies(sample.context_after))
    # 而且**确实没有**同会话的邻居可给 → 两串都空（宁缺勿错）。
    assert sample.context_before == ()
    assert sample.context_after == ()


def test_the_evidence_reader_never_crosses_sessions() -> None:
    """**证据读口**（同一份交错日志）：`collect_evidence` 的两串里一行 B 都没有。"""

    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "chat.jsonl"
        _write_log(log, _interleaved())
        entries, total, error = collect_evidence(log, group_id=GROUP_A, limit=10)
    assert error == ""
    assert total == 1
    entry = entries[0]
    assert entry.group_id == GROUP_A
    assert "B群的话" not in " ".join(_bodies(entry.context_before))
    assert "B群接的" not in " ".join(_bodies(entry.context_after))


def test_the_two_readers_share_one_context_implementation() -> None:
    """两个读口**同一个函数**：`build_samples` 与 `collect_evidence` 都走 `context_around`。

    收敛成一处是这次修复的关键（原来两处各写一份"按位置抓前后 N 条"，
    所以同一个病有两个入口）。这一条钉住"以后不许再长出第二份实现"。
    """

    import inspect

    source = inspect.getsource(quote_samples)
    assert source.count("def context_around(") == 1
    # 学习那一份（`_context`）与证据那一份（`context_for`）都只经由它取上下文。
    assert "return context_around(rows, rows[index]" in source
    assert "return context_around(rows, row, before=before" in source
    # 老的"按位置抓"那种循环不许再出现在别处（`_context` 里一句自己的扫描都不该有）。
    assert "for row in reversed(rows[:" not in source
    assert "for row in rows[index + 1:]" not in source


# --- 2. 同会话的邻居照旧要给；条数不够就如实少给（不许拿别群补）--------------


def test_same_session_neighbours_are_still_collected() -> None:
    """反过来也要对：**同会话**的邻居一条不少地给（别把过滤做得连自己人都不要了）。"""

    rows = [
        _incoming(A_SESSION, GROUP_A, "a1", "A群第一句", at=1.0, who="A君"),
        _incoming(B_SESSION, GROUP_B, "b1", "B群插一句", at=2.0, who="B君"),
        _incoming(A_SESSION, GROUP_A, "a2", "A群第二句", at=3.0, who="A君"),
        _her(A_SESSION, GROUP_A, HER_ID, "她接一句。", at=5.0),
        _incoming(A_SESSION, GROUP_A, "a3", "A群之后", at=6.0, who="A君"),
        _reaction(A_SESSION, GROUP_A, HER_ID),
    ]
    sample = build_samples(rows).samples[0]
    assert _bodies(sample.context_before) == ["A群第一句", "A群第二句"]
    assert _bodies(sample.context_after) == ["A群之后"]


def test_too_few_context_lines_are_not_padded_from_other_sessions() -> None:
    """**条数不够就如实少给**：窗口里同会话只有 1 条，就给 1 条。

    `DEFAULT_CONTEXT_BEFORE` 是 6，但同会话的行只有 1 条——**不许**拿 B 群的话补到 6 条。
    """

    assert DEFAULT_CONTEXT_BEFORE == 6
    rows = [
        # 6 条 B 群的话在她前面（"按位置抓"刚好会把这 6 条当成她的上文）。
        _incoming(B_SESSION, GROUP_B, "b0", "B群0", at=1.0, who="B君"),
        _incoming(B_SESSION, GROUP_B, "b1", "B群1", at=2.0, who="B君"),
        _incoming(B_SESSION, GROUP_B, "b2", "B群2", at=3.0, who="B君"),
        _incoming(B_SESSION, GROUP_B, "b3", "B群3", at=4.0, who="B君"),
        _incoming(B_SESSION, GROUP_B, "b4", "B群4", at=4.5, who="B君"),
        _incoming(A_SESSION, GROUP_A, "a1", "A群唯一的一句", at=4.8, who="A君"),
        _incoming(B_SESSION, GROUP_B, "b5", "B群5", at=4.9, who="B君"),
        _her(A_SESSION, GROUP_A, HER_ID, "她接一句。", at=5.0),
        _reaction(A_SESSION, GROUP_A, HER_ID),
    ]
    sample = build_samples(rows).samples[0]
    assert _bodies(sample.context_before) == ["A群唯一的一句"]      # 1 条，不是 6 条
    assert sample.context_after == ()


# --- 3. 私聊 / 非群会话同样不串 ------------------------------------------------


def test_private_chat_never_leaks_into_a_group_context() -> None:
    """私聊那行**不串进群**：`group_id` 是空的那种会话，只按群号判会被并进"空群号"。"""

    rows = [
        _incoming(PRIVATE, "", "p1", "私聊里说的话", at=1.0, who="她朋友"),
        _her(A_SESSION, GROUP_A, HER_ID, "她群里接一句。", at=5.0),
        _reaction(A_SESSION, GROUP_A, HER_ID),
    ]
    sample = build_samples(rows).samples[0]
    assert "私聊里说的话" not in " ".join(_bodies(sample.context_before))
    assert sample.context_before == ()


def test_a_private_quote_does_not_pull_the_group_context() -> None:
    """反过来也一样：**私聊**那条样本的上下文里，不许出现群里的话。"""

    rows = [
        _incoming(A_SESSION, GROUP_A, "a1", "群里说的话", at=1.0, who="A君"),
        _her(PRIVATE, "", OTHER_HER_ID, "她私聊回一句。", at=5.0),
        _reaction(PRIVATE, "", OTHER_HER_ID),
    ]
    selection = build_samples(rows)
    assert len(selection.samples) == 1
    sample = selection.samples[0]
    assert sample.group_id == ""                       # 私聊没有群号
    assert "群里说的话" not in " ".join(_bodies(sample.context_before))
    assert sample.context_before == ()


def test_the_evidence_reader_keeps_sessions_apart_too() -> None:
    """证据读口在"群里的话围着私聊那条"时同样一行都不给。"""

    rows = [
        _incoming(A_SESSION, GROUP_A, "a1", "群里说的话", at=1.0, who="A君"),
        _incoming(B_SESSION, GROUP_B, "b1", "另一个群的话", at=2.0, who="B君"),
        _her(PRIVATE, "", OTHER_HER_ID, "她私聊回一句。", at=5.0),
        _reaction(PRIVATE, "", OTHER_HER_ID),
    ]
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "chat.jsonl"
        _write_log(log, rows)
        # 证据读口按"群"取；私聊没有群号，所以不给（这一层只服务面板的群视图）。
        entries, total, _ = collect_evidence(log, group_id=GROUP_A, limit=10)
    assert total == 0 and entries == ()


# --- 4. 正文口径：别把整行 dict 递进 `_clip` ----------------------------------


def test_context_lines_are_bodies_not_stringified_rows() -> None:
    """上下文那一行必须是**正文**，不能是整条日志行的字符串。

    这是我在收敛两处实现时**自己写出来的**一个 bug：`_clip(text)` 收正文字符串、
    `_line_body(row)` 收整行 dict，两者入参不是一个东西。当时写成
    `_clip if clip else _line_body` 再统一喂 `row.get("body")`，
    于是 `clip=False`（证据读口）把**字符串**递给了 `_line_body` → 直接抛
    `AttributeError`；`clip=True` 那一侧会把 `str(字典)` 写进给模型的上下文。
    两个读口都要喂**整行**、由取正文的那一步决定怎么取——这一条钉住它的形状。
    """

    rows = [
        _incoming(A_SESSION, GROUP_A, "a1", "A群第一句", at=1.0, who="A君"),
        _her(A_SESSION, GROUP_A, HER_ID, "她接一句。", at=5.0),
        _reaction(A_SESSION, GROUP_A, HER_ID),
    ]
    # 学习那一份（clip=True）：正文是她的原文，不是整行的 str()。
    sample = build_samples(rows).samples[0]
    assert _bodies(sample.context_before) == ["A群第一句"]
    # 审核那一份（clip=False）：同一个形状（而且不截断）。
    earlier, later = quote_samples.context_for(rows, HER_ID, before=8, after=8,
                                              session=A_SESSION)
    assert [item["body"] for item in earlier] == ["A群第一句"]
    assert later == ()
    for item in earlier:
        assert not item["body"].startswith("{"), "上下文里出现了 str(字典)"
        assert "direction" not in item["body"]


# --- 5. 突变验证：把过滤去掉 → 这些断言必须变红 --------------------------------


def test_removing_the_session_filter_makes_the_context_cross_sessions() -> None:
    """**突变验证**：把会话过滤去掉（等价于原来"按位置抓前后 N 条"）→ 立刻串群。

    判据不是"某个测试变红"这种口头承诺，而是在同一个进程里**当场**证明：
    过滤去掉之后，A 群那条样本的上下文里真的出现了 B 群的话。
    上面那些断言用的正是同一个函数，所以它们必然会红。
    """

    rows = _interleaved()
    # 先确认正常情况是干净的。
    assert build_samples(rows).samples[0].context_before == ()

    # 突变：会话判定恒等 → `_session_of(item) != session` 永远不成立 → 等价于不过滤。
    with patch.object(quote_samples, "_session_of", lambda row: "ALL"):
        mutated = build_samples(rows).samples[0]
        bodies = " ".join(_bodies(mutated.context_before) + _bodies(mutated.context_after))
    assert "B群的话" in bodies, "突变没有生效：过滤去掉之后本该串进 B 群的话"
    assert "B群接的" in bodies

    # 补丁退出后必须复原（不然别的用例会被污染）。
    assert build_samples(rows).samples[0].context_before == ()


def test_removing_the_session_filter_makes_the_evidence_reader_cross_sessions() -> None:
    """同一个突变在**证据读口**上也要变红（两处原来是两份实现，得各钉一处）。"""

    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "chat.jsonl"
        _write_log(log, _interleaved())
        entries, _, _ = collect_evidence(log, group_id=GROUP_A, limit=10)
        assert entries[0].context_before == ()

        with patch.object(quote_samples, "_session_of", lambda row: "ALL"):
            mutated_entries, _, _ = collect_evidence(log, group_id=GROUP_A, limit=10)
            bodies = " ".join(_bodies(mutated_entries[0].context_before)
                              + _bodies(mutated_entries[0].context_after))
    assert "B群的话" in bodies
