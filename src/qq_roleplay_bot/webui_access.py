"""面板的**接入层**：本地与远程两套连接逻辑，路由与业务在别处，这里只管"怎么进来"。

由来（2026-10-01 用户）："为了方便未来的部署，要求做好本地和远程两套面板的连接逻辑"。

两套模式差异只在**接入**，共用同一份路由与业务代码（没有第二份面板实现）：

| | `local`（默认） | `remote` |
| --- | --- | --- |
| 绑定 | `127.0.0.1` | `QQBOT_WEBUI_HOST`（可 `0.0.0.0`） |
| 认证 | `Authorization: Bearer <token>` | 登录换会话 cookie + CSRF token（口令加盐哈希） |
| 来源校验 | 不校验 | `Origin`/`Referer` 必须在白名单里 |
| 限速 | 无 | 认证失败限速 + 写动作令牌桶 |
| 代理信任 | 不读 `X-Forwarded-*` | 仅 `QQBOT_WEBUI_BEHIND_PROXY=1` 时读 |

**默认必须是 local**：面板与 bot 同进程，远程访问面板等于远程访问这台机器上的 bot
控制面（能改 prompt、换 key、踢人）。所以切 remote 时启动打 WARNING。

TLS 不在这里做（标准库不干这个）：远程部署用反向代理（Caddy/nginx）或隧道
（Tailscale/frp）终止 TLS，面板本身绑 `127.0.0.1` 让代理转进来。步骤见 `docs/WEBUI.md`。
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

LOCAL = "local"
REMOTE = "remote"
MODES = (LOCAL, REMOTE)

#: 口令哈希的迭代次数。scrypt 在标准库里就有，不引第三方。
SCRYPT_N = 2 ** 14
#: 会话 cookie 的有效期（秒）。过了要重新登录。
SESSION_TTL_SECONDS = 12 * 3600
#: 认证失败限速：这么多秒内失败这么多次就锁。
FAIL_WINDOW_SECONDS = 60.0
FAIL_LIMIT = 5
#: 写动作限速：每分钟多少次。
WRITE_LIMIT_PER_MINUTE = 60
#: Bearer token 最短长度。太短的 token 等于没有 token。
MIN_TOKEN_CHARS = 16


def default_token_path() -> str:
    base = os.environ.get("QQBOT_DATA_DIR", "").strip()
    if base:
        from pathlib import Path

        return str(Path(base) / "webui_token")
    from pathlib import Path

    return str(Path(__file__).resolve().parents[2] / "data" / "webui_token")


def load_or_create_token(path: str | None = None) -> str:
    """取面板 token；没有就生成一份（0600）。

    放在 `data/` 下而不是 `.env`：`.env` 是人工维护的、有注释有顺序的文件，
    让程序去改它迟早写坏（这也是覆盖层存在的原因）。
    """

    from pathlib import Path

    target = Path(path or default_token_path())
    try:
        existing = target.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        existing = ""
    except OSError as exc:
        logger.warning("webui_token_read_failed category=%s", type(exc).__name__)
        existing = ""
    if len(existing) >= MIN_TOKEN_CHARS:
        return existing
    token = secrets.token_urlsafe(32)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(token + "\n", encoding="utf-8")
        try:
            os.chmod(target, 0o600)
        except OSError:  # pragma: no cover - 平台差异
            pass
        logger.info("已生成面板令牌：%s（只有本机能读）", target)
    except OSError as exc:
        logger.warning("webui_token_write_failed category=%s", type(exc).__name__)
    return token


def hash_password(password: str, *, salt: str = "") -> str:
    """`scrypt` 加盐哈希，返回 `scrypt$<salt>$<hex>`。空口令返回空串。"""

    if not password:
        return ""
    salt_bytes = bytes.fromhex(salt) if salt else secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt_bytes,
                            n=SCRYPT_N, r=8, p=1, dklen=32)
    return f"scrypt${salt_bytes.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """比对口令与哈希。任何格式问题都当作不匹配（fail-closed）。"""

    if not password or not stored:
        return False
    try:
        scheme, salt, _digest = stored.split("$", 2)
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    try:
        candidate = hash_password(password, salt=salt)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(candidate, stored)


@dataclass
class _Fails:
    stamps: list[float] = field(default_factory=list)

    def hit(self, now: float) -> bool:
        """记一次失败。**已经在锁定期内就不再记**——理由见下面 `blocked` 的注释。"""

        if self.blocked(now):
            return True
        self.stamps.append(now)
        return len(self.stamps) > FAIL_LIMIT

    def blocked(self, now: float) -> bool:
        """现在是否处于锁定期。

        **锁的窗口从"最后一次真实失败"算起，不是从"最后一次被拒的请求"算起。**
        2026-10-01 踩到：原来在锁定期内每收到一个请求都还记一笔，于是只要还有请求
        （面板自己每 5 秒轮询一次），窗口就被不断刷新——**永远解不开**，
        一个错令牌足以把面板锁死到重启。所以 `hit` 在锁定期内直接返回、不记时间戳。
        """

        cutoff = now - FAIL_WINDOW_SECONDS
        self.stamps = [item for item in self.stamps if item > cutoff]
        return len(self.stamps) > FAIL_LIMIT

    def clear(self) -> None:
        self.stamps.clear()


@dataclass
class AccessDecision:
    """一次接入判定的结果。`ok=False` 时 `reason` 是固定说法（不回显期望值）。

    `cookies` 是**一组** `Set-Cookie` 值，不是一个：会话 cookie 要 `HttpOnly`，
    而 CSRF 那半必须让页面里的 JS 读得到（要放进 `X-CSRF-Token` 头）——
    这两个属性不同，只能各发一条头。挤在一条头里的话浏览器只认第一段，
    CSRF 那半就永远到不了页面（2026-10-01 真 HTTP 集成测试抓到：
    远程模式下**任何写操作都不可能成功**）。
    """

    ok: bool
    reason: str = ""
    status: int = 200
    cookies: tuple[str, ...] = ()
    source: str = ""


class WebAccess:
    """两套接入逻辑的判定与限速。**无 IO、无路由**，方便单测。"""

    def __init__(self, *, mode: str = LOCAL, token: str = "", password_hash: str = "",
                 allowed_origins: tuple[str, ...] = (), behind_proxy: bool = False,
                 clock=time.time) -> None:
        self.mode = mode if mode in MODES else LOCAL
        self.token = token or ""
        self.password_hash = password_hash or ""
        self.allowed_origins = tuple(allowed_origins)
        self.behind_proxy = bool(behind_proxy)
        self.clock = clock
        self._sessions: dict[str, float] = {}
        self._fails: dict[str, _Fails] = {}
        self._writes: dict[str, list[float]] = {}
        self.login_failures = 0

    # --- 通用 -------------------------------------------------------------

    @property
    def remote(self) -> bool:
        return self.mode == REMOTE

    @property
    def configured(self) -> bool:
        """有没有可用的凭据。没有就不该装配这个插件（fail-closed）。"""

        if self.remote:
            return bool(self.password_hash)
        return len(self.token) >= MIN_TOKEN_CHARS

    def source_of(self, peer: str, headers: dict[str, str]) -> str:
        """记审计用的来源。**只有显式开了代理信任才读 `X-Forwarded-For`**。"""

        if self.behind_proxy:
            forwarded = str(headers.get("x-forwarded-for", "")).split(",")[0].strip()
            if forwarded:
                return forwarded
        return str(peer or "")

    def origin_ok(self, headers: dict[str, str]) -> bool:
        """远程模式的来源校验（CSRF 的第一道）。local 模式放行。"""

        if not self.remote:
            return True
        if not self.allowed_origins:
            return False
        seen = [str(headers.get("origin", "")).strip(),
                str(headers.get("referer", "")).strip()]
        for value in seen:
            if not value:
                continue
            if value.startswith("null"):
                return False
            if any(value == allowed or value.startswith(allowed.rstrip("/") + "/")
                   for allowed in self.allowed_origins):
                return True
        return False

    # --- 本地：Bearer token -------------------------------------------------

    def check_bearer(self, headers: dict[str, str], peer: str) -> AccessDecision:
        """本地模式：`Authorization: Bearer <token>`。

        **先看令牌对不对，再谈限速**：正确令牌任何时候都放行。
        理由（2026-10-01）：反过来写的话，一个错令牌把面板锁死之后，
        操作者就算把正确令牌填进去也进不来，而面板每 5 秒还在轮询——
        自己把自己锁在门外。限速要挡的是"猜令牌"，不是"令牌已经填对了的人"。
        """

        if not self.token:
            return AccessDecision(False, "unauthorized", 401)
        source = self.source_of(peer, headers)
        now = self.clock()
        raw = str(headers.get("authorization", ""))
        scheme, _, presented = raw.partition(" ")
        if scheme.casefold() == "bearer" and presented \
                and hmac.compare_digest(presented.strip(), self.token):
            self._fails.setdefault(source, _Fails()).clear()
            return AccessDecision(True, source=source)
        state = self._fails.setdefault(source, _Fails())
        if state.blocked(now):
            return AccessDecision(False, "too_many_attempts", 429, source=source)
        state.hit(now)
        self.login_failures += 1
        return AccessDecision(False, "unauthorized", 401, source=source)

    # --- 远程：口令换会话 ---------------------------------------------------

    def login(self, password: str, peer: str) -> AccessDecision:
        now = self.clock()
        source = str(peer or "")
        state = self._fails.setdefault(source, _Fails())
        # 口令对就放行，不受限速影响（同 `check_bearer`：限速挡的是猜口令，
        # 不是"口令已经对了的人"）。
        if verify_password(password, self.password_hash):
            state.clear()
            session = secrets.token_urlsafe(24)
            self._sessions[session] = now + SESSION_TTL_SECONDS
            csrf = secrets.token_urlsafe(18)
            # 两条头：会话那条 HttpOnly（JS 读不到、防 XSS 偷会话）；
            # CSRF 那条**故意不加 HttpOnly**（页面要读出来放进请求头），
            # 并且**故意不加 SameSite=Strict**（那条属性会连站内读取也挡掉）。
            return AccessDecision(True, cookies=(
                f"yunru_panel={session}; Path=/; HttpOnly; SameSite=Strict",
                f"csrf={csrf}; Path=/; SameSite=Strict",
            ), source=source)
        if state.blocked(now):
            return AccessDecision(False, "too_many_attempts", 429, source=source)
        state.hit(now)
        self.login_failures += 1
        return AccessDecision(False, "unauthorized", 401, source=source)

    def check_cookie(self, headers: dict[str, str], peer: str) -> AccessDecision:
        source = self.source_of(peer, headers)
        now = self.clock()
        state = self._fails.setdefault(source, _Fails())
        if state.blocked(now):
            return AccessDecision(False, "too_many_attempts", 429, source=source)
        cookies = _parse_cookie(str(headers.get("cookie", "")))
        session = cookies.get("yunru_panel", "")
        expiry = self._sessions.get(session, 0.0)
        if not session or expiry < now:
            self._sessions.pop(session, None)
            state.hit(now)
            return AccessDecision(False, "unauthorized", 401, source=source)
        state.clear()
        return AccessDecision(True, source=source)

    def csrf_ok(self, headers: dict[str, str]) -> bool:
        """远程模式下的写操作必须带 CSRF token（与 cookie 里的那一半比对）。"""

        if not self.remote:
            return True
        cookies = _parse_cookie(str(headers.get("cookie", "")))
        expected = cookies.get("csrf", "")
        presented = str(headers.get("x-csrf-token", ""))
        if not expected or not presented:
            return False
        return hmac.compare_digest(expected, presented)

    def logout(self, headers: dict[str, str]) -> None:
        cookies = _parse_cookie(str(headers.get("cookie", "")))
        self._sessions.pop(cookies.get("yunru_panel", ""), None)

    # --- 限速 -------------------------------------------------------------

    def write_allowed(self, source: str) -> bool:
        """写动作令牌桶（每分钟 `WRITE_LIMIT_PER_MINUTE` 次）。local 模式不限。"""

        if not self.remote:
            return True
        now = self.clock()
        stamps = [item for item in self._writes.get(source, []) if now - item < 60.0]
        if len(stamps) >= WRITE_LIMIT_PER_MINUTE:
            self._writes[source] = stamps
            return False
        stamps.append(now)
        self._writes[source] = stamps
        return True

    def stats(self) -> dict[str, object]:
        return {"mode": self.mode, "remote": self.remote,
                "sessions": len(self._sessions),
                "login_failures": self.login_failures,
                "behind_proxy": self.behind_proxy}


def _parse_cookie(raw: str) -> dict[str, str]:
    jar: dict[str, str] = {}
    for part in (raw or "").split(";"):
        name, _, value = part.strip().partition("=")
        if name:
            jar[name.strip()] = value.strip()
    return jar


def token_fingerprint(token: str) -> str:
    """审计用：token 的短指纹（与 `control_audit.token_fingerprint` 同一算法）。"""

    from .control_audit import token_fingerprint as _fingerprint

    return _fingerprint(token)
