"""超管系统诊断：进程 / GPU / 网量 / 风扇。

设计约束（沿用原来的三条）：

1. **只执行固定脚本**，不接受聊天内容当命令或参数——参数只做白名单映射；
2. **输出脱敏、限长**：只给进程名、PID、数值，不给窗口标题、命令行、路径；
3. **拿不到就说拿不到**，不编数字。

2026-09-28 扩了这一族（用户要求），数据源的实测能力写在每个方法上：

- 进程默认只列**有界面的程序**（`MainWindowHandle != 0`，也就是能 Alt-Tab 到的那些），
  且**不带窗口标题**——`/super` 可以在群里发，标题会泄露（"工资表.xlsx"这种）；
- `cpu` 采样 5 秒（两次 CPU 时间差 ÷ 5 秒 ÷ 核心数），由引擎先回"checking"再回结果；
- GPU 走性能计数器：3D 占用筛 `engtype_3d`，显存按 `Dedicated → Local → Shared` 依次退；
- 网络**只能做到网卡级**：Windows 没有便宜的每进程网络计数（要 ETW + 管理员 + 常驻会话），
  所以后台每 30 秒采一次累计字节，滚动 10 分钟窗口算上传/下载量；
- 风扇先读 `Win32_Fan`，再读 LibreHardwareMonitor 的 WMI（装了并以管理员运行才有），
  都没有就直说本机没暴露。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import time
from collections import deque
from typing import Callable, Protocol

from .security import sanitize_chat_text

logger = logging.getLogger(__name__)

CPU_SAMPLE_SECONDS = 5.0
NETWORK_SAMPLE_SECONDS = 30.0
NETWORK_WINDOW_SECONDS = 600.0
TOP_PROCESSES = 8
TOP_LAN = 5
MAX_OUTPUT_CHARS = 1000
SCRIPT_TIMEOUT = 20.0

PROCESS_MODES = {"default": "default", "memory": "memory", "cpu": "cpu",
                 "gpu": "gpu", "gpu-memory": "gpu-memory"}
_POWERSHELL = ("powershell", "-NoProfile", "-NonInteractive", "-Command")


class RuntimeDiagnostics(Protocol):
    async def processes(self, mode: str = "default") -> str:
        """本机进程诊断（已脱敏、限长）。`mode` 见 `PROCESS_MODES`。"""

    async def network(self) -> str:
        """网卡级上传/下载量（滚动 10 分钟窗口）。"""

    async def fans(self) -> str:
        """风扇转速；本机没暴露时如实说明。"""


def _run_script(script: str, *, timeout: float = SCRIPT_TIMEOUT) -> str:
    """跑一段**固定**的 PowerShell，返回 stdout。失败返回空串（调用方负责说清楚）。"""

    if os.name != "nt":
        return ""
    try:
        result = subprocess.run(
            [*_POWERSHELL, script],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("diagnostics_script_failed category=%s", type(exc).__name__)
        return ""
    return result.stdout if result.returncode == 0 else ""


def _parse_rows(raw: str) -> list[dict]:
    """把 `ConvertTo-Json` 的输出解析成字典列表。"""

    text = (raw or "").strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except ValueError:
        return []
    if isinstance(data, dict):
        return [data]
    return [row for row in data if isinstance(row, dict)]


class LocalRuntimeDiagnostics:
    """只跑固定脚本；进程/GPU/网卡/风扇各一段。"""

    def __init__(self, *, runner: Callable[..., str] | None = None,
                 clock: Callable[[], float] = time.time):
        self._runner = runner or _run_script
        self.clock = clock
        self._network_samples: deque[tuple[float, dict[str, tuple[float, float]]]] = deque()
        self._sampler: asyncio.Task | None = None

    # --- 内部：取数 ------------------------------------------------------------

    def _rows(self, script: str, *, timeout: float = SCRIPT_TIMEOUT) -> list[dict]:
        """跑脚本并解析 JSON。**所有取数都走这里**，注入的 runner 才真正生效
        （离线测试靠它造假的 PowerShell 输出，否则会真去读这台机器）。"""

        return _parse_rows(self._runner(script, timeout=timeout))

    def _process_names(self) -> dict[str, str]:
        rows = self._rows("Get-Process | Select-Object ProcessName,Id | ConvertTo-Json -Compress")
        return {str(row.get("Id")): str(row.get("ProcessName") or "?") for row in rows}

    # --- 前台进程 / 内存 / CPU ------------------------------------------------

    async def processes(self, mode: str = "default") -> str:
        mode = PROCESS_MODES.get((mode or "default").strip().lower(), "default")
        if mode == "cpu":
            return await asyncio.to_thread(self._processes_cpu)
        if mode in {"gpu", "gpu-memory"}:
            return await asyncio.to_thread(self._processes_gpu, mode)
        return await asyncio.to_thread(self._processes_memory, mode)

    def _processes_memory(self, mode: str) -> str:
        if mode == "default":
            script = ("Get-Process | Where-Object { $_.MainWindowHandle -ne 0 } | "
                      "Sort-Object WS -Descending | Select-Object -First 8 "
                      "ProcessName,Id,@{n='MB';e={[math]::Round($_.WS/1MB)}} | ConvertTo-Json -Compress")
            title = "前台程序（有界面的）"
        else:
            script = ("Get-Process | Sort-Object WS -Descending | Select-Object -First 8 "
                      "ProcessName,Id,@{n='MB';e={[math]::Round($_.WS/1MB)}} | ConvertTo-Json -Compress")
            title = "内存占用前 8"
        rows = self._rows(script)
        if not rows:
            return "超管诊断：没能读到进程列表。"
        lines = [f"超管诊断：{title}"]
        lines += [f"{row.get('ProcessName', '?')} | PID {row.get('Id', '?')} | 内存 {row.get('MB', 0)}MB"
                  for row in rows]
        return sanitize_chat_text("\n".join(lines), max_length=MAX_OUTPUT_CHARS)

    def _processes_cpu(self) -> str:
        """两次采样求平均：`CPU` 是进程累计处理器秒数，差值 ÷ 窗口 ÷ 核心数。"""

        cores = os.cpu_count() or 1
        script = (
            "$a=@{}; Get-Process | ForEach-Object { $a[$_.Id]=$_.CPU };"
            f" Start-Sleep -Seconds {int(CPU_SAMPLE_SECONDS)};"
            "$out=@(); Get-Process | ForEach-Object {"
            " $before=$a[$_.Id]; if ($before -ne $null -and $_.CPU -ne $null -and $_.CPU -gt $before) {"
            " $out += [pscustomobject]@{ ProcessName=$_.ProcessName; Id=$_.Id;"
            " CPU=$_.CPU - $before } } };"
            "$out | Sort-Object CPU -Descending | Select-Object -First 8 | ConvertTo-Json -Compress"
        )
        rows = self._rows(script, timeout=CPU_SAMPLE_SECONDS + SCRIPT_TIMEOUT)
        if not rows:
            return "超管诊断：5 秒采样期间没有读到可比较的进程。"
        lines = [f"超管诊断：CPU 占用前 8（{int(CPU_SAMPLE_SECONDS)} 秒平均，"
                 f"{cores} 核折合百分比）"]
        for row in rows:
            seconds = float(row.get("CPU") or 0.0)
            percent = seconds / CPU_SAMPLE_SECONDS / cores * 100
            lines.append(f"{row.get('ProcessName', '?')} | PID {row.get('Id', '?')} | "
                         f"{percent:.1f}%（{seconds:.2f}s）")
        return sanitize_chat_text("\n".join(lines), max_length=MAX_OUTPUT_CHARS)

    # --- GPU -----------------------------------------------------------------

    def _processes_gpu(self, mode: str) -> str:
        if mode == "gpu":
            script = ("Get-Counter '\\GPU Engine(*)\\Utilization Percentage' -MaxSamples 1 "
                      "-ErrorAction SilentlyContinue | Select-Object -ExpandProperty CounterSamples | "
                      "Where-Object { $_.InstanceName -like '*engtype_3d*' } | "
                      "Select-Object InstanceName,CookedValue | ConvertTo-Json -Compress")
            title = "GPU 3D 占用前 8"
            unit, scale = "%", 1.0
        else:
            # 集显常常 Dedicated=0（走共享内存），所以依次退到 Local/Shared。
            script = (
                "$sets=@('Dedicated Usage','Local Usage','Shared Usage');"
                "foreach ($s in $sets) {"
                " $c = Get-Counter \"\\GPU Process Memory(*)\\$s\" -MaxSamples 1 -ErrorAction SilentlyContinue;"
                " if ($c) { $v = $c.CounterSamples | Where-Object { $_.CookedValue -gt 0 };"
                " if ($v) { $v | Select-Object InstanceName,CookedValue | ConvertTo-Json -Compress; break } } }"
            )
            title = "显存占用前 8"
            unit, scale = "MB", 1.0 / 1024 / 1024
        rows = self._rows(script)
        if not rows:
            return (f"超管诊断：{title} —— 现在没有进程在占（或该计数器在本机不可用）。")
        totals: dict[str, float] = {}
        for row in rows:
            name = str(row.get("InstanceName") or "")
            pid = _pid_of(name)
            value = float(row.get("CookedValue") or 0.0) * scale
            if not pid or value <= 0:
                continue  # 0 就是在用——不能把"没用显存"的进程列进榜
            totals[pid] = totals.get(pid, 0.0) + value
        if not totals:
            return f"超管诊断：{title} —— 现在没有进程在占。"
        names = self._process_names()
        lines = [f"超管诊断：{title}"]
        for pid, value in sorted(totals.items(), key=lambda item: -item[1])[:TOP_PROCESSES]:
            lines.append(f"{names.get(pid, '?')} | PID {pid} | {value:.1f}{unit}")
        return sanitize_chat_text("\n".join(lines), max_length=MAX_OUTPUT_CHARS)

    # --- 网卡级流量（滚动 10 分钟） -------------------------------------------

    def sample_network(self) -> None:
        """采一次网卡累计字节。采样器每 30 秒调一次，命令读这份滚动窗口。"""

        rows = self._rows(
            "Get-NetAdapterStatistics | Select-Object Name,ReceivedBytes,SentBytes | "
            "ConvertTo-Json -Compress")
        snapshot = {str(row.get("Name")): (float(row.get("ReceivedBytes") or 0),
                                           float(row.get("SentBytes") or 0))
                    for row in rows if row.get("Name")}
        if not snapshot:
            return
        now = self.clock()
        self._network_samples.append((now, snapshot))
        cutoff = now - NETWORK_WINDOW_SECONDS
        while self._network_samples and self._network_samples[0][0] < cutoff:
            self._network_samples.popleft()

    def ensure_sampler(self) -> None:
        """启动后台采样。没有它，`/super lan` 只能看到"刚启动"这一瞬间。"""

        if self._sampler is not None and not self._sampler.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # 不在事件循环里（离线测试）：调用方自己调 sample_network
        self._sampler = loop.create_task(self._sample_loop(), name="diagnostics-net-sampler")

    async def _sample_loop(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self.sample_network)
            except Exception as exc:  # noqa: BLE001 - 采样失败不该拖垮运行
                logger.warning("network_sample_failed category=%s", type(exc).__name__)
            await asyncio.sleep(NETWORK_SAMPLE_SECONDS)

    async def network(self) -> str:
        return await asyncio.to_thread(self._network)

    def _network(self) -> str:
        if not self._network_samples:
            return "超管诊断：还没有网络采样（服务刚启动，约 30 秒后可用）。"
        first_at, first = self._network_samples[0]
        last_at, last = self._network_samples[-1]
        window = max(1.0, last_at - first_at)
        received: list[tuple[str, float]] = []
        sent: list[tuple[str, float]] = []
        for name, (now_rx, now_tx) in last.items():
            if name not in first:
                continue
            old_rx, old_tx = first[name]
            received.append((name, max(0.0, now_rx - old_rx)))
            sent.append((name, max(0.0, now_tx - old_tx)))
        if not received:
            return "超管诊断：窗口内没有可比较的网卡数据。"
        span = (f"{window / 60:.0f} 分钟" if window >= 60 else f"{window:.0f} 秒")
        lines = [f"超管诊断：网卡流量（最近 {span}，网卡级）"]
        lines.append("下载前 5：")
        lines += [f"  {name} | {_bytes(value)}" for name, value in
                  sorted(received, key=lambda item: -item[1])[:TOP_LAN]]
        lines.append("上传前 5：")
        lines += [f"  {name} | {_bytes(value)}" for name, value in
                  sorted(sent, key=lambda item: -item[1])[:TOP_LAN]]
        lines.append(f"合计：下载 {_bytes(sum(v for _, v in received))} / "
                     f"上传 {_bytes(sum(v for _, v in sent))}")
        lines.append("（进程级流量 Windows 没有便宜的接口，需要 ETW，这里不做。）")
        return sanitize_chat_text("\n".join(lines), max_length=MAX_OUTPUT_CHARS)

    # --- 风扇 -----------------------------------------------------------------

    async def fans(self) -> str:
        return await asyncio.to_thread(self._fans)

    def _fans(self) -> str:
        rows = self._rows(
            "Get-CimInstance Win32_Fan -ErrorAction SilentlyContinue | "
            "Select-Object Name,DesiredSpeed,ActiveCooling | ConvertTo-Json -Compress")
        speeds = [(str(row.get("Name") or "风扇"), float(row.get("DesiredSpeed") or 0))
                  for row in rows]
        # Win32_Fan 在消费级机器上常常只回一个没有转速的占位项。
        if not any(speed > 0 for _name, speed in speeds):
            lhm = self._rows(
                "Get-CimInstance -Namespace root\\LibreHardwareMonitor -ClassName Sensor "
                "-ErrorAction SilentlyContinue | Where-Object { $_.SensorType -eq 'Fan' } | "
                "Select-Object Name,Value | ConvertTo-Json -Compress")
            speeds = [(str(row.get("Name") or "风扇"), float(row.get("Value") or 0)) for row in lhm]
        if not speeds:
            return ("超管诊断：本机没有暴露风扇转速。"
                    "要读的话需要装 LibreHardwareMonitor、以管理员运行并开启 WMI。")
        lines = ["超管诊断：风扇转速"]
        lines += [f"{name} | {speed:.0f} RPM" for name, speed in speeds]
        return sanitize_chat_text("\n".join(lines), max_length=MAX_OUTPUT_CHARS)


def _pid_of(instance_name: str) -> str:
    """从 `pid_1234_luid_..._engtype_3d` 里取出 PID。"""

    if not instance_name.startswith("pid_"):
        return ""
    pid = instance_name[4:].split("_", 1)[0]
    return pid if pid.isdigit() else ""


def _bytes(value: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f}{unit}" if unit != "B" else f"{int(value)}B"
        value /= 1024
    return f"{value:.1f}TB"
