from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
import time
from dataclasses import dataclass, replace
from datetime import datetime
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
from .judge_sampling import JudgeSampler
from .builtin_commands import (
    HELP_COMMAND_PATTERN,
    build_command_registry,
    build_help_text,
)
from ._host import HostServices
from .ask_when_unsure import (
    NO_ASSERT_NOTE,
    context_is_explicit,
    decide_grounding,
    has_specific_claim,
)
from .dialogue_judge import (
    OFFENSE_CODES,
    JudgeVerdict,
    build_judge_messages,
    parse_judge_output,
)
from .dialogue_compaction import (
    build_compaction_messages,
    merge_summary,
    parse_compaction_output,
    split_compaction_batches,
)
from .onebot_ws import OneBotWebSocketTransport
# 识图把描述塞回正文用的那一句替换。**它现在是核心的**（原来在 `vision.py`）：
# 只用媒体标签，不需要插件；这样"有图的消息"这条路径不再从插件 import
# （原来 `_apply_vision` 里是 `from .vision import replace_media_placeholder`）。
from .media_segments import replace_media_placeholder
from .remind import (
    MAX_REMIND_SEEN,
    REMIND_CHANNEL_PHRASES,
    REMIND_DEDUPE_WINDOW_SECONDS,
    RemindEmailBook,
    RemindRequest,
    find_email,
    forever_channel,
    parse_remind_time,
    remind_channel,
    remind_key,
    said_this_time,
)
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
    build_roster_lines,
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
    # 出站正文的**保守净化**（2026-10-09）：在正文送出去之前剥掉"孤立的标签片段"。
    # 定义在 `security.py`：它属于"发出去的那串字"这一层，不属于对话判定。
    scrub_outbound_tags,
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
from . import data_boundary
from . import face_names
from . import provider_failover
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
# 「她自己的老习惯」那块材料的**换季周期**（秒），**与压缩一起**决定它什么时候重算。
#
# 2026-10-11 用户口径：*"我要丢到易变区开头，每几个小时更新一次，只有触发压缩的时候
# 才更新，这样命中率更高"*，然后一句更正：*"到了 3 小时也得等压缩，只有同时达到 3 小时
# 和压缩过后才会变"*。
#
# 所以判据是 **并且**，不是"或者"：`_style_material` 只在"自上次算它之后压过一次"
# **而且**"距上次算它已过这个时长"时才重算。单独出现任何一个都不动它——于是它的换季点
# 被压到"压缩刚发生 + 本来也该换了"那一刻，其余时间逐字不变。
# 三个小时是"几个小时"的中间值，没有实测依据（真要看的是前缀命中率，将来可以调）。
STYLE_MATERIAL_TTL_SECONDS = 3 * 3600.0
ACTIVE_TIMEOUT_SECONDS = 180.0
# 「被焦点排队过」的消息 id（`_resumed_ids`）的上限。超了淘汰**最旧**的一条。
# 条目只在那条消息"还在排队等当值"时有意义，而队列本身有上限
# （`dev_config.FOCUS_QUEUE_LIMIT` × 群数），所以这个数只是兜底；从前它是 `set`
# 且只在排空时清，被停用/排不出去的群会把条目永久留下（2026-10-08 隔离审计 [3]）。
MAX_RESUMED_IDS = 4096
# 「到点提醒」要用的两份按人状态（2026-10-08）：每人一份**邮箱**（核心手里那份，
# 递给插件用；**落盘**，见 `remind.RemindEmailBook`），以及"还在等他给邮箱"的待办。
# 两个都有上限——照 `_promise` / `_resume_later` 那套：`dict` 的插入序，超了淘汰
# **最旧**的一条。邮箱那份重启后仍在（用户要求"只第一次要"）；**待办**只活在这一轮
# 进程里（重启即弃：那条还没交出去的请求丢了就丢了，他再提一次就是）。
MAX_REMIND_ASKS = 64
# 「群聊」那一页要的列表：一次最多列多少个群（多出来的截掉——它是给人看的一页，
# 不是数据导出），以及缓存下来的那一份（`_groups_cache` / `_groups_cache_at`）。
MAX_GROUPS_LIST = 200

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
# 判定/回复的模型调用**连续**失败到这个数，`/super status` 那一行就升级成"异常"
# （用户要的是"她又突然不说话了"能一眼看见；单次失败可能只是抖一下，连续才是异常）。
CONSECUTIVE_MODEL_FAILURE_ALERT = 3
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
# `/super quote`：金句学习学出来的**表情含义表 + 笔记**（只读），
# 后面可以跟一段"人工改这个表情的方向"（`/super quote 128516 不赞成 在损她`）。
# 单独一条模式而不是塞进共享的 topic 组：后面跟的是 emoji_id 与自由文字，
# 塞进去会顺手让 `/super restart quote` 之类的组合变成合法命令（`permit` 踩过同样的坑）。
SUPER_QUOTE_PATTERN = re.compile(
    r"^[/#]super\s+(?:quote|quotes|金句|表情含义)(?:\s.*)?$",
    re.IGNORECASE,
)
# `/super slang`：她**被动听来的黑话词条**（只读），后面可以跟"改释义 / 删 / 标错"。
# 与 `/super quote` 同一形状（列 + 改 + 删），同样单独一条模式而不是塞进共享 topic 组：
# 它后面跟的是"词"与自由文字，塞进去会顺手让 `/super restart slang` 变成合法命令。
SUPER_SLANG_PATTERN = re.compile(
    r"^[/#]super\s+(?:slang|黑话|词条)(?:\s.*)?$",
    re.IGNORECASE,
)
# 群管理与群主命令的解析**已经搬进插件**（`builtin_group_commands.py`，2026-09-30
# 用户要求"stage4 的内容都用插件实现"）。核心不再认识这些命令的字面形状，
# 只保留下面这一条自己的记忆开关。
#
# `/super enable memory` / `/super disable memory`：**记忆开关**（按群）。
#
# 单独一条模式、先于通用的 `SUPER_COMMAND_PATTERN` 判：那个模式的 action 组里没有
# enable/disable，所以两者本来不会撞；单独列出来是为了让"这条命令的形状"一眼可见
# （它和 `/admin enable|disable` 是同一套用法，只是管的是另一张名单）。
# 尾部那个 group 捕获**不是参数**：它是用来"认出并拒绝手滑带了群号"的，
# 与 `/admin enable 群号` 的处理一致（`_admin_command_reply` 里那段说明）。
SUPER_MEMORY_SWITCH_PATTERN = re.compile(
    r"^[/#]super\s+(?P<action>enable|disable|开启|关闭|打开|停止)\s+(?P<target>memory|记忆)"
    r"(?:\s+(?P<group>\S+))?\s*$",
    re.IGNORECASE,
)

# 档位判定（`_plugin_level_allowed`）与动作执行（`_plugin_action_reply`）。
# `/super admin list` 最多列这么多行：出站正文上限是 1000 字符，
# 名单长了要主动截断并说明还有多少人，不能撞上限被静默截掉。
ADMIN_LIST_MAX_ROWS = 30
# `/super slang` 一趟最多列几条词条（出站正文上限 1000 字符，列不完要说明还有多少）。
SLANG_LIST_MAX_ROWS = 20
# 关系缓存的有效期（秒）。写入时我们会主动失效，所以"当轮变冷"不依赖它；
# TTL 是给维护 agent 那条通道兜底的——它在别的线程改库，引擎并不知情。
STANCE_CACHE_TTL_SECONDS = 60.0
# --- 防备轴**只在这几种由头下**才升（2026-10-09 用户改的设计）-----------------
#
# 用户原话两句：*"聊机制她不回不就好了"*、*"戒备我认为只有冒犯性的话或者阴阳怪气
# 才会提高"*。落地时把"算不算越界"拆成两件事：
#
# 1. **判定只出一个信号**（`dialogue_judge`）：`<guard>UP|HOLD|PROBE</guard>` +
#    `UP` 时的 `<guard_reason>`，**只能**填 `insult` / `sarcasm` / `belittle`
#    这三个词（表在 `dialogue_judge.OFFENSE_CODES`）；
# 2. **政策留在核心**（这里）：只有表里这几个由头才允许写防备轴。判定报 UP 而由头
#    不在表里（它把一句玩笑读成越界），核心**一个字都不写**——与"插件不决定政策"
#    同一条思路。
#
# 为什么要有这一层而不是只改提示词：从前"算不算冒犯"是判定一个人说了算，于是它把
# "有人聊到她的机制/内部实现"也当成信号，写了一条 `guardedness +1`
# （真库证据：`relationship_log` 里 `axis=guardedness 0→1 source=judge`）——
# **整个群被她抬起了戒备**，而她要的只是"聊机制这一轮不接"。
# 这一层是机械的：提示词漂了也拦得住。
GUARD_RAISING_OFFENSES = frozenset({"insult", "sarcasm", "belittle"})
#: 没有可用由头时记在日志里的说明（只有这一个字符串，不是判定写的自由文本）。
GUARD_RAISE_WITHOUT_OFFENSE = "no_allowed_offense"
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
    # **显式偏移，不读宿主时区**（2026-10-08 安全与时间口径审计 B-01）：
    # 从前是 `time.localtime(timestamp)`，于是"群里这边"跟着部署机器的时区走——
    # 换到 UTC 云主机上整整差 8 小时，而且不报错。
    return datetime.fromtimestamp(
        timestamp, tz=dev_config.local_timezone()
    ).strftime("%m-%d %H:%M:%S")


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
    MEMORY_ON = "memory_on"
    MEMORY_OFF = "memory_off"
    MEMORY_RECORDS = "memory_records"
    MEMORY_INBOX = "memory_inbox"
    MEMORY_AUDIT = "memory_audit"
    MEMORY_ARCHIVE = "memory_archive"
    PROFILE = "profile"
    QUOTE = "quote"
    SLANG = "slang"


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
    # 记忆开关先于通用模式判（形状与 `/admin enable|disable` 对称）。
    switch = SUPER_MEMORY_SWITCH_PATTERN.fullmatch(stripped)
    if switch:
        action = (switch.group("action") or "").casefold()
        return (SuperAction.MEMORY_ON if action in {"enable", "开启", "打开"}
                else SuperAction.MEMORY_OFF)
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
    # 金句学习的表情含义表：同理，先于通用模式。
    if SUPER_QUOTE_PATTERN.fullmatch(stripped):
        return SuperAction.QUOTE
    # 黑话词条（她被动听来的那些）：同样先于通用模式，否则 `/super 词条 x y`
    # 会被当成 `/super <topic>` 掉进记忆那一条。
    if SUPER_SLANG_PATTERN.fullmatch(stripped):
        return SuperAction.SLANG
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


# `/super quote <emoji_id> <方向> [说明]` 里"方向"那一段。emoji_id 实测是数字串
# （`"424"`、`"128516"`），但**不假定它一定是数字**——照 `reactions.py` 那条纪律，
# 它就是个字符串，所以这里只要求"不是空、没有空格"。
_QUOTE_OVERRIDE_PATTERN = re.compile(
    r"^[/#]super\s+(?:quote|quotes|金句|表情含义)\s+(?P<emoji>\S+)\s+(?P<sense>\S+)"
    r"(?:\s+(?P<note>.+))?$",
    re.IGNORECASE,
)
#: 撤掉人工方向的关键字（"改回它自己判的"）。
QUOTE_OVERRIDE_AUTO = frozenset({"auto", "自动", "清除", "撤销", "复原", "默认"})


def _parse_quote_override(text: str) -> tuple[str, str, str] | None:
    """解析"人工改一个表情的方向"那一段；不是这个形状就返回 None（于是只读展示）。

    返回 `(emoji_id, 方向或 "auto", 说明)`。方向**不在核心里查表**——它交给
    `quote_learning.normalize_sense` 归一化，两处各留一份词表迟早漂移。
    """

    if not isinstance(text, str):
        return None
    match = _QUOTE_OVERRIDE_PATTERN.fullmatch(text.strip())
    if not match:
        return None
    emoji_id = match.group("emoji").strip()
    sense = match.group("sense").strip()
    note = (match.group("note") or "").strip()
    if not emoji_id or not sense:
        return None
    # `note` / `笔记` 是**留给"停用一条笔记"那个形状的**（见 `_parse_quote_note_toggle`）：
    # 不在这里挡掉的话，`/super quote note off` 会写成"emoji=note、方向=off"，
    # 操作者以为停用了笔记，实际往覆盖表里塞了一个不存在的表情。
    if emoji_id.casefold() in {"note", "笔记"}:
        return None
    if sense.casefold() in QUOTE_OVERRIDE_AUTO:
        return emoji_id, "auto", note
    return emoji_id, sense, note


# `/super quote note <笔记id> off|on`：停用 / 恢复**某一条笔记**。
# 笔记的 id 是**内容指纹**（`quote_learning.note_id`），`/super quote` 那一列里印出来；
# 模型每一轮都会整份重写笔记，所以只有按内容记的停用才活得下去。
_QUOTE_NOTE_PATTERN = re.compile(
    r"^[/#]super\s+(?:quote|quotes|金句|表情含义)\s+(?:note|笔记)\s+(?P<id>\S+)\s+"
    r"(?P<state>\S+)$",
    re.IGNORECASE,
)
#: 停用 / 恢复的关键字。
QUOTE_NOTE_OFF = frozenset({"off", "no", "0", "停用", "关", "关闭", "别用"})
QUOTE_NOTE_ON = frozenset({"on", "yes", "1", "恢复", "启用", "开", "打开"})


def _parse_quote_note_toggle(text: str) -> tuple[str, bool] | None:
    """解析"停用/恢复一条笔记"那一段；不是这个形状就返回 None。"""

    if not isinstance(text, str):
        return None
    match = _QUOTE_NOTE_PATTERN.fullmatch(text.strip())
    if not match:
        return None
    note_id = match.group("id").strip()
    state = match.group("state").strip().casefold()
    if not note_id:
        return None
    if state in QUOTE_NOTE_OFF:
        return note_id, False
    if state in QUOTE_NOTE_ON:
        return note_id, True
    return None


# `/super slang <动作> <词> [新释义]` 的动作词。给的是**中文与英文两套**写法
# （与 `/super quote <方向>` 接受"正/不赞成/positive"同一套脾气：手机上打哪个都行）。
SLANG_VERBS = {
    "list": "list", "列表": "list", "看": "list",
    "all": "all", "全部": "all", "所有群": "all",
    "why": "why", "证据": "why", "原话": "why",
    "set": "set", "改": "set", "改释义": "set", "update": "set",
    "del": "del", "delete": "del", "删": "del", "删除": "del",
    "wrong": "wrong", "标错": "wrong", "no": "wrong",
    "ok": "ok", "取消标错": "ok", "恢复": "ok", "yes": "ok",
}


def _parse_slang_edit(text: str) -> tuple[str, str, str]:
    """解析 `/super slang ...` → `(动作, 词, 新释义)`。

    形状（与 `/super quote` 同一套脾气：**不给动作就是"看"**）：

    | 输入 | 结果 |
    | --- | --- |
    | `/super slang` / `list` | `("list", "", "")` |
    | `/super slang all` | `("all", "", "")` |
    | `/super slang why 电赛` | `("why", "电赛", "")` |
    | `/super slang set 电赛 电子设计竞赛` | `("set", "电赛", "电子设计竞赛")` |
    | `/super slang 电赛 电子设计竞赛` | 同上（动作可以省） |
    | `/super slang 电赛` | `("why", "电赛", "")`（只给词 = 看它的证据） |
    | `/super slang del 电赛` | `("del", "电赛", "")` |
    """

    if not isinstance(text, str):
        return "list", "", ""
    body = re.sub(r"^[/#]super\s+(?:slang|黑话|词条)\s*", "", text.strip(), flags=re.IGNORECASE)
    tokens = body.split()
    if not tokens:
        return "list", "", ""
    verb = SLANG_VERBS.get(tokens[0].casefold(), "")
    if verb:
        word = tokens[1] if len(tokens) > 1 else ""
        definition = " ".join(tokens[2:]).strip()
        return verb, word, definition
    word = tokens[0]
    definition = " ".join(tokens[1:]).strip()
    if not definition:
        return "why", word, ""
    return "set", word, definition


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
    """按会话保存短期聊天记录和语境，默认不会跨群共享。

    **会话数有上限，超了淘汰最久不活跃的那个**（LRU）。由来（2026-10-08 隔离增长
    审计 [1]）：`states` 从前只有 `setdefault` 与 `restore` 两处写入、**没有任何删除
    点**——`/admin disable`、`/super clear` 只把 mode 置回去（`leave()`），历史留在
    内存里，真正的回收只发生在进程重启。上限**调用时现读**（`dev_config`），
    淘汰只看"上限 + 最久不活跃"，**与开不开对话无关**：用户 2026-10-08 定死
    `/admin disable` 只等于"不回复"，历史与记忆照记。

    淘汰时通过 `on_evict` 通知引擎，好让它把那个会话的按会话 client 也一起丢掉
    （审计 [5]：那两个字典是强引用，不跟会话走就等于会话状态清不掉）。
    """

    def __init__(self, *, on_evict=None) -> None:
        self.states: dict[str, ConversationState] = {}
        self.triggers: dict[str, IdleTrigger] = {}
        #: 被上限淘汰掉的会话数（观测用；只记数）。
        self.evicted = 0
        #: 淘汰一个会话时要顺带清理的东西（引擎装的是 `_drop_session_clients`）。
        self.on_evict = on_evict

    def state(self, session_id: str) -> ConversationState:
        state = self.states.get(session_id)
        if state is None:
            state = ConversationState(history_limit=HISTORY_LIMIT)
            self.states[session_id] = state
            self._enforce_limit(session_id)
        return state

    def _enforce_limit(self, keep: str | None) -> None:
        """会话数上限：超了就淘汰**最久不活跃**的那个（LRU），一次一个。

        `keep` 是这一轮刚碰过的会话，**不许**淘汰它（新建的状态 `last_activity_at`
        还是 None，否则它会被自己立刻淘汰掉，调用方拿到一个已经不在表里的 state）。
        """

        limit = max(1, dev_config.session_cache_max_sessions())
        while len(self.states) > limit:
            victim = self._least_recent_session(keep=keep)
            if victim is None:
                return
            self._drop(victim)

    def _least_recent_session(self, *, keep: str | None) -> str | None:
        """最久不活跃的那个会话；从没活跃过的（`None`）排在最前。"""

        candidates = [(sid, st) for sid, st in self.states.items() if sid != keep]
        if not candidates:
            return None
        return min(candidates, key=lambda item: item[1].last_activity_at or 0.0)[0]

    def _drop(self, session_id: str) -> None:
        """真的淘汰一个会话：把它的状态摘出内存，并通知引擎做联动清理。"""

        state = self.states.pop(session_id, None)
        if state is None:
            return
        # 同一个 key 上的两样东西一起走：留下一个"状态已经不在了"的触发器没有意义。
        self.triggers.pop(session_id, None)
        self.evicted += 1
        logger.info(
            "short-term session evicted (LRU): session=%s remaining=%s history=%s",
            session_id, len(self.states), len(state.history),
        )
        if self.on_evict is not None:
            try:
                self.on_evict(session_id)
            except Exception:  # noqa: BLE001 - 清理失败不该影响收消息
                logger.exception("session eviction hook failed: session=%s", session_id)

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
        # 恢复回来的也受同一个上限管（持久化的那份至多 `MAX_PERSISTED_SESSIONS` 条，
        # 但上限配小了的话，这里要立刻收紧；`keep=None` = 没有"刚碰过"的那一个）。
        self._enforce_limit(None)
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
        memory_group_ids: frozenset[str] | None = None,
        relay_target_resolver: RelayTargetResolver | None = None,
        runtime_diagnostics: "RuntimeDiagnostics | None" = None,
        memory_service: MemoryService | None = None,
        state_store=None,
        context_provider=None,
        clock=time.monotonic,
        # 印给人看的时间用**墙上时钟**（`time.time`）。刻意与 `clock` 分开：
        # `clock` 只算时长（焦点值班、冷却、耗时），`wall_clock` 只印日期时刻。
        # `handle` 里那个 `now`（`self.clock()`）**同时喂着焦点值班的数学**，
        # 所以不能整体换成墙上时钟——只把"显示/提示"那几处改成 `self.wall_clock()`。
        wall_clock=time.time,
        group_listen: bool | None = None,
        typing_sim: bool | None = None,
        command_registry=None,
        judge_client=None,
        client_factory=None,
        judge_client_factory=None,
        feature_logs: FeatureLogs | None = None,
        style_reviewer=None,
        vision=None,
        remind=None,
        host: HostServices | None = None,
        quote_profile=None,
        slang_library=None,
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
        # 会话被 LRU 淘汰时，把那个会话的按会话 client 也一起丢掉
        # （2026-10-08 隔离增长审计 [5]）：`_session_clients` / `_session_judges` 是
        # 强引用，各自持一份"按会话隔离的缓存空间"，不跟会话走就等于状态永远清不掉。
        self.sessions.on_evict = self._drop_session_clients
        # 跨重启的累计账本（runtime 会塞进来；测试里是 None，等于只报本次进程）。
        self.usage_store = None
        # 群成员角色事实（**核心的能力**，2026-10-06 从插件搬进来）：回答
        # "某人在某群是什么角色"与"她自己是什么角色"。群主专属动作靠它决定能不能做；
        # 名册（`<people>`）也用它标出谁是管理员/群主。
        # 测试里是 None 时一律按"查不到"处理（fail-closed），不会误当成群主。
        #
        # `runtime.build_engine` 装的是 `_LazyGroupRoles`（懒构造，模块不在就降级成
        # "一律不知道"）；这里默认 None 保持"直接造引擎"的测试与旧调用方不变。
        self.group_roles = None
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
        # **哪些群开记忆**（`/super enable|disable memory` 在哪个群说的，就动哪个群）。
        #
        # 它与 `enabled_group_ids`（哪些群开对话）是**两张互不相干的名单**：
        # `enabled_group_ids` 只决定"回不回"，这张只决定"记不记"。2026-10-08 之前
        # 记忆用的是**上面那一张**，于是有人在群里发一句 `/admin disable` 就等于
        # 把那个群还没处理完的记忆材料清空（体检实测丢过 249 条普通聊天）。
        # 用户当天定的口径：*"我要的是**记忆**不是对话"*、*"只是不回复，不要影响
        # 上下文和记忆入栈"*。
        #
        # 默认**空**：一个群都不记，等操作者在要记的群里逐群 `/super enable memory`。
        # 这里刻意不回落成 `{target_group_id}`——那会把"单群部署"与"这个群要记忆"
        # 重新捆在一起，正是这次要拆开的东西。空名单在 `/super memory` 里看得见。
        self.memory_group_ids = (set(memory_group_ids) if memory_group_ids is not None
                                 else set())
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
        # 风格审核（可选）：她写完、要发出去之前看一眼能不能发。**它只判断、不改写**
        # （2026-10-05）：判不过就把理由交回回复 agent 重写一次（见 `_rewrite_after_review`）。
        # 没配就是 None（直通）。
        self.style_reviewer = style_reviewer
        #: "这一轮现在跑到哪一段了"（2026-10-13 夜间复盘）：`""` / `judging` /
        #: `replying` / `compacting` / `vision`，由 `dev_config.set_turn_stage` 在
        #: 跑模型前写、`runtime.handle_turn_within_budget` 在 `turn timeout` 那行读。
        #: 主循环**串行**处理消息（一条一轮），所以一个引擎上它只有一个值。
        self.turn_stage = ""
        # 识图（可选）：有图的消息在**进判定之前**先看一眼，把 `[图片]` 换成一句描述。
        # 它只改正文，不新增决策路径——"要不要回、回什么"照旧全在原来那套里。
        self.vision = vision
        # 「到点提醒」那一半能力（2026-10-08 用户拍板：**核心听懂 + 插件做事 + 核心记录
        # + 核心发送**）。它是**插件提供**的一个可调用对象 `(RemindRequest) -> None`：
        # 核心把绝对时刻与邮箱算好、装进那一条请求再交给它；插件自己存、自己到点发。
        # **缺省 None = 这次部署没有提醒插件**：核心认到提醒意图也什么都不做
        # （不报错、不回复、不留半条状态）——`runtime.build_engine` 只按"有没有"取。
        self.remind = remind
        #: 提醒用的两份**按人**状态：
        #: `_remind_emails` 是**核心自己那一份按人的邮箱**（`remind.RemindEmailBook`：
        #: 落盘在 `data/remind_emails.json`、重启后仍在——用户要求"邮箱只第一次要"。
        #: 它**不是从记忆库回查来的**：那个值来自他在聊天里说过的那一次；
        #: 记忆库那条路只负责把这句话记进去（按人）。独立审查 2026-10-09 第 10 条）。
        #: `_remind_asks` 是"他还没给邮箱"的待办（值就是那条还没交出去的请求，
        #: 只在内存里）。**都只在核心手里**——插件那边只收到一个装好的
        #: `RemindRequest`，没有任何读回记忆的口子。
        self._remind_emails: RemindEmailBook = RemindEmailBook()
        self._remind_asks: dict[str, RemindRequest] = {}
        #: "这件事刚认过"：`(人, 归一化的事情)` → 过期时刻（墙上钟）。**同一件事只办一次**——
        #: 判定每轮都读得到窗口里那句老话，于是同一句提醒会被反复认出来
        #: （2026-10-09 实测两次：连记 5 次待办 + 两条提醒；5 分钟里刷 5 条）。
        #: 窗口见 `remind.REMIND_DEDUPE_WINDOW_SECONDS`，上限见 `MAX_REMIND_SEEN`。
        self._remind_seen: dict[tuple[str, str], float] = {}
        #: 同一个人**上一次交给插件**是什么时候（墙上钟）——频次兜底用，
        #: 闸门见 `dev_config.remind_min_interval_seconds()`。
        self._remind_last_handover: dict[str, float] = {}
        #: 同一个人**上一次真正交给插件的收法**（`group`/`private`/`email`）：
        #: "等渠道"等超时时按他上次用过的那种办（见 `_expire_remind_channel_wait`）。
        self._remind_last_channel: dict[str, str] = {}
        #: 一个人**从什么时候开始**在等"怎么收"（墙上钟）：超时兜底用，交出去就清掉。
        self._remind_ask_since: dict[str, float] = {}
        #: 按人记住的"**以后都这么干**"那种收法（只有他明说过才写，见 `_remember_remind_channel`）。
        #: **普通选一次不写这里**——用户 2026-10-09 的规矩是"每一次都问，除非说以后都这么干"。
        self._remind_channel_pref: dict[str, str] = {}
        #: 这一轮里"他刚说了以后都这么干" → `人 → (消息 id, 收法)`：她那句确认要说清
        #: "以后都这么办"（别只应一声"行"）。
        self._remind_forever: dict[str, tuple[str, str]] = {}
        #: 这一轮里"收法是按他那条约定的" → `人 → (消息 id, 收法)`：让她顺口**复述**一句
        #: （"还照老规矩"），别重复问。
        self._remind_restate: dict[str, tuple[str, str]] = {}
        #: 他**最近一条**提醒（还没发的也算）——"这次群里叫我"要改的就是它。
        self._remind_last_request: dict[str, RemindRequest] = {}
        #: 这一轮里"他要改**这一次**的收法" → `人 → (消息 id, 收法)`：她那句确认要说清
        #: "这次…"（与"以后都这么办"分开）。
        self._remind_this_time: dict[str, tuple[str, str]] = {}
        #: 这一轮里提醒意图**被闸拦下**了 → `人 → 消息 id`：要让她知道"没排上"，
        #: 否则她会照旧答应一个时间（2026-10-09 实测：`一点四十叫你`，而 13:40 什么都不会发生）。
        self._remind_blocked: dict[str, str] = {}
        #: 「群聊」那一页的**群列表缓存**（`list_groups`）：一次查询的结果 + 取到的时刻
        #: （墙上钟）。ttl 见 `dev_config.groups_cache_seconds()`（默认 600 秒）。
        self._groups_cache: tuple[dict[str, object], ...] = ()
        self._groups_cache_at = 0.0
        # 金句学习学出来的那份**材料**（`quote_learning.QuoteProfile`，可选）。
        # 它只是"读一份本地 JSON 挑几条"，**热路径上一次模型调用都没有**；
        # 关掉开关或没有材料时回复 prompt 与改动前**逐字相同**（有测试钉住）。
        self.quote_profile = quote_profile
        # 黑话词条（她**被动听来**的那份，`slang_learning.SlangStore`）。
        # 与金句那份一样：**读一份本地 JSON**，热路径上一次模型调用都没有；
        # `runtime` 装配时把它接上，没接上（测试、干跑）时 `/super slang` 如实说"没有接入"。
        # `slang_watcher` 是**收消息那一侧**的观察者，由 `runtime` 挂上——
        # 引擎自己不调它（对话路径一个字都不用改）。
        self.slang_library = slang_library
        self.slang_watcher = None
        # 「没把握就别断言」这条路（2026-10-04 起叫"不懂就问"，2026-10-05 晚改口径）：
        # **没有限量器了**。旧版有一个 `AskBudget`（同一话题最多问一次），因为那时
        # "以问回应"会被她每句都用一遍；现在那条路拆掉了，兜底是"打回重写一次"——
        # 一次重写本身就是上限，不存在"每句都问"，所以这里不再持有一个配额账本。
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
        # 用 `dict` 而不是 `set`：插入序就是"谁先排的队"，超上限时淘汰**最旧**的那条
        # （与 `_promise` 同一写法）。条目正常只活到"那条消息被处理完"。
        self._resumed_ids: dict[str, None] = {}
        # 已经回过"稍等"、因此**必须给结果**的消息 id（判定不许否决）。
        # 用 `dict` 而不是 `set`：插入序就是"谁先答应的"（见 `_promise`）。
        self._promised_ids: dict[str, None] = {}
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
            # 单个群的排队上限（条）。队列里是消息全文，所以它必须有上界——
            # 2026-10-08 隔离增长审计 [7]：从前 `deque()` 没有 maxlen。
            queue_limit=dev_config.FOCUS_QUEUE_LIMIT,
        )
        # 判定采样（2026-10-10 用户："消息来得太快时攒几条再判"）：群消息速率高了
        # 就攒 5/10/20 条再让判定 agent 看一眼；被 @ 的不等攒满；私聊不进来。
        # 只拦"这条消息要不要触发判定调用"，别的（排队/调度/出站）一概不碰。
        self.judge_sampling = JudgeSampler()
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
        # **两个起始点，各管一摊**（2026-10-08 安全与时间口径审计 B-02）：
        # `started_at` 是墙上时刻，只用来印"开始于"；`started_monotonic` 是单调读数，
        # 只用来算"已运行多久"。从前 `uptime` 用 `time.time() - started_at` 算——
        # 一次 NTP 校时（或开机 RTC 错一次）就能让它跳变，往回拨还会被夹成 0。
        self.started_at = time.time()
        self.last_cache_report: dict[str, object] | None = None
        self.state_store = state_store
        self.clock = clock
        self.started_monotonic = self.clock()
        # **墙上时钟**：只给"印给人看的时间"用（她那边/群里这边那两个时刻）。
        # 与 `self.clock`（单调，只算"过了多少秒"：焦点值班、冷却、耗时）是两回事——
        # 混用会把开机时长印成 1970（2026-10-08 用户报的问题，见 `_now_text`）。
        self.wall_clock = wall_clock
        self.enabled = True
        self._stats = {name: 0 for name in (
            "accepted_messages", "ignored_messages", "blocked_messages",
            "deferred_messages", "model_calls", "replies", "history_seeded",
            "reply_segments", "judge_calls", "compaction_calls",
            # 焦点观测：排进队列多少条、回了多少"稍等"、切换了几次当值群、
            # 以及"答应过必须回"的消息里被回复段拒了几条（异常，应当为 0）。
            "focus_queued", "focus_acks", "focus_switches", "focus_queued_replies",
            "focus_declined_promised", "empty_forced_reply",
            # 关系：判定报了几次"这一句越界"、其中被机械上限拒了几次（应当很少）。
            # `guard_probe_held` 是"有人打探她怎么做出来的，所以这一轮不接"的次数
            # （2026-10-09 加）：它**不写关系轴**，只是"这一轮没开口"的出口。
            "guard_raised", "guard_rejected", "guard_probe_held",
            # 风格审核（只判不改）：被打回、要求重写的条数，"重写后仍被拒、
            # 照重写的那一版发出"的条数，以及"她把原来那句原样写回来 = 拒绝改"
            # 的条数（2026-10-11；没配审核时三者恒为 0）。
            "reply_reviewed", "reply_review_rejected_final",
            "reply_review_rewrite_declined",
            # 识图成功几次（把 `[图片]` 换成描述的次数；没配识图时恒为 0）。
            "media_described",
            # 「没把握就别断言」（旧名"不懂就问"）：`clarify_quiet` 是"没被叫到、没把握、
            # 又是具体话题，所以没插话"的次数；`reply_grounding_rewrites` 是"被叫到、
            # 话里有具体断言、三处都没有根据，于是被打回重写一次"的次数。
            # （关掉 `ask_when_unsure` 开关时两者恒为 0。只有计数，没有正文。）
            "clarify_quiet", "reply_grounding_rewrites",
            # 不可信数据的短码（`data_boundary`，2026-10-08）：`data_marker_retries` 是
            # "回复里抄了当轮短码、被打回重写一次"的次数；`data_marker_drops` 是
            # "重写又抄、于是这一条没发出去"的次数（fail-closed）。
            # 只有计数：**不记短码、不记正文**。
            "data_marker_retries", "data_marker_drops",
            # 出站正文里剥掉的**孤立标签片段**（2026-10-09 用户实测 `</parameter>`）：
            # 模型吐了半截协议/工具调用 XML，收尾标签留在正文里被发了出去。
            # 计的是**剥掉的标签个数**，日志里只写标签名——**不记正文**。
            "reply_tags_scrubbed",
            # 判定/回复这两条路的模型调用失败（2026-10-08，失败降级审计 1）：失败时
            # **照样沉默**（那是刻意的，测试钉着），但"她突然不说话"必须看得见——
            # 只有计数与"连续几次"，没有正文。
            "judge_failed", "reply_failed",
            # 一轮对话超过**总预算**（`QQBOT_TURN_TIMEOUT_SECONDS`，2026-10-08 失败降级
            # 审计 2）：主循环串行，没有上界时模型"拖着不回"会把整个接收循环卡住
            # （连 ping / 管理命令 / 私聊都进不来）。超时 = 这一条没有回复，
            # **历史照留**（她该知道群里发生过那句话）。
            "turn_timeouts",
        )}
        # 连续失败次数（成功一次就归零）：单次失败可能只是网络抖一下，连续几次才是
        # "她又突然不说话了"。只用来决定 `/super status` 那一行要不要升级成"异常"。
        self._judge_failed_streak = 0
        self._reply_failed_streak = 0
        self._pending_relays: dict[str, PendingRelay] = {}
        self._pending_relay_selections: dict[str, PendingRelaySelection] = {}
        self._relay_deliveries: deque[RelayDelivery] = deque()
        # 关系现状的小缓存：判定与回复在同一轮里各读一次，不该查两遍库。
        # 判定那条通道写入时主动失效（"立刻变冷"靠这个），TTL 是给维护 agent
        # 那条通道兜底的——它在别的线程改库，引擎收不到通知。
        self._stance_cache: dict[str, tuple[Stance, float]] = {}

    @property
    def self_roles(self):
        """**兼容别名**：群成员角色事实服务原来叫 `self_roles`（插件侧那棵树上还是）。

        它现在只是 `group_roles` 的另一个名字（读写都落到同一个属性上），
        所以不会出现"两份角色缓存、查到两套答案"。新代码请用 `group_roles`。
        """

        return self.group_roles

    @self_roles.setter
    def self_roles(self, value) -> None:
        self.group_roles = value

    def _data_marker(self, state) -> str:
        """这一轮边界记号的取法（两档，见 `data_boundary` 模块说明"两档"）。

        * 总闸门关掉（`data_marker_enabled`）→ `""`：下面每一处包裹与判定都变成恒等操作；
        * **缓存友好型**（默认，`data_marker_cache_friendly`）→ `state.ensure_season_marker()`：
          一季一个（3 个全角生僻符号），**压缩时换一季**；同一季里逐轮一字不差，所以
          带记号的金句材料能跟着它落进前缀缓存；
        * **增强型** → `session_marker()`：每轮现取 7 位随机，抄一次就作废。

        两档只在**取法**上不同：入站中和（`neutralize_text`）与出站判违规
        （`reply_leaks_marker`）都认两种形状，切档不留绕过。
        """

        flags = self._flags()
        if not flags.data_marker_enabled:
            return ""
        if flags.data_marker_cache_friendly:
            return state.ensure_season_marker()
        return data_boundary.session_marker()

    def _style_material(self, message: IncomingMessage, state) -> str:
        """「她自己的老习惯 / 她踩过的坑」那块材料（**热路径上零模型调用**）。

        来源是金句学习 agent 定期学出来的笔记（`quote_learning.QuoteProfile`）。五条纪律：

        1. **只在开关打开时读**：`quote_enabled` 关掉 = 直接空串，于是请求与改动前逐字相同；
        2. **任何异常都当"没有材料"**：读文件、挑笔记都是本地的，但仍然包一层
           ——她这一轮说不说话。**绝不能因为材料读不到而影响回复**；
        3. **不是指令、也不是台词**：渲染在 `quote_learning.inject_block` 里，
           措辞是"她以前遇到这种时候"，进的是 user 段的 DATA 区（system 一个字不动）；
        4. **算出来的结果缓存在会话状态上**（2026-10-11 用户原话：*"我要丢到易变区开头，
           每几个小时更新一次，只有触发压缩的时候才更新，这样命中率更高"*，紧接着一条
           更正：*"到了 3 小时也得等压缩，只有同时达到 3 小时和压缩过后才会变"*）：
           **两个条件同时满足**才重算——自上次算它之后**压过一次**
           （`state.compactions` 变了）**并且**距上次算它已过
           `STYLE_MATERIAL_TTL_SECONDS`。缺一个就沿用旧的那一段字节，
           其余每一轮都返回**同一段字节**——前缀缓存按前缀算，一个字变了后面全得重算。
           第一次没有缓存时当然要算（否则永远没有）；
        5. **空材料不缓存**：没挑到笔记时返回空串、且**不记账**，下一轮还会再试
           （空串本来就不占字节，不会破坏前缀；反过来"空着缓存几个小时"才是真的坏）。
        """

        profile = getattr(self, "quote_profile", None)
        if profile is None or not self._flags().quote_enabled:
            return ""
        cached = getattr(state, "style_material", "")
        computed_at = getattr(state, "style_material_at", 0.0)
        if computed_at and cached:
            compacted = getattr(state, "style_material_compactions", -1) != state.compactions
            aged = (self.clock() - computed_at) >= STYLE_MATERIAL_TTL_SECONDS
            if not (compacted and aged):
                return cached
        try:
            from .quote_learning import inject_block

            notes = profile.notes_for(
                message.target.group_id or "",
                message.text or "",
                state.context.topic or "",
            )
            material = inject_block(notes)
        except Exception:  # noqa: BLE001 - 材料拿不到不该影响这一轮回复
            logger.warning("quote_material_failed", exc_info=True)
            return ""
        if material and hasattr(state, "style_material"):
            state.style_material = material
            state.style_material_at = self.clock()
            state.style_material_compactions = state.compactions
        return material

    def _reaction_material(self, message: IncomingMessage) -> str:
        """「**刚才**有人给她贴了什么」那块材料（2026-10-11，用户选的"甲"）。

        与 `_style_material` 的差别只有一处：它**逐轮**算——"有人刚给她那句话贴了 ㊗"
        是眼前这件事，不能跟着那块稳定材料一起冻几个小时。读的是对话日志的**尾巴**
        （有界、纯本地，见 `quote_samples.ReactionFeed`），失败当没有。
        """

        profile = getattr(self, "quote_profile", None)
        feed = getattr(self, "quote_reactions", None)
        if profile is None or feed is None or not self._flags().quote_enabled:
            return ""
        try:
            from .quote_learning import inject_block

            emoji_ids = feed.recent_emoji_ids(
                message.target.group_id or "", session_id=message.session_id or "")
            if not emoji_ids:
                return ""
            lines = profile.emoji_notes_for(
                message.target.group_id or "", emoji_ids, just_now=True)
            # 这里**不设**"最多几条"的额外限制：`ReactionFeed` 已经只给最多
            # `MAX_REACTION_MATERIALS` 个，`inject_block` 还有一道总上限。
            return inject_block(lines)
        except Exception:  # noqa: BLE001 - 材料拿不到不该影响这一轮回复
            logger.warning("quote_reaction_material_failed", exc_info=True)
            return ""

    def _review_context(self, message: IncomingMessage, state) -> str:
        """交给风格审核的一点现场：**谁在跟她说话、对方说了什么、现在聊的是什么**。

        2026-10-05 追加：这三样是判"事实错了""越权""出戏"的最低需要——只给草稿的话，
        审核看不出"这句话跟刚过去那段对得上吗"。给整段历史既贵又会把审核带偏成
        "替她重新聊天"，所以仍然是**最近两句 + 当前这句 + 当前话题**，截断到 600 字。

        它进的是审核那次请求的 **user 段**（`StyleReviewer._messages`）：只读，
        不动她的 system，也不动审核自己的 system。
        """

        recent = [item for item in state.recent()[-3:] if item.text and not item.is_bot_message]
        lines: list[str] = []
        for item in recent[-2:]:
            who = getattr(item, "sender_name", "") or "有人"
            lines.append(f"{who}说：{item.text}")
        who_now = getattr(message, "sender_name", "") or "有人"
        lines.append(f"{who_now}说：{message.text or ''}")
        topic = str(getattr(getattr(state, "context", None), "topic", "") or "").strip()
        if topic and topic != "未知":
            lines.append(f"现在聊的是：{topic}")
        return "\n".join(lines)[-600:]

    def _revision_request(self, request: list[dict[str, str]], draft: str,
                          reason: str, *, note: str = "",
                          redact: tuple[str, ...] = (),
                          allow_decline: bool = False) -> list[dict[str, str]]:
        """把"这一版被退回来了"接在**同一份请求**后面。

        为什么不重新拼一遍整个请求：`build_dialogue_messages` 有十几个参数，抄一遍
        迟早会抄漏一个——那就变成"重写那一次看到的语境和第一版不一样"。
        这里只在末尾追加一条 user 消息：她仍然是那个人、仍然看着同一段语境，
        只是知道了"刚才那句为什么不行"。**指令进 user 段**，system 前缀一个字不动。

        `note` 是"打回"的具体要求。两条路各自给一句现成的措辞（都在易变段、
        不常驻、不点机制词）：
        * 风格审核那一趟：`reason` 就是审核给的理由，没有别的话；
        * 「没把握就别断言」那一趟：`note=ask_when_unsure.NO_ASSERT_NOTE`
          ——"别断言你不知道的 / 说你能说的，或者说这个你不清楚"，**不是**"问她一句"。

        `redact`（2026-10-08）：这几串在**引述的那一版正文**里要被抹掉。
        只有"回复抄了边界短码"那一趟用它——否则"你刚写的是：…‹7K3Q9ZM›…"会把那个
        短码**又递回给她一次**，等于一边说不许抄、一边示范一遍。抹掉的位置写
        `（那串记号）`，她看得到"这里原本有个记号"，但拿不到可抄的字串。

        `allow_decline`（2026-10-11，用户："审核agent打回后回复agent可以拒绝重写
        原样发出"）：**只给风格审核那一趟**用。文字上把"拒绝"的办法说清楚——她
        能说的话只有要发出去的那一句，所以拒绝的表达方式就是**原样再写一遍**；
        引擎把"重写版与第一版逐字相同"读成"她认为原来那句本来就没问题"。

        2026-10-11 第二次改（真机探针实测 9 组：给她退路 0 次用，宁可顺着改一点）：
        用户裁决"只把提示写硬一点"——补上"这不算顶嘴、不会因为这个再退回来、
        改不改你定"。**不改机制**：打回仍然打回，只是把"她可以不认"说到明处。

        为什么只给它：另外两趟里，"没把握就别断言"与"别抄不可信短码"都不是审美，
        是**这一版确实有问题**；那两趟必须照打回的要求改，不设退路。
        """

        shown = draft or ""
        for token in redact:
            if token:
                shown = shown.replace(token, "（那串记号）")
        note_block = f"\n{note.strip()}\n" if note.strip() else ""
        decline_block = (
            "\n要是你觉得原来那句本来就没问题、改了反而更差，就把原来那句"
            "**原样再写一遍**（一个字都别改）——那我就照它发出去，"
            "不会因为这个再退回来。这不算顶嘴：改不改，你定。"
            if allow_decline else ""
        )
        body = (
            "--- 你刚写的那一句要重来 ---\n"
            f"你刚写的是：{shown}\n"
            f"不合适的地方：{reason or '（没写清楚是哪一处）'}"
            f"{note_block}\n"
            "照这个地方**重写一遍**——只改不合适的那处，别的不用动。"
            "不要解释、也不要提这一段，像平常那样把新的一句说出来。"
            f"{decline_block}"
        )
        return [*request, {"role": "user", "content": body}]

    async def _rewrite_after_review(self, request, draft: str, reason: str, *,
                                    session_id: str, trigger: str, trigger_kind: str,
                                    context: str, known_message_ids: frozenset[str],
                                    must_reply: bool, note: str = "",
                                    redact: tuple[str, ...] = (),
                                    allow_decline: bool = False) -> str | None:
        """打回之后：**在同一轮里**让回复 agent 重写一次。返回新正文或 `None`。

        两个调用方共用这一条路（**不另造机器**）：
        * 风格审核判不过（2026-10-05）：`reason` 是审核给的那一句；
        * 「没把握就别断言」（2026-10-05 晚）：她这一版话里有具体断言，
          而知识库 / 记忆 / 语境三处都没有根据 → 同一个动作，
          只是多带一句现成的 `note`（`ask_when_unsure.NO_ASSERT_NOTE`）。

        第三条（2026-10-08，不可信数据的边界短码）也走它：`reason` 是"你把那串短码
        写进去了"；`redact=(marker,)` 保证引述那一版正文时**不把短码又递回去**。

        几条边界（都是"宁可照旧，也不许把一条回复弄丢"）：

        * **最多一次**：这里只调一次模型，重写的结果不再送回回复 agent 循环重写；
        * 重写这一趟失败/空手/解析不出正文 → 返回 `None`，调用方**发第一版**；
        * 重写后再过一次审核，仍被拒 → **照发重写的那一版**，只记一笔
          （`reply_review_rejected_final`）——审核打回不是否决权；
        * 走的是同一个回复 client（`_client_for`）、同一条调用路径与同一份账本，
          没有另造一套客户端。

        `allow_decline`（2026-10-11，用户："审核agent打回后回复agent可以拒绝重写
        原样发出"）：**只有风格审核那一趟传 True**。她的拒绝没法用一句话说出来
        （说什么都会变成发出去的那条），所以约定俗成：把原来那句**原样再写一遍**
        就是"不改"。这里认出这种重写 → 记一笔 `reply_review_rewrite_declined`、
        返回 `None`（调用方照第一版发）。**这是她对自己那句话的最后一次发言权**：
        审核能说"不行"，但不能替她把话改成另一句。
        """

        retry_request = self._revision_request(request, draft, reason, note=note,
                                               redact=redact, allow_decline=allow_decline)
        self._stats["model_calls"] += 1
        started_at = self.clock()
        try:
            raw = await self._client_for(session_id).complete(retry_request)
        except Exception as exc:  # noqa: BLE001 - 重写失败必须退回第一版，绝不丢回复
            summary = exc.safe_summary() if isinstance(exc, LLMError) else type(exc).__name__
            self._log_model_io("reply", retry_request, "", session_id=session_id,
                               trigger=trigger, error=summary)
            logger.warning("Stage 3 rewrite after review failed kind=%s；按第一版发出", summary)
            return None
        elapsed = self.clock() - started_at
        self.metrics.model_latency.observe(elapsed)
        self._log_model_io("reply", retry_request, raw, session_id=session_id,
                           trigger=trigger, elapsed=round(elapsed, 2))
        rewritten = parse_dialogue_output(raw, known_message_ids=known_message_ids,
                                         must_reply=must_reply)
        if rewritten.kind is not DecisionKind.REPLY or not rewritten.text:
            logger.warning("Stage 3 rewrite after review came back empty；按第一版发出")
            return None
        if allow_decline and rewritten.text.strip() == (draft or "").strip():
            # 她把原来那句原样写回来了 = "我不改"：按第一版发，记一笔（只记数，不记正文）。
            self._stats["reply_review_rewrite_declined"] += 1
            logger.info("Stage 3 rewrite declined by the reply agent: chars=%s",
                        len(rewritten.text))
            return None
        if self.style_reviewer is None:
            # 没配审核（或审核被关掉）时，这一条路只有"打回重写"这一半：
            # 重写的结果不再过一遍审核——没有审核可过。**它绝不能在这里炸**：
            # 「没把握就别断言」这一关不依赖审核是否存在。
            logger.info("Stage 3 rewritten after grounding/review: chars=%s final=%s",
                        len(rewritten.text), rewritten.text[:60])
            return rewritten.text
        still_passed, still_reason = await self.style_reviewer.review(
            rewritten.text, context=context)
        if not still_passed:
            # **不许把回复丢掉**：重写后仍被拒也照发这一版，只记一笔。
            self._stats["reply_review_rejected_final"] += 1
            logger.warning(
                "Stage 3 rewrite still rejected by review; sending it anyway: reason=%s final=%s",
                still_reason[:80], rewritten.text[:60])
        else:
            logger.info("Stage 3 reply rewritten after review: chars=%s final=%s",
                        len(rewritten.text), rewritten.text[:60])
        return rewritten.text

    async def _apply_vision(self, message: IncomingMessage, state) -> IncomingMessage:
        """识图：把 `[图片]`/`[表情包]` 换成一句描述，并把历史里那条也一起改掉。

        为什么历史也要改：她后面几轮再回头看这段对话时，看到的应该是"他发了张猫的图"，
        而不是一个 `[图片]` 占位符——不然那轮之后"图里有什么"就彻底丢了。
        识图失败（返回空）时原样返回，占位符留着。

        **表情包要按表情包问**（2026-09-30 用户："表情包和图片分不清"）：
        `media_kinds` 说这一条是贴图还是照片；整条都是贴图时才按贴图去描述，
        混着两种就当一个普通画面问，免得把照片也讲成梗图。
        """

        kinds = tuple(getattr(message, "media_kinds", ()) or ())
        sticker = bool(kinds) and all(kind == "sticker" for kind in kinds)
        # "这一轮被砍在哪一段"（2026-10-13 夜间复盘）：识图（它有自己的一档预算，
        # 见 dev_config 那段注释）。
        dev_config.set_turn_stage(self, "vision")
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

    def _note_remind(self, message: IncomingMessage, verdict: JudgeVerdict) -> None:
        """判定认出来的"到点提醒我"：**核心算时间，把请求交给插件**（2026-10-08）。

        形状是用户拍板的"**核心听懂 + 插件做事 + 核心记录 + 核心发送**"，而且**对话调起**
        （群里说"云茹，8 点提醒我交作业"），不是斜杠命令——所以它读的是**判定那一次调用的
        返回**（`JudgeVerdict.remind`），**一次模型调用都不多**。

        这里只做核心该做的三件：把时间说法算成**绝对时刻**（只有核心知道"群里这边"是哪个
        时区，见 `remind.parse_remind_time`）、把值装进 `RemindRequest`、有插件就交给它。
        **时间认不清就当普通聊天**：不猜、不追问、什么都不发生。

        **没有插件**（`self.remind is None`）= 认到了也什么都不做——不报错、不回复、
        不留半条状态；插件自己抛错也一样只记一行日志，绝不吃掉这一轮回复。
        """

        intent = getattr(verdict, "remind", None)
        if intent is None:
            return
        # **只认"这一轮真正的用户消息"**（2026-10-09 用户实测，她刷屏那次的主因）：
        # 她自己发出去的那句"@某人 到点了：给dsh做插件使用反馈"会被 QQ 回显成一条消息，
        # 而判定读的是整段谈话——于是"有人让你提醒"在她自己的话里又成立了一次，
        # 又排一条，循环。**判定模型那侧一个字没改**（它照旧读整段、照旧可能报提醒），
        # 是**核心**在这里不采信"机器人自己说的话"作为提醒请求。
        if message.is_bot_message:
            logger.info("Stage 3 remind: 这是她自己发的话，不当作请求 session=%s",
                        message.session_id)
            return
        now = self.wall_clock()
        chosen = remind_channel(intent.channel)
        # **时间必须出自"他这一条"**（2026-10-09 她刷屏那次加的第二道）：判定读的是整段
        # 谈话，她发出去的那句"到点了：…"也在里面，于是"有人让你提醒"在**老话**上又成立了一次。
        # 所以这里要求**当前这条消息自己带一个认得出的时间**——老话里翻出来的不算。
        # （判定那侧一个字没改：它照旧读整段、照旧可能报提醒；采信与否是核心的事。）
        if parse_remind_time(message.text or "", now=now) is None:
            # 这一条里没有新时间。两种情况要分开（2026-10-09 用户加的那句
            # *"然后要可以改，比如说叫云茹这次群里叫我"*）：
            if chosen:
                # 他说了收法、只是没给时间 → **改这一次**（改那条最近的、还没发出去的）。
                if self._remind_change(message, chosen):
                    return
                # 没有待发的提醒可改 → 什么都不做（不报错、不新建），让她的正常回复去接。
                logger.info("Stage 3 remind: 他要改收法，可没有待发的提醒 session=%s",
                            message.session_id)
                return
            if self._remind_asks.get(message.user_id) is not None \
                    and not self._remind_asks[message.user_id].channel:
                # **已经在等"怎么收"了**：这一轮别再让她问几点（时间早就有了），
                # 由 `_remind_ask_note` 那句"必须问清怎么叫他"来说——实测那条链就是断在
                # "只问一轮"上的（2026-10-09："还是没叫我"）。
                logger.info("Stage 3 remind: 还在等怎么收，这一句不再当新请求 session=%s",
                            message.session_id)
                return
            self._note_remind_blocked(message)
            logger.info("Stage 3 remind: 这一条里没有时间（判定是从老话里翻出来的），不算 session=%s",
                        message.session_id)
            return
        at = parse_remind_time(intent.time_text, now=now)
        if at is None:
            self._note_remind_blocked(message)
            logger.info(
                "Stage 3 remind: 时间说不清，这一句当普通聊天 session=%s", message.session_id)
            return
        # **每次都问**（2026-10-09 用户改的规矩：*"我要的是每一次都问，除非说以后都这么干"*）：
        # 他以前选过**不算数**——只有他明说过"以后都这么干"这类意思时，核心才记住这种收法、
        # 以后不再问。下面的 `forever` 就是这个判断（保守正则，见 `remind.forever_channel`）。
        forever = forever_channel(message.text or "", chosen)
        pref = self._remind_channel_pref.get(message.user_id, "")
        if forever:
            self._remember_remind_channel(message.user_id, forever)
            pref = forever
            self._remind_forever[message.user_id] = (message.message_id, forever)
        elif chosen and said_this_time(message.text or ""):
            # "这次群里叫我" + 同时给了一件新事：收法只管**这一次**，确认里要说清。
            self._remind_this_time[message.user_id] = (message.message_id, chosen)
        # 他**这一轮**说的收法优先；没有才看他有没有"以后都"那条约定的。
        reused = not chosen and bool(pref)
        channel = chosen or pref
        email = self._remind_emails.get(message.user_id)
        pending = self._remind_asks.get(message.user_id)
        # **同一件事只办一次**（2026-10-09 实测两次：第一次是"连记 5 次待办 + 两条提醒"，
        # 第二次是"她刷屏 5 条"）。键是 `(人, 归一化之后的事情)`——比**原文**比不出来：
        # 她每次写法都略不同（空格/标点/全角半角），归一化之后才是同一把键。
        key = (message.user_id, remind_key(intent.what))
        hot = self._remind_seen.get(key, 0.0) > now
        same_pending = pending is not None and pending.key == key[1]
        if hot and not same_pending:
            logger.info("Stage 3 remind: 同一件事已经认过，跳过重复 session=%s",
                        message.session_id)
            return
        if not hot:
            self._remember_remind_seen(key, now)
        if same_pending:
            # **同一件事**：把这一轮读到的新东西并进去（他改了口 = 改时间 / 改事 / 才说清
            # 怎么收），**不新开一条**——这就是"只记一次待办"，也是"改口就更新那一条"。
            request = replace(pending, at=at, channel=channel or pending.channel, email=email,
                              what=intent.what)
            changed = request != pending
        else:
            request = RemindRequest(
                person_id=message.user_id,
                session_id=message.session_id,
                group_id=message.target.group_id or "",
                at=at,
                what=intent.what,
                email=email,
                key=key[1],
                channel=channel,
                forever=bool(forever) or (reused and bool(pref)),
            )
            changed = True
        self._remember_remind_ask(request)
        if changed:
            logger.info("Stage 3 remind: 记下这一条（怎么收=%s）session=%s",
                        request.channel or "还没说", message.session_id)
        if reused:
            # 他这回没说收法、是用了他"以后都这么干"那条约定的 → 让她**复述**一句，别再问。
            self._remind_restate[message.user_id] = (message.message_id, channel)
        self._try_hand_over_remind(request)

    def _remind_change(self, message: IncomingMessage, chosen: str) -> bool:
        """**改这一次**：他这一条没有新时间、只说了"这次群里叫我" → 改那条最近的提醒。

        用户 2026-10-09 原话：*"然后要可以改，比如说叫云茹这次群里叫我"*。规矩：

        * 改的是**本会话里他最近一条还没发的**（核心这边记着的那一条；交出去的那条带着
          同一把 `key`，插件按它更新而不是新增）；
        * **只改这一次**：`forever=False`、**不动**长期偏好（`_remind_channel_pref`）；
          这条发完即止，下一次那条回到"每次都问"；
        * **没有待发的提醒** → 返回 False，什么都不做（不报错、不新建）。
        """

        # 要找的是**他最近那一条**：既包括已经交给插件的，也包括**核心自己手里
        # 还在等收法的那条**（2026-10-09 实测：13:49:10 "他要改收法，可没有待发的提醒"
        # ——那条正是因为只找插件队列里的，而核心手里那条等渠道的没算进去）。
        target = (self._remind_asks.get(message.user_id)
                  or self._remind_last_request.get(message.user_id))
        if target is None or target.channel == chosen:
            return False
        updated = replace(target, channel=chosen, forever=False)
        self._remind_last_request[message.user_id] = updated
        self._remind_this_time[message.user_id] = (message.message_id, chosen)
        logger.info("Stage 3 remind: 改这一次的收法 → %s session=%s", chosen, message.session_id)
        self._try_hand_over_remind(updated, update=True)
        return True

    def _expire_remind_channel_wait(self, message: IncomingMessage) -> None:
        """"等他说怎么收"等太久了：**先按他上次用过的收法办，没有就群里 @**。

        由来（2026-10-09 实测"还是没叫我"）：他 13:40 说了"五分钟后提醒我去导员办公室"，
        而她那一轮没问清收法（她甚至自己默认了群里），这条就一直卡在"等渠道"上、
        **永远不响**。提醒**迟到总比不到强**：

        * 等过 `dev_config.remind_channel_wait_seconds()`（默认 600 秒），**或者**
          提醒时刻已经到了（再等就没意义了）→ 就用 `_remind_last_channel` 那种
          （他上次真用过的），没有就 `"group"`；
        * **不写进**长期偏好（`_remind_channel_pref`）——下一次照旧每次都问。

        为什么选"上次用过的"而不是一律群里：**群里 @ 会吵到别人**，而他上次自己挑过
        哪种就说明他更愿意那样收；实在没得挑时才用群里（那也正是他提问的地方，
        最不容易漏掉——这次的问题就是"一条都没响"）。
        """

        pending = self._remind_asks.get(message.user_id)
        if pending is None or pending.channel:
            return
        since = self._remind_ask_since.get(message.user_id)
        if since is None:
            return
        now = self.wall_clock()
        waited = now - since
        if waited < dev_config.remind_channel_wait_seconds() and now < pending.at:
            return
        fallback = self._remind_last_channel.get(message.user_id) or "group"
        logger.info("Stage 3 remind: 等收法等了 %s 秒还没等到，先按 %s 办 session=%s",
                    int(waited), fallback, message.session_id)
        self._try_hand_over_remind(replace(pending, channel=fallback, forever=False))

    def _note_remind_blocked(self, message: IncomingMessage) -> None:
        """提醒意图**被闸拦下**时留一句给她（同一轮就用掉，别漏到下一轮）。

        2026-10-09 用户实测：闸拦得对（"这一条里没有时间…不算"），但**没把"没排上"
        告诉她**——她于是照旧答应"行，一点四十叫你，414是吧"，而 13:40 什么都不会发生。
        所以这里留一句（由 `_remind_ask_note` 在**这一轮**的易变段里说出来）：让她自己
        问一句几点，**别自己先许一个时间**。键是 (消息 id)，所以不会漏到下一轮去。
        """

        self._remind_blocked[message.user_id] = message.message_id
        if len(self._remind_blocked) > MAX_REMIND_ASKS:
            self._remind_blocked.pop(next(iter(self._remind_blocked)), None)

    def _remember_remind_channel(self, person_id: str, channel: str) -> None:
        """记住**他明说过"以后都这么干"**的那种收法（只有那种才不再问）。

        主叫点只有一处：`_note_remind` 里 `forever_channel(...)` 认到的时候。**普通选一次
        不写这里**——用户 2026-10-09 的规矩是"每一次都问，除非说以后都这么干"。
        """

        self._remind_channel_pref[person_id] = channel
        if len(self._remind_channel_pref) > MAX_REMIND_ASKS:
            self._remind_channel_pref.pop(next(iter(self._remind_channel_pref)), None)

    def _remember_remind_seen(self, key: tuple[str, str], now: float) -> None:
        """记下"这件事刚认过"（`(人, 归一化的事情)` → 过期时刻），清过期的、加上限。

        窗口是 `remind.REMIND_DEDUPE_WINDOW_SECONDS`（**60 分钟**，2026-10-09 第二版：
        她刷屏那次是 5 分钟里发了 5 条，10 分钟不够盖住）。"改口换个时间"不再靠窗口兜——
        那种由"同一把 key 交过去 = 更新那一条"解决。
        """

        expired = [item for item, until in self._remind_seen.items() if until <= now]
        for item in expired:
            self._remind_seen.pop(item, None)
        self._remind_seen[key] = now + REMIND_DEDUPE_WINDOW_SECONDS
        while len(self._remind_seen) > MAX_REMIND_SEEN:
            self._remind_seen.pop(next(iter(self._remind_seen)), None)

    def _remember_remind_ask(self, request: RemindRequest) -> None:
        """记下待办（一个人一条；超上限淘汰最旧的一条，同 `_promise` 的写法）。

        **一个人同时只留一条**：值不齐（还没定怎么收 / 选了邮箱却还没邮箱）的那一条。
        值一齐就交给插件、从待办里删掉（见 `_try_hand_over_remind`）。
        """

        self._remind_asks[request.person_id] = request
        if len(self._remind_asks) > MAX_REMIND_ASKS:
            self._remind_asks.pop(next(iter(self._remind_asks)), None)
        # **他最近那一条**在这里也记一份：等收法的那条还没交给插件，"这次群里叫我"
        # 也得改得到它（2026-10-09 实测那条断链）。
        self._remind_last_request[request.person_id] = request
        if len(self._remind_last_request) > MAX_REMIND_ASKS:
            self._remind_last_request.pop(next(iter(self._remind_last_request)), None)
        if not request.channel:
            # 从这一刻起在"等怎么收"——超时兜底按这个时刻算（见 `_expire_remind_channel_wait`）。
            self._remind_ask_since.setdefault(request.person_id, self.wall_clock())

    def _try_hand_over_remind(self, request: RemindRequest, *, update: bool = False) -> None:
        """**值齐了才交给插件**；不齐就留在待办里，由 `_remind_ask_note` 让她自己问。

        2026-10-09 用户实测（富哥之家那一晚）：频道还没定就把请求交给了插件，插件只好
        自己发一条罐头问话（"1 在本群里 @ 你 2 私聊你 3 寄到你的邮箱…"），而她**自己**
        也在群里问同一件事——同一件事两个声音，群里就"乱的要死"。所以"要不要问"
        由核心决定、"怎么问"交给她；插件只在**一条装好的请求**到手时才办事，
        它于是**没有理由再问一遍**。
        """

        if not request.channel:
            logger.info("Stage 3 remind: 还没问清他想怎么收，先不交给插件 session=%s",
                        request.session_id)
            return
        if request.channel == "email" and not request.email:
            logger.info("Stage 3 remind: 他想收邮箱、可邮箱还没有，先不交给插件 session=%s",
                        request.session_id)
            return
        # **他最近的那一条**（"这次群里叫我"要改的就是它）：**改这一次**时也要刷新，
        # 所以它放在频次闸**之前**。
        self._remind_last_request[request.person_id] = request
        if len(self._remind_last_request) > MAX_REMIND_ASKS:
            self._remind_last_request.pop(next(iter(self._remind_last_request)), None)
        # **频次兜底**（2026-10-09 她刷屏那次加的第三道闸）：同一个人两次交给插件之间
        # 至少隔 `dev_config.remind_min_interval_seconds()`（默认 300 秒，**调用时现读**；
        # 0 = 关掉）。前面两道（归一化去重、她自己的发言不算）万有一漏，也不会刷屏。
        # `update=True`（"这次群里叫我"那种**改**）不走这道闸：那是同一条的改法，不是新的一条。
        now = self.wall_clock()
        last = self._remind_last_handover.get(request.person_id, 0.0)
        floor = dev_config.remind_min_interval_seconds()
        if not update and floor > 0 and now - last < floor:
            logger.info(
                "Stage 3 remind: 距上一条只过了 %s 秒（闸 %s 秒），这一条先不交 session=%s",
                int(now - last), floor, request.session_id)
            return
        self._remind_last_handover[request.person_id] = now
        if len(self._remind_last_handover) > MAX_REMIND_ASKS:
            self._remind_last_handover.pop(next(iter(self._remind_last_handover)), None)
        # 真正交出去了：记住**这一次用的收法**（等渠道超时时按它办），并清掉"在等多久"。
        self._remind_last_channel[request.person_id] = request.channel
        if len(self._remind_last_channel) > MAX_REMIND_ASKS:
            self._remind_last_channel.pop(next(iter(self._remind_last_channel)), None)
        self._remind_asks.pop(request.person_id, None)
        self._remind_ask_since.pop(request.person_id, None)
        self._hand_over_remind(request)

    def _hand_over_remind(self, request: RemindRequest) -> None:
        """把装好的那一条交给插件（**核心唯一碰插件的这一处**）。"""

        seam = self.remind
        if seam is None:
            logger.info("Stage 3 remind: 没有提醒插件（registry.remind 为空），只认不办 session=%s",
                        request.session_id)
            return
        try:
            seam(request)
        except Exception:  # noqa: BLE001 - 插件坏了也不能吃掉这一轮
            logger.exception("Stage 3 remind: 插件没收下这一条 session=%s", request.session_id)
            return
        logger.info("Stage 3 remind scheduled: what_len=%s session=%s",
                    len(request.what), request.session_id)

    def _take_remind_email(self, message: IncomingMessage) -> None:
        """他在聊天里把邮箱说给她 → **核心**记下来（按人）并补交那条待办。

        由来（2026-10-08 用户拍板）：**插件绝不碰记忆**。所以邮箱由核心收：
        先从这一句里认出邮箱（保守正则，认不出就什么都不做、继续等），存进核心自己
        那一份按人的值（`RemindEmailBook`，**落盘、重启后仍在**——用户要求
        "邮箱只第一次要，之后记住"；独立审查 2026-10-09 第 10 条），
        再**明确**把它交给**既有那条记忆捕获**（`memory_service.capture`，按人入队）——
        这一步是**故意的**：他这一句常常只是"我的邮箱是 x@y"，走别的路容易被当成
        "不值得记"或被延迟；而这是用户明确给的、流程要用的值。同一个 message_id 在库里
        按事件键去重，所以不会记两遍。

        **两处不是一回事，别混**：书（`data/remind_emails.json`）是核心执行这条流程
        要用的值，**落盘**；记忆捕获是把"他说过邮箱"这件事记进记忆库（按人），
        核心**不做**"以后从记忆库回查邮箱"那一步——从前注释里"核心查好"的说法不准，
        这里改实话。
        """

        pending = self._remind_asks.get(message.user_id)
        if pending is None:
            return
        email = find_email(message.text or "")
        if email is None:
            return
        self._remind_emails.remember(message.user_id, email)
        service = self.memory_service
        if service is not None:
            try:
                service.capture(message)
            except Exception:  # noqa: BLE001 - 记不进去也不能吃掉这一轮
                logger.exception("Stage 3 remind: 邮箱进记忆失败 session=%s", message.session_id)
        updated = replace(pending, email=email)
        self._remember_remind_ask(updated)
        self._try_hand_over_remind(updated)

    def _remind_ask_note(self, message: IncomingMessage) -> str:
        """给这一轮易变段加**一句**核心写的话（没有就空串）；一轮最多一句。

        走的是既有的 `clarify_note` 口子（"一句话进易变段"）——**不是**插件提示词，
        也**不是**"核心直接替她发言"：2026-10-09 用户实测（富哥之家）那条"乱"正是
        她自己在群里问的同时、插件那条罐头问话也发进了群。这里的做法是**把要说的
        交给她，让她用自己的话说**；群里看到的只有她的话。

        优先级（一轮只说一句）：

        1. **这一轮的提醒被闸拦下了** → 让她问一句几点，**别自己先许一个时间**
           （2026-10-09 实测：闸拦对了，可没告诉她，于是她照旧答应"一点四十叫你"，
           而 13:40 什么都不会发生——比刷屏更该修）；
        2. 还没问清怎么收 → 三个选项让她问（**每次都问**、**每一轮都出**，直到问出来；
           除非他早说过"以后都这么干"）——措辞要"必须问"一点：2026-10-09 实测里
           她直接跳过这句、自己默认了群里，于是那条**永远不响**（"还是没叫我"）；
        3. 选了邮箱还没有邮箱 → 问邮箱（地址是**一次性**给的，不受"每次都问"那条影响）；
        4. 他刚说**这次**改收法 → 让她应一句"**这次**在群里 @ 他"这种（与"以后都"分开）；
        5. 他刚说**以后都这么干** → 让她应一句"**以后都这么办**"，把话说明白；
        6. 收法是按他那条"以后都"的约定用的 → 让她**复述**一句（"还照老规矩"），别再问。
        """

        blocked = self._remind_blocked.pop(message.user_id, None)
        if blocked == message.message_id:
            return ("（他这句没说清什么时候——这轮顺口问一句几点，"
                    "别自己先许一个时间。）\n")
        this_time = self._remind_this_time.pop(message.user_id, None)
        forever = self._remind_forever.pop(message.user_id, None)
        restate = self._remind_restate.pop(message.user_id, None)
        pending = self._remind_asks.get(message.user_id)
        if pending is not None and not pending.channel:
            # **必须问出来**：这句每一轮都会出现，直到他说清怎么收——别跳过去自己定
            # （2026-10-09 实测：她一跳过，那条提醒就永远不响）。
            return ("（他要你到点提醒他一件事，**怎么叫他还没问清**——这一轮必须问出来："
                    "在群里 @ 他、私聊他、还是寄到他邮箱？问清之前先别答应时间、"
                    "也别自己挑一种。）\n")
        if pending is not None and pending.channel == "email" and not pending.email:
            return "（他刚让你到点提醒他一件事，可你手上还没有他的邮箱——这轮顺口问一句就行。）\n"
        if this_time is not None and this_time[0] == message.message_id:
            where = REMIND_CHANNEL_PHRASES.get(this_time[1], "")
            if where:
                return (f"（他说这次{where}——顺口应一句「行，这次就这么办」就行，"
                        f"只有这一次。）\n")
        if forever is not None and forever[0] == message.message_id:
            where = REMIND_CHANNEL_PHRASES.get(forever[1], "")
            if where:
                return (f"（他刚说以后都{where}——这轮顺口应一句「好，以后都这么办」，"
                        f"把话说明白。）\n")
        if restate is not None and restate[0] == message.message_id:
            where = REMIND_CHANNEL_PHRASES.get(restate[1], "")
            if where:
                return (f"（他早说过以后都{where}——顺口应一句「还是照老规矩」就行，"
                        f"别再问一遍。）\n")
        return ""

    def _apply_guard_signal(self, verdict: JudgeVerdict, message: IncomingMessage) -> bool:
        """把判定的结论落到这一个回合上——**在拼这一轮回复之前**。返回要不要压掉回复。

        **它只升不降**：`may_lower_guard` 不传。降交给维护 agent（7 天最多一档），
        免得"他道了个歉"就立刻回暖。

        ## 三种取值，各自的待遇（2026-10-09 用户改的设计）

        * `<guard>PROBE</guard>`（有人当面打探她是怎么做出来的）：**这一轮不接**，
          返回 `True`，调用方直接结束这一轮；**关系轴一个字都不写**——
          "探机制"只是当轮不接，说完就过去，不该留成她对这个人的长久态度
          （用户原话：*"聊机制她不回不就好了"*）。
        * `<guard>UP</guard>` + `guard_reason` 在 `GUARD_RAISING_OFFENSES` 里
          （冒犯性言论 / 阴阳怪气）：写防备 +1——**这就是"被冒犯立刻变冷"的全部实现**：
          同一条回复就已经按新的分寸在说了（缓存失效在这一处）。
        * `<guard>UP</guard>` 而由头不在表里（判定把玩笑读成了越界）、或者干脆没给由头：
          **不写**，只加一个计数。**政策留在核心**，提示词漂了也拦得住。

        两者互斥：`PROBE` 先返回，所以探机制既不会压回复又顺手写防备这种事不会发生。
        """

        user_id = message.user_id
        store = getattr(self.memory_service, "store", None)
        if verdict.guard_probe:
            # **当轮不接**：不写关系、不进回复那一段。缓存不用动（没有任何值变了）。
            self._stats["guard_probe_held"] += 1
            logger.info(
                "Stage 3 guard probe: 这一轮不接（当轮不接，不写关系轴）user=%s", user_id or "-"
            )
            return True
        if not verdict.guard_up:
            return False
        if not user_id or store is None:
            return False
        offense = (verdict.guard_reason or "").strip().casefold()
        if offense not in GUARD_RAISING_OFFENSES:
            # **不是表里那几种由头 → 一个字都不写。** 这里从前是直接 +1 的，
            # 于是"判定把一句玩笑/一句探机制读成越界"会当场变成那个人一周的冷遇。
            self._stats["guard_rejected"] += 1
            logger.info(
                "Stage 3 guard signal 没有可用由头，防备不动: offense=%s user=%s",
                offense or GUARD_RAISE_WITHOUT_OFFENSE, user_id,
            )
            return False
        try:
            rejection = store.apply_affinity(
                # 依据写**固定的那句中文**（表在 `dialogue_judge.OFFENSE_CODES`），
                # 不把判定可能写歪的自由文本原样落库。
                user_id, guardedness=1, reason=OFFENSE_CODES.get(offense, offense),
                source="judge",
            )
        except Exception:  # noqa: BLE001 - 写不进去也不能打断这一轮回复
            logger.exception("Stage 3 guard write failed")
            return False
        self._stance_cache.pop(user_id, None)
        if rejection:
            self._stats["guard_rejected"] += 1
            logger.info("Stage 3 guard signal rejected: reason=%s user=%s", rejection, user_id)
        else:
            self._stats["guard_raised"] += 1
            logger.info("Stage 3 guard raised: user=%s", user_id)
        return False

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

    def _drop_session_clients(self, session_id: str) -> None:
        """会话被淘汰时，把它那两个按会话 client 从字典里摘掉。

        它们是**强引用**（`_client_for` / `_judge_for` 只写不删），会话状态都回收了
        还留着它们，等于"回收"只回收了一半——每个 client 还各持一份连接与缓存。
        下次这个会话再来消息会重新建一个（缓存空间重来一次，那是可接受的代价）。
        """

        self._session_clients.pop(session_id, None)
        self._session_judges.pop(session_id, None)
        # 判定采样的按会话计数同批清掉（LRU 淘汰时 `leave_session` 不会走这条路）。
        self.judge_sampling.forget_session(session_id)

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
        # `at` 是**给人看的时刻**（`/super apicheck` 用 `_format_clock` 印它），
        # 所以走注入的墙钟，不另取一次 `time.time()`。
        self.last_cache_report = {"reason": reason, "at": self.wall_clock(), **report}
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

    def note_turn_timeout(self) -> int:
        """一轮对话超过了总预算（`runtime.handle_turn_within_budget` 报进来）。

        由来（2026-10-08 失败降级审计 2）：主循环串行，一条卡住的腿会挡住后面所有消息；
        每一腿的网都只是**停顿网**，相加可以远超这个预算——这是最后一道网。
        （2026-10-13 更正：原来写"单次模型调用最坏 ≈ 181 秒（60×3 + 退避）、一轮最多
        三次"——随"不重试 + 超时只管首字"作废。）
        超时的处理与"模型抛异常"**同一个结果**：这一条没有回复（`handle` 返回 None）。

        **不删那条消息的历史**：它早就记进去了（`handle` 里 `state.add` 在模型调用
        之前），她该知道群里发生过那句话。只有计数与（runtime 那边的）一行 ERROR。
        """

        self._stats["turn_timeouts"] += 1
        return self._stats["turn_timeouts"]

    def snapshot(self) -> EngineSnapshot:
        """只读运行快照。内存数字每次读取时现取（取不到就是 0）。"""

        memory_now, memory_peak = process_memory_bytes()
        # 审核的计数在**它自己**身上（判不过/认不出都发生在它内部，引擎看不到中间那一步），
        # 所以快照现取一份，让 `/super status` 与面板能看到拦截率（2026-10-05 追加）。
        review = self.style_reviewer.snapshot() if self.style_reviewer is not None else {}
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
            started_monotonic=self.started_monotonic,
            sessions_evicted=self.sessions.evicted,
            memory_bytes=memory_now,
            memory_peak_bytes=memory_peak,
            persistence_enabled=self.state_store is not None,
            focus=self.focus_stats(),
            judge_sampling=self.judge_sampling.stats(self.clock()),
            review_calls=int(review.get("calls", 0)),
            review_blocked=int(review.get("blocked", 0)),
            review_unrecognized=int(review.get("unrecognized", 0)),
            review_failed=int(review.get("failed", 0)),
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
        if action in {SuperAction.MEMORY_ON, SuperAction.MEMORY_OFF}:
            return self._memory_group_switch(message, enabled=action is SuperAction.MEMORY_ON)
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
        if action is SuperAction.QUOTE:
            return self._quote_reply(message)
        if action is SuperAction.SLANG:
            return self._slang_reply(message)
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
        return self._super_memory(action, message)

    def _memory_group_switch(self, message: IncomingMessage, *, enabled: bool) -> str:
        """`/super enable|disable memory`：**在哪个群说，就动哪个群的记忆**。

        与 `/admin enable|disable` 完全对称——连"私聊里说"和"手滑带了群号"的处理都
        照抄那一条（见 `_admin_command_reply`）：记忆名单与对话名单都是**按群**的，
        两张名单互不相干。用户 2026-10-08 的两次纠正合起来就是这个形状：
        *"我要的是**记忆**不是对话"*、*"我要的是**在哪个群说 enable/disable 就开启/
        关闭哪个群的**"*。

        关掉**不删任何东西**：已有的待处理行留在库里，重新打开就接着处理。
        """

        group_id = message.target.group_id or ""
        if not group_id:
            # 照 `/admin` 私聊那一句的原话：这条命令管的就是你发命令的那个群。
            return "这条命令要在群里发：它管的就是你发命令的那个群。"
        typed = SUPER_MEMORY_SWITCH_PATTERN.fullmatch((message.text or "").strip())
        extra = (typed.group("group") or "") if typed else ""
        if extra.isdigit():
            # 与 `/admin enable 群号` 逐字同一句（那边也是"参数已去掉"的处理）。
            return ("群号参数已经去掉了：这条命令只管你发命令的这个群"
                    f"（现在就是 {group_id}）。")
        if extra:
            return f"这条命令不带参数：它只管你发命令的这个群（现在就是 {group_id}）。"
        if enabled:
            changed = self.enable_group_memory(group_id)
            text = (f"本群（{group_id}）已开启记忆：新消息会进记忆收件箱，维护照常处理。"
                    if changed else f"本群（{group_id}）本来就开着记忆。")
        else:
            changed = self.disable_group_memory(group_id)
            text = (f"本群（{group_id}）已关闭记忆：新消息不再进收件箱；"
                    "已有的待处理材料一条都不删，重新打开就接着处理。"
                    if changed else f"本群（{group_id}）当前未开启记忆。")
        self._log_memory_switch(group_id, enabled)
        return text

    def _log_memory_switch(self, group_id: str, enabled: bool, *, source: str = "super") -> None:
        """开关动作进 `control_audit`（与面板改配置同一条只增流水）。

        这条接缝可能没有（测试、裁掉审计能力的部署）——那是 `NoAudit` 或 `None`，
        两种情况都不该影响命令本身，所以整段兜住。

        `source` 区分**是谁按的**：命令那条是 `"super"`（缺省），面板那条传 `"panel"`
        （见 `runtime._SeamBinder.ui_set_group_memory`）。动作名两边**相同**，
        这样"这个群的记忆开关被谁动过"一次筛选就看得全。
        """

        audit = getattr(self, "control_audit", None)
        if audit is None:
            return
        try:
            audit.record("memory_group_switch",
                         detail={"group_id": group_id,
                                 "memory": "1" if enabled else "0"},
                         result="on" if enabled else "off", source=source)
        except Exception:  # noqa: BLE001 - 记账失败不该让命令看起来没生效
            logger.warning("memory_switch_audit_failed group=%s", group_id, exc_info=True)

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

        # **认不出的档位一律拒绝**（2026-10-08 安全面审计 A-03）：第一道网是装载点
        # （`CommandRegistry.add` 拒绝装载"声明了却不认识"的插件），这是第二道——
        # 万一有插件绕过装载点进了链（或档位是运行期被改的），也绝不放行：
        # 不猜、不降级成 public、不把这条交给模型。
        from .command_plugins import LEVELS

        if level not in LEVELS:
            logger.warning(
                "plugin level not allowed: level=%r user=%s group=%s",
                level, message.user_id, message.target.group_id or "",
            )
            return False
        if level == "super":
            return self._is_super_admin_control(message)
        if level == "admin":
            return self._is_admin_control(message) or self._is_super_admin_control(message)
        return True

    async def _plugin_action_reply(self, request, message: IncomingMessage):
        """把插件声明的动作意图真正执行掉（`ActionRequest` 的唯一执行点）。

        插件负责"认出命令、解析出目标与参数"，这里负责"能不能做、怎么做、记什么账"：
        权限档位在 `_plugin_level_allowed` 判过了，动作白名单在 `capabilities` 那道闸里，
        目标与参数护栏在插件那一侧的 `group_admin.py` / `group_owner.py` 里（执行函数由
        插件经 `registry.provide_group_action()` 放上来），全是 WARNING 审计。
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
        # 群管理与群主动作的**执行端在插件里**（`plugins/group_admin/`）：它们只
        # 解析与声明意图，护栏、权限、调用仍在这里。插件在 `register()` 里用
        # `registry.provide_group_action("<分组名>", execute)` 把执行函数放上来，
        # 这里只按 `request.group` 取——**核心不认识那个插件的模块名**。
        #
        # 2026-10-06 改（外部审查实测的那处耦合）：原来是两句
        # `from .plugins.group_admin.group_admin import execute` /
        # `…group_owner import execute`，被 `except ModuleNotFoundError` 兜住。
        # 于是把插件文件夹改个名字 = `discover()` 一切正常、命令**静默**回
        # "这条部署没有群管理能力"——核心还拴在具体插件上。现在换成
        # `getattr(self.plugin_registry, "group_action", None)`，与
        # `runtime.build_engine` 取识图工厂（`getattr(registry, "vision", None)`）同一形状。
        #
        # 取不到时**保持 fail-closed**：不执行、记一行日志、回那句话（那句话本身是对的）。
        #
        # 两个来源（与 `_group_action_caller` 同一套写法）：
        # 1. `engine.group_actions`——生产走这条（`runtime._build_group_actions` 已经把
        #    工厂调好、账本递进去了）；
        # 2. **没有它时现取注册表里的工厂**（只造一台 `DialogueEngine` 的测试是这种：
        #    它们不走 `build_engine`，但会往注册表里登记一个假工厂看核心有没有走这条路）。
        runner = getattr(self, "group_actions", {}).get(request.group) \
            if isinstance(getattr(self, "group_actions", None), dict) else None
        if not callable(runner):
            registry = getattr(self, "plugin_registry", None)
            taker = getattr(registry, "group_action", None)
            factory = taker(request.group) if callable(taker) else None
            if callable(factory):
                try:
                    runner = factory(getattr(self, "usage_store", None))
                except TypeError:
                    runner = factory()
        if request.group in {"group_admin", "group_owner"}:
            if not callable(runner):
                logger.warning("group_action_unavailable group=%s kind=%s",
                               request.group, request.kind)
                return "这条部署没有群管理能力。"
            if request.group == "group_admin":
                return await runner(
                    request.kind,
                    # **一个已过闸门的调用函数**，不是活的 transport
                    # （2026-10-01 适配：原来这里传 `transport=self.transport`，
                    # 也就是插件里的函数拿到了传输层，而本函数的 docstring
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
            return await runner(
                request.kind,
                call=self._group_action_caller("group_owner"),
                roles=getattr(self, "group_roles", None),
                group_id=group_id,
                actor_id=actor_id,
                target_id=request.target_id,
                text=request.text,
                mentioned=mentioned or request.mentioned,
                enabled=dev_config.GROUP_OWNER_ENABLED,
            )
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

    def _quote_reply(self, message: IncomingMessage) -> str:
        """`/super quote`：看金句学习学出来的**表情含义表 + 她的老习惯**，并**改一个字**。

        由来（2026-10-06 用户）：*"注意贴表情不是所有都是金句，比如贴祝（猪的谐音）就是
        不赞同或者 bot 回复不恰当"*、*"要能被操作者看到与纠正…不许它自己闷头学歪"*。
        所以这一条命令有两个动作，缺一不可：

        - **只读**：把当前那个群的表情含义表（含"哪几条是人工改的"）和她的老习惯原文
          发出来。**改一个字就生效**——覆盖写在 `data/quote/profile.json` 里，
          回复路径每次按 mtime 重新读（下一轮回复就变）。
        - **纠正**：`/super quote 128516 不赞成 在损她` —— 记下"这个表情在这个群里是
          不赞成"并（可选）附一句说明；`/super quote 128516 auto` 撤掉人工那份，
          退回模型判的。

        **只读的是金句学习那份文件**，一个字都不碰记忆库、不碰人格。
        """

        agent = getattr(self, "quote_agent", None)
        store = getattr(agent, "store", None) if agent is not None else getattr(self, "quote_profile", None)
        store = getattr(store, "store", store)
        if store is None or not hasattr(store, "effective_sense"):
            return "当前没有接入金句学习（没配 key，或 `QQBOT_QUOTE_LEARN=0` 关掉了）。"
        store.reload(force=True)
        # 方向的中文说法与 `quote_learning` 共用一份（两处漂移就会出现"命令里写不赞成、
        # 存下来是别的意思"）。**本地 import**：那个模块是可拔的，核心不该在顶部认识它。
        from .quote_learning import SENSE_LABELS, note_id

        group_id = message.target.group_id or (message.session_id.split(":", 1)[-1])
        changed = ""
        # **先判"停用一条笔记"**：它和"改一个表情的方向"共用 `/super quote` 这个前缀，
        # 判反了就会往覆盖表里塞一个叫 note 的假表情（见 `_parse_quote_override`）。
        toggle = _parse_quote_note_toggle(message.text or "")
        taken = None if toggle is not None else _parse_quote_override(message.text or "")
        if toggle is not None:
            note_key, enabled = toggle
            known = {note_id(item.get("when"), item.get("note")) for item in store.notes}
            if note_key not in known:
                changed = (f"没有找到这条笔记（id {note_key}）——可能是模型又重写了笔记，"
                           f"发 `/super quote` 看现在这一份的 id。")
            else:
                store.set_note_enabled(note_key, enabled)
                changed = (f"已{'恢复' if enabled else '停用'}那条笔记（{note_key}）。"
                           + ("" if enabled else "下一句回复就不再带它了。"))
        elif taken is not None:
            emoji_id, sense, note = taken
            if sense == "auto":
                store.clear_override(group_id, emoji_id)
                changed = f"已撤掉 {emoji_id} 的人工方向，改回它自己判的那一版。"
            else:
                normalized = store.override_sense(group_id, emoji_id, sense, note)
                changed = f"已记下：本群的 {emoji_id} 是「{SENSE_LABELS.get(normalized, normalized)}」" \
                          + (f"（说明：{note}）" if note else "") + "。下一句回复就按这个来。"

        lines: list[str] = []
        if changed:
            lines.append(changed)
        if agent is not None:
            stats = agent.snapshot()
            lines.append(
                f"金句学习：学过 {stats.get('runs', 0)} 轮   样本 {stats.get('samples', 0)} 条   "
                f"没跑成 {stats.get('failures', 0)} 次"
            )
            if stats.get("last_run_at"):
                lines.append(
                    f"上一次：{_format_clock(float(stats['last_run_at']))}   "
                    f"{stats.get('last_result', '')}"
                )
        faces = face_names.shared().snapshot()
        if faces["count"]:
            lines.append(f"表情名字表：{faces['count']} 条（{faces['path']}）")
        else:
            lines.append("表情名字表：还没有（下面只显示数字）")
        table = store.meanings.get(group_id) or {}
        if not table:
            lines.append(f"本群（{group_id}）还没有表情含义（样本不够，或者她的消息还没被贴过表情）。")
        else:
            lines.append(f"本群（{group_id}）的表情含义：")
            for emoji in sorted(table, key=lambda key: (len(key), key)):
                sense = store.effective_sense(group_id, emoji)
                detail = store.effective_note(group_id, emoji)
                # 方向是谁定的要看得出来：人工改的、用户点名写死的（`㊗`）、还是要它自己学的。
                source = store.sense_source(group_id, emoji) if hasattr(store, "sense_source") else ""
                mark = {"人工": "（人工改的）", "写死": "（写死的）"}.get(source, "")
                name = face_names.shared().label_for(emoji)
                shown = f"{emoji}（{name}）" if name else emoji
                lines.append(
                    f"· {shown} → {SENSE_LABELS.get(sense, sense)}{mark}"
                    + (f"：{detail}" if detail else "")
                )
        if store.notes:
            lines.append("她自己的老习惯（学来的，都是短句）：")
            for item in store.notes[:6]:
                key = note_id(item.get("when"), item.get("note"))
                marks = []
                if item.get("avoid"):
                    marks.append("反面：那次被嫌")
                if store.note_disabled(key):
                    marks.append("已停用")
                mark = "（" + "、".join(marks) + "）" if marks else ""
                lines.append(f"· [{key}] {item.get('when', '')}——{item.get('note', '')}{mark}")
            hidden = len(store.notes) - 6
            if hidden > 0:
                lines.append(f"… 还有 {hidden} 条")
        lines.append("改：/super quote <表情id> <赞成|不赞成|其它|不确定> [说明]")
        lines.append("撤：/super quote <表情id> auto")
        lines.append("停用/恢复一条笔记：/super quote note <笔记id> off|on")
        lines.append(f"（文件：{store.path}）")
        return "\n".join(lines)

    def _slang_reply(self, message: IncomingMessage) -> str:
        """`/super slang`：看她**被动听来的黑话词条**，并**改 / 删 / 标错**。

        由来（2026-10-06 用户口径）：*"金句我看应该划到**知识库**里"*、*"还有**黑话**"*。
        形状照 `/super quote`（列 + 改 + 删），因为它解决的是同一类问题：
        "要能被操作者看到与纠正，不许它自己闷头学歪"。

        词条是**群里真人解释的原话**（谁、什么时候、哪个群、原句都在），
        不是模型编的释义——所以这里能做的只有四件事：看、改一个字、删、标错。
        **这一条命令不进模型、不碰记忆库、也不改人格**；它读写的只有
        `data/slang/entries.json` 那一份（`data/` 已 gitignored）。
        """

        store = getattr(self, "slang_library", None)
        if store is None or not hasattr(store, "entries_for"):
            return "当前没有接入黑话词条（这一版没有它也能照常说话）。"
        store.reload(force=True)
        group_id = message.target.group_id or (message.session_id.split(":", 1)[-1])
        verb, word, definition = _parse_slang_edit(message.text or "")
        changed = ""
        if verb == "set" and word and definition:
            if store.update_definition(group_id, word, definition, by=f"超管 {message.user_id}"):
                changed = f"已改：本群的「{word}」= {definition}（旧的那条记进修订了）。"
            else:
                changed = (f"这一条没法记（词或释义不合格，或者含不该出现的词）："
                           f"{word} / {definition}")
        elif verb == "del" and word:
            changed = (f"已删掉本群的「{word}」。" if store.delete(group_id, word)
                       else f"本群没有「{word}」这条词条。")
        elif verb in {"wrong", "ok"} and word:
            wrong = verb == "wrong"
            if store.mark_wrong(group_id, word, wrong):
                changed = f"已{'标错' if wrong else '取消标错'}：{word}"
            else:
                changed = f"本群没有「{word}」这条词条。"
        elif verb == "why" and word:
            return self._slang_detail(store, group_id, word)

        lines: list[str] = []
        if changed:
            lines.append(changed)
        watcher = getattr(self, "slang_watcher", None)
        if watcher is not None:
            stats = watcher.snapshot()
            line = (f"黑话：听过 {stats.get('captured', 0)} 条   词条 {stats.get('entries', 0)} 条"
                    f"   群 {len(stats.get('groups') or [])} 个")
            if stats.get("failures"):
                line += f"   没记成 {stats.get('failures')} 次"
            lines.append(line)
        scope = None if verb == "all" else group_id
        rows = store.entries_for(scope)
        if not rows:
            lines.append(f"还没有词条（{'所有群' if scope is None else f'本群 {group_id}'}）："
                         f"群里有人解释一个词，她就记下来——不用她发问。")
        else:
            lines.append("词条：" + ("（所有群）" if scope is None else f"（本群 {group_id}）"))
            for item in rows[:SLANG_LIST_MAX_ROWS]:
                lines.append(self._slang_line(item, scope is None))
            hidden = len(rows) - SLANG_LIST_MAX_ROWS
            if hidden > 0:
                lines.append(f"… 还有 {hidden} 条")
        lines.append("看证据：/super slang why <词>")
        lines.append("改释义：/super slang set <词> <新释义>（也可以直接 /super slang <词> <新释义>）")
        lines.append("删：/super slang del <词>    标错/取消：/super slang wrong|ok <词>")
        lines.append("看所有群：/super slang all")
        lines.append(f"（文件：{store.path}）")
        return "\n".join(lines)

    def _slang_line(self, item: dict, with_group: bool) -> str:
        """一条词条的一行（列表用）。**带出处**：谁说的、什么时候。"""

        where = f"[{item.get('group_id', '')}] " if with_group else ""
        mark = "（标错）" if item.get("wrong") else ""
        times = int(item.get("times") or 0)
        explainers = list(item.get("explainers") or [])
        who = ""
        if explainers:
            last = explainers[-1]
            when = _format_clock(float(last.get("at") or 0.0))
            if last.get("source") == "edited":
                who = f"——操作者改于 {when}"
            else:
                name = str(last.get("name") or last.get("user_id") or "")
                who = f"——{name} 说于 {when}"
        return (f"· {where}{item.get('word', '')} = {item.get('definition', '')}{mark}"
                f"（听过 {times} 次{who}）")

    def _slang_detail(self, store, group_id: str, word: str) -> str:
        """`/super slang why <词>`：把这条词条的**原话证据**摊开给人看。"""

        item = store.find(group_id, word)
        if item is None:
            return f"本群（{group_id}）没有「{word}」这条词条。"
        lines = [f"【{item.get('word', '')}】= {item.get('definition', '')}"
                 + ("（已标错）" if item.get("wrong") else "")]
        lines.append(f"听过 {item.get('times', 0)} 次；"
                     f"第一次 {_format_clock(float(item.get('first_seen') or 0.0))}，"
                     f"最近 {_format_clock(float(item.get('last_seen') or 0.0))}")
        explainers = list(item.get("explainers") or [])
        if explainers:
            lines.append("谁解释的（原话）：")
            for row in explainers[-4:]:
                name = str(row.get("name") or row.get("user_id") or "")
                quote = str(row.get("quote") or "")
                dropped = "（原话含不该留的词，没存下来）" if row.get("evidence_dropped") else ""
                act = "操作者改的" if row.get("source") == "edited" else f"{name} 说的"
                lines.append(f"· {act}"
                             f"（{_format_clock(float(row.get('at') or 0.0))}）："
                             f"{quote or '—'}{dropped}")
        revisions = list(item.get("revisions") or [])
        if revisions:
            lines.append("改过口：")
            for row in revisions[-4:]:
                lines.append(f"· {_format_clock(float(row.get('at') or 0.0))} "
                             f"{row.get('by', '')}：{row.get('from', '')} → {row.get('to', '')}")
        lines.append(f"（文件：{store.path}）")
        return "\n".join(lines)

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
                # 同一个显式偏移（不读宿主时区），与 `_format_clock` 同源。
                stamp = datetime.fromtimestamp(updated, tz=dev_config.local_timezone())
                lines.append(f"（更新于 {stamp:%m-%d %H:%M}）")
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

        2026-10-13 加一行**模型来源**（`provider_failover.status_line()`）：它印的是
        "当值的是主家还是兜底那一家"，不是余额数字——余额仍然只在 `/super apicheck`。
        """

        snapshot = self.snapshot()
        metrics = snapshot.metrics or {}
        latency = (metrics.get("model_latency") or {}) if isinstance(metrics, dict) else {}
        # **"已运行多久"用单调钟算**（2026-10-08 安全与时间口径审计 B-02）：
        # 从前是 `time.time() - snapshot.started_at`，一次对时就能让它跳变，
        # 往回拨还会被 `max(0.0, …)` 夹成"已运行 0 秒"。`started_at` 只用来印
        # "开始于"那个时刻。
        uptime = (max(0.0, self.clock() - snapshot.started_monotonic)
                  if getattr(snapshot, "started_monotonic", 0.0) else 0.0)
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
            # 模型来源那一行：主家余额耗尽（HTTP 402）时会自动切到兜底那一家，
            # 这件事必须一眼看得见——否则"她怎么突然换了个口气"没有解释。
            provider_failover.status_line(),
            f"内存：{_format_bytes(snapshot.memory_bytes)}"
            + (f"（峰值 {_format_bytes(snapshot.memory_peak_bytes)}）" if snapshot.memory_peak_bytes else "")
            + (f"   数据目录：{_format_bytes(database)}" if database else ""),
            f"启用群：{'、'.join(snapshot.enabled_group_ids) or '无'}",
            f"短期会话：{len(snapshot.sessions)}"
            # 会话数上限淘汰掉的（2026-10-08 隔离增长审计 [1]）：内存里的会话
            # 为什么少了，只有这个数能回答——不然读起来像"她忘了之前的对话"。
            + (f"   已淘汰：{snapshot.sessions_evicted}" if snapshot.sessions_evicted else "")
            + f"   已补历史：{snapshot.history_seeded}",
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
        if focus.get("queued_total") or focus.get("queued_dropped"):
            lines.append(
                f"排队等她：{focus.get('queued_total', 0)} 条"
                f"（{len(focus.get('queued_sessions') or [])} 个会话）"
                # 队列满时丢的是**最旧**的（单群上限见 `FOCUS_QUEUE_LIMIT`）。
                + (f"   已丢最旧 {focus['queued_dropped']} 条" if focus.get("queued_dropped") else "")
            )
        # 判定采样（2026-10-10）：消息来得太快时攒几条再判。有采样活动的会话各印
        # 一行（档位 / 攒批 / 本群攒了几条 / 跳过几次），最后是累计跳过——
        # "累计 0"本身就是信息：那说明这套闸还没拦过任何判定调用。
        sampling = snapshot.judge_sampling or {}
        for session_id, info in sorted((sampling.get("sessions") or {}).items()):
            lines.append(
                f"判定采样：{session_id} 当前档 {info['tier']}"
                f"（攒满 {info['batch']} 条判一次）"
                f" / 本群攒了 {info['pending']} 条 / 跳过 {info['skipped']} 次"
            )
        lines.append(f"判定采样：累计跳过判定 {sampling.get('skipped_total', 0)} 次")
        if snapshot.guard_raised:
            lines.append(
                f"关系：本次升防 {snapshot.guard_raised} 次"
                + (f"（被上限拒 {snapshot.guard_rejected} 次）" if snapshot.guard_rejected else "")
            )
        if snapshot.empty_forced_reply:
            lines.append(f"异常：判定说要回但回复空手 {snapshot.empty_forced_reply} 次")
        # 出站正文里剥掉的孤立标签片段（2026-10-09）：她在群里看不见它们了，
        # 但"模型又开始吐半截协议标签"这件事必须看得见——长期非零说明模型侧在退化。
        if snapshot.reply_tags_scrubbed:
            lines.append(
                f"出站净化：剥掉孤立标签片段 {snapshot.reply_tags_scrubbed} 个"
                "（正文已经发出去了，只是没有那几个尾巴）"
            )
        # 一轮超过总预算（2026-10-08 失败降级审计 2）：主循环串行，这条就是
        # "为什么刚才整个 bot 都不理人"的出口——一条消息卡住最长多久有上界了。
        # 历史照留，所以"她不知道发生过"不成立；这里说明的是"这一条没回复"。
        if snapshot.turn_timeouts:
            lines.append(
                f"异常：一轮对话超过总预算 {snapshot.turn_timeouts} 次"
                f"（这一条没回复，消息仍在她的历史里）"
            )
        # 判定/回复的模型调用失败（2026-10-08，失败降级审计 1）：失败时她照样不出声，
        # 所以这一行就是"她突然不说话了"的唯一出口。连续几次才算异常——单次可能只是
        # 抖一下；连续说明模型那边真的挂了。
        if snapshot.judge_failed or snapshot.reply_failed:
            failed_parts = []
            if snapshot.judge_failed:
                failed_parts.append(f"判定挂掉 {snapshot.judge_failed} 次")
            if snapshot.reply_failed:
                failed_parts.append(f"回复挂掉 {snapshot.reply_failed} 次")
            streak = max(self._judge_failed_streak, self._reply_failed_streak)
            head = ("异常：模型调用连续失败" if streak >= CONSECUTIVE_MODEL_FAILURE_ALERT
                    else "模型调用失败")
            lines.append(
                f"{head}：" + "   ".join(failed_parts)
                + (f"（当前连续 {streak} 次）" if streak else "")
            )
        if getattr(self, "style_reviewer", None) is not None:
            # 审核的**拦截率**（2026-10-05 追加）：长期 0 拦截 = 它没在工作；
            # 拦截率很高 = 可能过紧。这一行照配置出现，所以"0 次"本身也是信息。
            rate = (snapshot.review_blocked / snapshot.review_calls
                    if snapshot.review_calls else 0.0)
            lines.append(
                f"风格审核：判断 {snapshot.review_calls} 次   打回 {snapshot.review_blocked} 次"
                f"（{rate:.0%}）"
                + (f"   读不出判定 {snapshot.review_unrecognized} 次"
                   if snapshot.review_unrecognized else "")
                + (f"   它自己出错 {snapshot.review_failed} 次" if snapshot.review_failed else "")
            )
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
            line = f"记忆：已入队 {memory.get('queued_events', 0)} 条待处理"
            # 读/写失败数（2026-10-08，失败降级审计 7）：与金句"没跑成 N 次"同一口径。
            # 记忆库读不到时她不提以前的事、"记忆：已入队 0 条"看着一切正常——查不到。
            if memory.get("failures"):
                line += f"   读写失败 {memory.get('failures')} 次"
            # 保留期清理掉的条数（2026-10-08 隔离增长审计 [8]）：关系流水按时间清旧行，
            # 清了多少要看得见——不然"关系曲线怎么少了一段"查不出来。
            pruned = memory.get("pruned") or {}
            if pruned.get("relationship_log") or pruned.get("tombstones"):
                line += (f"   已清过期关系流水 {pruned.get('relationship_log', 0)} 条"
                         f" / 墓碑 {pruned.get('tombstones', 0)} 条")
            # 库内体检（2026-10-09 用户实测："同一个键重复多份""只有键、内容栏空"）：
            # 三类历史脏数据的条数**照配置永远印出来**（0 也是信息——0 表示库里干净）。
            # 定义见 `MemoryStore._hygiene_counts`；历史行一条不删，清理要用户点头。
            hygiene = memory.get("hygiene") or {}
            if hygiene:
                line += (f"   库里：同键多行 {hygiene.get('duplicate_key_groups', 0)} 组"
                         f"（多 {hygiene.get('duplicate_key_rows', 0)} 行）"
                         f"   正文空行 {hygiene.get('empty_content_records', 0)} 条"
                         f"   无证据 {hygiene.get('evidence_free_records', 0)} 条")
            # 被依据闸门拒掉的写入（2026-10-09）：非零才印，省得常态刷屏。
            rejected = memory.get("rejected") or {}
            if rejected:
                detail = " / ".join(f"{code.replace('rejected_', '')} {count}"
                                    for code, count in sorted(rejected.items()))
                line += f"   本次拒写 {sum(rejected.values())} 条（{detail}）"
            lines.append(line)
        # 金句学习（2026-10-06）：**把学出来的东西露在概览里**——用户要的就是
        # "要能被操作者看到与纠正"。原文与逐条方向在 `/super quote`，这里只报计数
        # 与"有没有在跑"，免得概览被一大段笔记撑爆。
        quote = getattr(self, "quote_agent", None)
        if quote is not None:
            stats = quote.snapshot()
            line = (f"金句学习：学过 {stats.get('runs', 0)} 轮   样本 {stats.get('samples', 0)} 条   "
                    f"笔记 {stats.get('notes', 0)} 条   "
                    f"表情含义 {len(stats.get('groups') or [])} 群")
            if stats.get("failures"):
                line += f"   没跑成 {stats.get('failures')} 次"
            if stats.get("overrides"):
                line += f"   人工改过 {stats['overrides']} 个表情"
            lines.append(line)
            if stats.get("last_run_at"):
                lines.append(
                    f"  上一次：{_format_clock(float(stats['last_run_at']))}"
                    + (f"   {stats.get('last_result', '')}" if stats.get("last_result") else "")
                )
            lines.append("  看原文 / 改方向：/super quote")
        # 黑话词条（2026-10-06）：与金句同一条口径——**学来的东西要看得见**。
        # 概览只报计数与"在不在记"，词条原文与证据在 `/super slang`。
        slang = getattr(self, "slang_watcher", None)
        library = getattr(self, "slang_library", None)
        if slang is not None or library is not None:
            stats = slang.snapshot() if slang is not None else (library.snapshot() if library else {})
            line = (f"黑话词条：{stats.get('entries', 0)} 条   "
                    f"群 {len(stats.get('groups') or [])} 个")
            if slang is not None:
                line += f"   听过 {stats.get('captured', 0)} 条"
                if stats.get("failures"):
                    line += f"   没记成 {stats.get('failures')} 次"
            lines.append(line)
            lines.append("  看词条 / 改释义：/super slang")
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
        pending = status.get("pending_report") or {}
        if pending:
            lines.append(
                f"有一封写好了还没寄出去（{pending.get('day') or '?'} 写的"
                f"「{pending.get('subject') or '（无主题）'}」）：下次到点先把它原样寄出去。"
            )
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

    def _report_seams_for_mail(self):
        """日报要用的那份接缝（`ReportSeams`）。

        **不能把引擎自己当接缝递给 `run_once`**：`collect_materials` 张口就是
        `report.snap()`，而引擎上那个方法叫 `snapshot()`（也没有 `remember_letter`，
        它叫 `note_letter`）——所以 2026-10-11 之前 `/super mail send` 一按就
        `AttributeError`。装配点在 `runtime.py` 里把 `ReportSeams` 挂在
        `engine.plugin_registry.report` 上，这里取那一份。
        """

        registry = getattr(self, "plugin_registry", None)
        seams = getattr(registry, "report", None)
        return seams

    async def _mail_send_now(self) -> str:
        """立刻写一封发出去。用来"跑通一次"，也给超管一个不等到 23:00 的办法。"""

        reporter = getattr(self, "daily_reporter", None)
        if reporter is None:
            return "每日汇报未启用，无法发送。"
        seams = self._report_seams_for_mail()
        if seams is None:
            return "没有找到日报的接缝（插件装配没走到），这次不发。"
        draft = await reporter.run_once(seams, force=True)
        if draft is None:
            return "这次没发出去（写不出来或发送失败）。看看日志里的原因。"
        return f"已寄出：{draft.subject}（正文 {len(draft.body)} 字）。"

    # --- 她写出去的信（"她还记得自己写了什么"） --------------------------------

    def note_letter(self, letter: dict) -> None:
        """记下一封刚发出去的信（最近在前）。`DailyReporter` 与 runtime 都往这里灌。"""

        cleaned = {
            # 信的 `at` 是**墙上时间**（`mail_state.json` 里存的就是 `time.time()`），
            # 缺省值时不能拿单调时钟来凑——那是"开机时长当 epoch"，下面算信龄时
            # 会减出一个天文数字（这条与 `_now_text` 的 1970 是同一类错）。
            "at": float(letter.get("at") or self.wall_clock()),
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
        # 这里**只能**用墙上钟：`at` 是跨重启留下来的时刻，单调钟的零点在重启后没有
        # 可比性。所以它走注入的墙钟（`self.wall_clock()`），不是另取一次 `time.time()`。
        age_hours = max(0.0, (self.wall_clock() - float(newest.get("at") or 0.0)) / 3600.0)
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

    def _super_memory(self, action: SuperAction, message: IncomingMessage) -> str:
        """记忆查看。未接入记忆服务时给出明确说明而不是报错。

        `/super memory`（不带动词）多一行**本群记不记**：它是按群的开关，
        所以答案取决于"这句话是在哪个群说的"（`memory_group_note`）。
        待处理材料数在这份概览里本来就有，读写失败数在 `/super status` 那一行——
        这里不重复印第二遍。
        """

        if self.memory_service is None or getattr(self.memory_service, "store", None) is None:
            return "记忆服务未启用，无法查看。"
        store = self.memory_service.store
        try:
            if action is SuperAction.MEMORY:
                return build_overview(
                    store.counts(),
                    memory_note=self.memory_group_note(message.target.group_id),
                ).render()
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
        self._drop_pending_segments(session_id)
        # 判定采样的那份按会话计数一起走（口径与 `_drop_session_clients` 相同）：
        # 会话都没了，窗口与积攒再留着只是僵状态。
        self.judge_sampling.forget_session(session_id)
        return existed

    def _drop_pending_segments(self, session_id: str) -> None:
        """丢掉这个会话**还没发出去**的续发段。

        由来（2026-10-08 隔离增长审计，核查者补的漏）：`_follow_ups` 与 `states` 同键、
        同样只在"有消息被处理"时被取走（`take_follow_ups`），而 `leave()` /
        `disable_group()` 都不清它——被停用、被清理的会话，它的正文（长回复拆出来的
        那几段）会一直躺在内存里，直到进程重启。
        清它是**纯释放**：那几段属于一段已经作废的回复，不会再有人来取。
        """

        self._follow_ups.pop(session_id, None)

    def _forget_scheduled(self, session_id: str) -> int:
        """把这个会话从"排队等当值"里摘掉，并清掉那几条排队消息的记账。

        只碰**调度**：`focus` 的队列/顺序/稍等冷却，加上那几条消息在
        `_resumed_ids`（"已经记过历史、去过重"）与 `_promised_ids`（"答应过必须给
        结果"）里的条目。会话历史与记忆一律不动（用户 2026-10-08：`/admin disable`
        只等于"不回复"）。

        由来（2026-10-08 隔离增长审计 [7]）：被停用的群永远轮不到
        `take_next_session`，所以它的积压与那两条记账会僵到重启——比"随机淘汰"
        更彻底。返回摘掉了几条排队消息（给日志用）。
        """

        queued = self.focus.forget_session(session_id)
        for item in queued:
            self._forget_queued_message(item.message_id)
        return len(queued)

    def _forget_queued_message(self, message_id: str) -> None:
        """一条"被排队过"的消息作废时，把它在那两张记账表里的条目一起清掉。"""

        self._resumed_ids.pop(message_id, None)
        self._promised_ids.pop(message_id, None)

    async def list_groups(self) -> tuple[dict[str, object], ...]:
        """云茹所在的群聊（**给面板那一页用的只读列表**，2026-10-09 用户要求）。

        用户原话：*"群聊页，应当展示**云茹所在的所有群聊**并展示**群名**而不是群号"*。
        群名只能从核心这条 `get_group_list` 取（面板拿不到 transport），所以这一份是
        核心给的。每一项**只有四个字段**：`id` / `name` / `dialogue_on` / `memory_on`
        ——字段白名单由 `tests/test_ui_groups_seam.py` 逐个钉住，多一个就是多一条缝。

        **缓存**：`dev_config.groups_cache_seconds()`（默认 600 秒，**调用时现读**）
        之内连着调用只打一次 OneBot；**取失败回落上一次的已知列表**（再没有就用
        "哪些群开对话 / 开记忆"那两张名单自己拼、名字写成"群 <id>"）——
        **绝不抛异常**：面板那一页不许因为对面抖一下就崩掉。
        """

        now = self.wall_clock()
        ttl = dev_config.groups_cache_seconds()
        if self._groups_cache and ttl > 0 and now - self._groups_cache_at < ttl:
            return self._groups_cache
        transport = getattr(self, "transport", None)
        if transport is None:
            return self._groups_fallback()
        try:
            response = await transport.call_api("get_group_list")
        except Exception:  # noqa: BLE001 - 面板那一页不许因为对面抖一下就崩
            logger.exception("群列表查询失败，回落上一次的已知列表")
            return self._groups_fallback()
        rows = []
        # 回执拆包**复用同一份实现**（`OneBotRelayTargetResolver` 的静态方法）——不另写一套：
        # 两处拆法一旦分叉，"有的路认得回执、有的认不得"这类怪事就来了。
        for item in OneBotRelayTargetResolver._data_list(response):
            row = self._groups_row(item)
            if row is not None:
                rows.append(row)
        if not rows:
            return self._groups_fallback()
        self._groups_cache = tuple(rows[:MAX_GROUPS_LIST])
        self._groups_cache_at = now
        return self._groups_cache

    def _groups_row(self, item: dict[str, object]) -> dict[str, object] | None:
        """群列表里的一条 → 面板那一页要的四个字段（名字空就写"群 <id>"）。"""

        group_id = OneBotRelayTargetResolver._id_text(item.get("group_id"))
        if not group_id:
            return None
        return {
            "id": group_id,
            "name": OneBotRelayTargetResolver._text(item.get("group_name")) or f"群 {group_id}",
            "dialogue_on": group_id in self.enabled_group_ids,
            "memory_on": group_id in self.memory_group_ids,
        }

    def _groups_fallback(self) -> tuple[dict[str, object], ...]:
        """取不到群列表时给什么：**上一次的已知列表**；再没有就用那两张名单自己拼。"""

        if self._groups_cache:
            return self._groups_cache
        known = sorted(self.enabled_group_ids | self.memory_group_ids)[:MAX_GROUPS_LIST]
        return tuple({"id": group_id, "name": f"群 {group_id}",
                      "dialogue_on": group_id in self.enabled_group_ids,
                      "memory_on": group_id in self.memory_group_ids}
                     for group_id in known)

    def _forget_groups_cache(self) -> None:
        """名单一变就把群列表缓存作废（否则面板最长 600 秒还画着旧状态）。

        2026-10-11 用户报"webui 里的记忆开关不生效"：开关**本身**每次现读都对，
        坏的是记忆页那一列状态——它来自 `list_groups()`，而那份列表只在
        `groups_cache_seconds()`（默认 600 秒）到期时才重算，`memory_on` 是
        **填缓存那一刻**算的。于是关掉之后整页重画/切页回来又变回"开"（生产
        审计里那颗开关 71.5 秒内被点了两次，就是这么来的）。名单一变就作废：
        缓存只省"OneBot 打得太勤"，不再兼任"状态的旧快照"。
        """

        self._groups_cache = ()
        self._groups_cache_at = 0.0

    def enable_group(self, group_id: str) -> bool:
        # 群号必须是 **ASCII 数字**（`isdigit()` 对全角数字也返回真，而那不是 QQ 号）
        if not (group_id.isascii() and group_id.isdigit()) or not 5 <= len(group_id) <= 12:
            return False
        before = group_id in self.enabled_group_ids
        self.enabled_group_ids.add(group_id)
        if not before:
            self._forget_groups_cache()
            self.persist_state()
        return not before

    def disable_group(self, group_id: str) -> bool:
        # 群号必须是 **ASCII 数字**（`isdigit()` 对全角数字也返回真，而那不是 QQ 号）
        if not (group_id.isascii() and group_id.isdigit()) or not 5 <= len(group_id) <= 12:
            return False
        was_enabled = group_id in self.enabled_group_ids
        self.enabled_group_ids.discard(group_id)
        if was_enabled:
            self._forget_groups_cache()
            session_id = f"group:{group_id}"
            self.sessions.leave(session_id)
            # **只清"调度/待发"这类东西，不碰会话历史与记忆**：用户 2026-10-08 定死
            # 了 `/admin disable 群` 只等于"不回复"，上下文与记忆照记（见
            # `_absorb_without_reply`）。所以这里清的是"还要不要发/还要不要排队"，
            # 不是"记得什么"。
            self._drop_pending_segments(session_id)
            dropped = self._forget_scheduled(session_id)
            if dropped:
                logger.info(
                    "Stage 3 focus: dropped queued messages on disable session=%s count=%s",
                    session_id, dropped,
                )
            self.persist_state()
        return was_enabled

    def enable_group_memory(self, group_id: str) -> bool:
        """让这个群的群消息进记忆（`/super enable memory` 在哪个群说就动哪个群）。

        返回"这次真的改了没有"——与 `enable_group` 同一形状（幂等、只在真变化时落盘）。
        **不碰 `enabled_group_ids`**：回不回是另一张名单。
        """

        # 群号必须是 **ASCII 数字**（`isdigit()` 对全角数字也返回真，而那不是 QQ 号）
        if not (group_id.isascii() and group_id.isdigit()) or not 5 <= len(group_id) <= 12:
            return False
        before = group_id in self.memory_group_ids
        self.memory_group_ids.add(group_id)
        if not before:
            self._forget_groups_cache()
            self.persist_state()
        return not before

    def disable_group_memory(self, group_id: str) -> bool:
        """这个群不再收新记忆。**已有的待处理行一条都不删**（重新打开接着处理）。

        与 `disable_group`（关对话）只有一处一致的地方：都是幂等的。别的一概不同——
        关对话会顺手清掉这个群的短期会话状态，关记忆**什么都不清**：短期会话与记忆
        是两份东西，而且"关掉"只该意味着"不再收新的"。
        """

        # 群号必须是 **ASCII 数字**（`isdigit()` 对全角数字也返回真，而那不是 QQ 号）
        if not (group_id.isascii() and group_id.isdigit()) or not 5 <= len(group_id) <= 12:
            return False
        was_enabled = group_id in self.memory_group_ids
        self.memory_group_ids.discard(group_id)
        if was_enabled:
            self._forget_groups_cache()
            self.persist_state()
        return was_enabled

    def memory_group_note(self, group_id: str | None) -> str:
        """一句话说清"这个群记不记"（`/super memory` 用）。"""

        if not group_id:
            return ("本群记忆：这里不是群聊。这条开关按群生效，要在要记的那个群里发"
                    "（/super enable memory）。")
        if group_id in self.memory_group_ids:
            return f"本群记忆：开（{group_id} 的新消息会进记忆收件箱）"
        return (f"本群记忆：关（{group_id} 的新消息不进记忆收件箱；"
                "在本群发 /super enable memory 打开）")

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
        """把群启停名单、**群记忆名单**、启停开关与短期会话写入本地状态库。

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
            # 记忆名单与对话名单一起落盘、一起恢复，但**各存各的键**：
            # 一张是"回不回"，一张是"记不记"（见 `memory_group_ids` 的说明）。
            "memory_group_ids": sorted(self.memory_group_ids),
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
        # 记忆名单照自己的键恢复。**不跟对话名单互相兜底**：老状态文件里没有这个键，
        # 那就是"还没人开过记忆"（空名单），而不是"沿用对话名单"——那正是要拆掉的耦合。
        self.memory_group_ids = set(persisted.memory_group_ids)
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

    async def _absorb_without_reply(self, message: IncomingMessage, *, resumed: bool) -> None:
        """把一条**不该得到回复**的群消息记进上下文与记忆，什么都不回。

        用在两处：这个群没开对话（`/admin disable`），以及管理员在没开的群里闲聊。
        用户 2026-10-08 的原话是"**只是不回复，不要影响上下文和记忆入栈**"，
        所以这里只做"记"；**"不回"由调用方原样 `return None` 保证**，一个字没动。

        与冷群落背景那条路（`handle()` 里的焦点分流）同一个口径：
        `refresh_activity=False`——她没在跟这个群说话，不该把"最近活跃"往后推，
        否则这个群会在她没开口的情况下自己变成热群。
        """

        # 记了就必须去重：老路径"什么都不做"所以不需要它，现在历史与记忆都会多一条。
        if not resumed and not self.deduplicator.accept(message.message_id):
            return
        if self.memory_service is not None:
            # 记忆收不收是**它自己那张按群名单**的事（`/super enable memory`），
            # 这里不看群对话开关。
            self.memory_service.capture(message)
        state = self.sessions.state(message.session_id)
        # 先补背景再记这一条：`_seed_history` 只在历史为空时补，而下面这条一进去
        # 历史就不空了——不先补，这个会话之后永远补不上群背景（比改动前更差）。
        await self._seed_history(message, state)
        state.add(message, self.clock(), refresh_activity=False)

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
            # **`/admin disable 群` 从 2026-10-08 起只表示"不回复"**：这条消息照进
            # 短期历史，也照进记忆收件箱（**只要那个群开了记忆**），只有"回不回"
            # 被关掉。用户原话："只是不回复，不要影响上下文和记忆入栈"。
            #
            # 只对**群消息**这么做：私聊白名单外的人仍然整条丢弃（fail-closed，
            # 见 AGENTS.md §2.1），那里不是"这个群没开"而是"这个人没被允许"。
            # 记忆那张名单与这张互不相干（`DialogueEngine.memory_group_ids`）。
            if message.target.group_id is not None:
                await self._absorb_without_reply(message, resumed=resumed)
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
            # 同上：只是不回，上下文与记忆照记（管理员在没开的群里说的也是群里的真事）。
            await self._absorb_without_reply(message, resumed=resumed)
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
        #   提及/引用 → 静默进队列；@ 她 → **也静默进队列**。
        # 被 @ 不等于必须出声（2026-10-11 用户："被艾特到可以不说话"）：她可以
        # 排到队里，轮到她了再由判定决定接不接。**只有这个群真的积压了**
        # （队列里已经有别人在等）才回一句"稍等"（同群 120 秒冷却）——用户原话：
        # "只有积压的才会说必须回复一下，因为积压的时候会回复等会回你这种规则性发送的话"。
        # 没有人在当值时，走下面的 20 条巡检路子；巡检同一时刻只跑一个。
        if group_chat and not allowed_private and not is_admin_control and not resumed:
            if not self.focus.is_hot(message.session_id):
                if self.focus.hot is not None or self.focus.sweeping:
                    self._stats["deferred_messages"] += 1
                    # 做法一：消息**到达即进该群历史**（只作背景、不调模型），
                    # 所以她轮到这个群时能看见"她不在的时候群里聊了什么"。
                    state.add(message, now, refresh_activity=False)
                    # 判定采样：这条也是"最近 30 秒这个群来了多少消息"的一部分——
                    # 速率高了她才需要攒批，被排队的消息不能不算数。
                    self.judge_sampling.note_message(message.session_id, now)
                    if addressed:
                        queued = self.focus.enqueue(message.session_id, message)
                        # 标记为"已记过历史、且已过去重"：轮到它时 handle() 要跳过那两步。
                        self._resume_later(message.message_id)
                        self._stats["focus_queued"] += 1
                        logger.info(
                            "Stage 3 focus: queued for later session=%s depth=%s",
                            message.session_id,
                            queued,
                        )
                        # 积压才开口：`queued` 是这一条入队之后的深度。深度 1 说明
                        # 她是第一个在等的——那就先不出声（轮到她了，判定还能不接）；
                        # 深度 ≥2 说明这个群已经有人在等，回一句"稍等"才是真的在交代。
                        if (queued > 1 and message.is_bot_mentioned
                                and self.focus.ack_allowed(message.session_id, now)):
                            self.focus.note_ack(message.session_id, now)
                            ack = self._pick_ack()
                            # 回过"稍等"的，之后必须给结果：判定不许否决它。
                            self._promise(message.message_id)
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
            if group_chat:
                # 判定采样：同上——这条也进"最近窗口来了多少消息"的账（只记群）。
                self.judge_sampling.note_message(message.session_id, now)
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
            if group_chat:
                # 判定采样：消息从这里进历史，也从这里进"最近窗口来了多少条"的账；
                # 被焦点排队过的（resumed）到达时已经记过，不再重复计。
                self.judge_sampling.note_message(message.session_id, now)
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

        # --- 判定采样闸：群消息来得太快时攒几条再判（judge_sampling.py）----------
        # 只回答"这条消息要不要**现在**触发判定调用"这一个问题：不放行的消息
        # 上面已经进了历史，判定下一次照看得到，一条不丢。私聊不走采样（一对一
        # 不需要）；被焦点排队过、轮到这个群的消息（resumed）对闸免疫——她答应过
        # "稍等"就必须给结果；被 @ 的消息不等攒满（豁免在闸里，见 admit）。
        # 阈值档位（< 8 每条都判 / >= 8 攒 5 / >= 16 攒 10 / >= 30 攒 20）全部
        # `QQBOT_JUDGE_RATE_*` 现读，改配置不必重启。
        if group_chat:
            if resumed:
                self.judge_sampling.note_evaluation(message.session_id)
            elif not self.judge_sampling.admit(
                message.session_id, now, mentioned=message.is_bot_mentioned
            ):
                self._stats["deferred_messages"] += 1
                return None

        # 脱敏观测指标：把触发原因收敛成固定类别，不携带任何聊天内容。
        trigger_kind = self._trigger_kind(trigger)
        self.metrics.record_trigger(trigger_kind)
        # 没人在当值 → 这一次是"巡检"。同一时刻只允许一个群在巡检，别的群继续等
        # （两个群同时攒够 20 条时不会同时叫模型，也就不会同时出现两段当值对话）。
        # 守卫是**持有者标记**：拿不到就说明别人（另一个会话或焦点时钟那一跳）正在
        # 巡检，这一条按"延迟"处理；收尾时也只还自己那份（2026-10-08 失败降级审计 3）。
        sweep_owner: str | None = None
        if group_chat and not allowed_private and not resumed and self.focus.hot is None:
            if self.focus.sweeping:
                self._stats["deferred_messages"] += 1
                logger.debug("Stage 3 focus: sweep busy, defer session=%s", message.session_id)
                return None
            owner = f"recv:{message.session_id}"
            if self.focus.begin_sweep(owner):
                sweep_owner = owner
        try:
            return await self._model_path(
                message, state, now=now, mode=mode, trigger=trigger, trigger_kind=trigger_kind,
                addressed=addressed, group_chat=group_chat, resumed=resumed,
            )
        finally:
            if sweep_owner is not None:
                self.focus.end_sweep(sweep_owner)

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
        # 「惦记事」（2026-10-11）：她自己记着、还没划掉的事（"回头问他那个接口好没"）。
        # 与 `care_note` 是两样东西：那是"别人的处境，提一次就够"，这是"她自己的备忘，
        # 没办完就还在"——所以按小时限流、可以提不止一次，而且**判定与回复两边都看得到**
        # （用户口径："交给下一轮的回复agent和判定agent（判定这边我不是很确定，你看着办）"）。
        own_notes: tuple[dict, ...] = ()
        # 「没把握就别断言」这条路的确定性结论（`ask_when_unsure`）：第一道在判定刚回来时
        # 就能算（没被叫到又没懂 → 直接不出声），第二道要等资料取完才算得出来
        # （三处根据一样都没有 + 被叫到 → 之后要看她的草稿）。
        grounding = None
        ask_rules = self._flags().ask_when_unsure
        # 这一轮的**记号**（不可信数据的边界，2026-10-08；两档见 2026-10-11）：回复段用它
        # 把每个 DATA 块包起来，回复回来之后再拿它做一次纯字符串判定
        # （`data_boundary.reply_leaks_marker`）。总闸门关掉时是空串：下面的包裹与判定都
        # 变成恒等操作，请求与改动前逐字相同。**取法跟档位走**（`_data_marker`）：
        # 缓存友好型是"一季一个、压缩才换"，增强型是"每轮现取 7 位"。
        marker = self._data_marker(state)
        # 「到点提醒」的邮箱那一步（2026-10-08）：他这一句里如果带着邮箱，而核心正等着
        # 他给（上一轮认到提醒但没值），就在这里收下来——记进记忆（按人）+ 补交那条待办。
        # 放在判定之前：他这一句常常就是"我的邮箱是 x@y"，判定很可能判 NO_REPLY。
        self._take_remind_email(message)
        # "等他说怎么收"等太久了就先按上次那种办（2026-10-09："还是没叫我"——
        # 那条一直卡在等渠道上、永远不响）。放在拼回复 prompt 之前：这一轮就生效。
        self._expire_remind_channel_wait(message)
        # 双 agent 结构：先由判定 agent 决定"要不要接"。它的 prompt 与窗口都小得多，
        # 所以这次调用便宜；判定说 NO_REPLY 就直接结束，省掉回复那一次完整调用。
        if self._judge_on(message.session_id):
            must_reply = message.message_id in self._promised_ids
            verdict = await self._judge(
                message, state, trigger=trigger, addressed=addressed, must_reply=must_reply
            )
            # 判定说"这一句越界了" → 先落库 + 让缓存失效，**再**拼回复的 prompt。
            # 顺序就是这条需求本身：让她变冷的那句话，回复当场就是冷的。
            # 它同时回答"这一轮要不要接"：判定报 `PROBE`（有人打探她怎么做出来的）
            # 时返回 True，这一轮就不接（不写关系轴、也不调回复那一次）。
            if self._apply_guard_signal(verdict, message):
                return None
            # 「到点提醒我」（2026-10-08）：**同一次判定的返回**里就带着这个意图，
            # 这里只把它交给插件（没有插件就什么都不做）。放在回复之前：核心那部分
            # 不需要等回复，而且不改变"要不要回、回什么"。
            self._note_remind(message, verdict)
            if group_chat and self.focus.is_hot(message.session_id):
                # 有人接着这段话题（她被叫到，或判定说还在同一条线上）→ 刷新当值计时。
                if addressed or verdict.related:
                    self.focus.note_related(now)
            # 判定每轮都读到话题：即使这一轮她不出声，也要用它刷新上下文——
            # "她没说话"不等于"话题没变"，而带下去的旧话题正是她强推旧事的原因。
            self._refresh_context_from_verdict(state, verdict, session_id=message.session_id)
            verdict_related = bool(getattr(verdict, "related", True))
            lore_wanted = bool(getattr(verdict, "lore", True))
            # 「没把握就别断言」的第一道确定性规则（2026-10-04 加，2026-10-05 晚改口径）：
            # **没被叫到 + 没懂 + 具体话题 → 不插话**。不出声，**不是**"问一句"——
            # 用户否掉的就是"不懂就问"那条路（"没根据的时候，要么不出声，要么别断言"）。
            # 它放在取资料之前：这一轮本来就不出声，没必要为它去翻东西（翻要花钱）。
            # **"被叫到时没有否决权"的原意保留**：这里唯一的沉默条件是"没被叫到"，
            # 被叫到时她照样必须回应——只是那一版话里若有具体断言，会被打回重写一次
            # （见下面 `decide_grounding` 的第二道与草稿那一关）。
            if ask_rules and verdict.unsure and verdict.specialist and not (addressed or must_reply):
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
            # 与下面"事后通知"那一处同口径（2026-10-08，失败降级审计 8）：知识库/插件
            # 材料整段时间拿不到时，"她变笨了"也要能在计数上看见，不能只有一行日志。
            self.metrics.record_extension_failure()
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
            # 「惦记事」：她自己的备忘（最多两条；拿不到就是空元组）。
            own_notes = await self.memory_service.own_notes(message)
        # 「没把握就别断言」的第二道：**根据检查**（确定性，模型"觉得自己懂"绕不过它）。
        # 三种根据——知识库命中 / 记忆命中 / 语境明确——一样都没有、判定又说这是具体的
        # 专业·事实话题（或者判定说她没懂）时：**没被叫到就不出声**；被叫到照常开口，
        # 但那之后要看一眼她的草稿（见下面那一关）。日常、情感、闲聊
        # 不受这条限制（`care=False` 就是照旧，正常聊天一个字都不受影响）。
        #
        # 放在这里（而不是判定刚回来时）是因为前两样根据**要等资料取完才知道**。
        if ask_rules and self._judge_on(message.session_id):
            grounding = decide_grounding(
                verdict,
                called=bool(addressed or must_reply),
                has_knowledge=bool(prompt_material.knowledge_items),
                has_memory=bool(memory_material.records),
                context_explicit=context_is_explicit(message, state.recent()),
            )
            if not grounding.speak:
                self._stats["clarify_quiet"] += 1
                logger.info(
                    "Stage 3 clarify: %s session=%s topic=%s specialist=%s grounded=%s",
                    grounding.reason, message.session_id, state.context.topic or "-",
                    grounding.specialist, grounding.grounded,
                )
                return None
            # 只有"要当心"的那几种才记日志：照旧说话的绝大多数轮次不该多一条日志
            # （这条路上的每一轮都会跑到这里）。**这一层一个字都不进她的 prompt**：
            # 真正的动作是"草稿有具体断言就打回重写一次"（见下面）。
            if grounding.rewrite:
                logger.info(
                    "Stage 3 clarify: %s session=%s topic=%s understood=%s specialist=%s grounded=%s",
                    grounding.reason, message.session_id, state.context.topic or "-",
                    getattr(verdict, "understood", "clear"),
                    grounding.specialist, grounding.grounded,
                )
        # 名册（含**核心的角色事实**：她是谁、对方是不是管理员/群主）在调模型之前算一次。
        # `await` 的是"把角色取进缓存"那一步（`build_roster_lines`），渲染本身是同步的。
        roster = tuple(await build_roster_lines(
            # 只有这一份来源。插件不自己取：要问就问 `registry.shared_roles()`
            # （见 `runtime.build_engine` 里 `provide_roles` 那一处）。
            getattr(self, "group_roles", None), message.target.group_id, state))
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
            roster=roster,
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
            own_notes=own_notes,
            # 「她写出去的信」：不问也该知道刚寄出的那一封，问起时能翻出最近一封。
            # 只有收信人本人能看到内容（`_letter_note` 里判）。
            letter_note=self._letter_note(message),
            # 「现在是什么时候」：以前这条路完全没有时间，她答不出"现在几点/今天几号"，
            # 也判断不了"昨天晚上""三天前"（2026-09-30 用户："云茹不能获取时间吗"）。
            # 它进的是**易变段**，不影响前缀缓存。
            #
            # ⚠️ 传**墙上时钟**，不是上面那个 `now`：那个是 `self.clock()`（单调），
            # 拿它印日期就是把开机时长当 epoch（生产上真的印出过 `1970-01-02`）。
            # 焦点值班的数学仍然用 `now`——两者不能互换。
            now=self.wall_clock(),
            # 这一轮**不再有**"没把握就先问一句"的现场提示（2026-10-05 晚拆掉那条路：
            # 用户否掉的就是它）。`clarify_note` 这个口子留着（它只进易变段），
            # 现在由**核心**的提醒流程用它说一句话（正在等他给邮箱时才有；见
            # `_remind_ask_note`）——**插件一个字都不进 prompt**。
            clarify_note=self._remind_ask_note(message),
            # 「她自己的老习惯」（2026-10-06 金句学习）：按眼前这句的语境挑几条**材料**
            # 进 DATA 段。关掉开关 / 还没学出东西时是空串，请求与改动前逐字相同。
            # 2026-10-11 起它**缓存在会话状态上**（只在压缩或过了几小时才重算），
            # 理由见 `_style_material` 的说明；**放哪一段跟着档位走**（用户定案
            # "位置跟档位走"）：缓存友好型进稳定段（记号一季不变，材料才吃得到前缀
            # 缓存），增强型留在易变段——见 `build_dialogue_messages` 的
            # `style_material_stable`。
            style_material=self._style_material(message, state),
            style_material_stable=self._flags().data_marker_cache_friendly,
            # 「刚才有人给她贴了什么」（2026-10-11 的"甲"）：逐轮算，进易变段尾巴。
            reaction_material=self._reaction_material(message),
            # 不可信数据的边界记号（空串 = 总闸门关掉了，见上面 `marker`；取法跟档位走）。
            session_marker=marker,
        )
        self._stats["model_calls"] += 1
        started_at = self.clock()
        # "这一轮被砍在哪一段"（2026-10-13 夜间复盘）：回复这一腿（含它内部的审核与重写）。
        dev_config.set_turn_stage(self, "replying")
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
            # 回复这条路失败照旧往上抛（这一轮没有回复），但**要有人知道**：
            # 计数 + 连续次数，`/super status` 会印出来。
            self._stats["reply_failed"] += 1
            self._reply_failed_streak += 1
            self.metrics.record_model_error(summary)
            raise
        self._reply_failed_streak = 0
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
        # 都是最终稿。它**只判断、不改写**（2026-10-05）：不通过 → 把理由交回回复 agent
        # 在**同一轮**里重写一次（最多一次，不许循环）；重写后仍被拒 → **发重写的那一版**
        # （绝不把回复丢掉、也不卡住），并记一笔。审核超时/报错/输出认不出来一律按通过。
        # `review_enabled` 是运行期开关（面板可关）——关掉就是原稿直发。
        # **`allow_decline=True`（2026-10-11，用户："审核agent打回后回复agent可以拒绝重写
        # 原样发出"）**：这一趟是**审美**（措辞、分寸、不像人），不是"这句话有事实错误"，
        # 所以由**她自己**决定改不改——把原来那句原样写回来 = 拒绝，照第一版发。
        # 另两趟（「没把握就别断言」、「别抄不可信短码」）不传这个参数：那两趟是这一版
        # 确实有问题，必须照改，不设退路。
        # `style_rewrote` 记"**风格审核**已经把这一次重写用掉了"（见下面那一关）：
        # 它负责的是"审核与「没把握就别断言」**共用同一次**重写机会"这条规矩。
        style_rewrote = False
        if (self._flags().review_enabled and self.style_reviewer is not None
                and decision.kind is DecisionKind.REPLY
                and decision.text):
            checked = await self.style_reviewer.review_verdict(
                decision.text, context=self._review_context(message, state))
            # 「惦记事」的**入口**（2026-10-11 用户："惦记的事由审核agent负责"）：审核是唯一
            # 在她**发出去之前**看到成品的环节，所以她刚应下的那半句原话由这里顺手抓走。
            # 它进的是**记忆收件箱**（原始材料那条路，不是直接写记录——写记录只有维护 agent
            # 一条路）；维护 agent 下一拍照自己的规矩把它写成 `intention`。
            # **刻意不当场生效**：她刚说完那句，下一轮立刻再看见"你自己记着的事"就是复读。
            if checked.passed and checked.note and self.memory_service is not None:
                if self.memory_service.note_own_words(message, checked.note):
                    logger.info("Stage 3 own note captured chars=%s", len(checked.note))
            if not checked.passed:
                reason = checked.reason
                self._stats["reply_reviewed"] += 1
                logger.info(
                    "Stage 3 reply rejected by review: reason=%s draft=%s",
                    reason[:80], decision.text[:60])
                rewritten = await self._rewrite_after_review(
                    request, decision.text, reason,
                    session_id=message.session_id, trigger=trigger, trigger_kind=trigger_kind,
                    context=self._review_context(message, state),
                    known_message_ids=frozenset(message_ids),
                    must_reply=self._judge_on(message.session_id),
                    allow_decline=True,
                )
                style_rewrote = True
                if rewritten is not None:
                    # **只换正文**：这一轮要不要说、引用哪一句、语境是什么，
                    # 都是判定与第一次解析定下来的，不因为重写一遍就改主意。
                    decision = replace(decision, text=rewritten)
        # 「没把握就别断言」的**兜底那一关**（2026-10-05 晚加，治"不懂装懂"）。
        #
        # 条件全部确定性：**被叫到**（`grounding.rewrite` 只在被叫到时为 True）
        # + **没懂 / 具体话题又没有根据**（知识库 / 记忆 / 语境三处，`decide_grounding` 算的）
        # + **她这一版的话里确实出现了具体断言**（数字 / 型号 / 流程——`has_specific_claim`）
        # → **打回，让她自己在同一轮里重写一次**（复用上面那条路，不另造机器；最多一次）。
        #
        # 为什么放在审核之后：两条路共用**同一次**重写机会——先看审核，审核已经打回重写过
        # 就不再重写第二次（"最多一次"这条边界对两条路一起成立）。
        #
        # 为什么还要"有具体断言"才打回：只说她自己那点感受的（"这个我没跟上"、
        # "行，等你"）不打回——**不许把正常聊天掐死**。打回时递的那句话
        # （`NO_ASSERT_NOTE`）说的是"别断言你不知道的、不知道就直说"，
        # **不是**"问她一句"——那条路用户已经否掉了。
        if (grounding is not None and grounding.rewrite and not style_rewrote
                and decision.kind is DecisionKind.REPLY and decision.text
                and has_specific_claim(decision.text)):
            first_draft = decision.text
            self._stats["reply_grounding_rewrites"] += 1
            logger.info(
                "Stage 3 grounding: 没把握却写了具体断言，打回重写 session=%s topic=%s "
                "understood=%s specialist=%s draft=%s",
                message.session_id, state.context.topic or "-",
                getattr(verdict, "understood", "clear"),
                getattr(verdict, "specialist", False), first_draft[:60],
            )
            rewritten = await self._rewrite_after_review(
                request, first_draft, reason="",
                session_id=message.session_id, trigger=trigger, trigger_kind=trigger_kind,
                context=self._review_context(message, state),
                known_message_ids=frozenset(message_ids),
                must_reply=self._judge_on(message.session_id),
                note=NO_ASSERT_NOTE,
            )
            if rewritten is not None:
                # 重写版**已经过了一遍审核**（在 `_rewrite_after_review` 里，跟审核那条路
                # 共用同一次机会）：仍被拒也照发重写的那一版、并记一笔，
                # 审核打回不是否决权，回复绝不许被丢掉。这里只换正文。
                decision = replace(decision, text=rewritten)
            # 重写这一趟空手/失败 → **发第一版**（宁可照旧，也不许把回复弄丢）。
        # 「不可信数据的短码」那一关（2026-10-08，最后一道：**上面任何一次重写之后
        # 的最终稿**都要过它）。
        #
        # 判据是纯字符串（`data_boundary.reply_leaks_marker`，**零额外模型调用**）：
        # 她这一版正文里出现了**当轮那个短码** → 说明她把"资料记号"抄进了要说的话里
        # （那是注入生效的样子）。处理分两步，与前面两条重写路**刻意不同**：
        #
        # * **第一次**：交回回复 agent 在同一轮里重写一次，反馈照样走
        #   `_rewrite_request`（"你刚写的那一句要重来"）。这一趟**不消耗**风格审核/
        #   「别断言」那一次机会——那条"最多一次"的规矩是**那两条路之间**的，
        #   这里要求的是各自独立一次（安全维度不许被风格维度挤掉；`style_rewrote`
        #   仍然只由审核那一条路置位）；
        # * **再犯**：**这一条不发**（fail-closed），并记 `data_marker_drops`。
        #   宁可少说一句，也不把"能证明注入生效"的话发到群里。
        #
        # 日志里**不许写短码明文**（与 `security` 里"不记录敏感内容"同一口径）：
        # 只记会话、触发与条数。
        if (marker and decision.kind is DecisionKind.REPLY and decision.text
                and data_boundary.reply_leaks_marker(decision.text, marker)):
            self._stats["data_marker_retries"] += 1
            logger.warning(
                "Stage 3 reply carried the boundary marker; rewriting once: "
                "session=%s trigger=%s chars=%s",
                message.session_id, trigger_kind, len(decision.text))
            rewritten = await self._rewrite_after_review(
                request, decision.text,
                reason="你把消息里那串短码写进去了。那些短码只是记号，不要抄、也不要提。",
                session_id=message.session_id, trigger=trigger, trigger_kind=trigger_kind,
                context=self._review_context(message, state),
                known_message_ids=frozenset(message_ids),
                must_reply=self._judge_on(message.session_id),
                # **不许把短码又递回给她**：反馈里引述那一版正文时把它抹掉
                # （否则等于一边说不许抄、一边示范一遍）。
                redact=(marker,),
            )
            if rewritten is not None:
                decision = replace(decision, text=rewritten)
            if data_boundary.reply_leaks_marker(decision.text, marker):
                self._stats["data_marker_drops"] += 1
                logger.warning(
                    "Stage 3 reply still carried the boundary marker; dropping this one: "
                    "session=%s trigger=%s chars=%s",
                    message.session_id, trigger_kind, len(decision.text))
                decision = replace(decision, text="")
        # 「孤立的标签片段」那一关（**最后一道，只动发出去的那串字**）。
        #
        # 由来（2026-10-09 用户实测，有原文）：群里收到的一条正文尾巴上挂着
        # `</parameter>`——模型那一轮吐了半截工具调用那种 XML，解析只取到了前面的
        # 正文，收尾标签留在了正文里，跟着发了出去（400 条里 1 条，罕见但看得见）。
        #
        # 判据刻意保守（`security.scrub_outbound_tags`）：**只剥按名字认得出来的**
        # 标签——我们自己的信封与模型侧工具调用那一族；名字认不出来就一个字都不动。
        # 所以她聊代码写 `<div>`、写数学 `a<b>` 都不许被这里切掉。
        #
        # 位置：在**任何一次重写之后**、在写历史 / 写记忆 / 分段发送之前——
        # 于是历史里记的是净化后的正文，模型下一轮不会再看到那半截标签；
        # 判定要不要说、说什么、引用哪一句，一个都不受影响（只换那一串字）。
        scrubbed = scrub_outbound_tags(decision.text)
        if scrubbed.removed:
            self._stats["reply_tags_scrubbed"] += len(scrubbed.removed)
            logger.warning(
                "Stage 3 reply carried protocol tag fragments; stripped before send: "
                "session=%s trigger=%s tags=%s",
                message.session_id, trigger_kind, ",".join(scrubbed.removed))
            decision = replace(decision, text=scrubbed.text)
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
            if own_notes and self.memory_service is not None:
                # 「惦记事」：她自己的备忘**不是一次性的**（没办完就还得记着），所以这里
                # 只更新"上次说起它"的时刻，隔 `INTENTION_REPEAT_HOURS` 小时才会再拿出来。
                # 与上面同口径：把这一轮带进对话的那几条都记一笔，不分辨她具体提了哪一条。
                for note in own_notes:
                    await self.memory_service.mark_own_note_noted(note["id"])
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

    def _resume_later(self, message_id: str) -> None:
        """标记：这条消息**已经记过历史、去过重**，轮到它时 `handle()` 要跳过那两步。

        **有上限**（`MAX_RESUMED_IDS`），超了淘汰**最旧**的一条——与 `_promise` 同一
        写法（`dict` 的插入序，淘汰点固定）。

        由来（2026-10-08 隔离增长审计 [3]）：它从前是 `set`，且只在 `_serve_queue`
        排空时才 `discard`——被停用、或一直排不出去的群，条目会**永久**留着。
        正常路径下条目只活到"那条消息被处理完"，所以这个上限是兜底；真撞上时也没有
        第二个后果（最坏是那一条消息被当成新的、再记一次历史）。
        """

        self._resumed_ids[message_id] = None
        if len(self._resumed_ids) > MAX_RESUMED_IDS:
            oldest = next(iter(self._resumed_ids))
            self._resumed_ids.pop(oldest, None)
            logger.debug("resumed_ids full, dropping oldest: message_id=%s", oldest)

    def _promise(self, message_id: str) -> None:
        """记下"回过稍等、必须给结果"的消息 id；超上限时淘汰**最旧**的一条。

        为什么不是 `set` + `set.pop()`：`pop()` 拿掉的是**任意**一条，被丢的可能正是
        刚刚那句"稍等"——判定因此重新获得否决权，她答应过"回头答你"却静默了。用
        `dict`（插入序）就没这个问题：淘汰点固定是最先答应的那一条。
        """

        self._promised_ids[message_id] = None
        if len(self._promised_ids) > 256:
            self._promised_ids.pop(next(iter(self._promised_ids)))

    def focus_stats(self, now: float | None = None) -> dict[str, object]:
        """给观测用的一小撮焦点状态（不含任何聊天正文）。"""

        moment = self.clock() if now is None else now
        return {
            "hot": self.focus.hot_stats(moment),
            "queued_total": self.focus.total_queued(),
            "queued_sessions": self.focus.queued_sessions(),
            # 单个群队列满（`FOCUS_QUEUE_LIMIT`）时丢掉的**最旧**消息条数。
            "queued_dropped": self.focus.dropped,
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
        #
        # **拿不到就跳过这一跳**（2026-10-08 失败降级审计 3）：从前这里无条件
        # `begin_sweep()`、`finally` 里又无条件 `end_sweep()`，所以 5 秒一跳的它会把
        # 接收路径正持有的守卫**提前清掉**——两条任务于是可以同时进 `handle()`。
        # 现在只有拿到标记才干活，且只还自己那份。
        if not self.focus.begin_sweep("tick"):
            logger.debug("Stage 3 focus tick skipped: sweep held by %s",
                         self.focus.sweep_owner)
            return []
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
            self.focus.end_sweep("tick")
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
            self._resumed_ids.pop(message.message_id, None)
            self._promised_ids.pop(message.message_id, None)
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

        **闸门**：面板那个「对话压缩」开关（`runtime_flags.compaction_enabled`，
        环境变量 `QQBOT_COMPACTION=0` 同效）关掉时，这里在算待压缩段之前就返回——
        按阈值触发的那条与切走当值群那条（`force=True`）都不发请求，历史原样留着，
        靠活窗口滚动。与"压缩失败跳过"是同一个结果，只是连调用都不烧。
        """

        if not self._flags().compaction_enabled:
            # 每一轮都会走到这里，所以只留 debug：关掉这件事本身在面板上看得见，
            # `RuntimeFlags.set()` 关的时候也已经写过一行 `agent_disabled`。
            logger.debug("对话压缩已关（compaction_enabled=false），历史保持原样: session=%s",
                         session_id)
            return
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
            # "这一轮被砍在哪一段"：压缩。它一轮最多压 `MAX_COMPACT_BATCHES_PER_TURN` 批，
            # 而每批都有自己的停顿网——这是唯一能"合法地"吃掉大半预算的一段。
            dev_config.set_turn_stage(self, "compacting")
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
        own_notes: tuple[dict, ...] = ()
        if self.memory_service is not None:
            material = await self.memory_service.identity_for(message)
            identity = material.as_judge_note(message.target.group_id, message.user_id)
            # 「惦记事」：她自己记着的事，判定这边一样只看得到、只是 DATA（"判定这边我不是
            # 很确定，你看着办"——看着办的结果是**给它**：判定要判"还在不在同一条线上"，
            # 而"她答应过回头问一句"正是这条线上的一件事）。
            own_notes = await self.memory_service.own_notes(message)
        request = build_judge_messages(
            state.topic_history(),
            current=message,
            trigger=trigger,
            addressed=addressed,
            seq_of=state.seq_of,
            alias_of=state.alias_of,
            aliases=state.aliases,
            roster=tuple(await build_roster_lines(
                # **核心的角色事实**（她是谁、对方是不是管理员/群主）——只有这一份来源。
                # 插件不自己取：要问就问 `registry.shared_roles()`（见 `runtime.build_engine`）。
                getattr(self, "group_roles", None), message.target.group_id, state)),
            summary=state.summary,
            # 判定也看得到被引用的那句是谁说的（判断"还在不在同一条线上"要靠它）。
            lookup=state.recent(),
            current_start=_effective_topic_start(state),
            must_reply=must_reply,
            # 判定读的是**写入之前**的分寸：它这一轮报 UP，变化要下一句才体现在
            # 它自己看到的数据里（回复段则当场就用新值）。
            stance=self._stance_for(message),
            identity=identity,
            own_notes=own_notes,
            # 判定也要知道现在什么时候：它判断"还在不在同一条线上"靠的正是
            # "刚才/昨天晚上/三天前"这类说法（2026-09-30 加）。
            # 与回复那条路同源：`self.wall_clock()`（墙上时间），不是 `self.clock()`。
            now=self.wall_clock(),
        )
        self._stats["judge_calls"] += 1
        started_at = self.clock()
        # "这一轮被砍在哪一段"（2026-10-13 夜间复盘）：判定这一腿正在跑。
        dev_config.set_turn_stage(self, "judging")
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
            # 沉默是刻意的（fail-closed，`tests/test_dual_agent.py` 钉着"不出声"），
            # 但"她为什么不出声"要看得见：计数 + 连续次数，`/super status` 会印出来。
            self._stats["judge_failed"] += 1
            self._judge_failed_streak += 1
            self.metrics.record_model_error(summary)
            return JudgeVerdict(should_reply=False)
        self._judge_failed_streak = 0
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

    **实际发出去的每一段由传输层记进对话日志**（`data/logs/chat.jsonl`，见
    `chat_log.py`）：这里只把"第几段 / 共几段 / 从哪一轮来"这三个只有本函数知道的
    事实交上去。模型原文在另一份日志里（`reply.jsonl`）——**两份是分开的**，
    查"群里到底收到了什么"要去对话日志看。
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
    # 分段信息（第几段 / 共几段 / 从哪一轮来）**只有这里知道**，所以由这里传给传输层，
    # 让它记进对话日志（`chat_log.py`）。与 `send_typing` 同一套可选能力规矩：
    # 传输层没有这个方法就退回普通 `send`——日志少一点可以，消息发不出去不行。
    segmented_sender = getattr(transport, "send_segmented", None)
    origin = f"message:{source.message_id}" if source.message_id else ""
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
            if callable(segmented_sender):
                await segmented_sender(item.target, item.text,
                                       reply_to=item.reply_to_message_id,
                                       part=index + 1, total=len(outgoing), origin=origin)
            else:
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
