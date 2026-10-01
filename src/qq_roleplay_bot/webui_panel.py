"""WebUI 面板（Stage 4 后台插件）：本地 HTTP 控制面。

**它是插件，不是核心**（2026-10-01 用户："面板属于 stage4 内容，本质插件"）：

- 形状与 `join_approval.JoinApprovalPoller` 一样：`name` / `interval_seconds` / `poll_once()`
  （外加 `close()`——那一条原来是文档里写了却没人调，顺手对齐了实现）；
- **拿不到 `transport`、拿不到 `engine`**。它手上只有装配点注入的一串闭包：
  `state_reader` / `apply_overrides` / `execute_action` / `memory_ops` / `prompt_library`
  / `knowledge` / `control_audit` / `self_id`。权限判定（token / 口令）在 `webui_access`，
  动作执行在核心——插件只做"把 HTTP 翻译成闭包调用"。
- **HTTP 服务跑在线程里**（标准库的阻塞式 `ThreadingHTTPServer`），异步的节拍只负责
  "看好它、崩了拉起来、退出时收干净"。这样不占事件循环，也不用把每个请求都塞进 asyncio。

安全（两套接入的细节在 `webui_access.py`，这里只负责调它）：

1. **所有端点都要认证**（含 `/health`）——未认证拿不到任何数据，连"这个端口上有没有东西"
   都不告诉外面；`remote` 模式下认证是口令换会话 cookie + CSRF。
2. **写动作必须带 `confirm: true`**：踢人、撤回、改群名、删记忆都不可逆，
   面板上再来一次确认；接口层也拦一道（不靠前端自觉）。
3. **请求体上限 256KB**（prompt 全文要传得进来，但别拿它当上传通道）。
4. **静态资源只认三个名字**，其余 404；没有路径拼接，也就没有穿越。
"""

from __future__ import annotations

import inspect
import json
import logging
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from urllib.parse import parse_qs, urlparse

from . import webui_data
from .webui_access import AccessDecision, WebAccess

logger = logging.getLogger(__name__)

#: 请求体上限（prompt 全文用得上，但不该拿它当上传通道）。
MAX_BODY_BYTES = 256 * 1024
#: 静态资源白名单 → MIME。
STATIC: dict[str, str] = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
}
#: 面板自己的节拍：只做"看护线程"。
PANEL_TICK_SECONDS = 30.0

#: 跨线程跑协程的闭包（`runtime` 装配时用 `install_async_runner` 填）。
#: 定义放在模块顶部：`_call` 在运行时读它，**不能依赖"定义在文件后面"**
#: （第一次踩：调度顺序在定义之前，于是每个动作都变成 NameError → 500）。
_ASYNC_RUNNER = None


def install_async_runner(runner) -> None:
    """装配点注入"把协程丢回主事件循环"的闭包。"""

    global _ASYNC_RUNNER
    _ASYNC_RUNNER = runner


def _now() -> float:
    return time.time()


class Response:
    """一次响应。`headers` 是**列表**而不是 dict：`Set-Cookie` 必须能出现多次。"""

    __slots__ = ("status", "body", "content_type", "headers")

    def __init__(self, status: int, body: bytes, content_type: str,
                 headers: list[tuple[str, str]] | None = None) -> None:
        self.status = status
        self.body = body
        self.content_type = content_type
        self.headers = list(headers or [])


def json_response(payload: dict[str, object], *, status: int = 200,
                  headers: dict[str, str] | list[tuple[str, str]] | None = None) -> Response:
    body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    pairs = list(headers.items()) if isinstance(headers, dict) else list(headers or [])
    return Response(status, body, "application/json; charset=utf-8", pairs)


def _cookie_headers(cookies: tuple[str, ...]) -> list[tuple[str, str]]:
    """把若干 cookie 值变成若干条 `Set-Cookie` 头（一条头只能放一个 cookie）。"""

    return [("Set-Cookie", value) for value in cookies or () if value]


def _envelope(data: object, *, errors: list[str] | None = None) -> dict[str, object]:
    return {"generated_at": round(_now(), 3), "data": data, "errors": list(errors or [])}


def failure(error: str, *, detail: str = "", status: int = 200) -> Response:
    body: dict[str, object] = {"ok": False, "error": error}
    if detail:
        body["detail"] = detail
    return json_response(body, status=status)


def _static(name: str, content_type: str) -> Response:
    try:
        text = resources.files("qq_roleplay_bot.webui").joinpath(name).read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):  # pragma: no cover - 打包缺文件时的降级
        return failure("static_missing", detail=name, status=404)
    # **必须显式禁缓存**（2026-10-01 实测踩到）：静态文件不带任何缓存头时，浏览器会
    # 按 `Last-Modified` 猜一个新鲜期，于是改了 `app.css` 刷新页面还是旧样式——
    # 看起来像"改了没生效"，实际是缓存。面板是本机小工具，每次读文件的开销无所谓。
    return Response(200, text.encode("utf-8"), content_type,
                    headers=[("Cache-Control", "no-store")])


def _body_dict(raw: bytes) -> dict[str, object]:
    if not raw:
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


class _Lazy:
    """延后取值的接缝。

    由来（2026-10-01 现场踩到）：`memory_ops` 挂在引擎上的时机**晚于**装配点取接缝——
    `build_engine` 先建引擎、`serve` 起了记忆服务之后才把 store 交进去。装配点当时
    把 `getattr(engine, "memory_ops", None)` 直接存下来，于是面板拿到的是那一刻的
    `None`，记忆那两个按钮永远返回 503。用"取值的闭包"包一层就没有这个顺序依赖了。
    """

    __slots__ = ("getter",)

    def __init__(self, getter) -> None:
        self.getter = getter

    def value(self):
        return self.getter()


def _wants_html(headers: dict[str, str]) -> bool:
    """这是不是"浏览器直接开了一个页面"（而不是页面里的 JS 在发请求）？

    2026-10-01：有人在浏览器里打开了**接口地址**（不是首页），屏幕上就只有一行
    `{"ok": false, "error": "unauthorized"}`——浏览器把 401 的 JSON 当纯文本渲染了，
    看起来像面板坏了。顶层导航会在 `Accept` 里写 `text/html`，而 `fetch()` 默认是
    `*/*` 或 `application/json`，用这一点把两种情况分开：前者给引导页，后者照旧给 JSON。
    """

    accept = str(headers.get("accept", "")).casefold()
    return "text/html" in accept and "application/json" not in accept


#: 直接访问 API 却没带令牌时给的引导页（比一行裸 JSON 有用得多）。
_GUIDE_HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>云茹 · 控制面板</title>
<style>
  body { margin:0; padding:40px 24px; background:#14161a; color:#e6e8ee;
         font:15px/1.7 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif; }
  code { background:#232733; padding:2px 6px; border-radius:4px; }
  a { color:#7aa2f7; }
</style></head><body>
<h1 style="font-size:18px">这里是接口地址，不是页面</h1>
<p>控制面板的首页在 <a href="/">http://127.0.0.1:8790/</a>。</p>
<p>打开首页后，把 <code>data/webui_token</code> 里的内容填进右上角的「令牌」框，
再点「记住」。没带令牌的请求一律回 401，所以直接在地址栏访问接口只会看到一段 JSON。</p>
</body></html>
"""


def _unauthorized(method: str, path: str, headers: dict[str, str], reason: str,
                  status: int) -> Response:
    """未认证时的回应：浏览器导航给引导页，接口调用给 JSON。"""

    if method in {"GET", "HEAD"} and path.startswith("/api/") and _wants_html(headers):
        return Response(status, _GUIDE_HTML.encode("utf-8"), "text/html; charset=utf-8")
    return failure(reason or "unauthorized", status=status)


class Panel:
    """把 HTTP 请求翻译成核心闭包调用。**无状态、可单测**（不起线程）。"""

    def __init__(self, seams: dict[str, object], access: WebAccess, *,
                 started_at: float = 0.0) -> None:
        self.seams = seams or {}
        self.access = access
        self.started_at = started_at or _now()
        self.requests = 0
        self.write_requests = 0
        self.errors = 0

    # --- 接缝（全部可缺省：缺了就返回"这项没启用"，而不是崩） ------------------

    def _seam(self, name: str):
        value = self.seams.get(name)
        if isinstance(value, _Lazy):
            try:
                return value.value()
            except Exception:  # noqa: BLE001 - 取不到就当这项没启用
                logger.warning("webui_seam_failed name=%s", name, exc_info=True)
                return None
        return value

    @property
    def root(self):
        from pathlib import Path

        base = self._seam("data_root")
        return Path(base) if base else None

    # --- 认证 -------------------------------------------------------------

    #: 不需要认证的路径：**面板的外壳**（首页 + 两个静态文件）与健康检查。
    #: 理由：外壳里一个字的秘密都没有（就是 HTML/CSS/JS），而令牌要在**页面里**填——
    #: 外壳要是也拦，就死锁了：页面要令牌才给、令牌要在页面里填。
    #: 2026-10-01 现场事故正是这个：浏览器打开 `/` 拿到 401 JSON，满屏只有
    #: `{"ok": false, "error": "unauthorized"}`，看起来像面板坏了。
    PUBLIC_PATHS = frozenset({"/", "/index.html", "/app.css", "/app.js", "/health"})

    def authorize(self, method: str, path: str, headers: dict[str, str],
                  peer: str) -> AccessDecision | None:
        """返回 None 表示已通过；否则是"该直接回给对面的"决定。

        两类路径**不要求凭据**：公开外壳（见 `PUBLIC_PATHS`）与登录端点。
        远程模式下它们仍然要过**来源校验**（挡跨站），只是不要求已有会话。
        """

        if path in self.PUBLIC_PATHS or path == "/api/login":
            if self.access.remote and not self.access.origin_ok(headers):
                return AccessDecision(False, "bad_origin", 403)
            return None
        if self.access.remote:
            if not self.access.origin_ok(headers):
                return AccessDecision(False, "bad_origin", 403)
            return self.access.check_cookie(headers, peer)
        return self.access.check_bearer(headers, peer)

    # --- 入口 -------------------------------------------------------------

    def handle(self, method: str, target: str, body: bytes,
               headers: dict[str, str], peer: str) -> Response:
        self.requests += 1
        parsed = urlparse(target or "/")
        path = parsed.path or "/"
        query = {key: values[0] for key, values in parse_qs(parsed.query).items()}
        lowered = {str(k).casefold(): str(v) for k, v in (headers or {}).items()}
        # 记一行**请求路径**（不含任何正文）：排"浏览器到底在请求什么"这类问题时，
        # 这是唯一能定论的证据（2026-10-01 排查"打开面板看到一行 JSON"时加的）。
        logger.info("webui_request method=%s path=%s peer=%s accept=%s",
                    method, path, peer, lowered.get("accept", "")[:60])

        # 静态资源也走认证（本机面板没必要给未认证者发 JS，远程更不该）。
        decision = self.authorize(method, path, lowered, peer)
        if decision is not None and not decision.ok:
            if decision.reason == "too_many_attempts":
                return failure("too_many_attempts", status=429)
            return _unauthorized(method.upper(), path, lowered,
                                 decision.reason or "unauthorized",
                                 decision.status or 401)

        try:
            return self._route(method.upper(), path, query, body, lowered, peer)
        except Exception:  # noqa: BLE001 - 面板的异常绝不冒泡到对话路径
            self.errors += 1
            logger.warning("webui_request_failed path=%s", path, exc_info=True)
            return failure("internal_error", status=500)

    def _route(self, method: str, path: str, query: dict[str, str], body: bytes,
               headers: dict[str, str], peer: str) -> Response:
        write = method in {"POST", "PATCH", "DELETE"}
        if write and len(body) > MAX_BODY_BYTES:
            return failure("body_too_large", status=413)
        payload = _body_dict(body) if write else {}
        source = self.access.source_of(peer, headers)
        if write and not self.access.write_allowed(source):
            return failure("rate_limited", status=429)
        # `/api/login` 是**没有会话时唯一能打的写端点**，所以它不能要求 CSRF——
        # 那一半 cookie 正是这次登录要发的。对它做校验会让远程模式**永远登不进去**
        # （2026-10-01 真 HTTP 集成测试抓到：登录一律 403）。
        # 它自己的防护是口令比对 + 按来源的失败限速，不靠 CSRF。
        if write and self.access.remote and path != "/api/login" \
                and not self.access.csrf_ok(headers):
            return failure("bad_csrf", status=403)
        if write:
            self.write_requests += 1

        if path.startswith("/api/"):
            return self._api(method, path, query, payload, source)
        if method == "GET" and path == "/health":
            return json_response(_envelope(webui_data.read_health(
                self._seam("engine"), started_at=self.started_at)))
        if method == "GET" and path in STATIC:
            name, content_type = STATIC[path]
            return _static(name, content_type)
        return failure("not_found", status=404)

    # --- 读接口 -----------------------------------------------------------

    def _api(self, method: str, path: str, query: dict[str, str],
             payload: dict[str, object], source: str) -> Response:
        engine = self._seam("engine")
        root = self.root
        limit = _as_int(query.get("limit"), 50)
        offset = _as_int(query.get("offset"), 0)

        if method == "GET":
            if path == "/api/overview":
                return json_response(_envelope({
                    "health": webui_data.read_health(engine, started_at=self.started_at),
                    "state": webui_data.read_state(engine, root),
                    "usage": webui_data.read_usage(root),
                    "flags": self._flags(),
                    "access": self.access.stats(),
                }))
            if path == "/api/state":
                return json_response(_envelope(webui_data.read_state(engine, root)))
            if path == "/api/usage":
                return json_response(_envelope(webui_data.read_usage(root)))
            if path == "/api/session/messages":
                session = str(query.get("session_id") or "")
                if not session:
                    return failure("bad_request", detail="缺 session_id", status=400)
                return json_response(_envelope(
                    webui_data.read_session_messages(root, session, limit=limit)))
            if path == "/api/memory/overview":
                return json_response(_envelope(webui_data.read_memory_overview(root)))
            if path == "/api/memory/problem":
                which = str(query.get("filter") or "stale")
                try:
                    return json_response(_envelope(
                        webui_data.read_problem_records(root=root, which=which, limit=limit)))
                except ValueError:
                    return failure("bad_request", detail="filter 不合法", status=400)
            if path.startswith("/api/memory/"):
                table = path.rsplit("/", 1)[-1]
                if table not in webui_data._LIST_SQL:
                    return failure("not_found", status=404)
                filters = {key: query[key] for key in query
                           if key not in {"limit", "offset", "q"}}
                # 「人物画像」与「旧事」分两块列（用户 2026-10-01）：
                # `kind=profile` 是一条人一条、跟着人走、不进按词检索，本来就不是同一层。
                #   `/api/memory/records?kind=profile` → 只看画像
                #   `/api/memory/records`（默认）      → 旧事，**排除**画像
                kinds: tuple[str, ...] | None = None
                exclude: tuple[str, ...] = ()
                if table == "records":
                    wanted = str(query.get("kind") or "").strip()
                    if wanted == "profile":
                        kinds = ("profile",)
                    elif not wanted:
                        exclude = ("profile",)
                try:
                    return json_response(_envelope(webui_data.read_memory_table(
                        table, root=root, filters=filters, query=str(query.get("q") or ""),
                        limit=limit, offset=offset,
                        kinds=kinds, exclude_kinds=exclude)))
                except ValueError:
                    return failure("bad_request", detail="表名不合法", status=400)
            if path == "/api/logs":
                try:
                    return json_response(_envelope(
                        webui_data.read_logs(str(query.get("feature") or ""), root=root,
                                             limit=limit)))
                except ValueError:
                    return failure("bad_request", detail="feature 不合法", status=400)
            if path == "/api/settings":
                return json_response(_envelope(self._settings_view()))
            if path == "/api/prompts":
                return json_response(_envelope(self._prompts_view()))
            if path == "/api/prompts/versions":
                return self._prompt_versions(query)
            if path == "/api/knowledge/overview":
                return json_response(_envelope(self._knowledge_overview()))
            if path == "/api/knowledge/chunks":
                return json_response(_envelope(self._knowledge_chunks(query, limit, offset)))
            if path == "/api/knowledge/operator":
                return json_response(_envelope(self._operator_chunks()))
            if path == "/api/approvals/pending":
                return self._pending_approvals()
            if path == "/api/actions/preview":
                return json_response(_envelope({"note": "写动作都要 confirm=true；"
                                                        "这里只回可用的动作清单",
                                                "actions": _ACTION_CATALOG}))
            if path == "/api/history":
                audit = self._seam("control_audit")
                rows = audit.tail(limit) if audit is not None else []
                return json_response(_envelope({"rows": rows}))
            if path == "/health":
                return json_response(_envelope(webui_data.read_health(
                    engine, started_at=self.started_at)))
            return failure("not_found", status=404)

        return self._write(path, payload, source)

    # --- 写接口 -----------------------------------------------------------

    def _write(self, path: str, payload: dict[str, object], source: str) -> Response:
        # 登录是本机/远程都有的第一道门（local 模式下用 token，不需要它）。
        if path == "/api/login":
            decision = self.access.login(str(payload.get("password") or ""),
                                         source or "unknown")
            if not decision.ok:
                return failure(decision.reason or "unauthorized", status=decision.status or 401)
            return json_response(_envelope({"ok": True}),
                                headers=_cookie_headers(decision.cookies))
        if path == "/api/logout":
            self.access.logout({})
            return json_response(_envelope({"ok": True}))
        if not _confirmed(payload):
            return failure("needs_confirmation",
                           detail="危险动作必须在请求里带 confirm=true", status=400)

        if path == "/api/control/restart":
            return self._restart(payload, source)
        if path == "/api/action":
            return self._action(payload, source)
        if path == "/api/approval":
            return self._approval(payload, source)
        if path == "/api/groups":
            return self._groups(payload, source)
        if path == "/api/session/clear":
            return self._clear_session(payload, source)
        if path == "/api/memory/purge":
            return self._memory_purge(payload, source)
        if path == "/api/memory/affinity":
            return self._memory_affinity(payload, source)
        if path == "/api/settings":
            return self._settings(payload, source)
        if path == "/api/prompts":
            return self._prompt_write(payload, source)
        if path == "/api/knowledge/operator":
            return self._knowledge_write(payload, source)
        if path == "/api/knowledge/rebuild":
            return self._knowledge_rebuild(source)
        return failure("not_found", status=404)

    # --- 具体动作 ---------------------------------------------------------

    def _restart(self, payload: dict[str, object], source: str) -> Response:
        engine = self._seam("engine")
        if engine is None:
            return failure("engine_unavailable", status=503)
        gate = self._seam("restart_gate")
        if callable(gate) and not gate():
            return failure("rate_limited", detail="刚刚才重启过", status=429)
        text = engine.request_restart(f"webui:{source or 'unknown'}")
        self._audit("restart", payload, text, source)
        return json_response(_envelope({"ok": True, "message": text}), status=202)

    def _action(self, payload: dict[str, object], source: str) -> Response:
        execute = self._seam("execute_action")
        self_id = self._seam("self_id")
        if execute is None:
            return failure("engine_unavailable", status=503)
        actor = _call(self_id) if callable(self_id) else ""
        if not actor:
            # **fail-closed**：取不到"她自己是谁"就不动手（用户 2026-10-01：
            # "群管理动作的 actor 为云茹"，那她的身份就是前提，不能拿别人顶替）。
            return failure("self_id_unavailable",
                           detail="查不到云茹自己的 QQ 号，本次不动手", status=503)
        group = str(payload.get("group") or "group_admin")
        kind = str(payload.get("kind") or "")
        from .command_plugins import ActionRequest

        request = ActionRequest(
            group=group, kind=kind,
            target_id=str(payload.get("target_id") or ""),
            text=str(payload.get("text") or ""),
            mentioned=True,   # 面板=已经确认过目标（踢人那条护栏本来就要求明确指定）
            message_id=str(payload.get("message_id") or ""),
        )
        text = _call(execute, request, group_id=str(payload.get("group_id") or ""),
                     actor_id=actor, message_id=str(payload.get("message_id") or ""),
                     mentioned=True)
        self._audit("action", payload, str(text), source, actor=actor)
        return json_response(_envelope({"ok": True, "message": text, "actor_id": actor}))

    def _approval(self, payload: dict[str, object], source: str) -> Response:
        call_action = self._seam("call_action")
        if call_action is None:
            return failure("engine_unavailable", status=503)
        flag = str(payload.get("flag") or "")
        group = str(payload.get("group_id") or "")
        if not flag or not group:
            return failure("bad_request", detail="缺 flag 或 group_id", status=400)
        approve = bool(payload.get("approve"))
        params = {
            "flag": flag,
            "sub_type": "add",
            "approve": approve,
        }
        reason = str(payload.get("reason") or "").strip()
        if not approve and reason:
            params["reason"] = reason
        try:
            _call(call_action, "set_group_add_request", params)
        except Exception as exc:  # noqa: BLE001 - 对面拒绝就如实回报
            self._audit("approval", payload, f"failed:{type(exc).__name__}", source)
            return failure("action_failed", detail=type(exc).__name__, status=502)
        text = "已通过这条申请。" if approve else "已拒绝这条申请。"
        self._audit("approval", payload, text, source)
        return json_response(_envelope({"ok": True, "message": text}))

    def _pending_approvals(self) -> Response:
        call_action = self._seam("call_action")
        if call_action is None:
            return json_response(_envelope({"available": False, "rows": []}))
        try:
            data = _call(call_action, "get_group_system_msg", {})
        except Exception:  # noqa: BLE001 - 拿不到就当没有待处理
            return json_response(_envelope({"available": False, "rows": []}))
        rows: list[dict[str, object]] = []
        if isinstance(data, dict):
            from .join_approval import JOIN_SUB_TYPE, parse_pending

            for item in parse_pending(data):
                if getattr(item, "sub_type", JOIN_SUB_TYPE) != JOIN_SUB_TYPE:
                    continue
                rows.append({"flag": item.flag, "group_id": item.group_id,
                             "user_id": item.user_id, "nickname": item.nickname,
                             "comment": item.comment})
        return json_response(_envelope({"available": True, "rows": rows}))

    def _groups(self, payload: dict[str, object], source: str) -> Response:
        engine = self._seam("engine")
        if engine is None:
            return failure("engine_unavailable", status=503)
        group_id = str(payload.get("group_id") or "").strip()
        enabled = bool(payload.get("enabled"))
        if not (group_id.isdigit() and 5 <= len(group_id) <= 12):
            return failure("bad_request", detail="群号不合法", status=400)
        changed = engine.enable_group(group_id) if enabled else engine.disable_group(group_id)
        text = ("已开启" if enabled else "已关闭") + f"群 {group_id} 的对话"
        text += "。" if changed else "（本来就是这个状态）。"
        self._audit("group_switch", payload, text, source)
        return json_response(_envelope({"ok": True, "changed": bool(changed), "message": text}))

    def _clear_session(self, payload: dict[str, object], source: str) -> Response:
        engine = self._seam("engine")
        if engine is None:
            return failure("engine_unavailable", status=503)
        session = str(payload.get("session_id") or "").strip()
        if not session:
            return failure("bad_request", detail="缺 session_id", status=400)
        cleared = bool(engine.leave_session(session))
        self._audit("session_clear", payload, f"cleared={cleared}", source)
        return json_response(_envelope({"ok": True, "cleared": cleared}))

    def _memory_purge(self, payload: dict[str, object], source: str) -> Response:
        from .memory_ops import MemoryOpRejected

        ops = self._seam("memory_ops")
        if ops is None or not getattr(ops, "available", False):
            return failure("memory_unavailable", status=503)
        ids = payload.get("record_ids")
        if not isinstance(ids, list):
            return failure("bad_request", detail="record_ids 要是数组", status=400)
        try:
            removed = ops.purge(ids, reason=str(payload.get("reason") or ""))
        except MemoryOpRejected as exc:
            return failure("bad_request", detail=str(exc), status=400)
        self._audit("memory_purge", payload, f"removed={removed}", source)
        return json_response(_envelope({
            "ok": True, "removed": removed,
            "message": f"已删除 {removed} 条（原文已归档，保留 30 天）",
        }))

    def _memory_affinity(self, payload: dict[str, object], source: str) -> Response:
        from .memory_ops import MemoryOpRejected

        ops = self._seam("memory_ops")
        if ops is None or not getattr(ops, "available", False):
            return failure("memory_unavailable", status=503)
        try:
            if payload.get("reset"):
                changed = ops.reset_affinity(str(payload.get("user_id") or ""))
                self._audit("affinity_reset", payload, f"changed={changed}", source)
                return json_response(_envelope({"ok": True, "changed": bool(changed)}))
            values = ops.affinity(str(payload.get("user_id") or ""),
                                  str(payload.get("axis") or ""),
                                  _as_int(payload.get("delta"), 0))
        except MemoryOpRejected as exc:
            return failure("bad_request", detail=str(exc), status=400)
        self._audit("affinity", payload, str(values), source)
        return json_response(_envelope({"ok": True, "values": values}))

    def _settings(self, payload: dict[str, object], source: str) -> Response:
        apply_overrides = self._seam("apply_overrides")
        engine = self._seam("engine")
        if apply_overrides is None:
            return failure("engine_unavailable", status=503)
        body = {key: value for key, value in payload.items() if key != "confirm"}
        applied = _call(apply_overrides, engine, body,
                        audit=self._seam("control_audit"), source=source,
                        token=str(self._seam("token") or ""))
        return json_response(_envelope({"ok": True, "applied": applied,
                                        "settings": self._settings_view()}))

    def _prompt_write(self, payload: dict[str, object], source: str) -> Response:
        from .prompt_library import PromptRejected, PROMPTS

        library = self._seam("prompt_library")
        if library is None:
            return failure("engine_unavailable", status=503)
        action = str(payload.get("action") or "save")
        name = str(payload.get("name") or "")
        if name not in PROMPTS:
            return failure("bad_request", detail="不认识的 prompt", status=400)
        try:
            if action == "reset":
                library.reset(name, source=source)
            elif action == "restore":
                text = library.read_version(name, str(payload.get("version") or ""))
                library.save(name, text, source=source)
            else:
                library.save(name, str(payload.get("text") or ""), source=source)
        except PromptRejected as exc:
            self._audit("prompt", {"name": name, "action": action}, str(exc), source)
            return failure("prompt_rejected", detail=exc.detail or str(exc), status=400)
        self._audit("prompt", {"name": name, "action": action,
                               "chars": len(str(payload.get("text") or ""))},
                    "saved", source)
        return json_response(_envelope({"ok": True, "prompts": self._prompts_view()}))

    def _knowledge_write(self, payload: dict[str, object], source: str) -> Response:
        from .knowledge_operator import KnowledgeRejected

        knowledge = self._seam("knowledge")
        if knowledge is None or not hasattr(knowledge, "add_chunk"):
            return failure("knowledge_unavailable", status=503)
        action = str(payload.get("action") or "add")
        try:
            if action == "add":
                chunk = knowledge.add_chunk(
                    title=str(payload.get("title") or ""),
                    content=str(payload.get("content") or ""),
                    note=str(payload.get("note") or ""))
            elif action == "update":
                chunk = knowledge.update_chunk(
                    str(payload.get("id") or ""),
                    title=payload.get("title"), content=payload.get("content"),
                    note=payload.get("note"))
            elif action == "delete":
                removed = knowledge.delete_chunk(str(payload.get("id") or ""))
                self._audit("knowledge", payload, f"deleted={removed}", source)
                return json_response(_envelope({"ok": True, "removed": bool(removed)}))
            elif action == "restore":
                count = knowledge.restore_chunks(str(payload.get("version") or ""))
                self._audit("knowledge", payload, f"restored={count}", source)
                return json_response(_envelope({"ok": True, "count": count}))
            else:
                return failure("bad_request", detail="不认识的 action", status=400)
        except KnowledgeRejected as exc:
            self._audit("knowledge", payload, str(exc), source)
            return failure("knowledge_rejected", detail=exc.detail or str(exc), status=400)
        self._audit("knowledge", payload, f"saved:{chunk.id}", source)
        return json_response(_envelope({"ok": True, "chunk": chunk.as_row(),
                                        "chunks": self._operator_chunks()}))

    def _knowledge_rebuild(self, source: str) -> Response:
        rebuild = self._seam("knowledge_rebuild")
        if not callable(rebuild):
            return failure("not_supported",
                           detail="这次没接重建入口（离线索引由 knowledge_tool 建）",
                           status=501)
        try:
            stats = _call(rebuild)
        except Exception as exc:  # noqa: BLE001 - 重建失败如实回报，不影响对话
            self._audit("knowledge_rebuild", {}, f"failed:{type(exc).__name__}", source)
            return failure("rebuild_failed", detail=type(exc).__name__, status=500)
        self._audit("knowledge_rebuild", {}, str(stats), source)
        return json_response(_envelope({"ok": True, "stats": stats}))

    # --- 视图 -------------------------------------------------------------

    def _flags(self) -> dict[str, bool]:
        from . import runtime_flags

        flags = runtime_flags.shared()
        return flags.snapshot() if flags is not None else {}

    def _settings_view(self) -> dict[str, object]:
        from . import provider_registry, runtime_flags
        from .operator_config import SETTINGS, shared as shared_config, normalize

        config = self._seam("operator_config")
        if config is None or not hasattr(config, "values"):
            config = shared_config()
        stored = dict(getattr(config, "values", {}) or {})
        values = webui_data.mask_settings(stored)
        flags = runtime_flags.shared()
        live: dict[str, bool] = flags.snapshot() if flags is not None else {}
        fields: list[dict[str, object]] = []
        for key, spec in SETTINGS.items():
            # **"当前"要显示真正在生效的值**，不是"覆盖层里有没有写"：
            # 没被面板改过的项，生效值来自 `.env` / 默认值，显示空白会让人以为它没配。
            # 凭据类照旧只给掩码（`mask_secret`），明文永不出现在响应里。
            env = str(spec["env"])
            raw = stored.get(key, os.environ.get(env, ""))
            if spec.get("kind") == "secret":
                current: object = webui_data.mask_secret(os.environ.get(env, ""))
            elif spec.get("kind") == "bool":
                # 开关类显示"开 / 关"。**不能直接用覆盖层的值**：这些项在 `.env` 里
                # 往往是空的，而空 = 默认**开**。照原样显示空串，面板上就是一片 "—",
                # 看着像"全都没开"（实测就是这样）。
                # `applies="live"` 的项还要更准一档：直接读**运行中那份开关**，
                # 它才是真正生效的值（覆盖层改了、热更之后，这里立刻看得到）。
                if spec.get("applies") == "live" and key in live:
                    on = bool(live[key])
                else:
                    try:
                        # 空值按"默认开"算：这些开关的代码默认都是开着的。
                        on = normalize(key, raw.strip() or "1") == "1"
                    except Exception:  # noqa: BLE001 - 配置脏了也不该让整页读不出来
                        on = False
                current = "开" if on else "关"
            else:
                current = values.get(key) or raw
            fields.append({
                "key": key, "kind": spec["kind"], "applies": spec["applies"],
                "current": current,
                "overridden": key in stored,
                "note": runtime_flags.FLAG_NOTES.get(key, ""),
            })
        return {
            "fields": fields,
            "flags": live,
            "providers": provider_registry.options(),
            "overridden": sorted(values),
        }

    def _prompts_view(self) -> dict[str, object]:
        from .prompt_library import PROMPTS

        library = self._seam("prompt_library")
        if library is None:
            return {"available": False, "prompts": []}
        rows: list[dict[str, object]] = []
        for name in PROMPTS:
            text = library.text(name)
            rows.append({
                "name": name,
                "chars": len(text),
                "overridden": bool(library.is_overridden(name)),
                "text": text,
                "preview": text[:200],
            })
        return {"available": True, "prompts": rows, "directory": str(library.directory)}

    def _prompt_versions(self, query: dict[str, str]) -> Response:
        from .prompt_library import PromptRejected, PROMPTS

        library = self._seam("prompt_library")
        name = str(query.get("name") or "")
        if library is None or name not in PROMPTS:
            return failure("bad_request", detail="不认识的 prompt", status=400)
        try:
            rows = library.versions(name)
        except PromptRejected as exc:
            return failure("bad_request", detail=str(exc), status=400)
        return json_response(_envelope({"name": name, "versions": rows}))

    def _operator_chunks(self) -> dict[str, object]:
        knowledge = self._seam("knowledge")
        if knowledge is None or not hasattr(knowledge, "chunks"):
            return {"available": False, "chunks": []}
        chunks = knowledge.chunks()
        return {"available": True,
                "chunks": [chunk.as_row() for chunk in chunks],
                "versions": knowledge.chunk_versions()}

    def _knowledge_overview(self) -> dict[str, object]:
        knowledge = self._seam("knowledge")
        if knowledge is None or not hasattr(knowledge, "overview"):
            return {"available": False}
        try:
            return {"available": True, **knowledge.overview()}
        except Exception:  # noqa: BLE001 - 概览读不到不影响人工改块
            logger.warning("webui_knowledge_overview_failed", exc_info=True)
            return {"available": False}

    def _knowledge_chunks(self, query: dict[str, str], limit: int, offset: int) -> dict[str, object]:
        """全库块的模糊浏览（只读）：直接查索引库，**`mode=ro`**。"""

        from .knowledge_operator import index_overview, open_index_readonly

        knowledge = self._seam("knowledge")
        index = getattr(knowledge, "index", None)
        if index is None:
            return {"available": False, "rows": []}
        db = open_index_readonly(index.path)
        if db is None:
            return {"available": False, "rows": [], "overview": index_overview(index)}
        import sqlite3

        capped = max(1, min(webui_data.MAX_LIST, int(limit)))
        skip = max(0, int(offset))
        term = str(query.get("q") or "").strip()
        try:
            db.row_factory = sqlite3.Row
            if term:
                sql = ("SELECT source, title, content, authority FROM chunks "
                       "WHERE content LIKE ? OR title LIKE ? LIMIT ? OFFSET ?")
                params: list[object] = [f"%{term}%", f"%{term}%", capped, skip]
            else:
                sql = ("SELECT source, title, content, authority FROM chunks "
                       "LIMIT ? OFFSET ?")
                params = [capped, skip]
            rows = [dict(row) for row in db.execute(sql, params)]
            total = webui_data._count(db, "SELECT COUNT(*) FROM chunks")
        except sqlite3.Error as exc:
            return {"available": False, "rows": [],
                    "detail": type(exc).__name__}
        finally:
            db.close()
        return {"available": True, "rows": rows, "total": total,
                "limit": capped, "offset": skip,
                "overview": index_overview(index)}

    # --- 审计 -------------------------------------------------------------

    def _audit(self, action: str, payload: dict[str, object], result: str,
               source: str, *, actor: str = "") -> None:
        audit = self._seam("control_audit")
        if audit is None:
            return
        detail = dict(payload or {})
        if actor:
            detail["actor_id"] = actor
        audit.record(action, detail=detail, result=result, source=source,
                     token=str(self._seam("token") or ""))


def _confirmed(payload: dict[str, object]) -> bool:
    value = payload.get("confirm")
    if isinstance(value, bool):
        return value
    return str(value).strip().casefold() in {"1", "true", "yes"}


def _as_int(value: object, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _call(func, *args, **kwargs):
    """调接缝。同步的直接调；异步的丢回主事件循环等结果。

    面板跑在线程里，而 `execute_action` 是协程——注入的 runner 负责跨线程投递
    （`runtime` 装配时给，用的是 `run_coroutine_threadsafe`）。没有那个闭包时
    异步接缝一律不可用，**不自己猜事件循环**。
    """

    result = func(*args, **kwargs)
    if not inspect.isawaitable(result):
        return result
    if _ASYNC_RUNNER is None:
        raise RuntimeError("async seam unavailable")
    return _ASYNC_RUNNER(result)


#: 给 `/api/actions/preview` 的动作清单（面板渲染表单用）。
_ACTION_CATALOG: list[dict[str, object]] = [
    {"group": "group_admin", "kind": "kick", "label": "移出群聊", "needs_target": True},
    {"group": "group_admin", "kind": "ban", "label": "禁言", "needs_target": True,
     "needs_text": "分钟数"},
    {"group": "group_admin", "kind": "unban", "label": "解除禁言", "needs_target": True},
    {"group": "group_admin", "kind": "mute", "label": "全员禁言", "needs_target": False},
    {"group": "group_admin", "kind": "unmute", "label": "解除全员禁言", "needs_target": False},
    {"group": "group_admin", "kind": "recall", "label": "撤回一条消息", "needs_target": False,
     "needs_message": True},
    {"group": "group_owner", "kind": "qqadmin", "label": "设为 QQ 群管理员",
     "needs_target": True},
    {"group": "group_owner", "kind": "unqqadmin", "label": "取消 QQ 群管理员",
     "needs_target": True},
    {"group": "group_owner", "kind": "card", "label": "改群名片", "needs_target": True,
     "needs_text": "新名片（留空=清除）"},
    {"group": "group_owner", "kind": "groupname", "label": "改群名", "needs_target": False,
     "needs_text": "新群名"},
    {"group": "group_owner", "kind": "title", "label": "设群头衔", "needs_target": True,
     "needs_text": "头衔"},
    {"group": "group_owner", "kind": "notice", "label": "发群公告", "needs_target": False,
     "needs_text": "公告正文"},
]


class _Handler(BaseHTTPRequestHandler):
    """薄薄一层：把 stdlib 的请求交给 `Panel.handle`。"""

    panel: Panel
    server_version = "YunruPanel/1"
    sys_version = ""

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003 - 基类签名
        logger.debug("webui_http %s", fmt % args)

    def _headers(self) -> dict[str, str]:
        return {key: value for key, value in self.headers.items()}

    def _respond(self, response: Response) -> None:
        self.send_response(response.status)
        self.send_header("Content-Type", response.content_type)
        self.send_header("Content-Length", str(len(response.body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (response.headers or []):
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(response.body)

    def _dispatch(self) -> None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        body = self.rfile.read(length) if length > 0 else b""
        peer = self.client_address[0] if self.client_address else ""
        response = self.panel.handle(self.command, self.path, body, self._headers(), peer)
        self._respond(response)

    do_GET = _dispatch
    do_POST = _dispatch
    do_PATCH = _dispatch
    do_DELETE = _dispatch
    do_HEAD = _dispatch


class PanelServer:
    """HTTP 服务的生命周期（线程）。插件只持有它。"""

    def __init__(self, panel: Panel, *, host: str, port: int) -> None:
        self.panel = panel
        self.host = host
        self.port = int(port)
        # `port=0` 让内核挑一个（测试用）；实际端口从 `self.port` 读。
        self.httpd: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self.started_at = 0.0
        self.last_error = ""
        self.starts = 0
        # 关掉之后 `self.httpd` 是 None，但"刚才绑的是哪个端口"要留着——日志与面板
        # 都靠它说话（`--port 0` 让内核挑端口时更是唯一线索）。
        self.actual_port = int(port)

    @property
    def bound_port(self) -> int:
        if self.httpd is None:
            return self.actual_port
        return int(self.httpd.server_address[1])

    def start(self) -> bool:
        if self.thread is not None and self.thread.is_alive():
            return True
        try:
            handler = type("_BoundHandler", (_Handler,), {"panel": self.panel})
            self.httpd = ThreadingHTTPServer((self.host, self.port), handler)
            self.httpd.daemon_threads = True
        except OSError as exc:
            self.last_error = type(exc).__name__
            logger.warning("webui_listen_failed host=%s port=%s category=%s",
                           self.host, self.port, type(exc).__name__)
            return False
        self.thread = threading.Thread(target=self._serve, name="webui-panel", daemon=True)
        self.thread.start()
        self.started_at = time.time()
        self.starts += 1
        self.actual_port = int(self.httpd.server_address[1])
        self.last_error = ""
        logger.info("面板已监听：http://%s:%s/（模式 %s）", self.host, self.actual_port,
                    self.panel.access.mode)
        return True

    def _serve(self) -> None:
        assert self.httpd is not None
        try:
            self.httpd.serve_forever(poll_interval=0.5)
        except Exception:  # noqa: BLE001 - 线程不许把异常带走整个进程
            self.last_error = "serve_crashed"
            logger.warning("webui_serve_crashed", exc_info=True)

    @property
    def alive(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def stop(self) -> None:
        if self.httpd is not None:
            try:
                self.httpd.shutdown()
                self.httpd.server_close()
            except Exception:  # noqa: BLE001 - 关不干净也要往下走
                logger.debug("webui_shutdown_failed", exc_info=True)
            self.httpd = None
        if self.thread is not None:
            self.thread.join(timeout=3.0)
            self.thread = None

    def stats(self) -> dict[str, object]:
        return {"host": self.host, "port": self.bound_port, "alive": self.alive,
                "starts": self.starts, "last_error": self.last_error}


class WebPanelPlugin:
    """Stage 4 后台插件：看好那个 HTTP 线程。

    形状与其它后台插件一致（`name` / `interval_seconds` / `poll_once()` / `close()`）。
    **它不碰 `transport`，也拿不到引擎**——所有动作都经装配点注入的闭包。
    """

    name = "webui-panel"

    def __init__(self, server: PanelServer, *, interval_seconds: float = PANEL_TICK_SECONDS,
                 enabled: bool = True) -> None:
        self.server = server
        self.interval_seconds = float(interval_seconds)
        self.enabled = bool(enabled)

    async def poll_once(self) -> object:
        """第一次调用就把服务器拉起来（不必等 `FIRST_TICK_DELAY_SECONDS`）。"""

        if not self.server.alive:
            started = self.server.start()
            if not started:
                logger.warning("面板这次没起来（%s），下个节拍再试", self.server.last_error)
            return {"started": started, "port": self.server.bound_port}
        return {"alive": True, "port": self.server.bound_port}

    async def close(self) -> None:
        self.server.stop()
