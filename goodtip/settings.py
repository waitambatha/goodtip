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
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    # Traffic-driven fallback, enabled only by opting in — see AUTOSYNC_ENABLED.
    "data_sync.autosync.AutoSyncMiddleware",
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
                "orgs.context_processors.contact_form",
                "goodtip.context_processors.analytics",
            ],
        },
    },
]

# --- Google Analytics 4 -----------------------------------------------------
# Empty means the tag does not render at all. Defaulted OFF in development so
# that building the site does not fill the property with your own page views;
# put GA_MEASUREMENT_ID in .env to force it on anywhere.
GA_MEASUREMENT_ID = os.environ.get(
    "GA_MEASUREMENT_ID",
    "" if DEBUG else "G-ESB1RHRW49",
)

WSGI_APPLICATION = "goodtip.wsgi.application"

DATABASES = {
    "default": dj_database_url.parse(
        os.environ["DATABASE_URL"],
        conn_max_age=600,
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
    if os.environ.get("EMAIL_SEND_FOR_REAL", "False").lower() == "true" and POSTMARK_SERVER_TOKEN:
        EMAIL_BACKEND = "goodtip.email_backends.PostmarkEmailBackend"
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
    EMAIL_BACKEND = (
        "goodtip.email_backends.PostmarkEmailBackend" if POSTMARK_SERVER_TOKEN
        else "django.core.mail.backends.smtp.EmailBackend"
    )
    # Production site domain. Extra hosts can still be added via ALLOWED_HOSTS env.
    ALLOWED_HOSTS += ["goodtip.com.au", "www.goodtip.com.au"]
    CSRF_TRUSTED_ORIGINS = [
        "https://goodtip.com.au",
        "https://www.goodtip.com.au",
    ]

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
# Absolute base URL used in outbound email links.
SITE_BASE_URL = os.environ.get("SITE_BASE_URL", "https://goodtip.com.au")

# AI Group Recap (docs/ai-group-recap-spec.md). Generation is skipped —
# silently, with a log line — until the key is set, mirroring the SMTP setup.
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
RECAP_MODEL = os.environ.get("RECAP_MODEL", "claude-opus-4-8")

# NRL fixtures, scores and results come from API-SPORTS (v1.rugby.api-sports.io).
# THESPORTS_API_KEY is the old name, kept as a fallback so an existing .env keeps
# working — the host it was written for (api.thesportsapi.com) has no DNS record
# and never did, so nothing can be depending on the old client.
APISPORTS_KEY = os.environ.get("APISPORTS_KEY") or os.environ.get("THESPORTS_API_KEY", "")
THESPORTS_API_KEY = APISPORTS_KEY  # legacy alias
# The NRL's league id within API-SPORTS' rugby catalogue. Discovered from
# /leagues on first use when left unset, then worth pinning here to save a
# request per sync.
APISPORTS_NRL_LEAGUE_ID = os.environ.get("APISPORTS_NRL_LEAGUE_ID", "")

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
