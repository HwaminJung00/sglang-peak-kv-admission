"""
W3 결과 요약: 클라이언트 결과(JSON) + 서버 로그 + /metrics 를 한 표로 모은다.
GPU 가 필요 없고, bench/metrics.py 의 정의(summarize, per_request)를 그대로 가져다 쓴다.

    python -m w3.summarize 'results/reasoning_q2__*.json'            # 실행별 표
    python -m w3.summarize 'results/reasoning_q2__*.json' --group     # 반복을 묶어 중앙값 [min–max]
    python -m w3.summarize 'results/*.json' 'results/det/*.json' --csv results/w3_runs.csv

추가 열 (bench.metrics 에 없는 것):
  e2e_p50         E2E 중앙값
  ttft_mean       TTFT 평균 (W3 q≥2 의 TTFT 는 '즉시 입장'과 '30 s 이상 대기' 두 봉우리라 p50 이 경계에서 불안정)
  wait10_reqs     TTFT 가 10 s 를 넘은 요청 수 (입장·대기열에서 기다린 요청)
  stall5_reqs     한 요청 안에서 토큰 간격이 5 s 를 넘은 요청 수 (보조 SLO 위반 수)
  stall_max_s     가장 긴 정지(s)
  aux_slo_%       TPOT ≤ SLO 이고 정지 ≤ 5 s 인 요청 비율 (결과 보기 전에 정한 보조 지표)
  retracted       서버 로그 `#retracted_reqs` 합 (같은 요청이 여러 번 셀 수 있음)
  max_usage       Decode/Prefill 줄의 token usage 최댓값
  max_running     #running-req 최댓값
  max_queue       #queue-req 최댓값
  graph_%         Decode 줄 중 `cuda graph: True` 비율 (40 step 마다 한 줄 → 표본)
  peak_delays     [mine] `Peak-KV reservation delayed admission` 누적 #delays 마지막 값
  recompute_tok   /metrics: prefill_compute − input 유효 토큰 (선점된 요청의 재계산 KV)
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bench.metrics import pct, per_request, summarize  # noqa: E402

STALL_S = 5.0

RE_RETRACT = re.compile(r"#retracted_reqs: (\d+)")
RE_USAGE = re.compile(r"token usage: ([\d.]+)")
RE_RUN = re.compile(r"#running-req: (\d+)")
RE_QUEUE = re.compile(r"#queue-req: (\d+)")
RE_PEAK = re.compile(r"Peak-KV reservation delayed admission\. #delays: (\d+)")
RE_POOL = re.compile(r"max_total_num_tokens=(\d+)")
RE_METRIC = re.compile(r'^(sglang:[a-z_]+)\{([^}]*)\} ([0-9.eE+-]+)$')


def log_path_for(res_path: str) -> str:
    """results/det/x__t.json -> logs/det/server_x__t.log"""
    d, f = os.path.split(res_path)
    sub = os.path.relpath(d, "results")
    sub = "" if sub in (".", "") else sub
    name = os.path.splitext(f)[0]
    return os.path.join("logs", sub, f"server_{name}.log")


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
    rows = [per_request(r) for r in blob["records"]]
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
        "e2e_p50": pct([r["e2e_ms"] for r in good], 50),
        "ttft_mean": statistics.fmean([r["ttft_ms"] for r in good]) if good else float("nan"),
        "wait10_reqs": sum(1 for r in good if r["ttft_ms"] > 10_000),
        "stall5_reqs": sum(1 for g in stalls if g > STALL_S),
        "stall_max_s": max(stalls, default=0.0),
        "aux_slo_%": 100.0 * aux_ok / len(good) if good else float("nan"),
    }


def run_row(path: str) -> dict:
    blob = json.load(open(path))
    s = summarize(blob)
    name = os.path.splitext(os.path.basename(path))[0]
    stem, _, tag = name.partition("__")
    s.update(file=path, trace=stem, tag=tag,
             dataset=os.path.relpath(os.path.dirname(path), "results"))
    s.update(client_extra(blob))
    lp = log_path_for(path)
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


def main() -> None:
    ap = argparse.ArgumentParser(description="W3 결과 요약 (클라이언트 + 서버 로그)")
    ap.add_argument("files", nargs="+")
    ap.add_argument("--group", action="store_true", help="반복(_rN)을 묶어 중앙값")
    ap.add_argument("--csv", default=None)
    ap.add_argument("--cols", default=None, help="보여줄 열 (쉼표 구분)")
    args = ap.parse_args()

    paths = sorted({p for pat in args.files for p in glob.glob(pat)})
    if not paths:
        sys.exit("파일을 찾지 못했다. 패턴을 따옴표로 감쌌는지 확인할 것.")
    rows = [run_row(p) for p in paths]
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
        import csv
        with open(args.csv, "w", newline="") as f:
            wr = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            wr.writeheader()
            for r in data:
                wr.writerow({k: (get(r, k) if k in COLS else r.get(k, "")) for k in fields})
        print(f"\nCSV 저장: {args.csv}")


if __name__ == "__main__":
    main()
