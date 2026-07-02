import logging

from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction
from django.db.models import Count
from django.utils import timezone

from .models import (
    CharityVote,
    CharityVoteBallot,
    CharityVoteOption,
    OrgCharitySelection,
    OrgMember,
)

logger = logging.getLogger(__name__)


def notify_charity_suggestion(charity, org, user) -> None:
    """Email the GoodTip team that a league suggested an unlisted charity (deck slide 10).

    Best-effort: a mail failure must never block league creation.
    """
    try:
        send_mail(
            subject=f"[GoodTip] Charity suggested for review: {charity.name}",
            message=(
                f"{user.display_name} ({user.email}) created the league "
                f"\"{org.name}\" and suggested a charity for approval:\n\n"
                f"  Name: {charity.name}\n"
                f"  Website: {charity.website or '—'}\n"
                f"  Slug: {charity.slug}\n\n"
                "Review it in the admin and set is_approved once verified."
            ),
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[settings.GOODTIP_TEAM_EMAIL],
            fail_silently=True,
        )
    except Exception:  # noqa: BLE001 — never let a mail error break signup
        logger.exception("Failed to send charity-suggestion notification")


def _resolve_inviter(org, inviter_id, joining_user):
    """Return the inviting User, or None if invalid.

    Ignores self-invites and inviters who aren't themselves members of the org.
    """
    if not inviter_id or inviter_id == joining_user.id:
        return None
    inviter_membership = (
        OrgMember.objects.filter(org=org, user_id=inviter_id)
        .select_related("user")
        .first()
    )
    return inviter_membership.user if inviter_membership else None


def add_member(user, org, *, inviter_id=None, role=OrgMember.ROLE_PARTICIPANT) -> OrgMember:
    """Add a user to an org, recording who referred them (if known)."""
    inviter = _resolve_inviter(org, inviter_id, user)
    member, created = OrgMember.objects.get_or_create(
        user=user, org=org, defaults={"role": role, "invited_by": inviter},
    )
    # Backfill the referrer if they joined before we tracked it.
    if not created and member.invited_by_id is None and inviter is not None:
        member.invited_by = inviter
        member.save(update_fields=["invited_by"])
    return member


def set_member_role(member: OrgMember, role: str) -> OrgMember:
    """Set a member's base role (manager/captain/both/participant)."""
    valid = {choice for choice, _ in OrgMember.ROLE_CHOICES}
    if role not in valid:
        raise ValueError(f"Invalid role: {role}")
    member.role = role
    member.save(update_fields=["role"])
    return member


def nominate_manager_by_email(org, email: str):
    """Make an existing member a Team Manager by email (deck: owner nominates a manager).

    Returns the updated OrgMember, or None if no member with that email exists yet.
    """
    from accounts.models import User

    email = (email or "").strip().lower()
    if not email:
        return None
    user = User.objects.filter(email__iexact=email).first()
    if user is None:
        return None
    member = OrgMember.objects.filter(org=org, user=user).first()
    if member is None:
        return None
    # Preserve a captain hat if they already had one.
    new_role = OrgMember.ROLE_BOTH if member.is_captain else OrgMember.ROLE_MANAGER
    return set_member_role(member, new_role)


def record_charity_selection(org, charity, *, source=OrgCharitySelection.SOURCE_MANUAL):
    """Append a timeline row for the charity an org is backing this season.

    No-op when there's no charity, or when it matches the most recent selection
    (so re-saving the same choice doesn't create duplicate history).
    """
    if charity is None:
        return None
    latest = org.charity_selections.first()
    if latest and latest.charity_id == charity.id:
        return latest
    return OrgCharitySelection.objects.create(
        org=org, season=org.season, charity=charity, source=source,
    )


@transaction.atomic
def set_org_charity(org, charity, *, source=OrgCharitySelection.SOURCE_MANUAL):
    """Set the org's current charity and append it to the timeline.

    Past donations keep the charity frozen on their own payment rows, so changing
    the charity here never rewrites history.
    """
    org.charity = charity
    org.save(update_fields=["charity"])
    return record_charity_selection(org, charity, source=source)


@transaction.atomic
def open_charity_vote(org, charities) -> CharityVote:
    """Open a blind charity vote for an org, seeded with candidate charities."""
    vote = CharityVote.objects.create(org=org, status="open")
    for charity in charities:
        CharityVoteOption.objects.create(vote=vote, charity=charity)
    return vote


def cast_charity_ballot(*, user, vote: CharityVote, option: CharityVoteOption) -> CharityVoteBallot:
    """Record (or change) a user's blind ballot. Raises ValueError if invalid."""
    if not vote.is_open:
        raise ValueError("This charity vote has closed.")
    if option.vote_id != vote.id:
        raise ValueError("That option isn't part of this vote.")
    ballot, _ = CharityVoteBallot.objects.update_or_create(
        vote=vote, user=user, defaults={"option": option},
    )
    return ballot


@transaction.atomic
def close_charity_vote(vote: CharityVote):
    """Tally a vote, set the winning charity on the org, and mark it closed.

    Ties are broken by the option with the most votes, then alphabetically by
    charity name (CharityVoteOption default ordering).
    """
    if not vote.is_open:
        return vote.winning_charity
    tally = vote.options.annotate(n=Count("ballots")).order_by("-n")
    top = tally.first()
    winner = top.charity if top else None
    vote.winning_charity = winner
    vote.status = "closed"
    vote.closed_at = timezone.now()
    vote.save(update_fields=["winning_charity", "status", "closed_at"])
    if winner is not None:
        set_org_charity(vote.org, winner, source=OrgCharitySelection.SOURCE_VOTE)
    return winner
