"""One-tap links in code emails (client, 20 Nov 2026).

Every code email also carries a "Sign in" button, so nobody has to copy six
digits from one app into another. The link is the code itself, signed: it is
worth exactly what the code is worth, expires when the code does, and dies
with it — once either the code or the link is used, the other stops working.

THE ONE RULE THAT KEEPS IT SAFE. A link that worked in any browser would turn
"password AND email" into "email alone": anybody who could read the inbox
could skip the password. So a link only completes the sign-in in the browser
that typed the password (the member flow's pending session; the admin's
signed-in session). Opened anywhere else it explains itself and does nothing,
and the code still works where it should. That rule is also what makes mail
scanners harmless — they follow links without a session, so a scanner can
never use up somebody's code.
"""
from __future__ import annotations

from django.core import signing
from django.urls import reverse

SALT = "goodtip.code-link"


def make_url(request, row, code: str, route: str) -> str:
    token = signing.dumps({"r": row.pk, "c": code}, salt=SALT)
    return request.build_absolute_uri(reverse(route, args=[token]))


def read(token: str, max_age_seconds: int):
    """(row id, code) from a link, or None when it is forged or too old."""
    try:
        data = signing.loads(token, salt=SALT, max_age=max_age_seconds)
        return int(data["r"]), str(data["c"])
    except (signing.BadSignature, KeyError, TypeError, ValueError):
        return None
