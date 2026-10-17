"""收件人**必须由 `.env` 给**（2026-10-16 用户原话："改成必须由 `.env` 给"）。

由来：`dev_config.MAIL_REPORT_TO` 原来带一个**真实邮箱**的默认值，而这条仓库在
GitHub 上是公开的——等于把收件人随源码一起发布了。默认值改成空串之后，
"没配收件人"变成一个**必须说出口**的状态，这个文件把那条链钉住：

1. **源码里没有默认地址**：一份空 `.env` 的机器上，`MAIL_REPORT_TO` 就是 `""`；
2. **`.env` 给了就取到它**：同一个子进程里比对"常量 == 配置里那个值"；
3. **真要发的那一刻先问收件人**：空的时候 `require_mail_report_to()` 返回空串
   （调用方据此不发）并记一条**带键名、不含地址**的 WARNING；
4. `/super mail send` 这条路**真的不发**：空收件人时日报器一次都不被调用；
5. **启动自检**那一行带 `**缺**`，而配了的时候那一行**不打出地址**。

为什么不 reload `dev_config` 验第 1、2 条：`MAIL_REPORT_TO` 是 **import 期**求值的常量，
而 reload 会顺手重跑 `_inject_operator_config()` / `load_env_file()`，并把其它模块早就
取走的常量留在旧值上——那会让后跑的用例被前一条的残留污染。所以这两条起**子进程**，
里面那份环境干净、退出即销毁。

（这里用的收件人一律是 `ops@example.invalid` 这种**假地址**：真实值只在部署方的
`.env` 里，不进任何仓库文件、也不进日志。）
"""
import asyncio
import logging
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

from qq_roleplay_bot import dev_config, runtime
from qq_roleplay_bot.stage3_main import DialogueEngine

ROOT = Path(__file__).resolve().parents[1]
RECIPIENT_ENV = "QQBOT_MAIL_REPORT_TO"
#: 假收件人。`.invalid` 是 RFC 2606 留的"永远不存在"的顶级域，不会被误当成真地址。
FAKE_TO = "ops@example.invalid"


@contextmanager
def _recipient(value: str):
    """把 `QQBOT_MAIL_REPORT_TO` 设成 `value`（空串 = 清掉），跑完原样还回去。"""

    saved = os.environ.get(RECIPIENT_ENV)
    os.environ.pop(RECIPIENT_ENV, None)
    if value:
        os.environ[RECIPIENT_ENV] = value
    try:
        yield
    finally:
        os.environ.pop(RECIPIENT_ENV, None)
        if saved is not None:
            os.environ[RECIPIENT_ENV] = saved


def _constant_under_env_file(body: str) -> str:
    """起一个**子进程**、用临时 `.env`（内容 = `body`）导入 `dev_config`，返回常量值。

    子进程里显式清掉收件人相关的几个键，并置 `QQBOT_STATE_PERSIST=0`：
    后者让 `operator_config.inject()` 直接返回 0——测试绝不该读到本机那份面板覆盖层
    （同 `operator_config.inject()` docstring 里那条测试隔离约定）。
    """

    env = {
        key: value for key, value in os.environ.items()
        if key not in {RECIPIENT_ENV, "QQBOT_MAIL_OWNER_FROM", "QQBOT_ENV_FILE"}
    }
    with tempfile.TemporaryDirectory() as tmp:
        env_file = Path(tmp) / ".env"
        env_file.write_text(body, encoding="utf-8")
        env["QQBOT_ENV_FILE"] = str(env_file)
        env["QQBOT_STATE_PERSIST"] = "0"
        env["PYTHONPATH"] = os.pathsep.join(
            [str(ROOT / "src")] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
        proc = subprocess.run(
            [sys.executable, "-c",
             "from qq_roleplay_bot import dev_config; print(dev_config.MAIL_REPORT_TO)"],
            cwd=str(ROOT), env=env, capture_output=True, text=True,
        )
    assert proc.returncode == 0, proc.stderr
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    return lines[-1].strip() if lines else ""


class _Counter:
    """最小可用的 client（与 `test_api_report.Counter` 同形状）：本文件不调模型。"""

    model = "fake-model"

    def cache_stats(self) -> dict[str, object]:
        return {"calls": 0, "hit_tokens": 0, "miss_tokens": 0, "hit_rate": 0.0}

    async def complete(self, request):
        return "<reply>嗯。</reply>"


class _Reporter:
    """假日报器：只数"到底有没有人真的要发"，不连 SMTP、不写盘。"""

    def __init__(self, recipient: str = "") -> None:
        self.recipient = recipient
        self.hour = 23
        self.minute = 0
        self.calls = 0

    async def run_once(self, seams, force: bool = False):
        self.calls += 1
        return SimpleNamespace(subject="测试主题", body="测试正文")


def _engine() -> DialogueEngine:
    return DialogueEngine(_Counter())


# --- ① 源码里没有默认收件人 ---------------------------------------------------


def test_the_source_has_no_default_recipient() -> None:
    """空 `.env` 的机器上，收件人就是空串——**源码里没有默认地址**。

    这条是本次改动的要害：原来 `get("QQBOT_MAIL_REPORT_TO", "<真实邮箱>")` 会让任何一台
    直接 clone 下来的机器都把一个**别人的地址**当收件人。
    """

    assert _constant_under_env_file("") == ""


def test_the_recipient_comes_from_the_env_file() -> None:
    """`.env` 给了收件人时，`MAIL_REPORT_TO` 就等于它（同一个子进程里比对）。"""

    assert _constant_under_env_file(f"{RECIPIENT_ENV}={FAKE_TO}\n") == FAKE_TO


# --- ② 真要发的时候先问收件人 -------------------------------------------------


def test_the_guard_returns_the_configured_recipient_and_says_nothing() -> None:
    """配了就是配了：`mail_report_to()` / `require_mail_report_to()` 都拿得到它。"""

    with _recipient(FAKE_TO):
        assert dev_config.mail_report_to() == FAKE_TO
        assert dev_config.require_mail_report_to("letter") == FAKE_TO
        with unittest.TestCase().assertNoLogs("qq_roleplay_bot.dev_config", level="WARNING"):
            # 配好了还告警，等于逼人去关提示——所以这条路径**一个字都不该记**。
            assert dev_config.require_mail_report_to("daily_report") == FAKE_TO


def test_the_guard_refuses_with_a_warning_naming_the_key_only() -> None:
    """空收件人：返回空串（= 不发）+ 一条**带键名、不含地址**的 WARNING。"""

    case = unittest.TestCase()
    with _recipient(""):
        with case.assertLogs("qq_roleplay_bot.dev_config", level="WARNING") as logs:
            got = dev_config.require_mail_report_to("letter")

    assert got == "", "没配收件人时必须返回空串，好让调用方不发"
    blob = "\n".join(logs.output)
    assert "mail_recipient_missing" in blob, blob
    assert RECIPIENT_ENV in blob, blob
    assert "letter" in blob, "要能看出是被哪条路拦下来的"
    assert "@" not in blob, f"告警里不许出现地址：{blob}"


def test_super_mail_send_does_not_send_without_a_recipient() -> None:
    """`/super mail send` 在空收件人时**一封都不发**：日报器一次不被调用。"""

    case = unittest.TestCase()
    engine = _engine()
    reporter = _Reporter()
    engine.daily_reporter = reporter
    engine.plugin_registry = SimpleNamespace(report=object())

    with _recipient(""):
        with case.assertLogs("qq_roleplay_bot.dev_config", level="WARNING"):
            answer = asyncio.run(engine._mail_send_now())

    assert reporter.calls == 0, "收件人没配还敢发，等于把信寄给空地址"
    assert RECIPIENT_ENV in answer, answer


def test_super_mail_send_still_sends_when_the_recipient_is_configured() -> None:
    """配了收件人：同一条路照旧发（守卫只是拦"没配"，不是拦"发信"）。"""

    engine = _engine()
    reporter = _Reporter(recipient=FAKE_TO)
    engine.daily_reporter = reporter
    engine.plugin_registry = SimpleNamespace(report=object())

    with _recipient(FAKE_TO):
        answer = asyncio.run(engine._mail_send_now())

    assert reporter.calls == 1, answer
    assert "已寄出" in answer, answer


# --- ③ 启动自检 ---------------------------------------------------------------


def test_the_startup_self_check_marks_the_missing_recipient() -> None:
    """启动自检那一行带 `**缺**`，并把缺的汇总成一条 WARNING（自己也会被记进日志）。"""

    case = unittest.TestCase()
    with _recipient(""):
        with case.assertLogs("qq_roleplay_bot.runtime", level="WARNING") as logs:
            lines = runtime._log_mail_recipient_self_check()

    blob = "\n".join(lines)
    assert "**缺**" in blob, blob
    assert RECIPIENT_ENV in blob, blob
    assert "邮件配置缺收件人" in "\n".join(logs.output)


def test_the_startup_self_check_never_prints_the_address() -> None:
    """配了的时候那一行写"有值"，但**地址本身**不进日志。"""

    with _recipient(FAKE_TO):
        lines = runtime._log_mail_recipient_self_check()

    blob = "\n".join(lines)
    assert "有值" in blob and "**缺**" not in blob, blob
    assert FAKE_TO not in blob, f"自检把收件人打出来了：{blob}"
    assert "@" not in blob, blob


def test_the_self_check_lines_are_logged_at_info_not_warning() -> None:
    """配好了就不该出现 WARNING——自检的"正常"是安静的（噪声会教人忽略告警）。"""

    with _recipient(FAKE_TO):
        records: list[logging.LogRecord] = []

        class _Collect(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        handler = _Collect()
        logger = logging.getLogger("qq_roleplay_bot.runtime")
        # 默认级别是 WARNING（没人配置过 logging 时），INFO 会在进 handler 之前就被
        # `isEnabledFor` 挡掉——所以这里临时开 INFO，跑完原样还回去。
        saved_level = logger.level
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        try:
            runtime._log_mail_recipient_self_check()
        finally:
            logger.removeHandler(handler)
            logger.setLevel(saved_level)

    levels = {record.levelno for record in records}
    assert logging.INFO in levels, "自检行本身要打出来（否则等于没自检）"
    assert logging.WARNING not in levels, [r.getMessage() for r in records]
