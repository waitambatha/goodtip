#!/bin/bash
# Install/refresh the scheduled-job units. Idempotent — safe on every deploy.
#
# Called from deploy.sh so scheduling ships WITH the code. Previously the sync
# schedule lived only in DEPLOYMENT.md as crontab lines someone had to paste in
# by hand; when that was missed the site ran with healthy feeds, correct
# fixtures in the database and an empty dashboard, with nothing anywhere
# reporting a problem. Scheduling is part of the application, so it is deployed
# like the application.
set -e

UNIT_SRC="$(cd "$(dirname "$0")" && pwd)/systemd"
UNIT_DST=/etc/systemd/system

changed=0
for unit in goodtip-matchsync.service goodtip-matchsync.timer \
            goodtip-jobs.service goodtip-jobs.timer \
            goodtip-backfill.service goodtip-backfill.timer \
            goodtip-autodeploy.service goodtip-autodeploy.timer; do
  if ! cmp -s "$UNIT_SRC/$unit" "$UNIT_DST/$unit"; then
    sudo cp "$UNIT_SRC/$unit" "$UNIT_DST/$unit"
    changed=1
  fi
done

if [ "$changed" = "1" ]; then
  sudo systemctl daemon-reload
  echo "match-sync units updated"
fi

# enable --now is idempotent: it starts the timer if stopped and does nothing
# if it is already running.
sudo systemctl enable --now goodtip-matchsync.timer
sudo systemctl enable --now goodtip-jobs.timer
sudo systemctl enable --now goodtip-backfill.timer
# Self-managing: from here on, edits to the autodeploy units deploy themselves.
sudo systemctl enable --now goodtip-autodeploy.timer

echo "Timer status:"
systemctl is-active goodtip-matchsync.timer goodtip-jobs.timer goodtip-backfill.timer goodtip-autodeploy.timer || true
systemctl list-timers goodtip-matchsync.timer goodtip-jobs.timer goodtip-backfill.timer goodtip-autodeploy.timer --no-pager || true
