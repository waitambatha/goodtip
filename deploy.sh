#!/bin/bash
# Deploy GoodTip: bring this checkout up to origin/main and reload the site.
#
# Two callers, one code path:
#   deploy.sh          -- by hand. Always does the full sequence.
#   deploy.sh --auto   -- from goodtip-autodeploy.timer, every 2 minutes.
#                         Exits silently when origin has nothing new, so the
#                         journal shows deploys and not 720 heartbeats a day.
set -e

PROJECT_DIR="/home/mbatha-goodtip/projects/goodtip"
VENV="$PROJECT_DIR/venv"

cd "$PROJECT_DIR"

AUTO=0
[ "${1:-}" = "--auto" ] && AUTO=1

# One deploy at a time. A 2-minute timer over a job that can take longer than
# two minutes will eventually overlap, and two `git pull --rebase` runs in one
# checkout is how you get a half-rebased tree. Non-blocking: if a deploy is
# already running, this tick has nothing to add -- the running one is already
# picking up whatever we would have fetched.
exec 9>"$PROJECT_DIR/.deploy.lock"
if ! flock -n 9; then
  [ "$AUTO" = 1 ] || echo "Another deploy is in progress; nothing to do."
  exit 0
fi

# Is there anything to do? Ask before touching the working tree, so the common
# case (nothing pushed) costs one fetch and no stash, no migrate, no reload.
git fetch --quiet origin main
BEFORE=$(git rev-parse HEAD)
REMOTE=$(git rev-parse FETCH_HEAD)

if [ "$AUTO" = 1 ] && [ "$BEFORE" = "$REMOTE" ]; then
  exit 0
fi

echo "=== deploy $(date -Is) ==="
[ "$BEFORE" = "$REMOTE" ] || echo "new on origin/main: $(git log --oneline "$BEFORE".."$REMOTE" | wc -l) commit(s)"

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

AFTER=$(git rev-parse HEAD)

# What actually changed, for the conditional steps below. Comparing the two
# commits directly rather than HEAD~1: a --rebase pull replays local commits on
# top, so HEAD~1 is not reliably where this deploy started.
CHANGED=$(git diff --name-only "$BEFORE" "$AFTER" 2>/dev/null || true)

source "$VENV/bin/activate"

# Dependencies: only when the pin file moved. pip is the slowest step here and
# it is almost never the one that changed.
if [ "$AUTO" = 0 ] || grep -q '^requirements.txt$' <<<"$CHANGED"; then
  pip install -q -r requirements.txt
fi

# Migrations and static always run when we get this far. Both are no-ops when
# there is nothing to do (~1s for collectstatic), and guessing wrong about
# whether they were needed is exactly the failure that puts the site out of
# step with its database or serves last week's CSS.
python manage.py migrate
python manage.py collectstatic --noinput

# Install/refresh the scheduled-job units only when the unit files changed.
#
# Never fatal. This script runs under `set -e` and installing units needs sudo,
# so a sudo prompt or a tightened sudoers rule would abort the deploy HERE --
# after the code is pulled and migrated, but before gunicorn is reloaded. The
# site would then be running the old code against the new database with nothing
# reporting a problem. A timer that failed to install is worth shouting about;
# it is not worth taking the deploy down with it.
if grep -q '^deploy/systemd/' <<<"$CHANGED"; then
  if ! bash "$PROJECT_DIR/deploy/install-timers.sh"; then
    echo "WARNING: timer install failed -- run deploy/install-timers.sh by hand." >&2
  fi
fi

# Reload the site. SIGHUP respawns the workers under the running arbiter, so
# this needs no sudo (gunicorn runs as this user) and drops no requests.
#
# pkill returns 1 when it matched nothing, which under `set -e` would abort --
# but "no gunicorn running" is a real problem worth reporting, not swallowing.
if pkill -HUP -f "gunicorn.*goodtip.wsgi"; then
  echo "gunicorn reloaded"
else
  echo "WARNING: no gunicorn process matched -- is goodtipservice running?" >&2
fi

echo "Deployment completed successfully ($BEFORE -> $AFTER)"
