"""超时通知的**判定**、**措辞**与**发送**——这个插件的全部"内容"都在这里。

它**不认** `mail`、也不认 `outage_notice`：输入是"现在几点 + 一份只读快照"，
输出是"要不要写一封信、写什么"。装配在 `plugin.py`，节拍在 `background.py`。

## 那句话里的两个词怎么落成数字

用户 2026-10-12："短时间内频繁超时则发送邮件通知我"。"短时间"与"频繁"必须变成
确定的数，否则"什么时候会响"就没人说得清。当晚给用户的默认值是
**5 分钟内够 5 次就写一封，写完之后 30 分钟内不再写第二封**（用户可以改）：

| 配置 | 缺省 | 意思 |
| --- | --- | --- |
| `QQBOT_MAIL_TIMEOUT_ALERT` | `1` | 总开关（`0`/`false`/`no`/`off` 关） |
| `QQBOT_MAIL_TIMEOUT_ALERT_WINDOW` | `300` | 窗口多长（秒） |
| `QQBOT_MAIL_TIMEOUT_ALERT_TIMES` | `5` | 窗口内够几次才写 |
| `QQBOT_MAIL_TIMEOUT_ALERT_COOLDOWN` | `1800` | 写过之后多久内不再写 |
| `QQBOT_MAIL_TIMEOUT_ALERT_POLL` | `60` | 多久看一次（后台节拍的间隔） |

为什么门槛比核心那个"连续失败 3 次"（`CONSECUTIVE_MODEL_FAILURE_ALERT`）还高：
那 3 次是给 `/super status` 状态行看的，网络抖一下也算；而一封信是要吵醒人的，
得**确实频繁**才发。那一晚的现场是 5 分钟里十几次，所以 5 次这个门低到能及时响，
又不会因为一次抖动就写信。

## 为什么数"种类"而不是数总数

"超时"在快照里是**按种类计数**的：`EngineSnapshot.metrics["model_errors"]` 的键长得像
`"transport_error: TimeoutError"`（`metrics.record_model_error` 存的是
`LLMError.safe_summary()`，截到 40 字）。判定/回复/记忆/审查**任何一路**的超时
都算数——用户要的是"她老是接不上话"，不关心是哪一路。别的失败（401、余额不足、
格式错）不在这里报警：那些有各自的信号，混进"超时"会让这封信说的不是真事。
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass

from ... import dev_config

logger = logging.getLogger(__name__)

#: 用户那句话落成的四个数（见模块说明）。改它们不必改代码：写在 `.env` 里就行。
DEFAULT_WINDOW_SECONDS = 300.0
DEFAULT_THRESHOLD = 5
DEFAULT_COOLDOWN_SECONDS = 1800.0
DEFAULT_POLL_SECONDS = 60.0

ENV_ENABLED = "QQBOT_MAIL_TIMEOUT_ALERT"
ENV_WINDOW_SECONDS = "QQBOT_MAIL_TIMEOUT_ALERT_WINDOW"
ENV_THRESHOLD = "QQBOT_MAIL_TIMEOUT_ALERT_TIMES"
ENV_COOLDOWN_SECONDS = "QQBOT_MAIL_TIMEOUT_ALERT_COOLDOWN"
ENV_POLL_SECONDS = "QQBOT_MAIL_TIMEOUT_ALERT_POLL"

_FALSE_WORDS = frozenset({"0", "false", "no", "off"})


def _positive_float(name: str, default: float) -> float:
    """读一个正数配置。没写、写坏了、写了 0 或负数——一律用缺省值。

    口径宁可"就按缺省来"，也不要因为 `.env` 里一个手误变成"窗口 0 秒"
    或"阈值 0 次"那种**每轮都发**的样子：告警最怕的不是不响，是乱响。
    """

    raw = dev_config.get(name, "")
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _positive_int(name: str, default: int) -> int:
    """同上，取整数（次数）。"""

    raw = dev_config.get(name, "")
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def enabled() -> bool:
    """总开关，缺省**开**。用户要的是"超时通知我"，不是"先配好才通知"。"""

    return dev_config.get(ENV_ENABLED, "1").strip().lower() not in _FALSE_WORDS


def window_seconds() -> float:
    return _positive_float(ENV_WINDOW_SECONDS, DEFAULT_WINDOW_SECONDS)


def threshold() -> int:
    return _positive_int(ENV_THRESHOLD, DEFAULT_THRESHOLD)


def cooldown_seconds() -> float:
    return _positive_float(ENV_COOLDOWN_SECONDS, DEFAULT_COOLDOWN_SECONDS)


def poll_seconds() -> float:
    return _positive_float(ENV_POLL_SECONDS, DEFAULT_POLL_SECONDS)


#: 快照里"这是超时"的键名特征。核心记的是 `"transport_error: TimeoutError"`
#: 这种形状，所以这里认**小写子串**：`TimeoutError` / `timed out` / `ReadTimeout`
#: 都命中。认子串而不认全等，是因为键里还带着异常类名，攒不全。
TIMEOUT_MARKER = "timeout"


def timeout_count(snapshot: object) -> int | None:
    """从只读快照里数出"这个进程到现在为止一共超时多少次"。

    数不出来返回 `None`，**不是 0**：`None` 的意思是"我看不见"（快照形状不对、
    核心没带指标），`0` 的意思是"看见了，没超时"。两者混起来会让"看不见"
    变成"很安静"——那是告警最糟的失败方式：安静地不报警。
    """

    metrics = getattr(snapshot, "metrics", None)
    if not isinstance(metrics, dict):
        return None
    errors = metrics.get("model_errors")
    if not isinstance(errors, dict):
        return None
    total = 0
    for key, count in errors.items():
        if TIMEOUT_MARKER not in str(key).lower():
            continue
        if isinstance(count, int):
            total += count
    return total


#: 邮件主题。**固定**，只由时间区分（邮箱里按主题一眼认出这是告警）。
SUBJECT = "云茹老是卡住"

#: 正文模板。两条口径都来自《云茹断线了》那封的定稿过程（用户 2026-10-11 三次改口）：
#: **用她那口气**（第一人称、短句、直说，信的性质是"她自己在求人帮忙"而不是
#: 发给运维的通知）、**不要方括号小标题**、**不交代机制**（"这封是自动发的"那种
#: 结尾不留）。语气可以像她，但**仍然是手写定稿、不调模型**——告警不能依赖
#: "模型这一刻可用"，插件也不该去拿她的人格（`AGENTS.md`：插件不许干涉对话）。
BODY_TEMPLATE = (
    "我最近老是卡住。\n"
    "\n"
    "到 {moment} 为止的 {window} 里，我有 {count} 次没能接上话——"
    "等着等着就没下文了，我只好先把那一句咽回去。\n"
    "要是有空，帮我看一眼我这边是不是出了什么问题。"
)

#: 时刻的格式（本地时间 + UTC 偏移，读信的人不必自己换算时区）。
#: 这三行**故意从 `outage_notice` 抄过来、而不是 import 它**：两个插件之间不该有
#: 依赖，删掉一个不能让另一个跟着坏（`tests/test_timeout_alert.py` 有一条钉着
#: "本模块的源码里不出现 `outage_notice`"）。
MOMENT_FORMAT = "%Y-%m-%d %H:%M:%S"


def format_moment(when: float | None = None) -> str:
    """把时刻写成"本地时间（±HH:MM）"。`when` 缺省是此刻。"""

    local = time.localtime(time.time() if when is None else when)
    return time.strftime(MOMENT_FORMAT, local) + time.strftime("（%z）", local)


def window_label(seconds: float) -> str:
    """把窗口写成人话："5 分钟"，不是整分钟就说秒。"""

    if seconds >= 60.0 and seconds % 60.0 == 0.0:
        return f"{int(seconds // 60)} 分钟"
    return f"{int(round(seconds))} 秒"


@dataclass(frozen=True)
class Alert:
    """一次"该写这一封了"的判定结果：内容已经写好，发不发由调用方决定。

    判定与发送分开，是为了让"什么时候会响"这件事**可测**：用例喂一串采样点，
    看的就是这个对象来没来、里面那个 `count` 是多少。
    """

    count: int
    window_seconds: float
    at: float
    subject: str
    body: str


def build_alert(*, count: int, window_seconds: float, at: float) -> Alert:
    """按定稿模板拼一封信。**纯函数**：不碰网络、不碰注册表、不调模型。"""

    body = BODY_TEMPLATE.format(
        moment=format_moment(at),
        window=window_label(window_seconds),
        count=int(count),
    )
    return Alert(
        count=int(count),
        window_seconds=float(window_seconds),
        at=float(at),
        subject=SUBJECT,
        body=body,
    )


class TimeoutWatch:
    """窗口 + 阈值 + 降温：把一串"累计超时数"读成"要不要写一封信"。

    ## 为什么是"累计值求差"

    快照只给**累计次数**（进程内单调增），没有"每次超时发生在几点"。所以这里留一串
    `(时刻, 累计值)` 的采样点，窗口内的次数 = 最新累计 − **窗口内最早那个采样点**的
    累计。代价是少算了一点（窗口起点到那个采样点之间，最多一个采样间隔），
    这是**故意**的：宁可少报一次，不把窗口外的旧账算进来。

    ## 采样点只留窗口里的，而且至少两个才判

    窗口外的采样点一律丢掉——它们记的是断档前的旧账。要是拿它们当基准，
    机器睡一觉起来就会把几小时前的超时算进"最近五分钟"（**唯一的误报来源**）。
    窗口里不足两个采样点，说明"这一段的底"还不知道，这一轮**不判**。

    ## 采样间隔必须明显小于窗口

    采样的粒度就是判定的粒度：60 秒采一次、5 分钟窗口，最坏晚一分钟发现，够。
    反过来说，采样间隔要是跟窗口一个量级，窗口里可能一个采样点都没有，
    那就什么都判不出来（`plugin.py` 会把这种配置记一行日志提醒）。
    """

    def __init__(self, *, window_seconds: float, threshold: int,
                 cooldown_seconds: float) -> None:
        if window_seconds <= 0:
            raise ValueError("window_seconds 必须是正数")
        if threshold < 1:
            raise ValueError("threshold 至少是 1（0 次意味着每轮都写）")
        if cooldown_seconds < 0:
            raise ValueError("cooldown_seconds 不能是负数")
        self.window_seconds = float(window_seconds)
        self.threshold = int(threshold)
        self.cooldown_seconds = float(cooldown_seconds)
        self._samples: deque[tuple[float, int]] = deque()
        self._last_total: int | None = None
        self._last_sent: float | None = None

    def observe(self, now: float, total: int) -> Alert | None:
        """喂一次"现在几点 + 累计超时多少"，回要写的那封信（不写就 `None`）。"""

        if self._last_total is not None and total < self._last_total:
            # 累计值回退 = 计数器不在原来那条时间线上了（换过指标对象 / 进程重启过）。
            # 旧账一笔勾销、重新起算：两份计数器相减出来的是个没有意义的数。
            self._samples.clear()
        self._last_total = int(total)

        self._samples.append((float(now), int(total)))
        cutoff = float(now) - self.window_seconds
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()
        if len(self._samples) < 2:
            return None

        in_window = int(total) - self._samples[0][1]
        if in_window < self.threshold:
            return None
        if self._last_sent is not None and float(now) - self._last_sent < self.cooldown_seconds:
            # 刚写过一封：窗口滑走之前不再写（用户要的是"通知我"，不是"每分钟一封"）。
            return None

        self._last_sent = float(now)
        return build_alert(count=in_window, window_seconds=self.window_seconds, at=now)


async def send_alert(send, alert: Alert) -> bool:
    """把信交给发信口；**任何异常都只记一行、不上抛**，返回"发出去了没有"。

    `send` 是 **mail 插件放上来的那个函数**（`(subject, body) -> await`，见
    `plugins.MailSeams`）——本模块不知道它背后是哪个模块、哪个 CLI、发给谁。

    为什么不往上抛：调用方是核心的后台节拍（`run_background_plugin`），它兜住异常、
    记一行 `后台插件这一轮出错`——那句话说的是"插件写坏了"，而"信没发出去"是**另一
    件事**，得有自己的名字（`timeout_alert_send_failed`）才有排障价值。
    `KeyboardInterrupt` / `SystemExit` 不属于"发信失败"，照旧上抛。
    """

    try:
        await send(alert.subject, alert.body)
    except Exception as exc:  # noqa: BLE001 - 发信失败的代价只能是一行日志
        logger.warning("timeout_alert_send_failed error=%s", type(exc).__name__,
                       exc_info=True)
        return False
    logger.info("timeout_alert_sent count=%d window=%.0fs at=%.0f",
                alert.count, alert.window_seconds, alert.at)
    return True
