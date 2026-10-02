#!/usr/bin/env bash
# 실험 목록 파일을 위에서부터 한 줄씩 w3/run_one.sh 로 실행한다.
# 한 줄 형식:  <trace.jsonl> <out_dir> <tag> [서버 플래그...]     ('#' 로 시작하면 주석)
# 결과 파일이 이미 있고 에러가 0건이면 건너뛴다 → 중단된 큐를 같은 명령으로 이어서 돌릴 수 있다.
#
# 사용법 (project/ 에서):
#   bash w3/run_queue.sh w3/queues/p2_explore.txt
#   STOP 파일을 만들면(touch w3/STOP) 현재 실행이 끝난 뒤 멈춘다.
set -uo pipefail

Q=${1:?사용법: run_queue.sh <queue.txt>}
mkdir -p logs
QLOG="logs/w3_queue.log"
echo "[$(date '+%F %T')] 큐 시작: $Q" | tee -a "$QLOG"

while read -r TRACE OUT TAG FLAGS; do
  [ -z "${TRACE:-}" ] && continue
  case "$TRACE" in \#*) continue ;; esac
  if [ -f w3/STOP ]; then
    echo "[$(date '+%F %T')] w3/STOP 발견 → 큐 중단" | tee -a "$QLOG"; break
  fi
  STEM=$(basename "$TRACE" .jsonl)
  RES="${OUT}/${STEM}__${TAG}.json"
  if [ -f "$RES" ] && python - "$RES" <<'PY'
import json, sys
recs = json.load(open(sys.argv[1]))["records"]
sys.exit(0 if recs and not any(r["error"] for r in recs) else 1)
PY
  then
    echo "[$(date '+%F %T')] 건너뜀 (이미 있음): $RES" | tee -a "$QLOG"; continue
  fi
  # shellcheck disable=SC2086
  bash w3/run_one.sh "$TRACE" "$OUT" "$TAG" $FLAGS < /dev/null >> "$QLOG" 2>&1
  echo "[$(date '+%F %T')] rc=$? $RES" | tee -a "$QLOG"
done < "$Q"
echo "[$(date '+%F %T')] 큐 끝: $Q" | tee -a "$QLOG"
