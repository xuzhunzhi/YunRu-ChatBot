"""联网搜索插件：给写信 agent 的 `<web>` 工具提供**只读**的公开网页查询。

边界（与 `mail/letter_tools.py` 同一套纪律）：

- 只读：只发一次 GET，不登录、不提交表单、不碰仓库里的任何数据；
- 有界：一次最多 3 条结果、查询词最多 80 字、8 秒超时；
- 查不到不挡路：网络失败/解析失败一律回空串，写信那条路照常走（"没搜着"）。

这个插件**不注册**命令、后台或面板；它提供的是 `letter_tools.LetterToolbox._web`
那一条查询路径，由 mail 插件在写信工具环里调用。
"""

from __future__ import annotations

TITLE = "联网搜索"
VERSION = "0.1.0"
HOST_API = 1


def register(registry) -> None:  # noqa: ARG001 - 契约要求这个签名
    """没有要登记的东西：能力由 `search.search()` 直接被写信工具环引用。"""

    return None
