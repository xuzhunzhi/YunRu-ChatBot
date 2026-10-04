"""回复风格审核（可选）：把模型写好的台词过一道**窄职责**的校对。

由来（2026-09-29 用户："云茹现在说话很冲，很喜欢怼别人，而且很人机"）：
离线对照实测（同一批真实输入，见 `data/style_reviewer_ab.py`）——

1. 改人设（在语气规则里加 5 条反例）之后再跑**同一批输入**，她照样会怼：
   "…那解释不通，不是我的问题"、"是你先拿这个问的"、"总比某些人强，先把话说清楚再来骂"。
   也就是说这一类冲劲**光靠 prompt 压不住**。
2. 窄职责审核能在不改事实的前提下把这种尾巴删掉：10 条里改 5 条，中位长度 20→12 字，
   实测额外开销 +0.6~0.9 秒、约 360 token。
   （**这一段是历史**：当时确实以"去冲"为目标；2026-10-04 的边界见下。）
3. 开放式"重写"型审核会把事实改坏（实测把 `3.11` 改成 `0.11`），所以这里只做窄职责，
   并且加一条机械兜底：**原稿里出现过的数字串必须原样出现在定稿里**，否则退回原稿。

**2026-09-30 追加**：用户要求"增加对说教语气的审核"。真实样本里确实有这一类
（"我建议你去读一下故障码…至于 PTSD 这个词——你用得太随意了"、
"不想学也得先过，过了就不用再学了"、"你多出现几次，他们就记住你了"）——
给建议、讲道理、评价对方该怎么想、科普式讲解、安慰式的大道理。
现在它是第 6 条判据，分寸写成"**她可以有自己的看法，但不能站在讲台上**"。

**2026-10-04 改判据（用户原话）**：*"你先看看 prompt 里有没有约束，有的话改掉，
只要不是怼人就行，可以表达情绪"*。上一版审核把它当成了"去冲"的工具——判据 1 删反问、
判据 3 删吐槽、判据 5 按字数压、末尾写着"只删掉'冲'、'抬杠'和'上课'"，也就是说
**她的脾气与情绪正是被这道校对磨掉的**（它每条回复都跑一次）。现在判据收成**四类**：
**事实错误 / 越权 / 出戏 / 伤人**，并且明写"不耐烦、嫌弃、懒得理、写得冲都不改"、
"不负责把话改短、改客气、改平稳"。说教并进第 2 类"越权"，例子原样保留。
改动理由与审计清单位于 `docs/STAGE3_PENDING_DESIGNS.md` 第 ③ 节。

它永远不能挡住一条回复：审核超时、报错、输出可疑，一律按原稿发出。
"""
from __future__ import annotations

import asyncio
import logging
import re

logger = logging.getLogger(__name__)

# 审核是"多发一条消息之前"的最后一道，不能拖：超时就按原稿发。
REVIEW_TIMEOUT_SECONDS = 8.0
# 定稿不许比原稿长一倍以上（这条防的是"越改越像说明文"）。
MAX_GROWTH_RATIO = 2.0
MAX_GROWTH_EXTRA = 40
# 数字串兜底：原稿里的 `3.11`、`0.69` 这类必须原样出现在定稿里。
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
# 审核输出里常见的"包装"：引号、"修改后："、代码块。
_WRAPPER_RE = re.compile(r"^(?:修改后|定稿|最终|台词)\s*[:：]\s*", re.I)

REVIEW_SYSTEM_PROMPT = """你是一段台词的**校对**。绝大多数时候你什么都不用做。

只有在原稿**确实**出现下面某一条时，才改那一处；改完只输出最终台词本身：
1. **事实错了**——数字、称呼、时间、谁做过什么与原话不符，或者新造了没发生过的细节；
2. **越权**——她站到讲台上了：给建议/给办法（"我建议你…""你得先…""不如…""下次记得…"）、
   讲道理拔到一般结论（"其实…""本质上…""这就是为什么…"）、评价对方该怎么想怎么做
   （"你想多了""这不值得""你该习惯""别把…当成…"）、把原理来历讲一遍、
   把话说成规律（"早晚会…""人都这样"）、安慰式的大道理（"时间会…""慢慢就好了""别想太多"）、
   替人定对错或争谁说得对。她也可能本来就在教人，那就把"教"的那部分去掉，
   **改成一句平视的话；整句删掉也完全可以——宁短不长，但不许新造**。
   比如「有人连名字都没认全就得投票，你多出现几次，他们就记住你了。」
   →「有人连名字都没认全就得投票。」；「熬夜这事吧，时间久了身体会告诉你答案。」
   →「熬夜这事吧。」。改完看一眼：定稿里还看得出"在教对方"吗？看得出就继续删；
3. **出戏**——说自己是程序、模型、助手，或者讲自己的来历、设定、内部说法；
4. **伤人**——攻击、羞辱、教训、拿人的痛处或私事开刀、阴阳怪气到刺人。

**只改这四类。** 她的脾气、情绪、语气、冷淡与兴趣都**不是**要改的东西：
- 不耐烦、嫌弃、懒得理、突然没兴趣、来了兴致，都留着；写得冲也不改；
- 反问、抢白、吐槽、一句难听的实话，只要不是冲着人去的，一律留着；
- 长短、句子完不完整、有没有"其实/总之"这类口头上的连接词，都不归你管——
  **你不负责把话改短、改客气、改平稳**。

改的时候必须遵守：
- **所有数字、称呼、事实一个字都不许动**（"3.11"就还是"3.11"，"负 0.69"就还是"负 0.69"）；
- 不许把短话改长，不许加新信息，不许加客套、道歉、安慰、建议；
- 不要解释、不要加引号或前缀、不要加表情、不要换说法；
- **改完不许比原稿更冷、更淡、更客气**：删掉的只能是上面那四类，
  删完的句子该是什么语气还是什么语气。原稿里没有的情绪，也不要替她补上。

没有命中上面任何一条时，**把原稿一字不差地输出**（一个标点都不要改）。
最坏的情况是她的语气被你磨掉——原稿哪怕读着冲、带着脾气，只要不是冲着人去的，就一行都别改。"""


def _clean(raw: str) -> str:
    """剥掉审核常见的包装（引号、`修改后：`、代码块），只留台词本身。"""

    text = (raw or "").strip()
    text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text).strip()
    text = _WRAPPER_RE.sub("", text).strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'“”「」":
        text = text[1:-1].strip()
    return text


def keeps_the_facts(draft: str, reviewed: str) -> bool:
    """定稿不许**改动或新造**数字，也不许把数字全删光。

    实测教训（第一版）：开放式重写型审核把 `3.11` 改成了 `0.11`，一句话的意思就变了。
    但**不能**因此要求"原稿里每个数字都必须出现在定稿里"——审核要删的那半句
    常常带数字（"0.8 大。你要的是小数比大小，3.11 那个是版本号。" 该删成
    "0.8 大。"：后半句是在替对方定该比什么，属于越权）。第一版这么写，
    实测把这条合法改写也毙了（`reverted=1`）。

    所以改成两条：
    1. 定稿里出现的数字，原稿里必须**有**（挡住 3.11→0.11 这类改动与新造）；
    2. 原稿有数字时，定稿不能一个数字都不剩（挡住"把答案整句删掉"）。
    """

    draft_numbers = set(_NUMBER_RE.findall(draft))
    reviewed_numbers = _NUMBER_RE.findall(reviewed)
    if any(number not in draft_numbers for number in reviewed_numbers):
        return False
    return bool(reviewed_numbers) or not draft_numbers


def within_growth_limit(draft: str, reviewed: str) -> bool:
    limit = max(len(draft) * MAX_GROWTH_RATIO, len(draft) + MAX_GROWTH_EXTRA)
    return len(reviewed) <= limit


class StyleReviewer:
    """一次调用换一个"事实没被改坏、也没伤人"的定稿。没配客户端时是直通。

    它**不负责让她不冲**（2026-10-04 用户边界："只要不是怼人就行，可以表达情绪"）：
    脾气、情绪、冷淡与兴致都该原样留在她的话里。
    """

    def __init__(self, client, *, timeout: float = REVIEW_TIMEOUT_SECONDS) -> None:
        self.client = client
        self.timeout = max(0.5, float(timeout))
        self.stats = {"calls": 0, "changed": 0, "failed": 0, "reverted": 0}

    @property
    def enabled(self) -> bool:
        return self.client is not None

    async def review(self, draft: str, *, context: str = "") -> tuple[str, bool]:
        """返回 `(最终台词, 是否改过)`；任何不对劲都退回原稿。"""

        if self.client is None or not draft.strip():
            return draft, False
        self.stats["calls"] += 1
        try:
            raw = await asyncio.wait_for(
                self.client.complete(self._messages(draft, context)), self.timeout)
        except Exception as exc:  # noqa: BLE001 - 审核失败绝不能挡住一条回复
            self.stats["failed"] += 1
            logger.warning("style_review_failed category=%s", type(exc).__name__)
            return draft, False
        reviewed = _clean(raw)
        if not reviewed:
            self.stats["reverted"] += 1
            logger.info("style_review_reverted reason=empty")
            return draft, False
        if not keeps_the_facts(draft, reviewed) or not within_growth_limit(draft, reviewed):
            self.stats["reverted"] += 1
            logger.info("style_review_reverted reason=guard draft=%s reviewed=%s",
                        len(draft), len(reviewed))
            return draft, False
        if reviewed != draft:
            self.stats["changed"] += 1
        return reviewed, reviewed != draft

    @staticmethod
    def _messages(draft: str, context: str) -> list[dict[str, str]]:
        body = f"当前这句是：{context}\n\n草稿台词：{draft}" if context else f"草稿台词：{draft}"
        # prompt 每次现取：面板保存的覆盖版下一次审核就用新的（热更，2026-10-01）。
        from .prompt_library import resolve as _resolve_prompt

        return [{"role": "system", "content": _resolve_prompt("review", REVIEW_SYSTEM_PROMPT)},
                {"role": "user", "content": body}]

    def snapshot(self) -> dict[str, int]:
        return dict(self.stats)
