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

# HOLDING MODE (HOLDING_PAGE=true, only ever alongside STAGING_GATE).
#
# The gate hides the whole product; the client wants the public to be able to
# see a trailer of it at events while it is finished (24 Sep 2026). So a visitor
# WITHOUT gate access gets: "/" answered by the trailer page, and the trailer
# page itself with its waiting-list form. Everything else still goes to the
# gate. A visitor WITH access sees the real site as before.
#
# The trailer's menu and footer (How It Works, Blog, The Wall, About Us, Terms,
# Privacy) do not lead to those pages. Each opens a showcase on the trailer
# page: a filmed tour and stills of the page, so the visitor sees what it is
# but cannot click around inside it (client, 25 Sep 2026). A visitor who
# arrives at one of the real addresses (a shared link, an old bookmark, the
# back button) is sent to its showcase rather than to the password box.
#
# Deliberately nothing else is opened: no /media/, no login, signup or
# dashboard. Message attachments and change-request uploads live under /media/.
HOLDING_HOME_PATH = "/coming-soon/"
HOLDING_SHOWCASES = (
    ("/how-it-works/", "how-it-works"),
    ("/news/", "blog"),
    ("/wall/", "wall"),
    ("/about/", "about"),
    ("/terms/", "terms"),
    ("/privacy/", "privacy"),
)

# LOCKDOWN (HOLDING_LOCKDOWN=true): the trailer and nothing else, for everyone.
#
# Holding mode still lets anyone with the gate password through to the real
# site. The client wants goodtip.com.au to show the trailer only until launch
# (25 Sep 2026): a visitor who watches the trailer's tours and then types the
# addresses they saw (/news, /pricing) must not reach a page, and must not be
# shown a password box either. So under lockdown the gate cookie opens nothing,
# /gate/ itself is closed, and every path outside /coming-soon/ is sent to the
# trailer. The team works on staging, which has no lockdown.
#
# Static files stay open (the trailer's videos and styles are there), as do
# robots.txt and the Stripe webhook, which is server-to-server, not a page.
LOCKDOWN_EXEMPT_PREFIXES = (settings.STATIC_URL, "/stripe/webhook/", ROBOTS_PATH)


def _locked_down():
    return getattr(settings, "HOLDING_LOCKDOWN", False)


def is_holding_visitor(request):
    """True for someone the trailer is standing in front of the site for."""
    return _locked_down() or (
        getattr(settings, "STAGING_GATE", False)
        and getattr(settings, "HOLDING_PAGE", False)
        and not has_gate_access(request)
    )


def _showcase_for(path):
    """The showcase that stands in for `path`, or None."""
    for prefix, slug in HOLDING_SHOWCASES:
        if path.startswith(prefix):
            return slug
    return None


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
        if _locked_down():
            if request.path.startswith(LOCKDOWN_EXEMPT_PREFIXES):
                return self.get_response(request)
            return self._holding(request) or redirect(HOLDING_HOME_PATH)
        if (
            getattr(settings, "STAGING_GATE", False)
            and not request.path.startswith(EXEMPT_PREFIXES)
            and not has_gate_access(request)
        ):
            if getattr(settings, "HOLDING_PAGE", False):
                response = self._holding(request)
                if response is not None:
                    return response
            return redirect(f"{GATE_PATH}?next={request.path}")
        return self.get_response(request)

    def _holding(self, request):
        """The trailer's answer to `request`, or None when it has none."""
        if request.path == "/" and request.method in ("GET", "HEAD"):
            # Re-route, don't render: session, CSRF and the template
            # context all still run in their normal place after us.
            request.path = request.path_info = HOLDING_HOME_PATH
            return self.get_response(request)
        if request.path.startswith(HOLDING_HOME_PATH):
            return self.get_response(request)
        slug = _showcase_for(request.path)
        if slug and request.method in ("GET", "HEAD"):
            return redirect(f"{HOLDING_HOME_PATH}#{slug}")
        return None


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
