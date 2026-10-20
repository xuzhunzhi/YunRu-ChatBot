// The information architecture (unchanged from the old CSS/JS panel: `webui/app.js` `CARDS`).
//
// Six groups, one rail item per leaf. The 14 fixed leaves are literal; the 「插件」 group is one
// item per plugin, so its size follows the deployment (`/api/plugins` → 9 entries on this machine,
// see `pluginFallback` for the pre-login list).
//
// Icons come from `miuix-icons` (`top.yukonga.miuix.kmp.icon.extended.*`). Miuix's own `miuix-ui`
// only ships seven basic icons (ArrowRight / ArrowUpDown / Check / Close / Search / SearchCleanup /
// Sidebar), which cannot name 6 groups + 23 leaves — that is why this module is a real dependency
// and not decoration. Every icon used here is referenced through `MiuixIcons.<Name>`.

import androidx.compose.ui.graphics.vector.ImageVector
import top.yukonga.miuix.kmp.icon.MiuixIcons
import top.yukonga.miuix.kmp.icon.extended.Community
import top.yukonga.miuix.kmp.icon.extended.Contacts
import top.yukonga.miuix.kmp.icon.extended.ContactsBook
import top.yukonga.miuix.kmp.icon.extended.Favorites
import top.yukonga.miuix.kmp.icon.extended.Folder
import top.yukonga.miuix.kmp.icon.extended.Home
import top.yukonga.miuix.kmp.icon.extended.Layers
import top.yukonga.miuix.kmp.icon.extended.ListView
import top.yukonga.miuix.kmp.icon.extended.Lock
import top.yukonga.miuix.kmp.icon.extended.Messages
import top.yukonga.miuix.kmp.icon.extended.Notes
import top.yukonga.miuix.kmp.icon.extended.Phone
import top.yukonga.miuix.kmp.icon.extended.Settings
import top.yukonga.miuix.kmp.icon.extended.Tasks
import top.yukonga.miuix.kmp.icon.extended.Translate

/** A plugin as the panel reports it (`GET /api/plugins` → `{name, title}`). */
data class PluginEntry(val name: String, val title: String)

/**
 * One rail leaf. [body] is the "what this page will be" note for the pages that are still stubs —
 * the old panel's `TAB_NOTES`, kept so no page loses its meaning while it is being ported.
 *
 * [routes] are the **real** routes that page will read once it is ported. They are copied from the
 * `if path == "/api/…"` table in `src/qq_roleplay_bot/plugins/webui/webui_panel.py` (line numbers in
 * the commit message), not invented: a placeholder that promises a route the panel does not serve
 * would be worse than no placeholder at all.
 */
data class RailItem(
    val id: String,
    val label: String,
    val icon: ImageVector,
    val body: String = "",
    val routes: List<String> = emptyList(),
)

data class RailGroup(val title: String, val items: List<RailItem>)

/** The 「账号」 page is the only one wired to real data this round. */
const val PANE_ACCOUNT = "account"

/**
 * Pre-login plugin list: the nine built-in plugins `inventory()` reports on this machine
 * (`group_admin`, `join_approval`, `mail`, `outage_notice`, `remind`, `rss`, `timeout_alert`,
 * `vision`, `webui`). Replaced by the live list as soon as `/api/plugins` answers, so a plugin that
 * is installed or removed does not need a rebuild — the fallback only exists so the rail is
 * complete before the first login.
 */
val pluginFallback: List<PluginEntry> = listOf(
    PluginEntry("group_admin", "群管理"),
    PluginEntry("join_approval", "入群申请"),
    PluginEntry("mail", "邮件"),
    PluginEntry("outage_notice", "掉线通知"),
    PluginEntry("remind", "到点提醒"),
    PluginEntry("rss", "订阅推送"),
    PluginEntry("timeout_alert", "超时通知"),
    PluginEntry("vision", "识图"),
    PluginEntry("webui", "控制面板"),
)

fun railGroups(plugins: List<PluginEntry>): List<RailGroup> {
    val pluginItems = plugins.ifEmpty { pluginFallback }.map { entry ->
        RailItem(
            id = "plugin:${entry.name}",
            label = entry.title.ifBlank { entry.name },
            icon = MiuixIcons.Layers,
            body = "插件自报的那一页（GET /api/plugin/${entry.name}）：只读，按插件交的界面描述符画。",
            routes = listOf("/api/plugin/${entry.name}"),
        )
    }
    return listOf(
        RailGroup(
            title = "账号与设备",
            items = listOf(
                RailItem(
                    PANE_ACCOUNT, "账号", MiuixIcons.Contacts,
                    routes = listOf("/api/self", "/api/overview", "/api/model"),
                ),
                RailItem(
                    "device", "设备", MiuixIcons.Phone,
                    "设备：本机（地址 / Python / 数据目录）、与本体连接（端口 / 心跳 / 活快照）、占用空间（data/ 下各项）。",
                    routes = listOf("/api/overview", "/api/state", "/api/storage"),
                ),
            ),
        ),
        RailGroup(
            title = "聊天",
            items = listOf(
                RailItem("overview", "总览", MiuixIcons.Home,
                    "她现在在不在跑、在哪些群说话、这次重启之后干了多少活。",
                    routes = listOf("/api/overview", "/api/groups", "/api/usage")),
                RailItem("group", "群聊", MiuixIcons.Community,
                    "按群看会话：群列表、群记忆开关、发一句话。",
                    routes = listOf("/api/groups", "/api/session/messages", "/api/session/clear")),
                RailItem("private", "私聊", MiuixIcons.Messages,
                    "私聊会话与最近消息。",
                    routes = listOf("/api/history", "/api/session/messages")),
            ),
        ),
        RailGroup(
            title = "记忆",
            items = listOf(
                RailItem("memory", "记忆", MiuixIcons.Notes,
                    "旧事（排除画像）：按词检索、按群过滤、清理。",
                    routes = listOf("/api/memory/overview", "/api/memory/groups", "/api/memory/purge")),
                RailItem("memory_profile", "人物画像", MiuixIcons.ContactsBook,
                    "一条人一条画像（kind=profile），与「记忆」同级。",
                    routes = listOf("/api/memory/affinity", "/api/memory/problem")),
            ),
        ),
        RailGroup(
            title = "知识库",
            items = listOf(
                RailItem("knowledge", "知识库", MiuixIcons.Folder,
                    "操作者知识块（OperatorKnowledge 面板面）。",
                    routes = listOf("/api/knowledge/overview", "/api/knowledge/chunks", "/api/knowledge/operator")),
                RailItem("knowledge_quotes", "金句", MiuixIcons.Favorites,
                    "她从群里学来的金句（表情含义表 + 笔记），同一页里还有「审证据」子视图。",
                    routes = listOf("/api/knowledge/quotes", "/api/knowledge/quotes/evidence")),
                RailItem("knowledge_slang", "黑话", MiuixIcons.Translate,
                    "黑话词条表：看、改、删、标错。",
                    routes = listOf("/api/knowledge/slang")),
            ),
        ),
        RailGroup(title = "插件", items = pluginItems),
        RailGroup(
            title = "系统",
            items = listOf(
                RailItem("logs", "日志", MiuixIcons.ListView,
                    "日志 / 操作记录两块：判定 / 回复 / 记忆 / 规则 的最近 I/O ＋ 面板自己的操作审计。",
                    routes = listOf("/api/logs", "/api/notice")),
                RailItem("settings", "设置", MiuixIcons.Settings,
                    "六套 prompt、八个 agent 开关、供应商（兼容档）。",
                    routes = listOf("/api/settings", "/api/prompts", "/api/prompts/versions")),
                RailItem("model", "模型与凭据", MiuixIcons.Lock,
                    "密钥一行一把、每一路按「供应商 → key → 模型」绑。",
                    routes = listOf("/api/model", "/api/model/add", "/api/model/bind")),
                RailItem("actions", "动作", MiuixIcons.Tasks,
                    "清短期会话、调关系、重启 bot。",
                    routes = listOf("/api/action", "/api/control/restart", "/api/approval")),
            ),
        ),
    )
}
