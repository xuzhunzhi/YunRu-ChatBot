"""`/v2/` 静态路由（Compose/wasm 外壳）的离线覆盖。

为什么有这个文件（2026-10-18）：`/v2/` 是**用户批准的唯一一处后端改动**，范围是写死的：

1. `/v2`、`/v2/` → `v2/index.html`；子路径按**扩展名白名单**放行（不是通配）；
2. `.wasm` / `.ttf` 走 `read_bytes`（`application/wasm` / `font/ttf`），文本仍按 UTF-8 读；
3. 防穿越：`resolve()` 之后必须仍在 `v2/` 里，否则 404；
4. 缓存：`index.html` 必须 `no-store`，带内容哈希的 `.wasm` 才强缓存，其余 `no-cache` + ETag；
5. **老面板那四个路径一个字节都不许变** —— 这条不是靠"我看了没问题"，
   而是靠**改代码之前**先跑一遍真 HTTP（`ThreadingHTTPServer` + `_Handler`）把
   「状态 + Content-Type + 头 + 正文」的 sha256 抓下来，下面 `LEGACY_BASELINE` 就是那份基线。

基线怎么算的（与抓取脚本同一算法，改这个算法等于放弃这条锁）：

    sha256(str(status) + b"\\0" + content_type + b"\\0"
           + json.dumps(sorted(response.headers)).encode() + b"\\0" + body)

注意基线里那两条 `Cache-Control: no-store`：老面板线上**确实是两条**
（`_respond` 发一条硬编码的，`_static` 又往 headers 里塞一条）。这是既有行为，
v2 之前不动它 —— 所以这里比的是 `Response.headers`（一条），HTTP 层那两条由
`test_legacy_wire_headers_are_unchanged` 单独钉住。
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import sys
import tempfile
import threading
import unittest
import unittest.mock
import http.client
from http.server import ThreadingHTTPServer

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from qq_roleplay_bot.plugins.webui import webui_panel  # noqa: E402
from qq_roleplay_bot.plugins.webui.webui_access import LOCAL, WebAccess  # noqa: E402
from qq_roleplay_bot.plugins.webui.webui_panel import Panel  # noqa: E402

TOKEN = "test-token-0123456789abcdef"

#: 改 `/v2/` 之前抓的老外壳基线：路径 → (Content-Type, 字节数, `Response` 级 sha256)。
LEGACY_BASELINE: dict[str, tuple[str, int, str]] = {
    "/": ("text/html; charset=utf-8", 4101,
          "aee195ad3ea7a3eca34d78d9565ceaa1ea5610659f379c7b0a462356f76b946b"),
    "/index.html": ("text/html; charset=utf-8", 4101,
                    "aee195ad3ea7a3eca34d78d9565ceaa1ea5610659f379c7b0a462356f76b946b"),
    "/app.css": ("text/css; charset=utf-8", 143041,
                 "b3450ecb639c3e6dab948b32f9b58a5e63fdff890b55823bbe2bdff23f62823b"),
    "/app.js": ("application/javascript; charset=utf-8", 271723,
                "12e77bc912a27412dd8d1c7d318872c85efc425ce2b7ecc3a4f5c53bb7af13aa"),
}


def _v2_dir() -> pathlib.Path:
    return pathlib.Path(webui_panel.__file__).resolve().parent / "webui" / "v2"


def _response_digest(response) -> str:
    digest = hashlib.sha256()
    digest.update(str(response.status).encode())
    digest.update(b"\0")
    digest.update(response.content_type.encode())
    digest.update(b"\0")
    digest.update(json.dumps(sorted(response.headers)).encode())
    digest.update(b"\0")
    digest.update(response.body)
    return digest.hexdigest()


class _PanelFixture(unittest.TestCase):
    """一个本机令牌的面板；`handle()` 直接调，不起线程。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.panel = Panel({"data_root": self._tmp.name},
                           WebAccess(mode=LOCAL, token=TOKEN))

    def get(self, path: str, headers: dict[str, str] | None = None, *, token: bool = True):
        """带令牌地打一个 GET（`token=False` 模拟"页面还没填令牌"）。"""

        sent = dict(headers or {"accept": "*/*"})
        if token:
            sent["authorization"] = f"Bearer {TOKEN}"
        return self.panel.handle("GET", path, b"", sent, "127.0.0.1")


class LegacyShellIsUntouchedTest(_PanelFixture):
    """第 5 条：老路径的响应与改动前**逐字相同**。"""

    def test_legacy_static_responses_match_the_pre_change_baseline(self) -> None:
        for path, (content_type, size, expected) in LEGACY_BASELINE.items():
            with self.subTest(path=path):
                response = self.get(path)
                self.assertEqual(response.status, 200)
                self.assertEqual(response.content_type, content_type)
                self.assertEqual(len(response.body), size)
                # 老 `_static` 只在 headers 里放这一条；`_respond` 那条是另一层的事。
                self.assertEqual(response.headers, [("Cache-Control", "no-store")])
                self.assertTrue(response.cache_control is None,
                                "老路径不许走新的 cache_control 通道")
                self.assertEqual(_response_digest(response), expected)

    def test_legacy_wire_headers_are_unchanged(self) -> None:
        """真 HTTP 一层：老路径仍然是**两条** `no-store`、没有 ETag。"""

        panel = self.panel
        handler = type("H", (webui_panel._Handler,), {"panel": panel})
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        port = server.server_address[1]
        for path, (content_type, size, _digest) in LEGACY_BASELINE.items():
            with self.subTest(path=path):
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
                conn.request("GET", path, headers={"Accept": "*/*"})
                response = conn.getresponse()
                body = response.read()
                headers = [(k.lower(), v) for k, v in response.getheaders()]
                conn.close()
                self.assertEqual(response.status, 200)
                self.assertEqual(dict(headers)["content-type"], content_type)
                self.assertEqual(len(body), size)
                self.assertEqual(
                    [value for key, value in headers if key == "cache-control"],
                    ["no-store", "no-store"],
                )
                self.assertNotIn("etag", dict(headers))

    def test_unknown_path_is_still_404(self) -> None:
        # 带令牌的未知路径仍是 404（与改动前一致）。不带令牌时先撞认证那道门 —— 401 是
        # **既有**行为（`authorize` 在 `_route` 之前），不是这轮改出来的。
        self.assertEqual(self.get("/nope").status, 404)
        self.assertEqual(self.get("/v2x/index.html").status, 404)
        self.assertEqual(self.get("/nope", token=False).status, 401)


class V2ShellTest(_PanelFixture):
    """第 1 条：`/v2/` 与 `/v2/index.html` 都指向同一个入口。"""

    def setUp(self) -> None:
        super().setUp()
        if not (_v2_dir() / "index.html").is_file():
            self.skipTest("v2/ 还没拷进来（先跑 build-and-copy.ps1）")

    def test_v2_root_serves_the_shell_as_html(self) -> None:
        expected = (_v2_dir() / "index.html").read_bytes()
        for path in ("/v2/", "/v2/index.html"):
            with self.subTest(path=path):
                response = self.get(path)
                self.assertEqual(response.status, 200)
                self.assertEqual(response.content_type, "text/html; charset=utf-8")
                self.assertEqual(response.body, expected)
                # 第 4 条：壳必须 no-store，否则发版后浏览器拿旧壳。
                self.assertEqual(response.cache_control, "no-store")
                self.assertNotIn("ETag", [key for key, _ in response.headers])

    def test_bare_v2_redirects_to_the_trailing_slash(self) -> None:
        """`/v2` 少一个斜杠会让产物里的相对地址（`fonts/…`、`composeApp.js`）落到 `/` 下。

        所以 `/v2` 回 302 → `/v2/`，而不是直接把 index.html 吐出来：路由仍然"指向
        index.html"，只是多一跳。这条是刻意的偏差，写在这里免得被当成 bug。
        """

        response = self.get("/v2")
        self.assertEqual(response.status, 302)
        self.assertEqual(dict(response.headers)["Location"], "/v2/")
        self.assertEqual(response.cache_control, "no-store")

    def test_v2_is_a_public_shell(self) -> None:
        """没令牌也能取壳（否则"页面要先有令牌、令牌要在页面里填"死锁）。"""

        anonymous = Panel({"data_root": self._tmp.name}, WebAccess(mode=LOCAL, token=TOKEN))
        self.assertEqual(
            anonymous.handle("GET", "/v2/", b"", {"accept": "text/html"}, "127.0.0.1").status,
            200,
        )
        # 对照：接口仍然要令牌。
        self.assertEqual(
            anonymous.handle("GET", "/api/overview", b"", {"accept": "*/*"}, "127.0.0.1").status,
            401,
        )


class V2BinaryIsNotTextDecodedTest(_PanelFixture):
    """第 2 条：`.wasm` / `.ttf` 的字节数必须等于磁盘文件（证明没被 UTF-8 解码破坏）。"""

    def setUp(self) -> None:
        super().setUp()
        self.wasm = sorted(_v2_dir().glob("*.wasm")) if _v2_dir().is_dir() else []
        if not self.wasm:
            self.skipTest("v2/ 里没有 .wasm（先跑 build-and-copy.ps1）")

    def test_wasm_served_as_bytes_with_application_wasm(self) -> None:
        target = self.wasm[0]
        response = self.get(f"/v2/{target.name}")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.content_type, "application/wasm")
        self.assertEqual(len(response.body), target.stat().st_size)
        self.assertEqual(response.body, target.read_bytes())
        # 带内容哈希的名字才强缓存（`[contenthash]`），否则不该挂 immutable。
        self.assertEqual(response.cache_control, webui_panel.V2_IMMUTABLE_CACHE)
        etag = dict(response.headers)["ETag"]
        self.assertTrue(etag.startswith('"') and etag.endswith('"'))

    def test_font_is_revalidated_not_immutable(self) -> None:
        fonts = sorted((_v2_dir() / "fonts").glob("*.ttf")) if (_v2_dir() / "fonts").is_dir() else []
        if not fonts:
            self.skipTest("v2/fonts 里没有 .ttf")
        target = fonts[0]
        response = self.get(f"/v2/fonts/{target.name}")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.content_type, "font/ttf")
        self.assertEqual(len(response.body), target.stat().st_size)
        self.assertEqual(response.body, target.read_bytes())
        # 字体文件名不含哈希：强缓存会在换字体后拿旧的，所以只能 no-cache + ETag。
        self.assertEqual(response.cache_control, webui_panel.V2_REVALIDATE_CACHE)

    def test_etag_gives_304_without_a_body(self) -> None:
        target = self.wasm[0]
        first = self.get(f"/v2/{target.name}")
        etag = dict(first.headers)["ETag"]
        second = self.get(f"/v2/{target.name}", {"if-none-match": etag})
        self.assertEqual(second.status, 304)
        self.assertEqual(second.body, b"")


class V2TraversalIsRefusedTest(_PanelFixture):
    """第 3 条：`..`（含百分号编码）与目录外的一切都 404，且不读盘。"""

    def test_traversal_attempts_are_404(self) -> None:
        attempts = (
            "/v2/../app.js",
            "/v2/../../etc/passwd",
            "/v2/%2e%2e%2f%2e%2e%2fetc%2fpasswd",
            "/v2/fonts/../../webui_panel.py",
            "/v2/....//app.js",
            "/v2/./index.html",
            "/v2/../webui/app.js",
        )
        # 注意 `/v2//index.html` 不在这一组：路径里多出来的前导 `/` 只会被折叠，
        # 最后落在 `v2/index.html`（同一个文件），不是逃逸。
        for path in attempts:
            with self.subTest(path=path):
                self.assertEqual(self.get(path).status, 404)

    def test_extension_whitelist_is_not_a_wildcard(self) -> None:
        # `/v2/` 本身不在这一组：它映射到 index.html（那是第 1 条要求的 200）。
        for path in ("/v2/webui_panel.py", "/v2/__init__.py", "/v2/nope.js",
                     "/v2/fonts/nope.ttf", "/v2/index.html.bak"):
            with self.subTest(path=path):
                self.assertEqual(self.get(path).status, 404)

    def test_missing_v2_directory_degrades_to_404(self) -> None:
        """v2 还没拷进来时是 404（不是 500、不是路径穿越到别处）。"""

        with tempfile.TemporaryDirectory() as empty:
            with unittest.mock.patch.object(webui_panel, "_v2_root",
                                            lambda: pathlib.Path(empty)):
                self.assertEqual(self.get("/v2/").status, 404)
                self.assertEqual(self.get("/v2/composeApp.js").status, 404)


if __name__ == "__main__":  # pragma: no cover - 手动跑用
    unittest.main()
