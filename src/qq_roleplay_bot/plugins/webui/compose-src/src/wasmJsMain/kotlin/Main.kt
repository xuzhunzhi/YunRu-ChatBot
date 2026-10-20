// Entry point for the v2 panel.
//
// The one thing this file really owns is the **font gate**: the first frame that carries text must
// already have CJK glyphs. Last round measured the default behaviour — Chinese paints as □ and
// turns into real glyphs 1.2–1.9 s later, because Skiko's wasm font fallback resolves
// asynchronously (`NOTES.md` §"中文豆腐块"). Miuix's own web example solves it by preloading MiSans
// *through a CDN*; this deployment is offline, so the same idea runs against a font file that ships
// inside the bundle (see `tools/subset-font.py`).
//
// Ordering, and why it is this and not "paint first, fix later":
//
//   1. resolve the font bytes and hand them to Skia (`Font(...)`),
//   2. `FontFamily.Resolver.preload(...)` — this is the documented way to force the typeface into
//      the shared cache *before* any paragraph asks for it; after it returns, layout can find the
//      glyphs synchronously,
//   3. only then compose the panel (`PanelApp`), with the family applied to every Miuix text style.
//
// Until step 3 the page paints an empty themed surface: no text, so no tofu. `?nofont=1` skips the
// gate on purpose — that is the A/B frame used as evidence against the old behaviour.

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.runtime.withFrameNanos
import androidx.compose.ui.ExperimentalComposeUiApi
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalFontFamilyResolver
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.platform.Font
import androidx.compose.ui.window.ComposeViewport
import top.yukonga.miuix.kmp.theme.ColorSchemeMode
import top.yukonga.miuix.kmp.theme.MiuixTheme
import top.yukonga.miuix.kmp.theme.ThemeController

/** Bundle-relative path: `src/wasmJsMain/resources/fonts/…` lands next to `index.html`. */
private const val FONT_URL = "fonts/NotoSansSC-Regular-subset.ttf"
private const val FONT_IDENTITY = "NotoSansSC-subset"
private const val TOKEN_KEY = "yunru_panel_token"

@OptIn(ExperimentalComposeUiApi::class)
fun main() {
    // A/B switch for the evidence frames; nothing else reads it.
    val fontDisabled = queryParam("nofont") == "1"

    ComposeViewport(viewportContainerId = "composeApp") {
        val resolver = LocalFontFamilyResolver.current
        var family by remember { mutableStateOf<FontFamily?>(null) }
        var fontSettled by remember { mutableStateOf(fontDisabled) }
        var token by remember {
            mutableStateOf(localGet(TOKEN_KEY).ifBlank { queryParam("token") })
        }
        var plugins by remember { mutableStateOf(pluginFallback) }
        val themeController = remember { ThemeController(colorSchemeMode = ColorSchemeMode.System) }

        LaunchedEffect(Unit) {
            if (fontDisabled) {
                markFontReady(nowMs(), false)
                return@LaunchedEffect
            }
            val started = nowMs()
            val bytes = httpGetBytes(FONT_URL)
            if (bytes == null) {
                consoleWarn("[font] $FONT_URL 取不到，退回系统字体回退（中文会先出豆腐块）")
                markFontReady(started, false)
                fontSettled = true
                return@LaunchedEffect
            }
            val font = runCatching {
                Font(identity = FONT_IDENTITY, getData = { bytes }, weight = FontWeight.Normal)
            }.getOrNull()
            if (font == null) {
                consoleWarn("[font] Skia 不接受这份字体字节（${bytes.size} B），退回系统回退")
                markFontReady(started, false)
                fontSettled = true
                return@LaunchedEffect
            }
            val loaded = FontFamily(font)
            // `preload` is what turns "loaded once" into "every paragraph can find it": without it
            // the first layout still races the asynchronous typeface resolution.
            runCatching { resolver.preload(loaded) }
            markFontReady(started, true)
            family = loaded
            fontSettled = true
        }

        // Plugin list feeds the 「插件」 group. It needs the token, so it is refreshed whenever the
        // token changes; the static fallback keeps the rail complete before the first login.
        LaunchedEffect(token) {
            if (token.isBlank()) return@LaunchedEffect
            runCatching { loadPlugins(token) }.getOrNull()?.takeIf { it.isNotEmpty() }?.let { plugins = it }
        }

        if (!fontSettled) {
            // No text on purpose: this frame may be presented before Skia has the glyphs.
            MiuixTheme(controller = themeController) {
                Box(Modifier.fillMaxSize().background(MiuixTheme.colorScheme.surface))
            }
        } else {
            // First text-bearing frame, stamped the way the previous round measured: `withFrameNanos`
            // resumes at frame start, so the second resume means "the frame with this text is drawn".
            LaunchedEffect(Unit) {
                withFrameNanos { }
                withFrameNanos { }
                markFirstText()
            }
            PanelApp(
                family = family,
                token = token,
                onTokenChange = { value ->
                    token = value
                    if (value.isNotBlank()) localSet(TOKEN_KEY, value)
                },
                plugins = plugins,
            )
        }
    }
}
