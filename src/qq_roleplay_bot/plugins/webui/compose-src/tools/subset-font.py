r"""Build the CJK webfont the wasm bundle ships (offline, reproducible).

Why this script exists
----------------------
Miuix's own web example preloads MiSans from a Xiaomi CDN
(`example/web/src/webMain/kotlin/Main.web.kt:21-22`).  This deployment must work
offline, so the font has to live inside the bundle instead of being fetched from
cdn-font.hyperos.mi.com.  The font we ship is **Noto Sans SC** — the same family
Windows 11 already installs as `C:\Windows\Fonts\NotoSansSC-VF.ttf`
(SIL Open Font License 1.1, redistribution allowed); no network fetch is involved
in producing or consuming it.

The raw variable font is 17.8 MB, which would roughly double the wasm payload, so
this script instances it at wght=400 and subsets it to the characters the panel can
actually show:

  * ASCII + Latin-1 punctuation,
  * GB2312 (level 1 + level 2, 6763 hanzi) — so群名/昵称/模型名 typed by a human
    still find a glyph instead of waiting ~1.5 s for the async system fallback,
  * every CJK character the old panel's JS/HTML/CSS and this module's Kotlin
    sources contain, so no label in the UI can be missing,
  * CJK punctuation / fullwidth forms / general punctuation.

Anything outside that set still falls back to the platform stack the way it always
did (tofu for ~1.2–1.9 s, measured in `NOTES.md`); the point of the subset is that
our own chrome never waits for that.

Run:  python tools/subset-font.py <repo>/src/wasmJsMain/resources/fonts/NotoSansSC-Regular.ttf
      (run from the project dir: src/qq_roleplay_bot/plugins/webui/compose-src)
"""

from __future__ import annotations

import pathlib
import sys

from fontTools import subset, ttLib
from fontTools.varLib.instancer import instantiateVariableFont

SOURCE = pathlib.Path(r"C:\Windows\Fonts\NotoSansSC-VF.ttf")
ROOT = pathlib.Path(__file__).resolve().parent.parent
HERE = pathlib.Path(__file__).resolve().parent
CORPUS = (
    ROOT / "src" / "wasmJsMain" / "kotlin",
    # the old (CSS/JS) panel — its strings are the labels this panel keeps.
    # 2026-10-20: this project moved into the panel plugin package, so the old panel is now a
    # sibling (`plugins/webui/webui/`), not a repo-root-relative path.
    ROOT.parent / "webui",
)


def gb2312_chars() -> set[str]:
    out: set[str] = set()
    for high in range(0xB0, 0xF8):
        for low in range(0xA1, 0xFF):
            try:
                out.add(bytes((high, low)).decode("gb2312"))
            except UnicodeDecodeError:
                continue
    return out


def corpus_chars() -> set[str]:
    out: set[str] = set()
    for base in CORPUS:
        for path in base.rglob("*"):
            if path.suffix.lower() not in {".kt", ".js", ".css", ".html", ".md"}:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            out.update(ch for ch in text if ord(ch) > 0x7F)
    return out


def ranges(start: int, stop: int) -> set[str]:
    return {chr(code) for code in range(start, stop + 1)}


def main() -> int:
    target = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else (
        ROOT / "src" / "wasmJsMain" / "resources" / "fonts" / "NotoSansSC-Regular-subset.ttf"
    )
    target.parent.mkdir(parents=True, exist_ok=True)

    chars = (
        ranges(0x20, 0x7E)          # ASCII
        | ranges(0xA0, 0xFF)        # Latin-1 (·, °, ×, ÷ …)
        | ranges(0x2000, 0x206F)    # general punctuation (–, —, …, ‘ ’ “ ”)
        | ranges(0x3000, 0x303F)    # CJK punctuation
        | ranges(0xFF00, 0xFF60)    # fullwidth forms
        | gb2312_chars()
        | corpus_chars()
    )

    font = ttLib.TTFont(str(SOURCE), lazy=True)
    font = instantiateVariableFont(font, {"wght": 400}, inplace=False, updateFontNames=True)
    options = subset.Options()
    options.name_IDs = ["*"]
    options.name_legacy = True
    options.layout_features = ["kern"]
    options.drop_tables += ["DSIG"]
    options.recalc_bounds = True
    options.notdef_outline = True
    subsetter = subset.Subsetter(options=options)
    subsetter.populate(unicodes=[ord(ch) for ch in chars])
    subsetter.subset(font)
    font.flavor = None
    font.save(str(target))

    size = target.stat().st_size
    missing = sorted(
        ch for ch in corpus_chars() if ord(ch) not in font.getBestCmap()
    )
    print(f"source  : {SOURCE} ({SOURCE.stat().st_size} bytes)")
    print(f"requested: {len(chars)} code points")
    print(f"written : {target} ({size} bytes = {size / 1024:.1f} KiB)")
    # Print code points, not the characters: the Windows console codepage (cp936) cannot
    # encode some of them (U+FE0F …), and a crash here would hide the numbers above.
    print(f"corpus chars missing from the subset: {len(missing)}"
          + (f" -> {' '.join(f'U+{ord(c):04X}' for c in missing[:20])}" if missing else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
