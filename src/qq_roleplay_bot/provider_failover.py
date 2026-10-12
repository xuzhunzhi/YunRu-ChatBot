"""余额耗尽（HTTP 402）时把**所有**模型调用整条切到兜底那一家——保命功能。

一句话形状：默认一律用主家；某一趟被拒成"余额耗尽"时，进程内记一个状态，
之后 `endpoint_for(用途)` 改答 `model_config.resolve_fallback(用途)`；等主家的
余额探针说又能用了，再整条切回主家。这是"让她还能说话"，不是性能优化。

为什么地址不能与 key 分开取
--------------------------
一个 client 的 `base_url` / `api_key` / `model` **必须**由**同一条 key 条目**
解析出来。这条不变式有两条天天在跑的钉子：

- `tests/test_model_config.py::test_the_address_never_comes_from_a_second_place`
- `tests/test_model_config.py::test_changing_a_key_provider_moves_the_address_with_it`

2026-10-06 那次三系统 401 就是破了它：`QQBOT_API_BASE_URL` 指着 mimo，而
judge/memory/review 拿的是 DeepSeek 的 key。所以本模块切换时**整份来源一起换**：
`model_config.resolve_fallback(task)` 返回的就是一个正常 `TaskEndpoint`，地址只
可能来自那条 key 自己那家（`provider_base_url(source.provider)`）。
本模块**不读**任何 `*_API_BASE_URL`、不自己拼地址、不自己读 key。
"""

from __future__ import annotations

import logging
import threading
import time

from . import llm_client, model_config
from .balance_client import BalanceClient

logger = logging.getLogger(__name__)

#: 两种模式的名字（`status_line` 与测试都用它们）。
PRIMARY = "primary"
FALLBACK = "fallback"

#: 记账角色 → 用途名（`runtime._TASK_TO_ROLE` 的**反面**）。
#:
#: 为什么不 import runtime：`llm_client` → 本模块这条链必须在"只有库"的进程里
#: 也成立（测试与工具脚本都直接 import 这个包，不起引擎）。所以这张小表与
#: `runtime._TASK_TO_ROLE` 必须一致——不一致的后果正是"切了却没生效"。
ROLE_TO_TASK: dict[str, str] = {
    "dialogue": "reply",
    "reply": "reply",
    "letter": "letter",
    "judge": "judge",
    "memory": "memory",
    "review": "review",
    "vision": "vision",
}

#: 兜底模式下这些用途的闸放宽到多少秒（**别把这些数字散落到别处**）。
#:
#: 依据（2026-10-12 实测）：把真实 judge 形状在 `mimo-v2.6-pro` 上重放，延迟
#: 中位 36.76s / 最大 55.35s——主家那 5 秒的闸（`_judge_client_factory` 里的
#: `api_timeout("judge", default=5.0)`）在兜底上等于每次都丢。mimo 整体比
#: deepseek-flash 慢一个量级，所以统一给到 60 秒级；`memory` 本来就是长文维护
#: （主家按 `model_timeout - 1` 给，量级也是分钟），兜底给 120 秒。
#:
#: **待用户复核**：放宽哪些用途、放宽到多少，直接改这张表就行。表里没有的用途
#: （含 `letter`、`vision`）原样沿用调用方给的 default。
FALLBACK_TIMEOUTS: dict[str, float] = {
    "reply": 60.0,
    "judge": 60.0,
    "memory": 120.0,
    "review": 60.0,
}

#: 余额探针的节拍（秒）：只在兜底模式下真的发请求，所以给得稀疏些。
#: 它打的是 `GET {base_url}/user/balance`，**不花任何一次真实发言/判定**。
PROBE_INTERVAL_SECONDS = 300.0

#: 记在 client 实例上的私有属性名：切兜底前那道闸是多少秒（切回主家要还回去）。
_TIMEOUT_ATTR = "_failover_primary_timeout"

#: 进程内单例状态。**没有它就没有这个功能**：`mode` 一改，后面所有装配点
#: 都按新的一档解析端点。
_lock = threading.Lock()
_mode = PRIMARY
_since = 0.0
_reason = ""
_primary_provider = ""
_task = ""
_moved = 0


def mode() -> str:
    """现在这一档（`PRIMARY` / `FALLBACK`）。"""

    return _mode


def in_fallback() -> bool:
    """现在是不是在兜底模式。"""

    return _mode == FALLBACK


def task_of_role(role: str) -> str:
    """记账角色 → 用途名（认不出来返回空串）。"""

    return ROLE_TO_TASK.get(str(role or "").strip().casefold(), "")


def task_of_client(client: object) -> str:
    """一个 client 属于哪个用途：看它的 `usage_role`。"""

    return task_of_role(str(getattr(client, "usage_role", "") or ""))


def endpoint_for(task: str) -> model_config.TaskEndpoint:
    """某个用途**当前**该用的端点。

    primary 模式下**逐字**就是 `model_config.require(task)`（这是不变式：没切
    兜底时运行期一个字节都不受影响）；兜底模式下改答
    `model_config.resolve_fallback(task)`——那一家接不住这个用途（或没配兜底 key）
    时**老实回 `require(task)`**，让这一路明确失败，而不是硬凑一个端点。
    """

    if _mode != FALLBACK:
        return model_config.require(task)
    endpoint = model_config.resolve_fallback(task)
    if endpoint is None:
        return model_config.require(task)
    return endpoint


def note_balance_exhausted(provider: str, *, task: str = "") -> bool:
    """记下"这一家余额耗尽了"。**幂等**：只有"真的切了"才返回 True。

    返回 False 的几种情况（都不许切换，免得来回弹）：
    ① 已经在兜底模式；
    ② 没给用途（`task` 空）——那时判不出"这一家是不是主家那一路"，宁可不动
       （调用方都给得出用途：`llm_client` 用 `task_of_role`）；
    ③ 报 402 的这一家**不是这个用途在 primary 模式下用的那一家**（比如在 mimo 上
       的写信那一路被拒，那不是"主家余额耗尽"）；
    ④ 兜底那一路**拿不出端点**（没配兜底 key / 这个用途不在兜底表里）：切了也没
       地方去，不如不切，调用方按原样如实失败。
    """

    name = str(provider or "").strip().casefold()
    key = str(task or "").strip().casefold()
    global _mode, _since, _primary_provider, _task, _reason, _moved
    if not name or key not in model_config.TASKS:
        return False
    if _mode == FALLBACK:
        return False
    try:
        if model_config.resolve(key).provider != name:
            return False
        endpoint = model_config.resolve_fallback(key)
    except Exception as exc:  # noqa: BLE001 - 解析不出来就当作"没得切"
        logger.warning("余额兜底：解析兜底来源失败，本次不切换（%s）", exc)
        return False
    if endpoint is None or not endpoint.key_present():
        return False
    with _lock:
        if _mode == FALLBACK:  # 双重检查：并发的第二个 402 不许再切一次
            return False
        _mode = FALLBACK
        _since = time.time()
        _primary_provider = name
        _task = key
        _reason = f"{key} 报 HTTP 402（额度用尽）"
        _moved = 0
    logger.warning(
        "余额耗尽：整条切到兜底那一家（%s 报 HTTP 402）→ provider=%s model=%s；"
        "主家余额恢复后会自动切回",
        key,
        endpoint.provider,
        endpoint.model,
    )
    return True


def reapply(clients=None) -> int:
    """把**已经存在**的 client 实例改到当前模式对应的端点上，返回改了几个。

    primary 模式下**改 0 个、不打任何日志**：这是不变式——没切兜底时这条路径对
    运行期没有任何可观察的影响（`runtime.apply_overrides` 每次面板操作之后都会调
    它，就为了别让"面板写一次"把已经切走的 client 悄悄拉回主家）。
    """

    if _mode != FALLBACK:
        return 0
    return _repoint(clients, FALLBACK)


def effective_timeout(task: str, default: float) -> float:
    """这一趟该用多长的闸：primary **原样返回 `default`**；兜底给更宽的那一档。

    调用方先把既有的 `dev_config.api_timeout("<用途>", default=...)` 结果当
    `default` 传进来——操作者的 `QQBOT_<用途>_TIMEOUT` 因此照旧说了算；这里只在
    兜底模式下把它跟 `FALLBACK_TIMEOUTS` 取**较大**那个，其余什么都不做。
    """

    value = float(default)
    if _mode != FALLBACK:
        return value
    widened = FALLBACK_TIMEOUTS.get(str(task or "").strip().casefold())
    if widened is None:
        return value
    return max(value, float(widened))


def remember_primary_timeout(client: object, timeout: float) -> None:
    """把"这个 client 在主家那道闸是多少秒"记下来（切回主家时还回去）。

    只在**兜底模式期间新建** client 的构造点需要调它：那时构造出来的闸已经放宽了，
    不记原值的话，切回主家后它会一直带着放宽后的闸。
    """

    try:
        setattr(client, _TIMEOUT_ATTR, float(timeout))
    except Exception:  # noqa: BLE001 - 记不下来就只是"闸宽一点"，不值得炸
        pass


def restore_primary(*, reason: str = "") -> bool:
    """切回主家。只在余额探针说主家又能用了的时候调；返回是否真的切回来了。

    切回来这件事必须**自己**把已存在的 client 指回主家：`reapply()` 在 primary
    模式下是**故意不动**的（见它的 docstring），所以这里走内部那条两档通用的路。
    """

    global _mode, _since, _primary_provider, _task, _reason, _moved
    with _lock:
        if _mode != FALLBACK:
            return False
        _mode = PRIMARY
        _since = 0.0
        _reason = ""
        _primary_provider = ""
        _task = ""
        _moved = 0
    changed = _repoint(None, PRIMARY)
    logger.warning(
        "余额恢复：整条切回主家（%s；重指向 %s 个 client）",
        reason or "余额探针",
        changed,
    )
    return True


def status_line() -> str:
    """给 `/super status` 的一行：**模式 + 起始时间 + 原因**，绝不含 key 值。

    不并进 `model_config.self_check_lines()`：那一份是"每个用途一行"的解析表，
    条数被用例钉死；这一行是运行期状态，属于 status 那一面。

    用词上**故意不写"余额"两个字**：概览与对账是分工的（余额数字走
    `/super apicheck`），`test_api_report.py::test_status_shows_uptime_and_memory_
    but_not_hit_rate` 钉着"概览里不出现余额"。这里说的是**来源被切到了哪一家**，
    所以写"402 / 额度用尽"。
    """

    if _mode != FALLBACK:
        return "模型来源：主家（未切换）"
    started = time.strftime("%H:%M:%S", time.localtime(_since)) if _since else "未知"
    moved = f"；已重指向 {_moved} 个 client" if _moved else ""
    return (f"模型来源：兜底（{_primary_provider} 报 HTTP 402 额度用尽，"
            f"{_task}）——自 {started} 起{moved}；原因：{_reason}")


def reset_for_tests() -> None:
    """把进程内状态清回 primary（测试用；不碰任何 client）。"""

    global _mode, _since, _primary_provider, _task, _reason, _moved
    with _lock:
        _mode = PRIMARY
        _since = 0.0
        _reason = ""
        _primary_provider = ""
        _task = ""
        _moved = 0


def _repoint(clients, mode_wanted: str) -> int:
    """按指定那一档把已存在的 client 整条指过去，返回真的改了几个。"""

    targets = list(llm_client.clients() if clients is None else clients)
    changed = 0
    labels: list[str] = []
    for client in targets:
        task = task_of_client(client)
        if not task:
            continue
        try:
            if mode_wanted == FALLBACK:
                endpoint = model_config.resolve_fallback(task)
            else:
                endpoint = model_config.resolve(task)
        except Exception as exc:  # noqa: BLE001 - 一路解析失败不该拖垮其余
            logger.warning("模型来源切换：这一路解析不出端点，跳过（task=%s；%s）", task, exc)
            continue
        if endpoint is None or not endpoint.key_present():
            continue
        timeout = _timeout_for(client, task, mode_wanted)
        if _assign(client, endpoint, timeout):
            changed += 1
            labels.append(task)
    if changed:
        global _moved
        _moved = changed
        logger.warning(
            "模型来源切换：%s 个 client 已整条改到%s（用途：%s）",
            changed,
            "兜底那一家" if mode_wanted == FALLBACK else "主家",
            "、".join(sorted(set(labels))),
        )
    return changed


def _timeout_for(client: object, task: str, mode_wanted: str) -> float:
    """切过去时这个 client 该配多长的闸；顺手把主家那道闸的原值记住/还回去。"""

    current = float(getattr(client, "timeout", 60.0) or 60.0)
    if mode_wanted == FALLBACK:
        if getattr(client, _TIMEOUT_ATTR, None) is None:
            remember_primary_timeout(client, current)
        return effective_timeout(task, current)
    saved = getattr(client, _TIMEOUT_ATTR, None)
    if saved is None:
        # 兜底期间建的 client 没记过原值：闸保持现状（宽一点只是更耐心，不会误杀）。
        return current
    try:
        delattr(client, _TIMEOUT_ATTR)
    except Exception:  # noqa: BLE001 - 删不掉不影响还值
        pass
    return float(saved)


def _assign(client: object, endpoint: model_config.TaskEndpoint, timeout: float) -> bool:
    """就地把一个 client **整条**指到 `endpoint` 上（四件套 + 闸）。返回是否动了。"""

    base_url = str(endpoint.base_url or "").rstrip("/")
    touched = False
    if str(getattr(client, "base_url", "") or "").rstrip("/") != base_url:
        client.base_url = base_url
        touched = True
    if getattr(client, "api_key", "") != endpoint.api_key:
        client.api_key = endpoint.api_key
        touched = True
    if getattr(client, "model", "") != endpoint.model:
        client.model = endpoint.model
        touched = True
    # `provider` 决定请求体要不要带 DeepSeek 专有字段（见 `_complete_sync`），
    # 也是"402 报的是不是主家那一路"的判据（见 `note_balance_exhausted`）。
    if str(getattr(client, "provider", "") or "") != str(endpoint.provider or ""):
        client.provider = str(endpoint.provider or "")
        touched = True
    if timeout and abs(float(getattr(client, "timeout", 0.0) or 0.0) - float(timeout)) > 1e-9:
        client.timeout = float(timeout)
        touched = True
    return touched


class BalanceProbe:
    """余额探针：**只在兜底模式**下问一次主家余额，能用就整条切回去。

    它是核心后台通道里的一个节拍任务（`background_plugins.build_background_plugins`
    登记），不是 Stage 4 插件；也不新写 `while True`。

    - primary 模式下 `poll_once()` 立刻返回，**一个请求都不发**；
    - 探的是**主家那一路**（`model_config.resolve(task)` 的地址与 key），
      打 `GET {base_url}/user/balance`——**绝不花掉一次真实发言/判定**；
    - `is_available` 不是 False → `restore_primary()`（`balance_client` 的
      `summarize()` 也是这个口径："is_available is False 才算余额不足"）；
    - 任何异常只记日志，等下一次节拍（外面的 `run_background_plugin` 还会再兜一层）。
    """

    name = "provider-balance-probe"
    interval_seconds = PROBE_INTERVAL_SECONDS
    enabled = True

    def __init__(self, task: str = "") -> None:
        self.task = str(task or "")

    async def poll_once(self) -> object:
        if not in_fallback():
            return None
        task = self.task or _probe_task()
        if not task:
            return None
        try:
            endpoint = model_config.resolve(task)
        except Exception as exc:  # noqa: BLE001 - 探针不该把节拍弄崩
            logger.warning("余额探针：解析不出主家端点，本轮跳过（%s）", exc)
            return None
        if not endpoint.key_present():
            return None
        try:
            payload = await BalanceClient(endpoint.base_url, endpoint.api_key).fetch()
        except Exception as exc:  # noqa: BLE001 - 探不到就是"还没恢复"
            logger.info("余额探针：主家余额还用不了（task=%s；%s）", task, exc)
            return None
        if not isinstance(payload, dict) or payload.get("is_available") is False:
            return None
        restore_primary(reason=f"{task} 余额探针说主家可用")
        return payload


def _probe_task() -> str:
    """拿哪一路去探主家余额：余额是**账号**粒度的，所以随便哪一路活着都行。

    优先 `judge`（本机它一定有 key），否则按 `TASK_NAMES` 顺序找第一个有 key 的。
    """

    order = ["judge", *[name for name in model_config.TASK_NAMES if name != "judge"]]
    for name in order:
        try:
            if model_config.resolve(name).key_present():
                return name
        except Exception:  # noqa: BLE001 - 这一路解析不出来就换下一路
            continue
    return ""
