"""控制面板的装配。

从 `background_plugins.py` 原样搬来（只改 import 路径与函数名）。
注入给面板的是**一串闭包**，不是一个引擎对象——它因此没法绕开权限、护栏、审计。
"""
from __future__ import annotations

import asyncio
import logging
import os
import time

logger = logging.getLogger(__name__)


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

    from ... import dev_config
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
    """远程口令哈希的落点。路径算法只此一处（`dev_config.data_dir`）。"""

    from ...dev_config import data_dir

    return data_dir() / "webui_password"


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


def _panel_seams(ui, call_action) -> dict[str, object]:
    """面板能用的全部接缝。缺哪一项，面板就对那一块显示"没启用"。

    `ui` 是**窄接缝**（`plugins.UiSeams`），不是引擎——面板拿不到 `transport`、
    拿不到 `engine`、拿不到任何权限名单。它要用什么，`UiSeams` 里就列什么。
    """

    from ... import operator_config, prompt_library, runtime_flags

    seams: dict[str, object] = {
        "operator_config": operator_config.shared(),
        "prompt_library": prompt_library.shared(),
        "call_action": call_action,
        # 插件清单（**只读**）：面板"插件"卡拿它列 tab。缺了就显示"没启用"。
        "plugins": getattr(ui, "plugins", None),
    }
    audit = getattr(ui, "control_audit", None)
    if audit is None:
        from ...control_audit import ControlAudit

        audit = ControlAudit()
    seams["control_audit"] = audit

    if callable(getattr(ui, "execute_action", None)):
        seams["execute_action"] = ui.execute_action
    if callable(getattr(ui, "apply_overrides", None)):
        # 面板只管给参数；引擎由核心侧的闭包提前绑好（`runtime._ui_seams_for`）。
        seams["apply_overrides"] = ui.apply_overrides

    # **延后取**：`memory_ops` 是 `serve` 起了记忆服务之后才挂到引擎上的，
    # 这一行比它早（装配点在 `serve` 里记忆之前）。直接取值会拿到 None，
    # 面板的记忆按钮就永远 503（2026-10-01 现场踩到）。
    from .webui_panel import _Lazy

    if callable(getattr(ui, "memory_ops", None)):
        seams["memory_ops"] = _Lazy(ui.memory_ops)

    flags = runtime_flags.shared()
    if flags is not None:
        seams["flags"] = flags.snapshot

    # **活快照**：bot 在跑就用内存里的口径，取不到退回状态文件。
    # 给的是**取值闭包**，不是引擎——面板不该有能力顺着接缝摸到核心对象。
    if callable(getattr(ui, "state_reader", None)):
        seams["state_reader"] = _Lazy(ui.state_reader)

    if callable(getattr(ui, "restart", None)):
        seams["restart"] = ui.restart
    if callable(getattr(ui, "group_switch", None)):
        seams["group_switch"] = ui.group_switch
    if callable(getattr(ui, "session_clear", None)):
        seams["session_clear"] = ui.session_clear

    if callable(getattr(ui, "self_id", None)):
        async def _self_id(_get=ui.self_id):
            result = _get()
            if asyncio.iscoroutine(result):
                return await result
            return result

        seams["self_id"] = _self_id

    knowledge = getattr(ui, "knowledge", None)
    if knowledge is not None:
        seams["knowledge"] = knowledge
    # **她学来的东西**（金句 / 黑话，2026-10-06）：面板里「知识库 → 金句 / 黑话」两个
    # 子项要能看能改这两份数据。给的是核心的 `LearnedSeams`——**一串函数**，里面
    # 只有那七个口（读/改表情方向、停用笔记、读/改/删/标错词条），
    # 没有记忆入口、也没有对话入口（`tests/test_learned_seams.py` 逐个钉住函数清单）。
    #
    # **不加 `_Lazy`**：`LearnedSeams` 自己的每个函数在**调用时**才去引擎上取 store
    # （见 `runtime._SeamBinder.learned_quote_store`），所以这里存下来的那一刻是不是
    # "金句 store 已经装上了"不影响它——加了反而会多一层假延迟。
    learned = getattr(ui, "learned", None)
    if learned is not None:
        seams["learned"] = learned
    # 数据根目录：**算好再给**（面板不自己猜路径）。以前这里只传 `QQBOT_DATA_DIR`
    # 环境变量，没配就是空串，`Panel.root` 返回 None，面板于是自己回落到
    # `webui_data._data_root()`——那个函数搬过家之后指到了 `src/data`。
    from ...dev_config import data_dir

    seams["data_root"] = str(data_dir())
    return seams


# --- 装配：按配置决定装不装 ---------------------------------------------------

def build_web_panel(registry):
    """按配置装配面板。没开开关 / 没凭据 / 不落盘 → 返回 None（对话照常）。

    `registry` 是插件注册表：事件循环与窄接缝都由它取——
    `build_engine` 是同步的，那时候还没有运行中的循环，
    `serve` 起来之后才补上（`registry.set_loop`）。所以这里**不立刻**装异步执行器，
    而是给一个每次现取循环的闭包。

    **它不要引擎**：面板要用什么，`registry.ui` 里就列什么。
    """

    from ... import dev_config

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

    # 跨线程把协程丢回主循环。循环**现取**：装配时它可能还没跑起来（见 docstring）。
    def _runner(coro):
        active = registry.loop
        if active is None:
            # 还没有循环（干跑 / 单测直接调）：自己跑完，别把请求挂在那里。
            asyncio.run(coro)
            return None
        return asyncio.run_coroutine_threadsafe(coro, active).result(5.0)

    install_async_runner(_runner)

    seams = _panel_seams(registry.ui, registry.call_action)
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
