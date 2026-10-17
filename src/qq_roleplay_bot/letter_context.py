"""信里"他"那一份：**收信人自己说过的话**（带结构），加上她上一封信的主题。

由来（2026-10-13 用户原话）：*"加，不是说了允许读取记忆吗，你是不是不懂什么叫不是非要
一次输入就得到一封信"*。在这之前素材里**一句人话都没有**（`chat_window` 只数千条、
只给号码），她手上唯一有内容的东西是那份固定事实表——实测同一份素材三封信里，有一封
把事实表里的旧事写成了"今天"发生的新事，另一封干脆弃写。用户读到的那封
"根本不像是人写的信"，就是这个形状。

## 2026-10-16 补的那一半：结构（用户四条决定之第 1 条"先甲案"）

原话：**"素材：先甲案 + 判据。只补结构（时间说人话、会话标号、引用链、`has_media`），
不喂别人的原话"**。起因是 10-15 那封真信（`run\\data\\logs\\mail.jsonl` 里那条
`at=1792076506.413`）：素材只给了 8 条**光秃秃的句子**，于是她把其中三条
（"水平一言难尽…" / "谁家好人大二学生…" / "真牛逼的早被挖走了"）读成了一个
**素材里根本没有的场景**——"白天群里有人笑别人没本事，你没跟着笑，还替那个大二学生
解释了几句。我看见了。" 而同一封信里"你说预计九点"（← `预计9点到学校`）与
"你白天说那个游戏烂"（← `烂游戏。`）是对得上的：**自足的短句能连上，需要场景的一句
连不上**。

补的四样，逐条对应上面那处错：

- **时间说人话**：`at` 不再只用来筛窗口，渲染成"今天下午""昨天深夜""3 天前"；
- **会话标号**：`session_id`/`group_id` 收成"群1""私聊1"（本封信内稳定）；
- **引用链只给结构、不给内容**：他这条在接谁的话 ⇒ "接别人的话"/"接你的话"，
  **被引用的那句正文一个字都不带**（那是用户没点头的乙案）；
- **`has_media`**：图/表情包标出来（原来 `（发送了一条媒体消息）[表情包]` 被当成
  一句人话喂了进去）。

**为什么不直接带上别人的原话**（乙案）：`MAIL_DESIGN`（`run\\data\\private_docs\\MAIL_DESIGN.md`）
§3.1 写着"默认**不喂聊天原文**……要加'带几句原文'的版本得你单独点头"，
而且信只寄给他一个人、群里别人的话抄进邮箱性质就变了。这条**要用户单独拍**，
不是我们顺手放宽的（模块开头那条边界仍然只到收信人一个人）。

**为什么加在 `his_details` 而不是把 `his_lines` 改形状**：`his_lines` 的元组形状被
`tests/test_letter_context.py` 逐条钉住（那是"只有他的话、原样、留最近 8 条"这条口径的
守卫），换形状就等于顺手改守卫。新结构走**新字段**，两边对齐由 `len()` 与同序保证。

边界（这是对"一句话都不给"那条的一次破例，**只对收信人一个人破**）：

- 只有**收信人自己**说过的话进得来（`owner_ids` 由核心给：`_letter_owner_ids()`，
  部署级管理员 ∪ 超管）。别人的话**一句都不给**——信只寄给他一个人，他自己的原话
  回到他自己手上不构成外泄；别人的话一旦进了素材，就可能被写进信里再寄给第三人看。
- 只读数、不读别的：走 `quote_samples.read_rows`（与 `chat_window` 同一套读法），
  只取窗口内、有正文、非表情回应、也不是斜杠命令的那几条，按时间从早到晚，
  最多 `MAX_LINES` 条，每条截到 `MAX_LINE_CHARS` 字。
- 只给"他说过什么""那些话是什么时候、在哪儿、在接谁"和"上一封信的主题"，**不给任何计数**：
  素材里报数目会直接引她谈工作量（2026-10-11 用户："不要搞的跟实习生写日报一样"）。
- 读不出日志就返回 `None`（调用方按"他这几天没说什么"处理），**不编**。
"""
from __future__ import annotations

import datetime
import logging
import time
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

#: 一天里的时段——**说人话**（"今天下午"），不给 14:07 这种钟点。
#: 依据：素材里给钟点，她会想把它抄进信里；`daily_report._letter_note_line` 那条
#: "上一封信是几天前"是同一件事。
_TIME_BUCKETS = ((6, "凌晨"), (9, "早上"), (12, "上午"), (14, "中午"),
                 (17, "下午"), (19, "傍晚"), (22, "晚上"), (24, "深夜"))


def _hour_part(hour: int) -> str:
    for limit, name in _TIME_BUCKETS:
        if hour < limit:
            return name
    return "深夜"


def _when_text(at: float, now: float) -> str:
    """把时间戳说成人话：今天下午 / 昨天深夜 / 前天早上 / 3 天前。

    **按"日子"算，不按 86400 秒算**：差 23.3 小时的两封信跨了午夜，按秒算会写成
    "今天"（`daily_report._letter_note_line` 原来就是这个 bug，同一天一起修的）。
    """

    when = datetime.datetime.fromtimestamp(float(at))
    today = datetime.datetime.fromtimestamp(float(now))
    days = (today.date() - when.date()).days
    part = _hour_part(when.hour)
    if days <= 0:
        return f"今天{part}"
    if days == 1:
        return f"昨天{part}"
    if days == 2:
        return f"前天{part}"
    return f"{days} 天前"


@dataclass(frozen=True, slots=True)
class HisLine:
    """他的一句话，**带上那句周围能确定的结构**（正文一个字不动）。

    四样结构都不是"内容"：时间是她本来就知道的，会话标号不是群名，
    引用链只说"在接谁的话"而不带被引用的正文。
    """

    #: 墙上时间（调用方筛窗口用的那个）。
    at: float = 0.0
    #: 说人话的时间（"今天下午"）。
    when: str = ""
    #: 本封信内稳定的会话标号（"群1" / "私聊1"）。
    session: str = ""
    #: 引用链的**结构**："" / "接别人的话" / "接你的话" / "引了一条翻不到的话"。
    reply: str = ""
    #: 这条带图或表情包（她看不到内容，只能知道"他发的是一张图"）。
    media: bool = False
    #: 正文（与同序的 `LetterContext.his_lines` 逐字相同）。
    body: str = ""


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
    #: 与 `his_lines` **同序等长**的结构（见 `HisLine`）。新结构走新字段，是为了不动
    #: `his_lines` 那个被 `tests/test_letter_context.py` 逐条钉住的形状；
    #: 两者对不上时（老形状的替身对象）调用方只渲染 `his_lines`。
    his_details: tuple[HisLine, ...] = ()


def _clean(text: object) -> str:
    """收干净：换行折成空格、去掉首尾空白。**不改一个字**（那是他的话）。"""

    return " ".join(str(text or "").split())


def _is_command(text: str) -> bool:
    return text.startswith(_COMMAND_PREFIXES)


def _session_label(session_id: str, group_id: str, labels: dict[str, str]) -> str:
    """给会话一个本封信内稳定的短标号：群1 / 私聊1（不是群名，也不是群号）。"""

    key = session_id or group_id or ""
    if key in labels:
        return labels[key]
    is_group = bool(group_id) or str(session_id).startswith("group")
    prefix = "群" if is_group else "私聊"
    used = sum(1 for value in labels.values() if value.startswith(prefix))
    labels[key] = f"{prefix}{used + 1}"
    return labels[key]


def _reply_hint(row: dict, by_id: dict[str, dict]) -> str:
    """他这条在接谁的话——**只说结构，不带被引用那句的正文**。"""

    target = str(row.get("reply_to") or "")
    if not target:
        return ""
    quoted = by_id.get(target)
    if quoted is None:
        return "引了一条翻不到的话"
    return "接你的话" if str(quoted.get("direction") or "") == "out" else "接别人的话"


def collect_letter_context(
    path,
    *,
    since: float,
    owner_ids=(),
    last_letter: dict | None = None,
    max_rows: int = MAX_CONTEXT_ROWS,
    now: float | None = None,
) -> LetterContext | None:
    """从对话日志里取"他说过的话"（带结构） + 上一封信的主题。读不到日志返回 `None`。

    **`owner_ids` 空 = 一句话都不给**（fail-closed）：认不出谁是收信人时，宁可她没有
    素材，也不能把一屋子人的话递给写信那一侧。这条不是防御性写法，是这一整个模块的
    边界本身（见模块开头）。

    `now` 只用来把时间说成人话（"今天下午"），不参与筛窗口——窗口由 `since` 定。
    """

    rows, error = read_rows(path)
    if not rows:
        logger.info("letter_context_no_rows path=%s error=%s", path, error)
        return None
    if len(rows) > max_rows:
        rows = rows[-max_rows:]

    start = float(since or 0.0)
    stamp = time.time() if now is None else float(now)
    owners = {str(qq) for qq in tuple(owner_ids or ()) if str(qq)}
    lines: list[str] = []
    details: list[HisLine] = []
    #: 引用链要看"被引的那条是谁说的"：只拿这几行里的消息 ID 做对照，
    #: 翻不到就如实说"翻不到"，不猜。
    by_id = {str(row.get("message_id")): row for row in rows if row.get("message_id")}
    labels: dict[str, str] = {}

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
            # 连着两条一模一样的话只留一条（他手滑发两遍那种）。结构与正文**一起收**，
            # 否则 `his_details` 会和 `his_lines` 错位。
            if lines and lines[-1] == text:
                continue
            lines.append(text)
            details.append(HisLine(
                at=at,
                when=_when_text(at, stamp),
                session=_session_label(str(row.get("session_id") or ""),
                                       str(row.get("group_id") or ""), labels),
                reply=_reply_hint(row, by_id),
                media=bool(row.get("has_media")),
                body=text,
            ))

    letter_at = 0.0
    subject = ""
    if isinstance(last_letter, dict):
        try:
            letter_at = float(last_letter.get("at") or 0.0)
        except (TypeError, ValueError):
            letter_at = 0.0
        subject = _clean(last_letter.get("subject"))[:MAX_SUBJECT_CHARS]

    # 留最近说的那几句：信写的是"这段时间"，最近的话最接得上。
    kept = lines[-MAX_LINES:]
    return LetterContext(
        since=start,
        his_lines=tuple(kept),
        last_letter_at=letter_at,
        last_letter_subject=subject,
        his_details=tuple(details[-MAX_LINES:]) if kept else (),
    )
