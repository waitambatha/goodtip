# GoodTip Deployment Setup

## Overview
GoodTip is now deployed on this server with the following components:

### Services
- **goodtipservice**: Django application running via Gunicorn on port 8000
- **nginx**: Reverse proxy serving the application on port 80
- **goodtip-sync.timer**: Auto-deploy from GitHub every 2 minutes (idle ticks are no-ops)

### Database
- PostgreSQL database: `goodtip_db`
- User: `goodtip_user`
- Connection: `postgres://goodtip_user:<password-from-.env>@localhost:5432/goodtip_db` (real credentials live only in `.env`)

### Project Location
- `/home/mbatha-goodtip/projects/goodtip`

## Service Management

### Start/Stop Services
```bash
# Start all services
sudo systemctl start goodtipservice nginx

# Stop all services
sudo systemctl stop goodtipservice nginx

# Restart services
sudo systemctl restart goodtipservice nginx

# Check status
sudo systemctl status goodtipservice
sudo systemctl status nginx
```

### View Logs
```bash
# Application logs
sudo journalctl -u goodtipservice -f

# Nginx logs
sudo tail -f /var/log/nginx/error.log
sudo tail -f /var/log/nginx/access.log

# Sync logs
sudo journalctl -u goodtip-sync.service -f
```

## Auto-Sync from GitHub

**Pushing to `main` is the deploy.** `goodtip-sync.timer` checks origin every
2 minutes, so a push is live within ~2 minutes with nothing to run by hand.

It is pull-based rather than a GitHub webhook: no inbound port, no public
endpoint and no shared secret to leak. The box asks GitHub; GitHub is never
told about the box. The trade is latency, bounded by the timer interval.

### What a tick does

Almost always: one `git fetch`, notices origin has not moved, exits. This
matters more than it sounds. `deploy.sh --auto` returns *before* touching the
working tree, so an idle tick runs no migrations, no collectstatic and no
reload. (Until Aug 2026 the timer ran `deploy.sh` with no arguments and did
the full sequence every 5 minutes, recycling the gunicorn workers around the
clock whether or not anything had been pushed.)

When origin **has** moved:

1. Stashes any stray local edit, pulls `--rebase`, restores it
   (a conflict here aborts the deploy *before* migrate — see below)
2. `pip install` — only when `requirements.txt` itself changed
3. Runs database migrations
4. Collects static files
5. Reinstalls the job units — only when `deploy/systemd/` changed
6. SIGHUPs gunicorn: workers reload the new code, arbiter stays up,
   no dropped requests and no sudo (gunicorn runs as `mbatha-goodtip`)

A `flock` keeps two deploys out of the one checkout, so a slow deploy simply
makes the next tick a no-op instead of colliding with it.

### Manual sync

```bash
cd ~/projects/goodtip
./deploy.sh            # full sequence, skips none of the steps above
```

### Is it working?

```bash
sudo systemctl list-timers goodtip-sync.timer   # next/last run
journalctl -u goodtip-sync -n 50                # deploys only; idle ticks are silent
```

A healthy journal is *quiet* — entries appear when something was actually
deployed. "Deployment completed successfully" prints the before/after commit.

### When it stops

The one case that halts a deploy is a local edit on the server that conflicts
with what was pushed. `deploy.sh` stops before migrating, leaves the old code
serving and keeps the work in `git stash list`. Resolve on the server and the
next tick carries on:

```bash
cd ~/projects/goodtip && git status && git stash list
```

Note the deploy verifies nothing about the code itself — a push that passes
CI-less straight to `main` is a push that reaches the public site in two
minutes. The staging gate is what stands between a bad push and the public,
not the deploy pipeline.

## Configuration

### Environment Variables
Edit `/home/mbatha-goodtip/projects/goodtip/.env` to configure:
- `SECRET_KEY`: Django secret key (change in production)
- `DEBUG`: Set to False in production
- `ALLOWED_HOSTS`: Domain names
- `DATABASE_URL`: PostgreSQL connection string
- Email settings (SMTP)
- Stripe API keys
- TheSports API key

### Nginx Configuration
- Location: `/etc/nginx/sites-available/goodtip`
- Serves static files from: `/home/mbatha-goodtip/projects/goodtip/staticfiles/`
- Proxies requests to Gunicorn on `127.0.0.1:8000`

### Gunicorn Configuration
- Workers: 4
- Timeout: 120 seconds
- Binding: `127.0.0.1:8000`

## Domain Setup

The application is configured for:
- `goodtip.com.au`
- `www.goodtip.com.au`

Update DNS records to point to this server's IP address.

## SSL/TLS (HTTPS)

To enable HTTPS, install Certbot:
```bash
sudo apt-get install certbot python3-certbot-nginx
sudo certbot --nginx -d goodtip.com.au -d www.goodtip.com.au
```

## Database Migrations

Migrations run automatically during sync. To manually run:
```bash
cd ~/projects/goodtip
source venv/bin/activate
python manage.py migrate
```

## Static Files

Static files are collected automatically during sync. To manually collect:
```bash
cd ~/projects/goodtip
source venv/bin/activate
python manage.py collectstatic --noinput
```

## Troubleshooting

### Service won't start
```bash
sudo journalctl -xeu goodtipservice.service
```

### Database connection issues
```bash
# Test PostgreSQL connection
psql -U goodtip_user -d goodtip_db -h localhost
```

### Nginx errors
```bash
sudo nginx -t  # Test configuration
sudo systemctl restart nginx
```

### Sync not working
```bash
# Check timer
sudo systemctl status goodtip-sync.timer

# Run manually
~/projects/goodtip/deploy.sh

# Check logs
sudo journalctl -u goodtip-sync.service -n 50
```

## Scheduled Jobs (match data, elections, recaps)

These are **application** jobs, separate from `goodtip-sync.timer` (which only
deploys code from GitHub). None of them can run inside a web request — a feed
round-trip is far too slow for a page load — so they must be scheduled.


### Automatic syncing — how it runs

Match data syncing runs on a **systemd timer**, `goodtip-matchsync.timer`,
firing every 2 minutes. It is installed automatically: `deploy.sh` calls
`deploy/install-timers.sh` on every deploy, so scheduling ships with the code
and a fresh server is never left running an app that never pulls data.

The timer calls one command:

```bash
venv/bin/python manage.py run_due_syncs
```

`run_due_syncs` reads the `SyncSchedule` table to decide what is actually due —
live every 2 min, results every 15 min, fixtures hourly, ladder every 30 min —
so a 2-minute tick does not mean four feed calls every two minutes. Claiming a
slot is a single conditional UPDATE, so a slow run still going when the next
tick fires cannot double-hit a feed.

### The full-season sweep — `goodtip-backfill.timer`

A **second** timer, every 6 hours:

```bash
venv/bin/python manage.py sync_matches --backfill
```

It exists because everything above works from a window around today — one round
back, three forward. That keeps this week correct and is structurally incapable
of repairing a round that fell outside the window when the sync ran. The live
database held AFL 2026 rounds 1-5 and 21-25 with a permanent hole in between
for exactly that reason: nothing was ever going to ask about round 12 again.

The sweep asks each feed for **every** round it publishes, then runs fixtures,
results and ladder over the lot, so any hole closes itself within six hours.

It is a separate unit on purpose. `run_due_syncs` runs its kinds sequentially in
one oneshot process, so a sweep sharing that unit would park the two-minute live
poller behind it for as long as it ran. Its `TimeoutStartSec` is 2 hours, since
a first pass on an empty season has a lot to create; later passes are far
cheaper because the syncs write only what actually changed.

```bash
systemctl list-timers goodtip-backfill.timer
journalctl -u goodtip-backfill.service -n 50
manage.py sync_matches --backfill      # run the sweep now
manage.py rebuild_ladders --season 2026  # ladders only, no network at all
```

### Is the data actually complete?

Freshness stamps answer "did it run", never "is anything missing" — every sync
can report success while half a season is unfetched. The **Coverage** panel at
`/manage/sync/` is the one to read: rounds held per series, any missing round
numbers, results stored, and ladder rows. Note that Super League and Super
Netball appear there with no feed at all; they are 2027 roadmap entries with no
scraper and no teams, so leagues signed up to them will not receive fixtures.

**This is deliberately independent of site traffic.** Data has to be current
when a visitor arrives, not fetched because one did — someone opening the
dashboard at 3am must find last night's results already in.

Checking it:

```bash
systemctl status goodtip-matchsync.timer
systemctl list-timers goodtip-matchsync.timer
journalctl -u goodtip-matchsync.service -n 50
manage.py run_due_syncs --force        # run everything now, ignoring schedule
```

`AUTOSYNC_ENABLED=True` in `.env` turns on a traffic-driven fallback for an
environment with no systemd (a bare container, a staging box). Leave it off in
production: it only syncs while someone is browsing, and its work runs on a web
worker thread that dies mid-sync whenever the service restarts.

The crontab entries below are the equivalent for a server without systemd. Use
one mechanism or the other — both claim through the same lock, so running both
is harmless but pointless.

Add to the app user's crontab (`crontab -e`):

```cron
APP=/home/mbatha-goodtip/projects/goodtip

# In-play scores, quarter and clock. This is what puts "Q3 12:45" and a live
# score on the fixtures. Only touches rounds with a game near kickoff, so
# outside match windows it costs almost nothing.
*/2 * * * *  cd $APP && venv/bin/python manage.py sync_matches --live >> /var/log/goodtip/sync.log 2>&1

# Final scores. This is the job that grades tips and awards points.
*/15 * * * * cd $APP && venv/bin/python manage.py sync_matches --results >> /var/log/goodtip/sync.log 2>&1

# The draw. Runs in DISCOVERY mode: asks each feed which rounds it is
# publishing and creates whatever is missing, rather than refreshing the rounds
# already held. This is the only job that can bring a NEW round or game into
# the database, and it is what makes a league created today have fixtures today
# — so hourly, not nightly. One feed request per competition per league.
7 * * * *    cd $APP && venv/bin/python manage.py sync_matches --fixtures >> /var/log/goodtip/sync.log 2>&1

# Charity elections: open the ones whose start time has passed, close the ones
# whose end time has passed.
*/10 * * * * cd $APP && venv/bin/python manage.py open_due_elections >> /var/log/goodtip/jobs.log 2>&1

# Vote reminders to members who haven't voted — one a day out, one an hour out.
# Each is stamped on the vote once sent, so running often doesn't mean nagging.
*/10 * * * * cd $APP && venv/bin/python manage.py send_election_reminders >> /var/log/goodtip/jobs.log 2>&1

# Result emails: per-member round scorecards (only once every fixture in the
# round is graded) and closed-election outcomes.
0 * * * *    cd $APP && venv/bin/python manage.py send_result_emails >> /var/log/goodtip/jobs.log 2>&1

# AI round recaps for graded rounds (skipped silently unless ANTHROPIC_API_KEY is set).
15 6 * * *   cd $APP && venv/bin/python manage.py generate_recaps >> /var/log/goodtip/jobs.log 2>&1
```

```bash
sudo mkdir -p /var/log/goodtip && sudo chown mbatha-goodtip /var/log/goodtip
```

### Checking it's working

Every attempt is recorded as a `data_sync.SyncRun` row, so freshness is visible
in the app rather than only in logs:

- **Admin → Sync** shows "last successful run" per feed kind plus the last dozen
  runs. A stamp that stops advancing is the first sign a cron has died.
- Django admin → *Sync runs* has the full history with error messages.

```bash
# one-off manual run, verbose
cd $APP && venv/bin/python manage.py sync_matches --live --round 12
```

### Feed coverage

| Competition | Feed | Fixtures | Final scores | Live clock + score |
| --- | --- | --- | --- | --- |
| AFL | Squiggle (no key needed) | yes | yes | yes (`complete` %, `timestr`) |
| NRL / NRLW | TheSports API | **not implemented** | **not implemented** | **not implemented** |

`TheSportsAPISyncService` is still a stub and raises `SyncError`; the scheduled
command logs that and carries on with the competitions that do work, so an
unimplemented feed never blocks AFL. Wiring NRL needs `THESPORTS_API_KEY` in
`.env` plus the client methods.

## Email (Postmark)

Transactional email goes through the Postmark API via
`goodtip.email_backends.PostmarkEmailBackend`, which is a normal Django email
backend — every `send_mail` / `EmailMultiAlternatives` caller in the codebase
works through it unchanged.

- Set `POSTMARK_SERVER_TOKEN` in `.env` and production uses Postmark.
- Leave it blank and production falls back to SMTP (`EMAIL_HOST` etc.), exactly
  as before.
- With `DEBUG=True`, `EMAIL_SEND_FOR_REAL` picks the backend: `true` (the
  current default) delivers through Postmark, `false` prints to the console.
- `DEFAULT_FROM_EMAIL` must be an address Postmark has verified as a sender
  signature. The `goodtip.com.au` domain is approved, so `no-reply@goodtip.com.au`
  is the sender.

Check it end to end without going through a signup:

```bash
venv/bin/python manage.py check_email you@example.com            # sends one
venv/bin/python manage.py check_email you@example.com --dry-run  # config only
```

It prints the resolved backend, sender and token, then reports Postmark's own
verdict — including the per-message rejection (e.g. `[300]` for an unverified
`From`) that the backend otherwise only writes to a log.

Messages live in `templates/emails/`, each as an `.html` + `.txt` pair sharing
`emails/_base.html`:

| Template | Sent when |
| --- | --- |
| `welcome` | on signup |
| `election_open` | an election opens |
| `election_reminder` | 1 day and 1 hour before a vote closes (non-voters only) |
| `election_result` | a vote closes |
| `tip_results` | a round is fully graded |
| `news_published` | a news post goes out |
| `tell_the_boss` | a member sends the boss note |

Senders live in `orgs/notifications.py`; `goodtip/mail.py` renders both parts and
batches the fan-out sends. Every send path is best-effort — a mail failure is
logged and never breaks the action that triggered it.

The `goodtip.com.au` sending domain is verified in Postmark (DKIM +
Return-Path). Any new sending domain needs the same before it will deliver.

## GitHub SSH Setup

SSH key for GitHub is configured at `~/.ssh/github_key`. The public key has been added to your GitHub account.

To verify SSH connection:
```bash
ssh -T git@github.com
```

## Next Steps

1. Update `.env` with production values (SECRET_KEY, email settings, API keys)
2. Set up SSL/TLS with Certbot
3. Configure email settings for notifications
4. Add Stripe API keys if using billing features
5. Monitor logs and service health
