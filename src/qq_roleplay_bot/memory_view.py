"""超管记忆查看：把记忆库渲染成有界的、可在群里发送的文本。

设计约束：

1. **只读** —— 只调用 MemoryStore 上的只读查询，不改动任何记录；
2. **有界** —— 群消息不能太长，所有输出都按字符数截断并给出"还有多少没显示"；
3. **脱敏** —— 不输出 `source_event_ids` 这类内部标识，也不输出完整证据链；
4. **按需分页** —— `overview` 只给计数与摘要，`records` / `inbox` 给列表，
   一次最多显示固定条数。
"""
from __future__ import annotations

from dataclasses import dataclass

# 群消息安全长度：QQ 单条上限远大于此，但太长没人看，且容易触发风控。
MAX_REPLY_CHARS = 900
MAX_LIST_ITEMS = 8
CONTENT_PREVIEW = 70
# 归档的预览比普通记录长：它的用途是"看清当初删了什么"，太短就看不出该不该恢复。
ARCHIVE_PREVIEW = 110


@dataclass(frozen=True, slots=True)
class MemoryView:
    """一次记忆查看的结果。"""

    title: str
    body: str

    def render(self) -> str:
        text = f"{self.title}\n{self.body}" if self.body else self.title
        return text[:MAX_REPLY_CHARS]


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def build_overview(counts: dict[str, int]) -> MemoryView:
    """概览：各类计数，不含正文。"""

    lines = ["记忆库概览"]
    labels = (
        ("records", "长期记忆"),
        ("active_records", "  · 生效中"),
        ("inbox", "待处理材料"),
        ("audit", "维护操作"),
        ("tombstones", "墓碑(已删)"),
        ("archive", "归档(可恢复)"),
        ("receipts", "已处理回执"),
    )
    for key, label in labels:
        if key in counts:
            lines.append(f"{label}：{counts[key]}")
    lines.append("")
    lines.append("查看明细：/super memory records | inbox | audit | archive")
    return MemoryView("", "\n".join(lines))


def build_records_view(records: list[dict[str, object]], *, total: int) -> MemoryView:
    """记忆列表：key、类型、层级、置信度、状态与内容预览（两层混排）。"""

    if not records:
        return MemoryView("长期记忆", "（暂无记录）")
    lines = [f"长期记忆（共 {total} 条，显示 {len(records)} 条）"]
    for item in records[:MAX_LIST_ITEMS]:
        scope = str(item.get("scope_type", "?"))
        kind = str(item.get("kind", "?"))
        key = str(item.get("normalized_key", "?"))
        conf = item.get("confidence", 0)
        status = str(item.get("status", "?"))
        # 2026-09-28 起记忆分两层：长期是强化过的，中短期是还没被再确认的。
        tier = "长期" if item.get("tier") == "long" else "中短期"
        content = _clip(str(item.get("content", "")), CONTENT_PREVIEW)
        mark = "" if status == "active" else f"[{status}] "
        lines.append(f"· {mark}{key} [{scope}/{kind}/{tier}] conf={conf}")
        lines.append(f"  {content}")
    hidden = total - min(len(records), MAX_LIST_ITEMS)
    if hidden > 0:
        lines.append(f"… 还有 {hidden} 条未显示")
    return MemoryView("", "\n".join(lines))


def build_archive_view(items: list[dict[str, object]], *, total: int) -> MemoryView:
    """已删除记忆的归档：显示完整内容，供人工判断是否需要恢复。

    与 records 视图刻意不同：这里**不截断到很短的预览**（仍受单条上限约束），
    因为归档的用途就是"看清当初删了什么"，预览太短等于看不出该不该恢复。
    """

    if not items:
        return MemoryView("归档（已删除的记忆）", "（归档为空）")
    lines = [f"归档（共 {total} 条，显示 {len(items)} 条，保留 30 天）"]
    for item in items[:MAX_LIST_ITEMS]:
        key = str(item.get("normalized_key", "?"))
        scope = str(item.get("scope_type", "?"))
        reason = str(item.get("delete_reason", "?"))
        content = _clip(str(item.get("content", "")), ARCHIVE_PREVIEW)
        lines.append(f"· {key} [{scope}] 原因={reason}")
        lines.append(f"  {content}")
    hidden = total - min(len(items), MAX_LIST_ITEMS)
    if hidden > 0:
        lines.append(f"… 还有 {hidden} 条未显示")
    lines.append("")
    lines.append("恢复需人工操作数据库，不提供命令。")
    return MemoryView("", "\n".join(lines))


def build_inbox_view(items: list[dict[str, object]], *, total: int) -> MemoryView:
    """待处理材料：发言者与内容预览。不显示事件 id。"""

    if not items:
        return MemoryView("待处理材料", "（队列为空）")
    lines = [f"待处理材料（共 {total} 条，显示 {len(items)} 条）"]
    for item in items[:MAX_LIST_ITEMS]:
        speaker = str(item.get("speaker", "?"))
        who = "云茹" if speaker == "yunru" else f"user:{item.get('user_id', '?')}"
        lines.append(f"· {who}：{_clip(str(item.get('text', '')), CONTENT_PREVIEW)}")
    hidden = total - min(len(items), MAX_LIST_ITEMS)
    if hidden > 0:
        lines.append(f"… 还有 {hidden} 条未显示")
    return MemoryView("", "\n".join(lines))


def build_audit_view(rows: list[dict[str, object]], *, total: int) -> MemoryView:
    """维护 Agent 的操作流水。"""

    if not rows:
        return MemoryView("维护操作", "（暂无记录）")
    lines = [f"维护操作（共 {total} 条，显示 {len(rows)} 条，最近在前）"]
    for item in rows[:MAX_LIST_ITEMS]:
        op = str(item.get("op", "?"))
        scope = str(item.get("scope_type") or "-")
        lines.append(f"· {op} scope={scope}")
    hidden = total - min(len(rows), MAX_LIST_ITEMS)
    if hidden > 0:
        lines.append(f"… 还有 {hidden} 条未显示")
    return MemoryView("", "\n".join(lines))


SUPER_HELP = (
    "超管命令（仅超级管理员）\n"
    "记忆查看\n"
    "· /super memory 记忆库概览（计数）\n"
    "· /super memory records 长期记忆明细\n"
    "· /super memory inbox 待处理材料\n"
    "· /super memory audit 维护操作流水\n"
    "· /super memory archive 已删除记忆的归档（保留 30 天）\n"
    "诊断\n"
    "· /super processes 本机进程列表（默认只列有界面的程序）\n"
    "· /super processes memory|cpu|gpu|gpu-memory 内存 / 5 秒 CPU / GPU 3D / 显存 前 8\n"
    "· /super lan 网卡流量（滚动 10 分钟，上传下载分别前 5）\n"
    "· /super fan 风扇转速（本机没暴露会直说）\n"
    "· /super status 本次重启后的概览（运行时长、内存、计数、日志）\n"
    "· /super quote 看她学出来的表情含义表与老习惯；后面跟「<表情id> <方向> [说明]」可人工纠正\n"
    "· /super apicheck 按 agent 对账：回复/判定/记忆/风格审核/识图/写信 的命中率与余额\n"
    "· /super restart 重启 bot（几秒后回来；配置改动要靠它生效）\n"
    "人员\n"
    "· /super addadmin @某人 把被 @ 的人加为本群的管理员（也接受显式群号）\n"
    "· /super deladmin @某人 撤掉某人在本群的管理员权限\n"
    "· /super admin list 查看管理员：超管全局，管理员按群列\n"
    "· /super affinity @某人 看这个人跟她的关系现状（加 reset 按回默认档）\n"
    "· /super profile @某人 看她对这个人的完整印象（人物画像），不带人就看你自己的\n"
    "群管理（在本群发，只有超管能用）\n"
    "· /super kick @某人 移出群聊（必须 @ 到人）\n"
    "· /super ban @某人 [分钟] 禁言（默认 10 分钟，最长 30 天）；unban 解除\n"
    "· /super mute / unmute 开启 / 关闭全员禁言\n"
    "· /super recall 引用一条消息后发这句，撤回那一条\n"
    "· /super permit 引用一条被拒的 admin 命令，替他执行那一条（一次）\n"
    "群主专属（她自己在那个群是群主才能做；在群里发）\n"
    "· /super qqadmin @某人 设为 QQ 群管理员；unqqadmin 取消\n"
    "· /super card @某人 新名片 改群名片（留空=清除）\n"
    "· /super groupname 新群名 改群名\n"
    "· /super title @某人 头衔 设置群头衔；自己给自己设用 /title 头衔，不需要超管）\n"
    "· /super notice 正文 发群公告\n"
    "· /super help 显示本帮助\n"
    "\n"
    "管理命令仍可用：/admin help\n"
    "超管命令是全局的：在哪个群、私聊都能用，和群的启停状态无关。\n"
)

ADMIN_HELP = (
    "管理员命令菜单\n"
    "群聊管理\n"
    "· /admin enable 开启本群对话 / /admin disable 关闭本群对话\n"
    "· /admin status 查看当前启用群\n"
    "· /admin clear 清理本群的短期对话状态\n"
    "· /admin echo 文本 把这段文本原样发出来（用于公告/校对，内容不会再当命令解析）\n"
    "转告\n"
    "· /admin relay group 群号 内容\n"
    "· /admin relay user QQ号 内容\n"
    "· /admin relay group_name 群名 | 内容\n"
    "· /admin relay nickname 昵称 | 内容\n"
    "· /admin select 确认码 序号 / /admin confirm 确认码 / /admin cancel 确认码\n"
    "· /admin help 显示这份菜单（谁都能看）\n"
    "\n"
    "执行这些命令需要管理权限：你得是本群的管理员（超管不受限）；私聊里不生效。\n"
    "启停与清理只看你发命令的那个群，不带群号。\n"
    "前缀固定是 /admin。\n"
)

# 没有管理权限的人**也**能调出上面这份菜单（用户 2026-09-28 要求：
# "普通成员可以呼叫 /admin help 调起 help 菜单"），多出来的这一段告诉他
# 菜单不是画饼：想用哪一条，让超管引用着放行那一条就行。
ADMIN_HELP_GUEST_NOTE = (
    "\n"
    "你现在没有管理权限，所以上面这些命令还不能执行。\n"
    "想用其中某一条：先把它发出来（会被记下 5 分钟），\n"
    "再请超管引用你那条消息发一句 /super permit——他会替你执行这一条，只执行一次。\n"
)
