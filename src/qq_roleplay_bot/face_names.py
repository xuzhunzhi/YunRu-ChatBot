"""QQ 系统表情的**名字表**：`emoji_id` → 名字（`赞` / `太好笑` / `问号` / `㊗` …）。

由来（2026-10-11 用户原话）：*"你好歹去查询表情的名字给丢到提示词里吧"*。

## 为什么需要它

`group_msg_emoji_like` 推来的 `likes[].emoji_id` 是**一串数字**（`76`、`12951`），
金句学习 agent 在样本里看到的就只有这串数字——它**只能猜**。第一批学出来的
14 条表情含义里 13 条是 `other` / `unclear`，其中一条的原话就是"看不出是捧还是
随手点的"。这不是模型笨，是**材料里没写名字**。

## 名字从哪来：仓库里早就有这份契约，照它读

接口**不是我挑的**，是仓库里已经登记好的（`capabilities.py` 的只读白名单第 61 行）
那份动作表（`data/snowluma_actions.json`）里写着的：

| 动作 | 契约原文（照抄 `snowluma_actions.json`） |
| --- | --- |
| `fetch_sys_faces`（:2399） | "获取 QQ 系统表情目录"；返回 `{"packs":[{"pack_name","emojis":[…]}]}` |
| `fetch_face_entity`（:2141） | "按编号查询 QQ 系统表情；编号不存在时返回 null" |

`emojis[]` 里那三个键的说明也是契约原文：`q_sid` = "QQ 表情目录标识（**部分 emoji
为 Unicode 表情字符串**）"、`q_des` = "表情描述"、`q_cid` = 没写说明（**实测**只有
Unicode 表情那一包有值，且等于它的码点）。

**为什么用目录而不是 `fetch_face_entity` 逐个查**（2026-10-11 本机实测，3000 端口）：
`fetch_face_entity(face_id=…)` 对 `76` / `387` / `277` / `424` / `476` 都命中，但对
`12951` / `10068` / `234` **一律报错**（`retcode=100`，原文
`OIDB error 11000 on 0x9155_1: get emoji err:… sysemojis cfg not found`，客户端那一层
是**抛异常**而不是返回 null）。而目录表一次就把 `10068`（`q_cid`）这类也带回来了，
所以走目录：**一次请求、三种 id 全在本地索引里**。

## 三种 id 混在一起（2026-10-11 实测）

量法：把 `run/data/logs/chat.jsonl` 里全部 `kind="reaction"` 的行数一遍
（38 个不同 id），拿目录表（404 条）逐个对：

| 日志里的 id | 命中哪一栏 | 名字 | 数 |
| --- | --- | --- | --- |
| `76` / `118` / `387` / `476` … | `q_sid`（QQ 表情序号） | `赞` / `抱拳` / `太好笑` / `不是吧` | 30 个 |
| `10068` / `128516` / `128077` … | `q_cid`（**码点**） | `问号` / `高兴` / `厉害` | 5 个 |
| `12951` / `10060` | 目录里没有 | `㊗` / `❌`（见下） | 2 个 |
| `234` | 都没有 | **查不到** | 1 个 |

那 5 个 `q_cid` 命中是"**表情回应里的 id 就是 Unicode 码点**"这条规律的实测证据
（`chr(128516) == "😄"` 且表里 `q_cid=128516` → `/高兴`）。`12951` = `U+3297` = `㊗`、
`10060` = `U+274C` = `❌` 是同一形状（QQ 目录里没有这两个，但它们**是**码点，这是
算出来的、不是猜的）。`234` 就**如实查不到**——`chr(234) == "ê"`（字母，不是表情），
所以它既不是 `q_sid` 也不是码点，**不许**编一个名字给它。

查名字按 **`q_sid` → `q_cid` → `em_code` → 码点** 四档来，都没有就返回空串。

## 它不碰对话，也不花钱

- **回复路径上只读本地文件**：`data/faces.json`（按 mtime 缓存，与 `QuoteStore` 同一套），
  **一个网络请求都不发**；
- **刷新只在金句学习那条定期节拍里**发生（`QuoteLearningAgent._refresh_face_names`，
  一天一次级别），失败**保留旧表**、只记一笔日志；
- 没有这份表时，样本里就**不多那个字段**——行为与改动前逐字相同。

## 落盘

`data/faces.json`（`data/` 已 gitignored）：QQ 自己的表情元数据，操作者看得见、能删
（删了就回落到"没有名字"）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
import unicodedata
from pathlib import Path
from typing import TYPE_CHECKING

from . import dev_config

if TYPE_CHECKING:  # 只要那个对象有 `async call(action, params)` 就行（`SnowLumaHttpClient`）
    from .capabilities import CapabilityRegistry
    from .onebot_client import SnowLumaHttpClient

logger = logging.getLogger(__name__)

#: 拿目录的那一个动作名。**从动作表里读的**（`data/snowluma_actions.json:2399`，
#: 也在 `capabilities.py` 的只读白名单里），不是我试出来的名字。
FETCH_ACTION = "fetch_sys_faces"

CACHE_VERSION = 1
#: 多久算旧（秒）。金句学习一天跑一次级别，所以它实际决定"每天刷一次目录"。
DEFAULT_TTL_SECONDS = 86400.0
#: 刷新那一次的网络超时（秒）。失败不影响任何东西，只保留旧表。
DEFAULT_REFRESH_TIMEOUT = 20.0
#: 小于这个码点的不当表情看（挡掉 `234` 那种被 `chr()` 成 `ê` 的东西）。
UNICODE_MIN_CODEPOINT = 0x2000
#: 名字最多留几个字（QQ 的名字都是短的；长的截断，免得样本被一个名字撑大）。
NAME_MAX_CHARS = 24


def cache_path() -> Path:
    """那份 JSON 的位置。默认 `<项目根>/data/faces.json`（`data/` 已忽略）。"""

    value = os.environ.get("QQBOT_FACE_NAMES", "").strip() or dev_config.FACE_NAMES_FILE
    path = Path(value)
    if not path.is_absolute():
        path = Path(__file__).resolve().parents[2] / path
    return path


def clean_name(value: object) -> str:
    """QQ 那边的名字带一个前导斜杠（`/赞`），去掉它；空白归一、限长。"""

    text = " ".join(str(value if value is not None else "").split())
    return text.lstrip("/").strip()[:NAME_MAX_CHARS]


def unicode_name(emoji_id: object) -> str:
    """这一串数字会不会是**一个 Unicode 码点的十进制**（实测 QQ 就是那么报的）。

    判据两条，都硬：码点不小于 `UNICODE_MIN_CODEPOINT`（挡掉 ASCII 与拉丁补字），
    而且 `unicodedata` 说它是 `So`（符号/其它）或 `Sk`（修饰符号）。
    所以 `12951 → ㊗`、`10060 → ❌` 收，`234 → ê`（`Ll`，字母）**不收**。
    """

    text = str(emoji_id if emoji_id is not None else "").strip()
    if not text.isdigit():
        return ""
    code = int(text)
    if not UNICODE_MIN_CODEPOINT <= code <= 0x10FFFF:
        return ""
    char = chr(code)
    if unicodedata.category(char) not in {"So", "Sk"}:
        return ""
    return char


def flatten(payload: object) -> tuple[dict[str, str], ...]:
    """把目录按**契约的形状**摊平成一串 `{"q_sid","q_cid","em_code","name","pack"}`。

    形状就是 schema 里那句 `{"packs":[{"pack_name","emojis":[…]}]}`；
    **不认识的形状返回空**（宁可"没有名字"，也不要照一个我自己想的形状瞎读）。
    """

    packs = payload.get("packs") if isinstance(payload, dict) else None
    if not isinstance(packs, list):
        return ()
    out: list[dict[str, str]] = []
    for pack in packs:
        if not isinstance(pack, dict):
            continue
        pack_name = str(pack.get("pack_name") or "").strip()
        for item in pack.get("emojis") or []:
            if not isinstance(item, dict):
                continue
            name = clean_name(item.get("q_des"))
            if not name:
                continue
            out.append({
                "q_sid": str(item.get("q_sid") or "").strip(),
                "q_cid": str(item.get("q_cid") if item.get("q_cid") is not None else "").strip(),
                "em_code": str(item.get("em_code") or "").strip(),
                "name": name,
                "pack": pack_name,
            })
    return tuple(out)


class FaceNames:
    """那份 JSON 的读写与查询：三栏索引 + 按 mtime 缓存 + 原子替换。

    文件坏了 / 版本不认识 → 整份当空。**一份写坏的文件不该影响除"少一个名字"以外的
    任何东西**，更不该影响对话。
    """

    VERSION = CACHE_VERSION

    def __init__(self, path: Path | str | None = None, *, clock=time.time) -> None:
        self.path = Path(path) if path is not None else cache_path()
        self.clock = clock
        #: 三栏索引：`q_sid` / `q_cid`（码点）/ `em_code`。键统一成字符串。
        self.by_sid: dict[str, str] = {}
        self.by_cid: dict[str, str] = {}
        self.by_code: dict[str, str] = {}
        self.count = 0
        self.packs: dict[str, int] = {}
        self.updated_at = 0.0
        self.last_error = ""
        self._mtime = 0.0
        self.reload()

    # --- 读 ---------------------------------------------------------------

    def reload(self, *, force: bool = False) -> None:
        """按 mtime 决定要不要重新读盘（读侧每次都会走到这里）。"""

        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            mtime = 0.0
        if not force and mtime == self._mtime:
            return
        self._mtime = mtime
        self.by_sid, self.by_cid, self.by_code = {}, {}, {}
        self.count, self.packs = 0, {}
        self.updated_at = 0.0
        self.last_error = ""
        if not mtime:
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            self.last_error = type(exc).__name__
            logger.warning("face_names_unreadable category=%s", type(exc).__name__)
            return
        if not isinstance(data, dict) or data.get("version") != self.VERSION:
            self.last_error = "unsupported_version"
            logger.warning("face_names_unsupported_version；整份忽略")
            return
        self.updated_at = float(data.get("updated_at") or 0.0)
        for item in data.get("faces") or []:
            if isinstance(item, dict):
                self._index(item)

    def _index(self, item: dict) -> None:
        name = clean_name(item.get("name"))
        if not name:
            return
        for key, table in (("q_sid", self.by_sid), ("q_cid", self.by_cid),
                           ("em_code", self.by_code)):
            value = str(item.get(key) or "").strip()
            if value:
                table.setdefault(value, name)
        pack = str(item.get("pack") or "").strip()
        if pack:
            self.packs[pack] = self.packs.get(pack, 0) + 1
        self.count += 1

    # --- 问 ---------------------------------------------------------------

    def name_how(self, emoji_id: object) -> str:
        """名字是从哪一档查出来的：`""` / `"q_sid"` / `"q_cid"` / `"em_code"` / `"码点"`。

        **给操作者与日志看**（诊断"为什么这个名字不对"），不是判据。
        """

        key = str(emoji_id if emoji_id is not None else "").strip()
        if not key:
            return ""
        self.reload()
        if key in self.by_sid:
            return "q_sid"
        if key in self.by_cid:
            return "q_cid"
        if key in self.by_code:
            return "em_code"
        if unicode_name(key):
            return "码点"
        return ""

    def label_for(self, emoji_id: object) -> str:
        """**读侧唯一入口**：这个名字叫什么；查不到就是空串（绝不出编的名字）。"""

        key = str(emoji_id if emoji_id is not None else "").strip()
        if not key:
            return ""
        self.reload()
        for table in (self.by_sid, self.by_cid, self.by_code):
            if key in table:
                return table[key]
        return unicode_name(key)

    def stale(self, ttl: float = DEFAULT_TTL_SECONDS) -> bool:
        """该不该刷新：一条都没索引上就是"旧"，或者超过 TTL。"""

        if not self.count:
            return True
        return (self.clock() - self.updated_at) > max(0.0, float(ttl))

    def snapshot(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "count": self.count,
            "packs": sorted(self.packs),
            "updated_at": self.updated_at,
            "last_error": self.last_error,
        }

    # --- 写 ---------------------------------------------------------------

    def save(self, items: tuple[dict[str, str], ...] | list[dict[str, str]]) -> bool:
        """原子整份替换（与 `QuoteStore.save` 同一套：临时文件 + `os.replace`）。"""

        body = {
            "version": self.VERSION,
            "updated_at": round(self.clock(), 3),
            "count": len(items),
            "faces": [dict(item) for item in items],
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle = tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=str(self.path.parent),
                prefix=self.path.name + ".", suffix=".tmp", delete=False,
            )
            try:
                with handle:
                    json.dump(body, handle, ensure_ascii=False, indent=1)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(handle.name, self.path)
            except BaseException:
                Path(handle.name).unlink(missing_ok=True)
                raise
        except OSError as exc:
            self.last_error = type(exc).__name__
            logger.warning("face_names_write_failed category=%s", type(exc).__name__)
            return False
        self.reload(force=True)
        return True


_SHARED: FaceNames | None = None


def shared() -> FaceNames:
    """进程里共用那一份（读侧用它：只读本地文件，不联网）。"""

    global _SHARED
    if _SHARED is None:
        _SHARED = FaceNames()
    return _SHARED


def install(table: FaceNames | None) -> None:
    """换掉共用那一份（测试用；传 `None` 就是"下次重新建"）。"""

    global _SHARED
    _SHARED = table


async def refresh(client: "SnowLumaHttpClient | None", table: FaceNames | None = None, *,
                  registry: "CapabilityRegistry | None" = None,
                  ttl: float = DEFAULT_TTL_SECONDS,
                  timeout: float = DEFAULT_REFRESH_TIMEOUT) -> int:
    """把目录拉下来落盘。返回写进去几条；**任何失败都只记一笔、返回 0**。

    只在金句学习那条定期节拍里调（`QuoteLearningAgent`）。没到 TTL 就不拉
    （`table.stale()` 为假时直接返回 0），所以它不是"每次学习都打一次接口"。

    `registry` 给了就先过能力闸门的只读白名单（与 `conversation_context` 同一套：
    动作名在 `READ_ACTIONS` 里，见 `capabilities.py`）；没给就自己建一个默认的。
    """

    target = table or shared()
    if client is None or not target.stale(ttl):
        return 0
    gate = registry
    if gate is None:
        from .capabilities import CapabilityRegistry

        gate = CapabilityRegistry()
    try:
        gate.check(FETCH_ACTION, purpose="read")
    except Exception as exc:  # noqa: BLE001 - 闸门不放行就是不拉，不影响别的任何事
        logger.warning("face_names_refresh_denied category=%s", type(exc).__name__)
        return 0
    try:
        # 不传 `refresh`：目录由 SnowLuma 自己缓存，我们这一层已经有 TTL，不必每次逼它打 QQ。
        payload = await asyncio.wait_for(client.call(FETCH_ACTION, {}), timeout)
        items = flatten(payload)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - 拉不到名字不该影响任何别的事
        logger.warning("face_names_refresh_failed category=%s", type(exc).__name__)
        return 0
    if not items:
        logger.warning("face_names_refresh_empty")
        return 0
    if not target.save(items):
        return 0
    logger.info("face_names_refreshed count=%s", len(items))
    return len(items)
