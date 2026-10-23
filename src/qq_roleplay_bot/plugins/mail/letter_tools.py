"""她写信前**自己能翻的那些东西**：每一样都是一条只读的路，查不到就说没查着。

## 由来（2026-10-22 用户原话）

> 我要的是云茹想说什么就说什么，而不是给她一些素材照着写。agent 有一个重要原则，
> 就是想调用工具才调用……工具全部丢给她，后面再修。

在那之前，草稿阶段把 `DayMaterials`（窗口计数 / 他的句子 / 人的档位 / 关系变化 /
上一封）**一次性塞进请求**，她只能照着那份材料写。实测的后果写在 `daily_report.py`
开头：10-19 / 10-20 / 10-21 三封全是"你今晚说……我看见了"，因为手上只有那几句。

现在反过来：**材料不再进 prompt，而是变成这些一条一条可以查的东西**。她想知道什么
就自己看，一眼都不看也能直接写——写出来的具体的事必须是她看过的（核对那一步拿
"她查到的东西"当依据，见 `letter_writer`）。

## 这里的边界（一句话：只读、有界、查不到不挡路）

- **只读**：`memory` 是核心递过来的 `MemoryRead`（只有查询方法，没有 store）；
  `lore` 是核心递过来的一条只读函数。这个模块自己**不写任何东西**，也拿不到任何
  可写的对象。
- **有界**：每种最多给几条（`MAX_RECENT` / `MAX_OLD` / `MAX_LETTERS` …），
  一次查询词最多 `MAX_QUERY_CHARS` 字。
- **查不到不挡路**：`run()` 里的每一路都包着 try/except，任何一步失败只回一句
  "没翻着"，绝不抛给写信那条路（用户 2026-10-22 同一条："查不到就少写，绝不阻塞写信"）。
- **没有素材包**：她没查 `<his>`，就**没有**他的句子可写；核对那一步也会把"你没看过
  的事"删掉。这不是限制她，是"她查到什么就只能写什么"。

## 为什么 `run()` 返回 `ToolAnswer` 而不是 `str`

写信那边要把"她这一趟查到的"按顺序攒成**证据包**（核对阶段的依据），日志里也要能
看出"她查了哪几样、每样查到没有"。只回一个字符串，这三件事就都得调用方再记一遍。
`ToolAnswer.found` 决定它进不进证据包：没查着的那次没有内容可当依据。
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

#: 她这一趟能看的东西（也是 `letter_writer.TOOLS_PROMPT` 里那张目录的口径）。
#: 加一样就要两处一起加：这里与那一段目录（测试钉着两边不漏项）。
TOOL_NAMES = ("time", "lookup", "recent", "old", "person", "his", "letters", "group", "lore", "web")

#: 一次查询词最多留这么长（她偶尔会写一整句话过来）。
MAX_QUERY_CHARS = 80
#: 每样最多给几条。**不是省 token**：一次给一堆，她就会照着念，那又回到"素材"了。
MAX_RECENT = 6
MAX_OLD = 6
MAX_PERSON_ROWS = 4
MAX_HIS = 8
MAX_LETTERS = 4
#: 一封信的第一句最多带这么多字（`<letters>` 只给"她上次写过什么"的轮廓）。
MAX_LETTER_LINE = 60

#: 查不到时回给她的那句话。**不是错误**：她只该知道"这次没翻着"，然后照常写。
NOT_FOUND = "（这一样没翻着。）"

_OPEN = re.compile(r"<\s*([a-z]+)\s*/?\s*>", re.IGNORECASE)
_CLOSE = re.compile(r"</\s*([a-z]+)\s*>", re.IGNORECASE)
#: 信本体（与 `letter_writer.parse_letter` 同一个标签）：出现它就说明这一轮不是"看一样"。
_BODY = re.compile(r"<\s*body\s*>", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class ToolAnswer:
    """她查的一样品：哪一样、问了什么、翻到什么（空 = 没翻着）。"""

    tool: str
    query: str = ""
    text: str = ""

    @property
    def found(self) -> bool:
        return bool(str(self.text or "").strip())

    def label(self) -> str:
        """进 prompt / 证据包的那一行抬头（`<lookup>考试</lookup>` 这种原始写法）。"""

        if self.tool in ("lookup", "person", "lore", "web"):
            return f"<{self.tool}>{self.query}</{self.tool}>"
        return f"<{self.tool}>"


def parse_tool_call(raw: str, *, names: tuple[str, ...] = TOOL_NAMES):
    """她这一轮要看哪一样：`(名字, 查询词)`；不是"看一样"就返回 `None`。

    **只认完整的标签**：`<lore>` 少了后半截（模型偶尔写到一半断了）不算——
    猜她想要什么会把一次"格式没写全"变成一次"查一件她没想查的事"。
    带查询词的三样（`lookup` / `person` / `lore`）必须成对出现；另外几样只认
    `<time/>` 或 `<time>` 这一种开标签（它们没有参数）。

    多个标签同时出现时取**位置最前**的那个：那是她先想看的。
    """

    text = str(raw or "")
    if not text.strip():
        return None
    # 信已经写出来了，就当它没在看：`<subject>/<body>` 一旦出现，这一轮就是交信。
    # 理由与老的 `parse_lookup` 一样——模型偶尔会在信后面顺手带一个"我还想看…"，
    # 那不该让已经写好的信被一个"再看一眼"覆盖掉。
    if _BODY.search(text):
        return None
    best: tuple[int, str, str] | None = None
    for name in names:
        lowered = name.casefold()
        if name in ("lookup", "person", "lore", "web"):
            pattern = re.compile(rf"<\s*{re.escape(name)}\s*>(.*?)</\s*{re.escape(name)}\s*>",
                                 re.IGNORECASE | re.DOTALL)
            found = pattern.search(text)
            if not found:
                continue
            query = " ".join(found.group(1).split())[:MAX_QUERY_CHARS]
            start = found.start()
        else:
            found = None
            for match in _OPEN.finditer(text):
                if match.group(1).casefold() == lowered:
                    found = match
                    break
            if found is None:
                continue
            # 这几个没有参数：`<time>2026…</time>` 这种不算（她可能在想写时间）。
            after = text[found.end():]
            close = _CLOSE.search(after)
            if close and close.group(1).casefold() == lowered:
                continue
            query = ""
            start = found.start()
        if best is None or start < best[0]:
            best = (start, name, query)
    if best is None:
        return None
    return best[1], best[2]


def _first_sentence(text: object, limit: int = MAX_LETTER_LINE) -> str:
    """一段正文的第一句（`<letters>` 只要轮廓，不给她整封旧信）。"""

    body = " ".join(str(text or "").split())
    if not body:
        return ""
    for mark in ("。", "！", "？", "；", "\n"):
        head = body.split(mark, 1)[0]
        if head.strip():
            body = head.strip()
            break
    return body[:limit]


class LetterToolbox:
    """这一封信的数据源。**只读、有界**；任何一路失败都只回一句"没翻着"。"""

    def __init__(self, *, materials=None, memory=None, letters=(), lore=None,
                 now: float = 0.0, clock=time.time,
                 closeness_names=None, guardedness_names=None,
                 lookup=None, web_search=None) -> None:
        #: `daily_report.DayMaterials`（窗口热度、他的句子、人的档位）。
        self.materials = materials
        #: 核心的只读记忆口（`MemoryRead`）：只有 recall / recent / older / people。
        self.memory = memory
        #: 她最近写过的那几封信（`mail_state` 里那些 dict，旧 → 新）。
        self.letters = tuple(letters or ())
        #: 核心的"她世界里那些设定"只读口：`(query) -> str`，查不到回空串。
        self.lore = lore
        self.now = float(now or 0.0)
        self.clock = clock
        self.closeness_names = closeness_names
        self.guardedness_names = guardedness_names
        #: `<lookup>` 的替身（测试与老调用点用；生产走 `memory.recall`）。
        self.lookup = lookup
        #: `<web>` 的替身：`(query) -> str`（生产走 `plugins/web_search/search.py`）。
        #: 传 None 时 `_web` 会懒加载那一个模块；测试可以塞一个假的，不联网。
        self.web_search = web_search

    # --- 跑一样 -------------------------------------------------------------

    def run(self, tool: str, query: str = "") -> ToolAnswer:
        """跑她点的那一样。**绝不抛**：失败就是没翻着，她照常写。"""

        name = str(tool or "").strip().casefold()
        arg = " ".join(str(query or "").split())[:MAX_QUERY_CHARS]
        if name not in TOOL_NAMES:
            return ToolAnswer(tool=name or "?", query=arg)
        try:
            text = getattr(self, f"_{name}")(arg)
        except Exception:  # noqa: BLE001 - 查不到不挡写信（用户 2026-10-22）
            logger.warning("letter_tool_failed tool=%s", name, exc_info=True)
            text = ""
        return ToolAnswer(tool=name, query=arg, text=str(text or "").strip())

    # --- 每一样 -------------------------------------------------------------

    def _time(self, _arg: str) -> str:
        """现在几点 / 今天几号 / 距上一封信多久。"""

        stamp = time.strftime("%Y-%m-%d %H:%M",
                              time.localtime(self.now or self.clock()))
        parts = [f"现在是你那边的 {stamp}。"]
        note = str(getattr(self.materials, "last_letter_note", "") or "").strip()
        if note:
            parts.append(note)
        return "\n".join(parts)

    def _lookup(self, query: str) -> str:
        """翻一件旧事（一个名字、一个号码、一个词都行）。"""

        if not query:
            return ""
        taker = self.lookup if callable(self.lookup) else getattr(self.memory, "recall", None)
        if not callable(taker):
            return ""
        text = str(taker(query) or "").strip()
        # 核心那句"没找到"也是它自己的一句话；原样给她，别再加一层引号。
        return text

    def _recent(self, _arg: str) -> str:
        """最近记着的几件事。"""

        taker = getattr(self.memory, "recent", None)
        if not callable(taker):
            return ""
        return str(taker(limit=MAX_RECENT) or "").strip()

    def _old(self, _arg: str) -> str:
        """更早的旧事（让信里有时间纵深）。"""

        taker = getattr(self.memory, "older", None)
        if not callable(taker):
            return ""
        return str(taker(limit=MAX_OLD) or "").strip()

    def _person(self, query: str) -> str:
        """对某人的印象：关系档位 + 她记着的碎片。**号码优先，称呼退化成搜一遍。**"""

        if not query:
            return ""
        lines: list[str] = []
        rows: list[dict] = []
        raw_people = getattr(self.materials, "people", ()) or ()
        for item in raw_people:
            try:
                qq, closeness, guardedness = item
            except (TypeError, ValueError):
                continue
            if str(qq) == query:
                rows.append({"user_id": str(qq), "closeness": int(closeness),
                             "guardedness": int(guardedness)})
                break
        if not rows and callable(getattr(self.memory, "people", None)):
            try:
                for item in self.memory.people(0.0, limit=100):
                    if str(item.get("user_id")) == query:
                        rows.append(item)
                        break
            except Exception:  # noqa: BLE001 - 只读口炸了就当没这个人的档
                logger.warning("letter_tool_person_people_failed", exc_info=True)
        if rows:
            row = rows[0]
            lines.append(
                f"{row['user_id']}：亲近 {self._tier(self.closeness_names, row['closeness'])}，"
                f"防备 {self._tier(self.guardedness_names, row['guardedness'])}。"
            )
        taker = self.lookup if callable(self.lookup) else getattr(self.memory, "recall", None)
        if callable(taker):
            try:
                text = str(taker(query) or "").strip()
            except Exception:  # noqa: BLE001 - 搜不到就是没有
                text = ""
            if text:
                lines.append(text)
        # 关系变动里提到过这个人的那几句（只有"为什么"那一侧，没有正文）。
        changes = getattr(self.materials, "relationship_changes", ()) or ()
        if changes and not rows:
            lines.append("；".join(str(item).rstrip("。；; ") for item in tuple(changes)[:3]) + "。")
        return "\n".join(line for line in lines if line.strip())

    @staticmethod
    def _tier(names, value) -> str:
        """档名（只要 `——` 前面那一段）。`names` 是 `likes_text` / `guard_text`。"""

        if callable(names):
            return str(names(int(value))).split("——")[0]
        return str(value)

    def _his(self, _arg: str) -> str:
        """他最近说过的几句（带时间/会话/引用链，见 `DayMaterials`）。"""

        materials = self.materials
        lines = tuple(getattr(materials, "his_lines", ()) or ())[:MAX_HIS]
        if not lines:
            return ""
        rendered = []
        try:
            rendered = list(materials._his_lines_rendered())  # noqa: SLF001 - 同一个插件包
        except Exception:  # noqa: BLE001 - 结构渲染不出来就退化成裸句
            rendered = [f"· {line}" for line in lines]
        return "他这段时间说过的话（从早到晚）：\n" + "\n".join(rendered[:MAX_HIS])

    def _letters(self, _arg: str) -> str:
        """她上几封信写过什么（主题 + 开头一句，**不搬整封旧信**）。"""

        rows = [item for item in self.letters if isinstance(item, dict)]
        if not rows:
            return ""
        lines: list[str] = []
        for item in rows[-MAX_LETTERS:]:
            subject = str(item.get("subject") or "").strip()
            head = _first_sentence(item.get("body"))
            try:
                at = float(item.get("at") or 0.0)
            except (TypeError, ValueError):
                at = 0.0
            day = time.strftime("%m-%d", time.localtime(at)) if at > 0 else ""
            pieces = [piece for piece in (day, f"「{subject}」" if subject else "", head) if piece]
            if pieces:
                lines.append("· " + " ".join(pieces))
        if not lines:
            return ""
        return "你上几封信写过：\n" + "\n".join(lines)

    def _group(self, _arg: str) -> str:
        """群里最近什么样：**热度与谁在说话，不给原文、不给条数**。"""

        materials = self.materials
        if materials is None:
            return ""
        lines: list[str] = []
        if bool(getattr(materials, "counts_reliable", False)):
            buzzer = getattr(materials, "buzz", None)
            if callable(buzzer):
                lines.append(str(buzzer()))
        else:
            # 数不出来就说数不出来（2026-10-11 那一条：不许拿 0 当事实）。
            lines.append("这段时间群里什么样，你说不太准。")
        people = tuple(getattr(materials, "people", ()) or ())
        if people:
            ids = "、".join(str(item[0]) for item in people[:10]
                            if isinstance(item, (tuple, list)) and item)
            if ids:
                lines.append(f"这段时间跟你说过话的人：{ids}。")
        if int(getattr(materials, "addressed", 0) or 0) > 0:
            lines.append("有人专门叫过你。")
        if int(getattr(materials, "media", 0) or 0) > 0:
            lines.append("有人发过图或者表情。")
        return "\n".join(line for line in lines if line.strip())

    def _lore(self, query: str) -> str:
        """她世界里的设定/知识条目：**这条最要紧**（能写"她世界里的事"，不只是聊天记录）。"""

        if not query or not callable(self.lore):
            return ""
        return str(self.lore(query) or "").strip()

    def _web(self, query: str) -> str:
        """上网搜一下：只回标题 / 链接 / 摘要（最多 3 条）。

        **不是**"她身边发生的事"：结果来自公开网页，只当参考。查不到、断网、
        解析失败一律回空串（`run()` 会把它记成"没翻着"），绝不挡写信。
        """

        if not query:
            return ""
        taker = self.web_search
        if not callable(taker):
            try:
                from ..web_search.search import search as taker  # noqa: PLC0415 - 懒加载
            except Exception:  # noqa: BLE001 - 没装这个插件就当没有联网搜索
                logger.warning("letter_tool_web_missing", exc_info=True)
                return ""
        try:
            return str(taker(query) or "").strip()
        except Exception:  # noqa: BLE001 - 搜不到就是没有
            logger.warning("letter_tool_web_failed", exc_info=True)
            return ""
