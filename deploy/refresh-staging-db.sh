#!/bin/bash
# Rebuild the staging database from production, then scrub the personal data.
#
# Run it whenever staging's data has drifted far enough from production to stop
# being a useful preview -- after a season rolls over, before a demo, or after
# a staging experiment has made a mess. It is destructive to staging and reads
# production strictly read-only (pg_dump takes no locks that block writes).
#
#   sudo bash deploy/refresh-staging-db.sh --keep you@example.com,client@example.com
#
# The --keep addresses are the accounts left able to sign in. Sign-in is a code
# emailed to the address on the account, so an account whose address has been
# scrubbed to @staging.invalid can never receive one. Pass at least your own.
set -euo pipefail

PROD_DB="${PROD_DB:-goodtip_db}"
STAGING_DB="${STAGING_DB:-goodtip_staging_db}"
DB_OWNER="${DB_OWNER:-goodtip_user}"
STAGING_DIR="${GOODTIP_STAGING_DIR:-/home/mbatha-goodtip/projects/goodtip-staging}"
STAGING_USER="${STAGING_USER:-mbatha-goodtip}"
KEEP=""

while [ $# -gt 0 ]; do
  case "$1" in
    --keep) KEEP="${2:-}"; shift 2 ;;
    --keep=*) KEEP="${1#*=}"; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

# The one mistake this script must not make.
if [ "$PROD_DB" = "$STAGING_DB" ]; then
  echo "ERROR: PROD_DB and STAGING_DB are both '$PROD_DB'." >&2
  exit 1
fi

if [ -z "$KEEP" ]; then
  echo "WARNING: no --keep addresses. Every account will end up with an" >&2
  echo "         @staging.invalid address and nobody will be able to sign in." >&2
  read -r -p "Continue anyway? [y/N] " reply
  [ "$reply" = "y" ] || exit 1
fi

echo "This DROPS AND REBUILDS '$STAGING_DB' from '$PROD_DB'."
echo "Anything only on staging is lost. Production is read from, never written."
read -r -p "Type the staging database name to confirm: " typed
if [ "$typed" != "$STAGING_DB" ]; then
  echo "Did not match. Nothing done." >&2
  exit 1
fi

DUMP=$(mktemp /tmp/goodtip-prod-dump.XXXXXX.sql)
# The dump is a complete copy of the live database, including every member's
# real address, sitting in a world-readable directory until it is restored.
chmod 600 "$DUMP"
cleanup() { rm -f "$DUMP"; }
trap cleanup EXIT

echo "==> dumping $PROD_DB"
sudo -u postgres pg_dump --no-owner --no-privileges "$PROD_DB" > "$DUMP"
echo "    $(wc -l < "$DUMP") lines"

echo "==> recreating $STAGING_DB"
# Terminate stragglers first: a single idle gunicorn worker holding a connection
# is enough to make dropdb fail, and it fails after the confirmation prompt.
sudo -u postgres psql -q -d postgres -c \
  "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '$STAGING_DB' AND pid <> pg_backend_pid();" >/dev/null
sudo -u postgres dropdb --if-exists "$STAGING_DB"
sudo -u postgres createdb -O "$DB_OWNER" "$STAGING_DB"

echo "==> restoring"
# psql runs as postgres, which cannot open a root-owned 0600 file by path.
# Redirect instead: root's shell opens the fd, so the dump stays unreadable
# to everyone else while it sits in /tmp.
sudo -u postgres psql -q --set ON_ERROR_STOP=on -d "$STAGING_DB" >/dev/null <"$DUMP"
sudo -u postgres psql -q -d "$STAGING_DB" -c \
  "GRANT ALL ON ALL TABLES IN SCHEMA public TO $DB_OWNER;
   GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO $DB_OWNER;" >/dev/null

# From here the staging database holds a verbatim copy of every real member's
# personal data. If anything below fails, it must not be left that way.
scrub_failed() {
  echo >&2
  echo "ERROR: the scrub did not complete, so '$STAGING_DB' still holds real" >&2
  echo "       personal data. Dropping it rather than leaving it served." >&2
  sudo -u postgres dropdb --if-exists "$STAGING_DB"
  echo "       Dropped. Staging is down until this is re-run successfully." >&2
  exit 1
}

echo "==> migrating (production may be a schema behind staging's branch)"
sudo -u "$STAGING_USER" bash -c \
  "cd '$STAGING_DIR' && venv/bin/python manage.py migrate --noinput" || scrub_failed

echo "==> scrubbing personal data"
sudo -u "$STAGING_USER" bash -c \
  "cd '$STAGING_DIR' && venv/bin/python manage.py scrub_for_staging --keep '$KEEP'" || scrub_failed

echo "==> restarting staging"
sudo systemctl restart goodtip-staging.service

echo
echo "Done. staging.goodtip.com.au is serving a scrubbed copy of production."
