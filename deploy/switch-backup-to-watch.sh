#!/bin/bash
# Run ONCE on the server, from the STAGING checkout, with sudo:
#     sudo bash deploy/switch-backup-to-watch.sh
#
# Moves the live -> staging backup from the hourly timer to a watcher that
# checks live every 15 seconds (client, 26 Oct 2026: "when it is created in
# live we immediately have it also backed up"). Staging only — it refuses to
# run from anywhere but the staging checkout.
set -e
STAGING_DIR=/home/mbatha-goodtip/projects/goodtip-staging
HERE="$(cd "$(dirname "$0")/.." && pwd)"
if [ "$(readlink -f "$HERE")" != "$(readlink -f "$STAGING_DIR")" ]; then
  echo "ERROR: run this from $STAGING_DIR, not $HERE." >&2
  exit 1
fi
install -m 644 "$HERE/deploy/systemd/goodtip-staging-backup-watch.service" /etc/systemd/system/
systemctl daemon-reload
systemctl disable --now goodtip-staging-backup-sync.timer || true
systemctl enable --now goodtip-staging-backup-watch.service
sleep 3
systemctl --no-pager --lines=5 status goodtip-staging-backup-watch.service
echo
echo "Done. The backup now watches live every 15 seconds."
