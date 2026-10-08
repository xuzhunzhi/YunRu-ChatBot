"""「看 / 改我的提醒」的入口——**命令**形态（理由在下面）。

## 为什么是命令，而不是又一句对话

两件事分清楚：

- **"8 点提醒我交作业"是对话调起**（用户明确要求，核心在它那次判定里认出来）；
- **"看 / 改我的提醒"这条入口形态由我判断**（用户：优先对话、次选命令、说明理由）。

我选命令，理由是一条硬的：**对话那条路要核心先认得这句话**（判定里多一个字段），
而核心那一半当时还没提交完；命令这条路的核心契约（`name` / `min_level` / `match` /
`handle`）早就稳了，**插件这一侧就能落地、今天就能用**。

对话那条路我给留好了：真正的活干在 `RemindCapability.listing()` / `cancel()` /
`set_channel()` 里，将来核心认得"看我的提醒"时直接调这三个函数，
**命令这条入口用的就是同一份**——两处不会分叉。

## 隐私这件事说清楚

命令的回复落在**哪个会话由核心定**（在群里发就在群里答）。这里不另做遮掩：
那些提醒的内容本来就是他在那个会话里说出口的。
"""
from __future__ import annotations

import re

from .remind import USAGE

#: 命令名。中文与英文各一个写法，`/提醒` 是主用。
_PATTERN = re.compile(r"^/(?:提醒|remind)\s*(?P<rest>.*)$", re.IGNORECASE | re.DOTALL)
#: "删 2" / "取消第 2 条" / "删掉 2" 都认；数字从 1 数起。
_DELETE = re.compile(r"^(?:删|删除|删掉|取消)\s*第?\s*(\d+)\s*条?$")
#: "换 2" / "换邮箱" 都认。
_SWITCH = re.compile(r"^(?:换|换个|改成)\s*(.+)$")


class RemindCommand:
    """`/提醒`：看还有哪些、删一条、换以后叫我的方式。"""

    name = "remind"
    #: 谁都能看自己的——插件**不判权限**，档位只是声明（判定在核心）。
    min_level = "public"

    def __init__(self, capability) -> None:
        self._capability = capability

    def match(self, message) -> bool:
        return bool(_PATTERN.match(str(getattr(message, "text", "") or "").strip()))

    def help_text(self) -> str:
        return "/提醒 —— 看、删、换（到点提醒）"

    @staticmethod
    def _rest(message) -> str:
        found = _PATTERN.match(str(getattr(message, "text", "") or "").strip())
        return (found.group("rest") if found else "").strip()

    async def handle(self, message):
        """返回一句要说的话（发由核心发）；认不出的写法就回一行用法。"""

        person = str(getattr(message, "user_id", "") or "")
        if not person:
            return None
        rest = self._rest(message)
        if not rest:
            return await self._capability.listing(person)
        found = _DELETE.match(rest)
        if found is not None:
            return await self._capability.cancel(person, int(found.group(1)))
        found = _SWITCH.match(rest)
        if found is not None:
            return await self._capability.set_channel(person, found.group(1).strip())
        return USAGE
