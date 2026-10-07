"""「群里这边」的时间是**显式配置**，不跟宿主时区走（2026-10-08 安全与时间口径审计 B-01）。

修之前：`datetime.fromtimestamp(moment)` / `time.localtime(timestamp)` 取的是**本机时区**，
而代码把"本机=北京"写死在注释里。换到 UTC 云主机上，"群里这边"整整差 8 小时，
而且**没有任何信号**——刚加的钟表测试（`tests/test_two_clocks.py`）在 `TZ=UTC` 下照样全绿，
因为 `datetime(2026,10,8,22,12).timestamp()` 与 `fromtimestamp()` 是同一台机器上的
一对逆运算，两边一起平移，断言不变。

现在"这边"是 `QQBOT_LOCAL_TZ_OFFSET_HOURS`（默认 8，调用时现读，超出
`[-12, 14]` 夹进来）；"她那边" = 这个基准 + 既有的 `QQBOT_PERSONA_OFFSET_HOURS`（默认 3），
两句话的措辞一个字没变。

**这一份的断言与宿主时区无关**：喂进来的是**带时区构造的 UTC 时刻**
（`datetime(..., tzinfo=timezone.utc).timestamp()`），期望值是 +8 渲染出来的那些字。
所以同一条断言在 UTC 机器与北京机器上都成立。

突变（把显式偏移去掉、换回宿主时区）在这里**必红**：本机是 UTC+8，
"默认 8 小时"那一档看不出差别，但"配置成 5 小时"那一档会算回 22:12 而不是 19:12。
"""
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from qq_roleplay_bot import dev_config
from qq_roleplay_bot.stage3_main import _format_clock
from qq_roleplay_bot.stage3_runtime import _now_text

#: 2026-10-08 **14:12 UTC** = 北京（+8）22:12。显式带时区构造，所以与宿主时区无关。
UTC_MOMENT = datetime(2026, 10, 8, 14, 12, tzinfo=timezone.utc).timestamp()
#: +8 下的两句（"她那边" = +3，跨到次日）。
STAMP_AT_EIGHT = "你那边现在是 2026-10-09 周五 01:12；群里这边是 2026-10-08 周四 22:12"


# --- 1. 配置本身 -------------------------------------------------------------


def test_the_offset_defaults_to_plus_eight() -> None:
    with patch.dict(os.environ):
        os.environ.pop("QQBOT_LOCAL_TZ_OFFSET_HOURS", None)
        assert dev_config.local_tz_offset_hours() == 8.0
        assert dev_config.local_timezone().utcoffset(None) == timedelta(hours=8)


def test_the_offset_is_read_when_called_not_at_import() -> None:
    """改环境变量立刻生效（面板 / `.env` 都要能拉动它）。"""

    with patch.dict(os.environ, {"QQBOT_LOCAL_TZ_OFFSET_HOURS": "5"}):
        assert dev_config.local_tz_offset_hours() == 5.0
    with patch.dict(os.environ, {"QQBOT_LOCAL_TZ_OFFSET_HOURS": "-3.5"}):
        assert dev_config.local_tz_offset_hours() == -3.5


def test_the_offset_is_clamped_to_real_timezone_bounds() -> None:
    """离谱的值夹进 `[UTC-12, UTC+14]`：不整份作废，也不把时间变成几十年后。"""

    for text, expected in (("800", 14.0), ("-99", -12.0), ("14", 14.0), ("-12", -12.0)):
        with patch.dict(os.environ, {"QQBOT_LOCAL_TZ_OFFSET_HOURS": text}):
            assert dev_config.local_tz_offset_hours() == expected, text


def test_a_broken_value_falls_back_to_the_default() -> None:
    for text in ("", "   ", "北京", "8h"):
        with patch.dict(os.environ, {"QQBOT_LOCAL_TZ_OFFSET_HOURS": text}):
            assert dev_config.local_tz_offset_hours() == 8.0, text


# --- 2. 两句话 ---------------------------------------------------------------


def test_the_group_time_is_the_configured_offset_even_for_a_utc_moment() -> None:
    with patch.dict(os.environ):
        os.environ.pop("QQBOT_LOCAL_TZ_OFFSET_HOURS", None)
        assert _now_text(UTC_MOMENT) == STAMP_AT_EIGHT


def test_the_group_time_follows_the_configured_offset() -> None:
    """配置成 +5，"群里这边"就是 19:12——**不再是宿主的 +8**。

    这一条就是突变检测点：换回 `fromtimestamp`（宿主时区）时，本机（UTC+8）会印
    22:12，于是它红。
    """

    with patch.dict(os.environ, {"QQBOT_LOCAL_TZ_OFFSET_HOURS": "5"}):
        assert _now_text(UTC_MOMENT) == (
            "你那边现在是 2026-10-08 周四 22:12；群里这边是 2026-10-08 周四 19:12")


def test_her_side_is_still_the_group_side_plus_the_persona_offset() -> None:
    """两句的关系不变：她那边 = 这边 + `QQBOT_PERSONA_OFFSET_HOURS`（默认 3）。"""

    with patch.dict(os.environ, {"QQBOT_LOCAL_TZ_OFFSET_HOURS": "0",
                                 "QQBOT_PERSONA_OFFSET_HOURS": "3"}):
        assert _now_text(UTC_MOMENT) == (
            "你那边现在是 2026-10-08 周四 17:12；群里这边是 2026-10-08 周四 14:12")


# --- 3. `/super status` 那几处时刻 -------------------------------------------


def test_the_super_status_clock_uses_the_configured_offset() -> None:
    """`_format_clock`（`/super status`、`/super apicheck` 的"更新于"全用它）同样。"""

    with patch.dict(os.environ):
        os.environ.pop("QQBOT_LOCAL_TZ_OFFSET_HOURS", None)
        assert _format_clock(UTC_MOMENT) == "10-08 22:12:00"
    with patch.dict(os.environ, {"QQBOT_LOCAL_TZ_OFFSET_HOURS": "5"}):
        assert _format_clock(UTC_MOMENT) == "10-08 19:12:00"
    assert _format_clock(0.0) == "—"


if __name__ == "__main__":
    import unittest

    unittest.main()
