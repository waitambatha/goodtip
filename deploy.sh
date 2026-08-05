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
if git diff HEAD~1 --name-only 2>/dev/null | grep -q "^deploy/systemd/"; then
  bash "$PROJECT_DIR/deploy/install-timers.sh"
fi

# Restart the service (graceful gunicorn reload — no sudo needed)
pkill -HUP -f "gunicorn.*goodtip.wsgi" || true

echo "Deployment completed successfully"
