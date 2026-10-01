"""面板接入层：本地 token 与远程会话两套连接逻辑。

由来（2026-10-01 用户）："为了方便未来的部署，要求做好本地和远程两套面板的连接逻辑"。
两套只差**接入**（绑定、认证、来源校验、限速、代理信任），路由与业务共用一份。
这个文件钉的是"差异本身"，尤其是**默认必须安全**：远程多出来的每一条都要真的生效。
"""
import os
import tempfile
import time
from pathlib import Path

from qq_roleplay_bot.webui_access import (
    FAIL_LIMIT, FAIL_WINDOW_SECONDS, LOCAL, MIN_TOKEN_CHARS, REMOTE, WebAccess,
    hash_password, load_or_create_token, verify_password,
)

TOKEN = "t" * 32
PASSWORD = "yunru-panel-password"


def _local() -> WebAccess:
    return WebAccess(mode=LOCAL, token=TOKEN)


def _remote(**kwargs) -> WebAccess:
    return WebAccess(mode=REMOTE, password_hash=hash_password(PASSWORD),
                     allowed_origins=("http://panel.example:8790",), **kwargs)


def _bearer(value: str = TOKEN) -> dict[str, str]:
    return {"authorization": f"Bearer {value}"}


# --- 本地 -------------------------------------------------------------------

def test_local_mode_requires_the_token() -> None:
    access = _local()
    assert access.configured is True
    assert access.check_bearer({}, "127.0.0.1").ok is False
    assert access.check_bearer(_bearer("wrong"), "127.0.0.1").ok is False
    assert access.check_bearer(_bearer(), "127.0.0.1").ok is True


def test_local_mode_accepts_scheme_case_insensitively() -> None:
    access = _local()
    assert access.check_bearer({"authorization": f"bearer {TOKEN}"}, "x").ok is True


def test_local_mode_does_not_rate_limit_writes_or_check_origins() -> None:
    access = _local()
    for _ in range(200):
        assert access.write_allowed("127.0.0.1") is True
    assert access.origin_ok({}) is True
    assert access.csrf_ok({}) is True


def test_short_token_is_not_configured() -> None:
    """太短的 token 等于没有 token：`configured` 为假，插件就不该装配。"""

    access = WebAccess(mode=LOCAL, token="short")
    assert access.configured is False
    assert MIN_TOKEN_CHARS > len("short")


def test_bearer_failures_are_rate_limited() -> None:
    """猜令牌会被限速：锁定期内**错的**令牌直接 429（而不是慢慢试）。"""

    access = _local()
    for _ in range(FAIL_LIMIT + 1):
        access.check_bearer(_bearer("wrong"), "10.0.0.9")
    assert access.check_bearer(_bearer("wrong"), "10.0.0.9").status == 429
    # 另一个来源不受影响
    assert access.check_bearer(_bearer("wrong"), "10.0.0.10").status == 401
    assert access.check_bearer(_bearer(), "10.0.0.10").ok is True


def test_the_lock_expires_even_if_requests_keep_coming() -> None:
    """锁定期内继续被拒的请求**不再推迟解锁**。

    2026-10-01 现场：原来每收到一个被拒的请求就记一笔时间戳，于是只要还有请求
    （面板自己每 5 秒轮询一次），窗口就被不断刷新——错令牌足以把面板锁死到重启。
    """

    now = [0.0]
    access = WebAccess(mode=LOCAL, token=TOKEN, clock=lambda: now[0])
    for _ in range(FAIL_LIMIT + 1):
        access.check_bearer(_bearer("wrong"), "10.0.0.9")
    # 锁定期内"持续被拒"几十次
    for index in range(30):
        now[0] += 5.0
        access.check_bearer(_bearer("wrong"), "10.0.0.9")
    # 从最后一次**真实失败**算起过了窗口，就该解锁
    now[0] += FAIL_WINDOW_SECONDS + 1
    assert access.check_bearer(_bearer(), "10.0.0.9").ok is True


def test_a_correct_token_always_gets_in() -> None:
    """令牌填对了就必须放行——限速挡的是猜令牌，不是"已经填对的人"。

    这条是上面那个事故的另一半：锁死之后连正确令牌也进不来，操作者只能重启。
    """

    now = [0.0]
    access = WebAccess(mode=LOCAL, token=TOKEN, clock=lambda: now[0])
    for _ in range(FAIL_LIMIT + 5):
        access.check_bearer(_bearer("wrong"), "10.0.0.9")
    assert access.check_bearer(_bearer(), "10.0.0.9").ok is True


def test_remote_login_accepts_the_right_password_after_failures() -> None:
    access = _remote()
    for _ in range(FAIL_LIMIT + 1):
        access.login("nope", "7.7.7.7")
    assert access.login(PASSWORD, "7.7.7.7").ok is True


# --- 远程 -------------------------------------------------------------------

def test_remote_mode_needs_a_password_hash() -> None:
    assert WebAccess(mode=REMOTE).configured is False
    assert _remote().configured is True


def _cookie_pair(decision) -> tuple[str, str]:
    """从两条 `Set-Cookie` 里拼出浏览器会回传的东西：`(Cookie 头, CSRF token)`。

    `decision.cookies` 是**两条**头（会话那条 HttpOnly，CSRF 那条不加 HttpOnly），
    浏览器两条都会回传——所以这里拼成一条 `Cookie` 头。只回传会话那条的写法
    曾经让 `csrf_ok` 永远为假（真 HTTP 集成测试抓到）。
    """

    session = next((value for value in decision.cookies if value.startswith("yunru_panel=")), "")
    csrf = next((value for value in decision.cookies if value.startswith("csrf=")), "")
    pair = "; ".join([session.split(";")[0], csrf.split(";")[0]]).strip("; ")
    return pair, csrf[len("csrf="):].split(";")[0]


def test_remote_login_issues_a_session_then_cookie_works() -> None:
    access = _remote()
    assert access.check_cookie({}, "1.2.3.4").ok is False
    decision = access.login(PASSWORD, "1.2.3.4")
    assert decision.ok is True
    pair, csrf = _cookie_pair(decision)
    headers = {"cookie": pair}
    assert access.check_cookie(headers, "1.2.3.4").ok is True
    assert access.csrf_ok({**headers, "x-csrf-token": "wrong"}) is False
    assert access.csrf_ok({**headers, "x-csrf-token": csrf}) is True
    # 没有 CSRF 头一律不行（跨站表单发不出这个头）
    assert access.csrf_ok(headers) is False


def test_remote_login_is_rate_limited() -> None:
    """猜口令会被限速：锁定期内**错的**口令直接 429（口令对了照样放行，见下一条）。"""

    access = _remote()
    for _ in range(FAIL_LIMIT + 1):
        access.login("nope", "5.6.7.8")
    assert access.login("nope", "5.6.7.8").status == 429


def test_remote_origin_allowlist() -> None:
    access = _remote()
    assert access.origin_ok({"origin": "http://panel.example:8790"}) is True
    assert access.origin_ok({"referer": "http://panel.example:8790/api/overview"}) is True
    assert access.origin_ok({"origin": "http://evil.example"}) is False
    assert access.origin_ok({"origin": "null"}) is False
    assert access.origin_ok({}) is False


def test_remote_without_configured_origins_refuses_everything() -> None:
    access = WebAccess(mode=REMOTE, password_hash=hash_password(PASSWORD))
    assert access.origin_ok({"origin": "http://anything"}) is False


def test_remote_write_rate_limit() -> None:
    access = _remote()
    allowed = sum(1 for _ in range(200) if access.write_allowed("9.9.9.9"))
    assert 0 < allowed <= 60
    assert access.write_allowed("9.9.9.9") is False


def test_forwarded_header_only_trusted_when_enabled() -> None:
    plain = _remote()
    assert plain.source_of("10.0.0.1", {"x-forwarded-for": "8.8.8.8"}) == "10.0.0.1"
    trusting = _remote(behind_proxy=True)
    assert trusting.source_of("10.0.0.1", {"x-forwarded-for": "8.8.8.8, 1.2.3.4"}) == "8.8.8.8"


def test_session_expires() -> None:
    now = [1000.0]
    access = WebAccess(mode=REMOTE, password_hash=hash_password(PASSWORD),
                       allowed_origins=("http://x",), clock=lambda: now[0])
    cookie, _csrf = _cookie_pair(access.login(PASSWORD, "1.1.1.1"))
    assert access.check_cookie({"cookie": cookie}, "1.1.1.1").ok is True
    now[0] += 13 * 3600
    assert access.check_cookie({"cookie": cookie}, "1.1.1.1").ok is False


def test_logout_invalidates_the_session() -> None:
    access = _remote()
    cookie, _csrf = _cookie_pair(access.login(PASSWORD, "2.2.2.2"))
    access.logout({"cookie": cookie})
    assert access.check_cookie({"cookie": cookie}, "2.2.2.2").ok is False


def test_login_sets_two_separate_cookies() -> None:
    """会话与 CSRF 必须**分两条** `Set-Cookie`：属性不同，挤一条会丢后一半。

    真 HTTP 集成测试抓到过：挤在一条里时浏览器只认第一段，CSRF 那半到不了页面，
    于是远程模式下**任何写操作都不可能成功**。
    """

    decision = _remote().login(PASSWORD, "3.3.3.3")
    assert len(decision.cookies) == 2
    session = next(value for value in decision.cookies if value.startswith("yunru_panel="))
    csrf = next(value for value in decision.cookies if value.startswith("csrf="))
    assert "HttpOnly" in session, "会话 cookie 要 HttpOnly（防 XSS 偷会话）"
    assert "HttpOnly" not in csrf, "CSRF cookie 要让页面 JS 读得到"
    for value in (session, csrf):
        assert "Path=/" in value
        assert "SameSite=Strict" in value


# --- 口令哈希 ---------------------------------------------------------------

def test_password_hash_round_trip() -> None:
    stored = hash_password(PASSWORD)
    assert stored.startswith("scrypt$")
    assert PASSWORD not in stored, "哈希里不该出现明文"
    assert verify_password(PASSWORD, stored) is True
    assert verify_password("other", stored) is False
    assert verify_password("", stored) is False
    assert verify_password(PASSWORD, "") is False
    assert verify_password(PASSWORD, "bogus$format") is False
    # 同一口令两次哈希不同（加了随机盐）
    assert hash_password(PASSWORD) != stored


def test_token_file_is_created_and_reused() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "webui_token"
        first = load_or_create_token(str(path))
        assert len(first) >= MIN_TOKEN_CHARS
        assert load_or_create_token(str(path)) == first
        assert path.read_text(encoding="utf-8").strip() == first


def test_token_file_recovers_from_a_short_value() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "webui_token"
        path.write_text("abc", encoding="utf-8")
        fresh = load_or_create_token(str(path))
        assert len(fresh) >= MIN_TOKEN_CHARS
        assert fresh != "abc"


def test_default_token_path_follows_data_dir() -> None:
    from qq_roleplay_bot.webui_access import default_token_path

    with tempfile.TemporaryDirectory() as tmp:
        saved = os.environ.get("QQBOT_DATA_DIR")
        os.environ["QQBOT_DATA_DIR"] = tmp
        try:
            assert default_token_path().startswith(tmp)
        finally:
            if saved is None:
                os.environ.pop("QQBOT_DATA_DIR", None)
            else:
                os.environ["QQBOT_DATA_DIR"] = saved


def test_stats_reports_mode() -> None:
    assert _local().stats()["remote"] is False
    assert _remote().stats()["remote"] is True
    assert _local().stats()["mode"] == LOCAL


def test_clock_is_injected_for_tests() -> None:
    now = [0.0]
    access = WebAccess(mode=LOCAL, token=TOKEN, clock=lambda: now[0])
    assert access.check_bearer({"authorization": "Bearer bad"}, "x").ok is False
    now[0] += 3600.0
    assert access.check_bearer(_bearer(), "x").ok is True
    assert time.time() > 0
