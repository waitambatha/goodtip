#!/usr/bin/env bash
# Keep fixtures, live scores and results moving while you work.
#
# The three feeds move at different speeds, so they run on different clocks —
# same split the production cron uses (see data_sync/management/commands/
# sync_matches.py), just collapsed into one foreground process you can Ctrl-C.
#
#   live      every 60s   score, quarter and clock while a game is on
#   results   every 15m   final scores, which is what grades tips
#   fixtures  every 6h    the draw itself, which barely moves
#
# Usage:  ./scripts/watch_live.sh [org_id]
# Leaving org_id off sweeps every org.

set -uo pipefail
cd "$(dirname "$0")/.."

PY=./venv/bin/python
ORG_ARG=""
[ $# -ge 1 ] && ORG_ARG="--org $1"

LIVE_EVERY=60
RESULTS_EVERY=900
FIXTURES_EVERY=21600

now() { date +%s; }
log() { printf '%s  %s\n' "$(date '+%H:%M:%S')" "$*"; }

last_results=0
last_fixtures=0

log "watching${ORG_ARG:+ org $1} — live/${LIVE_EVERY}s results/${RESULTS_EVERY}s fixtures/${FIXTURES_EVERY}s"
log "Ctrl-C to stop"

# Feed outages are normal and must not kill the loop: sync_matches records each
# attempt as a SyncRun either way, so a failure is visible in the admin rather
# than as silence here.
while true; do
  t=$(now)

  if [ $((t - last_fixtures)) -ge $FIXTURES_EVERY ]; then
    log "fixtures…"
    $PY manage.py sync_matches --fixtures --all-rounds $ORG_ARG 2>&1 | tail -1
    last_fixtures=$t
  fi

  if [ $((t - last_results)) -ge $RESULTS_EVERY ]; then
    log "results…"
    $PY manage.py sync_matches --results --all-rounds $ORG_ARG 2>&1 | tail -1
    last_results=$t
  fi

  $PY manage.py sync_matches --live $ORG_ARG 2>&1 | tail -1

  sleep $LIVE_EVERY
done
