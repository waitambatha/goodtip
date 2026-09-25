#!/usr/bin/env bash
# Build goodtip_trailer: a throwaway database for filming the public trailer.
#
# The local `goodtip` database is a copy of staging, which is a copy of live,
# so it holds real members' names and addresses. The clips made from it are
# published on the public site, so they must never be filmed against it.
# This builds the schema fresh and copies over ONLY public reference data
# (catalogue, teams, MatchReader history) -- an allow-list, so a table added
# later is left out rather than leaked. Members, orgs, tips and chat are then
# invented by seed_trailer_db.py.
set -euo pipefail
C=goodtip_db; U=mbatha
psql_() { docker exec -i "$C" psql -U "$U" -v ON_ERROR_STOP=1 "$@"; }

psql_ -d postgres -c "DROP DATABASE IF EXISTS goodtip_trailer WITH (FORCE)"
psql_ -d postgres -c "CREATE DATABASE goodtip_trailer"
docker exec "$C" pg_dump -U "$U" -s goodtip | psql_ -q -d goodtip_trailer >/dev/null

TABLES=(django_migrations django_content_type auth_permission
  catalog_charity catalog_competition catalog_competition_series catalog_country
  catalog_goodlistconfig catalog_grouptype catalog_organisationtype catalog_season
  catalog_series catalog_sport catalog_state catalog_subcategory
  tipping_team matchreader_historicalmatch matchreader_modelversion
  billing_foundingrate)
ARGS=(); for t in "${TABLES[@]}"; do ARGS+=(-t "$t"); done
docker exec "$C" pg_dump -U "$U" -a --disable-triggers "${ARGS[@]}" goodtip | psql_ -q -d goodtip_trailer >/dev/null
echo "goodtip_trailer ready"
