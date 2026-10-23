"""联网搜索：一次 DuckDuckGo HTML 查询，返回**标题 + 链接 + 摘要**（最多 3 条）。

为什么用 stdlib `urllib` 而不是 httpx/requests：这个项目对第三方依赖很克制，
而"发一次 GET、解析几个标签"用标准库就够了；以后要换搜索源，只动这一个文件。

为什么是 DuckDuckGo HTML：免 key、免登录、返回的是服务端渲染的 HTML，
不用跑 JS；解析失败就回空串（写信那边按"没搜着"处理，绝不阻塞）。

**外部内容是不可信数据**：这里只做"取回 + 去标签 + 截断"，不执行、不渲染、
不跟随结果里的链接；调用方（写信 agent）只拿到纯文本。
"""

from __future__ import annotations

import html
import re
import urllib.parse
import urllib.request

#: 一次查询最多留这么多字（与 `letter_tools.MAX_QUERY_CHARS` 同口径）。
MAX_QUERY_CHARS = 80
#: 最多给几条。给多了她会照着念，跟"素材"没区别。
MAX_RESULTS = 3
#: 单次请求超时（秒）。写信是离线任务，宁可少一条，不拖住那一晚。
TIMEOUT = 8.0
#: 一次最多读这么多字节，防止一个畸形页面把内存撑爆。
MAX_BYTES = 400_000

_ENDPOINT = "https://html.duckduckgo.com/html/?q={query}"
_BING_ENDPOINT = "https://www.bing.com/search?q={query}&setlang=zh-CN&cc=CN"
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

#: 结果链接：`<a rel="nofollow" class="result__a" href="...">标题</a>`
_RESULT_LINK = re.compile(
    r'<a[^>]*class="[^"]*result__a[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
#: 摘要：`<a class="result__snippet" ...>…</a>` 或 `<div class="result__snippet">…</div>`
_SNIPPET = re.compile(
    r'<(?:a|div)[^>]*class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</(?:a|div)>',
    re.IGNORECASE | re.DOTALL,
)
_TAG = re.compile(r"<[^>]+>")
_SPACES = re.compile(r"\s+")

#: Bing 的结果块：`<li class="b_algo">…</li>`（里面 h2>a 是标题，p 是摘要）。
_BING_BLOCK = re.compile(r'<li[^>]*class="[^"]*b_algo[^"]*"[^>]*>(.*?)</li>',
                         re.IGNORECASE | re.DOTALL)
_BING_LINK = re.compile(r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
                        re.IGNORECASE | re.DOTALL)
_BING_SNIPPET = re.compile(r"<p[^>]*>(.*?)</p>", re.IGNORECASE | re.DOTALL)


def search(query: str, *, limit: int = MAX_RESULTS, timeout: float = TIMEOUT) -> str:
    """搜一次，返回给模型看的纯文本；失败/没结果一律返回空串。

    先走 DuckDuckGo；它超时/被挡/解析不出时退到 Bing（国内网络更稳）。
    两家都失败才回空串——写信那边按"没搜着"处理。
    """

    words = " ".join(str(query or "").split())[:MAX_QUERY_CHARS]
    if not words:
        return ""
    text = _fetch(_ENDPOINT.format(query=urllib.parse.quote(words)), timeout, limit, parse)
    if text:
        return text
    return _fetch(_BING_ENDPOINT.format(query=urllib.parse.quote(words)), timeout, limit, parse_bing)


def _fetch(url: str, timeout: float, limit: int, parser) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": _USER_AGENT,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            page = response.read(MAX_BYTES).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 - 网络失败不是错误，是"这次没搜着"
        return ""
    return parser(page, limit=limit)


def parse_bing(page: str, *, limit: int = MAX_RESULTS) -> str:
    """从 Bing 的 HTML 里抠结果（`li.b_algo` → h2 > a + p）。"""

    if not page:
        return ""
    rows: list[str] = []
    for block in _BING_BLOCK.finditer(page):
        chunk = block.group(1)
        link = _BING_LINK.search(chunk)
        if not link:
            continue
        title = _clean(link.group(2))
        href = _decode_bing(link.group(1))
        if not title or not href:
            continue
        snippet = _BING_SNIPPET.search(chunk)
        note = _clean(snippet.group(1)) if snippet else ""
        row = f"· {title}\n  {href}"
        if note:
            row += f"\n  {note[:160]}"
        rows.append(row)
        if len(rows) >= limit:
            break
    if not rows:
        return ""
    return "网上搜到的（公开网页，不是聊天里发生的事）：\n" + "\n".join(rows)


def _decode_bing(href: str) -> str:
    """Bing 有时把真实地址包在 `/ck/a?…&u=a1<base64>` 里，这里还原。"""

    value = html.unescape(str(href or "").strip())
    if value.startswith("//"):
        value = "https:" + value
    if "bing.com/ck/a" in value:
        target = urllib.parse.parse_qs(urllib.parse.urlparse(value).query).get("u", [""])[0]
        if target.startswith("a1"):
            import base64

            body = target[2:]
            body += "=" * (-len(body) % 4)
            try:
                decoded = base64.urlsafe_b64decode(body).decode("utf-8", "replace")
                if decoded.startswith(("http://", "https://")):
                    value = decoded
            except Exception:  # noqa: BLE001 - 解不出来就保留原链接
                pass
    if not value.startswith(("http://", "https://")):
        return ""
    return value


def parse(page: str, *, limit: int = MAX_RESULTS) -> str:
    """从 DuckDuckGo HTML 里抠出前 `limit` 条；解析不出就返回空串。"""

    if not page:
        return ""
    links = list(_RESULT_LINK.finditer(page))
    snippets = list(_SNIPPET.finditer(page))
    rows: list[str] = []
    for index, match in enumerate(links[:limit]):
        title = _clean(match.group(2))
        href = _clean_href(match.group(1))
        if not title or not href:
            continue
        snippet = _clean(snippets[index].group(1)) if index < len(snippets) else ""
        row = f"· {title}\n  {href}"
        if snippet:
            row += f"\n  {snippet[:160]}"
        rows.append(row)
    if not rows:
        return ""
    return "网上搜到的（公开网页，不是聊天里发生的事）：\n" + "\n".join(rows)


def _clean(fragment: str) -> str:
    """去标签、解实体、压空白；截断到 200 字。"""

    text = _TAG.sub(" ", fragment or "")
    text = html.unescape(text)
    text = _SPACES.sub(" ", text).strip()
    return text[:200]


def _clean_href(href: str) -> str:
    """DDG 的结果链接可能是 `/l/?uddg=<编码后的真实地址>`，还原成真实 URL。"""

    value = html.unescape(str(href or "").strip())
    if value.startswith("//"):
        value = "https:" + value
    parsed = urllib.parse.urlparse(value)
    if parsed.path.endswith("/l/") or parsed.path == "/l/":
        target = urllib.parse.parse_qs(parsed.query).get("uddg")
        if target:
            value = target[0]
    if not value.startswith(("http://", "https://")):
        return ""
    return value
