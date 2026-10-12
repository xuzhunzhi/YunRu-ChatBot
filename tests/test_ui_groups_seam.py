"""面板「群聊」那一页的**只读窄接缝**：`ui.groups`（2026-10-09 用户要求）。

用户原话：*"群聊页，应当展示**云茹所在的所有群聊**并展示**群名**而不是群号"*。

背景与边界：面板是插件，**拿不到 transport、也拿不到引擎**——群列表与群名**只有核心
能取**（`get_group_list` 那条路）。所以给面板一条**只读窄接缝**（照
`memory_ops` / `knowledge` / `ui.learned` 那三口的风格：逐字段列明 + 测试逐个钉住）：

    async () -> tuple[dict, ...]        # 每一项只有 id / name / dialogue_on / memory_on

这一份钉住：

1. **字段白名单**：每一项恰好四个键（多一个就红）；没有成员表、没有消息；
2. 群名来自 OneBot（`get_group_list`），两个开关来自引擎那两张名单；
3. **缓存**：`dev_config.groups_cache_seconds()`（默认 **600** 秒，调用时现读）之内
   连着调只打一次 OneBot；**取失败回落上一次的已知列表**（不许抛异常）；
   没有缓存过就用"开对话/开记忆"那两张名单自己拼（名字写成"群 <id>"）；
4. 名字空 → `群 <id>`（别显示空）；
5. 它是**函数、没有参数**（"顺手调个 action"那种口子不存在）。

突变（各自还原）：① 让这口能传 action 名（加一个参数转给 `call_api`）→ 第 5 条红；
② 去掉缓存（每次都打 OneBot）→ 第 3 条红；③ 取失败时抛异常 → 第 3 条红；
④ 返回值里多一个字段 → 第 1 条红。
"""
import asyncio
import inspect
import os
from unittest.mock import patch

from qq_roleplay_bot import dev_config, runtime

GROUP_A = "1084401296"
GROUP_B = "717151356"
GROUP_C = "555000111"
EXPECTED = frozenset({"id", "name", "dialogue_on", "memory_on"})


class _GroupsTransport:
    """够 `build_engine` 起得来，并按需要给出群列表 / 抛错。"""

    def __init__(self, groups=None, fail: bool = False) -> None:
        self.calls: list[str] = []
        self.fail = fail
        self.groups = groups if groups is not None else [
            {"group_id": GROUP_A, "group_name": "富哥之家"},
            {"group_id": GROUP_B, "group_name": ""},          # 名字空 → 回落"群 <id>"
            {"group_id": GROUP_C, "group_name": "没有人说话的群"},
        ]

    async def call_api(self, action, params=None):
        self.calls.append(action)
        if self.fail:
            raise RuntimeError("onebot 抖了一下")
        if action == "get_group_list":
            return {"status": "ok", "retcode": 0, "data": self.groups}
        return {"status": "ok", "retcode": 0, "data": {}}

    async def send(self, target, text, **kwargs):
        return None

    async def start(self):
        return None


def _engine(transport):
    engine = runtime.build_engine(transport)
    engine.enabled_group_ids = {GROUP_A}
    engine.memory_group_ids = {GROUP_B}
    return engine


def _seam(engine):
    return engine.plugin_registry.ui.groups


def _rows(engine) -> tuple:
    return asyncio.run(_seam(engine)())


# --- 1. 字段白名单 -----------------------------------------------------------


def test_every_row_has_exactly_the_four_fields() -> None:
    engine = _engine(_GroupsTransport())
    rows = _rows(engine)
    assert rows, "一个群都没返回"
    for row in rows:
        assert set(row) == EXPECTED, f"多给了字段：{set(row) - EXPECTED}"
    # 不给成员表、不给消息（连键名都不许有）。
    for row in rows:
        for banned in ("members", "member_list", "messages", "msg", "avatar",
                       "owner", "transport", "engine"):
            assert banned not in row, banned


def test_the_names_and_flags_come_from_the_core() -> None:
    engine = _engine(_GroupsTransport())
    by_id = {row["id"]: row for row in _rows(engine)}

    assert by_id[GROUP_A]["name"] == "富哥之家"
    assert by_id[GROUP_A]["dialogue_on"] is True and by_id[GROUP_A]["memory_on"] is False
    assert by_id[GROUP_B]["dialogue_on"] is False and by_id[GROUP_B]["memory_on"] is True
    assert by_id[GROUP_C]["dialogue_on"] is False and by_id[GROUP_C]["memory_on"] is False


def test_an_empty_name_falls_back_to_the_id() -> None:
    engine = _engine(_GroupsTransport())
    by_id = {row["id"]: row for row in _rows(engine)}
    assert by_id[GROUP_B]["name"] == f"群 {GROUP_B}", "名字空的时候不许显示空"


# --- 3. 缓存与失败回落 -------------------------------------------------------


def test_the_list_is_cached_within_the_window() -> None:
    transport = _GroupsTransport()
    engine = _engine(transport)
    with patch.dict(os.environ, {"QQBOT_GROUPS_CACHE_SECONDS": "600"}):
        first = _rows(engine)
        second = _rows(engine)

    assert first == second
    assert transport.calls.count("get_group_list") == 1, "缓存窗口内不该再打 OneBot"


def test_zero_disables_the_cache() -> None:
    transport = _GroupsTransport()
    engine = _engine(transport)
    with patch.dict(os.environ, {"QQBOT_GROUPS_CACHE_SECONDS": "0"}):
        _rows(engine)
        _rows(engine)
    assert transport.calls.count("get_group_list") == 2, "0 = 不缓存（给排查用）"


def test_flipping_a_switch_shows_up_on_the_next_read() -> None:
    """翻开关之后**立刻**再读就是新状态（2026-10-11 用户报"记忆开关不生效"）。

    开关本身一直是对的（消费侧每次现读闭包）；坏的是这一列状态：`memory_on` 是
    **填缓存那一刻**算出来的，而缓存最长 600 秒。生产审计里那颗开关 71.5 秒内被
    点了两次——"关完切页回来又显示开着，于是再关一次"，就是这条。名单一变缓存
    就得作废（突变自检：删掉 `_forget_groups_cache()` 那两处调用，这条必红）。
    """

    transport = _GroupsTransport()
    engine = _engine(transport)
    with patch.dict(os.environ, {"QQBOT_GROUPS_CACHE_SECONDS": "600"}):
        before = {row["id"]: row for row in _rows(engine)}
        assert before[GROUP_B]["memory_on"] is True
        assert before[GROUP_A]["dialogue_on"] is True

        assert engine.disable_group_memory(GROUP_B) is True
        assert engine.disable_group(GROUP_A) is True

        after = {row["id"]: row for row in _rows(engine)}

    assert after[GROUP_B]["memory_on"] is False, "关掉之后列表里必须已经是关"
    assert after[GROUP_A]["dialogue_on"] is False, "关对话那颗同理"
    assert set(after[GROUP_B]) == EXPECTED


def test_a_no_op_flip_keeps_the_cache() -> None:
    """已经开着的再"开"一次不算变了，缓存不必白作废（省一次 OneBot）。"""

    transport = _GroupsTransport()
    engine = _engine(transport)
    with patch.dict(os.environ, {"QQBOT_GROUPS_CACHE_SECONDS": "600"}):
        _rows(engine)
        assert engine.enable_group_memory(GROUP_B) is False, "本来就开着"
        _rows(engine)
    assert transport.calls.count("get_group_list") == 1


def test_a_failure_falls_back_to_the_last_known_list() -> None:
    """对面抖一下：**不抛异常**，把上一次拿到的列表原样给出去。"""

    transport = _GroupsTransport()
    engine = _engine(transport)
    with patch.dict(os.environ, {"QQBOT_GROUPS_CACHE_SECONDS": "0"}):
        good = _rows(engine)
        transport.fail = True
        again = _rows(engine)          # 这一次 call_api 抛错

    assert again == good, "取失败要回上一次的已知列表"
    assert set(again[0]) == EXPECTED


def test_a_failure_without_any_cache_still_returns_something() -> None:
    """一次都没取成功：用"开对话/开记忆"那两张名单自己拼，名字写"群 <id>"。"""

    transport = _GroupsTransport(fail=True)
    engine = _engine(transport)
    rows = _rows(engine)

    by_id = {row["id"]: row for row in rows}
    assert set(by_id) == {GROUP_A, GROUP_B}, by_id
    assert by_id[GROUP_A]["name"] == f"群 {GROUP_A}"
    assert by_id[GROUP_A]["dialogue_on"] is True
    assert by_id[GROUP_B]["memory_on"] is True


def test_a_group_list_that_says_nothing_falls_back_too() -> None:
    transport = _GroupsTransport(groups=[])
    engine = _engine(transport)
    rows = _rows(engine)
    assert {row["id"] for row in rows} == {GROUP_A, GROUP_B}, "空列表也走回落"


# --- 4 / 5. 窄到什么程度 -----------------------------------------------------


def test_the_seam_takes_no_arguments() -> None:
    """它**没有参数**——"顺手传个 action 名进去"那种口子不存在。"""

    engine = _engine(_GroupsTransport())
    seam = _seam(engine)
    assert list(inspect.signature(seam).parameters) == [], inspect.signature(seam)
    assert asyncio.iscoroutinefunction(seam), "群名要等 OneBot，所以是 async 的"


def test_the_cache_config_is_conservative_and_clamped() -> None:
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("QQBOT_GROUPS_CACHE_SECONDS", None)
        assert dev_config.groups_cache_seconds() == 600
    with patch.dict(os.environ, {"QQBOT_GROUPS_CACHE_SECONDS": "999999"}):
        assert dev_config.groups_cache_seconds() == 86400, "越界要夹住"
    with patch.dict(os.environ, {"QQBOT_GROUPS_CACHE_SECONDS": "不是数字"}):
        assert dev_config.groups_cache_seconds() == 600, "坏值回落默认"


if __name__ == "__main__":
    import unittest

    unittest.main()
