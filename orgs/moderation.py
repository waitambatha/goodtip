"""What happens around a flag: raising one, telling somebody, and enforcing.

The reading itself is orgs/prefect.py. This is the part with consequences, and
it is kept separate because the two change for different reasons — the reader
will be replaced by a language model, while who gets told and what they may do
about it is a decision about the product.

THE ORDER OF EVENTS IS DELIBERATE. A message is saved, delivered and read
normally; Prefect looks at it afterwards and, at most, asks a person to look
too. Nothing is held back for review, because a moderator standing between
somebody and the room they are typing into is a different product from the one
that was asked for — and because a false positive would then be censorship
rather than a notification somebody dismisses.
"""
from __future__ import annotations

import logging

from django.db.models import Q
from django.utils import timezone

logger = logging.getLogger(__name__)


def reviewers_for(org, group=None):
    """Who sees a flag: the people who run the organisation, and its captains.

    "The prefect reports it to the organisation admin or captain — so the
    captain will see the content." Captains are in the list because they are the
    people actually in the rooms; admins because they are the ones who can
    suspend anybody.

    Scoped to the group when the message was in one, plus the organisation's
    admins either way: a group captain should handle their own room, and an
    admin should not have to be in every group to hear about it.
    """
    from .models import GroupMember, OrgMember

    members = OrgMember.objects.filter(org=org).select_related("user")
    people = {m.user for m in members if m.can_manage}
    for m in members:
        if not m.is_captain:
            continue
        if group is None:
            people.add(m.user)
        elif GroupMember.objects.filter(group=group, user=m.user).exists():
            people.add(m.user)
    return people


def allowed_phrases(org) -> set[str]:
    from .models import PrefectAllowance

    return set(
        PrefectAllowance.objects.filter(org=org).values_list("phrase", flat=True)
    )


def review_message(entry):
    """Prefect's pass over one message. Returns the flag it raised, or None.

    Called after the message is saved and delivered. Swallows its own failures:
    a chat that cannot accept a message because the moderator fell over is a
    worse product than one that missed something.
    """
    from .models import ChatFlag
    from .prefect import classify

    try:
        thread = entry.thread
        org = thread.org
        if org is None:
            return None
        verdict = classify(entry.body, {"allowed_phrases": allowed_phrases(org)})
        if not verdict.reportable:
            return None
        flag = ChatFlag.objects.create(
            org=org, message=entry, author=entry.author,
            category=verdict.category, score=verdict.score,
            terms=verdict.terms, reason=verdict.reason,
        )
        notify_reviewers(flag, thread.group)
        return flag
    except Exception:  # noqa: BLE001 — see the docstring
        logger.exception("Prefect could not review message %s", getattr(entry, "id", "?"))
        return None


def report_message(entry, reporter, note=""):
    """A member raising one about somebody else. Same queue, different source.

    Refuses to raise a second open flag on the same message from the same
    person: a report button that can be pressed ten times is a way to make a
    queue useless, and the reviewer only needs to be asked once.
    """
    from .models import ChatFlag

    thread = entry.thread
    org = thread.org
    existing = ChatFlag.objects.filter(
        message=entry, raised_by=reporter, status=ChatFlag.STATUS_OPEN,
    ).first()
    if existing is not None:
        return existing
    flag = ChatFlag.objects.create(
        org=org, message=entry, author=entry.author, raised_by=reporter,
        category="member", score=0, note=(note or "").strip()[:2000],
    )
    notify_reviewers(flag, thread.group)
    return flag


def notify_reviewers(flag, group=None):
    """The inbox item, with the link that opens the review.

    "It will come as an inbox but a clickable link to check, review and flag it
    as not an issue."

    The notification says WHAT KIND of thing it is and never quotes the message.
    A one-line teaser of the exact words that were reported would put them on
    the bell of everybody who runs the organisation — which is publishing them
    further, not handling them.
    """
    from django.urls import reverse

    from .models import Notification

    who = flag.raised_by.display_name if flag.raised_by else "Prefect"
    title = (
        f"{who} reported a message"
        if flag.raised_by
        else f"Prefect flagged a message ({flag.get_category_display() if hasattr(flag, 'get_category_display') else flag.category})"
    )
    link = reverse("manage:prefect_flag", args=[flag.id])
    made = 0
    for person in reviewers_for(flag.org, group):
        if person == flag.author and flag.raised_by is None:
            # Telling somebody Prefect flagged their own message, before any
            # human has looked at it, is an accusation from a word list.
            continue
        Notification.objects.create(
            user=person, org=flag.org, kind=Notification.KIND_ADMIN_NOTE,
            title=title,
            message="Open it to read the message in context and decide what, if anything, to do.",
            link_url=link,
        )
        made += 1
    return made


# ---------------------------------------------------------------------------
# Enforcement
# ---------------------------------------------------------------------------

def active_suspension(user, org, group=None):
    """The suspension stopping this person posting here, if there is one.

    An organisation-wide suspension covers every room in it; a group one covers
    that group only. Checked in that order because the wider one is the one that
    should be reported back to the person — "you are suspended from the comp"
    rather than "you are suspended from Fleet & Logistics", when both are true.
    """
    from .models import MemberSanction

    now = timezone.now()
    qs = MemberSanction.objects.filter(
        org=org, user=user, kind=MemberSanction.KIND_SUSPENSION,
        lifted_at__isnull=True, starts_at__lte=now,
    ).filter(Q(ends_at__isnull=True) | Q(ends_at__gt=now))
    org_wide = qs.filter(group__isnull=True).first()
    if org_wide is not None:
        return org_wide
    if group is not None:
        return qs.filter(group=group).first()
    return None


def may_post(user, thread) -> tuple[bool, str]:
    """Whether this person may write in this room, and what to tell them.

    Returns a sentence rather than a bare False: somebody who cannot post is
    owed the reason and the date, and a composer that simply stops working is
    the most frustrating possible way to serve a suspension.
    """
    org = thread.org
    if org is None:
        return True, ""
    sanction = active_suspension(user, org, getattr(thread, "group", None))
    if sanction is None:
        return True, ""
    where = sanction.group.name if sanction.group_id else org.name
    if sanction.ends_at is None:
        return False, f"You can't post in {where} at the moment. An admin has paused it."
    return False, (
        f"You can't post in {where} until "
        f"{timezone.localtime(sanction.ends_at):%-d %B}."
    )
