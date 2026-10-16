"""云茹的邮箱：`agently-cli`（QQ 邮箱 Agent Mail）的子进程封装。

边界（为什么这么写）：

1. **固定子命令、固定 argv，绝不拼字符串、绝不 `shell=True`。** 允许的子命令就是
   下面 `_ALLOWED` 里那几条，调用方只能填"经过校验的值"（收件人、主题、正文文件）。
2. **正文走 `--body-file` 不走 argv。** argv 会出现在进程列表里，同机别的用户看得到；
   CLI 支持从 UTF-8 文件读正文（相对当前目录），所以信写在 `data/mail_outbox/` 下的
   临时文件里，发完就删。
3. **凭据在我们这边完全不存在**：token 在系统钥匙串里由 CLI 自己管，我们不读、不存、
   不打印；返回的也只有它输出的结构化字段。
4. **失败一律抛 `MailError`（带类别）**，不让 `CalledProcessError` 之类漏出去——
   调用方要按类别决定"这轮还说不说话、要不要重试"。
5. 输出**截断**：CLI 的返回可能有很长的正文，日志里不该出现整封信。
   失败时把 CLI 的**原文**（`error.message` 等，压成一行、抹掉像凭据的片段、截断到
   `MAX_ERROR_CHARS`）记进日志——2026-10-11 那次查了一整天的故障，就是因为只记了
   `action_kind`，看不出那句原文。**记的是原文，不是把 CLI 的输出整段倒出来。**

真实观测（2026-09-27，v1.0.18）：
- 成功是 `{"ok": true, "data": {...}}`，后面还会跟一行 `tip: ...`——所以解析要
  从第一个 `{` 开始 `raw_decode`，不能要求整段都是 JSON；
- 失败是 `{"ok": false, "error": {"type","code","message","request_id"}}`，**退出码非 0**（实测 404 → 6）；
- `--dry-run` 吐的是另一种形状（`description` + `calls`），没有 `ok` 字段；
- 不带 `--confirmed` 时请求体里没有 `skip_confirmation`（等于停在"待确认"）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_EXECUTABLE = "agently-cli"
# 单次调用的墙钟上限。CLI 本身有 requests_per_minute=10 的限制，慢是正常的。
TIMEOUT_SECONDS = 15.0
# 我们这边的长度上限，比 CLI 自己那套（主题 4KB、正文 1MB）严得多：
# 汇报要能读得下去，她也别写成长文。
MAX_SUBJECT_CHARS = 200
MAX_BODY_CHARS = 10000
# 输出截断：CLI 的返回可能带很长的正文，别让它进日志。
MAX_OUTPUT_CHARS = 8000
# 失败时**原文**的截断长度（2026-10-11 加）。够看清"钥匙串锁不上"还是"令牌过期了"，
# 又不会把整封信或一整段输出带进日志。
MAX_ERROR_CHARS = 300

# 只有这些子命令能被调用。**这是白名单，不是"拼接模板"**。
_ALLOWED = ("+me", "message +send", "message +list", "message +read")

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

    为什么要它（2026-10-16 的真事）：两级写信里**只有旧路**洗过主题
    （`daily_report.parse_report_output` 那个 `re.sub`），而线上走的是两级那条路——
    主题从 `letter_writer.parse_letter` / `parse_subject` 出来**一个字都没洗**
    就进了 `clean_subject`。23:01 那封的主题是『上次没寄出去的信，和那句"你别掺和"』，
    `mail_state.json` 的 `sends` 里于是躺了三条 `bad_subject`。

    **清洗必须发生在 `clean_subject` 那道判之前**，否则白搭——那是唯一拒发的地方。
    这里**换而不删**（尽量保住原意，也保住字数）：

    - 引号按出现顺序配成全角引号：`"a"` → `“a”`（单引号同理 `'a'` → `‘a’`）；
    - 其余那几个（`` ` `` `%` `&` `|` `<` `>` `^` `!` `(` `)` `$`）换成**全角**版本：
      字符还在、意思还在，但它不再是 cmd.exe 的元字符（那才是这条规则的由来）。

    返回 `(清洗后的主题, 换过哪些字符)`；第二个值给日志与状态用——**"标题被改过"要看得见**。
    清洗后为空（原文只有空白/控制字符）时返回空串，**由调用方给兜底标题**：
    编标题是内容的事，这里只管字符。**幂等**：洗过的再洗一次还是它、且报"没换"。
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
        # 于是正常路径根本走不到这条拒发上（2026-10-16 加，起因见那个函数）。
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

    # --- 对外的三个动作 ---------------------------------------------------

    async def whoami(self) -> dict:
        """看这个邮箱是谁。返回 `data` 段（别名、额度、权限）。"""

        return self._unwrap(await self._run(["+me"], action="+me"))

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

    async def list_messages(self, *, limit: int = 10, folder: str = "inbox",
                            unread_only: bool = False) -> dict:
        """列邮件。首版只给"读"用，不做任何自动处理。"""

        count = max(1, min(50, int(limit)))
        argv = ["message", "+list", "--limit", str(count), "--dir", folder]
        if unread_only:
            argv.append("--is-unread")
        return self._unwrap(await self._run(argv, action="message +list"))

    async def read_message(self, message_id: str) -> dict:
        text = str(message_id or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", text):
            raise MailError("邮件 id 不合法", kind="bad_message_id")
        return self._unwrap(await self._run(["message", "+read", "--id", text],
                                            action="message +read"))

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


def summarize_result(payload: dict) -> str:
    """只挑可展示的字段，避免把正文或凭据带进日志/回复。"""

    keys = ("message_id", "id", "subject", "status", "code", "request_id", "email", "name")
    parts = [f"{key}={payload[key]}" for key in keys if payload.get(key) not in (None, "")]
    return " ".join(parts) or "ok"


def truncate_output(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return text[:MAX_OUTPUT_CHARS] + f"…（截断，原长 {len(text)}）"


def make_body_file(workdir: Path | str, body: str) -> tuple[Path, str]:
    """把正文写到工作目录下（CLI 要求相对路径）。给非 async 的调用方用。"""

    directory = Path(workdir)
    directory.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=str(directory), prefix="mail-body-", suffix=".txt", delete=False,
    )
    with handle:
        handle.write(body)
    path = Path(handle.name)
    return path, path.name
