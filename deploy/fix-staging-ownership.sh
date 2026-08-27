#!/bin/bash
# Give goodtip_user ownership of everything in the staging database.
#
# WHY THIS IS NEEDED. restore-staging-verbatim.sh pipes the production dump
# into psql AS THE POSTGRES SUPERUSER, because the dump file is 0600 and root
# owned and postgres cannot open it by path. Everything the restore creates is
# therefore owned by `postgres`, and the GRANT ALL that follows does not change
# that: GRANT covers reading and writing rows, ownership covers changing the
# table. So staging could serve traffic perfectly while being unable to run a
# single migration --
#
#     django.db.utils.ProgrammingError: must be owner of table accounts_user
#
# -- which is a failure that only appears the first time a deploy carries a
# schema change, long after the restore that caused it looked like it worked.
#
# Idempotent, and safe to run against a live staging: it takes an ACCESS
# EXCLUSIVE lock per table for the instant the ALTER takes, and nothing else.
#
# Run it as:  sudo bash deploy/fix-staging-ownership.sh
set -euo pipefail

STAGING_DB=goodtip_staging_db
DB_OWNER=goodtip_user

# Belt and braces: never let this be pointed at production by editing one word.
if [ "$STAGING_DB" = "goodtip_db" ]; then
  echo "refusing to run against production" >&2
  exit 1
fi

echo "==> before"
sudo -u postgres psql -tA -d "$STAGING_DB" -c \
  "select tableowner, count(*) || ' tables'
     from pg_tables where schemaname='public' group by tableowner;"

sudo -u postgres psql -q -v ON_ERROR_STOP=1 -d "$STAGING_DB" <<SQL
ALTER SCHEMA public OWNER TO $DB_OWNER;
DO \$\$
DECLARE r record;
BEGIN
  FOR r IN SELECT tablename AS n FROM pg_tables WHERE schemaname='public' LOOP
    EXECUTE format('ALTER TABLE public.%I OWNER TO $DB_OWNER', r.n);
  END LOOP;
  FOR r IN SELECT sequencename AS n FROM pg_sequences WHERE schemaname='public' LOOP
    EXECUTE format('ALTER SEQUENCE public.%I OWNER TO $DB_OWNER', r.n);
  END LOOP;
  FOR r IN SELECT viewname AS n FROM pg_views WHERE schemaname='public' LOOP
    EXECUTE format('ALTER VIEW public.%I OWNER TO $DB_OWNER', r.n);
  END LOOP;
END
\$\$;
SQL

echo "==> after"
sudo -u postgres psql -tA -d "$STAGING_DB" -c \
  "select tableowner, count(*) || ' tables'
     from pg_tables where schemaname='public' group by tableowner;"
echo "OWNERSHIP FIXED"
