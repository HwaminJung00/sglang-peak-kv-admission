#!/usr/bin/env bash
# α*, N* 를 고른 뒤 (python -m w3.select_star) 최종 큐 두 개를 만든다.
#   bash w3/make_queues.sh <ASTAR> <NSTAR>
# → w3/queues/p4_final.txt  (q2 3-bar r2·r3, 순서 교대)
#   w3/queues/p5p7_mine.txt (부하 곡선 · 손해 탐색 · deterministic 의 mine 쪽)
set -euo pipefail
A=${1:?사용법: make_queues.sh <ASTAR> <NSTAR>}
N=${2:?NSTAR 필요}
MINE="--enable-peak-kv-reservation --peak-kv-reserve-ratio $A"
Q2=traces/reasoning_q2.jsonl

cat > w3/queues/p4_final.txt <<EOF
# P4 — 최종 3-bar (q2). r1 은 P2/P3 의 같은 조건 실행을 쓴다. α*=$A, N*=$N
# r2: ablated → mine → default
$Q2 results ablated_r2 --chunked-prefill-size -1
$Q2 results mine_a${A}_r2 $MINE
$Q2 results default_r2
# r3: mine → default → ablated
$Q2 results mine_a${A}_r3 $MINE
$Q2 results default_r3
$Q2 results ablated_r3 --chunked-prefill-size -1
EOF

cat > w3/queues/p5p7_mine.txt <<EOF
# mine(α*=$A) 쪽: 부하 곡선 · 손해 탐색 · deterministic 정확성
traces/reasoning.jsonl results mine_a${A}_r1 $MINE
traces/reasoning_q0.5.jsonl results mine_a${A}_r1 $MINE
traces/reasoning_q1.jsonl results mine_a${A}_r1 $MINE
traces/reasoning_q4.jsonl results mine_a${A}_r1 $MINE
traces/reasoning_q2_s0.3.jsonl results mine_a${A}_r1 $MINE
traces/reasoning_q2_s1.0.jsonl results mine_a${A}_r1 $MINE
traces/reasoning_q2_m1000.jsonl results mine_a${A}_r1 $MINE
$Q2 results/det mine_a${A}_r1 --enable-deterministic-inference --attention-backend triton $MINE
# 4번째 control(tuned N*)도 같은 조건에서 — q2 에 맞춘 고정 제한이 다른 부하·길이 분포에서도 맞는가
traces/reasoning_q1.jsonl results tuned_n${N}_r1 --max-running-requests $N
traces/reasoning_q4.jsonl results tuned_n${N}_r1 --max-running-requests $N
traces/reasoning_q2_s0.3.jsonl results tuned_n${N}_r1 --max-running-requests $N
traces/reasoning_q2_s1.0.jsonl results tuned_n${N}_r1 --max-running-requests $N
traces/reasoning_q2_m1000.jsonl results tuned_n${N}_r1 --max-running-requests $N
# 불변 조건 스모크 (plan.md 3.3): page 정렬 — --page-size 16 에서 OOM·오류·선점 없이 끝나는가
$Q2 results mine_a${A}_p16_r1 $MINE --page-size 16
EOF
echo "만듦: w3/queues/p4_final.txt, w3/queues/p5p7_mine.txt (α*=$A, N*=$N)"
