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
from .api_usage import ApiUsageStore
from .background_plugins import (
    build_background_plugins,
    plugin_enabled,
    run_background_plugin,
)
from .capabilities import CapabilityRegistry
from .control_audit import ControlAudit
from .conversation_context import ConversationContextProvider
from .extensions import PromptSources
from .llm_client import OpenAICompatibleClient, apply_client_overrides
from .memory_config import MemorySettings
from .memory_ops import MemoryOps
from .memory_service import MemoryService
from .onebot_client import SnowLumaHttpClient
from .outbox import Outbox
from .provider_registry import base_url_of, known as provider_known
from .qq_roles import SelfRoleCache
from .state_store import RuntimeStateStore
from .style_reviewer import StyleReviewer
from .vision import ImageDescriber
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
    audit = ControlAudit(enabled=state_persistence_enabled())

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
        prompt_sources=PromptSources(knowledge_base=build_knowledge_base()),
        style_reviewer=style_reviewer,
        vision=vision,
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
    # **她自己在每个群的 QQ 角色**：核心持有的基础设施（`qq_roles.py`），
    # 群主命令（`/super qqadmin`、`/title` …）与入群审批都用它。
    #
    # 2026-09-30 真机回归：这块原来由 `_start_join_approval` 顺手创建，后来那个函数
    # 搬进 `background_plugins.py` 时**只搬了用法、没搬创建**，于是 `self_roles`
    # 一直是 None——群主命令全变成"我在这个群里是查不到"，而入群审批因为拿不到角色，
    # 静默地什么都不批。所以创建放在这里（装配点），并加了测试钉住。
    engine.self_roles = SelfRoleCache(transport, ttl=dev_config.SELF_ROLE_TTL_SECONDS)
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
    return engine


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
    """

    async def call_action(action: str, params: dict[str, object] | None = None):
        registry = getattr(engine, "capabilities", None)
        if registry is not None:
            # 读用 `read`、写只放这一条 `join_approval`——**不能一律按写的那道闸判**：
            # `get_group_system_msg` 是只读 action，拿写入用途去查会被直接拒
            # （踩过：接缝第一版就是这么写的，审批每轮都读不到申请）。
            from .capabilities import JOIN_APPROVAL_ACTIONS

            purpose = "join_approval" if action in JOIN_APPROVAL_ACTIONS else "read"
            registry.check(action, purpose=purpose)
        call = getattr(transport, "call_api", None)
        if not callable(call):
            raise RuntimeError("这个通道不支持动作调用")
        from .onebot_client import unwrap_result

        return unwrap_result(await call(action, params or {}))

    async def notify(text: str):
        targets = tuple(
            str(item) for item in getattr(dev_config, "APPROVE_NOTIFY_USER_IDS", ()) if str(item)
        )[:3]
        for user_id in targets:
            await _send_or_queue(
                transport, getattr(engine, "outbox", None), MessageTarget(user_id=user_id), text
            )

    return call_action, notify


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
    # Stage 4 的后台通道（读信回信、每日汇报、入群审批）**都从插件装配点拿**：
    # 这里不再认识"邮箱""审批"这些名字，只拿到一串插件，给每个跑同一个节拍。
    # 权限与动作执行仍在核心：下面这两个闭包就是注入给插件的窄接缝。
    call_action, notify = _plugin_action_seams(engine, transport)
    background = build_background_plugins(
        engine,
        call_action=call_action,
        notify=notify,
        roles=getattr(engine, "self_roles", None),
        loop=asyncio.get_running_loop(),
    )
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
