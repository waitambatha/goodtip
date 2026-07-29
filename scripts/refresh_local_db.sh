#!/usr/bin/env bash
# Reseed the local dev database from production.
#
# Local dev runs against a docker Postgres (container `goodtip_db`, port 5434)
# rather than the production box, because every query to production costs a
# ~274ms round-trip — 29 of them on the dashboard alone. This script pulls a
# fresh snapshot down so local data still matches what's on the server.
#
#   ./scripts/refresh_local_db.sh
set -euo pipefail

cd "$(dirname "$0")/.."

# Source and target are read from their own dedicated keys, never from
# DATABASE_URL. DATABASE_URL points at whichever database you're currently
# developing against — which is normally the server — and this script runs
# `psql --clean` against its target, so aiming it at DATABASE_URL would drop and
# reload production the moment someone switched.
REMOTE=$(grep -E '^REMOTE_DATABASE_URL=' .env | cut -d= -f2-)
LOCAL=$(grep -E '^LOCAL_DATABASE_URL=' .env | cut -d= -f2-)

if [ -z "$REMOTE" ]; then
  echo "REMOTE_DATABASE_URL is not set in .env" >&2
  exit 1
fi
if [ -z "$LOCAL" ]; then
  echo "LOCAL_DATABASE_URL is not set in .env" >&2
  exit 1
fi

# Belt and braces: this script destroys its target, so refuse outright unless
# that target is a loopback address.
case "$LOCAL" in
  *@127.0.0.1:*|*@localhost:*) ;;
  *)
    echo "REFUSING: LOCAL_DATABASE_URL is not a localhost address." >&2
    echo "  got: ${LOCAL%%:*}://…@$(echo "$LOCAL" | sed -E 's#.*@([^/]+)/.*#\1#')" >&2
    echo "  This script runs a destructive --clean restore against it." >&2
    exit 1
    ;;
esac

if ! docker ps --format '{{.Names}}' | grep -qx goodtip_db; then
  echo "==> starting goodtip_db container"
  docker start goodtip_db >/dev/null
  until docker exec goodtip_db pg_isready -U mbatha -d goodtip >/dev/null 2>&1; do sleep 1; done
fi

DUMP=$(mktemp /tmp/goodtip_dump_XXXXXX.sql)
trap 'rm -f "$DUMP"' EXIT

echo "==> dumping production (slow — remote link)"
pg_dump "$REMOTE" --no-owner --no-privileges --clean --if-exists -f "$DUMP"

echo "==> loading into local ($(du -h "$DUMP" | cut -f1))"
# Errors on the DROPs are expected the first time round, so don't stop on them.
psql "$LOCAL" -v ON_ERROR_STOP=0 -q -f "$DUMP" 2>&1 \
  | grep -vi 'does not exist, skipping' || true

echo "==> done"
psql "$LOCAL" -qtA -c \
  "select 'users=' || (select count(*) from accounts_user)
       || ' orgs=' || (select count(*) from orgs_organisation)
       || ' matches=' || (select count(*) from tipping_match)
       || ' tips=' || (select count(*) from tipping_tip);"
