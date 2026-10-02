#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# Download the raw data (GitHub Release "data-v1") and unpack it into the
# repository root: results/ and logs/ (and traces/ if the bundle has them).
#
#   bash tools/fetch_raw.sh [--force]
#
#   --force   overwrite files that already exist. By default existing files
#             are kept, and they are still checked against MANIFEST.tsv.
#
# Steps
#   1. download SHA256SUMS, w3-raw-v1.tar.xz and MANIFEST.tsv into dist/
#      (a file already there whose sha256 matches is reused)
#   2. check both files against SHA256SUMS
#   3. extract regular files only (no links, absolute paths or ".."), each
#      under results/, logs/ or traces/, keeping the archived mtimes
#   4. check every file against MANIFEST.tsv (size and sha256) and print what
#      was extracted
#
# Environment (optional)
#   RAW_BASE_URL  where the assets are (default: the data-v1 release)
#   RAW_DEST      where to unpack (default: the repository root)
#   RAW_DOWNLOAD_DIR  where to keep the downloads (default: dist/, gitignored)
#
# Needs bash, python3 (standard library) and curl.
set -euo pipefail

force=0
for arg in "$@"; do
  case "$arg" in
    --force) force=1 ;;
    -h|--help) awk 'NR > 2 && /^set -euo/ { exit } NR > 2 { sub(/^# ?/, ""); print }' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "fetch_raw: unknown option: $arg" >&2; exit 2 ;;
  esac
done

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
base=${RAW_BASE_URL:-https://github.com/HwaminJung00/sglang-peak-kv-admission/releases/download/data-v1}
dest=${RAW_DEST:-$root}
dl=${RAW_DOWNLOAD_DIR:-$root/dist}
asset=w3-raw-v1.tar.xz
mkdir -p "$dl" "$dest"
export PYTHONDONTWRITEBYTECODE=1

for tool in python3 curl; do
  command -v "$tool" >/dev/null 2>&1 || { echo "fetch_raw: $tool is required" >&2; exit 2; }
done

sha256_of() { python3 -c 'import hashlib,sys
h=hashlib.sha256()
with open(sys.argv[1],"rb") as f:
    for b in iter(lambda: f.read(1<<20), b""): h.update(b)
print(h.hexdigest())' "$1"; }

download() {  # download NAME [required]
  local name=$1 required=${2:-1}
  echo "fetch_raw: downloading $name"
  if curl -fsSL --retry 3 --retry-delay 2 -o "$dl/$name.part" "$base/$name"; then
    mv "$dl/$name.part" "$dl/$name"
  else
    rm -f "$dl/$name.part"
    if [ "$required" = 1 ]; then
      echo "fetch_raw: could not download $base/$name" >&2
      exit 1
    fi
    return 1
  fi
}

# 1-2. SHA256SUMS first, then the assets it lists ---------------------------
download SHA256SUMS
expected_for() { awk -v n="$1" '$2 == n || $2 == "*" n { print $1 }' "$dl/SHA256SUMS"; }

have_manifest=1
for name in "$asset" MANIFEST.tsv; do
  want=$(expected_for "$name")
  if [ -z "$want" ]; then
    if [ "$name" = "$asset" ]; then
      echo "fetch_raw: SHA256SUMS has no entry for $asset" >&2; exit 1
    fi
    echo "fetch_raw: SHA256SUMS has no entry for $name; per-file checks will be skipped" >&2
    have_manifest=0; continue
  fi
  if [ -f "$dl/$name" ] && [ "$(sha256_of "$dl/$name")" = "$want" ]; then
    echo "fetch_raw: $name already present, sha256 OK"
    continue
  fi
  if [ "$name" = "$asset" ]; then
    download "$name"
  elif ! download "$name" 0; then
    echo "fetch_raw: MANIFEST.tsv not available; per-file checks will be skipped" >&2
    have_manifest=0; continue
  fi
  got=$(sha256_of "$dl/$name")
  if [ "$got" != "$want" ]; then
    echo "fetch_raw: sha256 mismatch for $name (expected $want, got $got)" >&2
    exit 1
  fi
  echo "fetch_raw: $name sha256 OK"
done

# 3-4. Extract and verify -----------------------------------------------------
manifest_arg=""
[ "$have_manifest" = 1 ] && manifest_arg="$dl/MANIFEST.tsv"
python3 - "$dl/$asset" "$dest" "$force" "$manifest_arg" <<'PY'
import hashlib, os, sys, tarfile
from collections import Counter

archive, dest, force, manifest = sys.argv[1], sys.argv[2], sys.argv[3] == "1", sys.argv[4]
ALLOWED_TOPS = ("results", "logs", "traces")

def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()

def safe_name(name):
    parts = name.split("/")
    return (not name.startswith("/") and ".." not in parts and "" not in parts
            and parts[0] in ALLOWED_TOPS and len(parts) > 1)

meta = {}
if manifest:
    with open(manifest, encoding="utf-8") as f:
        header = next(f).rstrip("\n").split("\t")
        for line in f:
            row = dict(zip(header, line.rstrip("\n").split("\t")))
            meta[row["path"]] = row

extracted, kept, problems = [], [], 0
with tarfile.open(archive, "r:xz") as tf:
    for m in tf:
        if m.isdir():
            continue
        if not m.isfile() or not safe_name(m.name):
            print(f"refusing member {m.name!r} (only regular files under "
                  f"{', '.join(ALLOWED_TOPS)} are allowed)", file=sys.stderr)
            sys.exit(1)
        target = os.path.join(dest, *m.name.split("/"))
        if os.path.lexists(target) and not force:
            kept.append(m.name)
            continue
        os.makedirs(os.path.dirname(target), exist_ok=True)
        tmp = target + ".part"
        src = tf.extractfile(m)
        with open(tmp, "wb") as out:
            for block in iter(lambda: src.read(1 << 20), b""):
                out.write(block)
        os.chmod(tmp, 0o644)
        os.utime(tmp, (m.mtime, m.mtime))
        os.replace(tmp, target)
        extracted.append((m.name, m.size))

print(f"extracted {len(extracted)} file(s) into {os.path.abspath(dest)}"
      + (f"; kept {len(kept)} existing file(s) (use --force to overwrite)" if kept else ""))
for name, size in extracted:
    row = meta.get(name, {})
    print(f"  {size:>12,}  {row.get('era', '-'):<6} {row.get('status', '-'):<9} {name}")

if meta:
    names = {n for n, _ in extracted} | set(kept)
    for path, row in sorted(meta.items()):
        target = os.path.join(dest, *path.split("/"))
        if path not in names:
            print(f"missing from the archive: {path}"); problems += 1; continue
        if not os.path.isfile(target):
            print(f"missing after extraction: {path}"); problems += 1; continue
        if os.path.getsize(target) != int(row["bytes"]) or sha256(target) != row["sha256"]:
            note = " (existing file; rerun with --force)" if path in kept else ""
            print(f"does not match MANIFEST.tsv: {path}{note}"); problems += 1
    extra = sorted(names - set(meta))
    for path in extra:
        print(f"not listed in MANIFEST.tsv: {path}"); problems += 1
    by_status = Counter(meta[p]["status"] for p in meta)
    print("manifest: " + ", ".join(f"{k} {v}" for k, v in sorted(by_status.items()))
          + f"; {len(meta)} files checked: " + ("OK" if problems == 0 else f"{problems} problem(s)"))
    print("status 'excluded', 'failed' and 'restored' are explained in data/README.md")
sys.exit(1 if problems else 0)
PY
