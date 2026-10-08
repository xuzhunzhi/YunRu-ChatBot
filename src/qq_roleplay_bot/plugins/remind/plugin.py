"""到点提醒插件的装配点：把**一个窄能力**交给核心，自己不认识核心。

## 交出去的是什么

| 交给谁 | 交的是什么 | 核心拿它做什么 |
| --- | --- | --- |
| `registry.remind = capability` | **同步可调用对象** `(RemindRequest) -> None`（真实形状） | 认到提醒请求时调一次；返回值核心不读 |
| `registry.background(...)` | 到点的节拍（`RemindPoller`） | 核心那份节拍循环每隔一会儿叫一次 `poll_once()` |
| `registry.command(...)` | `/提醒`（看 / 删 / 换） | 核心按老规矩收进命令链（档位判定仍在核心） |

**为什么是同一个对象既当"回调"又当"能力"**：核心那一侧要的就是一个可调用对象
（`plugins.PluginRegistry.remind` 的注释写着"形状比 `vision` 还窄"），所以本插件把
能力对象自己交上去——`__call__` 收请求、别的方法干别的，**没有第二个入口**。

## 两条窄口，各自可能缺席（都不许因此把插件整个弄坏）

1. **核心的发送口 `registry.remind_send`**（核心侧另派落地）：到点那一拍用它把
   "要发什么"交出去。**懒读取**（`core_sender_of`）——每拍再问一次，所以它晚一步接上来
   也没关系；缺它就 fail-closed：留待发、不烧重试次数、每拍一行 warning（见 `remind.py`）。
2. **邮件插件的 `registry.mail.send_to`**：邮箱那一路用它。取不到就**不提供那一路**
   （问他的时候不列第 3 条），绝不自造 SMTP、也绝不把邮件错寄给操作者。

`REQUIRES = ("mail",)` 保证 mail 先装；但 mail 只负责**提供**，用不用由本插件决定。
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
    """取邮件插件"**寄给某个地址**"的那条窄口（`MailSeams.send_to`）；没有就 `None`。

    注意取的**不是** `notify`：那一条的收件人写死在 mail 自己的配置里（操作者）。
    提醒要寄给**那一个人**，所以用的是 2026-10-08 新加的那条窄口。
    """

    sender = getattr(seams, "send_to", None)
    if callable(sender):
        return sender
    logger.warning("到点提醒：邮件插件没给「寄给某个地址」的口子，"
                   "这次不提供邮箱那一路（问他的时候不列第 3 条）")
    return None


def core_sender_of(registry) -> object:
    """核心那条发送口（`registry.remind_send`）的**懒读取**。

    为什么不在 `register()` 那一刻取死：它可能晚一步才接上来（装配顺序），而这道口子
    一旦接上就该立刻开始工作。返回一个**零参函数**：有就回可调用对象，没有回 `None`。
    """

    def sender():
        found = getattr(registry, "remind_send", None)
        return found if callable(found) else None

    return sender


def register(registry) -> None:
    """造能力、交给核心、登记节拍与命令。"""

    capability = RemindCapability(store=RemindStore(),
                                  mail_send=person_mail_sender(registry.mail),
                                  core_sender=core_sender_of(registry))

    # **核心那一侧的口子就是这个形状**：一个可调用对象，认到提醒请求时被调一次。
    try:
        registry.remind = capability
    except AttributeError:  # 老核心还没有这个槽位：**不抛**，只把话说清
        logger.warning("到点提醒这次没接上：这个核心版本没有 registry.remind 这个口子"
                       "（插件仍会装进来：/提醒 能用，收到的提醒没人接）")
    else:
        logger.info("到点提醒已接上：收请求走 registry.remind，发出走 %s；邮箱那一路=%s",
                    "registry.remind_send" if callable(core_sender_of(registry)())
                    else "（核心的发送口还没接上）",
                    "可用" if capability.can_mail else "这次不可用")

    registry.background(RemindPoller(capability, interval_seconds=POLL_SECONDS))
    registry.command(RemindCommand(capability))
