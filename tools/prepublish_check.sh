#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# Pre-publication checks CP1-CP7 for this repository.
#
#   bash tools/prepublish_check.sh             # check what git would publish
#   bash tools/prepublish_check.sh --verbose   # list every hit
#   bash tools/prepublish_check.sh --scan results logs
#                                              # secret/identifier scan only, for
#                                              # files that are not in git (raw data)
#
# Files checked: `git ls-files --cached --others --exclude-standard`, i.e.
# tracked files plus untracked files that are not ignored.
#
#   CP1 course material   path denylist, sha256 overlap with
#                         tools/course_files.sha256, course template phrases
#   CP2 secrets           token/key patterns, values of secret env variables
#   CP3 identifiers       session paths, private links, institution name, pod
#                         hostnames, personal e-mail, absolute local paths
#   CP4 sizes and paths   <= 5 MB per file, <= 10 MB total, ASCII paths, no raw
#                         data, binary documents, archives or caches
#   CP5 licensing         legal files present, vendored SGLang file intact
#   CP6 numbers           tools/check_numbers.py passes
#   CP7 framing           wording to review (WARN only)
#
# Prints a PASS/FAIL table and exits 1 if any check FAILs. Needs bash, git and
# python3 (standard library only). The logic lives in tools/prepublish_check.py.
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
root=$(cd "$here/.." && pwd)

if ! command -v python3 >/dev/null 2>&1; then
  echo "prepublish_check: python3 is required" >&2
  exit 2
fi
if ! command -v git >/dev/null 2>&1; then
  echo "prepublish_check: git not found; falling back to a filesystem walk" >&2
fi

cd "$root"
export PYTHONDONTWRITEBYTECODE=1
export GIT_OPTIONAL_LOCKS=0
exec python3 "$here/prepublish_check.py" "$@"
