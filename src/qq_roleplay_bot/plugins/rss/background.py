"""后台节拍：每一轮看一遍订阅，把**该推的那几条**推出去。

形状照 `plugins/remind/background.py` 与 `plugins/timeout_alert/background.py`
（`name` / `interval_seconds` / `enabled` / `poll_once()` / 可选 `close()`），
接着的是核心**唯一那一份**节拍循环（`background_plugins.run_background_plugin`）：
本插件不新开循环、不碰 `runtime.serve`。

## 一轮做什么（顺序就是护栏的顺序）

1. 读订阅文件（每轮现读 → 加一个源不用重启）；
2. 每条订阅：到点了吗（`store.due`，连续失败会被推后）→ 抓 → 解析；
3. **没见过这个源 → 只建基线，一条都不推**（首次订阅绝不补历史积压）；
4. 挑出"不在 id 集合里"的新条目，按**时间从旧到新**排序；
5. 这一轮最多 `min(每轮上限, 这一小时还剩的名额)` 条；
6. 逐条投递（群里走注入的 `notify`、邮箱走 `registry.mail`），投完记 id；
7. 写回 `seen.json`（一轮一次，不是一条一次）。

## 两个投递口都可能缺席（都不许因此把插件弄坏）

- `notify`：核心注入（`registry.notify`）。**发出去之后由核心记进会话历史**
  （`speaker="notice"`，用户 2026-10-19：「哎要不还是通知吧，不要当人了」）——
  本插件不知道"历史"长什么样，也不该知道。
- `registry.mail.notify(subject, body)`：邮件插件提供的能力。取不到（没装 mail、
  或 mail 没配收件人）就**跳过邮件那一路**，群里那一路照常，一行 warning 说清楚。

## 为什么不抛异常

`poll_once()` 是节拍的约定（不许抛出）：一个源坏了、一个群号写错了、邮件发不出去，
都只让**那一条**失败，下一轮照跑。
"""

from __future__ import annotations

import logging
import time

from . import config, feed as feed_module, fetch as fetch_module, text as text_module
from .store import RssStore, item_key

logger = logging.getLogger(__name__)

#: 群通知的抬头。**不写"系统/播报"这类词**：它要看起来像这个房间里的一句话。
GROUP_HEADER = "订阅更新 · {source}"


class RssPoller:
    """核心后台节拍认的形状。"""

    #: 节拍循环按这个名字记日志（`background_plugins.run_background_plugin`）。
    name = "rss"
    enabled = True

    def __init__(self, store: RssStore, *, notify=None, mail=None,
                 interval_seconds: float | None = None, clock=time.time,
                 feeds_loader=None, fetcher=None) -> None:
        self.store = store
        #: `async (target, text) -> 状态`：核心注入的通知口（群那一路）。
        self.notify = notify
        #: `() -> 可调用对象 | None`：邮件那一路的**懒读取**（mail 可能晚一步接上）。
        self.mail = mail
        self.interval_seconds = float(interval_seconds if interval_seconds is not None
                                      else config.interval_seconds())
        self.clock = clock
        #: 三个可注入的点（测试用；生产就是默认值）。
        self.feeds_loader = feeds_loader or (lambda: feed_module.load_feeds())
        self.fetcher = fetcher or fetch_module.fetch
        self._warned_no_feeds = False
        #: 这一轮做了什么的计数（观测用；节拍不读它）。
        self.stats = {"rounds": 0, "feeds": 0, "baseline": 0, "pushed": 0,
                      "failed": 0, "skipped_quota": 0}

    # --- 节拍入口 -----------------------------------------------------------

    async def poll_once(self) -> dict[str, int]:
        """跑一轮。**不许抛出**（节拍约定），自己兜一层并记一行。"""

        try:
            return await self._run_once()
        except Exception:  # noqa: BLE001 - 一轮失败不能停掉这条通道
            logger.exception("rss_poll_failed（这一轮跳过，下一次照跑）")
            return {}

    async def close(self) -> None:
        """收尾：没有要收的东西（状态每轮写完就落盘了）。"""

        return None

    # --- 一轮 ---------------------------------------------------------------

    async def _run_once(self) -> dict[str, int]:
        feeds = tuple(self.feeds_loader())
        self.stats["rounds"] += 1
        self.stats["feeds"] = len(feeds)
        if not feeds:
            if not self._warned_no_feeds:
                self._warned_no_feeds = True
                logger.info("rss：还没有订阅（%s 不存在或是空的），这一轮什么都不做",
                            self.store.path.parent / "feeds.json")
            return {"feeds": 0, "pushed": 0}

        summary = {"feeds": len(feeds), "baseline": 0, "pushed": 0, "failed": 0}
        now = self.clock()
        for spec in feeds:
            if not spec.enabled:
                continue
            if not self.store.due(spec.url, spec.interval(self.interval_seconds), now):
                continue
            document = self._fetch(spec)
            if document is None:
                summary["failed"] += 1
                continue
            if not self.store.known(spec.url):
                # **首次订阅只建基线**：一条都不推（用户明确要求）。
                self.store.set_baseline(
                    spec.url,
                    [item_key(item.guid, item.link, item.title) for item in document.items],
                    now,
                )
                summary["baseline"] += 1
                self.stats["baseline"] += 1
                logger.info("rss_baseline url=%s items=%s（只建基线，不推历史积压）",
                            spec.url, len(document.items))
                continue
            pushed = await self._push_new(spec, document, now)
            summary["pushed"] += pushed
            self.stats["pushed"] += pushed
            if pushed:
                # 一轮一次落盘：一条一条写会把"每轮最多 N 条"变成 N 次磁盘写。
                self.store.save()
        return summary

    def _fetch(self, spec):
        """抓 + 解析。失败只记一行并**降频**（连续失败→越来越久才再打一次）。"""

        try:
            document = self.fetcher(spec.url)
        except Exception as exc:  # noqa: BLE001 - 任何抓取失败都是"这一轮没有"
            wait = self.store.note_failure(spec.url, spec.interval(self.interval_seconds),
                                           self.clock())
            self.stats["failed"] += 1
            logger.warning("rss_fetch_failed url=%s category=%s next_attempt_in=%ss",
                           spec.url, type(exc).__name__,
                           max(0, int(wait - self.clock())))
            self.store.save()
            return None
        self.store.note_success(spec.url)
        return document

    async def _push_new(self, spec, document, now: float) -> int:
        """把这一轮该推的新条目推出去，返回推了几条。"""

        fresh = []
        for item in document.items:
            key = item_key(item.guid, item.link, item.title)
            if key and not self.store.seen(spec.url, key):
                fresh.append((item, key))
        if not fresh:
            return 0
        # **从旧到新**：先发生的事先说，别让一次抓到 3 条时顺序反着读。
        # 没有发布时间的排在后面（它们多半是"刚刚更新"的索引页条目）。
        fresh.sort(key=lambda pair: (pair[0].published
                                     if pair[0].published is not None else float("inf")))

        limit = min(spec.per_poll_limit(config.max_per_poll()),
                    self.store.hourly_remaining(spec.url, now))
        if limit <= 0:
            self.stats["skipped_quota"] += 1
            logger.info("rss_quota_exhausted url=%s pending=%s（下一小时再说）",
                        spec.url, len(fresh))
            return 0

        pushed = 0
        for item, key in fresh[:limit]:
            sent = await self._deliver(spec, document, item)
            # **投过就记 id**：哪怕这一条两个通道都失败了，也不反复重试同一条
            # （失败在日志里；重复推送比漏一条更烦人）。
            self.store.mark_seen(spec.url, key)
            if sent:
                self.store.note_pushed(spec.url, now)
                pushed += 1
        if len(fresh) > limit:
            logger.info("rss_deferred url=%s pending=%s", spec.url, len(fresh) - limit)
        return pushed

    async def _deliver(self, spec, document, item) -> bool:
        """投给这个订阅要的那几路。返回"至少有一路发出去了没有"。"""

        sent = False
        if spec.wants("group"):
            sent = await self._deliver_group(spec, document, item) or sent
        if spec.wants("mail"):
            sent = await self._deliver_mail(spec, document, item) or sent
        return sent

    async def _deliver_group(self, spec, document, item) -> bool:
        if not callable(self.notify):
            logger.warning("rss：核心没给通知口（registry.notify），群里那一路跳过")
            return False
        if not spec.group_id:
            logger.warning("rss：%s 没有 group_id，群里那一路跳过", spec.url)
            return False
        body = compose_group_text(document, item)
        try:
            # 目标用映射而不是核心的 `MessageTarget`：本插件不必 import 核心类型，
            # 核心那一边（`runtime._notice_target`）认这个形状。
            await self.notify({"group_id": spec.group_id}, body)
        except TypeError:
            # 最可能的一种：这个核心里 `notify` 还是**老形状**（只收正文，发给超管）。
            # 那一条"通知进历史"的核心改动就是本插件的**前置**——没合进来之前，
            # 群里这一路发不出去。**绝不退回 `notify(正文)`**：那会把订阅推给超管的
            # 私聊，投错地方比不投更糟（而且看不出是错的）。
            logger.warning(
                "rss_group_notify_failed reason=core_notify_takes_no_target "
                "（核心的 notify 还不接受目标：需要先合入「通知进历史」那一笔）url=%s",
                spec.url,
            )
            return False
        except Exception:  # noqa: BLE001 - 一个群发不出去不该带走别的源
            logger.warning("rss_group_notify_failed url=%s group=%s",
                           spec.url, spec.group_id, exc_info=True)
            return False
        return True

    async def _deliver_mail(self, spec, document, item) -> bool:
        sender = self.mail() if callable(self.mail) else None
        if not callable(sender):
            logger.info("rss：这次没有邮件能力（没 mail 插件、或它没配收件人），"
                        "邮件那一路跳过 url=%s", spec.url)
            return False
        subject, body = compose_mail(spec, document, item)
        try:
            await sender(subject, body)
        except Exception:  # noqa: BLE001 - 信发不出去只记一行
            logger.warning("rss_mail_failed url=%s", spec.url, exc_info=True)
            return False
        return True


def compose_group_text(document, item) -> str:
    """群里的那一条。**每一段都先洗过**（外部内容是不可信内容）。"""

    title = text_module.clean_title(item.title) or "（没有标题）"
    summary = text_module.clean_text(item.summary)
    link = text_module.safe_link(item.link)
    source = text_module.clean_title(document.title) or "订阅"
    lines = [GROUP_HEADER.format(source=source), title]
    if summary:
        lines.append(summary)
    if link:
        lines.append(link)
    return "\n".join(lines)


def compose_mail(spec, document, item) -> tuple[str, str]:
    """邮件那一封：主题短、正文把链接**原样**给出来（邮件里链接不该被洗掉）。"""

    source = text_module.clean_title(document.title) or "订阅"
    title = text_module.clean_title(item.title) or "（没有标题）"
    subject = f"{source} · {title}"[:120]
    parts = [title]
    summary = text_module.clean_text(item.summary)
    if summary:
        parts.append(summary)
    link = text_module.safe_link(item.link)
    if link:
        parts.append(link)
    parts.append(f"（来源：{text_module.safe_link(spec.url) or spec.url}）")
    return subject, "\n\n".join(parts)
