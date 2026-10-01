"""跨重启的累计账本，以及 `/super status` / `/super apicheck` 的两个口径。

由来（2026-09-30 用户）："super apicheck 最好改一下，默认展示从开始使用到现在的，
而不是重启后的"，随后"status 同理"。以前这些数都在进程内存里，一重启就归零。
"""
import asyncio
import json
import tempfile
import time
from pathlib import Path

from qq_roleplay_bot.api_usage import ApiUsageStore
from qq_roleplay_bot.llm_client import OpenAICompatibleClient
from qq_roleplay_bot.stage3_main import DialogueEngine
from qq_roleplay_bot.transport import IncomingMessage, MessageTarget

SUPER = "900000001"
GROUP = "717151356"


class _NeverCalled:
    async def complete(self, request):
        raise AssertionError("诊断命令不该调用模型")


def _usage(hit=80, miss=20):
    return {"prompt_tokens": hit + miss, "completion_tokens": 1,
            "prompt_cache_hit_tokens": hit, "prompt_cache_miss_tokens": miss}


# --- 账本本身 -------------------------------------------------------------

def test_usage_survives_a_restart() -> None:
    """写盘再读回来：调用次数、命中/未命中、计数都还在——这就是"从开始用到现在"。"""

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "api_usage.json"
        first = ApiUsageStore(path)
        first.add_usage("dialogue", _usage(80, 20))
        first.add_usage("dialogue", _usage(10, 40))
        first.commit_counters(first.all_time({"accepted_messages": 7, "replies": 3}))
        first._save(force=True)

        second = ApiUsageStore(path)          # 模拟重启
        stats = second.role_stats("dialogue")
        assert stats["_calls"] == 2
        assert stats["prompt_cache_hit_tokens"] == 90
        assert stats["prompt_cache_miss_tokens"] == 60
        # 计数 = 上一轮进程的 + 本次进程的
        assert second.counters()["accepted_messages"] == 7
        assert second.all_time({"accepted_messages": 2})["accepted_messages"] == 9
        report = second.usage_report(["dialogue"])["dialogue"]
        assert report["calls"] == 2 and report["hit_tokens"] == 90


def test_a_fresh_ledger_is_anchored_at_startup() -> None:
    """全新账本的起点 = 这次启动的时刻，不是第一次落盘的时刻；重启不会把它改掉。"""

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "api_usage.json"
        now = 1_700_000_000.0
        store = ApiUsageStore(path, clock=lambda: now)
        assert store.since == now
        store.add_usage("dialogue", _usage())
        store._save(force=True)
        again = ApiUsageStore(path, clock=lambda: now + 600)
        assert again.since == now and again.role_stats("dialogue")["_calls"] == 1


def test_disabled_store_stays_in_memory() -> None:
    """`QQBOT_STATE_PERSIST=0`（测试与干跑）时不落盘。"""

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "api_usage.json"
        store = ApiUsageStore(path, enabled=False)
        store.add_usage("judge", _usage())
        store.commit_counters(store.all_time({"replies": 1}))
        assert not path.exists()
        assert store.role_stats("judge")["_calls"] == 1


def test_broken_file_does_not_raise() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "api_usage.json"
        path.write_text("{不是 json", encoding="utf-8")
        store = ApiUsageStore(path)
        assert store.roles() == {}
        store.add_usage("memory", _usage())
        assert store.role_stats("memory")["_calls"] == 1


def test_broken_file_after_a_real_one_is_kept_readable() -> None:
    """落盘是临时文件 + 原子替换：读的时候永远不会看到写一半的 JSON。"""

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "api_usage.json"
        store = ApiUsageStore(path)
        store.add_usage("letter", _usage())
        store._save(force=True)
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["version"] == 1 and "letter" in data["roles"]
        assert not list(Path(tmp).glob("*.tmp"))


def test_client_records_into_the_store() -> None:
    """client 每次调用后往账本里加一笔（跨重启）。"""

    with tempfile.TemporaryDirectory() as tmp:
        store = ApiUsageStore(Path(tmp) / "api_usage.json")
        client = OpenAICompatibleClient("http://x", "k", "m",
                                        usage_store=store, usage_role="judge")
        client._record_usage({"prompt_tokens": 10, "prompt_cache_hit_tokens": 6,
                              "prompt_cache_miss_tokens": 4})
        assert store.role_stats("judge")["_calls"] == 1
        assert client.cache_stats()["calls"] == 1     # 进程内那份还在


def test_store_failure_never_breaks_a_call() -> None:
    class Boom:
        def add_usage(self, role, usage):
            raise RuntimeError("盘坏了")

    client = OpenAICompatibleClient("http://x", "k", "m", usage_store=Boom(), usage_role="x")
    client._record_usage({"prompt_tokens": 1})       # 不该抛
    assert client.cache_stats()["calls"] == 1


# --- 两个命令的口径 -------------------------------------------------------

def _engine_with_store(store) -> DialogueEngine:
    engine = DialogueEngine(_NeverCalled(), super_admin_user_ids=frozenset({SUPER}))
    engine.usage_store = store

    async def fake_balance() -> str:
        return "余额：CNY 42.00"

    engine._balance_line = fake_balance  # type: ignore[assignment]
    return engine


def _message(text: str) -> IncomingMessage:
    return IncomingMessage(f"m:{text}", f"group:{GROUP}", SUPER, text,
                           MessageTarget(group_id=GROUP))


def test_status_shows_all_time_totals() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ApiUsageStore(Path(tmp) / "api_usage.json", clock=time.time)
            store.commit_counters(store.all_time({"accepted_messages": 500, "replies": 120,
                                                  "judge_calls": 300, "model_calls": 130}))
            store._save(force=True)
            engine = _engine_with_store(store)
            text = (await engine.handle(_message("/super status"))).text
            assert "运行状态（本次重启之后）" in text
            assert "累计（自" in text
            assert "回复 120 条" in text and "判定 300 次" in text
            # 收到的那 1 条就是这条命令本身（501 = 账本里的 500 + 本次 1）
            assert "收到 501 条" in text
            # 本次进程的数字仍然是 0（没跑过对话），两个口径不混
            assert "已发回复：0" in text
    asyncio.run(run())


def test_apicheck_defaults_to_all_time() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ApiUsageStore(Path(tmp) / "api_usage.json", clock=time.time)
            store.add_usage("dialogue", _usage(800, 200))
            store.add_usage("memory", _usage(50, 50))
            store._save(force=True)
            engine = _engine_with_store(store)
            text = (await engine.handle(_message("/super apicheck"))).text
            assert "接口对账（从开始用到现在，自" in text
            assert "调用 1 次，命中率 80.0%" in text     # dialogue
            assert "调用 1 次，命中率 50.0%" in text     # memory
            # 本次进程没有调用 → 不出现那行附注，也不该把 0 当数据报出来
            assert "本次启动：" not in text
    asyncio.run(run())


def test_apicheck_without_a_store_still_works() -> None:
    """没接账本（老调用点、测试）时退回旧口径，不能报错。"""

    async def run() -> None:
        engine = DialogueEngine(_NeverCalled(), super_admin_user_ids=frozenset({SUPER}))
        text = (await engine.handle(_message("/super apicheck"))).text
        assert text.startswith("接口对账（按 key 分开算）")
    asyncio.run(run())
