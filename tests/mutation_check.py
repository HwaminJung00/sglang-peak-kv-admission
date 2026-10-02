#!/usr/bin/env python3
"""Mutation check: would the CPU tests notice a small bug in the gate?

    python3 tests/mutation_check.py          # about a minute; not part of CI

Each mutation edits one line of patch/peak_kv_reservation.diff in a temporary
copy of the test inputs (tests/, third_party/sglang_v0_5_18/, patch/ and
w3/test_peak_kv.py). The repository itself is never written. The copy's
checksums are refreshed, so only the logic checks can fail. Then both scripts
run on the copy, the equivalence checks with their CI default (N = 2,000). A
mutation is caught when at least one script exits non-zero.

Exit status is non-zero if the unmutated copy fails or any mutation survives.
Standard library only.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import peakkv_harness as H

# (description, text in the diff, replacement); each text occurs once in the diff.
MUTATIONS = [
    ("reserve check '<=' becomes '<'", "if min_free <= reserve * bs:", "if min_free < reserve * bs:"),
    ("page-size reserve dropped",
     "reserve = max(IGNORE_EOS_RESERVE_TOKENS, self.page_size)", "reserve = IGNORE_EOS_RESERVE_TOKENS"),
    ("running requests use prefix_indices",
     "shared = r.cached_tokens if in_running else len(r.prefix_indices)", "shared = len(r.prefix_indices)"),
    ("finished requests not skipped",
     "running = [r for r in running if not r.finished()]", "running = list(running)"),
    ("freed tokens off by one", "            freed += held\n", "            freed += held + 1\n"),
    ("states sorted in the wrong order", "states.sort(key=lambda x: x[0])", "states.sort(key=lambda x: -x[0])"),
    ("zero-left requests counted as 1 token left",
     "max(r.sampling_params.max_new_tokens - len(r.output_ids), 0)",
     "max(r.sampling_params.max_new_tokens - len(r.output_ids), 1)"),
    ("candidate prefill not page-rounded",
     "free = self.cur_rem_tokens - self.ceil_paged_tokens(extend_input_len)",
     "free = self.cur_rem_tokens - extend_input_len"),
    ("idle-server invariant removed", "        if not running and not self.can_run_list:\n", "        if False:\n"),
    ("gate condition changed", "                and self.dllm_config is None\n",
     "                and self.dllm_config is not None\n"),
]


def make_copy(root: Path) -> None:
    for part in ("tests", "third_party/sglang_v0_5_18", "patch"):
        shutil.copytree(H.REPO / part, root / part, ignore=shutil.ignore_patterns("__pycache__"))
    (root / "w3").mkdir()
    shutil.copy2(H.REPO / "w3" / "test_peak_kv.py", root / "w3" / "test_peak_kv.py")


def mutate(root: Path, old: str, new: str) -> None:
    diff_path = root / "patch" / "peak_kv_reservation.diff"
    text = diff_path.read_bytes().decode("utf-8")
    if text.count(old) != 1:
        raise SystemExit(f"mutation_check: {old!r} occurs {text.count(old)} times in the diff")
    diff_path.write_bytes(text.replace(old, new).encode("utf-8"))
    digest = H.sha256_bytes(diff_path.read_bytes())
    (root / "patch" / "SHA256SUMS").write_text(f"{digest}  peak_kv_reservation.diff\n", encoding="utf-8")
    target = next(fp for fp in H.parse_unified_diff(diff_path.read_bytes()) if fp.path == H.DIFF_TARGET)
    patched_digest = H.sha256_bytes(H.apply_strict(H.VENDORED_FILE.read_bytes(), target))
    harness = root / "tests" / "peakkv_harness.py"
    source, count = re.subn(r'PATCHED_SHA256 = "[0-9a-f]{64}"', f'PATCHED_SHA256 = "{patched_digest}"',
                            harness.read_text(encoding="utf-8"))
    if count != 1:
        raise SystemExit("mutation_check: PATCHED_SHA256 not found in the harness copy")
    harness.write_text(source, encoding="utf-8")


def run_tests(root: Path) -> tuple[bool, bool, list[str]]:
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    cpu = subprocess.run([sys.executable, "tests/run_cpu_tests.py"], cwd=root, env=env,
                         capture_output=True, text=True)
    eq = subprocess.run([sys.executable, "tests/test_upstream_equivalence.py"], cwd=root, env=env,
                        capture_output=True, text=True)
    failed = sorted(set(re.findall(r"^\[([A-Z])\] FAIL", eq.stdout, re.M)))
    return cpu.returncode == 0, eq.returncode == 0, failed


def main() -> int:
    rows = []
    survived = 0
    for label, old, new in [("(none: unmutated copy)", None, None)] + MUTATIONS:
        with tempfile.TemporaryDirectory(prefix="peakkv-mutant-") as tmp:
            root = Path(tmp)
            make_copy(root)
            if old is not None:
                mutate(root, old, new)
            cpu_ok, eq_ok, failed = run_tests(root)
        if old is None:
            if not (cpu_ok and eq_ok):
                print("mutation_check: the unmutated copy fails; fix that first", file=sys.stderr)
                return 1
            verdict = "-"
        else:
            caught = not (cpu_ok and eq_ok)
            survived += not caught
            verdict = "caught" if caught else "SURVIVED"
        eq_text = "pass" if eq_ok else "FAIL (" + ", ".join(failed) + ")" if failed else "FAIL"
        rows.append((label, "pass" if cpu_ok else "FAIL", eq_text, verdict))
        print(f"  {label:<44} run_cpu_tests {rows[-1][1]:<5} equivalence {eq_text:<20} {verdict}", flush=True)
    print(f"mutation_check: {len(MUTATIONS) - survived}/{len(MUTATIONS)} mutations caught")
    return 1 if survived else 0


if __name__ == "__main__":
    sys.exit(main())
