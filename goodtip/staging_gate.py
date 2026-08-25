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
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.cache import never_cache

GATE_PATH = "/gate/"
COOKIE_NAME = "gt_gate"
COOKIE_MAX_AGE = 14 * 24 * 3600  # re-prompt after a fortnight
SIGNING_SALT = "goodtip.staging_gate"

ROBOTS_PATH = "/robots.txt"

# Paths that must work without the cookie: the gate itself, static assets,
# the Stripe webhook (Stripe's servers can't answer a login page), and
# robots.txt.
#
# robots.txt is exempt because a crawler that gets redirected to the gate never
# reads the Disallow. In practice the redirect target carries `noindex` and the
# staging vhost sets X-Robots-Tag on every response, so staging stays out of
# the index either way -- but "our robots.txt says Disallow" is the check
# anyone auditing this will actually run, and it should give a straight answer
# rather than a 302 to a login page.
EXEMPT_PREFIXES = (GATE_PATH, settings.STATIC_URL, "/stripe/webhook/", ROBOTS_PATH)


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


@never_cache
def robots_view(request):
    """``Disallow: /`` for the whole staging site.

    Registered in urls.py only when IS_STAGING, so production is untouched and
    keeps whatever its own nginx serves. Belt and braces alongside the
    X-Robots-Tag header in deploy/nginx/: the header is what actually binds a
    crawler that has already fetched a page, this is what stops it fetching.
    """
    return HttpResponse(
        "# staging.goodtip.com.au is a pre-release copy of goodtip.com.au.\n"
        "# Nothing here should be indexed. The live site is https://goodtip.com.au\n"
        "User-agent: *\n"
        "Disallow: /\n",
        content_type="text/plain",
    )
