"""脱敏运行指标：只记录固定类别与耗时，绝不记录聊天正文或凭据。

设计约束：
- 计数器键是固定的枚举值（触发原因、决定类型、失败类别），不是自由文本；
- 耗时只保留观测值数量、累计值、最大值和有限窗口的分位数；
- 任何对外输出的字典都不包含会话正文、用户 QQ 号或 API Key。
"""
from __future__ import annotations

import ctypes
import os
import threading
from collections import deque

DECISION_KINDS = ("reply", "no_reply", "exit")
TRIGGER_KINDS = ("mention", "media", "threshold", "active_message", "private_debug", "unknown")
LATENCY_WINDOW = 64


def process_memory_bytes() -> tuple[int, int]:
    """当前进程的内存占用 (工作集, 峰值)，单位字节；取不到就返回 (0, 0)。

    只用标准库：Windows 走 `GetProcessMemoryInfo`，Linux 读 `/proc/self/status`。
    不用 tracemalloc——那要全程开着，对每次分配都记账，为了一个显示数字不值。
    """

    if os.name == "nt":
        try:
            from ctypes import wintypes

            class _Counters(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            # **必须声明 argtypes/restype**：不声明时 HANDLE 会被当成 32 位 int 传，
            # 伪句柄 -1 的高位就丢了，函数直接返回 FALSE（实测拿到 (0, 0)）。
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            psapi = ctypes.WinDLL("psapi", use_last_error=True)
            kernel32.GetCurrentProcess.restype = wintypes.HANDLE
            psapi.GetProcessMemoryInfo.argtypes = [
                wintypes.HANDLE, ctypes.POINTER(_Counters), wintypes.DWORD,
            ]
            psapi.GetProcessMemoryInfo.restype = wintypes.BOOL

            counters = _Counters()
            counters.cb = ctypes.sizeof(counters)
            handle = kernel32.GetCurrentProcess()
            if psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
                return int(counters.WorkingSetSize), int(counters.PeakWorkingSetSize)
        except Exception:  # noqa: BLE001 - 取不到就是没这个数字，不影响任何功能
            return 0, 0
        return 0, 0
    try:
        with open("/proc/self/status", encoding="ascii", errors="replace") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024, 0
    except (OSError, ValueError, IndexError):
        return 0, 0
    return 0, 0


class LatencyWindow:
    """固定长度的滑窗统计，用于观测模型与出站耗时。"""

    __slots__ = ("_samples", "_lock")

    def __init__(self, window: int = LATENCY_WINDOW) -> None:
        self._samples: deque[float] = deque(maxlen=window)
        self._lock = threading.Lock()

    def observe(self, seconds: float) -> None:
        if seconds < 0:
            return
        with self._lock:
            self._samples.append(round(float(seconds), 4))

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            samples = sorted(self._samples)
        if not samples:
            return {"count": 0, "avg": 0.0, "max": 0.0, "p50": 0.0, "p95": 0.0}
        count = len(samples)
        return {
            "count": count,
            "avg": round(sum(samples) / count, 4),
            "max": samples[-1],
            "p50": samples[min(count - 1, int(round(0.50 * (count - 1))))],
            "p95": samples[min(count - 1, int(round(0.95 * (count - 1))))],
        }


class RuntimeMetrics:
    """Stage 3 的运行计数与耗时；所有键都是固定类别。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.decisions: dict[str, int] = {kind: 0 for kind in DECISION_KINDS}
        self.triggers: dict[str, int] = {kind: 0 for kind in TRIGGER_KINDS}
        self.model_errors: dict[str, int] = {}
        self.send_failures = 0
        self.extension_failures = 0
        self.memory_degraded = 0
        self.model_latency = LatencyWindow()
        self.send_latency = LatencyWindow()
        # 判定 agent 单独计时：双 agent 结构下"慢在哪一段"要能分开看。
        self.judge_latency = LatencyWindow()
        # 对话压缩单独计时：它偶尔发生，但一次可能占掉几十秒。
        self.compaction_latency = LatencyWindow()

    def record_trigger(self, kind: str) -> None:
        key = kind if kind in self.triggers else "unknown"
        with self._lock:
            self.triggers[key] += 1

    def record_decision(self, kind: str) -> None:
        with self._lock:
            key = kind if kind in self.decisions else "no_reply"
            self.decisions[key] += 1

    def record_model_error(self, kind: str) -> None:
        # kind 来自 LLMError.kind 或异常类名，都是固定短标识，不含正文。
        key = str(kind)[:40] or "unknown"
        with self._lock:
            self.model_errors[key] = self.model_errors.get(key, 0) + 1

    def record_send_failure(self) -> None:
        with self._lock:
            self.send_failures += 1

    def record_extension_failure(self) -> None:
        with self._lock:
            self.extension_failures += 1

    def record_memory_degraded(self) -> None:
        with self._lock:
            self.memory_degraded += 1

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "decisions": dict(self.decisions),
                "triggers": dict(self.triggers),
                "model_errors": dict(self.model_errors),
                "send_failures": self.send_failures,
                "extension_failures": self.extension_failures,
                "memory_degraded": self.memory_degraded,
                "model_latency": self.model_latency.snapshot(),
                "send_latency": self.send_latency.snapshot(),
                "judge_latency": self.judge_latency.snapshot(),
                "compaction_latency": self.compaction_latency.snapshot(),
            }
