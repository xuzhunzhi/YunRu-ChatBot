"""左栏布局与「同一张卡下面的项是平级的」的**静态断言**（不起浏览器、不连网）。

三条判据是用户当天定的，各有一个容易被后来的改动悄悄破掉的地方：

1. **左栏里不许出现操作类控件**（2026-10-06 用户："左边边栏下面的这个卡片不明所以，
   我为什么要在左边栏显示 bot 在运行，显示个活快照，显示个刷新深色和退出。这个不应该
   在 system 页面吗。。"）。左栏从此**只放导航**（"在左边切换设置项"）。
   破坏方式很具体：再往 `aside` 或 `drawTabs` 里塞一颗按钮——所以这里钉的是
   "左栏产出的每个 `<button>` 都必须带 `data-tab`"，而不是"某几颗按钮不在左栏"。
   同时反向钉住那几样**功能没被删掉**（别为了清空左栏把功能一起砍了），
   它们各自有新的落点：本体状态 → 总览「运行」卡；刷新 → 内容区顶栏；
   深色 → 调参页「面板」分组的开关；退出 → 账号页。

2. **人物画像属于「记忆」这张卡，且与「记忆」平级**（用户 2026-10-06 两句要一起读：
   "我应该说过人物画像属于记忆的子项目" = 它属于这个分区；
   "webui那里，**人物画像和记忆 tab 是同等级的，都在记忆卡片下**" = 左栏里两个是平级项）。
   ⚠️ 这一条被理解反过一轮：曾按"挂在上面那个条目下面"做了缩进/缩小/层级线，并钉
   "子项必须与父项不同级"——那是错的，已整个撤掉（见 `SubLevelEntriesTests`）。
   现在钉的是**反面**：同一张卡里的项**渲染完全一样**，谁再给某一项单开一档就红。

3. **金句 / 黑话与「知识库」平级**（用户："**金句黑话同理**"）——判据与第 2 条同一套。

为什么走静态断言：这些都是**结构**约束，离线套件里能算出来；真机观感只能靠截图
（见提交信息里那三张 1440 / 1024 / 500）。`test_webui_panel.py` 里那条宽度断言
是同一个路子。

⚠️ 解析这一层**不许用正则截函数体**：正文里到处是缩进的右花括号（对象、模板串），
`(.*?)` 配一个"换行 + 右括号"会停在第一个缩进右括号上——第一版就是这么写的，
7 条断言全"失败"，而代码其实是好的。所以下面有一个配对计数的 `_function_body`。
"""

from __future__ import annotations

import pathlib
import re
import sys
import unittest
from importlib import resources

# ⚠️ **必须把本仓库的 `src` 顶到最前面。** 这台机器的 `.venv` 里有一个 editable 安装的
# `.pth` 指着**另一个检出**（`run\src`，见 `__editable__.qq_roleplay_bot-0.1.0.pth`），
# 而本文件是按 `importlib.resources` 读**包内资源**（`webui/app.js`）的——路径一歪，
# 断言读到的是**另一棵树里那份没改过的 app.js**。这个坑不是理论：第一版跑出来 6 条
# "失败"，而代码其实是好的（`tests/run_offline.py` 会把 `src` 插在前面，所以套件里看不到）。
# 项目里另有 7 个测试文件出于同样的理由这么做。
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

#: 从左栏底部搬走的那几样控件的 id：出现在左栏渲染代码里就是违规。
ACTION_IDS = ("health", "source", "refresh", "theme", "signout", "panel-theme")

#: 人物画像页的取数口前缀——判据（2）的"不新开内部通道"就落在它上面。
MEMORY_API_PREFIX = "/api/memory/"

#: 块注释。**非贪婪**（`.*?`），否则会从文件开头那一段一直吃到后面某个 `*/`，
#: 把整整一张 `CARDS` 卡吞掉——第一版就是这么写的，"记忆"卡里的子项直接解析没了。
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)

#: 行注释（带行首缩进的那种）。
_LINE_COMMENT = re.compile(r"(?m)^[ \t]*//.*$")


def _uncomment(text: str) -> str:
    """去掉注释——注释里出现控件 id、接口路径、`data-tab` 这类词不该让断言误判
    （这个仓库的注释里恰好到处是它们）。"""

    return _LINE_COMMENT.sub(" ", _BLOCK_COMMENT.sub(" ", text))


def _asset(name: str) -> str:
    return resources.files("qq_roleplay_bot.plugins.webui.webui").joinpath(
        name).read_text(encoding="utf-8")


def _balanced(text: str, start: int, opener: str, closer: str) -> int:
    """`text[start]` 必须是 `opener`；返回配对 `closer` 之后的下标。

    **跳过字符串与两种注释**：注释里一个 `{` 就能让计数永远配不平，
    而"配不平"与"解析器坏了"必须能分开（配不平就抛，调用方判失败）。
    """

    if text[start] != opener:
        raise ValueError(f"下标 {start} 处不是 {opener}")
    depth = 0
    index = start
    quote = ""
    while index < len(text):
        char = text[index]
        if quote:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                quote = ""
        elif char in "\"'`":
            quote = char
        elif text.startswith("/*", index):
            index = text.find("*/", index + 2)
            if index < 0:
                raise ValueError("块注释没闭合")
            index += 2
            continue
        elif text.startswith("//", index):
            index = text.find("\n", index)
            if index < 0:
                raise ValueError("行注释没换行")
            continue
        elif char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return index + 1
        index += 1
    raise ValueError("括号配不平")


def _function_body(js: str, signature: str, kind: str = "function") -> str:
    """取一个函数/方法的**函数体**（不含最外层花括号），靠配对计数而不是正则。

    `signature` 是函数名（`"drawTabs"`）。`kind="async"` 时找 `async function <名>(`。
    """

    pattern = (rf"async function {re.escape(signature)}\(" if kind == "async"
               else rf"function {re.escape(signature)}\(")
    found = re.search(pattern, js)
    if found is None:
        raise ValueError(f"app.js 里找不到 {signature}()")
    brace = js.index("{", found.end())
    return js[brace + 1:_balanced(js, brace, "{", "}") - 1]


def _cards(js: str) -> list[dict[str, object]]:
    """把 `CARDS` 解析成 `[{title, items, source}]`。

    `items` 里的子项**保留原样**（可能是 `"memory"`，也可能是 `{ id, sub }` 那种），
    判定交给 `_card_ids` / `_top_level_ids`——这里不替调用方"顺手展平"，
    否则"子项"这个信息在解析层就丢了。
    """

    plain = _uncomment(js)
    marker = re.search(r"const CARDS = \[", plain)
    if marker is None:
        raise ValueError("app.js 里找不到 `const CARDS = [`")
    array_start = plain.index("[", marker.start())
    array_end = _balanced(plain, array_start, "[", "]")

    found: list[dict[str, object]] = []
    cursor = array_start + 1
    while True:
        title = re.compile(r"\{\s*title:\s*\"([^\"]*)\"\s*,\s*items:\s*\[").search(
            plain, cursor)
        if title is None or title.start() > array_end:
            break
        items_start = plain.index("[", title.start())
        items_end = _balanced(plain, items_start, "[", "]")
        found.append({
            "title": title.group(1),
            "items": plain[items_start + 1:items_end - 1],
            "source": plain[title.start():items_end],
        })
        cursor = items_end
    if not found:
        raise ValueError("`CARDS` 里一条卡片都没解析出来")
    return found


def _entries(items_source: str) -> list[str]:
    """把 `items: [...]` 的正文切成条目（顶层逗号分段）。"""

    pieces: list[str] = []
    depth = 0
    current = ""
    for char in items_source:
        if char in "[{(":
            depth += 1
        elif char in "]})":
            depth -= 1
        if char == "," and depth == 0:
            pieces.append(current)
            current = ""
            continue
        current += char
    pieces.append(current)
    return [piece.strip() for piece in pieces if piece.strip()]


def _entry_id(entry: str) -> str:
    """一个条目的页签 id：`"memory"` 或 `{ id: "memory_profile", sub: 1 }`。"""

    literal = re.fullmatch(r"\"([a-z_]+)\"", entry)
    if literal:
        return literal.group(1)
    keyed = re.search(r"id:\s*\"([a-z_]+)\"", entry)
    return keyed.group(1) if keyed else entry


def _card_ids(card: dict[str, object]) -> list[str]:
    return [_entry_id(entry) for entry in _entries(str(card["items"]))]


def _top_level_ids(card: dict[str, object]) -> list[str]:
    """这一张卡里**不是子项**的那些口（`{ … }` 那种写法整块跳过）。"""

    return [_entry_id(entry) for entry in _entries(str(card["items"]))
            if not entry.startswith("{")]


class LeftRailTests(unittest.TestCase):
    """判据（1）：左栏只放导航；搬走的那几样功能仍在，各有新落点。"""

    def test_the_left_rail_holds_nothing_but_navigation(self) -> None:
        """`aside` 里不许有会**做事**的控件：它只装品牌与导航容器。

        导航条目是 `app.js` 运行时填进 `<nav id="tabs">` 的，所以这里断言
        `aside` 的静态部分一个控件都没有（品牌 + 一个空的 nav），
        动态那半边的判据在下面那条。
        """

        html = _asset("index.html")
        aside = re.search(r"<aside class=\"rail\">(.*?)</aside>", html, re.S)
        self.assertIsNotNone(aside, "index.html 里找不到左栏 `<aside class=\"rail\">`")
        rail = aside.group(1)
        buttons = re.findall(r"<button[^>]*>", rail)
        self.assertEqual(
            buttons, [],
            f"左栏的静态 HTML 里出现了按钮：{buttons}——左栏只放导航（用户 2026-10-06）")
        for control in ("<input", "<select", "<textarea"):
            self.assertNotIn(control, rail, f"左栏的静态 HTML 里出现了 {control}")
        # 那两枚徽章与整块脚卡都不该留下——留着就是死元素。
        for gone in ("rail-foot", "rail-actions", 'id="health"', 'id="source"'):
            self.assertNotIn(gone, rail, f"左栏里还留着 {gone}（那块已经撤了）")

    def test_every_button_the_nav_renders_is_a_navigation_entry(self) -> None:
        """`drawTabs()` 产出的每个 `<button>` 都必须带 `data-tab`。

        这条是判据（1）的**主判据**：它不点名"哪几颗按钮不许在左栏"，而是要求左栏里的
        按钮**只能是导航条目**——将来再塞一颗"重启""退出"进来，没有 `data-tab` 就当场失败。
        顺带钉住左栏不出现输入控件、不出现那几样操作控件的 id。
        """

        code = _uncomment(_function_body(_asset("app.js"), "drawTabs"))
        buttons = re.findall(r"<button[^>]*>", code)
        self.assertTrue(buttons, "`drawTabs()` 里一个 `<button>` 都没有，解析多半失效了")
        for tag in buttons:
            self.assertIn(
                "data-tab=", tag,
                f"左栏渲染出了一颗不是导航条目的按钮：{tag}——左栏只放导航")
        for control in ("<input", "<select", "<textarea", "<form"):
            self.assertNotIn(control, code, f"左栏的渲染代码里出现了 {control}")
        for action in ACTION_IDS:
            self.assertNotIn(
                f'id="{action}"', code,
                f"左栏的渲染代码里又出现了操作控件的 id `{action}`（它该在页面里）")

    def test_the_moved_controls_are_still_there_where_they_moved_to(self) -> None:
        """搬走 ≠ 删掉：那几样功能各自有新落点，且 `render()` 之后仍能被 `bind()` 认领。

        做法与项目里其余前端断言一致（认字符串），但每一处都点名它是"哪一个功能的
        新落点"，所以失败信息直接告诉人该去哪找。

        ⚠️ 「刷新」**不在这一条里了**（2026-10-06 用户："右上角的刷新先删掉反正，
        这个太丑了"）——它现在是**删掉**的，判据反过来钉在
        `test_there_is_no_permanent_refresh_button_in_the_content_head` 与
        `test_the_failure_state_grows_its_own_retry_entry`。
        """

        js = _asset("app.js")

        # 深色 → 调参页「面板」分组的开关（浏览器本地偏好，不写 data/、不进覆盖层）。
        self.assertIn('id="panel-theme"', js, "调参页里没有深色开关 `#panel-theme`")
        self.assertIn('target.id === "panel-theme"', js,
                      "深色开关没有接上（它长在页面内容里，要走 `bind()` 的委托）")
        self.assertIn('localStorage.setItem("yunru_panel_theme"', js,
                      "深色开关没有落进浏览器本地偏好")

        # 退出 → 账号页。
        self.assertIn('id="signout"', js, "账号页里没有「退出」按钮 `#signout`")
        self.assertIn('target.id === "signout"', js, "「退出」按钮没有接上")
        self.assertIn('localStorage.removeItem("yunru_panel_token")', js,
                      "「退出」没有清掉本地令牌")

        # 本体状态（bot 在运行 / 活快照）→ 总览「运行」卡。
        self.assertIn("function statusRow(", js,
                      "总览页没有接住从左栏搬来的本体状态那一行")
        self.assertIn("statusRow(health, st)", js,
                      "`viewOverview` 没有把本体状态那一行放进「运行」卡")

    def test_there_is_no_permanent_refresh_button_in_the_content_head(self) -> None:
        """内容区顶栏**不许有常驻控件**（用户 2026-10-06："右上角的刷新先删掉反正"）。

        「不许有常驻控件」的准确判据是：顶栏里除了窄屏那个 `#back`（它在桌面/中等屏
        `display:none`、只在窄屏阅读态出现，**不是常驻**），不许再有别的按钮/输入；
        而且 `app.js` 里也不许留着 `#refresh` 的绑定（删就要删干净）。
        """

        html = _asset("index.html")
        js = _asset("app.js")
        css = _asset("app.css")
        self.assertNotIn('id="refresh"', html,
                         "右上角那颗常驻「刷新」又回来了（用户要求删掉）")
        head = re.search(r'<header class="page-head">(.*?)</header>', html, re.S)
        self.assertIsNotNone(head, "index.html 里找不到内容区顶栏 `<header class=\"page-head\">`")
        buttons = re.findall(r"<button[^>]*>", head.group(1))
        for tag in buttons:
            self.assertIn('id="back"', tag,
                          f"内容区顶栏里出现了常驻控件：{tag}——要做事的话让"
                          "**失败态自己长入口**（见 `errorCard()`）")
        for control in ("<input", "<select", "<textarea"):
            self.assertNotIn(control, head.group(1),
                             f"内容区顶栏里出现了 {control}（那是常驻控件）")
        # `#back` 凭什么不算常驻：它在基础样式里是隐藏的，只在窄屏阅读态才出现。
        self.assertRegex(css, r"#back\s*\{\s*display:\s*none",
                         "`#back` 不再是「平时不出现」的那个返回按钮了")
        self.assertRegex(css, r"@media \(max-width: 699px\)[\s\S]*?body\.is-reading #back",
                         "`#back` 不再只由窄屏阅读态显出来")
        self.assertNotIn('$("refresh")', js,
                         "`#refresh` 的绑定还留着（按钮已经删了，别留死代码）")

    def test_the_failure_state_grows_its_own_retry_entry(self) -> None:
        """读不到时**那一页自己长出「重试」**（功能不裸删），平时页面上没有它。

        用户要求：删掉常驻刷新，但"抓取失败"这个场景要有重试入口——所以判据是
        `render()` 的 catch 走 `errorCard()`、那张卡里有 `#retry`、`bind()` 接住了它，
        而且**别处不产出**这个按钮（否则又变成常驻控件）。
        """

        js = _asset("app.js")
        render_body = _uncomment(_function_body(js, "render", kind="async"))
        self.assertIn("errorCard(error)", render_body,
                      "读不到时不再走失败态那张卡（`errorCard()`）")
        card_body = _uncomment(_function_body(js, "errorCard"))
        self.assertIn('id="retry"', card_body, "失败态那张卡里没有「重试」入口")
        bound = _uncomment(_function_body(js, "bind"))
        self.assertRegex(bound, r'if \(target\.id === "retry"\)',
                         "「重试」没有接上（它该走 `bind()` 的委托）")
        others = "\n".join(
            _uncomment(_function_body(js, name, kind="async"))
            for name in ("viewOverview", "viewAccount", "viewDevice", "viewSettings"))
        self.assertNotIn('id="retry"', others,
                         "「重试」跑到正常页面里去了——它只该在失败态出现")

    def test_the_status_row_reuses_the_single_overview_call(self) -> None:
        """`/api/overview` 一轮只拉一次：状态行复用 `state.overview`，不另开请求。

        由来：那两枚徽章原来在 `refreshHealth()` 里被写、`viewOverview` 再拉一次——
        同一轮两个请求。搬进总览页时如果顺手再 `await api(...)` 一遍，这条就破了。
        """

        js = _asset("app.js")
        body = _function_body(js, "refreshHealth", kind="async")
        self.assertIn('state.overview = await api("/api/overview")', body,
                      "`refreshHealth()` 不再是拉 `/api/overview` 的那一处")
        self.assertIn('state.overview || await api("/api/overview")', js,
                      "`viewOverview` 没有复用 `refreshHealth()` 拉回来的那一份")


class PersonProfilePlacementTests(unittest.TestCase):
    """判据（2）：人物画像属于「记忆」这张卡，且与「记忆」是**同一张卡下的平级项**。

    用户 2026-10-06 的两句话要一起读：*"我应该说过**人物画像属于记忆的子项目**"*
    （= 它属于「记忆」这个分区）与 *"webui那里，**人物画像和记忆 tab 是同等级的，
    都在记忆卡片下**"*（= 左栏里两个是**平级的并列项**，不是嵌在「记忆」条目下面）。

    所以这一组钉两件事：**只许出现在「记忆」那张卡里**（别处一处都不许有），
    以及**在那张卡里它就是个普通条目**——不写成 `{ id, sub }` 那种"挂在上面那一项
    下面"的形状，`TABS` 里也自己占一行。渲染上的"同级"由 `SubLevelEntriesTests` 钉。
    """

    def setUp(self) -> None:
        self.js = _asset("app.js")
        self.cards = _cards(self.js)

    def _card(self, title: str) -> dict[str, object]:
        for card in self.cards:
            if card["title"] == title:
                return card
        self.fail(f"`CARDS` 里没有标题为「{title}」的卡")

    def test_the_profile_only_ever_appears_under_memory(self) -> None:
        """`memory_profile` 只许出现在"记忆"那张卡里——别处一处都不许有。"""

        holders = [card["title"] for card in self.cards
                   if "memory_profile" in _card_ids(card)]
        self.assertEqual(
            holders, ["记忆"],
            f"`memory_profile` 出现在了这些卡里：{holders}——它属于「记忆」这一档")

    def test_the_profile_is_a_plain_entry_of_the_memory_card(self) -> None:
        """在"记忆"卡里它是**普通条目**（与「记忆」平级），不是嵌在它下面的一项。

        用户原话是*"人物画像和记忆 tab 是同等级的，都在记忆卡片下"*。所以：

        * `CARDS` 里它是**平铺的第二个口**（`"memory_profile"` 字符串），
          **不许**写成 `{ id: "memory_profile", sub: 1 }`（那是"挂在上面那一项下面"的形状，
          左栏里就会画成低一级——那一套已经被用户否掉、整个撤掉了）；
        * `TABS` 里它**自己占一行**（与别的页签一样），不再塞在「记忆」那一行的 `sub` 里；
        * 仍然不许挂到账号页去。
        """

        memory = self._card("记忆")
        self.assertIn("memory_profile", _card_ids(memory),
                      "「记忆」卡里没有人物画像这个口")
        self.assertEqual(_card_ids(memory), ["memory", "memory_profile"],
                         "「记忆」卡里的项不是「记忆 + 人物画像」这两个平级项")
        self.assertEqual(
            _top_level_ids(memory), _card_ids(memory),
            "「记忆」卡里有条目写成了 `{ id, sub }`——人物画像与「记忆」是同一张卡下的"
            "平级项（用户 2026-10-06：\"人物画像和记忆 tab 是同等级的，都在记忆卡片下\"）")

        tabs_vector = re.search(r"const TABS = \[(.*?)\n\];", self.js, re.S)
        self.assertIsNotNone(tabs_vector, "app.js 里找不到页签名表 `TABS`")
        vector = tabs_vector.group(1)
        self.assertRegex(
            vector, r"\[\s*\"memory_profile\"\s*,\s*\"人物画像\"\s*\]",
            "`TABS` 里人物画像没有自己的一行（它现在与「记忆」平级）")
        self.assertEqual(
            len(re.findall(r"\"memory_profile\"", vector)), 1,
            "`TABS` 里 `memory_profile` 出现了不止一次——它只该声明一次")
        self.assertNotIn(
            "sub", vector,
            "`TABS` 里又出现了 `sub` ——那是被用户否掉的「挂在上面那一项下面」的写法")
        self.assertNotIn("subItems", self.js,
                         "`subItems()` 那套「归一子项」的机制已经撤掉了（同一张卡里就是平级）")
        self.assertNotIn(
            "memory_profile", _card_ids(self._card("账号与设备")),
            "人物画像挂到账号页了（它属于「记忆」这一档）")

    def test_only_one_profile_block_is_rendered_at_a_time(self) -> None:
        """**同一时刻只画一个人**（用户 2026-10-06）：*"应该是一个卡片里来回切换多个人的
        画像，而不是每个人一个卡片往下翻，这得翻多久"*。

        判据落在"画几个"上：`viewPersonProfiles()` 里 `personBlock(` **只许出现一次**，
        而且必须拿**选中的那个键**（`current`）去画；谁要是把它丢进 `map()` 铺成一列
        （原来的写法是 `cards.join("")`），这一条当场红。
        """

        body = _uncomment(_function_body(self.js, "viewPersonProfiles", kind="async"))
        self.assertEqual(
            len(re.findall(r"personBlock\(", body)), 1,
            "`viewPersonProfiles()` 里画像块不止一处——同一时刻只许渲染一个人")
        self.assertIn("personBlock(who, data)", body,
                      "画像块不是拿**传进来的那个人**画的")
        self.assertIn("panelFor(current)", body,
                      "初次渲染没有拿**选中的那个人**去画")
        self.assertNotIn('cards.join("")', body,
                         "又变回「把所有画像拼成一列」的老写法了")
        # 一个人一块里的四样东西，都在 `personBlock()` 里（不在别处拼）。
        # 2026-10-09：这四样改成了**三节**（`关系` 那一节的标题从"关系两轴"简成"关系"，
        # 判据一个字没变——一个人一块里这四样都得在、且都在这一处拼）。
        block = _uncomment(_function_body(self.js, "personBlock"))
        for piece in ("整体印象", "关系", "零散记录"):
            self.assertIn(piece, block, f"画像块里少了「{piece}」")
        self.assertIn("main.revision", block, "整体印象没有带版本号")
        self.assertIn("还没成形像", block,
                      "没有画像的人那一档没有如实写「还没成形像」")

    def test_the_switcher_lists_exactly_the_people_the_api_gives(self) -> None:
        """人员列表里的人 = **有画像的 ∪ 有记录但还没成形像的**（一个都不许漏）。

        用户这一轮明确要求"名单里要有'有记录但还没成形像'的人"——所以判据是那条并集，
        以及**每一行都带着那个人的键**（`data-person`）；选中态只有一处（模板里的三元）。
        2026-10-09：控件从"横向胶囊"换成"搜索框 + 竖排列表"（用户："**主要是，横向的
        没法选人啊**"），判据跟着换成 `person-row`——**名单口径一个字没改**。
        """

        body = _uncomment(_function_body(self.js, "viewPersonProfiles", kind="async"))
        self.assertRegex(body, r"keys\.concat\(withoutProfile\)",
                         "名单不是「有画像 ∪ 有记录没画像」")
        self.assertIn("mentions.keys()", body,
                      "没画像但有记录的人没有进名单（用户要求他们也能选到）")
        self.assertRegex(body, r"ordered\.filter\(", "列表不是按那份完整名单铺的")
        self.assertIn('data-person="${esc(key)}"', body,
                      "人员行没有带那个人的键")
        self.assertIn('class="person-row', body, "人员列表不是 `person-row` 那套控件")
        self.assertEqual(
            len(re.findall(r'\? " on" : ""', body)), 1,
            "选中态不止一处（或者没了）——同一时刻只该有一行被选中")

    def test_switching_a_person_re_renders_the_block(self) -> None:
        """选人必须**重画**（内容跟着换），不是只换个高亮。"""

        body = _uncomment(_function_body(self.js, "viewPersonProfiles", kind="async"))
        self.assertIn('String(state.last.profileKey || "")', body,
                      "当前是谁没有从 `state.last.profileKey` 取")
        bound = _uncomment(_function_body(self.js, "bind"))
        handler = re.search(r'act\("person-row", \(el\) => (.*?)\)\) return;', bound, re.S)
        self.assertIsNotNone(handler, "人员行没有接上（`bind()` 里找不到）")
        self.assertIn("rememberPerson", handler.group(1),
                      "点了人没有走「记下来 + 重画」那一步")
        remember = _function_body(self.js, "rememberPerson")
        self.assertRegex(remember, r"state\.last\.profileKey = (String\(key|next)",
                         "记下来的不是「选的是谁」")
        self.assertIn("render()", remember, "换了人只改了高亮、没有重画内容")

    def test_the_profile_page_takes_its_data_from_the_existing_memory_seam(self) -> None:
        """取数只走面板**既有**的记忆读接口 `/api/memory/*`，不新开通道、不读库文件。

        "别新开内部通道"这条只能落在**前端请求的路径**上：`viewPersonProfiles` 里
        每一个 `api("…")` 都必须指向 `/api/memory/`——出现别的路径就说明有人绕过那道
        接缝去取数了（比如直接读 `data/memory/memory.sqlite3`）。
        """

        body = _uncomment(_function_body(self.js, "viewPersonProfiles", kind="async"))
        calls = re.findall(r"api\((`[^`]*`|\"[^\"]*\")", body)
        self.assertTrue(calls, "`viewPersonProfiles()` 里一个 `api(...)` 都没有")
        for call in calls:
            self.assertIn(
                MEMORY_API_PREFIX, call,
                f"人物画像页去打了 {call}——它的取数口只有面板既有的 `/api/memory/*`")
        for forbidden in ("sqlite", "open(", "readFile"):
            self.assertNotIn(forbidden, body.casefold(),
                             f"人物画像页里出现了 {forbidden}——取数只能走 `/api/memory/*`")

    def test_the_memory_page_no_longer_lists_profiles_a_second_time(self) -> None:
        """同一件事只在一个地方显示：「记忆」页不再自己列一份画像。

        用户 2026-10-01 定的是"画像与旧事分两块"；2026-10-06 画像**升成记忆的子页**
        之后，那一块就成了重复展示（改一处忘一处）。所以「记忆」页保留一句指路可以，
        但**不许**再请求 `kind=profile` 并把画像画一遍。
        """

        body = _uncomment(_function_body(self.js, "viewMemory", kind="async"))
        self.assertNotIn("kind=profile", body,
                         "「记忆」页又在按 `kind=profile` 取画像了——它在自己的子页里")
        self.assertNotIn("profiles.rows", body,
                         "「记忆」页又把画像画了一遍——同一件事只在一个地方显示")


class LearnedUnderKnowledgeTests(unittest.TestCase):
    """判据（3）：**金句 / 黑话只许出现在知识库那张卡里，且与「知识库」平级**。

    用户原话：*"金句我看应该划到**知识库**里"*、*"还有**黑话**"*，紧接着把层级也说了：
    *"**金句黑话同理**"*（同理于"人物画像和记忆 tab 是同等级的，都在记忆卡片下"）。
    两样都是她**从群里学来的东西**，由本体那条窄接缝 `registry.ui.learned` 提供。
    这里钉四件事：

    1. **位置**：`knowledge_quotes` / `knowledge_slang` 只出现在标题为「知识库」的那张
       卡里（别处一处都没有）；
    2. **同级**：在卡里它们是**平铺的普通条目**（不是 `{ id, sub }` 那种嵌套写法），
       `TABS` 里各占一行、各只声明一次——那套 `sub`（缩进/缩小/层级线）已经整个撤掉；
    3. **不新开通道**：两个页面里每个 `api(...)` 都必须指向 `/api/knowledge/`，
       页面代码里不许出现读文件/查库的字样（判据与「人物画像」那条同一套）；
    4. **接线还在**：两个页签在 `VIEWS` 里注册了，写入动作也都打在 `/api/knowledge/` 上。
    """

    #: 这几个页签的 id 与它们**只许出现**在的那张卡。
    IDS = ("knowledge_quotes", "knowledge_quotes_evidence", "knowledge_slang")
    PARENT = "知识库"
    #: 这两个页面取数/写数只许用的前缀。
    API_PREFIX = "/api/knowledge/"

    def setUp(self) -> None:
        self.js = _asset("app.js")
        self.cards = _cards(self.js)

    def _card(self, title: str) -> dict[str, object]:
        for card in self.cards:
            if card["title"] == title:
                return card
        self.fail(f"`CARDS` 里没有标题为「{title}」的卡")

    def test_the_two_learned_pages_only_ever_appear_under_knowledge(self) -> None:
        """金句与黑话**只许**出现在「知识库」那张卡里——别处一处都不许有。"""

        for page in self.IDS:
            with self.subTest(page=page):
                holders = [card["title"] for card in self.cards
                           if page in _card_ids(card)]
                self.assertEqual(
                    holders, [self.PARENT],
                    f"`{page}` 出现在了这些卡里：{holders}——它属于「知识库」这一档")

    def test_the_two_learned_pages_are_plain_entries_of_the_knowledge_card(self) -> None:
        """在「知识库」卡里，它们都是**平级的普通条目**（不是嵌在「知识库」下面）。

        用户原话*"金句黑话同理"*——同理于"人物画像和记忆 tab 是同等级的，都在记忆卡片下"。
        所以 `CARDS` 里它们是普通字符串；写成 `{ id: …, sub: 1 }` 就是被否掉的
        "挂在上面那个条目下面"的形状（左栏里会被画成低一级），当场失败。

        （2026-10-07 多了第四个口「金句审核」——它是"金句"的**证据视图**，
        与其余三个同级；这里把四个都点名，少一个/多一个都要人回来看一眼。）
        """

        knowledge = self._card(self.PARENT)
        for page in self.IDS:
            self.assertIn(page, _card_ids(knowledge),
                          f"「{self.PARENT}」卡里没有 {page} 这个口")
        self.assertEqual(_card_ids(knowledge),
                         ["knowledge", "knowledge_quotes", "knowledge_quotes_evidence",
                          "knowledge_slang"],
                         "「知识库」卡里的项不是「知识库 + 金句 + 金句审核 + 黑话」这四个平级项")
        self.assertEqual(
            _top_level_ids(knowledge), _card_ids(knowledge),
            "「知识库」卡里有条目写成了 `{ id, sub }`——它们和「知识库」是同一张卡"
            "下面的平级项（用户 2026-10-06：\"金句黑话同理\"）")
        for other in ("账号与设备", "记忆", "聊天"):
            self.assertNotIn(self.IDS[0], _card_ids(self._card(other)),
                             "金句挂到别的分区去了（用户明说过：它属于知识库）")

    def test_the_two_names_are_declared_once_and_as_ordinary_rows(self) -> None:
        """`TABS` 里它们**各占一行**、各只出现一次（与别的页签完全一样的写法）。

        "一处缩进一处拍平"是不可能的了：`sub` 那套（连同 `subItems()` 归一）已经撤掉，
        所以这里钉的是**反面**——`TABS` 里再出现 `sub`、或者名字没自己占一行，
        都当场失败。
        """

        tabs_vector = re.search(r"const TABS = \[(.*?)\n\];", self.js, re.S)
        self.assertIsNotNone(tabs_vector, "app.js 里找不到页签名表 `TABS`")
        vector = tabs_vector.group(1)
        for page, label in (("knowledge_quotes", "金句"),
                            ("knowledge_quotes_evidence", "金句审核"),
                            ("knowledge_slang", "黑话")):
            with self.subTest(page=page):
                self.assertRegex(
                    vector, r"\[\s*\"" + page + r"\"\s*,\s*\"" + label + r"\"\s*\]",
                    f"`TABS` 里 {page} 没有自己的一行（它与「知识库」平级）")
                self.assertEqual(
                    len(re.findall(r"\"" + page + r"\"", vector)), 1,
                    f"`TABS` 里 `{page}` 出现了不止一次——它只该声明一次")
                self.assertEqual(
                    len(re.findall(r"\"" + label + r"\"", vector)), 1,
                    f"`TABS` 里「{label}」这个名字出现了不止一次——它只该声明一次")
        self.assertNotIn("sub", vector,
                         "`TABS` 里又出现了 `sub` ——那是被用户否掉的「挂在上面那一项下面」的写法")
        # `labelOf` 回到"查表"这一件事：不再有归一子项那一步。
        body = _uncomment(_function_body(self.js, "labelOf"))
        self.assertIn("for (const entry of TABS)", body,
                      "`labelOf()` 不是按 `TABS` 逐行查名字了")
        self.assertNotIn("subItems", body, "`labelOf()` 里还留着 `subItems()` 那一步")

    def test_the_two_pages_only_ask_the_learned_paths(self) -> None:
        """取数/写数只走面板**既有的** `/api/knowledge/*`（背后是 `learned` 接缝）。

        "别新开内部通道"这条只能落在**前端请求的路径**上：两个页面里每一个
        `api("…")` 都必须指向 `/api/knowledge/`——出现别的路径就说明有人绕过那条接缝
        去取数了（比如直接读 `data/quote/profile.json`）。
        """

        for name in ("viewKnowledgeQuotes", "viewKnowledgeSlang", "viewQuoteEvidence"):
            with self.subTest(view=name):
                body = _uncomment(_function_body(self.js, name, kind="async"))
                calls = re.findall(r"api\((`[^`]*`|\"[^\"]*\")", body)
                self.assertTrue(calls, f"`{name}()` 里一个 `api(...)` 都没有")
                for call in calls:
                    self.assertIn(
                        self.API_PREFIX, call,
                        f"{name} 去打了 {call}——它的取数口只有 `/api/knowledge/*`")
                for forbidden in ("sqlite", "open(", "readFile", "data/"):
                    self.assertNotIn(forbidden, body.casefold(),
                                     f"{name}() 里出现了 {forbidden}——取数只能走接缝")

    def test_the_two_pages_are_wired_and_their_writes_go_the_same_way(self) -> None:
        """这几个页签在 `VIEWS` 里注册了，写入动作也只打在 `/api/knowledge/` 上。

        反向钉住"别为了加页面把功能接线弄丢"：改方向 / 恢复自动 / 停用笔记 / 黑话的三个动作，
        以及**金句审核**那五个（选一条 / 采纳 / 否定 / 改成某一档 / 恢复自动 / 停用笔记）
        都在 `bind()` 的委托里，且 `write("…")` 的路径都是知识库那个既有写口。
        """

        views = _uncomment(_function_body(self.js, "render"))
        self.assertIn("VIEWS[state.tab]", views, "`render()` 不再按 `VIEWS` 取页面")
        vector = re.search(r"const VIEWS = \{(.*?)\n\};", self.js, re.S)
        self.assertIsNotNone(vector, "app.js 里找不到 `VIEWS`")
        for page, name in (("knowledge_quotes", "viewKnowledgeQuotes"),
                           ("knowledge_quotes_evidence", "viewQuoteEvidence"),
                           ("knowledge_slang", "viewKnowledgeSlang")):
            self.assertRegex(vector.group(1), page + r":\s*" + name,
                             f"`VIEWS` 里没有把 {page} 接到 {name}()")

        bound = _uncomment(_function_body(self.js, "bind"))
        # 认的是**真的接上**的那个形状（`if (act("q-save", async (el) => {`），
        # 不是"文件里出现过这个名字"——按后者时，`if (false && act("q-auto"…` 这种
        # "把控件禁掉"的改法照样能溜过去（变异核对里抓到的）。
        for handler in ("q-save", "q-auto", "s-save", "s-wrong", "s-del",
                        "ev-accept", "ev-reject", "ev-save", "ev-auto"):
            self.assertRegex(
                bound, r"if \(act\(\"" + handler + r"\", async",
                f"`bind()` 里 {handler} 那颗控件没有接上"
                f"（要写成 `if (act(\"{handler}\", async (el) => {{ …`）")
        # 审核页的两颗**非 async** 控件：选一条、翻页（都只改 state 再重画）。
        self.assertRegex(
            bound, r"if \(act\(\"evidence-item\", \(el\) => \{",
            "`bind()` 里审核页的列表项没有接上（点了不会换人/换条）")
        self.assertRegex(
            bound, r'if \(target\.id === "ev-prev" \|\| target\.id === "ev-next"\)',
            "`bind()` 里审核页的翻页没有接上")
        for handler in ("note-toggle", "ev-note"):
            self.assertRegex(
                bound, r"if \(target\.classList\.contains\(\"" + handler + r"\"\)\)",
                f"`bind()` 里 {handler} 那个开关没有接上")
        writes = re.findall(r"write\(\"(/api/[^\"]*)\"", bound)
        self.assertTrue(writes, "`bind()` 里一个 `write(...)` 都没有")
        learned_writes = [path for path in writes if path.startswith("/api/knowledge/")]
        self.assertGreaterEqual(
            len(learned_writes), 5,
            f"金句/黑话/审核的写入没有全走 `/api/knowledge/`：{writes}")


class SubLevelEntriesTests(unittest.TestCase):
    """判据（4）：**同一张卡下面的项是平级的**（2026-10-06 用户口径，两处都算）。

    用户原话：*"webui那里，**人物画像和记忆 tab 是同等级的，都在记忆卡片下**"*、
    *"**金句黑话同理**"*。层级由**卡片**（`CARDS` 的 `title`）表达——同一张卡里的项，
    在左栏里**渲染完全一样**（与「聊天」卡里的「总览 / 群聊 / 私聊」同一套样式）。

    ⚠️ 这一条被理解反过一轮：曾按"挂在上面那个条目下面"给「人物画像」「金句」「黑话」
    做了缩进 / 图标收小 / 层级线（`data-sub` + `{ id, sub }` + `subItems()`），并钉了
    "子项必须与父项不同级"。那是错的，已经**整个撤掉**。现在钉的是**反面**：
    谁要是再给某一张卡里的某一项单开一档，这两条当场红——这也就同时保证了
    "两处用同一种做法"（因为已经不存在"另一套做法"）。
    """

    def setUp(self) -> None:
        self.js = _asset("app.js")
        self.css = _asset("app.css")

    def test_every_entry_in_a_card_is_drawn_by_the_same_template(self) -> None:
        """`drawTabs()` 里只有一个按钮模板，而且没有"是不是子项"的分岔。"""

        body = _uncomment(_function_body(self.js, "drawTabs"))
        self.assertNotIn("data-sub", body,
                         "左栏又给某一项挂了 `data-sub`（那会把它画成低一级）")
        self.assertNotIn("rail-sub-group", body,
                         "左栏又给某一项套了「子项组」的壳（同一张卡里应当平级）")
        self.assertNotIn("item.sub", body,
                         "`drawTabs()` 里又按某个字段分岔了——那就是层级分支")
        buttons = re.findall(r"<button[^>]*>", body)
        self.assertEqual(len(buttons), 1,
                         f"`drawTabs()` 里出现了不止一个按钮模板：{buttons}"
                         "——同一张卡里的项就不一样了")
        for tag in buttons:
            self.assertIn("data-tab=", tag, f"左栏渲染出了一颗不是导航条目的按钮：{tag}")

    def test_no_entry_gets_its_own_indent_or_size(self) -> None:
        """`app.css` 里不许有"按某一项/某一类项"写的缩进与尺寸。

        ⚠️ 扫的是**去掉注释之后**的 CSS：那一段注释里正好写着 `[data-sub]`
        （"谁要再给它单开一档…当场红"），按原文扫会把注释也算成违规。
        """

        css = re.sub(r"/\*.*?\*/", " ", self.css, flags=re.S)   # 注释里的话不算数
        self.assertNotIn("[data-sub]", css,
                         "`app.css` 里还留着 `[data-sub]` 那一套缩进/尺寸")
        self.assertEqual(re.findall(r"\[data-tab=[^\]]*\]", css), [],
                         "`app.css` 里出现了按具体页签写死的样式（等于给某一项单开一档）")
        self.assertNotIn("rail-sub-group", css,
                         "`app.css` 里还留着「子项组」那套（层级线 / 再缩进）")


class ReadoutGridTests(unittest.TestCase):
    """读数格（`.kv`）：**最后一行不满就填满**，而列边界仍落在同一套网格线上。

    由来（2026-10-06 外观返工）：总览「运行」卡是 5 格，三列排下来最后一行的右边空着
    一块（截图里看着像少放了一个格子）；而"让格子自己 `flex-grow`"又会把两格撑成
    1.5 格、与上一行错开（上一轮试过、被否掉）。现在是 `readoutGrid()` 在生成时
    算出来"最后一格跨几列"。这条钉住这个机制的三处：算的地方、跨度样式、窄屏复位。
    """

    def setUp(self) -> None:
        self.js = _asset("app.js")
        self.css = _asset("app.css")

    def test_the_last_row_of_a_readout_grid_is_filled(self) -> None:
        body = _uncomment(_function_body(self.js, "readoutGrid"))
        self.assertIn("span: per - rest + 1", body,
                      "`readoutGrid()` 不再给最后一格算跨度了（最后一行的右边会留洞）")
        self.assertRegex(
            self.css, r"\.grid \.kv\.span-2\s*\{[^}]*flex:[^;]*calc\(",
            "`app.css` 里没有 `span-2` 的宽度（两格宽 + 一个间隙）")
        self.assertRegex(
            self.css, r"@media \(max-width: 699px\)[\s\S]*?\.grid \.kv\.span-2",
            "窄屏那条媒体查询没有把 `span-2` 压回整行"
            "（`span-2` 比 `.grid .kv` 多一个类，特异性更高，不点名就压不住）")


class AccountAndDeviceTests(unittest.TestCase):
    """账号页 / 设备页的两条规格（2026-10-06 用户，他要拿小米平板对着 MIUI 比）。

    账号页（用户原话）：*"正常情况下**账号是不在下面的卡片里**其实，**显示一个头像一个
    昵称单开一个比正常 tab 高一点的卡片**"* —— 所以钉：**第一张就是资料头卡**
    （头像 + 她自己 + 状态，`card("")` 没有标题、排在最前）、头像是**浏览器**去取的
    （后端代码里不许出现那个 CDN）、她的号只从**既有的** `/api/self` 接缝来。

    设备页（用户只说"太丑"，没给规格）：照 MIUI「关于本机」= **分组卡 + 图标行 + 右侧值**，
    钉：`VIEWS.device` 指向 `viewDevice`、它用 `prefRow`/`pref-list` 而不是把键值对
    堆成一张表、只读既有的两个口（`/api/overview` + `/api/storage`）、并且**没接上的
    那几样如实指路**（`/super`）而不是摆一张空表。
    """

    #: 账号页允许请求的读口（顺序无关）。
    ACCOUNT_PATHS = ("/api/self", "/api/overview", "/api/settings")
    #: 设备页允许请求的读口。
    DEVICE_PATHS = ("/api/overview", "/api/storage")

    def setUp(self) -> None:
        self.js = _asset("app.js")

    def _paths(self, body: str) -> list[str]:
        return re.findall(r"api\(\"(/api/[^\"]*)\"", body)

    def test_the_account_page_starts_with_a_profile_card(self) -> None:
        body = _uncomment(_function_body(self.js, "viewAccount", kind="async"))
        # 头卡是第一张：`card("", …)`（**没有标题**）必须是 body 里第一个 `card(`。
        first = re.search(r"card\(\s*\"\"\s*,", body)
        self.assertIsNotNone(first, "账号页第一张不是「资料头卡」（该是 `card(\"\", …)`）")
        earlier = body[:first.start()]
        self.assertNotIn("card(", earlier, "资料头卡之前还有别的卡片了——它该排在最前")
        for part in ("profile-avatar", "avatar-img", "profile-name", "profile-tags"):
            self.assertIn(part, body, f"资料头卡里少了 `{part}`")
        # 昵称那条读口还没有 → 显示号；但**不许编**（要么她自己的号，要么"还没查到"）。
        self.assertIn("还没查到她的号", body,
                      "查不到她自己时没有如实说（会变成空白或编一个名字）")
        self.assertNotIn("viewStub(", body,
                         "账号页又变回「还没接」的占位卡了")
        # 旧占位里那几样"计划放这些"的英文键名不许再出现。
        for gone in ("provider_registry", "self_id\""):
            self.assertNotIn(gone, body, f"账号页里还留着占位文案 `{gone}`")

    def test_the_avatar_is_fetched_by_the_browser_not_the_backend(self) -> None:
        """头像由**浏览器**取（用户明确要求），后端一个字节都不许去抓。"""

        body = _uncomment(_function_body(self.js, "viewAccount", kind="async"))
        self.assertIn("q1.qlogo.cn", body, "资料头卡里没有 QQ 头像的地址（浏览器取头像那一步）")
        for name in ("webui_panel.py", "webui_data.py", "wire.py"):
            source = resources.files("qq_roleplay_bot.plugins.webui").joinpath(
                name).read_text(encoding="utf-8")
            self.assertNotIn("qlogo", source,
                             f"{name} 里去抓头像了——头像只能由浏览器取（用户要求）")

    def test_the_identity_comes_only_from_the_existing_self_seam(self) -> None:
        body = _uncomment(_function_body(self.js, "viewAccount", kind="async"))
        paths = self._paths(body)
        self.assertTrue(paths, "账号页一个 `api(...)` 都没有")
        for path in paths:
            self.assertIn(path, self.ACCOUNT_PATHS,
                          f"账号页去打了 {path}——它只该用 {self.ACCOUNT_PATHS}")

    def test_the_device_page_is_grouped_rows_not_a_key_value_dump(self) -> None:
        vector = re.search(r"const VIEWS = \{(.*?)\n\};", self.js, re.S)
        self.assertIsNotNone(vector, "app.js 里找不到 `VIEWS`")
        self.assertRegex(vector.group(1), r"device:\s*viewDevice",
                         "`VIEWS` 里 device 没有接到 `viewDevice()`")
        body = _uncomment(_function_body(self.js, "viewDevice", kind="async"))
        self.assertIn("prefRow(", body, "设备页不是「图标行」的写法（MIUI 那种）")
        self.assertIn("pref-list", body, "设备页没有分组卡的壳（`pref-list`）")
        self.assertNotIn("viewStub(", body, "设备页又变回占位卡了")
        self.assertNotIn("kv(", body, "设备页把键值对堆成表了——该用分组行")
        # 没有接缝的那几样要**如实指路**，不是摆一张空表。
        self.assertIn("/super processes", body,
                      "设备页没有说清「进程 / 网量」这类面板读不到、该去哪看")
        for path in self._paths(body):
            self.assertIn(path, self.DEVICE_PATHS,
                          f"设备页去打了 {path}——它只该用 {self.DEVICE_PATHS}")


def _css_rule(css: str, selector: str) -> str:
    """取一条 CSS 规则的正文。**行首锚定**（避免命中别人规则里的同名前缀），
    取**最后**一条（CSS 里后来者胜）。

    ⚠️ 先把注释抹掉：注释里写着的 `{ overflow: hidden }` 那种例子会把
    `[^}]*` 截断在一半（第一版就是这么红的，而规则其实是对的）。
    """

    body = re.sub(r"/\*.*?\*/", " ", css, flags=re.S)
    blocks = re.findall(r"(?m)^" + re.escape(selector) + r"\s*\{([^}]*)\}", body)
    if not blocks:
        raise AssertionError(f"app.css 里找不到 `{selector}` 这条规则")
    return blocks[-1]


class KnowledgeTableLayoutTests(unittest.TestCase):
    """知识库那张「全库浏览」表：**不许叠字、不许裁到卡片外**（2026-10-07 用户："知识库文字重叠"）。

    用户窗口是 **1267×796**（在 1024 与 1440 之间）。我按他截图里的形状自己造了一份
    知识库索引夹具、在 1267×796 上复现出**三处**，各自的根因都不是"样式没调好"：

    1. **来源压着标题**：那一列标了 `nowrap`，而来源是**路径**（`03-能力与装备/武器与装备
       清单.md`），比列宽长得多 → `white-space: nowrap` 让它不折行、**画到旁边那一列上**；
    2. **表格里半行字"压住"下一行**：`.md.clamp { max-height: 180px; overflow: hidden }`
       正好切在字中间 → 半行字看着像和下一行叠在一起；
    3. **内容被右边缘裁掉**：`.md-table` 是 `table-layout: auto`，内容偏好宽度可以**超过**
       `width: 100%`，表格比单元格还宽，再被 `.md.clamp` 的 `overflow: hidden` 裁掉。

    所以这一组钉的是**这三条的根因**（不是"把字切掉当修好"）。
    ⚠️ 如实标注近似：判据 ①（不许叠字）在离线套件里**测不到真实像素**，
    这里钉的是"不许用 `nowrap` 挤爆列宽"这个**可测的充分条件**；
    真正量到的是截图时那次探针（`data/tmp_learned_shot.mjs`：四个档位
    `table.scrollWidth <= clientWidth`、`rightOverflow` 为空，见提交信息里的读数）。
    """

    def setUp(self) -> None:
        self.js = _asset("app.js")
        self.css = _asset("app.css")

    def test_the_source_column_wraps_instead_of_painting_over_the_next_one(self) -> None:
        body = _uncomment(_function_body(self.js, "viewKnowledge", kind="async"))
        row = re.search(r"\{ label: \"来源\".*?\}", body, re.S)
        self.assertIsNotNone(row, "知识库那张表里找不到「来源」列")
        column = row.group(0)
        self.assertNotIn("nowrap", column,
                         "「来源」列又标了 `nowrap`——路径比列宽长，会画到「标题」列上")
        self.assertIn("wide: true", column,
                      "「来源」列没有 `wide`（= `overflow-wrap: anywhere`）："
                      "路径里没有空格，光靠 `break-word` 断不开")

    def test_markdown_tables_are_contained_instead_of_clipped(self) -> None:
        table_rule = _css_rule(self.css, ".md-table")
        self.assertRegex(
            table_rule, r"table-layout:\s*fixed",
            "`.md-table` 不是 `table-layout: fixed`：auto 布局下它可能比单元格还宽，"
            "再被 `.md.clamp` 的 `overflow: hidden` 裁掉（用户截图里的「内容被右边缘裁掉」）")
        cells = _css_rule(self.css, ".md-table th, .md-table td")
        self.assertRegex(
            cells, r"overflow-wrap:\s*anywhere",
            "`.md-table` 的单元格没有 `overflow-wrap: anywhere`——长 token 会顶出格子")

    def test_the_clamped_preview_fades_instead_of_cutting_through_a_line(self) -> None:
        clamp = _css_rule(self.css, ".md.clamp")
        # ⚠️ 必须**行首锚定属性名**：不锚的话 `-webkit-mask-image: linear-gradient…`
        # 也会被 `mask-image:` 匹配上，于是把 `mask-image` 那一行删掉照样绿
        # （变异核对抓到的：12 条里只有它没红）。
        self.assertRegex(
            clamp, r"(?m)^\s*mask-image:\s*linear-gradient",
            "`.md.clamp` 还是硬切（`overflow: hidden` + 固定 max-height）："
            "切在字中间时，半行字看着像和下一行「压在一起」。要留给它一层淡出")
        self.assertNotRegex(
            clamp, r"max-height:\s*180px",
            "`.md.clamp` 又回到了 180px——那个高度正好切在行中间（实测过）")

    def test_the_knowledge_column_widths_live_in_css_so_narrow_screens_can_override(self) -> None:
        body = _uncomment(_function_body(self.js, "viewKnowledge", kind="async"))
        self.assertIn('cols: "cols-knowledge"', body,
                      "知识库那张表没有用 `cols-knowledge`")
        self.assertNotRegex(
            body, r"widths:\s*\[\s*\"\d+%\"",
            "知识库那张表又把列宽写回 `widths` 了——那是 `<col style>`（行内样式），"
            "比 CSS 优先级高，窄屏那条媒体查询就压不住它了")
        for index, width in ((1, "22%"), (2, "24%"), (3, "54%")):
            self.assertRegex(
                self.css,
                r"\.cols-knowledge col:nth-child\(" + str(index) + r"\)\s*\{[^}]*"
                + re.escape(width),
                f"`.cols-knowledge` 第 {index} 列不是 {width}")
        self.assertRegex(
            self.css, r"@media \(max-width: 699px\)[\s\S]*?\.cols-knowledge col:nth-child\(1\)",
            "窄屏那条媒体查询里没有给「来源」列加宽（500px 时路径会被切成七八行）")

    def test_the_users_own_window_width_falls_in_the_wide_tier(self) -> None:
        """**1267×796 是用户真实的窗口**——把那个宽度钉进档位。

        判据：中等档的上界必须 **小于 1267**（于是 1267 落在宽屏档：左栏固定 426px、
        剩下的全给内容），而且内容区**不许有 `max-width`**（有的话 1267 这个尺寸上
        内容会白白少一截、表格更容易挤）。档位一改，这个尺寸上的排版就得重新看过。
        """

        medium = re.search(r"@media \(min-width: 700px\) and \(max-width: (\d+)px\)", self.css)
        self.assertIsNotNone(medium, "找不到中等档那条媒体查询")
        self.assertLess(
            int(medium.group(1)), 1267,
            f"中等档的上界是 {medium.group(1)}px，1267 就落进中等档了——"
            "用户的窗口会被当成平板处理")
        self.assertRegex(self.css, r"\.rail\s*\{[^}]*flex:\s*0 0 426px",
                         "宽屏档的左栏不是固定 426px")
        # 页脚那行"视口 … · 档位"的话**不许把档位判据写死成一个数**：
        # 它原来比的是 `basis === "240px"`，宽屏改成 426px 之后 1440/1267 被说成"中等"
        # （截图核对读到的原话："视口 1267×796 · DPR 1 · 中等（左栏 426px）"）。
        footer = _uncomment(_function_body(self.js, "showViewport"))
        self.assertNotRegex(footer, r'basis === "\d+px"',
                            "页脚又拿一个死数字当档位判据了（宽窄一改就说错话）")
        self.assertRegex(footer, r"/\^?px\$/\.test\(basis\)|endsWith\(\"px\"\)",
                         "页脚没有按「定宽还是比例」判档位")
        for block in re.findall(r"(?m)^\.content[^{]*\{([^}]*)\}", self.css):
            self.assertNotIn("max-width", block,
                             "内容区被限宽了：1267 这个尺寸下内容会白白少一截")


class QuoteEvidencePageTests(unittest.TestCase):
    """金句审核页（2026-10-06 用户：*"你连原文和上下文都不贴出来怎么审核"*）。

    这一页要干的事只有一件：把**证据**摆出来，让人能对着它判"这个表情在这个群是什么意思"
    判得对不对。所以钉三条：

    1. **同一时刻只渲染一条证据**（用户明确讨厌"一条一块往下翻"；谁把它丢进 `map()`
       铺成一列就红）——列表是**紧凑的一行一条**，详情是 `evidenceBlock(currentRow, …)` **一次**；
    2. **列表与详情用同一份数据**：列表的每一项带着那条证据的键（`data-evidence` =
       `message_id`），详情是**拿着这个键去同一个数组里 find 出来的**——
       不是"另取一份"（那样点第 N 条就可能展示第 M 条）；
    3. **不新开通道**：读只走 `/api/knowledge/quotes/evidence`（+ 同一接缝的
       `/api/knowledge/quotes` 拿笔记启停态），写只走既有那条 `POST /api/knowledge/quotes`
       ——面板里**没有**第二个写口（这一条在 `test_webui_panel.py` 里对路由钉）。
    """

    def setUp(self) -> None:
        self.js = _asset("app.js")

    def test_only_one_evidence_block_is_rendered_at_a_time(self) -> None:
        body = _uncomment(_function_body(self.js, "viewQuoteEvidence", kind="async"))
        self.assertEqual(
            len(re.findall(r"evidenceBlock\(", body)), 1,
            "`viewQuoteEvidence()` 里证据块不止一处——同一时刻只许渲染一条")
        self.assertIn("evidenceBlock(currentRow", body,
                      "证据块不是拿**选中的那一条**画的")
        block = _uncomment(_function_body(self.js, "evidenceBlock"))
        for piece in ("她说的", "她开口之前群里在说", "她说完之后群里接的", "被点了",
                      "为什么没有结论"):
            self.assertIn(piece, block, f"证据块里少了「{piece}」")
        self.assertIn('class="quote-text"', block,
                      "她的原话没有用不截断的那块（`.quote-text`）贴出来")

    def test_the_list_and_the_detail_use_the_same_data(self) -> None:
        body = _uncomment(_function_body(self.js, "viewQuoteEvidence", kind="async"))
        self.assertRegex(body, r"rows\.map\(", "列表不是按那一页的数据铺的")
        self.assertIn('data-evidence="${esc(key)}"', body,
                      "列表项没有带那条证据的键（`message_id`）")
        # 详情 = 拿键在**同一个数组**里找出来；找不到才退回第一条。
        self.assertRegex(
            body, r"rows\.find\(\(row\) => String\(row\.message_id\) === wanted\)",
            "详情不是从**同一份数据**里按 `message_id` 找出来的——"
            "那样点第 N 条可能展示第 M 条")
        self.assertIn("|| rows[0]", body, "选中的键不在这一页时没有退回第一条")
        bound = _uncomment(_function_body(self.js, "bind"))
        self.assertIn("state.last.evidenceKey = el.dataset.evidence", bound,
                      "点了列表项没有记下选的是哪一条")

    def test_the_evidence_page_never_opens_a_new_channel(self) -> None:
        body = _uncomment(_function_body(self.js, "viewQuoteEvidence", kind="async"))
        calls = re.findall(r"api\((`[^`]*`|\"[^\"]*\")", body)
        self.assertTrue(calls, "审核页一个 `api(...)` 都没有")
        for call in calls:
            self.assertIn("/api/knowledge/", call,
                          f"审核页去打了 {call}——它只该用既有那条接缝")
        for forbidden in ("sqlite", "open(", "readFile", "chat.jsonl", "data/"):
            self.assertNotIn(forbidden, body.casefold(),
                             f"审核页里出现了 {forbidden}——证据由核心那条接缝给，"
                             "面板不许自己去读日志文件")
        # 写侧：只在 `bind()` 里，且路径就是那一条既有写口。
        bound = _uncomment(_function_body(self.js, "bind"))
        evidence_writes = re.findall(
            r'write\("(/api/[^"]*)", \{\s*action: "(?:correct|note)"', bound)
        self.assertTrue(evidence_writes, "审核页的写入没有走 `action: correct|note`")
        self.assertEqual(set(evidence_writes), {"/api/knowledge/quotes"},
                         f"审核页出现了别的写口：{sorted(set(evidence_writes))}")


if __name__ == "__main__":
    unittest.main()


class PersonPickerTests(unittest.TestCase):
    """判据（2'）：人员选择是**搜索框 + 竖排列表**。

    用户 2026-10-09 指着截图说：*"**主要是，横向的没法选人啊**"*——所以那条 33 人的
    横向胶囊带**不是"不好看"，是"选不了人"**（触屏上找不到人、滚动也不明显）。
    这一组钉住换掉之后那几件必须成立的事：

    1. 横向胶囊带**撤了**（`person-chip` / `person-bar` 一个字都不许留）；
    2. **输入即筛**：名字 / 名片 / 号都能搜（筛的文本就是这三样拼出来的）；
    3. 按**最近活跃**排，**默认选最近活跃那个**；
    4. 列表是**竖排**的，每行"**称呼 + 尾部号**"，点按区够大（≥48px）；
    5. **键盘能走**（↑↓ + 回车），而且走的是"筛出来的那几个人"；
    6. 选中的那个人**写进地址栏**（`#<页签>&people=<号>`），载入时从那里读回来
       —— 可分享、刷新不丢。
    """

    def setUp(self) -> None:
        self.js = _asset("app.js")
        self.css = _asset("app.css")

    def test_the_horizontal_chip_bar_is_gone(self) -> None:
        self.assertNotIn("person-chip", self.js, "横向胶囊带还在（它正是“选不了人”那条）")
        self.assertNotIn("person-bar", self.css, "旧胶囊带的样式还留着")

    def test_the_search_box_filters_as_you_type(self) -> None:
        body = _function_body(self.js, "viewPersonProfiles", kind="async")
        self.assertIn('id="person-search"', body, "没有搜索框")
        self.assertIn("profileQuery", body, "搜索词没进 state（重画就丢了）")
        self.assertRegex(body, r"haystack|includes\(query\)", "没有“输入即筛”这一步")
        # 名字 / 名片 / 号三样都要进筛的文本。
        self.assertRegex(body, r"personLabel\(key, data\)", "筛的文本里没有“称呼”")
        self.assertRegex(body, r"data\.names\[key\]", "筛的文本里没有“名片”")
        self.assertRegex(body, r"\.\.\.\[\]|\[personLabel\(key, data\), key",
                         "筛的文本里没有“号”")
        bind = _uncomment(_function_body(self.js, "bind"))
        self.assertIn('addEventListener("input"', bind, "输入事件没接（打字列表不会变）")

    def test_the_default_pick_is_the_most_recently_active_person(self) -> None:
        body = _function_body(self.js, "viewPersonProfiles", kind="async")
        self.assertRegex(body, r"const activity = \(key\) => Math\.max",
                         "没有“最近活跃”这个读数")
        self.assertRegex(
            body, r"const current = ordered\.includes\(wanted\) \? wanted : \(ordered\[0\] \|\| \"\"\)",
            "默认选中的不是“最近活跃”那一个")

    def test_the_list_is_vertical_with_name_and_tail_and_big_targets(self) -> None:
        body = _function_body(self.js, "viewPersonProfiles", kind="async")
        self.assertIn("person-row", body, "没有竖排行")
        self.assertIn("personTail", body, "行里没有“尾部号”")
        self.assertIn('role="listbox"', body, "列表没有 listbox 语义")
        self.assertRegex(self.css, r"\.person-list\s*\{[^}]*flex-direction: column", "列表不是竖排")
        self.assertRegex(self.css, r"\.person-row\s*\{[^}]*min-height: 48px", "点按区不够大")
        self.assertRegex(self.css, r"\.person-row \+ \.person-row::before\s*\{[^}]*left: 14px",
                         "分隔线没有从文字处缩进")

    def test_keyboard_walks_the_filtered_list_and_enter_picks(self) -> None:
        bind = _uncomment(_function_body(self.js, "bind"))
        for key in ('"ArrowDown"', '"ArrowUp"', '"Enter"'):
            self.assertIn(key, bind, f"键盘少了 {key}")
        self.assertIn("personRowsInDom", bind, "键盘走的不是“筛出来的那几个人”")

    def test_the_picked_person_lands_in_the_url_and_survives_a_reload(self) -> None:
        self.assertRegex(self.js, r"history\.replaceState\(null, \"\", \"#\" \+ state\.tab",
                         "选完没写地址栏")
        self.assertIn("people=", self.js, "地址栏里没有那个人的号")
        self.assertRegex(self.js, r"profileKey: decodeURIComponent",
                         "载入时没有从地址栏读回来")
        self.assertIn("render()", _function_body(self.js, "rememberPerson"),
                      "选完没有重画（内容不跟着换）")


class PersonRepaintTests(unittest.TestCase):
    """判据（2''）：**换人只重画那一块**（用户 2026-10-09："换人不刷新"）。

    原来的写法是选一个人就整页 `render()` —— 那是**重新取数 + 重建整页**：搜索框里
    打的字、光标、列表滚动位置全丢，看起来就像刷新。这一组钉住新的写法：
    页面里留一个 `#person-panel` 容器；`viewPersonProfiles` 装上 `profilePaint`；
    `rememberPerson` **先原地重画**，只有这一块不在页面上才退回整页。
    """

    def setUp(self) -> None:
        self.js = _asset("app.js")

    def test_the_page_has_a_container_that_can_be_repainted_alone(self) -> None:
        body = _function_body(self.js, "viewPersonProfiles", kind="async")
        self.assertIn('id="person-panel"', body, "没有可单独重画的容器")
        self.assertRegex(body, r"profilePaint = \(who\) =>", "没有装上「只重画这一块」的画法")

    def test_the_repaint_path_never_refetches(self) -> None:
        """重画那一段里**一处 `api()` 都不许有**——有它就是又去取数了（那就是"刷新"）。"""

        body = _function_body(self.js, "viewPersonProfiles", kind="async")
        paint = re.search(r"profilePaint = \(who\) => \{(.*?)\n  \};", body, re.S)
        self.assertIsNotNone(paint, "找不到 profilePaint 的实现")
        self.assertNotIn("api(", paint.group(1), "重画那一段里又去取数了")
        # **`render()` 也是取数路径**（它整页重来），所以一起禁掉——只查 `api(` 会漏掉
        # "重画时调 render()"这种写法（2026-10-09 突变时发现的缺口）。
        self.assertNotIn("render(", paint.group(1), "重画那一段里调了整页 render()")
        self.assertIn("innerHTML", paint.group(1), "重画那一段没有换内容")

    def test_the_repaint_keeps_the_search_box_and_the_scroll(self) -> None:
        body = _function_body(self.js, "viewPersonProfiles", kind="async")
        paint = re.search(r"profilePaint = \(who\) => \{(.*?)\n  \};", body, re.S).group(1)
        self.assertIn("person-search", paint, "重画没有顾及搜索框")
        self.assertIn("focus()", paint, "重画之后焦点没还回去")
        self.assertIn("scrollTop", paint, "重画把列表滚动位置丢了")

    def test_choosing_a_person_repaints_that_block_first(self) -> None:
        remember = _function_body(self.js, "rememberPerson")
        self.assertIn("profilePaint", remember, "选人没有走「只重画这一块」")
        self.assertIn("render()", remember, "连「画法不在」时的退路都没有")
        # 退路必须在原地重画**之后**（先试重画，装不上才整页）。
        self.assertLess(remember.index("if (profilePaint && profilePaint("),
                        remember.index("\n  render();"),
                        "顺序反了：先整页 render 再重画")
        handlers = _uncomment(_function_body(self.js, "bind"))
        self.assertIn("profilePaint = null", handlers, "换页签时没有把画法清掉")


class PersonPageShapeTests(unittest.TestCase):
    """判据（3）：人物画像页那四条外观（用户 2026-10-09 指着截图点的 ①~④）。

    ① 页面上**不再铺设计说明**——那几段收进一个「?」（`noteTip` / `PROFILE_NOTES`），
       点开才看得见；
    ② 头部三样 = **称呼**（她的叫法 / 名片）+ **QQ 号** + **所在群**；昵称那种噪声
       （「有人转发了！」）放**次要行**、折叠起来，**不许当标题**；
    ③ **没有读数就整块不出现**（「关系两轴：还没有…」那句斜体占位删掉）；
    ④ 三节用**分段**，**一次只显示一块**（`.pane[hidden]` + 点一下只换那一节）。
    """

    def setUp(self) -> None:
        self.js = _asset("app.js")
        self.css = _asset("app.css")

    def test_the_design_notes_are_hidden_behind_a_question_mark(self) -> None:
        body = _uncomment(_function_body(self.js, "viewPersonProfiles", kind="async"))
        self.assertIn("noteTip(", body, "说明没有收进「?」")
        self.assertIn("${PROFILE_NOTES}", body, "说明段没有走那份只给「?」用的文案")
        self.assertNotRegex(body, r'<p class="hint">这里是',
                            "设计说明又直接铺在页面上了")
        self.assertIn("const PROFILE_NOTES", self.js, "说明文案没有单独的常量")
        self.assertRegex(self.css, r"\.note-tip > summary", "「?」没有样式")

    def test_the_head_shows_the_name_the_number_and_the_groups(self) -> None:
        block = _uncomment(_function_body(self.js, "personBlock"))
        self.assertIn("person-head-name", block, "头部没有称呼那一行")
        self.assertIn("esc(label)", block, "标题不是**称呼**（她的叫法 / 名片）")
        self.assertIn("person-head-meta", block, "头部没有号码与群那一行")
        self.assertIn('class="mono">${esc(key)}', block, "头部没有 QQ 号")
        self.assertIn("person-group", block, "头部没有群 chips")
        self.assertIn("person-nicks", block, "昵称没有折叠的次要行")
        self.assertLess(block.index("person-head-name"), block.index("nicknames.map(esc)"),
                        "昵称跑到称呼前面去了（那就等于拿它当标题）")
        self.assertRegex(self.css, r"\.person-group\s*\{", "群 chips 没有样式")

    def test_a_person_without_a_reading_gets_no_placeholder(self) -> None:
        block = _uncomment(_function_body(self.js, "personBlock"))
        self.assertNotIn("还没有关系两轴读数", block, "那句占位又回到了画像块里")
        self.assertNotIn("还没有关系两轴读数", self.js, "那句占位还留在 app.js 里")
        self.assertRegex(block, r"if \(relation_\) \{",
                         "关系那一节没有「有读数才出现」这一道")
        self.assertIn("panes.push", block, "三节不是同一张表里出来的")

    def test_the_sections_are_tabs_and_only_one_is_visible(self) -> None:
        block = _uncomment(_function_body(self.js, "personBlock"))
        self.assertIn("person-tabs", block, "三节没有做成分段")
        self.assertIn('role="tab"', block, "分段没有 tab 语义")
        self.assertRegex(block, r'id === showing \? "" : " hidden"',
                         "没有把不在看的那几节藏起来")
        self.assertRegex(self.css, r"\.pane\[hidden\]\s*\{\s*display: none;",
                         "藏起来的那几节没有 display: none")
        handlers = _uncomment(_function_body(self.js, "bind"))
        self.assertIn('act("person-tab"', handlers, "分段没有接线")
        tabs = re.search(r'act\("person-tab", \(el\) => \{(.*?)\}\)\) return;',
                         handlers, re.S)
        self.assertIsNotNone(tabs, "分段那一段找不到")
        self.assertIn("pane.hidden =", tabs.group(1), "点分段没有换显示的那一节")
        self.assertNotIn("render(", tabs.group(1), "点分段去整页重画了")


class PersonRecordsTests(unittest.TestCase):
    """判据（3）：零散记录表那两件事（用户 2026-10-09 指着截图点的 ⑤⑥）。

    ⑤ 记录表：**类别筛选**（这一节里出现过的类别各一枚、带条数）+
    **长内容默认两行、点开看全** + 显示「**共 N 条**」（取满 200 就如实说一句）；
    ⑥ 观感照 HyperOS：点按区够大、行高松一点（无描边大圆角那几条在整体样式里）。
    """

    def setUp(self) -> None:
        self.js = _asset("app.js")
        self.css = _asset("app.css")

    def test_the_records_can_be_filtered_by_kind(self) -> None:
        block = _uncomment(_function_body(self.js, "personBlock"))
        self.assertIn("ownKinds", block, "没有按类别归拢过")
        self.assertIn("kind-chip", block, "没有类别筛选那几枚")
        self.assertIn("state.last.profileKind", block, "挑了哪一类没有记下来")
        self.assertIn("shownOwn", block, "表不是按挑中的那一类铺的")
        self.assertRegex(block, r"(?s)table\(\[.*?\], shownOwn,",
                         "表铺的还是全部记录")
        # 点类别：**原地重画**，不取数。
        handlers = _uncomment(_function_body(self.js, "bind"))
        self.assertIn('act("kind-chip"', handlers, "类别筛选没有接线")
        picked = re.search(r'act\("kind-chip", \(el\) => \{(.*?)\}\)\) return;', handlers, re.S)
        self.assertIsNotNone(picked, "类别那一段找不到")
        self.assertIn("state.last.profileKind = el.dataset.kind", picked.group(1),
                      "点类别没有换挑选项")
        # **原地重画优先**：`render()` 只许当"画法装不上"时的退路，而且要排在重画**之后**
        # （这跟 `rememberPerson` 那条同一条规矩：先重画，装不上才整页）。
        self.assertIn("profilePaint(state.last.profileKey)", picked.group(1),
                      "点类别没有原地重画")
        self.assertNotIn("api(", picked.group(1), "点类别去取数了")
        self.assertLess(picked.group(1).index("profilePaint(state.last.profileKey)"),
                        picked.group(1).index("render();"),
                        "顺序反了：先整页 render 再重画（那还是刷新）")

    def test_long_content_is_clamped_until_you_open_it(self) -> None:
        block = _uncomment(_function_body(self.js, "personBlock"))
        self.assertIn('class="clamp"', block, "内容没有折起来的壳")
        self.assertIn("html: true", block, "那个壳会被转义掉（`table()` 的 `html` 开关）")
        self.assertRegex(self.css, r"\.clamp\s*\{[^}]*-webkit-line-clamp: 2", "默认不是两行")
        self.assertRegex(self.css, r"\.clamp\.open\s*\{[^}]*line-clamp: unset", "点开没有看全")
        handlers = _uncomment(_function_body(self.js, "bind"))
        self.assertIn('target.closest(".clamp")', handlers, "点开那一下没有接线")
        self.assertIn('classList.toggle("open")', handlers, "点开没有切换 class")

    def test_the_count_line_tells_how_many_rows_are_here(self) -> None:
        block = _uncomment(_function_body(self.js, "personBlock"))
        self.assertRegex(block, r"共 \$\{num\(shownOwn\.length\)\} 条", "没有「共 N 条」")
        self.assertIn("PROFILE_LIMIT", block, "取满一页时没有如实说一句")
        self.assertIn("kindChips", block, "记录那一节没有放筛选")
        self.assertIn("countLine", block, "记录那一节没有放计数那一行")

    def test_the_touch_targets_and_rows_are_comfortable(self) -> None:
        self.assertRegex(self.css, r"\.kind-chip\s*\{[^}]*min-height: 40px",
                         "类别那几枚点按区太小")
        self.assertRegex(self.css, r"\.cols-profile th, \.cols-profile td \{[^}]*line-height: 1\.55",
                         "记录表行高还是挤的")
        self.assertRegex(self.css, r"\.clamp\s*\{[^}]*cursor: pointer",
                         "折起来那一段没有「可以点」的样子")


class GroupPageLayoutTests(unittest.TestCase):
    """判据：群聊页把**群名**放主标题位、**群号**放次要位，且列的是**全部群**。

    用户 2026-10-09：*"群聊页，应当展示云茹所在的**所有**群聊并展示**群名**而不是群号"*。
    """

    def setUp(self) -> None:
        self.js = _asset("app.js")
        self.css = _asset("app.css")

    def _body(self) -> str:
        return _uncomment(_function_body(self.js, "viewGroup", kind="async"))

    def test_the_name_is_the_title_and_the_number_is_secondary(self) -> None:
        body = self._body()
        # 主标题位（`pref-title`）里放的是**群名**；群号在 `pref-summary` 里。
        title = re.search(r'<div class="pref-title">(.*?)</div>', body, re.S)
        self.assertIsNotNone(title, "找不到群名那一行的主标题位")
        self.assertIn("row.name", title.group(1), "主标题不是群名")
        # 群号只许当**没名字时的回落**（`row.name || \`群 ${row.id}\``），不许直接当标题。
        self.assertRegex(title.group(1), r"row\.name\s*\|\|",
                         "群名不是主标题（群号只该在没名字时回落）")
        self.assertNotRegex(title.group(1), r"esc\(row\.id\)", "主标题直接就是群号")
        summary = re.search(r'<div class="pref-summary">(.*?)</div>', body, re.S)
        self.assertIsNotNone(summary, "找不到群号那一行的次要位")
        self.assertIn("row.id", summary.group(1), "群号不在次要位")
        self.assertIn("copyable", summary.group(1), "群号没有做成可复制的小字")
        self.assertRegex(self.css, r"\.copyable\s*\{[^}]*user-select: all",
                         "群号那段不是「点一下就能复制」")

    def test_every_group_is_listed_not_only_the_ones_with_dialogue(self) -> None:
        body = self._body()
        self.assertIn('api("/api/groups")', body, "群列表没有走那条只读接缝")
        self.assertEqual(body.count('api("/api/groups")'), 1,
                         "群列表取了不止一次")
        self.assertNotIn("enabled_group_ids", body,
                         "又变回「只列开过对话的群」了——那一页要的是**全部群**")
        # 也不许**筛**：接缝给多少就列多少（筛掉没开对话的就是同一个毛病）。
        self.assertNotIn(".filter(", body, "群列表被筛过了（那一页要的是全部群）")
        self.assertIn("群聊", _asset("app.js"), "页签不见了")

    def test_the_two_states_are_badges_and_memory_stays_read_only(self) -> None:
        body = self._body()
        self.assertIn("dialogue_on", body, "没有标出「对话」那个状态")
        self.assertIn("memory_on", body, "没有标出「记忆」那个状态")
        # 2026-10-09：对话那个状态**已经是开关**（用户要"真开关"），所以徽标只剩「记忆」这一枚。
        self.assertEqual(body.count("group-badge"), 1,
                         "「记忆」那一枚徽标不在（它只读、只有它能是徽标）")
        self.assertIn("group-switch", body, "对话那个状态没做成开关")
        # 记忆那个**只读**：这一页不许出现写记忆的入口（面板没有按群开关记忆这回事）。
        self.assertNotIn("memory", body.replace("memory_on", ""),
                         "群聊页出现了碰记忆的入口（那个只有只读的份）")
        self.assertRegex(self.css, r"\.group-badge\s*\{", "徽标没有样式")

    def test_every_row_has_a_dialogue_switch_on_the_existing_post(self) -> None:
        """用户 2026-10-09：*"光列出来不设置开关啊…非要设置一个输入群号开启吗"*。

        每一行都要有一个**真开关**，写出去用**既有的** `POST /api/groups`
        （`write()` 会带 `confirm: true`，那口自带 confirm 校验与审计）——
        **不新增写入口、也不是两套控件**（原来那颗"关闭"并进同一个开关里）。
        """

        body = self._body()
        self.assertIn("group-switch", body, "每一行没有对话开关")
        self.assertIn('type="checkbox"', body, "开关不是勾选框那套控件")
        self.assertIn("row.dialogue_on ? 1 : 0", body, "开关没有记住它原来是开还是关")
        self.assertIn('row.dialogue_on ? " checked" : ""', body, "开着的那几行没打勾")
        self.assertNotIn("group-off", body, "旧的「关闭」按钮还在（两套控件）")
        handlers = _uncomment(_function_body(self.js, "bind"))
        self.assertIn("group-switch", handlers, "开关没有接线")
        changed = re.search(r'addEventListener\("change", async \(event\) => \{(.*?)\n  \}\);',
                            handlers, re.S)
        self.assertIsNotNone(changed, "找不到开关那个 change 委托")
        inner = changed.group(1)
        self.assertEqual(inner.count('write("/api/groups"'), 1,
                         "写出去的不是那条既有 POST（或者不止一处）")
        self.assertIn("enabled: on", inner, "写出去的开关状态不对")
        # **失败不翻状态**：把滑块放回原来那一格 + 在行里说一句人话。
        self.assertIn('box.checked = box.dataset.on === "1"', inner,
                      "写失败也把状态翻过去了（乐观更新）")
        self.assertIn("sayInRow", inner, "写失败没有在行里说一句")
        # **成功只重画那一行**：不整页 render、也不重取整张表。
        self.assertIn("groupRowPaint", inner, "成功之后没有只重画那一行")
        self.assertNotIn("render();\n", inner.replace(
            "if (!(groupRowPaint && groupRowPaint(box.dataset.id, on))) render();", ""),
            "成功之后还去整页 render 了")
        self.assertNotIn('api("/api/groups"', inner, "成功之后又去取了一遍整张表")
        self.assertRegex(self.css, r"\.row-switch\s*\{[^}]*min-height: 44px",
                         "开关那一块点按区太小")


class MemoryGroupSwitchLayoutTests(unittest.TestCase):
    """判据：**记忆页**里那个按群的记忆开关（用户："记忆开关肯定要上面板啊，在记忆页"）。

    它读的是 `ui.groups` 的那份 `memory_on`（与群聊页同一个数据源），写的是
    **另一条**路径 `/api/memory/groups`（对话那颗走 `/api/groups`）——两件事两个写路径。
    """

    def setUp(self) -> None:
        self.js = _asset("app.js")
        self.css = _asset("app.css")

    def _memory(self) -> str:
        return _uncomment(_function_body(self.js, "viewMemory", kind="async"))

    def test_the_memory_page_lists_every_group_with_a_memory_switch(self) -> None:
        body = self._memory()
        self.assertIn("group-memory-switch", body, "记忆页没有那个按群开关")
        self.assertIn('api("/api/groups")', body, "群列表没走 `ui.groups` 那条只读接缝")
        self.assertIn("memory_on", body, "开关读的不是 `memory_on`")
        self.assertIn("memory_on ? 1 : 0", body, "开关没记住它原来是开还是关")
        self.assertIn('memory_on ? " checked" : ""', body, "开着的那几行没打勾")
        # 群名在主标题位、群号在次要位（与群聊页同一套）。
        title = re.search(r'<div class="pref-title">(.*?)</div>', body, re.S)
        self.assertIsNotNone(title, "记忆页那一行没有主标题位")
        self.assertIn("row.name", title.group(1), "主标题不是群名")
        self.assertIn("copyable", body, "群号没有做成可复制的小字")

    def test_the_switch_writes_on_its_own_path_not_the_dialogue_one(self) -> None:
        body = self._memory()
        self.assertIn("memoryGroupPaint", body, "成功之后没有只重画那一行")
        handlers = _uncomment(_function_body(self.js, "bind"))
        changed = re.search(
            r'group-memory-switch"\)\) return;(.*?)\n  \}\);', handlers, re.S)
        self.assertIsNotNone(changed, "找不到记忆开关那个 change 委托")
        inner = changed.group(1)
        self.assertEqual(inner.count('write("/api/memory/groups"'), 1,
                         "写出去的不是那条按群记忆的路径（或者不止一处）")
        self.assertNotIn('write("/api/groups"', inner, "复用了对话那条写路径")
        self.assertIn("enabled: on", inner, "写出去的开关状态不对")
        self.assertIn('box.checked = box.dataset.on === "1"', inner,
                      "写失败也把状态翻过去了（乐观更新）")
        self.assertIn("sayInRow", inner, "写失败没有在行里说一句")
        self.assertIn("memoryGroupPaint", inner, "成功之后没有只重画那一行")
        self.assertNotIn("render();", inner.split("memoryGroupPaint(box.dataset.id, on)")[0],
                         "还没重画就整页 render 了")

    def test_the_group_page_keeps_only_a_badge_so_there_is_one_write_place(self) -> None:
        """一处写：群聊页那颗「记忆」只是**徽标**（不给开关），开关只在记忆页。"""

        group = _uncomment(_function_body(self.js, "viewGroup", kind="async"))
        self.assertNotIn("group-memory-switch", group, "两页各放了一个记忆开关")
        self.assertIn("group-badge", group, "群聊页那颗「记忆」徽标不见了")
        self.assertIn("要改去「记忆」页", group, "徽标没有指一句去哪改")

    def test_the_switch_is_comfortable_to_tap(self) -> None:
        self.assertRegex(self.css, r"\.row-switch\s*\{[^}]*min-height: 44px",
                         "开关那一块点按区太小")
        self.assertIn("input.switch", self.css, "开关不是那套既有的滑块")


class RecordsStatusLayoutTests(unittest.TestCase):
    """判据：页面侧也跟着"默认只看生效的"（2026-10-09 用户报的"空正文的记录"）。"""

    def setUp(self) -> None:
        self.js = _asset("app.js")

    def test_the_memory_page_asks_for_history_explicitly(self) -> None:
        """「记忆」页那个"全部状态"页签必须**显式**发哨兵（空串在 `parse_qs` 那步就没了）。"""

        body = _uncomment(_function_body(self.js, "viewMemory", kind="async"))
        self.assertIn('status=${encodeURIComponent(statusFilter || "any")}', body,
                      "「全部状态」没有显式发哨兵（那一档会看不到历史）")
        self.assertIn('state.last.memoryStatus || "active"', body,
                      "默认那一档不是 `active`")

    def test_the_profile_page_no_longer_counts_superseded_rows(self) -> None:
        """画像页现在只看生效的：不再另取"被顶替的旧版"，那句计数也删了，
        改成在「?」里说一句"历史去「记忆」页看"（判据：不 quietly 少一块信息）。"""

        body = _uncomment(_function_body(self.js, "viewPersonProfiles", kind="async"))
        self.assertNotIn("superseded", body, "画像页还在取被顶替的旧版")
        block = _uncomment(_function_body(self.js, "personBlock"))
        self.assertNotIn("superseded", block, "画像块还在用被顶替的旧版")
        self.assertIn("全部状态", self.js, "没有一句指路说历史去哪儿看")


class ProfileBadgeAndGroupChipTests(unittest.TestCase):
    """用户 2026-10-09 截图报的两处 + 一处间距：

    ① 标签页徽标与内容不是一个口径（「零散记录 (1)」点进去「共 0 条」）；
    ② 群标签出现「群 <这个人的号>」——`user_global` 那种个人 scope 的 `scope_key`
       存的就是人的号，被当成群号了；
    ③ 搜索框与列表贴死、列表最上面那行被截一半。
    """

    def setUp(self) -> None:
        self.js = _asset("app.js")
        self.css = _asset("app.css")

    def test_the_tab_count_uses_the_same_rows_as_the_pane(self) -> None:
        block = _uncomment(_function_body(self.js, "personBlock"))
        self.assertRegex(block, r'\["records", "零散记录", `\(\$\{num\(shownOwn\.length\)\}\)`',
                         "徽标数的不是那一节看得见的条数")
        self.assertRegex(block, r"共 \$\{num\(shownOwn\.length\)\} 条", "计数与列表不是一个口径")
        # 换人必须把类别筛选清掉（不然会给下一个人筛出 0 条，徽标却还说有）。
        remember = _function_body(self.js, "rememberPerson")
        self.assertIn("state.last.profileKind = \"\"", remember, "换人没清掉类别筛选")

    def test_group_chips_only_ever_come_from_group_scopes(self) -> None:
        body = _uncomment(_function_body(self.js, "viewPersonProfiles", kind="async"))
        self.assertIn("groupOfRecord", body, "群标签不是从来源字段判出来的")
        picker = re.search(r"const groupOfRecord = \(row\) => \{(.*?)\n  \};", body, re.S)
        self.assertIsNotNone(picker, "找不到 `groupOfRecord`")
        inner = picker.group(1)
        self.assertIn('=== "group"', inner, "没有认 `scope_type === 'group'`")
        self.assertIn('=== "user_group"', inner, "没有认 `scope_type === 'user_group'`")
        self.assertIn("split(\":\")[0]", inner, "`user_group` 没取冒号前那段（群号）")
        # **个人 scope 必须落空**：不许出现"别的都当群号"那种兜底。
        self.assertNotRegex(inner, r"return raw;\s*\n\s*\};", "个人 scope 也被当成群了")
        self.assertNotIn("row.scope_key)", body, "还有地方直接把 scope_key 当群号用")

    def test_the_list_has_breathing_room_under_the_search_box(self) -> None:
        self.assertRegex(self.css, r"\.person-list\s*\{[^}]*margin-top: 6px",
                         "搜索框与列表之间还是贴死的")
        self.assertRegex(self.css, r"\.person-list\s*\{[^}]*padding: 2px 0",
                         "列表没有上下留白（最上面那行会被截）")
