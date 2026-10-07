"""对话背景补全的两个缓存**有上限、也会过期**（2026-10-08 隔离增长审计 [6]）。

`ConversationContextProvider` 的 `_members`（**整群成员表**——一个几百人的群就是几百条）
与 `_last_refresh` 从前都只增不减：键是会话 / 群，值是名册与时刻，**全树没有一处删除**。
群多的部署里它们会一直长到重启。

现在按"最后一次刷新/写入"的时间淘汰：先看保留期
（`QQBOT_CONTEXT_CACHE_TTL_SECONDS`，默认 6 小时），再看条数上限
（`QQBOT_CONTEXT_CACHE_MAX_SESSIONS`，默认 200，超了丢**最旧**的，不是随机）。
两个都是**调用时现读**的配置；淘汰条数记在 `stats["cache_evicted"]` 里。

这一份钉住：

1. 超过上限 → 只留最新的那些，**丢的是最旧的那条**（不是刚进来的那条）；
2. 超过保留期 → 丢掉，并且下次会真的重新取一次名册（`_last_refresh` 也被清）；
3. 活跃的会话（每轮都刷新）不会被自己的保留期误伤；
4. 淘汰有计数；
5. 配置的坏值回落、超范围夹取。

去掉条数上限那段（突变 J）→ 第 1 条红；去掉保留期那段（突变 K）→ 第 2 条红。
"""
import asyncio
import os
from unittest.mock import patch

from qq_roleplay_bot import dev_config
from qq_roleplay_bot.conversation_context import ConversationContextProvider
from qq_roleplay_bot.transport import IncomingMessage, MessageTarget

GROUP = "717151356"
OTHER = "1084401296"
MAX = 10          # `QQBOT_CONTEXT_CACHE_MAX_SESSIONS` 的下界就是 10，测试用它最省事
TTL = 60.0        # `QQBOT_CONTEXT_CACHE_TTL_SECONDS` 的下界


class _FakeClient:
    """只回答"成员名单"与"群历史"两件事，并数调用次数。"""

    def __init__(self) -> None:
        self.member_calls = 0

    async def call(self, action, payload):
        return {"user_id": "900000002"}

    async def group_members(self, group_id):
        self.member_calls += 1
        return ({"user_id": 100, "nickname": f"成员{group_id}", "card": ""},)

    async def group_message_history(self, group_id, *, count=20, message_seq=None):
        return ()


def _message(group_id: str) -> IncomingMessage:
    return IncomingMessage(
        message_id=f"c{group_id}", session_id=f"group:{group_id}", user_id="100",
        text="在吗", target=MessageTarget(group_id=group_id), is_bot_mentioned=True,
    )


def _provider(clock) -> tuple[ConversationContextProvider, _FakeClient]:
    client = _FakeClient()
    return ConversationContextProvider(client, refresh_interval=0.0, clock=clock), client


def _seed(provider: ConversationContextProvider, group_id: str) -> None:
    asyncio.run(provider.collect_seed_messages(
        f"group:{group_id}", group_id, _message(group_id)))


# --- 1. 条数上限：丢最旧 -----------------------------------------------------


def test_the_cache_drops_the_oldest_sessions_when_over_the_limit() -> None:
    now = [1000.0]
    provider, _ = _provider(lambda: now[0])
    with patch.dict(os.environ, {"QQBOT_CONTEXT_CACHE_MAX_SESSIONS": str(MAX)}):
        for index in range(MAX + 2):
            now[0] += 1.0
            _seed(provider, str(700000000 + index))
        assert provider.cached_session_count() == MAX, provider.cached_session_count()
        # 最早的两位被丢掉了。
        for index in range(2):
            assert provider.cached_member_count(f"group:{700000000 + index}") == 0
        # 最新的那位还在。
        assert provider.cached_member_count(f"group:{700000000 + MAX + 1}") == 1
        assert provider.stats["cache_evicted"] >= 1


def test_the_newest_is_kept_even_though_it_arrived_last() -> None:
    """淘汰必须按**时间**，不能按"插入顺序"糊过去：被刷新过的那位要活下来。"""

    now = [1000.0]
    provider, _ = _provider(lambda: now[0])
    with patch.dict(os.environ, {"QQBOT_CONTEXT_CACHE_MAX_SESSIONS": str(MAX)}):
        for index in range(MAX + 1):
            now[0] += 1.0
            _seed(provider, str(700000000 + index))
        # 第 0 位再活跃一次 → 它的时间戳变成最新；第 1 位成了最旧的。
        now[0] += 1.0
        _seed(provider, str(700000000))
        now[0] += 1.0
        _seed(provider, str(700000000 + MAX + 5))

        assert provider.cached_session_count() == MAX
        assert provider.cached_member_count("group:700000000") == 1, "刚活跃过的被丢了"
        assert provider.cached_member_count("group:700000001") == 0, "最旧的没被丢"


# --- 2. 保留期 ---------------------------------------------------------------


def test_stale_sessions_are_dropped_after_the_ttl_and_fetched_again() -> None:
    now = [1000.0]
    provider, client = _provider(lambda: now[0])
    with patch.dict(os.environ, {"QQBOT_CONTEXT_CACHE_TTL_SECONDS": str(TTL)}):
        _seed(provider, GROUP)
        assert provider.cached_member_count(f"group:{GROUP}") == 1
        assert client.member_calls == 1

        now[0] += TTL + 1.0
        _seed(provider, OTHER)                      # 这一跳顺带淘汰

        assert provider.cached_member_count(f"group:{GROUP}") == 0, "过期了还在"
        _seed(provider, GROUP)                      # 再回来：限流表也被清了 → 重新取
        assert client.member_calls == 3, client.member_calls
        assert provider.stats["cache_evicted"] >= 1


def test_an_active_session_is_not_evicted_by_its_own_ttl() -> None:
    now = [1000.0]
    provider, client = _provider(lambda: now[0])
    with patch.dict(os.environ, {"QQBOT_CONTEXT_CACHE_TTL_SECONDS": str(TTL)}):
        for _ in range(5):
            _seed(provider, GROUP)
            now[0] += TTL - 1.0         # 每次都在保留期内回来
        assert provider.cached_member_count(f"group:{GROUP}") == 1
        assert client.member_calls == 5


# --- 3. 配置本身 -------------------------------------------------------------


def test_the_limits_fall_back_and_are_clamped() -> None:
    with patch.dict(os.environ):
        os.environ.pop("QQBOT_CONTEXT_CACHE_MAX_SESSIONS", None)
        os.environ.pop("QQBOT_CONTEXT_CACHE_TTL_SECONDS", None)
        assert dev_config.context_cache_max_sessions() == 200
        assert dev_config.context_cache_ttl_seconds() == 21600.0
    for text, expected in (("", 200), ("不是数", 200), ("0", 10), ("99999", 5000)):
        with patch.dict(os.environ, {"QQBOT_CONTEXT_CACHE_MAX_SESSIONS": text}):
            assert dev_config.context_cache_max_sessions() == expected, text
    for text, expected in (("", 21600.0), ("x", 21600.0), ("1", 60.0), ("99999999", 604800.0)):
        with patch.dict(os.environ, {"QQBOT_CONTEXT_CACHE_TTL_SECONDS": text}):
            assert dev_config.context_cache_ttl_seconds() == expected, text


if __name__ == "__main__":
    import unittest

    unittest.main()
