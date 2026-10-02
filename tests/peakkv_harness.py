"""Load SGLang 0.5.18 scheduler code for CPU-only tests without importing SGLang.

Importing ``sglang.srt.managers.schedule_policy`` needs torch, sgl_kernel and
the rest of the SGLang runtime, so the tests never import it. Instead they:

1. verify the hashes of the vendored upstream file and of the measured diff;
2. apply the ``schedule_policy.py`` hunks of ``patch/peak_kv_reservation.diff``
   to a copy of ``third_party/sglang_v0_5_18/schedule_policy.py`` in a
   temporary directory. The applier is strict: every context line must match
   at the line number given in the hunk header (no offset, no fuzz), and the
   result must have the sha256 that ``git apply`` and GNU ``patch -p2``
   produce;
3. parse the upstream or the patched source with ``ast`` and compile only the
   methods under test, plus the module-level names they read. Those names are
   found with ``symtable`` and resolved recursively; only standard-library
   imports may be pulled in, so nothing from SGLang or torch is executed.

Every statement that runs comes verbatim from the source file and keeps its
original line numbers, so tracebacks point at the real SGLang lines.

Standard library only.
"""

from __future__ import annotations

import __future__
import ast
import builtins
import contextlib
import copy
import hashlib
import io
import linecache
import re
import symtable
import sys
import tempfile
import types
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
VENDORED_DIR = REPO / "third_party" / "sglang_v0_5_18"
VENDORED_FILE = VENDORED_DIR / "schedule_policy.py"
PATCH_DIR = REPO / "patch"
DIFF_FILE = PATCH_DIR / "peak_kv_reservation.diff"
# Path of the file inside the diff (after stripping the a/ or b/ prefix).
DIFF_TARGET = "python/sglang/srt/managers/schedule_policy.py"
MODULE_NAME = "sglang.srt.managers.schedule_policy"

# sha256 of schedule_policy.py after the diff is applied. `git apply` on tag
# v0.5.18 (71de97b) and GNU patch 2.7.6 (`patch -p2` on the wheel layout) both
# produce exactly this file; the in-memory applier must produce it too.
PATCHED_SHA256 = "e8ceb3975e62a828580cfaccef91efa64fb2bccb170b1d0167708aabf712c8f0"

_MODULE_DUNDERS = frozenset(
    {"__name__", "__file__", "__doc__", "__spec__", "__loader__", "__package__", "__builtins__"}
)
_HUNK_RE = re.compile(rb"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_SUMS_RE = re.compile(r"^([0-9a-f]{64}) [ *](.+)$")


class HarnessError(Exception):
    """The inputs are not what the tests expect (hash, hunk or extraction mismatch)."""


def rel(path: Path) -> str:
    """Repo-relative POSIX path, for messages."""
    try:
        return Path(path).resolve().relative_to(REPO).as_posix()
    except ValueError:
        return str(path)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def check_sha256sums(directory: Path) -> list[tuple[str, str]]:
    """Verify every entry of ``<directory>/SHA256SUMS`` (``sha256sum`` format)."""
    sums_file = directory / "SHA256SUMS"
    checked = []
    for lineno, line in enumerate(sums_file.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip() or line.startswith("#"):
            continue
        match = _SUMS_RE.match(line)
        if match is None:
            raise HarnessError(f"{rel(sums_file)}:{lineno}: not a sha256sum line: {line!r}")
        digest, name = match.groups()
        actual = sha256_bytes((directory / name).read_bytes())
        if actual != digest:
            raise HarnessError(
                f"{rel(directory / name)}: sha256 {actual} does not match {rel(sums_file)} ({digest})"
            )
        checked.append((name, digest))
    if not checked:
        raise HarnessError(f"{rel(sums_file)} lists no files")
    return checked


# --------------------------------------------------------------------------
# Strict unified-diff parsing and application
# --------------------------------------------------------------------------


@dataclass
class Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: list[bytes]  # hunk body; each line keeps its ' ', '-' or '+' prefix and newline


@dataclass
class FilePatch:
    old_path: str
    new_path: str
    hunks: list[Hunk] = field(default_factory=list)

    @property
    def path(self) -> str:
        return self.new_path

    @property
    def added(self) -> int:
        return sum(1 for h in self.hunks for line in h.lines if line[:1] == b"+")

    @property
    def removed(self) -> int:
        return sum(1 for h in self.hunks for line in h.lines if line[:1] == b"-")


def _header_path(raw: bytes, prefix: str) -> str:
    text = raw.decode("utf-8").rstrip("\n").split("\t", 1)[0]
    if not text.startswith(prefix):
        raise HarnessError(f"diff header path {text!r} does not start with {prefix!r}")
    return text[len(prefix):]


def parse_unified_diff(data: bytes) -> list[FilePatch]:
    """Parse a unified diff (``diff -u`` / ``git diff`` style, a/ and b/ prefixes)."""
    lines = io.BytesIO(data).readlines()
    patches: list[FilePatch] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith(b"--- ") and i + 1 < len(lines) and lines[i + 1].startswith(b"+++ "):
            patches.append(
                FilePatch(_header_path(line[4:], "a/"), _header_path(lines[i + 1][4:], "b/"))
            )
            i += 2
            continue
        match = _HUNK_RE.match(line)
        if match is None:
            i += 1  # 'diff --git', 'index ...' and other metadata lines
            continue
        if not patches:
            raise HarnessError("hunk found before any file header")
        old_start, new_start = int(match[1]), int(match[3])
        old_count = 1 if match[2] is None else int(match[2])
        new_count = 1 if match[4] is None else int(match[4])
        body: list[bytes] = []
        n_old = n_new = 0
        i += 1
        while n_old < old_count or n_new < new_count:
            if i >= len(lines):
                raise HarnessError(f"truncated hunk @@ -{old_start},{old_count} @@")
            line = lines[i]
            if line == b"\n":  # an empty context line whose leading space was stripped
                line = b" \n"
            tag = line[:1]
            if tag == b" ":
                n_old += 1
                n_new += 1
            elif tag == b"-":
                n_old += 1
            elif tag == b"+":
                n_new += 1
            elif tag == b"\\":
                raise HarnessError("'\\ No newline at end of file' markers are not supported")
            else:
                raise HarnessError(f"unexpected line inside hunk: {line!r}")
            body.append(line)
            i += 1
        if n_old != old_count or n_new != new_count:
            raise HarnessError(f"hunk @@ -{old_start},{old_count} @@: line counts do not match its header")
        patches[-1].hunks.append(Hunk(old_start, old_count, new_start, new_count, body))
    return patches


def apply_strict(original: bytes, file_patch: FilePatch) -> bytes:
    """Apply the hunks of one file exactly where their headers say.

    Unlike ``patch`` there is no offset search and no fuzz: if any context or
    removed line differs from the original at the stated position, or the
    new-file line numbers are inconsistent, HarnessError is raised.
    """
    src = io.BytesIO(original).readlines()
    out: list[bytes] = []
    pos = 0  # next unread index in src (0-based)
    shift = 0  # (new line number) - (old line number) after the hunks so far
    for h in file_patch.hunks:
        start = h.old_start - 1 if h.old_count else h.old_start
        new_start = start + shift + 1 if h.new_count else start + shift
        if start < pos:
            raise HarnessError(f"{file_patch.path}: hunks overlap or are out of order at line {h.old_start}")
        if h.new_start != new_start:
            raise HarnessError(
                f"{file_patch.path}: hunk @@ -{h.old_start} +{h.new_start} @@ expected new start {new_start}"
            )
        old_block = [line[1:] for line in h.lines if line[:1] in (b" ", b"-")]
        new_block = [line[1:] for line in h.lines if line[:1] in (b" ", b"+")]
        actual = src[start:start + len(old_block)]
        if actual != old_block:
            k = next(
                (j for j, (a, b) in enumerate(zip(actual, old_block)) if a != b),
                min(len(actual), len(old_block)),
            )
            raise HarnessError(
                f"{file_patch.path}: hunk @@ -{h.old_start},{h.old_count} @@ does not match the "
                f"original at line {start + k + 1}"
            )
        out.extend(src[pos:start])
        out.extend(new_block)
        pos = start + len(old_block)
        shift += h.new_count - h.old_count
    out.extend(src[pos:])
    return b"".join(out)


# Filename used for code compiled from the patched source (tracebacks show it).
PATCHED_LABEL = "third_party/sglang_v0_5_18/schedule_policy.py + patch/peak_kv_reservation.diff"


@dataclass
class PatchedSource:
    upstream: bytes
    patched: bytes
    diff_sha256: str
    file_patches: list[FilePatch]
    target: FilePatch
    patched_path: Path  # the patched copy inside the temporary directory (deleted on exit)


@contextlib.contextmanager
def patched_schedule_policy():
    """Yield the upstream and patched schedule_policy.py (patched copy in a temp dir)."""
    check_sha256sums(VENDORED_DIR)
    check_sha256sums(PATCH_DIR)
    diff = DIFF_FILE.read_bytes()
    file_patches = parse_unified_diff(diff)
    targets = [fp for fp in file_patches if fp.old_path == DIFF_TARGET and fp.new_path == DIFF_TARGET]
    if len(targets) != 1:
        raise HarnessError(f"{rel(DIFF_FILE)}: expected exactly one section for {DIFF_TARGET}")
    upstream = VENDORED_FILE.read_bytes()
    with tempfile.TemporaryDirectory(prefix="peakkv-") as tmp:
        work = Path(tmp) / "schedule_policy.py"
        work.write_bytes(upstream)
        patched = apply_strict(work.read_bytes(), targets[0])
        work.write_bytes(patched)
        digest = sha256_bytes(patched)
        if digest != PATCHED_SHA256:
            raise HarnessError(f"patched schedule_policy.py has sha256 {digest}, expected {PATCHED_SHA256}")
        yield PatchedSource(upstream, patched, sha256_bytes(diff), file_patches, targets[0], work)


# --------------------------------------------------------------------------
# Extraction of methods and the module-level names they need
# --------------------------------------------------------------------------


def _defined_names(node: ast.stmt) -> list[str]:
    """Names bound at module level by a top-level statement."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return [node.name]
    if isinstance(node, ast.Import):
        return [a.asname or a.name.split(".")[0] for a in node.names]
    if isinstance(node, ast.ImportFrom):
        return [a.asname or a.name for a in node.names if a.name != "*"]
    if isinstance(node, ast.Assign):
        names = []
        for target in node.targets:
            for sub in ast.walk(target):
                if isinstance(sub, ast.Name):
                    names.append(sub.id)
        return names
    if isinstance(node, (ast.AnnAssign, ast.AugAssign)) and isinstance(node.target, ast.Name):
        return [node.target.id]
    return []


def _is_stdlib_import(node: ast.stmt) -> bool:
    if isinstance(node, ast.Import):
        return all(a.name.split(".")[0] in sys.stdlib_module_names for a in node.names)
    if isinstance(node, ast.ImportFrom):
        return node.level == 0 and (
            node.module == "__future__" or node.module.split(".")[0] in sys.stdlib_module_names
        )
    return True


def _check_relocatable(fn: ast.FunctionDef) -> None:
    """A method compiled outside its class body must not depend on the class scope."""
    for sub in ast.walk(fn):
        name = sub.id if isinstance(sub, ast.Name) else sub.attr if isinstance(sub, ast.Attribute) else None
        if name is None:
            continue
        if name in ("super", "__class__"):
            raise HarnessError(f"{fn.name} uses {name}; it cannot be compiled outside its class")
        if name.startswith("__") and not name.endswith("__"):
            raise HarnessError(f"{fn.name} uses the private (name-mangled) name {name}")


@dataclass
class Extracted:
    """Methods compiled from a source file, sharing one module namespace."""

    module: types.ModuleType
    methods: dict[str, types.FunctionType]  # "Class.method" -> function
    method_lines: dict[str, int]  # "Class.method" -> first line in the source
    globals_used: list[tuple[str, int]]  # module-level statements executed: (summary, line)


class SourceModule:
    """A parsed Python module whose pieces can be compiled on their own."""

    def __init__(self, source: bytes, filename: str):
        self.text = source.decode("utf-8")
        self.filename = filename
        self.tree = ast.parse(self.text, filename=filename)
        # Tracebacks read source lines through linecache; register them so they
        # show even when `filename` is a label or a deleted temporary file.
        linecache.cache[filename] = (len(self.text), None, self.text.splitlines(True), filename)
        self.flags = 0
        for node in self.tree.body:
            if isinstance(node, ast.ImportFrom) and node.module == "__future__":
                for alias in node.names:
                    self.flags |= getattr(__future__, alias.name).compiler_flag
        self.defs: dict[str, list[ast.stmt]] = {}
        for node in self.tree.body:
            for name in _defined_names(node):
                self.defs.setdefault(name, []).append(node)

    def class_def(self, class_name: str) -> ast.ClassDef:
        found = [n for n in self.tree.body if isinstance(n, ast.ClassDef) and n.name == class_name]
        if len(found) != 1:
            raise HarnessError(f"{self.filename}: expected one class {class_name}, found {len(found)}")
        return found[0]

    def method(self, class_name: str, method_name: str) -> ast.FunctionDef:
        found = [
            n
            for n in self.class_def(class_name).body
            if isinstance(n, ast.FunctionDef) and n.name == method_name
        ]
        if len(found) != 1:
            raise HarnessError(f"{self.filename}: expected one {class_name}.{method_name}, found {len(found)}")
        return found[0]

    def _reads(self, node: ast.stmt) -> set[str]:
        """Module-level names that ``node`` reads when it runs or when it is called."""
        prefix = "from __future__ import annotations\n" if self.flags & __future__.annotations.compiler_flag else ""
        table = symtable.symtable(prefix + ast.unparse(node), self.filename, "exec")
        names = {s.get_name() for s in table.get_symbols() if s.is_referenced()}
        stack = list(table.get_children())
        while stack:
            child = stack.pop()
            for sym in child.get_symbols():
                if sym.is_global() and (sym.is_referenced() or sym.is_assigned()):
                    names.add(sym.get_name())
            stack.extend(child.get_children())
        return names - set(_defined_names(node))

    def _closure(self, roots: list[ast.stmt]) -> list[ast.stmt]:
        """Top-level statements needed by ``roots``, in source order."""
        needed: dict[int, ast.stmt] = {}
        seen: set[str] = set()
        queue = list(roots)
        while queue:
            node = queue.pop()
            for name in sorted(self._reads(node)):
                if name in seen:
                    continue
                seen.add(name)
                definitions = self.defs.get(name)
                if definitions is None:
                    if name in _MODULE_DUNDERS or hasattr(builtins, name):
                        continue
                    raise HarnessError(f"{self.filename}: cannot resolve module-level name {name!r}")
                if len(definitions) != 1:
                    raise HarnessError(f"{self.filename}: {name!r} is bound {len(definitions)} times at module level")
                definition = definitions[0]
                if not _is_stdlib_import(definition):
                    raise HarnessError(
                        f"{self.filename}:{definition.lineno}: {name!r} comes from a non-stdlib import; "
                        "the code under test would need SGLang or torch"
                    )
                if id(definition) not in needed:
                    needed[id(definition)] = definition
                    queue.append(definition)
        return sorted(needed.values(), key=lambda n: n.lineno)

    def _compile(self, node: ast.stmt):
        module = ast.Module(body=[node], type_ignores=[])
        return compile(module, self.filename, "exec", flags=self.flags, dont_inherit=True)

    def extract(self, methods: dict[str, list[str]], module_name: str = MODULE_NAME) -> Extracted:
        """Compile ``{class: [method, ...]}`` and the module-level statements they need."""
        roots = [(c, m, self.method(c, m)) for c, names in methods.items() for m in names]
        statements = self._closure([node for _, _, node in roots])
        module = types.ModuleType(module_name)
        module.__file__ = self.filename
        namespace = module.__dict__
        for node in statements:
            exec(self._compile(node), namespace)
        compiled, lines = {}, {}
        for class_name, method_name, node in roots:
            _check_relocatable(node)
            scope: dict[str, object] = {}
            exec(self._compile(node), namespace, scope)
            fn = scope[method_name]
            fn.__qualname__ = f"{class_name}.{method_name}"
            compiled[f"{class_name}.{method_name}"] = fn
            lines[f"{class_name}.{method_name}"] = node.lineno
        used = []
        for node in statements:
            label = ", ".join(_defined_names(node)) if not isinstance(node, (ast.Import, ast.ImportFrom)) else (
                ast.unparse(node)
            )
            used.append((label, node.lineno))
        return Extracted(module, compiled, lines, used)


def strip_locations(tree: ast.AST) -> str:
    """ast.dump without line/column attributes, for structural comparison."""
    return ast.dump(tree, annotate_fields=True, include_attributes=False)


def deep_copy(tree: ast.AST) -> ast.AST:
    return copy.deepcopy(tree)


@contextlib.contextmanager
def fake_sglang_package(module: types.ModuleType):
    """Make ``module`` importable as sglang.srt.managers.schedule_policy, then undo it."""
    names = ["sglang", "sglang.srt", "sglang.srt.managers", MODULE_NAME]
    saved = {name: sys.modules.get(name) for name in names}
    try:
        for name in names[:-1]:
            package = types.ModuleType(name)
            package.__path__ = []  # a package with no files: nothing else can be imported from it
            sys.modules[name] = package
        sys.modules[MODULE_NAME] = module
        sys.modules["sglang.srt.managers"].schedule_policy = module
        yield module
    finally:
        for name, previous in saved.items():
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous


def fmt_count(n: int) -> str:
    return f"{n:,}"


def fmt_ratio(a: int, b: int) -> str:
    return f"{a:,}/{b:,}"
