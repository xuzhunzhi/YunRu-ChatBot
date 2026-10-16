from __future__ import annotations

import re
from dataclasses import dataclass

from .transport import IncomingMessage


@dataclass(frozen=True, slots=True)
class SecurityDecision:
    blocked: bool
    reason: str = ""
    reply: str = ""


SENSITIVE_REQUEST_PATTERNS = (
    re.compile(r"(?:电脑|本机|机器|系统|服务器).{0,12}(?:文件|进程|任务|程序|端口|环境|目录|路径|配置|数据|信息)", re.I),
    re.compile(r"(?:文件|进程|任务|程序|端口|环境变量|目录|路径|配置|数据).{0,12}(?:列表|内容|详情|信息|状态|读取|查看|导出|发给我|告诉我)", re.I),
    re.compile(r"(?:api[_ -]?key|access[_ -]?token|bearer|token|密钥|密码|凭据|secret|私钥|private key|id_rsa|cookie|session|开发配置|\.env|dev_config)", re.I),
    re.compile(r"(?:读取|查看|列出|导出|发送|上传|复制|打印|获取|告诉|执行|运行).{0,30}(?:本机|电脑|系统|文件|进程|端口|环境|密钥|密码|命令|脚本|目录|数据库|浏览器|ssh|cookie)", re.I),
    re.compile(r"(?:本机|电脑|系统|文件|进程|端口|环境|密钥|密码|命令|脚本|目录|数据库|浏览器|ssh|cookie).{0,30}(?:读取|查看|列出|导出|发送|上传|复制|打印|获取|告诉|执行|运行)", re.I),
    re.compile(r"(?:powershell|cmd|shell|命令行|执行命令|运行命令).{0,20}(?:读取|查看|导出|上传|发送|打印)", re.I),
    re.compile(r"(?:忽略|绕过|无视).{0,20}(?:安全|权限|规则|限制).{0,20}(?:文件|进程|密钥|配置|系统)", re.I),
)

BLOCKED_REPLY = ""
MAX_REPLY_LENGTH = 1000


def sanitize_chat_text(text: str, *, max_length: int = 4000) -> str:
    """Remove control characters and bound untrusted text before prompt construction."""

    cleaned = "".join(char for char in text if char in "\n\r\t" or ord(char) >= 32)
    return cleaned[:max_length]


def sanitize_reply_text(text: str) -> str:
    """约束最终出站正文，避免模型输出控制字符或异常长消息。"""

    cleaned = sanitize_chat_text(text, max_length=MAX_REPLY_LENGTH)
    cleaned = re.sub(
        r"<(decision|dialogue|context|history|message|system|user|assistant)\b[^>]*>.*?</\1\s*>",
        "",
        cleaned,
        flags=re.IGNORECASE | re.DOTALL,
    )
    cleaned = re.sub(
        r"</?(?:decision|dialogue|reply|context|history|message|system|user|assistant)\b[^>]*>",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    return cleaned[:MAX_REPLY_LENGTH].strip()


# --- 出站正文里的"孤立标签片段" ------------------------------------------------
# 由来（2026-10-09 用户实测，有原文）：群里收到的一条正文尾巴上挂着 `</parameter>`
# ——模型那一轮吐了半截工具调用那种 XML，解析只取到了前面的正文，收尾标签留在了
# 正文里，于是它跟着发出去了（那 400 条里 1 条，罕见但用户看得见）。
#
# 判据刻意保守（用户定的）：**只剥按名字认得出来的**标签；名字认不出来就一个字
# 都不动——宁可不剥，也不许切坏句子。所以她聊代码写 `<div>`、写数学 `a<b`，
# 这里都不该碰（`tests/test_reply_tag_scrub.py` 钉着）。
#
# 与 `sanitize_reply_text` 的分工：那个在**解析那一刻**清协议块与控制字符；
# 这一道在**正文送出去之前**兜最后一次，只动那一串字，不碰判定与语境。

#: 出站正文里**永远不该出现**的标签名——我们自己的信封与 DATA 分块。
#:
#: 名单是把 `src/` 里真正的标签用法 grep 出来的（`</?[a-z][a-z_0-9]*>`，排除正则
#: 命名组 `(?P<name>` 与文档里的路径占位符），分三处：
#:
#: * 回复 agent 的信封（`stage3_runtime` 解析）：`decision` / `dialogue` / `reply` /
#:   `reply_to` / `context`；
#: * 判定 agent 的信封（`dialogue_judge` 解析）：`route` / `topic` / `related` /
#:   `lore` / `understood` / `specialist` / `guard` / `guard_reason` / `remind`；
#:   **`topic_start` 现在只剩防御性**（2026-10-15 复核实测）：判定**不再**回报话题起点，
#:   `dialogue_judge` 里已经**没有任何代码解析它**（用户原话 *"我还要说几次，不切话题"*，
#:   给模型的历史一刀都不切）。名字留在名单里的理由是**出站方向**：它曾经是我们自己的
#:   信封标签，模型学舌或历史残留都可能把它吐回正文，而**出站正文里不该出现它**——
#:   剥它的成本是零（少剥一个已知标签只会让正文更脏），删它的风险是"下次它真漏出去
#:   就没人拦"。**这不是"解析还在"**，别照这一行去 `dialogue_judge` 里找消费者。
#:   什么时候能改回去：要么用户重新定一条"按话题裁历史"的口径（那时解析会回来），
#:   要么确认这段历史残留彻底不可能再出现在出站正文里（那时可以连名字一起删）。
#:   ⚠️ 名单本身**一个字都不许动**：`tests/test_reply_tag_scrub.py:148-154` 逐字钉着它。
#: * 我们往 prompt 里放的 DATA 分块（模型学舌时会连标签一起吐回来）：`people` /
#:   `history` / `message` / `text` / `earlier_summary` / `current_event` /
#:   `current_message` / `extra_prompt` / `plugin` / `knowledge` / `memory`。
OUTBOUND_TAG_NAMES = frozenset({
    "decision", "dialogue", "reply", "reply_to", "context",
    "route", "topic", "topic_start", "related", "lore", "understood",
    "specialist", "guard", "guard_reason", "remind",
    "people", "history", "message", "text", "earlier_summary",
    "current_event", "current_message", "extra_prompt",
    "plugin", "knowledge", "memory",
})

#: 模型侧工具调用 XML 的标签名。**不是我们写的**，但同样永远不该进群聊正文
#: （`</parameter>` 就是这一族；同族的其它写法一起认掉，免得下次换个名字又漏出去）。
TOOL_CALL_TAG_NAMES = frozenset({
    "parameter", "parameters", "function_calls", "function_call",
    "invoke", "invokes", "tool_calls", "tool_call", "tool_name",
    "tool_use", "tool_result", "tool_call_parameter", "antml",
})

_KNOWN_OUTBOUND_TAGS = OUTBOUND_TAG_NAMES | TOOL_CALL_TAG_NAMES

#: 一个完整的标签：`<name>` / `<name …>` / `</name>` / `<name/>`。
#: 名字里允许 `.` `:`（模型偶尔写成 `antml:parameter` 这种带命名空间的）。
_TAG_FRAGMENT_PATTERN = re.compile(r"</?([A-Za-z_][A-Za-z0-9_.:-]*)(?:\s[^<>]*)?/?>")

#: 末尾那半截**没合上**的标签（模型输出被截断）：`…正文</reply`、`…正文<reply name="x"`。
#: 只认"名字 + 规规矩矩的属性表"这一种形状——后面跟着的是中文正文就不动它
#: （`<memory 就行` 那种不该被当成标签，宁可留着）。
_TAIL_TAG_FRAGMENT_PATTERN = re.compile(
    r"</?([A-Za-z_][A-Za-z0-9_.:-]*)"
    r"(?:\s+[A-Za-z_][A-Za-z0-9_.:-]*(?:\s*=\s*\"[^<>\"]*\"|\s*=\s*'[^<>']*')?)*"
    r"/?$"
)


@dataclass(frozen=True, slots=True)
class ReplyTagScrub:
    """出站正文的标签净化结果。

    `removed` 是**剥掉的标签名**（按出现顺序、一次一个），不放正文——
    与"不记录敏感内容"同一口径，日志里也只许写名字。
    """

    text: str
    removed: tuple[str, ...] = ()


def _is_known_outbound_tag(name: str) -> bool:
    lowered = name.casefold()
    if lowered in _KNOWN_OUTBOUND_TAGS:
        return True
    # 带命名空间的写法（`antml:parameter`）：按最后一段认。
    if ":" not in lowered:
        return False
    return lowered.rpartition(":")[2] in _KNOWN_OUTBOUND_TAGS


def scrub_outbound_tags(text: str) -> ReplyTagScrub:
    """把"孤立的标签片段"从**要发出去的正文**里剥掉（保守净化）。

    **只按名字剥**：名字认不出是我们自己的信封、也不是模型侧工具调用那一族，
    就一个字都不动。末尾那些**没合上的半截标签**同样只在名字认得出来时才剥
    （模型输出被截断时就是这个样子）。

    抠掉的只有标签本身，**不吃它夹着的内容**——那可能是她真正要说的话，
    在这里丢掉等于替她改口。拿不准就不剥。
    """

    if not isinstance(text, str) or "<" not in text:
        return ReplyTagScrub(text if isinstance(text, str) else "")
    removed: list[str] = []

    def _drop(match: re.Match[str]) -> str:
        name = match.group(1)
        if not _is_known_outbound_tag(name):
            return match.group(0)
        removed.append(name)
        return ""

    cleaned = _TAG_FRAGMENT_PATTERN.sub(_drop, text)
    cleaned = _TAIL_TAG_FRAGMENT_PATTERN.sub(_drop, cleaned)
    if not removed:
        return ReplyTagScrub(text)
    # 只收掉被剥掉那一段留下的**首尾空白**，句子中间一个字都不动。
    return ReplyTagScrub(cleaned.strip(), tuple(removed))


def is_sensitive_local_request(text: str) -> bool:
    normalized = " ".join(sanitize_chat_text(text).casefold().split())
    return any(pattern.search(normalized) for pattern in SENSITIVE_REQUEST_PATTERNS)


def check_message_security(message: IncomingMessage, admin_user_ids: frozenset[str]) -> SecurityDecision:
    if not is_sensitive_local_request(message.text):
        return SecurityDecision(False)
    is_admin = message.user_id in admin_user_ids and message.sender_role in {"admin", "owner"}
    reason = "sensitive_local_request_admin_not_implemented" if is_admin else "sensitive_local_request_non_admin"
    return SecurityDecision(True, reason, BLOCKED_REPLY)


# --- 管理/超管命令的"形状"判定 ------------------------------------------------
# 2026-10-01 从 `stage3_main.py` 搬到这里，原因：**插件侧的委托与核心的闸门都要用它**，
# 放在 `stage3_main` 里会逼插件 `from ...stage3_main import privileged_command_level`
# ——插件不该 import 核心模块。它是安全谓词，`security.py` 是它该在的地方。

#: 管理/超管命令的"形状"。未授权时**不落到模型那一侧**，连历史都不进：
#: 交给模型回一句话等于确认这个前缀存在（冷群的消息会进历史并被巡检捡回模型），
#: 所以这道闸必须在会话过滤之前。
#: 按层级分别判：管理员发 `/super ...` 同样是没权限。
#: 收尾（2026-09-28）：`/admin ...` 回一句说明，`/super ...` 完全静默。
PRIVILEGED_COMMAND_PATTERN = re.compile(r"^[/#]\s*(?P<level>admin|super)\b", re.IGNORECASE)


def privileged_command_level(text: object) -> str | None:
    """消息长得像哪一层的命令：`"super"` / `"admin"` / `None`。

    只认前缀形状，**不要求能被解析出来**：`/admin 随便写点什么` 也该被挡住。

    ## 必须先剥掉开头的 @提及 / CQ:at（2026-10-01 第三轮审查抓到的真实越权）

    剥这一段原来只做在 `parse_admin_command` 里，**这里没做**，于是：

        "@x /admin relay group <群号> <内容>"

    闸门判成"不是特权命令"→ 放行 → 引擎剥掉 `@x ` 之后按管理员命令执行。
    审查者用**真 MailChannel + 真引擎**实测：伪造主人地址的来信靠这个
    **以超管身份执行 `/admin relay`**，两步之后能把攻击者给的内容发到任意群。

    现在两边**共用** `admin_control.strip_leading_mentions`——同一个口径，
    不许各写一套正则。
    """

    if not isinstance(text, str):
        return None
    from .admin_control import strip_leading_mentions

    match = PRIVILEGED_COMMAND_PATTERN.match(strip_leading_mentions(text).strip())
    return match.group("level").casefold() if match else None
