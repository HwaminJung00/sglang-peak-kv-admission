#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# Fetch the course harness into .course/ (gitignored). It is needed only to
# re-run the GPU measurements and to regenerate the traces; the offline
# analysis, the figures and the CPU tests do not use it.
#
#   bash tools/fetch_course_harness.sh            # fetch (or reuse), then verify
#   bash tools/fetch_course_harness.sh --verify   # only verify an existing .course/
#
# What it fetches: a shallow (depth 1), sparse, blob-filtered fetch of
#   https://github.com/mlleo/inference-engine-study
# at commit 802a1642fcefc06735a89af054213c9904b4ff45, limited to
#   project/bench  project/common  project/workloads  project/scripts
#
# What it verifies: HEAD is that commit; the tree ids of the four directories
# match; every checked-out file matches tools/course_files.sha256; nothing
# outside the four directories is checked out.
#
# The harness is the course instructor's work and its repository has no
# license. It is not part of this repository and must not be committed or
# redistributed.
#
# Environment (optional): COURSE_DIR (default: .course in the repository
# root), COURSE_REPO_URL (default: the GitHub URL above).
# Needs git >= 2.25 and python3.
set -euo pipefail

COMMIT=802a1642fcefc06735a89af054213c9904b4ff45
DIRS=(project/bench project/common project/workloads project/scripts)
TREES=(60dfbcd3ccf5b204c75868d87e720a1fa9e17345   # project/bench
       8cb56e06dbb3d93018b7747e965cb342111ee383   # project/common
       f3ee63e2180358d8347bc7ef7365e161a0d9507d   # project/workloads
       15459649bfe6c2a40f7c5aa7b8295fd00124f208)  # project/scripts

verify_only=0
for arg in "$@"; do
  case "$arg" in
    --verify) verify_only=1 ;;
    -h|--help) awk 'NR > 2 && /^set -euo/ { exit } NR > 2 { sub(/^# ?/, ""); print }' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "fetch_course_harness: unknown option: $arg" >&2; exit 2 ;;
  esac
done

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
url=${COURSE_REPO_URL:-https://github.com/mlleo/inference-engine-study.git}
dest=${COURSE_DIR:-$root/.course}
hashes=$root/tools/course_files.sha256
export PYTHONDONTWRITEBYTECODE=1

g() { git -C "$dest" -c advice.detachedHead=false "$@"; }
die() { echo "fetch_course_harness: $*" >&2; exit 1; }

notice() {
  cat <<EOF

NOTICE: $dest holds the course harness
  ($url @ ${COMMIT:0:7}: ${DIRS[*]}).
  It is the course instructor's work and is published without a license.
  It is used here only for GPU reproduction and trace regeneration.
  It is not part of this repository: do not commit or redistribute it.

Use it from the repository root with the harness on the module path:
  export PYTHONPATH="$dest/project"
See docs/EXPERIMENTS.md for the GPU runs and data/traces.sha256 for the
trace regeneration commands.
EOF
}

fetch() {
  command -v git >/dev/null 2>&1 || die "git is required"
  if [ -e "$dest" ] && [ ! -d "$dest/.git" ]; then
    die "$dest exists but is not a git checkout; move it away first"
  fi
  if [ ! -d "$dest/.git" ]; then
    git init -q "$dest"
    g remote add origin "$url"
  else
    g remote set-url origin "$url"
  fi
  # Non-cone sparse patterns: only the four directories, not even the files
  # that sit directly in project/ (cone mode would include those).
  g config core.sparseCheckout true
  g config core.sparseCheckoutCone false
  mkdir -p "$dest/.git/info"
  printf '/%s/\n' "${DIRS[@]}" > "$dest/.git/info/sparse-checkout"
  if ! g cat-file -e "$COMMIT^{commit}" 2>/dev/null; then
    echo "fetch_course_harness: fetching ${COMMIT:0:12} from $url"
    g fetch --quiet --depth 1 --filter=blob:none --no-tags origin "$COMMIT"
  fi
  g checkout --quiet --detach "$COMMIT"
}

verify() {
  [ -d "$dest/.git" ] || die "$dest is not a git checkout; run without --verify first"
  [ -f "$hashes" ] || die "missing $hashes"
  local head
  head=$(g rev-parse HEAD)
  [ "$head" = "$COMMIT" ] || die "HEAD is $head, expected $COMMIT"
  [ "$(g cat-file -t "$COMMIT")" = commit ] || die "$COMMIT is not a commit"
  local i
  for i in "${!DIRS[@]}"; do
    [ "$(g rev-parse "$COMMIT:${DIRS[$i]}")" = "${TREES[$i]}" ] \
      || die "tree id of ${DIRS[$i]} does not match"
  done
  python3 - "$dest" "$hashes" "${DIRS[@]}" <<'PY'
import hashlib, os, sys

dest, hashes, *dirs = sys.argv[1:]
prefixes = tuple(d.rstrip("/") + "/" for d in dirs)
expected = {}
with open(hashes, encoding="utf-8") as f:
    for line in f:
        sha, _, path = line.rstrip("\n").partition("  ")
        if path.startswith(prefixes):
            expected[path] = sha
found = {}
for dirpath, dirnames, filenames in os.walk(dest):
    dirnames[:] = [d for d in dirnames if not (dirpath == dest and d == ".git")]
    for fn in filenames:
        full = os.path.join(dirpath, fn)
        rel = os.path.relpath(full, dest).replace(os.sep, "/")
        with open(full, "rb") as fh:
            found[rel] = hashlib.sha256(fh.read()).hexdigest()
problems = []
problems += [f"outside the sparse set: {p}" for p in sorted(found) if not p.startswith(prefixes)]
problems += [f"missing: {p}" for p in sorted(set(expected) - set(found))]
problems += [f"sha256 differs from tools/course_files.sha256: {p}"
             for p in sorted(set(expected) & set(found)) if found[p] != expected[p]]
problems += [f"not in tools/course_files.sha256: {p}"
             for p in sorted(set(found) - set(expected)) if p.startswith(prefixes)]
for p in problems:
    print(f"fetch_course_harness: {p}", file=sys.stderr)
if problems:
    sys.exit(1)
print(f"fetch_course_harness: verified {len(expected)} files in {len(dirs)} directories")
PY
  echo "fetch_course_harness: commit ${COMMIT:0:12} and tree ids OK"
}

if [ "$verify_only" = 0 ]; then
  fetch
fi
verify
notice
