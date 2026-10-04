"""不懂就问：她没把握的时候以"问"回应，而不是硬编一句听起来合理的话。

由来（2026-10-04 用户）："问问题那个也可以上线了"。设计见
`docs/STAGE3_PENDING_DESIGNS.md` §①。用户举的三个例子——综测、电赛、谷歌手机——
共同点**不是"她不知道"**，而是"**她知道一点点，够填满一句话，不够说对**"：
能生成听起来合理的话，那才是"不懂装懂"最坏的形状（空白反而安全）。

这份模块装三件事：

1. **限量**（`AskBudget`）：同一话题最多一次、10 分钟内最多 3 次。
   没有它她会每句都问——那是把一个问题换成另一个问题。
2. **根据检查**（`decide_grounding`）：知识库命中 / 记忆命中 / 语境明确，
   三样都没有、判定又说这是**具体的专业·事实话题**时 → **不许主动断言**，
   只能问，或者转成她自己的角度。日常、情感、闲聊不受这条限制。
   **它是确定性的：模型"觉得自己懂"绕不过它。**
3. **措辞**（`UNSURE_ASK_RULE` 与两句 turn note）：真正写进她 prompt 的那几句话。

## 规则为什么放在这里，而不是人格里（这是 2026-10-04 那次的教训）

生产上 `BASE_PROMPT` **整段**来自 `data/private_docs/base_prompt.REAL.py`
（`base_prompt.py:249-251`：找到就整段返回，不做逐节覆盖），仓库里 `_BASE_PROMPT_TEMPLATE`
写什么都进不了她的 prompt。所以"不懂就问"这条规矩必须由**代码**拼进 system prompt
（`stage3_runtime.compose_system_prompt`），不能写进人格文件；
`tests/test_ask_when_unsure.py` 里有一条测试拿假人格证明它不依赖人格文件。

## 边界

- **不新增模型调用**：`understood` / `specialist` 两个信号由判定那趟"便宜调用"顺带给出。
- **不碰插件**：把 `plugins/` 拔掉，这一份照跑（它只依赖判定结果与眼前的消息）。
- **限量与根据检查都在引擎里**（`stage3_main._model_path`），不靠提示措辞自觉。
"""
from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # 只用于类型标注：运行期 import 会成环
    from .dialogue_judge import JudgeVerdict
    from .transport import IncomingMessage

# --- 写进她 prompt 的措辞 ---------------------------------------------------
#
# 机制词（检查/触发/调用/协议/提示词/上下文/记忆库/Stage）一个都不许出现在这里：
# 措辞会被角色吸收（AGENTS §2.2）。测试扫这一段。
# 措辞都写成**交谈视角**：没有人"给她下指令"，她只是在做自己。

#: 拼在回复 system prompt 最后的规矩（`compose_system_prompt`）。
#: 常驻、恒定：system 段每轮变一个字都会让整段前缀缓存作废。
UNSURE_ASK_RULE = """

还有一件你自己知道的事：**听不懂的时候先问一句，别硬接**。

- 他说的是一件**具体的事**——某个说法、某个行当里的规矩、某个玩意、某套流程、某个数字——
  你只是隐约有个印象、能顺口接一句却说不出一个真细节的时候，**别替他讲下去**。
  直接说你没跟上，让他说明白点；一句话就够，不用问成一串。
- 不许把他的话换个说法复述一遍当成懂了；不许说"这个我知道""我懂"，除非你真能说出一个
  具体细节；**更不许编**——数字、步骤、型号、人名、来龙去脉，不知道就说不知道。
- 同一件事问过一次就够了，别追着问个没完。真不方便细问的（他明显不想展开，或者在开玩笑），
  就只说自己那点感受，不用给出一个答案。
"""

#: 这一轮以"问"回应的现场提示（进易变段，不进 system——system 必须恒定）。
ASK_TURN_NOTE = """

--- 这一句你没跟上 ---
他说的是件具体的事，你没把握。**别急着答**：先问他一句，让他把这件事说明白
（一句话就够，别问成一串，也别装懂、别编细节）。
"""

#: 这一轮**不许断言**、但问的次数已经用完了：只用她自己的角度说。
HOLD_TURN_NOTE = """

--- 这一句你没跟上 ---
他说的是件具体的事，你没把握，而同一件事问过一次就够了。**别装懂、别下结论、别编细节**：
只说你自己的那点感受，或者把话头留给他。
"""

# --- 限量 -------------------------------------------------------------------

_TOPIC_NOISE = re.compile(r"\s+")


def topic_key(topic: object) -> str:
    """话题的比对键：去掉空白、大小写归一。

    判定每轮给的 `topic` 是几个字的一句话，同一件事两次可能多一个空格或者换个大小写；
    不归一就会出现"同一件事被当成两件事"，限量等于没限。
    """

    return _TOPIC_NOISE.sub("", str(topic or "")).casefold()[:60]


class AskBudget:
    """问的限量：**同一话题最多一次**、**一段时间内最多几次**。

    状态只在内存里，重启清零——它防的是"她每句都问"，不是配额账本，
    丢了不影响安全（最多少问几句）。
    时间由调用方传（引擎传它自己那口钟），所以测试不必等真实的十分钟。
    """

    __slots__ = ("max_per_topic", "max_per_window", "window_seconds", "_asked", "_times")

    def __init__(self, *, max_per_topic: int = 1, max_per_window: int = 3,
                 window_seconds: float = 600.0) -> None:
        # 0 表示"一次都不许问"——配置项可以直接把这条路关掉。
        self.max_per_topic = max(0, int(max_per_topic))
        self.max_per_window = max(0, int(max_per_window))
        self.window_seconds = max(0.0, float(window_seconds))
        self._asked: dict[tuple[str, str], int] = {}
        self._times: dict[str, deque[float]] = {}

    def _recent(self, session_id: str, now: float) -> deque[float]:
        """这个会话窗口内的问句时间（顺手把过期的裁掉）。"""

        times = self._times.setdefault(str(session_id), deque())
        cutoff = float(now) - self.window_seconds
        while times and times[0] < cutoff:
            times.popleft()
        return times

    def allows(self, session_id: str, topic: object, now: float) -> bool:
        """现在还能不能问。窗口按**会话**算：一个群问得太多，别的群不受牵连。"""

        if self.max_per_topic <= 0 or self.max_per_window <= 0:
            return False
        if self._asked.get((str(session_id), topic_key(topic)), 0) >= self.max_per_topic:
            return False
        return len(self._recent(session_id, now)) < self.max_per_window

    def note(self, session_id: str, topic: object, now: float) -> None:
        """记下"这次真的问了"。"""

        session = str(session_id)
        if len(self._asked) > 512 or len(self._times) > 512:  # 群多起来也不让它无限长
            self._asked.clear()
            self._times.clear()
        key = (session, topic_key(topic))
        self._asked[key] = self._asked.get(key, 0) + 1
        recent = self._recent(session, now)
        recent.append(float(now))

    def snapshot(self) -> dict[str, object]:
        """给 `/super status` 或排障看的只读计数（不含话题正文之外的东西）。"""

        return {
            "topics": len(self._asked),
            "sessions": len(self._times),
            "max_per_topic": self.max_per_topic,
            "max_per_window": self.max_per_window,
            "window_seconds": self.window_seconds,
        }


# --- 根据检查（确定性，模型绕不过它）----------------------------------------

#: "这一条自己正在解释这件事"的形状。刻意只认很窄的几种写法：
#: 宽一点就会把"这个就是我想说的"当成解释，于是"没有根据"被误判成"语境明确"——
#: 那正好放过了要防的"甩一个名词过来，她顺着编".
_EXPLAINING = re.compile(
    r"(?:意思|含意|含义|指的是|所谓|也就是)|就是[^。！？\n]{0,12}(?:的意思|意思)"
)


def context_is_explicit(message: "IncomingMessage", visible) -> bool:
    """「语境明确」的**确定性**判据（三种根据里的第三种）。

    只认两条线索，**判不准就往"不明确"那一边倒**（判不准就去问，不许硬答）：

    1. 这一条**挂在眼前某一句上**（引用/回复了这一段里看得见的那条）——
       被挂的那句就是它的来龙去脉；
    2. 这一条自己**正在解释这件事**（"……的意思是……""指的是……""也就是……"），
       也就是对方把话说开了，而不是只甩一个名词过来。

    **它不是"她答得对不对"的判据**，只是"眼前有没有可依的前文"。
    说得更直白一点：这条判据很粗，粗的那一侧是安全的（多问一句），
    而不是"多编一句"。
    """

    quoted = str(getattr(message, "reply_to_message_id", "") or "")
    if quoted:
        for other in visible or ():
            if getattr(other, "message_id", "") == quoted:
                return True
    return bool(_EXPLAINING.search(str(getattr(message, "text", "") or "")))


@dataclass(frozen=True, slots=True)
class GroundingDecision:
    """这一轮的确定性结论：要不要出声、出声的话是不是"问"。"""

    #: 这一轮要不要出声。False = 连判定说的 REPLY 也不接（没懂/没根据时不主动插话）。
    speak: bool
    #: 出声时以"问"回应；False 表示"只用自己的角度说，别下结论"。
    ask: bool
    #: 要不要把"别装懂、别编"的现场提示拼进 prompt（照旧断言时为 False）。
    care: bool
    #: 日志用的原因（固定几种说法，不带正文）。
    reason: str
    unclear: bool = False
    specialist: bool = False
    grounded: bool = False

    @property
    def turn_note(self) -> str:
        """这一次要拼进 prompt 的现场提示（不出声、或照旧说话时是空串）。"""

        if not self.care or not self.speak:
            return ""
        return ASK_TURN_NOTE if self.ask else HOLD_TURN_NOTE


def decide_grounding(
    verdict: "JudgeVerdict",
    *,
    called: bool,
    has_knowledge: bool,
    has_memory: bool,
    context_explicit: bool,
    ask_allowed: bool,
) -> GroundingDecision:
    """把判定给的信号 + 三样根据合成一个**确定性**的结论。

    规矩只有两条（`docs/STAGE3_PENDING_DESIGNS.md` §① 的设计）：

    1. **没听懂**：被叫到就"问"，没被叫到就不开口（不硬接）；
    2. **具体的专业·事实话题**要有至少一处根据（知识库命中 / 记忆命中 / 语境明确）
       才许主动接；三样都没有时，被叫到了也只能问，没被叫到就不开口。

    `called` 传"这一次是不是在跟她说话"（@ 她、点名她、直接问她、答应过人家"稍等"）。
    **被叫到时她不冷落人这条原意保留**：这里只允许她把"断言的答"换成"问"，
    不允许她沉默——唯一的沉默是"没被叫到 + 没把握"。
    """

    unclear = bool(getattr(verdict, "unsure", False))
    specialist = bool(getattr(verdict, "specialist", False))
    grounded = bool(has_knowledge or has_memory or context_explicit)
    # 日常、情感、闲聊不受"根据检查"的限制：只有判定说是**具体的专业·事实话题**、
    # 而三样根据都没有时，才不许主动断言。
    care = unclear or (specialist and not grounded)
    if not care:
        return GroundingDecision(speak=True, ask=False, care=False, reason="照旧",
                                 grounded=grounded)
    if not called:
        return GroundingDecision(
            speak=False, ask=False, care=True,
            reason="没听懂、也没被叫到" if unclear else "没有根据、也没被叫到",
            unclear=unclear, specialist=specialist, grounded=grounded)
    if ask_allowed:
        return GroundingDecision(speak=True, ask=True, care=True, reason="以问回应",
                                 unclear=unclear, specialist=specialist, grounded=grounded)
    return GroundingDecision(
        speak=True, ask=False, care=True, reason="问过一次了，只用自己的角度",
        unclear=unclear, specialist=specialist, grounded=grounded)
