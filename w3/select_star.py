"""
Apply the P3 selection rule to the q2 exploration runs and pick alpha* and N*
(plan_2026-09-13.md 4.3; the rule was fixed before any result was seen).

  1) auxiliary SLO: every request keeps its largest in-stream gap <= 5 s (stall5_reqs == 0)
  2) among those, the lowest TTFT p99
  3) if no setting satisfies 1), the lowest stall5_reqs, then the lowest TTFT p99

    python3 -m w3.select_star          # from data/derived/w3_runs.csv (no raw data needed)
    python3 -m w3.select_star --raw    # recompute from results/reasoning_q2__{mine_a*,tuned_n*}_r1.json
"""

import argparse
import csv
import glob
import re

RUNS_CSV = "data/derived/w3_runs.csv"
# (result-file pattern for --raw, setting regex): alpha from mine_a<alpha>_r1, N from tuned_n<N>_r1
SPECS = [("results/reasoning_q2__mine_a*_r1.json", r"mine_a([\d.]+)_r1"),
         ("results/reasoning_q2__tuned_n*_r1.json", r"tuned_n(\d+)_r1")]


def rows_from_csv(path: str) -> list[tuple[str, dict]]:
    """(result file, row) pairs from the per-run table written by w3.summarize --csv."""
    out = []
    with open(path, newline="") as fh:
        for r in csv.DictReader(fh):
            out.append((r["file"], {
                "stall5_reqs": int(r["stall5_reqs"]),
                "ttft_p99": float(r["ttft_p99"]),
                "retracted": int(r["retracted"]) if r["retracted"] != "" else -1,
                "goodput_rps": float(r["goodput_rps"]),
                "maxitl_p99": float(r["maxitl_p99"]),
            }))
    return out


def rows_from_raw(pattern: str) -> list[tuple[str, dict]]:
    from w3.summarize import run_row
    return [(p, run_row(p)) for p in sorted(glob.glob(pattern))]


def select(source: list[tuple[str, dict]], key_re: str):
    """Apply the rule to the q2 runs whose file name matches ``key_re``.

    Returns (rows, selected setting, whether any setting met the auxiliary SLO); rows are
    (setting, row) pairs sorted by the numeric setting.
    """
    rows = []
    for p, row in source:
        m = re.search(r"results/reasoning_q2__" + key_re + r"\.json$", p)   # skips smoke runs such as mine_a1.0_p16_r1
        if m:
            rows.append((m.group(1), row))
    rows.sort(key=lambda x: float(x[0]))
    ok = [x for x in rows if x[1]["stall5_reqs"] == 0]
    pool = ok if ok else rows
    best = min(pool, key=lambda x: (x[1]["stall5_reqs"], x[1]["ttft_p99"]))
    return rows, best[0], bool(ok)


def pick(source: list[tuple[str, dict]], label: str, key_re: str):
    rows, best, met = select(source, key_re)
    print(f"\n{label}")
    print(f"{'setting':>8} {'stall5':>7} {'TTFT p99(s)':>12} {'retr':>5} {'goodput':>8} {'maxITL p99(s)':>14}")
    for k, r in rows:
        print(f"{k:>8} {r['stall5_reqs']:>7} {r['ttft_p99']/1000:>12.1f} {r.get('retracted', -1):>5} "
              f"{r['goodput_rps']:>8.3f} {r['maxitl_p99']/1000:>14.2f}")
    why = ("lowest TTFT p99 among settings meeting the auxiliary SLO" if met
           else "no setting meets the auxiliary SLO -> fewest stall5, then lowest TTFT p99")
    print(f"=> selected: {best}  ({why})")
    return best


def main() -> None:
    ap = argparse.ArgumentParser(description="pick alpha* and N* with the preregistered P3 rule")
    ap.add_argument("--raw", action="store_true", help="recompute from results/ instead of " + RUNS_CSV)
    ap.add_argument("--csv", default=RUNS_CSV, help="per-run table (default: %(default)s)")
    args = ap.parse_args()
    picks = []
    for pattern, key_re in SPECS:
        source = rows_from_raw(pattern) if args.raw else rows_from_csv(args.csv)
        picks.append(pick(source, pattern if args.raw else f"{args.csv}: {pattern}", key_re))
    print(f"\nASTAR={picks[0]} NSTAR={picks[1]}")


if __name__ == "__main__":
    main()
