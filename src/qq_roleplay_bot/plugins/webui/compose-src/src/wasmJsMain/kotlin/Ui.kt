// The page vocabulary: 分组小标题 → 纯白卡片 → 卡内行（留白分隔，不画线）。
//
// 这三样不是自己发明的，是 Miuix 自带样例的构图（`run/data/tmp_miuix/miuix-0.9.4/assets/002.webp`
// 第一联，浅色默认配色；像素实测：页底 #F7F7F7、卡 #FFFFFF、分组标题在卡外、卡内行间无分割线、
// 圆角 20 级）。对应用到的令牌全部来自 `docs/guide/colors.md` 与 `docs/components/*.md`：
//
//   * 页底   = colorScheme.surface          （浅 #F7F7F7 / 深 #000000，样例测得的底）
//   * 卡片   = colorScheme.surfaceContainer （浅 #FFFFFF / 深 #242424，card.md 的默认卡色）
//   * 卡内块 = colorScheme.surfaceContainerHighest（浅 #E8E8E8 / 深 #2D2D2D，用来装等宽的路由串）
//
// 图标底色块：Miuix 的 `Icon` 是单色（`ColorFilter.tint`），`NavigationRailItem` 只收
// `ImageVector`，所以底块只能在**自己排的行**上做。底色 + 前景都从色板里取，不自造色：
// 蓝 = tertiaryContainer/onTertiaryContainer，红 = errorContainer/error，灰 =
// secondaryContainer/onSecondaryVariant（`docs/guide/colors.md` 那三对）。

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ColumnScope
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import top.yukonga.miuix.kmp.basic.BasicComponent
import top.yukonga.miuix.kmp.basic.Card
import top.yukonga.miuix.kmp.basic.Icon
import top.yukonga.miuix.kmp.basic.SmallTitle
import top.yukonga.miuix.kmp.basic.Text
import top.yukonga.miuix.kmp.theme.MiuixTheme

/** Card radius from the sample (16 is `CardDefaults.CornerRadius`; the sample reads ~20). */
private val CardRadius = 20.dp

/** Icon tile size / glyph size, from the reference's rounded-square icons. */
private val TileSize = 34.dp

/**
 * Which palette pair a tile uses. **This enum is the whole palette, not a stylistic choice**:
 * Miuix's non-Monet scheme (docs/guide/colors.md) carries a blue hue, a red hue and greys — nothing
 * else. The four slots below are exactly the pairs that stay visible **as a coloured square in both
 * light and dark**; the container variants are ruled out by contrast against the rail's `#F7F7F7` in
 * light mode (`secondaryContainer #F0F0F0` = 1.06 : 1, `tertiaryContainer #EAF2FF` = 1.05 : 1,
 * `errorContainer #FDF6F4` = 1.00 : 1 — an invisible square). Numbers in NOTES §14.3.
 *
 * 内容区按组取 Blue / Red / Neutral（Shell.kt `ACCENTS`），左栏按项取 Rail.kt `RailAccents`。
 */
enum class Accent { Blue, MidBlue, Red, Neutral }

@Composable
fun Accent.tileColor(): Color = when (this) {
    // 饱和底 + 白/深前景：参考图的图标块（WLAN 蓝、通知红）就是这个关系。
    Accent.Blue -> MiuixTheme.colorScheme.primary
    Accent.MidBlue -> MiuixTheme.colorScheme.primaryContainer
    Accent.Red -> MiuixTheme.colorScheme.error
    Accent.Neutral -> MiuixTheme.colorScheme.secondaryContainer
}

@Composable
fun Accent.tileContentColor(): Color = when (this) {
    Accent.Blue -> MiuixTheme.colorScheme.onPrimary
    Accent.MidBlue -> MiuixTheme.colorScheme.onPrimaryContainer
    Accent.Red -> MiuixTheme.colorScheme.onError
    Accent.Neutral -> MiuixTheme.colorScheme.onSecondaryVariant
}

/**
 * A rounded-square icon tile. Miuix's `Icon` paints one tint, so the colour lives in the container
 * behind it — that is the whole trick the reference's colourful rows use.
 *
 * [glyphSize] / [cornerRadius] 是这一轮为左栏加的（30dp 方块 + 20dp 图标 + 10dp 圆角）；
 * 默认值仍是原来那套 34dp 方块 / 0.58 图标 / 0.3 圆角，内容区一个字都不用改。
 */
@Composable
fun Tile(
    icon: ImageVector,
    accent: Accent,
    size: Dp = TileSize,
    glyphSize: Dp = size * 0.58f,
    cornerRadius: Dp = size * 0.3f,
) {
    Box(
        modifier = Modifier
            .size(size)
            .clip(RoundedCornerShape(cornerRadius))
            .background(accent.tileColor()),
        contentAlignment = Alignment.Center,
    ) {
        Icon(
            imageVector = icon,
            contentDescription = null,
            modifier = Modifier.size(glyphSize),
            tint = accent.tileContentColor(),
        )
    }
}

/** One page section: 灰色小标题在卡外，卡片在下面（样例的 "Checkbox / Switch / Dropdown" 节奏）。 */
@Composable
fun Section(title: String, content: @Composable ColumnScope.() -> Unit) {
    Column(modifier = Modifier.fillMaxWidth().padding(bottom = 20.dp)) {
        // 样例的分组标题与卡片左缘对齐，所以这里的 inside margin 只留竖向；
        // 默认的 PaddingValues(28, 8) 会把标题推进卡片里 28dp。
        SmallTitle(text = title, insideMargin = PaddingValues(horizontal = 2.dp, vertical = 8.dp))
        content()
    }
}

/** A pure-white card: radius 20, `surfaceContainer`, no in-card dividers (spacing only). */
@Composable
fun PanelCard(
    modifier: Modifier = Modifier,
    insideMargin: PaddingValues = PaddingValues(16.dp),
    content: @Composable ColumnScope.() -> Unit,
) {
    Card(
        modifier = modifier.fillMaxWidth(),
        cornerRadius = CardRadius,
        insideMargin = insideMargin,
        content = content,
    )
}

/**
 * label → value with an optional quiet explanation, on one row. `BasicComponent` is Miuix's own
 * "setting row" (title 17 medium, summary 14 grey, endActions on the right), so the rows here look
 * like the sample's `设备名称 / 存储空间` instead of a hand-rolled 140dp label column.
 *
 * 窄档（卡片自身 < [COMPACT_ROW_WIDTH]）改成"值掉到下一行"：310dp 宽的内容列里，右侧那 60%
 * 上限会把 `本机（Bearer token）`、`/api/model` 这种值挤成竖排，比堆叠更难看。
 *
 * [icon] is optional: the content lists carry a coloured tile (the reference's list-row shape),
 * while rows inside a plain card can stay text-only.
 */
@Composable
fun InfoRow(
    label: String,
    value: String,
    note: String = "",
    icon: ImageVector? = null,
    accent: Accent = Accent.Blue,
) {
    // 显式给类型：`icon?.let { … }` 那边推断不出带 @Composable 的 lambda。
    val start: (@Composable () -> Unit)? = if (icon == null) {
        null
    } else {
        { Tile(icon = icon, accent = accent, size = 30.dp) }
    }
    BoxWithConstraints {
        val compact = maxWidth < COMPACT_ROW_WIDTH
        if (compact) {
            BasicComponent(
                title = label,
                summary = note.ifBlank { null },
                startAction = start,
                bottomAction = {
                    // 与 label 的左缘对齐：BasicComponent 把 bottomAction 铺满整行，
                    // 所以自己让开图标那一列（tile 30 + 间隔 8）。
                    Box(modifier = Modifier.padding(start = if (icon == null) 0.dp else 38.dp)) {
                        RowValue(value, emphasis = true)
                    }
                },
                insideMargin = PaddingValues(horizontal = 16.dp, vertical = 10.dp),
            )
        } else {
            BasicComponent(
                title = label,
                summary = note.ifBlank { null },
                startAction = start,
                endActions = { RowValue(value) },
                insideMargin = PaddingValues(horizontal = 16.dp, vertical = 10.dp),
            )
        }
    }
}

/** Rows below this width stack the value instead of right-aligning it. */
private val COMPACT_ROW_WIDTH = 520.dp

@Composable
private fun RowValue(value: String, emphasis: Boolean = false) {
    Text(
        text = value,
        style = if (emphasis) MiuixTheme.textStyles.body1 else MiuixTheme.textStyles.body2,
        color = MiuixTheme.colorScheme.onSurfaceContainer,
    )
}

/** 卡内块：`surfaceContainerHighest` 上放一段等宽的路由串，卡内的"更深一层"。 */
@Composable
fun InnerBlock(modifier: Modifier = Modifier, content: @Composable () -> Unit) {
    Box(
        modifier = modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(12.dp))
            .background(MiuixTheme.colorScheme.surfaceContainerHighest)
            .padding(horizontal = 14.dp, vertical = 12.dp),
    ) {
        content()
    }
}

/** 卡片头：图标底块 + 页名 + 所属组，和参考图的列表项同一个形状。 */
@Composable
fun CardHeader(icon: ImageVector, accent: Accent, title: String, subtitle: String) {
    Row(
        modifier = Modifier.fillMaxWidth(),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Tile(icon = icon, accent = accent)
        Spacer(Modifier.size(12.dp))
        Column {
            Text(
                text = title,
                style = MiuixTheme.textStyles.title4,
                color = MiuixTheme.colorScheme.onSurfaceContainer,
            )
            Text(
                text = subtitle,
                style = MiuixTheme.textStyles.footnote2,
                color = MiuixTheme.colorScheme.onSurfaceContainerVariant,
            )
        }
    }
}
