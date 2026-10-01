"""面板写的运行配置覆盖层：`data/operator_config.json`。

由来（2026-10-01 用户）："面板权限除了不能动代码以外权限跟你是一致的"，
其中包含"可以修改 api key、可以切换供应商"。这些改动必须**落盘**才有意义，
而且**不能写 `.env`**（那里有真实凭据、注释与顺序，脚本改一次就可能写坏）。

所以新增一层，优先级最高：

    operator_config.json（只由面板写） > 真实环境变量 > .env > 代码默认

实现上刻意**不新造一套配置读取**：`dev_config` 用 `get()` 统一走 `os.environ`，
（`load_env_file()` 也是把 `.env` 的值填进 `os.environ`）。那么在 `dev_config` 被
import 之前，把覆盖层也填进 `os.environ` 就自动获得最高优先级——一处生效，不散落。

**绝不覆盖真实环境变量**：与 `load_env_file` 同一条纪律。显式设过的环境变量是
"这次运行的特殊要求"（测试、干跑、临时切换），面板的持久覆盖不该把它顶掉。

损坏时**整份忽略**并 WARNING，回落到 `.env`：一份写坏的 JSON 不该让 bot 起不来。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

STORE_VERSION = 1

#: 允许面板写进覆盖层的键。**白名单**：不在表里的键一律拒绝，
#: 免得"面板能改任何环境变量"变成一条悄悄开出来的后门。
#:
#: 每项含义：
#:  - `live`：改完下一次调用就读到新值（不需要重启）；
#:  - `restart`：落盘了，但要重启才生效（面板必须如实标注，不许假装已生效）；
#:  - `text`：长文本（prompt 之类），面板里有独立的编辑器，不进归一化处理。
SETTINGS: dict[str, dict[str, object]] = {
    # --- agent 开关（全部 live：调用点每次现读） ---
    "judge_enabled": {"kind": "bool", "applies": "live", "env": "QQBOT_JUDGE_ENABLED"},
    "review_enabled": {"kind": "bool", "applies": "live", "env": "QQBOT_STYLE_REVIEW"},
    "vision_enabled": {"kind": "bool", "applies": "live", "env": "QQBOT_VISION"},
    "memory_enabled": {"kind": "bool", "applies": "live", "env": "QQBOT_MEMORY_ENABLED"},
    "compaction_enabled": {"kind": "bool", "applies": "live", "env": "QQBOT_COMPACTION"},
    "letter_enabled": {"kind": "bool", "applies": "live", "env": "QQBOT_MAIL_REPORT"},
    "group_manage_enabled": {"kind": "bool", "applies": "live", "env": "QQBOT_GROUP_MANAGE"},
    "group_owner_enabled": {"kind": "bool", "applies": "live", "env": "QQBOT_GROUP_OWNER"},
    # --- 模型与供应商（live：client 是就地改属性的） ---
    "provider": {"kind": "str", "applies": "live", "env": "QQBOT_PROVIDER"},
    "api_base_url": {"kind": "str", "applies": "live", "env": "QQBOT_API_BASE_URL"},
    "api_model": {"kind": "str", "applies": "live", "env": "QQBOT_API_MODEL"},
    "memory_model": {"kind": "str", "applies": "live", "env": "QQBOT_MEMORY_MODEL"},
    # --- 凭据（live：同上；值**绝不回显**，见 webui_data.mask_settings） ---
    "api_key": {"kind": "secret", "applies": "live", "env": "QQBOT_API_KEY"},
    "judge_api_key": {"kind": "secret", "applies": "live", "env": "QQBOT_JUDGE_API_KEY"},
    "memory_api_key": {"kind": "secret", "applies": "live", "env": "QQBOT_MEMORY_API_KEY"},
    "review_api_key": {"kind": "secret", "applies": "live", "env": "QQBOT_REVIEW_API_KEY"},
    # --- 审批策略（restart：策略对象在装配时构造） ---
    "auto_approve_join": {"kind": "bool", "applies": "restart", "env": "QQBOT_AUTO_APPROVE_JOIN"},
    "approve_whitelist": {"kind": "csv", "applies": "restart", "env": "QQBOT_APPROVE_WHITELIST"},
    "approve_blacklist": {"kind": "csv", "applies": "restart", "env": "QQBOT_APPROVE_BLACKLIST"},
    "approve_pattern": {"kind": "str", "applies": "restart", "env": "QQBOT_APPROVE_PATTERN"},
    "approve_reject_reason": {"kind": "str", "applies": "restart",
                              "env": "QQBOT_APPROVE_REJECT_REASON"},
}

BOOL_TRUE = {"1", "true", "yes", "on"}


class ConfigRejected(ValueError):
    """面板提交的键或值不合法。消息是给人看的固定说法，不带内部细节。"""


def default_path() -> Path:
    base = os.environ.get("QQBOT_DATA_DIR", "").strip()
    root = Path(base).resolve() if base else Path(__file__).resolve().parents[2] / "data"
    return root / "operator_config.json"


def normalize(key: str, value: object) -> str:
    """把面板传来的值归一化成字符串（落盘与环境变量都只存字符串）。"""

    spec = SETTINGS.get(str(key))
    if spec is None:
        raise ConfigRejected(f"不认识的配置项：{key}")
    kind = str(spec["kind"])
    if kind == "bool":
        if isinstance(value, bool):
            return "1" if value else "0"
        text = str(value).strip().casefold()
        if text in BOOL_TRUE:
            return "1"
        if text in {"0", "false", "no", "off", ""}:
            return "0"
        raise ConfigRejected(f"{key} 只接受 true/false")
    if kind == "csv":
        items = value if isinstance(value, (list, tuple)) else str(value or "").split(",")
        cleaned = [str(item).strip() for item in items if str(item).strip()]
        return ",".join(cleaned)
    text = str(value or "").strip()
    if kind == "secret" and not text:
        # 空值 = "清掉这一项，回落到主 key / .env"。这是有意义的操作，不是错误。
        return ""
    return text


def applies(key: str) -> str:
    spec = SETTINGS.get(str(key))
    return str(spec["applies"]) if spec else "restart"


def env_name(key: str) -> str:
    spec = SETTINGS.get(str(key))
    return str(spec["env"]) if spec else ""


class OperatorConfig:
    """覆盖层的读写。`path=None` 时用 `data/operator_config.json`。"""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else default_path()
        self.values: dict[str, str] = {}
        self.last_error = ""

    # --- 读 ---------------------------------------------------------------

    def load(self) -> dict[str, str]:
        """读覆盖层。文件缺失/损坏/版本不符 → 空 dict（并记原因）。"""

        self.values = {}
        self.last_error = ""
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError as exc:
            self.last_error = type(exc).__name__
            logger.warning("operator_config_read_failed category=%s", type(exc).__name__)
            return {}
        try:
            data = json.loads(raw)
        except ValueError:
            self.last_error = "invalid_json"
            logger.warning("operator_config_invalid_json；整份忽略，回落到 .env")
            return {}
        if not isinstance(data, dict) or data.get("version") != STORE_VERSION:
            self.last_error = "unsupported_version"
            logger.warning("operator_config_unsupported_version；整份忽略，回落到 .env")
            return {}
        stored = data.get("values")
        if isinstance(stored, dict):
            for key, value in stored.items():
                name = str(key)
                if name not in SETTINGS or not isinstance(value, (str, int, float, bool)):
                    continue
                text = str(value)
                spec = SETTINGS[name]
                if not text and spec["kind"] == "secret":
                    # **空值不是覆盖**。凭据类留空表示"回落 `.env`"，所以文件里那个空串
                    # 必须当作"没有这一项"被丢掉。
                    #
                    # 2026-10-01 现场事故：这条原来是照单全收，于是 `inject()` 把
                    # `QQBOT_API_KEY=""` 写进了环境；紧接着 `load_env_file()` 按
                    # "不覆盖已存在的键"的纪律看到它已经存在，**就不再填 `.env` 里那把真 key**。
                    # 结果回复 agent 的 key 变成空串，模型一律 401——而她只是"不说话"，
                    # 命令（不走模型）照常响应，很难一眼看出是配置问题。
                    # 启动时自愈：忽略它，`.env` 那把照旧生效。
                    logger.warning("operator_config_empty_secret_ignored key=%s", name)
                    continue
                self.values[name] = text
        return dict(self.values)

    def inject(self) -> int:
        """把覆盖层填进 `os.environ`（**不覆盖真实环境变量**）。返回注入键数。

        必须在 `dev_config` 被 import 之前调用一次——`dev_config` 的所有常量都在
        import 时求值。见模块头"一处生效"的说明。
        """

        self.load()
        injected = 0
        for key, value in self.values.items():
            name = env_name(key)
            if not name or name in os.environ:
                continue
            os.environ[name] = value
            injected += 1
        if injected:
            logger.info("运行配置覆盖层已生效：%s 项（%s）", injected, self.path.name)
        return injected

    # --- 写 ---------------------------------------------------------------

    def set(self, key: str, value: object) -> str:
        """归一化并落盘，返回归一化后的值。不负责"让它生效"（那是核心接缝的事）。"""

        normalized = normalize(key, value)
        self.values[str(key)] = normalized
        self.save()
        return normalized

    def save(self) -> bool:
        body = {"version": STORE_VERSION, "values": dict(sorted(self.values.items()))}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle = tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=str(self.path.parent),
                prefix=self.path.name + ".", suffix=".tmp", delete=False,
            )
            try:
                with handle:
                    json.dump(body, handle, ensure_ascii=False, indent=2)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(handle.name, self.path)
            except BaseException:
                Path(handle.name).unlink(missing_ok=True)
                raise
        except OSError as exc:
            self.last_error = type(exc).__name__
            logger.warning("operator_config_write_failed category=%s", type(exc).__name__)
            return False
        self._restrict()
        self.last_error = ""
        return True

    def _restrict(self) -> None:
        """收紧权限：这份文件可能含 API Key。Windows 上 chmod 只是尽力而为。"""

        try:
            os.chmod(self.path, 0o600)
        except OSError:  # pragma: no cover - 平台差异，权限收紧失败不影响功能
            pass

    def stats(self) -> dict[str, object]:
        return {"path": str(self.path), "keys": sorted(self.values),
                "last_error": self.last_error}


def inject() -> int:
    """把覆盖层填进 `os.environ`。返回注入键数。

    **注入顺序是这里唯一需要小心的地方**：必须在 `load_env_file()` 之前调用——
    `.env` 的值写进 `os.environ` 之后就不再被覆盖（两处都是"不覆盖已存在的 key"
    这条纪律），所以面板的覆盖要抢在它前面才拿得到最高优先级。

    测试隔离：`QQBOT_STATE_PERSIST=0` 时直接返回 0。离线测试入口用这个变量标志
    "运行状态不落盘"，而面板的配置文件是 deployment 级的东西——测试绝不该读到
    真实的那一份（`data/` 在测试里本来就是隔离的，这里再加一道）。
    """

    if os.environ.get("QQBOT_STATE_PERSIST", "").strip().lower() in {"0", "false", "off", "no"}:
        return 0
    config = shared()
    return config.inject()


def shared() -> OperatorConfig:
    """进程内共用的一份（`webui_panel` 与 `runtime` 拿同一个对象）。"""

    global _SHARED
    if _SHARED is None:
        _SHARED = OperatorConfig()
    return _SHARED


_SHARED: OperatorConfig | None = None
