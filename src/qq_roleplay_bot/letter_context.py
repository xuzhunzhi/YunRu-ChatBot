"""信里"他"那一份：**收信人自己说过的话**，加上她上一封信的主题。

由来（2026-10-13 用户原话）：*"加，不是说了允许读取记忆吗，你是不是不懂什么叫不是非要
一次输入就得到一封信"*。在这之前素材里**一句人话都没有**（`chat_window` 只数千条、
只给号码），她手上唯一有内容的东西是那份固定事实表——实测同一份素材三封信里，有一封
把事实表里的旧事写成了"今天"发生的新事，另一封干脆弃写。用户读到的那封
"根本不像是人写的信"，就是这个形状。

边界（这是对"一句话都不给"那条的一次破例，**只对收信人一个人破**）：

- 只有**收信人自己**说过的话进得来（`owner_ids` 由核心给：`_letter_owner_ids()`，
  部署级管理员 ∪ 超管）。别人的话**一句都不给**——信只寄给他一个人，他自己的原话
  回到他自己手上不构成外泄；别人的话一旦进了素材，就可能被写进信里再寄给第三人看。
- 只读数、不读别的：走 `quote_samples.read_rows`（与 `chat_window` 同一套读法），
  只取窗口内、有正文、非表情回应、也不是斜杠命令的那几条，按时间从早到晚，
  最多 `MAX_LINES` 条，每条截到 `MAX_LINE_CHARS` 字。
- 只给"他说过什么"和"上一封信的主题"，**不给任何计数**：素材里报数目会直接引她谈
  工作量（2026-10-11 用户："不要搞的跟实习生写日报一样"）。
- 读不出日志就返回 `None`（调用方按"他这几天没说什么"处理），**不编**。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from .quote_samples import read_rows
from .security import sanitize_chat_text

logger = logging.getLogger(__name__)

#: 一份日志最多扫多少行（与 `chat_window.MAX_WINDOW_ROWS` 同一套兜底口径）：
#: 只用来防"一份异常大的文件把内存吃掉"，不是业务阈值。
MAX_CONTEXT_ROWS = 120000
#: 最多留几句他的话——素材给出去的是一封信的底，不是一份聊天记录。
MAX_LINES = 8
#: 每句最多多少字：他的话在这里只当"提一句"的引子，不是全文转录。
MAX_LINE_CHARS = 120
#: 主题最多多少字（`mail_state` 里的主题本来就不长，这里只兜异常）。
MAX_SUBJECT_CHARS = 100

#: 斜杠命令不进素材：他打的 `/super mail send` 是给我们看的，不是写给她的。
_COMMAND_PREFIXES = ("/", "／", "!")


@dataclass(frozen=True, slots=True)
class LetterContext:
    """窗口里**他**说过的话，和她的上一封信。**句句都是他的，别人的一句没有。**"""

    #: 窗口起点（与调用方给的一致，便于核对这段读的是哪一段）。
    since: float = 0.0
    #: 他这段时间说过的话（按时间从早到晚）。空 = 他这几天没说什么。
    his_lines: tuple[str, ...] = ()
    #: 上一封信的时间（墙上时间；0 = 没记过）。
    last_letter_at: float = 0.0
    #: 上一封信的主题（空 = 没记过 / 没主题）。
    last_letter_subject: str = ""


def _clean(text: object) -> str:
    """收干净：换行折成空格、去掉首尾空白。**不改一个字**（那是他的话）。"""

    return " ".join(str(text or "").split())


def _is_command(text: str) -> bool:
    return text.startswith(_COMMAND_PREFIXES)


def collect_letter_context(
    path,
    *,
    since: float,
    owner_ids=(),
    last_letter: dict | None = None,
    max_rows: int = MAX_CONTEXT_ROWS,
) -> LetterContext | None:
    """从对话日志里取"他说过的话" + 上一封信的主题。读不到日志返回 `None`。

    **`owner_ids` 空 = 一句话都不给**（fail-closed）：认不出谁是收信人时，宁可她没有
    素材，也不能把一屋子人的话递给写信那一侧。这条不是防御性写法，是这一整个模块的
    边界本身（见模块开头）。
    """

    rows, error = read_rows(path)
    if not rows:
        logger.info("letter_context_no_rows path=%s error=%s", path, error)
        return None
    if len(rows) > max_rows:
        rows = rows[-max_rows:]

    start = float(since or 0.0)
    owners = {str(qq) for qq in tuple(owner_ids or ()) if str(qq)}
    lines: list[str] = []

    if owners:
        for row in rows:
            try:
                at = float(row.get("at"))
            except (TypeError, ValueError):
                continue
            if at < start or str(row.get("direction") or "") != "in":
                continue
            # 表情回应不是"他说话了"（与 `chat_window` 的计数口径一致）。
            if str(row.get("kind") or "") == "reaction":
                continue
            if str(row.get("user_id") or "") not in owners:
                continue
            text = _clean(sanitize_chat_text(row.get("body"),
                                             max_length=MAX_LINE_CHARS * 4))
            if not text or _is_command(text):
                continue
            if len(text) > MAX_LINE_CHARS:
                text = text[:MAX_LINE_CHARS].rstrip() + "…"
            if lines and lines[-1] == text:
                continue
            lines.append(text)

    letter_at = 0.0
    subject = ""
    if isinstance(last_letter, dict):
        try:
            letter_at = float(last_letter.get("at") or 0.0)
        except (TypeError, ValueError):
            letter_at = 0.0
        subject = _clean(last_letter.get("subject"))[:MAX_SUBJECT_CHARS]

    return LetterContext(
        since=start,
        # 留最近说的那几句：信写的是"这段时间"，最近的话最接得上。
        his_lines=tuple(lines[-MAX_LINES:]),
        last_letter_at=letter_at,
        last_letter_subject=subject,
    )
