"""
W3 보고서 그림. 결과 JSON·서버 로그만 읽는다 (GPU 불필요).

    python -m w3.figures --out report/figs

색 = 역할 고정 (그림마다 바뀌지 않음):
  default 파랑 · ablated 주황 · mine 청록 · tuned(--max-running-requests) 노랑 · noradix 자홍
이중 y축은 쓰지 않는다. 척도가 다른 두 지표는 위아래 패널로 나눈다.
"""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import os
import re
import sys

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib import font_manager  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bench.metrics import pct  # noqa: E402
from w3.summarize import run_row  # noqa: E402

C = {"default": "#2a78d6", "ablated": "#eb6834", "mine": "#1baf7a",
     "tuned": "#eda100", "noradix": "#e87ba4", "upstream": "#2a78d6",
     "cap": "#4a3aa7"}
INK, INK2, MUTED, GRID, BASE = "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7"
LABEL = {"default": "default", "ablated": "ablated (chunked prefill OFF)",
         "mine": "mine (peak-KV 예약)", "tuned": "tuned (--max-running-requests)",
         "noradix": "noradix (--disable-radix-cache)"}


def setup_style():
    for name in ("NanumBarunGothic", "NanumGothic", "NanumSquare"):
        if any(name in f.name for f in font_manager.fontManager.ttflist):
            plt.rcParams["font.family"] = name
            break
    plt.rcParams.update({
        "axes.unicode_minus": False, "font.size": 9, "axes.titlesize": 10,
        "axes.edgecolor": BASE, "axes.labelcolor": INK2, "xtick.color": MUTED,
        "ytick.color": MUTED, "axes.grid": True, "grid.color": GRID,
        "grid.linewidth": 0.6, "axes.spines.top": False, "axes.spines.right": False,
        "legend.frameon": False, "figure.dpi": 150, "savefig.dpi": 200,
        "lines.linewidth": 2.0, "lines.markersize": 6,
        "figure.facecolor": "white", "axes.facecolor": "white",
    })


def save(fig, out, name):
    os.makedirs(out, exist_ok=True)
    p = os.path.join(out, name)
    fig.savefig(p, bbox_inches="tight")
    plt.close(fig)
    print("saved", p)
    return p


# ---------------------------------------------------------------- 1. 출력 길이
def fig_output_len(out, trace="traces/reasoning.jsonl"):
    reqs = [json.loads(l) for l in open(trace)][1:]
    xs = sorted(r["max_new_tokens"] for r in reqs)
    fig, axes = plt.subplots(1, 2, figsize=(8.2, 2.8))
    ax = axes[0]
    import numpy as np
    bins = np.logspace(np.log10(256), np.log10(16000), 25)
    ax.hist(xs, bins=bins, color=C["default"], edgecolor="white", linewidth=1.5)
    ax.set_xscale("log")
    ax.set_xlabel("출력 토큰 (max_new_tokens = 실제 길이, 로그)")
    ax.set_ylabel("요청 수")
    ax.set_title("출력 길이 히스토그램 (n=120)", color=INK, loc="left")
    ax = axes[1]
    ys = [(i + 1) / len(xs) for i in range(len(xs))]
    ax.plot(xs, ys, color=C["default"])
    ax.set_xscale("log")
    for p, lab in ((50, "p50"), (90, "p90"), (99, "p99")):
        v = pct(xs, p)
        ax.axvline(v, color=MUTED, lw=0.8, ls="--")
        ax.text(v, 0.04 + (p == 90) * 0.12 + (p == 99) * 0.24, f" {lab} {v:,.0f}",
                color=INK2, fontsize=8)
    ax.set_xlabel("출력 토큰 (로그)")
    ax.set_ylabel("누적 비율")
    ax.set_title("CDF · 입력은 386–387 토큰으로 고정", color=INK, loc="left")
    fig.tight_layout()
    return save(fig, out, "fig01_output_len.png")


# ---------------------------------------------------------------- 2. 시계열
RE_TS = re.compile(r"^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\]")


def parse_timeseries(log):
    pts, retr, delays = [], [], []
    t0 = None
    for line in open(log, errors="ignore"):
        m = RE_TS.match(line)
        if not m:
            continue
        t = dt.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").timestamp()
        if "Prefill batch" in line and t0 is None:
            nt = re.search(r"#new-token: (\d+)", line)
            # 첫 재생 prefill (워밍업 3개 요청 이후) 기준. 워밍업은 max_new_tokens=8
            if nt and int(nt.group(1)) > 300:
                t0 = t
        if t0 is None:
            continue
        if "Decode batch" in line or "Prefill batch" in line:
            u = re.search(r"token usage: ([\d.]+)", line)
            r = re.search(r"#running-req: (\d+)", line)
            q = re.search(r"#queue-req: (\d+)", line)
            if u and r and q:
                pts.append((t - t0, float(u.group(1)), int(r.group(1)), int(q.group(1))))
        elif "Retract requests" in line:
            k = re.search(r"#retracted_reqs: (\d+)", line)
            retr.append((t - t0, int(k.group(1)) if k else 1))
        elif "Peak-KV reservation delayed" in line:
            delays.append(t - t0)
    return pts, retr, delays


def fig_timeseries(out, runs, name="fig02_timeseries.png", title_suffix=""):
    """runs: list of (label, color, server_log)."""
    n = len(runs)
    fig, axes = plt.subplots(3, n, figsize=(4.1 * n, 5.6), sharex="col",
                             sharey="row", squeeze=False)
    for j, (lab, col, log) in enumerate(runs):
        pts, retr, _ = parse_timeseries(log)
        t = [p[0] for p in pts]
        axes[0][j].plot(t, [p[1] for p in pts], color=col, lw=1.4)
        axes[0][j].set_ylim(0, 1.05)
        for rt, k in retr:
            axes[0][j].axvline(rt, color="#e34948", lw=0.6, alpha=0.6)
        axes[0][j].set_title(f"{lab} · 선점 {sum(k for _, k in retr)}회 (빨간 선)",
                             color=INK, loc="left")
        axes[1][j].plot(t, [p[2] for p in pts], color=col, lw=1.4)
        axes[1][j].axhline(24, color=MUTED, lw=0.8, ls="--")
        axes[1][j].text(0, 25, "decode CUDA graph 최대 bs 24", color=MUTED, fontsize=7,
                        ha="left", va="bottom")
        axes[2][j].plot(t, [p[3] for p in pts], color=col, lw=1.4)
        axes[2][j].set_xlabel("첫 prefill 이후 시간 (s, 서버 로그 1 s 해상도)")
    axes[0][0].set_ylabel("KV token usage")
    axes[1][0].set_ylabel("#running-req")
    axes[2][0].set_ylabel("#queue-req")
    fig.tight_layout()
    return save(fig, out, name)


# ---------------------------------------------------------------- 3. 부하 스윕
def qps_of(stem):
    m = re.search(r"_q([\d.]+)", stem)
    return float(m.group(1)) if m else 0.35


def fig_sweep_default(out, rows, name="fig03_sweep_default.png"):
    rows = sorted(rows, key=lambda r: qps_of(r["trace"]))
    q = [qps_of(r["trace"]) for r in rows]
    fig, axes = plt.subplots(1, 3, figsize=(10.5, 2.8), sharex=True)
    series = [("goodput_rps", "goodput (req/s)"), ("retracted", "#retracted_reqs 합"),
              ("ttft_p99", "TTFT p99 (s)")]
    for ax, (k, lab) in zip(axes, series):
        ys = [r[k] / (1000 if k == "ttft_p99" else 1) for r in rows]
        ax.plot(q, ys, color=C["default"], marker="o")
        ax.set_xscale("log")
        ax.set_xticks(q)
        ax.set_xticklabels([f"{x:g}" for x in q])
        ax.set_title(lab, color=INK, loc="left")
        ax.set_xlabel("도착률 QPS (로그)")
        ax.axvline(0.6, color=MUTED, lw=0.8, ls="--")
    axes[0].text(0.6, axes[0].get_ylim()[0], " 첫 선점 q0.6", color=INK2, fontsize=8,
                 va="bottom")
    fig.tight_layout()
    return save(fig, out, name)


def fig_sweep_3bar(out, table, name_goodput="fig04_sweep_goodput.png",
                   name_latency="fig05_sweep_latency.png"):
    """table: {bar: [(qps, goodput, ttft_p99_ms, maxitl_p99_ms, slo_pct), ...]}
    그림 A: goodput · slo_%   그림 B: TTFT p99 · maxITL p99 (로그)"""
    qs = sorted({p[0] for v in table.values() for p in v})
    paths = []
    for name, specs in (
            (name_goodput, [(1, "goodput (req/s) — TPOT≤60 ms", False, 1),
                            (4, "slo_% (TPOT≤60 ms 만족 비율)", False, 1)]),
            (name_latency, [(2, "TTFT p99 (s, 로그)", True, 1000),
                            (3, "maxITL p99 (s, 로그)", True, 1000)])):
        fig, axes = plt.subplots(1, 2, figsize=(9.6, 3.0))
        for ax, (idx, title, logy, div) in zip(axes, specs):
            for bar in ("default", "ablated", "mine"):
                pts = sorted(table.get(bar, []))
                if not pts:
                    continue
                ax.plot([p[0] for p in pts], [p[idx] / div for p in pts], color=C[bar],
                        marker="o", label=LABEL[bar],
                        lw=2.4 if bar == "mine" else 1.6,
                        ls="--" if bar == "ablated" else "-")
            ax.set_xscale("log")
            if logy:
                ax.set_yscale("log")
            ax.set_xticks(qs)
            ax.set_xticklabels([f"{x:g}" for x in qs])
            ax.minorticks_off() if not logy else None
            ax.set_title(title, color=INK, loc="left")
            ax.set_xlabel("도착률 QPS (로그)")
        axes[0].legend(fontsize=7.5, loc="best")
        fig.tight_layout()
        paths.append(save(fig, out, name))
    return paths


# ---------------------------------------------------------------- 4. 교환비
def fig_tradeoff(out, pts, name="fig06_tradeoff.png"):
    """pts: list of dict(label, kind, retracted, stall5, ttft_p99, out_tok_s, goodput)"""
    marker = {"default": "o", "mine": "s", "tuned": "^", "noradix": "D", "ablated": "v"}
    fig, axes = plt.subplots(1, 3, figsize=(10.8, 3.2))
    specs = [("retracted", "ttft_p99", "#retracted_reqs 합", "TTFT p99 (s)"),
             ("retracted", "out_tok_s", "#retracted_reqs 합", "out_tok_s"),
             ("retracted", "goodput_rps", "#retracted_reqs 합", "goodput (req/s)")]
    for ax, (xk, yk, xl, yl) in zip(axes, specs):
        for kind in ("default", "ablated", "noradix", "tuned", "mine"):
            ps = [p for p in pts if p["kind"] == kind]
            if not ps:
                continue
            ps.sort(key=lambda p: p.get("order", 0))
            xs = [p[xk] for p in ps]
            ys = [p[yk] / (1000 if yk == "ttft_p99" else 1) for p in ps]
            big = kind == "default"
            ax.plot(xs, ys, color=C[kind], marker=marker[kind], markersize=10 if big else 7,
                    lw=1.2 if kind in ("mine", "tuned") else 0,
                    mfc="none" if big else C[kind], mew=2 if big else 1,
                    zorder=5 if big else 3, label=LABEL.get(kind, kind))
            # 겹치는 라벨 정리: α≤0.6 은 default 와 같은 점이라 하나로 묶는다
            place = {"α=0.5": None, "α=0.6": ("α=0.5·0.6", 6, 6), "default": ("default", 6, -13),
                     "noradix": None,   # 범례의 마름모로 식별 (직접 라벨은 선택적으로)
                     "N=40": ("N=40", 6, -15), "α=1.0": ("α=1.0", 6, 6)}
            if yk == "goodput_rps":        # 이 패널에서만 N=48 라벨이 mine 선과 겹친다
                place["N=48"] = ("N=48", 6, -13)
            for p, x, y in zip(ps, xs, ys):
                key = p["short"].rstrip("*")
                lab, dx, dy = (p["short"], 4, 4)
                if key in place:
                    if place[key] is None:
                        continue
                    lab, dx, dy = place[key][0] + ("*" if p["short"].endswith("*") else ""), place[key][1], place[key][2]
                ax.annotate(lab, (x, y), textcoords="offset points", xytext=(dx, dy),
                            fontsize=7, color=INK2)
        ax.set_xlabel(xl)
        ax.set_title(yl, color=INK, loc="left")
    axes[0].legend(fontsize=7, loc="upper right")
    fig.tight_layout()
    return save(fig, out, name)


def fig_alpha(out, name="fig08_alpha.png", trace="reasoning_q2"):
    """α(예약 비율) 곡선. α<1 은 출력 길이를 α 배로 과소 예측한 것과 같다. default 는 점선."""
    pts = []
    for p in glob.glob(f"results/{trace}__mine_a*_r1.json"):
        m = re.search(r"__mine_a([\d.]+)_r1\.json$", p)      # _p16 스모크 등은 제외
        if m:
            pts.append((float(m.group(1)), run_row(p)))
    if not pts:
        return None
    pts.sort()
    d = run_row(f"results/{trace}__default_r1.json")
    a = [x[0] for x in pts]
    fig, axes = plt.subplots(1, 3, figsize=(10.5, 2.8))
    specs = [(lambda r: r["retracted"], "#retracted_reqs 합", 1),
             (lambda r: r["ttft_p99"] / 1000, "TTFT p99 (s)", 1),
             (lambda r: r["goodput_rps"], "goodput (req/s)", 1)]
    for ax, (f, title, _) in zip(axes, specs):
        ax.plot(a, [f(r) for _, r in pts], color=C["mine"], marker="s", markersize=7)
        ax.axhline(f(d), color=C["default"], lw=1.4, ls="--")
        ax.text(a[0], f(d), " default", color=C["default"], fontsize=8, va="bottom")
        ax.set_xlabel("예약 비율 α (1 = 알려진 길이 전부)")
        ax.set_title(title, color=INK, loc="left")
        ax.set_xticks(a)
    fig.tight_layout()
    return save(fig, out, name)


def fig_gain_bars(out, groups, name, title, ylabel="default 대비 변화 (%)"):
    """groups: list of (label, value_pct)"""
    fig, ax = plt.subplots(figsize=(6.4, 2.6))
    labs = [g[0] for g in groups]
    vals = [g[1] for g in groups]
    # 색은 엔티티(mine)를 따른다. 부호는 기준선과 값 라벨로 읽는다.
    ax.bar(range(len(vals)), vals, color=C["mine"], width=0.55)
    ax.axhline(0, color=BASE, lw=1)
    span = max(vals) - min(min(vals), 0)
    ax.set_ylim(min(min(vals), 0) - 0.18 * span, max(vals) + 0.12 * span)
    for i, v in enumerate(vals):
        ax.text(i, v + (1 if v >= 0 else -1) * span * 0.03,
                f"{v:+.1f}%".replace("-0.0%", "±0.0%").replace("+0.0%", "±0.0%"),
                ha="center", va="bottom" if v >= 0 else "top", fontsize=8, color=INK)
    ax.set_xticks(range(len(vals)))
    ax.set_xticklabels(labs, fontsize=8)
    # 길이 분포 조건(q2)과 부하 조건 사이에 구분선
    split = next((i for i, l in enumerate(labs) if l.startswith("q")), None)
    if split:
        ax.axvline(split - 0.5, color=MUTED, lw=0.8, ls="--")
        ax.text(split - 0.45, max(vals) + 0.05 * span, "부하 (σ 0.6)", color=MUTED, fontsize=7.5)
        ax.text(-0.4, max(vals) + 0.05 * span, "길이 분포 (q2)", color=MUTED, fontsize=7.5)
    ax.set_ylabel(ylabel)
    ax.set_title(title, color=INK, loc="left")
    ax.grid(axis="x", visible=False)
    fig.tight_layout()
    return save(fig, out, name)


def _pair(trace, astar):
    base = "upstream" if trace == "reasoning" else "default"
    d = sorted(glob.glob(f"results/{trace}__{base}_r[0-9].json"))
    m = sorted(glob.glob(f"results/{trace}__mine_a{astar}_r[0-9].json"))
    return [run_row(p) for p in d], [run_row(p) for p in m]


def _med(rows, k):
    import statistics
    vs = [r[k] for r in rows if r.get(k) == r.get(k)]
    return statistics.median(vs) if vs else float("nan")


def main():
    """보고서의 그림 전부를 결과 파일에서 다시 만든다."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="report/figs")
    ap.add_argument("--astar", default="1.0", help="최종 α*")
    args = ap.parse_args()
    out, A = args.out, args.astar
    setup_style()
    fig_output_len(out)
    fig_timeseries(out, [("default (q2)", C["default"], "logs/server_reasoning_q2__default_r1.log"),
                         (f"mine α={A} (q2)", C["mine"], f"logs/server_reasoning_q2__mine_a{A}_r1.log")])
    from w3.summarize import parse_server_log
    rows = []
    for q in ["0.5", "0.6", "0.8", "1", "2", "4", "8"]:
        r = dict(run_row(f"results/reasoning_q{q}__default.json"))
        r.update(parse_server_log(f"logs/server_q{q}.log"))
        rows.append(r)
    up = [run_row(p) for p in sorted(glob.glob("results/reasoning__upstream_r[0-9].json"))]
    rows.append({"trace": "reasoning", "goodput_rps": _med(up, "goodput_rps"), "retracted": 0,
                 "ttft_p99": _med(up, "ttft_p99")})
    fig_sweep_default(out, rows)
    table = {"default": [], "ablated": [], "mine": []}
    for tr, q in (("reasoning", 0.35), ("reasoning_q0.5", 0.5), ("reasoning_q1", 1.0),
                  ("reasoning_q2", 2.0), ("reasoning_q4", 4.0)):
        for bar, tag in (("default", "default"), ("ablated", "ablated"), ("mine", f"mine_a{A}")):
            t = "upstream" if (tr == "reasoning" and bar == "default") else tag
            rs = [run_row(p) for p in sorted(glob.glob(f"results/{tr}__{t}_r[0-9].json"))]
            if rs:
                table[bar].append((q, _med(rs, "goodput_rps"), _med(rs, "ttft_p99"),
                                   _med(rs, "maxitl_p99"), _med(rs, "slo_%")))
    fig_sweep_3bar(out, table)
    pts = []
    for p in sorted(glob.glob("results/reasoning_q2__*_r1.json")):
        tag = re.search(r"__(.+)_r1\.json", p).group(1)
        if "_p16" in tag:          # page-size 스모크는 교환비 그림에서 제외
            continue
        kind = ("mine" if tag.startswith("mine") else "tuned" if tag.startswith("tuned") else
                tag if tag in ("default", "noradix") else None)
        if not kind:
            continue
        r = run_row(p)
        num = re.search(r"([\d.]+)$", tag)
        pts.append(dict(kind=kind, short=tag.replace("mine_a", "α=").replace("tuned_n", "N="),
                        order=float(num.group(1)) if num else 0, retracted=r["retracted"],
                        ttft_p99=r["ttft_p99"], out_tok_s=r["out_tok_s"], goodput_rps=r["goodput_rps"]))
    fig_tradeoff(out, pts)
    bars = []
    for tr, lab in (("reasoning_q2_s0.3", "σ=0.3"), ("reasoning_q2", "σ=0.6"), ("reasoning_q2_s1.0", "σ=1.0"),
                    ("reasoning_q2_m1000", "mean 1000"), ("reasoning", "q0.35"), ("reasoning_q0.5", "q0.5"),
                    ("reasoning_q1", "q1"), ("reasoning_q4", "q4")):
        d, m = _pair(tr, A)
        if d and m:
            bars.append((lab, 100 * (_med(m, "goodput_rps") - _med(d, "goodput_rps")) / _med(d, "goodput_rps")))
    if bars:
        fig_gain_bars(out, bars, "fig07_conditions.png", f"조건별 mine(α={A}) goodput 변화 (default 대비)")
    fig_alpha(out)


if __name__ == "__main__":
    main()
