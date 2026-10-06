"""把 `tests/config_support.py` 里那三道守卫**自己**钉住。

## 为什么守卫也需要测试（2026-10-02 外部审查第五轮）

`skip_unless_memory_enabled()` 原来这样写：

    from qq_roleplay_bot import dev_config
    if not getattr(dev_config, "MEMORY_ENABLED", False):   # ← 这个属性不存在
        raise unittest.SkipTest(...)

**`dev_config` 里从来没有 `MEMORY_ENABLED`**，于是 `getattr(..., False)` 恒 `False`
→ 守卫**恒跳过**。后果不是"保守"，而是：

- `test_memory` 里**唯一**验"记忆服务有没有被接上组装路径"的那条测试，
  **从来没在这台机器上跑过**；
- 外部审查实测：把 `_start_memory` 改成永远 `return None`（真回归），
  那组测试**仍然 SKIP**；摘掉守卫、同一个突变 → **RED**。

也就是说：**一道写错前提的守卫 = 一条被永久删掉的测试，而且仪表盘上看不出来**
（`skipped=1` 长得像"本机没配"）。

这是同一个失败模式的第三次：**属性名是推断的，不是查过的**。
所以这个文件的作用是——**再写错一次就立刻红**，而不是安静地少跑一条测试。

## 这里怎么测

不真去改源码，而是**对着"真实来源"核对守卫问到的东西**：

1. 守卫引用的每个 `dev_config` 属性**必须真的存在**（`hasattr`）；
2. 在**本机确实配好了**的前提下，守卫**必须放行**（不许抛 `SkipTest`）；
3. 反过来，把开关显式关掉时必须**确实跳过**（守卫不能是"永远放行"）。

第 2 条是抓"死守卫"的；第 3 条是抓"守卫忘了生效"的。两条都要。
"""
import unittest


def test_every_guard_reads_an_attribute_that_exists() -> None:
    """守卫读的属性必须真的存在——`getattr(x, "不存在", 默认值)` 是沉默的。

    这条直接钉住本次的教训：`getattr(dev_config, "MEMORY_ENABLED", False)`
    语法上完全合法、跑起来也不报错，只是**永远返回默认值**。
    """

    from qq_roleplay_bot import dev_config

    for name in ("REVIEW_ENABLED", "API_KEY", "VISION_ENABLED", "MEMORY_API_KEY"):
        # 这些是 `config_support` 与其它守卫实际读的
        assert hasattr(dev_config, name), f"dev_config.{name} 不存在，读它的守卫会静默走错分支"

    # 记忆开关**不在** `dev_config` 上——这正是原守卫写错的地方，
    # 写进测试免得有人"顺手加回去"。
    assert not hasattr(dev_config, "MEMORY_ENABLED"), (
        "dev_config 现在有 MEMORY_ENABLED 了？那请顺手改 tests/config_support.py "
        "去读它，并删掉这条断言")


def test_the_memory_guard_lets_a_configured_machine_run() -> None:
    """**本机配好了就必须放行**——这条抓的就是那个死守卫（它原来恒跳过）。"""

    from config_support import skip_unless_memory_enabled
    from qq_roleplay_bot import runtime

    if runtime._memory_endpoint() is None:
        raise unittest.SkipTest("本机确实没有记忆 key，这条不适用")

    try:
        skip_unless_memory_enabled()
    except unittest.SkipTest as exc:
        raise AssertionError(
            f"本机有记忆 key，守卫却跳过了——它读的东西不对：{exc}") from None


def test_the_memory_guard_skips_when_there_is_no_key() -> None:
    """反过来也要成立：**没有 key 时必须真的跳过**。

    守卫的判据必须是"真实前提"（现在就是 `runtime._memory_endpoint()`，也就是
    装配时用的那一个函数），否则会矫枉过正——用"看着像开关"的东西当判据，
    守卫就**永不跳过**，那条测试在干净 clone 上会直接红
    （`services` 空 → `services[0]` → IndexError）。

    这条测试就是抓住我第一版修法的那一条。
    """

    import os

    from config_support import skip_unless_memory_enabled
    from qq_roleplay_bot import dev_config

    names = ("QQBOT_MEMORY_API_KEY", "QQBOT_JUDGE_API_KEY", "QQBOT_API_KEY")
    saved = {name: os.environ.get(name) for name in names}
    saved_key = dev_config.API_KEY
    for name in names:
        os.environ.pop(name, None)
    # `.env` 带入的那份也要清掉——`model_config` 的兼容档会看它，
    # 留着它就等于"这台机器其实配了主 key"，记忆服务照样起得来。
    dev_config.API_KEY = ""
    try:
        try:
            skip_unless_memory_enabled()
        except unittest.SkipTest:
            pass
        else:
            raise AssertionError("没有记忆 key 时守卫没有跳过——判据用错了东西")
    finally:
        dev_config.API_KEY = saved_key
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def test_the_memory_guard_matches_what_the_runtime_actually_checks() -> None:
    """守卫的判据必须与 `runtime` 装配记忆服务时的**真实前提**一致。

    2026-10-02 的教训是双向的：第一版守卫读了一个不存在的属性（恒跳过），
    第二版读了一个与实际前提无关的属性（恒不跳过）。所以这里把两者绑在一起。

    2026-10-06 三层配置落地后，判据不再是一个常量、而是
    `runtime._memory_endpoint()`（`_start_memory` 的第一句就是它）：

    - `_start_memory` 必须调 `_memory_endpoint()`；
    - `_memory_endpoint` 必须走 `model_config.resolve("memory")`（那是"哪把 key"的唯一来源）；
    - 守卫必须调同一个 `_memory_endpoint()`。

    改任何一处，这条测试都会红——那正是提醒你去同步。
    """

    import inspect

    from config_support import skip_unless_memory_enabled

    from qq_roleplay_bot import runtime

    start_source = inspect.getsource(runtime._start_memory)
    endpoint_source = inspect.getsource(runtime._memory_endpoint)
    guard_source = inspect.getsource(skip_unless_memory_enabled)
    assert "_memory_endpoint()" in start_source, (
        "`_start_memory` 不再用 `_memory_endpoint()` 了？"
        "请同步 tests/config_support.skip_unless_memory_enabled 的判据")
    assert 'resolve("memory")' in endpoint_source, (
        "`_memory_endpoint` 不再从 `model_config` 解析 memory 用途了？")
    assert "_memory_endpoint()" in guard_source, (
        "守卫没有问运行时那个判据（它又回去读某个常量了？）")

