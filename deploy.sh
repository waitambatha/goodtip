#!/bin/bash
# Deploy GoodTip: bring this checkout up to origin/main and reload the site.
#
# Two callers, one code path:
#   deploy.sh          -- by hand. Always does the full sequence.
#   deploy.sh --auto   -- from goodtip-sync.timer, every 2 minutes.
#                         Exits silently when origin has nothing new, so the
#                         journal shows deploys and not 720 heartbeats a day.
#
# Before anything is migrated or served, the pulled code goes through a
# verification gate (see DEPLOY_TEST_GATE below). A commit that fails is
# rolled straight back out of the tree, so the site keeps serving the last
# code that passed.
set -e

# Run from an immutable copy of this file.
#
# This script rewrites itself in the ordinary course of its job: the stash
# before the pull, and the pull itself, both edit deploy.sh while bash is only
# part-way through reading it. Bash reads a script lazily by byte offset, so an
# edit that shifts those offsets makes it resume mid-token, skip a line, or run
# a line that was appended after it started -- all three verified locally with
# a script that appends to itself. Every deploy that touches deploy.sh is
# therefore a coin flip on a production box. Copying first costs a millisecond
# and makes the file being executed impossible to change underneath us.
if [ -z "${DEPLOY_SELF_COPY:-}" ]; then
  SELF_COPY=$(mktemp /tmp/goodtip-deploy.XXXXXX)
  cp "$0" "$SELF_COPY"
  DEPLOY_SELF_COPY=1 bash "$SELF_COPY" "$@"
  rc=$?
  rm -f "$SELF_COPY"
  exit $rc
fi

# Overridable so the script can be exercised against a throwaway clone rather
# than only ever being tested in production. Defaults to the real checkout.
PROJECT_DIR="${GOODTIP_DIR:-/home/mbatha-goodtip/projects/goodtip}"
VENV="${GOODTIP_VENV:-$PROJECT_DIR/venv}"

cd "$PROJECT_DIR"

AUTO=0
[ "${1:-}" = "--auto" ] && AUTO=1

# One deploy at a time. A 2-minute timer over a job that can take longer than
# two minutes will eventually overlap, and two `git pull --rebase` runs in one
# checkout is how you get a half-rebased tree. Non-blocking: if a deploy is
# already running, this tick has nothing to add -- the running one is already
# picking up whatever we would have fetched.
# Lock lives inside .git: same filesystem, never tracked, so it can never
# collide with an incoming pull the way a dotfile in the tree root does.
exec 9>"$PROJECT_DIR/.git/deploy.lock"
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

# A commit that already failed the gate is not retried every two minutes: the
# result will not change until someone pushes something, and re-running a
# six-minute suite 720 times a day to relearn the same answer helps nobody.
# The failure was logged loudly once, when it happened.
BLOCKED_FILE="$PROJECT_DIR/.git/deploy-blocked-sha"
if [ "$AUTO" = 1 ] && [ -f "$BLOCKED_FILE" ] && [ "$(cat "$BLOCKED_FILE")" = "$REMOTE" ]; then
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

# --- verification gate ------------------------------------------------------
#
# Everything below this point either writes to the database or changes what the
# public sees, so this is the last place a bad commit can be stopped cheaply.
# It runs against the pulled code with local edits still stashed -- what is
# being judged is what is in git, not whatever someone left in the tree.
#
# DEPLOY_TEST_GATE (set in goodtip-sync.service):
#   fast   default. `check` + `makemigrations --check`, ~1.5s. Catches import
#          and syntax errors and model/migration drift.
#   block  fast, then the full suite. A failure stops the deploy.
#   warn   fast blocks; suite failures are reported but deploy anyway.
#   off    no gate at all.
#
# Why `fast` is the default and not `block`: as of Aug 2026 the suite is red
# (461 tests, 11 failures, 6 errors) and takes ~6 minutes. Switching to block
# today would stop every deploy on arrival. Move to `block` once it is green.
#
# Worth being honest about the limit of `fast`: it imports the code, so it
# catches a module that will not load, but not a name that only blows up when
# a request reaches it. The NameError shipped on 2026-08-23 -- a function
# raising an exception class nobody had defined -- passes `check` cleanly. Only
# the suite catches that class of bug.
GATE="${DEPLOY_TEST_GATE:-fast}"

gate_reject() {
  echo "ERROR: $1" >&2
  echo "       Deploy stopped before migrate; $(git rev-parse --short "$BEFORE") is still serving." >&2
  echo "       Rolling the tree back so disk matches what is running." >&2
  echo "$REMOTE" > "$BLOCKED_FILE"
  git reset --hard "$BEFORE" >&2
  if [ "$STASHED" = 1 ]; then
    git stash pop >&2 || echo "WARNING: local changes left in \`git stash list\`." >&2
  fi
  echo "       This commit will not be retried until something new is pushed." >&2
  exit 1
}

if [ "$GATE" != "off" ]; then
  python manage.py check || gate_reject "django system check failed"
  python manage.py makemigrations --check --dry-run >/dev/null \
    || gate_reject "models changed with no migration -- run makemigrations and commit it"
fi

if [ "$GATE" = "block" ] || [ "$GATE" = "warn" ]; then
  # --noinput matters: a leftover test database otherwise prompts for
  # confirmation and dies on EOF with no tty, which reads as a test failure.
  if python manage.py test --noinput; then
    echo "test suite passed"
  elif [ "$GATE" = "block" ]; then
    gate_reject "test suite failed"
  else
    echo "WARNING: test suite failed; deploying anyway (DEPLOY_TEST_GATE=warn)." >&2
  fi
fi

# Gate passed. Restore local edits now -- after the verdict, so a stray edit on
# the server can neither rescue a broken commit nor sink a good one.
if [ "$STASHED" = 1 ]; then
  # A conflicting pop leaves markers in the tree and keeps the entry on the
  # stack. Stop here rather than run migrate and collectstatic over a file
  # with <<<<<<< in it: the old code is still serving, and the work is still
  # in `git stash list`.
  git stash pop || {
    echo "ERROR: conflict restoring local changes. Deploy stopped before migrate." >&2
    echo "       Resolve the conflict, then re-run this script." >&2
    exit 1
  }
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

rm -f "$BLOCKED_FILE"
echo "Deployment completed successfully ($BEFORE -> $AFTER)"
