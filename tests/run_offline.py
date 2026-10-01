"""Run the existing plain test functions and unittest suites without pytest.

覆盖当前 Stage 3 主路径（tests/）以及已归档的历史回退实现（archive/）。
两者都不访问真实模型、不连接 QQ、不发送任何外部消息。
"""
import importlib.util
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

sys.path.insert(0, str(root / "src"))
sys.path.insert(0, str(root / "archive"))
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
result = unittest.TextTestRunner(verbosity=1).run(suite)
if result.wasSuccessful():
    print("ALL_OFFLINE_TESTS_PASSED")
sys.exit(0 if result.wasSuccessful() else 1)
