"""给召回到的知识块**判相关档**：一次模型调用，输出只有编号 + 档位。

由来（2026-10-18 用户原话）：*"这块不行也加个agent，用dsflash，effort调none"*、
*"prompt记得优化排序，保证高命中率"*、*"相关程度3挡，012，0永远不调起，优先程度用乘法"*。

为什么要有这一层：本地那套判档是**字面重合**（3 字窗口 / 内容词），它挡得住 2 字滑窗
与纯数字的假命中，但挡不住"3 字窗口跨虚词"这类——实测 `护发精油是干什么的` 会正撞上
同人里的「是干什」「干什么」，5 个名额全被无关块占掉。用户给的判据是**能不能用上**，
不是"字面像不像"，那只能由模型判。

形状（与写信那条同一套，2026-10-12 验过：编号 + 只给标签 + 代码裁决，格式抖动最少）：
* **一轮一次**：本地判为 1 或 2 的候选**一起**塞进同一次调用，不是每块一次；
* 输出一行一个 `编号:档位`，不解释、不写理由；
* 解析容错；解析不出来 → **fail-open**，用本地那份档位（这一轮不能因此没料）；
* 模型判出的档就是最终档（本地只当兜底），最终分仍然 = **档 × 权威度**。
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

#: 单块正文进这次调用的上限（判"能不能用上"不需要整段）。
TIER_ITEM_CHARS = 240
#: 一次最多判几块（正常情况下就是 `QQBOT_KNOWLEDGE_ITEMS` 的 5 条）。
TIER_MAX_ITEMS = 8
#: 这次调用的停顿网（秒）：它是**一轮里多加的一次调用**，不能拖住整条流水线。
TIER_TIMEOUT_SECONDS = 8.0

TIER_SYSTEM_PROMPT = """你在判断一段资料能不能帮上忙。你**只判断，不改写、不解释**。

这一句问的是：下面每一条能不能帮上忙，用来回「{query}」。

只看「能不能用上」，不要因为字面上没有同样的词就判低。
问的是某个人、某件事、某个地方，只要这一段讲的正是那一个人/那件事，就算 2，
哪怕用词完全不一样。

2 = 正好对得上：直接回答了这句在问的东西，或者里面就有答案要的那一点
1 = 沾得上、可能用得上：同一个话题、同一段时期、同一件事的一部分，但没直接回答
0 = 用不上：只是碰巧有同样的字、同样的数字，或者话题根本不沾。
    问的是别的东西（某个程序、某个日程、某个人的日常安排），资料讲的却是她这个人的
    经历——那也是 0，不是 1。

拿不准就给 1，不要给 0。

资料：
{items}

只输出编号和档位，一行一个，像这样（不要解释、不要写理由、不要输出任何别的字）：
1:2
2:1
3:0"""


def build_tier_messages(query: str, items) -> list[dict[str, str]]:
    """把"这一句 + 候选块"拼成**一次**请求（user 段只读，不用 system 之外的通道）。"""

    lines: list[str] = []
    for index, item in enumerate(items[:TIER_MAX_ITEMS], start=1):
        title = " ".join(str(getattr(item, "title", "") or "").split())
        body = " ".join(str(getattr(item, "content", "") or "").split())
        lines.append(f"{index}) {title}：{body[:TIER_ITEM_CHARS]}")
    system = TIER_SYSTEM_PROMPT.format(
        query=" ".join(str(query or "").split())[:300],
        items="\n".join(lines) or "（没有资料）",
    )
    return [{"role": "system", "content": system}]


_TIER_LINE_RE = re.compile(r"^\s*(\d{1,3})\s*[:：]\s*([012])\b")


def parse_tiers(raw: str) -> dict[int, int]:
    """从模型输出里读 `编号:档位`。**容错**：只认整行开头的编号 + 0/1/2，别的行忽略。

    返回 `{编号(1 起): 档位}`；一行都没读出来就是空字典（调用方按 fail-open 处理）。
    """

    tiers: dict[int, int] = {}
    for line in str(raw or "").splitlines():
        match = _TIER_LINE_RE.match(line)
        if not match:
            continue
        number = int(match.group(1))
        if 1 <= number <= TIER_MAX_ITEMS:
            tiers[number] = int(match.group(2))
    return tiers


async def tier_items(client, query: str, items, *, local_tiers: list[int]) -> list[int] | None:
    """问一次模型："这几块能不能用上"。返回与 `items` 对齐的档位，失败/读不出来返回 `None`。

    `local_tiers` 是本地判的档（同一顺序）：**模型没给某一块**时就用它（fail-open）。
    调用方拿到 `None` 时整轮都用本地档——这一层绝不能因为格式抖动让这一轮没料。
    """

    if not items or client is None:
        return None
    request = build_tier_messages(query, items)
    try:
        raw = await client.complete(request)
    except Exception:  # noqa: BLE001 - 这一层失败只等于"用本地档"
        logger.warning("knowledge tier call failed；按本地档继续", exc_info=True)
        return None
    parsed = parse_tiers(raw)
    if not parsed:
        logger.warning("knowledge tier output unparseable (%s chars)；按本地档继续",
                       len(str(raw or "")))
        return None
    tiers: list[int] = []
    for index, local in enumerate(local_tiers, start=1):
        tiers.append(parsed.get(index, local))
    return tiers


def apply_tiers(items, tiers, *, limit: int) -> list:
    """按"**模型档 × 权威度**"降序取前 `limit` 条；模型判 0 的一条都不取。

    并列时保持**进来时的次序**（那已经是本地按"档 × 权威度 + 提到她 + 来源"排好的），
    所以这一层只可能把顺序往更准的方向调，不会把稳定的次序打乱。
    权威度 0（测试/外部实现的假条目）按 1 算——别让它乘成 0 直接消失。
    """

    scored = [
        (int(tier) * max(1, int(getattr(item, "authority", 0) or 0)), index, item)
        for index, (item, tier) in enumerate(zip(items, tiers))
        if int(tier) > 0
    ]
    scored.sort(key=lambda row: (-row[0], row[1]))
    return [item for _score, _index, item in scored[:limit]]
