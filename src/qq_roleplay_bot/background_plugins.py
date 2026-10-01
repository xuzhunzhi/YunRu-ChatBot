"""Stage 4 的后台插件：不是命令，是"隔一会儿干一件事"（用户 2026-09-30 要求）。

由来：用户先要求"stage4 的内容都用插件实现"，把群管理与群主命令搬进插件之后，
又指出"**入群审批也是插件，属于 stage4 内容**"。它确实不是命令——没人发一句什么，
它是自己按节拍去查有没有待处理的入群申请。所以插件接口要分两种：

| 种类 | 触发 | 长什么样 |
| --- | --- | --- |
| **命令插件**（`command_plugins.py`） | 一条消息进来 | `match()` / `handle()` → 文本 / `ImageReply` / `ActionRequest` |
| **后台插件**（这里） | 时间到了 | `name` / `interval_seconds` / `poll_once()`，可选 `close()` |

共同约束不变：**插件不直接持有 `transport`**。后台插件要动外部世界时，走核心注入的
两个窄接缝：

- `call_action(action, params)`：核心先过 `capabilities` 闸门（按用途），再调对面，
  并把回执拆成 `data` 返回；失败抛异常。插件因此不需要知道协议长什么样；
- `notify(target, text)`：核心统一发送（走补发队列）。

**`runtime.serve` 不再认识任何具体通道**：它只问 `build_background_plugins()` 拿到一串
插件，给每个插件跑同一个节拍循环。加一个新的后台能力 = 加一个插件，不改主循环。
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Protocol

logger = logging.getLogger(__name__)

# 启动后第一轮之前等一会儿：对面（NapCat）常常比 bot 晚几秒连上来。
FIRST_TICK_DELAY_SECONDS = 20.0
# 节拍下限：防配置写个 1 秒把对面打爆。
MIN_INTERVAL_SECONDS = 15.0


class BackgroundPlugin(Protocol):
    """一个后台插件。"""

    name: str
    #: 每隔多少秒跑一次 `poll_once`。
    interval_seconds: float
    #: 关掉时核心不会为它起任务（装配阶段就能判断，不用等第一轮）。
    enabled: bool

    async def poll_once(self) -> object:
        """跑一轮。**不许抛出**（核心会兜一层，但自己处理掉更好记日志）。"""

    async def close(self) -> None:
        """可选：收尾。节拍任务被取消时由 `run_background_plugin` 调一次。

        2026-10-01 补：这个钩子原来只写在文档里、**没有任何地方调它**（模块头那句
        "可选 `close()`"）。面板插件要持有 HTTP 线程，收尾必须有个确定的落点，
        所以把它真正接上了：`run_background_plugin` 的 `finally` 里调。
        没有 `close()` 的插件照旧（其它几个不需要）。
        """


def plugin_enabled(plugin: object) -> bool:
    """插件是否启用。没声明 `enabled` 的按启用处理（它自己会在 poll_once 里判断）。"""

    return bool(getattr(plugin, "enabled", True))


async def run_background_plugin(plugin: BackgroundPlugin, *, first_delay: float | None = None,
                                min_interval: float = MIN_INTERVAL_SECONDS) -> None:
    """**唯一的后台节拍**：等一会儿 → 反复 `poll_once`，出错只记日志、不停。

    `runtime.serve` 里每个插件一个任务，都跑这个函数——所以"加一条后台通道"
    不需要再写一个 `while True` 循环（以前邮箱、入群审批、每日汇报各写了一份）。

    `min_interval` 是节拍下限（默认 15 秒，防配置写个 1 秒把对面打爆）；
    测试会把它调小，不然一轮要等 15 秒。

    退出时（任务被取消）调插件自己的 `close()`——这是插件收尾的**唯一确定时机**。
    """

    delay = FIRST_TICK_DELAY_SECONDS if first_delay is None else float(first_delay)
    interval = max(float(min_interval), float(getattr(plugin, "interval_seconds", 60.0) or 60.0))
    try:
        if delay > 0:
            await asyncio.sleep(delay)
        while True:
            try:
                await plugin.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - 一轮失败不能停掉这条通道
                logger.exception("后台插件这一轮出错 name=%s，继续等下一次",
                                 getattr(plugin, "name", "?"))
            await asyncio.sleep(interval)
    finally:
        closer = getattr(plugin, "close", None)
        if callable(closer):
            try:
                result = closer()
                if asyncio.iscoroutine(result):
                    await result
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - 收尾失败也不能带走别的任务
                logger.exception("后台插件收尾出错 name=%s", getattr(plugin, "name", "?"))


# --- 具体插件：把已有的通道包成同一形状 -------------------------------------
#
# 这些类**只做装配与节拍**，业务逻辑还在各自的模块里（`mail_channel.py`、
# `daily_report.py`、`join_approval.py`）。搬进来的原因是"Stage 4 的东西怎么接上"
# 应当集中在一处，而不是散在 `runtime.serve` 里三个 `create_task`。


class MailChannelPlugin:
    """读信回信（`mail_channel.py`）。它走引擎的公开接缝，本来就不碰 transport。"""

    name = "mail-channel"

    def __init__(self, channel, *, interval_seconds: float) -> None:
        self.channel = channel
        self.interval_seconds = float(interval_seconds)
        self.enabled = channel is not None

    async def poll_once(self) -> object:
        return await self.channel.poll_once()


class DailyReportPlugin:
    """每日汇报（`daily_report.py`）：到点就写一封发出去。

    "到点"由 `DailyReporter.due()` 判——所以这里只是个包装，不是调度实现。
    """

    name = "daily-report"

    def __init__(self, reporter, engine, *, interval_seconds: float) -> None:
        self.reporter = reporter
        self.engine = engine
        self.interval_seconds = float(interval_seconds)
        self.enabled = reporter is not None

    async def poll_once(self) -> object:
        if self.reporter is None or not self.reporter.due():
            return None
        return await self.reporter.run_once(self.engine)


class JoinApprovalPlugin:
    """入群申请自动审批（`join_approval.py`）。

    它是**策略**那一半：黑名单/白名单/正则怎么判、同一批别重复处理、每轮最多几条。
    读申请、批/拒、通知超管走核心注入的 `call_action` / `notify`——
    插件手上没有 transport，也就没法绕开闸门与审计。
    """

    name = "join-approval"

    def __init__(self, poller, *, interval_seconds: float, enabled: bool = True) -> None:
        self.poller = poller
        self.interval_seconds = float(interval_seconds)
        self.enabled = bool(enabled) and poller is not None

    async def poll_once(self) -> object:
        return await self.poller.tick()


def build_background_plugins(engine, *, call_action=None, notify=None, roles=None,
                             loop=None, transport=None):
    """装配所有后台插件。**Stage 4 的唯一装配点**（`runtime.serve` 只管跑）。

    每一块都有独立开关，关掉就整个不存在（不装配、不起任务、不占内存）：

    - 读信回信：`QQBOT_MAIL_REPLY`（还要配主人地址/QQ 号）
    - 每日汇报：`QQBOT_MAIL_REPORT`
    - 入群审批：`QQBOT_AUTO_APPROVE_JOIN`
    - **WebUI 面板：`QQBOT_WEBUI`**（2026-10-01 用户："面板属于 stage4 内容，本质插件"）

    `loop` 只有面板用得上：它的 HTTP 服务跑在线程里，而动作执行（`execute_action`）
    是协程，所以要有个跨线程投递的闭包。其余插件不需要它。
    """

    plugins: list[BackgroundPlugin] = []
    plugins.extend(_mail_plugins(engine))
    approval = _join_approval_plugin(call_action=call_action, notify=notify, roles=roles)
    if approval is not None:
        plugins.append(approval)
    panel = _web_panel_plugin(engine, call_action=call_action, loop=loop)
    if panel is not None:
        plugins.append(panel)
    return tuple(plugins)


def mail_workdir():
    """邮箱 CLI 的工作目录。和 `data/` 一起搬走就能接着跑。"""

    import os
    from pathlib import Path

    base = os.environ.get("QQBOT_DATA_DIR", "").strip()
    root = Path(base).resolve() if base else Path(__file__).resolve().parents[2] / "data"
    return root / "mail_outbox"


def _mail_plugins(engine) -> list[BackgroundPlugin]:
    from . import dev_config

    built: list[BackgroundPlugin] = []
    reporter = _build_daily_report(engine)
    if reporter is not None:
        built.append(DailyReportPlugin(reporter, engine, interval_seconds=REPORT_TICK_SECONDS))
    channel = _build_mail_channel(engine)
    if channel is not None:
        built.append(MailChannelPlugin(channel, interval_seconds=dev_config.MAIL_POLL_SECONDS))
    return built


# 每日汇报的检查节拍：它只做"到点了吗"，60 秒足够细（发送时刻精确到分钟）。
REPORT_TICK_SECONDS = 60.0


def _build_daily_report(engine):
    """装配每日汇报。**没装 CLI / 没开开关就返回 None**，对话照常。"""

    from . import dev_config

    if not dev_config.MAIL_REPORT_ENABLED:
        logger.info("每日汇报未启用（QQBOT_MAIL_REPORT=0）")
        return None
    from .daily_report import DailyReporter
    from .letter_writer import LetterWriter
    from .mail_client import MailClient
    from .mail_state import MailStateStore

    mail = MailClient(dev_config.MAIL_CLI, workdir=mail_workdir())
    store = MailStateStore()
    # 写信 agent 用**自己的 client 与 user_id**（2026-09-30 用户要求）：
    # 以前借用回复 agent 的 client，信与群聊共用一份缓存隔离空间。
    from .runtime import _build_letter_client

    writer = LetterWriter(_build_letter_client(), max_chars=dev_config.MAIL_REPORT_MAX_CHARS)
    reporter = DailyReporter(
        mail_client=mail,
        state_store=store,
        recipient=dev_config.MAIL_REPORT_TO,
        send_at=dev_config.MAIL_REPORT_AT,
        enabled=True,
        max_chars=dev_config.MAIL_REPORT_MAX_CHARS,
        max_tries=dev_config.MAIL_REPORT_MAX_TRIES,
        writer=writer,
    )
    engine.daily_reporter = reporter
    # 把她以前写出去的信灌回引擎（最近在前）：这样**重启之后**她照样记得自己写过什么、
    # 那些信是写给谁的。存储层是唯一来源，引擎只持有内存副本。
    try:
        state = store.load()
    except Exception:  # noqa: BLE001 - 读不到就当她还没写过信，对话照常
        logger.exception("mail_state_load_failed")
    else:
        for letter in reversed(state.letters):
            engine.note_letter(letter)
    return reporter


def _build_mail_channel(engine):
    """装配读信回信。关掉开关、没配主人地址就返回 None。"""

    from . import dev_config

    if not dev_config.MAIL_REPLY_ENABLED:
        logger.info("读信回信未启用（QQBOT_MAIL_REPLY=0）")
        return None
    if not (dev_config.MAIL_OWNER_FROM and dev_config.MAIL_OWNER_USER_ID):
        logger.warning("读信回信没配主人地址或 QQ 号，本次不启用")
        return None
    from .mail_channel import MailChannel
    from .mail_client import MailClient
    from .mail_state import MailStateStore

    # 邮件进来时走的是"私聊"这条路，主人那份用他自己的 QQ 号；陌生发件人由通道在
    # 收到信时**自己登记**进私聊白名单（策略留在 Stage 4 通道里，引擎的判定不动）。
    if (dev_config.MAIL_SENDERS.strip().casefold() in {"owner", "主人"}
            and dev_config.MAIL_OWNER_USER_ID not in dev_config.PRIVATE_DEBUG_USER_IDS):
        logger.warning(
            "读信回信：QQ %s 不在私聊白名单里，来信会被引擎静默丢弃（把 TA 加进 "
            "QQBOT_PRIVATE_DEBUG_USER_IDS 或超管名单）",
            dev_config.MAIL_OWNER_USER_ID,
        )
    return MailChannel(
        mail_client=MailClient(dev_config.MAIL_CLI, workdir=mail_workdir()),
        state_store=MailStateStore(),
        engine=engine,
        owner_from=dev_config.MAIL_OWNER_FROM,
        owner_user_id=dev_config.MAIL_OWNER_USER_ID,
        enabled=True,
        max_replies_per_day=dev_config.MAIL_MAX_REPLIES_PER_DAY,
        self_from=dev_config.MAIL_SELF_FROM,
        max_age_hours=dev_config.MAIL_MAX_AGE_HOURS,
        senders=dev_config.MAIL_SENDERS,
        max_replies_per_sender=dev_config.MAIL_MAX_REPLIES_PER_SENDER,
    )


def _join_approval_plugin(*, call_action, notify, roles):
    """装配入群审批：策略（配置）＋轮询器＋节拍。"""

    from . import dev_config

    if not dev_config.AUTO_APPROVE_JOIN:
        logger.info("入群申请自动审批未启用（QQBOT_AUTO_APPROVE_JOIN=0）")
        return None
    if roles is None:
        # **没有角色来源就不装这个插件**（fail-closed 并且**出声**）：
        # 拿不到"她在那个群是什么角色"时，审批会一条都不处理——以前这是静默的，
        # 真机上表现为"审批好像没开"，查起来很难（2026-09-30 踩过）。
        logger.warning("入群申请自动审批没有角色查询能力（engine.self_roles 为空），本次不启用")
        return None
    from .join_approval import JoinApprovalPolicy, JoinApprovalPoller

    policy = JoinApprovalPolicy(
        whitelist=dev_config.APPROVE_WHITELIST,
        blacklist=dev_config.APPROVE_BLACKLIST,
        pattern=dev_config.APPROVE_PATTERN,
        reject_reason=dev_config.APPROVE_REJECT_REASON,
    )
    poller = JoinApprovalPoller(
        call_action=call_action,
        notify=notify,
        policy=policy,
        roles=roles,
        enabled=True,
        max_per_tick=dev_config.APPROVE_MAX_PER_TICK,
    )
    logger.info("入群申请自动审批已就绪：每 %s 秒看一次；判据 %s",
                int(dev_config.APPROVE_POLL_SECONDS), policy.explain)
    return JoinApprovalPlugin(poller, interval_seconds=dev_config.APPROVE_POLL_SECONDS)


# --- WebUI 面板（Stage 4 后台插件） -----------------------------------------
#
# 装配点在这里，**实现细节在 `webui_panel.py` / `webui_access.py` / `webui_data.py`**：
# 与读信回信、入群审批一个分工（策略与实现在各自模块，插件只负责"怎么接上"）。
#
# 注入给面板的是**一串闭包**，不是一个引擎对象——它因此没法绕开权限、护栏、审计：
#   state_reader / execute_action / call_action / apply_overrides / memory_ops
#   / prompt_library / knowledge / control_audit / self_id
# 这些全部由核心提供（`runtime` 在这一层之后会补齐），面板自己只有 HTTP 与 HTML。


def _web_panel_plugin(engine, *, call_action=None, loop=None):
    """按配置装配面板。没开开关 / 没凭据 / 起不来 → 返回 None（对话照常）。"""

    from . import dev_config

    if not dev_config.WEBUI_ENABLED:
        logger.info("WebUI 面板未启用（QQBOT_WEBUI=0）")
        return None
    if os.environ.get("QQBOT_STATE_PERSIST", "").strip().lower() in {"0", "false", "off", "no"}:
        # 测试 / 干跑（不落盘运行状态）时**不装配面板**：面板会写自己的令牌与口令文件，
        # 那属于部署级产物；离线测试跑一遍不该在 `data/` 里留下它们。
        # 需要面板的测试自己直接构造 `Panel` / `WebPanelPlugin`（见 test_webui_panel.py）。
        logger.info("WebUI 面板未启用（本次运行不落盘状态：测试或干跑）")
        return None
    from .webui_access import LOCAL, load_or_create_token
    from .webui_panel import Panel, PanelServer, WebPanelPlugin, install_async_runner

    mode = dev_config.WEBUI_ACCESS_MODE
    token = ""
    if mode == LOCAL:
        token = dev_config.WEBUI_TOKEN or load_or_create_token()
    access = _build_access(mode, token)
    if not access.configured:
        # **fail-closed 并且出声**：没凭据就不装配，不出现"面板开着但谁都能进"。
        logger.warning(
            "WebUI 面板没有可用凭据（local 需要 token / remote 需要口令），本次不启用"
        )
        return None

    if loop is not None:
        import asyncio

        install_async_runner(lambda coro: asyncio.run_coroutine_threadsafe(coro, loop).result(5.0))

    seams = _panel_seams(engine, call_action)
    panel = Panel(seams, access)
    server = PanelServer(panel, host=dev_config.WEBUI_HOST, port=dev_config.WEBUI_PORT)
    seams["restart_gate"] = _restart_gate
    if access.remote:
        logger.warning(
            "面板以**远程模式**启动：监听 %s:%s，允许来源 %s。"
            "它等于这台机器上 bot 的控制面（能改 prompt、换 key、踢人），"
            "请确认前面有反向代理或隧道做 TLS 与访问控制。",
            dev_config.WEBUI_HOST, dev_config.WEBUI_PORT,
            "、".join(dev_config.WEBUI_ALLOWED_ORIGINS) or "（未配置）",
        )
    logger.info("WebUI 面板已装配：模式=%s 监听=%s:%s", mode,
                dev_config.WEBUI_HOST, dev_config.WEBUI_PORT)
    return WebPanelPlugin(server)


#: 面板重启的冷却：这么短时间里重复点，第二次直接拒。
#:
#: 由来（2026-10-01 现场抓到）：面板文档与计划里都写了"10 秒内重复 → 429"，
#: 但**当时根本没接这个闸**——连点两次真的会重启两次（实测第二次也是 202）。
#: 模块级状态而不是实例属性：它只是个时间戳，不需要跟插件生命周期绑定。
RESTART_COOLDOWN_SECONDS = 10.0
_LAST_RESTART = [0.0]


def _restart_gate() -> bool:
    """现在允许重启吗。允许时**记下这次时刻**（调用即占用）。"""

    now = time.time()
    if now - _LAST_RESTART[0] < RESTART_COOLDOWN_SECONDS:
        logger.warning("webui_restart_ignored age=%.1fs", now - _LAST_RESTART[0])
        return False
    _LAST_RESTART[0] = now
    return True


def _build_access(mode: str, token: str):
    """按模式造接入层。remote 的口令哈希存 `data/`（0600），不进 `.env`。"""

    from . import dev_config
    from .webui_access import LOCAL, REMOTE, WebAccess

    if mode != REMOTE:
        return WebAccess(mode=LOCAL, token=token)
    stored = _load_password_hash()
    if not stored and dev_config.WEBUI_PASSWORD:
        from .webui_access import hash_password

        stored = hash_password(dev_config.WEBUI_PASSWORD)
        _save_password_hash(stored)
    return WebAccess(mode=REMOTE, password_hash=stored,
                     allowed_origins=dev_config.WEBUI_ALLOWED_ORIGINS,
                     behind_proxy=dev_config.WEBUI_BEHIND_PROXY)


def _password_file():
    from pathlib import Path

    base = os.environ.get("QQBOT_DATA_DIR", "").strip()
    root = Path(base) if base else Path(__file__).resolve().parents[2] / "data"
    return root / "webui_password"


def _load_password_hash() -> str:
    try:
        return _password_file().read_text(encoding="utf-8").strip()
    except (FileNotFoundError, OSError):
        return ""


def _save_password_hash(value: str) -> None:
    path = _password_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value + "\n", encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:  # pragma: no cover - 平台差异
            pass
        logger.info("面板登录口令已设置（只存哈希）：%s", path)
    except OSError as exc:
        logger.warning("webui_password_write_failed category=%s", type(exc).__name__)


def _panel_seams(engine, call_action) -> dict[str, object]:
    """面板能用的全部接缝。缺哪一项，面板就对那一块显示"没启用"。"""

    from . import operator_config, prompt_library, runtime_flags

    seams: dict[str, object] = {
        "engine": engine,
        "operator_config": operator_config.shared(),
        "prompt_library": prompt_library.shared(),
        "control_audit": getattr(engine, "control_audit", None),
        "call_action": call_action,
    }
    if seams["control_audit"] is None:
        from .control_audit import ControlAudit

        seams["control_audit"] = ControlAudit()
    if engine is not None:
        # 用 `getattr` 取而不是直接点属性：测试里那些"像引擎"的替身没有这些方法，
        # 装配阶段不能因为"替身不够真"就整条装配挂掉（面板装不成，对话照跑）。
        from .runtime import apply_overrides as _apply

        action = getattr(engine, "execute_action", None)
        if action is not None:
            seams["execute_action"] = action
        seams["apply_overrides"] = _apply
        # **延后取**：`memory_ops` 是 `serve` 起了记忆服务之后才挂到引擎上的，
        # 这一行比它早（装配点在 `serve` 里记忆之前）。直接取值会拿到 None，
        # 面板的记忆按钮就永远 503（2026-10-01 现场踩到）。
        from .webui_panel import _Lazy

        seams["memory_ops"] = _Lazy(lambda: getattr(engine, "memory_ops", None))
        flags = runtime_flags.shared()
        if flags is not None:
            seams["flags"] = flags.snapshot
    knowledge = getattr(engine, "prompt_sources", None) if engine is not None else None
    knowledge_base = getattr(knowledge, "knowledge_base", None)
    if knowledge_base is not None:
        seams["knowledge"] = knowledge_base
    roles = getattr(engine, "self_roles", None) if engine is not None else None
    if roles is not None:
        async def _self_id(_roles=roles):
            result = _roles.self_id()
            if asyncio.iscoroutine(result):
                return await result
            return result

        seams["self_id"] = _self_id
    seams.setdefault("data_root", os.environ.get("QQBOT_DATA_DIR", "").strip())
    return seams