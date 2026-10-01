"""本地状态持久化：群启停名单、启停开关与短期会话。

设计约束：
- 只用标准库 JSON，原子写入（临时文件 + 替换），不使用数据库连接；
- 任何读写失败都降级为“按默认值运行”，绝不阻塞 QQ 对话；
- 只保存运行状态（会话阶段、绑定用户、短期语境、最近消息），
  不保存 API Key、模型请求体或记忆库内容。
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

STATE_VERSION = 1
MAX_PERSISTED_SESSIONS = 200
MAX_PERSISTED_HISTORY = 50


def state_path_default() -> Path:
    """默认状态文件路径：可通过 QQBOT_STATE_FILE / QQBOT_DATA_DIR 覆盖。"""

    explicit = os.environ.get("QQBOT_STATE_FILE", "").strip()
    if explicit:
        return Path(explicit)
    base = os.environ.get("QQBOT_DATA_DIR", "").strip()
    root = Path(base) if base else Path(__file__).resolve().parents[2] / "data"
    return root / "runtime_state.json"


@dataclass(frozen=True, slots=True)
class PersistedState:
    """一次加载得到的运行状态快照。"""

    enabled: bool = True
    enabled_group_ids: tuple[str, ...] = ()
    target_group_id: str = ""
    sessions: tuple[dict[str, object], ...] = ()
    # 管理员是**按群授权**的：`/super addadmin @某人` 在哪个群发的，就记在哪个群下面。
    # 配置里的 `ADMIN_USER_IDS` 是部署级名单，每次启动重新生效，不写在这里。
    group_admins: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # 早期版本写过的"全局管理员名单"。现在只读出来用于提示，不再写——
    # 它没有群的信息，没法平移成按群授权（fail-closed：宁可不给权限）。
    admin_user_ids: tuple[str, ...] = ()


class RuntimeStateStore:
    """带原子写入与全量降级的运行状态存储。"""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else state_path_default()
        self.enabled = True
        self.last_error = ""

    def load(self) -> PersistedState:
        """读取状态；文件缺失、损坏或权限不足时返回默认值。"""

        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return PersistedState()
        except OSError as exc:
            self.last_error = type(exc).__name__
            logger.warning("runtime_state_read_failed category=%s", type(exc).__name__)
            return PersistedState()
        try:
            data = json.loads(raw)
        except ValueError:
            self.last_error = "invalid_json"
            logger.warning("runtime_state_invalid_json; falling back to defaults")
            return PersistedState()
        if not isinstance(data, dict) or data.get("version") != STATE_VERSION:
            self.last_error = "unsupported_version"
            logger.warning("runtime_state_unsupported_version; falling back to defaults")
            return PersistedState()

        groups = data.get("enabled_group_ids")
        sessions = data.get("sessions")
        admins = data.get("admin_user_ids")
        self.enabled = bool(data.get("enabled", True))
        return PersistedState(
            enabled=bool(data.get("enabled", True)),
            enabled_group_ids=self._clean_group_ids(groups),
            target_group_id=str(data.get("target_group_id", "") or ""),
            sessions=tuple(item for item in sessions if isinstance(item, dict)) if isinstance(sessions, list) else (),
            group_admins=self._clean_group_admins(data.get("group_admins")),
            admin_user_ids=self._clean_user_ids(admins),
        )

    @classmethod
    def _clean_group_admins(cls, value: object) -> dict[str, tuple[str, ...]]:
        """`{群号: [QQ号, …]}`；群号与 QQ 号都按同一套规则清洗（状态文件是外部输入）。"""

        if not isinstance(value, dict):
            return {}
        cleaned: dict[str, tuple[str, ...]] = {}
        for group_id, admins in value.items():
            text = str(group_id).strip()
            if not (text.isdigit() and 5 <= len(text) <= 12):
                continue
            users = cls._clean_user_ids(admins if isinstance(admins, list) else None)
            if users:
                cleaned[text] = users
        return cleaned

    def save(self, payload: dict[str, object]) -> bool:
        """原子写入状态；失败只记录异常类别并返回 False。"""

        body = {"version": STATE_VERSION, **payload}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle = tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=str(self.path.parent),
                prefix=self.path.name + ".", suffix=".tmp", delete=False,
            )
            try:
                with handle:
                    json.dump(body, handle, ensure_ascii=False)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(handle.name, self.path)
            except BaseException:
                Path(handle.name).unlink(missing_ok=True)
                raise
        except OSError as exc:
            self.last_error = type(exc).__name__
            logger.warning("runtime_state_write_failed category=%s", type(exc).__name__)
            return False
        self.last_error = ""
        return True

    @staticmethod
    def _clean_group_ids(value: object) -> tuple[str, ...]:
        if not isinstance(value, list):
            return ()
        cleaned = []
        for item in value:
            text = str(item).strip()
            # 与 DialogueEngine.enable_group 的校验保持一致。
            if text.isdigit() and 5 <= len(text) <= 12:
                cleaned.append(text)
        return tuple(sorted(set(cleaned)))

    @staticmethod
    def _clean_user_ids(value: object) -> tuple[str, ...]:
        """管理员名单只收纯数字 QQ 号；其它一律丢掉（状态文件是外部输入）。"""

        if not isinstance(value, list):
            return ()
        cleaned = []
        for item in value:
            text = str(item).strip()
            if text.isdigit() and 5 <= len(text) <= 12:
                cleaned.append(text)
        return tuple(sorted(set(cleaned)))
