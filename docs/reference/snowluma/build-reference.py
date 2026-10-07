"""Generate the local action list and the per-category API reference tables.

Reads two JSON documents and writes Markdown:

* the extraction produced by ``extract-actions.py`` (this machine's install), and
* ``catalog.json`` from the upstream docs site (``https://snowluma.github.io/api/catalog.json``).

Usage::

    python build-reference.py --extract extract.json --catalog catalog.json \
        --out-dir . --generated-on 2026-10-08
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

# Local extractor helper -> the parameters that helper pre-seeds for every action.
PRESEEDED = {
    "defineAction": (),
    "defineStreamAction": (),
    "groupAction": ("group_id",),
    "groupUserAction": ("group_id", "user_id"),
}

# Chinese category label -> the English path segment the docs site uses.
# Taken from the site's own navigation links (see https://snowluma.github.io/llms.txt).
CATEGORY_SLUG = {
    "信息": "system",
    "消息": "message",
    "好友": "friend",
    "群信息": "group-info",
    "群管理": "group-admin",
    "群文件": "group-file",
    "请求": "request",
    "扩展": "extended",
    "群相册": "group-album",
    "空间": "qzone",
    "系统表情": "sys-face",
    "流式接口": "stream",
}


def cell(text: str) -> str:
    """Make a value safe for a Markdown table cell."""
    return text.replace("|", "\\|").replace("\n", " ").strip() or "-"


def brief(text: str, limit: int = 110) -> str:
    """Collapse a description to one line and truncate it."""
    first = " ".join(text.split())
    return first if len(first) <= limit else first[: limit - 1].rstrip() + "…"


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_text(path: Path, text: str) -> None:
    """Write UTF-8 without a BOM and with LF endings, like the rest of the repo."""
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def local_actions_table(extract: dict) -> str:
    lines = [
        "| 动作 | 分类 | 别名 | 参数 |",
        "| --- | --- | --- | --- |",
    ]
    for a in extract["actions"]:
        params = list(a["params"])
        for name in reversed(PRESEEDED.get(a["declared_with"], ())):
            if name not in params:
                params.insert(0, name)
        lines.append(
            "| `%s` | %s | %s | %s |"
            % (
                a["name"],
                a["category"] or "-",
                ", ".join(f"`{x}`" for x in a["aliases"]) or "-",
                ", ".join(f"`{x}`" for x in params) or "-",
            )
        )
    return "\n".join(lines)


def upstream_table(actions: list[dict]) -> str:
    lines = [
        "| 动作 | 摘要 | 参数（名称: 类型） | 只读 | 官方页面 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for a in sorted(actions, key=lambda x: x["name"]):
        params = []
        for p in a.get("params", []):
            schema = p.get("schema", {})
            kind = schema.get("type", "?")
            if isinstance(kind, list):
                kind = "|".join(kind)
            elif kind == "array":
                kind = "%s[]" % schema.get("items", {}).get("type", "?")
            if "default" in schema:
                kind += " = %r" % (schema["default"],)
            if not p.get("required", False):
                kind = "可选 " + kind
            params.append(f"`{p['name']}`: {kind}")
        lines.append(
            "| `%s` | %s | %s | %s | [文档](%s) |"
            % (
                a["name"],
                cell(brief(a.get("summary", ""))),
                cell("、".join(params)) if params else "-",
                "是" if a.get("readOnly") else "",
                "https://snowluma.github.io/zh/docs/api/%s/%s"
                % (CATEGORY_SLUG.get(a["category"], a["category"]), a["name"]),
            )
        )
    return "\n".join(lines)


def write_local_actions(extract: dict, out_dir: Path, generated_on: str) -> Path:
    counts: dict[str, int] = {}
    for a in extract["actions"]:
        key = a["category"] or "(未归入分类)"
        counts[key] = counts.get(key, 0) + 1
    count_rows = "\n".join(f"| {k} | {v} |" for k, v in counts.items())

    by_form: dict[str, int] = {}
    for a in extract["actions"]:
        by_form[a["declared_with"]] = by_form.get(a["declared_with"], 0) + 1
    form_rows = "\n".join(
        "  | `%s({{...}})` | %s | %d |" % (fn, note, by_form.get(fn, 0))
        for fn, note in (
            ("defineAction", "基础写法"),
            ("defineStreamAction", "基础 + `stream: true`"),
            ("groupAction", "预置必填 `group_id`"),
            ("groupUserAction", "预置必填 `group_id` + `user_id`"),
        )
    )

    aliased = [a for a in extract["actions"] if a["aliases"]]
    body = f"""# 本机 SnowLuma 动作清单

从本机安装的分包里抽取出来的动作清单。**只读**抽取，没有改动那份安装里的任何字节。

- 抽取到 **{len(extract['actions'])}** 个动作。
- 分包里有 {extract['declarations']} 条动作声明，其中
  {len(extract['skipped_non_literal_declarations'])} 条是包装函数的定义、没有字面量动作名，不计入。
- 来源文件：`{extract['bundle']}`
- 抽取日期：{generated_on}
- 抽取方式：本目录 `extract-actions.py`；复跑方法见 `README.md`。

## 动作总表

{local_actions_table(extract)}

## 按分类计数

| 分类 | 动作数 |
| --- | --- |
{count_rows}

## 抽取时的判定规则（抽的时候实际用到的，不是猜的）

- 安装目录里的 `index.mjs` 是打包产物，动作注册表被拆成分包放在 `config-<hash>.js` 里；
  本机当前是 `config-ClrCrmAk.js`。
- 每个动作都由四个声明辅助函数之一写出来，而它们最终都走 `defineAction`：

  | 声明写法 | 含义 | 本机条数 |
  | --- | --- | --- |
{form_rows}

- `name` 属性有两种写法：普通字符串，或数组（`[0]` 是动作名，其余是别名）。
  本机共 {len(aliased)} 个动作带别名，都在上面那张表里。
- 分类来自同一分包里的 `ACTION_GROUPS` 字面量：它把分类名映射到一个或多个
  `actions$N` 数组，每条声明算在包含它的**最内层**数组所属的分类里。
- `.get_word_slices`、`.ocr_image`、`.send_packet` 这些带点号的名字是真实动作或别名，
  不是内部函数；真正被排除的只有 `ACTION_REGISTRY` 里那两个快速操作处理器
  （`.handle_quick_operation` / `.handle_quick_operation_async`）——它们不在
  `ACTION_GROUPS` 里、也不是可调用的 OneBot 动作。

## 参数列说明

参数名是从同一份分包里每个动作 `params: {{...}}` 字面量的顶层键抽出来的，按原始书写顺序排列。
用 `groupAction` / `groupUserAction` 写出来的动作，其 `group_id` / `user_id` 由辅助函数预置、
不在动作自己的字面量里，上表已把它们补回。

**参数列没有类型、必填性和默认值**：那三样在分包里是链式调用构造出来的
（`f.string({{ allowEmpty: false }})` 这种），静态抽取不能保证忠实，所以这里不写、不猜。
要类型、默认值和应答形状，请看官方文档那一份（`api/` 目录下）。
"""
    path = out_dir / "local-actions.md"
    write_text(path, body)
    return path


def build(extract: dict, catalog: dict, out_dir: Path, generated_on: str) -> list[Path]:
    written = [write_local_actions(extract, out_dir, generated_on)]

    by_cat: dict[str, list[dict]] = {}
    for a in catalog["actions"]:
        by_cat.setdefault(a["category"], []).append(a)

    # Keep the site's own category order; append anything it does not list.
    order: list[str] = []
    for c in catalog["categories"]:
        if c["category"] not in order:
            order.append(c["category"])
    for a in catalog["actions"]:
        if a["category"] not in order:
            order.append(a["category"])

    local_by_cat: dict[str, list[dict]] = {}
    for a in extract["actions"]:
        local_by_cat.setdefault(a["category"], []).append(a)

    api_dir = out_dir / "api"
    api_dir.mkdir(exist_ok=True)

    index_rows = []
    for c in order:
        items = by_cat.get(c, [])
        body = [
            f"# {c}（官方文档存档 · {len(items)} 个动作）",
            "",
            f"来源：<https://snowluma.github.io/zh/docs/api>（该分类 {len(items)} 个动作）。"
            f"抓取日期：{generated_on}。",
            "",
            "表格由上游 `catalog.json` 生成，摘要与参数取自该文件；每个动作的完整页面"
            "（示例、试调面板、应答形状）在上游站点上，链接在最后一列。",
            "",
            upstream_table(items) if items else "_上游该分类下没有动作。_",
            "",
        ]
        path = api_dir / f"{c}.md"
        write_text(path, "\n".join(body))
        written.append(path)
        index_rows.append((c, len(items), len(local_by_cat.get(c, []))))

    rows = "\n".join(
        "| %s | %d | %d | [api/%s.md](api/%s.md) |" % (c, u, l, c, c)
        for c, u, l in index_rows
    )
    index = f"""# 官方文档分类索引

上游文档站：<https://snowluma.github.io/zh/docs/api>（简体中文）。
抓取日期：{generated_on}。本页只做索引，正文在各分类文件里。

| 分类 | 上游动作数 | 本机动作数 | 文件 |
| --- | --- | --- | --- |
{rows}
| **合计** | **{sum(u for _, u, _ in index_rows)}** | **{sum(l for _, _, l in index_rows)}** | |

「本机动作数」来自 `local-actions.md` 那次抽取；两列对不上的地方见 `README.md`。
"""
    path = out_dir / "api-index.md"
    write_text(path, index)
    written.append(path)
    return written


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--extract", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--generated-on", default="")
    args = parser.parse_args()

    written = build(
        load(args.extract), load(args.catalog), args.out_dir, args.generated_on
    )
    for p in written:
        print(p)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
