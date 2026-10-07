"""**"过了多少秒"一律用单调钟**：墙上时钟被对时/改表拨动时，时长不许跟着跳。

由来（2026-10-08 安全与时间口径审计 B-02）：已经定下"单调时钟只算时长、墙上时钟只印
时刻"的纪律，但仍有几处把**墙上时刻之差**当时长：运行时长（`/super status` 的"已运行"）、
出站队列的"这条消息排了多久"、账本落盘节流。NTP 校时或手动改表（Windows 开机 RTC
错一次很常见）时它们会一次性失真——最坏 `uptime` 被 `max(0.0, …)` 夹成 0。

这一份把三个读数都喂一个"墙上时钟被往回拨 / 往前跳"的场景，断言**它们都不受影响**：

1. `DialogueEngine._super_status` 的"已运行"——用 `started_monotonic`（单调）算，
   `started_at`（墙上）只用来印"开始于"；
2. `Outbox` 的年龄/过期判断——默认钟就是 `time.monotonic`；
3. `ApiUsageStore` 的落盘节流——间隔用单调钟量，`since`/`updated_at` 仍写墙上时刻。

三处各自把钟换回墙上（突变）都会红：往回拨时"已运行"被夹成 0 秒、出站年龄被算成 0、
节流被一次跨天跳变打乱。
"""
import asyncio
import tempfile
from pathlib import Path
from unittest.mock import patch

from qq_roleplay_bot.api_usage import SAVE_INTERVAL_SECONDS, ApiUsageStore
from qq_roleplay_bot.outbox import Outbox
from qq_roleplay_bot.stage3_main import DialogueEngine
from qq_roleplay_bot.transport import MessageTarget

GROUP = "717151356"
TARGET = MessageTarget(group_id=GROUP)


class _NeverCalled:
    async def complete(self, request):
        raise AssertionError("这些路径不该调用模型")


class _Transport:
    connected = True

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, target, text, reply_to="") -> None:
        self.sent.append(text)


def _usage() -> dict:
    return {"prompt_tokens": 10, "completion_tokens": 1,
            "prompt_cache_hit_tokens": 6, "prompt_cache_miss_tokens": 4}


# --- 1. "已运行多久" ---------------------------------------------------------


def test_uptime_ignores_a_wall_clock_that_is_stepped_back() -> None:
    """墙上时间往回拨一小时：`已运行` 必须还是真的那一小时零一分。"""

    mono = [1000.0]
    wall = [1_700_000_000.0]
    with patch("time.time", lambda: wall[0]):
        engine = DialogueEngine(_NeverCalled(), clock=lambda: mono[0],
                                wall_clock=lambda: wall[0])
        mono[0] += 3661.0
        wall[0] -= 3600.0          # 系统对时，往回拨一小时
        text = engine._super_status()

    assert "已运行：1 小时 1 分" in text, text
    # "开始于"仍印那次启动的**墙上时刻**（它本来就是个时刻，不是时长）。
    assert "开始于：" in text, text


def test_uptime_ignores_a_wall_clock_that_jumps_forward() -> None:
    """往前跳一天也一样：真实过了 10 秒，就报 10 秒。"""

    mono = [500.0]
    wall = [1_700_000_000.0]
    with patch("time.time", lambda: wall[0]):
        engine = DialogueEngine(_NeverCalled(), clock=lambda: mono[0],
                                wall_clock=lambda: wall[0])
        mono[0] += 10.0
        wall[0] += 86_400.0
        text = engine._super_status()

    assert "已运行：10 秒" in text, text


def test_the_snapshot_carries_both_starting_points() -> None:
    """快照里两个起始点都在：墙上那个印时刻，单调那个算时长。"""

    mono = [700.0]
    wall = [1_700_000_000.0]
    with patch("time.time", lambda: wall[0]):
        engine = DialogueEngine(_NeverCalled(), clock=lambda: mono[0])
        snapshot = engine.snapshot()

    assert snapshot.started_at == wall[0]
    assert snapshot.started_monotonic == 700.0


# --- 2. 出站队列"这条排了多久" ----------------------------------------------


def test_the_outbox_age_ignores_the_wall_clock() -> None:
    """年龄 = 单调钟的两次读数之差；墙上时钟被拨回一小时也不影响它。"""

    mono = [50.0]
    wall = [1000.0]
    with patch("time.monotonic", lambda: mono[0]), patch("time.time", lambda: wall[0]):
        outbox = Outbox()                      # 默认钟就该是单调的
        outbox.enqueue(TARGET, "一句没发出去的话")
        mono[0] += 5.0
        wall[0] -= 3600.0
        item = outbox.pending()[0]
        age = outbox.age(item)

    assert age == 5.0, age


def test_the_outbox_drops_what_really_aged_out() -> None:
    """真的过了 5 秒（上限 4 秒）→ 该丢；墙上时钟往回拨不该把它救回来。"""

    mono = [50.0]
    wall = [1000.0]
    with patch("time.monotonic", lambda: mono[0]), patch("time.time", lambda: wall[0]):
        outbox = Outbox(max_age_seconds=4.0)
        outbox.enqueue(TARGET, "一句")
        mono[0] += 5.0
        wall[0] -= 3600.0
        transport = _Transport()
        resent, dropped = asyncio.run(outbox.flush(transport))

    assert (resent, dropped) == (0, 1), (resent, dropped)
    assert transport.sent == []


def test_the_outbox_keeps_what_did_not_age_out() -> None:
    """只过了 1 秒（上限 4 秒）→ 照发；墙上时钟往前跳一天不该把它判成过期。"""

    mono = [50.0]
    wall = [1000.0]
    with patch("time.monotonic", lambda: mono[0]), patch("time.time", lambda: wall[0]):
        outbox = Outbox(max_age_seconds=4.0)
        outbox.enqueue(TARGET, "一句")
        mono[0] += 1.0
        wall[0] += 86_400.0
        transport = _Transport()
        resent, dropped = asyncio.run(outbox.flush(transport))

    assert (resent, dropped) == (1, 0), (resent, dropped)
    assert transport.sent == ["一句"]


# --- 3. 账本落盘节流 ---------------------------------------------------------


def test_the_save_throttle_ignores_the_wall_clock() -> None:
    """距上次落盘只过了 1 秒（间隔 5 秒）→ 不写盘，哪怕墙上时钟跳了一天。"""

    mono = [10.0]
    wall = [1000.0]
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "api_usage.json"
        with patch("time.monotonic", lambda: mono[0]), patch("time.time", lambda: wall[0]):
            store = ApiUsageStore(path)
            store.add_usage("dialogue", _usage())      # 这一进程的第一次：一定落盘
            assert path.exists()
            mono[0] += 1.0
            wall[0] += 86_400.0
            store.add_usage("judge", _usage())         # 1 秒 < 5 秒 → 节流掉
            body = path.read_text(encoding="utf-8")

        assert "judge" not in body, body
        assert "dialogue" in body

        with patch("time.monotonic", lambda: mono[0]), patch("time.time", lambda: wall[0]):
            # 单调钟真的走过 SAVE_INTERVAL_SECONDS 之后，照写。
            mono[0] += SAVE_INTERVAL_SECONDS + 1.0
            store.add_usage("letter", _usage())
            body = path.read_text(encoding="utf-8")

        assert "judge" in body and "letter" in body


def test_the_ledger_still_records_wall_clock_moments() -> None:
    """`since` / `updated_at` 是**印给人看的时刻**，仍然写墙上时钟。"""

    mono = [10.0]
    wall = [1_700_000_000.0]
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "api_usage.json"
        with patch("time.monotonic", lambda: mono[0]), patch("time.time", lambda: wall[0]):
            store = ApiUsageStore(path)
            store.add_usage("dialogue", _usage())
            body = path.read_text(encoding="utf-8")

    assert str(int(wall[0])) in body, body


if __name__ == "__main__":
    import unittest

    unittest.main()
