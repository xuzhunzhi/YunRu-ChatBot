"""好感相对主人的三道上限（2026-10-25 用户口径）。

用户原话：*"超过主人20后停止上涨；主人追平后才允许继续上涨。到达80以后需落后
主人5点以上才允许增长，到达85后需等待主人95才允许增长"*；追问"追平"的口径，
用户答：*"我也到75"* —— 追平 = 主人好感 ≥ 对方好感。

这里钉四件事：
1. 三道的判据各自独立（20 点 / 80 档 5 点 / 85 档等主人 95）；
2. 只拦**正增量**，且只拦"别人"：主人自己、`manual=True`、负增量都不受限；
3. 被冻住时**不整单拒绝**：guardedness 照旧写，closeness 归零；
4. 主人还没建过关系（关系表里没这个人）→ 不做上限（否则新部署会把人冻在 30）。
"""
import tempfile
import time
from pathlib import Path

from qq_roleplay_bot import dev_config
from qq_roleplay_bot.memory_store import MemoryStore, closeness_allowed_by_owner

OWNER = "900000001"
OTHER = "123456789"
QUOTE = {"quote": "聊得挺开心", "who_alias": "", "seq": 0}


def _store() -> tuple[MemoryStore, Path]:
    directory = Path(tempfile.mkdtemp(prefix="owner-cap-"))
    return MemoryStore(directory / "memory.sqlite3"), directory / "memory.sqlite3"


def _set(store: MemoryStore, user: str, closeness: int, guardedness: int = 0) -> None:
    with store.connection() as db:
        db.execute(
            "INSERT INTO relationships VALUES (?,?,?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET closeness=excluded.closeness, "
            "guardedness=excluded.guardedness",
            (user, closeness, guardedness, time.time(), time.time()),
        )


def _raise(store: MemoryStore, user: str, delta: int = 8, **kwargs) -> str:
    return store.apply_affinity(
        user, closeness=delta, reason="聊得来", source="maintenance",
        evidence=QUOTE, **kwargs)


def test_the_curve_itself() -> None:
    # 75 vs 44：超过 20 点 → 冻到主人追平
    assert closeness_allowed_by_owner(75, 44) is False
    assert closeness_allowed_by_owner(75, 54) is False      # 差 21 > 20
    assert closeness_allowed_by_owner(75, 55) is True        # 差正好 20：不冻
    assert closeness_allowed_by_owner(75, 75) is True        # 追平
    # 80..84：必须落后主人 5 点
    assert closeness_allowed_by_owner(82, 79) is False
    assert closeness_allowed_by_owner(82, 80) is False       # 只落后 2 点
    assert closeness_allowed_by_owner(82, 87) is True
    # 85 以上：等主人 95
    assert closeness_allowed_by_owner(85, 94) is False
    assert closeness_allowed_by_owner(85, 95) is True


def test_a_user_more_than_twenty_ahead_is_frozen_until_the_owner_catches_up() -> None:
    store, path = _store()
    original = dev_config.MAIL_OWNER_USER_ID
    dev_config.MAIL_OWNER_USER_ID = OWNER
    try:
        _set(store, OWNER, 44)
        _set(store, OTHER, 75)
        assert _raise(store, OTHER) == "affinity_rejected_owner_cap"
        assert store.relationship(OTHER)[0] == 75, "冻住就是一点都不涨"

        _set(store, OWNER, 75)  # 主人追平
        assert _raise(store, OTHER) == ""
        assert store.relationship(OTHER)[0] == 77, "75 档边际 +2"
    finally:
        dev_config.MAIL_OWNER_USER_ID = original
        store.path.unlink(missing_ok=True)
        path.unlink(missing_ok=True)


def test_at_eighty_the_user_must_stay_five_behind() -> None:
    store, path = _store()
    original = dev_config.MAIL_OWNER_USER_ID
    dev_config.MAIL_OWNER_USER_ID = OWNER
    try:
        _set(store, OTHER, 82)
        _set(store, OWNER, 79)
        assert _raise(store, OTHER) == "affinity_rejected_owner_cap"
        _set(store, OWNER, 80)  # 落后 2 点 → 还不够
        assert _raise(store, OTHER) == "affinity_rejected_owner_cap"
        _set(store, OWNER, 87)  # 落后 5 点
        assert _raise(store, OTHER) == ""
    finally:
        dev_config.MAIL_OWNER_USER_ID = original
        path.unlink(missing_ok=True)


def test_at_eighty_five_the_owner_must_reach_ninety_five() -> None:
    store, path = _store()
    original = dev_config.MAIL_OWNER_USER_ID
    dev_config.MAIL_OWNER_USER_ID = OWNER
    try:
        _set(store, OTHER, 85)
        _set(store, OWNER, 94)
        assert _raise(store, OTHER) == "affinity_rejected_owner_cap"
        _set(store, OWNER, 95)
        assert _raise(store, OTHER) == ""
    finally:
        dev_config.MAIL_OWNER_USER_ID = original
        path.unlink(missing_ok=True)


def test_the_block_only_hits_closeness_not_guardedness() -> None:
    store, path = _store()
    original = dev_config.MAIL_OWNER_USER_ID
    dev_config.MAIL_OWNER_USER_ID = OWNER
    try:
        _set(store, OWNER, 10)
        _set(store, OTHER, 75)
        assert store.apply_affinity(
            OTHER, closeness=8, guardedness=8, reason="越界", source="maintenance",
            evidence=QUOTE) == ""
        closeness, guardedness = store.relationship(OTHER)
        assert closeness == 75, "好感冻住"
        assert guardedness == 8, "防备照旧写"
    finally:
        dev_config.MAIL_OWNER_USER_ID = original
        path.unlink(missing_ok=True)


def test_owner_self_manual_and_drops_are_not_capped() -> None:
    store, path = _store()
    original = dev_config.MAIL_OWNER_USER_ID
    dev_config.MAIL_OWNER_USER_ID = OWNER
    try:
        _set(store, OWNER, 90)
        # 主人自己：不受"不能超过主人"限制（90 档边际只有 +1）
        assert _raise(store, OWNER, 2) == ""
        assert store.relationship(OWNER)[0] == 91
        # manual：面板/超管那条路不受限（95 会撞"高位不降"，所以只在 95 上加）
        _set(store, OTHER, 95)
        assert store.apply_affinity(
            OTHER, closeness=5, reason="人工", source="manual", manual=True) == ""
        assert store.relationship(OTHER)[0] == 100
        # 负增量：不受三道上限（80 档、距离主人很远也照降）
        _set(store, OTHER, 80)
        assert store.apply_affinity(
            OTHER, closeness=-4, reason="冷一点", source="maintenance",
            evidence=QUOTE) == ""
        assert store.relationship(OTHER)[0] == 76
    finally:
        dev_config.MAIL_OWNER_USER_ID = original
        path.unlink(missing_ok=True)


def test_no_owner_row_means_no_cap() -> None:
    store, path = _store()
    original = dev_config.MAIL_OWNER_USER_ID
    dev_config.MAIL_OWNER_USER_ID = OWNER
    try:
        # 关系表里没有主人 → 不做上限（新部署不该一上来就把人冻住）
        _set(store, OTHER, 75)
        assert _raise(store, OTHER) == ""
    finally:
        dev_config.MAIL_OWNER_USER_ID = original
        path.unlink(missing_ok=True)
