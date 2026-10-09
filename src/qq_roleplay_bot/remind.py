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
   也不携带任何回调**，所以插件拿不到记忆 API（测试逐个字段钉住）；
5. `RemindEmailBook`：**核心自己那一份按人的邮箱**（用户在聊天里说过的那一次），
   **落盘在核心自己的状态文件**里（`data/remind_emails.json`，**不是** `data/memory/`），
   重启后仍在——他上次给过的地址不该再问一遍。**它不是"从记忆库回查"**：
   记忆库那条路只负责把"他说过邮箱"这句话**记进去**（按人），回查这一步没有做，
   也正因为如此才有这个书（口径见独立审查 2026-10-09 第 10 条）。

插件那一半（收下之后怎么存、到点怎么发、后台节拍）在插件里，核心一个字不管；
插件没装时核心**什么都不做**（不报错、不回复）——见 `DialogueEngine.remind` 的说明。
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from . import dev_config

logger = logging.getLogger(__name__)

#: 核心那一份按人邮箱最多记多少人（超出淘汰**最旧**的一条）。
#: 与 `stage3_main` 里那些"按人小表"同一个纪律：有上限，不许无限长。
MAX_REMIND_EMAILS = 256

#: "同一件事"在多长时间里**只办一次**（秒）。
#:
#: 2026-10-09 用户实测（富哥之家）：同一句提醒请求被**每一轮**重新认出来——核心连记 5 次
#: 待办、插件那边**生成了两条提醒**（一次时间不对、一次是 +5 分钟）。重复来自"那句话还在
#: 判定读得到的窗口里"。10 分钟够盖住一轮对话里的反复识别，又不会把"过一阵子又提同一件事"
#: 挡掉（那才是真新请求）。
REMIND_DEDUPE_WINDOW_SECONDS = 600

#: 上面那张"已经认过的同一件事"表最多几行（超出淘汰最旧的一条）。
MAX_REMIND_SEEN = 256

#: 判定给的"他想怎么收"（1/2/3）→ 核心这边的话。
#: 词与投递那条 `RemindDelivery.channel` 对齐（`group` / `private`），第三档是邮箱。
REMIND_CHANNEL_WORDS = {"1": "group", "2": "private", "3": "email"}

#: 状态文件的版本号（照 `state_store.STATE_VERSION` 的做法：读出来对不上就按空的重来）。
REMIND_EMAILS_VERSION = 1

#: 中文数字（日常说法就这些）→ 数值。`两` 当 2（"两个小时"），`十` 参与组合。
_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_CHARS = "".join(_CN_DIGITS) + "十"
#: 一个"数"的写法：阿拉伯数字，或中文数字（`五` / `两` / `十五` / `二十五`）。
_NUMBER = rf"(?:\d+|[{_CN_CHARS}]+)"
#: 光说这些中文钟点**不认**——日常另有所指（"一点儿"、"两点意见"）。
#: 用户 2026-10-09 实测报的是"五分钟后"认不出，所以中文数字必须认；
#: 但认的范围只到"日常说法"，不往自然语言解析器上做。
_BARE_CN_HOURS_REFUSED = {"一", "两", "二"}


def _number(raw: str) -> int | None:
    """把 `五` / `十五` / `二十五` / `5` 这类写法换成整数；认不出返回 None。

    **只认日常写法**：单个中文数字（含 `两`）、`十`、`十X`、`X十`、`X十Y`（一到九十九）。
    别的（`五五`、`一百`、`廿`…）一律 None——**宁可不认，也不猜**。
    """

    text = (raw or "").strip()
    if not text:
        return None
    if text.isascii() and text.isdigit():
        return int(text)
    if "十" not in text:
        return _CN_DIGITS.get(text)
    head, _, tail = text.partition("十")
    if "十" in tail:
        return None                      # "十十"、"二十五十" 这种不认
    tens = 1 if head == "" else _CN_DIGITS.get(head)
    ones = 0 if tail == "" else _CN_DIGITS.get(tail)
    if tens is None or ones is None:
        return None
    return tens * 10 + ones


#: 相对说法的单位：`20分钟后` / `五分钟` / `半小时后` / `两个小时后` / `3天后` / `两周后`。
#: **只收这些写法**；"一会儿""晚点""待会儿"一律不认（不猜）。
#: `分钟` / `小时` 的"后"可以省（它们本身就是时长）；`秒` / `天` / `周` 要带"后"
#: （光说"两天"多数是"两天没睡了"那种意思，不是时限）。
_RELATIVE_UNITS: tuple[tuple[re.Pattern[str], float], ...] = (
    (re.compile(rf"({_NUMBER})\s*秒\s*后"), 1.0),
    (re.compile(rf"({_NUMBER})\s*个?\s*分钟\s*后?"), 60.0),
    (re.compile(rf"({_NUMBER})\s*分\s*后"), 60.0),
    (re.compile(rf"({_NUMBER})\s*个?\s*小时\s*后?"), 3600.0),
    (re.compile(rf"({_NUMBER})\s*天\s*后"), 86400.0),
    (re.compile(rf"({_NUMBER})\s*个?\s*周\s*后"), 7 * 86400.0),
)
_HALF_HOUR = re.compile(r"半\s*(?:个)?\s*小时\s*后?")
_ONE_AND_HALF_HOUR = re.compile(r"(?:一|1)\s*个\s*半\s*小时\s*后?")

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
#: 钟点：`8点` / `8点30` / `8点半` / `三点` / `下午三点半` / `明天八点二十`。
#: 数与单位都可以是中文（用户 2026-10-09 实测："五分钟后"那种说法必须认）。
_CLOCK_HOUR = re.compile(rf"({_NUMBER})\s*点\s*(?:半|(?:({_NUMBER})\s*分?)?)")
#: "刻"我们不认（三点一刻 ≠ 3:00）——撞上就不填，别猜。
_CLOCK_QUARTER = re.compile(r"\s*(?:一|两|二|三|四|半)?\s*刻")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")


@dataclass(frozen=True, slots=True)
class RemindIntent:
    """判定认出来的"这是提醒请求"：**时间怎么说**（原话）+ **提醒什么事** + **他想怎么收**。

    `channel` 是判定从聊天里读到的那个选择（`"1"` 群里 @ / `"2"` 私聊 / `"3"` 邮箱），
    空串 = 他还没说。它**不是**另一次调用：与 `time_text` / `what` 挤在同一个
    `<remind>` 字段里（判定 prompt 的大小有测试钉着，另开一个标签会顶破它）。
    `remind_channel()` 把它翻成核心的说法（`group` / `private` / `email`）。
    """

    time_text: str
    what: str
    channel: str = ""


@dataclass(frozen=True, slots=True)
class RemindRequest:
    """递给插件的那一条提醒。**只有这几个字段**——没有引擎、没有记忆服务、没有回调。

    `at` 是**绝对时刻**（epoch 秒，墙上时间）：核心算好了才交出去，插件不做时间推算
    （它拿不到"群里这边"是哪个时区）。`email` 是**核心手里那一份按人的邮箱**
    （可能为空串）：来自他在聊天里说过的那一次，核心记在 `RemindEmailBook` 里
    （落盘，重启后仍在）。**没有"从记忆库回查"这一步**——记忆库那条路只负责把
    "他说过邮箱"这句话记进去（按人）；这里如实说清，不是"查出来的"
    （独立审查 2026-10-09 第 10 条）。插件要邮箱只能从这里拿，拿不到记忆。
    """

    person_id: str
    session_id: str
    group_id: str
    at: float
    what: str
    email: str = ""
    #: 他想**怎么收到**这条提醒：`"group"`（群里 @ 他）/ `"private"`（私聊）/ `"email"`（寄邮箱）。
    #: 留空串 = **还没问清**——那种情况下核心**不把请求交给插件**（先让她自己问一句，
    #: 见 `DialogueEngine._try_hand_over_remind`）。2026-10-09 用户实测那条"乱"就是这么来的：
    #: 频道没定就把请求交出去，插件只好自己发一条罐头问话，于是同一件事两个声音在群里说。
    channel: str = ""


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


def remind_channel(raw: str) -> str:
    """判定给的那点东西（`1` / `2` / `3`，可能带着"群里 @"这种尾巴）→ 核心的说法。

    认不出、或者他还没说 → 空串（**不猜**：猜一个就可能把话送到错的地方）。
    """

    for char in str(raw or ""):
        word = REMIND_CHANNEL_WORDS.get(char)
        if word is not None:
            return word
    return ""


def state_persistence_enabled() -> bool:
    """是否落盘（**与运行状态那套同一个开关**：`QQBOT_STATE_PERSIST=0/false/off/no` 关掉）。

    为什么要落盘：用户要求"**邮箱只第一次要，之后记住**"（2026-10-08）——只在内存里
    记着，重启就忘，等于每次都重新问一遍（独立审查 2026-10-09 第 10 条）。

    为什么**不**直接 `from .runtime import state_persistence_enabled`：
    `runtime` 在模块层 import 本模块（它要 `RemindDelivery` / `RemindSendResult`），
    模块层再反向 import 就是循环。这里**只在调用时**懒取那一份实现，
    逻辑一处都不重复（离线测试入口把它设成 0，所以测试默认不碰磁盘）。
    """

    from .runtime import state_persistence_enabled as enabled

    return enabled()


def remind_emails_path() -> Path:
    """核心那份按人邮箱落在哪：`QQBOT_REMIND_EMAILS_FILE` → `QQBOT_DATA_DIR` → `data/`。

    写法照 `state_store.state_path_default()`。**刻意不是 `data/memory/`**：
    那是记忆库（模型写的东西），这里是核心自己的一条流程状态，两回事。
    """

    explicit = os.environ.get("QQBOT_REMIND_EMAILS_FILE", "").strip()
    if explicit:
        return Path(explicit)
    base = os.environ.get("QQBOT_DATA_DIR", "").strip()
    root = Path(base) if base else Path(__file__).resolve().parents[2] / "data"
    return root / "remind_emails.json"


class RemindEmailBook:
    """**核心那一份按人的邮箱**：落盘、重启后仍在、读写失败一律降级。

    形状与失败口径都照 `state_store.RuntimeStateStore`：标准库 JSON、原子写入
    （临时文件 + `os.replace`）、任何读写失败只记一行日志并"按空的重来"——
    绝不阻塞对话。

    **它不碰记忆库**：路径自己算（`remind_emails_path`），默认落在 `data/` 下。
    """

    def __init__(self, path: Path | str | None = None, *,
                 limit: int = MAX_REMIND_EMAILS, persist: bool | None = None) -> None:
        self.path = Path(path) if path is not None else remind_emails_path()
        self.limit = max(1, int(limit))
        self.persist = state_persistence_enabled() if persist is None else bool(persist)
        self.last_error = ""
        self._emails: dict[str, str] = {}
        if self.persist:
            self._load()

    # --- 读 ---------------------------------------------------------------

    def get(self, person_id: str, default: str = "") -> str:
        return self._emails.get(str(person_id), default)

    def as_dict(self) -> dict[str, str]:
        return dict(self._emails)

    def __contains__(self, person_id: object) -> bool:
        return str(person_id) in self._emails

    def __len__(self) -> int:
        return len(self._emails)

    def _load(self) -> None:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except OSError as exc:
            self.last_error = type(exc).__name__
            logger.warning("remind_emails_read_failed category=%s", type(exc).__name__)
            return
        try:
            data = json.loads(raw)
        except ValueError:
            self.last_error = "invalid_json"
            logger.warning("remind_emails_invalid_json; starting empty")
            return
        if not isinstance(data, dict) or data.get("version") != REMIND_EMAILS_VERSION:
            self.last_error = "unsupported_version"
            logger.warning("remind_emails_unsupported_version; starting empty")
            return
        people = data.get("emails")
        if not isinstance(people, dict):
            self.last_error = "invalid_shape"
            logger.warning("remind_emails_invalid_shape; starting empty")
            return
        # 只收"人 → 邮箱"这种两段字符串；别的键值一律丢掉（坏文件不许污染内存）。
        for key, value in people.items():
            if isinstance(key, str) and isinstance(value, str) and key and value:
                self._emails[key] = value
        while len(self._emails) > self.limit:
            self._emails.pop(next(iter(self._emails)), None)

    # --- 写 ---------------------------------------------------------------

    def remember(self, person_id: str, email: str) -> None:
        """记下他的邮箱（**立刻落盘**）——下一次提醒、甚至重启之后都还用得上。"""

        person_id, email = str(person_id), str(email)
        if not person_id or not email:
            return
        self._emails[person_id] = email
        while len(self._emails) > self.limit:
            self._emails.pop(next(iter(self._emails)), None)
        self._save()

    def _save(self) -> None:
        if not self.persist:
            return
        payload = json.dumps({"version": REMIND_EMAILS_VERSION, "emails": self._emails},
                             ensure_ascii=False, indent=2)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # 原子写入：同目录临时文件 + 替换（照 `state_store` 的做法）。
            handle = tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=str(self.path.parent), delete=False,
                prefix=self.path.name + ".", suffix=".tmp")
            try:
                with handle:
                    handle.write(payload)
                os.replace(handle.name, self.path)
            except BaseException:
                try:
                    os.unlink(handle.name)
                except OSError:
                    pass
                raise
        except OSError as exc:
            self.last_error = type(exc).__name__
            logger.warning("remind_emails_write_failed category=%s", type(exc).__name__)


def _clock(body: str, *, bare_cn_allowed: bool) -> tuple[int, int] | None:
    """取出钟点（小时、分钟）；没有就 None。`20:30` 优先于 `8点` / `三点`。

    数与单位都认中文（用户 2026-10-09 实测："五分钟后"这类说法必须认）。
    `bare_cn_allowed=False` 时**不认光说的中文钟点里的一/两/二**
    （"一点儿""两点意见"日常另有所指）——带"下午/明天"那种就照认。
    """

    hm = _CLOCK_HM.search(body)
    if hm is not None:
        hour, minute = int(hm.group(1)), int(hm.group(2))
        return (hour, minute) if hour <= 23 and minute <= 59 else None
    at = _CLOCK_HOUR.search(body)
    if at is None:
        return None
    raw = at.group(1)
    if _CLOCK_QUARTER.match(body[at.end():]):
        return None                      # "三点一刻"：刻不认识，不猜
    if not bare_cn_allowed and not raw.isascii() and raw in _BARE_CN_HOURS_REFUSED:
        return None
    hour = _number(raw)
    if hour is None or hour > 23:
        return None
    raw_minute = at.group(2)
    if "半" in at.group(0):
        minute = 30
    elif raw_minute:
        minute = _number(raw_minute)
        if minute is None:
            return None                  # 分钟位认不出（"十点十十"）→ 整条不认
    else:
        minute = 0
    if minute > 59:
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

    * 相对：`20分钟后` / `五分钟` / `3小时后` / `两个小时` / `半小时后` / `一个半小时后` /
      `2天后` / `两周后` / `30秒后`（`分钟`/`小时` 的"后"可省，"秒"/"天"/"周"要带）；
    * 带日子：`今天20:30` / `明天8点` / `后天下午3点` / `明天下午三点`；
    * 只有钟点（可带 `早上|上午|中午|下午|傍晚|晚上|夜里|凌晨`）：取**下一次**这个钟点
      ——今天还没到就今天，已经过了就明天；
    * `今晚8点` / `明晚8点` / `今晚八点` 这类说法同上（日子词自带时间段）。

    **数与单位都认中文**（`一二三四五六七八九十` / `两` / `十` / `十五` / `二十五`）：
    这是 2026-10-09 用户实测报的问题——他说"**五分钟后**提醒我给 dsh 反馈插件运行情况"，
    而那时候只有阿拉伯数字能认。**只认日常说法，不做自然语言解析器**：
    `五五` / `一百` / `廿` 这种一律 None；光说的中文钟点里 `一点` / `两点` 也不认
    （"一点儿"、"两点意见"日常另有所指），要说得上"三点"这种或者带上时间段；
    `三点一刻` 的"刻"不认。

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

    # 1) 相对：`20分钟后` 这一类（`一个半小时` / `半小时` 先看，
    #    免得被 "半" 后面的数字规则抢走）。
    if _ONE_AND_HALF_HOUR.search(body):
        return moment + 5400.0
    if _HALF_HOUR.search(body):
        return moment + 1800.0
    for pattern, unit in _RELATIVE_UNITS:
        found = pattern.search(body)
        if found is None:
            continue
        value = _number(found.group(1))
        if value is None:
            continue                     # 数认不出（"五五"）→ 当没这条，接着试别的
        return moment + float(value) * unit

    # 2) / 3) 钟点（必须给钟点；只给"明天"不算）。
    day = _day(body)
    daypart = _daypart(body, day[1] if day is not None else "")
    clock = _clock(body, bare_cn_allowed=(day is not None or daypart is not None))
    if clock is None:
        return None
    hour, minute = clock
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
