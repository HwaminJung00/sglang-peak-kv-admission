#!/usr/bin/env bash
# bench.verify 를 여러 쌍에 돌리고 출력 전체를 logs/verify/ 에 남긴다.
# 기준(95%)은 바꾸지 않는다. bench/verify.py 는 수정하지 않는다.
#
# 사용법 (project/ 에서):
#   bash w3/verify_pairs.sh <ref.json> <test.json> [<ref2.json> <test2.json> ...]
set -uo pipefail
mkdir -p logs/verify
while [ $# -ge 2 ]; do
  REF=$1; TEST=$2; shift 2
  A=$(basename "$REF" .json); B=$(basename "$TEST" .json)
  D=$(basename "$(dirname "$REF")")
  OUTF="logs/verify/${D}__${A}__VS__${B}.txt"
  {
    echo "\$ python -m bench.verify $REF $TEST"
    python -m bench.verify "$REF" "$TEST"
    echo "(exit code $?)"
  } > "$OUTF" 2>&1
  printf '%-70s %s\n' "$A vs $B" "$(grep -E '완전 일치|=== (PASS|FAIL)' "$OUTF" | tr '\n' ' ')"
done
