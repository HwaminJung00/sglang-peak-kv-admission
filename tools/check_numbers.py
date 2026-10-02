#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Check the number markers in the Markdown docs against artifacts/*.json.

Every headline number in the docs is wrapped in an HTML comment marker that
GitHub renders invisibly:

    <!--n:KEY-->DISPLAY<!--/n-->

KEY must exist in one of the artifacts/*.json files, and DISPLAY must equal
that entry's "display" string exactly (character for character, so a
U+2212 minus sign is not the same as a hyphen).

Artifact files map key -> {"value", "display", "n", "agg", "source"}.
Top-level keys that start with "_" or "$" are treated as metadata and skipped.
A key defined twice (in one file or across files) is an error.

Scanned docs (files that do not exist yet are skipped):
    README*.md, ERRATA.md, docs/**/*.md, data/*.md, patch/*.md, tests/*.md
Markers inside fenced code blocks and inline code spans are ignored, because
GitHub shows them as literal text there.

Usage:
    python3 tools/check_numbers.py [--list-unused] [--root DIR]

Exit status: 0 = every marker matches; 1 = an unknown key, a mismatch, a
malformed marker or an invalid artifact file; 2 = usage error.
Standard library only.
"""

from __future__ import annotations

import argparse
import bisect
import json
import re
import signal
import sys
from pathlib import Path

DOC_GLOBS = (
    "README*.md",
    "ERRATA.md",
    "docs/**/*.md",
    "data/*.md",
    "patch/*.md",
    "tests/*.md",
)
FIELDS = ("value", "display", "n", "agg", "source")
AGG_VALUES = {"median", "single", "range", "count", "static", "derived"}

KEY_CHARS = r"[A-Za-z0-9_.:+\-]+"
# A well-formed marker. DISPLAY may not span lines or contain another comment.
MARKER_RE = re.compile(r"<!--n:(" + KEY_CHARS + r")-->((?:(?!<!--)[^\n])*?)<!--/n-->")
# Anything that looks like an attempt to open or close a marker.
OPEN_RE = re.compile(r"<!--\s*n\s*:")
CLOSE_RE = re.compile(r"<!--\s*/\s*n\s*-->")
FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
CODE_SPAN_RE = re.compile(r"(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)")


class Report:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def error(self, msg: str) -> None:
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)


def _reject_duplicates(pairs: list[tuple[str, object]]) -> dict:
    seen: dict[str, object] = {}
    dups = []
    for k, v in pairs:
        if k in seen:
            dups.append(k)
        seen[k] = v
    if dups:
        raise ValueError("duplicate key(s) in one JSON object: " + ", ".join(sorted(set(dups))))
    return seen


def load_artifacts(root: Path, rep: Report) -> tuple[dict[str, dict], dict[str, str]]:
    """Return (numbers, origin) where origin maps key -> artifact file."""
    numbers: dict[str, dict] = {}
    origin: dict[str, str] = {}
    art_dir = root / "artifacts"
    files = sorted(art_dir.glob("*.json")) if art_dir.is_dir() else []
    for path in files:
        rel = path.relative_to(root).as_posix()
        try:
            data = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicates)
        except (ValueError, UnicodeDecodeError) as exc:
            rep.error(f"{rel}: cannot parse: {exc}")
            continue
        if not isinstance(data, dict):
            rep.error(f"{rel}: top level must be a JSON object")
            continue
        for key, entry in data.items():
            if key.startswith(("_", "$")):
                continue
            where = f"{rel}: key {key!r}"
            if not re.fullmatch(KEY_CHARS, key):
                rep.error(f"{where}: key has characters outside {KEY_CHARS}")
                continue
            if key in numbers:
                rep.error(f"{where}: duplicate key (already defined in {origin[key]})")
                continue
            if not isinstance(entry, dict):
                rep.error(f"{where}: entry must be an object with fields {', '.join(FIELDS)}")
                continue
            missing = [f for f in FIELDS if f not in entry]
            if missing:
                rep.error(f"{where}: missing field(s) {', '.join(missing)}")
            if not isinstance(entry.get("display"), str):
                rep.error(f"{where}: 'display' must be a string")
                continue
            if "agg" in entry and entry["agg"] not in AGG_VALUES:
                rep.error(f"{where}: 'agg' is {entry['agg']!r}, expected one of {sorted(AGG_VALUES)}")
            n = entry.get("n")
            if "n" in entry and not (n is None or (isinstance(n, int) and not isinstance(n, bool) and n >= 1)):
                rep.error(f"{where}: 'n' must be a positive integer or null, got {n!r}")
            if "source" in entry and not isinstance(entry["source"], str):
                rep.error(f"{where}: 'source' must be a string")
            numbers[key] = entry
            origin[key] = rel
    return numbers, origin


def doc_files(root: Path) -> list[Path]:
    found: set[Path] = set()
    for pattern in DOC_GLOBS:
        for p in root.glob(pattern):
            if p.is_file():
                found.add(p)
    return sorted(found, key=lambda p: p.relative_to(root).as_posix())


def mask_code(text: str) -> str:
    """Blank out fenced code blocks and inline code spans, keeping offsets."""
    out_lines = []
    fence: str | None = None
    for line in text.split("\n"):
        m = FENCE_RE.match(line)
        if fence is None and m:
            fence = m.group(1)[0] * len(m.group(1))
            out_lines.append(" " * len(line))
            continue
        if fence is not None:
            stripped = line.strip()
            if stripped.startswith(fence) and set(stripped) <= {fence[0]}:
                fence = None
            out_lines.append(" " * len(line))
            continue
        out_lines.append(CODE_SPAN_RE.sub(lambda mm: " " * len(mm.group(0)), line))
    return "\n".join(out_lines)


def scan_doc(path: Path, rel: str, numbers: dict[str, dict], rep: Report, used: dict[str, int]) -> tuple[int, int]:
    """Return (markers, errors) for one doc."""
    try:
        raw = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        rep.error(f"{rel}: not valid UTF-8 ({exc})")
        return 0, 1
    text = mask_code(raw)
    newlines = [i for i, ch in enumerate(text) if ch == "\n"]

    def line_of(pos: int) -> int:
        return bisect.bisect_left(newlines, pos) + 1

    markers = 0
    problems: list[tuple[int, str]] = []
    consumed_open: set[int] = set()
    consumed_close: set[int] = set()
    for m in MARKER_RE.finditer(text):
        markers += 1
        consumed_open.add(m.start())
        consumed_close.add(m.end() - len("<!--/n-->"))
        # Masking keeps offsets, so read the text from the unmasked document.
        key, shown = m.group(1), raw[m.start(2):m.end(2)]
        where = f"{rel}:{line_of(m.start())}"
        entry = numbers.get(key)
        if entry is None:
            problems.append((m.start(), f"{where}: unknown key {key!r} (text {shown!r})"))
            continue
        used[key] = used.get(key, 0) + 1
        display = entry["display"]
        if shown != display:
            hint = ""
            if shown.replace("-", "\u2212") == display:
                hint = " (use U+2212 MINUS SIGN, not a hyphen)"
            elif shown.strip() == display:
                hint = " (extra whitespace inside the marker)"
            problems.append((m.start(), f"{where}: key {key!r}: doc text {shown!r} != display {display!r}{hint}"))
    for m in OPEN_RE.finditer(text):
        if m.start() not in consumed_open:
            problems.append((m.start(), f"{rel}:{line_of(m.start())}: malformed or unclosed number marker"))
    for m in CLOSE_RE.finditer(text):
        if m.start() not in consumed_close:
            problems.append((m.start(), f"{rel}:{line_of(m.start())}: closing marker without a matching <!--n:KEY-->"))
    for _pos, msg in sorted(problems):
        rep.error(msg)
    return markers, len(problems)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--list-unused", action="store_true",
                    help="also list artifact keys that no scanned doc references")
    ap.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent,
                    help="repository root (default: the parent of tools/)")
    args = ap.parse_args(argv)
    root = args.root.resolve()
    if not root.is_dir():
        print(f"check_numbers: root {root} is not a directory", file=sys.stderr)
        return 2

    rep = Report()
    numbers, origin = load_artifacts(root, rep)
    art_files = sorted(set(origin.values()))
    print(f"check_numbers: {len(numbers)} keys from "
          f"{', '.join(art_files) if art_files else 'no artifacts/*.json files'}")

    used: dict[str, int] = {}
    docs = doc_files(root)
    total_markers = 0
    width = max([len(d.relative_to(root).as_posix()) for d in docs] + [10])
    for d in docs:
        rel = d.relative_to(root).as_posix()
        markers, errors = scan_doc(d, rel, numbers, rep, used)
        total_markers += markers
        status = "ok" if errors == 0 else f"{errors} error(s)"
        print(f"  {rel:<{width}}  {markers:4d} markers  {status}")
    if not docs:
        print("  (no docs found yet)")

    for msg in rep.warnings:
        print(f"WARN  {msg}")
    for msg in rep.errors:
        print(f"ERROR {msg}")

    if args.list_unused:
        unused = sorted(k for k in numbers if k not in used)
        print(f"unused keys: {len(unused)}")
        for k in unused:
            print(f"  {k}  ({origin[k]})  display={numbers[k]['display']!r}")

    print(f"check_numbers: {total_markers} markers in {len(docs)} files, "
          f"{len(used)} distinct keys used, {len(rep.errors)} error(s)")
    return 1 if rep.errors else 0


if __name__ == "__main__":
    if hasattr(signal, "SIGPIPE"):  # exit quietly when piped into head
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    sys.exit(main())
