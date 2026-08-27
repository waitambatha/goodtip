#!/bin/bash
# Rebuild staging from production WITHOUT scrubbing.
#
# Staging is meant to be the live system with a gate in front of it: every real
# account signable, every feature testable. The scrub exists for the opposite
# case, so this is deliberately a separate script rather than a flag -- you
# should have to name which one you want.
#
# Consequence, stated once: the staging database ends up holding real member
# addresses, and with EMAIL_ALLOWLIST=* the jobs timer can mail them for real.
set -euo pipefail

PROD_DB=goodtip_db
STAGING_DB=goodtip_staging_db
DB_OWNER=goodtip_user
APP_USER=mbatha-goodtip
PROD_DIR=/home/mbatha-goodtip/projects/goodtip
STAGING_DIR=/home/mbatha-goodtip/projects/goodtip-staging
DUMP=$(mktemp /tmp/gt-restore.XXXXXX.sql)
chmod 600 "$DUMP"
trap 'rm -f "$DUMP"' EXIT

echo "==> stopping staging so it releases its database connections"
systemctl stop goodtip-staging.service

echo "==> dumping $PROD_DB (read-only; production is never written)"
sudo -u postgres pg_dump --no-owner --no-privileges "$PROD_DB" > "$DUMP"
echo "    $(wc -l < "$DUMP") lines"

# The service is stopped, but a psql session or a timer job can still hold a
# connection, and dropdb fails on any one of them.
echo "==> terminating stragglers"
sudo -u postgres psql -q -d postgres -c \
  "SELECT pg_terminate_backend(pid) FROM pg_stat_activity
    WHERE datname = '$STAGING_DB' AND pid <> pg_backend_pid();" >/dev/null

echo "==> recreating $STAGING_DB"
sudo -u postgres dropdb --if-exists "$STAGING_DB"
sudo -u postgres createdb -O "$DB_OWNER" "$STAGING_DB"

# Redirect rather than -f: psql runs as postgres and cannot open a root-owned
# 0600 file by path.
# SET ROLE first, so every object the restore creates is OWNED by the app user
# rather than by postgres. Without it the restore still works and staging still
# serves -- but GRANT ALL confers reading and writing, not ownership, and
# ALTER TABLE requires ownership. Staging then cannot run a single migration
# ("must be owner of table accounts_user"), which surfaces days later on the
# first deploy that carries a schema change rather than here where it was
# caused.
echo "==> restoring verbatim (no scrub)"
{ echo "SET ROLE $DB_OWNER;"; cat "$DUMP"; } |
  sudo -u postgres psql -q --set ON_ERROR_STOP=on -d "$STAGING_DB" >/dev/null
sudo -u postgres psql -q -d "$STAGING_DB" -c \
  "GRANT ALL ON ALL TABLES IN SCHEMA public TO $DB_OWNER;
   GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO $DB_OWNER;" >/dev/null

# And re-assert it regardless, so a restore run from an older copy of this
# script, or one interrupted midway, cannot leave staging un-migratable.
bash "$(dirname "$0")/fix-staging-ownership.sh" >/dev/null

echo "==> syncing media"
sudo -u "$APP_USER" rsync -a --delete "$PROD_DIR/media/" "$STAGING_DIR/media/"

echo "==> migrating (staging's branch may be ahead of production's schema)"
sudo -u "$APP_USER" bash -c "cd '$STAGING_DIR' && venv/bin/python manage.py migrate --noinput"

echo "==> starting staging"
systemctl start goodtip-staging.service

rm -f /tmp/gt.sql   # the earlier half-finished attempt, which holds real data

echo
sudo -u postgres psql -tAc \
  "select count(*) || ' accounts, ' ||
          count(*) filter (where email not like '%@staging.invalid') ||
          ' can sign in' from accounts_user" "$STAGING_DB"
echo "RESTORED"
