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
