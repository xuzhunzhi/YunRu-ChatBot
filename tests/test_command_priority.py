"""命令不排在一堆对话后面（2026-09-30 用户："我发一个命令一分多钟才回复"）。

真机日志给的数字（群 800000001，18 分钟里她回了 40+ 条）：

```
17:52:56 回复 1/2 → 17:52:59 回复 2/2 → 17:53:16 → 17:53:58 → 17:54:57 …
```

每条对话的成本是「判定 ~2s + 拟人停顿 2~4.6s」，而 `receive_loop` 是**严格串行**的。
命令只要几毫秒（不进模型），却要排在几十条对话后面 —— 这就是那一分多钟。

两处改动的边界都钉在这里：

1. **命令优先**：主循环把一批里已经排队的消息一起拿出来，命令先跑（同批内相对顺序不变）；
2. **积压时去掉拟人停顿**：停顿是装饰，积压时它只会让积压更长。
"""
import asyncio

from qq_roleplay_bot.stage3_main import DialogueEngine, _as_outgoing_list, _deliver_reply
from qq_roleplay_bot.transport import IncomingMessage, MessageTarget

GROUP = "800000001"
ME = "900000001"


class NeverCalled:
    async def complete(self, request):  # pragma: no cover - 命令不该碰模型
        raise AssertionError("命令不该调用模型")


def msg(text: str, *, user_id: str = ME, mid: str = "", mentioned: bool = False) -> IncomingMessage:
    return IncomingMessage(
        message_id=mid or f"m:{text}:{user_id}", session_id=f"group:{GROUP}", user_id=user_id,
        text=text, target=MessageTarget(group_id=GROUP), is_bot_mentioned=mentioned,
    )


# --- 谁算"命令" ---------------------------------------------------------------

def test_handles_command_recognises_the_command_paths() -> None:
    engine = DialogueEngine(NeverCalled(), super_admin_user_ids=frozenset({ME}))
    # 插件（公开）——注意 `/ping` 本身**不是**命令，只认 `/yunru ping` 与"@她 + ping"
    assert engine.handles_command(msg("/yunru ping")) is True
    assert engine.handles_command(msg("ping", mentioned=True)) is True
    assert engine.handles_command(msg("/ping")) is False
    assert engine.handles_command(msg("/help")) is True
    # 组内命令：/admin help 与 /super ...
    assert engine.handles_command(msg("/admin help")) is True
    assert engine.handles_command(msg("/super status")) is True
    # 群管理命令（`/super kick`…）是 Stage 4 插件，**不在这条分支上**，
    # 所以这里不认识它；认领它的是 `stage4-plugins` 分支。
    assert engine.handles_command(msg("/super kick @某人")) is False
    # 普通聊天不是命令
    assert engine.handles_command(msg("今天心情不错")) is False
    assert engine.handles_command(msg("/admin enable 800000001")) is False  # 不是 help 那条
    # 她自己的消息永远不是命令
    assert engine.handles_command(
        IncomingMessage(message_id="x", session_id=f"group:{GROUP}", user_id="900000002",
                        text="/super status", target=MessageTarget(group_id=GROUP),
                        is_bot_message=True)) is False


def test_handles_command_does_not_authenticate() -> None:
    """没权限的人发 `/super ...` 也算命令（会被静默丢掉）——提前处理它没有副作用。"""

    engine = DialogueEngine(NeverCalled(), super_admin_user_ids=frozenset({ME}))
    assert engine.handles_command(msg("/super restart", user_id="10086")) is True


# --- 拟人停顿：积压时去掉 -----------------------------------------------------

class SendingTransport:
    def __init__(self):
        self.sent: list[str] = []

    async def send(self, target, text, *, reply_to=""):
        self.sent.append(text)


def _engine(**kwargs) -> DialogueEngine:
    """用**真引擎**（它带着 clock / metrics / logs），不给假对象补一堆属性。"""

    engine = DialogueEngine(NeverCalled(), super_admin_user_ids=frozenset({ME}), **kwargs)
    engine.typing_sim = True
    return engine


def _outgoing(text: str, *, paced: bool = True):
    from qq_roleplay_bot.transport import OutgoingMessage

    return OutgoingMessage(MessageTarget(group_id=GROUP), text, paced=paced)


def test_backlog_skips_the_human_pause() -> None:
    """`backlog=True` → 不睡停顿、立刻发（不然积压会越滚越长）。"""

    class SendingTransport:
        def __init__(self):
            self.sent: list[str] = []

        async def send(self, target, text, *, reply_to=""):
            self.sent.append(text)

    async def run(backlog: bool, transport):
        await _deliver_reply(transport, _engine(), msg("在吗"),
                             [_outgoing("第一条"), _outgoing("第二条")], backlog=backlog)

    transport = SendingTransport()
    loop = asyncio.new_event_loop()
    try:
        started = loop.time()
        loop.run_until_complete(run(True, transport))
        elapsed = loop.time() - started
        assert transport.sent == ["第一条", "第二条"]
        # 拟人停顿是 2~4 秒级别（delay_plan），积压时必须一点不睡
        assert elapsed < 1.0, f"积压时不该有停顿，实测 {elapsed:.2f}s"
    finally:
        loop.close()


def test_commands_are_never_paused_even_without_backlog() -> None:
    """命令回复本来就 `paced=False`，与积压无关——这条防的是"顺手给命令加节奏"。"""

    from qq_roleplay_bot.transport import OutgoingMessage

    class SendingTransport:
        def __init__(self):
            self.sent: list[str] = []

        async def send(self, target, text, *, reply_to=""):
            self.sent.append(text)

    transport = SendingTransport()
    command = OutgoingMessage(MessageTarget(group_id=GROUP), "pong", paced=False)
    asyncio.run(_deliver_reply(transport, _engine(), msg("/yunru ping"), [command]))
    assert transport.sent == ["pong"]


# --- 主循环：命令优先 ---------------------------------------------------------

class QueueTransport:
    """带 `drain_pending` 的假传输层（真传输层加了同名方法，见 `onebot_ws`）。"""

    def __init__(self, items):
        self.items = list(items)
        self.sent: list[str] = []

    async def receive(self):
        return self.items.pop(0) if self.items else None

    def drain_pending(self):
        drained, self.items = self.items, []
        return drained

    async def send(self, target, text, *, reply_to=""):
        self.sent.append(text)


class RecordingEngine(DialogueEngine):
    """真引擎 + 只换掉"处理"那一步：用它观察处理顺序，省得给假对象补一堆属性。"""

    def __init__(self):
        super().__init__(NeverCalled())
        self.typing_sim = False
        self.order: list[str] = []

    def handles_command(self, message):
        return message.text.startswith("/")

    async def handle(self, message):
        self.order.append(message.text)
        from qq_roleplay_bot.transport import OutgoingMessage

        return OutgoingMessage(message.target, f"回:{message.text}")

    def take_follow_ups(self, session_id):
        return []

    def drain_relay_deliveries(self):
        return []

    def consume_restart_request(self):
        return False

    async def tick(self):
        return []


def test_message_loop_runs_commands_first() -> None:
    """一批里混着对话与命令时，命令先处理（同批内相对顺序不变）。"""

    from qq_roleplay_bot.runtime import _message_loop

    transport = QueueTransport([msg("闲聊一"), msg("闲聊二"), msg("/ping"), msg("闲聊三")])
    engine = RecordingEngine()

    async def main():
        task = asyncio.create_task(_message_loop(transport, engine, tick_seconds=0.01))
        await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(main())
    assert engine.order[0] == "/ping", engine.order
    assert engine.order[1:] == ["闲聊一", "闲聊二", "闲聊三"], engine.order


def test_delivery_helper_passes_backlog_through() -> None:
    """`_deliver_reply` 的 `backlog` 参数是主循环传的；签名不能被改掉。"""

    import inspect

    signature = inspect.signature(_deliver_reply)
    assert "backlog" in signature.parameters
    assert signature.parameters["backlog"].default is False
    assert _as_outgoing_list(None) == []
