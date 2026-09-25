#!/usr/bin/env bash
# Rebuild the trailer database from scratch and seed it. Takes about a minute.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
source "$HERE/env.sh"
"$HERE/build_db.sh"
"$PY" "$ROOT/scripts/trailer/seed_trailer_db.py"
"$PY" "$ROOT/manage.py" rebuild_ladders
