"""Putting an admin's SEO settings into the HTML on its way out.

WHY A REWRITER AND NOT TEMPLATE BLOCKS
--------------------------------------
Every one of these pages already has a `<title>` and a description in its
template, and roughly half of them are `TemplateView`s with no view code at all
(see `goodtip/urls.py`). Threading a context processor through them would mean
touching every template and every view to read a row that is almost always
absent — and would still miss the pages whose response is assembled somewhere
else.

`PageEditMiddleware` is already parsing the finished HTML of exactly these
pages to apply wording edits. The head is right there. So the same pass that
rewrites a paragraph rewrites the title, and a page with no SEO row is not
touched at all — the cost is one indexed query per registered page, and zero
for everything else.

WHAT "OVERRIDE" MEANS HERE
--------------------------
Only what has been filled in is replaced. An empty `meta_title` leaves the
template's own `<title>` exactly as it was, rather than blanking it — which is
what makes it safe to save a row for a page and fill in one box.
"""
import re

from django.utils.html import escape


# Deliberately narrow. These match the tags the site's own base templates emit
# (`templates/public/base.html`, `templates/app_base.html`) — one line each,
# attributes in a known order — and anything that does not match is left alone
# and the replacement is appended instead. A greedy or clever pattern here
# would be rewriting arbitrary markup on the landing page.
_TITLE_RE = re.compile(r"<title>.*?</title>", re.IGNORECASE | re.DOTALL)
_HEAD_CLOSE_RE = re.compile(r"</head>", re.IGNORECASE)


def _meta_re(kind: str, name: str) -> re.Pattern:
    return re.compile(
        r'<meta\s+[^>]*?{kind}=["\']{name}["\'][^>]*>'.format(
            kind=kind, name=re.escape(name)
        ),
        re.IGNORECASE,
    )


_DESC_RE = _meta_re("name", "description")
_ROBOTS_RE = _meta_re("name", "robots")
_CANONICAL_RE = re.compile(
    r'<link\s+[^>]*?rel=["\']canonical["\'][^>]*>', re.IGNORECASE
)


def _replace_or_append(html: str, pattern: re.Pattern, tag: str) -> str:
    """Swap the first match for `tag`, or add `tag` just before </head>.

    Appending is the fallback rather than the error case: a page that never had
    a canonical link still needs to be able to get one, and a base template that
    is rewritten tomorrow must not silently stop honouring the admin's setting.
    """
    if pattern.search(html):
        # A lambda, not the string: `re.sub` reads backslashes and \\g in a
        # replacement string, and these tags carry admin-typed text.
        return pattern.sub(lambda _m: tag, html, count=1)
    return _HEAD_CLOSE_RE.sub(lambda _m: tag + "\n</head>", html, count=1)


def _og_re(prop: str) -> re.Pattern:
    return _meta_re("property", prop)


def head_tags(seo, *, absolute_image="") -> list:
    """The (pattern, tag) pairs one SEO record wants applied.

    Shared by the page middleware and by anything else that needs to render the
    same seven settings — the shape is the whole point of `SeoFieldsMixin`.
    """
    out = []
    title = (seo.meta_title or "").strip()
    if title:
        out.append((_TITLE_RE, f"<title>{escape(title)}</title>"))

    desc = (seo.meta_description or "").strip()
    if desc:
        out.append((
            _DESC_RE, f'<meta name="description" content="{escape(desc)}">',
        ))

    # ALWAYS EMITTED, unlike the rest.
    #
    # The others are overrides on something the page already says; this one is
    # a statement the page does not otherwise make, and its default — index,
    # follow — is what a crawler assumes anyway. Emitting it always means the
    # tell-the-boss page in the brief can be set to noindex and the tag is
    # actually there, and it means the setting is visible in view-source rather
    # than having to be inferred from its absence.
    out.append((
        _ROBOTS_RE, f'<meta name="robots" content="{escape(seo.robots_directive)}">',
    ))

    canonical = (seo.canonical_url or "").strip()
    if canonical:
        out.append((
            _CANONICAL_RE, f'<link rel="canonical" href="{escape(canonical)}">',
        ))

    og_title = (seo.og_title or seo.meta_title or "").strip()
    if og_title:
        out.append((_og_re("og:title"), f'<meta property="og:title" content="{escape(og_title)}">'))
    og_desc = (seo.og_description or seo.meta_description or "").strip()
    if og_desc:
        out.append((
            _og_re("og:description"),
            f'<meta property="og:description" content="{escape(og_desc)}">',
        ))
    if absolute_image:
        out.append((
            _og_re("og:image"),
            f'<meta property="og:image" content="{escape(absolute_image)}">',
        ))
    return out


def apply_to_html(html: str, seo, *, request=None) -> str:
    """Rewrite one page's head to match its SEO record."""
    image_url = ""
    if seo.og_image:
        try:
            image_url = seo.og_image.url
        except ValueError:
            image_url = ""
        if image_url and request is not None:
            image_url = request.build_absolute_uri(image_url)
    for pattern, tag in head_tags(seo, absolute_image=image_url):
        html = _replace_or_append(html, pattern, tag)
    return html


# ---------------------------------------------------------------------------
# Serving a page at an address an admin chose
# ---------------------------------------------------------------------------

def serve_override(request):
    """The page whose `path_override` is this address, rendered — or None.

    WHY THIS IS NOT A URL PATTERN.
    ------------------------------
    The obvious shape is a `<path:...>` catch-all mounted last. It works, and
    it quietly breaks the whole site: APPEND_SLASH only redirects an address
    that does not resolve, and a catch-all resolves everything. So
    "goodtip.com.au/pricing" — no trailing slash, which is how half the links
    and every typed address in the world are written — stops being redirected to
    "/pricing/" and starts 404ing. Requiring a trailing slash in the pattern
    fixes that one and introduces another: every genuine 404 on a slashless
    path becomes a 301 to the slashed version and THEN a 404.

    Called from the 404 fallback instead, nothing new resolves. Routing is
    untouched, APPEND_SLASH behaves exactly as it always did, a real page
    always wins over an override, and an address nobody has claimed still 404s
    on the first response rather than the second.

    Returns a rendered response, or None if this address is not an override.
    """
    from django.urls import resolve, reverse

    from . import pages as page_registry
    from .models import PageSeo

    path = request.path
    wanted = "/" + path.strip("/") + "/"
    row = PageSeo.objects.filter(path_override__in=[wanted, wanted.rstrip("/")]).first()
    if row is None:
        return None
    page = page_registry.BY_KEY.get(row.page)
    # `needs` means the page takes an organisation id, so it has no single
    # address to move to and an override on it is meaningless.
    if page is None or page.needs:
        return None

    try:
        match = resolve(reverse(page.view_name))
    except Exception:  # noqa: BLE001 — a page that will not resolve is not served
        return None

    # The wording editor and the SEO rewriter both key off
    # `resolver_match.view_name`, and this request resolved to nothing. Pointing
    # it at the page's own match is what keeps a renamed page editable — without
    # it, moving a page silently drops every edit ever made to it.
    request.resolver_match = match
    response = match.func(request, *match.args, **match.kwargs)
    # A TemplateResponse is normally rendered by the handler, which is not in
    # this path. Unrendered, its content would raise on the way out.
    if hasattr(response, "render") and callable(response.render):
        response = response.render()
    return response
