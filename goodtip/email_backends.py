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
