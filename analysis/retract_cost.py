#!/usr/bin/env python3
"""
retract_cost.py — cost of SGLang retractions (preemption) in the long-reasoning workload (W3)

For every retraction it lines up three things:
  · KV tokens freed      server log `#new_tokens_gained`
  · tokens recomputed    tokens prefilled again on resume (server Prefill line / client estimate /
                         prefill excess over a run without retractions)
  · time                 how long the victim's stream stopped (client chunk_times) and the delay from
                         retraction to re-prefill (server log)
and writes a report in which every number points to the file and line (log) or record (result file)
it came from. The report and the terminal tables are in Korean (the language of the original study).

Usage (from the repository root; raw results/ and logs/ from the data-v1 release, see data/RAW.md)
    python3 -m analysis.retract_cost                      # discover every run in results/*.json
    python3 -m analysis.retract_cost --show 30            # print up to 30 detail rows per run
    python3 -m analysis.retract_cost --run q1 results/reasoning_q1__default.json logs/server_q1.log
    python3 -m analysis.retract_cost --sweep-0907 --out data/derived/retract_cost --force
                                                          # regenerate the committed 09-07 sweep analysis
    python3 -m analysis.retract_cost --stall-sec 3 --out retract_cost_out/stall3s

    --project DIR    folder that holds results/ and logs/ (default: the repository root)

Outputs (--out; default: a new folder retract_cost_out/run_<UTC timestamp>/ under the current
directory; an existing non-empty --out folder is only overwritten with --force)
    report.md     summary tables + per-retraction evidence links + method + SGLang source references
    events.csv    one row per (retraction slot, victim request), plus stalls with no retraction
    summary.csv   one row per run
Paths inside the outputs are relative to --project, and nothing depends on the time of the run,
so the same inputs give byte-identical files.

Without --run/--sweep-0907, server logs are paired with result files by name: w3/run_one.sh
writes logs/server_<trace>__<tag>.log; the 09-07 sweep wrote logs/server_q<Q>.log; scripts/run_matrix.sh
of the course harness wrote logs/server_<tag>.log. A candidate is accepted only when its
modification time is within 15 minutes of the result file (one log = one run), so unpack the raw
data with its time stamps (tar does this by default).

Terms
  "Recompute", not "regenerate": SGLang 0.5.18 keeps the output tokens a retracted request has
  already produced (output_ids) and frees only its KV. On resume it prefills input + output so far
  again, reusing whatever prefix is still in the radix cache (the shared system prompt); no output
  token is decoded twice.

Line numbers
  L = VS Code numbering (\\r also ends a line), g = `grep -n` / `sed -n` numbering (\\n only).
  They differ because the CUDA graph capture progress bar at the top of a server log contains \\r.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import glob
import importlib.util
import json
import os
import re
import statistics
import sys
import unicodedata

from w3 import metrics as w3m   # clean-room client metrics (request_metrics, summarize_run)

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PROJECT = os.path.dirname(HERE)   # repository root (holds results/ and logs/)
PAIR_MTIME_SEC = 900          # 결과 파일과 서버 로그를 같은 실행으로 볼 수정 시각 차 한도
EXACT = ("exact-out", "exact-out+prompt")
# The 09-07 load sweep (unpatched SGLang 0.5.18): label, result file, server log
SWEEP_0907 = [(f"q{q}", f"results/reasoning_q{q}__default.json", f"logs/server_q{q}.log")
              for q in ("0.5", "0.6", "0.8", "1", "2", "4", "8")]

# [YYYY-mm-dd HH:MM:SS(.mmm)( TP0 DP0 …)] — SGLang 로거 형식 (SGLANG_LOG_MS, TP/DP 접두어 포함)
RX_TS = re.compile(r"^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)(?:[.,](\d{1,6}))?((?: [A-Za-z_]+\d+)*)\]")
RX_RETRACT = re.compile(
    r"KV cache pool is full\. Retract requests\. #retracted_reqs: (\d+), "
    r"#new_tokens_gained: (-?\d+)(?:, #mamba_num_gained: -?\d+)?"
    r"(?:, #new_token_ratio: ([\d.]+) -> ([\d.]+))?")
RX_PREFILL = re.compile(r"Prefill batch, #new-seq: (\d+), #new-token: (\d+), #cached-token: (\d+)")
RX_KVCAP = re.compile(r"max_total_num_tokens=(\d+)")
RX_CHUNK = re.compile(r"chunked_prefill_size=(-?\d+)")
RX_POLICY = re.compile(r"schedule_policy='([^']+)'")
RX_CONSERV = re.compile(r"schedule_conservativeness=([\d.]+)")


# ----------------------------------------------------------------------------
# 입력 읽기
# ----------------------------------------------------------------------------
def read_log_lines(path):
    """[(vscode_line, grep_line, text)] — \\r\\n, \\r, \\n 을 모두 줄바꿈으로 본다(VS Code 방식)."""
    raw = open(path, "rb").read().decode("utf-8", "replace")
    out, v, g, start = [], 1, 1, 0
    for m in re.finditer(r"\r\n|\r|\n", raw):
        out.append((v, g, raw[start:m.start()]))
        v += 1
        if "\n" in m.group():
            g += 1
        start = m.end()
    if start < len(raw):
        out.append((v, g, raw[start:]))
    return out


def parse_ts(text):
    """(epoch 초, 해상도 초, 대표 rank 여부). 로그 시각은 시간대 없는 벽시계로 보고 UTC 로 고정 해석한다."""
    m = RX_TS.match(text)
    if not m:
        return None, None, True
    ts = dt.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc).timestamp()
    res = 1.0
    if m.group(2):
        ts += int(m.group(2)) / 10 ** len(m.group(2))
        res = 10.0 ** -len(m.group(2))
    ranks = re.findall(r"\d+", m.group(3) or "")
    return ts, res, all(int(x) == 0 for x in ranks)   # TP>1 이면 rank 0 줄만 쓴다


def parse_log(path):
    events, prefills, info = [], [], {}
    res_seen, skipped_rank = [], 0
    for v, g, text in read_log_lines(path):
        if "Retract requests" in text or "Prefill batch" in text:
            ts, res, primary = parse_ts(text)
            if not primary:
                skipped_rank += 1
                continue
            if res is not None:
                res_seen.append(res)
            if "Retract requests" in text:
                m = RX_RETRACT.search(text)
                if m:
                    events.append(dict(ts=ts, L=v, g=g, k=int(m.group(1)), gained=int(m.group(2)),
                                       ratio=(f"{m.group(3)} -> {m.group(4)}" if m.group(3) else "")))
            else:
                m = RX_PREFILL.search(text)
                if m:
                    prefills.append(dict(ts=ts, L=v, g=g, seq=int(m.group(1)),
                                         new=int(m.group(2)), cached=int(m.group(3))))
            continue
        if "server_args=" in text and "server_args" not in info:
            info["server_args"] = (v, g)
            for key, rx in (("chunked_prefill_size", RX_CHUNK), ("schedule_policy", RX_POLICY),
                            ("schedule_conservativeness", RX_CONSERV)):
                m = rx.search(text)
                if m:
                    info[key] = m.group(1)
        if "kv_capacity" not in info:
            m = RX_KVCAP.search(text)
            if m:
                info["kv_capacity"] = (int(m.group(1)), v, g)
    info["ts_res"] = max(res_seen) if res_seen else 1.0
    info["skipped_rank_lines"] = skipped_rank
    info["events_without_ts"] = sum(1 for e in events if e["ts"] is None)
    return dict(events=events, prefills=prefills, info=info)


def load_results(path, stall_sec):
    with open(path) as fh:
        blob = json.load(fh)
    recs = blob["records"]
    stalls, per = [], {}
    for idx, r in enumerate(recs):
        per[r["rid"]] = w3m.request_metrics(r)
        ct, ck = r.get("chunk_times") or [], r.get("chunk_tokens") or []
        cum = 0
        for i in range(1, len(ct)):
            cum += ck[i - 1] if i - 1 < len(ck) else 1
            gap = ct[i] - ct[i - 1]
            if gap > stall_sec:
                stalls.append(dict(
                    rid=r["rid"], idx=idx, chunk=i, t_start=ct[i - 1], t_end=ct[i], dur=gap,
                    before=cum, prompt=r.get("prompt_tokens") or 0,
                    cached=r.get("cached_tokens") or 0, completion=r.get("completion_tokens") or 0,
                    num_retractions=r.get("num_retractions")))
    stalls.sort(key=lambda s: s["t_start"])
    good = [p for p in per.values() if "ttft_ms" in p]
    totals = dict(prompt=sum(p["prompt_tokens"] for p in good),
                  output=sum(p["completion_tokens"] for p in good))
    sm = w3m.summarize_run(blob)
    return dict(blob=blob, recs=recs, stalls=stalls, per=per, summary=sm, totals=totals,
                n=len(recs), n_ok=sm.get("n_ok", 0))


# ----------------------------------------------------------------------------
# 서버 선점 ↔ 클라이언트 정지 매칭
# ----------------------------------------------------------------------------
def token_relation(gained, s):
    """풀린 KV 가 이 정지 요청과 토큰 수로 맞는가 (선점 1건 = 요청 1개일 때)."""
    uniq = s["prompt"] - s["cached"]
    d = gained - s["before"]
    if d == 0:
        return "exact-out"          # 출력분만 풀림 (입력 KV 는 캐시에 남음)
    if d == uniq:
        return "exact-out+prompt"   # 출력분 + 공유되지 않는 입력분이 풀림
    if abs(d) <= 2 or abs(d - uniq) <= 2:
        return "approx"
    return None


def anchor_score(offset, recs, prefills):
    """독립 검증: 요청의 첫 토큰은 그 요청을 담은 Prefill 줄 직후에 나온다.
    오프셋이 맞으면 거의 모든 요청의 (첫 토큰 시각 + offset) 이 Prefill 줄이 찍힌 초(또는 1~2초 뒤)에 떨어진다."""
    secs = {int(p["ts"] // 1) for p in prefills if p["ts"] is not None}
    hits = tot = 0
    for r in recs:
        ft = r.get("first_token_t")
        if ft is None or r.get("error"):
            continue
        tot += 1
        s = int((ft + offset) // 1)
        if s in secs or (s - 1) in secs or (s - 2) in secs:
            hits += 1
    return (hits / tot) if tot else None


def estimate_offset(events, stalls, res, recs, prefills):
    """서버 시각 ≈ 클라이언트 상대 시각 + offset.
    토큰이 정확히 맞는 후보 쌍의 시각 차 가운데 가장 많이 모인 무리(폭 = 로그 시각 해상도)를 고르고
    그 중앙값에 절삭 보정(해상도/2)을 더한다. 무리 크기가 같으면 첫 토큰↔Prefill 줄 일치율로 가른다."""
    cands = [e["ts"] - s["t_start"] for e in events if e["ts"] is not None and e["k"] == 1
             for s in stalls if token_relation(e["gained"], s) in EXACT]
    win = max(res, 1.0)
    if cands:
        clusters = {}
        for c in cands:
            cl = tuple(sorted(d for d in cands if abs(d - c) <= win))
            clusters[cl] = None
        scored = []
        for cl in clusters:
            off = statistics.median(cl) + res / 2
            sc = anchor_score(off, recs, prefills)
            scored.append(((len(cl), sc if sc is not None else 0.0, -(cl[-1] - cl[0])), cl, off, sc))
        scored.sort(key=lambda x: x[0], reverse=True)
        _, cl, off, sc = scored[0]
        how = (f"토큰이 정확히 맞는 후보 {len(cands)}쌍 중 {win:g}s 안에 모인 {len(cl)}쌍의 시각 차 중앙값"
               f" + 로그 시각 절삭 보정 {res / 2:g}s")
        if len(cl) < len(cands):
            how += f" — 우연히 토큰만 맞은 {len(cands) - len(cl)}쌍 제외"
        if any(x[0][0] == len(cl) and abs(x[2] - off) > win for x in scored[1:]):
            how += " — 크기가 같은 다른 무리가 있어 첫 토큰↔Prefill 일치율로 선택"
        if sc is not None:
            how += f"; 첫 토큰↔Prefill 줄 일치율 {sc:.0%}" + (" (낮음 — 확인 필요)" if sc < 0.8 else "")
        return off, how
    slots = [e for e in events if e["ts"] is not None for _ in range(e["k"])]
    if slots and len(slots) == len(stalls):
        off = statistics.median(e["ts"] - s["t_start"] for e, s in zip(slots, stalls)) + res / 2
        sc = anchor_score(off, recs, prefills)
        return off, ("토큰이 맞는 쌍이 없어 선점 슬롯과 정지를 시간 순서대로 짝지은 중앙값 (확인 필요)"
                     + (f"; 첫 토큰↔Prefill 줄 일치율 {sc:.0%}" if sc is not None else ""))
    return None, "추정 불가 — 토큰이 맞는 쌍이 없고 선점 슬롯 수와 정지 수가 다르다"


def match_events(events, stalls, offset, tol):
    """전역 배정: 모든 (선점, 정지) 후보를 (토큰 일치 등급, 시각 오차) 순으로 정렬해
    정확 일치부터 배정한다. 선점 1건에는 #retracted_reqs 만큼의 자리가 있다."""
    rank = {"exact-out": 0, "exact-out+prompt": 0, "approx": 1, None: 2}
    cand = []
    if offset is not None:
        for ei, e in enumerate(events):
            if e["ts"] is None:
                continue
            for si, s in enumerate(stalls):
                r = e["ts"] - (s["t_start"] + offset)
                if abs(r) <= tol:
                    rel = token_relation(e["gained"], s) if e["k"] == 1 else None
                    cand.append((rank[rel], abs(r), ei, si, rel))
    cand.sort()
    got = {ei: [] for ei in range(len(events))}
    used = set()
    for _, _, ei, si, rel in cand:
        if si in used or len(got[ei]) >= events[ei]["k"]:
            continue
        got[ei].append((si, rel))
        used.add(si)

    pairs = []
    for ei, e in enumerate(events):
        chosen = sorted(got[ei], key=lambda x: stalls[x[0]]["t_start"])
        multi = "시각(동시 선점)"
        if e["k"] > 1 and len(chosen) == e["k"]:
            sb = sum(stalls[si]["before"] for si, _ in chosen)
            su = sum(stalls[si]["prompt"] - stalls[si]["cached"] for si, _ in chosen)
            if abs(e["gained"] - sb) <= 2 * e["k"] or abs(e["gained"] - sb - su) <= 2 * e["k"]:
                multi = "시각+토큰 합 일치(동시 선점)"
        for slot in range(e["k"]):
            if slot < len(chosen):
                si, rel = chosen[slot]
                if e["k"] > 1:
                    method = multi
                elif rel in EXACT:
                    method = "시각+토큰 정확"
                elif rel == "approx":
                    method = "시각+토큰 근사"
                else:
                    method = "시각만"
                pairs.append(dict(ei=ei, si=si, slot=slot + 1, method=method, rel=rel))
            else:
                pairs.append(dict(ei=ei, si=None, slot=slot + 1,
                                  method="미매칭" if e["ts"] is not None else "미매칭(시각 없음)", rel=None))
    return pairs, [si for si in range(len(stalls)) if si not in used]


def find_reprefill(e, s, prefills, offset, used_lines, res):
    """재개 prefill: 그 요청 하나만 담긴 줄(#new-seq: 1)에서 new + cached == 입력 + 정지 전 생성(±1),
    시각은 재개 시점(정지 끝) 부근. 캐시 적중이 첫 prefill 과 1토큰 달라도 잡힌다."""
    target = s["prompt"] + s["before"]
    t_resume = (s["t_end"] + offset) if offset is not None else None
    best = None
    for p in prefills:
        if p["ts"] is None or p["seq"] != 1 or p["L"] in used_lines:
            continue
        if abs(p["new"] + p["cached"] - target) > 1:
            continue
        if e["ts"] is not None and p["ts"] < e["ts"] - res:
            continue
        if t_resume is not None and abs(p["ts"] - t_resume) > 2.0 + res:
            continue
        d = abs(p["ts"] - t_resume) if t_resume is not None else p["ts"]
        if best is None or d < best[0]:
            best = (d, p)
    if best:
        used_lines.add(best[1]["L"])
        return best[1]
    return None


# ----------------------------------------------------------------------------
# 실행 탐색 · 경로
# ----------------------------------------------------------------------------
def discover(project):
    runs = []
    for res in sorted(glob.glob(os.path.join(project, "results", "*.json"))):
        base = os.path.basename(res)[:-5]
        if "__" not in base:
            continue
        stem, tag = base.split("__", 1)
        mq = re.search(r"_q([\d.]+)$", stem)
        root = stem[: mq.start()] if mq else stem
        name = ((f"q{mq.group(1)}" if root == "reasoning" else f"{root}-q{mq.group(1)}") if mq
                else ("" if root == "reasoning" else root))
        label = (name if tag == "default" else f"{name}-{tag}") if name else tag
        # Log candidates, most specific first: w3/run_one.sh name (server_<trace>__<tag>.log, errata C6),
        # the 09-07 sweep name (server_q<Q>.log, default results only), the run_matrix name (server_<tag>.log).
        cands = [os.path.join(project, "logs", f"server_{stem}__{tag}.log")]
        if mq and tag == "default":
            cands.append(os.path.join(project, "logs", f"server_q{mq.group(1)}.log"))
        cands.append(os.path.join(project, "logs", f"server_{tag}.log"))
        log, notes = None, []
        for c in cands:
            if not os.path.exists(c):
                continue
            gap = abs(os.path.getmtime(res) - os.path.getmtime(c))
            if gap <= PAIR_MTIME_SEC:
                log = c
                notes.append(f"{os.path.basename(c)} — 파일명 규칙 + 수정 시각 차 {gap:.0f}s")
                break
            notes.append(f"{os.path.basename(c)} 는 수정 시각이 {gap / 60:.0f}분 달라 다른 실행으로 보고 제외")
        runs.append(dict(label=label, results=res, log=log,
                         pair_note="; ".join(notes) if notes else "서버 로그 없음(클라이언트만)"))

    # 같은 로그를 여러 결과가 차지하면 수정 시각이 가장 가까운 결과만 남긴다
    by_log = {}
    for r in runs:
        if r["log"]:
            by_log.setdefault(r["log"], []).append(r)
    for log, rs in by_log.items():
        if len(rs) > 1:
            rs.sort(key=lambda r: abs(os.path.getmtime(r["results"]) - os.path.getmtime(log)))
            for r in rs[1:]:
                r["log"] = None
                r["pair_note"] = (f"{os.path.basename(log)} 는 {rs[0]['label']} 결과와 더 가까워 제외 "
                                  "(한 로그 = 한 실행)")

    seen = {}
    for r in runs:   # 라벨 중복 방지
        if r["label"] in seen:
            seen[r["label"]] += 1
            r["label"] = f"{r['label']}#{seen[r['label']]}"
        else:
            seen[r["label"]] = 1

    def order(r):
        m = re.match(r"q([\d.]+)", r["label"])
        return (0, float(m.group(1)), r["label"]) if m else (1, 0.0, r["label"])

    return sorted(runs, key=order)


def resolve_path(x, project):
    """절대 경로 → 그대로, 상대 경로 → 현재 폴더에 있으면 그것, 없으면 project 기준."""
    if os.path.isabs(x):
        return x
    if os.path.exists(x):
        return os.path.abspath(x)
    return os.path.join(project, x)


# ----------------------------------------------------------------------------
# Source references for report section 4. The line numbers are pinned to the versions that
# produced the data and are linked as GitHub permalinks, so the report does not depend on what
# is installed on the machine that runs this script:
#   SGLang v0.5.18 (checked against sglang-0.5.18-cp312 wheel, sha256 fba7bb31...),
#   course harness commit 802a164 (linked only; its code is not part of this repository).
# An SGLang source tree (--sglang-src, or an importable sglang) is used only to re-check the
# pinned SGLang lines; mismatches are printed on the terminal.
# ----------------------------------------------------------------------------
SGLANG_URL = "https://github.com/sgl-project/sglang/blob/v0.5.18/python/sglang/"
COURSE_URL = ("https://github.com/mlleo/inference-engine-study/blob/"
              "802a1642fcefc06735a89af054213c9904b4ff45/project/")
SRC_REFS = [   # (label, path under python/sglang/, regex, line in v0.5.18)
    ("선점 실행", "srt/managers/scheduler.py", r"batch\.retract_decode\(", 3504),
    ("#new_tokens_gained = 선점 전후 빈 KV 슬롯 수 차이", "srt/managers/scheduler.py",
     r"new_token_gained = new_available_tokens - old_available_tokens", 3508),
    ("선점 로그 문구", "srt/managers/scheduler.py", r"KV cache pool is full\. Retract requests", 3538),
    ("선점된 요청을 대기열에 다시 넣음", "srt/managers/scheduler.py", r"_add_request_to_queue\(req, is_retracted=True\)",
     3552),
    ("대기열 맨 뒤에 추가", "srt/managers/scheduler.py", r"self\.waiting_queue\.append\(req\)", 2725),
    ("fcfs 는 대기열을 재정렬하지 않음", "srt/managers/schedule_policy.py",
     r"if self\.policy == CacheAgnosticPolicy\.FCFS:", 254),
    ("선점 요청 KV 를 radix 캐시에 넣지 않고 해제", "srt/managers/schedule_batch.py",
     r"release_kv_cache\(req, tree_cache, is_insert=False\)", 1934),
    ("선점 뒤에도 output_ids 유지 — input_embeds 요청만 비운다(주석)", "srt/managers/schedule_batch.py",
     r"we discard the generated output_ids and restart prefill", 1709),
    ("prefill 입력 = origin_input_ids + output_ids (불변식)", "srt/managers/schedule_batch.py",
     r"Keep full_untruncated_fill_ids == origin_input_ids \+ output_ids", 1272),
    ("재개 시 새 출력만 이어 붙이는 경로", "srt/managers/schedule_batch.py",
     r"self\.full_untruncated_fill_ids\.extend\(self\.output_ids\[n_have_output:\]\)", 1288),
    ("선점 순서 (생성 토큰이 적은 요청부터)", "srt/managers/schedule_batch.py", r"def _get_decode_retraction_order",
     2867),
    ("SGLang 자체 재계산 집계: 선점됐던 요청의 재prefill 토큰", "srt/managers/schedule_policy.py",
     r"self\.reprocessed_log_input_tokens \+= extend_input_len", 904),
    ("재계산 = realtime_tokens_total{mode=prefill_compute} − prefill_effective_tokens_total{mode=input}",
     "srt/observability/metrics_collector.py", r'name="sglang:prefill_effective_tokens_total"', 893),
    ("num_retracted_input_tokens_total = 선점 시점 요청의 입력 길이 합 (재계산량 아님)",
     "srt/observability/metrics_collector.py", r"num_retracted_input_tokens_total = Counter", 467),
    ("응답 meta_info 의 요청별 선점 횟수", "srt/managers/tokenizer_manager.py",
     r'"num_retractions": recv_obj\.retraction_counts', 2246),
]
COURSE_REFS = [   # (label, path under project/, line at 802a164)
    ("replay 는 rid 를 서버에 보내지 않음", "bench/replay.py", 79),
    ("chunk_times 를 t0 기준 상대 시각으로 저장", "bench/replay.py", 58),
    ("t0 = perf_counter (벽시계 아님)", "bench/replay.py", 135),
    ("W3 SLO: TPOT ≤ 60 ms", "workloads/generators.py", 161),
]
REPO_REFS = [   # (label, path in this repository) — linked without a line number
    ("TPOT 정의 (첫 토큰 이후 평균): request_metrics", "w3/metrics.py"),
]


def find_line(path, rx):
    """First line (1-based) of ``path`` matching ``rx``; None when the file or the match is missing."""
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8", errors="replace") as fh:
        for i, t in enumerate(fh, 1):
            if re.search(rx, t):
                return i
    return None


def check_sglang_refs(root):
    """Compare the pinned SGLang lines with a source tree: [(label, rel, pinned, found)] mismatches."""
    return [(label, rel, line, found) for label, rel, rx, line in SRC_REFS
            if (found := find_line(os.path.join(root, rel), rx)) != line]


def sglang_root(explicit):
    if explicit:
        return explicit
    try:
        spec = importlib.util.find_spec("sglang")
        if spec and spec.origin:
            return os.path.dirname(spec.origin)
    except Exception:
        pass
    return None


# ----------------------------------------------------------------------------
# 표 출력 (한글 폭 2칸 보정)
# ----------------------------------------------------------------------------
def dw(s):
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in str(s))


def fmt_table(headers, rows, right=None):
    right = right or set()
    cols = list(zip(*([headers] + rows))) if rows else [[h] for h in headers]
    w = [max(dw(x) for x in col) for col in cols]

    def line(cells):
        out = []
        for j, c in enumerate(cells):
            c = str(c)
            padn = w[j] - dw(c)
            out.append(" " * padn + c if j in right else c + " " * padn)
        return "  ".join(out).rstrip()

    return "\n".join([line(headers), "  ".join("-" * x for x in w)] + [line(r) for r in rows])


def md_table(headers, rows):
    esc = lambda x: str(x).replace("|", "\\|")
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    out += ["| " + " | ".join(esc(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def mdlink(text, target):
    """공백·괄호가 든 경로도 깨지지 않게 <...> 로 감싼다."""
    if re.search(r"[\s()<>]", target):
        target = "<" + target.replace("<", "%3C").replace(">", "%3E") + ">"
    return f"[{text}]({target})"


def n(x):
    return f"{x:,}" if isinstance(x, int) else str(x)


def f(x, spec):
    """None·NaN 이면 '—' (유효 요청이 없는 실행 등)."""
    if x is None or (isinstance(x, float) and x != x):
        return "—"
    return format(x, spec)


def hhmmss(ts):
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%H:%M:%S") if ts is not None else "—"


def slo_mark(pr):
    if not pr:
        return "—"
    if pr.get("error"):
        return "err"
    tp = pr.get("tpot_ms")
    mark = "✓" if pr.get("slo_ok") is True else ("✗" if pr.get("slo_ok") is False else "—")
    return f"{tp:.1f} {mark}" if tp is not None else mark


# ----------------------------------------------------------------------------
# 분석 본체
# ----------------------------------------------------------------------------
def analyze_run(run, stall_sec, tol):
    res = load_results(run["results"], stall_sec)
    a = dict(run=run, res=res, log=None, rows=[], offset=None, offset_how=None, unmatched_stalls=[])
    if run["log"]:
        log = parse_log(run["log"])
        r = log["info"]["ts_res"]
        offset, how = estimate_offset(log["events"], res["stalls"], r, res["recs"], log["prefills"])
        pairs, unmatched = match_events(log["events"], res["stalls"], offset, tol)
        a.update(log=log, offset=offset, offset_how=how, unmatched_stalls=unmatched)
        used_lines = set()
        for p in pairs:
            e = log["events"][p["ei"]]
            s = res["stalls"][p["si"]] if p["si"] is not None else None
            rp = find_reprefill(e, s, log["prefills"], offset, used_lines, r) if s else None
            a["rows"].append(dict(e=e, eid=p["ei"] + 1, slot=p["slot"], s=s, method=p["method"], rel=p["rel"],
                                  rp=rp, pr=res["per"].get(s["rid"]) if s else None))
        for si in unmatched:
            s = res["stalls"][si]
            a["rows"].append(dict(e=None, eid=None, slot=None, s=s, method="선점과 짝 없음", rel=None, rp=None,
                                  pr=res["per"].get(s["rid"])))
    else:
        a["unmatched_stalls"] = list(range(len(res["stalls"])))
        for s in res["stalls"]:
            a["rows"].append(dict(e=None, eid=None, slot=None, s=s, method="클라이언트만", rel=None, rp=None,
                                  pr=res["per"].get(s["rid"])))
    return a


def signature(a):
    """기준선 비교용: 같은 트레이스·같은 요청·같은 입력 길이인가."""
    recs = a["res"]["recs"]
    return (a["res"]["blob"].get("trace"), len(recs),
            tuple(sorted((r["rid"], r.get("prompt_tokens")) for r in recs)))


def baseline_ok(a):
    return (a["log"] is not None and not a["log"]["events"]
            and a["res"]["n_ok"] == a["res"]["n"] and a["res"]["n"] > 0)


def assign_baselines(analyses, explicit):
    """실행마다 prefill 초과분의 기준선을 고른다: 선점 0건, 전 요청 성공, 같은 트레이스·요청·입력 길이."""
    if explicit:
        cand = [a for a in analyses if a["run"]["label"] == explicit]
        if not cand:
            sys.exit(f"--baseline {explicit}: 그런 라벨의 실행이 없다 (라벨: {', '.join(a['run']['label'] for a in analyses)})")
        b = cand[0]
        if not baseline_ok(b):
            sys.exit(f"--baseline {explicit}: 기준선이 될 수 없다 — 서버 로그가 있고, 선점 0건이며, 모든 요청이 성공해야 한다")
        pool = [b]
    else:
        pool = [a for a in analyses if baseline_ok(a)]
    sigs = {id(b): signature(b) for b in pool}
    for a in analyses:
        a["baseline"], a["baseline_note"] = None, ""
        if not a["log"]:
            continue
        match = [b for b in pool if sigs[id(b)] == signature(a)]
        if match:
            a["baseline"] = match[0]
        elif explicit:
            a["baseline_note"] = f"기준선 {explicit} 와 트레이스·요청·입력 길이가 달라 prefill 초과를 계산하지 않음"
        else:
            a["baseline_note"] = "같은 트레이스·요청의 선점 0건 실행이 없어 prefill 초과를 계산하지 않음"


def summarize_run(a):
    res, log, rows = a["res"], a["log"], a["rows"]
    total = res["totals"]["prompt"] + res["totals"]["output"]
    ev = log["events"] if log else []
    victim_rows = [r for r in rows if r["e"] is not None]
    matched = [r for r in victim_rows if r["s"] is not None]
    unmatched_slots = sum(1 for r in victim_rows if r["s"] is None)
    gained = sum(e["gained"] for e in ev)
    if log:
        ub_known = sum(r["s"]["before"] + r["s"]["prompt"] - r["s"]["cached"] for r in matched)
    else:
        ub_known = sum(s["before"] + s["prompt"] - s["cached"] for s in res["stalls"])
    ub_complete = unmatched_slots == 0
    confirmed = [r["rp"]["new"] for r in rows if r["rp"]]
    pf_sum = sum(p["new"] for p in log["prefills"]) if log else None
    b = a.get("baseline")
    excess = (pf_sum - sum(p["new"] for p in b["log"]["prefills"])) if (log and b is not None) else None
    waste_basis = excess if excess is not None else (gained if log else None)
    durs = [s["dur"] for s in res["stalls"]]
    vict = {r["s"]["rid"]: (r["pr"] or {}) for r in matched}
    delays = [r["rp"]["ts"] - r["e"]["ts"] for r in rows if r["rp"] and r["e"] and r["e"]["ts"] is not None]
    methods = [r["method"] for r in victim_rows]
    sm = res["summary"]
    ms = lambda k: (sm[k] / 1000) if sm.get(k) is not None else None
    return dict(
        label=a["run"]["label"], events=len(ev), retracted=sum(e["k"] for e in ev) if log else None,
        gained=gained if log else None, recompute_lb=gained if log else None,
        recompute_ub=ub_known if ub_complete else None, recompute_ub_known=ub_known, recompute_ub_complete=ub_complete,
        confirmed_n=len(confirmed), confirmed_sum=sum(confirmed), prefill_sum=pf_sum, prefill_excess=excess,
        baseline=b["run"]["label"] if b is not None else "", baseline_note=a.get("baseline_note", ""),
        total_tokens=total, waste_pct=(100 * waste_basis / total) if (waste_basis is not None and total) else None,
        waste_basis=("prefill 초과분" if excess is not None else ("회수 KV(하한)" if log else "")),
        stalls=len(durs), stall_sum=sum(durs), stall_mean=(sum(durs) / len(durs)) if durs else 0.0,
        stall_max=max(durs) if durs else 0.0,
        exact=sum(1 for m in methods if m == "시각+토큰 정확" or m.startswith("시각+토큰 합")),
        approx=sum(1 for m in methods if m == "시각+토큰 근사"),
        timeonly=sum(1 for m in methods if m in ("시각만", "시각(동시 선점)")),
        unmatched_slots=unmatched_slots, unmatched_stalls=len(a["unmatched_stalls"]),
        delay_mean=(sum(delays) / len(delays)) if delays else None,
        victims=len(vict), victims_slo_violation=sum(1 for p in vict.values() if p.get("slo_ok") is False),
        victims_error=sum(1 for p in vict.values() if p.get("error")),
        maxitl_p99_s=ms("maxitl_p99"), ttft_p99_s=ms("ttft_p99"),
        out_tok_s=sm.get("out_tok_s"), goodput_rps=sm.get("goodput_rps"), slo_pct=sm.get("slo_%"),
        offset_utc=hhmmss(a["offset"]) if a["offset"] is not None else "", offset_how=a["offset_how"] or "",
        results=a["run"]["results"], log=a["run"]["log"] or "", pair_note=a["run"]["pair_note"],
    )


def ub_text(s):
    if s["recompute_ub"] is not None:
        return n(s["recompute_ub"])
    lb = s["recompute_lb"] or 0
    return f"≥{n(max(lb, s['recompute_ub_known']))}"   # 짝 없는 선점이 있어 상한을 다 알 수 없다


# ----------------------------------------------------------------------------
# 메인
# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="SGLang retraction 비용 분석 + 근거 보고서")
    ap.add_argument("--project", default=DEFAULT_PROJECT,
                    help="folder that holds results/ and logs/ (default: the repository root)")
    ap.add_argument("--run", nargs="+", action="append", metavar="ARG",
                    help="LABEL RESULTS.json [SERVER.log] — 여러 번 지정 가능. 상대 경로는 현재 폴더에 있으면 그것, "
                         "없으면 --project 기준. 없으면 자동 탐색")
    ap.add_argument("--sweep-0907", action="store_true",
                    help="analyse the 09-07 load sweep: q0.5..q8 with logs/server_q<Q>.log (same as 7 --run)")
    ap.add_argument("--stall-sec", type=float, default=5.0, help="이 시간(초)보다 긴 토큰 간격을 정지로 본다")
    ap.add_argument("--tol", type=float, default=2.5, help="서버 선점 시각 ↔ 클라이언트 정지 시작 허용 오차(초)")
    ap.add_argument("--baseline", default=None, help="prefill 초과분 기준선 실행 라벨 (기본: 조건이 맞는 선점 0건 실행)")
    ap.add_argument("--sglang-src", default=None,
                    help="SGLang package folder used to re-check the pinned v0.5.18 source lines "
                         "(default: the installed sglang, if any); the report always links v0.5.18")
    ap.add_argument("--show", type=int, default=8, help="실행별 상세를 터미널에 몇 건까지 보일지")
    ap.add_argument("--out", default=None,
                    help="output folder (default: a new retract_cost_out/run_<UTC timestamp>/ in the current folder)")
    ap.add_argument("--force", action="store_true", help="allow writing into an existing non-empty --out folder")
    args = ap.parse_args()

    project = os.path.abspath(args.project)
    if args.sweep_0907 and args.run:
        ap.error("--sweep-0907 and --run cannot be combined")
    if args.sweep_0907:
        args.run = [[label, res, log] for label, res, log in SWEEP_0907]
    if args.run:
        runs = []
        for spec in args.run:
            if len(spec) not in (2, 3):
                ap.error("--run 은 LABEL RESULTS [LOG] 형식")
            paths = [resolve_path(x, project) for x in spec[1:]]
            for p in paths:
                if not os.path.exists(p):
                    ap.error(f"파일이 없다: {p}")
            note = ("--sweep-0907 (09-07 스윕 로그 이름 server_q<Q>.log)" if args.sweep_0907
                    else "--run 으로 지정")
            runs.append(dict(label=spec[0], results=paths[0], log=paths[1] if len(paths) == 2 else None,
                             pair_note=note))
        if len({r["label"] for r in runs}) != len(runs):
            ap.error("--run 라벨이 겹친다")
    else:
        runs = discover(project)
    if not runs:
        sys.exit(f"결과 파일을 찾지 못했다: {project}/results/*.json")
    # Errata C6/C7: never overwrite earlier outputs by default.
    if args.out:
        out_dir = args.out
    else:
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out_dir = os.path.join("retract_cost_out", f"run_{stamp}")
    if os.path.isdir(out_dir) and os.listdir(out_dir) and not args.force:
        sys.exit(f"{out_dir} already has files; pass --force to overwrite them, or choose another --out")

    analyses = [analyze_run(r, args.stall_sec, args.tol) for r in runs]
    assign_baselines(analyses, args.baseline)
    sums = [summarize_run(a) for a in analyses]
    disp = lambda p: os.path.relpath(p, project) if p else ""   # paths in the outputs: relative to --project
    for s in sums:
        s["results"], s["log"] = disp(s["results"]), disp(s["log"])

    # ------------------------------------------------------------------ 터미널
    print("=" * 100)
    print("SGLang retraction 비용 분석")
    print(f"  project : {project}")
    print(f"  정지 기준: 토큰 간격 > {args.stall_sec:g}s   매칭 허용 오차: ±{args.tol:g}s")
    print("=" * 100)

    print("\n[A] 토큰 — 회수한 KV vs 재계산")
    rowsA = []
    for s in sums:
        rowsA.append([
            s["label"], f(s["retracted"], "d"), f(s["gained"], ","), f(s["recompute_lb"], ","), ub_text(s),
            f"{n(s['confirmed_sum'])} ({s['confirmed_n']})" if s["confirmed_n"] else "—",
            f(s["prefill_excess"], ","), s["baseline"] or "—", n(s["total_tokens"]),
            f(s["waste_pct"], ".2f") + ("%" if s["waste_pct"] is not None else "")])
    print(fmt_table(["run", "선점", "회수 KV", "재계산 하한", "재계산 상한", "확인된 재계산(건)",
                     "prefill 초과", "기준선", "총 처리 토큰", "낭비율"], rowsA, right={1, 2, 3, 4, 5, 6, 8, 9}))

    print("\n[B] 시간 — 피해 요청이 멈춘 시간")
    rowsB = []
    for s in sums:
        if s["log"]:
            match = f"{s['exact']}/{s['approx']}/{s['timeonly']}/{s['unmatched_slots']}" if s["events"] else "—"
        else:
            match = "로그 없음"
        rowsB.append([
            s["label"], s["stalls"], match, s["unmatched_stalls"] if s["log"] else "—",
            f"{s['stall_sum']:.1f}", f"{s['stall_mean']:.1f}", f"{s['stall_max']:.1f}", f(s["delay_mean"], ".1f"),
            f"{s['victims_slo_violation']}/{s['victims']}" if s["victims"] else "—",
            f(s["maxitl_p99_s"], ".1f"), f(s["ttft_p99_s"], ".1f"), f(s["out_tok_s"], ".0f"), f(s["goodput_rps"], ".3f")])
    print(fmt_table(["run", "정지", "매칭 정확/근사/시각/미매칭", "짝 없는 정지", "정지 합(s)", "건당(s)", "최대(s)",
                     "선점→재계산(s)", "피해 요청 SLO 위반", "maxITL p99(s)", "TTFT p99(s)", "out tok/s", "goodput"],
                    rowsB, right=set(range(1, 13))))

    for a, s in zip(analyses, sums):
        rows = a["rows"]
        if not rows:
            continue
        rel_log = os.path.relpath(a["run"]["log"], project) if a["run"]["log"] else "—"
        print(f"\n[{s['label']}] 서버 {rel_log}  ↔  클라이언트 {os.path.relpath(a['run']['results'], project)}")
        if a["run"]["log"]:
            print(f"   시계: 클라이언트 t0 ≈ 서버 {s['offset_utc'] or '—'} ({s['offset_how']})")
        tr = []
        for r in rows[: args.show]:
            e, st_, rp = r["e"], r["s"], r["rp"]
            g_txt = (n(e["gained"]) if e["k"] == 1 else f"Σ{n(e['gained'])}(k={e['k']})") if e else "—"
            tr.append([
                f"L{e['L']}/g{e['g']}" if e else "—", hhmmss(e["ts"]) if e else "—", g_txt,
                st_["rid"] if st_ else "—", n(st_["before"]) if st_ else "—", f"{st_['dur']:.1f}" if st_ else "—",
                f"L{rp['L']}/g{rp['g']} {rp['new']}+{rp['cached']}c" if rp else "—", r["method"]])
        print(fmt_table(["서버 줄", "시각", "회수 KV", "피해 요청", "정지 전 생성", "정지(s)",
                         "재계산 줄 new+cached", "판정"], tr, right={2, 4, 5}))
        if len(rows) > args.show:
            print(f"   … {len(rows) - args.show}건 더 (보고서 참고, --show {len(rows)} 로 전부 출력)")

    # ------------------------------------------------------------------ 파일
    os.makedirs(out_dir, exist_ok=True)
    ev_path = os.path.join(out_dir, "events.csv")
    with open(ev_path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["run", "event_id", "slot", "server_log", "log_line_vscode", "log_line_grep", "log_time",
                    "retracted_reqs", "event_new_tokens_gained", "new_token_ratio", "results_file", "record_index",
                    "rid", "chunk_index", "stall_start_s", "stall_s", "tokens_before_stall", "prompt_tokens",
                    "cached_tokens", "recompute_lb_event", "recompute_ub_row", "reprefill_line_vscode",
                    "reprefill_line_grep", "reprefill_time", "reprefill_new_token", "reprefill_cached_token",
                    "retract_to_reprefill_s", "victim_tpot_ms", "victim_slo_ok", "victim_error",
                    "victim_completion_tokens", "match_method", "token_relation"])
        for a, s in zip(analyses, sums):
            for r in a["rows"]:
                e, st_, rp, pr = r["e"], r["s"], r["rp"], r["pr"] or {}
                tp = pr.get("tpot_ms")
                w.writerow([
                    s["label"], r["eid"] or "", r["slot"] or "", disp(a["run"]["log"]),
                    e["L"] if e else "", e["g"] if e else "", hhmmss(e["ts"]) if e else "", e["k"] if e else "",
                    e["gained"] if e else "", e["ratio"] if e else "",
                    disp(a["run"]["results"]), st_["idx"] if st_ else "", st_["rid"] if st_ else "",
                    st_["chunk"] if st_ else "", f"{st_['t_start']:.3f}" if st_ else "", f"{st_['dur']:.3f}" if st_ else "",
                    st_["before"] if st_ else "", st_["prompt"] if st_ else "", st_["cached"] if st_ else "",
                    e["gained"] if (e and r["slot"] == 1) else "",
                    st_["before"] + st_["prompt"] - st_["cached"] if st_ else "",
                    rp["L"] if rp else "", rp["g"] if rp else "", hhmmss(rp["ts"]) if rp else "",
                    rp["new"] if rp else "", rp["cached"] if rp else "",
                    f"{rp['ts'] - e['ts']:.0f}" if (rp and e and e["ts"] is not None) else "",
                    f"{tp:.2f}" if tp is not None else "", "" if pr.get("error") else pr.get("slo_ok", ""),
                    pr.get("error") or "", st_["completion"] if st_ else "", r["method"], r["rel"] or ""])
    sm_path = os.path.join(out_dir, "summary.csv")
    with open(sm_path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=list(sums[0].keys()))
        w.writeheader()
        w.writerows(sums)
    rp_path = os.path.join(out_dir, "report.md")
    write_report(rp_path, args, project, analyses, sums)

    print("\n산출물")
    for p in (rp_path, ev_path, sm_path):
        print(f"  {p}")
    sg = sglang_root(args.sglang_src)
    if sg:   # terminal-only check of the pinned v0.5.18 line numbers used in report section 4
        bad = check_sglang_refs(sg)
        print(f"\nSGLang source lines ({sg}): {len(SRC_REFS) - len(bad)}/{len(SRC_REFS)} match the pinned v0.5.18 lines")
        for label, rel_, pinned, found in bad:
            print(f"  ! {rel_}: pinned {pinned}, found {found} — {label}")
    print("\n줄 번호: L = VS Code 기준, g = grep -n / sed -n 기준 (로그의 \\r 때문에 다르다). 시각은 로그에 적힌 그대로.")


def write_report(path, args, project, analyses, sums):
    rd = os.path.dirname(path)
    rel = lambda p: os.path.relpath(p, rd)
    link = lambda p, line: mdlink(f"L{line}", f"{rel(p)}#L{line}")
    L = []
    L.append("# Retraction 비용 분석 보고서\n")
    # No time stamp and no absolute path: the same inputs and arguments give a byte-identical report.
    # Options that do not change the files (--force, --show, --sglang-src) are left out of the command.
    shown, skip = [], 0
    for x in sys.argv[1:]:
        if skip:
            skip -= 1
        elif x == "--force":
            pass
        elif x in ("--show", "--sglang-src"):
            skip = 1
        elif not x.startswith(("--show=", "--sglang-src=")):
            shown.append(x)
    L.append(f"- 명령: `python3 -m analysis.retract_cost {' '.join(shown)}`")
    L.append(f"- project: `{os.path.relpath(project)}` (실행 폴더 기준) · "
             f"정지 기준: 토큰 간격 > {args.stall_sec:g}s · 매칭 허용 오차 ±{args.tol:g}s")
    L.append("- 줄 번호 링크는 VS Code 기준(L). 괄호 안 g 는 `grep -n` 기준. 시각은 로그에 적힌 그대로.")
    L.append("- 결과·로그 링크는 project 폴더 기준 상대 경로다. 이 저장소에서는 원시 데이터(data/RAW.md)를 "
             "저장소 루트에 풀어야 열린다.\n")

    L.append("## 1. 요약\n")
    L.append("### 토큰 — 회수한 KV vs 재계산\n")
    L.append(md_table(
        ["run", "선점", "회수 KV", "재계산 하한", "재계산 상한", "확인된 재계산(건)", "prefill 초과", "기준선", "총 처리 토큰", "낭비율", "낭비율 기준"],
        [[s["label"], f(s["retracted"], "d"), f(s["gained"], ","), f(s["recompute_lb"], ","), ub_text(s),
          f"{n(s['confirmed_sum'])} ({s['confirmed_n']})" if s["confirmed_n"] else "—", f(s["prefill_excess"], ","),
          s["baseline"] or ("없음" if s["baseline_note"] else "—"), n(s["total_tokens"]),
          f(s["waste_pct"], ".2f") + ("%" if s["waste_pct"] is not None else ""), s["waste_basis"] or "—"] for s in sums]))
    L.append("\n### 시간 — 피해 요청이 멈춘 시간\n")
    L.append(md_table(
        ["run", "정지", "매칭 정확/근사/시각/미매칭", "짝 없는 정지", "정지 합(s)", "건당(s)", "최대(s)", "선점→재계산(s)",
         "피해 요청 SLO 위반", "maxITL p99(s)", "TTFT p99(s)", "out tok/s", "goodput"],
        [[s["label"], s["stalls"],
          (f"{s['exact']}/{s['approx']}/{s['timeonly']}/{s['unmatched_slots']}" if s["events"] else "—") if s["log"] else "로그 없음",
          s["unmatched_stalls"] if s["log"] else "—", f"{s['stall_sum']:.1f}", f"{s['stall_mean']:.1f}",
          f"{s['stall_max']:.1f}", f(s["delay_mean"], ".1f"),
          f"{s['victims_slo_violation']}/{s['victims']}" if s["victims"] else "—",
          f(s["maxitl_p99_s"], ".1f"), f(s["ttft_p99_s"], ".1f"), f(s["out_tok_s"], ".0f"), f(s["goodput_rps"], ".3f")]
         for s in sums]))
    notes = [f"- {s['label']}: {s['baseline_note']}" for s in sums if s["baseline_note"]]
    if notes:
        L.append("\n" + "\n".join(notes))

    L.append("\n## 2. 각 열의 정의와 출처\n")
    L.append(md_table(["열", "정의", "출처"], [
        ["선점", "`#retracted_reqs` 합 (선점된 요청 수)", "서버 로그 `KV cache pool is full. Retract requests.` 줄"],
        ["회수 KV", "`#new_tokens_gained` 합 = 선점 직전·직후 빈 KV 슬롯 수 차이", "서버 로그 같은 줄 · 4절"],
        ["재계산 하한", "회수 KV. 풀린 KV 는 재개할 때 반드시 다시 계산된다", "서버 로그"],
        ["재계산 상한", "Σ(정지 전 생성 토큰 + 입력 − 첫 prefill 캐시 적중). 공유 프리픽스 말고 전부 다시 계산한 경우. "
                   "짝 없는 선점이 있으면 전부 알 수 없어 '≥' 로 표시", "클라이언트 `chunk_tokens`, `prompt_tokens`, `cached_tokens`"],
        ["확인된 재계산", "재개 시 그 요청 하나만 담긴 `Prefill batch, #new-seq: 1` 줄 가운데 "
                     "`#new-token + #cached-token = 입력 + 정지 전 생성`(±1)이고 재개 시각 부근인 줄의 `#new-token`", "서버 로그 Prefill 줄"],
        ["prefill 초과", "이 실행의 `#new-token` 합 − 기준선 실행의 합. 기준선 = 서버 로그가 있고 선점 0건, 전 요청 성공, "
                      "같은 트레이스·요청·입력 길이인 실행", "서버 로그 Prefill 줄 전체"],
        ["총 처리 토큰", "성공한 요청의 입력 + 출력 토큰", "클라이언트 `prompt_tokens`, `completion_tokens`"],
        ["낭비율", "prefill 초과(없으면 회수 KV) ÷ 총 처리 토큰", "위 두 열"],
        ["정지", f"한 요청 안에서 연속 두 토큰 간격이 {args.stall_sec:g}s 를 넘은 횟수 (선점과 짝 없는 것 포함)", "클라이언트 `chunk_times`"],
        ["매칭", "먼저 시계 오프셋을 추정한다(토큰이 정확히 맞는 후보 쌍 가운데 1초 안에 가장 많이 모인 무리의 중앙값 + 절삭 보정). "
               "그다음 모든 (선점, 정지) 후보를 토큰 일치 등급 → 시각 오차 순으로 전역 배정한다. "
               "'정확' = 회수 KV 가 정지 전 생성 토큰(+ 공유 안 된 입력분)과 같음, '근사' = ±2 토큰", "두 출처 대조"],
        ["선점→재계산", "선점 줄 시각 → 확인된 재계산 줄 시각 (로그 시각이 1초 단위라 ±1s)", "서버 로그"],
        ["피해 요청 SLO 위반", "선점된 요청 중 TPOT > SLO(60 ms) 인 수 / 선점된 요청 수 (오류 요청 제외)", "`w3.metrics.request_metrics`"],
        ["maxITL · TTFT · out tok/s · goodput", "`w3.metrics.summarize_run` 정의", "클라이언트 결과 파일"],
    ]))
    L.append("\n'재생성'이 아니라 **재계산**이다. 선점된 요청이 이미 만든 출력 토큰은 유지되고(`output_ids`), 버려지는 것은 KV 뿐이다. "
             "재개할 때 입력 + 지금까지의 출력을 prefill 로 다시 계산하며, radix 캐시에 남은 앞부분은 재사용한다.\n")
    L.append("서버에서 정확히 재려면(`--enable-metrics` 필요) 서버를 끄기 전에 `/metrics` 에서 "
             "`sglang:realtime_tokens_total{mode=\"prefill_compute\"}` − `sglang:prefill_effective_tokens_total{mode=\"input\"}` 을 받는다. "
             "`sglang:num_retracted_input_tokens_total` 은 선점 시점 요청의 입력 길이 합이라 재계산량이 아니다.\n")

    L.append("## 3. 실행별 상세와 근거\n")
    for a, s in zip(analyses, sums):
        run = a["run"]
        L.append(f"### {s['label']}\n")
        L.append(f"- 클라이언트: {mdlink(os.path.relpath(run['results'], project), rel(run['results']))} "
                 f"(요청 {a['res']['n']}건 · 성공 {a['res']['n_ok']}건, JSON 한 줄 파일 — 레코드 번호는 0부터)")
        if run["log"]:
            info = a["log"]["info"]
            extras = []
            if "kv_capacity" in info:
                cap, v, g = info["kv_capacity"]
                extras.append(f"KV 풀 {n(cap)} 토큰 {link(run['log'], v)}(g{g})")
            for k in ("schedule_policy", "chunked_prefill_size", "schedule_conservativeness"):
                if k in info:
                    extras.append(f"`{k}={info[k]}`")
            if "server_args" in info:
                v, g = info["server_args"]
                extras.append(f"server_args {link(run['log'], v)}(g{g})")
            L.append(f"- 서버: {mdlink(os.path.relpath(run['log'], project), rel(run['log']))} — {run['pair_note']}")
            if extras:
                L.append("- 서버 설정: " + " · ".join(extras))
            if info["skipped_rank_lines"]:
                L.append(f"- rank 0 이 아닌 줄 {info['skipped_rank_lines']}개는 중복이라 뺐다")
            L.append(f"- 시계: 클라이언트 t0 ≈ 서버 {s['offset_utc'] or '—'} ({s['offset_how']})")
        else:
            L.append(f"- 서버: 없음 — {run['pair_note']}. 토큰 열은 클라이언트 추정(상한)만 있다.")
        if not a["rows"]:
            L.append("\n선점도, 기준을 넘는 정지도 없다.\n")
            continue
        L.append("")
        hdr = ["#", "서버 선점 줄", "시각", "회수 KV", "new_token_ratio", "피해 요청 (레코드#, chunk#)", "정지 전 생성", "정지(s)",
               "재계산 하한~상한", "재계산 줄 (new/cached)", "선점→재계산", "TPOT(ms) SLO", "판정"]
        body = []
        for i, r in enumerate(a["rows"], 1):
            e, st_, rp, pr = r["e"], r["s"], r["rp"], r["pr"]
            ub_row = (st_["before"] + st_["prompt"] - st_["cached"]) if st_ else None
            if e and e["k"] == 1:
                g_txt, rng = n(e["gained"]), (f"{n(e['gained'])}~{n(ub_row)}" if st_ else f"{n(e['gained'])}~?")
            elif e:
                g_txt, rng = f"Σ{n(e['gained'])} (선점 {r['eid']}, k={e['k']})", (f"~{n(ub_row)}" if st_ else "—")
            else:
                g_txt, rng = "—", (f"~{n(ub_row)}" if st_ else "—")
            body.append([
                i, f"{link(run['log'], e['L'])} (g{e['g']})" if e else "—", hhmmss(e["ts"]) if e else "—", g_txt,
                e["ratio"] if e and e["ratio"] else "—",
                f"{st_['rid']} (#{st_['idx']}, chunk {st_['chunk']})" if st_ else "—",
                n(st_["before"]) if st_ else "—", f"{st_['dur']:.1f}" if st_ else "—", rng,
                f"{link(run['log'], rp['L'])} (g{rp['g']}) {rp['new']}/{rp['cached']}" if rp else "—",
                f"{rp['ts'] - e['ts']:.0f}s" if (rp and e and e["ts"] is not None) else "—",
                slo_mark(pr), r["method"] + (f" ({r['rel']})" if r["rel"] else ""),
            ])
        L.append(md_table(hdr, body))
        L.append("")

    L.append("## 4. 계산의 근거가 된 소스 위치\n")
    L.append("측정에 쓴 버전에 고정한 줄 번호다: SGLang v0.5.18(GitHub 태그) · 과정 하니스 커밋 802a164(링크만, "
             "이 저장소에는 없음) · 이 저장소의 지표 정의.\n")
    rows = [[label, mdlink(f"{os.path.basename(rel_)}:{line}", f"{SGLANG_URL}{rel_}#L{line}")]
            for label, rel_, _, line in SRC_REFS]
    rows += [[label, mdlink(f"{os.path.basename(rel_)}:{line}", f"{COURSE_URL}{rel_}#L{line}")]
             for label, rel_, line in COURSE_REFS]
    rows += [[label, mdlink(rel_, rel(os.path.join(DEFAULT_PROJECT, rel_)))] for label, rel_ in REPO_REFS]
    L.append(md_table(["근거", "위치"], rows))

    L.append("\n## 5. 한계\n")
    L.append("- 서버 로그 시각은 1초 단위(절삭)다. 오프셋에 0.5초 절삭 보정을 더하고 ±허용 오차 안에서 짝지은 뒤 토큰 수로 확인한다. "
             "`SGLANG_LOG_MS=1` 로 서버를 띄우면 밀리초 시각을 그대로 쓴다.")
    L.append("- 서버 로그에는 요청 ID 가 없고 replay 는 rid 를 서버에 보내지 않는다. 짝짓기는 시각과 토큰 수로 한다. "
             "응답 `meta_info.num_retractions` 를 결과 파일에 저장하면(현재 replay.py 는 버림) 요청별 선점 횟수를 직접 쓸 수 있다.")
    L.append("- 재개 prefill 이 다른 요청과 한 배치로 섞이거나 chunked prefill 로 쪼개지면 그 요청의 재계산 줄을 따로 떼어낼 수 없다('—'). "
             "이때는 실행 전체의 'prefill 초과'가 가장 정확한 재계산량이다.")
    L.append("- 'prefill 초과'는 기준선과 트레이스·요청·입력 길이가 같을 때만 계산한다. 헬스체크·워밍업 캐시 적중 차이로 수 토큰~수백 토큰 오차가 날 수 있다.")
    L.append("- 5초 넘는 정지가 모두 선점 때문은 아니다. 선점과 짝이 없는 정지는 '선점과 짝 없음'으로 따로 적었다.")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(L) + "\n")


if __name__ == "__main__":
    main()
