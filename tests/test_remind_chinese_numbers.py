"""`remind.parse_remind_time` 认**中文数字**（用户 2026-10-09 实测报的问题）。

他原话：**"五分钟后提醒我给 dsh 反馈插件运行情况"** —— 那时候只有阿拉伯数字能认
（`'五分钟后' → None`），功能没坏，但他的原话进不去。这一份钉住中文数字那一半。

**范围是刻意划住的：只认日常说法，不做自然语言解析器。** 认 `一二三四五六七八九十`、
`两`、`半`，以及组合（`十五` / `二十五` / `三十` / `一个半小时`）×
单位（`秒` / `分钟` / `分后` / `小时` / `点` / `天` / `周`）。
认不出的仍然 `None`（"宁可不填，也别猜"），这一条一个字没改。

这一份只加**新**用例：`tests/test_remind_intent.py` 里那些阿拉伯数字/英文写法
一条都没动（那是"不许把认不出改成猜一个"的底线）。

突变（各自还原）：把 `_number()` 里的中文那一支去掉 → 这一份红。
"""
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from qq_roleplay_bot.remind import parse_remind_time

TZ = {"QQBOT_LOCAL_TZ_OFFSET_HOURS": "8"}
#: 2026-10-08 周四 22:12（+8）：与 `test_remind_intent.py` 同一个基准时刻。
NOW = datetime(2026, 10, 8, 22, 12, tzinfo=timezone(timedelta(hours=8))).timestamp()
#: 同一天的上午 10:00：用来验"当晚还没到的那些说法"。
MORNING = datetime(2026, 10, 8, 10, 0, tzinfo=timezone(timedelta(hours=8))).timestamp()


def _at(year: int, month: int, day: int, hour: int, minute: int = 0) -> float:
    return datetime(year, month, day, hour, minute,
                    tzinfo=timezone(timedelta(hours=8))).timestamp()


def test_the_sentence_the_user_actually_said() -> None:
    """用户原话：五分钟后提醒我给 dsh 反馈插件运行情况。"""

    with patch.dict(os.environ, TZ):
        parsed = parse_remind_time("五分钟后提醒我给 dsh 反馈插件运行情况", now=NOW)
    assert parsed == NOW + 300


def test_chinese_relative_durations() -> None:
    with patch.dict(os.environ, TZ):
        assert parse_remind_time("五分钟后", now=NOW) == NOW + 300
        assert parse_remind_time("五分钟", now=NOW) == NOW + 300
        assert parse_remind_time("十分钟后", now=NOW) == NOW + 600
        assert parse_remind_time("十五分钟", now=NOW) == NOW + 900
        assert parse_remind_time("二十五分钟后", now=NOW) == NOW + 1500
        assert parse_remind_time("一个小时", now=NOW) == NOW + 3600
        assert parse_remind_time("两个小时", now=NOW) == NOW + 7200
        assert parse_remind_time("三个小时后", now=NOW) == NOW + 10800


def test_half_hours() -> None:
    with patch.dict(os.environ, TZ):
        assert parse_remind_time("半小时后", now=NOW) == NOW + 1800
        assert parse_remind_time("半个小时", now=NOW) == NOW + 1800
        assert parse_remind_time("一个半小时后", now=NOW) == NOW + 5400
        assert parse_remind_time("一个半小时", now=NOW) == NOW + 5400


def test_seconds_days_and_weeks() -> None:
    with patch.dict(os.environ, TZ):
        assert parse_remind_time("三十秒后", now=NOW) == NOW + 30
        assert parse_remind_time("两天后", now=NOW) == NOW + 172800
        assert parse_remind_time("两周后", now=NOW) == NOW + 1209600
        assert parse_remind_time("一周后", now=NOW) == NOW + 604800


def test_chinese_clock_times() -> None:
    with patch.dict(os.environ, TZ):
        # 光说"三点"＝下一次 3 点（与光说"3点"同一条规矩）。
        assert parse_remind_time("三点", now=NOW) == _at(2026, 10, 9, 3)
        assert parse_remind_time("十点", now=NOW) == _at(2026, 10, 9, 10)
        # 带时间段/日子词就按那个算。
        assert parse_remind_time("下午三点", now=NOW) == _at(2026, 10, 9, 15)
        assert parse_remind_time("下午三点半", now=NOW) == _at(2026, 10, 9, 15, 30)
        assert parse_remind_time("下午两点", now=NOW) == _at(2026, 10, 9, 14)
        assert parse_remind_time("明天八点二十", now=NOW) == _at(2026, 10, 9, 8, 20)
        assert parse_remind_time("后天上午十点", now=NOW) == _at(2026, 10, 10, 10)
        # 当晚还没到：`今晚八点` 在上午十点说是今天 20:00。
        assert parse_remind_time("今晚八点", now=MORNING) == _at(2026, 10, 8, 20)


def test_unclear_chinese_wording_is_still_none() -> None:
    """认不出的仍然 None —— "宁可不填，也别猜"这一条没变。"""

    with patch.dict(os.environ, TZ):
        for text in (
            "三点一刻",          # "刻"不支持
            "一点儿小事",        # 光说"一点"不认（日常另有所指）
            "有点儿冷",
            "两点意见",          # 光说"两点"同上
            "两天没睡了",        # 光说"两天"（没有"后"）不是时限
            "五五分钟后",        # 认不出的数
            "一百分钟后",        # 不支持"百"
            "廿分钟后",
            "一会儿",
            "十点十十",          # 分钟位认不出
        ):
            assert parse_remind_time(text, now=NOW) is None, text


def test_the_guards_do_not_leak_into_the_digit_forms() -> None:
    """中文那几条规矩不许改变阿拉伯数字的既有行为。"""

    with patch.dict(os.environ, TZ):
        assert parse_remind_time("5分钟后", now=NOW) == NOW + 300
        assert parse_remind_time("8点", now=NOW) == _at(2026, 10, 9, 8)
        assert parse_remind_time("20:30", now=NOW) == _at(2026, 10, 9, 20, 30)
        assert parse_remind_time("下午3点", now=NOW) == _at(2026, 10, 9, 15)
        assert parse_remind_time("25点", now=NOW) is None


if __name__ == "__main__":
    import unittest

    unittest.main()
