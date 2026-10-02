"""
Figures for the README and the reports, drawn from the derived data in git (no GPU, no raw data).

    python3 -m w3.figures [--out docs/figures] [--only hero fig03 ...]

Run from the repository root. Needs matplotlib (figures only, see requirements.txt).

Inputs, all in git:
    data/derived/per_request.csv.gz   one row per request and run: hero_timeline, fig01, fig05, fig06
    data/derived/cum_output.csv.gz    time at which each 1 % of the output tokens had arrived: fig05
    data/derived/w3_runs.csv          one row per 09-13 run: hero_timeline, fig03, fig06
    data/derived/w3_q2_grouped.csv    q2 medians with min/max over the repetitions: fig03, fig04
    data/derived/sweep_0907.csv       09-07 unpatched load sweep: fig02
    artifacts/numbers.json            the text of every number printed on a figure
results/ and logs/ are never read.

Every measured number printed on a figure is the "display" string of an artifacts/numbers.json key
(axis ticks, thresholds such as 5 s or 10 s, and configuration names are not measurements). Before
drawing, the script recomputes each printed number from the plotted data and stops (SystemExit) if
it would display differently, so a figure cannot drift from the docs. It also stops if any text
would be cut off at the figure edge.

Conventions, the same in every figure:
    colors   default #6b6b6b solid, ablated #a0a0a0 dashed, patch (mine) #1f77b4,
             --disable-radix-cache #2ca02c, --max-running-requests #ff7f0e, KV-pool cap #9467bd.
             Green and orange look alike to red-green colour-blind readers, so those marks also
             differ in marker or line style and carry a direct label.
    markers  filled = median of 3 runs, with min-max error bars; hollow = a single run (n=1).
    labels   "default" is the patched build with the flag off (09-13 runs); fig02 is the
             unpatched 09-07 sweep. Log axes print plain numbers and no minor-tick labels
             (errata C4).
Output: PNG, 150 dpi, a fixed size per figure, no Software or time metadata, so the bytes repeat
with the same matplotlib version.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
import os
import re
import statistics
from collections import defaultdict

try:
    import matplotlib
except ImportError:
    raise SystemExit("w3.figures needs matplotlib (figures only): python3 -m pip install matplotlib==3.11.2")

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib import ticker  # noqa: E402
from matplotlib.collections import LineCollection  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402
from matplotlib.text import Text  # noqa: E402

D = "data/derived/"
NUMBERS = "artifacts/numbers.json"
DPI = 150
WIDTH = 8.8          # inches: 1,320 px at 150 dpi, shown at about 880 px in a GitHub README
STALL_S = 5.0        # "stall" = a gap of more than 5 s between two streamed tokens
WAIT_S = 10.0        # "waiting" = TTFT above 10 s

# series: one color per configuration, fixed across figures
C_DEFAULT, C_ABLATED, C_PATCH = "#6b6b6b", "#a0a0a0", "#1f77b4"
C_NORADIX, C_TUNED, C_CAP = "#2ca02c", "#ff7f0e", "#9467bd"
M_DEFAULT, M_ABLATED, M_PATCH, M_NORADIX, M_TUNED, M_CAP = "o", "s", "D", "v", "^", "X"
C_WALL = "#9ecae1"   # light step of the patch blue: the wall-time part of the gain
# request states in the timeline
C_WAIT, C_GEN, C_STALL = "#b0b0b0", C_PATCH, "#d62728"
# text and chrome
INK, INK2, MUTED, GRID, SPINE, BAND = "#1a1a1a", "#454545", "#6e6e6e", "#e9e9e9", "#b5b5b5", "#e8edf3"
MINUS, EN = "\u2212", "\u2013"
LAMBDA = "$\\lambda_\\mathrm{max}$"

L_DEFAULT = "default (flag off)"
L_ABLATED = "ablated (--chunked-prefill-size -1)"
L_PATCH = "patch, \u03b1=1.0"
L_NORADIX = "--disable-radix-cache"
L_TUNED = "--max-running-requests 40"


# ----------------------------------------------------------------------------- numbers.json
class Numbers:
    """Display strings from artifacts/numbers.json, with a check against the plotted data."""

    def __init__(self, path=NUMBERS):
        with open(path, encoding="utf-8") as fh:
            self.d = json.load(fh)

    def __call__(self, key):
        return self.entry(key)["display"]

    def entry(self, key):
        if key not in self.d:
            raise SystemExit(f"{NUMBERS}: missing key {key}")
        return self.d[key]

    def value(self, key):
        return self.entry(key)["value"]

    def agree(self, key, *computed):
        """Stop unless the computed value(s) round to the display of KEY (one value, or two for a range)."""
        parts = self(key).split(EN) if len(computed) == 2 else [self(key)]
        if len(parts) != len(computed):
            raise SystemExit(f"{key}: display {self(key)!r} does not have {len(computed)} part(s)")
        for text, x in zip(parts, computed):
            t = text.replace(",", "").replace(MINUS, "-").replace("+", "")
            nd = len(t.split(".")[1]) if "." in t else 0
            if abs(float(t) - float(x)) > 0.5 * 10 ** -nd + 1e-9:
                raise SystemExit(f"{key}: the figure data give {x}, numbers.json displays {self(key)!r}")
        return self(key)


# ----------------------------------------------------------------------------- inputs
def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return x


def read_csv(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return [{k: _num(v) for k, v in r.items()} for r in csv.DictReader(fh)]


def per_request():
    out = defaultdict(list)
    with gzip.open(D + "per_request.csv.gz", "rt", encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            out[r["run"]].append(r)
    return out


def cum_output():
    out = defaultdict(dict)
    with gzip.open(D + "cum_output.csv.gz", "rt", encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            out[r["run"]][int(r["pct"])] = float(r["t_s"])
    return out


def ok_rows(rows):
    return [r for r in rows if not r["error"]]


def fcol(rows, key):
    return [float(r[key]) for r in rows if r[key] != ""]


def median_run(runs, stem):
    """Name of the repetition r1..r3 of STEM with the median goodput (data/derived/w3_runs.csv)."""
    names = sorted((runs[f"{stem}_r{i}"]["goodput_rps"], f"{stem}_r{i}") for i in (1, 2, 3))
    return names[1][1]


def type7(xs, p):
    """Percentile with linear interpolation between order statistics (numpy 'linear', R type 7)."""
    xs = sorted(xs)
    h = (len(xs) - 1) * p / 100
    lo = math.floor(h)
    return xs[lo] + (h - lo) * (xs[min(lo + 1, len(xs) - 1)] - xs[lo])


# ----------------------------------------------------------------------------- style helpers
def style():
    plt.rcParams.update({
        "font.family": "DejaVu Sans", "font.size": 9.5, "text.color": INK,
        "axes.titlesize": 10, "axes.titlelocation": "left", "axes.titlepad": 7, "axes.titlecolor": INK,
        "axes.labelsize": 9.5, "axes.labelcolor": INK2, "axes.edgecolor": SPINE,
        "axes.linewidth": 0.8, "axes.spines.top": False, "axes.spines.right": False,
        "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.7, "axes.axisbelow": True,
        "xtick.color": SPINE, "ytick.color": SPINE, "xtick.labelcolor": INK2, "ytick.labelcolor": INK2,
        "xtick.labelsize": 8.5, "ytick.labelsize": 8.5, "legend.frameon": False, "legend.fontsize": 8.5,
        "lines.linewidth": 2.0, "lines.solid_capstyle": "round", "lines.solid_joinstyle": "round",
        "figure.facecolor": "white", "axes.facecolor": "white", "savefig.facecolor": "white",
        "figure.dpi": DPI, "savefig.dpi": DPI, "axes.unicode_minus": True,
    })


def frame(fig, title, subtitle=None, foot=None):
    """Figure title (top left), optional subtitle and footnote, at fixed distances in inches."""
    h = fig.get_figheight()
    fig.text(0.012, 1 - 0.20 / h, title, ha="left", va="top", fontsize=12, fontweight="bold", color=INK)
    if subtitle:
        fig.text(0.012, 1 - 0.47 / h, subtitle, ha="left", va="top", fontsize=9, color=INK2, linespacing=1.4)
    if foot:
        fig.text(0.012, 0.10 / h, foot, ha="left", va="bottom", fontsize=8, color=MUTED, linespacing=1.4)


def log_axis(axis, ticks, fmt=None):
    """On a log-scaled axis: plain-number major labels, unlabeled minor ticks (errata C4)."""
    axis.set_major_locator(ticker.FixedLocator(ticks))
    axis.set_major_formatter(ticker.FuncFormatter(fmt or (lambda v, _: f"{v:,g}")))
    axis.set_minor_formatter(ticker.NullFormatter())


def point(ax, x, y, color, marker, n3, xerr=None, yerr=None, ms=None):
    """Filled marker with min-max error bars for a 3-run median; hollow marker for n=1.

    Filled markers are drawn smaller and on top, so a hollow n=1 marker at the same place stays visible
    as a ring around it."""
    if n3:
        if xerr is not None or yerr is not None:
            ax.errorbar([x], [y], xerr=xerr, yerr=yerr, fmt="none", ecolor=color, elinewidth=1.3,
                        capsize=3, capthick=1.3, zorder=6)
        ax.plot([x], [y], ls="none", marker=marker, ms=ms or 6.5, mfc=color, mec="white", mew=1.0, zorder=7)
    else:
        ax.plot([x], [y], ls="none", marker=marker, ms=ms or 8.5, mfc="white", mec=color, mew=1.7, zorder=5)


def key_marker(color, marker, filled, ls="none", lw=2.0, label=""):
    return Line2D([], [], color=color, ls=ls, lw=lw, marker=marker, ms=7 if filled else 8, mew=1.0 if filled else 1.7,
                  mfc=color if filled else "white", mec="white" if filled else color, label=label)


def lead(ax, text, xy, xytext, ha="left", va="center", size=8, coords="offset points", **kw):
    """A label in ink colors with a thin leader line to the point it describes."""
    return ax.annotate(text, xy=xy, xytext=xytext, textcoords=coords, ha=ha, va=va, fontsize=size, color=INK2,
                       linespacing=1.15, arrowprops=dict(arrowstyle="-", color=MUTED, lw=0.7, shrinkA=1.5,
                                                         shrinkB=3.5), zorder=8, **kw)


def label(ax, text, xy, off, ha="left", va="center", size=8):
    """A label at an offset (points) from a data point, no leader."""
    return ax.annotate(text, xy=xy, xytext=off, textcoords="offset points", ha=ha, va=va, fontsize=size, color=INK2,
                       linespacing=1.15, zorder=8)


def check_inside(fig, name):
    """Stop if any drawn text would be cut off at the figure edge (tick labels outside the view are not drawn)."""
    fig.canvas.draw()
    r = fig.canvas.get_renderer()
    hidden = set()
    for ax in fig.axes:
        for axis in (ax.xaxis, ax.yaxis):
            lo, hi = sorted(axis.get_view_interval())
            for tick in axis.get_major_ticks() + axis.get_minor_ticks():
                if not lo - 1e-9 <= tick.get_loc() <= hi + 1e-9:
                    hidden.update((id(tick.label1), id(tick.label2)))
    w, h = fig.bbox.width, fig.bbox.height
    for t in fig.findobj(Text):
        if id(t) in hidden or not t.get_visible() or not t.get_text().strip():
            continue
        bb = t.get_window_extent(r)
        if bb.x0 < 0 or bb.y0 < 0 or bb.x1 > w or bb.y1 > h:
            raise SystemExit(f"{name}: text outside the figure: {t.get_text()[:60]!r}")


def save(fig, out, name):
    check_inside(fig, name)
    os.makedirs(out, exist_ok=True)
    path = os.path.join(out, name)
    fig.savefig(path, dpi=DPI, metadata={"Software": None})
    plt.close(fig)
    print("wrote", path)
    return path


# ----------------------------------------------------------------------------- hero
def fig_hero(out, N, pr, runs):
    """q2, default r2 vs patch r2: one row per request (waiting, generating, stalled)."""
    spec = [("default", N.value("hero.run_default"), L_DEFAULT), ("mine", N.value("hero.run_mine"), L_PATCH)]
    medians = [median_run(runs, "reasoning_q2__default"), median_run(runs, "reasoning_q2__mine_a1.0")]
    if medians != [run for _, run, _ in spec]:
        raise SystemExit("hero.run_* in numbers.json are not the median-goodput runs")
    H = 6.2
    fig, axes = plt.subplots(1, 2, figsize=(WIDTH, H), sharex=True, sharey=True)
    fig.subplots_adjust(left=0.07, right=0.985, top=1 - 1.62 / H, bottom=0.86 / H, wspace=0.07)
    longest = N.value("q2.longest.rid")
    for ax, (key, run, title) in zip(axes, spec):
        rows = sorted(pr[run], key=lambda r: float(r["submit_t"]))
        if len(ok_rows(rows)) != len(rows):
            raise SystemExit(f"{run}: failed requests")
        wait, gen, stall, gaps = [], [], [], []
        for i, r in enumerate(rows):
            s, f, e = float(r["submit_t"]), float(r["first_token_t"]), float(r["end_t"])
            wait.append([(s, i), (f, i)])
            gen.append([(f, i), (e, i)])
            if int(r["n_gaps_gt5s"]) > 1:
                raise SystemExit(f"{run} {r['rid']}: more than one gap > 5 s (per_request keeps only the largest)")
            g = float(r["max_gap_s"] or 0)
            if g > STALL_S:
                g0 = float(r["max_gap_start_s"])
                stall.append([(g0, i), (g0 + g, i)])
                gaps.append(g)
        for segs, color, z in ((wait, C_WAIT, 2), (gen, C_GEN, 3), (stall, C_STALL, 4)):
            ax.add_collection(LineCollection(segs, colors=color, linewidths=1.55, capstyle="butt", zorder=z))
        N.agree(f"hero.{key}.stalled", len(gaps))
        N.agree(f"hero.{key}.wait10", sum(1 for r in rows if float(r["ttft_s"]) > WAIT_S))
        if key == "default":
            stats = (f"{N('hero.default.stalled')} stalls of "
                     f"{N.agree('hero.default.stall_range_s', min(gaps), max(gaps))} s")
        else:
            stats = f"{N('hero.mine.stalled')} stalls"
        stats += f"  \u00b7  {N(f'hero.{key}.wait10')} requests wait >10 s to start"
        ax.annotate(title, xy=(0, 1), xycoords="axes fraction", xytext=(0, 22), textcoords="offset points",
                    ha="left", va="bottom", fontsize=10.5, fontweight="bold", color=INK)
        ax.annotate(stats, xy=(0, 1), xycoords="axes fraction", xytext=(0, 7), textcoords="offset points",
                    ha="left", va="bottom", fontsize=9, color=INK2)
        # the last token of the run ends the goodput denominator
        span = max(float(r["end_t"]) for r in rows) - min(float(r["submit_t"]) for r in rows)
        wall = N.agree(f"q2.{key}.wall_s", span)
        ax.axvline(span, color=INK2, lw=0.8, ls=(0, (2, 2)), zorder=1)
        ax.text(span - 3, 1, f"last token {wall} s", rotation=90, ha="right", va="top", fontsize=8, color=INK2)
        # the longest request finishes last in both runs
        i64 = next(i for i, r in enumerate(rows) if r["rid"] == longest)
        N.agree("q2.longest.out_tokens", rows[i64]["completion_tokens"])
        if max(rows, key=lambda r: float(r["end_t"]))["rid"] != longest:
            raise SystemExit(f"{run}: {longest} is not the last request to finish")
        lead(ax, f"{longest} ({N('q2.longest.out_tokens')} tokens)\nfinishes last",
             (float(rows[i64]["end_t"]) - 1.5, i64), (span - 60, i64 - 15), ha="center", va="bottom", coords="data")
        ax.set_xlim(0, 300)
        ax.set_ylim(len(rows) - 0.5, -0.5)
        ax.xaxis.set_major_locator(ticker.MultipleLocator(50))
        ax.set_yticks([0, 29, 59, 89, 119], ["1", "30", "60", "90", "120"])
        ax.grid(axis="y", visible=False)
        ax.set_xlabel("time since the first request was sent (s)")
    axes[0].set_ylabel("request, in arrival order")
    N.agree("trace.n_requests", len(pr[spec[0][1]]))
    frame(fig, "Retraction stalls move into queueing delay",
          f"q2 trace: {N('trace.n_requests')} requests sent at 2 req/s, one row per request. Both panels show the run "
          "with the median goodput of 3 (r2).",
          "Stall = a gap of more than 5 s between two streamed tokens. In the unpatched 09-07 sweep, each stall "
          f"matched one server retraction ({N('diag.stall_match')}).")
    keys = [(C_WAIT, "waiting for the first token"), (C_GEN, "generating"), (C_STALL, "stalled >5 s mid-generation")]
    fig.legend(handles=[Line2D([], [], color=c, lw=5, solid_capstyle="butt", label=t) for c, t in keys],
               loc="upper left", bbox_to_anchor=(0.012, 1 - 0.80 / H), ncol=3, fontsize=9, handlelength=2.2,
               columnspacing=2.0, borderaxespad=0, borderpad=0)
    return save(fig, out, "hero_timeline.png")


# ----------------------------------------------------------------------------- fig01
def fig01_output_len(out, N, pr):
    rows = ok_rows(pr["reasoning_q2__default_r1"])
    toks = sorted(int(r["completion_tokens"]) for r in rows)
    N.agree("trace.n_requests", len(toks))
    N.agree("trace.out_max", max(toks))
    N.agree("trace.out_total", sum(toks))
    N.agree("trace.out_mean", statistics.fmean(toks))
    pt = [int(r["prompt_tokens"]) for r in rows]
    N.agree("trace.prompt_tokens_range", min(pt), max(pt))
    # the footnote's claim: every load level (reasoning, reasoning_q<Q>, det/...) sends these same requests
    lens = {r["rid"]: r["completion_tokens"] for r in rows}
    for name in sorted(pr):
        if re.fullmatch(r"(det/)?reasoning(_q[\d.]+)?__.+", name):
            if any(lens.get(r["rid"]) != r["completion_tokens"] for r in ok_rows(pr[name])):
                raise SystemExit(f"{name}: output lengths differ from reasoning_q2__default_r1")
    H = 4.0
    fig, ax = plt.subplots(figsize=(WIDTH, H))
    fig.subplots_adjust(left=0.07, right=0.975, top=1 - 1.14 / H, bottom=0.88 / H)
    # bins 0.05 decades wide; each bar leaves a 2 px gap (0.0027 decades at this scale), empty bins draw nothing
    lo, step, gap = 2.75, 0.05, 0.0027
    counts = defaultdict(int)
    for t in toks:
        counts[math.floor((math.log10(t) - lo) / step)] += 1
    if min(counts) < 0:
        raise SystemExit("an output length below the first bin")
    for i, c in sorted(counts.items()):
        left, right = 10 ** (lo + step * i + gap / 2), 10 ** (lo + step * (i + 1) - gap / 2)
        ax.bar(left, c, width=right - left, align="edge", color=C_DEFAULT, lw=0, zorder=3)
    ax.set_xscale("log")
    log_axis(ax.xaxis, [500, 1000, 2000, 5000, 10000])
    ax.set_xlim(10 ** 2.65, 10 ** 4.25)
    ax.set_ylim(0, 16)
    ax.yaxis.set_major_locator(ticker.MultipleLocator(4))
    for name, key, p, side in (("p50", "trace.out_p50", 50, 1), ("p99", "trace.out_p99", 99, -1),
                               ("max", "trace.out_max", None, 1)):
        v = float(N.value(key))
        if p is not None:
            N.agree(key, type7(toks, p))
        ax.axvline(v, color=INK, lw=1.0, ls=(0, (3, 2)), zorder=4)
        ax.text(v * 10 ** (0.008 * side), 15.6, f"{name} {N(key)}", ha="left" if side > 0 else "right", va="top",
                fontsize=8.5, color=INK)
    ax.set_xlabel("output tokens per request (log scale)")
    ax.set_ylabel("requests per bin")
    ax.grid(axis="x", visible=False)
    frame(fig, "Output lengths of the trace",
          f"{N('trace.n_requests')} requests with prompts of {N('trace.prompt_tokens_range')} tokens; mean output "
          f"{N('trace.out_mean')} tokens, {N('trace.out_total')} in total.\n"
          "ignore_eos=True: each request generates exactly its max_new_tokens, so the gate reserves known lengths "
          "(an oracle).",
          "Every load level of the study sends these same requests; only the arrival times differ. "
          "Bins are 0.05 decades wide.")
    return save(fig, out, "fig01_output_len.png")


# ----------------------------------------------------------------------------- fig02
def fig02_sweep(out, N, sweep):
    rows = sorted(sweep, key=lambda r: r["qps"])
    q = [r["qps"] for r in rows]
    qk = [f"q{x:g}" for x in q]
    for r, k in zip(rows, qk):
        N.agree(f"sweep0907.retractions.{k}", r["retractions"])
        N.agree(f"sweep0907.ttft_p99_s.{k}", r["ttft_p99_s"])
        N.agree(f"sweep0907.maxitl_p99_s.{k}", r["maxitl_p99_s"])
    N.agree("diag.first_retraction_qps", min(r["qps"] for r in rows if r["retractions"] > 0))
    lam = [float(x) for x in N.value("diag.little_lambda_max").split(EN)]
    N.agree("diag.little_lambda_max", *lam)

    H = 6.1
    fig, (a1, a2) = plt.subplots(2, 1, figsize=(WIDTH, H), sharex=True,
                                 gridspec_kw=dict(height_ratios=[1, 1.3], hspace=0.22))
    fig.subplots_adjust(left=0.08, right=0.975, top=1 - 1.12 / H, bottom=1.02 / H)
    for ax in (a1, a2):
        ax.axvspan(lam[0], lam[1], color=BAND, lw=0, zorder=0)
        ax.set_xscale("log")
        log_axis(ax.xaxis, q)
        ax.set_xlim(0.42, 9.6)
        ax.grid(axis="x", visible=False)
    # retractions: one bar per load, 0.04 decades wide on the log axis
    half = 10 ** 0.02
    a1.bar([x / half for x in q], [r["retractions"] for r in rows], width=[x * half - x / half for x in q],
           align="edge", color=C_DEFAULT, zorder=3)
    for x, k, r in zip(q, qk, rows):
        a1.text(x, r["retractions"] + 0.9, N(f"sweep0907.retractions.{k}"), ha="center", va="bottom", fontsize=8.5,
                color=INK)
    a1.set_ylim(0, 37)
    a1.yaxis.set_major_locator(ticker.MultipleLocator(10))
    a1.set_ylabel("retractions")
    a1.set_title("Requests retracted (preempted) by the server, per run")
    lead(a1, f"first retraction\nat {N('diag.first_retraction_qps')} req/s", (0.6, 5.0), (0.6, 14), ha="center",
         va="bottom", coords="data")
    a1.text(lam[1] * 1.04, 34.5, f"shaded: Little's-law limit\n{LAMBDA} = {N('diag.little_lambda_max')} req/s",
            ha="left", va="top", fontsize=8.5, color=INK2, linespacing=1.3)
    # latency lines on a log axis; hollow markers (n=1 per load)
    series = [("ttft_p99_s", "TTFT p99 (time to first token)", INK2, M_DEFAULT),
              ("maxitl_p99_s", "maxITL p99 (worst gap between two tokens)", C_STALL, M_ABLATED)]
    handles = []
    for col, lab, color, marker in series:
        ys = [r[col] for r in rows]
        a2.plot(q, ys, color=color, lw=1.8, zorder=3)
        a2.plot(q, ys, ls="none", marker=marker, ms=7, mfc="white", mec=color, mew=1.6, zorder=4)
        handles.append(Line2D([], [], color=color, lw=1.8, marker=marker, ms=7, mfc="white", mec=color, mew=1.6,
                              label=lab))
    a2.set_yscale("log")
    log_axis(a2.yaxis, [0.1, 1, 10, 100], lambda v, _: f"{v:g}")
    a2.set_ylim(0.035, 300)
    for k, col, off, ha, va in (("q0.5", "ttft_p99_s", (-7, 2), "right", "bottom"),
                                ("q0.6", "ttft_p99_s", (-6, 6), "right", "bottom"),
                                ("q8", "ttft_p99_s", (0, -9), "center", "top"),
                                ("q0.6", "maxitl_p99_s", (7, -3), "left", "top"),
                                ("q0.8", "maxitl_p99_s", (7, -3), "left", "top"),
                                ("q8", "maxitl_p99_s", (0, 9), "center", "bottom")):
        r = rows[qk.index(k)]
        label(a2, N(f"sweep0907.{col}.{k}"), (r["qps"], r[col]), off, ha=ha, va=va)
    a2.legend(handles=handles, loc="lower right", fontsize=8.5, handlelength=2.4, borderaxespad=0.6)
    a2.set_ylabel("seconds (log scale)")
    a2.set_title("Tail latency per run")
    a2.set_xlabel("arrival rate (req/s, log scale)")
    for r in rows:
        N.agree("trace.n_requests", r["n_ok"])
    frame(fig, f"Unpatched SGLang: retractions start at {N('diag.first_retraction_qps')} req/s",
          f"SGLang v0.5.18 without the patch (09-07): the same {N('trace.n_requests')} requests sent faster. "
          "One run per load (n=1).",
          f"{LAMBDA} = KV pool / (TPOT \u00d7 mean KV token-steps per request), with an assumed TPOT of 20\u201324 ms "
          "and a 387-token prompt: by Little's law,\nthe arrival rate above which the running requests no longer fit "
          "in the KV pool on average.")
    return save(fig, out, "fig02_sweep_0907.png")


# ----------------------------------------------------------------------------- fig03
LOADS = [(0.35, "q0.35", "reasoning"), (0.5, "q0.5", "reasoning_q0.5"), (1.0, "q1", "reasoning_q1"),
         (2.0, "q2", "reasoning_q2"), (4.0, "q4", "reasoning_q4")]


def fig03_load(out, N, runs, grouped, pr):
    bars = [("default", "default", C_DEFAULT, M_DEFAULT, "-", L_DEFAULT),
            ("ablated", "ablated", C_ABLATED, M_ABLATED, "--", L_ABLATED),
            ("mine", "mine_a1.0", C_PATCH, M_PATCH, "-", L_PATCH),
            ("tuned_n40", "tuned_n40", C_TUNED, M_TUNED, "-", L_TUNED + " (n=1)")]
    H = 4.8
    fig, ax = plt.subplots(figsize=(WIDTH, H))
    fig.subplots_adjust(left=0.08, right=0.975, top=1 - 1.05 / H, bottom=0.90 / H)
    pts = {}
    for bar, tag, color, marker, ls, _ in bars:
        line = []
        for qps, qk, stem in LOADS:
            key = f"load.{qk}.{bar}.goodput"
            if key not in N.d:
                continue
            if qk == "q2" and bar != "tuned_n40":
                g = grouped[f"{stem}__{tag}"]
                y, lo, hi, n3 = g["goodput_rps"], g["goodput_rps_min"], g["goodput_rps_max"], True
            elif qk == "q0.35" and bar == "default":
                gp = [runs[f"reasoning__upstream_r{i}"]["goodput_rps"] for i in (1, 2, 3)]
                y, lo, hi, n3 = statistics.median(gp), min(gp), max(gp), True
            else:
                y, lo, hi, n3 = runs[f"{stem}__{tag}_r1"]["goodput_rps"], None, None, False
            N.agree(key, y)
            if N.entry(key)["n"] != (3 if n3 else 1):
                raise SystemExit(f"{key}: n in numbers.json is {N.entry(key)['n']}")
            pts[(qk, bar)] = (qps, y)
            line.append((qps, y, lo, hi, n3))
        ax.plot([p[0] for p in line], [p[1] for p in line], color=color, ls=ls, lw=2.2 if bar == "mine" else 1.8,
                zorder=3 if bar == "mine" else 2)
        for qps, y, lo, hi, n3 in line:
            point(ax, qps, y, color, marker, n3, yerr=[[y - lo], [hi - y]] if n3 else None)
    ax.set_xscale("log")
    log_axis(ax.xaxis, [x for x, _, _ in LOADS])
    ax.set_xlim(0.30, 4.9)
    ax.set_ylim(0.285, 0.50)
    ax.yaxis.set_major_locator(ticker.FixedLocator([0.32, 0.36, 0.40, 0.44, 0.48]))
    ax.yaxis.set_major_formatter(ticker.FuncFormatter(lambda v, _: f"{v:.2f}"))
    ax.grid(axis="x", visible=False)
    ax.set_xlabel("arrival rate (req/s, log scale)")
    ax.set_ylabel("goodput (req/s)")
    # selective labels: the q2 values, and the patch's goodput change at each load with KV pressure
    label(ax, N("load.q2.mine.goodput"), pts[("q2", "mine")], (-8, 7), ha="right", va="bottom", size=8.5)
    label(ax, N("load.q2.default.goodput"), pts[("q2", "default")], (-8, -7), ha="right", va="top", size=8.5)
    label(ax, N("load.q2.tuned_n40.goodput"), pts[("q2", "tuned_n40")], (8, -4), ha="left", va="top", size=8.5)
    for qk, key, n in (("q1", "load.q1.gain_pct", "n=1"), ("q2", "q2.gain.goodput_pct", "median of 3"),
                       ("q4", "load.q4.gain_pct", "n=1")):
        N.agree(key, 100 * (pts[(qk, "mine")][1] / pts[(qk, "default")][1] - 1))
        ax.text(pts[(qk, "mine")][0], 0.493, f"patch vs default\n{N(key)}% ({n})", ha="center", va="top",
                fontsize=8, color=INK2, linespacing=1.15)
    for stem in ("reasoning", "reasoning_q0.5"):
        if runs[f"{stem}__mine_a1.0_r1"]["peak_delays"] != 0:
            raise SystemExit(f"{stem}: the gate delayed requests")
    lead(ax, "default, ablated and\npatch coincide: the\ngate never delays\na request", pts[("q0.5", "mine")],
         (0.31, 0.415), ha="left", va="top", coords="data")
    handles = [key_marker(c, m, True, ls=ls, lw=1.8, label=lab) for _, _, c, m, ls, lab in bars]
    handles += [key_marker(INK2, "o", True, label="median of 3 runs (bars: min\u2013max)"),
                key_marker(INK2, "o", False, label="one run (n=1)")]
    ax.legend(handles=handles, loc="lower right", ncol=2, fontsize=8.5, handlelength=2.6, columnspacing=1.5,
              borderaxespad=0.5)
    for r in pr["reasoning_q2__default_r1"]:
        N.agree("trace.tpot_slo_ms", r["tpot_slo_ms"])
    frame(fig, "Goodput across loads",
          f"goodput = requests that met the mean-TPOT \u2264 {N('trace.tpot_slo_ms')} ms SLO, divided by the wall "
          "time of the run. 09-13 runs, one server start per run.",
          "q0.35 default = unpatched upstream, 3-run median. q2 points of default, ablated and patch: median of 3; "
          "all other points: one run.")
    return save(fig, out, "fig03_load_curve.png")


# ----------------------------------------------------------------------------- fig04
def fig04_tradeoff(out, N, grouped):
    alphas = ["0.5", "0.6", "0.75", "0.9", "1.0"]
    tuned = ["24", "32", "40", "48"]
    P = [("default", "default", C_DEFAULT, M_DEFAULT, True), ("ablated", "ablated", C_ABLATED, M_ABLATED, True)]
    P += [("mine" if a == "1.0" else f"mine_a{a}", f"mine_a{a}", C_PATCH, M_PATCH, a == "1.0") for a in alphas]
    P += [(f"tuned_n{n}", f"tuned_n{n}", C_TUNED, M_TUNED, False) for n in tuned]
    P += [("noradix", "noradix", C_NORADIX, M_NORADIX, False), ("cap65k", "cap65k", C_CAP, M_CAP, False)]
    data = {}
    for key, cond, color, marker, n3 in P:
        g = grouped[f"reasoning_q2__{cond}"]
        if int(g["reps"]) != (3 if n3 else 1):
            raise SystemExit(f"{cond}: {g['reps']} repetitions in w3_q2_grouped.csv")
        d = dict(x=g["ttft_mean"] / 1000, y=g["retracted"], z=g["maxitl_p99"] / 1000,
                 xlo=g["ttft_mean_min"] / 1000, xhi=g["ttft_mean_max"] / 1000, ylo=g["retracted_min"],
                 yhi=g["retracted_max"], zlo=g["maxitl_p99_min"] / 1000, zhi=g["maxitl_p99_max"] / 1000)
        N.agree(f"q2.{key}.ttft_mean_s", d["x"])
        N.agree(f"q2.{key}.retractions", d["y"])
        N.agree(f"q2.{key}.maxitl_p99_s", d["z"])
        data[cond] = d
    for a in ("0.5", "0.6"):   # "the gate never fired": no logged delay at alpha <= 0.6
        if N.agree(f"q2.mine_a{a}.peak_delays", grouped[f"reasoning_q2__mine_a{a}"]["peak_delays"]) != "0":
            raise SystemExit(f"alpha={a}: the gate delayed requests")

    H = 5.3
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(WIDTH, H), sharex=True)
    fig.subplots_adjust(left=0.07, right=0.985, top=1 - 1.42 / H, bottom=1.18 / H, wspace=0.2)
    for ax, k in ((a1, "y"), (a2, "z")):
        for conds, color in (([f"mine_a{a}" for a in alphas], C_PATCH), ([f"tuned_n{n}" for n in tuned], C_TUNED)):
            ax.plot([data[c]["x"] for c in conds], [data[c][k] for c in conds], color=color, lw=1.0, alpha=0.5,
                    zorder=2)
        for key, cond, color, marker, n3 in P:
            d = data[cond]
            xerr = [[d["x"] - d["xlo"]], [d["xhi"] - d["x"]]] if n3 else None
            yerr = [[d[k] - d[k + "lo"]], [d[k + "hi"] - d[k]]] if n3 else None
            point(ax, d["x"], d[k], color, marker, n3, xerr, yerr)
    A = "\u03b1"
    xy = lambda c, k: (data[c]["x"], data[c][k])  # noqa: E731
    # left: retractions (labels placed by hand so that none covers a marker)
    lead(a1, f"default, ablated, {A}=0.5, {A}=0.6\n({A} \u2264 0.6: the gate never fired)", (19.2, 21.8),
         (13.6, 26.6), ha="left", va="center", coords="data")
    label(a1, f"{A}=0.75", xy("mine_a0.75", "y"), (9, 0))
    label(a1, f"{A}=0.9", xy("mine_a0.9", "y"), (9, 2))
    label(a1, "N=48", xy("tuned_n48", "y"), (9, -2))
    lead(a1, f"{A}=1.0 and --disable-radix-cache", (30.4, -0.8), (33.5, -3.6), ha="left", va="center",
         coords="data")
    for n in ("40", "32", "24"):
        label(a1, f"N={n}", xy(f"tuned_n{n}", "y"), (0, 10), ha="center", va="bottom")
    label(a1, "KV pool capped", xy("cap65k", "y"), (0, -11), ha="center", va="top")
    a1.set_ylim(-6, 29.5)
    a1.yaxis.set_major_locator(ticker.FixedLocator([0, 5, 10, 15, 20, 25]))
    a1.set_ylabel("retractions per run")
    a1.set_title("Retractions")
    # right: maxITL p99
    lead(a2, f"default, ablated, {A}=0.5, {A}=0.6", (19.0, 80), (14.0, 260), ha="left", va="bottom", coords="data")
    lead(a2, f"{A}=0.75", (21.8, 48), (20.6, 22), ha="center", va="top", coords="data")
    lead(a2, f"{A}=0.9", (26.6, 46), (24.4, 7.5), ha="center", va="top", coords="data")
    label(a2, "N=48", xy("tuned_n48", "z"), (9, 0))
    label(a2, "KV pool capped", xy("cap65k", "z"), (9, 0))
    lead(a2, f"{A}=1.0 and\n--disable-radix-cache", (29.7, 0.112), (-14, 18), ha="right", va="bottom")
    label(a2, "N=40", xy("tuned_n40", "z"), (8, -6), ha="left", va="top")
    label(a2, "N=32", xy("tuned_n32", "z"), (0, -10), ha="center", va="top")
    label(a2, "N=24", xy("tuned_n24", "z"), (0, 10), ha="center", va="bottom")
    a2.set_yscale("log")
    log_axis(a2.yaxis, [0.1, 1, 10, 100], lambda v, _: f"{v:g}")
    a2.set_ylim(0.025, 450)
    a2.set_ylabel("maxITL p99 (s, log scale)")
    a2.set_title("Worst gap between two tokens (maxITL p99)")
    for ax in (a1, a2):
        ax.set_xlim(13, 69)
        ax.xaxis.set_major_locator(ticker.MultipleLocator(10))
        ax.set_xlabel("mean time to first token (s)")
    handles = [key_marker(C_DEFAULT, M_DEFAULT, True, label="default (flag off)"),
               key_marker(C_ABLATED, M_ABLATED, True, label="ablated"),
               key_marker(C_PATCH, M_PATCH, False, ls="-", lw=1.0, label=f"patch, reserve ratio {A}"),
               key_marker(C_TUNED, M_TUNED, False, ls="-", lw=1.0, label="--max-running-requests N"),
               key_marker(C_NORADIX, M_NORADIX, False, label="--disable-radix-cache"),
               key_marker(C_CAP, M_CAP, False, label=f"KV pool capped at {N('kv.cap65k_pool_tokens')} tokens")]
    fig.legend(handles=handles, loc="lower left", bbox_to_anchor=(0.06, 0.10 / H), ncol=3, fontsize=8.5,
               columnspacing=1.6, handlelength=2.2, borderaxespad=0)
    frame(fig, "q2 trade-off: each setting that removes retractions makes requests wait longer to start",
          "Filled = median of 3 runs (bars: min\u2013max); hollow = one run (n=1).\n"
          f"{A} = share of each request's known remaining output that the gate reserves; N = --max-running-requests.")
    return save(fig, out, "fig04_tradeoff.png")


# ----------------------------------------------------------------------------- fig05
def fig05_decomposition(out, N, cum, pr):
    curves = {}
    for bar, tag in (("default", "default"), ("mine", "mine_a1.0")):
        reps = [cum[f"reasoning_q2__{tag}_r{i}"] for i in (1, 2, 3)]
        curves[bar] = [statistics.median(r[p] for r in reps) for p in range(1, 101)]
        for p in (50, 90, 99):
            N.agree(f"q2.cum.{bar}_t{p}_s", curves[bar][p - 1])
        N.agree(f"q2.{bar}.wall_s", curves[bar][99])
    for p in (50, 90):
        N.agree(f"q2.cum.t{p}_mine_minus_default_s", curves["mine"][p - 1] - curves["default"][p - 1])
    # the right panel's numbers, recomputed from the per-request rows
    rid = N.value("q2.longest.rid")
    d_runs = [ok_rows(pr[f"reasoning_q2__default_r{i}"]) for i in (1, 2, 3)]
    m_runs = [ok_rows(pr[f"reasoning_q2__mine_a1.0_r{i}"]) for i in (1, 2, 3)]

    def met(rows, drop=None):
        return sum(1 for r in rows if r["slo_ok"] == "1" and r["rid"] != drop)

    def span(rows, drop=None):
        rr = [r for r in rows if r["rid"] != drop]
        return max(float(r["end_t"]) for r in rr) - min(float(r["submit_t"]) for r in rr)
    md, mm = statistics.median(met(r) for r in d_runs), statistics.median(met(r) for r in m_runs)
    wd, wm = statistics.median(span(r) for r in d_runs), statistics.median(span(r) for r in m_runs)
    N.agree("q2.default.slo_met", md)
    N.agree("q2.mine.slo_met", mm)
    N.agree("q2.gain.slo_factor", mm / md)
    N.agree("q2.gain.wall_factor", wd / wm)
    N.agree("q2.gain.goodput_pct", 100 * (mm / md * wd / wm - 1))
    N.agree("q2.gain.out_tok_s_pct", 100 * (wd / wm - 1))
    ex = [statistics.median(met(r, rid) / span(r, rid) for r in runs_) for runs_ in (d_runs, m_runs)]
    N.agree("q2.excl_longest.gain_pct", 100 * (ex[1] / ex[0] - 1))
    N.agree("q2.longest.default_stall_s", statistics.median(
        float(next(x for x in r if x["rid"] == rid)["max_gap_s"]) for r in d_runs))
    N.agree("q2.longest.mine_ttft_s", statistics.median(
        float(next(x for x in r if x["rid"] == rid)["ttft_s"]) for r in m_runs))
    for runs_ in (d_runs, m_runs):
        if any(max(r, key=lambda x: float(x["end_t"]))["rid"] != rid for r in runs_):
            raise SystemExit(f"{rid} is not the last request to finish in every q2 run")

    H = 4.9
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(WIDTH, H), gridspec_kw=dict(width_ratios=[1.0, 1.0], wspace=0.34))
    fig.subplots_adjust(left=0.075, right=0.965, top=1 - 1.30 / H, bottom=1.05 / H)
    # left: cumulative output
    pct = list(range(1, 101))
    for bar, color, lab in (("default", C_DEFAULT, L_DEFAULT), ("mine", C_PATCH, L_PATCH)):
        a1.plot([0] + curves[bar], [0] + pct, color=color, lw=2.0, label=lab, zorder=3 if bar == "mine" else 2)
    for p in (50, 90, 100):
        if p < 100:
            a1.axhline(p, color=SPINE, lw=0.8, ls=(0, (2, 2)), zorder=1)
        for bar, color in (("default", C_DEFAULT), ("mine", C_PATCH)):
            a1.plot([curves[bar][p - 1]], [p], ls="none", marker="o", ms=5.5, mfc=color, mec="white", mew=1.0,
                    zorder=4)
    # direct labels at the dots: default left of patch at 50% and 90%, patch left of default at the last token
    for p, k in ((50, "t50"), (90, "t90")):
        label(a1, f"{N(f'q2.cum.default_{k}_s')} s", (curves["default"][p - 1], p), (-5, 5), ha="right",
              va="bottom")
        label(a1, f"{N(f'q2.cum.mine_{k}_s')} s", (curves["mine"][p - 1], p), (5, -5), ha="left", va="top")
    label(a1, f"{N('q2.mine.wall_s')} s", (curves["mine"][99], 100), (-4, 6), ha="right", va="bottom")
    label(a1, f"{N('q2.default.wall_s')} s", (curves["default"][99], 100), (2, 6), ha="left", va="bottom")
    a1.set_xlim(0, 320)
    a1.set_ylim(0, 112)
    a1.yaxis.set_major_locator(ticker.FixedLocator([0, 25, 50, 75, 90, 100]))
    a1.yaxis.set_major_formatter(ticker.FuncFormatter(lambda v, _: f"{v:g}%"))
    a1.xaxis.set_major_locator(ticker.MultipleLocator(50))
    a1.set_xlabel("time since the first request was sent (s)")
    a1.set_ylabel("share of all output tokens received")
    a1.set_title("Output tokens received over time")
    a1.legend(loc="upper left", fontsize=8.5, handlelength=1.8, borderaxespad=0.3)

    # right: +9.1% = SLO-met factor x wall factor; the segments split it in proportion to ln(factor)
    total = float(N.value("q2.gain.goodput_pct"))
    sf, wf = float(N.value("q2.gain.slo_factor")), float(N.value("q2.gain.wall_factor"))
    s_len = total * math.log(sf) / (math.log(sf) + math.log(wf))
    h = 0.36
    y0, y1, y2 = 2.0, 1.0, 0.0
    a2.barh(y0, s_len, height=h, color=C_PATCH, zorder=3)
    a2.barh(y0, total - s_len, left=s_len, height=h, color=C_WALL, zorder=3)
    a2.plot([s_len, s_len], [y0 - h / 2, y0 + h / 2], color="white", lw=2.5, zorder=4)
    a2.text(s_len / 2, y0, f"\u00d7{N('q2.gain.slo_factor')}", ha="center", va="center", fontsize=8.5, color="white",
            fontweight="bold", zorder=5)
    a2.text((s_len + total) / 2, y0, f"\u00d7{N('q2.gain.wall_factor')}", ha="center", va="center", fontsize=8.5,
            color=INK, fontweight="bold", zorder=5)
    a2.text(s_len / 2, y0 + h / 2 + 0.08, f"SLO met\n{N('q2.default.slo_met')} \u2192 {N('q2.mine.slo_met')}",
            ha="center", va="bottom", fontsize=8, color=INK2, linespacing=1.15)
    a2.text((s_len + total) / 2, y0 + h / 2 + 0.08,
            f"wall time\n{N('q2.default.wall_s')} \u2192 {N('q2.mine.wall_s')} s", ha="center", va="bottom",
            fontsize=8, color=INK2, linespacing=1.15)
    for y, key, color in ((y0, "q2.gain.goodput_pct", None), (y1, "q2.excl_longest.gain_pct", C_PATCH),
                          (y2, "q2.gain.out_tok_s_pct", C_WALL)):
        v = float(N.value(key))
        if color:
            a2.barh(y, v, height=h, color=color, zorder=3)
        a2.text(v + 0.2, y, f"{N(key)}%", ha="left", va="center", fontsize=9.5, color=INK, fontweight="bold")
    a2.text(0.15, y2 - h / 2 - 0.1, "same tokens in a shorter wall time", ha="left", va="top", fontsize=8,
            color=INK2)
    a2.text(0.15, y1 - h / 2 - 0.1, f"{rid} left out of both runs", ha="left", va="top", fontsize=8, color=INK2)
    a2.set_yticks([y0, y1, y2], ["goodput", f"goodput without\n{rid}", "output tokens/s"])
    a2.tick_params(axis="y", length=0, labelsize=8.5, labelcolor=INK)
    a2.set_ylim(-0.75, 2.95)
    a2.set_xlim(0, 11.4)
    a2.xaxis.set_major_locator(ticker.MultipleLocator(2))
    a2.xaxis.set_major_formatter(ticker.FuncFormatter(lambda v, _: f"+{v:g}%" if v else "0"))
    a2.grid(axis="y", visible=False)
    a2.spines["left"].set_visible(False)
    a2.set_xlabel("change vs default")
    a2.set_title("Goodput gain = SLO-met factor \u00d7 wall factor")
    frame(fig, f"Where the {N('q2.gain.goodput_pct')}% goodput comes from (q2, median of 3 runs)",
          "The patch does not make tokens arrive faster: it reaches 50% of them "
          f"{N('q2.cum.t50_mine_minus_default_s')} s later and 90% {N('q2.cum.t90_mine_minus_default_s')} s later. "
          "The run ends earlier\nbecause its last "
          f"request, {rid}, waits {N('q2.longest.mine_ttft_s')} s to start instead of stalling "
          f"{N('q2.longest.default_stall_s')} s mid-answer. That is about half of the gain.",
          "Left: pointwise median of the 3 runs of each bar. Right: goodput = SLO-met requests / wall time, so the "
          "two factors multiply;\nthe segment lengths split the total in proportion to ln(factor).")
    return save(fig, out, "fig05_goodput_decomposition.png")


# ----------------------------------------------------------------------------- fig06
def fig06_ttft(out, N, pr, runs):
    rd, rm = median_run(runs, "reasoning_q2__default"), median_run(runs, "reasoning_q2__mine_a1.0")
    if (rd, rm) != (N.value("hero.run_default"), N.value("hero.run_mine")):
        raise SystemExit("the median-goodput runs are not the runs of the timeline figure")
    spec = [(rd, f"{L_DEFAULT}, r2", C_DEFAULT, "-", "hero.default.wait10", 2.2),
            (rm, f"{L_PATCH}, r2", C_PATCH, "-", "hero.mine.wait10", 2.4),
            ("reasoning_q2__noradix_r1", f"{L_NORADIX} (n=1)", C_NORADIX, (0, (7, 2, 1.5, 2)), "q2.noradix.wait10",
             1.9),
            ("reasoning_q2__tuned_n40_r1", f"{L_TUNED} (n=1)", C_TUNED, (0, (1.2, 1.4)), "q2.tuned_n40.wait10", 2.2)]
    H = 4.7
    fig, ax = plt.subplots(figsize=(WIDTH, H))
    fig.subplots_adjust(left=0.08, right=0.695, top=1 - 1.12 / H, bottom=0.72 / H)
    ax.axvline(WAIT_S, color=SPINE, lw=0.9, zorder=1)
    ax.text(WAIT_S * 1.06, 1.5, "10 s", ha="left", va="bottom", fontsize=8, color=MUTED)
    handles = []
    for run, lab, color, ls, wkey, lw in spec:
        t = sorted(fcol(ok_rows(pr[run]), "ttft_s"))
        N.agree(wkey, sum(1 for x in t if x > WAIT_S))
        xs, ys = [t[0]], [0]
        for i, x in enumerate(t):
            xs += [x, x]
            ys += [i, i + 1]
        ax.plot(xs + [1000], ys + [len(t)], color=color, ls=ls, lw=lw, zorder=4 if color == C_PATCH else 3,
                solid_joinstyle="miter", dash_capstyle="butt")
        handles.append(Line2D([], [], color=color, ls=ls, lw=lw, label=f"{lab}\n{N(wkey)} requests wait >10 s"))
    t_d, t_m = fcol(ok_rows(pr[rd]), "ttft_s"), fcol(ok_rows(pr[rm]), "ttft_s")
    for run, *_ in spec:
        N.agree("trace.n_requests", len(pr[run]))
    N.agree("q2.ttft_bimodal.default_lt1s", sum(1 for x in t_d if x < 1))
    N.agree("q2.ttft_bimodal.mine_lt1s", sum(1 for x in t_m if x < 1))
    N.agree("q2.ttft_bimodal.mine_ge12s", sum(1 for x in t_m if x >= 12))
    if any(1 <= x < 12 for x in t_m):
        raise SystemExit(f"{rm}: a TTFT between 1 s and 12 s")
    lead(ax, f"default: {N('q2.ttft_bimodal.default_lt1s')} requests start within 1 s",
         (0.5, N.value("q2.ttft_bimodal.default_lt1s") + 0.6), (0.33, 98), ha="center", va="bottom", coords="data")
    lead(ax, f"patch: {N('q2.ttft_bimodal.mine_lt1s')} start within 1 s,\n"
             f"{N('q2.ttft_bimodal.mine_ge12s')} wait 12 s or more,\nnone in between",
         (1.0, N.value("q2.ttft_bimodal.mine_lt1s") - 0.6), (1.0, 40), ha="center", va="top", coords="data")
    ax.set_xscale("log")
    log_axis(ax.xaxis, [0.01, 0.1, 1, 10, 100], lambda v, _: f"{v:g}")
    ax.set_xlim(0.012, 150)
    ax.set_ylim(0, 125)
    ax.yaxis.set_major_locator(ticker.FixedLocator([0, 30, 60, 90, 120]))
    ax.set_xlabel("time to first token (s, log scale)")
    ax.set_ylabel("requests with TTFT \u2264 x")
    ax.legend(handles=handles, loc="upper left", bbox_to_anchor=(1.03, 1.0), fontsize=8.5, labelspacing=1.2,
              handlelength=2.8, borderaxespad=0)
    frame(fig, "Time to first token at q2",
          f"Cumulative count of the {N('trace.n_requests')} requests by TTFT. Every setting without retractions "
          "moves more requests into "
          "the slow group.\nUnder the patch a request is either admitted at once or held until the gate's "
          "simulation, with every known length, fits the KV pool.",
          "default and patch: the median-goodput run of 3 (r2), the same runs as the timeline figure.")
    return save(fig, out, "fig06_ttft_cdf.png")


# ----------------------------------------------------------------------------- main
FIGS = ["hero", "fig01", "fig02", "fig03", "fig04", "fig05", "fig06"]


def main():
    ap = argparse.ArgumentParser(description="draw docs/figures/*.png from data/derived and artifacts/numbers.json")
    ap.add_argument("--out", default="docs/figures", help="output folder (default: %(default)s)")
    ap.add_argument("--only", nargs="+", choices=FIGS, help="draw only these figures")
    args = ap.parse_args()
    style()
    N = Numbers()
    pr = per_request()
    runs = {r["name"]: r for r in read_csv(D + "w3_runs.csv")}
    grouped = {r["cond"]: r for r in read_csv(D + "w3_q2_grouped.csv")}
    sweep = read_csv(D + "sweep_0907.csv")
    cum = cum_output()
    draw = {"hero": lambda: fig_hero(args.out, N, pr, runs),
            "fig01": lambda: fig01_output_len(args.out, N, pr),
            "fig02": lambda: fig02_sweep(args.out, N, sweep),
            "fig03": lambda: fig03_load(args.out, N, runs, grouped, pr),
            "fig04": lambda: fig04_tradeoff(args.out, N, grouped),
            "fig05": lambda: fig05_decomposition(args.out, N, cum, pr),
            "fig06": lambda: fig06_ttft(args.out, N, pr, runs)}
    for name in FIGS:
        if not args.only or name in args.only:
            draw[name]()


if __name__ == "__main__":
    main()
