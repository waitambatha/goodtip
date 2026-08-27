"""A second factor in front of the Django admin.

WHY THE ADMIN NEEDS ITS OWN GATE.

/admin/ is the control plane: every organisation, every member, every charity,
every payment record, editable by hand. Django protects it with `is_staff` and
a password, which is the same single factor guarding a member's tipping
account — and the member app already asks for more than that, since sign-in
there emails a six-digit code. The most powerful door in the product had the
weakest lock on it.

So reaching the admin now requires a fresh code emailed to the address on the
account, on top of whatever got the session authenticated in the first place.
That last part matters: the check is on the SESSION, not on the login. Signing
in through the member app and then navigating to /admin/ does not skip it,
because the member app's own two-step is a different purpose with a different
code, and this gate does not care how you arrived.

WHAT THIS IS NOT. It is not TOTP. Email is a weaker second factor than an
authenticator app and nobody should pretend otherwise — but it is the factor
this product already has working, already delivers reliably through Postmark,
and requires no enrolment step that would lock an admin out of their own
control plane on a Friday afternoon. Moving to TOTP later changes this module
and nothing that calls it.
"""
from __future__ import annotations

from datetime import timedelta

from django.utils import timezone


# How long one verification is good for. Long enough that a day's work is not
# a stream of interruptions; short enough that a laptop left open in a cafe is
# not a standing invitation. Re-verifying costs one email and six digits.
SESSION_TTL = timedelta(hours=8)

# The session key holding when the code was accepted. Storing the TIME rather
# than a boolean is what makes the TTL enforceable — a `True` would have had
# to be expired by something else, and nothing would have been.
SESSION_KEY = "admin_otp_verified_at"

# Where to send them back to once verified.
NEXT_KEY = "admin_otp_next"


def mark_verified(session) -> None:
    session[SESSION_KEY] = timezone.now().isoformat()


def clear(session) -> None:
    session.pop(SESSION_KEY, None)
    session.pop(NEXT_KEY, None)


def is_verified(session) -> bool:
    """Has this session cleared the admin gate, recently enough to still count?

    A malformed or missing stamp is treated as "no". That is the safe
    direction: the cost of a false negative is one more email, the cost of a
    false positive is unauthenticated access to everything.
    """
    stamp = session.get(SESSION_KEY)
    if not stamp:
        return False
    try:
        when = timezone.datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return False
    if timezone.is_naive(when):
        return False
    return timezone.now() - when < SESSION_TTL


def issue_and_send(user) -> bool:
    """Email a fresh admin code. Returns whether it went.

    Failure is reported rather than swallowed: an admin staring at a code
    entry box for a mail that is never coming needs to be told, and unlike the
    member-facing sends there is no graceful degradation available here.
    """
    from accounts.models import LoginCode
    from goodtip.mail import send_template

    row, code = LoginCode.issue(user, purpose=LoginCode.PURPOSE_ADMIN)
    sent = send_template(
        "admin_code",
        subject=f"{code} is your GoodTip admin code",
        to=user.email,
        context={"user": user, "code": code, "ttl_minutes": int(LoginCode.TTL.total_seconds() // 60)},
    )
    if not sent:
        # In development mail is often not configured at all. The code is
        # useless to an attacker who cannot read this process's stdout, and
        # an admin who cannot get in cannot fix the mail settings either.
        _echo_to_console(user, code)
    return sent


def _echo_to_console(user, code: str) -> None:
    import sys

    banner = (
        f"\n{'=' * 58}\n"
        f"  ADMIN CODE for {user.email}: {code}\n"
        f"  (email did not send — printed here so you are not locked out)\n"
        f"{'=' * 58}\n"
    )
    sys.stdout.write(banner)
    sys.stdout.flush()


def check(user, code: str) -> bool:
    """Verify a submitted admin code against the newest usable one."""
    from accounts.models import LoginCode

    row = (
        LoginCode.objects
        .filter(user=user, purpose=LoginCode.PURPOSE_ADMIN, consumed_at__isnull=True)
        .order_by("-created_at")
        .first()
    )
    return bool(row and row.verify(code))
