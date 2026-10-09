"""llm_client 的离线覆盖：只验证错误分类与脱敏，不发起真实网络请求。"""
import asyncio
import io
import json
import os
import socket
import urllib.error
from unittest.mock import patch

from qq_roleplay_bot.llm_client import LLMError, OpenAICompatibleClient

REQUEST = [{"role": "user", "content": "hi"}]


def _client() -> OpenAICompatibleClient:
    return OpenAICompatibleClient("https://example.invalid/v1", "secret-key", "test-model", timeout=1.0)


def _complete(client=None):
    return asyncio.run((client or _client()).complete(REQUEST))


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_success_returns_content_and_usage() -> None:
    body = {"choices": [{"message": {"content": "  你好  "}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "ignored": 9}}
    with patch("qq_roleplay_bot.llm_client._urlopen", return_value=_Response(json.dumps(body).encode())):
        client = _client()
        assert _complete(client) == "你好"
    assert client.last_usage == {"prompt_tokens": 3, "completion_tokens": 2}


def test_cache_usage_fields_are_kept_and_accumulated() -> None:
    """缓存计量必须保留并累计：这是判断提示词前缀是否稳定的唯一读数。"""

    body = {"choices": [{"message": {"content": "好"}}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 5,
                      "prompt_cache_hit_tokens": 900, "prompt_cache_miss_tokens": 100}}
    client = _client()
    encoded = json.dumps(body).encode()
    with patch("qq_roleplay_bot.llm_client._urlopen", side_effect=lambda *a, **k: _Response(encoded)):
        _complete(client)
        _complete(client)
    assert client.last_usage["prompt_cache_hit_tokens"] == 900
    stats = client.cache_stats()
    assert stats["calls"] == 2
    assert stats["hit_tokens"] == 1800
    assert stats["miss_tokens"] == 200
    assert stats["hit_rate"] == 0.9


def test_cache_stats_is_zero_without_cache_fields() -> None:
    """厂商不返回缓存字段时必须是 0 而不是报错。"""

    body = {"choices": [{"message": {"content": "好"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 1}}
    client = _client()
    assert client.cache_stats()["calls"] == 0
    with patch("qq_roleplay_bot.llm_client._urlopen", return_value=_Response(json.dumps(body).encode())):
        _complete(client)
    stats = client.cache_stats()
    assert stats["calls"] == 1
    assert stats["hit_rate"] == 0.0


def test_http_error_keeps_status_without_leaking_credentials() -> None:
    error = urllib.error.HTTPError(
        "https://example.invalid/v1/chat/completions", 429, "Too Many Requests",
        {}, io.BytesIO(b'{"error":"rate limited"}'))
    client = OpenAICompatibleClient("https://example.invalid/v1", "secret-key", "test-model",
                                   timeout=1.0, max_attempts=1, backoff_seconds=0.0)
    with patch("qq_roleplay_bot.llm_client._urlopen", side_effect=error):
        try:
            _complete(client)
        except LLMError as exc:
            assert exc.kind == "http_error"
            assert "429" in exc.detail
            assert "rate limited" in exc.detail
            assert "secret-key" not in exc.safe_summary()
        else:
            raise AssertionError("HTTPError 应当被转换为 LLMError")
    assert client.last_attempts == 1


def test_http_error_body_survives_retries() -> None:
    """同一个 HTTPError 的响应流只能读一次；重试不能让 body 变成空串。"""

    attempts = {"n": 0}

    def fake_urlopen(request, timeout=None):
        attempts["n"] += 1
        raise urllib.error.HTTPError(
            "https://example.invalid/v1/chat/completions", 429, "Too Many Requests",
            {}, io.BytesIO(b'{"error":"slow down"}'))

    client = OpenAICompatibleClient("https://example.invalid/v1", "k", "m",
                                   timeout=1.0, max_attempts=3, backoff_seconds=0.0)
    with patch("qq_roleplay_bot.llm_client._urlopen", side_effect=fake_urlopen):
        try:
            _complete(client)
        except LLMError as exc:
            assert exc.kind == "http_error"
            assert "slow down" in exc.detail, exc.detail
        else:
            raise AssertionError("重试耗尽后应当抛出 LLMError")
    assert attempts["n"] == 3


def test_client_error_is_not_retried() -> None:
    attempts = {"n": 0}

    def fake_urlopen(request, timeout=None):
        attempts["n"] += 1
        raise urllib.error.HTTPError(
            "https://example.invalid/v1/chat/completions", 400, "Bad Request",
            {}, io.BytesIO(b'{"error":"bad request"}'))

    client = OpenAICompatibleClient("https://example.invalid/v1", "k", "m",
                                   timeout=1.0, max_attempts=3, backoff_seconds=0.0)
    with patch("qq_roleplay_bot.llm_client._urlopen", side_effect=fake_urlopen):
        try:
            _complete(client)
        except LLMError as exc:
            assert exc.kind == "http_error"
        else:
            raise AssertionError("4xx 应当抛出 LLMError")
    # 4xx 是请求本身的问题，重试没有意义。
    assert attempts["n"] == 1


def test_transport_error_is_retried_until_it_succeeds() -> None:
    attempts = {"n": 0}

    def fake_urlopen(request, timeout=None):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise ConnectionResetError("reset")
        return _Response(json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode())

    client = OpenAICompatibleClient("https://example.invalid/v1", "k", "m",
                                   timeout=1.0, max_attempts=3, backoff_seconds=0.0)
    with patch("qq_roleplay_bot.llm_client._urlopen", side_effect=fake_urlopen):
        assert _complete(client) == "ok"
    assert client.last_attempts == 2


def test_invalid_reply_shape_is_not_retried() -> None:
    attempts = {"n": 0}

    def fake_urlopen(request, timeout=None):
        attempts["n"] += 1
        return _Response(json.dumps({"unexpected": True}).encode())

    client = OpenAICompatibleClient("https://example.invalid/v1", "k", "m",
                                   timeout=1.0, max_attempts=3, backoff_seconds=0.0)
    with patch("qq_roleplay_bot.llm_client._urlopen", side_effect=fake_urlopen):
        try:
            _complete(client)
        except LLMError as exc:
            assert exc.kind == "invalid_shape"
        else:
            raise AssertionError("格式错误应当抛出 LLMError")
    # 同样的请求不会因为重试而变好。
    assert attempts["n"] == 1


def test_connection_reset_is_classified_as_transport_error() -> None:
    """ConnectionResetError 是 OSError，不会进入 URLError 分支。"""

    with patch("qq_roleplay_bot.llm_client._urlopen", side_effect=ConnectionResetError("reset")):
        try:
            _complete()
        except LLMError as exc:
            assert exc.kind == "transport_error"
            assert exc.detail == "ConnectionResetError"
        else:
            raise AssertionError("OSError 应当被转换为 LLMError")


def test_socket_timeout_is_classified_as_transport_error() -> None:
    with patch("qq_roleplay_bot.llm_client._urlopen", side_effect=socket.timeout("slow")):
        try:
            _complete()
        except LLMError as exc:
            assert exc.kind == "transport_error"
        else:
            raise AssertionError("socket.timeout 应当被转换为 LLMError")


def test_undecodable_body_is_classified_as_invalid_json() -> None:
    with patch("qq_roleplay_bot.llm_client._urlopen", return_value=_Response(b"\xff\xfe not utf8")):
        try:
            _complete()
        except LLMError as exc:
            assert exc.kind == "invalid_json"
            assert exc.detail == "UnicodeDecodeError"
        else:
            raise AssertionError("无法解码的响应体应当被转换为 LLMError")


def test_empty_and_malformed_replies_are_classified() -> None:
    for body, kind in (
        ({"choices": [{"message": {"content": "   "}}]}, "empty_reply"),
        ({"choices": []}, "invalid_shape"),
        ({"unexpected": True}, "invalid_shape"),
    ):
        with patch("qq_roleplay_bot.llm_client._urlopen", return_value=_Response(json.dumps(body).encode())):
            try:
                _complete()
            except LLMError as exc:
                assert exc.kind == kind, (body, exc.kind)
            else:
                raise AssertionError(f"{body} 应当被拒绝")


def test_max_tokens_is_only_sent_when_configured() -> None:
    captured: list[bytes] = []

    def fake_urlopen(request, timeout=None):
        captured.append(request.data)
        return _Response(json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode())

    with patch("qq_roleplay_bot.llm_client._urlopen", side_effect=fake_urlopen):
        _complete(OpenAICompatibleClient("https://example.invalid/v1", "k", "m"))
        _complete(OpenAICompatibleClient("https://example.invalid/v1", "k", "m", max_tokens=256))

    assert "max_tokens" not in json.loads(captured[0])
    assert json.loads(captured[1])["max_tokens"] == 256


def test_request_is_non_streaming_and_bearer_scoped() -> None:
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["data"] = json.loads(request.data)
        captured["auth"] = request.get_header("Authorization")
        return _Response(json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode())

    with patch("qq_roleplay_bot.llm_client._urlopen", side_effect=fake_urlopen):
        _complete()

    assert captured["data"]["stream"] is False
    assert captured["auth"] == "Bearer secret-key"


def test_transport_error_detail_keeps_the_inner_reason() -> None:
    """网络失败要记下**内层原因**，不能只写一个 "URLError"。

    由来（2026-09-29 开机自启那次）：日志里只有 `detail=URLError`，等于什么都没说——
    真因是"进程第一次调用时把系统代理读死了，之后一直走死代理"，查了很久。
    """

    from qq_roleplay_bot.llm_client import _transport_detail

    detail = _transport_detail(urllib.error.URLError(socket.gaierror(11001, "getaddrinfo failed")))
    assert "getaddrinfo" in detail and "URLError" in detail
    refused = _transport_detail(urllib.error.URLError(ConnectionRefusedError(10061, "拒绝连接")))
    assert "10061" in refused
    # 超长 reason 要截断、且不含换行（日志是按行读的）
    long_detail = _transport_detail(urllib.error.URLError("x" * 500))
    assert len(long_detail) <= 160 and "\n" not in long_detail


def test_client_rebuilds_the_opener_on_every_request() -> None:
    """每次调用都现建 opener：模块级全局 opener 会把**当时的**代理设置读死。

    踩过：开机自启瞬间系统代理指向尚未启动的本机代理，那个进程之后每一次模型调用
    都走死代理（一整天 URLError），而新起的进程完全正常。
    """

    import urllib.request

    built = {"count": 0}

    class _Opener:
        def open(self, request, timeout=None):  # noqa: ARG002
            raise urllib.error.URLError("boom")

    real_build = urllib.request.build_opener

    def fake_build(*handlers):  # noqa: ARG001
        built["count"] += 1
        return _Opener()

    urllib.request.build_opener = fake_build
    try:
        client = OpenAICompatibleClient("http://127.0.0.1:9", "k", "m", timeout=0.1)
        for _ in range(3):
            try:
                client._complete_sync([{"role": "user", "content": "hi"}])
            except Exception:  # noqa: BLE001 - 只关心 opener 重建了几次
                pass
    finally:
        urllib.request.build_opener = real_build
    assert built["count"] == 3


# --- 采样参数：回复默认带三样，其余用途配了才发 ---------------------------------

_SAMPLING_KEYS = ("temperature", "top_p", "frequency_penalty")
#: 六个用途 × 三个参数的全部变量名：测试先把这些从环境里清掉再断言默认行为，
#: 免得本机 `.env` 里恰好配过哪个，把"默认不发"验成"发了"。
_SAMPLING_ENV_NAMES = tuple(
    f"QQBOT_{task.upper()}_{key.upper()}"
    for task in ("reply", "judge", "memory", "review", "letter", "vision")
    for key in _SAMPLING_KEYS
)


def _clear_sampling_env() -> None:
    for name in _SAMPLING_ENV_NAMES:
        os.environ.pop(name, None)


def _role_client(usage_role: str) -> OpenAICompatibleClient:
    return OpenAICompatibleClient("https://example.invalid/v1", "k", "m",
                                  timeout=1.0, usage_role=usage_role)


def _captured_body(client: OpenAICompatibleClient) -> dict:
    captured: dict = {}

    def fake_urlopen(request, timeout=None):
        captured["data"] = json.loads(request.data)
        return _Response(json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode())

    with patch("qq_roleplay_bot.llm_client._urlopen", side_effect=fake_urlopen):
        asyncio.run(client.complete(REQUEST))
    return captured["data"]


def test_reply_sends_the_three_default_sampling_params() -> None:
    """回复那一档默认就发三样（写死在代码里，不开 .env 也生效）。"""

    with patch.dict(os.environ):
        _clear_sampling_env()
        body = _captured_body(_role_client("dialogue"))
    assert body["temperature"] == 0.85
    assert body["top_p"] == 0.9
    assert body["frequency_penalty"] == 0.3


def test_usages_other_than_reply_send_no_sampling_keys_by_default() -> None:
    """判定 / 记忆 / 审核 / 写信 / 识图默认一个采样键都不发；不带 usage_role 的
    client（历史路径）同样不发——没配置的用途保持从前的请求体。"""

    with patch.dict(os.environ):
        _clear_sampling_env()
        assert not set(_captured_body(_client())) & set(_SAMPLING_KEYS)
        for role in ("judge", "memory", "review", "letter", "vision"):
            body = _captured_body(_role_client(role))
            assert not set(body) & set(_SAMPLING_KEYS), role


def test_env_overrides_are_read_per_request_on_the_same_client() -> None:
    """环境变量覆盖要**每次请求现读**：改了环境，同一个 client 下一次就跟着变
    （读了常量/构造期快照的实现在这条上会红）。"""

    client = _role_client("dialogue")
    with patch.dict(os.environ):
        _clear_sampling_env()
        assert _captured_body(client)["temperature"] == 0.85
        os.environ["QQBOT_REPLY_TEMPERATURE"] = "1.3"
        os.environ["QQBOT_REPLY_TOP_P"] = "0.5"
        os.environ["QQBOT_REPLY_FREQUENCY_PENALTY"] = "0.0"
        body = _captured_body(client)
    assert body["temperature"] == 1.3
    assert body["top_p"] == 0.5
    assert body["frequency_penalty"] == 0.0


def test_other_usages_send_sampling_keys_only_when_configured() -> None:
    """其余用途配了哪个键才发哪个键（judge 配 temperature → 只多这一个键）。"""

    client = _role_client("judge")
    with patch.dict(os.environ):
        _clear_sampling_env()
        assert not set(_captured_body(client)) & set(_SAMPLING_KEYS)
        os.environ["QQBOT_JUDGE_TEMPERATURE"] = "0.2"
        body = _captured_body(client)
    assert body["temperature"] == 0.2
    assert "top_p" not in body
    assert "frequency_penalty" not in body


def test_bad_sampling_values_fall_back_and_out_of_range_clamp() -> None:
    with patch.dict(os.environ):
        _clear_sampling_env()
        # 回复：坏值回落默认，越界夹进来。
        os.environ["QQBOT_REPLY_TEMPERATURE"] = "不是数"
        os.environ["QQBOT_REPLY_TOP_P"] = "9"
        body = _captured_body(_role_client("dialogue"))
        assert body["temperature"] == 0.85
        assert body["top_p"] == 1.0
        # 其余用途：坏值当没配（那个键不发），越界夹进来。
        os.environ["QQBOT_JUDGE_TEMPERATURE"] = "不是数"
        os.environ["QQBOT_JUDGE_FREQUENCY_PENALTY"] = "-9"
        body = _captured_body(_role_client("judge"))
        assert "temperature" not in body
        assert body["frequency_penalty"] == -2.0
