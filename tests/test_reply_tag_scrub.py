"""出站正文的**保守净化**：剥掉"孤立的标签片段"，但不许吃掉正常文本。

由来（2026-10-09 用户实测，有原文）：群里收到的一条正文尾巴上挂着 `</parameter>`
——模型那一轮吐了半截工具调用那种 XML，解析只取到了前面的正文，收尾标签留在了
正文里，于是它跟着发了出去（400 条里 1 条，罕见但用户看得见）。

这里钉住三件事：

1. 那条**真实原文**净化之后正好是前面那段，而且计数 +1；
2. **只剥按名字认得出来的**标签（我们自己的信封 + 模型侧工具调用那一族）——
   `<div>`、`a<b` 这类正常尖括号文本一个字都不许动（判据见 `security.py`）；
3. 剥掉了要**看得见**：进 `EngineSnapshot.reply_tags_scrubbed` 与 `/super status`，
   且历史里记的是净化后的正文。
"""
import asyncio

from qq_roleplay_bot.api_usage import COUNTER_NAMES
from qq_roleplay_bot.security import (
    OUTBOUND_TAG_NAMES,
    TOOL_CALL_TAG_NAMES,
    scrub_outbound_tags,
)
from qq_roleplay_bot.stage3_main import DialogueEngine
from qq_roleplay_bot.transport import IncomingMessage, MessageTarget

GROUP = "717151356"
SUPER = "900000001"
TARGET = MessageTarget(group_id=GROUP)

#: 用户实测那条的原文：正文 + 半截工具调用 XML 的收尾标签。
REAL_REPLY = "ID摆在那儿，不叫她叫什么。</parameter>"
REAL_BODY = "ID摆在那儿，不叫她叫什么。"


class ScriptedClient:
    """脚本化的模型输出；记录收到的请求。"""

    def __init__(self, output: str) -> None:
        self.output = output
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        return self.output


def reply_output(body: str) -> str:
    return f"<decision>REPLY</decision><dialogue>KEEP</dialogue><reply>{body}</reply>"


def group_message(text: str, index: int = 1, *, mentioned: bool = False) -> IncomingMessage:
    return IncomingMessage(
        message_id=f"m{index}",
        session_id=f"group:{GROUP}",
        user_id="100",
        text=text,
        target=TARGET,
        sender_name="某人",
        is_bot_mentioned=mentioned,
    )


# --- 1. 那条真实原文 ---------------------------------------------------------


def test_the_real_reply_loses_exactly_the_orphan_tail() -> None:
    """`ID摆在那儿，不叫她叫什么。</parameter>` → 正好是前面那段，计数 +1。"""

    result = scrub_outbound_tags(REAL_REPLY)
    assert result.text == REAL_BODY
    assert result.removed == ("parameter",)


def test_scrubbing_is_idempotent() -> None:
    once = scrub_outbound_tags(REAL_REPLY)
    twice = scrub_outbound_tags(once.text)
    assert twice.text == once.text
    assert twice.removed == ()


def test_the_engine_strips_it_before_sending_and_counts_once() -> None:
    """端到端：发出去的是净化后的正文，计数 +1，历史里也是净化后的正文。"""

    async def run() -> str:
        engine = DialogueEngine(
            ScriptedClient(reply_output(REAL_REPLY)),
            super_admin_user_ids=frozenset({SUPER}),
        )
        sent = await engine.handle(group_message("@YunRu 在吗", mentioned=True))
        snapshot = engine.snapshot()
        assert sent is not None and sent.text == REAL_BODY

        assert snapshot.reply_tags_scrubbed == 1, "剥掉了一个标签片段"
        assert snapshot.replies == 1
        # 语义一个字没动：照样是一次 REPLY，也不算"回复空手"。
        assert snapshot.empty_forced_reply == 0

        state = engine.sessions.state(f"group:{GROUP}")
        bot_lines = [item.text for item in state.history if item.is_bot_message]
        assert bot_lines and bot_lines[-1] == REAL_BODY, bot_lines

        status = await engine.handle(IncomingMessage(
            "m:status", f"group:{GROUP}", SUPER, "/super status", TARGET))
        return status.text

    status_text = asyncio.run(run())
    assert "孤立标签片段 1 个" in status_text, status_text


# --- 2. 不许吃掉正常文本 -----------------------------------------------------


def test_normal_angle_bracket_text_is_never_touched() -> None:
    """她聊代码写 `<div>`、写数学 `a<b`，都不该被这里碰到。"""

    for text in (
        "这个用 <div> 包一层就行。",
        "<div>标题</div>",
        "看看这个 <div>",
        "a<b 且 c>d，这是数学不是标签。",
        '她写的是 <span style="color:red">红字</span>。',
        "if a < b: print(a)",
        "把 <br> 换成空行。",
        "他打的是 <p> 不是 <div>。",
        "1 < 2 > 0 恒成立。",
    ):
        result = scrub_outbound_tags(text)
        assert result.text == text, text
        assert result.removed == (), text


def test_the_engine_leaves_an_ordinary_reply_alone() -> None:
    body = "这个用 <div> 包一层，a<b 也是这么写的。"
    engine = DialogueEngine(ScriptedClient(reply_output(body)))
    result = asyncio.run(engine.handle(group_message("@YunRu 在吗", mentioned=True)))

    assert result is not None and result.text == body
    assert engine.snapshot().reply_tags_scrubbed == 0


def test_the_known_names_are_pinned_by_name_not_by_shape() -> None:
    """名单钉死：往里加一个通用词（`div` / `b` / `a`）就会红。

    判据是"**只剥已知标签名**"，所以名单本身就是这条保证的一部分：
    它一旦被放宽成"形状像标签就剥"，`<div>` 与 `a<b` 立刻会被吃掉。
    """

    assert OUTBOUND_TAG_NAMES == frozenset(
        "decision dialogue reply reply_to context "
        "route topic topic_start related lore understood specialist guard guard_reason "
        "remind "
        "people history message text earlier_summary current_event current_message "
        "extra_prompt plugin knowledge memory".split()
    )
    assert TOOL_CALL_TAG_NAMES == frozenset(
        "parameter parameters function_calls function_call invoke invokes "
        "tool_calls tool_call tool_name tool_use tool_result tool_call_parameter antml".split()
    )
    for generic in ("div", "span", "b", "a", "p", "br", "pre", "code", "html", "img", "svg"):
        assert generic not in OUTBOUND_TAG_NAMES, generic
        assert generic not in TOOL_CALL_TAG_NAMES, generic


# --- 3. 认得出来的名字都要能剥 -----------------------------------------------


def test_every_known_name_is_scrubbed_in_every_orphan_shape() -> None:
    """每个已知名字：开标签、闭标签、带属性、自闭合，四种形状都要剥掉。"""

    for name in sorted(OUTBOUND_TAG_NAMES | TOOL_CALL_TAG_NAMES):
        for tail in (f"<{name}>", f"</{name}>", f'<{name} k="v">', f"<{name}/>",
                     f"<{name} k='v'>"):
            result = scrub_outbound_tags(f"正文{tail}")
            assert result.text == "正文", (name, tail, result)
            assert result.removed == (name,), (name, tail, result)


def test_only_the_tag_is_removed_never_the_content_between() -> None:
    """抠掉的只有标签本身：夹在中间的话是她真正要说的话，一个字都不许丢。"""

    result = scrub_outbound_tags("<history>她昨天说过那句话</history>")
    assert result.text == "她昨天说过那句话"
    assert result.removed == ("history", "history")


def test_a_truncated_tail_fragment_is_scrubbed_only_when_the_name_is_ours() -> None:
    """模型输出被截断时的半截标签：名字认得出来才剥。"""

    assert scrub_outbound_tags("那就不说了</reply").text == "那就不说了"
    assert scrub_outbound_tags("那就不说了</reply\n").text == "那就不说了"
    assert scrub_outbound_tags('开头<parameter name="text"').text == "开头"
    # 名字不是我们的 → 半截也不动（宁可留着那个尾巴，也不许切错）。
    assert scrub_outbound_tags("这个标签叫</unknown").text == "这个标签叫</unknown"
    # 名字后面跟的是正文而不是属性表 → 不当成标签（`<memory 就行`）。
    assert scrub_outbound_tags("变量名写 <memory 就行").text == "变量名写 <memory 就行"


def test_size_and_namespace_variants_are_recognised() -> None:
    assert scrub_outbound_tags("正文</PARAMETER>").text == "正文"
    assert scrub_outbound_tags("正文</antml:parameter>").text == "正文"
    assert scrub_outbound_tags("正文</Parameter>").text == "正文"


# --- 4. 计数看得见 -----------------------------------------------------------


def test_the_counter_is_on_the_snapshot_and_in_the_ledger() -> None:
    engine = DialogueEngine(ScriptedClient(reply_output("在。")))
    assert engine.snapshot().reply_tags_scrubbed == 0
    assert "reply_tags_scrubbed" in COUNTER_NAMES


def test_scrubbing_never_touches_an_empty_or_missing_body() -> None:
    assert scrub_outbound_tags("").text == ""
    assert scrub_outbound_tags("").removed == ()
    # 标签被剥光之后只剩空串——由引擎那条"空手而归"的既有判据接手，不在这里编语义。
    assert scrub_outbound_tags("</reply>").text == ""
    assert scrub_outbound_tags("</reply>").removed == ("reply",)


if __name__ == "__main__":
    import unittest

    unittest.main()
