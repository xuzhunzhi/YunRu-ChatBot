"""`mail_client` 失败时**记不记原文**这件事的离线覆盖（2026-10-11 加）。

由来：那天邮箱整条路断了（每 5 分钟 `mail_channel_failed` 一次），日志里只有

    mail_command_failed action_kind=credential_lock_unavailable code=None

CLI 的 `message` 里明明写着原因（"failed to acquire token storage lock…" vs
"timed out waiting for token storage lock…"是两码事），我们却把它丢掉了——
于是同一件事查了一整天。这一份钉住四件事：

1. **原文进日志**：`error.message` 与"哪一条命令"（`action=`）都要看得到；
2. **脱敏**：原文里像凭据的片段被抹掉——日志不是保险箱，CLI 说的话也要过这道闸；
3. **截断**：超长原文只留开头（别把一整段输出倒进日志）；
4. **没有 JSON 也要留痕**：连 `{}` 都解析不出来时，把两段输出的开头记下来。

突变（各自还原）：把 `describe_error(...)` 从 `logger.warning` 里去掉 → 第 1 条红；
不调 `_clip`/`_SECRET` → 第 2 条红；不截断 → 第 3 条红；`payload is None` 那支不记日志
→ 第 4 条红。
"""
from __future__ import annotations

import asyncio
import logging
import pathlib
import subprocess
import tempfile

from qq_roleplay_bot.plugins.mail.mail_client import (
    MAX_ERROR_CHARS,
    MailClient,
    MailError,
)

LOGGER_NAME = "qq_roleplay_bot.plugins.mail.mail_client"


class _Catcher(logging.Handler):
    """把 WARNING 及以上都收下来（比 `assertLogs` 好用：可以一次只看自己关心的那几条）。"""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover - 纯转发
        self.records.append(record)

    def lines(self) -> list[str]:
        return [record.getMessage() for record in self.records]


def _client(result: subprocess.CompletedProcess) -> MailClient:
    """一个用假 runner 的客户端。

    `_executable_path` 直接钉死，免得这条用例依赖"本机装没装 agently-cli"。
    """

    client = MailClient(workdir=tempfile.mkdtemp(prefix="mail-client-errors-"),
                        runner=lambda *a, **kw: result)
    client._executable_path = "agently-cli（替身）"
    return client


def _fail(*, stdout: str, stderr: str = "", returncode: int = 6) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def _run(client: MailClient) -> MailError:
    """跑一次 `message +list`，返回它抛出的 `MailError`（没抛就是 bug）。"""

    try:
        asyncio.run(client.list_messages(limit=1))
    except MailError as exc:
        return exc
    raise AssertionError("CLI 说 ok=false，这里却当成功了")


def _capture(fn) -> tuple[list[str], object]:
    catcher = _Catcher()
    logger = logging.getLogger(LOGGER_NAME)
    logger.addHandler(catcher)
    try:
        value = fn()
    finally:
        logger.removeHandler(catcher)
    return catcher.lines(), value


def _json_error(kind: str, message: str, code: object = None) -> str:
    payload = {"ok": False, "error": {"type": kind, "code": code, "message": message}}
    import json

    return json.dumps(payload, ensure_ascii=False)


# --- 1. 原文与"哪一条命令"都要在日志里 ----------------------------------------


def test_the_cli_message_and_the_action_are_both_logged() -> None:
    message = 'failed to acquire token storage lock for workspace "default"'
    client = _client(_fail(stdout=_json_error("credential_lock_unavailable", message)))

    lines, exc = _capture(lambda: _run(client))

    assert isinstance(exc, MailError) and exc.kind == "credential_lock_unavailable"
    hit = [line for line in lines if line.startswith("mail_command_failed")]
    assert hit, f"一条失败日志都没写：{lines}"
    line = hit[0]
    assert "action=message +list" in line, line
    assert "action_kind=credential_lock_unavailable" in line, line
    assert "token storage lock" in line, f"CLI 的原文没记下来：{line}"


def test_the_workspace_name_in_the_message_is_not_swallowed() -> None:
    """原文要**整句**留下来：worker 与 workspace 这类词正是分辨原因的关键。"""

    client = _client(_fail(stdout=_json_error(
        "credential_lock_unavailable", 'timed out waiting for token storage lock for workspace "default"')))

    lines, _ = _capture(lambda: _run(client))

    assert any('workspace "default"' in line for line in lines), lines


# --- 2. 像凭据的片段要抹掉 ----------------------------------------------------


def test_a_secret_looking_message_is_masked() -> None:
    secret = "sk-abcdefghijklmnopqrstuvwxyz"
    client = _client(_fail(stdout=_json_error(
        "http_error", f"Authentication Fails (auth header format should be Bearer {secret})")))

    lines, _ = _capture(lambda: _run(client))

    text = " ".join(lines)
    assert secret not in text, f"凭据原文进了日志：{text}"
    assert "[已隐去]" in text, text


def test_an_ordinary_message_is_not_mangled() -> None:
    """脱敏别过度：普通原因句要原样留着。"""

    client = _client(_fail(stdout=_json_error("not_found", "message not found")))

    lines, _ = _capture(lambda: _run(client))

    assert any("message not found" in line for line in lines), lines
    assert not any("[已隐去]" in line for line in lines), lines


# --- 3. 超长原文要截断 --------------------------------------------------------


def test_a_very_long_message_is_truncated() -> None:
    long_message = "正文很长的失败原因：" + ("很长" * 4000)
    client = _client(_fail(stdout=_json_error("command_failed", long_message)))

    lines, _ = _capture(lambda: _run(client))

    line = [item for item in lines if item.startswith("mail_command_failed")][0]
    assert "后略" in line, "没截断"
    assert len(line) < MAX_ERROR_CHARS + 200, f"截断没生效，长度 {len(line)}"
    assert line.count("很长") < 200, "整段还是倒进来了"


# --- 4. 连 JSON 都没有时也要留痕 ----------------------------------------------


def test_unparsable_output_still_leaves_a_trace() -> None:
    client = _client(_fail(stdout="boom: something went wrong", returncode=3))

    lines, exc = _capture(lambda: _run(client))

    assert isinstance(exc, MailError) and exc.kind == "unparsable"
    hit = [line for line in lines if line.startswith("mail_command_unparsable")]
    assert hit, f"没有 JSON 时一个字都没记：{lines}"
    assert "boom: something went wrong" in hit[0], hit[0]
    assert "returncode=3" in hit[0], hit[0]


# --- 5. 成功那一轮不许写这些行 ------------------------------------------------


def test_a_successful_call_logs_no_failure_line() -> None:
    ok = _fail(stdout='{"ok": true, "data": {"messages": []}}', returncode=0)
    client = _client(ok)

    lines, value = _capture(lambda: asyncio.run(client.list_messages(limit=1)))

    assert value == {"messages": []}, value
    assert lines == [], f"成功也写了失败日志：{lines}"


def test_the_source_keeps_the_original_cli_message_in_the_log_call() -> None:
    """源码判据：`describe_error(...)` 必须真的被用在 `logger.warning` 那一行里。

    这几条断言看着啰嗦，但它们拦的是"以后有人为了省事又把原文去掉"——
    正是这次要修的那件事。
    """

    source = (pathlib.Path(__file__).resolve().parents[1]
              / "src" / "qq_roleplay_bot" / "plugins" / "mail" / "mail_client.py").read_text("utf-8")
    assert "describe_error(error, returncode=returncode)" in source
    assert "mail_command_failed action=%s action_kind=%s code=%s detail=%s" in source
    assert "mail_command_unparsable action=%s returncode=%s detail=%s" in source
    assert "_SECRET.sub" in source, "脱敏那一步不见了"
