"""订阅插件的配置：路径、节拍、额度、超时——**全是这个插件自己的**。

为什么不在核心的 `dev_config` 里（那段说明在包的 docstring 里，这里只留结论）：
插件线有一条机械门槛 `tests/check_plugin_branch_scope.py`，只允许改
`src/qq_roleplay_bot/plugins/**` 与白名单里的测试；`dev_config.py` 是核心文件，
往里塞订阅私货会当场越界。订阅列表本来就是插件的数据，不是部署的通用配置。

## 键与默认值（都能用环境变量改，给"临时调一下"留口子）

| 环境变量 | 默认 | 意思 |
| --- | --- | --- |
| `QQBOT_RSS_FEEDS` | `<data>/rss/feeds.json` | 订阅文件（不进 git） |
| `QQBOT_RSS_SEEN` | `<data>/rss/seen.json` | 去重 / 额度 / 降频的状态（不进 git） |
| `QQBOT_RSS_INTERVAL_SECONDS` | `900` | 默认多久看一轮（每源可覆盖） |
| `QQBOT_RSS_MAX_PER_POLL` | `3` | 每源每轮最多推几条（每源可覆盖） |
| `QQBOT_RSS_MAX_PER_HOUR` | `12` | 每源每小时最多推几条 |
| `QQBOT_RSS_BACKOFF_CAP_SECONDS` | `21600`（6 小时） | 连续失败时最长隔多久再打一次 |
| `QQBOT_RSS_TIMEOUT_SECONDS` | `10` | 单次抓取超时（交底里定的 10 秒） |
| `QQBOT_RSS_MAX_BYTES` | `2097152`（2MB） | 单次响应上限（交底里定的 2MB） |

**为什么节拍下限是 300 秒**：外面那些站不是我们的服务器。定太短等于替订阅源做压力
测试，而 RSS 的更新粒度本来就在分钟级。要更密只能在测试/调试里直接调 `poll_once()`。
"""

from __future__ import annotations

import logging
import os
import pathlib

logger = logging.getLogger(__name__)

#: 节拍下限：比这更密没有意义（见上面那段）。
MIN_INTERVAL_SECONDS = 300.0


def _data_root() -> pathlib.Path:
    """运行数据目录。**用核心那一个**（`dev_config.data_dir()`），不自己发明路径。"""

    try:
        from ...dev_config import data_dir

        return pathlib.Path(data_dir())
    except Exception:  # noqa: BLE001 - 极端情况下退回包根下的 data（测试替身会用到）
        logger.warning("rss：拿不到核心的 data_dir，退回包根下的 data/")
        return pathlib.Path(__file__).resolve().parents[2] / "data"


def _env_path(name: str, default: pathlib.Path) -> pathlib.Path:
    value = os.environ.get(name, "").strip()
    if not value:
        return default
    path = pathlib.Path(value)
    if not path.is_absolute():
        path = _data_root() / path
    return path


def _env_number(name: str, default: float, *, minimum: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return float(default)
    try:
        value = float(raw)
    except ValueError:
        logger.warning("rss：%s=%r 不是数字，按默认值 %s 处理", name, raw, default)
        return float(default)
    if value < minimum:
        logger.warning("rss：%s=%s 小于下限 %s，按下限处理", name, value, minimum)
        return float(minimum)
    return value


def feeds_path() -> pathlib.Path:
    """订阅文件（`data/rss/feeds.json`，不进 git）。"""

    return _env_path("QQBOT_RSS_FEEDS", _data_root() / "rss" / "feeds.json")


def seen_path() -> pathlib.Path:
    """去重与额度状态（`data/rss/seen.json`，不进 git）。"""

    return _env_path("QQBOT_RSS_SEEN", _data_root() / "rss" / "seen.json")


def interval_seconds() -> float:
    """默认多长时间看一轮。每源可以在订阅文件里覆盖。"""

    return _env_number("QQBOT_RSS_INTERVAL_SECONDS", 900.0, minimum=MIN_INTERVAL_SECONDS)


def max_per_poll() -> int:
    """每源每轮最多推几条（默认 3，交底里定的）。"""

    return int(_env_number("QQBOT_RSS_MAX_PER_POLL", 3.0, minimum=1.0))


def max_per_hour() -> int:
    """每源每小时最多推几条（防"一口气补 200 条"那种）。"""

    return int(_env_number("QQBOT_RSS_MAX_PER_HOUR", 12.0, minimum=1.0))


def backoff_cap_seconds() -> float:
    """连续失败时的最长间隔（再长就等于放弃了，但那一行 warning 仍在）。"""

    return _env_number("QQBOT_RSS_BACKOFF_CAP_SECONDS", 6 * 3600.0,
                       minimum=interval_seconds())


def timeout_seconds() -> float:
    """单次抓取超时（交底：10 秒）。"""

    return _env_number("QQBOT_RSS_TIMEOUT_SECONDS", 10.0, minimum=1.0)


def max_bytes() -> int:
    """单次响应上限（交底：2MB）。"""

    return int(_env_number("QQBOT_RSS_MAX_BYTES", 2 * 1024 * 1024.0, minimum=4096.0))


#: 每个源在 `seen.json` 里保留多少条"看过了"的 id。500 条足够覆盖绝大多数源
#: 两次抓取之间的新增量，又不至于把文件撑大（500 × 16 字节哈希 ≈ 8 KB/源）。
SEEN_LIMIT = 500

#: 每源每小时额度的窗口长度。
HOUR_SECONDS = 3600.0
