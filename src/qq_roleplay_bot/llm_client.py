from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import socket
import urllib.error
import urllib.request
import weakref

logger = logging.getLogger(__name__)

# 只有这些失败值得重试：限流、服务端错误和传输层抖动。
# 4xx（除 429）是请求本身的问题，重试没有意义。
RETRYABLE_HTTP_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})
RETRYABLE_KINDS = frozenset({"transport_error"})
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BACKOFF_SECONDS = 0.5
MAX_BACKOFF_SECONDS = 8.0

# 需要保留的 usage 字段。`prompt_cache_hit_tokens` / `prompt_cache_miss_tokens`
# 是 DeepSeek 特有的缓存计量（见 https://api-docs.deepseek.com/guides/kv_cache），
# 其余厂商的字段名不同，缺失时只是不统计，不影响调用。
_USAGE_FIELDS = frozenset({
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "prompt_cache_hit_tokens",
    "prompt_cache_miss_tokens",
})

# user_id 只允许这些字符（服务端要求），且不超过 512；不合法就直接不带，
# 而不是带着一个会被服务端拒绝的值去请求。
_USER_ID_PATTERN = re.compile(r"^[a-zA-Z0-9\-_]{1,512}$")


def _clean_user_id(value: str) -> str:
    """校验并规范化 user_id；不合法则返回空串（等于不隔离）。"""

    text = str(value or "").strip()
    if not text:
        return ""
    if not _USER_ID_PATTERN.fullmatch(text):
        logger.warning("user_id 不合法，已忽略该项隔离设置")
        return ""
    return text


class LLMError(RuntimeError):
    """模型请求失败。

    属性只携带可安全记录的分类信息（`kind`）与有限长度的诊断细节（`detail`），
    绝不包含 API Key、请求头或聊天正文，因此可以安全写入日志。
    """

    def __init__(self, message: str, *, kind: str = "unknown", detail: str = "") -> None:
        super().__init__(message)
        self.kind = kind
        self.detail = detail

    def safe_summary(self) -> str:
        return f"{self.kind}: {self.detail}" if self.detail else self.kind


def _urlopen(request: urllib.request.Request, *, timeout: float):
    """发一次 HTTP 请求。**每次现建 opener**，理由见 `_complete_sync` 里的注释。

    单独包一层是为了有个明确的打桩点：以前测试打的是 `urllib.request.urlopen`
    （模块级全局 opener），换成现建 opener 之后就打不到了。
    """

    return urllib.request.build_opener().open(request, timeout=timeout)


# --- client 登记簿（2026-10-01 面板热更用） ---------------------------------
#
# 由来（用户）："可以修改 api key，可以切换供应商"，而且"能热更的就热更"。
# key / base_url / model 是实例属性、每次请求才读，所以**就地改**就能立即生效；
# 难点只在"改哪些实例"：按会话分了一层 client（每个群/私聊一个），
# 面板改一次必须覆盖**全部**，否则会出现"主群换了新模型、别的群还是旧的"。
#
# 用弱引用：client 是按会话懒建的，会话清掉之后这些对象该被回收，
# 登记簿不该把它们钉在内存里（那等于把"按会话隔离"变成"永不释放"）。
_CLIENTS: "weakref.WeakSet[OpenAICompatibleClient]" = weakref.WeakSet()


def register_client(client: "OpenAICompatibleClient") -> None:
    """登记一个 client（构造时自动调用）。"""

    try:
        _CLIENTS.add(client)
    except TypeError:  # pragma: no cover - 不可弱引用的实现不该出现
        return


def clients() -> list["OpenAICompatibleClient"]:
    """当前活着的全部 client（顺序不保证）。"""

    return [client for client in list(_CLIENTS)]


def apply_client_overrides(*, api_key: str | None = None, base_url: str | None = None,
                           model: str | None = None, usage_role: str = "") -> int:
    """就地更新所有 client 的凭据/地址/模型。返回改了几个。

    - 传 `None` 表示"这一项不动"（空串是**有效值**：表示回落，不在这里处理）；
    - `usage_role` 给了就只改那一类（对话 / 判定 / 记忆 / 审核 / 识图 / 写信），
      不传就全改——面板换供应商是全局的事。
    """

    changed = 0
    for client in clients():
        if usage_role and str(getattr(client, "usage_role", "")) != usage_role:
            continue
        if api_key is not None:
            client.api_key = api_key
        if base_url is not None:
            client.base_url = str(base_url).rstrip("/")
        if model is not None:
            client.model = model
        changed += 1
    return changed


def _transport_detail(exc: BaseException, *, limit: int = 160) -> str:
    """把网络异常的**内层原因**压成一段可安全记录的文字。

    只取异常类型与 `reason`（形如 `[Errno 11001] getaddrinfo failed`、
    `Connection refused`）——不含请求头、URL 查询串或用例正文，可以进日志。
    长度卡住，避免某些实现把整段响应塞进 reason。
    """

    reason = getattr(exc, "reason", None)
    text = f"{type(exc).__name__}: {reason}" if reason else type(exc).__name__
    return " ".join(str(text).split())[:limit]


class ModelRequestError(Exception):
    """内部信号：一次同步请求失败。

    携带尚未字符串化的结构化信息（HTTP 状态码、已读出的响应体），
    使 `complete()` 的重试判定不必从字符串里反解状态码；重试时也不会
    重复读取同一个 HTTPError 的响应流。
    """

    def __init__(self, kind: str, *, status: int = 0, body: str = "", detail: str = "") -> None:
        super().__init__(kind)
        self.kind = kind
        self.status = status
        self.body = body
        self.detail = detail


class OpenAICompatibleClient:
    """使用标准库调用 OpenAI-compatible chat completions 接口。

    一条业务消息仍然只对应一次 `complete()` 调用；`complete()` 内部可能对
    限流、服务端错误和网络抖动做有限重试，这不改变 Stage 3 的“一次调用”语义。
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 60.0,
        *,
        max_tokens: int | None = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        backoff_seconds: float = DEFAULT_BACKOFF_SECONDS,
        user_id: str = "",
        usage_store=None,
        usage_role: str = "",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.max_tokens = max_tokens
        # 面板的热更登记（2026-10-01）：`base_url` / `api_key` / `model` 是**实例属性**，
        # 每次请求现读（`_complete_sync` 里），所以就地改这三个就立即生效。
        # 登记下来是为了让面板改一次能覆盖**所有**已存在的 client（按会话分出来的那些）。
        register_client(self)
        self.max_attempts = max(1, int(max_attempts))
        self.backoff_seconds = max(0.0, float(backoff_seconds))
        # 业务侧实体标识，用于**同一账号下按 agent 隔离** KVCache 与调度。
        # 见 https://api-docs.deepseek.com/zh-cn/quick_start/rate_limit/
        # 注意：并发限制是账号粒度（与 API Key 无关），user_id 只做隔离与细粒度限速。
        self.user_id = _clean_user_id(user_id)
        self.last_usage: dict[str, int] = {}
        self.last_attempts = 0
        self.last_error_kind = ""
        # 进程内累计，用于观察提示词前缀稳定性；只存数字，不含任何内容。
        self.usage_totals: dict[str, int] = {name: 0 for name in _USAGE_FIELDS}
        self.usage_calls = 0
        # 跨重启的累计账本（可选，2026-09-30）：有它才看得到"从开始用到现在"，
        # 而不是"这次重启之后"。`usage_role` 是它记账时的名目（dialogue/judge/memory/…）。
        self.usage_store = usage_store
        self.usage_role = str(usage_role or "")

    def cache_stats(self) -> dict[str, object]:
        """提示词缓存命中情况。命中率低说明请求前缀每次都在变，成本会成倍上升。"""

        hit = self.usage_totals.get("prompt_cache_hit_tokens", 0)
        miss = self.usage_totals.get("prompt_cache_miss_tokens", 0)
        total = hit + miss
        return {
            "calls": self.usage_calls,
            "hit_tokens": hit,
            "miss_tokens": miss,
            "hit_rate": round(hit / total, 4) if total else 0.0,
        }

    def _record_usage(self, usage: dict[str, int]) -> None:
        self.usage_calls += 1
        for key, value in usage.items():
            self.usage_totals[key] = self.usage_totals.get(key, 0) + value
        store = self.usage_store
        if store is not None:
            try:
                store.add_usage(self.usage_role or "unknown", usage)
            except Exception:  # noqa: BLE001 - 记账失败绝不能影响一次调用
                logger.warning("api_usage_record_failed category=%s", "store")

    async def complete(self, messages: list[dict[str, str]]) -> str:
        self.last_usage = {}
        self.last_attempts = 0
        self.last_error_kind = ""
        last_error: LLMError | None = None
        for attempt in range(1, self.max_attempts + 1):
            self.last_attempts = attempt
            try:
                return await asyncio.to_thread(self._complete_sync, messages)
            except ModelRequestError as exc:
                error = self._classify(exc)
                self.last_error_kind = error.kind
                if attempt >= self.max_attempts or not self._is_retryable(exc):
                    raise error from exc
                last_error = error
                delay = self._backoff_delay(attempt)
                logger.warning(
                    "model_retry attempt=%s/%s kind=%s detail=%s delay=%.2fs",
                    attempt,
                    self.max_attempts,
                    error.kind,
                    error.detail,
                    delay,
                )
                await asyncio.sleep(delay)
            except LLMError as exc:
                # 结构性错误（响应格式、空回复）不重试：同样的请求不会变好。
                self.last_error_kind = exc.kind
                raise
        raise last_error if last_error is not None else LLMError("模型请求失败")

    def _classify(self, exc: ModelRequestError) -> LLMError:
        if exc.kind == "http_error":
            detail = f"status={exc.status} body={exc.body}"
        else:
            detail = exc.detail
        return LLMError("模型请求失败", kind=exc.kind, detail=detail)

    def _is_retryable(self, exc: ModelRequestError) -> bool:
        if exc.kind in RETRYABLE_KINDS:
            return True
        return exc.kind == "http_error" and exc.status in RETRYABLE_HTTP_STATUS

    def _backoff_delay(self, attempt: int) -> float:
        """指数退避并加入抖动，避免多个实例同时重试。"""

        base = self.backoff_seconds * (2 ** (attempt - 1))
        return min(MAX_BACKOFF_SECONDS, base) * (0.5 + random.random() / 2)

    def _complete_sync(self, messages: list[dict[str, str]]) -> str:
        body = {
            "model": self.model,
            "messages": messages,
            "stream": False,
        }
        if self.max_tokens is not None:
            body["max_tokens"] = self.max_tokens
        if self.user_id:
            # 同一账号下按 agent 隔离 KVCache 与调度；不带则所有请求共享隔离空间。
            body["user_id"] = self.user_id
        payload = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=payload,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "QQRoleplayBot/0.1",
            },
            method="POST",
        )
        try:
            # **每次调用现建一个 opener**，不要用 `urllib.request.urlopen`：
            # 那个走的是模块级全局 opener，而它在**第一次调用时**就把代理设置读死了
            # （`ProxyHandler` 构造时调一次 `getproxies()`）。踩过（2026-09-29 开机自启）：
            # 开机瞬间系统代理已指向 127.0.0.1:7890，但代理本身还没起来，于是那个进程
            # 之后**每一次**模型调用都走那个死代理，一整天 `URLError` 全废；而新起的
            # 进程（代理设置已恢复）完全正常——现象是"进程活着但谁也联系不上"。
            # 现建现读，代理后来起来了/关掉了都能跟上；代价只是一次对象构造。
            with _urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            # 立即读出并限制响应体：同一个 HTTPError 的流只能读一次，
            # 否则重试时 body 会变成空串，丢掉唯一的排障线索。
            raise ModelRequestError(
                "http_error",
                status=exc.code,
                body=self._safe_error_body(exc),
            ) from exc
        except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
            # detail 里带上**内层原因**（如 `[Errno 11001] getaddrinfo failed`、
            # `Connection refused`）：只记一个 "URLError" 等于什么都没说，
            # 上面那类"走死代理"的问题当初就是因此查了半天。
            raise ModelRequestError("transport_error", detail=_transport_detail(exc)) from exc
        except OSError as exc:
            # ConnectionResetError / ConnectionAbortedError 等不会进入 URLError 分支。
            raise ModelRequestError("transport_error", detail=_transport_detail(exc)) from exc

        try:
            result = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise LLMError("模型返回不是合法 JSON", kind="invalid_json", detail=type(exc).__name__) from exc

        try:
            content = result["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError("模型返回格式不正确", kind="invalid_shape", detail=type(exc).__name__) from exc
        if not isinstance(content, str) or not content.strip():
            raise LLMError("模型返回了空回复", kind="empty_reply")
        usage = result.get("usage", {})
        if isinstance(usage, dict):
            # 缓存命中字段是判断"提示词前缀是否稳定"的唯一可观测量：
            # 命中率高说明请求前缀可复用，否则每次都在重新计算整个输入。
            # 见 https://api-docs.deepseek.com/guides/kv_cache
            self.last_usage = {key: value for key, value in usage.items()
                               if key in _USAGE_FIELDS
                               and type(value) is int and value >= 0}
            self._record_usage(self.last_usage)
        return content.strip()

    @staticmethod
    def _safe_error_body(exc: urllib.error.HTTPError, *, limit: int = 500) -> str:
        """提取有限长度的错误响应体，失败时返回空串而不是抛出新异常。"""

        try:
            raw = exc.read()
        except Exception:  # noqa: BLE001 - 诊断信息永远不能反过来打断请求流程
            return ""
        if not isinstance(raw, (bytes, bytearray)):
            return ""
        text = raw.decode("utf-8", errors="replace").strip()
        return " ".join(text.split())[:limit]
