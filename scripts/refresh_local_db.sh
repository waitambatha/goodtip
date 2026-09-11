#!/usr/bin/env bash
# Reseed the local dev database from the server.
#
# Local dev runs against a docker Postgres (container `goodtip_db`, port 5434)
# rather than the server box, because every query to the server costs a ~274ms
# round-trip — 29 of them on the dashboard alone. This script pulls a fresh
# snapshot down so local data still matches what's on the server.
#
#   ./scripts/refresh_local_db.sh              # from staging (the default)
#   ./scripts/refresh_local_db.sh --from live  # from production
#
# WHY STAGING IS THE DEFAULT
# --------------------------
# Staging is a verbatim, unscrubbed copy of live with no functional
# restrictions (see CLAUDE.md), so it answers every question production would
# and is the environment the work is actually shipped to first. Pulling from
# live means opening a connection to the database real members are sitting on,
# to see data that is already sitting on staging. Ask for it explicitly.
#
# TWO DIFFERENT ROUTES, AND WHY
# -----------------------------
# Production's Postgres accepts a connection from this machine — that is what
# REMOTE_DATABASE_URL is — so `--from live` runs pg_dump here, pointed there.
#
# Staging's does NOT: pg_hba.conf has no entry for goodtip_staging_db from
# outside the box, and adding one would put a second door on the copy of the
# database that holds every real member address. So `--from staging` runs
# pg_dump ON the server, over ssh, and streams the dump back down the pipe.
# Nothing is written to the server's disk and no port has to be opened.
set -euo pipefail

cd "$(dirname "$0")/.."

SOURCE=staging
while [ $# -gt 0 ]; do
  case "$1" in
    --from) SOURCE="${2:-}"; shift 2 ;;
    --from=*) SOURCE="${1#*=}"; shift ;;
    live|production|staging) SOURCE="$1"; shift ;;
    -h|--help) sed -n '2,29p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
case "$SOURCE" in
  production) SOURCE=live ;;
  live|staging) ;;
  *) echo "--from takes 'staging' or 'live', got '$SOURCE'" >&2; exit 2 ;;
esac

# The ssh host for `--from staging`, and the checkout whose .env names the
# staging database. Both are what CLAUDE.md documents; overridable so this is
# not the file to edit when a box moves.
SSH_HOST="${GOODTIP_SSH_HOST:-goodtip}"
STAGING_CHECKOUT="${GOODTIP_STAGING_CHECKOUT:-~/projects/goodtip-staging}"

# The target is read from its own dedicated key, never from DATABASE_URL.
# DATABASE_URL points at whichever database you're currently developing
# against, and this script runs `psql --clean` against its target, so aiming it
# at DATABASE_URL would drop and reload whatever that happened to be.
LOCAL=$(grep -E '^LOCAL_DATABASE_URL=' .env | cut -d= -f2-)

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

if [ "$SOURCE" = staging ]; then
  echo "==> dumping staging over ssh ($SSH_HOST)"
  # The URL is read out of staging's own .env on the server and used in the
  # same shell, so the staging password is never copied into this repo, into
  # this machine's .env, or into the ssh command line (where it would show up
  # in `ps` on the server). `grep | cut` rather than sourcing the file: it has
  # unquoted values with parentheses and spaces in them, which a shell chokes
  # on. --no-owner/--no-privileges because goodtip_user does not exist locally.
  ssh "$SSH_HOST" "URL=\$(grep -E '^DATABASE_URL=' $STAGING_CHECKOUT/.env | cut -d= -f2-); \
                   pg_dump \"\$URL\" --no-owner --no-privileges --clean --if-exists" > "$DUMP"
else
  echo "==> dumping PRODUCTION (slow — remote link, and this is the live database)"
  REMOTE=$(grep -E '^REMOTE_DATABASE_URL=' .env | cut -d= -f2-)
  if [ -z "$REMOTE" ]; then
    echo "REMOTE_DATABASE_URL is not set in .env" >&2
    exit 1
  fi
  pg_dump "$REMOTE" --no-owner --no-privileges --clean --if-exists -f "$DUMP"
fi

if [ ! -s "$DUMP" ]; then
  echo "the dump came back empty — nothing has been loaded" >&2
  exit 1
fi

echo "==> loading into local ($(du -h "$DUMP" | cut -f1))"
# Errors on the DROPs are expected the first time round, so don't stop on them.
psql "$LOCAL" -v ON_ERROR_STOP=0 -q -f "$DUMP" 2>&1 \
  | grep -vi 'does not exist, skipping' || true

echo "==> done, from $SOURCE"
psql "$LOCAL" -qtA -c \
  "select 'users=' || (select count(*) from accounts_user)
       || ' orgs=' || (select count(*) from orgs_organisation)
       || ' stories=' || (select count(*) from admin_panel_newspost)
       || ' matches=' || (select count(*) from tipping_match)
       || ' tips=' || (select count(*) from tipping_tip);"
