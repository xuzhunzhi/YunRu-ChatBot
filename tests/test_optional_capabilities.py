"""**可插能力缺席时，核心必须照跑**——这条是判据的正面用法，所以要有测试守。

判据（用户）："删掉它，Stage 3 会不会出问题。"

## 由来（2026-10-02 外部审查第四轮，real bug）

`runtime.py` 与 `prompt_library.py` 里那几处"局部 import 可选能力"**没有兜住
`ImportError`**，所以注释里"没有它核心照样跑"是**假的**：

    vision          能 import    build_engine 炸（经 prompt_library 那套 prompt）
    qq_roles        能 import    build_engine 炸（runtime 里的裸 import）
    control_audit   能 import    build_engine 炸（同上）

`import qq_roleplay_bot.runtime` 能过，是因为失败在**函数体里**；
真正的入口 `build_engine()` → `serve` 第一步就炸。判据问的是"能不能跑起来"，
**不是"能不能 import"**——所以我原来那个只测 import 的探针给出了误导性的"已解耦"。

## 这个文件怎么测

把目标模块在 `sys.modules` 里置成 `None`：CPython 随后对它的 `import` 会抛
`ImportError`（"import of X halted; None in sys.modules"）。这比真的改文件名干净
——不碰磁盘、跑完自动还原、也不会让别的测试收集失败。
"""
import sys

from qq_roleplay_bot import runtime


class _FakeTransport:
    """够 `build_engine` 装配的最小传输层。"""

    def __init__(self) -> None:
        self.sent: list[object] = []

    async def call_api(self, action, params=None):
        if action == "get_login_info":
            return {"status": "ok", "retcode": 0, "data": {"user_id": "9", "nickname": "n"}}
        return {"status": "ok", "retcode": 0, "data": {"role": "owner"}}

    async def send(self, target, text, **kwargs):  # pragma: no cover - 装配不需要
        self.sent.append((target, text))

    async def start(self):  # pragma: no cover
        return None


class _Hidden:
    """临时让某个模块"看起来不存在"。

    `sys.modules[name] = None` 之后，`from ... import ...` 会抛 `ImportError`——
    这正好走我们要守的那条 `except` 分支。
    """

    def __init__(self, *names: str) -> None:
        self._names = names
        self._saved: dict[str, object] = {}

    def __enter__(self):
        for name in self._names:
            self._saved[name] = sys.modules.get(name, "absent")
            sys.modules[name] = None
        return self

    def __exit__(self, *exc):
        for name, value in self._saved.items():
            if value == "absent":
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value
        return False


def test_build_engine_survives_a_missing_audit_module() -> None:
    """没有 `control_audit` 时 `build_engine` 必须**照样装出一台机器**。

    降级不能只是"不炸"：审计要变成 `_host.NoAudit`——**同一个协议的空实现**
    （`record` / `tail` 都在），而不是 `None` 再到别处 `AttributeError`。
    """

    import qq_roleplay_bot.control_audit as audit_module

    with _Hidden("qq_roleplay_bot.control_audit"):
        engine = runtime.build_engine(_FakeTransport())

    # 它得真的能记 / 读（空实现），不能是 None
    engine.control_audit.record("test", detail={"a": 1})
    assert engine.control_audit.tail(5) == []
    # 还原之后真实现还得能用（证明我们没把模块搞坏）
    assert audit_module.ControlAudit is not None


def test_build_engine_survives_a_missing_role_module() -> None:
    """**角色查询缺席**时是降级、不是崩；而且它的唯一来源是插件。

    ## 这条在 2026-10-04 改了测的东西（改测的东西，不是因为红了才改）

    原来它 `_Hidden("qq_roleplay_bot.qq_roles")` 再断言 `engine.self_roles is None`。
    `qq_roles.SelfRoleCache` 是**核心自己那第二份**角色缓存——"单源"那一改之后，
    核心不再 import 它（`runtime.build_engine` 里那个创建已删），于是藏起它
    什么也证明不了（能力还在，是插件给的）。**这属于测试的前提变了，不是放宽断言。**

    （函数名留着不改了：它是 `qq_roles` 那个模块第一次被抓到的现场，改名字会让
    "这条守的是什么"的历史断掉。测的东西以本 docstring 为准。）

    新的前提：角色查询归 `plugins/roles/`，由核心注入的 `call_action` 现造。
    所以"这项能力不在"= **`call_action` 不在**。这条负向路径分两半验：

    1. `roles.plugin.register` 在没有 `call_action` 时**明确抛错**，
       不许"装上一个查不了角色的缓存"（那正是 2026-09-30 那次静默回归的形状）；
    2. 核心这一侧：`build_engine` 装出来的机器**不再自己造**那份缓存——
       `self_roles` 就是插件放进注册表的那一个对象（单源）。
    """

    from qq_roleplay_bot.plugins import PluginRegistry
    from qq_roleplay_bot.plugins.roles import plugin as roles_plugin

    registry = PluginRegistry()  # `call_action` 缺省就是 None
    try:
        roles_plugin.register(registry)
    except RuntimeError:
        # 明确拒绝是对的——**不许静默装一个查不了角色的缓存**。
        pass
    else:  # pragma: no cover - 不该走到
        raise AssertionError("没有 call_action 时 roles 插件不该装上去")
    assert registry.shared_roles() is None, "装失败了就不该留下半个共享角色"

    # 核心照旧起得来（`build_engine` 跑通本身就是这条判据），
    # 而且 `self_roles` 与注册表上那份**是同一个对象**：单源。
    engine = runtime.build_engine(_FakeTransport())
    assert engine.plugin_registry.shared_roles() is engine.self_roles, (
        "角色的唯一来源必须是 roles 插件放进注册表的那一份（单源）")


def test_build_engine_survives_a_missing_vision_module() -> None:
    """没有 `vision` 时 `build_engine` 也必须起得来。

    失败点不在 `runtime.py`，而在 `prompt_library.builtin("vision")`——
    `backfill_all()` 会在启动期遍历到它。所以降级要落在 prompt 那一侧：
    `available()` 得把识图滤掉，而不是让 `ModuleNotFoundError` 冒出去。
    """

    from qq_roleplay_bot.prompt_library import PromptLibrary

    with _Hidden("qq_roleplay_bot.vision"):
        # builtin 必须给一个**受控**的拒绝，而不是放 ModuleNotFoundError 出去
        from qq_roleplay_bot.prompt_library import PromptRejected

        try:
            PromptLibrary.builtin("vision")
        except PromptRejected:
            pass
        else:
            raise AssertionError("vision 不在时 builtin('vision') 应该抛 PromptRejected")

        assert "vision" not in PromptLibrary.available(), PromptLibrary.available()
        # 真正的判据：整台机器装得起来
        engine = runtime.build_engine(_FakeTransport())
        assert engine is not None


def test_every_available_prompt_is_readable() -> None:
    """`available()` 里的每一套都必须能 `text()` 出来。

    面板的 prompt 列表照 `available()` 遍历，所以这条不成立时列表会整个 500。
    （这次修复前它遍历的是 `PROMPTS`，没有 `vision` 的部署上就会炸。）
    """

    from qq_roleplay_bot.prompt_library import PromptLibrary

    with _Hidden("qq_roleplay_bot.vision"):
        library = PromptLibrary()
        names = library.available()
        assert "persona" in names and "vision" not in names, names
        for name in names:
            assert library.text(name).strip(), name


def test_backfill_all_skips_a_missing_capability_without_faking_a_prompt() -> None:
    """缺席的那一套要**跳过**，不许伪造占位 prompt。

    伪造一份会让面板看起来"有识图这套 prompt"，而它其实不在这次部署里——
    那正是这个仓库反复栽过的"文档/界面撒谎"。
    """

    import tempfile
    from pathlib import Path

    from qq_roleplay_bot.prompt_library import PromptLibrary

    with tempfile.TemporaryDirectory() as tmp:
        with _Hidden("qq_roleplay_bot.vision"):
            library = PromptLibrary(Path(tmp))
            added = library.backfill_all()
        # 五套真的存在，识图那套被跳过
        assert added == 5, added
        versions = sorted(p.name.split(".")[0] for p in (Path(tmp) / "versions").glob("*.json"))
        assert "vision" not in versions, versions
        assert "persona" in versions, versions
