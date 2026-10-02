# CPU tests

These tests check the logic of the peak-KV admission gate (`PrefillAdder._peak_kv_fits` and its call site) without a GPU and without installing SGLang. They need Python 3.12 and the standard library only.

```bash
python3 tests/run_cpu_tests.py                     # under 1 s; prints "all peak-KV unit tests passed"
python3 tests/test_upstream_equivalence.py         # N = 2,000 draws per check, about 5 s (CI)
python3 tests/test_upstream_equivalence.py --full  # N = 20,000, about 40 s; rewrites artifacts/equivalence.json
python3 tests/mutation_check.py                    # about 1 min; optional, not in CI
python3 tests/run_cpu_tests.py --bench             # also times one _peak_kv_fits call on this host
```

Each command exits non-zero on failure. CI (`.github/workflows/cpu-tests.yml`) runs the first three on every push and pull request. After `--full` it runs `git diff --exit-code artifacts/equivalence.json`, which proves the committed numbers are reproducible. The timings are from the development machine.

## Where the code under test comes from

SGLang cannot be imported without torch, `sgl_kernel` and a GPU stack. So the tests build the code under test from source text, in four steps (`tests/peakkv_harness.py`):

1. **Hashes.** They check `third_party/sglang_v0_5_18/SHA256SUMS`, which covers the unmodified upstream `schedule_policy.py`, and `patch/SHA256SUMS`, which covers the measured diff.
2. **Patch.** They apply the five `schedule_policy.py` hunks of `patch/peak_kv_reservation.diff` to a copy of the vendored file in a temporary directory. The applier is strict: every context line must match at the stated line number, with no offset and no fuzz. The result must have the sha256 that `git apply` (on tag v0.5.18) and `patch -p2` (on the wheel) produce: `e8ceb3975e62a828580cfaccef91efa64fb2bccb170b1d0167708aabf712c8f0`.
3. **Extract.** They parse the upstream or the patched source with `ast` and compile only the methods under test, verbatim and with their original line numbers. The module-level names those methods read are found with `symtable` and pulled in recursively. Only standard-library imports are allowed; anything that would need SGLang or torch stops the test. For `_peak_kv_fits` the pulled-in names are `logging`, `logger`, `time`, `IGNORE_EOS_RESERVE_TOKENS`, the two delay counters and `_log_peak_kv_delay`.
4. **Stub.** `w3/test_peak_kv.py` imports `PrefillAdder` from `sglang.srt.managers.schedule_policy`. `run_cpu_tests.py` registers a module under that name that holds the compiled code, and removes it afterwards. Requests and adders in the equivalence checks are small stubs that carry only the fields the code reads.

## What is verified

| Script | Check | Result (`--full`) |
|---|---|---|
| `run_cpu_tests.py` | [1] Hashes of the vendored file and of the diff. | pass |
| | [2] The schedule_policy.py hunks apply exactly; the result has the expected sha256; the whole diff is +<!--n:patch.lines_added-->102<!--/n-->/−<!--n:patch.lines_removed-->0<!--/n-->. | pass |
| | [3] Structure. The patched module, minus eight listed additions, has the same AST as upstream. The gate sits inside `with self._lock_node(...)` in `add_one_req`: after the `rem_total_tokens` and SWA re-checks, before the prefill-delayer negotiation. It has one call site and its condition starts with `peak_kv_reserve_ratio is not None`. The flags default to off and α = 1.0, and the scheduler passes `None` unless the flag is set. | pass |
| | [4] `w3/test_peak_kv.py`, unmodified: 7 hand-computed cases, plus 2,000 random states checked against a re-typed copy of upstream's loop. | pass |
| | [5] Delay counter (errata C8). Every rejection is counted, but the log line is throttled to one per 5 s, so the last logged `#delays` is a lower bound. | pass |
| `test_upstream_equivalence.py` | [A] Same verdict as upstream `add_one_req_ignore_eos` (run verbatim) on the domain below. | <!--n:patch.equiv_upstream-->19,907/19,907<!--/n--> |
| | [E] Edge cases excluded from [A]. Verdicts that differ from upstream: | <!--n:patch.equiv_edge_upstream_diff-->695/19,887<!--/n--> |
| | Same states after applying rules E1–E3 to upstream: | <!--n:patch.equiv_edge_explained-->19,887/19,887<!--/n--> |
| | [B] Same verdict as a token-by-token brute-force simulation, at the sampled free KV and at the threshold. | <!--n:patch.equiv_bruteforce-->20,000/20,000<!--/n--> |
| | [P] Admitted states that run to completion in a physical paged pool without a page-allocation failure. | <!--n:patch.equiv_paged_physical-->18,277/18,277<!--/n--> |
| | [L] The documented approximations L1–L3 (below) are reproduced exactly. | pass |

All draws are seeded, so every run gives the same numbers. `--full` writes them to `artifacts/equivalence.json`.

### [A] The domain where the gate equals upstream

The gate ports upstream's exact-length simulation, `add_one_req_ignore_eos`, to the radix-cache path. Upstream runs that method only when the radix cache is disabled. The two must agree when all of the following hold:

- α = 1 and `page_size` = 1;
- no shared prefix: `cached_tokens` = 0 and `prefix_indices` empty;
- every request has `ignore_eos=True`, is unfinished and has at least 1 token left;
- `can_run_list` holds 0–4 requests;
- at least one request is running or already admitted this round.

Two parts of upstream's method are not compared:

- `rem_total_tokens` is left unbounded, because both paths check it the same way before the simulation;
- the bookkeeping after the verdict (`_update_prefill_budget`, `budget_state`) is stubbed.

Upstream's verdict is "the candidate was appended to `can_run_list`".

Some other differences are excluded from this domain by design, because upstream has no counterpart:

- α < 1 and the page-size reserve are additions of the patch;
- shared prefixes do not exist when the radix cache is off;
- upstream discounts requests without `ignore_eos` by `new_token_ratio`, and the gate never does.

These features are covered by [B] instead.

### [E] The excluded edge cases, and the 695 differences

An earlier exploratory comparison added finished and zero-left requests to the [A] generator and reported 695 differing verdicts in 19,887 states. `--full` reproduces that exactly. Every difference follows from one of three rules:

- **E1, idle.** Suppose no running request is unfinished and `can_run_list` is empty. The gate then admits unconditionally, which is its deadlock guard. Upstream simulates the candidate alone and can reject.
- **E2, finished.** A finished request can still be in `running_batch.reqs`, with its KV already freed. The gate skips it. Upstream's loop never calls `finished()`, so it simulates any such request that still has tokens left. It counts that request's growth, and later credits its tokens as freed a second time. The verdict can therefore move in either direction.
- **E3, zero-left.** An unfinished request with 0 tokens left counts as live at step 0 in the gate, then releases its tokens before the first decode step. Upstream drops it from the simulation entirely: it is never counted and never released.

The check runs upstream's own method on each state after transforming it by E1–E3, and requires the same verdict as the gate in every state. Almost all of the 695 differences come from E2. The exploratory generator marks running requests as finished regardless of their remaining budget. An `ignore_eos` request can only reach that combination through an abort or a stop condition.

Two dedicated generators test the rules one at a time:

- **finished:** 15% of running requests are finished with 0 tokens left, which is how an `ignore_eos` request normally ends. The only differences are E1 cases.
- **zero-left:** 15% of running requests are unfinished with 0 tokens left. Every difference is E3, and in every one the gate admits where upstream rejects.

Output of `--full` (verbatim, abridged):

```text
[A] ok   upstream add_one_req_ignore_eos vs _peak_kv_fits: 19,907/19,907 verdicts agree (11,375 admitted; ...; 93 idle draws skipped)
[E] ok   finished and zero-left requests mixed (the earlier exploratory generator): verdict differs from upstream in 695/19,887 states (by rule: E1 idle: 2, E2 finished: 687, E2 finished + E3 zero-left: 6; gate admits, upstream rejects: 630, gate rejects, upstream admits: 65); equals upstream after rules E1-E3 in 19,887/19,887
[E] ok   15% finished requests with 0 tokens left: verdict differs from upstream in 4/20,000 states (by rule: E1 idle: 4; ...); equals upstream after rules E1-E3 in 20,000/20,000
[E] ok   15% unfinished requests with 0 tokens left: verdict differs from upstream in 958/20,000 states (by rule: E3 zero-left: 958; gate admits, upstream rejects: 958); equals upstream after rules E1-E3 in 20,000/20,000
[B] ok   token-by-token brute force vs _peak_kv_fits: 20,000/20,000 states agree at the sampled free KV and at the threshold (12,699 admitted, 90 idle; ...)
[P] ok   page alignment: 18,277/18,277 admitted states run to completion in a physical paged pool without a page-allocation failure (page_size 2/16/64, alpha=1, 20,000 draws)
```

### [B] Brute force

The reference simulation is written from the model's definition, not from the patch, and works step by step:

- every live request appends one token per step, until it has used α × its remaining budget;
- a request is live up to and including the step that produces its last budgeted token;
- at the next step it returns its own tokens (held minus the radix-shared prefix, rounded up to whole pages) plus the tokens it generated;
- the state is rejected if free KV ≤ `max(1, page_size)` × live requests at any step.

The draws mix:

- α = k/16 and the measured values 0.6 and 0.9;
- `page_size` from 1 to 64;
- shared prefixes;
- finished requests in the running batch;
- `can_run_list` entries, including prompts longer than one 2,048-token chunk;
- retracted requests coming back with output, zero-left requests and idle servers.

Each state is checked twice: at its sampled free KV, and at the brute-force threshold, where one token less must be rejected and the threshold itself admitted. That second check pins the `<=` boundary and the page rounding. α × remaining is an exact integer in every state, so integer steps are exact. Non-integral budgets are not covered by [B]; the gate handles them in floating point.

### [P] and [L]: the model against a physical pool

- **[P]** uses α = 1, no sharing and `page_size` 2, 16 or 64. In the physical pool, a request takes a new page each time its token count crosses a page boundary and frees all its pages when it finishes. No admitted state in the draws fails an allocation, so the reserve of `max(1, page_size)` tokens per live request absorbs the page slack.
- **[L]** reproduces the approximations listed in [`patch/README.md`](../patch/README.md) (errata C9). Each case is checked at several free-KV levels:
  - **L1:** the request that created a shared prefix finishes first. The gate's model overestimates free KV by exactly the <!--n:trace.shared_prefix_tokens-->36<!--/n-->-token prefix, which other requests still lock.
  - **L2:** a chunked prefill in `can_run_list` still has 1,904 prompt tokens to allocate. The model overestimates free KV by exactly those 1,904 tokens.
  - **L3:** the gate depends on known output lengths. An EOS-terminated request that sets no `max_tokens` gets a `max_new_tokens` of 32,379, which is a context bound. The α = 1 gate then admits only 2 such requests started together in the 87,552-token W3 pool.

### Mutation check

`mutation_check.py` changes one line of the gate at a time in a temporary copy (the repository is not written). It then reruns both scripts with their CI settings. All ten mutations are caught:

| Mutation | Caught by |
|---|---|
| `<=` becomes `<` in the reserve check | [B] |
| page-size reserve dropped | [B], [P] |
| running requests use `prefix_indices` instead of `cached_tokens` | [4], [B] |
| finished requests not skipped | [4], [B], [E] |
| freed tokens off by one | [B] |
| states sorted in the wrong order | [4], [A], [B], [E], [L] |
| zero-left requests counted as 1 token left | [B] |
| candidate prefill not page-rounded | [B] |
| idle-server invariant removed | [4], [B], [E] |
| gate condition changed (`dllm_config is not None`) | [3] |

The study's own unit test (`[4]`) catches four of the ten.

## What is not verified

- **GPU behaviour and performance.** Nothing here runs a model, a CUDA graph or a kernel. Throughput, latency, goodput and retraction counts in the README come from the GPU runs (`data/`), not from these tests. `--bench` times the Python function on the host CPU only.
- **The KV allocator and the radix tree.** `cur_rem_tokens`, `cached_tokens` and `prefix_indices` are inputs to the stubs, not values computed by SGLang. Eviction, `_lock_node` locking and page allocation are not exercised, apart from the toy pools in [P] and [L].
- **Real scheduler integration.** `scheduler.py` and `server_args.py` are not executed. Their hunks are checked only structurally from the diff text in step [3]; CI also checks that the whole diff applies to the wheel. The following are not tested:
  - the `get_schedule()` wiring and flag parsing;
  - how the admission loop reacts to `NO_TOKEN` (`batch_is_full`);
  - overlap scheduling, chunked-prefill continuation, retraction and the prefill delayer;
  - tensor or data parallelism.
- **That the gate prevents retractions in general.** [P] uses an idealized pool without sharing or chunking, and [L] shows where the model is optimistic. "α = 1 gave zero retractions" is a measurement under W3 conditions.
- **Unknown output lengths.** The gate is an oracle reservation that trusts `max_new_tokens`; it does not predict lengths, and no test measures it on traffic whose lengths are unknown.
- **Other paths.** Hybrid-SWA, dLLM, Mamba and AMD/HIP paths are not covered. The gate is disabled for SWA and dLLM.

## Provenance

`w3/test_peak_kv.py` is the study's own unit test and is run unchanged. The rest of `tests/` was written after the study for this repository, with AI assistance (Claude). Checks [A], [B], [E], [P] and [L] port exploratory scripts from the post-study review into seeded, repo-relative, standard-library tests.
