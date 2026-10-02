"""
Headline numbers: every number that README / REPORT / ERRATA quote, computed from the derived data
in git and written to artifacts/numbers.json.

    python3 -m analysis.headline            # write artifacts/numbers.json (from the repository root)
    python3 -m analysis.headline --check    # recompute and compare with the committed file

Inputs (all in git; regenerate them from the raw data release with w3.summarize,
w3.export_per_request and analysis.retract_cost, see their docstrings):
    data/derived/w3_runs.csv, w3_det_runs.csv, sweep_0907.csv, server_logs.csv
    data/derived/per_request.csv.gz, cum_output.csv.gz
    data/derived/retract_cost/summary.csv, events.csv
    data/verify/*.txt, data/logs_sample/server_reasoning_q2__default_r1.log
    w3/runs.tsv, patch/peak_kv_reservation.diff

Each entry: {"value", "display", "n", "agg", "source"}. "display" is the exact text the docs use
(tools/check_numbers.py compares the <!--n:KEY-->DISPLAY<!--/n--> markers with it). "n" is the
number of runs per condition behind the value (3 = median of three repetitions, 1 = one run),
null for constants. Display rules:
    goodput 3 decimals; seconds 1 decimal (2 below 1 s); ms 1 decimal; signed percent changes
    with 1 decimal and a U+2212 minus; plain percents 1 decimal; counts with thousands
    separators; ratios "a/b"; ranges "a–b" (en dash).
    Deliberate exceptions, used where one decimal would print 0.0 or hide the stated value:
    retract-cost percent range (2 decimals), cross-workload changes and repetition spreads
    (2 decimals), goodput_range keys (4 decimals), factors and token-usage fractions.
Float values are stored rounded to 6 decimals; displays are formatted from the unrounded values.
"""

from __future__ import annotations

import argparse
import csv
import glob
import gzip
import hashlib
import json
import math
import os
import re
import statistics
import sys
from collections import Counter, defaultdict

from w3.metrics import percentile
from w3.select_star import SPECS, rows_from_csv, select

OUT = "artifacts/numbers.json"
D = "data/derived/"
MINUS = "\u2212"   # U+2212 MINUS SIGN for negative changes
EN = "\u2013"      # U+2013 EN DASH for ranges

# Model constants (Qwen/Qwen3-4B config.json at revision 1cfa9a7208912126459214e8b04321603b3df60c)
N_LAYERS, N_KV_HEADS, HEAD_DIM, DTYPE_BYTES = 36, 8, 128, 2
# Little's-law bound: prompt length and TPOT range assumed for the estimate (the sweep's TPOT p50
# ranges from 20.4 ms at q0.5 to 24.5 ms at q4; the study used 20 and 24 ms)
LITTLE_PROMPT, LITTLE_TPOT_S = 387, (0.024, 0.020)
SHORT_OUT = 1000          # "short request" = fewer output tokens than this
TTFT_SLO_POSTHOC_S = 10.0  # hypothetical TTFT SLO for the post-hoc goodput
STALL_S = 5.0


# ----------------------------------------------------------------------------- formatting
def _sign(x: float, text: str) -> str:
    if float(text) == 0:
        return text
    return ("+" if x > 0 else MINUS) + text


def f_goodput(x): return f"{x:.3f}"
def f_goodput4(x): return f"{x:.4f}"
def f_s(x): return f"{x:.1f}" if abs(x) >= 1 else f"{x:.2f}"
def f_ms(x): return f"{x:.1f}"
def f_pct(x, nd=1): return f"{x:.{nd}f}"
def f_chg(x, nd=1): return _sign(x, f"{abs(x):.{nd}f}")
def f_count(x): return f"{int(round(float(x))):,}"
def f_tok(x): return f"{int(x):,}" if float(x).is_integer() else f"{x:,.1f}"
def f_tput(x): return f"{x:,.1f}"
def f_usage(x): return f"{x:.2f}"
def f_factor(x): return f"{x:.3f}"
def f_ratio(a, b): return f"{a:,}/{b:,}"
def f_range(a, b, f): return f"{f(a)}{EN}{f(b)}"


NUM: dict[str, dict] = {}


def put(key, value, display, n, agg, source):
    """Add one entry; the source names the inputs and the function of this module that computed it."""
    if key in NUM:
        raise SystemExit(f"duplicate key {key}")
    if isinstance(value, float):
        if math.isnan(value):
            raise SystemExit(f"{key}: value is NaN")
        value = round(value, 6)
    fn = sys._getframe(1).f_code.co_name
    NUM[key] = {"value": value, "display": display, "n": n, "agg": agg,
                "source": f"{source} [analysis/headline.py:{fn}]"}


def rng_value(a, b, nd=6):
    return f"{round(a, nd)}{EN}{round(b, nd)}"


# ----------------------------------------------------------------------------- inputs
def num(x):
    if x in ("", None):
        return None
    try:
        return float(x)
    except ValueError:
        return x


def read_csv(path, encoding="utf-8"):
    with open(path, newline="", encoding=encoding) as fh:
        return list(csv.DictReader(fh))


def load():
    runs = {r["name"]: {k: num(v) for k, v in r.items()} for r in read_csv(D + "w3_runs.csv")}
    det = {r["name"]: {k: num(v) for k, v in r.items()} for r in read_csv(D + "w3_det_runs.csv")}
    sweep = {f"q{r['qps']}": {k: num(v) for k, v in r.items()} for r in read_csv(D + "sweep_0907.csv")}
    logs = {r["log"]: {k: num(v) for k, v in r.items()} for r in read_csv(D + "server_logs.csv")}
    pr = defaultdict(list)
    with gzip.open(D + "per_request.csv.gz", "rt", encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            pr[r["run"]].append(r)
    cum = defaultdict(dict)
    with gzip.open(D + "cum_output.csv.gz", "rt", encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            cum[r["run"]][int(r["pct"])] = float(r["t_s"])
    rc_sum = read_csv(D + "retract_cost/summary.csv", encoding="utf-8-sig")
    rc_ev = read_csv(D + "retract_cost/events.csv", encoding="utf-8-sig")
    return runs, det, sweep, logs, pr, cum, rc_sum, rc_ev


# ----------------------------------------------------------------------------- per-run helpers
class Run:
    """Per-request rows of one run (data/derived/per_request.csv.gz)."""

    def __init__(self, name, rows):
        if not rows:
            raise SystemExit(f"no per-request rows for run {name}")
        self.name, self.rows = name, rows
        self.ok = [r for r in rows if r["slo_ok"] != ""]

    def span(self, drop=()):
        rr = [r for r in self.rows if r["rid"] not in drop]
        return max(float(r["end_t"]) for r in rr if r["end_t"]) - min(float(r["submit_t"]) for r in rr if r["submit_t"])

    def met(self, drop=(), ttft_max=None):
        return sum(1 for r in self.ok if r["rid"] not in drop and r["slo_ok"] == "1"
                   and (ttft_max is None or float(r["ttft_s"]) <= ttft_max))

    def goodput(self, drop=(), ttft_max=None):
        return self.met(drop, ttft_max) / self.span(drop)

    def col(self, key, rows=None):
        return [float(r[key]) for r in (self.ok if rows is None else rows) if r[key] != ""]

    def by_rid(self):
        return {r["rid"]: r for r in self.rows}

    def stalled(self):
        return [r for r in self.ok if r["max_gap_s"] and float(r["max_gap_s"]) > STALL_S]


def med(xs):
    return statistics.median(xs)


# ----------------------------------------------------------------------------- sections
def trace_and_kv(R, logs):
    base = R("reasoning_q2__default_r1")
    src = ("data/derived/per_request.csv.gz, run reasoning_q2__default_r1 (every trace sets ignore_eos=True, so "
           "completion_tokens = max_new_tokens of traces/reasoning_q2.jsonl)")
    out = [int(r["completion_tokens"]) for r in base.ok]
    # every 09-13 run of the base length distribution generated the same lengths
    lens = {r["rid"]: r["completion_tokens"] for r in base.ok}
    for name in R.names():
        if re.match(r"(det/)?reasoning(_q[\d.]+)?__", name):
            for r in R(name).ok:
                if lens[r["rid"]] != r["completion_tokens"]:
                    raise SystemExit(f"output length differs: {name} {r['rid']}")
    put("trace.n_requests", len(base.rows), f_count(len(base.rows)), None, "count", src + ": rows")
    pt = [int(r["prompt_tokens"]) for r in base.ok]
    put("trace.prompt_tokens_range", f"{min(pt)}{EN}{max(pt)}", f_range(min(pt), max(pt), f_count), None, "range",
        src + ": prompt_tokens min/max")
    for p in (50, 90, 99):
        v = percentile(out, p)
        put(f"trace.out_p{p}", v, f_tok(v), None, "derived", src + f": type-7 percentile {p} of completion_tokens")
    put("trace.out_max", max(out), f_count(max(out)), None, "derived", src + ": max completion_tokens")
    put("trace.out_total", sum(out), f_count(sum(out)), None, "derived", src + ": sum of completion_tokens")
    put("trace.out_mean", statistics.fmean(out), f_tok(round(statistics.fmean(out), 1)), None, "derived",
        src + ": mean completion_tokens")
    cached = Counter(int(r["cached_tokens"]) for n in ("reasoning_q2__default_r1", "reasoning_q2__default_r2",
                                                         "reasoning_q2__default_r3") for r in R(n).ok)
    mode = cached.most_common(1)[0][0]
    put("trace.shared_prefix_tokens", mode, f_count(mode), None, "derived",
        "data/derived/per_request.csv.gz, reasoning_q2__default_r1..r3: most common cached_tokens "
        "(radix-cache hit = shared system-prompt prefix)")
    slo = {r["tpot_slo_ms"] for r in base.rows}
    if len(slo) != 1 or any(r["ttft_slo_ms"] for r in base.rows):
        raise SystemExit(f"unexpected SLO fields in {base.name}")
    tpot_slo = int(float(slo.pop()))
    put("trace.tpot_slo_ms", tpot_slo, f_count(tpot_slo), None, "static",
        src + ": tpot_slo_ms (mean TPOT SLO per request; the trace has no TTFT SLO)")
    up = [R(f"reasoning__upstream_r{i}") for i in (1, 2, 3)]
    gaps = [(max(u.col("submit_t", u.rows)) - min(u.col("submit_t", u.rows))) / (len(u.rows) - 1) for u in up]
    put("trace.mean_interarrival_s", med(gaps), f_s(med(gaps)), 3, "median",
        "data/derived/per_request.csv.gz, reasoning__upstream_r1..r3 (qps 0.35 trace): (last - first submit_t) / 119; "
        "open-loop submit lag is a few ms")
    put("trace.nominal_interarrival_s", 1 / 0.35, f_s(1 / 0.35), None, "static", "trace generator qps=0.35: 1/qps")

    def hit(u, skip):
        rr = sorted(u.ok, key=lambda r: int(r["idx"]))[skip:]
        return 100.0 * sum(int(r["cached_tokens"]) for r in rr) / sum(int(r["prompt_tokens"]) for r in rr)
    put("trace.cache_hit_pct", med([hit(u, 0) for u in up]), f_pct(med([hit(u, 0) for u in up])), 3, "median",
        "data/derived/per_request.csv.gz, reasoning__upstream_r1..r3: 100 * sum(cached) / sum(prompt)")
    put("trace.cache_hit_excl_warmup_pct", med([hit(u, 3) for u in up]), f_pct(med([hit(u, 3) for u in up])), 3,
        "median", "same, without the first 3 requests, whose prompts the replay warm-up had already cached")

    pools = {int(v["kv_pool_tokens"]) for k, v in logs.items() if "cap65k" not in k and "batch_test" not in k}
    if len(pools) != 1:
        raise SystemExit(f"KV pool differs between logs: {pools}")
    pool = pools.pop()
    put("kv.pool_tokens", pool, f_count(pool), None, "static",
        "data/derived/server_logs.csv: max_total_num_tokens (identical in every log except cap65k)")
    cap = int(logs["logs/server_reasoning_q2__cap65k_r1.log"]["kv_pool_tokens"])
    put("kv.cap65k_pool_tokens", cap, f_count(cap), None, "static",
        "data/derived/server_logs.csv: logs/server_reasoning_q2__cap65k_r1.log (--max-total-tokens 65536)")
    bpt = 2 * N_LAYERS * N_KV_HEADS * HEAD_DIM * DTYPE_BYTES
    put("kv.bytes_per_token", bpt, f_count(bpt), None, "static",
        "Qwen/Qwen3-4B config.json (36 layers x 8 KV heads x head_dim 128) x 2 (K and V) x 2 bytes (bf16)")
    gib = pool * bpt / 2 ** 30
    # cross-check with the allocation line of a server log in git ("K size: 6.01 GB, V size: 6.01 GB", GiB)
    with open("data/logs_sample/server_reasoning_q2__default_r1.log", errors="ignore") as fh:
        m = re.search(r"#tokens: (\d+), K size: ([\d.]+) GB, V size: ([\d.]+) GB", fh.read())
    if not m or int(m.group(1)) != pool or abs(float(m.group(2)) + float(m.group(3)) - gib) > 0.011:
        raise SystemExit("KV pool size does not match the server log allocation line")
    put("kv.pool_gib", gib, f"{gib:.1f}", None, "derived",
        "kv.pool_tokens x kv.bytes_per_token / 2^30 (log: K 6.01 GB + V 6.01 GB in "
        "data/logs_sample/server_reasoning_q2__default_r1.log)")
    return pool, out


def sweep_0907(sweep, rc_sum, rc_ev, R, pool, out, logs):
    src = "data/derived/sweep_0907.csv (results/reasoning_q<Q>__default.json + logs/server_q<Q>.log, 09-07, unpatched)"
    qs = ["q0.5", "q0.6", "q0.8", "q1", "q2", "q4", "q8"]
    if sorted(sweep, key=lambda q: float(q[1:])) != qs:
        raise SystemExit(f"unexpected sweep points {sorted(sweep)}")
    metrics = [("goodput", "goodput_rps", f_goodput), ("slo_pct", "slo_pct", f_pct),
               ("ttft_p50_s", "ttft_p50_s", f_s), ("ttft_p99_s", "ttft_p99_s", f_s),
               ("maxitl_p99_s", "maxitl_p99_s", f_s), ("tpot_p50_ms", "tpot_p50_ms", f_ms),
               ("tpot_p99_ms", "tpot_p99_ms", f_ms), ("retractions", "retractions", f_count),
               ("max_usage", "max_token_usage", f_usage), ("out_tok_s", "out_tok_s", f_tput),
               ("wait10", "wait10_reqs", f_count), ("stall5", "stall5_reqs", f_count)]
    for q in qs:
        for key, col, f in metrics:
            v = sweep[q][col]
            put(f"sweep0907.{key}.{q}", int(v) if f is f_count else v, f(v), 1, "single", f"{src}: {col}")
    for q in qs:
        lg = logs[sweep[q]["server_log"]]
        for key, col in (("max_running", "max_running_reqs"), ("max_queue", "max_queue_reqs")):
            put(f"sweep0907.{key}.{q}", int(lg[col]), f_count(lg[col]), 1, "single",
                f"data/derived/server_logs.csv, {sweep[q]['server_log']}: {col}")
    for a, b in (("q0.5", "q1"), ("q0.5", "q8")):
        v = 100 * (sweep[b]["out_tok_s"] / sweep[a]["out_tok_s"] - 1)
        put(f"sweep0907.out_tok_s_change_{a}_{b}_pct", v, f_chg(v), 1, "derived",
            f"{src}: out_tok_s {b} / {a} - 1 (out_tok_s = 337,433 output tokens / wall)")
    first = min((float(q[1:]) for q in qs if sweep[q]["retractions"] > 0))
    put("diag.first_retraction_qps", first, f"{first:g}", 1, "derived",
        f"{src}: lowest qps with retractions > 0")

    rsrc = "data/derived/retract_cost/summary.csv (python3 -m analysis.retract_cost --sweep-0907)"
    labels = [r["label"] for r in rc_sum]
    if labels != qs:
        raise SystemExit(f"retract_cost summary is not the 09-07 sweep: {labels}")
    tot = lambda k: sum(int(r[k]) for r in rc_sum)
    slots, stalls = tot("retracted"), tot("stalls")
    matched = tot("exact") + tot("approx") + tot("timeonly")
    if tot("unmatched_stalls") or tot("unmatched_slots") or matched != slots or stalls != slots:
        raise SystemExit("retraction/stall matching is not one-to-one")
    put("diag.stall_match", f"{matched}/{slots}", f_ratio(matched, slots), 1, "count",
        rsrc + ": retraction slots matched to a client stall > 5 s / all retraction slots (#retracted_reqs), "
        "q0.5..q8; every stall > 5 s is matched too (unmatched_stalls = 0)")
    put("diag.stalls_gt5s", stalls, f_count(stalls), 1, "count", rsrc + ": client stalls > 5 s, q0.5..q8")
    put("diag.stall_match_exact", tot("exact"), f_count(tot("exact")), 1, "count",
        rsrc + ": matches where time and token counts agree exactly")
    put("diag.stall_match_approx", tot("approx"), f_count(tot("approx")), 1, "count",
        rsrc + ": matches where token counts agree within 2")
    st = [float(e["stall_s"]) for e in rc_ev if e["stall_s"]]
    put("diag.stall_range_s", rng_value(min(st), max(st), 3), f_range(min(st), max(st), f_s), 1, "range",
        "data/derived/retract_cost/events.csv: min/max stall_s over the 89 stalls")
    st2 = [float(e["stall_s"]) for e in rc_ev if e["stall_s"] and e["run"] == "q2"]
    put("diag.stall_range_q2_s", rng_value(min(st2), max(st2), 3), f_range(min(st2), max(st2), f_s), 1, "range",
        "data/derived/retract_cost/events.csv: min/max stall_s, run q2")
    w = [float(r["waste_pct"]) for r in rc_sum if int(r["retracted"]) > 0]
    put("diag.recompute_pct_range", rng_value(min(w), max(w), 4), f_range(min(w), max(w), lambda x: f"{x:.2f}"),
        1, "range", rsrc + ": waste_pct (prefill tokens above the q0.5 baseline / processed tokens), "
        "runs with retractions (q0.6..q8); 2 decimals")
    for r in rc_sum:
        if r["prefill_excess"] not in ("", None) and int(r["retracted"]) > 0:
            put(f"diag.prefill_excess.{r['label']}", int(r["prefill_excess"]), f_count(r["prefill_excess"]), 1, "single",
                rsrc + f": prefill_excess, run {r['label']} (prefill tokens above the q0.5 run)")
    conf = [e for e in rc_ev if e["reprefill_new_token"]]
    put("diag.reprefill_confirmed", len(conf), f_count(len(conf)), 1, "count",
        "data/derived/retract_cost/events.csv: retractions whose re-prefill line was identified")
    dmax = max(abs(float(e["retract_to_reprefill_s"]) - float(e["stall_s"])) for e in conf)
    put("diag.reprefill_delay_vs_stall_max_s", dmax, f_s(dmax), 1, "derived",
        "data/derived/retract_cost/events.csv: max |retract_to_reprefill_s - stall_s| over the confirmed re-prefills "
        "(log time stamps have 1 s resolution)")
    e = statistics.fmean(LITTLE_PROMPT * o + o * o / 2 for o in out)
    lam = [pool / (t * e) for t in LITTLE_TPOT_S]
    put("diag.little_lambda_max", rng_value(lam[0], lam[1], 4), f_range(lam[0], lam[1], lambda x: f"{x:.2f}"),
        None, "derived",
        "Little's law: lambda_max = kv.pool_tokens / (TPOT x E[387*o + o^2/2]) with o = trace output lengths "
        "(data/derived/per_request.csv.gz), TPOT 24..20 ms (analysis/headline.py LITTLE_TPOT_S)")
    # every TPOT-SLO violator of the sweep is a retraction victim
    victims = defaultdict(set)
    for ev in rc_ev:
        if ev["rid"]:
            victims[ev["run"]].add(ev["rid"])
    viol = vin = 0
    for q in qs:
        for r in R(f"reasoning_{q}__default").ok:
            if r["slo_ok"] == "0":
                viol += 1
                vin += r["rid"] in victims[q]
    put("diag.slo_violators_all_victims", f"{vin}/{viol}", f_ratio(vin, viol), 1, "count",
        "data/derived/per_request.csv.gz (era 0907, slo_ok = 0) and data/derived/retract_cost/events.csv (victim rid): "
        "SLO violators that were retraction victims / all SLO violators, q0.5..q8")


def baseline(runs):
    names = [f"reasoning__upstream_r{i}" for i in (1, 2, 3)]
    rows = [runs[n] for n in names]
    src = "data/derived/w3_runs.csv, reasoning__upstream_r1..r3 (unpatched, qps 0.35, 09-13): median of "
    m = lambda c: med([r[c] for r in rows])
    p = "baseline.q035.upstream."
    put(p + "goodput", m("goodput_rps"), f_goodput(m("goodput_rps")), 3, "median", src + "goodput_rps")
    for key, col in (("ttft_p50", "ttft_p50"), ("ttft_p99", "ttft_p99"), ("maxitl_p99", "maxitl_p99")):
        put(p + key + "_s", m(col) / 1000, f_s(m(col) / 1000), 3, "median", src + col + " / 1000")
        put(p + key + "_ms", m(col), f_ms(m(col)), 3, "median", src + col)
    put(p + "tpot_p50_ms", m("tpot_p50"), f_ms(m("tpot_p50")), 3, "median", src + "tpot_p50")
    put(p + "tpot_p99_ms", m("tpot_p99"), f_ms(m("tpot_p99")), 3, "median", src + "tpot_p99")
    put(p + "out_tok_s", m("out_tok_s"), f_tput(m("out_tok_s")), 3, "median", src + "out_tok_s")
    put(p + "retractions", int(m("retracted")), f_count(m("retracted")), 3, "median", src + "retracted")
    put(p + "max_usage", m("max_usage"), f_usage(m("max_usage")), 3, "median", src + "max_usage")
    put(p + "max_running", int(m("max_running")), f_count(m("max_running")), 3, "median", src + "max_running")
    put(p + "wall_s", m("wall_s"), f_s(m("wall_s")), 3, "median", src + "wall_s")
    put(p + "e2e_p99_s", m("e2e_p99") / 1000, f_s(m("e2e_p99") / 1000), 3, "median", src + "e2e_p99 / 1000")
    put(p + "slo_pct", m("slo_%"), f_pct(m("slo_%")), 3, "median", src + "slo_%")
    put(p + "cache_hit_pct", m("cache_hit_%"), f_pct(m("cache_hit_%")), 3, "median", src + "cache_hit_%")


Q2_3BAR = {"default": "default", "ablated": "ablated", "mine": "mine_a1.0"}
Q2_SINGLE = ["upstream", "noradix", "cap65k", "tuned_n24", "tuned_n32", "tuned_n40", "tuned_n48",
             "mine_a0.5", "mine_a0.6", "mine_a0.75", "mine_a0.9"]


def q2_bars(runs, R, cum):
    W = "data/derived/w3_runs.csv"
    P = "data/derived/per_request.csv.gz"
    med3 = {}
    for bar, tag in Q2_3BAR.items():
        names = [f"reasoning_q2__{tag}_r{i}" for i in (1, 2, 3)]
        rows, rr = [runs[n] for n in names], [R(n) for n in names]
        src = f"{W}, {names[0][:-1]}1..r3: "
        psrc = f"{P}, {names[0][:-1]}1..r3: "
        p = f"q2.{bar}."
        col = lambda c: [r[c] for r in rows]
        gp = col("goodput_rps")
        put(p + "goodput", med(gp), f_goodput(med(gp)), 3, "median", src + "median goodput_rps")
        put(p + "goodput_min", min(gp), f_goodput(min(gp)), 3, "range", src + "min goodput_rps")
        put(p + "goodput_max", max(gp), f_goodput(max(gp)), 3, "range", src + "max goodput_rps")
        put(p + "goodput_range", rng_value(min(gp), max(gp)), f_range(min(gp), max(gp), f_goodput4), 3, "range",
            src + "min-max goodput_rps, 4 decimals")
        spread = 100 * (max(gp) - min(gp)) / med(gp)
        put(p + "goodput_spread_pct", spread, f_pct(spread, 2), 3, "derived",
            src + "(max - min) / median goodput_rps x 100, 2 decimals")
        met = [r.met() for r in rr]
        put(p + "slo_met", int(med(met)), f_count(med(met)), 3, "median", psrc + "median count of slo_ok = 1")
        put(p + "slo_ratio", f"{int(med(met))}/{len(rr[0].rows)}", f_ratio(int(med(met)), len(rr[0].rows)), 3, "median",
            psrc + "median count of slo_ok = 1 / requests")
        put(p + "slo_pct", med(col("slo_%")), f_pct(med(col("slo_%"))), 3, "median", src + "median slo_%")
        rt = col("retracted")
        put(p + "retractions", int(med(rt)), f_count(med(rt)), 3, "median",
            src + "median retracted (sum of #retracted_reqs in the server log)")
        put(p + "retractions_min", int(min(rt)), f_count(min(rt)), 3, "range", src + "min retracted")
        put(p + "retractions_max", int(max(rt)), f_count(max(rt)), 3, "range", src + "max retracted")
        for key, c in (("maxitl_p99", "maxitl_p99"), ("ttft_p99", "ttft_p99")):
            v = [x / 1000 for x in col(c)]
            put(p + key + "_s", med(v), f_s(med(v)), 3, "median", src + f"median {c} / 1000")
            put(p + key + "_range_s", rng_value(min(v), max(v)), f_range(min(v), max(v), f_s), 3, "range",
                src + f"min-max {c} / 1000")
        t50 = [x / 1000 for x in col("ttft_p50")]
        put(p + "ttft_p50_s", med(t50), f_s(med(t50)), 3, "median", src + "median ttft_p50 / 1000")
        put(p + "ttft_p50_min", min(t50), f_s(min(t50)), 3, "range", src + "min ttft_p50 / 1000 (s)")
        put(p + "ttft_p50_max", max(t50), f_s(max(t50)), 3, "range", src + "max ttft_p50 / 1000 (s)")
        tm = [x / 1000 for x in col("ttft_mean")]
        put(p + "ttft_mean_s", med(tm), f_s(med(tm)), 3, "median", src + "median ttft_mean / 1000")
        put(p + "tpot_p50_ms", med(col("tpot_p50")), f_ms(med(col("tpot_p50"))), 3, "median", src + "median tpot_p50")
        put(p + "tpot_p99_ms", med(col("tpot_p99")), f_ms(med(col("tpot_p99"))), 3, "median", src + "median tpot_p99")
        e50 = [x / 1000 for x in col("e2e_p50")]
        put(p + "e2e_p50_s", med(e50), f_s(med(e50)), 3, "median", src + "median e2e_p50 / 1000")
        e99 = [x / 1000 for x in col("e2e_p99")]
        put(p + "e2e_p99_s", med(e99), f_s(med(e99)), 3, "median", src + "median e2e_p99 / 1000")
        em = [statistics.fmean(r.col("e2e_s")) for r in rr]
        put(p + "e2e_mean_s", med(em), f_s(med(em)), 3, "median", psrc + "median of the per-run mean e2e_s")
        put(p + "out_tok_s", med(col("out_tok_s")), f_tput(med(col("out_tok_s"))), 3, "median",
            src + "median out_tok_s (= 337,433 output tokens / wall)")
        sp = [r.span() for r in rr]
        put(p + "wall_s", med(sp), f_s(med(sp)), 3, "median",
            psrc + "median span (max end_t - min submit_t), the goodput denominator")
        put(p + "wait10", int(med(col("wait10_reqs"))), f_count(med(col("wait10_reqs"))), 3, "median",
            src + "median wait10_reqs (TTFT > 10 s)")
        put(p + "stall5", int(med(col("stall5_reqs"))), f_count(med(col("stall5_reqs"))), 3, "median",
            src + "median stall5_reqs (requests with an in-stream gap > 5 s)")
        put(p + "peak_delays", int(med(col("peak_delays"))), f_count(med(col("peak_delays"))), 3, "median",
            src + "median peak_delays (last logged #delays; lower bound)")
        put(p + "aux_slo_pct", med(col("aux_slo_%")), f_pct(med(col("aux_slo_%"))), 3, "median",
            src + "median aux_slo_% (TPOT SLO met and no gap > 5 s)")
        put(p + "graph_pct", med(col("graph_%")), f_pct(med(col("graph_%"))), 3, "median",
            src + "median graph_% (Decode lines with cuda graph: True)")
        put(p + "cache_hit_pct", med(col("cache_hit_%")), f_pct(med(col("cache_hit_%"))), 3, "median",
            src + "median cache_hit_%")
        med3[bar] = dict(goodput=med(gp), met=med(met), span=med(sp), ttft_p99=med(col("ttft_p99")),
                         ttft_mean=med(tm), out_tok_s=med(col("out_tok_s")), e2e_p50=med(e50), e2e_p99=med(e99),
                         e2e_mean=med(em), runs=rr)
    # default-run details used in the text
    d = med3["default"]["runs"]
    st = [r.stalled() for r in d]
    gaps = [float(x["max_gap_s"]) for s_ in st for x in s_]
    put("q2.default.stall_range_s", rng_value(min(gaps), max(gaps)), f_range(min(gaps), max(gaps), f_s), 3, "range",
        f"{P}, reasoning_q2__default_r1..r3: min-max max_gap_s of requests with a gap > 5 s")
    n5 = [len(s_) for s_ in st]
    put("q2.default.stall5_range", f"{min(n5)}{EN}{max(n5)}", f_range(min(n5), max(n5), f_count), 3, "range",
        f"{P}, reasoning_q2__default_r1..r3: requests with a gap > 5 s per run")
    okst = [sum(1 for x in s_ if x["slo_ok"] == "1") for s_ in st]
    put("q2.default.stalled_tpot_ok_range", f"{min(okst)}{EN}{max(okst)}", f_range(min(okst), max(okst), f_count), 3,
        "range", f"{P}, reasoning_q2__default_r1..r3: requests with a gap > 5 s that still met the mean-TPOT SLO")
    aux = [runs[f"reasoning_q2__default_r{i}"]["aux_slo_%"] for i in (1, 2, 3)]
    put("q2.default.aux_slo_range_pct", rng_value(min(aux), max(aux), 4), f_range(min(aux), max(aux), f_pct), 3,
        "range", f"{W}, reasoning_q2__default_r1..r3: min-max aux_slo_%")

    for bar in Q2_SINGLE:
        name = f"reasoning_q2__{bar}_r1"
        r, rr = runs[name], R(name)
        src = f"{W}, {name} (n=1): "
        p = f"q2.{bar}."
        put(p + "goodput", r["goodput_rps"], f_goodput(r["goodput_rps"]), 1, "single", src + "goodput_rps")
        put(p + "slo_met", rr.met(), f_count(rr.met()), 1, "single", f"{P}, {name}: count of slo_ok = 1")
        put(p + "slo_ratio", f"{rr.met()}/{len(rr.rows)}", f_ratio(rr.met(), len(rr.rows)), 1, "single",
            f"{P}, {name}: count of slo_ok = 1 / requests")
        put(p + "slo_pct", r["slo_%"], f_pct(r["slo_%"]), 1, "single", src + "slo_%")
        put(p + "retractions", int(r["retracted"]), f_count(r["retracted"]), 1, "single", src + "retracted")
        put(p + "maxitl_p99_s", r["maxitl_p99"] / 1000, f_s(r["maxitl_p99"] / 1000), 1, "single", src + "maxitl_p99 / 1000")
        put(p + "ttft_p50_s", r["ttft_p50"] / 1000, f_s(r["ttft_p50"] / 1000), 1, "single", src + "ttft_p50 / 1000")
        put(p + "ttft_p99_s", r["ttft_p99"] / 1000, f_s(r["ttft_p99"] / 1000), 1, "single", src + "ttft_p99 / 1000")
        put(p + "ttft_mean_s", r["ttft_mean"] / 1000, f_s(r["ttft_mean"] / 1000), 1, "single", src + "ttft_mean / 1000")
        put(p + "tpot_p50_ms", r["tpot_p50"], f_ms(r["tpot_p50"]), 1, "single", src + "tpot_p50")
        put(p + "wait10", int(r["wait10_reqs"]), f_count(r["wait10_reqs"]), 1, "single", src + "wait10_reqs")
        put(p + "stall5", int(r["stall5_reqs"]), f_count(r["stall5_reqs"]), 1, "single", src + "stall5_reqs")
        put(p + "peak_delays", int(r["peak_delays"]), f_count(r["peak_delays"]), 1, "single",
            src + "peak_delays (last logged #delays; lower bound)")
        put(p + "wall_s", rr.span(), f_s(rr.span()), 1, "single", f"{P}, {name}: span")
        put(p + "graph_pct", r["graph_%"], f_pct(r["graph_%"]), 1, "single", src + "graph_%")
        put(p + "cache_hit_pct", r["cache_hit_%"], f_pct(r["cache_hit_%"]), 1, "single", src + "cache_hit_%")
        med3[bar] = dict(goodput=r["goodput_rps"], runs=[rr])

    # gain decomposition (default -> mine, medians of 3)
    dm, mm = med3["default"], med3["mine"]
    gsrc = "q2.default.* and q2.mine.* (3-run medians): "
    put("q2.gain.goodput_pct", 100 * (mm["goodput"] / dm["goodput"] - 1),
        f_chg(100 * (mm["goodput"] / dm["goodput"] - 1)), 3, "derived", gsrc + "goodput mine / default - 1")
    sf, wf = mm["met"] / dm["met"], dm["span"] / mm["span"]
    put("q2.gain.slo_factor", sf, f_factor(sf), 3, "derived", gsrc + "slo_met mine / default")
    put("q2.gain.wall_factor", wf, f_factor(wf), 3, "derived", gsrc + "wall_s default / mine")
    if abs(sf * wf - mm["goodput"] / dm["goodput"]) > 1e-4:
        raise SystemExit("goodput gain is not slo_factor x wall_factor")
    for key, k in (("ttft_p99_pct", "ttft_p99"), ("ttft_mean_pct", "ttft_mean"), ("out_tok_s_pct", "out_tok_s"),
                   ("e2e_p50_pct", "e2e_p50"), ("e2e_p99_pct", "e2e_p99"), ("e2e_mean_pct", "e2e_mean")):
        v = 100 * (mm[k] / dm[k] - 1)
        put(f"q2.gain.{key}", v, f_chg(v), 3, "derived", gsrc + f"{k} mine / default - 1")

    # the longest request decides the wall time
    d_runs, m_runs = dm["runs"], mm["runs"]
    base = d_runs[0]
    longest = max(base.ok, key=lambda r: int(r["completion_tokens"]))
    rid = longest["rid"]
    put("q2.longest.rid", rid, rid, None, "derived", f"{P}, reasoning_q2__default_r1: request with the most output tokens")
    put("q2.longest.out_tokens", int(longest["completion_tokens"]), f_count(longest["completion_tokens"]), None,
        "derived", f"{P}: completion_tokens of {rid}")
    last = sum(1 for r in d_runs if max(r.ok, key=lambda x: float(x["end_t"]))["rid"] == rid)
    put("q2.longest.last_to_finish_default", f"{last}/{len(d_runs)}", f_ratio(last, len(d_runs)), 3, "count",
        f"{P}, reasoning_q2__default_r1..r3: runs in which {rid} finished last")
    g = [float(r.by_rid()[rid]["max_gap_s"]) for r in d_runs]
    put("q2.longest.default_stall_s", med(g), f_s(med(g)), 3, "median",
        f"{P}, reasoning_q2__default_r1..r3: median max_gap_s of {rid}")
    put("q2.longest.default_stall_range_s", rng_value(min(g), max(g)), f_range(min(g), max(g), f_s), 3, "range",
        f"{P}, reasoning_q2__default_r1..r3: min-max max_gap_s of {rid}")
    t = [float(r.by_rid()[rid]["ttft_s"]) for r in m_runs]
    put("q2.longest.mine_ttft_s", med(t), f_s(med(t)), 3, "median",
        f"{P}, reasoning_q2__mine_a1.0_r1..r3: median ttft_s of {rid}")
    ex_d = med([r.goodput(drop={rid}) for r in d_runs])
    ex_m = med([r.goodput(drop={rid}) for r in m_runs])
    esrc = f"{P}, reasoning_q2__{{default,mine_a1.0}}_r1..r3: median of count(slo_ok)/span without {rid}"
    put("q2.excl_longest.default_goodput", ex_d, f_goodput(ex_d), 3, "median", esrc)
    put("q2.excl_longest.mine_goodput", ex_m, f_goodput(ex_m), 3, "median", esrc)
    put("q2.excl_longest.gain_pct", 100 * (ex_m / ex_d - 1), f_chg(100 * (ex_m / ex_d - 1)), 3, "derived",
        esrc + "; mine / default - 1")

    # cumulative output: the patch does not produce tokens faster
    for bar, tag in (("default", "default"), ("mine", "mine_a1.0")):
        for pct in (50, 90, 99):
            v = med([cum[f"reasoning_q2__{tag}_r{i}"][pct] for i in (1, 2, 3)])
            put(f"q2.cum.{bar}_t{pct}_s", v, f_s(v), 3, "median",
                f"data/derived/cum_output.csv.gz, reasoning_q2__{tag}_r1..r3: median time to {pct}% of the output tokens")

    for pct in (50, 90):
        dv = NUM[f"q2.cum.mine_t{pct}_s"]["value"] - NUM[f"q2.cum.default_t{pct}_s"]["value"]
        put(f"q2.cum.t{pct}_mine_minus_default_s", dv, f_s(dv), 3, "derived",
            f"q2.cum.mine_t{pct}_s - q2.cum.default_t{pct}_s (positive = the patched server reaches {pct}% later)")

    # how much of the gain the existing flags reproduce (n=1 each)
    for bar in ("noradix", "tuned_n40"):
        v = 100 * (med3[bar]["goodput"] - dm["goodput"]) / (mm["goodput"] - dm["goodput"])
        put(f"q2.share_of_gain.{bar}_pct", v, f_pct(v), 1, "derived",
            f"(q2.{bar}.goodput - q2.default.goodput) / (q2.mine.goodput - q2.default.goodput); {bar} n=1, others n=3")

    # TTFT has two modes under the gate
    lt1 = [sum(1 for x in r.col("ttft_s") if x < 1) for r in m_runs]
    ge12 = [sum(1 for x in r.col("ttft_s") if x >= 12) for r in m_runs]
    put("q2.ttft_bimodal.mine_lt1s", int(med(lt1)), f_count(med(lt1)), 3, "median",
        f"{P}, reasoning_q2__mine_a1.0_r1..r3: requests with TTFT < 1 s")
    put("q2.ttft_bimodal.mine_ge12s", int(med(ge12)), f_count(med(ge12)), 3, "median",
        f"{P}, reasoning_q2__mine_a1.0_r1..r3: requests with TTFT >= 12 s")
    d1 = [sum(1 for x in r.col("ttft_s") if x < 1) for r in d_runs]
    put("q2.ttft_bimodal.default_lt1s", int(med(d1)), f_count(med(d1)), 3, "median",
        f"{P}, reasoning_q2__default_r1..r3: requests with TTFT < 1 s")
    if sum(1 for x in m_runs[0].col("ttft_s") if 1 <= x < 12) != 0:
        raise SystemExit("mine TTFT is not bimodal")
    o60 = [sorted(r.col("ttft_s"))[59] for r in m_runs]
    o61 = [sorted(r.col("ttft_s"))[60] for r in m_runs]
    put("q2.ttft_bimodal.mine_60th_range_s", rng_value(min(o60), max(o60)), f_range(min(o60), max(o60), f_s), 3, "range",
        f"{P}, reasoning_q2__mine_a1.0_r1..r3: 60th smallest ttft_s (the p50 interpolates between the 60th and 61st)")
    put("q2.ttft_bimodal.mine_61st_range_s", rng_value(min(o61), max(o61)), f_range(min(o61), max(o61), f_s), 3, "range",
        f"{P}, reasoning_q2__mine_a1.0_r1..r3: 61st smallest ttft_s")

    # short requests (output < 1,000 tokens) wait behind long ones
    for bar, runs_ in (("default", d_runs), ("mine", m_runs)):
        short = [[r_ for r_ in r.ok if int(r_["completion_tokens"]) < SHORT_OUT] for r in runs_]
        v = med([percentile([float(x["ttft_s"]) for x in s_], 50) for s_ in short])
        put(f"q2.short_ttft_p50.{bar}_s", v, f_s(v), 3, "median",
            f"{P}, reasoning_q2__{Q2_3BAR[bar]}_r1..r3: median over runs of the p50 of ttft_s of requests with "
            f"< {SHORT_OUT:,} output tokens")
    nshort = len([r_ for r_ in d_runs[0].ok if int(r_["completion_tokens"]) < SHORT_OUT])
    put("q2.short_ttft_p50.n_requests", nshort, f_count(nshort), None, "count",
        f"{P}: requests with < {SHORT_OUT:,} output tokens")

    # hero figure caption (q2, r2 of default and mine)
    hd, hm = R("reasoning_q2__default_r2"), R("reasoning_q2__mine_a1.0_r2")
    put("hero.run_default", hd.name, hd.name, 1, "single", "run shown in docs/figures/hero_timeline.png (default)")
    put("hero.run_mine", hm.name, hm.name, 1, "single", "run shown in docs/figures/hero_timeline.png (mine)")
    hs = [float(x["max_gap_s"]) for x in hd.stalled()]
    put("hero.default.stalled", len(hs), f_count(len(hs)), 1, "single", f"{P}, {hd.name}: requests with a gap > 5 s")
    put("hero.default.stall_range_s", rng_value(min(hs), max(hs)), f_range(min(hs), max(hs), f_s), 1, "range",
        f"{P}, {hd.name}: min-max max_gap_s of those requests")
    put("hero.mine.stalled", len(hm.stalled()), f_count(len(hm.stalled())), 1, "single",
        f"{P}, {hm.name}: requests with a gap > 5 s")
    for key, rr in (("default", hd), ("mine", hm)):
        w10 = sum(1 for x in rr.col("ttft_s") if x > 10)
        put(f"hero.{key}.wait10", w10, f_count(w10), 1, "single", f"{P}, {rr.name}: requests with TTFT > 10 s")
    return med3


LOAD = {"q0.35": "reasoning", "q0.5": "reasoning_q0.5", "q1": "reasoning_q1", "q2": "reasoning_q2",
        "q4": "reasoning_q4"}
LOAD_BARS = {"default": "default", "ablated": "ablated", "mine": "mine_a1.0", "tuned_n40": "tuned_n40"}


def load_curve(runs, R, med3):
    W = "data/derived/w3_runs.csv"
    vals = {}
    for q, stem in LOAD.items():
        for bar, tag in LOAD_BARS.items():
            if q == "q2" and bar in med3:
                v, n, agg, src = med3[bar]["goodput"], (3 if bar != "tuned_n40" else 1), \
                    ("median" if bar != "tuned_n40" else "single"), f"q2.{bar}.goodput"
            elif q == "q0.35" and bar == "default":
                gp = [runs[f"reasoning__upstream_r{i}"]["goodput_rps"] for i in (1, 2, 3)]
                v, n, agg = med(gp), 3, "median"
                src = f"{W}, reasoning__upstream_r1..r3: median goodput_rps (unpatched; no flag-off run at qps 0.35)"
            else:
                name = f"{stem}__{tag}_r1"
                if name not in runs:
                    continue
                v, n, agg, src = runs[name]["goodput_rps"], 1, "single", f"{W}, {name}: goodput_rps"
            vals[(q, bar)] = v
            put(f"load.{q}.{bar}.goodput", v, f_goodput(v), n, agg, src)
            name = f"{stem}__{tag}_r1"
            if q in ("q0.5", "q1", "q4") and name in runs:
                r = runs[name]
                for key, c, f, sc in (("retractions", "retracted", f_count, 1), ("ttft_mean_s", "ttft_mean", f_s, 1e-3),
                                      ("ttft_p50_s", "ttft_p50", f_s, 1e-3), ("ttft_p99_s", "ttft_p99", f_s, 1e-3),
                                      ("wait10", "wait10_reqs", f_count, 1)):
                    v2 = r[c] * sc
                    put(f"load.{q}.{key}.{bar}", int(v2) if f is f_count else v2, f(v2), 1, "single",
                        f"{W}, {name}: {c}" + (" / 1000" if sc != 1 else ""))
                rr = R(name)
                put(f"load.{q}.slo_met.{bar}", rr.met(), f_count(rr.met()), 1, "single",
                    f"data/derived/per_request.csv.gz, {name}: count of slo_ok = 1")
                put(f"load.{q}.wall_s.{bar}", rr.span(), f_s(rr.span()), 1, "single",
                    f"data/derived/per_request.csv.gz, {name}: span")
    for q in ("q0.5", "q1", "q4"):
        v = 100 * (vals[(q, "mine")] / vals[(q, "default")] - 1)
        put(f"load.{q}.gain_pct", v, f_chg(v), 1, "derived", f"load.{q}.mine.goodput / load.{q}.default.goodput - 1 (n=1)")
    v = 100 * (vals[("q1", "ablated")] / vals[("q1", "default")] - 1)
    put("load.q1.ablated_vs_default_pct", v, f_chg(v), 1, "derived", "load.q1.ablated.goodput / load.q1.default.goodput - 1")
    for q in ("q1", "q4"):
        for key in ("ttft_mean_s", "ttft_p99_s"):
            a, b = NUM[f"load.{q}.{key}.default"]["value"], NUM[f"load.{q}.{key}.mine"]["value"]
            v = 100 * (b / a - 1)
            put(f"load.{q}.{key[:-2]}_pct", v, f_chg(v), 1, "derived", f"load.{q}.{key}.mine / default - 1")
    d4, m4 = R("reasoning_q4__default_r1"), R("reasoning_q4__mine_a1.0_r1")
    rid = NUM["q2.longest.rid"]["value"]
    v = 100 * (m4.goodput(drop={rid}) / d4.goodput(drop={rid}) - 1)
    put("load.q4.excl_longest_gain_pct", v, f_chg(v), 1, "derived",
        f"data/derived/per_request.csv.gz, reasoning_q4__{{mine_a1.0,default}}_r1: count(slo_ok)/span without {rid}")
    put("load.q4.slo_factor", m4.met() / d4.met(), f_factor(m4.met() / d4.met()), 1, "derived",
        "load.q4.slo_met.mine / load.q4.slo_met.default")
    put("load.q4.wall_factor", d4.span() / m4.span(), f_factor(d4.span() / m4.span()), 1, "derived",
        "load.q4.wall_s.default / load.q4.wall_s.mine")
    d1, m1 = R("reasoning_q1__default_r1"), R("reasoning_q1__mine_a1.0_r1")
    last = max(d1.ok, key=lambda r: float(r["end_t"]))["rid"]
    if max(m1.ok, key=lambda r: float(r["end_t"]))["rid"] != last:
        raise SystemExit("q1: a different request finishes last")
    put("load.q1.last_rid", last, last, 1, "single", "data/derived/per_request.csv.gz, reasoning_q1__default_r1: last request to finish")
    for key, rr in (("default", d1), ("mine", m1)):
        v = float(rr.by_rid()[last]["ttft_s"])
        put(f"load.q1.last_ttft_s.{key}", v, f_s(v), 1, "single",
            f"data/derived/per_request.csv.gz, {rr.name}: ttft_s of {last}")


def harm_and_retries(runs, R, logs):
    W = "data/derived/w3_runs.csv"
    for cond in ("s0.3", "s1.0", "m1000"):
        for bar, tag in (("default", "default"), ("mine", "mine_a1.0"), ("tuned_n40", "tuned_n40")):
            name = f"reasoning_q2_{cond}__{tag}_r1"
            r = runs[name]
            p = f"harm.{cond}.{bar}."
            put(p + "goodput", r["goodput_rps"], f_goodput(r["goodput_rps"]), 1, "single", f"{W}, {name}: goodput_rps")
            put(p + "retractions", int(r["retracted"]), f_count(r["retracted"]), 1, "single", f"{W}, {name}: retracted")
            for key, c in (("ttft_mean_s", "ttft_mean"), ("ttft_p50_s", "ttft_p50"), ("ttft_p99_s", "ttft_p99")):
                put(p + key, r[c] / 1000, f_s(r[c] / 1000), 1, "single", f"{W}, {name}: {c} / 1000")
            put(p + "wait10", int(r["wait10_reqs"]), f_count(r["wait10_reqs"]), 1, "single", f"{W}, {name}: wait10_reqs")
            put(p + "slo_met", R(name).met(), f_count(R(name).met()), 1, "single",
                f"data/derived/per_request.csv.gz, {name}: count of slo_ok = 1")
        a, b = NUM[f"harm.{cond}.default.goodput"]["value"], NUM[f"harm.{cond}.mine.goodput"]["value"]
        put(f"harm.{cond}.gain_pct", 100 * (b / a - 1), f_chg(100 * (b / a - 1)), 1, "derived",
            f"harm.{cond}.mine.goodput / harm.{cond}.default.goodput - 1")
    for cond, d_ in (("q1", "load.q1"), ("q4", "load.q4"), ("s0.3", "harm.s0.3"), ("s1.0", "harm.s1.0"),
                     ("m1000", "harm.m1000"), ("q2", "q2")):
        mk = f"{d_}.mine.goodput" if d_ != "q2" else "q2.mine.goodput"
        nk = f"{d_}.tuned_n40.goodput" if d_ != "q2" else "q2.tuned_n40.goodput"
        a, b = NUM[nk]["value"], NUM[mk]["value"]
        v = 100 * (b / a - 1)
        put(f"compare.{cond}.mine_vs_tuned_n40_pct", v, f_chg(v, 2), 1, "derived",
            f"{mk} / {nk} - 1, 2 decimals (tuned_n40 n=1; q2 mine is a 3-run median)")
    for key, name in (("q1", "reasoning_q1__tuned_n40_r1"), ("q2", "reasoning_q2__tuned_n40_r1"),
                      ("q4", "reasoning_q4__tuned_n40_r1"), ("s0.3", "reasoning_q2_s0.3__tuned_n40_r1"),
                      ("s1.0", "reasoning_q2_s1.0__tuned_n40_r1"), ("m1000", "reasoning_q2_m1000__tuned_n40_r1")):
        v = int(runs[name]["retracted"])
        put(f"reretract.tuned_n40.{key}", v, f_count(v), 1, "single",
            f"{W}, {name}: retracted (--max-running-requests 40 tuned at q2)")
    mine_logs = [k for k in logs if "__mine_a1.0" in k or k.endswith("server_cross_mine.log")]
    clean = sum(1 for k in mine_logs if logs[k]["retracted_reqs"] == 0)
    put("reretract.mine_a1.0.logs_without_retraction", f"{clean}/{len(mine_logs)}", f_ratio(clean, len(mine_logs)), 1,
        "count", "data/derived/server_logs.csv: server logs of alpha=1.0 runs (all loads and conditions, the "
        "deterministic run, the page-size 16 smoke run and the cross replay) with retracted_reqs = 0 / all")


def slo_ttft10(R):
    P = "data/derived/per_request.csv.gz"
    note = (f"POST-HOC: count(slo_ok and TTFT <= {TTFT_SLO_POSTHOC_S:g} s) / span; the trace has no TTFT SLO, "
            "this hypothetical SLO was added after the results were seen")
    for key, tag, reps in (("q2.default", "reasoning_q2__default", 3), ("q2.mine", "reasoning_q2__mine_a1.0", 3),
                           ("q2.noradix", "reasoning_q2__noradix", 1), ("q2.tuned_n40", "reasoning_q2__tuned_n40", 1),
                           ("q4.default", "reasoning_q4__default", 1), ("q4.mine", "reasoning_q4__mine_a1.0", 1),
                           ("q1.default", "reasoning_q1__default", 1), ("q1.mine", "reasoning_q1__mine_a1.0", 1)):
        vals = [R(f"{tag}_r{i}").goodput(ttft_max=TTFT_SLO_POSTHOC_S) for i in range(1, reps + 1)]
        put(f"slo_ttft10.{key}", med(vals), f_goodput(med(vals)), reps, "median" if reps > 1 else "single",
            f"{P}, {tag}_r1..r{reps}: {note}")
        if reps > 1:
            put(f"slo_ttft10.{key}_range", rng_value(min(vals), max(vals)), f_range(min(vals), max(vals), f_goodput),
                reps, "range", f"{P}, {tag}_r1..r{reps}: min-max, {note}")


def verify_counts():
    """{(ref run, test run): (exact, total)} parsed from data/verify/*.txt (bench.verify output)."""
    out = {}
    for p in sorted(glob.glob("data/verify/*.txt")):
        with open(p, encoding="utf-8") as fh:
            text = fh.read()
        m = re.search(r"python -m bench\.verify (\S+) (\S+)", text)
        n = re.search(r"비교 대상\s*:\s*(\d+)건", text)
        e = re.search(r"완전 일치\s*:\s*(\d+)건", text)
        run = lambda s: os.path.splitext(os.path.relpath(s, "results"))[0]
        out[(run(m.group(1)), run(m.group(2)))] = (int(e.group(1)), int(n.group(1)))
    return out


def correctness(R):
    P = "data/derived/per_request.csv.gz"
    vc = verify_counts()

    def compare(a, b):
        A, B = R(a).by_rid(), R(b).by_rid()
        if set(A) != set(B) or any(not A[k]["output_sha16"] or not B[k]["output_sha16"] for k in A):
            raise SystemExit(f"cannot compare outputs of {a} and {b}")
        same = sum(1 for k in A if A[k]["output_sha16"] == B[k]["output_sha16"])
        if (a, b) in vc and vc[(a, b)] != (same, len(A)):
            raise SystemExit(f"{a} vs {b}: {same}/{len(A)} identical outputs, data/verify says {vc[(a, b)]}")
        mism = {k for k in A if A[k]["output_sha16"] != B[k]["output_sha16"]}
        vict = {k for k in A if int(A[k]["n_gaps_gt5s"]) > 0} | {k for k in B if int(B[k]["n_gaps_gt5s"]) > 0}
        return same, len(A), mism, vict

    def src(a, b):
        f = f"data/verify/{'det' if a.startswith('det/') else 'results'}__{a.split('/')[-1]}__VS__{b.split('/')[-1]}.txt"
        return (f"{P}: requests of {a} and {b} with identical output_sha16"
                + (f" (equals {f})" if os.path.exists(f) else ""))

    pairs = [("det_q02up_vs_q2mine", "det/reasoning_q0.2__upstream_r1", "det/reasoning_q2__mine_a1.0_r1"),
             ("det_q2up_r1_vs_r2", "det/reasoning_q2__upstream_r1", "det/reasoning_q2__upstream_r2"),
             ("det_q2up_vs_q2mine", "det/reasoning_q2__upstream_r1", "det/reasoning_q2__mine_a1.0_r1"),
             ("det_q02up_vs_q02off", "det/reasoning_q0.2__upstream_r1", "det/reasoning_q0.2__default_off_r1"),
             ("det_q02up_vs_q2up", "det/reasoning_q0.2__upstream_r1", "det/reasoning_q2__upstream_r1"),
             ("det_q2up_vs_q2off", "det/reasoning_q2__upstream_r1", "det/reasoning_q2__default_off_r1"),
             ("nondet_q2default_r1_vs_r2", "reasoning_q2__default_r1", "reasoning_q2__default_r2"),
             ("nondet_q2mine_r1_vs_r2", "reasoning_q2__mine_a1.0_r1", "reasoning_q2__mine_a1.0_r2"),
             ("nondet_q2up_vs_q2default", "reasoning_q2__upstream_r1", "reasoning_q2__default_r1")]
    res = {}
    for key, a, b in pairs:
        same, tot, mism, vict = compare(a, b)
        res[key] = (same, tot, mism, vict)
        n = 1
        put(f"correct.{key}", f"{same}/{tot}", f_ratio(same, tot), n, "count", src(a, b))
    same, tot, mism, vict = res["det_q2up_vs_q2mine"]
    put("correct.det_q2up_vs_q2mine_pct", 100 * same / tot, f_pct(100 * same / tot), 1, "derived",
        "correct.det_q2up_vs_q2mine as a percentage (bench.verify passes at 95%)")
    inv = len(mism & vict)
    put("correct.mismatches_in_victims", f"{inv}/{len(mism)}", f_ratio(inv, len(mism)), 1, "count",
        f"{P}: outputs that differ between det/reasoning_q2__upstream_r1 and det/reasoning_q2__mine_a1.0_r1 "
        "that belong to requests with an in-stream gap > 5 s (retraction victims) in either run / all differing outputs")
    nv = sum(1 for r in R("det/reasoning_q2__upstream_r1").ok if int(r["n_gaps_gt5s"]) > 0)
    put("correct.det_q2up_victims", nv, f_count(nv), 1, "count",
        f"{P}, det/reasoning_q2__upstream_r1: requests with a gap > 5 s")
    same, tot, mism, vict = res["det_q2up_r1_vs_r2"]
    put("correct.det_q2up_r1_vs_r2_mismatches_in_victims", f"{len(mism & vict)}/{len(mism)}",
        f_ratio(len(mism & vict), len(mism)), 1, "count",
        f"{P}: differing outputs of det q2 upstream r1 vs r2 that belong to retraction victims / all differing")
    same, tot, _, _ = res["nondet_q2default_r1_vs_r2"]
    put("correct.nondet_q2default_r1_vs_r2_pct", 100 * same / tot, f_pct(100 * same / tot), 1, "derived",
        "correct.nondet_q2default_r1_vs_r2 as a percentage")


def cross(R, logs):
    P = "data/derived/per_request.csv.gz"
    ch = {}
    for w in ("rag", "agent", "reasoning", "structured", "mixed"):
        d, m = R(f"cross/{w}_x__default"), R(f"cross/{w}_x__mine")
        v = 100 * (m.goodput() / d.goodput() - 1)
        ch[w] = v
        put(f"cross.{w}.goodput_pct", v, f_chg(v, 2), 1, "derived",
            f"{P}, cross/{w}_x__{{default,mine}}: goodput (count(slo_ok)/span) mine / default - 1, 2 decimals")
        put(f"cross.{w}.n_requests", len(d.rows), f_count(len(d.rows)), None, "count", f"{P}, cross/{w}_x__default: rows")
        outmax = max(int(r["completion_tokens"]) for r in d.ok)
        put(f"cross.{w}.out_max", outmax, f_count(outmax), None, "derived",
            f"{P}, cross/{w}_x__default: largest completion_tokens (= max_new_tokens, ignore_eos)")
        ttft_slo = {r["ttft_slo_ms"] for r in d.rows if r["ttft_slo_ms"]}
        if len(ttft_slo) == 1:
            v2 = float(ttft_slo.pop()) / 1000
            put(f"cross.{w}.ttft_slo_s", v2, f_s(v2), None, "static", f"{P}, cross/{w}_x__default: ttft_slo_ms / 1000")
    lo, hi = min(ch.values()), max(ch.values())
    put("cross.w3_row_min_pct", lo, f_chg(lo, 2), 1, "derived",
        "min over cross.<workload>.goodput_pct (the 'mine' row of the 5x5 cross replay, --scale 0.3), 2 decimals")
    put("cross.w3_row_max_pct", hi, f_chg(hi, 2), 1, "derived", "max over cross.<workload>.goodput_pct, 2 decimals")
    v = int(logs["logs/server_cross_mine.log"]["peak_kv_delays_logged"])
    put("cross.gate_fired", v, f_count(v), 1, "count",
        "data/derived/server_logs.csv, logs/server_cross_mine.log: last logged Peak-KV #delays over the whole "
        "cross replay (one server for all 5 workloads)")
    v = int(logs["logs/server_cross_default.log"]["retracted_reqs"])
    put("cross.default_retractions", v, f_count(v), 1, "count",
        "data/derived/server_logs.csv, logs/server_cross_default.log: retracted_reqs over the whole cross replay")


def budget_select_smoke_patch(runs):
    rows = []
    with open("w3/runs.tsv", encoding="utf-8") as fh:
        for line in fh:
            f = line.rstrip("\n").split("\t")
            rows.append(dict(status=f[3], startup=int(f[4].split("=")[1].rstrip("s")),
                             replay=int(f[5].split("=")[1].rstrip("s"))))
    src = "w3/runs.tsv (one line per w3/run_one.sh run on 09-13): "
    put("budget.runs", len(rows), f_count(len(rows)), None, "count", src + "lines")
    ok = sum(1 for r in rows if r["status"] == "OK")
    put("budget.runs_ok", ok, f_count(ok), None, "count", src + "status OK")
    put("budget.runs_err", len(rows) - ok, f_count(len(rows) - ok), None, "count", src + "status not OK (ERR1, re-run)")
    su, rp = sum(r["startup"] for r in rows), sum(r["replay"] for r in rows)
    put("budget.startup_s", su, f_count(su), None, "count", src + "sum of startup=")
    put("budget.replay_s", rp, f_count(rp), None, "count", src + "sum of replay=")
    put("budget.gpu_hours", (su + rp) / 3600, f"{(su + rp) / 3600:.2f}", None, "derived",
        src + "(startup + replay) / 3600, excluding the two 09-13 cross replays (about 18 min)")

    src_rows = rows_from_csv("data/derived/w3_runs.csv")
    a = select(src_rows, SPECS[0][1])[1]
    n = select(src_rows, SPECS[1][1])[1]
    put("select.alpha_star", a, a, 1, "derived",
        "w3.select_star on data/derived/w3_runs.csv: preregistered rule (no gap > 5 s, then lowest TTFT p99), "
        "q2 mine_a{0.5,0.6,0.75,0.9,1.0}_r1")
    put("select.n_star", int(n), n, 1, "derived",
        "w3.select_star on data/derived/w3_runs.csv: same rule over q2 tuned_n{24,32,40,48}_r1")

    name = "reasoning_q2__mine_a1.0_p16_r1"
    r = runs[name]
    s = f"data/derived/w3_runs.csv, {name} (--page-size 16 smoke run, n=1): "
    put("smoke.page16", f"{int(r['n_ok'])}/{int(r['n_ok'] + r['n_err'])}",
        f_ratio(int(r["n_ok"]), int(r["n_ok"] + r["n_err"])), 1, "count", s + "requests completed without error / all")
    put("smoke.page16.retractions", int(r["retracted"]), f_count(r["retracted"]), 1, "single", s + "retracted")
    put("smoke.page16.goodput", r["goodput_rps"], f_goodput(r["goodput_rps"]), 1, "single", s + "goodput_rps")
    put("smoke.page16.peak_delays", int(r["peak_delays"]), f_count(r["peak_delays"]), 1, "single",
        s + "peak_delays (lower bound)")
    put("smoke.page16.ttft_p99_s", r["ttft_p99"] / 1000, f_s(r["ttft_p99"] / 1000), 1, "single", s + "ttft_p99 / 1000")
    put("smoke.page16.cache_hit_pct", r["cache_hit_%"], f_pct(r["cache_hit_%"]), 1, "single", s + "cache_hit_%")

    path = "patch/peak_kv_reservation.diff"
    with open(path, "rb") as fh:
        raw = fh.read()
    files, per_file, cur = 0, {}, None
    added = removed = 0
    for line in raw.decode("utf-8").splitlines():
        if line.startswith("+++ "):
            files += 1
            cur = line[4:].split("/")[-1]
            per_file[cur] = 0
        elif line.startswith("+") and not line.startswith("+++"):
            added += 1
            per_file[cur] += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    put("patch.files", files, f_count(files), None, "count", path + ": files changed")
    put("patch.lines_added", added, f_count(added), None, "count", path + ": added lines")
    put("patch.lines_removed", removed, f_count(removed), None, "count", path + ": removed lines")
    for f_, v in sorted(per_file.items()):
        put(f"patch.lines_added.{f_.replace('.py', '')}", v, f_count(v), None, "count", path + f": added lines in {f_}")
    sha = hashlib.sha256(raw).hexdigest()
    put("patch.sha256", sha, sha, None, "static", path + ": sha256 of the measured diff")


# ----------------------------------------------------------------------------- main
def build() -> dict:
    runs, det, sweep, logs, pr, cum, rc_sum, rc_ev = load()
    cache = {}

    def R(name):
        if name not in cache:
            cache[name] = Run(name, pr.get(name, []))
        return cache[name]
    R.names = lambda: sorted(pr)

    NUM.clear()
    pool, out = trace_and_kv(R, logs)
    sweep_0907(sweep, rc_sum, rc_ev, R, pool, out, logs)
    baseline(runs)
    med3 = q2_bars(runs, R, cum)
    load_curve(runs, R, med3)
    harm_and_retries(runs, R, logs)
    slo_ttft10(R)
    correctness(R)
    cross(R, logs)
    budget_select_smoke_patch(runs)
    det_ret = {n: int(r["retracted"]) for n, r in det.items()}
    for n, v in sorted(det_ret.items()):
        put(f"det.retractions.{n.split('/')[-1]}", v, f_count(v), 1, "single",
            f"data/derived/w3_det_runs.csv, {n}: retracted (deterministic mode)")
    meta = {"_about": "Generated by python3 -m analysis.headline from files in git; see the module docstring. "
                      "Do not edit by hand."}
    return {**meta, **{k: NUM[k] for k in sorted(NUM)}}


def dump(data: dict) -> str:
    return json.dumps(data, indent=1, sort_keys=True, ensure_ascii=False) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description="compute artifacts/numbers.json from the derived data in git")
    ap.add_argument("--out", default=OUT, help="default: %(default)s")
    ap.add_argument("--check", action="store_true", help="compare with the existing file instead of writing it")
    args = ap.parse_args()
    text = dump(build())
    if args.check:
        try:
            with open(args.out, encoding="utf-8") as fh:
                old = fh.read()
        except FileNotFoundError:
            sys.exit(f"{args.out} does not exist")
        if old != text:
            a, b = json.loads(old), json.loads(text)
            diff = sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))
            sys.exit(f"{args.out} is out of date ({len(diff)} keys differ, e.g. {', '.join(diff[:8])})")
        print(f"{args.out} is up to date ({len(json.loads(text)) - 1} keys)")
        return
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    print(f"wrote {args.out}: {len(json.loads(text)) - 1} keys")


if __name__ == "__main__":
    main()
