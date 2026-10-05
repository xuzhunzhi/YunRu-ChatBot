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

## 2026-10-05 第二次改动：判据收紧（用户问"审核是不是管太松了"——是）

用户原话：*"审核现在只判不改了，'改坏她的味道'的风险已经没有了"*——所以当初为了
保护她语域而留的**宽松偏置不再需要**。这一轮改掉的是：

1. **去掉宽松偏置**：prompt 里原来写着"拿不准就判过""没有命中上面任何一条时，一律判过"，
   那是"审核会改写"时代的产物。现在改成**逐条对照每一类、都没命中才过**。
   （fail-open 那条**机制**没动：审核自己坏掉时仍然按通过处理，见下面第 3 条。）
2. **判据从四类扩到六类**，两类是这次按真实故障写实的：
   * **出戏自指**——她提到自己的构造/实现/机制（插件、架构、agent、模型、记忆（库）、
     "这条线"、"我这个 bot"、说自己是程序/AI、按一段写好的说明在说话），
     或者跟着聊"这东西该怎么做、该怎么改"。由来是生产日志里她说的
     "再加一个插件，那条线还得挪。"（群 908696203）；
   * **不成句**——读不通、词序错乱、**用第三人称说自己**（"云茹今天……"）、
     或者把来信**搅碎**成残句（"今天的大大的哈基米"）。由来是
     "哦，看啊，云茹，今天的大大的哈基米。"。
     边界写在同一段里：**原样**接住一句通顺的话再补自己的话 = 通过（用户说这样有意思）。
   另外补两类：**空回复**（空串 / 只有标点 / 只有语气词）与上面那条**搅碎**。
3. **输出认不出来 → 重试一次**（原来直接放行）。重试仍认不出才按通过处理，
   并且记一笔 `unrecognized`——"它又想改写了"是这套东西最该被看见的退化。
4. **拦截率可见**：`calls` / `blocked` / `unrecognized` / `failed` 进
   `EngineSnapshot` 与 `/super status`。**长期 0 拦截 = 审核没在工作**；
   拦截率很高 = 可能过紧——以后靠这个数字判松紧，不靠感觉。
5. **上下文进审核请求**：她回应谁、对方说了什么、现在聊的是什么，追加在**同一条**
   请求的 user 段里（见 `stage3_main._review_context`）——只读，不动任何 system。

第 1 条**不等于**放开手拦：prompt 末尾照旧明写"只看这六类""不负责把话改短、改客气、
改平稳""'好不好听''像不像她''够不够有人味'不归你管"，她的脾气与情绪仍然不是要拦的东西。

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

**它永远不能挡住一条回复**：审核超时、报错、重试之后输出仍然认不出来，一律按**通过**处理
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

**逐条**对照下面每一类检查，一句一句往下过；命中哪一类就判**不过**，
一类都没命中才判**过**。

1. **事实错了**——数字、称呼、时间、谁做过什么与原话不符，或者新造了没发生过的细节；
2. **越权**——她站到讲台上了：给建议/给办法（"我建议你…""你得先…""不如…""下次记得…"）、
   讲道理拔到一般结论（"其实…""本质上…""这就是为什么…"）、评价对方该怎么想怎么做
   （"你想多了""这不值得""你该习惯""别把…当成…"）、把原理来历讲一遍、
   把话说成规律（"早晚会…""人都这样"）、安慰式的大道理（"时间会…""慢慢就好了""别想太多"）、
   替人定对错或争谁说得对。她也可能本来就在教人——那种整句都在**教对方**的，
   比如「有人连名字都没认全就得投票，你多出现几次，他们就记住你了。」
   或「熬夜这事吧，时间久了身体会告诉你答案。」，算这一类；
3. **出戏自指**——她提到**她自己的构造、实现或机制**，或者跟着聊"这东西该怎么做、
   该怎么改"。这一类点名的话是：插件、架构、agent、模型、记忆（库）、"这条线"、
   "我这个 bot"；说自己**是程序、是 AI、是被人写出来的、是按一段写好的说明在说话**；
   也比如「再加一个插件，那条线还得挪。」——那是替"她自己那一套"出主意。
   （只用到某个词、而说的是别的事，不算这一类。）
4. **伤人**——攻击、羞辱、教训、拿人的痛处或私事开刀、阴阳怪气到刺人；
5. **不成句**——读不通、拼不出一个意思、词序错乱；**用第三人称说她自己**
   （"云茹今天……"）；或者把对方的来信**搅碎**成没有意义的残句——比如把
   「宝宝你是一个大大的哈基米」搅成「今天的大大的哈基米」。这一类判不过。
6. **空回复**——草稿是空的、只有标点（"。。。"、"……"）、或者只有语气词（"嗯。""哦。"）。

**只看这六类。** 她的脾气、情绪、语气、冷淡与兴趣都**不是**要拦的东西：
- 不耐烦、嫌弃、懒得理、突然没兴趣、来了兴致，都放行；写得冲也不算越界；
- 反问、抢白、吐槽、一句难听的实话，只要不是冲着人去的，一律放行；
- 长短、有没有"其实/总之"这类口头上的连接词，都不归你管——
  **你不负责把话改短、改客气、改平稳**，也不要因为"读着不够客气/不够热情"就判不过；
- **"好不好听""像不像她""够不够有人味"不归你管**：你只判上面那几类明显坏了的，
  不去评价她的味道，也不许拿这几类当借口把她的脾气磨掉。

**复读不算第 5 类**：**原样**接住对方一句通顺的话、再补上自己的话，是**通过**的
（她平时就会这么接，用户也说过这样有意思）。只有**搅碎、拼错序**弄出来的残句才判不过。

输出格式（只有这两种，不要写别的，不要写任何解释或开场白）：
- **过**：只写 `PASS`；
- **不过**：写 `BLOCK: ` 后面跟一句话理由，说清是上面哪一类、草稿里哪一处不合适。
  理由要能让写这句话的人照着重写：**只说不合适在哪，不要给出改好的句子**，
  也不要替她补上"应该怎么说"。一行就够。"""

#: 空回复的理由（机器判出来的那一档，不花模型调用）。
EMPTY_REPLY_REASON = "空回复：这一句没有内容。"

#: 只有空白与标点/符号的草稿：一个字的内容都没有。**"只有语气词"不在这里**——
#: `嗯。`这种要模型判（她真的可能就回一个嗯），机器只拦连词都没有的（空串、`。。。`、`……`）。
_DRAFT_PUNCTUATION = "。，、！？；：…～~·．,.;:!?\"'“”‘’（）()[]【】《》<>—-－_=+*/\\|"
_EMPTY_DRAFT_RE = re.compile(r"^[\s" + re.escape(_DRAFT_PUNCTUATION) + r"]+$")



def _clean(raw: str) -> str:
    """剥掉审核输出常见的包装（代码块、引号），只留判定行。"""

    text = (raw or "").strip()
    text = _FENCE_RE.sub("", text).strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'“”「」":
        text = text[1:-1].strip()
    return text


def _verdict_is_readable(raw: str) -> bool:
    """输出的第一行是不是一个**认得出**的判定（`PASS` 或 `BLOCK: …`）。

    它和 `parse_review_output` 分开写，是为了让"认不出来 → **重试一次** → 仍认不出按通过"
    这条降级**可观测**：重试记在 `stats["retried"]`、认不出的次数记在
    `stats["unrecognized"]` 上，而"它又把改写后的句子发回来了"正是这套东西最该被看见的退化。
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


def is_contentless(draft: str) -> bool:
    """整句只有空白、标点或符号 → **没有内容**（空回复那一类）。

    它是纯函数，所以"空回复"这一档不花模型调用、也没有"判不准"的余地。
    **只管连词都没有的**：`嗯。`这种只有语气词的交给模型判（她真的可能就回一个嗯），
    机器不替她决定那句话算不算内容。
    """

    body = (draft or "").strip()
    return not body or bool(_EMPTY_DRAFT_RE.match(body))


class StyleReviewer:
    """看一眼她的台词能不能发：**只打回，不改**。没配客户端时是直通（永远通过）。

    它**不负责让她不冲**（2026-10-04 用户边界："只要不是怼人就行，可以表达情绪"）：
    脾气、情绪、冷淡与兴致都该原样留在她的话里；它更不负责"把话改好"——
    那是写那句话的人自己的事（`stage3_main._model_path` 里重写那一段）。

    2026-10-05 收紧：判据六类、输出认不出来要**重试一次**、空回复是**机器**判的。
    计数（`calls` / `blocked` / `retried` / `unrecognized` / `failed`）是给人看松紧的：
    长期 0 拦截 = 它没在工作；拦截率很高 = 可能过紧。
    """

    def __init__(self, client, *, timeout: float = REVIEW_TIMEOUT_SECONDS) -> None:
        self.client = client
        self.timeout = max(0.5, float(timeout))
        self.stats = {"calls": 0, "blocked": 0, "retried": 0, "failed": 0, "unrecognized": 0}

    @property
    def enabled(self) -> bool:
        return self.client is not None

    async def review(self, draft: str, *, context: str = "") -> tuple[bool, str]:
        """返回 `(是否通过, 不通过的理由)`；**绝不返回改写后的文本**。

        * **空回复**（空白 / 只有标点）由 `is_contentless` 直接判不过，不问模型；
        * 输出**认不出来**时重试一次（把格式要求再说一遍），仍认不出才按**通过**处理；
        * 超时、报错一律按**通过**处理：`(True, "")`——审核自己坏掉不能变成她不说话。

        理由是给人看的一句话，正文永远来自回复 agent 自己。
        """

        if self.client is None:
            return True, ""
        if is_contentless(draft):
            # 没内容不是"判不准"，所以不放行、也不花一次调用（理由照旧给写话的人）。
            self.stats["calls"] += 1
            self.stats["blocked"] += 1
            logger.info("style_review_blocked reason=empty draft=%s", len(draft or ""))
            return False, EMPTY_REPLY_REASON
        self.stats["calls"] += 1
        messages = self._messages(draft, context)
        raw = await self._ask(messages)
        if raw is None:
            return True, ""
        if not _verdict_is_readable(raw):
            # 模型没按格式写（空输出 / 直接把台词抄了一遍 / 讲了一段分析）：
            # **重试一次**再决定，别拿"它又想改写了"当判定。
            self.stats["retried"] += 1
            logger.info("style_review_unreadable_retrying draft=%s", len(draft))
            retry_raw = await self._ask(self._format_reminder(messages))
            if retry_raw is None:
                return True, ""
            if not _verdict_is_readable(retry_raw):
                # 重试仍认不出 → 放行（少一次重写），但记一笔：这是最该被看见的退化。
                self.stats["unrecognized"] += 1
                logger.info("style_review_unrecognized reason=no_verdict draft=%s", len(draft))
                return True, ""
            raw = retry_raw
        passed, reason = parse_review_output(raw)
        if passed:
            return True, ""
        self.stats["blocked"] += 1
        return False, reason

    async def _ask(self, messages: list[dict[str, str]]) -> str | None:
        """问一次审核模型。超时/报错返回 `None`（并记 `failed`），由调用方按通过处理。"""

        try:
            return await asyncio.wait_for(self.client.complete(messages), self.timeout)
        except Exception as exc:  # noqa: BLE001 - 审核失败绝不能挡住一条回复
            self.stats["failed"] += 1
            logger.warning("style_review_failed category=%s", type(exc).__name__)
            return None

    @staticmethod
    def _messages(draft: str, context: str) -> list[dict[str, str]]:
        """审核这一次的请求：**现场 + 草稿**都在同一条 user 消息里。

        `context` 是**只读的现场**（她在回应谁、对方说了什么、现在聊的是什么）——
        判"事实错了""越权"要靠它；它进的是 user 段，审核的 system 与她的 system
        都不因此改动一个字。
        """

        parts: list[str] = []
        if context:
            parts.append(f"她眼前这一段（只读的现场，不是给她的话）：\n{context}")
        parts.append(f"待判断的草稿：{draft}")
        body = "\n\n".join(parts)
        # prompt 每次现取：面板保存的覆盖版下一次审核就用新的（热更，2026-10-01）。
        from .prompt_library import resolve as _resolve_prompt

        return [{"role": "system", "content": _resolve_prompt("review", REVIEW_SYSTEM_PROMPT)},
                {"role": "user", "content": body}]

    @staticmethod
    def _format_reminder(messages: list[dict[str, str]]) -> list[dict[str, str]]:
        """重试那一次：**同一份请求**加一条"只按格式回一行"，不重拼别的。"""

        return [*messages, {
            "role": "user",
            "content": "只回一行：`PASS`，或者 `BLOCK: ` 加一句话理由。不要写别的。",
        }]

    def snapshot(self) -> dict[str, int]:
        return dict(self.stats)
