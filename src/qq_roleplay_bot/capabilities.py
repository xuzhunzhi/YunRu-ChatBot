"""SnowLuma 能力层：194 个 OneBot action 的登记、风险分级与调用闸门。

设计目标：

1. **能力可查** —— `src/qq_roleplay_bot/data/snowluma_actions.json` 是从
   SnowLuma WebUI `/api/debug/actions` 导出的权威清单（含参数与返回说明），
   离线可读，不依赖 SnowLuma 在线。
2. **危险接口默认关闭** —— 有一部分 action 能拿到账号凭证或发送原始协议包。
   它们**不在任何允许集合里**，调用时直接抛 `CapabilityDenied`，而不是
   "记得别用"。
3. **写操作要显式授权** —— 默认只允许只读；写操作必须由调用方把 action 名
   放进 `allow_write`，避免顺手把群管理能力暴露给模型。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

CATALOG_PATH = Path(__file__).resolve().parent / "data" / "snowluma_actions.json"

# 能拿到账号凭证、解密密钥或发送原始协议包的接口。
# 这些不是"要小心使用"，而是 Stage 4 绝不暴露给对话流程的部分。
FORBIDDEN_ACTIONS = frozenset({
    "get_credentials",          # 账号凭证
    "get_cookies",              # Cookies
    "get_clientkey",            # clientkey
    "get_csrf_token",           # CSRF 令牌
    "request_decrypt_key",      # 数据库解密密钥
    "send_packet",              # 原始 SSO 包
    "set_restart",              # 重启（不支持）
    "bot_exit",                 # 退出机器人
})

# 会造成账号状态或对外可见后果的写操作，需要单独显式授权。
SENSITIVE_WRITE_ACTIONS = frozenset({
    "set_group_kick", "set_group_kick_members", "set_group_ban", "set_group_whole_ban",
    "set_group_leave", "set_group_admin", "delete_friend", "set_group_name",
    "set_qq_profile", "set_qq_avatar", "set_self_longnick", "send_qzone_msg",
    "comment_qzone", "delete_qzone_msg", "delete_group_file", "delete_group_folder",
    "clean_cache", "upload_group_file", "upload_private_file",
})

# Stage 4 对话流程实际允许的只读查询。
READ_ACTIONS = frozenset({
    # 上下文补全
    "get_group_msg_history", "get_friend_msg_history", "get_forward_msg", "get_msg",
    # 身份与群语境
    "get_group_member_list", "get_group_member_info", "get_group_info",
    "get_group_detail_info", "get_group_info_ex", "get_group_list",
    "get_stranger_info", "get_friend_list", "get_friends_with_category",
    "get_login_info", "get_status", "get_version_info",
    # 能力查询
    "can_send_image", "can_send_record", "get_group_at_all_remain",
    # 内容理解
    "ocr_image", "fetch_ptt_text",
    # 表情
    "fetch_sys_faces", "fetch_face_entity", "search_sys_faces",
    "get_msg_emoji_likes", "fetch_emoji_like", "fetch_custom_face",
    "get_collection_list", "fetch_custom_face_detail", "fetch_super_face_id",
    # 群资料类只读
    "get_group_honor_info", "get_group_shut_list", "get_essence_msg_list",
    "get_group_root_files", "get_group_files_by_folder", "get_group_file_url",
    "get_group_todo_list", "get_group_signed_list", "get_group_system_msg",
    "get_group_ignore_add_request", "get_group_ignored_notifies",
    "get_group_admin_settings", "get_group_album_list", "get_group_album_media_list",
    "_get_group_notice",
    # 其它
    "check_url_safely", "nc_get_user_status", "nc_get_packet_status", "get_rkey",
})

# Stage 4 允许的互动类写操作（低风险、可逆、不影响账号状态）。
INTERACTION_ACTIONS = frozenset({
    "set_msg_emoji_like", "set_group_reaction", "send_poke", "group_poke",
    "mark_msg_as_read", "mark_group_msg_as_read", "mark_private_msg_as_read",
    "set_essence_msg", "delete_essence_msg",
})

# Stage 4 允许的发送类写操作。
SEND_ACTIONS = frozenset({
    "send_group_msg", "send_private_msg", "send_msg",
    "send_group_forward_msg", "send_private_forward_msg", "send_forward_msg",
    "delete_msg",
})

# **群管理**（2026-09-30 用户要求"优先接入群管理功能"）。
#
# 这是一个**单独列名的用途**，不是把 `admin` 那道闸门放宽：`admin` 用途按设计
# **排除全部敏感写操作**（见下面 `is_allowed` 里那一行），因为管理命令大多是
# "改这个 bot 自己的状态"。群管理动的是**群里的人**，性质完全不同，所以：
#   - 只列这四个（都是可以在群里纠正回来的）：禁言/解禁/全体禁言/撤回一条消息；
#   - `set_group_kick` 也在里面，但执行侧额外要求"必须 @ 到人"（见 group_admin.py），
#     因为"手滑发个群号就把人踢了"是这条路上最典型的失误；
#   - **故意不含** `set_group_leave`（退群，自毁）、`set_group_admin`（给/撤真管理员，
#     那是提权）、`set_group_name`（改名）、`set_group_card`（改别人的群名片）。
#     这四个要么不可逆、要么等于发权限，要单独提需求再评估。
GROUP_MANAGE_ACTIONS = frozenset({
    "set_group_ban", "set_group_whole_ban", "delete_msg", "set_group_kick",
})

# **群主专属**（2026-09-30 用户："群主的接口应该比管理员更多来着……能适配自动审批，
# 添加管理这些吗"）。前提是实测出来的：她（QQ 900000002）在测试群 717151356
# 「Yunru Bot testing」里 role=owner，别的群都是 member（`get_group_member_info` 读的）。
#
# 群主能做的事确实比管理员多，而这五条原先**一条都不在允许集合里**（当初的判断是
# "要么不可逆、要么等于发权限，要单独提需求再评估"，现在就是那次评估的结果）。
# 单独开一个用途，`group_manage` 那份四条短名单一个字没动。
#
# **故意不含**（提问时也标了"不推荐"）：
#   - `set_group_leave`：退群；参数带上就是**解散群**，自毁且不可逆；
#   - `transfer_group` / 转让群：等于把群交出去；
#   - `set_group_remark`：只是本地备注，没有群内效果，不值得开一条写权限。
GROUP_OWNER_ACTIONS = frozenset({
    "set_group_admin",            # 设/取消 QQ 群管理员（他说的"添加管理"）
    "set_group_card",             # 改群名片
    "set_group_name",             # 改群名
    "set_group_special_title",    # 群头衔
    "_send_group_notice",         # 群公告
})

# 入群申请审批。读的那一头 `get_group_system_msg`（待处理申请，带 flag）
# **本来就在 READ_ACTIONS 里**，所以只多这一条写。
JOIN_APPROVAL_ACTIONS = frozenset({"set_group_add_request"})


class CapabilityDenied(PermissionError):
    """请求的 action 不在允许集合内。"""


@dataclass(frozen=True, slots=True)
class Capability:
    """一个 SnowLuma action 的元数据。"""

    name: str
    read_only: bool
    summary: str = ""
    params: tuple[dict[str, object], ...] = ()
    returns: str = ""

    @property
    def risk(self) -> str:
        if self.name in FORBIDDEN_ACTIONS:
            return "forbidden"
        if self.name in SENSITIVE_WRITE_ACTIONS:
            return "sensitive_write"
        if self.read_only:
            return "read"
        return "write"

    @property
    def param_names(self) -> tuple[str, ...]:
        return tuple(str(item.get("name")) for item in self.params)


def load_catalog(path: Path | None = None) -> dict[str, Capability]:
    """读取离线 capability 目录；文件缺失时返回空字典而不是抛错。"""

    target = path or CATALOG_PATH
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.warning("snowluma_catalog_missing path=%s", target.name)
        return {}
    except ValueError:
        logger.warning("snowluma_catalog_invalid_json path=%s", target.name)
        return {}
    items = raw.get("actions") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        return {}
    catalog: dict[str, Capability] = {}
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            continue
        params = item.get("params")
        catalog[item["name"]] = Capability(
            name=item["name"],
            read_only=bool(item.get("readOnly")),
            summary=str(item.get("summary") or ""),
            params=tuple(p for p in params if isinstance(p, dict)) if isinstance(params, list) else (),
            returns=str(item.get("returns") or ""),
        )
    return catalog


class CapabilityRegistry:
    """按业务用途给 action 分类，并提供参数校验与调用闸门。"""

    def __init__(self, path: Path | None = None) -> None:
        self.catalog = load_catalog(path)

    def __len__(self) -> int:
        return len(self.catalog)

    def get(self, name: str) -> Capability | None:
        return self.catalog.get(name)

    def describe(self, name: str) -> str:
        """给日志或管理员看的简短说明。"""

        item = self.get(name)
        if item is None:
            return f"{name}: 未知 action"
        return f"{name} [{item.risk}] {item.summary}".rstrip()

    # --- 调用闸门 ---------------------------------------------------------

    def is_allowed(self, name: str, *, purpose: str) -> bool:
        """判断某个用途下是否允许调用该 action。"""

        if name in FORBIDDEN_ACTIONS:
            return False
        if purpose == "read":
            return name in READ_ACTIONS
        if purpose == "interaction":
            return name in INTERACTION_ACTIONS
        if purpose == "send":
            return name in SEND_ACTIONS
        if purpose == "group_manage":
            # 群管理：**只认 GROUP_MANAGE_ACTIONS 这份短名单**，别的敏感写操作照样不批。
            return name in GROUP_MANAGE_ACTIONS
        if purpose == "group_owner":
            return name in GROUP_OWNER_ACTIONS
        if purpose == "join_approval":
            return name in JOIN_APPROVAL_ACTIONS
        if purpose == "admin":
            # 管理用途仍不允许敏感写操作与禁用集合。
            return name not in FORBIDDEN_ACTIONS and name not in SENSITIVE_WRITE_ACTIONS
        return False

    def check(self, name: str, *, purpose: str) -> Capability:
        """通过则返回元数据，否则抛 CapabilityDenied。"""

        if name in FORBIDDEN_ACTIONS:
            raise CapabilityDenied(f"{name} 属于禁用接口（可获取凭证或操作账号），Stage 4 不允许调用")
        if not self.is_allowed(name, purpose=purpose):
            raise CapabilityDenied(f"{name} 不在用途 '{purpose}' 的允许集合内")
        item = self.get(name)
        if item is None:
            # 目录缺失时不阻断已知允许集合内的调用，但仍要求名字合法。
            logger.warning("snowluma_capability_unknown name=%s", name)
            return Capability(name=name, read_only=False)
        return item

    def missing_required(self, name: str, params: dict[str, object]) -> tuple[str, ...]:
        """返回缺失的必填参数名，用于在发请求前给出清晰错误。"""

        item = self.get(name)
        if item is None:
            return ()
        missing = []
        for param in item.params:
            if param.get("required") and param.get("name") not in params:
                missing.append(str(param.get("name")))
        return tuple(missing)

    # --- 汇总 -------------------------------------------------------------

    def summary(self) -> dict[str, object]:
        counts = {"read": 0, "write": 0, "forbidden": 0, "sensitive_write": 0, "unknown": 0}
        for name in self.catalog:
            counts[self.catalog[name].risk] += 1
        return {
            "total": len(self.catalog),
            "by_risk": counts,
            "allowed_read": sorted(READ_ACTIONS),
            "allowed_interaction": sorted(INTERACTION_ACTIONS),
            "allowed_send": sorted(SEND_ACTIONS),
            "forbidden": sorted(FORBIDDEN_ACTIONS),
        }
