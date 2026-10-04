"""与通道无关的运行装配：把传输层、引擎、记忆服务装起来并跑主循环。

**为什么单独成模块**：Stage 3 用反向 WebSocket（本机监听，NapCat 连进来），
Stage 4 计划用 WebSocket 客户端（主动连 SnowLuma 的 wsServer）。两者**只有
"用哪个传输层"不同**——状态恢复、记忆启动、上下文补全、出站节奏、优雅退出
全都一样。

如果不把这段抽出来，Stage 4 只能复制一份主循环，之后 Stage 3 的每个改动都得
手动同步两遍。这个模块的存在就是为了让"怎么跑"只有一份。

依赖方向是单向的：本模块依赖 `stage3_main`（引擎与命令表都在那里），
而 `stage3_main` 只在 `main()` 内部延迟导入本模块以打破循环。
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

from . import dev_config
from . import operator_config
from . import prompt_library
from . import runtime_flags
from ._host_adapters import FileAuditSink, LocalMachineProbe, build_host_services
from .api_usage import ApiUsageStore
from .background_plugins import (
    build_background_plugins,
    plugin_enabled,
    run_background_plugin,
)
from .capabilities import CapabilityRegistry
from .conversation_context import ConversationContextProvider
from .extensions import PromptSources
from .llm_client import OpenAICompatibleClient, apply_client_overrides
from .memory_config import MemorySettings
from .memory_ops import MemoryOps
from .memory_service import MemoryService
from .onebot_client import SnowLumaHttpClient
from .outbox import Outbox
from .state_store import RuntimeStateStore
from .style_reviewer import StyleReviewer
from .stage3_main import (
    BATCH_SIZE,
    COOLDOWN_SECONDS,
    CONTEXT_HISTORY_COUNT,
    CONTEXT_REFRESH_INTERVAL_SECONDS,
    CONTEXT_TIMEOUT_SECONDS,
    OneBotRelayTargetResolver,
    DialogueEngine,
    _as_outgoing_list,
    _deliver_relay,
    _deliver_reply,
    _send_or_queue,
    _set_public_help,
    # "这条话长得像特权命令吗"——只认前缀形状，不要求能解析出来。
    # 渠道注入的话要用它拒投（见 `_SeamBinder.deliver`）。
    privileged_command_level,
)
from .plugins import (
    ChatSeams,
    PluginRegistry,
    ReportSeams,
    UiSeams,
    attach_plugins,
    inventory,
)
from .transport import IncomingMessage, MessageTarget, QQTransport

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def build_context_provider() -> ConversationContextProvider | None:
    """构造对话背景 provider；未配置 SnowLuma 地址时返回 None。

    走只读 HTTP 接口拿群消息历史与成员名单，并经过能力闸门的 read 白名单校验。
    """

    if not dev_config.SNOWLUMA_HTTP_BASE_URL:
        logger.info("对话背景补全未启用：未配置 SnowLuma 地址")
        return None
    http_client = SnowLumaHttpClient(
        dev_config.SNOWLUMA_HTTP_BASE_URL, dev_config.SNOWLUMA_ACCESS_TOKEN
    )
    return ConversationContextProvider(
        http_client,
        registry=CapabilityRegistry(),
        history_count=CONTEXT_HISTORY_COUNT,
        timeout=CONTEXT_TIMEOUT_SECONDS,
        refresh_interval=CONTEXT_REFRESH_INTERVAL_SECONDS,
    )


def state_persistence_enabled() -> bool:
    """是否启用运行状态持久化（默认启用）。

    由 `QQBOT_STATE_PERSIST=0/false/off/no` 显式关闭。离线测试入口
    (`tests/run_offline.py`) 会设置这个变量，避免测试运行覆盖真实的
    `data/runtime_state.json`；日常运行不需要做任何配置。
    """

    explicit = os.environ.get("QQBOT_STATE_PERSIST", "").strip().lower()
    return explicit not in {"0", "false", "off", "no"}


def build_knowledge_base():
    """知识库：索引存在就用，不存在就静默关掉（迁移友好）。

    索引是离线构建的（`data/knowledge_tool.py build`），运行时不读语料、不建索引——
    启动路径上不该有 1 MB 文本的解析。删掉索引文件即等于关掉知识库。
    """

    configured = os.environ.get("QQBOT_KNOWLEDGE_INDEX", "")
    base = Path(os.environ.get("QQBOT_DATA_DIR") or (_PROJECT_ROOT / "data"))
    path = Path(configured) if configured else base / "knowledge" / "knowledge.sqlite3"
    if os.environ.get("QQBOT_KNOWLEDGE", "1").lower() in {"0", "false", "off"}:
        return None
    if not path.exists():
        logger.info("知识库未启用：索引不存在（%s）", path)
        return None
    from .knowledge_base import KnowledgeIndex
    from .knowledge_operator import OperatorChunksStore, OperatorKnowledge

    logger.info("知识库已就绪：%s", path)
    # **必须包一层**：引擎那个端口是 `async def search`，而 `KnowledgeIndex.search` 是同步的。
    # 直接返回索引对象时，`await` 一个 list 会抛 TypeError 并被上层吞掉——知识库会静默失效
    # （2026-09-28 之前一直如此，日志里 4 次 `knowledge lookup failed`）。
    #
    # 2026-10-01 再加一层 `OperatorKnowledge`：面板能改的那部分（**面板块**）叠在只读语料
    # 之上。这是"未来插件插得进来"的接口——`knowledge_base.py` 的检索与切块逻辑一行没动。
    return OperatorKnowledge(
        KnowledgeIndex(path),
        OperatorChunksStore(path.parent),
    )


# --- 面板接缝：配置 / prompt / 记忆 / 审计 -----------------------------------
#
# 面板（Stage 4 插件）不能自己碰引擎，只能调这里装配出来的那几个闭包。
# 这一段就是"核心给插件的东西"的落点：权限、动作执行、审计都在核心里。


def _build_client_overrides(engine, operator) -> dict[str, str]:
    """把覆盖层里的凭据类配置列出来（**不回显值**，只给"改过没有"）。"""

    stored = getattr(operator, "values", {}) or {}
    return {key: ("已设置" if stored.get(key) else "未设置")
            for key, spec in operator_config.SETTINGS.items() if spec["kind"] == "secret"}


def apply_overrides(engine, payload: dict[str, object], *,
                    store=None, audit=None,
                    source: str = "", token: str = "") -> dict[str, dict[str, str]]:
    """面板改配置的**唯一入口**：校验 → 落盘 → 立刻生效能生效的。

    返回 `{键: {"applied": "live"|"restart_required"|"rejected", "detail": …}}`。
    逐键独立：某一项不合法不影响其它项（面板上是一张表单，不该整张失败）。

    几点刻意的取舍：

    - **`provider` 只翻译成地址**，不写进 `os.environ`：它是面板的聚合字段，
      环境里真正认的是 `QQBOT_API_BASE_URL`。面板要是同时给了 `api_base_url`，
      以**显式地址**为准（不猜操作者更想要哪个）。
    - **空值 = 回落**：留空的 key 类配置会被写进覆盖层，含义是"清掉这一项"，
      而不是"把空 key 设上去"。这与 `agent_api_key` 的回落语义一致。
    - **重启项如实标注**：`restart_required` 就是不生效，面板必须照实显示。
    """

    config = store if store is not None else operator_config.shared()
    applied: dict[str, dict[str, str]] = {}
    chosen_provider = str(payload.get("provider", "") or "").strip().casefold()
    explicit_url = str(payload.get("api_base_url", "") or "").strip()
    for key in list(payload):
        spec = operator_config.SETTINGS.get(str(key))
        if spec is None:
            applied[str(key)] = {"applied": "rejected", "detail": "不认识的配置项"}
            continue
        raw = payload[key]
        try:
            value = operator_config.normalize(key, raw)
        except operator_config.ConfigRejected as exc:
            applied[str(key)] = {"applied": "rejected", "detail": str(exc)}
            continue
        config.values[str(key)] = value
        effect = _apply_live(engine, str(key), value, config=config)
        if effect is None:
            applied[str(key)] = {"applied": "restart_required",
                                 "detail": "已保存，重启后生效"}
        elif effect is False:
            applied[str(key)] = {"applied": "rejected",
                                 "detail": "这个值没法立刻生效（看模式或地址）"}
            config.values.pop(str(key), None)
        else:
            applied[str(key)] = {"applied": "live", "detail": "已生效"}
    # 供应商 → 地址（放在循环之后，让显式地址赢）
    if "provider" in payload and not explicit_url:
        # 本地 import：`provider_registry` 是面板的接入面，**不是底层的必需品**。
        # 放模块顶部就等于"删掉它核心起不来"，那正是不该有的拴缚。
        from .provider_registry import base_url_of, known as provider_known

        if provider_known(chosen_provider) and chosen_provider != "custom":
            url = base_url_of(chosen_provider)
            config.values["api_base_url"] = url
            apply_client_overrides(base_url=url)
            applied["provider"] = {"applied": "live", "detail": f"地址已切到 {url}"}
        elif chosen_provider == "custom":
            applied["provider"] = {"applied": "live",
                                   "detail": "自定义地址：请同时填 api_base_url"}
        else:
            applied["provider"] = {"applied": "rejected", "detail": "不认识的供应商"}
    config.save()
    if audit is not None:
        audit.record("settings", detail={k: v for k, v in payload.items()},
                     result=";".join(f"{k}={v['applied']}" for k, v in applied.items()),
                     source=source, token=token)
    return applied


def _apply_live(engine, key: str, value: str, *, config=None) -> bool | None:
    """让一项配置立刻生效。返回 True/False（成/败）或 None（要重启）。

    `config` 是覆盖层对象：清空某项凭据时要连文件里那条一起抹掉，所以得能改它。
    """

    flags = runtime_flags.shared()
    truthy = str(value).strip().casefold() in {"1", "true", "yes", "on"}
    if key in runtime_flags.FLAG_NOTES:
        if flags is None:
            return None
        flags.set(key, truthy)
        return True
    if key in {"api_key", "judge_api_key", "memory_api_key", "review_api_key"}:
        role = {"api_key": "dialogue", "judge_api_key": "judge",
                "memory_api_key": "memory", "review_api_key": "review"}[key]
        from . import dev_config as _cfg

        env = operator_config.env_name(key)
        if not value:
            # **空值 = 回落 `.env` 里那一把**，绝不是"把空 key 设上去"。
            #
            # 2026-10-01 现场事故（这一版之前的写法）：这里原来写的是
            # `os.environ[env] = ""`，而且紧跟着读 `_cfg.API_KEY` 当回落值——
            # 但 `_cfg.API_KEY` 是**这个进程启动时**从环境里读的常量，
            # 一旦环境里已经被写成空串，它自己就是空的。于是：
            #   环境里留下 `QQBOT_API_KEY=""` → `load_env_file()` 按"不覆盖已存在的键"
            #   看到它存在，就不再填 `.env` 那把真 key → 回复 agent 的 key 变成空串
            #   → 模型一律 401。
            # 而 `_factory_api_key` 读的也是环境变量，所以**之后新建的会话 client 也全空**。
            #
            # 正确做法（现在这样）：
            #   ① 把环境变量**删掉**（不是设空串）——`load_env_file` 里绑定的 `.env` 值
            #      本来就在 `dev_config` 常量里，直接拿它当回落；
            #   ② `is None` 判断算的是"这一项根本没有"，不是"它是不是空串"（原来写
            #      `if not fallback` 时，取值写成常量访问会直接 KeyError）。
            os.environ.pop(env, None)
            if config is not None:
                # 覆盖层里那一项也**一起清掉**：空值启动时本来就会被忽略（见
                # `OperatorConfig.load`），留在文件里只会让面板显示成"已覆盖"、
                # 让人以为配置生效了一层其实没有的东西。清掉之后它老实回落 `.env`。
                config.values.pop(key, None)
            if key == "api_key":
                fallback = os.environ.get("QQBOT_API_KEY", "") or _cfg.API_KEY
                if not fallback:
                    # 主 key 回落不到 = 谁都调不动，这个状态不该由面板造出来。
                    return False
                apply_client_overrides(api_key=fallback)
                return True
            main_key = os.environ.get("QQBOT_API_KEY", "") or _cfg.API_KEY
            if not main_key:
                return False
            apply_client_overrides(api_key=main_key, usage_role=role)
            return True
        # 只改那一类 client（判定 key 不该顺手把回复的也改了）。
        apply_client_overrides(api_key=value, usage_role=role)
        os.environ[env] = value
        if key == "api_key":
            apply_client_overrides(api_key=value)
        return True
    if key in {"api_model", "memory_model"}:
        model = str(value).strip()
        role = "memory" if key == "memory_model" else ""
        apply_client_overrides(model=model, usage_role=role)
        os.environ[operator_config.env_name(key)] = model
        return True
    if key == "api_base_url":
        url = str(value).strip()
        if not url:
            return False
        apply_client_overrides(base_url=url)
        os.environ[operator_config.env_name(key)] = url
        return True
    if key == "provider":
        # 由 `apply_overrides` 在循环之后统一处理（要与显式地址比优先级）。
        return True
    return None


def build_engine(transport: QQTransport, *, state_store=None) -> DialogueEngine:
    """按给定传输层组装引擎。传输层是这里唯一的自由度。"""

    # 面板改的那一份（凭据 / prompt / 开关 / 知识库）先就位：后面几处会读它。
    operator = operator_config.shared()
    prompts = prompt_library.shared()
    prompt_library.install(prompts)
    # 给六套 prompt 各留一份"内置原稿"版本：这样"改坏了"能回滚到改之前，
    # 而不只是"恢复内置默认"（两者不一样：后者丢掉了你自己改的那一版）。
    # 幂等——已经有覆盖或已经有历史的套，跳过。
    prompts.backfill_all()
    flags = runtime_flags.build_flags(operator)
    runtime_flags.install(flags)
    # 审计（可插：`_host.AuditSink`）。本地 import——没有它就不记审计，核心照样跑。
    #
    # 2026-10-02 修：原来只有"本地 import"、**没有兜住 ImportError**，所以这句
    # "核心照样跑"是假的——删掉 `control_audit` 之后 `build_engine` 直接抛
    # `ModuleNotFoundError`（外部审查第四轮实测，我在冻结的提交上复现）。现在真的降级：
    # 用 `_host.NoAudit`（**同一个协议的空实现**，`record` / `tail` 都在），
    # 而不是让审计变成 `None` 再在别处 `AttributeError`。
    # 这也是 `_host` 目前唯一一处"马上就用上"的协议。
    try:
        from .control_audit import ControlAudit

        audit = ControlAudit(enabled=state_persistence_enabled())
    except ImportError as exc:
        # 抓 `ImportError` 而不只是 `ModuleNotFoundError`：模块在、但它自己的依赖
        # 缺失（或 `sys.modules` 里被置 None）时同样是"这次没有审计能力"。
        from ._host import NoAudit

        logger.warning("审计模块不可用（%s），本次不记审计流水", type(exc).__name__)
        audit = NoAudit()
    # 跨重启的累计账本（2026-09-30）：`/super apicheck` 与 `/super status` 默认看它，
    # 而不是"这次重启之后"。关掉状态持久化（测试/干跑）时它只活在内存里。
    usage_store = ApiUsageStore(enabled=state_persistence_enabled())
    global _USAGE_STORE
    _USAGE_STORE = usage_store
    client = OpenAICompatibleClient(
        dev_config.API_BASE_URL,
        dev_config.API_KEY,
        dev_config.API_MODEL,
        # 按 agent 隔离 KVCache 与调度（同一账号下生效，与 API Key 无关）。
        user_id=dev_config.DIALOGUE_USER_ID,
        usage_store=usage_store,
        usage_role="dialogue",
    )
    judge_client = _build_judge_client(usage_store)
    if judge_client is not None:
        logger.info("判定与回复分离：两个 agent 各用独立 user_id 隔离缓存")
    style_reviewer = _build_style_reviewer(usage_store)
    vision = _build_vision(usage_store)
    # **prompt 扩展的汇聚口**。这里先只放知识库；插件是装配之后才发现的，
    # 所以下面 `attach_plugins()` 之后会 `add_plugins(registry.shared_prompts())`。
    # 用**同一份可变列表**是有意的：不必为了"插件后到"再构造一次（2026-10-01 改）。
    prompt_sources = PromptSources(knowledge_base=build_knowledge_base())
    # 再按**会话**分一层：每个群/私聊有自己的缓存空间，切走再回来时前缀还在。
    # 实测：切走一小段再回来，那一次仍命中 7424 tokens（稳态中位数 7168）。
    engine = DialogueEngine(
        client,
        relay_target_resolver=OneBotRelayTargetResolver(transport),
        state_store=state_store,
        context_provider=build_context_provider(),
        judge_client=judge_client,
        client_factory=_dialogue_client_factory,
        judge_client_factory=_judge_client_factory if judge_client is not None else None,
        prompt_sources=prompt_sources,
        style_reviewer=style_reviewer,
        vision=vision,
        # 宿主能力：出站表现（分段与停顿）、帮助卡片、审计。
        # **实现都在 `_host_adapters` 里延迟 import**——核心模块顶部不再认识
        # `typing_sim` / `help_card` / `control_audit`，于是把它们删掉时核心仍然起得来。
        # 机器诊断要等引擎自己造出来（`engine.runtime_diagnostics`），所以在下面补。
        host=build_host_services(audit=FileAuditSink(audit)),
    )
    # 诊断命令的延迟回发通道：`/super processes cpu` 先回一句、采完 5 秒再单独发结果。
    # 命令回复不走拟人化节奏，也不进会话状态——它就是一条工具性回执。
    # 它**也走补发队列**：超管等的是那个结果，连接刚好断掉不该让它消失
    # （`engine.outbox` 由 `serve` 装上，闭包取的时候已经有了）。
    async def _send_diagnostics(target, text):
        status = await _send_or_queue(transport, getattr(engine, "outbox", None), target, text)
        if status != "sent":
            logger.warning("诊断回执没发出去（%s），已按补发处理", status)

    engine.async_sender = _send_diagnostics
    # 帮助卡片的发图接缝：只有传输层支持 `send_image` 时才接上（反向 WS 支持；
    # 测试里的假传输层没有这个方法，于是帮助自动退回文字——这正是我们要的降级）。
    async def _send_image(target, png: bytes) -> bool:
        sender = getattr(transport, "send_image", None)
        if not callable(sender):
            return False
        try:
            await sender(target, png)
        except Exception:  # noqa: BLE001 - 发图失败由调用方退回文字，这里只报告失败
            logger.warning("帮助卡片发送失败，退回文字", exc_info=True)
            return False
        return True

    engine.image_sender = _send_image
    # 账本挂到引擎上：`/super status` 与 `/super apicheck` 从它读全时累计。
    engine.usage_store = usage_store
    # 群管理要能直接调 action（`/super ban` 之类）。传输层照样是唯一变量：
    # 引擎只拿着它调 `call_api`，不碰任何 QQ 细节。
    engine.transport = transport
    # **不再由核心创建"她自己的角色查询"**（原来这里 `SelfRoleCache(transport, ...)`）。
    # 它是群管理与入群审批的前置能力，**删掉它 Stage 3 照样答话**——所以按判据它不是底层，
    # 归插件：`plugins/roles/` 以核心注入的 `call_action` 造它，群管理与入群审批
    # 用 `REQUIRES = ("roles",)` 声明依赖。核心这边留一个空位，插件装上了就填。
    #
    # **单一来源**（2026-10-04 对齐）：插件那一个实例经
    # `registry.provide_roles()` → `chat.roles_sink()`（`_SeamBinder.roles_sink`）
    # 落到这里，同时留在 `registry.shared_roles()` 给别的插件用。
    # 所以 `engine.self_roles is registry.shared_roles()` 恒真——"她在这个群里
    # 是不是群主"只有一个真相，不会出现核心查到一套、插件查到另一套。
    engine.self_roles = None
    # 面板要用的四样东西挂到引擎上（插件拿不到引擎，只拿得到装配点给的闭包）：
    # 覆盖层、prompt 库、记忆人工操作、操作审计。
    engine.operator_config = operator
    engine.prompt_library = prompts
    engine.memory_ops = MemoryOps(None, source="webui")
    engine.control_audit = audit
    # 记忆维护的**运行期总闸**（面板能关）：关掉只是不再批新记忆，检索照旧。
    engine.memory_enabled = flags.memory_enabled
    # 网卡流量要滚动窗口：后台每 30 秒采一次（`/super lan` 读这份样本）。
    diagnostics = engine.runtime_diagnostics
    if hasattr(diagnostics, "ensure_sampler"):
        diagnostics.ensure_sampler()
    # 机器诊断"也是宿主能力"——**这句注释原来写着"引擎只认 `host.machine.sample()`"，
    # 那是假的**（2026-10-02 外部审查第五轮三行代码证伪）：`DialogueEngine` 源码里
    # `host.machine` 出现 **0 次**，而 `self.runtime_diagnostics` 出现 9 次——
    # `/super processes|lan|fan` 读的是**后者**。
    #
    # 这批赋值现在只是"把诊断对象也挂到宿主接口上"，**没有消费者**
    # （见 `AGENTS.md` §3.3 那张表：`machine` 一处赋值、零处读取）。
    # 而且 `LocalMachineProbe.sample()` 已于同日改成**显式返回空样本**
    # （它原来是同步方法里调 async 取数，拿到的是协程对象）——所以即便有人接了
    # 它，也只会拿到空。要真用 `host.machine`，得先把 `sample` 改成 async。
    engine.host.machine = LocalMachineProbe(diagnostics)
    # **接插件**（发现只跑这一次，所以放在最后：引擎上该有的东西都已经就位）。
    # 命令插件进 `engine.commands`；后台插件留在 `engine.plugin_registry.backgrounds`
    # 给 `serve` 的节拍用。
    #
    # **插件拿不到引擎**（2026-10-01 修掉的越界）：引擎上有 `transport` 与三份权限名单，
    # 给出去就等于"插件能自己发消息、能读权限名单"。所以这里造两个**窄接缝对象**：
    # `ChatSeams`（对话四件事）与 `ReportSeams`（写日报五件事），
    # 它们内部持有引擎引用，但对插件只暴露**函数**。
    call_action, notify = _plugin_action_seams(engine, transport)
    registry = PluginRegistry(call_action=call_action, notify=notify,
                              roles=None, loop=None,
                              chat=_chat_seams_for(engine),
                              report=_report_seams_for(engine),
                              ui=_ui_seams_for(engine),
                              action_caller=_action_caller_for(engine, transport))
    attach_plugins(registry, engine.commands)
    engine.plugin_registry = registry
    # **prompt 扩展**（恋人 / 剧情 / 关系那类）在这里并进汇聚口。
    # 走 `PromptSources`：插件写的东西是不可信 DATA（过 sanitize + 长度上限 + 标来源），
    # **碰不到 system 前缀**——那正是"人格稳定"的地基。
    prompt_plugins = registry.shared_prompts()
    if prompt_plugins:
        prompt_sources.add_plugins(prompt_plugins)
        logger.info("prompt 扩展已接上：%s",
                    "、".join(getattr(p, "name", "?") for p in prompt_plugins))
    # 公开帮助要反映**这台机器实际装了什么**：插件可能带来新命令（群管理那几条）。
    _set_public_help(engine.commands)
    return engine


def _chat_seams_for(engine: DialogueEngine) -> ChatSeams:
    """把"对话那一侧"的四件事包成函数给插件。

    | 接缝 | 给出去的是什么 |
    | --- | --- |
    | `reserved_user_ids` | **只读**一份"不该被冒充的号"；插件拿不到可变集合，改不了名单 |
    | `allow_private` | 放行一个私聊号（邮件通道收信后要放行发件人） |
    | `deliver` | 收 `DeliveredMessage`（渠道 + 发件人 + 正文），**核心盖章成一条消息** |
    | `take_follow_ups` | 取走某个会话的续发段 |

    **不给** `engine` 本身，也不给 `transport` / `capabilities` / 任何名单的可写引用。
    换一套思维链路时，只要新的引擎能提供这四个函数，插件一行都不用改。

    ## 身份与特权命令由**这一侧**决定（2026-10-01 审查后修的）

    原来 `deliver` 直接吃一整条 `IncomingMessage`，于是**插件能自己填 `user_id`**。
    审查者实测：只拿 `registry.chat` 的插件把 `user_id` 填成超管的号，
    发 `/super addadmin @X`，**真的授了群管理员**——全程没碰 transport。

    现在：插件只交 `DeliveredMessage`，`user_id` / `sender_role` / `target` /
    `session_id` 都在**这里**算出来；并且**渠道注入的话永远拿不到特权命令**：
    正文里出现 `/super` 或 `/admin` 一律拒投（`privileged_command_level`，核心那个函数）。

    为什么必须在这里拒、而不是让每个插件自己记得拒：仓库里 `mail_channel` 原来就
    自己写了一道 `is_privileged_command`，注释还写着"SMTP 的 From 可以伪造"——
    **护栏写在插件里等于约定，不是边界**。现在它归核心，插件没有绕过的余地。
    """

    return _SeamBinder(engine).chat_seams()


#: 哪些渠道**允许**声明"这条来自主人"。白名单，不是黑名单。
#:
#: 目前只有邮件：`MAIL_OWNER_FROM` 是配置里写死的主人的发件地址，
#: 通道比对过发件地址才敢声明 `claims_owner`。别的渠道（以后新增的）默认**不在**这张表里，
#: 也就是它们无论如何声明都只是普通发件人——要么在这里加一条（核心改动，看得见），
#: 要么就没有主人权限。
_OWNER_CHANNELS: tuple[str, ...] = ("mail",)

#: 接缝用的"不透明令牌 → 引擎/传输层"私表。见 `_SeamBinder`。
_ENGINES: dict[str, "DialogueEngine"] = {}
_TRANSPORTS: dict[str, object] = {}


def _exact_text(value: object) -> str:
    """把一个不可信的值取成**精确的 `str`**（不是子类），用于"判一次、用一次"的闸门。

    ## 为什么 `str(x)` 不够（2026-10-01 第三轮审查实测打穿）

    CPython 的 `str(x)` 有三种行为：

    * `x` 是**精确 str** → 原样返回；
    * `x` 是 **str 子类** → 拷成精确 str（这一种是安全的）;
    * `x` 是**非 str 对象** → 返回 `type(x).__str__(x)` 的结果，而**那结果只要是
      str 实例（子类也算）就原样返回、不再拷**。

    于是这个形状能穿过 `str(...)`：

        class Flip(str):                      # 底层字串就是载荷
            def strip(self, *a):              # 第一次给闸门看无害内容
                return "/help" if first else self
        class Wrap:
            def __str__(self): return Flip("/super admin list")

        str(Wrap())  →  type(...) is str  ==  False   # 还是那个会变脸的子类

    闸门调 `strip()` 拿到 `/help` 放行，**同一个活对象**进了 `IncomingMessage`，
    引擎再调 `strip()` 就拿到载荷 → 实测执行了 `/super admin list` 并把名单读出来。

    ## 这里怎么取

    `str(value or "")` 之后，如果不是精确 str 就 `text[:]`——切片产出新的精确 str
    （`str.__getitem__` 不保留子类），返回的是底层真实字串。
    附带好处：`or ""` 顺便把 `None` / 空串归一了。

    ## ⚠️ 这一步是**类型契约**，别把它当成"某次攻击的修复"（2026-10-02 更正）

    这个 docstring 原来写的是上面那个 `Flip` / `Wrap` 形状"靠 `text[:]` 挡住"。
    **那是把推断写成了实测。** 外部审查第四轮做了突变实验（把 `text[:]` 拆掉、
    其余一字不动）：**1139 条测试没有一条因此变红**，两条号称"守它"的端到端测试
    也全绿（我自己用对照实验复核过：打/不打突变，红条数完全一样）。

    实测到的机制是这样的（本机 CPython 3.14.6）：

    * 闸门 `privileged_command_level` **自己**调了一次 `text.strip()`——那次调用
      拿到的是**载荷**，所以判成特权命令、**直接拒投**；
    * 而 CPython 3.14 的 `re.match` / `re.sub` **不会**去调 `str` 子类被覆盖的
      `strip`（量到调用数 0），所以"闸门看 `/help`、引擎看载荷"那个形状
      在当前解释器上**不可达**——闸门与引擎之间没有第二个 `.strip()` 消费者。

    所以准确的表述是：**在这台解释器上、对现有这两个形状，`text[:]` 不是承重的。**
    **不要**读成"它没用、可以删"：正则对子类的行为在别的 Python 版本上未必相同
    （审查者只有 3.14，明确声明无法验证更早版本），而且它承担的是一份**接口契约**——
    `_exact_text` 的调用方有权假设"拿到的就是精确 str"。

    那份契约现在由 `tests/test_mail_channel.py` 里的
    `test_exact_text_always_returns_a_plain_str` **直接钉住**（拆掉 `text[:]` 立刻红）。
    它断言的是接口形状，不依赖任何具体攻击能不能成功——后者会随解释器变化。
    """

    text = str(value or "")
    if type(text) is not str:  # noqa: E721 - **这里刻意用 type() 而不是 isinstance**：
        # `isinstance(text, str)` 对子类是 True，而子类正是要挡的东西。
        text = text[:]
    return text


class _SeamBinder:
    """给插件造接缝。**注意：它挡不住恶意插件，这一点必须说清。**

    ## 三次尝试，三次被审查者绕过（2026-10-01，都记下来）

    1. `PluginRegistry` 持有 `engine` → `registry.engine.transport` 直接可用。
    2. 改成"窄接缝"（闭包）→ `registry.report.snapshot.__self__`（绑定方法带 owner）
       与 `registry.notify.__closure__[1].cell_contents` 照样拿回引擎与活 transport。
    3. 改成"闭包只捕获不透明令牌"（就是下面这个 `_SeamBinder`）→ 审查者一步就到：
       接缝是 `self` 的**绑定方法**，所以 `registry.chat.deliver.__self__._require()`
       就是引擎；而且 `registry.chat.deliver.__globals__["_ENGINES"]`
       **连 token 都不用**，直接拿到整张 token→引擎 表。

    ## 结论：进程内隔离做不到，别再声称做到了

    只要插件代码与核心在同一个进程、能执行 Python，它就能读任何函数的
    `__globals__` / `__closure__` / `__self__`，或者 `import qq_roleplay_bot.runtime`、
    `gc.get_objects()`、`sys.modules`。**纯 Python 里没有能挡住这件事的写法。**

    所以这个类的定位是**架构与可审查性**，不是安全：

    * 它让"插件需要引擎"这件事在代码里显式（`_require` 一抛就是 bug）；
    * 它让 `grep engine` 在插件目录里查不到东西；
    * 它把"能力"写成一个一个具体的函数，接新思维链路时知道要在哪对齐。

    **真正的边界是：插件是受信任的代码。** 它们在同一个仓库里、走同一套代码审查。
    核心侧另有三道**不依赖任何隐藏**的检查，各有独立测试：身份由核心盖章、
    正文只取一次值（TOCTOU）、特权命令与动作执行在核心。
    """

    __slots__ = ("token",)

    def __init__(self, engine: "DialogueEngine", transport: object = None) -> None:
        import uuid

        self.token = uuid.uuid4().hex
        _ENGINES[self.token] = engine
        if transport is not None:
            _TRANSPORTS[self.token] = transport

    def _require(self):
        engine = _ENGINES.get(self.token)
        if engine is None:
            # 走到这里说明有人手工删了私表项，或者接缝被序列化到别的进程去了。
            # **明确抛错**，不静默返回 None——静默会让"没有能力"和"能力丢了"分不清。
            raise RuntimeError("plugin seam lost its engine binding")
        return engine

    def _require_transport(self):
        transport = _TRANSPORTS.get(self.token)
        if transport is None:
            raise RuntimeError("plugin seam lost its transport binding")
        return transport

    # --- 动作接缝（`call_action` / `notify`）------------------------------

    async def call_action(self, action: str, params: dict[str, object] | None = None,
                          *, purpose: str | None = None):
        """先过 `capabilities` 闸门，再调对面；插件绕不开闸门。

        闸门规则：

        - `purpose` 不明说时按 action 自己推（见下面那段"读用 read、写只放 join_approval"）；
        - `purpose` 明说时（核心执行某类动作，`action_caller` 就是这么用的），
          允许"**这一族写动作 + 任何只读动作**"：
          `is_allowed(action, purpose=purpose) or is_allowed(action, purpose="read")`。
          为什么要额外放行只读：`group_owner` 那族里有一步**回读**（设完头衔要现查一次，
          因为 QQ 会静默截断），而 `get_group_member_info` 属 `READ_ACTIONS`、
          不属 `GROUP_OWNER_ACTIONS`——只按写闸判会把回读一起拒掉（实测踩到）。
          反过来这仍然**比旧代码紧得多**：旧代码把活的 transport 交给插件，
          它能调**任意** action；现在最多是"它那一族写 + 只读"。

        闸门拒绝会抛 `plugins.ActionDenied`（**不是**核心的 `CapabilityDenied`）：
        插件不该认识 `capabilities.py`，所以这里翻译一次。
        """

        from .plugins import ActionDenied

        engine = self._require()
        transport = self._require_transport()
        registry = getattr(engine, "capabilities", None)
        if registry is not None:
            # 读用 `read`、写只放这一条 `join_approval`——**不能一律按写的那道闸判**：
            # `get_group_system_msg` 是只读 action，拿写入用途去查会被直接拒
            # （踩过：接缝第一版就是这么写的，审批每轮都读不到申请）。
            from .capabilities import JOIN_APPROVAL_ACTIONS, CapabilityDenied

            if purpose is None:
                purpose = "join_approval" if action in JOIN_APPROVAL_ACTIONS else "read"
            # 用 `check()` 而不是 `is_allowed()`：`check` 是既有契约（替身与真实实现都有），
            # 而且它同时挡住 `FORBIDDEN_ACTIONS`。只读那道闸作为**回退**再试一次。
            try:
                registry.check(action, purpose=purpose)
            except CapabilityDenied:
                try:
                    registry.check(action, purpose="read")
                except CapabilityDenied as exc:
                    raise ActionDenied(f"{action} 不允许用于 {purpose}") from exc
        call = getattr(transport, "call_api", None)
        if not callable(call):
            raise RuntimeError("这个通道不支持动作调用")
        from .onebot_client import unwrap_result

        return unwrap_result(await call(action, params or {}))

    def action_caller(self, purpose: str):
        """造一个**已绑好用途**的调用函数：`(action, params) -> response`。

        给核心执行"插件声明的动作"用（`stage3_main._plugin_action_reply`）。
        这样插件侧只需要一个 `call(action, params)`，拿不到 transport、也不需要
        认识 `capabilities`——闸门在这条闭包里，它绕不过去。
        """

        async def call(action: str, params: dict[str, object] | None = None):
            return await self.call_action(action, params, purpose=purpose)

        return call

    async def notify(self, text: str) -> None:
        """通知超管——走补发队列，连接断了也不会把这条提示弄丢。"""

        engine = self._require()
        transport = self._require_transport()
        targets = tuple(
            str(item) for item in getattr(dev_config, "APPROVE_NOTIFY_USER_IDS", ()) if str(item)
        )[:3]
        for user_id in targets:
            await _send_or_queue(
                transport, getattr(engine, "outbox", None), MessageTarget(user_id=user_id), text
            )

    # --- ChatSeams -------------------------------------------------------

    def reserved_user_ids(self) -> frozenset[str]:
        engine = self._require()
        merged = {str(item) for item in getattr(engine, "super_admin_user_ids", ()) or ()}
        merged |= {str(item) for item in getattr(engine, "admin_user_ids", ()) or ()}
        return frozenset(merged)

    async def allow_private(self, user_id: str) -> None:
        allowed = getattr(self._require(), "private_debug_user_ids", None)
        if isinstance(allowed, set):
            allowed.add(str(user_id))

    async def deliver(self, parcel) -> object:
        """把关卡全在这里过一遍，然后才交给对话流程。

        ## 四条必须遵守的规矩（都来自 2026-10-01 三轮审查）

        1. **正文取一次值，而且要取成"精确 str"**：`_exact_text(parcel.text)`，
           之后判闸门与投递都用这一个值。
           - 第一版是 `privileged_command_level(parcel.text)` 判完再把 `parcel.text`
             （**同一个活对象**）放进 `IncomingMessage`：`str` 子类的 `strip()` 第一次
             返回 `/help`、之后返回载荷 → 闸门看 `/help`、引擎执行 `/super admin list`。
           - 第二版改成 `str(parcel.text or "")`——**只修好一半**。第三轮审查实测：
             `str(x)` 对**非 str 对象**返回其 `__str__` 的结果，而那结果只要是 str 实例
             （**子类也算**）就**原样返回、不再拷**。于是一个 `__str__` 返回"会变脸的
             str 子类"的对象照样穿透。`_exact_text` 补上这一步。
        2. **身份不是插件说了算**：`sender` 只是"渠道说这是谁"。主人那一档由
           `_OWNER_CHANNELS` 白名单 + 渠道的 `claims_owner` 共同决定，而
           **`claims_owner` 是插件给的布尔、不可信**——所以它只影响 `sender_role`
           这个给模型看的标签，**不授予任何命令权限**（命令权限一律走 QQ 那条路，
           见 `stage3_main._is_super_admin_control`）。
        3. **每封必须有渠道内唯一的 id**：见 `DeliveredMessage.message_id`
           （原来核心自己拼了个常量，导致同一个发件人的后续来信被去重器静默丢掉）。
        4. **特权命令的形状判定要与引擎的解析口径一致**：`privileged_command_level`
           现在会先剥掉开头的 @提及/CQ 段（`admin_control` 一直在剥）。
           第三轮审查实测过不一致的后果：`"@x /admin relay group <群号> <内容>"`
           **闸门放行、引擎按管理员命令执行** —— 伪造主人的邮件能让她以超管身份
           往任意群发任意内容（`/admin relay` 两步可全自动）。
        """

        from .plugins import DeliveredMessage

        engine = self._require()
        if not isinstance(parcel, DeliveredMessage):
            return None
        # 1) **只取值一次，而且必须是精确 str**（TOCTOU 防护）。
        text = _exact_text(parcel.text)
        level = privileged_command_level(text)
        if level is not None:
            logger.warning("plugin_deliver_refused reason=privileged-command level=%s channel=%s",
                           level, parcel.channel)
            return None
        # 2) 身份与画像是**这一侧**算的。
        sender = _exact_text(parcel.sender)
        channel = _exact_text(parcel.channel)
        namespace = _exact_text(parcel.session_namespace) or channel
        is_owner = bool(parcel.claims_owner) and channel in _OWNER_CHANNELS
        # 3) 渠道内唯一的 id（渠道没给就退回"渠道+发件人"，至少不比以前差）。
        inner = _exact_text(parcel.message_id) or f"{namespace}:{sender}"
        message = IncomingMessage(
            message_id=f"{channel}:{inner}",
            session_id=f"{namespace}:{sender}",
            user_id=sender,
            text=text,
            target=MessageTarget(user_id=sender),
            sender_role="owner" if is_owner else "mailer",
            sender_name=_exact_text(parcel.sender_name) or (sender or "主人" if is_owner else sender),
        )
        return await engine.handle(message)

    def take_follow_ups(self, session_id: str):
        taker = getattr(self._require(), "take_follow_ups", None)
        return taker(session_id) if callable(taker) else []

    def roles_sink(self, cache: object) -> None:
        self._require().self_roles = cache

    def reporter_sink(self, reporter: object) -> None:
        self._require().daily_reporter = reporter

    def chat_seams(self) -> ChatSeams:
        return ChatSeams(reserved_user_ids=self.reserved_user_ids,
                         allow_private=self.allow_private,
                         deliver=self.deliver,
                         take_follow_ups=self.take_follow_ups,
                         roles_sink=self.roles_sink,
                         reporter_sink=self.reporter_sink,
                         owner_channels=_OWNER_CHANNELS)

    # --- ReportSeams -----------------------------------------------------

    def report_snapshot(self):
        taker = getattr(self._require(), "snapshot", None)
        return taker() if callable(taker) else None

    def report_log_io(self, feature, request, raw, *, session_id="", trigger=""):
        logger_fn = getattr(self._require(), "_log_model_io", None)
        if callable(logger_fn):
            logger_fn(feature, request, raw, session_id=session_id, trigger=trigger)

    def report_note_letter(self, letter) -> None:
        taker = getattr(self._require(), "note_letter", None)
        if callable(taker):
            taker(letter)

    def report_seams(self) -> ReportSeams:
        engine = self._require()
        return ReportSeams(snapshot=self.report_snapshot,
                           client=getattr(engine, "client", None),
                           log_model_io=self.report_log_io,
                           note_letter=self.report_note_letter,
                           memory_service=getattr(engine, "memory_service", None),
                           # **工厂**：写信 agent 自己那把通道（独立 user_id，
                           # 与群聊不共用缓存隔离空间）。给工厂而不是给 client，
                           # 因为装配点离写第一封信很远，client 要现造。
                           letter_client=_build_letter_client)

    # --- UiSeams ---------------------------------------------------------

    def ui_execute_action(self, request):
        return self._require().execute_action(request)

    def ui_restart(self):
        return self._require().request_restart()

    def ui_session_clear(self, session_id: str):
        return self._require().leave_session(session_id)

    def ui_group_switch(self, group_id: str, enabled: bool):
        engine = self._require()
        enable, disable = engine.enable_group, engine.disable_group
        return enable(group_id) if enabled else disable(group_id)

    def ui_memory_ops(self):
        return getattr(self._require(), "memory_ops", None)

    def ui_state_reader(self):
        engine = self._require()
        return {"snapshot": engine.snapshot(), "usage_store": getattr(engine, "usage_store", None)}

    def ui_self_id(self):
        roles = getattr(self._require(), "self_roles", None)
        return roles.self_id() if roles is not None else ""

    def ui_apply_overrides(self, body, **kwargs):
        from .runtime import apply_overrides as _apply_overrides

        return _apply_overrides(self._require(), body, **kwargs)

    def ui_seams(self) -> UiSeams:
        engine = self._require()
        groups = getattr(engine, "enable_group", None), getattr(engine, "disable_group", None)
        return UiSeams(
            control_audit=getattr(engine, "control_audit", None),
            execute_action=self.ui_execute_action,
            apply_overrides=self.ui_apply_overrides,
            memory_ops=self.ui_memory_ops,
            state_reader=self.ui_state_reader,
            restart=self.ui_restart,
            group_switch=self.ui_group_switch if all(callable(g) for g in groups) else None,
            session_clear=self.ui_session_clear,
            self_id=self.ui_self_id,
            knowledge=_knowledge_for(engine),
            # 插件清单（**只读**）：面板"插件"卡拿它列 tab。给的是函数 `inventory`，
            # 不是某个对象——接缝一律是函数，理由见 `UiSeams` 的说明。
            plugins=inventory,
        )


def _roles_sink_for(engine: DialogueEngine):
    """前置插件把"她自己是什么角色"的查询放回核心（`provide_roles` 用它）。"""

    def sink(cache: object) -> None:
        engine.self_roles = cache

    return sink


def _reporter_sink_for(engine: DialogueEngine):
    """插件把每日汇报器放回核心（`register_reporter` 用它，面板要读它判今天发没发）。"""

    def sink(reporter: object) -> None:
        engine.daily_reporter = reporter

    return sink


def _report_seams_for(engine: DialogueEngine) -> ReportSeams:
    """写日报要用、但**不属于权限**的五样东西（见 `ReportSeams` 的说明）。

    ⚠️ **每一样都必须经由 `_SeamBinder`，不能直接塞引擎的方法或属性。**
    2026-10-01 审查抓到两轮：

    1. `snapshot=getattr(engine, "snapshot", None)` 是**绑定方法**，
       `registry.report.snapshot.__self__` 就是引擎；
    2. 改成普通闭包之后，`registry.report.snapshot.__closure__[0].cell_contents`
       **照样**是引擎——闭包在 CPython 里是可读的。

    所以现在闭包只捕获**不透明令牌**（`_SeamBinder`），真身在模块私表里。
    详见 `_SeamBinder` 的说明（含"这不是安全边界"那条限度）。
    """

    return _SeamBinder(engine).report_seams()


def _ui_seams_for(engine: DialogueEngine) -> UiSeams:
    """控制面板要的东西。

    **面板权限最大，所以更不能拿引擎**：拿到就能顺着 `engine.transport` 发消息、
    顺着 `engine.super_admin_user_ids` 读名单，绕开它自己那套 token/CSRF 认证。
    这里逐项给**不受绑定方法与闭包泄漏影响**的接缝（`_SeamBinder`）。
    """

    return _SeamBinder(engine).ui_seams()


def _group_switch_for(engine: DialogueEngine):
    """面板的群开关：`(group_id, enabled) -> 是否真的变了`。"""

    enable = getattr(engine, "enable_group", None)
    disable = getattr(engine, "disable_group", None)
    if not (callable(enable) and callable(disable)):
        return None

    def _switch(group_id: str, enabled: bool) -> bool:
        return enable(group_id) if enabled else disable(group_id)

    return _switch


def _knowledge_for(engine: DialogueEngine):
    """面板的知识库面板块要的知识源（没有就是 None，面板显示"没启用"）。"""

    sources = getattr(engine, "prompt_sources", None)
    return getattr(sources, "knowledge_base", None)


def _factory_api_key(env_name: str, fallback: str) -> str:
    """工厂里取 key：**每次现读环境**，这样面板热更的 key 对新会话也生效。

    以前这里读 `dev_config.<常量>`（import 时求值一次），面板改了 key 之后
    新建的会话 client 还会拿到旧值——"主群换了、别的群还是旧的"。

    **空串要当成"没有设置"**：环境里一个空值会把 `.env` 的填充挡在门外
    （`load_env_file` 不覆盖已存在的键），于是"设成空"等于"把 key 弄没了"。
    2026-10-01 出过一次这样的事故，回复 agent 的 key 变空、模型全 401。
    """

    current = os.environ.get(env_name, "").strip()
    return current or fallback


def _dialogue_client_factory(session_id: str) -> OpenAICompatibleClient:
    return OpenAICompatibleClient(
        os.environ.get("QQBOT_API_BASE_URL", dev_config.API_BASE_URL),
        _factory_api_key("QQBOT_API_KEY", dev_config.API_KEY),
        os.environ.get("QQBOT_API_MODEL", dev_config.API_MODEL),
        user_id=dev_config.session_user_id(dev_config.DIALOGUE_USER_ID, session_id),
        usage_store=_USAGE_STORE,
        usage_role="dialogue",
    )


def _judge_client_factory(session_id: str) -> OpenAICompatibleClient:
    return OpenAICompatibleClient(
        os.environ.get("QQBOT_API_BASE_URL", dev_config.API_BASE_URL),
        _factory_api_key("QQBOT_JUDGE_API_KEY", dev_config.JUDGE_API_KEY),
        os.environ.get("QQBOT_JUDGE_MODEL", dev_config.API_MODEL),
        user_id=dev_config.session_user_id(dev_config.JUDGE_USER_ID, session_id),
        usage_store=_USAGE_STORE,
        usage_role="judge",
    )


def _build_judge_client(usage_store=None):
    """判定 agent 的 client；未启用双 agent 模式时返回 None（退回单 agent）。"""

    if not (dev_config.JUDGE_API_KEY and dev_config.DUAL_AGENT_ENABLED):
        return None
    return OpenAICompatibleClient(
        dev_config.API_BASE_URL,
        dev_config.JUDGE_API_KEY,
        os.environ.get("QQBOT_JUDGE_MODEL", dev_config.API_MODEL),
        user_id=dev_config.JUDGE_USER_ID,
        usage_store=usage_store if usage_store is not None else _USAGE_STORE,
        usage_role="judge",
    )


# 按会话新建的 client（`_dialogue_client_factory` 等）也要记账，但它们拿不到
# `build_engine` 的局部变量；所以这里放一个进程级引用，由 `build_engine` 填上。
# 只在同一个进程内使用，测试里因为关掉了持久化，它就是个纯内存账本。
_USAGE_STORE = ApiUsageStore(enabled=False)


def _build_style_reviewer(usage_store=None):
    """风格审核（可选 agent）；没开开关、也没有 key 时返回 None（回复直通）。

    默认**不开**：它给每条回复多加一次调用（实测 +0.6~0.9 秒、约 360 token）。
    开了之后每次改了什么都会记一行 INFO，方便回头核对它到底有没有用。
    """

    if not dev_config.REVIEW_ENABLED:
        logger.info("风格审核未启用（QQBOT_STYLE_REVIEW=0 / 未配 QQBOT_REVIEW_API_KEY）")
        return None
    key = dev_config.REVIEW_API_KEY or dev_config.API_KEY
    if not key:
        logger.warning("风格审核已开启但没有任何可用 key，本次不启用")
        return None
    logger.info("风格审核已启用：每条回复发送前过一道窄职责校对（会多一次模型调用）")
    return StyleReviewer(
        OpenAICompatibleClient(
            dev_config.API_BASE_URL,
            key,
            os.environ.get("QQBOT_REVIEW_MODEL", dev_config.API_MODEL),
            user_id=dev_config.REVIEW_USER_ID,
            usage_store=usage_store if usage_store is not None else _USAGE_STORE,
            usage_role="review",
        )
    )


def _build_vision(usage_store=None):
    """识图（多模态）。没开开关、也没有 key 时返回 None（有图的消息照旧只留占位符）。

    独立 client 与 user_id（默认 `qqbot-vision`），而且**不分群**：识图每次带的图都不同，
    前缀注定命中不了缓存，按群拆只会把一份缓存拆散（用户 2026-09-30 的要求）。
    模型默认 `deepseek-flash`——实测它能看图，不给图时也不会编。
    """

    if not dev_config.VISION_ENABLED:
        logger.info("识图未启用（QQBOT_VISION=0）")
        return None
    key = dev_config.VISION_API_KEY or dev_config.API_KEY
    if not key:
        logger.warning("识图已开启但没有任何可用 key，本次不启用")
        return None
    logger.info("识图已启用：model=%s user_id=%s（有图的消息在判定前先看一眼）",
                dev_config.VISION_MODEL, dev_config.VISION_USER_ID)
    # 本地 import：识图是**可插能力**，不是底层必需品（删掉它，她只会说"看不到图"）。
    #
    # 2026-10-02 修：这里原来是裸 import。**这一处**其实还排不上先炸——真正的
    # 第一处炸点在 `prompt_library.builtin("vision")`（`build_engine` 里的
    # `prompts.backfill_all()` 会遍历到它），见那个文件里的说明。两处都兜住。
    try:
        from .vision import ImageDescriber
    except ImportError as exc:
        logger.warning("识图模块不可用（%s），有图的消息只留占位符", type(exc).__name__)
        return None

    return ImageDescriber(
        OpenAICompatibleClient(
            dev_config.API_BASE_URL,
            key,
            dev_config.VISION_MODEL,
            user_id=dev_config.VISION_USER_ID,
            usage_store=usage_store if usage_store is not None else _USAGE_STORE,
            usage_role="vision",
        )
    )


def _build_letter_client() -> OpenAICompatibleClient:
    """写信 agent 的 client：独立 user_id（默认 `qqbot-letter`）。

    为什么单独一份：信的 prompt 前缀又大又恒定（人设 + 信件规矩 + 已确定事实），
    跟群聊共用一份缓存隔离空间只会互相挤；分开之后它的命中率与成本都能单独看。
    `QQBOT_MAIL_API_KEY` 不配就回落主 key。

    留在核心（而不是跟着装配搬去 `background_plugins.py`）：它要往**核心的用量账本**
    （`_USAGE_STORE`）里记一笔，那是核心状态。
    """

    return OpenAICompatibleClient(
        dev_config.API_BASE_URL,
        dev_config.MAIL_API_KEY or dev_config.API_KEY,
        os.environ.get("QQBOT_MAIL_MODEL", dev_config.API_MODEL),
        user_id=dev_config.MAIL_USER_ID,
        usage_store=_USAGE_STORE,
        usage_role="letter",
    )


def _plugin_action_seams(engine: DialogueEngine, transport: QQTransport):
    """给后台插件注入的两个窄接缝：`call_action` 与 `notify`。

    **权限与协议都留在核心**（2026-09-30 用户："入群审批也是插件"）：

    - `call_action(action, params)`：先过 `capabilities` 闸门（入群审批只批
      `purpose="join_approval"` 那一条），再调对面，回执拆成 `data` 返回；
      失败抛异常（`onebot_ws.call_api` 已经把 retcode≠0 变成异常了）。
      插件因此不需要知道回执形状，也绕不开闸门。
    - `notify(text)`：通知超管——走补发队列，连接断了也不会把这条提示弄丢。

    她们（后台插件）拿到的就是这两个函数，拿不到 transport 本身。

    ⚠️ 经由 `_SeamBinder`：闭包只捕获不透明令牌。第一版这两个闭包**直接捕获了
    `engine` 与 `transport`**，审查者用 `registry.notify.__closure__[1].cell_contents`
    就拿到了**活的传输层**并真的发了消息（2026-10-01）。
    """

    binder = _SeamBinder(engine, transport)
    return binder.call_action, binder.notify


def _action_caller_for(engine: DialogueEngine, transport: QQTransport):
    """`(purpose) -> 已过闸门的调用函数`。核心执行插件声明的动作时用它。"""

    return _SeamBinder(engine, transport).action_caller


def _start_memory(engine: DialogueEngine, client) -> MemoryService | None:
    """长期记忆服务。未配置 key 时返回 None，对话照常。"""

    if not dev_config.MEMORY_API_KEY:
        logger.warning("未设置 QQBOT_MEMORY_API_KEY / QQBOT_API_KEY；仅启用命令，普通对话和长期记忆维护暂不可用")
        return None
    settings = MemorySettings.from_environment()
    maintenance_client = OpenAICompatibleClient(
        dev_config.API_BASE_URL,
        dev_config.MEMORY_API_KEY,
        os.environ.get("QQBOT_MEMORY_MODEL", dev_config.API_MODEL),
        timeout=settings.model_timeout - 1,
        max_tokens=4096,
        user_id=dev_config.MEMORY_USER_ID,
        usage_store=_USAGE_STORE,
        usage_role="memory",
    )
    allowed = lambda: set(engine.enabled_group_ids) if engine.enabled else set()
    memory = MemoryService(maintenance_client, allowed, settings=settings,
                           feature_logs=engine.logs)
    engine.memory_service = memory
    # 记忆维护用的是**自己的 key**，命中率要单独算，所以把这个 client 记在引擎上。
    engine.memory_client = maintenance_client
    return memory


async def serve(transport: QQTransport, *, stage_label: str = "Stage 3") -> None:
    """装配并运行主循环，直到传输层关闭。

    `stage_label` 只用于日志，让两个阶段跑同一套循环时日志仍可分辨。
    """

    state_store = RuntimeStateStore() if state_persistence_enabled() else None
    engine = build_engine(transport, state_store=state_store)
    # 补发出站消息的队列。放在 serve 这一层：它是"投递"的事，不是"说什么"的事。
    # 挂到引擎上只是为了让 `/super status` 能报一句"有几条在等"。
    outbox = Outbox()
    engine.outbox = outbox

    provider = engine.context_provider
    if provider is not None and provider.client is not None:
        logger.info(
            "对话背景补全已启用：history=%s refresh=%ss http=%s",
            provider.history_count,
            int(provider.refresh_interval),
            dev_config.SNOWLUMA_HTTP_BASE_URL,
        )
    if state_store is not None:
        restored = engine.restore_state()
        logger.info(
            "运行状态已恢复：groups=%s sessions=%s admins=%s enabled=%s",
            restored["groups"],
            restored["sessions"],
            restored.get("admins", 0),
            engine.enabled,
        )

    memory = _start_memory(engine, engine.client)
    # 记忆起来之后把 store 交给人工操作接缝（面板的"删除问题记忆 / 调关系"用它）。
    # 放在这里而不是 `build_engine`：记忆服务是 `serve` 这一层起的。
    engine.memory_ops = MemoryOps(
        getattr(memory, "store", None) if memory is not None else None, source="webui",
    )
    # Stage 4 的插件（命令 + 后台通道）**已经由 `build_engine` 装好了**：
    # 发现只跑一次，这里只是把它的后台那一半拿出来交给同一条节拍循环。
    # 这里不再造 `call_action` / `notify`——那两个窄接缝在接插件时就注入给插件了。
    plugin_registry = getattr(engine, "plugin_registry", None)
    if plugin_registry is not None:
        # 事件循环补上：`build_engine` 是同步的，那时候还没有运行中的循环。
        # 跨线程往主循环丢协程的插件（面板）靠它。
        plugin_registry.set_loop(asyncio.get_running_loop())
    background = build_background_plugins(engine)
    await transport.start()
    if memory is not None:
        await memory.start()

    logger.info(
        "%s started: group=%s batch=%s cooldown=%s model=%s 判定=%s 记忆维护=%s",
        stage_label,
        dev_config.TARGET_GROUP_ID,
        BATCH_SIZE,
        COOLDOWN_SECONDS,
        engine.client.model,
        "开（judge' 用于积压消息）" if engine.judge_client is not None else "关（单 agent）",
        "开" if memory is not None else "关",
    )
    reporter = getattr(engine, "daily_reporter", None)
    if reporter is not None:
        logger.info(
            "每日汇报已就绪：收件人=%s 发送时间=%s（窗口＝距上次汇报到现在）",
            reporter.recipient,
            f"{reporter.hour:02d}:{reporter.minute:02d}",
        )
    background_tasks = [
        asyncio.create_task(run_background_plugin(plugin), name=f"plugin-{plugin.name}")
        for plugin in background if plugin_enabled(plugin)
    ]
    connection_task = asyncio.create_task(
        _watch_connection(transport), name="connection-watchdog"
    )
    restart_requested = False
    try:
        restart_requested = await _message_loop(transport, engine, outbox=outbox)
    finally:
        if state_store is not None:
            engine.persist_state()
        connection_task.cancel()
        await asyncio.gather(connection_task, return_exceptions=True)
        if restart_requested:
            # 刚发出的"正在重启"得有机会到达对面：进程一换，这条连接就没了。
            await asyncio.sleep(RESTART_GRACE_SECONDS)
        for task in background_tasks:
            task.cancel()
        await asyncio.gather(*background_tasks, return_exceptions=True)
        if memory is not None:
            await memory.close()
        await transport.close()
    if restart_requested:
        _restart_process()


# 启动后多久还没等到对面连上来就告警（秒），以及之后每隔多久再提醒一次。
# 由来（2026-09-29 开机自启那次）：NapCat 走注册表 Run 自启、bot 走启动文件夹，
# Explorer 先跑 Run 键 → NapCat 比 bot 早 10 秒，试连时 8080 还没开，之后再没重试。
# 结果是**她活着、日志也在走，但一条消息都进不来**，而且日志里只有"监听于 8080"，
# 看不出"没人连上来"。这一条告警就是为了让这种情况一眼可见。
CONNECTION_WARN_AFTER_SECONDS = 60.0
CONNECTION_WARN_EVERY_SECONDS = 600.0


async def _watch_connection(transport: QQTransport) -> None:
    """盯"对面连上来没有"。没连上就明确告警，并说清怎么救。"""

    await asyncio.sleep(CONNECTION_WARN_AFTER_SECONDS)
    while True:
        if not bool(getattr(transport, "connected", True)):
            logger.warning(
                "还没有 OneBot 客户端连上来（%s 秒）：消息进不来，她不会说话。"
                "常见原因是 QQ 客户端（NapCat）先于 bot 启动、之后没有重试——"
                "把它重开一次即可；开机顺序问题见 README「开机自启动」。",
                int(CONNECTION_WARN_AFTER_SECONDS),
            )
        await asyncio.sleep(CONNECTION_WARN_EVERY_SECONDS)


# 换进程之前留给"正在重启"这条回复的时间。
RESTART_GRACE_SECONDS = 1.0


def restart_argv(executable: str, spec_name: str | None, script_path: str) -> list[str]:
    """拼出"重新启动自己"的命令行。

    `-m` 启动的（`__main__.__spec__` 有名字）就按**模块名**重启：直接重跑
    `stage3_main.py` 的路径会让包内相对导入失去包上下文。这里不写死 stage3，
    两个阶段共用这套循环，硬编码会让 Stage 4 重启回 Stage 3。
    """

    if spec_name:
        return [executable, "-m", spec_name]
    return [executable, os.path.abspath(script_path)]


def _main_spec_name() -> str | None:
    spec = getattr(sys.modules.get("__main__"), "__spec__", None)
    name = getattr(spec, "name", None)
    return str(name) if name else None


def _restart_process() -> None:
    """用同一条命令把自己换掉——不依赖外部守护进程，PID 保持不变。

    Windows 上 `os.execv` 并不是真正的 exec，而是"起一个新进程 + 结束自己"，
    所以调用方必须**先把该关的关掉**（传输层、记忆服务、状态落盘）再调这里；
    新进程会继承 standard 句柄，因此日志还接着往同一个文件里写。
    """

    argv = restart_argv(sys.executable, _main_spec_name(), sys.argv[0])
    logger.warning("Stage 3 正在重启：%s", " ".join(argv))
    # 换进程前把日志与缓冲刷干净，否则最后几行会丢。
    logging.shutdown()
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:  # noqa: BLE001 - 刷不干净也不能挡住重启
            pass
    os.execv(sys.executable, argv)


# 焦点时钟的节拍。它负责"当值群安静下来之后，轮到队列里的下一个"——
# 没有它，"稍等"过的人要等到下一条消息才可能被处理。5 秒对 45 秒的判据足够细。
FOCUS_TICK_SECONDS = 5.0
# 补发时钟的节拍：连接一回来，最多 5 秒内就把攒下的消息发出去。
OUTBOX_TICK_SECONDS = 5.0


async def _message_loop(
    transport: QQTransport, engine: DialogueEngine, *, tick_seconds: float = FOCUS_TICK_SECONDS,
    outbox=None,
) -> bool:
    """一条输入 → 若干条出站的主循环，外加一个焦点时钟和补发时钟。

    三个任务并行：
    - 收：从传输层取消息 → `engine.handle()` → 投递（这一步与从前一样，逐条串行）；
    - 焦点时钟：每 `FOCUS_TICK_SECONDS` 调一次 `engine.tick()`，由它决定要不要释放当值群、
      切到下一个群、并把被排队消息的回复交出来；
    - 补发时钟：每 `OUTBOX_TICK_SECONDS` 让 `Outbox` 试着把"确定没送出去"的消息补上
      （连接没回来时它直接返回，不会去等那 15 秒的连接超时）。

    单条消息失败只记日志并继续等待后续消息——一次异常不能带走整个连接。

    返回值表示"是否收到了重启指令"：主循环会就此停下，剩下的收尾（落盘、关传输层、
    换进程）交给 `serve`。`/super restart` 必须是**最后一条**被处理的消息，
    所以这里一旦拿到标记就直接返回。
    """

    async def receive_loop() -> bool:
        while True:
            message = await transport.receive()
            if message is None:
                return False
            # 把**已经排队**的消息一起拿出来，并按"命令优先"排序（2026-09-30 用户：
            # "我发一个命令一分多钟才回复"）。命令不进模型，几毫秒就回完；
            # 而一条对话要「判定 ~2s + 拟人停顿 2~4s」，串行时命令会排在几十条后面。
            # 同一批里各自的相对顺序不变，只是命令先跑。
            batch = [message]
            drain = getattr(transport, "drain_pending", None)
            if callable(drain):
                try:
                    batch.extend(drain())
                except Exception:  # noqa: BLE001 - 排序用的优化，失败就当没排队
                    logger.debug("drain_pending 失败，按原顺序处理", exc_info=True)
            ordered: list[tuple[IncomingMessage, bool]] = []
            for item in batch:
                try:
                    is_command = engine.handles_command(item)
                except Exception:  # noqa: BLE001 - 判定失败就按普通消息处理
                    is_command = False
                ordered.append((item, is_command))
            ordered.sort(key=lambda pair: not pair[1])
            for index, (item, _is_command) in enumerate(ordered):
                # 后面还有东西没处理（无论是不是命令）→ 把拟人停顿去掉，先追上队列。
                backlog = index < len(ordered) - 1
                if await _handle_one(item, backlog=backlog):
                    return True

    async def _handle_one(item: IncomingMessage, *, backlog: bool) -> bool:
        """处理一条消息并把它的出站投递掉。返回是否收到了重启指令。"""

        try:
            reply = await engine.handle(item)
            # 一条输入可能对应多条出站消息：首条是 handle() 的返回值，
            # 其余几段在 follow-up 队列里，按同样节奏接着发。
            # 2026-09-30 起续发**按会话**取：读信回信是第二个调用方，
            # 引擎级的列表会让两个会话互抢（见 take_follow_ups 的注释）。
            outgoing = _as_outgoing_list(reply) + engine.take_follow_ups(item.session_id)
            if outgoing:
                await _deliver_reply(transport, engine, item, outgoing, outbox=outbox,
                                     backlog=backlog)
            for delivery in engine.drain_relay_deliveries():
                await _deliver_relay(transport, delivery, outbox=outbox)
        except Exception:
            logger.exception("消息处理失败，继续等待后续消息")
        # 重启标记在 try 之外读：回复发失败（比如连接刚断）也必须照着重启，
        # 否则管理员下的指令会被静默吞掉。
        if engine.consume_restart_request():
            logger.info("收到重启指令，停止接收新消息")
            return True
        return False

    async def focus_loop() -> None:
        while True:
            await asyncio.sleep(tick_seconds)
            try:
                for source, outgoing in await engine.tick():
                    await _deliver_reply(transport, engine, source, outgoing, outbox=outbox)
            except Exception:
                logger.exception("焦点时钟出错，继续")

    async def outbox_loop() -> None:
        if outbox is None:
            return
        while True:
            await asyncio.sleep(OUTBOX_TICK_SECONDS)
            try:
                resent, dropped = await outbox.flush(transport)
                if resent or dropped:
                    logger.info(
                        "outbox flush: resent=%s dropped=%s pending=%s",
                        resent, dropped, len(outbox),
                    )
            except Exception:
                logger.exception("补发出站消息时出错，继续")

    receiver = asyncio.create_task(receive_loop(), name="receive")
    ticker = asyncio.create_task(focus_loop(), name="focus-tick")
    resender = asyncio.create_task(outbox_loop(), name="outbox-tick")
    logger.info(
        "焦点时钟已启动：每 %.1fs 一跳（当值群安静后轮到队列里的下一个）", tick_seconds
    )
    try:
        return await receiver
    finally:
        # 收消息的任务结束（传输层关闭）就停掉时钟；再把在途的收尾做完。
        ticker.cancel()
        resender.cancel()
        await asyncio.gather(ticker, resender, return_exceptions=True)
