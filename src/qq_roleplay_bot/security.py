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


def is_sensitive_local_request(text: str) -> bool:
    normalized = " ".join(sanitize_chat_text(text).casefold().split())
    return any(pattern.search(normalized) for pattern in SENSITIVE_REQUEST_PATTERNS)


def check_message_security(message: IncomingMessage, admin_user_ids: frozenset[str]) -> SecurityDecision:
    if not is_sensitive_local_request(message.text):
        return SecurityDecision(False)
    is_admin = message.user_id in admin_user_ids and message.sender_role in {"admin", "owner"}
    reason = "sensitive_local_request_admin_not_implemented" if is_admin else "sensitive_local_request_non_admin"
    return SecurityDecision(True, reason, BLOCKED_REPLY)
