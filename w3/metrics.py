"""Client-side serving metrics for one replay result file (clean-room implementation).

This module was written from the formula specification below. It does not copy
the course harness; the course code was only used afterwards, as a black box, to
check that every summary field of every result file comes out bit-identical.

A result file is JSON with a ``records`` list. Each record carries
``submit_t``, ``first_token_t`` and ``end_t`` (seconds on the client clock),
``chunk_times`` (arrival time of every streamed chunk), ``chunk_tokens``,
``completion_tokens``, ``prompt_tokens``, ``cached_tokens``, ``error`` and the
per-request SLOs ``ttft_slo_ms`` / ``tpot_slo_ms`` (``None`` = no SLO).

Per request (``request_metrics``), with n = completion_tokens:

    TTFT   = first_token_t - submit_t
    E2E    = end_t - submit_t
    TPOT   = (end_t - first_token_t) / (n - 1)        only when n > 1
    maxITL = largest gap between consecutive chunk arrival times
    slo_ok = (ttft_slo is None or TTFT <= ttft_slo)
             and (tpot_slo is None or n <= 1 or TPOT <= tpot_slo)

Per run (``summarize_run``), over the requests that finished without an error
and produced a first token ("ok" requests):

    percentiles  Hyndman-Fan type 7: linear interpolation at rank (k - 1) * p / 100
    span         max(end_t) - min(submit_t) over all records that have timestamps
    goodput_rps  count(slo_ok) / span
    out_tok_s    sum(completion_tokens) / span
    slo_%        100 * count(slo_ok) / n_ok
    cache_hit_%  100 * sum(cached_tokens) / sum(prompt_tokens)

Times are reported in milliseconds (``*_ms`` fields and percentiles), except
``wall_s``, which is the span in seconds rounded to 0.1 s for display. Goodput
and output throughput always use the unrounded span.

Equivalence check (2026-10): for all 76 result files of this study, every field
of ``summarize_run`` and of ``request_metrics`` (9,720 records) is ``==`` to the
course harness output, and ``percentile`` matched it on 20,000 random inputs.
"""

from __future__ import annotations

__all__ = [
    "STD_COLS",
    "max_gap",
    "percentile",
    "request_metrics",
    "stream_gaps",
    "summarize_run",
]

# Column order of the standard per-run summary table (what ``summarize_run``
# returns, minus the request count ``n``).
STD_COLS = [
    "tag", "n_ok", "n_err", "wall_s",
    "ttft_p50", "ttft_p99", "tpot_p50", "tpot_p99", "maxitl_p99", "e2e_p99",
    "out_tok_s", "cache_hit_%", "slo_%", "goodput_rps",
]


def percentile(values, p: float) -> float:
    """Type-7 percentile of ``values`` (``p`` in 0..100); NaN for an empty input.

    The rank is h = (k - 1) * p / 100 on the sorted values; the result
    interpolates linearly between the two neighbouring order statistics.
    """
    v = sorted(values)
    if not v:
        return float("nan")
    h = (len(v) - 1) * p / 100.0
    lo = int(h)
    hi = min(lo + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (h - lo)


def stream_gaps(chunk_times) -> list[tuple[float, float]]:
    """``(gap_s, start_s)`` for every pair of consecutive chunk arrivals."""
    ct = list(chunk_times or [])
    return [(b - a, a) for a, b in zip(ct, ct[1:])]


def max_gap(chunk_times):
    """Largest stream gap as ``(gap_s, start_s)``; ``None`` with fewer than two chunks.

    Ties keep the earliest gap.
    """
    best = None
    for gap, start in stream_gaps(chunk_times):
        if best is None or gap > best[0]:
            best = (gap, start)
    return best


def request_metrics(rec: dict) -> dict:
    """Metrics of one request record.

    Always returns ``rid`` and ``error``. A request that failed, or that never
    produced a first token, gets nothing else. Otherwise the result also holds
    ``ttft_ms``, ``e2e_ms``, the three token counts, ``slo_ok`` and
    ``slo_class``, plus ``tpot_ms`` when more than one token was generated and
    ``max_itl_ms`` when at least two chunks arrived.
    """
    out = {"rid": rec.get("rid"), "error": rec.get("error")}
    if rec.get("error") or rec.get("first_token_t") is None:
        return out
    n = rec.get("completion_tokens") or 0
    out["ttft_ms"] = (rec["first_token_t"] - rec["submit_t"]) * 1000.0
    out["e2e_ms"] = (rec["end_t"] - rec["submit_t"]) * 1000.0
    out["completion_tokens"] = n
    out["prompt_tokens"] = rec.get("prompt_tokens") or 0
    out["cached_tokens"] = rec.get("cached_tokens") or 0
    if n > 1:
        out["tpot_ms"] = (rec["end_t"] - rec["first_token_t"]) * 1000.0 / (n - 1)
    mg = max_gap(rec.get("chunk_times"))
    if mg is not None:
        out["max_itl_ms"] = mg[0] * 1000.0
    ttft_slo = rec.get("ttft_slo_ms")
    tpot_slo = rec.get("tpot_slo_ms")
    out["slo_ok"] = bool(
        (ttft_slo is None or out["ttft_ms"] <= ttft_slo)
        and (tpot_slo is None or n <= 1 or out["tpot_ms"] <= tpot_slo))
    out["slo_class"] = rec.get("slo_class")
    return out


def summarize_run(blob: dict) -> dict:
    """Run-level summary of a result file (see the module docstring).

    Returns ``tag``, ``n`` and ``n_ok``; when at least one request is ok it
    also returns every other field of ``STD_COLS``.
    """
    recs = blob.get("records") or []
    rows = [request_metrics(r) for r in recs]
    ok = [m for m in rows if "ttft_ms" in m]
    out = {"tag": blob.get("tag"), "n": len(recs), "n_ok": len(ok)}
    if not ok:
        return out
    submits = [r["submit_t"] for r in recs if r.get("submit_t") is not None]
    ends = [r["end_t"] for r in recs if r.get("end_t") is not None]
    span = max(ends) - min(submits)
    met = sum(1 for m in ok if m["slo_ok"])
    prompt = sum(m["prompt_tokens"] for m in ok)

    def col(key):
        return [m[key] for m in ok if key in m]

    out["n_err"] = sum(1 for m in rows if m["error"])
    out["wall_s"] = round(span, 1)
    out["ttft_p50"] = percentile(col("ttft_ms"), 50)
    out["ttft_p99"] = percentile(col("ttft_ms"), 99)
    out["tpot_p50"] = percentile(col("tpot_ms"), 50)
    out["tpot_p99"] = percentile(col("tpot_ms"), 99)
    out["maxitl_p99"] = percentile(col("max_itl_ms"), 99)
    out["e2e_p99"] = percentile(col("e2e_ms"), 99)
    # A zero span cannot happen with real streams; report 0 rather than divide by zero.
    out["out_tok_s"] = sum(m["completion_tokens"] for m in ok) / span if span > 0 else 0.0
    out["cache_hit_%"] = 100.0 * sum(m["cached_tokens"] for m in ok) / prompt if prompt else 0.0
    out["slo_%"] = 100.0 * met / len(ok)
    out["goodput_rps"] = met / span if span > 0 else 0.0
    return out
