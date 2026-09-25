#!/usr/bin/env bash
# Serve the app on :8002 against the trailer database (never :8000, which is the
# real dev server).
set -euo pipefail
source "$(dirname "$0")/env.sh"
exec "$PY" "$ROOT/manage.py" runserver 127.0.0.1:8002 --noreload
