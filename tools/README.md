# tools/

Scripts for checking and publishing this repository. They need bash, git and
the Python 3 standard library only (no numpy). Run them from the repository
root.

| Command | What it does |
|---|---|
| `python3 tools/check_numbers.py [--list-unused]` | Checks every number marker in the docs against `artifacts/*.json` |
| `bash tools/prepublish_check.sh [--verbose]` | Runs the pre-publication checks CP1–CP7 and prints a PASS/FAIL table |
| `bash tools/make_raw_bundle.sh [--with-traces]` | Builds the raw-data release files in `dist/` from `results/` and `logs/` |
| `bash tools/fetch_raw.sh [--force]` | Downloads the `data-v1` release and unpacks it into `results/` and `logs/` |
| `bash tools/fetch_course_harness.sh [--verify]` | Fetches the course harness into `.course/`, for GPU reproduction only |

| Data file | Contents |
|---|---|
| `tools/manifest.tsv` | Where each file of the as-run snapshot commit came from, with its sha256 |
| `tools/course_files.sha256` | sha256 of the 38 files tracked by the course repository (hashes and paths only), used by CP1 |

## Number markers: `check_numbers.py`

Every headline number in the Markdown docs is written as

    <!--n:KEY-->DISPLAY<!--/n-->

GitHub hides the comments, so readers see only DISPLAY. The checker loads all
`artifacts/*.json` files (a key defined twice is an error), scans `README*.md`,
`ERRATA.md`, `docs/**/*.md`, `data/*.md`, `patch/*.md` and `tests/*.md`, and
fails when a KEY is unknown, when DISPLAY differs from the key's `display`
string (character for character, so U+2212 "−" is not "-"), or when a marker
is malformed. Markers inside code spans and fenced code blocks are ignored.
`--list-unused` also lists keys that no doc uses. Files that do not exist yet
are skipped.

## Pre-publication checks: `prepublish_check.sh`

The checks cover every file git would publish: tracked files plus untracked
files that are not ignored (`git ls-files --cached --others --exclude-standard`).
Text inside `.gz` files and PNG text chunks is scanned too.

| Check | Fails when |
|---|---|
| CP1a | a path contains a course directory (`bench/`, `common/`, `workloads/`, `scripts/`, `bp/`) or a course document name |
| CP1b | a non-empty file is byte-identical to a course file (`tools/course_files.sha256`) |
| CP1c | a file contains a phrase that only occurs in the course's report and grading templates |
| CP2a | a file contains something shaped like an API token, access key, private key or bearer header |
| CP2b | a file contains the value of a secret-looking environment variable of the current shell (only the variable name is printed) |
| CP3a | a file contains an AI-session scratch path, a private claude.ai link, the institution name from the submitted PDF's metadata, or a pod hostname |
| CP3b | a file contains a personal e-mail address (GitHub noreply addresses and the Claude co-author address are allowed) |
| CP3c | a file contains an absolute path from the study machine; the two sample server logs in `data/logs_sample/` may keep the SGLang install and cache paths |
| CP4a–d | a file is over 5 MB, all files together are over 10 MB, a path is not ASCII, or raw data (`results/`, `logs/`, `traces/`), PDF/DOCX/HWP documents, archives, wheels or bytecode are included |
| CP5a–b | `LICENSE`, `LICENSE-docs`, `NOTICE` or `THIRD_PARTY.md` is missing, or the vendored SGLang file lost its upstream header or no longer matches the v0.5.18 wheel |
| CP5c | WARN only: `patch/README.md` (the change notice) or the vendored `LICENSE` is missing |
| CP6 | `python3 tools/check_numbers.py` fails |
| CP7 | WARN only: a Markdown line uses wording the docs must avoid; negated uses are fine, so this row never fails |

`--scan PATH...` runs only CP2 (FAIL) and CP3a/b (WARN) on arbitrary files;
`make_raw_bundle.sh` uses it on the raw data. Exit status: 0 = no FAIL,
1 = a FAIL, 2 = usage error. The pattern sources in `prepublish_check.py` are
written so that the script does not match itself.

## Git hook: `.githooks/pre-push`

Enable it once per clone:

    git config core.hooksPath .githooks

It refuses a push when the remote URL points at the course repository, when
any blob in the pushed commits is over 5 MB, or when
`tools/prepublish_check.sh` fails.

## Raw data: `make_raw_bundle.sh` and `fetch_raw.sh`

`make_raw_bundle.sh` writes three files to `dist/` (gitignored):

- `w3-raw-v1.tar.xz`: every regular file under `results/` and `logs/`
  (`traces/` only with `--with-traces`).
- `MANIFEST.tsv`: `path, bytes, sha256, era, status` for each file.
- `SHA256SUMS`: the sha256 of the two files above.

The build is deterministic: names are sorted, mtimes are kept, owner and group
are 0, modes are 0644, the pax headers carry no atime/ctime, and `xz -6 -T0`
writes the same bytes whatever the number of cores. Before writing, it scans
the files with `prepublish_check.sh --scan`, and a secret aborts the build.
After writing, it re-reads the archive and checks every member against the
manifest. The 2026-10-02 build packed 270 files (about 428 MB) into about
61 MB in under a minute.

- `era`: `det` or `cross` for files in a `det/` or `cross/` folder; otherwise
  the file's mtime date in UTC as MMDD (`0903`, `0906`, `0907`, `0913`).
- `status`:
  - `failed`: `results/failed/`, `logs/failed/`.
  - `excluded`: `results/reasoning__batch_test.json`,
    `results/reasoning__noradix.json` (overwritten by a failed rerun),
    `logs/server_batch_test.log`.
  - `restored`: `results/backup/reasoning__noradix.orig.json`, the original
    of the overwritten run.
  - `ok`: everything else.

`fetch_raw.sh` downloads `SHA256SUMS`, `w3-raw-v1.tar.xz` and `MANIFEST.tsv`
from the `data-v1` release and checks them. It extracts regular files only
(no links, absolute paths or `..`) under `results/`, `logs/` or `traces/`,
keeping the archived mtimes, then checks every file against the manifest.
Existing files are kept unless `--force` is given. `RAW_BASE_URL`, `RAW_DEST`
and `RAW_DOWNLOAD_DIR` override the source, the target and the download
folder.

## Course harness: `fetch_course_harness.sh`

The workload definition, trace generator and replay/metrics harness come from
the course repository
[mlleo/inference-engine-study](https://github.com/mlleo/inference-engine-study),
which has no license. They are not part of this repository and must not be
committed or redistributed. The script makes a shallow, sparse, blob-filtered
fetch of commit `802a1642fcefc06735a89af054213c9904b4ff45`, limited to
`project/{bench,common,workloads,scripts}`, into `.course/` (gitignored). It
then verifies the commit, the four tree ids, and every file against
`tools/course_files.sha256`. Use it with `PYTHONPATH=.course/project`, and
only to re-run the GPU measurements or to regenerate the traces.

## Provenance files

`tools/manifest.tsv` has one row per file of the as-run snapshot commit
(`b26ac23`, "Import as-run study artifacts"):

- `source`: the original location in the study environment, relative to
  its working directory (`inference-engine-study/project/...` for the course
  clone, `research/...` for the analysis folder); `-` for files created in
  this repository.
- `repo_path`: the path in this repository.
- `sha256`: computed from the source file. It equals the committed blob for
  all 41 files that have a source, so the snapshot is byte-identical.

`tools/course_files.sha256` is in `sha256sum` format, with paths relative to
the course repository root. Running `sha256sum -c` on it from the root of a
course checkout at `802a164` reports every file as OK.
