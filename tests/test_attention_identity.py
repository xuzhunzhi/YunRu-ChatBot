"""注意力与身份：群里"谁在跟谁说话"必须写进 prompt，话题锚不能发霉。

两个真实缺陷（2026-09-28 用户在群里报的）：

1. **把不同的人认成一个人。** 两个信息以前根本没进 prompt：
   `at` 段（@ 了谁）被解析层丢掉了，`reply_to` 渲染的是 OneBot 原始消息 ID
   （历史里的消息却按 seq 编号，对不上）。用户原话：
   "你怎么分不清我艾特别人的消息和回复你的消息啊"。
2. **话题已经转移还在强推先前话题。** 回复段自报的 `topic_status` 35 轮里只有 1 次是
   shifted，而判定每轮都给新的 `<topic>`/`<related>`；引擎当时只信回复段、且只在
   shifted/ended 时才清未了问题，于是"体温量了吗"在群里聊到"明天吃什么"之后又问了四轮。
"""
import asyncio

from qq_roleplay_bot.dialogue_judge import build_judge_messages
from qq_roleplay_bot.stage3_main import DialogueEngine
from qq_roleplay_bot.stage3_runtime import (ConversationMode, ContextState, _format_history,
                                            build_dialogue_messages)
from qq_roleplay_bot.transport import IncomingMessage, MessageTarget

GROUP = "717151356"
TARGET = MessageTarget(group_id=GROUP)


def msg(index, text="测试", *, user_id="100", at=(), name="某人", reply_to="", mentioned=False):
    return IncomingMessage(
        message_id=f"m{index}", session_id=f"group:{GROUP}", user_id=user_id,
        text=text, target=TARGET, sender_name=name, is_bot_mentioned=mentioned,
        mentioned_user_ids=tuple(at), reply_to_message_id=reply_to,
    )


def render(messages, *, seq_of=None, aliases=None, lookup=None):
    return _format_history(messages, seq_of=seq_of or (lambda m: int(m.message_id[1:])),
                           alias_of=lambda m: None if m.is_bot_message else ALIASES.get(m.user_id),
                           aliases=aliases or ALIASES, lookup=lookup)


ALIASES = {"100": 1, "200": 2, "300": 3}


# --- 1. @ 到了谁 -------------------------------------------------------------


def test_at_targets_are_visible_to_both_agents() -> None:
    """@ 了别人和 @ 了她，模型必须看得出来（以前 `at` 段在解析层就被丢了）。"""

    rows = [
        msg(1, "小B 你昨天说的那个", user_id="200", at=("200",), name="小B"),
        msg(2, "在吗", user_id="300", at=("300",), name="某人甲"),
        msg(3, "云茹你看看", user_id="100", mentioned=True),
        msg(4, "顺便说一下", user_id="100", at=("200",), mentioned=True),
    ]
    rendered = render(rows)
    assert 'at="2"' in rendered, "自己 @ 自己被过滤掉，剩下的就是名册编号"
    assert 'at="3"' in rendered
    # @ 她时既有 at="yunru" 也有沿用已久的 mentioned="true"
    assert 'at="yunru"' in rendered and 'mentioned="true"' in rendered
    assert 'at="2,yunru"' in rendered, "既 @ 了别人又 @ 了她，两个都要写"
    assert 'at="yunru"' in build_judge_messages(
        rows, current=rows[-1], trigger="mention", addressed=True, aliases=ALIASES
    )[1]["content"], "判定也要看得到这一条 @ 的是谁"


def test_unknown_at_target_falls_back_to_qq() -> None:
    """名册里没有的人也要标出来——"这条 @ 的是别人"本身就是关键信息。"""

    rendered = render([msg(1, "喂", user_id="100", at=("424242",))])
    assert 'at="QQ424242"' in rendered


# --- 2. 引用的是谁的第几条 ---------------------------------------------------


def test_quote_refers_to_speaker_and_number_not_an_opaque_id() -> None:
    """引用要写成"谁的第几条"，不能是 OneBot 的原始消息 ID（对不上号）。

    2026-09-29 起还会跟上被引用那句的开头几个字：只有编号的话，编号一旦不在
    她眼前的窗口里，她只看到一个数字，等于还是不知道"谁说了什么"。
    """

    rows = [
        msg(10, "我周末去爬山", user_id="200", name="小B"),
        msg(11, "带上我", user_id="100", reply_to="m10"),
    ]
    rendered = render(rows)
    assert 'reply_to="2/10 我周末去爬山"' in rendered, rendered
    assert "m10" not in rendered, "原始消息 ID 不该出现在 prompt 里"


def test_quote_outside_the_topic_slice_still_names_speaker_and_text() -> None:
    """话题起点之前、但还在会话窗口里的消息，被引用时要能认出是谁说的。

    这条对应 2026-09-29 查到的真实误认：邦邦说"我电脑没电关机了"，几轮之后
    她对着另一个人说"你电脑刚好没电"——因为被引用的那两条消息都渲染成
    "（不在当前上下文里）"，她只能拿此刻说话的人当前提。
    """

    full = [
        msg(10, "我电脑没电关机了", user_id="200", name="邦邦"),
        msg(11, "那你先记着", user_id="100"),
        msg(12, "没电的是我的电脑", user_id="200", name="邦邦", reply_to="m10"),
    ]
    topic_slice = full[1:]  # 话题起点把第 10 条挡在渲染之外，它仍在窗口里
    rendered = render(topic_slice, lookup=full)
    history_part = rendered.split("<history>")[-1].split("</history>")[0]
    assert history_part.count("<message") == 2, "渲染的仍然只有话题切片里的两条"
    assert 'reply_to="2/10 我电脑没电关机了"' in rendered, rendered


def test_quote_of_unknown_speaker_never_renders_none() -> None:
    """名册里查不到编号时写 `?`：`None` 是程序词，会被角色当成话学走。"""

    rows = [
        msg(10, "我是没登记的人", user_id="999"),
        msg(11, "回一句", user_id="100", reply_to="m10"),
    ]
    rendered = render(rows)
    assert "None" not in rendered, rendered
    assert 'reply_to="?/10 我是没登记的人"' in rendered, rendered


def test_quote_excerpt_does_not_smuggle_escaped_quotes() -> None:
    """摘录进的是属性值：半角引号要换掉，不能让 `&quot;` / `&#x27;` 出现在 prompt 里被照抄。"""

    rows = [
        msg(10, '他说"这不行"', user_id="200", name="小B"),
        msg(11, "是吗", user_id="100", reply_to="m10"),
    ]
    rendered = render(rows)
    assert "&quot;" not in rendered and "&#x27;" not in rendered, rendered
    assert 'reply_to="2/10 他说”这不行”"' in rendered, rendered


def test_quote_outside_the_window_is_labelled() -> None:
    rendered = render([msg(11, "还是那句话", user_id="100", reply_to="m999")])
    assert 'reply_to="（不在当前上下文里）"' in rendered


def test_current_message_also_carries_at_and_quote() -> None:
    """当前这条才是最要紧的：她要判断"这是不是对我说的"，靠的就是它自己的标记。"""

    history = [msg(10, "我周末去爬山", user_id="200", name="小B")]
    current = msg(11, "带上我", user_id="100", reply_to="m10")
    request = build_dialogue_messages(
        history, current=current, mode=ConversationMode.ACTIVE, trigger="active_message",
        context=ContextState(), group_chat=True,
        alias_of=lambda m: ALIASES.get(m.user_id), aliases=ALIASES,
        seq_of=lambda m: int(m.message_id[1:]),
    )
    volatile = request[2]["content"]
    # 小B 是名册里的 2，被引用的是他的第 10 条
    assert 'reply_to="2/10 我周末去爬山"' in volatile, volatile[-400:]

    quoted_her = msg(12, "接着说", user_id="100", at=("200",))
    request = build_dialogue_messages(
        history, current=quoted_her, mode=ConversationMode.ACTIVE, trigger="active_message",
        context=ContextState(), group_chat=True,
        alias_of=lambda m: ALIASES.get(m.user_id), aliases=ALIASES,
        seq_of=lambda m: int(m.message_id[1:]),
    )
    assert 'at="2"' in request[2]["content"], request[2]["content"][-200:]


def test_current_quote_can_be_resolved_from_the_whole_window() -> None:
    """当前这条引用的消息在话题起点之前时，也要靠完整窗口把"谁说了什么"写出来。"""

    full = [
        msg(10, "我电脑没电关机了", user_id="200", name="邦邦"),
        msg(11, "你先记着", user_id="100"),
    ]
    current = msg(12, "你电脑怎么没电了", user_id="100", reply_to="m10")
    request = build_dialogue_messages(
        [full[1]], current=current, mode=ConversationMode.ACTIVE, trigger="active_message",
        context=ContextState(), group_chat=True,
        alias_of=lambda m: ALIASES.get(m.user_id), aliases=ALIASES,
        seq_of=lambda m: int(m.message_id[1:]), lookup=full,
    )
    volatile = request[2]["content"]
    assert 'reply_to="2/10 我电脑没电关机了"' in volatile, volatile[-400:]


def test_judge_sees_the_same_quote_label() -> None:
    rows = [
        msg(10, "我周末去爬山", user_id="200", name="小B"),
        msg(11, "带上我", user_id="100", reply_to="m10"),
    ]
    user = build_judge_messages(rows, current=rows[-1], trigger="mention", addressed=True,
                                aliases=ALIASES, alias_of=lambda m: ALIASES.get(m.user_id),
                                seq_of=lambda m: int(m.message_id[1:]))[1]["content"]
    assert 'reply_to="2/10 我周末去爬山"' in user, user[-400:]


# --- 3. 每条都写 who ---------------------------------------------------------


def test_every_message_names_its_speaker() -> None:
    """同一个人的第二条也要写 who：身份不能再靠"顺移"（顺错就认成一个人）。"""

    rows = [msg(1, "甲", user_id="100"), msg(2, "甲又说", user_id="100"),
            msg(3, "乙说", user_id="200")]
    lines = render(rows).splitlines()
    assert 'who="1"' in lines[0] and 'who="1"' in lines[1]
    assert 'who="2"' in lines[2]


# --- 4. 话题锚与未了问题 -----------------------------------------------------


def test_main_partner_is_rendered_as_a_roster_number() -> None:
    """main_partner 给名册编号，不给 QQ 号（号码要二次查表，容易认错人）。"""

    current = msg(3, "在吗", user_id="100")
    request = build_dialogue_messages(
        [current], current=current, mode=ConversationMode.ACTIVE, trigger="mention",
        context=ContextState(), active_user_id="100", group_chat=True,
        alias_of=lambda m: ALIASES.get(m.user_id), aliases=ALIASES,
    )
    volatile = request[2]["content"]
    assert "main_partner=1" in volatile
    assert "main_partner=100" not in volatile


class _Judge:
    def __init__(self, output):
        self.output = output

    async def complete(self, request):
        return self.output


class _Reply:
    def __init__(self, output):
        self.output = output

    async def complete(self, request):
        return self.output


def test_judge_topic_refreshes_the_context_even_when_she_stays_quiet() -> None:
    """判定每轮都读到话题；它给的新话题要覆盖带下去的旧标签。"""

    judge = _Judge('<route>NO_REPLY</route><topic>音箱音质</topic><topic_start>11</topic_start>'
                   '<related>NO</related>')
    engine = DialogueEngine(_Reply("<decision>REPLY</decision><reply>嗯。</reply>"),
                            judge_client=judge)
    asyncio.run(engine.handle(msg(10, "@她 先聊这个", user_id="200", mentioned=True)))
    state = engine.sessions.state(f"group:{GROUP}")
    state.context = ContextState(topic="着凉发烧", pending_question="体温量了吗")
    asyncio.run(engine.handle(msg(11, "@她 低音糊", user_id="200", mentioned=True)))
    assert state.context.topic == "音箱音质", "带下去的旧话题标签要被判定这一轮覆盖"
    assert state.context.pending_question == "无", "判定说这一句不在原来那条线上，旧问题作废"


def test_related_yes_keeps_the_pending_question() -> None:
    """对照：判定说还在同一条线上时，未了问题要留着（那是她该追问的）。"""

    judge = _Judge('<route>NO_REPLY</route><topic>着凉发烧</topic><topic_start>10</topic_start>'
                   '<related>YES</related>')
    engine = DialogueEngine(_Reply("<decision>REPLY</decision><reply>嗯。</reply>"),
                            judge_client=judge)
    asyncio.run(engine.handle(msg(10, "@她 我有点发烧", user_id="200", mentioned=True)))
    state = engine.sessions.state(f"group:{GROUP}")
    state.context = ContextState(topic="着凉发烧", pending_question="体温量了吗")
    asyncio.run(engine.handle(msg(11, "@她 还是难受", user_id="200", mentioned=True)))
    assert state.context.pending_question == "体温量了吗"
