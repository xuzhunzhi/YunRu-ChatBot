"""到点提醒插件的装配点：把**一个窄能力**交给核心，自己不认识核心。

## 交出去的是什么

形状照 `registry.vision`（那是"插件交一个工厂、核心装配时调它"的现成写法）：

| 交给谁 | 交的是什么 | 核心拿它做什么 |
| --- | --- | --- |
| `registry.remind = build` | 一个工厂 `build(hand_off=None)` | 核心装配时调一次，把它自己的**发送口**递进来；拿到手的能力挂在 `engine.remind` 上 |
| `registry.background(...)` | 到点的节拍（`RemindPoller`） | 核心那份节拍循环每隔一会儿叫一次 `poll_once()` |
| `registry.command(...)` | `/提醒`（看 / 删 / 换） | 核心按老规矩收进命令链（档位判定仍在核心） |

**为什么给工厂、不给造好的对象**：到点发东西要核心的发送口，而那个口是核心装配时才
存在的（与识图的用量账本同一形状：`build_vision(usage_store)`）。所以这里只交一个
"收下发送口"的函数；能力对象本身在 `register()` 就造好了，工厂只是**把它接上**
（`capability.attach(hand_off)`）——这样"核心还没调工厂"的那段时间里，
命令那一半照常能用。

## 核心这一版还没有这个口子时：明确不接，但不静默

`registry` 上现在没有 `remind` 这个槽位（`PluginRegistry.__slots__` 里没有），
所以赋值会 `AttributeError`——那是**预期**的（核心那一半还在写）。
这时记一行 `warning` 说清"提醒收不到、也发不出去"，插件其余部分照装：
**宁可让它只坏掉自己那一半，也不要静默装成一个用不了的东西。**

## 邮箱那一路：用邮件插件的能力，取不到就不提供

见 `person_mail_sender`。`REQUIRES = ("mail",)` 保证 mail 先装；
但 mail 现在只开放"给**操作者**发一封"，所以"寄给某个人"那条窄口取不到时，
本插件**不提供邮箱这一路**（问他的时候就不列第 3 条），
而不是把邮件错寄给操作者、也不是自己再写一套 SMTP。
"""
from __future__ import annotations

import logging

from .background import RemindPoller
from .command import RemindCommand
from .remind import POLL_SECONDS, RemindCapability
from .store import RemindStore

logger = logging.getLogger(__name__)

#: **前置插件**：邮箱那一路走 mail 的发信能力（方向：mail 提供，本插件使用，
#: 与掉线通知同一条规矩）。`discover()` 会先装 mail，mail 装不上就跳过本插件，
#: 核心照常跑。
REQUIRES = ("mail",)

#: 面板上的显示名（`plugins.inventory()` 读它）。
TITLE = "到点提醒"


def person_mail_sender(seams) -> object | None:
    """取邮件插件"**寄给某个人**"的那条窄口；没有就返回 `None`。

    现在 mail 经 `MailSeams` 开放的只有 `notify(subject, body)`——那一个的收件人
    写死在 mail 自己的配置里（操作者）。提醒要寄给**那一个人**，所以这里找的是另一条：
    `send_to(address, subject, body)`。取不到 = 这次不提供邮箱那一路（fail-closed）。
    """

    sender = getattr(seams, "send_to", None)
    if callable(sender):
        return sender
    logger.warning("到点提醒：邮件插件没给「寄给某个人」的口子，"
                   "这次不提供邮箱那一路（问他的时候不列第 3 条）")
    return None


def _hand_to_core(registry, build) -> bool:
    """把工厂交给核心。核心这一版还没有 `remind` 这个口子时返回 `False`（**不抛**）。"""

    provide = getattr(registry, "provide_remind", None)
    if callable(provide):
        provide(build)
        return True
    try:
        registry.remind = build
    except AttributeError:
        return False
    return True


def register(registry) -> None:
    """造能力、交给核心、登记节拍与命令。"""

    capability = RemindCapability(store=RemindStore(),
                                  mail_send=person_mail_sender(registry.mail))

    def build(hand_off=None):
        """核心的装配口：把它的发送口接上来，然后把同一个能力交回去。

        没被调用也不影响命令那一半；到点的那几条会记成"没发出去、可重试"。
        """

        capability.attach(hand_off)
        return capability

    if _hand_to_core(registry, build):
        logger.info("到点提醒已接上：到点由核心发（邮箱那一路=%s）",
                    "可用" if capability.can_mail else "这次不可用")
    else:
        logger.warning("到点提醒这次没接上：核心这一版还没有 registry.remind 这个口子。"
                       "插件仍会装进来（/提醒 能用），但认到的提醒没人接、到点也发不出去")

    registry.background(RemindPoller(capability, interval_seconds=POLL_SECONDS))
    registry.command(RemindCommand(capability))
