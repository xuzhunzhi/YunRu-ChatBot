"""控制面板（`plugins/webui`）的离线覆盖：**路由层 + 接缝 + 授权**，不起线程、不连网。

为什么补这个文件（2026-10-06）：这条分支上**一条面板测试都没有**
（`wire.py` 的注释一直写着"见 test_webui_panel.py"，而那个文件在插件侧重建时没了），
于是两个接缝断掉都没有任何东西拦：

1. `webui_data.read_health` 里 `from ...onebot_ws import port_in_use`——那个函数
   **从来没存在过**（核心那边叫 `stage3_main._port_in_use`）→ `/health` 与
   `/api/overview` 500；而前端那道登录门正是拿 `/api/overview` 验令牌的，
   **令牌再对也进不去**（截图：一直停在登录页）；
2. `webui_data.read_usage` 里 `from ...api_usage import APICHECK_ROLES`——本体把它
   搬去了 `stage3_main`，这里没跟着改 → `/api/usage` 500（总览页整块用量读不出来）。

两条都不是"启动就炸"的那种：面板的 import 大多在函数体里，**点到了才 500**。
所以这里除了逐个钉住这两条，还加了一条"**任何读接口都不许回 500**"的冒烟，
以及一条"**插件里每一条 import 的目标都得还在**"的静态扫描——那正是这两次断线的
共同形状（核心搬了名字，插件这边没人知道）。
"""

from __future__ import annotations

import ast
import importlib
import json
import os
import pathlib
import re
import socket
import sys
import tempfile
import unittest
from importlib import resources
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from qq_roleplay_bot.plugins.webui import webui_data  # noqa: E402
from qq_roleplay_bot.plugins.webui.webui_access import LOCAL, WebAccess  # noqa: E402
from qq_roleplay_bot.plugins.webui.webui_panel import Panel  # noqa: E402

#: 测试用令牌（`WebAccess.configured` 要求至少 16 个字符）。
TOKEN = "test-token-0123456789abcdef"

#: 走一遍全部**读**接口。每一项都只读文件/接缝，不产生任何外部动作。
READ_PATHS = (
    "/health",
    "/api/overview",
    "/api/state",
    "/api/plugins",
    "/api/usage",
    "/api/settings",
    "/api/prompts",
    "/api/prompts/versions?name=reply",
    "/api/knowledge/overview",
    "/api/knowledge/chunks",
    "/api/knowledge/operator",
    "/api/approvals/pending",
    "/api/actions/preview",
    "/api/history",
    "/api/logs?feature=judge",
    "/api/memory/overview",
    "/api/memory/problem?filter=stale",
    "/api/memory/records",
    "/api/memory/archive",
    "/api/memory/inbox",
    "/api/memory/audit",
    "/api/memory/relationships",
    "/api/memory/people",
    "/api/memory/tombstones",
    "/api/session/messages?session_id=group:717151356",
)


class _PanelCase(unittest.TestCase):
    """造一个**真的 `Panel`**（真路由、真授权），seams 只给数据根目录。

    数据根目录指到临时目录：面板的读接口一律只读文件，指过去就不会读到这台机器上
    真实的 `data/`（跑测试不该碰它，`wire.py` 里同一条纪律）。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        self._env = mock.patch.dict(os.environ, {"QQBOT_DATA_DIR": str(self.root)})
        self._env.start()
        self.addCleanup(self._env.stop)
        self.addCleanup(self._tmp.cleanup)
        self.panel = Panel({"data_root": str(self.root)},
                           WebAccess(mode=LOCAL, token=TOKEN))

    def request(self, method: str, path: str, body: bytes = b"", *,
                token: str | None = TOKEN, accept: str = "application/json"):
        headers = {"accept": accept}
        if token is not None:
            headers["authorization"] = "Bearer " + token
        return self.panel.handle(method, path, body, headers, "127.0.0.1")

    def payload(self, response) -> dict:
        return json.loads(response.body.decode("utf-8"))


class ReadRoutesTests(_PanelCase):
    def test_no_read_route_answers_500(self) -> None:
        """**任何读接口都不许回 500**——断了接缝的那两个正是这么暴露的。"""

        for path in READ_PATHS:
            with self.subTest(path=path):
                response = self.request("GET", path)
                self.assertNotEqual(
                    response.status, 500,
                    f"{path} 回了 500（多半是某条 import 的目标没了）："
                    f"{response.body[:200]!r}")

    def test_health_reports_a_real_port_probe(self) -> None:
        """`/health` 得能落地：bot_port 是配置里的端口，bot_live 是探出来的真假。"""

        response = self.request("GET", "/health")
        self.assertEqual(response.status, 200)
        data = self.payload(response)["data"]
        self.assertIn("bot_port", data)
        self.assertIsInstance(data["bot_live"], bool)
        self.assertIn("data_dir", data)

    def test_overview_works_because_the_login_gate_probes_it(self) -> None:
        """前端登录门拿 `/api/overview` 验令牌——它 500 就等于**面板打不开**。"""

        response = self.request("GET", "/api/overview")
        self.assertEqual(response.status, 200)
        data = self.payload(response)["data"]
        for key in ("health", "state", "usage", "flags", "access"):
            self.assertIn(key, data)

    def test_usage_lists_the_agents_from_the_core_catalog(self) -> None:
        """角色名必须来自核心那份 `APICHECK_ROLES`，不是面板自己抄的一份。"""

        from qq_roleplay_bot.stage3_main import APICHECK_ROLES

        response = self.request("GET", "/api/usage")
        self.assertEqual(response.status, 200)
        rows = self.payload(response)["data"]["role_list"]
        self.assertEqual([(row["role"], row["label"]) for row in rows[:len(APICHECK_ROLES)]],
                         list(APICHECK_ROLES))

    def test_unknown_api_path_is_404_not_500(self) -> None:
        self.assertEqual(self.request("GET", "/api/nope").status, 404)


class AuthorizationTests(_PanelCase):
    def test_api_without_a_token_is_unauthorized(self) -> None:
        response = self.request("GET", "/api/overview", token=None)
        self.assertEqual(response.status, 401)
        self.assertEqual(self.payload(response)["error"], "unauthorized")

    def test_a_wrong_token_is_unauthorized(self) -> None:
        response = self.request("GET", "/api/overview", token="x" * 30)
        self.assertEqual(response.status, 401)

    def test_the_shell_is_public_so_the_login_page_can_load(self) -> None:
        """外壳（HTML/CSS/JS）与 `/health` 不认证：不然"页面要令牌、令牌要在页面里填"死锁。"""

        for path in ("/", "/index.html", "/app.css", "/app.js", "/health"):
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path, token=None).status, 200)

    def test_api_paths_are_not_public(self) -> None:
        for path in ("/api/state", "/api/plugins", "/api/prompts"):
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path, token=None).status, 401)

    def test_static_is_a_whitelist_not_a_path_join(self) -> None:
        """静态资源**只认三个名字**，没有路径拼接——所以也就没有穿越。

        带着令牌问（认证在路由之前：未认证的请求一律 401，**不告诉外面某个路径存不存在**，
        那是 fail-closed 的一半，另一条用例钉住它）。
        """

        for path in ("/../.env", "/app.css/../index.html", "/secrets.txt", "/webui_data.py"):
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path).status, 404)

    def test_a_write_without_confirm_is_refused(self) -> None:
        """危险动作必须带 `confirm: true`——挡住的是"前端忘了问"，不是"用户点错了"。"""

        body = json.dumps({"group_id": "717151356", "enabled": True}).encode()
        response = self.request("POST", "/api/groups", body)
        self.assertEqual(response.status, 400)
        self.assertEqual(self.payload(response)["error"], "needs_confirmation")


class PortProbeTests(unittest.TestCase):
    """`webui_data._port_in_use`：**bind 探法**（不产生半截连接，见那个函数的说明）。"""

    def test_a_listening_port_reads_as_in_use(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            port = listener.getsockname()[1]
            self.assertTrue(webui_data._port_in_use("127.0.0.1", port))

    def test_a_free_port_reads_as_free(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        self.assertFalse(webui_data._port_in_use("127.0.0.1", port))


class PanelSeamImportTests(unittest.TestCase):
    """插件里**每一条 import 的目标都必须还在**。

    面板的 import 大多写在函数体里（点到了才执行），所以"核心把某个名字搬走"
    在启动时不会报错——只会在用户点到那一页时变成 500。这个扫描把每条
    `from ...x import y` 的目标解析一遍，正是上面两次断线的共同形状。
    """

    PKG = "qq_roleplay_bot"

    def _plugin_files(self):
        root = pathlib.Path(webui_data.__file__).resolve().parents[1]
        return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)

    def test_every_import_target_still_exists(self) -> None:
        src = pathlib.Path(webui_data.__file__).resolve().parents[3]
        missing: list[str] = []
        for path in self._plugin_files():
            current = path.resolve().relative_to(src).with_suffix("")
            parts = list(current.parts)
            if parts[-1] == "__init__":
                parts.pop()
            current_name = ".".join(parts)
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.ImportFrom) or not node.level:
                    continue                      # 绝对 import（标准库那种）不相对本模块
                base = current_name.split(".")[: len(current_name.split(".")) - node.level]
                target = ".".join(base + (node.module.split(".") if node.module else []))
                if not target.startswith(self.PKG):
                    continue
                try:
                    module = importlib.import_module(target)
                except Exception as exc:          # noqa: BLE001
                    missing.append(f"{path.name}:{node.lineno} 模块导不进来 {target}（{exc}）")
                    continue
                for alias in node.names:
                    if hasattr(module, alias.name):
                        continue
                    try:                          # `from ... import 子模块` 取的是模块对象
                        importlib.import_module(f"{target}.{alias.name}")
                    except Exception:             # noqa: BLE001
                        missing.append(f"{path.name}:{node.lineno} 名字没了 {target}.{alias.name}")
        self.assertEqual(missing, [], "插件里引用了核心已经不存在的名字：" + "；".join(missing))


class LayoutIsCssDrivenTests(unittest.TestCase):
    """左栏宽度：**由 CSS 驱动、跨断点连续**。

    由来（2026-10-06 用户）："从**平板视图切换到大屏视图**时，左边栏会缩回去，变得很窄"。
    先说清**实测**的结论（无头 Edge + CDP，在**同一个文档**里改视口）：
    `document.querySelector(".rail").getAttribute("style")` 全程都是空串、`body.className`
    也是空串——**不是**残留内联宽度，也不是类没摘。真正的原因是 CSS 两档宽度不连续：
    中等档 `40%` 在 1099px 处约 425px，而宽屏档写的是 240px（Miuix 平板栏那个 token），
    于是越过 1100px 时栏体从 ~386px 掉到 240px。

    这里钉两条**能在离线套件里算出来**的判据（真机宽度只能靠截图/CDP，见提交信息里的数）：

    1. 宽屏档的固定宽度 ≥ 中等档在断点处的宽度（否则一过界就"缩回去"）；
    2. 布局不许由 JS 写内联尺寸驱动（那才会真的出现"跨断点残留"这一整类 bug）。
    """

    #: 中等档那条媒体查询的上界（`max-width: 1099px`）。
    BREAKPOINT_MAX = 1099

    @staticmethod
    def _asset(name: str) -> str:
        return resources.files("qq_roleplay_bot.plugins.webui.webui").joinpath(
            name).read_text(encoding="utf-8")

    def _body_padding(self, css: str) -> int:
        """`body` 的左右内边距——下面那条算术要用它（百分比针对内层宽度解析）。"""

        for block in re.findall(r"\nbody\s*\{([^}]*)\}", css):
            found = re.search(r"\bpadding:\s*(\d+)px", block)
            if found:
                return int(found.group(1))
        self.fail("app.css 里找不到 body 的 padding")

    def test_the_wide_tier_keeps_the_width_the_medium_tier_ends_with(self) -> None:
        css = self._asset("app.css")
        padding = self._body_padding(css)
        self.assertEqual(padding, 18,
                         "body 内边距变了：这条用例下面的算术（断点 − 2×内边距）要跟着改")

        wide = re.search(r"\.rail\s*\{([^}]*)\}", css)
        self.assertIsNotNone(wide, "找不到 .rail 的基础样式")
        fixed_px = re.search(r"flex:\s*0 0 (\d+)px", wide.group(1))
        self.assertIsNotNone(
            fixed_px,
            "宽屏档的 .rail 必须是**固定宽度**（flex: 0 0 <N>px）——写成百分比就不是"
            "'到达一定尺寸后固定宽度'了")

        medium = re.search(
            r"@media \(min-width: 700px\) and \(max-width: (\d+)px\)\s*\{(.*?)\n\}",
            css, re.S)
        self.assertIsNotNone(medium, "找不到中等档那条媒体查询")
        ratio = re.search(r"\.rail\s*\{[^}]*flex:\s*0 0 ([\d.]+)%", medium.group(2))
        self.assertIsNotNone(ratio, "中等档的 .rail 应当是 40% 那种比例宽度")

        at_breakpoint = float(ratio.group(1)) / 100 * (self.BREAKPOINT_MAX - 2 * padding)
        self.assertGreaterEqual(
            float(fixed_px.group(1)), at_breakpoint - 2.0,
            f"宽屏档 {fixed_px.group(1)}px 比中等档在断点处的 {at_breakpoint:.0f}px 窄："
            "窗口从平板宽度拉到大屏时左栏会**缩回去**（用户 2026-10-06 报的那个 bug）")

    def test_the_frontend_never_writes_inline_sizes(self) -> None:
        """`app.js` 里不该有 `.style.<属性> = …` 这种赋值。

        面板的布局全靠 class + CSS（换页签、切阅读态、切主题都是 `classList`）。
        一旦有人开始"量一下再写死宽度"，跨断点就会留下内联宽度——那正是这次被怀疑的机制
        （实测不是它，但它确实是**真正会出现那类 bug** 的写法）。这条用例把那条路堵上。
        """

        js = self._asset("app.js")
        writes = re.findall(r"\.style\.[A-Za-z]+\s*[^=]*=[^=]", js)
        self.assertEqual(writes, [], f"app.js 里出现了内联样式写入：{writes}")
        self.assertNotIn(".style.setProperty(", js, "app.js 里出现了内联样式写入")


if __name__ == "__main__":
    unittest.main()
