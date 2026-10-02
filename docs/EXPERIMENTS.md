# Experiment log

This page records how the measurements were made: the order of the runs, the GPU budget, the failed run, the server flags and configurations, how to re-run everything, and where the runs departed from the preregistered plan. All times are UTC. The run order and durations come from the as-run ledgers [`w3/runs.tsv`](../w3/runs.tsv) (one line per run) and [`data/ledger/w3_queue.log`](../data/ledger/w3_queue.log) (the queue runner's log). Neither file has been edited.

## Timeline

| When (UTC) | What happened | Data (era label) |
|---|---|---|
| 2026-09-03 | Setup. A first harness test replayed the base trace before the server was ready, so all 120 requests failed to connect. Not a measurement. | `reasoning__batch_test` (0903, excluded) |
| 2026-09-06 | Unpatched baseline at 0.35 req/s. The run shown in the submitted report's baseline table was overwritten by a re-run later that day. | `reasoning__default` (0906) |
| 2026-09-07 | Unpatched exploration. Ablations at 0.35 req/s (chunked prefill off, CUDA graphs off; no server logs kept), then the load sweep: 0.5, 1, 2, 4 and 8 req/s (09:49–10:17), then 0.6 and 0.8 req/s (11:17–11:28). | `reasoning__{ablated,nograph}`, `reasoning_q*__default` (0907) |
| 2026-09-13, morning | `--disable-radix-cache` at 0.35 req/s (09:58, from the file time). Retraction-cost analysis of the sweep. At 12:22 a re-run of that ablation started without a server and overwrote its result file; the original survives as a backup. | `reasoning__noradix` (0913, restored) |
| 2026-09-13 13:59:57 | The plan was written: [docs/preregistration/plan_2026-09-13.md](preregistration/plan_2026-09-13.md). | |
| 2026-09-13 15:03–20:26 | <!--n:budget.runs-->53<!--/n--> runs of [`w3/run_one.sh`](../w3/run_one.sh) from six queue files (next section). The patch file is dated 15:06:56. It was applied to the installed SGLang at 16:04, after the unpatched queue. | `w3/runs.tsv` (0913, `det/`) |
| 2026-09-13 20:26–20:44 | Cross-workload replay with the course's script, once without and once with the gate. | `cross/` (cross) |
| 2026-09-17 | Report submitted (11-page PDF; see [ERRATA.md](../ERRATA.md)). | |
| 2026-10 | Post-submission audit and this repository. | |

The 2026-09-03 to 09-13-morning entries are reconstructed from file times. From 15:03 on 09-13, every run is in the ledgers.

## Queues and run counts (2026-09-13)

The queues ran back to back in this order (the queue files are in [`w3/queues/`](../w3/queues/)). The order differs from the file names: `p5p7_indep` ran before `p4_final`.

| # | Queue | Start–end | Runs | Contents |
|---|---|---|---|---|
| 1 | `u_upstream.txt` | 15:03:02–16:04:35 | 7 | **Unpatched SGLang.** Deterministic q2 ×2 (the plan's reproducibility check), 0.35 req/s ×3 (baseline), q2 ×1 (unpatched reference for the flag-off build), deterministic q0.2 ×1 (retraction-free reference for the output comparisons) |
| – | patch applied | 16:04:43–16:04:55 | – | The patch was applied to the installed SGLang and smoke-tested. The chaining script was not kept; the ledger records both steps. |
| 2 | `p2_explore.txt` | 16:04:55–16:42:59 | 7 | q2: default, α = 1.0, α = 0.5, `--disable-radix-cache`, N = 32, KV pool 65,536, ablated (all r1) |
| 3 | `p3_pareto.txt` | 16:42:59–17:15:33 | 6 | q2: α = 0.75 / 0.9 / 0.6, N = 24 / 40 / 48. The selection rule gives α* = <!--n:select.alpha_star-->1.0<!--/n-->, N* = <!--n:select.n_star-->40<!--/n--> |
| 4 | `p5p7_indep.txt` | 17:15:33–18:33:14 | 12 | Flag off: default and ablated at 0.5 / 1 / 4 req/s, ablated at 0.35 req/s, default at σ = 0.3 / σ = 1.0 / mean output 1,000, deterministic flag-off at q2 and q0.2 |
| 5 | `p4_final.txt` | 18:33:15–19:05:13 | 6 | Final q2 3-bar, r2 (ablated → patch → default) and r3 (patch → default → ablated) |
| 6 | `p5p7_mine.txt` | 19:05:13–20:26:16 | 15 | α = 1.0 at 0.35 / 0.5 / 1 / 4 req/s, σ = 0.3 / σ = 1.0 / mean output 1,000 and deterministic q2; N = 40 at 1 / 4 req/s, σ = 0.3 / σ = 1.0 / mean output 1,000; the re-run of the failed q1 ablated run; the page-size 16 smoke run |
| 7 | cross replay | 20:26:16–20:44:24 | 2 | The course's cross-replay script: five scaled-down traces on one server, default, then α = 1.0 |

Runs 1–6 total <!--n:budget.runs-->53<!--/n-->. `w3/make_queues.sh <ASTAR> <NSTAR>` wrote queues 5 and 6 after the selection in queue 3. The r1 runs of the final 3-bar are the q2 runs of queue 2. The tuned (N = 40) and `--disable-radix-cache` references at q2 also come from queues 2 and 3.

## Budget

| Item | Value |
|---|---|
| Runs of `w3/run_one.sh` | <!--n:budget.runs-->53<!--/n--> (<!--n:budget.runs_ok-->52<!--/n--> OK, <!--n:budget.runs_err-->1<!--/n--> with a client error) |
| Server start-up | <!--n:budget.startup_s-->1,571<!--/n--> s in total (25–69 s per run) |
| Replay | <!--n:budget.replay_s-->17,430<!--/n--> s in total (85–765 s per run) |
| GPU time | <!--n:budget.gpu_hours-->5.28<!--/n--> h (start-up + replay) |
| Not included | the cross replay, two runs of about 9 minutes each |

The plan had budgeted about 45 runs and 5–6 GPU-hours. The extra runs are listed under [deviations](#deviations-from-the-preregistered-plan).

## The failed run and its re-run

`reasoning_q1__ablated_r1` (17:33–17:38) finished with 1 of 120 requests failed: `reason-00077` got `ClientOSError: [Errno 104] Connection reset by peer` from the client. `w3/run_one.sh` marked the run `ERR1` in `w3/runs.tsv`. The result was excluded and moved, with its server, metrics and replay logs, to `results/failed/` and `logs/failed/` (file names end in `.err1`). It was re-run at the end of queue 6 (20:15–20:21) with status OK; that re-run is the q1 ablated result used everywhere.

The queue log shows `rc=0` for the failed run, as for every run. `w3/run_queue.sh` read `$?` after a `$(date)` substitution, so it always logged 0. The script is fixed in this repository (it now saves the exit code first). The as-run log is kept unchanged.

## Server flags

Every run started a fresh server through `w3/run_one.sh` with these common flags:

```text
--model-path Qwen/Qwen3-4B --port 30000 --context-length 32768
--mem-fraction-static 0.85 --random-seed 42 --log-level info --enable-metrics
```

Resolved values (from the `server_args` line of [`data/logs_sample/server_reasoning_q2__default_r1.log`](../data/logs_sample/server_reasoning_q2__default_r1.log)): FlashInfer attention, `chunked_prefill_size=2048`, `schedule_policy='fcfs'`, `page_size=1`, `schedule_conservativeness=1.0`, `retraction_policy='length'`, `max_running_requests=None`. Decode CUDA graphs are captured for batch sizes 1–24 only. The KV pool holds <!--n:kv.pool_tokens-->87,552<!--/n--> tokens in bf16 (K and V 6.01 GB each). The software was SGLang 0.5.18 (`71de97b`) and Qwen/Qwen3-4B at revision `1cfa9a7208912126459214e8b04321603b3df60c`, on one RTX 4090 24 GB.

Each run: start the server, wait for `/health_generate`, replay the trace open-loop with 3 warm-up requests, save `/metrics`, stop the server and wait until GPU memory is free. `w3/run_one.sh` refuses to start while a server answers on the port or more than 1,000 MiB of GPU memory is in use.

## Configurations ("bars")

The tag in each result name is the bar plus a repetition suffix `_rN`. The flags below are added to the common flags.

| Bar | Extra flags | What it is |
|---|---|---|
| `upstream` | none, unpatched SGLang | Queue 1 only, before the patch was applied |
| `default` | none | 09-13: the patched build with the flag off. In the 09-06 and 09-07 data (`reasoning__default`, `reasoning_q*__default`) it is unpatched SGLang |
| `default_off` | none | The flag-off build in deterministic runs (`det/`) |
| `ablated` | `--chunked-prefill-size -1` | The protocol's ablation. It also turns off prefill CUDA graphs (resolved `max_bs=-1`) |
| `mine_a<α>` | `--enable-peak-kv-reservation --peak-kv-reserve-ratio <α>` | The patch. α = 1.0 everywhere; 0.5, 0.6, 0.75 and 0.9 at q2 only |
| `mine_a1.0_p16` | the above with α = 1.0, plus `--page-size 16` | Page-alignment smoke run |
| `tuned_n<N>` | `--max-running-requests <N>` | A fixed concurrency cap, the protocol's tuned fourth control. N = 24, 32, 40, 48 at q2; N = 40 elsewhere |
| `noradix` | `--disable-radix-cache` | SGLang's existing exact-length path (q2 on 09-13; 0.35 req/s on the morning of 09-13) |
| `cap65k` | `--max-total-tokens 65536` | KV pool cut to <!--n:kv.cap65k_pool_tokens-->65,536<!--/n--> tokens (capacity control) |
| `nograph` | CUDA graphs off (exact flag not logged) | 09-07 ablation at 0.35 req/s only |
| `batch_test` | none | 09-03 harness test with no server; excluded |
| `det/…` | `--enable-deterministic-inference --attention-backend triton` | Output comparisons. Triton is required: deterministic mode with FlashInfer disables the radix cache |
| `cross/<w>_x__default`, `cross/<w>_x__mine` | none, or the patch with α = 1.0 | The cross replay of five scaled-down workload traces on one server per configuration |

## Traces

All traces come from the course's generator at commit `802a164` and set `ignore_eos=True`. Their sha256 values and generator commands are in [`data/traces.sha256`](../data/traces.sha256); the files themselves are not redistributed.

| Trace | Arrivals (req/s) | Output lengths | Used by |
|---|---|---|---|
| `reasoning` | 0.35 | base (generator mean 3,000) | 09-06 and 09-07 runs; 09-13 baseline and the 0.35 req/s comparisons |
| `reasoning_q0.2` | 0.2 | base | deterministic retraction-free reference |
| `reasoning_q0.5` … `reasoning_q8` | 0.5, 0.6, 0.8, 1, 2, 4, 8 | base | 09-07 sweep; 0.5 / 1 / 2 / 4 again on 09-13 |
| `reasoning_q2_s0.3`, `reasoning_q2_s1.0` | 2 | narrower (σ = 0.3) or wider (σ = 1.0) spread | condition tests |
| `reasoning_q2_m1000` | 2 | generator mean 1,000 | condition test |
| `rag_x`, `agent_x`, `reasoning_x`, `structured_x`, `mixed_x` | generator defaults per workload | per workload | cross replay; `--scale 0.3` keeps 30% of each workload's requests |

## Re-running on a GPU

1. **Hardware and software.** A 24 GB GPU (the study used an RTX 4090), `sglang==0.5.18`, and Qwen/Qwen3-4B (the study used revision `1cfa9a72…`). For the patched runs, apply [`patch/peak_kv_reservation.diff`](../patch/peak_kv_reservation.diff) as described in [`patch/README.md`](../patch/README.md). Queue 1 (`u_upstream.txt`) needs the unpatched install; every other queue needs the patched one.
2. **The course harness.** Run `bash tools/fetch_course_harness.sh`, then `export PYTHONPATH=$PWD/.course/project`. It provides `bench.replay`, which `w3/run_one.sh` calls, and the trace generator. It is fetched, verified and gitignored, never committed.
3. **Traces.** Generate them into `traces/` with the commands in `data/traces.sha256`, then check them with `sha256sum -c --ignore-missing data/traces.sha256`. The prompt texts depend on the tokenizer revision.
4. **Runs.** From the repository root:

   ```bash
   bash w3/run_one.sh traces/reasoning_q2.jsonl results default_r1                  # one run, flag off
   bash w3/run_one.sh traces/reasoning_q2.jsonl results mine_a1.0_r1 \
        --enable-peak-kv-reservation --peak-kv-reserve-ratio 1.0                    # one run, gate on
   bash w3/run_queue.sh w3/queues/p2_explore.txt                                   # a whole queue
   ```

   `w3/run_queue.sh` skips results that already exist without errors, so an interrupted queue can be resumed with the same command. `touch w3/STOP` stops it after the current run. Each run writes `results/<trace>__<tag>.json` and `logs/{server,metrics,replay}_<trace>__<tag>.*`, plus `results/det/` and `logs/det/` for deterministic runs, and appends a line to `w3/runs.tsv`. That file is the as-run ledger in git, so restore it with `git checkout w3/runs.tsv` if the new runs should not be committed.
5. **Comparability.** Check `max_total_num_tokens` in the server log. If it is not <!--n:kv.pool_tokens-->87,552<!--/n--> (another GPU, driver or memory setting), the load at which retractions start moves, and the numbers will not match.
6. **Summaries.** `python3 -m w3.summarize 'results/reasoning_q2__*_r*.json' --group` prints the median [min–max] table. The commands below rebuild every derived file.

## Regenerating the derived data

From the repository root, with the raw data unpacked by `bash tools/fetch_raw.sh`:

```bash
python3 -m w3.summarize 'results/reasoning*__*_r*.json' --csv data/derived/w3_runs.csv
python3 -m w3.summarize 'results/det/*.json' --csv data/derived/w3_det_runs.csv
python3 -m w3.summarize 'results/reasoning_q2__*_r*.json' --group --csv data/derived/w3_q2_grouped.csv
python3 -m w3.summarize 'results/reasoning_q2__default_r*.json' 'results/reasoning_q2__ablated_r*.json' \
    'results/reasoning_q2__mine_a1.0_r*.json' --std-csv data/derived/reasoning_q2_3bar_runs.csv
python3 -m w3.summarize 'results/reasoning_q*__default.json' --sweep-csv data/derived/sweep_0907.csv
python3 -m w3.export_per_request          # per_request.csv.gz, cum_output.csv.gz, server_logs.csv
python3 -m analysis.retract_cost --sweep-0907 --out data/derived/retract_cost --force
python3 -m analysis.headline              # artifacts/numbers.json
python3 -m w3.figures                     # docs/figures/*.png (needs matplotlib)
python3 tools/check_numbers.py            # the numbers in the docs still match
```

On 2026-10-02 the CSV and `.csv.gz` outputs and `retract_cost/{events,summary}.csv` came out byte-identical to the committed files, and the whole sequence took about a minute of CPU time. Without the raw data, `python3 -m analysis.headline --check` confirms that `artifacts/numbers.json` matches `data/derived/`.

## Deviations from the preregistered plan

The [plan](preregistration/plan_2026-09-13.md) was written before the first run and is published as written. The runs differed from it as follows:

1. **α grid.** The plan listed α ∈ {0.25, 0.5, 1.0}. The runs used 0.5, 0.6, 0.75, 0.9 and 1.0. The reason is recorded in `w3/queues/p3_pareto.txt`: at α = 0.5 the gate delayed no admission and followed the default trajectory, and rejection is monotone in α, so α = 0.25 could not fire either. The range 0.5–1.0 was sampled more densely instead. The selection rule itself was applied as written.
2. **Small W4 trace replaced by the cross replay.** The plan's "W4 small trace" condition was not run on its own. The structured-output workload was covered by the five-workload cross replay, which ran without the grammar constraint.
3. **CUDA-graph control not run.** The plan allowed a common `--cuda-graph-max-bs 96` for every bar if graphs became a confound. It was not run; the share of decode steps on a graph is reported per bar instead (<!--n:q2.default.graph_pct-->55.3<!--/n-->% for default and <!--n:q2.mine.graph_pct-->50.9<!--/n-->% for the patch at q2).
4. **r1 reused from exploration.** The plan had the final 3-bar as three new runs per bar plus new tuned and `--disable-radix-cache` runs. In practice r1 and both references reused the queue-2 and queue-3 runs at the same settings, and only r2 and r3 were new. The order was alternated but not balanced (default 1-3-2, patch 2-2-1, ablated 3-1-3).
5. **Gate details.** The plan's pseudo-code subtracted `prefix_indices` for every request and treated an empty running batch as idle. The measured patch uses `cached_tokens` for running requests, because after prefill `prefix_indices` covers the whole 387-token prompt and the first version over-reserved. It also skips finished requests and treats the server as idle only when no unfinished request is running and nothing was admitted this round.
6. **More runs.** <!--n:budget.runs-->53<!--/n--> runs instead of about 45: ablated at every load, N = 40 at all five pressure conditions, the page-size 16 smoke run and one re-run. GPU time stayed within the planned 5–6 hours (<!--n:budget.gpu_hours-->5.28<!--/n-->).

The correctness gate in the plan (P1) passed as specified. Two deterministic unpatched q2 runs matched in <!--n:correct.det_q2up_r1_vs_r2-->115/120<!--/n--> outputs, at or above the course tool's 95% mark.
