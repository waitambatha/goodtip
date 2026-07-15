"""Site-wide staging gate.

Replaces nginx ``auth_basic`` (the unstyleable browser popup) with a branded
lock page. While ``STAGING_GATE=true``, every request must carry a signed
gate cookie; without one the visitor is redirected to the gate login. Once
unlocked they use the site normally (log in, sign up, everything).

Credentials come from the ``STAGING_GATE_USERS`` env var as
``name:password,name:password`` — one pair for the dev team, one for the
client. Flip ``STAGING_GATE=false`` (or unset it) at launch to open the site;
no code change needed.
"""

import hmac

from django.conf import settings
from django.core import signing
from django.shortcuts import redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.cache import never_cache

GATE_PATH = "/gate/"
COOKIE_NAME = "gt_gate"
COOKIE_MAX_AGE = 14 * 24 * 3600  # re-prompt after a fortnight
SIGNING_SALT = "goodtip.staging_gate"

# Paths that must work without the cookie: the gate itself, static assets,
# and the Stripe webhook (Stripe's servers can't answer a login page).
EXEMPT_PREFIXES = (GATE_PATH, settings.STATIC_URL, "/stripe/webhook/")


def _credentials():
    """Parse STAGING_GATE_USERS ('name:pass,name:pass') into a dict."""
    creds = {}
    for pair in getattr(settings, "STAGING_GATE_USERS", "").split(","):
        name, sep, password = pair.strip().partition(":")
        if sep and name and password:
            creds[name] = password
    return creds


def has_gate_access(request):
    try:
        name = signing.loads(
            request.COOKIES.get(COOKIE_NAME, ""),
            salt=SIGNING_SALT,
            max_age=COOKIE_MAX_AGE,
        )
    except signing.BadSignature:
        return False
    return name in _credentials()


class StagingGateMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if (
            getattr(settings, "STAGING_GATE", False)
            and not request.path.startswith(EXEMPT_PREFIXES)
            and not has_gate_access(request)
        ):
            return redirect(f"{GATE_PATH}?next={request.path}")
        return self.get_response(request)


@never_cache
def gate_view(request):
    next_url = request.POST.get("next") or request.GET.get("next") or "/"
    if not url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
        next_url = "/"

    if not getattr(settings, "STAGING_GATE", False) or has_gate_access(request):
        return redirect(next_url)

    error = False
    if request.method == "POST":
        name = request.POST.get("username", "").strip()
        password = request.POST.get("password", "")
        expected = _credentials().get(name)
        if expected is not None and hmac.compare_digest(password, expected):
            response = redirect(next_url)
            response.set_cookie(
                COOKIE_NAME,
                signing.dumps(name, salt=SIGNING_SALT),
                max_age=COOKIE_MAX_AGE,
                httponly=True,
                secure=request.is_secure(),
                samesite="Lax",
            )
            return response
        error = True

    return render(request, "staging_gate.html", {"error": error, "next": next_url}, status=401)
