"""Account emails that carry a credential — kept apart from orgs.notifications
so the one template that must never be batched, queued or logged in full sits
on its own.
"""
import sys

from django.conf import settings

from goodtip.mail import build, send_bulk

from .models import LoginCode


def _echo_code_to_console(user, code: str, purpose: str) -> None:
    """Print a sign-in code to the terminal running the dev server.

    Off by default now that Postmark delivers for real — a code belongs in an
    inbox, not in scrollback. It stays for the case real delivery isn't an
    option: on the console backend the code is buried in a full HTML render,
    which is miserable to dig a six-digit number out of.

    Two guards, both of which must hold: DEBUG, and SHOW_OTP_IN_CONSOLE. A live
    server has neither, so a real code can never reach a log.
    """
    if not settings.DEBUG or not getattr(settings, "SHOW_OTP_IN_CONSOLE", False):
        return
    label = "signup confirmation" if purpose == LoginCode.PURPOSE_SIGNUP else "sign-in"
    minutes = int(LoginCode.TTL.total_seconds() // 60)
    rule = "=" * 54
    sys.stdout.write(
        f"\n{rule}\n"
        f"  ONE-TIME CODE: {code}   ({label}, {minutes} min)\n"
        f"  for: {user.email}\n"
        f"{rule}\n\n"
    )
    sys.stdout.flush()


def send_login_code(user, code: str, purpose: str = LoginCode.PURPOSE_LOGIN) -> int:
    """Email a one-time code.

    Returns the number sent so the caller can tell the member the truth when
    mail is down, rather than parking them on a code page that will never
    receive anything.
    """
    if not user.email:
        return 0
    _echo_code_to_console(user, code, purpose)
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
