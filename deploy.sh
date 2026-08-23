#!/bin/bash
set -e

PROJECT_DIR="/home/mbatha-goodtip/projects/goodtip"
VENV="$PROJECT_DIR/venv"

cd "$PROJECT_DIR"

# Pull latest changes from GitHub.
#
# The deploy target is a working checkout, so a stray edit would abort the
# pull. Stash around it -- but only pop what THIS run stashed. `git stash`
# saves nothing on a clean tree and still exits 0, so an unconditional pop
# would restore whatever unrelated entry happened to be on top of the stack
# and quietly deploy it. Test the tree first and remember the answer.
STASHED=0
if ! git diff --quiet HEAD; then
  git stash push -m deploy-autostash
  STASHED=1
fi

git pull --rebase origin main

if [ "$STASHED" = 1 ]; then
  # A conflicting pop leaves markers in the tree and keeps the entry on the
  # stack. Stop here rather than run migrate and collectstatic over a file
  # with <<<<<<< in it: the old code is still serving, and the work is still
  # in `git stash list`. This is the one failure in this script worth being
  # fatal -- everything past this point writes to the database or the site.
  git stash pop || {
    echo "ERROR: conflict restoring local changes. Deploy stopped before migrate." >&2
    echo "       Resolve the conflict, then re-run this script." >&2
    exit 1
  }
fi

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
