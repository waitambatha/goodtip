"""Which environment created a record: live (real) or staging (testing).

Every account-bearing model a client actually signs up into — User,
Organisation, Group, LaunchSignup, and the two tables that join a person to
one — carries this. New rows are stamped by `current_origin()` at creation
time, in whichever environment actually created them; nothing here changes
that after the fact. The one place ORIGIN_LIVE rows show up on staging is as
a deliberate copy, made by `orgs.management.commands.sync_live_backup`
(staging-only), which is the only code allowed to write ORIGIN_LIVE into
staging's own database.
"""

from django.conf import settings
from django.db import models

ORIGIN_LIVE = "live"
ORIGIN_STAGING = "staging"
ORIGIN_CHOICES = [(ORIGIN_LIVE, "Live (real)"), (ORIGIN_STAGING, "Staging (testing)")]


def current_origin():
    return ORIGIN_STAGING if settings.IS_STAGING else ORIGIN_LIVE


class OriginMixin(models.Model):
    """Adds the real/testing label, and the hook the backup sync matches on.

    `live_id` is this row's id on the live database — set only by
    sync_live_backup when it copies a live row onto staging, and the key it
    re-matches on every run so a repeat sync updates rather than duplicates.
    Null everywhere on live itself, and null on staging for anything staging
    created on its own.
    """

    origin = models.CharField(
        max_length=10, choices=ORIGIN_CHOICES, default=current_origin,
        editable=False, db_index=True,
    )
    live_id = models.PositiveBigIntegerField(null=True, blank=True, editable=False, db_index=True)

    class Meta:
        abstract = True
