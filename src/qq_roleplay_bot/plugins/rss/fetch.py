"""抓一个订阅源，解析成条目——**只用标准库**（`urllib` + `xml.etree`）。

## 三条护栏（交底里逐条点名）

1. **超时 10 秒**（`config.timeout_seconds()`）：卡住的站不许把整个节拍拖死。
2. **响应上限 2MB**（`config.max_bytes()`）：先看 `Content-Length`，再按上限读，
   读出来还超就整条扔掉——不解析半截 XML（半截 XML 只会得到半截条目）。
3. **只认 http/https**：`file://` / `javascript:` 之类的地址在 `feed.py` 那道校验里
   就被挡掉了，这里再挡一次（纵深：这个函数将来可能被别处调用）。

## 支持两种格式

* **RSS 2.0**：`<rss><channel><item>`（`title` / `link` / `description` / `guid` / `pubDate`）；
* **Atom**：`<feed><entry>`（`title` / `link href` / `summary|content` / `id` / `updated`）。

判据用**标签的本地名**（`{http://www.w3.org/2005/Atom}entry` → `entry`），所以带不带
命名空间都认。别的方言（RDF、RSS 0.9x）没有专门处理：认不出 `<item>`/`<entry>` 就当
"这个源这一轮没有条目"，不猜。

## 一个如实记下的边界

`xml.etree` **不解析外部实体**，但内部实体展开这类攻击它没有专门的防护。
这里的取舍是：2MB 上限 + 只解析我们自己列出来的那几个标签——真要防到"实体炸弹"那一层，
stdlib 这条路就得换成 `defusedxml`，而**交底明确不许新增依赖**。这条写在这里，
免得以后有人以为这里有过防御。
"""

from __future__ import annotations

import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime

from . import config

#: 自称一下（有些站会拒绝没有 UA 的请求）。
USER_AGENT = "yunru-rss/1.0 (RSS/Atom reader; stdlib only)"

#: 有些站按 `Accept` 返回 HTML，把 RSS 也写进去。
ACCEPT = ("application/rss+xml, application/atom+xml, application/xml, "
          "text/xml;q=0.9, */*;q=0.5")


class FetchError(RuntimeError):
    """抓取失败（超时 / 太大 / 不是 http(s) / 网络错误）——调用方据此降频。"""


@dataclass(frozen=True, slots=True)
class Item:
    """一条条目。`guid` 是**去重键**的唯一来源（没有 id 时退回链接、再退回标题）。"""

    guid: str
    title: str = ""
    summary: str = ""
    link: str = ""
    published: float | None = None


@dataclass(frozen=True, slots=True)
class FeedDocument:
    """一次抓取的结果：源标题 + 条目（**按文件里的顺序**，不改写）。"""

    title: str = ""
    items: tuple[Item, ...] = ()


def _local(tag: object) -> str:
    """`{ns}item` → `item`（判据只看本地名，带不带命名空间都认）。"""

    text = str(tag)
    return text.rsplit("}", 1)[-1] if "}" in text else text


def _text(element: object, *names: str) -> str:
    """取第一个名字对得上的子节点的文本（去空白）。"""

    if element is None:
        return ""
    for child in list(element):
        if _local(child.tag) in names:
            value = (child.text or "").strip()
            if value:
                return value
    return ""


def _atom_link(element: object) -> str:
    """Atom 的链接在属性里（`<link rel="alternate" href="…"/>`）；优先 alternate。"""

    if element is None:
        return ""
    fallback = ""
    for child in list(element):
        if _local(child.tag) != "link":
            continue
        href = str(child.attrib.get("href", "") or "").strip()
        if not href:
            continue
        rel = str(child.attrib.get("rel", "alternate") or "alternate").strip().lower()
        if rel == "alternate":
            return href
        if not fallback:
            fallback = href
    return fallback


def _timestamp(raw: str) -> float | None:
    """`pubDate`（RFC 822）与 `updated`（ISO 8601）都能吃；认不出来就 `None`。"""

    text = (raw or "").strip()
    if not text:
        return None
    try:
        return parsedate_to_datetime(text).timestamp()
    except (TypeError, ValueError, IndexError):
        pass
    candidate = text.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(candidate).timestamp()
    except ValueError:
        return None


def parse(xml_text: str) -> FeedDocument:
    """把一份 XML 解析成条目。解析失败抛 `FetchError`（那是"这个源坏了"）。"""

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise FetchError(f"XML 解析失败：{exc}") from exc

    root_name = _local(root.tag)
    if root_name == "rss":
        channel = next((child for child in list(root) if _local(child.tag) == "channel"), None)
        if channel is None:
            return FeedDocument()
        title = _text(channel, "title")
        entries = [child for child in list(channel) if _local(child.tag) == "item"]
        items = [_parse_rss_item(entry) for entry in entries]
    elif root_name == "feed":
        title = _text(root, "title")
        entries = [child for child in list(root) if _local(child.tag) == "entry"]
        items = [_parse_atom_item(entry) for entry in entries]
    else:
        # 认不出的方言：不猜，也不报错（有些站会回一段 HTML 错误页）。
        return FeedDocument()
    return FeedDocument(title=title, items=tuple(item for item in items if item is not None))


def _parse_rss_item(entry) -> Item | None:
    title = _text(entry, "title")
    link = _text(entry, "link").strip()
    if not link:
        # 有些 RSS 把链接写在 `<guid isPermaLink="true">` 里。
        guid_node = next((child for child in list(entry) if _local(child.tag) == "guid"), None)
        if guid_node is not None and str(guid_node.attrib.get("isPermaLink", "")).lower() == "true":
            link = (guid_node.text or "").strip()
    summary = _text(entry, "description", "summary", "encoded")
    guid = _text(entry, "guid", "id") or link or title
    if not guid:
        return None
    return Item(guid=guid, title=title, summary=summary, link=link,
                published=_timestamp(_text(entry, "pubDate", "published", "updated")))


def _parse_atom_item(entry) -> Item | None:
    title = _text(entry, "title")
    link = _atom_link(entry)
    summary = _text(entry, "summary", "content")
    guid = _text(entry, "id") or link or title
    if not guid:
        return None
    return Item(guid=guid, title=title, summary=summary, link=link,
                published=_timestamp(_text(entry, "updated", "published")))


def fetch(url: str, *, opener=None, timeout: float | None = None,
          max_bytes: int | None = None) -> FeedDocument:
    """抓一个源并解析。**这个函数会碰网络**——所以测试一律传 `opener` 打桩。

    `opener(request, timeout=…)` 是可注入的（默认 `urllib.request.urlopen`）：
    测试里用一个假的 opener 就能覆盖"超时 / 太大 / 302 到别处"这些分支，
    **不必真的去打外网**。
    """

    document, _final_url = fetch_xml(url, opener=opener, timeout=timeout, max_bytes=max_bytes)
    return parse(document)


def fetch_xml(url: str, *, opener=None, timeout: float | None = None,
              max_bytes: int | None = None) -> tuple[str, str]:
    """抓取并解码成 `(文本, 最终地址)`。**不解析**——解析在 `parse()`。

    单独一个函数是为了让"抓"与"解"能各测各的（打桩 opener 不需要造 XML；
    测解析不需要造 HTTP 响应）。
    """

    parsed = urllib.parse.urlparse(str(url or "").strip())
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
        raise FetchError("只抓 http/https 的地址")
    limit = int(max_bytes if max_bytes is not None else config.max_bytes())
    seconds = float(timeout if timeout is not None else config.timeout_seconds())
    request = urllib.request.Request(str(url).strip(), headers={
        "User-Agent": USER_AGENT,
        "Accept": ACCEPT,
    })
    open_url = opener or urllib.request.urlopen
    payload = b""
    final_url = str(url).strip()
    charset = "utf-8"
    try:
        with open_url(request, timeout=seconds) as response:
            headers = getattr(response, "headers", None)
            declared = ""
            content_type = ""
            try:
                declared = str(headers.get("Content-Length", "") or "") if headers else ""
                content_type = str(headers.get("Content-Type", "") or "") if headers else ""
            except Exception:  # noqa: BLE001 - 拿不到就当没有（不影响正文）
                declared, content_type = "", ""
            if declared.isdigit() and int(declared) > limit:
                raise FetchError(f"响应声明 {declared} 字节，超过上限 {limit}")
            # `read(limit + 1)`：多要一个字节就够判断"到底超没超"，不必读完整份。
            payload = response.read(limit + 1)
            final_url = str(getattr(response, "url", "") or url)
            marker = "charset="
            lowered = content_type.lower()
            if marker in lowered:
                charset = (content_type[lowered.index(marker) + len(marker):]
                           .split(";")[0].strip() or "utf-8")
    except FetchError:
        raise
    except Exception as exc:  # noqa: BLE001 - urllib 的异常族太多，统一翻译
        raise FetchError(f"抓取失败：{type(exc).__name__}") from exc
    if len(payload) > limit:
        raise FetchError(f"响应超过上限 {limit} 字节，整条丢掉")
    try:
        text = payload.decode(charset, errors="replace")
    except (LookupError, ValueError):
        text = payload.decode("utf-8", errors="replace")
    return text, final_url
