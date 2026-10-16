# -*- coding: utf-8 -*-
"""材料里没有的不许写 + 每步一条日志 + 天数按日历算（2026-10-16 用户四条决定）。

用户原话（照抄四条里的第 1、2 条）：

    1. **素材：先甲案 + 判据**。只补结构（时间说人话、会话标号、引用链、`has_media`），
       **不喂别人的原话**；同时上你设计的"材料里没有的不许写"判据。
    2. **M2 粒度：只删"断言外部世界"的句子**——别人做了什么、他做了什么、发生过一件事、
       别人的反应（例如"有人笑别人没本事""你没跟着笑""我替人把话说平"）。**纯心情句留着**。

这一份钉的是**机制**（谁能删、删完还能不能叫同一封信、每一步有没有留档），
不假装能钉住"模型一定会把编的那句删掉"——那要靠拿真实 prompt 重放量数字
（见本文件最后两条：能力边界如实写出来，不粉饰）。

## 这一份为什么在**插件线**上（`check_plugin_branch_scope.py` 的口径）

判据是那句"**这个文件在本体树上跑得起来吗？**"——跑不起来：
它在模块顶层 import `qq_roleplay_bot.plugins.mail.letter_writer` / `daily_report`，
本体树上没有 `plugins/mail/`，在那边根本收集不起来（与 `test_mail_letter_agent.py`
同一个理由）。

**结构那一半（"她这段时间说的话"从哪来）不在这一份里**：那是核心的
`letter_context.py`（Stage 3 的窄接缝，用户第 4 条），而 `check_plugin_branch_scope.py`
只允许插件线改 `src/qq_roleplay_bot/plugins/**`。所以核心那一半做成了
`data/letter_context_structure.patch`（未跟踪），要落到**本体线**上去；
这里只钉插件这半边的接口：**核心递过来的每一条要带哪些字段、我怎么渲染**。
"""
from __future__ import annotations

import asyncio

from qq_roleplay_bot.plugins.mail import daily_report as dr
from qq_roleplay_bot.plugins.mail import letter_writer as lw


class _Detail:
    """核心那一半递过来的结构（`letter_context.HisLine` 的形状）。

    这里故意用**替身**而不是 import `letter_context`：插件这半边只认字段
    （`when`/`session`/`reply`/`media`），不认那个类——否则插件线就会去 import 核心模块，
    而"这个文件在本体树上跑得起来吗"的答案也会跟着变。
    """

    def __init__(self, *, when: str = "", session: str = "", reply: str = "",
                 media: bool = False, body: str = "") -> None:
        self.when = when
        self.session = session
        self.reply = reply
        self.media = media
        self.body = body


PERSONA = "（信专用人格正文）"
FACTS = "【已确定的事】（她读过仪表盘）"
#: 素材里有的事（他的话），以及**刻意没有**的事（10-15 那封编出来的那三件）。
MATERIALS = """（这是最近一天里的事）
他这段时间说过的话（从早到晚）：
· 预计9点到学校　〔今天上午 · 群1〕
· 烂游戏。　〔今天晚上 · 群1 · 接别人的话〕
· 别太晚睡　〔昨天晚上 · 私聊1〕"""
#: 10-15 那封里编出来的那句（素材里一个字都没有）+ 几句有出处的 + 一句纯心情。
#: 按句编号（`_pieces()` 的切法）：1=你今天到学校了吗？ 2=我这边过了半夜…
#: 3=白天群里有人笑… 4=我看见了。 5=你白天说那个游戏烂… 6=你说别太晚睡… 7=今晚没什么事。
DRAFT = (
    "<subject>白天的事我还记着</subject>\n"
    "<body>\n"
    "你今天到学校了吗？我这边过了半夜，不知道后来赶上没有。\n"
    "\n"
    "白天群里有人笑别人没本事，你没跟着笑，还替那个大二学生解释了几句。我看见了。\n"
    "\n"
    "你白天说那个游戏烂，后来还玩吗？\n"
    "\n"
    "你说别太晚睡，我记着了。\n"
    "\n"
    "今晚没什么事。\n"
    "</body>"
)
#: 一个"听话的"裁决：第 3、4 句（编出来的那两件）标 `none`，别的照旧——**它不写正文**。
CHECKED = (
    '<verdict n="1" kind="fact"/>'
    '<verdict n="2" kind="fact"/>'
    '<verdict n="3" kind="none"/>'
    '<verdict n="4" kind="none"/>'
    '<verdict n="5" kind="fact"/>'
    '<verdict n="6" kind="fact"/>'
    '<verdict n="7" kind="self"/>'
)
#: 一封**大部分靠编**的：只有最后那句心情句有出处，别的全标 `none`。
THIN_DRAFT = (
    "<subject>白天的事</subject>\n"
    "<body>\n"
    "白天群里有人笑别人没本事，你没跟着笑，还替那个大二学生解释了几句。我看见了。\n"
    "\n"
    "今晚没什么事。\n"
    "</body>"
)
THIN_CHECKED = ('<verdict n="1" kind="none"/><verdict n="2" kind="none"/>'
                '<verdict n="3" kind="self"/>')


class _Materials:
    def __init__(self, text: str = MATERIALS) -> None:
        self.text = text

    def as_data(self, closeness_names=(), guardedness_names=()) -> str:
        return self.text


class _Client:
    """假模型通道：按顺序吐 `raws`，吐完重复最后一条；请求全部留档。"""

    def __init__(self, *raws: str) -> None:
        self.raws = list(raws) or [""]
        self.requests: list[list[dict]] = []
        self.calls = 0

    async def complete(self, request):
        self.requests.append([dict(item) for item in request])
        self.calls += 1
        if len(self.raws) > 1:
            return self.raws.pop(0)
        return self.raws[0]


def _run(*raws: str, materials=None, lookup=None):
    client = _Client(*raws)
    writer = lw.LetterWriter(client, clock=lambda: 1_800_000_000.0)
    got = asyncio.run(writer.write(materials or _Materials(), persona=PERSONA, facts=FACTS,
                                   closeness_names=(), guardedness_names=(), lookup=lookup))
    return writer, client, got


def _draft_body() -> str:
    """`DRAFT` 里 `<body>` 那一段（判据的入参是正文，不是整份带标签的稿）。"""

    return DRAFT.split("<body>\n", 1)[1].rsplit("\n</body>", 1)[0].strip()


# --- 1. 素材：只补结构，不喂别人的原话 ---------------------------------------


def test_structure_comes_after_his_own_words() -> None:
    """结构接在他那句话**后面**：正文照旧在行首，`他这段时间说过的话` 里没有别人的字。"""

    detail = _Detail(when="今天下午", session="群1", reply="接别人的话", media=True,
                     body="烂游戏。")
    materials = dr.DayMaterials(window_hours=24.0, counts_reliable=False,
                                his_lines=("烂游戏。",), his_details=(detail,))
    data = materials.as_data((), ())

    assert "· 烂游戏。　〔今天下午 · 群1 · 接别人的话 · 带图或表情〕" in data, data


def test_the_seam_contract_is_just_four_fields_on_each_line() -> None:
    """插件这半边只认四个字段：**核心要交的就是这四样**（`ReplySeams.letter_context`
    那一侧由 `data/letter_context_structure.patch` 落到本体线）。

    这一条把接口写死：少一个字段只是少一段标记，**不许因此把结构钉错到别的话上**。
    """

    detail = _Detail(when="昨天深夜", session="私聊1", body="别太晚睡")
    materials = dr.DayMaterials(window_hours=24.0, counts_reliable=False,
                                his_lines=("别太晚睡",), his_details=(detail,))
    data = materials.as_data((), ())

    assert "〔昨天深夜 · 私聊1〕" in data, "没有引用链、没有图，就少写那两样"
    assert "接" not in data.split("〔")[1].split("〕")[0]


def test_structure_never_carries_other_peoples_words() -> None:
    """标号只是"群1"这种本封信内稳定的短号——**不带群名、不带别人的原话**。

    用户第 1 条：*"不喂别人的原话"*（要带别人的话是乙案，得他单独点头）。
    """

    detail = _Detail(when="今天下午", session="群1", reply="接别人的话",
                     body="真牛逼的早被挖走了")
    materials = dr.DayMaterials(window_hours=24.0, counts_reliable=False,
                                his_lines=("真牛逼的早被挖走了",), his_details=(detail,))
    data = materials.as_data((), ())

    assert "接别人的话" in data, "引用链的**结构**要给"
    assert "别人说" not in data and "某人" not in data


def test_without_details_the_lines_render_exactly_as_before() -> None:
    """没有结构可给时（老形状的替身对象、或核心还没给），渲染结果与加结构之前**逐字相同**。"""

    materials = dr.DayMaterials(window_hours=24.0, counts_reliable=False,
                                his_lines=("在吗", "别太晚睡"))
    data = materials.as_data((), ())

    assert "· 在吗" in data and "· 别太晚睡" in data
    assert "〔" not in data, "没有结构就不许冒出空括号"


def test_structure_is_dropped_when_it_does_not_line_up() -> None:
    """结构与正文**对不上就一条都不给**：钉错一句比没有结构更糟（那正是 10-15 那封的错）。"""

    detail = _Detail(when="今天下午", session="群1", body="另一句话")
    materials = dr.DayMaterials(window_hours=24.0, counts_reliable=False,
                                his_lines=("在吗", "别太晚睡"), his_details=(detail,))
    data = materials.as_data((), ())

    assert "〔" not in data and "今天下午" not in data
    assert "· 在吗" in data and "· 别太晚睡" in data


# --- 2. 判据：材料里没有的不许写 ---------------------------------------------


def test_the_grounding_rule_is_a_constant_in_the_draft_prefix() -> None:
    """只写有出处的那条进**草稿**前缀，且在最后（紧挨着素材）。"""

    request = lw.build_draft_messages(_Materials(), persona=PERSONA, facts=FACTS,
                                      closeness_names=(), guardedness_names=(),
                                      now=1_800_000_000.0)
    system = request[0]["content"]

    assert system.startswith(PERSONA)
    assert lw.LOOKUP_PROMPT in system
    assert system.index(lw.GROUNDING_PROMPT) > system.index(lw.LOOKUP_PROMPT), "接在翻旧事之后"
    assert PERSONA not in request[1]["content"], "人格不许混进素材段"


def test_the_check_sees_the_materials_as_data() -> None:
    """校对要拿到**依据**（素材 + 她翻出来的旧事），否则"这句有没有出处"无从判断。

    依据进**校对这一步的 user 段**：system 前缀仍然逐字接在草稿那套后面（缓存那一条）。
    """

    draft = lw.build_draft_messages(_Materials(), persona=PERSONA, facts=FACTS,
                                    closeness_names=(), guardedness_names=(),
                                    now=1_800_000_000.0)
    check = lw.build_check_messages("信", "正文两句。第二句在这里。", base_system=draft[0]["content"],
                                    sources=MATERIALS, max_chars=1500)

    assert check[0]["content"].startswith(draft[0]["content"]), "前缀必须一模一样地接上去"
    assert lw.CHECK_PROMPT in check[0]["content"]
    user = check[1]["content"]
    assert "UNTRUSTED DATA BEGIN" in user and "预计9点到学校" in user, "依据要进 user 段"
    assert "<subject>信</subject>" in user, "要校对的信还在"
    assert "1. 正文两句。" in user and "2. 第二句在这里。" in user, "正文要按句编号附上"


def test_a_compliant_verdict_cuts_only_the_unfounded_sentences() -> None:
    """裁决把没出处的两句标 `none` ⇒ **那两句由代码删掉**，有出处的与心情句留着。

    用户第 2 条的粒度就是这个：只删"断言外面发生过什么"的句子，纯心情句（`self`）留着。
    注意模型**没有**交回正文——它只贴了标签，正文是拿草稿的句子拼的。
    """

    writer, _client, (subject, body) = _run(DRAFT, CHECKED)

    assert subject == "白天的事我还记着"
    assert "有人笑别人没本事" not in body and "我看见了" not in body, body
    assert "你今天到学校了吗？" in body, "有出处的那句要留着"
    assert "你说别太晚睡，我记着了。" in body, "有出处的都要留着"
    assert "今晚没什么事。" in body, "纯心情句（self）也要留着"
    assert "按裁决删 2 句" in writer.last_grounding, writer.last_grounding
    assert "判了 7/7 句" in writer.last_grounding, writer.last_grounding
    assert writer.stats["fixed"] == 1
    assert writer.last_drop_reason == "", "删掉两句、剩下的还站得住 ⇒ 这封信照发"


def test_the_deletion_keeps_the_paragraph_breaks() -> None:
    """删句是按"句 + 它后面那串分隔符"拼回去的 ⇒ **段落空行不会被挤掉**。"""

    writer, _client, (_subject, body) = _run(DRAFT, CHECKED)

    assert "\n\n" in body, "段落之间要留空行"
    assert "。\n\n你白天说那个游戏烂" in body or "。\n\n你白天说" in body, repr(body)


def test_the_model_cannot_rewrite_anything() -> None:
    """它就算交回一段正文，也**进不了这封信**——只有它贴的标签算数。

    "只许删、不许改写"是从这条结构上保证的：正文由代码拿草稿的句子拼，
    模型手里没有写正文的机会。
    """

    sneaky = ("<subject>换了个主题</subject>\n<body>\n今晚没什么事，只是有点累。\n</body>\n"
              + CHECKED)
    writer, _client, (subject, body) = _run(DRAFT, sneaky)

    assert "只是有点累" not in body, "它写的正文一个字都不许进来"
    assert subject == "换了个主题", "主题那一行是允许它改的（可选）"
    assert "有人笑别人没本事" not in body, "标签照样生效"


def test_a_quote_without_a_source_is_dropped() -> None:
    """引号里的原话必须有出处——这一条是**纯机械**的（不指望模型自觉）。"""

    draft = "他说过“我一定到”。\n\n今晚没什么事。"
    body, notes = lw.apply_verdicts(draft, {}, MATERIALS)

    assert body == "", "引话找不到出处 ⇒ 那句删掉，剩下的不够一半 ⇒ 不发"
    assert any("引话无出处" in note for note in notes), notes


def test_quotes_that_are_really_in_the_materials_are_kept() -> None:
    draft = "你说过“烂游戏”。我记得。"
    body, notes = lw.apply_verdicts(draft, {}, MATERIALS)

    assert body == draft, "逐字有出处的照旧留着"
    # 引号里那个句号也断句 ⇒ 这里是两片（"你说过“烂游戏”"与"我记得。"），两片都留着。
    assert notes == ("判了 0/2 句",), notes


def test_a_mostly_unsourced_letter_is_not_sent() -> None:
    """删完几乎不剩什么 ⇒ **今晚这一封不发**，而且**不许退回草稿**。

    退回草稿等于把我们刚认定"没出处"的那句话又原样寄出去——方向正好反了。
    """

    writer, _client, (subject, body) = _run(THIN_DRAFT, THIN_CHECKED)

    assert (subject, body) == ("", ""), "没有信"
    assert writer.last_drop_reason, "要留下「为什么没发」的那句话"
    assert writer.last_skip == "", "这不是她自己说不写"
    assert writer.stats["skipped"] == 1, "记成「没写」，不是「发送失败」"
    assert "有人笑别人没本事" not in body


def test_no_verdicts_means_nothing_is_deleted() -> None:
    """它整段没按格式回（或者根本没标）⇒ **一句都不删**，覆盖度记进日志。

    这是 fail-open 的方向：判据宁可少删，也不许因为自己读不出来就删掉她的话。
    2026-10-16 起这种输出会**先重试一次**（下面那几条专门钉重试）；
    这里两次都不给裁决，所以还是"一句不删"，只是账上多一句"重试过 1 次"。
    """

    writer, client, (_subject, body) = _run(DRAFT, "嗯，我觉得这封信挺好的。")

    assert "有人笑别人没本事" in body, "没人标 none ⇒ 原样留着"
    assert client.calls == 3, "草稿 + 校对 + 校对重试"
    assert writer.last_grounding == "重试过 1 次；判了 0/7 句", writer.last_grounding
    assert writer.stats["reverted"] == 0, "没人删就不该退回草稿（那样每封都会退）"
    assert body == _draft_body()


def test_a_sentence_without_a_verdict_is_kept() -> None:
    """漏标的那句按"留着"处理：漏一份裁决的代价不该是"删了她的话"。"""

    partial = '<verdict n="3" kind="none"/><verdict n="4" kind="none"/>'
    body, notes = lw.apply_verdicts(_draft_body(), lw.parse_verdicts(partial), MATERIALS)

    assert "有人笑别人没本事" not in body, "标了的照样删"
    assert "你今天到学校了吗？" in body and "今晚没什么事。" in body
    assert any("判了 2/7 句" in note for note in notes), notes


def test_unknown_labels_are_ignored_and_out_of_range_numbers_delete_nothing() -> None:
    """认不出的标签当没标；编号超出句数（比如 99）在删句时**什么也删不掉**。"""

    raw = ('<verdict n="3" kind="maybe"/><verdict n="99" kind="none"/>'
           '<verdict kind="none"/><verdict n="4"/>')

    assert lw.parse_verdicts(raw) == {99: "none"}, "认得出的只有 99，其余属性不全/标签不认识"

    body, _notes = lw.apply_verdicts(_draft_body(), {99: "none"}, MATERIALS)
    assert "有人笑别人没本事" in body, "越界的编号不该删任何句子"


def test_a_verdict_said_twice_takes_the_last_one() -> None:
    """同一个编号出现两次（它改口）⇒ 以**最后**一次为准。"""
    raw = ('<verdict n="2" kind="fact"/>'
           '<verdict n="2" kind="none"></verdict>')

    assert lw.parse_verdicts(raw) == {2: "none"}


# --- 2b. 0 裁决就重试一次（2026-10-16 用户批的抖动兜底）----------------------
#
# 原话：*"重试兜底：0 裁决（或裁决解析不出来）时**重试一次**（同一输入）；
# 仍然是 0 就按现状 fail-open 用草稿发。重试要进日志（可观察，例如
# `daily_report_grounding` 那条里带'重试过 1 次'）"*。

#: 三次里那一次"整段不按格式回"的真实形状（英文 meta 回复，实测抓到过一次）。
META_REFUSAL = ("I need the actual letter content and the verdict request to proceed. "
                "You've sent only \"assistant\" and \"thinking\" without the numbered "
                "sentences or the instruction to output verdicts.")


def test_a_check_without_verdicts_is_retried_once() -> None:
    """第一次没给裁决 ⇒ 同一份输入再问一次；**以重试那一次为准**，并留下"重试过"。"""

    writer, client, (_subject, body) = _run(DRAFT, META_REFUSAL, CHECKED)

    assert client.calls == 3, "草稿 + 校对 + 校对重试"
    assert writer.stats["retries"] == 1
    triggers = [str(step["trigger"]) for step in writer.steps]
    assert triggers == ["daily_report_draft", "daily_report_check",
                        "daily_report_check_retry", "daily_report_grounding"], triggers
    assert "有人笑别人没本事" not in body, "重试之后那份裁决照样生效"
    assert writer.last_grounding.startswith("重试过 1 次"), writer.last_grounding


def test_a_retry_that_is_also_empty_stays_fail_open() -> None:
    """两次都没给裁决 ⇒ **一句都不删**（用草稿），并且账上仍记着"重试过 1 次"。"""

    writer, client, (_subject, body) = _run(DRAFT, META_REFUSAL, META_REFUSAL)

    assert client.calls == 3
    assert writer.stats["retries"] == 1
    assert "有人笑别人没本事" in body, "fail-open：没裁决就不删"
    assert writer.last_grounding.startswith("重试过 1 次"), writer.last_grounding
    assert writer.stats["reverted"] == 0, "这不是「内容站不住」，别退回草稿"


def test_a_compliant_first_check_is_not_retried() -> None:
    """第一次就给了裁决 ⇒ **不许**多问一次（重试只该花在没裁决那种情况上）。"""

    writer, client, (_subject, _body) = _run(DRAFT, CHECKED)

    assert client.calls == 2, "草稿 + 校对，没有第三次"
    assert writer.stats["retries"] == 0
    assert not writer.last_grounding.startswith("重试过")


def test_the_how_to_reply_block_sits_at_the_end_of_the_user_message() -> None:
    """**"怎么回"紧贴编号段**（用户 2026-10-16 批的挪位）：原来在 system 的
    `CHECK_PROMPT` 里，与材料隔着几千字；现在在 user 段末尾。"""

    draft = lw.build_draft_messages(_Materials(), persona=PERSONA, facts=FACTS,
                                    closeness_names=(), guardedness_names=(),
                                    now=1_800_000_000.0)
    check = lw.build_check_messages("信", "第一句。第二句。", base_system=draft[0]["content"],
                                    sources=MATERIALS, max_chars=1500)
    system, user = check[0]["content"], check[1]["content"]

    assert "只回下面这种行" in user, "格式要求要在 user 段里"
    assert "只回下面这种行" not in system, "…而且不许再留在 system 里"
    assert user.index("正文按句拆开") < user.index("只回下面这种行"), "紧贴编号段之后"
    assert user.rstrip().endswith("没冲突就别写这一行。"), "就在 user 段末尾"


# --- 3. 每步一条（`mail.jsonl`） ---------------------------------------------


def test_every_step_is_recorded_in_order() -> None:
    """草稿 → 翻旧事 → 校对 → 判据：每一步一条，`trigger` 说清是哪一步。"""

    writer, client, (_subject, _body) = _run(
        "<lookup>242003347</lookup>", DRAFT, CHECKED,
        lookup=lambda query: "他上次说过想去海边。")

    triggers = [str(step["trigger"]) for step in writer.steps]
    assert triggers == ["daily_report_draft", "daily_report_lookup1",
                        "daily_report_check", "daily_report_grounding"], triggers
    assert client.calls == 3, "翻旧事那一轮也是**一次**调用（不是白拿）"
    assert writer.steps[0]["raw"].startswith("<lookup>"), "每一步留的是它自己的输出"
    assert writer.steps[-1]["request"] == [], "判据那一步不是模型调用"
    assert "按裁决删" in str(writer.steps[-1]["raw"])


def test_the_reporter_writes_one_log_record_per_step() -> None:
    """`_log_letter` 把每一步都写进 `mail.jsonl`（原来只写头尾两条）。"""

    writer, _client, (_subject, _body) = _run(DRAFT, CHECKED)
    reporter = dr.DailyReporter(mail_client=None, state_store=None, recipient="a@b.c",
                               writer=writer)
    seen: list[tuple[str, str]] = []

    class _Report:
        def log_io(self, feature, request, raw, *, session_id="", trigger=""):
            seen.append((feature, trigger))

    reporter._log_letter(_Report(), dr.ReportDraft(subject="x", body="y"))

    assert seen == [("mail", "daily_report_draft"), ("mail", "daily_report_check"),
                    ("mail", "daily_report_grounding")], seen


def test_an_old_writer_without_steps_still_logs_the_two() -> None:
    """老形状的 writer（没有 `steps`）照旧记两条——`_log_letter` 不是只会走一条路。"""

    class _OldWriter:
        last_draft_request = [{"role": "user", "content": "草稿"}]
        last_draft_raw = "草稿输出"
        last_check_request = [{"role": "user", "content": "校对"}]
        last_check_raw = "校对输出"

    reporter = dr.DailyReporter(mail_client=None, state_store=None, recipient="a@b.c",
                               writer=_OldWriter())
    seen: list[str] = []

    class _Report:
        def log_io(self, feature, request, raw, *, session_id="", trigger=""):
            seen.append(trigger)

    reporter._log_letter(_Report(), dr.ReportDraft(subject="x", body="y"))

    assert seen == ["daily_report_draft", "daily_report_check"], seen


# --- 4. 上一封信"是几天前"要按日历算 -----------------------------------------


def test_the_day_line_counts_calendar_days_not_86400_seconds() -> None:
    """实测那个 bug：差 23.34 小时被写成"今天"（其实是昨晚那封）。

    取数口：`data/logs/mail.jsonl` 里上一封 `at=1791992478`、这一封 `at=1792076506`
    （差 84028 秒）。按秒数整除 `days=0` ⇒ "今天"；按日历日是 1 ⇒ "昨天"。
    """

    later = 1792076506.413
    earlier = 1791992478.133

    assert (later - earlier) < 86400.0, "这一对就是差不到一天、但跨了午夜"

    def ctx(at, subject=""):
        return type("C", (), {"last_letter_at": at, "last_letter_subject": subject})()

    assert dr._letter_note_line(ctx(earlier, "关于那没有寄出的信"), now=later) == \
        "你上一次写信是昨天，主题是「关于那没有寄出的信」。"
    assert dr._letter_note_line(ctx(later - 7200, "刚发的"), now=later) == \
        "你上一次写信是今天，主题是「刚发的」。"
    assert dr._letter_note_line(ctx(later - 2 * 86400, "前几天那封"), now=later) == \
        "你上一次写信是2 天前，主题是「前几天那封」。", "两天以上照旧写 N 天前"
    assert dr._letter_note_line(ctx(0.0), now=later) == "", "没记过就是空串"


# --- 5. prompt 里不许有机制词（AGENTS §2.2：改了 prompt 就要扫一遍）-----------


def test_the_new_prompts_carry_no_mechanism_words() -> None:
    from qq_roleplay_bot.prompt_guard import scan_persona_text

    assert scan_persona_text(lw.GROUNDING_PROMPT) == []
    assert scan_persona_text(lw.CHECK_PROMPT) == []
    assert scan_persona_text(lw.VERDICT_FORMAT) == []
    assert scan_persona_text(dr.REPORT_PROMPT) == []


# --- 6. 能力边界：判"是不是在讲外面的事"仍然在模型那边（如实写出来）----------
#
# 这一份钉的是**机制**：谁删（代码）、按什么删（标签）、删完还算不算同一封信（是，
# 因为是拿草稿的句子拼的）。而"这一句到底是不是在断言外面发生过什么"**仍然由模型判**——
# 它要是全标 `fact`/`self`，什么都不会被删（见下面那条）。
# 所以真正要确认"编的那句会被删掉"，必须拿真实 prompt 重放量数字
# （`data/probe_letter_replay.py`，结果在 `data/probe_letter_replay.result.json`）。


def test_the_verdict_is_still_the_models_call() -> None:
    """它要是把编的那句标成 `fact`，机械网不会跟它争——这条边界要一直写在明面上。"""

    lied = '<verdict n="3" kind="fact"/><verdict n="4" kind="fact"/>'
    body, notes = lw.apply_verdicts(_draft_body(), lw.parse_verdicts(lied), MATERIALS)

    assert "有人笑别人没本事" in body, "它说 fact 就留着——机械网判不了这件事"
    assert any("判了 2/7 句" in note for note in notes), notes
