"""超管系统诊断的格式与降级：进程 / GPU / 网量 / 风扇。

所有用例都注入假 runner（PowerShell 输出用 JSON 造），所以离线可跑、不依赖显卡或风扇。
"""
import json

from qq_roleplay_bot.runtime_diagnostics import LocalRuntimeDiagnostics


def make(script_outputs: dict[str, str]) -> LocalRuntimeDiagnostics:
    """按脚本关键字返回假输出的 runner。"""

    def runner(script: str, *, timeout: float = 20.0) -> str:
        for marker, output in script_outputs.items():
            if marker in script:
                return output
        return ""

    return LocalRuntimeDiagnostics(runner=runner)


def test_default_mode_lists_foreground_apps_without_window_titles() -> None:
    """默认档只列有界面的程序；**不出现窗口标题**（/super 可以在群里发）。"""

    diagnostic = make({"MainWindowHandle": json.dumps(
        [{"ProcessName": "msedge", "Id": 10, "MB": 312}])})
    text = __import__("asyncio").run(diagnostic.processes("default"))
    assert "前台程序" in text and "msedge" in text and "PID 10" in text
    assert "标题" not in text


def test_memory_mode_sorts_by_working_set() -> None:
    diagnostic = make({"Sort-Object WS": json.dumps(
        [{"ProcessName": "big", "Id": 1, "MB": 900}, {"ProcessName": "small", "Id": 2, "MB": 12}])})
    text = __import__("asyncio").run(diagnostic.processes("memory"))
    assert "内存占用前 8" in text
    assert text.index("big") < text.index("small")


def test_cpu_mode_converts_seconds_to_percent() -> None:
    """CPU 是累计处理器秒数 → 差值 ÷ 采样窗口 ÷ 核心数。"""

    import asyncio
    import os

    cores = os.cpu_count() or 1
    diagnostic = make({"Start-Sleep": json.dumps(
        [{"ProcessName": "busy", "Id": 7, "CPU": 5.0}])})
    text = asyncio.run(diagnostic.processes("cpu"))
    expected = 5.0 / 5.0 / cores * 100
    assert f"{expected:.1f}%" in text
    assert "5 秒平均" in text


def test_gpu_mode_sums_engines_per_process() -> None:
    """同一个进程可能有好几个 3D 引擎实例，要按 PID 合并。"""

    import asyncio

    diagnostic = make({
        "GPU Engine": json.dumps([
            {"InstanceName": "pid_100_luid_0x0_phys_0_eng_0_engtype_3d", "CookedValue": 12.5},
            {"InstanceName": "pid_100_luid_0x0_phys_0_eng_5_engtype_3d", "CookedValue": 7.5},
            {"InstanceName": "pid_200_luid_0x0_phys_0_eng_0_engtype_3d", "CookedValue": 5.0},
        ]),
        "Get-Process |": json.dumps([{"ProcessName": "game", "Id": 100},
                                     {"ProcessName": "browser", "Id": 200}]),
    })
    text = asyncio.run(diagnostic.processes("gpu"))
    assert "game | PID 100 | 20.0%" in text
    assert text.index("game") < text.index("browser")


def test_gpu_memory_reports_when_nothing_is_using_it() -> None:
    """计数器在、但全是 0 时要说清楚，不能编数字。"""

    import asyncio

    diagnostic = make({"GPU Process Memory": json.dumps(
        [{"InstanceName": "pid_100_luid_0x0", "CookedValue": 0}])})
    assert "没有进程在占" in asyncio.run(diagnostic.processes("gpu-memory"))


def test_network_window_uses_first_and_last_sample() -> None:
    """网量 = 窗口内累计字节的差；上传下载分别排序，各取前 5。"""

    import asyncio

    samples = iter([
        json.dumps([{"Name": "Wi-Fi", "ReceivedBytes": 1000, "SentBytes": 500}]),
        json.dumps([{"Name": "Wi-Fi", "ReceivedBytes": 1100, "SentBytes": 800}]),
    ])
    diagnostic = LocalRuntimeDiagnostics(runner=lambda script, **kw: next(samples, ""))
    clock = iter([0.0, 300.0])
    diagnostic.clock = lambda: next(clock, 300.0)
    diagnostic.sample_network()
    diagnostic.sample_network()
    text = asyncio.run(diagnostic.network())
    assert "网卡流量" in text and "5 分钟" in text
    assert "下载前 5" in text and "上传前 5" in text
    assert "100B" in text and "300B" in text
    assert "进程级流量" in text  # 说清楚为什么只有网卡级


def test_network_without_samples_says_so() -> None:
    import asyncio

    diagnostic = make({})
    assert "还没有网络采样" in asyncio.run(diagnostic.network())


def test_fans_falls_back_to_libre_hardware_monitor() -> None:
    """Win32_Fan 在消费级机器上只回占位项 → 退到 LibreHardwareMonitor 的 WMI。"""

    import asyncio

    diagnostic = make({
        "Win32_Fan": json.dumps([{"Name": "冷却设备", "DesiredSpeed": 0, "ActiveCooling": True}]),
        "LibreHardwareMonitor": json.dumps([{"Name": "CPU Fan", "Value": 1820.0}]),
    })
    text = asyncio.run(diagnostic.fans())
    assert "CPU Fan | 1820 RPM" in text


def test_fans_without_any_source_says_so() -> None:
    """两个数据源都没有时如实说明，并给出以后能读到的条件。"""

    import asyncio

    diagnostic = make({"Win32_Fan": json.dumps([{"Name": "冷却设备", "DesiredSpeed": 0}])})
    text = asyncio.run(diagnostic.fans())
    assert "没有暴露风扇转速" in text
    assert "LibreHardwareMonitor" in text
