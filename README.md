English · [한국어](README.ko.md)

# Peak-KV admission for SGLang: diagnosing and removing retraction stalls in long-output serving

At 2 requests per second, SGLang 0.5.18 froze <!--n:q2.default.stall5-->19<!--/n--> of <!--n:trace.n_requests-->120<!--/n--> long answers mid-stream, each for <!--n:q2.default.stall_range_s-->30.8–64.8<!--/n--> s. This study traces those pauses to KV-cache retractions on the default (radix-cache) admission path. It removes them with a flag-gated scheduler gate that reuses SGLang's own exact-length admission simulation, then measures what the change costs and how much of the gain SGLang's existing flags already deliver. Setup: Qwen3-4B on one RTX 4090, a synthetic long-reasoning trace, <!--n:budget.runs-->53<!--/n--> measured runs. Every result in this README is computed from the data in this repository, and a script checks each quoted number against it.

SGLang 0.5.18 + patch (+<!--n:patch.lines_added-->102<!--/n-->/−<!--n:patch.lines_removed-->0<!--/n-->) · Qwen3-4B bf16 · RTX 4090 24GB · CPU-tested · Apache-2.0 / CC BY 4.0

> This is a corrected and extended version of a submitted course report; see [ERRATA.md](ERRATA.md). The work began as an individual assignment in a 5-week LLM inference-engine study; see [Credits and provenance](#credits-and-provenance).

## TL;DR

- **Diagnosis.** In an unpatched sweep over 7 loads, every KV retraction matched a mid-answer stall longer than 5 s, and every such stall matched a retraction (<!--n:diag.stall_match-->89/89<!--/n-->). A retracted request loses its KV and goes to the back of the queue. The stall is that wait; recomputation is only <!--n:diag.recompute_pct_range-->0.22–8.41<!--/n-->% of the processed tokens.
- **Change.** The gate ports SGLang's existing exact-length admission simulation (`add_one_req_ignore_eos`, which runs only when the radix cache is disabled) to the radix-cache path, behind a flag (+<!--n:patch.lines_added-->102<!--/n-->/−<!--n:patch.lines_removed-->0<!--/n--> lines). It reserves the known output lengths of a synthetic `ignore_eos` trace. It is an oracle reservation and does not predict lengths.
- **Result at 2 req/s, median of 3 runs.** Retractions <!--n:q2.default.retractions-->19<!--/n--> → <!--n:q2.mine.retractions-->0<!--/n-->. p99 worst token gap <!--n:q2.default.maxitl_p99_s-->63.1<!--/n--> s → <!--n:q2.mine.maxitl_p99_s-->0.11<!--/n--> s. Goodput <!--n:q2.default.goodput-->0.403<!--/n--> → <!--n:q2.mine.goodput-->0.440<!--/n--> req/s (<!--n:q2.gain.goodput_pct-->+9.1<!--/n-->%), which is SLO-met <!--n:q2.default.slo_met-->115<!--/n--> → <!--n:q2.mine.slo_met-->120<!--/n--> (×<!--n:q2.gain.slo_factor-->1.043<!--/n-->) times wall time <!--n:q2.default.wall_s-->285.2<!--/n--> → <!--n:q2.mine.wall_s-->272.8<!--/n--> s (×<!--n:q2.gain.wall_factor-->1.045<!--/n-->). The wall part comes from one request, <!--n:q2.longest.rid-->reason-00064<!--/n-->, whose <!--n:q2.longest.default_stall_s-->61.7<!--/n--> s stall disappears; without it the gain is <!--n:q2.excl_longest.gain_pct-->+4.4<!--/n-->%. Output tokens/s change by <!--n:q2.gain.out_tok_s_pct-->+4.5<!--/n-->% for the same wall-time reason, not because anything is processed faster.
- **Cost.** TTFT p99 <!--n:q2.default.ttft_p99_s-->64.2<!--/n--> → <!--n:q2.mine.ttft_p99_s-->92.3<!--/n--> s (<!--n:q2.gain.ttft_p99_pct-->+43.6<!--/n-->%), TTFT mean <!--n:q2.default.ttft_mean_s-->19.0<!--/n--> → <!--n:q2.mine.ttft_mean_s-->30.1<!--/n--> s, requests waiting more than 10 s for their first token <!--n:q2.default.wait10-->43<!--/n--> → <!--n:q2.mine.wait10-->60<!--/n-->. The pause moves from mid-answer to before the first token.
- **Existing flags get most of it.** `--disable-radix-cache` recovers <!--n:q2.share_of_gain.noradix_pct-->88.0<!--/n-->% of the goodput gain and `--max-running-requests 40` recovers <!--n:q2.share_of_gain.tuned_n40_pct-->78.1<!--/n-->% (one run each). Every comparison outside the three q2 bars below rests on single runs and is labeled n=1.

### Results at 2 req/s

Trace `reasoning_q2`: 120 requests, Poisson arrivals at 2 req/s, a fresh server for every run. ★ = median of 3 runs with [min–max] over the runs; the other rows are single runs.

| Configuration | Goodput (req/s) | SLO met | Retractions | p99 worst token gap (s) | TTFT p99 (s) | TTFT mean (s) | Waited >10 s |
|---|---|---|---|---|---|---|---|
| default ★ (patched build, flag off) | <!--n:q2.default.goodput-->0.403<!--/n--> [<!--n:q2.default.goodput_range-->0.4025–0.4035<!--/n-->] | <!--n:q2.default.slo_ratio-->115/120<!--/n--> | <!--n:q2.default.retractions-->19<!--/n--> [<!--n:q2.default.retractions_min-->19<!--/n-->–<!--n:q2.default.retractions_max-->20<!--/n-->] | <!--n:q2.default.maxitl_p99_s-->63.1<!--/n--> [<!--n:q2.default.maxitl_p99_range_s-->62.8–63.1<!--/n-->] | <!--n:q2.default.ttft_p99_s-->64.2<!--/n--> [<!--n:q2.default.ttft_p99_range_s-->64.1–64.3<!--/n-->] | <!--n:q2.default.ttft_mean_s-->19.0<!--/n--> | <!--n:q2.default.wait10-->43<!--/n--> |
| ablated ★ (`--chunked-prefill-size -1`) | <!--n:q2.ablated.goodput-->0.407<!--/n--> [<!--n:q2.ablated.goodput_range-->0.4016–0.4067<!--/n-->] | <!--n:q2.ablated.slo_ratio-->116/120<!--/n--> | <!--n:q2.ablated.retractions-->21<!--/n--> [<!--n:q2.ablated.retractions_min-->20<!--/n-->–<!--n:q2.ablated.retractions_max-->21<!--/n-->] | <!--n:q2.ablated.maxitl_p99_s-->63.2<!--/n--> [<!--n:q2.ablated.maxitl_p99_range_s-->62.3–63.6<!--/n-->] | <!--n:q2.ablated.ttft_p99_s-->64.5<!--/n--> [<!--n:q2.ablated.ttft_p99_range_s-->63.6–65.0<!--/n-->] | <!--n:q2.ablated.ttft_mean_s-->19.1<!--/n--> | <!--n:q2.ablated.wait10-->43<!--/n--> |
| **patch ★** (`--enable-peak-kv-reservation`, α = 1) | **<!--n:q2.mine.goodput-->0.440<!--/n-->** [<!--n:q2.mine.goodput_range-->0.4396–0.4399<!--/n-->] | **<!--n:q2.mine.slo_ratio-->120/120<!--/n-->** | **<!--n:q2.mine.retractions-->0<!--/n-->** [<!--n:q2.mine.retractions_min-->0<!--/n-->–<!--n:q2.mine.retractions_max-->0<!--/n-->] | **<!--n:q2.mine.maxitl_p99_s-->0.11<!--/n-->** [<!--n:q2.mine.maxitl_p99_range_s-->0.11–0.11<!--/n-->] | <!--n:q2.mine.ttft_p99_s-->92.3<!--/n--> [<!--n:q2.mine.ttft_p99_range_s-->92.2–92.4<!--/n-->] | <!--n:q2.mine.ttft_mean_s-->30.1<!--/n--> | <!--n:q2.mine.wait10-->60<!--/n--> |
| existing `--disable-radix-cache` (n=1) | <!--n:q2.noradix.goodput-->0.435<!--/n--> | <!--n:q2.noradix.slo_ratio-->120/120<!--/n--> | <!--n:q2.noradix.retractions-->0<!--/n--> | <!--n:q2.noradix.maxitl_p99_s-->0.12<!--/n--> | <!--n:q2.noradix.ttft_p99_s-->89.9<!--/n--> | <!--n:q2.noradix.ttft_mean_s-->30.3<!--/n--> | <!--n:q2.noradix.wait10-->60<!--/n--> |
| existing `--max-running-requests 40` (n=1) | <!--n:q2.tuned_n40.goodput-->0.432<!--/n--> | <!--n:q2.tuned_n40.slo_ratio-->120/120<!--/n--> | <!--n:q2.tuned_n40.retractions-->0<!--/n--> | <!--n:q2.tuned_n40.maxitl_p99_s-->0.09<!--/n--> | <!--n:q2.tuned_n40.ttft_p99_s-->93.5<!--/n--> | <!--n:q2.tuned_n40.ttft_mean_s-->35.2<!--/n--> | <!--n:q2.tuned_n40.wait10-->71<!--/n--> |

SLO: mean time per output token (TPOT) ≤ <!--n:trace.tpot_slo_ms-->60<!--/n--> ms per request; the trace sets no TTFT SLO. Goodput = SLO-met requests / wall time. p99 worst token gap = the p99, over requests, of each request's longest pause between two streamed chunks (maxITL p99). SLO met, TTFT mean and Waited >10 s are medians; across the three runs they move by at most one request or 0.4 s ([`data/derived/w3_q2_grouped.csv`](data/derived/w3_q2_grouped.csv)). "default" is the patched build with the flag off. An unpatched run at the same load (n=1) gave <!--n:q2.upstream.goodput-->0.403<!--/n--> req/s and <!--n:q2.upstream.retractions-->19<!--/n--> retractions.

![Request timelines at 2 req/s: default vs. patch](docs/figures/hero_timeline.png)

*One run of each configuration at 2 req/s: the run with the median goodput of the three (r2), n=1 per panel. Each row is one request in arrival order, showing its wait before the first token, its generation, and any pause longer than 5 s during generation. Default: <!--n:hero.default.stalled-->19<!--/n--> requests stall for <!--n:hero.default.stall_range_s-->32.5–64.8<!--/n--> s after they have started answering. Patch: <!--n:hero.mine.stalled-->0<!--/n--> stalls, but <!--n:hero.mine.wait10-->60<!--/n--> requests instead of <!--n:hero.default.wait10-->43<!--/n--> wait more than 10 s for their first token. Data: `data/derived/per_request.csv.gz`; script: `w3/figures.py`.*

### How to read these numbers

1. **Where the <!--n:q2.gain.goodput_pct-->+9.1<!--/n-->% comes from.** Goodput is SLO-met requests divided by wall time, and wall time ends when the last request finishes. SLO-met rises <!--n:q2.default.slo_met-->115<!--/n--> → <!--n:q2.mine.slo_met-->120<!--/n--> (×<!--n:q2.gain.slo_factor-->1.043<!--/n-->) and wall time falls <!--n:q2.default.wall_s-->285.2<!--/n--> → <!--n:q2.mine.wall_s-->272.8<!--/n--> s (×<!--n:q2.gain.wall_factor-->1.045<!--/n-->). The wall term is one request: <!--n:q2.longest.rid-->reason-00064<!--/n-->, the longest answer (<!--n:q2.longest.out_tokens-->10,541<!--/n--> tokens), finishes last in <!--n:q2.longest.last_to_finish_default-->3/3<!--/n--> default runs after a <!--n:q2.longest.default_stall_s-->61.7<!--/n--> s mid-answer stall. With the patch it waits <!--n:q2.longest.mine_ttft_s-->32.1<!--/n--> s for admission and then streams without a pause. Without that request the gain is <!--n:q2.excl_longest.gain_pct-->+4.4<!--/n-->% (<!--n:q2.excl_longest.default_goodput-->0.420<!--/n--> → <!--n:q2.excl_longest.mine_goodput-->0.439<!--/n--> req/s). Output tokens/s change by <!--n:q2.gain.out_tok_s_pct-->+4.5<!--/n-->% for the same reason: the total output is fixed at <!--n:trace.out_total-->337,433<!--/n--> tokens, so tokens/s is that total divided by wall time. Half of all output tokens arrive <!--n:q2.cum.t50_mine_minus_default_s-->5.6<!--/n--> s *later* with the patch (<!--n:q2.cum.default_t50_s-->96.3<!--/n--> → <!--n:q2.cum.mine_t50_s-->101.9<!--/n--> s). See [`fig05_goodput_decomposition.png`](docs/figures/fig05_goodput_decomposition.png).
2. **Existing flags reproduce most of the gain.** `--disable-radix-cache` switches SGLang to the exact-length path the gate is ported from and reaches <!--n:q2.noradix.goodput-->0.435<!--/n--> req/s (<!--n:q2.share_of_gain.noradix_pct-->88.0<!--/n-->% of the gain, n=1). `--max-running-requests 40` reaches <!--n:q2.tuned_n40.goodput-->0.432<!--/n--> (<!--n:q2.share_of_gain.tuned_n40_pct-->78.1<!--/n-->%, n=1). All three settings meet the SLO for 120/120 requests, so their remaining goodput differences are wall time and single runs. The gate adds two things: exact-length admission with the radix cache on, and no retraction under any load tested. N = 40 retracted again at 1 req/s, 4 req/s and σ = 1.0 (<!--n:reretract.tuned_n40.q1-->1<!--/n-->, <!--n:reretract.tuned_n40.q4-->4<!--/n--> and <!--n:reretract.tuned_n40.s1.0-->3<!--/n--> times, n=1 each). This trace shares only a <!--n:trace.shared_prefix_tokens-->36<!--/n-->-token prefix, so keeping the radix cache on is worth little here. Whether larger shared prefixes favor the gate was not measured.
3. **Oracle lengths.** Every trace sets `ignore_eos=True`, so `max_new_tokens` equals each request's real output length, and the gate reserves exactly what each request will use. Read the result as an upper bound for any scheme that has to guess lengths. It is not usable as-is for traffic with unknown output lengths or with a TTFT SLO (see [Costs](#costs-and-when-not-to-use-it)).

[Report (English)](docs/REPORT.md) · [보고서 (한국어)](docs/REPORT.ko.md) · [Patch](patch/README.md) · [CPU tests](tests/README.md) · [Hard questions](#faq) · [Errata](ERRATA.md) · [Experiment log](docs/EXPERIMENTS.md) · [Data](data/README.md)

## Problem and setup

The workload is the course's long-reasoning workload (called W3 in file names): short prompts, long streamed answers.

| Item | Value |
|---|---|
| Requests | <!--n:trace.n_requests-->120<!--/n--> independent requests with Poisson arrivals. Base trace 0.35 req/s; main experiments at 2 req/s (`q2`); load sweep 0.5–8 req/s |
| Prompt | <!--n:trace.prompt_tokens_range-->386–387<!--/n--> tokens, of which <!--n:trace.shared_prefix_tokens-->36<!--/n--> are a prefix shared through the radix cache (cache hit <!--n:trace.cache_hit_pct-->11.5<!--/n-->%, or <!--n:trace.cache_hit_excl_warmup_pct-->9.3<!--/n-->% without the three warm-up prompts) |
| Output | p50 <!--n:trace.out_p50-->2,338<!--/n-->, p90 <!--n:trace.out_p90-->4,744.7<!--/n-->, p99 <!--n:trace.out_p99-->8,927.2<!--/n-->, max <!--n:trace.out_max-->10,541<!--/n--> tokens; <!--n:trace.out_total-->337,433<!--/n--> in total. `ignore_eos=True`, so each request generates exactly `max_new_tokens` tokens |
| SLO | mean TPOT ≤ <!--n:trace.tpot_slo_ms-->60<!--/n--> ms per request; no TTFT SLO |
| Goodput | SLO-met requests / wall time, where wall time runs from the first submit to the last completion |
| KV pool | <!--n:kv.pool_tokens-->87,552<!--/n--> tokens ≈ <!--n:kv.pool_gib-->12.0<!--/n--> GiB (<!--n:kv.bytes_per_token-->147,456<!--/n--> bytes per token) |
| Stack | SGLang 0.5.18 (`71de97b`), Qwen/Qwen3-4B bf16 (revision `1cfa9a72`), one RTX 4090 24 GB |
| Server flags | `--context-length 32768 --mem-fraction-static 0.85 --random-seed 42 --log-level info --enable-metrics`. Resolved: FlashInfer attention, chunked prefill 2,048, FCFS, page size 1, decode CUDA graphs for batches ≤ 24 |

Each request's KV grows with every generated token, and the longest answers hold their KV for minutes. Little's law gives the arrival rate this pool can carry without running out: about <!--n:diag.little_lambda_max-->0.56–0.67<!--/n--> req/s (assuming 20–24 ms per token). Condition names used below: `qX` = X req/s; `σ` = the generator's output-length spread (`out_sigma`; 0.3 is narrower and 1.0 wider than the base trace); `m1000` = mean output 1,000 tokens instead of 3,000.

## Diagnosis: why long answers stall

**Mechanism.** SGLang admits a waiting request when its prompt fits next to a reserve for the running requests. With the radix cache on (the default), the reserve per running request is `min(remaining tokens, 4096) × new_token_ratio` ([`schedule_policy.py:654`](third_party/sglang_v0_5_18/schedule_policy.py#L654)). The ratio decays from 0.7 to a floor of 0.098, so a request that may still generate 10,000 tokens reserves about 400. When the pool runs out, SGLang retracts the running request with the fewest output tokens. Its KV is freed and it goes to the back of the waiting queue, and its stream stops until it is admitted again. SGLang also has an exact simulation, `add_one_req_ignore_eos` ([`:1065`](third_party/sglang_v0_5_18/schedule_policy.py#L1065)). It reserves the full remaining length of `ignore_eos` requests, taking them in finish order, but it runs only when the radix cache is disabled ([`:1213`](third_party/sglang_v0_5_18/schedule_policy.py#L1213)).

**Evidence**, from a load sweep on unpatched SGLang (2026-09-07, n=1 per load; [`data/derived/sweep_0907.csv`](data/derived/sweep_0907.csv), [`fig02_sweep_0907.png`](docs/figures/fig02_sweep_0907.png)):

| Load (req/s) | 0.5 | 0.6 | 0.8 | 1 | 2 | 4 | 8 |
|---|---|---|---|---|---|---|---|
| Retractions | <!--n:sweep0907.retractions.q0.5-->0<!--/n--> | <!--n:sweep0907.retractions.q0.6-->1<!--/n--> | <!--n:sweep0907.retractions.q0.8-->4<!--/n--> | <!--n:sweep0907.retractions.q1-->7<!--/n--> | <!--n:sweep0907.retractions.q2-->20<!--/n--> | <!--n:sweep0907.retractions.q4-->29<!--/n--> | <!--n:sweep0907.retractions.q8-->28<!--/n--> |
| Requests stalled >5 s | <!--n:sweep0907.stall5.q0.5-->0<!--/n--> | <!--n:sweep0907.stall5.q0.6-->1<!--/n--> | <!--n:sweep0907.stall5.q0.8-->4<!--/n--> | <!--n:sweep0907.stall5.q1-->7<!--/n--> | <!--n:sweep0907.stall5.q2-->20<!--/n--> | <!--n:sweep0907.stall5.q4-->29<!--/n--> | <!--n:sweep0907.stall5.q8-->28<!--/n--> |
| Max KV usage | <!--n:sweep0907.max_usage.q0.5-->0.82<!--/n--> | <!--n:sweep0907.max_usage.q0.6-->0.99<!--/n--> | <!--n:sweep0907.max_usage.q0.8-->1.00<!--/n--> | <!--n:sweep0907.max_usage.q1-->1.00<!--/n--> | <!--n:sweep0907.max_usage.q2-->1.00<!--/n--> | <!--n:sweep0907.max_usage.q4-->1.00<!--/n--> | <!--n:sweep0907.max_usage.q8-->1.00<!--/n--> |
| p99 worst token gap (s) | <!--n:sweep0907.maxitl_p99_s.q0.5-->0.07<!--/n--> | <!--n:sweep0907.maxitl_p99_s.q0.6-->0.12<!--/n--> | <!--n:sweep0907.maxitl_p99_s.q0.8-->24.6<!--/n--> | <!--n:sweep0907.maxitl_p99_s.q1-->29.5<!--/n--> | <!--n:sweep0907.maxitl_p99_s.q2-->62.6<!--/n--> | <!--n:sweep0907.maxitl_p99_s.q4-->102.5<!--/n--> | <!--n:sweep0907.maxitl_p99_s.q8-->116.2<!--/n--> |
| TTFT p99 (s) | <!--n:sweep0907.ttft_p99_s.q0.5-->0.07<!--/n--> | <!--n:sweep0907.ttft_p99_s.q0.6-->16.2<!--/n--> | <!--n:sweep0907.ttft_p99_s.q0.8-->29.1<!--/n--> | <!--n:sweep0907.ttft_p99_s.q1-->46.5<!--/n--> | <!--n:sweep0907.ttft_p99_s.q2-->63.9<!--/n--> | <!--n:sweep0907.ttft_p99_s.q4-->75.7<!--/n--> | <!--n:sweep0907.ttft_p99_s.q8-->78.2<!--/n--> |
| TPOT p99 (ms) | <!--n:sweep0907.tpot_p99_ms.q0.5-->21.9<!--/n--> | <!--n:sweep0907.tpot_p99_ms.q0.6-->23.9<!--/n--> | <!--n:sweep0907.tpot_p99_ms.q0.8-->38.6<!--/n--> | <!--n:sweep0907.tpot_p99_ms.q1-->36.4<!--/n--> | <!--n:sweep0907.tpot_p99_ms.q2-->93.2<!--/n--> | <!--n:sweep0907.tpot_p99_ms.q4-->94.9<!--/n--> | <!--n:sweep0907.tpot_p99_ms.q8-->102.3<!--/n--> |
| SLO met (%) | <!--n:sweep0907.slo_pct.q0.5-->100.0<!--/n--> | <!--n:sweep0907.slo_pct.q0.6-->100.0<!--/n--> | <!--n:sweep0907.slo_pct.q0.8-->100.0<!--/n--> | <!--n:sweep0907.slo_pct.q1-->100.0<!--/n--> | <!--n:sweep0907.slo_pct.q2-->96.7<!--/n--> | <!--n:sweep0907.slo_pct.q4-->90.0<!--/n--> | <!--n:sweep0907.slo_pct.q8-->88.3<!--/n--> |
| Goodput (req/s) | <!--n:sweep0907.goodput.q0.5-->0.365<!--/n--> | <!--n:sweep0907.goodput.q0.6-->0.390<!--/n--> | <!--n:sweep0907.goodput.q0.8-->0.407<!--/n--> | <!--n:sweep0907.goodput.q1-->0.425<!--/n--> | <!--n:sweep0907.goodput.q2-->0.407<!--/n--> | <!--n:sweep0907.goodput.q4-->0.379<!--/n--> | <!--n:sweep0907.goodput.q8-->0.365<!--/n--> |

![Load sweep on unpatched SGLang: retractions start at 0.6 req/s, and TTFT p99 and the worst token gap climb with them](docs/figures/fig02_sweep_0907.png)

1. **The onset matches capacity.** The Little's-law bound is <!--n:diag.little_lambda_max-->0.56–0.67<!--/n--> req/s. The first retraction appears at <!--n:diag.first_retraction_qps-->0.6<!--/n--> req/s, where KV usage peaks at <!--n:sweep0907.max_usage.q0.6-->0.99<!--/n-->. At 0.5 req/s usage peaks at <!--n:sweep0907.max_usage.q0.5-->0.82<!--/n--> and nothing is retracted.
2. **One retraction, one stall.** [`analysis/retract_cost.py`](analysis/retract_cost.py) matches every `Retract requests` line in the server logs to the client's token stream. <!--n:diag.stall_match-->89/89<!--/n--> retractions match a gap of more than 5 s (<!--n:diag.stall_match_exact-->87<!--/n--> exactly by time and token count, <!--n:diag.stall_match_approx-->2<!--/n--> approximately), and no such gap is left unmatched. The stalls last <!--n:diag.stall_range_s-->9.1–116.7<!--/n--> s. Every request that missed the SLO in the sweep was a retraction victim (<!--n:diag.slo_violators_all_victims-->30/30<!--/n-->).
3. **The stall is queueing, not recomputation.** In the <!--n:diag.reprefill_confirmed-->38<!--/n--> cases where the victim's re-prefill can be found in the log, the time from retraction to re-prefill equals the stall within <!--n:diag.reprefill_delay_vs_stall_max_s-->0.90<!--/n--> s. Recomputed prefill is <!--n:diag.recompute_pct_range-->0.22–8.41<!--/n-->% of the tokens processed per run ([`data/derived/retract_cost/`](data/derived/retract_cost/)).
4. **Interventions at 2 req/s (2026-09-13).** Every change that admits more conservatively removes the retractions. Turning chunked prefill off changes nothing, and shrinking the KV pool makes the tail worse:

| q2 control (★ = median of 3, others n=1) | Retractions | p99 worst token gap (s) | TTFT p99 (s) | SLO met | Goodput (req/s) |
|---|---|---|---|---|---|
| default ★ | <!--n:q2.default.retractions-->19<!--/n--> | <!--n:q2.default.maxitl_p99_s-->63.1<!--/n--> | <!--n:q2.default.ttft_p99_s-->64.2<!--/n--> | <!--n:q2.default.slo_ratio-->115/120<!--/n--> | <!--n:q2.default.goodput-->0.403<!--/n--> |
| KV pool cut to <!--n:kv.cap65k_pool_tokens-->65,536<!--/n--> tokens (`--max-total-tokens 65536`) | <!--n:q2.cap65k.retractions-->22<!--/n--> | <!--n:q2.cap65k.maxitl_p99_s-->103.8<!--/n--> | <!--n:q2.cap65k.ttft_p99_s-->114.0<!--/n--> | <!--n:q2.cap65k.slo_ratio-->117/120<!--/n--> | <!--n:q2.cap65k.goodput-->0.383<!--/n--> |
| chunked prefill off (ablated) ★ | <!--n:q2.ablated.retractions-->21<!--/n--> | <!--n:q2.ablated.maxitl_p99_s-->63.2<!--/n--> | <!--n:q2.ablated.ttft_p99_s-->64.5<!--/n--> | <!--n:q2.ablated.slo_ratio-->116/120<!--/n--> | <!--n:q2.ablated.goodput-->0.407<!--/n--> |
| `--disable-radix-cache` | <!--n:q2.noradix.retractions-->0<!--/n--> | <!--n:q2.noradix.maxitl_p99_s-->0.12<!--/n--> | <!--n:q2.noradix.ttft_p99_s-->89.9<!--/n--> | <!--n:q2.noradix.slo_ratio-->120/120<!--/n--> | <!--n:q2.noradix.goodput-->0.435<!--/n--> |
| `--max-running-requests 24` | <!--n:q2.tuned_n24.retractions-->0<!--/n--> | <!--n:q2.tuned_n24.maxitl_p99_s-->0.05<!--/n--> | <!--n:q2.tuned_n24.ttft_p99_s-->156.3<!--/n--> | <!--n:q2.tuned_n24.slo_ratio-->120/120<!--/n--> | <!--n:q2.tuned_n24.goodput-->0.382<!--/n--> |
| `--max-running-requests 32` | <!--n:q2.tuned_n32.retractions-->0<!--/n--> | <!--n:q2.tuned_n32.maxitl_p99_s-->0.07<!--/n--> | <!--n:q2.tuned_n32.ttft_p99_s-->114.6<!--/n--> | <!--n:q2.tuned_n32.slo_ratio-->120/120<!--/n--> | <!--n:q2.tuned_n32.goodput-->0.407<!--/n--> |
| `--max-running-requests 40` | <!--n:q2.tuned_n40.retractions-->0<!--/n--> | <!--n:q2.tuned_n40.maxitl_p99_s-->0.09<!--/n--> | <!--n:q2.tuned_n40.ttft_p99_s-->93.5<!--/n--> | <!--n:q2.tuned_n40.slo_ratio-->120/120<!--/n--> | <!--n:q2.tuned_n40.goodput-->0.432<!--/n--> |
| `--max-running-requests 48` | <!--n:q2.tuned_n48.retractions-->5<!--/n--> | <!--n:q2.tuned_n48.maxitl_p99_s-->76.1<!--/n--> | <!--n:q2.tuned_n48.ttft_p99_s-->72.9<!--/n--> | <!--n:q2.tuned_n48.slo_ratio-->117/120<!--/n--> | <!--n:q2.tuned_n48.goodput-->0.426<!--/n--> |
| patch, α = 1 ★ | <!--n:q2.mine.retractions-->0<!--/n--> | <!--n:q2.mine.maxitl_p99_s-->0.11<!--/n--> | <!--n:q2.mine.ttft_p99_s-->92.3<!--/n--> | <!--n:q2.mine.slo_ratio-->120/120<!--/n--> | <!--n:q2.mine.goodput-->0.440<!--/n--> |

The smaller pool is a single run, and its <!--n:q2.cap65k.retractions-->22<!--/n--> retractions are only just above the default and ablated runs (<!--n:q2.default.retractions_min-->19<!--/n-->–<!--n:q2.ablated.retractions_max-->21<!--/n-->), so it is medium-strength evidence. The stronger evidence is the matching above and the interventions that remove the retractions.

## What I changed

### Who did what

| Already in SGLang 0.5.18 | Provided by the course | My work |
|---|---|---|
| `add_one_req_ignore_eos`: exact admission check for `ignore_eos` requests that uses their full remaining length in finish order. Used only when the radix cache is disabled. | Workload definition and trace generator (the long-reasoning workload, `ignore_eos`, mean-TPOT SLO). | Diagnosis: the load sweep, matching retractions to stalls (`analysis/retract_cost.py`) and the Little's-law bound. |
| Radix-cache path: reserves `min(remaining, 4096) × new_token_ratio` per running request (floor 0.098). | Open-loop replay, metrics (including the goodput definition) and the output-verify tool. | Finding that the computation the course hinted at already exists upstream but is off on the default path, and porting it to the radix-cache path as a reject-only gate (+<!--n:patch.lines_added-->102<!--/n-->/−<!--n:patch.lines_removed-->0<!--/n-->, off by default). The port adds shared-prefix accounting, α, a page-size reserve, a finished-request filter, an idle invariant and a throttled log line. |
| Retraction (fewest output tokens first, re-queued at the back), `--disable-radix-cache`, `--max-running-requests`, `--schedule-conservativeness`. | Protocol: restart per bar, 3 repetitions in alternating order, a tuned fourth control, a deterministic-mode output check. The hint to explore conservative peak-KV reservation at admission. | Experiment automation (`w3/run_one.sh`, `w3/run_queue.sh`, queue files), the preregistered selection rule, <!--n:budget.runs-->53<!--/n--> runs, the analysis scripts, the unit test `w3/test_peak_kv.py` and the report. |

After submission the results were re-audited with AI assistance; see [Tools used](#tools-used).

### The gate in five steps

`PrefillAdder._peak_kv_fits` runs inside `add_one_req`, after upstream's own checks. [`patch/README.md`](patch/README.md) describes every hunk.

1. Collect every unfinished running request, every request admitted earlier in this scheduling round, and the candidate.
2. For each one, compute `left = α × (max_new_tokens − tokens generated)`. Also compute `own`: the KV it holds minus the prefix it shares through the radix cache (`cached_tokens` for running requests, `prefix_indices` for new ones), rounded up to whole pages.
3. Sort by `left`. Every live request grows by one token per decode step, so this is the order in which they finish.
4. Walk the finish events. When the i-th request finishes, free KV = free now + `own` of the requests already finished − `left_i` × (requests still live). "Free now" is available plus evictable KV, minus this round's admissions and the candidate's prefill.
5. If free KV at any finish event is ≤ `max(1, page_size)` × (requests still live), return `NO_TOKEN`. The candidate keeps its place in the queue and is checked again in a later round.

### Invariants

- **Reject-only.** The gate runs after upstream's checks and can only refuse, so it never admits a request that upstream would refuse.
- **No deadlock.** An idle server (no unfinished running request, nothing admitted this round) always admits.
- **Off means upstream.** With the flag off the scheduler passes `None` and upstream's code path runs. In deterministic mode, flag-off and unpatched outputs are identical for <!--n:correct.det_q02up_vs_q02off-->120/120<!--/n--> requests, and at 2 req/s the flag-off build reaches <!--n:q2.default.goodput_range-->0.4025–0.4035<!--/n--> req/s against <!--n:q2.upstream.goodput-->0.403<!--/n--> unpatched.
- **Queue order is kept.** `NO_TOKEN` ends the admission round without reordering the FCFS queue, so a delayed request is admitted once earlier requests finish.
- **Page-aligned.** A `--page-size 16` smoke run completed <!--n:smoke.page16-->120/120<!--/n--> requests with <!--n:smoke.page16.retractions-->0<!--/n--> retractions (n=1).

### Known approximations

- **Oracle lengths.** The gate trusts `max_new_tokens`. For an EOS-terminated request without `max_tokens`, SGLang sets it to a context bound of about 32k tokens. In this pool the α = 1 gate would then admit only two such requests started together (CPU check; never measured on GPU).
- **Shared prefix.** If the request that created the shared prefix finishes first, the model counts the prefix as freed while other requests still lock it. The gate is then optimistic by <!--n:trace.shared_prefix_tokens-->36<!--/n--> tokens here.
- **Chunked prefill.** A chunked prefill admitted this round is credited with its whole prompt at finish, but the part not yet allocated is not modelled as growth. This matters only for prompts longer than 2,048 tokens; W3 prompts are not chunked.
- **The log counter.** The `#delays` value in the server log is printed at most every 5 s, so the last logged value is a lower bound.

"α = 1 gives zero retractions" is therefore a measurement under W3 conditions (<!--n:reretract.mine_a1.0.logs_without_retraction-->13/13<!--/n--> server logs of α = 1 runs show none), not a guarantee. Upstream's retraction remains the safety net. The [CPU tests](tests/README.md) reproduce each approximation.

## Evaluation method

- **Preregistered selection rule.** The [plan](docs/preregistration/plan_2026-09-13.md) was written at 13:59:57 UTC on 2026-09-13. The first measured run started at 15:03:02 UTC ([`data/ledger/w3_queue.log`](data/ledger/w3_queue.log)). The rule: among the α and N settings tried at 2 req/s, keep those with no request stalled more than 5 s (an auxiliary SLO fixed in the plan), then take the lowest TTFT p99. Result: α* = <!--n:select.alpha_star-->1.0<!--/n-->, N* = <!--n:select.n_star-->40<!--/n--> (`python3 -m w3.select_star`). The mean-TPOT SLO alone misses most stalls: <!--n:q2.default.stalled_tpot_ok_range-->14–15<!--/n--> of the <!--n:q2.default.stall5_range-->19–20<!--/n--> stalled requests in each default run still met it. The auxiliary SLO favors a gate built to remove stalls, and the file times are a record, not a proof.
- **A fresh server for every run.** [`w3/run_one.sh`](w3/run_one.sh) starts a new server for each run (3 warm-up requests, open-loop replay, `/metrics` dump, shutdown). It refuses to start while another server or GPU process is running.
- **Three repetitions in alternated order.** For the q2 bars, r1 reused the exploration runs (16:04–16:43 UTC), and r2 and r3 ran back to back from 18:33 to 19:05: ablated → patch → default, then patch → default → ablated. The position within a round was default 1-3-2, patch 2-2-1, ablated 3-1-3: alternated, not balanced. Run-to-run goodput spread: default <!--n:q2.default.goodput_spread_pct-->0.24<!--/n-->%, ablated <!--n:q2.ablated.goodput_spread_pct-->1.24<!--/n-->%, patch <!--n:q2.mine.goodput_spread_pct-->0.05<!--/n-->%. All are well below the <!--n:q2.gain.goodput_pct-->+9.1<!--/n-->% difference.
- **Correctness in deterministic mode.** The output comparisons use `--enable-deterministic-inference --attention-backend triton`. Triton is required: deterministic mode with FlashInfer disables the radix cache, which would change the admission path under test.
- **Budget.** <!--n:budget.runs-->53<!--/n--> runs (<!--n:budget.runs_ok-->52<!--/n--> OK; <!--n:budget.runs_err-->1<!--/n--> with a client error was excluded and re-run), <!--n:budget.startup_s-->1,571<!--/n--> s of server start-up plus <!--n:budget.replay_s-->17,430<!--/n--> s of replay = <!--n:budget.gpu_hours-->5.28<!--/n--> GPU-h, plus two cross-workload replays of about 9 minutes each. The full timeline is in [docs/EXPERIMENTS.md](docs/EXPERIMENTS.md).

## Costs and when not to use it

**Where the wait goes.** With the patch, TTFT is bimodal: in each run <!--n:q2.ttft_bimodal.mine_lt1s-->60<!--/n--> requests start within 1 s and <!--n:q2.ttft_bimodal.mine_ge12s-->60<!--/n--> wait 12 s or more. The median therefore jumps between runs (<!--n:q2.mine.ttft_p50_min-->6.1<!--/n-->–<!--n:q2.mine.ttft_p50_max-->8.1<!--/n--> s), so the tables use the mean and the count of requests that waited more than 10 s ([`fig06_ttft_cdf.png`](docs/figures/fig06_ttft_cdf.png)). Short answers pay the most. The <!--n:q2.short_ttft_p50.n_requests-->9<!--/n--> requests with fewer than 1,000 output tokens see their TTFT p50 go from <!--n:q2.short_ttft_p50.default_s-->0.06<!--/n--> s to <!--n:q2.short_ttft_p50.mine_s-->31.6<!--/n--> s (head-of-line blocking behind long requests). End-to-end latency barely moves: E2E p50 <!--n:q2.default.e2e_p50_s-->86.8<!--/n--> → <!--n:q2.mine.e2e_p50_s-->91.2<!--/n--> s (<!--n:q2.gain.e2e_p50_pct-->+5.0<!--/n-->%), mean <!--n:q2.default.e2e_mean_s-->92.0<!--/n--> → <!--n:q2.mine.e2e_mean_s-->92.3<!--/n--> s (<!--n:q2.gain.e2e_mean_pct-->+0.3<!--/n-->%), p99 <!--n:q2.default.e2e_p99_s-->240.8<!--/n--> → <!--n:q2.mine.e2e_p99_s-->227.3<!--/n--> s (<!--n:q2.gain.e2e_p99_pct-->−5.6<!--/n-->%).

![Trade-off at 2 req/s: every setting that removes retractions raises the mean time to first token](docs/figures/fig04_tradeoff.png)

**Other loads and output distributions** (each cell: default / N = 40 / patch; "–" = not run; [`fig03_load_curve.png`](docs/figures/fig03_load_curve.png)):

| Condition | Goodput (req/s) | Patch vs default | Retractions | TTFT mean (s) | TTFT p99 (s) | Waited >10 s |
|---|---|---|---|---|---|---|
| 0.5 req/s (n=1) | <!--n:load.q0.5.default.goodput-->0.365<!--/n--> / – / <!--n:load.q0.5.mine.goodput-->0.364<!--/n--> | <!--n:load.q0.5.gain_pct-->0.0<!--/n-->% | <!--n:load.q0.5.retractions.default-->0<!--/n--> / – / <!--n:load.q0.5.retractions.mine-->0<!--/n--> | <!--n:load.q0.5.ttft_mean_s.default-->0.05<!--/n--> / – / <!--n:load.q0.5.ttft_mean_s.mine-->0.05<!--/n--> | <!--n:load.q0.5.ttft_p99_s.default-->0.08<!--/n--> / – / <!--n:load.q0.5.ttft_p99_s.mine-->0.08<!--/n--> | <!--n:load.q0.5.wait10.default-->0<!--/n--> / – / <!--n:load.q0.5.wait10.mine-->0<!--/n--> |
| 1 req/s (n=1) | <!--n:load.q1.default.goodput-->0.425<!--/n--> / <!--n:load.q1.tuned_n40.goodput-->0.416<!--/n--> / <!--n:load.q1.mine.goodput-->0.422<!--/n--> | <!--n:load.q1.gain_pct-->−0.7<!--/n-->% | <!--n:load.q1.retractions.default-->7<!--/n--> / <!--n:load.q1.retractions.tuned_n40-->1<!--/n--> / <!--n:load.q1.retractions.mine-->0<!--/n--> | <!--n:load.q1.ttft_mean_s.default-->13.7<!--/n--> / <!--n:load.q1.ttft_mean_s.tuned_n40-->18.8<!--/n--> / <!--n:load.q1.ttft_mean_s.mine-->16.2<!--/n--> | <!--n:load.q1.ttft_p99_s.default-->46.2<!--/n--> / <!--n:load.q1.ttft_p99_s.tuned_n40-->50.2<!--/n--> / <!--n:load.q1.ttft_p99_s.mine-->53.5<!--/n--> | <!--n:load.q1.wait10.default-->46<!--/n--> / <!--n:load.q1.wait10.tuned_n40-->63<!--/n--> / <!--n:load.q1.wait10.mine-->50<!--/n--> |
| 2 req/s (default, patch: median of 3; N = 40: n=1) | <!--n:q2.default.goodput-->0.403<!--/n--> / <!--n:q2.tuned_n40.goodput-->0.432<!--/n--> / <!--n:q2.mine.goodput-->0.440<!--/n--> | <!--n:q2.gain.goodput_pct-->+9.1<!--/n-->% | <!--n:q2.default.retractions-->19<!--/n--> / <!--n:q2.tuned_n40.retractions-->0<!--/n--> / <!--n:q2.mine.retractions-->0<!--/n--> | <!--n:q2.default.ttft_mean_s-->19.0<!--/n--> / <!--n:q2.tuned_n40.ttft_mean_s-->35.2<!--/n--> / <!--n:q2.mine.ttft_mean_s-->30.1<!--/n--> | <!--n:q2.default.ttft_p99_s-->64.2<!--/n--> / <!--n:q2.tuned_n40.ttft_p99_s-->93.5<!--/n--> / <!--n:q2.mine.ttft_p99_s-->92.3<!--/n--> | <!--n:q2.default.wait10-->43<!--/n--> / <!--n:q2.tuned_n40.wait10-->71<!--/n--> / <!--n:q2.mine.wait10-->60<!--/n--> |
| 4 req/s (n=1) | <!--n:load.q4.default.goodput-->0.378<!--/n--> / <!--n:load.q4.tuned_n40.goodput-->0.442<!--/n--> / <!--n:load.q4.mine.goodput-->0.449<!--/n--> | <!--n:load.q4.gain_pct-->+18.7<!--/n-->% | <!--n:load.q4.retractions.default-->29<!--/n--> / <!--n:load.q4.retractions.tuned_n40-->4<!--/n--> / <!--n:load.q4.retractions.mine-->0<!--/n--> | <!--n:load.q4.ttft_mean_s.default-->16.3<!--/n--> / <!--n:load.q4.ttft_mean_s.tuned_n40-->42.4<!--/n--> / <!--n:load.q4.ttft_mean_s.mine-->35.4<!--/n--> | <!--n:load.q4.ttft_p99_s.default-->75.6<!--/n--> / <!--n:load.q4.ttft_p99_s.tuned_n40-->107.1<!--/n--> / <!--n:load.q4.ttft_p99_s.mine-->89.2<!--/n--> | <!--n:load.q4.wait10.default-->34<!--/n--> / <!--n:load.q4.wait10.tuned_n40-->78<!--/n--> / <!--n:load.q4.wait10.mine-->60<!--/n--> |
| 2 req/s, σ = 0.3 (n=1) | <!--n:harm.s0.3.default.goodput-->0.492<!--/n--> / <!--n:harm.s0.3.tuned_n40.goodput-->0.495<!--/n--> / <!--n:harm.s0.3.mine.goodput-->0.497<!--/n--> | <!--n:harm.s0.3.gain_pct-->+0.9<!--/n-->% | <!--n:harm.s0.3.default.retractions-->27<!--/n--> / <!--n:harm.s0.3.tuned_n40.retractions-->0<!--/n--> / <!--n:harm.s0.3.mine.retractions-->0<!--/n--> | <!--n:harm.s0.3.default.ttft_mean_s-->27.7<!--/n--> / <!--n:harm.s0.3.tuned_n40.ttft_mean_s-->43.9<!--/n--> / <!--n:harm.s0.3.mine.ttft_mean_s-->42.0<!--/n--> | <!--n:harm.s0.3.default.ttft_p99_s-->97.0<!--/n--> / <!--n:harm.s0.3.tuned_n40.ttft_p99_s-->112.4<!--/n--> / <!--n:harm.s0.3.mine.ttft_p99_s-->101.6<!--/n--> | <!--n:harm.s0.3.default.wait10-->53<!--/n--> / <!--n:harm.s0.3.tuned_n40.wait10-->80<!--/n--> / <!--n:harm.s0.3.mine.wait10-->74<!--/n--> |
| 2 req/s, σ = 1.0 (n=1) | <!--n:harm.s1.0.default.goodput-->0.338<!--/n--> / <!--n:harm.s1.0.tuned_n40.goodput-->0.336<!--/n--> / <!--n:harm.s1.0.mine.goodput-->0.341<!--/n--> | <!--n:harm.s1.0.gain_pct-->+0.9<!--/n-->% | <!--n:harm.s1.0.default.retractions-->9<!--/n--> / <!--n:harm.s1.0.tuned_n40.retractions-->3<!--/n--> / <!--n:harm.s1.0.mine.retractions-->0<!--/n--> | <!--n:harm.s1.0.default.ttft_mean_s-->8.4<!--/n--> / <!--n:harm.s1.0.tuned_n40.ttft_mean_s-->19.0<!--/n--> / <!--n:harm.s1.0.mine.ttft_mean_s-->14.9<!--/n--> | <!--n:harm.s1.0.default.ttft_p99_s-->48.5<!--/n--> / <!--n:harm.s1.0.tuned_n40.ttft_p99_s-->59.6<!--/n--> / <!--n:harm.s1.0.mine.ttft_p99_s-->56.2<!--/n--> | <!--n:harm.s1.0.default.wait10-->23<!--/n--> / <!--n:harm.s1.0.tuned_n40.wait10-->59<!--/n--> / <!--n:harm.s1.0.mine.wait10-->36<!--/n--> |
| 2 req/s, mean output 1,000 (n=1) | <!--n:harm.m1000.default.goodput-->1.451<!--/n--> / <!--n:harm.m1000.tuned_n40.goodput-->1.452<!--/n--> / <!--n:harm.m1000.mine.goodput-->1.452<!--/n--> | <!--n:harm.m1000.gain_pct-->0.0<!--/n-->% | <!--n:harm.m1000.default.retractions-->0<!--/n--> / <!--n:harm.m1000.tuned_n40.retractions-->0<!--/n--> / <!--n:harm.m1000.mine.retractions-->0<!--/n--> | <!--n:harm.m1000.default.ttft_mean_s-->0.05<!--/n--> / <!--n:harm.m1000.tuned_n40.ttft_mean_s-->0.05<!--/n--> / <!--n:harm.m1000.mine.ttft_mean_s-->0.05<!--/n--> | <!--n:harm.m1000.default.ttft_p99_s-->0.07<!--/n--> / <!--n:harm.m1000.tuned_n40.ttft_p99_s-->0.07<!--/n--> / <!--n:harm.m1000.mine.ttft_p99_s-->0.07<!--/n--> | <!--n:harm.m1000.default.wait10-->0<!--/n--> / <!--n:harm.m1000.tuned_n40.wait10-->0<!--/n--> / <!--n:harm.m1000.mine.wait10-->0<!--/n--> |

- **Goodput rises only where retractions cost SLO misses.** At 4 req/s (n=1), <!--n:load.q4.gain_pct-->+18.7<!--/n-->% is SLO-met <!--n:load.q4.slo_met.default-->108<!--/n--> → <!--n:load.q4.slo_met.mine-->120<!--/n--> (×<!--n:load.q4.slo_factor-->1.111<!--/n-->) times wall time <!--n:load.q4.wall_s.default-->285.5<!--/n--> → <!--n:load.q4.wall_s.mine-->267.2<!--/n--> s (×<!--n:load.q4.wall_factor-->1.068<!--/n-->); without <!--n:q2.longest.rid-->reason-00064<!--/n--> it is <!--n:load.q4.excl_longest_gain_pct-->+12.5<!--/n-->%. Elsewhere goodput changes by about 1% or less, within single-run noise.
- **The TTFT cost appears wherever the gate fires.** At 1 req/s the <!--n:load.q1.gain_pct-->−0.7<!--/n-->% is wall time only: the last request, <!--n:load.q1.last_rid-->reason-00101<!--/n-->, was admitted later (TTFT <!--n:load.q1.last_ttft_s.default-->27.6<!--/n--> → <!--n:load.q1.last_ttft_s.mine-->34.2<!--/n--> s). Default and ablated differ by <!--n:load.q1.ablated_vs_default_pct-->−0.5<!--/n-->% at the same load, so the goodput change is noise. The TTFT p99 change is not: <!--n:load.q1.ttft_p99_pct-->+15.9<!--/n-->%.
- **A fixed cap needs retuning.** N = 40, picked at 2 req/s, retracts again at 1 req/s, 4 req/s and σ = 1.0. None of the α = 1 server logs shows a retraction (<!--n:reretract.mine_a1.0.logs_without_retraction-->13/13<!--/n-->). Its TTFT mean is lower than N = 40's in all five conditions with retractions. At 1 req/s, though, N = 40 has the lower TTFT p99.
- **Post-hoc check (not part of the trace's SLO).** If requests also had to start within 10 s, default would win. Goodput at 2 req/s would be <!--n:slo_ttft10.q2.default_range-->0.252–0.256<!--/n--> for default (3 runs) against <!--n:slo_ttft10.q2.mine-->0.220<!--/n--> for the patch, and at 4 req/s <!--n:slo_ttft10.q4.default-->0.270<!--/n--> against <!--n:slo_ttft10.q4.mine-->0.225<!--/n--> (n=1). This SLO was added after the results were seen.

**When to use it and when not to.**

- *Consider it* for long generations with known or tightly bounded lengths under KV pressure, where an answer that stops for a minute is worse than a slower start.
- *Do not use it as-is* with a TTFT SLO (interactive chat). At 2 req/s the TTFT p99 change was <!--n:q2.gain.ttft_p99_pct-->+43.6<!--/n-->%.
- *Do not use it as-is* when lengths are unknown (EOS-terminated requests). `max_new_tokens` is then only an upper bound and the gate over-reserves, down to two concurrent requests without `max_tokens`.
- *Nothing to gain at low load.* Below about <!--n:diag.first_retraction_qps-->0.6<!--/n--> req/s on this setup nothing is retracted and the gate never fires.
- *Short shared prefixes.* With a prefix as short as this one (<!--n:trace.shared_prefix_tokens-->36<!--/n--> tokens), `--disable-radix-cache` gets most of the benefit without a patch.

## Correctness

Output comparisons in deterministic mode (`--enable-deterministic-inference --attention-backend triton`, n=1 per run). The course's verify tool passes a comparison at 95% identical outputs.

| Compared runs | Identical outputs | What it shows |
|---|---|---|
| unpatched at 0.2 req/s (no retractions) vs patch at 2 req/s | <!--n:correct.det_q02up_vs_q2mine-->120/120<!--/n--> | the gate does not change any output |
| unpatched vs patched build with the flag off, both at 0.2 req/s | <!--n:correct.det_q02up_vs_q02off-->120/120<!--/n--> | flag off = upstream |
| unpatched vs unpatched, 2 req/s, runs r1 and r2 | <!--n:correct.det_q2up_r1_vs_r2-->115/120<!--/n--> | retraction perturbs outputs even between identical runs; the differing requests are all retraction victims (<!--n:correct.det_q2up_r1_vs_r2_mismatches_in_victims-->5/5<!--/n-->) |
| unpatched vs patch, both at 2 req/s | <!--n:correct.det_q2up_vs_q2mine-->98/120<!--/n--> (<!--n:correct.det_q2up_vs_q2mine_pct-->81.7<!--/n-->%) | the differing requests (<!--n:correct.mismatches_in_victims-->22/22<!--/n-->) are all among the <!--n:correct.det_q2up_victims-->30<!--/n--> requests that the unpatched run retracted |

In ordinary (non-deterministic) mode, two flag-off runs at the same load agree on only <!--n:correct.nondet_q2default_r1_vs_r2-->20/120<!--/n--> outputs, so that mode cannot serve as a correctness gate. The verify outputs are in [`data/verify/`](data/verify/), and per-request output hashes are in `data/derived/per_request.csv.gz` (`output_sha16`).

The [CPU tests](tests/README.md) check the gate's logic against the unmodified SGLang 0.5.18 file in [`third_party/`](third_party/sglang_v0_5_18/). Where the two paths should coincide, the gate's verdict equals upstream `add_one_req_ignore_eos`, run verbatim, in <!--n:patch.equiv_upstream-->19,907/19,907<!--/n--> random states. It equals a token-by-token brute-force simulation in <!--n:patch.equiv_bruteforce-->20,000/20,000<!--/n--> states. Admitted states run without a page-allocation failure in a simulated paged pool in <!--n:patch.equiv_paged_physical-->18,277/18,277<!--/n-->. The tests cover the gate's logic only, not GPU behavior or scheduler integration.

## FAQ

**1. Isn't this already in SGLang?**
Half of it. The same finish-order simulation exists as `add_one_req_ignore_eos`, but SGLang uses it only when the radix cache is disabled. The default path reserves `min(remaining, 4096) × new_token_ratio` per running request, about 400 tokens once the ratio reaches its floor of 0.098. On this workload `--disable-radix-cache` alone recovers <!--n:q2.share_of_gain.noradix_pct-->88.0<!--/n-->% of the gain (n=1). This project adds four things: the diagnosis that the exact path is off by default and that this causes minute-long stalls, a port that works with the radix cache on, a check that the port matches upstream (<!--n:patch.equiv_upstream-->19,907/19,907<!--/n-->), and measurements of both gains and costs.

**2. Isn't it an oracle?**
Yes. Every trace sets `ignore_eos=True`, so `max_new_tokens` is the true output length and the gate reserves exactly that. Read the results as an upper bound for schemes that have to guess lengths. With unknown lengths, `max_new_tokens` becomes about 32k and the α = 1 gate admits only two such requests together (CPU check, not measured on GPU). α is not an error model either. α < 1 simulates a world in which every request is shorter by that factor. At 2 req/s, α = 0.5 and α = 0.6 never delayed an admission (logged delays: <!--n:q2.mine_a0.5.peak_delays-->0<!--/n--> and <!--n:q2.mine_a0.6.peak_delays-->0<!--/n-->) and behaved like default (<!--n:q2.mine_a0.5.retractions-->19<!--/n--> and <!--n:q2.mine_a0.6.retractions-->20<!--/n--> retractions, n=1).

**3. Why does half of the <!--n:q2.gain.goodput_pct-->+9.1<!--/n-->% come from one request?**
Goodput divides SLO-met requests by wall time, and wall time ends when the last request finishes. In all three default runs that is <!--n:q2.longest.rid-->reason-00064<!--/n-->, the longest answer (<!--n:q2.longest.out_tokens-->10,541<!--/n--> tokens). It is retracted and stalls for <!--n:q2.longest.default_stall_range_s-->61.6–61.8<!--/n--> s. With the patch it waits <!--n:q2.longest.mine_ttft_s-->32.1<!--/n--> s before starting, then streams without a pause, and wall time drops from <!--n:q2.default.wall_s-->285.2<!--/n--> s to <!--n:q2.mine.wall_s-->272.8<!--/n--> s. Without that request the gain is <!--n:q2.excl_longest.gain_pct-->+4.4<!--/n-->%. The robust results are the retractions (<!--n:q2.default.retractions-->19<!--/n--> → <!--n:q2.mine.retractions-->0<!--/n-->) and the p99 worst token gap (<!--n:q2.default.maxitl_p99_s-->63.1<!--/n--> s → <!--n:q2.mine.maxitl_p99_s-->0.11<!--/n--> s).

**4. Why not just set `--max-running-requests` or `--schedule-conservativeness`?**
A fixed cap has to be retuned for each load and length mix. N = 40, picked at 2 req/s by the preregistered rule, retracts again at 1 req/s, 4 req/s and σ = 1.0 (<!--n:reretract.tuned_n40.q1-->1<!--/n-->, <!--n:reretract.tuned_n40.q4-->4<!--/n--> and <!--n:reretract.tuned_n40.s1.0-->3<!--/n--> times), and N = 24 has a TTFT p99 of <!--n:q2.tuned_n24.ttft_p99_s-->156.3<!--/n--> s at 2 req/s (all n=1). `--schedule-conservativeness` and the `SGLANG_INIT_NEW_TOKEN_RATIO`, `SGLANG_MIN_NEW_TOKEN_RATIO_FACTOR` and `SGLANG_CLIP_MAX_NEW_TOKENS_ESTIMATION` variables were **not measured**. From the code: conservativeness only scales the initial ratio (capped at 1.0), while the floor (0.14 × the initial ratio) and the 4,096-token clip stay, so long requests would still be under-reserved. Measuring this is the first follow-up GPU experiment.

**5. What is the causal evidence that KV capacity is the bottleneck?**
Four pieces. Retractions and stalls longer than 5 s match one to one (<!--n:diag.stall_match-->89/89<!--/n-->). Each stall equals the wait from retraction to re-prefill (within <!--n:diag.reprefill_delay_vs_stall_max_s-->0.90<!--/n--> s). The Little's-law bound (<!--n:diag.little_lambda_max-->0.56–0.67<!--/n--> req/s) matches the onset (<!--n:diag.first_retraction_qps-->0.6<!--/n--> req/s). And at 2 req/s, every way of admitting more conservatively (the patch, `--disable-radix-cache`, N ≤ 40) removes the retractions, while turning chunked prefill off changes nothing. Shrinking the pool made the tail worse, but that is one run, so it counts as medium-strength evidence.

**6. Why does the correctness check say 81.7%, FAIL?**
That row compares unpatched SGLang with the patch at the same load in deterministic mode: <!--n:correct.det_q2up_vs_q2mine-->98/120<!--/n--> outputs are identical. The differing requests (<!--n:correct.mismatches_in_victims-->22/22<!--/n-->) are all among the <!--n:correct.det_q2up_victims-->30<!--/n--> requests that the unpatched run retracted; retraction and re-prefill change the numerics. Compared with an unpatched run without retractions (0.2 req/s), the patched outputs match <!--n:correct.det_q02up_vs_q2mine-->120/120<!--/n-->.

**7. Whose idea was it?**
The direction, reserving peak KV conservatively at admission, came from the course's hint. The rest is my work from the course: finding that SGLang already computes this exactly for `ignore_eos` requests but only with the radix cache off, porting it to the radix-cache path, the diagnosis, the experiments and the analysis. The equivalence tests and this post-submission audit were done with AI assistance ([Tools used](#tools-used)). The course repository's instructor-only solution folder (`bp/step3/`) was not consulted, as the header of the preregistered plan records.

## Reproduce

Steps A to D run on a CPU-only machine with Python 3.12. Step E needs a GPU.

**A. Apply the patch** to SGLang v0.5.18. [`patch/README.md`](patch/README.md) also covers an installed wheel (`patch -p2`) and how to revert.

```bash
git clone https://github.com/sgl-project/sglang.git && cd sglang
git checkout v0.5.18                       # commit 71de97b
git apply /path/to/sglang-peak-kv-admission/patch/peak_kv_reservation.diff
# then add these flags to your usual launch command:
#   --enable-peak-kv-reservation --peak-kv-reserve-ratio 1.0
```

**B. CPU tests** (standard library only, under a minute; [`tests/README.md`](tests/README.md)):

```bash
python3 tests/run_cpu_tests.py                 # prints "all peak-KV unit tests passed"
python3 tests/test_upstream_equivalence.py     # add --full for 20,000 states per check (about 40 s)
```

**C. Numbers, tables and figures from `data/derived/`** (in git; needs numpy, and matplotlib for the figures):

```bash
python3 -m w3.select_star               # last line: ASTAR=1.0 NSTAR=40
python3 -m analysis.headline --check    # artifacts/numbers.json is up to date with data/derived/
python3 -m w3.figures                   # redraws docs/figures/*.png
python3 tools/check_numbers.py          # every number marker in the docs matches artifacts/*.json
```

**D. Raw data** from the GitHub Release `data-v1` (61 MB download, 428 MB unpacked; see [`data/RAW.md`](data/RAW.md)):

```bash
bash tools/fetch_raw.sh                 # download, verify, unpack into results/ and logs/
python3 -m w3.export_per_request        # rebuild data/derived/ from the raw files
python3 -m analysis.retract_cost --sweep-0907 --out data/derived/retract_cost --force
python3 -m analysis.headline            # rewrite artifacts/numbers.json
```

The complete rebuild sequence, which also regenerates the CSV tables byte for byte, is in [docs/EXPERIMENTS.md](docs/EXPERIMENTS.md#regenerating-the-derived-data).

**E. Re-run on a GPU** (24 GB class such as an RTX 4090, with `sglang==0.5.18` installed and patched as in A). The course harness is fetched from its public repository and is not redistributed here:

```bash
bash tools/fetch_course_harness.sh      # course harness at 802a164 into .course/ (verified, gitignored)
export PYTHONPATH=$PWD/.course/project
python -m workloads.generators reasoning --out traces/ --model Qwen/Qwen3-4B --set qps=2 --suffix _q2
sha256sum -c --ignore-missing data/traces.sha256
bash w3/run_one.sh traces/reasoning_q2.jsonl results mine_a1.0_r1 \
     --enable-peak-kv-reservation --peak-kv-reserve-ratio 1.0
bash w3/run_queue.sh w3/queues/p4_final.txt   # the final 3-bar repetitions r2 and r3 (6 runs)
```

`w3/run_one.sh` appends a line to `w3/runs.tsv`, the as-run ledger in git. [`data/traces.sha256`](data/traces.sha256) lists the generator command for each trace. If the server log reports a KV pool other than 87,552 tokens (another GPU or driver), the knee moves.

Maintainers: `git config core.hooksPath .githooks` enables the pre-push hook, and `bash tools/prepublish_check.sh` runs the publication checks ([`tools/README.md`](tools/README.md)).

## Limitations

- **Oracle lengths.** All traces use `ignore_eos=True`. Behavior with unknown output lengths was not measured.
- **Few repetitions.** Only the three q2 bars (and the 0.35 req/s unpatched baseline) have 3 runs; everything else is n=1, from one trace seed. Differences of 1–2% between single runs, for example patch vs. N = 40 vs. `--disable-radix-cache`, are not evidence.
- **One setup.** One GPU (RTX 4090), one model (Qwen3-4B), one SGLang version (0.5.18). A different KV capacity moves the knee.
- **Not measured.** `--schedule-conservativeness`, the new-token-ratio environment variables, an FP8 KV cache, and EOS-terminated traffic.
- **Cross-workload check.** The five-workload replay ran at low load and the gate fired once (<!--n:cross.gate_fired-->1<!--/n--> logged delay), so it is inconclusive. It does not show that other workloads are unaffected.
- **Short shared prefix.** At <!--n:trace.shared_prefix_tokens-->36<!--/n--> tokens, this workload cannot show what keeping the radix cache on is worth.
- **Confounds.** Decode CUDA graphs are captured only for batches ≤ 24. The share of decode steps on a graph was <!--n:q2.default.graph_pct-->55.3<!--/n-->% for default and <!--n:q2.mine.graph_pct-->50.9<!--/n-->% for the patch, so the patch's gain is not a graph effect. N = 24 ran on graphs <!--n:q2.tuned_n24.graph_pct-->100.0<!--/n-->% of the time, which flatters its TPOT. The ablated bar also turns off prefill CUDA graphs.
- **Order of work.** The patch was written after the load-sweep diagnosis but before the 2 req/s controls (smaller pool, chunked prefill off) were run. Both are in the timeline in [docs/EXPERIMENTS.md](docs/EXPERIMENTS.md).
- **Test scope.** The CPU tests check the gate's logic, not GPU behavior or the scheduler integration.

## Repository layout

```text
patch/         the measured diff, its SHA256SUMS, README (flags, apply/revert, errata notes)
third_party/   one unmodified SGLang 0.5.18 file and its LICENSE, used only by the CPU tests
tests/         CPU tests: unit tests, upstream equivalence, brute force, mutation check
w3/            experiment scripts (run_one.sh, run_queue.sh, queues/), metrics, summaries, figures
analysis/      retraction-to-stall matching, headline numbers
artifacts/     numbers.json and equivalence.json: every number the docs quote
data/          derived tables, verify outputs, run ledger, sample logs, trace hashes (raw data: Release)
docs/          REPORT.md, REPORT.ko.md, EXPERIMENTS.md, figures/, preregistration/
tools/         number checker, pre-publication checks, raw-data and course-harness fetchers
ERRATA.md      corrections to the submitted report
```

Data files are described in [`data/README.md`](data/README.md).

## Credits and provenance

- The study began as an individual assignment in a 5-week LLM inference-engine study (August–September 2026). The course repository is public: [mlleo/inference-engine-study](https://github.com/mlleo/inference-engine-study) (it has no license file; the study used commit `802a164`).
- The course provided the workload definition, the trace generator, the replay and metrics harness, the experiment protocol, and the hint to explore peak-KV reservation at admission.
- None of the course's files are in this repository. [`tools/fetch_course_harness.sh`](tools/fetch_course_harness.sh) fetches the harness for GPU re-runs only. [`w3/metrics.py`](w3/metrics.py) re-implements the metric formulas from their specification and was checked against the harness as a black box.
- The course repository's instructor-only solution folder (`bp/step3/`) was not consulted; the preregistered plan says so in its header.
- SGLang (Apache-2.0, Copyright SGLang Team) and Qwen3-4B (Apache-2.0, Copyright Alibaba Cloud): see [NOTICE](NOTICE) and [THIRD_PARTY.md](THIRD_PARTY.md). This repository is not affiliated with the SGLang project.

## Tools used

The original study — patch, experiments, analysis scripts and report — was done during the course. After submission, an AI coding assistant ([Claude Code](https://claude.com/claude-code)) was used to audit the results, write the clean-room metrics module and equivalence tests, regenerate the figures and draft these corrected documents. The author reviewed the content before publication. Commits made with the assistant carry a `Co-Authored-By: Claude` line.

## License

- Code, the patch and the tests: Apache License 2.0 ([LICENSE](LICENSE)).
- Documentation, figures and derived data: CC BY 4.0 ([LICENSE-docs](LICENSE-docs) lists the paths).
- Third-party material keeps its own license ([NOTICE](NOTICE), [THIRD_PARTY.md](THIRD_PARTY.md)). The raw-data release contains text generated by Qwen3-4B (Apache-2.0).
