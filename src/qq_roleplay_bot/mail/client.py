"""核心的发信客户端（M1：邮件"发信"能力进核心）。

## 为什么在核心复制一份，而不是 import 插件的 `mail_client`

M1 的判据是**删掉 `plugins/mail/` 之后 `outage_notice` / `timeout_alert` 仍能装、
仍能发信**。核心如果去 import 插件里的 `mail_client`，删掉插件目录时核心就
`ImportError` 了——判据直接不成立，而且 `tests/check_module_removal.py` 的
"核心有没有直连插件目录"那一列会红（核心不许认识具体插件模块）。

所以这里搬的是**发信所需的最小一份**：配置读取、CLI 子进程、`send`、标题清洗、
错误分类。**不搬**写信/日报/收信（那些在 M2–M4 才动），也不新增任何模型调用。

## 为什么要等到 M4 才能删掉插件里那份

`plugins/mail/` 现在还承担读信回信、日报、写信三件事，M1 只把"把一封信发出去"
这一小块复制过来，插件那份 `mail_client.py` 仍在使用。两份在 M1 期间**并存**是
刻意的：

- 插件那份还要服务 `list_messages` / `read_message` / `watch` 这些 M1 不搬的能力；
- 现在就让插件 import 核心，等于让插件树依赖"核心一定有这个模块"，
  而 M1 的验收要先证明**删掉插件、核心照跑**，不是反过来；
- M4（`dev/data/plans/02-邮件重构.md` §三）把写日报/写信拆出去、整个目录删除时，
  才会只剩核心这一份。到那时两处行为必须逐字一致（清洗、错误类别、日志字段
  都是同一套），否则就是"搬完悄悄变了个行为"。

## 与 `plugins/mail/mail_client.py` 的关系（给接手的人）

本文件的**发信路径**与插件那份逐字同源：常量、`_clip` / `_SECRET` 的脱敏、
`_ARGV_FORBIDDEN` 的 argv 闸门、`clean_*` 的校验、`MailError.kind` 的类别、
`mail_command_failed` 那行日志都没有另写一套。差别只有两处，都是"只留发信"：

- `_ALLOWED` 只留 `message +send`（收信那三条 M1 不搬）；
- 不再有 `list_messages` / `read_message`（它们属于 M2）。

这样 M4 删插件目录时，插件里剩下的收信代码与核心这份发信代码不会互相带着走私。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path

from .. import dev_config
from ..dev_config import data_dir

logger = logging.getLogger(__name__)

DEFAULT_EXECUTABLE = "agently-cli"
# 单次调用的墙钟上限。CLI 本身有 requests_per_minute=10 的限制，慢是正常的。
TIMEOUT_SECONDS = 15.0
# 我们这边的长度上限，比 CLI 自己那套（主题 4KB、正文 1MB）严得多：
# 汇报要能读得下去，她也别写成长文。
MAX_SUBJECT_CHARS = 200
MAX_BODY_CHARS = 10000
# 失败时**原文**的截断长度。够看清"钥匙串锁不上"还是"令牌过期了"，
# 又不会把整封信或一整段输出带进日志。
MAX_ERROR_CHARS = 300

# 只有这些子命令能被调用。**这是白名单，不是"拼接模板"**。
# M1 只搬发信：收信那三条（+me / +list / +read / +watch）留给 M2。
_ALLOWED = ("message +send",)

_EMAIL = re.compile(r"^[A-Za-z0-9._+-]{1,64}@[A-Za-z0-9](?:[A-Za-z0-9.-]{0,253})\.[A-Za-z]{2,}$")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
# argv 里**任何** token 都不许出现这些字符。原因很具体：Windows 上 npm 装出来的是
# `agently-cli.cmd`，而 `subprocess` 起 `.cmd` 时会走一遍 cmd.exe 的解析——主题里一个
# 引号就可能破坏引号边界，把"模型写的字符串"变成"本机命令"。**这是唯一一处会被
# 外部文本碰到 argv 的地方**（正文走文件），所以这里直接按最严的规则卡掉。
_ARGV_FORBIDDEN = frozenset('"\'`%&|<>^!()$')
#: 那几个字符**发送前**换成什么（`sanitize_subject`）。换成全角：字符还在、意思还在，
#: 但它不再是 cmd.exe 的元字符——"能保留原意就保留"落到具体字符上就是这个映射。
_SUBJECT_FULLWIDTH = {
    "`": "｀", "%": "％", "&": "＆", "|": "｜", "<": "＜", ">": "＞",
    "^": "＾", "!": "！", "(": "（", ")": "）", "$": "＄",
}
#: 引号没有"全角版"可换，按**出现顺序**配成一对：第 1 个是左引号、第 2 个是右引号……
#: 这样 `"你别掺和"` 出来就是 `“你别掺和”`，而不是两个一样的引号。
_SUBJECT_QUOTES = {'"': ("“", "”"), "'": ("‘", "’")}
_MAX_ARGV_CHARS = 500
# 只挡"像凭据"的串。**刻意不复用 `memory_model.SENSITIVE`**：那一条还禁 `<`、邮箱地址
# 和 Windows 路径（它是给记忆正文用的），拿来卡一封信会把正常内容也卡掉。
_SECRET = re.compile(
    r"sk-[A-Za-z0-9_-]{12,}|(?:password|passwd|api[_ -]?key|secret|bearer|access[_ -]?token)"
    r"\s*[:=]\s*\S{6,}|(?:密码|密钥|私钥|口令)\s*[:：=]\s*\S{4,}",
    re.IGNORECASE,
)


def _clip(text: object) -> str:
    """压成一行、抹掉像凭据的片段、截断。外部程序说的话要经这一道才准进日志。"""

    line = " ".join(str(text or "").split())
    line = _SECRET.sub("[已隐去]", line)
    if len(line) > MAX_ERROR_CHARS:
        line = line[:MAX_ERROR_CHARS] + "…（后略）"
    return line


def describe_error(error: object, *, returncode: object = None) -> str:
    """把 CLI 失败时的**原文**压成一行，供日志使用。

    为什么必须记（2026-10-11 的教训）：那天日志里只有 `action_kind=credential_lock_unavailable
    code=None`，看不出到底是"钥匙串被别的进程占着"还是"令牌过期续不上"——CLI 的 `message`
    里写着原因，我们却把它丢掉了，于是同一件事查了一整天。现在把原文留下来（仍截断+脱敏）。
    """

    parts: list[str] = []
    if isinstance(error, dict):
        for key in ("message", "hint", "request_id"):
            value = error.get(key)
            if value is None or value == "":
                continue
            text = " ".join(str(value).split())
            if text:
                parts.append(f"{key}={text}")
        if not parts and error:
            parts.append("error=" + json.dumps(error, ensure_ascii=False, sort_keys=True))
    if returncode is not None and str(returncode) not in ("0", "None"):
        parts.append(f"returncode={returncode}")
    return _clip(" ".join(parts))


class MailError(RuntimeError):
    """一次邮箱操作失败。`kind` 是固定类别，用来决定上层的策略。"""

    def __init__(self, message: str, *, kind: str = "unknown", code: int | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.code = code


def clean_email(value: object) -> str:
    """收件人校验。不合法就抛——**绝不猜地址、绝不"修一下试试"**。"""

    text = str(value or "").strip()
    if not _EMAIL.match(text):
        raise MailError("收件人地址不合法", kind="bad_recipient")
    return text


def sanitize_subject(value: object) -> tuple[str, tuple[str, ...]]:
    """主题**发送前**的兜底清洗：把那几个不能进 argv 的字符换掉。

    为什么它必须在 `clean_subject` 那道判之前（2026-10-16 的真事）：清洗要在**唯一拒发
    的地方**之前发生，否则白搭。这里**换而不删**（尽量保住原意，也保住字数）：

    - 引号按出现顺序配成全角引号：`"a"` → `“a”`（单引号同理 `'a'` → `‘a’`）；
    - 其余那几个（`` ` `` `%` `&` `|` `<` `>` `^` `!` `(` `)` `$`）换成**全角**版本：
      字符还在、意思还在，但它不再是 cmd.exe 的元字符（那才是这条规则的由来）。

    返回 `(清洗后的主题, 换过哪些字符)`；第二个值给日志与状态用——**"标题被改过"要看得见**。
    清洗后为空时返回空串，**由调用方给兜底标题**：编标题是内容的事，这里只管字符。
    **幂等**：洗过的再洗一次还是它、且报"没换"。
    """

    text = _CONTROL.sub("", str(value or "")).replace("\r", " ").replace("\n", " ").strip()
    counts = {quote: 0 for quote in _SUBJECT_QUOTES}
    seen: set[str] = set()
    changed: list[str] = []
    out: list[str] = []
    for char in text:
        if char in _SUBJECT_QUOTES:
            pair = _SUBJECT_QUOTES[char]
            out.append(pair[counts[char] % 2])
            counts[char] += 1
        elif char in _SUBJECT_FULLWIDTH:
            out.append(_SUBJECT_FULLWIDTH[char])
        else:
            out.append(char)
            continue
        if char not in seen:
            seen.add(char)
            changed.append(char)
    return "".join(out).strip(), tuple(changed)


#: 清洗后一个可用字符都不剩时用的标题。**各处共用这一个常量**
#: （插件树 `daily_report` 那个 `DEFAULT_SUBJECT` 就是它，两条路不再各写一个字面量）。
DEFAULT_SUBJECT = "给你的信"


def safe_subject(value: object, *, where: str = "",
                 default: str = DEFAULT_SUBJECT) -> str:
    """**发送前统一走的那一道**：清洗 + 兜底标题 + 留一行日志。

    `where` 只进日志，用来指认是哪一处洗的（`core_operator` / `core_send_to`）。
    **它必须在 `clean_subject` 那一道判之前调**——那是唯一拒发的地方；而且幂等。
    """

    cleaned, changed = sanitize_subject(value)
    if changed:
        logger.info("mail_subject_sanitized where=%s chars=%s before_len=%s after_len=%s",
                    where or "-", "".join(changed), len(str(value or "")), len(cleaned))
    if not cleaned:
        if default:
            logger.warning("mail_subject_empty_after_sanitize where=%s chars=%s",
                           where or "-", "".join(changed))
        return default
    return cleaned


def clean_subject(value: object) -> str:
    text = _CONTROL.sub("", str(value or "")).replace("\r", " ").replace("\n", " ").strip()
    if not text:
        raise MailError("主题不能为空", kind="bad_subject")
    if len(text) > MAX_SUBJECT_CHARS:
        raise MailError("主题过长", kind="bad_subject")
    if _ARGV_FORBIDDEN & set(text):
        # 主题是**唯一会进 argv 的自由文本**。含引号/百分号这类字符时宁可拒发，
        # 也不去"转义一下试试"——Windows 的引号转义规则不是一个函数能说清的。
        # 发送前那道兜底清洗是 `sanitize_subject`：它换掉这些字符，
        # 于是正常路径根本走不到这条拒发上。
        raise MailError("主题里有不能用的符号（引号、百分号、&这类）", kind="bad_subject")
    return text


def clean_body(value: object) -> str:
    """正文清理与上限。控制字符去掉，长度按我们自己的上限卡。"""

    text = _CONTROL.sub("", str(value or "")).strip()
    if not text:
        raise MailError("正文不能为空", kind="empty_body")
    if len(text) > MAX_BODY_CHARS:
        raise MailError("正文过长", kind="too_long")
    if _SECRET.search(text):
        # 她可能被诱导把密钥写进信里；"是她自己写的"不构成豁免。
        raise MailError("正文里出现疑似凭据", kind="secret_in_body")
    return text


class MailClient:
    """一个邮箱句柄。`runner` 可注入，测试里不用真的起进程。"""

    def __init__(
        self,
        executable: str = DEFAULT_EXECUTABLE,
        *,
        workdir: Path | str,
        timeout: float = TIMEOUT_SECONDS,
        runner=None,
        clock=time.time,
    ) -> None:
        self.executable = executable
        self.workdir = Path(workdir)
        self.timeout = timeout
        self.runner = runner or subprocess.run
        self.clock = clock
        self.calls = 0
        self.failures = 0
        self._executable_path = ""

    async def send(
        self,
        *,
        to: str,
        subject: str,
        body: str,
        confirmed: bool = True,
        dry_run: bool = False,
    ) -> dict:
        """发一封信。正文写文件传，argv 里只有收件人与主题。"""

        recipient = clean_email(to)
        clean_subject_value = clean_subject(subject)
        text = clean_body(body)
        argv = ["message", "+send", "--to", recipient, "--subject", clean_subject_value]
        if confirmed:
            argv.append("--confirmed")
        if dry_run:
            argv.append("--dry-run")
        body_name = f"mail-body-{uuid.uuid4().hex}.txt"
        argv.extend(["--body-file", body_name])
        path = self.workdir / body_name
        try:
            self.workdir.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        except OSError as exc:
            raise MailError("无法写入正文文件", kind="io_error") from exc
        try:
            payload = await self._run(argv, action="message +send")
        finally:
            # 正文文件必须删掉：它是**信的内容**，留在盘上等于第二份副本。
            try:
                path.unlink(missing_ok=True)
            except OSError:
                logger.warning("mail_body_cleanup_failed")
        if dry_run:
            # --dry-run 吐的是另一种形状（description + calls），没有 ok/data。
            return payload
        return self._unwrap(payload)

    # --- 内部 -------------------------------------------------------------

    async def _run(self, argv: list[str], *, action: str) -> dict:
        """起一次子进程，返回解析后的 JSON。同步跑，用 to_thread 包住；
        任何异常都变成 `MailError`，不让调用方看到 subprocess 的细节。"""

        if not self._allowed(argv):
            raise MailError(f"不允许的邮箱操作：{action}", kind="not_allowed")
        self._check_argv(argv)
        self.calls += 1
        try:
            return await asyncio.to_thread(self._run_sync, argv, action)
        except Exception as exc:  # noqa: BLE001
            self.failures += 1
            if isinstance(exc, MailError):
                raise
            raise MailError(f"调用邮箱工具失败：{type(exc).__name__}", kind="spawn_error") from exc

    @staticmethod
    def _check_argv(argv: list[str]) -> None:
        """给整条 argv 过一道闸：这是防"外部文本变成命令行"的最后一道。"""

        for token in argv:
            text = str(token)
            if len(text) > _MAX_ARGV_CHARS:
                raise MailError("参数过长", kind="bad_argument")
            if _CONTROL.search(text):
                raise MailError("参数里有控制字符", kind="bad_argument")
            if _ARGV_FORBIDDEN & set(text):
                raise MailError("参数里有不能用的符号", kind="bad_argument")

    def _resolve_executable(self) -> str:
        """找到真正的可执行文件。

        Windows 上 npm 装的是 `agently-cli.cmd`，而 `subprocess` 不像 shell 那样套用
        PATHEXT——直接传 "agently-cli" 会 `WinError 2`。`shutil.which` 才会去找 .CMD。
        找不到就抛 `not_installed`：上层据此静默关掉整条路径，而不是每轮报错。
        """

        if self._executable_path:
            return self._executable_path
        found = shutil.which(self.executable)
        if not found:
            raise MailError("没有找到邮箱工具", kind="not_installed")
        self._executable_path = found
        return found

    def _run_sync(self, argv: list[str], action: str = "") -> dict:
        executable = self._resolve_executable()
        try:
            # cwd 必须存在：指向不存在的目录时 CreateProcess 会报 WinError 2
            # （"系统找不到指定的文件"），看起来像可执行文件没装，其实只是目录没有。
            self.workdir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise MailError("无法创建工作目录", kind="io_error") from exc
        try:
            result = self.runner(
                [executable, *argv],
                cwd=str(self.workdir),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout,
                shell=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise MailError("邮箱工具超时", kind="timeout") from exc
        except FileNotFoundError as exc:
            # 没装 CLI：静默不可用，上层据此关掉整条路径。
            raise MailError("没有找到邮箱工具", kind="not_installed") from exc
        except OSError as exc:
            raise MailError(f"无法启动邮箱工具：{type(exc).__name__}", kind="spawn_error") from exc

        stdout = str(getattr(result, "stdout", "") or "")
        stderr = str(getattr(result, "stderr", "") or "")
        returncode = getattr(result, "returncode", None)
        payload = self._parse(stdout) or self._parse(stderr)
        if payload is None:
            # 连 JSON 都没有：把两段输出的开头留下（截断+脱敏）。这类失败原来一个字都不记，
            # 出了事只能靠"再手工跑一遍 CLI"去猜。
            logger.warning(
                "mail_command_unparsable action=%s returncode=%s detail=%s",
                action, returncode, _clip(f"{stdout} {stderr}"),
            )
            raise MailError("邮箱工具没有返回可解析的结果", kind="unparsable")
        if payload.get("ok") is False or returncode not in (0, None):
            error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
            kind = str(error.get("type") or "command_failed")
            code = error.get("code")
            # 类别 + 错误码 + **CLI 的原文**（截断+脱敏）。原文是查故障的唯一线索：
            # credential_lock_unavailable 既可能是"锁被占"，也可能是"写不了令牌存储"，
            # 两者只有 CLI 那句话能分开。
            logger.warning(
                "mail_command_failed action=%s action_kind=%s code=%s detail=%s",
                action, kind, code, describe_error(error, returncode=returncode),
            )
            raise MailError(
                f"邮箱操作失败（{kind}）",
                kind=kind,
                code=int(code) if isinstance(code, int) else None,
            )
        return payload

    @staticmethod
    def _allowed(argv: list[str]) -> bool:
        joined = " ".join(argv[:2])
        return any(joined.startswith(prefix) for prefix in _ALLOWED)

    @staticmethod
    def _parse(text: str) -> dict | None:
        """从输出里取出第一个 JSON 对象。

        实测 CLI 会在 JSON 之后再打一行 `tip: ...`，所以不能要求整段都是 JSON。
        """

        if not isinstance(text, str) or not text.strip():
            return None
        start = text.find("{")
        if start < 0:
            return None
        try:
            value, _ = json.JSONDecoder().raw_decode(text[start:])
        except ValueError:
            return None
        return value if isinstance(value, dict) else None

    @staticmethod
    def _unwrap(payload: object) -> dict:
        if not isinstance(payload, dict):
            return {}
        data = payload.get("data")
        return data if isinstance(data, dict) else dict(payload)


def mail_workdir() -> Path:
    """邮箱 CLI 的工作目录。和 `data/` 一起搬走就能接着跑。"""

    return data_dir() / "mail_outbox"


def operator_recipient() -> str:
    """「给操作者发一封」的收件人：**这一刻现读**，空串 = 没配。

    与插件侧 `wire.build_operator_mail_sender` 同一口径：主读数 `MAIL_REPORT_TO`
    现读环境（`dev_config.mail_report_to()`），回落到主人地址 `MAIL_OWNER_FROM`。
    **不猜默认地址**（用户 2026-10-16：收件人"必须由 `.env` 给"）——猜错就是把信
    寄给陌生人，比不发严重得多。
    """

    recipient = (dev_config.mail_report_to() or "").strip()
    if not recipient:
        recipient = (dev_config.MAIL_OWNER_FROM or "").strip()
    return recipient


def build_client() -> MailClient:
    """按当前配置造一个客户端（装配时调；测试可以换掉 `MailClient`）。"""

    return MailClient(dev_config.MAIL_CLI, workdir=mail_workdir())
