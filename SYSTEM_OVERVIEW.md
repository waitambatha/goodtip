# GoodTip System Overview

## What is GoodTip?

GoodTip is a Django-based sports tipping platform where users form leagues, make predictions on sports matches, and donate entry fees to charities of their choice. It's a gamified fundraising platform for Australian sports (AFL, AFLW, NRL, NRLW).

---

## Core Architecture

### Tech Stack
- **Backend**: Django 5.2 (Python 3.12)
- **Database**: PostgreSQL 16
- **Web Server**: Nginx (reverse proxy)
- **App Server**: Gunicorn (4 workers)
- **Frontend**: HTML/CSS/JavaScript (templates)
- **Deployment**: Systemd services with auto-sync from GitHub

### Project Structure

```
goodtip/
├── goodtip/              # Django project settings
│   ├── settings.py       # Configuration (DB, apps, middleware)
│   ├── urls.py           # Main URL routing
│   ├── wsgi.py           # WSGI application
│   └── middleware.py     # Custom middleware
├── accounts/             # User authentication & profiles
├── orgs/                 # Organizations/Leagues management
├── tipping/              # Tipping/predictions logic
├── billing/              # Payment & donation handling
├── catalog/              # Sports data (teams, competitions)
├── data_sync/            # External data synchronization
├── admin_panel/          # Admin dashboard
├── templates/            # HTML templates
├── static/               # CSS, JS, images
├── manage.py             # Django CLI
└── requirements.txt      # Python dependencies
```

---

## Django Apps Explained

### 1. **accounts** - User Management
- **Purpose**: User registration, login, profiles
- **Key Models**: `User` (custom user model using email as login)
- **Features**:
  - Email-based authentication (no username)
  - User profiles with display names
  - Password reset functionality
- **Files**: `models.py`, `views.py`, `forms.py`, `backends.py`

### 2. **orgs** - Leagues/Organizations
- **Purpose**: Create and manage sports leagues/groups
- **Key Models**: 
  - `Organisation` - A league/group
  - `OrgMember` - Users in a league
  - `CharityVote` - Voting on which charity to support
- **Features**:
  - Create leagues with team size limits
  - Invite members via signed links
  - Charity selection voting
  - Member roles (owner, admin, member)
- **Files**: `models.py`, `views.py`, `forms.py`, `services.py`

### 3. **tipping** - Predictions/Tipping
- **Purpose**: Core tipping game logic
- **Key Models**:
  - `Round` - A round of matches
  - `Match` - Individual game
  - `Tip` - User's prediction
  - `Score` - Scoring/leaderboard
- **Features**:
  - Create tipping rounds
  - Submit predictions
  - Calculate scores
  - Leaderboards
- **Files**: `models.py`, `views.py`, `services.py`

### 4. **billing** - Payments & Donations
- **Purpose**: Handle payments and charity donations
- **Key Models**:
  - `DonationPledge` - User's pledge amount
  - `DonationPayment` - Actual payment
  - `CharityDisbursement` - Funds sent to charity
- **Features**:
  - Stripe integration for payments
  - Donation tracking
  - Charity fund distribution
- **Files**: `models.py`, `views.py`, `services.py`, `donations.py`

### 5. **catalog** - Sports Data
- **Purpose**: Reference data for sports
- **Key Models**:
  - `Competition` - AFL, NRL, etc.
  - `Series` - Season/year
  - `Team` - Sports teams
  - `Charity` - Supported charities
- **Features**:
  - Team data
  - Competition definitions
  - Charity listings
- **Files**: `models.py`, `admin.py`

### 6. **data_sync** - External Data Integration
- **Purpose**: Sync sports data from external APIs
- **Features**:
  - Pull match data from TheSports API
  - Update match results
  - Sync team information
- **Files**: `services.py`

### 7. **admin_panel** - Admin Dashboard
- **Purpose**: Admin-only management interface
- **Features**:
  - Manage organizations
  - View member lists
  - Manage rounds and matches
  - Data sync controls
- **Files**: `views.py`, `urls.py`

---

## URL Routing Map

```
/                           → Landing page (public)
/how-it-works/              → How it works page (public)
/wall/                      → Wall of supporters (public)
/leaderboard/               → Global leaderboard (public)
/pricing/                   → Pricing page (public)

/dashboard/                 → User dashboard (login required)
/profile/                   → User profile management
/password-reset/            → Password reset flow

/leagues/                   → League management (orgs app)
/join/<org_id>/<token>/     → Join league via invite link

/billing/                   → Billing & donations (billing app)
/stripe/webhook/            → Stripe webhook endpoint

/org/                       → Tipping interface (tipping app)

/manage/                    → Admin panel (admin_panel app)

/admin/                     → Django admin interface
```

---

## Database Schema (Key Tables)

### Users
- `accounts_user` - User accounts (email-based login)

### Organizations
- `orgs_organisation` - Leagues/groups
- `orgs_orgmember` - League membership
- `orgs_charityvote` - Charity voting

### Tipping
- `tipping_round` - Tipping rounds
- `tipping_match` - Individual matches
- `tipping_tip` - User predictions
- `tipping_score` - Leaderboard scores

### Billing
- `billing_donationpledge` - Pledged amounts
- `billing_donationpayment` - Actual payments
- `billing_charitydisbursement` - Charity payouts

### Catalog
- `catalog_competition` - AFL, NRL, etc.
- `catalog_team` - Sports teams
- `catalog_charity` - Supported charities

---

## User Flows

### 1. New User Registration
1. User visits landing page
2. Clicks "Sign Up"
3. Enters email, password, display name
4. Account created
5. Redirected to dashboard

### 2. Create a League
1. User clicks "Create League"
2. Fills in league name, team size, charity preference
3. League created
4. User becomes owner
5. Can invite members via signed link

### 3. Join a League
1. User receives invite link
2. Clicks link (validates signature)
3. Joins league as member
4. Can now participate in tipping

### 4. Make a Tip
1. User views current round
2. Sees upcoming matches
3. Selects predicted winner for each match
4. Submits tips
5. Tips locked when match starts

### 5. Make a Payment
1. User pledges donation amount
2. Clicks "Pay Now"
3. Redirected to Stripe checkout
4. Completes payment
5. Funds tracked for charity disbursement

---

## Key Features

### Authentication
- Email-based login (no username)
- Custom user model
- Password reset via email
- Session-based authentication

### League Management
- Create private leagues
- Invite members via signed links
- Role-based access (owner, admin, member)
- Charity voting system

### Tipping Game
- Create tipping rounds
- Submit predictions
- Auto-calculate scores
- Leaderboards (league & global)

### Payments
- Stripe integration
- Donation pledges
- Payment tracking
- Charity disbursement

### Admin Features
- Manage organizations
- View member lists
- Create/manage rounds
- Sync external sports data
- Manual data management

---

## Configuration Files

### `.env` - Environment Variables
```
SECRET_KEY              # Django secret key
DEBUG                   # Debug mode (False in production)
ALLOWED_HOSTS           # Allowed domain names
DATABASE_URL            # PostgreSQL connection string
THESPORTS_API_KEY       # Sports data API key
STRIPE_SECRET_KEY       # Stripe API key
EMAIL_HOST_USER         # SMTP email address
EMAIL_HOST_PASSWORD     # SMTP password
```

### `settings.py` - Django Configuration
- Installed apps
- Middleware
- Database settings
- Static files configuration
- Email settings
- Authentication backends

---

## Deployment Setup

### Services
- **goodtipservice** - Gunicorn app server (systemd)
- **goodtip-sync.timer** - Auto-sync from GitHub (every 5 minutes)
- **nginx** - Web server (systemd)

### Directories
- **Project**: `/home/mbatha-goodtip/projects/goodtip`
- **Venv**: `/home/mbatha-goodtip/projects/goodtip/venv`
- **Static files**: `/home/mbatha-goodtip/projects/goodtip/staticfiles`
- **Database**: PostgreSQL on localhost

### SSL/TLS
- Let's Encrypt certificate via Certbot
- Auto-renewal enabled
- HTTPS enforced (HTTP redirects to HTTPS)

---

## Common Tasks

### Add a New Feature
1. Create models in app's `models.py`
2. Create migration: `python manage.py makemigrations`
3. Apply migration: `python manage.py migrate`
4. Create views in `views.py`
5. Add URLs in `urls.py`
6. Create templates in `templates/`
7. Test locally
8. Push to GitHub
9. Auto-sync pulls changes (every 5 minutes)

### Create a Superuser
```bash
cd ~/projects/goodtip
source venv/bin/activate
python manage.py createsuperuser
```

### Run Migrations
```bash
cd ~/projects/goodtip
source venv/bin/activate
python manage.py migrate
```

### Access Django Shell
```bash
cd ~/projects/goodtip
source venv/bin/activate
python manage.py shell
```

### View Logs
```bash
sudo journalctl -u goodtipservice -f
sudo journalctl -u goodtip-sync.service -f
sudo tail -f /var/log/nginx/error.log
```

---

## API Endpoints (Key Views)

### Public
- `GET /` - Landing page
- `GET /how-it-works/` - Info page
- `GET /leaderboard/` - Global leaderboard

### Authentication
- `POST /accounts/signup/` - Register
- `POST /accounts/login/` - Login
- `GET /accounts/logout/` - Logout
- `POST /password-reset/` - Reset password

### Leagues
- `GET /leagues/` - List user's leagues
- `POST /leagues/create/` - Create league
- `GET /leagues/<id>/` - View league
- `POST /leagues/<id>/invite/` - Invite member
- `GET /join/<org_id>/<token>/` - Join via link

### Tipping
- `GET /org/` - View current round
- `POST /org/tip/` - Submit tip
- `GET /org/leaderboard/` - League leaderboard

### Billing
- `GET /billing/plans/` - View plans
- `POST /billing/pledge/` - Create pledge
- `GET /billing/checkout/` - Stripe checkout

### Admin
- `GET /manage/` - Admin dashboard
- `GET /manage/orgs/` - Manage organizations
- `GET /manage/sync/` - Data sync controls

---

## Next Steps for Development

1. **Add Features**: Modify models, views, templates
2. **Test Locally**: Run `python manage.py runserver`
3. **Push to GitHub**: Commit and push changes
4. **Auto-Deploy**: Changes sync every 5 minutes
5. **Monitor**: Check logs for errors

---

## Support & Troubleshooting

### Service Issues
```bash
sudo systemctl status goodtipservice
sudo systemctl restart goodtipservice
```

### Database Issues
```bash
psql -U goodtip_user -d goodtip_db -h localhost
```

### Static Files Issues
```bash
cd ~/projects/goodtip
source venv/bin/activate
python manage.py collectstatic --noinput
```

### Sync Issues
```bash
~/projects/goodtip/deploy.sh
sudo journalctl -u goodtip-sync.service -n 50
```
