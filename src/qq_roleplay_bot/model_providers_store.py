"""`data/model_providers.json` 的**唯一读写层**（核心侧契约 §3.4）。

## 这个文件是干什么的

六个用途（reply / letter / judge / memory / review / vision，用户口中的 "agent"）**每一个
都能单独切**：用哪一家、哪把 key、哪个模型名。这些值**成对**存在一个本地文件里，程序
直接读它；**改文件即生效，不用重启**。

文件形状（2026-10-18 第二轮，用户拍的新口径）：

    {"version": 2,
     "providers": {
       名字: {"base_url": …, "client_type"?,
              "models": [这家提供的模型名, …],
              "keys":   [{"id": "k1", "label": "主号"?, "value": "sk-…"}, …]}},
     "agents": {用途: {"provider": 名字, "key": id?, "model": …, "reasoning_effort"?}}}

## 为什么 key 是**列表**而不是按用途命名（用户 2026-10-18 原话）

> 「我倾向于**一个key一行**而不是一个供应商一行外加好多key。**agent那边先选供应商再选
> 对应的key最后选供应商提供的模型**。」

所以"用途 → key"这层关系**不写在 key 上**，写在 `agents.<用途>.key` 里（存的是那把
key 的 **id**，不是值）。一个供应商下面有几把 key 由 `keys` 列表决定，**不强制最少几把**
（同一天那句"填一把也行"）；一把都没有也读得下去（那几路会按回落链往 `.env` 走）。

三件套的顺序也照用户那句话写死：**provider → key → model**。模型名允许填
`providers.<名字>.models` 之外的值（面板上会标注"不在该供应商声明的模型里"）——中转站
常常临时上一个模型，写死白名单会让人没法用。

## 写盘纪律（契约 §6.6，如实写在这里）

- **进程内**（面板的 HTTP 线程 vs bot 主循环）：模块级 `threading.Lock` 包住
  "**锁内重读文件** → 改 → temp + `os.replace`"。**必须是锁内重读**，不能拿缓存那份做
  读改写——否则"面板加一个字段"会把"脚本刚写的另一个字段"覆盖掉。
- **跨进程**（`model_switch.py` 进程 vs bot 进程）：**不加锁**。Windows 上要
  `msvcrt.locking` 才做得到，与 "v2 小步" 不符。如实交代：两次写入撞在一起时
  **后写的赢、可能丢掉前一次的一个字段**；典型用法（脚本改一次、面板改一次，中间隔着
  动作）不会撞上。
- 读侧不加锁：`(mtime_ns, size)` 变了就**构建新的一份再一次性替换**（CPython 里是原子的）
  ⇒ 读方要么看到旧整份、要么看到新整份。【推断】依赖 CPython 语义，本项目只用 CPython。

## 用户 2026-10-18 补定的两条（写在这里，免得下一个人"顺手加密"或"顺手放开删除"）

1. **key 就明文存在 `providers.<名字>.keys[].value` 里**。`data/` 不进 git（`.gitignore`
   的 `/data/`），安全等级与 `.env` 相同。**面板那一页必须明写"这里能看到明文"**——
   那句话落在面板那一轮（不要在核心这里偷偷加一层"加密"：那会让 `.env` 与文件两种
   来源的排障方式变成两套，而且密钥最终还是得有一把能解它）。
2. **"正在用的那把 / 那一套"不许直接删**：`remove_provider_key()` 发现还有
   `agents.<用途>.key` 指着它时**拒绝**并点名哪几路；`remove_provider()` 对"还有用途
   指着这一套"同理。直接删的后果是那几路悄悄掉回环境变量那套（很可能当场没有 key）。

## 版本

`STORE_VERSION = 2`。v1（一个供应商一把 `key`）**整份忽略**并 WARNING、回落到 `.env`
那套（与 `operator_config.STORE_VERSION` 同一条纪律：看不懂的文件不猜、不覆盖）。
理由：v1 从未部署过，三棵树里都没有这种文件（实测 `dev/data`、`run/data`、
`yunru-stage4/data` 三处都不存在 `model_providers.json`），所以"忽略 + 说清楚"比写一段
没人跑过的迁移代码更安全；要迁移的话是单独一笔。

## import 方向（不许成环）

`model_config` → 本模块（import 读函数）；**本模块绝不 import `model_config`**，也
**不认识 `TASK_NAMES`**——用途名的合法性由读侧按自己那张表判（写侧只判"是个合法标识
符"）。两份用途名清单必然漂移，所以只能有一份。

`dev_config` 只在 `default_path()` 里**延迟** import：`dev_config` 在模块级就 import
`model_config`（它要 `provider_base_url`），而 `model_config` 又 import 本模块——写死了
就成了环。
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

#: 文件格式版本。缺失当 2；存在且 ≠2 → **整份忽略**并 WARNING（契约 D2）。
STORE_VERSION = 2

#: 把文件路径整体换掉的变量名（测试与"这份文件不在 `data/` 下"的部署用）。
#: 名字里的 PROVIDERS 是文件的语义，MODEL 是它的家族（`QQBOT_API_MODEL` 那一族）。
PATH_ENV = "QQBOT_MODEL_PROVIDERS_FILE"

#: 供应商名字的合法形状（写侧只判这个，不判"这个用途名认不认识"）。
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}$")

#: 一把 key 的 **id** 的合法形状（`agents.<用途>.key` 存的就是它）。
#:
#: 为什么 id 要单独一个字段、而不是"按列表下标"：下标会在删掉中间一把之后**整体挪位**
#: ——那意味着"我删了备用那把，结果回复换成了第三把"，是最坏的一类静默变更。id 稳定，
#: 删除只影响指着它的那几路（而它们会被 `remove_provider_key` 拒绝）。
_KEY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,31}$")

#: 落盘时卡住几个长度，免得一份手滑的文件把日志/内存撑爆。
_URL_MAX = 1000
_KEY_MAX = 8192
_MODEL_MAX = 200
_LABEL_MAX = 120
_MODELS_MAX = 64


class ModelProvidersRejected(ValueError):
    """一次写请求被**明确拒绝**。消息是给人看的：说清"为什么"与"先做什么"。"""


@dataclass(frozen=True)
class ProviderKey:
    """一把 key：**id**（给 `agents.<用途>.key` 指的东西）+ 可选标签 + 明文值。"""

    id: str
    label: str
    value: str


@dataclass(frozen=True)
class Snapshot:
    """某一刻的文件内容（不可变；几个 dict 都是普通 dict 的拷贝）。"""

    path: Path
    present: bool = False
    #: `(st_mtime_ns, st_size)`；缺文件时是 `None`。缓存与"变没变"都认它。
    token: tuple[int, int] | None = None
    mtime: float = 0.0
    #: 读失败的原因（`invalid_json` / `unsupported_version` / 异常类名）；空串 = 好的。
    last_error: str = ""
    version_ok: bool = True
    #: 供应商名 → **连接信息**（`base_url` / `client_type`）。**这里永远没有 key**
    #: （key 在下面 `keys` 那张表里：一个供应商多把，"一个key一行"）。
    providers: dict[str, dict[str, str]] = field(default_factory=dict)
    #: 供应商名 → **这家声明的模型名**（顺序即文件里的顺序；面板的下拉框就是它）。
    models: dict[str, tuple[str, ...]] = field(default_factory=dict)
    #: 供应商名 → **key 列表**（顺序 = 文件里的顺序 = 面板上从上到下；"第一把"就是它）。
    keys: dict[str, tuple[ProviderKey, ...]] = field(default_factory=dict)
    #: 用途名 → `{provider, key?, model, reasoning_effort?}`（未校验；`agents.key` 是 id）。
    agents: dict[str, dict[str, str]] = field(default_factory=dict)
    #: 单条坏条目（只废自己，不整份作废）+ 原因，给人看的短句。
    issues: tuple[str, ...] = ()
    #: 同一个地址被写成两个名字（用户 U3：同账号两名会让 402 兜底静默失效）。
    duplicate_urls: tuple[str, ...] = ()


_CACHE_LOCK = threading.Lock()
_CACHE: dict[str, Snapshot] = {}

#: 包住"锁内重读 → 改 → 原子替换"那把锁（进程内共用；见模块说明的 §6.6）。
_WRITE_LOCK = threading.RLock()


def default_path() -> Path:
    """文件默认落点：`QQBOT_MODEL_PROVIDERS_FILE` → `dev_config.data_dir()`。

    为什么延迟 import `dev_config`：它在模块级 import `model_config`，而
    `model_config` 又 import 本模块——写死了就成环（见模块说明）。
    """

    configured = os.environ.get(PATH_ENV, "").strip()
    if configured:
        # **不用 `Path.resolve()`**：它在 Windows 上每次要一次 `nt._getfinalpathname`
        # 系统调用（实测 0.3 ms/次），而这条路径在 `_providers_file()` 里是**热路径**
        # （每个用途每次 resolve 都会走）。`abspath` 只按当前目录拼一下，不碰文件系统；
        # 这里也不需要解析符号链接——读写两侧都用同一个字符串当键。
        return Path(os.path.abspath(os.path.expanduser(configured)))
    try:
        from .dev_config import data_dir  # 延迟 import：只为打破环
    except ImportError:
        # `dev_config` 正在 import 的窗口里（它模块级 import `model_config`，而
        # `model_config` import 本模块）——那时 `data_dir` 还没定义。按**同一条算法**
        # 现算一次（`QQBOT_DATA_DIR` 优先，否则 `<项目根>/data`），不让这个窗口变成崩溃。
        base = os.environ.get("QQBOT_DATA_DIR", "").strip()
        root = Path(base).resolve() if base else Path(__file__).resolve().parents[2] / "data"
        return root / "model_providers.json"
    return data_dir() / "model_providers.json"


# --- 读 -----------------------------------------------------------------------


def load(path: Path | str | None = None) -> Snapshot:
    """读一份当前内容。文件不存在 / 坏了都**不抛**（一份坏 JSON 不该让 bot 起不来）。"""

    target = Path(path) if path is not None else default_path()
    try:
        stat = target.stat()
    except FileNotFoundError:
        return Snapshot(path=target)
    except OSError as exc:
        return Snapshot(path=target, present=True, last_error=type(exc).__name__)
    token = (stat.st_mtime_ns, stat.st_size)
    with _CACHE_LOCK:
        cached = _CACHE.get(str(target))
    if cached is not None and cached.token == token:
        return cached
    snapshot = _parse(target, token, stat.st_mtime)
    with _CACHE_LOCK:
        _CACHE[str(target)] = snapshot
    return snapshot


def status(path: Path | str | None = None) -> dict[str, object]:
    """给自检 / 面板的读数。**只报事实**（路径 / 在不在 / 条目名 / 坏在哪）。

    只报 **key 的 id 与标签**，**绝不报值**：这一层从第一版起就不往外递明文，
    多 key 之后这一条只更重要（一次泄露就是好几把）。
    """

    snapshot = load(path)
    return {
        "path": str(snapshot.path),
        "present": snapshot.present,
        "mtime": round(snapshot.mtime, 3),
        "token": list(snapshot.token) if snapshot.token else None,
        "last_error": snapshot.last_error,
        "version_ok": snapshot.version_ok,
        "providers": sorted(snapshot.providers),
        #: 供应商 → 它的 key **id 列表**（顺序即"第一把"的顺序）。
        "provider_keys": {name: [item.id for item in items]
                          for name, items in sorted(snapshot.keys.items())},
        #: 供应商 → 它声明的模型名。
        "provider_models": {name: list(names)
                            for name, names in sorted(snapshot.models.items())},
        "agents": sorted(snapshot.agents),
        "issues": list(snapshot.issues),
        "notes": list(snapshot.duplicate_urls),
    }


def _parse(target: Path, token: tuple[int, int], mtime: float) -> Snapshot:
    try:
        raw = target.read_text(encoding="utf-8")
    except OSError as exc:
        return Snapshot(path=target, present=True, token=token, mtime=mtime,
                        last_error=type(exc).__name__)
    try:
        data = json.loads(raw)
    except ValueError:
        return Snapshot(path=target, present=True, token=token, mtime=mtime,
                        last_error="invalid_json")
    if not isinstance(data, dict):
        # 顶层不是对象：按"坏的"处理（`operator_config` 同一条纪律）。
        return Snapshot(path=target, present=True, token=token, mtime=mtime,
                        last_error="invalid_json")
    if data.get("version", STORE_VERSION) != STORE_VERSION:
        logger.warning("model_providers_unsupported_version；整份忽略，回落到环境变量那套")
        return Snapshot(path=target, present=True, token=token, mtime=mtime,
                        last_error="unsupported_version", version_ok=False)

    issues: list[str] = []
    providers: dict[str, dict[str, str]] = {}
    models: dict[str, tuple[str, ...]] = {}
    keys: dict[str, tuple[ProviderKey, ...]] = {}
    raw_providers = data.get("providers", {})
    if not isinstance(raw_providers, dict):
        issues.append("providers 不是对象：整段忽略")
        raw_providers = {}
    for name, entry in raw_providers.items():
        spec, model_names, key_list, why, warnings = _clean_provider(name, entry)
        if spec is None:
            issues.append(f"providers.{name}: {why}")
            continue
        normalized = str(name).strip().casefold()
        providers[normalized] = spec
        if model_names:
            models[normalized] = model_names
        if key_list:
            keys[normalized] = key_list
        for warning in warnings:
            issues.append(f"providers.{name}: {warning}")

    agents: dict[str, dict[str, str]] = {}
    raw_agents = data.get("agents", {})
    if not isinstance(raw_agents, dict):
        issues.append("agents 不是对象：整段忽略")
        raw_agents = {}
    for name, entry in raw_agents.items():
        cleaned, why = _clean_agent(name, entry)
        if cleaned is None:
            issues.append(f"agents.{name}: {why}")
            continue
        agents[str(name).strip()] = cleaned

    duplicates: list[str] = []
    by_url: dict[str, str] = {}
    for name, spec in providers.items():
        url = spec.get("base_url", "").rstrip("/")
        if not url:
            continue
        other = by_url.get(url)
        if other is not None:
            duplicates.append(
                f"providers.{other} 与 providers.{name} 是同一个地址（{url}）——"
                "同一个账号写成两个名字会让 402 余额兜底静默失效（用户 U3）")
            continue
        by_url[url] = name

    return Snapshot(path=target, present=True, token=token, mtime=mtime,
                    providers=providers, models=models, keys=keys, agents=agents,
                    issues=tuple(issues), duplicate_urls=tuple(duplicates))


def _clean_provider(name: object, entry: object) -> tuple[
        dict[str, str] | None, tuple[str, ...], tuple[ProviderKey, ...], str, list[str]]:
    """一个 `providers.<名字>` 条目 → `(连接信息, 模型名, key 列表, 丢弃原因, 警告)`。

    五个返回值而不是一个对象：读侧只有这一处用它，摊开比多一个 dataclass 更好读。
    `警告` 是"这一条还能用、但你有东西写错了"（未知形状 / 空的 key 值 / id 重复），
    由调用方记进 `issues`——**只废那一格，不废整套**（单条坏条目只废自己是既有纪律）。
    """

    text = str(name or "").strip()
    if not _NAME_RE.fullmatch(text):
        return None, (), (), "名字不合法（只允许字母/数字/下划线/点/连字符）", []
    if not isinstance(entry, dict):
        return None, (), (), "不是一个对象", []
    base_url = _one_line(entry.get("base_url"))[:_URL_MAX]
    if not base_url:
        return None, (), (), "缺 base_url（这家打哪儿必须写清）", []
    spec = {"base_url": base_url}
    client_type = _one_line(entry.get("client_type"))
    if client_type:
        spec["client_type"] = client_type
    model_names, model_warnings = _clean_models(entry.get("models"))
    key_list, key_warnings = _clean_key_list(entry.get("keys"))
    return spec, model_names, key_list, "", model_warnings + key_warnings


def _clean_models(raw: object) -> tuple[tuple[str, ...], list[str]]:
    """`models` → 去重后的模型名元组（**列表外的值不在这里判**，那是读侧的事）。"""

    if raw is None or raw == "":
        return (), []
    if not isinstance(raw, list):
        return (), ["models 不是数组：整段忽略"]
    names: list[str] = []
    warnings: list[str] = []
    for index, item in enumerate(raw):
        if not isinstance(item, (str, int, float)) or isinstance(item, bool):
            warnings.append(f"models[{index}] 不是字符串：忽略")
            continue
        text = _one_line(item)[:_MODEL_MAX]
        if not text:
            continue
        if text in names:
            warnings.append(f"models 里 {text} 重复：只算一次")
            continue
        if len(names) >= _MODELS_MAX:
            warnings.append(f"models 超过 {_MODELS_MAX} 个：后面那些忽略")
            break
        names.append(text)
    return tuple(names), warnings


def _clean_key_list(raw: object) -> tuple[tuple[ProviderKey, ...], list[str]]:
    """`keys` → `(ProviderKey, …)`（顺序即文件顺序）。

    四条判断（每条都对应一次"顺手就会破"）：

    1. **不是数组 → 整段忽略**（记账），这一家就当"还没有 key"；
    2. **`value` 为空的那一项忽略**：一把没有值的 key 不是 key，留着只会让"这一家有 key"
       与"这一路能跑"对不上；
    3. **`id` 缺省就自动给**（`k1` / `k2` / … 取第一个空位）：手写文件时不必先想名字，
       而"自动给的 id 可能被别人指走"这件事由读侧的下一次写自动补上（写侧永远显式给 id）；
    4. **id 重复 → 后面那个忽略**（记账）：两个 k1 会让"agents.key = k1"变成掷骰子。
    """

    if raw is None or raw == "":
        return (), []
    if not isinstance(raw, list):
        return (), ["keys 不是数组：整段忽略"]
    keys: list[ProviderKey] = []
    warnings: list[str] = []
    used: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            warnings.append(f"keys[{index}] 不是对象：忽略")
            continue
        value = _one_line(item.get("value"))[:_KEY_MAX]
        if not value:
            warnings.append(f"keys[{index}] 没有 value：忽略（一把没有值的 key 不是 key）")
            continue
        key_id = str(item.get("id") or "").strip()
        if key_id and not _KEY_ID_RE.fullmatch(key_id):
            warnings.append(f"keys[{index}] 的 id={key_id!r} 不合法（只允许字母/数字/"
                            "下划线/点/连字符）：这一项忽略")
            continue
        if not key_id:
            key_id = _next_key_id(used)
        if key_id in used:
            warnings.append(f"keys 里 id={key_id} 重复：后面那一项忽略")
            continue
        used.add(key_id)
        keys.append(ProviderKey(id=key_id, label=_one_line(item.get("label"))[:_LABEL_MAX],
                                value=value))
    return tuple(keys), warnings


def _next_key_id(used: object) -> str:
    """第一个没被占用的 `k<n>`（从 k1 起）。**顺序稳定**，两份文件的 diff 才可读。"""

    taken = {str(item) for item in (used or ())}
    index = 1
    while f"k{index}" in taken:
        index += 1
    return f"k{index}"


def _clean_agent(name: object, entry: object) -> tuple[dict[str, str] | None, str]:
    text = str(name or "").strip()
    if not _NAME_RE.fullmatch(text):
        return None, "名字不合法"
    if not isinstance(entry, dict):
        return None, "不是一个对象"
    cleaned: dict[str, str] = {}
    for key in ("provider", "key", "model", "reasoning_effort"):
        value = entry.get(key, "")
        if isinstance(value, (str, int, float)) and str(value).strip():
            cleaned[key] = str(value).strip()
    return (cleaned or None), ("" if cleaned else "是空的")


# --- 写 -----------------------------------------------------------------------


def _atomic_write(target: Path, data: dict[str, object]) -> None:
    """一次 `os.replace` 落地（原子跃迁：读侧看不到中间态，契约 D11）。"""

    body = {
        "version": STORE_VERSION,
        "providers": dict(sorted((name, _writable_provider(entry))
                                 for name, entry in (data.get("providers") or {}).items())),
        "agents": dict(sorted((data.get("agents") or {}).items())),
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=str(target.parent),
        prefix=target.name + ".", suffix=".tmp", delete=False,
    )
    try:
        with handle:
            json.dump(body, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(handle.name, target)
    except BaseException:
        Path(handle.name).unlink(missing_ok=True)
        raise
    _restrict(target)
    # `os.replace` 已经换了 mtime；这里再显式碰一下是给"同一秒内写两次"留一条硬保证
    # （读侧缓存与 `sync_from_file()` 都靠 mtime 判"变没变"，撞成同一个 ns 就会漏一次）。
    try:
        now = time.time()
        os.utime(target, (now, now))
    except OSError:  # pragma: no cover - 某些文件系统不支持 utime，不影响正确性
        pass
    with _CACHE_LOCK:
        _CACHE.pop(str(target), None)


def _writable_provider(entry: object) -> dict[str, object]:
    """落盘前把一个 `providers.<名字>` 条目收成唯一一种写法。

    形状：`base_url` / `client_type` / `models` / `keys`。**不认识的顶层字段原样带回去**
    （不是我们的写口放进去的，就不该被我们悄悄删掉）。
    """

    if not isinstance(entry, dict):
        return {"base_url": ""}
    result: dict[str, object] = {}
    base_url = _one_line(entry.get("base_url"))
    if base_url:
        result["base_url"] = base_url
    client_type = _one_line(entry.get("client_type"))
    if client_type:
        result["client_type"] = client_type
    for field_name, value in entry.items():
        if field_name not in {"base_url", "client_type", "models", "keys"}:
            result[field_name] = value
    models, _warnings = _clean_models(entry.get("models"))
    if models:
        result["models"] = list(models)
    keys, _warnings = _clean_key_list(entry.get("keys"))
    if keys:
        result["keys"] = [{"id": item.id,
                           **({"label": item.label} if item.label else {}),
                           "value": item.value} for item in keys]
    else:
        result["keys"] = []
    return result


def _restrict(target: Path) -> None:
    """收紧权限：这份文件**明文存着 key**（用户 2026-10-18 拍定）。

    Windows 上 `chmod` 只是尽力而为（契约 §3.4 / `operator_config._restrict` 同一套）。
    """

    try:
        os.chmod(target, 0o600)
    except OSError:  # pragma: no cover - 平台差异，权限收紧失败不影响功能
        pass


def _read_for_write(target: Path) -> dict[str, object]:
    """**锁内**重读文件（绝不拿缓存那份做读改写，见模块说明 §6.6）。"""

    try:
        raw = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"version": STORE_VERSION, "providers": {}, "agents": {}}
    except OSError as exc:
        raise ModelProvidersRejected(
            f"读不了 {target.name}（{type(exc).__name__}）：不敢写，怕把现有内容覆盖掉"
        ) from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ModelProvidersRejected(
            f"{target.name} 不是合法 JSON：先修好它（或删掉它）再写，绝不覆盖") from exc
    if not isinstance(data, dict):
        raise ModelProvidersRejected(f"{target.name} 的顶层不是对象：先修好它再写")
    if data.get("version", STORE_VERSION) != STORE_VERSION:
        raise ModelProvidersRejected(
            f"{target.name} 的 version={data.get('version')!r}，这份代码只认 {STORE_VERSION}："
            "先把它手工改回来或删掉，不要由我们来覆盖一份看不懂的文件"
            "（v1 是「一个供应商一把 key」的旧形状，这一版整份忽略）")
    if not isinstance(data.get("providers", {}), dict):
        raise ModelProvidersRejected(f"{target.name} 的 providers 不是对象：先修好它再写")
    if not isinstance(data.get("agents", {}), dict):
        raise ModelProvidersRejected(f"{target.name} 的 agents 不是对象：先修好它再写")
    data["version"] = STORE_VERSION
    data.setdefault("providers", {})
    data.setdefault("agents", {})
    return data


def _provider_entry(data: dict[str, object], name: str) -> dict[str, object] | None:
    providers = data.get("providers") or {}
    entry = providers.get(name) if isinstance(providers, dict) else None
    return entry if isinstance(entry, dict) else None


def _key_list(entry: object) -> list[dict[str, str]]:
    """一个供应商条目现在的 key 列表（**写侧**用的普通 dict 形态，含明文值）。"""

    if not isinstance(entry, dict):
        return []
    cleaned, _warnings = _clean_key_list(entry.get("keys"))
    return [{"id": item.id, "label": item.label, "value": item.value} for item in cleaned]


def set_provider(name: str, *, base_url: str, client_type: str | None = None,
                 models: object = None, audit=None, source: str = "",
                 token: str = "", path: Path | str | None = None) -> dict[str, object]:
    """加/改**一套**的连接信息（`base_url` / `client_type` / `models`），**不碰 key**。

    为什么把 key 从这一口拿出去（2026-10-18 第二轮）：key 现在是**列表**（一个供应商多把，
    "一个key一行"），增删改各是一次独立动作（`put_provider_key` / `remove_provider_key`）。
    把"列表"塞进"改地址"这一口，等于每次改地址都可能把别人的 key 覆盖掉。
    """

    target = Path(path) if path is not None else default_path()
    clean_name = _provider_name(name)
    clean_url = _url(base_url)
    model_names = None if models is None else _clean_models(models)[0]
    with _WRITE_LOCK:
        data = _read_for_write(target)
        providers: dict[str, dict[str, object]] = data["providers"]  # type: ignore[assignment]
        existing = _find_by_url(providers, clean_url, exclude=clean_name)
        if existing is not None:
            entry = providers[existing]
            if model_names is not None:
                entry["models"] = list(model_names)
            _atomic_write(target, data)
            _audit(audit, "model_providers", {
                "action": "add", "provider": existing, "requested": clean_name,
                "base_url": str(entry.get("base_url", "") or ""),
                "models": list(model_names or ()),
            }, result=f"按地址收敛到 {existing}", source=source, token=token)
            return {"name": existing, "converged": True, "requested": clean_name,
                    "base_url": str(entry.get("base_url", "") or ""),
                    "models": list(model_names or ()),
                    "note": f"同一地址已经有 providers.{existing}，按地址收敛（用户 U3）"}
        entry = dict(providers.get(clean_name) or {})
        entry["base_url"] = clean_url
        if client_type:
            entry["client_type"] = _one_line(client_type)
        elif "client_type" not in entry:
            entry["client_type"] = "openai"
        if model_names is not None:
            entry["models"] = list(model_names)
        providers[clean_name] = entry
        _atomic_write(target, data)
        _audit(audit, "model_providers", {
            "action": "add", "provider": clean_name, "base_url": clean_url,
            "models": list(model_names or ()),
        }, result="written", source=source, token=token)
        return {"name": clean_name, "converged": False, "requested": clean_name,
                "base_url": clean_url, "models": list(model_names or ()), "note": ""}


def put_provider_key(name: str, *, key_id: str = "", label: str | None = None,
                     value: str | None = None, audit=None, source: str = "",
                     token: str = "", path: Path | str | None = None) -> dict[str, object]:
    """**加一把 / 改一把** key（`key_id=""` = 追加到列表末尾）。

    - 追加时 `value` 不能为空（一把没有值的 key 不是 key）；
    - 改的时候 `value=None` 或空串 = **不动这一把的值**（面板只拿得到掩码，交上来的空
      输入框就是"没改"，不是"清空"——真要删用 `remove_provider_key`）；
    - `label` 同理（`None` = 不动）。
    """

    target = Path(path) if path is not None else default_path()
    clean_name = _provider_name(name)
    clean_id = str(key_id or "").strip()
    if clean_id and not _KEY_ID_RE.fullmatch(clean_id):
        raise ModelProvidersRejected(f"key 的 id 不合法：{key_id!r}（只允许字母/数字/"
                                      "下划线/点/连字符）")
    text_value = _one_line(value)[:_KEY_MAX]
    text_label = None if label is None else _one_line(label)[:_LABEL_MAX]
    with _WRITE_LOCK:
        data = _read_for_write(target)
        entry = _provider_entry(data, clean_name)
        if entry is None:
            existing = sorted((data.get("providers") or {}))
            raise ModelProvidersRejected(
                f"文件里没有 providers.{clean_name} 这一套"
                f"（现有的：{'、'.join(existing) or '一个都没有'}）："
                "先在「添加一套」里把这一家加进来，再给它加 key")
        keys = _key_list(entry)
        created = False
        if clean_id:
            found = next((item for item in keys if item["id"] == clean_id), None)
            if found is None:
                raise ModelProvidersRejected(
                    f"providers.{clean_name} 里没有 id={clean_id} 这把 key"
                    f"（它现在有：{'、'.join(item['id'] for item in keys) or '一把都没有'}）")
            if text_value:
                found["value"] = text_value
            if text_label is not None:
                found["label"] = text_label
        else:
            if not text_value:
                raise ModelProvidersRejected(
                    "新加一把 key 必须给值（空值不能算一把 key；要「留空」就别加）")
            clean_id = _next_key_id([item["id"] for item in keys])
            keys.append({"id": clean_id, "label": text_label or "", "value": text_value})
            created = True
        entry["keys"] = keys
        _atomic_write(target, data)
        _audit(audit, "model_providers", {
            "action": "key", "provider": clean_name, "key_id": clean_id,
            "created": created, "value_changed": bool(text_value),
            "label": "" if text_label is None else "[已改]",
        }, result="created" if created else "updated", source=source, token=token)
        return {"name": clean_name, "id": clean_id, "created": created,
                "count": len(keys)}


def remove_provider_key(name: str, *, key_id: str, audit=None, source: str = "",
                        token: str = "", path: Path | str | None = None
                        ) -> dict[str, object]:
    """删掉**一把** key。**还有 `agents.<用途>.key` 指着它就拒绝**（点名叫哪几路）。

    理由与 `remove_provider` 那条一模一样（用户 2026-10-18："删除操作必须先切到别的、
    再删"）：直接删的后果是那几路悄悄掉回"这一套的第一把"或 `.env`——它们当场可能没有
    key，而日志上看起来只是"某个 agent 忽然不说话了"。
    """

    target = Path(path) if path is not None else default_path()
    clean_name = _provider_name(name)
    clean_id = str(key_id or "").strip()
    if not clean_id:
        raise ModelProvidersRejected("要删哪一把 key？缺 key_id")
    with _WRITE_LOCK:
        data = _read_for_write(target)
        entry = _provider_entry(data, clean_name)
        if entry is None:
            raise ModelProvidersRejected(f"文件里没有 providers.{clean_name} 这一套")
        keys = _key_list(entry)
        if not any(item["id"] == clean_id for item in keys):
            raise ModelProvidersRejected(
                f"providers.{clean_name} 里没有 id={clean_id} 这把 key"
                f"（它现在有：{'、'.join(item['id'] for item in keys) or '一把都没有'}）")
        agents: dict[str, dict[str, str]] = data["agents"]  # type: ignore[assignment]
        users = sorted(task for task, spec in agents.items()
                       if str(spec.get("provider", "") or "").strip().casefold() == clean_name
                       and str(spec.get("key", "") or "").strip() == clean_id)
        if users:
            raise ModelProvidersRejected(
                f"providers.{clean_name} 的 {clean_id} 还被 "
                + "、".join(f"agents.{task}" for task in users)
                + " 用着：删掉它这几路会悄悄掉回别的 key（很可能当场没有 key）。"
                "先在「各路绑定」里把它们换一把，再删。")
        entry["keys"] = [item for item in keys if item["id"] != clean_id]
        _atomic_write(target, data)
        _audit(audit, "model_providers", {
            "action": "key_removed", "provider": clean_name, "key_id": clean_id,
        }, result="removed", source=source, token=token)
        return {"name": clean_name, "id": clean_id, "removed": True,
                "count": len(entry["keys"])}


def upsert_agent(task: str, *, provider: str, model: str, key_id: str | None = None,
                 base_url: str = "", reasoning_effort: str | None = None,
                 audit=None, source: str = "", token: str = "",
                 path: Path | str | None = None) -> dict[str, object]:
    """把**一个用途**绑成 `provider → key → model` 三件套（用户 2026-10-18 那句的顺序）。

    `key_id` 三种含义（这一条是本文件的语义核心，别混）：

    - `None` = **不动**这一路现有的 key 绑定（只改 provider / model 时用它）；
    - `""`   = **解开**绑定（写侧把 `key` 从条目里删掉 ⇒ 读侧按"这一套的第一把"走）；
    - `"k2"` = 绑到那把（**必须真在这一家的 key 列表里**，否则拒绝：悬空 id 会让这一路
      当场没有 key，而日志上看着只是"没配"）。
    """

    target = Path(path) if path is not None else default_path()
    clean_task = _ident(task, "用途")
    clean_name = _provider_name(provider)
    clean_model = _one_line(model)[:_MODEL_MAX]
    if not clean_model:
        raise ModelProvidersRejected("模型名不能为空：换供应商时必须一起说清用哪个模型")
    clean_url = _url(base_url)
    clean_key = None if key_id is None else str(key_id or "").strip()
    if clean_key and not _KEY_ID_RE.fullmatch(clean_key):
        raise ModelProvidersRejected(f"key 的 id 不合法：{key_id!r}")
    with _WRITE_LOCK:
        data = _read_for_write(target)
        providers: dict[str, dict[str, object]] = data["providers"]  # type: ignore[assignment]
        agents: dict[str, dict[str, str]] = data["agents"]  # type: ignore[assignment]
        provider_from = str((agents.get(clean_task) or {}).get("provider", "") or "")
        converged = False
        if clean_name in providers:
            entry = providers[clean_name]
            if clean_url:
                entry["base_url"] = clean_url
            clean_url = str(entry.get("base_url", "") or "")
        else:
            if not clean_url:
                raise ModelProvidersRejected(
                    f"新的供应商 {clean_name} 必须同时给地址（地址与 key 成对落在这里）")
            other = _find_by_url(providers, clean_url)
            if other is not None:
                clean_name = other
                clean_url = str(providers[other].get("base_url", "") or clean_url)
                entry = providers[other]
                converged = True
                logger.info("model_providers_url_converged requested=%s -> %s",
                            provider, other)
            else:
                entry = {"base_url": clean_url, "client_type": "openai", "keys": []}
                providers[clean_name] = entry
        entry.setdefault("base_url", clean_url)
        previous = dict(agents.get(clean_task) or {})
        resolved_key = previous.get("key", "") if clean_key is None else clean_key
        if resolved_key:
            available = [item["id"] for item in _key_list(entry)]
            if resolved_key not in available:
                raise ModelProvidersRejected(
                    f"providers.{clean_name} 里没有 id={resolved_key} 这把 key"
                    f"（它现在有：{'、'.join(available) or '一把都没有'}）："
                    "悬空的 id 会让这一路当场没有 key，所以这里不敢写")
        agent_entry: dict[str, str] = {"provider": clean_name, "model": clean_model}
        if resolved_key:
            agent_entry["key"] = resolved_key
        effort = (reasoning_effort if reasoning_effort is not None
                  else previous.get("reasoning_effort", ""))
        if effort:
            agent_entry["reasoning_effort"] = _one_line(effort)
        agents[clean_task] = agent_entry
        _atomic_write(target, data)
        _audit(audit, "model_providers", {
            "action": "agent", "task": clean_task, "provider": clean_name,
            "provider_from": provider_from, "model": clean_model,
            "base_url": entry.get("base_url", ""),
            "key_id": resolved_key, "key_changed": clean_key is not None,
        }, result=f"{clean_task}: {provider_from or '(今天那套)'} -> {clean_name}",
            source=source, token=token)
        return {"task": clean_task, "provider": clean_name, "provider_from": provider_from,
                "model": clean_model, "base_url": entry.get("base_url", ""),
                "key_id": resolved_key, "converged": converged,
                "key_changed": clean_key is not None}


def clear_agent(task: str, *, audit=None, source: str = "", token: str = "",
                path: Path | str | None = None) -> dict[str, object]:
    """把**一个用途**从文件里删掉 ⇒ 它回到环境变量那套（契约 D12 / §6.8 的第 2 条退路）。

    **没有文件、或文件里本来就没这一路 → 一个字节都不写**。为什么这条要写清楚：面板上
    把某一项清空是很常见的动作，而"清空"绝不能顺手创建一份空文件——"没有这个文件"是
    "今天那套"的锚点（§5.2 / §6.8），一条 no-op 把锚点消灭掉，回滚与对拍就失去基准。
    """

    target = Path(path) if path is not None else default_path()
    clean_task = _ident(task, "用途")
    with _WRITE_LOCK:
        if not target.exists():
            return {"task": clean_task, "cleared": False, "provider_from": "",
                    "noop": True}
        data = _read_for_write(target)
        agents: dict[str, dict[str, str]] = data["agents"]  # type: ignore[assignment]
        if clean_task not in agents:
            return {"task": clean_task, "cleared": False, "provider_from": "",
                    "noop": True}
        previous = dict(agents.pop(clean_task, None) or {})
        _atomic_write(target, data)
        _audit(audit, "model_providers", {
            "action": "agent_cleared", "task": clean_task,
            "provider_from": str(previous.get("provider", "") or ""),
        }, result=f"{clean_task}: 回到环境变量那套", source=source, token=token)
        return {"task": clean_task, "cleared": bool(previous),
                "provider_from": str(previous.get("provider", "") or "")}


def remove_provider(name: str, *, audit=None, source: str = "", token: str = "",
                    path: Path | str | None = None) -> dict[str, object]:
    """删掉**一套**（一个 `providers.<名字>`）。**还有 agent 指着它时拒绝**。

    用户 2026-10-18 补定（原话意思）："删除操作必须先切到别的套、再删"。所以这里没有
    `force`——要删就先在「各路绑定」里把指着它的路换掉，然后再删。
    拒绝理由里点名**是哪几路**在用，别说"不允许删除"这种让人无从下手的话。
    """

    target = Path(path) if path is not None else default_path()
    clean_name = _provider_name(name)
    with _WRITE_LOCK:
        data = _read_for_write(target)
        providers: dict[str, dict[str, object]] = data["providers"]  # type: ignore[assignment]
        agents: dict[str, dict[str, str]] = data["agents"]  # type: ignore[assignment]
        if clean_name not in providers:
            raise ModelProvidersRejected(
                f"文件里没有 providers.{clean_name} 这一套"
                f"（现有的：{'、'.join(sorted(providers)) or '一个都没有'}）")
        users = sorted(task for task, entry in agents.items()
                       if str(entry.get("provider", "") or "").strip().casefold() == clean_name)
        if users:
            raise ModelProvidersRejected(
                f"providers.{clean_name} 还被 " + "、".join(f"agents.{task}" for task in users)
                + " 用着：删掉它这几路会悄悄掉回环境变量那套（很可能当场没有 key）。"
                "先把它们绑到别家（面板「各路绑定」，或 `use <用途> <别的名字>`），再删。")
        removed = providers.pop(clean_name)
        _atomic_write(target, data)
        _audit(audit, "model_providers", {
            "action": "remove", "provider": clean_name,
            "base_url": str(removed.get("base_url", "") or ""),
        }, result="removed", source=source, token=token)
        return {"name": clean_name, "removed": True}


def _find_by_url(providers: dict[str, dict[str, object]], base_url: str,
                 *, exclude: str = "") -> str | None:
    """同一个地址只允许一个名字（用户 U3：同账号两名会让 402 兜底静默失效）。"""

    wanted = base_url.rstrip("/")
    if not wanted:
        return None
    for name, entry in providers.items():
        if name == exclude:
            continue
        if str(entry.get("base_url", "") or "").rstrip("/") == wanted:
            return name
    return None


def _provider_name(value: str) -> str:
    text = str(value or "").strip()
    if not _NAME_RE.fullmatch(text):
        raise ModelProvidersRejected(
            f"供应商名字不合法：{value!r}（只允许字母/数字/下划线/点/连字符，最长 64）")
    return text.casefold()


def _ident(value: str, what: str) -> str:
    text = str(value or "").strip()
    if not _NAME_RE.fullmatch(text):
        raise ModelProvidersRejected(f"{what}名不合法：{value!r}")
    return text


def _url(value: str) -> str:
    text = _one_line(value)
    if len(text) > _URL_MAX:
        raise ModelProvidersRejected("地址太长（超过 1000 字符）")
    return text


def _one_line(value: object) -> str:
    """写进文件的值一律压成一行（这个文件是给程序和眼睛一起看的，不吃换行）。"""

    if value is None or isinstance(value, bool):
        return ""
    return " ".join(str(value).split())


def _audit(audit, action: str, detail: dict[str, object], *, result: str,
           source: str, token: str) -> None:
    """写侧落审计（谁 / 什么时候 / 从哪套到哪套），复用 `control_audit`。

    审计失败**绝不**挡住那次写：面板/脚本已经改成功了，记不上账是次要问题
    （与 `control_audit.ControlAudit.record` 自己的纪律一致）。
    注意 detail 里**没有任何 key 值**：`value_changed` 只是布尔、`label` 只记"[已改]"。
    """

    if audit is None:
        return
    try:
        audit.record(action, detail=detail, result=result, source=source, token=token)
    except Exception:  # noqa: BLE001 - 审计失败不能挡住写
        logger.warning("model_providers_audit_failed", exc_info=True)


def reset_cache() -> None:
    """把读缓存清空（测试与"我刚换过路径"用；不碰任何文件）。"""

    with _CACHE_LOCK:
        _CACHE.clear()
