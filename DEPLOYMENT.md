# GoodTip Deployment Setup

## Overview
GoodTip is now deployed on this server with the following components:

### Services
- **goodtipservice**: Django application running via Gunicorn on port 8000
- **nginx**: Reverse proxy serving the application on port 80
- **goodtip-sync.timer**: Auto-sync from GitHub every 5 minutes

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

The system automatically syncs from GitHub every 5 minutes via the `goodtip-sync.timer`.

### Manual Sync
```bash
cd ~/projects/goodtip
./deploy.sh
```

### What the sync does:
1. Pulls latest changes from GitHub (main branch)
2. Installs any new dependencies
3. Runs database migrations
4. Collects static files
5. Restarts the application service

### Sync Timer Status
```bash
sudo systemctl status goodtip-sync.timer
sudo systemctl list-timers goodtip-sync.timer
```

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

Add to the app user's crontab (`crontab -e`):

```cron
APP=/home/mbatha-goodtip/projects/goodtip

# In-play scores, quarter and clock. This is what puts "Q3 12:45" and a live
# score on the fixtures. Only touches rounds with a game near kickoff, so
# outside match windows it costs almost nothing.
*/2 * * * *  cd $APP && venv/bin/python manage.py sync_matches --live >> /var/log/goodtip/sync.log 2>&1

# Final scores. This is the job that grades tips and awards points.
*/15 * * * * cd $APP && venv/bin/python manage.py sync_matches --results >> /var/log/goodtip/sync.log 2>&1

# The draw itself — kickoff times and venues. Barely moves, so nightly.
30 4 * * *   cd $APP && venv/bin/python manage.py sync_matches --fixtures --all-rounds >> /var/log/goodtip/sync.log 2>&1

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
- With `DEBUG=True` email prints to the console. Set `EMAIL_SEND_FOR_REAL=true`
  to actually deliver while developing.

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

Before launch: verify the sending domain in Postmark (DKIM + Return-Path), or
delivery will be rejected.

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
