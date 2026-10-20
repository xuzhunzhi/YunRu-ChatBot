// 「账号」 — the only page wired to real data this round.
//
// Token: this uses the same mechanism the old panel already uses — a token field, remembered in
// `localStorage["yunru_panel_token"]` (see `webui/app.js`, `state.token`). We do NOT ask the backend
// for anything new: local mode authenticates with `Authorization: Bearer <data/webui_token>`
// (`webui_access.WebAccess.check_bearer`), and `?token=…` in the URL prefills the field so a
// bookmark can carry it.
//
// Everything rendered below comes from a route this page actually called; when a route answers
// `available: false` the page says so instead of drawing an empty table (that is `/api/model` on
// this machine today — the `model_providers` seam is not injected in the deployment).
//
// 版式按 Miuix 样例（`assets/002.webp` 第一联）：分组小标题在卡外 → 一张纯白卡装若干行 →
// 行与行之间**不画线**，只靠 BasicComponent 的 56dp 行高 + 10dp 竖向内边距拉开。
// 上一版每行之间那条 0.75dp 的 HorizontalDivider 去掉了：样例的卡片里没有任何分割线，
// 线条 + 卡 + 行三层叠起来就是"加厚"。

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.input.TextFieldValue
import androidx.compose.ui.unit.dp
import top.yukonga.miuix.kmp.basic.Button
import top.yukonga.miuix.kmp.basic.ButtonDefaults
import top.yukonga.miuix.kmp.basic.Text
import top.yukonga.miuix.kmp.basic.TextField
import top.yukonga.miuix.kmp.icon.MiuixIcons
import top.yukonga.miuix.kmp.icon.extended.Contacts
import top.yukonga.miuix.kmp.icon.extended.Home
import top.yukonga.miuix.kmp.icon.extended.Layers
import top.yukonga.miuix.kmp.icon.extended.Lock
import top.yukonga.miuix.kmp.icon.extended.Messages
import top.yukonga.miuix.kmp.icon.extended.Tasks
import top.yukonga.miuix.kmp.theme.MiuixTheme

private sealed interface AccountState {
    data object Idle : AccountState
    data object Loading : AccountState
    data class Ready(val data: AccountData) : AccountState
    data class Failed(val reason: String) : AccountState
}

/**
 * One icon per row of `accountDetails(...)`, same order. Purely presentational: the labels/values
 * still come from `Api.kt`, so a row that changes its text cannot silently get the wrong icon —
 * if the list grows, `getOrNull` leaves the new row without a tile instead of mislabelling it.
 */
private val RowIcons = listOf(
    MiuixIcons.Contacts,
    MiuixIcons.Home,
    MiuixIcons.Lock,
    MiuixIcons.Messages,
    MiuixIcons.Tasks,
)

@Composable
fun AccountPane(token: String, onTokenChange: (String) -> Unit, accent: Accent) {
    var state by remember { mutableStateOf<AccountState>(AccountState.Idle) }
    var reload by remember { mutableIntStateOf(0) }
    // The field is a *draft*: what is typed is persisted in localStorage immediately, but a load
    // only happens on 「载入」 (or on entry, when a token was already there). Otherwise every
    // keystroke would fire three HTTP calls and the page would flicker through half-typed tokens.
    var draft by remember { mutableStateOf(TextFieldValue(token)) }
    var applied by remember { mutableStateOf(token) }

    LaunchedEffect(applied, reload) {
        if (applied.isBlank()) {
            state = AccountState.Idle
            return@LaunchedEffect
        }
        state = AccountState.Loading
        state = try {
            AccountState.Ready(loadAccount(applied))
        } catch (e: ApiFailure) {
            AccountState.Failed(e.reason)
        } catch (e: Throwable) {
            AccountState.Failed(e.message ?: "载入失败（未知错误）")
        }
    }

    Section("面板会话") {
        PanelCard {
            TextField(
                value = draft,
                onValueChange = {
                    draft = it
                    onTokenChange(it.text)
                },
                modifier = Modifier.fillMaxWidth(),
                label = "token",
                useLabelAsPlaceholder = true,
                singleLine = true,
                // Masked on purpose: the panel token is a bearer credential, and a screenshot of
                // this page is exactly how it would leak. The old panel keeps it in localStorage
                // and never prints it either; the value-based overload is used for this one call
                // because the state-based overload has no `visualTransformation`.
                visualTransformation = PasswordVisualTransformation(),
            )
            Spacer(Modifier.size(12.dp))
            // 窄卡片（310dp 内容列）里"按钮 + 说明"并排会把说明挤成两行，改成上下堆。
            BoxWithConstraints {
                val compact = maxWidth < 420.dp
                val button: @Composable () -> Unit = {
                    Button(
                        onClick = {
                            applied = draft.text.trim()
                            reload += 1
                        },
                        enabled = state !is AccountState.Loading,
                        // 主操作用 primary（样例里那个蓝勾就是 primary 的位置），默认的
                        // secondaryVariant 是灰的 —— 整页发灰有一半是它。
                        colors = ButtonDefaults.buttonColorsPrimary(),
                    ) {
                        Text(if (state is AccountState.Loading) "载入中…" else if (token.isBlank()) "载入" else "重新载入")
                    }
                }
                val caption: @Composable () -> Unit = {
                    Text(
                        text = if (token.isBlank()) "还没填 token" else "token 已存进这台浏览器的 localStorage",
                        style = MiuixTheme.textStyles.footnote2,
                        color = MiuixTheme.colorScheme.onSurfaceContainerVariant,
                    )
                }
                if (compact) {
                    Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
                        button()
                        caption()
                    }
                } else {
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        verticalAlignment = Alignment.CenterVertically,
                        horizontalArrangement = Arrangement.spacedBy(12.dp),
                    ) {
                        button()
                        caption()
                    }
                }
            }
            when (val current = state) {
                AccountState.Idle -> {
                    Spacer(Modifier.size(8.dp))
                    Hint("先填面板 token（本机是 data/webui_token 里那串），再按「载入」。")
                }
                AccountState.Loading -> {
                    Spacer(Modifier.size(8.dp))
                    Hint("正在读 /api/overview · /api/self · /api/model …")
                }
                is AccountState.Failed -> {
                    Spacer(Modifier.size(8.dp))
                    Hint("读不到：${current.reason}", isError = true)
                }
                is AccountState.Ready -> Unit
            }
        }
    }

    val ready = state as? AccountState.Ready ?: return
    Section("她的号 · 运行状态") {
        PanelCard(insideMargin = PaddingValues(vertical = 6.dp)) {
            accountDetails(ready.data).forEachIndexed { index, row ->
                InfoRow(
                    label = row.label,
                    value = row.value,
                    note = row.note,
                    icon = RowIcons.getOrNull(index),
                    accent = accent,
                )
            }
        }
    }
    Section("供应商与模型") {
        PanelCard(insideMargin = PaddingValues(vertical = 6.dp)) {
            if (!ready.data.modelAvailable) {
                InfoRow(
                    label = "/api/model",
                    value = "面板拿不到这份数据",
                    note = "路由回了 available:false —— 装配点没把 model_providers 接缝递进来；这里不编空表。",
                    icon = MiuixIcons.Layers,
                    accent = accent,
                )
            } else {
                ModelRows(ready.data, accent)
            }
        }
    }
}

@Composable
private fun ModelRows(data: AccountData, accent: Accent) {
    if (data.providers.isEmpty()) {
        InfoRow(
            label = "供应商",
            value = "还没有加过",
            note = "文件里 providers 是空的。",
            icon = MiuixIcons.Layers,
            accent = accent,
        )
    }
    data.providers.forEach { provider ->
        InfoRow(
            label = provider.name,
            value = "key ${provider.keyCount} 把" +
                if (provider.models.isEmpty()) "" else " · 模型 ${provider.models.joinToString("、")}",
            note = listOfNotNull(
                provider.baseUrl.takeIf { it.isNotBlank() },
                provider.usedBy.takeIf { it.isNotEmpty() }?.let { "这几路在用：${it.joinToString("、")}" },
            ).joinToString("　"),
            icon = MiuixIcons.Layers,
            accent = accent,
        )
    }
    if (data.bindings.isNotEmpty()) {
        data.bindings.forEach { binding ->
            InfoRow(
                label = binding.label,
                value = binding.provider + (if (binding.model.isBlank()) "" else " · ${binding.model}"),
                note = if (binding.layer.isBlank()) "" else "实际落层：${binding.layer}",
                icon = MiuixIcons.Layers,
                accent = accent,
            )
        }
    }
}

@Composable
private fun Hint(text: String, isError: Boolean = false) {
    Text(
        modifier = Modifier.padding(horizontal = 2.dp),
        text = text,
        style = MiuixTheme.textStyles.body2,
        color = if (isError) MiuixTheme.colorScheme.error else MiuixTheme.colorScheme.onSurfaceContainerVariant,
    )
}
