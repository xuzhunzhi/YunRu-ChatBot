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
import time
from pathlib import Path

from . import dev_config
from . import notice_text
from . import operator_config
from . import prompt_library
from . import provider_failover
from . import runtime_flags
from ._host_adapters import FileAuditSink, LocalMachineProbe, build_host_services
from .api_usage import ApiUsageStore
from .background_plugins import (
    build_background_plugins,
    plugin_enabled,
    run_background_plugin,
)
from .capabilities import CapabilityRegistry
from .chat_window import collect_window_facts
from .conversation_context import ConversationContextProvider
from .extensions import PromptSources
from .letter_context import collect_letter_context
from .letter_docs import load_letter_facts, load_letter_persona
from .llm_client import OpenAICompatibleClient, apply_client_overrides
from .memory_config import MemorySettings
from .memory_ops import MemoryOps
from .memory_readonly import MemoryRead
from .memory_service import MemoryService
from .onebot_client import SnowLumaHttpClient
from .outbox import Outbox
from .state_store import RuntimeStateStore
from .style_reviewer import REVIEW_TIMEOUT_SECONDS, StyleReviewer
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
    LearnedSeams,
    PluginRegistry,
    ReportSeams,
    UiSeams,
    _maybe_await,
    attach_plugins,
    inventory,
    mount_data_plugins,
)
from .transport import (
    DeliveryRejected,
    DeliveryUncertain,
    IncomingMessage,
    MessageNotDelivered,
    MessageTarget,
    QQTransport,
)
from .remind import RemindDelivery, RemindSendResult

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


# --- 模型 / 凭据自检（2026-10-06）--------------------------------------------


#: 面板上"某一路的 key"这项配置 → 它管哪个 `usage_role` 的 client。
#:
#: 2026-10-06 加了 `letter_api_key` / `vision_api_key`：这两个用途以前**没有**面板
#: 入口（识图那把 key 甚至没有装配点，见汇报里的清单）。它们的取值方式与
#: 其余几路一字不差——都走 `model_config` 的用途解析。
#:
#: 2026-10-12 起 `letter_api_key` 有一处要留神：写信那一路的**默认** key 已经改成借
#: `judge` 那条（见 `model_config.TASKS`），所以面板改这一项只影响这个 client 就地
#: 用的那把 key（`_apply_live` 是按 `usage_role` 找它的），**不改**"默认从哪个变量
#: 来"。要整路换默认：地址（`letter_api_base_url`）与模型（`letter_api_model`）得
#: 跟着一起改，否则就是"A 家 key 打 B 家地址"。
_KEY_SETTING_ROLES: dict[str, str] = {
    "api_key": "dialogue",
    "reply_api_key": "dialogue",
    "judge_api_key": "judge",
    "memory_api_key": "memory",
    "review_api_key": "review",
    "letter_api_key": "letter",
    "vision_api_key": "vision",
}

#: `model_config` 的用途名 → 账本 / client 上的 `usage_role`。
#: 两者是同一件事的两个叫法：账本按 `usage_role` 分桶（历史名字），
#: 三层配置按用途名分条。（只有 reply 与 dialogue 不同名，其余同名；写全是为了不许猜。）
_TASK_TO_ROLE: dict[str, str] = {
    "reply": "dialogue",
    "letter": "letter",
    "judge": "judge",
    "memory": "memory",
    "review": "review",
    "vision": "vision",
}

#: 反过来。**从 `_TASK_TO_ROLE` 现算**，不另抄一份（两份表会漂移）。
_ROLE_TO_TASK: dict[str, str] = {role: task for task, role in _TASK_TO_ROLE.items()}


def _log_model_config_self_check() -> list[str]:
    """启动自检：每个用途一行，**只打名字与"有值没有"**。

    这一行是给"今晚那种事"准备的：`.env` 里全局地址指着 mimo、主 key 是 mimo 那把，
    而判定 / 记忆 / 审核的 key 是 deepseek 那把 —— 于是那三个子系统拿 deepseek 的
    key 打 mimo，全 401，而这**在日志里一点痕迹都没有**（只看到一排 401）。

    现在每个用途都报：provider 名 / 模型名 / key 从哪个变量来（有值没有）。
    **key 的值绝不出现**（`model_config.TaskEndpoint.safe_summary` 只认变量名）。
    地址的来源就是 key 那条所绑的 provider，所以上下两行必须同家；不同家一眼可见。

    返回那几行（测试直接断言它，不必去抓日志）。
    """

    from .model_config import providers_file_lines, self_check_lines

    lines = self_check_lines()
    for line in lines:
        logger.info("模型配置 %s", line)
    missing = [line for line in lines if "**缺**" in line]
    if missing:
        logger.warning("有用途没有可用的 key：%s", "；".join(missing))
    # **返回值一个字不动**（用例钉着 `返回值 == self_check_lines()`）；文件状态那几行
    # 只进日志。理由：那一份是"每个用途一行"的纯解析表，混进去会让条数不变式破掉
    # （契约 §3.2，照 `fallback_self_check_lines` 的先例）。
    try:
        for line in providers_file_lines():
            logger.info("%s", line)
    except Exception:  # noqa: BLE001 - 自检多一行失败不该让启动的这条链断掉
        logger.warning("模型供应商文件状态打印失败", exc_info=True)
    return lines


def _log_mail_recipient_self_check() -> list[str]:
    """启动自检：**收件人配了没有**——只打键名，绝不打地址。

    为什么单开一条钩子而不是并进 `_log_model_config_self_check()`：那一份是
    "每个用途一行"、条数被用例钉死（`test_the_self_check_prints_names_not_values`
    断言 `len(lines) == len(EXPECTED)`），混进去会让那条不变式不再成立。

    由来（2026-10-16 用户原话）：**"改成必须由 `.env` 给"**。收件人的默认值被拿掉之后，
    "没配"就成了一个**必须说出口**的状态：夜里那封信没来，与"她今晚没话说"在日志上
    长得一模一样，而这一行是唯一能分清的地方。形状照上面那份凭据自检：缺的带 `**缺**`，
    再把缺的那几行汇总成一条 WARNING。
    """

    from .dev_config import mail_report_self_check_lines

    lines = mail_report_self_check_lines()
    for line in lines:
        logger.info("邮件配置 %s", line)
    missing = [line for line in lines if "**缺**" in line]
    if missing:
        logger.warning("邮件配置缺收件人（写信 / 日报不会发）：%s", "；".join(missing))
    return lines


# --- 面板接缝：配置 / prompt / 记忆 / 审计 -----------------------------------
#
# 面板（Stage 4 插件）不能自己碰引擎，只能调这里装配出来的那几个闭包。
# 这一段就是"核心给插件的东西"的落点：权限、动作执行、审计都在核心里。


#: 面板上**投给文件**的键 → `(用途, 这个键管哪一项)`。
#:
#: 契约 §3.5 的落点：这些键必须在 `SETTINGS` 查表**之前**被接管（那一张表的语义是
#: "写进 `os.environ`"，正是要退掉的那条路）。**显式枚举、不许通配**：前缀匹配会破掉
#: `operator_config.py:33-34`"白名单就是能力边界"那条性质。
#:
#: ⚠️ 2026-10-18 第二轮（新形状：key 是供应商下的列表、用途由 `agents.<用途>` 绑
#: `provider → key → model`）：这里**只剩"模型名"一类**。
#:
#: - `*_api_key` / `*_provider` 换成 `ui.model_provider_key` / `ui.model_provider_bind`；
#: - `*_api_base_url` 也一起搬走：**地址是供应商的属性**，不是某一路的属性。新形状里
#:   没有"只给 reply 换地址"这件事（要给一个地址，就加一套供应商再把那一路绑过去）。
#:   老的那种写法留着只会造出"这一路的地址与这一路的 key 不同源"的半条——正是要消灭的。
_FILE_SETTINGS: dict[str, tuple[str, str]] = {
    "reply_api_model": ("reply", "model"),
    "letter_api_model": ("letter", "model"),
    "memory_model": ("memory", "model"),
    "vision_api_model": ("vision", "model"),
}

#: 这些键**已经搬到面板「模型与凭据」那一页**（密钥一行一把 / 绑定一行三件套）。
#:
#: 留着它们当"能写进覆盖层"是个陷阱：文件里的 `agents.<用途>` 一旦写了，覆盖层就被压住，
#: 于是"我明明填了 key"与"它根本没生效"会同时成立。所以这里**明确拒绝**并指向新页面——
#: 拒绝比静默无效好（与 `operator_config` 的"白名单就是能力边界"同一条思路）。
_MOVED_SETTINGS: dict[str, str] = {
    **{f"{task}_api_key": task for task in
       ("reply", "letter", "judge", "memory", "review", "vision")},
    **{f"{task}_provider": task for task in
       ("reply", "letter", "judge", "memory", "review", "vision")},
    **{f"{task}_api_base_url": task for task in
       ("reply", "letter", "vision")},
}

#: `*_api_key` 这项配置 → 它管哪个用途（"key 有没有值"的回显要按文件 ∪ 覆盖层判）。
_SECRET_SETTING_TASK: dict[str, str] = {
    "reply_api_key": "reply",
    "letter_api_key": "letter",
    "judge_api_key": "judge",
    "memory_api_key": "memory",
    "review_api_key": "review",
    "vision_api_key": "vision",
}


def _file_value(field: str, value: object) -> str:
    """把面板传来的值归一化（**不**走 `operator_config.normalize`：新键不在白名单里）。

    只有两种：地址 / 模型名，都是字符串；空串 = "把这一路从文件里删掉"（D12）。
    """

    text = " ".join(str(value or "").split())
    return text


def _sync_after_write(task: str) -> None:
    """写完文件之后让活 client 生效（契约 D13：重指向，而不是把面板值盖上去）。

    两道一起上：
    - `sync_from_file()`：按文件 mtime 判断，兜底模式下**故意不动**（推迟、不丢）；
    - `repoint_task(task)`：只重指这一路。为什么还要它——"清空某一路"在文件里可能是
      一个 **no-op**（本来就没写这一条），mtime 没变、第一道会正确地什么都不做，但内存里
      那个 client 仍然需要回到环境那套（D12 的"空值 = 回落"）。
    """

    from . import provider_failover

    try:
        provider_failover.sync_from_file()
        provider_failover.repoint_task(task)
    except Exception:  # noqa: BLE001 - 同步失败不该让"已经写进文件"变成写失败
        logger.warning("模型来源同步失败（写完之后）", exc_info=True)


def _apply_file_settings(engine, task: str, fields: dict[str, str],
                         keys: list[str], *, config, audit, source: str,
                         token: str) -> dict[str, object]:
    """把一路的**模型名**改动写进文件，然后让活 client 重指向。

    2026-10-18 第二轮之后的形状：这里**只剩 `model` 一个字段**。供应商与 key 的绑定走
    `ui.model_provider_bind`（每路一行：供应商 → key → 模型）；地址是供应商的属性，
    走「模型与凭据」页给那一套改，不在这一条路上。

    - **清空（空串）** = 把这一路从文件里删掉（D12：回到 `.env` 那套），不造半条；
    - **改模型** = `agents.<用途>.model`，`provider` / `key` 绑定一个字不动。

    生效不靠"把面板值盖到 client 上"，而是写完之后 `sync_from_file()` 重新 `resolve()`
    一遍（D13）。
    """

    from . import model_config
    from .model_providers_store import ModelProvidersRejected, clear_agent, upsert_agent

    endpoint = model_config.resolve(task)  # 当前这一路（文件有就按文件，否则按环境）
    entry = model_config.file_agent_entry(task)

    # D12：**空值 = 把这一路从文件里删掉**（回到环境变量那套）。
    if "model" in fields and not fields["model"]:
        try:
            result = clear_agent(task, audit=audit, source=source, token=token)
        except ModelProvidersRejected as exc:
            return {"applied": "rejected", "detail": str(exc), "keys": keys}
        _sync_after_write(task)
        popped = _drop_operator_values(config, keys)
        detail = f"已把模型名清空 ⇒ {task} 从模型供应商文件里删掉（回到 .env 那套）"
        if popped:
            detail += "；旧覆盖项已清理"
        return {"applied": "live", "detail": detail, "keys": keys,
                "cleared": bool(result.get("cleared"))}

    provider = str(entry.get("provider", "") or "") or endpoint.provider
    if not provider:
        return {"applied": "rejected", "detail": "说不清要写哪一家（provider 为空）", "keys": keys}
    model = fields.get("model", "") or str(entry.get("model", "") or "") or endpoint.model
    if not model:
        return {"applied": "rejected", "detail": "模型名为空", "keys": keys}
    try:
        written = upsert_agent(
            task, provider=provider, model=model,
            base_url=model_config.provider_base_url(provider),
            audit=audit, source=source, token=token,
        )
    except ModelProvidersRejected as exc:
        return {"applied": "rejected", "detail": str(exc), "keys": keys}
    # 写完之后**不**把面板值盖到 client 上：让它重新 `resolve()` 一遍
    # （兜底期间 `sync_from_file()` 故意不动，改动推迟、不丢，见 §6.4）。
    _sync_after_write(task)
    popped = _drop_operator_values(config, keys)
    detail = (f"已写入 {os.path.basename(model_config.providers_file_path())}：{task} → "
              f"{written['provider']}（模型 {written['model']}）")
    if written.get("converged"):
        detail += f"；同一地址已有 {written['provider']}，按地址收敛（用户 U3）"
    if popped:
        detail += "；旧覆盖项已清理"
    return {"applied": "live", "detail": detail, "keys": keys}


def _drop_operator_values(config, keys: list[str]) -> bool:
    """D8：面板把某用途写进文件时，**顺手清掉** `operator_config.json` 里同一项的旧值。

    不清的话，将来把那一项**从文件里删掉**时旧值会**悄悄复活**（"我明明删了"）。
    只删**同一项**；值仍在 `control_audit` 里有记录、`.env` 也还在。
    """

    popped = False
    for key in keys:
        if key in getattr(config, "values", {}):
            config.values.pop(key, None)
            popped = True
    return popped


def _build_client_overrides(engine, operator) -> dict[str, str]:
    """凭据类配置"改过没有"：**文件 ∪ 覆盖层**，永不返回值。

    契约 §3.5：key 现在住在 `data/model_providers.json` 里，只读 `operator_config`
    会显示成"未设置"（而它明明配好了）——这就是"面板说没配、实际有 key"的分叉。
    """

    from . import model_config

    stored = getattr(operator, "values", {}) or {}
    result: dict[str, str] = {}
    for key, spec in operator_config.SETTINGS.items():
        if spec["kind"] != "secret":
            continue
        if stored.get(key):
            result[key] = "已设置"
            continue
        task = _SECRET_SETTING_TASK.get(key)
        result[key] = "已设置" if task and model_config.file_has_key(task) else "未设置"
    return result


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
    - **六个用途的模型 / 地址改投文件**（2026-10-18 契约 §3.5）：它们在进白名单查表之前
      就被 `_FILE_SETTINGS` 接管，写进 `data/model_providers.json`（唯一存储），然后同步
      重指向活 client，所以**不会**再被写进 `os.environ` 或 `operator_config.json`。
    - **已经搬去「模型与凭据」页的那几个键（`_MOVED_SETTINGS`）在这里明确拒绝**，
      并说清去哪儿改。为什么不是"留着它顺手写进覆盖层"：文件里的 `agents.<用途>` 一旦
      写了就会压住覆盖层，于是"我明明填了 key"与"它根本没生效"会同时成立——静默无效
      是这类配置里最坏的一种。
    """

    config = store if store is not None else operator_config.shared()
    applied: dict[str, dict[str, str]] = {}
    chosen_provider = str(payload.get("provider", "") or "").strip().casefold()
    explicit_url = str(payload.get("api_base_url", "") or "").strip()
    # ⓪ **投给文件的键先接管**——必须排在 `SETTINGS` 查表之前（那张表的语义是
    #    "写进 os.environ"，正是本轮要退掉的那条路）。
    for key in list(payload):
        task = _MOVED_SETTINGS.get(str(key))
        if task is None:
            continue
        applied[str(key)] = {"applied": "rejected", "detail": (
            f"{key} 这一项已经搬到面板「模型与凭据」页：那一页每路一行，"
            "依次选供应商 → key → 模型（用户 2026-10-18：「agent那边先选供应商再选"
            "对应的key最后选供应商提供的模型」）。在这里改不会生效，所以直接拒掉")}
    file_groups: dict[str, dict[str, str]] = {}
    file_keys: dict[str, list[str]] = {}
    for key in list(payload):
        if str(key) in applied:
            continue
        routing = _FILE_SETTINGS.get(str(key))
        if routing is None:
            continue
        task, field = routing
        file_groups.setdefault(task, {})[field] = _file_value(field, payload[key])
        file_keys.setdefault(task, []).append(str(key))
    for task, fields in file_groups.items():
        result = _apply_file_settings(engine, task, fields, file_keys.get(task, []),
                                      config=config, audit=audit, source=source,
                                      token=token)
        for key in result.get("keys", []):  # type: ignore[union-attr]
            applied[str(key)] = {"applied": str(result["applied"]),
                                 "detail": str(result["detail"])}
    for key in list(payload):
        if str(key) in applied:
            continue  # 已由文件那一档处理（成功或拒绝）
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
            # 「换供应商」的实质是**换那个供应商的地址**（三层配置里的 providers 那一层）。
            # 它不该再把 `QQBOT_API_BASE_URL` 铺给所有 client——那正是"A 家 key 打
            # B 家地址"的来源。只有"没有自己那条 key、按兼容档那一对在走"的用途
            # 需要跟着兼容档地址走，所以这里也把兼容档地址一起改掉，然后逐用途重算。
            url = base_url_of(chosen_provider)
            config.values["api_base_url"] = url
            os.environ["QQBOT_API_BASE_URL"] = url
            # **兼容档那一对现在是这个供应商的**：把声明也写上（`QQBOT_API_PROVIDER`），
            # 绑定在别家 key 上的用途仍然按它们自己那条走（下一步重算）。
            from .model_config import COMPAT_PROVIDER_ENV

            if os.environ.get("QQBOT_API_KEY", "").strip():
                os.environ[COMPAT_PROVIDER_ENV] = chosen_provider
            # 有自己那条 key 的用途（判定 / 记忆 / 审核 / 识图 / 写信，以及配了
            # 专属地址的回复）要按**自己那条来源**重算并盖回去——否则面板切一次
            # 全局供应商就会造出"deepseek 的 key + mimo 的地址"，
            # 那正是 2026-10-06 那个 401 的形状。
            _reapply_pairing_all()
            applied["provider"] = {"applied": "live", "detail": f"地址已切到 {url}"}
        elif chosen_provider == "custom":
            applied["provider"] = {"applied": "live",
                                   "detail": "自定义地址：请同时填 api_base_url"}
        else:
            applied["provider"] = {"applied": "rejected", "detail": "不认识的供应商"}
    # 面板改完配置之后，**已经存在**的那些 client 要按"当值模式"重新指一遍：
    # 余额兜底期间（fallback）面板写任何一项，都不能把它们悄悄拉回主家。
    # primary 模式下这是空操作——`reapply()` 故意一个都不动、也不打日志。
    moved = provider_failover.reapply()
    if moved:
        logger.info("余额兜底期间面板改了配置：已按兜底那一家的端点重指 %s 个 client", moved)
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
    if key in _KEY_SETTING_ROLES:
        role = _KEY_SETTING_ROLES[key]
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
                # 回复 agent 有专属 key 时，不该被这次"全局回落"冲掉。
                _reapply_reply_overrides(api_key=True)
                return True
            main_key = os.environ.get("QQBOT_API_KEY", "") or _cfg.API_KEY
            if not main_key:
                return False
            apply_client_overrides(api_key=main_key, usage_role=role)
            _reapply_pairing(role)
            return True
        # 只改那一类 client（判定 key 不该顺手把回复的也改了）。
        #
        # **先把值写进环境，再解析那一对**：`model_config` 是按环境里的**当前**值
        # 解析"这条 key 属于谁"的，顺序反了就会拿到旧 key 的供应商。
        os.environ[env] = value
        endpoint = _endpoint_of(_ROLE_TO_TASK[role])
        apply_client_overrides(api_key=value, base_url=endpoint.base_url, usage_role=role)
        if key == "api_key":
            apply_client_overrides(api_key=value)
            # 上面这次是"全局"改写，回复 agent 有专属 key 时要盖回去。
            _reapply_reply_overrides(api_key=True)
        else:
            # **地址要跟着 key 走**：换了某一路的 key，就把那一路的地址按"这条 key
            # 所绑的供应商"重新解析一次。没有这一步，内存里的 client 会保留旧地址，
            # 重启之后又变回去——那正是"面板显示的和实际生效的不一样"。
            _reapply_pairing(role)
        return True
    if key in {"api_model", "memory_model"}:
        model = str(value).strip()
        role = "memory" if key == "memory_model" else ""
        apply_client_overrides(model=model, usage_role=role)
        os.environ[operator_config.env_name(key)] = model
        if key == "api_model":
            # 同上：回复 agent 有专属模型时，全局改写之后要盖回去。
            _reapply_reply_overrides(model=True)
        return True
    if key == "reply_api_model":
        # 回复 agent 的模型可以单独换。**空值 = 回落全局模型**（清掉覆盖层那一项）。
        env = operator_config.env_name(key)
        model = str(value).strip()
        os.environ.pop(env, None)
        if model:
            os.environ[env] = model
        elif config is not None:
            config.values.pop(key, None)
        from . import dev_config as _cfg

        apply_client_overrides(model=_cfg.reply_api_model(), usage_role="dialogue")
        return True
    if key == "api_provider":
        # **那把主 key 属于哪一家**（`model_config.COMPAT_PROVIDER_ENV`）。
        # 改它等于改"兼容档那一对是哪家的"——兼容档的地址也跟着它算，
        # 所以改完要按新的那家逐用途重算一遍。
        provider = str(value).strip().casefold()
        env = operator_config.env_name(key)
        if not provider:
            os.environ.pop(env, None)
            if config is not None:
                config.values.pop(key, None)
        else:
            from .model_config import file_provider_specs, PROVIDERS

            # 2026-10-18（契约 §3.5）：这一处也走**合并视图**——文件里的新家
            # （如 `relay1`）同样可以声明"主 key 属于它"，否则面板选得到、解析却报
            # "不认识的供应商"。
            if provider not in PROVIDERS and provider not in file_provider_specs():
                return False
            os.environ[env] = provider
        _reapply_pairing_all()
        return True
    if key == "api_base_url":
        url = str(value).strip()
        if not url:
            return False
        apply_client_overrides(base_url=url)
        os.environ[operator_config.env_name(key)] = url
        # **有自己那条 key 的用途不许被搬走**：全局地址只对"没有自己 key、
        # 因此按兼容档那一对在走"的用途有效（回复专属地址也一样）。
        _reapply_pairing_all()
        return True
    if key == "reply_api_base_url":
        # 回复 agent 的地址可以单独换。**空值 = 回落全局地址**（清掉覆盖层那一项）。
        env = operator_config.env_name(key)
        url = str(value).strip()
        os.environ.pop(env, None)
        if url:
            os.environ[env] = url
        elif config is not None:
            config.values.pop(key, None)
        # 清掉之后地址回落到**reply 那条 key 所绑的那家**（不是"全局地址"）。
        _reapply_pairing("dialogue")
        return True
    if key == "provider":
        # 由 `apply_overrides` 在循环之后统一处理（要与显式地址比优先级）。
        return True
    return None


def _reapply_reply_overrides(*, model: bool = False, api_key: bool = False) -> None:
    """全局改动之后，把**回复 agent 已配好的专属值**再盖回去。

    语义是"**兼容档 = 默认值，reply 专属（任务级覆盖）= 覆盖它**"
    （见 `dev_config.reply_api_*`）。没有这一步，面板改一次全局模型 / 主 key 就会把
    已经配好的 reply 专属值在**内存里**冲掉，而重启之后它又回来了——那正是
    "面板显示的和实际生效的不一样"。

    **只在"专属值真的配了"时才盖回去**（判据就是那两个环境变量非空）：
    没配的时候全局那次改动本来就是对的，再"盖回去"等于把刚设的全局值又还原成
    旧值。

    ⚠️ 2026-10-06：**地址那一档从这个函数里撤掉了**。地址不再由"全局 vs 专属"
    决定，而是按"这条 key 属于谁"解析——那是 `_reapply_pairing` /
    `_reapply_pairing_all` 的事（见它们的说明）。
    """

    from . import dev_config as _cfg

    def _specific(name: str) -> str:
        return os.environ.get(name, "").strip()

    def _file_has() -> bool:
        """文件里配了 reply 这一路吗（读 store 的解析结果，不猜）。"""

        from . import model_config

        return model_config.file_has_agent("reply")

    # 判据（契约 §3.5）：原来只看环境变量，而"文件里配了、环境里没配"是新架构下最常见
    # 的形状——那时这一步会被**永久跳过**（文件里的专属值被全局改动冲掉）。
    if model and (_file_has() or _specific("QQBOT_REPLY_API_MODEL")):
        apply_client_overrides(model=_cfg.reply_api_model(), usage_role="dialogue")
    if api_key and (_file_has() or _specific("QQBOT_REPLY_API_KEY")):
        apply_client_overrides(api_key=_cfg.reply_api_key(), usage_role="dialogue")


def _reapply_pairing(usage_role: str) -> None:
    """某一路的 key 改过之后，把**它那一对的地址**按同一条重新解析并盖回去。

    这是"base 与 key 成对取自同一条"在**热更路径**上的落点：面板换了判定那把 key，
    判定 client 的内存地址就要跟着换成那条 key 所绑的供应商的地址。少了这一步，
    内存里会留下"新 key + 旧地址"，而那正是今晚那种 401 的形状——只是这次由
    面板自己造出来，重启后自愈，所以更难查。
    """

    task = _ROLE_TO_TASK.get(str(usage_role), "")
    if not task:
        return
    from .model_config import resolve

    endpoint = resolve(task)
    apply_client_overrides(base_url=endpoint.base_url, usage_role=usage_role)


def _reapply_pairing_all() -> None:
    """把所有 client 的地址按**每个用途自己那条来源**重算一遍。

    什么时候需要它：面板改了"全局地址 / 全局供应商"。那时**只能**影响那些
    "没有自己那条 key、因此按兼容档那一对在走"的用途；有自己那条 key 的用途
    （判定 / 记忆 / 审核 / 识图 / 写信，配了自己变量的那些）地址必须留在自己那家——
    否则面板切一次全局供应商就会造出"deepseek 的 key + mimo 的地址"，
    而那正是 2026-10-06 那个 401 的形状。

    判据全部来自 `model_config.resolve`（地址就是它算出来的那一个），这里**不重算**
    任何东西——重算就等于又开了一条能拼出混搭的路径。
    """

    from .model_config import TASK_NAMES, resolve

    for task in TASK_NAMES:
        endpoint = resolve(task)
        apply_client_overrides(base_url=endpoint.base_url,
                               usage_role=_TASK_TO_ROLE.get(task, task))


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
    # **启动自检**（2026-10-06）：每个用途一行，只打 provider 名 / 模型名 /
    # key 从哪个变量来（有值没有）——**绝不打 key 的值**（见 `model_config`）。
    #
    # 为什么放在装配最前面：今晚那种事（deepseek 的 key 打 mimo 的地址）在这里
    # 一眼就能看出来——地址的来源就是 key 那条所绑的 provider，所以那一行里的
    # provider 名与 key 变量名必须同家；缺 key 的那一行带 `**缺**`。
    _log_model_config_self_check()
    # **收件人自检**（2026-10-16）：`MAIL_REPORT_TO` 不再有代码默认值，所以"没配"
    # 必须在这里说一句——否则表现只是"夜里那封信没来"，与"她今晚没话说"分不开。
    _log_mail_recipient_self_check()
    client = OpenAICompatibleClient(
        *_reply_endpoint(),
        # 请求体里发不发 DeepSeek 专有字段（`user_id` / `reasoning_effort`）由它决定；
        # 余额兜底切到 mimo 之后这里会跟着变成 "mimo"。
        provider=_endpoint_of("reply").provider,
        # 按 agent 隔离 KVCache 与调度（同一账号下生效，与 API Key 无关）。
        user_id=dev_config.DIALOGUE_USER_ID,
        usage_store=usage_store,
        usage_role="dialogue",
    )
    judge_client = _build_judge_client(usage_store)
    if judge_client is not None:
        logger.info("判定与回复分离：两个 agent 各用独立 user_id 隔离缓存")
    style_reviewer = _build_style_reviewer(usage_store)
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
        # 识图（`vision`）**不在这里**：它是 Stage 4 插件，`attach_plugins()` 之后
        # 由 `registry.vision` 那个工厂现造（见下面那一行）。`engine.vision` 缺省
        # 是 `None` = 有图的消息只留 `[图片]` 占位符。
        vision=None,
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
    # **身份与权限事实进核心了**（2026-10-06 用户："行，进核心"）。
    #
    # 它原来长在插件侧（`plugins/roles/`），核心这边留一个空位等插件来填；那条形状错了
    # 两处：一是身份判定属于"权限判定/护栏"，按 `AGENTS.md` §2.3 必须留在核心；
    # 二是插件那份**只回答"她自己是什么角色"**，"对方是管理员还是群主"根本没得问，
    # 而她要知道的恰恰有对方那一半。
    #
    # 现在：核心自己造这个服务（`group_roles.py`），事实只走 `call_action` 的**只读**
    # 那一族（`action_caller("read")`：已过 `capabilities` 闸门，插件与引擎都拿不到
    # transport）。插件要问"某人是不是管理员"就调 `registry.shared_roles()` 上这一份，
    # **不自己取**。
    #
    # `LazyGroupRoles` 是**懒构造**的：模块的 import 推迟到第一次真要用的时候，
    # 而且失败被兜住 → 那台机器上角色一律 `unknown`（fail-closed），
    # **引擎照样起得来**。这保住了判据"删掉它，Stage 3 照样跑得起来"。
    engine.group_roles = _LazyGroupRoles(engine, transport)
    # 面板要用的四样东西挂到引擎上（插件拿不到引擎，只拿得到装配点给的闭包）：
    # 覆盖层、prompt 库、记忆人工操作、操作审计。
    engine.operator_config = operator
    engine.prompt_library = prompts
    engine.memory_ops = MemoryOps(None, source="webui")
    engine.control_audit = audit
    # 记忆维护的**运行期总闸**在 `memory_maintenance_agent` 那一侧（面板能关）：它每一拍现读
    # `runtime_flags`，关掉只是不再批新记忆，检索照旧——**面板那颗开关的语义就是这个**。
    # 2026-10-11 查"面板开关不生效"时才发现这里原来还有一句
    # `engine.memory_enabled = flags.memory_enabled`：全仓库（三条线）只有那一次赋值、
    # 零次读取，抄过去的值谁也不看，只让面板回的 `applied:"live"` 显得比实际更可信。删掉。
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
    # 黑话词条（2026-10-06 用户口径："还有**黑话**"）：**被动**听来的解释 → 一份本地 JSON。
    # 装配点在这里把两半接上：
    # - `engine.slang_library`：读/改写口，`/super slang` 与面板那条窄接缝用它；
    # - `engine.slang_watcher`：观察者，由 `_message_loop` 在**收**那一侧顺手调一次。
    # 它**没有** transport、没有 notify、没有模型 client，也没有 `async def`——
    # "她因此问一句"在这条路上没有可调用的东西（`slang_learning` 的模块说明里写了）。
    engine.slang_library, engine.slang_watcher = _build_slang_learning(engine)
    # **第二个加载根**（2026-10-08）：`data/plugins/` 里"装进来的"插件与
    # `src/qq_roleplay_bot/plugins/` 里预装的走**同一条**发现路径（`discover()` 扫
    # 整个 `__path__`）。目录不在、取不到数据目录 = 没有第二个根，**不是错误**，
    # 也不许成为必需依赖——"删掉全部插件核心照跑"这条判据继续成立。
    # 装配点只有这里一处：别的地方不许自己往 `__path__` 上加根。
    mount_data_plugins()
    # **接插件**（发现只跑这一次，所以放在最后：引擎上该有的东西都已经就位）。
    # 命令插件进 `engine.commands`；后台插件留在 `engine.plugin_registry.backgrounds`
    # 给 `serve` 的节拍用。
    #
    # **插件拿不到引擎**（2026-10-01 修掉的越界）：引擎上有 `transport` 与三份权限名单，
    # 给出去就等于"插件能自己发消息、能读权限名单"。所以这里造两个**窄接缝对象**：
    # `ChatSeams`（对话四件事）与 `ReportSeams`（写日报五件事），
    # 它们内部持有引擎引用，但对插件只暴露**函数**。
    call_action, notify = _plugin_action_seams(engine, transport)
    # 动作调用函数与角色事实服务**共用同一个 `_SeamBinder`**：一个令牌一张私表，
    # 少一处"又造了一个绑定"的机会。
    binder = _SeamBinder(engine, transport)
    registry = PluginRegistry(call_action=call_action, notify=notify,
                              roles=None, loop=None,
                              chat=_chat_seams_for(engine),
                              report=_report_seams_for(engine),
                              ui=_ui_seams_for(engine),
                              # 提醒那条投递口（2026-10-08）：**核心拥有出站**，
                              # 插件只递一个"发给谁/发到哪/说什么"的意图对象。
                              remind_send=binder.remind_send,
                              action_caller=binder.action_caller)
    # **把核心的角色事实服务放到共享位上**：插件要问"某人是不是管理员"就走
    # `registry.shared_roles()`，拿到的是核心这一份——插件**不自己取**（`AGENTS.md`
    # §2.3：权限判定、护栏留在核心）。走既有接缝而不是新造一个：`provide_roles()`
    # 本来就是"把角色查询放到共享位上"那个口，现在由核心来放，语义没变。
    # 放在 `discover()` **之前**，插件在 `register()` 里就取得到。
    registry.provide_roles(engine.group_roles)
    attach_plugins(registry, engine.commands)
    engine.plugin_registry = registry
    # **识图**：能力由插件给（`plugins/vision/`），核心只问"有没有"。
    #
    # 为什么不在这里 import 那个模块（2026-10-05 搬它时改）：`plugins/__init__.py`
    # 的规矩是"核心**不 import 具体插件**，只调 `discover()`"。原来是
    # `_build_vision()` 里一句 `from .vision import ImageDescriber` 兜 `ImportError`——
    # 那是"核心知道插件模块叫什么"，搬完就删了。现在插件经 `registry.vision`
    # 放一个**工厂**上来，核心把用量账本递进去（识图的账要记在核心账本上，
    # 与 `_build_judge_client(usage_store)` 同一形状）。
    #
    # 没有插件（删掉 `plugins/vision/`）= 工厂是 `None` = 有图的消息只留占位符，
    # **其余一切照旧**。这正是判据"删掉它，Stage 3 照样跑得起来"。
    build_vision = getattr(registry, "vision", None)
    engine.vision = build_vision(usage_store) if callable(build_vision) else None
    # **到点提醒**：能力由插件给，核心只问"有没有"（形状同识图那一行，但更窄）。
    # 登记的是 `(RemindRequest) -> None`：核心算好**绝对时刻**（只有核心知道"群里这边"
    # 是哪个时区）与**邮箱**（从记忆里查好）之后交给它；插件自己存、自己到点发，
    # 后台节拍在插件那一侧——核心不加循环。
    # 没有插件（删掉那个文件夹）= None = 核心**认到提醒意图也什么都不做**
    # （不报错、不回复、不留半条状态）：判据"删掉它，Stage 3 照样跑得起来"靠它成立。
    engine.remind = getattr(registry, "remind", None)
    # **群管理动作的执行函数**同样由插件给（`registry.provide_group_action(...)`）：
    # 核心不再 `from .plugins.group_admin.group_admin import execute`——那个写法把
    # 插件模块名写进了核心，改名就等于**静默丢功能**（见 `plugins/__init__.py` 那段）。
    # 这里把账本递进去、结果挂在 `engine.group_actions` 上，`execute_action` 只按
    # `ActionRequest.group` 取。没有插件 = 空字典 = 那两句回 fail-closed 的文案。
    #
    # 形状与识图那一行同一套：登记的是**工厂**（`(usage_store) -> execute`），
    # 因为执行函数要用核心的账本记 API 用量，而账本在装配时才现造。
    _build_group_actions(engine, registry, usage_store)
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


#: 哪些渠道**允许**声明"这条来自主人"——**引导项**，不是清单本身。
#:
#: ## 清单的主人是**渠道自己**（2026-10-06 改）
#:
#: 这个常量原来叫 `_OWNER_CHANNELS`、是一条 `("mail",)` 的白名单，也就是
#: **核心知道有一个叫 mail 的渠道**。外部审查判定它是"扩展性耦合"而不是安全洞：
#: `claims_owner` 只影响给模型看的 `sender_role` 标签，**不授予任何命令权限**
#: （命令权限一律走 QQ 那条路，见 `stage3_main._is_super_admin_control`），
#: 但每加一个同样可信的渠道都得回来改核心。
#:
#: 现在改成**渠道自己声明**（`registry.provide_owner_channel("<名字>")`，
#: 在插件的 `register()` 里，见 `plugins.PluginRegistry.provide_owner_channel`），
#: 核心只问"这个渠道说过它可以吗"（`_SeamBinder._owner_channel_ok`）。
#:
#: 这里留的这一条是**过渡用的引导项**，理由是"改动要能单独落地、不能要求插件侧
#: 同时改"：`mail` 那条通道即使还没加那句声明，行为也一字不变。
#: **新渠道一律走声明，不许往这里加。** 那句"新渠道要在这里加一条"的注释
#: 连同这张写死的表一起删掉了——现在唯一的接缝是 `provide_owner_channel`。
_OWNER_CHANNELS_BUILTIN: tuple[str, ...] = ("mail",)

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


# --- 模型供应商：面板那一组接缝的只读视图（2026-10-18）------------------------
#
# 用户原话：「**这里，我要的是添加而非替换**」，以及同一天的「每个agent一把key，
# 一个供应商下最少两把，用来跟agent一一对应」。所以面板那一页是**多套并存**的管理面：
# 一份清单 + **每套六格 + main** + 加一套 + 切当前 + 删一套。这一小段只负责"把文件里的
# 事实说清楚"，写是 `_SeamBinder.model_provider_*` 那几个方法的事（它们转发给
# `model_providers_store` 的正式写层）。
#
# **密钥在这里就被掐掉**（只给掩码与长度）。理由写一遍省得下次有人"顺手把值传出去
# 让面板自己掩"：那种约定活不过下一轮——已经在 2026-10-06 的 `mask_settings` 上
# 见过一次（面板那一侧掩了，另一条路没掩）。测试按"整份 JSON 里不含明文"钉住。


def _mask_provider_key(value: object) -> str:
    """密钥的展示形式。**8 位以内全星号**（首尾都不给，短 key 露两个字符就露太多）。

    长度也一并返回（`_provider_key_view`）：操作者要能分清"没填"与"填了一把"，
    而这两件事在 4+4 的掩码上可能长得一样。
    """

    text = str(value or "")
    if not text:
        return ""
    if len(text) <= 8:
        return "*" * len(text)
    return f"{text[:4]}…{text[-4:]}"


def _provider_key_view(value: object) -> dict[str, object]:
    """**一格** key 的事实：**有没有 / 多长 / 掩码**，没有明文。

    参数从"一个条目的 dict"改成"一把 key 的值"（2026-10-18 第二轮）：一个条目下现在有
    七格，掩码要按格给——面板的每一行才知道自己那一格填过什么、填了几个字符。
    """

    key = str(value or "")
    return {"present": bool(key), "length": len(key), "masked": _mask_provider_key(key)}


#: 写信那一行的小字（用户 2026-10-18：插件读的是"核心里配好的"那份）。
LETTER_ROW_NOTE = "由 mail 插件使用（它不自己配 key，只从核心接缝拿客户端）"


def _provider_key_rows(provider_keys) -> list[dict[str, object]]:
    """一套供应商的 key 行（面板「密钥」那一块的**平铺列表**，一 key 一行）。

    只给 `id / label / 掩码 / 长度`，**没有明文**。`used_by`（被哪几路用着）由调用方
    按 `agents.<用途>.key` 填——那是"用途 → key"这一层关系唯一存在的地方（用户
    2026-10-18：key 是供应商下的一个列表，用途绑哪把写在 agents 里）。
    """

    rows: list[dict[str, object]] = []
    for item in provider_keys or ():
        rows.append({
            "id": str(getattr(item, "id", "") or ""),
            "label": str(getattr(item, "label", "") or ""),
            **_provider_key_view(getattr(item, "value", "")),
        })
    return rows


def _binding_rows(tasks: tuple[str, ...]) -> list[dict[str, object]]:
    """**各路绑定**（每路一行）：`provider → key → model` 三件套 + 现在实际落在哪一层。

    读三处，**各读各的、不互相猜**：

    - `provider` / `key` / `model`：文件里 `agents.<用途>` 的**原始绑定**（没写就是空串，
      面板据此把下拉框留空 ="不指定"）；
    - `layer` / `layer_label` / `present` / `reason`：`model_config.task_key_layer()`
      ——**四层回落链的唯一实现**。不在面板这一侧另算一遍：两份实现必然漂移，而漂移的
      结果正好是最坏的那种（面板说"绑的是 k1"、实际打的是 `.env` 主 key）。
    - `model_declared`：这个模型名在不在 `providers.<那一家>.models` 里（空列表 = 那一家
      没声明过模型，这一项报 `None`="说不清"，面板就不标红）。
    """

    from . import model_config
    from . import model_providers_store as store

    view = store.load()
    agents = dict(getattr(view, "agents", {}) or {})
    models = dict(getattr(view, "models", {}) or {})
    rows: list[dict[str, object]] = []
    for task in tasks:
        entry = dict(agents.get(task) or {})
        provider = str(entry.get("provider", "") or "").strip().casefold()
        key_id = str(entry.get("key", "") or "").strip()
        model = str(entry.get("model", "") or "").strip()
        try:
            layer = model_config.task_key_layer(task)
        except Exception:  # noqa: BLE001 - 一路算不出来不该让整页打不开
            layer = {}
        declared = tuple(models.get(provider, ()) or ())
        key_label = ""
        if provider and key_id:
            for item in (getattr(view, "keys", {}) or {}).get(provider, ()) or ():
                if str(getattr(item, "id", "") or "") == key_id:
                    key_label = str(getattr(item, "label", "") or "")
                    break
        rows.append({
            "task": task,
            "label": model_config.TASK_LABELS.get(task, task),
            "note": LETTER_ROW_NOTE if task == "letter" else "",
            "provider": provider,
            "key": key_id,
            "key_label": key_label,
            "model": model,
            #: 这一路现在**实际**用的是哪一层 key（四层里的一层，或 "env" = 没写进文件）。
            "layer": str(layer.get("layer") or ""),
            "layer_label": str(layer.get("layer_label") or ""),
            "present": bool(layer.get("present")),
            "reason": str(layer.get("reason") or ""),
            "source": str(layer.get("source") or ""),
            #: 这个模型名在这一家声明的模型里吗（`None` = 那一家没声明过模型）。
            "model_declared": (model in declared) if declared else None,
        })
    return rows


def _model_name_list(models: object) -> tuple[list[str] | None, str]:
    """把面板给的"这一家声明提供哪些模型"整理成模型名列表；`None` = **不动这一项**。

    三种输入：`None`（不改）/ 空串或空列表（清空声明）/ 列表或逗号分隔的字符串。
    **模型名允许是列表外的值**（用户 2026-10-18：模型可以手输，面板会标注"不在该供应商
    声明的模型里"）——所以这一份只是"声明的清单"，**不是白名单**，别拿它去卡写入。
    """

    if models is None:
        return None, ""
    if isinstance(models, str):
        items: list[object] = [part for part in models.replace("，", ",").split(",")]
    elif isinstance(models, (list, tuple)):
        items = list(models)
    elif models == "":
        return [], ""
    else:
        return None, "「声明的模型」要写成数组或逗号分隔的字符串"
    names: list[str] = []
    for item in items:
        text = " ".join(str(item or "").split())
        if not text:
            continue
        if len(text) > 200:
            return None, f"模型名太长（超过 200 字符）：{text[:20]}…"
        if text in names:
            continue
        names.append(text)
    return names, ""


def _task_legacy_keys(task: str) -> list[str]:
    """一路的**旧式面板键**（`reply_provider` / `reply_api_key` / …）。

    写文件时顺手把 `operator_config.json` 里同名旧值清掉（D8：不清的话，将来把那一项
    从文件里删掉时旧值会**悄悄复活**——"我明明删了"）。这一份也用在"搬走的那几个键"的
    拒绝理由里，避免两处各写一份清单。
    """

    return [f"{task}_provider", f"{task}_api_key", f"{task}_api_base_url",
            f"{task}_api_model"]


def _provider_key_list(keys: object) -> tuple[list[dict[str, str]], str]:
    """把面板给的"这一套的若干把 key"整理成 `[{id?, label?, value}]`；不合法就给原因。

    **形状**：列表，一项一把。`id` 可选（不给就由 store 按 `k1/k2/…` 自动给），
    `label` 可选，`value` 必填（空值的项直接忽略——"一把没有值的 key 不是 key"）。

    **不限制最少几把**（用户 2026-10-18："不强制最少几把 key，填一把也行"），
    但**至少要有一把**：只写地址、一把 key 都没有的条目在读侧会被拒（地址与 key 必须
    成对），那样加进来就是个用不了的空壳，不如当场说清。
    """

    if keys is None or keys == "":
        return [], ""
    if isinstance(keys, dict):
        # 宽容另一种形状：`{id: value}`（人手工调接口时省事）。
        keys = [{"id": str(slot), "value": value} for slot, value in keys.items()]
    if not isinstance(keys, (list, tuple)):
        return [], "「若干 key」要写成 [{value: …}, …]（一项一把 key）"
    cleaned: list[dict[str, str]] = []
    for index, item in enumerate(keys):
        if isinstance(item, str):
            item = {"value": item}
        if not isinstance(item, dict):
            return [], f"第 {index + 1} 把 key 不是对象（要写成 {{value: …}}）"
        value = " ".join(str(item.get("value") or "").split())
        if not value:
            continue
        entry = {"value": value}
        key_id = str(item.get("id") or "").strip()
        if key_id:
            entry["id"] = key_id
        label = " ".join(str(item.get("label") or "").split())
        if label:
            entry["label"] = label
        cleaned.append(entry)
    return cleaned, ""


def _model_providers_view() -> dict[str, object]:
    """`ui.model_providers()` 的实现：**密钥清单** + **各路绑定** + 被拒原因。

    为什么放在**模块级函数**而不是接缝方法里：`_SeamBinder` 那一族的每个方法都该是
    "转发一层"，读视图这段有几十行判断，塞进类里会让那一族不再一眼看得出"它们只是转发"。

    两块（用户 2026-10-18 定的页面结构）：

    - `providers[].keys`：**平铺的 key 列表**（一家下面一行一把），每行带
      `id / label / 掩码 / 长度 / 被哪几路用着`；
    - `bindings[]`：**每路一行**的 `provider → key → model` 绑定 + "现在实际落在哪一层"。

    **没有文件**时返回 `providers: []`、`bindings` 里全是"没写进文件"（不是报错）：
    面板第一次打开时文件往往还不存在，那时"一个都没有"是**正确答案**。
    """

    from . import model_config
    from . import model_providers_store as store

    snapshot = store.load()
    status = model_config.status()
    tasks = tuple(model_config.TASK_NAMES)
    file_agents = dict(getattr(snapshot, "agents", {}) or {})

    # 每一套被哪几路用着（按 `agents.<用途>.provider`）；每一把 key 又被哪几路用着
    # （按 `agents.<用途>.key`）。这两张表是"删除按钮能不能点"的唯一依据。
    used_by_provider: dict[str, list[str]] = {}
    used_by_key: dict[tuple[str, str], list[str]] = {}
    for task in tasks:
        entry = file_agents.get(task) or {}
        name = str(entry.get("provider", "") or "").strip().casefold()
        if not name:
            continue
        used_by_provider.setdefault(name, []).append(task)
        key_id = str(entry.get("key", "") or "").strip()
        if key_id:
            used_by_key.setdefault((name, key_id), []).append(task)

    providers: list[dict[str, object]] = []
    raw_providers = dict(getattr(snapshot, "providers", {}) or {})
    keys_by_name = dict(getattr(snapshot, "keys", {}) or {})
    models_by_name = dict(getattr(snapshot, "models", {}) or {})
    for name in sorted(raw_providers):
        entry = raw_providers.get(name) or {}
        key_rows = _provider_key_rows(keys_by_name.get(name))
        for row in key_rows:
            row["used_by"] = list(used_by_key.get((name, row["id"]), ()))
        blocked = _provider_remove_blocker(name, used_by_provider.get(name, ()))
        providers.append({
            "name": name,
            "base_url": str(entry.get("base_url", "") or ""),
            "client_type": str(entry.get("client_type", "") or ""),
            # 这家声明的模型名（面板第三级下拉框就是它；空 = 没声明，只能手输）。
            "models": list(models_by_name.get(name, ()) or ()),
            "used_by": list(used_by_provider.get(name, ())),
            # 平铺的 key 列表（**一家下面一行一把**）：id / label / 掩码 / 长度 / 被谁用。
            "keys": key_rows,
            "key_count": len(key_rows),
            "can_remove": not blocked,
            "remove_reason": blocked,
        })

    return {
        "path": str(status.get("path", "")),
        "present": bool(status.get("present")),
        "last_error": str(status.get("last_error", "") or ""),
        "version_ok": bool(status.get("version_ok", True)),
        "tasks": list(tasks),
        "providers": providers,
        #: 每路一行的绑定（`provider → key → model` + 实际落在哪一层）。
        "bindings": _binding_rows(tasks),
        #: 供应商下拉框的选项：内置那几家 + 文件里的新家（文件里的**优先**，
        #: 与 `provider_registry` 合并视图同一口径）。
        "options": _provider_options(),
        "file_agents": {task: dict(file_agents.get(task) or {}) for task in tasks
                        if task in file_agents},
        # 文件里写了但这一路用不了（key 悬空 / 缺 key / 不认识的供应商）。
        "rejected": dict(status.get("rejected") or {}),
        "issues": list(status.get("issues") or ()),
        # 同一个地址两个名字（用户 U3：会让 402 余额兜底静默失效）。
        "notes": list(status.get("notes") or ()),
    }


def _provider_options() -> list[dict[str, object]]:
    """供应商下拉框的数据：**只给名字与地址**（公开信息），**一个 key 都不给**。

    内置那几家在前（面板一直以来的顺序），文件里的新家排在后面；同名由文件那一条覆盖
    地址（与 `model_config._provider_spec` 同一口径：面板显示的就是这一趟真会打过去的
    地址）。`in_file` 让面板知道"这一家在文件里有没有条目"——没有的话它的 key 下拉框
    是空的（那份 key 只能在 `.env` 里）。
    """

    from . import model_config

    specs = model_config.file_provider_specs()
    try:
        from . import provider_registry

        builtin = list(provider_registry.options())
    except Exception:  # noqa: BLE001 - 下拉框不该因为这份表读不到而打不开
        builtin = []
    if not builtin:
        builtin = [{"value": name, "label": name, "base_url": spec["base_url"]}
                   for name, spec in model_config.PROVIDERS.items()]
    options: list[dict[str, object]] = []
    seen: set[str] = set()
    for item in builtin:
        name = str(item.get("value") or "").strip().casefold()
        if not name or name in seen:
            continue
        seen.add(name)
        spec = specs.get(name)
        options.append({
            "value": name,
            "label": str(item.get("label") or name),
            "base_url": (spec or {}).get("base_url") or str(item.get("base_url") or ""),
            "in_file": spec is not None,
        })
    for name in sorted(specs):
        if name in seen:
            continue
        seen.add(name)
        options.append({"value": name, "label": name,
                        "base_url": specs[name]["base_url"], "in_file": True})
    return options


def _provider_remove_blocker(name: str, used_by: object = ()) -> str:
    """删这一套会不会有问题 → 原因（空串 = 可以删）。**视图与写口共用这一处判断**。

    两条（第一条是用户 2026-10-18 定的，第二条是上一轮那道补充）：

    1. **还有 `agents.<用途>` 指着它** → 拒绝（先切走再删，否则那几路悄悄掉回 `.env`）；
    2. `.env` 的 `QQBOT_API_PROVIDER` 正声明着它、而它只存在于文件里 → 也拒绝
       （删掉之后走主 key 的那几路会**当场解析不出来**）。

    为什么把它抽出来：面板要**预先**把删除按钮禁用并写明原因，而写口要真的拒绝——
    两处各写一遍的话，迟早出现"按钮能点、点了报错"或者更坏的"按钮说安全、实际删了出事"。
    """

    import os

    from .model_config import COMPAT_PROVIDER_ENV, PROVIDERS

    clean = str(name or "").strip().casefold()
    users = [str(task) for task in (used_by or ())]
    if users:
        return ("还被 " + "、".join(f"agents.{task}" for task in sorted(users))
                + " 用着：先在「各路绑定」里把它们换到别家，再删")
    declared = os.environ.get(COMPAT_PROVIDER_ENV, "").strip().casefold()
    if clean and clean == declared and clean not in PROVIDERS:
        return (f"{COMPAT_PROVIDER_ENV} 现在声明着 {clean}（主 key 属于那一家），"
                "而它只存在于这份文件里：删掉之后走主 key 的那几路会当场解析不出来。"
                "先把 .env 里那一行（或面板「供应商」那个字段）改掉，再删。")
    return ""



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

    async def notify(self, target: object, text: str | None = None) -> str:
        """通知：**发出去**，然后把这一条记进目标会话的历史（`speaker="notice"`）。

        ## 两种调用形状（老的那个一个字不用改）

        * `notify(text)`：老写法，收件人是超管（`APPROVE_NOTIFY_USER_IDS`，最多 3 个），
          与 2026-09-30 起 `join_approval` 用的一模一样。
        * `notify(target, text)`：`target` 是一个 `MessageTarget`（群或私聊），或者
          从配置里读出来的映射 `{"group_id": "…"}` / `{"user_id": "…"}`。
          **不收裸字符串**：`"800000001"` 是群号还是 QQ 号，这里猜不出来，
          猜错等于把通知发到别处去——比不发更糟（`_notice_target` 会抛）。

        ## 为什么记历史这件事放在这里（而不是交给插件）

        用户 2026-10-19：「发送通知这个，包括命令的回复，能不能通过规则让云茹识别成
        另一个人在说话」→「哎要不还是通知吧，不要当人了」。也就是**发出去的通知要进
        她的历史**，记成 `speaker="notice"`（不是人、也不是她说的）。

        这件事只能在核心做：插件拿不到会话历史（`AGENTS.md` §2.3：插件不许碰对话与
        记忆），而"这条到底发出去没有"只有这里知道。

        ## "成功发出才记"

        `_send_or_queue` 的三种结果：

        | 结果 | 记不记 | 为什么 |
        | --- | --- | --- |
        | `sent` | 记 | 对面收到了 |
        | `queued` | 记 | 进了补发队列，**它一定会被补发**；补发那条路不会再回调这里，不记就等于永远丢 |
        | `failed` | **不记** | 队列都没有/满了，这条真的没出去——记进去就是让她以为发过 |

        返回值是这三种状态之一（旧版返回 `None`；多这一位是为了让插件能记数，
        `join_approval` 那种忽略返回值的调用方不受影响）。

        进历史的那一份**不是原文**：先过 `notice_text.for_history`（去 CQ 码/@、
        脱敏 QQ 号/路径/凭据、截断 200 字）。见 `_record_notice`。
        """

        engine = self._require()
        transport = self._require_transport()
        if text is None:
            # 老写法：`notify(text)`。收件人由配置定，正文就是那个参数。
            body = "" if target is None else str(target)
            targets: tuple[MessageTarget, ...] = tuple(
                MessageTarget(user_id=str(item))
                for item in getattr(dev_config, "APPROVE_NOTIFY_USER_IDS", ()) if str(item)
            )[:3]
        else:
            body = str(text)
            targets = (_notice_target(target),)
        status = "failed"
        for item in targets:
            status = await _send_or_queue(
                transport, getattr(engine, "outbox", None), item, body
            )
            if status in ("sent", "queued"):
                _record_notice(engine, item, body)
        return status

    async def remind_send(self, delivery: RemindDelivery) -> RemindSendResult:
        """把一条提醒**发出去**：`async (RemindDelivery) -> RemindSendResult`。

        用户 2026-10-08 拍板 (a)：**核心拥有出站，插件只交意图**（AGENTS §2.3 的既有规矩
        ——插件拿不到 `transport`，也拿不到任意 action 名）。所以插件递进来的
        `RemindDelivery` **只有**"发给谁 / 发到哪 / 说什么"三个字段，怎么发由这里定：

        * **群里** → 用 `@那个人` 发（传输层支持 `send_at` 就用；不支持就退回纯文本，
          消息本身照发——少一个 @ 可以接受，**发不出去不行**）；
        * **私聊** → 直接发给他；对面拒收（还不是好友 / 没有临时会话）时，
          传输层那条"给操作者一句人话"的路照旧（`onebot_ws` 那一处），
          这里把结果**如实**回成 `rejected`，让插件自己记数与决定要不要再试。

        **目标不合法就不发**（2026-10-09 独立审查第 9 条）：`channel="group"` 却没给群号
        （从前会**悄悄按私聊发**）、`channel="private"` 却给了群号、`channel` 认不出、
        `person_id`/`text` 为空——一律回 `no_target`，什么都不发。`channel` 留空串才按
        `group_id` 推（那是老插件的写法，只有一种读法，不构成"猜"）。

        **不抛异常给插件**（"别抛到没人接"）：确定没送出去 / 不知道 / 被拒 / 别的异常，
        一律分类成 `RemindSendResult` 返回。**不复用 `_send_or_queue`**：补发队列会让
        插件看不到失败，而"计数与重试"是插件那一侧的事。

        只有"接缝自己丢了绑定"（`_require*`）照旧抛——那是核心的 bug，不是投递结果。
        """

        engine = self._require()
        transport = self._require_transport()
        person_id = str(getattr(delivery, "person_id", "") or "")
        text = str(getattr(delivery, "text", "") or "")
        group_id = str(getattr(delivery, "group_id", "") or "")
        channel = str(getattr(delivery, "channel", "") or "").strip().lower()
        del engine  # 只要它还在（绑定没丢）——值本身这里不用，见上面的说明
        # 认不出的投递：什么都不发，也不报错（fail-closed）。
        if not person_id or not text:
            return RemindSendResult(False, "no_target", "person_id/text 为空")
        if channel not in ("", "group", "private"):
            # 认不出的说法一律不发：猜一个可能把话送到错的地方去。
            return RemindSendResult(False, "no_target", "channel 认不出：%s" % channel)
        if channel == "group" and not group_id:
            # **独立审查 2026-10-09 第 9 条**：说了"发到群里"却没给群号时，从前是
            # **悄悄按私聊发**——等于把群里该说的话塞进私聊（插件那边的回复还写着
            # "我在群里 @ 你"，对不上）。不合法就不发，如实回结果。
            return RemindSendResult(False, "no_target", "说了发到群里，却没给群号")
        if channel == "private" and group_id:
            return RemindSendResult(False, "no_target", "说了私聊，却给了群号")
        # 留空串 = 老插件没给：按群号推。这两种都只有一种读法，不构成"猜"。
        is_group = channel == "group" or (not channel and bool(group_id))
        target = (MessageTarget(group_id=group_id) if is_group
                  else MessageTarget(user_id=person_id))
        try:
            send_at = getattr(transport, "send_at", None)
            if is_group and callable(send_at):
                await send_at(target, person_id, text)
            else:
                await transport.send(target, text)
        except MessageNotDelivered as exc:
            return RemindSendResult(False, "not_delivered", str(exc))
        except DeliveryUncertain as exc:
            return RemindSendResult(False, "uncertain", str(exc))
        except DeliveryRejected as exc:
            return RemindSendResult(False, "rejected", str(exc))
        except Exception as exc:  # noqa: BLE001 - 传输层没分类的异常也如实回
            logger.exception("remind_send failed: target=%s", target.group_id or person_id)
            return RemindSendResult(False, "error", type(exc).__name__)
        logger.info("remind delivered: group=%s person=%s", group_id or "-", person_id)
        return RemindSendResult(True, "ok")

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
           渠道**自己声明**（`registry.provide_owner_channel`，过渡期另加核心那条
           引导项）+ 渠道的 `claims_owner` 共同决定（判定在 `_owner_channel_ok`），
           而 **`claims_owner` 是插件给的布尔、不可信**——所以它只影响
           `sender_role` 这个给模型看的标签，**不授予任何命令权限**（命令权限一律走
           QQ 那条路，见 `stage3_main._is_super_admin_control`）。
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
        is_owner = bool(parcel.claims_owner) and self._owner_channel_ok(channel, engine)
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
        self._require().group_roles = cache

    def reporter_sink(self, reporter: object) -> None:
        self._require().daily_reporter = reporter

    def _owner_channel_ok(self, channel: str, engine) -> bool:
        """这个渠道**允许**声明"这条来自主人"吗。**核心不认识任何具体渠道名。**

        两个来源，缺一不可地合成一张当前清单：

        1. **渠道自己声明的**（`registry.provide_owner_channel("<名字>")`）——
            以后新增渠道走这条；
        2. 核心那份**过渡引导项**（模块级那个元组，目前只有 `mail`）——
            为了"这次改动不要求插件侧同时改"而留的，行为与改之前一致。

        判定仍然是核心的：声明只表示"这个渠道愿意为这句话负责"，
        最终还要**同时**满足 `claims_owner is True`（见 `deliver`）。
        """

        if not str(channel):
            return False
        registry = getattr(engine, "plugin_registry", None) if engine is not None else None
        knows = getattr(registry, "knows_owner_channel", None)
        if callable(knows) and knows(channel):
            return True
        return str(channel) in _OWNER_CHANNELS_BUILTIN

    def owner_channels(self, engine) -> tuple[str, ...]:
        """当前"允许声明主人"的渠道清单（**只读快照**，给接缝与测试看）。"""

        declared = ()
        registry = getattr(engine, "plugin_registry", None) if engine is not None else None
        taker = getattr(registry, "declared_owner_channels", None)
        if callable(taker):
            declared = tuple(str(item) for item in taker())
        return tuple(sorted(set(_OWNER_CHANNELS_BUILTIN) | set(declared)))

    def chat_seams(self) -> ChatSeams:
        return ChatSeams(reserved_user_ids=self.reserved_user_ids,
                         allow_private=self.allow_private,
                         deliver=self.deliver,
                         take_follow_ups=self.take_follow_ups,
                         roles_sink=self.roles_sink,
                         reporter_sink=self.reporter_sink,
                         # 只读快照：清单是"渠道声明的 + 那条过渡引导项"，
                         # 不再是核心写死的一张表。
                         owner_channels=self.owner_channels(self._require()))

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

    def report_window_facts(self, since: float):
        """日报要的窗口聚合（用户 2026-10-11 授权的**只读**例外）。

        回的是 `chat_window.collect_window_facts(...)` 那份**计数**：多少条、她回了
        多少、多少次专门叫她、几条带图或表情、哪些群与哪些人最活跃。
        **正文一个字都不出去**——聚合函数自己只看计数，不把 `body` 往外带。

        为什么不能只靠 `snapshot()`：引擎那份计数是**进程内**的，重启归零，
        她因此连着几天在信里写"两个群里没人开口"。聊天日志是落盘的，不受重启影响。
        """

        path = _chat_log_path(self._require())
        if path is None:
            return None
        try:
            return collect_window_facts(path, since=since)
        except Exception:  # noqa: BLE001 - 数不出来就不报数字，不编
            logger.exception("report_window_facts_failed")
            return None

    def report_letter_context(self, since: float):
        """写信要的"他说过的话"（用户 2026-10-13 授权的第二次破例）。

        由来说在 `letter_context.py` 与 `ReportSeams.letter_context` 上。这里只做三件
        装配上的事：

        1. 收信人由**引擎**认（`_letter_owner_ids()`：部署级管理员 ∪ 超管）。认不出来
           （或这台引擎没有那个方法）就**一句话都不给**——`collect_letter_context`
           `owner_ids` 为空时就是这个行为，宁可她今晚没素材，也不把一屋子人的话递出去。
        2. 上一封信取 `letter_history[0]`（`note_letter` 记的，最新的那条）。
        3. 读不出来就 `None`：调用方按"他这几天没说什么"处理，**不编**。
        """

        path = _chat_log_path(self._require())
        if path is None:
            return None
        owner = getattr(self._require(), "_letter_owner_ids", None)
        owners = ()
        if callable(owner):
            try:
                owners = tuple(owner() or ())
            except Exception:  # noqa: BLE001 - 认不出收信人就不给素材
                logger.exception("report_letter_owner_failed")
                owners = ()
        letters = list(getattr(self._require(), "letter_history", ()) or ())
        try:
            return collect_letter_context(path, since=since, owner_ids=owners,
                                          last_letter=letters[0] if letters else None)
        except Exception:  # noqa: BLE001 - 读不出来就不给素材，不编
            logger.exception("report_letter_context_failed")
            return None

    def report_seams(self) -> ReportSeams:
        engine = self._require()
        return ReportSeams(snapshot=self.report_snapshot,
                           client=getattr(engine, "client", None),
                           log_model_io=self.report_log_io,
                           note_letter=self.report_note_letter,
                           # 只读记忆口（2026-10-12 换掉了 `memory_service`）：
                           # 递过去的是 `MemoryRead`——只有查询方法，没有 store。
                           memory_read=_memory_read(engine),
                           # 只读窗口聚合（计数，不含正文）。见 `report_window_facts`。
                           facts=self.report_window_facts,
                           # 收信人自己说过的话 + 她上一封信的主题。见
                           # `report_letter_context`：只对收信人一个人破例。
                           letter_context=self.report_letter_context,
                           # **这一封信专用**的人格正文，每次现读（不是聊天那份）。
                           # 读不到返回 `None`，那一晚就不写——不回落到模板。
                           letter_persona=load_letter_persona,
                           # 同一条路走的《已确定的事》：也是现读，读不到那晚同样不写
                           # （事实校对全靠它，对着模板"校"等于没校）。
                           letter_facts=load_letter_facts,
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

    def ui_set_group_memory(self, group_id: str, enabled: bool) -> bool:
        """面板「记忆」页那颗**按群开关**：`(group_id, enabled) -> 这次到底改没改`。

        用户 2026-10-09：*"记忆开关肯定要上面板啊，在记忆页"*。**只包一层**：转发到引擎
        既有的 `enable_group_memory` / `disable_group_memory`（`/super enable memory`
        那套，落盘与恢复都是现成的）——**不造新状态**，也不给记忆内容或记忆 API。
        群号校验（数字 + 长度 5..12，与面板 `POST /api/groups` 那条一致）与幂等
        （本来就是这个状态返回 `False`）都在引擎那两个方法里。

        审计走既有的 `control_audit`：动作名与命令那条**相同**（`memory_group_switch`，
        这样一次筛选就能看到"这个群的记忆开关被谁动过"），`source="panel"` 区分是谁按的。
        """

        engine = self._require()
        enable = getattr(engine, "enable_group_memory", None)
        disable = getattr(engine, "disable_group_memory", None)
        if not (callable(enable) and callable(disable)):
            return False
        changed = bool(enable(group_id) if enabled else disable(group_id))
        # 命令那条是"每次调用都记一笔"（见 `_log_memory_switch`），这里保持一致：
        # 点了没变也留一笔（谁在什么时候按过，本身就是审计要回答的问题）。
        engine._log_memory_switch(group_id, bool(enabled), source="panel")
        return changed

    def ui_memory_ops(self):
        return getattr(self._require(), "memory_ops", None)

    def ui_state_reader(self):
        engine = self._require()
        return {"snapshot": engine.snapshot(), "usage_store": getattr(engine, "usage_store", None)}

    def ui_self_id(self):
        roles = getattr(self._require(), "group_roles", None)
        # **同步**取：这个接缝是 `() -> self_id`（见 `UiSeams.self_id`），不能 await。
        # 角色服务查过一次 `get_login_info` 之后这里就有值；没查过就是空串
        # （面板那边本来就要处理"还没有 self_id"这一档）。
        reader = getattr(roles, "cached_self_id", None)
        return reader() if callable(reader) else ""

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
            # 面板「记忆」页那颗按群开关：引擎既有那一对的**面板面**（见 `ui_set_group_memory`）。
            set_group_memory=self.ui_set_group_memory,
            session_clear=self.ui_session_clear,
            self_id=self.ui_self_id,
            knowledge=_knowledge_for(engine),
            # 插件清单（**只读**）：面板"插件"卡拿它列 tab。给的是函数 `inventory`，
            # 不是某个对象——接缝一律是函数，理由见 `UiSeams` 的说明。
            plugins=inventory,
            # 她**学来的东西**（金句 / 黑话）的读写：见下面 `learned_seams()`。
            learned=self.learned_seams(),
            # 「群聊」那一页：**只读**的群列表（名字 + 两个开关）。见 `ui_groups`。
            groups=self.ui_groups,
            # 「群聊」页每张卡片里的发送框（**操作者的话**，不是她发言）：
            # 见 `ui_notice` 与 `plugins.UiSeams.notice` 的说明。
            notice=self.ui_notice,
            # 「模型与凭据」页（2026-10-18 用户：「添加而非替换」+「一个key一行」）：
            # 一组六口——读清单 / 加一套 / 加或改一把 key / 删一把 key / 绑一路 / 删一套。
            # **密钥只给掩码与长度**，写一律走 `model_providers_store` 的正式写层
            # （审计也在那一层落）。面板因此**读不到也写不到**
            # `data/model_providers.json`（AGENTS §2.3）。
            model_providers=self.model_providers,
            model_provider_add=self.model_provider_add,
            model_provider_key=self.model_provider_key,
            model_provider_key_remove=self.model_provider_key_remove,
            model_provider_bind=self.model_provider_bind,
            model_provider_remove=self.model_provider_remove,
        )

    async def ui_groups(self) -> tuple[dict[str, object], ...]:
        """面板「群聊」那一页要的列表（2026-10-09 用户："展示云茹所在的所有群聊并展示群名"）。

        **只读、只这一件事**：没有参数（传什么都 `TypeError`——想调别的请走
        `execute_action`，那口过得是核心的闸门与审计）；每一项只有
        `id` / `name` / `dialogue_on` / `memory_on` 四个字段（白名单由
        `tests/test_ui_groups_seam.py` 逐个钉住），没有成员表、没有消息。
        缓存、以及"取失败回落上一次的已知列表"都在 `DialogueEngine.list_groups` 里。
        """

        return await self._require().list_groups()

    def ui_notice(self, group_id: str, text: str, at_user_ids=()) -> dict[str, object]:
        """面板那张卡片里的发送框：把**操作者的话**交给核心（`UiSeams.notice`）。

        **只包一层**：转发到 `DialogueEngine.notice_group`——四条守卫（空文本 / 只许群 /
        只许 `enabled_group_ids` / 3 秒同内容）与审计**全在引擎那一侧**，接缝层不做
        第二套判断（两处各判一半，迟早会分叉）。

        **为什么不在这里 `await` 发送**：这是面板 HTTP 线程直接调的**同步**闭包，
        而发送是运行时那个循环的事。所以只把 `RelayDelivery` 放进 relay 那条队列，
        由既有的 `drain_relay_deliveries()` → `_deliver_relay()` 投递（`_message_loop`
        里每处理一条消息、以及每 5 秒那个焦点节拍各取一次）。面板拿到的 `ok` 是
        "**已经交给核心、排上队**"，不是"对面已经收到"。

        **接缝层拒掉的只有一种**：引擎没装上这个方法（旧核心 / 单测里直接造的引擎）。
        那也如实说，不假装成功。
        """

        notifier = getattr(self._require(), "notice_group", None)
        if not callable(notifier):
            return {"ok": False, "reason": "这一版核心还没有面板通知这条路，先更新核心。"}
        return dict(notifier(group_id, text, at_user_ids))

    # --- 模型供应商（`ui.model_providers*`，2026-10-18）-----------------------
    #
    # 用户原话：「**这里，我要的是添加而非替换**」，以及「**每个agent一把key，一个供应商下
    # 最少两把，用来跟agent一一对应**」。所以面板「模型」那一页拿到的是**一组五口**：
    # 读清单 / 加一套 / 改某一套的几格 key / 切当前 / 删一套——**没有"编辑已有那套的
    # 地址"这一口**，那正是"替换"的形状（地址与 key 要改就删了重加，那是一步明确的动作）。
    #
    # 三条边界都在这里落地，别只记住第一条：
    #
    # 1. **面板拿不到文件**：它不读也不写 `data/model_providers.json`，只调这五个函数
    #    （AGENTS §2.3 的既有硬边界：面板只能调装配点注入的闭包）。
    # 2. **写只走一处实现**：`set_provider` / `upsert_agent` / `remove_provider`——
    #    这一族**不自己拼 JSON**。两个写者两套纪律正是契约 §3.1 作废 B7 的原因
    #    （"脚本写的能生效、面板写的不能"最难查）。
    # 3. **审计在写层落**（`control_audit`，`source="panel"`）：这一层不重复记，
    #    记两笔等于同一件事两条记录（与 `ui_set_group_memory` / `ui_notice` 同一条处理）。

    def _provider_audit(self):
        """写这一族要用那份审计。取不到就 `None`（store 的 `_audit` 对 None 是空操作）。"""

        return getattr(self._require(), "control_audit", None)

    def model_providers(self) -> dict[str, object]:
        """只读清单（见 `_model_providers_view`）：**密钥只有掩码与长度，没有明文**。

        没有文件时返回 `providers: []`——那不是错误（面板第一次打开时文件往往还没建）。
        """

        return _model_providers_view()

    def model_provider_add(self, name: str, base_url: str, keys: object = None,
                           client_type: str = "", models: object = None
                           ) -> dict[str, object]:
        """**添加一套**（`providers.<名字>`）+ 它下面那几把 key（一次落盘）。

        与 store 的 `set_provider()`（那是 upsert）**差一步**：这里先查同名与同地址，
        有一个就**拒绝**。为什么不在 store 里加一个 `add_provider(create_only=True)`：
        脚本那边"同名改地址"是它唯一的改法；而面板这一侧要的恰恰是"绝不覆盖"。
        两边的差别写成一行判断，比在写层多一个开关更不容易被误用。

        `keys` 是**列表**（`[{label?, value}, …]`，"一个key一行"），至少一把：只写地址、
        一把 key 都没有的条目在读侧会被拒（地址与 key 必须成对），那是加了个空壳。
        `models` 是这家**声明的模型名**（面板第三级下拉框的数据源，可空 = 只能手输）。
        """

        from . import model_providers_store as store
        from .model_providers_store import ModelProvidersRejected

        clean = str(name or "").strip()
        snapshot = store.load()
        existing = {str(item).strip().casefold()
                    for item in (getattr(snapshot, "providers", {}) or {})}
        if clean.casefold() in existing:
            return {"ok": False, "reason": (
                f"providers.{clean} 已经有一套了。这里是「添加」，同名**不会覆盖**"
                "（用户 2026-10-18：「我要的是添加而非替换」）——换一个名字再加，"
                "或者先把那一套删掉。要给它加 key，用「密钥」那一块的「加一把」。")}
        url = str(base_url or "").strip().rstrip("/")
        if url:
            same_url = [item for item, spec in
                        (getattr(snapshot, "providers", {}) or {}).items()
                        if str(spec.get("base_url", "") or "").rstrip("/") == url]
            if same_url:
                return {"ok": False, "reason": (
                    f"地址 {url} 已经属于 providers.{same_url[0]}（同一个账号写成两个名字会"
                    "让 402 余额兜底静默失效，用户 U3）。要加 key 就在那一套下面加，"
                    "或者先把那一套删掉。")}
        entries, bad = _provider_key_list(keys)
        if bad:
            return {"ok": False, "reason": bad}
        if not entries:
            return {"ok": False, "reason": (
                "添加一套至少要有一把 key：地址与 key 要成对落进同一个 providers.<名字>。"
                "（只写地址的那一条，读侧会拒掉，等于加了个用不了的空壳。"
                "填一把就够——「不强制最少几把 key」是用户定的口径。）")}
        model_names, bad = _model_name_list(models)
        if bad:
            return {"ok": False, "reason": bad}
        try:
            written = store.set_provider(
                clean, base_url=base_url, models=model_names,
                client_type=str(client_type or "").strip() or None,
                audit=self._provider_audit(), source="panel")
        except ModelProvidersRejected as exc:
            return {"ok": False, "reason": str(exc)}
        written_name = str(written.get("name") or clean)
        added: list[str] = []
        failed: list[str] = []
        for entry in entries:
            try:
                result = store.put_provider_key(
                    written_name, label=entry.get("label", ""),
                    value=entry["value"], audit=self._provider_audit(), source="panel")
                added.append(str(result.get("id") or ""))
            except ModelProvidersRejected as exc:
                failed.append(str(exc))
        note = str(written.get("note") or "")
        result: dict[str, object] = {
            "ok": not failed, "name": written_name,
            "converged": bool(written.get("converged")),
            "key_ids": added, "note": note,
        }
        if failed:
            # **半成功要如实说**：这一套已经加进去了，只是那几把 key 没全落上。
            # 不假装整条失败（那会让人再点一次、撞上"同名已存在"）。
            result["reason"] = (f"providers.{written_name} 已经加上了，但这几把 key 没落上："
                                + "；".join(failed) + "。可以稍后在「密钥」里逐个补。")
            return result
        message = f"已添加 providers.{written_name}（{len(added)} 把 key）"
        if note:
            message += f"（{note}）"
        if model_names:
            message += f"；声明的模型：{'、'.join(model_names)}"
        result["message"] = message
        return result

    def model_provider_key(self, name: str, key_id: str = "", label: object = None,
                           value: object = None) -> dict[str, object]:
        """**加一把 / 改一把** key（`ui.model_provider_key`）。

        两种用途、同一条口：

        - `key_id=""` → **追加一把**（追加到列表末尾；"第一把"因此永远是文件里最上面那把）；
        - `key_id="k2"` → **改那一把**的 `label` / `value`；`None` = 不动那一项
          （面板只拿得到掩码，空输入框就是"没改"，不是"清空"——真要删用
          `model_provider_key_remove`）。

        **不限制最少几把**：删到只剩一把、或者一把都没有都允许删到底（后者只是那几路
        会往 `.env` 回落，面板上会显示"实际用的是 .env 那一层"）。删"还在用"的那把由
        `model_provider_key_remove` 拒。
        """

        from . import model_providers_store as store
        from .model_providers_store import ModelProvidersRejected

        clean = str(name or "").strip().casefold()
        snapshot = store.load()
        providers = dict(getattr(snapshot, "providers", {}) or {})
        if clean not in providers:
            return {"ok": False, "reason": (
                f"文件里没有 providers.{clean or '（空名字）'} 这一套"
                f"（现有的：{'、'.join(sorted(providers)) or '一个都没有'}）。"
                "先在「添加一套」里把这一家加进来，再给它加 key。")}
        try:
            written = store.put_provider_key(
                clean, key_id=str(key_id or "").strip(),
                label=None if label is None else str(label),
                value=None if value is None else str(value),
                audit=self._provider_audit(), source="panel")
        except ModelProvidersRejected as exc:
            return {"ok": False, "reason": str(exc)}
        key = str(written.get("id") or "")
        created = bool(written.get("created"))
        # 改了一把 key 的值 ⇒ 用到它的那几路要重指向（`resolve()` 会拿到新值）。
        touched = [task for task, entry in (getattr(snapshot, "agents", {}) or {}).items()
                   if str(entry.get("provider", "") or "").strip().casefold() == clean
                   and str(entry.get("key", "") or "").strip() == key]
        if not touched:
            # 没被任何一路指明，但它可能是某几路的"第一把"（没写 key 的那些）——
            # 那是"这一家的顺序变了"，最稳的做法是这一套被谁用着就重指谁。
            touched = [task for task, entry in (getattr(snapshot, "agents", {}) or {}).items()
                       if str(entry.get("provider", "") or "").strip().casefold() == clean]
        for task in touched:
            _sync_after_write(task)
        message = (f"已加一把 key（{key}）" if created else f"已更新 key {key}")
        return {"ok": True, "name": clean, "id": key, "created": created,
                "count": int(written.get("count") or 0), "message": message}

    def model_provider_key_remove(self, name: str, key_id: str) -> dict[str, object]:
        """**删一把** key。**还有 `agents.<用途>.key` 指着它就拒绝**（点名哪几路）。"""

        from . import model_providers_store as store
        from .model_providers_store import ModelProvidersRejected

        clean = str(name or "").strip().casefold()
        try:
            removed = store.remove_provider_key(
                clean, key_id=str(key_id or "").strip(),
                audit=self._provider_audit(), source="panel")
        except ModelProvidersRejected as exc:
            return {"ok": False, "reason": str(exc)}
        key = str(removed.get("id") or key_id)
        # 删掉的那把可能是某几路的"第一把"（它们没写 key）⇒ 那几路要重指向。
        for task, entry in (getattr(store.load(), "agents", {}) or {}).items():
            if str(entry.get("provider", "") or "").strip().casefold() == clean:
                _sync_after_write(str(task))
        return {"ok": True, "name": clean, "id": key,
                "count": int(removed.get("count") or 0),
                "message": f"已删掉 providers.{clean} 的 key {key}"}

    def model_provider_bind(self, task: str, provider: object, key: object = None,
                            model: object = None) -> dict[str, object]:
        """**把一路绑成 `provider → key → model`**（`ui.model_provider_bind`）。

        用户 2026-10-18 那句话的落点：「agent那边先选供应商再选对应的key最后选供应商
        提供的模型」。所以这一口收的就正是这三样，缺一不可（除了"解绑"那种情况）。

        `provider` 传空串 = **这一路解开绑定**（回到 `.env` 那套，同一件事的另一个方向）；
        那时 `key` / `model` 一律忽略。

        校验（都在核心这一侧，**不信面板**）：

        - 用途名必须是那六个之一；
        - 供应商必须是内置表或文件里认识的（`UnknownProvider` 的措辞原样给出去）；
        - `key` 必须是**那一家真有的 id**（悬空 id 会让这一路当场没有 key，而日志上
          看着只是"没配"）。
        """

        from . import model_config
        from . import model_providers_store as store
        from .model_providers_store import ModelProvidersRejected

        clean_task = str(task or "").strip().casefold()
        if clean_task not in model_config.TASK_NAMES:
            return {"ok": False, "reason": (
                f"不认识的用途：{task!r}（只有 {'、'.join(model_config.TASK_NAMES)}）")}
        clean_provider = str(provider or "").strip().casefold()
        if not clean_provider:
            try:
                cleared = store.clear_agent(clean_task, audit=self._provider_audit(),
                                            source="panel")
            except ModelProvidersRejected as exc:
                return {"ok": False, "reason": str(exc)}
            _sync_after_write(clean_task)
            _drop_operator_values(operator_config.shared(), _task_legacy_keys(clean_task))
            return {"ok": True, "task": clean_task, "cleared": bool(cleared.get("cleared")),
                    "message": f"{clean_task} 不再由文件管：回到 .env 那套"}
        try:
            base_url = model_config.provider_base_url(clean_provider)
        except model_config.UnknownProvider as exc:
            return {"ok": False, "reason": str(exc)}
        text_model = " ".join(str(model or "").split())
        if not text_model:
            return {"ok": False, "reason": (
                f"{clean_task} 选了供应商 {clean_provider}，就必须一起选一个模型名"
                "（用户 2026-10-18：先选供应商、再选 key、最后选它提供的模型）")}
        text_key = "" if key is None else str(key or "").strip()
        if text_key:
            known = model_config.file_provider_key_ids(clean_provider)
            if not known:
                return {"ok": False, "reason": (
                    f"providers.{clean_provider} 在文件里还没有 key（这一家现在只在 .env 那套"
                    "里）：先在「密钥」里给它加一把，或者换一个 supplier")}
            if text_key not in known:
                return {"ok": False, "reason": (
                    f"providers.{clean_provider} 里没有 id={text_key} 这把 key"
                    f"（它现在有：{'、'.join(known)}）")}
        try:
            written = store.upsert_agent(
                clean_task, provider=clean_provider, model=text_model,
                key_id=(None if key is None else text_key), base_url=base_url,
                audit=self._provider_audit(), source="panel")
        except ModelProvidersRejected as exc:
            return {"ok": False, "reason": str(exc)}
        _sync_after_write(clean_task)
        _drop_operator_values(operator_config.shared(), _task_legacy_keys(clean_task))
        declared = model_config.file_provider_models(clean_provider)
        out: dict[str, object] = {
            "ok": True, "task": clean_task, "provider": str(written.get("provider") or ""),
            "key": str(written.get("key_id") or ""), "model": text_model,
            "model_declared": (text_model in declared) if declared else None,
        }
        message = (f"{clean_task} → {out['provider']}（key "
                   f"{out['key'] or '（第一把）'} / 模型 {text_model}）")
        if declared and text_model not in declared:
            message += "；这个模型名不在它声明的模型里（应当能用，但请确认那一家确实有它）"
        out["message"] = message
        return out

    def model_provider_remove(self, name: str) -> dict[str, object]:
        """删掉一套。**还有用途指着它就拒绝**（store 那边点名哪几路，原因原样递出去）。

        另外补了一道（比用户那三条口径**多出来的**，写在汇报里让人裁决）：
        如果 `.env` 里 `QQBOT_API_PROVIDER` 正声明着它，也拒绝。理由是那个声明会让
        `model_config.resolve()` 认这个名字，删掉之后**走兼容档的那几路当场解析不出来**
        （`UnknownProvider`）——那与"那几路悄悄掉回环境那套"是同一类事故，
        只是这次发生在"解析"而不是"取值"上。

        `QQBOT_API_PROVIDER` 那一判与面板上的"禁用 + 原因"**共用**
        `_provider_remove_blocker()`：两处各写一遍，迟早出现"按钮能点、点了报错"
        或者更坏的"按钮说安全、实际删了出事"。
        """

        from . import model_providers_store as store
        from .model_providers_store import ModelProvidersRejected

        clean = str(name or "").strip().casefold()
        snapshot = store.load()
        agents = dict(getattr(snapshot, "agents", {}) or {})
        users = [task for task, entry in agents.items()
                 if str(entry.get("provider", "") or "").strip().casefold() == clean]
        blocked = _provider_remove_blocker(clean, users)
        if blocked and not users:
            # 只有 `QQBOT_API_PROVIDER` 那一条会走到这里（"还在用"那条由 store 自己拒，
            # 它点名的措辞更具体，见 `remove_provider`）。排在 store 之前说，是因为
            # store 不认识"环境里声明了它"这件事（它只读文件）。
            return {"ok": False, "reason": blocked}
        try:
            removed = store.remove_provider(clean, audit=self._provider_audit(),
                                            source="panel")
        except ModelProvidersRejected as exc:
            return {"ok": False, "reason": str(exc)}
        # **不需要** `_sync_after_write`：能删的前提是没有任何 `agents.<用途>` 指着它，
        # 所以每一路的 `resolve()` 结果一个字都没变（删完再重指向是空操作）。
        return {"ok": True, "name": str(removed.get("name") or clean),
                "message": f"已删掉 providers.{removed.get('name') or clean}"}

    # --- LearnedSeams（她学来的东西：金句 / 黑话）--------------------------
    #
    # 面板要能看、能改这两份数据，但**不许**碰记忆、也不许驱动对话
    # （`AGENTS.md` §2.3）。所以这里只给八个函数，全部**转发到核心那两个 store**：
    # `quote_learning.QuoteStore`（金句那份 JSON）与 `slang_learning.SlangStore`
    # （黑话词条）。**接缝里不存第二份数据**——多一份就迟早分叉。
    # （2026-10-06 加的是第八个：`learned_quote_evidence`。前面七个一个字没动。）
    #
    # 两个 store 都是**懒取**（`getattr(..., None)` 在**调用时**才求值）：
    # `_ui_seams_for()` 是 `build_engine` 里跑的，那时 `engine.quote_profile` 还没装上
    # （金句那份在 `serve` 里随学习 agent 一起接）；写成"装配时取值"会把面板锁死在
    # "启动那一刻有什么"，与"改一个字下一轮就生效"直接冲突。

    def learned_quote_store(self):
        """金句那份 store（`quote_learning.QuoteStore`）。**复用它的读写口**，不另写一套。"""

        profile = getattr(self._require(), "quote_profile", None)
        store = getattr(profile, "store", profile)
        if store is None or not hasattr(store, "effective_sense"):
            return None
        store.reload()
        return store

    def learned_slang_store(self):
        """黑话那份 store（`slang_learning.SlangStore`）。"""

        store = getattr(self._require(), "slang_library", None)
        if store is None or not hasattr(store, "entries_for"):
            return None
        store.reload()
        return store

    def learned_quote_view(self, group_id: str) -> dict:
        """读：某个群的**表情含义表 + 她的老习惯**（含"哪几条是人工改的 / 已停用"）。"""

        from .quote_learning import SENSE_LABELS, note_id

        store = self.learned_quote_store()
        group = _exact_text(group_id)
        if store is None or not group:
            return {}
        table = store.meanings.get(group) or {}
        forced = store.overrides.get(group) or {}
        meanings = []
        for emoji in sorted(set(table) | set(forced), key=lambda key: (len(key), key)):
            sense = store.effective_sense(group, emoji)
            meanings.append({
                "emoji_id": emoji,
                "sense": sense,
                "sense_label": SENSE_LABELS.get(sense, sense),
                "note": store.effective_note(group, emoji),
                "manual": bool((forced.get(emoji) or {}).get("sense")),
            })
        notes = []
        for item in store.notes:
            key = note_id(item.get("when"), item.get("note"))
            notes.append({
                "id": key,
                "kind": _exact_text(item.get("kind")),
                "when": _exact_text(item.get("when")),
                "note": _exact_text(item.get("note")),
                "enabled": not store.note_disabled(key),
                # 反面材料（"她那次这么说被嫌了"）：面板上要看得出来，免得
                # 操作者把它当成"她的老习惯"去核对。
                "avoid": bool(item.get("avoid")),
            })
        return {"group_id": group, "meanings": meanings, "notes": notes,
                "path": str(store.path)}

    def learned_quote_evidence(self, group_id: str, limit: int = 10, offset: int = 0) -> dict:
        """读：**证据**——她那条被点过表情的原文 + 当时的上下文 + 表情 + 结论落到哪条笔记。

        由来（2026-10-06 用户原话）：*"金句，你连原文和上下文都不贴出来怎么审核"*。
        在它之前这一侧只有**加工后的结论**（`quote_view` 的表情含义表 + 笔记），
        没有证据——那样面板上"这个表情在损她"这句话谁也没法判对错。

        **只读**，三个数各自都有上限（`quote_samples` 里的常量），取不到一律回空结构：

        - 数据来自 `chat.jsonl`（**只读**，配对只认 `message_id`）；
        - 结论来自 `QuoteStore`（那份 JSON），**这一层不自己存一份**；
        - 一个群可能很多条：默认最近 `limit` 条（上限 `MAX_EVIDENCE_LIMIT`），
          `offset` 往后翻——**不一次全吐**。
        """

        from .quote_samples import (DEFAULT_EVIDENCE_LIMIT, MAX_EVIDENCE_LIMIT,
                                    collect_evidence)

        group = _exact_text(group_id)
        if not group:
            return {}
        store = self.learned_quote_store()
        if store is None:
            return {}
        try:
            size = int(limit)
        except (TypeError, ValueError):
            size = DEFAULT_EVIDENCE_LIMIT
        try:
            start = int(offset)
        except (TypeError, ValueError):
            start = 0
        size = max(0, min(MAX_EVIDENCE_LIMIT, size))
        start = max(0, start)
        table = store.meanings.get(group) or {}
        forced = store.overrides.get(group) or {}
        chat_log, reply_log = _quote_evidence_paths(self._require())
        entries, total, error = collect_evidence(
            chat_log, reply_log_path=reply_log, group_id=group, limit=size,
            offset=start)
        evidence = []
        for entry in entries:
            row: dict[str, object] = {
                "message_id": entry.message_id,
                "group_id": entry.group_id,
                "at": entry.at,
                "她说的": entry.body,
                "她开口之前群里在说": _evidence_lines(entry.context_before),
                "她说完之后群里接的": _evidence_lines(entry.context_after),
                "被点了": [dict(item) for item in entry.as_data()["被点了"]],
                "meanings": self._evidence_meanings(entry.reactions, table, forced),
                "notes": self._evidence_notes(store, entry.reactions, table, forced),
                "sample_id": entry.sample.sample_id if entry.sample is not None else "",
                "当时的话题": entry.sample.topic if entry.sample is not None else "",
                "对方想干什么": entry.sample.intent if entry.sample is not None else "",
            }
            # 拿不到结论时**如实说为什么**（面板上"没有东西可审"与"还没学"是两回事）。
            if not row["meanings"]:
                row["为什么没有结论"] = ("这个群还没有学过（表情含义表是空的）" if not table
                                        and not forced else "这些表情学习 agent 还没判过")
            evidence.append(row)
        return {"group_id": group, "total": total, "offset": start, "limit": size,
                "evidence": evidence, "error": error, "path": str(store.path)}

    @staticmethod
    def _evidence_meanings(reactions, table: dict, forced: dict) -> list:
        """这一条证据里**每个被点过的表情**在这个群的含义（学过的才给，没学过的不硬凑）。"""

        from .quote_learning import SENSE_LABELS, SENSES

        out = []
        seen: set[str] = set()
        for reaction in reactions:
            if reaction.emoji_id in seen:
                continue
            seen.add(reaction.emoji_id)
            entry = dict(table.get(reaction.emoji_id) or {})
            override = dict(forced.get(reaction.emoji_id) or {})
            if not entry and not override:
                continue
            # 生效方向与 `QuoteStore.effective_sense` 同一套判据：人工覆盖压过模型判的。
            sense = str(override.get("sense") or entry.get("sense") or "")
            if sense not in SENSES:
                continue
            out.append({
                "emoji_id": reaction.emoji_id,
                "sense": sense,
                "sense_label": SENSE_LABELS.get(sense, sense),
                "note": _exact_text(override.get("note") or entry.get("note")),
                "quote": _exact_text(entry.get("quote")),
                "manual": bool(override.get("sense")),
            })
        return out

    @staticmethod
    def _evidence_notes(store, reactions, table: dict, forced: dict) -> list:
        """这些结论**落到了哪条笔记上**（对不上就是空 id——如实标"没落到任何笔记"）。"""

        out = []
        seen: set[str] = set()
        for reaction in reactions:
            if reaction.emoji_id in seen:
                continue
            seen.add(reaction.emoji_id)
            entry = dict(table.get(reaction.emoji_id) or {})
            manual_note = _exact_text((forced.get(reaction.emoji_id) or {}).get("note"))
            key, note = store.note_for_quote(entry.get("quote"))
            if not key and manual_note:
                key, note = store.note_for_quote(manual_note)
            out.append({
                "emoji_id": reaction.emoji_id,
                "id": key,
                "note": note,
                "manual": bool((forced.get(reaction.emoji_id) or {}).get("sense")),
            })
        return out

    def learned_quote_correct(self, group_id: str, emoji_id: str, sense: object,
                              note: object = "") -> dict:
        """改：纠正某个表情在这个群的方向。`sense` 写 `auto`（或"撤掉"那类词）＝撤销覆盖。"""

        store = self.learned_quote_store()
        group = _exact_text(group_id)
        emoji = _exact_text(emoji_id)
        if store is None or not group or not emoji:
            return {}
        # 撤销那一档的取值与 `/super quote <emoji> auto` **共用一份**表：
        # 两处各留一份，迟早出现"命令认得、面板不认得"。
        from .stage3_main import QUOTE_OVERRIDE_AUTO

        wanted = _exact_text(sense).casefold()
        if wanted in QUOTE_OVERRIDE_AUTO:
            store.clear_override(group, emoji)
            return {"group_id": group, "emoji_id": emoji,
                    "sense": store.effective_sense(group, emoji), "cleared": True,
                    "written": store.last_error == ""}
        normalized = store.override_sense(group, emoji, sense, _exact_text(note))
        return {"group_id": group, "emoji_id": emoji, "sense": normalized,
                "cleared": False, "written": store.last_error == ""}

    def learned_quote_note_enabled(self, note_id_value: object, enabled: bool) -> dict:
        """改：停用 / 恢复一条笔记（按**内容指纹**，模型重写多少遍都还停着）。"""

        store = self.learned_quote_store()
        key = _exact_text(note_id_value)
        if store is None or not key:
            return {}
        written = store.set_note_enabled(key, bool(enabled))
        return {"id": key, "enabled": bool(enabled), "written": bool(written)}

    def learned_slang_list(self, group_id: object = None) -> list:
        """读：黑话词条（`group_id` 给空 = 所有群）。"""

        store = self.learned_slang_store()
        if store is None:
            return []
        return store.entries_for(_exact_text(group_id) or None)

    def learned_slang_update(self, group_id: str, word: str, definition: str) -> dict:
        """改：改一个词的释义（记一次修订）。词不存在时**新建一条**（操作者手工加词）。"""

        store = self.learned_slang_store()
        group = _exact_text(group_id)
        term = _exact_text(word)
        if store is None or not group or not term:
            return {}
        if not store.update_definition(group, term, definition, by="面板"):
            return {}
        return store.find(group, term) or {}

    def learned_slang_delete(self, group_id: str, word: str) -> bool:
        """改：删一条词条。"""

        store = self.learned_slang_store()
        if store is None:
            return False
        return bool(store.delete(_exact_text(group_id), _exact_text(word)))

    def learned_slang_mark_wrong(self, group_id: str, word: str, wrong: bool = True) -> dict:
        """改：标错 / 取消标错（只动 `status`，释义与证据一个字不改）。"""

        store = self.learned_slang_store()
        group = _exact_text(group_id)
        term = _exact_text(word)
        if store is None or not group or not term:
            return {}
        if not store.mark_wrong(group, term, bool(wrong)):
            return {}
        return store.find(group, term) or {}

    def learned_seams(self) -> LearnedSeams:
        """八个函数的清单。**多一个都不给**——函数集合本身就是那份契约。"""

        return LearnedSeams(
            quote_view=self.learned_quote_view,
            quote_evidence=self.learned_quote_evidence,
            quote_correct=self.learned_quote_correct,
            quote_note_enabled=self.learned_quote_note_enabled,
            slang_list=self.learned_slang_list,
            slang_update=self.learned_slang_update,
            slang_delete=self.learned_slang_delete,
            slang_mark_wrong=self.learned_slang_mark_wrong,
        )


def _quote_evidence_paths(engine) -> tuple[Path, Path]:
    """证据读口读哪两份日志：`(chat.jsonl, reply.jsonl)`。

    优先用**这次运行真正在写的那一份**（`quote_agent.chat_log_path`，它在
    `_start_quote_learning` 里按 `data_dir` 定下来）；学习 agent 没装
    （开关关掉 / 没有 client）时退到与它同一套口径的默认位置
    （`quote_samples.default_paths()`，走 `chat_log_directory()`）。

    两份日志都可能不存在：`read_rows` 会返回空 + 原因，这一层不抛。
    """

    agent = getattr(engine, "quote_agent", None)
    chat_log = getattr(agent, "chat_log_path", None)
    if chat_log is not None:
        return Path(chat_log), Path(chat_log).parent / "reply.jsonl"
    from .quote_samples import default_paths

    return default_paths()


def _chat_log_path(engine):
    """聊天日志在哪（**只读**，日报数窗口用）。

    取法与 quote 学习那条证据读口完全同一条（`_quote_evidence_paths`）：
    优先这次运行真正在写的那一份，退到同一套口径的默认位置。
    这里**只取路径**：不新建、不轮转、不改内容，也不动 quote 学习那边。
    """

    chat_log, _ = _quote_evidence_paths(engine)
    return chat_log


def _memory_read(engine) -> MemoryRead:
    """写信那条路的**只读**记忆口（见 `memory_readonly.py`）。

    拿的是记忆库那个**文件的路径**（`MemoryStore.path`），不是 `MemoryStore`：
    写信端手上因此没有 `commit` / `purge_records` / `retrieve`（它会写
    `last_used_at`）这些东西——"插件不准碰记忆"由接缝的形状保证，不再靠
    它自己"恰巧只调了两个只读方法"。

    2026-10-12 换的。改之前这个位置递的是 `engine.memory_service`（真 service，
    顺着 `.store` 能拿到整个可写的库）。
    """

    store = getattr(getattr(engine, "memory_service", None), "store", None)
    return MemoryRead(db_path=getattr(store, "path", None))


def _evidence_lines(context) -> list:
    """证据里的上下文：**一条条列出来**，不是压成一段说明。

    `context` 是 `quote_samples` 给的 `({"who":…, "body":…}, …)`；空就是空列表。
    """

    out = []
    for item in context or ():
        who = _exact_text(item.get("who")) or "?"
        body = _exact_text(item.get("body"))
        out.append(f"{who}：{body}" if body else f"{who}：（没说话）")
    return out


def _reporter_sink_for(engine: DialogueEngine):
    """插件把每日汇报器放回核心（`register_reporter` 用它，面板要读它判今天发没发）。"""

    def sink(reporter: object) -> None:
        engine.daily_reporter = reporter

    return sink


def _report_seams_for(engine: DialogueEngine) -> ReportSeams:
    """写日报、写信要用、但**不属于权限**的那几样东西（见 `ReportSeams` 的说明）。

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

    2026-10-06 多了一项 `learned`（她学来的金句 / 黑话）：面板要能看能改这两份数据，
    但那一份**只放七个函数**、只碰这两份 JSON——记忆入口、对话入口一个都不给，
    函数清单由 `tests/test_learned_seams.py` 逐个钉住。
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


def _reply_endpoint() -> tuple[str, str, str]:
    """回复 agent 这一套 `(base_url, api_key, model)`。

    **每个 agent 各用各的**（2026-10-05 用户要求），而现在这件事有了来源：
    `model_config.resolve("reply")` —— 地址与 key **成对取自同一条 key 条目**
    （reply 那条绑 mimo）。判定 / 记忆 / 审核 / 写信各走自己那条。

    没配 `QQBOT_REPLY_API_*` 时它返回的仍是全局那一套，**逐字不变**。
    """

    return _endpoint_of("reply").triple()


def _endpoint_of(task: str):
    """某个用途**当值**解析出来的"一个客户端"（缺 key 时 `MissingCredential` 明确抛出）。

    这是**唯一**允许构造模型客户端来源的地方：地址、key、模型名由
    `model_config` 从同一条 key 条目解析出来，所以"用 A 家 key 打 B 家地址"
    在这里没有落点。

    2026-10-13 起它多了一层：主家余额耗尽（HTTP 402）时 `provider_failover`
    会把**所有用途**改指到兜底那一家（mimo）。但那一层只是"换一条同样成对的
    来源"——地址与 key 仍然由上一条 key 条目一起给出，`model_config` 那条
    不变式一个字都没松。**primary 模式下它与 `model_config.require(task)`
    逐字相同**（`tests/test_provider_failover.py` 钉着这条）。
    """

    return provider_failover.endpoint_for(task)


def _dialogue_client_factory(session_id: str) -> OpenAICompatibleClient:
    # 2026-10-13（用户拍板："超时只管首字，首字超时才跳过，不要重试，重试还不如干等着，
    # 所有 agent 都一样"）：这个数**不是总上限**，而是"连着这么久一个字节都不吐"
    # 才认连接死了的**停顿网**。首字那 3 秒由 `llm_client.STREAM_STALL_SECONDS` 单独管；
    # 首字来了就让她说完（旧口径的 12 秒是给**非流式**整段用的，会砍掉"一直在流"的长回复）。
    # 30 秒远大于实测的块间最大间隔（2026-10-13 实测 0.64s），只有连接真死了才会碰到。
    reply_timeout = dev_config.api_timeout("reply", default=30.0)
    endpoint = _endpoint_of("reply")
    client = OpenAICompatibleClient(
        *endpoint.triple(),
        provider=endpoint.provider,
        user_id=dev_config.session_user_id(dev_config.DIALOGUE_USER_ID, session_id),
        usage_store=_USAGE_STORE,
        usage_role="dialogue",
        # 余额兜底那一档更宽（mimo 慢一个量级），由 `provider_failover` 集中决定。
        timeout=provider_failover.effective_timeout("reply", reply_timeout),
    )
    # 切回主家时要把这道闸还回原值（兜底那一档是临时放宽的）。
    provider_failover.remember_primary_timeout(client, reply_timeout)
    return client


def _judge_client_factory(session_id: str) -> OpenAICompatibleClient:
    # 2026-10-13 起这个数的**含义变了**（用户拍板："超时只管首字，首字超时才跳过，
    # 不要重试，重试还不如干等着，所有 agent 都一样"）：它不是"整段生成的预算"，
    # 也不是总上限，而是**停顿网**——首字 3 秒不来就由
    # `llm_client.STREAM_STALL_SECONDS` 把这一轮放掉（判定不出声）；首字来了之后，
    # 只有"连着这么久一个字节都不吐"才当连接死了。
    #
    # 为什么不再是 5 秒那种总上限：旧口径下它砍在 3.88s 中位数附近，34% 的判定是
    # "生成被砍掉、白付一次时间还不算数"（2026-10-12 实测：成功判定中位 2.92s）。
    # 现在只要一直在输出就不会被砍。20 秒远大于块间最大间隔（2026-10-13 实测 0.64s），
    # 兜的是"连接活着但永远说不完"；真碰到就是这一次判定不出声（fail-closed）。
    # 当天另有一件事已经被别的改法解决：那 3 条 50.4/49.2/54.4s 的判定是
    # "23s 长生成失败 + 又重试一轮"，现在**默认不重试**（`DEFAULT_MAX_ATTEMPTS = 1`）。
    judge_timeout = dev_config.api_timeout("judge", default=20.0)
    endpoint = _endpoint_of("judge")
    client = OpenAICompatibleClient(
        *endpoint.triple(),
        provider=endpoint.provider,
        user_id=dev_config.session_user_id(dev_config.JUDGE_USER_ID, session_id),
        usage_store=_USAGE_STORE,
        usage_role="judge",
        timeout=provider_failover.effective_timeout("judge", judge_timeout),
    )
    provider_failover.remember_primary_timeout(client, judge_timeout)
    return client


def _build_judge_client(usage_store=None):
    """判定 agent 的 client；未启用双 agent 模式、或没有可用的 key 时返回 None。"""

    if not dev_config.DUAL_AGENT_ENABLED:
        return None
    from .model_config import MissingCredential

    try:
        endpoint = _endpoint_of("judge")
    except MissingCredential as exc:
        logger.warning("判定 agent 缺少凭据，本次不启用：%s", exc)
        return None
    return OpenAICompatibleClient(
        *endpoint.triple(),
        provider=endpoint.provider,
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

    默认**不开**：它给每条回复多加一次调用（实测 +0.6~0.9 秒、约 360 token）；
    判不过还要**多一次重写**（`stage3_main._rewrite_after_review`）。所以说它
    "贵一点"不是随口说的——只有真要看这道判断时才开。
    开了之后每次打回都会记一行 INFO，方便回头核对它到底有没有用。

    **2026-10-05 起它只判不改**（用户："审核只负责打回，不负责修改"）：
    它不再返回改写后的文本，正文永远出自回复 agent 自己。
    """

    # 判据是**函数**（2026-10-18，契约 D15）：import 期常量会让"文件里给了 review 的 key"
    # 永远不生效——配了却永远不生效是最坏的一种。
    if not dev_config.review_enabled():
        logger.info("风格审核未启用（QQBOT_STYLE_REVIEW=0 / 文件与 .env 都没有 review 的 key）")
        return None
    from .model_config import MissingCredential

    try:
        endpoint = _endpoint_of("review")
    except MissingCredential as exc:
        logger.warning("风格审核已开启但没有任何可用 key，本次不启用：%s", exc)
        return None
    if not endpoint.api_key:
        logger.warning("风格审核已开启但没有任何可用 key，本次不启用")
        return None
    logger.info("风格审核已启用：每条回复发送前看一眼（只判不改，判不过就重写一次）")
    return StyleReviewer(
        OpenAICompatibleClient(
            *endpoint.triple(),
            provider=endpoint.provider,
            user_id=dev_config.REVIEW_USER_ID,
            usage_store=usage_store if usage_store is not None else _USAGE_STORE,
            usage_role="review",
            # 审核这一路的 **停顿网**（首字那 3 秒由 `STREAM_STALL_SECONDS` 管，
            # 不再有"整趟总上限"；见 style_reviewer.REVIEW_TIMEOUT_SECONDS 的注释）。
            timeout=REVIEW_TIMEOUT_SECONDS,
        )
    )


def _build_letter_client() -> OpenAICompatibleClient:
    """写信 agent 的 client：独立 user_id（默认 `qqbot-letter`）。

    为什么单独一份：信的 prompt 前缀又大又恒定（人设 + 信件规矩 + 已确定事实），
    跟群聊共用一份缓存隔离空间只会互相挤；分开之后它的命中率与成本都能单独看。

    凭据走 `model_config` 的 **letter** 那个用途。2026-10-12 起那一行借的是
    `judge` 那条（deepseek / `deepseek-v4-pro`）——与 reply 同一个形状：模型的哪家
    就借哪家的 key。它自己那把 `QQBOT_LETTER_API_KEY`（mimo）留着不动，主家余额
    耗尽切兜底时由 `FALLBACK_TASKS` + `FALLBACK_KEY` 借走（见 `model_config`）。

    留在核心（而不是跟着装配搬去 `background_plugins.py`）：它要往**核心的用量账本**
    （`_USAGE_STORE`）里记一笔，那是核心状态。
    """

    endpoint = _endpoint_of("letter")
    return OpenAICompatibleClient(
        *endpoint.triple(),
        provider=endpoint.provider,
        user_id=dev_config.MAIL_USER_ID,
        usage_store=_USAGE_STORE,
        usage_role="letter",
    )


def _build_group_actions(engine: DialogueEngine, registry, usage_store) -> None:
    """把插件登记的群动作**工厂**变成 `engine.group_actions`（`{分组名: 执行函数}`）。

    由来（2026-10-06 外部审查实测的那处耦合）：`stage3_main.execute_action` 原来直接
    `from .plugins.group_admin.group_admin import execute`——核心**按插件模块名**找执行
    函数。把那个文件夹改个名字，`discover()` 照样说它装上了（`loaded` 里有它），
    而那条命令**静默**回一句"这条部署没有群管理能力"：改名 = 丢功能，且没有任何测试
    会红。现在执行函数由插件用 `registry.provide_group_action("<分组名>", 工厂)` 放上来，
    核心只按 `ActionRequest.group` 取。

    形状（与识图那条 `registry.vision` 同一套）：登记的是**工厂**
    `(usage_store) -> execute`，因为执行函数要用核心的用量账本记 API 用量，
    而账本在装配时才现造（`ApiUsageStore`）。插件侧不碰那个账本，只收一个只读引用。

    降级：没有插件（或它没登记）时 `engine.group_actions` 就是空字典，
    `execute_action` 回那句 fail-closed 的文案，其余一切照旧。
    """

    taker = getattr(registry, "group_action", None)
    if not callable(taker):
        engine.group_actions = {}
        return
    provided = getattr(registry, "provided_group_actions", None)
    names = provided() if callable(provided) else ()
    built: dict[str, object] = {}
    for name in names:
        factory = taker(name)
        if not callable(factory):
            continue
        try:
            built[str(name)] = factory(usage_store)
        except TypeError:
            # 更窄的实现可能不吃账本；退一步再试一次（同 `ReportSeams.log_io` 的规矩）。
            built[str(name)] = factory()
    engine.group_actions = built


def _notice_target(target: object) -> MessageTarget:
    """把插件递进来的"发给谁"统一成 `MessageTarget`。

    只收两种形状（**刻意不收裸字符串**）：

    * `MessageTarget`——插件可以 `from ...transport import MessageTarget`；插件 import
      核心的类型在这一族里已有先例（`remind` 用核心的 `RemindDelivery`、
      `timeout_alert` 用核心的快照口）。
    * 映射 `{"group_id": "…"}` / `{"user_id": "…"}`——订阅列表这种从 JSON 读出来的
      配置直接用得上，不必为了一个目标去 import 核心类型。

    为什么不收 `"800000001"`：群号与 QQ 号都是纯数字，这里**猜不出来**。猜错的代价是
    "通知发到了另一个人/另一个群"，那比不发更糟，而且发出去就收不回来。所以宁可抛
    `ValueError`，让写插件的人在第一次跑的时候就看到。
    """

    if isinstance(target, MessageTarget):
        return target
    if isinstance(target, dict):
        group_id = target.get("group_id")
        user_id = target.get("user_id")
        if group_id not in (None, ""):
            return MessageTarget(group_id=str(group_id))
        if user_id not in (None, ""):
            return MessageTarget(user_id=str(user_id))
    raise ValueError(
        "notify 的目标要是 MessageTarget，或 {'group_id': …} / {'user_id': …} 这样的映射；"
        f"收到的是 {type(target).__name__}（裸字符串在这里认不出是群号还是 QQ 号）"
    )


def _record_notice(engine, target: MessageTarget, text: str) -> None:
    """把一条**已经发出去的通知**记进目标会话的历史（`speaker="notice"`）。

    三条边界：

    1. **只动历史**。出站那一步已经用原文发完了；这里洗的只是"进历史的那一份"
       （`notice_text.for_history`：去 CQ 码/@、脱敏、截断 200 字）。
    2. **失败不许冒泡**：记历史失败不能把"已经发出去"变成插件眼里的异常
       （插件会以为自己没发成、再发一遍）。
    3. **会话状态照常建**：`DialogueSessions.state()` 会新建一个空会话。这不是
       "顺手多记一点"——通知本来就该在她下次开口时看得见；而"新建会话会让群背景
       补不进来"那件事在 `_seed_history` 里另有判据（通知不算"有人说过话"）。
    """

    sessions = getattr(engine, "sessions", None)
    state_of = getattr(sessions, "state", None)
    if not callable(state_of):
        # 最小引擎替身（测试里的 `_Recorder` 那种）没有会话表：什么都不记，也不炸。
        return
    cleaned = notice_text.for_history(text)
    if not cleaned:
        return
    from .chat_log import session_id_of

    session_id = session_id_of(target)
    clock = getattr(engine, "clock", None)
    now = float(clock()) if callable(clock) else time.time()
    try:
        state_of(session_id).record_notice(session_id, target, cleaned, now)
    except Exception:  # noqa: BLE001 - 记历史失败不许影响出站
        logger.warning("plugin notice history skipped session=%s", session_id, exc_info=True)


def _plugin_action_seams(engine: DialogueEngine, transport: QQTransport):
    """给后台插件注入的两个窄接缝：`call_action` 与 `notify`。

    **权限与协议都留在核心**（2026-09-30 用户："入群审批也是插件"）：

    - `call_action(action, params)`：先过 `capabilities` 闸门（入群审批只批
      `purpose="join_approval"` 那一条），再调对面，回执拆成 `data` 返回；
      失败抛异常（`onebot_ws.call_api` 已经把 retcode≠0 变成异常了）。
      插件因此不需要知道回执形状，也绕不开闸门。
    - `notify(target, text)`：通知——**发出去，并且记进那个会话的历史**
      （`speaker="notice"`，见 `_SeamBinder.notify`）。老写法 `notify(text)` 仍然可用，
      收件人是超管。连接断了也不会把这条提示弄丢（走补发队列）。

    她们（后台插件）拿到的就是这两个函数，拿不到 transport 本身。

    ⚠️ 经由 `_SeamBinder`：闭包只捕获不透明令牌。第一版这两个闭包**直接捕获了
    `engine` 与 `transport`**，审查者用 `registry.notify.__closure__[1].cell_contents`
    就拿到了**活的传输层**并真的发了消息（2026-10-01）。
    """

    binder = _SeamBinder(engine, transport)
    return binder.call_action, binder.notify


def _action_caller_for(engine: DialogueEngine, transport: QQTransport):
    """`(purpose) -> 已过闸门的调用函数`。核心执行插件声明的动作时用它。

    2026-10-06：`build_engine` 改成直接持有 `_SeamBinder(...).action_caller`（它还要把
    同一个绑定给角色事实服务用，见 `_LazyGroupRoles`），所以这里现在**没有调用点**。
    留着是因为它就是"核心要一个已过闸门的调用函数"这件事的现成写法，
    下一处需要的人直接调它比再写一遍 `_SeamBinder` 好——**它不 import 任何可选能力**，
    不影响任何一条判据。
    """

    return _SeamBinder(engine, transport).action_caller


#: `group_roles.ROLE_UNKNOWN` 的**字面**副本。
#:
#: 为什么不在降级分支里 `from .group_roles import ROLE_UNKNOWN`：那个 import 正是
#: "模块不在时会炸"的那一句——降级路径去 import 它，等于把懒构造白做了。
#: 代理只需要这一个字符串，抄一份字面量比多一条 import 边便宜。
_ROLE_UNKNOWN = "unknown"


class _LazyGroupRoles:
    """**懒构造**核心的角色事实服务（`group_roles.GroupRoles`）。

    ## 为什么要懒构造（不是洁癖，是判据）

    `AGENTS.md` 的判据是"**删掉它，Stage 3 照样跑得起来**"，而 `check_module_removal`
    探的正是 `build_engine()` 这一句。所以构造点必须满足两条：

    1. **不在 `build_engine` 顶层 import 它**（否则删掉模块 → `ModuleNotFoundError`）；
    2. 模块真的不在时，构造失败要**当场被兜住**、并且降级到"一律不知道"。

    两条合起来的结果：模块被拿掉时，引擎照常装配起来，只是每个角色都是 `unknown`
    ——那是 fail-closed 的那一侧，不是"炸掉"或"猜一个"。

    ## 为什么 `ensure()` 与 `ready` 是两个口

    名册要在**同步**渲染里读角色，所以服务必须能"先异步取一次、再同步读很多次"：

    - `await ensure()`：把服务造出来（造过就直接返回），拿不到就 `None`；
    - `ready`：**同步**取已经造好的那一个，没造过就是 `None`（绝不在同步上下文里
      触发 import 或网络）。

    代理方法只做转发；**没有 `ready` 时那些方法名仍然在**（`role` / `is_owner` …），
    所以调用方不需要写 `if 服务在不在`——照常 `await`，拿到的是 `unknown` / `False`。
    """

    __slots__ = ("_engine", "_transport", "_service")

    def __init__(self, engine: DialogueEngine, transport: QQTransport) -> None:
        self._engine = engine
        self._transport = transport
        self._service = None

    @property
    def ready(self):
        """已经造好的服务；没造好就是 `None`。**同步、不触发 import。**"""

        return self._service

    async def ensure(self):
        """造出服务并返回它；这台机器上没有它时返回 `None`（只报告一次）。"""

        if self._service is not None:
            return self._service
        try:
            from .group_roles import GroupRoles

            caller = _SeamBinder(self._engine, self._transport).action_caller("read")
            from . import dev_config

            self._service = GroupRoles(caller, ttl=dev_config.SELF_ROLE_TTL_SECONDS)
        except Exception as exc:  # noqa: BLE001 - 没有它不该让对话或装配炸掉
            logger.warning("group_roles_unavailable category=%s", type(exc).__name__)
            return None
        return self._service

    # --- 转发：没造好时一律 fail-closed ----------------------------------

    async def role(self, group_id: str, user_id: str = "", **kwargs) -> str:
        service = await self.ensure()
        if service is None:
            return _ROLE_UNKNOWN
        return await service.role(group_id, user_id, **kwargs)

    async def self_role(self, group_id: str, **kwargs) -> str:
        service = await self.ensure()
        if service is None:
            return _ROLE_UNKNOWN
        return await service.self_role(group_id, **kwargs)

    async def is_owner(self, group_id: str, user_id: str = "") -> bool:
        service = await self.ensure()
        return False if service is None else await service.is_owner(group_id, user_id)

    async def at_least_admin(self, group_id: str, user_id: str = "") -> bool:
        service = await self.ensure()
        return False if service is None else await service.at_least_admin(group_id, user_id)

    async def prime(self, group_id: str, user_ids, **kwargs) -> None:
        service = await self.ensure()
        if service is not None:
            await service.prime(group_id, user_ids, **kwargs)

    def cached_self_id(self) -> str:
        """**同步**取已经查到过的那份；服务还没造出来时是空串。"""

        service = self._service
        if service is None:
            # 服务还没造出来 = 还没查过一次。**这里不能顺手把服务造出来**：
            # 这是个同步口（面板的 `self_id` 接缝），它一 await 就把调用方拖下水。
            # 空串本来就是那个接缝认得的一档（"还没有 self_id"）。
            return ""
        return service.cached_self_id()

    def labels_for(self, group_id: str, user_ids) -> dict[str, str]:
        """**同步**读缓存：`{user_id: 角色说明}`，只含缓存里已有的。

        没造好服务时是空表——渲染方按"不知道"处理（名册上就不写角色），
        绝不填一个默认角色。
        """

        service = self._service
        if service is None:
            return {}
        return service.labels_for(group_id, user_ids)

    def known_role(self, group_id: str, user_id: str) -> str:
        """缓存里已知的角色；没有就是 `unknown`（同 `labels_for`，同步、不发查询）。"""

        service = self._service
        if service is None:
            return _ROLE_UNKNOWN
        return service.known_role(group_id, user_id)

    def forget(self, group_id: str = "", user_id: str = "") -> None:
        service = self._service
        if service is not None:
            service.forget(group_id, user_id)


def _memory_endpoint():
    """记忆服务的凭据来源：`model_config` 的 **memory** 那个用途。

    没有可用的 key → 返回 `None`（调用方照旧"仅启用命令"）。判据仍然是**那把 key**
    （见 `tests/config_support.py` 里那段说明），只是它现在从三层配置里解析出来，
    而不是直接读某个常量——这样地址与 key 一定同家。
    """

    from .model_config import resolve

    endpoint = resolve("memory")
    if endpoint.api_key:
        return endpoint
    logger.warning(
        "未设置 %s（也没配同家回落项）；仅启用命令，普通对话和长期记忆维护暂不可用",
        endpoint.key_env,
    )
    return None


def _start_memory(engine: DialogueEngine, client) -> MemoryService | None:
    """长期记忆服务。未配置 key 时返回 None，对话照常。"""

    endpoint = _memory_endpoint()
    if endpoint is None:
        return None
    settings = MemorySettings.from_environment()
    maintenance_client = OpenAICompatibleClient(
        *endpoint.triple(),
        # 记忆维护是**后台任务**，本来就不吃回复/判定那两道短闸（它的闸由
        # `settings.model_timeout` 给），所以这里只跟着换来源，不动闸。
        provider=endpoint.provider,
        timeout=settings.model_timeout - 1,
        max_tokens=4096,
        user_id=dev_config.MEMORY_USER_ID,
        usage_store=_USAGE_STORE,
        usage_role="memory",
    )
    # **记忆开放的群**，不是"开对话的群"：`engine.memory_group_ids` 由
    # `/super enable|disable memory`（在哪个群说就动哪个群）维护，与 `/admin enable|
    # disable` 那张对话名单互不相干（2026-10-08 用户："我要的是**记忆**不是对话"）。
    # 全局停用（`engine.enabled`）时一份都不给：那种状态是"整个 bot 停了"。
    allowed = lambda: set(engine.memory_group_ids) if engine.enabled else set()
    memory = MemoryService(maintenance_client, allowed, settings=settings,
                           feature_logs=engine.logs)
    engine.memory_service = memory
    # 记忆维护用的是**自己的 key**，命中率要单独算，所以把这个 client 记在引擎上。
    engine.memory_client = maintenance_client
    return memory


def _build_slang_learning(engine: DialogueEngine):
    """黑话词条的**装配**：读/写口（`engine.slang_library`）+ 被动观察者。

    返回 `(store, watcher)`；关掉开关（`QQBOT_SLANG_LEARN=0`）时 `watcher` 是 `None`，
    但 `store` **照样给**——"关掉"应当表现为"不再记新的"，而不是"面板上那份数据也没了"
    （与金句那边"开关关掉时读侧照样在"同一条纪律）。

    任何失败都降级成"没有观察者"：她该说的话一个字都不少。
    """

    try:
        from .slang_learning import SlangStore, SlangWatcher, learning_enabled

        store = SlangStore()
    except Exception:  # noqa: BLE001 - 学不学黑话绝不能挡住启动
        logger.warning("黑话词条未装配", exc_info=True)
        return None, None
    if not learning_enabled():
        logger.info("黑话捕获未启用（QQBOT_SLANG_LEARN=0）")
        return store, None
    # `groups` 给的是**闭包**：她这会儿在听哪些群，是运行期会变的事实
    # （`/admin enable|disable` 随时改），不能在这里取一次就固定下来。
    watcher = SlangWatcher(
        store, groups=lambda: set(engine.enabled_group_ids) if engine.enabled else set(),
    )
    logger.info("黑话捕获已挂上：写盘 %s，日志 %s", store.path, watcher.log_path)
    return store, watcher


#: 金句学习那条 agent 的输出预算（token）。**不能跟记忆维护共用 4096**——由来见
#: `_quote_learning_client`。
QUOTE_LEARNING_MAX_TOKENS = 8192


def _quote_learning_client(engine):
    """金句学习 agent 的 client：与记忆维护**同一条 key 与模型**，但**输出预算单独放大**。

    2026-10-13：把一批从 8 条样本提到 12 条之后，实测连着两次
    `quote_learning_failed category=LLMError`——原因就是记忆维护那个 `max_tokens = 4096`
    被**思考**吃光、正文一个字没吐（`llm_client` 里 `empty_reply` 那条注释写的就是这个形状：
    "例如 max_tokens 光被思考吃光"）。输出的 JSON 本身才 ~1.2k token，8192 给的是推理的余量。
    """

    endpoint = _memory_endpoint()
    if endpoint is None:
        # 没有记忆端点就退到引擎手上那一份（照旧能学，只是预算归它管）。
        return getattr(engine, "memory_client", None) or getattr(engine, "client", None)
    return OpenAICompatibleClient(
        *endpoint.triple(),
        provider=endpoint.provider,
        timeout=MemorySettings.from_environment().model_timeout - 1,
        max_tokens=QUOTE_LEARNING_MAX_TOKENS,
        user_id=dev_config.MEMORY_USER_ID,
        usage_store=_USAGE_STORE,
        usage_role="memory",
    )


def _start_quote_learning(engine: DialogueEngine, *, allowed_groups):
    """金句学习 agent（`quote_learning.py`）：**定期**学"她怎么说、什么场合说什么"。

    三条装不上的情况都**静默降级**，对话一个字都不受影响：

    - `QQBOT_QUOTE_LEARN=0`（或配置里关掉）→ 不装；
    - 没有可用的 client → 不装；
    - 它的节拍任务起不来 → 只是不学，回复路径那边读到的是空材料。

    返回 `(agent, task)`；装不上时 `(None, None)`。**它绝不碰对话**：
    唯一的接缝是 `engine.quote_profile`（读一份本地 JSON 挑几条材料），
    模型调用只在它自己那条定期节拍里发生（`run()` 里 `sleep(interval)`）。
    """

    from .quote_learning import QuoteProfile, QuoteStore, build_quote_agent

    client = _quote_learning_client(engine)
    # 表情的名字表要从本机 SnowLuma 的只读接口拉（`fetch_sys_faces`，动作名与契约在
    # `data/snowluma_actions.json` 里，`capabilities.py` 的白名单里也有）。没配地址就是
    # `None`：那样照旧学，只是样本里少一个名字字段。
    snowluma = None
    if dev_config.SNOWLUMA_HTTP_BASE_URL:
        snowluma = SnowLumaHttpClient(
            dev_config.SNOWLUMA_HTTP_BASE_URL, dev_config.SNOWLUMA_ACCESS_TOKEN)
    agent = build_quote_agent(client, allowed_groups=allowed_groups, snowluma=snowluma)
    # **无论装没装上，读侧都要接上**：开关关掉时它照样存在，只是 `quote_enabled`
    # 让 `DialogueEngine._style_material` 每次返回空串——"关掉＝与改动前逐字相同"
    # 这条由**开关**守（有测试），不由装配守（那会让"关掉"变成另一种代码路径）。
    store = agent.store if agent is not None else QuoteStore()
    engine.quote_profile = QuoteProfile(store)
    # **"刚才有人给她贴了什么"的读口**（2026-10-11，用户选的"甲"）：回复路径上只读
    # 对话日志的**尾巴**（有界、纯本地、零模型调用），把"刚才有人贴 ㊗"变成材料。
    # 它不依赖学习那条节拍跑没跑过：含义表读的是同一份 JSON（`quote_profile`）。
    from .quote_samples import ReactionFeed, default_paths

    chat_log = agent.chat_log_path if agent is not None else default_paths()[0]
    engine.quote_reactions = ReactionFeed(chat_log)
    if agent is None:
        logger.info("金句学习未启用（QQBOT_QUOTE_LEARN=0 或没有可用的模型 client）")
        return None, None
    engine.quote_agent = agent
    return agent, asyncio.create_task(agent.run(), name="quote-learning")


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
    # 余额探针：**核心自己的**一条后台节拍，不是插件（它不依赖 `plugins/` 目录）。
    # 它只在"已经切到兜底那一家的模式"下才真的发那次请求；primary 模式下
    # `poll_once()` 立刻返回，一次网络都不打。走既有节拍循环，不新起 `while True`。
    balance_probe = provider_failover.BalanceProbe()
    background_tasks.append(
        asyncio.create_task(run_background_plugin(balance_probe),
                            name=f"plugin-{balance_probe.name}")
    )
    # 金句学习：**独立的一条节拍**（一天一次级别），不在插件那条循环里。
    # 它是 Stage 3 的事（学的是"她怎么说、什么场合说什么"，直接进回复 prompt 的 DATA 段），
    # 所以不按 Stage 4 插件包装；装配与降级见 `_start_quote_learning`。
    quote_agent, quote_task = _start_quote_learning(
        engine, allowed_groups=lambda: set(engine.enabled_group_ids) if engine.enabled else set())
    if quote_task is not None:
        background_tasks.append(quote_task)
        logger.info("金句学习已挂上节拍：样本来自 %s，落盘 %s",
                    quote_agent.chat_log_path, quote_agent.store.path)
    connection_task = asyncio.create_task(
        # 掉线事件的广播对象在**这里**造：`build_engine` 已经把插件装好了，
        # 所以插件经 `registry.link` 登记的接收者此刻就在名单里（没有插件就是空的）。
        _watch_connection(transport, _disconnect_notifier_for(engine)),
        name="connection-watchdog",
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
# **日志节奏**（秒）：掉线期间那条告警**至多**这么频繁地打一条。
# 名字没改（"warn every"本来就是"隔多久再提醒一次"）；但它现在**只管日志**了——
# 循环周期不再等于它，见下面那个常量（2026-10-06）。
CONNECTION_WARN_EVERY_SECONDS = 600.0
# **检测节奏**（秒）：看门狗多久采一次 `transport.connected`、喂一次掉线状态机。
#
# 为什么要跟日志节奏分开（2026-10-06 用户）：*"用户要的是『断了就告诉我』，
# 不是『断了十分钟后告诉我』"*。原来循环只有 `sleep(600 秒)` 一个节奏，于是
# **掉线最坏 10 分钟才被发现**。现在采样快、日志照旧：
#
# | 事 | 节奏 | 常量 |
# | --- | --- | --- |
# | 采样 + 喂状态机（检测掉线、广播） | ≤60 秒 | 本常量 |
# | 那条告警日志 | 10 分钟 | `CONNECTION_WARN_EVERY_SECONDS` |
#
# **不许再拿它当循环周期以外的东西用**，也不许把两个节奏并回一个名字。
CONNECTION_DETECT_EVERY_SECONDS = 60.0


class _ConnectionTicker:
    """**一次采样**：该不该打那条告警，以及把状态喂给掉线状态机。

    为什么单独成一件东西：检测与告警**是两个节奏**（见上面两个常量的说明）。
    把两件事写在一个 `if` 里，下一次改动很容易又并回去——分成"一次 tick"之后，
    "多久 tick 一次"（`_watch_connection` 的 sleep）与"多久打一条日志"
    （这里按最后一个日志点的时钟判定）各自只有一个地方。

    ## 两个节奏的具体口径

    * **检测**：每次 `tick()` 都喂 `_DisconnectNotifier.observe(connected)`，
      与日志打没打无关。所以"断了"最多晚一个检测周期（≤60 秒）被广播；
    * **日志**：**日志点**由时钟推进（第一次就是启动后 `CONNECTION_WARN_AFTER_SECONDS`
      那一次检查，与改动前同刻；之后每 `CONNECTION_WARN_EVERY_SECONDS`
      **推进一次**，不管当时连没连上），到点时若没连上就打那一条。于是
      "掉线期间日志在哪几秒打"**与改动前逐条一致**——它只是不再兼职当采样周期。

    `clock` 可注入：测试用假时钟把时间推着走，不必真的等 60 秒。
    """

    __slots__ = ("_notifier", "warn_every", "detect_every", "_clock", "_last_log_slot")

    def __init__(self, notifier: _DisconnectNotifier | None = None, *,
                 warn_every: float | None = None,
                 detect_every: float | None = None,
                 clock=None) -> None:
        self._notifier = notifier
        # 缺省值**在这里现读模块常量**，不写成参数默认值：参数默认值在函数**定义时**
        # 就绑定了，那样测试改 `CONNECTION_*` 常量完全不生效（写这版时踩到过）。
        self.warn_every = float(
            CONNECTION_WARN_EVERY_SECONDS if warn_every is None else warn_every)
        #: 循环该多久 tick 一次（`_watch_connection` 读它睡）。放这里是为了让
        #: "检测节奏"只有**一个**来源：常量 → ticker → sleep。
        self.detect_every = float(
            CONNECTION_DETECT_EVERY_SECONDS if detect_every is None else detect_every)
        self._clock = clock if clock is not None else time.monotonic
        # 让**第一次** tick 就是日志点（改动前：启动后 60 秒那一次检查就打）。
        self._last_log_slot = self._clock() - self.warn_every

    async def tick(self, transport: object) -> bool:
        """采一次样。返回**这一次打没打那条告警**（测试用；调用方不必依赖）。

        `transport` 只用到 `connected`（取不到就按"连着"——与改动前同一处理）。
        """

        connected = bool(getattr(transport, "connected", True))
        due = self._log_slot_due()
        warned = False
        if due and not connected:
            logger.warning(
                "还没有 OneBot 客户端连上来（%s 秒）：消息进不来，她不会说话。"
                "常见原因是 QQ 客户端（NapCat）先于 bot 启动、之后没有重试——"
                "把它重开一次即可；开机顺序问题见 README「开机自启动」。",
                int(CONNECTION_WARN_AFTER_SECONDS),
            )
            warned = True
        if self._notifier is not None:
            # **每次都喂**（不只掉线那几次）：重连也是靠这里看到的，
            # 而"重新武装"正是下一次掉线能再通知一次的前提。
            await self._notifier.observe(connected)
        return warned

    def _log_slot_due(self) -> bool:
        """到下一个日志点了吗。**每次 tick 都要问**——日志点由时钟推进，与状态无关。"""

        now = self._clock()
        if now - self._last_log_slot < self.warn_every:
            return False
        self._last_log_slot = now
        return True


class _DisconnectNotifier:
    """把"在线 → 掉线"这个**边沿**变成一次广播。**一次掉线只广播一次。**

    由来（2026-10-06 用户）：*"掉线可以调用 mail 插件给我发消息通知我"*、
    *"断一次只发一次，不要反复调用"*、*"bot 本身稳定性我认为是很可靠的，
    不用额外通知"*。本体侧只做这一件事：**把已有的状态变成确定的边沿并广播出去**
    （接缝是 `plugins.LinkSeams`），谁接、发什么，全在插件那一侧。

    ## 确定的状态迁移（这就是"一次只发一次"的全部实现）

    | 上一刻 | 这一刻看到 | 结果 |
    | --- | --- | --- |
    | 武装（看门狗刚起来就是） | 连着 | 什么都不发，仍然武装 |
    | 武装 | 没连 | **广播一次**，这一段转成"已播过" |
    | 已播过 | 没连 | 什么都不发（掉线期间反复检查**不算新事件**） |
    | 已播过 | 连着 | **重新武装**，什么都不发（**恢复不发**） |

    也就是说：**状态只由"喂进来的 connected"决定**，与"检查了多少次"无关。
    这样一来"每轮都通知"这种写法不会有任何立足点（突变验证用的正是它）。

    ## 三个必须说清的限度（别把推断当实测）

    1. **广播是采样出来的，不是事件回调**：看门狗每
       `CONNECTION_DETECT_EVERY_SECONDS`（≤60 秒）看一次 `transport.connected`。
       所以"断了"最多晚**一个检测周期**被广播；而**两次采样之间断了又连上**
       （窗口 <60 秒的瞬时抖动）仍然**不会被看见**，也就不会通知。要彻底不漏，
       得让传输层在 `_connection_ready.clear()` 那里发事件——**这次没做**。
    2. **第一次采样在启动后 60 秒**（沿用看门狗原有的计时）：开机时对面还没连上来，
       算**一段掉线**，会广播一次。**这是有意的**（2026-10-06 用户定的）：
       "机器重启后 NapCat 没起来、机器人活着但谁也看不见"正是这条通知最大的价值。
       它与那条告警日志的口径一致（"消息进不来"是同一个事实）。
    3. **接收者抛异常只记一笔**，不会把看门狗带走，也不影响后面的接收者。
    """

    __slots__ = ("_receivers", "_enabled", "_announced")

    def __init__(self, receivers: object = (), *, enabled: bool = True) -> None:
        self._receivers: tuple[object, ...] = tuple(receivers)  # type: ignore[arg-type]
        self._enabled = bool(enabled)
        #: 当前这一段掉线**播过没有**。名字刻意是"播过"而不是"断着"：
        #: 状态只关心边沿，不关心断了多久。
        self._announced = False

    @property
    def enabled(self) -> bool:
        """这一个实例的开关（来源是 `dev_config.DISCONNECT_NOTICE_ENABLED`）。"""

        return self._enabled

    @property
    def receivers(self) -> tuple[object, ...]:
        """广播名单（装配时取的那一份快照）。"""

        return self._receivers

    async def observe(self, connected: bool) -> bool:
        """喂一次"现在连着没有"；返回**这一次是不是新广播了一段掉线**。

        返回值只用于测试与日志（"这一段是不是新的"），调用方不必依赖它。
        """

        if connected:
            # 重连 = **重新武装**。这里刻意什么都不发：用户明确说过
            # "bot 本身稳定性很可靠，不用额外通知"，恢复通知只是噪音。
            self._announced = False
            return False
        if self._announced:
            # 同一段掉线里的后续检查：**不是新事件**，一次都不再叫。
            return False
        self._announced = True
        if not self._enabled:
            # 开关关掉时行为与改动前逐字相同：状态照常迁移，一个接收者都不叫。
            return False
        await self._broadcast()
        return True

    async def _broadcast(self) -> None:
        """逐个叫接收者。**每一个都单独兜异常**——发信失败不许把机器人带走。"""

        for receiver in self._receivers:
            try:
                await _maybe_await(receiver())
            except Exception:  # noqa: BLE001 - 接收者自己的失败不该有别的后果
                logger.warning("disconnect_receiver_failed receiver=%s",
                               getattr(receiver, "__qualname__", repr(receiver)),
                               exc_info=True)


def _disconnect_notifier_for(engine: DialogueEngine) -> _DisconnectNotifier:
    """给看门狗造广播对象：名单是**装配时**已登记的那一份，开关来自配置。

    名单在这里取一次快照，而不是每次广播都去问注册表：一次掉线里"谁在听"
    中途变化的话，"叫了谁"就不好说清了（`PluginRegistry.disconnect_receivers()`
    返回的也是元组快照）。没有插件注册时名单为空——广播是空转，**核心照常跑**。
    """

    registry = getattr(engine, "plugin_registry", None)
    receivers = registry.disconnect_receivers() if registry is not None else ()
    return _DisconnectNotifier(receivers, enabled=dev_config.DISCONNECT_NOTICE_ENABLED)


async def _watch_connection(transport: QQTransport,
                            notifier: _DisconnectNotifier | None = None,
                            ticker: _ConnectionTicker | None = None) -> None:
    """盯"对面连上来没有"：**检测快**（≤60 秒一次），**日志照旧**（10 分钟一条）。

    `notifier` 是**掉线事件**的广播口（`plugins.LinkSeams` 那条接缝的发送端）。
    缺省 `None` = 没有广播（老调用方与不关心事件的测试照常）。
    `ticker` 是采样/日志的节奏（见 `_ConnectionTicker`）；缺省按两个常量造一个。
    给它留参数只是为了让测试能换掉时钟与节奏——生产路径永远走缺省那一份。

    **还是这一条看门狗、还是同一个循环**（没有第二个定时器）：
    第一次检查仍在启动后 `CONNECTION_WARN_AFTER_SECONDS`（60 秒），
    之后每 `CONNECTION_DETECT_EVERY_SECONDS`（≤60 秒）转一圈——**周期变快了**，
    而那条告警日志由 `_ConnectionTicker` 按 `CONNECTION_WARN_EVERY_SECONDS`
    （10 分钟）单独裁，文案与出现时刻都没变（见那个类的说明）。
    """

    ticker = ticker if ticker is not None else _ConnectionTicker(notifier)
    await asyncio.sleep(CONNECTION_WARN_AFTER_SECONDS)
    while True:
        await ticker.tick(transport)
        await asyncio.sleep(ticker.detect_every)


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


async def handle_turn_within_budget(engine: DialogueEngine, item: IncomingMessage):
    """给**一轮对话**包一层总预算，超时返回 None（2026-10-08 失败降级审计 2）。

    为什么需要它：接收循环是**串行**的（`_handle_one` 逐条 await），所以一条卡住的腿
    会把后面所有消息一起挡住。2026-10-13 之后**每一腿的网都只是"停顿网"**
    （首字 3 秒 + 连续 `timeout` 秒无字节），它们相加可以远超这个预算：识图 20 +
    扩展材料 2 + 压缩 6×20 + 判定 20 + 回复 30 + 审核 6 + 重写一次 30 ≈ 228 秒。
    这一层是**最后一道网**，也是唯一还会在"她正在流式输出"时把整轮砍掉的地方——
    取舍是"宁可这一条不回，也不让整个 bot（含 ping、`/admin`、私聊）停摆"。
    （2026-10-13 更正：这里原来写"单次 `complete()` 最坏 ≈ 181 秒（60 秒 × 3 次尝试
    + 退避）、一轮约 9 分钟"——那套算式随"不重试 + 超时只管首字"作废。）

    超时的结果与"模型抛异常"**完全相同**：这一条没有回复（返回 None），
    后续消息照常处理。上游 `_handle_one` 的异常分支本来就是这样收尾的，
    这里只是把"要等多久"从"模型自己愿意回"变成"有上界"。

    **不动历史**：那条消息在 `engine.handle()` 里早就 `state.add` 过了（记历史发生在
    任何模型调用之前），超时也不把它删掉——她该知道群里发生过那句话。

    预算**调用时现读**（`QQBOT_TURN_TIMEOUT_SECONDS`，默认 90 秒，夹在 5..600）。
    """

    budget = dev_config.turn_timeout_seconds()
    # 每一轮从"还没进任何一段"开始：不重置的话，上一轮留下的值会替这一轮背锅
    # （这条网只在卡住时响，卡在收尾/发送这类没有模型调用的时候也会响，那时该印 `-`）。
    dev_config.set_turn_stage(engine, "")
    try:
        return await asyncio.wait_for(engine.handle(item), timeout=budget)
    except asyncio.TimeoutError:
        # 超时会取消里面那个任务（`asyncio.wait_for` 的语义）；引擎那边的
        # `finally` 负责把巡检守卫还回去（见 `FocusController.end_sweep`）。
        engine.note_turn_timeout()
        # **被砍在哪一段**（2026-10-13 夜间复盘）：原来只写 session 与 budget，
        # 事后无从查起。引擎在跑模型前把它写在**自己身上**（`set_turn_stage`），
        # 取消不会影响这个值；还没进任何一段（收尾/发送/纯逻辑）时如实印成 `-`。
        logger.error(
            "Stage 3 turn timeout: session=%s budget=%ss stage=%s（这一条没有回复，历史照留）",
            item.session_id, budget, dev_config.current_turn_stage(engine) or "-",
        )
        return None


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

        # 黑话是**被动听来的**（2026-10-06 用户："还有黑话"）：在**收**这一侧顺手看一眼，
        # 写不写词条与"她说不说"完全无关。放在 `engine.handle` **外面**是刻意的——
        # 对话引擎（判定 / 回复 / 记忆）一个字都不用改，观察者也拿不到它的任何东西。
        # 它自己的异常在这里就兜住：写盘失败只该表现为"少学一条"，不能影响这条消息。
        watcher = getattr(engine, "slang_watcher", None)
        if watcher is not None:
            try:
                watcher.observe(item)
            except Exception:  # noqa: BLE001 - 观察者绝不影响对话
                logger.warning("slang_observe_failed", exc_info=True)
        try:
            reply = await handle_turn_within_budget(engine, item)
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
            # "改文件即生效"的钩子之二（契约 D6）：后台那几条（记忆维护 / 金句 / 读信 /
            # 余额探针）在**另一个任务**里跑模型，只留 `handle()` 那一处会出现"记忆那一路
            # 还在用旧家、要等下一轮群消息才恢复"的窗口。没变时只做一次 `stat`。
            try:
                provider_failover.sync_from_file()
            except Exception:  # noqa: BLE001 - 同步失败不该带走焦点时钟
                logger.warning("模型来源同步失败（焦点时钟）", exc_info=True)
            try:
                for source, outgoing in await engine.tick():
                    await _deliver_reply(transport, engine, source, outgoing, outbox=outbox)
            except Exception:
                logger.exception("焦点时钟出错，继续")
            # **转告/通知也要在这个节拍上取一次**（2026-10-18）：原来只有
            # `_handle_one()`（收到一条消息）才 drain，于是"面板发的那句话"在
            # **没人说话的群里会一直躺在队列里**——那正好是它要解决的场景。
            # 这里复用同一个 `drain_relay_deliveries()` + `_deliver_relay()`：
            # 不是第二条出站，只是第二个取件点；两个点都是"取走即清空"，
            # 同一条不会被发两次。
            try:
                for delivery in engine.drain_relay_deliveries():
                    await _deliver_relay(transport, delivery, outbox=outbox)
            except Exception:
                logger.exception("转告投递出错，继续")

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
