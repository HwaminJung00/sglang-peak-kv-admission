#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# Build the raw-data release bundle (GitHub Release "data-v1") from results/
# and logs/ in the repository root:
#
#   dist/w3-raw-v1.tar.xz   the raw files, mtimes preserved
#   dist/MANIFEST.tsv       path, bytes, sha256, era, status of every file
#   dist/SHA256SUMS         sha256 of the two files above
#
# Usage:
#   bash tools/make_raw_bundle.sh [--with-traces] [--no-scan] [--no-verify]
#
#   --with-traces  also bundle traces/. Off by default: traces are published
#                  only as data/traces.sha256 plus a regeneration command.
#   --no-scan      skip the secret/identifier scan of the bundled files
#                  (tools/prepublish_check.sh --scan). A secret aborts the build.
#   --no-verify    skip re-reading the archive against MANIFEST.tsv
#
# Determinism: the file list is sorted bytewise; regular files only (no
# directory entries); mtimes are kept; owner and group are 0 (numeric); modes
# are normalized to 0644; pax headers carry no atime/ctime and use fixed
# names; xz -6 -T0 always writes the multi-threaded format (xz >= 5.4), so
# the output does not depend on the number of cores. Another xz version may
# compress differently; MANIFEST.tsv (per-file sha256) is the
# version-independent check.
#
# era:    "det" or "cross" for files in a det/ or cross/ folder; otherwise the
#         file's mtime date in UTC as MMDD (0903, 0906, 0907 or 0913)
# status: failed    results/failed/*, logs/failed/*
#         excluded  results/reasoning__batch_test.json (setup test),
#                   results/reasoning__noradix.json (overwritten by a failed
#                   rerun), logs/server_batch_test.log (setup test)
#         restored  results/backup/reasoning__noradix.orig.json (the original
#                   of the overwritten noradix run)
#         ok        everything else
set -euo pipefail

with_traces=0
scan=1
verify=1
for arg in "$@"; do
  case "$arg" in
    --with-traces) with_traces=1 ;;
    --no-scan) scan=0 ;;
    --no-verify) verify=0 ;;
    -h|--help) awk 'NR > 2 && /^set -euo/ { exit } NR > 2 { sub(/^# ?/, ""); print }' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "make_raw_bundle: unknown option: $arg" >&2; exit 2 ;;
  esac
done

for tool in tar xz python3 sha256sum; do
  command -v "$tool" >/dev/null 2>&1 || { echo "make_raw_bundle: $tool is required" >&2; exit 2; }
done
if ! tar --version 2>/dev/null | grep -q 'GNU tar'; then
  echo "make_raw_bundle: GNU tar is required (for --sort, --pax-option and --owner)" >&2
  exit 2
fi

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$root"
export LC_ALL=C TZ=UTC PYTHONDONTWRITEBYTECODE=1

dirs=(results logs)
[ "$with_traces" = 1 ] && dirs+=(traces)
for d in "${dirs[@]}"; do
  if [ ! -d "$d" ]; then
    echo "make_raw_bundle: $d/ not found in the repository root." >&2
    echo "  Copy the raw data there first (or run tools/fetch_raw.sh)." >&2
    exit 1
  fi
done
special=$(find "${dirs[@]}" ! -type f ! -type d -print | head -5)
if [ -n "$special" ]; then
  echo "make_raw_bundle: refusing to bundle symlinks or special files:" >&2
  echo "$special" >&2
  exit 1
fi

out=dist
name=w3-raw-v1.tar.xz
mkdir -p "$out"
work=$(mktemp -d -t make_raw_bundle.XXXXXX)
trap 'rm -rf "$work"' EXIT
start=$(date +%s)

# 1. File list and MANIFEST.tsv --------------------------------------------
python3 - "$work/files.nul" "$out/MANIFEST.tsv" "${dirs[@]}" <<'PY'
import hashlib, os, sys, time
from collections import Counter

list_path, manifest_path, *dirs = sys.argv[1:]
EXCLUDED = {
    "results/reasoning__batch_test.json",
    "results/reasoning__noradix.json",
    "logs/server_batch_test.log",
}
RESTORED = {"results/backup/reasoning__noradix.orig.json"}
KNOWN_ERAS = {"0903", "0906", "0907", "0913", "det", "cross"}

paths = []
for d in dirs:
    for dirpath, dirnames, filenames in os.walk(d):
        dirnames.sort()
        paths.extend(os.path.join(dirpath, f).replace(os.sep, "/") for f in filenames)
paths.sort(key=lambda p: p.encode("utf-8", "surrogateescape"))

def era(p, st):
    parts = p.split("/")[:-1]
    for tag in ("det", "cross"):
        if tag in parts:
            return tag
    return time.strftime("%m%d", time.gmtime(st.st_mtime))

def status(p):
    top, _, rest = p.partition("/")
    if rest.startswith("failed/"):
        return "failed"
    if p in EXCLUDED:
        return "excluded"
    if p in RESTORED:
        return "restored"
    return "ok"

rows = []
for p in paths:
    st = os.stat(p)
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    rows.append((p, st.st_size, h.hexdigest(), era(p, st), status(p)))

with open(list_path, "wb") as f:
    for p in paths:
        f.write(p.encode("utf-8", "surrogateescape") + b"\0")
with open(manifest_path, "w", encoding="utf-8", newline="\n") as f:
    f.write("path\tbytes\tsha256\tera\tstatus\n")
    for r in rows:
        f.write(f"{r[0]}\t{r[1]}\t{r[2]}\t{r[3]}\t{r[4]}\n")

unknown = sorted({r[3] for r in rows} - KNOWN_ERAS)
if unknown:
    print(f"warning: unexpected era value(s): {', '.join(unknown)}", file=sys.stderr)
missing = (EXCLUDED | RESTORED) - {r[0] for r in rows}
if missing:
    print(f"warning: listed in the status rules but not found: {', '.join(sorted(missing))}",
          file=sys.stderr)

by_top = Counter()
bytes_top = Counter()
for r in rows:
    top = r[0].split("/")[0]
    by_top[top] += 1
    bytes_top[top] += r[1]
print("files to bundle:")
for top in sorted(by_top):
    print(f"  {top + '/':<10} {by_top[top]:5d} files {bytes_top[top]:>15,} bytes")
print(f"  {'total':<10} {len(rows):5d} files {sum(bytes_top.values()):>15,} bytes")
print("status: " + ", ".join(f"{k} {v}" for k, v in sorted(Counter(r[4] for r in rows).items())))
print("era:    " + ", ".join(f"{k} {v}" for k, v in sorted(Counter(r[3] for r in rows).items())))
PY

# 2. Secret / identifier scan ------------------------------------------------
if [ "$scan" = 1 ]; then
  echo
  echo "== scanning the bundled files (CP2 secrets abort the build; CP3 is informational)"
  if ! python3 tools/prepublish_check.py --scan "${dirs[@]}"; then
    echo "make_raw_bundle: secret scan failed; nothing was written to $out/$name" >&2
    exit 1
  fi
fi

# 3. Deterministic tar | xz ------------------------------------------------------
echo
echo "== writing $out/$name (xz -6 -T0)"
tar --create --file=- \
    --null --no-recursion --files-from="$work/files.nul" \
    --format=pax \
    --pax-option='exthdr.name=%d/PaxHeaders/%f,delete=atime,delete=ctime' \
    --owner=0 --group=0 --numeric-owner --mode=u=rw,go=r \
    --no-acls --no-selinux --no-xattrs \
  | xz -6 -T0 -c > "$work/$name"
mv "$work/$name" "$out/$name"
(cd "$out" && sha256sum MANIFEST.tsv "$name" > SHA256SUMS)

# 4. Verify the archive against MANIFEST.tsv -------------------------------------
if [ "$verify" = 1 ]; then
  echo "== verifying $out/$name against MANIFEST.tsv"
  python3 - "$out/$name" "$out/MANIFEST.tsv" <<'PY'
import hashlib, os, sys, tarfile

archive, manifest = sys.argv[1:]
expected = {}
with open(manifest, encoding="utf-8") as f:
    next(f)
    for line in f:
        path, size, sha, _era, _status = line.rstrip("\n").split("\t")
        expected[path] = (int(size), sha)
seen = []
problems = 0
with tarfile.open(archive, "r:xz") as tf:
    for m in tf:
        if not m.isfile():
            print(f"unexpected non-file member: {m.name}"); problems += 1; continue
        seen.append(m.name)
        h = hashlib.sha256()
        src = tf.extractfile(m)
        for block in iter(lambda: src.read(1 << 20), b""):
            h.update(block)
        size, sha = expected.get(m.name, (None, None))
        if (m.size, h.hexdigest()) != (size, sha):
            print(f"content mismatch: {m.name}"); problems += 1
        if int(m.mtime) != int(os.stat(m.name).st_mtime):
            print(f"mtime not preserved: {m.name}"); problems += 1
        if (m.uid, m.gid, m.mode) != (0, 0, 0o644):
            print(f"owner/mode not normalized: {m.name}"); problems += 1
if seen != sorted(expected, key=lambda p: p.encode("utf-8", "surrogateescape")):
    print("member list differs from MANIFEST.tsv"); problems += 1
print(f"verified {len(seen)} members: {'OK' if problems == 0 else f'{problems} problem(s)'}")
sys.exit(1 if problems else 0)
PY
fi

end=$(date +%s)
echo
echo "== done in $((end - start)) s"
ls -l "$out/$name" "$out/MANIFEST.tsv" "$out/SHA256SUMS" | awk '{printf "  %15s  %s\n", $5, $NF}'
cat "$out/SHA256SUMS" | sed 's/^/  /'
if [ "$with_traces" = 1 ]; then
  echo "NOTE: traces/ is included. Publish this bundle only if the trace files may be shared."
fi
