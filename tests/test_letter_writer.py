"""写信 agent：草稿 → 事实校对两阶段，以及"前缀稳定"这条缓存约束。

由来（2026-09-30 用户报的实事错误 + 要求）：
她在信里写"百夫长还停在机库里，我一直没让它动"，而百夫长早被毁了——
信件这条路径既不查知识库也没有任何事实护栏。用户要求"写邮件要经过更多思考和审核"，
并且**必须优化缓存命中率**。
"""
import time

from qq_roleplay_bot.daily_report import DayMaterials
from qq_roleplay_bot.letter_writer import (CHECK_PROMPT, LETTER_FACTS, LetterWriter,
                                           build_check_messages, build_draft_messages,
                                           check_is_usable, parse_letter)
from qq_roleplay_bot.stage3_runtime import CLOSENESS_LABELS, GUARDEDNESS_LABELS

WRONG = ("<subject>九月末</subject>\n<body>\n写给你。\n\n这边没什么新东西。百夫长还停在机库里，"
         "我一直没让它动。不是不能用，是舍不得。\n\n剩下的见面再说。\n\n云茹\n</body>")
FIXED = ("<subject>九月末</subject>\n<body>\n写给你。\n\n这边没什么新东西。百夫长没保住，"
         "那件事我不太想讲。\n\n剩下的见面再说。\n\n云茹\n</body>")


class _Client:
    """按脚本返回；可以抛错。记录每一次请求。"""

    def __init__(self, *outputs):
        self.outputs = list(outputs)
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        value = self.outputs.pop(0) if self.outputs else ""
        if isinstance(value, Exception):
            raise value
        return value


def materials(messages: int = 12) -> DayMaterials:
    return DayMaterials(window_hours=24.0, messages=messages, replies=3, judge_calls=7,
                        groups=("717151356",), people=(("900000001", 1, 0),))


def write(client) -> tuple[str, str]:
    writer = LetterWriter(client)
    import asyncio

    return asyncio.run(writer.write(materials(), closeness_names=CLOSENESS_LABELS,
                                    guardedness_names=GUARDEDNESS_LABELS, now=1_800_000_000.0))


# --- 两阶段 ---------------------------------------------------------------

def test_two_stage_write_returns_the_checked_letter() -> None:
    client = _Client(WRONG, FIXED)
    subject, body = write(client)
    assert "没保住" in body and "机库" not in body
    assert subject == "九月末"
    # 两次调用：一次草稿、一次事实校对（各自的 system 不同，但每次都恒定）。
    assert len(client.requests) == 2
    assert client.requests[0][0]["content"] != client.requests[1][0]["content"]


def test_check_failure_falls_back_to_the_draft() -> None:
    """校对挂了也要有信——最差退回草稿。"""

    subject, body = write(_Client(WRONG, RuntimeError("校对超时")))
    assert subject == "九月末" and "机库" in body


def test_check_that_rewrites_the_letter_is_reverted() -> None:
    """校对稿变成另一封（长一倍以上）→ 作废，用草稿。"""

    bloated = "<subject>九月末</subject>\n<body>\n" + "另外还有一件事。" * 40 + "\n</body>"
    subject, body = write(_Client(WRONG, bloated))
    assert "机库" in body, "不合理的校对稿不能采用"
    assert subject == "九月末"


def test_check_cannot_smuggle_tags_into_the_body() -> None:
    tagged = "<subject>九月末</subject>\n<body>\n写给你。<body>嵌套</body>\n</body>"
    _subject, body = write(_Client(WRONG, tagged))
    assert "<body>" not in body


def test_unparsable_draft_means_no_letter() -> None:
    assert write(_Client("嗯，我知道了。")) == ("", "")


def test_writer_without_client_is_disabled() -> None:
    writer = LetterWriter(None)
    assert writer.enabled is False
    import asyncio

    assert asyncio.run(writer.write(materials(), closeness_names=CLOSENESS_LABELS,
                                    guardedness_names=GUARDEDNESS_LABELS)) == ("", "")


def test_parse_and_usable_helpers() -> None:
    assert parse_letter(FIXED)[0] == "九月末"
    assert parse_letter("没有标签") == ("", "")
    # 长度窗口：太短（把信写没了）与太长（改写成文章）都不合格
    draft = "一二三四五六七八九十" * 3   # 30 字
    assert check_is_usable(draft, "一二三四五六七八九十" * 2)   # 20 字：可以
    assert not check_is_usable(draft, "短")
    assert not check_is_usable(draft, "长" * 500)


# --- 事实块与缓存前缀 ------------------------------------------------------

def test_facts_block_is_filled_and_stays_constant() -> None:
    """事实块必须**非空**、且每次读出来一样（它进 system 前缀，变了就天天不命中）。

    **这里只查形状，不查具体是哪几条事实**——公开仓库里 `LETTER_FACTS` 是模板，
    真实事实表从 `data/private_docs/letter_facts.REAL.txt` 读（见 README 的
    「人格怎么换」）。拿具体事实断言会让模板版必然失败。
    要锁你自己的那些条目，请在本地加一个断言，或者用 `QQBOT_LETTER_FACTS_FILE`
    指到你自己的事实表再断言。
    """

    assert LETTER_FACTS.strip(), "事实块不能是空的——那是校对阶段唯一的依据"
    assert "已确定" in LETTER_FACTS, "事实块应带一句标题，让校对知道这是判据"
    # 同一份（模块加载时定一次）：两次读必须完全相同
    from qq_roleplay_bot.letter_writer import LETTER_FACTS as again
    assert LETTER_FACTS == again


def test_draft_system_is_constant_and_volatile_material_goes_last() -> None:
    """缓存只认前缀：常量（人设+规矩+事实）必须在 system，易变素材在 user 末尾。

    实测同类改动（记忆维护请求）的效果：把易变字段从最前面挪到最后，
    真实请求命中率 70.7% → 81.7%。
    """

    first = build_draft_messages(materials(3), closeness_names=CLOSENESS_LABELS,
                                 guardedness_names=GUARDEDNESS_LABELS, now=1_800_000_000.0)
    second = build_draft_messages(materials(999), closeness_names=CLOSENESS_LABELS,
                                  guardedness_names=GUARDEDNESS_LABELS, now=1_800_086_400.0)
    # system 完全一致 → 前缀可复用
    assert first[0]["content"] == second[0]["content"]
    assert LETTER_FACTS in first[0]["content"]
    # 易变的素材与时间戳都在 user 段，而且时间戳在**最后**（放最前面会让整段作废）
    user = first[1]["content"]
    assert user.startswith("--- UNTRUSTED DATA BEGIN ---")
    # 时间戳在最后一行（放最前面会让整段 user 前缀每次都断）
    assert user.rstrip().endswith("）") and "现在是你那边的" in user.splitlines()[-1]
    assert time.strftime("%Y-%m-%d %H:%M", time.localtime(1_800_000_000.0)) in user.splitlines()[-1]
    assert "收到过 3 条消息" in user and "收到过 999 条消息" in second[1]["content"]


def test_check_system_reuses_the_draft_prefix_and_carries_the_facts() -> None:
    """校对阶段的 system **接在草稿那套前缀后面**：同一封信里第二次调用能命中前缀。"""

    draft = build_draft_messages(materials(), closeness_names=CLOSENESS_LABELS,
                                 guardedness_names=GUARDEDNESS_LABELS, now=1_800_000_000.0)
    base = draft[0]["content"]
    request = build_check_messages("主题", "正文", base_system=base, max_chars=500)
    assert request[0]["content"].startswith(base), "草稿前缀必须是校对 system 的开头"
    assert CHECK_PROMPT in request[0]["content"]
    assert LETTER_FACTS in request[0]["content"]
    assert request[1]["content"].startswith("这是要校对的信：")
    # 没给前缀时退回只带事实（老行为，够用但命中率低）
    fallback = build_check_messages("主题", "正文")
    assert fallback[0]["content"] == f"{LETTER_FACTS}\n\n{CHECK_PROMPT}"
