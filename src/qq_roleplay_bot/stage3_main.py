from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
import time
from dataclasses import dataclass, replace
from collections import deque
from enum import Enum
from pathlib import Path
from typing import Awaitable, Callable, Protocol, TYPE_CHECKING

from . import dev_config
from .dev_config import (    BATCH_SIZE,
    COOLDOWN_SECONDS,
    ADMIN_USER_IDS,
    SUPER_ADMIN_USER_IDS,
    PRIVATE_DEBUG_USER_IDS,
    TARGET_GROUP_ID,
    ONEBOT_WS_HOST,
    ONEBOT_WS_PORT,
    ONEBOT_ACCESS_TOKEN,
)
from .llm_client import LLMError, OpenAICompatibleClient
from .attention import address_reason, is_addressed_to_bot, mentions_name
from .focus import FocusController
from .builtin_commands import (
    HELP_COMMAND_PATTERN,
    build_command_registry,
    build_help_text,
)
from ._host import HostServices
from .ask_when_unsure import AskBudget, context_is_explicit, decide_grounding
from .dialogue_judge import JudgeVerdict, build_judge_messages, parse_judge_output
from .dialogue_compaction import (
    build_compaction_messages,
    merge_summary,
    parse_compaction_output,
    split_compaction_batches,
)
from .onebot_ws import OneBotWebSocketTransport
from .trigger import IdleTrigger, MessageDeduplicator
from .stage3_runtime import (
    CLOSENESS_LABELS,
    ConversationMode,
    ConversationState,
    DecisionKind,
    DialogueStatus,
    DialogueDecision,
    GUARDEDNESS_LABELS,
    Stance,
    build_dialogue_messages,
    message_index_of,
    parse_dialogue_output,
)
from .transport import (
    IncomingMessage,
    MessageNotDelivered,
    MessageTarget,
    OutgoingMessage,
    QQTransport,
)
from .security import (
    check_message_security,
    # 管理/超管命令的**形状判定**：定义在 `security.py`（插件那边也要用它，
    # 放在这里会逼插件 import 核心模块，2026-10-01 搬）。这里再导出一次，
    # 既有的调用方与测试仍然 `from .stage3_main import privileged_command_level`。
    privileged_command_level,
    sanitize_chat_text,
)
from .snapshots import EngineSnapshot, SessionSnapshot
from .metrics import RuntimeMetrics, process_memory_bytes
from .feature_log import FeatureLogs, log_capacity, request_parts
from .memory_view import (
    ADMIN_HELP,
    ADMIN_HELP_GUEST_NOTE,
    MAX_LIST_ITEMS,
    SUPER_HELP,
    build_archive_view,
    build_audit_view,
    build_inbox_view,
    build_overview,
    build_records_view,
)
from .extensions import PromptContext, PromptMaterial, PromptSources
# `/admin` 是 **Stage 3 自己的命令**（直接管理聊天：开关群、清会话、转告），
# 这个模块就是它的解析器，自身零依赖（只有 `re` + enum + dataclass）。
# 所以这是**合法的 stage3 依赖**，不是要拆掉的拴缚。
from .admin_control import AdminCommand, AdminCommandKind, parse_admin_command
from .memory_model import MemoryMaterial
from .memory_service import MemoryService
from . import runtime_flags

if TYPE_CHECKING:
    # 只为类型标注：机器诊断是**可插能力**，运行时由 `_host_adapters.HostMachineProbe`
    # 兜底（采不到就如实回"采不到"），所以这里绝不 import 真的实现。
    from .runtime_diagnostics import RuntimeDiagnostics

# 短期语境的**硬上限**（deque 容量）。它只作为兜底，不是压缩阈值——
# 设得足够高，让压缩先发生，避免 deque 悄悄丢弃最旧一条（那会打断缓存前缀）。
HISTORY_LIMIT = 2000
# 压缩阈值：活历史超过这么多条就压缩。**这同时决定回复段 prompt 的上界**。
LIVE_TARGET = 500
# 压缩后保留多少条不压。之后窗口重新累积，直到再次超过 LIVE_TARGET。
COMPACT_KEEP = 50
# 一轮最多压几批。压缩按条数切批（见 `split_compaction_batches`），
# 一次触发通常 4 批左右能压完；这里只是防止积压过大时一轮里发出太多调用。
MAX_COMPACT_BATCHES_PER_TURN = 6
ACTIVE_TIMEOUT_SECONDS = 180.0

# 冷群里 @ 她时先回的那句"稍等"。随机挑一条，四条属性见 docs/MULTI_GROUP_FOCUS.md：
# 进她的历史 / 不进长期记忆 / 不算连回 50 条 / 立刻发不摆节奏。
# 措辞要求：短、疲惫、不像客服、不出现机制词（否则她会开始讲自己的机制）。
FOCUS_ACK_LINES: tuple[str, ...] = (
    "记下了。这边还有话没说完，回头答你。",
    "看到了——等我把这边收个尾。",
    "先放着，我这边还缠着件事，完了就过去。",
    "听见了。给我一会儿，说完这边就回你。",
    "知道了。这边一时腾不出手，稍等。",
    "嗯，先记着。手上这段收完就来。",
    "看到了。这边还差一点，回头说。",
    "知道了，别急——我把这头说完就过来。",
)
# 连回满 50 条时说的那句（用户原话）。
FOCUS_FAREWELL_LINE = "我得先处理一下别的消息，回来继续。"
# 群聊中是否让每条消息都进入模型判断（而不是只认"叫她"的消息）。
# 关掉就退回旧的锁焦行为：她只跟当前对话对象说话，别人的发言只当背景。
# 打开会增加模型调用次数——换来的是她真的看得见群里在聊什么。
def _group_listen_enabled() -> bool:
    return os.environ.get("QQBOT_GROUP_LISTEN", "1").strip().lower() not in {
        "0", "false", "no", "off",
    }
EXTENSION_TIMEOUT_SECONDS = 2.0
RELAY_CONFIRMATION_TIMEOUT_SECONDS = 120.0
RELAY_CONTENT_MAX_LENGTH = 1000
MAX_PERSISTED_SESSIONS = 200
# 会话活跃时的持久化节流间隔，避免每条消息都写盘。
PERSIST_MIN_INTERVAL_SECONDS = 10.0
PING_REPLY = "pong"
# 拟人化节奏：发送前的"正在输入"状态，以及每条消息之间的打字停顿。
# 关掉 QQBOT_TYPING_SIM=0 即恢复"模型一返回就立刻发"的旧行为。
TYPING_NOTICE = "typing"


def _typing_sim_enabled() -> bool:
    return os.environ.get("QQBOT_TYPING_SIM", "1").strip().lower() not in {
        "0", "false", "no", "off",
    }


def default_host() -> HostServices:
    """不传 `host` 时的默认：**这一侧现成的实现**（`typing_sim` 分段与停顿等）。

    为什么不给空实现：`QQBOT_TYPING_SIM` 默认是**开**的，也就是说"拟人化停顿"
    是当前的正常行为。如果默认给空实现，所有直接造引擎的调用点（测试、干跑）
    都会静默变成"一次发出去"——那是**改了行为**，不是"拆了接口"。
    拆接口的这一步必须行为不变。

    所以默认 = 现状。要"没有这项能力"的效果，显式传 `host=HostServices()`
    （全空实现）；装配点也可以只覆盖其中几项。

    import 放在函数内：否则又把 `typing_sim` 拴在核心模块顶上了，那正是要拆的。
    """

    from ._host_adapters import build_host_services

    return build_host_services()


# 上下文补全：拉多少条历史、单次超时、同一群多久最多补一次。
CONTEXT_HISTORY_COUNT = 30
CONTEXT_TIMEOUT_SECONDS = 5.0
CONTEXT_REFRESH_INTERVAL_SECONDS = 180.0
# 用于"看起来像 ping 但没匹配上"的日志提示，帮助排查为什么没有响应。
# ping 的匹配规则本身在命令插件里（`builtin_commands.is_ping_command`），
# 这里只留一个"像但不是"的宽松提示，不再复制一份正式规则。
PING_LIKE_PATTERN = re.compile(r"(?:yunru|云如|云茹).{0,6}ping|^[/!#]?ping\b", re.IGNORECASE)

# 分层帮助：/help（公开）→ /admin help（管理员）→ /super help（超管）。
# 用 admin 而不是别的词，是为了和代码里既有的 ADMIN_USER_IDS / admin_control 一致。
ADMIN_HELP_PATTERN = re.compile(r"^[/#]admin(?:\s+(?:help|帮助))?$", re.IGNORECASE)
SUPER_HELP_PATTERN = re.compile(r"^[/#]super(?:\s+(?:help|帮助))?$", re.IGNORECASE)
SUPER_COMMAND_PATTERN = re.compile(
    r"^[/#]super\s+(?P<action>memory|记忆|process(?:es)?|进程|lan|net|网络|fan|fans|风扇"
    r"|status|状态|restart|重启|apicheck|api|接口对账)"
    r"(?:\s+(?P<topic>records|record|记忆|inbox|收件|audit|审计|archive|归档"
    r"|memory|cpu|gpu|gpu-memory))?\s*$",    re.IGNORECASE,
)
# `/super mail [now]` 单开一条：`now` 不能塞进上面那个共享的 topic 组，
# 否则 `/super restart now` 也会被当成合法命令（踩过）。
SUPER_MAIL_PATTERN = re.compile(
    r"^[/#]super\s+(?:mail|邮件|汇报)(?:\s+(?P<when>now|现在))?\s*$",
    re.IGNORECASE,
)
# addadmin / deladmin 单独一条：真正的身份在被 @ 的人身上（`mentioned_user_ids`），
# 正文里剩下什么都无所谓——聊天里手滑 @ 完又打几个字，不该让命令整个失效。
SUPER_ADD_ADMIN_PATTERN = re.compile(
    r"^[/#]super\s+(?:addadmin|添加管理员)(?:\s.*)?$",
    re.IGNORECASE,
)
SUPER_DEL_ADMIN_PATTERN = re.compile(
    r"^[/#]super\s+(?:deladmin|del_admin|removeadmin|删除管理员|移除管理员)(?:\s.*)?$",
    re.IGNORECASE,
)
SUPER_ADMIN_LIST_PATTERN = re.compile(
    r"^[/#]super\s+(?:admin\s*list|admins|admin_list|管理员列表|管理员名单)\s*$",
    re.IGNORECASE,
)
# `/super affinity @某人` 看关系；`... reset` 直接回默认档（人工纠正模型误判）。
SUPER_AFFINITY_PATTERN = re.compile(
    r"^[/#]super\s+(?:affinity|关系|好感度)(?:\s+(?P<action>reset|重置|清空))?(?:\s.*)?$",
    re.IGNORECASE,
)
# `/super permit`：超管**引用**那条被拒的 admin 命令再发这一句，我按原样执行那一条。
# 也接受"授权"这类说法，但语义只有一种：放行**那一条命令**，不是给谁一个身份。
SUPER_PERMIT_PATTERN = re.compile(
    r"^[/#]super\s+(?:permit|授权|临时授权)(?:\s.*)?$",
    re.IGNORECASE,
)
# `/super profile @某人`：把这个人的人物画像完整发到群里（2026-09-30 用户要求）。
# 单独一条模式而不是塞进共享的 topic 组：后面跟的是 @ 段或 QQ 号，塞进去会顺手让
# `/super restart profile` 之类的组合变成合法命令（`permit` 踩过同样的坑）。
SUPER_PROFILE_PATTERN = re.compile(
    r"^[/#]super\s+(?:profile|画像|人物画像|portrait)(?:\s.*)?$",
    re.IGNORECASE,
)
# 群管理与群主命令的解析**已经搬进插件**（`builtin_group_commands.py`，2026-09-30
# 用户要求"stage4 的内容都用插件实现"）。核心不再认识这些命令的字面形状，只保留：
# 档位判定（`_plugin_level_allowed`）与动作执行（`_plugin_action_reply`）。
# `/super admin list` 最多列这么多行：出站正文上限是 1000 字符，
# 名单长了要主动截断并说明还有多少人，不能撞上限被静默截掉。
ADMIN_LIST_MAX_ROWS = 30
# 关系缓存的有效期（秒）。写入时我们会主动失效，所以"当轮变冷"不依赖它；
# TTL 是给维护 agent 那条通道兜底的——它在别的线程改库，引擎并不知情。
STANCE_CACHE_TTL_SECONDS = 60.0
# 她写出去的信在她眼前留多久（小时）。刚发出去的那一封必须能想起来 —— 用户
# 2026-09-28 的现场问题是"她不记得自己邮件发了什么"。过了这个窗口就只在被问到时
# （LETTER_ASK_RE 命中）才拿出来，且最多回溯 LETTER_RECALL_DAYS 天。
LETTER_FRESH_HOURS = 36.0
LETTER_RECALL_DAYS = 30.0
# 信里问她"写了什么"的问法。刻意**只做补充**：新鲜的信用不着它。
LETTER_ASK_RE = re.compile(r"信|邮件|邮箱|汇报|邮局|寄给|寄过来", re.IGNORECASE)
# 注入时正文截断长度：信的正文上限是 1500，整封塞进去每轮都贵。
LETTER_BODY_CHARS = 600
# 引擎内存里留几封（存储层留 5 封，这里多留一点给"往前翻"用）。
LETTER_HISTORY_LIMIT = 10
# 管理/超管命令的"形状"判定（`privileged_command_level`）已经搬到 `security.py`
# ——插件侧的委托与核心的闸门都要用它，放在这里会逼插件 import 核心模块。
# `PRIVILEGED_COMMAND_PATTERN` 跟着一起搬；这里只在顶部 import 里再导出那个函数。
logger = logging.getLogger(__name__)


def _unique_clients(clients) -> list[object]:
    """去重并丢掉 None。"""

    unique: list[object] = []
    seen: set[int] = set()
    for client in clients:
        if client is None or id(client) in seen:
            continue
        seen.add(id(client))
        unique.append(client)
    return unique


def _level_name(labels: tuple[str, ...], value: int) -> str:
    """档位名（不含行为说明）。`/super affinity` 只给人看名字。"""

    return labels[max(0, min(len(labels) - 1, int(value)))].split("——")[0]


def _token_count(value: int) -> str:
    """token 数的人话写法：上千才缩成 `k`。

    以前一律 `{n/1000:.0f}k`，小数目会写成"命中 0k / 未命中 0k"——看着像没数据。
    """

    number = max(0, int(value))
    return f"{number / 1000:.0f}k" if number >= 1000 else str(number)


def _format_bytes(size: object) -> str:
    try:
        value = float(size or 0)
    except (TypeError, ValueError):
        return "—"
    if value <= 0:
        return "—"
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f}{unit}" if unit == "B" else f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}GB"


def _format_duration(seconds: float) -> str:
    total = int(max(0.0, seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours} 小时 {minutes} 分"
    if minutes:
        return f"{minutes} 分 {secs} 秒"
    return f"{secs} 秒"


def _format_clock(timestamp: float) -> str:
    if not timestamp:
        return "—"
    return time.strftime("%m-%d %H:%M:%S", time.localtime(timestamp))


def _sorted_qq(user_ids: object) -> list[str]:
    """QQ 号按数值排序，看起来才像名单而不是字符串表。"""

    return sorted(
        (str(qq) for qq in user_ids),  # type: ignore[union-attr]
        key=lambda qq: (not qq.isdigit(), int(qq) if qq.isdigit() else 0, qq),
    )


def _admin_command_target_group(command: "AdminCommand") -> str:
    """这条管理员命令点名了哪个**群**；没点名就返回空串。

    enable/disable/clear **不在其列**：它们只作用于发命令的那个群，本来就不可能
    点到别的群。真正需要判的是"往别的群发东西"——也就是按群号/群名的转告。
    """

    if command.kind is AdminCommandKind.RELAY and command.target_kind == "group":
        return command.group_id or ""
    return ""


def looks_like_ping_command(text: str) -> bool:
    """消息近似 ping 但未被识别时用于日志提示，避免"发了没反应还查不到原因"。"""

    return isinstance(text, str) and bool(PING_LIKE_PATTERN.search(text.strip()))


def is_help_command(text: str) -> bool:
    """识别公开的帮助命令。

    保留为薄封装：匹配规则的事实来源在命令插件里（`builtin_commands`），
    这里不再复制一份正则，避免两处漂移。
    """

    return isinstance(text, str) and bool(HELP_COMMAND_PATTERN.fullmatch(text.strip()))


class SuperAction(str, Enum):
    """超管命令的动作。"""

    HELP = "help"
    STATUS = "status"
    PROCESSES = "processes"
    LAN = "lan"
    FAN = "fan"
    PERMIT = "permit"
    RESTART = "restart"
    API_CHECK = "api_check"
    MAIL = "mail"
    MAIL_NOW = "mail_now"
    ADD_ADMIN = "add_admin"
    DEL_ADMIN = "del_admin"
    ADMIN_LIST = "admin_list"
    AFFINITY = "affinity"
    AFFINITY_RESET = "affinity_reset"
    MEMORY = "memory"
    MEMORY_RECORDS = "memory_records"
    MEMORY_INBOX = "memory_inbox"
    MEMORY_AUDIT = "memory_audit"
    MEMORY_ARCHIVE = "memory_archive"
    PROFILE = "profile"


def parse_super_command(text: str) -> SuperAction | None:
    """解析 /super 系列命令；非超管前缀一律返回 None。

    注意：这里**只解析、不鉴权**。真正的权限判断在 DialogueEngine 里，
    和现有 `/super processes` 保持同一套规则。
    """

    if not isinstance(text, str):
        return None
    stripped = text.strip()
    if SUPER_HELP_PATTERN.fullmatch(stripped):
        return SuperAction.HELP
    if SUPER_ADD_ADMIN_PATTERN.fullmatch(stripped):
        return SuperAction.ADD_ADMIN
    if SUPER_DEL_ADMIN_PATTERN.fullmatch(stripped):
        return SuperAction.DEL_ADMIN
    if SUPER_ADMIN_LIST_PATTERN.fullmatch(stripped):
        return SuperAction.ADMIN_LIST
    affinity = SUPER_AFFINITY_PATTERN.fullmatch(stripped)
    if affinity:
        action = (affinity.group("action") or "").casefold()
        return SuperAction.AFFINITY_RESET if action in {"reset", "重置", "清空"} else SuperAction.AFFINITY
    # permit 单独一条：它后面可以跟 @ 段和说明文字，塞不进共享的 topic 组——
    # 塞进去还会顺手让 `/super restart permit` 之类的组合变成合法命令（踩过）。
    if SUPER_PERMIT_PATTERN.fullmatch(stripped):
        return SuperAction.PERMIT
    # 画像要在通用模式之前判：`/super profile ...` 不能被当成 `/super <topic>` 吃掉。
    if SUPER_PROFILE_PATTERN.fullmatch(stripped):
        return SuperAction.PROFILE
    # **群管理与群主命令不在这里**：它们是插件（`builtin_group_commands.py`），
    # 在 `handle()` 的插件分发那段就被认领掉了，核心只认"档位"与"动作执行"。
    mail = SUPER_MAIL_PATTERN.fullmatch(stripped)
    if mail:
        when = (mail.group("when") or "").casefold()
        return SuperAction.MAIL_NOW if when in {"now", "现在"} else SuperAction.MAIL
    match = SUPER_COMMAND_PATTERN.fullmatch(stripped)
    if not match:
        return None
    action = match.group("action").casefold()
    topic = (match.group("topic") or "").casefold()
    if action in {"process", "processes", "进程"}:
        return SuperAction.PROCESSES
    if action in {"lan", "net", "网络"}:
        return SuperAction.LAN
    if action in {"fan", "fans", "风扇"}:
        return SuperAction.FAN
    if action in {"restart", "重启"}:
        return SuperAction.RESTART
    if action in {"apicheck", "api", "接口对账"}:
        return SuperAction.API_CHECK
    if action in {"status", "状态"}:
        return SuperAction.STATUS
    # memory / 记忆
    if topic in {"records", "record", "记忆"}:
        return SuperAction.MEMORY_RECORDS
    if topic in {"inbox", "收件"}:
        return SuperAction.MEMORY_INBOX
    if topic in {"audit", "审计"}:
        return SuperAction.MEMORY_AUDIT
    if topic in {"archive", "归档"}:
        return SuperAction.MEMORY_ARCHIVE
    return SuperAction.MEMORY


# `/super apicheck` 报哪些 agent：**每个 agent 一条**。
#
# 为什么不是"每个 key 一条"：同一个 key 也可能跑好几个 agent（没单独配 key 时都回落主 key），
# 而每个 agent 有自己的 user_id —— 那才是缓存真正分桶的地方。所以判据是 agent。
#
# 2026-09-30 用户："审核 agent 用量为什么不在 apicheck 里显示"——以前这里只有三条
# （回复/判定/记忆），风格审核、识图、写信的账记在账本里却没露出来。
APICHECK_ROLES: tuple[tuple[str, str], ...] = (
    ("dialogue", "回复 agent（QQBOT_API_KEY）"),
    ("judge", "判定 agent（QQBOT_JUDGE_API_KEY）"),
    ("memory", "记忆维护（QQBOT_MEMORY_API_KEY）"),
    ("review", "风格审核（QQBOT_REVIEW_API_KEY，可回落主 key）"),
    ("vision", "识图（QQBOT_VISION_API_KEY，可回落主 key）"),
    ("letter", "写信 agent（QQBOT_MAIL_API_KEY，可回落主 key）"),
)

# 被拒的命令可以被超管引用放行的时间窗（秒）。比"授权"宽松些：超管得先看见那句
# "没权限"、再决定引不引用它；过了这个窗口就得让对方重发。
DENIED_ADMIN_TTL_SECONDS = 300.0


def _admin_deny_notice_enabled() -> bool:
    """无权限的 admin 命令要不要回一句说明。

    默认回（用户 2026-09-28 要求："回复无权限用户的 admin 命令"），
    但 AGENTS.md 里"未授权时静默回落、不暴露层级存在"是安全侧的默认，
    所以留 `QQBOT_ADMIN_DENY_NOTICE=0` 一键退回静默。
    """

    return os.environ.get("QQBOT_ADMIN_DENY_NOTICE", "1").lower() not in {"0", "false", "off"}


def _admin_deny_notice() -> str:
    # 出站是**纯文本**（QQ 不渲染 Markdown），所以别在这里用 `**`：那会变成两个星号。
    return ("你没有这个群的管理权限，这条命令没有执行。"
            "可以让超管引用这条消息发一句 `/super permit`，他会替你执行这一条。")


def parse_super_topic(text: str) -> str:
    """取出 `/super <action> <topic>` 里的 topic（小写）。

    单独一个函数而不是改 `parse_super_command` 的返回值：那个函数被大量测试按
    "返回 SuperAction"断言，改返回类型会连锁破坏；这里只给需要细分档位的命令用。
    """

    if not isinstance(text, str):
        return ""
    match = SUPER_COMMAND_PATTERN.fullmatch(text.strip())
    return (match.group("topic") or "").casefold() if match else ""


def is_admin_help_command(text: str) -> bool:
    """识别 /admin help。只解析、不鉴权。"""

    return isinstance(text, str) and bool(ADMIN_HELP_PATTERN.fullmatch(text.strip()))


# 公开帮助。不再是写死的常量：正文由各命令插件自述的行汇总而成，
# 加命令不需要改这里，也不会出现"帮助里没有但命令存在"的漂移。
#
# 模块导入时先算一份**只有核心命令**的（那时候还没接插件）；`build_engine`
# 接上插件之后会调 `_set_public_help(engine.commands)` 重算，让它反映
# **这台机器实际装了什么**。测试里 `PUBLIC_HELP` 就是这么拿到最终值的。
PUBLIC_HELP = build_help_text(build_command_registry().help_lines())


def _set_public_help(commands) -> str:
    """按**当前命令链**重算公开帮助，并更新模块常量。

    为什么要更新常量而不是只给引擎一份：公开帮助必须和核心 `HelpCommand` 的输出
    **逐字相同**（测试就是这么比的），两边只能有一个来源——就是这条命令链。
    """

    global PUBLIC_HELP
    PUBLIC_HELP = build_help_text(commands.help_lines())
    return PUBLIC_HELP


@dataclass(frozen=True, slots=True)
class RelayDelivery:
    target: MessageTarget
    text: str
    admin_target: MessageTarget


@dataclass(frozen=True, slots=True)
class PendingRelay:
    token: str
    admin_user_id: str
    target: MessageTarget
    target_label: str
    text: str
    created_at: float


@dataclass(frozen=True, slots=True)
class RelayTargetCandidate:
    target: MessageTarget
    label: str


class RelayTargetResolver(Protocol):
    async def resolve(self, target_kind: str, query: str) -> tuple[RelayTargetCandidate, ...]:
        """根据群名或昵称返回可供管理员确认的目标。"""


@dataclass(frozen=True, slots=True)
class PendingRelaySelection:
    token: str
    admin_user_id: str
    target_kind: str
    query: str
    text: str
    candidates: tuple[RelayTargetCandidate, ...]
    created_at: float


class OneBotRelayTargetResolver:
    """通过 OneBot 只读 API 将群名、好友昵称或群名片解析为号码。"""

    def __init__(self, transport: OneBotWebSocketTransport) -> None:
        self.transport = transport

    async def resolve(self, target_kind: str, query: str) -> tuple[RelayTargetCandidate, ...]:
        normalized_query = self._normalize(query)
        if not normalized_query:
            return ()
        if target_kind == "group_name":
            return await self._resolve_groups(normalized_query)
        if target_kind == "nickname":
            return await self._resolve_users(normalized_query)
        return ()

    async def _resolve_groups(self, query: str) -> tuple[RelayTargetCandidate, ...]:
        response = await self.transport.call_api("get_group_list")
        candidates: list[RelayTargetCandidate] = []
        for item in self._data_list(response):
            group_id = self._id_text(item.get("group_id"))
            group_name = self._text(item.get("group_name"))
            if group_id and self._normalize(group_name) == query:
                candidates.append(
                    RelayTargetCandidate(
                        target=MessageTarget(group_id=group_id),
                        label=f"{group_name or '未命名群聊'}（群号 {group_id}）",
                    )
                )
        return tuple(candidates)

    async def _resolve_users(self, query: str) -> tuple[RelayTargetCandidate, ...]:
        candidates: dict[str, RelayTargetCandidate] = {}
        try:
            response = await self.transport.call_api("get_friend_list")
        except Exception:
            logger.exception("OneBot 好友列表查询失败")
        else:
            for item in self._data_list(response):
                self._add_user_candidate(candidates, item, query, source="好友")

        try:
            groups_response = await self.transport.call_api("get_group_list")
        except Exception:
            logger.exception("OneBot 群列表查询失败，跳过群成员昵称匹配")
            return tuple(candidates.values())

        for group in self._data_list(groups_response):
            group_id = self._id_text(group.get("group_id"))
            if not group_id:
                continue
            group_name = self._text(group.get("group_name")) or group_id
            try:
                members_response = await self.transport.call_api(
                    "get_group_member_list",
                    {"group_id": int(group_id)},
                )
            except Exception:
                logger.warning("OneBot 群成员列表查询失败：group=%s", group_id)
                continue
            for item in self._data_list(members_response):
                self._add_user_candidate(candidates, item, query, source=f"群聊 {group_name}")
        return tuple(candidates.values())

    def _add_user_candidate(
        self,
        candidates: dict[str, RelayTargetCandidate],
        item: dict[str, object],
        query: str,
        *,
        source: str,
    ) -> None:
        user_id = self._id_text(item.get("user_id"))
        if not user_id:
            return
        aliases = [
            self._text(item.get("nickname")),
            self._text(item.get("remark")),
            self._text(item.get("card")),
        ]
        if not any(self._normalize(alias) == query for alias in aliases if alias):
            return
        display = next((alias for alias in aliases if alias), user_id)
        candidates.setdefault(
            user_id,
            RelayTargetCandidate(
                target=MessageTarget(user_id=user_id),
                label=f"{display}（QQ {user_id}，来源：{source}）",
            ),
        )

    @staticmethod
    def _data_list(response: dict[str, object]) -> list[dict[str, object]]:
        data = response.get("data")
        if not isinstance(data, list):
            return []
        return [item for item in data if isinstance(item, dict)]

    @staticmethod
    def _id_text(value: object) -> str:
        if isinstance(value, bool) or value is None:
            return ""
        value = str(value).strip()
        return value if value.isdigit() else ""

    @staticmethod
    def _text(value: object) -> str:
        return value.strip() if isinstance(value, str) else ""

    @staticmethod
    def _normalize(value: str) -> str:
        return " ".join(sanitize_chat_text(value, max_length=128).casefold().split())


async def _send_or_queue(
    transport: QQTransport, outbox, target: MessageTarget, text: str, *, reply_to: str = "",
) -> str:
    """发一条单发消息；"确定没送出去"就排进补发队列。

    返回 `"sent"` / `"queued"` / `"failed"`，调用方据此决定怎么回执。
    转告与超管诊断的延迟回发走这里（用户 2026-09-28："接入"）——它们以前各自
    直接 `transport.send`，连接一断就静默消失：转告是管理员确认过的动作，
    诊断是超管等了 5 秒的结果，都不该因为一次断线就没了。
    """

    try:
        await transport.send(target, text, reply_to=reply_to)
    except MessageNotDelivered as exc:
        if outbox is not None and outbox.enqueue(target, text, reply_to=reply_to):
            logger.warning("Stage 3 message queued for resend (%s): group=%s",
                           exc, target.group_id or target.user_id)
            return "queued"
        logger.error("Stage 3 message dropped (outbox unavailable/full): group=%s",
                     target.group_id or target.user_id)
        return "failed"
    except Exception:
        logger.exception("Stage 3 message send failed")
        return "failed"
    return "sent"


async def _deliver_relay(transport: QQTransport, delivery: RelayDelivery, *, outbox=None) -> None:
    """发送已确认的转告，并把 OneBot 结果通知回发起管理员。

    "确定没送出去"时不报失败——它没失败，是**晚一点**：排进补发队列，并且带一张
    便条（`note_target`），补发成功之后自动告诉发起人"已经发出去了"。
    """

    try:
        await transport.send(delivery.target, delivery.text)
    except MessageNotDelivered as exc:
        logger.warning("Stage 3 relay not delivered (%s); queueing for resend", exc)
        queued = outbox is not None and outbox.enqueue(
            delivery.target, delivery.text,
            note_target=delivery.admin_target, note_text=f"转告已发送：{delivery.text}",
        )
        await _send_or_queue(
            transport, outbox, delivery.admin_target,
            "转告这次没送出去（对方连接不在），我已经记下了，接上就自动补发；发出去会再告诉你。"
            if queued else "转告发送失败，请检查目标会话和 OneBot 连接。",
        )
    except Exception:
        logger.exception("Stage 3 relay delivery failed")
        await _send_or_queue(
            transport, outbox, delivery.admin_target, "转告发送失败，请检查目标会话和 OneBot 连接。",
        )
    else:
        await _send_or_queue(
            transport, outbox, delivery.admin_target, f"转告已发送：{delivery.text}",
        )


class DialogueSessions:
    """按会话保存短期聊天记录和语境，默认不会跨群共享。"""

    def __init__(self) -> None:
        self.states: dict[str, ConversationState] = {}
        self.triggers: dict[str, IdleTrigger] = {}

    def state(self, session_id: str) -> ConversationState:
        return self.states.setdefault(session_id, ConversationState(history_limit=HISTORY_LIMIT))

    def trigger(self, session_id: str) -> IdleTrigger:
        return self.triggers.setdefault(
            session_id,
            IdleTrigger(BATCH_SIZE, 60.0, COOLDOWN_SECONDS),
        )

    def is_active(self, state: ConversationState, now: float) -> bool:
        if state.mode is not ConversationMode.ACTIVE:
            return False
        if state.last_activity_at is not None and now - state.last_activity_at > ACTIVE_TIMEOUT_SECONDS:
            state.exit()
            return False
        return True

    def leave(self, session_id: str) -> None:
        self.state(session_id).exit()
        self.trigger(session_id).reset()

    def export(self, *, max_sessions: int = 200) -> list[dict[str, object]]:
        """导出有内容的会话，用于跨重启持久化；按最近活动排序并限量。"""

        candidates = [(session_id, state) for session_id, state in self.states.items() if state.history]
        candidates.sort(key=lambda item: item[1].last_activity_at or 0.0, reverse=True)
        exported: list[dict[str, object]] = []
        for session_id, state in candidates[:max_sessions]:
            record = state.as_state()
            record["session_id"] = session_id
            exported.append(record)
        return exported

    def restore(self, records) -> int:
        """从持久化数据恢复会话；单条损坏只跳过该条，不影响其它会话。"""

        restored = 0
        for record in records:
            if not isinstance(record, dict):
                continue
            session_id = record.get("session_id")
            if not isinstance(session_id, str) or not session_id:
                continue
            try:
                state = ConversationState.from_state(record, history_limit=HISTORY_LIMIT)
            except (TypeError, ValueError):
                logger.warning("runtime_state_session_skipped reason=invalid_record")
                continue
            if state is None or not state.history:
                continue
            self.states[session_id] = state
            restored += 1
        return restored


class DialogueEngine:
    """把消息筛选、一次模型判断和会话状态变更组合成可测试的业务层。"""

    def __init__(
        self,
        client: OpenAICompatibleClient,
        *,
        target_group_id: str = TARGET_GROUP_ID,
        private_debug_user_ids: frozenset[str] = PRIVATE_DEBUG_USER_IDS,
        sessions: DialogueSessions | None = None,
        deduplicator: MessageDeduplicator | None = None,
        prompt_sources: PromptSources | None = None,
        admin_user_ids: frozenset[str] = ADMIN_USER_IDS,
        super_admin_user_ids: frozenset[str] = SUPER_ADMIN_USER_IDS,
        enabled_group_ids: frozenset[str] | None = None,
        relay_target_resolver: RelayTargetResolver | None = None,
        runtime_diagnostics: "RuntimeDiagnostics | None" = None,
        memory_service: MemoryService | None = None,
        state_store=None,
        context_provider=None,
        clock=time.monotonic,
        group_listen: bool | None = None,
        typing_sim: bool | None = None,
        command_registry=None,
        judge_client=None,
        client_factory=None,
        judge_client_factory=None,
        feature_logs: FeatureLogs | None = None,
        style_reviewer=None,
        vision=None,
        host: HostServices | None = None,
        ask_budget: AskBudget | None = None,
    ) -> None:
        self.client = client
        # **宿主能力**（出站表现、卡片渲染、机器探测、审计、余额、角色、检索）。
        # 引擎只说"我需要什么"，谁实现的由装配点给进来；见 `_host/__init__.py`。
        #
        # 不传时用 `default_host()`：它是**这一侧现成的实现**（`typing_sim` 等），
        # 而不是空实现——理由见那个函数。装配点（`runtime.build_engine`）会传完整的
        # 一包进来，所以生产路径走的是它自己那一份。
        self.host = host if host is not None else default_host()
        # 判定 agent 的 client。为 None 时退回单 agent（判定与回复同一次调用），
        # 这样双 agent 结构可以随时回退，也让既有测试不必全部改造。
        self.judge_client = judge_client
        # 按会话取 client 的工厂：让每个群/私聊有自己的 user_id（= 自己的缓存隔离空间）。
        # 不传工厂时就一直用上面那两个 client（测试与单群场景不受影响）。
        self.client_factory = client_factory
        self.judge_client_factory = judge_client_factory
        self._session_clients: dict[str, object] = {}
        self._session_judges: dict[str, object] = {}
        self.target_group_id = target_group_id
        self.private_debug_user_ids = private_debug_user_ids
        self.sessions = sessions or DialogueSessions()
        # 跨重启的累计账本（runtime 会塞进来；测试里是 None，等于只报本次进程）。
        self.usage_store = None
        # 她自己在每个群的 QQ 角色（群主/管理员/成员）。群主专属动作靠它决定能不能做；
        # 测试里是 None 时一律按"查不到"处理（fail-closed），不会误当成群主。
        self.self_roles = None
        # 发一张本地图片的接缝（`(target, png_bytes) -> Awaitable[bool]`）。
        # 只有"帮助卡片"用它；没接（测试、别的传输层）时帮助自动退回文字。
        self.image_sender = None
        # 普通用户命令的注册表。默认只装内置的 /help；新增功能以插件形式加进来，
        # 而不是往 handle() 里再塞一个 if 块。
        self.commands = command_registry if command_registry is not None else build_command_registry()
        # 群聊是否让每条消息都进模型。默认跟随环境变量，也可由调用方直接指定。
        self.group_listen = _group_listen_enabled() if group_listen is None else bool(group_listen)
        # 是否在出站时加入拟人化停顿（"正在输入" + 分段间隔）。
        self.typing_sim = _typing_sim_enabled() if typing_sim is None else bool(typing_sim)
        self.deduplicator = deduplicator or MessageDeduplicator()
        self.prompt_sources = prompt_sources or PromptSources()
        # 管理员/超管名单。**管理员是按群授权的**：`/super addadmin @某人` 在哪个群
        # 发的，那个人就只在这个群里有管理员权限——不然一个群的管理员能跑到别的群
        # 把别人的对话关掉。配置里的 `admin_user_ids` 是部署级名单，对所有群生效。
        self.admin_user_ids: set[str] = set(admin_user_ids)
        # 配置里写死的那部分单独留一份：它们不能通过 `/super deladmin` 删掉
        # （删了重启也会回来），也不算"运行时授的权"。
        self._configured_admin_user_ids: frozenset[str] = frozenset(admin_user_ids)
        # 群号 → 该群的管理员集合。授权与撤销都落在这一张表上。
        self.group_admin_ids: dict[str, set[str]] = {}
        self.super_admin_user_ids: set[str] = set(super_admin_user_ids)
        self.enabled_group_ids = set(enabled_group_ids) if enabled_group_ids is not None else {target_group_id}
        self.relay_target_resolver = relay_target_resolver
        # 机器诊断（进程/网卡/风扇）。**可插能力**：没接上时 `_HostMachineProbe()`
        # 是个空壳，`/super processes|lan|fan` 会如实回"采不到"，而不是让核心起不来。
        if runtime_diagnostics is None:
            from ._host_adapters import HostMachineProbe

            runtime_diagnostics = HostMachineProbe()
        self.runtime_diagnostics = runtime_diagnostics
        # 最近被拒的 admin 命令：超管**引用**它再发 `/super permit` 时，用来绑定"就那一条"。
        # 键是**消息 id**（引用给的也是消息 id），值是整条消息——放行时要按原样重放它。
        self._denied_admin: dict[str, tuple[IncomingMessage, float]] = {}
        # 她发出去的信（最近在前）：{"at","to","subject","body"}。由 runtime 从
        # `mail_state` 里灌一次（重启后还在），`DailyReporter` 发完一封会当场补进来。
        self.letter_history: list[dict] = []
        # 延迟回发：给需要采样的诊断命令用（/super processes cpu 先回一句、5 秒后再发结果）。
        # 由 runtime 接到 transport.send；没接上时命令退化成同步等采样完再回。
        self.async_sender: Callable[[MessageTarget, str], Awaitable[None]] | None = None
        self.memory_service = memory_service
        self.context_provider = context_provider
        # 风格审核（可选）：她写完、要发出去之前过一道窄职责校对。没配就是 None（直通）。
        self.style_reviewer = style_reviewer
        # 识图（可选）：有图的消息在**进判定之前**先看一眼，把 `[图片]` 换成一句描述。
        # 它只改正文，不新增决策路径——"要不要回、回什么"照旧全在原来那套里。
        self.vision = vision
        # 「不懂就问」的限量（2026-10-04）：同一话题最多一次、同一个群一段时间内最多几次。
        # 计数只在内存里（见 `AskBudget`），默认值来自 `dev_config.QQBOT_ASK_*`；
        # 测试可以直接塞一份自己的进来，把"十分钟"缩成"一秒"。
        self.ask_budget = ask_budget if ask_budget is not None else AskBudget(
            max_per_topic=dev_config.ASK_MAX_PER_TOPIC,
            max_per_window=dev_config.ASK_MAX_PER_WINDOW,
            window_seconds=dev_config.ASK_WINDOW_SECONDS,
        )
        # 能力闸门（群管理要用它按用途校验 action）。默认自己建一份离线目录；
        # runtime 会把它自己的那份塞进来，保证与补上下文用的是同一份白名单。
        from .capabilities import CapabilityRegistry

        self.capabilities = CapabilityRegistry()
        self._pending_memory_replies: dict[str, str] = {}
        # 一条输入可能拆成多条出站消息；首条由 handle() 返回，其余排在这里。
        # **按会话存**：见 `take_follow_ups` 的说明（2026-09-30 起有第二个调用方）。
        self._follow_ups: dict[str, list[OutgoingMessage]] = {}
        # 被焦点排队过的消息 id：轮到它那个群时我们会**再走一次 handle()**，
        # 这次要跳过去重与"记进历史"（消息在到达时已经记过了），否则历史里会出现两遍。
        self._resumed_ids: set[str] = set()
        # 已经回过"稍等"、因此**必须给结果**的消息 id（判定不许否决）。
        self._promised_ids: set[str] = set()
        # `/super restart` 留下的意图标记：由 runtime 在发完回复、关掉传输层之后读走。
        self._restart_requested = False
        # 运行期开关（面板可改）的**替身**：`runtime` 装配时会 `runtime_flags.install()`
        # 一份真的，引擎这边就优先用它；没装配（测试、干跑）时用这份全开的，
        # 行为与"还没有面板"时一模一样。
        self._default_flags = runtime_flags.RuntimeFlags()
        # 多群焦点：同一时刻只有一段对话在当值，其余群排队（见 focus.py）。
        self.focus = FocusController(
            quiet_seconds=dev_config.FOCUS_QUIET_SECONDS,
            duty_limit_seconds=dev_config.FOCUS_DUTY_SECONDS,
            reply_limit=dev_config.FOCUS_REPLY_LIMIT,
            ack_cooldown_seconds=dev_config.FOCUS_ACK_COOLDOWN_SECONDS,
        )
        self.metrics = RuntimeMetrics()
        # 按功能分开的输入输出日志（判定 / 回复 / 记忆 / 规则拦截放行），
        # 每个功能各留最近 1000 次。正文只在文件里，内存里只有计数与预览。
        self.logs = feature_logs if feature_logs is not None else FeatureLogs()
        if self.logs.enabled:
            logger.info(
                "功能日志已启用：%s（每个功能各留最近 %s 次）",
                ", ".join(sorted(self.logs.logs)),
                log_capacity(),
            )
        # 记忆维护用的 client。它跟对话会话的 client 是**不同的 key**，
        # 命中率要分开算，所以这里留一个引用（由 runtime 在启动记忆时注入）。
        self.memory_client = None
        self.started_at = time.time()
        self.last_cache_report: dict[str, object] | None = None
        self.state_store = state_store
        self.clock = clock
        self.enabled = True
        self._stats = {name: 0 for name in (
            "accepted_messages", "ignored_messages", "blocked_messages",
            "deferred_messages", "model_calls", "replies", "history_seeded",
            "reply_segments", "judge_calls", "compaction_calls",
            # 焦点观测：排进队列多少条、回了多少"稍等"、切换了几次当值群、
            # 以及"答应过必须回"的消息里被回复段拒了几条（异常，应当为 0）。
            "focus_queued", "focus_acks", "focus_switches", "focus_queued_replies",
            "focus_declined_promised", "empty_forced_reply",
            # 关系：判定报了几次"越界"、其中被机械上限拒了几次（应当很少）。
            "guard_raised", "guard_rejected",
            # 风格审核改了几条（没配审核时恒为 0）。
            "reply_reviewed",
            # 识图成功几次（把 `[图片]` 换成描述的次数；没配识图时恒为 0）。
            "media_described",
            # 「不懂就问」：以"问"回应的次数，以及"没把握/没根据所以没插话"的次数
            # （关掉那条规则时两者恒为 0）。只有计数，没有正文。
            "clarify_asked", "clarify_quiet",
        )}
        self._pending_relays: dict[str, PendingRelay] = {}
        self._pending_relay_selections: dict[str, PendingRelaySelection] = {}
        self._relay_deliveries: deque[RelayDelivery] = deque()
        # 关系现状的小缓存：判定与回复在同一轮里各读一次，不该查两遍库。
        # 判定那条通道写入时主动失效（"立刻变冷"靠这个），TTL 是给维护 agent
        # 那条通道兜底的——它在别的线程改库，引擎收不到通知。
        self._stance_cache: dict[str, tuple[Stance, float]] = {}

    def _review_context(self, message: IncomingMessage, state) -> str:
        """交给风格审核的一点现场：她正在回的那句话 + 最近两句。

        审核只需要判断"这句是不是在抬杠"，所以给最小的现场就够——给整段历史
        既贵又会把审核带偏成"替她重新聊天"。
        """

        recent = [item.text for item in state.recent()[-3:] if item.text and not item.is_bot_message]
        lines = [f"上一句：{text}" for text in recent[-2:]]
        lines.append(f"当前这句：{message.text or ''}")
        return "\n".join(lines)[-600:]

    async def _apply_vision(self, message: IncomingMessage, state) -> IncomingMessage:
        """识图：把 `[图片]`/`[表情包]` 换成一句描述，并把历史里那条也一起改掉。

        为什么历史也要改：她后面几轮再回头看这段对话时，看到的应该是"他发了张猫的图"，
        而不是一个 `[图片]` 占位符——不然那轮之后"图里有什么"就彻底丢了。
        识图失败（返回空）时原样返回，占位符留着。

        **表情包要按表情包问**（2026-09-30 用户："表情包和图片分不清"）：
        `media_kinds` 说这一条是贴图还是照片；整条都是贴图时才按贴图去描述，
        混着两种就当一个普通画面问，免得把照片也讲成梗图。
        """

        from .vision import replace_media_placeholder

        kinds = tuple(getattr(message, "media_kinds", ()) or ())
        sticker = bool(kinds) and all(kind == "sticker" for kind in kinds)
        try:
            description = await self.vision.describe(message.media_urls, sticker=sticker)
        except Exception:  # noqa: BLE001 - 识图这条路的异常一律降级
            logger.exception("Stage 3 vision failed")
            return message
        if not description:
            return message
        described = replace(
            message,
            text=replace_media_placeholder(message.text, description),
            has_media=False,
            media_urls=(),
            media_kinds=(),
        )
        self._stats["media_described"] += 1
        for index, item in enumerate(state.history):
            if item.message_id == message.message_id:
                state.history[index] = described
                break
        logger.info("Stage 3 vision described: session=%s chars=%s sticker=%s",
                    message.session_id, len(description), sticker)
        return described

    def _stance_for(self, message: IncomingMessage) -> Stance:
        """当前这个人的关系现状。查不到、没开记忆、读库失败 → 默认档（生疏 + 如常）。

        关系是**按人、跨群**的：同一个人在哪个群都是同一段关系。

        缓存有 TTL：判定那条通道写入时我们会主动失效（"立刻变冷"靠这个），
        但**维护 agent 是在别的线程里改库的**，引擎不知情——没有 TTL 就会一直
        读到旧档位。60 秒对"关系按天变"这件事足够细，对"当轮变冷"没有影响。
        """

        user_id = message.user_id
        store = getattr(self.memory_service, "store", None)
        if not user_id or store is None:
            return Stance()
        now = self.clock()
        cached = self._stance_cache.get(user_id)
        if cached is not None and now - cached[1] <= STANCE_CACHE_TTL_SECONDS:
            return cached[0]
        try:
            closeness, guardedness = store.relationship(user_id)
        except Exception:  # noqa: BLE001 - 读不到就当陌生人，不能挡住对话
            logger.exception("Stage 3 stance read failed")
            return Stance()
        if len(self._stance_cache) > 512:
            self._stance_cache.clear()
        self._stance_cache[user_id] = (Stance(closeness, guardedness), now)
        return self._stance_cache[user_id][0]

    def _apply_guard_signal(self, verdict: JudgeVerdict, message: IncomingMessage) -> None:
        """把判定的 `<guard>UP` 立刻落到防备上——**在拼这一轮回复之前**。

        这是"被冒犯立刻变冷"的全部实现：判定本来就在每条消息上跑，这里只是把它的
        结论写进去，然后让缓存失效，于是**同一条回复**就已经按新的分寸在说了。

        只升不降：`may_lower_guard` 不传。降交给维护 agent（7 天最多一档），
        免得"他道了个歉"就立刻回暖。
        """

        if not verdict.guard_up:
            return
        user_id = message.user_id
        store = getattr(self.memory_service, "store", None)
        if not user_id or store is None:
            return
        reason = verdict.guard_reason or "他说了些越过分寸的话"
        try:
            rejection = store.apply_affinity(
                user_id, guardedness=1, reason=reason, source="judge",
            )
        except Exception:  # noqa: BLE001 - 写不进去也不能打断这一轮回复
            logger.exception("Stage 3 guard write failed")
            return
        self._stance_cache.pop(user_id, None)
        if rejection:
            self._stats["guard_rejected"] += 1
            logger.info("Stage 3 guard signal rejected: reason=%s user=%s", rejection, user_id)
        else:
            self._stats["guard_raised"] += 1
            logger.info("Stage 3 guard raised: user=%s", user_id)

    def stance_delay_for(self, message: IncomingMessage) -> float:
        """这条回复要不要先搁一下（秒）。命令路径不调它。"""

        return self._stance_for(message).reply_delay()

    def _log_model_io(self, feature: str, request, output: str, **meta: object) -> None:
        """把一次模型调用的完整输入输出写进对应功能的日志。

        `request_parts` 会把**所有** user 段都收进来：回复请求有稳定段与易变段两段，
        旧实现只记第一段，"你与这个人"这类后来加进易变段的东西在日志里根本看不到。
        """

        system, user_content = request_parts(request)
        self.logs.record(feature, system=system, input=user_content,
                         output=output or "", **meta)

    def _log_security(self, message: IncomingMessage, decision) -> None:
        """规则拦截/放行各记一条。内容是消息正文，所以和模型日志同一处管理。"""

        self.logs.record(
            "security",
            session_id=message.session_id,
            user_id=message.user_id,
            group_id=message.target.group_id or "",
            blocked=bool(decision.blocked),
            reason=getattr(decision, "reason", "") or "",
            input=message.text or "",
            output=getattr(decision, "reply", "") or "",
        )

    def _client_for(self, session_id: str):
        """这个会话该用哪个对话 client（按会话隔离 user_id / 缓存空间）。"""

        if self.client_factory is None:
            return self.client
        client = self._session_clients.get(session_id)
        if client is None:
            client = self.client_factory(session_id)
            self._session_clients[session_id] = client
        return client

    def _judge_for(self, session_id: str):
        """这个会话该用哪个判定 client；退回单 agent 时返回 None。

        2026-10-01：加一道**运行期开关**（面板能关判定 agent）。
        它的语义与"没配判定 key / 关了双 agent"完全一样：退回单 agent，
        回复照常出得来——绝不会出现"关了判定她就不说话"。
        """

        if not self._flags().judge_enabled:
            return None
        if self.judge_client is None:
            return None
        if self.judge_client_factory is None:
            return self.judge_client
        client = self._session_judges.get(session_id)
        if client is None:
            client = self.judge_client_factory(session_id)
            self._session_judges[session_id] = client
        return client

    def _judge_on(self, session_id: str = "") -> bool:
        """这个会话现在有没有判定 agent。**只用于分支判断**，取 client 走 `_judge_for`。

        统一成一个方法是为了不散落：面板关掉判定之后，这几处（识图后的判定、回复 prompt
        的 must_reply、空手而归的异常计数）必须**同时**切到单 agent 那条路上，
        否则会出现"判定不跑了，回复却还按必须回来写"这种半开状态。
        """

        return self._judge_for(session_id) is not None

    def _flags(self):
        """运行期开关（面板能改）。没装配时给一份全开的替身，行为与从前一致。"""

        flags = runtime_flags.shared()
        if flags is None:
            flags = self._default_flags
        return flags

    def _all_model_clients(self) -> list[object]:
        """所有真正在跑模型调用的 client（去重）。

        按会话分 client 之后，用量散在各处：兜底 client、每个会话的对话 client、
        每个会话的判定 client、记忆维护 client。
        """

        return _unique_clients([
            self.client, self.judge_client, self.memory_client,
            *self._session_clients.values(), *self._session_judges.values(),
        ])

    def _cache_buckets(self) -> dict[str, list[object]]:
        """按 **API key 的角色** 分组：判定 / 回复 / 记忆。

        以前把全部 client 合成一个数（"整个账号的命中率"），那个数字没法用：
        判定与回复的 prompt 形状完全不同，混在一起既看不出谁在退化，也会掩盖
        "记忆的 key 其实一次都没命中"这类问题。三个 key 就是三条独立的口径。
        """

        memory = [self.memory_client] if self.memory_client is not None else []
        # 兜底 client 在生产里只被记忆维护用到；它同时是对话的兜底时才算进对话，
        # 否则会把记忆的用量算到回复头上。
        fallback_for_dialogue = (
            [self.client] if self.client is not None and self.client is not self.memory_client else []
        )
        judge_fallback = (
            [self.judge_client]
            if self.judge_client is not None and self.judge_client is not self.memory_client
            else []
        )
        return {
            "dialogue": _unique_clients([*fallback_for_dialogue, *self._session_clients.values()]),
            "judge": _unique_clients([*judge_fallback, *self._session_judges.values()]),
            "memory": _unique_clients(memory),
        }

    @staticmethod
    def _bucket_stats(clients: list[object]) -> dict[str, object]:
        calls = hit = miss = 0
        for client in clients:
            reader = getattr(client, "cache_stats", None)
            if not callable(reader):
                continue
            try:
                stats = reader()
            except Exception:  # noqa: BLE001 - 统计读不到不该影响命令
                continue
            calls += int(stats.get("calls", 0))
            hit += int(stats.get("hit_tokens", 0))
            miss += int(stats.get("miss_tokens", 0))
        total = hit + miss
        return {
            "calls": calls,
            "hit_tokens": hit,
            "miss_tokens": miss,
            "clients": len(clients),
            "hit_rate": round(hit / total, 4) if total else 0.0,
        }

    def cache_report(self) -> dict[str, object]:
        """**按 key** 的提示词缓存报告（给 `/super apicheck` 与对话结束时的检查用）。"""

        buckets = self._cache_buckets()
        report: dict[str, object] = {
            role: self._bucket_stats(clients) for role, clients in buckets.items()
        }
        report["total"] = self._bucket_stats(self._all_model_clients())
        return report

    def cache_totals(self) -> dict[str, object]:
        """合计口径。保留给"只想要一个总数"的地方；对外展示用 `cache_report`。"""

        return dict(self.cache_report()["total"])  # type: ignore[arg-type]

    def log_cache_report(self, reason: str) -> dict[str, object]:
        """算一次按 key 的命中率并记日志，同时留给 `/super apicheck` 看。"""

        report = self.cache_report()
        self.last_cache_report = {"reason": reason, "at": time.time(), **report}
        parts = []
        for role in ("dialogue", "judge", "memory"):
            stats = report.get(role) or {}
            if not isinstance(stats, dict) or not stats.get("calls"):
                parts.append(f"{role}=无调用")
                continue
            parts.append(f"{role}={float(stats.get('hit_rate', 0.0)):.1%}")
        logger.info("缓存命中率检查（%s）：%s", reason, "  ".join(parts))
        return report

    async def _seed_history(self, message: IncomingMessage, state: ConversationState) -> int:
        """把群聊背景补进短期历史；返回并入的条数。

        只在会话里已有的历史为空时补（新会话的第一条消息），避免每轮都判断；
        真正的限流由 provider 自己按群维护。任何失败都只是少一点背景。
        """

        if self.context_provider is None or state.history:
            return 0
        try:
            seeded = await self.context_provider.collect_seed_messages(
                message.session_id, message.target.group_id, message
            )
        except Exception:
            # 补上下文永远不能影响对话本身。
            logger.exception("Stage 3 context seeding failed")
            return 0
        for item in seeded:
            # **必须走 state.add()**：它顺手 `note_speaker`，给每条补上名册编号。
            # 直接 `state.history.append()` 会留下一整段"没有 who"的历史——渲染出来
            # 每行都不带说话人，她只能自己顺着上下文猜谁是谁（实测离线复现：
            # seed 进去的消息渲染成 `<message index="1">`，名册里也没有这些人）。
            # refresh_activity=False：补历史不是"刚刚有人说话"，别拿它刷新活跃时间。
            state.add(item, self.clock(), refresh_activity=False)
        if seeded:
            self._stats["history_seeded"] += len(seeded)
            logger.info(
                "Stage 3 history seeded: session=%s messages=%s",
                message.session_id,
                len(seeded),
            )
        return len(seeded)

    def snapshot(self) -> EngineSnapshot:
        """只读运行快照。内存数字每次读取时现取（取不到就是 0）。"""

        memory_now, memory_peak = process_memory_bytes()
        sessions = tuple(
            SessionSnapshot(
                session_id=session_id,
                mode=state.mode.value,
                history_size=len(state.history),
                topic=state.context.topic,
                topic_status=state.context.topic_status,
                persisted=self.state_store is not None and bool(state.history),
            )
            for session_id, state in self.sessions.states.items()
            if state.history
        )
        return EngineSnapshot(
            enabled=self.enabled,
            target_group_id=self.target_group_id,
            enabled_group_ids=tuple(sorted(self.enabled_group_ids)),
            sessions=sessions,
            memory=self.memory_service.snapshot() if self.memory_service else None,
            metrics=self.metrics.snapshot(),
            model_trace=self.logs.snapshot(),
            started_at=self.started_at,
            memory_bytes=memory_now,
            memory_peak_bytes=memory_peak,
            persistence_enabled=self.state_store is not None,
            focus=self.focus_stats(),
            **self._stats,
        )

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = bool(enabled)
        self.persist_state()

    # --- 权限判定 ---------------------------------------------------------

    def _control_scope_ok(self, message: IncomingMessage) -> bool:
        """管理员命令**在哪儿**能生效：**这个群的授权名单里有他**。

        管理员是群聊里的角色，而且是**一个群一个群授权的**：超管在哪个群发
        `/super addadmin`，那个人就只在那个群管事。私聊里不生效。

        超管不走这道判定（见 `_is_super_admin_control`）：超管是全局身份。

        注意这里**不按群启停过滤**：启停名单管的是"这个群的对话要不要处理"，
        不是"管理员能不能管这个群"。
        """

        group_id = message.target.group_id
        if group_id is None:
            return False
        # 管理员判定只有这一个入口；**没有例外**：超管放行走的是另一条路
        # （引用那条命令 + `/super permit` → 直接执行那一条），不往这里塞临时身份。
        return self.is_group_admin(message.user_id, group_id)

    def is_group_admin(self, user_id: str, group_id: str) -> bool:
        """这个人在这个群里有没有管理员权限。

        两条来源：配置里的部署级名单（对所有群生效），以及超管按群授的权。
        """

        if user_id in self.admin_user_ids:
            return True
        return user_id in self.group_admin_ids.get(group_id, ())

    def admin_groups_of(self, user_id: str) -> tuple[str, ...]:
        """这个人被授权管哪些群（不含配置级名单——那个覆盖所有群）。"""

        return tuple(sorted(g for g, admins in self.group_admin_ids.items() if user_id in admins))

    def all_admin_user_ids(self) -> set[str]:
        """所有有管理员权限的人（配置 ∪ 按群授权），只用于安全判定与统计。"""

        everyone = set(self.admin_user_ids)
        for admins in self.group_admin_ids.values():
            everyone.update(admins)
        return everyone

    def _admin_may_manage_group(self, message: IncomingMessage, group_id: str) -> bool:
        """这个管理员能不能**管**这个群（启停、清理、往这里转告）。

        只管"在哪个群下命令"是不够的：他在自己被授权的群里发
        `/admin disable 别人的群号` 一样能关掉别的群。所以凡是点名了目标群的命令，
        目标群也必须在他的授权范围里。超管与部署级名单不受限。
        """

        if message.user_id in self.super_admin_user_ids or message.user_id in self.admin_user_ids:
            return True
        return group_id in self.admin_groups_of(message.user_id)

    def _is_admin_control(self, message: IncomingMessage) -> bool:
        """发送者是不是**本群**的管理员。"""

        return self._control_scope_ok(message)

    def _is_super_admin_control(self, message: IncomingMessage) -> bool:
        """超管是**全局**的：只要是名单里的本人，在哪个群、私聊都算数。

        与管理员的分层不是"权限更大"，而是**适用范围不同**：管理员管的是群，
        超管管的是这台 bot 本身（进程诊断、记忆库、加管理员），这跟"在哪个群"
        没有关系。
        """

        return message.user_id in self.super_admin_user_ids

    # --- 超管命令回复 -----------------------------------------------------

    async def _super_reply(self, action: SuperAction, message: IncomingMessage):
        """渲染超管命令的回复。每一步都独立降级，任一失败不影响其它命令。

        返回 `str`（普通命令回复），或者 `OutgoingMessage`（`/super permit` 放行时
        直接给出那条命令自己的执行结果）。
        """

        if action is SuperAction.HELP:
            return await self._help_reply(message, title="云茹 · 超管命令", text=SUPER_HELP)
        if action is SuperAction.ADD_ADMIN:
            return self._add_admin(message)
        if action is SuperAction.DEL_ADMIN:
            return self._del_admin(message)
        if action is SuperAction.ADMIN_LIST:
            return self._admin_list()
        if action is SuperAction.PERMIT:
            # 唯一一条不返回文本的超管命令：放行会**执行**那条命令，回复就是执行结果。
            return await self._permit_reply(message, now=self.clock())
        if action in {SuperAction.AFFINITY, SuperAction.AFFINITY_RESET}:
            return self._affinity_reply(message, reset=action is SuperAction.AFFINITY_RESET)
        if action is SuperAction.PROFILE:
            return self._profile_reply(message)
        if action is SuperAction.PROCESSES:
            mode = parse_super_topic(message.text) or "default"
            note = None
            if mode == "cpu" and self.async_sender is not None:
                # CPU 要采 5 秒。用户要的是"先回 checking、5 秒后再发结果"，
                # 所以走延迟回发；没有这条缝就退化成同步等 5 秒再回一条。
                note = "超管诊断：正在采样 5 秒的 CPU 占用，结果随后发上来。"
                self._spawn_diagnostics(message.target, mode)
                return note
            try:
                return await self.runtime_diagnostics.processes(mode)
            except Exception:
                logger.exception("Stage 3 super processes failed")
                return "超管诊断失败：无法读取当前进程列表。"
        if action is SuperAction.LAN:
            try:
                return await self.runtime_diagnostics.network()
            except Exception:
                logger.exception("Stage 3 super lan failed")
                return "超管诊断失败：无法读取网络流量。"
        if action is SuperAction.FAN:
            try:
                return await self.runtime_diagnostics.fans()
            except Exception:
                logger.exception("Stage 3 super fan failed")
                return "超管诊断失败：无法读取风扇转速。"
        if action is SuperAction.RESTART:
            return self._request_restart(message)
        if action is SuperAction.STATUS:
            return self._super_status()
        if action is SuperAction.API_CHECK:
            return await self._super_apicheck()
        if action is SuperAction.MAIL:
            return self._mail_status()
        if action is SuperAction.MAIL_NOW:
            return await self._mail_send_now()
        return self._super_memory(action)

    def _spawn_diagnostics(self, target: MessageTarget, mode: str) -> None:
        """先回一句、稍后把诊断结果单独发出去（`/super processes cpu` 用）。

        命令回复一律 `paced=False`——工具性响应不该掺拟人化停顿或"收着"的延迟。
        发送失败只记日志：这是一条诊断回执，不该影响会话状态。
        """

        sender = self.async_sender

        async def run() -> None:
            try:
                text = await self.runtime_diagnostics.processes(mode)
            except Exception:
                logger.exception("Stage 3 super processes failed")
                text = "超管诊断失败：无法读取当前进程列表。"
            try:
                await sender(target, text)
            except Exception:
                logger.exception("Stage 3 diagnostics delivery failed")

        asyncio.create_task(run(), name=f"diagnostics-{mode}")

    def handles_command(self, message: IncomingMessage) -> bool:
        """这条消息会不会走**命令**那条路（插件 / `/admin help` / `/super ...`）。

        **只判断、不执行**，给主循环排序用（2026-09-30 用户："我发一个命令一分多钟才回复"）：
        命令回复只要几毫秒（不进模型），而一条对话要「判定 ~2s + 拟人停顿 2~4s」。
        严格串行时一条命令会排在几十条对话后面，实测等过一分多钟。
        所以主循环把命令提到前面——这个判断就是那个入口。

        注意它**不做鉴权**：没权限的人发 `/super ...` 同样是"命令"（会被静默丢掉），
        提前处理它没有副作用；把没权限的命令排到队尾反而更糟。
        """

        if message.is_bot_message:
            return False
        if self.commands.resolve(message) is not None:
            return True
        text = message.text or ""
        if is_admin_help_command(text):
            return True
        return parse_super_command(text) is not None

    def _plugin_level_allowed(self, level: str, message: IncomingMessage) -> bool:
        """这条命令的档位，这个人够不够。**判定只在核心**（插件只能声明档位）。

        - `public`：任何会话都允许（会话范围另由插件的 `session_allowed` 收窄）；
        - `admin`：本群管理员或超管；
        - `super`：超管（全局身份，群里私聊都一样）。

        不够时的语义与既有的 `/super ...` 一致：**完全静默**——不回复、不报错、
        不暴露层级存在，也不把这条交给模型（调用方看到 False 就直接 return None）。
        """

        if level == "super":
            return self._is_super_admin_control(message)
        if level == "admin":
            return self._is_admin_control(message) or self._is_super_admin_control(message)
        return True

    async def _plugin_action_reply(self, request, message: IncomingMessage):
        """把插件声明的动作意图真正执行掉（`ActionRequest` 的唯一执行点）。

        插件负责"认出命令、解析出目标与参数"，这里负责"能不能做、怎么做、记什么账"：
        权限档位在 `_plugin_level_allowed` 判过了，动作白名单在 `capabilities` 那道闸里，
        目标与参数护栏在 `group_admin.py` / `group_owner.py` 里，全是 WARNING 审计。
        插件从头到尾没拿到 `transport`。

        2026-10-01：取参与分发拆开了——`execute_action` 是那部分，这里只负责
        把"一条消息"折算成它的参数，然后包成 `OutgoingMessage`。
        面板要执行同一个动作时调的是 `execute_action`，**不是另写一份**。
        """

        text = await self.execute_action(
            request,
            group_id=message.target.group_id,
            actor_id=message.user_id,
            message_id=message.reply_to_message_id or "",
        )
        # 动作结果一律是"工具性响应"：立刻送出，不掺拟人化停顿。
        return OutgoingMessage(message.target, str(text or ""), paced=False)

    async def execute_action(self, request, *, group_id: str | None, actor_id: str,
                             message_id: str = "", mentioned: bool = False) -> str:
        """执行一个插件声明的动作，返回给"发起方"看的一句话。

        这是**动作执行的公开接缝**（2026-10-01 加）：群里命令与面板走同一条路，
        护栏与审计因此不可能漂移。参数表示"如果这条命令是某个人在某个群发的"：

        - `group_id`：动作作用在哪个群（面板显式给；命令用发命令的那个群）；
        - `actor_id`：**以谁的名义做**。群里命令是他自己；面板是**云茹自己**
          （用户 2026-10-01："群管理动作的 actor 为云茹"）——所以 `group_owner`
          那几个动作仍要求她当前确实是那个群的群主，面板不代替她越权。
        - `mentioned`：群里是"确实 @ 到了人"。面板上没有 @ 这个过程，调用方必须显式
          传 True（面板=已经确认过目标），否则 `kick` 会被护栏挡回（那是**有意的**：
          踢人必须明确指定，手滑给个群号就踢人是这条路上最典型的失误）。
        """

        from .command_plugins import ActionRequest

        assert isinstance(request, ActionRequest)  # pragma: no cover - 调用方已经判过
        # 群管理与群主动作的**执行端在插件目录里**（`plugins/group_admin/`）：它们只
        # 解析与声明意图，护栏、权限、调用仍在这里。插件目录整个不在时（只要 Stage 3
        # 的那份部署），下面两句 import 会失败 → **fail-closed**：不执行、记一行日志。
        # 这条分支上不会有插件产出这两种 `ActionRequest`，真收到只能是装配错了。
        if request.group in {"group_admin", "group_owner"}:
            try:
                if request.group == "group_admin":
                    from .plugins.group_admin.group_admin import execute as run_group_action

                    return await run_group_action(
                        request.kind,
                        # **一个已过闸门的调用函数**，不是活的 transport
                        # （2026-10-01 适配：原来这里传 `transport=self.transport`，
                        # 也就是插件目录里的函数拿到了传输层，而本函数的 docstring
                        # 还写着"插件从头到尾没拿到 transport"——那句当时是假的）。
                        call=self._group_action_caller("group_manage"),
                        group_id=group_id,
                        actor_id=actor_id,
                        target_id=request.target_id,
                        minutes=request.text,
                        message_id=message_id or request.message_id,
                        mentioned=mentioned or request.mentioned,
                        protected_ids=frozenset(self._protected_group_ids()),
                        enabled=dev_config.GROUP_MANAGE_ENABLED,
                    )
                from .plugins.group_admin.group_owner import execute as run_owner_action

                return await run_owner_action(
                    request.kind,
                    call=self._group_action_caller("group_owner"),
                    roles=getattr(self, "self_roles", None),
                    group_id=group_id,
                    actor_id=actor_id,
                    target_id=request.target_id,
                    text=request.text,
                    mentioned=mentioned or request.mentioned,
                    enabled=dev_config.GROUP_OWNER_ENABLED,
                )
            except ModuleNotFoundError:
                logger.warning("group_action_unavailable group=%s kind=%s",
                               request.group, request.kind)
                return "这条部署没有群管理能力。"
        logger.warning("Stage 3 unknown plugin action group=%s", request.group)
        return "这个动作没有对应的执行通道，已拒绝。"

    def _group_action_caller(self, purpose: str):
        """造一个**已过 `capabilities` 闸门**的群动作调用函数，交给插件目录里的执行函数。

        闸门在核心这条闭包里，插件侧只拿到 `(action, params) -> response`——
        它没有 transport，也没有 `capabilities` 可以自己查，所以绕不过闸门。

        两个来源，优先用装配点给的那个（`runtime._SeamBinder.action_caller`）：

        1. `plugin_registry.action_caller`——生产走这条；
        2. **没有注册表时在这里现造一个**（直接测引擎的用例是这样：它们只造一个
           `DialogueEngine`，不走 `build_engine`）。现造的也在**核心这一侧**，
           所以"插件拿不到 transport"这条性质不变。
        """

        registry = getattr(self, "plugin_registry", None)
        maker = getattr(registry, "action_caller", None)
        if callable(maker):
            return maker(purpose)
        transport = getattr(self, "transport", None)
        caps = getattr(self, "capabilities", None)
        if transport is None:
            return None

        async def call(action: str, params: dict[str, object] | None = None):
            if caps is not None:
                from .capabilities import CapabilityDenied
                from .plugins import ActionDenied

                # 与 `runtime._SeamBinder.call_action` 同一条规矩：
                # 这一族写动作 + 任何只读动作（回读要用）。
                try:
                    caps.check(action, purpose=purpose)
                except CapabilityDenied:
                    try:
                        caps.check(action, purpose="read")
                    except CapabilityDenied as exc:
                        raise ActionDenied(f"{action} 不允许用于 {purpose}") from exc
            result = await transport.call_api(action, params or {})
            from .onebot_client import unwrap_result

            return unwrap_result(result)

        return call

    def _protected_group_ids(self) -> set[str]:
        """群管理不许动的人：超管、配置级管理员、按群授权的管理员、以及她自己。

        「她自己」来自补上下文那层问过的 `get_login_info`；拿不到就不加这一条，
        对面 QQ 也会拒绝（她自己是群主时更是动不了）。
        """

        protected = set(self.super_admin_user_ids) | set(self.admin_user_ids)
        for admins in self.group_admin_ids.values():
            protected.update(admins)
        provider = getattr(self, "context_provider", None)
        if provider is not None and hasattr(provider, "self_id"):
            try:
                self_id = str(provider.self_id() or "")
            except Exception:  # noqa: BLE001 - 拿不到就别拦，对面也会拒
                self_id = ""
            if self_id:
                protected.add(self_id)
        return protected

    def _grant_group(self, message: IncomingMessage) -> str | None:
        """解析"授权作用在哪个群"。

        默认是**发命令的这个群**——超管在哪个群发的，那个人就管哪个群。
        也允许显式写群号（`/super addadmin @某人 123456789`），因为 addadmin 对
        正文里的多余内容一向宽容（@ 段才是身份），与其静默忽略一个群号，
        不如把它当明确的目标，写错就报错。

        私聊里必须显式给群号：那里没有"这个群"可言。
        """

        mentions = set(message.mentioned_user_ids)
        explicit = [
            token
            for token in message.text.split()
            # @ 到的 QQ 号也可能以文本形式出现，别把它当成群号。
            if token.isdigit() and len(token) >= 5 and token not in mentions
        ]
        candidate = explicit[-1] if explicit else (message.target.group_id or "")
        if not candidate or not 5 <= len(candidate) <= 12:
            return None
        return candidate

    def _add_admin(self, message: IncomingMessage) -> str:
        """`/super addadmin @某人 [群号]`：把人加为**某个群**的管理员，并立刻持久化。

        身份只认 @ 段里的 QQ 号（`mentioned_user_ids`），不认正文里写的号码——
        正文是聊天内容，@ 段才是明确的指定。授权范围默认是**发命令的这个群**。
        """

        group_id = self._grant_group(message)
        if group_id is None:
            return (
                "用法：/super addadmin @某人（在要授权的群里发；"
                "私聊里要写成 /super addadmin @某人 群号）"
            )
        targets = tuple(qq for qq in message.mentioned_user_ids if qq.isdigit())
        if not targets:
            return "用法：/super addadmin @某人（要 @ 到那个人，正文里写号码不算）"
        added, existed = [], []
        for user_id in targets:
            if user_id == message.user_id or user_id in self.super_admin_user_ids:
                existed.append(user_id)
                continue
            if self.is_group_admin(user_id, group_id):
                existed.append(user_id)
                continue
            self.group_admin_ids.setdefault(group_id, set()).add(user_id)
            added.append(user_id)
        if added:
            self.persist_state()
            logger.info(
                "Stage 3 admin granted group=%s by=%s count=%s",
                group_id, message.user_id, len(added),
            )
        parts = []
        if added:
            parts.append(f"已加为群 {group_id} 的管理员：" + "、".join(added))
        if existed:
            parts.append("本来就有权限：" + "、".join(existed))
        parts.append(self._admin_roster_line(group_id))
        return "\n".join(parts)

    def _del_admin(self, message: IncomingMessage) -> str:
        """`/super deladmin @某人 [群号]`：撤掉某人在**某个群**的管理员权限。

        身份同样只认 @ 段。**配置里写死的那部分撤不掉**——那是部署时定的，
        撤了重启也会回来；与其给一个"看起来成功、重启后失效"的假动作，
        不如当场说清楚为什么不行。
        """

        group_id = self._grant_group(message)
        if group_id is None:
            return (
                "用法：/super deladmin @某人（在要撤权的群里发；"
                "私聊里要写成 /super deladmin @某人 群号）"
            )
        targets = tuple(qq for qq in message.mentioned_user_ids if qq.isdigit())
        if not targets:
            return "用法：/super deladmin @某人（要 @ 到那个人，正文里写号码不算）"
        removed, configured, missing = [], [], []
        grants = self.group_admin_ids.get(group_id)
        for user_id in targets:
            if user_id in self._configured_admin_user_ids or user_id in self.super_admin_user_ids:
                configured.append(user_id)
                continue
            if grants and user_id in grants:
                grants.discard(user_id)
                removed.append(user_id)
                continue
            missing.append(user_id)
        if grants is not None and not grants:
            self.group_admin_ids.pop(group_id, None)
        if removed:
            self.persist_state()
            logger.info(
                "Stage 3 admin revoked group=%s by=%s count=%s",
                group_id, message.user_id, len(removed),
            )
        parts = []
        if removed:
            parts.append(f"已移出群 {group_id} 的管理员：" + "、".join(removed))
        if configured:
            parts.append(
                "配置里的管理员（或超管）不能在这里撤，要改部署配置："
                + "、".join(configured)
            )
        if missing:
            parts.append(f"本来就不是群 {group_id} 的管理员：" + "、".join(missing))
        parts.append(self._admin_roster_line(group_id))
        return "\n".join(parts)

    def _admin_list(self) -> str:
        """列出管理员：超管是全局的，管理员**按群**列。只有 QQ 号，没有聊天内容。"""

        lines = [f"超管（全局，{len(self.super_admin_user_ids)} 人）"]
        lines.extend(f"· {qq}" for qq in _sorted_qq(self.super_admin_user_ids))
        if self.admin_user_ids:
            lines.append(f"配置里的管理员（对所有群生效，{len(self.admin_user_ids)} 人）")
            lines.extend(f"· {qq}" for qq in _sorted_qq(self.admin_user_ids))
        lines.append(f"按群授权的管理员（{len(self.group_admin_ids)} 个群）")
        if not self.group_admin_ids:
            lines.append("· 无")
        budget = ADMIN_LIST_MAX_ROWS
        hidden = 0
        for group_id in sorted(self.group_admin_ids):
            roster = "、".join(_sorted_qq(self.group_admin_ids[group_id]))
            if budget <= 0:
                hidden += 1
                continue
            budget -= 1
            lines.append(f"· {group_id}：{roster}")
        if hidden > 0:
            lines.append(f"… 还有 {hidden} 个群未显示")
        return "\n".join(lines)

    def _affinity_reply(self, message: IncomingMessage, *, reset: bool) -> str:
        """`/super affinity @某人 [reset]`：看关系，或把它按回默认档。

        **只读或重置，没有逐档编辑**：可写的档位越少越安全，也免得"手刷好感"
        之后分不清哪些是真实互动攒出来的。
        """

        targets = tuple(qq for qq in message.mentioned_user_ids if qq.isdigit())
        if not targets:
            return "用法：/super affinity @某人（看这个人的关系现状）；加 reset 按回默认档"
        store = getattr(self.memory_service, "store", None)
        if store is None:
            return "当前没有接入长期记忆，关系信息不可用。"
        lines = []
        for user_id in targets:
            if reset:
                changed = store.reset_relationship(user_id, reason=f"超管重置 by={message.user_id}")
                self._stance_cache.pop(user_id, None)
                lines.append(f"{user_id}：{'已按回默认档（生疏 + 如常）' if changed else '本来就是默认档'}")
                continue
            detail = store.relationship_detail(user_id)
            closeness = int(detail["closeness"])
            guardedness = int(detail["guardedness"])
            lines.append(
                f"{user_id}：亲近 {_level_name(CLOSENESS_LABELS, closeness)}，"
                f"防备 {_level_name(GUARDEDNESS_LABELS, guardedness)}"
            )
            last = detail.get("last")
            if isinstance(last, dict):
                axis = "亲近" if last.get("axis") == "closeness" else "防备"
                lines.append(
                    f"  最近一次：{axis} {last.get('before_value')}→{last.get('after_value')}"
                    f"（{last.get('source')}）{last.get('reason') or ''}"
                )
            else:
                lines.append("  还没有变化记录。")
        return "\n".join(lines)

    async def _help_reply(self, message: IncomingMessage, *, title: str, text: str):
        """把一份帮助发出去：**优先图片，失败退回文字**。

        由来（2026-09-30 用户）："后面命令的 help 就用图片展示了"。帮助是一屏长的清单，
        群里刷一屏文字很吵；图片是一条消息。但**图不能变成单点故障**：

        - 没装 Pillow / 画不出来（`render` 返回空）→ 发文字；
        - 没接图片通道（测试、别的传输层）→ 发文字；
        - 图发出去了但是对面拒了/超时 → 记一笔再发文字。

        返回 `None` 表示"图已经发出去了，不用再发文字"。
        """

        if dev_config.HELP_IMAGE:
            sender = getattr(self, "image_sender", None)
            png = b""
            if sender is not None:
                from .help_card import render

                try:
                    png = render(title, text, width=dev_config.HELP_IMAGE_WIDTH)
                except Exception:  # noqa: BLE001 - 渲染异常按"画不出来"处理
                    logger.exception("Stage 3 help card render failed")
                    png = b""
            if png:
                try:
                    if await sender(message.target, png):
                        logger.info("Stage 3 help card sent: title=%s bytes=%s", title, len(png))
                        return None
                except Exception:  # noqa: BLE001 - 发图失败退回文字，帮助照样看得到
                    logger.warning("Stage 3 help card send failed; 退回文字")
        return OutgoingMessage(message.target, text, paced=False)

    def _profile_reply(self, message: IncomingMessage) -> str:
        """`/super profile @某人`：把那个人的**完整人物画像**发出来（发给群里）。

        由来（2026-09-30 用户）："加入 super 命令人物画像查看，super+xx+艾特某人输出
        完整人物画像到群里"。所以这里是**只读展示**，一个字都不写库；
        没画像时如实说"还没攒出来"，并退回她手里关于这个人的零散记录（称呼/边界），
        免得只回一句"无"看不出东西。
        """

        targets = tuple(qq for qq in message.mentioned_user_ids if qq.isdigit())
        if not targets:
            # 也接受直接写 QQ 号；都没给就看他自己的（超管通常想先看看自己那份）。
            found = re.findall(r"(?<!\d)\d{5,12}(?!\d)", message.text or "")
            targets = tuple(dict.fromkeys(found)) or (message.user_id,)
        store = getattr(self.memory_service, "store", None)
        if store is None:
            return "当前没有接入长期记忆，人物画像不可用。"
        blocks: list[str] = []
        blocks_by_person: dict[str, str] = {}
        for user_id in targets[:3]:
            # 同一个人的两个号被一起点名时只画一次：按**规范号**去重，
            # 否则他们会在群里看到同一份画像出现两遍（"她有两个人格"的错觉）。
            try:
                person = store.canonical_user(user_id)
            except Exception:  # noqa: BLE001
                person = user_id
            if person not in blocks_by_person:
                blocks_by_person[person] = self._profile_block(store, user_id, message)
        blocks.extend(blocks_by_person.values())
        return "\n\n".join(blocks)

    def _profile_block(self, store, user_id: str, message: IncomingMessage) -> str:
        """一个人的画像块：名字、关系档位、画像正文、更新时间。"""

        group_id = message.target.group_id
        name = ""
        try:
            names = store.person_names(group_id, [user_id]) if group_id else {}
            name = str(names.get(user_id) or "")
        except Exception:  # noqa: BLE001 - 名字拿不到不影响画像本身
            name = ""
        who = f"{name}（QQ {user_id}）" if name else f"QQ {user_id}"
        lines = [f"【人物画像】{who}"]
        try:
            detail = store.relationship_detail(user_id)
            lines.append(
                f"亲近 {_level_name(CLOSENESS_LABELS, int(detail['closeness']))}，"
                f"防备 {_level_name(GUARDEDNESS_LABELS, int(detail['guardedness']))}"
            )
        except Exception:  # noqa: BLE001
            pass
        profile = ""
        updated = 0.0
        try:
            record = store.profile_record(user_id)
            profile = str(getattr(record, "content", "") or "")
            updated = float(getattr(record, "updated_at", 0.0) or 0.0)
        except Exception:  # noqa: BLE001
            profile = ""
        lines.append("—— 她对这个人的印象 ——")
        if profile:
            lines.append(profile)
            if updated:
                lines.append(f"（更新于 {time.strftime('%m-%d %H:%M', time.localtime(updated))}）")
        else:
            lines.append("还没有画像（记忆维护 agent 还没攒出来）。")
            fallback = self._identity_lines(store, group_id, user_id)
            if fallback:
                lines.append("她手里关于这个人的零散记录：")
                lines.extend(fallback)
        # 出站正文上限 1000 字符：画像本身 ≤300 字，这里只是防多目标叠加时撞上限。
        return "\n".join(lines)[:900]

    def _identity_lines(self, store, group_id: str | None, user_id: str) -> list[str]:
        """退回展示"她记得你是谁、你的红线是什么"（就是判定用的那两条）。"""

        if not group_id:
            return []
        try:
            records = store.speaker_identity(group_id, user_id, limit=3)
        except Exception:  # noqa: BLE001
            return []
        lines = []
        for record in records:
            kind = {"name": "称呼", "boundary": "边界", "preference": "偏好"}.get(
                str(getattr(record, "kind", "")), str(getattr(record, "kind", "记录")))
            lines.append(f"  · {kind}：{str(getattr(record, 'content', ''))[:80]}")
        return lines

    def _admin_roster_line(self, group_id: str) -> str:
        granted = len(self.group_admin_ids.get(group_id, ()))
        return f"群 {group_id} 的管理员 {granted} 人（超管 {len(self.super_admin_user_ids)} 人，全局）。"

    # --- 放行一条被拒的命令（/super permit）-----------------------------------

    def _denied_admin_reply(self, message: IncomingMessage) -> OutgoingMessage | None:
        """无权限的 admin 命令：先**记下这条消息本身**（等超管引用它放行），再决定回不回。

        记的是整条 `IncomingMessage`（不是文本），因为超管的 `/super permit` 是靠
        **引用**那条消息来指定"就这一条"的：`reply_to_message_id` 是唯一不会认错的线索。
        只记**内存**、5 分钟就清，不写库、不写日志正文；命令原文（可能含转告正文）
        也只在这个窗口里短暂留在内存里。

        "记"与"回"都只针对**解析得出来的 admin 命令**：`/admin 随便写点什么` 这种
        不成形的文本更像手滑，既不该为它暴露层级，也没什么可"执行"的。
        """

        if parse_admin_command(message.text) is None:
            return None
        self._denied_admin[message.message_id] = (message, self.clock())
        # 剩下三种情况保持静默：
        # - 私聊：管理员命令在私聊里本来就不生效，回一句等于把人引到一条走不通的路上；
        # - 已经是管理员的人（哪怕是在别的群）：他清楚自己有没有权限，这句只是噪音；
        # - 配置关掉了说明（`QQBOT_ADMIN_DENY_NOTICE=0`）。
        if message.target.group_id is None:
            return None
        if message.user_id in self.all_admin_user_ids():
            return None
        if not _admin_deny_notice_enabled():
            return None
        return OutgoingMessage(message.target, _admin_deny_notice(), paced=False)

    def _recent_denied_admins(self, group_id: str) -> list[IncomingMessage]:
        """本群里还没被放行、也还没过期的被拒命令，新的在前。过期的顺手清掉。"""

        now = self.clock()
        for key, (_, at) in list(self._denied_admin.items()):
            if now - at > DENIED_ADMIN_TTL_SECONDS:
                self._denied_admin.pop(key, None)
        return [
            item for _, (item, _) in sorted(
                self._denied_admin.items(), key=lambda kv: kv[1][1], reverse=True
            )
            if item.target.group_id == group_id
        ]

    def _permit_usage(self, group_id: str) -> str:
        """没有可放行的对象时回什么：用法 + 本群最近几条被拒的命令（好让人照着引用）。"""

        lines = [
            "用法：引用那条被拒的 /admin 命令，再发 /super permit——我按原样执行那一条（只执行一次）。",
        ]
        recent = self._recent_denied_admins(group_id)
        if recent:
            lines.append("本群最近的被拒命令：")
            for item in recent[:3]:
                text = " ".join(item.text.split())
                if len(text) > 40:
                    text = text[:40] + "…"
                lines.append(f"· {item.user_id}：{text}")
            lines.append("引用其中一条再发 /super permit 就行。")
        else:
            lines.append("本群现在没有被拒的 admin 命令（只在 5 分钟内、且命令本身能解析出来时才记）。")
        return "\n".join(lines)

    async def _permit_reply(self, message: IncomingMessage, *, now: float):
        """超管**引用**一条被拒的 admin 命令 + `/super permit`：替他执行那一条。

        刻意不做"发一张票给某人、他自己再发一遍"那种临时授权（用户 2026-09-28 明确否掉）：
        放行的对象是**那条命令**，不是那个人的身份，所以：
        - 只认 `reply_to_message_id` 指到的那一条，执行完即从记忆里删掉（一次即消）；
        - 不给任何人留下可复用的权限，`_control_scope_ok` 里也不再有例外；
        - 换成别的命令当然不放行（那不是被引用的那一条）。
        """

        group_id = message.target.group_id or ""
        quoted = getattr(message, "reply_to_message_id", "") or ""
        if not quoted:
            return self._permit_usage(group_id)
        entry = self._denied_admin.get(quoted)
        if entry is None:
            return self._permit_usage(group_id)
        original, at = entry
        if now - at > DENIED_ADMIN_TTL_SECONDS:
            self._denied_admin.pop(quoted, None)
            return "这条命令已经超过放行时限（5 分钟），让他重发一次再放行。"
        if original.target.group_id != group_id:
            return "只能在命令被发出来的那个群里放行。"
        command = parse_admin_command(original.text)
        if command is None:  # 理论上进不来：记的时候已经解析过
            self._denied_admin.pop(quoted, None)
            return "这条已经不是能执行的管理员命令了。"
        self._denied_admin.pop(quoted, None)  # 一次即消：放行过就不再有效
        self._stats["admin_permit_executed"] = self._stats.get("admin_permit_executed", 0) + 1
        logger.info("Stage 3 admin command approved by super admin: kind=%s by=%s user=%s group=%s",
                    command.kind.value, message.user_id, original.user_id, group_id)
        return await self._run_admin_command(command, original, now=now)

    async def _run_admin_command(self, command: AdminCommand, message: IncomingMessage, *,
                                 now: float, check_scope: bool = True) -> OutgoingMessage:
        """执行一条已授权的 admin 命令（普通路径与超管放行路径共用同一段）。

        `check_scope=False` 只用于超管放行：那时越界与否由**超管**自己看过原文、
        引用着放了行，不再拿"发命令的人管哪些群"去卡他。
        """

        if check_scope:
            # 点名了目标群的命令，目标群也必须在授权范围里：不然他在自己被授权的
            # 群里发 `/admin disable 别人的群` 一样能关掉别人的对话。
            target_group = _admin_command_target_group(command)
            if target_group and not self._admin_may_manage_group(message, target_group):
                self._stats["ignored_messages"] += 1
                logger.warning(
                    "Stage 3 admin command out of scope: kind=%s user=%s target=%s",
                    command.kind.value, message.user_id, target_group,
                )
                return OutgoingMessage(
                    message.target, self._out_of_scope_reply(message, target_group), paced=False
                )
        if command.kind in {
            AdminCommandKind.RELAY,
            AdminCommandKind.SELECT,
            AdminCommandKind.CONFIRM,
            AdminCommandKind.CANCEL,
        }:
            return OutgoingMessage(
                message.target, await self._relay_reply(command, message, now), paced=False
            )
        return OutgoingMessage(
            message.target, self._admin_command_reply(command, message), paced=False
        )

    def _out_of_scope_reply(self, message: IncomingMessage, group_id: str) -> str:
        """管理员越界时的回复。

        这里**明确回一句**而不是静默回落：对方本来就是管理员（不然走不到这里），
        静默只会让他以为是 bot 卡了。回复只发在他自己发命令的那个群里，
        不会把"你有多少权限"泄露给外面。
        """

        mine = self.admin_groups_of(message.user_id)
        scope = "、".join(mine) if mine else "无"
        return (
            f"你只能管自己被授权的群（{scope}），群 {group_id} 不在里面。"
        )

    def _request_restart(self, message: IncomingMessage) -> str:
        """`/super restart`：这里只**记一个意图**，真正的重启由 runtime 做。

        由 runtime 做的原因是顺序：这条回复得先发出去，传输层（8080）也得先关干净，
        再去换进程；在引擎里直接 `os.execv` 会把回复连同连接一起掐掉，
        新进程还可能撞上还没松开的端口。
        """

        return self.request_restart(f"qq:{message.user_id}")

    def request_restart(self, source: str = "") -> str:
        """**任意入口**请求重启（`/super restart` 与面板共用这一个）。

        2026-10-01 加：面板上那个"重启"按钮不能自己 `os.execv`（同一个理由：顺序），
        所以它也只设这个标记——由 `serve` 在收尾时读走。
        """

        self._restart_requested = True
        logger.warning("Stage 3 restart requested by=%s", source or "unknown")
        return "正在重启，几秒后回来。"

    def consume_restart_request(self) -> bool:
        """取出并清掉"请求重启"标记；runtime 在关完传输层之后读它。"""

        requested = self._restart_requested
        self._restart_requested = False
        return requested

    def _super_status(self) -> str:
        """概览：这次重启之后的运行情况 + **从开始用到现在的累计**。

        **不含命中率与余额**——那两个是"对账"性质的东西，看的时候要单独看
        （`/super apicheck`）。概览里混进一堆成本数字，反而看不出"现在正常吗"。

        2026-09-30 用户："status 同理"——所以除了"本次重启之后"那一行，
        再加一行全时累计（账本 `ApiUsageStore` 里的 baseline + 本次进程）。
        """

        snapshot = self.snapshot()
        metrics = snapshot.metrics or {}
        latency = (metrics.get("model_latency") or {}) if isinstance(metrics, dict) else {}
        uptime = max(0.0, time.time() - snapshot.started_at) if snapshot.started_at else 0.0
        database = self._memory_database_bytes()
        store = getattr(self, "usage_store", None)
        current = {
            "accepted_messages": snapshot.accepted_messages,
            "replies": snapshot.replies,
            "judge_calls": snapshot.judge_calls,
            "model_calls": snapshot.model_calls,
            "blocked_messages": snapshot.blocked_messages,
        }
        all_time = store.all_time(current) if store is not None else {}
        lines = [
            "运行状态（本次重启之后）",
            f"已运行：{_format_duration(uptime)}   开始于：{_format_clock(snapshot.started_at)}",
            f"内存：{_format_bytes(snapshot.memory_bytes)}"
            + (f"（峰值 {_format_bytes(snapshot.memory_peak_bytes)}）" if snapshot.memory_peak_bytes else "")
            + (f"   数据目录：{_format_bytes(database)}" if database else ""),
            f"启用群：{'、'.join(snapshot.enabled_group_ids) or '无'}",
            f"短期会话：{len(snapshot.sessions)}   已补历史：{snapshot.history_seeded}",
            f"收到消息：{snapshot.accepted_messages}   忽略：{snapshot.ignored_messages}   "
            f"阻断：{snapshot.blocked_messages}   延迟：{snapshot.deferred_messages}",
            f"模型调用：{snapshot.model_calls}   已发回复：{snapshot.replies}   "
            f"判定调用：{snapshot.judge_calls}",
        ]
        if all_time:
            since = _format_clock(float(getattr(store, "since", 0.0) or 0.0))
            lines.append(
                f"累计（自 {since}）：收到 {all_time.get('accepted_messages', 0)} 条   "
                f"回复 {all_time.get('replies', 0)} 条   判定 {all_time.get('judge_calls', 0)} 次   "
                f"模型 {all_time.get('model_calls', 0)} 次"
            )
        if snapshot.reply_segments > snapshot.replies:
            lines.append(
                f"其中分段发送：{snapshot.reply_segments - snapshot.replies} 条"
                f"（共 {snapshot.reply_segments} 段，均 {snapshot.reply_segments / snapshot.replies:.1f} 段/次）"
            )
        if latency:
            lines.append(
                f"模型耗时：平均 {latency.get('avg', 0)}s  最大 {latency.get('max', 0)}s  "
                f"（样本 {latency.get('count', 0)}）"
            )
        focus = snapshot.focus or {}
        hot = focus.get("hot")
        if hot:
            lines.append(
                f"当值：{hot['session_id']} 已 {hot['seconds_on_duty']}s"
                f"（话题静了 {hot['since_related']}s，已回 {hot['replies']} 条）"
            )
        if focus.get("queued_total"):
            lines.append(
                f"排队等她：{focus['queued_total']} 条"
                f"（{len(focus.get('queued_sessions') or [])} 个会话）"
            )
        if snapshot.guard_raised:
            lines.append(
                f"关系：本次升防 {snapshot.guard_raised} 次"
                + (f"（被上限拒 {snapshot.guard_rejected} 次）" if snapshot.guard_rejected else "")
            )
        if snapshot.empty_forced_reply:
            lines.append(f"异常：判定说要回但回复空手 {snapshot.empty_forced_reply} 次")
        outbox = getattr(self, "outbox", None)
        if outbox is not None:
            stats = outbox.stats()
            if stats["pending"]:
                lines.append(
                    f"等她发出去：{stats['pending']} 条（连接回来会自动补上；"
                    f"本次已补 {stats['resent']} 条，放弃 {stats['dropped']} 条）"
                )
            elif stats["resent"] or stats["dropped"]:
                lines.append(
                    f"出站补发：已补 {stats['resent']} 条，放弃 {stats['dropped']} 条"
                )
        if self.memory_service is not None:
            memory = self.memory_service.snapshot()
            lines.append(f"记忆：已入队 {memory.get('queued_events', 0)} 条待处理")
        logs = snapshot.model_trace or {}
        if logs:
            parts = []
            for feature in sorted(logs):
                entry = logs[feature] if isinstance(logs[feature], dict) else {}
                parts.append(f"{feature} {entry.get('recorded', 0)}")
            lines.append(f"功能日志：{'  '.join(parts)}")
            lines.append(f"日志目录：{self.logs.directory}（合计 {_format_bytes(self.logs.total_bytes())}）")
        return "\n".join(lines)

    def _memory_database_bytes(self) -> int:
        """记忆库文件大小：它常驻在磁盘上，内存数字里看不到它。"""

        store = getattr(self.memory_service, "store", None)
        path = getattr(store, "path", None)
        try:
            return int(Path(path).stat().st_size) if path else 0
        except OSError:
            return 0

    def _agent_available(self) -> dict[str, bool]:
        """这个 agent 这次装配起来了没有（`/super apicheck` 用它区分"没启用"与"还没调用"）。

        看不到 client 的那几条（如写信 agent 由后台插件装配）返回 True——不假装知道，
        让用量数字自己说话。
        """

        return {
            "dialogue": self.client is not None or bool(self._session_clients),
            "judge": self.judge_client is not None or bool(self._session_judges),
            "memory": self.memory_client is not None,
            "review": getattr(self, "style_reviewer", None) is not None,
            "vision": getattr(self, "vision", None) is not None,
            "letter": True,
        }

    async def _super_apicheck(self) -> str:
        """按 agent 对账：命中率 + 余额。

        **每个 agent 一条**（判定 / 回复 / 记忆 / 风格审核 / 识图 / 写信），不能合成一个数：
        它们各有自己的 key（没配就回落主 key）与自己的缓存空间（各自的 user_id），
        混起来既看不出谁在退化，也会掩盖"某个 key 一次都没命中"。

        2026-09-30 用户要求"默认展示从开始使用到现在的，而不是重启后的"：所以主口径是
        `ApiUsageStore` 里跨重启累计的数字，本次进程的数字退成最后一行附注。
        同日用户又问"审核 agent 用量为什么不在 apicheck 里显示"——以前这里只列了三条，
        风格审核 / 识图 / 写信 的账都在账本里，却没有展示出来；现在按 `APICHECK_ROLES` 全列。
        """

        report = self.log_cache_report("apicheck")
        roles = tuple(role for role, _ in APICHECK_ROLES)
        names = dict(APICHECK_ROLES)
        available = self._agent_available()
        store = getattr(self, "usage_store", None)
        lines: list[str] = []
        if store is not None and getattr(store, "enabled", False):
            # 主口径：账本里跨重启累加的数字。用户要的就是这一行——
            # 以前只报"本次重启之后"，重启一次前面的账就再也看不到了。
            stats_by_role = store.usage_report(roles)
            since = _format_clock(float(getattr(store, "since", 0.0) or 0.0))
            lines.append(f"接口对账（从开始用到现在，自 {since}）")
            empty_note = "还没有调用"
        else:
            # 没接账本（老调用点、离线测试）：退回本次进程的口径，措辞标明"本次"。
            stats_by_role = report
            lines.append("接口对账（按 key 分开算）")
            empty_note = "本次还没有调用"
        for role in roles:
            stats = stats_by_role.get(role) or {}
            if not isinstance(stats, dict) or not int(stats.get("calls", 0)):
                # 区分"没装配"与"装配了但还没调用"——前者说明这个 agent 根本没开。
                note = "没启用" if available.get(role) is False else empty_note
                lines.append(f"· {names[role]}：{note}")
                continue
            hit = int(stats.get("hit_tokens", 0))
            miss = int(stats.get("miss_tokens", 0))
            total = hit + miss
            rate = f"{hit / total:.1%}" if total else "—"
            lines.append(
                f"· {names[role]}：调用 {stats['calls']} 次，命中率 {rate}"
                f"（命中 {_token_count(hit)} / 未命中 {_token_count(miss)}）"
            )
        # 本次进程的数字：接了账本才需要附这一行（没接时上面报的就是本次）。
        # 只在真有调用时出现，避免"两个口径都不清不楚"。
        if store is not None and getattr(store, "enabled", False):
            current: list[str] = []
            for role in roles:
                stats = report.get(role) or {}
                if isinstance(stats, dict) and stats.get("calls"):
                    hit = int(stats.get("hit_tokens", 0))
                    miss = int(stats.get("miss_tokens", 0))
                    total = hit + miss
                    rate = f"{hit / total:.1%}" if total else "—"
                    current.append(f"{names[role].split('（')[0]} {stats['calls']} 次 {rate}")
            if current:
                lines.append("本次启动：" + "；".join(current))
        previous = self.last_cache_report or {}
        if previous.get("reason") and previous.get("reason") != "apicheck":
            lines.append(
                f"（上次自动对账：{previous.get('reason')} @ {_format_clock(float(previous.get('at', 0) or 0))}）"
            )
        lines.append(await self._balance_line())
        return "\n".join(lines)

    def _mail_status(self) -> str:
        """邮箱与汇报的状态。收件人是常量，所以可以直说。"""

        reporter = getattr(self, "daily_reporter", None)
        if reporter is None:
            return "每日汇报未启用（QQBOT_MAIL_REPORT=0），或这台机器上还没有装配邮箱。"
        status = reporter.status()
        lines = [
            "邮箱与汇报",
            f"收件人：{status['recipient']}（配置里的常量，不从对话里取）",
            f"发送时间：每晚 {status['send_at']}",
            f"上次汇报：{_format_clock(float(status['last_report_at']))}"
            + ("" if status["last_report_at"] else "（还没发过）"),
            f"下次：{_format_clock(float(status['next_due_at']))}"
            + ("　现在就该发" if status["due_now"] else ""),
            f"今天还能重试：{status['tries_left_today']} 次",
            f"本次进程：成功 {status['sent']} 封，失败 {status['failures']} 次",
        ]
        sends = status.get("last_sends") or []
        if sends:
            lines.append("最近几次：")
            for item in sends:
                mark = "成功" if item.get("ok") else "失败"
                lines.append(
                    f"· {_format_clock(float(item.get('at', 0) or 0))} {mark}"
                    f"（{item.get('detail') or '-'}）"
                )
        letters = status.get("last_letters") or []
        if letters:
            # 只看主题与长度：正文可能写着群友的不是，而这条回复是**群里发的**。
            # 想看全文就去 data/mail_state.json（或者她本人那边问她）。
            lines.append("最近寄出的信：")
            for item in letters:
                lines.append(
                    f"· {_format_clock(float(item.get('at', 0) or 0))}"
                    f"「{item.get('subject') or '（无主题）'}」（正文 {item.get('body_len', 0)} 字）"
                )
        return "\n".join(lines)

    async def _mail_send_now(self) -> str:
        """立刻写一封发出去。用来"跑通一次"，也给超管一个不等到 23:00 的办法。"""

        reporter = getattr(self, "daily_reporter", None)
        if reporter is None:
            return "每日汇报未启用，无法发送。"
        draft = await reporter.run_once(self, force=True)
        if draft is None:
            return "这次没发出去（写不出来或发送失败）。看看日志里的原因。"
        return f"已寄出：{draft.subject}（正文 {len(draft.body)} 字）。"

    # --- 她写出去的信（"她还记得自己写了什么"） --------------------------------

    def note_letter(self, letter: dict) -> None:
        """记下一封刚发出去的信（最近在前）。`DailyReporter` 与 runtime 都往这里灌。"""

        cleaned = {
            "at": float(letter.get("at") or self.clock()),
            "to": str(letter.get("to") or ""),
            "subject": str(letter.get("subject") or "").strip(),
            "body": str(letter.get("body") or "").strip(),
        }
        if not (cleaned["subject"] or cleaned["body"]):
            return
        self.letter_history = [cleaned] + [
            item for item in self.letter_history if item.get("at") != cleaned["at"]
        ]
        del self.letter_history[LETTER_HISTORY_LIMIT:]

    def _letter_owner_ids(self) -> set[str]:
        """信是写给"一直照看她的那个人"的：部署级管理员与超管。

        刻意**不用** `all_admin_user_ids()`——那个还包含按群授权的人，别人只是某个群的
        管理员，不是她写信的对象。信里的内容（谁让她不痛快）不该递到旁人眼前。
        """

        return set(self._configured_admin_user_ids) | set(self.super_admin_user_ids)

    def _letter_note(self, message: IncomingMessage) -> dict | None:
        """这一轮要不要把"她写过的那封信"放到她眼前。**只有收信人本人能看**。

        两种情形给：① 刚发出去的那一封（`LETTER_FRESH_HOURS` 内）——不问也该知道；
        ② 他问起信/邮件时，把最近一封（`LETTER_RECALL_DAYS` 内）拿出来。
        其它时候不给：信是私下写给他的，不该变成她平时挂在嘴边的话题。
        """

        if not self.letter_history:
            return None
        if message.user_id not in self._letter_owner_ids():
            return None
        newest = self.letter_history[0]
        # 信的时间戳是**墙上时间**（`mail_state.json` 里存的是 `time.time()`），
        # 而 `self.clock` 是单调时钟（默认 `time.monotonic`，只用来算"过了多少秒"）。
        # 两者不能相减——混着用会让刚寄出的信算成 0 小时、"很久以前"算成负数。
        age_hours = max(0.0, (time.time() - float(newest.get("at") or 0.0)) / 3600.0)
        asked = isinstance(message.text, str) and bool(LETTER_ASK_RE.search(message.text))
        if age_hours > LETTER_FRESH_HOURS and not (
            asked and age_hours <= LETTER_RECALL_DAYS * 24
        ):
            return None
        return {
            "age_hours": age_hours,
            "subject": newest.get("subject") or "",
            "body": sanitize_chat_text(newest.get("body") or "", max_length=LETTER_BODY_CHARS),
            "asked": asked,
        }

    async def _balance_line(self) -> str:
        """余额。查不到就说查不到，不装作没这回事。"""

        try:
            from .balance_client import summarize
            from .builtin_balance_command import build_balance_client

            client = build_balance_client(
                dev_config.API_BASE_URL, dev_config.API_KEY,
                cache_seconds=dev_config.BALANCE_CACHE_SECONDS,
            )
            return "余额：" + summarize(await client.fetch())
        except Exception as exc:  # noqa: BLE001 - 余额查不到不该影响对账本身
            kind = getattr(exc, "kind", "") or type(exc).__name__
            return f"余额：查询失败（{kind}）"

    def _super_memory(self, action: SuperAction) -> str:
        """记忆查看。未接入记忆服务时给出明确说明而不是报错。"""

        if self.memory_service is None or getattr(self.memory_service, "store", None) is None:
            return "记忆服务未启用，无法查看。"
        store = self.memory_service.store
        try:
            if action is SuperAction.MEMORY:
                return build_overview(store.counts()).render()
            if action is SuperAction.MEMORY_RECORDS:
                records, total = store.list_records(limit=MAX_LIST_ITEMS)
                return build_records_view(records, total=total).render()
            if action is SuperAction.MEMORY_INBOX:
                items, total = store.list_inbox(limit=MAX_LIST_ITEMS)
                return build_inbox_view(items, total=total).render()
            if action is SuperAction.MEMORY_ARCHIVE:
                # 归档是删除的后悔药：只读展示，恢复一律走人工数据库操作。
                rows, total = store.list_archive(limit=MAX_LIST_ITEMS)
                return build_archive_view(rows, total=total).render()
            rows, total = store.list_audit(limit=MAX_LIST_ITEMS)
            return build_audit_view(rows, total=total).render()
        except Exception:
            logger.exception("Stage 3 super memory view failed")
            return "读取记忆库失败，请查看日志。"

    def leave_session(self, session_id: str) -> bool:
        existed = session_id in self.sessions.states or session_id in self.sessions.triggers
        self.sessions.leave(session_id)
        return existed

    def enable_group(self, group_id: str) -> bool:
        if not group_id.isdigit() or not 5 <= len(group_id) <= 12:
            return False
        before = group_id in self.enabled_group_ids
        self.enabled_group_ids.add(group_id)
        if not before:
            self.persist_state()
        return not before

    def disable_group(self, group_id: str) -> bool:
        if not group_id.isdigit() or not 5 <= len(group_id) <= 12:
            return False
        was_enabled = group_id in self.enabled_group_ids
        self.enabled_group_ids.discard(group_id)
        if was_enabled:
            self.sessions.leave(f"group:{group_id}")
            self.persist_state()
        return was_enabled

    def _commit_usage_counters(self) -> None:
        """把"全时总量 = 账本 baseline + 本次进程"写回账本。

        只写这一份，不逐个去改 `_stats[...] += 1`——那样要动七八处，还会漏。
        """

        store = getattr(self, "usage_store", None)
        if store is None:
            return
        try:
            store.commit_counters(store.all_time(self._stats))
        except Exception:  # noqa: BLE001 - 记账失败绝不影响对话
            logger.debug("api_usage_commit_failed", exc_info=True)

    def persist_state(self) -> bool:
        """把群启停名单、启停开关与短期会话写入本地状态库。

        未配置 state_store 时是空操作；写入失败只记录，不影响对话。

        顺带**把累计计数结一次账**（2026-09-30）：账本里存的是"之前几轮进程的累计"
        （baseline），展示时再加上本次进程的数字。放在这里是因为它本来就在状态变化时
        被调用，且内部有节流（最多丢最近几秒的数字，对"一共回了多少条"无所谓）。
        """

        self._commit_usage_counters()
        if self.state_store is None:
            return False
        return self.state_store.save({
            "enabled": self.enabled,
            "enabled_group_ids": sorted(self.enabled_group_ids),
            "target_group_id": self.target_group_id,
            "sessions": self.sessions.export(max_sessions=MAX_PERSISTED_SESSIONS),
            # 授权按群落盘：`/super addadmin` 在哪个群发的，就记在哪个群下面。
            # 配置里那份不写（每次启动从配置读回来）。旧版本状态文件里可能还有
            # 一份没有群信息的全局名单，恢复时不会被当成权限（见 restore_state）。
            "group_admins": {
                group_id: sorted(admins)
                for group_id, admins in sorted(self.group_admin_ids.items())
                if admins
            },
        })

    def restore_state(self) -> dict[str, int]:
        """启动时恢复持久化状态；任何异常都退化为默认值。"""

        if self.state_store is None:
            return {"groups": 0, "sessions": 0, "admins": 0}
        persisted = self.state_store.load()
        self.enabled = persisted.enabled
        if persisted.enabled_group_ids:
            self.enabled_group_ids = set(persisted.enabled_group_ids)
        restored = self.sessions.restore(persisted.sessions)
        # 授权按群恢复：state 里存的就是"哪个群、哪几个人"。
        for group_id, admins in persisted.group_admins.items():
            self.group_admin_ids.setdefault(group_id, set()).update(admins)
        if persisted.admin_user_ids:
            # 旧版本写过一份**没有群信息**的全局管理员名单。它没法平移成按群授权，
            # 照搬就等于把权限放大到所有群——所以只提示，不生效（fail-closed）。
            logger.warning(
                "runtime_state_admin_user_ids_ignored count=%s（旧版全局名单，"
                "请用 /super addadmin 在目标群里重新授权）",
                len(persisted.admin_user_ids),
            )
        return {
            "groups": len(self.enabled_group_ids),
            "sessions": restored,
            "admins": len(self.all_admin_user_ids()),
        }

    def drain_relay_deliveries(self) -> tuple[RelayDelivery, ...]:
        deliveries = tuple(self._relay_deliveries)
        self._relay_deliveries.clear()
        return deliveries

    def record_sent_reply(self, message: IncomingMessage, text: str) -> None:
        """Only a successful normal model reply may become YunRu inbox evidence."""
        expected = self._pending_memory_replies.pop(message.message_id, None)
        if expected == text and self.memory_service is not None:
            self.memory_service.record_sent_reply(message, text)

    def _purge_expired_relays(self, now: float) -> None:
        expired = [
            token
            for token, pending in self._pending_relays.items()
            if now - pending.created_at > RELAY_CONFIRMATION_TIMEOUT_SECONDS
        ]
        for token in expired:
            self._pending_relays.pop(token, None)

        expired_selections = [
            token
            for token, pending in self._pending_relay_selections.items()
            if now - pending.created_at > RELAY_CONFIRMATION_TIMEOUT_SECONDS
        ]
        for token in expired_selections:
            self._pending_relay_selections.pop(token, None)

    def _new_relay_token(self) -> str:
        token = secrets.token_hex(4)
        while token in self._pending_relays or token in self._pending_relay_selections:
            token = secrets.token_hex(4)
        return token

    def _queue_pending_relay(
        self,
        message: IncomingMessage,
        target: MessageTarget,
        target_label: str,
        text: str,
        now: float,
    ) -> str:
        # 转告是"以 bot 的身份往那边发消息"，所以往群里的转告同样受按群授权约束。
        # 放在这个收口处判：群号、群名、以及重名选择三条路径最后都落到这里。
        if target.group_id is not None and not self._admin_may_manage_group(message, target.group_id):
            logger.warning(
                "Stage 3 relay target out of scope: user=%s target=%s",
                message.user_id, target.group_id,
            )
            return self._out_of_scope_reply(message, target.group_id)
        token = self._new_relay_token()
        self._pending_relays[token] = PendingRelay(
            token=token,
            admin_user_id=message.user_id,
            target=target,
            target_label=target_label,
            text=text,
            created_at=now,
        )
        return (
            f"待确认转告：\n目标：{target_label}\n内容：{text}\n"
            f"请确认：/admin confirm {token}\n"
            f"取消：/admin cancel {token}\n有效期：{int(RELAY_CONFIRMATION_TIMEOUT_SECONDS)} 秒"
        )

    async def _relay_reply(self, command: AdminCommand, message: IncomingMessage, now: float) -> str:
        self._purge_expired_relays(now)
        if command.kind is AdminCommandKind.RELAY:
            text = sanitize_chat_text(command.content or "", max_length=RELAY_CONTENT_MAX_LENGTH).strip()
            if not text:
                return "转告内容不能为空。"
            if command.target_kind in {"group", "user"}:
                target_id = command.group_id or ""
                target = MessageTarget(group_id=target_id) if command.target_kind == "group" else MessageTarget(user_id=target_id)
                target_label = f"群聊 {target_id}" if command.target_kind == "group" else f"QQ {target_id}"
                return self._queue_pending_relay(message, target, target_label, text, now)

            query = sanitize_chat_text(command.target_query or "", max_length=128).strip()
            if not query:
                return "转告目标名称不能为空。"
            if self.relay_target_resolver is None:
                return "当前未配置名称查询，请暂时使用群号或 QQ 号转告。"
            try:
                candidates = await self.relay_target_resolver.resolve(command.target_kind or "", query)
            except Exception:
                logger.exception("转告目标名称查询失败：kind=%s", command.target_kind)
                return "查询转告目标失败，请暂时使用群号或 QQ 号重试。"
            if not candidates:
                return f"没有找到名称为“{query}”的转告目标。"
            if len(candidates) > 1:
                token = self._new_relay_token()
                self._pending_relay_selections[token] = PendingRelaySelection(
                    token=token,
                    admin_user_id=message.user_id,
                    target_kind=command.target_kind or "",
                    query=query,
                    text=text,
                    candidates=candidates,
                    created_at=now,
                )
                lines = [
                    f"名称“{query}”匹配到多个目标，请选择：",
                    *[f"{index}. {candidate.label}" for index, candidate in enumerate(candidates, start=1)],
                    f"内容：{text}",
                    f"请选择：/admin select {token} 序号",
                    f"有效期：{int(RELAY_CONFIRMATION_TIMEOUT_SECONDS)} 秒",
                ]
                return "\n".join(lines)
            candidate = candidates[0]
            return self._queue_pending_relay(message, candidate.target, candidate.label, text, now)

        if command.kind is AdminCommandKind.SELECT:
            token = command.token or ""
            selection = self._pending_relay_selections.get(token)
            if selection is None:
                return "找不到这条名称选择，可能已确认、取消或过期。"
            if selection.admin_user_id != message.user_id:
                return "这条名称选择只能由发起它的管理员操作。"
            index = command.index or 0
            if not 1 <= index <= len(selection.candidates):
                return f"序号无效，请选择 1 到 {len(selection.candidates)}。"
            self._pending_relay_selections.pop(token, None)
            candidate = selection.candidates[index - 1]
            return self._queue_pending_relay(message, candidate.target, candidate.label, selection.text, now)

        token = command.token or ""
        pending = self._pending_relays.get(token)
        if pending is None:
            return "找不到这条转告，可能已确认、取消或过期。"
        if pending.admin_user_id != message.user_id:
            return "这条转告只能由发起它的管理员确认或取消。"
        if command.kind is AdminCommandKind.CANCEL:
            self._pending_relays.pop(token, None)
            return f"已取消向{pending.target_label}的转告。"

        self._pending_relays.pop(token, None)
        self._relay_deliveries.append(
            RelayDelivery(target=pending.target, text=pending.text, admin_target=message.target)
        )
        return f"已确认，正在向{pending.target_label}发送转告：{pending.text}"

    def _admin_echo(self, command: AdminCommand) -> str:
        """`/admin echo <正文>`：原样发回去（公告/校对用）。

        两处刻意的处理：
        - **正文是数据**：它不会再被当成命令解析（解析在上一层只做一次），所以
          "echo /super restart" 只会把这句话发出来，不会真的重启；
        - `@` 换成全角 `＠`：避免有人借 echo 去 @全体成员 刷屏（群里喊人的正经需求
          用真 @ 就行，不需要绕这一道）。
        """

        raw = sanitize_chat_text(command.content or "", max_length=200).strip()
        if not raw:
            return "管理员命令：`/admin echo` 后面要写正文。"
        return raw.replace("@", "＠")

    def _admin_command_reply(self, command: AdminCommand, message: IncomingMessage) -> str:
        if command.kind is AdminCommandKind.ECHO:
            return self._admin_echo(command)
        if command.kind is AdminCommandKind.HELP:
            # 与 /admin help 共用同一份说明。两者都只对管理员生效，
            # 所以不会把管理员命令清单暴露给普通群成员。
            return ADMIN_HELP
        if command.kind is AdminCommandKind.STATUS:
            groups = "、".join(sorted(self.enabled_group_ids)) or "无"
            return f"当前启用群聊：{groups}"
        # enable / disable / clear 一律作用于**发命令的这个群**：管理员管的是自己
        # 所在的群，点名别人的群号没有意义，只是多一处出错的地方。
        group_id = message.target.group_id or ""
        if not group_id:
            return "这条命令要在群里发：它管的就是你发命令的那个群。"
        if command.group_id:
            return (
                "群号参数已经去掉了：这条命令只管你发命令的这个群"
                f"（现在就是 {group_id}）。"
            )
        if command.kind is AdminCommandKind.ENABLE:
            changed = self.enable_group(group_id)
            return f"本群（{group_id}）已开启对话。" if changed else f"本群（{group_id}）本来就开着。"
        if command.kind is AdminCommandKind.DISABLE:
            changed = self.disable_group(group_id)
            return f"本群（{group_id}）已关闭对话。" if changed else f"本群（{group_id}）当前未开启。"
        existed = self.leave_session(f"group:{group_id}")
        return (
            f"本群（{group_id}）的短期对话状态已清理。"
            if existed
            else f"本群（{group_id}）当前没有短期对话状态。"
        )

    async def handle(self, message: IncomingMessage) -> OutgoingMessage | None:
        """处理一条输入；返回要发送的目标和正文，否则表示不发送。

        `message` 可能是**被焦点排队过、现在轮到它那个群**的消息：这种消息在到达时
        就已经过了去重、也已经记进历史，所以这次要跳过那两步（不然历史里会出现两遍）。
        """

        if message.is_bot_message:
            self._stats["ignored_messages"] += 1
            logger.debug("Stage 3 ignored bot-originated message")
            return None
        resumed = message.message_id in self._resumed_ids
        # 命令走插件注册表。**分两步**：先 resolve（只匹配），由核心判档位，再 run（执行）。
        # 权限判定留在核心是刻意的——插件拿不到 admin_user_ids，也没法自行放行
        # （`docs/ADD_A_COMMAND.md` §"需要权限的命令怎么办"）。
        plugin = self.commands.resolve(message)
        if plugin is not None:
            from .command_plugins import ActionRequest, ImageReply, level_of

            level = level_of(plugin)
            if not self._plugin_level_allowed(level, message):
                # fail-closed：命令的**存在**与层级都不暴露，也不交给模型。
                self._stats["ignored_messages"] += 1
                logger.info(
                    "Stage 3 ignored plugin command by level: name=%s level=%s user=%s group=%s",
                    getattr(plugin, "name", "?"), level, message.user_id, message.target.group_id,
                )
                return None
            if not self.deduplicator.accept(message.message_id):
                self._stats["ignored_messages"] += 1
                logger.debug("Stage 3 ignored duplicate plugin command: id=%s", message.message_id)
                return None
            self._stats["accepted_messages"] += 1
            logger.info(
                "Stage 3 plugin command: name=%s level=%s group=%s private=%s",
                getattr(plugin, "name", "?"),
                level,
                message.target.group_id,
                message.target.user_id is not None,
            )
            plugin_reply = await self.commands.run(plugin, message)
            if plugin_reply is None:
                return None
            if isinstance(plugin_reply, ImageReply):
                # 插件只声明"这份内容适合画成卡片"，画与发都在核心（也可以退回文字）。
                return await self._help_reply(
                    message, title=plugin_reply.title, text=plugin_reply.text
                )
            if isinstance(plugin_reply, ActionRequest):
                # 插件只声明意图；权限、角色、闸门、审计、真调 action 全在这里。
                return await self._plugin_action_reply(plugin_reply, message)
            return OutgoingMessage(message.target, plugin_reply, paced=False)
        # /admin help 与 /super help 的先后：**菜单本身是公开的**，
        # 谁发都能看到管理员命令清单（用户 2026-09-28 要求"普通成员可以呼叫 /admin help
        # 调起 help 菜单"），没权限的人在末尾多读一段"怎么让超管放行一次"。
        # `/super help` 仍然只给超管：那一层不是"菜单"，是这台 bot 自己的操作面。
        is_super_admin = self._is_super_admin_control(message)
        # 超管是全局的，管理员命令对它没有额外限制：分层是"超管 ⊃ 管理员"，
        # 不该出现"是超管却用不了 /admin"的怪事。
        is_admin_control = self._is_admin_control(message) or is_super_admin
        if is_admin_help_command(message.text):
            if not self.deduplicator.accept(message.message_id):
                self._stats["ignored_messages"] += 1
                return None
            self._stats["accepted_messages"] += 1
            logger.info("Stage 3 admin help: user=%s granted=%s",
                        message.user_id, is_admin_control)
            text = ADMIN_HELP if is_admin_control else ADMIN_HELP + ADMIN_HELP_GUEST_NOTE
            return await self._help_reply(message, title="云茹 · 管理员命令", text=text)
        super_action = parse_super_command(message.text)
        if super_action is not None and is_super_admin:
            if not self.deduplicator.accept(message.message_id):
                self._stats["ignored_messages"] += 1
                return None
            self._stats["accepted_messages"] += 1
            logger.info(
                "Stage 3 super command: action=%s user=%s", super_action.value, message.user_id
            )
            reply = await self._super_reply(super_action, message)
            # `None` = 已经用图片发出去了（帮助卡片），不用再发文字。
            if reply is None:
                return None
            # 放行那条会直接给出"被执行命令"的回复（目标和节奏都在里面）。
            if isinstance(reply, OutgoingMessage):
                return reply
            return OutgoingMessage(message.target, reply, paced=False)
        # 长得像命令但**这一层**没权限的：一概不落到模型、不进历史。
        # 注意按层级分别判：一个管理员发 `/super ...` 同样是没权限。
        # 收尾分两种：`/admin ...` 回一句说明（用户 2026-09-28 要求，也是 permit 的由来），
        # `/super ...` 仍然完全静默——那一层的存在对不是超管的人不该可见。
        privileged_level = privileged_command_level(message.text)
        if (privileged_level == "super" and not is_super_admin) or (
            privileged_level == "admin" and not is_admin_control
        ):
            self._stats["ignored_messages"] += 1
            logger.info(
                "Stage 3 ignored unauthorized privileged command: level=%s user=%s group=%s private=%s",
                privileged_level,
                message.user_id,
                message.target.group_id,
                message.target.user_id is not None,
            )
            if privileged_level == "admin":
                # 无权限的 admin 命令：记下这一条，并按配置回一句说明。
                # 这与 AGENTS.md 的"未授权时静默回落、不暴露层级存在"有张力——
                # 那条是为了不暴露层级；用户明确要求回一句，并留了
                # `QQBOT_ADMIN_DENY_NOTICE=0` 一键退回静默。
                # `/super ...`（level=super）一律不记不回：那一层对不是超管的人不该可见。
                return self._denied_admin_reply(message)
            return None
        if not self.enabled:
            self._stats["ignored_messages"] += 1
            logger.debug("Stage 3 disabled; ignored message")
            return None
        # 会话闸门：管理员命令在群里生效（超管已经在上面的分支处理完），
        # 普通对话只处理已启用的群与私聊白名单。
        allowed_group = message.target.group_id in self.enabled_group_ids
        allowed_private = (
            message.target.user_id is not None
            and message.target.user_id in self.private_debug_user_ids
        )
        if not allowed_group and not allowed_private and not is_admin_control:
            self._stats["ignored_messages"] += 1
            # 近似 ping 的消息出现在未启用会话时也记一条，便于区分"没收到"和"没匹配上"。
            if looks_like_ping_command(message.text):
                logger.info(
                    "Stage 3 ping-like message ignored by session filter: group=%s private=%s",
                    message.target.group_id,
                    message.target.user_id is not None,
                )
            # **明确找她的消息**更要记一条 INFO：这个群没开时，普通聊天一律静默丢弃，
            # 日志里一个字都没有——2026-09-28 的现场就是"云茹怎么不说话了"，
            # 最后只能去翻安全日志才知道这个群被人 `/admin disable` 关掉了。
            elif (message.is_bot_mentioned or bool(message.reply_to_message_id)
                  or mentions_name(message.text or "")):
                logger.info(
                    "Stage 3 ignored: 群未启用，这句是在叫她（group=%s user=%s；"
                    "在那个群发 /admin enable 就能打开）",
                    message.target.group_id,
                    message.user_id,
                )
            logger.debug("Stage 3 ignored non-target session: %s", message.session_id)
            return None
        if not resumed and not self.deduplicator.accept(message.message_id):
            self._stats["ignored_messages"] += 1
            logger.debug("Stage 3 ignored duplicate message: id=%s", message.message_id)
            return None

        self._stats["accepted_messages"] += 1
        now = self.clock()
        admin_command = parse_admin_command(message.text)
        if admin_command is not None:
            logger.info(
                "Stage 3 admin command candidate: kind=%s user=%s group=%s private=%s",
                admin_command.kind.value,
                message.user_id,
                message.target.group_id,
                message.target.user_id is not None,
            )
        elif any(keyword in message.text for keyword in ("转告", "转发", "进程", "super", "超管")):
            logger.info(
                "Stage 3 possible admin command rejected by parser: user=%s group=%s private=%s",
                message.user_id,
                message.target.group_id,
                message.target.user_id is not None,
            )
        elif looks_like_ping_command(message.text):
            # 位于允许会话里、但没通过 ping 精确匹配：会被当作普通聊天送给模型。
            # 记一条提示，避免"发了 yunru ping 相关的话却没反应"只能靠猜。
            logger.info(
                "Stage 3 ping-like message not matched; treated as chat: group=%s private=%s",
                message.target.group_id,
                message.target.user_id is not None,
            )
        # **有权限、但这条命令没被认出来**：`/super 打错的命令`、`/admin 不存在的动作`。
        # 必须在这里**明确收尾**，不能让它继续往下走：
        # 2026-09-30 真机踩过——`/super notice` 因为正文是多行没匹配上（正则跨不过换行），
        # 掉进了对话路径，又被安全过滤（`sensitive_local_request`）拦下，而那条拦截的回复
        # 是空串，结果用户看到的是"公告发不出去，还一个字都没回"。
        # 命令前缀就是命令：认不出来就回一句用法，不进模型、也不进安全判定。
        # 位置很关键：**必须在 admin 命令能正常派出之前不拦截已认出的命令**——
        # 这里用 `admin_command is None` 把已认出的让过去（`/admin enable` 之类照旧）。
        if admin_command is None and privileged_level is not None:
            if privileged_level == "super" or (privileged_level == "admin" and is_admin_control):
                logger.info(
                    "Stage 3 unrecognised privileged command: level=%s user=%s group=%s",
                    privileged_level, message.user_id, message.target.group_id,
                )
                hint = "/super help" if privileged_level == "super" else "/admin help"
                return OutgoingMessage(
                    message.target,
                    f"这条 {privileged_level} 命令我没认出来。发 {hint} 看全部可用命令。",
                    paced=False,
                )
        security = check_message_security(message, self.all_admin_user_ids())
        if security.blocked:
            self._stats["blocked_messages"] += 1
            logger.warning(
                "Stage 3 blocked request: reason=%s user=%s role=%s",
                security.reason,
                message.user_id,
                message.sender_role,
            )
            self._log_security(message, security)
            if not security.reply:
                return None
            return OutgoingMessage(message.target, security.reply, paced=False)
        # 放行也记一条：拦截与放行是同一套规则的两种结果，只记拦截看不出规则有没有放宽。
        self._log_security(message, security)

        if admin_command is not None:
            if not is_admin_control:
                # 无权限（@ 前缀写法会落到这里，`/admin` 打头的在上一道闸就返回了）：
                # 记下这条命令，并按配置回一句说明。
                self._stats["ignored_messages"] += 1
                logger.warning("Stage 3 ignored unauthorized admin command: user=%s group=%s",
                               message.user_id, message.target.group_id)
                return self._denied_admin_reply(message)
            return await self._run_admin_command(admin_command, message, now=now)
        if is_admin_control and message.target.group_id is not None and not allowed_group:
            # 管理员在**没开对话的群**里闲聊不处理（只认他们的命令）。
            # 私聊不走这一条：那把超管折进管理员判定之后，他自己的私聊会被这里静默吃掉。
            # 这条路径原本**一个字都不记**：2026-09-28 用户问"云茹怎么不说话了"，
            # 而当时他正在一个被 `/admin disable` 关掉的群里说话——超级管理员在没开的群里
            # 说什么都会被这里吃掉，日志里却查不到。现在记一条，写明原因和怎么开回来。
            self._stats["ignored_messages"] += 1
            logger.info(
                "Stage 3 ignored admin message in a disabled group: group=%s user=%s "
                "（这个群没开对话，只有命令会回；在那个群发 /admin enable 就打开）",
                message.target.group_id,
                message.user_id,
            )
            return None

        if self.memory_service is not None:
            self.memory_service.capture(message)
        state = self.sessions.state(message.session_id)
        # 补一次对话背景：Bot 只看得见自己启动后的消息，@ 之前群里聊了什么
        # 一概不知道，于是只能对每句话都给泛泛的反应。按群限流，失败即跳过。
        await self._seed_history(message, state)
        active = self.sessions.is_active(state, now)
        # “是否在叫她”：@、引用回复她的消息、或正文提名，任一成立即算。
        # 只回答"有没有叫她"，该不该开口仍交给模型按群聊分寸判断。
        addressed = is_addressed_to_bot(message, state.recent())
        # 群聊里不锁死单一对话对象。旧的锁焦逻辑会把"她正和 A 说话时 B 的发言"
        # 整条丢掉，结果她在群里表现得像在私聊：看不见别人，也就接不住群话题。
        # 现在群聊中每条消息都送进模型，由模型决定接话还是 NO_REPLY；
        # 私聊仍保持一对一，不做任何放开。
        group_chat = message.session_id.startswith("group:")
        # --- 焦点门：同一时刻只有一段对话在当值 -------------------------------
        # 热群照常处理（它正在当值）；私聊不进焦点，与群并行、优先处理；
        # 其余（冷群）只把消息记成背景，**明确找她**的进队列：
        #   @ 她 → 先回一句"稍等"（同群 120 秒冷却）；提及/引用 → 静默进队列。
        # 没有人在当值时，走下面的 20 条巡检路子；巡检同一时刻只跑一个。
        if group_chat and not allowed_private and not is_admin_control and not resumed:
            if not self.focus.is_hot(message.session_id):
                if self.focus.hot is not None or self.focus.sweeping:
                    self._stats["deferred_messages"] += 1
                    # 做法一：消息**到达即进该群历史**（只作背景、不调模型），
                    # 所以她轮到这个群时能看见"她不在的时候群里聊了什么"。
                    state.add(message, now, refresh_activity=False)
                    if addressed:
                        queued = self.focus.enqueue(message.session_id, message)
                        # 标记为"已记过历史、且已过去重"：轮到它时 handle() 要跳过那两步。
                        self._resumed_ids.add(message.message_id)
                        self._stats["focus_queued"] += 1
                        logger.info(
                            "Stage 3 focus: queued for later session=%s depth=%s",
                            message.session_id,
                            queued,
                        )
                        if message.is_bot_mentioned and self.focus.ack_allowed(message.session_id, now):
                            self.focus.note_ack(message.session_id, now)
                            ack = self._pick_ack()
                            # 回过"稍等"的，之后必须给结果：判定不许否决它。
                            self._promised_ids.add(message.message_id)
                            if len(self._promised_ids) > 256:
                                self._promised_ids.pop()
                            # "稍等"要进她的历史（她之后看得到自己说过），但**不进长期记忆**、
                            # 也不算进"连回 50 条"——它不是一个真实回应。
                            state.record_bot_reply(message.session_id, message.target, ack, now)
                            self._stats["focus_acks"] += 1
                            return OutgoingMessage(message.target, ack, paced=False)
                    logger.debug(
                        "Stage 3 focus: deferred to background session=%s", message.session_id
                    )
                    return None
                # 没人在当值：下面是巡检路径，稍后在真正要调模型前抢巡检锁。
        if (
            active
            and state.active_user_id is not None
            and message.user_id != state.active_user_id
            and not addressed
            and not message.has_media
            and not (group_chat and self.group_listen)
        ):
            # 私聊场景下仍不让第三方插话抢走焦点，也不因无关消息刷新活跃超时。
            state.add(message, now, refresh_activity=False)
            self._stats["deferred_messages"] += 1
            logger.debug(
                "Stage 3 deferred other participant: session=%s active_user=%s message_user=%s",
                message.session_id,
                state.active_user_id,
                message.user_id,
            )
            return None
        # 群聊里"别人说话"只提供背景，不刷新活跃超时；否则一句群闲聊就能把
        # 对话无限续期，活跃期再也结束不了。
        refreshes = message.user_id == state.active_user_id or addressed or message.has_media
        resumed = message.message_id in self._resumed_ids
        if not resumed:
            state.add(message, now, refresh_activity=refreshes)
        if active:
            mode = ConversationMode.ACTIVE
            trigger = "active_message"
        else:
            mode = ConversationMode.IDLE
            # 被叫到就强制触发：@、引用回复、或正文里叫了她的名字（addressed 含这三样），
            # 否则"叫她名字却不带 @"会被当成群聊噪声压到 20 条才处理。
            #
            # **群里的图不再单独强制触发**（2026-09-27 修正）。原来只要 `has_media` 就强制，
            # 于是群里任何人发张图都会叫一次判定——实测一天 140 次、占全部判定调用的 41%，
            # 而她**看不见图**，那条判定只能答"不接"。触发标签写着"有人发了一张图给你看"，
            # 更是误导：那只是群里有人发了图。现在：
            #   群里别人发的图 → 只作为背景进历史（并入 20 条批量路径）；
            #   明确给她的图（@ 她 / 引用她 / 私聊）→ 照旧强制触发。
            forced = addressed or allowed_private
            if self.sessions.trigger(message.session_id).add(message, force=forced) is None:
                self._stats["deferred_messages"] += 1
                logger.debug("Stage 3 deferred check: session=%s", message.session_id)
                return None
            if allowed_private:
                trigger = "private_debug"
            elif addressed:
                trigger = address_reason(message, state.recent())
            else:
                trigger = "threshold"

        # 脱敏观测指标：把触发原因收敛成固定类别，不携带任何聊天内容。
        trigger_kind = self._trigger_kind(trigger)
        self.metrics.record_trigger(trigger_kind)
        # 没人在当值 → 这一次是"巡检"。同一时刻只允许一个群在巡检，别的群继续等
        # （两个群同时攒够 20 条时不会同时叫模型，也就不会同时出现两段当值对话）。
        swept = False
        if group_chat and not allowed_private and not resumed and self.focus.hot is None:
            if self.focus.sweeping:
                self._stats["deferred_messages"] += 1
                logger.debug("Stage 3 focus: sweep busy, defer session=%s", message.session_id)
                return None
            self.focus.begin_sweep()
            swept = True
        try:
            return await self._model_path(
                message, state, now=now, mode=mode, trigger=trigger, trigger_kind=trigger_kind,
                addressed=addressed, group_chat=group_chat, resumed=resumed,
            )
        finally:
            if swept:
                self.focus.end_sweep()

    async def _model_path(
        self,
        message: IncomingMessage,
        state: ConversationState,
        *,
        now: float,
        mode: ConversationMode,
        trigger: str,
        trigger_kind: str,
        addressed: bool,
        group_chat: bool,
        resumed: bool,
    ) -> OutgoingMessage | None:
        """真正调模型的那条路：判定 → 压缩 → 回复 → 应用决定。

        抽出来是因为它有两个入口：新消息（`handle`）和被焦点排队过、轮到这个群时
        再走一次的消息。后者不能重新记进历史，也不能重新走去重。
        """

        # 识图放在最前面：判定 agent 要先知道"图里是什么"才有得判（用户 2026-09-30 要求）。
        # 失败就保留 `[图片]` 占位符，绝不因为识图失败耽误一条回复。
        if self._flags().vision_enabled and self.vision is not None \
                and getattr(self.vision, "enabled", False) and message.media_urls:
            message = await self._apply_vision(message, state)
        logger.info(
            "Stage 3 model check: session=%s trigger=%s mode=%s history=%s",
            message.session_id,
            trigger,
            mode.value,
            len(state.history),
        )
        prompt_context = PromptContext(
            session_id=message.session_id,
            message=message,
            recent_messages=tuple(state.recent()),
            mode=mode.value,
            trigger=trigger,
            context=state.context,
        )
        prompt_material = PromptMaterial()
        memory_material = MemoryMaterial()
        # 「人物画像」：默认空；记忆服务在下面按当前说话人取一次。
        profile_note = ""
        # 判定说"这一句还接着原来那条线吗"——「顺带一提」用它判断话题有没有离开那件事。
        verdict_related = True
        # 判定说"这一句在问她的世界吗"——只有 YES 才去翻世界观资料（单 agent 模式照旧查）。
        lore_wanted = True
        care_note: dict | None = None
        # 「不懂就问」这一轮的现场提示（`ask_when_unsure`）：空串＝照旧说话。
        # 它进的是**易变段**，所以不影响前缀缓存（见 `build_dialogue_messages`）。
        clarify_note = ""
        ask_rules = self._flags().ask_when_unsure
        # 双 agent 结构：先由判定 agent 决定"要不要接"。它的 prompt 与窗口都小得多，
        # 所以这次调用便宜；判定说 NO_REPLY 就直接结束，省掉回复那一次完整调用。
        if self._judge_on(message.session_id):
            must_reply = message.message_id in self._promised_ids
            verdict = await self._judge(
                message, state, trigger=trigger, addressed=addressed, must_reply=must_reply
            )
            # 判定说"这一句越界了" → 先落库 + 让缓存失效，**再**拼回复的 prompt。
            # 顺序就是这条需求本身：让她变冷的那句话，回复当场就是冷的。
            self._apply_guard_signal(verdict, message)
            if group_chat and self.focus.is_hot(message.session_id):
                # 有人接着这段话题（她被叫到，或判定说还在同一条线上）→ 刷新当值计时。
                if addressed or verdict.related:
                    self.focus.note_related(now)
            # 判定每轮都读到话题：即使这一轮她不出声，也要用它刷新上下文——
            # "她没说话"不等于"话题没变"，而带下去的旧话题正是她强推旧事的原因。
            self._refresh_context_from_verdict(state, verdict, session_id=message.session_id)
            verdict_related = bool(getattr(verdict, "related", True))
            lore_wanted = bool(getattr(verdict, "lore", True))
            # 「不懂就问」的第一道确定性规则（2026-10-04）：**没听懂、又没被叫到 → 不插话**。
            # 它放在取资料之前：这一轮本来就不出声，没必要为它去翻东西（翻要花钱）。
            # **"被叫到时没有否决权"的原意保留**：这里唯一的沉默条件是"没被叫到"，
            # 被叫到时她照样必须回应——只是允许她把"断言的答"换成"问"（见下面）。
            if ask_rules and verdict.unsure and not (addressed or must_reply):
                self._stats["clarify_quiet"] += 1
                logger.info(
                    "Stage 3 clarify: 没听懂、也没被叫到，不插话 session=%s topic=%s",
                    message.session_id, state.context.topic or "-",
                )
                return None
            if not verdict.should_reply:
                return None
        # 历史达到上限时先把旧对话压成摘要——它是回复段的稳定前缀。
        await self._maybe_compact(state, session_id=message.session_id)
        # 回复段带的是**当前话题**的消息（起点之后、只追加），有摘要时接在摘要后面。
        history = state.topic_history()
        try:
            prompt_material = await asyncio.wait_for(
                self.prompt_sources.collect(prompt_context, knowledge_enabled=lore_wanted),
                timeout=EXTENSION_TIMEOUT_SECONDS,
            )
        except Exception:
            logger.exception("Stage 3 extension collection failed; continuing without extension data")
            prompt_material = PromptMaterial()
        if self.memory_service is not None:
            memory_material = await self.memory_service.retrieve(
                message, state.context.topic, mentioned=self._mentioned_people(message, state))
            # 「人物画像」：她对当前说话人的整体印象（跟着人走，不跟着话题走）。
            profile_note = await self.memory_service.profile_for(message)
            # 「顺带一提」：话题已经离开那件事（或那条状态过了一天）时，允许她关心一句。
            # 次数由存储层的 reminded_at 兜住——一条状态只提醒一次。
            care_note = await self.memory_service.care_note(
                message,
                topic_shifted=bool(self._judge_on(message.session_id) and not verdict_related),
            )
        # 「不懂就问」的第二道：**根据检查**（确定性，模型"觉得自己懂"绕不过它）。
        # 三种根据——知识库命中 / 记忆命中 / 语境明确——一样都没有、判定又说这是具体的
        # 专业·事实话题时，不许主动断言：被叫到就以"问"回应（问的次数还够的话），
        # 没被叫到就干脆不出声。日常、情感、闲聊不受这条限制（`care=False` 就是照旧）。
        #
        # 放在这里（而不是判定刚回来时）是因为前两样根据**要等资料取完才知道**。
        if ask_rules and self._judge_on(message.session_id):
            grounding = decide_grounding(
                verdict,
                called=bool(addressed or must_reply),
                has_knowledge=bool(prompt_material.knowledge_items),
                has_memory=bool(memory_material.records),
                context_explicit=context_is_explicit(message, state.recent()),
                ask_allowed=self.ask_budget.allows(message.session_id, state.context.topic, now),
            )
            if not grounding.speak:
                self._stats["clarify_quiet"] += 1
                logger.info(
                    "Stage 3 clarify: %s session=%s topic=%s specialist=%s grounded=%s",
                    grounding.reason, message.session_id, state.context.topic or "-",
                    grounding.specialist, grounding.grounded,
                )
                return None
            # 只有"要当心"的那几种才拼提示、才记日志：照旧说话的绝大多数轮次
            # 不该多一条日志（这条路上的每一轮都会跑到这里）。
            if grounding.care:
                clarify_note = grounding.turn_note
                if grounding.ask:
                    self.ask_budget.note(message.session_id, state.context.topic, now)
                    self._stats["clarify_asked"] += 1
                logger.info(
                    "Stage 3 clarify: %s session=%s topic=%s understood=%s specialist=%s grounded=%s",
                    grounding.reason, message.session_id, state.context.topic or "-",
                    getattr(verdict, "understood", "clear"),
                    grounding.specialist, grounding.grounded,
                )
        request = build_dialogue_messages(
            history,
            current=message,
            mode=mode,
            trigger=trigger,
            context=state.context,
            active_user_id=state.active_user_id,
            prompt_material=prompt_material,
            group_chat=group_chat,
            addressed=addressed,
            memory_material=memory_material,
            seq_of=state.seq_of,
            summary=state.summary,
            alias_of=state.alias_of,
            aliases=state.aliases,
            roster=tuple(state.roster_lines()),
            # 引用可能指向话题起点之前、甚至已经被摘要盖住的消息：渲染的仍是本话题，
            # 但**查引用目标**要拿完整窗口，否则只能写一句"看不见"（实测 686 次引用里
            # 218 次如此；其中一次真的让她把邦邦说的"我电脑没电了"算到了另一个人头上）。
            lookup=state.recent(),
            # 双 agent 模式：要不要回已经由判定定了，回复 agent 的 prompt 里
            # 没有 `<decision>`——它在结构上就没有否决权。
            must_reply=self._judge_on(message.session_id),
            # 关系现状在判定之后读：判定刚把防备升上去的话，这里读到的就是新值。
            stance=self._stance_for(message),
            profile_note=profile_note,
            care_note=care_note,
            # 「她写出去的信」：不问也该知道刚寄出的那一封，问起时能翻出最近一封。
            # 只有收信人本人能看到内容（`_letter_note` 里判）。
            letter_note=self._letter_note(message),
            # 「现在是什么时候」：以前这条路完全没有时间，她答不出"现在几点/今天几号"，
            # 也判断不了"昨天晚上""三天前"（2026-09-30 用户："云茹不能获取时间吗"）。
            # 它进的是**易变段**，不影响前缀缓存。
            now=now,
            # 「不懂就问」的现场提示（空串＝照旧）。同样只进易变段。
            clarify_note=clarify_note,
        )
        self._stats["model_calls"] += 1
        started_at = self.clock()
        try:
            result = await self._client_for(message.session_id).complete(request)
        except Exception as exc:
            # LLMError 自带脱敏的分类信息；其它异常只记录类别，绝不记录正文或凭据。
            summary = exc.safe_summary() if isinstance(exc, LLMError) else type(exc).__name__
            self._log_model_io("reply", request, "", session_id=message.session_id,
                               trigger=trigger, error=summary)
            logger.error(
                "Stage 3 model call failed: session=%s trigger=%s kind=%s elapsed=%.2fs detail=%s",
                message.session_id,
                trigger,
                trigger_kind,
                self.clock() - started_at,
                summary,
            )
            raise
        elapsed = self.clock() - started_at
        self.metrics.model_latency.observe(elapsed)
        # 完整输入输出落在 data/logs/reply.jsonl（最近 1000 次）。
        self._log_model_io("reply", request, result, session_id=message.session_id,
                           trigger=trigger, elapsed=round(elapsed, 2))
        message_ids = message_index_of(history, message, seq_of=state.seq_of)
        decision = parse_dialogue_output(
            result, known_message_ids=frozenset(message_ids),
            must_reply=self._judge_on(message.session_id),
        )
        # 风格审核放在**解析之后、其它一切之前**：这样统计、语境、记忆与发送看到的
        # 都是最终稿。审核失败/超时/可疑输出一律退回原稿（见 style_reviewer.py）。
        # `review_enabled` 是运行期开关（面板可关）——关掉就是原稿直发。
        if (self._flags().review_enabled and self.style_reviewer is not None
                and decision.kind is DecisionKind.REPLY
                and decision.text):
            reviewed, changed = await self.style_reviewer.review(
                decision.text, context=self._review_context(message, state))
            if changed:
                self._stats["reply_reviewed"] += 1
                logger.info(
                    "Stage 3 reply reviewed: before=%s after=%s draft=%s final=%s",
                    len(decision.text), len(reviewed), decision.text[:60], reviewed[:60])
                decision = replace(decision, text=reviewed)
        self.metrics.record_decision(decision.kind.value)
        logger.info(
            "Stage 3 decision: session=%s trigger=%s kind=%s dialogue=%s reply_length=%s "
            "topic=%s confidence=%.2f elapsed=%.2fs",
            message.session_id,
            trigger_kind,
            decision.kind.value,
            decision.dialogue.value,
            len(decision.text),
            decision.context.topic,
            decision.context.confidence,
            elapsed,
        )
        if self._judge_on(message.session_id) and decision.kind is DecisionKind.REPLY \
                and not decision.text:
            # 双 agent 下"空手而归"是**模型抽风**，不是"选择不说话"——它没有这个选择权。
            # 记成异常（并且不改会话状态），别让它悄悄变成一次沉默。
            self._stats["empty_forced_reply"] += 1
            logger.warning(
                "Stage 3 forced reply came back empty: session=%s trigger=%s",
                message.session_id,
                trigger_kind,
            )
            return None
        self._apply_decision(message.session_id, state, decision, active_user_id=message.user_id)
        if decision.kind is DecisionKind.REPLY and decision.text:
            state.record_bot_reply(message.session_id, message.target, decision.text, now)
            if care_note and self.memory_service is not None:
                # 她这一轮真的开口了 → 这条状态算"提过了"，以后不再出现。
                # 时机选在这里而不是"发送成功之后"：宁可万一发失败少提一次，
                # 也不能因为重试把同一件事提醒两遍（那正是要防的唠叨）。
                await self.memory_service.mark_care_noted(care_note["id"])
            # 她真的开口了 → 这段对话开始"当值"。
            # 往后这段时间里，别的群"明确找她"的消息只排队 + 回一句"稍等"。
            if group_chat:
                if not self.focus.is_hot(message.session_id):
                    self.focus.acquire(message.session_id, now)
                    self._stats["focus_switches"] += 1
                self.focus.note_reply()
        try:
            await asyncio.wait_for(
                self.prompt_sources.notify(prompt_context, decision),
                timeout=EXTENSION_TIMEOUT_SECONDS,
            )
        except Exception:
            logger.exception("Stage 3 extension notification failed")
            self.metrics.record_extension_failure()
        if decision.kind is DecisionKind.REPLY and decision.text:
            self._stats["replies"] += 1
            if self.memory_service is not None:
                self._pending_memory_replies[message.message_id] = decision.text
                if len(self._pending_memory_replies) > 128:
                    self._pending_memory_replies.pop(next(iter(self._pending_memory_replies)))
            # 把模型给的索引映射回真实消息 ID；不引用时为空串。
            reply_to = message_ids.get(decision.reply_to_message_id, "")
            self._persist_after_activity(now)
            # 按空行拆成"连着发的几条"，让长回答不像一段公告。
            # 第一条按原接口返回；后续几条放进 follow-up 队列，由发送方取走。
            # 这样 handle() 的返回类型不变，命令类回复与既有调用点都不受影响。
            # 只有引用挂第一条：引用是"在回应哪一句"，不是每句都要挂。
            segments = self.host.styler.split(decision.text)
            self._stats["reply_segments"] += len(segments)
            if len(segments) > 1:
                logger.info(
                    "Stage 3 reply split: session=%s segments=%s lengths=%s",
                    message.session_id,
                    len(segments),
                    ",".join(str(len(part)) for part in segments),
                )
                for part in segments[1:]:
                    self._follow_ups.setdefault(message.session_id, []).append(
                        OutgoingMessage(message.target, part))
            return OutgoingMessage(
                message.target,
                segments[0],
                reply_to,
                # 只在"要分几条发"时先亮一次"正在输入"。
                TYPING_NOTICE if len(segments) > 1 else "",
                # 角色回复是唯一会参与拟人化节奏的出站消息。
                paced=True,
            )
        return None

    def take_follow_ups(self, session_id: str | None = None) -> list[OutgoingMessage]:
        """取走并清空待续发的分段消息（**按会话**）。

        2026-09-30 改成按会话取：以前它是引擎级的一个列表，靠"handle() 返回后立刻取、
        中间不许插入 await"这条前提才不出错——那前提只在**单一调用方**（主循环）下成立。
        读信回信那条通道接上之后就有了第二个调用方：两个会话并发时，先返回的那个会把
        另一个的续发段抢走（两段分两条流发出，甚至发到另一个渠道去）。按会话存之后，
        这条前提不再需要。

        `session_id=None` 仍然返回全部，给既有调用点与测试留后路。
        """

        if session_id is None:
            pending = [item for items in self._follow_ups.values() for item in items]
            self._follow_ups.clear()
            return pending
        return self._follow_ups.pop(session_id, [])

    # --- 多群焦点：时钟、回避、排队 -------------------------------------------

    def _pick_ack(self) -> str:
        """随机挑一句"稍等"。用 secrets 而不是 random：它不该有可预测的规律。"""

        return secrets.choice(FOCUS_ACK_LINES)

    def focus_stats(self, now: float | None = None) -> dict[str, object]:
        """给观测用的一小撮焦点状态（不含任何聊天正文）。"""

        moment = self.clock() if now is None else now
        return {
            "hot": self.focus.hot_stats(moment),
            "queued_total": self.focus.total_queued(),
            "queued_sessions": self.focus.queued_sessions(),
            "sweeping": self.focus.sweeping,
        }

    async def tick(
        self, now: float | None = None
    ) -> list[tuple[IncomingMessage, list[OutgoingMessage]]]:
        """焦点时钟的一跳：到点就释放当值群，并轮到队列里的下一个。

        返回这一跳要送出的消息（告别语 + 被排队消息的回复），由 runtime 的定时任务
        负责投递；测试可以直接调用并检查返回值。没有可做的事就返回空列表。

        为什么需要这个时钟：当值群安静下来、而另一个群的人还在等的时候，
        没有任何新消息会到达——不靠定时器，"稍等"就永远等不到下文。
        """

        moment = self.clock() if now is None else now
        deliveries: list[tuple[IncomingMessage, list[OutgoingMessage]]] = []
        # 整跳都占着"巡检锁"：释放与切换之间有一段 await（压缩是模型调用），
        # 若此时刚好有新消息进来，它可能被当成一次巡检去调模型，
        # 与正在压缩的那个会话撞上（同一个会话的并发处理是必须避免的）。
        self.focus.begin_sweep()
        try:
            reason = self.focus.release_reason(moment)
            hot = self.focus.hot
            if reason is not None and hot is not None:
                if reason == "replies":
                    deliveries.extend(self._farewell(hot.session_id, moment))
                logger.info(
                    "Stage 3 focus release: session=%s reason=%s backlog=%s",
                    hot.session_id,
                    reason,
                    self.focus.total_queued(),
                )
                self.focus.release()
                if self.focus.has_backlog():
                    # **只有真的要切走时**才把这一段收起来：压进摘要、窗口重开。
                    await self._force_compact(self.sessions.state(hot.session_id), hot.session_id)
            switched = None
            # **只有在没有当值群的时候才轮下一个**。这里曾经漏了这个判断，
            # 于是时钟每一跳都把排队的人提上来，等于完全绕过了"不打断当前对话"。
            if self.focus.hot is None:
                switched = self.focus.take_next_session(moment)
            if switched is not None:
                self._stats["focus_switches"] += 1
                logger.info("Stage 3 focus switch: session=%s", switched)
                deliveries.extend(await self._serve_queue(switched, moment))
        finally:
            self.focus.end_sweep()
        return deliveries

    def _farewell(self, session_id: str, now: float) -> list[tuple[IncomingMessage, list[OutgoingMessage]]]:
        """连回满 50 条时交代一句再走。"""

        state = self.sessions.state(session_id)
        if not state.history:
            return []
        source = state.history[-1]
        text = FOCUS_FAREWELL_LINE
        state.record_bot_reply(session_id, source.target, text, now)
        return [(source, [OutgoingMessage(source.target, text, paced=False)])]

    async def _serve_queue(
        self, session_id: str, now: float
    ) -> list[tuple[IncomingMessage, list[OutgoingMessage]]]:
        """轮到某个群了：把它排队的消息按到达顺序逐条处理。"""

        queued = self.focus.drain(session_id)
        deliveries: list[tuple[IncomingMessage, list[OutgoingMessage]]] = []
        for position, message in enumerate(queued):
            promised = message.message_id in self._promised_ids
            try:
                reply = await self.handle(message)
            except Exception:
                logger.exception("Stage 3 focus: queued message failed session=%s", session_id)
                reply = None
            outgoing = _as_outgoing_list(reply) + self.take_follow_ups(message.session_id)
            self._resumed_ids.discard(message.message_id)
            self._promised_ids.discard(message.message_id)
            if outgoing:
                self._stats["focus_queued_replies"] += 1
                deliveries.append((message, outgoing))
            elif promised:
                # 回过"稍等"却没给出结果：记一条，别让它静默消失。
                self._stats["focus_declined_promised"] += 1
                logger.warning(
                    "Stage 3 focus: promised message got no reply session=%s", session_id
                )
            if self.focus.release_reason(now) is not None:
                # 处理积压的过程中又到点了（例如连回满 50 条）：停下来，剩下的留到下一轮。
                self._requeue(session_id, queued[position + 1:])
                break
        return deliveries

    def _requeue(self, session_id: str, messages: list[IncomingMessage]) -> None:
        for message in messages:
            self.focus.enqueue(session_id, message)

    async def _force_compact(self, state: ConversationState, session_id: str) -> None:
        """回避时把当值群这一段压进摘要、窗口收起来。

        与按阈值触发的压缩走同一条路，只是不看 500 条这个门槛：切走之后这个群要
        重新变成"背景"，窗口留着没有意义，而摘要能让它下次回来时仍有上下文。
        """

        await self._maybe_compact(state, session_id=session_id, force=True)

    def _persist_after_activity(self, now: float) -> None:
        """按节流间隔保存一次状态，避免每条消息都写盘。"""

        if self.state_store is None:
            return
        last = getattr(self, "_last_persist_at", None)
        if last is not None and now - last < PERSIST_MIN_INTERVAL_SECONDS:
            return
        self._last_persist_at = now
        self.persist_state()

    async def _maybe_compact(
        self, state: ConversationState, *, session_id: str, force: bool = False
    ) -> None:
        """历史达到上限时压缩旧对话，为回复段腾出稳定的前缀。

        设计取舍：**同步做，且失败就跳过**。

        - 同步：压缩会改 `state.summary`，而它正是这一轮回复段的前缀；异步做会
          出现"这一轮用旧摘要、下一轮用新摘要"的错位，前缀反而更不稳。
        - 失败跳过：压缩失败只该表现为"这段历史还没被压缩"，而不是丢消息或报错。
          历史保留原样，下一轮再试。

        `force=True` 用于**焦点回避**：那个时刻不看 500 条的门槛，直接把这一段收进摘要
        （"当前群这一段压进摘要、窗口收起来"），这样切走之后它不再占着当值状态，
        下次回来时摘要还在。

        用 `judge_client` 而不是对话 client：压缩请求的 prompt 形态与对话完全不同，
        共用同一个 user_id 会让两种模式抢同一份 KVCache，两边都命中不了。
        """

        pending = (
            state.compaction_pending_all(keep=COMPACT_KEEP)
            if force
            else state.compaction_pending(target=LIVE_TARGET, keep=COMPACT_KEEP)
        )
        if pending is None:
            return
        messages, _boundary = pending
        # 按条数切批：一批压完、边界推进一批，**被标记为已摘要的永远只是真的压进去的**。
        batches = split_compaction_batches(messages)
        # 压缩与判定共用同一个会话的通道：两者的 prompt 形态完全不同，
        # 但都属于"这个群的后台活"，共用一个隔离空间比再切一个更省事。
        client = self._judge_for(session_id) or self._client_for(session_id)
        compressed_batches = 0
        compressed_messages = 0
        for batch in batches[:MAX_COMPACT_BATCHES_PER_TURN]:
            boundary = state.seq_of(batch[-1])
            if boundary is None:
                # 这一批已经不在窗口里了（理论上不会发生）。停在这儿，
                # 宁可少压一次，也不能把边界推到没压过的地方。
                logger.warning("对话压缩边界缺失，提前停止: session=%s", session_id)
                break
            request = build_compaction_messages(batch)
            self._stats["compaction_calls"] += 1
            started_at = self.clock()
            try:
                raw = await client.complete(request)
            except Exception as exc:
                summary = exc.safe_summary() if isinstance(exc, LLMError) else type(exc).__name__
                logger.error(
                    "对话压缩失败，这一段历史保持原样: session=%s kind=%s", session_id, summary
                )
                break
            addition = parse_compaction_output(raw)
            if not addition:
                logger.warning("对话压缩返回空摘要，跳过: session=%s", session_id)
                break
            state.summary = merge_summary(state.summary, addition)
            # 边界 = 这一批的最后一条：它确实进了这次请求，也确实被摘要覆盖了。
            state.summary_through = boundary
            # 把已摘要的消息移出窗口。不移除的话下一轮会认为"还有可压缩的"，
            # 于是每轮都压缩一次——既浪费调用，又让摘要每轮都变（前缀永远不稳）。
            state.mark_compacted(boundary)
            # 压缩之后启用**新的话题起点**：旧起点已经在摘要里了，锚点必须落到
            # 保留窗口的第一条。这就是"压缩会切进话题"的正式说法。
            state.topic_start_seq = state.seq_range()[0] if state.seq_range() else None
            self.metrics.compaction_latency.observe(self.clock() - started_at)
            compressed_batches += 1
            compressed_messages += len(batch)
        if compressed_batches:
            logger.info(
                "对话压缩完成: session=%s 批=%s 条=%s through_seq=%s summary_len=%s",
                session_id,
                compressed_batches,
                compressed_messages,
                state.summary_through,
                len(state.summary),
            )

    @staticmethod
    def _refresh_context_from_verdict(state, verdict, *, session_id: str = "") -> None:
        """用**判定这一轮**读到的话题刷新会话上下文，并丢掉过期的未了问题。

        由来（2026-09-28 实测）：回复段自报的 `topic_status` 35 轮里只有 1 次是 shifted，
        而判定每轮都会给一个新的 `<topic>` 和 `<related>`。引擎却把**回复段上一轮的**
        topic / pending_question 一直带下去，于是：判说明明写了 `related=NO`（这一句不在
        原来那条线上），她下一句还在追问上一个话题——"体温量了吗"连问四轮，群里已经
        聊到"明天吃什么"了。

        规则很机械：判定说这一句不在原来那条线上（`related=NO`）→ 未了问题一律作废；
        判定给了新话题 → 就用它，别再拿上一轮回复里的旧标签。
        """

        fresh = (getattr(verdict, "topic", "") or "").strip()
        related = bool(getattr(verdict, "related", True))
        context = state.context
        if not related and context.pending_question not in {"", "无"}:
            logger.info("Stage 3 dropped stale pending question: session=%s topic=%s",
                        session_id, fresh or context.topic)
            context = replace(context, pending_question="无")
        if fresh:
            context = replace(context, topic=fresh)
        state.context = context

    @staticmethod
    def _mentioned_people(message: IncomingMessage, state) -> tuple[str, ...]:
        """这条消息**提到了谁**（@ 到的 + 引用回复的那个人），用来借他们的旧事。

        只认两种确定性线索：`mentioned_user_ids` 与 reply 段对应的那条消息的发送者。
        不在正文里做人名匹配——昵称会改、会重复，认错人比想不起来更糟
        （理由见 docs/MEMORY_PEOPLE.md 第七节）。
        """

        people: list[str] = [uid for uid in message.mentioned_user_ids if uid]
        reply_to = getattr(message, "reply_to_message_id", "")
        if reply_to:
            for other in reversed(state.recent()):
                if other.message_id == reply_to:
                    if not other.is_bot_message:
                        people.append(other.user_id)
                    break
        return tuple(uid for uid in dict.fromkeys(people)
                     if uid and uid != message.user_id)

    async def _judge(
        self,
        message: IncomingMessage,
        state: ConversationState,
        *,
        trigger: str,
        addressed: bool,
        must_reply: bool = False,
    ) -> JudgeVerdict:
        """调判定 agent；失败时回落为"不出声"。

        判定失败不该让整条消息处理炸掉，也不该冒险让回复段在缺语境时硬说一句——
        回落成 NO_REPLY 是安全的一侧。

        `must_reply=True` 走 **judge'**（`JUDGE_ROUTING_PROMPT`）：给积压消息用。
        她已经答应过人家"稍等"，"要不要回"不再由判定决定，所以那一版连 route 都不输出，
        只回答话题起点与"还在不在同一条线上"。
        """

        identity = ""
        if self.memory_service is not None:
            material = await self.memory_service.identity_for(message)
            identity = material.as_judge_note(message.target.group_id, message.user_id)
        request = build_judge_messages(
            state.topic_history(),
            current=message,
            trigger=trigger,
            addressed=addressed,
            seq_of=state.seq_of,
            alias_of=state.alias_of,
            aliases=state.aliases,
            roster=tuple(state.roster_lines()),
            summary=state.summary,
            # 判定也看得到被引用的那句是谁说的（判断"还在不在同一条线上"要靠它）。
            lookup=state.recent(),
            current_start=_effective_topic_start(state),
            must_reply=must_reply,
            # 判定读的是**写入之前**的分寸：它这一轮报 UP，变化要下一句才体现在
            # 它自己看到的数据里（回复段则当场就用新值）。
            stance=self._stance_for(message),
            identity=identity,
            # 判定也要知道现在什么时候：它判断"还在不在同一条线上"靠的正是
            # "刚才/昨天晚上/三天前"这类说法（2026-09-30 加）。
            now=self.clock(),
        )
        self._stats["judge_calls"] += 1
        started_at = self.clock()
        try:
            raw = await self._judge_for(message.session_id).complete(request)
        except Exception as exc:
            summary = exc.safe_summary() if isinstance(exc, LLMError) else type(exc).__name__
            self._log_model_io("judge", request, "", session_id=message.session_id,
                               trigger=trigger, must_reply=must_reply, error=summary)
            logger.error(
                "Stage 3 judge call failed: session=%s trigger=%s kind=%s elapsed=%.2fs",
                message.session_id, trigger, summary, self.clock() - started_at,
            )
            return JudgeVerdict(should_reply=False)
        self.metrics.judge_latency.observe(self.clock() - started_at)
        self._log_model_io("judge", request, raw, session_id=message.session_id,
                           trigger=trigger, must_reply=must_reply,
                           elapsed=round(self.clock() - started_at, 2))
        # 只接受"判定这次真的看得见"的编号；看不懂就保留原来的起点。
        visible = frozenset(
            seq for seq in (state.seq_of(m) for m in state.topic_history()) if seq is not None
        )
        verdict = parse_judge_output(raw, known_seqs=visible, must_reply=must_reply)
        # 判定这一轮说了两件事：**回不回**（route）+ **把哪一段交给回复段**（话题起点）。
        #
        # ⚠️ 这个起点**只裁剪视图，不销毁消息**：`topic_history()` 只是给模型的视图，
        # `live_history()`（压缩的取数口）**不按起点过滤**——被排除在视图之外的消息
        # 仍在窗口里，下一次满 500 条压缩时照旧进摘要。
        #
        # "只许前进"保留（2026-10-02 实测 1464 条判定：88% 原地确认、12% 前进、
        # **0% 后退**——所以这条限制挡掉的动作根本不发生，没有代价）。
        if state.advance_topic_start(verdict.topic_start):
            logger.info(
                "Stage 3 topic start advanced: session=%s start=%s",
                message.session_id,
                state.topic_start_seq,
            )
            # 话题往前挪了（= 换话题了）→ 摘要若已与当前话题脱节就丢掉，
            # 别把旧话题拖进新话题。话题起点落在摘要覆盖范围内时不丢
            # （那是跨压缩边界延续下来的同一个话题，摘要里有它的前文）。
            if state.drop_summary_if_stale(state.topic_start_seq):
                logger.info(
                    "Stage 3 旧摘要已丢弃（与当前话题脱节）: session=%s", message.session_id)
        logger.info(
            "Stage 3 judge: session=%s trigger=%s verdict=%s",
            message.session_id, trigger, verdict.describe(),
        )
        return verdict

    @staticmethod
    def _trigger_kind(trigger: str) -> str:
        # 收拢成固定类别写进指标；新增的 name / reply_to_bot 是"被叫到"的两种
        # 新依据，必须单列，否则在触发分布里会糊成 unknown，看不出群聊注意力
        # 放开之后的实际构成。
        return trigger if trigger in {
            "mention", "media", "threshold", "active_message", "private_debug",
            "name", "reply_to_bot",
        } else "unknown"

    def _apply_decision(
        self,
        session_id: str,
        state: ConversationState,
        decision: DialogueDecision,
        *,
        active_user_id: str | None = None,
    ) -> None:
        if decision.kind is DecisionKind.EXIT:
            self.sessions.leave(session_id)
            return
        context = decision.context
        # 话题已经转移或结束时，"未决问题"属于上一个话题，必须丢掉。
        #
        # 由来：模型把"话题转移"（topic_status）和"退出交谈"（dialogue）当成两个
        # 独立信号，实机统计里 KEEP+shifted 真的会同时出现——它知道话题变了，却仍
        # 保持对话状态。结果是 pending_question 沉淀下来，她换了话题之后还追着上一
        # 个话题的问题问。这里用 topic_status 直接兜住，不依赖对话状态退出。
        if context.topic_status in {"shifted", "ended"} and context.pending_question != "无":
            logger.debug(
                "Stage 3 dropped stale pending question: session=%s topic_status=%s",
                session_id,
                context.topic_status,
            )
            context = replace(context, pending_question="无")
        state.context = context
        if decision.kind is DecisionKind.REPLY:
            state.enter(active_user_id)
        if decision.dialogue is DialogueStatus.EXIT:
            self.sessions.leave(session_id)
            # 一场交谈结束是**天然的对账时刻**：这时候把按 key 的缓存命中率算一遍
            # 记进日志。挑这个时机是因为它既不像每轮那样吵，也不像定时那样可能
            # 永远等不到——对话结束就是一个明确的"这一段算完了"。
            try:
                self.log_cache_report("dialogue_end")
            except Exception:  # noqa: BLE001 - 对账失败不该影响结束对话
                logger.exception("Stage 3 cache check failed")


def _effective_topic_start(state: ConversationState) -> int | None:
    """当前话题起点；还没定过就用窗口第一条。

    为什么要给个默认值而不是让判定从零猜：**判定的任务是"确认或前移"，不是"重新找"**。
    实测不告诉它当前起点时，它每轮都挑最新那条，起点一格一格往前爬，
    回复段最后只剩一条历史（见 `docs/MULTI_GROUP_FOCUS.md` 第十节）。
    """

    if state.topic_start_seq is not None:
        return state.topic_start_seq
    span = state.seq_range()
    return span[0] if span else None


def _as_outgoing_list(reply: object) -> list[OutgoingMessage]:
    """把 handle() 的返回值统一成出站消息列表。

    历史接口返回单条 `OutgoingMessage`；拟人化分段后可能返回多条。
    两种都接受，避免每个调用点各自判断类型。
    """

    if reply is None:
        return []
    if isinstance(reply, OutgoingMessage):
        return [reply]
    if isinstance(reply, (list, tuple)):
        return [item for item in reply if isinstance(item, OutgoingMessage)]
    return []


async def _deliver_reply(
    transport: QQTransport,
    engine: DialogueEngine,
    source: IncomingMessage,
    outgoing: list[OutgoingMessage],
    *,
    outbox=None,
    backlog: bool = False,
) -> None:
    """按拟人化节奏逐条发送回复。

    停顿放在这里而不是模型侧：模型只管写内容，节奏是框架的事。
    第一条之前先亮一次"正在输入"，最后一条之后把整批正文并入短期语境。

    `backlog=True` 表示"后面还有一堆消息在排队"（主循环传进来）：这时候**把拟人停顿
    去掉**，直接发。理由（2026-09-30 用户："发一个命令一分多钟才回复"）：停顿是装饰，
    队列积压时它只会让积压更长；2~4 秒 × 几十条就是好几分钟的延迟。

    **没送出去的要留住**（用户 2026-09-28 要的）：连接不在、等连接超时、写入前就断了
    这三类抛 `MessageNotDelivered`，交给 `outbox` 等连接回来补发；其它失败照旧只记日志
    （`DeliveryUncertain` 补发会造成重复，`DeliveryRejected` 是对面不要）。
    第一条确定送不出去之后，这一批剩下的段**不再逐条去撞 15 秒的连接等待**，
    直接按顺序排进队列。
    """

    # 只有角色回复才加节奏。命令类回复（ping / help / #bot …）的 paced 为 False，
    # 必须立刻送出——工具性响应等一秒反而显得迟钝。
    paced = engine.typing_sim and bool(outgoing[0].paced) and not backlog
    pauses = engine.host.styler.delay_plan([item.text for item in outgoing]) if paced else ()
    # 收着他的时候先搁一下：这是"不情愿"的信号，所以要落在**她真的开口**的时候。
    # 命令回复不掺这个；上限见 `STANCE_DELAY_SECONDS`（最慢 12 秒）。
    if paced:
        stance_delay = engine.stance_delay_for(source)
        if stance_delay > 0:
            logger.info("Stage 3 reply held back: %.1fs", stance_delay)
            await asyncio.sleep(stance_delay)
    typing_sender = getattr(transport, "send_typing", None)
    combined: list[str] = []
    connection_down = False
    for index, item in enumerate(outgoing):
        pause = pauses[index] if index < len(pauses) else 0.0
        if pause > 0 and not connection_down:
            await asyncio.sleep(pause)
        sent_at = engine.clock()
        if connection_down:
            # 这一批已经确定发不出去了：不再逐条去等 `connection_timeout`，
            # 直接排队，顺序仍然保持。
            _queue_undelivered(outbox, item)
            combined.append(item.text)
            continue
        try:
            # 只在"要分几条发"时先亮一次状态：单条短回复本来就该干脆。
            if item.typing_notice and len(outgoing) > 1 and callable(typing_sender):
                await typing_sender(item.target, item.typing_notice)
            await transport.send(item.target, item.text, reply_to=item.reply_to_message_id)
            engine.metrics.send_latency.observe(engine.clock() - sent_at)
            combined.append(item.text)
            logger.info(
                "Stage 3 reply sent: group=%s segment=%s/%s reply_length=%s quoted=%s pause=%.2fs",
                item.target.group_id,
                index + 1,
                len(outgoing),
                len(item.text),
                bool(item.reply_to_message_id),
                pause,
            )
        except MessageNotDelivered as exc:
            # 确定没送出去：排队等连接回来（这不是"失败"，是"晚一点"）。
            engine.metrics.record_send_failure()
            connection_down = True
            logger.warning("Stage 3 reply not delivered (%s); queueing for resend", exc)
            _queue_undelivered(outbox, item)
            combined.append(item.text)
        except Exception:
            engine.metrics.record_send_failure()
            logger.exception("Stage 3 reply send failed")
    if combined:
        # 语境里存的是一次完整回应（各段合起来），不是碎片。
        # 排队中的那几段也算"她说过"：否则连接一断，她的语境里就没有那句话，
        # 下一轮会当成什么都没说过——重复比迟到更难看。
        engine.record_sent_reply(source, "\n\n".join(combined))


def _queue_undelivered(outbox, item: OutgoingMessage) -> None:
    """把一条没送出去的消息放进补发队列；没有队列（测试/单次调用）就只记日志。"""

    if outbox is None:
        logger.error("Stage 3 reply dropped: no outbox (group=%s)", item.target.group_id)
        return
    if not outbox.enqueue(item.target, item.text, reply_to=item.reply_to_message_id):
        logger.error("Stage 3 reply dropped: outbox full (group=%s)", item.target.group_id)


def _port_in_use(host: str, port: int) -> bool:
    """这个端口上已经有人监听了吗（用来防重复启动）。

    开机自启动之后，"已经有一个实例在跑、登录时又拉起一个"是很常见的组合：
    新实例会撞端口、报一串 traceback 然后退出。这里先探一下，探到了就干净地放弃。

    **用 bind 探，不用 connect 探**：连一下确实能探出"有人在听"，但那个半截连接会被
    对面当成一次失败的握手，在**现有实例的日志里留一条异常堆栈**（踩过：
    `websockets.exceptions.InvalidMessage: did not receive a valid HTTP request`）。
    bind 不产生连接，占着端口的话直接报 `WSAEADDRINUSE`，干净。
    """

    import socket

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind((host, port))
    except OSError:
        return True
    finally:
        probe.close()
    return False


async def run() -> None:
    """Stage 3 入口：反向 WebSocket（本机监听，NapCat 连进来）。

    装配与主循环都在 `runtime.serve` 里，与通道无关；这里只决定"用哪个通道"。
    Stage 4 会用同一套 `runtime.serve`，只是换成 OneBotClientTransport。

    **端口探测放在 `main()` 里，不在这里**：`run()` 会被测试直接调用（装配假传输层），
    把"真机端口被占就退出"塞进来，测试就会随"本机是否正在跑 bot"而红绿不定——踩过。
    """

    # 延迟导入：runtime 依赖本模块（引擎与命令表在这里），模块级互导会成环。
    from .runtime import serve

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    transport = OneBotWebSocketTransport(
        host=ONEBOT_WS_HOST,
        port=ONEBOT_WS_PORT,
        access_token=ONEBOT_ACCESS_TOKEN or None,
    )
    await serve(transport)


def main() -> None:
    """进程入口：先确认端口没人占，再进 `run()`。

    防重复启动：已经有一个实例在听 8080 时，这次启动干净地放弃（记一行日志），
    不要去抢端口、也不要抛一串 traceback。开机自启动之后这种组合很常见
    （手动开过一个，登录时又拉起一个）。
    """

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if _port_in_use(ONEBOT_WS_HOST, ONEBOT_WS_PORT):
        logger.error(
            "端口 %s 已经被占用：可能已经有一个实例在跑，这次启动放弃（不抢端口）。"
            "如果是残留进程，先把它停掉再启动。",
            ONEBOT_WS_PORT,
        )
        return
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
