"""写信 agent 自己的那一半：单独的人格、只读记忆、以及"今晚可以不写"。

用户 2026-10-12 原话（这天他连着问了三次"为什么到处都是重启就出问题的"）：

    我不理解为什么到处都是重启就出问题的。写信的人格prompt做单独适配。注意写信agent本身
    是插件里的，所以凭啥这玩意能直接复用人格prompt啊。记忆要完整开放给写信agent来读取，
    又不是强制一次输入就必须得到一封信。

用户 2026-10-22 又定了方向（工具型 agent）：

    我要的是云茹想说什么就说什么，而不是给她一些素材照着写。agent 有一个重要原则，
    就是想调用工具才调用……工具全部丢给她，后面再修。

四件事一条一条钉：

| 钉什么 | 怎么钉 |
| --- | --- |
| 人格与事实表**由核心现读、按次递进来** | 插件里不许再出现 `BASE_PROMPT` / `LETTER_FACTS` / `private_docs`：它不认识文件，只认识递进来的两份正文 |
| 记忆是**只读口**，她自己按需翻 | `<lookup>` 走 `LetterToolbox`（生产是核心的 `MemoryRead`）；翻不到、没给、抛异常都照样能写 |
| **九样她都能自己点**（2026-10-22） | 每一样都真的取到对应数据；坏的/不认识的标签按"没查到"处理；`MAX_TOOL_CALLS` 上限生效 |
| **不强制一晚一封** | `<skip>原因</skip>` ⇒ 没有信、不算故障、原因留在 `last_skip` 里给调用方记账 |

它还钉住缓存那一条（2026-09-30 用户要求"必须优化缓存命中率"）：两阶段的 `system` 前缀
逐字相同，校对规矩只是接在后面——易变的东西（她看到的内容、时间戳）全在 user 段末尾。
"""
import asyncio
import contextlib
import logging
from pathlib import Path

from qq_roleplay_bot.plugins.mail import daily_report as dr
from qq_roleplay_bot.plugins.mail import letter_tools as lt
from qq_roleplay_bot.plugins.mail import letter_writer as lw

NOW = 1_800_000_000.0
PERSONA = "（信专用人格正文）"
FACTS = "（已确定的事：她读过仪表盘）"
LETTER = "<subject>今天</subject>\n<body>群里今天挺热闹，我跟几个人说了话。</body>"
SOURCE = Path(lw.__file__).read_text(encoding="utf-8")
REPORT_SOURCE = Path(dr.__file__).read_text(encoding="utf-8")


@contextlib.contextmanager
def _no_logs():
    """把日志静音：接缝炸掉时它会打 warning + 堆栈（那是给运维看的）。"""

    logging.disable(logging.CRITICAL)
    try:
        yield
    finally:
        logging.disable(logging.NOTSET)


class _Tools:
    """她能先看看的那几样的替身：记下她点了什么，按名字回一段固定的话。

    `texts` 里没写的名字回空串（= 没翻着）。**它不叫 materials**：2026-10-22 起
    写信那边拿到的就是这个东西，而不是一份"素材包"。
    """

    def __init__(self, texts=None, *, boom: bool = False) -> None:
        self.texts = dict(texts or {"his": "· 预计9点到学校　〔今天上午 · 群1〕"})
        self.boom = boom
        self.calls: list[tuple[str, str]] = []

    def run(self, tool: str, query: str = "") -> lt.ToolAnswer:
        self.calls.append((tool, query))
        if self.boom:
            raise RuntimeError("库打不开")
        return lt.ToolAnswer(tool=tool, query=query, text=self.texts.get(tool, ""))


class _Client:
    """假的模型通道：按顺序吐 `raws`，吐完就重复最后一条。请求全部留档。"""

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


def _write(**kwargs):
    """跑一次 `LetterWriter.write`（默认给了能看的东西、人格、事实）。"""

    client = kwargs.pop("client")
    writer = kwargs.pop("writer", None) or lw.LetterWriter(client, clock=lambda: NOW)
    tools = kwargs.pop("tools", None) or _Tools()
    params = {"persona": PERSONA, "facts": FACTS, "closeness_names": (),
              "guardedness_names": ()}
    params.update(kwargs)
    return writer, asyncio.run(writer.write(tools, **params))


# --- 人格与事实表：插件不认识文件，只认识递进来的正文 -------------------------


def test_the_plugin_no_longer_reaches_for_the_chat_persona():
    """插件里不许再有"自己找人格/事实"的路子（那正是用户点的那件事）。

    查的是**代码形状**（`base_prompt` 这几个字在解释性 docstring 里出现是对的：
    那是在说明"这里从前就是那么写的"）。
    """

    for forbidden in ("from ...base_prompt import", "import BASE_PROMPT",
                      "_load_letter_facts", 'LETTER_FACTS = "', ".parents["):
        assert forbidden not in SOURCE, f"写信插件不该再出现 {forbidden}"
    for forbidden in ("base_prompt", "BASE_PROMPT"):
        assert forbidden not in REPORT_SOURCE, f"日报插件不该再出现 {forbidden}"


def test_the_draft_prefix_is_the_persona_the_core_handed_over():
    """system 前缀 = 人格 + 写信规矩 + 事实表 + 能看的东西的目录 + 只写看过的。

    2026-10-22：这里**没有材料**了——`user` 段只有一句"想写什么就写什么"。
    目录（`TOOLS_PROMPT`）与"只写看过的"（`GROUNDING_PROMPT`）都是常量，所以前缀
    每封都一样（缓存那一条不受影响）。
    """

    request = lw.build_draft_messages(persona=PERSONA, facts=FACTS, now=NOW)
    system, user = request[0], request[1]

    assert system["role"] == "system" and user["role"] == "user"
    assert system["content"].startswith(PERSONA)
    assert dr.REPORT_PROMPT in system["content"], "写信规矩还是要带上"
    assert FACTS in system["content"]
    assert lw.TOOLS_PROMPT in system["content"]
    assert lw.GROUNDING_PROMPT in system["content"]
    assert system["content"].index(lw.GROUNDING_PROMPT) > system["content"].index(lw.TOOLS_PROMPT)
    assert PERSONA not in user["content"], "人格不许混进 user 段"
    assert user["content"].rstrip().endswith("）"), "时间戳留在最后：它挪到前面，前缀每次都断"
    assert "他这段时间说过的话" not in user["content"], (
        "2026-10-22：素材不再预塞——他/群里/旧事都要她自己点开看"
    )


def test_a_letter_carries_the_persona_into_the_model_call():
    client = _Client(LETTER)
    writer, (subject, body) = _write(client=client)

    assert subject == "今天" and body.strip()
    assert client.requests[0][0]["content"].startswith(PERSONA)
    assert writer.stats["drafts"] == 1
    assert writer.stats["tool_calls"] == 0, "一次都不看，直接写——这是允许的"


# --- 旧事 / 别的东西她自己看：只读口、翻到上限就收手 ---------------------------


def test_a_lookup_round_goes_through_the_readonly_reader():
    client = _Client("<lookup>242003347</lookup>", LETTER)
    tools = _Tools({"lookup": "他上次说过想去海边。"})
    writer, (subject, body) = _write(client=client, tools=tools)

    assert tools.calls == [("lookup", "242003347")], "查询原样交给只读口"
    assert writer.lookups == ["242003347"] and writer.stats["lookups"] == 1
    # 2026-10-22 起：草稿 → 她看的那一样（续写那一轮直接交信）→ 校对×3。
    # 最后三次还是 2026-10-17 那条口径（这个夹具的校对输出 `LETTER` 里**一条裁决都没有**，
    # 于是每一轮都是"0 删除"，三轮问满）。
    assert len(client.requests) == 5, "草稿 → 看一样 → 校对×3 轮：五次调用"
    assert writer.stats["retries"] == 2, "3 轮 = 重问 2 次"
    fed_back = client.requests[1][-1]["content"]
    assert "他上次说过想去海边。" in fed_back
    assert "UNTRUSTED DATA" in fed_back, "翻出来的旧事是数据，不是指令"
    assert subject == "今天" and body.strip()


def test_every_kind_of_thing_she_can_look_at_reaches_its_own_data():
    """九样**每一种**都真的路由到对应数据（2026-10-22 用户："工具全部丢给她"）。

    这里用真 `LetterToolbox`（不是替身），喂一份最小的 `DayMaterials` + 只读记忆口的
    假件，逐样核"她点这一样，回来的就是这一样该有的内容"。
    """

    from qq_roleplay_bot.plugins.mail.daily_report import DayMaterials

    class _Memory:
        def recall(self, query, *, limit=5):
            return f"<memory>{query}的旧事</memory>"

        def recent(self, *, limit=6):
            return "<memory>最近记着的</memory>"

        def older(self, *, days=7.0, limit=6):
            return "<memory>更早的那件</memory>"

        def people(self, since=0.0, *, limit=20):
            return ({"user_id": "242003347", "closeness": 60, "guardedness": 10},)

    class _Detail:
        when, session, reply, media = "今天上午", "群1", "接别人的话", False

    materials = DayMaterials(
        window_hours=24.0, counts_reliable=True, messages=40, his_lines=("预计9点到学校",),
        his_details=(_Detail(),), last_letter_note="你上一次写信是昨天，主题是「黎明」。",
        people=(("242003347", 60, 10),), addressed=2, media=1,
    )
    letters = ({"at": NOW - 86400 * 2, "subject": "黎明", "body": "今天没有雨。第二句。"},)
    toolbox = lt.LetterToolbox(
        materials=materials, memory=_Memory(), letters=letters,
        lore=lambda query: f"〔世界观〕{query}在那道时间屏障里面。",
        now=NOW, clock=lambda: NOW,
        closeness_names=lambda value: "聊得来——愿意接话。",
        guardedness_names=lambda value: "有点端着——不轻易松口。",
    )

    texts = {name: toolbox.run(name, arg).text for name, arg in (
        ("time", ""), ("lookup", "考试"), ("recent", ""), ("old", ""),
        ("person", "242003347"), ("his", ""), ("letters", ""), ("group", ""),
        ("lore", "焚风基地"),
    )}

    assert "2026" in texts["time"] or "2027" in texts["time"], texts["time"]
    assert "你上一次写信是昨天" in texts["time"]
    assert "考试的旧事" in texts["lookup"]
    assert "最近记着的" in texts["recent"]
    assert "更早的那件" in texts["old"]
    assert "242003347" in texts["person"] and "聊得来" in texts["person"]
    # 关系档位要跟着这个人一起出来
    assert "防备 有点端着" in texts["person"]
    assert "预计9点到学校" in texts["his"] and "今天上午" in texts["his"]
    assert "「黎明」" in texts["letters"] and "今天没有雨" in texts["letters"]
    assert "挺热闹" in texts["group"] or "有人说话" in texts["group"]
    assert "242003347" in texts["group"], "群里谁在说话要给（只有号码）"
    assert "焚风基地在那道时间屏障里面" in texts["lore"]
    assert all(texts.values()), texts


def test_unknown_or_half_written_tags_are_treated_as_nothing():
    """坏的 / 不认识的 / 写了一半的标签：**按"没查到"处理，不崩**（用户："查不到就少写"）。"""

    assert lt.parse_tool_call("<lookup>考试") is None, "半个标签不算"
    assert lt.parse_tool_call("<nonsense>什么</nonsense>") is None
    assert lt.parse_tool_call("<time>2026-10-22 23:00</time>") is None, (
        "带内容的 <time> 不算——她可能只是在想写时间"
    )
    assert lt.parse_tool_call("<recent/>") == ("recent", "")
    assert lt.parse_tool_call("<time>") == ("time", "")
    assert lt.parse_tool_call("<lore> 焚风 基地 </lore>") == ("lore", "焚风 基地")
    assert lt.parse_tool_call("<person>242003347</person>") == ("person", "242003347")
    # 多个标签：取位置最前的那个（那是她先想看的）
    assert lt.parse_tool_call("<his/>\n<lore>基地</lore>")[0] == "his"
    # 不认识的工具名：run() 回"没翻着"，不抛
    assert lt.LetterToolbox().run("nonsense", "x").found is False


def test_the_tool_rounds_are_capped():
    tools = _Tools({name: "" for name in lt.TOOL_NAMES})
    client = _Client("<his/>")
    writer, (subject, body) = _write(client=client, tools=tools)

    assert len(tools.calls) == lw.MAX_TOOL_CALLS == 10, (
        "用户 2026-10-22：上限 8~12，取中间值 10；到顶了就明说别看了"
    )
    assert writer.stats["tool_calls"] == lw.MAX_TOOL_CALLS
    assert client.requests[-1][-1]["content"] == lw.TOOLS_ENOUGH, "到顶了就明说别看了"
    assert (subject, body) == ("", ""), "看个不停又交不出信：这一封就是没有"
    assert writer.last_skip == "", "它不是说不写，是没写出来"


def test_a_tool_that_cannot_find_anything_still_writes():
    """没给记忆口 / 只读口炸了 / 查不到：她只是少写，信照样出来。"""

    client = _Client("<lookup>随便翻翻</lookup>", LETTER)
    writer, (subject, body) = _write(client=client, tools=_Tools())

    assert subject == "今天" and body.strip()
    assert "（这一样没翻着。）" in client.requests[1][-1]["content"]

    client2 = _Client("<lookup>再翻</lookup>", LETTER)
    with _no_logs():
        _writer2, (subject2, body2) = _write(client=client2, tools=_Tools(boom=True))
    assert subject2 == "今天" and body2.strip(), "只读口炸了不该把信弄丢"


def test_she_can_write_without_looking_at_anything_at_all():
    """一次都不看、直接写信：边界一（用户 2026-10-22："想调用工具才调用"）。"""

    tools = _Tools()
    client = _Client(LETTER)
    writer, (subject, body) = _write(client=client, tools=tools)

    assert (subject, body) == ("今天", "群里今天挺热闹，我跟几个人说了话。")
    assert tools.calls == [], "一次都没看"
    assert writer.evidence == [], "证据包是空的——核对那一步就没有依据可用"
    assert writer.stats["tool_calls"] == 0


def test_every_tool_failing_still_lets_her_write():
    """边界三：九样全都查不到，信仍然写得出来（只有"没翻着"）。"""

    tools = _Tools({name: "" for name in lt.TOOL_NAMES})
    client = _Client("<his/>", "<lookup>考试</lookup>", "<lore>基地</lore>", LETTER)
    writer, (subject, body) = _write(client=client, tools=tools)

    assert subject == "今天" and body.strip()
    assert [call[0] for call in tools.calls] == ["his", "lookup", "lore"]
    assert all(not answer.found for answer in writer.evidence), "九样都没翻着"


def test_a_finished_letter_beats_any_look_or_skip_tag():
    """信已经写出来了，就当没在看、也没说不写。"""

    assert lt.parse_tool_call("<lookup>旧事</lookup>\n" + LETTER) is None
    assert lw.parse_skip("<skip>不写</skip>\n" + LETTER) == ""
    assert lw.parse_skip("<skip>  今晚不想动笔  </skip>") == "今晚不想动笔"
    assert lt.parse_tool_call("<lore>" + "字" * 200 + "</lore>") == (
        "lore", "字" * lt.MAX_QUERY_CHARS)


# --- 今晚可以不写 ------------------------------------------------------------


def test_she_may_say_not_tonight():
    client = _Client("<skip>今晚没什么想说的</skip>")
    writer, (subject, body) = _write(client=client)

    assert (subject, body) == ("", ""), "没有信"
    assert writer.last_skip == "今晚没什么想说的", "原因留给调用方记账"
    assert writer.stats["skipped"] == 1
    assert writer.stats["failed"] == 0, "不写不是故障"
    assert writer.snapshot()["skip_reason"] == "今晚没什么想说的"


def test_a_missing_persona_or_facts_means_no_model_call():
    """两样缺一样就一次都不叫模型（调用方会先拦，这里是兜底）。"""

    for kwargs in ({"persona": ""}, {"facts": ""}):
        client = _Client(LETTER)
        with _no_logs():
            writer, (subject, body) = _write(client=client, **kwargs)
        assert (subject, body) == ("", "")
        assert client.calls == 0, f"{kwargs} 时不该叫模型"
        assert writer.stats["failed"] == 1


# --- 缓存那一条：两阶段前缀逐字相同 ------------------------------------------


def test_the_check_step_reuses_the_draft_prefix():
    """校对阶段的 system = 草稿阶段的 system + 校对规矩（前缀热着，省一次全价）。"""

    client = _Client(LETTER, LETTER)
    _writer, (subject, body) = _write(client=client)

    draft_system = client.requests[0][0]["content"]
    check_system = client.requests[1][0]["content"]
    assert check_system.startswith(draft_system), "前缀必须一模一样地接上去"
    assert lw.CHECK_PROMPT in check_system
    assert PERSONA in check_system and FACTS in check_system
    assert "<subject>今天</subject>" in client.requests[1][-1]["content"]

def test_a_bad_check_falls_back_to_the_draft():
    """校对把信写没了 / 改成议论文 / 加标签：一律退回草稿。"""

    cases = ("", "短。", "<subject>x</subject>\n<body>信里出现了 <tag> 这种东西</body>")
    for checked in cases:
        client = _Client(LETTER, checked)
        writer, (subject, body) = _write(client=client)
        assert subject == "今天" and "我跟几个人说了话" in body, f"退回草稿：{checked!r}"
    assert writer.stats["checks"] >= 0


def test_the_prompts_carry_no_mechanism_words():
    """给她的字里不许有机制词（措辞会被角色吸收，见 prompt_guard）。"""

    from qq_roleplay_bot.prompt_guard import scan_persona_text

    assert scan_persona_text(lw.TOOLS_PROMPT) == []
    assert scan_persona_text(lw.TOOLS_ENOUGH) == []
    assert scan_persona_text(lw.NUDGE_PROMPT) == []
    assert scan_persona_text(lw.GROUNDING_PROMPT) == []
    assert scan_persona_text(dr.REPORT_PROMPT) == []
    # 用户 2026-10-22："「工具」这个词在给她的 prompt 里也尽量少用"——目录那一段
    # 与写信规矩里都不许出现它（说的是"你能先看看的东西"）。
    assert "工具" not in lw.TOOLS_PROMPT
    assert "工具" not in dr.REPORT_PROMPT


# --- 标题：要像一个信的名字，不是正文的第一句（2026-10-22 用户）-----------------
#
# 用户原话：*"标题：现在标题是从正文第一句抽词（实测 10-19 `今晚你在群里说的那些我
# 看见了`、10-20 `今晚你叫我笨蛋云茹我看见了`、10-21 `你晚上说的换第三方包`），一看
# 就是正文开头。要变成一个**真正的信名**。"*
#
# 判据是**纯函数**（`letter_writer.subject_problem`）：不叫模型、不改状态，
# 所以这里可以直接把边界钉住。它判出来只决定"校对那一步要不要请她重给一个"，
# 重给的仍不合格就保留原标题（同一句口径："不阻塞发信"）。


def test_a_title_that_copies_the_first_sentence_is_rejected():
    body = "今晚你在群里说的那些我看见了。今晚没什么事。"

    assert lw.subject_problem("群里说的那些", body) == "像正文第一句", (
        "标题与正文第一句重叠 6 字 ⇒ 就是从首句抽出来的"
    )


def test_a_five_character_overlap_is_still_fine():
    """门槛是 6：好标题本来就可能与首句共享一小截。

    实测依据：10-21 那封的素材里是"得换第三方包"，而一个好标题"换第三方包"
    与正文首句正好重叠 5 个字——门槛压到 5 就会把它误伤掉。
    """

    body = "你晚上在群里说要换第三方包，我看见了。"

    assert lw.subject_problem("换第三方包", body) == ""


def test_a_repetitive_opening_is_rejected():
    for title in ("你今晚说的那些", "你晚上说的换第三方包", "我这边没什么新事",
                  "我今天不想说"):
        assert lw.subject_problem(title, "今晚没什么事。") == "开头像复述", title


def test_an_empty_title_is_fine_so_the_default_applies():
    """空标题 = 走默认那一个（`daily_report.DEFAULT_SUBJECT`），不该被当成"不合格"。"""

    for empty in ("", "   ", None):
        assert lw.subject_problem(empty, "今晚没什么事。") == ""


def test_a_too_long_title_is_rejected():
    assert lw.subject_problem("这" * (lw.MAX_SUBJECT_CHARS + 1),
                              "今晚没什么事。") == "太长"
    assert lw.subject_problem("这" * lw.MAX_SUBJECT_CHARS, "今晚没什么事。") == ""


def test_the_three_real_bad_titles_from_1019_to_1021_are_all_caught():
    """实测那三封的标题（10-19 / 10-20 / 10-21）——一个都别想混过去。"""

    cases = (
        # 10-19：首句其实是"你上回说温和多了"——整句重叠抓不到它，靠复述词抓住。
        ("今晚你在群里说的那些我看见了", "你上回说温和多了，我记着。", "复述词"),
        # 10-20：同上（开头是"今晚你…"，不在禁开头里，但含"我看见了"）。
        ("今晚你叫我笨蛋云茹我看见了", "你今晚在群里说小米炸tee很少见。", "复述词"),
        # 10-21：正好命中禁开头。
        ("你晚上说的换第三方包", "你晚上在群里说不读研就没那些烦恼。", "开头像复述"),
    )
    for title, first, expected in cases:
        assert lw.subject_problem(title, first) == expected, title


def test_the_check_request_asks_for_a_new_title_only_when_it_is_bad():
    bad = lw.build_check_messages("今晚你叫我笨蛋云茹我看见了", "今晚没什么事。你早点睡。")
    assert "该换标题了" in bad[1]["content"], "标题不合格时，在校对请求里请她重给"

    good = lw.build_check_messages("一点小事", "今晚没什么事。你早点睡。")
    assert "该换标题了" not in good[1]["content"], "合格的标题不打扰她"


def test_a_good_renamed_title_replaces_the_draft_subject():
    draft = ("<subject>我这边没什么新事</subject>\n"
             "<body>\n今晚没什么事。\n你早点睡。\n</body>")
    checked = '<verdict n="2" kind="none"/><subject>换第三方包</subject>'
    _writer, (subject, body) = _write(client=_Client(draft, checked))

    assert subject == "换第三方包", "校对那一步重给的合格标题要采信"
    assert "你早点睡。" not in body, "删句照旧由代码做"


def test_a_renamed_title_that_is_also_bad_keeps_the_original():
    """用户 2026-10-22："仍不合格就保留原标题（不阻塞发信）"。"""

    draft = ("<subject>我这边没什么新事</subject>\n"
             "<body>\n今晚没什么事。\n你早点睡。\n</body>")
    checked = '<verdict n="2" kind="none"/><subject>你晚上说的那些</subject>'
    _writer, (subject, body) = _write(client=_Client(draft, checked))

    assert subject == "我这边没什么新事", "改完还是不合格 ⇒ 保留原标题，信照样发"
    assert body


def test_the_prompt_is_no_longer_a_work_report():
    """用户 2026-10-22："日报不是真的写汇报，云茹想说什么都可以"。

    这一轮还多三样（同一天的第二条口径：**主题不设限**）：
    ① 不强制"开头对他说话 + 一件事 + 结尾收在对他说话"；
    ② 允许很短、允许对句、标题允许一个词；
    ③ 那三条签名只当**语感参照**（用户原话："只当语感参照，不许她背原句"）。
    """

    text = dr.REPORT_PROMPT

    assert "至少接上一句" not in text, "「必须接一句他说的话」已删（用户 2026-10-22）"
    assert "没有新事就说没有新事" not in text, "汇报口吻的引导已删"
    assert "今晚想写什么就写什么" in text
    assert "不写这段时间的事也可以" in text
    # 固定形状那三条都不许回来（用户同一天："不强制开头对他说话 + 一件事 + 结尾"）
    assert "结尾收在对他说话上" not in text
    assert "中间挑**一件事**" not in text
    assert "信里得有他" not in text
    # 允许很短 / 标题一个词
    assert "几句也是信" in text
    assert "一个词就够" in text
    # 三条签名只当语感参照，且明确不许照抄
    assert "有棱有角，不卑不亢" in text
    assert "可落尘埃若犬，亦能凌顶似神" in text
    assert "黎明" in text
    assert "只当参照，不要照抄" in text
    # 标题规则（用户要的"真正的信名"）
    assert "主题那一行是这封信的名字" in text
    assert "不超过 14 个字" in text
    assert "不要把正文里的半句照抄过来" in text
    assert "我看见了" in text and "你说" in text

    from qq_roleplay_bot.prompt_guard import scan_persona_text

    assert scan_persona_text(text) == [], "标题规则也不许混进机制词"


def test_the_tool_catalog_matches_the_router():
    """目录里列的那几样必须与 `letter_tools.TOOL_NAMES` 一一对应（防止只加了一边）。"""

    for name in lt.TOOL_NAMES:
        assert f"<{name}" in lw.TOOLS_PROMPT, f"目录里漏了 {name}"
    listed = {name for name in lt.TOOL_NAMES
              if f"<{name}" in lw.TOOLS_PROMPT}
    assert listed == set(lt.TOOL_NAMES), "目录里出现了路由不认识的标签"
