// Read-only view models for the 「账号」 page, built on the panel's existing HTTP routes.
//
// Route → field map (verified against `src/qq_roleplay_bot/plugins/webui/webui_panel.py` and by
// hitting the live panel on 127.0.0.1:8790 with its Bearer token):
//
//   GET /api/overview  → data.health.{bot_live, engine_attached, panel_port, bot_port,
//                        panel_uptime_seconds, data_dir, python}
//                        data.access.{mode, remote, sessions}
//   GET /api/self      → data.{available, self_id}                    ← 「她的号」
//   GET /api/model     → data.{available, providers[], bindings[]}     ← 「供应商与模型」
//   GET /api/plugins   → data.plugins[{name, title}]                   ← 「插件」那一组
//
// Nothing is invented: when a seam is missing the route itself says `available: false` (that is
// what `/api/model` returns on this machine today) and the page repeats it instead of showing an
// empty table that would read as "nothing is configured".

import kotlinx.coroutines.async
import kotlinx.coroutines.coroutineScope

/** Base URL of the panel API. Empty = same origin, which is how `/v2/` is meant to be served. */
fun apiBase(): String = queryParam("api").trimEnd('/')

private fun unwrap(body: String): Map<String, Any?>? {
    val root = runCatching { parseJson(body) }.getOrNull().asObj() ?: return null
    // `_envelope(...)` wraps every payload in {ok, data} (webui_panel.json_response).
    val data = root["data"].asObj()
    if (data != null) return data
    return if (root.containsKey("ok")) null else root
}

private suspend fun getData(path: String, token: String): Map<String, Any?>? {
    val url = apiBase() + path
    return when (val result = httpGetText(url, token)) {
        is HttpResult.Ok -> unwrap(result.body)
        is HttpResult.Failed -> throw ApiFailure(result.reason)
    }
}

class ApiFailure(val reason: String) : Exception(reason)

/** One row on the 「账号」 page: a label, a value, and optionally a quiet explanation. */
data class Detail(val label: String, val value: String, val note: String = "")

data class ModelProviderRow(
    val name: String,
    val baseUrl: String,
    val keyCount: Int,
    val models: List<String>,
    val usedBy: List<String>,
)

data class ModelBindingRow(val label: String, val provider: String, val model: String, val layer: String)

data class AccountData(
    val selfId: String,
    val selfAvailable: Boolean,
    val botLive: Boolean,
    val engineAttached: Boolean,
    val accessMode: String,
    val remote: Boolean,
    val sessions: Int,
    val panelPort: Int,
    val botPort: Int,
    val uptimeSeconds: Double,
    val providers: List<ModelProviderRow>,
    val bindings: List<ModelBindingRow>,
    val modelAvailable: Boolean,
)

/** Translation of the `access.mode` value the panel reports (`local` / `remote`). */
private fun accessModeLabel(mode: String, remote: Boolean): String = when (mode) {
    "local" -> "本机（Bearer token）"
    "remote" -> "远程（口令 + 会话 cookie）"
    else -> if (remote) "远程" else if (mode.isBlank()) "未知" else mode
}

private fun humanDuration(seconds: Double): String {
    val total = seconds.toLong()
    if (total <= 0) return "—"
    val days = total / 86_400
    val hours = (total % 86_400) / 3_600
    val minutes = (total % 3_600) / 60
    return when {
        days > 0 -> "${days}天${hours}小时"
        hours > 0 -> "${hours}小时${minutes}分"
        minutes > 0 -> "${minutes}分"
        else -> "${total}秒"
    }
}

private fun rowsOfModel(model: Map<String, Any?>?, available: Boolean): Pair<List<ModelProviderRow>, List<ModelBindingRow>> {
    if (!available || model == null) return emptyList<ModelProviderRow>() to emptyList()
    val providers = model["providers"].asList().mapNotNull { raw ->
        val obj = raw.asObj() ?: return@mapNotNull null
        ModelProviderRow(
            name = raw.string("name"),
            baseUrl = raw.string("base_url"),
            keyCount = raw.number("key_count").toInt(),
            models = raw.stringList("models"),
            usedBy = raw.stringList("used_by"),
        ).takeIf { it.name.isNotBlank() }
    }
    val bindings = model["bindings"].asList().mapNotNull { raw ->
        val obj = raw.asObj() ?: return@mapNotNull null
        ModelBindingRow(
            label = raw.string("label").ifBlank { raw.string("task") },
            provider = raw.string("provider").ifBlank { "未绑定" },
            model = raw.string("model"),
            layer = raw.string("layer_label"),
        )
    }
    return providers to bindings
}

/**
 * `GET /api/plugins` → one rail item per plugin. The route is read-only and reports
 * `available:false` when the assembly point did not hand over the `plugins` lister, in which case
 * the caller keeps the static fallback rather than emptying the group.
 */
suspend fun loadPlugins(token: String): List<PluginEntry> {
    val data = getData("/api/plugins", token) ?: return emptyList()
    if (!data.bool("available")) return emptyList()
    return data["plugins"].asList().mapNotNull { raw ->
        val name = raw.string("name")
        if (name.isBlank()) null else PluginEntry(name, raw.string("title").ifBlank { name })
    }
}

/**
 * Loads everything the 「账号」 page shows. The three calls are independent, so they overlap; a
 * failure in one of them must not blank the others (the panel's own comment on `LEARNED_CALLS`:
 * "缺一个只坏对应的那一件事").
 */
suspend fun loadAccount(token: String): AccountData = coroutineScope {
    val overviewDeferred = async { getData("/api/overview", token) }
    val selfDeferred = async { runCatching { getData("/api/self", token) }.getOrNull() }
    val modelDeferred = async { runCatching { getData("/api/model", token) }.getOrNull() }

    val overview = overviewDeferred.await()
    val self = selfDeferred.await()
    val model = modelDeferred.await()

    val health = overview?.get("health").asObj()
    val access = overview?.get("access").asObj()
    val modelAvailable = model?.bool("available") == true
    val (providers, bindings) = rowsOfModel(model, modelAvailable)

    AccountData(
        selfId = self.string("self_id"),
        selfAvailable = self.bool("available"),
        botLive = health.bool("bot_live"),
        engineAttached = health.bool("engine_attached"),
        accessMode = accessModeLabel(access.string("mode"), access.bool("remote")),
        remote = access.bool("remote"),
        sessions = access.number("sessions").toInt(),
        panelPort = health.number("panel_port").toInt(),
        botPort = health.number("bot_port").toInt(),
        uptimeSeconds = health.number("panel_uptime_seconds"),
        providers = providers,
        bindings = bindings,
        modelAvailable = modelAvailable,
    )
}

fun accountDetails(data: AccountData): List<Detail> = listOf(
    Detail(
        label = "她的号",
        value = if (data.selfAvailable && data.selfId.isNotBlank()) data.selfId else "还没查到",
        note = if (data.selfAvailable) "" else "/api/self 没给可用值（核心那侧没缓存到号）",
    ),
    Detail(
        label = "bot 在运行",
        value = if (data.botLive) "在运行" else "没探到监听",
        note = "端口探测 bot_port=${data.botPort}；核心接缝" + if (data.engineAttached) "已接上" else "没接上",
    ),
    Detail(label = "接入模式", value = data.accessMode, note = "远程写操作需 CSRF；本机用 Bearer token"),
    Detail(label = "面板会话数", value = data.sessions.toString(), note = "远程模式下内存里的会话条数"),
    Detail(
        label = "面板运行时长",
        value = humanDuration(data.uptimeSeconds),
        note = "面板端口 ${data.panelPort}",
    ),
)
