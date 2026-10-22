"""钉住 `llm_client` 的"哪一家"这一维：请求体门控 + 402 那一次保命重试。

这一份要钉住的三件事：

1. `provider` 是 `""`（还不知道是哪一家）或 `"deepseek"` 时，请求体与今天
   **逐字节相同**（含 `user_id` 与 `reasoning_effort`）——新增这个参数本身
   不许改变任何一次现有调用。
2. **`reasoning_effort` 自 2026-10-20 起默认全发**（白名单 → "已知不兼容"黑名单）：
   provider 是别的家（`"mimo"`）时，只去掉 **`user_id`**，`reasoning_effort` 仍在。
   这是任务的**目的**（换 key 源之后 effort 不能静默失效）；真不支持的家由它自己
   报错。采样参数与 `max_tokens` 照旧不分家。
   `QQBOT_<TASK>_SEND_REASONING_EFFORT=0` 可以按用途强制关掉（测试钉住回复那一路
   读的是 `QQBOT_REPLY_*`，不是 `DIALOGUE`）。
3. 主家回 HTTP 402 时：切到兜底那一家，并**用新端点重试同一次调用**；这次重试
   **不占** `max_attempts` 的普通重试配额（整条命只切一次）；没有兜底可切时
   如实失败（`http_error`，**不吞**），也不许在同一家硬重试。

突变说明（把哪一行改坏，这一份就会红）：

- `_send_reasoning_effort` 改回按 provider 白名单 → 第 2 条红；
- 改成恒 `False` → 第 1 条红（`reasoning_effort` 不见了）；
- `user_id` 跟着改成全发 → 第 2 条红（mimo 请求里多出 `user_id`）；
- 按用途开关读错名字（不映射 `dialogue`→`reply`）→ 强制关掉那条红；
- `complete()` 里 402 不再 `attempt -= 1` → 第 3 条红（`last_attempts` 变 2）；
- 402 时不换端点只重试 → 第 3 条红（第二次仍打在 deepseek 地址上）；
- `_is_balance_exhausted` 改成看 body 文本 → 第 3 条红（body 里没有
  "Insufficient Balance" 那类字样时就不切了）。
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import urllib.error
from contextlib import contextmanager
from unittest.mock import patch

from qq_roleplay_bot import dev_config, provider_failover
from qq_roleplay_bot.llm_client import LLMError, OpenAICompatibleClient

REQUEST = [{"role": "user", "content": "hi"}]
OK_BODY = {"choices": [{"message": {"content": "  好  "}}]}

#: 本文件会读到的环境变量（跑完原样还回去）。
ALL_VARS: tuple[str, ...] = (
    "QQBOT_API_KEY", "QQBOT_API_BASE_URL", "QQBOT_API_MODEL", "QQBOT_API_PROVIDER",
    "QQBOT_REPLY_API_KEY", "QQBOT_REPLY_API_BASE_URL", "QQBOT_REPLY_API_MODEL",
    "QQBOT_LETTER_API_KEY", "QQBOT_LETTER_API_BASE_URL", "QQBOT_LETTER_API_MODEL",
    "QQBOT_MAIL_API_KEY", "QQBOT_MAIL_MODEL",
    "QQBOT_JUDGE_API_KEY", "QQBOT_JUDGE_API_BASE_URL", "QQBOT_JUDGE_MODEL",
    "QQBOT_MEMORY_API_KEY", "QQBOT_MEMORY_API_BASE_URL", "QQBOT_MEMORY_MODEL",
    "QQBOT_REVIEW_API_KEY", "QQBOT_REVIEW_API_BASE_URL", "QQBOT_REVIEW_MODEL",
    "QQBOT_VISION_API_KEY", "QQBOT_VISION_API_BASE_URL", "QQBOT_VISION_MODEL",
    "QQBOT_FALLBACK_API_KEY",
    # 按用途的 reasoning_effort 强制开关（2026-10-20）：本机 .env 里设了也不许
    # 影响这几条"默认照发"的断言。
    "QQBOT_REPLY_SEND_REASONING_EFFORT", "QQBOT_JUDGE_SEND_REASONING_EFFORT",
    "QQBOT_LETTER_SEND_REASONING_EFFORT", "QQBOT_MEMORY_SEND_REASONING_EFFORT",
    "QQBOT_REVIEW_SEND_REASONING_EFFORT", "QQBOT_VISION_SEND_REASONING_EFFORT",
)

MAIN_ENV: dict[str, str] = {
    "QQBOT_JUDGE_API_KEY": "sk-fake-judge",
    "QQBOT_LETTER_API_KEY": "sk-fake-letter",
}


@contextmanager
def _env(**values):
    """清空这批变量后按 `values` 设好，跑完原样还回去。

    做法与理由见 `test_provider_failover._env`（`dev_config.API_KEY` 也一起清掉，
    否则"什么都没配"那一档在本机测不出来）。
    """

    saved = {name: os.environ.get(name) for name in ALL_VARS}
    saved_key = dev_config.API_KEY
    for name in ALL_VARS:
        os.environ.pop(name, None)
    dev_config.API_KEY = ""
    for name, value in values.items():
        os.environ[name] = value
    try:
        yield
    finally:
        dev_config.API_KEY = saved_key
        for name in ALL_VARS:
            os.environ.pop(name, None)
        for name, value in saved.items():
            if value is not None:
                os.environ[name] = value


@contextmanager
def _isolated(**values):
    """`_env` + 兜底模式（**进程单例**）的进出场，理由见同名 helper。"""

    provider_failover.reset_for_tests()
    with _env(**values):
        try:
            yield
        finally:
            provider_failover.restore_primary(reason="测试收尾")
            provider_failover.reset_for_tests()


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _client(provider: str) -> OpenAICompatibleClient:
    return OpenAICompatibleClient(
        "https://api.deepseek.com", "sk-fake-judge", "deepseek-flash",
        timeout=5.0, user_id="qqbot-dialogue", provider=provider, usage_role="dialogue")


def _capture_into(seen: list, body: dict):
    """打桩 opener：把每次请求的 `(URL, 原始字节)` 记下来，再回一份正常响应。"""

    def fake_urlopen(request, timeout=None):
        seen.append((request.full_url, request.data))
        return _Response(json.dumps(body).encode())

    return fake_urlopen


def _body_for(provider: str) -> bytes:
    seen: list = []
    client = _client(provider)
    with patch("qq_roleplay_bot.llm_client._urlopen",
               side_effect=_capture_into(seen, OK_BODY)):
        assert asyncio.run(client.complete(REQUEST)) == "好"
    assert len(seen) == 1
    return seen[0][1]


# --- 1 & 2. 请求体门控 --------------------------------------------------------


def test_an_unset_provider_sends_exactly_what_deepseek_sends() -> None:
    with _env():
        assert _body_for("") == _body_for("deepseek"), "provider 默认值必须与 deepseek 逐字节相同"
        payload = json.loads(_body_for("").decode("utf-8"))
    assert payload["user_id"] == "qqbot-dialogue"
    assert payload["reasoning_effort"], "默认这一档仍然要带 reasoning_effort"


def test_a_non_deepseek_provider_keeps_reasoning_effort_but_not_user_id() -> None:
    """2026-10-20：`reasoning_effort` 默认全发（只有 `user_id` 仍按家门控）。

    这是本次改动的**目的**（mimo 换 key 源之后 effort 不能静默失效）；mimo 若真不
    支持就由它自己报错。`user_id` 是 DeepSeek 的 KV 隔离字段，不是这次的范围。
    """

    with _env():
        plain = json.loads(_body_for("").decode("utf-8"))
        other = json.loads(_body_for("mimo").decode("utf-8"))
    assert "reasoning_effort" in plain and "user_id" in plain
    assert "reasoning_effort" in other, "mimo 也必须拿到 reasoning_effort（默认全发）"
    assert "user_id" not in other
    # 除 `user_id` 之外**逐键相同**：模型名、消息、采样参数、max_tokens 都不许分家
    assert set(other) == set(plain) - {"user_id"}
    assert other == {k: v for k, v in plain.items() if k != "user_id"}


def test_a_task_switch_can_turn_reasoning_effort_off() -> None:
    """`QQBOT_<TASK>_SEND_REASONING_EFFORT=0` 按用途强制关掉。

    client 的 `usage_role` 是 `dialogue`，配置名是 `QQBOT_REPLY_*`——读错名字
    （读到 `QQBOT_DIALOGUE_*`）这条就红。
    """

    with _env(QQBOT_REPLY_SEND_REASONING_EFFORT="0"):
        body = json.loads(_body_for("deepseek").decode("utf-8"))
    assert "reasoning_effort" not in body
    assert "user_id" in body, "关掉 effort 不该连 user_id 一起关"

    with _env(QQBOT_REPLY_SEND_REASONING_EFFORT="1"):
        body = json.loads(_body_for("deepseek").decode("utf-8"))
    assert "reasoning_effort" in body


def test_the_switch_forces_reasoning_effort_on_even_for_a_blocklisted_provider() -> None:
    """`=1` 的语义是"强制发"：即使某一家被拉进黑名单也一样。

    默认黑名单是空的；这里临时塞一个，钉住"强制开关优先于黑名单"。
    """

    from qq_roleplay_bot import llm_client

    with patch.object(llm_client, "REASONING_EFFORT_INCOMPATIBLE_PROVIDERS", frozenset({"mimo"})):
        with _env():
            body = json.loads(_body_for("mimo").decode("utf-8"))
            assert "reasoning_effort" not in body
        with _env(QQBOT_REPLY_SEND_REASONING_EFFORT="1"):
            body = json.loads(_body_for("mimo").decode("utf-8"))
            assert "reasoning_effort" in body


# --- 3. 402 = 保命重试 --------------------------------------------------------


def test_a_402_switches_the_provider_and_retries_the_very_call() -> None:
    """2026-10-16：驱动这一条的用途回到 `judge`（用户原话："切换回ds。gemini好蠢。"）。

    判定自 2026-10-14 起一度借 `reply` 那条 key（xkiro）——那段它**不属于 deepseek
    那一家**，拿 `usage_role="judge"` 造"deepseek 报 402"这个场景不成立（闸门正确地
    认定"deepseek 不是这一路的主家"，于是不换端点），所以当时这条改用 `letter`。
    2026-10-16 判定回到 deepseek（`TASKS["judge"]` 又用自己那条 key），驱动随之回到
    判定本人——不是为了让用例变绿，是因为这个场景重新成立。
    """

    with _isolated(**MAIN_ENV):
        client = OpenAICompatibleClient(
            "https://api.deepseek.com", "sk-fake-judge", "deepseek-flash",
            timeout=5.0, provider="deepseek", usage_role="judge")
        calls: list[str] = []

        def fake_urlopen(request, timeout=None):
            calls.append(request.full_url)
            if "api.deepseek.com" in request.full_url:
                raise urllib.error.HTTPError(
                    request.full_url, 402, "Payment Required", {},
                    io.BytesIO(b'{"error":"Insufficient Balance"}'))
            return _Response(json.dumps(OK_BODY).encode())

        with patch("qq_roleplay_bot.llm_client._urlopen", side_effect=fake_urlopen):
            assert asyncio.run(client.complete(REQUEST)) == "好"

        assert calls == ["https://api.deepseek.com/chat/completions",
                         "https://api.xiaomimimo.com/v1/chat/completions"]
        assert (client.base_url, client.model, client.provider) == (
            "https://api.xiaomimimo.com/v1", "mimo-v2.6-pro", "mimo")
        # 这次保命重试**不占**普通重试配额：`attempt` 还是 1（不是 2）
        assert client.last_attempts == 1
        assert provider_failover.in_fallback() is True


def test_a_402_without_a_fallback_endpoint_still_fails_as_before() -> None:
    with _isolated(QQBOT_JUDGE_API_KEY="sk-fake-judge"):
        client = OpenAICompatibleClient(
            "https://api.deepseek.com", "sk-fake-judge", "deepseek-flash",
            timeout=5.0, max_attempts=1, backoff_seconds=0.0,
            provider="deepseek", usage_role="judge")
        calls: list[str] = []

        def fake_urlopen(request, timeout=None):
            calls.append(request.full_url)
            raise urllib.error.HTTPError(
                request.full_url, 402, "Payment Required", {},
                io.BytesIO(b'{"error":"Insufficient Balance"}'))

        with patch("qq_roleplay_bot.llm_client._urlopen", side_effect=fake_urlopen):
            try:
                asyncio.run(client.complete(REQUEST))
            except LLMError as exc:
                assert exc.kind == "http_error"
                assert "402" in exc.detail
            else:
                raise AssertionError("没有兜底可切时必须如实失败（fail-closed）")

        # 不许拿同一家硬重试：402 不是可重试状态
        assert calls == ["https://api.deepseek.com/chat/completions"]
        assert provider_failover.in_fallback() is False
