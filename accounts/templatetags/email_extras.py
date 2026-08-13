"""Template helpers that only email needs.

Lives here rather than in tipping's team_extras because none of it is about
teams, and email has one requirement no in-app template has: every URL must be
absolute. A mail client has no origin to resolve "/static/..." against.
"""
from django.conf import settings
from django.template import Library
from django.templatetags.static import static

register = Library()


@register.simple_tag
def email_image(path: str) -> str:
    """Absolute URL for an image referenced from an email.

    Defensive about the manifest on purpose. In production static files are
    served by ManifestStaticFilesStorage, whose ``static()`` RAISES for a file
    missing from the manifest — and goodtip.mail.build catches template errors
    by dropping the whole message. A missing decorative photo would therefore
    silently cancel somebody's sign-in code. Falling back to the unhashed path
    turns that into a broken image at worst.
    """
    try:
        url = static(path)
    except Exception:                           # noqa: BLE001 — see above
        url = f"{settings.STATIC_URL}{path.lstrip('/')}"
    if url.startswith(("http://", "https://", "//")):
        return url
    base = getattr(settings, "SITE_BASE_URL", "").rstrip("/")
    return f"{base}{url}"
