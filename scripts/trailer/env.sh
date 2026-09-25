# Sourced by the trailer scripts. Points the app at the throwaway database and
# turns off every way the app could reach a real person or the live services.
# The password is read from .env and never printed.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
REAL_URL="$(grep -E '^DATABASE_URL=' "$ROOT/.env" | head -1 | cut -d= -f2-)"
export SOURCE_DB_NAME="${REAL_URL##*/}"
export DATABASE_URL="${REAL_URL%/*}/goodtip_trailer"
export DEBUG=True
export EMAIL_SEND_FOR_REAL=False
export SHOW_OTP_IN_CONSOLE=True
export STAGING_GATE=False
export POSTMARK_SERVER_TOKEN=
export STRIPE_SECRET_KEY= STRIPE_PUBLISHABLE_KEY= STRIPE_WEBHOOK_SECRET=
PY="$ROOT/venv/bin/python"
