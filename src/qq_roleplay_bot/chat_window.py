"""窗口里"群里发生过什么"的只读聚合——**只数，不抄一个字**。

由来（2026-10-11）：每日汇报里那句"你收到过 N 条消息"取自引擎的**进程内计数**
（`stage3_main.py` 的 `self._stats`），进程一重启就归零。10-07 到 10-11 之间重启过
二十多次，于是她每封信都看到"两个群里没人开口"，而同一时期群里的真实量是
**每天上千条**——她于是自己编（用户 2026-10-11：*"还有就是mail agent的幻觉很严重啊"*）。

用户对"能不能读"给了准话（2026-10-11）：*"读取为啥不给读取，只读又不影响"*——
允许给日报一份**只读**的窗口事实：从对话日志里**数**，不从进程计数里减。

边界（照 `AGENTS.md` §2.3 那几条用户授权例外的写法，做成**窄接缝**）：
`ChatWindowFacts` 里**只有计数与编号**——条数、会话条数、人的条数、被叫到的次数、
带图/表情的条数。**没有正文、没有原话、没有昵称**，也没有记忆库与知识库的入口。
字段清单由 `tests/test_mail_report_window.py` 逐个钉住；要用这里的任何一样，
先照那张清单想清楚它是不是"她该知道的事"。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from .attention import mentions_name
from .quote_samples import read_rows

logger = logging.getLogger(__name__)

#: 一份日志最多扫多少行。日志本身是滚动文件（最多 `容量 × 2` 条，见 `chat_log.py`），
#: 这个上限只用来兜"别让一份异常大的文件把内存吃掉"，不是业务阈值。
MAX_WINDOW_ROWS = 120000
#: 群与人各最多留几条——接缝给出去的是一句话，不是一张报表。
MAX_GROUPS = 8
MAX_PEOPLE = 8

#: 会话前缀（`chat_log.session_id_of` 的写法）：只有 `group:` 才算"群"。
_GROUP_PREFIX = "group:"


@dataclass(frozen=True, slots=True)
class ChatWindowFacts:
    """窗口里可数的事实。**故意的窄**：这里没有一句人说的话。"""

    #: 窗口起点（与调用方给的一致，便于核对这段数的是哪一段）。
    since: float = 0.0
    #: 窗口里收到的消息条数（含带图/表情的）。
    incoming: int = 0
    #: 她开口的次数（同一条消息切成几段算一次）。
    hers: int = 0
    #: 有人叫她名字、或者接着她的话说的次数。
    addressed: int = 0
    #: 带图片/表情的消息条数。
    media: int = 0
    #: (会话, 条数)，条数从多到少；只有 `group:` 开头的会话在里头。
    groups: tuple[tuple[str, int], ...] = ()
    #: (QQ号, 条数)，条数从多到少。**只给号，不给昵称**——昵称是别人递来的文本。
    people: tuple[tuple[str, int], ...] = ()
    #: 日志最早一条是否早于窗口开头。**假** = 这段窗口读不全（轮转掉的部分数不出来），
    #: 那就不能说"只有这么多"，只能说不清。
    covers_window: bool = False


def _row_at(row: dict) -> float | None:
    """一条日志的时间戳；取不到就当这条不算（坏行在 `read_rows` 那一层已经被跳过）。"""

    try:
        return float(row.get("at"))
    except (TypeError, ValueError):
        return None


def _is_first_part(row: dict) -> bool:
    """多段消息里只数第一段，这样"回了 N 次"是次数，不是段数。"""

    part = row.get("part")
    if part is None:
        return True
    try:
        return int(part) <= 1
    except (TypeError, ValueError):
        return True


def _rank(counts: dict[str, int], limit: int) -> tuple[tuple[str, int], ...]:
    """按条数从多到少排；条数一样就按号排，保证同一份日志每次数的结果一样。"""

    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return tuple(ordered[:limit])


def collect_window_facts(
    path,
    *,
    since: float,
    until: float | None = None,
    max_rows: int = MAX_WINDOW_ROWS,
) -> ChatWindowFacts | None:
    """把窗口里的事**数**出来。读不到日志就返回 `None`（调用方按"说不清"处理）。

    **正文只在这个函数里被看见，一个字符都不进返回值**：`body` 只用来判
    "有没有叫她的名字"，判完即弃。要让返回值多一样东西，先看模块开头那段边界。
    """

    rows, error = read_rows(path)
    if not rows:
        logger.info("chat_window_no_rows path=%s error=%s", path, error)
        return None
    if len(rows) > max_rows:
        rows = rows[-max_rows:]

    start = float(since or 0.0)
    end = float(until) if until is not None else float("inf")

    earliest: float | None = None
    incoming = hers = addressed = media = 0
    groups: dict[str, int] = {}
    people: dict[str, int] = {}
    # 她说过话的那些消息号：别人"接着她的话说"就是拿它当 reply_to。
    her_message_ids: set[str] = set()

    for row in rows:
        at = _row_at(row)
        if at is None:
            continue
        earliest = at if earliest is None else min(earliest, at)
        direction = str(row.get("direction") or "")
        if direction == "out":
            message_id = row.get("message_id")
            if message_id:
                her_message_ids.add(str(message_id))
            if not start <= at <= end:
                continue
            # 表情回应不是"开口说话"，与 `stage3_main` 的计数口径一致。
            if str(row.get("kind") or "") == "reaction":
                continue
            if _is_first_part(row):
                hers += 1
            continue
        if direction != "in":
            continue
        if not start <= at <= end:
            continue
        incoming += 1
        if row.get("has_media"):
            media += 1
        session = str(row.get("session_id") or "")
        if session.startswith(_GROUP_PREFIX):
            groups[session] = groups.get(session, 0) + 1
        user_id = row.get("user_id")
        if user_id:
            people[str(user_id)] = people.get(str(user_id), 0) + 1
        reply_to = row.get("reply_to")
        body = row.get("body")
        if (reply_to and str(reply_to) in her_message_ids) or (
            isinstance(body, str) and mentions_name(body)
        ):
            addressed += 1

    return ChatWindowFacts(
        since=start,
        incoming=incoming,
        hers=hers,
        addressed=addressed,
        media=media,
        groups=_rank(groups, MAX_GROUPS),
        people=_rank(people, MAX_PEOPLE),
        covers_window=earliest is not None and earliest <= start,
    )
