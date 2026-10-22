"""通知正文进她历史之前的**唯一那道清洗**：去协议记号、脱敏、截断。

## 由来（用户 2026-10-19 两轮口径，原话）

- 「发送通知这个，包括命令的回复，能不能通过规则让云茹识别成另一个人在说话」
  → 最后定的是「哎要不还是通知吧，不要当人了」：记成 `speaker="notice"`，
  **不是人**——不进名册、不带 `user_id`、不占名册编号。
- 「这个也进，一起当通知」：**命令的回复**也记进她的历史，跟后台插件发的通知
  同一套。

## 为什么进历史的那一份还要单独洗一遍（发出去的那一份一个字都不改）

两条完全不同的理由，都指向"**只动进历史的那一份**"：

1. **DATA 纪律**：通知正文来自插件与命令输出，都是**不可信内容**。`[CQ:at,qq=…]`
   是协议记号，不是给人看的话——留在历史里模型会照抄（她学舌吐 `[CQ:…]` 这件事
   在出站那条路上早就有专门的补丁，见 `security.OUTBOUND_TAG_NAMES`）。
2. **不记录敏感内容**（`AGENTS.md` §2.1 硬约束）：命令输出里有本机路径、进程信息、
   凭据形状的串、QQ 号/群号。**发给发命令的那个人是对的**（他本来就有权限看），
   但**不许进她的历史**：历史每轮都会被检索进 prompt，压缩时还会被写进摘要。

## 打码哪几类（换成一枚记号 `〔略〕`，不区分是哪一类——分标签本身也是信息）

| 类别 | 判据 | 为什么 |
| --- | --- | --- |
| 协议记号 | `[CQ:…]`、`@某人` | 不是话，模型会学舌 |
| 凭据形状 | `sk-…`；`token/secret/password/api_key/bearer` 后面跟值 | §2.1：凭据不进历史 |
| 绝对路径 | `C:\…` / `C:/…` / `/a/b/…`（**不吃 URL**） | §2.1：本机信息不进历史 |
| 邮箱 | `a@b.com` | 标识一个人，且 `@` 一并会被当成提及 |
| 长数字串 | 连续 ≥6 位数字 | QQ 号、群号、进程号、端口一类的标识 |

短数字（"3 条""0.5 秒"）**不动**：命令回复里到处是这种数，全糊掉等于没信息。

## 什么时候能改回去

只有用户重新定"通知不进历史"或"命令回复可以原样进历史"时。改之前先想清楚：
去掉这一道之后，`/super status` 那类输出**会原样**落进她的历史并可能被压缩进摘要。
"""
from __future__ import annotations

import re

from .security import sanitize_chat_text

#: 进历史的一份**最多**多少字（用户 2026-10-19：「正文截断 200 字」）。
MAX_NOTICE_CHARS = 200

#: 打码后用的那一枚记号。**只有一枚**，不按类别分开——"这里是路径"本身也是信息。
REDACTED = "〔略〕"

#: `[CQ:at,qq=123]` / `[CQ:image,…]` 这类协议记号：不是话，只在中转协议里才有意义。
_CQ_CODE = re.compile(r"\[CQ:[^\]]*\]", re.IGNORECASE)
#: `@某人` / `@123456`。**邮箱先走**，所以这里剩下的都是提及记号。
_MENTION = re.compile(r"@[^\s@，。！？；：、,.!?;:]{1,32}")
#: 凭据形状：`sk-` 前缀的 key，以及 `token=…` `password: …` 这类"名字后面跟值"。
_SECRET = re.compile(
    r"(?:sk-[A-Za-z0-9_\-]{4,}"
    r"|(?i:\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|"
    r"password|passwd|passphrase|bearer)\b)\s*[:=：]\s*\S+)"
)
#: Windows 绝对路径（`C:\Users\x` / `D:/data`）。
_WINDOWS_PATH = re.compile(r"\b[A-Za-z]:[\\/][^\s，。；、）)\]}]*")
#: POSIX 绝对路径（`/home/x`、`/var/log/a.log`）。
#: 前面不许是 `:` `/` 或字母数字——否则 `http://a/b/c` 里那半截会被当成路径吃掉。
_POSIX_PATH = re.compile(r"(?<![\w:/])/(?:[A-Za-z0-9_.\-]+/)+[A-Za-z0-9_.\-]*")
#: 邮箱（要在 `_MENTION` **之前**处理，否则 `a@b.com` 会被当成提及切一半）。
_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
#: 长数字串：QQ 号 / 群号 / 端口 / 进程号。**6 位起**（短数字到处是，不动）。
_LONG_DIGITS = re.compile(r"\d{6,}")


def strip_cq(text: str) -> str:
    """去掉 `[CQ:…]` 协议段——它们是中转协议里的记号，不是话。"""

    return _CQ_CODE.sub(" ", text)


def strip_mentions(text: str) -> str:
    """去掉 `@某人` / `@123456` 提及记号。

    ⚠️ **必须在 `redact` 之后调**：邮箱里也有 `@`，先脱邮箱再剥提及，否则
    `a@b.com` 会被切成 `a .com`（一半是地址、一半是噪音，比整条留着更糟）。
    """

    return _MENTION.sub(" ", text)


def strip_protocol_marks(text: str) -> str:
    """去掉协议记号（`[CQ:…]` 与 `@某人`）——它们不是话。

    单独一个函数是给"只想看干净文本"的调用方与测试用的；`for_history` 走的是
    更细的顺序（`strip_cq` → `redact` → `strip_mentions`），别直接换用这一个。
    """

    return strip_mentions(strip_cq(text))


def redact(text: str) -> str:
    """把五类敏感内容换成一枚记号（顺序有讲究，见模块头那张表）。"""

    cleaned = _EMAIL.sub(REDACTED, text)
    cleaned = _SECRET.sub(REDACTED, cleaned)
    cleaned = _WINDOWS_PATH.sub(REDACTED, cleaned)
    cleaned = _POSIX_PATH.sub(REDACTED, cleaned)
    cleaned = _LONG_DIGITS.sub(REDACTED, cleaned)
    return cleaned


def for_history(text: object, *, max_chars: int = MAX_NOTICE_CHARS) -> str:
    """**进她历史的那一份**：去协议记号 → 脱敏 → 去控制字符 → 截断到 `max_chars`。

    每一步的顺序都不是随手排的：

    1. `strip_cq`：协议段整段先去掉（它里面的数字/冒号不该参与后面的判定）；
    2. `redact`：**邮箱必须在剥提及之前**（见 `strip_mentions` 的说明）；
    3. `strip_mentions`：剩下的 `@` 就都是提及记号了；
    4. `sanitize_chat_text`：控制字符 + 长度上限（放在脱敏**之后**，免得半截凭据
       正好落在截断点上）。

    空串（什么都没剩下，例如整条只有一个 CQ 码）返回空串，调用方据此**不记**——
    历史里多一条空通知比少一条更糟。

    调用方**只许拿它写历史**：真正发出去的那一份必须是原文（命令回复的原文是
    操作者要看的东西，脱敏了就等于把他的信息藏起来了）。
    """

    cleaned = strip_cq(str(text or ""))
    cleaned = redact(cleaned)
    cleaned = strip_mentions(cleaned)
    # 控制字符走 `sanitize_chat_text`（它只管控制字符 + 上限，先不截断——
    # 截断放在下面自己那一行，这样"到底截没截"是确定的，不会因为上游先切成 200
    # 字而悄悄少了那个省略号）。
    cleaned = sanitize_chat_text(cleaned)
    # 多行输出（`/super status` 那种）保留换行——她得看得出那是几条；
    # 每一行内部的连续空白压成一个空格（缩进在历史里只是噪音）。
    cleaned = "\n".join(" ".join(line.split()) for line in cleaned.splitlines()).strip()
    if len(cleaned) <= max_chars:
        return cleaned
    # 截断要**看得出来是被截断的**：少一个字换成省略号，总长仍是 `max_chars`。
    return cleaned[:max_chars - 1].rstrip() + "…"
