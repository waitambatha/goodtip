"""The waiting list as a small, separate world.

While the product is finished behind the staging gate, goodtip.com.au is a
holding page. A person who joins the list needs somewhere to come back to and
something to see — but not the product, which is why none of this touches
accounts.User, Django's auth session or the login views.

  signup   name + email + password (+ two optional answers). The address is
           unproven, so a six-digit code goes to it; nothing sticks to a
           verified account until that code comes back.
  sign-in  email + password. NO code. The one code was the proof of the address.
  session  its own key. It authenticates nothing in the real system.

Whether a person is INVITED is LaunchSignup.notified_at, and the invitation
itself (WaitlistCampaign) is composed and sent from the super admin.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from django.contrib.auth.hashers import check_password, make_password
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.utils import timezone

from goodtip.mail import build, send_bulk, site_url

from .models import LaunchSignup, WaitlistCampaign

logger = logging.getLogger(__name__)

SESSION_KEY = "waitlist_member_id"
MIN_PASSWORD = 8

# A hash to burn time against when the address is unknown, so a wrong address
# and a wrong password take the same time to answer.
_DUMMY_HASH = make_password("not-a-real-password")


# ---------------------------------------------------------------- sign-up ----

@dataclass
class Result:
    ok: bool
    error: str = ""
    row: LaunchSignup | None = None


def _clean_choice(value: str, choices) -> str:
    value = (value or "").strip()
    return value if value in {c[0] for c in choices} else ""


def start_signup(data, *, source_page: str = "coming-soon") -> Result:
    """Take the sign-up form and email a code. Nothing is claimed yet.

    A brand-new address becomes a (still unverified) row straight away, because
    the lead is the asset and an abandoned code step should not lose it. An
    address that already has a verified account is NOT touched: whatever was
    typed waits in `pending` and is applied only by whoever reads that inbox.
    Either way the answer is the same, so the form cannot be used to find out
    who is on the list. As a side effect this is also the "forgot my password"
    path — sign up again with the same address and the new password takes
    effect once the code is entered.
    """
    name = (data.get("name") or "").strip()[:120]
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    if not name or not email:
        return Result(False, "Both a name and an email address are needed.")
    try:
        validate_email(email)
    except ValidationError:
        return Result(False, "That email address doesn't look right — check it and try again.")
    if len(password) < MIN_PASSWORD:
        return Result(False, f"Choose a password of at least {MIN_PASSWORD} characters.")
    if len(password) > 128:
        return Result(False, "That password is too long.")

    org_type = _clean_choice(data.get("org_type"), LaunchSignup.ORG_TYPE_CHOICES)
    platform = _clean_choice(data.get("current_platform"), LaunchSignup.PLATFORM_CHOICES)

    try:
        with transaction.atomic():
            row = LaunchSignup.objects.select_for_update().filter(email=email).first()
            if row is None:
                row = LaunchSignup(email=email, source_page=(source_page or "")[:200])
            elif row.code_sent_at and timezone.now() - row.code_sent_at < LaunchSignup.RESEND_AFTER:
                return Result(False, "A code was only just sent — give it a moment, then try again.")
            if not row.is_verified:
                row.name, row.org_type, row.current_platform = name, org_type, platform
            row.pending = {
                "name": name, "org_type": org_type, "current_platform": platform,
                "password_hash": make_password(password),
            }
            code = row.issue_code()
            row.save()
    except Exception:  # noqa: BLE001 — a lead is never worth a 500
        logger.exception("Waiting-list sign-up failed to save")
        return Result(False, "That didn't save — please try again in a moment.")

    if send_code(row, code) == 0:
        return Result(False, "We couldn't send the code just now — please try again in a minute.", row)
    return Result(True, row=row)


def send_code(row: LaunchSignup, code: str) -> int:
    """Email the confirmation code. Returns how many messages went."""
    from .notifications import _echo_code_to_console

    class _Who:  # the console helper wants something with .email
        email = row.email

    _echo_code_to_console(_Who, code, "signup")
    msg = build(
        "waitlist_code",
        # The code in the subject, as the sign-in email does: it is readable
        # from the inbox list, and a fresh subject each time stops Gmail filing
        # it into an older "Confirm your email" thread where nobody looks.
        subject=f"{code} is your GoodTip waiting-list code",
        to=row.email,
        context={
            "name": (row.pending or {}).get("name") or row.name,
            "code": code,
            "minutes": int(LaunchSignup.CODE_TTL.total_seconds() // 60),
        },
    )
    return send_bulk([msg])


def resend_code(email: str) -> Result:
    row = LaunchSignup.objects.filter(email=(email or "").strip().lower()).first()
    # Same answer whether or not there is anything to resend.
    if row is None or not row.pending:
        return Result(True)
    if row.code_sent_at and timezone.now() - row.code_sent_at < LaunchSignup.RESEND_AFTER:
        return Result(False, "A code was only just sent — give it a moment, then try again.")
    code = row.issue_code()
    row.save(update_fields=["code_hash", "code_expires_at", "code_sent_at", "code_attempts"])
    if send_code(row, code) == 0:
        return Result(False, "We couldn't send the code just now — please try again in a minute.")
    return Result(True, row=row)


def confirm_code(email: str, code: str) -> Result:
    """Check the code; on success apply the pending details and verify."""
    row = LaunchSignup.objects.filter(email=(email or "").strip().lower()).first()
    bad = Result(False, "That code isn't right, or it has expired. Check it, or ask for a new one.")
    if row is None or not row.pending:
        return bad
    if row.code_attempts >= LaunchSignup.CODE_MAX_ATTEMPTS:
        return Result(False, "Too many tries on that code — ask for a new one.")
    good = row.check_code(code)
    if not good:
        row.save(update_fields=["code_attempts"])
        return bad

    p = row.pending
    row.name = p.get("name") or row.name
    row.org_type = p.get("org_type", row.org_type)
    row.current_platform = p.get("current_platform", row.current_platform)
    row.password_hash = p.get("password_hash", "")
    row.email_verified_at = row.email_verified_at or timezone.now()
    row.pending, row.code_hash, row.code_expires_at, row.code_attempts = {}, "", None, 0
    row.failed_signins, row.locked_until = 0, None
    row.save()
    send_welcome(row)
    return Result(True, row=row)


def send_welcome(row: LaunchSignup) -> None:
    """The launch_received acknowledgement, sent once the address is proven."""
    try:
        from .form_replies import reply_for
        from goodtip.mail import send_template

        reply = reply_for("launch")
        send_template(
            reply["template"], subject=reply["subject"], to=[row.email],
            context={"signup": row, "site_link": site_url("/")},
        )
    except Exception:  # noqa: BLE001 — a courtesy, never a failure
        logger.exception("Waiting-list welcome to %s failed", row.pk)


# ---------------------------------------------------------------- sign-in ----

def sign_in(email: str, password: str) -> Result:
    """Email + password. No code. Failures are rate-limited per account."""
    email = (email or "").strip().lower()
    row = LaunchSignup.objects.filter(email=email).first()
    generic = Result(False, "That email and password don't match.")
    if row is None or not row.has_account:
        check_password(password or "", _DUMMY_HASH)
        return generic
    now = timezone.now()
    if row.locked_until and row.locked_until > now:
        return Result(False, "Too many attempts. Try again in a few minutes.")
    if not row.check_password(password or ""):
        row.failed_signins += 1
        if row.failed_signins >= LaunchSignup.SIGNIN_MAX_FAILS:
            row.locked_until = now + LaunchSignup.SIGNIN_LOCK
            row.failed_signins = 0
        row.save(update_fields=["failed_signins", "locked_until"])
        return generic
    row.failed_signins, row.locked_until, row.last_signin_at = 0, None, now
    row.save(update_fields=["failed_signins", "locked_until", "last_signin_at"])
    return Result(True, row=row)


def log_in(request, row: LaunchSignup) -> None:
    request.session.cycle_key()
    request.session[SESSION_KEY] = row.pk


def log_out(request) -> None:
    request.session.pop(SESSION_KEY, None)


def current_member(request) -> LaunchSignup | None:
    pk = request.session.get(SESSION_KEY)
    if not pk:
        return None
    row = LaunchSignup.objects.filter(pk=pk).first()
    if row is None or not row.has_account:
        request.session.pop(SESSION_KEY, None)
        return None
    return row


# ------------------------------------------------------------ the invitation --

DEFAULT_SUBJECT = "GoodTip is on — you can sign in now"
DEFAULT_HEADING = "The wait is over."
DEFAULT_BUTTON = "Get started"
DEFAULT_BODY = """Hi {first_name},

GoodTip is now fully built, and it is open. You asked us to tell you the moment sign-ups opened — this is that email.

Here is how to get going:
1. Use the button below to create your account.
2. Set up your organisation: a workplace, a club, a school or a group of mates.
3. Pick the charity your organisation gives to, and invite everyone in.

Tipping is free for everyone who plays. Your organisation pays one platform fee, and GoodTip gives a share of it to the charity your organisation picks. Founding Member pricing is open to this list first.

Round one is coming. Be in the room when it does.

Questions? Just reply to this email — it reaches a person.

The GoodTip team"""


def default_campaign_fields() -> dict:
    return {
        "subject": DEFAULT_SUBJECT,
        "heading": DEFAULT_HEADING,
        "body": DEFAULT_BODY,
        "button_label": DEFAULT_BUTTON,
    }


def first_name_of(name: str) -> str:
    parts = (name or "").split()
    return parts[0] if parts else "there"


def fill(text: str, signup) -> str:
    """Swap the two placeholders. Plain replace, so a stray brace can't raise."""
    return (text or "").replace("{first_name}", first_name_of(signup.name)).replace("{name}", signup.name or "there")


def paragraphs(text: str) -> list[str]:
    return [p.strip() for p in (text or "").replace("\r\n", "\n").split("\n\n") if p.strip()]


def invitation_link() -> str:
    return site_url("/signup/")


def build_invitation(campaign: WaitlistCampaign, signup, to=None):
    """One recipient's copy of the invitation."""
    body = fill(campaign.body, signup)
    return build(
        "waitlist_invite",
        subject=fill(campaign.subject, signup),
        to=to or signup.email,
        context={
            "heading": fill(campaign.heading, signup),
            "paragraphs": paragraphs(body),
            "body_text": body,
            "button_label": campaign.button_label,
            "button_url": invitation_link(),
        },
    )


def audience():
    """Everyone who has not been invited yet."""
    return LaunchSignup.objects.filter(notified_at__isnull=True).order_by("created_at", "pk")


def send_campaign(campaign: WaitlistCampaign, *, budget_seconds: float | None = None, batch: int = 50) -> int:
    """Send the invitation to everybody not yet invited. Returns how many went.

    ONCE PER PERSON is the invariant, and it is worth more than delivering to
    everyone. Each chunk is CLAIMED first — notified_at is stamped inside a
    transaction that skips rows another run has locked — and only then mailed,
    so two overlapping runs (the timer and a button press) cannot both mail the
    same address.

    What happens to a chunk afterwards:
      * every message accepted  -> done
      * none accepted           -> mail is down or refusing; the chunk is
                                   un-stamped so a later run tries it again,
                                   and this run stops
      * SOME accepted           -> the provider rejected particular addresses
                                   (a bounced or suppressed inbox) and the batch
                                   call cannot say which. Retrying would mail the
                                   people who did get it a second time, and the
                                   rejected ones would fail again, so the chunk
                                   stays stamped and the shortfall is counted in
                                   `failed_count` for the screen to show.

    `budget_seconds` lets a web request send what it can and hand the rest to
    the jobs timer; the command runs to the end.
    """
    started = time.monotonic()
    campaign.status = WaitlistCampaign.STATUS_SENDING
    campaign.started_at = campaign.started_at or timezone.now()
    campaign.save(update_fields=["status", "started_at"])

    sent, mail_down, skip = 0, False, []
    while not mail_down:
        if budget_seconds is not None and time.monotonic() - started > budget_seconds:
            break
        with transaction.atomic():
            rows = list(
                LaunchSignup.objects.select_for_update(skip_locked=True)
                .filter(notified_at__isnull=True).exclude(pk__in=skip).order_by("created_at", "pk")[:batch]
            )
            if not rows:
                break
            LaunchSignup.objects.filter(pk__in=[r.pk for r in rows]).update(notified_at=timezone.now())

        msgs = [(r, build_invitation(campaign, r)) for r in rows]
        built = [m for _, m in msgs if m is not None]
        unbuilt = [r.pk for r, m in msgs if m is None]
        went = send_bulk(built) if built else 0
        if built and went == 0:
            mail_down = True
            skip += [r.pk for r in rows]
            LaunchSignup.objects.filter(pk__in=[r.pk for r in rows]).update(notified_at=None)
            continue
        if unbuilt:
            # An address that cannot even be addressed is not retried.
            skip += unbuilt
            LaunchSignup.objects.filter(pk__in=unbuilt).update(notified_at=None)
        campaign.failed_count += (len(built) - went) + len(unbuilt)
        sent += went

    campaign.sent_count += sent
    left = audience().exclude(pk__in=skip).exists()
    if left or mail_down:
        # Budget ran out, or mail is down: the timer picks the rest up.
        campaign.status = WaitlistCampaign.STATUS_SENDING
        campaign.scheduled_for = campaign.scheduled_for or timezone.now()
    else:
        campaign.status = WaitlistCampaign.STATUS_SENT
        campaign.finished_at = timezone.now()
        campaign.scheduled_for = None
    campaign.save()
    return sent


def due_campaign():
    """The campaign whose scheduled time has come, if any."""
    now = timezone.now()
    return (
        WaitlistCampaign.objects
        .filter(status__in=[WaitlistCampaign.STATUS_SCHEDULED, WaitlistCampaign.STATUS_SENDING],
                scheduled_for__isnull=False, scheduled_for__lte=now)
        .order_by("pk").first()
    )
