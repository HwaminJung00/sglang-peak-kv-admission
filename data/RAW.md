# Raw data: GitHub Release `data-v1`

The raw client results and server logs of the study are not stored in git; they total 270 files and 427,796,769 bytes. They are published as assets of the release `data-v1` of this repository. Everything in [`data/derived/`](derived/) and every number in the docs can be rebuilt from them ([docs/EXPERIMENTS.md](../docs/EXPERIMENTS.md#regenerating-the-derived-data)).

## Assets

| Asset | Bytes | sha256 |
|---|---|---|
| `w3-raw-v1.tar.xz` | 61,091,648 | `0537f73172c7f0ac87fb9d42ae8afc0eb7e683c5f8e7472491ea2edda3674c9a` |
| `MANIFEST.tsv` | 32,902 | `f1a258001eb86de3b9a8ce08a42da71b0a9281e762732dc24ae286a78dad49fd` |
| `SHA256SUMS` | 162 | holds the two hashes above |

`MANIFEST.tsv` has one line per archived file with the columns `path`, `bytes`, `sha256`, `era` and `status`. The archive paths are relative to the repository root (`results/…`, `logs/…`), and every file keeps its original modification time.

## Contents

| Path | Files | Bytes | What |
|---|---|---|---|
| `results/*.json` | 58 | 355,637,086 | Client results of the non-deterministic runs from 09-03 to 09-13, including the two excluded files: per request the timestamps, every streamed chunk's arrival time, token counts and the generated text |
| `results/*.csv` | 3 | 35,952 | The summary tables as they were on 09-13 (identical to `data/derived/`) |
| `results/det/` | 7 | 39,841,140 | The six deterministic-mode runs and their summary table |
| `results/cross/` | 10 | 6,699,150 | The cross-workload replay: five traces, default and patch |
| `results/failed/` | 1 | 6,489,746 | The q1 ablated run with one client error, excluded and re-run |
| `results/backup/` | 1 | 6,702,329 | The original `--disable-radix-cache` run at 0.35 req/s, overwritten in `results/` by a failed re-run |
| `logs/server_*.log` | 56 | 7,481,119 | SGLang server logs: resolved arguments, `Prefill`/`Decode batch` lines, `Retract requests` lines, gate log lines |
| `logs/metrics_*.txt`, `logs/replay_*.txt` | 46 + 46 | 3,405,830 | The `/metrics` dump taken before shutdown, and the replay console output, for each 09-13 run |
| `logs/det/` | 18 | 1,226,527 | Server, metrics and replay logs of the deterministic runs |
| `logs/failed/` | 3 | 180,522 | Logs of the excluded q1 ablated run |
| `logs/verify/` | 11 | 17,159 | Verify-tool outputs (also in `data/verify/`) |
| `logs/w3_queue.log` | 1 | 38,695 | The queue runner's log (also in `data/ledger/`) |
| `logs/sched_*.txt` | 6 | 27,312 | Scheduler-log excerpts from 09-07 (drain phase; not used, see [data/README.md](README.md#incidents)) |
| `logs/cross_*_console.txt`, `logs/analyze_*.txt` | 2 + 1 | 14,202 | Console output of the cross replay; one output of the course's analysis tool |

Traces are not included (see [below](#traces)).

**Status** (column `status` of the manifest):

| Status | Files | Meaning |
|---|---|---|
| `ok` | 262 | The study's results, logs and tables as recorded |
| `failed` | 4 | `results/failed/` and `logs/failed/`: the q1 ablated run with a client error, re-run later |
| `excluded` | 3 | Not a measurement: `results/reasoning__batch_test.json` and `logs/server_batch_test.log` (replay before the server was up) and `results/reasoning__noradix.json` (a failed re-run that overwrote the original) |
| `restored` | 1 | `results/backup/reasoning__noradix.orig.json`, the original of the overwritten run |

**Era** (column `era`): `0913` 210 files, `det` 25, `0907` 22, `cross` 10, `0903` 2, `0906` 1. Files under `det/` and `cross/` get those labels, and every other file gets its modification date in UTC as MMDD. [data/README.md](README.md#era-labels) explains the labels and the two files whose label follows the file time.

## Fetch and verify

From the repository root:

```bash
bash tools/fetch_raw.sh            # download into dist/, verify, unpack into results/ and logs/
bash tools/fetch_raw.sh --force    # also overwrite files that already exist
```

`tools/fetch_raw.sh` downloads the three assets and checks the archive and the manifest against `SHA256SUMS`. It unpacks regular files only, refusing links, absolute paths and `..`, and keeps the archived modification times. It then checks every unpacked file's size and sha256 against `MANIFEST.tsv`. Existing files are kept unless `--force` is given, and they are still checked. `RAW_BASE_URL`, `RAW_DEST` and `RAW_DOWNLOAD_DIR` override the source, the target folder and the download folder.

By hand:

```bash
base=https://github.com/HwaminJung00/sglang-peak-kv-admission/releases/download/data-v1
mkdir -p dist && cd dist            # dist/ is gitignored
curl -L --remote-name-all "$base/SHA256SUMS" "$base/w3-raw-v1.tar.xz" "$base/MANIFEST.tsv"
sha256sum -c SHA256SUMS
cd .. && tar -xJf dist/w3-raw-v1.tar.xz   # unpack in the repository root; tar keeps the modification times
```

Keep the modification times. Without `--run` arguments, `analysis.retract_cost` pairs result files with server logs by name and by modification time.

`bash tools/make_raw_bundle.sh` rebuilds the three assets from `results/` and `logs/`. The build is deterministic: sorted names, original modification times, owner 0, mode 0644, `xz -6`. It produced the hashes above with xz 5.4. Another xz version may compress to different bytes; the per-file hashes in `MANIFEST.tsv` are the version-independent reference. The build scans every file for secrets before packing and re-checks every member afterwards.

## Traces

The 17 replay traces are output of the course's trace generator and are not redistributed. [`data/traces.sha256`](traces.sha256) lists the sha256 of each trace and the command that generates it with the course repository at commit `802a164`:

```bash
bash tools/fetch_course_harness.sh          # course harness into .course/ (gitignored, not redistributed)
export PYTHONPATH=$PWD/.course/project
python -m workloads.generators reasoning --out traces/ --model Qwen/Qwen3-4B --set qps=2 --suffix _q2
sha256sum -c --ignore-missing data/traces.sha256
```

The prompt texts depend on the tokenizer, so use the Qwen/Qwen3-4B revision `1cfa9a7208912126459214e8b04321603b3df60c` to reproduce the hashes. The generator has no revision flag; pass a local snapshot of that revision as `--model`. The arrival times, output lengths and request ids of all 17 files were re-checked against the generator on 2026-10-02.

## License

The release assets carry the same terms as the derived data: CC BY 4.0 ([LICENSE-docs](../LICENSE-docs)). The generated texts inside the result files come from Qwen/Qwen3-4B, which is licensed under Apache-2.0 ([NOTICE](../NOTICE), [THIRD_PARTY.md](../THIRD_PARTY.md)).
