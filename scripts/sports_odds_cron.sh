#!/bin/bash
# S1 collector entry point for launchd (research only, never trades).
#   scripts/sports_odds_cron.sh baseline|closing|resolve
# One lock per mode so a slow run is skipped, not stacked. Logs: logs/sports_odds/<mode>.log
set -u
MODE="${1:?usage: sports_odds_cron.sh baseline|closing|resolve}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="$ROOT/.venv/bin/python"
LOGDIR="$ROOT/logs/sports_odds"
LOCK="/tmp/s1_sports_odds_${MODE}.lock"
mkdir -p "$LOGDIR"
exec >>"$LOGDIR/$MODE.log" 2>&1
if ! mkdir "$LOCK" 2>/dev/null; then
  echo "$(date -u +%FT%TZ) $MODE: previous run still active, skipped"
  exit 0
fi
trap 'rmdir "$LOCK"' EXIT
cd "$ROOT" || exit 1
echo "$(date -u +%FT%TZ) $MODE start"
case "$MODE" in
  baseline) "$PY" scripts/sports_odds_scan.py --credit-reserve 2000 ;;
  closing)  "$PY" scripts/sports_odds_scan.py --closing --credit-reserve 2000 ;;
  resolve)  "$PY" scripts/sports_odds_resolve.py ;;
  *) echo "unknown mode $MODE"; exit 2 ;;
esac
echo "$(date -u +%FT%TZ) $MODE exit $?"
