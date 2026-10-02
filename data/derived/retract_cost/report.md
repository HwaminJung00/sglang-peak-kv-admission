# Retraction 비용 분석 보고서

- 명령: `python3 -m analysis.retract_cost --sweep-0907 --out data/derived/retract_cost`
- project: `.` (실행 폴더 기준) · 정지 기준: 토큰 간격 > 5s · 매칭 허용 오차 ±2.5s
- 줄 번호 링크는 VS Code 기준(L). 괄호 안 g 는 `grep -n` 기준. 시각은 로그에 적힌 그대로.
- 결과·로그 링크는 project 폴더 기준 상대 경로다. 이 저장소에서는 원시 데이터(data/RAW.md)를 저장소 루트에 풀어야 열린다.

## 1. 요약

### 토큰 — 회수한 KV vs 재계산

| run | 선점 | 회수 KV | 재계산 하한 | 재계산 상한 | 확인된 재계산(건) | prefill 초과 | 기준선 | 총 처리 토큰 | 낭비율 | 낭비율 기준 |
|---|---|---|---|---|---|---|---|---|---|---|
| q0.5 | 0 | 0 | 0 | 0 | — | 0 | q0.5 | 383,867 | 0.00% | prefill 초과분 |
| q0.6 | 1 | 857 | 857 | 857 | — | 857 | q0.5 | 383,867 | 0.22% | prefill 초과분 |
| q0.8 | 4 | 4,070 | 4,070 | 5,474 | — | 5,477 | q0.5 | 383,867 | 1.43% | prefill 초과분 |
| q1 | 7 | 7,174 | 7,174 | 8,228 | 3,414 (4) | 8,235 | q0.5 | 383,867 | 2.15% | prefill 초과분 |
| q2 | 20 | 18,639 | 18,639 | 21,448 | 4,991 (5) | 21,452 | q0.5 | 383,867 | 5.59% | prefill 초과분 |
| q4 | 29 | 25,561 | 25,561 | 26,967 | 13,750 (13) | 26,972 | q0.5 | 383,867 | 7.03% | prefill 초과분 |
| q8 | 28 | 30,532 | 30,532 | 32,289 | 19,760 (16) | 32,294 | q0.5 | 383,867 | 8.41% | prefill 초과분 |

### 시간 — 피해 요청이 멈춘 시간

| run | 정지 | 매칭 정확/근사/시각/미매칭 | 짝 없는 정지 | 정지 합(s) | 건당(s) | 최대(s) | 선점→재계산(s) | 피해 요청 SLO 위반 | maxITL p99(s) | TTFT p99(s) | out tok/s | goodput |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| q0.5 | 0 | — | 0 | 0.0 | 0.0 | 0.0 | — | — | 0.1 | 0.1 | 1026 | 0.365 |
| q0.6 | 1 | 1/0/0/0 | 0 | 9.1 | 9.1 | 9.1 | — | 0/1 | 0.1 | 16.2 | 1095 | 0.390 |
| q0.8 | 4 | 4/0/0/0 | 0 | 98.1 | 24.5 | 25.0 | — | 0/4 | 24.6 | 29.1 | 1144 | 0.407 |
| q1 | 7 | 7/0/0/0 | 0 | 147.3 | 21.0 | 35.3 | 13.8 | 0/7 | 29.5 | 46.5 | 1196 | 0.425 |
| q2 | 20 | 20/0/0/0 | 0 | 1113.9 | 55.7 | 64.1 | 47.6 | 4/20 | 62.6 | 63.9 | 1183 | 0.407 |
| q4 | 29 | 28/1/0/0 | 0 | 2240.8 | 77.3 | 107.3 | 79.8 | 12/29 | 102.5 | 75.7 | 1184 | 0.379 |
| q8 | 28 | 27/1/0/0 | 0 | 2517.4 | 89.9 | 116.7 | 98.8 | 14/28 | 116.2 | 78.2 | 1162 | 0.365 |

## 2. 각 열의 정의와 출처

| 열 | 정의 | 출처 |
|---|---|---|
| 선점 | `#retracted_reqs` 합 (선점된 요청 수) | 서버 로그 `KV cache pool is full. Retract requests.` 줄 |
| 회수 KV | `#new_tokens_gained` 합 = 선점 직전·직후 빈 KV 슬롯 수 차이 | 서버 로그 같은 줄 · 4절 |
| 재계산 하한 | 회수 KV. 풀린 KV 는 재개할 때 반드시 다시 계산된다 | 서버 로그 |
| 재계산 상한 | Σ(정지 전 생성 토큰 + 입력 − 첫 prefill 캐시 적중). 공유 프리픽스 말고 전부 다시 계산한 경우. 짝 없는 선점이 있으면 전부 알 수 없어 '≥' 로 표시 | 클라이언트 `chunk_tokens`, `prompt_tokens`, `cached_tokens` |
| 확인된 재계산 | 재개 시 그 요청 하나만 담긴 `Prefill batch, #new-seq: 1` 줄 가운데 `#new-token + #cached-token = 입력 + 정지 전 생성`(±1)이고 재개 시각 부근인 줄의 `#new-token` | 서버 로그 Prefill 줄 |
| prefill 초과 | 이 실행의 `#new-token` 합 − 기준선 실행의 합. 기준선 = 서버 로그가 있고 선점 0건, 전 요청 성공, 같은 트레이스·요청·입력 길이인 실행 | 서버 로그 Prefill 줄 전체 |
| 총 처리 토큰 | 성공한 요청의 입력 + 출력 토큰 | 클라이언트 `prompt_tokens`, `completion_tokens` |
| 낭비율 | prefill 초과(없으면 회수 KV) ÷ 총 처리 토큰 | 위 두 열 |
| 정지 | 한 요청 안에서 연속 두 토큰 간격이 5s 를 넘은 횟수 (선점과 짝 없는 것 포함) | 클라이언트 `chunk_times` |
| 매칭 | 먼저 시계 오프셋을 추정한다(토큰이 정확히 맞는 후보 쌍 가운데 1초 안에 가장 많이 모인 무리의 중앙값 + 절삭 보정). 그다음 모든 (선점, 정지) 후보를 토큰 일치 등급 → 시각 오차 순으로 전역 배정한다. '정확' = 회수 KV 가 정지 전 생성 토큰(+ 공유 안 된 입력분)과 같음, '근사' = ±2 토큰 | 두 출처 대조 |
| 선점→재계산 | 선점 줄 시각 → 확인된 재계산 줄 시각 (로그 시각이 1초 단위라 ±1s) | 서버 로그 |
| 피해 요청 SLO 위반 | 선점된 요청 중 TPOT > SLO(60 ms) 인 수 / 선점된 요청 수 (오류 요청 제외) | `w3.metrics.request_metrics` |
| maxITL · TTFT · out tok/s · goodput | `w3.metrics.summarize_run` 정의 | 클라이언트 결과 파일 |

'재생성'이 아니라 **재계산**이다. 선점된 요청이 이미 만든 출력 토큰은 유지되고(`output_ids`), 버려지는 것은 KV 뿐이다. 재개할 때 입력 + 지금까지의 출력을 prefill 로 다시 계산하며, radix 캐시에 남은 앞부분은 재사용한다.

서버에서 정확히 재려면(`--enable-metrics` 필요) 서버를 끄기 전에 `/metrics` 에서 `sglang:realtime_tokens_total{mode="prefill_compute"}` − `sglang:prefill_effective_tokens_total{mode="input"}` 을 받는다. `sglang:num_retracted_input_tokens_total` 은 선점 시점 요청의 입력 길이 합이라 재계산량이 아니다.

## 3. 실행별 상세와 근거

### q0.5

- 클라이언트: [results/reasoning_q0.5__default.json](../../../results/reasoning_q0.5__default.json) (요청 120건 · 성공 120건, JSON 한 줄 파일 — 레코드 번호는 0부터)
- 서버: [logs/server_q0.5.log](../../../logs/server_q0.5.log) — --sweep-0907 (09-07 스윕 로그 이름 server_q<Q>.log)
- 서버 설정: KV 풀 87,552 토큰 [L139](../../../logs/server_q0.5.log#L139)(g61) · `schedule_policy=fcfs` · `chunked_prefill_size=2048` · `schedule_conservativeness=1.0` · server_args [L7](../../../logs/server_q0.5.log#L7)(g7)
- 시계: 클라이언트 t0 ≈ 서버 — (추정 불가 — 토큰이 맞는 쌍이 없고 선점 슬롯 수와 정지 수가 다르다)

선점도, 기준을 넘는 정지도 없다.

### q0.6

- 클라이언트: [results/reasoning_q0.6__default.json](../../../results/reasoning_q0.6__default.json) (요청 120건 · 성공 120건, JSON 한 줄 파일 — 레코드 번호는 0부터)
- 서버: [logs/server_q0.6.log](../../../logs/server_q0.6.log) — --sweep-0907 (09-07 스윕 로그 이름 server_q<Q>.log)
- 서버 설정: KV 풀 87,552 토큰 [L139](../../../logs/server_q0.6.log#L139)(g61) · `schedule_policy=fcfs` · `chunked_prefill_size=2048` · `schedule_conservativeness=1.0` · server_args [L7](../../../logs/server_q0.6.log#L7)(g7)
- 시계: 클라이언트 t0 ≈ 서버 11:17:59 (토큰이 정확히 맞는 후보 1쌍 중 1s 안에 모인 1쌍의 시각 차 중앙값 + 로그 시각 절삭 보정 0.5s; 첫 토큰↔Prefill 줄 일치율 99%)

| # | 서버 선점 줄 | 시각 | 회수 KV | new_token_ratio | 피해 요청 (레코드#, chunk#) | 정지 전 생성 | 정지(s) | 재계산 하한~상한 | 재계산 줄 (new/cached) | 선점→재계산 | TPOT(ms) SLO | 판정 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | [L553](../../../logs/server_q0.6.log#L553) (g475) | 11:20:22 | 857 | 0.0980 -> 0.5929 | reason-00087 (#87, chunk 506) | 506 | 9.1 | 857~857 | — | — | 29.8 ✓ | 시각+토큰 정확 (exact-out+prompt) |

### q0.8

- 클라이언트: [results/reasoning_q0.8__default.json](../../../results/reasoning_q0.8__default.json) (요청 120건 · 성공 120건, JSON 한 줄 파일 — 레코드 번호는 0부터)
- 서버: [logs/server_q0.8.log](../../../logs/server_q0.8.log) — --sweep-0907 (09-07 스윕 로그 이름 server_q<Q>.log)
- 서버 설정: KV 풀 87,552 토큰 [L137](../../../logs/server_q0.8.log#L137)(g59) · `schedule_policy=fcfs` · `chunked_prefill_size=2048` · `schedule_conservativeness=1.0` · server_args [L7](../../../logs/server_q0.8.log#L7)(g7)
- 시계: 클라이언트 t0 ≈ 서버 11:23:49 (토큰이 정확히 맞는 후보 4쌍 중 1s 안에 모인 4쌍의 시각 차 중앙값 + 로그 시각 절삭 보정 0.5s; 첫 토큰↔Prefill 줄 일치율 100%)

| # | 서버 선점 줄 | 시각 | 회수 KV | new_token_ratio | 피해 요청 (레코드#, chunk#) | 정지 전 생성 | 정지(s) | 재계산 하한~상한 | 재계산 줄 (new/cached) | 선점→재계산 | TPOT(ms) SLO | 판정 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | [L475](../../../logs/server_q0.8.log#L475) (g397) | 11:25:32 | 744 | 0.0980 -> 0.5762 | reason-00075 (#75, chunk 744) | 744 | 24.6 | 744~1,095 | — | — | 43.2 ✓ | 시각+토큰 정확 (exact-out) |
| 2 | [L477](../../../logs/server_q0.8.log#L477) (g399) | 11:25:33 | 1,008 | 0.5451 -> 0.5844 | reason-00074 (#74, chunk 1008) | 1,008 | 24.5 | 1,008~1,359 | — | — | 39.2 ✓ | 시각+토큰 정확 (exact-out) |
| 3 | [L480](../../../logs/server_q0.8.log#L480) (g402) | 11:25:34 | 1,104 | 0.5443 -> 0.5974 | reason-00072 (#72, chunk 1104) | 1,104 | 25.0 | 1,104~1,455 | — | — | 34.9 ✓ | 시각+토큰 정확 (exact-out) |
| 4 | [L482](../../../logs/server_q0.8.log#L482) (g404) | 11:25:35 | 1,214 | 0.5523 -> 0.6093 | reason-00071 (#71, chunk 1214) | 1,214 | 23.9 | 1,214~1,565 | — | — | 36.2 ✓ | 시각+토큰 정확 (exact-out) |

### q1

- 클라이언트: [results/reasoning_q1__default.json](../../../results/reasoning_q1__default.json) (요청 120건 · 성공 120건, JSON 한 줄 파일 — 레코드 번호는 0부터)
- 서버: [logs/server_q1.log](../../../logs/server_q1.log) — --sweep-0907 (09-07 스윕 로그 이름 server_q<Q>.log)
- 서버 설정: KV 풀 87,552 토큰 [L139](../../../logs/server_q1.log#L139)(g61) · `schedule_policy=fcfs` · `chunked_prefill_size=2048` · `schedule_conservativeness=1.0` · server_args [L7](../../../logs/server_q1.log#L7)(g7)
- 시계: 클라이언트 t0 ≈ 서버 09:56:06 (토큰이 정확히 맞는 후보 7쌍 중 1s 안에 모인 7쌍의 시각 차 중앙값 + 로그 시각 절삭 보정 0.5s; 첫 토큰↔Prefill 줄 일치율 100%)

| # | 서버 선점 줄 | 시각 | 회수 KV | new_token_ratio | 피해 요청 (레코드#, chunk#) | 정지 전 생성 | 정지(s) | 재계산 하한~상한 | 재계산 줄 (new/cached) | 선점→재계산 | TPOT(ms) SLO | 판정 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | [L457](../../../logs/server_q1.log#L457) (g379) | 09:57:35 | 1,003 | 0.0980 -> 0.5538 | reason-00072 (#72, chunk 1003) | 1,003 | 29.7 | 1,003~1,354 | — | — | 37.0 ✓ | 시각+토큰 정확 (exact-out) |
| 2 | [L460](../../../logs/server_q1.log#L460) (g382) | 09:57:35 | 1,061 | 0.5167 -> 0.5644 | reason-00071 (#71, chunk 1061) | 1,061 | 28.8 | 1,061~1,412 | — | — | 38.5 ✓ | 시각+토큰 정확 (exact-out) |
| 3 | [L486](../../../logs/server_q1.log#L486) (g408) | 09:57:46 | 1,696 | 0.1310 -> 0.6419 | reason-00070 (#70, chunk 1696) | 1,696 | 35.3 | 1,696~2,048 | — | — | 34.0 ✓ | 시각+토큰 정확 (exact-out) |
| 4 | [L563](../../../logs/server_q1.log#L563) (g485) | 09:58:35 | 744 | 0.0980 -> 0.4169 | reason-00119 (#119, chunk 393) | 393 | 13.0 | 744~744 | [L580](../../../logs/server_q1.log#L580) (g502) 744/36 | 13s | 23.2 ✓ | 시각+토큰 정확 (exact-out+prompt) |
| 5 | [L564](../../../logs/server_q1.log#L564) (g486) | 09:58:35 | 858 | 0.4028 -> 0.4238 | reason-00118 (#118, chunk 507) | 507 | 12.7 | 858~858 | [L581](../../../logs/server_q1.log#L581) (g503) 858/36 | 13s | 28.3 ✓ | 시각+토큰 정확 (exact-out+prompt) |
| 6 | [L566](../../../logs/server_q1.log#L566) (g488) | 09:58:35 | 876 | 0.4068 -> 0.4333 | reason-00117 (#117, chunk 525) | 525 | 13.3 | 876~876 | [L583](../../../logs/server_q1.log#L583) (g505) 876/36 | 14s | 26.9 ✓ | 시각+토큰 정확 (exact-out+prompt) |
| 7 | [L567](../../../logs/server_q1.log#L567) (g489) | 09:58:36 | 936 | 0.4152 -> 0.4458 | reason-00116 (#116, chunk 585) | 585 | 14.6 | 936~936 | [L586](../../../logs/server_q1.log#L586) (g508) 936/36 | 15s | 25.3 ✓ | 시각+토큰 정확 (exact-out+prompt) |

### q2

- 클라이언트: [results/reasoning_q2__default.json](../../../results/reasoning_q2__default.json) (요청 120건 · 성공 120건, JSON 한 줄 파일 — 레코드 번호는 0부터)
- 서버: [logs/server_q2.log](../../../logs/server_q2.log) — --sweep-0907 (09-07 스윕 로그 이름 server_q<Q>.log)
- 서버 설정: KV 풀 87,552 토큰 [L139](../../../logs/server_q2.log#L139)(g61) · `schedule_policy=fcfs` · `chunked_prefill_size=2048` · `schedule_conservativeness=1.0` · server_args [L7](../../../logs/server_q2.log#L7)(g7)
- 시계: 클라이언트 t0 ≈ 서버 10:01:26 (토큰이 정확히 맞는 후보 22쌍 중 1s 안에 모인 21쌍의 시각 차 중앙값 + 로그 시각 절삭 보정 0.5s — 우연히 토큰만 맞은 1쌍 제외; 첫 토큰↔Prefill 줄 일치율 99%)

| # | 서버 선점 줄 | 시각 | 회수 KV | new_token_ratio | 피해 요청 (레코드#, chunk#) | 정지 전 생성 | 정지(s) | 재계산 하한~상한 | 재계산 줄 (new/cached) | 선점→재계산 | TPOT(ms) SLO | 판정 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | [L387](../../../logs/server_q2.log#L387) (g309) | 10:02:06 | 652 | 0.0980 -> 0.3485 | reason-00076 (#76, chunk 301) | 301 | 55.0 | 652~652 | [L498](../../../logs/server_q2.log#L498) (g420) 652/36 | 55s | 50.2 ✓ | 시각+토큰 정확 (exact-out+prompt) |
| 2 | [L389](../../../logs/server_q2.log#L389) (g311) | 10:02:07 | 663 | 0.3395 -> 0.3527 | reason-00075 (#75, chunk 312) | 312 | 56.4 | 663~663 | — | — | 67.0 ✗ | 시각+토큰 정확 (exact-out+prompt) |
| 3 | [L390](../../../logs/server_q2.log#L390) (g312) | 10:02:07 | 759 | 0.3427 -> 0.3575 | reason-00074 (#74, chunk 408) | 408 | 59.7 | 759~759 | — | — | 59.9 ✓ | 시각+토큰 정확 (exact-out+prompt) |
| 4 | [L392](../../../logs/server_q2.log#L392) (g314) | 10:02:07 | 773 | 0.3465 -> 0.3608 | reason-00073 (#73, chunk 422) | 422 | 59.4 | 773~773 | — | — | 99.4 ✗ | 시각+토큰 정확 (exact-out+prompt) |
| 5 | [L394](../../../logs/server_q2.log#L394) (g316) | 10:02:08 | 790 | 0.3497 -> 0.3672 | reason-00072 (#72, chunk 439) | 439 | 59.6 | 790~790 | — | — | 49.7 ✓ | 시각+토큰 정확 (exact-out+prompt) |
| 6 | [L396](../../../logs/server_q2.log#L396) (g318) | 10:02:08 | 848 | 0.3351 -> 0.3766 | reason-00071 (#71, chunk 497) | 497 | 58.8 | 848~848 | — | — | 53.5 ✓ | 시각+토큰 정확 (exact-out+prompt) |
| 7 | [L397](../../../logs/server_q2.log#L397) (g319) | 10:02:09 | 905 | 0.3625 -> 0.3863 | reason-00070 (#70, chunk 553) | 553 | 58.5 | 905~905 | — | — | 40.3 ✓ | 시각+토큰 정확 (exact-out+prompt) |
| 8 | [L404](../../../logs/server_q2.log#L404) (g326) | 10:02:10 | 976 | 0.3341 -> 0.4042 | reason-00069 (#69, chunk 625) | 625 | 61.4 | 976~976 | [L515](../../../logs/server_q2.log#L515) (g437) 977/35 | 61s | 37.0 ✓ | 시각+토큰 정확 (exact-out+prompt) |
| 9 | [L407](../../../logs/server_q2.log#L407) (g329) | 10:02:11 | 997 | 0.3882 -> 0.4075 | reason-00068 (#68, chunk 646) | 646 | 61.4 | 997~997 | — | — | 119.3 ✗ | 시각+토큰 정확 (exact-out+prompt) |
| 10 | [L418](../../../logs/server_q2.log#L418) (g340) | 10:02:13 | 1,133 | 0.3272 -> 0.4438 | reason-00066 (#66, chunk 782) | 782 | 64.1 | 1,133~1,133 | — | — | 26.9 ✓ | 시각+토큰 정확 (exact-out+prompt) |
| 11 | [L428](../../../logs/server_q2.log#L428) (g350) | 10:02:15 | 1,233 | 0.3475 -> 0.4666 | reason-00065 (#65, chunk 882) | 882 | 62.9 | 1,233~1,233 | — | — | 48.9 ✓ | 시각+토큰 정확 (exact-out+prompt) |
| 12 | [L431](../../../logs/server_q2.log#L431) (g353) | 10:02:16 | 1,004 | 0.4114 -> 0.5097 | reason-00064 (#64, chunk 1004) | 1,004 | 61.5 | 1,004~1,355 | — | — | 24.7 ✓ | 시각+토큰 정확 (exact-out) |
| 13 | [L433](../../../logs/server_q2.log#L433) (g355) | 10:02:17 | 1,062 | 0.4816 -> 0.5197 | reason-00063 (#63, chunk 1062) | 1,062 | 61.3 | 1,062~1,413 | — | — | 53.7 ✓ | 시각+토큰 정확 (exact-out) |
| 14 | [L438](../../../logs/server_q2.log#L438) (g360) | 10:02:19 | 1,151 | 0.4445 -> 0.5442 | reason-00062 (#62, chunk 1151) | 1,151 | 59.5 | 1,151~1,502 | — | — | 42.0 ✓ | 시각+토큰 정확 (exact-out) |
| 15 | [L445](../../../logs/server_q2.log#L445) (g367) | 10:02:21 | 1,245 | 0.4539 -> 0.5724 | reason-00061 (#61, chunk 1245) | 1,245 | 61.6 | 1,245~1,596 | — | — | 42.8 ✓ | 시각+토큰 정확 (exact-out) |
| 16 | [L449](../../../logs/server_q2.log#L449) (g371) | 10:02:24 | 1,370 | 0.4871 -> 0.5909 | reason-00060 (#60, chunk 1370) | 1,370 | 60.2 | 1,370~1,722 | — | — | 65.7 ✗ | 시각+토큰 정확 (exact-out) |
| 17 | [L486](../../../logs/server_q2.log#L486) (g408) | 10:02:53 | 769 | 0.0980 -> 0.6150 | reason-00089 (#89, chunk 418) | 418 | 30.9 | 769~769 | — | — | 44.6 ✓ | 시각+토큰 정확 (exact-out+prompt) |
| 18 | [L487](../../../logs/server_q2.log#L487) (g409) | 10:02:53 | 736 | 0.5939 -> 0.6396 | reason-00088 (#88, chunk 736) | 736 | 32.0 | 736~1,087 | [L545](../../../logs/server_q2.log#L545) (g467) 1087/36 | 32s | 29.2 ✓ | 시각+토큰 정확 (exact-out) |
| 19 | [L489](../../../logs/server_q2.log#L489) (g411) | 10:02:54 | 769 | 0.6075 -> 0.6498 | reason-00087 (#87, chunk 769) | 769 | 31.8 | 769~1,120 | [L547](../../../logs/server_q2.log#L547) (g469) 1120/36 | 32s | 47.4 ✓ | 시각+토큰 정확 (exact-out) |
| 20 | [L491](../../../logs/server_q2.log#L491) (g413) | 10:02:55 | 804 | 0.6157 -> 0.6991 | reason-00086 (#86, chunk 804) | 804 | 58.0 | 804~1,155 | [L575](../../../logs/server_q2.log#L575) (g497) 1155/36 | 58s | 26.1 ✓ | 시각+토큰 정확 (exact-out) |

### q4

- 클라이언트: [results/reasoning_q4__default.json](../../../results/reasoning_q4__default.json) (요청 120건 · 성공 120건, JSON 한 줄 파일 — 레코드 번호는 0부터)
- 서버: [logs/server_q4.log](../../../logs/server_q4.log) — --sweep-0907 (09-07 스윕 로그 이름 server_q<Q>.log)
- 서버 설정: KV 풀 87,552 토큰 [L139](../../../logs/server_q4.log#L139)(g61) · `schedule_policy=fcfs` · `chunked_prefill_size=2048` · `schedule_conservativeness=1.0` · server_args [L7](../../../logs/server_q4.log#L7)(g7)
- 시계: 클라이언트 t0 ≈ 서버 10:06:48 (토큰이 정확히 맞는 후보 28쌍 중 1s 안에 모인 28쌍의 시각 차 중앙값 + 로그 시각 절삭 보정 0.5s; 첫 토큰↔Prefill 줄 일치율 100%)

| # | 서버 선점 줄 | 시각 | 회수 KV | new_token_ratio | 피해 요청 (레코드#, chunk#) | 정지 전 생성 | 정지(s) | 재계산 하한~상한 | 재계산 줄 (new/cached) | 선점→재계산 | TPOT(ms) SLO | 판정 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | [L390](../../../logs/server_q4.log#L390) (g312) | 10:07:13 | 597 | 0.0980 -> 0.2617 | reason-00085 (#85, chunk 247) | 247 | 69.5 | 597~597 | — | — | 71.8 ✗ | 시각+토큰 정확 (exact-out+prompt) |
| 2 | [L391](../../../logs/server_q4.log#L391) (g313) | 10:07:13 | 632 | 0.2557 -> 0.2693 | reason-00084 (#84, chunk 281) | 281 | 71.6 | 632~632 | — | — | 36.0 ✓ | 시각+토큰 정확 (exact-out+prompt) |
| 3 | [L392](../../../logs/server_q4.log#L392) (g314) | 10:07:13 | 650 | 0.2632 -> 0.2730 | reason-00083 (#83, chunk 299) | 299 | 71.4 | 650~650 | — | — | 58.9 ✓ | 시각+토큰 정확 (exact-out+prompt) |
| 4 | [L393](../../../logs/server_q4.log#L393) (g315) | 10:07:14 | 658 | 0.2660 -> 0.2779 | reason-00082 (#82, chunk 307) | 307 | 71.2 | 658~658 | — | — | 51.7 ✓ | 시각+토큰 정확 (exact-out+prompt) |
| 5 | [L395](../../../logs/server_q4.log#L395) (g317) | 10:07:14 | 673 | 0.2698 -> 0.2821 | reason-00081 (#81, chunk 322) | 322 | 72.1 | 673~674 | — | — | 62.6 ✗ | 시각+토큰 근사 (approx) |
| 6 | [L397](../../../logs/server_q4.log#L397) (g319) | 10:07:14 | 682 | 0.2751 -> 0.2856 | reason-00080 (#80, chunk 330) | 330 | 71.9 | 682~682 | — | — | 68.7 ✗ | 시각+토큰 정확 (exact-out+prompt) |
| 7 | [L400](../../../logs/server_q4.log#L400) (g322) | 10:07:14 | 690 | 0.2776 -> 0.2915 | reason-00079 (#79, chunk 339) | 339 | 71.7 | 690~690 | — | — | 47.7 ✓ | 시각+토큰 정확 (exact-out+prompt) |
| 8 | [L401](../../../logs/server_q4.log#L401) (g323) | 10:07:15 | 710 | 0.2835 -> 0.2966 | reason-00078 (#78, chunk 359) | 359 | 71.5 | 710~710 | — | — | 52.7 ✓ | 시각+토큰 정확 (exact-out+prompt) |
| 9 | [L403](../../../logs/server_q4.log#L403) (g325) | 10:07:15 | 722 | 0.2886 -> 0.3043 | reason-00077 (#77, chunk 371) | 371 | 73.6 | 722~722 | — | — | 40.9 ✓ | 시각+토큰 정확 (exact-out+prompt) |
| 10 | [L404](../../../logs/server_q4.log#L404) (g326) | 10:07:15 | 767 | 0.2953 -> 0.3092 | reason-00076 (#76, chunk 416) | 416 | 73.4 | 767~767 | — | — | 58.6 ✓ | 시각+토큰 정확 (exact-out+prompt) |
| 11 | [L406](../../../logs/server_q4.log#L406) (g328) | 10:07:15 | 781 | 0.3001 -> 0.3127 | reason-00075 (#75, chunk 430) | 430 | 75.3 | 781~781 | — | — | 81.0 ✗ | 시각+토큰 정확 (exact-out+prompt) |
| 12 | [L409](../../../logs/server_q4.log#L409) (g331) | 10:07:16 | 828 | 0.3027 -> 0.3170 | reason-00074 (#74, chunk 477) | 477 | 78.9 | 828~828 | [L528](../../../logs/server_q4.log#L528) (g450) 828/36 | 78s | 71.2 ✗ | 시각+토큰 정확 (exact-out+prompt) |
| 13 | [L411](../../../logs/server_q4.log#L411) (g333) | 10:07:16 | 841 | 0.3059 -> 0.3200 | reason-00073 (#73, chunk 490) | 490 | 79.1 | 841~841 | [L529](../../../logs/server_q4.log#L529) (g451) 841/36 | 79s | 123.9 ✗ | 시각+토큰 정확 (exact-out+prompt) |
| 14 | [L413](../../../logs/server_q4.log#L413) (g335) | 10:07:16 | 854 | 0.3100 -> 0.3253 | reason-00072 (#72, chunk 503) | 503 | 81.6 | 854~854 | — | — | 58.8 ✓ | 시각+토큰 정확 (exact-out+prompt) |
| 15 | [L415](../../../logs/server_q4.log#L415) (g337) | 10:07:18 | 923 | 0.2702 -> 0.3377 | reason-00071 (#71, chunk 572) | 572 | 80.1 | 923~923 | — | — | 63.8 ✗ | 시각+토큰 정확 (exact-out+prompt) |
| 16 | [L417](../../../logs/server_q4.log#L417) (g339) | 10:07:18 | 960 | 0.3246 -> 0.3459 | reason-00070 (#70, chunk 608) | 608 | 82.4 | 960~960 | [L538](../../../logs/server_q4.log#L538) (g460) 959/36 | 82s | 46.4 ✓ | 시각+토큰 정확 (exact-out+prompt) |
| 17 | [L422](../../../logs/server_q4.log#L422) (g344) | 10:07:22 | 1,131 | 0.1844 -> 0.3808 | reason-00069 (#69, chunk 780) | 780 | 79.9 | 1,131~1,131 | [L540](../../../logs/server_q4.log#L540) (g462) 1132/35 | 80s | 40.9 ✓ | 시각+토큰 정확 (exact-out+prompt) |
| 18 | [L423](../../../logs/server_q4.log#L423) (g345) | 10:07:22 | 1,180 | 0.3627 -> 0.4032 | reason-00066 (#66, chunk 829) | 829 | 80.4 | 1,180~1,180 | [L542](../../../logs/server_q4.log#L542) (g464) 1180/36 | 81s | 28.1 ✓ | 시각+토큰 정확 (exact-out+prompt) |
| 19 | [L425](../../../logs/server_q4.log#L425) (g347) | 10:07:23 | 1,201 | 0.3841 -> 0.4114 | reason-00065 (#65, chunk 850) | 850 | 82.2 | 1,201~1,201 | [L545](../../../logs/server_q4.log#L545) (g467) 1201/36 | 82s | 56.6 ✓ | 시각+토큰 정확 (exact-out+prompt) |
| 20 | [L426](../../../logs/server_q4.log#L426) (g348) | 10:07:24 | 1,256 | 0.3913 -> 0.4414 | reason-00064 (#64, chunk 905) | 905 | 83.3 | 1,256~1,256 | — | — | 25.8 ✓ | 시각+토큰 정확 (exact-out+prompt) |
| 21 | [L428](../../../logs/server_q4.log#L428) (g350) | 10:07:24 | 1,295 | 0.4194 -> 0.4498 | reason-00063 (#63, chunk 944) | 944 | 82.9 | 1,295~1,295 | — | — | 64.1 ✗ | 시각+토큰 정확 (exact-out+prompt) |
| 22 | [L429](../../../logs/server_q4.log#L429) (g351) | 10:07:25 | 1,325 | 0.4277 -> 0.4620 | reason-00062 (#62, chunk 974) | 974 | 82.3 | 1,325~1,325 | — | — | 48.4 ✓ | 시각+토큰 정확 (exact-out+prompt) |
| 23 | [L432](../../../logs/server_q4.log#L432) (g354) | 10:07:26 | 1,034 | 0.4038 -> 0.4818 | reason-00061 (#61, chunk 1034) | 1,034 | 107.0 | 1,034~1,385 | [L577](../../../logs/server_q4.log#L577) (g499) 1385/36 | 107s | 54.4 ✓ | 시각+토큰 정확 (exact-out) |
| 24 | [L435](../../../logs/server_q4.log#L435) (g357) | 10:07:29 | 1,150 | 0.3865 -> 0.5030 | reason-00060 (#60, chunk 1150) | 1,150 | 107.3 | 1,150~1,502 | [L580](../../../logs/server_q4.log#L580) (g502) 1501/36 | 107s | 98.1 ✗ | 시각+토큰 정확 (exact-out) |
| 25 | [L478](../../../logs/server_q4.log#L478) (g400) | 10:08:00 | 668 | 0.0980 -> 0.5502 | reason-00099 (#99, chunk 317) | 317 | 76.4 | 668~668 | [L582](../../../logs/server_q4.log#L582) (g504) 668/36 | 77s | 66.5 ✗ | 시각+토큰 정확 (exact-out+prompt) |
| 26 | [L479](../../../logs/server_q4.log#L479) (g401) | 10:08:01 | 685 | 0.5342 -> 0.5592 | reason-00098 (#98, chunk 334) | 334 | 81.1 | 685~685 | [L589](../../../logs/server_q4.log#L589) (g511) 685/36 | 81s | 71.8 ✗ | 시각+토큰 정확 (exact-out+prompt) |
| 27 | [L481](../../../logs/server_q4.log#L481) (g403) | 10:08:01 | 793 | 0.5412 -> 0.5839 | reason-00097 (#97, chunk 442) | 442 | 83.5 | 793~793 | [L592](../../../logs/server_q4.log#L592) (g514) 793/36 | 84s | 35.0 ✓ | 시각+토큰 정확 (exact-out+prompt) |
| 28 | [L490](../../../logs/server_q4.log#L490) (g412) | 10:08:10 | 846 | 0.2357 -> 0.6478 | reason-00095 (#95, chunk 846) | 846 | 75.9 | 846~1,197 | [L594](../../../logs/server_q4.log#L594) (g516) 1197/36 | 76s | 80.5 ✗ | 시각+토큰 정확 (exact-out) |
| 29 | [L566](../../../logs/server_q4.log#L566) (g488) | 10:09:03 | 1,029 | 0.0980 -> 0.4895 | reason-00119 (#119, chunk 1029) | 1,029 | 23.5 | 1,029~1,380 | [L596](../../../logs/server_q4.log#L596) (g518) 1380/36 | 23s | 24.7 ✓ | 시각+토큰 정확 (exact-out) |

### q8

- 클라이언트: [results/reasoning_q8__default.json](../../../results/reasoning_q8__default.json) (요청 120건 · 성공 120건, JSON 한 줄 파일 — 레코드 번호는 0부터)
- 서버: [logs/server_q8.log](../../../logs/server_q8.log) — --sweep-0907 (09-07 스윕 로그 이름 server_q<Q>.log)
- 서버 설정: KV 풀 87,552 토큰 [L139](../../../logs/server_q8.log#L139)(g61) · `schedule_policy=fcfs` · `chunked_prefill_size=2048` · `schedule_conservativeness=1.0` · server_args [L7](../../../logs/server_q8.log#L7)(g7)
- 시계: 클라이언트 t0 ≈ 서버 10:12:10 (토큰이 정확히 맞는 후보 28쌍 중 1s 안에 모인 27쌍의 시각 차 중앙값 + 로그 시각 절삭 보정 0.5s — 우연히 토큰만 맞은 1쌍 제외; 첫 토큰↔Prefill 줄 일치율 100%)

| # | 서버 선점 줄 | 시각 | 회수 KV | new_token_ratio | 피해 요청 (레코드#, chunk#) | 정지 전 생성 | 정지(s) | 재계산 하한~상한 | 재계산 줄 (new/cached) | 선점→재계산 | TPOT(ms) SLO | 판정 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | [L386](../../../logs/server_q8.log#L386) (g308) | 10:12:30 | 590 | 0.0980 -> 0.2708 | reason-00083 (#83, chunk 239) | 239 | 71.1 | 590~590 | — | — | 58.5 ✓ | 시각+토큰 정확 (exact-out+prompt) |
| 2 | [L387](../../../logs/server_q8.log#L387) (g309) | 10:12:31 | 862 | 0.2648 -> 0.2743 | reason-00082 (#82, chunk 511) | 511 | 70.9 | 862~862 | — | — | 51.0 ✓ | 시각+토큰 정확 (exact-out+prompt) |
| 3 | [L389](../../../logs/server_q8.log#L389) (g311) | 10:12:31 | 875 | 0.2643 -> 0.2784 | reason-00081 (#81, chunk 524) | 524 | 72.5 | 875~876 | — | — | 62.2 ✗ | 시각+토큰 근사 (approx) |
| 4 | [L390](../../../logs/server_q8.log#L390) (g312) | 10:12:32 | 904 | 0.2513 -> 0.2848 | reason-00080 (#80, chunk 552) | 552 | 71.8 | 904~904 | — | — | 68.0 ✗ | 시각+토큰 정확 (exact-out+prompt) |
| 5 | [L392](../../../logs/server_q8.log#L392) (g314) | 10:12:32 | 914 | 0.2748 -> 0.2904 | reason-00079 (#79, chunk 563) | 563 | 71.6 | 914~914 | — | — | 47.5 ✓ | 시각+토큰 정확 (exact-out+prompt) |
| 6 | [L393](../../../logs/server_q8.log#L393) (g315) | 10:12:32 | 932 | 0.2794 -> 0.2956 | reason-00078 (#78, chunk 581) | 581 | 71.2 | 932~932 | — | — | 52.2 ✓ | 시각+토큰 정확 (exact-out+prompt) |
| 7 | [L394](../../../logs/server_q8.log#L394) (g316) | 10:12:33 | 959 | 0.2705 -> 0.3060 | reason-00077 (#77, chunk 608) | 608 | 70.7 | 959~959 | — | — | 40.8 ✓ | 시각+토큰 정확 (exact-out+prompt) |
| 8 | [L396](../../../logs/server_q8.log#L396) (g318) | 10:12:33 | 991 | 0.2939 -> 0.3108 | reason-00076 (#76, chunk 640) | 640 | 70.4 | 991~991 | — | — | 56.7 ✓ | 시각+토큰 정확 (exact-out+prompt) |
| 9 | [L397](../../../logs/server_q8.log#L397) (g319) | 10:12:34 | 1,006 | 0.2988 -> 0.3143 | reason-00075 (#75, chunk 655) | 655 | 72.9 | 1,006~1,006 | — | — | 78.3 ✗ | 시각+토큰 정확 (exact-out+prompt) |
| 10 | [L398](../../../logs/server_q8.log#L398) (g320) | 10:12:34 | 1,039 | 0.3012 -> 0.3185 | reason-00074 (#74, chunk 688) | 688 | 72.5 | 1,039~1,039 | — | — | 66.6 ✗ | 시각+토큰 정확 (exact-out+prompt) |
| 11 | [L400](../../../logs/server_q8.log#L400) (g322) | 10:12:34 | 1,054 | 0.3045 -> 0.3216 | reason-00073 (#73, chunk 703) | 703 | 76.4 | 1,054~1,054 | [L518](../../../logs/server_q8.log#L518) (g440) 1054/36 | 77s | 119.5 ✗ | 시각+토큰 정확 (exact-out+prompt) |
| 12 | [L402](../../../logs/server_q8.log#L402) (g324) | 10:12:35 | 1,086 | 0.2915 -> 0.3301 | reason-00072 (#72, chunk 735) | 735 | 79.4 | 1,086~1,086 | [L522](../../../logs/server_q8.log#L522) (g444) 1086/36 | 80s | 57.3 ✓ | 시각+토큰 정확 (exact-out+prompt) |
| 13 | [L403](../../../logs/server_q8.log#L403) (g325) | 10:12:36 | 1,109 | 0.3150 -> 0.3353 | reason-00071 (#71, chunk 758) | 758 | 82.0 | 1,109~1,109 | [L527](../../../logs/server_q8.log#L527) (g449) 1109/36 | 82s | 64.1 ✗ | 시각+토큰 정확 (exact-out+prompt) |
| 14 | [L405](../../../logs/server_q8.log#L405) (g327) | 10:12:36 | 1,157 | 0.3011 -> 0.3467 | reason-00070 (#70, chunk 805) | 805 | 87.3 | 1,157~1,157 | [L534](../../../logs/server_q8.log#L534) (g456) 1156/36 | 88s | 47.9 ✓ | 시각+토큰 정확 (exact-out+prompt) |
| 15 | [L407](../../../logs/server_q8.log#L407) (g329) | 10:12:37 | 1,200 | 0.3075 -> 0.3608 | reason-00069 (#69, chunk 849) | 849 | 91.6 | 1,200~1,200 | [L540](../../../logs/server_q8.log#L540) (g462) 1201/35 | 92s | 43.3 ✓ | 시각+토큰 정확 (exact-out+prompt) |
| 16 | [L408](../../../logs/server_q8.log#L408) (g330) | 10:12:38 | 1,234 | 0.3427 -> 0.3815 | reason-00066 (#66, chunk 883) | 883 | 93.3 | 1,234~1,234 | [L543](../../../logs/server_q8.log#L543) (g465) 1234/36 | 93s | 28.7 ✓ | 시각+토큰 정확 (exact-out+prompt) |
| 17 | [L410](../../../logs/server_q8.log#L410) (g332) | 10:12:38 | 1,255 | 0.3625 -> 0.3893 | reason-00065 (#65, chunk 904) | 904 | 93.8 | 1,255~1,255 | [L545](../../../logs/server_q8.log#L545) (g467) 1255/36 | 94s | 60.4 ✗ | 시각+토큰 정확 (exact-out+prompt) |
| 18 | [L411](../../../logs/server_q8.log#L411) (g333) | 10:12:39 | 1,293 | 0.3702 -> 0.4167 | reason-00064 (#64, chunk 942) | 942 | 109.4 | 1,293~1,293 | [L562](../../../logs/server_q8.log#L562) (g484) 1293/36 | 109s | 26.9 ✓ | 시각+토큰 정확 (exact-out+prompt) |
| 19 | [L413](../../../logs/server_q8.log#L413) (g335) | 10:12:40 | 1,323 | 0.3956 -> 0.4244 | reason-00063 (#63, chunk 972) | 972 | 111.1 | 1,323~1,323 | [L566](../../../logs/server_q8.log#L566) (g488) 1323/36 | 111s | 76.6 ✗ | 시각+토큰 정확 (exact-out+prompt) |
| 20 | [L415](../../../logs/server_q8.log#L415) (g337) | 10:12:41 | 1,375 | 0.3752 -> 0.4413 | reason-00062 (#62, chunk 1024) | 1,024 | 115.9 | 1,375~1,375 | [L573](../../../logs/server_q8.log#L573) (g495) 1375/36 | 116s | 56.7 ✓ | 시각+토큰 정확 (exact-out+prompt) |
| 21 | [L417](../../../logs/server_q8.log#L417) (g339) | 10:12:41 | 1,400 | 0.4182 -> 0.4533 | reason-00061 (#61, chunk 1049) | 1,049 | 115.6 | 1,400~1,400 | [L574](../../../logs/server_q8.log#L574) (g496) 1400/36 | 116s | 57.0 ✓ | 시각+토큰 정확 (exact-out+prompt) |
| 22 | [L418](../../../logs/server_q8.log#L418) (g340) | 10:12:42 | 1,085 | 0.4292 -> 0.4598 | reason-00060 (#60, chunk 1085) | 1,085 | 116.7 | 1,085~1,437 | [L577](../../../logs/server_q8.log#L577) (g499) 1436/36 | 117s | 104.2 ✗ | 시각+토큰 정확 (exact-out) |
| 23 | [L421](../../../logs/server_q8.log#L421) (g343) | 10:12:43 | 1,140 | 0.4066 -> 0.4814 | reason-00059 (#59, chunk 1140) | 1,140 | 116.3 | 1,140~1,491 | [L579](../../../logs/server_q8.log#L579) (g501) 1492/35 | 117s | 49.5 ✓ | 시각+토큰 정확 (exact-out) |
| 24 | [L426](../../../logs/server_q8.log#L426) (g348) | 10:12:48 | 1,319 | 0.3038 -> 0.5202 | reason-00058 (#58, chunk 1319) | 1,319 | 113.9 | 1,319~1,670 | — | — | 89.2 ✗ | 시각+토큰 정확 (exact-out) |
| 25 | [L430](../../../logs/server_q8.log#L430) (g352) | 10:12:51 | 1,435 | 0.4048 -> 0.5455 | reason-00057 (#57, chunk 1435) | 1,435 | 111.1 | 1,435~1,786 | — | — | 94.5 ✗ | 시각+토큰 정확 (exact-out) |
| 26 | [L432](../../../logs/server_q8.log#L432) (g354) | 10:12:52 | 1,475 | 0.5064 -> 0.5602 | reason-00056 (#56, chunk 1475) | 1,475 | 115.9 | 1,475~1,826 | [L590](../../../logs/server_q8.log#L590) (g512) 1826/36 | 116s | 64.1 ✗ | 시각+토큰 정확 (exact-out) |
| 27 | [L474](../../../logs/server_q8.log#L474) (g396) | 10:13:23 | 750 | 0.0980 -> 0.5639 | reason-00099 (#99, chunk 399) | 399 | 85.4 | 750~750 | [L591](../../../logs/server_q8.log#L591) (g513) 750/36 | 85s | 71.6 ✗ | 시각+토큰 정확 (exact-out+prompt) |
| 28 | [L476](../../../logs/server_q8.log#L476) (g398) | 10:13:23 | 770 | 0.5449 -> 0.5734 | reason-00098 (#98, chunk 419) | 419 | 86.7 | 770~770 | [L594](../../../logs/server_q8.log#L594) (g516) 770/36 | 87s | 75.6 ✗ | 시각+토큰 정확 (exact-out+prompt) |

## 4. 계산의 근거가 된 소스 위치

측정에 쓴 버전에 고정한 줄 번호다: SGLang v0.5.18(GitHub 태그) · 과정 하니스 커밋 802a164(링크만, 이 저장소에는 없음) · 이 저장소의 지표 정의.

| 근거 | 위치 |
|---|---|
| 선점 실행 | [scheduler.py:3504](https://github.com/sgl-project/sglang/blob/v0.5.18/python/sglang/srt/managers/scheduler.py#L3504) |
| #new_tokens_gained = 선점 전후 빈 KV 슬롯 수 차이 | [scheduler.py:3508](https://github.com/sgl-project/sglang/blob/v0.5.18/python/sglang/srt/managers/scheduler.py#L3508) |
| 선점 로그 문구 | [scheduler.py:3538](https://github.com/sgl-project/sglang/blob/v0.5.18/python/sglang/srt/managers/scheduler.py#L3538) |
| 선점된 요청을 대기열에 다시 넣음 | [scheduler.py:3552](https://github.com/sgl-project/sglang/blob/v0.5.18/python/sglang/srt/managers/scheduler.py#L3552) |
| 대기열 맨 뒤에 추가 | [scheduler.py:2725](https://github.com/sgl-project/sglang/blob/v0.5.18/python/sglang/srt/managers/scheduler.py#L2725) |
| fcfs 는 대기열을 재정렬하지 않음 | [schedule_policy.py:254](https://github.com/sgl-project/sglang/blob/v0.5.18/python/sglang/srt/managers/schedule_policy.py#L254) |
| 선점 요청 KV 를 radix 캐시에 넣지 않고 해제 | [schedule_batch.py:1934](https://github.com/sgl-project/sglang/blob/v0.5.18/python/sglang/srt/managers/schedule_batch.py#L1934) |
| 선점 뒤에도 output_ids 유지 — input_embeds 요청만 비운다(주석) | [schedule_batch.py:1709](https://github.com/sgl-project/sglang/blob/v0.5.18/python/sglang/srt/managers/schedule_batch.py#L1709) |
| prefill 입력 = origin_input_ids + output_ids (불변식) | [schedule_batch.py:1272](https://github.com/sgl-project/sglang/blob/v0.5.18/python/sglang/srt/managers/schedule_batch.py#L1272) |
| 재개 시 새 출력만 이어 붙이는 경로 | [schedule_batch.py:1288](https://github.com/sgl-project/sglang/blob/v0.5.18/python/sglang/srt/managers/schedule_batch.py#L1288) |
| 선점 순서 (생성 토큰이 적은 요청부터) | [schedule_batch.py:2867](https://github.com/sgl-project/sglang/blob/v0.5.18/python/sglang/srt/managers/schedule_batch.py#L2867) |
| SGLang 자체 재계산 집계: 선점됐던 요청의 재prefill 토큰 | [schedule_policy.py:904](https://github.com/sgl-project/sglang/blob/v0.5.18/python/sglang/srt/managers/schedule_policy.py#L904) |
| 재계산 = realtime_tokens_total{mode=prefill_compute} − prefill_effective_tokens_total{mode=input} | [metrics_collector.py:893](https://github.com/sgl-project/sglang/blob/v0.5.18/python/sglang/srt/observability/metrics_collector.py#L893) |
| num_retracted_input_tokens_total = 선점 시점 요청의 입력 길이 합 (재계산량 아님) | [metrics_collector.py:467](https://github.com/sgl-project/sglang/blob/v0.5.18/python/sglang/srt/observability/metrics_collector.py#L467) |
| 응답 meta_info 의 요청별 선점 횟수 | [tokenizer_manager.py:2246](https://github.com/sgl-project/sglang/blob/v0.5.18/python/sglang/srt/managers/tokenizer_manager.py#L2246) |
| replay 는 rid 를 서버에 보내지 않음 | [replay.py:79](https://github.com/mlleo/inference-engine-study/blob/802a1642fcefc06735a89af054213c9904b4ff45/project/bench/replay.py#L79) |
| chunk_times 를 t0 기준 상대 시각으로 저장 | [replay.py:58](https://github.com/mlleo/inference-engine-study/blob/802a1642fcefc06735a89af054213c9904b4ff45/project/bench/replay.py#L58) |
| t0 = perf_counter (벽시계 아님) | [replay.py:135](https://github.com/mlleo/inference-engine-study/blob/802a1642fcefc06735a89af054213c9904b4ff45/project/bench/replay.py#L135) |
| W3 SLO: TPOT ≤ 60 ms | [generators.py:161](https://github.com/mlleo/inference-engine-study/blob/802a1642fcefc06735a89af054213c9904b4ff45/project/workloads/generators.py#L161) |
| TPOT 정의 (첫 토큰 이후 평균): request_metrics | [w3/metrics.py](../../../w3/metrics.py) |

## 5. 한계

- 서버 로그 시각은 1초 단위(절삭)다. 오프셋에 0.5초 절삭 보정을 더하고 ±허용 오차 안에서 짝지은 뒤 토큰 수로 확인한다. `SGLANG_LOG_MS=1` 로 서버를 띄우면 밀리초 시각을 그대로 쓴다.
- 서버 로그에는 요청 ID 가 없고 replay 는 rid 를 서버에 보내지 않는다. 짝짓기는 시각과 토큰 수로 한다. 응답 `meta_info.num_retractions` 를 결과 파일에 저장하면(현재 replay.py 는 버림) 요청별 선점 횟수를 직접 쓸 수 있다.
- 재개 prefill 이 다른 요청과 한 배치로 섞이거나 chunked prefill 로 쪼개지면 그 요청의 재계산 줄을 따로 떼어낼 수 없다('—'). 이때는 실행 전체의 'prefill 초과'가 가장 정확한 재계산량이다.
- 'prefill 초과'는 기준선과 트레이스·요청·입력 길이가 같을 때만 계산한다. 헬스체크·워밍업 캐시 적중 차이로 수 토큰~수백 토큰 오차가 날 수 있다.
- 5초 넘는 정지가 모두 선점 때문은 아니다. 선점과 짝이 없는 정지는 '선점과 짝 없음'으로 따로 적었다.
