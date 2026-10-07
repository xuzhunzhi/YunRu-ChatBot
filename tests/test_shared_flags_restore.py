"""进程共用的运行期开关：**谁改了它，谁负责还回去**（2026-10-08 实测的顺序污染）。

现象（`tests/run_single.py test_memory.py test_affinity_store.py test_affinity_behavior.py`
同一个进程里跑）：3 failed + 1 error。根因不在那三个文件之间的"数据"，而在一份
**进程级单例**：

- `runtime.build_engine()` 里有 `runtime_flags.install(build_flags(operator))`；
- `DialogueEngine._flags()` **优先读共用那份**（面板热更靠它），直接造出来的引擎也跟着走；
- `tests/test_memory.py` 的接线用例把 `dev_config.DUAL_AGENT_ENABLED` patch 成 False
  跑一次真装配 → 共用那份的 `judge_enabled=False` **留在进程里**
  → 后面 `test_affinity_behavior.py` 的引擎整轮不跑判定（判定不跑，防备用不掉）。

整套按文件名字母序跑时 `test_affinity_behavior.py` 在 `test_memory.py` **之前**，
所以这条污染一直没暴露；只有显式指定顺序才会撞上。

这一份钉住两件事：

1. `install(None)` = 卸掉共用那份（引擎落回自己那份替身，不再受装配影响）；
2. 复原之后，直接造出来的引擎读到的又是它自己那份——也就是"还回去"真的生效。

修法是 `tests/test_memory.py` 在 `finally` 里 `install(旧的)`（去掉那一句 → 上面那三个
文件同进程跑重新变红）。
"""
from qq_roleplay_bot import runtime_flags
from qq_roleplay_bot.runtime_flags import RuntimeFlags
from qq_roleplay_bot.stage3_main import DialogueEngine


class _NeverCalled:
    async def complete(self, request):
        raise AssertionError("这些路径不该调用模型")


def test_an_engine_follows_the_shared_flags_while_they_are_installed() -> None:
    """共用那份是**活引用**：装配改它，直接造的引擎下一轮就跟着变。"""

    engine = DialogueEngine(_NeverCalled())
    previous = runtime_flags.shared()
    try:
        runtime_flags.install(RuntimeFlags(judge_enabled=False))
        assert engine._flags().judge_enabled is False, "引擎没读共用那份"
    finally:
        runtime_flags.install(previous)

    assert engine._flags().judge_enabled is True, "复原之后还受装配影响"


def test_install_none_drops_the_shared_flags() -> None:
    """卸掉共用那份 → 引擎落回自己那份替身（`_default_flags`）。"""

    previous = runtime_flags.shared()
    try:
        runtime_flags.install(None)
        assert runtime_flags.shared() is None
        engine = DialogueEngine(_NeverCalled())
        engine._default_flags = RuntimeFlags(review_enabled=False)
        assert engine._flags().review_enabled is False, "落回替身之后读错了"
    finally:
        runtime_flags.install(previous)


if __name__ == "__main__":
    import unittest

    unittest.main()
