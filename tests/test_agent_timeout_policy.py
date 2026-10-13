"""口径守门（2026-10-13 用户拍板）：超时只管首字、不要重试——机械拦住"总上限被加回来"。

用户原话：「超时只管首字，首字超时才跳过，不要重试，重试还不如干等着，所有 agent 都一样」。

这条测试**不看运行行为**（行为在 `tests/test_llm_client.py` 与 `tests/test_memory.py` 里钉着），
它扫源码结构：`src/` 里**不许**出现把模型调用（`*.complete(...)`）包在
`asyncio.wait_for(...)` 里的写法——那正是"整趟总上限"的形状。

为什么需要它（2026-10-13 专家评审指出的缺口）：把某一腿的 `wait_for` 加回去，
**当时没有任何测试会红**；`plugins/vision/vision.py` 里那道 20 秒上限就是这么一直在跑的。
唯一挡住的形状（`_ask` 里写 `self.timeout`）是巧合，不是判据。

允许存在的 `wait_for` 都是**非模型**的：`asyncio.to_thread(...)`（数据库 / 磁盘）、
传输层（onebot / websocket future）、事件等待、以及 `runtime` 那一处整轮预算
（`engine.handle`，不是单次模型调用）。所以判据只认"参数里出现 `.complete(`"。
"""
from __future__ import annotations

import ast
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "qq_roleplay_bot"

#: 模型调用的方法名（`OpenAICompatibleClient.complete`）。
MODEL_CALL_ATTRS = frozenset({"complete"})


def _wrapped_model_calls(path: pathlib.Path) -> list[tuple[int, str]]:
    """这一份源码里"被 wait_for 包住的模型调用"：返回 (行号, 被包住的方法名)。"""

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "wait_for"):
            continue
        arguments = [*node.args, *(keyword.value for keyword in node.keywords)]
        for argument in arguments:
            for inner in ast.walk(argument):
                if not isinstance(inner, ast.Call):
                    continue
                target = inner.func
                name = (target.attr if isinstance(target, ast.Attribute)
                        else target.id if isinstance(target, ast.Name) else "")
                if name in MODEL_CALL_ATTRS:
                    hits.append((node.lineno, name))
    return hits


class AgentTimeoutPolicyTests(unittest.TestCase):
    def test_no_model_call_is_wrapped_in_a_total_timeout(self) -> None:
        offenders: dict[str, list[tuple[int, str]]] = {}
        for path in sorted(SRC.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            hits = _wrapped_model_calls(path)
            if hits:
                offenders[str(path.relative_to(ROOT))] = hits
        self.assertEqual(
            offenders,
            {},
            "模型调用不许再套整趟总上限：首字那 3 秒由 `llm_client.STREAM_STALL_SECONDS` 管，"
            "首字来了就让它说完（用户 2026-10-13：超时只管首字）",
        )

    def test_the_default_is_a_single_attempt(self) -> None:
        """不要重试：默认只跑一轮（机制还在——显式传 `max_attempts` 才会重试）。"""

        from qq_roleplay_bot.llm_client import DEFAULT_MAX_ATTEMPTS

        self.assertEqual(DEFAULT_MAX_ATTEMPTS, 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
