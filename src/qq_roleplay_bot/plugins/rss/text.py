"""外部正文的清洗：**RSS 正文是不可信内容**，投递前先把能作恶的部分去掉。

## 为什么这里要单独洗一遍（而不是指望下游）

下游的 `notify` 走核心，核心会把**进历史的那一份**再洗一次（去 CQ/@、脱敏、截断）。
但那是"历史那一份"；**发到群里、寄进邮箱的那一份是原文**。订阅源是外部内容，
里面可以带任何东西：

- `[CQ:at,qq=all]`：可以让一条订阅把一个群所有人 @ 一遍（这就是"外部内容驱动了群行
  为"）。所以投递前**先去掉** CQ 码与 `@`；
- 控制字符：会把日志和终端搅乱，也过不了某些客户端；
- 超长正文：一条 RSS 的 `description` 可能是整篇文章（几十 KB）。交底定的口径是
  **截断到 200 字**；
- 链接：只保留 `http(s)`。`javascript:` / `file:` / `data:` 一律换成空。

## 口径（交底原话）：去掉 `[CQ:...]`、`@`、控制字符，截断到 200 字，链接只保留 http/https

这条也顺带保证"通知看起来还是通知"：它不会被外部内容改造成一条 @全体成员。
"""

from __future__ import annotations

import re
import urllib.parse

#: 一条通知里正文最多多少字（交底定的 200）。
MAX_SUMMARY_CHARS = 200
#: 标题最多多少字（标题比正文短得多，200 是给它留的余量）。
MAX_TITLE_CHARS = 80

#: `[CQ:at,qq=all]` 这类协议段。
_CQ_CODE = re.compile(r"\[CQ:[^\]]*\]", re.IGNORECASE)
#: `@某人` / `@全体成员` / `@123456789`（不吞标点：中文顿号、逗号、句号停）。
_MENTION = re.compile(r"[@＠][^\s@，。！？；：、,.!?;:]{1,32}")
#: 控制字符（保留 `\n` `\r` `\t`，其余 C0/C1 与 DEL 全去）。
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
#: 一串连续空白压成一个空格。
_SPACES = re.compile(r"\s+")
#: HTML 标签：RSS 的 `description` 经常是 HTML 片段，留着会让群里看到一堆 `<a href=…>`。
_TAG = re.compile(r"<[^>]{1,200}>")


def clean_text(raw: object, *, limit: int = MAX_SUMMARY_CHARS) -> str:
    """去 CQ 码 / @ / 控制字符 / HTML 标签，压空白，截断到 `limit` 字。"""

    text = str(raw or "")
    text = _CQ_CODE.sub(" ", text)
    text = _TAG.sub(" ", text)
    text = _CONTROL.sub("", text)
    text = _MENTION.sub(" ", text)
    # `&nbsp;` 这类实体：只处理最常见的几个（不引第三方库，也不做完整反转义——
    # 反转义出来的 `&#64;` 会重新变回 `@`，那等于把上面那道判断绕过去）。
    for entity, replacement in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"),
                                ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'")):
        text = text.replace(entity, replacement)
    text = _SPACES.sub(" ", text).strip()
    if len(text) <= limit:
        return text
    return text[:limit - 1].rstrip() + "…"


def clean_title(raw: object) -> str:
    """标题：同一套清洗，短一点。"""

    return clean_text(raw, limit=MAX_TITLE_CHARS)


def safe_link(raw: object, *, limit: int = 500) -> str:
    """只放行 `http(s)` 的绝对链接；别的（`javascript:` / `file:` / 垃圾）返回空串。"""

    text = str(raw or "").strip()
    if not text or len(text) > limit:
        return ""
    if _CONTROL.search(text) or any(char.isspace() for char in text):
        return ""
    parsed = urllib.parse.urlparse(text)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
        return ""
    return text
