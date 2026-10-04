from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class AdminCommandKind(str, Enum):
    ENABLE = "enable"
    DISABLE = "disable"
    STATUS = "status"
    CLEAR = "clear"
    HELP = "help"
    ECHO = "echo"
    RELAY = "relay"
    SELECT = "select"
    CONFIRM = "confirm"
    CANCEL = "cancel"


@dataclass(frozen=True, slots=True)
class AdminCommand:
    kind: AdminCommandKind
    group_id: str | None = None
    target_kind: str | None = None
    content: str | None = None
    token: str | None = None
    target_query: str | None = None
    index: int | None = None


# 管理员命令统一用 `/admin` 前缀。旧的 `#bot` 前缀已移除：
# 两个前缀并存时，帮助文字、文档和用户记忆里永远会有一份是过时的。
# enable/disable/clear **不接受群号**：它们管的就是发命令的那个群（见下方校验）。
_ADMIN_PREFIX = r"^\s*[/#]\s*admin\s+"

_COMMAND_PATTERN = re.compile(
    _ADMIN_PREFIX
    + r"(?P<action>enable|开启|disable|关闭|status|状态|clear|清理|help|帮助)"
    r"(?:\s+(?P<group_id>\d{5,12}))?\s*$",
    flags=re.IGNORECASE,
)

_RELAY_PATTERN = re.compile(
    _ADMIN_PREFIX
    + r"(?:relay|转告|转发)\s*(?:到|给)?\s*"
    r"(?P<target_kind>group|群|群聊|群组|user|qq|私聊|个人|好友)\s+"
    r"(?P<target_id>\d{5,12})\s+(?P<content>\S[\s\S]{0,999}?)\s*$",
    flags=re.IGNORECASE,
)

# `/admin echo <文本>`：把管理员给的正文原样发出来（用于公告/校对）。
# 正文是**数据**，不是命令——它不会再被解析成命令（解析在上一层只做一次）。
_ECHO_PATTERN = re.compile(
    _ADMIN_PREFIX + r"(?:echo|复读|重复|原样发)\s+(?P<content>[\s\S]+?)\s*$",
    flags=re.IGNORECASE,
)

_RELAY_NAME_PATTERN = re.compile(
    _ADMIN_PREFIX
    + r"(?:relay|转告|转发)\s*(?:到|给)?\s*"
    r"(?P<target_kind>group_name|groupname|群名|nickname|nick|昵称)\s+"
    r"(?P<target_query>[^|]+?)\s*\|\s*"
    r"(?P<content>\S[\s\S]{0,999}?)\s*$",
    flags=re.IGNORECASE,
)

_RELAY_NAME_SIMPLE_PATTERN = re.compile(
    _ADMIN_PREFIX
    + r"(?:relay|转告|转发)\s*(?:到|给)?\s*"
    r"(?P<target_kind>group_name|groupname|群名|nickname|nick|昵称)\s+"
    r"(?P<target_query>\S+)\s+(?P<content>\S[\s\S]{0,999}?)\s*$",
    flags=re.IGNORECASE,
)

_SELECT_PATTERN = re.compile(
    _ADMIN_PREFIX
    + r"(?P<action>select|选择)\s+"
    r"(?P<token>[A-Za-z0-9_-]{8,64})\s+(?P<index>\d{1,3})\s*$",
    flags=re.IGNORECASE,
)

_TOKEN_PATTERN = re.compile(
    _ADMIN_PREFIX
    + r"(?P<action>confirm|确认|cancel|取消)\s+(?P<token>[A-Za-z0-9_-]{8,64})\s*$",
    flags=re.IGNORECASE,
)


#: 消息开头**可能带着一段 @ 或 CQ:at**（群里她被人 @ 时，OneBot 会把那个 at 段放在最前）。
#: 解析命令前必须把它剥掉，否则"@ 她 + 命令"就认不出来。
_LEADING_MENTION = re.compile(r"^\s*(?:\[CQ:at,[^\]]+\]\s*|@[^\s]+\s*)", re.IGNORECASE)


def strip_leading_mentions(text: str) -> str:
    """剥掉消息开头的 @提及 / `[CQ:at,…]` 段（只剥**一段**，与解析口径一致）。

    ## 为什么把它拿出来单放（2026-10-01 第三轮审查抓到的一个真实越权）

    `parse_admin_command` 一直在剥这一段，而 `stage3_main.privileged_command_level`
    （"这条长得像不像特权命令"那道闸）**没有**。两处口径不一致的后果：

        "@x /admin relay group <群号> <内容>"

    闸门看到的是 `@x ...`，判成"不是特权命令"→ 放行；引擎剥掉 `@x ` 之后按管理员命令
    执行。审查者实测（真 MailChannel + 真引擎）：伪造主人地址的来信靠这个
    **以超管身份执行 `/admin relay`**，两步之后 `runtime.serve` 会真的把攻击者给的
    内容发到任意群 —— 而 SMTP 的 From 可以伪造，这正是 `mail_channel` 自己点名的威胁模型。

    所以**两边必须用同一个函数剥**，不许各写一套正则。
    """

    if not isinstance(text, str):
        return ""
    return _LEADING_MENTION.sub("", text, count=1)


def parse_admin_command(text: str) -> AdminCommand | None:
    """只识别明确的管理命令，不把普通聊天内容当作控制请求。

    命令一律以 `/admin` 开头（旧的 `#bot` 前缀已移除）。这里**只解析、不鉴权**：
    权限与生效范围由 DialogueEngine 判定。
    """

    if not isinstance(text, str):
        return None
    text = strip_leading_mentions(text)
    relay_name = _RELAY_NAME_PATTERN.match(text)
    if relay_name:
        target_kind = relay_name.group("target_kind").casefold()
        target_kind = "group_name" if target_kind in {"group_name", "groupname", "群名"} else "nickname"
        return AdminCommand(
            AdminCommandKind.RELAY,
            target_kind=target_kind,
            target_query=relay_name.group("target_query").strip(),
            content=relay_name.group("content").strip(),
        )
    relay_name_simple = _RELAY_NAME_SIMPLE_PATTERN.match(text)
    if relay_name_simple:
        target_kind = relay_name_simple.group("target_kind").casefold()
        target_kind = "group_name" if target_kind in {"group_name", "groupname", "群名"} else "nickname"
        return AdminCommand(
            AdminCommandKind.RELAY,
            target_kind=target_kind,
            target_query=relay_name_simple.group("target_query").strip(),
            content=relay_name_simple.group("content").strip(),
        )
    relay = _RELAY_PATTERN.match(text)
    if relay:
        target_kind = relay.group("target_kind").casefold()
        target_kind = "group" if target_kind in {"group", "群", "群聊", "群组"} else "user"
        return AdminCommand(
            AdminCommandKind.RELAY,
            group_id=relay.group("target_id"),
            target_kind=target_kind,
            content=relay.group("content").strip(),
        )
    selection = _SELECT_PATTERN.match(text)
    if selection:
        return AdminCommand(
            AdminCommandKind.SELECT,
            token=selection.group("token"),
            index=int(selection.group("index")),
        )
    token_command = _TOKEN_PATTERN.match(text)
    if token_command:
        action = token_command.group("action").casefold()
        kind = AdminCommandKind.CONFIRM if action in {"confirm", "确认"} else AdminCommandKind.CANCEL
        return AdminCommand(kind, token=token_command.group("token"))
    echo = _ECHO_PATTERN.match(text)
    if echo:
        return AdminCommand(AdminCommandKind.ECHO, content=echo.group("content"))
    match = _COMMAND_PATTERN.match(text)
    if not match:
        return None
    action = match.group("action").casefold()
    aliases = {
        "enable": AdminCommandKind.ENABLE,
        "开启": AdminCommandKind.ENABLE,
        "disable": AdminCommandKind.DISABLE,
        "关闭": AdminCommandKind.DISABLE,
        "status": AdminCommandKind.STATUS,
        "状态": AdminCommandKind.STATUS,
        "clear": AdminCommandKind.CLEAR,
        "清理": AdminCommandKind.CLEAR,
        "help": AdminCommandKind.HELP,
        "帮助": AdminCommandKind.HELP,
        "relay": AdminCommandKind.RELAY,
    }
    kind = aliases.get(action)
    if kind is None:
        return None
    group_id = match.group("group_id")
    # enable / disable / clear **只作用于发命令的这个群**，群号参数已经取消：
    # 管理员管的是自己所在的群，点别人的群号没有任何意义，只是多一处出错的地方。
    # 旧写法（带群号）仍然解析得出来——交给上层回一句"写法变了"，
    # 不让它掉进普通聊天里被模型接一句莫名其妙的话。
    if kind in {AdminCommandKind.STATUS, AdminCommandKind.HELP} and group_id:
        return None
    return AdminCommand(kind, group_id)
