"""黑话词条：**被动听来的解释**，落成一条条可查、可改、可删的词条。

由来（2026-10-06 用户口径）：*"金句我看应该划到**知识库**里"*、*"还有**黑话**"*。
设计稿在 `docs/STAGE3_PENDING_DESIGNS.md` §②（"问 → 解释 → 词条"）。

## 这一版只做"听"，**不做"问"**（用户已经否掉那一版）

`docs/PENDING.md`（2026-10-05 晚）：*"别做不懂就问"* · *"先改掉不懂装懂硬插话"*。
所以入口只有一个：**她在群里听到别人解释，就记下来**。`SlangWatcher` 是个**纯被动**的
观察者——收一条消息、确定性匹配、写一条词条，然后什么都不做。

它**没有** transport、没有 notify、没有模型 client，也没有 `async def`：
"她因此问一句"这条路上**根本没有可调用的东西**（`tests/test_slang_learning.py`
里那条 `test_the_watcher_has_nothing_to_speak_with` 就是钉这个的）。

## 判解释句：为什么用**确定性匹配**，不借判定那趟的信号

任务书给了三选一（确定性 / 判定给信号 / 两者结合），这里选**确定性**，理由三条：

1. **判定那趟解决的是另一个问题**。"要不要接这句话"只在她在场、要被叫到时才有意义；
   而"群里有人在解释黑话"是**与她无关就发生的事实**——她没被判定到的那条解释照样该记。
   挂在判定上，等于"她没说话就不学"，与"被动捕获"的目标刚好相反。
2. **零成本、零热路径风险**。判定那趟的输出形状（`stance` / `route` / `topic`）是给
   回复用的；把数据层的采集绑在它上面，等于以后改判定 prompt 会顺手改掉采集行为。
   一条正则扫过去：不花钱、不 await、不进对话路径。
3. **可审计**。漏了什么、误报了什么，看 `data/logs/slang.jsonl` 里那条 `shape`
   与正则本身就知道——不需要"再跑一次模型"来解释上一次模型为什么那么判。

代价是**会有漏**，也会有**误报**，所以护栏（`_STOP_CHARS` / `_STOP_WORDS` /
`_is_question`）一律**宁严勿松**：漏一条的代价是"少学一个词"，
误报的代价是"词库里多一条错词条，而她以后会照着它理解别人的话"。
本模块的 docstring 末尾如实列了这次实测到的漏与误报。

## 一条词条长什么样（落 `data/`，已 gitignored）

| 字段 | 什么 | 谁写的 |
| --- | --- | --- |
| `group_id` / `word` | 哪个群、哪个词（**一起**才是键） | 观察者 |
| `definition` | 释义——**群里那个人原话里的那一段**，不是模型编的 | 观察者 / 操作者改 |
| `times` | 被解释过几次（含改口那次） | 观察者 |
| `first_seen` / `last_seen` | 第一次 / 最近一次听到 | 观察者 |
| `explainers[]` | **谁解释的**（`user_id` / 名字 / 时间 / 那次给的释义 / **原句**） | 观察者 |
| `revisions[]` | 改口记录（`from` → `to`，谁、什么时候、当时的原句） | 观察者 / 操作者 |
| `status` | `ok` / `wrong`（"标错"由操作者按） | 操作者 |

**存原句证据**（设计稿 §②"只给证据，不给定义"）：`explainer.quote` 是那条消息的
**原话**（截断到 `MAX_EVIDENCE_CHARS`）。`definition` 只是从原话里切出来的那一段，
所以"释义错了"能一直回溯到"谁在哪一句里这么说的"。

## 合并与改口的规则（都是确定性的，写在 `apply_explanation` 里）

- **同一个词 + 同一个群**才算同一条词条；换个群就是另一条（黑话是**按群**的）。
- 释义（归一化后）**一样** → 只 `times += 1`、更新 `last_seen`、把这次的说话人记进
  `explainers`（同一个人的同一条说法不重复记）。**不新增修订**。
- 释义**不一样** → 旧的那条进 `revisions`（`from`/`to`/谁/何时/原句），
  `definition` 换成新的，`status` 回到 `ok`（新解释推翻"标错"）。
- `status == "wrong"` 时**重复同一套说法不会把它洗白**（同一句错话重复三遍还是错话）；
  只有**新释义**或操作者显式 `ok` 才清掉。

## 护栏（代码里守，不靠自觉）

| 要求 | 怎么守 |
| --- | --- |
| 机制词零命中 | `word` / `definition` 过 `prompt_guard.scan_persona_text`，命中**整条丢掉**；原句命中就**不存原句**（`evidence_dropped` 计数），释义与"谁说的"照旧留下 |
| 落盘坏掉不影响对话 | `save()` 只记 `last_error` 不抛；`observe()` 整个兜住，失败只 `failures += 1` |
| 不覆盖人格 | 这里只写 `data/` 下的一份 JSON，**一个字都不进 system 前缀**；检索/注入这一轮**不做** |
| 不重复记同一条消息 | 消息 id 进一个有界 `deque`（焦点排队会把同一条再交一次手） |
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import time
from collections import deque
from pathlib import Path

from . import dev_config, prompt_guard
from .feature_log import RollingJsonlFile, logs_directory

logger = logging.getLogger(__name__)

#: 词最长多少字。黑话是"一个词"，不是一句话；超过这个长度的八成是子句（护栏的判据之一）。
MAX_WORD_CHARS = 12
#: 释义最长多少字（用户口径里的"释义"是别人的一句话，不是一篇解释）。
MAX_DEFINITION_CHARS = 40
#: 原句证据最多留多少字（它只给人看，不进任何 prompt）。
MAX_EVIDENCE_CHARS = 200
#: 最多留多少条词条（防它无限长大；满了就不再收新的，**不静默删老的**）。
MAX_ENTRIES = 500
#: 一条词条最多留几个"谁解释的"。
MAX_EXPLAINERS = 8
#: 一条词条最多留几次改口。
MAX_REVISIONS = 20
#: 一条消息里最多收几条解释（一条消息列三个词条已经很多了）。
MAX_PER_MESSAGE = 3
#: 记过的消息 id 最多留多少（只用于"同一条消息不重复记"）。
MAX_SEEN_MESSAGES = 2000
#: 捕获日志留多少条（与别的日志同一套滚动实现）。
DEFAULT_LOG_CAPACITY = 200

ENTRY_OK = "ok"
ENTRY_WRONG = "wrong"

_WORD_CHARS = r"[\w\u4e00-\u9fff]"
#: 词：2–12 个字，而且**左边是边界**（句首 / 空白 / 标点）。
#:
#: 为什么非要这条 lookbehind：中文没有分词，没有它时 `[\w]{2,12}` 会把"就是"前面
#: **一整段话**吃进来——"但是这个字幕好像只能识别就是……"会得到一个 11 字的"词"。
#: 有了它，词只能从句首或标点/空白之后开始；再配 `_STOP_CHARS` 把"但是/这个"挡掉。
_TERM = r"(?<![\w\u4e00-\u9fff])(?P<word>" + _WORD_CHARS + _WORD_CHARS + r"{1,11})"
#: 粘在中文里的那几种句式（"我们管 X 叫 Y"）用这一条：词的左右由**字面词**夹住，
#: 所以不需要边界（"管电赛叫"中间没有空格时上面那条会失配）。
_TERM_GLUED = r"(?P<word>" + _WORD_CHARS + _WORD_CHARS + r"{1,11})"
#: 释义：到句子标点为止的一小段（不含空白与断句标点）。
_DEF = r"(?P<definition>[^\s，。！？；：、,.!?;:]{1," + str(MAX_DEFINITION_CHARS) + r"})"
#: 同上，但**非贪婪**——后面还要跟"的意思"这种尾巴时用它。
_DEF_LAZY = r"(?P<definition>[^\s，。！？；：、,.!?;:]{1," + str(MAX_DEFINITION_CHARS) + r"}?)"
_LOCATOR = r"在?(?:我们|咱们|群里|这边|这儿|这里|本地)(?:这儿|这边|这里)?"

#: 解释句的**确定性形状**，**从具体到宽泛**（顺序就是判据的一部分）。
#:
#: 为什么顺序要紧：`X就是Y的意思` 如果先试最宽的那条（`X就是Y`），会得到
#: `definition="Y 的意思"`——多一个尾巴，人看着别扭；反过来先试窄的，
#: 宽的那条只兜"没有专门句式"的解释。**同一个词只取第一条命中的**（见 `detect_explanations`）。
_EXPLANATION_SHAPES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("A就是B的意思", re.compile(_TERM + r"\s*就是\s*" + _DEF_LAZY + r"\s*的意思")),
    ("A是B的意思", re.compile(_TERM + r"\s*是\s*" + _DEF_LAZY + r"\s*的意思")),
    ("A是B的简称", re.compile(_TERM + r"\s*是\s*" + _DEF_LAZY
                              + r"\s*的(?:简称|缩写|别称|俗称|谐音|全称|全名)")),
    ("A的简称是B", re.compile(_TERM + r"\s*的(?:全称|简称|缩写|别称|俗称|谐音|全名)(?:就)?是\s*" + _DEF)),
    ("A的意思是B", re.compile(_TERM + r"\s*的?意思(?:就)?是\s*" + _DEF)),
    ("A指的是B", re.compile(_TERM + r"\s*(?:指的就是|指的是|指)\s*" + _DEF)),
    ("A也就是B", re.compile(_TERM + r"\s*(?:也就是|也叫做|又叫|又称)\s*" + _DEF)),
    ("我们管A叫B", re.compile(r"(?:我们|咱们|这边|群里|这儿|这里|本地)(?:这儿|这边|这里)?\s*"
                              r"(?:管|把)\s*" + _TERM_GLUED + r"\s*(?:叫|称作|称为|说成)\s*" + _DEF)),
    ("我们这儿A叫B", re.compile(_LOCATOR + r"\s*(?:管|把)?\s*" + _TERM_GLUED
                                + r"\s*(?:叫|称作|称为|就是|是指)\s*" + _DEF)),
    ("A就是B", re.compile(_TERM + r"\s*就是\s*" + _DEF)),
)

#: 词里**不许出现**的字。中文没分词，这是第二道，专挡那种"整段子句当了词"的误报：
#: `但是这个字幕好像只能识别` 里有 但/是/这/个，一律不算词。
#: 真实黑话（电赛 / 综测 / 尊嘟假嘟 / yyds / 绝绝子）没有一个带这些字。
_STOP_CHARS = frozenset(
    "的了着过吗呢吧啊哦呀嗯是在有不没也都还我你他她它们那什么怎因所以但如若虽然"
    "和跟把被让给对从到就会能可要想说做去来这那个"
)
#: 整词就是这些的，一律不算词条（它们是句子零件，不是黑话）。
_STOP_WORDS = frozenset({
    "这个", "那个", "这些", "那些", "这里", "那里", "什么", "怎么", "为什么", "意思",
    "东西", "事情", "问题", "时候", "地方", "我们", "你们", "他们", "大家", "这样",
    "那样", "真的", "假的", "不是", "可以", "应该", "可能", "因为", "所以", "但是",
    "如果", "然后", "一样", "有点", "一点", "反正", "其实",
})
#: 释义末尾要剥掉的口吻尾巴（"就是聪明的人吧" → "聪明的人"）。
_TAIL_CHARS = "。.，,；;、 \t的了吧啊呢呀哈~～"
#: 释义两侧成套的引号/括号（"就是「你好」的意思" → `你好`）。
_WRAPPERS = (("「", "」"), ("『", "』"), ("“", "”"), ("‘", "’"),
             ("《", "》"), ("〈", "〉"), ("（", "）"), ("(", ")"),
             ("[", "]"), ("【", "】"), ('"', '"'), ("'", "'"))


def _text(value: object, limit: int = 0) -> str:
    body = " ".join(str(value if value is not None else "").split())
    if limit and len(body) > limit:
        return body[:limit]
    return body


def _norm(value: object) -> str:
    """比较用的归一化：只去空白与标点，不改变字（中文没有词形变化）。"""

    return "".join(ch for ch in _text(value) if ch.isalnum())


def _is_question(text: str) -> bool:
    """这一句像不像在**问**，而不是在**解释**。

    解释句是陈述句。问号、句末的"吗"、"是不是"这类**不确定**的形状一律跳过——
    "X 就是 Y 吗？"问的是"是不是这样"，把它当解释记下来，等于把她自己没把握的东西
    当成群的共识。宁可漏，也不要记错（用户口径：材料不许覆盖事实）。
    """

    body = _text(text)
    if "？" in body or "?" in body:
        return True
    if body.endswith(("吗", "么")):
        return True
    return "是不是" in body


def _clean_word(value: object) -> str:
    """一个候选词能不能当词条：长度、护栏、机制词三道。不合格返回空串。"""

    word = _text(value)
    if not word or len(word) > MAX_WORD_CHARS or len(word) < 2:
        return ""
    if any(ch in _STOP_CHARS for ch in word):
        return ""
    if word in _STOP_WORDS:
        return ""
    if prompt_guard.scan_persona_text(word):
        return ""
    return word


def _strip_wrappers(value: str) -> str:
    """剥掉释义两侧成套的引号/括号（成对才剥，``"你好`` 这种半拉的不动）。"""

    body = value
    for _ in range(3):
        for left, right in _WRAPPERS:
            if len(body) > len(left) + len(right) and body.startswith(left) \
                    and body.endswith(right):
                body = body[len(left):-len(right)].strip()
    return body


def _clean_definition(value: object, *, word: str = "") -> str:
    """释义的准入：非空、不超长、不与自己相同、不含机制词（命中就整条丢掉）。"""

    definition = _text(value).strip(_TAIL_CHARS).strip()
    definition = _strip_wrappers(definition).strip(_TAIL_CHARS).strip()
    if not definition or len(definition) > MAX_DEFINITION_CHARS:
        return ""
    if word and _norm(definition) == _norm(word):
        return ""
    if prompt_guard.scan_persona_text(definition):
        return ""
    return definition


def _clean_evidence(value: object) -> tuple[str, bool]:
    """原句证据：截断后过机制词扫描。

    命中时**只丢原句、不丢词条**（`(空串, True)`）：原句是群里人的话，
    我们没权力替他们改；但这份 JSON 以后可能被注入，所以文件里不许留机制词。
    释义与"谁说的、什么时候"照旧留下——审计链只少一环，不是断掉。
    """

    quote = _text(value, MAX_EVIDENCE_CHARS)
    if not quote:
        return "", False
    if prompt_guard.scan_persona_text(quote):
        return "", True
    return quote, False


def _clause_at(text: str, start: int, end: int) -> str:
    """取命中位置所在的**那一小句**（按断句标点切），给人看时比整段消息干净。"""

    left = max((text.rfind(ch, 0, start) for ch in "，。！？；：、,.!?;:"), default=-1)
    right = min((pos for pos in (text.find(ch, end) for ch in "，。！？；：、,.!?;:")
                 if pos != -1), default=-1)
    return _text(text[left + 1:right if right != -1 else len(text)], MAX_EVIDENCE_CHARS)


def detect_explanations(text: object) -> list[dict[str, str]]:
    """从一条消息里挑出"在解释一个词"的那些句子 → `[{word, definition, shape, sentence}]`。

    **确定性、纯函数、零调用**：同一个输入永远给同一个输出（测试直接钉住它）。
    顺序：形状从具体到宽泛逐个 `finditer`，**同一个词只留第一条命中的那条**
    （`A就是B的意思` 赢了之后，宽泛的 `A就是B` 不再覆盖它）。
    """

    body = _text(text)
    if not body or _is_question(body):
        return []
    found: dict[str, dict[str, str]] = {}
    for shape, pattern in _EXPLANATION_SHAPES:
        for match in pattern.finditer(body):
            word = _clean_word(match.group("word"))
            if not word or word in found:
                continue
            definition = _clean_definition(match.group("definition"), word=word)
            if not definition:
                continue
            found[word] = {
                "word": word,
                "definition": definition,
                "shape": shape,
                "sentence": _clause_at(body, match.start(), match.end()),
            }
            if len(found) >= MAX_PER_MESSAGE:
                return list(found.values())
    return list(found.values())


def profile_path() -> Path:
    """那份 JSON 的位置。默认 `<项目根>/data/slang/entries.json`（`data/` 已忽略）。"""

    value = os.environ.get("QQBOT_SLANG_PROFILE", "").strip() or dev_config.SLANG_PROFILE_FILE
    path = Path(value)
    if not path.is_absolute():
        path = Path(__file__).resolve().parents[2] / path
    return path


def slang_log_path() -> Path:
    """捕获日志。默认与其它日志并列在 `data/logs/`（同一套目录口径）。"""

    return logs_directory() / "slang.jsonl"


def learning_enabled() -> bool:
    """黑话捕获开不开。**默认开**；`QQBOT_SLANG_LEARN=0` 关掉即什么都不记。"""

    value = os.environ.get("QQBOT_SLANG_LEARN", "").strip()
    if not value:
        return bool(dev_config.SLANG_LEARN_ENABLED)
    return value.lower() not in {"0", "false", "no", "off"}


def entry_id(group_id: object, word: object) -> str:
    """给面板/命令用的稳定 id：`群号:词`。它只是**展示用**的拼法，不改语义。"""

    return f"{_text(group_id)}:{_text(word)}"


class SlangStore:
    """`data/slang/entries.json` 的读写：词条、证据、修订。

    与 `quote_learning.QuoteStore` 同一套做法：读按 **mtime** 缓存（操作者手改文件、
    命令改一个词，下一轮就读到），写**原子替换**（临时文件 + `os.replace`）。
    文件坏了整份当空——**一份写坏的 JSON 不该让她不说话**（她本来也不需要它才能说话）。
    """

    VERSION = 1

    def __init__(self, path: Path | str | None = None, *, clock=time.time) -> None:
        self.path = Path(path) if path is not None else profile_path()
        self.clock = clock
        self._entries: dict[tuple[str, str], dict[str, object]] = {}
        self.updated_at = 0.0
        self.last_error = ""
        #: 准入时被丢掉几条（护栏命中的计数；给人看"漏了多少"，不静默）。
        self.dropped = 0
        #: 原句因机制词没存下来的次数。
        self.evidence_dropped = 0
        self._mtime = 0.0
        self.reload()

    # --- 读 ---------------------------------------------------------------

    def reload(self, *, force: bool = False) -> None:
        """按 mtime 决定要不要重新读盘（命令与面板每次调用都会走到这里）。"""

        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            mtime = 0.0
        if not force and mtime == self._mtime:
            return
        self._mtime = mtime
        self._entries = {}
        self.updated_at = 0.0
        self.last_error = ""
        if not mtime:
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            self.last_error = type(exc).__name__
            logger.warning("slang_profile_unreadable category=%s", type(exc).__name__)
            return
        if not isinstance(data, dict) or data.get("version") != self.VERSION:
            self.last_error = "unsupported_version"
            logger.warning("slang_profile_unsupported_version；整份忽略")
            return
        self.updated_at = float(data.get("updated_at") or 0.0)
        for item in data.get("entries") or []:
            if not isinstance(item, dict):
                continue
            group = _text(item.get("group_id"))
            word = _text(item.get("word"))
            if not group or not word:
                continue
            self._entries[(group, word)] = {
                "group_id": group,
                "word": word,
                "definition": _text(item.get("definition")),
                "status": ENTRY_WRONG if item.get("status") == ENTRY_WRONG else ENTRY_OK,
                "times": max(0, int(item.get("times") or 0)),
                "first_seen": float(item.get("first_seen") or 0.0),
                "last_seen": float(item.get("last_seen") or 0.0),
                "explainers": [dict(row) for row in (item.get("explainers") or [])
                               if isinstance(row, dict)][-MAX_EXPLAINERS:],
                "revisions": [dict(row) for row in (item.get("revisions") or [])
                              if isinstance(row, dict)][-MAX_REVISIONS:],
            }

    def find(self, group_id: object, word: object) -> dict[str, object] | None:
        """一条词条的**快照**（改了它不影响库里那份）。没有就是 None。"""

        entry = self._entries.get((_text(group_id), _text(word)))
        return _copy_entry(entry) if entry is not None else None

    def entries_for(self, group_id: object | None = None) -> list[dict[str, object]]:
        """词条列表（新的在前）。`group_id=None` = 所有群。"""

        group = _text(group_id)
        rows = [entry for key, entry in self._entries.items() if not group or key[0] == group]
        rows.sort(key=lambda item: (-float(item.get("last_seen") or 0.0),
                                   str(item.get("group_id")), str(item.get("word"))))
        return [_copy_entry(entry) for entry in rows]

    def groups(self) -> list[str]:
        return sorted({key[0] for key in self._entries})

    def snapshot(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "entries": len(self._entries),
            "groups": self.groups(),
            "wrong": sum(1 for entry in self._entries.values()
                         if entry.get("status") == ENTRY_WRONG),
            "dropped": self.dropped,
            "evidence_dropped": self.evidence_dropped,
            "updated_at": self.updated_at,
            "last_error": self.last_error,
        }

    # --- 写 ---------------------------------------------------------------

    def apply_explanation(self, group_id: object, word: object, definition: object, *,
                          by_user_id: object = "", by_name: object = "", quote: object = "",
                          at: float | None = None, message_id: object = "",
                          source: str = "heard") -> str:
        """把"某人在某个群里解释了一个词"并进库里。

        返回 `"new"`（新建）/ `"merged"`（同一个说法又听了一遍）/ `"revised"`（改口）
        / `""`（护栏挡掉了，什么都没写）。**写盘失败也不抛**（返回的是"并进去了什么"，
        盘上有没有那份数据看 `last_error`）。

        `source` 区分**听来的**（`heard`）与**操作者改的**（`edited`）——两条都会记进
        `explainers`，因为"这个释义是谁给的"这件事两种来源都要能回溯；界面上分开显示。
        """

        clean_word = _clean_word(word)
        clean_definition = _clean_definition(definition, word=clean_word)
        if not clean_word or not clean_definition:
            self.dropped += 1
            return ""
        group = _text(group_id)
        if not group:
            self.dropped += 1
            return ""
        key = (group, clean_word)
        entry = self._entries.get(key)
        if entry is None:
            if len(self._entries) >= MAX_ENTRIES:
                self.dropped += 1
                logger.warning("slang_store_full entries=%s；不再收新的", len(self._entries))
                return ""
            entry = {
                "group_id": group, "word": clean_word, "definition": clean_definition,
                "status": ENTRY_OK, "times": 0, "first_seen": 0.0, "last_seen": 0.0,
                "explainers": [], "revisions": [],
            }
            self._entries[key] = entry
            action = "new"
        elif _norm(entry.get("definition")) == _norm(clean_definition):
            action = "merged"
        else:
            action = "revised"

        stamp = float(at if at is not None else self.clock())
        reason = ""
        evidence, evidence_dropped = _clean_evidence(quote)
        if evidence_dropped:
            reason = "evidence-dropped"
        if action == "revised":
            revisions = list(entry.get("revisions") or [])
            revisions.append({
                "at": stamp, "by": _text(by_user_id) or "操作者",
                "from": _text(entry.get("definition")), "to": clean_definition,
                "quote": evidence,
            })
            entry["revisions"] = revisions[-MAX_REVISIONS:]
            entry["definition"] = clean_definition
            # 新解释推翻"标错"：那一条已经被人更正了（同一句重复不算，见模块说明）。
            entry["status"] = ENTRY_OK
        if not entry.get("first_seen"):
            entry["first_seen"] = stamp
        entry["last_seen"] = stamp
        entry["times"] = int(entry.get("times") or 0) + 1
        self._remember_explainer(entry, by_user_id=by_user_id, by_name=by_name,
                                 definition=clean_definition, quote=evidence, at=stamp,
                                 message_id=message_id, evidence_dropped=evidence_dropped,
                                 source=source)
        self.updated_at = stamp
        saved = self.save()
        if not saved:
            reason = reason or "write-failed"
        if reason:
            logger.info("slang_capture_note action=%s reason=%s word=%s", action, reason,
                        clean_word)
        return action

    def update_definition(self, group_id: object, word: object, definition: object, *,
                          by: object = "操作者", at: float | None = None,
                          quote: object = "") -> bool:
        """操作者/面板改一个词的释义（**记一次修订**）。改不动返回 False。

        词条不存在时走**同一条路**（`apply_explanation` 会新建一条）——操作者
        手工加一个词与"改一个词"是同一个写口，少一条分叉。
        """

        action = self.apply_explanation(group_id, word, definition,
                                        by_user_id=_text(by), at=at, quote=quote,
                                        source="edited")
        return action != ""

    def mark_wrong(self, group_id: object, word: object, wrong: bool = True) -> bool:
        """标错 / 取消标错。**只改一个字段**，释义与证据一个字都不动。"""

        entry = self._entries.get((_text(group_id), _text(word)))
        if entry is None:
            return False
        entry["status"] = ENTRY_WRONG if wrong else ENTRY_OK
        self.updated_at = self.clock()
        return self.save()

    def delete(self, group_id: object, word: object) -> bool:
        """删一条词条。删不掉（本来就没有 / 写盘失败）返回 False。"""

        if self._entries.pop((_text(group_id), _text(word)), None) is None:
            return False
        self.updated_at = self.clock()
        return self.save()

    def _remember_explainer(self, entry: dict, *, by_user_id: object, by_name: object,
                            definition: str, quote: str, at: float, message_id: object,
                            evidence_dropped: bool, source: str) -> None:
        """把"谁解释的"记一笔。同一个人的**同一条说法**只记一次（重复记没信息量）。"""

        rows = list(entry.get("explainers") or [])
        user_id = _text(by_user_id)
        for row in rows:
            if (_text(row.get("user_id")) == user_id
                    and _norm(row.get("definition")) == _norm(definition)):
                row["at"] = at
                row["count"] = int(row.get("count") or 1) + 1
                if quote:
                    row["quote"] = quote
                if message_id:
                    row["message_id"] = _text(message_id)
                entry["explainers"] = rows[-MAX_EXPLAINERS:]
                return
        rows.append({
            "user_id": user_id, "name": _text(by_name), "at": at, "definition": definition,
            "quote": quote, "message_id": _text(message_id), "count": 1,
            "source": "edited" if source == "edited" else "heard",
            "evidence_dropped": 1 if evidence_dropped else 0,
        })
        entry["explainers"] = rows[-MAX_EXPLAINERS:]
        if evidence_dropped:
            self.evidence_dropped += 1

    def save(self) -> bool:
        """整份原子重写。**失败只记 `last_error`**（她把话说完比这份文件重要）。"""

        body = {
            "version": self.VERSION,
            "updated_at": round(self.updated_at or self.clock(), 3),
            "entries": [self._entries[key] for key in sorted(self._entries)],
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle = tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=str(self.path.parent),
                prefix=self.path.name + ".", suffix=".tmp", delete=False,
            )
            try:
                with handle:
                    json.dump(body, handle, ensure_ascii=False, indent=2)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(handle.name, self.path)
            except BaseException:
                Path(handle.name).unlink(missing_ok=True)
                raise
        except OSError as exc:
            self.last_error = type(exc).__name__
            logger.warning("slang_profile_write_failed category=%s", type(exc).__name__)
            return False
        self._mtime = self.path.stat().st_mtime
        self.last_error = ""
        return True


def _copy_entry(entry: dict) -> dict[str, object]:
    """深拷一层（列表里那些 dict 也要拷）——给出去的是快照，不是库里那份。"""

    return {
        "id": entry_id(entry.get("group_id"), entry.get("word")),
        "group_id": _text(entry.get("group_id")),
        "word": _text(entry.get("word")),
        "definition": _text(entry.get("definition")),
        "status": _text(entry.get("status")) or ENTRY_OK,
        "wrong": entry.get("status") == ENTRY_WRONG,
        "times": int(entry.get("times") or 0),
        "first_seen": float(entry.get("first_seen") or 0.0),
        "last_seen": float(entry.get("last_seen") or 0.0),
        "explainers": [dict(row) for row in (entry.get("explainers") or [])],
        "revisions": [dict(row) for row in (entry.get("revisions") or [])],
    }


class SlangWatcher:
    """**被动**收词条的观察者：收一条群消息 → 判解释句 → 写词条 → 什么都不做。

    ## 它为什么不可能"因此问一句"

    这个对象上**没有** `notify` / `transport` / `client` / `call_action`，也没有任何
    `async def`——`observe()` 是同步的，它的全部副作用就是往一份 JSON 里写几行。
    要让它发问，得先给它一样"能发东西"的东西，而那是一次**看得见**的接口改动
    （`tests/test_slang_learning.py::test_the_watcher_has_nothing_to_speak_with` 钉住字段清单）。

    ## 参数

    - `groups`：`None` = 不限制；可调用对象（核心给的是"当前启用的群"）或一组群号。
      **空集合 = 哪个群都不听**（她这会儿没在听任何群），所以那一条不记——
      这与"不限制"是两回事，刻意分开。
    - `enabled=False`：整条通道关掉（`QQBOT_SLANG_LEARN=0`），构造出来的对象也一个字节不写。
    """

    def __init__(self, store: SlangStore, *, groups=None, enabled: bool = True,
                 clock=time.time, log_path: Path | str | None = None,
                 log_capacity: int = DEFAULT_LOG_CAPACITY) -> None:
        self.store = store
        self.groups = groups
        self.enabled = bool(enabled)
        self.clock = clock
        self.captured = 0
        self.failures = 0
        self.last_result = ""
        self._seen: deque[str] = deque(maxlen=MAX_SEEN_MESSAGES)
        self._file = RollingJsonlFile(log_path or slang_log_path(),
                                      capacity=log_capacity, label="slang")

    # --- 给状态行看的一份摘要（不含正文以外的东西）--------------------------

    @property
    def log_path(self) -> Path:
        """捕获日志的位置（装配点打一行日志、`/super slang` 指路都要它）。"""

        return self._file.path

    def snapshot(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "captured": self.captured,
            "failures": self.failures,
            "last_result": self.last_result,
            "entries": len(self.store.entries_for()),
            "groups": self.store.groups(),
            "file": str(self.store.path),
            "log": str(self._file.path),
            "write_failures": self._file.write_failures,
        }

    def allowed_groups(self) -> set[str] | None:
        """这一刻允许记哪些群；`None` = 不限制。"""

        groups = self.groups() if callable(self.groups) else self.groups
        if groups is None:
            return None
        return {_text(group) for group in groups if _text(group)}

    # --- 一条消息 ---------------------------------------------------------

    def observe(self, message: object) -> int:
        """看一眼这条消息；返回**新写/并进去几条**。

        任何异常都在这里兜住（只 `failures += 1`）——观察者坏了绝不能影响对话：
        写盘失败、文件被写成目录、message 少字段，都只该表现为"少学一条"。
        """

        try:
            return self._observe(message)
        except Exception:  # noqa: BLE001 - 捕获失败绝不影响对话
            self.failures += 1
            logger.warning("slang_capture_failed", exc_info=True)
            return 0

    def _observe(self, message: object) -> int:
        if not self.enabled or message is None:
            return 0
        if bool(getattr(message, "is_bot_message", False)):
            return 0   # 她自己的话不算"群里有人解释"（那是循环）
        target = getattr(message, "target", None)
        group_id = _text(getattr(target, "group_id", ""))
        if not group_id:
            return 0   # 私聊不是"群里听到"
        allowed = self.allowed_groups()
        if allowed is not None and group_id not in allowed:
            return 0
        message_id = _text(getattr(message, "message_id", ""))
        if message_id:
            if message_id in self._seen:
                return 0
            self._seen.append(message_id)
        text = _text(getattr(message, "text", ""))
        if not text or text.startswith(("/", "#")):
            return 0   # 命令不是解释
        found = detect_explanations(text)
        if not found:
            return 0
        at = float(self.clock())
        user_id = _text(getattr(message, "user_id", ""))
        sender_name = _text(getattr(message, "sender_name", ""))
        written = 0
        for item in found:
            action = self.store.apply_explanation(
                group_id, item["word"], item["definition"],
                by_user_id=user_id, by_name=sender_name, quote=item["sentence"],
                at=at, message_id=message_id,
            )
            self._log(item, action=action, group_id=group_id, user_id=user_id,
                      sender_name=sender_name, message_id=message_id, at=at)
            if action:
                written += 1
        if written:
            self.captured += written
            self.last_result = "; ".join(
                f"{item['word']}={item['definition']}" for item in found[:MAX_PER_MESSAGE]
            )
        return written

    def _log(self, item: dict[str, str], *, action: str, group_id: str, user_id: str,
             sender_name: str, message_id: str, at: float) -> None:
        """落一份捕获记录（与其它日志同一套轮转）。**日志失败不影响词条**。"""

        self._file.append({
            "at": round(at, 3),
            "feature": "slang",
            "group_id": group_id,
            "user_id": user_id,
            "name": sender_name,
            "message_id": message_id,
            "word": item.get("word", ""),
            "definition": item.get("definition", ""),
            "shape": item.get("shape", ""),
            "action": action,
            "quote": item.get("sentence", ""),
        })


def build_watcher(*, groups=None, log_path: Path | str | None = None,
                  clock=time.time) -> tuple[SlangStore, SlangWatcher | None]:
    """装配"被动捕获"那一半（`runtime` 用）。**关掉开关就只有一个空库、没有观察者**。

    返回 `(store, watcher)`：`store` **无论开不开都给**——命令与面板的读侧
    靠它（"关了开关"应该表现为"不再记新的"，而不是"面板上那份数据也没了"）。
    """

    store = SlangStore()
    if not learning_enabled():
        return store, None
    return store, SlangWatcher(store, groups=groups, clock=clock, log_path=log_path)
