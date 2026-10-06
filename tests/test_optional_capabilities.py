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

## 2026-10-06：`qq_roles` 不再是"可插能力"

用户当天拍板 **"行，进核心"**：群成员角色事实从插件搬进核心并改名
`group_roles.py`（`GroupRoles` 回答"某人在某群是什么角色"与"她自己是什么角色"）。
它**不在**这个文件的名单里，因为它的缺席**会**影响 Stage 3（她认不出谁是管理员/群主）。

它的缺席路径另有用例守：`tests/test_group_roles.py` 里的
`test_the_lazy_wrapper_degrades_when_the_module_is_missing`（模块不在时一律
fail-closed、不抛异常），以及 `tests/check_module_removal.py` 那段说明。

## 这个文件怎么测

把目标模块在 `sys.modules` 里置成 `None`：CPython 随后对它的 `import` 会抛
`ImportError`（"import of X halted; None in sys.modules"）。这比真的改文件名干净
——不碰磁盘、跑完自动还原、也不会让别的测试收集失败。

## 两条要造插件的用例搬走了（2026-10-05 分支拆分）

`test_build_engine_survives_a_missing_role_module` 与
`test_build_engine_survives_a_missing_vision_module` 要**真的 import 插件的
`plugin.py`**（`roles_plugin` / `vision_plugin`）：前者验"没有 `call_action` 时
roles 插件明确拒绝装配"，后者验"撤掉插件的识图登记后 `engine.vision` 就是 `None`"。
本体侧的正常形态是插件文件夹不在，那两条在那种树上**根本不该存在**（不是"存在但
跳过"），所以**整条搬到 `tests/test_optional_capabilities_plugins.py`**，判据归插件侧。

留在这里的三条都不需要插件文件存在：`control_audit` 缺席、`available()` 可读、
`backfill_all()` 不伪造缺席那套 prompt（后两条把识图那套 prompt 藏起来，
验的正是"少了它照样起"）。断言一字未改。

## 2026-10-06（rebase 到本体 `a23f748` 之后）：藏模块要连**登记**一起造

本体把"识图这套 prompt 在不在"的判据从 `find_spec("…plugins.vision.vision")`
改成了"**插件登记过这一套没有**"（`prompt_library._provided_prompt` 读注册表）。
于是光把子模块 `vision.vision` 藏起来不再等于缺席：注册表上那一轮登记还在
（实测：藏子模块 + 再装配一次，`available()` 里照样有 `vision`）。
下面两条现在按新判据造缺席——**藏整个插件包**（`_Hidden("…plugins.vision")`，
`discover()` 就装不上它）**再装配一次**（`build_engine` 每次新造注册表，
"最新那一份"上于是真的没有识图这一条）。断言一字未改。
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
    """临时让某个模块（**连子树**）"看起来不存在"。

    `sys.modules[name] = None` 之后，`import name` 会抛
    `ImportError: import of ... halted; None in sys.modules`——这正是我们要守的降级分支。

    ## 为什么还要做另外两件事（2026-10-05 搬识图时被全量套件抓到）

    光置 `sys.modules` 只对"**第一次** import"有效：`plugin.py` 一旦被执行过，它就把
    `vision.vision` 绑进了自己的命名空间，`discover()` 走的是那个**旧引用**，插件照样
    注册得上。这个用例单跑是绿的、进了全量套件就红（实测 `loaded` 里躺着 `vision`、
    `engine.vision` 是个真 `ImageDescriber`）。所以 `__enter__` 里：

    1. 把子树里已经加载的模块从 `sys.modules` 摘掉（跑完原样放回）→ 逼 `plugin.py`
       重新 import，那时才会撞上第 2 条；
    2. 父包上那个属性置 `None`（`from 父包 import 兄弟模块` 这条捷径一起堵掉），
       `__exit__` 里恢复。
    """

    def __init__(self, *names: str) -> None:
        self._names = names
        self._saved: dict[str, object] = {}
        self._attrs: list[tuple[object, str, object]] = []

    def __enter__(self):
        import importlib

        for name in self._names:
            for cached in [key for key in sys.modules
                           if key == name or key.startswith(name + ".")]:
                self._saved[cached] = sys.modules.pop(cached)
            self._saved[name] = None
            sys.modules[name] = None
            parent_name, _, leaf = name.rpartition(".")
            try:
                parent = importlib.import_module(parent_name) if parent_name else None
            except ImportError:
                parent = None
            if parent is not None and hasattr(parent, leaf):
                self._attrs.append((parent, leaf, getattr(parent, leaf)))
                setattr(parent, leaf, None)
        return self

    def __exit__(self, *exc):
        for parent, leaf, value in self._attrs:
            setattr(parent, leaf, value)
        self._attrs.clear()
        for name, value in self._saved.items():
            if value is None:
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


def test_every_available_prompt_is_readable() -> None:
    """`available()` 里的每一套都必须能 `text()` 出来。

    面板的 prompt 列表照 `available()` 遍历，所以这条不成立时列表会整个 500。
    （这次修复前它遍历的是 `PROMPTS`，没有 `vision` 的部署上就会炸。）
    """

    from qq_roleplay_bot.prompt_library import PromptLibrary

    with _Hidden("qq_roleplay_bot.plugins.vision"):
        # 2026-10-06：判据是"插件**登记过**这一套没有"，所以缺席要这样造——
        # 藏掉整个插件包，再装配一次，让"最新那一份注册表"上真的没有识图这一条
        # （见模块头那段）。
        runtime.build_engine(_FakeTransport())
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
        with _Hidden("qq_roleplay_bot.plugins.vision"):
            runtime.build_engine(_FakeTransport())   # 同上面那条：缺席要造在登记那一侧
            library = PromptLibrary(Path(tmp))
            added = library.backfill_all()
        # 五套真的存在，识图那套被跳过
        assert added == 5, added
        versions = sorted(p.name.split(".")[0] for p in (Path(tmp) / "versions").glob("*.json"))
        assert "vision" not in versions, versions
        assert "persona" in versions, versions
