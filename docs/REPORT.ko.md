[English](REPORT.md) · 한국어

# W3 장문 추론 워크로드의 KV 선점 분석과 peak-KV admission 게이트 (SGLang 0.5.18)

> **정정판 안내.** 이 문서는 2026-09-17에 제출한 W3 최종 보고서를 정정하고 보강한 판이다. 제출본(11쪽 PDF)은 공개하지 않으며, 제출본과 달라진 수치·해석과 그 근거는 [ERRATA.md](../ERRATA.md)에 정리했다. 제출본에서 빠졌던 풀 축소 인과 대조, 기존 플래그(`--disable-radix-cache`, `--max-running-requests`) 비교, 반복별 범위, 정확성 결과의 해석, 부록 A–D도 되살렸다. 영어판은 [REPORT.md](REPORT.md)다.

| 항목 | 내용 |
|---|---|
| 작성자 | HwaminJung00 |
| 워크로드 | W3 장문 추론(reasoning, Long CoT) |
| 제출 / 정정 | 2026-09-17 제출 / 2026-10-02 정정판 |
| 엔진 | SGLang v0.5.18 (`71de97b`) + [`patch/peak_kv_reservation.diff`](../patch/peak_kv_reservation.diff) (+<!--n:patch.lines_added-->102<!--/n-->/−<!--n:patch.lines_removed-->0<!--/n-->줄, 플래그 기본 OFF) |
| 모델 · GPU | Qwen/Qwen3-4B (bf16) · RTX 4090 24 GB · KV 풀 <!--n:kv.pool_tokens-->87,552<!--/n-->토큰 |

**출처와 도구.** 이 작업은 5주 LLM 추론 엔진 스터디(2026년 8–9월)의 개인 과제로 시작했다. 워크로드 정의, trace 생성기, 재생·지표 하니스, 그리고 peak KV 예약을 탐색해 보라는 힌트는 과정 저장소 [mlleo/inference-engine-study](https://github.com/mlleo/inference-engine-study)가 제공했고, 이 저장소에는 그 파일을 넣지 않았다. 강사용 풀이 자료는 참고하지 않았다(사전 계획에 기록). 병목 분석, 패치, 실험 자동화, 검증, 보고서는 본인 작업이다. 정정판의 재계산·검증·문서화에는 AI 코딩 도구를 보조로 썼다([README의 Tools used](../README.md#tools-used)).

**읽는 법.**

- **부하.** `q2`는 같은 요청 120개를 2 QPS로 보내는 trace(`reasoning_q2`)이고, `qps 0.35`는 기본 trace다. QPS만 다르고 프롬프트와 출력 길이는 모두 같다.
- **bar 이름.**
  - `upstream`: 순정 SGLang 0.5.18(09-13).
  - `default`: 패치 빌드에서 플래그를 끈 것(09-13). 순정과 같다는 근거는 3.3에 있다.
  - `ablated`: default + `--chunked-prefill-size -1`.
  - `mine`: 패치 플래그 ON(`--enable-peak-kv-reservation`). 따로 적지 않으면 α = 1.0이다.
  - `tuned N`: `--max-running-requests N`.
  - `noradix`: `--disable-radix-cache`.
  - `cap65k`: `--max-total-tokens 65536`.
  - `09-07 스윕`: 패치 전 순정 0.5.18의 부하 스윕(부하당 1회).
- **반복 수.** ★는 q2에서 3회 반복한 중앙값이고 대괄호는 [최소–최대]다. ★가 없는 결과는 모두 1회 측정(n=1)이다.
- **지표.**
  - 정지: 한 요청의 스트림 안에서 5 s 넘게 토큰이 오지 않은 구간. maxITL은 요청 안의 최대 토큰 간격이다.
  - 선점 수: 서버 로그 `#retracted_reqs`의 합.
  - goodput: SLO 충족 요청 수 ÷ wall. wall은 첫 제출부터 마지막 완료까지의 시간이다.
- **숫자의 출처.** 본문의 주요 수치는 [`artifacts/numbers.json`](../artifacts/numbers.json)에서 왔다. 마크다운 원문에서는 `<!--n:키-->` 주석으로 감싸 두었고, `python3 tools/check_numbers.py`가 이 둘이 같은지 대조한다. 주석이 없는 수치는 표 캡션이나 본문에 적은 파생 데이터에서 계산했다.

## 요약

**표 1. 한눈에 보기**

| 항목 | 내용 |
|---|---|
| 이 워크로드가 어려운 이유 | 입력은 <!--n:trace.prompt_tokens_range-->386–387<!--/n-->토큰인데 출력은 중앙값 <!--n:trace.out_p50-->2,338<!--/n-->·최대 <!--n:trace.out_max-->10,541<!--/n-->토큰(lognormal)이라, 요청 하나의 KV가 수 분 동안 커진다. <!--n:kv.pool_tokens-->87,552<!--/n-->토큰(<!--n:kv.pool_gib-->12.0<!--/n--> GiB) KV 풀로 선점 없이 받을 수 있는 도착률은 Little 법칙으로 약 <!--n:diag.little_lambda_max-->0.56–0.67<!--/n--> QPS로 추정된다. |
| 규명한 병목 | radix cache를 켠 기본 경로의 낙관적 admission이다. 09-07 순정 스윕(부하당 n=1)에서 q<!--n:diag.first_retraction_qps-->0.6<!--/n-->부터 KV 풀이 차서(token usage 최대 <!--n:sweep0907.max_usage.q0.6-->0.99<!--/n-->, q0.8부터 <!--n:sweep0907.max_usage.q0.8-->1.00<!--/n-->) 선점이 생긴다. 이 스윕에서 선점과 5 s 넘는 생성 중 정지(<!--n:diag.stalls_gt5s-->89<!--/n-->건)는 하나씩 짝지어진다(<!--n:diag.stall_match-->89/89<!--/n-->). 정지(<!--n:diag.stall_range_s-->9.1–116.7<!--/n--> s)의 본체는 재계산(처리 토큰의 <!--n:diag.recompute_pct_range-->0.22–8.41<!--/n-->%)이 아니라 대기열 맨 뒤에서의 대기다. |
| 구현한 것 | SGLang 0.5.18에 이미 있는 정확 길이 admission 시뮬레이션(`add_one_req_ignore_eos`, radix cache를 끈 경우에만 실행)을 radix cache를 켠 경로로 옮긴 게이트다. 플래그 `--enable-peak-kv-reservation`(기본 OFF) 뒤에 두었고 +<!--n:patch.lines_added-->102<!--/n-->/−<!--n:patch.lines_removed-->0<!--/n-->줄이다. 합성 trace는 `ignore_eos=True`라 `max_new_tokens`가 곧 실제 출력 길이이므로, 이 게이트는 **알려진 출력 길이를 쓰는 오라클 예약**이다. |
| 핵심 결과 (q2, 3회 중앙값) | goodput <!--n:q2.default.goodput-->0.403<!--/n--> → <!--n:q2.mine.goodput-->0.440<!--/n--> req/s(<!--n:q2.gain.goodput_pct-->+9.1<!--/n-->%), SLO 충족 <!--n:q2.default.slo_ratio-->115/120<!--/n--> → <!--n:q2.mine.slo_ratio-->120/120<!--/n-->, 선점 <!--n:q2.default.retractions-->19<!--/n--> → <!--n:q2.mine.retractions-->0<!--/n-->, maxITL p99 <!--n:q2.default.maxitl_p99_s-->63.1<!--/n--> → <!--n:q2.mine.maxitl_p99_s-->0.11<!--/n--> s. <!--n:q2.gain.goodput_pct-->+9.1<!--/n-->%는 SLO 충족 수 ×<!--n:q2.gain.slo_factor-->1.043<!--/n-->과 wall <!--n:q2.default.wall_s-->285.2<!--/n--> → <!--n:q2.mine.wall_s-->272.8<!--/n--> s(×<!--n:q2.gain.wall_factor-->1.045<!--/n-->)의 곱이다. wall 몫은 가장 긴 요청 <!--n:q2.longest.rid-->reason-00064<!--/n-->의 약 <!--n:q2.longest.default_stall_s-->61.7<!--/n--> s 정지가 사라진 효과라, 그 한 건을 빼면 <!--n:q2.excl_longest.gain_pct-->+4.4<!--/n-->%다. 기존 플래그만으로 `--disable-radix-cache`가 이 이득의 <!--n:q2.share_of_gain.noradix_pct-->88.0<!--/n-->%, `--max-running-requests 40`이 <!--n:q2.share_of_gain.tuned_n40_pct-->78.1<!--/n-->%를 낸다(각 n=1). |
| 대가 | 정지가 생성 도중에서 첫 토큰 전으로 옮겨갔다. TTFT p99 <!--n:q2.default.ttft_p99_s-->64.2<!--/n--> → <!--n:q2.mine.ttft_p99_s-->92.3<!--/n--> s(<!--n:q2.gain.ttft_p99_pct-->+43.6<!--/n-->%), 평균 <!--n:q2.default.ttft_mean_s-->19.0<!--/n--> → <!--n:q2.mine.ttft_mean_s-->30.1<!--/n--> s(<!--n:q2.gain.ttft_mean_pct-->+58.4<!--/n-->%), 10 s 넘게 기다린 요청 <!--n:q2.default.wait10-->43<!--/n--> → <!--n:q2.mine.wait10-->60<!--/n-->건. 출력 1k 미만인 짧은 요청 <!--n:q2.short_ttft_p50.n_requests-->9<!--/n-->건의 TTFT p50은 <!--n:q2.short_ttft_p50.default_s-->0.06<!--/n--> → <!--n:q2.short_ttft_p50.mine_s-->31.6<!--/n--> s다. 선점이 없는 조건(qps 0.35·q0.5·out_mean 1000)과 교차 워크로드에서는 효과가 없었고(goodput ±0.8% 이내, 각 n=1), q1에서는 goodput 이득 없이 TTFT p99만 <!--n:load.q1.ttft_p99_pct-->+15.9<!--/n-->% 늘었다(n=1). |

**표 2. 핵심 수치 (q2, 3회 중앙값 [최소–최대])**

| 지표 | default★ | mine (α=1.0)★ | 변화 |
|---|---|---|---|
| goodput (req/s, 평균 TPOT ≤ 60 ms) | <!--n:q2.default.goodput-->0.403<!--/n--> [<!--n:q2.default.goodput_range-->0.4025–0.4035<!--/n-->] | <!--n:q2.mine.goodput-->0.440<!--/n--> [<!--n:q2.mine.goodput_range-->0.4396–0.4399<!--/n-->] | <!--n:q2.gain.goodput_pct-->+9.1<!--/n-->% |
| SLO 충족 | <!--n:q2.default.slo_ratio-->115/120<!--/n--> | <!--n:q2.mine.slo_ratio-->120/120<!--/n--> | ×<!--n:q2.gain.slo_factor-->1.043<!--/n--> |
| wall (s) | <!--n:q2.default.wall_s-->285.2<!--/n--> | <!--n:q2.mine.wall_s-->272.8<!--/n--> | ×<!--n:q2.gain.wall_factor-->1.045<!--/n--> (default ÷ mine) |
| goodput, <!--n:q2.longest.rid-->reason-00064<!--/n--> 제외 | <!--n:q2.excl_longest.default_goodput-->0.420<!--/n--> | <!--n:q2.excl_longest.mine_goodput-->0.439<!--/n--> | <!--n:q2.excl_longest.gain_pct-->+4.4<!--/n-->% |
| 선점 (#retracted_reqs 합) | <!--n:q2.default.retractions-->19<!--/n--> [<!--n:q2.default.retractions_min-->19<!--/n-->–<!--n:q2.default.retractions_max-->20<!--/n-->] | <!--n:q2.mine.retractions-->0<!--/n--> | |
| 5 s 넘게 멈춘 요청 | <!--n:q2.default.stall5-->19<!--/n--> [<!--n:q2.default.stall5_range-->19–20<!--/n-->] | <!--n:q2.mine.stall5-->0<!--/n--> | |
| maxITL p99 (s) | <!--n:q2.default.maxitl_p99_s-->63.1<!--/n--> [<!--n:q2.default.maxitl_p99_range_s-->62.8–63.1<!--/n-->] | <!--n:q2.mine.maxitl_p99_s-->0.11<!--/n--> [<!--n:q2.mine.maxitl_p99_range_s-->0.11–0.11<!--/n-->] | |
| TTFT p99 (s) | <!--n:q2.default.ttft_p99_s-->64.2<!--/n--> [<!--n:q2.default.ttft_p99_range_s-->64.1–64.3<!--/n-->] | <!--n:q2.mine.ttft_p99_s-->92.3<!--/n--> [<!--n:q2.mine.ttft_p99_range_s-->92.2–92.4<!--/n-->] | <!--n:q2.gain.ttft_p99_pct-->+43.6<!--/n-->% |
| TTFT 평균 (s) | <!--n:q2.default.ttft_mean_s-->19.0<!--/n--> | <!--n:q2.mine.ttft_mean_s-->30.1<!--/n--> | <!--n:q2.gain.ttft_mean_pct-->+58.4<!--/n-->% |
| 10 s 넘게 기다린 요청 | <!--n:q2.default.wait10-->43<!--/n--> | <!--n:q2.mine.wait10-->60<!--/n--> | |
| out_tok/s (= <!--n:trace.out_total-->337,433<!--/n-->토큰 ÷ wall) | <!--n:q2.default.out_tok_s-->1,183.3<!--/n--> | <!--n:q2.mine.out_tok_s-->1,236.7<!--/n--> | <!--n:q2.gain.out_tok_s_pct-->+4.5<!--/n-->% (wall 효과, 처리 속도와 무관) |

![q2 요청별 타임라인: default r2와 mine r2](figures/hero_timeline.png)

*그림 1. q2에서 default(r2)와 mine(r2)의 요청별 타임라인. 요청 120개를 도착 순으로 한 줄씩 그렸다. default는 <!--n:hero.default.stalled-->19<!--/n-->건이 생성 도중 <!--n:hero.default.stall_range_s-->32.5–64.8<!--/n--> s 멈춘다. mine은 생성 중 정지가 <!--n:hero.mine.stalled-->0<!--/n-->건인 대신 10 s 넘게 첫 토큰을 기다린 요청이 <!--n:hero.default.wait10-->43<!--/n-->건에서 <!--n:hero.mine.wait10-->60<!--/n-->건으로 늘었다.*

## 1. 워크로드 분석

### 1.1 시나리오와 트래픽 특성

추론 모델(Long CoT) 서빙을 흉내 낸 워크로드다. 사용자가 짧은 문제(<!--n:trace.prompt_tokens_range-->386–387<!--/n-->토큰)를 보내면 모델이 긴 사고 과정을 스트리밍으로 생성한다. 요청 <!--n:trace.n_requests-->120<!--/n-->개는 서로 독립이고 포아송 과정으로 도착한다(기본 0.35 QPS). 모두 greedy(temperature 0)이고 `ignore_eos=True`라 출력 길이가 `max_new_tokens`와 같다. 출력 길이는 lognormal(생성기 기본값: 평균 3,000, σ 0.6, [256, 16,000] 구간)이라 요청 하나의 KV가 수 분 동안 계속 커진다.

**표 3. W3 트래픽 특성**

| 항목 | 값 | 근거 |
|---|---|---|
| 입력 토큰 | <!--n:trace.prompt_tokens_range-->386–387<!--/n--> (사실상 고정) | 결과 파일의 `prompt_tokens` |
| 출력 토큰 p50 / p90 / p99 / 최대 | <!--n:trace.out_p50-->2,338<!--/n--> / <!--n:trace.out_p90-->4,744.7<!--/n--> / <!--n:trace.out_p99-->8,927.2<!--/n--> / <!--n:trace.out_max-->10,541<!--/n--> (평균 <!--n:trace.out_mean-->2,811.9<!--/n-->, 합 <!--n:trace.out_total-->337,433<!--/n-->) | `ignore_eos=True`라 `max_new_tokens` = 실제 출력 길이 |
| 입력 : 출력 | 중앙값 1 : 6.0, 토큰 합 1 : 7.3 | prefill과 decode의 1차 근사 |
| 도착 | 포아송. 기본 0.35 QPS: 명목 간격 1/0.35 = <!--n:trace.nominal_interarrival_s-->2.9<!--/n--> s, 실측 평균 <!--n:trace.mean_interarrival_s-->2.7<!--/n--> s. 최종 비교는 같은 요청을 2 QPS로 보내는 q2(도착 구간 56.0 s) | QPS만 바꾼 trace들은 프롬프트·출력 길이가 같고 도착 시각만 다르다(trace 파일로 확인) |
| 세션 구조 | 독립 요청 <!--n:trace.n_requests-->120<!--/n-->개 (`session_id`가 모두 다르고 `depends_on` 없음) | trace 필드 |
| 공유 prefix | 요청끼리 공유하는 것은 시스템 안내와 문제 머리말의 앞 <!--n:trace.shared_prefix_tokens-->36<!--/n-->토큰뿐이다. cache hit <!--n:trace.cache_hit_pct-->11.5<!--/n-->%에는 재생 워밍업이 앞 3건의 프롬프트를 미리 캐시한 몫이 들어 있고, 앞 3건을 빼면 <!--n:trace.cache_hit_excl_warmup_pct-->9.3<!--/n-->%다 | `cached_tokens` 최빈값 <!--n:trace.shared_prefix_tokens-->36<!--/n-->. 문제 본문은 요청마다 달라 적중률이 낮은 것이 정상 |
| KV 크기 | 토큰당 144 KiB(<!--n:kv.bytes_per_token-->147,456<!--/n--> B). 중앙값 요청(입력+출력 2,725토큰)은 끝날 때 0.37 GiB, 가장 긴 요청(10,928토큰)은 1.50 GiB | Qwen3-4B: 36층 × KV head 8 × head_dim 128 × K·V × bf16 |
| KV 풀과 동시 수용 | 풀 <!--n:kv.pool_tokens-->87,552<!--/n-->토큰 = <!--n:kv.pool_gib-->12.0<!--/n--> GiB ≈ 중앙값 요청 32개분. Little 법칙으로 선점 없이 버티는 도착률 λmax = 풀 ÷ (TPOT × E[387·o + o²/2]) ≈ <!--n:diag.little_lambda_max-->0.56–0.67<!--/n--> QPS (o = 출력 길이, TPOT 20–24 ms 가정) | 출력 총합이 아니라 시간가중 동시 KV로 계산한다. 상위 10% 긴 요청이 KV×시간의 41.2%를 차지한다 |
| qps 0.35 실측 | 최대 동시 <!--n:baseline.q035.upstream.max_running-->26<!--/n-->, token usage 최대 <!--n:baseline.q035.upstream.max_usage-->0.76<!--/n-->(약 6.7만 토큰), 선점 <!--n:baseline.q035.upstream.retractions-->0<!--/n--> | 09-13 순정 3회 |

![출력 토큰 길이 분포](figures/fig01_output_len.png)

*그림 2. 출력 토큰 길이 분포. 요청 <!--n:trace.n_requests-->120<!--/n-->개이고, `max_new_tokens`가 곧 실제 출력 길이다.*

### 1.2 SLO 정의와 근거

**표 4. SLO 정의**

| 지표 | 목표 | 근거 | trace 필드 |
|---|---|---|---|
| TPOT (요청별 평균) | ≤ <!--n:trace.tpot_slo_ms-->60<!--/n--> ms | trace에 고정된 값이다. 60 ms/토큰 ≈ 16.7토큰/s라, 스트리밍되는 추론 과정을 사람이 따라 읽기에 충분하다. 순정 qps 0.35의 TPOT p99는 <!--n:baseline.q035.upstream.tpot_p99_ms-->20.2<!--/n--> ms로 약 3배 여유가 있다. SLO는 바꾸지 않았다. | `tpot_slo_ms` |
| TTFT | 없음 | trace 고정값이다(`ttft_slo_ms = null`). 긴 생성이 본체라 첫 토큰보다 흐름이 끊기지 않는 것이 중요하다는 설정이다. 그러나 선점을 없애면 대가가 TTFT로 옮겨가므로 TTFT p50·평균·p99를 모든 결과 표에 따로 싣는다. | `ttft_slo_ms` |
| (보조) 요청 안 최대 정지 ≤ 5 s | 보고용 | 결과를 보기 전에 사전 계획 4.2에서 정한 보조 지표다(`aux_slo_%`, 5 s 넘게 멈춘 요청 수 `stall5_reqs`). goodput 판정에는 넣지 않는다. maxITL p99, E2E p50/p99, wall도 보고용이다. | 클라이언트 chunk 도착 시각 |

평균 TPOT SLO 하나로는 긴 정지를 잡지 못한다. 60 s를 멈춰도 출력이 길면 평균 TPOT는 60 ms 아래에 남기 때문이다. 실제로 q2 default에서 5 s 넘게 멈춘 요청 <!--n:q2.default.stall5_range-->19–20<!--/n-->건 가운데 <!--n:q2.default.stalled_tpot_ok_range-->14–15<!--/n-->건은 평균 TPOT SLO를 통과했다(4.2).

### 1.3 무엇이 어려운가

입력은 <!--n:trace.prompt_tokens_range-->386–387<!--/n-->토큰으로 고정인데 출력은 중앙값 <!--n:trace.out_p50-->2,338<!--/n-->·최대 <!--n:trace.out_max-->10,541<!--/n-->토큰이라, E2E의 99.9%가 decode다(qps 0.35에서 TTFT가 E2E에서 차지하는 비중은 p50 0.1%). 요청 하나의 KV는 decode step마다 144 KiB씩 커져, 중앙값 요청은 끝날 때 0.37 GiB, 가장 긴 요청은 1.50 GiB가 된다. <!--n:kv.pool_tokens-->87,552<!--/n-->토큰(<!--n:kv.pool_gib-->12.0<!--/n--> GiB) 풀에서 선점 없이 받을 수 있는 도착률은 Little 법칙으로 약 <!--n:diag.little_lambda_max-->0.56–0.67<!--/n--> QPS이고, 실제로 q<!--n:diag.first_retraction_qps-->0.6<!--/n-->에서 첫 선점이 나온다.

radix cache를 켠 기본 경로의 스케줄러는 새 요청이 얼마나 길게 생성할지 쓰지 않는다. 실행 중 요청마다 min(남은 길이, 4,096) × `new_token_ratio`만큼만 예약하고, 이 비율은 0.7에서 시작해 0.098까지 줄어든다. 그래서 부하가 λmax를 넘으면 긴 요청이 너무 많이 동시에 들어와 풀이 가득 차고, 생성 토큰이 가장 적은 요청이 선점되어 대기열 맨 뒤로 간다. 이미 만든 출력 토큰은 남지만 KV는 버려져 재prefill된다. 재계산은 처리 토큰의 <!--n:diag.recompute_pct_range-->0.22–8.41<!--/n-->%뿐이지만, 피해 요청은 <!--n:diag.stall_range_s-->9.1–116.7<!--/n--> s 동안 멈춘다. 게다가 상위 10% 긴 요청이 KV×시간의 41.2%를 차지해, 평균 길이로 용량을 계산하면 틀린다.

## 2. 병목 규명

### 2.1 베이스라인 측정

**표 5. 베이스라인 (순정 0.5.18, qps 0.35, 09-13, 3회)**

| 지표 | 단위 | r1 | r2 | r3 | 중앙값 |
|---|---|---|---|---|---|
| n_ok / n_err | 건 | 120 / 0 | 120 / 0 | 120 / 0 | 120 / 0 |
| wall | s | 393.2 | 393.2 | 393.2 | <!--n:baseline.q035.upstream.wall_s-->393.2<!--/n--> |
| TTFT p50 | ms | 51.6 | 50.8 | 49.9 | <!--n:baseline.q035.upstream.ttft_p50_ms-->50.8<!--/n--> |
| TTFT p99 | ms | 69.4 | 66.0 | 70.1 | <!--n:baseline.q035.upstream.ttft_p99_ms-->69.4<!--/n--> |
| TPOT p50 | ms | 16.5 | 16.5 | 16.5 | <!--n:baseline.q035.upstream.tpot_p50_ms-->16.5<!--/n--> |
| TPOT p99 | ms | 20.2 | 20.2 | 20.2 | <!--n:baseline.q035.upstream.tpot_p99_ms-->20.2<!--/n--> |
| maxITL p99 | ms | 59.4 | 60.1 | 59.7 | <!--n:baseline.q035.upstream.maxitl_p99_ms-->59.7<!--/n--> |
| E2E p99 | s | 152.0 | 152.1 | 152.1 | <!--n:baseline.q035.upstream.e2e_p99_s-->152.1<!--/n--> |
| out_tok/s | tok/s | 858.2 | 858.3 | 858.2 | <!--n:baseline.q035.upstream.out_tok_s-->858.2<!--/n--> |
| cache hit | % | 11.5 | 11.5 | 11.5 | <!--n:baseline.q035.upstream.cache_hit_pct-->11.5<!--/n--> |
| SLO 충족 | % | 100.0 | 100.0 | 100.0 | <!--n:baseline.q035.upstream.slo_pct-->100.0<!--/n--> |
| goodput | req/s | 0.305 | 0.305 | 0.305 | <!--n:baseline.q035.upstream.goodput-->0.305<!--/n--> |
| 선점 | 회 | 0 | 0 | 0 | <!--n:baseline.q035.upstream.retractions-->0<!--/n--> |
| token usage 최대 | | 0.76 | 0.76 | 0.76 | <!--n:baseline.q035.upstream.max_usage-->0.76<!--/n--> |
| 최대 동시 요청 | 건 | 26 | 26 | 26 | <!--n:baseline.q035.upstream.max_running-->26<!--/n--> |

반복별 값은 `data/derived/w3_runs.csv`에 있다. 제출본의 베이스라인 표는 09-06 실행의 화면 값을 옮긴 것이었다. 그 원본 결과 파일은 같은 날 재실행으로 덮어써져 어떤 파일로도 재현되지 않으므로, 09-13 순정 3회로 바꿨다.

**표 6. 진단 요약 (과정 하니스의 진단 도구 `bench.analyze`, upstream r1)**

| 항목 | 값 | 해석 |
|---|---|---|
| TTFT가 E2E에서 차지하는 비중 (p50 / p90) | 0.1% / 0.3% | decode 지배. TTFT p50 약 51 ms는 387토큰 prefill 1회분이다 |
| 최대 동시 요청 | <!--n:baseline.q035.upstream.max_running-->26<!--/n--> (약 130 s와 280 s의 두 봉우리) | 선점 <!--n:baseline.q035.upstream.retractions-->0<!--/n-->, token usage 최대 <!--n:baseline.q035.upstream.max_usage-->0.76<!--/n-->. 배치가 커졌다 무너지는 패턴은 없다 |
| 누적 미완료 요청 | 2 → 22 → 13 → 22 → 0으로 오르내린다(최대 22) | 단조 증가하지 않으므로 포화가 아니다. 이 톱니는 도착 변동 때문이고 메모리 압박의 증거가 아니다 |
| 프롬프트 길이 구간별 TTFT | 386–387 한 구간뿐 | 길이 차이가 없어 비교할 수 없다. 이 부하에서는 큐 대기도 없다 |

qps 0.35에서는 병목이 관측되지 않는다(서버 로그 발췌는 부록 D-5). wall <!--n:baseline.q035.upstream.wall_s-->393.2<!--/n--> s는 마지막으로 끝나는 reason-00101(8,263토큰)의 제출 시각 257.2 s에 그 요청의 E2E 135.9 s를 더한 값이다. 병목은 λmax를 넘는 q<!--n:diag.first_retraction_qps-->0.6<!--/n--> 이상에서 나타난다(2.3).

### 2.2 병목 지목과 증거

**지목:** KV 풀(<!--n:kv.pool_tokens-->87,552<!--/n-->토큰) 대비 낙관적인 admission. 도착률이 λmax ≈ 0.6 QPS를 넘으면 실행 중 긴 요청들의 KV 성장이 풀을 채워 선점이 일어난다. 피해 요청은 대기열 맨 뒤로 밀려 수십~백여 초 멈춘다. prefill이나 연산 속도의 문제가 아니다.

**증거의 출처와 시점.** 병목을 지목한 사전 근거는 09-07 순정 부하 스윕(표 7의 ①–③)이다. 패치는 09-13 15:06 UTC에 작성했다. 표 8의 q2 대조 실험(풀 축소 16:31, chunked prefill OFF 16:37 시작)은 그 뒤, 사전 계획 P2 탐색 단계의 E2·E3 실험으로 실행했다. 제출본 2.2는 선점이 없는 qps 0.35에서 청킹·radix cache·CUDA graph를 끈 결과를 근거로 들었다. 그러나 이 부하는 λmax보다 낮아 선점이 0이고 token usage도 최대 <!--n:baseline.q035.upstream.max_usage-->0.76<!--/n-->이라, 병목 판정에 정보가 없다. 그 09-07 실행들은 서버 로그도 남아 있지 않다.

**표 7. 병목 증거**

| 증거 | 내용 | 출처 |
|---|---|---|
| ① 선점 = 긴 정지 (1:1) | 09-07 순정 스윕 7개 부하(q0.5–q8, 각 n=1)의 선점 수 <!--n:sweep0907.retractions.q0.5-->0<!--/n-->/<!--n:sweep0907.retractions.q0.6-->1<!--/n-->/<!--n:sweep0907.retractions.q0.8-->4<!--/n-->/<!--n:sweep0907.retractions.q1-->7<!--/n-->/<!--n:sweep0907.retractions.q2-->20<!--/n-->/<!--n:sweep0907.retractions.q4-->29<!--/n-->/<!--n:sweep0907.retractions.q8-->28<!--/n-->이 5 s 넘게 멈춘 요청 수 <!--n:sweep0907.stall5.q0.5-->0<!--/n-->/<!--n:sweep0907.stall5.q0.6-->1<!--/n-->/<!--n:sweep0907.stall5.q0.8-->4<!--/n-->/<!--n:sweep0907.stall5.q1-->7<!--/n-->/<!--n:sweep0907.stall5.q2-->20<!--/n-->/<!--n:sweep0907.stall5.q4-->29<!--/n-->/<!--n:sweep0907.stall5.q8-->28<!--/n-->과 부하마다 같다. 선점 하나하나가 정지 하나와 시각·토큰 위치로 짝지어진다(정지 <!--n:diag.stalls_gt5s-->89<!--/n-->건, 짝 <!--n:diag.stall_match-->89/89<!--/n-->; 정확 <!--n:diag.stall_match_exact-->87<!--/n-->, 근사 <!--n:diag.stall_match_approx-->2<!--/n-->). 이 스윕의 SLO 위반 요청도 전부 선점 피해 요청이다(<!--n:diag.slo_violators_all_victims-->30/30<!--/n-->). | [`data/derived/retract_cost/`](../data/derived/retract_cost/), `python3 -m analysis.retract_cost --sweep-0907` |
| ② 정지의 본체는 대기 | 재prefill 줄을 찾은 <!--n:diag.reprefill_confirmed-->38<!--/n-->건에서 선점 → 재prefill 지연이 정지 시간과 <!--n:diag.reprefill_delay_vs_stall_max_s-->0.90<!--/n--> s 이내로 같다(로그 시각 해상도 1 s). 정지 시간은 대기열에서 다시 입장하기를 기다린 시간이다. 재계산 초과분은 처리 토큰의 <!--n:diag.recompute_pct_range-->0.22–8.41<!--/n-->%뿐이다. | 같은 폴더의 `events.csv`, `summary.csv` |
| ③ 용량 한계와 첫 선점 지점 일치 | Little 법칙 λmax <!--n:diag.little_lambda_max-->0.56–0.67<!--/n--> QPS와 첫 선점 지점 q<!--n:diag.first_retraction_qps-->0.6<!--/n-->(token usage 최대 <!--n:sweep0907.max_usage.q0.6-->0.99<!--/n-->, 선점 <!--n:sweep0907.retractions.q0.6-->1<!--/n-->회)이 맞는다. q0.5는 usage 최대 <!--n:sweep0907.max_usage.q0.5-->0.82<!--/n-->에 선점 <!--n:sweep0907.retractions.q0.5-->0<!--/n-->이고, q0.8부터 usage가 <!--n:sweep0907.max_usage.q0.8-->1.00<!--/n-->이다. | [`data/derived/sweep_0907.csv`](../data/derived/sweep_0907.csv) |
| ④ q2 대조 실험 (표 8) | 입장을 보수적으로 바꾼 개입은 모두 선점을 없앴다. 풀을 줄이면 정지와 대기 꼬리가 커졌다. chunked prefill OFF는 차이가 없다. | `data/derived/w3_runs.csv` |

**표 8. q2 대조 실험 (09-13, bar마다 서버 재시작. ★ = 3회 중앙값 [최소–최대], 나머지 n=1)**

| 구성 | KV 풀 | 선점 | 5 s 넘는 정지 | maxITL p99 (s) | TTFT p99 (s) | TTFT 평균 (s) | SLO 충족 | wall (s) | goodput |
|---|---|---|---|---|---|---|---|---|---|
| default★ | <!--n:kv.pool_tokens-->87,552<!--/n--> | <!--n:q2.default.retractions-->19<!--/n--> [<!--n:q2.default.retractions_min-->19<!--/n-->–<!--n:q2.default.retractions_max-->20<!--/n-->] | <!--n:q2.default.stall5-->19<!--/n--> [<!--n:q2.default.stall5_range-->19–20<!--/n-->] | <!--n:q2.default.maxitl_p99_s-->63.1<!--/n--> [<!--n:q2.default.maxitl_p99_range_s-->62.8–63.1<!--/n-->] | <!--n:q2.default.ttft_p99_s-->64.2<!--/n--> [<!--n:q2.default.ttft_p99_range_s-->64.1–64.3<!--/n-->] | <!--n:q2.default.ttft_mean_s-->19.0<!--/n--> | <!--n:q2.default.slo_ratio-->115/120<!--/n--> | <!--n:q2.default.wall_s-->285.2<!--/n--> | <!--n:q2.default.goodput-->0.403<!--/n--> [<!--n:q2.default.goodput_range-->0.4025–0.4035<!--/n-->] |
| upstream (순정, n=1) | <!--n:kv.pool_tokens-->87,552<!--/n--> | <!--n:q2.upstream.retractions-->19<!--/n--> | <!--n:q2.upstream.stall5-->19<!--/n--> | <!--n:q2.upstream.maxitl_p99_s-->63.2<!--/n--> | <!--n:q2.upstream.ttft_p99_s-->64.3<!--/n--> | <!--n:q2.upstream.ttft_mean_s-->19.0<!--/n--> | <!--n:q2.upstream.slo_ratio-->115/120<!--/n--> | <!--n:q2.upstream.wall_s-->285.7<!--/n--> | <!--n:q2.upstream.goodput-->0.403<!--/n--> |
| **cap65k** 풀 축소 (n=1) | <!--n:kv.cap65k_pool_tokens-->65,536<!--/n--> | <!--n:q2.cap65k.retractions-->22<!--/n--> | <!--n:q2.cap65k.stall5-->22<!--/n--> | <!--n:q2.cap65k.maxitl_p99_s-->103.8<!--/n--> | <!--n:q2.cap65k.ttft_p99_s-->114.0<!--/n--> | <!--n:q2.cap65k.ttft_mean_s-->35.3<!--/n--> | <!--n:q2.cap65k.slo_ratio-->117/120<!--/n--> | <!--n:q2.cap65k.wall_s-->305.5<!--/n--> | <!--n:q2.cap65k.goodput-->0.383<!--/n--> |
| ablated★ chunked prefill OFF | <!--n:kv.pool_tokens-->87,552<!--/n--> | <!--n:q2.ablated.retractions-->21<!--/n--> [<!--n:q2.ablated.retractions_min-->20<!--/n-->–<!--n:q2.ablated.retractions_max-->21<!--/n-->] | <!--n:q2.ablated.stall5-->21<!--/n--> | <!--n:q2.ablated.maxitl_p99_s-->63.2<!--/n--> [<!--n:q2.ablated.maxitl_p99_range_s-->62.3–63.6<!--/n-->] | <!--n:q2.ablated.ttft_p99_s-->64.5<!--/n--> [<!--n:q2.ablated.ttft_p99_range_s-->63.6–65.0<!--/n-->] | <!--n:q2.ablated.ttft_mean_s-->19.1<!--/n--> | <!--n:q2.ablated.slo_ratio-->116/120<!--/n--> | <!--n:q2.ablated.wall_s-->285.3<!--/n--> | <!--n:q2.ablated.goodput-->0.407<!--/n--> [<!--n:q2.ablated.goodput_range-->0.4016–0.4067<!--/n-->] |
| noradix 정확 길이 admission (n=1) | <!--n:kv.pool_tokens-->87,552<!--/n--> | <!--n:q2.noradix.retractions-->0<!--/n--> | <!--n:q2.noradix.stall5-->0<!--/n--> | <!--n:q2.noradix.maxitl_p99_s-->0.12<!--/n--> | <!--n:q2.noradix.ttft_p99_s-->89.9<!--/n--> | <!--n:q2.noradix.ttft_mean_s-->30.3<!--/n--> | <!--n:q2.noradix.slo_ratio-->120/120<!--/n--> | <!--n:q2.noradix.wall_s-->275.6<!--/n--> | <!--n:q2.noradix.goodput-->0.435<!--/n--> |
| tuned N=24 (n=1) | <!--n:kv.pool_tokens-->87,552<!--/n--> | <!--n:q2.tuned_n24.retractions-->0<!--/n--> | <!--n:q2.tuned_n24.stall5-->0<!--/n--> | <!--n:q2.tuned_n24.maxitl_p99_s-->0.05<!--/n--> | <!--n:q2.tuned_n24.ttft_p99_s-->156.3<!--/n--> | <!--n:q2.tuned_n24.ttft_mean_s-->62.6<!--/n--> | <!--n:q2.tuned_n24.slo_ratio-->120/120<!--/n--> | <!--n:q2.tuned_n24.wall_s-->313.9<!--/n--> | <!--n:q2.tuned_n24.goodput-->0.382<!--/n--> |
| tuned N=32 (n=1) | <!--n:kv.pool_tokens-->87,552<!--/n--> | <!--n:q2.tuned_n32.retractions-->0<!--/n--> | <!--n:q2.tuned_n32.stall5-->0<!--/n--> | <!--n:q2.tuned_n32.maxitl_p99_s-->0.07<!--/n--> | <!--n:q2.tuned_n32.ttft_p99_s-->114.6<!--/n--> | <!--n:q2.tuned_n32.ttft_mean_s-->45.6<!--/n--> | <!--n:q2.tuned_n32.slo_ratio-->120/120<!--/n--> | <!--n:q2.tuned_n32.wall_s-->294.7<!--/n--> | <!--n:q2.tuned_n32.goodput-->0.407<!--/n--> |
| tuned N=40 (n=1) | <!--n:kv.pool_tokens-->87,552<!--/n--> | <!--n:q2.tuned_n40.retractions-->0<!--/n--> | <!--n:q2.tuned_n40.stall5-->0<!--/n--> | <!--n:q2.tuned_n40.maxitl_p99_s-->0.09<!--/n--> | <!--n:q2.tuned_n40.ttft_p99_s-->93.5<!--/n--> | <!--n:q2.tuned_n40.ttft_mean_s-->35.2<!--/n--> | <!--n:q2.tuned_n40.slo_ratio-->120/120<!--/n--> | <!--n:q2.tuned_n40.wall_s-->277.9<!--/n--> | <!--n:q2.tuned_n40.goodput-->0.432<!--/n--> |
| tuned N=48 (n=1) | <!--n:kv.pool_tokens-->87,552<!--/n--> | <!--n:q2.tuned_n48.retractions-->5<!--/n--> | <!--n:q2.tuned_n48.stall5-->5<!--/n--> | <!--n:q2.tuned_n48.maxitl_p99_s-->76.1<!--/n--> | <!--n:q2.tuned_n48.ttft_p99_s-->72.9<!--/n--> | <!--n:q2.tuned_n48.ttft_mean_s-->28.5<!--/n--> | <!--n:q2.tuned_n48.slo_ratio-->117/120<!--/n--> | <!--n:q2.tuned_n48.wall_s-->274.4<!--/n--> | <!--n:q2.tuned_n48.goodput-->0.426<!--/n--> |

입장을 보수적으로 바꾼 개입(정확 길이 admission, 동시 제한 N ≤ 40)은 모두 선점과 긴 정지를 없앴다. 풀을 줄이면(cap65k) 정지와 대기 꼬리가 커졌다: maxITL p99 <!--n:q2.default.maxitl_p99_s-->63.1<!--/n--> → <!--n:q2.cap65k.maxitl_p99_s-->103.8<!--/n--> s, TTFT p99 <!--n:q2.default.ttft_p99_s-->64.2<!--/n--> → <!--n:q2.cap65k.ttft_p99_s-->114.0<!--/n--> s. 다만 cap65k는 1회이고, 선점 <!--n:q2.cap65k.retractions-->22<!--/n-->회는 반복 범위(default·ablated <!--n:q2.default.retractions_min-->19<!--/n-->–<!--n:q2.ablated.retractions_max-->21<!--/n-->)를 겨우 넘으므로 **중간 강도의 증거**다. cap65k의 SLO 충족(<!--n:q2.cap65k.slo_ratio-->117/120<!--/n-->)이 default보다 많은 것도 n=1의 흔들림 안이다. goodput이 낮은 것은 wall이 <!--n:q2.cap65k.wall_s-->305.5<!--/n--> s로 길어졌기 때문이다. chunked prefill OFF는 선점 수와 goodput 모두 반복 편차 안이라 prefill 경로는 원인이 아니다.

선점 직후의 서버 로그(default r1, [`data/logs_sample/server_reasoning_q2__default_r1.log`](../data/logs_sample/server_reasoning_q2__default_r1.log) 268–284행, HTTP 접근 로그 줄은 생략):

```text
[2026-09-13 16:06:04] Decode batch, #running-req: 67, #token: 81316, token usage: 0.93, cuda graph: False, gen throughput (token/s): 2663.43, #queue-req: 9
[2026-09-13 16:06:05] Decode batch, #running-req: 66, #token: 82002, token usage: 0.94, cuda graph: False, gen throughput (token/s): 2622.21, #queue-req: 10
[2026-09-13 16:06:06] Decode batch, #running-req: 66, #token: 84642, token usage: 0.97, cuda graph: False, gen throughput (token/s): 2574.67, #queue-req: 11
[2026-09-13 16:06:07] Decode batch, #running-req: 66, #token: 87282, token usage: 1.00, cuda graph: False, gen throughput (token/s): 2529.33, #queue-req: 13
[2026-09-13 16:06:07] KV cache pool is full. Retract requests. #retracted_reqs: 1, #new_tokens_gained: 646, #new_token_ratio: 0.0980 -> 0.3487
[2026-09-13 16:06:07] KV cache pool is full. Retract requests. #retracted_reqs: 1, #new_tokens_gained: 657, #new_token_ratio: 0.3397 -> 0.3530
[2026-09-13 16:06:07] KV cache pool is full. Retract requests. #retracted_reqs: 1, #new_tokens_gained: 761, #new_token_ratio: 0.3440 -> 0.3574
[2026-09-13 16:06:08] KV cache pool is full. Retract requests. #retracted_reqs: 1, #new_tokens_gained: 775, #new_token_ratio: 0.3464 -> 0.3607
[2026-09-13 16:06:08] Decode batch, #running-req: 62, #token: 87001, token usage: 0.99, cuda graph: False, gen throughput (token/s): 2467.25, #queue-req: 18
[2026-09-13 16:06:08] KV cache pool is full. Retract requests. #retracted_reqs: 1, #new_tokens_gained: 792, #new_token_ratio: 0.3497 -> 0.3671
[2026-09-13 16:06:09] Decode batch, #running-req: 60, #token: 87429, token usage: 1.00, cuda graph: False, gen throughput (token/s): 2391.98, #queue-req: 20
```

동시 66개가 decode하는 동안 풀이 1.00에 닿자 2초 사이에 다섯 요청이 선점되고, 대기열이 13에서 20으로 는다. `new_token_ratio`는 선점 직후 0.098에서 0.35 근처로 튀었다가 다시 줄어드는 톱니를 그린다. 같은 패턴이 순정 q2(`logs/server_reasoning_q2__upstream_r1.log`, 15:46:58, 원시 데이터)에도 있다.

### 2.3 부하 스윕

**표 9. 부하 스윕 (순정 0.5.18. qps 0.35는 09-13 순정 3회 중앙값, 나머지는 09-07 스윕 각 n=1)**

| QPS | goodput (req/s) | SLO 충족 (%) | TTFT p99 (s) | TPOT p50 (ms) | TPOT p99 (ms) | maxITL p99 (s) | 선점 | 5 s 넘는 정지 | token usage 최대 | out_tok/s |
|---|---|---|---|---|---|---|---|---|---|---|
| 0.35★ | <!--n:baseline.q035.upstream.goodput-->0.305<!--/n--> | <!--n:baseline.q035.upstream.slo_pct-->100.0<!--/n--> | <!--n:baseline.q035.upstream.ttft_p99_s-->0.07<!--/n--> | <!--n:baseline.q035.upstream.tpot_p50_ms-->16.5<!--/n--> | <!--n:baseline.q035.upstream.tpot_p99_ms-->20.2<!--/n--> | <!--n:baseline.q035.upstream.maxitl_p99_s-->0.06<!--/n--> | <!--n:baseline.q035.upstream.retractions-->0<!--/n--> | 0 | <!--n:baseline.q035.upstream.max_usage-->0.76<!--/n--> | <!--n:baseline.q035.upstream.out_tok_s-->858.2<!--/n--> |
| 0.5 | <!--n:sweep0907.goodput.q0.5-->0.365<!--/n--> | <!--n:sweep0907.slo_pct.q0.5-->100.0<!--/n--> | <!--n:sweep0907.ttft_p99_s.q0.5-->0.07<!--/n--> | <!--n:sweep0907.tpot_p50_ms.q0.5-->20.4<!--/n--> | <!--n:sweep0907.tpot_p99_ms.q0.5-->21.9<!--/n--> | <!--n:sweep0907.maxitl_p99_s.q0.5-->0.07<!--/n--> | <!--n:sweep0907.retractions.q0.5-->0<!--/n--> | <!--n:sweep0907.stall5.q0.5-->0<!--/n--> | <!--n:sweep0907.max_usage.q0.5-->0.82<!--/n--> | <!--n:sweep0907.out_tok_s.q0.5-->1,026.1<!--/n--> |
| 0.6 | <!--n:sweep0907.goodput.q0.6-->0.390<!--/n--> | <!--n:sweep0907.slo_pct.q0.6-->100.0<!--/n--> | <!--n:sweep0907.ttft_p99_s.q0.6-->16.2<!--/n--> | <!--n:sweep0907.tpot_p50_ms.q0.6-->21.7<!--/n--> | <!--n:sweep0907.tpot_p99_ms.q0.6-->23.9<!--/n--> | <!--n:sweep0907.maxitl_p99_s.q0.6-->0.12<!--/n--> | <!--n:sweep0907.retractions.q0.6-->1<!--/n--> | <!--n:sweep0907.stall5.q0.6-->1<!--/n--> | <!--n:sweep0907.max_usage.q0.6-->0.99<!--/n--> | <!--n:sweep0907.out_tok_s.q0.6-->1,095.4<!--/n--> |
| 0.8 | <!--n:sweep0907.goodput.q0.8-->0.407<!--/n--> | <!--n:sweep0907.slo_pct.q0.8-->100.0<!--/n--> | <!--n:sweep0907.ttft_p99_s.q0.8-->29.1<!--/n--> | <!--n:sweep0907.tpot_p50_ms.q0.8-->23.0<!--/n--> | <!--n:sweep0907.tpot_p99_ms.q0.8-->38.6<!--/n--> | <!--n:sweep0907.maxitl_p99_s.q0.8-->24.6<!--/n--> | <!--n:sweep0907.retractions.q0.8-->4<!--/n--> | <!--n:sweep0907.stall5.q0.8-->4<!--/n--> | <!--n:sweep0907.max_usage.q0.8-->1.00<!--/n--> | <!--n:sweep0907.out_tok_s.q0.8-->1,143.8<!--/n--> |
| 1 | <!--n:sweep0907.goodput.q1-->0.425<!--/n--> | <!--n:sweep0907.slo_pct.q1-->100.0<!--/n--> | <!--n:sweep0907.ttft_p99_s.q1-->46.5<!--/n--> | <!--n:sweep0907.tpot_p50_ms.q1-->23.6<!--/n--> | <!--n:sweep0907.tpot_p99_ms.q1-->36.4<!--/n--> | <!--n:sweep0907.maxitl_p99_s.q1-->29.5<!--/n--> | <!--n:sweep0907.retractions.q1-->7<!--/n--> | <!--n:sweep0907.stall5.q1-->7<!--/n--> | <!--n:sweep0907.max_usage.q1-->1.00<!--/n--> | <!--n:sweep0907.out_tok_s.q1-->1,196.0<!--/n--> |
| 2 | <!--n:sweep0907.goodput.q2-->0.407<!--/n--> | <!--n:sweep0907.slo_pct.q2-->96.7<!--/n--> | <!--n:sweep0907.ttft_p99_s.q2-->63.9<!--/n--> | <!--n:sweep0907.tpot_p50_ms.q2-->24.1<!--/n--> | <!--n:sweep0907.tpot_p99_ms.q2-->93.2<!--/n--> | <!--n:sweep0907.maxitl_p99_s.q2-->62.6<!--/n--> | <!--n:sweep0907.retractions.q2-->20<!--/n--> | <!--n:sweep0907.stall5.q2-->20<!--/n--> | <!--n:sweep0907.max_usage.q2-->1.00<!--/n--> | <!--n:sweep0907.out_tok_s.q2-->1,183.2<!--/n--> |
| 4 | <!--n:sweep0907.goodput.q4-->0.379<!--/n--> | <!--n:sweep0907.slo_pct.q4-->90.0<!--/n--> | <!--n:sweep0907.ttft_p99_s.q4-->75.7<!--/n--> | <!--n:sweep0907.tpot_p50_ms.q4-->24.5<!--/n--> | <!--n:sweep0907.tpot_p99_ms.q4-->94.9<!--/n--> | <!--n:sweep0907.maxitl_p99_s.q4-->102.5<!--/n--> | <!--n:sweep0907.retractions.q4-->29<!--/n--> | <!--n:sweep0907.stall5.q4-->29<!--/n--> | <!--n:sweep0907.max_usage.q4-->1.00<!--/n--> | <!--n:sweep0907.out_tok_s.q4-->1,183.8<!--/n--> |
| 8 | <!--n:sweep0907.goodput.q8-->0.365<!--/n--> | <!--n:sweep0907.slo_pct.q8-->88.3<!--/n--> | <!--n:sweep0907.ttft_p99_s.q8-->78.2<!--/n--> | <!--n:sweep0907.tpot_p50_ms.q8-->24.3<!--/n--> | <!--n:sweep0907.tpot_p99_ms.q8-->102.3<!--/n--> | <!--n:sweep0907.maxitl_p99_s.q8-->116.2<!--/n--> | <!--n:sweep0907.retractions.q8-->28<!--/n--> | <!--n:sweep0907.stall5.q8-->28<!--/n--> | <!--n:sweep0907.max_usage.q8-->1.00<!--/n--> | <!--n:sweep0907.out_tok_s.q8-->1,162.2<!--/n--> |

표 아래 주:

- 제출본은 TPOT p99 열에 p50 값을 옮겨 적었다. 실제 p99는 q2부터 SLO <!--n:trace.tpot_slo_ms-->60<!--/n--> ms를 넘는다(<!--n:sweep0907.tpot_p99_ms.q2-->93.2<!--/n--> ms). 선점된 요청은 수십 초 멈춘 시간까지 평균 TPOT에 들어가기 때문이다. 그래서 SLO 충족률이 <!--n:sweep0907.slo_pct.q2-->96.7<!--/n--> → <!--n:sweep0907.slo_pct.q4-->90.0<!--/n--> → <!--n:sweep0907.slo_pct.q8-->88.3<!--/n-->%로 떨어진다.
- 선점 수는 서버 로그 `Retract requests` 줄의 `#retracted_reqs` 합이다. `grep -ci retract`는 server_args 줄(`retraction_policy='length'`)까지 1줄 더 세므로 쓰지 않았다. 이 스윕의 q2(선점 <!--n:sweep0907.retractions.q2-->20<!--/n-->)는 4.2의 q2 default(09-13 패치 빌드·플래그 OFF, 3회 중앙값 <!--n:q2.default.retractions-->19<!--/n-->)와 실행 집합이 다르다.

![09-07 순정 부하 스윕](figures/fig02_sweep_0907.png)

*그림 3. 09-07 순정 부하 스윕(부하당 n=1). 위: QPS에 따른 선점 수. 아래: TTFT p99와 maxITL p99(로그 축). 음영은 Little 법칙 한계 λmax <!--n:diag.little_lambda_max-->0.56–0.67<!--/n--> QPS다.*

**무릎(knee).** 아래는 모두 09-07 스윕(부하당 n=1)의 값이다. 선점은 q<!--n:diag.first_retraction_qps-->0.6<!--/n-->에서 시작한다(1회, 피해 요청 reason-00087이 9.1 s 정지, token usage 최대 <!--n:sweep0907.max_usage.q0.6-->0.99<!--/n-->). 풀은 q0.8부터 <!--n:sweep0907.max_usage.q0.8-->1.00<!--/n-->에 닿는다. goodput은 q1(<!--n:sweep0907.goodput.q1-->0.425<!--/n-->)에서 가장 높지만, 이 정점은 처리 능력의 한계가 아니다.

- **q1까지 goodput이 오르는 이유.** goodput = SLO 충족 수 ÷ wall이고 W3 SLO는 요청별 평균 TPOT 하나뿐이다. 그래서 q ≤ 1에서는 선점 피해 요청도 평균 TPOT ≤ 60 ms를 통과하고, goodput이 도착률을 따라 오른다.
- **q2부터 SLO 위반이 생기는 이유.** 선점된 요청이 대기열 맨 뒤에서 <!--n:diag.stall_range_q2_s-->30.9–64.1<!--/n--> s 기다리면서 평균 TPOT가 60 ms를 넘는다. 위반은 q2·q4·q8에서 4 → 12 → 14건이다. 위반 요청은 짧은 요청에 몰린다: 같은 정지라도 토큰이 적으면 평균 TPOT가 더 크게 오르기 때문이고, 위반 요청의 출력 길이 중앙값은 통과 요청의 0.44(q2)·0.64(q4)·0.68(q8)배다.
- **wall을 정하는 요청.** wall은 마지막으로 끝나는 요청 하나가 정한다. q ≤ 1에서는 reason-00101(8,263토큰)이고, q0.8·q1에서는 이 요청의 입장 대기 28.7/27.7 s가 wall에 들어간다. q ≥ 2에서는 <!--n:q2.longest.rid-->reason-00064<!--/n-->(<!--n:q2.longest.out_tokens-->10,541<!--/n-->토큰)이고, 이 요청 자신이 선점돼 멈춘 61.5/83.3/109.4 s(q2/q4/q8)가 wall에 들어간다.
- **out_tok/s는 포화 지표가 아니다.** out_tok/s = <!--n:trace.out_total-->337,433<!--/n--> ÷ wall이다. q0.5 → q1에서 <!--n:sweep0907.out_tok_s_change_q0.5_q1_pct-->+16.6<!--/n-->%(<!--n:sweep0907.out_tok_s.q0.5-->1,026.1<!--/n--> → <!--n:sweep0907.out_tok_s.q1-->1,196.0<!--/n-->), q0.5 → q8은 <!--n:sweep0907.out_tok_s_change_q0.5_q8_pct-->+13.3<!--/n-->%(<!--n:sweep0907.out_tok_s.q0.5-->1,026.1<!--/n--> → <!--n:sweep0907.out_tok_s.q8-->1,162.2<!--/n-->)다. 정점 이후의 감소는 재prefill 낭비가 아니다: 재계산 초과분은 q2 <!--n:diag.prefill_excess.q2-->21,452<!--/n--> · q4 <!--n:diag.prefill_excess.q4-->26,972<!--/n--> · q8 <!--n:diag.prefill_excess.q8-->32,294<!--/n-->토큰뿐이다. 서버 로그의 순간 생성 처리량(`gen throughput`) 최대값도 q0.5 1,703 → q8 4,056 tok/s로 계속 오르므로(원시 로그 `logs/server_q{Q}.log`) 엔진이 포화된 것도 아니다.

## 3. 최적화 설계와 구현

### 3.1 SGLang이 이미 하는 것

**표 10. W3와 관련된 기존 메커니즘 (SGLang v0.5.18)**

| 기존 메커니즘 | 위치 (`python/sglang/srt/…`) | 동작 | 이 워크로드에서 |
|---|---|---|---|
| 연속 배칭 | `managers/scheduler.py` | 매 step 배치를 다시 구성해 끝난 요청 자리에 새 요청을 넣는다 | qps 0.35: 최대 동시 <!--n:baseline.q035.upstream.max_running-->26<!--/n-->, TPOT p50 <!--n:baseline.q035.upstream.tpot_p50_ms-->16.5<!--/n--> ms로 정상이다. q2: 동시 <!--n:sweep0907.max_running.q2-->68<!--/n-->까지 늘지만 그 사이에 선점이 섞인다 |
| 미래 토큰 예약 admission (radix ON 기본 경로) | [`managers/schedule_policy.py:654`](https://github.com/sgl-project/sglang/blob/v0.5.18/python/sglang/srt/managers/schedule_policy.py#L654) `PrefillAdder`, [`new_token_ratio_tracker.py`](https://github.com/sgl-project/sglang/blob/v0.5.18/python/sglang/srt/managers/scheduler_components/new_token_ratio_tracker.py) | 실행 중 요청마다 min(남은 `max_new_tokens`, 4,096) × `new_token_ratio`만큼 KV를 예약하고, 남는 만큼만 새 요청을 받는다. `new_token_ratio`는 0.7 × `--schedule-conservativeness`에서 시작해 600 step에 걸쳐 그 0.14배(0.098)까지 줄고, 선점 직후 다시 튄다 | q2에서 풀 1.00, 선점 <!--n:q2.default.retractions-->19<!--/n-->회(★). 요청당 예약이 최대 4,096 × 0.098 ≈ 401토큰이라, 평균 <!--n:trace.out_mean-->2,811.9<!--/n-->토큰을 더 쓸 요청들을 너무 많이 받는다 |
| Retraction(선점) | [`managers/schedule_batch.py:2867`](https://github.com/sgl-project/sglang/blob/v0.5.18/python/sglang/srt/managers/schedule_batch.py#L2867) 선점 순서, [`managers/scheduler.py:2725`](https://github.com/sgl-project/sglang/blob/v0.5.18/python/sglang/srt/managers/scheduler.py#L2725) 재진입 | KV가 모자라면 생성 토큰이 가장 적은 요청부터(동률이면 입력이 긴 쪽) 선점한다. 선점된 요청의 KV는 해제하고 대기열 맨 뒤에 다시 넣는다 | 09-07 q2(n=1): 피해 요청 <!--n:sweep0907.stall5.q2-->20<!--/n-->건이 <!--n:diag.stall_range_q2_s-->30.9–64.1<!--/n--> s 멈췄고 재계산 초과분은 <!--n:diag.prefill_excess.q2-->21,452<!--/n-->토큰이다. 낭비의 본체는 재계산이 아니라 대기다. '생성 토큰이 가장 적은 요청부터 선점'은 이미 기본값이다 |
| ignore_eos 전용 정확 길이 admission | [`managers/schedule_policy.py:1065`](https://github.com/sgl-project/sglang/blob/v0.5.18/python/sglang/srt/managers/schedule_policy.py#L1065) `add_one_req_ignore_eos`, 분기 [`:1213`](https://github.com/sgl-project/sglang/blob/v0.5.18/python/sglang/srt/managers/schedule_policy.py#L1213) | `ignore_eos=True`이고 radix cache가 꺼져 있을 때만 쓰인다. 각 요청의 남은 `max_new_tokens` 전체로 미래 KV 최고점을 시뮬레이션해 입장시킨다 | q2 `--disable-radix-cache`(n=1): 선점 <!--n:q2.noradix.retractions-->0<!--/n-->, maxITL p99 <!--n:q2.noradix.maxitl_p99_s-->0.12<!--/n--> s, TTFT p99 <!--n:q2.noradix.ttft_p99_s-->89.9<!--/n--> s, goodput <!--n:q2.noradix.goodput-->0.435<!--/n-->. radix를 끈 결과를 캐시 효과로만 해석하면 안 되는 이유이기도 하다 |
| Chunked prefill | `--chunked-prefill-size` (기본 2,048) | 긴 prefill을 쪼갠다 | 입력이 387토큰이라 한 번도 쪼개지지 않는다. q2 OFF(ablated★): 선점 <!--n:q2.ablated.retractions-->21<!--/n-->, goodput <!--n:q2.ablated.goodput-->0.407<!--/n-->로 default와 차이가 없다 |
| Hierarchical KV 오프로딩 | `--enable-hierarchical-cache` | KV를 CPU 메모리로 내린다(기본 꺼짐) | 측정하지 않았다. 선점된 KV를 host로 옮기면 재계산은 줄지만, 정지의 원인(대기열 맨 뒤 재진입)은 그대로라 범위 밖으로 두었다 |

### 3.2 내가 추가한 것

**표 11. 기존 SGLang(radix ON 기본 경로)과 이 패치의 차이**

| 비교 항목 | 기존 SGLang 0.5.18 | 이 패치 (플래그 ON) |
|---|---|---|
| 결정 시점 | 사전 admission은 느슨하게 하고, 모자라면 사후 retraction | 사전 admission에서 앞으로의 KV 최고점을 시뮬레이션해 입장을 늦춘다. retraction은 그대로 남아 안전망이 된다 |
| 쓰는 정보 | 실행 중 요청의 남은 `max_new_tokens`(4,096 clip) × 전역 `new_token_ratio`. 새 요청의 길이는 쓰지 않는다 | 실행 중·이번 라운드·후보 요청 각각의 남은 `max_new_tokens`(clip 없음), 현재 보유 토큰, radix로 공유된 prefix, available + evictable. trace의 출력 길이를 그대로 믿는다(오라클) |
| 예약량 계산 | Σ min(left, 4,096) × ratio (선형 합) | 남은 길이 순으로 정렬해, i번째 요청이 끝나는 순간의 여유 = free + Σ(먼저 끝난 요청이 푸는 토큰) − (남은 요청 수) × α·left_i. 모든 i에서 이 값이 (남은 요청 수) × max(1, page_size)보다 커야 입장 |
| 길이 정보가 틀렸을 때 | 과소 예약 → 풀이 차면 선점 → 대기열 맨 뒤 | α < 1(과소 예약)이면 남는 위험을 기존 retraction이 처리한다. 출력이 `max_new_tokens`보다 일찍 끝나는 요청(EOS)이면 과대 예약이 되어 KV가 비어 있어도 입장을 미룬다 → GPU 유휴·TTFT 증가(이 스터디에서는 측정하지 않음) |
| 추가 상태 | — | 없음. 입장 시도마다 O(n log n) 계산, 로그용 모듈 전역 카운터 2개 |
| 튜닝 파라미터 | `--schedule-conservativeness`, `--max-running-requests`, `SGLANG_CLIP_MAX_NEW_TOKENS_ESTIMATION` | `--peak-kv-reserve-ratio` α ∈ (0, 1] |

SGLang 0.5.18에는 이미 '알려진 출력 길이로 미래 최고점을 시뮬레이션하는 입장 검사'(`add_one_req_ignore_eos`)가 있다. 그러나 radix cache가 꺼져 있을 때만 쓰이고([`schedule_policy.py:1213`](https://github.com/sgl-project/sglang/blob/v0.5.18/python/sglang/srt/managers/schedule_policy.py#L1213)), 기본값인 radix ON에서는 요청당 최대 약 401토큰만 예약하는 느슨한 경로를 탄다. 그래서 기본 설정에서는 q0.6 이상에서 선점이 일어나고, 선점된 요청은 대기열 맨 뒤로 가서 수십 초 멈춘다.

이 패치는 그 기존 시뮬레이션을 radix ON 경로에 추가 검사로 **이식**한 것이다(+<!--n:patch.lines_added-->102<!--/n-->/−<!--n:patch.lines_removed-->0<!--/n-->줄, 플래그 뒤). radix ON에 맞추려고 공유 prefix는 끝나도 풀리지 않는다고 보고, evictable 캐시는 여유로 센다. 예약 비율 α로 '기존(거의 예약하지 않음)'과 '오라클(α = 1)' 사이를 조절할 수 있게 했다. α = 1은 기존 `--disable-radix-cache` 경로의 radix 호환판이다. 스케줄링 정책을 새로 설계한 것이 아니고, 기존 검사를 약하게 만들지 않고 거절만 추가하므로 켜도 순정보다 많이 받아들이는 일은 없다. 이식으로 실제로 얻는 것이 무엇인지는 4.2에서 수치로 비교한다. W3에서는 기존 `--disable-radix-cache`만으로 이득의 <!--n:q2.share_of_gain.noradix_pct-->88.0<!--/n-->%가 나온다(n=1).

**출력 길이에 대한 전제.** 모든 trace가 `ignore_eos=True`라 `max_new_tokens`가 곧 실제 출력 길이다. 따라서 이 게이트는 합성 trace의 알려진 출력 길이를 이용한 예약(오라클)이고, 출력 길이를 추정하는 모델을 만든 것이 아니다. 길이를 모를 때 생기는 과대 예약의 손해는 측정하지 않았다(교차 trace 5종도 모두 `ignore_eos=True`). 예컨대 `max_tokens` 없이 EOS로 끝나는 요청은 `max_new_tokens`가 context 한도에서 정해져(이 설정에서 32,379토큰), α = 1 게이트는 동시에 시작한 그런 요청을 2개까지만 받는다. 이 값은 GPU 측정이 아니라 CPU에서 게이트를 실행한 결과다([tests/README.md](../tests/README.md)의 L3).

**아이디어의 출처.** '실행 중 요청과 이번에 받을 요청의 peak KV를 보수적으로 추정해 입장을 늦춘다'는 방향은 과정 힌트([step3_hint.md 6절](https://github.com/mlleo/inference-engine-study/blob/802a1642fcefc06735a89af054213c9904b4ff45/project/docs/step3_hint.md))에서 왔다. 기존 정확 길이 경로가 radix ON에서 쓰이지 않는다는 분석, radix ON 이식과 α 손잡이, 측정과 검증은 본인 작업이다.

### 3.3 구현

**표 12. 수정 파일**

| 파일 (`python/sglang/srt/…`) | 변경 | +줄 / −줄 |
|---|---|---|
| `server_args.py` | `enable_peak_kv_reservation`(bool, 기본 False)·`peak_kv_reserve_ratio`(float, 기본 1.0) 필드를 `NS("schedule")`로 추가하고 범위 검사 | +<!--n:patch.lines_added.server_args-->17<!--/n--> / −0 |
| `managers/scheduler.py` | `PrefillAdder`를 만들 때 플래그가 켜져 있으면 α, 꺼져 있으면 `None`을 넘긴다 | +<!--n:patch.lines_added.scheduler-->5<!--/n--> / −0 |
| `managers/schedule_policy.py` | `_peak_kv_fits()` 추가. `add_one_req()`의 기존 KV 예산 검사 뒤, prefill delayer 협상 앞에 게이트 하나. 지연 로그(5 s에 최대 1줄) | +<!--n:patch.lines_added.schedule_policy-->80<!--/n--> / −0 |
| 합계 | <!--n:patch.files-->3<!--/n-->파일, 삭제·수정한 기존 줄 0 (추가만) | +<!--n:patch.lines_added-->102<!--/n--> / −<!--n:patch.lines_removed-->0<!--/n--> |

활성화 플래그는 `--enable-peak-kv-reservation`(기본 꺼짐)이고, 추가 파라미터는 `--peak-kv-reserve-ratio`(기본 1.0, 범위 (0, 1])다. 최종 선택은 α* = <!--n:select.alpha_star-->1.0<!--/n-->이다. 핵심 코드(`patch/peak_kv_reservation.diff` 발췌, docstring 생략):

```python
# schedule_policy.py — PrefillAdder.add_one_req(): 기존 KV 예산 재검사 뒤, prefill delayer 협상 앞
            if (
                self.peak_kv_reserve_ratio is not None
                and not self.is_hybrid_swa
                and self.dllm_config is None
                and not self._peak_kv_fits(req, cand_extend_input_len)
            ):
                return AddReqResult.NO_TOKEN

    def _peak_kv_fits(self, req: Req, extend_input_len: int) -> bool:
        running = [] if self.running_batch is None else self.running_batch.reqs
        running = [r for r in running if not r.finished()]
        if not running and not self.can_run_list:
            # Invariant: an idle server always admits one request (no deadlock).
            return True

        ratio = self.peak_kv_reserve_ratio
        states = []
        for r, in_running in (
            *((r, True) for r in running),
            *((r, False) for r in self.can_run_list),
            (req, False),
        ):
            left = ratio * max(r.sampling_params.max_new_tokens - len(r.output_ids), 0)
            held = len(r.origin_input_ids) + len(r.output_ids)
            # A prefix shared through the radix cache stays locked by the other
            # requests, so it is not freed when this request finishes.
            shared = r.cached_tokens if in_running else len(r.prefix_indices)
            states.append((left, self.ceil_paged_tokens(max(held - shared, 0))))
        states.sort(key=lambda x: x[0])

        free = self.cur_rem_tokens - self.ceil_paged_tokens(extend_input_len)
        reserve = max(IGNORE_EOS_RESERVE_TOKENS, self.page_size)
        freed = 0
        for i, (left, held) in enumerate(states):
            bs = len(states) - i
            min_free = free + freed - left * bs
            if min_free <= reserve * bs:
                _log_peak_kv_delay(len(running), len(self.can_run_list), min_free)
                return False
            freed += held
        return True
```

#### 동작 방식

- **언제.** 스케줄러가 새 prefill 배치를 만들 때 대기열을 FCFS 순으로 돌며 요청마다 `add_one_req()`를 부른다. 게이트는 기존 검사(`rem_total_tokens`, hybrid SWA 재검사)를 통과하고 prefix 노드를 잠근 직후, prefill delayer 협상 전에 실행된다(패치된 파일의 1369–1377행). 거절하면 `NO_TOKEN`을 돌려주고, 기존 코드가 그 라운드의 입장을 멈춘다(대기열 순서는 유지). 모델 안에서는 실행 중 요청이 한 step 진행해도 각 종료 시점의 여유가 변하지 않는다. free는 요청 수만큼 줄지만, 요청마다 left가 1 줄고 held가 1 늘어 그만큼을 정확히 메운다. 그래서 거절된 요청은 실행 중 요청이 끝나 KV가 풀릴 때 비로소 들어간다.
- **무엇을.** 실행 중·이번 라운드·후보 요청의 (남은 예약 α·left, 끝날 때 풀리는 held − 공유 prefix)를 left 순으로 정렬한다. 그리고 i번째 요청이 끝나기 직전의 여유 free + Σ_{j<i} held_j − (n−i)·left_i가 모든 i에서 (n−i) × max(1, page_size)보다 큰지 본다. free는 available + evictable − 이번 라운드 사용분 − 후보의 prefill 길이다. 먼저 끝난 요청의 성장분은 끝날 때 함께 풀리므로 식에서 상쇄된다.
- **불변 조건.**
  - idle 서버(끝나지 않은 실행 중 요청도 이번 라운드 입장도 없음)는 항상 1개를 받는다. 따라서 deadlock이 없다.
  - chunked prefill 이어받기(`add_chunked_req`)에는 검사를 넣지 않는다.
  - 끝난 요청(`finished()`)은 KV가 이미 풀렸으므로 뺀다.
  - 공유 prefix는 실행 중이면 `cached_tokens`, 아직 prefill 전이면 `prefix_indices` 길이만큼 빼서 이중 계산하지 않는다.
  - page 정렬은 `ceil_paged_tokens`와 live 요청당 max(1, page_size)토큰의 여유로 처리한다.
  - hybrid SWA·dLLM 경로는 제외한다.
- **알려진 근사.** 시뮬레이션은 자기 모델 안에서는 정확하지만, 실제 radix 풀과 비교하면 두 경우에 낙관적이다. (1) 공유 prefix를 처음 만든 요청(`cached_tokens = 0`)이나 선점 뒤 다시 들어온 요청이 먼저 끝나면, 공유 prefix 길이(W3에서 <!--n:trace.shared_prefix_tokens-->36<!--/n-->토큰)만큼 낙관적으로 계산한다. (2) 이번 라운드의 chunked 요청은 아직 할당하지 않은 나머지 프롬프트만큼 낙관적이다. 이는 프롬프트가 2,048토큰보다 긴 교차 워크로드(W1·W2·W5)에서 의미가 있고, W3 프롬프트는 쪼개지지 않는다. 'α = 1에서 선점 0'은 W3 조건(공유 <!--n:trace.shared_prefix_tokens-->36<!--/n-->토큰, 387토큰 프롬프트)에서의 실측이지 일반 보장이 아니다. 기존 retraction이 안전망으로 남는다.
- **docstring 정정.** 패치의 `_peak_kv_fits` docstring은 'add_one_req_ignore_eos와 같은 시뮬레이션이되 CLIP_MAX_NEW_TOKENS clip과 new_token_ratio 할인이 없다'고 쓰지만 부정확하다. upstream 경로도 clip을 쓰지 않고, `new_token_ratio`는 `ignore_eos`가 아닌 요청에만 적용된다. 실제 차이는 공유 prefix 차감, α, page 여유, 끝난 요청 제외, idle 불변식이다. 측정에 쓴 diff는 바이트 그대로 두고, 정정 문안은 [patch/README.md](../patch/README.md#notes-from-the-errata)에 적었다.
- **`#delays`는 하한.** 지연 로그는 5 s에 한 번만 찍히므로, 로그의 마지막 `#delays` 값은 실행 마지막 몇 초의 거절을 놓칠 수 있다. 같은 요청이 여러 라운드에서 거절되면 여러 번 센다. 이 보고서의 입장 지연 횟수는 모두 "≥"로 읽는다.

#### 확인한 것

- **GPU 실행.**
  - mine 실행 16회(α 탐색, 부하, 출력 분포 변형, 결정론, page 16)가 모두 120/120을 끝냈다(에러 0, 입장 deadlock 없음).
  - α = 0.9에서 선점된 7건도 모두 끝났다. 재진입 요청이 영구히 기다리는 일은 없었다.
  - W1 RAG(프롬프트 7,296–7,297토큰, chunked prefill로 이어받기) 교차 재생은 60/60 완료했다.
  - `--page-size 16` 스모크(q2, α = 1.0, n=1)는 <!--n:smoke.page16-->120/120<!--/n--> 완료, 선점 <!--n:smoke.page16.retractions-->0<!--/n-->, goodput <!--n:smoke.page16.goodput-->0.440<!--/n-->이었다.
- **CPU 검사(정정판에서 추가, [tests/README.md](../tests/README.md)).** GPU 없이 게이트 로직만 확인한다.
  - α = 1, page 1, 공유 prefix 없음인 영역에서, upstream `add_one_req_ignore_eos`를 그대로 실행한 결과와 판정이 <!--n:patch.equiv_upstream-->19,907/19,907<!--/n--> 일치한다.
  - 토큰 단위 brute-force 시뮬레이션과는 <!--n:patch.equiv_bruteforce-->20,000/20,000<!--/n--> 일치한다.
  - page 2/16/64의 물리 풀에서, 입장시킨 상태 <!--n:patch.equiv_paged_physical-->18,277/18,277<!--/n-->이 page 할당 실패 없이 끝난다.
  - 끝난 요청과 남은 길이가 0인 요청을 섞으면 upstream과 <!--n:patch.equiv_edge_upstream_diff-->695/19,887<!--/n--> 상태에서 판정이 다르다. 이 차이는 의도한 규칙(idle 불변식, 끝난 요청 제외, 남은 길이 0 처리)으로 모두 설명된다(<!--n:patch.equiv_edge_explained-->19,887/19,887<!--/n-->).
  - 이 검사는 GPU 동작이나 스케줄러 통합을 검증하지 않는다.

**표 13. 플래그 OFF가 순정과 같다는 근거**

| 확인 방법 | 결과 | 근거 |
|---|---|---|
| 코드 경로 | 새 코드는 모두 `peak_kv_reserve_ratio is not None` 분기 안에 있고, 플래그 OFF면 스케줄러가 `None`을 넘긴다. 추가 <!--n:patch.lines_added-->102<!--/n-->줄·삭제 <!--n:patch.lines_removed-->0<!--/n-->줄이다. CPU 테스트는 패치본에서 정해진 추가분 8개를 빼면 upstream과 AST가 같은지 확인한다. 서버 로그의 resolved 설정은 `enable_peak_kv_reservation=False`다 | 부록 A, `python3 tests/run_cpu_tests.py` |
| 출력 동일성 (결정론 모드) | 선점 없는 q0.2: upstream ↔ OFF <!--n:correct.det_q02up_vs_q02off-->120/120<!--/n--> PASS. q2는 FAIL(<!--n:correct.det_q2up_vs_q2off-->106/120<!--/n-->)이지만 불일치 14건이 모두 어느 한쪽의 선점 피해 요청이다. 선점이 있는 조건의 재실행 편차이고, upstream끼리 같은 q2를 두 번 돌려도 <!--n:correct.det_q2up_r1_vs_r2-->115/120<!--/n-->이다 | [`data/verify/`](../data/verify/), 4.5 |
| 성능 동일성 | q2 upstream(n=1) goodput <!--n:q2.upstream.goodput-->0.403<!--/n-->, 선점 <!--n:q2.upstream.retractions-->19<!--/n-->, TTFT p99 <!--n:q2.upstream.ttft_p99_s-->64.3<!--/n--> s. 플래그 OFF★는 <!--n:q2.default.goodput-->0.403<!--/n--> [<!--n:q2.default.goodput_range-->0.4025–0.4035<!--/n-->], <!--n:q2.default.retractions-->19<!--/n--> [<!--n:q2.default.retractions_min-->19<!--/n-->–<!--n:q2.default.retractions_max-->20<!--/n-->], <!--n:q2.default.ttft_p99_s-->64.2<!--/n--> s [<!--n:q2.default.ttft_p99_range_s-->64.1–64.3<!--/n-->]. 반복 편차 안이다 | `data/derived/w3_runs.csv` |

### 3.4 구현 중 막혔던 지점

**표 14. 시도했다 버린 것**

| # | 시도한 것 | 관측 결과 | 원인 / 바꾼 것 | 배운 점 |
|---|---|---|---|---|
| 1 | 일반 실행끼리 `bench.verify`로 정확성 판정 | 순정끼리도 FAIL이었다. 09-06 qps 0.35 실행과 출력이 같은 요청은 09-07 q2에서 0/120, q0.5에서 24/120뿐이었다 | 배치 구성과 선점 시점이 바뀌면 greedy 출력이 바뀐다. 95% 기준을 낮추지 않고, 모든 bar에 결정론 모드를 켠 별도 데이터셋으로 판정했다 | 정확성 게이트는 '같은 조건의 재현성'부터 재야 한다 |
| 2 | RTX 4090에서 `--enable-deterministic-inference`를 기본 설정으로 사용 | attention backend를 지정하지 않으면 FA3(Hopper 전용)로 폴백한다. flashinfer는 결정론 모드에서 radix cache를 강제로 끈다([`server_args.py:8253`](https://github.com/sgl-project/sglang/blob/v0.5.18/python/sglang/srt/server_args.py#L8253)) | radix가 꺼지면 admission이 `add_one_req_ignore_eos`로 바뀌어 default까지 오라클이 된다. 그래서 `--attention-backend triton`(radix 호환)을 명시했다 | 결정론 모드도 스케줄러 경로를 바꿀 수 있다. resolved server_args를 반드시 확인한다 |
| 3 | 결정론 모드면 같은 조건 재실행이 100% 같을 것이라 기대 | q2 upstream r1 ↔ r2가 <!--n:correct.det_q2up_r1_vs_r2-->115/120<!--/n-->이고, 불일치한 요청은 모두 선점 피해 요청이다(<!--n:correct.det_q2up_r1_vs_r2_mismatches_in_victims-->5/5<!--/n-->) | 선점 후 재prefill로 다시 계산한 KV는 decode로 쌓은 KV와 수치가 다르다. 선점 없는 기준(q0.2)을 추가해 분리했다: q0.2 ↔ q2는 <!--n:correct.det_q02up_vs_q2up-->98/120<!--/n-->이고, 불일치 22건이 모두 피해 요청이며, 피해를 입지 않은 90건은 100% 같다 | 선점은 지연뿐 아니라 출력도 바꾼다. mine은 선점 없는 기준과 비교해야 공정하다 |
| 4 | 처음 설계: 실행 중 요청의 공유 prefix를 `len(prefix_indices)`로 빼기 | 코드를 보니 prefill 뒤 `cache_unfinished_req`가 `prefix_indices`를 프롬프트 전체로 바꾼다([`radix_cache.py:577`](https://github.com/sgl-project/sglang/blob/v0.5.18/python/sglang/srt/mem_cache/radix_cache.py#L577)) | 그대로 쓰면 끝날 때 풀리는 토큰을 요청마다 프롬프트 길이 가까이 적게 봐서 과보수가 된다. 실행 중 요청은 첫 prefill 때의 `cached_tokens`(<!--n:trace.shared_prefix_tokens-->36<!--/n-->)를 쓰도록 바꿨다 | 스케줄러 필드는 수명 주기마다 의미가 바뀐다. 쓰기 전에 누가 언제 덮어쓰는지 추적한다 |
| 5 | goodput 한 숫자로 개선 판단("0.3 → 0.4") | qps 0.35 기준선 <!--n:baseline.q035.upstream.goodput-->0.305<!--/n-->와 q0.5–q8 스윕의 <!--n:sweep0907.goodput.q0.5-->0.365<!--/n-->–<!--n:sweep0907.goodput.q1-->0.425<!--/n-->를 같은 default로 비교한 착시에, 소수 1자리 반올림이 겹쳤다 | SLO가 거의 100%면 goodput ≈ 120 ÷ wall이고, wall은 도착 구간과 마지막 긴 요청이 정한다. goodput은 항상 QPS와 함께, 소수 3자리로, TTFT·maxITL·wall과 같이 보고한다 | 헤드라인 지표가 무엇에 묶여 있는지부터 분해한다 |

## 4. 평가

### 4.1 실험 환경과 절차

**표 15. 실험 환경**

| 항목 | 값 |
|---|---|
| SGLang | 0.5.18, commit `71de97b`(release/v0.5.18, editable 설치). 패치 `patch/peak_kv_reservation.diff`(sha256 `bb3d8657…`)는 09-13 16:04 UTC에 적용했고, 그 전의 실행은 순정이다 |
| torch / CUDA | 2.13.0+cu129 / 12.9 (09-13 실험 파드에서 남긴 환경 기록. 서버 로그에는 남지 않는다) |
| 모델 | Qwen/Qwen3-4B (bf16), snapshot `1cfa9a7208912126459214e8b04321603b3df60c` |
| GPU | NVIDIA GeForce RTX 4090, 24,564 MiB, 드라이버 580.126.20 (위와 같은 환경 기록) |
| KV 풀 | `max_total_num_tokens` = <!--n:kv.pool_tokens-->87,552<!--/n--> (K 6.01 GB + V 6.01 GB, 토큰당 144 KiB). cap65k 대조만 <!--n:kv.cap65k_pool_tokens-->65,536<!--/n--> |
| 서버 공통 플래그 | `--context-length 32768 --mem-fraction-static 0.85 --random-seed 42 --log-level info --enable-metrics`. resolved: `attention_backend=flashinfer`, `chunked_prefill_size=2048`, `schedule_policy=fcfs`, `page_size=1`, decode CUDA graph는 배치 ≤ 24 |
| 정확성 데이터셋 | 위 플래그 + `--enable-deterministic-inference --attention-backend triton` (모든 bar에 공통) |
| ablated | `--chunked-prefill-size -1`. resolved 설정에서 prefill CUDA graph도 꺼진다(`cuda_graph_config.prefill`의 `max_bs=-1, bs=[]`; default는 `max_bs=2048`) |
| mine | `--enable-peak-kv-reservation --peak-kv-reserve-ratio 1.0` (α* = <!--n:select.alpha_star-->1.0<!--/n-->, 선택 규칙은 4.2) |
| 4번째 control | `--max-running-requests 40` (N* = <!--n:select.n_star-->40<!--/n-->) |
| trace | 최종 비교 `traces/reasoning_q2.jsonl`(sha256 `a43a9e4a…0c`), 기본 `traces/reasoning.jsonl`(`c90bb7f2…47`). 전체 해시와 재생성 명령은 [`data/traces.sha256`](../data/traces.sha256) |
| 반복 | q2의 default·ablated·mine만 bar마다 3회(★, 중앙값 [최소–최대]), 그 밖은 모두 1회(n=1). 실행마다 서버를 새로 시작하고, 재생 워밍업 3건 뒤 측정한다 |
| 실행 예산 | 본 실험 <!--n:budget.runs-->53<!--/n-->회(OK <!--n:budget.runs_ok-->52<!--/n-->, 실패 <!--n:budget.runs_err-->1<!--/n-->). 실패한 1회(q1 ablated, 요청 1건이 ClientOSError)는 결과에서 빼고 같은 조건을 다시 돌렸다. 서버 기동 <!--n:budget.startup_s-->1,571<!--/n--> s + 재생 <!--n:budget.replay_s-->17,430<!--/n--> s = <!--n:budget.gpu_hours-->5.28<!--/n--> GPU-h. 교차 재생 2회(약 18분)는 별도 |
| 발사 지연 | open-loop 재생에서 예정 도착 → 실제 제출 지연은 결과 파일 52개에서 최대 약 7 ms |

**절차.** 모든 시각은 UTC이고 실행 기록 [`data/ledger/w3_queue.log`](../data/ledger/w3_queue.log)에서 복원했다.

- **사전 계획.** 측정 전에 [사전 계획](preregistration/plan_2026-09-13.md)(09-13 13:59 작성)에 bar 구성, 지표, 선택 규칙을 고정했다. 첫 측정은 15:03에 시작했다.
- **실행 순서.**
  1. 순정 7회(15:03–16:04)
  2. 패치 적용·스모크(16:04)
  3. P2 탐색 7회
  4. P3 파레토 6회
  5. α와 무관한 부하·손해·정확성 실행 12회
  6. P4 최종 r2·r3 6회(18:33–19:05)
  7. mine·N=40 쪽 15회(–20:26)
  8. 교차 재생 default·mine(20:26–20:44)
- **q2 3-bar의 반복.** r1은 P2 탐색 실행(16:04–16:43)을 재사용했고, r2·r3은 새로 돌렸다. 회차 안 위치는 default 1·3·2, mine 2·2·1, ablated 3·1·3번째로 교대했지만 완전한 균형은 아니다. 반복 간 goodput 범위가 default <!--n:q2.default.goodput_spread_pct-->0.24<!--/n-->%, ablated <!--n:q2.ablated.goodput_spread_pct-->1.24<!--/n-->%, mine <!--n:q2.mine.goodput_spread_pct-->0.05<!--/n-->%라 순서의 영향은 작다.
- **계획과 달라진 점.** 사전 계획 끝의 'Changes after execution'에 정리했다.

### 4.2 3-bar 결과 (q2)

**표 16. 3-bar 결과 (q2. ★ = 3회 중앙값 [최소–최대], 참고 행은 n=1)**

| bar | TTFT p50 (s) | TTFT p99 (s) | TPOT p50 (ms) | maxITL p99 (s) | cache hit (%) | SLO 충족 | wall (s) | goodput (req/s) |
|---|---|---|---|---|---|---|---|---|
| default★ | <!--n:q2.default.ttft_p50_s-->0.06<!--/n--> | <!--n:q2.default.ttft_p99_s-->64.2<!--/n--> [<!--n:q2.default.ttft_p99_range_s-->64.1–64.3<!--/n-->] | <!--n:q2.default.tpot_p50_ms-->24.0<!--/n--> | <!--n:q2.default.maxitl_p99_s-->63.1<!--/n--> [<!--n:q2.default.maxitl_p99_range_s-->62.8–63.1<!--/n-->] | <!--n:q2.default.cache_hit_pct-->11.5<!--/n--> | <!--n:q2.default.slo_ratio-->115/120<!--/n--> | <!--n:q2.default.wall_s-->285.2<!--/n--> | <!--n:q2.default.goodput-->0.403<!--/n--> [<!--n:q2.default.goodput_range-->0.4025–0.4035<!--/n-->] |
| ablated★ | <!--n:q2.ablated.ttft_p50_s-->0.06<!--/n--> | <!--n:q2.ablated.ttft_p99_s-->64.5<!--/n--> [<!--n:q2.ablated.ttft_p99_range_s-->63.6–65.0<!--/n-->] | <!--n:q2.ablated.tpot_p50_ms-->24.0<!--/n--> | <!--n:q2.ablated.maxitl_p99_s-->63.2<!--/n--> [<!--n:q2.ablated.maxitl_p99_range_s-->62.3–63.6<!--/n-->] | <!--n:q2.ablated.cache_hit_pct-->11.5<!--/n--> | <!--n:q2.ablated.slo_ratio-->116/120<!--/n--> | <!--n:q2.ablated.wall_s-->285.3<!--/n--> | <!--n:q2.ablated.goodput-->0.407<!--/n--> [<!--n:q2.ablated.goodput_range-->0.4016–0.4067<!--/n-->] |
| **mine (α=1.0)★** | <!--n:q2.mine.ttft_p50_s-->8.1<!--/n--> [<!--n:q2.mine.ttft_p50_min-->6.1<!--/n-->–<!--n:q2.mine.ttft_p50_max-->8.1<!--/n-->]† | <!--n:q2.mine.ttft_p99_s-->92.3<!--/n--> [<!--n:q2.mine.ttft_p99_range_s-->92.2–92.4<!--/n-->] | <!--n:q2.mine.tpot_p50_ms-->23.0<!--/n--> | <!--n:q2.mine.maxitl_p99_s-->0.11<!--/n--> [<!--n:q2.mine.maxitl_p99_range_s-->0.11–0.11<!--/n-->] | <!--n:q2.mine.cache_hit_pct-->11.5<!--/n--> | <!--n:q2.mine.slo_ratio-->120/120<!--/n--> | <!--n:q2.mine.wall_s-->272.8<!--/n--> | <!--n:q2.mine.goodput-->0.440<!--/n--> [<!--n:q2.mine.goodput_range-->0.4396–0.4399<!--/n-->] |
| (참고, n=1) tuned N=40 | <!--n:q2.tuned_n40.ttft_p50_s-->28.9<!--/n--> | <!--n:q2.tuned_n40.ttft_p99_s-->93.5<!--/n--> | <!--n:q2.tuned_n40.tpot_p50_ms-->22.3<!--/n--> | <!--n:q2.tuned_n40.maxitl_p99_s-->0.09<!--/n--> | <!--n:q2.tuned_n40.cache_hit_pct-->11.5<!--/n--> | <!--n:q2.tuned_n40.slo_ratio-->120/120<!--/n--> | <!--n:q2.tuned_n40.wall_s-->277.9<!--/n--> | <!--n:q2.tuned_n40.goodput-->0.432<!--/n--> |
| (참고, n=1) noradix | <!--n:q2.noradix.ttft_p50_s-->16.8<!--/n--> | <!--n:q2.noradix.ttft_p99_s-->89.9<!--/n--> | <!--n:q2.noradix.tpot_p50_ms-->23.3<!--/n--> | <!--n:q2.noradix.maxitl_p99_s-->0.12<!--/n--> | <!--n:q2.noradix.cache_hit_pct-->0.0<!--/n--> | <!--n:q2.noradix.slo_ratio-->120/120<!--/n--> | <!--n:q2.noradix.wall_s-->275.6<!--/n--> | <!--n:q2.noradix.goodput-->0.435<!--/n--> |
| (참고, n=1) upstream (순정) | <!--n:q2.upstream.ttft_p50_s-->0.06<!--/n--> | <!--n:q2.upstream.ttft_p99_s-->64.3<!--/n--> | <!--n:q2.upstream.tpot_p50_ms-->24.0<!--/n--> | <!--n:q2.upstream.maxitl_p99_s-->63.2<!--/n--> | <!--n:q2.upstream.cache_hit_pct-->11.5<!--/n--> | <!--n:q2.upstream.slo_ratio-->115/120<!--/n--> | <!--n:q2.upstream.wall_s-->285.7<!--/n--> | <!--n:q2.upstream.goodput-->0.403<!--/n--> |

† mine의 TTFT는 봉우리가 둘이라 p50이 대표값이 아니다(본문과 그림 5).

**표 17. 보조 지표 (q2. ★ = 3회 중앙값, 참고 행은 n=1)**

| bar | 선점 | 5 s 넘는 정지 | 10 s 넘게 대기 | TTFT 평균 (s) | TPOT p99 (ms) | 보조 SLO 충족 (%) | 입장 지연 (#delays) | decode graph 사용 (%) | 최대 동시 |
|---|---|---|---|---|---|---|---|---|---|
| default★ | <!--n:q2.default.retractions-->19<!--/n--> [<!--n:q2.default.retractions_min-->19<!--/n-->–<!--n:q2.default.retractions_max-->20<!--/n-->] | <!--n:q2.default.stall5-->19<!--/n--> [<!--n:q2.default.stall5_range-->19–20<!--/n-->] | <!--n:q2.default.wait10-->43<!--/n--> | <!--n:q2.default.ttft_mean_s-->19.0<!--/n--> | <!--n:q2.default.tpot_p99_ms-->93.2<!--/n--> | <!--n:q2.default.aux_slo_pct-->84.2<!--/n--> [<!--n:q2.default.aux_slo_range_pct-->83.3–84.2<!--/n-->] | <!--n:q2.default.peak_delays-->0<!--/n--> | <!--n:q2.default.graph_pct-->55.3<!--/n--> | 68 |
| ablated★ | <!--n:q2.ablated.retractions-->21<!--/n--> [<!--n:q2.ablated.retractions_min-->20<!--/n-->–<!--n:q2.ablated.retractions_max-->21<!--/n-->] | <!--n:q2.ablated.stall5-->21<!--/n--> | <!--n:q2.ablated.wait10-->43<!--/n--> | <!--n:q2.ablated.ttft_mean_s-->19.1<!--/n--> | <!--n:q2.ablated.tpot_p99_ms-->92.9<!--/n--> | <!--n:q2.ablated.aux_slo_pct-->82.5<!--/n--> | <!--n:q2.ablated.peak_delays-->0<!--/n--> | <!--n:q2.ablated.graph_pct-->55.3<!--/n--> | 68 |
| **mine★** | <!--n:q2.mine.retractions-->0<!--/n--> | <!--n:q2.mine.stall5-->0<!--/n--> | <!--n:q2.mine.wait10-->60<!--/n--> | <!--n:q2.mine.ttft_mean_s-->30.1<!--/n--> | <!--n:q2.mine.tpot_p99_ms-->24.4<!--/n--> | <!--n:q2.mine.aux_slo_pct-->100.0<!--/n--> | ≥ <!--n:q2.mine.peak_delays-->24<!--/n--> | <!--n:q2.mine.graph_pct-->50.9<!--/n--> | 55 |
| tuned N=40 (n=1) | <!--n:q2.tuned_n40.retractions-->0<!--/n--> | <!--n:q2.tuned_n40.stall5-->0<!--/n--> | <!--n:q2.tuned_n40.wait10-->71<!--/n--> | <!--n:q2.tuned_n40.ttft_mean_s-->35.2<!--/n--> | 24.3 | 100.0 | — | <!--n:q2.tuned_n40.graph_pct-->48.7<!--/n--> | 40 |
| noradix (n=1) | <!--n:q2.noradix.retractions-->0<!--/n--> | <!--n:q2.noradix.stall5-->0<!--/n--> | <!--n:q2.noradix.wait10-->60<!--/n--> | <!--n:q2.noradix.ttft_mean_s-->30.3<!--/n--> | 25.0 | 100.0 | — | <!--n:q2.noradix.graph_pct-->49.7<!--/n--> | 54 |

**표 18. 3-bar 해석 (goodput 기준)**

| 항목 | 계산 | 값 | 의미 |
|---|---|---|---|
| 기존 기능의 이득 | Δ기존 = default − ablated | ablated <!--n:q2.ablated.goodput-->0.407<!--/n--> [<!--n:q2.ablated.goodput_range-->0.4016–0.4067<!--/n-->] ≥ default <!--n:q2.default.goodput-->0.403<!--/n--> [<!--n:q2.default.goodput_range-->0.4025–0.4035<!--/n-->]. 범위가 겹쳐 0에 가깝다 | chunked prefill은 W3에서 하는 일이 없다(입력 387토큰 < 2,048) |
| 내 기여 | Δ내것 = mine ÷ default − 1 | <!--n:q2.gain.goodput_pct-->+9.1<!--/n-->% (= SLO ×<!--n:q2.gain.slo_factor-->1.043<!--/n--> × wall ×<!--n:q2.gain.wall_factor-->1.045<!--/n-->) | 출처는 아래 분해 |
| 비율 | Δ내것 ÷ Δ기존 | 정의할 수 없다 | Δ기존이 반복 편차 안이다 |

**goodput 이득의 분해.** goodput = SLO 충족 수 ÷ wall이다. <!--n:q2.gain.goodput_pct-->+9.1<!--/n-->% = SLO 충족 <!--n:q2.default.slo_met-->115<!--/n--> → <!--n:q2.mine.slo_met-->120<!--/n-->건(×<!--n:q2.gain.slo_factor-->1.043<!--/n-->) × wall <!--n:q2.default.wall_s-->285.2<!--/n--> → <!--n:q2.mine.wall_s-->272.8<!--/n--> s(×<!--n:q2.gain.wall_factor-->1.045<!--/n-->)다. 두 항 모두 선점 제거에서 나온다.

- **SLO 항.** default에서 평균 TPOT SLO를 어긴 5건은 모두 선점 피해 요청이고, mine에서는 선점이 없어 모두 통과한다.
- **wall 항.** 한 요청 때문이다. 마지막으로 끝나는 <!--n:q2.longest.rid-->reason-00064<!--/n-->(<!--n:q2.longest.out_tokens-->10,541<!--/n-->토큰)는 default에서 3회 모두 선점되어 <!--n:q2.longest.default_stall_range_s-->61.6–61.8<!--/n--> s 멈췄고, 3회 모두 마지막으로 끝났다(<!--n:q2.longest.last_to_finish_default-->3/3<!--/n-->). mine에서는 3회 모두 도착 후 약 <!--n:q2.longest.mine_ttft_s-->32.1<!--/n--> s를 기다려 입장한 뒤 한 번도 멈추지 않았다.
- **그 한 요청을 빼면.** goodput은 <!--n:q2.excl_longest.default_goodput-->0.420<!--/n--> → <!--n:q2.excl_longest.mine_goodput-->0.439<!--/n-->(<!--n:q2.excl_longest.gain_pct-->+4.4<!--/n-->%, 3회 중앙값)이다.

따라서 goodput 이득은 처리 속도가 빨라진 결과가 아니다. 선점 제거가 가장 긴 요청 하나와, TPOT가 60 ms를 넘던 피해 요청들을 구한 효과다.

**out_tok/s는 처리 속도가 아니다.** out_tok/s <!--n:q2.default.out_tok_s-->1,183.3<!--/n--> → <!--n:q2.mine.out_tok_s-->1,236.7<!--/n-->(<!--n:q2.gain.out_tok_s_pct-->+4.5<!--/n-->%)은 처리 속도가 빨라진 것이 아니다. 120건의 출력 합이 <!--n:trace.out_total-->337,433<!--/n-->토큰으로 고정이라 out_tok/s = <!--n:trace.out_total-->337,433<!--/n--> ÷ wall이고, <!--n:q2.gain.out_tok_s_pct-->+4.5<!--/n-->%는 wall이 준 것과 같은 값이다. 누적 출력 도달 시각(3회 중앙값)을 보면 차이가 드러난다(그림 4).

- 50% 지점: <!--n:q2.cum.default_t50_s-->96.3<!--/n--> → <!--n:q2.cum.mine_t50_s-->101.9<!--/n--> s. mine이 <!--n:q2.cum.t50_mine_minus_default_s-->5.6<!--/n--> s 늦다.
- 90% 지점: <!--n:q2.cum.default_t90_s-->192.7<!--/n--> → <!--n:q2.cum.mine_t90_s-->194.9<!--/n--> s. mine이 <!--n:q2.cum.t90_mine_minus_default_s-->2.2<!--/n--> s 늦다.
- 99% 지점: <!--n:q2.cum.default_t99_s-->260.9<!--/n--> → <!--n:q2.cum.mine_t99_s-->253.6<!--/n--> s. mine은 이 지점 이후의 꼬리에서만 앞선다.

![q2 goodput 이득의 분해](figures/fig05_goodput_decomposition.png)

*그림 4. q2 goodput 이득의 분해(3회 중앙값). 왼쪽: 누적 출력 도달 시각. mine이 앞서는 것은 마지막 꼬리뿐이다. 오른쪽: goodput 이득 = SLO 충족 배율 × wall 배율, <!--n:q2.longest.rid-->reason-00064<!--/n-->를 뺀 이득, out_tok/s 변화.*

**대가는 TTFT로 옮겨갔다.** goodput 밖의 지표가 대가를 보여 준다. 선점과 5 s 넘는 정지가 0이 되어 maxITL p99는 <!--n:q2.default.maxitl_p99_s-->63.1<!--/n--> s에서 <!--n:q2.mine.maxitl_p99_s-->0.11<!--/n--> s로 줄었다. 그 대신 다음이 나빠졌다.

- 10 s 넘게 입장을 기다린 요청: <!--n:q2.default.wait10-->43<!--/n--> → <!--n:q2.mine.wait10-->60<!--/n-->건.
- TTFT 평균: <!--n:q2.default.ttft_mean_s-->19.0<!--/n--> → <!--n:q2.mine.ttft_mean_s-->30.1<!--/n--> s(<!--n:q2.gain.ttft_mean_pct-->+58.4<!--/n-->%).
- TTFT p99: <!--n:q2.default.ttft_p99_s-->64.2<!--/n--> → <!--n:q2.mine.ttft_p99_s-->92.3<!--/n--> s(<!--n:q2.gain.ttft_p99_pct-->+43.6<!--/n-->%).

mine의 TTFT는 봉우리가 둘이다(그림 5). <!--n:q2.ttft_bimodal.mine_lt1s-->60<!--/n-->건은 1 s 안에 입장했고 <!--n:q2.ttft_bimodal.mine_ge12s-->60<!--/n-->건은 12 s 이상 기다렸으며, 1–10 s 구간은 0건이다. 그래서 p50은 60번째 값(약 0.07 s)과 61번째 값(<!--n:q2.ttft_bimodal.mine_61st_range_s-->12.2–16.1<!--/n--> s) 사이의 보간이 되어, 반복마다 <!--n:q2.mine.ttft_p50_min-->6.1<!--/n-->–<!--n:q2.mine.ttft_p50_max-->8.1<!--/n--> s로 흔들린다. 대표값으로는 TTFT 평균, 10 s 넘는 대기 수, 1 s 안 입장 수(default <!--n:q2.ttft_bimodal.default_lt1s-->77<!--/n--> → mine <!--n:q2.ttft_bimodal.mine_lt1s-->60<!--/n-->건)를 함께 본다.

default에서도 이미 <!--n:q2.default.wait10-->43<!--/n-->건이 10 s 넘게 기다렸다. 선점 직후에는 풀이 차 있고 `new_token_ratio`가 튀어 새 입장이 막히기 때문이다. mine은 대기를 새로 만든 것이 아니라 17건 늘렸다. 멈춤이 사라진 것이 아니라, 생성 도중에서 생성 시작 전으로 옮겨간 것이다. E2E는 p50 <!--n:q2.default.e2e_p50_s-->86.8<!--/n--> → <!--n:q2.mine.e2e_p50_s-->91.2<!--/n--> s(<!--n:q2.gain.e2e_p50_pct-->+5.0<!--/n-->%), 평균 <!--n:q2.default.e2e_mean_s-->92.0<!--/n--> → <!--n:q2.mine.e2e_mean_s-->92.3<!--/n--> s(<!--n:q2.gain.e2e_mean_pct-->+0.3<!--/n-->%), p99 <!--n:q2.default.e2e_p99_s-->240.8<!--/n--> → <!--n:q2.mine.e2e_p99_s-->227.3<!--/n--> s(<!--n:q2.gain.e2e_p99_pct-->−5.6<!--/n-->%)다.

![q2 TTFT 분포](figures/fig06_ttft_cdf.png)

*그림 5. q2의 TTFT 누적 분포. default와 mine은 r2이고, noradix와 N=40은 n=1이다. mine은 즉시 입장한 무리와 12 s 넘게 기다린 무리로 나뉜다.*

**기존 플래그와의 비교.** SGLang 기존 기능만으로도 이득의 대부분이 재현된다.

- `--disable-radix-cache`(noradix, n=1)가 default → mine goodput 이득의 <!--n:q2.share_of_gain.noradix_pct-->88.0<!--/n-->%를 낸다.
- `--max-running-requests 40`(n=1)이 <!--n:q2.share_of_gain.tuned_n40_pct-->78.1<!--/n-->%를 낸다.

이 패치의 기여는, radix cache를 끄면 쓰이는 기존 정확 길이 경로(`add_one_req_ignore_eos`)의 미래 KV 시뮬레이션을 radix cache를 켠 채로도 쓰게 한 것이다(+<!--n:patch.lines_added-->102<!--/n-->줄). W3는 공유 prefix가 <!--n:trace.shared_prefix_tokens-->36<!--/n-->토큰뿐이라 radix 유지의 이득은 작다(noradix cache hit <!--n:q2.noradix.cache_hit_pct-->0.0<!--/n-->% 대 <!--n:q2.default.cache_hit_pct-->11.5<!--/n-->%).

q2에서 mine <!--n:q2.mine.goodput-->0.440<!--/n-->(3회)과 N=40 <!--n:q2.tuned_n40.goodput-->0.432<!--/n-->·noradix <!--n:q2.noradix.goodput-->0.435<!--/n-->(각 n=1)의 차이는 우열의 근거가 되지 않는다. 세 설정 모두 SLO <!--n:q2.mine.slo_ratio-->120/120<!--/n-->을 지키므로 차이가 전부 wall(마지막 요청의 완료 시각)에서 나오고, 비교 상대는 1회 실행이다. N=40 대비 데이터가 지지하는 우위는 goodput이 아니라 두 가지다. 부하·길이 분포가 바뀌어도 선점이 다시 생기지 않고, 압박 조건에서 TTFT 평균이 더 짧다(5.2, 표 25).

**α*·N*의 선택 규칙.** 규칙은 결과를 보기 전에 사전 계획 4.3에 고정했다. 보조 SLO '요청 안 최대 정지 ≤ 5 s'(5 s 넘는 정지 0건)를 만족하는 설정 중 TTFT p99가 가장 작은 것을 고른다. 그 결과 α* = <!--n:select.alpha_star-->1.0<!--/n-->, N* = <!--n:select.n_star-->40<!--/n-->이다(`python3 -m w3.select_star`, 표 23).

- q2 보조 SLO 충족률은 default <!--n:q2.default.aux_slo_range_pct-->83.3–84.2<!--/n-->% → mine <!--n:q2.mine.aux_slo_pct-->100.0<!--/n-->%다.
- default에서 5 s 넘게 멈춘 <!--n:q2.default.stall5_range-->19–20<!--/n-->건 중 <!--n:q2.default.stalled_tpot_ok_range-->14–15<!--/n-->건은 평균 TPOT SLO(60 ms)를 통과했다. 평균 TPOT SLO가 긴 정지를 잡지 못하는 맹점이다.
- 이 보조 지표는 정지를 없애는 mine에 유리하게 정의되어 있다.

**교란 요인.**

- **CUDA graph.** 4090 기본 설정에서 decode CUDA graph는 배치 ≤ 24에서만 쓰인다. mine의 이득은 graph 덕이 아니다. decode 줄 중 graph를 쓴 비율은 default <!--n:q2.default.graph_pct-->55.3<!--/n-->%, mine <!--n:q2.mine.graph_pct-->50.9<!--/n-->%로 mine이 오히려 낮다. 반대로 동시 제한 N=24는 graph <!--n:q2.tuned_n24.graph_pct-->100.0<!--/n-->%(TPOT p50 <!--n:q2.tuned_n24.tpot_p50_ms-->18.1<!--/n--> ms)라, 그 TPOT 개선의 일부는 graph 효과다.
- **ablated.** `--chunked-prefill-size -1`은 chunked prefill과 함께 prefill CUDA graph도 끈다(4.1). W3 프롬프트는 2,048토큰보다 짧아 chunked prefill이 하는 일이 없고, ablated가 default 이상이므로 기존 기능의 이득(Δ기존)은 0에 가깝다.

### 4.3 부하 곡선

**표 19. 부하별 3-bar (09-13. q2만 3회 중앙값★, 그 밖은 bar마다 n=1)**

| QPS | bar | goodput | SLO 충족 | 선점 | TTFT p50 (s) | TTFT 평균 (s) | TTFT p99 (s) | 10 s 넘게 대기 | wall (s) |
|---|---|---|---|---|---|---|---|---|---|
| 0.35 | default (순정 3회★) | <!--n:load.q0.35.default.goodput-->0.305<!--/n--> | 120/120 | <!--n:baseline.q035.upstream.retractions-->0<!--/n--> | <!--n:baseline.q035.upstream.ttft_p50_s-->0.05<!--/n--> | 0.05 | <!--n:baseline.q035.upstream.ttft_p99_s-->0.07<!--/n--> | 0 | <!--n:baseline.q035.upstream.wall_s-->393.2<!--/n--> |
| 0.35 | ablated | <!--n:load.q0.35.ablated.goodput-->0.305<!--/n--> | 120/120 | 0 | 0.05 | 0.05 | 0.07 | 0 | 393.2 |
| 0.35 | mine | <!--n:load.q0.35.mine.goodput-->0.305<!--/n--> | 120/120 | 0 | 0.05 | 0.05 | 0.07 | 0 | 393.2 |
| 0.5 | default | <!--n:load.q0.5.default.goodput-->0.365<!--/n--> | <!--n:load.q0.5.slo_met.default-->120<!--/n-->/120 | <!--n:load.q0.5.retractions.default-->0<!--/n--> | <!--n:load.q0.5.ttft_p50_s.default-->0.05<!--/n--> | <!--n:load.q0.5.ttft_mean_s.default-->0.05<!--/n--> | <!--n:load.q0.5.ttft_p99_s.default-->0.08<!--/n--> | <!--n:load.q0.5.wait10.default-->0<!--/n--> | <!--n:load.q0.5.wall_s.default-->329.2<!--/n--> |
| 0.5 | ablated | <!--n:load.q0.5.ablated.goodput-->0.364<!--/n--> | <!--n:load.q0.5.slo_met.ablated-->120<!--/n-->/120 | <!--n:load.q0.5.retractions.ablated-->0<!--/n--> | <!--n:load.q0.5.ttft_p50_s.ablated-->0.05<!--/n--> | <!--n:load.q0.5.ttft_mean_s.ablated-->0.05<!--/n--> | <!--n:load.q0.5.ttft_p99_s.ablated-->0.07<!--/n--> | <!--n:load.q0.5.wait10.ablated-->0<!--/n--> | <!--n:load.q0.5.wall_s.ablated-->329.3<!--/n--> |
| 0.5 | mine | <!--n:load.q0.5.mine.goodput-->0.364<!--/n--> | <!--n:load.q0.5.slo_met.mine-->120<!--/n-->/120 | <!--n:load.q0.5.retractions.mine-->0<!--/n--> | <!--n:load.q0.5.ttft_p50_s.mine-->0.05<!--/n--> | <!--n:load.q0.5.ttft_mean_s.mine-->0.05<!--/n--> | <!--n:load.q0.5.ttft_p99_s.mine-->0.08<!--/n--> | <!--n:load.q0.5.wait10.mine-->0<!--/n--> | <!--n:load.q0.5.wall_s.mine-->329.3<!--/n--> |
| 1 | default | <!--n:load.q1.default.goodput-->0.425<!--/n--> | <!--n:load.q1.slo_met.default-->120<!--/n-->/120 | <!--n:load.q1.retractions.default-->7<!--/n--> | <!--n:load.q1.ttft_p50_s.default-->0.06<!--/n--> | <!--n:load.q1.ttft_mean_s.default-->13.7<!--/n--> | <!--n:load.q1.ttft_p99_s.default-->46.2<!--/n--> | <!--n:load.q1.wait10.default-->46<!--/n--> | <!--n:load.q1.wall_s.default-->282.4<!--/n--> |
| 1 | ablated | <!--n:load.q1.ablated.goodput-->0.423<!--/n--> | <!--n:load.q1.slo_met.ablated-->120<!--/n-->/120 | <!--n:load.q1.retractions.ablated-->6<!--/n--> | <!--n:load.q1.ttft_p50_s.ablated-->0.06<!--/n--> | <!--n:load.q1.ttft_mean_s.ablated-->13.7<!--/n--> | <!--n:load.q1.ttft_p99_s.ablated-->45.8<!--/n--> | <!--n:load.q1.wait10.ablated-->46<!--/n--> | <!--n:load.q1.wall_s.ablated-->283.8<!--/n--> |
| 1 | mine | <!--n:load.q1.mine.goodput-->0.422<!--/n--> | <!--n:load.q1.slo_met.mine-->120<!--/n-->/120 | <!--n:load.q1.retractions.mine-->0<!--/n--> | <!--n:load.q1.ttft_p50_s.mine-->0.06<!--/n--> | <!--n:load.q1.ttft_mean_s.mine-->16.2<!--/n--> | <!--n:load.q1.ttft_p99_s.mine-->53.5<!--/n--> | <!--n:load.q1.wait10.mine-->50<!--/n--> | <!--n:load.q1.wall_s.mine-->284.3<!--/n--> |
| 2 | default★ | <!--n:q2.default.goodput-->0.403<!--/n--> | <!--n:q2.default.slo_ratio-->115/120<!--/n--> | <!--n:q2.default.retractions-->19<!--/n--> | <!--n:q2.default.ttft_p50_s-->0.06<!--/n--> | <!--n:q2.default.ttft_mean_s-->19.0<!--/n--> | <!--n:q2.default.ttft_p99_s-->64.2<!--/n--> | <!--n:q2.default.wait10-->43<!--/n--> | <!--n:q2.default.wall_s-->285.2<!--/n--> |
| 2 | ablated★ | <!--n:q2.ablated.goodput-->0.407<!--/n--> | <!--n:q2.ablated.slo_ratio-->116/120<!--/n--> | <!--n:q2.ablated.retractions-->21<!--/n--> | <!--n:q2.ablated.ttft_p50_s-->0.06<!--/n--> | <!--n:q2.ablated.ttft_mean_s-->19.1<!--/n--> | <!--n:q2.ablated.ttft_p99_s-->64.5<!--/n--> | <!--n:q2.ablated.wait10-->43<!--/n--> | <!--n:q2.ablated.wall_s-->285.3<!--/n--> |
| 2 | mine★ | <!--n:q2.mine.goodput-->0.440<!--/n--> | <!--n:q2.mine.slo_ratio-->120/120<!--/n--> | <!--n:q2.mine.retractions-->0<!--/n--> | <!--n:q2.mine.ttft_p50_s-->8.1<!--/n--> | <!--n:q2.mine.ttft_mean_s-->30.1<!--/n--> | <!--n:q2.mine.ttft_p99_s-->92.3<!--/n--> | <!--n:q2.mine.wait10-->60<!--/n--> | <!--n:q2.mine.wall_s-->272.8<!--/n--> |
| 4 | default | <!--n:load.q4.default.goodput-->0.378<!--/n--> | <!--n:load.q4.slo_met.default-->108<!--/n-->/120 | <!--n:load.q4.retractions.default-->29<!--/n--> | <!--n:load.q4.ttft_p50_s.default-->0.06<!--/n--> | <!--n:load.q4.ttft_mean_s.default-->16.3<!--/n--> | <!--n:load.q4.ttft_p99_s.default-->75.6<!--/n--> | <!--n:load.q4.wait10.default-->34<!--/n--> | <!--n:load.q4.wall_s.default-->285.5<!--/n--> |
| 4 | ablated | <!--n:load.q4.ablated.goodput-->0.379<!--/n--> | <!--n:load.q4.slo_met.ablated-->108<!--/n-->/120 | <!--n:load.q4.retractions.ablated-->29<!--/n--> | <!--n:load.q4.ttft_p50_s.ablated-->0.06<!--/n--> | <!--n:load.q4.ttft_mean_s.ablated-->16.3<!--/n--> | <!--n:load.q4.ttft_p99_s.ablated-->79.2<!--/n--> | <!--n:load.q4.wait10.ablated-->34<!--/n--> | <!--n:load.q4.wall_s.ablated-->284.7<!--/n--> |
| 4 | mine | <!--n:load.q4.mine.goodput-->0.449<!--/n--> | <!--n:load.q4.slo_met.mine-->120<!--/n-->/120 | <!--n:load.q4.retractions.mine-->0<!--/n--> | <!--n:load.q4.ttft_p50_s.mine-->8.8<!--/n--> | <!--n:load.q4.ttft_mean_s.mine-->35.4<!--/n--> | <!--n:load.q4.ttft_p99_s.mine-->89.2<!--/n--> | <!--n:load.q4.wait10.mine-->60<!--/n--> | <!--n:load.q4.wall_s.mine-->267.2<!--/n--> |

qps 0.35에는 플래그 OFF 실행이 없어, default 칸은 09-13 순정 3회 중앙값이다. 이 표의 수치는 q2를 빼면 모두 bar마다 1회 측정이다.

- **선점이 없는 부하.** qps 0.35와 q0.5에서는 세 bar가 같다. 게이트가 한 번도 발동하지 않았다.
- **q1 (n=1).** mine은 선점을 <!--n:load.q1.retractions.default-->7<!--/n--> → <!--n:load.q1.retractions.mine-->0<!--/n-->회로 없앴지만, goodput은 <!--n:load.q1.gain_pct-->−0.7<!--/n-->%이고 TTFT p99는 <!--n:load.q1.ttft_p99_pct-->+15.9<!--/n-->% 늘었다.
- **q2·q4 (3회·n=1).** 선점 피해 요청의 SLO 위반이 사라져 goodput이 <!--n:q2.gain.goodput_pct-->+9.1<!--/n-->%(3회), <!--n:load.q4.gain_pct-->+18.7<!--/n-->%(n=1) 늘었다. 대신 TTFT 평균이 각각 <!--n:q2.gain.ttft_mean_pct-->+58.4<!--/n-->%, <!--n:load.q4.ttft_mean_pct-->+117.3<!--/n-->% 늘었다.
- **q4의 분해.** q4의 <!--n:load.q4.gain_pct-->+18.7<!--/n-->%도 SLO <!--n:load.q4.slo_met.default-->108<!--/n--> → <!--n:load.q4.slo_met.mine-->120<!--/n-->건(×<!--n:load.q4.slo_factor-->1.111<!--/n-->)과 wall <!--n:load.q4.wall_s.default-->285.5<!--/n--> → <!--n:load.q4.wall_s.mine-->267.2<!--/n--> s(×<!--n:load.q4.wall_factor-->1.068<!--/n-->)의 곱이다. <!--n:q2.longest.rid-->reason-00064<!--/n-->를 빼면 <!--n:load.q4.excl_longest_gain_pct-->+12.5<!--/n-->%다.

![부하 곡선](figures/fig03_load_curve.png)

*그림 6. 부하(QPS)에 따른 goodput. default / ablated / mine과 `--max-running-requests 40`을 비교했다. q2의 default·ablated·mine만 3회 중앙값이고, 나머지 점은 n=1이다. TTFT는 표 19를 본다.*

### 4.4 출력 길이 구간별 분해 (q2, default r1 → mine r1, n=1)

**표 20. 출력 길이 구간별 분해 (q2, r1 한 쌍)**

| 출력 길이 | 건수 | TTFT p99 (s) | TPOT p50 (ms) | E2E p99 (s) | SLO 충족 (%) |
|---|---|---|---|---|---|
| < 1k | 9 | 64.0 → 65.3 | 25.1 → 23.5 | 85.5 → 84.6 | 77.8 → 100.0 |
| 1k – 3k | 67 | 63.6 → 91.9 | 24.2 → 23.0 | 129.9 → 145.7 | 95.5 → 100.0 |
| 3k – 8k | 41 | 64.1 → 91.5 | 23.8 → 22.9 | 221.4 → 218.5 | 100.0 → 100.0 |
| ≥ 8k | 3 | 61.2 → 63.4 | 24.7 → 20.4 | 260.1 → 247.2 | 100.0 → 100.0 |

이 표는 [`data/derived/per_request.csv.gz`](../data/derived/per_request.csv.gz)에서 다시 계산했다(type-7 백분위). 평균이 감춘 것은 누가 피해를 떠안는가다.

- **SLO 위반은 짧은 요청에 몰린다.** default에서 1k 미만 9건의 SLO 충족률은 77.8%, 1k–3k는 95.5%이고, 3k 이상은 100%다. 같은 60 s 정지라도 토큰 수가 적은 요청은 평균 TPOT가 60 ms를 넘기 때문이다. mine은 모든 구간에서 100%다.
- **대가도 짧은 요청이 먼저 치른다.** 1k 미만의 TTFT p50이 <!--n:q2.short_ttft_p50.default_s-->0.06<!--/n--> → <!--n:q2.short_ttft_p50.mine_s-->31.6<!--/n--> s로 늘었다(3회 중앙값). 대기열이 FCFS라, 앞선 긴 요청의 예약이 풀리길 짧은 요청도 기다린다(head-of-line). 그래도 도중에 멈추지 않으니 1k 미만의 E2E p50은 77.9 → 49.3 s(r1)로 오히려 줄었다.
- **손해는 중간 길이의 꼬리에 남는다.** 1k–3k의 TTFT p99는 63.6 → 91.9 s, E2E p99는 129.9 → 145.7 s다.
- **TPOT.** 중앙값이 모든 구간에서 0.9–4.3 ms 낮다(배치가 작아진 효과).
- **반복 간 차이.** r2·r3 쌍에서도 방향은 대체로 같다. 다만 3k–8k의 E2E p99는 r2·r3에서 202 → 219 s로 반대 방향이므로, 이 칸은 한 쌍만으로 읽지 않는다.

### 4.5 정확성 검증

판정은 모두 결정론 모드(`--enable-deterministic-inference --attention-backend triton`) 데이터셋에서 했고, 95% 기준은 그대로다. `bench.verify` 출력 원문은 [`data/verify/`](../data/verify/)에 있다.

**표 21. 정확성 비교 (결정론 모드, 각 실행 n=1)**

| 비교 | 완전 일치 | 불일치 중 선점 피해 요청 | 판정 (95%) |
|---|---|---|---|
| upstream q2 r1 ↔ r2 (노이즈 기준선) | <!--n:correct.det_q2up_r1_vs_r2-->115/120<!--/n--> | <!--n:correct.det_q2up_r1_vs_r2_mismatches_in_victims-->5/5<!--/n--> | PASS |
| upstream q0.2(선점 0) ↔ upstream q2 (선점의 효과) | <!--n:correct.det_q02up_vs_q2up-->98/120<!--/n--> | 22/22 | FAIL |
| upstream q0.2 ↔ mine α=1.0 q2 (플래그 ON) | <!--n:correct.det_q02up_vs_q2mine-->120/120<!--/n--> | — | PASS |
| upstream q2 ↔ mine α=1.0 q2 (플래그 ON, 같은 부하) | <!--n:correct.det_q2up_vs_q2mine-->98/120<!--/n--> | <!--n:correct.mismatches_in_victims-->22/22<!--/n--> | FAIL |
| upstream q0.2 ↔ 플래그 OFF q0.2 (OFF = 순정) | <!--n:correct.det_q02up_vs_q02off-->120/120<!--/n--> | — | PASS |
| upstream q2 ↔ 플래그 OFF q2 | <!--n:correct.det_q2up_vs_q2off-->106/120<!--/n--> | 14/14 | FAIL |
| (참고, 일반 모드) default q2 r1 ↔ r2 | <!--n:correct.nondet_q2default_r1_vs_r2-->20/120<!--/n--> | — | FAIL |
| (참고, 일반 모드) mine q2 r1 ↔ r2 | <!--n:correct.nondet_q2mine_r1_vs_r2-->85/120<!--/n--> | — | FAIL |

'선점 피해 요청'은 어느 한쪽 실행에서 스트림이 5 s 넘게 멈춘 요청이다. 결정론 비교의 불일치 가운데 앞 32자 안에서 갈라진 것은 없다.

- **노이즈 기준선.** 순정끼리 같은 q2를 두 번 돌려도 겨우 통과한다(<!--n:correct.det_q2up_r1_vs_r2-->115/120<!--/n-->). 불일치는 모두 선점 피해 요청이다.
- **선점의 효과.** 선점이 없는 q0.2 순정과 q2 순정을 비교하면 FAIL이다(<!--n:correct.det_q02up_vs_q2up-->98/120<!--/n-->). 그러나 불일치 22건은 모두 선점 피해 요청(<!--n:correct.det_q2up_victims-->30<!--/n-->건 중)이고, 피해를 입지 않은 90건은 배치 구성이 크게 달라도 100% 같다. 즉 결정론 모드는 배치 구성에 대해 불변이고, 출력을 바꾸는 것은 선점 뒤 재prefill로 다시 계산한 KV다.
- **플래그 OFF.** 선점 없는 q0.2에서 순정 ↔ OFF는 PASS다(<!--n:correct.det_q02up_vs_q02off-->120/120<!--/n-->). q2에서는 <!--n:correct.det_q2up_vs_q2off-->106/120<!--/n-->(FAIL)이지만 불일치 14건이 모두 선점 피해 요청이다. 이는 선점이 있는 조건의 재실행 편차이지 OFF 경로의 차이가 아니다.
- **플래그 ON.** 프로토콜 그대로의 비교(같은 부하 q2, 순정 ↔ mine)는 FAIL이다(<!--n:correct.det_q2up_vs_q2mine-->98/120<!--/n-->, <!--n:correct.det_q2up_vs_q2mine_pct-->81.7<!--/n-->%). 하지만 불일치 22건은 모두 순정 실행에서 선점된 피해 요청이고, mine 실행에는 선점이 없다. mine은 선점을 없애므로 기준은 선점 없는 순정(q0.2)이 맞고, 그 결과는 <!--n:correct.det_q02up_vs_q2mine-->120/120<!--/n--> PASS다. mine의 모든 요청이 순수 decode 출력과 바이트 단위로 같다.
- **결론.** 플래그 OFF는 순정과 같은 출력을 낸다. 플래그 ON은 요청의 출력을 바꾸지 않고, 선점이 만들던 출력 변화를 없앤다.

일반(비결정론) 실행끼리의 verify는 판정에 쓰지 않는다. 같은 빌드로 같은 q2를 다시 돌려도 default는 <!--n:correct.nondet_q2default_r1_vs_r2-->20/120<!--/n-->(<!--n:correct.nondet_q2default_r1_vs_r2_pct-->16.7<!--/n-->%)만 같아서 게이트로 쓸 수 없기 때문이다. mine끼리는 같은 요청이 더 많았다(<!--n:correct.nondet_q2mine_r1_vs_r2-->85/120<!--/n-->). 선점이 없으면 실행마다 배치 구성도 덜 흔들린다.

## 5. 트레이드오프 분석

### 5.1 무엇이 나빠졌는가

**표 22. 비용 항목 (q2는 3회 중앙값, q0.5는 n=1)**

| 비용 항목 | 측정값 | 비고 |
|---|---|---|
| 메모리 | 0. 새 자료구조 없이, 입장 시도마다 지역 리스트만 만든다. mine 서버 로그의 `max_total_num_tokens`는 <!--n:kv.pool_tokens-->87,552<!--/n-->로 default와 같다 | KV를 실제로 떼어 두지 않고 입장만 늦춘다 |
| CPU | `_peak_kv_fits` 1회 호출은 #running-req 8–128에서 약 12–153 µs다(`python3 tests/run_cpu_tests.py --bench`, 정정 작업 호스트). mine q2의 최대 동시 55 기준 약 60–70 µs로, decode step(TPOT p50 <!--n:q2.mine.tpot_p50_ms-->23.0<!--/n--> ms)의 약 0.3%다. 압박 없는 q0.5에서 TTFT p50은 <!--n:load.q0.5.ttft_p50_s.default-->0.05<!--/n--> → <!--n:load.q0.5.ttft_p50_s.mine-->0.05<!--/n--> s, TPOT p50은 20.5 → 20.5 ms로 같다 | 입장 시도가 있을 때(대기열이 있고 배치가 가득 차지 않았을 때)만 불리며 O(n log n)이다. 제출본의 '5–64 µs'는 원 측정 출력이 남아 있지 않아 다시 쟀다. 값은 CPU에 따라 다르다 |
| 특정 요청군 | 출력 1k 미만 <!--n:q2.short_ttft_p50.n_requests-->9<!--/n-->건의 TTFT p50 <!--n:q2.short_ttft_p50.default_s-->0.06<!--/n--> → <!--n:q2.short_ttft_p50.mine_s-->31.6<!--/n--> s. 같은 요청들의 E2E p50은 77.9 → 49.3 s로 오히려 줄었다(r1). 1k–3k의 TTFT p99 63.6 → 91.9 s, E2E p99 129.9 → 145.7 s(r1) | FCFS 대기열이라 앞선 긴 요청의 예약이 풀릴 때까지 짧은 요청도 못 들어온다(head-of-line). default에서는 짧은 요청이 선점 피해로 SLO를 어겼다(4.4) |
| 꼬리 지연 | TTFT p99 <!--n:q2.default.ttft_p99_s-->64.2<!--/n--> → <!--n:q2.mine.ttft_p99_s-->92.3<!--/n--> s(<!--n:q2.gain.ttft_p99_pct-->+43.6<!--/n-->%), TTFT 평균 <!--n:q2.default.ttft_mean_s-->19.0<!--/n--> → <!--n:q2.mine.ttft_mean_s-->30.1<!--/n--> s(<!--n:q2.gain.ttft_mean_pct-->+58.4<!--/n-->%), 10 s 넘게 대기 <!--n:q2.default.wait10-->43<!--/n--> → <!--n:q2.mine.wait10-->60<!--/n-->건. 반면 maxITL p99 <!--n:q2.default.maxitl_p99_s-->63.1<!--/n--> → <!--n:q2.mine.maxitl_p99_s-->0.11<!--/n--> s, E2E p99 <!--n:q2.default.e2e_p99_s-->240.8<!--/n--> → <!--n:q2.mine.e2e_p99_s-->227.3<!--/n--> s | 꼬리 지연이 '생성 중 정지'에서 '첫 토큰 전 대기'로 옮겨갔다. TTFT SLO가 있는 서비스라면 회귀다 |
| 정확성 / 품질 | 길이 근사 없음(trace의 `max_new_tokens`를 그대로 쓴다). 결정론 비교에서 선점 없는 순정 ↔ mine <!--n:correct.det_q02up_vs_q2mine-->120/120<!--/n--> PASS | 오히려 선점 뒤 재prefill이 만들던 출력 변화(피해 요청 <!--n:correct.det_q2up_victims-->30<!--/n-->건 중 22건)가 없어진다 |
| out_tok/s (q2) | <!--n:q2.default.out_tok_s-->1,183.3<!--/n--> → <!--n:q2.mine.out_tok_s-->1,236.7<!--/n--> (<!--n:q2.gain.out_tok_s_pct-->+4.5<!--/n-->%) | 처리 속도의 변화가 아니라 wall 단축을 다르게 표현한 값이다(4.2). 누적 출력 50% 도달은 mine이 <!--n:q2.cum.t50_mine_minus_default_s-->5.6<!--/n--> s 늦다 |

**표 23. 선점 ↔ 대기 교환비 (q2. ★ = 3회 중앙값, 그 밖은 설정마다 n=1)**

| 설정 | 선점 | 5 s 넘는 정지 | goodput | SLO 충족 | wall (s) | TTFT 평균 / p99 (s) | 10 s 넘게 대기 | maxITL p99 (s) | 입장 지연 (#delays) |
|---|---|---|---|---|---|---|---|---|---|
| default★ | <!--n:q2.default.retractions-->19<!--/n--> | <!--n:q2.default.stall5-->19<!--/n--> | <!--n:q2.default.goodput-->0.403<!--/n--> | <!--n:q2.default.slo_ratio-->115/120<!--/n--> | <!--n:q2.default.wall_s-->285.2<!--/n--> | <!--n:q2.default.ttft_mean_s-->19.0<!--/n--> / <!--n:q2.default.ttft_p99_s-->64.2<!--/n--> | <!--n:q2.default.wait10-->43<!--/n--> | <!--n:q2.default.maxitl_p99_s-->63.1<!--/n--> | <!--n:q2.default.peak_delays-->0<!--/n--> |
| mine α=0.5 | <!--n:q2.mine_a0.5.retractions-->19<!--/n--> | <!--n:q2.mine_a0.5.stall5-->19<!--/n--> | <!--n:q2.mine_a0.5.goodput-->0.403<!--/n--> | <!--n:q2.mine_a0.5.slo_ratio-->115/120<!--/n--> | <!--n:q2.mine_a0.5.wall_s-->285.2<!--/n--> | <!--n:q2.mine_a0.5.ttft_mean_s-->19.0<!--/n--> / <!--n:q2.mine_a0.5.ttft_p99_s-->64.4<!--/n--> | <!--n:q2.mine_a0.5.wait10-->43<!--/n--> | <!--n:q2.mine_a0.5.maxitl_p99_s-->63.2<!--/n--> | <!--n:q2.mine_a0.5.peak_delays-->0<!--/n--> |
| mine α=0.6 | <!--n:q2.mine_a0.6.retractions-->20<!--/n--> | <!--n:q2.mine_a0.6.stall5-->20<!--/n--> | <!--n:q2.mine_a0.6.goodput-->0.402<!--/n--> | <!--n:q2.mine_a0.6.slo_ratio-->115/120<!--/n--> | <!--n:q2.mine_a0.6.wall_s-->285.8<!--/n--> | <!--n:q2.mine_a0.6.ttft_mean_s-->19.0<!--/n--> / <!--n:q2.mine_a0.6.ttft_p99_s-->64.0<!--/n--> | <!--n:q2.mine_a0.6.wait10-->43<!--/n--> | <!--n:q2.mine_a0.6.maxitl_p99_s-->62.7<!--/n--> | <!--n:q2.mine_a0.6.peak_delays-->0<!--/n--> |
| mine α=0.75 | <!--n:q2.mine_a0.75.retractions-->13<!--/n--> | <!--n:q2.mine_a0.75.stall5-->13<!--/n--> | <!--n:q2.mine_a0.75.goodput-->0.414<!--/n--> | <!--n:q2.mine_a0.75.slo_ratio-->118/120<!--/n--> | <!--n:q2.mine_a0.75.wall_s-->285.1<!--/n--> | <!--n:q2.mine_a0.75.ttft_mean_s-->21.8<!--/n--> / <!--n:q2.mine_a0.75.ttft_p99_s-->62.7<!--/n--> | <!--n:q2.mine_a0.75.wait10-->49<!--/n--> | <!--n:q2.mine_a0.75.maxitl_p99_s-->62.3<!--/n--> | ≥ <!--n:q2.mine_a0.75.peak_delays-->1<!--/n--> |
| mine α=0.9 | <!--n:q2.mine_a0.9.retractions-->7<!--/n--> | <!--n:q2.mine_a0.9.stall5-->7<!--/n--> | <!--n:q2.mine_a0.9.goodput-->0.430<!--/n--> | <!--n:q2.mine_a0.9.slo_ratio-->119/120<!--/n--> | <!--n:q2.mine_a0.9.wall_s-->276.4<!--/n--> | <!--n:q2.mine_a0.9.ttft_mean_s-->26.7<!--/n--> / <!--n:q2.mine_a0.9.ttft_p99_s-->64.3<!--/n--> | <!--n:q2.mine_a0.9.wait10-->57<!--/n--> | <!--n:q2.mine_a0.9.maxitl_p99_s-->59.4<!--/n--> | ≥ <!--n:q2.mine_a0.9.peak_delays-->8<!--/n--> |
| **mine α=1.0★ (선택)** | <!--n:q2.mine.retractions-->0<!--/n--> | <!--n:q2.mine.stall5-->0<!--/n--> | <!--n:q2.mine.goodput-->0.440<!--/n--> | <!--n:q2.mine.slo_ratio-->120/120<!--/n--> | <!--n:q2.mine.wall_s-->272.8<!--/n--> | <!--n:q2.mine.ttft_mean_s-->30.1<!--/n--> / <!--n:q2.mine.ttft_p99_s-->92.3<!--/n--> | <!--n:q2.mine.wait10-->60<!--/n--> | <!--n:q2.mine.maxitl_p99_s-->0.11<!--/n--> | ≥ <!--n:q2.mine.peak_delays-->24<!--/n--> |
| tuned N=24 | <!--n:q2.tuned_n24.retractions-->0<!--/n--> | <!--n:q2.tuned_n24.stall5-->0<!--/n--> | <!--n:q2.tuned_n24.goodput-->0.382<!--/n--> | <!--n:q2.tuned_n24.slo_ratio-->120/120<!--/n--> | <!--n:q2.tuned_n24.wall_s-->313.9<!--/n--> | <!--n:q2.tuned_n24.ttft_mean_s-->62.6<!--/n--> / <!--n:q2.tuned_n24.ttft_p99_s-->156.3<!--/n--> | <!--n:q2.tuned_n24.wait10-->90<!--/n--> | <!--n:q2.tuned_n24.maxitl_p99_s-->0.05<!--/n--> | — |
| tuned N=32 | <!--n:q2.tuned_n32.retractions-->0<!--/n--> | <!--n:q2.tuned_n32.stall5-->0<!--/n--> | <!--n:q2.tuned_n32.goodput-->0.407<!--/n--> | <!--n:q2.tuned_n32.slo_ratio-->120/120<!--/n--> | <!--n:q2.tuned_n32.wall_s-->294.7<!--/n--> | <!--n:q2.tuned_n32.ttft_mean_s-->45.6<!--/n--> / <!--n:q2.tuned_n32.ttft_p99_s-->114.6<!--/n--> | <!--n:q2.tuned_n32.wait10-->82<!--/n--> | <!--n:q2.tuned_n32.maxitl_p99_s-->0.07<!--/n--> | — |
| **tuned N=40 (선택)** | <!--n:q2.tuned_n40.retractions-->0<!--/n--> | <!--n:q2.tuned_n40.stall5-->0<!--/n--> | <!--n:q2.tuned_n40.goodput-->0.432<!--/n--> | <!--n:q2.tuned_n40.slo_ratio-->120/120<!--/n--> | <!--n:q2.tuned_n40.wall_s-->277.9<!--/n--> | <!--n:q2.tuned_n40.ttft_mean_s-->35.2<!--/n--> / <!--n:q2.tuned_n40.ttft_p99_s-->93.5<!--/n--> | <!--n:q2.tuned_n40.wait10-->71<!--/n--> | <!--n:q2.tuned_n40.maxitl_p99_s-->0.09<!--/n--> | — |
| tuned N=48 | <!--n:q2.tuned_n48.retractions-->5<!--/n--> | <!--n:q2.tuned_n48.stall5-->5<!--/n--> | <!--n:q2.tuned_n48.goodput-->0.426<!--/n--> | <!--n:q2.tuned_n48.slo_ratio-->117/120<!--/n--> | <!--n:q2.tuned_n48.wall_s-->274.4<!--/n--> | <!--n:q2.tuned_n48.ttft_mean_s-->28.5<!--/n--> / <!--n:q2.tuned_n48.ttft_p99_s-->72.9<!--/n--> | <!--n:q2.tuned_n48.wait10-->63<!--/n--> | <!--n:q2.tuned_n48.maxitl_p99_s-->76.1<!--/n--> | — |
| noradix (기존 오라클 경로) | <!--n:q2.noradix.retractions-->0<!--/n--> | <!--n:q2.noradix.stall5-->0<!--/n--> | <!--n:q2.noradix.goodput-->0.435<!--/n--> | <!--n:q2.noradix.slo_ratio-->120/120<!--/n--> | <!--n:q2.noradix.wall_s-->275.6<!--/n--> | <!--n:q2.noradix.ttft_mean_s-->30.3<!--/n--> / <!--n:q2.noradix.ttft_p99_s-->89.9<!--/n--> | <!--n:q2.noradix.wait10-->60<!--/n--> | <!--n:q2.noradix.maxitl_p99_s-->0.12<!--/n--> | — |

선택 규칙(5 s 넘는 정지 0건인 설정 중 TTFT p99 최소)으로 α 쪽에서는 α = <!--n:select.alpha_star-->1.0<!--/n-->, N 쪽에서는 N = <!--n:select.n_star-->40<!--/n-->을 골랐다.

- **α < 1.** 선점과 5 s 넘는 정지가 남는다(α = 0.9에서 <!--n:q2.mine_a0.9.retractions-->7<!--/n-->회).
- **α ≤ 0.6.** 게이트가 한 번도 발동하지 않아 default와 같다.
- **α의 다른 의미.** α는 출력 길이를 α배로 짧게 잘못 안 경우와 같은 의미다. 그래서 이 곡선은 길이 정보가 짧게 틀릴 때의 민감도이기도 하다. 길게 틀리는 경우(과대 예약)는 측정하지 않았다.
- **N.** 동시 제한은 N이 작을수록 graph 효과로 TPOT는 좋아지지만, TTFT 꼬리가 크게 는다(N=24: TTFT p99 <!--n:q2.tuned_n24.ttft_p99_s-->156.3<!--/n--> s). N=48에서는 선점이 다시 생긴다.

![선점과 대기의 교환](figures/fig04_tradeoff.png)

*그림 7. q2에서 선점을 줄이는 설정들의 교환. 왼쪽은 선점 수, 오른쪽은 maxITL p99를 TTFT 평균에 대해 그렸다. 예약 비율 α, 동시 제한 N, noradix, cap65k를 비교했다. 채운 점(default·ablated·α=1.0)은 3회 중앙값이고, 빈 점은 n=1이다.*

### 5.2 효과가 없거나 손해인 조건

**표 24. 조건별 default → mine (α=1.0). q2만 3회 중앙값★, 그 밖은 bar마다 n=1. goodput 변화 순**

| 조건 | goodput | 선점 | TTFT 평균 (s) | TTFT p50 (s) | TTFT p99 (s) | 10 s 넘게 대기 | 해석 |
|---|---|---|---|---|---|---|---|
| q1 | <!--n:load.q1.default.goodput-->0.425<!--/n--> → <!--n:load.q1.mine.goodput-->0.422<!--/n--> (<!--n:load.q1.gain_pct-->−0.7<!--/n-->%) | <!--n:load.q1.retractions.default-->7<!--/n--> → <!--n:load.q1.retractions.mine-->0<!--/n--> | <!--n:load.q1.ttft_mean_s.default-->13.7<!--/n--> → <!--n:load.q1.ttft_mean_s.mine-->16.2<!--/n--> | <!--n:load.q1.ttft_p50_s.default-->0.06<!--/n--> → <!--n:load.q1.ttft_p50_s.mine-->0.06<!--/n--> | <!--n:load.q1.ttft_p99_s.default-->46.2<!--/n--> → <!--n:load.q1.ttft_p99_s.mine-->53.5<!--/n--> (<!--n:load.q1.ttft_p99_pct-->+15.9<!--/n-->%) | <!--n:load.q1.wait10.default-->46<!--/n--> → <!--n:load.q1.wait10.mine-->50<!--/n--> | 손해는 TTFT. goodput 차이는 편차 범위(아래) |
| q0.5 | <!--n:load.q0.5.default.goodput-->0.365<!--/n--> → <!--n:load.q0.5.mine.goodput-->0.364<!--/n--> (<!--n:load.q0.5.gain_pct-->0.0<!--/n-->%) | <!--n:load.q0.5.retractions.default-->0<!--/n--> → <!--n:load.q0.5.retractions.mine-->0<!--/n--> | <!--n:load.q0.5.ttft_mean_s.default-->0.05<!--/n--> → <!--n:load.q0.5.ttft_mean_s.mine-->0.05<!--/n--> | <!--n:load.q0.5.ttft_p50_s.default-->0.05<!--/n--> → <!--n:load.q0.5.ttft_p50_s.mine-->0.05<!--/n--> | <!--n:load.q0.5.ttft_p99_s.default-->0.08<!--/n--> → <!--n:load.q0.5.ttft_p99_s.mine-->0.08<!--/n--> | <!--n:load.q0.5.wait10.default-->0<!--/n--> → <!--n:load.q0.5.wait10.mine-->0<!--/n--> | 게이트 발동 0회 → default와 같은 스케줄. 무효 |
| qps 0.35 (default = 순정 3회★) | <!--n:load.q0.35.default.goodput-->0.305<!--/n--> → <!--n:load.q0.35.mine.goodput-->0.305<!--/n--> | 0 → 0 | 0.05 → 0.05 | 0.05 → 0.05 | 0.07 → 0.07 | 0 → 0 | 게이트 발동 0회. 무효 |
| q2 · out_mean 1000 | <!--n:harm.m1000.default.goodput-->1.451<!--/n--> → <!--n:harm.m1000.mine.goodput-->1.452<!--/n--> (<!--n:harm.m1000.gain_pct-->0.0<!--/n-->%) | <!--n:harm.m1000.default.retractions-->0<!--/n--> → <!--n:harm.m1000.mine.retractions-->0<!--/n--> | <!--n:harm.m1000.default.ttft_mean_s-->0.05<!--/n--> → <!--n:harm.m1000.mine.ttft_mean_s-->0.05<!--/n--> | <!--n:harm.m1000.default.ttft_p50_s-->0.05<!--/n--> → <!--n:harm.m1000.mine.ttft_p50_s-->0.05<!--/n--> | <!--n:harm.m1000.default.ttft_p99_s-->0.07<!--/n--> → <!--n:harm.m1000.mine.ttft_p99_s-->0.07<!--/n--> | <!--n:harm.m1000.default.wait10-->0<!--/n--> → <!--n:harm.m1000.mine.wait10-->0<!--/n--> | 출력이 짧아 KV 압박이 없다. 게이트 발동 0회. 무효 |
| q2 · out_sigma 1.0 (꼬리 두꺼움) | <!--n:harm.s1.0.default.goodput-->0.338<!--/n--> → <!--n:harm.s1.0.mine.goodput-->0.341<!--/n--> (<!--n:harm.s1.0.gain_pct-->+0.9<!--/n-->%) | <!--n:harm.s1.0.default.retractions-->9<!--/n--> → <!--n:harm.s1.0.mine.retractions-->0<!--/n--> | <!--n:harm.s1.0.default.ttft_mean_s-->8.4<!--/n--> → <!--n:harm.s1.0.mine.ttft_mean_s-->14.9<!--/n--> | <!--n:harm.s1.0.default.ttft_p50_s-->0.06<!--/n--> → <!--n:harm.s1.0.mine.ttft_p50_s-->0.06<!--/n--> | <!--n:harm.s1.0.default.ttft_p99_s-->48.5<!--/n--> → <!--n:harm.s1.0.mine.ttft_p99_s-->56.2<!--/n--> | <!--n:harm.s1.0.default.wait10-->23<!--/n--> → <!--n:harm.s1.0.mine.wait10-->36<!--/n--> | SLO <!--n:harm.s1.0.default.slo_met-->118<!--/n--> → <!--n:harm.s1.0.mine.slo_met-->120<!--/n-->, wall 349.3 → 352.0 s. 이득은 1회 편차 수준이고 대기 비용이 남는다 (입장 지연 ≥ 19) |
| q2 · out_sigma 0.3 (꼬리 얇음) | <!--n:harm.s0.3.default.goodput-->0.492<!--/n--> → <!--n:harm.s0.3.mine.goodput-->0.497<!--/n--> (<!--n:harm.s0.3.gain_pct-->+0.9<!--/n-->%) | <!--n:harm.s0.3.default.retractions-->27<!--/n--> → <!--n:harm.s0.3.mine.retractions-->0<!--/n--> | <!--n:harm.s0.3.default.ttft_mean_s-->27.7<!--/n--> → <!--n:harm.s0.3.mine.ttft_mean_s-->42.0<!--/n--> | <!--n:harm.s0.3.default.ttft_p50_s-->0.06<!--/n--> → <!--n:harm.s0.3.mine.ttft_p50_s-->43.8<!--/n--> | <!--n:harm.s0.3.default.ttft_p99_s-->97.0<!--/n--> → <!--n:harm.s0.3.mine.ttft_p99_s-->101.6<!--/n--> | <!--n:harm.s0.3.default.wait10-->53<!--/n--> → <!--n:harm.s0.3.mine.wait10-->74<!--/n--> | SLO <!--n:harm.s0.3.default.slo_met-->119<!--/n--> → <!--n:harm.s0.3.mine.slo_met-->120<!--/n-->. 선점이 SLO를 거의 깨지 않던 조건이라 이득은 편차 수준이고 TTFT 비용만 남는다 (입장 지연 ≥ 16) |
| q2★ | <!--n:q2.default.goodput-->0.403<!--/n--> → <!--n:q2.mine.goodput-->0.440<!--/n--> (<!--n:q2.gain.goodput_pct-->+9.1<!--/n-->%) | <!--n:q2.default.retractions-->19<!--/n--> → <!--n:q2.mine.retractions-->0<!--/n--> | <!--n:q2.default.ttft_mean_s-->19.0<!--/n--> → <!--n:q2.mine.ttft_mean_s-->30.1<!--/n--> | <!--n:q2.default.ttft_p50_s-->0.06<!--/n--> → <!--n:q2.mine.ttft_p50_s-->8.1<!--/n--> | <!--n:q2.default.ttft_p99_s-->64.2<!--/n--> → <!--n:q2.mine.ttft_p99_s-->92.3<!--/n--> | <!--n:q2.default.wait10-->43<!--/n--> → <!--n:q2.mine.wait10-->60<!--/n--> | 4.2 |
| q4 | <!--n:load.q4.default.goodput-->0.378<!--/n--> → <!--n:load.q4.mine.goodput-->0.449<!--/n--> (<!--n:load.q4.gain_pct-->+18.7<!--/n-->%) | <!--n:load.q4.retractions.default-->29<!--/n--> → <!--n:load.q4.retractions.mine-->0<!--/n--> | <!--n:load.q4.ttft_mean_s.default-->16.3<!--/n--> → <!--n:load.q4.ttft_mean_s.mine-->35.4<!--/n--> (<!--n:load.q4.ttft_mean_pct-->+117.3<!--/n-->%) | <!--n:load.q4.ttft_p50_s.default-->0.06<!--/n--> → <!--n:load.q4.ttft_p50_s.mine-->8.8<!--/n--> | <!--n:load.q4.ttft_p99_s.default-->75.6<!--/n--> → <!--n:load.q4.ttft_p99_s.mine-->89.2<!--/n--> | <!--n:load.q4.wait10.default-->34<!--/n--> → <!--n:load.q4.wait10.mine-->60<!--/n--> | SLO <!--n:load.q4.slo_met.default-->108<!--/n--> → <!--n:load.q4.slo_met.mine-->120<!--/n-->(×<!--n:load.q4.slo_factor-->1.111<!--/n-->), wall ×<!--n:load.q4.wall_factor-->1.068<!--/n-->. <!--n:q2.longest.rid-->reason-00064<!--/n-->를 빼면 <!--n:load.q4.excl_longest_gain_pct-->+12.5<!--/n-->% |

**q1을 어떻게 읽나.** goodput <!--n:load.q1.default.goodput-->0.425<!--/n--> → <!--n:load.q1.mine.goodput-->0.422<!--/n-->(<!--n:load.q1.gain_pct-->−0.7<!--/n-->%, n=1)은 '손해'로 단정할 수 없다.

- **차이의 출처.** SLO 충족 수는 <!--n:load.q1.slo_met.default-->120<!--/n-->/120으로 같아, 차이가 전부 wall(<!--n:load.q1.wall_s.default-->282.4<!--/n--> → <!--n:load.q1.wall_s.mine-->284.3<!--/n--> s)에서 나온다. 마지막 요청 <!--n:load.q1.last_rid-->reason-00101<!--/n-->의 입장이 <!--n:load.q1.last_ttft_s.default-->27.6<!--/n--> → <!--n:load.q1.last_ttft_s.mine-->34.2<!--/n--> s로 늦어졌기 때문이다.
- **잡음의 크기.** 기능상 같은 q1 default ↔ ablated도 <!--n:load.q1.ablated_vs_default_pct-->−0.5<!--/n-->%이고, q2 ablated 3회 범위가 <!--n:q2.ablated.goodput_spread_pct-->1.24<!--/n-->%다. 따라서 이 goodput 차이는 1회 측정의 편차 범위다.
- **확실한 손해는 TTFT다.** p99 <!--n:load.q1.ttft_p99_s.default-->46.2<!--/n--> → <!--n:load.q1.ttft_p99_s.mine-->53.5<!--/n--> s(<!--n:load.q1.ttft_p99_pct-->+15.9<!--/n-->%), 평균 <!--n:load.q1.ttft_mean_s.default-->13.7<!--/n--> → <!--n:load.q1.ttft_mean_s.mine-->16.2<!--/n--> s, 10 s 넘게 대기 <!--n:load.q1.wait10.default-->46<!--/n--> → <!--n:load.q1.wait10.mine-->50<!--/n-->건이다. 선점 <!--n:load.q1.retractions.default-->7<!--/n-->회가 SLO 위반을 하나도 만들지 않던 부하라, 선점을 없애도 얻을 것이 없고 입장 지연(≥ 13회)의 비용만 남는다.

**표 25. 고정 제한 `--max-running-requests 40`(q2에서 고른 N*)을 다른 조건에 그대로 쓸 때 vs mine (α=1.0). d = default, N = N=40, m = mine. q2의 d·m만 3회★, 그 밖은 n=1**

| 조건 | goodput d / N / m | 선점 d / N / m | TTFT 평균 (s) d / N / m | TTFT p99 (s) d / N / m | 10 s 넘게 대기 d / N / m |
|---|---|---|---|---|---|
| q1 | <!--n:load.q1.default.goodput-->0.425<!--/n--> / <!--n:load.q1.tuned_n40.goodput-->0.416<!--/n--> / <!--n:load.q1.mine.goodput-->0.422<!--/n--> | <!--n:load.q1.retractions.default-->7<!--/n--> / <!--n:reretract.tuned_n40.q1-->1<!--/n--> / <!--n:load.q1.retractions.mine-->0<!--/n--> | <!--n:load.q1.ttft_mean_s.default-->13.7<!--/n--> / <!--n:load.q1.ttft_mean_s.tuned_n40-->18.8<!--/n--> / <!--n:load.q1.ttft_mean_s.mine-->16.2<!--/n--> | <!--n:load.q1.ttft_p99_s.default-->46.2<!--/n--> / <!--n:load.q1.ttft_p99_s.tuned_n40-->50.2<!--/n--> / <!--n:load.q1.ttft_p99_s.mine-->53.5<!--/n--> | <!--n:load.q1.wait10.default-->46<!--/n--> / <!--n:load.q1.wait10.tuned_n40-->63<!--/n--> / <!--n:load.q1.wait10.mine-->50<!--/n--> |
| q2 | <!--n:q2.default.goodput-->0.403<!--/n--> / <!--n:q2.tuned_n40.goodput-->0.432<!--/n--> / <!--n:q2.mine.goodput-->0.440<!--/n--> | <!--n:q2.default.retractions-->19<!--/n--> / <!--n:reretract.tuned_n40.q2-->0<!--/n--> / <!--n:q2.mine.retractions-->0<!--/n--> | <!--n:q2.default.ttft_mean_s-->19.0<!--/n--> / <!--n:q2.tuned_n40.ttft_mean_s-->35.2<!--/n--> / <!--n:q2.mine.ttft_mean_s-->30.1<!--/n--> | <!--n:q2.default.ttft_p99_s-->64.2<!--/n--> / <!--n:q2.tuned_n40.ttft_p99_s-->93.5<!--/n--> / <!--n:q2.mine.ttft_p99_s-->92.3<!--/n--> | <!--n:q2.default.wait10-->43<!--/n--> / <!--n:q2.tuned_n40.wait10-->71<!--/n--> / <!--n:q2.mine.wait10-->60<!--/n--> |
| q4 | <!--n:load.q4.default.goodput-->0.378<!--/n--> / <!--n:load.q4.tuned_n40.goodput-->0.442<!--/n--> / <!--n:load.q4.mine.goodput-->0.449<!--/n--> | <!--n:load.q4.retractions.default-->29<!--/n--> / <!--n:reretract.tuned_n40.q4-->4<!--/n--> / <!--n:load.q4.retractions.mine-->0<!--/n--> | <!--n:load.q4.ttft_mean_s.default-->16.3<!--/n--> / <!--n:load.q4.ttft_mean_s.tuned_n40-->42.4<!--/n--> / <!--n:load.q4.ttft_mean_s.mine-->35.4<!--/n--> | <!--n:load.q4.ttft_p99_s.default-->75.6<!--/n--> / <!--n:load.q4.ttft_p99_s.tuned_n40-->107.1<!--/n--> / <!--n:load.q4.ttft_p99_s.mine-->89.2<!--/n--> | <!--n:load.q4.wait10.default-->34<!--/n--> / <!--n:load.q4.wait10.tuned_n40-->78<!--/n--> / <!--n:load.q4.wait10.mine-->60<!--/n--> |
| q2 · σ 0.3 | <!--n:harm.s0.3.default.goodput-->0.492<!--/n--> / <!--n:harm.s0.3.tuned_n40.goodput-->0.495<!--/n--> / <!--n:harm.s0.3.mine.goodput-->0.497<!--/n--> | <!--n:harm.s0.3.default.retractions-->27<!--/n--> / <!--n:reretract.tuned_n40.s0.3-->0<!--/n--> / <!--n:harm.s0.3.mine.retractions-->0<!--/n--> | <!--n:harm.s0.3.default.ttft_mean_s-->27.7<!--/n--> / <!--n:harm.s0.3.tuned_n40.ttft_mean_s-->43.9<!--/n--> / <!--n:harm.s0.3.mine.ttft_mean_s-->42.0<!--/n--> | <!--n:harm.s0.3.default.ttft_p99_s-->97.0<!--/n--> / <!--n:harm.s0.3.tuned_n40.ttft_p99_s-->112.4<!--/n--> / <!--n:harm.s0.3.mine.ttft_p99_s-->101.6<!--/n--> | <!--n:harm.s0.3.default.wait10-->53<!--/n--> / <!--n:harm.s0.3.tuned_n40.wait10-->80<!--/n--> / <!--n:harm.s0.3.mine.wait10-->74<!--/n--> |
| q2 · σ 1.0 | <!--n:harm.s1.0.default.goodput-->0.338<!--/n--> / <!--n:harm.s1.0.tuned_n40.goodput-->0.336<!--/n--> / <!--n:harm.s1.0.mine.goodput-->0.341<!--/n--> | <!--n:harm.s1.0.default.retractions-->9<!--/n--> / <!--n:reretract.tuned_n40.s1.0-->3<!--/n--> / <!--n:harm.s1.0.mine.retractions-->0<!--/n--> | <!--n:harm.s1.0.default.ttft_mean_s-->8.4<!--/n--> / <!--n:harm.s1.0.tuned_n40.ttft_mean_s-->19.0<!--/n--> / <!--n:harm.s1.0.mine.ttft_mean_s-->14.9<!--/n--> | <!--n:harm.s1.0.default.ttft_p99_s-->48.5<!--/n--> / <!--n:harm.s1.0.tuned_n40.ttft_p99_s-->59.6<!--/n--> / <!--n:harm.s1.0.mine.ttft_p99_s-->56.2<!--/n--> | <!--n:harm.s1.0.default.wait10-->23<!--/n--> / <!--n:harm.s1.0.tuned_n40.wait10-->59<!--/n--> / <!--n:harm.s1.0.mine.wait10-->36<!--/n--> |
| q2 · mean 1000 | <!--n:harm.m1000.default.goodput-->1.451<!--/n--> / <!--n:harm.m1000.tuned_n40.goodput-->1.452<!--/n--> / <!--n:harm.m1000.mine.goodput-->1.452<!--/n--> | <!--n:harm.m1000.default.retractions-->0<!--/n--> / <!--n:reretract.tuned_n40.m1000-->0<!--/n--> / <!--n:harm.m1000.mine.retractions-->0<!--/n--> | <!--n:harm.m1000.default.ttft_mean_s-->0.05<!--/n--> / <!--n:harm.m1000.tuned_n40.ttft_mean_s-->0.05<!--/n--> / <!--n:harm.m1000.mine.ttft_mean_s-->0.05<!--/n--> | <!--n:harm.m1000.default.ttft_p99_s-->0.07<!--/n--> / <!--n:harm.m1000.tuned_n40.ttft_p99_s-->0.07<!--/n--> / <!--n:harm.m1000.mine.ttft_p99_s-->0.07<!--/n--> | <!--n:harm.m1000.default.wait10-->0<!--/n--> / <!--n:harm.m1000.tuned_n40.wait10-->0<!--/n--> / <!--n:harm.m1000.mine.wait10-->0<!--/n--> |

N=40은 q2에 맞춘 값이다.

- **선점 재발.** 같은 값을 다른 조건에 쓰면 q1·q4·σ1.0에서 선점이 다시 생긴다(<!--n:reretract.tuned_n40.q1-->1<!--/n-->·<!--n:reretract.tuned_n40.q4-->4<!--/n-->·<!--n:reretract.tuned_n40.s1.0-->3<!--/n-->회). 이때 maxITL p99는 q4에서 46.4 s, σ1.0에서 14.3 s다. mine(α=1)은 모든 조건에서 선점이 0회였다(α = 1.0 서버 로그 <!--n:reretract.mine_a1.0.logs_without_retraction-->13/13<!--/n-->개에서 선점 0).
- **TTFT 평균.** 압박 조건 5개(q1/q2/q4/σ0.3/σ1.0) 모두에서 mine이 짧다.
- **mine이 앞서지 않는 곳.** q1 TTFT p99는 N=40이 더 낮다(<!--n:load.q1.ttft_p99_s.tuned_n40-->50.2<!--/n--> vs <!--n:load.q1.ttft_p99_s.mine-->53.5<!--/n--> s). mine과 N=40의 goodput 차이(q2에서 <!--n:compare.q2.mine_vs_tuned_n40_pct-->+1.85<!--/n-->%)는 비교 상대가 1회 실행이라 우열의 근거가 되지 않는다.

### 5.3 5×5 교차 검증 (W3 행)

**표 26. 내 엔진 × 5개 워크로드 (mine vs default goodput 변화, 교차 재생 `--scale 0.3`, 각 n=1)**

| | W1 RAG | W2 에이전트 | W3 추론 | W4 구조화 | W5 혼합 |
|---|---|---|---|---|---|
| 내 엔진 (W3) | <!--n:cross.rag.goodput_pct-->+0.01<!--/n-->% | <!--n:cross.agent.goodput_pct-->+0.15<!--/n-->% | <!--n:cross.reasoning.goodput_pct-->−0.03<!--/n-->% | <!--n:cross.structured.goodput_pct-->+0.75<!--/n-->% | <!--n:cross.mixed.goodput_pct-->+0.10<!--/n-->% |

**표 27. 교차 검증 상세 (각 n=1)**

| 워크로드 | 요청 수 | 출력 `max_new_tokens` 최대 | TTFT SLO | goodput default → mine | TTFT p99 (s) | SLO 충족 (%) | 해석 |
|---|---|---|---|---|---|---|---|
| W1 RAG | <!--n:cross.rag.n_requests-->60<!--/n--> | <!--n:cross.rag.out_max-->330<!--/n--> | <!--n:cross.rag.ttft_slo_s-->4.0<!--/n--> s | 0.535 → 0.535 | 1.1 → 1.1 | 100 → 100 | 프롬프트 7,296–7,297토큰으로 KV 대부분이 prefill이다. 예약할 출력 몫이 작다 |
| W2 에이전트 | <!--n:cross.agent.n_requests-->72<!--/n--> | <!--n:cross.agent.out_max-->500<!--/n--> | <!--n:cross.agent.ttft_slo_s-->1.5<!--/n--> s | 0.763 → 0.764 | 0.15 → 0.15 | 100 → 100 | 세션 여러 턴에 걸친 공유 prefix(프롬프트 1,596–6,871토큰). 출력이 짧다 |
| W3 추론 (축소판) | <!--n:cross.reasoning.n_requests-->36<!--/n--> | <!--n:cross.reasoning.out_max-->7,845<!--/n--> | 없음 | 0.197 → 0.197 | 0.06 → 0.06 | 100 → 100 | 0.35 QPS의 저부하라 default도 선점이 0이다. 이득도 비용도 드러날 수 없다 |
| W4 구조화 | <!--n:cross.structured.n_requests-->450<!--/n--> | <!--n:cross.structured.out_max-->300<!--/n--> | <!--n:cross.structured.ttft_slo_s-->0.50<!--/n--> s | 10.825 → 10.907 | 0.14 → 0.13 | 91.8 → 92.4 | KV 압박 없음. `--grammar` 없이 재생해 문법 제약이 꺼져 있었다 |
| W5 혼합 | <!--n:cross.mixed.n_requests-->282<!--/n--> | <!--n:cross.mixed.out_max-->808<!--/n--> | <!--n:cross.mixed.ttft_slo_s-->0.50<!--/n--> s | 2.093 → 2.095 | 26.2 → 26.3 | 43.6 → 43.6 | 대화형 요청과 12k토큰 배치 요청이 섞인다(배치 요청 하나가 풀의 약 14%). 교차 실행 전체에서 게이트가 발동한 것은 여기서 1회뿐이고, default의 선점 1회도 여기서 났다 |

교차 재생은 축소 trace(`--scale 0.3`)를 한 서버에서 연달아 1회씩 재생한 것이다.

- **게이트 발동.** 실행 전체에서 게이트는 <!--n:cross.gate_fired-->1<!--/n-->회(W5)만 발동했다. default의 선점도 <!--n:cross.default_retractions-->1<!--/n-->회뿐이었다.
- **goodput 변화.** <!--n:cross.w3_row_min_pct-->−0.03<!--/n-->%(W3) ~ <!--n:cross.w3_row_max_pct-->+0.75<!--/n-->%(W4)로 모두 1회 편차 안이다.
- **길이 정보.** 교차 trace 5종도 모두 `ignore_eos=True`(출력 길이 오라클)다.

따라서 이 결과가 보여 주는 것은 '다른 워크로드에서도 손해가 없다'가 아니라, '이 저부하 축소 조건에서는 게이트가 거의 발동하지 않는다'이다. W1·W2·W4·W5에는 TTFT SLO가 있어서, 압박 조건이라면 입장 지연이 곧바로 goodput 손실이 될 수 있다(측정하지 않음). 스터디 전체의 5×5 행렬은 다른 담당자의 행이 없어 W3 행만 싣는다.

### 5.4 결론: 언제 켜고 언제 끄는가

이 게이트가 goodput을 높인 조건은 두 가지가 겹칠 때다. KV 풀 대비 도착률이 λmax(≈ 0.6 QPS)를 넘어 선점이 SLO 위반을 만드는 부하(q2·q4)이고, 출력 길이를 미리 아는 장문 생성(ignore_eos 합성 trace)이다. 이때 goodput이 q2에서 <!--n:q2.gain.goodput_pct-->+9.1<!--/n-->%(3회), q4에서 <!--n:load.q4.gain_pct-->+18.7<!--/n-->%(n=1) 올랐다. 다만 이 이득에는 세 가지 단서가 붙는다.

- **기존 플래그로도 대부분 얻는다.** `--disable-radix-cache`로 <!--n:q2.share_of_gain.noradix_pct-->88.0<!--/n-->%를 얻는다(n=1).
- **일부는 wall 효과다.** 가장 긴 요청 한 건의 정지가 사라진 효과라, 그 한 건을 빼면 <!--n:q2.excl_longest.gain_pct-->+4.4<!--/n-->%·<!--n:load.q4.excl_longest_gain_pct-->+12.5<!--/n-->%다.
- **대가는 TTFT다.**
  - q2: p99 <!--n:q2.gain.ttft_p99_pct-->+43.6<!--/n-->%, 평균 <!--n:q2.gain.ttft_mean_pct-->+58.4<!--/n-->%.
  - q4: 평균 <!--n:load.q4.ttft_mean_pct-->+117.3<!--/n-->%(n=1).
  - 선점이 SLO를 깨지 않는 중간 부하 q1: goodput 이득 없이 TTFT p99만 <!--n:load.q1.ttft_p99_pct-->+15.9<!--/n-->% 늘었다(n=1).

이 trace에는 TTFT SLO가 없어(`ttft_slo_ms = null`) goodput이 입장 대기를 벌하지 않는다. 결과를 본 뒤, 가정으로 'TTFT ≤ 10 s'를 SLO에 더해 다시 계산해 보았다(**사후 분석**, 사전 계획에 없던 기준). 그러면 default가 앞선다.

- q2: default <!--n:slo_ttft10.q2.default_range-->0.252–0.256<!--/n-->(3회) vs mine <!--n:slo_ttft10.q2.mine-->0.220<!--/n-->.
- q4: <!--n:slo_ttft10.q4.default-->0.270<!--/n--> vs <!--n:slo_ttft10.q4.mine-->0.225<!--/n-->(n=1).

따라서 TTFT SLO가 있는 서비스, 선점이 SLO를 깨지 않는 중간 부하가 흔한 배포, 출력 길이를 미리 모르는 실서비스에서는 기본으로 켜지 말아야 한다. 켤 때는 α = 1이다. α < 1이면 선점과 5 s 넘는 정지가 남는다(α = 0.9에서 <!--n:q2.mine_a0.9.retractions-->7<!--/n-->회, α ≤ 0.6에서는 default와 같다).

**표 28. 배포 조건별 권장**

| 배포 조건 | 권장 | 근거 |
|---|---|---|
| λmax를 넘는 장문 추론 부하, SLO가 TPOT·끊김뿐 (q2·q4) | ON (α=1). 먼저 기존 `--disable-radix-cache`로 충분한지 확인한다 | q2 <!--n:q2.gain.goodput_pct-->+9.1<!--/n-->%(3회), q4 <!--n:load.q4.gain_pct-->+18.7<!--/n-->%(n=1), 선점·5 s 정지 0. 대가: TTFT 평균 <!--n:q2.gain.ttft_mean_pct-->+58.4<!--/n-->%·<!--n:load.q4.ttft_mean_pct-->+117.3<!--/n-->%. noradix가 이득의 <!--n:q2.share_of_gain.noradix_pct-->88.0<!--/n-->%(n=1) |
| 선점이 없는 저부하·짧은 출력 (q ≤ 0.5, out_mean 1000) | 무관 (켜도 같다) | 게이트 발동 0회, goodput 차이 ±0.1% 이내(각 n=1) |
| 선점이 SLO를 깨지 않는 중간 부하 (q1), 꼬리가 아주 두꺼운 분포 (σ 1.0) | OFF | q1: goodput <!--n:load.q1.gain_pct-->−0.7<!--/n-->%(편차 범위), TTFT p99 <!--n:load.q1.ttft_p99_pct-->+15.9<!--/n-->%. σ1.0: TTFT 평균 <!--n:harm.s1.0.default.ttft_mean_s-->8.4<!--/n--> → <!--n:harm.s1.0.mine.ttft_mean_s-->14.9<!--/n--> s (각 n=1) |
| TTFT SLO가 있는 대화형, 출력 길이를 모르는 실서비스 (EOS) | OFF | q2 TTFT p99 <!--n:q2.gain.ttft_p99_pct-->+43.6<!--/n-->%. TTFT ≤ 10 s를 가정하면 default가 앞선다(사후 분석). `max_new_tokens`가 상한일 뿐이면 과대 예약이 된다(미측정. CPU 실행에서 `max_tokens` 없는 EOS 요청은 동시 2개만 입장) |
| 다른 워크로드 (W1·W2·W4·W5) | 판단 보류 | 저부하 축소 조건에서 게이트 발동 <!--n:cross.gate_fired-->1<!--/n-->회, goodput <!--n:cross.w3_row_min_pct-->−0.03<!--/n--> ~ <!--n:cross.w3_row_max_pct-->+0.75<!--/n-->%(각 n=1). 압박 조건은 측정하지 않았다 |

## 6. 배운 것 / 다음에 한다면

### 6.1 이 워크로드에 대해 시작 전에는 몰랐던 것

선점의 비용이 '버려지는 토큰'이라고 생각했다. 실제로는 이미 만든 출력 토큰은 남고 KV만 다시 계산하며, 그 양은 처리 토큰의 <!--n:diag.recompute_pct_range-->0.22–8.41<!--/n-->%뿐이었다. 피해의 본체는 선점된 요청이 대기열 맨 뒤로 가서 <!--n:diag.stall_range_s-->9.1–116.7<!--/n--> s 멈추는 것이다(09-07 스윕 q0.6–q8, 부하당 n=1의 정지 <!--n:diag.stalls_gt5s-->89<!--/n-->건 전체. q2만 보면 <!--n:diag.stall_range_q2_s-->30.9–64.1<!--/n--> s).

하나 더 있다. 결정론 모드에서 보니, 재prefill로 다시 만든 KV가 피해 요청 <!--n:correct.det_q2up_victims-->30<!--/n-->건 중 22건의 greedy 출력을 바꿨다(결정론 q2 순정, n=1).

W3의 goodput은 요청별 평균 TPOT 하나로 판정되고, wall은 가장 긴 한 요청이 정한다. 그래서 goodput 숫자 하나만 보면 '빨라졌다/느려졌다'를 거의 말하지 못한다. 실제로 <!--n:q2.gain.goodput_pct-->+9.1<!--/n-->% 가운데 상당 부분은 그 한 요청의 정지가 사라진 효과였다(빼면 <!--n:q2.excl_longest.gain_pct-->+4.4<!--/n-->%).

### 6.2 프로덕션 엔진 설계에 대해 알게 된 것

- **admission과 preemption은 한 문제의 두 끝이다.** 같은 자원(KV)을 두고 '지금 기다리게 할 것인가, 나중에 생성 도중 멈추게 할 것인가'를 고르는 문제다. 어느 쪽이 나은지는 TTFT와 TPOT·끊김 중 무엇을 SLO로 두는지가 정한다.
- **스케줄러 정책이 기능 조합에 따라 조용히 바뀐다.** SGLang은 radix OFF에서는 정확한 길이 시뮬레이션을, radix ON에서는 거의 예약하지 않는 낙관 경로를 쓴다. 이런 구조가 측정을 어렵게 했다. 결정론 모드 + flashinfer가 radix를 끄는 것도 같은 문제다.
- **고정 상수는 조건이 바뀌면 다시 튜닝해야 한다.** q2에서 가장 좋았던 고정 동시 제한(N=40)은 부하나 길이 분포가 바뀌면 선점을 다시 허용하거나(q1·q4·σ1.0에서 <!--n:reretract.tuned_n40.q1-->1<!--/n-->·<!--n:reretract.tuned_n40.q4-->4<!--/n-->·<!--n:reretract.tuned_n40.s1.0-->3<!--/n-->회, 각 n=1) 요청을 더 많이 기다리게 했다. 남은 길이와 현재 KV를 보는 입장 제어가 나은 점은 성능 최고점이 아니라, (출력 길이를 아는 조건에서는) 조건이 바뀌어도 다시 튜닝할 부담이 작다는 데 있다.
- **핵심 계산은 이미 SGLang 안에 있었다.** 새로 만드는 일보다, 기존 경로가 어디서 쓰이지 못하는지 찾고 그 효과를 기존 플래그와 정량 비교하는 일이 더 컸다.

### 6.3 시간이 더 있다면

1. **잔여 길이 추정 예약.** 오라클 대신, 지금까지 생성한 양이 주어졌을 때의 기대 잔여 길이를 완료 요청 분포로 추정해 예약하고, α 곡선과 비교한다. 그 전에 출력 길이를 모르는 EOS trace에서 과대 예약의 손해부터 잰다.
2. **원인별 분리 측정.** 선점된 요청을 대기열 맨 뒤가 아니라 앞에 다시 넣는 정책(정지의 직접 원인)과 FP8 KV(풀 2배)를 각각 따로 측정해 이번 결과와 교차시킨다.
3. **TTFT 예산.** TTFT 예산을 가진 SLO(예: TTFT p99 ≤ 10 s)를 결과를 보기 전에 정한다. 그리고 대기가 예산에 가까워지면 입장시키는, SLO를 고려한 α 조절을 시험한다.
4. **남은 기존 손잡이.** `--schedule-conservativeness`와 관련 환경변수(`SGLANG_INIT_NEW_TOKEN_RATIO` 등)는 측정하지 않았다. 같은 q2 조건에서 이 손잡이와도 비교한다.
5. **업스트림 제안.** `add_one_req_ignore_eos`를 radix ON에서도 쓰게 하는 변경을 SGLang에 이슈로 제안한다.

## 부록

### A. 전체 diff

측정에 쓴 diff는 바이트 그대로 [`patch/peak_kv_reservation.diff`](../patch/peak_kv_reservation.diff)에 있다.

- sha256: `bb3d8657c653efa5d48f7163994e2206099171503b6e289ef3820b12774c65c9`
- 크기: <!--n:patch.files-->3<!--/n-->파일, +<!--n:patch.lines_added.server_args-->17<!--/n--> / +<!--n:patch.lines_added.scheduler-->5<!--/n--> / +<!--n:patch.lines_added.schedule_policy-->80<!--/n--> = +<!--n:patch.lines_added-->102<!--/n-->/−<!--n:patch.lines_removed-->0<!--/n-->

적용·되돌리기 방법, Apache-2.0 4조의 변경 고지, 정오표 메모(docstring, `#delays` 하한, 근사)는 [patch/README.md](../patch/README.md)에 있다. GPU 없이 하는 검사는 [tests/README.md](../tests/README.md)에 있다.

**표 29. diff의 구성 (hunk 단위)**

| 파일 | hunk (원본 → 패치본 행) | 내용 |
|---|---|---|
| `server_args.py` | `@@ -888,6 +888,20 @@` | `enable_peak_kv_reservation`, `peak_kv_reserve_ratio` 필드 (`NS("schedule")`) |
| `server_args.py` | `@@ -9280,6 +9294,9 @@` | `0 < peak_kv_reserve_ratio <= 1.0` 검사 |
| `managers/scheduler.py` | `@@ -3271,6 +3271,11 @@` | `PrefillAdder(..., peak_kv_reserve_ratio=α 또는 None)` |
| `managers/schedule_policy.py` | `@@ -28,6 +28,7 @@` | `import time` |
| `managers/schedule_policy.py` | `@@ -91,6 +92,26 @@` | 지연 카운터와 5 s 간격 로그 `_log_peak_kv_delay` |
| `managers/schedule_policy.py` | `@@ -520,9 +541,12 @@` | `PrefillAdder.__init__`의 `peak_kv_reserve_ratio=None` 인자 |
| `managers/schedule_policy.py` | `@@ -1198,6 +1222,52 @@` | `_peak_kv_fits()` (3.3의 발췌) |
| `managers/schedule_policy.py` | `@@ -1296,6 +1366,16 @@` | `add_one_req()` 안의 게이트 |

### B. 재현 명령

모든 명령은 저장소 루트에서 실행한다. 코드는 Python 3.12와 표준 라이브러리, numpy만 쓰고, 그림만 matplotlib가 필요하다. GPU 재현 절차 전체는 [docs/EXPERIMENTS.md](EXPERIMENTS.md)에 있다.

**(1) GPU와 원시 데이터 없이 (git에 있는 파일만으로)**

```bash
python3 tests/run_cpu_tests.py                 # 패치 적용·구조·단위 테스트 ("all peak-KV unit tests passed")
python3 tests/test_upstream_equivalence.py     # upstream 동치·brute force (--full: N=20,000, artifacts/equivalence.json 재생성)
python3 -m w3.select_star                      # 사전 규칙으로 α*, N* 선택 → 마지막 줄 "ASTAR=1.0 NSTAR=40"
python3 -m analysis.headline --check           # artifacts/numbers.json이 data/derived와 맞는지 확인
python3 tools/check_numbers.py                 # 문서의 수치 마커 ↔ artifacts/*.json 대조
python3 -m w3.figures                          # docs/figures/*.png (matplotlib 필요)
```

**(2) 원시 데이터로 파생 데이터를 다시 만들기**

```bash
bash tools/fetch_raw.sh                        # Release data-v1 → results/, logs/ (sha256 검증, mtime 보존)
python3 -m w3.summarize 'results/reasoning*__*_r*.json' --csv data/derived/w3_runs.csv
python3 -m w3.summarize 'results/det/*.json' --csv data/derived/w3_det_runs.csv
python3 -m w3.summarize 'results/reasoning_q2__*_r*.json' --group --csv data/derived/w3_q2_grouped.csv
python3 -m w3.summarize 'results/reasoning_q2__default_r*.json' 'results/reasoning_q2__ablated_r*.json' \
    'results/reasoning_q2__mine_a1.0_r*.json' --std-csv data/derived/reasoning_q2_3bar_runs.csv
python3 -m w3.summarize 'results/reasoning_q*__default.json' --sweep-csv data/derived/sweep_0907.csv
python3 -m w3.export_per_request               # per_request.csv.gz, cum_output.csv.gz, server_logs.csv
python3 -m analysis.retract_cost --sweep-0907 --out data/derived/retract_cost --force
python3 -m analysis.headline                   # artifacts/numbers.json
```

**(3) GPU에서 다시 측정하기 (요약)**

```bash
bash tools/fetch_course_harness.sh             # 과정 하니스(커밋 802a164)를 .course/에 받는다. 재배포하지 않는다
export PYTHONPATH=$PWD/.course/project         # bench.replay 등 과정 하니스
pip install sglang==0.5.18                     # GPU 환경에서. 패치 적용 방법은 patch/README.md
# trace 재생성 명령과 해시: data/traces.sha256 (sha256sum -c data/traces.sha256)
bash w3/run_queue.sh w3/queues/u_upstream.txt  # 순정 실행 (패치 적용 전)
# 여기서 patch/peak_kv_reservation.diff 적용 (patch/README.md의 git apply 또는 patch -p2)
bash w3/run_queue.sh w3/queues/p2_explore.txt
bash w3/run_queue.sh w3/queues/p3_pareto.txt
python3 -m w3.select_star --raw                # → ASTAR=1.0 NSTAR=40
# p4_final.txt와 p5p7_mine.txt는 w3/make_queues.sh 1.0 40으로 만든 큐다.
# 저장소에 있는 판에는 실제로 돌린 q1 ablated 재실행 줄이 더 있으므로 다시 만들지 않는다.
bash w3/run_queue.sh w3/queues/p5p7_indep.txt
bash w3/run_queue.sh w3/queues/p4_final.txt
bash w3/run_queue.sh w3/queues/p5p7_mine.txt
bash w3/verify_pairs.sh <ref.json> <test.json> ...   # 4.5의 결정론 비교 쌍
```

교차 재생(5.3)은 과정 저장소의 `scripts/cross_replay.sh`를 default와 mine(`MINE_FLAGS="--enable-peak-kv-reservation --peak-kv-reserve-ratio 1.0"`)으로 한 번씩 실행했다. 정정 작업에서 실행 스크립트를 두 군데 고쳤다. `w3/run_queue.sh`가 종료 코드를 항상 0으로 기록하던 문제를 고쳤고, `w3/summarize.py`가 09-07 스윕의 서버 로그(`logs/server_q{Q}.log`)를 찾도록 했다. 측정값에는 영향이 없다.

### C. 원시 데이터 목록

원시 결과 JSON과 서버 로그는 git에 넣지 않고 GitHub Release `data-v1`의 `w3-raw-v1.tar.xz`로 공개한다. 크기, sha256, 파일별 MANIFEST는 [data/RAW.md](../data/RAW.md)에 있다. trace는 sha256과 재생성 명령만 공개한다([data/traces.sha256](../data/traces.sha256)). 파생 데이터와 열 정의는 [data/README.md](../data/README.md)에 있다.

**표 30. 원시 결과 파일 (묶음 안의 경로)**

| 파일 (패턴) | 조건 | n | 본문 |
|---|---|---|---|
| `results/reasoning__upstream_r{1,2,3}.json` | 순정 0.5.18, qps 0.35 | 3 | 2.1 |
| `results/reasoning_q2__{default,ablated,mine_a1.0}_r{1,2,3}.json` | q2 3-bar | 각 3 | 4.2 |
| `results/reasoning_q2__{upstream,noradix,cap65k,tuned_n24,tuned_n32,tuned_n40,tuned_n48,mine_a0.5,mine_a0.6,mine_a0.75,mine_a0.9,mine_a1.0_p16}_r1.json` | q2 대조, α·N 탐색, page 16 스모크 | 각 1 | 2.2, 3.3, 5.1 |
| `results/reasoning{,_q0.5,_q1,_q4}__{default,ablated,mine_a1.0,tuned_n40}_r1.json` (있는 조합만) | 부하 곡선 | 각 1 | 4.3, 5.2 |
| `results/reasoning_q2_{s0.3,s1.0,m1000}__{default,mine_a1.0,tuned_n40}_r1.json` | 출력 분포 변형 | 각 1 | 5.2 |
| `results/det/*.json` | 결정론 정확성 데이터셋(q0.2, q2) | 각 1 | 4.5 |
| `results/cross/{rag,agent,reasoning,structured,mixed}_x__{default,mine}.json` | 교차 재생(`--scale 0.3`) | 각 1 | 5.3 |
| `results/reasoning_q{0.5,0.6,0.8,1,2,4,8}__default.json` + `logs/server_q{Q}.log` | 09-07 순정 부하 스윕 | 각 1 | 2.2, 2.3 |
| `logs/server_*.log`, `logs/metrics_*.txt`, `logs/replay_*.txt`, `logs/w3_queue.log` | 서버 로그, `/metrics`, 재생 출력, 실행 기록 | — | 2.2, 부록 D |
| `results/failed/reasoning_q1__ablated_r1.err1.json`, `logs/failed/*` | 제외한 실행(요청 1건 ClientOSError) → 같은 조건 재실행 | — | 4.1 |
| `results/reasoning__batch_test.json` | 측정 아님(09-03 서버 기동 전 재생, 120건 연결 오류) | — | 쓰지 않음 |
| `results/reasoning__noradix.json` / `results/backup/reasoning__noradix.orig.json` | 09-13 실패한 재생으로 덮어써진 파일 / 덮어쓰기 전 qps 0.35 noradix 원본 | — | 쓰지 않음 |
| `results/reasoning__{default,ablated,nograph}.json` | 09-06·09-07의 이전 qps 0.35 실행(서버 로그 없음). 제출본 2.1·2.2의 근거였다 | 각 1 | 쓰지 않음(2.1, 2.2) |

git 안에는 원시 데이터 없이 표와 그림을 다시 만들 수 있는 파생 데이터가 있다([`data/derived/`](../data/derived/)). 서버 로그 두 개와 `bench.verify` 출력, 실행 기록도 git에 있다([`data/logs_sample/`](../data/logs_sample/), [`data/verify/`](../data/verify/), [`data/ledger/`](../data/ledger/)).

### D. 서버 로그 발췌

모두 SGLang 서버 로그를 그대로 옮겼다. q2 default r1과 mine r1의 로그 전체는 [`data/logs_sample/`](../data/logs_sample/)에 있다.

**D-1. resolved server_args (default r1 / mine r1, 핵심 필드만)**

```text
$ grep -m1 'server_args=' data/logs_sample/server_reasoning_q2__default_r1.log | grep -oE '\b(chunked_prefill_size|schedule_policy|retraction_policy|schedule_conservativeness|enable_peak_kv_reservation|peak_kv_reserve_ratio|page_size|disable_radix_cache|attention_backend)=[^,]*' | paste -sd' '
chunked_prefill_size=2048 schedule_policy='fcfs' retraction_policy='length' schedule_conservativeness=1.0 enable_peak_kv_reservation=False peak_kv_reserve_ratio=1.0 page_size=1 disable_radix_cache=False attention_backend='flashinfer'
$ grep -m1 'server_args=' data/logs_sample/server_reasoning_q2__mine_a1.0_r1.log | grep -oE '\b(chunked_prefill_size|schedule_policy|retraction_policy|schedule_conservativeness|enable_peak_kv_reservation|peak_kv_reserve_ratio|page_size|disable_radix_cache|attention_backend)=[^,]*' | paste -sd' '
chunked_prefill_size=2048 schedule_policy='fcfs' retraction_policy='length' schedule_conservativeness=1.0 enable_peak_kv_reservation=True peak_kv_reserve_ratio=1.0 page_size=1 disable_radix_cache=False attention_backend='flashinfer'
```

resolved server_args 전체를 default r1과 비교하면 mine r1은 `enable_peak_kv_reservation=True` 한 필드만 다르다. ablated r1은 `chunked_prefill_size=-1`과 `cuda_graph_config`의 prefill 설정(`max_bs=-1, bs=[]`)만 다르다(원시 로그 `logs/server_reasoning_q2__ablated_r1.log`). decode CUDA graph는 모든 bar에서 `max_bs=24`다.

**D-2. KV 풀 용량**

```text
# default r1 (data/logs_sample/server_reasoning_q2__default_r1.log, 22·30행)
[2026-09-13 16:05:16] KV Cache is allocated. dtype: torch.bfloat16, #tokens: 87552, K size: 6.01 GB, V size: 6.01 GB
[2026-09-13 16:05:21] max_total_num_tokens=87552, chunked_prefill_size=2048, max_prefill_tokens=16384, max_running_requests=2048, context_len=32768, available_gpu_mem=1.91 GB
# mine α=1.0 r1 (data/logs_sample/server_reasoning_q2__mine_a1.0_r1.log, 22·30행)
[2026-09-13 16:10:42] KV Cache is allocated. dtype: torch.bfloat16, #tokens: 87552, K size: 6.01 GB, V size: 6.01 GB
[2026-09-13 16:10:47] max_total_num_tokens=87552, chunked_prefill_size=2048, max_prefill_tokens=16384, max_running_requests=2048, context_len=32768, available_gpu_mem=1.91 GB
# cap65k r1 (원시 로그 logs/server_reasoning_q2__cap65k_r1.log, 22·30행)
[2026-09-13 16:32:11] KV Cache is allocated. dtype: torch.bfloat16, #tokens: 65536, K size: 4.50 GB, V size: 4.50 GB
[2026-09-13 16:32:16] max_total_num_tokens=65536, chunked_prefill_size=2048, max_prefill_tokens=16384, max_running_requests=2048, context_len=32768, available_gpu_mem=4.87 GB
```

**D-3. 선점 (`KV cache pool is full. Retract requests`)**

default r1의 첫 선점 전후는 2.2에 실었다. 선점 줄 수는 다음과 같다(줄마다 `#retracted_reqs: 1`이라 선점 수와 같다).

```text
$ grep -c 'KV cache pool is full' data/logs_sample/*.log
data/logs_sample/server_reasoning_q2__default_r1.log:20
data/logs_sample/server_reasoning_q2__mine_a1.0_r1.log:0
```

09-07 스윕의 첫 선점(q0.6, 원시 로그 `logs/server_q0.6.log`, 474–475행):

```text
[2026-09-07 11:20:22] Decode batch, #running-req: 35, #token: 86979, token usage: 0.99, cuda graph: False, gen throughput (token/s): 1401.91, #queue-req: 4
[2026-09-07 11:20:22] KV cache pool is full. Retract requests. #retracted_reqs: 1, #new_tokens_gained: 857, #new_token_ratio: 0.0980 -> 0.5929
```

**D-4. 입장 지연 (`Peak-KV reservation delayed admission`, mine α=1.0 r1)**

선점 줄은 0개다. 아래 11줄이 이 실행의 지연 로그 전부다(5 s에 최대 1줄이라 `#delays`는 하한).

```text
$ grep 'Peak-KV reservation delayed admission' data/logs_sample/server_reasoning_q2__mine_a1.0_r1.log
[2026-09-13 16:11:17] Peak-KV reservation delayed admission. #delays: 1, #running-req: 55, #admitted-this-round: 0, min_free_tokens: -873
[2026-09-13 16:11:23] Peak-KV reservation delayed admission. #delays: 3, #running-req: 53, #admitted-this-round: 0, min_free_tokens: -569
[2026-09-13 16:11:29] Peak-KV reservation delayed admission. #delays: 6, #running-req: 50, #admitted-this-round: 0, min_free_tokens: 28
[2026-09-13 16:11:34] Peak-KV reservation delayed admission. #delays: 9, #running-req: 48, #admitted-this-round: 0, min_free_tokens: -890
[2026-09-13 16:11:53] Peak-KV reservation delayed admission. #delays: 10, #running-req: 39, #admitted-this-round: 1, min_free_tokens: -1280
[2026-09-13 16:11:58] Peak-KV reservation delayed admission. #delays: 13, #running-req: 37, #admitted-this-round: 0, min_free_tokens: -1049
[2026-09-13 16:12:04] Peak-KV reservation delayed admission. #delays: 15, #running-req: 35, #admitted-this-round: 0, min_free_tokens: -804
[2026-09-13 16:12:12] Peak-KV reservation delayed admission. #delays: 17, #running-req: 31, #admitted-this-round: 0, min_free_tokens: -484
[2026-09-13 16:12:48] Peak-KV reservation delayed admission. #delays: 19, #running-req: 50, #admitted-this-round: 1, min_free_tokens: -18
[2026-09-13 16:12:54] Peak-KV reservation delayed admission. #delays: 21, #running-req: 50, #admitted-this-round: 0, min_free_tokens: -842
[2026-09-13 16:13:00] Peak-KV reservation delayed admission. #delays: 24, #running-req: 47, #admitted-this-round: 0, min_free_tokens: -610
```

`min_free_tokens`는 시뮬레이션에서 처음으로 기준 이하가 된 종료 시점의 예상 여유다. live 요청 수 × max(1, page_size) 이하이면 거절한다. 첫 지연 때 token usage는 0.55였다. 풀이 비어 있어도 앞으로의 최고점을 보고 입장을 미룬다는 뜻이다.

**D-5. qps 0.35 순정의 최대 동시·최대 점유 (원시 로그 `logs/server_reasoning__upstream_r1.log`, 450·788행)**

```text
[2026-09-13 15:26:53] Decode batch, #running-req: 26, #token: 42842, token usage: 0.49, cuda graph: False, gen throughput (token/s): 1365.33, #queue-req: 0
[2026-09-13 15:29:34] Decode batch, #running-req: 21, #token: 66833, token usage: 0.76, cuda graph: True, gen throughput (token/s): 998.42, #queue-req: 0
```

동시 요청이 가장 많을 때도 대기열은 0이고, 점유가 가장 높을 때도 token usage는 0.76이다. 이 로그에는 선점 줄이 없다.
