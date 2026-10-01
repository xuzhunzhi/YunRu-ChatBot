"""邮箱封装层：argv 形状、正文不进 argv、失败分类、凭据不外泄。

全部离线：`MailClient` 的 `runner` 可注入，测试里不起真进程、不发真邮件。
真实行为是 2026-09-27 用 v1.0.18 观测到的（成功 `{"ok":true,"data":{…}}` 后面还跟一行
`tip: …`；失败是 `{"ok":false,"error":{"type","code"}}` + 退出码非 0；`--dry-run` 是另一种形状）。
"""
import asyncio
import json
import subprocess
import tempfile
from pathlib import Path

from qq_roleplay_bot.mail_client import (
    MAX_BODY_CHARS,
    MailClient,
    MailError,
    clean_body,
    clean_email,
    clean_subject,
    summarize_result,
    truncate_output,
)

RECIPIENT = "xuzhunzhi@foxmail.com"


class FakeCompleted:
    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class FakeRunner:
    """记下每次 argv，按脚本返回；用来断言"到底发出了什么命令"。"""

    def __init__(self, stdout: str = "", returncode: int = 0, error: Exception | None = None) -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.error = error
        self.calls: list[dict] = []

    def __call__(self, argv, **kwargs):
        self.calls.append({"argv": list(argv), **kwargs})
        if self.error is not None:
            raise self.error
        return FakeCompleted(self.stdout, returncode=self.returncode)


def ok_output(data: dict, tip: str = "\ntip: agently-cli message +list\n") -> str:
    return json.dumps({"ok": True, "data": data}, ensure_ascii=False) + tip


def make_client(tmp: str, runner) -> MailClient:
    return MailClient(workdir=Path(tmp), runner=runner, timeout=5.0)


# --- 校验：不合法的输入一律不发 ---------------------------------------------


def test_recipient_must_be_an_email() -> None:
    for bad in ("", "not-an-email", "a@b", "a b@c.com", "@x.com", None, 123):
        try:
            clean_email(bad)
        except MailError as exc:
            assert exc.kind == "bad_recipient"
        else:
            raise AssertionError(f"应当拒绝：{bad!r}")


def test_subject_and_body_limits() -> None:
    assert clean_subject("  晚上好\n世界  ") == "晚上好 世界"
    assert clean_body("  正文  ") == "正文"
    for value, kind in ((("x" * 300), "bad_subject"),):
        try:
            clean_subject(value)
        except MailError as exc:
            assert exc.kind == kind
        else:
            raise AssertionError("超长主题应当被拒")
    try:
        clean_body("x" * (MAX_BODY_CHARS + 1))
    except MailError as exc:
        assert exc.kind == "too_long"
    else:
        raise AssertionError("超长正文应当被拒")
    try:
        clean_body("   ")
    except MailError as exc:
        assert exc.kind == "empty_body"
    else:
        raise AssertionError("空正文应当被拒")


def test_secret_like_body_is_refused() -> None:
    """她可能被诱导把密钥写进信里；"是她自己写的"不构成豁免。"""

    for text in ("我的 key 是 sk-abcdefghijklmnopqrst",
                 "api_key = 8f3ba91c77",
                 "密码：hunter2000"):
        try:
            clean_body(text)
        except MailError as exc:
            assert exc.kind == "secret_in_body", text
        else:
            raise AssertionError(f"应当拒绝疑似凭据：{text}")


def test_invalid_recipient_never_reaches_the_cli() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        runner = FakeRunner(ok_output({}))
        client = make_client(tmp, runner)
        try:
            asyncio.run(client.send(to="bad", subject="s", body="b"))
        except MailError as exc:
            assert exc.kind == "bad_recipient"
        else:
            raise AssertionError("非法收件人不该走到 CLI")
        assert runner.calls == []


# --- argv 形状：正文不进 argv ----------------------------------------------


def test_send_puts_the_body_in_a_file_not_in_argv() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        runner = FakeRunner(ok_output({"message_id": "msg_1"}))
        client = make_client(tmp, runner)
        secret_body = "这封信不该出现在进程列表里"
        result = asyncio.run(client.send(
            to=RECIPIENT, subject="汇报", body=secret_body, confirmed=True,
        ))
        assert result == {"message_id": "msg_1"}
        assert len(runner.calls) == 1
        call = runner.calls[0]
        argv = call["argv"]
        # argv[0] 是**解析过的**可执行文件路径：Windows 上 npm 装的是 .cmd，
        # 直接传 "agently-cli" 会 WinError 2（subprocess 不套用 PATHEXT）。
        assert argv[0].lower().endswith(("agently-cli", "agently-cli.cmd", "agently-cli.exe"))
        assert "--body-file" in argv
        assert "--body" not in argv
        assert not any(secret_body in part for part in argv), "正文绝不能出现在 argv 里"
        assert secret_body not in json.dumps(argv, ensure_ascii=False)
        # 收件人、主题、确认标志都在
        assert argv[argv.index("--to") + 1] == RECIPIENT
        assert argv[argv.index("--subject") + 1] == "汇报"
        assert "--confirmed" in argv
        # shell 必须关掉，cwd 必须是工作目录（CLI 要求相对路径）
        assert call["shell"] is False
        assert call["cwd"] == tmp


def test_body_file_exists_during_the_call_and_is_deleted_after() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        seen: list[Path] = []

        def runner(argv, **kwargs):
            name = argv[argv.index("--body-file") + 1]
            seen.append(Path(tmp) / name)
            assert seen[-1].exists(), "调用时正文文件必须已经写好"
            assert seen[-1].read_text(encoding="utf-8") == "信的内容"
            return FakeCompleted(ok_output({"message_id": "msg_1"}))

        client = MailClient(workdir=Path(tmp), runner=runner)
        asyncio.run(client.send(to=RECIPIENT, subject="s", body="信的内容"))
        assert seen and not seen[0].exists(), "发完必须删掉正文文件"


def test_body_file_is_deleted_even_when_the_cli_fails() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        seen: list[Path] = []

        def runner(argv, **kwargs):
            seen.append(Path(tmp) / argv[argv.index("--body-file") + 1])
            return FakeCompleted('{"ok": false, "error": {"type": "rate_limited"}}', returncode=6)

        client = MailClient(workdir=Path(tmp), runner=runner)
        try:
            asyncio.run(client.send(to=RECIPIENT, subject="s", body="x"))
        except MailError:
            pass
        assert seen and not seen[0].exists()


def test_confirmed_flag_is_omitted_when_not_confirmed() -> None:
    """CLI 自带确认机制：不带 --confirmed 就是"停在待确认"。"""

    with tempfile.TemporaryDirectory() as tmp:
        runner = FakeRunner(ok_output({"status": "pending"}))
        client = make_client(tmp, runner)
        asyncio.run(client.send(to=RECIPIENT, subject="s", body="b", confirmed=False))
        assert "--confirmed" not in runner.calls[0]["argv"]


def test_dry_run_passes_the_flag_and_returns_the_plan() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        plan = {"description": "Send a new message", "calls": [{"method": "POST"}]}
        runner = FakeRunner(json.dumps(plan))
        client = make_client(tmp, runner)
        result = asyncio.run(client.send(to=RECIPIENT, subject="s", body="b", dry_run=True))
        assert result == plan
        assert "--dry-run" in runner.calls[0]["argv"]


# --- 读路径 ----------------------------------------------------------------


def test_whoami_ignores_the_tip_line() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        payload = {"aliases": [{"email": "shiyunru@agent.qq.com"}], "rate_limits": {"daily_send_quota": 50}}
        runner = FakeRunner(ok_output(payload))
        client = make_client(tmp, runner)
        assert asyncio.run(client.whoami())["aliases"][0]["email"] == "shiyunru@agent.qq.com"


def test_read_rejects_a_weird_message_id() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        runner = FakeRunner(ok_output({}))
        client = make_client(tmp, runner)
        try:
            asyncio.run(client.read_message("../../etc/passwd"))
        except MailError as exc:
            assert exc.kind == "bad_message_id"
        else:
            raise AssertionError("奇怪的 id 不该走到 CLI")
        assert runner.calls == []


def test_list_clamps_the_limit() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        runner = FakeRunner(ok_output({"messages": []}))
        client = make_client(tmp, runner)
        asyncio.run(client.list_messages(limit=999))
        argv = runner.calls[0]["argv"]
        assert argv[argv.index("--limit") + 1] == "50"


# --- 失败分类 --------------------------------------------------------------


def test_api_error_carries_kind_and_code() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        body = json.dumps({"ok": False, "error": {"type": "api_error", "code": 404,
                                                 "message": "Resource not found"}})
        client = make_client(tmp, FakeRunner(body, returncode=6))
        try:
            asyncio.run(client.whoami())
        except MailError as exc:
            assert exc.kind == "api_error" and exc.code == 404
        else:
            raise AssertionError("应当抛 MailError")


def test_missing_cli_is_a_distinct_kind() -> None:
    """没装 CLI：上层据此静默关掉整条路径，而不是每轮报错。"""

    with tempfile.TemporaryDirectory() as tmp:
        client = make_client(tmp, FakeRunner(error=FileNotFoundError("no such file")))
        try:
            asyncio.run(client.whoami())
        except MailError as exc:
            assert exc.kind == "not_installed"
        else:
            raise AssertionError("应当抛 MailError")


def test_timeout_is_a_distinct_kind() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        client = make_client(tmp, FakeRunner(error=subprocess.TimeoutExpired("agently-cli", 5)))
        try:
            asyncio.run(client.whoami())
        except MailError as exc:
            assert exc.kind == "timeout"
        else:
            raise AssertionError("应当抛 MailError")


def test_unparsable_output_is_reported() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        client = make_client(tmp, FakeRunner("这不是 JSON"))
        try:
            asyncio.run(client.whoami())
        except MailError as exc:
            assert exc.kind == "unparsable"
        else:
            raise AssertionError("应当抛 MailError")


def test_only_whitelisted_commands_can_run() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        runner = FakeRunner(ok_output({}))
        client = make_client(tmp, runner)
        try:
            asyncio.run(client._run(["message", "+delete", "--all"], action="delete"))
        except MailError as exc:
            assert exc.kind == "not_allowed"
        else:
            raise AssertionError("删除不在白名单里")
        assert runner.calls == []


def test_subject_rejects_windows_metacharacters() -> None:
    """主题是唯一会进 argv 的自由文本，而 Windows 上 `.cmd` 会再过一遍 cmd 解析。

    一个引号就可能破坏引号边界，把"模型写的字符串"变成"本机命令"。宁可拒发，
    也不去"转义一下试试"——那套规则不是一个函数能说清的。
    """

    for text in ('他说"你好"', "完成度 50%", "a & b", "a|b", "a > b", "a^b", "it's"):
        try:
            clean_subject(text)
        except MailError as exc:
            assert exc.kind == "bad_subject", text
        else:
            raise AssertionError(f"应当拒绝含特殊字符的主题：{text!r}")


def test_email_pattern_rejects_quoting_characters() -> None:
    for bad in ('a"b@c.com', "a&b@c.com", "a|b@c.com", "a b@c.com", "a`b@c.com"):
        try:
            clean_email(bad)
        except MailError as exc:
            assert exc.kind == "bad_recipient", bad
        else:
            raise AssertionError(f"应当拒绝：{bad!r}")


def test_unknown_executable_is_not_installed() -> None:
    """找不到 CLI 时给一个明确类别：上层据此静默关掉整条路径。"""

    with tempfile.TemporaryDirectory() as tmp:
        client = MailClient(executable="definitely-not-installed-cli-xyz", workdir=Path(tmp),
                            runner=FakeRunner(ok_output({})))
        try:
            asyncio.run(client.whoami())
        except MailError as exc:
            assert exc.kind == "not_installed"
        else:
            raise AssertionError("应当抛 not_installed")


def test_workdir_is_created_before_spawning() -> None:
    """cwd 不存在时 CreateProcess 会报 WinError 2，看起来像"没装 CLI"——很容易误判。"""

    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "not" / "yet"
        runner = FakeRunner(ok_output({"aliases": []}))
        client = MailClient(workdir=target, runner=runner)
        asyncio.run(client.whoami())
        assert target.is_dir()
        assert runner.calls[0]["cwd"] == str(target)


# --- 不外泄 -----------------------------------------------------------------


def test_summarize_and_truncate_keep_logs_small() -> None:
    assert summarize_result({"message_id": "msg_1", "body": "整封信的内容"}) == "message_id=msg_1"
    long = "字" * 20000
    assert len(truncate_output(long)) < 8100
    assert "截断" in truncate_output(long)
