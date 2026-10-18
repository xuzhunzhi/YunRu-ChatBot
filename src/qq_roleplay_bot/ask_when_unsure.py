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
4. **"她把过往说成事实"那一关**（2026-10-18 加，**纯正则 + 纯字符串，0 次模型调用**）：
   `has_past_claim` 认**五种**形状（`我……过` / `我那时候` / `记不清哪一年` / `V过就是V过` /
   **无主语 + 经历性动词 + 过 + 数量词**，最后这一种是 2026-10-18 用户裁定补的，
   原话见 `_subject_elided_claim` 的说明），
   `check_past_claim` 拿**这一轮的材料全文**（`material_haystack`，**排掉
   `speaker="yunru"` 那些行**——她自己的话不算依据）去比最长的那段中文片段。
   核不过 → 同一个重写通道、同一句"换成不确定的说法"（`PAST_CLAIM_NOTE`）。
   为什么它必须与第 3 条并列、而不是收窄第 3 条：第 3 条只认数字 / 型号，实测对她
   **8 条编造原话 8/8 返回 False**；而她编的是"拆过几张椅子"这种**日常细节**。

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
  不点机制词；**确定性拦的那一层（`decide_grounding` / `has_specific_claim` /
  `check_past_claim`）一个字都不进她的 prompt**——只在打回的那一轮，由
  `NO_ASSERT_NOTE` / `PAST_CLAIM_NOTE` 这种**已经写成交谈视角**的话替它说话。
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

#: 「她把自己的过往说成事实」那一趟打回时递的话（2026-10-18 加）。
#:
#: 它出现在哪：`stage3_main._model_path` 里"过往断言核不过"那一关，走的是既有那条
#: 重写通道（`_rewrite_after_review`），作为一条 user 消息接在既有请求末尾。
#: 所以它**不常驻**（system 段一个字不加），也**只此一次**（重写最多一趟）。
#:
#: 三条要求都来自交底：
#: * **只许要求"换成不确定的说法"**——不许要求她删掉整句（删掉等于让她闭嘴，
#:   而"没被叫到时可以不出声、被叫到时必须开口"那条口径在 `decide_grounding` 里）；
#: * 所以这里明说"意思照旧"，并且给出三个她自己会用的退路（记不清了／好像有过／说不太准）；
#: * 措辞不许点机制词（检查/触发/调用/协议/提示词/上下文/记忆库/Stage），也不出现
#:   "材料""依据"这类我们这边的说法——她会捡去用。
PAST_CLAIM_NOTE = """
--- 刚那一句，重来一次 ---
这一句你把一件旧事说成了真发生过的。可这件事，眼前这些字里没有一处对得上——
你手里没有那一句。
**别把它当成事实说**：意思照旧，换成没那么确定的说法（"记不清了""好像有过"
"说不太准"）；实在说不出来，就说这件你说不出来。
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


# --- 「她把自己的过往说成事实」（确定性，2026-10-18 加）----------------------
#
# **与上面那条并列，不动 `_SPECIFIC_CLAIM`**：那条认数字/型号，这条认"经历"。
#
# 由来（用户 2026-10-18 交底，都是实测）：最近 2 天里"断言自身过往"的 23 条有 **16 条
# 不可核实**（70%）；最硬的一例是「椅子倒是拆过几张」，而那一轮材料里「拆」出现 **0 次**。
# 人格里那条反编造（`base_prompt.REAL.py:168`）1507/1507 条 prompt 都在、**没挡住**：
# 它写的是"设定里没有的细节"，而她编的是**日常细节**（拆椅子、吃过什么、以前怎么弄的）。
# 上面那条 `_SPECIFIC_CLAIM` 只认数字/型号/流程，实测对她 **8 条编造原话 8/8 返回 False**。
#
# 所以这一条**不看判定的结论**（`decide_grounding` 那三重门槛让它在生产上 0 次生效：
# 判定说 CLEAR + 非专业就放行、记忆一命中就放行），只看**她这句话自己的形状**：
# 只要她把一段经历说成"事实"（"我……过"／"我当年……"／"记不清哪一年"／"V 过就是 V 过"／
# "拆过几张"这种**没有主语**的），就要求材料里**真的有那一段字**对得上。
#
# 代价是零：纯正则 + 子串包含，**0 次模型调用**（`decide_grounding` 那套也是零成本，
# 但那套在生产上从没触发过——根因是它在等判定点头，而判定对"日常细节"不觉得有事）。
#
# 判据刻意写窄（宁可漏、也不许把正常聊天掐死）：
# * 形状一到四只认**第一人称**（我／俺）；"没玩过这个"这种**没有主语**的否命题不碰。
#   **唯一**的例外是 2026-10-18 用户裁定补的形状五（没有主语 + 动词过 + 数量词），
#   它也只认那一种写法，其余无主语句子照旧不碰；
# * 主语与动词之间**不许夹否定词**（没／不／未／无）："我没拆过"是**承认不知道**，
#   那是要鼓励的说法，不能打回；
# * 经历性动词只认**交底里那一串**（见听吃去做拆修买玩走看读写过用），
#   不认识的动词（焊过、拧过）不在此列——漏了就是漏了，别把判据扩成"凡是过都算"。
_EXPERIENCE_VERBS = "见听吃去做拆修买玩走看读写过用"

#: 形状一：`我/俺 + …… + <经历性动词>过`。主语与动词之间**排除**否定词
#: （没／不／未／无），于是"我从没拆过""我从未拆过"这类不会命中——它们是**在承认不知道**，
#: 那正是要鼓励的说法，不能反过来打回。
_PAST_ACTION = re.compile(
    rf"(?:我|俺)[^。！？!?；;\n没不未无]{{0,8}}?[{_EXPERIENCE_VERBS}]过"
)

#: 形状二：`我(那会儿|当年|以前|那时候|上回|那次)`——把一段经历挂在一个具体时段上。
#: 它本身不带"过"，但"我那时候……"最常见的用法就是把编出来的细节说成回忆。
_PAST_TIME = re.compile(r"(?:我|俺)(?:那会儿|当年|以前|那时候|上回|那次)")

#: 形状三：`记不清哪一年`——**这句本身就是"编了一个时间框架"**：她把"记不清"当护身符，
#: 后面接着说的仍然是一件被她当成发生过的事。所以它**不算** `_HEDGED` 那一类。
_PAST_VAGUE_TIME = re.compile(r"记不清(?:哪一年|哪年)")

#: 形状四：`V 过就是 V 过`——把一段编出来的经历说成"这事没得商量"。
_PAST_INSIST = re.compile(
    rf"[{_EXPERIENCE_VERBS}]过[^。！？!?；;\n]{{0,3}}就是[^。！？!?；;\n]{{0,3}}过"
)

#: 形状五（2026-10-18 **用户裁定补的**，原话："扩，只扩 V过+数量词 这一种形态"）。
#:
#: 由来：前四种都要求**有主语**（我／俺），而真实编造里有一半主语被省掉了。交底点名的
#: 那一例是「椅子倒是拆过几张」——没有"我"，前四种一个都不命中（上一段那条测试就是
#: 钉住这个缺口的）。
#:
#: 只扩这一种形态，多一个都不扩（不认"以前……"、不认没有数量词的"拆过"）：
#: * 数量词 = 数词（至少认 `一二三四五六七八九十几两`）+ 量词（`张个次回顿场位条`）。
#:   **刻意只认这一串**——多认一个量词就多一类误伤，而这一种只是想补"没有我"的那半边；
#: * 动词前面紧挨着否定词（没／不／未／无）的**不算**：与形状一同一口径，
#:   "没拆过几张"是在承认不知道，不是断言。
_QUANTITY = r"[一二三四五六七八九十几两]+[张个次回顿场位条]"

#: 形状五的本体：经历性动词 + 过 + 数量词（不带主语）。
_NO_SUBJECT_PAST = re.compile(rf"(?<![没不未无])[{_EXPERIENCE_VERBS}]过{_QUANTITY}")

#: 形状五的**锚**形态：动词就在句首，或者跟在逗号/空白后面（"拆过几张""行吧，去过几次"）。
#: 这一种天然没有主语，不用再看整句。
_NO_SUBJECT_ANCHORED = re.compile(rf"(?:^|[，,、：:\s])[{_EXPERIENCE_VERBS}]过{_QUANTITY}")

#: 一句里出现任何一个人称代词，就不算"无主语"（"他说他吃过两回"说的是别人，不是她）。
_SUBJECT_PRONOUNS = ("我", "俺", "你", "您", "他", "她", "它", "咱")


def _subject_elided_claim(sentence: str) -> bool:
    """这一句里有没有"没有主语"的那种过往断言（形状五，用户 2026-10-18 裁定的那一种）。

    两种都算：

    1. **动词就在句首、或逗号/空白后面**（"拆过几张""行吧，去过几次"）——最直白的无主语；
    2. **动词挂在别的东西后面，但整句一个人称代词都没有**（"椅子倒是拆过几张"）——
       交底点名的那一例正是这种：椅子是话题，主语（我）被省掉了。

    ⚠️ 为什么不是"只认第 1 种"：用户给的字面写法就是"句首或标点/换行之后"，可那**抓不到
    他自己举例的那一例**——「椅子倒是拆过几张」里"拆"前面是"是"，既不在句首也不在标点后。
    所以第 2 种是必须的：无主语的本义是"这句话不点主语"，不是"动词必须落在句首"。
    这一条是我按那条例子的实际形状补的，与"只扩 V过+数量词"的口径一致（形态没变，只是
    不再要求动词贴边）。

    判错的方向仍然是**多打回一次**（重写只要求换成不确定的说法，回复永远照发）。
    """

    if not _NO_SUBJECT_PAST.search(sentence):
        return False
    if _NO_SUBJECT_ANCHORED.search(sentence):
        return True
    return not any(word in sentence for word in _SUBJECT_PRONOUNS)


#: 她句子里的"一段中文片段"（连续汉字）。核对拿**最长的那一段**去材料里找。
_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")

#: 拿去做子串判断的最短长度。短于它就没法当"可指认的原话"——
#: 四个字的短语在材料里处处都能撞上，判了等于没判（交底：≥6 字）。
PAST_CLAIM_MIN_CHUNK = 6


def _past_claim_patterns_hit(sentence: str) -> bool:
    """这一句里有没有"把过往说成事实"的形状（五种，见上面各自的说明）。"""

    return bool(
        _PAST_ACTION.search(sentence)
        or _PAST_TIME.search(sentence)
        or _PAST_VAGUE_TIME.search(sentence)
        or _PAST_INSIST.search(sentence)
        or _subject_elided_claim(sentence)
    )


def _claim_sentences(body: str) -> list[str]:
    """按句读切开，只留"命中形状"的那些句子（顺序保持原样）。

    为什么要先切句：这五种形状都是**句子级**的（"我那时候"跟"过"分属两句时不是同一件事），
    而且日志要报"是哪一句"，所以切句这一步不能省。
    """

    out: list[str] = []
    for sentence in re.split(r"[\n。！!；;？?]+", body):
        flat = sentence.strip()
        if flat and _past_claim_patterns_hit(flat):
            out.append(flat)
    return out


def has_past_claim(text: object) -> bool:
    """她这一版话里有没有**把一段过往说成事实**（纯正则，零模型调用）。

    五种形状：`我/俺……<经历性动词>过`、`我(那会儿|当年|以前|那时候|上回|那次)`、
    `记不清哪一年`、`V 过就是 V 过`、以及**没有主语**的
    `<经历性动词>过 + 数量词`（"拆过几张""去过几次"；2026-10-18 用户裁定补的，
    边界见 `_subject_elided_claim`）。**主语后面跟着否定词的都不算**（"我没拆过"）。

    它只回答"这句话像不像在断言自己的过去"，**不回答**"她到底做过没有"——那由
    `check_past_claim` 拿材料去比（纯字符串）。判错的那一侧是**多打回一次**，
    不是"凭空把正常聊天掐死"：真正的动作只有"换成不确定的说法重说一遍"，
    重写后仍不合格**照发**（回复绝不丢）。
    """

    body = str(text or "").strip()
    if not body:
        return False
    return bool(_claim_sentences(body))


@dataclass(frozen=True, slots=True)
class PastClaimCheck:
    """一次"她这话有没有依据"的确定性结论（日志用，也是路由用的那个判据）。"""

    #: 有没有命中那四种形状之一。
    claim: bool = False
    #: 材料里对不对得上。`claim=False` 时恒为 True（"没有可核的事"）。
    grounded: bool = True
    #: 触发的那一句（**日志用**：要能看见"哪一句"）。
    sentence: str = ""
    #: 拿去比的那一段最长中文片段；判不过时它就是"材料里缺的那一段"。
    chunk: str = ""


def _longest_cjk_run(sentence: str) -> str:
    runs = _CJK_RUN.findall(sentence)
    return max(runs, key=len) if runs else ""


def check_past_claim(text: object, material: object) -> PastClaimCheck:
    """她这一版里"过往断言"有没有材料对得上（**纯字符串，0 次模型调用**）。

    做法（交底原话）：把这一轮的**材料全文**当 haystack，取她那一句里**最长的一段
    中文片段（≥ `PAST_CLAIM_MIN_CHUNK` 字）**做子串包含判断。找不到 → 这一条核不过。

    几条边界：

    * 最长片段短于 6 字 → **不核**（放行）。四个字以下在材料里处处撞得上，
      判了等于没判；这也是这一条**刻意留着的一个漏口**（她带上逗号写短句就绕过了），
      交底就是这么定的，别自己加花样；
    * 只要有一句核不过就返回`grounded=False` 并带上**那一句**与**缺的那一段**——
      日志要能看见这两样（"哪一句、材料里缺什么"）；
    * ⚠️ "缺的那一段"是**那一句里最长的中文片段**，**不保证就是断言那半句**：
      真例子「椅子我拆过，具身不具身我可不知道」报出来的是「具身不具身我可不知道」。
      为什么不改成"优先取含断言的片段"——那个片段只有 5 个字、低于门槛，等于放行，
      而它正是要抓的那一例。代价是**偶尔多打回一次**（断言在材料里、尾巴不在时），
      这个方向是安全的（重写只要求换成不确定的说法，**回复永远照发**）；
    * `material` 的取法有专门的口子：`material_haystack`，它**必须**先把
      `speaker="yunru"` 那些行整段摘掉（那是她自己说过的话，拿它当依据就是自证）。
    """

    body = str(text or "").strip()
    if not body:
        return PastClaimCheck()
    haystack = str(material or "")
    sentences = _claim_sentences(body)
    if not sentences:
        return PastClaimCheck()
    first = sentences[0]
    first_chunk = _longest_cjk_run(first)
    for sentence in sentences:
        chunk = _longest_cjk_run(sentence)
        if len(chunk) < PAST_CLAIM_MIN_CHUNK:
            continue  # 太短，核不了 → 放行
        if chunk in haystack:
            continue
        return PastClaimCheck(claim=True, grounded=False, sentence=sentence, chunk=chunk)
    return PastClaimCheck(claim=True, grounded=True, sentence=first, chunk=first_chunk)


#: `<message ... speaker="yunru" ...>……</message>` —— **她自己说过的话**。
#:
#: 为什么要整段摘掉（2026-10-18）：她上一轮说过的话已经进了短期历史，会**原样出现在
#: 这一轮的材料里**；重写那一轮又有"把她上一版递回去"的放大器（交底里点名要拆的那一处）。
#: 不摘掉的话，她只要把上一句原样再说一遍，"最长片段"就一定命中——那不是"材料里有依据"，
#: 那是**拿她自己的话证明她自己有依据**（自证循环，正是 15:41→15:42 越编越具体的推手）。
_OWN_MESSAGE = re.compile(r'<message\b[^>]*\bspeaker="yunru"[^>]*>.*?</message>', re.S)

#: 兜底：万一那段 `<message>` 没闭合（被截断），至少把**带那个标记的那些行**整行摘掉。
_OWN_MESSAGE_LINE = re.compile(r'^.*\bspeaker="yunru".*$', re.M)


def material_haystack(request: object) -> str:
    """把这一轮请求里的**材料全文**拼成 haystack，**排除她自己说过的话**。

    `request` 就是递给回复 agent 的那份 `list[dict[str, str]]`（`build_dialogue_messages`
    的产物），所以这里不另取数据：**她看到什么，就核什么**。

    **system 段（她的人格）也算在内**——这是**用户 2026-10-18 裁决**的（原话：*"system 段
    保留"*），理由写在下面第 0 条。**只有 `speaker="yunru"` 那两类行被摘掉**，别的都留着。

    **system 段算内的理由**（裁决的原话：*"人格里写着的就是她有资格讲的，排除掉会把
    合法自述全判成编造"*）：

    0. 人格是**她自己**的来历（"我在巴米扬待过""我造过百夫长"），那不是编造，是她有资格
       讲的东西。把它排掉，她每一句合法的自述都会核不过、每一句都被打回重写一次——
       那等于让这一关天天掐她的正常说话，代价比它拦下的还大。
       它也不违背 DATA 边界：这一份 haystack 只用来**核对**（拿她的正文去比材料里有没有
       那段字），**不回到任何 prompt 里去**——"可信段不能当材料"那条约束管的是"进
       prompt 的东西从哪来"，不是"核对时能看什么"。她看到什么，就核什么。

    排除分两道（顺序不能反）：
    1. 先整段摘掉 `<message ... speaker="yunru" ...>…</message>`——**她的正文可能带换行**，
       只按行摘会漏掉后面几行，等于把半句自证留了下来；
    2. 再把残留的、带 `speaker="yunru"` 的行整行摘掉（那段没闭合时只有这一道管用）。
    """

    parts: list[str] = []
    for item in request or ():
        if isinstance(item, dict):
            parts.append(str(item.get("content") or ""))
        else:  # 不是我们认识的那份请求：宁可当空材料，也别拿别的字段来凑
            continue
    text = "\n".join(parts)
    text = _OWN_MESSAGE.sub("", text)
    return _OWN_MESSAGE_LINE.sub("", text)


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
