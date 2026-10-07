"""Read-only extractor for the local SnowLuma action registry.

The SnowLuma install that ships inside NapCatQQ Desktop keeps its authored action
list in one JS bundle: ``components/SnowLuma/config-<hash>.js`` (pulled in by the
first import line of ``index.mjs``). That file is a build artifact, not source, so
the action declarations appear verbatim. Every action is declared by one of four
helpers, and all four funnel into ``defineAction``:

===========================  =========================================
``defineAction({...})``      base form
``defineStreamAction({...})``  base + ``stream: true``
``groupAction({...})``       pre-seeds a required ``group_id``
``groupUserAction({...})``   pre-seeds a required ``group_id`` + ``user_id``
===========================  =========================================

The ``name`` property is either a plain string (the action name) or an array whose
head is the action name and whose tail is that action's aliases.

Categories come from the ``ACTION_GROUPS`` literal in the same bundle: it maps a
category label to one or more ``actions$N`` arrays, and each declaration belongs to
the innermost such array that contains it.

Nothing here writes to the SnowLuma install. Usage::

    python extract-actions.py                 # auto-discover the newest bundle
    python extract-actions.py <bundle.js>     # explicit bundle
    python extract-actions.py --json out.json # also dump the raw extraction

Run with the project interpreter:
``..\\..\\..\\.venv\\Scripts\\python.exe extract-actions.py``
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

DEFAULT_COMPONENT_DIR = Path(r"C:\ProgramData\NapCatQQ Desktop\components\SnowLuma")

DECLARERS = ("defineAction", "defineStreamAction", "groupAction", "groupUserAction")

# The quick-operation handler is declared in ACTION_REGISTRY, not in ACTION_GROUPS,
# and is not a callable OneBot action. The bundle keeps it out of the action list,
# so this extractor reports it separately. Note that a leading dot is NOT itself a
# marker for "hidden": `.get_word_slices`, `.ocr_image` and `.send_packet` are all
# real, documented actions or aliases.
HIDDEN_ACTION_NAMES = frozenset(
    {".handle_quick_operation", ".handle_quick_operation_async"}
)


def skip_string(text: str, i: int) -> int:
    """Return the index just past the string literal starting at ``text[i]``."""
    quote = text[i]
    i += 1
    while i < len(text):
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if c == quote:
            return i + 1
        i += 1
    return i


def skip_line_comment(text: str, i: int) -> int:
    nl = text.find("\n", i)
    return len(text) if nl == -1 else nl + 1


def skip_block_comment(text: str, i: int) -> int:
    end = text.find("*/", i + 2)
    return len(text) if end == -1 else end + 2


def match_delimiter(text: str, start: int, opener: str, closer: str) -> int:
    """``text[start]`` is ``opener``; return the index just past its match."""
    depth = 0
    i = start
    while i < len(text):
        c = text[i]
        if c in "\"'`":
            i = skip_string(text, i)
            continue
        if c == "/" and text.startswith("//", i):
            i = skip_line_comment(text, i)
            continue
        if c == "/" and text.startswith("/*", i):
            i = skip_block_comment(text, i)
            continue
        if c == opener:
            depth += 1
        elif c == closer:
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return -1


def match_balanced(text: str, start: int) -> int:
    """Match a ``{``/``[``/``(`` at ``start`` against its counterpart."""
    pairs = {"(": ")", "[": "]", "{": "}"}
    opener = text[start]
    closer = pairs[opener]
    depth = 0
    i = start
    while i < len(text):
        c = text[i]
        if c in "\"'`":
            i = skip_string(text, i)
            continue
        if c == "/" and text.startswith("//", i):
            i = skip_line_comment(text, i)
            continue
        if c == "/" and text.startswith("/*", i):
            i = skip_block_comment(text, i)
            continue
        if c in pairs:
            depth += 1
        elif c in pairs.values():
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return -1


def read_name(args: str) -> list[str]:
    """Read the ``name:`` property of an action literal (string or array)."""
    m = re.search(r"(?:^|[,{\s])name:\s*\[([^\]]*)\]", args)
    if m:
        return re.findall(r'"([^"]*)"', m.group(1))
    m = re.search(r'(?:^|[,{\s])name:\s*"([^"]*)"', args)
    return [m.group(1)] if m else []


def read_params(args: str) -> list[str]:
    """Top-level keys of the action's ``params: {...}`` literal, in authored order."""
    m = re.search(r"(?:^|[,{\s])params:\s*\{", args)
    if not m:
        return []
    open_brace = args.index("{", m.start() + m.group(0).find("{"))
    end = match_delimiter(args, open_brace, "{", "}")
    body = args[open_brace + 1 : end - 1]

    # Split the literal on commas that sit at nesting depth zero, then take the key
    # of each entry (everything before its first top-level colon).
    entries: list[str] = []
    current = ""
    depth = 0
    i = 0
    while i < len(body):
        c = body[i]
        if c in "\"'`":
            j = skip_string(body, i)
            current += body[i:j]
            i = j
            continue
        if c == "/" and body.startswith("//", i):
            i = skip_line_comment(body, i)
            continue
        if c == "/" and body.startswith("/*", i):
            i = skip_block_comment(body, i)
            continue
        if c in "{[(":
            depth += 1
        elif c in "}])":
            depth -= 1
        elif c == "," and depth == 0:
            entries.append(current)
            current = ""
            i += 1
            continue
        current += c
        i += 1
    entries.append(current)

    keys: list[str] = []
    for entry in entries:
        key = entry.split(":", 1)[0].strip().strip('"').strip("'")
        if re.fullmatch(r"[\w$]+", key):
            keys.append(key)
    return keys


def read_summary(args: str) -> str:
    m = re.search(r'summary:\s*"((?:[^"\\]|\\.)*)"', args)
    return m.group(1) if m else ""


def helper_line_numbers(src: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for fn in DECLARERS:
        m = re.search(rf"function\s+{fn}\s*\(", src)
        if m:
            out[fn] = src.count("\n", 0, m.start()) + 1
    return out


def action_arrays(src: str) -> dict[str, tuple[int, int]]:
    arrays: dict[str, tuple[int, int]] = {}
    for m in re.finditer(r"var\s+(actions(?:\$\d+)?)\s*=\s*\[", src):
        open_bracket = src.index("[", m.start() + len(m.group(0)) - 1)
        arrays[m.group(1)] = (open_bracket, match_balanced(src, open_bracket))
    return arrays


def category_map(src: str) -> list[tuple[str, list[str]]]:
    start = src.index("var ACTION_GROUPS")
    open_bracket = src.index("[", start)
    end = match_balanced(src, open_bracket)
    block = src[start:end]
    out: list[tuple[str, list[str]]] = []
    # The last category is written as a bare `actions` shorthand:
    #     { category: "系统表情", actions }
    # so the value may be the identifier itself rather than a `actions: <expr>`
    # pair. Try the longer `actions$N` name first so it is not matched as `actions`.
    pattern = re.compile(
        r'category:\s*"([^"]+)",\s*actions\b\s*(?::\s*'
        r'(actions\$\d+|\[[^\]]*\])'
        r"|)"
    )
    for m in pattern.finditer(block):
        # An empty second group is the `actions` shorthand.
        expr = m.group(2) or "actions"
        arrays = re.findall(r"(actions(?:\$\d+)?)", expr)
        out.append((m.group(1), arrays))
    return out


def find_declarations(src: str) -> list[dict]:
    decls: list[dict] = []
    for fn in DECLARERS:
        idx = 0
        needle = fn + "("
        while (idx := src.find(needle, idx)) != -1:
            if re.search(r"function\s*$", src[max(0, idx - 40) : idx]):
                idx += len(needle)
                continue
            open_paren = idx + len(fn)
            end = match_delimiter(src, open_paren, "(", ")")
            if end == -1:
                break
            args = src[open_paren + 1 : end - 1]
            names = read_name(args)
            decls.append(
                {
                    "declared_with": fn,
                    "offset": idx,
                    "name": names[0] if names else None,
                    "aliases": names[1:],
                    "summary": read_summary(args),
                    "params": read_params(args),
                }
            )
            idx = open_paren + 1
    return decls


def extract(bundle: Path) -> dict:
    src = bundle.read_text(encoding="utf-8")
    arrays = action_arrays(src)
    cats = category_map(src)
    decls = find_declarations(src)

    actions: list[dict] = []
    skipped: list[dict] = []
    hidden: list[str] = []
    for d in decls:
        if d["name"] is None:
            skipped.append({"declared_with": d["declared_with"], "offset": d["offset"]})
            continue
        owners = [
            (name, span)
            for name, span in arrays.items()
            if span[0] < d["offset"] < span[1]
        ]
        owners.sort(key=lambda kv: kv[1][0], reverse=True)
        array_name = owners[0][0] if owners else None
        category = next(
            (label for label, names in cats if array_name in names),
            None,
        )
        if d["name"] in HIDDEN_ACTION_NAMES:
            hidden.append(d["name"])
            continue
        actions.append(
            {
                "name": d["name"],
                "aliases": d["aliases"],
                "category": category,
                "array": array_name,
                "declared_with": d["declared_with"],
                "params": d["params"],
                "summary": d["summary"],
            }
        )

    actions.sort(key=lambda a: (a["category"] or "", a["name"]))
    names = [a["name"] for a in actions]
    return {
        "bundle": str(bundle),
        "helper_function_lines": helper_line_numbers(src),
        "categories": [{"category": c, "arrays": a} for c, a in cats],
        "declarations": len(decls),
        "actions": actions,
        "duplicate_names": sorted({n for n in names if names.count(n) > 1}),
        "skipped_non_literal_declarations": skipped,
        "hidden_quick_operation_handlers": hidden,
    }


def newest_bundle(component_dir: Path) -> Path:
    candidates = sorted(
        component_dir.glob("config-*.js"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    if not candidates:
        raise SystemExit(f"no config-*.js under {component_dir}")
    return candidates[0]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", nargs="?", type=Path, help="config-<hash>.js to read")
    parser.add_argument("--component-dir", type=Path, default=DEFAULT_COMPONENT_DIR)
    parser.add_argument("--json", type=Path, help="also write the raw extraction here")
    parser.add_argument("--tsv", action="store_true", help="print name/category/alias TSV")
    args = parser.parse_args()

    bundle = args.bundle or newest_bundle(args.component_dir)
    result = extract(bundle)

    counts: dict[str, int] = {}
    for a in result["actions"]:
        key = a["category"] or "(unattributed)"
        counts[key] = counts.get(key, 0) + 1

    if args.json:
        args.json.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    if args.tsv:
        for a in result["actions"]:
            print(
                "\t".join(
                    [
                        a["name"],
                        a["category"] or "(unattributed)",
                        ",".join(a["aliases"]),
                        ",".join(a["params"]),
                    ]
                )
            )
        return 0

    print(f"bundle: {bundle}")
    print(f"declarations: {result['declarations']}")
    print(f"actions (name-bearing, non-hidden): {len(result['actions'])}")
    print(f"duplicate names: {result['duplicate_names'] or 'none'}")
    print(f"non-literal declarations skipped: {len(result['skipped_non_literal_declarations'])}")
    print("per category:")
    for label, n in counts.items():
        print(f"  {label}: {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
