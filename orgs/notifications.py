"""Outbound email for the things that happen in a league.

One module for all of it so the recipient rules live in a single place: who gets
a reminder (only people who haven't voted), who gets round results (only members
with a graded tip), and what URL each message points at.

Every function is best-effort and returns a count. A mail failure must never
roll back the thing that triggered it — an election still opens, a round is
still graded.
"""
from __future__ import annotations

import logging

from django.db.models import Count
from django.utils import timezone

from goodtip.mail import build, send_bulk, site_url

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# URLs. Kept here rather than reverse()d in templates because these go into
# email and must be absolute.
def _vote_url(org_id: int, *, path: str = "") -> str:
    """Absolute URL of a charity vote screen.

    `path` lets a caller that already resolved the route — a group election
    lives at a different one — pass it straight through, rather than this
    helper growing a second hardcoded string that can drift from urls.py.
    """
    return site_url(path or f"/leagues/{org_id}/charity-vote/")


def _dashboard_url(org_id: int) -> str:
    return site_url(f"/dashboard/?org={org_id}")


def _leaderboard_url(org_id: int) -> str:
    return site_url(f"/org/{org_id}/leaderboard/")


# ---------------------------------------------------------------------------
# Invitations
def send_org_invites(org, inviter, emails, join_url: str, message: str = "") -> int:
    """Email a join link to people who aren't in the group yet.

    The link is the same signed token the Copy-link button hands out, so an
    emailed invite and a pasted one are the same thing — there is no second
    kind of invitation to keep working.

    Addresses are not checked against existing accounts on purpose: inviting
    someone who has never heard of GoodTip is the common case, and the join
    view already handles signing up on the way in.
    """
    from django.conf import settings

    inviter_name = (inviter.display_name or inviter.email or "A GoodTip member").strip()
    messages = []
    for email in emails:
        messages.append(build(
            "org_invite",
            subject=f"{inviter_name} invited you to join {org.name} on GoodTip",
            to=email,
            context={
                "org": org,
                "inviter_name": inviter_name,
                "join_url": join_url,
                "message": (message or "").strip(),
                "expires_days": settings.JOIN_LINK_MAX_AGE_DAYS,
            },
            # A reply should reach the person who invited them, not a no-reply
            # box — "who is this and why am I getting it" is the likeliest reply.
            # EmailMultiAlternatives wants a list here, not a bare address.
            reply_to=[inviter.email] if inviter.email else None,
        ))
    return send_bulk(messages)


# ---------------------------------------------------------------------------
# Welcome
def send_welcome(user) -> int:
    if not user.email:
        return 0
    msg = build(
        "welcome",
        subject="Welcome to GoodTip — your tipping just got a purpose",
        to=user.email,
        context={"user": user, "dashboard_url": site_url("/dashboard/")},
    )
    return send_bulk([msg])


# ---------------------------------------------------------------------------
# Charity elections
def _ballot_user_ids(vote) -> set[int]:
    return set(vote.ballots.values_list("user_id", flat=True))


def send_election_reminders(vote, *, urgency: str) -> int:
    """Remind members who still haven't voted. ``urgency`` is "day" or "hour"."""
    if urgency not in ("day", "hour"):
        raise ValueError("urgency must be 'day' or 'hour'")

    voted = _ballot_user_ids(vote)
    options = list(vote.options.select_related("charity"))
    subject = (
        f"Last call — {vote.org.name}'s charity vote closes soon"
        if urgency == "hour"
        else f"Reminder: {vote.org.name}'s charity vote closes tomorrow"
    )

    messages = []
    for m in vote.org.members.select_related("user"):
        user = m.user
        # Nagging someone who already voted is the fastest way to get a
        # transactional sender marked as spam.
        if not user.email or user.id in voted:
            continue
        messages.append(build(
            "election_reminder",
            subject=subject,
            to=user.email,
            context={
                "user": user, "org": vote.org, "vote": vote,
                "options": options, "urgency": urgency,
                "vote_url": _vote_url(vote.org_id),
            },
        ))
    sent = send_bulk(messages)
    stamp = "reminder_hour_sent_at" if urgency == "hour" else "reminder_day_sent_at"
    setattr(vote, stamp, timezone.now())
    vote.save(update_fields=[stamp])
    return sent


def send_election_result(vote) -> int:
    """Tell every member how the vote landed, once it's closed."""
    winner = vote.winning_charity
    if winner is None:
        logger.warning("Election %s has no winning charity — result email skipped.", vote.pk)
        return 0

    tallies = (
        vote.options.select_related("charity")
        .annotate(n=Count("ballots"))
        .order_by("-n", "charity__name")
    )
    results = [
        {"charity": o.charity, "votes": o.n, "is_winner": o.charity_id == winner.id}
        for o in tallies
    ]

    messages = []
    for m in vote.org.members.select_related("user"):
        user = m.user
        if not user.email:
            continue
        messages.append(build(
            "election_result",
            subject=f"{vote.org.name} is raising for {winner.name}",
            to=user.email,
            context={
                "user": user, "org": vote.org, "vote": vote,
                "winner": winner, "results": results,
                "dashboard_url": _dashboard_url(vote.org_id),
            },
        ))
    sent = send_bulk(messages)
    vote.result_email_sent_at = timezone.now()
    vote.save(update_fields=["result_email_sent_at"])
    return sent


# ---------------------------------------------------------------------------
# Round results
def send_round_results(round_obj) -> int:
    """Send each member their own scorecard for a graded round.

    ONE SCORECARD PER ROOM. A tip belongs to the context it was made in, so
    someone who tips in Marketing and in the organisation has two results and
    two positions, and one mail cannot honestly report both. Before this the
    mail was organisation-only: a member who only ever tipped inside a group
    was written to about a ladder they are not on, with a rank they do not
    have, over picks they did not make there.

    Nobody is written to twice about the same room, and nobody is written to at
    all about a room they made no real picks in.
    """
    from .models import Group
    from tipping.models import Tip
    from tipping.services import leaderboard_for_org, user_rank_in_org

    org = round_obj.org
    matches = list(
        round_obj.matches.select_related("home_team", "away_team").order_by("kickoff_at")
    )
    if not matches:
        return 0

    rooms = [None]
    if org.groups_enabled:
        rooms += list(
            Group.objects.filter(org=org, approval_status=Group.APPROVAL_APPROVED)
        )

    messages = []
    for room in rooms:
        messages += _round_result_messages(round_obj, org, room, matches)

    sent = send_bulk(messages)
    round_obj.results_email_sent_at = timezone.now()
    round_obj.save(update_fields=["results_email_sent_at"])
    return sent


def _round_result_messages(round_obj, org, room, matches) -> list:
    """Every scorecard for one room. `room=None` is the organisation itself."""
    from .models import GroupMember
    from tipping.models import Tip
    from tipping.services import leaderboard_for_org, user_rank_in_org

    tips_by_user: dict[int, dict[int, Tip]] = {}
    for tip in (
        Tip.objects.filter(match__in=matches, org=org, group=room)
        .select_related("match")
    ):
        tips_by_user.setdefault(tip.user_id, {})[tip.match_id] = tip

    total_tippers = len(leaderboard_for_org(org, group=room))

    if room is None:
        recipients = [m.user for m in org.members.select_related("user")]
    else:
        recipients = [
            gm.user for gm in
            GroupMember.objects.filter(group=room).select_related("user")
        ]

    messages = []
    for user in recipients:
        user_tips = tips_by_user.get(user.id, {})
        # No tips in this round means nothing to report on.
        #
        # Auto-assigned tips do not count. The missed-tip default gives every
        # member the away side at grading time, so without this check a
        # scorecard would go to somebody who never opened the round — a mail
        # reporting "your picks" over picks the system made on their behalf.
        # Someone who sat a round out gets silence, exactly as before.
        if not user.email or not any(not t.is_auto for t in user_tips.values()):
            continue

        rows, correct, graded, points = [], 0, 0, 0
        for match in matches:
            tip = user_tips.get(match.id)
            picked_name = ""
            if tip:
                picked_name = (
                    match.home_team.name if tip.selection == "home" else match.away_team.name
                )
                if tip.is_correct is not None:
                    graded += 1
                    if tip.is_correct:
                        correct += 1
                        points += tip.points_awarded
            rows.append({
                "match": match, "tip": tip, "picked_name": picked_name,
                "correct": bool(tip and tip.is_correct),
                "points": tip.points_awarded if tip else 0,
            })

        if not graded:
            continue

        # The room is named in the subject when there is one, because the
        # alternative is two mails an hour apart with identical subjects and
        # different numbers inside them.
        where = f" in {room.name}" if room is not None else ""
        messages.append(build(
            "tip_results",
            subject=(
                f"Round {round_obj.round_number}{where}: you got {correct} of {graded}"
            ),
            to=user.email,
            context={
                "user": user, "org": org, "group": room,
                "round": round_obj, "rows": rows,
                "correct": correct, "graded": graded, "round_points": points,
                "rank": user_rank_in_org(user, org, group=room),
                "total_tippers": total_tippers,
                "leaderboard_url": _leaderboard_url(org.id),
            },
        ))
    return messages


# ---------------------------------------------------------------------------
# News
def send_news_published(post, recipients) -> int:
    """Announce a published post. ``recipients`` is an iterable of users."""
    post_url = site_url(f"/news/{post.slug}/")
    image_url = ""
    if getattr(post, "image", None):
        try:
            image_url = site_url(post.image.url)
        except ValueError:
            image_url = ""

    messages = []
    for user in recipients:
        if not getattr(user, "email", ""):
            continue
        messages.append(build(
            "news_published",
            subject=post.title,
            to=user.email,
            context={
                "user": user, "post": post, "post_url": post_url,
                "post_image_url": image_url,
                "prefs_url": site_url("/profile/"),
            },
        ))
    return send_bulk(messages)
