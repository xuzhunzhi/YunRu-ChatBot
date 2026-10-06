"""六套 system prompt 的可编辑层：`data/prompts/<name>.json` + 版本回滚。

由来（2026-10-01 用户）："面板可以用来修改 prompt"，且六套都要能改：

| 名字 | 内置默认 | 谁在用 |
| --- | --- | --- |
| `persona` | `base_prompt.BASE_PROMPT` | 回复 agent 的第一个人格层 |
| `reply` | `stage3_runtime.SYSTEM_PROMPT` | 回复 agent 的完整 system（人设 + 群聊底线 + 回话格式） |
| `judge` | `dialogue_judge.JUDGE_SYSTEM_PROMPT` | 判定 agent |
| `memory` | `memory_maintenance_agent.MEMORY_SYSTEM_PROMPT` | 记忆维护 agent |
| `review` | `style_reviewer.REVIEW_SYSTEM_PROMPT` | 风格审核 agent |
| `vision` | **`plugins/vision/` 插件登记的原稿**（原来是 `vision.VISION_SYSTEM_PROMPT`） | 识图 agent |

**`vision` 那一套为什么由插件给**（2026-10-05 搬识图时改）：识图是 Stage 4 插件，
核心不该知道那个模块叫什么、在哪。插件在 `register()` 里用
`registry.provide_prompt("vision", …)` 把原稿放上来，这里只按名字取——
**"登记过没有"就是"这次部署有没有它"**，不再靠 `except ImportError`
兜底（那正是这个文件原来那句 `from .vision import …` 的写法）。
其余五套照旧从核心那五个模块取。解析、校验、版本、回滚一个字没动。

> 2026-10-06 更正：这里曾经在"登记过没有"之外**另加一句**
> `find_spec("qq_roleplay_bot.plugins.vision.vision")` 当判据，于是核心又把插件的
> **模块全名**写了回来（只改名 = 面板上识图那一套凭空消失）。那句已删，现在判据
> 只有一条：**插件登记过这一套**（见 `_provided_prompt`）。

三条设计决定，每条都是为了"面板不能成为绕过边界的口子"：

1. **内置默认永远在**：面板只写覆盖文件；覆盖文件缺失/损坏/被"恢复默认"清掉，
   立刻回到代码里的那一份。所以面板坏了等于没装它，而不是"她没 prompt 了"。
2. **保存前必须过校验**（见 `validate`）：`reply` 必须保住 must-reply 派生的五个锚点
   —— 少一个的话 `derive_must_reply` 会抛错，而它是在**每条消息的路径上**调用的，
   等于改一次 prompt 就把回复打瘫。宁可拒绝保存。另外过机制词扫描（AGENTS 2.2）。
3. **版本留痕**：每次保存把**上一版**另存一份，最多 20 份，可回滚。

存储目录可以用 `QQBOT_PROMPT_DIR` 换（测试指向临时目录）。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path

from .prompt_guard import scan_text

logger = logging.getLogger(__name__)

STORE_VERSION = 1
#: 单套 prompt 的长度上限。20000 字足够写很长的人设，也挡得住"贴进来一整本书"。
MAX_CHARS = 20000
#: 每套留多少个历史版本。
MAX_VERSIONS = 20

#: 六个名字，面板与测试都用这份。
#:
#: `vision` 这一套**由 `plugins/vision/` 插件登记原稿**（见 `builtin`）——
#: 名字留在这里是为了面板的列表顺序与"保存/回滚某一套"的入参校验
#: （`webui_panel` 用 `name not in PROMPTS` 判合法性）；插件不在时
#: `available()` 会把它滤掉，面板那一行就自然消失。
PROMPTS = ("persona", "reply", "judge", "memory", "review", "vision")

#: 需要"必须回"派生版的 prompt（目前只有回复那一套用双 agent 分支）。
DERIVED = ("reply",)


class PromptRejected(ValueError):
    """面板提交的 prompt 不合法。`detail` 是给人看的原因（固定说法，不带内部细节）。"""

    def __init__(self, message: str, *, detail: str = "") -> None:
        super().__init__(message)
        self.detail = detail or message


def default_dir() -> Path:
    explicit = os.environ.get("QQBOT_PROMPT_DIR", "").strip()
    if explicit:
        return Path(explicit)
    base = os.environ.get("QQBOT_DATA_DIR", "").strip()
    root = Path(base).resolve() if base else Path(__file__).resolve().parents[2] / "data"
    return root / "prompts"


# --- must-reply 派生（从 `stage3_runtime` 搬过来，现在两边共用一份） -----------

#: 五处替换：把"要不要说"的那部分换成"已经决定要说"。
#: 用替换而不是另写一份，是为了避免两份人格 prompt 慢慢漂移。
MUST_REPLY_ANCHORS: tuple[tuple[str, str], ...] = (
    (
        "<decision>REPLY|NO_REPLY|EXIT_DIALOGUE</decision>\n",
        "",
    ),
    (
        "<reply>只在 REPLY 时填写聊天正文。",
        "<reply>填写你要说的聊天正文——这一轮必须填，不能留空。",
    ),
    (
        "确实没什么想说的，直接 NO_REPLY。一整天一句不搭反而不像你。",
        "把话说出来就好：不必硬找话，但**不要空着不答**，也不要想着一整天不搭话。",
    ),
    (
        "不要假设每句话都是对你说的，也不要因为有人在群里说话就必须表态。",
        "别硬把无关的话认成对你说的；但这一轮你已经决定开口——所以**别写"
        "“不是在问我”“这跟我无关”“刚才那句不是对你说的”这类撇清的话**，"
        "也别解释自己为什么会被叫到，直接把话说到具体的人和事上。",
    ),
    (
        "NO_REPLY 表示这次不发言但话题继续；EXIT_DIALOGUE 表示退出当前这场交谈且不发言。",
        "想说完这句就结束这场交谈时，把 <dialogue> 填成 EXIT；不结束就填 KEEP。",
    ),
)


def missing_anchors(text: str) -> list[str]:
    """派生需要的片段里，这份文本缺了哪几个（返回片段开头，便于人核对）。"""

    body = text or ""
    return [old[:24] for old, _new in MUST_REPLY_ANCHORS if old not in body]


def derive_must_reply(text: str) -> str:
    """把一份普通回复 prompt 派生成"必须回"版。缺片段时**大声失败**。

    找不到片段就必须抛错、不能悄悄跳过：那意味着"否决权又回到了回复 agent 手里"
    （原注释：`_replace_once` 的由来），是行为级的静默退化。
    """

    if not text or not text.strip():
        raise PromptRejected("prompt 不能为空")
    missing = missing_anchors(text)
    if missing:
        raise PromptRejected(
            "这份 prompt 少了必需片段，保存会被拒绝（否则回复会出错）",
            detail="缺少片段：" + " / ".join(missing),
        )
    result = text
    for old, new in MUST_REPLY_ANCHORS:
        result = result.replace(old, new, 1)
    return result


def _plugin_prompts() -> dict[str, str]:
    """插件登记上来的 prompt 原稿（`{名字: 文本}`）。取不到就返回空字典。

    **为什么在这里 import 插件注册表**：这是唯一一处"读插件登记的东西"的地方，
    而 `plugins` 自己不 import `prompt_library`（它只在方法体里延迟取），所以没有环。
    做成函数、每次现取：注册表在 `build_engine` 里才被造出来，而 `builtin()` 可能在
    它之前就被调用（例如某个测试直接问内置默认）。
    """

    from .plugins import registry as _registry

    table = _registry()
    if table is None:
        return {}
    return dict(getattr(table, "_prompt_defaults", {}) or {})


def _provided_prompt(name: str) -> str | None:
    """**插件登记过这一套 prompt 吗**——登记过就算它在（返回原稿，否则 `None`）。

    判据是**登记这件事本身**，不是"某个模块名在不在"。

    ## 为什么不是 `importlib.util.find_spec("…plugins.vision.vision")`

    2026-10-06 外部审查实测的那处耦合：这里原来有一句
    `find_spec("qq_roleplay_bot.plugins.vision.vision")` 当"识图在不在"的判据。
    后果是：**只把 `plugins/vision/` 改个名字**（插件自己照样装上、`register()` 照样
    把原稿登记上来），`available()` 里的识图那套就消失了——面板上那一行凭空不见。
    而且那句 `find_spec` 把插件的**模块全名**写进了核心，与
    `plugins/__init__.py` 那句"核心连识图这个模块名都不提了"**直接矛盾**
    （同一份文件里还留着"核心不知道那个模块叫什么、在哪"的注释，那句是假的）。

    登记口本来就是为这件事做的（2026-10-05 搬识图时加）：插件在自己 `register()`
    里 `provide_prompt(name, 原稿)`，核心只按名字取。所以"登记过 = 这一次部署有它"，
    改名 / 换实现 / 换目录都不影响。

    ## 为什么不担心"进程里那份注册表留着上一轮的原稿"

    `discover()` 是"登记发生了"的那一刻，而 `build_engine()` **每次装配都新造一个
    注册表**（`plugins/__init__.py` 的 `_REGISTRY` 只是"最新那一份"的活引用）。
    测试里把某个插件模块藏起来时，那次 `build_engine()` 走的是**新注册表**，
    它上面根本没有那一轮登记——所以这里问的就是当下的事实。
    """

    return _plugin_prompts().get(str(name))


class PromptLibrary:
    """六套 prompt 的读、写、版本、回滚。线程安全够用（文件操作是原子的）。"""

    def __init__(self, directory: Path | str | None = None, *, enabled: bool = True,
                 clock=time.time) -> None:
        self.directory = Path(directory) if directory is not None else default_dir()
        self.enabled = bool(enabled)
        self.clock = clock
        self._cache: dict[str, str] = {}
        self._loaded = False
        self.last_error = ""
        self.rejected = 0

    # --- 默认值 -----------------------------------------------------------

    @staticmethod
    def builtin(name: str) -> str:
        """代码里的那一份。**这是唯一的人设来源**（AGENTS 2.2）。"""

        if name == "persona":
            from .base_prompt import BASE_PROMPT

            return BASE_PROMPT
        if name == "reply":
            from .stage3_runtime import SYSTEM_PROMPT

            return SYSTEM_PROMPT
        if name == "judge":
            from .dialogue_judge import JUDGE_SYSTEM_PROMPT

            return JUDGE_SYSTEM_PROMPT
        if name == "memory":
            from .memory_maintenance_agent import MEMORY_SYSTEM_PROMPT

            return MEMORY_SYSTEM_PROMPT
        if name == "review":
            from .style_reviewer import REVIEW_SYSTEM_PROMPT

            return REVIEW_SYSTEM_PROMPT
        if name == "vision":
            # 识图是**可插能力**：`plugins/vision/` 整个文件夹可以不在这次的部署里。
            # 所以它的原稿**由插件登记**（`registry.provide_prompt("vision", …)`），
            # 这里只按名字取——核心这边**没有那个插件的模块名**（判据是"登记过没有"，
            # 不是"某个模块在不在"，见 `_provided_prompt`）。
            #
            # 没人登记（这次部署没有那个插件）时**明确拒绝**（`PromptRejected`），
            # 不要放 `ModuleNotFoundError` 出去：那会在启动期炸掉整个 `build_engine`
            # （2026-10-02 外部审查第四轮实测：修之前 `build_engine` →
            #  `prompts.backfill_all()` → `backfill()` 就到了这一行，也就是说
            #  "删掉识图不影响说话"这句话当时是假的）。
            provided = _provided_prompt("vision")
            if not provided:
                raise PromptRejected("识图不在这次部署里（没有插件登记这一套）")

            return provided
        raise PromptRejected(f"不认识的 prompt：{name}")

    @classmethod
    def available(cls) -> tuple[str, ...]:
        """这次部署里**真的存在**的那几套 prompt。

        可插能力缺席（例如没有 `vision` 模块）时对应的那一套会被滤掉——
        调用方（`backfill_all`、面板列表）应该照这个结果遍历，而不是照 `PROMPTS`
        硬遍历：`text("vision")` 在那种部署里会抛 `PromptRejected`，
        面板列表会整个 500。
        """

        names: list[str] = []
        for name in PROMPTS:
            try:
                cls.builtin(name)
            except PromptRejected:
                continue
            names.append(name)
        return tuple(names)

    # --- 读 ---------------------------------------------------------------

    def _path(self, name: str) -> Path:
        return self.directory / f"{name}.json"

    def _versions_dir(self, name: str) -> Path:
        return self.directory / "versions"

    def load(self) -> None:
        """把覆盖文件读进内存（启动时一次）。坏文件逐个跳过，不整份失败。"""

        self._cache = {}
        self.last_error = ""
        self._loaded = True
        if not self.enabled:
            return
        for name in PROMPTS:
            text = self._read_file(self._path(name))
            if text is not None:
                self._cache[name] = text

    def _read_file(self, path: Path) -> str | None:
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            self.last_error = type(exc).__name__
            logger.warning("prompt_read_failed file=%s category=%s", path.name,
                           type(exc).__name__)
            return None
        try:
            data = json.loads(raw)
        except ValueError:
            self.last_error = "invalid_json"
            logger.warning("prompt_invalid_json file=%s；用内置默认", path.name)
            return None
        if not isinstance(data, dict) or data.get("version") != STORE_VERSION:
            return None
        text = data.get("text")
        return text if isinstance(text, str) and text.strip() else None

    def text(self, name: str) -> str:
        """取当前生效的全文（覆盖层优先，否则内置默认）。"""

        key = str(name)
        if key not in PROMPTS:
            raise PromptRejected(f"不认识的 prompt：{name}")
        if not self._loaded:
            self.load()
        override = self._cache.get(key)
        if override:
            return override
        return self.builtin(key)

    def is_overridden(self, name: str) -> bool:
        if not self._loaded:
            self.load()
        return str(name) in self._cache

    def derived(self, name: str, *, must_reply: bool) -> str:
        """取"普通版"或"必须回版"。只有 `reply` 有派生；其它名字原样返回。"""

        body = self.text(name)
        if must_reply and name in DERIVED:
            return derive_must_reply(body)
        return body

    # --- 写 ---------------------------------------------------------------

    def validate(self, name: str, text: str) -> None:
        """不合法就抛 `PromptRejected`。理由见模块头第 2 条。"""

        if str(name) not in PROMPTS:
            raise PromptRejected(f"不认识的 prompt：{name}")
        body = text or ""
        if not body.strip():
            raise PromptRejected("prompt 不能为空")
        if len(body) > MAX_CHARS:
            raise PromptRejected(f"太长了（上限 {MAX_CHARS} 字）")
        if name in DERIVED:
            missing = missing_anchors(body)
            if missing:
                raise PromptRejected(
                    "这份 prompt 少了必需片段，保存会被拒绝（否则回复会出错）",
                    detail="缺少片段：" + " / ".join(missing),
                )
        hits = scan_text(name, body)
        if hits:
            raise PromptRejected(
                "这份 prompt 里有机制性措辞，会被角色吸收，保存被拒绝",
                detail="命中：" + "、".join(hits),
            )

    def save(self, name: str, text: str, *, source: str = "") -> str:
        """保存并返回生效后的文本。先校验，再留旧版，最后原子写。

        "留旧版"对**第一次保存**也成立：那一次先把内置默认留一版，
        否则"改坏了"就只剩"恢复内置默认"这一条退路，看不到自己改之前是什么样。
        """

        key = str(name)
        if key not in PROMPTS:
            raise PromptRejected(f"不认识的 prompt：{name}")
        body = str(text or "")
        self.validate(key, body)
        if not self._loaded:
            self.load()
        self.backfill(key)
        previous = self._cache.get(key)
        if previous is not None:
            self._stash(key, previous)
        self._write(key, body)
        self._cache[key] = body
        logger.warning("prompt_saved name=%s chars=%s source=%s", key, len(body), source or "-")
        return body

    def backfill(self, name: str) -> None:
        """如果这一套还没有覆盖、历史也是空的，就把**当前内置默认**留一版。

        这样"恢复内置默认"不会导致历史里丢掉原稿（那正是最想拿回来的一版）。
        幂等：已经有覆盖或已经有历史时什么都不做。

        返回是否真的补了一版。**可插能力缺席的那一套直接跳过**（返回 False）：
        不伪造占位 prompt——伪造一份会让面板看起来"有识图这套"，
        而它其实不在这次部署里。
        """

        key = str(name)
        if not self.enabled or key in self._cache:
            return False
        try:
            if any(self._versions_dir(key).glob(f"{key}.*.json")):
                return False
        except OSError:  # pragma: no cover
            return False
        try:
            builtin = self.builtin(key)
        except PromptRejected as exc:
            logger.info("prompt_backfill_skipped name=%s reason=%s", key, exc)
            return False
        self._stash(key, builtin)
        return True

    def reset(self, name: str, *, source: str = "") -> str:
        """恢复内置默认（把覆盖文件挪进版本目录，而不是删掉）。"""

        key = str(name)
        if key not in PROMPTS:
            raise PromptRejected(f"不认识的 prompt：{name}")
        if not self._loaded:
            self.load()
        current = self._cache.pop(key, None)
        if current is not None:
            self._stash(key, current)
            try:
                self._path(key).unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:  # pragma: no cover - 权限问题的降级
                logger.warning("prompt_reset_unlink_failed category=%s", type(exc).__name__)
        logger.warning("prompt_reset name=%s source=%s", key, source or "-")
        return self.builtin(key)

    def backfill_all(self) -> int:
        """启动时给每一套**存在**的 prompt 补一份"原稿"版本。返回实际补了几套。

        遍历的是 `available()` 而不是 `PROMPTS`：可插能力缺席的那几套会被跳过
        （见 `backfill`），所以"识图删掉之后核心照样起得来"这句话才成立。
        """

        if not self.enabled:
            return 0
        if not self._loaded:
            self.load()
        added = 0
        for name in self.available():
            if name in self._cache:
                continue
            try:
                existed = any(self._versions_dir(name).glob(f"{name}.*.json"))
            except OSError:  # pragma: no cover
                continue
            if existed:
                continue
            if self.backfill(name):
                added += 1
        if added:
            logger.info("已为 %s 套 prompt 留下原稿版本（%s）", added, self.directory)
        return added

    # --- 版本 -------------------------------------------------------------

    def _stash(self, name: str, text: str) -> None:
        """把一版存进 `versions/`，并只保留最近 `MAX_VERSIONS` 份。

        文件名用**微秒**时间戳，且撞名就往后挪一格：`int(clock())` 只有秒级、
        毫秒也有可能在同一次请求里连存两版（面板"保存 → 立刻再保存"就是这种），
        撞名会让后一版把前一版覆盖掉——而"刚才那版"正是想回滚的那一版。
        """

        if not self.enabled:
            return
        directory = self._versions_dir(name)
        try:
            directory.mkdir(parents=True, exist_ok=True)
            stamp = int(self.clock() * 1_000_000)
            target = directory / f"{name}.{stamp:016d}.json"
            while target.exists():          # 同一微秒内连存两版
                stamp += 1
                target = directory / f"{name}.{stamp:016d}.json"
            target.write_text(json.dumps(
                {"version": STORE_VERSION, "name": name, "text": text,
                 "saved_at": round(self.clock(), 3)}, ensure_ascii=False), encoding="utf-8")
        except OSError as exc:
            logger.warning("prompt_version_write_failed category=%s", type(exc).__name__)
            return
        self._prune_versions(name)

    def _prune_versions(self, name: str) -> None:
        try:
            files = sorted(self._versions_dir(name).glob(f"{name}.*.json"))
        except OSError:
            return
        for stale in files[:-MAX_VERSIONS]:
            try:
                stale.unlink()
            except OSError:  # pragma: no cover
                pass

    def versions(self, name: str) -> list[dict[str, object]]:
        """历史版本（新的在前）：`{id, saved_at, chars, overridden}`。"""

        if name not in PROMPTS:
            raise PromptRejected(f"不认识的 prompt：{name}")
        try:
            files = sorted(self._versions_dir(name).glob(f"{name}.*.json"), reverse=True)
        except OSError:
            return []
        rows: list[dict[str, object]] = []
        for path in files:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict):
                continue
            rows.append({
                "id": path.stem,
                "saved_at": data.get("saved_at", 0),
                "chars": len(str(data.get("text", ""))),
            })
        return rows
    def stash_current(self, name: str) -> None:
        """把**当前生效**的文本留一版（回滚前调用，保证操作可逆）。"""

        if name not in PROMPTS:
            raise PromptRejected(f"不认识的 prompt：{name}")
        self._stash(str(name), self.text(name))

    def read_version(self, name: str, version_id: str) -> str:
        if name not in PROMPTS:
            raise PromptRejected(f"不认识的 prompt：{name}")
        # 版本号来自 HTTP 路径，按**白名单形状**校验：只允许 `<name>.<13 位数字>`。
        # 不拼路径、不做 `..` 归一化，穿越就没有落脚点。
        safe = str(version_id)
        prefix = f"{name}."
        digits = safe[len(prefix):] if safe.startswith(prefix) else ""
        if not digits.isdigit():
            raise PromptRejected("版本号不合法")
        path = self._versions_dir(str(name)) / f"{prefix}{digits}.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise PromptRejected("没有这个版本") from None
        except (OSError, ValueError):
            raise PromptRejected("这个版本读不出来") from None
        text = data.get("text") if isinstance(data, dict) else None
        if not isinstance(text, str):
            raise PromptRejected("这个版本读不出来")
        return text

    # --- 落盘 -------------------------------------------------------------

    def _write(self, name: str, text: str) -> None:
        if not self.enabled:
            return
        path = self._path(name)
        body = {"version": STORE_VERSION, "name": name, "text": text,
                "saved_at": int(self.clock())}
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=str(path.parent),
                prefix=path.name + ".", suffix=".tmp", delete=False,
            )
            try:
                with handle:
                    json.dump(body, handle, ensure_ascii=False, indent=2)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(handle.name, path)
            except BaseException:
                Path(handle.name).unlink(missing_ok=True)
                raise
        except OSError as exc:
            logger.warning("prompt_write_failed category=%s", type(exc).__name__)
            raise PromptRejected("写不进去（磁盘或权限问题）") from None

    def stats(self) -> dict[str, object]:
        return {
            "directory": str(self.directory),
            "overridden": sorted(name for name in PROMPTS if self.is_overridden(name)),
            "last_error": self.last_error,
        }


def resolve(name: str, fallback: str) -> str:
    """取当前生效的某套 prompt；任何异常都回落到 `fallback`（调用方的模块常量）。

    这是**给独立 agent 的热更入口**：它们各自在组装 messages 时调一次，
    所以面板保存后下一次调用就读到新值。取不到覆盖版时返回内置默认——
    prompt 层出问题绝不能让那条链路失效。
    """

    try:
        return shared().text(name)
    except Exception:  # noqa: BLE001 - 降级不是失败
        return fallback


def shared() -> PromptLibrary:
    """进程内共用的一份（`runtime` 装配时 `install`）。"""

    global _SHARED
    if _SHARED is None:
        _SHARED = PromptLibrary()
    return _SHARED


def install(library: PromptLibrary) -> None:
    global _SHARED
    _SHARED = library


_SHARED: PromptLibrary | None = None
