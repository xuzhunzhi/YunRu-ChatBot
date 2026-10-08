"""「到点提醒我」——**核心侧那一半**：听懂、算时间、备好值、把请求交给插件。

形状（用户 2026-10-08 拍板）：**核心听懂 + 插件做事 + 核心记录 + 核心发送**。
群里说"云茹，8 点提醒我交作业"是**对话调起**的（不是斜杠命令），所以它挤在
**现有那次判定**的返回里（`dialogue_judge` 的 `<remind>` 字段）——**不新增模型调用**。

这个模块只做四件纯数据/纯函数的事（都能离线测）：

1. `RemindIntent`：判定给出的"时间怎么说 + 要提醒什么事"（**原话**，核心不做解释）；
2. `parse_remind_time()`：把时间说法算成**绝对时刻**（epoch）。放核心是因为
   **只有核心知道"群里这边"是哪个时区**（`dev_config.local_timezone()`，
   `QQBOT_LOCAL_TZ_OFFSET_HOURS`）。**认不清就返回 None**——调用方据此当普通聊天，
   不猜、也不追问；
3. `find_email()`：从一句话里认出邮箱（保守正则）；
4. `RemindRequest`：递给**插件**的那个不可变对象——只有它需要的那几个值
   （谁、什么时候、提醒什么、邮箱、发到哪个会话）。**它不携带引擎、不携带记忆服务、
   也不携带任何回调**，所以插件拿不到记忆 API（测试逐个字段钉住）。

插件那一半（收下之后怎么存、到点怎么发、后台节拍）在插件里，核心一个字不管；
插件没装时核心**什么都不做**（不报错、不回复）——见 `DialogueEngine.remind` 的说明。
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

from . import dev_config

#: 相对说法的单位：`20分钟后` / `3小时后` / `2天后` / `半小时后`。
#: **只收这些写法**；"一会儿""晚点""待会儿"一律不认（不猜）。
_RELATIVE_UNITS: tuple[tuple[re.Pattern[str], float], ...] = (
    (re.compile(r"(\d+)\s*个?\s*分钟?\s*后"), 60.0),
    (re.compile(r"(\d+)\s*个?\s*小时\s*后"), 3600.0),
    (re.compile(r"(\d+)\s*天\s*后"), 86400.0),
)
_HALF_HOUR = re.compile(r"半\s*(?:个)?\s*小时\s*后")

#: 日子词 → （往后几天，这个词自带的时间段）。先匹配到的算数（长的写在前面）。
_DAY_WORDS: tuple[tuple[str, int, str], ...] = (
    ("后晚", 2, "晚上"), ("后天", 2, ""),
    ("明晚", 1, "晚上"), ("明天", 1, ""),
    ("今晚", 0, "晚上"), ("今天", 0, ""),
)
#: 时间段 → 12 小时制换算基准（0 = 上午那半边，12 = 下午那半边）。
_DAYPARTS: tuple[tuple[str, int], ...] = (
    ("凌晨", 0), ("早上", 0), ("早晨", 0), ("上午", 0),
    ("中午", 12), ("下午", 12), ("傍晚", 12), ("晚上", 12), ("夜里", 12),
)
_CLOCK_HM = re.compile(r"(\d{1,2})\s*[:：]\s*(\d{1,2})")
_CLOCK_HOUR = re.compile(r"(\d{1,2})\s*点\s*(?:(\d{1,2})\s*分?)?")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")


@dataclass(frozen=True, slots=True)
class RemindIntent:
    """判定认出来的"这是提醒请求"：**时间怎么说**（原话）+ **提醒什么事**。"""

    time_text: str
    what: str


@dataclass(frozen=True, slots=True)
class RemindRequest:
    """递给插件的那一条提醒。**只有这几个字段**——没有引擎、没有记忆服务、没有回调。

    `at` 是**绝对时刻**（epoch 秒，墙上时间）：核心算好了才交出去，插件不做时间推算
    （它拿不到"群里这边"是哪个时区）。`email` 是**核心查好递过来的值**（可能为空串）：
    插件要邮箱只能从这里拿，拿不到记忆。
    """

    person_id: str
    session_id: str
    group_id: str
    at: float
    what: str
    email: str = ""


@dataclass(frozen=True, slots=True)
class RemindDelivery:
    """插件交给核心的**一条投递意图**：发给谁、发到哪、说什么。

    字段就这四个——插件塞不进别的东西：**没有 transport、没有 action 名、没有权限名单**。
    怎么发由核心决定（群里 **@ 那个人** / 私聊直接发；见
    `runtime._SeamBinder.remind_send`）。

    `channel` 是**明说发到哪一类地方**（`"group"` / `"private"`）；留空串 = 老插件没给，
    核心按 `group_id` 推（给了群号就是群、没给就是私聊）。**为什么要这个字段**：
    独立审查 2026-10-09（`docs/audits/review-remind-2026-10-09.md` 第 9 条）实测——
    插件那一侧说的是"发到群里"、回复也说"我在群里 @ 你"，而 `group_id` 是空的，
    核心于是**悄悄按私聊发了**：等于把群里该说的话塞进私聊。现在说 `"group"` 却缺群号
    = **不合法**：什么都不发、如实回 `no_target`（判据见 `runtime._SeamBinder.remind_send`）。
    """

    person_id: str
    text: str
    group_id: str = ""
    channel: str = ""


@dataclass(frozen=True, slots=True)
class RemindSendResult:
    """核心**如实回**的投递结果（不抛异常出去；计数与重试归插件那侧）。

    `reason` 就是失败分类：`ok` / `no_target` / `not_delivered`（确定没送出去，可重发）/
    `uncertain`（不知道，别重发）/ `rejected`（对面拒收，别重发）/ `error`（别的异常）。
    """

    sent: bool
    reason: str
    detail: str = ""


def find_email(text: str) -> str | None:
    """从一句话里认出邮箱；认不出返回 None（保守：必须是完整形状）。"""

    found = _EMAIL.search(text or "")
    return found.group(0) if found else None


def _clock(body: str) -> tuple[int, int] | None:
    """取出钟点（小时、分钟）；没有就 None。`20:30` 优先于 `8点`。"""

    hm = _CLOCK_HM.search(body)
    if hm is not None:
        hour, minute = int(hm.group(1)), int(hm.group(2))
        return (hour, minute) if hour <= 23 and minute <= 59 else None
    at = _CLOCK_HOUR.search(body)
    if at is None:
        return None
    hour = int(at.group(1))
    minute = int(at.group(2)) if at.group(2) else 0
    if hour > 23 or minute > 59:
        return None
    return hour, minute


def _day(body: str) -> tuple[int, str] | None:
    """取出日子词 → （往后几天，自带的时间段）；没有日子词就 None。"""

    for word, offset, daypart in _DAY_WORDS:
        if word in body:
            return offset, daypart
    return None


def _daypart(body: str, fallback: str) -> tuple[str, int] | None:
    """取出时间段 → （名字，12 小时制基准）；没有就用 `fallback`（可能为空）。"""

    for word, base in _DAYPARTS:
        if word in body:
            return word, base
    if fallback:
        for word, base in _DAYPARTS:
            if word == fallback:
                return word, base
    return None


def _hour_24(hour: int, daypart: tuple[str, int] | None) -> int | None:
    """把 12 小时制的钟点换算成 24 小时制。认不清返回 None。"""

    if daypart is None:
        return hour if hour <= 23 else None
    name, base = daypart
    if base == 12:
        if hour == 12:
            # "中午12点"是当天正午；"晚上12点"其实是次日 0 点——后者不猜。
            return 12 if name == "中午" else None
        return hour + 12 if hour < 12 else None
    if hour == 12:
        return 0
    return hour if hour <= 11 else None


def parse_remind_time(text: str, *, now: float | None = None) -> float | None:
    """把"什么时候"的说法算成**绝对时刻**（epoch）；认不清返回 None。

    认这几类（其余一律 None——**不猜**）：

    * 相对：`20分钟后` / `3小时后` / `半小时后` / `2天后`；
    * 带日子：`今天20:30` / `明天8点` / `后天下午3点`；
    * 只有钟点（可带 `早上|上午|中午|下午|傍晚|晚上|夜里|凌晨`）：取**下一次**这个钟点
      ——今天还没到就今天，已经过了就明天；
    * `今晚8点` / `明晚8点` 这类说法同上（日子词自带时间段）。

    **带日子却已经过去**（例如 21:00 时说"今天20:30"）→ None：那是说不清，不是"改天"。
    **没有钟点**（只说"明天提醒我"）→ None：一整天都算"到点"等于没说。

    时区用核心那份显式配置（`dev_config.local_timezone()`），不看部署机器的时区。
    """

    body = (text or "").strip()
    if not body:
        return None
    moment = time.time() if now is None else float(now)
    tz = dev_config.local_timezone()
    base = datetime.fromtimestamp(moment, tz=tz)

    # 1) 相对：`20分钟后` 这一类（`半小时后` 先看，免得被 "半" 后面的数字规则抢走）。
    if _HALF_HOUR.search(body):
        return moment + 1800.0
    for pattern, unit in _RELATIVE_UNITS:
        found = pattern.search(body)
        if found is not None:
            return moment + float(found.group(1)) * unit

    # 2) / 3) 钟点（必须给钟点；只给"明天"不算）。
    clock = _clock(body)
    if clock is None:
        return None
    hour, minute = clock
    day = _day(body)
    daypart = _daypart(body, day[1] if day is not None else "")
    hour24 = _hour_24(hour, daypart)
    if hour24 is None:
        return None

    target = datetime(base.year, base.month, base.day, hour24, minute, tzinfo=tz)
    if day is not None:
        target += timedelta(days=day[0])
        # 说了哪一天、却已经过了 → 说不清，不猜。
        return target.timestamp() if target.timestamp() > moment else None
    # 没日子：取**下一次**这个钟点。
    if target.timestamp() <= moment:
        target += timedelta(days=1)
    return target.timestamp()
