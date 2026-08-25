"""Postmark delivery, as a Django email backend.

Written as a backend rather than a set of API calls sprinkled through the code
so every existing ``send_mail`` / ``EmailMultiAlternatives`` caller — invites,
password resets, election notices, Tell the Boss — keeps working untouched, and
swapping providers later means changing one setting.

Batches into Postmark's /email/batch endpoint: an election opening on a
200-member org is 200 messages, and one request beats 200.
"""
from __future__ import annotations

import json
import logging

import requests
from django.conf import settings
from django.core.mail.backends.base import BaseEmailBackend

logger = logging.getLogger(__name__)

# RFC 2606 reserves .invalid to never resolve, and scrub_for_staging mints
# every anonymised member as <something>@staging.invalid. Mail to these can
# only ever hard-bounce, and staging sends on the same Postmark token as
# production -- so a bounce here is charged against the live site's sending
# reputation. Never deliverable, never delivered, whatever the allowlist says.
UNROUTABLE_SUFFIXES = (".invalid",)

API_URL = "https://api.postmarkapp.com/email/batch"
# Postmark's documented ceiling for a batch call.
MAX_BATCH = 500


class PostmarkEmailBackend(BaseEmailBackend):
    def __init__(self, fail_silently=False, **kwargs):
        super().__init__(fail_silently=fail_silently, **kwargs)
        self.token = getattr(settings, "POSTMARK_SERVER_TOKEN", "")
        self.stream = getattr(settings, "POSTMARK_MESSAGE_STREAM", "outbound")
        self.timeout = getattr(settings, "EMAIL_TIMEOUT", 10)
        # Postmark's per-message verdicts from the last send_messages() call.
        # Nothing in the app reads them — send_messages returns a count, as
        # Django requires — but check_email uses the MessageIDs to confirm a
        # message Postmark said "OK" to actually entered the sending queue.
        self.last_results: list[dict] = []

    def send_messages(self, email_messages):
        if not email_messages:
            return 0
        self.last_results = []
        if not self.token:
            # Mirrors how the SMTP path behaves pre-launch: log loudly, don't
            # raise, so an unconfigured environment can't break a signup.
            logger.warning(
                "POSTMARK_SERVER_TOKEN is not set — %d message(s) dropped.",
                len(email_messages),
            )
            return 0

        payloads = []
        for m in email_messages:
            try:
                payloads.append(self._payload(m))
            except Exception:  # noqa: BLE001 — one bad message must not sink the batch
                logger.exception("Could not build Postmark payload for %r", m.subject)
                if not self.fail_silently:
                    raise

        sent = 0
        for i in range(0, len(payloads), MAX_BATCH):
            sent += self._post(payloads[i:i + MAX_BATCH])
        return sent

    def _payload(self, message) -> dict:
        text_body = message.body or ""
        html_body = ""
        # EmailMultiAlternatives puts the HTML in alternatives; a plain
        # EmailMessage with content_subtype="html" puts it in body.
        for content, mimetype in getattr(message, "alternatives", []) or []:
            if mimetype == "text/html":
                html_body = content
                break
        if not html_body and getattr(message, "content_subtype", "plain") == "html":
            html_body, text_body = text_body, ""

        payload = {
            "From": message.from_email or settings.DEFAULT_FROM_EMAIL,
            "To": ",".join(message.to or []),
            "Subject": message.subject or "",
            "MessageStream": self.stream,
        }
        if text_body:
            payload["TextBody"] = text_body
        if html_body:
            payload["HtmlBody"] = html_body
        if message.cc:
            payload["Cc"] = ",".join(message.cc)
        if message.bcc:
            payload["Bcc"] = ",".join(message.bcc)
        if message.reply_to:
            payload["ReplyTo"] = ",".join(message.reply_to)
        return payload

    def _post(self, batch) -> int:
        try:
            r = requests.post(
                API_URL,
                data=json.dumps(batch),
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "X-Postmark-Server-Token": self.token,
                },
                timeout=self.timeout,
            )
            r.raise_for_status()
            results = r.json()
        except requests.RequestException as e:
            logger.error("Postmark batch of %d failed: %s", len(batch), e)
            if not self.fail_silently:
                raise
            return 0

        # Postmark returns 200 for the batch with per-message ErrorCode values,
        # so a 2xx does not mean every message was accepted.
        ok = 0
        for res in results if isinstance(results, list) else []:
            self.last_results.append(res)
            if res.get("ErrorCode") == 0:
                ok += 1
            else:
                logger.error(
                    "Postmark rejected %s: [%s] %s",
                    res.get("To"), res.get("ErrorCode"), res.get("Message"),
                )
        return ok


class AllowlistEmailBackend(BaseEmailBackend):
    """Deliver only to approved addresses; log and drop everything else.

    Staging runs on a scrubbed copy of the production database, which means it
    holds thousands of member rows whose addresses *look* real because they
    have the same shape as the originals. Any code path that mails "all members
    of this org" — an election opening, a round reminder, a recap — is one
    misconfigured token away from doing that for real from a box nobody thinks
    of as the live site.

    So staging never gets a bare Postmark backend. It gets this, wrapping it.
    ``EMAIL_ALLOWLIST`` is a comma-separated list of addresses (``me@x.com``)
    or whole domains (``@client.com``); a message survives only if every one of
    its recipients matches. Partial delivery is deliberately not a thing: a
    message that reached half its To: line is harder to reason about than one
    that was dropped and logged.

    A single ``*`` means deliver to every real address. That is the right
    setting when the staging gate is already the access boundary -- everyone
    who can reach the site was let in by someone holding the gate password, so
    a second list of who may be emailed just blocks testing invites, signup
    and sign-in. ``*`` still does not deliver to ``UNROUTABLE_SUFFIXES``:
    those addresses are the scrub's own invention and can only bounce.

    An empty allowlist drops everything. That is the default on staging on
    purpose — the failure mode of a forgotten env var should be silence, not a
    send to the entire membership.
    """

    def __init__(self, fail_silently=False, **kwargs):
        super().__init__(fail_silently=fail_silently, **kwargs)
        entries = [
            entry.strip().lower()
            for entry in getattr(settings, "EMAIL_ALLOWLIST", "").split(",")
            if entry.strip()
        ]
        self.allow_all = "*" in entries
        self.allowed = [entry for entry in entries if entry != "*"]
        self._delegate = None

    @property
    def delegate(self):
        """The real backend, built lazily so a fully-blocked send never opens
        a connection or reads a token it has no use for."""
        if self._delegate is None:
            from django.core.mail import get_connection

            self._delegate = get_connection(
                backend=getattr(
                    settings,
                    "EMAIL_ALLOWLIST_DELEGATE",
                    "django.core.mail.backends.smtp.EmailBackend",
                ),
                fail_silently=self.fail_silently,
            )
        return self._delegate

    def _permitted(self, address):
        address = (address or "").strip().lower()
        if not address:
            return False
        domain = address[address.rfind("@"):] if "@" in address else ""
        if domain.endswith(UNROUTABLE_SUFFIXES):
            return False
        if self.allow_all:
            return True
        return any(
            address == entry if not entry.startswith("@") else domain == entry
            for entry in self.allowed
        )

    def send_messages(self, email_messages):
        if not email_messages:
            return 0

        permitted, blocked = [], []
        for message in email_messages:
            recipients = message.recipients()
            if recipients and all(self._permitted(r) for r in recipients):
                permitted.append(message)
            else:
                blocked.append(message)

        if blocked:
            # One line per message, at WARNING, with the subject: when someone
            # asks "did staging send that?", the journal has to be able to say.
            for message in blocked:
                logger.warning(
                    "EMAIL_ALLOWLIST: dropped %r to %s",
                    message.subject,
                    ", ".join(message.recipients()) or "(no recipients)",
                )
            logger.warning(
                "EMAIL_ALLOWLIST: %d message(s) dropped, %d allowed through.",
                len(blocked),
                len(permitted),
            )

        return self.delegate.send_messages(permitted) if permitted else 0
