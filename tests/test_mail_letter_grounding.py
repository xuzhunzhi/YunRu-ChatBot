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
#: 一个"听话的"校对：把那句没出处的删了，别的留着，并自报删了什么。
CHECKED = (
    "<subject>白天的事我还记着</subject>\n"
    "<body>\n"
    "你今天到学校了吗？我这边过了半夜，不知道后来赶上没有。\n"
    "\n"
    "你白天说那个游戏烂，后来还玩吗？\n"
    "\n"
    "你说别太晚睡，我记着了。\n"
    "\n"
    "今晚没什么事。\n"
    "</body>\n"
    "<cut>白天群里有人笑别人没本事，你没跟着笑，还替那个大二学生解释了几句。我看见了。</cut>"
)
#: 一封**大部分靠编**的：删掉那句没出处的之后几乎不剩什么。
THIN_DRAFT = (
    "<subject>白天的事</subject>\n"
    "<body>\n"
    "白天群里有人笑别人没本事，你没跟着笑，还替那个大二学生解释了几句。我看见了。\n"
    "\n"
    "今晚没什么事。\n"
    "</body>"
)
THIN_CHECKED = ("<subject>白天的事</subject>\n<body>\n今晚没什么事。\n</body>\n"
                "<cut>白天群里有人笑别人没本事，你没跟着笑，还替那个大二学生解释了几句。我看见了。</cut>")


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
    check = lw.build_check_messages("信", "正文", base_system=draft[0]["content"],
                                    sources=MATERIALS, max_chars=1500)

    assert check[0]["content"].startswith(draft[0]["content"]), "前缀必须一模一样地接上去"
    assert lw.CHECK_PROMPT in check[0]["content"]
    user = check[1]["content"]
    assert "UNTRUSTED DATA BEGIN" in user and "预计9点到学校" in user, "依据要进 user 段"
    assert "<subject>信</subject>" in user, "要校对的信还在"


def test_a_compliant_check_can_cut_an_invented_sentence() -> None:
    """校对把那句没出处的删掉之后：**删掉的那句真的没了，有出处的与心情句留着**。

    用户第 2 条的粒度就是这个：只删"断言外面发生过什么"的句子，纯心情句留着。
    """

    writer, client, (subject, body) = _run(DRAFT, CHECKED)

    assert subject == "白天的事我还记着"
    assert "有人笑别人没本事" not in body and "我看见了" not in body, body
    assert "你今天到学校了吗？" in body, "有出处的那句要留着"
    assert "你说别太晚睡，我记着了。" in body, "有出处的都要留着"
    assert "今晚没什么事。" in body, "纯心情句也要留着"
    assert "删句" in writer.last_grounding, writer.last_grounding
    assert writer.stats["fixed"] == 1
    assert writer.last_drop_reason == "", "只删掉一句、剩下的还站得住 ⇒ 这封信照发"


def test_a_quote_without_a_source_is_dropped() -> None:
    """引号里的原话必须有出处——这一条是**纯机械**的（不指望模型自觉）。"""

    draft = "他说过“我一定到”。\n\n今晚没什么事。"
    checked = "他说过“我明天肯定不来”，还说他很忙。\n\n今晚没什么事。"
    body, notes = lw.check_against_sources(draft, checked, MATERIALS)

    assert body == "", "引话找不到出处 ⇒ 不吃这句改写"
    assert any("引话无出处" in note for note in notes), notes


def test_quotes_that_are_really_in_the_materials_are_kept() -> None:
    draft = "你说过“烂游戏”。我记得。"
    body, notes = lw.check_against_sources(draft, draft, MATERIALS)

    assert body == draft, "逐字有出处的照旧留着"
    assert notes == ()


def test_a_rewritten_check_falls_back_to_the_draft() -> None:
    """校对稿里冒出草稿里没有的句子 ⇒ **退回草稿**（只许删，不许改写）。

    这一条是"这一步写坏了"（不是内容站不住），所以按老规矩退回草稿——草稿才是她自己的字。
    """

    checked = ("<subject>白天的事我还记着</subject>\n<body>\n"
               "你今天到学校了吗？我这边过了半夜，不知道后来赶上没有。\n\n"
               "今晚没什么事，只是有点累。\n</body>")
    writer, _client, (_subject, body) = _run(DRAFT, checked)

    assert "今晚没什么事，只是有点累" not in body, "改写过的句子不许进来"
    assert body.startswith("你今天到学校了吗？"), "退回草稿"
    assert writer.last_drop_reason == "", "这不是「内容站不住」，别当成不发"


def test_a_mostly_unsourced_letter_is_not_sent() -> None:
    """删完几乎不剩什么 ⇒ **今晚这一封不发**，而且**不许退回草稿**。

    退回草稿等于把我们刚认定"没出处"的那句话又原样寄出去——方向正好反了。
    这是 2026-10-16 与 `MIN_RATIO` 那道老兜底撞出来的分岔，见 `letter_writer._check`。
    """

    writer, _client, (subject, body) = _run(THIN_DRAFT, THIN_CHECKED)

    assert (subject, body) == ("", ""), "没有信"
    assert writer.last_drop_reason, "要留下「为什么没发」的那句话"
    assert writer.last_skip == "", "这不是她自己说不写"
    assert writer.stats["skipped"] == 1, "记成「没写」，不是「发送失败」"
    assert "有人笑别人没本事" not in body


def test_an_unreported_deletion_falls_back_to_the_draft() -> None:
    """草稿里有、校对稿里没有、**又没在 `<cut>` 里报过**的句子 ⇒ 当成这一步写坏了。

    这是原来那道"0.5 倍下限"的替代：下限要保留，但只该拦住**没人认领的删**
    （截断、跑飞、把信写没一半）。
    """

    draft = "第一句有出处。\n\n第二句也有。"
    body, notes = lw.check_against_sources(draft, "第一句有出处。", MATERIALS)

    assert body == "", "静默删句 ⇒ 退回草稿"
    assert any("没报就删了 1 句" in note for note in notes), notes


def test_a_reported_deletion_is_accepted_and_accounted() -> None:
    """报过的那句删掉 ⇒ 接受；账上也记得对得上。"""

    draft = "第一句有出处。\n\n第二句也有。"
    cuts = lw.parse_cuts("<cut>第二句也有。</cut>")
    body, notes = lw.check_against_sources(draft, "第一句有出处。", MATERIALS, cuts=cuts)

    assert body == "第一句有出处。"
    assert any("对得上 1 / 对不上 0" in note for note in notes), notes


def test_a_cut_that_is_not_in_the_draft_is_only_accounted() -> None:
    """`<cut>` 里混进一句草稿里没有的话：只记账，不改这封信。"""

    draft = "第一句有出处。\n\n第二句也有。"
    cuts = lw.parse_cuts("<cut>第二句也有。</cut>\n<cut>我没说过这句话。</cut>")
    body, notes = lw.check_against_sources(draft, "第一句有出处。", MATERIALS, cuts=cuts)

    assert body == "第一句有出处。"
    assert any("对得上 1 / 对不上 1" in note for note in notes), notes


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
    assert "删句" in str(writer.steps[-1]["raw"])


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
    assert scan_persona_text(dr.REPORT_PROMPT) == []


# --- 6. 能力边界：机械网只能"删"，判不了"是不是在断言"（如实写出来）----------
#
# 这不是"我们想要的成品"，而是**这条判据能保证什么、不能保证什么**：
# 机械那三条只能保证"只删不改写、引话要有出处"；"这句是不是在讲外面发生过的事"
# 由校对那一步（模型）判断。所以真正要确认"编的那句会被删掉"，
# 必须拿真实 prompt 重放量数字——离线测试钉不住它。


def test_the_mechanical_net_cannot_judge_an_invention_by_itself() -> None:
    """校对什么都不动时，草稿原样通过（机械网不替模型做判断）。

    10-15 那封的机制就是**这个形状**：草稿与校对稿逐字相同 ⇒ 编出来的那句进了信。
    """

    writer, _client, (_subject, body) = _run(DRAFT, DRAFT)

    assert "有人笑别人没本事" in body, "机械网不会自己把它删掉——这是它的边界"
    assert writer.last_grounding == "没删句"
    assert writer.stats["reverted"] == 0, "没人删句就不该退回草稿（那样每封都会退）"
