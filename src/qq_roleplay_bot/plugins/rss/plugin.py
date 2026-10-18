"""订阅插件的装配点：取两个投递口、挂上后台节拍。

## 装配只做四件事，顺序固定

1. 造自己的状态（`RssStore`，指向 `data/rss/seen.json`）；
2. **取**核心注入的通知口（`registry.notify`）——群那一路用它；
3. **取**邮件那条窄口（`registry.mail`）——**懒读**：每轮投递时再问一次，
   所以 mail 晚一步接上也没关系，取不到就只是"这次没有邮件那一路"；
4. `registry.background(RssPoller(...))` 把节拍挂上去。

## 为什么"没有订阅也照挂"

订阅是每轮现读的文件（`data/rss/feeds.json`）：先挂上节拍，之后往里加一个源就不必
重启。没有订阅时它只记**一行** info（只在第一轮），不是每 15 分钟灌一次日志。

## 为什么没有 `REQUIRES`

邮件那一路是**可选的**（取不到就不寄，群里照推）。`REQUIRES = ("mail",)` 会让
"这个部署没装 mail"变成"整个订阅功能消失"——那正是 `plugins/remind/plugin.py` 里
记过的那个教训（2026-10-09 审查第 8 条）。所以依赖只经由**接缝**表达：有就用、
没有就少一路。

## 本插件不认识任何别的插件

不 import `plugins/mail/**`、不 import 核心的对话/记忆模块、也不 import `transport`。
它只认两个可调用对象——这是"删掉整个 `plugins/rss/`，核心与别的插件照跑"能成立的
原因。
"""

from __future__ import annotations

import logging

from . import config
from .background import RssPoller
from .store import RssStore

logger = logging.getLogger(__name__)

#: 面板上的显示名（`plugins.inventory()` 读它；必须与模块名不同）。
TITLE = "订阅推送"


def mail_sender_of(registry):
    """邮件那一路的**懒读取**：返回一个零参函数，取到发信口就返回可调用对象。

    为什么不是"注册那一刻取死"：`mail` 可能晚一步接上，而这道口子一旦接上就该开始
    工作。为什么先问 `knows()` 而不直接拿 `notify`：`notify` 是**类上的方法**，
    永远可调用；没有提供者时它才抛 `RuntimeError`——拿"方法在不在"当判据，
    等于每次都让一条订阅去撞一次异常（`plugins/remind/plugin.py` 记过同一件事）。
    """

    def sender():
        seams = getattr(registry, "mail", None)
        knows = getattr(seams, "knows", None)
        if callable(knows) and knows():
            return getattr(seams, "notify", None)
        return None

    return sender


def register(registry) -> None:
    """把订阅这件事接到节拍上。"""

    store = RssStore()
    notify = getattr(registry, "notify", None)
    if not callable(notify):
        # 老核心还没有这个接缝：群里那一路发不出去，但邮箱那一路还能用，
        # 所以**不放弃整个插件**，只把话说清（可搜）。
        logger.warning("订阅推送：这个核心没有 notify 接缝，群里那一路不工作（邮箱照常）")

    poller = RssPoller(store, notify=notify, mail=mail_sender_of(registry),
                       interval_seconds=config.interval_seconds())
    registry.background(poller)
    logger.info("订阅推送已接上：每 %ss 看一轮，每源每轮最多 %s 条、每小时最多 %s 条；"
                "订阅文件=%s；群里那一路=%s；邮件那一路=%s",
                int(poller.interval_seconds), config.max_per_poll(), config.max_per_hour(),
                config.feeds_path(),
                "可用" if callable(notify) else "不可用（核心没给 notify）",
                "看 mail 装没装")
