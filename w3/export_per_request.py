"""
Export one row per request for every replay result, plus per-run cumulative-output curves and
per-log server facts, so that the headline numbers (analysis/headline.py), the goodput
decomposition and the request timelines can be recomputed without the raw data release.

    python3 -m w3.export_per_request          # from the repository root, raw data in results/ and logs/

Inputs: results/*.json, results/det/*.json, results/cross/*.json and the server logs in logs/ and
logs/det/ (results/failed/, logs/failed/ and results/backup/ are skipped: a run that was re-run
after a client error, and the original of a run that a failed re-run overwrote).

Outputs (the .gz files use gzip with mtime 0 and no file name; rows are sorted by (run, rid) or
by log path; floats have fixed formats; so the same inputs give byte-identical files):

data/derived/per_request.csv.gz, one row per (run, rid):
  run                 result path under results/ without .json, e.g. reasoning_q2__default_r1, det/..., cross/...
  era                 0903 | 0906 | 0907 | 0913 | det | cross (see ERA_RULES)
  trace               trace stem, e.g. reasoning_q2, reasoning_q2_s0.3, rag_x
  bar                 configuration tag without the repetition suffix, e.g. default, mine_a1.0, tuned_n40
  rep                 repetition number from the _rN suffix (empty when the run has none)
  rid, idx            request id and its position in the result file
  submit_t, first_token_t, end_t
                      seconds relative to the run's earliest submit_t
  ttft_s, tpot_ms, e2e_s
                      w3.metrics.request_metrics (empty for failed requests; tpot_ms empty when
                      fewer than two tokens were generated)
  completion_tokens, prompt_tokens, cached_tokens
  max_gap_s, max_gap_start_s
                      largest gap between consecutive streamed chunks and when it started
                      (relative to the run's earliest submit_t)
  n_gaps_gt5s         number of gaps longer than 5 s
  slo_ok              1 / 0, empty for failed requests
  error               client error text, empty when the request succeeded
  slo_class, ttft_slo_ms, tpot_slo_ms
                      the request's SLO fields as stored in the result file
  output_sha16        first 16 hex digits of the SHA-256 of the generated text (UTF-8); empty
                      for failed requests and for results that did not store the text. Equal
                      values across runs mean identical outputs (used for the correctness checks)

data/derived/cum_output.csv.gz, the cumulative output-token curve of every run:
  run, pct, tokens, t_s
                      t_s = arrival time (relative to the run's earliest submit_t) of output token
                      number `tokens` = floor(total_output_tokens * pct / 100), pct = 1..100

data/derived/server_logs.csv, one row per server log in logs/ and logs/det/ (w3.summarize.parse_server_log):
  log, retract_events, retracted_reqs, max_token_usage, max_running_reqs, max_queue_reqs,
  peak_kv_delays_logged (last cumulative #delays logged; logged at most every 5 s, so a lower
  bound), kv_pool_tokens
"""

from __future__ import annotations

import argparse
import csv
import glob
import gzip
import hashlib
import io
import json
import os
import re
import sys

from w3.metrics import max_gap, request_metrics, stream_gaps
from w3.summarize import parse_server_log

STALL_S = 5.0

# era by result name (path under results/ without .json); the first match wins
ERA_RULES = [
    (r"^det/", "det"),                                  # 09-13, deterministic mode (correctness checks)
    (r"^cross/", "cross"),                              # 09-13, 5-workload cross replay
    (r"^[^/]+__.+_r\d+$", "0913"),                      # 09-13 runs of w3/run_one.sh
    (r"^reasoning__noradix$", "0913"),                  # 09-13 failed re-run that overwrote the original
    (r"^reasoning__batch_test$", "0903"),               # 09-03 harness test, no server was up
    (r"^reasoning__default$", "0906"),                  # 09-06 unpatched baseline (qps 0.35)
    (r"^reasoning__(ablated|nograph)$", "0907"),        # 09-07 unpatched ablations (qps 0.35)
    (r"^reasoning_q[\d.]+__default$", "0907"),          # 09-07 unpatched load sweep
]

COLUMNS = ["run", "era", "trace", "bar", "rep", "rid", "idx",
           "submit_t", "first_token_t", "end_t", "ttft_s", "tpot_ms", "e2e_s",
           "completion_tokens", "prompt_tokens", "cached_tokens",
           "max_gap_s", "max_gap_start_s", "n_gaps_gt5s", "slo_ok", "error",
           "slo_class", "ttft_slo_ms", "tpot_slo_ms", "output_sha16"]
CUM_COLUMNS = ["run", "pct", "tokens", "t_s"]
LOG_COLUMNS = ["log", "retract_events", "retracted_reqs", "max_token_usage", "max_running_reqs",
               "max_queue_reqs", "peak_kv_delays_logged", "kv_pool_tokens"]


def era_of(run: str) -> str:
    for rx, era in ERA_RULES:
        if re.search(rx, run):
            return era
    sys.exit(f"no era rule for results/{run}.json: add one to ERA_RULES")


def sec(x) -> str:
    return "" if x is None else f"{x:.6f}"


def opt(x) -> str:
    return "" if x is None else str(x)


def output_sha16(rec: dict) -> str:
    if rec.get("error") or "output" not in rec:
        return ""
    return hashlib.sha256(rec["output"].encode("utf-8")).hexdigest()[:16]


def result_files(results: str) -> list[str]:
    files = []
    for sub in ("", "det", "cross"):
        files += glob.glob(os.path.join(results, sub, "*.json"))
    return sorted(files)


def run_name(path: str, results: str) -> str:
    return os.path.splitext(os.path.relpath(path, results))[0].replace(os.sep, "/")


def export_run(path: str, results: str):
    """(per-request rows, cumulative-output rows) of one result file."""
    run = run_name(path, results)
    with open(path) as fh:
        recs = json.load(fh)["records"]
    stem, _, tag = os.path.basename(run).partition("__")
    m = re.search(r"_r(\d+)$", tag)
    bar, rep = (tag[: m.start()], m.group(1)) if m else (tag, "")
    era = era_of(run)
    submits = [r["submit_t"] for r in recs if r.get("submit_t") is not None]
    t0 = min(submits) if submits else 0.0
    rel = lambda t: None if t is None else t - t0

    rows = []
    for idx, r in enumerate(recs):
        pm = request_metrics(r)
        ok = "ttft_ms" in pm
        mg = max_gap(r.get("chunk_times"))
        tp = pm.get("tpot_ms")
        rows.append([
            run, era, stem, bar, rep, r["rid"], str(idx),
            sec(rel(r.get("submit_t"))), sec(rel(r.get("first_token_t"))), sec(rel(r.get("end_t"))),
            sec(pm["ttft_ms"] / 1000.0) if ok else "", f"{tp:.6f}" if tp is not None else "",
            sec(pm["e2e_ms"] / 1000.0) if ok else "",
            str(r.get("completion_tokens") or 0), str(r.get("prompt_tokens") or 0), str(r.get("cached_tokens") or 0),
            sec(mg[0]) if mg else "", sec(rel(mg[1])) if mg else "",
            str(sum(1 for g, _ in stream_gaps(r.get("chunk_times")) if g > STALL_S)),
            ("1" if pm["slo_ok"] else "0") if ok else "", r.get("error") or "",
            opt(r.get("slo_class")), opt(r.get("ttft_slo_ms")), opt(r.get("tpot_slo_ms")),
            output_sha16(r),
        ])
    rows.sort(key=lambda x: x[5])          # by rid (stable: ties keep the file order)

    # cumulative output tokens over time: one entry per streamed token
    arrivals = sorted(t for r in recs for t, k in zip(r.get("chunk_times") or [], r.get("chunk_tokens") or [])
                      for _ in range(k))
    cum = []
    if arrivals:
        total = len(arrivals)
        for pct in range(1, 101):
            k = total * pct // 100
            if k >= 1:
                cum.append([run, str(pct), str(k), sec(arrivals[k - 1] - t0)])
    return rows, cum


def log_rows(logs: str) -> list[list[str]]:
    """Server-log facts for logs/server_*.log and logs/det/server_*.log (logs/failed/ is skipped)."""
    rows = []
    for p in sorted(glob.glob(os.path.join(logs, "server_*.log")) + glob.glob(os.path.join(logs, "det", "server_*.log"))):
        s = parse_server_log(p)
        rows.append([p.replace(os.sep, "/"), str(s["retract_events"]), str(s["retracted"]), f"{s['max_usage']:.2f}",
                     str(s["max_running"]), str(s["max_queue"]), str(s["peak_delays"]), opt(s["kv_pool"])])
    return sorted(rows)


def write_csv_gz(path: str, header: list[str], rows: list[list[str]]) -> int:
    buf = io.StringIO()
    wr = csv.writer(buf, lineterminator="\n")
    wr.writerow(header)
    wr.writerows(rows)
    raw = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0, compresslevel=9) as gz:
        gz.write(buf.getvalue().encode("utf-8"))
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(raw.getvalue())
    return len(raw.getvalue())


def main() -> None:
    ap = argparse.ArgumentParser(description="export per-request rows, cumulative-output curves and server-log facts")
    ap.add_argument("--results", default="results", help="raw result folder (default: %(default)s)")
    ap.add_argument("--out", default="data/derived/per_request.csv.gz", help="default: %(default)s")
    ap.add_argument("--cum-out", default="data/derived/cum_output.csv.gz", help="default: %(default)s")
    ap.add_argument("--logs", default="logs", help="raw server-log folder (default: %(default)s)")
    ap.add_argument("--logs-out", default="data/derived/server_logs.csv", help="default: %(default)s")
    args = ap.parse_args()

    files = result_files(args.results)
    if not files:
        sys.exit(f"no result files under {args.results}/ (see data/RAW.md for the raw data release)")
    rows, cum = [], []
    for p in files:
        r, c = export_run(p, args.results)
        rows += r
        cum += c
    rows.sort(key=lambda x: x[0])          # by run; within a run the rid order is kept
    cum.sort(key=lambda x: x[0])
    n1 = write_csv_gz(args.out, COLUMNS, rows)
    n2 = write_csv_gz(args.cum_out, CUM_COLUMNS, cum)
    eras = {}
    for r in rows:
        eras.setdefault(r[1], set()).add(r[0])
    print(f"{args.out}: {len(rows)} rows from {len(files)} runs, {n1:,} bytes")
    print("  runs per era: " + ", ".join(f"{e} {len(v)}" for e, v in sorted(eras.items())))
    print(f"{args.cum_out}: {len(cum)} rows, {n2:,} bytes")
    lrows = log_rows(args.logs)
    with open(args.logs_out, "w", newline="") as fh:
        wr = csv.writer(fh, lineterminator="\n")
        wr.writerow(LOG_COLUMNS)
        wr.writerows(lrows)
    print(f"{args.logs_out}: {len(lrows)} server logs")


if __name__ == "__main__":
    main()
