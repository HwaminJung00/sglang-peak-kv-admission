"""
P3 선택 규칙을 결과에 그대로 적용해 α*, N* 를 고른다 (plan.md 4.3, 결과를 보기 전에 정한 규칙).

  1) 보조 SLO: 모든 요청이 '요청 안 최대 정지 ≤ 5 s' 를 만족 (stall5_reqs == 0)
  2) 그 가운데 TTFT p99 최소
  3) 만족하는 설정이 없으면 stall5_reqs 최소 → TTFT p99 최소

    python -m w3.select_star          # q2 탐색 결과(results/reasoning_q2__{mine_a*,tuned_n*}_r1.json)
"""

import glob
import re

from w3.summarize import run_row


def pick(pattern, key_re):
    rows = []
    for p in sorted(glob.glob(pattern)):
        m = re.search(key_re + r"\.json$", p)      # mine_a1.0_p16_r1 같은 스모크 실행은 제외
        if not m:
            continue
        rows.append((m.group(1), run_row(p)))
    print(f"\n{pattern}")
    print(f"{'설정':>8} {'stall5':>7} {'TTFT p99(s)':>12} {'선점':>5} {'goodput':>8} {'maxITL p99(s)':>14}")
    for k, r in sorted(rows, key=lambda x: float(x[0])):
        print(f"{k:>8} {r['stall5_reqs']:>7} {r['ttft_p99']/1000:>12.1f} {r.get('retracted', -1):>5} "
              f"{r['goodput_rps']:>8.3f} {r['maxitl_p99']/1000:>14.2f}")
    ok = [x for x in rows if x[1]["stall5_reqs"] == 0]
    pool = ok if ok else rows
    best = min(pool, key=lambda x: (x[1]["stall5_reqs"], x[1]["ttft_p99"]))
    print(f"=> 선택: {best[0]}  ({'보조 SLO 만족 중 TTFT p99 최소' if ok else '만족 설정 없음 → stall5 최소, TTFT p99 최소'})")
    return best[0]


if __name__ == "__main__":
    a = pick("results/reasoning_q2__mine_a*_r1.json", r"mine_a([\d.]+)_r1")
    n = pick("results/reasoning_q2__tuned_n*_r1.json", r"tuned_n(\d+)_r1")
    print(f"\nASTAR={a} NSTAR={n}")
