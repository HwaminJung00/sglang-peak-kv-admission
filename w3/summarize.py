"""
W3 result summary: client result JSON + server log + /metrics dump in one table.
Needs no GPU. The client-side metrics come from w3.metrics (summarize_run, request_metrics).

    python3 -m w3.summarize 'results/reasoning_q2__*.json'              # one row per run
    python3 -m w3.summarize 'results/reasoning_q2__*.json' --group       # repetitions grouped: median [min-max]
    python3 -m w3.summarize 'results/reasoning*__*_r*.json' --csv data/derived/w3_runs.csv

Regenerating the derived tables in data/derived/ (run from the repository root, raw data in results/ and logs/):

    python3 -m w3.summarize 'results/reasoning*__*_r*.json' --csv data/derived/w3_runs.csv
    python3 -m w3.summarize 'results/det/*.json' --csv data/derived/w3_det_runs.csv
    python3 -m w3.summarize 'results/reasoning_q2__*_r*.json' --group --csv data/derived/w3_q2_grouped.csv
    python3 -m w3.summarize 'results/reasoning_q2__default_r*.json' 'results/reasoning_q2__ablated_r*.json' \\
        'results/reasoning_q2__mine_a1.0_r*.json' --std-csv data/derived/reasoning_q2_3bar_runs.csv
    python3 -m w3.summarize 'results/reasoning_q*__default.json' --sweep-csv data/derived/sweep_0907.csv

Extra columns (beyond the standard summary of w3.metrics):
  e2e_p50         median E2E
  ttft_mean       mean TTFT (at q >= 2 the TTFT distribution has two modes, admitted at once
                  vs. waiting 30 s or more, so its p50 is unstable at the boundary)
  wait10_reqs     requests whose TTFT exceeded 10 s (waited for admission in the queue)
  stall5_reqs     requests with a gap of more than 5 s between two streamed tokens
                  (violations of the auxiliary SLO)
  stall_max_s     longest such gap (s)
  aux_slo_%       share of requests with TPOT <= SLO and no gap > 5 s (auxiliary metric fixed
                  before the results were seen)
  retracted       sum of `#retracted_reqs` in the server log (a request can be counted twice)
  max_usage       largest `token usage` on Decode/Prefill lines
  max_running     largest #running-req
  max_queue       largest #queue-req
  graph_%         share of Decode lines with `cuda graph: True` (one line per 40 steps, a sample)
  peak_delays     [mine] last cumulative #delays of `Peak-KV reservation delayed admission`
                  (logged at most every 5 s, so a lower bound)
  recompute_tok   /metrics: prefill_compute - input effective tokens (KV recomputed for
                  retracted requests)

Server logs are found by the w3/run_one.sh naming rule, logs/<sub>/server_<trace>__<tag>.log.
The 09-07 load sweep (results/reasoning_q<Q>__default.json) predates that script; its logs are
logs/server_q<Q>.log.
"""

from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import re
import statistics
import sys

from w3.metrics import STD_COLS, percentile, request_metrics, summarize_run

STALL_S = 5.0

RE_RETRACT = re.compile(r"#retracted_reqs: (\d+)")
RE_USAGE = re.compile(r"token usage: ([\d.]+)")
RE_RUN = re.compile(r"#running-req: (\d+)")
RE_QUEUE = re.compile(r"#queue-req: (\d+)")
RE_PEAK = re.compile(r"Peak-KV reservation delayed admission\. #delays: (\d+)")
RE_POOL = re.compile(r"max_total_num_tokens=(\d+)")
RE_METRIC = re.compile(r'^(sglang:[a-z_]+)\{([^}]*)\} ([0-9.eE+-]+)$')
# 09-07 load sweep result names (one run per QPS, no repetition suffix)
RE_SWEEP_0907 = re.compile(r"^reasoning_q([\d.]+)__default$")


def log_path_for(res_path: str) -> str:
    """results/det/x__t.json -> logs/det/server_x__t.log

    Fallback for the 09-07 sweep: results/reasoning_q<Q>__default.json -> logs/server_q<Q>.log
    when the run_one.sh name does not exist.
    """
    d, f = os.path.split(res_path)
    sub = os.path.relpath(d, "results")
    sub = "" if sub in (".", "") else sub
    name = os.path.splitext(f)[0]
    path = os.path.join("logs", sub, f"server_{name}.log")
    m = RE_SWEEP_0907.match(name)
    if not sub and m and not os.path.exists(path):
        alt = os.path.join("logs", f"server_q{m.group(1)}.log")
        if os.path.exists(alt):
            return alt
    return path


def parse_server_log(path: str) -> dict:
    out = {"log": os.path.exists(path)}
    if not out["log"]:
        return out
    retracted = events = 0
    usage = running = queue = 0.0
    dec = graph = 0
    run_sum = 0
    peak = 0
    pool = None
    with open(path, errors="ignore") as f:
        for line in f:
            if "Retract requests" in line:
                m = RE_RETRACT.search(line)
                if m:
                    retracted += int(m.group(1))
                    events += 1
            elif "Decode batch" in line or "Prefill batch" in line:
                m = RE_USAGE.search(line)
                if m:
                    usage = max(usage, float(m.group(1)))
                m = RE_RUN.search(line)
                if m:
                    running = max(running, int(m.group(1)))
                    if "Decode batch" in line:
                        run_sum += int(m.group(1))
                m = RE_QUEUE.search(line)
                if m:
                    queue = max(queue, int(m.group(1)))
                if "Decode batch" in line:
                    dec += 1
                    graph += "cuda graph: True" in line
            elif "Peak-KV reservation" in line:
                m = RE_PEAK.search(line)
                if m:
                    peak = max(peak, int(m.group(1)))
            elif pool is None and "max_total_num_tokens=" in line:
                m = RE_POOL.search(line)
                if m:
                    pool = int(m.group(1))
    out.update(retracted=retracted, retract_events=events, max_usage=usage,
               max_running=int(running), max_queue=int(queue),
               graph_pct=100.0 * graph / dec if dec else float("nan"),
               mean_running=run_sum / dec if dec else float("nan"),
               peak_delays=peak, kv_pool=pool)
    return out


def parse_metrics(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    vals: dict[str, float] = {}
    for line in open(path, errors="ignore"):
        m = RE_METRIC.match(line.strip())
        if not m:
            continue
        name, labels, v = m.groups()
        mode = re.search(r'mode="([^"]+)"', labels)
        key = f"{name}[{mode.group(1)}]" if mode else name
        vals[key] = vals.get(key, 0.0) + float(v)
    out = {}
    pc = vals.get("sglang:realtime_tokens_total[prefill_compute]")
    pi = vals.get("sglang:prefill_effective_tokens_total[input]")
    if pc is not None and pi is not None:
        out["recompute_tok"] = pc - pi
    if "sglang:num_retracted_requests_total" in vals:
        out["retracted_metric"] = vals["sglang:num_retracted_requests_total"]
    if "sglang:num_retracted_output_tokens_total" in vals:
        out["retracted_out_tok"] = vals["sglang:num_retracted_output_tokens_total"]
    return out


def client_extra(blob: dict) -> dict:
    rows = [request_metrics(r) for r in blob["records"]]
    good = [r for r in rows if not r["error"] and "ttft_ms" in r]
    stalls, aux_ok = [], 0
    for rec, row in zip(blob["records"], rows):
        if row["error"] or "ttft_ms" not in row:
            continue
        ct = rec["chunk_times"]
        gmax = max((b - a for a, b in zip(ct, ct[1:])), default=0.0)
        stalls.append(gmax)
        aux_ok += bool(row.get("slo_ok")) and gmax <= STALL_S
    return {
        "e2e_p50": percentile([r["e2e_ms"] for r in good], 50),
        "ttft_mean": statistics.fmean([r["ttft_ms"] for r in good]) if good else float("nan"),
        "wait10_reqs": sum(1 for r in good if r["ttft_ms"] > 10_000),
        "stall5_reqs": sum(1 for g in stalls if g > STALL_S),
        "stall_max_s": max(stalls, default=0.0),
        "aux_slo_%": 100.0 * aux_ok / len(good) if good else float("nan"),
    }


def run_row(path: str) -> dict:
    with open(path) as fh:
        blob = json.load(fh)
    s = summarize_run(blob)
    name = os.path.splitext(os.path.basename(path))[0]
    stem, _, tag = name.partition("__")
    s["std_tag"] = s["tag"]          # tag as stored in the result file (standard table)
    s.update(file=path, trace=stem, tag=tag,
             dataset=os.path.relpath(os.path.dirname(path), "results"),
             qps=(blob.get("meta") or {}).get("qps"))
    s.update(client_extra(blob))
    lp = log_path_for(path)
    s["server_log"] = lp if os.path.exists(lp) else ""
    s.update(parse_server_log(lp))
    s.update(parse_metrics(lp.replace("server_", "metrics_").replace(".log", ".txt")))
    return s


def cond_of(row: dict) -> str:
    tag = re.sub(r"_r\d+$", "", row["tag"])
    ds = "" if row["dataset"] in (".", "") else row["dataset"] + "/"
    return f"{ds}{row['trace']}__{tag}"


COLS = ["n_ok", "n_err", "wall_s", "ttft_p50", "ttft_p99", "ttft_mean", "wait10_reqs",
        "tpot_p50", "tpot_p99",
        "maxitl_p99", "e2e_p50", "e2e_p99", "out_tok_s", "cache_hit_%", "slo_%",
        "goodput_rps", "aux_slo_%", "stall5_reqs", "stall_max_s", "retracted",
        "max_usage", "max_running", "max_queue", "graph_%", "peak_delays",
        "recompute_tok"]
ALIAS = {"graph_%": "graph_pct"}


def get(r, c):
    return r.get(ALIAS.get(c, c), float("nan"))


def fmt(v, c) -> str:
    if isinstance(v, str):
        return v
    if v is None or (isinstance(v, float) and v != v):
        return "—"
    if c in ("goodput_rps", "max_usage"):
        return f"{v:.3f}"
    if c in ("n_ok", "n_err", "retracted", "max_running", "max_queue", "wait10_reqs",
             "stall5_reqs", "peak_delays", "recompute_tok"):
        return f"{v:.0f}"
    return f"{v:.1f}"


def group(rows: list[dict]) -> list[dict]:
    by: dict[str, list[dict]] = {}
    for r in rows:
        by.setdefault(cond_of(r), []).append(r)
    out = []
    for k, rs in by.items():
        g = {"cond": k, "reps": len(rs)}
        for c in COLS:
            vs = [get(r, c) for r in rs]
            vs = [v for v in vs if isinstance(v, (int, float)) and v == v]
            if not vs:
                g[c] = float("nan")
                continue
            g[c] = statistics.median(vs)
            g[c + "_min"], g[c + "_max"] = min(vs), max(vs)
        out.append(g)
    return out


def print_table(rows: list[dict], key: str, cols: list[str], grouped: bool) -> None:
    head = [key] + (["reps"] if grouped else []) + cols
    lines = []
    for r in rows:
        cells = [r[key]] + ([str(r["reps"])] if grouped else [])
        for c in cols:
            v = r.get(c, get(r, c))
            s = fmt(v, c)
            if grouped and r.get("reps", 1) > 1 and (c + "_min") in r and c in (
                    "goodput_rps", "ttft_p99", "maxitl_p99", "retracted", "out_tok_s"):
                s += f" [{fmt(r[c + '_min'], c)}–{fmt(r[c + '_max'], c)}]"
            cells.append(s)
        lines.append(cells)
    w = [max(len(h), *(len(l[i]) for l in lines)) for i, h in enumerate(head)]
    print(" | ".join(h.rjust(w[i]) for i, h in enumerate(head)))
    print("-+-".join("-" * x for x in w))
    for l in lines:
        print(" | ".join(c.rjust(w[i]) for i, c in enumerate(l)))


def write_std_csv(path: str, rows: list[dict]) -> None:
    """Standard per-run summary (STD_COLS) in the order the input patterns were given."""
    with open(path, "w", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=STD_COLS, extrasaction="ignore")
        wr.writeheader()
        for r in rows:
            wr.writerow({**{k: r.get(k, "") for k in STD_COLS}, "tag": r["std_tag"]})


# Load-sweep table: (column, source key, scale, format). Times in explicit units.
SWEEP_COLS = [
    ("qps", "qps", None, "{:g}"),
    ("n_ok", "n_ok", None, "{:d}"),
    ("wall_s", "wall_s", None, "{:.1f}"),
    ("goodput_rps", "goodput_rps", 1.0, "{:.6f}"),
    ("slo_pct", "slo_%", 1.0, "{:.4f}"),
    ("ttft_p50_s", "ttft_p50", 1e-3, "{:.6f}"),
    ("ttft_p99_s", "ttft_p99", 1e-3, "{:.6f}"),
    ("tpot_p50_ms", "tpot_p50", 1.0, "{:.4f}"),
    ("tpot_p99_ms", "tpot_p99", 1.0, "{:.4f}"),
    ("maxitl_p99_s", "maxitl_p99", 1e-3, "{:.6f}"),
    ("retractions", "retracted", None, "{:d}"),
    ("max_token_usage", "max_usage", 1.0, "{:.2f}"),
    ("out_tok_s", "out_tok_s", 1.0, "{:.4f}"),
    ("wait10_reqs", "wait10_reqs", None, "{:d}"),
    ("stall5_reqs", "stall5_reqs", None, "{:d}"),
    ("results_file", "file", None, "{}"),
    ("server_log", "server_log", None, "{}"),
]


def write_sweep_csv(path: str, rows: list[dict]) -> None:
    """One row per run, sorted by QPS: the load-sweep table with explicit units.

    retractions = sum of `#retracted_reqs` over the run's server log; max_token_usage = largest
    `token usage` on its Decode/Prefill lines. Runs without a server log are rejected.
    """
    missing = [r["file"] for r in rows if not r.get("log")]
    if missing:
        sys.exit("no server log for: " + ", ".join(missing))
    if any(r.get("qps") is None for r in rows):
        sys.exit("--sweep-csv needs meta.qps in every result file")
    with open(path, "w", newline="") as f:
        wr = csv.writer(f, lineterminator="\n")
        wr.writerow([c for c, *_ in SWEEP_COLS])
        for r in sorted(rows, key=lambda x: (float(x["qps"]), x["file"])):
            cells = []
            for _, key, scale, spec in SWEEP_COLS:
                v = r.get(key)
                if scale is not None:
                    v = v * scale
                cells.append(spec.format(v))
            wr.writerow(cells)


def main() -> None:
    ap = argparse.ArgumentParser(description="W3 result summary (client results + server logs)")
    ap.add_argument("files", nargs="+", help="result JSON paths or glob patterns (quote the patterns)")
    ap.add_argument("--group", action="store_true", help="group repetitions (_rN): median [min-max]")
    ap.add_argument("--csv", default=None, help="write the printed table (all columns) as CSV")
    ap.add_argument("--std-csv", default=None,
                    help="write only the standard summary columns, rows in the order of the patterns")
    ap.add_argument("--sweep-csv", default=None,
                    help="write the load-sweep table (qps, goodput, latency percentiles, retractions)")
    ap.add_argument("--cols", default=None, help="columns to print (comma-separated)")
    args = ap.parse_args()

    ordered = []                      # pattern order, each pattern sorted (for --std-csv)
    for pat in args.files:
        for p in sorted(glob.glob(pat)):
            if p not in ordered:
                ordered.append(p)
    paths = sorted(ordered)
    if not paths:
        sys.exit("no files found. Did you quote the glob patterns?")
    by_path = {p: run_row(p) for p in paths}
    rows = [by_path[p] for p in paths]
    for r in rows:
        r["name"] = cond_of(r) + (("_" + r["tag"].rsplit("_", 1)[1])
                                  if re.search(r"_r\d+$", r["tag"]) else "")
    cols = args.cols.split(",") if args.cols else COLS
    if args.group:
        g = group(rows)
        print_table(g, "cond", cols, True)
        data, fields = g, ["cond", "reps"] + [x for c in COLS for x in (c, c + "_min", c + "_max")]
    else:
        print_table(rows, "name", cols, False)
        data, fields = rows, ["name", "file", "dataset", "trace", "tag"] + COLS + [
            "retract_events", "mean_running", "kv_pool", "ttft_mean", "retracted_metric",
            "retracted_out_tok"]
    if args.csv:
        with open(args.csv, "w", newline="") as f:
            wr = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            wr.writeheader()
            for r in data:
                wr.writerow({k: (get(r, k) if k in COLS else r.get(k, "")) for k in fields})
        print(f"\nCSV written: {args.csv}")
    if args.std_csv:
        write_std_csv(args.std_csv, [by_path[p] for p in ordered])
        print(f"\nstandard CSV written: {args.std_csv}")
    if args.sweep_csv:
        write_sweep_csv(args.sweep_csv, rows)
        print(f"\nsweep CSV written: {args.sweep_csv}")


if __name__ == "__main__":
    main()
