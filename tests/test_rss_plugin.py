"""RSS / Atom 订阅插件：解析、护栏、去重、额度、降频、投递、首次订阅只建基线。

**这个文件不碰外网**：抓取那一步由 `fetcher` / `opener` 打桩注入（生产用的是
`fetch.fetch` / `urllib.request.urlopen`）。这样"超时 / 太大 / 站点挂了"这些分支
都能测到，而一条真实请求都不会发出去。

口径来源（用户 2026-10-19 交底，逐条钉在这里，别自己改语义）：

- 只用标准库、**不新增依赖**；
- 抓取超时 10 秒、响应上限 2MB、连续失败要降频；
- 去重：`data/rss/seen.json` 每源最近 500 条 guid/link 的哈希；**首次订阅只建基线、
  绝不推送历史积压**；
- 频率：每源每轮最多 N 条（默认 3）、同一源每小时有上限；
- 投递：群用核心注入的 `notify(target, text)`（它就接上"记成通知"那一笔）；
  邮件用 `registry.mail.notify(subject, body)`，**没有 mail 就跳过、不报错**；
- 安全：RSS 正文是外部不可信内容——去掉 `[CQ:...]`、`@`、控制字符，截断到 200 字，
  链接只保留 http/https。
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import tempfile
import unittest
from unittest.mock import patch

from qq_roleplay_bot.plugins import PluginRegistry
from qq_roleplay_bot.plugins.rss import config
from qq_roleplay_bot.plugins.rss import fetch as fetch_module
from qq_roleplay_bot.plugins.rss import plugin as plugin_module
from qq_roleplay_bot.plugins.rss import text as text_module
from qq_roleplay_bot.plugins.rss.background import RssPoller, compose_group_text
from qq_roleplay_bot.plugins.rss.feed import FeedSpec, load_feeds
from qq_roleplay_bot.plugins.rss.store import RssStore, item_key

GROUP = "800000001"
FEED_URL = "https://example.com/feed"

RSS_2 = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <title>示例源</title>
  <item>
    <title>第一条</title>
    <link>https://example.com/a</link>
    <guid>guid-a</guid>
    <description>摘要一</description>
    <pubDate>Tue, 20 Oct 2026 10:00:00 +0800</pubDate>
  </item>
  <item>
    <title>第二条</title>
    <link>https://example.com/b</link>
    <guid>guid-b</guid>
    <description>摘要二</description>
  </item>
</channel></rss>
"""

ATOM = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Atom 源</title>
  <entry>
    <title>甲</title>
    <link rel="alternate" href="https://example.org/1"/>
    <id>urn:entry:1</id>
    <summary>摘要甲</summary>
    <updated>2026-10-20T02:00:00Z</updated>
  </entry>
</feed>
"""


def item(guid: str, *, title=None, summary="摘要", link="", published=None
         ) -> fetch_module.Item:
    return fetch_module.Item(
        guid=guid,
        title=title if title is not None else f"标题-{guid}",
        summary=summary,
        link=link or f"https://example.com/{guid}",
        published=published,
    )


def document_for(*items: fetch_module.Item, title="示例源") -> fetch_module.FeedDocument:
    return fetch_module.FeedDocument(title=title, items=tuple(items))


class FakeOpener:
    """`urllib.request.urlopen` 的替身：**记下超时**，返回一份可控的响应。"""

    def __init__(self, body: bytes = b"<rss/>", *, headers=None, raises=None) -> None:
        self.body = body
        self.headers = dict(headers or {})
        self.raises = raises
        self.timeouts: list[float] = []
        self.urls: list[str] = []

    def __call__(self, request, timeout=None):
        self.timeouts.append(timeout)
        self.urls.append(getattr(request, "full_url", str(request)))
        if self.raises is not None:
            raise self.raises
        opener = self

        class Response:
            headers = opener.headers

            def __enter__(self):
                return self

            def __exit__(self, *exc_info):
                return False

            def read(self, amt=None):
                return opener.body if amt is None else opener.body[:amt]

        return Response()


class TempStoreMixin(unittest.TestCase):
    """每个用例一份自己的 `seen.json` / `feeds.json`（系统临时目录，不碰 `data/`）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = pathlib.Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def store(self) -> RssStore:
        return RssStore(self.tmp / "rss" / "seen.json")

    def poller(self, store: RssStore, items=(), *, notify=None, mail=None,
               per_poll=3, url=FEED_URL, interval=300.0, clock=None) -> RssPoller:
        """一个什么都打桩好的 poller：抓取只返回 `items`，时钟固定。"""

        return RssPoller(
            store,
            notify=notify,
            mail=mail or (lambda: None),
            interval_seconds=interval,
            clock=clock or (lambda: 1000.0),
            feeds_loader=lambda: (FeedSpec(url=url, to=("group", "mail"), group_id=GROUP,
                                           max_per_poll=per_poll),),
            fetcher=lambda _url: document_for(*items),
        )


# ---------------------------------------------------------------------------
# 1. 解析：RSS 2.0 与 Atom，认不出就不猜
# ---------------------------------------------------------------------------


class ParseTests(unittest.TestCase):
    def test_rss_2_0_items_are_parsed(self) -> None:
        document = fetch_module.parse(RSS_2)
        self.assertEqual(document.title, "示例源")
        self.assertEqual([entry.guid for entry in document.items], ["guid-a", "guid-b"])
        first = document.items[0]
        self.assertEqual(first.title, "第一条")
        self.assertEqual(first.link, "https://example.com/a")
        self.assertEqual(first.summary, "摘要一")
        self.assertIsNotNone(first.published)
        self.assertIsNone(document.items[1].published)

    def test_atom_items_are_parsed_with_the_href_link(self) -> None:
        document = fetch_module.parse(ATOM)
        self.assertEqual(document.title, "Atom 源")
        self.assertEqual(len(document.items), 1)
        entry = document.items[0]
        self.assertEqual(entry.guid, "urn:entry:1")
        self.assertEqual(entry.link, "https://example.org/1")
        self.assertEqual(entry.title, "甲")
        self.assertIsNotNone(entry.published)

    def test_an_unknown_dialect_is_not_guessed(self) -> None:
        """回一段 HTML 错误页的站很常见：认不出 `<item>`/`<entry>` 就当没有条目。"""

        self.assertEqual(fetch_module.parse("<html><body>502</body></html>").items, ())

    def test_broken_xml_is_an_error(self) -> None:
        with self.assertRaises(fetch_module.FetchError):
            fetch_module.parse("<rss><channel>")


# ---------------------------------------------------------------------------
# 2. 抓取护栏：http(s) / 超时 / 2MB
# ---------------------------------------------------------------------------


class FetchGuardTests(unittest.TestCase):
    def test_only_http_and_https_are_fetched(self) -> None:
        for url in ("file:///etc/passwd", "javascript:alert(1)", "ftp://x/y", ""):
            with self.assertRaises(fetch_module.FetchError):
                fetch_module.fetch_xml(url, opener=FakeOpener())

    def test_the_timeout_is_passed_through(self) -> None:
        opener = FakeOpener(b"<rss/>")
        fetch_module.fetch_xml(FEED_URL, opener=opener, timeout=10.0)
        self.assertEqual(opener.timeouts, [10.0])

    def test_a_body_over_the_cap_is_rejected(self) -> None:
        opener = FakeOpener(b"x" * 5000)
        with self.assertRaises(fetch_module.FetchError):
            fetch_module.fetch_xml(FEED_URL, opener=opener, max_bytes=4096)

    def test_a_declared_length_over_the_cap_is_rejected_before_reading(self) -> None:
        opener = FakeOpener(b"<rss/>", headers={"Content-Length": "9999999"})
        with self.assertRaises(fetch_module.FetchError):
            fetch_module.fetch_xml(FEED_URL, opener=opener, max_bytes=4096)

    def test_a_network_error_becomes_a_fetch_error(self) -> None:
        opener = FakeOpener(raises=OSError("连不上"))
        with self.assertRaises(fetch_module.FetchError):
            fetch_module.fetch_xml(FEED_URL, opener=opener)

    def test_the_documented_defaults_are_10_seconds_and_2mb(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("QQBOT_RSS_TIMEOUT_SECONDS", None)
            os.environ.pop("QQBOT_RSS_MAX_BYTES", None)
            self.assertEqual(config.timeout_seconds(), 10.0)
            self.assertEqual(config.max_bytes(), 2 * 1024 * 1024)


# ---------------------------------------------------------------------------
# 3. 投递：群走 notify、邮件走 mail、缺一路不许弄坏另一路
# ---------------------------------------------------------------------------


class DeliveryTests(TempStoreMixin):
    def test_the_group_notice_goes_through_the_injected_notify(self) -> None:
        seen: list[tuple[object, str]] = []

        async def notify(target, text):
            seen.append((target, text))

        store = self.store()
        asyncio.run(self.poller(store, (item("g1"),), notify=notify).poll_once())  # 基线
        poller = self.poller(store, (item("g1"), item("g2")), notify=notify)
        asyncio.run(poller.poll_once())

        self.assertTrue(seen, "新条目必须经过 notify 投出去")
        target, text = seen[-1]
        self.assertEqual(target, {"group_id": GROUP})
        self.assertIn("标题-g2", text)
        # 抬头不写"系统/播报"这类词：它要像这个房间里的一句话。
        self.assertIn("订阅更新", text)

    def test_mail_goes_through_the_mail_seam(self) -> None:
        mails: list[tuple[str, str]] = []

        async def mail_notify(subject, body):
            mails.append((subject, body))

        store = self.store()
        asyncio.run(self.poller(store, (item("g1"),)).poll_once())               # 基线
        poller = self.poller(store, (item("g1"), item("g2")), mail=lambda: mail_notify)
        asyncio.run(poller.poll_once())
        self.assertEqual(len(mails), 1, mails)
        subject, body = mails[0]
        self.assertIn("标题-g2", subject)
        self.assertIn("https://example.com/g2", body)

    def test_without_mail_the_group_route_still_works(self) -> None:
        seen: list[str] = []

        async def notify(target, text):
            seen.append(text)

        store = self.store()
        asyncio.run(self.poller(store, (item("g1"),), notify=notify).poll_once())
        poller = self.poller(store, (item("g1"), item("g2")), notify=notify,
                             mail=lambda: None)     # 没有 mail 插件
        summary = asyncio.run(poller.poll_once())
        self.assertEqual(summary["pushed"], 1)
        self.assertEqual(len(seen), 1, "邮件那一路缺席不许把群里这一路带走")

    def test_a_failing_notify_does_not_kill_the_round(self) -> None:
        async def notify(target, text):
            raise RuntimeError("对面拒收")

        store = self.store()
        asyncio.run(self.poller(store, (item("g1"),), notify=notify).poll_once())
        poller = self.poller(store, (item("g1"), item("g2")), notify=notify)
        summary = asyncio.run(poller.poll_once())
        self.assertEqual(summary["pushed"], 0)
        # 投过就记 id：两个通道都失败也不反复重试同一条。
        self.assertTrue(store.seen(FEED_URL, item_key("g2")))


# ---------------------------------------------------------------------------
# 4. 首次订阅只建基线 / 去重 / 额度
# ---------------------------------------------------------------------------


class BaselineAndQuotaTests(TempStoreMixin):
    def test_the_first_poll_only_builds_a_baseline(self) -> None:
        """**只建基线、绝不推送历史积压**（用户明确要求）。"""

        delivered: list[str] = []

        async def notify(target, text):
            delivered.append(text)

        store = self.store()
        poller = self.poller(store, (item("g1"), item("g2"), item("g3")), notify=notify)
        summary = asyncio.run(poller.poll_once())

        self.assertEqual(summary["baseline"], 1)
        self.assertEqual(delivered, [], "首次订阅一条都不许推")
        self.assertTrue(store.known(FEED_URL))
        for guid in ("g1", "g2", "g3"):
            self.assertTrue(store.seen(FEED_URL, item_key(guid)), "基线要把当前条目全记下")

    def test_only_items_after_the_baseline_are_pushed(self) -> None:
        delivered: list[str] = []

        async def notify(target, text):
            delivered.append(text)

        store = self.store()
        asyncio.run(self.poller(store, (item("g1"), item("g2")), notify=notify).poll_once())
        poller = self.poller(store, (item("g1"), item("g2"), item("g3")), notify=notify)
        asyncio.run(poller.poll_once())
        self.assertEqual(len(delivered), 1)
        self.assertIn("标题-g3", delivered[0])

    def test_an_item_is_never_pushed_twice(self) -> None:
        delivered: list[str] = []

        async def notify(target, text):
            delivered.append(text)

        store = self.store()
        asyncio.run(self.poller(store, (item("g1"),), notify=notify).poll_once())
        poller = self.poller(store, (item("g1"), item("g2")), notify=notify)
        asyncio.run(poller.poll_once())
        first = list(delivered)
        asyncio.run(poller.poll_once())
        self.assertEqual(delivered, first, "第二次不该再推同一条")

    def test_the_per_poll_limit_is_respected_and_the_rest_is_kept(self) -> None:
        delivered: list[str] = []

        async def notify(target, text):
            delivered.append(text)

        store = self.store()
        asyncio.run(self.poller(store, (item("g0"),), notify=notify).poll_once())  # 基线
        lots = tuple(item(f"g{index}") for index in range(1, 6))
        poller = self.poller(store, (item("g0"),) + lots, notify=notify, per_poll=2)
        asyncio.run(poller.poll_once())
        self.assertEqual(len(delivered), 2, "每源每轮最多 N 条")
        asyncio.run(poller.poll_once())
        self.assertEqual(len(delivered), 4, "剩下的下一轮继续推")
        asyncio.run(poller.poll_once())
        self.assertEqual(len(delivered), 5)

    def test_the_hourly_cap_stops_a_flood(self) -> None:
        delivered: list[str] = []

        async def notify(target, text):
            delivered.append(text)

        store = self.store()
        asyncio.run(self.poller(store, (item("g0"),), notify=notify).poll_once())  # 基线
        lots = tuple(item(f"g{index}") for index in range(1, 10))
        poller = self.poller(store, (item("g0"),) + lots, notify=notify, per_poll=10)
        with patch.dict(os.environ, {"QQBOT_RSS_MAX_PER_HOUR": "3"}):
            asyncio.run(poller.poll_once())
            self.assertEqual(len(delivered), 3, "一小时最多 3 条")
            asyncio.run(poller.poll_once())
            self.assertEqual(len(delivered), 3, "这一小时不再推了")

    def test_a_disabled_feed_is_never_fetched(self) -> None:
        calls: list[str] = []
        poller = RssPoller(
            self.store(), notify=None, mail=lambda: None, interval_seconds=300.0,
            clock=lambda: 1000.0,
            feeds_loader=lambda: (FeedSpec(url=FEED_URL, to=("group",), group_id=GROUP,
                                           enabled=False),),
            fetcher=lambda url: calls.append(url) or document_for(item("g1")),
        )
        asyncio.run(poller.poll_once())
        self.assertEqual(calls, [])

    def test_new_items_are_pushed_oldest_first(self) -> None:
        delivered: list[str] = []

        async def notify(target, text):
            delivered.append(text)

        store = self.store()
        asyncio.run(self.poller(store, (item("g0"),), notify=notify).poll_once())
        # 源里是"新的在前"，但推的时候要**从旧到新**。
        poller = self.poller(
            store,
            (item("g3", published=300.0), item("g2", published=200.0),
             item("g1", published=100.0)),
            notify=notify, per_poll=2,
        )
        asyncio.run(poller.poll_once())
        self.assertIn("标题-g1", delivered[0])
        self.assertIn("标题-g2", delivered[1])


# ---------------------------------------------------------------------------
# 5. 失败降频
# ---------------------------------------------------------------------------


class BackoffTests(TempStoreMixin):
    def test_consecutive_failures_back_off_exponentially(self) -> None:
        store = self.store()
        self.assertEqual(store.note_failure(FEED_URL, 300.0, 1000.0), 1300.0,
                         "第 1 次失败：等 1 个节拍")
        self.assertEqual(store.note_failure(FEED_URL, 300.0, 1000.0), 1600.0,
                         "第 2 次失败：等 2 个节拍")
        self.assertEqual(store.note_failure(FEED_URL, 300.0, 1000.0), 2200.0,
                         "第 3 次失败：等 4 个节拍")

    def test_a_dead_site_is_not_hit_every_round(self) -> None:
        store = self.store()
        store.note_failure(FEED_URL, 300.0, 1000.0)
        self.assertFalse(store.due(FEED_URL, 300.0, 1100.0))
        self.assertTrue(store.due(FEED_URL, 300.0, 1300.0))

    def test_a_success_clears_the_backoff(self) -> None:
        store = self.store()
        store.note_failure(FEED_URL, 300.0, 1000.0)
        store.note_success(FEED_URL)
        self.assertTrue(store.due(FEED_URL, 300.0, 1000.0))

    def test_a_failing_fetch_does_not_stop_the_round(self) -> None:
        store = self.store()

        def boom(_url):
            raise fetch_module.FetchError("站挂了")

        poller = RssPoller(
            store, notify=None, mail=lambda: None, interval_seconds=300.0,
            clock=lambda: 1000.0,
            feeds_loader=lambda: (FeedSpec(url="https://example.com/dead",
                                           to=("group",), group_id=GROUP),),
            fetcher=boom,
        )
        summary = asyncio.run(poller.poll_once())
        self.assertEqual(summary["failed"], 1)
        self.assertFalse(store.due("https://example.com/dead", 300.0, 1000.0))


# ---------------------------------------------------------------------------
# 6. 安全：外部正文进群/进邮件之前必须洗
# ---------------------------------------------------------------------------


class SanitizeTests(unittest.TestCase):
    def test_cq_codes_and_mentions_are_stripped(self) -> None:
        cleaned = text_module.clean_text("[CQ:at,qq=all] @全体成员 正文")
        self.assertNotIn("[CQ:", cleaned)
        self.assertNotIn("@", cleaned)
        self.assertIn("正文", cleaned)

    def test_control_characters_are_stripped(self) -> None:
        self.assertNotIn("\x00", text_module.clean_text("坏\x00字符\x1b[31m"))

    def test_the_summary_is_truncated_to_two_hundred_chars(self) -> None:
        cleaned = text_module.clean_text("字" * 1000)
        self.assertLessEqual(len(cleaned), 200)
        self.assertTrue(cleaned.endswith("…"))

    def test_html_is_stripped(self) -> None:
        self.assertNotIn("<b>", text_module.clean_text("<b>粗</b>"))

    def test_only_http_and_https_links_survive(self) -> None:
        for bad in ("javascript:alert(1)", "file:///etc/passwd", "data:text/html,x",
                    "https://example.com/a b", "https://"):
            self.assertEqual(text_module.safe_link(bad), "", bad)
        self.assertEqual(text_module.safe_link("https://example.com/a?b=1"),
                         "https://example.com/a?b=1")

    def test_the_delivered_group_text_is_clean(self) -> None:
        raw = item("g1", title="标题 [CQ:at,qq=all]",
                   summary="摘要 @全体成员 [CQ:face,id=1] " + "很长" * 200,
                   link="javascript:alert(1)")
        body = compose_group_text(document_for(raw), raw)
        self.assertNotIn("[CQ:", body)
        self.assertNotIn("@", body)
        self.assertNotIn("javascript:", body)
        summary_line = body.split("\n")[2]
        self.assertLessEqual(len(summary_line), 200)


# ---------------------------------------------------------------------------
# 7. 状态文件：500 条上限、坏文件不炸
# ---------------------------------------------------------------------------


class StoreTests(TempStoreMixin):
    def test_at_most_five_hundred_ids_are_kept(self) -> None:
        store = self.store()
        for index in range(520):
            store.mark_seen(FEED_URL, f"k{index}")
        state = store.state(FEED_URL)
        self.assertEqual(len(state.ids), 500)
        self.assertNotIn("k0", state.ids, "最旧的先淘汰")
        self.assertIn("k519", state.ids)

    def test_a_broken_state_file_does_not_stop_the_round(self) -> None:
        path = self.tmp / "rss" / "seen.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ 这不是 JSON", encoding="utf-8")
        store = RssStore(path)
        self.assertFalse(store.known(FEED_URL))
        store.mark_seen(FEED_URL, "k1")     # 不许抛

    def test_saving_and_loading_keeps_the_state(self) -> None:
        path = self.tmp / "rss" / "seen.json"
        first = RssStore(path)
        first.set_baseline(FEED_URL, ["k1", "k2"], 1000.0)
        first.save()
        second = RssStore(path)
        self.assertTrue(second.known(FEED_URL))
        self.assertTrue(second.seen(FEED_URL, "k1"))


# ---------------------------------------------------------------------------
# 8. 订阅文件：坏一条只丢一条
# ---------------------------------------------------------------------------


class FeedFileTests(TempStoreMixin):
    def _write(self, document) -> pathlib.Path:
        path = self.tmp / "rss" / "feeds.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
        return path

    def test_a_missing_file_means_no_feeds(self) -> None:
        self.assertEqual(load_feeds(self.tmp / "rss" / "feeds.json"), ())

    def test_one_bad_entry_is_skipped_and_the_rest_survives(self) -> None:
        path = self._write({"feeds": [
            {"url": "https://example.com/ok.xml", "to": ["group"], "group_id": GROUP},
            {"url": "https://example.com/no-group.xml", "to": ["group"]},
            {"url": "ftp://example.com/bad.xml", "to": ["mail"]},
            {"url": "https://example.com/mail.xml", "to": ["mail"]},
        ]})
        self.assertEqual([spec.url for spec in load_feeds(path)], [
            "https://example.com/ok.xml", "https://example.com/mail.xml",
        ])

    def test_both_targets_can_be_requested(self) -> None:
        path = self._write({"feeds": [
            {"url": "https://example.com/both.xml", "to": ["group", "mail"],
             "group_id": GROUP},
        ]})
        spec = load_feeds(path)[0]
        self.assertTrue(spec.wants("group"))
        self.assertTrue(spec.wants("mail"))

    def test_per_feed_limits_override_the_file_defaults(self) -> None:
        path = self._write({"max_per_poll": 5, "default_interval_seconds": 900,
                            "feeds": [
                                {"url": "https://example.com/a.xml", "to": ["mail"]},
                                {"url": "https://example.com/b.xml", "to": ["mail"],
                                 "max_per_poll": 1, "interval_seconds": 1800},
                            ]})
        specs = load_feeds(path)
        self.assertEqual(specs[0].max_per_poll, 5)
        self.assertEqual(specs[0].interval_seconds, 900)
        self.assertEqual(specs[1].max_per_poll, 1)
        self.assertEqual(specs[1].interval_seconds, 1800)


# ---------------------------------------------------------------------------
# 9. 装配：是一个规规矩矩的后台插件
# ---------------------------------------------------------------------------


class AssemblyTests(unittest.TestCase):
    def test_register_hangs_a_background_plugin_on_the_registry(self) -> None:
        registry = PluginRegistry()
        plugin_module.register(registry)
        self.assertEqual(len(registry.backgrounds), 1)
        poller = registry.backgrounds[0]
        self.assertEqual(poller.name, "rss")
        self.assertGreaterEqual(poller.interval_seconds, config.MIN_INTERVAL_SECONDS)
        self.assertTrue(asyncio.iscoroutinefunction(poller.poll_once))
        self.assertTrue(asyncio.iscoroutinefunction(poller.close))

    def test_an_empty_feed_list_does_nothing_and_does_not_raise(self) -> None:
        poller = RssPoller(RssStore(pathlib.Path(os.devnull)), notify=None,
                           mail=lambda: None, interval_seconds=300.0,
                           feeds_loader=lambda: ())
        self.assertEqual(asyncio.run(poller.poll_once()), {"feeds": 0, "pushed": 0})

    def test_a_broken_loader_never_raises_out_of_poll_once(self) -> None:
        def boom():
            raise RuntimeError("读不了")

        poller = RssPoller(RssStore(pathlib.Path(os.devnull)), notify=None,
                           mail=lambda: None, interval_seconds=300.0,
                           feeds_loader=boom)
        self.assertEqual(asyncio.run(poller.poll_once()), {})

    def test_the_plugin_has_a_chinese_title(self) -> None:
        self.assertTrue(plugin_module.TITLE.strip())
        self.assertNotEqual(plugin_module.TITLE, "rss")


if __name__ == "__main__":
    unittest.main()
