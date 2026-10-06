"""左栏布局与「记忆 → 人物画像」从属关系的**静态断言**（不起浏览器、不连网）。

两条判据是用户当天定的，各有一个容易被后来的改动悄悄破掉的地方：

1. **左栏里不许出现操作类控件**（2026-10-06 用户："左边边栏下面的这个卡片不明所以，
   我为什么要在左边栏显示 bot 在运行，显示个活快照，显示个刷新深色和退出。这个不应该
   在 system 页面吗。。"）。左栏从此**只放导航**（"在左边切换设置项"）。
   破坏方式很具体：再往 `aside` 或 `drawTabs` 里塞一颗按钮——所以这里钉的是
   "左栏产出的每个 `<button>` 都必须带 `data-tab`"，而不是"某几颗按钮不在左栏"。
   同时反向钉住那几样**功能没被删掉**（别为了清空左栏把功能一起砍了），
   它们各自有新的落点：本体状态 → 总览「运行」卡；刷新 → 内容区顶栏；
   深色 → 调参页「面板」分组的开关；退出 → 账号页。

2. **人物画像不许出现在记忆之外的分区**（用户："我应该说过人物画像属于记忆的子项目"；
   事实核对：`memory_model.py` 里它是第七类 `kind='profile'`）。
   判据落在**数据**上（`CARDS`）：`memory_profile` 只能出现在标题为"记忆"的那张卡里、
   带 `sub: 1`（= 子项），而且不是顶层页签、不挂账号页。

为什么走静态断言：这两条都是**结构**约束，离线套件里能算出来；真机像素宽度只能靠
截图（见提交信息里那三张 1440 / 1024 / 500）。`test_webui_panel.py` 里那条宽度断言
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
        """搬走 ≠ 删掉：四样功能各自有新落点，且 `render()` 之后仍能被 `bind()` 认领。

        做法与项目里其余前端断言一致（认字符串），但每一处都点名它是"哪一个功能的
        新落点"，所以失败信息直接告诉人该去哪找。
        """

        html = _asset("index.html")
        js = _asset("app.js")

        # 刷新 → 内容区顶栏（`render()` 重画的就是"当前这一页"，所以它属于每一页自己）。
        self.assertRegex(
            html, r'<button[^>]*id="refresh"[^>]*>',
            "内容区顶栏没有「刷新」按钮：它从左栏搬到了 `<header class=\"page-head\">`")
        self.assertIn('$("refresh").addEventListener("click", render)', js,
                      "「刷新」按钮没有接上 `render`")

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
    """判据（2）：人物画像属于记忆，只能是它的子项。"""

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
            f"`memory_profile` 出现在了这些卡里：{holders}——它只能是「记忆」的子项")

    def test_the_profile_is_a_child_entry_not_a_top_level_page(self) -> None:
        """在"记忆"卡里，它必须是**子项**写法（`{ id: "memory_profile", sub: 1 }`）。

        为什么判这个：用户原话是"**子项目**"。若写成平铺的第二个口，`CARDS` 上看起来
        就是同层的两个页——层级没了，栏里也就没法缩进表达。同样地，**显示名表 `TABS`
        里它也只许写在"记忆"那一行的 `sub` 里**，不许自己占一行（占一行就是"顶层页"
        的形状）；也不许挂账号页。
        """

        memory = self._card("记忆")
        self.assertIn("memory_profile", _card_ids(memory),
                      "「记忆」卡里没有人物画像这个口")
        self.assertEqual(_top_level_ids(memory), ["memory"],
                         "「记忆」卡里除 'memory' 之外还有平铺的口——人物画像要写成子项")
        self.assertRegex(
            str(memory["items"]), r"\{\s*id:\s*\"memory_profile\"\s*,\s*sub:\s*1\s*\}",
            "人物画像没标成子项（该写成 `{ id: \"memory_profile\", sub: 1 }`）")

        tabs_vector = re.search(r"const TABS = \[(.*?)\n\];", self.js, re.S)
        self.assertIsNotNone(tabs_vector, "app.js 里找不到页签名表 `TABS`")
        self.assertRegex(
            tabs_vector.group(1), r"\[\s*\"memory\"\s*,\s*\"记忆\"[^\]]*sub:\s*\[\s*\"memory_profile\"",
            "`TABS` 里人物画像没有写在「记忆」那一行的 `sub` 里")
        self.assertEqual(
            len(re.findall(r"\"memory_profile\"", tabs_vector.group(1))), 1,
            "`TABS` 里 `memory_profile` 出现了不止一次——它只该在「记忆」的 sub 里声明一次")
        self.assertNotIn(
            "memory_profile", _card_ids(self._card("账号与设备")),
            "人物画像挂到账号页了（用户明说过：它是记忆的子项）")

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


if __name__ == "__main__":
    unittest.main()
