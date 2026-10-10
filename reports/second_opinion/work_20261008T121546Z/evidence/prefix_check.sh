#!/bin/bash
# Usage (bash or zsh with ${=spec}): prefix_check.sh <sha> <test files...>   (run from repo root)
# Extracts parent tree to /tmp/audit_prefix_<sha>, overlays the commit's tests/, runs pytest hermetically.
# HARDENED after incident (see notes): SHA validated, all paths quoted, rm only under /tmp/audit_prefix_.
set -euo pipefail
REPO=$(pwd); SHA="$1"; shift
[[ "$SHA" =~ ^[0-9a-f]{7,40}$ ]] || { echo "bad sha: $SHA" >&2; exit 2; }
P="/tmp/audit_prefix_${SHA}"
case "$P" in /tmp/audit_prefix_*) ;; *) exit 3;; esac
rm -rf -- "$P"; mkdir -p -- "$P"
git archive "${SHA}^" | tar -x -C "$P"
git archive "$SHA" tests | tar -x -C "$P"
cd "$P" && TRADE_DB_ENABLED=0 "$REPO/.venv/bin/python" -m pytest -q -p no:cacheprovider "$@" 2>&1 | tail -6
