"""Account emails that carry a credential — kept apart from orgs.notifications
so the one template that must never be batched, queued or logged in full sits
on its own.
"""
from goodtip.mail import build, send_bulk

from .models import LoginCode


def send_login_code(user, code: str, purpose: str = LoginCode.PURPOSE_LOGIN) -> int:
    """Email a one-time code.

    Returns the number sent so the caller can tell the member the truth when
    mail is down, rather than parking them on a code page that will never
    receive anything.
    """
    if not user.email:
        return 0
    subject = (
        "Confirm your email — GoodTip"
        if purpose == LoginCode.PURPOSE_SIGNUP
        else f"{code} is your GoodTip sign-in code"
    )
    msg = build(
        "login_code",
        subject=subject,
        to=user.email,
        context={
            "user": user,
            "code": code,
            "purpose": purpose,
            "minutes": int(LoginCode.TTL.total_seconds() // 60),
        },
    )
    return send_bulk([msg])
