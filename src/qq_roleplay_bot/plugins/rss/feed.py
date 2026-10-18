"""订阅文件（`data/rss/feeds.json`）的形状与校验。

## 形状（脱敏示例——真实文件在 `data/rss/feeds.json`，**不进 git**）

```json
{
  "default_interval_seconds": 900,
  "max_per_poll": 3,
  "feeds": [
    {
      "url": "https://example.com/feed.xml",
      "enabled": true,
      "to": ["group", "mail"],
      "group_id": "800000001",
      "max_per_poll": 2
    },
    {
      "url": "https://another.example.org/atom.xml",
      "enabled": true,
      "to": ["mail"]
    }
  ]
}
```

`to` 是"投到哪儿"：`group`（群里）、`mail`（操作者邮箱），两个都写就是两个都要；
群目标**必须**额外给 `group_id`（不然这条订阅不知道发给哪个群，宁可整条跳过并在日志
里指名，也不猜）。

顶层那两个数字（`default_interval_seconds` / `max_per_poll`）是**这份文件里的默认值**，
每条订阅可以各自覆盖；文件里没写就退回环境变量那套（见 `config.py`）。
**每小时上限只在环境变量里**（`QQBOT_RSS_MAX_PER_HOUR`）：它是"防一口气补太多"的
护栏，不是某一条订阅的属性，写进订阅文件只会让人以为每条都能单独放松它。

## 校验的取舍：**坏一条只丢那一条**

一个订阅文件里只要有一条写错（少个 `group_id`、URL 不是 http(s)、`to` 拼错），
不能因此让整份订阅失效——那会让"加了一个源"变成"所有源都停了"。
所以：错的那条记一行 warning、**跳过**，其余照跑。整份文件读不出来（JSON 坏了）时
退到"这次没有订阅"，也记一行。
"""

from __future__ import annotations

import json
import logging
import pathlib
import urllib.parse
from dataclasses import dataclass

from . import config

logger = logging.getLogger(__name__)

#: 允许的投递目标。
TARGETS = ("group", "mail")

#: 一个群的群号至少这么长（QQ 群号实测都是 6 位以上；这不是安全判据，
#: 只是"写错了就别发"的粗筛）。
MIN_GROUP_ID_LENGTH = 5


@dataclass(frozen=True, slots=True)
class FeedSpec:
    """一条订阅。

    `max_per_poll` / `interval_seconds` 留 `None` 表示"跟文件里/环境里的默认值走"——
    不用 0 当哨兵（0 是合法的"不推"吗？不是，所以别让它有第二个含义）。
    """

    url: str
    to: tuple[str, ...] = ("group",)
    group_id: str = ""
    enabled: bool = True
    max_per_poll: int | None = None
    interval_seconds: float | None = None

    def wants(self, target: str) -> bool:
        return target in self.to

    def per_poll_limit(self, default: int) -> int:
        return self.max_per_poll if self.max_per_poll is not None else default

    def interval(self, default: float) -> float:
        return self.interval_seconds if self.interval_seconds is not None else default


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _http_url(value: object) -> str:
    """只认 http/https 的绝对地址（`file://` / `javascript:` 一律不算）。"""

    if not isinstance(value, str):
        return ""
    text = value.strip()
    if not text:
        return ""
    parsed = urllib.parse.urlparse(text)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
        return ""
    return text


def _targets(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return ()
    picked: list[str] = []
    for item in value:
        name = str(item).strip().lower()
        if name and name in TARGETS and name not in picked:
            picked.append(name)
    return tuple(picked)


def parse_feed(raw: object, *, default_interval: float, default_max_per_poll: int) -> FeedSpec | None:
    """一条订阅 → `FeedSpec`；写错了就返回 `None`（调用方记日志、跳过这一条）。

    `default_*` 是这份文件的顶层默认值（文件里没写、那就由 `load_feeds` 传环境变量
    那套进来）——所以返回的 `FeedSpec` 两个数字**总是有值**，`None` 只留给
    程序里直接构造的用法。
    """

    if not isinstance(raw, dict):
        return None
    url = _http_url(raw.get("url"))
    if not url:
        logger.warning("rss：订阅条目没有可用的 http(s) URL，跳过这一条")
        return None
    to = _targets(raw.get("to", ["group"]))
    if not to:
        logger.warning("rss：%s 的 to 里没有一个认得的投递目标（%s），跳过这一条",
                       url, "、".join(TARGETS))
        return None
    group_id = str(raw.get("group_id", "") or "").strip()
    if "group" in to and not (group_id.isdigit() and len(group_id) >= MIN_GROUP_ID_LENGTH):
        logger.warning("rss：%s 要投到群里，但 group_id=%r 不像一个群号，跳过这一条",
                       url, group_id)
        return None
    enabled = raw.get("enabled", True)
    per_poll = _number(raw.get("max_per_poll"))
    interval = _number(raw.get("interval_seconds"))
    return FeedSpec(
        url=url,
        to=to,
        group_id=group_id,
        enabled=bool(enabled),
        max_per_poll=max(1, int(per_poll)) if per_poll else int(default_max_per_poll),
        interval_seconds=(max(config.MIN_INTERVAL_SECONDS, interval) if interval
                          else float(default_interval)),
    )


def load_feeds(path: pathlib.Path | None = None) -> tuple[FeedSpec, ...]:
    """读订阅文件。没有文件 = 没有订阅（不是错误，第一次装就是这样）。"""

    target = pathlib.Path(path) if path is not None else config.feeds_path()
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ()
    except OSError as exc:
        logger.warning("rss：订阅文件读不出来（%s），这次当作没有订阅", type(exc).__name__)
        return ()
    try:
        document = json.loads(text)
    except ValueError:
        logger.warning("rss：订阅文件不是合法的 JSON（%s），这次当作没有订阅", target.name)
        return ()
    if not isinstance(document, dict):
        logger.warning("rss：订阅文件的顶层必须是对象，这次当作没有订阅")
        return ()

    default_interval = _number(document.get("default_interval_seconds"))
    default_interval = (max(config.MIN_INTERVAL_SECONDS, default_interval)
                        if default_interval else config.interval_seconds())
    default_per_poll = _number(document.get("max_per_poll"))
    default_per_poll = int(default_per_poll) if default_per_poll else config.max_per_poll()

    entries = document.get("feeds")
    if not isinstance(entries, list):
        logger.warning("rss：订阅文件里没有 feeds 数组，这次当作没有订阅")
        return ()

    feeds: list[FeedSpec] = []
    seen_urls: set[str] = set()
    for raw in entries:
        spec = parse_feed(raw, default_interval=default_interval,
                          default_max_per_poll=default_per_poll)
        if spec is None:
            continue
        if spec.url in seen_urls:
            logger.warning("rss：%s 写了不止一次，后面那次忽略", spec.url)
            continue
        seen_urls.add(spec.url)
        feeds.append(spec)
    return tuple(feeds)
