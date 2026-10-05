"""掉线通知的**正文**与"被叫一次"要做的事。

## 措辞（用户把这一条交给我们了：*"怎么触发，yunru 怎么说话就你自己看着办了"*）

这封信的收件人是**操作者**（同 `MAIL_REPORT_TO`），不是"云茹写给主人"的那种信——
所以它**不扮演**、不用她的口吻，只讲三件事：**什么时候断的、现在是什么症状、
该怎么办**。正文是固定模板、不调模型：

    【时间】2026-10-06 21:37:12（+08:00）
    【症状】QQ 那侧的连接没上来：她收不到群里的消息，也发不出消息。
    【怎么办】把 NapCat 重开一次（重启后她只会收新消息，不会把断线期间漏掉的消息补回来）。

    —— 这封是自动发的告警，断线一次只发一封；恢复不发。

## 为什么不扮演、也不讲机制词

`AGENTS.md` §2.2 那张表（`检查` / `触发` / `调用` / `协议` / `提示词` / `上下文` /
`记忆库` / `Stage`）是给**会流进她本人 prompt 的文本**定的。这封信不进 prompt，
但同一条道理在这里更硬：收件人读了要**马上知道做什么**，而"用她的口吻讲机制词"
只会让这封告警看不懂。所以：**直说**。测试里有一条专门扫这张表。

（"恢复不发"写在结尾是有意的：它就是用户说的*"bot 本身稳定性我认为是很可靠的，
不用额外通知"*——操作者看到这封信时该知道"没有第二封说恢复了"。）

## "被叫一次"要做的事，就是发一封信

**这里没有"这一段断线通知过没有"的状态**——那是核心的活（见包的说明第 1 条）。
本模块被叫一次就发一封；同一段断线里核心不会再叫第二次。
"""

from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

#: 邮件主题。**固定**，只由时间区分（邮箱里按主题就能一眼认出这是告警）。
SUBJECT = "云茹断线了"

#: 正文模板。时间那一行占位，其余固定。
NOTICE_TEMPLATE = (
    "【时间】{moment}\n"
    "【症状】QQ 那侧的连接没上来：她收不到群里的消息，也发不出消息。\n"
    "【怎么办】把 NapCat 重开一次（重启后她只会收新消息，不会把断线期间漏掉的消息补回来）。\n"
    "\n"
    "—— 这封是自动发的告警，断线一次只发一封；恢复不发。"
)

#: 时间那一行的格式（本地时间 + UTC 偏移，操作者不必再自己换算）。
MOMENT_FORMAT = "%Y-%m-%d %H:%M:%S"


def format_moment(when: float | None = None) -> str:
    """把时刻写成"本地时间（±HH:MM）"。`when` 缺省是此刻。

    为什么带偏移：这台机器与读信的人可能不在一个时区，而"什么时候断的"是这封信
    最要紧的一条信息——只有裸时刻的话，对方每读一次都要自己推算。
    """

    local = time.localtime(time.time() if when is None else when)
    return time.strftime(MOMENT_FORMAT, local) + time.strftime("（%z）", local)


def build_notice(when: float | None = None) -> tuple[str, str]:
    """回 `(主题, 正文)`。**纯函数**：不碰网络、不碰注册表、不调模型。"""

    return SUBJECT, NOTICE_TEMPLATE.format(moment=format_moment(when))


async def notice_on_disconnect(send, when: float | None = None) -> bool:
    """掉线那一次被叫：发一封告警。返回"发出去了没有"。

    `send` 是 **mail 插件放上来的那个函数**（`(subject, body) -> await`，
    见 `plugins.MailSeams`）——本模块不知道它背后是哪个模块、哪个 CLI、发给谁。

    ## 失败只记一笔（用户要求 + `AGENTS.md` §2.1）

    **不许把机器人搞崩**：发不出去（没网、CLI 没装、凭据过期、对面退信）只在这里
    记一行 `outage_notice_send_failed`，然后返回 `False`。
    为什么**不再往上抛**：这一步的调用方是核心的广播侧
    （`runtime._DisconnectNotifier._broadcast`），它兜住异常、记一行
    `disconnect_receiver_failed`，**但它区分不了**"发信失败"与"这个插件有 bug"。
    在这里就地记下**具体是哪件事失败**，那行日志才有排障价值；
    而"不崩"这条判据不依赖调用方——本函数自己就保证不抛。
    `KeyboardInterrupt` / `SystemExit` 不属于"发信失败"，照旧上抛。
    """

    subject, body = build_notice(when)
    try:
        await send(subject, body)
    except Exception as exc:  # noqa: BLE001 - 发信失败的代价只能是一行日志
        logger.warning("outage_notice_send_failed error=%s", type(exc).__name__,
                       exc_info=True)
        return False
    logger.info("outage_notice_sent subject=%s", subject)
    return True
