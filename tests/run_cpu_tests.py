#!/usr/bin/env python3
"""CPU-only tests for the peak-KV admission gate (no GPU, no SGLang install).

    python3 tests/run_cpu_tests.py            # prints 'all peak-KV unit tests passed'
    python3 tests/run_cpu_tests.py --bench    # also times _peak_kv_fits (w3/test_peak_kv.py --bench)

Steps:
1. Inputs: check third_party/sglang_v0_5_18/SHA256SUMS and patch/SHA256SUMS.
2. Patch: apply the schedule_policy.py hunks of patch/peak_kv_reservation.diff
   to the vendored upstream file in a temporary directory (strict: exact
   context, no offset, no fuzz) and check the sha256 of the result.
3. Structure: the patched module is the upstream module plus exactly the
   patch's additions (compared as ASTs); the gate sits inside
   PrefillAdder.add_one_req where patch/README.md says; both flags are off by
   default and the scheduler passes ratio=None unless the flag is set.
4. Unit tests: run w3/test_peak_kv.py, unmodified, against
   PrefillAdder._peak_kv_fits compiled from the patched source.
5. Delay counter (errata C8): every rejection increments the module counter,
   but the log line is throttled to one per 5 s, so the last logged #delays
   is a lower bound.

Standard library only. Exits non-zero on any failure.
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import io
import logging
import runpy
import sys
import traceback

import peakkv_harness as H

TEST_FILE = H.REPO / "w3" / "test_peak_kv.py"
SUCCESS_LINE = "all peak-KV unit tests passed"


def say(message: str) -> None:
    print(f"run_cpu_tests: {message}", flush=True)


# --------------------------------------------------------------------------
# Step 3: structure of the patch
# --------------------------------------------------------------------------


def _remove_one(body: list, predicate, what: str) -> None:
    hits = [i for i, node in enumerate(body) if predicate(node)]
    if len(hits) != 1:
        raise H.HarnessError(f"expected exactly one {what}, found {len(hits)}")
    del body[hits[0]]


def _calls(node: ast.AST, attr: str) -> bool:
    return any(
        isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute) and sub.func.attr == attr
        for sub in ast.walk(node)
    )


def _lock_block(fn: ast.FunctionDef) -> ast.With:
    blocks = [
        node
        for node in ast.walk(fn)
        if isinstance(node, ast.With)
        and len(node.items) == 1
        and ast.unparse(node.items[0].context_expr) == "self._lock_node(req.last_node)"
    ]
    if len(blocks) != 1:
        raise H.HarnessError(f"expected one 'with self._lock_node(req.last_node)' block, found {len(blocks)}")
    return blocks[0]


def check_structure(upstream: bytes, patched: bytes, target: H.FilePatch, file_patches) -> list[str]:
    """Return human-readable findings; raise HarnessError on any mismatch."""
    up = ast.parse(upstream)
    pt = ast.parse(patched)
    stripped = H.deep_copy(pt)
    removed = []

    # Module level: import time, two counters, the throttled log helper.
    _remove_one(
        stripped.body,
        lambda n: isinstance(n, ast.Import) and [a.name for a in n.names] == ["time"],
        "'import time'",
    )
    for name in ("_PEAK_KV_DELAYS", "_PEAK_KV_LAST_LOG"):
        _remove_one(
            stripped.body,
            lambda n, name=name: isinstance(n, ast.Assign)
            and [ast.unparse(t) for t in n.targets] == [name],
            f"module-level {name}",
        )
    _remove_one(
        stripped.body,
        lambda n: isinstance(n, ast.FunctionDef) and n.name == "_log_peak_kv_delay",
        "_log_peak_kv_delay()",
    )
    removed += ["import time", "_PEAK_KV_DELAYS = 0", "_PEAK_KV_LAST_LOG = 0.0", "def _log_peak_kv_delay"]

    adder = next(n for n in stripped.body if isinstance(n, ast.ClassDef) and n.name == "PrefillAdder")
    _remove_one(
        adder.body,
        lambda n: isinstance(n, ast.FunctionDef) and n.name == "_peak_kv_fits",
        "PrefillAdder._peak_kv_fits()",
    )
    removed.append("def PrefillAdder._peak_kv_fits")

    # __init__: one keyword parameter (default None) and one attribute assignment.
    init = next(n for n in adder.body if isinstance(n, ast.FunctionDef) and n.name == "__init__")
    last_arg, last_default = init.args.args[-1], init.args.defaults[-1]
    if last_arg.arg != "peak_kv_reserve_ratio" or not (
        isinstance(last_default, ast.Constant) and last_default.value is None
    ):
        raise H.HarnessError("PrefillAdder.__init__ must end with peak_kv_reserve_ratio=None")
    del init.args.args[-1], init.args.defaults[-1]
    _remove_one(
        init.body,
        lambda n: ast.unparse(n) == "self.peak_kv_reserve_ratio = peak_kv_reserve_ratio",
        "'self.peak_kv_reserve_ratio = peak_kv_reserve_ratio'",
    )
    removed += ["__init__(..., peak_kv_reserve_ratio=None)", "self.peak_kv_reserve_ratio = peak_kv_reserve_ratio"]

    # add_one_req: the gate, inside the _lock_node block.
    add_one_req = next(n for n in adder.body if isinstance(n, ast.FunctionDef) and n.name == "add_one_req")
    block = _lock_block(add_one_req)

    def index(predicate, what):
        hits = [i for i, node in enumerate(block.body) if predicate(node)]
        if len(hits) != 1:
            raise H.HarnessError(f"add_one_req: expected one {what} in the _lock_node block, found {len(hits)}")
        return hits[0]

    i_total = index(
        lambda n: isinstance(n, ast.If) and ast.unparse(n.test) == "total_tokens >= self.rem_total_tokens",
        "rem_total_tokens re-check",
    )
    i_swa = index(lambda n: isinstance(n, ast.If) and ast.unparse(n.test) == "self.is_hybrid_swa", "SWA re-check")
    i_gate = index(lambda n: isinstance(n, ast.If) and _calls(n.test, "_peak_kv_fits"), "peak-KV gate")
    i_delay = index(
        lambda n: isinstance(n, ast.If) and _calls(n.test, "negotiate_should_allow_prefill"),
        "prefill-delayer negotiation",
    )
    i_load = index(
        lambda n: isinstance(n, ast.If) and ast.unparse(n.test) == "req.needs_host_load_back()",
        "init_load_back",
    )
    if not i_total < i_swa < i_gate < i_delay < i_load:
        raise H.HarnessError("the gate is not between the SWA re-check and the prefill-delayer negotiation")
    gate = block.body[i_gate]
    terms = [ast.unparse(v) for v in gate.test.values] if isinstance(gate.test, ast.BoolOp) else []
    expected_terms = [
        "self.peak_kv_reserve_ratio is not None",
        "not self.is_hybrid_swa",
        "self.dllm_config is None",
        "not self._peak_kv_fits(req, cand_extend_input_len)",
    ]
    if not isinstance(gate.test, ast.BoolOp) or not isinstance(gate.test.op, ast.And) or terms != expected_terms:
        raise H.HarnessError(f"unexpected gate condition: {terms}")
    if [ast.unparse(s) for s in gate.body] != ["return AddReqResult.NO_TOKEN"] or gate.orelse:
        raise H.HarnessError("the gate must only 'return AddReqResult.NO_TOKEN'")
    del block.body[i_gate]
    removed.append("the gate in add_one_req")

    call_sites = sum(
        1
        for n in ast.walk(pt)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "_peak_kv_fits"
    )
    if call_sites != 1:
        raise H.HarnessError(f"_peak_kv_fits must have one call site, found {call_sites}")

    if H.strip_locations(stripped) != H.strip_locations(up):
        raise H.HarnessError("patched module minus the listed additions differs from upstream")

    # Flags (taken from the diff itself; server_args.py and scheduler.py are not vendored).
    sections = {fp.path: fp for fp in file_patches}
    server_args = sections["python/sglang/srt/server_args.py"]
    fields = ast.parse(
        "class ServerArgsFields:\n"
        + "".join(l[1:].decode() for l in server_args.hunks[0].lines if l[:1] == b"+")
    ).body[0].body
    defaults = {f.target.id: f.value.value for f in fields if isinstance(f, ast.AnnAssign)}
    if defaults != {"enable_peak_kv_reservation": False, "peak_kv_reserve_ratio": 1.0}:
        raise H.HarnessError(f"unexpected flag defaults: {defaults}")
    scheduler = sections["python/sglang/srt/managers/scheduler.py"]
    call = ast.parse(
        "PrefillAdder(\n" + "".join(l[1:].decode() for l in scheduler.hunks[0].lines if l[:1] == b"+") + ")"
    ).body[0].value
    (kw,) = call.keywords
    if not (
        kw.arg == "peak_kv_reserve_ratio"
        and isinstance(kw.value, ast.IfExp)
        and ast.unparse(kw.value.test) == "get_schedule().enable_peak_kv_reservation"
        and isinstance(kw.value.orelse, ast.Constant)
        and kw.value.orelse.value is None
    ):
        raise H.HarnessError("the scheduler must pass peak_kv_reserve_ratio=None unless the flag is set")

    return [
        f"patched module = upstream module + {len(removed)} additions (AST comparison): " + "; ".join(removed),
        "gate position in add_one_req, inside 'with self._lock_node(req.last_node)': "
        "rem_total_tokens re-check < SWA re-check < gate < prefill-delayer negotiation < init_load_back",
        "gate condition: " + " and ".join(expected_terms) + " -> return AddReqResult.NO_TOKEN (one call site)",
        "flags: --enable-peak-kv-reservation default False, --peak-kv-reserve-ratio default 1.0; "
        "scheduler passes peak_kv_reserve_ratio=None unless the flag is set",
    ]


# --------------------------------------------------------------------------
# Step 4: the student's unit tests, unmodified
# --------------------------------------------------------------------------


class _Tee(io.TextIOBase):
    def __init__(self, *streams):
        self.streams = streams

    def write(self, text):
        for stream in self.streams:
            stream.write(text)
        return len(text)

    def flush(self):
        for stream in self.streams:
            stream.flush()


def run_unit_tests(extracted: H.Extracted, bench: bool) -> tuple[str, int]:
    module = extracted.module
    module.PrefillAdder = type(
        "PrefillAdder",
        (),
        {
            "__module__": H.MODULE_NAME,
            "__doc__": "PrefillAdder reduced to the method under test (compiled from the patched source).",
            "_peak_kv_fits": extracted.methods["PrefillAdder._peak_kv_fits"],
        },
    )
    captured = io.StringIO()
    saved_argv = sys.argv
    sys.argv = [H.rel(TEST_FILE)] + (["--bench"] if bench else [])
    try:
        with H.fake_sglang_package(module), contextlib.redirect_stdout(_Tee(sys.stdout, captured)):
            runpy.run_path(str(TEST_FILE), run_name="__main__")
    finally:
        sys.argv = saved_argv
    return captured.getvalue(), module._PEAK_KV_DELAYS


# --------------------------------------------------------------------------
# Step 5: #delays counter semantics (errata C8)
# --------------------------------------------------------------------------


class _FakeClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now


class _Records(logging.Handler):
    def __init__(self):
        super().__init__(logging.INFO)
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


def check_delay_counter(extracted: H.Extracted) -> str:
    module = extracted.module
    clock = _FakeClock()
    module.time = clock  # _log_peak_kv_delay reads the module-level name 'time'
    logger = module.logger
    handler = _Records()
    saved_level, saved_propagate = logger.level, logger.propagate
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    try:
        for t in (100.0, 101.0, 104.9, 105.0, 106.0, 109.0):
            clock.now = t
            module._log_peak_kv_delay(40, 0, -1.0)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(saved_level)
        logger.propagate = saved_propagate
    logged = [int(m.split("#delays: ")[1].split(",")[0]) for m in handler.messages]
    if module._PEAK_KV_DELAYS != 6 or logged != [1, 4]:
        raise H.HarnessError(f"delay counter: counted {module._PEAK_KV_DELAYS}, logged {logged}")
    return (
        "6 rejections at t = 100, 101, 104.9, 105, 106, 109 s: counter = 6, "
        "log lines show '#delays: 1' and '#delays: 4' only (last logged value is a lower bound)"
    )


# --------------------------------------------------------------------------


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--bench", action="store_true", help="also run w3/test_peak_kv.py --bench")
    args = parser.parse_args(argv)
    try:
        with H.patched_schedule_policy() as src:
            say(f"[1/5] inputs: {H.rel(H.VENDORED_FILE)} sha256 {H.sha256_bytes(src.upstream)} "
                f"(= SGLang 0.5.18 wheel and tag v0.5.18); {H.rel(H.DIFF_FILE)} sha256 {src.diff_sha256}")
            stats = ", ".join(f"{fp.path.rsplit('/', 1)[-1]} +{fp.added}/-{fp.removed}" for fp in src.file_patches)
            added = sum(fp.added for fp in src.file_patches)
            deleted = sum(fp.removed for fp in src.file_patches)
            if (added, deleted) != (102, 0) or (src.target.added, src.target.removed) != (80, 0):
                raise H.HarnessError(f"unexpected diffstat: {stats}")
            say(f"[2/5] patch: {len(src.target.hunks)} hunks applied to schedule_policy.py with exact context "
                f"in a temp dir; result sha256 {H.PATCHED_SHA256} (diff total +{added}/-{deleted}: {stats})")

            for finding in check_structure(src.upstream, src.patched, src.target, src.file_patches):
                say(f"[3/5] structure: {finding}")

            source = H.SourceModule(src.patched, H.PATCHED_LABEL)
            extracted = source.extract({"PrefillAdder": ["_peak_kv_fits"]})
            names = ", ".join(f"{label} (line {line})" for label, line in extracted.globals_used)
            say(f"[4/5] compiled PrefillAdder._peak_kv_fits (line "
                f"{extracted.method_lines['PrefillAdder._peak_kv_fits']}) with: {names}")
            say(f"[4/5] running {H.rel(TEST_FILE)} unmodified")
            output, delays = run_unit_tests(extracted, args.bench)
            if SUCCESS_LINE not in output.splitlines():
                raise H.HarnessError(f"{H.rel(TEST_FILE)} finished without printing '{SUCCESS_LINE}'")
            say(f"[4/5] {H.rel(TEST_FILE)} rejected {delays:,} candidate states "
                "(module counter _PEAK_KV_DELAYS)")

            fresh = source.extract({"PrefillAdder": ["_peak_kv_fits"]})
            say(f"[5/5] delay counter (C8): {check_delay_counter(fresh)}")
    except SystemExit as exc:  # w3/test_peak_kv.py calls sys.exit(message) when it cannot run
        if exc.code not in (None, 0):
            print(f"run_cpu_tests: FAILED: {H.rel(TEST_FILE)} exited: {exc.code}", file=sys.stderr)
            return 1
        raise
    except Exception:  # noqa: BLE001 - report any failure with its traceback
        traceback.print_exc()
        print("run_cpu_tests: FAILED", file=sys.stderr)
        return 1
    say("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
