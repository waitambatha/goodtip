"""Prove outbound email works, before finding out via a failed signup.

Every real send in the app is fire-and-forget by design — goodtip.mail never
raises, because a bounced election notice must not take down the action that
triggered it. Good behaviour in production, useless for diagnosis: a misconfigured
sender looks exactly like a working one from the outside.

This command is the opposite trade. It reports the resolved configuration, sends
one message, and prints Postmark's own verdict on it — including the per-message
rejection that the backend would normally only write to a log.

"Postmark said OK" is not the same as "Postmark sent it". On the free plan the
API keeps returning ErrorCode 0 with a MessageID after the monthly allowance is
spent, and those messages never appear in Activity and never leave — which is
indistinguishable from working, from inside the app. So the send is followed by
a lookup of that MessageID, and the month's running total is printed either way.
"""
from __future__ import annotations

import datetime as dt
import logging
import time

import requests
from django.conf import settings
from django.core.mail import EmailMultiAlternatives, get_connection
from django.core.management.base import BaseCommand, CommandError

API_BASE = "https://api.postmarkapp.com"
# Emails per month included on Postmark's free plan. Sending stops dead at this
# number until the billing cycle resets; there are no overages to buy.
FREE_PLAN_MONTHLY = 100

# Postmark's code for "the From address isn't a verified sender signature".
# Far and away the most common failure, and the least self-explanatory, so it
# gets spelled out rather than left as a bare number.
ERR_UNVERIFIED_SENDER = 300
# "This message was not found" — what a MessageID lookup returns for a message
# that was accepted but never queued.
ERR_MESSAGE_NOT_FOUND = 701


class Command(BaseCommand):
    help = "Send a test email and report exactly what the provider said."

    def add_arguments(self, parser):
        parser.add_argument("to", help="Address to send the test message to")
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Show the configuration and stop, without sending anything.",
        )

    def handle(self, *args, **options):
        to = options["to"]
        backend = settings.EMAIL_BACKEND
        token = getattr(settings, "POSTMARK_SERVER_TOKEN", "")
        sender = settings.DEFAULT_FROM_EMAIL

        self.stdout.write(self.style.MIGRATE_HEADING("Email configuration"))
        self._row("Backend", backend.rsplit(".", 1)[-1])
        self._row("From", sender)
        self._row("To", to)
        if "Postmark" in backend:
            self._row("Token", f"…{token[-6:]}" if token else "(not set)")
            self._row("Stream", getattr(settings, "POSTMARK_MESSAGE_STREAM", "outbound"))
        self._row("Site base URL", getattr(settings, "SITE_BASE_URL", ""))

        used = self._sent_this_month(token) if "Postmark" in backend and token else None
        if used is not None:
            self._row("Sent this month", str(used))
        self.stdout.write("")

        # Being over the free-plan figure is a REASON TO CHECK, not a verdict.
        # This used to assert outright that "nothing is being sent" past 100,
        # which is only true on the free plan — an upgraded account sails past
        # it. On 2026-08-24, with 192 sent, that wording sent a real
        # investigation off after a billing problem that did not exist while
        # the actual codes were being delivered normally (Postmark logged
        # "Delivered … 250 OK" for them). The count alone cannot tell the two
        # cases apart; only Activity can, which is what the post-send lookup
        # below is for.
        if used is not None and used >= FREE_PLAN_MONTHLY:
            self.stdout.write(self.style.WARNING(
                f"{used} sent this month, past the {FREE_PLAN_MONTHLY} included on "
                "Postmark's FREE plan. If this account is still on that plan, sends "
                "beyond the allowance are accepted with a MessageID and silently "
                "dropped — check Billing at account.postmarkapp.com. If it is on a "
                "paid plan this figure is fine and delivery is unaffected; the "
                "delivery check below is what actually settles it."
            ))
            self.stdout.write("")

        if "console" in backend:
            self.stdout.write(self.style.WARNING(
                "The console backend is active — this prints the message and "
                "delivers nothing.\nSet EMAIL_SEND_FOR_REAL=True in .env to send "
                "through Postmark."
            ))
        elif "Postmark" in backend and not token:
            raise CommandError(
                "POSTMARK_SERVER_TOKEN is empty — the backend would drop the "
                "message and return 0. Add the token to .env."
            )

        if options["dry_run"]:
            self.stdout.write("Dry run — nothing sent.")
            return

        # The backend logs a per-message rejection at ERROR and returns a count,
        # so a plain "0 sent" hides the reason. Tap the logger to bring it out.
        captured: list[logging.LogRecord] = []
        handler = _Capture(captured)
        log = logging.getLogger("goodtip.email_backends")
        log.addHandler(handler)
        try:
            sent, results = self._send(to)
        finally:
            log.removeHandler(handler)

        if sent:
            self.stdout.write(self.style.SUCCESS(f"Postmark accepted the message for {to}."))
            self._confirm_queued(token, results)
            return

        self.stdout.write(self.style.ERROR(f"Not delivered to {to}."))
        for record in captured:
            self.stdout.write(f"  {record.getMessage()}")
        if any(str(ERR_UNVERIFIED_SENDER) in r.getMessage() for r in captured):
            self.stdout.write(self.style.WARNING(
                f"\nPostmark will not send from {sender} — that address isn't a "
                "verified sender signature.\nEither verify it in Postmark → "
                "Sender Signatures, or set DEFAULT_FROM_EMAIL in .env to an "
                "address that already is."
            ))

    def _sent_this_month(self, token: str) -> int | None:
        """How many emails this server has sent since the 1st, or None if unknown.

        The allowance is what runs out, so it's worth showing before the send
        rather than after — a number one short of the cap is the only warning
        there is that the next election notice fans out into nothing.
        """
        today = dt.date.today()
        try:
            r = requests.get(
                f"{API_BASE}/stats/outbound/sends",
                params={"fromdate": today.replace(day=1).isoformat(), "todate": today.isoformat()},
                headers={"Accept": "application/json", "X-Postmark-Server-Token": token},
                timeout=15,
            )
            r.raise_for_status()
            return int(r.json().get("Sent", 0))
        except (requests.RequestException, ValueError, TypeError):
            # Diagnosis is the job here; a stats hiccup shouldn't stop the send.
            return None

    def _confirm_queued(self, token: str, results: list[dict]) -> None:
        """Look the accepted MessageIDs back up in Postmark's Activity.

        A message that was really queued resolves within a second or two. One
        that doesn't exist at all was swallowed — the signature of a spent
        allowance, and the whole reason this command can't stop at "accepted".
        """
        ids = [r.get("MessageID") for r in results if r.get("ErrorCode") == 0 and r.get("MessageID")]
        if not token or not ids:
            return

        for message_id in ids:
            status, detail = None, {}
            for attempt in range(3):
                if attempt:
                    time.sleep(3)
                try:
                    r = requests.get(
                        f"{API_BASE}/messages/outbound/{message_id}/details",
                        headers={"Accept": "application/json", "X-Postmark-Server-Token": token},
                        timeout=15,
                    )
                    detail = r.json() if r.content else {}
                except (requests.RequestException, ValueError):
                    return  # Can't reach the API to check; say nothing rather than cry wolf.
                if r.status_code == 200:
                    status = detail.get("Status", "Queued")
                    break
                if detail.get("ErrorCode") != ERR_MESSAGE_NOT_FOUND:
                    return  # Some other API problem — not evidence of a drop.

            if status:
                self.stdout.write(
                    f"  Postmark Activity says: {status}. "
                    "If it isn't in the inbox, check spam."
                )
            else:
                self.stdout.write(self.style.ERROR(
                    f"  …but message {message_id} never reached Postmark's Activity, "
                    "so it was accepted and dropped — it will not arrive.\n"
                    "  That is what a spent monthly allowance looks like: check "
                    "'Sent this month' above against the plan, and Billing at "
                    "account.postmarkapp.com."
                ))

    def _send(self, to: str) -> tuple[int, list[dict]]:
        msg = EmailMultiAlternatives(
            subject="GoodTip — email delivery test",
            body=(
                "This is a test message from the GoodTip check_email command.\n\n"
                "If you're reading it, outbound email works: sign-in codes, "
                "invites, election notices and round results can all be "
                "delivered.\n"
            ),
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[to],
        )
        msg.attach_alternative(
            "<p>This is a test message from the GoodTip "
            "<code>check_email</code> command.</p>"
            "<p>If you're reading it, outbound email works: sign-in codes, "
            "invites, election notices and round results can all be "
            "delivered.</p>",
            "text/html",
        )
        # fail_silently=False so a network or auth failure surfaces here as a
        # traceback rather than a silent zero — this command exists to be told.
        connection = get_connection(fail_silently=False)
        sent = connection.send_messages([msg]) or 0
        return sent, list(getattr(connection, "last_results", []))

    def _row(self, label: str, value) -> None:
        self.stdout.write(f"  {label:<14} {value}")


class _Capture(logging.Handler):
    def __init__(self, sink: list):
        super().__init__(level=logging.ERROR)
        self.sink = sink

    def emit(self, record):
        self.sink.append(record)
