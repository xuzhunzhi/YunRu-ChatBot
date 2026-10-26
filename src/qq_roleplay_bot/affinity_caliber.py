"""画像里的"他怎么对待你"口径，以及判定/维护两侧共用的词表。

为什么单独成模块：**同一句口径要同时送到两条路上**——判定侧不许把"带教式纠正"
读成贬低，维护侧不许因为它语气难听就降关系。各写一套会在下一次调措辞时分叉；
2026-10-24 那次事故（18:29 维护把 closeness 44→40、20:52 判定报 `belittle` 并把
guardedness 0→10）就是同一个口径没有同时送到两条路上。放在这里，任何一侧改词表
都会同时影响两边。

为什么不是把词表塞进 `dialogue_judge`：维护 agent 不 import 判定，判定也不该成为
记忆写入的依赖；这个模块只依赖 `re`，没有循环。

依据（用户 2026-10-24 交底，原话照抄）：人物画像里写着 **"他说话直，常常当面说你
哪一句不对、让你别插话、让你换个说法。那不是瞧不起你，是在带你；话难听也未必是
嫌你。"** 但判定只拿到了称呼与边界，维护拿到了画像却没有"口径压过语气"的规矩。

什么时候能改回去：用户收回"带教式纠正不算贬低、也不该降关系"这条口径时，连同
`dialogue_judge.downgrade_correction_misread` 与
`memory_maintenance_agent.reject_unbacked_affinity` 两个闸门一起删，别只删词表。
"""
from __future__ import annotations

import re

#: 画像里出现这些句式，才说明"他怎么对待你"有明确口径。
#: 这四条就是用户交底里那段画像的原话特征，不另造同义词——换措辞等于换判据。
CALIBER_MARKERS = ("不是嫌", "在带你", "不是瞧不起", "话难听")

#: 判定/维护共用的"纠正式词"（用户 2026-10-24 点名的那些，原话照抄）。
CORRECTION_WORDS = ("插嘴", "插话", "别乱说", "没把握", "不懂", "记不住", "说不满", "别说满")

#: 判定/维护共用的"贬损词"。**必须真的冲她这个人**才算——只是说她这一句做错了、
#: 让她别插嘴的，走上面那张表。词表宁可短一点：多认一个"蠢"会漏掉一次真贬低，
#: 多认一个"笨"会把玩笑打成冒犯（2026-10-16 的 4/4 误判就是这么来的）。
BELITTLE_WORDS = (
    "没本事", "就这点本事", "傻", "废物", "蠢", "就你这水平", "什么都不会",
    "看不起", "嘲笑", "没用", "垃圾", "白痴", "智障", "懂个屁", "讨人嫌",
)

#: 注入判定那一行**连同前缀**的总长上限（用户 2026-10-24：一行、≤60 字）。
CALIBER_MAX_CHARS = 60
CALIBER_PREFIX = "他对你说直话时："

_SPLIT = re.compile(r"[\n\r。；;！!？?]+")


def _shorten(piece: str, limit: int) -> str:
    """把一段截到 `limit` 字；标记落在后半段时从标记附近起截。

    为什么不是一律从头截：画像常写成"他说话直，常常当面说……那不是瞧不起你"，
    从头截会把"不是瞧不起你"截掉，注入的那一行就只剩语气、没有口径，等于没加。
    """

    if len(piece) <= limit:
        return piece
    hit = min((piece.find(marker) for marker in CALIBER_MARKERS if marker in piece),
              default=0)
    if hit >= limit:
        piece = piece[hit:]
        return piece if len(piece) <= limit else piece[: limit - 1] + "…"
    return piece[: limit - 1].rstrip("，,、；; ") + "…"


def profile_caliber(profile: str) -> str:
    """从画像正文里抠出"带教式纠正"那一句；没有就返回空串（**不许编**）。

    画像现在是多片拼起来的（`profile` 总览 + `profile/<方面>`），这里只按句号/分号/
    换行切段找**含 CALIBER_MARKERS 的那一段**，不拼别的内容：判定那一行已经够短，
    多塞一句就会把它撑成第二块记忆。
    """

    text = (profile or "").strip()
    if not text:
        return ""
    limit = CALIBER_MAX_CHARS - len(CALIBER_PREFIX)
    for chunk in _SPLIT.split(text):
        piece = chunk.strip(" \t，,、：:")
        if not piece or not any(marker in piece for marker in CALIBER_MARKERS):
            continue
        return f"{CALIBER_PREFIX}{_shorten(piece, limit)}"
    return ""


def has_caliber(profile: str) -> bool:
    """画像里有没有"这是带你、不是嫌你"的口径。维护侧的闸门只看这一条。"""

    return bool(profile_caliber(profile))


def is_correction_quote(text: str) -> bool:
    """这句原话像不像"纠正式的话"（让她别插嘴、别说满、说记不住）。"""

    return any(word in (text or "") for word in CORRECTION_WORDS)


def is_belittle_quote(text: str) -> bool:
    """这句原话是不是明确的贬损（冲她这个人）。**判据只看词，不猜语气。**"""

    return any(word in (text or "") for word in BELITTLE_WORDS)
