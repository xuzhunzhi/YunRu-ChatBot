"""Run the existing plain test functions and unittest suites without pytest.

覆盖当前 Stage 3 主路径（tests/）以及已归档的历史回退实现（archive/）。
两者都不访问真实模型、不连接 QQ、不发送任何外部消息。

**每次运行都会留痕**（`qq_roleplay_bot.verify_log`，底层能力）：
一行摘要（时间 + 提交 + 工作区脏不脏 + 计数 + 结论）追加到 `data/logs/verify_runs.log`，
整份输出覆盖写到 `data/logs/verify_last.txt`。
这样"某次全绿"能被**独立核对**，而不是只能选择相信写文档的人
（2026-10-02 的一次外部观察正是因为找不到凭证而无法核对）。
"""
import importlib.util
import io
import os
import sys
import unittest
from pathlib import Path

root = Path(__file__).resolve().parents[1]

# 测试必须与真实运行数据隔离：关闭运行状态持久化并把状态文件指向临时目录，
# 否则一组离线测试会覆盖 data/runtime_state.json 里的真实群名单和会话。
os.environ["QQBOT_STATE_PERSIST"] = "0"
os.environ["QQBOT_STATE_FILE"] = str(root / ".tmp_test_run" / "runtime_state.json")
# 同理关闭模型 I/O 追踪：`.env` 里为排查注入问题开着它，测试会跟着把上千条合成
# prompt（含 system prompt 全文）写进 data/traces/model_trace.jsonl，把真实追踪淹掉。
# 配置文件的键不会覆盖已存在的环境变量，所以这里显式置 0 是有效的。
os.environ["QQBOT_DEBUG_MODEL_IO"] = "0"
# 同理关掉按功能分的输入输出日志（判定/回复/记忆/规则），并把目录指向临时目录：
# 它**默认是开的**，跑一次全量测试会往 data/logs/ 灌几十 MB 合成 prompt
# （实测一次 84 MB）。要测日志本身的用例会显式传 enabled=True。
os.environ["QQBOT_FEATURE_LOG"] = "0"
os.environ["QQBOT_FEATURE_LOG_DIR"] = str(root / ".tmp_test_run" / "logs")
# 同理关掉**对话日志**（实际收发，`chat.jsonl`）：它默认也是开的，而一次全量测试会
# 真的走"发送三段"这类路径，把成千上万条合成消息写进日志。要测日志本身的用例显式传
# `enabled=True` + 自己的临时目录（见 tests/test_chat_log.py），不靠这个开关。
os.environ["QQBOT_CHAT_LOG"] = "0"
os.environ["QQBOT_CHAT_LOG_DIR"] = str(root / ".tmp_test_run" / "logs")

sys.path.insert(0, str(root / "src"))
sys.path.insert(0, str(root / "archive"))
# `tests/` 也进 path：测试之间共用的辅助按普通模块名互相 import——
# 下面用 spec_from_file_location 加载测试文件本身，所以它们不在包命名空间里。
sys.path.insert(0, str(root / "tests"))
suite = unittest.TestSuite()
for directory in (root / "tests", root / "archive"):
    for path in sorted(directory.glob("test_*.py")):
        spec = importlib.util.spec_from_file_location(path.stem, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[path.stem] = module
        spec.loader.exec_module(module)
        suite.addTests(unittest.defaultTestLoader.loadTestsFromModule(module))
        for name in sorted(vars(module)):
            value = getattr(module, name)
            if name.startswith("test_") and callable(value):
                suite.addTest(unittest.FunctionTestCase(value, description=f"{path.name}:{name}"))

# 一边跑一边把输出抓下来，留给 verify_log 存档（失败时不必让人重跑一遍——
# 重跑会碰真实 data/，前几任审查者都因此不敢跑）。
#
# **stdout 与 stderr 都要抓**：`unittest.TextTestRunner` 默认把进度与 traceback 写到
# **stderr**（`self.stream = stream or sys.stderr`，在构造时取一次）。
# 只替换 stdout 会抓不到任何东西——我第一版就是这么写的，结果存档里只有 6 个字节。
stream = io.StringIO()
real_stdout, real_stderr = sys.stdout, sys.stderr


class _Tee:
    """同时写终端与缓冲：终端的实时输出不变，缓冲用来存档。"""

    def __init__(self, mirror) -> None:
        self._mirror = mirror

    def write(self, text: str) -> int:
        stream.write(text)
        return self._mirror.write(text)

    def flush(self) -> None:
        self._mirror.flush()

    def __getattr__(self, name):  # isatty / encoding / fileno 之类照旧转发给真终端
        return getattr(self._mirror, name)


sys.stdout = _Tee(real_stdout)  # type: ignore[assignment]
sys.stderr = _Tee(real_stderr)  # type: ignore[assignment]
try:
    # 在替换之后**才构造** runner：它在构造时就把 `sys.stderr` 抓进 `self.stream`。
    result = unittest.TextTestRunner(verbosity=1).run(suite)
finally:
    sys.stdout, sys.stderr = real_stdout, real_stderr

if result.wasSuccessful():
    print("ALL_OFFLINE_TESTS_PASSED")

# **无条件留痕**——包括失败的那次。留痕自己出错只会打一行 warning，不改退出码
# （见 verify_log 的模块说明：日志写不进去不能伪装成"测试失败"，反之亦然）。
from qq_roleplay_bot.verify_log import record_run  # noqa: E402 - 必须在 src 进 path 之后

summary = record_run(
    root, label="offline",
    ran=result.testsRun, failures=len(result.failures), errors=len(result.errors),
    skipped=len(result.skipped), ok=result.wasSuccessful(), output=stream.getvalue(),
)
if summary is not None:
    print(f"（留痕已写入 {summary}）")

sys.exit(0 if result.wasSuccessful() else 1)
