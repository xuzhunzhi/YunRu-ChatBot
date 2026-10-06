"""金句样本的**选取**：从对话日志里挑出"她说过、而且被点了表情"的那几条，连上下文一起。

由来（2026-10-06 用户原话）：*"富哥之家群刚刚提出爆的金句都是优质 rl 训练轨迹…
可以通过给消息点的表情来定位"*，紧接着两条纠正：*"只挑三条这个我不理解，我认为金句更
重要的是**说话风格和上下文语境**，需要调用一个 agent 来专门学习"*、*"注意贴表情不是
所有都是金句，比如贴**祝（猪的谐音）就是不赞同或者 bot 回复不恰当**"*。

所以这一份只做**第一步**：把"她的话 + 谁给它点了什么 + 当时在聊什么"整理成一条条
**样本**，交给学习 agent（`quote_learning.py`）去读。**这里一个字都不判断金句/不判断
正负**——正负由 agent 从语境里推（见那个模块），硬编码一张"赞=正、祝=负"的表正是
用户否掉的那种做法。

## 判据（都是现成的数据，不新拉接口、不花钱）

数据源是传输层已经落盘的 `data/logs/chat.jsonl`（`chat_log.py` + `reactions.py`），
三类记录共用一份文件：

| 记录 | 判据 | 用途 |
| --- | --- | --- |
| 她说过的话 | `direction="out"` 且 `kind != "reaction"` | 样本本体 |
| 它被点了什么 | `kind="reaction"`，`message_id` 指回上面那条 | 正/反面的**线索** |
| 当时的上下文 | `direction="in"` 且时间在它之前 | 她当时在回应谁、对方说了什么 |

- **配对只认 `message_id`**：表情回应那一行的 `message_id` 必须**逐字等于**她某条
  消息的 `message_id`。对不上的一律**不计入**（`unmatched_reactions`），
  **不按时间邻近去猜**——猜错会把别人的话当成她的金句，那比漏掉更坏。
- 上下文取她那条**之前**的入站消息（默认 6 条）；“她话之后”群里的反应也取一点
  （默认 2 条入站 + 她自己的后续），因为"贴完表情群里怎么接"是判正负的主要证据。
- `topic` / `intent` 从 `reply.jsonl` 里**那一条的 SESSION CONTEXT 块**取，且要求
  时间对得上（默认 120 秒内，取最近的一次）。**对不上就不写这两个键**，不硬凑。

## 真实数据实测（2026-10-06，`run/data/logs/chat.jsonl` 3135 行）

- `kind="reaction"` **51** 行；其中 `message_id` 能对上 `chat.jsonl` 里某条**入站**
  消息的 15 行，能对上她**自己**那条的 **0** 行；
- 也就是说**这份日志窗口里一条"她被点表情"的样本都没有**：51 个表情全点在别人头上。
  于是 `build_samples` 返回空 → 学习 agent **什么都不做**（这正是要的行为，
  见 `quote_learning.py` 的"没有新金句就不跑"）。本模块**不为了凑样本而放宽配对**。

**落盘**：这一份不落盘（它只读日志、返回内存里的样本）。金句含义表与笔记在
`quote_learning.py` 的 `QuoteStore` 里，落 `data/`（已 gitignored）。
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

#: 上下文取她那条之前几条入站消息。
DEFAULT_CONTEXT_BEFORE = 6
#: 她那条之后再看几条（入站 + 她自己的后续），"群里怎么接"是判正负的主要证据。
DEFAULT_CONTEXT_AFTER = 2
#: 单条消息正文的截断长度（进模型的材料，不是给人看的全文）。
MAX_BODY_CHARS = 240
#: 单条样本最多带几个表情回应。
MAX_REACTIONS_PER_SAMPLE = 6
#: `reply.jsonl` 里那次回复与这条消息最多差多少秒才算"同一个话题"。
TOPIC_MATCH_SECONDS = 120.0

_SESSION_CONTEXT = re.compile(r"--- SESSION CONTEXT BEGIN ---(.*?)--- SESSION CONTEXT END ---", re.S)
_SESSION_FIELD = re.compile(r"^(topic|topic_status|intent|tone|confidence)=(.*)$")


@dataclass(frozen=True, slots=True)
class QuoteReaction:
    """一条**点在她消息上**的表情回应（原样照抄日志里的字段，不解释含义）。"""

    emoji_id: str
    user_id: str
    count: int = 0
    sub_type: str = ""

    def as_data(self) -> dict[str, object]:
        data: dict[str, object] = {"emoji_id": self.emoji_id, "by": self.user_id}
        if self.count:
            data["count"] = self.count
        if self.sub_type:
            data["sub_type"] = self.sub_type
        return data


@dataclass(frozen=True, slots=True)
class QuoteSample:
    """一条候选金句：她的话 + 谁点了什么 + 前后语境。

    `topic` / `intent` 只在能与 `reply.jsonl` 对上时才有值（对不上是空串）。
    """

    message_id: str
    group_id: str
    body: str
    at: float
    reactions: tuple[QuoteReaction, ...]
    context_before: tuple[dict[str, str], ...] = ()
    context_after: tuple[dict[str, str], ...] = ()
    topic: str = ""
    intent: str = ""

    @property
    def sample_id(self) -> str:
        """幂等用的稳定标识：她那条消息的 id（日志滚动后仍然稳定，只有换群会撞）。"""

        return f"{self.group_id}:{self.message_id}"

    def emoji_ids(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(r.emoji_id for r in self.reactions))

    def as_data(self) -> dict[str, object]:
        """给学习 agent 看的那一份（DATA，不是指令）。"""

        data: dict[str, object] = {
            "sample_id": self.sample_id,
            "group_id": self.group_id,
            "她说的": _clip(self.body),
            "被点了": [r.as_data() for r in self.reactions],
        }
        if self.topic:
            data["当时的话题"] = self.topic
        if self.intent:
            data["对方想干什么"] = self.intent
        if self.context_before:
            data["她开口之前群里在说"] = _lines(self.context_before)
        if self.context_after:
            data["她说完之后群里接的"] = _lines(self.context_after)
        return data


@dataclass(frozen=True, slots=True)
class SampleSelection:
    """一次选取的结果：样本 + 拒绝计入的计数（**如实报，不静默丢**）。"""

    samples: tuple[QuoteSample, ...] = ()
    reactions: int = 0
    matched_reactions: int = 0
    unmatched_reactions: int = 0
    quotes_seen: int = 0
    lines: int = 0
    error: str = ""

    @property
    def new_reactions(self) -> int:
        return self.matched_reactions

    def snapshot(self) -> dict[str, object]:
        return {
            "samples": len(self.samples),
            "reactions": self.reactions,
            "matched_reactions": self.matched_reactions,
            "unmatched_reactions": self.unmatched_reactions,
            "quotes_seen": self.quotes_seen,
            "lines": self.lines,
            "error": self.error,
        }


def _clip(text: object, limit: int = MAX_BODY_CHARS) -> str:
    body = " ".join(str(text or "").split())
    return body if len(body) <= limit else body[: limit - 1] + "…"


def _lines(messages: tuple[dict[str, str], ...]) -> list[str]:
    out: list[str] = []
    for item in messages:
        who = item.get("who") or "?"
        body = item.get("body") or ""
        out.append(f"{who}：{body}" if body else f"{who}：（没说话）")
    return out


def _clean_id(value: object) -> str:
    """`message_id` 一律当**字符串**处理（实测有负数与正数两种，见 `reactions.py`）。"""

    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return value.strip()
    return ""


def _float(value: object) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def _is_reaction(row: dict) -> bool:
    return str(row.get("kind") or "") == "reaction"


def _is_her_message(row: dict) -> bool:
    """她的消息：`direction="out"` 且不是反应记录（反应记录本身也是 `direction="in"`）。"""

    return (str(row.get("direction") or "") == "out" and not _is_reaction(row)
            and bool(_clean_id(row.get("message_id"))))


def _is_incoming(row: dict) -> bool:
    return str(row.get("direction") or "") == "in" and not _is_reaction(row)


def _display_name(row: dict) -> str:
    name = str(row.get("sender_name") or "").strip()
    card = str(row.get("sender_card") or "").strip()
    if name and card and card != name:
        return f"{name}（{card}）"
    return name or card or str(row.get("user_id") or "?")


def read_rows(path: Path | str) -> tuple[list[dict], str]:
    """读一份 JSONL 日志。**坏行跳过、整份读不开就返回空**（调用方据此什么都不做）。"""

    rows: list[dict] = []
    try:
        with Path(path).open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except FileNotFoundError:
        return [], "missing"
    except OSError as exc:
        logger.warning("quote_samples_read_failed category=%s", type(exc).__name__)
        return [], type(exc).__name__
    return rows, ""


def read_topic_index(reply_log: Path | str | None) -> tuple[dict[str, list[tuple[float, str, str]]], str]:
    """从 `reply.jsonl` 里抽出每次回复的 `(时刻, topic, intent)`，按会话分组。

    `reply.jsonl` 是模型日志：每条记录的 `input` 里有一段 SESSION CONTEXT，
    写着那一次回复时的 `topic` 与 `intent`。它没有单独的顶层字段，所以只能解析文本，
    解析不出来就当没有（**不猜**）。
    """

    index: dict[str, list[tuple[float, str, str]]] = {}
    if reply_log is None:
        return index, ""
    rows, error = read_rows(reply_log)
    for row in rows:
        at = _float(row.get("at"))
        session_id = str(row.get("session_id") or "")
        if not session_id or not at:
            continue
        body = row.get("input")
        if not isinstance(body, str):
            continue
        match = _SESSION_CONTEXT.search(body)
        if not match:
            continue
        topic = intent = ""
        for line in match.group(1).splitlines():
            field_match = _SESSION_FIELD.match(line.strip())
            if not field_match:
                continue
            key, value = field_match.group(1), field_match.group(2).strip()
            if key == "topic":
                topic = value
            elif key == "intent":
                intent = value
        if topic or intent:
            index.setdefault(session_id, []).append((at, topic, intent))
    for items in index.values():
        items.sort(key=lambda item: item[0])
    return index, error


def _topic_for(index, session_id: str, at: float) -> tuple[str, str]:
    """找这条消息**最近的那次回复**的话题；差太远就算了（宁缺勿错）。"""

    items = index.get(session_id) or []
    if not items or not at:
        return "", ""
    best: tuple[float, str, str] | None = None
    for item in items:
        gap = abs(item[0] - at)
        if gap > TOPIC_MATCH_SECONDS:
            continue
        if best is None or gap < abs(best[0] - at):
            best = item
    if best is None:
        return "", ""
    return best[1], best[2]


def _context(rows: list[dict], index: int, *, before: int, after: int) -> tuple[
        tuple[dict[str, str], ...], tuple[dict[str, str], ...]]:
    """她那条**之前**的入站消息与**之后**的入站消息（各自最多 N 条）。"""

    earlier: list[dict[str, str]] = []
    for row in reversed(rows[:index]):
        if len(earlier) >= before:
            break
        if not _is_incoming(row):
            continue
        earlier.append({"who": _display_name(row), "body": _clip(row.get("body"))})
    earlier.reverse()

    later: list[dict[str, str]] = []
    for row in rows[index + 1:]:
        if len(later) >= after:
            break
        if _is_incoming(row):
            later.append({"who": _display_name(row), "body": _clip(row.get("body"))})
    return tuple(earlier), tuple(later)


def build_samples(rows: list[dict], *, topic_index=None, group_ids=(),
                  context_before: int = DEFAULT_CONTEXT_BEFORE,
                  context_after: int = DEFAULT_CONTEXT_AFTER) -> SampleSelection:
    """把日志行整理成样本。**只认 `message_id` 配对**，不做任何正负判断。

    `group_ids` 给了就只收这些群（与"允许的群"同一口径）；空元组 = 全都收。
    """

    allowed = {str(g) for g in group_ids if str(g)}
    reactions: list[dict] = [row for row in rows if _is_reaction(row)]
    by_target: dict[str, list[dict]] = {}
    for row in reactions:
        target = _clean_id(row.get("message_id"))
        if target:
            by_target.setdefault(target, []).append(row)

    samples: list[QuoteSample] = []
    matched = 0
    for position, row in enumerate(rows):
        if not _is_her_message(row):
            continue
        message_id = _clean_id(row.get("message_id"))
        group_id = str(row.get("group_id") or "")
        if allowed and group_id not in allowed:
            continue
        hits = by_target.get(message_id) or []
        if not hits:
            continue
        matched += len(hits)
        reactions_of_quote = tuple(
            QuoteReaction(
                emoji_id=_clean_id(hit.get("emoji_id")),
                user_id=_clean_id(hit.get("user_id")),
                count=int(_float(hit.get("count"))),
                sub_type=str(hit.get("sub_type") or ""),
            )
            for hit in hits[:MAX_REACTIONS_PER_SAMPLE]
            if _clean_id(hit.get("emoji_id"))
        )
        if not reactions_of_quote:
            continue
        earlier, later = _context(rows, position, before=context_before, after=context_after)
        topic, intent = _topic_for(topic_index or {}, str(row.get("session_id") or ""),
                                   _float(row.get("at")))
        samples.append(QuoteSample(
            message_id=message_id,
            group_id=group_id,
            body=_clip(row.get("body")),
            at=_float(row.get("at")),
            reactions=reactions_of_quote,
            context_before=earlier,
            context_after=later,
            topic=topic,
            intent=intent,
        ))

    return SampleSelection(
        samples=tuple(samples),
        reactions=len(reactions),
        matched_reactions=matched,
        unmatched_reactions=len(reactions) - matched,
        quotes_seen=sum(1 for row in rows if _is_her_message(row)),
        lines=len(rows),
    )


def collect_samples(chat_log_path: Path | str, *, reply_log_path: Path | str | None = None,
                    group_ids=(), context_before: int = DEFAULT_CONTEXT_BEFORE,
                    context_after: int = DEFAULT_CONTEXT_AFTER) -> SampleSelection:
    """一次选取的入口：读两份日志 → 返回样本。**任何读不开都返回空样本 + 原因**。"""

    rows, error = read_rows(chat_log_path)
    if not rows:
        return SampleSelection(error=error or "empty")
    index, _ = read_topic_index(reply_log_path)
    selection = build_samples(rows, topic_index=index, group_ids=group_ids,
                             context_before=context_before, context_after=context_after)
    return selection if not error else SampleSelection(
        samples=selection.samples, reactions=selection.reactions,
        matched_reactions=selection.matched_reactions,
        unmatched_reactions=selection.unmatched_reactions,
        quotes_seen=selection.quotes_seen, lines=selection.lines, error=error)


def new_samples(selection: SampleSelection, seen: set[str]) -> tuple[QuoteSample, ...]:
    """去掉**已经学过、而且没有新表情**的样本（幂等：同一批不会重复烧钱）。

    判据是 `sample_id` 在 `seen` 里，且它的表情集合与上次记下的完全一致。
    只记 id 的话，同一句后来又被贴了一个表情（新的信号）就永远学不到了。
    """

    fresh: list[QuoteSample] = []
    for sample in selection.samples:
        key = sample.sample_id
        fingerprint = ",".join(sorted(
            f"{r.emoji_id}:{r.user_id}" for r in sample.reactions))
        if f"{key}|{fingerprint}" in seen:
            continue
        fresh.append(sample)
    return tuple(fresh)


def fingerprint_of(sample: QuoteSample) -> str:
    """记进"学过了"那本账的一行（与 `new_samples` 的判据逐字对齐）。"""

    reactions = ",".join(sorted(f"{r.emoji_id}:{r.user_id}" for r in sample.reactions))
    return f"{sample.sample_id}|{reactions}"


def counts_by_emoji(selection: SampleSelection) -> dict[str, dict[str, int]]:
    """按群、按 emoji 数一遍（给操作者看的那张表用：**证据够不够数**一眼看得出）。"""

    table: dict[str, dict[str, int]] = {}
    for sample in selection.samples:
        group = table.setdefault(sample.group_id, {})
        for reaction in sample.reactions:
            group[reaction.emoji_id] = group.get(reaction.emoji_id, 0) + 1
    return table
