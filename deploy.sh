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

# Restart the service
sudo systemctl restart goodtipservice

echo "Deployment completed successfully"
