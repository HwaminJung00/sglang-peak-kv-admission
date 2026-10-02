#!/usr/bin/env python3
"""Seeded equivalence checks for PrefillAdder._peak_kv_fits (CPU only, stdlib only).

    python3 tests/test_upstream_equivalence.py          # N = 2,000 draws per check (CI)
    python3 tests/test_upstream_equivalence.py --full   # N = 20,000; writes artifacts/equivalence.json

The code under test is compiled from source by tests/peakkv_harness.py:
upstream = third_party/sglang_v0_5_18/schedule_policy.py (unmodified SGLang 0.5.18),
patched  = that file plus the schedule_policy.py hunks of patch/peak_kv_reservation.diff.
Requests and adders are small stubs that carry only the fields this code reads.

[A] Upstream. The gate ports SGLang's exact-length admission simulation,
    PrefillAdder.add_one_req_ignore_eos (upstream runs it only when the radix
    cache is disabled), to the radix-cache path. On the domain where the two
    are meant to coincide, their verdicts must be equal:
      alpha = 1, page_size = 1, no shared prefix (cached_tokens = 0,
      prefix_indices empty), every request ignore_eos=True and unfinished with
      >= 1 token left, 0-4 requests already admitted this round
      (can_run_list), and at least one running or admitted request.
    The upstream method runs verbatim; rem_total_tokens is unbounded because
    both paths check it identically before this simulation, and the
    post-decision bookkeeping (_update_prefill_budget, budget_state) is stubbed.
    Verdict: the candidate was appended to can_run_list.

[E] Edge cases excluded from [A], tested separately. Every verdict difference
    must follow from three documented rules:
      E1 idle: with no unfinished running request and an empty can_run_list
         the gate admits unconditionally (deadlock guard); upstream simulates
         the candidate alone and can reject.
      E2 finished: the gate skips finished requests still in running_batch
         (their KV is already freed); upstream's loop does not check
         finished() and simulates any that still have tokens left.
      E3 zero-left: an unfinished request with 0 tokens left counts as live at
         step 0 and releases its tokens before the first decode step in the
         gate; upstream drops it from the simulation (never counted, never
         released).
    Check: the gate's verdict equals upstream's add_one_req_ignore_eos run on
    the state transformed by E1-E3, for every state. Three generators:
    "mixed" is the earlier exploratory one (finished requests with any budget
    left plus rare zero-left ones; it reported 695 differing verdicts out of
    19,887 states and --full reproduces that); "finished" gives 15% of running
    requests finished with 0 tokens left (how an ignore_eos request normally
    finishes); "zero-left" gives 15% unfinished with 0 tokens left.

[B] Brute force. The gate's verdict must equal a token-by-token simulation
    written independently of the patch: every live request grows by one token
    per step until it has used alpha x its remaining budget; a request that
    finishes releases its own tokens (held minus the radix-shared prefix,
    rounded up to whole pages) plus the tokens it generated; reject if
    free <= reserve x (live requests) at any step, reserve = max(1, page_size).
    Covers random alpha (k/16 and the measured 0.6, 0.9), page sizes 1-64,
    shared prefixes, finished requests in the running batch, can_run_list
    entries (including prompts longer than one 2,048-token chunk), zero-left
    and idle states. Every state is checked at its sampled free KV and at the
    brute-force threshold (one token below: reject; at it: admit). alpha x
    remaining is an exact integer in every state, so integer steps are exact.

[P] Page alignment (alpha = 1, no sharing, page_size 2/16/64): whenever the gate
    admits, a physical paged pool (a request takes a new page each time its
    token count crosses a page boundary, and frees all its pages when it
    finishes) never fails a page allocation.

[L] Documented approximations (errata C9), reproduced exactly. Against a
    physical radix pool the gate's model overestimates free KV (L1) by the
    shared prefix when the request that created it finishes first, and (L2) by
    the prompt tokens a chunked prefill in can_run_list has yet to allocate.
    The check asserts the exact gap and the free-KV window in which the gate
    admits although the physical margin is <= 0. (L3) Oracle dependence: for
    EOS-terminated requests without max_tokens, max_new_tokens is a context
    bound and the alpha = 1 gate admits only 2 requests started together in
    the W3 pool.

Exit status is non-zero if any check fails.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from collections import Counter
from dataclasses import dataclass
from fractions import Fraction

import peakkv_harness as H

ARTIFACT = H.REPO / "artifacts" / "equivalence.json"
DEFAULT_N, FULL_N = 2_000, 20_000
SEEDS = {"A": 0, "E": 1, "B": 2, "E-finished": 5, "E-zero-left": 5, "P": 7}
INPUTS = "third_party/sglang_v0_5_18/schedule_policy.py + patch/peak_kv_reservation.diff"


# --------------------------------------------------------------------------
# Stubs: only the fields the code under test reads
# --------------------------------------------------------------------------


class SamplingParams:
    __slots__ = ("max_new_tokens", "ignore_eos")

    def __init__(self, max_new_tokens: int, ignore_eos: bool = True):
        self.max_new_tokens = max_new_tokens
        self.ignore_eos = ignore_eos


class ExtendRange:
    __slots__ = ("start", "end", "length")

    def __init__(self, start: int, end: int):
        self.start, self.end, self.length = start, end, end - start


class Req:
    """A request. Token-id lists are range objects: the code under test only takes len()."""

    def __init__(self, inp: int, out: int, max_new: int, *, prefix: int = 0, cached: int = 0,
                 finished: bool = False):
        self.origin_input_ids = range(inp)
        self.output_ids = range(out)
        self.sampling_params = SamplingParams(max_new)
        self.prefix_indices = range(prefix)
        self.cached_tokens = cached
        self.retracted_stain = False
        self.mamba_pool_idx = None
        self.extend_range = None
        self._finished = finished

    @property
    def full_untruncated_fill_ids(self):
        return range(len(self.origin_input_ids) + len(self.output_ids))

    def finished(self) -> bool:
        return self._finished

    def set_extend_range(self, start: int, end: int) -> None:
        self.extend_range = ExtendRange(start, end)


class Batch:
    def __init__(self, reqs):
        self.reqs = list(reqs)

    def batch_size(self) -> int:
        return len(self.reqs)


@dataclass
class State:
    running: list
    can_run: list
    cand: Req
    cur_rem: int
    ratio: float = 1.0
    page: int = 1


def held(r: Req) -> int:
    return len(r.origin_input_ids) + len(r.output_ids)


def remaining(r: Req) -> int:
    return max(r.sampling_params.max_new_tokens - len(r.output_ids), 0)


def extend_len(r: Req) -> int:
    """cand_extend_input_len exactly as add_one_req computes it."""
    return len(r.full_untruncated_fill_ids) - len(r.prefix_indices)


def ceil_to_page(tokens: int, page: int) -> int:
    return -(-tokens // page) * page


# --------------------------------------------------------------------------
# Code under test
# --------------------------------------------------------------------------


class Code:
    """Patched and upstream methods, compiled from source once."""

    def __init__(self):
        with H.patched_schedule_policy() as src:
            patched = H.SourceModule(src.patched, H.PATCHED_LABEL).extract(
                {"PrefillAdder": ["_peak_kv_fits", "ceil_paged_tokens"]}
            )
            self.diff_sha256 = src.diff_sha256
        upstream = H.SourceModule(H.VENDORED_FILE.read_bytes(), str(H.VENDORED_FILE)).extract(
            {"PrefillAdder": ["add_one_req_ignore_eos", "ceil_paged_tokens", "_mamba_gap_budget_for_req"]}
        )
        self.patched, self.upstream = patched, upstream
        self.reserve_tokens = upstream.module.IGNORE_EOS_RESERVE_TOKENS
        AddReqResult = upstream.module.AddReqResult
        self.NO_TOKEN = AddReqResult.NO_TOKEN

        class PatchedAdder:
            """The PrefillAdder fields that _peak_kv_fits reads."""

            _peak_kv_fits = patched.methods["PrefillAdder._peak_kv_fits"]
            ceil_paged_tokens = patched.methods["PrefillAdder.ceil_paged_tokens"]

            def __init__(self, st: State):
                self.running_batch = Batch(st.running)
                self.can_run_list = list(st.can_run)
                self.cur_rem_tokens = st.cur_rem
                self.peak_kv_reserve_ratio = st.ratio
                self.page_size = st.page

        class UpstreamAdder:
            """The PrefillAdder fields that add_one_req_ignore_eos reads (non-HIP, no SWA/dLLM/Mamba)."""

            add_one_req_ignore_eos = upstream.methods["PrefillAdder.add_one_req_ignore_eos"]
            ceil_paged_tokens = upstream.methods["PrefillAdder.ceil_paged_tokens"]
            _mamba_gap_budget_for_req = upstream.methods["PrefillAdder._mamba_gap_budget_for_req"]

            def __init__(self, st: State):
                self.running_batch = Batch(st.running)
                self.can_run_list = list(st.can_run)
                self.cur_rem_tokens = st.cur_rem
                self.rem_total_tokens = 1 << 62  # checked identically before both simulations
                self.page_size = st.page
                self.is_hybrid_swa = False
                self.req_states = None
                self.prefill_delayer_single_pass = None
                self.dllm_config = None
                self.rem_chunk_tokens = None
                self._mamba_slot_cost = 0

            @property
            def new_token_ratio(self):
                raise AssertionError("new_token_ratio is only read for requests without ignore_eos")

            def _check_prefill_tile_budget(self, candidate_extend_len):
                return None  # AMD/HIP-only budget; None on every other platform

            def _update_prefill_budget(self, *args, **kwargs):
                pass  # bookkeeping after the verdict

            def budget_state(self):
                return AddReqResult.CONTINUE

        self.PatchedAdder, self.UpstreamAdder = PatchedAdder, UpstreamAdder

    def patched_admits(self, st: State, cur_rem: int | None = None) -> bool:
        if cur_rem is not None:
            st = State(st.running, st.can_run, st.cand, cur_rem, st.ratio, st.page)
        return self.PatchedAdder(st)._peak_kv_fits(st.cand, extend_len(st.cand))

    def upstream_admits(self, st: State) -> bool:
        adder = self.UpstreamAdder(st)
        result = adder.add_one_req_ignore_eos(st.cand)
        admitted = any(r is st.cand for r in adder.can_run_list)
        if admitted == (result is self.NO_TOKEN):
            raise AssertionError(f"inconsistent upstream result {result} (admitted={admitted})")
        return admitted


# --------------------------------------------------------------------------
# [A] and [E]: the generator of the earlier exploratory comparison (kept so
# its numbers are reproducible), with the upstream method run verbatim
# --------------------------------------------------------------------------


def legacy_state(rnd, *, ratio_choices, page_choices, sharing, finished, can_run_max, zero_left, scale=1.0):
    ratio = rnd.choice(ratio_choices)
    page = rnd.choice(page_choices)
    q = 4

    def make(running):
        inp = rnd.randint(30, 600)
        out = rnd.randint(0, 3000) if running else rnd.choice([0, 0, 0, rnd.randint(1, 2000)])
        rem = q * rnd.randint(0 if zero_left else 1, int(2500 * scale))
        pre = rnd.choice([0, 36, 36, 128]) if sharing else 0
        pre = min(pre, inp)
        fin = running and finished and rnd.random() < 0.1
        return Req(inp, out, out + rem, prefix=0 if running else pre, cached=pre if running else 0,
                   finished=fin)

    running = [make(True) for _ in range(rnd.randint(0, 40))]
    can_run = [make(False) for _ in range(rnd.randint(0, can_run_max))]
    cand = make(False)
    cur_rem = rnd.randint(-2000, 120000)
    return State(running, can_run, cand, cur_rem, ratio, page)


def check_upstream(code: Code, draws: int) -> dict:
    rnd = random.Random(SEEDS["A"])
    agree = total = admitted = 0
    mismatches = []
    for _ in range(draws):
        st = legacy_state(rnd, ratio_choices=[1.0], page_choices=[1], sharing=False, finished=False,
                          can_run_max=4, zero_left=False)
        if not st.running and not st.can_run:
            continue  # idle server: the gate admits by design (E1)
        members = st.running + st.can_run + [st.cand]
        assert st.ratio == 1.0 and st.page == 1
        assert all(not r.finished() and remaining(r) >= 1 and r.cached_tokens == 0 and not r.prefix_indices
                   for r in members)
        total += 1
        p, u = code.patched_admits(st), code.upstream_admits(st)
        admitted += p
        if p == u:
            agree += 1
        elif len(mismatches) < 5:
            mismatches.append((p, u, len(st.running), len(st.can_run), st.cur_rem))
    return {"agree": agree, "total": total, "admitted": admitted, "mismatches": mismatches,
            "skipped_idle": draws - total}


def explained_upstream_admits(code: Code, st: State) -> bool:
    """Upstream add_one_req_ignore_eos on the state transformed by rules E1-E3."""
    live = [r for r in st.running if not r.finished()]
    if not live and not st.can_run:
        return True  # E1
    members = live + st.can_run + [st.cand]
    zero = [r for r in members if remaining(r) == 0]
    if zero:
        free = st.cur_rem - extend_len(st.cand)
        if free <= code.reserve_tokens * len(members):
            return False  # E3: the zero-left requests are still live at step 0
    released = sum(held(r) for r in zero)  # E3: released before the first decode step
    running = [r for r in live if remaining(r) > 0]  # E2: finished requests dropped
    can_run = [r for r in st.can_run if remaining(r) > 0]
    return code.upstream_admits(State(running, can_run, st.cand, st.cur_rem + released, st.ratio, st.page))


def edge_rules(st: State) -> str:
    live = [r for r in st.running if not r.finished()]
    if not live and not st.can_run:
        return "E1 idle"
    rules = []
    if any(r.finished() and remaining(r) > 0 for r in st.running):
        rules.append("E2 finished")
    if any(remaining(r) == 0 for r in live + st.can_run + [st.cand]):
        rules.append("E3 zero-left")
    return " + ".join(rules) or "none"


def edge_state(rnd, mode: str) -> State:
    """One rule at a time: 15% of running requests are finished with 0 tokens left
    (mode 'finished', as an ignore_eos request finishes) or unfinished with 0 left
    (mode 'zero-left')."""
    running = []
    for _ in range(rnd.randint(1, 40)):
        inp, out = rnd.randint(30, 600), rnd.randint(1, 3000)
        if mode == "finished" and rnd.random() < 0.15:
            running.append(Req(inp, out, out, finished=True))
        elif mode == "zero-left" and rnd.random() < 0.15:
            running.append(Req(inp, out, out))
        else:
            running.append(Req(inp, out, out + rnd.randint(1, 8000)))
    cand = Req(rnd.randint(30, 600), 0, rnd.randint(256, 12000))
    return State(running, [], cand, rnd.randint(0, 120000))


def check_edge_cases(code: Code, draws: int, mode: str = "mixed") -> dict:
    rnd = random.Random(SEEDS["E"] if mode == "mixed" else SEEDS["E-" + mode])
    total = differ = explained = 0
    by_rule: Counter = Counter()
    direction: Counter = Counter()
    unexplained = []
    for _ in range(draws):
        if mode == "mixed":
            st = legacy_state(rnd, ratio_choices=[1.0], page_choices=[1], sharing=False, finished=True,
                              can_run_max=4, zero_left=True)
        else:
            st = edge_state(rnd, mode)
        if not st.running and not st.can_run:
            continue
        total += 1
        p = code.patched_admits(st)
        if p == explained_upstream_admits(code, st):
            explained += 1
        elif len(unexplained) < 5:
            unexplained.append((p, len(st.running), len(st.can_run), st.cur_rem))
        if p != code.upstream_admits(st):
            differ += 1
            by_rule[edge_rules(st)] += 1
            direction["gate admits, upstream rejects" if p else "gate rejects, upstream admits"] += 1
    return {"total": total, "differ": differ, "explained": explained, "by_rule": by_rule,
            "direction": direction, "unexplained": unexplained}


# --------------------------------------------------------------------------
# [B] brute force
# --------------------------------------------------------------------------

# (alpha, step): remaining budgets are multiples of `step`, so alpha * remaining
# is an exact integer in binary floating point.
ALPHA_STEPS = tuple((k / 16, Fraction(k, 16).denominator) for k in range(1, 17)) + ((0.6, 5), (0.9, 10))
PAGE_SIZES = (1, 1, 2, 16, 64)


def bruteforce_state(rnd) -> State:
    alpha, step = rnd.choice(ALPHA_STEPS)
    page = rnd.choice(PAGE_SIZES)

    def budget():
        return step * rnd.randint(0, 4000 // step)

    running = []
    for _ in range(rnd.randint(0, 40)):
        inp, out = rnd.randint(30, 600), rnd.randint(0, 3000)
        cached = min(rnd.choice((0, 0, 36, 128, inp)), inp)
        running.append(Req(inp, out, out + budget(), cached=cached, finished=rnd.random() < 0.1))

    def waiting():
        inp = rnd.randint(30, 600) if rnd.random() < 0.8 else rnd.randint(2049, 8192)
        out = 0 if rnd.random() < 0.75 else rnd.randint(1, 2000)  # >0: a retracted request coming back
        prefix = min(rnd.choice((0, 0, 36, 128)), inp)
        return Req(inp, out, out + budget(), prefix=prefix)

    can_run = [waiting() for _ in range(rnd.randint(0, 5))]
    cand = waiting()
    # Most admission thresholds of these states lie below ~16k free tokens.
    cur_rem = rnd.randint(-2000, 16000) if rnd.random() < 0.75 else rnd.randint(16000, 160000)
    return State(running, can_run, cand, cur_rem, alpha, page)


def bruteforce(st: State) -> float:
    """Smallest margin free - reserve x live over all integer steps (inf for an idle server).

    Written from the model's definition, not from the patch: step by step,
    every live request appends one token; a request is live up to and
    including the step that produces its last budgeted token, and at the next
    step it returns its own tokens (held - shared prefix, page-rounded) and
    the tokens it generated. The gate admits iff the margin stays > 0.
    """
    live_running = [r for r in st.running if not r.finished()]
    if not live_running and not st.can_run:
        return float("inf")  # an idle server always admits one request
    members = [(r, r.cached_tokens) for r in live_running]
    members += [(r, len(r.prefix_indices)) for r in st.can_run + [st.cand]]
    budgets, release = [], []
    for r, shared in members:
        b = st.ratio * remaining(r)
        if not b.is_integer():
            raise ValueError(f"alpha x remaining = {b} is not an integer")
        budgets.append(int(b))
        release.append(ceil_to_page(max(held(r) - shared, 0), st.page))
    reserve = max(1, st.page)
    horizon = max(budgets)
    last_step = [[] for _ in range(horizon + 1)]
    for i, b in enumerate(budgets):
        last_step[b].append(i)
    free = st.cur_rem - ceil_to_page(extend_len(st.cand), st.page)
    live = len(members)
    worst = free - reserve * live  # step 0, before any decode
    for t in range(1, horizon + 1):
        for i in last_step[t - 1]:
            free += release[i] + budgets[i]
            live -= 1
        free -= live  # every live request appends one token
        worst = min(worst, free - reserve * live)
    return worst


def check_bruteforce(code: Code, draws: int) -> dict:
    rnd = random.Random(SEEDS["B"])
    agree = admitted = idle = 0
    features: Counter = Counter()
    mismatches = []
    for _ in range(draws):
        st = bruteforce_state(rnd)
        margin = bruteforce(st)
        ok = code.patched_admits(st) == (margin > 0)
        if margin != float("inf"):
            threshold = st.cur_rem - int(margin) + 1  # smallest free KV the model admits
            ok = ok and not code.patched_admits(st, threshold - 1) and code.patched_admits(st, threshold)
        else:
            idle += 1
        agree += ok
        admitted += margin > 0
        if not ok and len(mismatches) < 5:
            mismatches.append((st.ratio, st.page, len(st.running), len(st.can_run), st.cur_rem))
        features["finished in running"] += any(r.finished() for r in st.running)
        features["shared prefix"] += any(r.cached_tokens for r in st.running) or any(
            len(r.prefix_indices) for r in st.can_run + [st.cand])
        features["can_run_list"] += bool(st.can_run)
        features["prompt > 2,048"] += any(len(r.origin_input_ids) > 2048 for r in st.can_run + [st.cand])
        features["zero-left"] += any(remaining(r) == 0 for r in st.running + st.can_run + [st.cand])
        features["page > 1"] += st.page > 1
        features["alpha < 1"] += st.ratio < 1
    return {"agree": agree, "total": draws, "admitted": admitted, "idle": idle,
            "features": features, "mismatches": mismatches}


# --------------------------------------------------------------------------
# [P] page alignment against a physical paged pool
# --------------------------------------------------------------------------


def physical_paged_ok(free: int, reqs: list[tuple[int, int]], page: int) -> bool:
    """reqs: (tokens held now, tokens left to generate >= 1). Step-by-step physical pool.

    A request holding n tokens owns ceil(n / page) pages. Each step every live
    request appends one token and needs a new page when n is a multiple of the
    page size; after its last token it frees all its pages. Requests are
    grouped by n mod page, so each step costs O(1) on top of the releases.
    """
    if any(left < 1 for _, left in reqs):
        raise ValueError("every request must have at least one token left to generate")
    by_residue = [0] * page
    ends: dict[int, list[tuple[int, int]]] = {}
    for n, left in reqs:
        by_residue[n % page] += 1
        ends.setdefault(left, []).append((n, left))
    for t in range(max(left for _, left in reqs)):
        need = page * by_residue[(-t) % page]
        if free < need:
            return False
        free -= need
        for n, left in ends.get(t + 1, ()):
            by_residue[n % page] -= 1
            free += ceil_to_page(n + left, page)
    return True


def check_paged_physical(code: Code, draws: int) -> dict:
    rnd = random.Random(SEEDS["P"])
    admitted = ok = 0
    failures = []
    for _ in range(draws):
        page = rnd.choice((2, 16, 64))
        running = []
        for _ in range(rnd.randint(1, 25)):
            inp, out = rnd.randint(30, 600), rnd.randint(1, 1500)
            running.append(Req(inp, out, out + rnd.randint(1, 1500)))
        cand = Req(rnd.randint(30, 600), 0, rnd.randint(1, 1500))
        used = sum(ceil_to_page(held(r), page) for r in running)
        st = State(running, [], cand, rnd.randint(used // 4, used * 2), 1.0, page)
        if not code.patched_admits(st):
            continue
        admitted += 1
        reqs = [(held(r), remaining(r)) for r in running] + [(extend_len(cand), remaining(cand))]
        if physical_paged_ok(st.cur_rem - ceil_to_page(extend_len(cand), page), reqs, page):
            ok += 1
        elif len(failures) < 5:
            failures.append((page, len(running), st.cur_rem))
    return {"ok": ok, "admitted": admitted, "draws": draws, "failures": failures}


# --------------------------------------------------------------------------
# [L] documented approximations (errata C9)
# --------------------------------------------------------------------------


def physical_radix_worst(free: int, reqs, prefix_groups=None, extra_alloc=None) -> tuple[int, int, int]:
    """Worst step of a physical radix pool (page_size 1) under the gate's own rule.

    reqs: (left, own tokens, prefix group or None). A shared prefix (group ->
    tokens, stored once) is freed only when every request in its group has
    finished. extra_alloc: {request index: prompt tokens it still allocates at
    step 1}. Returns (margin, step, free) at the step with the smallest
    margin = free - 1 token per live request (the gate admits iff margin > 0).
    """
    prefix_groups = prefix_groups or {}
    extra_alloc = extra_alloc or {}
    worst = None
    for t in range(max(left for left, _, _ in reqs) + 1):
        growth = sum(min(t, left) for left, _, _ in reqs) + (sum(extra_alloc.values()) if t >= 1 else 0)
        released = sum(own + left + extra_alloc.get(i, 0) for i, (left, own, _) in enumerate(reqs) if left < t)
        for group, tokens in prefix_groups.items():
            if all(left < t for left, _, g in reqs if g == group):
                released += tokens
        live = sum(1 for left, _, _ in reqs if left >= t)
        now = free - growth + released
        if worst is None or now - live < worst[0]:
            worst = (now - live, t, now)
    return worst


def _approximation_rows(code: Code, scenario: str):
    """(free KV, gate admits, model margin, physical margin, physical free KV, step) per free-KV level."""
    if scenario == "L1":
        # Running request A created the 36-token shared prefix (cached_tokens =
        # 0) and finishes first; running request B and the candidate C matched
        # it. The gate counts A's prefix as freed when A finishes, but B and C
        # keep it locked in the radix tree.
        p = 36
        a, b, c = Req(100, 0, 10, cached=0), Req(100, 0, 1000, cached=p), Req(100, 0, 1000, prefix=p)
        running, can_run, cand = [a, b], [], c
        physical = dict(reqs=[(10, 100 - p, "sys"), (1000, 100 - p, "sys"), (1000, 100 - p, "sys")],
                        prefix_groups={"sys": p})
        levels = (1950, 1980, 2000, 2010, 2040)
    else:
        # X is a chunked prefill in can_run_list: 6,000-token prompt, 2,048
        # tokens prefilled in an earlier round (prefix_indices), this round's
        # 2,048 already charged to cur_rem_tokens, 1,904 still to allocate next
        # round. The gate counts X's whole prompt as freed at finish but models
        # no growth for the 1,904 tokens it has yet to allocate.
        x = Req(6000, 0, 3000, prefix=2048)
        running, can_run, cand = [Req(300, 1000, 1000 + 400, cached=0)], [x], Req(300, 0, 400)
        physical = dict(reqs=[(400, 1300, None), (3000, 6000 - 1904, None), (400, 300, None)],
                        extra_alloc={1: 1904})
        levels = (1400, 2000, 2600, 3200, 3500, 4000)
    rows = []
    for cur_rem in levels:
        st = State(running, can_run, cand, cur_rem)
        margin, step, free = physical_radix_worst(cur_rem - extend_len(cand), **physical)
        rows.append((cur_rem, code.patched_admits(st), bruteforce(st), margin, free, step))
    return rows


def check_documented_approximations(code: Code) -> list[str]:
    findings = []
    for scenario, gap, window, text in (
        ("L1", 36, [1980, 2000],
         "L1 the request that created a shared prefix finishes first: the gate's model overestimates free KV by "
         "exactly the 36-token prefix (still locked by the others) at every level; it admits at free KV 1,980 and "
         "2,000, where a physical radix pool reaches {free} free tokens at step {step} (margin {margin})"),
        ("L2", 1904, [2000, 2600, 3200, 3500],
         "L2 a chunked prefill in can_run_list still has 1,904 prompt tokens to allocate: the gate's model "
         "overestimates free KV by exactly 1,904 tokens at every level; it admits at free KV 2,000-3,500, where a "
         "physical pool reaches {free} free tokens at step {step} (margin {margin})"),
    ):
        rows = _approximation_rows(code, scenario)
        gaps = {model - margin for _, _, model, margin, _, _ in rows}
        consistent = all(admits == (model > 0) for _, admits, model, _, _, _ in rows)
        admitted_short = [r for r in rows if r[1] and r[3] <= 0]
        if gaps != {gap} or not consistent or [r[0] for r in admitted_short] != window:
            raise AssertionError(f"{scenario} differs from the documented behaviour: {rows}")
        first, last = admitted_short[0], admitted_short[-1]
        span = (lambda a, b: f"{a:,}" if a == b else f"{a:,} ... {b:,}")
        findings.append(text.format(free=span(first[4], last[4]), step=f"{first[5]:,}",
                                    margin=span(first[3], last[3])))

    # L3, oracle dependence: an EOS-terminated request that sets no max_tokens
    # gets max_new_tokens = min(context_len - 1, pool - 1) - prompt - 1, and the
    # gate reserves all of it. W3 server: context 32,768, pool 87,552 tokens,
    # 387-token prompts sharing a 36-token prefix; nothing generated yet.
    pool, prompt, shared = 87552, 387, 36
    max_new = min(32768 - 1, pool - 1) - prompt - 1
    admitted = 0
    while admitted < 64:
        running = [Req(prompt, 0, max_new, cached=shared) for _ in range(admitted)]
        used = shared + admitted * (prompt - shared) if admitted else 0
        if not code.patched_admits(State(running, [], Req(prompt, 0, max_new, prefix=shared), pool - used)):
            break
        admitted += 1
    if (max_new, admitted) != (32379, 2):
        raise AssertionError(f"L3: max_new_tokens {max_new}, admitted {admitted}")
    findings.append(
        "L3 oracle dependence: for EOS-terminated requests without max_tokens SGLang sets max_new_tokens to "
        "32,379 (387-token prompt, context 32,768); in the 87,552-token pool the alpha = 1 gate then admits "
        "2 such requests started together")
    return findings


# --------------------------------------------------------------------------


def entry(value: str, display: str, source: str) -> dict:
    return {"agg": "count", "display": display, "n": None, "source": source, "value": value}


def write_artifact(results: dict, n: int) -> None:
    a, e, b, p = results["A"], results["E"], results["B"], results["P"]
    script = "tests/test_upstream_equivalence.py --full"
    data = {
        "patch.equiv_upstream": entry(
            f"{a['agree']}/{a['total']}", H.fmt_ratio(a["agree"], a["total"]),
            f"{INPUTS}; {script}: check_upstream (seed {SEEDS['A']}, {n:,} draws; idle draws skipped); "
            "agreeing verdicts / states, _peak_kv_fits vs upstream add_one_req_ignore_eos at alpha=1, page_size=1, "
            "no shared prefix, no finished or zero-left requests"),
        "patch.equiv_bruteforce": entry(
            f"{b['agree']}/{b['total']}", H.fmt_ratio(b["agree"], b["total"]),
            f"{INPUTS}; {script}: check_bruteforce (seed {SEEDS['B']}, {n:,} states); states where _peak_kv_fits "
            "agrees with the token-by-token simulation at the sampled free KV and at the threshold"),
        "patch.equiv_edge_upstream_diff": entry(
            f"{e['differ']}/{e['total']}", H.fmt_ratio(e["differ"], e["total"]),
            f"{INPUTS}; {script}: check_edge_cases (seed {SEEDS['E']}, {n:,} draws); states with finished or "
            "zero-left requests where the verdict differs from upstream add_one_req_ignore_eos (rules E1-E3)"),
        "patch.equiv_edge_explained": entry(
            f"{e['explained']}/{e['total']}", H.fmt_ratio(e["explained"], e["total"]),
            f"{INPUTS}; {script}: check_edge_cases (seed {SEEDS['E']}, {n:,} draws); states where the verdict equals "
            "upstream add_one_req_ignore_eos after rules E1-E3"),
        "patch.equiv_paged_physical": entry(
            f"{p['ok']}/{p['admitted']}", H.fmt_ratio(p["ok"], p["admitted"]),
            f"{INPUTS}; {script}: check_paged_physical (seed {SEEDS['P']}, {n:,} draws, page_size 2/16/64, alpha=1); "
            "admitted states with no page-allocation failure / admitted states"),
    }
    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT.write_text(json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--full", action="store_true",
                        help=f"{FULL_N:,} draws per check and write {H.rel(ARTIFACT)}")
    parser.add_argument("--n", type=int, help="draws per check (overrides the default; never writes the artifact)")
    args = parser.parse_args(argv)
    n = args.n or (FULL_N if args.full else DEFAULT_N)
    write = args.full and args.n is None

    started = time.perf_counter()
    code = Code()
    failed = []
    results = {}

    def report(tag, ok, text):
        print(f"[{tag}] {'ok  ' if ok else 'FAIL'} {text}", flush=True)
        if not ok:
            failed.append(tag)

    print(f"test_upstream_equivalence: {n:,} draws per check; code from {INPUTS} "
          f"(diff sha256 {code.diff_sha256[:12]}...)", flush=True)

    a = results["A"] = check_upstream(code, n)
    report("A", a["agree"] == a["total"] and a["total"] > 0,
           f"upstream add_one_req_ignore_eos vs _peak_kv_fits: {H.fmt_ratio(a['agree'], a['total'])} verdicts agree "
           f"({a['admitted']:,} admitted; alpha=1, page_size=1, no shared prefix, no finished/zero-left requests; "
           f"{a['skipped_idle']:,} idle draws skipped){'' if not a['mismatches'] else ' first: ' + str(a['mismatches'])}")

    for mode, label in (("mixed", "finished and zero-left requests mixed (the earlier exploratory generator)"),
                        ("finished", "15% finished requests with 0 tokens left"),
                        ("zero-left", "15% unfinished requests with 0 tokens left")):
        e = check_edge_cases(code, n, mode)
        if mode == "mixed":
            results["E"] = e
        rules = ", ".join(f"{k}: {v:,}" for k, v in sorted(e["by_rule"].items())) or "none"
        directions = ", ".join(f"{k}: {v:,}" for k, v in sorted(e["direction"].items()))
        report("E", e["explained"] == e["total"] and e["total"] > 0 and "none" not in e["by_rule"],
               f"{label}: verdict differs from upstream in {H.fmt_ratio(e['differ'], e['total'])} states "
               f"(by rule: {rules}{'; ' + directions if directions else ''}); equals upstream after rules E1-E3 "
               f"in {H.fmt_ratio(e['explained'], e['total'])}"
               f"{'' if not e['unexplained'] else ' unexplained: ' + str(e['unexplained'])}")

    b = results["B"] = check_bruteforce(code, n)
    feats = ", ".join(f"{k} {v:,}" for k, v in b["features"].items())
    report("B", b["agree"] == b["total"],
           f"token-by-token brute force vs _peak_kv_fits: {H.fmt_ratio(b['agree'], b['total'])} states agree at the "
           f"sampled free KV and at the threshold ({b['admitted']:,} admitted, {b['idle']:,} idle; states with: {feats})"
           f"{'' if not b['mismatches'] else ' first: ' + str(b['mismatches'])}")

    p = results["P"] = check_paged_physical(code, n)
    report("P", p["ok"] == p["admitted"] and p["admitted"] > 0,
           f"page alignment: {H.fmt_ratio(p['ok'], p['admitted'])} admitted states run to completion in a physical "
           f"paged pool without a page-allocation failure (page_size 2/16/64, alpha=1, {p['draws']:,} draws)"
           f"{'' if not p['failures'] else ' failures: ' + str(p['failures'])}")

    try:
        for finding in check_documented_approximations(code):
            report("L", True, f"documented approximation reproduced: {finding}")
    except AssertionError as exc:
        report("L", False, str(exc))

    elapsed = time.perf_counter() - started
    if failed:
        print(f"test_upstream_equivalence: FAILED checks {', '.join(failed)} ({elapsed:.1f} s)", file=sys.stderr)
        return 1
    if write:
        write_artifact(results, n)
        print(f"test_upstream_equivalence: wrote {H.rel(ARTIFACT)}")
    print(f"test_upstream_equivalence: all checks passed ({elapsed:.1f} s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
