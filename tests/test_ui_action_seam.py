"""面板的两条**执行类接缝**：`UiSeams.execute_action` / `UiSeams.restart`（2026-10-22 补）。

## 为什么单独一份（这组用例就是"未测 = 已坏"那件事的善后）

`plugins/webui` 在这棵树上看不到（插件源码在 `plugins` 分支），所以面板那条路
一直是"插件树自己测自己"：面板按 4 个关键字调、接缝只收 1 个参数，**没有任何东西拦**。
线上表现是「动作」页每个按钮 `internal_error`（`run/data/bot.err.log` 约 105736 行，
2026-10-22 01:02:42 的 `TypeError`）；「重启」按钮同样是 500（面板传
`webui:<来源>`，接缝不收）。而且**就算面板只传 `request` 也照样炸**：
`stage3_main.execute_action` 的 `group_id` / `actor_id` 是必填 keyword-only，
闭包一个都没转发。

## 这一份钉什么

1. **签名对照**：`_SeamBinder.ui_execute_action` 的参数名 / 种类 / 默认值必须与
   `DialogueEngine.execute_action` **逐个相同**；`ui_restart` 与 `request_restart` 同理。
   "以后核心改了签名、插件跟着红"就是靠这一条，不靠谁记得。
2. **真 `_SeamBinder` + 假引擎**：四个动作参数（`group_id` / `actor_id` /
   `message_id` / `mentioned`）逐个送到假引擎，一个都不许被吞。
3. **重启的来源**：`webui:<来源>` 原样到 `engine.request_restart(source)`。
4. **必填就是必填**：漏了 `group_id` / `actor_id` 当场 `TypeError`，
   不静默按空串执行（那等于替核心回答"动作作用在哪个群、以谁的名义"）。

突变（各自还原）：① 接缝退回只收 `request` → 第 1、2 条红；
② 把 `group_id`/`actor_id` 补成默认空串 → 第 1、4 条红；
③ 重启接缝不转发 `source` → 第 3 条红。
"""
from __future__ import annotations

import asyncio
import inspect

from qq_roleplay_bot import runtime
from qq_roleplay_bot.command_plugins import ActionRequest
from qq_roleplay_bot.stage3_main import DialogueEngine

GROUP = "717151356"
ACTOR = "10001000"
REQUEST_ID = "m-20261022-001"


class _FakeEngine:
    """**假引擎**：只实现 `_SeamBinder` 这一路真正会调到的两个方法，并把实参记下来。

    刻意不造真 `DialogueEngine`：这一份测的是"接缝转发得对不对"，不是核心的判定。
    核心的权限/护栏/审计各自有覆盖，接缝层**不该**在这里替它判任何东西。
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def execute_action(self, request, *, group_id, actor_id,
                             message_id="", mentioned=False):
        self.calls.append({"request": request, "group_id": group_id,
                           "actor_id": actor_id, "message_id": message_id,
                           "mentioned": mentioned})
        return "已执行（假引擎）"

    def request_restart(self, source=""):
        self.calls.append({"restart_source": source})
        return "正在重启，几秒后回来。"


def _seams(engine):
    return runtime._SeamBinder(engine).ui_seams()


# --- 1. 签名对照（核心是唯一的那一份） ----------------------------------------


def _assert_mirrors(seam_func, core_func, *, skip_self: bool) -> None:
    seam = inspect.signature(seam_func)
    core = inspect.signature(core_func)
    keep = (lambda name: name != "self") if skip_self else (lambda name: True)
    core_params = [name for name in core.parameters if keep(name)]
    seam_params = [name for name in seam.parameters if keep(name)]
    assert seam_params == core_params, (
        f"接缝参数 {seam_params} 与核心 {core_params} 对不上")
    for name in core_params:
        assert seam.parameters[name].kind == core.parameters[name].kind, (
            f"{name} 的参数种类与核心不一致（位置/关键字-only 混了）")
        assert seam.parameters[name].default == core.parameters[name].default, (
            f"{name} 的默认值与核心不一致（{seam.parameters[name].default!r} != "
            f"{core.parameters[name].default!r}）")


def test_the_action_seam_mirrors_the_core_signature() -> None:
    _assert_mirrors(runtime._SeamBinder.ui_execute_action,
                    DialogueEngine.execute_action, skip_self=True)


def test_the_restart_seam_mirrors_the_core_signature() -> None:
    _assert_mirrors(runtime._SeamBinder.ui_restart,
                    DialogueEngine.request_restart, skip_self=True)


# --- 2. 真 binder + 假引擎：四个动作参数逐个透传 ------------------------------


def test_all_four_action_arguments_reach_the_engine() -> None:
    """这四条正是面板 `_action()` 传的那四个（`webui_panel.py` 的 `_call(...)`）。"""

    engine = _FakeEngine()
    seams = _seams(engine)
    request = ActionRequest(group="group_admin", kind="kick",
                            target_id="424242", mentioned=True)

    text = asyncio.run(seams.execute_action(
        request, group_id=GROUP, actor_id=ACTOR,
        message_id=REQUEST_ID, mentioned=True))

    assert text == "已执行（假引擎）"
    assert engine.calls == [{"request": request, "group_id": GROUP,
                             "actor_id": ACTOR, "message_id": REQUEST_ID,
                             "mentioned": True}], engine.calls


def test_the_optional_arguments_keep_the_core_defaults() -> None:
    """`message_id` / `mentioned` 跟核心一样可省（群命令那条路就不传这两个）。"""

    engine = _FakeEngine()
    seams = _seams(engine)
    request = ActionRequest(group="group_admin", kind="kick", target_id="424242")

    asyncio.run(seams.execute_action(request, group_id=GROUP, actor_id=ACTOR))

    assert engine.calls[0]["message_id"] == ""
    assert engine.calls[0]["mentioned"] is False


def test_missing_required_arguments_raise_instead_of_running_with_blanks() -> None:
    """**必填就是必填**：静默补空串等于替核心回答"作用在哪个群、以谁的名义"。"""

    engine = _FakeEngine()
    seams = _seams(engine)
    request = ActionRequest(group="group_admin", kind="kick", target_id="424242")

    for kwargs in ({"actor_id": ACTOR}, {"group_id": GROUP}):
        try:
            asyncio.run(seams.execute_action(request, **kwargs))
        except TypeError:
            continue
        raise AssertionError(f"缺参数 {sorted(kwargs)} 竟然执行了：{engine.calls}")
    assert engine.calls == [], "缺参数时不该碰引擎一下"


# --- 3. 重启：来源原样到核心 -------------------------------------------------


def test_restart_forwards_the_source() -> None:
    engine = _FakeEngine()
    seams = _seams(engine)

    text = seams.restart(f"webui:{'127.0.0.1'}")

    assert text == "正在重启，几秒后回来。"
    assert engine.calls == [{"restart_source": "webui:127.0.0.1"}], engine.calls


def test_restart_without_a_source_matches_the_command_path() -> None:
    """`/super restart` 那条路本来就不给来源，接缝的默认值必须与核心一致（空串）。"""

    engine = _FakeEngine()
    _seams(engine).restart()

    assert engine.calls == [{"restart_source": ""}], engine.calls


# --- 4. 装配：这两条真的挂在 `UiSeams` 上（不是留在 None） --------------------


def test_the_ui_seams_carry_the_action_and_restart_closures() -> None:
    seams = _seams(_FakeEngine())

    assert seams.execute_action is not None, "`UiSeams.execute_action` 是 None，面板永远 503"
    assert seams.restart is not None, "`UiSeams.restart` 是 None，重启按钮永远 503"
    # 面板 `_call()` 判的就是 `inspect.isawaitable(result)`（同步接缝照直调、异步的交给
    # 注入的 runner 跨线程跑）。**别按 `iscoroutinefunction` 判**：Python 3.14 的
    # `inspect.iscoroutinefunction` 对**绑定方法**回 False（3.14.6 实测），而这里正是
    # 绑定方法——用错了这条会假红，然后有人会去"修"一个没坏的东西。
    pending = seams.execute_action(
        ActionRequest(group="group_admin", kind="kick", target_id="424242"),
        group_id=GROUP, actor_id=ACTOR)
    try:
        assert inspect.isawaitable(pending), (
            "动作接缝不是协程：面板靠注入的 runner 跨线程跑它，别改成同步假装成功")
    finally:
        pending.close()          # 只验形状，不跑；关掉免得留"协程从没被 await"的残骸
