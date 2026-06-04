#!/usr/bin/env bash
set -euo pipefail

echo "==> Goodtip local setup"

# --- 1. Python -----------------------------------------------------------
if ! command -v python3 >/dev/null 2>&1; then
  echo "ERROR: Python 3 is not installed."
  echo "  macOS:  brew install python@3.12"
  echo "  Linux:  sudo apt install python3 python3-venv python3-pip"
  exit 1
fi
echo "    Found $(python3 --version)"

# --- 2. Postgres ---------------------------------------------------------
if ! command -v psql >/dev/null 2>&1; then
  echo "ERROR: PostgreSQL client (psql) not found."
  echo "  macOS:  brew install postgresql@16 && brew services start postgresql@16"
  echo "  Linux:  sudo apt install postgresql postgresql-contrib"
  exit 1
fi

echo "==> Ensuring Postgres role 'mbatha' and database 'goodtip' exist"
if [[ "$OSTYPE" == "darwin"* ]]; then
  PSQL_SUPER=(psql -d postgres)
else
  PSQL_SUPER=(sudo -u postgres psql)
fi

if ! "${PSQL_SUPER[@]}" -tAc "SELECT 1 FROM pg_roles WHERE rolname='mbatha'" | grep -q 1; then
  echo "    Creating role 'mbatha'"
  "${PSQL_SUPER[@]}" -c "CREATE ROLE mbatha WITH LOGIN PASSWORD 'masterclass' CREATEDB;"
fi

if ! "${PSQL_SUPER[@]}" -tAc "SELECT 1 FROM pg_database WHERE datname='goodtip'" | grep -q 1; then
  echo "    Creating database 'goodtip'"
  "${PSQL_SUPER[@]}" -c "CREATE DATABASE goodtip OWNER mbatha;"
fi

# --- 3. venv + dependencies ---------------------------------------------
if [ ! -d venv ]; then
  echo "==> Creating Python virtual environment in ./venv"
  python3 -m venv venv
fi

echo "==> Installing Python dependencies"
./venv/bin/pip install --quiet --upgrade pip
./venv/bin/pip install --quiet -r requirements.txt

# --- 4. .env -------------------------------------------------------------
if [ ! -f .env ]; then
  echo "==> Creating .env from .env.example with a random SECRET_KEY"
  cp .env.example .env
  SECRET=$(openssl rand -base64 48 | tr -d '\n/=+' | cut -c1-50)
  sed "s|^SECRET_KEY=.*|SECRET_KEY=${SECRET}|" .env > .env.tmp && mv .env.tmp .env
else
  echo "==> .env already exists, leaving it as-is"
fi

# --- 5. Migrate + seed ---------------------------------------------------
echo "==> Running database migrations"
./venv/bin/python manage.py migrate --noinput

echo "==> Seeding teams"
./venv/bin/python manage.py seed_teams || echo "    (seed_teams skipped)"

# --- 6. Default admin user ----------------------------------------------
echo "==> Ensuring default admin user (admin / admin)"
./venv/bin/python manage.py shell -c "
from django.contrib.auth import get_user_model
U = get_user_model()
if not U.objects.filter(username='admin').exists():
    U.objects.create_superuser('admin', 'admin@example.com', 'admin')
    print('Created superuser admin/admin')
else:
    print('Superuser admin already exists')
"

# --- 7. Run the dev server ----------------------------------------------
cat <<EOF

============================================================
 Goodtip is ready.

 Sign in:
   URL:      http://localhost:8000/admin
   username: admin
   password: admin

 Starting the dev server now on http://localhost:8000
 (press Ctrl+C to stop)
============================================================

EOF
exec ./venv/bin/python manage.py runserver 8000
