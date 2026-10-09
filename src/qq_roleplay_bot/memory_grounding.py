"""写入前的**依据闸门**：这条事实站不住就不写（fail-closed，不问模型）。

由来（2026-10-09 用户实测，面板「人物画像 → 零散记录」）：

    `participating_in_diansai`（`user_global/2639157559`，kind=status）被写成
    "在参与电赛…他也在其中；被别人催着去准备"。用户原话："**我没在打电赛**，
    我说要参加的是**雏雁和大创**"。

翻真库 + 真日志（`run/data/logs/memory.jsonl`）核对，这条的三个来源是：

| 那一步 | 真实证据 | 结果 |
| --- | --- | --- |
| `2026-10-04 22:24:06` | 他本人："笨蛋，**大家只是在讨论**电赛" | 记成"**他在参与**电赛" |
| `2026-10-04 22:24:34` | 他本人："那你咋还**催别人**去准备电赛" | 记成"**被别人催着**去准备" |
| `2026-10-06 14:31:23` | 他本人："已换电池"（**手机**） | 记成"**笔记本**已换了新电池…应已处理" |

第一条的"电赛"三个字**不是他先说出来的**：群里最早是 `242003347` 在
`2026-10-04 21:41:45` 说的"带我打电赛"（同一分钟内还有 `1647182422`、
`2595055768`"电子院的电赛预赛"、`2843418910`），他只是在**反驳**。

所以写入路径上缺的不是"总结能力"，而是**两道机械闸**：

1. **归属**：这条事实的依据必须是**他本人说的那句话**。引用别人的话当依据、
   或者一条依据都不给就改正文 → 不写（`rejected_other_speaker` /
   `rejected_no_evidence`）。归属在 `memory_store.commit` 里一直是
   `batch.user_id`（那一批的说话人），**内容说的是谁从来没有被检查过**。
2. **自述**（**处境类 `status`**）：依据里必须有**第一人称自述**（"我/咱/俺/自己/本人"），
   或者正文就是**照抄**他那一句（词元重合 ≥ `QUOTE_RATIO`）。"大家只是在讨论X"、
   "已换电池"、"绷不住"这类**没有第一人称**的话，就不再能变成他名下的事实
   （`rejected_not_self_statement`）。这正是用户要的方向："拿不准 → 不记"。
   这道闸只对 `status` 生效（见 `SELF_STATEMENT_KINDS` 的说明）：称呼/偏好/边界
   另有既有的闸门，而真库里报上来的三条全是处境类。归属那道闸（第 1 条）对所有
   "关于一个人"的事实类都生效。

另外两道窄闸：

3. **数字与型号不许凭空补全**（`rejected_ungrounded_specifics`）：正文里出现的
   数字/拉丁词（`5050`、`4060`、`12450H`、`i5-12490F` 这类）必须在**这一批看到过
   的材料**里出现过。真库里 `planning_gpu_purchase` 引用的原话是"我来商场看手机的"，
   正文却写着"5050/4060/7700 之间选"——那是从别处搬来的具体记号。
4. **空正文不许入库**（`rejected_empty_content`）：防御性的一道，正常路径上
   `memory_model.parse_operations` 已经拦掉了空串（真库里的空正文行是**删除路径**
   故意清空的，见 `memory_store.commit` 的 DELETE 分支与 `_cleanup`）。

**为什么不做更宽的"正文必须能在原话里找到"**（我量过，别再来一遍）：
拿真库 + 真日志的 505 次写入算正文对证据的词元覆盖率，中位数只有 0.30
（`UPDATE` 0.13）；按"覆盖率 ≥0.6 才许写"来判会拒掉约 **89%** 的历史写入——
维护 agent 的本职就是概括，词元覆盖率不是"有没有依据"的判据。
（`data/` 下的校准脚本与逐条清单不随代码提交；数字见提交说明与
`tests/test_memory_grounding.py` 的夹具。）

**汉字专名（"雏雁/大创"被写成"电赛"）机械上判不出来**：没有词典就不能区分
"电赛"是专名还是普通词。我量过两条替代判据都不成立——按"正文词元必须都能在
证据里找到"会拒掉 89% 的真实写入；按"正文里出现了同群**别人**记忆里的词就拒"
会命中 47% 的真实写入（`喜欢`/`自称`/`有人`/`最近` 这类通用词也算）。所以
专名那一条落在维护 prompt 的规矩里（见 `memory_maintenance_agent.py`），
机械侧只能做到上面第 3 条（数字与型号）。
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # 只为类型标注：这一层不需要认识存储或模型契约
    from .memory_model import InboxEvent, MemoryOperation

# --- 判据 -------------------------------------------------------------------
#: 第一人称标记：他说"我/咱/俺/自己/本人"，才算在说**自己的**事。
FIRST_PERSON_MARKERS = ("我", "咱", "俺", "自己", "本人")
#: 正文与依据的词元重合到这个比例，就算"照抄他那句话"（豁免第一人称这一条）。
QUOTE_RATIO = 0.6
#: "一个人一条"的事实类。`group` 是群公共事实，不属于任何人，不走归属闸。
PERSONAL_KINDS = frozenset({"name", "preference", "boundary", "status", "profile"})
#: **自述那道闸只对处境类**（`status`）生效。理由：处境只凭一句话就写下来、
#: 下一句就会被讲出来（她会在下一轮问"电赛准备得怎么样"），错记的代价最直接；
#: 而称呼/偏好/边界另有既有的闸门（近重复、系统自身过滤、称呼的证据要求）。
#: 真库里用户报上来的三条全是 `status`。
SELF_STATEMENT_KINDS = frozenset({"status"})
#: 具体记号 = **含数字的记号**（型号 / 编号 / 版本 / 容量：`5050`、`12450H`、`i5-12490F`、
#: `17T`、`1.9%`）。只认含数字的：纯拉丁词（`Minecraft`/`AMD`/`Overcooked`/`Windows`）
#: 是名字，概括时本来就会被换写法（真库里"MC JE"写成"Minecraft Java 版"、
#: "《胡闹厨房》"写 `Overcooked`），把它们也算成"凭空补全"会误杀约 15% 的活跃记录
#: （165 条里 25 条，实测）。
SPECIFIC_PATTERN = re.compile(r"[A-Za-z0-9_.+-]*\d[A-Za-z0-9_.+-]*")
#: 日期与年份不算"具体记号"：正文里的"2026-10 上旬""前后"是**什么时候说的**的标注，
#: 本来就该由写的人自己算（维护 prompt 允许这种标注）。把它当成"凭空补全"会误杀——
#: 拿真库 + 真日志的 165 条活跃记录回放，去掉日期豁免后多拦 26 条（16%），
#: 被拦的理由全是"2026-10 不在原话里"。
DATE_PATTERN = re.compile(r"\d{4}\s*[-/年]\s*\d{1,2}(\s*[-/月]\s*\d{1,2}\s*日?)?|\d{4}\s*年")

# 单字与高频虚词命中没有区分度。**与检索共用同一份**（`memory_store` 从这里 import，
# 别抄第二份：两份实现一定会漂移）。
STOPWORDS = frozenset({
    "的", "了", "是", "在", "和", "与", "我", "你", "他", "她", "它", "们",
    "这", "那", "有", "没", "不", "吗", "呢", "吧", "啊", "呀", "就", "也",
    "都", "很", "会", "要", "把", "被", "给", "对", "为", "以", "及", "或",
    "一个", "什么", "怎么", "可以", "自己", "这个", "那个", "我们", "你们",
})

#: 拒绝码 → 人话（审计与 `/super status` 用；日志里只出现码，不含正文）。
REJECTION_LABELS = {
    "rejected_empty_content": "正文为空",
    "rejected_other_speaker": "依据是别人说的话",
    "rejected_no_evidence": "没有依据就改正文",
    "rejected_not_self_statement": "不是他本人在自述",
    "rejected_ungrounded_specifics": "具体记号不在原话里",
}


def query_terms(query: str) -> frozenset[str]:
    """把一句话切成可用于子串命中的词元；忽略单字噪声与常见虚词。

    与检索（`MemoryStore.retrieve`）共用：检索按词命中、闸门按词比覆盖率，
    口径必须一致。
    """

    text = (query or "").casefold()[:1000]
    terms = {
        token
        for token in re.findall(r"[a-z0-9_]+", text)
        if len(token) >= 2
    }
    for run in re.findall(r"[\u4e00-\u9fff]+", text):
        if len(run) == 1:
            continue
        terms.update(run[index:index + 2] for index in range(len(run) - 1))
        terms.add(run)
    return frozenset(term for term in terms if term not in STOPWORDS)


def has_first_person(text: str) -> bool:
    """这句话里有没有第一人称标记（"我/咱/俺/自己/本人"）。"""

    return any(marker in (text or "") for marker in FIRST_PERSON_MARKERS)


def coverage(content: str, ground: str) -> float:
    """正文的词元有多大比例能在 `ground` 里找到（0..1；正文没有词元时算 1.0）。"""

    wanted = query_terms(content)
    if not wanted:
        return 1.0
    found = query_terms(ground)
    return len(wanted & found) / len(wanted)


def invented_specifics(content: str, ground: str) -> tuple[str, ...]:
    """正文里出现、而这一批材料里**没出现过**的数字/拉丁词（日期与年份不算）。"""

    haystack = (ground or "").casefold()
    text = DATE_PATTERN.sub(" ", content or "")
    return tuple(dict.fromkeys(
        token for token in SPECIFIC_PATTERN.findall(text)
        if token.casefold() not in haystack
    ))


def grounding_rejection(op: "MemoryOperation", *, cited: tuple["InboxEvent", ...], ground: str,
                        target_contents: tuple[str, ...] = (),
                        foreign_speakers: tuple[str, ...] = ()) -> str:
    """这条事实站不站得住。返回拒绝码，或空串表示放行。

    - `cited`：`op.evidence_event_ids` 真正解出来的事件（顺序按 op 里的顺序）。
    - `ground`：这一批**看到过**的材料全文（引用到的原话 + 目标记录正文 +
      本批其它事件），只用来做"数字/型号不许凭空补全"。
    - `target_contents`：`op.targets` 解出来的目标记录正文，用来判断"正文有没有被改"。
    - `foreign_speakers`：被引用的发言里**不属于归属人**的那些号。归属人自己的其他号
      （`person_aliases`）由调用方先按规范号算好，所以到了这里只剩"真是别人"的号。
    """

    content = op.content or ""
    # 4. 空正文不许入库（正常路径上 parse_operations 已经拦掉；这里是存储层兜底）。
    if not content.strip():
        return "rejected_empty_content"

    personal = op.kind in PERSONAL_KINDS and op.scope_type != "group"
    if personal:
        # 1. 归属：依据必须是**他本人**说的；引用别人的话当自己的依据 → 不记。
        if foreign_speakers:
            return "rejected_other_speaker"
        spoken = tuple(e for e in cited if e.speaker == "user")
        if not spoken:
            # 没有本人发言：只放行"正文一个字没改"的复查（周期复查常常只确认一下），
            # 其余（拿旧记录当依据改出新正文）一律不记——真库那条 `participating_in_diansai`
            # 的 rev3 就是 evidence=[] 改写出来的（`memory.jsonl` line=1489）。
            if (op.op == "UPDATE" and len(target_contents) == 1
                    and content.strip() == target_contents[0].strip()):
                return ""
            return "rejected_no_evidence"
        # 2. 自述：处境类必须是他本人在自述（第一人称），或者正文就是照抄他那句话。
        if op.kind in SELF_STATEMENT_KINDS and not (
                any(has_first_person(e.text) for e in spoken)
                or coverage(content, " ".join(e.text for e in spoken)) >= QUOTE_RATIO):
            return "rejected_not_self_statement"

    # 3. 数字与型号不许凭空补全。
    if invented_specifics(content, ground):
        return "rejected_ungrounded_specifics"
    return ""
