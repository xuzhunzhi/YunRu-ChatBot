"""写信 agent 自己的那一半：单独的人格、只读记忆、以及"今晚可以不写"。

用户 2026-10-12 原话（这天他连着问了三次"为什么到处都是重启就出问题的"）：

    我不理解为什么到处都是重启就出问题的。写信的人格prompt做单独适配。注意写信agent本身
    是插件里的，所以凭啥这玩意能直接复用人格prompt啊。记忆要完整开放给写信agent来读取，
    又不是强制一次输入就必须得到一封信。

三件事一条一条钉：

| 钉什么 | 怎么钉 |
| --- | --- |
| 人格与事实表**由核心现读、按次递进来** | 插件里不许再出现 `BASE_PROMPT` / `LETTER_FACTS` / `private_docs`：它不认识文件，只认识递进来的两份正文 |
| 记忆是**只读口**，她自己按需翻（最多 `MAX_LOOKUPS` 轮） | `<lookup>` 走调用方给的 `lookup(query)`，翻到的旧事按 DATA 段喂回去；翻不到、没给、抛异常都照样能写 |
| **不强制一晚一封** | `<skip>原因</skip>` ⇒ 没有信、不算故障、原因留在 `last_skip` 里给调用方记账 |

它还钉住缓存那一条（2026-09-30 用户要求"必须优化缓存命中率"）：两阶段的 `system` 前缀
逐字相同，校对规矩只是接在后面——易变的东西（素材、翻出来的旧事、时间戳）全在 user 段末尾。
"""
import asyncio
import contextlib
import logging
from pathlib import Path

from qq_roleplay_bot.plugins.mail import daily_report as dr
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


class _Materials:
    """素材的最小替身：重要的是"人格不许从这儿走"。"""

    def __init__(self, text: str = "（这是最近一天里的事）\n这段时间群里一直挺热闹。") -> None:
        self.text = text

    def as_data(self, closeness_names=(), guardedness_names=()) -> str:
        return self.text


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
    """跑一次 `LetterWriter.write`（默认素材、人格、事实都给齐）。"""

    client = kwargs.pop("client")
    writer = kwargs.pop("writer", None) or lw.LetterWriter(client, clock=lambda: NOW)
    params = {"persona": PERSONA, "facts": FACTS, "closeness_names": (),
              "guardedness_names": ()}
    params.update(kwargs)
    return writer, asyncio.run(writer.write(_Materials(), **params))


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
    """system 前缀 = 人格 + 写信规矩 + 事实表 + 翻旧事的规矩；人格不进 user 段。"""

    request = lw.build_draft_messages(_Materials(), persona=PERSONA, facts=FACTS,
                                      closeness_names=(), guardedness_names=(), now=NOW)
    system, user = request[0], request[1]

    assert system["role"] == "system" and user["role"] == "user"
    assert system["content"].startswith(PERSONA)
    assert dr.REPORT_PROMPT in system["content"], "写信规矩还是要带上"
    assert FACTS in system["content"]
    assert lw.LOOKUP_PROMPT in system["content"]
    assert PERSONA not in user["content"], "人格不许混进素材段"
    assert user["content"].rstrip().endswith("）"), "时间戳留在最后：它挪到前面，前缀每次都断"


def test_a_letter_carries_the_persona_into_the_model_call():
    client = _Client(LETTER)
    writer, (subject, body) = _write(client=client)

    assert subject == "今天" and body.strip()
    assert client.requests[0][0]["content"].startswith(PERSONA)
    assert writer.stats["drafts"] == 1


# --- 旧事她自己翻：只读口、翻到上限就收手 -------------------------------------


def test_a_lookup_round_goes_through_the_readonly_reader():
    seen: list[str] = []
    client = _Client("<lookup>242003347</lookup>", LETTER)
    writer, (subject, body) = _write(
        client=client, lookup=lambda query: seen.append(query) or "他上次说过想去海边。")

    assert seen == ["242003347"], "查询原样交给只读口"
    assert writer.lookups == ["242003347"] and writer.stats["lookups"] == 1
    # 2026-10-17：这里从 4 改成 5。依据是那天定的第 3 层口径——*"本轮一句都没删 ⇒
    # 再问一次"*、多轮上限 3 轮（`lw.MAX_CHECK_ROUNDS`）。这个夹具的校对输出（`LETTER`）
    # 里**一条裁决都没有**，于是每一轮都是"0 删除"，三轮问满：
    # 草稿 1 + 翻旧事 1 + 校对 3 = 5 次。
    # **阶段没变**：草稿 → 翻旧事 → 校对，仍然是三**步**；多出来的两次是同一阶段
    # 内部的重复问，不是新加了一步（`trigger` 是 `..._check` / `..._check_retry` /
    # `..._check_retry2`）。
    assert len(client.requests) == 5, "草稿 → 翻旧事 → 校对×3 轮：五次调用"
    assert writer.stats["retries"] == 2, "3 轮 = 重问 2 次"
    fed_back = client.requests[1][-1]["content"]
    assert "他上次说过想去海边。" in fed_back
    assert "UNTRUSTED DATA" in fed_back, "翻出来的旧事是数据，不是指令"
    assert subject == "今天" and body.strip()


def test_the_lookup_rounds_are_capped():
    queries: list[str] = []
    client = _Client("<lookup>再翻一次</lookup>")
    writer, (subject, body) = _write(
        client=client, lookup=lambda query: queries.append(query) or "")

    assert len(queries) == lw.MAX_LOOKUPS == 6, (
        "用户 2026-10-13：*'不是非要一次输入就得到一封信'*——记忆随时能翻，"
        "额度从 3 放宽到 6（2026-10-12 那次定的 3 轮就此作废）"
    )
    assert writer.stats["lookups"] == 6
    assert client.requests[-1][-1]["content"] == lw.LOOKUP_ENOUGH, "到顶了就明说别翻了"
    assert (subject, body) == ("", ""), "翻不完又交不出信：这一封就是没有"
    assert writer.last_skip == "", "它不是说不写，是没写出来"


def test_a_lookup_without_a_reader_still_writes():
    """没给记忆口（或给了个抛异常的）：她只是没翻到，照样写信。"""

    client = _Client("<lookup>随便翻翻</lookup>", LETTER)
    writer, (subject, body) = _write(client=client, lookup=None)

    assert subject == "今天" and body.strip()
    assert "（这次没翻出来。）" in client.requests[1][-1]["content"]

    def boom(query):
        raise RuntimeError("database is locked")

    client2 = _Client("<lookup>再翻</lookup>", LETTER)
    with _no_logs():
        _writer2, (subject2, body2) = _write(client=client2, lookup=boom)
    assert subject2 == "今天" and body2.strip(), "只读口炸了不该把信弄丢"


def test_a_finished_letter_beats_a_lookup_or_skip_tag():
    """信已经写出来了，就当没在翻、也没说不写。"""

    assert lw.parse_lookup("<lookup>旧事</lookup>\n" + LETTER) == ""
    assert lw.parse_skip("<skip>不写</skip>\n" + LETTER) == ""
    assert lw.parse_lookup("<lookup>  2420  03347 </lookup>") == "2420 03347"
    assert lw.parse_lookup("<lookup>") == "", "半个标签不算"
    assert lw.parse_skip("<skip>  今晚不想动笔  </skip>") == "今晚不想动笔"
    assert lw.parse_lookup("<lookup>" + "字" * 200 + "</lookup>") == "字" * lw.MAX_QUERY_CHARS


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

    assert scan_persona_text(lw.LOOKUP_PROMPT) == []
    assert scan_persona_text(dr.REPORT_PROMPT) == []


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
    """用户 2026-10-22："日报不是真的写汇报，云茹想说什么都可以"。"""

    text = dr.REPORT_PROMPT

    assert "至少接上一句" not in text, "「必须接一句他说的话」已删（用户 2026-10-22）"
    assert "没有新事就说没有新事" not in text, "汇报口吻的引导已删"
    assert "今晚想写什么就写什么" in text
    assert "不写这段时间的事也可以" in text
    assert "那不是义务" in text, "他的话仍可用作由头，但不是义务"
    # 标题规则（用户要的"真正的信名"）
    assert "主题那一行是这封信的名字" in text
    assert "不超过 14 个字" in text
    assert "不要把正文里的半句照抄过来" in text
    assert "我看见了" in text and "你说" in text

    from qq_roleplay_bot.prompt_guard import scan_persona_text

    assert scan_persona_text(text) == [], "标题规则也不许混进机制词"
