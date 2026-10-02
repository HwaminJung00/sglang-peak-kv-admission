#!/usr/bin/env bash
# W3 실험 1회 = 서버 새로 시작 → 워밍업 포함 재생 → /metrics 저장 → 서버 종료.
#
# scripts/run_matrix.sh 의 run_bar() 와 같은 공통 플래그를 쓴다. 차이는 세 가지다.
#   1) 결과·로그 이름에 반복 번호를 붙일 수 있다 (run_matrix.sh 는 매번 덮어쓴다).
#   2) 서버를 끄기 전에 /metrics 를 저장한다 (재계산 토큰 집계용).
#   3) 이전 서버가 살아 있거나 GPU 가 비어 있지 않으면 시작하지 않는다.
# 기존 스크립트(scripts/*.sh)와 bench/* 는 수정하지 않는다.
#
# 사용법 (project/ 에서):
#   bash w3/run_one.sh <trace.jsonl> <out_dir> <tag> [서버 플래그...]
# 예:
#   bash w3/run_one.sh traces/reasoning_q2.jsonl results default_r1
#   bash w3/run_one.sh traces/reasoning_q2.jsonl results mine_a0.5_r1 \
#        --enable-peak-kv-reservation --peak-kv-reserve-ratio 0.5
#   bash w3/run_one.sh traces/reasoning_q2.jsonl results/det default_r1 \
#        --enable-deterministic-inference --attention-backend triton
#
# 결과: <out_dir>/<trace이름>__<tag>.json
# 로그: logs/<out_dir 이 results 가 아니면 그 하위 이름>/server_<trace이름>__<tag>.log
set -uo pipefail

TRACE=${1:?사용법: run_one.sh <trace.jsonl> <out_dir> <tag> [서버 플래그...]}
OUT=${2:?out_dir 필요}
TAG=${3:?tag 필요}
shift 3
EXTRA=("$@")

MODEL=${MODEL:-Qwen/Qwen3-4B}
PORT=${PORT:-30000}
URL="http://127.0.0.1:${PORT}"
STEM=$(basename "$TRACE" .jsonl)
SUB=${OUT#results}; SUB=${SUB#/}          # results/det -> det, results -> ""
LOGDIR="logs${SUB:+/$SUB}"
mkdir -p "$OUT" "$LOGDIR"
NAME="${STEM}__${TAG}"
SLOG="${LOGDIR}/server_${NAME}.log"
MLOG="${LOGDIR}/metrics_${NAME}.txt"
RLOG="${LOGDIR}/replay_${NAME}.txt"
RES="${OUT}/${NAME}.json"

# 공통 플래그: scripts/run_matrix.sh 의 COMMON 과 같다 (docs/01 고정 사항)
COMMON=(--model-path "$MODEL" --port "$PORT" --context-length 32768
        --mem-fraction-static 0.85 --random-seed 42 --log-level info --enable-metrics)

ts() { date '+%F %T'; }

# (0) 이전 서버 재사용 금지 (docs/step3_hint 7절)
if curl -sf "${URL}/health" >/dev/null 2>&1; then
  echo "[$(ts)] !! ${URL} 에 이미 서버가 떠 있다. 종료하고 다시 실행할 것." >&2; exit 3
fi
USED=$(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits | head -1)
if [ "${USED:-0}" -gt 1000 ]; then
  echo "[$(ts)] !! GPU 메모리 ${USED} MiB 사용 중. 다른 프로세스를 먼저 종료할 것." >&2; exit 3
fi

echo "[$(ts)] === ${NAME}  flags: ${EXTRA[*]:-(없음)} ==="
T0=$(date +%s)
python -m sglang.launch_server "${COMMON[@]}" "${EXTRA[@]}" > "$SLOG" 2>&1 &
PID=$!

cleanup() {
  kill "$PID" 2>/dev/null || true
  for _ in $(seq 1 60); do kill -0 "$PID" 2>/dev/null || break; sleep 1; done
  kill -9 "$PID" 2>/dev/null || true
  # 자식 프로세스(scheduler/detokenizer)가 남으면 GPU 를 잡고 있다
  pkill -f "sglang.launch_server --model-path" 2>/dev/null || true
  pkill -f "sglang::" 2>/dev/null || true
  for _ in $(seq 1 60); do
    U=$(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits | head -1)
    [ "${U:-0}" -lt 1000 ] && break; sleep 1
  done
  sleep 3
}
trap cleanup EXIT

READY=0
for _ in $(seq 1 240); do
  if curl -sf "${URL}/health_generate" >/dev/null 2>&1; then READY=1; break; fi
  kill -0 "$PID" 2>/dev/null || break
  sleep 2
done
if [ "$READY" != 1 ]; then
  echo "[$(ts)] !! 서버 준비 실패 → 이 bar 는 성능 결과에서 제외. 로그: $SLOG" >&2
  echo -e "$(ts)\t${OUT}\t${NAME}\tSERVER_FAIL\t-\t-\t${EXTRA[*]:-}" >> w3/runs.tsv
  exit 2
fi
T1=$(date +%s)

python -m bench.replay --trace "$TRACE" --url "$URL" --tag "$TAG" \
  --out "$OUT" --keep-output 2>&1 | tee "$RLOG"
RC=${PIPESTATUS[0]}

curl -sf "${URL}/metrics" > "$MLOG" 2>/dev/null || true
T2=$(date +%s)

NERR=$(python - "$RES" <<'PY' 2>/dev/null || echo "?"
import json, sys
recs = json.load(open(sys.argv[1]))["records"]
print(sum(1 for r in recs if r["error"]))
PY
)
STATUS=OK
[ "$RC" != 0 ] && STATUS="REPLAY_RC${RC}"
[ "$NERR" != "0" ] && STATUS="ERR${NERR}"
echo -e "$(ts)\t${OUT}\t${NAME}\t${STATUS}\tstartup=$((T1-T0))s\treplay=$((T2-T1))s\t${EXTRA[*]:-}" >> w3/runs.tsv
echo "[$(ts)] === ${NAME} 끝: ${STATUS} (서버 기동 $((T1-T0))s, 재생 $((T2-T1))s) ==="
[ "$STATUS" = OK ]
