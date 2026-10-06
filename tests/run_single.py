"""**在一个干净的进程里**跑一份测试文件（不加载整套 `run_offline`）。

## 为什么需要它

`tests/run_offline.py` 把全部测试装进**同一个进程**，而判据里有一条是"材料必须在
user 段的 DATA 区、不在 system 段"——那一条要在 import `stage3_runtime` **之前**
改源码才有意义（模块级常量在 import 时就渲染好了，同一个进程里改文件已经晚了）。

所以突变验证（把材料挪进 system / 写成命令式）要用**新进程 + 单文件**跑::

    .\\.venv\\Scripts\\python.exe tests\\run_single.py tests\\test_quote_injection_shape.py

普通情况下没人需要它；它是给"验一条判据真的能红"用的工具，跟
`tests/check_module_removal.py` 同一类（门槛必须在仓库里，不能只写在文档里）。
"""
from __future__ import annotations

import importlib.util
import os
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

os.environ.setdefault("QQBOT_STATE_PERSIST", "0")
os.environ.setdefault("QQBOT_DEBUG_MODEL_IO", "0")
os.environ.setdefault("QQBOT_FEATURE_LOG", "0")
os.environ.setdefault("QQBOT_CHAT_LOG", "0")
os.environ.setdefault("QQBOT_RAW_EVENTS", "0")
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(HERE))


def load(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[path.stem] = module
    spec.loader.exec_module(module)
    return module


def main(argv: list[str]) -> int:
    targets = [Path(arg) for arg in argv[1:]] or [HERE / "test_quote_injection_shape.py"]
    suite = unittest.TestSuite()
    for target in targets:
        path = target if target.is_absolute() else (ROOT / target)
        module = load(path)
        suite.addTests(unittest.defaultTestLoader.loadTestsFromModule(module))
        for name in sorted(vars(module)):
            value = getattr(module, name)
            if name.startswith("test_") and callable(value):
                suite.addTest(unittest.FunctionTestCase(value, description=f"{path.name}:{name}"))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    print(f"ran={result.testsRun} failures={len(result.failures)} errors={len(result.errors)}")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
