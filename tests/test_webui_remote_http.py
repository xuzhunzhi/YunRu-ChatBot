"""远程模式**走真 HTTP** 的端到端验证：登录 → 会话 cookie → CSRF → 来源校验。

由来：`remote` 那一半原先只有单元测试（`test_webui_access.py` 直接调判定函数），
**从没有一个请求真的从 socket 上走完**。这一条补上，钉住"文档写的三道门真的在"。

另外钉住一条边界：**未认证拿不到静态页**（远程下连 JS 都不给）。
"""
import http.cookiejar
import json
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from qq_roleplay_bot.webui_access import REMOTE, hash_password
from qq_roleplay_bot.webui_panel import Panel, PanelServer

PASSWORD = "yunru-remote-password"
ORIGIN = "http://panel.example:8790"
GROUP = "717151356"


def _minimal_data(root: Path) -> None:
    (root / "runtime_state.json").write_text(json.dumps({
        "version": 1, "enabled": True, "enabled_group_ids": [GROUP],
        "target_group_id": GROUP, "sessions": [],
    }, ensure_ascii=False), encoding="utf-8")


class _Remote:
    """极简客户端：保留 cookie、能带任意 Origin / CSRF。"""

    def __init__(self, port: int) -> None:
        self.base = f"http://127.0.0.1:{port}"
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))

    def cookie(self, name: str) -> str:
        for item in self.jar:
            if item.name == name:
                return item.value
        return ""

    def call(self, path: str, *, method: str = "GET", body=None,
             origin: str | None = None, csrf: str | None = None):
        request = urllib.request.Request(self.base + path, method=method)
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            request.add_header("Content-Type", "application/json")
        if origin is not None:
            request.add_header("Origin", origin)
        if csrf is not None:
            request.add_header("X-CSRF-Token", csrf)
        try:
            with self.opener.open(request, data, timeout=10) as response:
                raw = response.read().decode("utf-8")
                return response.status, (json.loads(raw) if raw.strip().startswith("{") else raw)
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8")
            try:
                return exc.code, json.loads(raw)
            except ValueError:
                return exc.code, raw


class _FakeEngine:
    """只够面板 `/api/groups` 用（真引擎有同一对方法）。"""

    def __init__(self, *, enabled: tuple[str, ...] = ()) -> None:
        self.groups: set[str] = set(enabled)

    def enable_group(self, group_id: str) -> bool:
        before = group_id in self.groups
        self.groups.add(group_id)
        return not before

    def disable_group(self, group_id: str) -> bool:
        before = group_id in self.groups
        self.groups.discard(group_id)
        return before


class RemoteHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        _minimal_data(root)
        from qq_roleplay_bot.webui_access import WebAccess

        self.access = WebAccess(mode=REMOTE, password_hash=hash_password(PASSWORD),
                                allowed_origins=(ORIGIN,))
        self.engine = _FakeEngine(enabled=(GROUP,))
        panel = Panel({"data_root": str(root), "engine": self.engine}, self.access)
        self.server = PanelServer(panel, host="127.0.0.1", port=0)
        assert self.server.start() is True
        self.client = _Remote(self.server.bound_port)

    def tearDown(self) -> None:
        self.server.stop()
        self.tmp.cleanup()

    def _reset_limits(self) -> None:
        """清掉限速计数。

        每个用例都从 127.0.0.1 发请求，而认证失败限速是按来源记的——
        不清的话第二个用例会先撞到 429，测到的东西就变味了（真实浏览器只有一个来源，
        这条只影响测试）。
        """

        self.access._fails.clear()
        self.access._writes.clear()

    def test_full_login_flow_over_http(self) -> None:
        # 1) 没有会话：**数据**一个都拿不到（外壳另说，见下一条）
        for path in ("/api/overview", "/api/usage", "/api/state", "/api/settings"):
            status, _body = self.client.call(path, origin=ORIGIN)
            self.assertEqual(status, 401, path)
        self._reset_limits()

        # 2) 口令错 -> 401
        status, _body = self.client.call("/api/login", method="POST",
                                         body={"password": "wrong"}, origin=ORIGIN)
        self.assertEqual(status, 401)

        # 3) 口令对 -> 拿到会话与 CSRF
        status, body = self.client.call("/api/login", method="POST",
                                        body={"password": PASSWORD}, origin=ORIGIN)
        self.assertEqual(status, 200)
        self.assertTrue(self.client.cookie("yunru_panel"))
        csrf = self.client.cookie("csrf")
        self.assertTrue(csrf)

        # 4) 有会话但**不带 CSRF** 的写操作 -> 403
        status, body = self.client.call(
            "/api/groups", method="POST",
            body={"confirm": True, "group_id": GROUP, "enabled": False}, origin=ORIGIN)
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "bad_csrf")

        # 5) CSRF 错 -> 403
        status, _body = self.client.call(
            "/api/groups", method="POST",
            body={"confirm": True, "group_id": GROUP, "enabled": False},
            origin=ORIGIN, csrf="nope")
        self.assertEqual(status, 403)

        # 6) 读接口只要会话（GET 不需要 CSRF）
        status, body = self.client.call("/api/state", origin=ORIGIN)
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["enabled_group_ids"], [GROUP])

    def test_cross_origin_is_refused_even_with_a_session(self) -> None:
        self._reset_limits()
        self.client.call("/api/login", method="POST", body={"password": PASSWORD},
                         origin=ORIGIN)
        csrf = self.client.cookie("csrf")
        for origin in ("http://evil.example", "null", None):
            status, body = self.client.call(
                "/api/overview", origin=origin, csrf=csrf)
            self.assertEqual(status, 403, origin)
            self.assertEqual(body["error"], "bad_origin", origin)

    def test_shell_is_public_but_data_is_not(self) -> None:
        """远程模式下：**外壳免会话**（否则没地方登录），API 一律要会话。

        外壳公开是刻意的：它只是一个 HTML 壳 + 一段 JS，登录表单就在里面。
        远程下仍然要过来源校验（跨站拿不到）。
        """

        status, html = self.client.call("/", origin=ORIGIN)
        self.assertEqual(status, 200)
        self.assertIn("云茹", html)
        self.assertEqual(self.client.call("/app.js", origin=ORIGIN)[0], 200)
        self.assertEqual(self.client.call("/api/state", origin=ORIGIN)[0], 401)
        # 跨站连外壳都不给
        self.assertEqual(self.client.call("/", origin="http://evil.example")[0], 403)
    def test_login_endpoint_needs_no_session_but_is_rate_limited(self) -> None:
        self._reset_limits()
        statuses = [self.client.call("/api/login", method="POST",
                                     body={"password": "wrong"}, origin=ORIGIN)[0]
                    for _ in range(7)]
        self.assertEqual(statuses[0], 401)
        self.assertIn(429, statuses, statuses)

    def test_login_response_sets_both_cookies(self) -> None:
        """真 HTTP 上核对两条 `Set-Cookie` 都到了（挤成一条时 CSRF 会丢）。"""

        self._reset_limits()
        status, _body = self.client.call("/api/login", method="POST",
                                         body={"password": PASSWORD}, origin=ORIGIN)
        self.assertEqual(status, 200)
        self.assertTrue(self.client.cookie("yunru_panel"), "会话 cookie 没到")
        self.assertTrue(self.client.cookie("csrf"), "CSRF cookie 没到（写操作会全 403）")
        # 带着这两半，一次写操作应当真的成功
        status, body = self.client.call(
            "/api/groups", method="POST",
            body={"confirm": True, "group_id": GROUP, "enabled": False},
            origin=ORIGIN, csrf=self.client.cookie("csrf"))
        self.assertEqual(status, 200, body)
        self.assertTrue(body["data"]["ok"])
        self.assertNotIn(GROUP, self.engine.groups, "写操作该真的落到引擎上")

    def test_local_mode_does_not_accept_a_remote_style_request(self) -> None:
        """`local` 模式只认 Bearer：有个会话 cookie 也不算数（两套逻辑不混用）。"""

        from qq_roleplay_bot.webui_access import LOCAL, WebAccess

        panel = Panel({"data_root": self.tmp.name},
                      WebAccess(mode=LOCAL, token="t" * 32))
        server = PanelServer(panel, host="127.0.0.1", port=0)
        server.start()
        try:
            client = _Remote(server.bound_port)
            status, body = client.call("/api/overview", origin=ORIGIN)
            self.assertEqual(status, 401)
            self.assertEqual(body["error"], "unauthorized")
        finally:
            server.stop()
