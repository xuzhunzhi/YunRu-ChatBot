"""「她把过往说成事实」的那条判据（`ask_when_unsure.has_past_claim` + `check_past_claim`）。

由来（用户 2026-10-18 交底，全是实测数字）：

* 最近 2 天"断言自身过往"的 23 条里，**16 条不可核实（70%）**；最硬的一例是
  「椅子倒是拆过几张」，而那一轮输入里「拆」出现 **0 次**；
* 现有的 `_SPECIFIC_CLAIM`（数字/型号/流程）拿她的 **8 条编造原话去测，8/8 返回 False**；
* `decide_grounding` 那三重门槛（判定 CLEAR + 非专业就放行 / 记忆一命中就放行）
  让这道 grounding 在生产上**0 次触发**（同期风格审核打回 336 次）。

所以这一条**不看判定的结论**，只看她这句话自己的形状，然后拿**这一轮的材料全文**去比
（纯正则 + 子串，**0 次模型调用**）。

两件事在这一份里被钉住：

1. 判据本身：**五种**形状命中（2026-10-18 用户裁定补的第五种 = **无主语 + 经历性动词 +
   过 + 数量词**，见 `test_subject_elided_claims_with_a_quantity_are_now_caught`）；
   "没玩过这个"这类**承认不知道**的说法、没有数量词的无主语句子都不许命中；
2. 核对链：材料里有 → 放行；材料里没有 → 核不过并报出"哪一句、缺哪一段"；
   **她自己的上一句不能当依据**（不排除的话，重复上一句就能"通过"，那正是自证循环，
   也是 15:41→15:42 越编越具体那个放大器的一半）。

后半段是**接线**（2026-10-19，`stage3_main._model_path` 里那一关）的五条：
材料里有依据不打回 / 没有则打回一次且话术正确 / 她自己的上一句不能当依据 /
**因核对丢的回复 = 0** / 两条 WARNING。
"""
import asyncio
import unittest

from qq_roleplay_bot.ask_when_unsure import (
    PAST_CLAIM_MIN_CHUNK,
    PAST_CLAIM_NOTE,
    check_past_claim,
    has_past_claim,
    material_haystack,
)
from qq_roleplay_bot.prompt_guard import scan_persona_text
from qq_roleplay_bot.stage3_main import DialogueEngine
from qq_roleplay_bot.transport import IncomingMessage, MessageTarget

#: 她**真实说过**的编造（`run/data/logs/reply.jsonl` 里原样抄的，2026-10-18 那份日志，
#: 15:41→15:42 那两轮）。这两条是这次交底里点名的那一例。
REAL_FABRICATED = (
    "椅子是具身的，这点我拆过",
    "椅子我拆过，具身不具身我可不知道",
)

#: 另外 6 条**按同一批形状构造**的（交底里没给那 8 条的原文，只有"最硬的一例"）。
#: 构造的这 6 条覆盖交底点名的四种形状；报告里要说清哪 2 条是原话、哪 6 条是构造的。
CONSTRUCTED_FABRICATED = (
    "我拆过好几张椅子，那坐垫是真的撑不住",
    "我当年在那座设施里待过一阵子",
    "我那时候天天泡在车间里，哪有机会出门",
    "记不清哪一年了，反正那阵子手边的东西我都拆开看过",
    "拆过就是拆过，没什么好说的",
    "我吃过那种东西，便宜管饱",
)

#: **不许**命中的：承认不知道（否定）、纯粹的建议、没有数量词的无主语句子。
NOT_CLAIMS = (
    "没玩过这个",
    "买之前先看那四块盘",
    "我没拆过那种椅子",
    "我从没玩过这个",
    "我从未拆过椅子",
    "我看了一眼，还行",
    "没用过也不用特地买，头发不干就不缺这个",
    "这种事我真不清楚",
    "",
)

#: 形状五（**无主语 + 经历性动词 + 过 + 数量词**）的正面样本：用户 2026-10-18 给的例子
#: 原样抄在这里，外加交底点名的那一例（他自己举的就是「椅子倒是拆过几张」）。
NO_SUBJECT_CLAIMS = (
    "拆过几张",
    "去过几次",
    "吃过两回",
    "看过两场",
    "修过三个",
    # 交底点名的那一例（`run/data/logs/reply.jsonl` 原样）：
    "椅子倒是拆过几张",
    "椅子倒是拆过几张，两百多那种坐垫真的撑不住",
    # 动词落在逗号后面也是"无主语"：
    "行吧，去过几次",
)

#: 形状五**不许**命中的：没有数量词、动词前面有否定词、主语是别人、或者根本没有"过"。
NO_SUBJECT_NON_CLAIMS = (
    "吃过，不然我哪敢跟你说",       # 交底点名：没有数量词
    "没用过也不用特地买",            # 交底点名：没有数量词（前面还带否定）
    "没拆过几张",                    # 否定：那是承认不知道
    "他说他吃过两回",                # 主语是他（人称代词在，就不算"无主语"）
    "你吃过几回",                    # 问的是对方
)


def test_the_eight_fabricated_lines_all_hit() -> None:
    """她 8 条编造原话全部命中（2 条原话 + 6 条同形状构造，见上面两组的说明）。"""

    for text in REAL_FABRICATED + CONSTRUCTED_FABRICATED:
        assert has_past_claim(text) is True, text


def test_admitting_ignorance_is_not_a_claim() -> None:
    """**承认不知道不算断言**——"没玩过这个""买之前先看那四块盘"这类不许命中。

    否定词（没/不/未/无）夹在主语与动词之间时一律不算：那是要鼓励的说法，
    打回它等于教她别承认不知道。
    """

    for text in NOT_CLAIMS:
        assert has_past_claim(text) is False, text


def test_none_and_empty_are_safe() -> None:
    assert has_past_claim(None) is False
    assert check_past_claim(None, "").claim is False
    assert check_past_claim("", None).grounded is True


def test_material_that_carries_the_line_passes() -> None:
    """材料里真有那一段字 → 放行（不打回），也不报"缺什么"。"""

    draft = "椅子我拆过，坐垫早塌了"
    material = (
        '<message who="1" index="4"><text>那椅子我拆过，坐垫早塌了</text></message>\n'
        "--- 你记得的旧事 开始 ---\n"
        '<memory scope="user_group" subject="100" source="quote" age="3天前">'
        "他提过这椅子坐垫早塌了。</memory>"
    )
    verdict = check_past_claim(draft, material)
    assert verdict.claim is True
    assert verdict.grounded is True, verdict


def test_material_without_the_line_is_flagged_with_what_is_missing() -> None:
    """材料里没有那一段字 → 核不过，并且报得出**哪一句、缺哪一段**（日志要用）。"""

    draft = "我拆过好几张椅子，那坐垫是真的撑不住"
    material = '<message who="1" index="4"><text>今天好热</text></message>'
    verdict = check_past_claim(draft, material)
    assert verdict.claim is True
    assert verdict.grounded is False
    assert verdict.sentence == draft, verdict.sentence
    assert verdict.chunk not in material
    assert verdict.chunk == "那坐垫是真的撑不住", verdict.chunk
    # 注意：报出来的这一段是**这一句里最长的中文片段**，不是"断言那半句"。
    # 交底就是按"最长片段"定的（见 `check_past_claim` 的说明与下面那条用例）。
    assert "我拆过好几张椅子" not in verdict.chunk


def test_the_reported_chunk_is_the_longest_run_not_the_claim_clause() -> None:
    """⚠️ 已知的**精度代价**（交底定的算法，不是实现走样）：报出的"缺的那一段"可能**不是**
    断言那半句，而是同一句里更长的描述性尾巴。

    真实例子（`reply.jsonl` 原样）：「椅子我拆过，具身不具身我可不知道」——
    报出来的是「具身不具身我可不知道」。后果有两样：日志里"材料里缺什么"读起来对不上断言；
    以及**断言那半句其实在材料里、尾巴不在**时，这一关也会判核不过（多一次重写）。

    为什么不在实现里改成"优先取含断言的片段"：改了会漏掉真事——上面那条真例子里的
    断言片段只有「椅子我拆过」5 个字，**低于 6 字门槛**，优先取它就等于放行，而它正是
    交底点名要抓的那一例。宁可多打回一次，也不放过一句。
    """

    verdict = check_past_claim("椅子我拆过，具身不具身我可不知道", "椅子我拆过")
    assert verdict.grounded is False
    assert verdict.chunk == "具身不具身我可不知道", verdict.chunk


def test_her_own_previous_line_is_not_evidence() -> None:
    """**关键用例**：她上一句自己说过的话，不能当这一句的依据。

    不排除 `speaker="yunru"` 的行，她只要把上一句原样再说一遍，"最长片段"必然命中——
    那不是"材料里有"，那是**自证**。下面同时钉住"不排除就会通过"这件事，
    否则这条测试什么都没测（排除那一步要是被删掉，这里仍然绿）。
    """

    draft = "椅子是具身的，这点我拆过"
    request = [
        {"role": "system", "content": "你是云茹。"},
        {"role": "user", "content": (
            '<people>\n1=小K\n</people>\n'
            '<message who="1" index="1"><text>椅子结实吗</text></message>\n'
            f'<message speaker="yunru" index="2"><text>{draft}</text></message>\n'
        )},
    ]
    haystack = material_haystack(request)
    # 她自己的那一行整段摘掉了，别人的话还在。
    assert draft not in haystack
    assert "椅子结实吗" in haystack
    verdict = check_past_claim(draft, haystack)
    assert verdict.grounded is False, "她自己的上一句被当成依据了（自证循环）"
    # 反证：不排除的话，这句"通过"——所以排除那一步是**真的在起作用**。
    naive = "\n".join(str(item.get("content") or "") for item in request)
    assert check_past_claim(draft, naive).grounded is True


def test_her_own_message_with_a_line_break_is_removed_whole() -> None:
    """她的正文可能带换行：**整段**摘掉，不是只摘第一行。"""

    request = [{"role": "user", "content": (
        '<message speaker="yunru" index="2"><text>第一行\n第二行我拆过椅子</text></message>\n'
        '<message who="1" index="3"><text>别的话题</text></message>'
    )}]
    haystack = material_haystack(request)
    assert "第二行我拆过椅子" not in haystack
    assert "别的话题" in haystack


def test_a_chunk_shorter_than_the_floor_is_not_checked() -> None:
    """最长片段短于 6 字 → **不核**（放行）。这是交底里留的漏口，别自己加花样。"""

    assert PAST_CLAIM_MIN_CHUNK == 6
    verdict = check_past_claim("我拆过，行", "")
    assert verdict.claim is True
    assert verdict.grounded is True, "短片段不该被判成核不过"


def test_the_rewrite_wording_only_asks_for_a_hedge() -> None:
    """打回时递的话：**只要求换成不确定的说法**，不许要求删掉整句，也不许点机制词。

    ⚠️ 它现在**没有调用点**（接线在下一段）。这里先把它钉住，是因为它一旦写错，
    线上就是"她被迫不说话了"。
    """

    assert "意思照旧" in PAST_CLAIM_NOTE
    assert "记不清了" in PAST_CLAIM_NOTE
    assert "好像有过" in PAST_CLAIM_NOTE
    for word in ("删掉", "别提", "不用说"):
        assert word not in PAST_CLAIM_NOTE, f"话术里出现了\"{word}\"：它会让她整句不说"
    assert scan_persona_text(PAST_CLAIM_NOTE) == [], scan_persona_text(PAST_CLAIM_NOTE)


def test_subject_elided_claims_with_a_quantity_are_now_caught() -> None:
    """**用户 2026-10-18 裁定：扩，只扩 V过+数量词 这一种形态。**

    （原 `test_known_gap_subject_elided_claims_are_not_caught`——那一版钉的是"抓不到"，
    现在按裁定改成"抓得到"。）

    缺口原样：真实编造里有一半**没有主语**，前四种形状一个都不命中。交底点名的两例，
    都是从 `run/data/logs/reply.jsonl` 里原样抄的（实测，2026-10-18）：

    * 「椅子倒是拆过几张」——这次交底举的那一例，整句没有"我"，第一版不命中；
    * 「吃过，不然我哪敢跟你说」——"吃过"前面也没有主语。

    裁定只扩**一种形态**（原话：*"扩，只扩 V过+数量词 这一种形态"*）：

    * 「椅子倒是拆过几张」→ **现在命中**（无主语 + 经历性动词"拆" + 过 + 数量词"几张"）；
    * 「吃过，不然我哪敢跟你说」→ **仍然不命中**（没有数量词）。这条**必须**继续不命中：
      按裁定"只扩这一种形态"，它能命中就说明扩过头了。

    ⚠️ 用户给的字面写法是"句首或标点/换行之后"，可那只认第一种（"拆过几张"），抓不到
    他自己举的「椅子倒是拆过几张」（"拆"前面是"是"）。所以实现里多认了"整句没有人称
    代词"这一种（见 `_subject_elided_claim`）——形态没变，只是不再要求动词贴边。
    """

    # 1) 交底点名的那一例：现在抓得到。
    assert has_past_claim("椅子倒是拆过几张，两百多那种坐垫真的撑不住") is True
    # 2) 另一例**仍然**抓不到（没有数量词）——"只扩这一种形态"的那条边界。
    assert has_past_claim("吃过，不然我哪敢跟你说") is False


def test_the_fifth_shape_covers_the_five_examples_and_the_anchor() -> None:
    """形状五的正面样本：`拆过几张 / 去过几次 / 吃过两回 / 看过两场 / 修过三个` 全命中。"""

    for text in NO_SUBJECT_CLAIMS:
        assert has_past_claim(text) is True, text


def test_the_fifth_shape_does_not_fire_without_a_quantity_or_with_a_subject() -> None:
    """形状五的反例：**没有数量词**、动词前带否定、主语是别人——一个都不许命中。

    这几条正是"只扩一种形态"的护栏：多认出其中任何一条都说明扩过头了。
    另外「我看了一眼，还行」这类（没有"过"）由 `NOT_CLAIMS` 那一组钉住。
    """

    for text in NO_SUBJECT_NON_CLAIMS:
        assert has_past_claim(text) is False, text
    # 有第一人称的那句照旧由**形状一**抓到（不是形状五，但结果一样是"要核"）。
    assert has_past_claim("我看过两场，都是老片子") is True


def test_mentioning_a_moment_without_past_tense_is_not_enough() -> None:
    """只有"那时候/以前"这类词、但说的不是**她的**经历时，不该被这条判据盯上。"""

    assert has_past_claim("那时候天还没黑，你们先吃") is False
    assert has_past_claim("以前的事，想说的时候再说吧") is False


# --- 接线（`stage3_main._model_path` 里那一关，2026-10-19）-------------------
#
# 上面测的是判据（纯函数）；这一段测的是"这一关真的挂上去了没有"——五条：
# 材料里有依据不打回 / 没有则打回一次且话术正确 / 她自己的上一句不能当依据 /
# 因核对丢的回复 = 0 / 两条 WARNING。

GROUP = "717151356"
TARGET = MessageTarget(group_id=GROUP)


def _msg(index, text="测试", *, mentioned=False, user_id="100"):
    return IncomingMessage(
        message_id=f"m{index}", session_id=f"group:{GROUP}", user_id=user_id,
        text=text, target=TARGET, sender_name="某人", is_bot_mentioned=mentioned,
    )


class ScriptedJudge:
    """脚本化的判定：一次一条输出（最后一条一直重复）。"""

    def __init__(self, *outputs) -> None:
        self.outputs = list(outputs) or ["<route>REPLY</route><topic>话题</topic>"]
        self.requests: list = []

    async def complete(self, request):
        self.requests.append(request)
        index = min(len(self.requests) - 1, len(self.outputs) - 1)
        return self.outputs[index]


class ScriptedReply:
    """脚本化的回复 agent：一次一条输出（最后一条一直重复）。"""

    def __init__(self, *texts, prefix="<decision>REPLY</decision><dialogue>KEEP</dialogue>") -> None:
        self.texts = list(texts) or ["我接一句。"]
        self.prefix = prefix
        self.requests: list = []

    async def complete(self, request):
        self.requests.append(request)
        index = min(len(self.requests) - 1, len(self.texts) - 1)
        return f"{self.prefix}<reply>{self.texts[index]}</reply>"


class FailingRewriteReply(ScriptedReply):
    """第一版正常吐；**第二次**调用（重写那一趟）直接抛——模拟重写失败。"""

    async def complete(self, request):
        if self.requests:
            self.requests.append(request)
            raise RuntimeError("重写那一趟挂了")
        return await super().complete(request)


def _engine(reply, **flag_values) -> DialogueEngine:
    """装一台引擎，并把运行期开关**钉在这一台上**（默认全开）。

    ⚠️ 这里用实例级替身（`engine._flags = lambda: …`）而**不是** `runtime_flags.install`：
    `_flags()` 优先读进程里共用的那一份，别的测试装过的全局开关会让"跑单条"与"跑全量"
    结果不同（2026-10-08 记过这个坑）。`tests/test_style_reviewer.py` 用的是同一个写法。
    """

    from qq_roleplay_bot.runtime_flags import RuntimeFlags

    engine = DialogueEngine(
        reply, judge_client=ScriptedJudge(),
        # 群必须在启停名单里，否则 `handle` 在"群未启用"那一步就静默落下（实测）。
        enabled_group_ids=frozenset({GROUP}),
    )
    flags = RuntimeFlags(**flag_values)
    engine._flags = lambda: flags
    return engine


#: 她**真实说过**的一句编造（`reply.jsonl` 15:41→15:42 那两轮里的一版，原样）：
#: 用的是形状五（没有主语、有数量词），第一版判据抓不到，这一版抓得到。
FABRICATED = "椅子倒是拆过几张，那坐垫真的撑不住"


def test_a_grounded_past_line_is_let_through() -> None:
    """**材料里有那一段字 → 不打回**（有依据就该照说）。"""

    reply = ScriptedReply("椅子我拆过，那坐垫是真的撑不住")
    engine = _engine(reply)

    result = asyncio.run(engine.handle(
        _msg(1, "@YunRu 那把椅子我拆过，那坐垫是真的撑不住，你说呢", mentioned=True)))

    assert result is not None and result.text == "椅子我拆过，那坐垫是真的撑不住"
    assert len(reply.requests) == 1, "材料里明明有那一段字，却被打回了"
    snapshot = engine.snapshot()
    assert snapshot.reply_past_claim_hits == 1, "判据没命中（这一条就什么都没测）"
    assert snapshot.reply_past_claim_rewrites == 0
    assert snapshot.reply_past_claim_unfixed == 0


def test_an_ungrounded_past_line_is_pushed_back_once_with_the_right_wording() -> None:
    """**材料里没有 → 打回一次**，递的话是"换成不确定的说法"（不是"删掉"，也不是"问她"）。"""

    reply = ScriptedReply(FABRICATED, "记不清了，好像有过几张，说不太准。")
    engine = _engine(reply)

    result = asyncio.run(engine.handle(_msg(1, "@YunRu 你在忙什么", mentioned=True)))

    assert result is not None and result.text == "记不清了，好像有过几张，说不太准。", \
        "发出的是重写的那一版"
    assert len(reply.requests) == 2, "没有打回重写（或者重写了不止一次）"
    feedback = reply.requests[1][-1]["content"]
    assert "你刚写的那一句要重来" in feedback
    assert "意思照旧" in feedback, "话术要求换了说法，不是删掉"
    assert "换成没那么确定的说法" in feedback
    for word in ("删掉", "别说这句"):
        assert word not in feedback, f"话术里出现了\"{word}\"：它会让她整句不说"
    # **放大器已拆**：她那一版原话（连同这一句里最要命的那半段）一个字都不许贴回来。
    assert FABRICATED not in feedback
    assert "你刚写的是" not in feedback
    # 常驻 system 一个字都没变。
    assert reply.requests[1][0]["content"] == reply.requests[0][0]["content"]
    snapshot = engine.snapshot()
    assert snapshot.reply_past_claim_hits == 1
    assert snapshot.reply_past_claim_rewrites == 1
    assert snapshot.reply_past_claim_unfixed == 0


def test_her_own_previous_line_does_not_count_as_material() -> None:
    """**她自己的上一句不能当依据**（不排除就是自证循环，那是放大器的一半）。

    这里让她第一轮说一句别的话、第二轮才说那句编造：如果 `speaker="yunru"` 那些行
    没被摘掉，"最长片段"会命中**她自己**刚说过的话，于是这一关一声不响——
    这条用例就会红。
    """

    reply = ScriptedReply(
        "结实，坐垫还没塌。",          # 第一轮：不是断言
        "椅子我拆过，那坐垫是真的撑不住",  # 第二轮：编造（材料里只有她自己在说）
        "记不清了，说不准。",            # 重写那一版
    )
    engine = _engine(reply)

    assert asyncio.run(engine.handle(_msg(1, "@YunRu 椅子结实吗", mentioned=True))) is not None
    result = asyncio.run(engine.handle(_msg(2, "@YunRu 那你拆过椅子吗", mentioned=True)))

    assert len(reply.requests) == 3, "她上一句被当成依据了（这一关没触发）"
    assert result is not None and result.text == "记不清了，说不准。"
    snapshot = engine.snapshot()
    assert snapshot.reply_past_claim_rewrites == 1


def test_this_gate_never_drops_the_reply() -> None:
    """**因核对丢的回复 = 0**：两条失败路都不许把 `decision.text` 置空。

    与短码那一关**刻意相反**：那是安全维度（宁可少说一句），这一关是分寸问题
    （重写空手 → 发第一版；重写后仍核不过 → 发重写那一版，只记一笔）。
    """

    # ① 重写那一版**仍然**核不过 → 照发重写的那一版（不是第一版、更不是空）。
    reply = ScriptedReply(FABRICATED)          # 重写那一趟还是同一句
    engine = _engine(reply)
    result = asyncio.run(engine.handle(_msg(1, "@YunRu 你在忙什么", mentioned=True)))
    assert result is not None and result.text == FABRICATED
    snapshot = engine.snapshot()
    assert snapshot.reply_past_claim_rewrites == 1
    assert snapshot.reply_past_claim_unfixed == 1
    assert snapshot.replies == 1, "这一条必须真的发出去（不许被这一关丢掉）"

    # ② 重写那一趟**失败**（抛异常）→ 发第一版，绝不留空。
    reply = FailingRewriteReply(FABRICATED)
    engine = _engine(reply)
    result = asyncio.run(engine.handle(_msg(1, "@YunRu 你在忙什么", mentioned=True)))
    assert result is not None and result.text == FABRICATED
    assert engine.snapshot().replies == 1


def test_the_gate_warns_twice_when_the_rewrite_is_still_ungrounded() -> None:
    """**两条 WARNING**：打回一次、以及重写之后仍核不过（照发）——两件事都要看得见。"""

    reply = ScriptedReply(FABRICATED)
    engine = _engine(reply)

    case = unittest.TestCase()
    with case.assertLogs("qq_roleplay_bot.stage3_main", level="WARNING") as captured:
        asyncio.run(engine.handle(_msg(1, "@YunRu 你在忙什么", mentioned=True)))

    warnings = [record for record in captured.records
                if "past claim" in record.getMessage()]
    assert len(warnings) == 2, [record.getMessage() for record in warnings]
    assert "打回重写" in warnings[0].getMessage()
    assert "照发" in warnings[1].getMessage()


def test_the_gate_can_be_turned_off_by_the_same_switch() -> None:
    """面板关掉 `ask_when_unsure` → 这一条也不再生效（与那个开关的说明一致）。"""

    reply = ScriptedReply(FABRICATED)
    engine = _engine(reply, ask_when_unsure=False)

    result = asyncio.run(engine.handle(_msg(1, "@YunRu 你在忙什么", mentioned=True)))

    assert result is not None and result.text == FABRICATED
    assert len(reply.requests) == 1, "关掉之后不该再打回"
    assert engine.snapshot().reply_past_claim_rewrites == 0


if __name__ == "__main__":  # pragma: no cover - 手动跑
    unittest.main()
