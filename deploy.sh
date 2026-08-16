#!/bin/bash
set -e

PROJECT_DIR="/home/mbatha-goodtip/projects/goodtip"
VENV="$PROJECT_DIR/venv"

cd "$PROJECT_DIR"

# Pull latest changes from GitHub
git pull origin main

# Activate venv and install any new dependencies
source "$VENV/bin/activate"
pip install -q -r requirements.txt

# Run migrations
python manage.py migrate

# Collect static files
python manage.py collectstatic --noinput

# Install/refresh the scheduled-job units only when the unit files changed.
#
# Never fatal. This script runs under `set -e` and installing units needs sudo,
# so a sudo prompt or a tightened sudoers rule would abort the deploy HERE —
# after the code is pulled and migrated, but before gunicorn is reloaded. The
# site would then be running the old code against the new database with nothing
# reporting a problem. A timer that failed to install is worth shouting about;
# it is not worth taking the deploy down with it.
if git diff HEAD~1 --name-only 2>/dev/null | grep -q "^deploy/systemd/"; then
  if ! bash "$PROJECT_DIR/deploy/install-timers.sh"; then
    echo "WARNING: timer install failed — run deploy/install-timers.sh by hand." >&2
  fi
fi

# Restart the service (graceful gunicorn reload — no sudo needed)
pkill -HUP -f "gunicorn.*goodtip.wsgi" || true

echo "Deployment completed successfully"
