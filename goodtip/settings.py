from pathlib import Path
import os
import sys
import dj_database_url
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

SECRET_KEY = os.environ["SECRET_KEY"]
DEBUG = os.environ.get("DEBUG", "False").lower() == "true"
ALLOWED_HOSTS = [h.strip() for h in os.environ.get("ALLOWED_HOSTS", "").split(",") if h.strip()]

# Which deployment this process is. Two non-DEBUG environments now run the same
# code off the same box — goodtip.com.au from `main`, staging.goodtip.com.au
# from `staging` — and a handful of settings below have to tell them apart.
# It is deliberately not derived from the hostname or the checkout path: those
# are things that get moved, and a staging box that silently decides it is
# production is exactly the failure this whole arrangement exists to prevent.
GOODTIP_ENV = os.environ.get("GOODTIP_ENV", "production").strip().lower()
IS_STAGING = GOODTIP_ENV == "staging"

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    "accounts",
    "catalog",
    "orgs",
    "tipping",
    "data_sync",
    "matchreader",
    "admin_panel",
    "billing",
    "sysadmin",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "goodtip.staging_gate.StagingGateMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "goodtip.middleware.ForceCsrfCookieMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # After AuthenticationMiddleware (it reads request.user) and before the
    # view runs. The Django admin is the control plane; a password alone is
    # the same single factor a member's tipping account has, so it asks for an
    # emailed code too. See sysadmin.otp.
    "sysadmin.middleware.AdminOTPMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    # Traffic-driven fallback, enabled only by opting in — see AUTOSYNC_ENABLED.
    "data_sync.autosync.AutoSyncMiddleware",
    # Last, so it sees the finished HTML. Passes everything straight through
    # except the handful of pages named in admin_panel.pages.
    "admin_panel.middleware.PageEditMiddleware",
]

# Syncing runs on the server's clock via goodtip-matchsync.timer, NOT on site
# traffic: data has to be current when a visitor arrives, not fetched because
# one did. This flag turns on a traffic-driven fallback and exists only for an
# environment with no timer (a bare container, a staging box). Off by default —
# on, it would sync only while someone happened to be browsing, and its work
# would die with the web worker on every restart.
AUTOSYNC_ENABLED = os.environ.get("AUTOSYNC_ENABLED", "False").lower() == "true"

ROOT_URLCONF = "goodtip.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "orgs.context_processors.user_orgs",
                "accounts.context_processors.onboarding",
                "orgs.context_processors.contact_form",
                "goodtip.context_processors.analytics",
                "goodtip.context_processors.environment",
            ],
        },
    },
]

# --- Google Analytics 4 -----------------------------------------------------
# Empty means the tag does not render at all. Defaulted OFF in development so
# that building the site does not fill the property with your own page views;
# put GA_MEASUREMENT_ID in .env to force it on anywhere. Off on staging for the
# same reason it is off in development, and a stronger one: a client clicking
# through a demo is not site traffic, and those sessions landing in the real
# property would quietly corrupt the only numbers anyone reports on.
GA_MEASUREMENT_ID = os.environ.get(
    "GA_MEASUREMENT_ID",
    "" if (DEBUG or IS_STAGING) else "G-ESB1RHRW49",
)

WSGI_APPLICATION = "goodtip.wsgi.application"

DATABASES = {
    "default": dj_database_url.parse(
        os.environ["DATABASE_URL"],
        conn_max_age=600,
        # Validate a reused connection before handing it out, and reconnect if
        # it has died. Without this, a persistent connection killed at the far
        # end — a database restart, an idle timeout, a firewall reaping a quiet
        # TCP session — is handed to the next request anyway and fails with
        # "the connection is closed". conn_max_age=600 makes that MORE likely,
        # not less: the longer a connection is kept, the more chance it is
        # dead by the time it is next used.
        conn_health_checks=True,
    )
}

AUTH_USER_MODEL = "accounts.User"

AUTHENTICATION_BACKENDS = [
    "accounts.backends.EmailBackend",
]

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
    {"NAME": "accounts.validators.PasswordComplexityValidator"},
]

LANGUAGE_CODE = "en-au"
TIME_ZONE = "Australia/Sydney"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
# Django 5.1+ only reads STORAGES (the old STATICFILES_STORAGE is ignored).
# Manifest storage gives content-hashed filenames, so nginx's long-lived
# "immutable" caching of /static/ can never serve a stale file after a deploy.
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"
    },
}

# User uploads (profile photos). Served by Django via the /media/ route in
# goodtip/urls.py — fine at this scale, swap for nginx/S3 if uploads grow.
MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGIN_URL = "accounts:login"
LOGIN_REDIRECT_URL = "dashboard"
LOGOUT_REDIRECT_URL = "landing"

# --- Sports data -------------------------------------------------------------
# There are no keys here on purpose. Fixtures, live scores and results come
# from nrl.com and afl.com.au directly (data_sync/scrapers/), and the ladder is
# derived from those results rather than fetched. Sportradar's trial key,
# Squiggle and API-SPORTS were all removed on 14 Aug 2026 — see
# data_sync/services.get_sync_service for why each one went.

# Postmark is the transactional email provider. The token lives only in .env.
POSTMARK_SERVER_TOKEN = os.environ.get("POSTMARK_SERVER_TOKEN", "")
POSTMARK_MESSAGE_STREAM = os.environ.get("POSTMARK_MESSAGE_STREAM", "outbound")

# Print one-time sign-in codes to the terminal. This was the stand-in while
# Postmark approval was pending; now that mail really sends it defaults off, so
# a code lives in an inbox rather than in a terminal anyone can scroll back
# through. Turn it on in .env when working offline. Only ever consulted when
# DEBUG is on (see accounts.notifications), so a live server can't leak one.
SHOW_OTP_IN_CONSOLE = os.environ.get("SHOW_OTP_IN_CONSOLE", "False").lower() == "true"

if DEBUG:
    # Postmark when EMAIL_SEND_FOR_REAL says so, console otherwise. Both paths
    # are kept: the console one is what to fall back to offline, or when you
    # don't want dev traffic counted against the Postmark quota.
    #
    # SMTP sits between them, and exists because Postmark's free plan is 100
    # messages a calendar month with no overage — past that it still answers
    # OK with a MessageID and silently drops the mail, which is impossible to
    # tell from success inside the app. Dev had no way around that: the only
    # two choices here were the exhausted account or a console backend that
    # never reaches an inbox. Clear POSTMARK_SERVER_TOKEN and set EMAIL_HOST
    # (any SMTP provider, or Gmail with an app password) to actually receive
    # mail while the quota is gone. Mirrors what the non-DEBUG branch below
    # has always done.
    if os.environ.get("EMAIL_SEND_FOR_REAL", "False").lower() == "true":
        if POSTMARK_SERVER_TOKEN:
            EMAIL_BACKEND = "goodtip.email_backends.PostmarkEmailBackend"
        elif os.environ.get("EMAIL_HOST", ""):
            EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
        else:
            EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
    else:
        EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
    # Browsers treat localhost and 127.0.0.1 as different sites with separate
    # cookie stores. Trust both so invite links work regardless of which the
    # admin used when generating the link.
    ALLOWED_HOSTS += ["localhost", "127.0.0.1"]
    CSRF_TRUSTED_ORIGINS = [
        "http://localhost:8000",
        "http://127.0.0.1:8000",
    ]
else:
    # Postmark when there's a token, SMTP as the fallback so an environment
    # without one behaves exactly as it did before.
    _delivery_backend = (
        "goodtip.email_backends.PostmarkEmailBackend" if POSTMARK_SERVER_TOKEN
        else "django.core.mail.backends.smtp.EmailBackend"
    )
    if IS_STAGING:
        # Never a bare delivery backend on staging. See AllowlistEmailBackend:
        # staging runs on scrubbed production data, so "mail every member of
        # this org" is a live code path over thousands of real-shaped rows.
        # Empty allowlist == nothing goes out, which is the correct behaviour
        # for a forgotten env var.
        EMAIL_ALLOWLIST_DELEGATE = _delivery_backend
        EMAIL_BACKEND = "goodtip.email_backends.AllowlistEmailBackend"
    else:
        EMAIL_BACKEND = _delivery_backend
    # Site domain for this environment. Extra hosts can still be added via the
    # ALLOWED_HOSTS env var; these are the ones that are always right so a
    # missing env var cannot take the site off the air.
    if IS_STAGING:
        ALLOWED_HOSTS += ["staging.goodtip.com.au"]
        CSRF_TRUSTED_ORIGINS = ["https://staging.goodtip.com.au"]
    else:
        ALLOWED_HOSTS += ["goodtip.com.au", "www.goodtip.com.au"]
        CSRF_TRUSTED_ORIGINS = [
            "https://goodtip.com.au",
            "https://www.goodtip.com.au",
        ]

if IS_STAGING:
    # Staging sits behind the nginx vhost in deploy/nginx/, which sets
    # X-Forwarded-Proto on every proxied request unconditionally -- so trusting
    # it here is safe, and without it request.is_secure() is False behind the
    # TLS terminator and the gate cookie never gets its Secure flag.
    #
    # Scoped to staging on purpose. Production's vhost is managed by hand on
    # the box and is not in this repo, so whether it sets that header on every
    # path is not something this file can verify -- and trusting a header the
    # proxy might not always overwrite is worth more care than a config change
    # made in passing. Production behaves exactly as it did before.
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# CSRF origins had no env escape hatch while production was the only deployment
# and the list could be a literal. It needs one now: an origin missing from this
# list fails every POST on the site with a 403 referer check, and that is not a
# thing to discover by shipping a code change to add a domain.
CSRF_TRUSTED_ORIGINS += [
    o.strip() for o in os.environ.get("CSRF_TRUSTED_ORIGINS", "").split(",")
    if o.strip() and o.strip() not in CSRF_TRUSTED_ORIGINS
]

# Comma-separated addresses ("me@x.com") or whole domains ("@client.com") that
# staging is allowed to mail. Consulted only by AllowlistEmailBackend.
EMAIL_ALLOWLIST = os.environ.get("EMAIL_ALLOWLIST", "")

EMAIL_HOST = os.environ.get("EMAIL_HOST", "")
EMAIL_PORT = int(os.environ.get("EMAIL_PORT", "587"))
EMAIL_HOST_USER = os.environ.get("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = os.environ.get("EMAIL_USE_TLS", "True").lower() == "true"
# Never let a slow/unconfigured SMTP host hang a request.
EMAIL_TIMEOUT = int(os.environ.get("EMAIL_TIMEOUT", "10"))
DEFAULT_FROM_EMAIL = os.environ.get("DEFAULT_FROM_EMAIL", "GoodTip <no-reply@goodtip.com.au>")
# Where "suggest a charity" notifications go for manual review (deck slide 10).
GOODTIP_TEAM_EMAIL = os.environ.get("GOODTIP_TEAM_EMAIL", "team@goodtip.com.au")
# Absolute base URL used in outbound email links. Defaults per environment so a
# staging invite cannot hand someone a link into production, which would look
# like it worked right up until they acted on live data.
SITE_BASE_URL = os.environ.get(
    "SITE_BASE_URL",
    "https://staging.goodtip.com.au" if IS_STAGING else "https://goodtip.com.au",
)

# Group Recap (docs/ai-group-recap-spec.md) needs no configuration: the
# writer lives in orgs/recaps.py and runs off the database. Nothing to key,
# nothing to bill, nothing to be down.

# NRL fixtures, scores and results come from nrl.com's own draw page — see
# data_sync/scrapers/nrl.py. There is no key and no API-SPORTS settings block
# any more: that client needed a paid key that was never issued, so it never
# ran once, and it was removed on 14 Aug 2026 along with Sportradar and
# Squiggle.

# Stripe (Phase 1: single-destination platform-fee charges via Checkout).
# Left blank until test/live keys are supplied — billing stays dormant if unset.
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_PUBLISHABLE_KEY = os.environ.get("STRIPE_PUBLISHABLE_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")

JOIN_LINK_MAX_AGE_DAYS = 7

# Pre-launch staging gate (branded replacement for nginx auth_basic).
# STAGING_GATE=true locks the whole site behind /gate/; credentials are
# "name:password,name:password" pairs (one for the team, one for the client).
# Unset or set to false at launch to open the site.
STAGING_GATE = os.environ.get("STAGING_GATE", "False").lower() == "true"
STAGING_GATE_USERS = os.environ.get("STAGING_GATE_USERS", "")
if "test" in sys.argv:
    # Never let a developer's .env lock the test client out of every view;
    # gate tests enable the gate explicitly via override_settings.
    STAGING_GATE = False
    # Manifest storage requires a collectstatic-built manifest, which the test
    # environment doesn't have; hashed URLs aren't what tests assert on anyway.
    STORAGES["staticfiles"] = {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"
    }

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        "django": {"handlers": ["console"], "level": "INFO", "propagate": False},
        "data_sync": {"handlers": ["console"], "level": "INFO", "propagate": False},
    },
}


# Charity logo fetching reaches out to a third-party web server on a daemon
# thread (see catalog.logos.backfill_in_background). That is right in
# production and wrong under `manage.py test`: the thread outlives the test's
# transaction, so it wakes to find the charity row rolled away and logs a
# DatabaseError traceback into every run — and each one is a real network call
# to a stranger's site from CI. Off during tests, on everywhere else.
CHARITY_LOGO_FETCH = "test" not in sys.argv
