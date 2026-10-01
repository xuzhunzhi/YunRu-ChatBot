"""余额命令的离线覆盖。

重点不是"能查到余额"，而是**三件容易出错的事**：
1. 默认 fail-closed —— 没配置时任何会话都查不到，而不是泄漏给所有人；
2. 凭据不落日志、不回显；
3. 接口异常时给分类错误，而不是把原始响应或异常正文发出去。
"""
import asyncio
import io
import json
import urllib.error
from unittest.mock import patch

from qq_roleplay_bot.balance_client import BalanceClient, BalanceError, summarize
from qq_roleplay_bot.builtin_balance_command import BalanceCommand
from qq_roleplay_bot.transport import IncomingMessage, MessageTarget

ADMIN = "900000001"
GROUP = "717151356"
OTHER_GROUP = "999999999"

PAYLOAD = {
    "is_available": True,
    "balance_infos": [{
        "currency": "CNY",
        "total_balance": "42.50",
        "granted_balance": "2.50",
        "topped_up_balance": "40.00",
    }],
}


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _client(**kwargs) -> BalanceClient:
    return BalanceClient("https://example.invalid", "secret-key", cache_seconds=0, **kwargs)


def _fetch(client):
    return asyncio.run(client.fetch())


def msg(text, *, target=None, user_id=ADMIN, message_id="b1"):
    return IncomingMessage(
        message_id=message_id, session_id="x", user_id=user_id, text=text,
        target=target or MessageTarget(user_id=ADMIN), sender_name="tester",
    )


# --- 客户端 -----------------------------------------------------------------


def test_fetch_returns_payload() -> None:
    with patch("urllib.request.urlopen", return_value=_Response(json.dumps(PAYLOAD).encode())):
        assert _fetch(_client()) == PAYLOAD


def test_result_is_cached_within_window() -> None:
    clock = [1000.0]
    client = BalanceClient("https://example.invalid", "k", cache_seconds=60, clock=lambda: clock[0])
    calls = []

    def fake_urlopen(*args, **kwargs):
        calls.append(1)
        return _Response(json.dumps(PAYLOAD).encode())

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        _fetch(client)
        _fetch(client)
        clock[0] += 61
        _fetch(client)
    assert len(calls) == 2, "缓存期内不应重复请求"


def test_missing_credential_is_rejected_before_any_request() -> None:
    with patch("urllib.request.urlopen") as mocked:
        with patch("urllib.request.urlopen", side_effect=AssertionError("不应发起请求")):
            try:
                _fetch(BalanceClient("https://example.invalid", ""))
            except BalanceError as exc:
                assert exc.kind == "no_credential"
            else:
                raise AssertionError("未配置凭据时应抛 BalanceError")
    assert not mocked.called


def test_http_error_becomes_classified_error_without_body() -> None:
    error = urllib.error.HTTPError(
        "https://example.invalid/user/balance", 401, "Unauthorized", {},
        io.BytesIO(b'{"error":"secret-key is invalid"}'))
    with patch("urllib.request.urlopen", side_effect=error):
        try:
            _fetch(_client())
        except BalanceError as exc:
            assert exc.kind == "http_401"
            # 异常文本里不能带上响应体（可能回显凭据）。
            assert "secret-key" not in str(exc)
        else:
            raise AssertionError("HTTP 错误应抛 BalanceError")


def test_transport_error_is_classified() -> None:
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("boom")):
        try:
            _fetch(_client())
        except BalanceError as exc:
            assert exc.kind == "transport_error"
        else:
            raise AssertionError("传输错误应抛 BalanceError")


def test_request_carries_bearer_token_and_no_extra_identity() -> None:
    seen = {}

    def fake_urlopen(request, timeout=None):
        seen["url"] = request.full_url
        seen["auth"] = request.get_header("Authorization")
        seen["method"] = request.get_method()
        return _Response(json.dumps(PAYLOAD).encode())

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        _fetch(_client())
    assert seen["url"].endswith("/user/balance")
    assert seen["auth"] == "Bearer secret-key"
    assert seen["method"] == "GET"


# --- 渲染 -------------------------------------------------------------------


def test_summarize_formats_amounts_and_currency() -> None:
    text = summarize(PAYLOAD)
    assert "CNY 42.50" in text
    assert "赠送 2.50" in text
    assert "充值 40.00" in text


def test_summarize_flags_insufficient_balance() -> None:
    text = summarize({**PAYLOAD, "is_available": False})
    assert "余额不足" in text


def test_summarize_handles_malformed_payloads() -> None:
    assert "没有返回明细" in summarize({})
    assert "没有返回明细" in summarize({"balance_infos": []})
    assert "没有返回明细" in summarize({"balance_infos": "not-a-list"})
    # 金额是字符串；非法值原样展示，不做计算也不崩。
    assert "?" in summarize({"balance_infos": [{"currency": "CNY", "total_balance": None}]})


# --- 会话范围（fail-closed）------------------------------------------------


def test_default_denies_every_session() -> None:
    """两个集合都为空时，任何会话都不允许——配置漏了要查不到，而不是泄漏。"""

    command = BalanceCommand(_client(), allowed_user_ids=(), allowed_group_ids=())
    assert command.session_allowed(MessageTarget(user_id=ADMIN)) is False
    assert command.session_allowed(MessageTarget(group_id=GROUP)) is False


def test_only_configured_private_user_is_allowed() -> None:
    command = BalanceCommand(_client(), allowed_user_ids={ADMIN})
    assert command.session_allowed(MessageTarget(user_id=ADMIN)) is True
    assert command.session_allowed(MessageTarget(user_id="900009999")) is False
    # 配置了私聊不等于群聊也开放。
    assert command.session_allowed(MessageTarget(group_id=GROUP)) is False


def test_group_requires_explicit_opt_in() -> None:
    command = BalanceCommand(_client(), allowed_user_ids={ADMIN}, allowed_group_ids={GROUP})
    assert command.session_allowed(MessageTarget(group_id=GROUP)) is True
    assert command.session_allowed(MessageTarget(group_id=OTHER_GROUP)) is False


def test_matches_chinese_and_latin_aliases() -> None:
    command = BalanceCommand(_client())
    for text in ("/balance", "/余额", "  /BALANCE  ", "yunru balance"):
        assert command.match(msg(text)), text
    for text in ("/balance me", "余额", "/help", ""):
        assert not command.match(msg(text)), text


def test_not_listed_in_public_help() -> None:
    """运维命令不该出现在群里的 /help 里。"""

    assert BalanceCommand(_client()).help_text() == ""


# --- 命令行为 ---------------------------------------------------------------


def test_handle_returns_summary() -> None:
    with patch("urllib.request.urlopen", return_value=_Response(json.dumps(PAYLOAD).encode())):
        command = BalanceCommand(_client(), allowed_user_ids={ADMIN})
        result = asyncio.run(command.handle(msg("/balance")))
    assert result is not None and "CNY 42.50" in result


def test_handle_reports_failure_kind_without_leaking() -> None:
    error = urllib.error.HTTPError(
        "https://example.invalid/user/balance", 403, "Forbidden", {},
        io.BytesIO(b'{"error":"secret-key invalid"}'))
    with patch("urllib.request.urlopen", side_effect=error):
        command = BalanceCommand(_client(), allowed_user_ids={ADMIN})
        result = asyncio.run(command.handle(msg("/balance")))
    assert result is not None
    assert "http_403" in result
    assert "secret-key" not in result


def test_handle_without_client_says_unconfigured() -> None:
    result = asyncio.run(BalanceCommand(None).handle(msg("/balance")))
    assert result is not None and "未配置" in result
