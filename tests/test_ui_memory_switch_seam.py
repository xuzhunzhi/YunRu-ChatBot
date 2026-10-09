"""面板「记忆」页那颗**按群开关**：`ui.set_group_memory`（2026-10-09 用户要求）。

用户原话：*"**记忆开关肯定要上面板啊，在记忆页**"*。

形状（**只包一层**，不造新状态）：

    # UiSeams.set_group_memory
    (group_id: str, enabled: bool) -> bool     # 返回"这次到底改没改"

它转发到引擎**既有**的 `enable_group_memory` / `disable_group_memory`
（`/super enable memory` 那套：校验、幂等、落盘与恢复都是现成的）——
不给记忆内容、不给记忆 API、不给任意 action。审计走既有 `control_audit`，
动作名与命令那条**相同**（`memory_group_switch`），`source="panel"` 区分是谁按的。

这一份钉住：

1. 真的写进引擎那份名单（`memory_group_ids`），而且**不碰** `enabled_group_ids`
   （"回不回"与"记不记"是两张名单，2026-10-08 用户定过）；
2. 群号校验：非数字 / 太短 / 太长 → `False` 且什么都没变；
3. 幂等：本来就是这个状态 → `False`（面板据此显示"本来就是这个状态"）；
4. 审计：一次调用留一笔（动作名、群号、开关、`source`）；
5. **参数白名单**：只收"群号 + 布尔"，多传一样就 `TypeError`；
6. 与命令共用同一份状态（不新造）：接缝开过之后命令那条返回"没改"。

突变（各自还原）：① 接缝多收一样东西（`action`）→ 第 5 条红；
② 跳过既有校验、直接改集合 → 第 2 条红；③ 去掉审计 → 第 4 条红。
"""
import inspect

from qq_roleplay_bot import runtime

GROUP = "1084401296"
OTHER = "717151356"


class _Transport:
    """够 `build_engine` 起得来就行（这条接缝不打 OneBot）。"""

    async def call_api(self, action, params=None):
        return {"status": "ok", "retcode": 0, "data": []}

    async def send(self, target, text, **kwargs):
        return None

    async def start(self):
        return None


class _Audit:
    def __init__(self) -> None:
        self.entries: list[tuple[str, dict, str, str]] = []

    def record(self, action, *, detail=None, result="", source=""):
        self.entries.append((action, dict(detail or {}), result, source))


def _engine():
    engine = runtime.build_engine(_Transport())
    engine.control_audit = _Audit()
    engine.memory_group_ids = set()
    engine.enabled_group_ids = {OTHER}
    return engine


def _seam(engine):
    return engine.plugin_registry.ui.set_group_memory


# --- 1 / 3. 写进既有名单、幂等 -----------------------------------------------


def test_the_switch_writes_into_the_existing_list() -> None:
    engine = _engine()
    seam = _seam(engine)

    assert seam(GROUP, True) is True
    assert GROUP in engine.memory_group_ids
    assert GROUP not in engine.enabled_group_ids, "记忆开关不许顺手动'回不回'那张名单"
    assert OTHER in engine.enabled_group_ids, "别人的名单一个字都不许动"

    # 幂等：本来就是这个状态 → False（面板据此显示"本来就是这个状态"）。
    assert seam(GROUP, True) is False
    assert seam(GROUP, False) is True
    assert GROUP not in engine.memory_group_ids
    assert seam(GROUP, False) is False


def test_it_shares_the_state_with_the_command() -> None:
    """接缝与 `/super enable memory` 是**同一份状态**（不新造第二份）。"""

    engine = _engine()
    _seam(engine)(GROUP, True)
    assert engine.enable_group_memory(GROUP) is False, "命令那条该看到'已经开着'"


# --- 2. 群号校验 -------------------------------------------------------------


def test_a_bad_group_id_changes_nothing() -> None:
    engine = _engine()
    seam = _seam(engine)
    for bad in ("", "abc", "1234", "1234567890123", "12345abc", "１２３４５６"):
        assert seam(bad, True) is False, bad
    assert engine.memory_group_ids == set()
    assert len(engine.control_audit.entries) == len(
        ("", "abc", "1234", "1234567890123", "12345abc", "１２３４５６")), \
        "点了就留一笔审计（与命令那条一致）"


# --- 4. 审计 -----------------------------------------------------------------


def test_the_switch_is_audited_like_the_command() -> None:
    engine = _engine()
    _seam(engine)(GROUP, True)
    action, detail, result, source = engine.control_audit.entries[-1]

    assert action == "memory_group_switch", "与命令那条同名：一次筛选看全这个群的开关史"
    assert detail == {"group_id": GROUP, "memory": "1"}
    assert result == "on"
    assert source == "panel", "区分是谁按的（命令那条是 super）"

    _seam(engine)(GROUP, False)
    assert engine.control_audit.entries[-1][1] == {"group_id": GROUP, "memory": "0"}
    assert engine.control_audit.entries[-1][2] == "off"


# --- 5. 参数白名单 -----------------------------------------------------------


def test_the_seam_only_takes_a_group_id_and_a_bool() -> None:
    """**多一样就红**：它不接受 action 名、也不接受记忆内容。"""

    engine = _engine()
    seam = _seam(engine)
    assert list(inspect.signature(seam).parameters) == ["group_id", "enabled"], \
        inspect.signature(seam)
    for extra in (("group", True, "kick"), ("group", True, {"content": "x"})):
        try:
            seam(*extra)
        except TypeError:
            continue
        raise AssertionError(f"多传一样竟然收下了：{extra}")


def test_the_seam_degrades_when_the_engine_lacks_the_methods() -> None:
    """引擎没有那两个方法（裁掉能力的部署）→ 如实返回 False，不炸。"""

    class _Bare:
        pass

    from qq_roleplay_bot.runtime import _SeamBinder

    seam = _SeamBinder(_Bare()).ui_set_group_memory
    assert seam(GROUP, True) is False


if __name__ == "__main__":
    import unittest

    unittest.main()
