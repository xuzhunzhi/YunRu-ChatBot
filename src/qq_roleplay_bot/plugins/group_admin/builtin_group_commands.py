"""Stage 4 的群管理 / 群主命令，做成**插件**（2026-09-30 用户要求）。

由来：用户指出"我之前说过 stage4 的内容都用插件实现，你好像直接加到 super 的底层里去了"。
原来的做法是往核心 `stage3_main.py` 里加 `/super kick`、`/super qqadmin` 的解析与分发
（跟更早的 `/super kick|ban` 一样）。这次按用户的规则搬出来：

- **解析与意图在插件**：插件认出命令、把文字解析成 `ActionRequest`；
- **执行在核心**：权限档位（`min_level = "super"`）、她是不是群主、动作白名单、
  目标限制、审计——全部留在核心，`group_admin.py` / `group_owner.py` 一行没搬；
- **插件依然拿不到 `transport`**，也拿不到 `admin_user_ids`：它只说"想做什么"。

这是 `docs/ADD_A_COMMAND.md` 里"需要权限的命令"那条的新答案：以前是"不要用插件做需要
身份判断的命令"，现在插件可以**声明档位**，判定仍在核心。想读权限表？插件手上没有。
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

from ...command_plugins import ActionRequest

if TYPE_CHECKING:
    # **只做类型标注**：`transport.IncomingMessage` 是消息的**数据形状**，
    # 插件要能声明"我收到的是这样一条消息"。标成 TYPE_CHECKING 之后，
    # 这个模块在运行时**不 import 核心的任何模块**（`command_plugins` 是插件契约模块）。
    from ...transport import IncomingMessage

# --- 群管理（禁言/踢/撤回/全员禁言）-----------------------------------------
# 作用范围只有发命令的那个群：不接受群号参数，也就没有"敲个号去管别的群"这条路。
GROUP_PATTERN = re.compile(
    r"^[/#]super\s+(?P<action>kick|踢|踢出|移出|ban|禁言|unban|解禁|解除禁言|"
    r"mute|全员禁言|unmute|解除全员禁言|recall|撤回|撤销)"
    r"(?:\s+(?P<rest>.*?))?\s*$",
    re.IGNORECASE,
)
GROUP_ACTIONS = {
    "kick": "kick", "踢": "kick", "踢出": "kick", "移出": "kick",
    "ban": "ban", "禁言": "ban",
    "unban": "unban", "解禁": "unban", "解除禁言": "unban",
    "mute": "mute", "全员禁言": "mute",
    "unmute": "unmute", "解除全员禁言": "unmute",
    "recall": "recall", "撤回": "recall", "撤销": "recall",
}

# --- 群主专属（设管理员/群名片/群名/头衔/群公告）-----------------------------
# 单独一条模式，不塞进群管理那条：群管理的 `rest` 是"@ 段 + 数字"的宽松扫描，
# 而这一族要的是**一段自由文本**（群名/公告/头衔），混在一起必然互相吃掉。
#
# **必须 `re.DOTALL`**（2026-09-30 真机踩过）：群公告正文是多行的，而 `.*?` 默认
# 跨不过换行 → `fullmatch` 失败 → 这条命令没被插件认领，掉进对话路径又被安全过滤
# 拦下（那条拦截的回复是空串），用户看到的就是"发公告发不出去、还没任何反应"。
OWNER_PATTERN = re.compile(
    r"^[/#]super\s+(?P<action>qqadmin|setadmin|设管理|设置管理|unqqadmin|unsetadmin|取消管理|"
    r"card|名片|群名片|groupname|群名|改群名|title|头衔|群头衔|notice|公告|群公告)"
    r"(?:\s+(?P<rest>.*?))?\s*$",
    re.IGNORECASE | re.DOTALL,
)
OWNER_ACTIONS = {
    "qqadmin": "qqadmin", "setadmin": "qqadmin", "设管理": "qqadmin", "设置管理": "qqadmin",
    "unqqadmin": "unqqadmin", "unsetadmin": "unqqadmin", "取消管理": "unqqadmin",
    "card": "card", "名片": "card", "群名片": "card",
    "groupname": "groupname", "群名": "groupname", "改群名": "groupname",
    "title": "title", "头衔": "title", "群头衔": "title",
    "notice": "notice", "公告": "notice", "群公告": "notice",
}

# 公开的"给自己设头衔"：`/title 龙王`、`#title 龙王`、`/yunru title 龙王`。
# **裸 `title 龙王` 不算**——跟 `/help` 那条同一个理由（"title 是什么意思"是聊天）。
TITLE_PATTERN = re.compile(
    r"^[/#]\s*title\b\s*(?P<title>.*)$"
    r"|^[/#]?\s*yunru\s+title\b\s*(?P<title2>.*)$",
    re.IGNORECASE | re.DOTALL,
)


def parse_group_action(text: str) -> tuple[str, str, str] | None:
    """`/super <群管理动作> ...` → `(动作, 目标QQ, 分钟数)`；不是群管理命令就 None。

    参数顺序不固定（`ban @某人 30`、`ban 123456789 30`、`ban 30` 都收）：
    QQ 号按"5~12 位纯数字"认，剩下的纯数字里最后一个当年限。@ 段本身不在正文里，
    真正认人靠 `mentioned_user_ids`（调用方优先用它）。
    """

    match = GROUP_PATTERN.fullmatch((text or "").strip())
    if match is None:
        return None
    kind = GROUP_ACTIONS.get((match.group("action") or "").casefold(), "")
    if not kind:
        return None
    tokens = [token for token in re.split(r"\s+", (match.group("rest") or "").strip()) if token]
    target = ""
    for token in tokens:
        if token.isdigit() and 5 <= len(token) <= 12:
            target = token
            break
    duration = ""
    for token in reversed(tokens):
        if token.isdigit() and token != target and len(token) <= 6:
            duration = token
            break
    return kind, target, duration


def parse_owner_action(text: str) -> tuple[str, str] | None:
    """`/super qqadmin @某人` / `/super notice 正文` → `(动作, 文本)`；不是就 None。

    返回的"文本"里还带着 @ 段之外的内容；真正的目标优先取 `mentioned_user_ids`
    （@ 段不在正文里）。**@ 之后的文字算正文**，所以调用方要先剥掉 @ 段留下的空白。
    """

    match = OWNER_PATTERN.fullmatch((text or "").strip())
    if match is None:
        return None
    kind = OWNER_ACTIONS.get((match.group("action") or "").casefold(), "")
    if not kind:
        return None
    return kind, (match.group("rest") or "").strip()


def _strip_target(text: str, target: str) -> str:
    """把正文开头的目标 QQ 号（如果有）去掉，剩下的才是正文。"""

    if not target:
        return text
    stripped = re.sub(r"^[\s,，:：]*" + re.escape(target) + r"[\s,，:：]*", "", text)
    return stripped.strip()


class GroupManageCommand:
    """`/super kick|ban|unban|mute|unmute|recall`：禁言/踢人/撤回/全员禁言。

    只做两件事：认出这条命令、把目标与参数解析清楚。**执行在核心**——
    它在 `group_admin.py` 里有四道护栏（动作白名单、目标限制、参数上限、全程审计），
    插件这边既不判身份也不碰 transport，连"她能不能做"都不知道。
    """

    name = "group_manage"
    min_level = "super"

    def match(self, message: IncomingMessage) -> bool:
        return parse_group_action(message.text or "") is not None

    async def handle(self, message: IncomingMessage):
        parsed = parse_group_action(message.text or "")
        if parsed is None:  # pragma: no cover - match 已经保证解析得出来
            return None
        kind, typed_target, duration = parsed
        mentioned = tuple(qq for qq in message.mentioned_user_ids if qq.isdigit())
        return ActionRequest(
            group="group_admin",
            kind=kind,
            target_id=mentioned[0] if mentioned else typed_target,
            text=duration,
            mentioned=bool(mentioned),
            message_id=message.reply_to_message_id or "",
        )

    def help_text(self) -> str:
        return ""


class TitleCommand:
    """`/title 头衔`：**自己给自己设群头衔**（2026-09-30 用户："头衔那个不用 /super 权限，
    改成 title+头衔即可，自己给自己申请"）。

    和 `/super title @某人 头衔` 的区别只有一个：这条**不要求权限**（`min_level="public"`），
    但它只能改发起人自己的头衔——目标写死成 `message.user_id`，插件给不了别人。
    能不能真改，取决于她自己在那个群是不是群主（核心现查），不是群主就回一句"做不了"。
    """

    name = "title"
    min_level = "public"

    def match(self, message: IncomingMessage) -> bool:
        return parse_title_command(message.text or "") is not None

    async def handle(self, message: IncomingMessage):
        title = parse_title_command(message.text or "")
        if title is None:  # pragma: no cover - match 已经保证解析得出来
            return None
        if not title.strip():
            return "用法：/title 头衔（只有我在这个群是群主时才改得动）"
        return ActionRequest(
            group="group_owner",
            kind="title",
            target_id=message.user_id,
            text=title,
            mentioned=False,   # 不是 @ 出来的：目标就是他自己
        )

    def help_text(self) -> str:
        return "群头衔\n· /title 头衔 → 给自己设一个群头衔（要在她当群主的群）。"


def parse_title_command(text: str) -> str | None:
    """`/title 龙王`、`#title 龙王`、`/yunru title 龙王` → `"龙王"`；不是这条命令就 None。

    裸 `title 龙王` **不算**（跟帮助那条同一个道理：认了会把日常聊天吞掉，
    比如有人问"title 是什么意思"）。
    """

    match = TITLE_PATTERN.fullmatch((text or "").strip())
    if match is None:
        return None
    return (match.group("title") or match.group("title2") or "").strip()


class GroupOwnerCommand:
    """`/super qqadmin|card|groupname|title|notice`：群主专属动作。

    前提是**她自己在那个群确实是群主**——但那个判断在核心（角色由核心递给执行端，
    见 `group_owner.execute` 的 `roles`），
    插件只把意图交出去。所以她在这儿看起来"什么都能做"，实际上核心会拦。
    """

    name = "group_owner"
    min_level = "super"

    def match(self, message: IncomingMessage) -> bool:
        return parse_owner_action(message.text or "") is not None

    async def handle(self, message: IncomingMessage):
        parsed = parse_owner_action(message.text or "")
        if parsed is None:  # pragma: no cover - match 已经保证解析得出来
            return None
        kind, rest = parsed
        mentioned = tuple(qq for qq in message.mentioned_user_ids if qq.isdigit())
        target = ""
        text = rest
        if mentioned:
            target = mentioned[0]
            # 正文里 @ 段本身不出现；但有人会写 `/super card @某人 新名片`，
            # 去掉开头的 QQ 号/空白后剩下的才是正文。
            text = _strip_target(text, target)
        elif kind in {"qqadmin", "unqqadmin", "card", "title"}:
            # 这几个必须有目标：没 @ 就看他有没有直接写 QQ 号。
            found = re.findall(r"(?<!\d)\d{5,12}(?!\d)", rest)
            if found:
                target = found[0]
                text = rest.replace(found[0], "", 1).strip(" \t,，:：")
        return ActionRequest(
            group="group_owner",
            kind=kind,
            target_id=target,
            text=text,
            mentioned=bool(mentioned),
        )

    def help_text(self) -> str:
        return ""
