import logging
import sys
from pathlib import Path

from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction
from django.db.models import Count, Q
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify

from catalog.models import Charity

from .models import (
    CharityVote,
    CharityVoteBallot,
    CharityVoteOption,
    MembershipRequest,
    OrgCharitySelection,
    OrgMember,
)

logger = logging.getLogger(__name__)


def unique_charity_slug(name: str) -> str:
    from catalog.models import Charity

    base = slugify(name)[:200] or "charity"
    slug = base
    i = 2
    while Charity.objects.filter(slug=slug).exists():
        slug = f"{base}-{i}"
        i += 1
    return slug


def notify_charity_suggestion(charity, org, user=None) -> None:
    """Email the GoodTip team that an organisation added an unlisted charity.

    Best-effort: a mail failure must never block the admin's actual task.

    `user` is optional because the callers changed. This used to fire only
    from the creation wizard, where there was always a signed-in creator;
    it now also fires from Manage → Charities and from scripted/seeded adds
    that have no user at all, and an AttributeError here would be swallowed
    into a log line saying only that the review email failed.
    """
    who = (
        f"{user.display_name} ({user.email})"
        if user is not None else "Someone at the organisation"
    )
    try:
        send_mail(
            subject=f"[GoodTip] Charity suggested for review: {charity.name}",
            message=(
                f"{who} added a charity to "
                f"\"{org.name}\" that isn't on the approved list:\n\n"
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
    from tipping.services import backdate_missed_tips

    inviter = _resolve_inviter(org, inviter_id, user)
    member, created = OrgMember.objects.get_or_create(
        user=user, org=org, defaults={"role": role, "invited_by": inviter},
    )
    # Backfill the referrer if they joined before we tracked it.
    if not created and member.invited_by_id is None and inviter is not None:
        member.invited_by = inviter
        member.save(update_fields=["invited_by"])
    if created:
        # A mid-season joiner starts on the away side for every round that is
        # already gone, rather than on nothing. See backdate_missed_tips for
        # why this cannot be left to the grading-time default.
        #
        # Only on the join itself: it is idempotent, but re-running it on
        # every call would be a full-season scan on each of these.
        backdate_missed_tips(user, org)
    return member


def notify(users, *, kind, title, message="", link_url="", org=None) -> int:
    """Drop an in-app notification in one or more people's bell panel.

    The email side of a process event and the in-app side had drifted: joining
    emailed the admins and told the requester nothing, so after "Ask to join"
    the app went quiet until an admin happened to act. Everything that emails
    about a process now writes here too, through this one call.

    Best-effort by design — a notification is a courtesy, and failing to write
    one must never roll back the join or the vote that triggered it.
    """
    from .models import Notification

    users = [u for u in users if u is not None]
    if not users:
        return 0
    try:
        rows = Notification.objects.bulk_create([
            Notification(
                user=u, org=org, kind=kind,
                title=title, message=message, link_url=link_url,
            )
            for u in users
        ])
        return len(rows)
    except Exception:  # noqa: BLE001 — never let the bell panel break the action
        logger.exception("Could not write %r notification for %d user(s)", kind, len(users))
        return 0


def org_admin_users(org):
    """Everyone who can act on an admin decision for this org."""
    from django.db.models import Q

    return [
        m.user
        for m in OrgMember.objects.filter(org=org)
        .filter(Q(role__in=[OrgMember.ROLE_MANAGER, OrgMember.ROLE_BOTH]) | Q(is_league_owner=True))
        .select_related("user")
    ]


def _notify_join_request(req: MembershipRequest) -> None:
    """Email the org's managers that someone asked to join (best-effort:
    a mail failure must never block the request itself)."""
    from django.db.models import Q

    manager_emails = list(
        OrgMember.objects.filter(org=req.org)
        .filter(Q(role__in=[OrgMember.ROLE_MANAGER, OrgMember.ROLE_BOTH]) | Q(is_league_owner=True))
        .values_list("user__email", flat=True)
    )
    if not manager_emails:
        return
    try:
        send_mail(
            subject=f"[GoodTip] {req.user.display_name} wants to join {req.org.name}",
            message=(
                f"{req.user.display_name} ({req.user.email}) has asked to join "
                f"\"{req.org.name}\".\n\n"
                "Approve or decline the request from your Members page."
            ),
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=manager_emails,
            fail_silently=True,
        )
    except Exception:  # noqa: BLE001 — never let a mail error break the request
        logger.exception("Failed to send join-request notification")


def request_to_join(user, org) -> MembershipRequest:
    """A user asks to join an org they found via search (org-structure §2,
    client amendment: the org's admin must approve). Idempotent: asking again
    while a request is pending returns the existing one; a declined user may
    ask again (new pending row).
    """
    if OrgMember.objects.filter(user=user, org=org).exists():
        raise ValueError("You're already a member of this organisation.")
    existing = MembershipRequest.objects.filter(
        user=user, org=org, status=MembershipRequest.STATUS_PENDING,
    ).first()
    if existing:
        return existing
    req = MembershipRequest.objects.create(user=user, org=org)
    _notify_join_request(req)
    _notify_join_request_in_app(req)
    return req


def _notify_join_request_in_app(req: MembershipRequest) -> None:
    """Both sides of a pending request: the asker sees they're in the queue,
    the admins see there's something waiting on them."""
    from .models import Notification

    notify(
        [req.user], org=req.org,
        kind=Notification.KIND_JOIN_REQUESTED,
        title=f"Request sent to {req.org.name}",
        message=(
            f"You've asked to join {req.org.name}. An admin there reviews every "
            "request, so it may take a little while. We'll let you know here as "
            "soon as they've had a look — once you're in you can tip each round, "
            "climb the ladder and help pick the charity your organisation raises for."
        ),
        link_url="/dashboard/",
    )
    notify(
        org_admin_users(req.org), org=req.org,
        kind=Notification.KIND_JOIN_REVIEW,
        title=f"{req.user.display_name} wants to join",
        message=(
            f"{req.user.display_name} has asked to join {req.org.name}. "
            "Open this to see them and approve or decline — they're waiting to hear back."
        ),
        # Straight to the one request, not the Members page: from a notification
        # the admin should land on the person, with the buttons already there.
        link_url=reverse("orgs:review_request", args=[req.org_id, req.id]),
    )


@transaction.atomic
def approve_membership_request(req: MembershipRequest, *, by_user) -> OrgMember:
    """Admin approves a join request: the requester becomes a participant.

    Raises ValueError if the request was already decided.
    """
    if not req.is_pending:
        raise ValueError("This request has already been decided.")
    req.status = MembershipRequest.STATUS_APPROVED
    req.decided_at = timezone.now()
    req.decided_by = by_user
    req.save(update_fields=["status", "decided_at", "decided_by"])
    member = add_member(req.user, req.org)
    from .models import Notification

    notify(
        [req.user], org=req.org,
        kind=Notification.KIND_JOIN_APPROVED,
        title=f"You're in — welcome to {req.org.name}",
        message=(
            f"An admin approved your request to join {req.org.name}. Your tips "
            "count from the next round, and you'll get a say when the group votes "
            "on the charity it raises for. Head to your dashboard to get started."
        ),
        link_url="/dashboard/",
    )
    return member


def decline_membership_request(req: MembershipRequest, *, by_user) -> MembershipRequest:
    """Admin declines a join request. The user may request again later."""
    if not req.is_pending:
        raise ValueError("This request has already been decided.")
    req.status = MembershipRequest.STATUS_DECLINED
    req.decided_at = timezone.now()
    req.decided_by = by_user
    req.save(update_fields=["status", "decided_at", "decided_by"])
    from .models import Notification

    # Deliberately gentle and non-final: a decline is often "wrong group" or
    # "we don't know you yet", and the model allows asking again.
    notify(
        [req.user], org=req.org,
        kind=Notification.KIND_JOIN_DECLINED,
        title=f"Your request to join {req.org.name} wasn't approved",
        message=(
            f"An admin at {req.org.name} didn't approve this one. If you think "
            "it's a mix-up, have a word with them and you're welcome to ask "
            "again — or find another group, or start your own."
        ),
        link_url=reverse("orgs:search"),
    )
    return req


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


def demote_child_org_admin(member: OrgMember) -> OrgMember:
    """Parent-admin action (org-structure §6): strip a child org's admin back
    to participant. They stay a member — only the admin hats come off."""
    member.role = OrgMember.ROLE_PARTICIPANT
    member.is_league_owner = False
    member.save(update_fields=["role", "is_league_owner"])
    return member


@transaction.atomic
def reassign_child_org_admin(child, email: str, *, by_user):
    """Parent-admin action (org-structure §6): hand a child org's admin role
    to a user by email. §6's rationale is a closed-down location: the parent
    must be able to step in, so the target needn't already be a member — an
    existing GoodTip user (including the parent admin) is added if needed.
    Returns the admin OrgMember, or None if no user has that email.
    """
    from accounts.models import User

    email = (email or "").strip().lower()
    if not email:
        return None
    user = User.objects.filter(email__iexact=email).first()
    if user is None:
        return None
    member, _ = OrgMember.objects.get_or_create(user=user, org=child)
    member.role = OrgMember.ROLE_BOTH
    member.is_league_owner = True
    member.save(update_fields=["role", "is_league_owner"])
    return member


def record_charity_selection(org, charity, *, source=OrgCharitySelection.SOURCE_MANUAL,
                             group=None):
    """Append a timeline row for the charity a room is backing this season.

    No-op when there's no charity, or when it matches the most recent selection
    for that same room (so re-saving the same choice doesn't create duplicate
    history). `group=None` is the organisation's own timeline; a group's rows
    are kept separate, or a franchise changing its local cause would read as
    head office changing theirs.
    """
    if charity is None:
        return None
    latest = org.charity_selections.filter(group=group).first()
    if latest and latest.charity_id == charity.id:
        return latest
    return OrgCharitySelection.objects.create(
        org=org, group=group, season=org.season, charity=charity, source=source,
    )


@transaction.atomic
def set_group_charity(group, charity, *, source=OrgCharitySelection.SOURCE_MANUAL):
    """Point one group at its own cause, and append it to the timeline.

    Clearing it (charity=None) is meaningful and supported: the group goes
    back to backing whatever its organisation backs. See
    Group.effective_charity.
    """
    group.charity = charity
    group.save(update_fields=["charity"])
    return record_charity_selection(group.org, charity, source=source, group=group)


@transaction.atomic
def add_charity_for_org(org, *, name, website="", by_user=None):
    """Add a charity an organisation wants that GoodTip's list doesn't have.

    Usable by that organisation the moment it exists — the admin is mid-task
    and being told to wait for approval is where the old wizard lost people —
    but `is_approved` stays False, so it does not surface in any other
    organisation's picker until GoodTip has looked at it.

    An existing charity with the same name is RETURNED rather than duplicated,
    whoever owns it. Near-duplicate charity rows are the specific damage this
    whole ownership model exists to stop, and "Beyond Blue" typed by two
    different admins must not become two rows.
    """
    name = (name or "").strip()
    if not name:
        raise ValueError("Give the charity a name.")
    existing = Charity.objects.filter(name__iexact=name).first()
    if existing is not None:
        return existing
    charity = Charity.objects.create(
        name=name,
        slug=unique_charity_slug(name),
        website=(website or "").strip(),
        is_approved=False,
        owner_org=org,
        added_by=by_user,
        added_at=timezone.now(),
    )
    notify_charity_suggestion(charity, org, by_user)
    # Fire-and-forget: a charity card renders fine on initials, and the
    # admin must not wait on someone else's web server.
    from catalog.logos import backfill_in_background
    backfill_in_background(charity)
    return charity


def charities_for(org):
    """The charity picker for one organisation: vetted list plus its own."""
    return Charity.objects.available_to(org).order_by("name")


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
    """Tally a vote and either lock in the winner or hand it to a captain.

    A SHARED TOP COUNT IS NOT A RESULT. This used to take
    ``.order_by("-n").first()``, which on a tie returns whichever row the
    database felt like returning first — an arbitrary charity, presented to
    the whole organisation as its decision. Ties are not an edge case here:
    an even split between two charities is the expected outcome for a group
    with an even number of people, and a vote nobody turned up to ties every
    option at nil.

    So a tie stops. The vote moves to ``tied``, no charity is set, nothing is
    announced as a result, and the people who can break it are asked to. See
    ``break_charity_vote_tie``.

    Returns the winning charity, or None when the vote is now tied or had no
    options at all.
    """
    if not vote.is_open:
        return vote.winning_charity
    tally = list(
        vote.options.select_related("charity")
        .annotate(n=Count("ballots"))
        .order_by("-n", "charity__name")
    )
    from .models import Notification

    if not tally:
        # Nothing was ever on the ballot. There is no tie to break and no
        # winner to find, so this closes with no result, as it always did.
        vote.status = CharityVote.STATUS_CLOSED
        vote.closed_at = timezone.now()
        vote.save(update_fields=["status", "closed_at"])
        notify(
            [m.user for m in vote.org.members.select_related("user")], org=vote.org,
            kind=Notification.KIND_ELECTION_RESULT,
            title=f"Charity vote closed — {vote.org.name}",
            message=(
                "The vote has closed without a result, so no charity was chosen "
                "this time. Your admin can start another one whenever the "
                "group's ready."
            ),
            link_url=reverse("orgs:charity_vote", args=[vote.org_id]),
        )
        return None

    top = tally[0].n
    tied = [o for o in tally if o.n == top]
    if len(tied) > 1:
        vote.status = CharityVote.STATUS_TIED
        vote.closed_at = timezone.now()
        vote.save(update_fields=["status", "closed_at"])
        names = _readable_list([o.charity.name for o in tied])
        # Everyone hears that voting is over and why there is no winner yet —
        # silence here is what made the old screen look like it was still
        # loading. Only the people who can act are told to act.
        deciders = {m.user_id for m in vote.resolve_by()}
        for member in vote.org.members.select_related("user"):
            can_act = member.user_id in deciders
            notify(
                [member.user], org=vote.org,
                kind=Notification.KIND_ELECTION_TIED,
                title=f"Your charity vote is tied — {vote.org.name}",
                message=(
                    f"{names} finished level on {top} vote{'s' if top != 1 else ''}. "
                    + ("It's your call: pick the one that goes through."
                       if can_act else
                       "Your captain will make the call and you'll hear as soon "
                       "as they have.")
                ),
                link_url=reverse("orgs:charity_vote", args=[vote.org_id]),
            )
        return None

    return _finish_charity_vote(vote, tally[0].charity)


def _readable_list(names) -> str:
    """"A, B and C" — for a sentence, not a bulleted list."""
    names = list(names)
    if len(names) <= 1:
        return names[0] if names else ""
    return f"{', '.join(names[:-1])} and {names[-1]}"


@transaction.atomic
def _finish_charity_vote(vote: CharityVote, winner, *, by_user=None):
    """Lock in ``winner``, tell everyone, and mark the vote closed.

    Shared by the ordinary path (one option had the most votes) and the
    captain's call, so a tie-broken vote lands in exactly the same state as a
    clean one — same charity record, same selection history, same
    announcement. The only difference is that the announcement says who made
    the call.
    """
    from .models import Notification

    vote.winning_charity = winner
    vote.status = CharityVote.STATUS_CLOSED
    if vote.closed_at is None:
        vote.closed_at = timezone.now()
    fields = ["winning_charity", "status", "closed_at"]
    if by_user is not None:
        vote.tie_broken_by = by_user
        vote.tie_broken_at = timezone.now()
        fields += ["tie_broken_by", "tie_broken_at"]
    vote.save(update_fields=fields)
    if vote.group_id:
        set_group_charity(vote.group, winner, source=OrgCharitySelection.SOURCE_VOTE)
    else:
        set_org_charity(vote.org, winner, source=OrgCharitySelection.SOURCE_VOTE)

    # A group election is the group's news, not the whole company's, and the
    # wording has to match: "your group" is what the people in it recognise.
    room = vote.room_label
    unit = "group" if vote.group_id else "organisation"
    if by_user is not None:
        who = getattr(by_user, "display_name", "") or by_user.email
        title = f"{winner.name} takes it — captain's call"
        body = (
            f"Your charity vote finished level, so {who} made the call: "
            f"{room} is backing {winner.name}. Everything your "
            f"{unit} raises from here goes to them."
        )
    else:
        title = f"{winner.name} won your charity vote"
        body = (
            f"{room} has chosen {winner.name}. Everything your {unit} "
            "raises from here goes to them — thanks for having your say."
        )
    if vote.group_id:
        audience = [m.user for m in vote.group.memberships.select_related("user")]
        link = reverse("orgs:group_charity_vote", args=[vote.org_id, vote.group_id])
    else:
        audience = [m.user for m in vote.org.members.select_related("user")]
        link = reverse("orgs:charity_vote", args=[vote.org_id])
    notify(
        audience, org=vote.org,
        kind=Notification.KIND_ELECTION_RESULT,
        title=title, message=body,
        link_url=link,
    )
    return winner


def can_break_charity_vote_tie(user, vote: CharityVote) -> bool:
    """Captains, managers and the league owner. See CharityVote.resolve_by."""
    if not getattr(user, "is_authenticated", False) or not vote.is_tied:
        return False
    return vote.resolve_by().filter(user=user).exists()


@transaction.atomic
def break_charity_vote_tie(vote: CharityVote, charity, *, by_user):
    """The captain's call: pick which of the tied charities goes through.

    Restricted to the options that ACTUALLY tied. A captain resolving a
    deadlock is breaking a tie, not overriding the vote — letting them reach
    for an option that came third would make the whole election advisory.
    """
    if not vote.is_tied:
        raise ValueError("That vote isn't tied.")
    if not can_break_charity_vote_tie(by_user, vote):
        raise ValueError("Only a captain or manager can make that call.")
    tied_ids = {o.charity_id for o in vote.tied_options()}
    if charity is None or charity.pk not in tied_ids:
        raise ValueError("Pick one of the charities that tied.")
    return _finish_charity_vote(vote, charity, by_user=by_user)


def can_lock_fundraising(org) -> bool:
    """Charity Partner Workflow (categories doc): only a Charities-type org
    whose partner flag GoodTip staff have set in admin may lock fundraising
    to itself. The flag is never self-declared.
    """
    return bool(org.organisation_type_id and org.organisation_type.is_charity_type and org.is_charity_partner)


def is_creator_admin(user, org, *, membership=None) -> bool:
    """Whether `user` may use org's owner-only Manage surfaces (Members,
    Settings, Groups-admin, Season summary, Charity election).

    Scoped to Organisation.created_by rather than OrgMember.can_manage's
    per-membership role: a manager invited into someone else's org should
    not gain control of *that org's* admin surfaces — only the org's own
    creator should. Orgs recorded before `created_by` existed (created_by is
    None) grandfather every existing can_manage member, so nobody running a
    legacy org is locked out by a field that was never set on their row.
    """
    if org is None or not user.is_authenticated:
        return False
    m = membership if membership is not None else OrgMember.objects.filter(user=user, org=org).first()
    if m is None or not m.can_manage:
        return False
    if org.created_by_id is None:
        return True
    return org.created_by_id == user.id


@transaction.atomic
def lock_fundraising_to_self(org):
    """Point the org's fundraising at itself — no participant vote required.

    Creates (or reuses) a Charity record carrying the org's own name and makes
    it the org's charity. Any open vote is closed unresolved: the lock
    supersedes it. The org's own Charity record stays ``is_approved=False`` so
    it doesn't enter every other league's public picker.
    """
    from catalog.models import Charity

    if not can_lock_fundraising(org):
        raise ValueError("Only confirmed GoodTip Partner Charities can lock fundraising to themselves.")
    vote = org.active_charity_vote
    if vote is not None:
        vote.status = "closed"
        vote.closed_at = timezone.now()
        vote.save(update_fields=["status", "closed_at"])
    charity = Charity.objects.filter(name__iexact=org.name).first()
    if charity is None:
        charity = Charity.objects.create(
            name=org.name, slug=unique_charity_slug(org.name), is_approved=False,
        )
    set_org_charity(org, charity, source=OrgCharitySelection.SOURCE_SELF)
    return charity


# --------------------------------------------------------------------------
# Charity elections — scheduled votes with member notification (client ask,
# Jul 2026): creating a member-vote comp no longer opens the vote silently.
# The admin schedules the election (or starts it now); when it opens, every
# member gets a branded email plus an in-app popup + notification entry.
# --------------------------------------------------------------------------

@transaction.atomic
def create_charity_election(org, charities) -> CharityVote:
    """Create the election in draft, seeded with candidate charities.

    Nothing is announced yet — the admin schedules it from their dashboard.
    """
    vote = CharityVote.objects.create(org=org, status=CharityVote.STATUS_DRAFT)
    for charity in charities:
        CharityVoteOption.objects.create(vote=vote, charity=charity)
    return vote


@transaction.atomic
def create_group_charity_election(group, charities) -> CharityVote:
    """Start a charity election inside one group.

    Options are restricted to what the group's ORGANISATION has made available
    — the vetted list plus anything the org added. A group cannot invent a
    charity: the org is the one that adds them, so a franchise wanting a local
    cause on the ballot asks head office to add it once, and every store can
    then vote for it.

    Refuses to run a second election while one is live, because two open
    ballots for the same group's charity have no defined winner.
    """
    charities = list(charities)
    if len(charities) < 2:
        raise ValueError("Put at least two charities on the ballot.")
    allowed = set(
        Charity.objects.available_to(group.org)
        .filter(pk__in=[c.pk for c in charities])
        .values_list("pk", flat=True)
    )
    rejected = [c for c in charities if c.pk not in allowed]
    if rejected:
        raise ValueError(
            f"{rejected[0].name} isn't one of your organisation's charities."
        )
    live = group.charity_votes.filter(
        status__in=(
            CharityVote.STATUS_DRAFT, CharityVote.STATUS_SCHEDULED,
            CharityVote.STATUS_OPEN, CharityVote.STATUS_TIED,
        )
    ).first()
    if live is not None:
        raise ValueError(f"{group.name} already has a charity vote on the go.")
    vote = CharityVote.objects.create(
        org=group.org, group=group, status=CharityVote.STATUS_DRAFT,
    )
    for charity in charities:
        CharityVoteOption.objects.create(vote=vote, charity=charity)
    return vote


def group_charity_vote(group):
    """The group's current or most recent election, or None."""
    return group.charity_votes.first()


def can_run_group_election(user, group) -> bool:
    """Who may open or close a group's charity vote.

    The group's own admins, plus the organisation's admins — a group whose
    admin has left must not be stuck with no way to run an election, and an
    org admin is already able to do everything else to its groups.
    """
    if not getattr(user, "is_authenticated", False):
        return False
    if is_creator_admin(user, group.org):
        return True
    return group.memberships.filter(user=user, is_admin=True).exists()


def vote_electorate(vote: CharityVote):
    """Who votes in this election, and where its screen lives.

    One resolver rather than a `if vote.group_id` at every notification site:
    the group election was added late, and the failure mode of scattering that
    branch is a group's ballot emailed to the entire company — which is both a
    privacy problem and the fastest way to make people stop reading GoodTip's
    mail. Returns (members, link, room_label) where `members` are OrgMember or
    GroupMember rows, each with `.user`.
    """
    if vote.group_id:
        return (
            list(vote.group.memberships.select_related("user")),
            reverse("orgs:group_charity_vote", args=[vote.org_id, vote.group_id]),
            vote.group.name,
        )
    return (
        list(vote.org.members.select_related("user")),
        reverse("orgs:charity_vote", args=[vote.org_id]),
        vote.org.name,
    )


def schedule_charity_election(vote: CharityVote, *, when, close_at=None, message="") -> CharityVote:
    """Schedule the election to open at `when` (or open immediately if due).

    `close_at` optionally sets an automatic end time; the admin can still
    close the vote manually at any point while it's open.
    """
    if vote.status not in (CharityVote.STATUS_DRAFT, CharityVote.STATUS_SCHEDULED):
        raise ValueError("This election has already opened.")
    if close_at is not None and close_at <= when:
        raise ValueError("The election must close after it opens.")
    vote.admin_message = (message or "").strip()
    vote.scheduled_open_at = when
    vote.scheduled_close_at = close_at
    vote.status = CharityVote.STATUS_SCHEDULED
    vote.save(update_fields=["admin_message", "scheduled_open_at", "scheduled_close_at", "status"])
    if when <= timezone.now():
        open_charity_election(vote)
        return vote

    # Only worth announcing when it's genuinely in the future — an election that
    # opens immediately sends its own "vote now" notification from
    # open_charity_election, and both would land in the same second.
    from django.utils import formats

    from .models import Notification

    opens = formats.date_format(timezone.localtime(when), "DATETIME_FORMAT")
    closes = (
        formats.date_format(timezone.localtime(close_at), "DATETIME_FORMAT")
        if close_at else ""
    )
    members, link, room = vote_electorate(vote)
    unit = "group" if vote.group_id else "organisation"
    notify(
        [m.user for m in members], org=vote.org,
        kind=Notification.KIND_ELECTION_SCHEDULED,
        title=f"Charity vote coming up — {room}",
        message=(
            f"Your {unit} is about to choose the charity it raises for this season. "
            f"Voting opens {opens}"
            + (f" and closes {closes}" if closes else "")
            # em dash, not a full stop: the localised time already ends in one
            # ("5:14 p.m."), and a period after it reads as a typo.
            + " — every member gets one vote, so keep an eye out. We'll let you "
            "know here the moment it opens."
            + (f"\n\nFrom your admin: {vote.admin_message}" if vote.admin_message else "")
        ),
        link_url=link,
    )
    return vote


def set_election_close_time(vote: CharityVote, close_at) -> CharityVote:
    """Set/change (or clear, with None) the automatic end time of an open vote."""
    if not vote.is_open:
        raise ValueError("Only an open election can have its end time changed.")
    if close_at is not None and close_at <= timezone.now():
        raise ValueError("Pick an end time in the future — or just close the vote now.")
    vote.scheduled_close_at = close_at
    vote.save(update_fields=["scheduled_close_at"])
    return vote


@transaction.atomic
def open_charity_election(vote: CharityVote) -> CharityVote:
    """Open the election and tell every member: email + in-app notification."""
    if vote.status == CharityVote.STATUS_OPEN:
        return vote
    if vote.status == CharityVote.STATUS_CLOSED:
        raise ValueError("This election has already closed.")
    vote.status = CharityVote.STATUS_OPEN
    vote.opened_at = timezone.now()
    vote.save(update_fields=["status", "opened_at"])

    from .models import Notification

    members, link, room = vote_electorate(vote)
    unit = "group" if vote.group_id else "organisation"
    title = f"Charity election open — {room}"
    body = vote.admin_message or (
        f"Your {unit} is choosing where this season's money goes. Cast your vote!"
    )
    Notification.objects.bulk_create([
        Notification(
            user=m.user, org=vote.org,
            kind=Notification.KIND_ELECTION_OPEN,
            title=title, message=body, link_url=link,
        )
        for m in members
    ])
    transaction.on_commit(lambda: send_election_open_emails(vote))
    return vote


def send_election_open_emails(vote: CharityVote) -> int:
    """Branded HTML email to every member. Best-effort, never raises.

    Rendering and delivery go through goodtip.mail, which handles the
    "email isn't configured yet" case and batches the send over one connection
    instead of opening one per member.
    """
    from goodtip.mail import build, send_bulk
    from orgs.notifications import _vote_url

    options = list(vote.options.select_related("charity"))
    members, link, room = vote_electorate(vote)
    vote_url = _vote_url(vote.org_id, path=link)
    messages = [
        build(
            "election_open",
            subject=f"Vote now — where should {room}'s money go?",
            to=m.user.email,
            context={
                "user": m.user, "org": vote.org, "vote": vote,
                "group": vote.group, "room": room,
                "options": options, "vote_url": vote_url,
            },
        )
        for m in members
        if m.user.email
    ]
    return send_bulk(messages)


def open_due_elections(orgs=None) -> int:
    """Open every scheduled election whose time has come.

    Called lazily from the dashboard/vote views and by the
    `open_due_elections` management command (cron).
    """
    qs = CharityVote.objects.filter(
        status=CharityVote.STATUS_SCHEDULED,
        scheduled_open_at__lte=timezone.now(),
    )
    if orgs is not None:
        qs = qs.filter(org__in=orgs)
    n = 0
    for vote in qs.select_related("org", "group"):
        try:
            open_charity_election(vote)
            n += 1
        except Exception:  # noqa: BLE001 — one bad vote must not block the rest
            logger.exception("Failed to open election %s", vote.pk)
    return n


def close_due_elections(orgs=None) -> int:
    """Close every open election whose scheduled end time has passed.

    Called lazily from the vote view and by the `open_due_elections`
    management command (cron), so results reveal on time either way.
    """
    qs = CharityVote.objects.filter(
        status=CharityVote.STATUS_OPEN,
        scheduled_close_at__lte=timezone.now(),
    )
    if orgs is not None:
        qs = qs.filter(org__in=orgs)
    n = 0
    for vote in qs.select_related("org", "group"):
        try:
            close_charity_vote(vote)
            n += 1
        except Exception:  # noqa: BLE001 — one bad vote must not block the rest
            logger.exception("Failed to close election %s", vote.pk)
    return n


# ---------------------------------------------------------------------------
# Departments
# ---------------------------------------------------------------------------
#
# A department is a child org, named for what it is to the people in it. The
# point is scale: in a bank of 20,000 nobody knows who they are tipping
# against, and a single leaderboard of 20,000 is not a community. Twelve people
# in IT who eat lunch together is.
#
# Everything but the name, the type and the CAUSE is inherited from the parent.
# A department does not re-answer which codes it tips or which season it is in,
# because those are the organisation's answers and a department that could
# contradict them would fragment the roll-up the parent exists to produce. That
# is also why creating one is a two-field form and not the four-step wizard a
# top-level org goes through.
#
# Charity came out of that list in Aug 2026. A franchise's stores are separate
# businesses under one banner and back their own local causes, so a group may
# hold its own charity — chosen in its own election, from the charities its
# organisation has made available. See Group.effective_charity.


def create_group(org, *, name, by_user, kind=None, label="", country=None):
    """Create a group inside ``org``.

    Who is asking decides whether it is live or a request. An admin creates it
    outright; anyone else raises it for approval, because the alternative is
    any of 20,000 staff being able to mint official-looking sub-groups of the
    organisation unchecked.

    Either way the creator becomes the group's admin. They are the person who
    wanted it to exist and who knows who belongs in it, and a group nobody can
    administer is worse than no group.
    """
    from .models import Group, GroupMember, OrgMember

    name = (name or "").strip()
    if not name:
        raise ValueError("Give the group a name.")
    if org.parent_id:
        raise ValueError("Groups sit inside a top-level organisation.")
    if not org.groups_enabled:
        raise ValueError(f"{org.name} hasn't switched groups on.")
    if Group.objects.filter(org=org, name__iexact=name).exists():
        raise ValueError(f"{org.name} already has a group called {name}.")
    if not OrgMember.objects.filter(user=by_user, org=org).exists():
        raise ValueError("You have to be in the organisation to start a group in it.")

    is_admin = OrgMember.objects.filter(
        user=by_user, org=org,
    ).filter(
        Q(role__in=[OrgMember.ROLE_MANAGER, OrgMember.ROLE_BOTH])
        | Q(is_league_owner=True)
    ).exists()

    with transaction.atomic():
        group = Group.objects.create(
            org=org,
            name=name,
            kind=kind,
            label=(label or "").strip(),
            # Blank is a real answer, not a missing one: it means "wherever
            # the organisation is", which Group.effective_country resolves.
            # Only a group that is somewhere DIFFERENT needs to say so.
            country=country,
            created_by=by_user,
            approval_status=(
                Group.APPROVAL_APPROVED if is_admin else Group.APPROVAL_PENDING
            ),
            approved_at=timezone.now() if is_admin else None,
            approved_by=by_user if is_admin else None,
        )
        GroupMember.objects.create(group=group, user=by_user, is_admin=True)

    if not is_admin:
        admins = [
            m.user for m in OrgMember.objects.filter(org=org).select_related("user")
            if m.can_manage
        ]
        if admins:
            notify(
                admins,
                kind="group_requested",
                title=f"{by_user.display_name} wants to start {group.name}",
                message=f"A new group inside {org.name} is waiting for your approval.",
                link_url=reverse("orgs:groups", args=[org.pk]),
                org=org,
            )
    return group


def approve_group(group, *, by_user):
    """Approve a pending group and tell the person who asked for it."""
    from .models import Group

    if group.approval_status != Group.APPROVAL_PENDING:
        return group                      # someone got there first
    group.approval_status = Group.APPROVAL_APPROVED
    group.approved_at = timezone.now()
    group.approved_by = by_user
    group.save(update_fields=["approval_status", "approved_at", "approved_by"])

    if group.created_by_id:
        notify(
            [group.created_by],
            kind="group_approved",
            title=f"{group.name} is live",
            message=(
                f"Your group inside {group.org.name} was approved. "
                "Bring your team in and start tipping."
            ),
            link_url=reverse("orgs:groups", args=[group.org_id]),
            org=group.org,
        )
    return group


def decline_group(group, *, by_user):
    """Decline a pending group.

    The row is deleted rather than kept in a declined state. A group that was
    never approved has no members but its creator, no tips and no history, so
    there is nothing to preserve, and leaving rejected rows behind would put
    ghost groups in the very directory this feature exists to keep readable.
    Declining is only ever offered while the group is still pending.
    """
    from .models import Group

    if group.approval_status != Group.APPROVAL_PENDING:
        raise ValueError("That group has already been approved.")

    org, name, creator = group.org, group.name, group.created_by
    group.delete()

    if creator:
        notify(
            [creator],
            kind="group_declined",
            title=f"{name} wasn't approved",
            message=(
                f"An admin of {org.name} declined the group you asked for. "
                "Have a word with them if you think it should exist."
            ),
            link_url=reverse("orgs:groups", args=[org.pk]),
            org=org,
        )
    return None


def join_group(group, *, user):
    """Put someone in a group they can see.

    Joining is open to any member of the organisation: a group is a place to
    tip from, not a permission, and making people ask twice to sit with their
    own department is friction for its own sake.
    """
    from .models import Group, GroupMember, OrgMember

    if group.approval_status != Group.APPROVAL_APPROVED:
        raise ValueError("That group is still waiting to be approved.")
    if not OrgMember.objects.filter(user=user, org=group.org).exists():
        raise ValueError("You have to be in the organisation first.")
    member, created = GroupMember.objects.get_or_create(group=group, user=user)
    if created:
        # A group is its own ladder, so it needs its own backdating — the
        # organisation's rows do not score here. See backdate_missed_tips.
        from tipping.services import backdate_missed_tips

        backdate_missed_tips(user, group.org, group=group)
    return member


def leave_group(group, *, user):
    """Take someone out of a group.

    Their tips stay. They were made in that group and scored on its ladder, and
    deleting them would silently rewrite a season everyone else remembers.
    """
    from .models import GroupMember

    GroupMember.objects.filter(group=group, user=user).delete()


def groups_for(org, *, include_pending_for=None):
    """The groups of an organisation, for showing in the directory.

    Pending ones are hidden from the membership at large. Two people see them:
    an admin, who has to decide on them, and the member who raised the request,
    so their own group does not silently vanish while it waits.
    """
    from .models import Group, OrgMember

    root = org.root
    qs = Group.objects.filter(org=root).select_related("kind", "created_by")

    if include_pending_for is None:
        return qs.filter(approval_status=Group.APPROVAL_APPROVED)

    user = include_pending_for
    is_admin = OrgMember.objects.filter(
        user=user, org=root,
    ).filter(
        Q(role__in=[OrgMember.ROLE_MANAGER, OrgMember.ROLE_BOTH])
        | Q(is_league_owner=True)
    ).exists()
    if is_admin:
        return qs
    return qs.filter(
        Q(approval_status=Group.APPROVAL_APPROVED) | Q(created_by=user)
    )


# ---------------------------------------------------------------------------
# Work-email / domain verification
# ---------------------------------------------------------------------------
#
# The claim being checked is "this organisation is mine to create". The
# evidence is a code delivered to a mailbox at the organisation's own domain.
# That is not proof of employment and it is not meant to be. It is proof that
# whoever is asking can read mail inside the organisation, which is the
# strongest thing obtainable without a human in the loop, and it is enough to
# stop a stranger registering a bank.


# Domains anyone can get an address at in thirty seconds. An address here
# proves you hold a mailbox and nothing whatsoever about where you work, so
# the whole check would be theatre. Deliberately a denylist and not an
# allowlist: we cannot enumerate every legitimate company domain on earth, but
# we can name the handful that carry no signal.
PUBLIC_EMAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.com.au", "ymail.com",
    "hotmail.com", "hotmail.co.uk", "hotmail.com.au", "outlook.com",
    "outlook.com.au", "live.com", "live.com.au", "msn.com", "icloud.com",
    "me.com", "mac.com", "aol.com", "proton.me", "protonmail.com",
    "gmx.com", "mail.com", "zoho.com", "yandex.com", "fastmail.com",
    "bigpond.com", "bigpond.net.au", "optusnet.com.au", "iinet.net.au",
    "tpg.com.au", "internode.on.net", "westnet.com.au",
    # Throwaway services. Not exhaustive, and not trying to be: this is a
    # speed bump for the lazy, not a defence against the determined.
    "mailinator.com", "guerrillamail.com", "10minutemail.com",
    "tempmail.com", "trashmail.com", "yopmail.com", "sharklasers.com",
}


class VerificationError(ValueError):
    """A verification problem that knows which field caused it.

    Every one of these used to surface as the same anonymous flash at the top
    of the page: three inputs on screen, one red banner, and no indication of
    which box to fix. ``field`` is what lets the form mark the offending input
    and put the sentence underneath it, which is where someone is already
    looking when they get it wrong.
    """

    def __init__(self, message, field=None):
        super().__init__(message)
        self.field = field


def normalise_domain(raw: str) -> str:
    """Reduce whatever someone typed to a bare hostname, or raise ValueError.

    People paste "https://www.acme.com.au/careers", type "ACME.COM.AU", or
    give their email address instead. All three mean the same domain and all
    three should be accepted, because rejecting them teaches nothing and
    costs a signup.
    """
    value = (raw or "").strip().lower()
    if not value:
        raise VerificationError("Enter your organisation's website domain, e.g. acme.com.au", "domain")
    if "@" in value:                      # they pasted an email address
        value = value.rsplit("@", 1)[1]
    for prefix in ("https://", "http://", "//"):
        if value.startswith(prefix):
            value = value[len(prefix):]
    value = value.split("/")[0].split("?")[0].split("#")[0]
    value = value.split(":")[0]           # strip any port
    if value.startswith("www."):
        value = value[4:]
    value = value.strip(".")

    if "." not in value or len(value) > 253:
        raise VerificationError(f"\u201c{raw.strip()}\u201d isn't a domain we recognise. Enter just the domain, like acme.com.au", "domain")
    labels = value.split(".")
    if any(not l or len(l) > 63 for l in labels):
        raise ValueError(f"'{raw.strip()}' doesn't look like a domain. Try something like acme.com.au")
    allowed = set("abcdefghijklmnopqrstuvwxyz0123456789-")
    if any(set(l) - allowed or l.startswith("-") or l.endswith("-") for l in labels):
        raise ValueError(f"'{raw.strip()}' doesn't look like a domain. Try something like acme.com.au")
    return value


def email_domain(email: str) -> str:
    return (email or "").strip().lower().rsplit("@", 1)[-1]


def start_work_email_verification(*, user, role, domain, email):
    """Issue and send a code to a work address at ``domain``.

    Every rule that matters is here rather than in the view, so the HTTP layer
    cannot be bypassed into skipping one.
    """
    from .models import WorkEmailVerification

    role = (role or "").strip()
    if not role:
        raise VerificationError("Tell us your role, e.g. Operations Manager. It helps our team know who we are dealing with.", "role")

    domain = normalise_domain(domain)
    email = (email or "").strip().lower()
    if "@" not in email:
        raise VerificationError("Enter the work email address we should send your code to.", "work_email")

    at = email_domain(email)
    if domain in PUBLIC_EMAIL_DOMAINS:
        raise VerificationError(
            f"{domain} is a public email provider, not an organisation's domain. "
            "Anyone can get an address there, so it can't show where you work. "
            "Enter the domain from your organisation's website instead.",
            "domain",
        )
    if at in PUBLIC_EMAIL_DOMAINS:
        raise VerificationError(
            f"{at} is a public email provider. Use your work address at "
            f"{domain} so we can confirm you're inside the organisation.",
            "work_email",
        )

    # The address must live AT the claimed domain, or at a subdomain of it
    # (mail.acme.com.au, au.acme.com are the same organisation). Without this
    # the domain field would be a free-text label with no evidence behind it.
    if at != domain and not at.endswith("." + domain):
        raise VerificationError(
            f"This address is at {at}, but you said your organisation's domain is "
            f"{domain}. They need to match. Either use an address ending in "
            f"@{domain}, or correct the domain above.",
            "work_email",
        )

    row, code = WorkEmailVerification.issue(user=user, role=role, domain=domain, email=email)
    _send_work_email_code(row, code)
    return row


def resend_work_email_code(row):
    """Send a fresh code for an existing check, if the limits allow it."""
    if row.is_verified:
        raise ValueError("That address is already verified.")
    if row.sends >= row.MAX_SENDS:
        raise ValueError(
            "That's the last code we can send to this address. Check your spam folder, "
            "or start again with a different address."
        )
    if not row.can_resend:
        raise ValueError(f"Hang on {row.resend_wait_seconds} seconds before asking for another code.")
    code = row.reissue()
    _send_work_email_code(row, code)
    return row


def _echo_work_code_to_console(row, code: str) -> None:
    """Print a work-email code to the terminal running the dev server.

    The sign-in path has done this for as long as SHOW_OTP_IN_CONSOLE has
    existed (accounts.notifications._echo_code_to_console), and this one
    never did — so with real delivery unavailable, a sign-in code could
    still be read off the terminal while a work-domain code simply went
    nowhere and looked like the wizard was broken. The address here is the
    organisation's, not the user's, which is exactly the case where nobody
    developing locally can open the inbox to check.

    Same two guards as the sign-in echo, both of which must hold: DEBUG, and
    SHOW_OTP_IN_CONSOLE. A live server has neither, so a real code can never
    reach a log.
    """
    if not settings.DEBUG or not getattr(settings, "SHOW_OTP_IN_CONSOLE", False):
        return
    minutes = int(row.TTL.total_seconds() // 60)
    rule = "=" * 54
    sys.stdout.write(
        f"\n{rule}\n"
        f"  WORK EMAIL CODE: {code}   ({row.domain}, {minutes} min)\n"
        f"  for: {row.email}\n"
        f"{rule}\n\n"
    )
    sys.stdout.flush()


def _send_work_email_code(row, code: str) -> None:
    """Send the work-domain code the same way the sign-in code is sent.

    This used to call send_mail() with render_to_string() directly, which
    looked equivalent and was not: emails/_base.html — the base BOTH of these
    templates extend — paints its hero from {{ scene_url }}, and scene_url is
    injected by goodtip.mail.build(), not by the template. Rendering around
    build() left the variable undefined, so every work-email code went out
    with background="", background-image:url('') and <v:fill src="">, while
    the sign-in code (which does go through build()) rendered its artwork
    properly. Three empty image references is a spam-filter signal, which is
    a very good way for one of these to reach an inbox while the other is
    quietly filed elsewhere.

    Going through build()/send_bulk() also picks up the email_configured()
    guard, so a missing backend is logged rather than raised mid-request.
    """
    from goodtip.mail import build, send_bulk

    _echo_work_code_to_console(row, code)
    msg = build(
        "work_email_code",
        subject=f"{code} is your GoodTip verification code",
        to=row.email,
        context={
            "code": code,
            "domain": row.domain,
            "email": row.email,
            "minutes": row.ttl_minutes,
        },
    )
    # send_bulk swallows and logs its own failures. That is deliberate here:
    # the row is already saved, so a provider hiccup must not destroy a code
    # the user may yet receive, and Resend is one click away on the screen
    # they are already on.
    if not send_bulk([msg]):
        logger.warning("work email code not sent for %s", row.email)


def active_work_verification(user):
    """This user's current check, verified or not. None if they never started."""
    from .models import WorkEmailVerification
    return WorkEmailVerification.objects.filter(user=user).order_by("-created_at").first()


def apply_verification_to_org(org, row) -> None:
    """Stamp a completed check onto the organisation it was done for."""
    if not row or not row.is_verified:
        return
    org.domain = row.domain
    org.contact_role = row.role
    org.domain_verified_at = row.verified_at
    org.save(update_fields=["domain", "contact_role", "domain_verified_at"])
    row.org = org
    row.save(update_fields=["org"])


# ---------------------------------------------------------------------------
# Message attachments
# ---------------------------------------------------------------------------

def attach_files(message, files) -> list:
    """Hang uploaded files off a message. Returns the problems, not raises.

    A partial send is the right outcome here and an exception cannot express
    it. Somebody attaches four screenshots and one of them is a 40 MB video:
    losing the message, the other three files and everything they typed —
    which is what raising would do — is a much worse answer than sending what
    was valid and telling them which one did not go.

    Three limits, all cheap and all checked here rather than in the browser,
    because the browser's copy of them is a convenience and not a control:
    count, size, and an allowlist of suffixes (MessageAttachment).
    """
    from .models import MessageAttachment

    problems = []
    kept = 0
    for upload in files or []:
        if kept >= MessageAttachment.MAX_PER_MESSAGE:
            problems.append(
                f"Only {MessageAttachment.MAX_PER_MESSAGE} files per message — "
                f"“{upload.name}” wasn't attached."
            )
            continue
        suffix = Path(upload.name).suffix.lower()
        if suffix not in MessageAttachment.ALLOWED_SUFFIXES:
            problems.append(
                f"“{upload.name}” isn't a kind of file we accept "
                "(images, PDFs, documents and spreadsheets)."
            )
            continue
        if upload.size > MessageAttachment.MAX_BYTES:
            problems.append(
                f"“{upload.name}” is bigger than "
                f"{MessageAttachment.MAX_BYTES // (1024 * 1024)} MB."
            )
            continue
        MessageAttachment.objects.create(
            message=message,
            file=upload,
            # Truncated to the column, and only ever a label: the path on disk
            # is a UUID (message_attachment_path).
            original_name=upload.name[:200],
            content_type=(getattr(upload, "content_type", "") or "")[:100],
            size=upload.size or 0,
        )
        kept += 1
    return problems


# ---------------------------------------------------------------------------
# The member's inbox
# ---------------------------------------------------------------------------

def member_threads(user, *, org=None) -> list:
    """Every message thread this member can read, newest activity first.

    ACROSS ALL THEIR ORGANISATIONS, and that is the point of it. The screen
    this feeds used to be scoped to one organisation — whichever one the nav
    happened to be pointing at — so a member of seven orgs who raised something
    in the sixth went back to Messages and was told "Nothing yet". The message
    was fine. It was in another room, and nothing on the page said so or
    offered a way there.

    An organisation can still be named to narrow it, which is what the
    per-organisation views want; the inbox passes nothing.

    Readability is decided by MessageThread.can_read and not re-expressed as a
    filter here, because there is one rule about who may read a thread and a
    second copy of it in SQL is a second thing to keep in step. What this does
    do is hand can_read the membership row it would otherwise fetch itself, so
    the loop costs no queries at all.
    """
    from .models import MessageThread, OrgMember

    if not (user and user.is_authenticated):
        return []
    memberships = {
        m.org_id: m for m in OrgMember.objects.filter(user=user).select_related("org")
    }
    if org is not None:
        memberships = {k: v for k, v in memberships.items() if k == org.id}
    if not memberships:
        return []

    threads = (
        MessageThread.objects
        .filter(org_id__in=memberships)
        .select_related("started_by", "org", "group")
        .prefetch_related("recipients")
        .order_by("-last_message_at")
    )
    return [
        t for t in threads
        if t.can_read(user, membership=memberships.get(t.org_id))
    ]


def unread_message_count(user) -> int:
    """How many threads have something in them this member has not opened.

    Counted in THREADS, not messages: the badge sits on a menu item that opens
    a list of conversations, so "3" has to mean three conversations to look at.
    A count of messages would read as three things to do when it might be one
    admin sending three lines in a row.

    Their own messages never count. You do not have unread mail because you
    sent some.

    NARROW FIRST, THEN ASK WHO MAY READ IT. This runs from the context
    processor on every authenticated page view, so it must not walk the
    organisation's whole message history to draw a badge — an admin of a large
    organisation has every thread in it in `member_threads`, and materialising
    all of them per request to count three is the kind of thing that is
    invisible until it is not.

    So the unread messages are found first, in one query, and only the handful
    of threads they belong to are put through can_read. The rule itself is
    still MessageThread.can_read and is not restated as SQL — a second
    expression of who may read a thread is a second thing to keep in step.
    """
    from .models import Message, MessageThread, OrgMember

    if not (user and user.is_authenticated):
        return 0
    org_ids = list(
        OrgMember.objects.filter(user=user).values_list("org_id", flat=True)
    )
    if not org_ids:
        return 0

    candidates = set(
        Message.objects
        .filter(thread__org_id__in=org_ids)
        .exclude(author=user)
        .exclude(read_by=user)
        .values_list("thread_id", flat=True)
    )
    if not candidates:
        return 0

    memberships = {
        m.org_id: m for m in OrgMember.objects.filter(user=user, org_id__in=org_ids)
    }
    threads = (
        MessageThread.objects.filter(pk__in=candidates).prefetch_related("recipients")
    )
    return sum(
        1 for t in threads
        if t.can_read(user, membership=memberships.get(t.org_id))
    )


# ---------------------------------------------------------------------------
# One thread, read and written from both ends
# ---------------------------------------------------------------------------
#
# The member's view (orgs.views) and the organisation admin's
# (admin_panel.org_views) are two chairs at one conversation, so the logic for
# reading and quoting it lives here rather than in either of them. It was
# briefly in the member's view with the admin's importing the private names
# across — which works, and says the wrong thing about where it belongs.

def quoted_message(thread, raw_id):
    """The message a reply is answering, or None.

    Scoped to the thread on purpose: the id arrives from a hidden field the
    page filled in, and a posted id from another conversation would otherwise
    let somebody quote a message they are not allowed to read — putting words
    a person said in private into a thread they never wrote in.
    """
    raw = (raw_id or "").strip()
    if not raw.isdigit():
        return None
    return thread.messages.filter(pk=int(raw)).first()



def thread_audience(thread):
    """Who a thread is actually addressed to, as one line the page can print.

    "At the top tells me this is the organisation, this number of users, so
    this is where I know this message is going" — and until now nothing said
    it. A thread carries three different audiences in one model (the whole
    organisation, one group, or named people) and the reader could tell them
    apart only by guessing from the subject.

    Returns a dict rather than a string so the template can weight the name
    against the count, and so "and 4 others" is a decision made once here
    instead of in every template that shows an audience.

    Counting: a broadcast reaches everyone in the organisation INCLUDING
    people who join tomorrow — that is what storing no recipients means — so
    the number is the membership as it stands now and is honestly a moving
    one. A group thread counts that group. Named recipients count themselves.
    """
    from .models import GroupMember, OrgMember

    named = list(thread.recipients.all()[:6])
    if named:
        total = thread.recipients.count()
        return {
            "scope": "people",
            "name": ", ".join(u.display_name for u in named[:3]),
            "count": total,
            "extra": max(0, total - 3),
            "org": thread.org,
            "group": thread.group,
        }

    if thread.group_id is not None:
        return {
            "scope": "group",
            "name": thread.group.name,
            "count": GroupMember.objects.filter(group_id=thread.group_id).count(),
            "extra": 0,
            "org": thread.org,
            "group": thread.group,
        }

    return {
        "scope": "org",
        "name": thread.org.name,
        "count": OrgMember.objects.filter(org_id=thread.org_id).count(),
        "extra": 0,
        "org": thread.org,
        "group": None,
    }

def thread_entries(thread, user):
    """A thread's messages, ready to draw as a conversation.

    Marks everything read for this reader — opening the thread IS reading it —
    and decorates each entry with three things the chat needs and the model
    cannot answer on its own:

    * `is_mine`, which decides the side of the page a bubble sits on;
    * `show_head`, whether this bubble repeats the sender's name and face. A
      run of four messages from the same person is one turn in the
      conversation, and stamping the name on all four makes it read as four
      separate arrivals;
    * `author_role`, for the card that opens when you press somebody's name.

    Roles are fetched in ONE query for the whole thread rather than per
    message. Threads are short, but a query per bubble is the kind of thing
    that is invisible until a thread is fifty bubbles long.
    """
    from .models import OrgMember

    entries = list(
        thread.messages
        .select_related("author", "reply_to", "reply_to__author")
        .prefetch_related("attachments", "read_by")
        .order_by("created_at")
    )
    roles = {
        m.user_id: m
        for m in OrgMember.objects.filter(
            org_id=thread.org_id,
            user_id__in={e.author_id for e in entries},
        )
    }
    previous = None
    for entry in entries:
        # THE RECEIPT, COMPUTED BEFORE THIS READER IS ADDED BELOW.
        #
        # `read_by` already carries who has opened each message — it was there
        # for the unread count — so the two states the client asked for are a
        # read of existing data rather than a new column: one tick and "Sent"
        # while nobody on the other side has opened it, two ticks and "Read"
        # once somebody has.
        #
        # THE AUTHOR IS EXCLUDED, always. Opening your own thread adds you to
        # `read_by` on your own messages a moment after you send them, so
        # counting yourself would mark everything read the instant it was
        # sent, which is the one outcome that makes a receipt worthless.
        #
        # ORDER MATTERS: this runs BEFORE `read_by.add(user)` two lines down.
        # Reversed, opening a thread would mark the other side's newest
        # message as read by you and then report it back to you as read —
        # true, but not what the tick is for.
        #
        # A thread with no named recipients went to the whole organisation, so
        # "read" there means at least one person has opened it and the count
        # is worth showing. In a thread between two people the count is always
        # one and saying so adds nothing.
        readers = [u for u in entry.read_by.all() if u.id != entry.author_id]
        entry.read_count = len(readers)
        entry.is_read = bool(readers)
        entry.read_by.add(user)
        entry.is_mine = entry.author_id == user.id
        entry.show_head = (
            previous is None
            or previous.author_id != entry.author_id
            # A gap long enough that the two are not one breath. Same rule
            # every chat app uses, and for the same reason: a reply the next
            # morning is a new turn even from the same person.
            or (entry.created_at - previous.created_at).total_seconds() > 900
        )
        member = roles.get(entry.author_id)
        entry.author_role = (
            "Organisation admin" if (member and member.can_manage)
            else "Member" if member
            else "No longer a member"
        )
        previous = entry
    return entries
