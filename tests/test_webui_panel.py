"""面板本身：认证前置、写动作护栏、只读保证、接缝转交。

面板是 Stage 4 后台插件（用户 2026-10-01："面板属于 stage4 内容，本质插件"），
它拿不到 `engine`、拿不到 `transport`，只能调装配点注入的闭包。这个文件钉的是：

1. **每个端点都要认证**（含 `/health`），未认证拿不到任何数据；
2. **写动作必须 `confirm: true`**，并且缺 `confirm` 时不调用任何接缝；
3. 群动作**以云茹为 actor**，取不到她的 QQ 号就 fail-closed；
4. 读接口**不改任何文件**（mtime 为证）；
5. 参数校验：`limit` 夹取、未知 feature / 表名 400、缺 session 400。
"""
import asyncio
import json
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from qq_roleplay_bot.webui_access import LOCAL, WebAccess
from qq_roleplay_bot.webui_panel import Panel, PanelServer

TOKEN = "t" * 32
GROUP = "717151356"
ME = "900000001"


def _fixture(tmp: str) -> dict[str, object]:
    root = Path(tmp)
    (root / "api_usage.json").write_text(json.dumps({
        "version": 1, "since": 1000.0, "updated_at": 2000.0,
        "roles": {"dialogue": {"_calls": 3, "prompt_tokens": 100, "completion_tokens": 10,
                               "total_tokens": 110, "prompt_cache_hit_tokens": 80,
                               "prompt_cache_miss_tokens": 20}},
        "counters": {"accepted_messages": 9},
    }, ensure_ascii=False), encoding="utf-8")
    (root / "runtime_state.json").write_text(json.dumps({
        "version": 1, "enabled": True, "enabled_group_ids": [GROUP],
        "target_group_id": GROUP,
        "sessions": [{"session_id": f"group:{GROUP}", "mode": "active",
                      "active_user_id": ME, "context": {"topic": "测试"},
                      "history": [{"message_id": "m1", "user_id": ME, "text": "在吗",
                                   "sender_name": "许纯之", "is_bot_message": False}]}],
    }, ensure_ascii=False), encoding="utf-8")
    memory = root / "memory"
    memory.mkdir()
    db = sqlite3.connect(memory / "memory.sqlite3")
    db.executescript("""
        CREATE TABLE memory_short (id TEXT PRIMARY KEY, scope_type TEXT, scope_key TEXT,
            subject_user_id TEXT, kind TEXT, normalized_key TEXT, content TEXT,
            confidence REAL, created_at REAL, updated_at REAL, expires_at REAL,
            revision INTEGER, visibility TEXT, status TEXT, last_used_at REAL,
            last_reviewed_at REAL, origin TEXT, source_event_ids TEXT, durable INTEGER,
            reminded_at REAL);
        CREATE TABLE memory_long (id TEXT PRIMARY KEY, scope_type TEXT, scope_key TEXT,
            subject_user_id TEXT, kind TEXT, normalized_key TEXT, content TEXT,
            confidence REAL, created_at REAL, updated_at REAL, expires_at REAL,
            revision INTEGER, visibility TEXT, status TEXT, last_used_at REAL,
            last_reviewed_at REAL, origin TEXT, source_event_ids TEXT, durable INTEGER,
            reminded_at REAL, promoted_at REAL);
        CREATE TABLE archive (id TEXT PRIMARY KEY, scope_type TEXT, scope_key TEXT,
            subject_user_id TEXT, kind TEXT, normalized_key TEXT, content TEXT,
            confidence REAL, created_at REAL, updated_at REAL, expires_at REAL,
            revision INTEGER, deleted_at REAL, delete_reason TEXT);
        CREATE TABLE inbox (id TEXT PRIMARY KEY, group_id TEXT, user_id TEXT, speaker TEXT,
            text TEXT, occurred_at REAL, expires_at REAL, lease_id TEXT);
        CREATE TABLE audit (batch_id TEXT, operation_index INTEGER, op TEXT,
            scope_type TEXT, created_at REAL);
        CREATE TABLE people (group_id TEXT, user_id TEXT, name TEXT, updated_at REAL);
        CREATE TABLE relationships (user_id TEXT PRIMARY KEY, closeness INTEGER,
            guardedness INTEGER, updated_at REAL, last_seen_at REAL);
        CREATE TABLE tombstones (scope_type TEXT, scope_key TEXT, normalized_key TEXT,
            deleted_at REAL);
        CREATE VIEW records AS
            SELECT id,scope_type,scope_key,subject_user_id,kind,normalized_key,content,
                   confidence,created_at,updated_at,expires_at,revision,visibility,status,
                   last_used_at,last_reviewed_at,origin,source_event_ids,durable,reminded_at,
                   NULL AS promoted_at,'short' AS tier FROM memory_short;
        INSERT INTO memory_short VALUES ('r1','user_group','g1',NULL,'preference','likes_tea',
            '喜欢喝奶茶',0.9,1.0,2.0,NULL,1,'group_safe','active',NULL,NULL,'agent_inferred',
            '[]',0,NULL);
        INSERT INTO relationships VALUES ('900000001',2,1,5.0,5.0);
    """)
    db.commit()
    db.close()
    return {"root": root}


class _Seams:
    """假接缝：记录被调了什么，让测试能断言"插件只调闭包、不碰引擎"。"""

    def __init__(self, root: Path) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []
        self.root = root
        self.self_id_value = ME
        self.settings: dict[str, str] = {}

    # --- 动作 ---

    async def execute_action(self, request, *, group_id, actor_id, message_id="",
                             mentioned=False):
        self.calls.append(("execute_action", (request.group, request.kind),
                           {"group_id": group_id, "actor_id": actor_id,
                            "mentioned": mentioned, "target_id": request.target_id}))
        return f"已执行 {request.kind}"

    async def call_action(self, action, params=None):
        self.calls.append(("call_action", (action, dict(params or {})), {}))
        return {}

    async def self_id(self):
        self.calls.append(("self_id", (), {}))
        return self.self_id_value


def _seams(root: Path) -> _Seams:
    return _Seams(root)


class _Client:
    def __init__(self, port: int, token: str = TOKEN) -> None:
        self.base = f"http://127.0.0.1:{port}"
        self.token = token

    def __call__(self, path: str, *, method: str = "GET", body=None, token: str | None = None,
                 headers: dict[str, str] | None = None, raw: bool = False,
                 with_headers: bool = False):
        request = urllib.request.Request(self.base + path, method=method)
        use = self.token if token is None else token
        if use:
            request.add_header("Authorization", f"Bearer {use}")
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            request.add_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            with urllib.request.urlopen(request, data, timeout=5) as response:
                payload = response.read().decode("utf-8")
                if with_headers:
                    # 响应头是 dict（同名头会合并）——测缓存头够用。
                    return response.status, payload, dict(response.headers)
                if raw:
                    return response.status, payload
                return response.status, (json.loads(payload) if payload else {})
        except urllib.error.HTTPError as exc:
            payload = exc.read().decode("utf-8")
            if with_headers:
                return exc.code, payload, dict(exc.headers)
            if raw:
                return exc.code, payload
            try:
                return exc.code, json.loads(payload)
            except ValueError:
                return exc.code, {"raw": payload}


class _Engine:
    """只实现面板用到的那几个引擎方法（**不与真引擎共享实现**，就是几个开关）。"""

    def __init__(self) -> None:
        self.enabled_groups: set[str] = {GROUP}
        self.sessions: set[str] = {f"group:{GROUP}"}
        self.restarts: list[str] = []

    def snapshot(self):
        raise RuntimeError("测试里不走活快照（要的是文件那条路）")

    def enable_group(self, group_id: str) -> bool:
        before = group_id in self.enabled_groups
        self.enabled_groups.add(group_id)
        return not before

    def disable_group(self, group_id: str) -> bool:
        before = group_id in self.enabled_groups
        self.enabled_groups.discard(group_id)
        return before

    def leave_session(self, session_id: str) -> bool:
        before = session_id in self.sessions
        self.sessions.discard(session_id)
        return before

    def request_restart(self, source: str = "") -> str:
        self.restarts.append(source)
        return "正在重启，几秒后回来。"


class _PanelCase(unittest.TestCase):
    """起一个真的 HTTP 服务（绑 0 端口），测完关掉。"""

    access_seams: _Seams

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        _fixture(self.tmp.name)
        self.seams = _seams(self.root)
        self.engine = _Engine()
        access = WebAccess(mode=LOCAL, token=TOKEN)
        self.panel = Panel({
            "data_root": str(self.root),
            # 引擎：给一个**只实现面板用到的那几个方法**的替身；
            # `snapshot()` 会抛错，于是 `/api/state` 走文件那条路（测试要的正是它）。
            "engine": self.engine,
            "execute_action": self.seams.execute_action,
            "call_action": self.seams.call_action,
            "self_id": self.seams.self_id,
            "memory_ops": _MemoryOps(),
            "operator_config": _Config(),
            "prompt_library": _Library(self.root),
            "knowledge": _Knowledge(self.root),
            "control_audit": _Audit(),
            "apply_overrides": _apply,
        }, access)
        # 测试里"把协程丢回主循环"就是直接跑完（生产用 run_coroutine_threadsafe）。
        from qq_roleplay_bot.webui_panel import install_async_runner

        install_async_runner(lambda coro: asyncio.run(coro))
        self.server = PanelServer(self.panel, host="127.0.0.1", port=0)
        assert self.server.start() is True
        self.client = _Client(self.server.bound_port)

    def tearDown(self) -> None:
        self.server.stop()
        from qq_roleplay_bot.webui_panel import install_async_runner as _install

        _install(None)
        self.tmp.cleanup()


class _MemoryOps:
    available = True

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def purge(self, ids, *, reason=""):
        self.calls.append(("purge", tuple(ids), reason))
        return len(list(ids))

    def affinity(self, user_id, axis, delta):
        self.calls.append(("affinity", user_id, axis, delta))
        return {"closeness": 1, "guardedness": 0}

    def reset_affinity(self, user_id):
        self.calls.append(("reset", user_id))
        return True


class _Config:
    def __init__(self) -> None:
        self.values: dict[str, str] = {"api_model": "deepseek-chat",
                                       "api_key": "sk-super-secret-value-1234"}


class _Library:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.saved: list[tuple[str, str]] = []

    @property
    def directory(self) -> Path:
        return self.root / "prompts"

    def text(self, name: str) -> str:
        return f"<{name} 的当前正文>"

    def is_overridden(self, name: str) -> bool:
        return False

    def versions(self, name: str):
        return []

    def save(self, name: str, text: str, *, source: str = "") -> str:
        self.saved.append((name, text))
        return text

    def reset(self, name: str, *, source: str = "") -> str:
        self.saved.append((name, "<reset>"))
        return self.text(name)


class _Knowledge:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.chunks_added: list[dict] = []

    def chunks(self):
        return []

    def chunk_versions(self):
        return []

    def overview(self):
        return {"base": {}, "operator": {"chunks": 0}, "operator_chunks": 0}

    def add_chunk(self, *, title: str, content: str, note: str = ""):
        from qq_roleplay_bot.knowledge_operator import OperatorChunk

        self.chunks_added.append({"title": title, "content": content})
        return OperatorChunk(id="c1", title=title, content=content, note=note)


class _Audit:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def record(self, action, *, detail=None, result="", source="", token="") -> None:
        self.rows.append({"action": action, "detail": dict(detail or {}),
                          "result": result, "source": source,
                          "token": token[:4]})

    def tail(self, count: int = 50):
        return self.rows[-count:]


def _apply(engine, payload, **kwargs):
    return {key: {"applied": "live", "detail": "已生效"} for key in payload}


# --- 认证 -------------------------------------------------------------------

class AuthTests(_PanelCase):
    def test_the_shell_loads_without_a_token(self) -> None:
        """**首页与静态文件必须免认证**，否则令牌没地方填（死锁）。

        2026-10-01 现场事故：浏览器打开 `/` 拿到 401 JSON，满屏只有
        `{"ok": false, "error": "unauthorized"}`——看起来像面板坏了。
        外壳里没有秘密，令牌本来就是要填进这个页面的。
        """

        for path in ("/", "/index.html", "/app.css", "/app.js", "/health"):
            status, _body = self.client(path, token="", raw=True)
            self.assertEqual(status, 200, path)
        status, html = self.client("/", token="", raw=True)
        self.assertIn("云茹", html)
        self.assertIn("令牌", html)

    def test_api_still_needs_the_token(self) -> None:
        """外壳放开了，**数据一个都不放开**。"""

        for path in ("/api/overview", "/api/usage", "/api/state", "/api/settings",
                     "/api/memory/overview", "/api/prompts", "/api/logs?feature=reply"):
            status, body = self._anonymous(path)
            self.assertEqual(status, 401, path)
            self.assertEqual(body["error"], "unauthorized", path)

    def test_every_api_endpoint_requires_the_token(self) -> None:
        """逐个接口用**全新面板实例**验一遍（认证失败本身有按来源的限速）。

        每个路径一个实例：共用一个会先撞到 429，测到的东西就变味了。
        """

        for path in ("/api/overview", "/api/usage", "/api/state", "/api/memory/overview",
                     "/api/knowledge/overview", "/api/settings"):
            with self.subTest(path=path):
                status, body = self._anonymous(path)
                self.assertEqual(status, 401, path)
                self.assertEqual(body["error"], "unauthorized", path)

    def _anonymous(self, path: str) -> tuple[int, dict]:
        with tempfile.TemporaryDirectory() as tmp:
            _fixture(tmp)
            panel = Panel({"data_root": tmp}, WebAccess(mode=LOCAL, token=TOKEN))
            server = PanelServer(panel, host="127.0.0.1", port=0)
            server.start()
            try:
                status, body = _Client(server.bound_port)(
                    path, token="", raw=True,
                    headers={"Accept": "application/json"})
                if isinstance(body, str):
                    try:
                        return status, json.loads(body)
                    except ValueError:
                        return status, {"raw": body[:80]}
                return status, body
            finally:
                server.stop()

    def test_wrong_token_looks_exactly_like_missing(self) -> None:
        missing = self.client("/api/overview", token="")
        wrong = self.client("/api/overview", token="x" * 32)
        self.assertEqual(missing, wrong)

    def test_browser_navigation_to_an_api_path_gets_a_guide_page(self) -> None:
        """在浏览器里直接打开接口地址时给引导页，而不是一行裸 JSON。

        2026-10-01：屏幕上是 `{"ok":false,"error":"unauthorized"}`，看起来像面板坏了。
        顶层导航带 `Accept: text/html`，用这一点分开两种情况。
        """

        status, html = self.client("/api/overview", token="", raw=True, headers={
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        })
        self.assertEqual(status, 401)
        self.assertIn("这里是接口地址", html)
        self.assertIn("webui_token", html)

        # 页面里的 fetch（Accept 是 */* 或 json）仍然拿到 JSON
        status, body = self.client("/api/overview", token="", raw=True,
                                   headers={"Accept": "*/*"})
        self.assertEqual(status, 401)
        self.assertIn("unauthorized", body)

    def test_static_assets_are_served_with_the_token(self) -> None:
        status, html = self.client("/", raw=True)
        self.assertEqual(status, 200)
        self.assertIn("云茹", html)
        self.assertEqual(self.client("/app.css", raw=True)[0], 200)
        self.assertEqual(self.client("/app.js", raw=True)[0], 200)

    def test_unknown_path_is_404(self) -> None:
        self.assertEqual(self.client("/nope")[0], 404)


# --- 读接口 -----------------------------------------------------------------

class ReadTests(_PanelCase):
    def test_usage_matches_the_ledger(self) -> None:
        status, body = self.client("/api/usage")
        self.assertEqual(status, 200)
        role = body["data"]["roles"]["dialogue"]
        self.assertEqual(role["calls"], 3)
        self.assertEqual(role["hit_rate"], 0.8)

    def test_state_reads_the_file_when_engine_is_absent(self) -> None:
        _status, body = self.client("/api/state")
        self.assertEqual(body["data"]["source"], "files")
        self.assertEqual(body["data"]["enabled_group_ids"], [GROUP])

    def test_session_messages_are_returned(self) -> None:
        _status, body = self.client(f"/api/session/messages?session_id=group:{GROUP}")
        self.assertEqual(body["data"]["total"], 1)
        self.assertEqual(body["data"]["messages"][0]["sender_name"], "许纯之")
        self.assertEqual(self.client("/api/session/messages")[0], 400)

    def test_memory_overview_and_tables(self) -> None:
        _status, body = self.client("/api/memory/overview")
        self.assertTrue(body["data"]["available"])
        self.assertEqual(body["data"]["counts"]["short"], 1)
        _status, rows = self.client("/api/memory/records?limit=10")
        self.assertEqual(rows["data"]["total"], 1)
        self.assertEqual(rows["data"]["rows"][0]["normalized_key"], "likes_tea")
        self.assertEqual(self.client("/api/memory/nope")[0], 404)

    def test_settings_shows_effective_boolean_not_a_blank(self) -> None:
        """开关类要显示**生效值**（开 / 关），不能因为覆盖层里没写就空着。

        由来（2026-10-01 用户："bug有点太多了"）：`.env` 里这些开关本来就是空的，
        而空 = 代码默认**开**。原样显示空串，面板上整列都是 "—"，看着像"全都没开"。
        """

        _status, body = self.client("/api/settings")
        fields = {item["key"]: item for item in body["data"]["fields"]}
        for key in ("judge_enabled", "memory_enabled", "group_owner_enabled"):
            self.assertIn(key, fields, f"{key} 应当出现在设置里")
            self.assertIn(fields[key]["current"], {"开", "关"},
                          f"{key} 的当前值应当是开/关，而不是 {fields[key]['current']!r}")
        # 开关项一律不该是空串（那正是这个 bug 的表现）。
        for key, item in fields.items():
            if item["kind"] == "bool":
                self.assertNotEqual(item["current"], "", f"{key} 显示成了空白")

    def test_static_assets_are_not_cached(self) -> None:
        """静态文件必须显式禁缓存。

        由来（2026-10-01 实测）：不带任何缓存头时，浏览器按 `Last-Modified` 猜新鲜期，
        于是改了 `app.css` 刷新还是旧样式——看起来像"改了没生效"。面板每次读文件
        的开销无所谓，所以直接 `no-store`。
        """

        for path in ("/", "/app.css", "/app.js"):
            status, _body, headers = self.client(path, with_headers=True)
            self.assertEqual(status, 200, path)
            self.assertEqual(headers.get("Cache-Control"), "no-store", path)

    def test_reads_do_not_touch_any_file(self) -> None:
        """**只读保证**：读一圈之后所有文件（含 sqlite 的 mtime）都不动。"""

        watched = [self.root / "runtime_state.json", self.root / "api_usage.json",
                   self.root / "memory" / "memory.sqlite3"]
        before = {path.name: path.stat().st_mtime_ns for path in watched}
        time.sleep(0.02)
        for path in ("/api/overview", "/api/usage", "/api/state", "/api/memory/overview",
                     "/api/memory/records", "/api/memory/archive", "/api/memory/problem",
                     "/api/memory/relationships", "/api/settings", "/api/prompts",
                     "/api/knowledge/overview", "/api/knowledge/chunks",
                     "/api/knowledge/operator", "/api/approvals/pending",
                     f"/api/session/messages?session_id=group:{GROUP}"):
            self.client(path)
        self.assertEqual({path.name: path.stat().st_mtime_ns for path in watched}, before)

    def test_limit_is_clamped(self) -> None:
        _status, body = self.client("/api/memory/records?limit=99999")
        self.assertLessEqual(body["data"]["limit"], 200)

    def test_unknown_log_feature_is_400(self) -> None:
        self.assertEqual(self.client("/api/logs?feature=../../etc/passwd")[0], 400)
        self.assertEqual(self.client("/api/logs?feature=nope")[0], 400)

    def test_settings_never_echo_the_secret(self) -> None:
        """凭据类只给掩码——响应里不许出现明文（用户说正文不脱敏，但 key 是另一回事）。"""

        _status, body = self.client("/api/settings")
        raw = json.dumps(body, ensure_ascii=False)
        self.assertNotIn("sk-super-secret-value-1234", raw)
        secrets = [field for field in body["data"]["fields"] if field["kind"] == "secret"]
        self.assertTrue(secrets)
        for field in secrets:
            self.assertTrue(field["current"].startswith("未设置")
                            or "…" in field["current"], field)


# --- 写接口 -----------------------------------------------------------------

class WriteTests(_PanelCase):
    def test_write_requires_confirmation(self) -> None:
        status, body = self.client("/api/groups", method="POST",
                                   body={"group_id": GROUP, "enabled": False})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "needs_confirmation")

    def test_unknown_setting_key_is_reported_per_key(self) -> None:
        _status, body = self.client("/api/settings", method="POST",
                                    body={"confirm": True, "nope": "1"})
        self.assertEqual(_status, 200)
        self.assertTrue(body["data"]["ok"])

    def test_action_uses_yunru_as_the_actor(self) -> None:
        """**用户 2026-10-01**："群管理动作的 actor 为云茹"。"""

        status, body = self.client("/api/action", method="POST", body={
            "confirm": True, "group": "group_owner", "kind": "notice",
            "group_id": GROUP, "text": "测试公告",
        })
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["actor_id"], ME)
        call = [c for c in self.seams.calls if c[0] == "execute_action"][-1]
        self.assertEqual(call[2]["actor_id"], ME)
        self.assertEqual(call[2]["group_id"], GROUP)
        self.assertTrue(call[2]["mentioned"])

    def test_action_fails_closed_without_self_id(self) -> None:
        self.seams.self_id_value = ""
        status, body = self.client("/api/action", method="POST", body={
            "confirm": True, "group": "group_admin", "kind": "kick",
            "group_id": GROUP, "target_id": "10001",
        })
        self.assertEqual(status, 503)
        self.assertEqual(body["error"], "self_id_unavailable")
        self.assertFalse([c for c in self.seams.calls if c[0] == "execute_action"])

    def test_memory_purge_passes_ids_to_the_seam(self) -> None:
        ops = self.panel.seams["memory_ops"]
        status, body = self.client("/api/memory/purge", method="POST", body={
            "confirm": True, "record_ids": ["r1", "r2"], "reason": "测试",
        })
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["removed"], 2)
        self.assertEqual(ops.calls[-1][1], ("r1", "r2"))

    def test_affinity_rejects_a_big_delta(self) -> None:
        from qq_roleplay_bot.memory_ops import MemoryOpRejected

        class _Strict(_MemoryOps):
            def affinity(self, user_id, axis, delta):
                if delta not in (-1, 0, 1):
                    raise MemoryOpRejected("增量只能填 -1、0 或 1")
                return super().affinity(user_id, axis, delta)

        self.panel.seams["memory_ops"] = _Strict()
        status, body = self.client("/api/memory/affinity", method="POST", body={
            "confirm": True, "user_id": ME, "axis": "closeness", "delta": 5,
        })
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "bad_request")

    def test_prompt_save_reports_rejection(self) -> None:
        from qq_roleplay_bot.prompt_library import PromptRejected

        class _Strict(_Library):
            def save(self, name, text, *, source=""):
                raise PromptRejected("这份 prompt 里有机制性措辞", detail="命中：检查")

        self.panel.seams["prompt_library"] = _Strict(self.root)
        status, body = self.client("/api/prompts", method="POST", body={
            "confirm": True, "action": "save", "name": "persona", "text": "检查",
        })
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "prompt_rejected")
        self.assertIn("检查", body["detail"])

    def test_prompt_save_goes_through_the_seam(self) -> None:
        library = self.panel.seams["prompt_library"]
        self.client("/api/prompts", method="POST", body={
            "confirm": True, "action": "save", "name": "judge", "text": "新判定 prompt",
        })
        self.assertEqual(library.saved[-1], ("judge", "新判定 prompt"))

    def test_knowledge_chunk_goes_through_the_seam(self) -> None:
        knowledge = self.panel.seams["knowledge"]
        status, body = self.client("/api/knowledge/operator", method="POST", body={
            "confirm": True, "action": "add", "title": "泉水的口头禅", "content": "她一提泉水就说“又是泉水”。",
        })
        self.assertEqual(status, 200)
        self.assertEqual(knowledge.chunks_added[-1]["title"], "泉水的口头禅")
        self.assertEqual(body["data"]["chunk"]["id"], "c1")

    def test_approval_requires_flag_and_group(self) -> None:
        self.assertEqual(self.client("/api/approval", method="POST",
                                     body={"confirm": True})[0], 400)

    def test_approval_calls_set_group_add_request(self) -> None:
        status, _body = self.client("/api/approval", method="POST", body={
            "confirm": True, "flag": "f1", "group_id": GROUP, "approve": True,
        })
        self.assertEqual(status, 200)
        call = [c for c in self.seams.calls if c[0] == "call_action"][-1]
        self.assertEqual(call[1][0], "set_group_add_request")
        self.assertTrue(call[1][1]["approve"])

    def test_body_limit(self) -> None:
        status, body = self.client("/api/settings", method="POST", body={
            "confirm": True, "api_model": "x" * (300 * 1024),
        })
        self.assertEqual(status, 413)

    def test_rate_limit_applies_to_writes_in_remote_mode(self) -> None:
        from qq_roleplay_bot.webui_access import REMOTE, hash_password

        self.panel.access = WebAccess(mode=REMOTE,
                                      password_hash=hash_password("abcdefgh"),
                                      allowed_origins=("http://127.0.0.1",))
        allowed = 0
        for _ in range(80):
            allowed += 1 if self.panel.access.write_allowed("1.1.1.1") else 0
        self.assertLessEqual(allowed, 60)


# --- 审计与统计 -------------------------------------------------------------

class AuditTests(_PanelCase):
    def test_writes_are_audited_without_secrets(self) -> None:
        audit = self.panel.seams["control_audit"]
        _status, body = self.client("/api/groups", method="POST", body={
            "confirm": True, "group_id": GROUP, "enabled": False,
        })
        self.assertTrue(body["data"]["ok"])
        self.assertTrue(audit.rows, "写动作该留下审计")
        raw = json.dumps(audit.rows, ensure_ascii=False)
        self.assertNotIn(TOKEN, raw)

    def test_panel_survives_a_broken_seam(self) -> None:
        def _explode(*_args, **_kwargs):
            raise RuntimeError("boom")

        self.panel.seams["apply_overrides"] = _explode
        status, body = self.client("/api/settings", method="POST",
                                   body={"confirm": True, "api_model": "m"})
        self.assertEqual(status, 500)
        self.assertEqual(body["error"], "internal_error")

    def test_server_lifecycle(self) -> None:
        self.assertTrue(self.server.alive)
        port = self.server.bound_port
        self.assertGreater(port, 0)
        self.server.stop()
        self.assertFalse(self.server.alive)
        # 停掉之后端口仍然记得（日志与面板靠它说话；`port=0` 时更是唯一线索）
        self.assertEqual(self.server.stats()["port"], port)
        self.assertEqual(self.server.bound_port, port)


# --- 插件形状 ---------------------------------------------------------------

def test_panel_plugin_shape_and_close() -> None:
    """面板插件与其它后台插件同形：`name` / `interval_seconds` / `poll_once` / `close`。"""

    from qq_roleplay_bot.webui_panel import WebPanelPlugin

    with tempfile.TemporaryDirectory() as tmp:
        _fixture(tmp)
        panel = Panel({"data_root": tmp}, WebAccess(mode=LOCAL, token=TOKEN))
        server = PanelServer(panel, host="127.0.0.1", port=0)
        plugin = WebPanelPlugin(server, interval_seconds=30)
        assert plugin.name == "webui-panel"
        assert plugin.enabled is True
        import asyncio

        started = asyncio.run(plugin.poll_once())
        assert started["started"] is True
        assert server.alive is True
        asyncio.run(plugin.close())
        assert server.alive is False
        # 再 poll 一次会把它拉回来（自愈）
        again = asyncio.run(plugin.poll_once())
        assert again["started"] is True
        asyncio.run(plugin.close())


def test_panel_plugin_holds_no_transport_or_engine() -> None:
    from qq_roleplay_bot.webui_panel import Panel, PanelServer, WebPanelPlugin

    with tempfile.TemporaryDirectory() as tmp:
        panel = Panel({"data_root": tmp}, WebAccess(mode=LOCAL, token=TOKEN))
        plugin = WebPanelPlugin(PanelServer(panel, host="127.0.0.1", port=0))
        for forbidden in ("transport", "engine", "capabilities", "self_roles"):
            assert not hasattr(plugin, forbidden), forbidden


def test_run_background_plugin_calls_close() -> None:
    """`run_background_plugin` 的 `finally` 真的调 `close()`（原来只在文档里）。"""

    import asyncio

    from qq_roleplay_bot.background_plugins import run_background_plugin

    closed: list[bool] = []

    class _Plugin:
        name = "probe"
        interval_seconds = 0.01
        enabled = True

        async def poll_once(self):
            return 1

        async def close(self):
            closed.append(True)

    async def main():
        task = asyncio.create_task(
            run_background_plugin(_Plugin(), first_delay=0, min_interval=0.01))
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(main())
    assert closed == [True]


def test_lazy_seam_resolves_after_assembly() -> None:
    """装配点在 `serve` 里跑，而 `memory_ops` 是起了记忆服务之后才挂上引擎的。

    直接取值会拿到 `None`（记忆按钮永远 503，2026-10-01 现场踩到），
    所以这里钉住"延迟接缝"的语义：每次用的时候现取。
    """

    from qq_roleplay_bot.webui_panel import _Lazy

    holder: dict[str, object] = {}

    class _Ops:
        available = True

        def purge(self, ids, *, reason=""):
            return len(list(ids))

    panel = Panel({"memory_ops": _Lazy(lambda: holder.get("ops"))},
                  WebAccess(mode=LOCAL, token=TOKEN))
    assert panel._seam("memory_ops") is None
    holder["ops"] = _Ops()
    assert panel._seam("memory_ops") is not None

    def _explode():
        raise RuntimeError("boom")

    panel.seams["memory_ops"] = _Lazy(_explode)
    assert panel._seam("memory_ops") is None, "取不到就当这项没启用，不能冒泡"


def test_memory_action_works_with_a_lazy_seam() -> None:
    """端到端：延迟接缝下 `/api/memory/purge` 也要能用。"""

    with tempfile.TemporaryDirectory() as tmp:
        _fixture(tmp)
        ops = _MemoryOps()
        from qq_roleplay_bot.webui_panel import _Lazy

        panel = Panel({"data_root": tmp, "memory_ops": _Lazy(lambda: ops)},
                      WebAccess(mode=LOCAL, token=TOKEN))
        server = PanelServer(panel, host="127.0.0.1", port=0)
        server.start()
        try:
            status, body = _Client(server.bound_port)(
                "/api/memory/purge", method="POST",
                body={"confirm": True, "record_ids": ["r1"]})
            assert status == 200
            assert body["data"]["removed"] == 1
            assert ops.calls[-1][1] == ("r1",)
        finally:
            server.stop()


def test_restart_gate_is_wired_in_production() -> None:
    """生产装配必须真的接上重启冷却。

    由来（2026-10-01 现场抓到）：文档与计划里都写了"10 秒内重复 → 429"，
    实际**根本没接**——连点两次真的重启了两次。
    """

    import inspect

    from qq_roleplay_bot import background_plugins as bp

    source = inspect.getsource(bp._web_panel_plugin)
    assert '"restart_gate"' in source
    assert bp._restart_gate.__name__ == "_restart_gate"
    # 冷却本身：最近一次重启之后立刻再来应当被拒
    with tempfile.TemporaryDirectory() as tmp:
        _fixture(tmp)
        panel = Panel({"data_root": tmp, "restart_gate": bp._restart_gate,
                       "engine": _Engine()},
                      WebAccess(mode=LOCAL, token=TOKEN))
        server = PanelServer(panel, host="127.0.0.1", port=0)
        server.start()
        client = _Client(server.bound_port)
        saved = list(bp._LAST_RESTART)
        bp._LAST_RESTART[0] = 0.0
        try:
            first = client("/api/control/restart", method="POST", body={"confirm": True})
            second = client("/api/control/restart", method="POST", body={"confirm": True})
            assert first[0] == 202, first
            assert second[0] == 429, second
            assert self_engine_restarts(panel) == ["webui:127.0.0.1"]
        finally:
            bp._LAST_RESTART[:] = saved
            server.stop()


def self_engine_restarts(panel: Panel) -> list[str]:
    return list(panel.seams["engine"].restarts)


def test_memory_rows_carry_who_they_are_about() -> None:
    """记忆列表必须带「关于谁」——这是记忆里最重要的那一维。

    由来（2026-10-01 用户："记忆要跟人对应……难怪记忆调不起来"）：面板原来只回
    `subject_user_id`，界面上看不出哪条记忆挂在谁身上，也就没法核对有没有挂错人。
    现在按 `docs/MEMORY_PEOPLE.md` 的口径补 `subjects`（关联人）与 `about`（显示名，
    查不到退回 QQ 号）。
    """

    from qq_roleplay_bot import webui_data

    with tempfile.TemporaryDirectory() as tmp:
        _fixture(tmp)
        root = Path(tmp)
        db = sqlite3.connect(root / "memory" / "memory.sqlite3")
        db.executescript("""
            CREATE TABLE IF NOT EXISTS record_subjects (
                record_id TEXT NOT NULL, user_id TEXT NOT NULL, group_id TEXT NOT NULL,
                PRIMARY KEY (record_id, user_id));
            INSERT INTO people VALUES ('717151356', '900000001', '许纯之', 1.0);
            INSERT INTO record_subjects VALUES ('r1', '900000001', '717151356');
        """)
        db.commit()
        db.close()

        out = webui_data.read_memory_table("records", root=root, limit=5)
        row = out["rows"][0]
        assert row["subjects"] == ["900000001"], row
        assert row["about"] == "许纯之", row

        # 名册里没有的人：**退回 QQ 号，不猜名字**
        db = sqlite3.connect(root / "memory" / "memory.sqlite3")
        db.execute("UPDATE record_subjects SET user_id='999999' WHERE record_id='r1'")
        db.commit()
        db.close()
        out = webui_data.read_memory_table("records", root=root, limit=5)
        assert out["rows"][0]["about"] == "999999", out["rows"][0]


def test_archive_rows_also_carry_who_they_were_about() -> None:
    """归档列表也要带「关于谁」：删错了得看清删的是谁的事。

    这条测试故意**不建 `record_subjects` 表**——真机上那张表读不到时，
    归属人还有 `subject_user_id` 这条来源，`about` 不该整列消失。
    """

    from qq_roleplay_bot import webui_data

    with tempfile.TemporaryDirectory() as tmp:
        _fixture(tmp)
        root = Path(tmp)
        db = sqlite3.connect(root / "memory" / "memory.sqlite3")
        db.executescript("""
            INSERT INTO people VALUES ('717151356', '900000001', '许纯之', 1.0);
            INSERT INTO archive VALUES ('a1','user_group','717151356:900000001','900000001',
                'preference','likes_tea','喜欢喝奶茶',0.9,1.0,2.0,NULL,1,3.0,'面板人工清理');
        """)
        db.commit()
        db.close()

        out = webui_data.read_memory_table("archive", root=root, limit=5)
        assert out["rows"][0]["about"] == "许纯之", out["rows"][0]
        assert out["rows"][0]["subjects"] == ["900000001"], out["rows"][0]


def test_memory_overview_and_tables(self_helper=None) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        _fixture(tmp)
        from qq_roleplay_bot import webui_data

        overview = webui_data.read_memory_overview(Path(tmp))
        assert overview["available"] is True
        assert overview["counts"]["short"] == 1


def test_threaded_requests_do_not_interleave_state() -> None:
    """并发请求不该互相干扰（服务是 ThreadingHTTPServer）。"""

    with tempfile.TemporaryDirectory() as tmp:
        _fixture(tmp)
        panel = Panel({"data_root": tmp}, WebAccess(mode=LOCAL, token=TOKEN))
        server = PanelServer(panel, host="127.0.0.1", port=0)
        server.start()
        client = _Client(server.bound_port)
        results: list[int] = []

        def worker():
            for _ in range(5):
                results.append(client("/api/overview")[0])

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        server.stop()
        assert results and set(results) == {200}
