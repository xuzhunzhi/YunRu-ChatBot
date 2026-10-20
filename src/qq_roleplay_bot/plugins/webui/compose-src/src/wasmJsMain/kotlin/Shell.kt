// The shell: MiuixTheme (system light/dark) → Surface(surface) → Row { 左栏 | 0.75dp 竖线 | 页列 }.
//
// 骨架按 Miuix 自带样例摆，不再按"规范推"：
//
//   * `assets/002.webp` / `004.webp`（浅色第一联）像素实测：页底 #F7F7F7、卡片 #FFFFFF、
//     分组小标题在卡外、卡内行间没有分割线、卡圆角 20 级、卡与卡之间约 20 留白。
//     这里就照这个摆：Surface 底色 = colorScheme.surface，卡片 = surfaceContainer（见 Ui.kt）。
//   * 样例的手机形态是"标题居中"，而参考图（小米平板 HyperOS 设置页）是"左对齐大标题"。
//     两者 Miuix 都有现成组件，按窗宽分档，而不是自己摆一个：
//       - 宽档（≥ 1100dp，rail 展开）：`TopAppBar`（largeTitle 左对齐，TitlePadding 26，
//         CollapsedHeight 52，副标题 SubtitleBottomPadding 8）；
//       - 窄档（< 1100dp，rail 收起）：`SmallTopAppBar`（Miuix 的手机形态，标题居中）。
//     **顶栏是页列的第一个元素**，不再横跨整个窗口：它只盖内容列，rail 从 y=0 一直到页底。
//   * 顶栏与内容之间**不画线**：样例里没有，Miuix 组件也不画；顶栏底色 = 页底色，本来就没有接缝。
//     拿到的对比度见 NOTES.md（#F7F7F7 vs #F7F7F7 是同色，画线只会多一条横幅）。
//   * 页列有最大宽度 `PAGE_MAX_WIDTH = 1320dp`（依据见那个常量的 KDoc）。超宽时**左对齐**、
//     不居中：参考图（平板 HyperOS）的右列就是贴着左栏起步的；而且上一轮第 4 条抱怨是
//     "内容区标题离左栏太远"——居中会在 1666 的窗口上凭空再塞 154dp 的左边距，正好把那条抱怨
//     做回来。
//   * `Scaffold` 被换掉了（用户的取舍：Scaffold 的 topBar 槽天然横跨整宽）。代价写在这里，
//     免得下一轮踩：Scaffold 同时兼职三件事——window insets、`MiuixPopupHost`（Overlay* /
//     Dropdown 的宿主）、以及 Surface 容器。现在 insets 由 rail 与顶栏各自声明（wasm 上是 0），
//     Surface 自己来；**popup host 没了**——将来哪一页要用 `OverlayDialog` / 下拉菜单，必须把
//     Scaffold 请回来（或自己提供 LocalPopupStates）。这一轮没有任何一页用到它们。
//
// 2026-10-19：左栏从 `NavigationRail` 换成 `Rail.kt` 里自搭的 `PanelRail`（`BasicComponent`
// Custom Content Variant），为的是每项左侧的**彩色圆角方块**。几何仍全部取
// `NavigationRailDefaults`（80/240、竖向 24、项外边距 12、项内 14×14、药丸圆角 16）；
// rail 与页列之间那条 0.75dp 竖线现在由这里自己画（`VerticalDivider`，色 = `dividerLine`），
// 因为它原来是 `NavigationRail` 的 `showDivider`。

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.unit.dp
import top.yukonga.miuix.kmp.basic.SmallTopAppBar
import top.yukonga.miuix.kmp.basic.Surface
import top.yukonga.miuix.kmp.basic.Text
import top.yukonga.miuix.kmp.basic.TopAppBar
import top.yukonga.miuix.kmp.basic.TopAppBarDefaults
import top.yukonga.miuix.kmp.basic.VerticalDivider
import top.yukonga.miuix.kmp.theme.ColorSchemeMode
import top.yukonga.miuix.kmp.theme.MiuixTheme
import top.yukonga.miuix.kmp.theme.TextStyles
import top.yukonga.miuix.kmp.theme.ThemeController

/**
 * Fixed breakpoint between the expanded and the collapsed rail.
 *
 * Why 1100dp and not 900: the rail is *fixed* (it does not overlay the pane), so the pane pays for
 * it. Expanded = 240dp; at 1100 the pane still gets ≥ 860dp, which is the width the old panel's
 * tables were designed against (`run/data/private_docs/WEBUI.md` §"布局" measured its six-column
 * audit table at 1180px). Below that the 80dp collapsed rail keeps the pane ≥ 1020dp at 1100 and
 * still ≥ 940dp at 1020.
 *
 * Measured in `dp`, which on wasm/Compose == CSS px / devicePixelRatio; `BoxWithConstraints.maxWidth`
 * is the window content width, so this is literally "window inner width in CSS px at dpr 1".
 */
private val EXPAND_RAIL_AT = 1100.dp

/**
 * 页列最大宽度。超过它的窗宽不再把内容拉满。
 *
 * 为什么是 1320 而不是 1180：1666 的窗口里 rail 占 240，内容列 1426；页列若按 1180，卡片会在
 * 1390 就结束，右边留下 **276dp** 的灰带（占窗宽 16.6%）——上一轮被指出的"右侧大片空白"只是
 * 换了个位置重演。1320 把那条灰带压到 80dp（正常页边距），同时仍比 1426 窄，没有"拉满整宽"。
 * 1180 那个数是老面板**整张六列表**的宽度（`run/data/private_docs/WEBUI.md` §"布局"），
 * 当成"页面最大宽度"会把页边距也算进表宽。
 */
private val PAGE_MAX_WIDTH = 1320.dp

/** 组 → 图标底色块的三档色板（见 Ui.kt `Accent`）。按组分配，颜色同时标记"这是一组"。 */
private val ACCENTS = listOf(Accent.Blue, Accent.Red, Accent.Neutral)

/** Applies [family] to every Miuix text style, so no component has to opt in per call site. */
@Composable
fun cjkTextStyles(family: FontFamily?): TextStyles {
    // Reads the *outer* value: this is called before MiuixTheme provides its own copy, so it is the
    // library default when the caller did not provide one.
    val base = MiuixTheme.textStyles
    if (family == null) return base
    return remember(base, family) {
        base.copy(
            main = base.main.copy(fontFamily = family),
            paragraph = base.paragraph.copy(fontFamily = family),
            body1 = base.body1.copy(fontFamily = family),
            body2 = base.body2.copy(fontFamily = family),
            button = base.button.copy(fontFamily = family),
            footnote1 = base.footnote1.copy(fontFamily = family),
            footnote2 = base.footnote2.copy(fontFamily = family),
            headline1 = base.headline1.copy(fontFamily = family),
            headline2 = base.headline2.copy(fontFamily = family),
            subtitle = base.subtitle.copy(fontFamily = family),
            title1 = base.title1.copy(fontFamily = family),
            title2 = base.title2.copy(fontFamily = family),
            title3 = base.title3.copy(fontFamily = family),
            title4 = base.title4.copy(fontFamily = family),
        )
    }
}

@Composable
fun PanelApp(
    family: FontFamily?,
    token: String,
    onTokenChange: (String) -> Unit,
    plugins: List<PluginEntry>,
) {
    val controller = remember { ThemeController(colorSchemeMode = ColorSchemeMode.System) }
    val textStyles = cjkTextStyles(family)
    MiuixTheme(controller = controller, textStyles = textStyles) {
        val groups = remember(plugins) { railGroups(plugins) }
        val flat = remember(groups) { groups.flatMap { it.items } }
        var selectedId by remember { mutableStateOf(PANE_ACCOUNT) }
        val selected = flat.firstOrNull { it.id == selectedId } ?: flat.first()
        val groupIndex = groups.indexOfFirst { group -> group.items.any { it.id == selected.id } }
            .coerceAtLeast(0)
        val groupTitle = groups.getOrNull(groupIndex)?.title.orEmpty()
        val accent = ACCENTS[groupIndex % ACCENTS.size]

        BoxWithConstraints {
            val wide = maxWidth >= EXPAND_RAIL_AT
            // 展开档的来源有两处，优先级是"用户按过 toggle 就以用户为准"：
            //   1. 窗宽断点（≥ 1100dp 展开）——上一轮的行为，默认态；
            //   2. 用户按 toggle 之后的覆盖值 `userExpanded`。
            // 窗口跨过断点时把覆盖清掉（`LaunchedEffect(wide)`），否则在 390 上手滑展开一次之后，
            // 拉回 1666 也还是收起的，看起来像坏了。
            var userExpanded by remember { mutableStateOf<Boolean?>(null) }
            LaunchedEffect(wide) { userExpanded = null }
            val expanded = userExpanded ?: wide
            // 顶栏用不用 largeTitle 只看**窗宽**，不看 rail 是否被手动收起：窄窗手动展开 rail
            // 不该把页面切成平板形态（内容列会被 240 挤到 150，两种都难看，取更稳的那个）。
            Surface(modifier = Modifier.fillMaxSize(), color = MiuixTheme.colorScheme.surface) {
                Row(modifier = Modifier.fillMaxSize()) {
                    PanelRail(
                        groups = groups,
                        selectedId = selected.id,
                        expanded = expanded,
                        onSelect = { selectedId = it },
                        onToggle = { userExpanded = !expanded },
                    )
                    // rail 与内容之间那条竖线：原来是 `NavigationRail` 的 `showDivider`，
                    // 现在自己画，参数仍取 `DividerDefaults`（0.75dp / `dividerLine`）。
                    VerticalDivider()
                    PageColumn(
                        modifier = Modifier.weight(1f).fillMaxHeight(),
                        wide = wide,
                        item = selected,
                        group = groupTitle,
                        accent = accent,
                        token = token,
                        onTokenChange = onTokenChange,
                    )
                }
            }
        }
    }
}

/**
 * The content column. First element is the page's own TopAppBar (so it never spans the window),
 * then the scrollable page. Both live inside one [PAGE_MAX_WIDTH]-wide container, which is what
 * keeps the title, the group titles and the cards on the same left edge.
 */
@Composable
private fun PageColumn(
    modifier: Modifier,
    wide: Boolean,
    item: RailItem,
    group: String,
    accent: Accent,
    token: String,
    onTokenChange: (String) -> Unit,
) {
    val scroll = rememberScrollState()
    Column(modifier = modifier) {
        Column(
            modifier = Modifier
                // widthIn 必须在 fillMaxWidth 前面：先夹住上限，再填满（反过来 minWidth 仍是全宽）。
                .widthIn(max = PAGE_MAX_WIDTH)
                .fillMaxWidth()
                .weight(1f),
        ) {
            PageHeader(item = item, group = group, wide = wide)
            Column(
                modifier = Modifier
                    .weight(1f)
                    .fillMaxWidth()
                    .verticalScroll(scroll)
                    .padding(
                        // 与 TopAppBar 的 TitlePadding 同值：标题左缘 = 卡片左缘。
                        start = TopAppBarDefaults.TitlePadding,
                        end = TopAppBarDefaults.TitlePadding,
                        bottom = 40.dp,
                    ),
            ) {
                if (item.id == PANE_ACCOUNT) {
                    AccountPane(token = token, onTokenChange = onTokenChange, accent = accent)
                } else {
                    StubPane(item = item, group = group, accent = accent)
                }
                Spacer(Modifier.height(8.dp))
            }
        }
    }
}

/**
 * 顶栏：宽档左对齐大标题（`TopAppBar` 的 largeTitle 本来就画在 TitlePadding 处），
 * 窄档用 `SmallTopAppBar`（它的标题是居中的——手机形态，见文件头）。
 * 底色直接用默认的 `surface`，与页底同色，所以它与内容之间没有横幅感，也不需要分割线。
 */
@Composable
private fun PageHeader(item: RailItem, group: String, wide: Boolean) {
    if (wide) {
        TopAppBar(
            title = item.label,
            largeTitle = item.label,
            subtitle = group,
        )
    } else {
        SmallTopAppBar(
            title = item.label,
            subtitle = group,
        )
    }
}

/**
 * Every page that is not 「账号」 yet. It deliberately keeps the page's own description visible: the
 * rail is the only place the IA lives, and a blank placeholder would hide what is still owed.
 *
 * 形状照样例：分组小标题在卡外 → 一张纯白卡（图标底块 + 页名 + 组 / 用途）→ 另一张卡装
 * 「将来要读的路由」（`surfaceContainerHighest` 卡内块）。行与行之间只有留白。
 */
@Composable
private fun StubPane(item: RailItem, group: String, accent: Accent) {
    Section("这一页") {
        PanelCard {
            CardHeader(icon = item.icon, accent = accent, title = item.label, subtitle = group)
            Spacer(Modifier.height(14.dp))
            Text(
                text = "这一页还没搬过来。",
                style = MiuixTheme.textStyles.subtitle,
                color = MiuixTheme.colorScheme.onSurfaceContainer,
            )
            Spacer(Modifier.height(6.dp))
            Text(
                text = item.body,
                style = MiuixTheme.textStyles.body2,
                color = MiuixTheme.colorScheme.onSurfaceContainerVariant,
            )
        }
    }
    Section("将来要读的路由") {
        PanelCard {
            InnerBlock {
                Column(verticalArrangement = Arrangement.spacedBy(2.dp)) {
                    item.routes.forEach { route ->
                        Text(
                            text = route,
                            style = MiuixTheme.textStyles.footnote1,
                            color = MiuixTheme.colorScheme.onSurfaceContainerHighest,
                        )
                    }
                }
            }
        }
    }
}
