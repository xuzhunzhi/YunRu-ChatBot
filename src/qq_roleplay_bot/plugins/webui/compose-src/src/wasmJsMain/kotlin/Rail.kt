// 左栏：不再用 `NavigationRail`，改成用 `BasicComponent`（Custom Content Variant）自搭。
//
// 为什么换（用户 2026-10-19 批的改动，原话「做呗」）：目标是**每项左侧一个彩色圆角方块**。
// `NavigationRail` 做不到，不是没调参，是结构上没有那个槽：
//   * `NavigationRailItem` 的签名是 `icon: ImageVector`（navigationrail.md:99-109），内部就一句
//     `Image(…, colorFilter = ColorFilter.tint(onSurfaceContainer))`（NavigationRail.kt:397-405）
//     —— 一个项只有**一个**色相，图标本身是单色路径，没有"容器色"这一层；
//   * 选中药丸的色也写死在组件里（`surfaceContainerHigh`，NavigationRail.kt:442），不让传。
// 两条路都被"不 fork 库"否掉：① 把 `NavigationRailItem` 连同它约 120 行的 morph 布局抄一遍；
// ② 按它内部几何用 `drawBehind` 硬画（两档是两个 composable、y 坐标也不同，一改库就漂）。
// 剩下的就是 ③：换成 `BasicComponent` 的 Custom Content Variant（basiccomponent.md:74-101）——
// 文档原话 "full control over the layout inside the component while still reusing the container
// and interactions"。`startAction` 槽收任意 composable，所以彩色方块放这里；container、点击、
// 按压反馈仍是 Miuix 的，库一个字节没改。
//
// 几何**照 `NavigationRailDefaults`**（navigationrail.md:117-135），不是自己拍的：
//   展开 240 / 收起 80 / 整列竖向 24 / 展开项左右外边距 12 / 选中药丸圆角 16 / 项内 14×14 /
//   项间 12（这一条 Miuix 没有对应常量，是用户这轮给的）。
//
// ## 方块 x 的对齐账（这是两条要求唯一打架的地方，算清楚）
//
//   展开档：项外边距 12 + 项内左内边距 14 = **26** —— 与 NavigationRail 展开态图标左缘同值
//          （`ExpandedItemHorizontalMargin + ExpandedItemContentHorizontalPadding`）。
//   收起档：80 宽、方块要居中 ⇒ 左缘 = (80 − 方块边长) / 2。
//   两条同时成立，要求 方块边长 = 80 − 2 × 26 = **28dp**。但方块边长被要求在 30–34dp，所以
//   **"居中"和"同一条线"在 30dp 上差 (30 − 28) / 2 = 1dp**，物理上不可能同时精确成立。
//
//   取 30dp（区间下限，把差值压到最小）+ 项外边距两档都是 12：
//     收起：80 − 12 − 12 = 56（药丸宽）→ 项内 (56 − 30) / 2 = **13** → 方块左缘 12 + 13 = **25**，
//           中心 25 + 15 = **40** = 80 / 2（精确居中，方块在 56 的药丸里左右各 13，也精确居中）；
//     展开：12 + 14 = **26**，中心 41。
//   即：收起档精确居中（差 0），与展开档同一条线差 **1dp**。1 CSS px，切换时看不出来。
//
// ## 动画用的哪个
//
//   几何（rail 宽、项内边距、方块与标签之间的间距）用 **`animateDpAsState`**（220ms）；
//   标签淡入淡出、分组小标题的淡入淡出与占位高度用 **`animateFloatAsState`** + `Modifier.alpha`
//   / `Modifier.height`（分组标题 200ms / 标签 160ms）。
//
//   **用了 `AnimatedVisibility`，然后扔掉了**——不是没试。第一版分组小标题是
//   `AnimatedVisibility(visible = expanded, enter = fadeIn + expandVertically, …)`，实机在 390
//   上点开时拍到小标题**横向错位**：松手后约 60ms 那帧标题墨迹左缘 x=2、约 140ms 那帧 x=0
//   （被窗口左缘切掉，"账号与设备"只剩"与设备"），稳定后才是 x=26。也就是说它的尺寸动画在
//   换档过程中把子内容按"过渡开始时的容器宽"摆了位置。标签那 16sp 的位移能靠 `alpha` 遮过去，
//   一行标题错位 26px 遮不过去，所以改成自己算：外层 `height(32dp × p)` + `clipToBounds()`，
//   内层固定 32dp、左缘恒为 26，p 从 0 到 1。改完同一时刻再拍，标题左缘就是 26（NOTES §14.5）。
//
//   标签没有用 `AnimatedVisibility` 的另一个理由：它的收缩是"先量出内容宽、再夹到 0"，而
//   `BasicComponent` 的 center 槽本来就被 startAction 挤到一个会随 rail 变窄而趋 0 的宽度上，
//   两层夹一起会在收起尾帧出现"文字被压成竖排"的观感（`Component.kt` 文件头警告过的那个坑）。
//   `compose.animation` 本来就在 `wasmJsCompileClasspath` 上（foundation 的 api 依赖），
//   所以试它没有新增依赖；现在不用它了，依赖也没变。

import androidx.compose.animation.core.animateDpAsState
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.tween
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.selection.selectableGroup
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.clipToBounds
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.selected
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import top.yukonga.miuix.kmp.basic.BasicComponent
import top.yukonga.miuix.kmp.basic.Icon
import top.yukonga.miuix.kmp.basic.IconButton
import top.yukonga.miuix.kmp.basic.NavigationRailDefaults
import top.yukonga.miuix.kmp.basic.SmallTitle
import top.yukonga.miuix.kmp.basic.Text
import top.yukonga.miuix.kmp.icon.MiuixIcons
import top.yukonga.miuix.kmp.icon.basic.Sidebar
import top.yukonga.miuix.kmp.theme.MiuixTheme

/** 方块边长。规范给的区间是 30–34，取 30（理由见文件头"对齐账"：28 才是两档精确同线的边长）。 */
private val TileSide = 30.dp

/** 方块里的图标。规范给的区间是 20–24，取 20（30 的方块里留 5dp 边，与 34/20 的内容区块比更"实"）。 */
private val TileGlyph = 20.dp

/** 方块圆角：规范 10–12，取 10（30 的方块用 1/3 = 10，与内容区 34 方块的 0.3 比例一致）。 */
private val TileCorner = 10.dp

/** 选中药丸圆角 = `NavigationRailDefaults.ExpandedItemCornerRadius`（16dp，navigationrail.md:129）。 */
private val PillRadius = NavigationRailDefaults.ExpandedItemCornerRadius

/** 项与项之间的间距（用户这轮给的 12；Miuix 自己没有这个常量——它靠项内 padding 拉开）。 */
private val ItemSpacing = 12.dp

/** toggle 按钮边长：56 = `IconSize 28 + ExpandedItemContentVerticalPadding 14 × 2`（= Miuix 自己的算法）。 */
private val ToggleSize = NavigationRailDefaults.IconSize +
    NavigationRailDefaults.ExpandedItemContentVerticalPadding * 2

/** 展开档项内左内边距 = `ExpandedItemContentHorizontalPadding`（14dp）。 */
private val ItemPaddingExpanded = NavigationRailDefaults.ExpandedItemContentHorizontalPadding

/** 收起档项内左内边距：见文件头，`(MinWidth − 2 × 项外边距 − 方块边长) / 2 = (80 − 24 − 30) / 2 = 13`。 */
private val ItemPaddingCollapsed =
    (NavigationRailDefaults.MinWidth - NavigationRailDefaults.ExpandedItemHorizontalMargin * 2 - TileSide) / 2

/** 方块与标签之间的间距：`ExpandedItemIconTextSpacing` = 16，减去 `BasicComponent` 自己那 8dp spacer。 */
private val LabelGapExpanded = NavigationRailDefaults.ExpandedItemIconTextSpacing - 8.dp

/** 展开档里分组小标题 / 品牌行的左缘 = 方块左缘（项外边距 12 + 项内 14 = 26）。 */
private val ContentInset = NavigationRailDefaults.ExpandedItemHorizontalMargin + ItemPaddingExpanded

/**
 * 分组小标题那一行的固定高度。取 32 = `subtitle` 14sp 的一行（约 19）+ 上下各 6~7 的留白；
 * 这个常数只用于"从 0 长到它"的动画与裁切，文字本身仍然由 `SmallTitle` 自己排版。
 */
private val GroupTitleHeight = 32.dp

/** 非首组的小标题额外留白（首组上面已经有 HeaderSpacing 24，不需要再叠）。 */
private val GroupTitleGap = 8.dp

/**
 * 项 → 色槽的**固定映射表**。写成字面表而不是"按 index 轮换"：插件清单是活的
 * （`GET /api/plugins`），按 index 轮换会让同一个插件在装了/卸了别的插件之后换颜色。
 * 键是 [RailItem.id]（插件项是 `plugin:<name>`）。
 *
 * 色槽只用 `docs/guide/colors.md` 里现成的：`primary` / `primaryContainer` / `error`，
 * 没有再叠 container 变体——**浅色档里那些 container 在 rail 底色 `#F7F7F7` 上对比度只有
 * 1.00–1.06**（`secondaryContainer #F0F0F0` = 1.06、`tertiaryContainer #EAF2FF` = 1.05、
 * `errorContainer #FDF6F4` = 1.00），方块等于看不见，过不了"每项左侧是有颜色的方块"这条。
 * 深浅两档都能立住的只有这三个（数字见 NOTES §14.3）。
 *
 * 未登记的 id（将来新装的插件）走 [railAccent] 里的散列分支（`stableSlot` + `RailAccentCycle`）：
 * 按名字做**确定性散列**，同样跨会话稳定
 * （不用 `hashCode()`——虽然它也是确定的，但写法上依赖 JVM 定义，这里自己算一个更省心）。
 */
private val RailAccents: Map<String, Accent> = mapOf(
    // 账号与设备
    PANE_ACCOUNT to Accent.Blue,
    "device" to Accent.MidBlue,
    // 聊天
    "overview" to Accent.Blue,
    "group" to Accent.MidBlue,
    "private" to Accent.Red,
    // 记忆
    "memory" to Accent.Blue,
    "memory_profile" to Accent.MidBlue,
    // 知识库
    "knowledge" to Accent.Red,
    "knowledge_quotes" to Accent.Blue,
    "knowledge_slang" to Accent.MidBlue,
    // 插件（这 9 个是本机 `/api/plugins` 的固定清单，Nav.kt `pluginFallback` 同一份）
    "plugin:group_admin" to Accent.Blue,
    "plugin:join_approval" to Accent.MidBlue,
    "plugin:mail" to Accent.Red,
    "plugin:outage_notice" to Accent.Blue,
    "plugin:remind" to Accent.MidBlue,
    "plugin:rss" to Accent.Red,
    "plugin:timeout_alert" to Accent.Blue,
    "plugin:vision" to Accent.MidBlue,
    "plugin:webui" to Accent.Red,
    // 系统
    "logs" to Accent.Blue,
    "settings" to Accent.MidBlue,
    "model" to Accent.Red,
    "actions" to Accent.Blue,
)

/** 散列取的色槽顺序。挑的顺序是"相邻两项尽量不同色"，本身也是固定的。 */
private val RailAccentCycle = listOf(Accent.Blue, Accent.MidBlue, Accent.Red)

/** 确定性散列（不依赖平台 `hashCode`），只为"新插件也稳定"。 */
private fun stableSlot(id: String): Int {
    var h = 0
    for (ch in id) h = (h * 31 + ch.code) and 0x7fff_ffff
    return h % RailAccentCycle.size
}

/** 一个 rail 项的色槽：先查固定表，查不到才散列。 */
fun railAccent(itemId: String): Accent = RailAccents[itemId] ?: RailAccentCycle[stableSlot(itemId)]

/**
 * 左栏。宽度自己在 [NavigationRailDefaults.MinWidth] / [ExpandedWidth] 之间用 [animateDpAsState] 动，
 * 换档时项内边距、标签间距跟着一起插值；内容列仍由调用方的 `weight(1f)` 吃掉剩余宽度。
 *
 * [expanded] 是"当前要不要展开"，由调用方决定（窗宽断点 + 用户按 toggle 后的覆盖，见 Shell.kt）。
 */
@Composable
fun PanelRail(
    modifier: Modifier = Modifier,
    groups: List<RailGroup>,
    selectedId: String,
    expanded: Boolean,
    onSelect: (String) -> Unit,
    onToggle: () -> Unit,
) {
    val width by animateDpAsState(
        targetValue = if (expanded) NavigationRailDefaults.ExpandedWidth else NavigationRailDefaults.MinWidth,
        animationSpec = tween(durationMillis = 220),
        label = "railWidth",
    )
    val itemPadding by animateDpAsState(
        targetValue = if (expanded) ItemPaddingExpanded else ItemPaddingCollapsed,
        animationSpec = tween(durationMillis = 220),
        label = "railItemPadding",
    )
    val labelGap by animateDpAsState(
        targetValue = if (expanded) LabelGapExpanded else 0.dp,
        animationSpec = tween(durationMillis = 220),
        label = "railLabelGap",
    )
    val labelAlpha by animateFloatAsState(
        targetValue = if (expanded) 1f else 0f,
        animationSpec = tween(durationMillis = 160),
        label = "railLabelAlpha",
    )
    val scroll = rememberScrollState()

    Column(
        modifier = modifier
            .width(width)
            .fillMaxHeight()
            // 底色照 `NavigationRail` 的默认（`colorScheme.surface`），与页底同色：深色档的药丸
            // `surfaceContainerHigh` 与 `surfaceContainer`/`surfaceVariant` 是同一个 #242424，
            // 给 rail 换底色会把选中态抹掉（上一轮 NOTES §13.7-2 已经算过）。
            .background(MiuixTheme.colorScheme.surface)
            .verticalScroll(scroll)
            // 语义上的"这是一组选项"：`NavigationRail` 内部有，换成 `clickable` 之后得自己补。
            .selectableGroup()
            .padding(vertical = NavigationRailDefaults.VerticalPadding),
    ) {
        RailToggle(expanded = expanded, onToggle = onToggle)
        Spacer(Modifier.height(NavigationRailDefaults.HeaderSpacing))
        Text(
            modifier = Modifier.fillMaxWidth().padding(start = ContentInset),
            text = if (expanded) "云茹" else "云",
            style = MiuixTheme.textStyles.title2,
            color = MiuixTheme.colorScheme.onSurface,
        )
        Spacer(Modifier.height(NavigationRailDefaults.HeaderSpacing))

        groups.forEachIndexed { index, group ->
            // 分组小标题只活在展开档，占位高度从 0 长到 [GroupTitleHeight]（见文件头：
            // `AnimatedVisibility` 在这条路上会把标题横向摆错，所以自己算）。
            RailGroupTitle(expanded = expanded, text = group.title, first = index == 0)
            group.items.forEach { item ->
                RailItemRow(
                    item = item,
                    isSelected = item.id == selectedId,
                    expanded = expanded,
                    itemPadding = itemPadding,
                    labelGap = labelGap,
                    labelAlpha = labelAlpha,
                    onClick = { onSelect(item.id) },
                )
                Spacer(Modifier.height(ItemSpacing))
            }
        }
    }
}

/**
 * 分组小标题（`SmallTitle`）的进出。
 *
 * 外层高度 = [GroupTitleHeight] × p，`clipToBounds()` 负责在 p < 1 时裁掉下半截；
 * 内层固定 [GroupTitleHeight] 高、`CenterStart` 对齐，所以**左缘恒为 [ContentInset]**，
 * 与方块左缘同一条线，p 怎么变都不动（这一点是替换 `AnimatedVisibility` 的原因，见文件头）。
 */
@Composable
private fun RailGroupTitle(expanded: Boolean, text: String, first: Boolean) {
    val progress by animateFloatAsState(
        targetValue = if (expanded) 1f else 0f,
        animationSpec = tween(durationMillis = 200),
        label = "railGroupTitle",
    )
    // 首组上面少留一点：展开档的 toggle + 品牌行已经给了 48dp 的 HeaderSpacing。
    val height = GroupTitleHeight + if (first) 0.dp else GroupTitleGap
    Box(
        modifier = Modifier
            .fillMaxWidth()
            .height(height * progress)
            .alpha(progress)
            .clipToBounds(),
    ) {
        Box(
            modifier = Modifier.fillMaxWidth().height(height),
            contentAlignment = Alignment.CenterStart,
        ) {
            SmallTitle(
                text = text,
                modifier = Modifier.fillMaxWidth(),
                insideMargin = PaddingValues(
                    start = ContentInset,
                    end = NavigationRailDefaults.ExpandedItemHorizontalMargin,
                ),
            )
        }
    }
}

/**
 * 收起 / 展开按钮。位置两档都是 `start = ExpandedItemHorizontalMargin`（12）：
 *   * 展开档 12 之后是 56 宽的按钮，中心 40，与项里的方块中心（41）差 1dp（= 方块比 28 宽 2 的那半个）；
 *   * 收起档 (80 − 56) / 2 = 12，正好也是 12，所以两档按钮**不动**。
 */
@Composable
private fun RailToggle(expanded: Boolean, onToggle: () -> Unit) {
    Box(
        modifier = Modifier
            .fillMaxWidth()
            .padding(start = NavigationRailDefaults.ExpandedItemHorizontalMargin),
    ) {
        IconButton(
            onClick = onToggle,
            minWidth = ToggleSize,
            minHeight = ToggleSize,
        ) {
            Icon(
                imageVector = MiuixIcons.Basic.Sidebar,
                contentDescription = if (expanded) "收起侧栏" else "展开侧栏",
                modifier = Modifier.size(NavigationRailDefaults.IconSize),
                tint = MiuixTheme.colorScheme.onSurfaceContainer,
            )
        }
    }
}

/**
 * 一项。结构是 药丸 Box（只有选中项有底色）→ `BasicComponent`（Custom Content Variant）：
 * `startAction` 放彩色方块，`content` 放标题。
 *
 * 药丸用 `RoundedCornerShape(16)` + `background` 自己画（用户这轮明确要的就是这个），
 * 不是 Miuix 的 `squircleBackground`——两者在 16dp 圆角、56~58px 高的块上肉眼无差，
 * 但这一份是自搭的，用普通圆角就不必再把 squircle 的 API 拖进这一层。
 */
@Composable
private fun RailItemRow(
    item: RailItem,
    isSelected: Boolean,
    expanded: Boolean,
    itemPadding: Dp,
    labelGap: Dp,
    labelAlpha: Float,
    onClick: () -> Unit,
) {
    // 未选中不画底色（`Color.Transparent` 仍然占位，保证选中/未选中的布局一模一样）。
    val pill = if (isSelected) MiuixTheme.colorScheme.surfaceContainerHigh else Color.Transparent
    Box(
        modifier = Modifier
            .fillMaxWidth()
            .padding(horizontal = NavigationRailDefaults.ExpandedItemHorizontalMargin)
            .clip(RoundedCornerShape(PillRadius))
            .background(pill),
    ) {
        BasicComponent(
            // `NavigationRailItem` 用的是 `selectable(selected=…)`，那条 API 自带 `selected` 语义；
            // `BasicComponent` 只给 `clickable`，所以这里手动补一个——不加的话读屏只会念
            // "账号，标签页"，不会说"已选中"（NOTES §14.7）。
            modifier = Modifier.semantics { this.selected = isSelected },
            startAction = {
                // end 内边距就是"方块与标签的间距"：`BasicComponent` 自己在 start 与 center 之间
                // 固定塞 8dp（Component.kt:200），所以这里补 8 凑成 `ExpandedItemIconTextSpacing` 16。
                // 收起档这个 padding 动画到 0，方块就在 30 宽的内容区里精确居中。
                Box(modifier = Modifier.padding(end = labelGap)) {
                    Tile(
                        icon = item.icon,
                        accent = railAccent(item.id),
                        size = TileSide,
                        glyphSize = TileGlyph,
                        cornerRadius = TileCorner,
                    )
                }
            },
            insideMargin = PaddingValues(
                horizontal = itemPadding,
                vertical = NavigationRailDefaults.ExpandedItemContentVerticalPadding,
            ),
            onClick = onClick,
            // 与 `NavigationRailItem` 同一个 role（navigationrail.md:99-109 那一支内部用的是
            // `selectable(role = Role.Tab)`）。差别见 NOTES §14.7：`clickable` 没有 `selected` 语义。
            role = Role.Tab,
        ) {
            if (expanded || labelAlpha > 0f) {
                Text(
                    text = item.label,
                    modifier = Modifier.alpha(labelAlpha),
                    // = ExpandedLabelFontSize（16sp），navigationrail.md:127
                    fontSize = NavigationRailDefaults.ExpandedLabelFontSize,
                    fontWeight = FontWeight.Medium,
                    color = MiuixTheme.colorScheme.onSurfaceContainer,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                )
            }
        }
    }
}
