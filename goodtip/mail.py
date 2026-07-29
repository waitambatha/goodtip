"""One way to send a GoodTip email.

Before this, each sender rolled its own: some used ``send_mail`` with a plain
string, one built ``EmailMultiAlternatives`` by hand, and each had its own idea
of what "email isn't configured yet" meant. That made it easy to add a message
that silently lacked a text part, or that raised during a signup.

``send_template`` renders ``emails/<name>.html`` plus ``emails/<name>.txt``,
sends both parts, and never raises — a failed email must not fail the action
that triggered it.
"""
from __future__ import annotations

import logging

from django.conf import settings
from django.core.mail import EmailMultiAlternatives, get_connection
from django.template.loader import render_to_string

logger = logging.getLogger(__name__)


def email_configured() -> bool:
    """Whether outbound email can actually go anywhere right now.

    Postmark needs only a token. The SMTP fallback needs host + credentials.
    The console backend always "works" and is what local dev uses.
    """
    backend = settings.EMAIL_BACKEND
    if "console" in backend or "locmem" in backend or "dummy" in backend:
        return True
    if "Postmark" in backend:
        return bool(getattr(settings, "POSTMARK_SERVER_TOKEN", ""))
    if "smtp" in backend:
        return bool(settings.EMAIL_HOST and settings.EMAIL_HOST_USER and settings.EMAIL_HOST_PASSWORD)
    return True


def site_url(path: str = "") -> str:
    base = getattr(settings, "SITE_BASE_URL", "https://goodtip.com.au").rstrip("/")
    if not path:
        return base
    return f"{base}/{path.lstrip('/')}"


def build(name: str, *, subject: str, to, context: dict, reply_to=None) -> EmailMultiAlternatives | None:
    """Render one message from emails/<name>.html + emails/<name>.txt."""
    if isinstance(to, str):
        to = [to]
    to = [addr for addr in (to or []) if addr]
    if not to:
        return None

    ctx = {"site_url": site_url(), **context}
    try:
        text = render_to_string(f"emails/{name}.txt", ctx)
        html = render_to_string(f"emails/{name}.html", ctx)
    except Exception:  # noqa: BLE001 — a template typo shouldn't break a signup
        logger.exception("Could not render email template %r", name)
        return None

    msg = EmailMultiAlternatives(
        subject=subject,
        body=text,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=to,
        reply_to=reply_to,
    )
    msg.attach_alternative(html, "text/html")
    return msg


def send_template(name: str, *, subject: str, to, context: dict, reply_to=None) -> bool:
    """Render and send a single templated email. Returns whether it went."""
    if not email_configured():
        logger.warning("Email not configured — %r to %s skipped.", name, to)
        return False
    msg = build(name, subject=subject, to=to, context=context, reply_to=reply_to)
    if msg is None:
        return False
    try:
        return bool(msg.send(fail_silently=False))
    except Exception:  # noqa: BLE001
        logger.exception("Sending %r to %s failed", name, to)
        return False


def send_bulk(messages) -> int:
    """Send many pre-built messages over one connection.

    Matters for the fan-out cases — an election opening or round results on a
    200-member org. The Postmark backend turns this into batch API calls
    instead of one request per recipient.
    """
    messages = [m for m in messages if m is not None]
    if not messages:
        return 0
    if not email_configured():
        logger.warning("Email not configured — %d message(s) skipped.", len(messages))
        return 0
    try:
        connection = get_connection(fail_silently=True)
        return connection.send_messages(messages) or 0
    except Exception:  # noqa: BLE001
        logger.exception("Bulk send of %d message(s) failed", len(messages))
        return 0
