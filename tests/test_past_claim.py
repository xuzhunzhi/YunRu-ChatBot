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

1. 判据本身：四种形状命中、"没玩过这个"这类**承认不知道**的说法不许命中；
2. 核对链：材料里有 → 放行；材料里没有 → 核不过并报出"哪一句、缺哪一段"；
   **她自己的上一句不能当依据**（不排除的话，重复上一句就能"通过"，那正是自证循环，
   也是 15:41→15:42 越编越具体那个放大器的一半）。

⚠️ 这一份**只测函数**：接线（`stage3_main._model_path` 里那一关）在下一段。
"""
import unittest

from qq_roleplay_bot.ask_when_unsure import (
    PAST_CLAIM_MIN_CHUNK,
    PAST_CLAIM_NOTE,
    check_past_claim,
    has_past_claim,
    material_haystack,
)
from qq_roleplay_bot.prompt_guard import scan_persona_text

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

#: **不许**命中的：承认不知道（否定）、纯粹的建议、没有主语的日常句子。
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


def test_known_gap_subject_elided_claims_are_not_caught() -> None:
    """⚠️ **已知缺口，不是设计意图**：真实编造里有一半**没有主语**，这四种形状抓不到。

    这两条都是从 `run/data/logs/reply.jsonl` 里原样抄的（实测，2026-10-18）：
    「椅子倒是拆过几张」正是这次交底举的那一例，而它没有"我"，所以不命中；
    「吃过，不然我哪敢跟你说」也是——"吃过"前面没有主语。

    留这条测试是为了**让缺口可见**：以后谁把判据扩到无主语的写法，这里会红，
    于是必须回来读这段说明、按交底/用户的话决定要不要扩。**别把它当成"这样挺好"。**
    """

    for text in ("椅子倒是拆过几张，两百多那种坐垫真的撑不住", "吃过，不然我哪敢跟你说"):
        assert has_past_claim(text) is False, text


def test_mentioning_a_moment_without_past_tense_is_not_enough() -> None:
    """只有"那时候/以前"这类词、但说的不是**她的**经历时，不该被这条判据盯上。"""

    assert has_past_claim("那时候天还没黑，你们先吃") is False
    assert has_past_claim("以前的事，想说的时候再说吧") is False


if __name__ == "__main__":  # pragma: no cover - 手动跑
    unittest.main()
