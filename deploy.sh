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

# Install/refresh the scheduled-job units. Scheduling ships with the code so a
# fresh server is never left with an app that runs but never pulls any data.
bash "$PROJECT_DIR/deploy/install-timers.sh"

# Restart the service (graceful gunicorn reload — no sudo needed)
pkill -HUP -f "gunicorn.*goodtip.wsgi" || true

echo "Deployment completed successfully"
