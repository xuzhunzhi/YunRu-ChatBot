"""判断一条群消息是否"在叫 YunRu"。

为什么单独成模块：是否被叫到直接决定要不要为这条消息付出一次模型调用，
所以这里的判断必须可单测、可审查，而不是散落在主循环的条件表达式里。

判定顺序（任一成立即为被叫到）：
1. OneBot 的 @ 标记——最可靠，优先级最高；
2. 引用回复了 YunRu 自己的某条消息——QQ 里这是明确的对话对象指定；
3. 正文出现她的名字——中文没有词边界，只能靠子串匹配，因此只收窄到
   明确指向她的写法，宁可漏判也不滥用常见词。

注意：本模块只回答"是否在叫她"，不回答"该不该回复"。后者由模型结合
群聊语境决定，prompt 里已有约束。
"""
from __future__ import annotations

import re

from .transport import IncomingMessage

# 会指向她本人的写法。只放明确无歧义的形式：
# - 中文名"云茹"及常见误写"芸茹"；
# - 拉丁写法 yunru（大小写不敏感）。
# 刻意不收"云""茹"这类单字——群里聊天气、聊人名都会误伤。
BOT_NAMES: tuple[str, ...] = ("云茹", "芸茹", "yunru")

# 拉丁名做不区分大小写的整词匹配，避免 "yunruntime" 之类误命中。
_LATIN_PATTERN = re.compile(r"(?<![a-z0-9])yunru(?![a-z0-9])")


def mentions_name(text: str) -> bool:
    """正文里是否出现她的名字。"""

    if not text:
        return False
    lowered = text.casefold()
    for name in BOT_NAMES:
        # 拉丁名走整词匹配，中文名直接子串匹配（中文无词边界）。
        if name.isascii():
            continue
        if name in text:
            return True
    return bool(_LATIN_PATTERN.search(lowered))


def is_reply_to_bot(message: IncomingMessage, recent: object) -> bool:
    """这条消息是否引用回复了 YunRu 自己的某条消息。"""

    target_id = getattr(message, "reply_to_message_id", "") or ""
    if not target_id:
        return False
    for item in recent or ():
        if getattr(item, "is_bot_message", False) and getattr(item, "message_id", "") == target_id:
            return True
    return False


def is_addressed_to_bot(message: IncomingMessage, recent: object = ()) -> bool:
    """综合判断：@、引用回复、或正文提名，任一成立即视为被叫到。"""

    if getattr(message, "is_bot_mentioned", False):
        return True
    if is_reply_to_bot(message, recent):
        return True
    return mentions_name(getattr(message, "text", "") or "")


def address_reason(message: IncomingMessage, recent: object = ()) -> str:
    """给出判定依据，用于脱敏日志与观测。"""

    if getattr(message, "is_bot_mentioned", False):
        return "mention"
    if is_reply_to_bot(message, recent):
        return "reply_to_bot"
    if mentions_name(getattr(message, "text", "") or ""):
        return "name"
    return "none"


# 说明：这里曾经有过一个"没人叫她时是否值得看一眼"的关键词闸门，用来减少
# 模型调用。实测在真实群历史上只挡掉 4.2% 的消息，而且挡的全是"6""中"这类
# 过场——真正出问题的那句（"dsh模拟测算的修改后的命中率能到87%"）词面上完全
# 正常，任何关键词闸门都挡不住。该不该插话是**语义**判断，只有模型能做，
# 所以那层闸门已移除：减少"硬插一句"要靠 prompt 里的分寸，不是靠过滤器。
