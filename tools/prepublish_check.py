#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Pre-publication checks CP1-CP7 for this repository (standard library only).

Run it through the wrapper:
    bash tools/prepublish_check.sh [--verbose]
    bash tools/prepublish_check.sh --scan results logs   # CP2/CP3 on any files

The default mode checks every file that git would publish:
`git ls-files --cached --others --exclude-standard`, i.e. tracked files plus
untracked files that are not ignored.

  CP1 course material  path denylist; sha256 overlap with
                       tools/course_files.sha256 (empty files ignored);
                       phrases that only occur in the course templates
  CP2 secrets          token and key patterns; exact values of
                       secret-looking environment variables (only the
                       variable name is ever printed)
  CP3 identifiers      AI-session paths and private links, the institution
                       name, pod hostnames (and this machine's hostname when
                       it is a container id), personal e-mail addresses,
                       absolute local paths
  CP4 sizes and paths  each file <= 5 MB and the total <= 10 MB
                       (MB = 1,000,000 bytes), ASCII-only paths, no raw data,
                       no binary documents, archives or caches
  CP5 licensing        LICENSE, LICENSE-docs, NOTICE and THIRD_PARTY.md; the
                       vendored SGLang file is unmodified and keeps its header
  CP6 numbers          python3 tools/check_numbers.py passes
  CP7 framing          phrasings the docs must not use. Negated uses are fine,
                       so this row is WARN at most and never fails the run.

Text inside .gz files and PNG text chunks is scanned too; for other binary
files the printable strings are scanned.

Exit status: 0 = no FAIL, 1 = at least one FAIL, 2 = usage error.

The patterns below are written so that this file does not match itself
(for example "[s]" inside a literal, or \\u escapes for Korean text).
Keep it that way when editing.
"""

from __future__ import annotations

import argparse
import fnmatch
import gzip
import hashlib
import io
import os
import re
import signal
import socket
import subprocess
import sys
import zlib
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MB = 1_000_000
MAX_FILE_BYTES = 5 * MB
MAX_TOTAL_BYTES = 10 * MB
MAX_DECOMPRESSED = 256 * MB
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()

# --- CP1: course material -------------------------------------------------
COURSE_HASH_FILE = "tools/course_files.sha256"
DENY_DIRS = ("bench", "common", "workloads", "scripts", "bp")
DENY_SUBSTRINGS = ("docs/workloads", "step3_hint", "artifacts_deterministic")
COURSE_PHRASES = (  # phrases from the course templates (escaped on purpose)
    "\uc790\ub3d9\uac10\uc810",
    "\uac00\uc0b0\uc810",
    "\ubc30\uc810",
    "\u203b \uc791\uc131",
)

# --- CP2: secrets -----------------------------------------------------------
# (label, regex, prefilter). The prefilter is a tuple of literals, one of
# which must occur in the text (lower-cased when the tuple starts with CI)
# before the slower regex runs.
CI = "<case-insensitive>"
SECRET_PATTERNS = (
    ("Hugging Face token", r"\bhf_[A-Za-z0-9]{30,}", ("hf_",)),
    ("RunPod API key", r"\brpa_[A-Za-z0-9]{20,}", ("rpa_",)),
    ("GitHub token", r"\bgh[pousr]_[A-Za-z0-9]{30,}", ("ghp_", "gho_", "ghu_", "ghs_", "ghr_")),
    ("GitHub fine-grained token", r"\bgithub_pa[t]_[A-Za-z0-9_]{20,}", ("github_pa" + "t_",)),
    ("sk- style API key", r"(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_-]{20,}", ("sk-",)),
    ("AWS access key id", r"\bAKI[A][0-9A-Z]{16}\b", ("AKI" + "A",)),
    ("private key block", r"PRIVATE[ ]KEY", None),
    ("bearer authorization header", r"(?i)authori[z]ation:\s*bearer\b", (CI, "authori" + "zation")),
)
# Names of environment variables whose values are secrets or identify the
# machine (pod id, hostname, public IP). Only the name is ever printed.
SECRET_ENV_NAME = re.compile(
    r"TOKEN|SECRET|PASSW(?:OR)?D|API_?KEY|ACCESS_?KEY|PRIVATE|CREDENTIAL"
    r"|(?:^|_)KEY$|(?:^|_)PAT$|_POD_ID$|_POD_HOSTNAME$|PUBLIC_IP$",
    re.I,
)
MIN_NEEDLE_LEN = 12

# --- CP3: identifiers -------------------------------------------------------
POD_HOSTNAMES = ("6302b18a8e9[7]", "d5ea3f6a593[e]", "14a76129bc5[2]",
                 "e3bf68a7254[c]", "3176d1e2a4a[c]")
POD_HOSTNAME_LITERALS = tuple(h.replace("[", "").replace("]", "") for h in POD_HOSTNAMES)
IDENTIFIER_PATTERNS = (
    ("AI-session scratch path", r"/tm[p]/claude[-]\d", None),
    ("private claude.ai link", r"claude[.]ai/(?:code|artifact|chat)\b", None),
    ("institution name", r"(?i)hose[o]", (CI, "hose" + "o")),
    ("pod hostname", r"(?i)(?:" + "|".join(POD_HOSTNAMES) + r")", (CI, *POD_HOSTNAME_LITERALS)),
)
EMAIL_LOCAL_RE = re.compile(r"[A-Za-z0-9._%+-]{1,64}$")
EMAIL_DOMAIN_RE = re.compile(r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.([A-Za-z]{2,})\b")
ALLOWED_EMAILS = {"noreply@anthropic.com", "git@github.com"}
ALLOWED_EMAIL_SUFFIXES = ("@users.noreply.github.com",)
ALLOWED_EMAIL_DOMAINS = {"example.com", "example.org", "example.net"}
FILE_EXT_TLDS = {  # "name@2x.png" and similar are file names, not addresses
    "png", "jpg", "jpeg", "gif", "svg", "py", "md", "txt", "json", "csv", "tsv",
    "log", "sh", "gz", "xz", "whl", "yml", "yaml", "html", "css", "js", "diff",
    "patch", "toml", "cfg", "ini", "jsonl",
}
LOCAL_PATH_PATTERNS = (  # labels avoid the literal paths so this file stays clean
    ("pod workspace dir", r"(?<![\w.~-])/work[s]pace(?![\w-])"),
    ("SGLang install dir", r"(?<![\w.~-])/sgl-work[s]pace(?![\w-])"),
    ("root home dir", r"(?<![\w.~-])/roo[t]/"),
    ("temp dir", r"(?<![\w.~-])/tm[p]/"),
    ("user home dir", r"(?<![\w.~-])/hom[e]/[A-Za-z0-9_.-]+"),
)
# The two sample server logs are verbatim SGLang output and legitimately
# mention the container's cache and install paths.
LOCAL_PATH_ALLOW = (("data/logs_sample/*.log", {"root home dir", "SGLang install dir"}),)

# --- CP4: sizes and paths --------------------------------------------------
RAW_TOP_DIRS = ("results", "logs", "traces")
FORBIDDEN_DIR_PARTS = {"__pycache__", ".course", "_work", "dist"}
FORBIDDEN_SUFFIXES = (".pdf", ".docx", ".doc", ".hwp", ".hwpx", ".whl", ".pyc",
                      ".pyo", ".tar", ".tgz", ".txz", ".zip", ".7z", ".bundle")
FORBIDDEN_NAME_GLOBS = ("*.tar.*", ".env", ".env.*", "jupyter*.log")

# --- CP5: licensing ---------------------------------------------------------
LEGAL_FILES = {
    "LICENSE": ("Apache License", "Version 2.0, January 2004"),
    "LICENSE-docs": ("Attribution 4.0 International",),
    "NOTICE": (),
    "THIRD_PARTY.md": (),
}
VENDORED_FILE = "third_party/sglang_v0_5_18/schedule_policy.py"
VENDORED_SHA256 = "7fa154986e574cabdbc341875401a59a7a388f94c548f11bf3d2bd7badc131d4"
UPSTREAM_HEADER = (
    "# Copyright 2023-2024 SGLang Team\n"
    '# Licensed under the Apache License, Version 2.0 (the "License");\n'
    "# you may not use this file except in compliance with the License.\n"
    "# You may obtain a copy of the License at\n"
    "#\n"
    "#     http://www.apache.org/licenses/LICENSE-2.0\n"
    "#\n"
    "# Unless required by applicable law or agreed to in writing, software\n"
    '# distributed under the License is distributed on an "AS IS" BASIS,\n'
    "# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.\n"
    "# See the License for the specific language governing permissions and\n"
    "# limitations under the License.\n"
    "# " + "=" * 78 + "\n"
)

# --- CP7: framing -----------------------------------------------------------
FRAMING_PATTERNS = (
    ("novel scheduling algorithm", r"(?i)novel\s+scheduling\s+algorith[m]"),
    ("throughput improvement", r"(?i)throughput\s+improvemen[t]"),
    ("no downside", r"(?i)\bno\s+downsid[e]"),
    ("length predictor", r"(?i)length\s+predicto[r]"),
    ("\uc190\ud574 \uc5c6\uc74c", "\uc190\ud574\\s*\uc5c6\uc74c"),
    ("\uae38\uc774 \uc608\uce21", "\uae38\uc774\\s*\uc608\uce21"),
    ("\uc0c8\ub85c\uc6b4 \uc2a4\ucf00\uc904\ub9c1 \uc54c\uace0\ub9ac\uc998",
     "\uc0c8\ub85c\uc6b4\\s*\uc2a4\ucf00\uc904\ub9c1\\s*\uc54c\uace0\ub9ac\uc998"),
    ("\ucc98\ub9ac\ub7c9 \ud5a5\uc0c1", "\ucc98\ub9ac\ub7c9\\s*\ud5a5\uc0c1"),
)

PER_FILE_HIT_CAP = 50


def _compile(patterns):
    """(label, regex[, prefilter]) -> (label, compiled, prefilter)."""
    out = []
    for label, rx, *pre in patterns:
        out.append((label, re.compile(rx), pre[0] if pre else None))
    return out


SECRET_RES = _compile(SECRET_PATTERNS)
LOCAL_PATH_RES = _compile(LOCAL_PATH_PATTERNS)
FRAMING_RES = _compile(FRAMING_PATTERNS)


def identifier_res():
    pats = list(IDENTIFIER_PATTERNS)
    host = socket.gethostname().strip().lower()
    if re.fullmatch(r"[0-9a-f]{12}", host) and host not in POD_HOSTNAME_LITERALS:
        pats.append(("this machine's hostname", re.escape(host), (CI, host)))
    return _compile(pats)


# --------------------------------------------------------------------------
@dataclass
class Item:
    rel: str
    size: int = 0
    sha256: str = ""
    kind: str = "text"  # text | gz | png | binary | empty | symlink | missing | dir
    text: str = ""      # what the content checks scan
    note: str = ""


@dataclass
class Hit:
    rel: str
    line: int
    label: str
    shown: str = ""     # safe to print (never a secret value)


def line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def png_text(data: bytes) -> str:
    """Return the tEXt/zTXt/iTXt chunks of a PNG as 'key: value' lines."""
    out = []
    pos = 8
    while pos + 8 <= len(data):
        length = int.from_bytes(data[pos:pos + 4], "big")
        ctype = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        try:
            if ctype == b"tEXt":
                k, _, v = body.partition(b"\0")
                out.append(f"{k.decode('latin-1')}: {v.decode('latin-1')}")
            elif ctype == b"zTXt":
                k, _, rest = body.partition(b"\0")
                out.append(f"{k.decode('latin-1')}: {zlib.decompress(rest[1:]).decode('latin-1')}")
            elif ctype == b"iTXt":
                k, _, rest = body.partition(b"\0")
                comp, rest = rest[0], rest[2:]
                _lang, _, rest = rest.partition(b"\0")
                _tkey, _, txt = rest.partition(b"\0")
                if comp:
                    txt = zlib.decompress(txt)
                out.append(f"{k.decode('latin-1')}: {txt.decode('utf-8', 'replace')}")
            elif ctype == b"IEND":
                break
        except (zlib.error, IndexError, ValueError):
            out.append(f"[unreadable {ctype.decode('latin-1')} chunk]")
    return "\n".join(out)


def printable_strings(data: bytes) -> str:
    return "\n".join(m.decode("ascii") for m in re.findall(rb"[\x20-\x7e]{6,}", data))


def gunzip(data: bytes) -> bytes:
    with gzip.GzipFile(fileobj=io.BytesIO(data)) as gz:
        out = gz.read(MAX_DECOMPRESSED + 1)
    if len(out) > MAX_DECOMPRESSED:
        raise ValueError("decompressed size exceeds limit")
    return out


def decode_text(data: bytes) -> str | None:
    if b"\0" in data[:8192]:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("utf-8", "replace")


def load(root: Path, rel: str) -> tuple[Item, bytes]:
    path = root / rel
    it = Item(rel)
    if path.is_symlink():
        target = os.readlink(path).encode("utf-8", "surrogateescape")
        it.kind, it.size, it.text = "symlink", len(target), target.decode("utf-8", "replace")
        it.sha256 = hashlib.sha256(target).hexdigest()
        return it, target
    if path.is_dir():
        it.kind = "dir"
        return it, b""
    if not path.is_file():
        it.kind = "missing"
        return it, b""
    data = path.read_bytes()
    it.size, it.sha256 = len(data), hashlib.sha256(data).hexdigest()
    if not data:
        it.kind = "empty"
        return it, data
    low = rel.lower()
    if low.endswith(".gz"):
        it.kind = "gz"
        try:
            inner = gunzip(data)
            it.text = decode_text(inner) or printable_strings(inner)
        except (OSError, EOFError, ValueError, zlib.error) as exc:
            it.note = f"cannot decompress ({exc}); scanned raw strings"
            it.text = printable_strings(data)
    elif data[:8] == b"\x89PNG\r\n\x1a\n":
        it.kind, it.text = "png", png_text(data)
    else:
        txt = decode_text(data)
        if txt is None:
            it.kind, it.text = "binary", printable_strings(data)
        else:
            it.text = txt
    return it, data


def list_repo_files(root: Path) -> tuple[list[str], str]:
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0")
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            capture_output=True, check=True, env=env,
        ).stdout
        files = sorted({p.decode("utf-8", "surrogateescape") for p in out.split(b"\0") if p})
        return files, "git ls-files --cached --others --exclude-standard"
    except (OSError, subprocess.CalledProcessError):
        skip = {".git", *RAW_TOP_DIRS, "dist", "_work", ".course", "__pycache__"}
        files = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d not in skip)
            for fn in filenames:
                files.append(Path(dirpath, fn).relative_to(root).as_posix())
        return sorted(files), "filesystem walk (git not available; ignore rules approximated)"


def list_scan_files(root: Path, targets: list[str]) -> list[str]:
    files = []
    for t in targets:
        p = Path(t)
        p = p if p.is_absolute() else (Path.cwd() / p)
        if p.is_dir():
            for dirpath, dirnames, filenames in os.walk(p):
                dirnames.sort()
                for fn in sorted(filenames):
                    files.append(str(Path(dirpath, fn)))
        elif p.exists():
            files.append(str(p))
        else:
            raise FileNotFoundError(t)
    return sorted(set(files))


# --- content scanners ---------------------------------------------------------
def regex_hits(it: Item, compiled, show_match: bool = False) -> list[Hit]:
    hits = []
    lowered = None
    for label, rx, pre in compiled:
        if pre:
            if pre[0] == CI:
                lowered = it.text.lower() if lowered is None else lowered
                if not any(lit in lowered for lit in pre[1:]):
                    continue
            elif not any(lit in it.text for lit in pre):
                continue
        for n, m in enumerate(rx.finditer(it.text)):
            if n >= PER_FILE_HIT_CAP:
                hits.append(Hit(it.rel, 0, label, "(more matches of this pattern not listed)"))
                break
            shown = m.group(0)[:80] if show_match else ""
            hits.append(Hit(it.rel, line_of(it.text, m.start()), label, shown))
    return hits


def email_hits(it: Item) -> list[Hit]:
    hits = []
    text = it.text
    for m in re.finditer("@", text):
        left = EMAIL_LOCAL_RE.search(text, max(0, m.start() - 64), m.start())
        if not left or left.end() != m.start():
            continue
        right = EMAIL_DOMAIN_RE.match(text, m.end())
        if not right:
            continue
        local, domain = left.group(0).lstrip("."), right.group(0)
        if not local:
            continue
        addr = f"{local}@{domain}".lower()
        if (addr in ALLOWED_EMAILS or addr.endswith(ALLOWED_EMAIL_SUFFIXES)
                or domain.lower() in ALLOWED_EMAIL_DOMAINS
                or right.group(1).lower() in FILE_EXT_TLDS):
            continue
        hits.append(Hit(it.rel, line_of(text, m.start()), "e-mail address", f"{local[0]}***@{domain}"))
        if len(hits) >= PER_FILE_HIT_CAP:
            hits.append(Hit(it.rel, 0, "e-mail address", "(more addresses not listed)"))
            break
    return hits


def local_path_hits(it: Item) -> list[Hit]:
    allowed = set()
    for pattern, labels in LOCAL_PATH_ALLOW:
        if fnmatch.fnmatchcase(it.rel, pattern):
            allowed |= labels
    hits = []
    for label, rx, _pre in LOCAL_PATH_RES:
        if label in allowed:
            continue
        for n, m in enumerate(rx.finditer(it.text)):
            if n >= PER_FILE_HIT_CAP:
                hits.append(Hit(it.rel, 0, label, "(more matches not listed)"))
                break
            end = it.text.find("\n", m.start())
            frag = it.text[m.start(): m.start() + 70 if end < 0 else min(end, m.start() + 70)]
            hits.append(Hit(it.rel, line_of(it.text, m.start()), label, frag))
    return hits


def env_needles() -> dict[str, bytes]:
    needles = {}
    for name, value in os.environ.items():
        v = value.strip()
        if SECRET_ENV_NAME.search(name) and len(v) >= MIN_NEEDLE_LEN and not v.isdigit():
            needles[name] = v.encode("utf-8", "surrogateescape")
    return needles


def needle_hits(it: Item, raw: bytes, needles: dict[str, bytes]) -> list[Hit]:
    hits = []
    extra = it.text.encode("utf-8", "replace") if it.kind in ("gz", "png") else b""
    for name, needle in sorted(needles.items()):
        if needle in raw or (extra and needle in extra):
            hits.append(Hit(it.rel, 0, f"value of environment variable {name}"))
    return hits


# --- reporting -----------------------------------------------------------------
class Table:
    def __init__(self, verbose: bool) -> None:
        self.rows: list[tuple[str, str, str, str]] = []
        self.verbose = verbose

    def add(self, cid: str, title: str, hits: list[Hit] | None = None, *,
            status: str | None = None, detail: str = "", fail_status: str = "FAIL",
            show: bool = True) -> None:
        hits = hits or []
        if status is None:
            status = fail_status if hits else "PASS"
        if hits and not detail:
            nfiles = len({h.rel for h in hits})
            detail = f"{len(hits)} hit(s) in {nfiles} file(s)"
        elif not detail and status == "PASS":
            detail = "0 hits"
        self.rows.append((cid, title, status, detail))
        if hits:
            limit = None if self.verbose else 25
            print(f"\n[{cid}] {title}: {status}")
            for h in hits[:limit]:
                loc = f"{h.rel}:{h.line}" if h.line > 0 else h.rel
                extra = f"  {h.shown}" if (show and h.shown) else ""
                print(f"  {loc}  [{h.label}]{extra}")
            if limit is not None and len(hits) > limit:
                print(f"  ... {len(hits) - limit} more (use --verbose)")

    def render(self) -> int:
        w1 = max(len(r[0]) for r in self.rows)
        w2 = max(len(r[1]) for r in self.rows)
        line = "-" * (w1 + w2 + 20 + max(len(r[3]) for r in self.rows))
        print("\n" + line)
        print(f"{'ID':<{w1}}  {'Check':<{w2}}  {'Result':<6}  Detail")
        print(line)
        for cid, title, status, detail in self.rows:
            print(f"{cid:<{w1}}  {title:<{w2}}  {status:<6}  {detail}")
        print(line)
        fails = [r for r in self.rows if r[2] == "FAIL"]
        warns = [r for r in self.rows if r[2] == "WARN"]
        verdict = "FAIL" if fails else "PASS"
        print(f"prepublish_check: {verdict} ({len(fails)} FAIL, {len(warns)} WARN, "
              f"{len(self.rows)} checks)")
        return 1 if fails else 0


# --- checks --------------------------------------------------------------------
def load_course_hashes(root: Path) -> dict[str, str] | None:
    f = root / COURSE_HASH_FILE
    if not f.is_file():
        return None
    out = {}
    for line in f.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^([0-9a-f]{64})\s+\*?(.+)$", line.strip())
        if m and m.group(1) != EMPTY_SHA256:
            out.setdefault(m.group(1), m.group(2))
    return out


def repo_mode(root: Path, verbose: bool) -> int:
    files, how = list_repo_files(root)
    print(f"prepublish_check: {len(files)} files from {how}")
    print(f"repository root: {root.name}/")
    tbl = Table(verbose)

    items: list[Item] = []
    cp1c, cp2a, cp2b, cp3a, cp3b, cp3c, cp7 = ([] for _ in range(7))
    needles = env_needles()
    id_res = identifier_res()
    phrase_res = [(p, re.compile(re.escape(p)), None) for p in COURSE_PHRASES]
    for rel in files:
        it, raw = load(root, rel)
        items.append(it)
        if it.kind in ("missing", "dir"):
            continue
        cp1c += regex_hits(it, phrase_res)
        cp2a += regex_hits(it, SECRET_RES)
        cp2b += needle_hits(it, raw, needles)
        cp3a += regex_hits(it, id_res)
        if it.kind != "binary":  # random bytes give false e-mail matches
            cp3b += email_hits(it)
        cp3c += local_path_hits(it)
        if rel.lower().endswith(".md"):
            cp7 += regex_hits(it, FRAMING_RES, show_match=True)
        it.text = ""  # free memory

    missing = [it.rel for it in items if it.kind == "missing"]
    if missing:
        print(f"note: {len(missing)} tracked path(s) are missing from the working tree "
              f"and were skipped: {', '.join(missing[:5])}{' ...' if len(missing) > 5 else ''}")
    present = [it for it in items if it.kind not in ("missing", "dir")]

    # CP1
    deny = []
    for it in items:
        parts = it.rel.split("/")
        bad_dir = next((d for d in parts[:-1] if d in DENY_DIRS), None)
        bad_sub = next((s for s in DENY_SUBSTRINGS if s in it.rel), None)
        if bad_dir or bad_sub:
            deny.append(Hit(it.rel, 0, f"denied path part '{bad_dir or bad_sub}'"))
    tbl.add("CP1a", "course paths (denylist)", deny)

    course = load_course_hashes(root)
    if course is None:
        tbl.add("CP1b", "course file sha256 overlap", status="FAIL",
                detail=f"{COURSE_HASH_FILE} is missing")
    else:
        overlap = [Hit(it.rel, 0, f"identical to course file {course[it.sha256]}")
                   for it in present if it.size > 0 and it.sha256 in course]
        tbl.add("CP1b", "course file sha256 overlap", overlap,
                detail="" if overlap else f"0 of {len(present)} files match "
                                          f"{len(course)} course hashes (empty files ignored)")
    tbl.add("CP1c", "course template phrases", cp1c)

    # CP2
    tbl.add("CP2a", "secret patterns", cp2a, show=False)
    tbl.add("CP2b", "secret env values", cp2b, show=False,
            detail="" if cp2b else f"0 hits for {len(needles)} secret-looking env values")

    # CP3
    tbl.add("CP3a", "session paths, links, names, hosts", cp3a, show=False)
    tbl.add("CP3b", "personal e-mail addresses", cp3b)
    tbl.add("CP3c", "absolute local paths", cp3c)

    # CP4
    big = [Hit(it.rel, 0, f"{it.size:,} bytes > {MAX_FILE_BYTES:,}") for it in present
           if it.size > MAX_FILE_BYTES]
    total = sum(it.size for it in present)
    tbl.add("CP4a", "file size <= 5 MB", big,
            detail="" if big else f"largest {max((it.size for it in present), default=0):,} bytes")
    tbl.add("CP4b", "total size <= 10 MB", status="PASS" if total <= MAX_TOTAL_BYTES else "FAIL",
            detail=f"{total:,} bytes in {len(present)} files")
    nonascii = [Hit(it.rel.encode("ascii", "backslashreplace").decode(), 0, "non-ASCII path")
                for it in items if not it.rel.isascii()]
    tbl.add("CP4c", "ASCII-only paths", nonascii)
    forbidden = []
    for it in items:
        parts = it.rel.split("/")
        name = parts[-1].lower()
        why = None
        if parts[0] in RAW_TOP_DIRS and len(parts) > 1:
            why = f"raw data directory {parts[0]}/"
        elif "results" in parts[:-1] and name.endswith(".json"):
            why = "results JSON"
        elif any(p in FORBIDDEN_DIR_PARTS for p in parts[:-1]):
            why = "cache or local working directory"
        elif name.endswith(FORBIDDEN_SUFFIXES):
            why = "binary document, archive or bytecode"
        elif any(fnmatch.fnmatchcase(name, g) for g in FORBIDDEN_NAME_GLOBS):
            why = "archive, environment file or notebook log"
        if why:
            forbidden.append(Hit(it.rel, 0, why))
    tbl.add("CP4d", "no raw data, documents, archives", forbidden)

    # CP5
    legal = []
    for name, needles_txt in LEGAL_FILES.items():
        p = root / name
        if not p.is_file() or p.stat().st_size == 0:
            legal.append(Hit(name, 0, "missing or empty"))
            continue
        txt = p.read_text(encoding="utf-8", errors="replace")
        for needle in needles_txt:
            if needle not in txt:
                legal.append(Hit(name, 0, f"does not contain '{needle}'"))
        if name not in files:
            legal.append(Hit(name, 0, "exists but is ignored by git"))
    tbl.add("CP5a", "LICENSE, LICENSE-docs, NOTICE, THIRD_PARTY", legal)

    vend = root / VENDORED_FILE
    if not vend.is_file():
        tbl.add("CP5b", "vendored SGLang file intact", status="SKIP",
                detail=f"{VENDORED_FILE} not present")
    else:
        data = vend.read_bytes()
        txt = data.decode("utf-8", "replace")
        problems = []
        if UPSTREAM_HEADER not in txt:
            problems.append(Hit(VENDORED_FILE, 0, "upstream copyright/license header missing or changed"))
        if hashlib.sha256(data).hexdigest() != VENDORED_SHA256:
            problems.append(Hit(VENDORED_FILE, 0, "sha256 differs from the SGLang v0.5.18 wheel "
                                                  "(NOTICE says the file is unmodified)"))
        tbl.add("CP5b", "vendored SGLang file intact", problems,
                detail="" if problems else "upstream header present, sha256 matches the wheel")

    soft = []
    if not (root / "patch/README.md").is_file():
        soft.append(Hit("patch/README.md", 0, "missing (Apache-2.0 4(b) change notice for the patch)"))
    if vend.is_file() and not (vend.parent / "LICENSE").is_file():
        soft.append(Hit(f"{vend.parent.relative_to(root).as_posix()}/LICENSE", 0,
                        "missing next to the vendored file"))
    tbl.add("CP5c", "change notice, vendored LICENSE", soft, fail_status="WARN")

    # CP6
    checker = root / "tools/check_numbers.py"
    if not checker.is_file():
        tbl.add("CP6", "numbers match artifacts", status="FAIL", detail="tools/check_numbers.py missing")
    else:
        print("\n[CP6] python3 tools/check_numbers.py")
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        proc = subprocess.run([sys.executable, str(checker), "--root", str(root)],
                              capture_output=True, text=True, env=env)
        out_lines = (proc.stdout + proc.stderr).rstrip().splitlines()
        for ln in out_lines if verbose else out_lines[-30:]:
            print(f"  {ln}")
        last = out_lines[-1] if out_lines else ""
        tbl.add("CP6", "numbers match artifacts", status="PASS" if proc.returncode == 0 else "FAIL",
                detail=last.replace("check_numbers: ", "")[:90])

    # CP7
    tbl.add("CP7", "framing phrasings (review)", cp7, fail_status="WARN",
            detail="" if not cp7 else f"{len(cp7)} line(s) to review; negated uses are fine")
    return tbl.render()


def scan_mode(root: Path, targets: list[str], verbose: bool) -> int:
    try:
        files = list_scan_files(root, targets)
    except FileNotFoundError as exc:
        print(f"prepublish_check --scan: no such file or directory: {exc}", file=sys.stderr)
        return 2
    print(f"prepublish_check --scan: {len(files)} files under {', '.join(targets)}")
    tbl = Table(verbose)
    needles = env_needles()
    id_res = identifier_res()
    cp2a, cp2b, cp3a, cp3b = [], [], [], []
    total = 0
    for f in files:
        p = Path(f)
        try:
            rel = p.resolve().relative_to(root).as_posix()
        except ValueError:
            rel = f
        it, raw = load(p.parent, p.name)
        it.rel = rel
        total += it.size
        cp2a += regex_hits(it, SECRET_RES)
        cp2b += needle_hits(it, raw, needles)
        cp3a += regex_hits(it, id_res, show_match=True)
        if it.kind != "binary":
            cp3b += email_hits(it)
    print(f"scanned {total:,} bytes")
    tbl.add("CP2a", "secret patterns", cp2a, show=False)
    tbl.add("CP2b", "secret env values", cp2b, show=False,
            detail="" if cp2b else f"0 hits for {len(needles)} secret-looking env values")
    tbl.add("CP3a", "session paths, links, names, hosts", cp3a, fail_status="WARN")
    tbl.add("CP3b", "e-mail addresses", cp3b, fail_status="WARN")
    return tbl.render()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Pre-publication checks CP1-CP7 (see the module docstring).")
    ap.add_argument("--verbose", "-v", action="store_true", help="list every hit")
    ap.add_argument("--scan", nargs="+", metavar="PATH",
                    help="only run the secret (CP2, FAIL) and identifier (CP3a/b, WARN) "
                         "scans on these files or directories, e.g. the raw data")
    ap.add_argument("--root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    root = args.root.resolve()
    if args.scan:
        return scan_mode(root, args.scan, args.verbose)
    return repo_mode(root, args.verbose)


if __name__ == "__main__":
    if hasattr(signal, "SIGPIPE"):  # exit quietly when piped into head
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    sys.exit(main())
