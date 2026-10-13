"""llm_client 的离线覆盖：只验证错误分类与脱敏，不发起真实网络请求。"""
import asyncio
import io
import json
import os
import socket
import time
import urllib.error
from unittest.mock import patch

from qq_roleplay_bot.llm_client import LLMError, OpenAICompatibleClient, STREAM_STALL_SECONDS

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


def test_http_error_body_survives_the_error_path() -> None:
    """同一个 HTTPError 的响应流只能读一次；body 必须留在 detail 里（排障唯一的线索）。"""

    attempts = {"n": 0}

    def fake_urlopen(request, timeout=None):
        attempts["n"] += 1
        raise urllib.error.HTTPError(
            "https://example.invalid/v1/chat/completions", 429, "Too Many Requests",
            {}, io.BytesIO(b'{"error":"slow down"}'))

    client = OpenAICompatibleClient("https://example.invalid/v1", "k", "m",
                                   timeout=1.0, backoff_seconds=0.0)
    with patch("qq_roleplay_bot.llm_client._urlopen", side_effect=fake_urlopen):
        try:
            _complete(client)
        except LLMError as exc:
            assert exc.kind == "http_error"
            assert "slow down" in exc.detail, exc.detail
        else:
            raise AssertionError("HTTP 失败应当抛出 LLMError")
    # 默认只跑一轮（2026-10-13 用户拍板："不要重试，重试还不如干等着"）。
    assert attempts["n"] == 1
    assert client.last_attempts == 1


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


def test_transport_error_is_not_retried() -> None:
    """默认只跑一轮（2026-10-13 用户拍板："不要重试，重试还不如干等着，所有 agent 都一样"）。

    理由不是"重试没用"，而是**重试的等待比放掉这一轮更贵**：当天实测有 3 条判定是
    "23s 长生成失败 + 又重试一轮" ⇒ 50.4 / 49.2 / 54.4s，而它换来的只是再赌一次。
    """

    attempts = {"n": 0}

    def fake_urlopen(request, timeout=None):
        attempts["n"] += 1
        raise ConnectionResetError("reset")

    client = OpenAICompatibleClient("https://example.invalid/v1", "k", "m",
                                    timeout=1.0, backoff_seconds=0.0)
    with patch("qq_roleplay_bot.llm_client._urlopen", side_effect=fake_urlopen):
        try:
            _complete(client)
        except LLMError as exc:
            assert exc.kind == "transport_error"
        else:
            raise AssertionError("传输层失败应当抛出 LLMError")
    assert attempts["n"] == 1
    assert client.last_attempts == 1


def test_the_default_is_a_single_attempt() -> None:
    """机制还在（显式传 `max_attempts` 才会重试），但**默认是 1**——生产没有人传过 >1。"""

    from qq_roleplay_bot.llm_client import DEFAULT_MAX_ATTEMPTS

    assert DEFAULT_MAX_ATTEMPTS == 1
    assert _client().max_attempts == 1


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


def test_request_is_streaming_and_bearer_scoped() -> None:
    """请求体里的 `stream` 是 True（2026-10-13 改的，见 llm_client 顶部那条常量）。

    这条以前叫 `..._non_streaming_...`：那时闸门盖的是"整段生成"，5 秒砍一次、
    砍完重来，日志里那 16~18 秒就是这么来的。
    """

    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["data"] = json.loads(request.data)
        captured["auth"] = request.get_header("Authorization")
        captured["accept"] = request.get_header("Accept")
        captured["timeout"] = timeout
        return _Response(_sse({"choices": [{"delta": {"content": "ok"}}]}))

    with patch("qq_roleplay_bot.llm_client._urlopen", side_effect=fake_urlopen):
        _complete()

    assert captured["data"]["stream"] is True
    assert captured["auth"] == "Bearer secret-key"
    assert captured["accept"] == "text/event-stream"
    # 每一次读等的上限是那道"没声音"的闸（这里 client 的 timeout=1.0 更小，取小的）。
    assert captured["timeout"] == min(STREAM_STALL_SECONDS, 1.0)


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


def test_reply_sends_the_default_sampling_params() -> None:
    """回复那一档默认就发两样（temperature + top_p，写死在代码里，不开 .env 也生效）
    外加 reasoning_effort=low（2026-10-10 用户拍板）。frequency_penalty 已被
    DeepSeek API 废弃（发了不生效），已删。"""

    with patch.dict(os.environ):
        _clear_sampling_env()
        body = _captured_body(_role_client("dialogue"))
    assert body["temperature"] == 0.85
    assert body["top_p"] == 0.9
    assert body["reasoning_effort"] == "low"
    assert "frequency_penalty" not in body


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


# --- 流式：输出按块读，"3 秒没声音就砍掉本轮" ---------------------------------


def _sse(*chunks) -> bytes:
    """拼一份和真实服务端一样的 SSE 响应体（每条 `data:` 一行，末尾 `[DONE]`）。"""

    lines = [f"data: {json.dumps(chunk)}" for chunk in chunks]
    lines.append("data: [DONE]")
    return ("\n\n".join(lines) + "\n\n").encode()


class _StallingResponse:
    """先吐几行、然后"没声音了"：读空之后每次 `readline()` 都抛 `socket.timeout`。"""

    def __init__(self, lines, *, then_raise=None):
        self._lines = list(lines)
        self._exc = then_raise if then_raise is not None else socket.timeout("silent")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def readline(self):
        if self._lines:
            return self._lines.pop(0)
        raise self._exc


class _PausingResponse:
    """吐一块 → 静几拍（`readline()` 抛 `socket.timeout`）→ 再吐下一块。

    "静几拍"是**瞬间**发生的（不真睡），用来测"首字来了之后还接着等"这条语义：
    只要没超过停顿网，读超时只是心跳，不是失败。
    """

    def __init__(self, chunks, *, quiet_reads: int = 3):
        self._chunks = list(chunks)
        self._quiet = quiet_reads

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def readline(self):
        if self._chunks:
            return self._chunks.pop(0)
        if self._quiet > 0:
            self._quiet -= 1
            raise socket.timeout("silent")
        return b""


def test_streaming_assembles_content_and_usage_from_the_chunks() -> None:
    """思考与正文都在 `delta` 里一块块来：正文拼起来，usage 取最后带它的那一块。"""

    body = _sse(
        {"choices": [{"delta": {"reasoning_content": "先想"}}]},
        {"choices": [{"delta": {"reasoning_content": "一下"}}]},
        {"choices": [{"delta": {"content": "你"}}]},
        {"choices": [{"delta": {"content": "好"}}]},
        {"choices": [], "usage": {"prompt_tokens": 7, "completion_tokens": 3,
                                  "prompt_cache_hit_tokens": 5, "prompt_cache_miss_tokens": 2}},
    )
    client = _client()
    with patch("qq_roleplay_bot.llm_client._urlopen", return_value=_Response(body)):
        assert _complete(client) == "你好"
    assert client.last_usage == {"prompt_tokens": 7, "completion_tokens": 3,
                                 "prompt_cache_hit_tokens": 5, "prompt_cache_miss_tokens": 2}


def test_a_stream_that_dies_half_way_is_not_retried() -> None:
    """说过话之后再也没声音、且超过停顿网 → `stream_stalled`，**不重发**。

    这里的停顿网只有 0.05s（跑得快），所以"静"是真的会撞网——语义与生产一致：
    首字来了照旧读完，但连接真死了就别耗着。
    """

    attempts = {"n": 0}

    def fake_urlopen(request, timeout=None):  # noqa: ARG001
        attempts["n"] += 1
        line = f"data: {json.dumps({'choices': [{'delta': {'reasoning_content': '想'}}]})}\n"
        return _StallingResponse([line.encode()])

    client = OpenAICompatibleClient("https://example.invalid/v1", "k", "m",
                                    timeout=0.05, backoff_seconds=0.0)
    with patch("qq_roleplay_bot.llm_client._urlopen", side_effect=fake_urlopen):
        try:
            _complete(client)
        except LLMError as exc:
            assert exc.kind == "stream_stalled"
        else:  # pragma: no cover - 走到这里说明闸没生效
            raise AssertionError("说到一半断了却没有失败")
    assert attempts["n"] == 1
    assert client.last_error_kind == "stream_stalled"


def test_a_mid_stream_pause_keeps_reading() -> None:
    """首字来了之后**不按总时长砍**：中间静几拍照旧等下去（"超时只管首字"）。"""

    first = f"data: {json.dumps({'choices': [{'delta': {'content': '前'}}]})}\n".encode()
    second = f"data: {json.dumps({'choices': [{'delta': {'content': '后'}}]})}\n".encode()

    client = OpenAICompatibleClient("https://example.invalid/v1", "k", "m",
                                    timeout=1.0, backoff_seconds=0.0)
    with patch("qq_roleplay_bot.llm_client._urlopen",
               return_value=_PausingResponse([first, second, b"data: [DONE]\n"], quiet_reads=3)):
        assert _complete(client) == "前后"


def test_a_stream_without_an_end_marker_is_a_half_sentence() -> None:
    """SSE **干净收摊却没有结束标记** ⇒ 半截，不许当成完整回复发出去（2026-10-13 补）。

    这是改流式时新开的无声截断面：`readline()` 返回空（EOF，不是超时）时，
    以前会把已经拼到的半句话交出去；判定的结构化输出多半解析失败（fail-closed），
    但回复那一路就是**把半句发出去**。
    """

    body = (f"data: {json.dumps({'choices': [{'delta': {'content': '半句话'}}]})}\n\n").encode()
    client = _client()
    with patch("qq_roleplay_bot.llm_client._urlopen", return_value=_Response(body)):
        try:
            _complete(client)
        except LLMError as exc:
            assert exc.kind == "stream_stalled", exc.kind
            assert "结束标记" in exc.detail, exc.detail
        else:  # pragma: no cover - 走到这里说明半句被当成完整回复了
            raise AssertionError("没有结束标记却当成完整回复了")


def test_a_flowing_stream_is_not_cut_by_a_total_cap() -> None:
    """一直在吐字节就不砍：总耗时超过 client 的 `timeout` 也照旧读完（2026-10-13 拍板）。"""

    class _Flowing:
        def __init__(self):
            self.n = 0

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def readline(self):
            self.n += 1
            if self.n <= 6:
                time.sleep(0.03)  # 6 块 × 0.03s = 0.18s > timeout(0.05s)
                return f"data: {json.dumps({'choices': [{'delta': {'content': '字'}}]})}\n".encode()
            if self.n == 7:
                # 结束标记只给 `finish_reason`（不给 `[DONE]`）：两样认一样。
                return (f"data: {json.dumps({'choices': [{'delta': {}, 'finish_reason': 'stop'}]})}\n"
                        ).encode()
            return b""

    client = OpenAICompatibleClient("https://example.invalid/v1", "k", "m",
                                    timeout=0.05, backoff_seconds=0.0)
    with patch("qq_roleplay_bot.llm_client._urlopen", return_value=_Flowing()):
        assert _complete(client) == "字" * 6


def test_a_stream_with_no_output_at_all_is_not_retried() -> None:
    """一个字节都没来 ⇒ `transport_error`（首字超时），**也不再重试**：这一轮直接放掉。"""

    attempts = {"n": 0}

    def fake_urlopen(request, timeout=None):  # noqa: ARG001
        attempts["n"] += 1
        return _StallingResponse([])

    client = OpenAICompatibleClient("https://example.invalid/v1", "k", "m",
                                    timeout=1.0, backoff_seconds=0.0)
    with patch("qq_roleplay_bot.llm_client._urlopen", side_effect=fake_urlopen):
        try:
            _complete(client)
        except LLMError as exc:
            assert exc.kind == "transport_error"
        else:  # pragma: no cover
            raise AssertionError("首字没来却没有失败")
    assert attempts["n"] == 1
    assert client.last_error_kind == "transport_error"


def test_a_stream_that_only_thinks_is_an_empty_reply() -> None:
    """只有思考、正文一个字没有 → 仍按空回复处理（细节里带上思考长度便于排查）。"""

    body = _sse({"choices": [{"delta": {"reasoning_content": "光想不说"}}]})
    client = _client()
    with patch("qq_roleplay_bot.llm_client._urlopen", return_value=_Response(body)):
        try:
            _complete(client)
        except LLMError as exc:
            assert exc.kind == "empty_reply"
            assert exc.detail == "think_chars=4"
        else:  # pragma: no cover - 走到这里说明空回复没被拦住
            raise AssertionError("只有思考却当成了正常回复")


def test_a_whole_json_body_is_still_accepted() -> None:
    """不是 SSE 的一整份 JSON（打桩 / 别的家）仍走老路解析，不是流式专属。"""

    body = json.dumps({"choices": [{"message": {"content": "老形状"}}],
                       "usage": {"prompt_tokens": 1, "completion_tokens": 1}}).encode()
    client = _client()
    with patch("qq_roleplay_bot.llm_client._urlopen", return_value=_Response(body)):
        assert _complete(client) == "老形状"
    assert client.last_usage == {"prompt_tokens": 1, "completion_tokens": 1}
