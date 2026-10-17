"""面板往**已启用对话的群**里发一句操作者的话：`ui.notice`（2026-10-18 用户定的）。

用户原话（这一笔的四条硬口径，一个字都不许改）：

1. *"不进她的对话历史（跟 `/admin relay` 一致：这是操作者的话，不是她想说的）"*；
2. *"只允许「已启用对话的群」（`enabled_group_ids`）。私聊一律不许发，其他群也不许。
   接缝层就拒，不要留给界面层挡。"*；
3. *"3 秒防误点：同一个群 + 完全相同的内容，3 秒内重复提交 → 拦回来并给出可读原因。
   这是防手抖，不是限权"*；
4. 面板不新开页：发送框在既有的「群聊」页每个启用群的卡片里（面板那棵树的事，
   这里只管核心这一半）。

这一份钉住：

* 接缝的形状：`(group_id, text, at_user_ids=())`，名字里**没有 chat**（它是通知，
  不是她自由发言），同步函数、返回结构化结果（`ok` / `reason`）；
* **四条守卫的判定顺序**：空文本 → 群号形状（私聊表达不出来）→ 只许
  `enabled_group_ids` → 3 秒同内容；
* `at_user_ids` **清洗而不是整条拒**（非数字丢掉并记账，最多认一个，理由见
  `notice_group` 的 docstring）；
* 投递**复用 relay 那条路**（`RelayDelivery` → `_relay_deliveries` →
  `drain_relay_deliveries` → `_deliver_relay`），这一侧**不碰 transport**；
* **不进对话历史**（不建会话、不动 `sessions`）；
* 每次尝试（成功与拒绝）都进 `control_audit`，动作名 `panel_notice`，
  字段含群号 / 字数 / @ 数量 / 结果；**不记正文**；
* 面板那一侧拿不到 `transport` / 引擎（`UiSeams` 里没有这两个东西）。

突变（各自还原）：① 去掉"群必须在 `enabled_group_ids` 里"那一判 → 第 2 条守卫红；
② 把去重窗口从 3 秒改成 0 → 3 秒那条红；③ 在 `notice_group` 里直接
`await transport.send(...)`（新写出站）→ "不碰 transport"那条红；
④ 把通知也 `state.add` 进会话 → "不进历史"那条红；⑤ 拒绝时不写审计 → 审计那条红。
"""
import asyncio
import json
import tempfile
from pathlib import Path

from qq_roleplay_bot import runtime
from qq_roleplay_bot.control_audit import ControlAudit
from qq_roleplay_bot.stage3_main import (
    NOTICE_CONTENT_MAX_LENGTH,
    NOTICE_DEDUPE_MAX_ENTRIES,
    NOTICE_DUPLICATE_WINDOW_SECONDS,
    NOTICE_MAX_AT_USERS,
    RelayDelivery,
    _deliver_relay,
)
from qq_roleplay_bot.transport import MessageTarget

GROUP_A = "1084401296"
GROUP_B = "717151356"
GROUP_OFF = "555000111"
USER_ID = "87654321"


class _Transport:
    """够 `build_engine` 起得来；记下**真的发出去**了什么（这一侧不该发任何东西）。"""

    def __init__(self) -> None:
        self.sent: list[tuple[MessageTarget, str]] = []
        self.ated: list[tuple[MessageTarget, str, str]] = []

    async def call_api(self, action, params=None):
        if action == "get_group_list":
            return {"status": "ok", "retcode": 0, "data": [
                {"group_id": GROUP_A, "group_name": "富哥之家"},
                {"group_id": GROUP_B, "group_name": "另一个群"},
            ]}
        return {"status": "ok", "retcode": 0, "data": {}}

    async def send(self, target, text, **kwargs):
        self.sent.append((target, text))

    async def send_at(self, target, user_id, text, **kwargs):
        self.ated.append((target, user_id, text))

    async def start(self):
        return None


def _engine(transport=None):
    transport = transport or _Transport()
    engine = runtime.build_engine(transport)
    engine.enabled_group_ids = {GROUP_A, GROUP_B}
    return engine, transport


def _seam(engine):
    return engine.plugin_registry.ui.notice


def _audit(engine):
    """给引擎挂一份**真的**审计（落临时目录），返回那个文件。"""

    tmp = tempfile.TemporaryDirectory()
    path = Path(tmp.name) / "control_audit.jsonl"
    engine.control_audit = ControlAudit(path)
    engine._notice_audit_tmp = tmp        # 让 TemporaryDirectory 活到用例结束
    return path


def _rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# --- 1. 接缝的形状 -----------------------------------------------------------


def test_the_seam_is_a_sync_function_named_notice_with_three_parameters() -> None:
    import inspect

    engine, _ = _engine()
    seam = _seam(engine)

    assert list(inspect.signature(seam).parameters) == ["group_id", "text", "at_user_ids"]
    assert not asyncio.iscoroutinefunction(seam), "它是同步接缝：只入队，发送归运行时的循环"
    # 名字里**不许出现 chat**：它是通知，不是她自由发言（用户定的）。
    assert "chat" not in seam.__name__.casefold()
    assert set(inspect.signature(engine.notice_group).parameters) >= {
        "group_id", "text", "at_user_ids"}


def test_the_seam_returns_a_reason_instead_of_raising() -> None:
    engine, _ = _engine()
    result = _seam(engine)(GROUP_A, "   ")

    assert isinstance(result, dict)
    assert result["ok"] is False
    assert isinstance(result["reason"], str) and result["reason"].strip(), "拒绝原因不能是空的"


# --- 2 / 3. 四条守卫（顺序也钉住）--------------------------------------------


def test_an_empty_or_blank_text_is_rejected_first() -> None:
    engine, transport = _engine()
    audit = _audit(engine)
    for blank in ("", "   ", "\n\t "):
        result = _seam(engine)(GROUP_A, blank)
        assert result["ok"] is False, blank
        assert "空" in result["reason"], result["reason"]
    assert engine.drain_relay_deliveries() == (), "空文本也排上了队"
    assert transport.sent == [] and transport.ated == []
    rows = _rows(audit)
    assert all(row["action"] == "panel_notice" for row in rows)
    assert len(rows) == 3, "每次尝试（包括被拒）都要留一笔"


def test_a_private_chat_target_cannot_be_expressed_and_bad_group_ids_are_rejected() -> None:
    """**私聊一律不许发**：这条接缝上私聊根本表达不出来（永远按 `group_id=` 造目标）。

    这里喂一个看起来像 QQ 号的东西（私聊号）与几个畸形群号：一律在接缝层就拒，
    **不留给界面层挡**（用户 2026-10-18）。
    """

    engine, _ = _engine()
    for bad in (USER_ID, "", "abc", "12345x", "１084401296", "1"):
        result = _seam(engine)(bad, "在吗")
        assert result["ok"] is False, bad
        assert "群" in result["reason"], (bad, result["reason"])
    assert engine.drain_relay_deliveries() == ()


def test_a_group_without_dialogue_is_rejected_with_a_readable_reason() -> None:
    engine, _ = _engine()
    result = _seam(engine)(GROUP_OFF, "在吗")

    assert result["ok"] is False
    assert GROUP_OFF in result["reason"] and "没开对话" in result["reason"]
    assert engine.drain_relay_deliveries() == (), "没开对话的群也排上了队"


def test_a_notice_to_an_enabled_group_is_queued_for_the_relay_queue() -> None:
    engine, transport = _engine()
    result = _seam(engine)(GROUP_A, "我先说一句：今晚八点开播。")

    assert result["ok"] is True and result["group_id"] == GROUP_A
    queued = engine.drain_relay_deliveries()
    assert len(queued) == 1
    delivery = queued[0]
    assert isinstance(delivery, RelayDelivery)
    # **只可能是群目标**（`user_id` 永远是 None）：私聊在这条路上不存在。
    assert delivery.target == MessageTarget(group_id=GROUP_A)
    assert delivery.target.user_id is None
    # 面板没有"发起它的 QQ 会话"：不许给谁补"转告已发送"的回执。
    assert delivery.admin_target is None
    assert transport.sent == [] and transport.ated == [], "接缝自己把消息发出去了"
    assert engine.drain_relay_deliveries() == (), "取走即清空（同一条不许发两次）"


def test_the_same_content_twice_within_three_seconds_is_rejected() -> None:
    engine, _ = _engine()
    now = [100.0]
    engine.clock = lambda: now[0]

    first = _seam(engine)(GROUP_A, "同一句话")
    assert first["ok"] is True
    assert engine.drain_relay_deliveries()

    now[0] = 100.0 + NOTICE_DUPLICATE_WINDOW_SECONDS - 0.1
    again = _seam(engine)(GROUP_A, "同一句话")
    assert again["ok"] is False
    assert "3 秒" in again["reason"] and "重复" in again["reason"], again["reason"]

    # 窗口一过，同样的话再发一次是**合法**的（防手抖不是限权）。
    now[0] = 100.0 + NOTICE_DUPLICATE_WINDOW_SECONDS
    assert _seam(engine)(GROUP_A, "同一句话")["ok"] is True


def test_the_duplicate_window_does_not_span_groups_or_different_text() -> None:
    engine, _ = _engine()
    now = [200.0]
    engine.clock = lambda: now[0]

    assert _seam(engine)(GROUP_A, "同一句话")["ok"] is True
    # 同一个群、不同内容：放行。
    assert _seam(engine)(GROUP_A, "另一句话")["ok"] is True
    # 不同群、同样内容：放行（"同一个群 + 完全相同的内容"才是重复）。
    assert _seam(engine)(GROUP_B, "同一句话")["ok"] is True
    assert len(engine.drain_relay_deliveries()) == 3


def test_a_rejected_attempt_does_not_occupy_the_duplicate_slot() -> None:
    """被拒的那次（没开对话）不占去重位置：把群打开之后，同一句话马上能发。"""

    engine, _ = _engine()
    now = [300.0]
    engine.clock = lambda: now[0]

    assert _seam(engine)(GROUP_OFF, "一句话")["ok"] is False
    assert engine.enable_group(GROUP_OFF) is True
    assert _seam(engine)(GROUP_OFF, "一句话")["ok"] is True


def test_the_dedupe_table_is_pruned_and_bounded() -> None:
    engine, _ = _engine()
    now = [400.0]
    engine.clock = lambda: now[0]

    for index in range(NOTICE_DEDUPE_MAX_ENTRIES + 40):
        now[0] = 400.0 + index * 10.0       # 每次都超出窗口，不会被判重复
        assert _seam(engine)(GROUP_A, f"第 {index} 句")["ok"] is True
    assert len(engine._notice_recent) <= NOTICE_DEDUPE_MAX_ENTRIES


# --- @：清洗而不是整条拒 -----------------------------------------------------


def test_at_ids_are_sanitized_dropped_and_counted() -> None:
    engine, _ = _engine()
    result = _seam(engine)(
        GROUP_A, "喊你一声", ("123456", "abc", "１２３", "777888"),
    )

    assert result["ok"] is True
    delivery = engine.drain_relay_deliveries()[0]
    assert delivery.at_user_ids == ("123456",), "只认第一个合法 QQ 号（出站只有 send_at）"
    # 非数字（含全角数字）与超出的那一个都**丢掉并记账**，不是整条拒。
    assert result["at_dropped"] == 3
    assert NOTICE_MAX_AT_USERS == 1


def test_the_notice_text_is_trimmed_and_bounded() -> None:
    engine, _ = _engine()
    result = _seam(engine)(GROUP_A, "啊" * (NOTICE_CONTENT_MAX_LENGTH + 50))

    assert result["ok"] is True
    delivery = engine.drain_relay_deliveries()[0]
    assert len(delivery.text) == NOTICE_CONTENT_MAX_LENGTH
    assert _seam(engine)(GROUP_A, "  一句话  ")["ok"] is True
    assert engine.drain_relay_deliveries()[0].text == "一句话"


# --- 4. 投递复用 relay 那条路，这一侧不碰 transport --------------------------


def test_delivery_uses_the_existing_relay_sender_without_an_admin_echo() -> None:
    engine, transport = _engine()
    assert _seam(engine)(GROUP_A, "一句通知")["ok"] is True
    delivery = engine.drain_relay_deliveries()[0]

    asyncio.run(_deliver_relay(transport, delivery))

    assert transport.sent == [(MessageTarget(group_id=GROUP_A), "一句通知")], \
        "多发了东西（`admin_target=None` 时不许补「转告已发送」）"


def test_the_at_goes_through_the_transports_existing_send_at_capability() -> None:
    engine, transport = _engine()
    assert _seam(engine)(GROUP_A, "一句通知", ("123456",))["ok"] is True
    delivery = engine.drain_relay_deliveries()[0]

    asyncio.run(_deliver_relay(transport, delivery))

    assert transport.ated == [(MessageTarget(group_id=GROUP_A), "123456", "一句通知")]
    assert transport.sent == []


def test_a_transport_without_send_at_still_gets_the_plain_text() -> None:
    class _NoAt:
        def __init__(self) -> None:
            self.sent: list[tuple[MessageTarget, str]] = []

        async def send(self, target, text, **kwargs):
            self.sent.append((target, text))

    engine, _ = _engine()
    assert _seam(engine)(GROUP_A, "一句通知", ("123456",))["ok"] is True
    delivery = engine.drain_relay_deliveries()[0]

    transport = _NoAt()                   # 没有 send_at 这个可选能力
    asyncio.run(_deliver_relay(transport, delivery))
    assert transport.sent == [(MessageTarget(group_id=GROUP_A), "一句通知")], \
        "少一个 @ 可以接受，消息发不出去不行"


def test_the_notice_path_never_touches_transport_or_sends_anything() -> None:
    """接缝只入队：它**一次传输层都不碰**（谁要是顺手 `transport.send` 当场炸）。"""

    class _Boom(_Transport):
        async def call_api(self, action, params=None):
            raise AssertionError(f"通知那条路自己调了 OneBot：{action}")

        async def send(self, *args, **kwargs):
            raise AssertionError("通知那条路自己发了消息（新写出站）")

        async def send_at(self, *args, **kwargs):
            raise AssertionError("通知那条路自己 @ 了人（新写出站）")

    engine, _ = _engine(_Boom())
    assert _seam(engine)(GROUP_A, "一句通知", ("123456",))["ok"] is True
    assert len(engine.drain_relay_deliveries()) == 1


def test_the_focus_tick_is_a_second_drain_point() -> None:
    """`_handle_one` 只在**收到消息**时取件——安静群里的通知得靠节拍取走。"""

    import inspect

    from qq_roleplay_bot import runtime as runtime_module

    loop_source = inspect.getsource(runtime_module._message_loop)
    assert loop_source.count("engine.drain_relay_deliveries()") == 2, \
        "转告/通知的取件点不是一个消息处理 + 一个节拍（安静群里会一直躺着）"


# --- 5. 不进她的对话历史 -----------------------------------------------------


def test_a_notice_never_enters_her_conversation_history() -> None:
    engine, _ = _engine()
    assert engine.sessions.states == {}

    assert _seam(engine)(GROUP_A, "今晚八点开播。")["ok"] is True

    assert engine.sessions.states == {}, "通知进了她的会话（那是她想说的话才该有的）"
    assert f"group:{GROUP_A}" not in engine.sessions.states


# --- 6. 审计：每次尝试都留痕，只记计数不记正文 -------------------------------


def test_every_attempt_lands_in_the_control_audit_without_the_body() -> None:
    engine, _ = _engine()
    path = _audit(engine)

    assert _seam(engine)(GROUP_A, "成功的这一句", ("123456",))["ok"] is True
    assert _seam(engine)(GROUP_OFF, "被拒的这一句")["ok"] is False

    rows = _rows(path)
    assert [row["action"] for row in rows] == ["panel_notice", "panel_notice"]
    assert all(row["source"] == "panel" for row in rows)
    ok_row, rejected_row = rows
    assert ok_row["result"] == "queued"
    assert ok_row["detail"]["group_id"] == GROUP_A
    # `ControlAudit.summarize` 把一切压成字符串，所以这里对字符串。
    assert ok_row["detail"]["chars"] == str(len("成功的这一句"))
    assert ok_row["detail"]["at"] == "1"
    assert rejected_row["result"].startswith("rejected:")
    assert rejected_row["detail"]["group_id"] == GROUP_OFF
    # **正文不进审计**（`data/` 里不该再多一份群里的话）。
    dumped = json.dumps(rows, ensure_ascii=False)
    assert "成功的这一句" not in dumped and "被拒的这一句" not in dumped


# --- 7. 面板拿不到 transport / 引擎（既有硬边界）------------------------------


def test_the_panel_seams_hold_functions_not_the_engine_or_the_transport() -> None:
    engine, _ = _engine()
    seams = engine.plugin_registry.ui

    assert not hasattr(seams, "transport"), "接缝把 transport 递给面板了"
    assert not hasattr(seams, "engine"), "接缝把引擎递给面板了"
    assert callable(seams.notice)

    import inspect

    params = list(inspect.signature(seams.notice).parameters)
    assert params == ["group_id", "text", "at_user_ids"], params


if __name__ == "__main__":
    import unittest

    unittest.main()
