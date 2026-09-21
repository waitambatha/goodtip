"""Closing the loop on a "tell the boss" note.

Three moments, each a different part of the codebase, so the logic lives here
rather than being scattered through signup and org creation:

  1. the note is sent          -> BossInvite(status=sent)
  2. the boss signs up         -> status=joined
  3. the boss creates a group  -> status=complete, and the sender is added to it

Step 3 is the promise the feature makes. Someone who talks their workplace into
starting a comp should not then have to find it and ask to join like a stranger.
"""
from __future__ import annotations

import logging

from django.utils import timezone

logger = logging.getLogger(__name__)


def link_boss_signup(user) -> int:
    """Called after a new account is created. Matches any note sent to them."""
    from .models import BossInvite

    if not user.email:
        return 0
    return BossInvite.objects.filter(
        boss_email__iexact=user.email, status=BossInvite.STATUS_SENT,
    ).update(
        status=BossInvite.STATUS_JOINED,
        joined_at=timezone.now(),
        boss_user=user,
    )


def complete_boss_invites(org, creator) -> int:
    """Called when an org is created. Adds whoever asked for it to the group.

    Only fires for notes this creator actually received, and only once — a
    second league later should not re-add people who asked about the first.
    """
    from orgs.models import OrgMember
    from orgs.services import add_member

    from .models import BossInvite

    invites = list(
        BossInvite.objects.filter(boss_user=creator, status=BossInvite.STATUS_JOINED)
        .select_related("sender")
    )
    added = 0
    for invite in invites:
        # THROUGH add_member, NOT OrgMember.objects.get_or_create. This wrote
        # the membership row directly and so skipped everything joining an
        # organisation is supposed to do — the person who talked their workplace
        # into starting a comp landed in it with no picks carried across from
        # their other comps and no backdating for rounds already gone, then had
        # to tip a round they had already tipped. That is half of what the
        # client reported on 16 Sep 2026; see tipping.carry.carry_existing_tips.
        #
        # It is still idempotent: the sender may have joined under their own
        # steam while waiting, and add_member's get_or_create covers that the
        # same way, without double-counting them on the ladder.
        before = OrgMember.objects.filter(user=invite.sender, org=org).exists()
        add_member(invite.sender, org, role=OrgMember.ROLE_BOTH)
        created = not before
        invite.status = BossInvite.STATUS_COMPLETE
        invite.completed_at = timezone.now()
        invite.org = org
        invite.save(update_fields=["status", "completed_at", "org"])
        if created:
            added += 1
        logger.info("Boss invite completed: %s joined %s via %s",
                    invite.sender_id, org.id, creator.id)
    return added
