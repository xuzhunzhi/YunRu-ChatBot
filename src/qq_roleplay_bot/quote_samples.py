"""金句样本的**选取**：从对话日志里挑出"她说过、而且被点了表情"的那几条，连上下文一起。

由来（2026-10-06 用户原话）：*"富哥之家群刚刚提出爆的金句都是优质 rl 训练轨迹…
可以通过给消息点的表情来定位"*，紧接着两条纠正：*"只挑三条这个我不理解，我认为金句更
重要的是**说话风格和上下文语境**，需要调用一个 agent 来专门学习"*、*"注意贴表情不是
所有都是金句，比如贴**祝（猪的谐音）就是不赞同或者 bot 回复不恰当**"*。

所以这一份只做**第一步**：把"她的话 + 谁给它点了什么 + 当时在聊什么"整理成一条条
**样本**，交给学习 agent（`quote_learning.py`）去读。**这里一个字都不判断金句/不判断
正负**——正负由 agent 从语境里推（见那个模块）。

样本里那个表情**叫什么名字**照抄 QQ 自己的（`face_names.py`：`76` → `赞`、`12951` → `㊗`），
**名字只是线索、不是方向**：`赞` 在有些群里是真心捧、在有些群里是阴阳，仍然由 agent 按
语境判。这条路上**唯一的先验**是用户 2026-10-11 点名写死的那一个（`㊗` = 不赞同，
见 `quote_learning.KNOWN_EMOJI_SENSES`）——原来那句"代码里不许有'赞=正、祝=负'的表"
在这一点上已经**按用户的要求改了**，别再把这段读成"一个先验都没有"。

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
- **上下文严格限定在同一会话**（`session_id`，见 `context_around`）。这一条是
  2026-10-06 的用户报缺陷补上的：原来按**日志位置**抓前后 N 条，于是别群的消息
  混进了她的语境（实测 `run/data/logs/chat.jsonl`：128 行上下文里 **38 行**是从
  **别的会话**抓来的，其中 32 行明确属于主群 `1084401296`）。
  条数不够就**如实少给**，**绝不**拿别群的话补位。
- `topic` / `intent` 从 `reply.jsonl` 里**那一条的 SESSION CONTEXT 块**取，且要求
  时间对得上（默认 120 秒内，取最近的一次）。**对不上就不写这两个键**，不硬凑。

## 真实数据实测（2026-10-06，`run/data/logs/chat.jsonl` 3135 行）

- `kind="reaction"` **51** 行；其中 `message_id` 能对上 `chat.jsonl` 里某条**入站**
  消息的 15 行，能对上她**自己**那条的 **0** 行；
- 也就是说**这份日志窗口里一条"她被点表情"的样本都没有**：51 个表情全点在别人头上。
  于是 `build_samples` 返回空 → 学习 agent **什么都不做**（这正是要的行为，
  见 `quote_learning.py` 的"没有新金句就不跑"）。本模块**不为了凑样本而放宽配对**。

## 真实数据实测（2026-10-06 晚，同一份日志涨到 5201 行；**跨会话那次的实测**）

这一趟才真的跑出了样本（`kind="reaction"` 321 行，其中 **22** 行点在她自己的消息上，
全在群 `908696203`；`build_samples` 出 8 条样本）。也正因为有样本，才量出上面那个缺陷：

量法是**同一个锚点上同时算两串**（"按日志位置抓" vs "按 `session_id` 过滤"），
再取差集——这样谁都不用猜锚点（`message_id` 会跨会话撞），也不受上层回退逻辑影响：

| 读口 | 过滤后的上下文行 | **不过滤会串进来的行** | 来自哪个会话 |
| --- | --- | --- | --- |
| 学习输入 `build_samples` | 128 | **38**（before 24 + after 14） | `group:1084401296` ×32，另 6 行是跨群复用的图片/卡片正文 |
| 证据读口 `collect_evidence` | 同上（两个读口共用 `context_around`） | 同上 | 同上 |

改完（按 `session_id` 过滤 + 两处收敛成 `context_around`）再跑同一份日志：
真实函数返回的那一串与"按会话过滤"那一串**逐字相同 32 次、不同 0 次** → **跨会话 0 行**。
条数会**如实变少**（不够就是不够，不拿别群补）——例如她那条 `-712103696` 的 6 行污染
被丢掉后，那一条的 before 只剩 0 行，于是那一条改走"样本自带"的回退分支。

**落盘**：这一份不落盘（它只读日志、返回内存里的样本）。金句含义表与笔记在
`quote_learning.py` 的 `QuoteStore` 里，落 `data/`（已 gitignored）。
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path

from . import face_names

logger = logging.getLogger(__name__)

#: 上下文取她那条之前几条入站消息。
DEFAULT_CONTEXT_BEFORE = 6
#: 她那条之后再看几条（入站 + 她自己的后续），"群里怎么接"是判正负的主要证据。
DEFAULT_CONTEXT_AFTER = 2
#: 单条消息正文的截断长度（进模型的材料，不是给人看的全文）。
MAX_BODY_CHARS = 240
#: 单条样本最多带几个表情回应。
MAX_REACTIONS_PER_SAMPLE = 6
#: **没有表情**的样本（2026-10-13 用户："学的太少了"）。
#:
#: 实情：她 1442 条发言里**只有 97 条**被点过表情——只学这 97 条，池子早就干了
#: （档案 `seen` 107 条，日志里天天 `quote_learning_idle`）。所以开第二条来源：
#: 她说的话**没人点、但说完之后这么短时间里有人接着说话**——弱信号，含义是
#: "有人在接"，**不是**"这句说得好"。
#:
#: 因此这类样本在提示词里只许写 `style_notes` / `context_notes`（她怎么说、
#: 什么场合说什么），**不许**下"被嫌/避开"的结论：判方向仍然只认表情那一条路
#: （尤其 `㊗` = 不赞同，用户点名说定的）。
DEFAULT_UNREACTIVE_WINDOW_SECONDS = 120.0
#: `reply.jsonl` 里那次回复与这条消息最多差多少秒才算"同一个话题"。
TOPIC_MATCH_SECONDS = 120.0

# --- "证据读口"（面板审核用）的那几个数 ---------------------------------------
#
# 与上面那几个**学习用**的数是两回事，别混着改：
# 学习那一批是"喂给模型的材料"，截得短、条数少（省 token）；
# 这一批是"给人审核的证据"，要的是**原文与上下文**（客户 2026-10-06：
# *"金句，你连原文和上下文都不贴出来怎么审核"*）。所以这里**不截正文**。
#
#: 证据里一次最多给几条（默认最近 N 条，见 `DEFAULT_EVIDENCE_LIMIT`）。
MAX_EVIDENCE_LIMIT = 50
#: 默认给几条。
DEFAULT_EVIDENCE_LIMIT = 10
#: 上下文的**扫描窗口**：她那条前后各看这么多**日志行**，再从中筛入站消息。
#: 给 0 就是"按行数只取已有的那几条"——注意这里数的是行，不是筛出来的条数
#: （学新词的那份 `_context` 数的是条数，两处判据不同是故意的）。
EVIDENCE_CONTEXT_WINDOW = 40
#: 一条证据的上下文里，最多给几条入站消息。
EVIDENCE_CONTEXT_MAX = 8
#: 找一条证据时最多走多少条日志（防"这份日志大得离谱"时整份扫一遍）。
MAX_EVIDENCE_SCAN = 2000
#: 一条证据里最多列几个表情回应。
MAX_EVIDENCE_REACTIONS = 12

_SESSION_CONTEXT = re.compile(r"--- SESSION CONTEXT BEGIN ---(.*?)--- SESSION CONTEXT END ---", re.S)
_SESSION_FIELD = re.compile(r"^(topic|topic_status|intent|tone|confidence)=(.*)$")


@dataclass(frozen=True, slots=True)
class QuoteReaction:
    """一条**点在她消息上**的表情回应（原样照抄日志里的字段，不解释含义）。"""

    emoji_id: str
    user_id: str
    count: int = 0
    sub_type: str = ""

    #: 给**学习 agent**看的那一份里，表情只用名字（`西瓜`），不带那串数字。
    #: 名字是代码查出来的（`face_names`，纯规则），模型不该跟编号打交道；
    #: 查不到名字、或者**这一批里两个编号重名**时，才退回带 `emoji_id`。
    LEARNING_KEY = "表情"

    def as_data(self, *, for_learning: bool = False, names: "BatchNames | None" = None,
                ) -> dict[str, object]:
        """`for_learning=False`（默认，面板证据用）：`emoji_id` + 有名字就带 `名字`。

        `for_learning=True`：**只给名字**（`"表情": "西瓜"`）。规则能定的那一步
        （编号 → 名字）由代码做完，不留给模型去猜；确实没有名字（例如 `234`）时
        才把编号给它——那时编号是唯一能指认它的东西。
        """

        data: dict[str, object] = {}
        # `names` 没给（面板证据那条路）就现查本地那份名字表；给了就用 caller 那一份
        # ——学习那条路必须用**这一批的**对照（重名的要靠它区分）。
        if names is None:
            name = face_names.shared().label_for(self.emoji_id)
            ambiguous: frozenset[str] = frozenset()
        else:
            name, ambiguous = names.name_of(self.emoji_id), names.ambiguous
        if for_learning and name and name not in ambiguous:
            data[self.LEARNING_KEY] = name
        else:
            data["emoji_id"] = self.emoji_id
            if name and not for_learning:
                data["名字"] = name
        data["by"] = self.user_id
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

    #: `as_data()` 里那两个字段的中文名。**存进去的和读出来的共用一份**，
    #: 免得"当时的话题"在写侧叫一个名、在证据里又叫另一个（那就等于对不上）。
    TOPIC_KEY = "当时的话题"
    INTENT_KEY = "对方想干什么"
    CONTEXT_BEFORE_KEY = "她开口之前群里在说"
    CONTEXT_AFTER_KEY = "她说完之后群里接的"

    def as_data(self, *, for_learning: bool = False, names: "BatchNames | None" = None,
                ) -> dict[str, object]:
        """给学习 agent 看的那一份（DATA，不是指令）。"""

        data: dict[str, object] = {
            "sample_id": self.sample_id,
            "group_id": self.group_id,
            "她说的": _clip(self.body),
            # 没有表情的样本明写「（无）」：提示词里对这两类给的规矩不同
            # （有表情的才允许判方向、写"避开"）。
            "被点了": ([r.as_data(for_learning=for_learning, names=names)
                        for r in self.reactions] if self.reactions else "（无）"),
        }
        if self.topic:
            data[self.TOPIC_KEY] = self.topic
        if self.intent:
            data[self.INTENT_KEY] = self.intent
        if self.context_before:
            data[self.CONTEXT_BEFORE_KEY] = _lines(self.context_before)
        if self.context_after:
            data[self.CONTEXT_AFTER_KEY] = _lines(self.context_after)
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


def _line_body(row: dict) -> str:
    """证据读口里的一条正文：**只做空白归一，不截断**。

    为什么不截：这一份是给**人**审核的（原文 + 上下文），截了就没法对照；
    给模型的那一份（`_clip`）才需要省 token。两者是两回事。
    """

    return " ".join(str(row.get("body") or "").split())


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


def _followed_by_incoming(rows: list[dict], position: int, window: float) -> bool:
    """她这条之后 `window` 秒内有没有入站消息（弱信号："有人在接"）。

    这是给**没有表情**的样本用的补位判据：表情是"说得好不好"的线索，这个只是
    "有没有人在听"。所以它挑出来的样本**不许**用来判方向。
    """

    if window <= 0:
        return False
    at = _float(rows[position].get("at"))
    for row in rows[position + 1:]:
        stamp = _float(row.get("at"))
        if stamp - at > window:
            return False
        if _is_incoming(row):
            return True
    return False


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


def _session_of(row: dict) -> str:
    """一行属于**哪个会话**——取上下文时唯一允许的边界。

    判据用 `session_id`（日志里形如 `group:<群号>`，私聊是 `private:<号>`）；
    它缺失时才退到 `group_id`。**不拿 `group_id` 当主判据**：私聊那行的 `group_id`
    是空的，只认群号就会把私聊并进"空群号"那一堆里——那正是这次要修的病。
    """

    session = str(row.get("session_id") or "").strip()
    if session:
        return session
    group = str(row.get("group_id") or "").strip()
    return f"group:{group}" if group else ""


def context_around(rows: list[dict], anchor: dict, *, before: int, after: int,
                   window: int, clip: bool) -> tuple[
        tuple[dict[str, str], ...], tuple[dict[str, str], ...]]:
    """**取上下文的唯一实现**：锚点那条前后各若干条入站消息，**严格限定在同一会话**。

    学习用的 `build_samples` 与审核用的证据读口（`context_for`）都走这一处——
    原来两处各写一份"按日志位置抓前后 N 条"，于是**两个地方都会串群**：
    她把 B 群里的话当成了 A 群的语境，学出来的含义表与笔记本身就是脏的
    （用户 2026-10-06 报的正是这个：*"你没有区分群聊……你把**不同群聊的上下文**
    都塞到金句的上下文里了"*）。收敛成一份之后，"哪一处还漏了过滤"这种漏洞
    在结构上就不存在了。

    规则（几条都是硬的）：

    * **一行都不许跨会话**：会话与锚点不同的一行直接跳过，**不算条数**；
    * 条数不够就**如实少给**——**绝不**拿别群的话补位（宁少勿错）；
    * 最多往回 / 往前看 `window` 行（`window <= 0` = 不限，看到日志开头 / 结尾）；
    * `clip=True` 截 240 字（给模型的材料省 token），`False` 给**原文**（给人审核）。

    返回 `(她开口之前, 她说完之后)`，各自是 `{"who": …, "body": …}` 的元组。
    """

    index = -1
    for position, row in enumerate(rows):
        if row is anchor:
            index = position
            break
    session = _session_of(anchor)
    start = max(0, index - window) if window > 0 else 0
    stop = (index + 1 + window) if window > 0 else len(rows)
    # ⚠️ 两个 `_` 函数的入参**不是一个东西**，别把整行递进去：
    # `_clip(text)` 收**正文字符串**（给模型的那份截 240 字），
    # `_line_body(row)` 收**整行 dict**（给人审核的那份只归一空白、不截）。
    # 混用会静默产出 `str(字典)`，而这处曾真的写错过——现在由
    # `tests/test_quote_session_isolation.py` 的
    # `test_context_lines_are_bodies_not_stringified_rows` 钉住正文形状。
    if clip:
        def take(row: dict) -> str:
            return _clip(row.get("body"))
    else:
        take = _line_body

    earlier: list[dict[str, str]] = []
    for item in reversed(rows[start:index]):
        if len(earlier) >= before:
            break
        if not _is_incoming(item) or _session_of(item) != session:
            continue
        earlier.append({"who": _display_name(item), "body": take(item)})
    earlier.reverse()

    later: list[dict[str, str]] = []
    for item in rows[index + 1:stop]:
        if len(later) >= after:
            break
        if not _is_incoming(item) or _session_of(item) != session:
            continue
        later.append({"who": _display_name(item), "body": take(item)})
    return tuple(earlier), tuple(later)


def _context(rows: list[dict], index: int, *, before: int, after: int) -> tuple[
        tuple[dict[str, str], ...], tuple[dict[str, str], ...]]:
    """她那条**之前**的入站消息与**之后**的入站消息（各自最多 N 条，同一会话内）。

    只是 `context_around` 的一个薄壳（调用方手里是下标，锚点就在这里取）。
    正文走 `_clip`——这一份是给模型看的材料。
    """

    return context_around(rows, rows[index], before=before, after=after,
                          window=0, clip=True)


def context_for(rows: list[dict], message_id: object, *, before: int, after: int,
                session: str = "") -> tuple[
        tuple[dict[str, str], ...], tuple[dict[str, str], ...]]:
    """按 `message_id` 找她那条消息，再取它前后的入站消息（**复数条**）。

    这是"补上下文"用的：样本里本来只存了 `body`（学习那一批要的是短材料），
    所以证据读口要按 id 回日志里把**当时的上下文**补出来。找不到那条就是两串空。

    **与学习那一份（`_context` → `context_around`）是同一个实现**，
    只有两处参数不同：这里按 `EVIDENCE_CONTEXT_WINDOW` 行扫窗口、
    正文取**原文不截**（给人审核）。**过滤会话那一条两边逐字一样**——
    这正是要收敛成同一处的原因。

    `session` 给了就**连会话一起比**（推荐）：`message_id` 是传输层给的原始 id，
    本模块**不假设**它跨会话唯一（实测这份日志里 4579 个入站 id 没有一个跨会话撞号，
    但这不在任何契约里；真撞了，只按 id 找就会把别群那条当成本条）。
    为空时退到"第一条同 id 的"。"""

    target = _clean_id(message_id)
    if not target:
        return (), ()
    wanted_session = str(session or "").strip()
    for row in rows[:MAX_EVIDENCE_SCAN]:
        if not _is_her_message(row) or _clean_id(row.get("message_id")) != target:
            continue
        if wanted_session and _session_of(row) != wanted_session:
            continue
        return context_around(rows, row, before=before, after=after,
                              window=EVIDENCE_CONTEXT_WINDOW, clip=False)
    return (), ()


def build_samples(rows: list[dict], *, topic_index=None, group_ids=(),
                  context_before: int = DEFAULT_CONTEXT_BEFORE,
                  context_after: int = DEFAULT_CONTEXT_AFTER,
                  unreactive_max: int = 0,
                  unreactive_window_seconds: float = DEFAULT_UNREACTIVE_WINDOW_SECONDS,
                  ) -> SampleSelection:
    """把日志行整理成样本。**只认 `message_id` 配对**，不做任何正负判断。

    `group_ids` 给了就只收这些群（与"允许的群"同一口径）；空元组 = 全都收。

    `unreactive_max > 0` 时**再收一类样本**（2026-10-13 用户："学的太少了"）：
    她说过、**一个表情都没有**、但说完之后 `unreactive_window_seconds` 秒内
    有人接着说话。只取**最近的** `unreactive_max` 条，而且**排在带表情的样本后面**
    （先学有方向的那批）。这类样本只许写"她怎么说 / 什么场合说什么"。
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

    unreactive: list[QuoteSample] = []
    if unreactive_max > 0:
        for position, row in enumerate(rows):
            if not _is_her_message(row):
                continue
            message_id = _clean_id(row.get("message_id"))
            group_id = str(row.get("group_id") or "")
            if allowed and group_id not in allowed:
                continue
            if by_target.get(message_id):
                continue  # 有表情的走上一条路（只有那些允许判方向）
            if not _followed_by_incoming(rows, position, unreactive_window_seconds):
                continue
            earlier, later = _context(rows, position, before=context_before, after=context_after)
            topic, intent = _topic_for(topic_index or {}, str(row.get("session_id") or ""),
                                       _float(row.get("at")))
            unreactive.append(QuoteSample(
                message_id=message_id,
                group_id=group_id,
                body=_clip(row.get("body")),
                at=_float(row.get("at")),
                reactions=(),
                context_before=earlier,
                context_after=later,
                topic=topic,
                intent=intent,
            ))

    return SampleSelection(
        samples=tuple(samples) + tuple(unreactive[-unreactive_max:] if unreactive_max else ()),
        reactions=len(reactions),
        matched_reactions=matched,
        unmatched_reactions=len(reactions) - matched,
        quotes_seen=sum(1 for row in rows if _is_her_message(row)),
        lines=len(rows),
    )


def collect_samples(chat_log_path: Path | str, *, reply_log_path: Path | str | None = None,
                    group_ids=(), context_before: int = DEFAULT_CONTEXT_BEFORE,
                    context_after: int = DEFAULT_CONTEXT_AFTER,
                    unreactive_max: int = 0,
                    unreactive_window_seconds: float = DEFAULT_UNREACTIVE_WINDOW_SECONDS,
                    ) -> SampleSelection:
    """一次选取的入口：读两份日志 → 返回样本。**任何读不开都返回空样本 + 原因**。

    `unreactive_max` 的来历见 `build_samples`（"没有表情、但有人在接"的那一类）。
    """

    rows, error = read_rows(chat_log_path)
    if not rows:
        return SampleSelection(error=error or "empty")
    index, _ = read_topic_index(reply_log_path)
    selection = build_samples(rows, topic_index=index, group_ids=group_ids,
                             context_before=context_before, context_after=context_after,
                             unreactive_max=unreactive_max,
                             unreactive_window_seconds=unreactive_window_seconds)
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


def batch_emoji_names(samples) -> "BatchNames":
    """这一批样本里**名字 ↔ 编号**的对照（含"在这一批里重名"的那些名字）。

    为什么要按"这一批"算而不是查整份目录：整份目录里 404 条有 24 个名字是重的
    （`西瓜` 同时是编号 `89` 和那个 🍉，实测），而**日志里出现过的 38 个编号一个都不撞**
    （同一份实测）。所以"这一批里唯一"就足够精确，重名的那几个退回带编号——**不靠猜**。

    模型只看得见名字、回话时也只报名字，所以这里两个方向都要：拼样本要"编号 → 名字"，
    解析它回的话要"名字 → 编号"。
    """

    by_id: dict[str, str] = {}
    ids_of: dict[str, set[str]] = {}
    for sample in samples:
        for reaction in sample.reactions:
            name = face_names.shared().label_for(reaction.emoji_id)
            if not name:
                continue
            by_id.setdefault(reaction.emoji_id, name)
            ids_of.setdefault(name, set()).add(reaction.emoji_id)
    ambiguous = frozenset(name for name, ids in ids_of.items() if len(ids) > 1)
    by_name = {name: next(iter(ids)) for name, ids in ids_of.items()
               if name not in ambiguous}
    return BatchNames(by_id=by_id, by_name=by_name, ambiguous=ambiguous)


@dataclass(frozen=True, slots=True)
class BatchNames:
    """一批样本的"名字 ↔ 编号"对照（模型只看见名字；代码两边都要用）。"""

    #: 编号 → 名字（拼样本时用）。
    by_id: dict[str, str]
    #: 名字 → 编号（解析模型回话时用；**只收这一批里唯一的那些**）。
    by_name: dict[str, str]
    #: 这一批里重名的那些名字（它们退回用编号，避免"猜是哪一个"）。
    ambiguous: frozenset[str] = frozenset()

    def name_of(self, emoji_id: object) -> str:
        """这个编号在这一批里叫什么（查不到就是空串）。"""

        return self.by_id.get(str(emoji_id), "")

    def resolve(self, label: object) -> str:
        """模型报的名字翻回编号；**翻不出来就是空串**（它编的名字不该变成一个乱标的编号）。"""

        return self.by_name.get(str(label if label is not None else ""), "")


def counts_by_emoji(selection: SampleSelection) -> dict[str, dict[str, int]]:
    """按群、按 emoji 数一遍（给操作者看的那张表用：**证据够不够数**一眼看得出）。"""

    table: dict[str, dict[str, int]] = {}
    for sample in selection.samples:
        group = table.setdefault(sample.group_id, {})
        for reaction in sample.reactions:
            group[reaction.emoji_id] = group.get(reaction.emoji_id, 0) + 1
    return table


# --- 证据读口：把"当时到底发生了什么"整条取出来 --------------------------------


@dataclass(frozen=True, slots=True)
class EvidenceEntry:
    """**一条证据**：她的原文 + 消息 id / 时间 / 群 + 当时的上下文 + 谁贴了什么。

    与 `QuoteSample` 的差别（别把两者当成一份东西）：

    |  | `QuoteSample`（学习用） | `EvidenceEntry`（审核用） |
    | --- | --- | --- |
    | 正文 | `_clip` 截 240 字 | **原文，不截** |
    | 上下文 | 只在她**被选中**那一次采 | 按 id 回日志补（找不到就空） |
    | 存不存 | 只活在内存里 | 也只活在内存里（`as_data` 给人看） |

    `sample` 是它对应的那条学习样本——**有值就代表学习 agent 真的读过它**
    （`结论` 那些字段也才有来源）。为 `None` 时说明她这条被点过表情、
    但这一轮没进样本（见 `_collect_evidence` 的说明）。
    """

    message_id: str
    group_id: str
    body: str
    at: float
    reactions: tuple[QuoteReaction, ...] = ()
    context_before: tuple[dict[str, str], ...] = ()
    context_after: tuple[dict[str, str], ...] = ()
    sample: QuoteSample | None = None

    def as_data(self) -> dict[str, object]:
        """给人（面板 / 操作者）看的那一份。**全是只读证据，没有一处是判断。**"""

        data: dict[str, object] = {
            "message_id": self.message_id,
            "group_id": self.group_id,
            "at": self.at,
            "她说的": self.body,
            "被点了": [r.as_data() for r in self.reactions],
            "她开口之前群里在说": _lines(self.context_before),
            "她说完之后群里接的": _lines(self.context_after),
        }
        if self.sample is not None:
            data["当时的话题"] = self.sample.topic
            data["对方想干什么"] = self.sample.intent
            data["样本里的她说的"] = self.sample.body
        return data


def evidence_pages(entries: tuple[EvidenceEntry, ...], *, offset: int = 0,
                   limit: int = DEFAULT_EVIDENCE_LIMIT) -> tuple[tuple[EvidenceEntry, ...], int]:
    """分页：**默认最近 N 条**（新→旧），`offset` 往后翻。返回 `(这一页, 总数)`。"""

    total = len(entries)
    start = max(0, int(offset or 0))
    size = max(0, min(MAX_EVIDENCE_LIMIT, int(limit if limit is not None
                                            else DEFAULT_EVIDENCE_LIMIT)))
    if not size or start >= total:
        return (), total
    return tuple(entries[start:start + size]), total


def _entries_for(rows: list[dict], selection: SampleSelection, group: str, *,
                 wanted: set[str]) -> list[EvidenceEntry]:
    """把样本（新→旧）翻成一条条证据；`wanted` 里那几条**没有样本也要给**。

    两处刻意与样本不一样（那是"给模型看的材料"，这是"给人审核的证据"）：

    * **正文取自日志那一行**，不是样本里那份截到 240 字的（要审就要看原文）；
    * 上下文按 id **回日志补**（`context_for`），补不到才退回样本自带的那几条。
      这样"上下文是不是复数条"有个**真能变红的判据**——只改样本那一侧不会红。
    """

    samples = list(reversed(selection.samples))
    if wanted:
        have = {sample.message_id for sample in selection.samples}
        # 被点过、但这一轮没进样本的（例如日志窗口滚动过）：也要能审，
        # 只是没有"学习 agent 据此得出的结论"那一块（`sample=None`）。
        samples.extend(
            QuoteSample(message_id=mid, group_id=group, body="", at=0.0, reactions=(),
                        context_before=(), context_after=())
            for mid in sorted(wanted - have)
        )
    full_bodies = {_clean_id(row.get("message_id")): _line_body(row)
                   for row in rows if _is_her_message(row)}
    sessions: dict[str, set[str]] = {}
    for row in rows:
        if _is_her_message(row):
            sessions.setdefault(_clean_id(row.get("message_id")), set()).add(_session_of(row))
    fallback_session = f"group:{group}" if group else ""
    out: list[EvidenceEntry] = []
    for sample in samples:
        known = sessions.get(sample.message_id) or set()
        # `message_id` 是传输层给的原始 id，**不假设**它跨会话唯一（实测这份日志里
        # 4579 个入站 id 没撞号，但没契约保证）。撞了就以本群/样本自己的会话为准：
        # 只按 id 挑会话会把"另一个会话里同号的那条"的上下文贴到这一条上。
        if known == {fallback_session}:
            session = fallback_session
        elif len(known) == 1:
            session = next(iter(known))
        else:
            session = fallback_session or (sample.group_id and f"group:{sample.group_id}") or ""
        earlier, later = context_for(rows, sample.message_id, before=EVIDENCE_CONTEXT_MAX,
                                     after=EVIDENCE_CONTEXT_MAX, session=session)
        # **只在日志里找不到那条时才退回样本自带的**，不能用 `(later or ...)`：
        # 回日志补出来的结果是空元组**有两种含义**——"找不到那条"（该退）
        # 与"同会话里确实一条都没有"（**不该退**，退了就等于拿别群的话补位）。
        # 样本自带的那一份也已经是按会话过滤过的，但它可能来自另一次扫描窗口。
        out.append(EvidenceEntry(
            message_id=sample.message_id,
            group_id=sample.group_id or group,
            body=full_bodies.get(sample.message_id, "") or sample.body,
            at=sample.at,
            reactions=sample.reactions[:MAX_EVIDENCE_REACTIONS],
            context_before=(earlier if earlier or later else sample.context_before
                            )[:EVIDENCE_CONTEXT_MAX],
            context_after=(later if earlier or later else sample.context_after
                           )[:EVIDENCE_CONTEXT_MAX],
            sample=sample if sample.body else None,
        ))
    return out


def collect_evidence(chat_log_path: Path | str, /, *, reply_log_path: Path | str | None = None,
                     group_id: str, limit: int = DEFAULT_EVIDENCE_LIMIT,
                     offset: int = 0) -> tuple[tuple[EvidenceEntry, ...], int, str]:
    """证据读口的入口：**只读**一份日志，返回 `(这一页, 总数, 出错原因)`。

    * **配对只认 `message_id`**（与 `build_samples` 同一套判据，不按时间邻近猜）；
    * 只给**她说过、而且被贴了表情**的那些（`direction="out"` 且表情 `message_id` 对上）；
    * 上下文按 id 回日志补（她开口之前 / 说完之后各若干条，**复数条**）；
    * **任何读不开都返回空 + 原因**，不抛。

    上下文以**样本自带的**为准（那是学习 agent 判定时真正读过的那几条），
    样本没带才回日志补——两处口径不同就等于"审核看的"与"它学的"不是一回事。
    """

    rows, error = read_rows(chat_log_path)
    if not rows:
        return (), 0, error or "empty"
    targets: dict[str, list[dict]] = {}
    for row in rows:
        if _is_reaction(row):
            target = _clean_id(row.get("message_id"))
            if target:
                targets.setdefault(target, []).append(row)
    group = str(group_id or "")
    wanted = {
        _clean_id(row.get("message_id"))
        for row in rows
        if _is_her_message(row) and _clean_id(row.get("message_id")) in targets
        and (not group or str(row.get("group_id") or "") == group)
    }
    wanted.discard("")
    if not wanted:
        return (), 0, error
    index, _ = read_topic_index(reply_log_path)
    selection = build_samples(rows, topic_index=index)
    entries = _entries_for(rows, selection, group, wanted=wanted)
    page, total = evidence_pages(tuple(entries), offset=offset, limit=limit)
    return page, total, error


def default_paths() -> tuple[Path, Path]:
    """证据读口默认读哪两份日志：`(chat.jsonl, reply.jsonl)`。

    与 `quote_learning._default_chat_log_path` 是同一套口径（`chat_log_directory()`），
    只是这里**两份一起给**（话题要从 `reply.jsonl` 里对）。
    """

    from .chat_log import CHAT_LOG_FILE, chat_log_directory

    chat_log = Path(chat_log_directory()) / CHAT_LOG_FILE
    return chat_log, chat_log.parent / "reply.jsonl"


# --- "刚才有人给她贴了什么"：回复路径上的读口（2026-10-11，用户选了"甲"）---------
#
# 由来：金句学习学出来的**表情含义**（"㊗ 在这个群是嫌她"）当时只出现在面板与
# `/super quote` 上，**进不了她的回复**（`quote_learning.emoji_notes_for` 写好了却没有
# 调用点，表情回应事件本身也不进核心队列）。用户 2026-10-11 拍了"甲"：
#
#     回话前扫一眼对话日志**最近几分钟**的表情回应，只挑**点在她自己消息上**的那些，
#     把对应的材料塞进同一块（纯本地、零模型调用、不动对话判定）。
#
# 三条纪律与这条路上别的东西一致：
#
# 1. **有界**：只读日志**末尾**若干行（`read_tail_rows`）——`chat.jsonl` 实测 3.4 MB /
#    12950 行，整份读+解析是每一轮都白花的钱；
# 2. **她那条消息与表情必须都在尾巴里**才算数（配对只认 `message_id`，与学习那边同一套
#    判据）。配对不上就**当没有**：宁可不提，也不能把"别人被贴的表情"说成是在嫌她；
# 3. **一条只提一次**：同一（哪条消息 + 哪个表情）在她那儿只出现一次，免得同一个
#    ㊗ 在十分钟里每轮都念一遍。
#: "刚才"的窗口（秒）。只取最近这么久的表情回应。
DEFAULT_REACTION_WINDOW = 900.0
#: 只读日志末尾多少行（够把她那条消息与紧随其后的表情一起框进来）。
DEFAULT_TAIL_LINES = 1200
#: 读尾巴时最多读多少字节（防一行特别长的正文把内存吃掉）。
MAX_TAIL_BYTES = 512 * 1024
#: 一次最多给几个表情的材料（材料块总上限是 5 条，别让表情把笔记挤光）。
MAX_REACTION_MATERIALS = 2
#: "已经提过的"最多记多少条（防它无限长大）。
MAX_SHOWN_REACTIONS = 200


def read_tail_rows(path: Path | str, *, max_lines: int = DEFAULT_TAIL_LINES,
                   max_bytes: int = MAX_TAIL_BYTES) -> list[dict]:
    """只读日志**末尾**若干行（从文件尾往前读块，凑够行数或读满字节就停）。

    与 `read_rows`（整份读，给离线学习用）是两回事：这一份在**回复路径**上被调用，
    所以有界。坏行跳过；文件不在 / 读不开就返回空（调用方据此什么都不做）。
    """

    try:
        with open(path, "rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            chunk = b""
            position = size
            while position > 0 and (size - position) < max_bytes \
                    and chunk.count(b"\n") <= max_lines:
                step = min(65536, position, max_bytes - (size - position))
                if step <= 0:
                    break
                position -= step
                handle.seek(position)
                chunk = handle.read(step) + chunk
    except FileNotFoundError:
        return []
    except OSError as exc:
        logger.warning("quote_tail_read_failed category=%s", type(exc).__name__)
        return []

    lines = [line for line in chunk.split(b"\n") if line.strip()]
    if position > 0 and lines:
        # 从文件中间切下去的，第一行多半是半截 → 丢掉（宁可少一行，不要一个假样本）。
        lines = lines[1:]
    rows: list[dict] = []
    for raw in lines[-max_lines:]:
        text = raw.decode("utf-8", "replace").strip()
        try:
            row = json.loads(text)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


class ReactionFeed:
    """回复路径上"**刚才**有人给她贴了什么"的读口。**任何异常都当没有**。"""

    def __init__(self, path: Path | str, *, clock=time.time,
                 window: float = DEFAULT_REACTION_WINDOW,
                 max_lines: int = DEFAULT_TAIL_LINES,
                 max_materials: int = MAX_REACTION_MATERIALS) -> None:
        self.path = Path(path)
        self.clock = clock
        self.window = max(0.0, float(window))
        self.max_lines = max(1, int(max_lines))
        self.max_materials = max(0, int(max_materials))
        #: 已经提过的（`message_id:emoji_id`）→ 第一次提的时刻。有界（见 MAX_SHOWN_REACTIONS）。
        self.shown: dict[str, float] = {}

    def recent_emoji_ids(self, group_id: str, *, session_id: str = "",
                         now: float | None = None) -> tuple[str, ...]:
        """最近几分钟、**点在她自己消息上**、**还没提过**的表情编号（新的在前，最多几个）。

        判据三条，都要满足：同一会话、时间在窗口内、而且被点的那条消息**确实是她说的**
        （她那条也必须在读到的尾巴里，否则宁可当没有）。
        """

        if not self.max_materials:
            return ()
        moment = self.clock() if now is None else float(now)
        wanted = str(session_id or (f"group:{group_id}" if group_id else ""))
        if not wanted:
            return ()
        rows = read_tail_rows(self.path, max_lines=self.max_lines)
        if not rows:
            return ()
        hers = {
            _clean_id(row.get("message_id"))
            for row in rows
            if _is_her_message(row) and _session_of(row) == wanted
        }
        hers.discard("")
        seen: list[str] = []
        keys: list[str] = []
        for row in reversed(rows):
            if not _is_reaction(row) or _session_of(row) != wanted:
                continue
            at = _float(row.get("at"))
            if self.window and at and (moment - at) > self.window:
                continue
            emoji_id = _clean_id(row.get("emoji_id"))
            message_id = _clean_id(row.get("message_id"))
            if not emoji_id or message_id not in hers:
                continue
            key = f"{wanted}:{message_id}:{emoji_id}"
            if key in self.shown or emoji_id in seen:
                continue
            seen.append(emoji_id)
            keys.append(key)
            if len(seen) >= self.max_materials:
                break
        # **返回即记账**：同一个表情在这条消息上只提一次。代价是"这一轮后来没回成"
        # 会白丢一次提示——那比每轮都念一遍同一句 ㊗ 轻得多。
        for key in keys:
            self.shown[key] = moment
        self._trim()
        return tuple(seen)

    def _trim(self) -> None:
        if len(self.shown) <= MAX_SHOWN_REACTIONS:
            return
        for key, _ in sorted(self.shown.items(), key=lambda item: item[1])[
                :len(self.shown) - MAX_SHOWN_REACTIONS]:
            self.shown.pop(key, None)

    def snapshot(self) -> dict[str, object]:
        return {"path": str(self.path), "window": self.window,
                "max_materials": self.max_materials, "shown": len(self.shown)}
