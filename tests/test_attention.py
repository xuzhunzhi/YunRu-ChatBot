"""attention 模块的离线覆盖。

"是否被叫到"直接决定要不要为一条消息付出一次模型调用，所以边界必须测死：
宁可漏判，也不能因为正文里出现常见词就误判成在叫她。
"""
from qq_roleplay_bot.attention import (
    address_reason,
    is_addressed_to_bot,
    is_reply_to_bot,
    mentions_name,
)
from qq_roleplay_bot.transport import IncomingMessage, MessageTarget

TARGET = MessageTarget(group_id="717151356")


def msg(
    text: str = "",
    *,
    mentioned: bool = False,
    reply_to: str = "",
    message_id: str = "m1",
    bot: bool = False,
):
    return IncomingMessage(
        message_id=message_id,
        session_id="group:717151356",
        user_id="900000002" if bot else "100",
        text=text,
        target=TARGET,
        is_bot_mentioned=mentioned,
        reply_to_message_id=reply_to,
        is_bot_message=bot,
    )


# --- 名字识别 ---------------------------------------------------------------


def test_chinese_name_is_detected() -> None:
    assert mentions_name("云茹你在吗")
    assert mentions_name("我觉得云茹说得对")
    assert mentions_name("问一下云茹")
    # 常见误写也要认。
    assert mentions_name("芸茹呢")


def test_latin_name_is_case_insensitive_and_word_bounded() -> None:
    assert mentions_name("YunRu")
    assert mentions_name("yunru ping")
    assert mentions_name("hey YUNRU")
    # 整词匹配：不该被更长的单词误命中。
    assert not mentions_name("yunruntime")
    assert not mentions_name("myunru")


def test_common_words_do_not_trigger_name_detection() -> None:
    """单字和常见词绝不能算叫她，否则群里每句话都会触发模型。"""

    for text in ["今天云很多", "茹毛饮血", "云边孤雁", "我看见一朵云",
                 "你好", "在吗", "天气不错", ""]:
        assert not mentions_name(text), text


def test_name_inside_other_nickname_is_not_addressing() -> None:
    """群成员的昵称里含她的名字，不代表在叫她——这是已知的误判来源。

    这里记录当前行为：昵称含"云茹"仍会被判为提名。保留是因为宁可她判断后
    不接，也不愿漏掉真正的呼叫；prompt 里已要求她结合语境判断。
    """

    # 明确记录为"会命中"，避免以后误以为这里是精确的。
    assert mentions_name("云茹bot测试群")


# --- @ 与引用回复 -----------------------------------------------------------


def test_mention_flag_wins() -> None:
    assert is_addressed_to_bot(msg("@YunRu", mentioned=True))
    assert address_reason(msg("@YunRu", mentioned=True)) == "mention"


def test_reply_to_bot_message_is_detected() -> None:
    bot_message = msg("我之前说的", message_id="b1", bot=True)
    recent = [msg("别人说的", message_id="u1"), bot_message]
    reply = msg("那这个呢", reply_to="b1")
    assert is_reply_to_bot(reply, recent)
    assert is_addressed_to_bot(reply, recent)
    assert address_reason(reply, recent) == "reply_to_bot"


def test_reply_to_other_user_is_not_addressing_the_bot() -> None:
    recent = [msg("别人说的", message_id="u1")]
    reply = msg("回你一句", reply_to="u1")
    assert not is_reply_to_bot(reply, recent)
    assert not is_addressed_to_bot(reply, recent)
    assert address_reason(reply, recent) == "none"


def test_reply_without_matching_history_is_not_addressing() -> None:
    """引用了一个不在近期历史里的消息 ID：不能当成在叫她。"""

    assert not is_reply_to_bot(msg("喂", reply_to="ghost"), [])
    assert not is_addressed_to_bot(msg("喂", reply_to="ghost"), [])


def test_plain_chatter_is_not_addressing() -> None:
    assert not is_addressed_to_bot(msg("今天天气不错"))
    assert address_reason(msg("今天天气不错")) == "none"
