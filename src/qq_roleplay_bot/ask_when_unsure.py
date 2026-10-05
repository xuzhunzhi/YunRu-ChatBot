"""没把握的时候：**要么不出声，要么别断言**——不是"问她一句"。

模型名里的 `ask` 是历史遗留（这一份 2026-10-04 叫"不懂就问"）。**2026-10-05 晚改口径**：
用户否掉了"不懂就问"那条路（原话：*"别做不懂就问"* / *"先改掉**不懂装懂硬插话**"*）。
现在这一份装的是三件事：

1. **判定给的两个信号**（`JudgeVerdict.understood` / `.specialist`，在 `dialogue_judge.py`）：
   这一句她**懂没懂**、是不是**具体的专业·事实话题**。它们由原本那趟判定"顺带"给出，
   **不多花一次模型调用**；没给标签时一律退回旧行为（clear / 不专业）。
2. **路由**（`decide_grounding`，纯函数、确定性）：**没被叫到**、又没懂、又是具体话题
   → **不插话**（不出声，**不是**"问一句"）；被叫到 → 照常开口，走到第 3 条。
3. **兜底**（本模块只提供判据，动作在 `stage3_main._model_path`）：
   被叫到、她这一版的话里**有具体断言**（数字 / 型号 / 流程），而知识库 / 记忆 / 语境
   **三处都没有根据** → **打回，让她自己重写一次**
   （复用"审核只打回、回复 agent 在同一轮重写一次"那条路，**不新增机器**）。

## 上一版的两个教训（这一版两条都不许重犯）

`docs/STAGE3_PENDING_DESIGNS.md` §① 与 `docs/PENDING.md` 记着：

* **① 常驻 system 里不许放行为指导。** 上一版把 `UNSURE_ASK_RULE` **无条件**拼进她的
  常驻 system，system 从 7061 → 7378 字，多出来的正是那段"他说的是一件**具体的事**——
  某个说法、某个行当里的规矩、某个数字……"。**措辞会被角色吸收**（AGENTS §2.2），
  她的语域被带成了技术顾问，用户报"她的回复风格变得很奇怪、很 ai"。
  → 现在：`UNSURE_ASK_RULE` **不在 system 里**（`SYSTEM_PROMPT` 与面板内置默认都不带它；
  守卫在 `tests/test_ask_when_unsure.py` 与 `tests/test_persona_shape.py`）。
  这一版唯一写进她 prompt 的那句话（`NO_ASSERT_NOTE`）**只在她被判定打回的那一轮**
  出现在回复请求的**易变段末尾**（`stage3_main._revision_request`），只此一次、口语化、
  不点机制词；**确定性拦的那一层（`decide_grounding` / `has_specific_claim`）
  一个字都不进她的 prompt**。
* **② 只做"提示"，模型不听就没有兜底。** → 真的打回重写由核心决定
  （`stage3_main._model_path` 里"她这一版有没有具体断言"那一关），不靠她自己自觉。

## 边界

- **不新增模型调用**：`understood` / `specialist` 由判定那趟顺带给出；打回重写复用
  已经存在的那一次重写路径，**最多一次**。
- **不碰插件**：把 `plugins/` 拔掉，这一份照跑（它只依赖判定结果与眼前的消息）。
- **"问"这条路已经拆掉**：`ASK_TURN_NOTE` / `HOLD_TURN_NOTE` 两份旧措辞留在文件里当
  **历史与文字来源**（它们仍要过机制词扫描），但**没有任何调用点**，也不再进她的 prompt。
  `AskBudget` 同理：留着是因为它仍然可用，但现在**没有消费者**（见那个类的说明）。
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

#: 旧版（2026-10-04"不懂就问"）那份**常驻**规矩的完整说法。
#:
#: ⚠️ **它不再进 system**，也**不再进任何一轮的 prompt**——留着只是因为它是下面两条
#: 现场提示的文字来源，而且测试拿它扫机制词、钉三条禁令（复述当懂了 / 说"我知道" /
#: 编细节）。改这里时要跟着改 `NO_ASSERT_NOTE`（那才是现在真的会递到她眼前的句子）。
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

#: 旧版"以问回应"的现场提示。**没有调用点**（2026-10-05 晚起"问"这条路被否掉了），
#: 留着只作历史与文字来源；测试仍然扫它有没有机制词。
ASK_TURN_NOTE = """

--- 这一句你没跟上 ---
他说的是件具体的事，你没把握。**别急着答**：先问他一句，让他把这件事说明白
（一句话就够，别问成一串，也别装懂、别编细节）。
"""

#: 旧版"问的次数用完了"的现场提示。**没有调用点**，同上。
HOLD_TURN_NOTE = """

--- 这一句你没跟上 ---
他说的是件具体的事，你没把握，而同一件事问过一次就够了。**别装懂、别下结论、别编细节**：
只说你自己的那点感受，或者把话头留给他。
"""

#: **这一版唯一会递到她眼前的那句话**（2026-10-05 晚加）。
#:
#: 它出现在哪：`stage3_main._revision_request(..., note=...)` —— 也就是她被判定打回、
#: 要在**同一轮**里重写一次的时候，作为一条 user 消息接在既有请求末尾。
#: 所以它**不常驻**（system 段一个字不加），也**只此一次**（重写最多一趟）。
#:
#: 措辞的三条要求（用户 2026-10-05 原话）：
#: * **别断言你不知道的**；
#: * "说你能说的，或者说这个你不清楚"——把"不知道"说出来**是允许的**；
#: * **不是**"问她一句"——所以这里只说"不知道就说不知道"，不出现"问"这个动作；
#:   被叫到的时候她**必须开口**，抱怨式反问不算交代，这一点由这句的"别绕过去"钉住。
NO_ASSERT_NOTE = """
--- 刚那一句，重说一次 ---
这一句里你说到了具体的数字、型号、步骤或者行当里的说法，而那些你手里没有一句可靠的话。
**别断言你不知道的**：能确定的部分照说，不能确定的就直说这个你不清楚——
把"不知道"说出来不丢人，比编一个像样的答案强。
**别拿反问绕过去**：他不是要你反过来问他一串，是要你给一句实话。
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
    """"问"的限量：**同一话题最多一次**、**一段时间内最多几次**。

    ⚠️ **2026-10-05 晚起它没有消费者了。** 它的来历是旧版"不懂就问"——怕她每句都问。
    现在那条路换成"打回重写一次"（重写本身就是一次上限，不存在"每句都问"），
    所以引擎不再问它。**留着不删**是因为它仍然是个可用的工具类、测试也在钉它的语义；
    但它出现在这里**不代表**"问"这条路还在：真正的行为由 `decide_grounding` 与
    `stage3_main._model_path` 决定，那两处都不碰它。

    状态只在内存里，重启清零——它防的是"她每句都问"，不是配额账本，
    丢了不影响安全。时间由调用方传（引擎传它自己那口钟），所以测试不必等真实的十分钟。
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
#: 那正好放过了要防的"甩一个名词过来，她顺着编"。
_EXPLAINING = re.compile(
    r"(?:意思|含意|含义|指的是|所谓|也就是)|就是[^。！？\n]{0,12}(?:的意思|意思)"
)


def context_is_explicit(message: "IncomingMessage", visible) -> bool:
    """「语境明确」的**确定性**判据（三种根据里的第三种）。

    只认两条线索，**判不准就往"不明确"那一边倒**（判不准就当没有根据，不许硬答）：

    1. 这一条**挂在眼前某一句上**（引用/回复了这一段里看得见的那条）——
       被挂的那句就是它的来龙去脉；
    2. 这一条自己**正在解释这件事**（"……的意思是……""指的是……""也就是……"），
       也就是对方把话说开了，而不是只甩一个名词过来。

    **它不是"她答得对不对"的判据**，只是"眼前有没有可依的前文"。
    说得更直白一点：这条判据很粗，粗的那一侧是安全的（多打回一次），
    而不是"多编一句"。
    """

    quoted = str(getattr(message, "reply_to_message_id", "") or "")
    if quoted:
        for other in visible or ():
            if getattr(other, "message_id", "") == quoted:
                return True
    return bool(_EXPLAINING.search(str(getattr(message, "text", "") or "")))


# --- 「话里有没有具体断言」（确定性，治"装懂"的那一道）---------------------
#
# 看的是**她自己写出来的那一版**（草稿），不是判定的结论：判定只给"这一句是具体话题"，
# "她答的时候有没有写死一个数字/型号/流程"要靠这一段。
#
# 刻意收窄（宁可少打回，也别把正常聊天掐死）：
# * 只认**数字**（阿拉伯或汉字）、**型号/编号**（拉丁字母+数字）、**流程词**；
# * 一句里已经有"不清楚/不确定/不知道/我不懂/没听说"这类**承认不知道**的写法，
#   或者整句是**反问/疑问**（她没在断言，是在问），就不算断言；
# * 去掉空白与全角空格再扫（"1 5 号"这种写法照样算数字）。

#: 具体断言：数字（含量词写法）、型号编号、流程词。
_SPECIFIC_CLAIM = re.compile(
    r"""
    \d{1,}\s*(?:号|点|分|块|元|楼|期|届|版|项|人|次|个|天|周|月|年|小时|分钟|%|％)  # 数字+量词
    |\d{1,}                                                                          # 光一个数字
    |[A-Za-z]{1,8}[-_ ]?\d{2,}                                                       # 型号/编号
    |[第]\s*[一二三四五六七八九十百千0-9]{1,3}\s*(?:步|条|项|阶段|轮|批)               # 第 N 步/条
    |(?:先|然后|接着|再|最后)\s*(?:去|把|点|打开|填|交|提交|登录|注册|报名|申请|下载|上传|扫码|截图)
    """,
    re.VERBOSE,
)

#: "她自己承认不知道"的写法。句子里有它 → 不算断言。
_HEDGED = re.compile(
    r"不清楚|不确定|不知道|不懂|不明白|没把握|说不准|记不清|没有印象|不记得"
    r"|没听说过|无法确定|说不上|不太熟|不熟"
)

#: 疑问/反问：整句在问她自己的疑问，不是在断言。
_QUESTION = re.compile(r"[?？]|是不是|有没有|难道|该不会|你(?:说|觉得)")


def has_specific_claim(text: object) -> bool:
    """这一版话里有没有**具体的断言**（数字 / 型号 / 流程 / 术语）。

    只回答"她有没有写死一个具体的东西"，**不回答**"她说得对不对"——对不对没人能在这里判。
    判据全在正文上，确定性，模型"觉得自己懂"绕不过它。

    判错的那一侧是**少打回一次**（放行一句可能有问题的），不是"凭空打回正常聊天"：
    没有数字、型号、流程词的话一律 False。
    """

    body = str(text or "").strip()
    if not body:
        return False
    # 空行分段：一句里承认不知道，不该把整版都算成断言。
    for sentence in re.split(r"[\n。！!；;]+", body):
        flat = re.sub(r"\s+", "", sentence)
        if not flat:
            continue
        if _HEDGED.search(flat):
            continue
        if _QUESTION.search(flat):
            continue
        if _SPECIFIC_CLAIM.search(flat):
            return True
    return False


# --- 确定性结论（纯函数）----------------------------------------------------


@dataclass(frozen=True, slots=True)
class GroundingDecision:
    """这一轮的确定性结论：**要不要出声**，以及**要不要把草稿打回重写一次**。"""

    #: 这一轮要不要出声。False = 连判定说的 REPLY 也不接（没被叫到、又没懂的具体话题）。
    speak: bool
    #: 出声之后还要不要再看一眼草稿：被叫到、又没把握/没根据 → 她一断言就重写一次。
    rewrite: bool
    #: 日志用的原因（固定几种说法，不带正文）。
    reason: str
    unclear: bool = False
    specialist: bool = False
    grounded: bool = False


def decide_grounding(
    verdict: "JudgeVerdict",
    *,
    called: bool,
    has_knowledge: bool,
    has_memory: bool,
    context_explicit: bool,
) -> GroundingDecision:
    """把判定给的两个信号 + 三样根据合成一个**确定性**的结论。

    规矩只有两条（用户 2026-10-05 的口径：*"没根据的时候，要么不出声，要么别断言"*）：

    1. **没被叫到 + 没懂 + 具体话题** → **不插话**（`speak=False`）——
       不出声，**不是**"问一句"；
    2. **被叫到** → 照常开口（`speak=True`）；**没有任何一处根据**（知识库没命中、
       记忆没命中、语境也不明确）时，再看一眼她的草稿：有具体断言就重写一次
       （`rewrite=True`）。
    3. **有根据** → 照旧，`care` 那一层**不生效**（正式验收第 4 条：
       "有根据（知识库/记忆/语境命中）→ 不触发任何拦截"）——手里有材料就该照说。

    `called` 传"这一次是不是在跟她说话"（@ 她、点名她、直接问她、答应过人家"稍等"）。
    **被叫到时她不冷落人这条原意保留**：这里不允许她沉默——唯一的沉默是"没被叫到"。
    """

    unclear = bool(getattr(verdict, "unsure", False))
    specialist = bool(getattr(verdict, "specialist", False))
    # 「三处都没有根据」＝知识库没命中、记忆没命中、语境也不明确。
    grounded = bool(has_knowledge or has_memory or context_explicit)
    #: 要盯的那两种情形：没懂，或者具体话题。**有根据时一律不盯**（照旧说话）——
    #: 手里有材料就该照说，不能因为判定说她"没懂"就把一句话打回重写。
    care = (unclear or specialist) and not grounded
    if not care:
        return GroundingDecision(speak=True, rewrite=False, reason="照旧",
                                 unclear=unclear, specialist=specialist, grounded=grounded)
    if not called:
        return GroundingDecision(
            speak=False, rewrite=False,
            reason="没听懂、也没被叫到" if unclear else "没有根据、也没被叫到",
            unclear=unclear, specialist=specialist, grounded=grounded)
    return GroundingDecision(speak=True, rewrite=True, reason="被叫到，但没把握也没根据",
                             unclear=unclear, specialist=specialist, grounded=grounded)
