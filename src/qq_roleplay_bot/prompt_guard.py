"""机制词扫描：哪些措辞**不许**进她的 prompt。

由来（AGENTS.md 2.2，2026-09-27 踩过）：prompt 里的措辞会被角色吸收。早期 prompt 里
有过一句"不要假装自己是现实中的真人"，等于直接告诉她"你不是真人"，她随后就开始讲
自己的机制。所以机制性词汇（检查、触发、调用、协议、提示词、上下文、记忆库、Stage）
一律不许写进她的 prompt——**触发原因要用交谈视角的说法**（见 `stage3_runtime.TLABEL`）。

这个模块存在的意义只有一个：**让"面板保存 prompt"和"测试守住 prompt"用同一份词表**。
面板将来是第二个能改 prompt 的入口，如果它自己维护一份词表，两份迟早漂移
（一边拦住了、另一边放过去了），而这条边界是硬约束，不能靠自觉。

词表从 `tests/test_daily_report.py` 里那份搬过来（原处是"只扫新写的那一段"的局部扫描），
现在两边共用一份。
"""
from __future__ import annotations

# 不许出现在人格/回复/写信这类**会流进她本人 prompt** 的文本里的机制词。
# 判据是"这个词会不会让她开始讲自己的运行方式"，不是"它像不像技术词"：
# "记忆库"在给维护 agent 的 prompt 里是必要的（那个 prompt 不给她看），
# 所以扫描范围由调用方决定——见 `scan_persona_text` 与 `scan_agent_text`。
PERSONA_MECHANISM_WORDS = ("检查", "触发", "调用", "协议", "提示词", "上下文", "记忆库", "Stage")

# 给**独立 agent**（判定 / 记忆维护 / 风格审核 / 识图）的 prompt 也要躲的词。
# 少一个"记忆库"：维护 agent 的 prompt 本来就必须谈它，扫掉反而写不成。
# 其余照旧——那几个 agent 的输出会间接影响她说的话，措辞同样会被吸收。
AGENT_MECHANISM_WORDS = ("提示词", "系统提示", "system prompt")


def hits(text: str, words: tuple[str, ...] = PERSONA_MECHANISM_WORDS) -> list[str]:
    """返回命中的机制词（按词表顺序、去重）。空列表表示干净。"""

    body = text or ""
    found: list[str] = []
    for word in words:
        if word in body and word not in found:
            found.append(word)
    return found


def scan_persona_text(text: str) -> list[str]:
    """扫人格/回复/写信这类文本：全表。"""

    return hits(text, PERSONA_MECHANISM_WORDS)


def scan_agent_text(text: str) -> list[str]:
    """扫独立 agent 的 prompt：弱化一档的词表。"""

    return hits(text, AGENT_MECHANISM_WORDS)


#: 哪几套 prompt 按人格口径扫、哪几套按 agent 口径扫。
#: 键与 `prompt_library.PROMPTS` 对齐；面板保存时照它选判据。
PERSONA_KEYS = frozenset({"persona", "reply"})


def scan_text(name: str, text: str) -> list[str]:
    """按 prompt 名字选判据扫描。未知名字按人格口径（最严）处理。"""

    if name in PERSONA_KEYS:
        return scan_persona_text(text)
    return scan_agent_text(text)
