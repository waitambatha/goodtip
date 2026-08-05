"""Prove outbound email works, before finding out via a failed signup.

Every real send in the app is fire-and-forget by design — goodtip.mail never
raises, because a bounced election notice must not take down the action that
triggered it. Good behaviour in production, useless for diagnosis: a misconfigured
sender looks exactly like a working one from the outside.

This command is the opposite trade. It reports the resolved configuration, sends
one message, and prints Postmark's own verdict on it — including the per-message
rejection that the backend would normally only write to a log.
"""
from __future__ import annotations

import logging

from django.conf import settings
from django.core.mail import EmailMultiAlternatives, get_connection
from django.core.management.base import BaseCommand, CommandError

# Postmark's code for "the From address isn't a verified sender signature".
# Far and away the most common failure, and the least self-explanatory, so it
# gets spelled out rather than left as a bare number.
ERR_UNVERIFIED_SENDER = 300


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
            sent = self._send(to)
        finally:
            log.removeHandler(handler)

        if sent:
            self.stdout.write(self.style.SUCCESS(
                f"Accepted for delivery to {to}.\n"
                "If it doesn't arrive, check spam and then Postmark → Activity "
                "for the bounce."
            ))
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

    def _send(self, to: str) -> int:
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
        return connection.send_messages([msg]) or 0

    def _row(self, label: str, value) -> None:
        self.stdout.write(f"  {label:<14} {value}")


class _Capture(logging.Handler):
    def __init__(self, sink: list):
        super().__init__(level=logging.ERROR)
        self.sink = sink

    def emit(self, record):
        self.sink.append(record)
