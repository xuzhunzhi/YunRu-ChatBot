"""去重、每小时额度、失败降频——插件自己的那份状态（`data/rss/seen.json`，不进 git）。

## 三个数各是什么

| 字段 | 意思 |
| --- | --- |
| `ids` | 这个源**最近 500 条**看过的条目 id（哈希后的短串，新的在后） |
| `baseline` | 有没有建过基线。**没有 → 这一轮只建基线、一条都不推** |
| `failures` / `next_attempt_at` | 连续失败几次、下一次最早什么时候再打这个站 |

外加一份每小时的账：`hour_window_start` / `hour_count`（窗口 1 小时，过了就重开）。

## 为什么 id 要哈希

`guid` / `link` 里可能有很长的 query（追踪参数）、也可能带订阅源自己的私货。
只留 `sha256(...)[:16]`：去重只需要"等不等"，不需要原文。

## 首次订阅只建基线（用户明确要求）

一个已经积了 3000 条旧文的源，刚订上就往外推 3000 条不是功能，是事故。
判据是"这个 URL 在 `seen.json` 里**从来没有出现过**"——那一刻只把当前条目的 id
全记下来（最多 500 条），`baseline=true`，然后 `continue`。下一轮起，只有**不在
这份 id 集合里**的条目才算"新的"。

⚠️ 反过来的坑也堵了：`baseline` 记着、但当时那个源**一条都没有**（空 feed）——下一轮
来了 1 条，它就是"新的"，该推。所以判据不能写成"baseline 存在就永远不推"。

## 失败降频（不要每轮都去打同一个死站）

第 n 次连续失败 → 下次最早在 `now + interval × 2^(n-1)` 之后（`interval` 是那个源的
节拍），最长不超过 `config.backoff_cap_seconds()`（默认 6 小时）。成功一次就清零。
"""

from __future__ import annotations

import hashlib
import json
import logging
import pathlib
import time
from dataclasses import dataclass, field

from . import config

logger = logging.getLogger(__name__)

#: `seen.json` 的版本号（将来改形状时用来判"要不要丢弃重来"）。
STORE_VERSION = 1


def item_key(guid: str, link: str = "", title: str = "") -> str:
    """去重键：`guid` 优先，没有就用链接，再没有就用标题。"""

    raw = (guid or link or title or "").strip()
    if not raw:
        return ""
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:16]


@dataclass
class FeedState:
    """一个源的状态（整份写回 `seen.json`）。"""

    ids: list[str] = field(default_factory=list)
    baseline_at: float = 0.0
    failures: int = 0
    next_attempt_at: float = 0.0
    hour_window_start: float = 0.0
    hour_count: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "ids": list(self.ids),
            "baseline_at": self.baseline_at,
            "failures": self.failures,
            "next_attempt_at": self.next_attempt_at,
            "hour_window_start": self.hour_window_start,
            "hour_count": self.hour_count,
        }

    @classmethod
    def from_dict(cls, raw: object) -> "FeedState":
        if not isinstance(raw, dict):
            return cls()
        ids = raw.get("ids")
        return cls(
            ids=[str(item) for item in ids if isinstance(item, str)][-config.SEEN_LIMIT:]
            if isinstance(ids, list) else [],
            baseline_at=_number(raw.get("baseline_at")),
            failures=int(_number(raw.get("failures"))),
            next_attempt_at=_number(raw.get("next_attempt_at")),
            hour_window_start=_number(raw.get("hour_window_start")),
            hour_count=int(_number(raw.get("hour_count"))),
        )


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value)


class RssStore:
    """`seen.json` 的读写 + 三个判定。**坏文件只当"没有状态"，不许让节拍停**。"""

    def __init__(self, path: pathlib.Path | None = None, *, clock=time.time) -> None:
        self.path = pathlib.Path(path) if path is not None else config.seen_path()
        self.clock = clock
        self._feeds: dict[str, FeedState] = {}
        self._loaded = False

    # --- 读写 ---------------------------------------------------------------

    def load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            logger.warning("rss：seen.json 读不出来（%s），这次当作没有状态",
                           type(exc).__name__)
            return
        if not isinstance(document, dict):
            return
        if document.get("version") not in (None, STORE_VERSION):
            logger.warning("rss：seen.json 的版本号认不出（%r），这次当作没有状态",
                           document.get("version"))
            return
        feeds = document.get("feeds")
        if not isinstance(feeds, dict):
            return
        self._feeds = {str(url): FeedState.from_dict(raw) for url, raw in feeds.items()}

    def save(self) -> None:
        """写回。**写失败只记一行**：状态丢了顶多重复推一次，不该让节拍崩。"""

        self.load()
        document = {
            "version": STORE_VERSION,
            "feeds": {url: state.as_dict() for url, state in self._feeds.items()},
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n",
                                 encoding="utf-8")
        except OSError as exc:
            logger.warning("rss：seen.json 写不进去（%s）", type(exc).__name__)

    def state(self, url: str) -> FeedState:
        self.load()
        state = self._feeds.get(url)
        if state is None:
            state = FeedState()
            self._feeds[url] = state
        return state

    def known(self, url: str) -> bool:
        """这个源**建过基线没有**（= 以前真的见过）。

        判据是 `baseline_at > 0`，**不是**"这个 URL 在字典里"：`due()` / `hourly_remaining()`
        这些查询会顺手把条目建出来（空状态），拿"在字典里"当判据，一个刚订上的源就会
        被当成"见过"——于是第一次抓取把 3000 条旧文全推出去。这个坑写在这里，
        别再拆开这两件事。
        """

        self.load()
        state = self._feeds.get(url)
        return bool(state is not None and state.baseline_at > 0)

    # --- 去重 ---------------------------------------------------------------

    def seen(self, url: str, key: str) -> bool:
        return key in self.state(url).ids

    def mark_seen(self, url: str, key: str) -> None:
        if not key:
            return
        state = self.state(url)
        if key in state.ids:
            return
        state.ids.append(key)
        # 只留最近 `SEEN_LIMIT` 条（新的在后；超出时淘汰最旧的）。
        if len(state.ids) > config.SEEN_LIMIT:
            del state.ids[:len(state.ids) - config.SEEN_LIMIT]

    def set_baseline(self, url: str, keys: list[str], now: float | None = None) -> None:
        """建基线：把当前条目的 id 记下来，**一条都不推**。"""

        state = self.state(url)
        state.ids = []
        for key in keys:
            self.mark_seen(url, key)
        state.baseline_at = float(now if now is not None else self.clock())
        state.failures = 0
        state.next_attempt_at = 0.0

    # --- 额度 ---------------------------------------------------------------

    def hourly_remaining(self, url: str, now: float | None = None) -> int:
        """这一小时还剩几条名额。窗口过了就重开。"""

        moment = float(now if now is not None else self.clock())
        state = self.state(url)
        if moment - state.hour_window_start >= config.HOUR_SECONDS:
            state.hour_window_start = moment
            state.hour_count = 0
        return max(0, config.max_per_hour() - state.hour_count)

    def note_pushed(self, url: str, now: float | None = None) -> None:
        moment = float(now if now is not None else self.clock())
        state = self.state(url)
        self.hourly_remaining(url, moment)   # 顺手把窗口对齐
        state.hour_count += 1

    # --- 降频 ---------------------------------------------------------------

    def due(self, url: str, interval: float, now: float | None = None) -> bool:
        """到点了没有（连续失败的源在这段时间里**不许再打**）。"""

        moment = float(now if now is not None else self.clock())
        return moment >= self.state(url).next_attempt_at

    def note_success(self, url: str) -> None:
        state = self.state(url)
        state.failures = 0
        state.next_attempt_at = 0.0

    def note_failure(self, url: str, interval: float, now: float | None = None) -> float:
        """记一次失败，返回"下一次最早什么时候再打"（也写进状态）。"""

        moment = float(now if now is not None else self.clock())
        state = self.state(url)
        state.failures = min(30, state.failures + 1)
        # 1 次 → 1×interval，2 次 → 2×，3 次 → 4×……（指数退避，封顶在 cap）
        delay = float(interval) * (2 ** (state.failures - 1))
        delay = min(delay, config.backoff_cap_seconds())
        state.next_attempt_at = moment + delay
        return state.next_attempt_at
