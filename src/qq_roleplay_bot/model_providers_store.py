"""`data/model_providers.json` 的**唯一读写层**（核心侧契约 §3.4）。

## 这个文件是干什么的

六个用途（reply / letter / judge / memory / review / vision，用户口中的 "agent"）**每一个
都能单独切**：用哪一家、哪个地址、哪把 key、哪个模型名。这些值**成对**存在一个本地
文件里，程序直接读它；**改文件即生效，不用重启**。

文件形状（用户定的，本方案不改）：

    {"version": 1,
     "providers": {名字: {"base_url": ..., "key": ..., "client_type"?}},
     "agents":    {用途: {"provider": 名字, "model": ..., "reasoning_effort"?}}}

## 为什么写层只有这一份实现

脚本（`data/model_switch.py`）与（下一轮要做的）面板**都调这里的函数**，不许自己拼
JSON 直写。理由是很具体的一条：两个写者两套纪律，一旦不一致，出现的就是"脚本写的
能生效、面板写的不能"——那是最难查的一类故障（契约 §3.1 作废的 B7 就是它）。

## 用户 2026-10-18 补定的两条（写在这里，免得下一个人"顺手加密"或"顺手放开删除"）

1. **key 就明文存在 `providers.<名字>.key` 里**。`data/` 不进 git（`.gitignore` 的
   `/data/`），安全等级与 `.env` 相同。**面板侧以后必须明写"这里能看到明文"**——
   这一笔只做核心，那句话留给面板那一轮（不要在核心这里偷偷加一层"加密"：那会让
   `.env` 与文件两种来源的排障方式变成两套，而且密钥最终还是得有一把能解它）。
2. **"当前那一套"不许直接删**：`remove_provider()` 发现还有 `agents.<用途>` 指着它时
   **拒绝**并给出可读原因——先把那几路切到别的家，再删。直接删的后果是那几路悄悄
   掉回环境变量那套（很可能当场没有 key）。

## 写盘纪律（契约 §6.6，如实写在这里）

- **进程内**（面板的 HTTP 线程 vs bot 主循环）：模块级 `threading.Lock` 包住
  "**锁内重读文件** → 改 → temp + `os.replace`"。**必须是锁内重读**，不能拿缓存那份做
  读改写——否则"面板加一个字段"会把"脚本刚写的另一个字段"覆盖掉。
- **跨进程**（`model_switch.py` 进程 vs bot 进程）：**不加锁**。Windows 上要
  `msvcrt.locking` 才做得到，与 "v1 小步" 不符。如实交代：两次写入撞在一起时
  **后写的赢、可能丢掉前一次的一个字段**；典型用法（脚本改一次、面板改一次，中间隔着
  动作）不会撞上。
- 读侧不加锁：`(mtime_ns, size)` 变了就**构建新的一份再一次性替换**（CPython 里是原子的）
  ⇒ 读方要么看到旧整份、要么看到新整份。【推断】依赖 CPython 语义，本项目只用 CPython。

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

#: 文件格式版本。缺失当 1；存在且 ≠1 → **整份忽略**并 WARNING（契约 D2，与
#: `operator_config.STORE_VERSION` 同一条纪律：两份"面板的存储"用同一种退路）。
STORE_VERSION = 1

#: 把文件路径整体换掉的变量名（测试与"这份文件不在 `data/` 下"的部署用）。
#: 名字里的 PROVIDERS 是文件的语义，MODEL 是它的家族（`QQBOT_API_MODEL` 那一族）。
PATH_ENV = "QQBOT_MODEL_PROVIDERS_FILE"

#: 供应商名字的合法形状（写侧只判这个，不判"这个用途名认不认识"）。
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}$")

#: 落盘时卡住两个长度，免得一份手滑的文件把日志/内存撑爆。
_URL_MAX = 1000
_KEY_MAX = 8192
_MODEL_MAX = 200


class ModelProvidersRejected(ValueError):
    """一次写请求被**明确拒绝**。消息是给人看的：说清"为什么"与"先做什么"。"""


@dataclass(frozen=True)
class Snapshot:
    """某一刻的文件内容（不可变；`providers` / `agents` 是普通 dict 的拷贝）。"""

    path: Path
    present: bool = False
    #: `(st_mtime_ns, st_size)`；缺文件时是 `None`。缓存与"变没变"都认它。
    token: tuple[int, int] | None = None
    mtime: float = 0.0
    #: 读失败的原因（`invalid_json` / `unsupported_version` / 异常类名）；空串 = 好的。
    last_error: str = ""
    version_ok: bool = True
    providers: dict[str, dict[str, str]] = field(default_factory=dict)
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
    """给自检 / 面板的读数。**只报事实**（路径 / 在不在 / 条目名 / 坏在哪）。"""

    snapshot = load(path)
    return {
        "path": str(snapshot.path),
        "present": snapshot.present,
        "mtime": round(snapshot.mtime, 3),
        "token": list(snapshot.token) if snapshot.token else None,
        "last_error": snapshot.last_error,
        "version_ok": snapshot.version_ok,
        "providers": sorted(snapshot.providers),
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
    raw_providers = data.get("providers", {})
    if not isinstance(raw_providers, dict):
        issues.append("providers 不是对象：整段忽略")
        raw_providers = {}
    for name, entry in raw_providers.items():
        cleaned, why = _clean_provider(name, entry)
        if cleaned is None:
            issues.append(f"providers.{name}: {why}")
            continue
        providers[str(name).strip().casefold()] = cleaned

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
                    providers=providers, agents=agents, issues=tuple(issues),
                    duplicate_urls=tuple(duplicates))


def _clean_provider(name: object, entry: object) -> tuple[dict[str, str] | None, str]:
    text = str(name or "").strip()
    if not _NAME_RE.fullmatch(text):
        return None, "名字不合法（只允许字母/数字/下划线/点/连字符）"
    if not isinstance(entry, dict):
        return None, "不是一个对象"
    base_url = str(entry.get("base_url", "") or "").strip()
    if not base_url:
        return None, "缺 base_url（地址与 key 必须成对落在这里）"
    cleaned = {"base_url": base_url[:_URL_MAX]}
    key = str(entry.get("key", "") or "").strip()
    if key:
        cleaned["key"] = key[:_KEY_MAX]
    client_type = str(entry.get("client_type", "") or "").strip()
    if client_type:
        cleaned["client_type"] = client_type
    return cleaned, ""


def _clean_agent(name: object, entry: object) -> tuple[dict[str, str] | None, str]:
    text = str(name or "").strip()
    if not _NAME_RE.fullmatch(text):
        return None, "名字不合法"
    if not isinstance(entry, dict):
        return None, "不是一个对象"
    cleaned: dict[str, str] = {}
    for key in ("provider", "model", "reasoning_effort"):
        value = entry.get(key, "")
        if isinstance(value, (str, int, float)) and str(value).strip():
            cleaned[key] = str(value).strip()
    return (cleaned or None), ("" if cleaned else "是空的")


# --- 写 -----------------------------------------------------------------------


def _atomic_write(target: Path, data: dict[str, object]) -> None:
    """一次 `os.replace` 落地（原子跃迁：读侧看不到中间态，契约 D11）。"""

    body = {
        "version": STORE_VERSION,
        "providers": dict(sorted((data.get("providers") or {}).items())),
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
            "先把它手工改回来或删掉，不要由我们来覆盖一份看不懂的文件")
    if not isinstance(data.get("providers", {}), dict):
        raise ModelProvidersRejected(f"{target.name} 的 providers 不是对象：先修好它再写")
    if not isinstance(data.get("agents", {}), dict):
        raise ModelProvidersRejected(f"{target.name} 的 agents 不是对象：先修好它再写")
    data["version"] = STORE_VERSION
    data.setdefault("providers", {})
    data.setdefault("agents", {})
    return data


def set_provider(name: str, *, base_url: str, key: str | None = None,
                 client_type: str | None = None, audit=None, source: str = "",
                 token: str = "", path: Path | str | None = None) -> dict[str, object]:
    """加/改**一套**（一个 `providers.<名字>` 条目）。地址与 key 一起落在这里。

    返回答给调用方的话（`name` 是**实际写进去**的名字：可能按地址收敛到已有的一个）。
    """

    target = Path(path) if path is not None else default_path()
    clean_name = _provider_name(name)
    clean_url = _url(base_url)
    with _WRITE_LOCK:
        data = _read_for_write(target)
        providers: dict[str, dict[str, str]] = data["providers"]  # type: ignore[assignment]
        existing = _find_by_url(providers, clean_url, exclude=clean_name)
        if existing is not None:
            existing_key = str(providers[existing].get("key", "") or "")
            new_key = str(key or "").strip()
            if new_key and existing_key and new_key != existing_key:
                raise ModelProvidersRejected(
                    f"地址 {clean_url} 已经属于 providers.{existing}，而那一条的 key 与这次给的"
                    "不同：一个地址只能有一条 key（仓库里没有'同地址两个账号'的表达方式）。"
                    "要么用同一个名字改 key，要么先把那一条删掉")
            entry = providers[existing]
            if new_key and not existing_key:
                entry["key"] = new_key
            _atomic_write(target, data)
            _audit(audit, "model_providers", {
                "action": "add", "provider": existing, "requested": clean_name,
                "base_url": entry.get("base_url", ""), "key_changed": bool(new_key),
            }, result=f"按地址收敛到 {existing}", source=source, token=token)
            return {"name": existing, "converged": True, "requested": clean_name,
                    "base_url": entry.get("base_url", ""),
                    "note": f"同一地址已经有 providers.{existing}，按地址收敛（用户 U3）"}
        entry = dict(providers.get(clean_name) or {})
        entry["base_url"] = clean_url
        text_key = str(key or "").strip()
        if text_key:
            entry["key"] = text_key
        if client_type:
            entry["client_type"] = _one_line(client_type)
        providers[clean_name] = entry
        _atomic_write(target, data)
        _audit(audit, "model_providers", {
            "action": "add", "provider": clean_name, "base_url": clean_url,
            "key_changed": bool(text_key),
        }, result="written", source=source, token=token)
        return {"name": clean_name, "converged": False, "requested": clean_name,
                "base_url": clean_url, "note": ""}


def upsert_agent(task: str, *, provider: str, model: str, base_url: str = "",
                 key: str | None = None, reasoning_effort: str | None = None,
                 audit=None, source: str = "", token: str = "",
                 path: Path | str | None = None) -> dict[str, object]:
    """把**一个用途**指到某一家上，并在**同一次 `os.replace`** 里补齐那一家的地址/key。

    契约 D11："一次 replace 写全 `providers.<名字>` + `agents.<用途>`"——否则读侧能看到
    "agent 已经指向一个还没写 key 的 provider"的中间态。
    """

    target = Path(path) if path is not None else default_path()
    clean_task = _ident(task, "用途")
    clean_name = _provider_name(provider)
    clean_model = _one_line(model)[:_MODEL_MAX]
    if not clean_model:
        raise ModelProvidersRejected("模型名不能为空：改供应商时必须一起说清用哪个模型")
    clean_url = _url(base_url)
    with _WRITE_LOCK:
        data = _read_for_write(target)
        providers: dict[str, dict[str, str]] = data["providers"]  # type: ignore[assignment]
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
                entry = {"base_url": clean_url, "client_type": "openai"}
                providers[clean_name] = entry
        if key is not None:
            text_key = str(key).strip()
            if text_key:
                entry["key"] = text_key
            else:
                # D12：空值 = "把这一项从文件里删掉"，不是"写一个空 key"。
                entry.pop("key", None)
        entry.setdefault("base_url", clean_url)
        previous = dict(agents.get(clean_task) or {})
        agent_entry: dict[str, str] = {"provider": clean_name, "model": clean_model}
        effort = (reasoning_effort if reasoning_effort is not None
                  else previous.get("reasoning_effort", ""))
        if effort:
            agent_entry["reasoning_effort"] = _one_line(effort)
        agents[clean_task] = agent_entry
        _atomic_write(target, data)
        _audit(audit, "model_providers", {
            "action": "agent", "task": clean_task, "provider": clean_name,
            "provider_from": provider_from, "model": clean_model,
            "base_url": entry.get("base_url", ""), "key_changed": key is not None,
        }, result=f"{clean_task}: {provider_from or '(今天那套)'} -> {clean_name}",
            source=source, token=token)
        return {"task": clean_task, "provider": clean_name, "provider_from": provider_from,
                "model": clean_model, "base_url": entry.get("base_url", ""),
                "converged": converged, "key_changed": key is not None}


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
    `force`——要删就先 `use <用途> <别的名字>` / 面板改 `*_provider`，然后再删。
    拒绝理由里点名**是哪几路**在用，别说"不允许删除"这种让人无从下手的话。
    """

    target = Path(path) if path is not None else default_path()
    clean_name = _provider_name(name)
    with _WRITE_LOCK:
        data = _read_for_write(target)
        providers: dict[str, dict[str, str]] = data["providers"]  # type: ignore[assignment]
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
                "先把它们切到别的家（`use <用途> <别的名字>`，或面板改对应的 *_provider），再删。")
        removed = providers.pop(clean_name)
        _atomic_write(target, data)
        _audit(audit, "model_providers", {
            "action": "remove", "provider": clean_name,
            "base_url": str(removed.get("base_url", "") or ""),
        }, result="removed", source=source, token=token)
        return {"name": clean_name, "removed": True}


def _find_by_url(providers: dict[str, dict[str, str]], base_url: str,
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

    return " ".join(str(value or "").split())


def _audit(audit, action: str, detail: dict[str, object], *, result: str,
           source: str, token: str) -> None:
    """写侧落审计（谁 / 什么时候 / 从哪套到哪套），复用 `control_audit`。

    审计失败**绝不**挡住那次写：面板/脚本已经改成功了，记不上账是次要问题
    （与 `control_audit.ControlAudit.record` 自己的纪律一致）。
    注意 detail 里**没有任何 key 值**：`key_changed` 只是布尔。
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
