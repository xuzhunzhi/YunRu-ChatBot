"""回复风格审核（可选）：只**判断**她写好的台词能不能发，**不改写**。

## 2026-10-05 改判据（用户原话："审核只负责打回，不负责修改"）

在这之前它是**改写型**的：`review()` 返回改完的文本，`REVIEW_SYSTEM_PROMPT` 自称
"校对"、末尾写着"只改这四类"。用户报的故障是"她的回复风格变得很奇怪，很 ai"
（另一个原因见 `stage3_runtime.SYSTEM_PROMPT` 上面那段「不懂就问」的更正）；
**风格审核在直接改写她的话**是这次定位到的第二个原因。

现在的形状：

* `review(draft) -> (passed, reason)`——**只给判定 + 理由**，
  第二项永远是"为什么不通过"，**绝不返回改写后的文本**；
* 不通过时由**写这句话的人**（回复 agent）在同一轮里重写一次，理由交回给它。
  装配点见 `stage3_main._model_path`：被拒 → 重写一次；**最多一次，不许循环**；
  重写后仍被拒 → **发重写的那一版**（不许把回复丢掉、不许卡住），并记一笔；
* 四类判据保留：**事实错误 / 越权 / 出戏 / 伤人**。她的脾气、情绪、语气、
  冷淡与兴趣都**不是**要拦的东西。

## 历史（为什么它原来是窄职责改写，而不是开放重写）

由来（2026-09-29 用户："云茹现在说话很冲，很喜欢怼别人，而且很人机"）：
离线对照实测（同一批真实输入，见 `data/style_reviewer_ab.py`）——

1. 改人设（在语气规则里加 5 条反例）之后再跑**同一批输入**，她照样会怼：
   "…那解释不通，不是我的问题"、"是你先拿这个问的"、"总比某些人强，先把话说清楚再来骂"。
   也就是说这一类冲劲**光靠 prompt 压不住**。
2. 窄职责改写能在不改事实的前提下把这种尾巴删掉：10 条里改 5 条，中位长度 20→12 字，
   实测额外开销 +0.6~0.9 秒、约 360 token。
3. 开放式"重写"型审核会把事实改坏（实测把 `3.11` 改成 `0.11`），所以当时只做窄职责，
   并加一条机械兜底：原稿里出现过的数字串必须原样出现在定稿里。

**那三条现在都不成立了**：审核不再产出文本，所以"改坏事实"由**结构**排除
（不是靠兜底），`keeps_the_facts` / `within_growth_limit` 两条机械规则连同
"数字必须逐字保留"的写法一起删掉了——它们守的那件事已经不存在了。

**2026-09-30 追加**（当时）：用户要求"增加对说教语气的审核"。真实样本里确实有这一类
（"我建议你去读一下故障码…至于 PTSD 这个词——你用得太随意了"、
"不想学也得先过，过了就不用再学了"、"你多出现几次，他们就记住你了"）——
给建议、讲道理、评价对方该怎么想、科普式讲解、安慰式的大道理。
现在它是第 2 类"越权"的判据，分寸写成"**她可以有自己的看法，但不能站在讲台上**"。

**2026-10-04 改判据**（用户原话）：*"你先看看 prompt 里有没有约束，有的话改掉，
只要不是怼人就行，可以表达情绪"*。上一版审核把它当成了"去冲"的工具——判据 1 删反问、
判据 3 删吐槽、判据 5 按字数压、末尾写着"只删掉'冲'、'抬杠'和'上课'"，也就是说
**她的脾气与情绪正是被这道审核磨掉的**（它每条回复都跑一次）。判据收成**四类**，
并且明写"不耐烦、嫌弃、懒得理、写得冲都不改"、"不负责把话改短、改客气、改平稳"。
改动理由与审计清单位于 `docs/STAGE3_PENDING_DESIGNS.md` 第 ③ 节。

**它永远不能挡住一条回复**：审核超时、报错、输出认不出来，一律按**通过**处理
（少一次重写，直接发原稿）。被拒也只多花一次重写，最后发出去的一定是一句话。
"""
from __future__ import annotations

import asyncio
import logging
import re

logger = logging.getLogger(__name__)

# 审核是"多发一条消息之前"的最后一道，不能拖：超时就按通过处理（原稿直发）。
REVIEW_TIMEOUT_SECONDS = 8.0
# 理由的长度上限：它是给回复 agent 看的一句话，不是一篇分析。
MAX_REASON_CHARS = 200
# 审核输出里常见的"包装"：引号、代码块。
_FENCE_RE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")
#: 判定行：通过。模型偶尔会写成中文或 `OK`。
_PASS_RE = re.compile(r"^(?:PASS|OK|通过|没问题|可以)[。.！!\s]*$", re.I)
#: 判定行：不通过（后面跟理由）。
_BLOCK_RE = re.compile(r"^(?:BLOCK|REJECT|FAIL|不通过|不过关|驳回)\s*[:：]?\s*", re.I)

REVIEW_SYSTEM_PROMPT = """你在一句话**发出去之前**看一眼它能不能发。你**只判断，不改写**：
你的输出是"过"或"不过 + 理由"；不过的时候，**由写这句话的人自己重写**，
你不提供任何改好的句子。

只有在草稿**确实**出现下面某一条时，才判**不过**：
1. **事实错了**——数字、称呼、时间、谁做过什么与原话不符，或者新造了没发生过的细节；
2. **越权**——她站到讲台上了：给建议/给办法（"我建议你…""你得先…""不如…""下次记得…"）、
   讲道理拔到一般结论（"其实…""本质上…""这就是为什么…"）、评价对方该怎么想怎么做
   （"你想多了""这不值得""你该习惯""别把…当成…"）、把原理来历讲一遍、
   把话说成规律（"早晚会…""人都这样"）、安慰式的大道理（"时间会…""慢慢就好了""别想太多"）、
   替人定对错或争谁说得对。她也可能本来就在教人——那种整句都在**教对方**的，
   比如「有人连名字都没认全就得投票，你多出现几次，他们就记住你了。」
   或「熬夜这事吧，时间久了身体会告诉你答案。」，算这一类；
3. **出戏**——说自己是程序、模型、助手，或者讲自己的来历、设定、内部说法；
4. **伤人**——攻击、羞辱、教训、拿人的痛处或私事开刀、阴阳怪气到刺人。

**只看这四类。** 她的脾气、情绪、语气、冷淡与兴趣都**不是**要拦的东西：
- 不耐烦、嫌弃、懒得理、突然没兴趣、来了兴致，都放行；写得冲也不算越界；
- 反问、抢白、吐槽、一句难听的实话，只要不是冲着人去的，一律放行；
- 长短、句子完不完整、有没有"其实/总之"这类口头上的连接词，都不归你管——
  **你不负责把话改短、改客气、改平稳**，也不要因为"读着不够客气/不够热情"就判不过。

输出格式（只有这两种，不要写别的，不要写任何解释或开场白）：
- **过**：只写 `PASS`；
- **不过**：写 `BLOCK: ` 后面跟一句话理由，说清是上面哪一类、草稿里哪一处不合适。
  理由要能让写这句话的人照着重写：**只说不合适在哪，不要给出改好的句子**，
  也不要替她补上"应该怎么说"。一行就够。

拿不准就判**过**：最坏的情况是她的语气被人为磨掉——原稿哪怕读着冲、带着脾气，
只要不是冲着人去的，就判过。没有命中上面任何一条时，**一律判过**。"""


def _clean(raw: str) -> str:
    """剥掉审核输出常见的包装（代码块、引号），只留判定行。"""

    text = (raw or "").strip()
    text = _FENCE_RE.sub("", text).strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'“”「」":
        text = text[1:-1].strip()
    return text


def _verdict_is_readable(raw: str) -> bool:
    """输出的第一行是不是一个**认得出**的判定（`PASS` 或 `BLOCK: …`）。

    它和 `parse_review_output` 分开写，是为了让"认不出来 → 按通过"这条降级
    **可观测**：认不出的次数记在 `stats["unrecognized"]` 上，
    而"它又把改写后的句子发回来了"正是这套东西最该被看见的退化。
    """

    first = _clean(raw).partition("\n")[0].strip()
    return bool(_PASS_RE.match(first) or _BLOCK_RE.match(first))


def parse_review_output(raw: str) -> tuple[bool, str]:
    """把审核的输出读成 `(是否通过, 不通过的理由)`。

    **认不出来一律按通过**（fail-open）：审核是"多发一条消息之前"的最后一道，
    它自己出问题时，代价必须是"少一次重写"，不能是"她的话被挡下来"。
    所以这里没有"可疑就拒"这一档——只有明确写了"不过"才算拒。
    """

    text = _clean(raw)
    if not text:
        return True, ""
    first, _, rest = text.partition("\n")
    first = first.strip()
    if _BLOCK_RE.match(first):
        reason = _BLOCK_RE.sub("", first).strip() or rest.strip()
        # 理由也认不出来（只写了 "BLOCK"）时仍算不通过，但理由留空——
        # 调用方照着空理由重写一次，总比"因为审核少写了几个字就放它过去"更符合意图。
        return False, " ".join(reason.split())[:MAX_REASON_CHARS]
    if _PASS_RE.match(first):
        return True, ""
    return True, ""


class StyleReviewer:
    """看一眼她的台词能不能发：**只打回，不改**。没配客户端时是直通（永远通过）。

    它**不负责让她不冲**（2026-10-04 用户边界："只要不是怼人就行，可以表达情绪"）：
    脾气、情绪、冷淡与兴致都该原样留在她的话里；它更不负责"把话改好"——
    那是写那句话的人自己的事（`stage3_main._model_path` 里重写那一段）。
    """

    def __init__(self, client, *, timeout: float = REVIEW_TIMEOUT_SECONDS) -> None:
        self.client = client
        self.timeout = max(0.5, float(timeout))
        self.stats = {"calls": 0, "blocked": 0, "failed": 0, "unrecognized": 0}

    @property
    def enabled(self) -> bool:
        return self.client is not None

    async def review(self, draft: str, *, context: str = "") -> tuple[bool, str]:
        """返回 `(是否通过, 不通过的理由)`；**绝不返回改写后的文本**。

        任何不对劲（超时、报错、空输出、认不出的输出）都按**通过**处理：
        `(True, "")`。理由是给人看的一句话，正文永远来自回复 agent 自己。
        """

        if self.client is None or not draft.strip():
            return True, ""
        self.stats["calls"] += 1
        try:
            raw = await asyncio.wait_for(
                self.client.complete(self._messages(draft, context)), self.timeout)
        except Exception as exc:  # noqa: BLE001 - 审核失败绝不能挡住一条回复
            self.stats["failed"] += 1
            logger.warning("style_review_failed category=%s", type(exc).__name__)
            return True, ""
        passed, reason = parse_review_output(raw)
        if not _verdict_is_readable(raw):
            # 模型没按格式写（空输出 / 直接把台词抄了一遍 / 讲了一段分析）：
            # 按通过处理，但要记一笔——"它又想改写了"是这套东西最该被看见的退化。
            self.stats["unrecognized"] += 1
            logger.info("style_review_unrecognized reason=no_verdict draft=%s", len(draft))
            return True, ""
        if passed:
            return True, ""
        self.stats["blocked"] += 1
        return False, reason

    @staticmethod
    def _messages(draft: str, context: str) -> list[dict[str, str]]:
        body = (f"当前这句是：{context}\n\n待判断的草稿：{draft}" if context
                else f"待判断的草稿：{draft}")
        # prompt 每次现取：面板保存的覆盖版下一次审核就用新的（热更，2026-10-01）。
        from .prompt_library import resolve as _resolve_prompt

        return [{"role": "system", "content": _resolve_prompt("review", REVIEW_SYSTEM_PROMPT)},
                {"role": "user", "content": body}]

    def snapshot(self) -> dict[str, int]:
        return dict(self.stats)
