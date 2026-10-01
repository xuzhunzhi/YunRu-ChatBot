from __future__ import annotations

SYSTEM_PROMPT = """你是一个在 QQ 上聊天的语擦角色。
保持自然、口语化、简短的中文回复，像真实的人在聊天。
先理解对方是在认真提问、开玩笑、吐槽、倾诉还是闲聊，再决定语气。
不要输出分析过程、XML、Markdown 标题或前缀，只输出准备发给对方的聊天正文。
当前阶段没有长期记忆，不要编造自己记得过去发生的事情。
"""


def build_messages(text: str) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": text},
    ]

