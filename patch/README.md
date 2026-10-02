# `peak_kv_reservation.diff`

`peak_kv_reservation.diff` is the patch every patched run in this study used, byte for byte. Its sha256 is <!--n:patch.sha256-->bb3d8657c653efa5d48f7163994e2206099171503b6e289ef3820b12774c65c9<!--/n--> (also in `SHA256SUMS`).

It equals the study's own copy, last modified on 2026-09-13 before the first patched run that day. It has not been edited for this repository, not even to fix the wording problem in note C3 below. The server logs of the patched runs contain the log line this diff defines (see [`data/logs_sample/`](../data/logs_sample/)).

The same diff is also committed on top of the `v0.5.18` tag in the fork [`HwaminJung00/sglang`, branch `peak-kv-admission`](https://github.com/HwaminJung00/sglang/tree/peak-kv-admission) (commit [`5157a47`](https://github.com/HwaminJung00/sglang/commit/5157a47e8649c6b3cc43705114fa8ec41d8d733d)), so the change can be read in the context of the SGLang source, or cloned with `git clone --depth 1 -b peak-kv-admission https://github.com/HwaminJung00/sglang`.

## What it does

The patch ports SGLang's existing exact-length admission simulation to the radix-cache path, behind a flag. That simulation is `PrefillAdder.add_one_req_ignore_eos`, and upstream runs it only when the radix cache is disabled. The diff is +<!--n:patch.lines_added-->102<!--/n-->/−<!--n:patch.lines_removed-->0<!--/n--> lines in <!--n:patch.files-->3<!--/n--> files.

The gate trusts `max_new_tokens` as each request's real output length. In the synthetic W3 trace every request has `ignore_eos=True`, so this value is exact. The gate is therefore an **oracle reservation that uses known output lengths**; it does not predict lengths.

Upstream already runs this simulation when the radix cache is off. In the study, the existing flag `--disable-radix-cache` recovered <!--n:q2.share_of_gain.noradix_pct-->88.0<!--/n-->% of the gate's goodput gain at q2 on its own, and `--max-running-requests 40` recovered <!--n:q2.share_of_gain.tuned_n40_pct-->78.1<!--/n-->% (one run each). What the patch adds is the same check with the radix cache kept on, plus α. The gate delays admissions, so its cost is time to first token: TTFT p99 changed by <!--n:q2.gain.ttft_p99_pct-->+43.6<!--/n-->% against the flag-off default at q2 (median of 3 runs). The README reports the full trade-off.

### Flags

Both flags are in the `schedule` group of `ServerArgs`.

| Flag | Default | Meaning |
|---|---|---|
| `--enable-peak-kv-reservation` | off | Turns the gate on. When it is off, the scheduler passes `peak_kv_reserve_ratio=None` and `add_one_req` skips the gate, so the code path is upstream's. |
| `--peak-kv-reserve-ratio` | `1.0` | α, in (0, 1]: the fraction of each request's remaining output budget to reserve. Values outside that range fail validation. |

### Where the gate sits

The gate is in `PrefillAdder.add_one_req`, in `python/sglang/srt/managers/schedule_policy.py` (lines 1369–1377 of the patched file). It is inside the `with self._lock_node(req.last_node):` block, so the candidate's matched prefix is already locked and not counted as evictable. The order in that block is:

1. upstream's `total_tokens >= self.rem_total_tokens` re-check;
2. upstream's hybrid-SWA re-check;
3. **the gate**: `self.peak_kv_reserve_ratio is not None and not self.is_hybrid_swa and self.dllm_config is None and not self._peak_kv_fits(req, cand_extend_input_len)` → `return AddReqResult.NO_TOKEN`;
4. upstream's prefill-delayer negotiation, then `init_load_back`.

The gate can only reject, so it never admits a request that upstream would not admit. It is skipped for hybrid-SWA and dLLM models. With `--disable-radix-cache`, `ignore_eos` requests take upstream's `add_one_req_ignore_eos` branch earlier in `add_one_req` and never reach the gate. Chunked-prefill continuations (`add_chunked_req`) are not gated.

### What `_peak_kv_fits` computes

The method is at line 1225 of the patched file. It works as follows:

- **Idle server.** If no unfinished request is running and nothing has been admitted this round, it admits. One request always fits, so the gate cannot deadlock.
- **Requests in the simulation.** It takes every unfinished running request, every request admitted this round (`can_run_list`) and the candidate.
- **Per-request values.** For each request it computes:
  - `left = α × max(max_new_tokens − len(output_ids), 0)`;
  - `own` = tokens held minus the prefix shared through the radix cache, rounded up to whole pages. The shared prefix is `cached_tokens` for running requests and `len(prefix_indices)` for new ones.
- **Event check.** It sorts the requests by `left`. Every request grows one token per step. At the step where the i-th request finishes, free KV is `free + (own of the requests that finished earlier) − left_i × (requests still live)`.
  - `free` is `cur_rem_tokens` (available + evictable − this round's admissions) minus the candidate's page-rounded prefill.
  - The gate rejects if free KV at any finish step is `<= max(1, page_size) ×` the requests still live.
- **Logging.** Each rejection increments a module-level counter. At most once per 5 s it logs `Peak-KV reservation delayed admission. #delays: …`.

### Files

| File | Change | Lines |
|---|---|---|
| `python/sglang/srt/server_args.py` | the two fields (+14); validation `0 < peak_kv_reserve_ratio <= 1.0` (+3) | +<!--n:patch.lines_added.server_args-->17<!--/n-->/−0 |
| `python/sglang/srt/managers/scheduler.py` | passes `peak_kv_reserve_ratio` to `PrefillAdder`, or `None` when the flag is off | +<!--n:patch.lines_added.scheduler-->5<!--/n-->/−0 |
| `python/sglang/srt/managers/schedule_policy.py` | `import time`; delay counter and throttled log (`_log_peak_kv_delay`, line 102); `__init__` parameter `peak_kv_reserve_ratio=None`; `_peak_kv_fits`; the gate | +<!--n:patch.lines_added.schedule_policy-->80<!--/n-->/−0 |

`tests/run_cpu_tests.py` checks this structure by comparing the patched `schedule_policy.py` with upstream as Python ASTs. Once those additions are removed, the two must be identical.

## How to apply

The base is SGLang **v0.5.18**, commit `71de97b264b04dcd514cf904003028aefe9775c8`. The PyPI wheel `sglang==0.5.18` contains byte-identical copies of the three files.

**On a git checkout** (the paths in the diff are `python/sglang/srt/…`):

```bash
git clone https://github.com/sgl-project/sglang.git && cd sglang
git checkout v0.5.18                      # 71de97b264b04dcd514cf904003028aefe9775c8
git apply --check /path/to/patch/peak_kv_reservation.diff
git apply /path/to/patch/peak_kv_reservation.diff
```

**On an installed wheel** (layout `sglang/srt/…`, so strip `a/python/` with `-p2`):

```bash
# the directory that contains sglang/ (find_spec locates the package without importing it)
SITE=$(python3 -c 'import importlib.util, pathlib; print(pathlib.Path(importlib.util.find_spec("sglang").origin).parents[1])')
cd "$SITE"
patch -p2 --dry-run --forward --fuzz=0 < /path/to/patch/peak_kv_reservation.diff
patch -p2 --forward --fuzz=0 < /path/to/patch/peak_kv_reservation.diff
```

Both routes were checked on 2026-10-02 with GNU patch 2.7.6 and git 2.43.0:

- the wheel's copies of the three files (`patch -p2`);
- the same files fetched from tag v0.5.18 (`git apply`).

All 8 hunks applied with no offset and no fuzz, and both routes produced identical files. In the patched `schedule_policy.py` (sha256 `e8ceb3975e62a828580cfaccef91efa64fb2bccb170b1d0167708aabf712c8f0`), the gate is at the lines given above. CI repeats the `patch -p2 --dry-run` check against the wheel on every push.

Then start the server with `--enable-peak-kv-reservation --peak-kv-reserve-ratio 1.0` on top of your usual flags. While the gate holds requests back, the server log shows `Peak-KV reservation delayed admission` lines.

## How to revert

```bash
git apply -R /path/to/patch/peak_kv_reservation.diff                        # git checkout
patch -p2 -R --forward --fuzz=0 < /path/to/patch/peak_kv_reservation.diff   # wheel layout, in "$SITE"
```

Reverting restores files byte-identical to upstream; this was checked for both routes. Reinstalling the wheel (`sglang==0.5.18`) has the same effect. To turn the gate off without reverting, leave out `--enable-peak-kv-reservation`.

## Modification notice (Apache License 2.0, section 4)

SGLang is Copyright 2023-2024 SGLang Team and licensed under the Apache License, Version 2.0. A copy of the license is in [`third_party/sglang_v0_5_18/LICENSE`](../third_party/sglang_v0_5_18/LICENSE).

This diff modifies three SGLang 0.5.18 files:

- `python/sglang/srt/server_args.py`: +<!--n:patch.lines_added.server_args-->17<!--/n-->/−0
- `python/sglang/srt/managers/scheduler.py`: +<!--n:patch.lines_added.scheduler-->5<!--/n-->/−0
- `python/sglang/srt/managers/schedule_policy.py`: +<!--n:patch.lines_added.schedule_policy-->80<!--/n-->/−0

Total: +<!--n:patch.lines_added-->102<!--/n-->/−<!--n:patch.lines_removed-->0<!--/n-->. No upstream line is removed or changed.

The changes were made by the author of this repository (GitHub `HwaminJung00`) on 2026-09-13. The added code is marked `[W3 study]` in the new comments, the docstring and the flag help texts. The modified files are distributed only as this diff. This README is the notice that the files were changed.

## Notes from the errata

These points come from the post-study review (see [`ERRATA.md`](../ERRATA.md)). The diff above is kept as measured. The corrections live here and in the tests.

### C3: the `_peak_kv_fits` docstring is inaccurate

The docstring ends with: *"Same simulation as add_one_req_ignore_eos, but without the CLIP_MAX_NEW_TOKENS clip and new_token_ratio discount."* This is wrong on two counts:

- Upstream's `add_one_req_ignore_eos` does not use `CLIP_MAX_NEW_TOKENS` either.
- Upstream applies `new_token_ratio` only to requests without `ignore_eos`.

The real differences from upstream are:

- the radix-shared prefix is not counted as freed when a request finishes;
- the remaining budget is scaled by α;
- finished requests are skipped;
- requests with zero tokens left count as live at step 0;
- the reserve is `max(1, page_size)` tokens per live request;
- an idle server always admits;
- `new_token_ratio` is never applied, so requests without `ignore_eos` also reserve their full budget.

The corrected docstring would read:

> Same event-ordered simulation as add_one_req_ignore_eos (upstream uses it only when the radix cache is disabled), adapted for radix ON: the radix-shared prefix is not counted as freed at finish (cached_tokens for running requests, prefix_indices for new ones), the remaining budget is scaled by peak_kv_reserve_ratio, finished requests are skipped, the page-size reserve is applied, and new_token_ratio is never applied. Neither path uses CLIP_MAX_NEW_TOKENS.

`tests/test_upstream_equivalence.py` checks the gate against upstream's method. On the domain where the two should coincide, the verdicts agree in <!--n:patch.equiv_upstream-->19,907/19,907<!--/n--> states. Where finished, zero-left or idle cases are mixed in, the verdicts differ in <!--n:patch.equiv_edge_upstream_diff-->695/19,887<!--/n--> states, and each difference follows from the rules above (see [`tests/README.md`](../tests/README.md)).

### C9: the model is an approximation of the radix pool

The simulation is exact within its own model; see the brute-force check in `tests/`. Against a physical radix pool it is optimistic in two known cases, and the tests reproduce both:

1. **Shared prefix.** A running request's shared prefix is taken from `cached_tokens`. Suppose the request that created the prefix finishes first: its `cached_tokens` is 0, so the gate counts the prefix as freed while other requests still lock it. The gate is then optimistic by the prefix length, which is <!--n:trace.shared_prefix_tokens-->36<!--/n--> tokens in W3. A request that comes back after a retraction keeps the `cached_tokens` of its first prefill, so it can end up in the same situation.
2. **Chunked prefill.** A chunked prefill in `can_run_list` is credited with its whole prompt at finish. But the prompt tokens it has not yet allocated are not modelled as growth. The gate is optimistic by that remainder. This matters for prompts longer than `chunked_prefill_size` (2,048 here), as in the long-prompt cross-workload traces. W3 prompts are not chunked.

"α = 1 gives zero retractions" is what the W3 runs measured, with short prompts and a <!--n:trace.shared_prefix_tokens-->36<!--/n-->-token shared prefix. It is not a guarantee. Upstream's retraction remains the safety net.

The gate also trusts `max_new_tokens`. If a request is EOS-terminated and sets no `max_tokens`, SGLang sets `max_new_tokens` to a context-length bound, about 32k tokens here. In the 87,552-token pool, the α = 1 gate would then admit only two such requests started together. This comes from running the extracted gate on CPU (check L3 in `tests/test_upstream_equivalence.py`); it was never measured, because every trace in the study used `ignore_eos=True`.

### C8: `#delays` is a lower bound when read from the log

`_PEAK_KV_DELAYS` counts every rejection, that is, every `_peak_kv_fits` call that returns `False`. The same waiting request can be rejected in several scheduling rounds, so this is not a count of distinct requests. The log line prints the counter at most once per 5 s. The last `#delays` value in a server log can therefore miss rejections from the run's last seconds. `w3/summarize.py` reports that last logged value (`peak_delays`), so read it as "at least". Each scheduler process has its own counter, one per TP/DP rank. `tests/run_cpu_tests.py` (step 5) demonstrates the throttling with a fake clock. A fix would print the final value at shutdown or export it as a metric; it is not applied here.

## Verify

```bash
cd patch && sha256sum -c SHA256SUMS && cd ..
python3 tests/run_cpu_tests.py                 # hashes, strict apply, structure, unit tests
python3 tests/test_upstream_equivalence.py     # equivalence with upstream and with a brute-force simulation
```
