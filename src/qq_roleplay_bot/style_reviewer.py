"""回复风格审核（可选）：把模型写好的台词过一道**窄职责**的校对。

由来（2026-09-29 用户："云茹现在说话很冲，很喜欢怼别人，而且很人机"）：
离线对照实测（同一批真实输入，见 `data/style_reviewer_ab.py`）——

1. 改人设（在语气规则里加 5 条反例）之后再跑**同一批输入**，她照样会怼：
   "…那解释不通，不是我的问题"、"是你先拿这个问的"、"总比某些人强，先把话说清楚再来骂"。
   也就是说这一类冲劲**光靠 prompt 压不住**。
2. 窄职责审核能在不改事实的前提下把这种尾巴删掉：10 条里改 5 条，中位长度 20→12 字，
   实测额外开销 +0.6~0.9 秒、约 360 token。
3. 开放式"重写"型审核会把事实改坏（实测把 `3.11` 改成 `0.11`），所以这里只做窄职责，
   并且加一条机械兜底：**原稿里出现过的数字串必须原样出现在定稿里**，否则退回原稿。

**2026-09-30 追加**：用户要求"增加对说教语气的审核"。真实样本里确实有这一类
（"我建议你去读一下故障码…至于 PTSD 这个词——你用得太随意了"、
"不想学也得先过，过了就不用再学了"、"你多出现几次，他们就记住你了"）——
给建议、讲道理、评价对方该怎么想、科普式讲解、安慰式的大道理。
现在它是第 6 条判据，分寸写成"**她可以有自己的看法，但不能站在讲台上**"。

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
1. 反问句回敬或抢白（"你打算怎么办""难道不是""你就不这么说了"）；
2. 指责对方没看/没说/搞混/绕回来（"你没看""你说的是什么""你非要当成一回事"）；
3. 评论这段对话本身、讲道理、下结论、争谁对（"先说清楚""我没法帮你""不是我的问题"
   "是你先拿这个问的"）；
4. 助手腔连接词（"其实""总之""因此""如果""换句话说""值得"）；
5. 超过 40 字；
6. **说教/上课**——2026-09-30 用户要求补的这一条：
   - **给建议、给办法**："我建议你…""你得先…""不如…""下次记得…""多出现几次就好了"；
   - **讲道理、拔到一般结论**："其实…""本质上…""这就是为什么…""不是心理问题，是机械问题"；
   - **评价对方该怎么想、怎么做**："你想多了""这不值得""你该习惯""别把…当成…"；
   - **科普式讲解**：把原理/来历给对方讲一遍（"它其实是靠…实现的""这一类东西都…"）；
   - **顺带讲解**：对方只是随口一问／一说，却把背后的道理讲给他听
     （"…是同一个路子——伤害全靠…，只需要记得先放哪个"）；
   - **把话说成规律**："…也得…，…就不用…了""早晚会…""人都这样"；
   - **安慰式的大道理**："时间会…""慢慢就好了""别想太多"。
   这里的分寸是：**她可以有自己的看法，但不能站在讲台上**。改成一句平视、就事论事的话；
   整句删掉也完全可以。

   **第 6 条怎么改（照这几个例子来，都是删，不是补）**：
   - 「我建议你去读一下故障码。缺缸的车挂不上高档是机械问题，不是心理问题。」
     → 「缺缸的车挂不上高档，是机械问题。」（删掉"我建议你…"与后半句的纠正）
   - 「有人连名字都没认全就得投票，你多出现几次，他们就记住你了。」
     → 「有人连名字都没认全就得投票。」（删掉"教他怎么做"的那半句）
   - 「复变的名字听着吓人，其实是最规矩的一门。」→ 「复变听着吓人，规矩得很。」
     （去掉"其实…"这种讲解腔）
   - 「熬夜这事吧，时间久了身体会告诉你答案。你现在觉得没事，以后会慢慢还回来的。」
     → 「熬夜这事吧。」（整条都在讲道理时，只留最短的那半句——**宁短不长，但不许新造**）
   改完再自检一遍：定稿里还看得出"在教对方"吗？看得出就继续删。

改的时候必须遵守：
- **所有数字、称呼、事实一个字都不许动**（"3.11"就还是"3.11"，"负 0.69"就还是"负 0.69"）；
- 不许把短话改长，不许加新信息，不许加客套、道歉、安慰、建议；
- 不要解释、不要加引号或前缀、不要加表情；
- 她的疏离与干涩的自嘲要留着，只删掉"冲"、"抬杠"和"上课"。

没有命中上面任何一条时，**把原稿一字不差地输出**（一个标点都不要改）。"""


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
    但**不能**因此要求"原稿里每个数字都必须出现在定稿里"——审核的活就是删掉冲的尾巴，
    而那些句子里常常带数字（"0.8 大。你要的是小数比大小，3.11 那个是版本号。" 该删成
    "0.8 大。"）。第一版这么写，实测把这条合法改写也毙了（`reverted=1`）。

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
    """一次调用换一个"更像她、且不冲"的定稿。没配客户端时是直通。"""

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
