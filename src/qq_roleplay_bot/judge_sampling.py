"""判定采样：群消息来得太快时，攒几条再让判定 agent 看一眼。

用户 2026-10-10 的原话：

- *"如果短时间内入栈消息太多则修改判定 agent 调起频率，达到一定量变成 5 条一次，
  达到一定量变成 10 条一次，达到一定量变成 20 条一次"*；
- *"五条跑一次指判定这五条有没有回复的必要而不是 5 条取一条剩下的不管"*。

所以"攒"的只是**判定调用**，不是消息：攒着的消息照进短期历史，判定的输入本来就
是整段历史，一条不丢；攒满判的那一次问的是"这几条里有没有值得回的"。判定说
REPLY 就回一句，NO_REPLY 就不出声、等下一批。

档位表（默认值；全部 `QQBOT_JUDGE_RATE_*` 可配，`dev_config` **调用时现读**）：

    速率（条 / 窗口）      判定行为
    < 8                    每条各判一次（现状，不受影响）
    >= 8                   攒满 5 条判一次
    >= 16                  攒满 10 条判一次
    >= 30                  攒满 20 条判一次

三条边界语义（用户已确认）：

1. **被 @ 的消息不等攒满**：立刻触发判定。判定照旧决定回不回，只是"会被看到"
   这一层必须保住——有人在群里点名她，不能等到第 20 条才回头。
2. **窗口是滚动的**：`deque` 存最近窗口内的入站时间戳，消息量回落档位自然回落，
   下一批回到更小的攒数。
3. **私聊不走采样**：一对一不需要，引擎那边根本不会把私聊会话喂进来。

这个模块只做**计数与放行**：不调模型、不碰消息正文（只存时间戳与条数）、
不知道判定 prompt 长什么样，也不动焦点控制器的排队/调度——它只回答
"这条消息要不要触发判定调用"这一个问题。
"""
from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field

from . import dev_config

logger = logging.getLogger(__name__)

#: 三档的默认攒批条数（`QQBOT_JUDGE_RATE_BATCH_SIZES` 没配 / 全都写坏时的回落）。
DEFAULT_BATCH_SIZES: tuple[int, ...] = (5, 10, 20)


@dataclass
class _SessionSample:
    """一个会话（群）的两份计数。只存数字，不存任何消息正文。"""

    #: 最近窗口内的入站时间戳（单调钟）。滚动窗口本体：读取前先裁掉过期的。
    window: deque[float] = field(default_factory=deque)
    #: 自上次判定以来积攒的条数（判定真正发生时清零）。
    pending: int = 0
    #: 这个会话被采样闸拦下的次数（观测用，`/super status` 会印）。
    skipped: int = 0


def _tier_and_batch(rate: int) -> tuple[int, int]:
    """按当前速率查表 → (档位, 攒多少条才判一次)。

    档位 0 = 每条各判一次（现状）；1/2/3 对应三个阈值档。阈值与攒批条数
    全部**现读** `dev_config`（不是 import 期常量）：改配置不必重启，
    突变测试（"档位查表写死不读环境变量"）靠的就是这一点。
    """

    if rate >= dev_config.judge_rate_tier3():
        tier = 3
    elif rate >= dev_config.judge_rate_tier2():
        tier = 2
    elif rate >= dev_config.judge_rate_tier1():
        tier = 1
    else:
        return 0, 1
    sizes = dev_config.judge_rate_batch_sizes()
    if not sizes:
        sizes = DEFAULT_BATCH_SIZES
    batch = sizes[min(tier - 1, len(sizes) - 1)]
    return tier, max(1, batch)


@dataclass
class JudgeSampler:
    """按会话维护"最近窗口内的入站条数"与"自上次判定以来的积攒条数"。

    引擎在两个时点调它：消息**入历史**时 `note_message`（窗口 + 积攒各 +1），
    消息**要进判定路径之前** `admit`（查表决定放行还是攒着）。判定因为别的
    原因发生了（比如轮到被焦点排队的消息）用 `note_evaluation` 补一次清零——
    判定看得到全部历史，所以它发生的那一刻积攒就算处理完了。
    """

    _samples: dict[str, _SessionSample] = field(default_factory=dict)
    #: 全部会话加起来跳过的总次数（观测用；与会话粒度的那份分开，淘汰会话也不丢）。
    skipped_total: int = 0

    # --- 记一笔 ---------------------------------------------------------------

    def note_message(self, session_id: str, now: float) -> None:
        """一条入站消息进了这个会话的历史：滚动窗口与积攒各记一笔。

        只该在**真的有消息进来**的路径上调用（含"只记不判"的背景路径）；
        补进来的历史背景（`_seed_history` 拉的老消息）不算——那不是"刚刚
        有人说话"，不该把速率顶上去。
        """

        sample = self._samples.get(session_id)
        if sample is None:
            sample = _SessionSample()
            self._samples[session_id] = sample
        sample.window.append(now)
        self._prune(sample, now)
        sample.pending += 1

    def note_evaluation(self, session_id: str) -> None:
        """判定因为采样闸之外的原因发生了（被焦点排队后轮到）：积攒清零。"""

        sample = self._samples.get(session_id)
        if sample is not None:
            sample.pending = 0

    # --- 闸 -------------------------------------------------------------------

    def admit(self, session_id: str, now: float, *, mentioned: bool) -> bool:
        """这条消息要不要触发判定调用。

        - 被 @ → 立刻放行并清零（不等攒满）；
        - 积攒数 ≥ 当前档位的攒批条数 → 放行并清零；
        - 否则 → 不放行（消息照进历史，这是调用方的事），跳过计数 +1，
          记一行 DEBUG（高频路径，不刷 INFO）。
        """

        sample = self._samples.get(session_id)
        if sample is None:
            sample = _SessionSample()
            self._samples[session_id] = sample
        self._prune(sample, now)
        rate = len(sample.window)
        tier, batch = _tier_and_batch(rate)
        if mentioned:
            # @ 豁免：判定照旧决定回不回，只是"会被看到"这一层保住。
            sample.pending = 0
            return True
        if sample.pending >= batch:
            sample.pending = 0
            return True
        sample.skipped += 1
        self.skipped_total += 1
        logger.debug(
            "judge sampling: hold session=%s pending=%s batch=%s tier=%s rate=%s",
            session_id, sample.pending, batch, tier, rate,
        )
        return False

    # --- 观测与清理 -------------------------------------------------------------

    def stats(self, now: float) -> dict[str, object]:
        """给 `/super status` 的一小撮状态：只有档位/条数/次数，没有正文。"""

        sessions: dict[str, dict[str, int]] = {}
        for session_id, sample in self._samples.items():
            self._prune(sample, now)
            if not sample.window and not sample.pending and not sample.skipped:
                continue
            tier, batch = _tier_and_batch(len(sample.window))
            sessions[session_id] = {
                "tier": tier,
                "batch": batch,
                "rate": len(sample.window),
                "pending": sample.pending,
                "skipped": sample.skipped,
            }
        return {"skipped_total": self.skipped_total, "sessions": sessions}

    def forget_session(self, session_id: str) -> None:
        """会话被清理/淘汰时把它的计数摘掉（与 `_drop_session_clients` 同一口径）。"""

        self._samples.pop(session_id, None)

    def _prune(self, sample: _SessionSample, now: float) -> None:
        """滚动窗口：裁掉滑出窗口的旧时间戳。窗口秒数现读配置。"""

        window = dev_config.judge_rate_window_seconds()
        timestamps = sample.window
        while timestamps and now - timestamps[0] > window:
            timestamps.popleft()
