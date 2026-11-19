"""Certificates by email (client, 17 Nov 2026).

"Once a season ends and I am the winner it should be sent to me automatically,
to my email ... send them both the formats." The admin's part is to watch it
happen and, where it did not, send it by hand — never to download someone
else's certificate.

AUTOMATIC SENDING ONLY FOR SEASONS THAT CLOSE FROM NOW ON. Staging's database
is a copy of live with real members' addresses and its own jobs timer; seasons
that had already finished before this shipped would otherwise all be mailed on
the first tick. ``CERTIFICATE_AUTOSEND_SINCE`` (a date, settings) is the line:
a season whose last match was before it is left to the admin's "Send to
winner".
"""
import datetime
import logging

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)


def _since():
    raw = getattr(settings, "CERTIFICATE_AUTOSEND_SINCE", "2026-11-17")
    try:
        d = datetime.date.fromisoformat(str(raw))
    except ValueError:
        d = datetime.date(2026, 11, 17)
    return timezone.make_aware(datetime.datetime.combine(d, datetime.time.min))


def send_certificate(org, series, cert, *, group=None, trigger, by=None):
    """Email ``cert`` to its winner with the PDF and the PNG attached, and
    record the attempt. Returns the CertificateDelivery row."""
    from accounts.models import User
    from goodtip.mail import build, email_configured, site_url

    from . import certificates as certs
    from .models import CertificateDelivery
    from .views import _champion_photo_field

    user = User.objects.get(pk=cert.user_id)
    row = CertificateDelivery(
        org=org, series=series, group=group, user=user, year=cert.year, place=cert.place,
        email=user.email or "", trigger=trigger, sent_by=by, status=CertificateDelivery.STATUS_FAILED,
    )
    if not user.email:
        row.error = "This member has no email address."
        row.save()
        return row
    if not email_configured():
        row.error = "Email isn't configured on this server."
        row.save()
        return row
    try:
        photo = _champion_photo_field(org, series, user.id)
        pdf = certs.cached_file(cert, photo, "pdf")
        png = certs.cached_file(cert, photo, "png")
        from django.urls import reverse
        page = site_url(reverse("tipping:my_certificate", args=[org.id, series.name]))
        msg = build(
            "certificate_awarded",
            subject=f"Your {cert.year} {series.name} certificate: {cert.place_label}",
            to=user.email,
            context={"user": user, "org": org, "cert": cert, "series": series,
                     "where": group.name if group else org.name, "page_url": page},
        )
        if msg is None:
            row.error = "The email could not be written."
            row.save()
            return row
        msg.attach(cert.filename("pdf"), pdf, "application/pdf")
        msg.attach(cert.filename("png"), png, "image/png")
        if msg.send(fail_silently=False):
            row.status = CertificateDelivery.STATUS_SENT
        else:
            row.error = "The mail server did not accept it."
    except Exception as exc:  # noqa: BLE001 — record it; the admin can resend
        logger.exception("Certificate email to %s failed", user.email)
        row.error = str(exc)[:300]
    row.save()
    return row


def auto_send_due(log=None) -> int:
    """Send every certificate whose season has just become final and that has
    not been sent yet. Cheap on a tick with nothing to do."""
    from orgs.models import Group, Organisation

    from . import certificates as certs
    from .models import CertificateDelivery

    from .views import _champion_photo_field

    since = _since()
    sent = 0
    for org in Organisation.objects.filter(season__isnull=False).select_related("season"):
        for series in certs.competitions_for(org):
            state = certs.season_state(org, series)
            if not state.final:
                continue
            # Draw every placegetter's preview ahead of time, so the first
            # person to open one waits for nothing (client, 17 Nov 2026).
            for cert in certs.placegetters(org, series):
                certs.cached_file(cert, _champion_photo_field(org, series, cert.user_id), "preview")
            if state.closed_on is None or state.closed_on < since:
                continue
            done = set(
                CertificateDelivery.objects.filter(
                    org=org, series=series, status=CertificateDelivery.STATUS_SENT,
                    year=org.season.year,
                ).values_list("user_id", "group_id")
            )
            for group in [None, *Group.objects.filter(org=org)]:
                for cert in certs.placegetters(org, series, group=group):
                    if (cert.user_id, group.id if group else None) in done:
                        continue
                    row = send_certificate(org, series, cert, group=group, trigger=CertificateDelivery.TRIGGER_AUTO)
                    if row.status == CertificateDelivery.STATUS_SENT:
                        sent += 1
                    if log:
                        log(f"  {org.name} · {series.name} #{cert.place} {row.email}: {row.status}")
    return sent
