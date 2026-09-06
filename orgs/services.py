import logging
import sys
from pathlib import Path

from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction
from django.db.models import Case, Count, IntegerField, Q, Value, When
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
    """Whether `user` may use org's Manage surfaces (Members, Settings,
    Groups-admin, Season summary, Charity election).

    THE RULE IS THE MEMBERSHIP'S ROLE IN THIS ORGANISATION, and nothing else.

    It used to be narrower: `can_manage` AND being the organisation's own
    `created_by`, on the reasoning that "a manager invited into someone else's
    org should not gain control of that org's admin surfaces". The effect on
    real data was that somebody appointed Team Manager of a league — appointed
    BY that league, deliberately, through its own members screen — got a member's
    nav and a 403 on the screens they had just been given the role for. Three
    live memberships on staging were in exactly that state.

    The client's instruction, twice: "if I pick the organisation that I am an
    admin, I see what the admin should see... if I get into an organisation that
    I am not an admin then I see the menu and all as a member."

    So the question this answers is now the same question the members screen
    asks when it hands the role out. WORTH BEING PLAIN ABOUT WHAT THAT WIDENS:
    a Team Manager can now reach Members, Settings, Groups and the charity
    election of an organisation they did not create. Billing is NOT in that set
    and never was — the Plan link is gated on `is_league_owner` separately, so
    the bill still belongs to the owner alone.

    The name is kept because it is referenced from a dozen call sites and one
    rename is not worth the diff; what it means is documented here.
    """
    if org is None or not user.is_authenticated:
        return False
    m = membership if membership is not None else OrgMember.objects.filter(user=user, org=org).first()
    return bool(m and m.can_manage)


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

def attach_files(message, files, *, voice=False, duration_s=0) -> list:
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
        if voice and suffix not in MessageAttachment.ALLOWED_SUFFIXES:
            # A recording whose container this build does not list. Nothing
            # the sender can do about it, so say what happened rather than
            # accusing them of attaching the wrong kind of file.
            problems.append("That recording didn\u2019t come through in a format we can play.")
            continue
        if suffix not in MessageAttachment.ALLOWED_SUFFIXES:
            problems.append(
                f"“{upload.name}” isn't a kind of file we accept "
                "(images, video, PDFs, documents and spreadsheets)."
            )
            continue
        # Video gets its own ceiling: 8 MB is about six seconds of phone
        # footage, so the general limit would have made the paperclip accept
        # .mp4 and then reject every real clip anybody tried to send.
        limit = MessageAttachment.limit_for(upload.name)
        if upload.size > limit:
            problems.append(
                f"“{upload.name}” is bigger than {limit // (1024 * 1024)} MB."
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
            # A voice note is a recording the page just made, not a file
            # anybody chose, and the two arrive in the same containers — a
            # .webm is either. Only the caller knows which this is.
            is_voice=voice,
            # Clamped, because the number arrives from the page and the page
            # is not a source of truth about anything. The recorder stops at
            # the ceiling; this is what happens when something else does not.
            duration_s=(
                min(int(duration_s or 0), MessageAttachment.MAX_VOICE_SECONDS)
                if voice else 0
            ),
        )
        kept += 1
    return problems


# ---------------------------------------------------------------------------
# The member's inbox
# ---------------------------------------------------------------------------

def member_threads(user, *, org=None) -> list:
    """Every message thread this member can read, newest activity first.

    `keep` names one conversation that must appear whatever else is true —
    see the empty-direct-message case below.

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
        plausible_threads(user, list(memberships))
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

    # Narrowed to threads this person could plausibly be in BEFORE the unread
    # messages in them are counted. Without it, every direct message between
    # two other members of a large organisation is a candidate — none of them
    # have been read by this user, because none of them are theirs — and the
    # badge on every page view walks the lot to discard them.
    candidates = set(
        Message.objects
        .filter(thread__in=plausible_threads(user, org_ids))
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

#: How much of a room is drawn at once. A support ticket is a dozen messages
#: and this never mattered; an organisation room is open-ended, and rendering
#: three years of it into every page load — and into every twelve-second
#: refresh — is the difference between a screen that opens and one that does
#: not. The newest 200, oldest first, which is what a conversation is.
ROOM_PAGE = 200


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

    A CLEARED CONVERSATION STARTS FROM WHERE IT WAS CLEARED. "Clear chat" moves
    a line for one reader; everything at or before it stops being shown to them
    and nothing is deleted, so the other forty people in an organisation room
    still have every word of it.
    """
    from .models import Message, OrgMember, ThreadPreference

    # NEWEST 200, THEN TURNED BACK ROUND. Slicing from the end is the only way
    # to say "the most recent" in SQL; the list is then reversed so the
    # conversation still reads oldest-first, which is what a conversation is.
    qs = thread.messages.select_related("author", "reply_to", "reply_to__author")
    # The reader's own line, if they have cleared this conversation. Applied to
    # the QUERY rather than to the list, so a cleared thread costs nothing to
    # open — and applied before the slice, or "the newest 200" would count
    # messages this reader cannot see and hand back an empty page.
    cleared = (
        ThreadPreference.objects
        .filter(user=user, thread=thread, cleared_at__isnull=False)
        .values_list("cleared_at", flat=True)
        .first()
    ) if user and user.is_authenticated else None
    if cleared is not None:
        qs = qs.filter(created_at__gt=cleared)
    entries = list(
        reversed(
            qs
            # `reactions` prefetched with the rest, so a thread with a
            # reaction on every bubble still costs the same few queries as one
            # with none.
            .prefetch_related("attachments", "read_by", "reactions")
            .order_by("-created_at")[:ROOM_PAGE]
        )
    )
    roles = {
        m.user_id: m
        for m in OrgMember.objects.filter(
            org_id=thread.org_id,
            user_id__in={e.author_id for e in entries},
        )
    }
    previous = None
    to_mark = []
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
        # COLLECTED, NOT WRITTEN HERE. `entry.read_by.add(user)` is one query
        # per message, which for a support ticket was invisible and for a room
        # of two hundred messages refreshed every twelve seconds is two
        # hundred writes per viewer per refresh. They go up in one statement
        # after the loop instead — see below.
        if not any(u.id == user.id for u in entry.read_by.all()):
            to_mark.append(entry.id)
        entry.is_mine = entry.author_id == user.id
        entry.show_head = (
            previous is None
            or previous.author_id != entry.author_id
            # A gap long enough that the two are not one breath. Same rule
            # every chat app uses, and for the same reason: a reply the next
            # morning is a new turn even from the same person.
            or (entry.created_at - previous.created_at).total_seconds() > 900
        )
        # One chip per distinct emoji: the emoji, how many people, and whether
        # you are one of them — which is what lets pressing a chip take you
        # back out of it. Built here from the prefetched rows rather than
        # counted in SQL per message, because it is three fields off a list
        # that is already in memory.
        chips = {}
        for row in entry.reactions.all():
            chip = chips.setdefault(row.emoji, {"emoji": row.emoji, "count": 0, "mine": False})
            chip["count"] += 1
            if row.user_id == user.id:
                chip["mine"] = True
        entry.reaction_chips = list(chips.values())

        member = roles.get(entry.author_id)
        entry.author_role = (
            "Organisation admin" if (member and member.can_manage)
            else "Member" if member
            else "No longer a member"
        )
        previous = entry

    if to_mark:
        # One INSERT for the lot. ignore_conflicts because two tabs open on
        # the same room will both decide the same message is unread, and the
        # unique index on the through table is what makes that safe rather
        # than a crash.
        through = Message.read_by.through
        through.objects.bulk_create(
            [through(message_id=mid, user_id=user.id) for mid in to_mark],
            ignore_conflicts=True,
        )
    return entries


# ---------------------------------------------------------------------------
# THE ROOMS (Sep 2026)
#
# "Someone joins, creates an organisation … within those organisations they
# might want groups … so basically the messages is where I can chat."
#
# Three rooms, one model. An organisation room every member of the
# organisation is in; a group room every member of that group is in; and a
# direct message between two people. All three are MessageThread rows with a
# different `kind`, so everything already built around a thread — bubbles,
# attachments, quoting, read receipts, notifications, the unread badge — works
# on them without being written a second time.
#
# Every one of these is get-or-create rather than a row made up front. There
# is no migration that walks every organisation and every group creating empty
# threads: a room comes into existence the first time somebody opens it, which
# means a comp that never uses chat carries no rows for it, and a group created
# tomorrow needs nothing doing to it.
# ---------------------------------------------------------------------------

def org_room(org):
    """The organisation-wide room. Everyone in the organisation is in it."""
    from .models import MessageThread

    thread, _ = MessageThread.objects.get_or_create(
        org=org, kind=MessageThread.KIND_ORG, group=None,
        defaults={
            "subject": org.name,
            # The room belongs to the organisation, not to whoever happened to
            # open it first — but `started_by` is not nullable and the row has
            # to name somebody. The organisation's earliest admin is the least
            # arbitrary answer, and nothing reads it for a room.
            "started_by": _room_owner(org),
        },
    )
    return thread


def group_room(group):
    """One group's room. Its members, and nobody else — see can_read."""
    from .models import MessageThread

    thread, _ = MessageThread.objects.get_or_create(
        org_id=group.org_id, kind=MessageThread.KIND_GROUP, group=group,
        defaults={"subject": group.name, "started_by": _room_owner(group.org)},
    )
    return thread


def _room_owner(org):
    """Somebody to hang a room on. See org_room for why this is arbitrary."""
    from .models import OrgMember

    member = (
        OrgMember.objects.filter(org=org, is_league_owner=True).first()
        or OrgMember.objects.filter(org=org).order_by("joined_at").first()
    )
    if member is None:
        raise ValueError("An organisation with no members has no rooms.")
    return member.user


def direct_thread(user, other, org):
    """The conversation between two people, made if it is not there yet.

    LOOKED UP IN BOTH DIRECTIONS. Who started it is an accident of who pressed
    first, and a lookup that only matched `started_by=me` would hand each
    person their own half of the conversation — two threads, each showing one
    side of it, which is the classic way to get this wrong.

    Scoped to an organisation, because that is what gives two people the right
    to write to each other at all: `can_read` requires membership, and a
    thread has to hang off one organisation to be found by the inbox. Two
    people in three shared organisations get three conversations, which is the
    honest answer — writing to somebody about the Marketing comp is not the
    same conversation as writing to them about the McDonald's one.
    """
    from django.db.models import Q

    from .models import MessageThread

    if other.id == user.id:
        raise ValueError("You cannot start a conversation with yourself.")
    existing = (
        MessageThread.objects
        .filter(org=org, kind=MessageThread.KIND_DIRECT)
        .filter(
            Q(started_by=user, recipients=other)
            | Q(started_by=other, recipients=user)
        )
        .distinct()
        .first()
    )
    if existing is not None:
        return existing
    thread = MessageThread.objects.create(
        org=org, kind=MessageThread.KIND_DIRECT,
        # The subject is never printed for a direct message — the person's
        # name is the title — but the column is not nullable and a blank one
        # makes every admin listing unreadable.
        subject=f"{user.display_name} & {other.display_name}"[:160],
        started_by=user,
    )
    thread.recipients.add(other)
    return thread


def room_audience_ids(thread) -> list:
    """Every user id that can read this room, for notifications.

    Rooms do not use `recipients` for their audience — an organisation room
    with a row per member would need maintaining every time somebody joins —
    so who is in one is a query, and this is the one place that asks it.
    """
    from .models import GroupMember, MessageThread, OrgMember

    if thread.kind == MessageThread.KIND_ORG:
        return list(
            OrgMember.objects.filter(org_id=thread.org_id)
            .values_list("user_id", flat=True)
        )
    if thread.kind == MessageThread.KIND_GROUP:
        return list(
            GroupMember.objects.filter(group_id=thread.group_id)
            .values_list("user_id", flat=True)
        )
    if thread.kind == MessageThread.KIND_DIRECT:
        return [thread.started_by_id] + list(
            thread.recipients.values_list("id", flat=True)
        )
    return []


# ---------------------------------------------------------------------------
# The sidebar
# ---------------------------------------------------------------------------

def plausible_threads(user, org_ids):
    """The threads worth asking `can_read` about, narrowed in SQL first.

    WHY THIS EXISTS, and it is not premature optimisation.

    Before the rooms, every thread in an organisation was addressed to its
    admins, so "load them all and ask can_read" cost one row per support
    ticket — a handful. Direct messages change the arithmetic completely: two
    members writing to each other creates a MessageThread hanging off THEIR
    organisation, so an organisation of five hundred people accumulates as
    many direct threads as its members care to start, and every one of them
    is in `filter(org_id__in=...)`. Loading all of them to hand the reader
    the four they are in is work that grows with other people's conversations.

    So the obvious exclusions happen in the database, and `can_read` still
    decides. That ordering matters: this is a NARROWING, not a second
    expression of the rule. Anything this lets through is still asked, so a
    filter drifting out of step with can_read can only cost a wasted row —
    never leak one.
    """
    from django.db.models import Q

    from .models import MessageThread

    return (
        MessageThread.objects
        .filter(org_id__in=org_ids)
        .filter(
            # Support threads: readable by the admins and by whoever raised
            # it, plus notices, all of which can_read works out from rows this
            # query has already fetched.
            Q(kind__in=(MessageThread.KIND_RAISED, MessageThread.KIND_NOTICE))
            # The organisation room: membership is the whole test, and
            # org_id__in is already that.
            | Q(kind=MessageThread.KIND_ORG)
            # A group room, only for the groups this person is in.
            | Q(kind=MessageThread.KIND_GROUP, group__memberships__user=user)
            # A direct message, only from one of its two ends.
            | Q(kind=MessageThread.KIND_DIRECT, started_by=user)
            | Q(kind=MessageThread.KIND_DIRECT, recipients=user)
        )
        # The joins above (group__memberships, recipients) multiply rows.
        .distinct()
    )


def conversations_for(user, *, keep=None, show="active") -> list:
    """Every conversation this person can open, newest activity first.

    ONE LIST, EVERY ORGANISATION — the same decision `member_threads` made and
    for the same reason: a member of seven organisations has one inbox, and
    scoping it to whichever room the nav happens to be pointing at is how
    somebody presses Messages and is told there is nothing there.

    Each row is a dict rather than the model, because what the sidebar has to
    print — the title, the subtitle, the avatar letter, whether it is a person
    or a room — is four different derivations depending on the kind, and doing
    them in the template means four nested {% if %}s repeated in three places.

    COST. One query for the threads, one for the last message of each (in
    bulk), one for the unread set, one for direct-message partners. It does
    not grow with the number of conversations in the way a per-row lookup
    would, which matters because this list is the page.
    """
    from django.db.models import Max

    from .models import Message, MessageThread, OrgMember

    if not (user and user.is_authenticated):
        return []
    memberships = {
        m.org_id: m for m in OrgMember.objects.filter(user=user).select_related("org")
    }
    if not memberships:
        return []

    threads = list(
        plausible_threads(user, list(memberships))
        .select_related("started_by", "org", "group")
        .prefetch_related("recipients")
        .order_by("-last_message_at")
    )
    threads = [
        t for t in threads
        if t.can_read(user, membership=memberships.get(t.org_id))
    ]
    if not threads:
        return []

    ids = [t.id for t in threads]
    # The last line of each conversation, in one query. Two steps rather than
    # one because "the newest row per group" is not something the ORM will
    # express portably — the ids come back first, then the rows themselves.
    last_ids = [
        row["last"] for row in
        Message.objects.filter(thread_id__in=ids)
        .values("thread_id").annotate(last=Max("id"))
    ]
    last = {
        m.thread_id: m for m in
        Message.objects.filter(pk__in=last_ids)
        .select_related("author").prefetch_related("attachments")
    }
    unread = set(
        Message.objects.filter(thread_id__in=ids)
        .exclude(author=user).exclude(read_by=user)
        .values_list("thread_id", flat=True)
    )
    unread_counts = {}
    for tid in (
        Message.objects.filter(thread_id__in=unread)
        .exclude(author=user).exclude(read_by=user)
        .values_list("thread_id", flat=True)
    ):
        unread_counts[tid] = unread_counts.get(tid, 0) + 1

    rows = []
    for thread in threads:
        newest = last.get(thread.id)
        # AN EMPTY DIRECT MESSAGE IS NOT A CONVERSATION.
        #
        # Opening a DM is a GET that creates the thread, so that a room has
        # one address however you arrived at it — which also means a browser
        # prefetching the link, or somebody pressing a name and changing their
        # mind, leaves a thread with nothing in it. Listing those puts a ghost
        # row in TWO people's sidebars for a conversation neither of them had.
        #
        # The row is dropped, not the thread: it is still readable at its own
        # URL, and it becomes a real conversation the moment either of them
        # writes in it.
        # `keep` is the conversation currently open: a DM you have just
        # started is empty by definition, and dropping it would leave the
        # chat pane showing a conversation with no row selected beside it.
        if (
            newest is None
            and thread.kind == MessageThread.KIND_DIRECT
            and thread.id != keep
        ):
            continue
        rows.append(_conversation_row(thread, user, newest, unread_counts))

    # ---- what this reader has decided about their own list -----------------
    #
    # Applied here rather than in the query, because the state lives in a table
    # keyed on (user, thread) and joining it into `plausible_threads` would make
    # a permission query answer a preference question as well. One extra query
    # for the whole sidebar; see chatprefs.prefs_by_thread.
    from .chatprefs import blocked_user_ids, prefs_by_thread

    prefs = prefs_by_thread(user, [r["id"] for r in rows])
    blocked = blocked_user_ids(user)
    kept = []
    for row in rows:
        pref = prefs.get(row["id"])
        # A DIRECT MESSAGE WITH SOMEBODY BLOCKED IS NOT LISTED — in either
        # direction. Rooms are untouched: a falling-out between two people must
        # not quietly edit the organisation's own chat for one of them.
        other = row.get("avatar_user")
        if row["face"] == "person" and other is not None and other.id in blocked:
            continue
        row["pinned"] = bool(pref and pref.is_pinned)
        row["favourite"] = bool(pref and pref.is_favourite)
        row["muted"] = bool(pref and pref.is_muted)
        row["archived"] = bool(pref and pref.is_archived)
        row["menu"] = _chat_menu(row)
        # A muted room still counts its unread; it simply does not interrupt.
        # Hiding the number as well would make mute mean "ignore", which is a
        # different thing somebody would have chosen archive for.
        if row["archived"] and show != "archived" and row["id"] != keep:
            # Archived comes BACK on new activity — otherwise archiving a live
            # room is indistinguishable from leaving it, and people lose
            # conversations they meant to tidy away.
            if not row["unread"]:
                continue
        if show == "archived" and not row["archived"]:
            continue
        kept.append(row)

    # Pinned first, everything else in the order it already had.
    #
    # ONE STABLE SORT, on one key. The rows arrive newest-activity-first from
    # the query, and Python's sort is stable — so sorting on "is it pinned"
    # alone lifts the pinned ones to the top and leaves both groups in
    # activity order. Sorting on a compound key here would mean re-deriving the
    # ordering the database already did, and getting it subtly wrong for the
    # rows where last_message_at is null.
    kept.sort(key=lambda r: 0 if r["pinned"] else 1)
    return kept


def _chat_menu(row) -> list:
    """The right-click menu for one row, labelled for its current state.

    LABELS READ THE STATE — "Unpin" when it is pinned — because the endpoint is
    a toggle and a menu saying "Pin" over an already-pinned chat would be
    offering to do what it would actually undo.

    Block is only on a direct message. Blocking somebody "in" the organisation
    room would be asking to stop hearing a colleague in a room forty people
    share, which is a different request and not one this grants.
    """
    items = [
        ("pin", "Unpin chat" if row["pinned"] else "Pin chat"),
        ("favourite", "Remove from favourites" if row["favourite"] else "Add to favourites"),
        ("mute", "Unmute notifications" if row["muted"] else "Mute notifications"),
        ("archive", "Unarchive chat" if row["archived"] else "Archive chat"),
        ("clear", "Clear messages"),
        ("delete", "Delete chat"),
    ]
    if row["face"] == "person":
        items.append(("block", "Block this person"))
    return items


def _conversation_row(thread, user, last_message, unread_counts) -> dict:
    """One sidebar row. See conversations_for for why this is not a template."""
    from .models import MessageThread

    other = thread.other_party(user) if thread.kind == MessageThread.KIND_DIRECT else None
    if thread.kind == MessageThread.KIND_DIRECT:
        title = other.display_name if other else "Direct message"
        subtitle = "Direct message"
        avatar_user, letter, face = other, (title or "?")[:1].upper(), "person"
    elif thread.kind == MessageThread.KIND_GROUP:
        title = thread.group.name if thread.group else thread.subject
        subtitle = thread.org.name
        avatar_user, letter, face = None, (title or "?")[:1].upper(), "group"
    elif thread.kind == MessageThread.KIND_ORG:
        title = thread.org.name
        subtitle = "Everyone in this organisation"
        avatar_user, letter, face = None, (title or "?")[:1].upper(), "org"
    else:
        # A support thread — raised with the admins, or a notice from them.
        title = thread.subject
        subtitle = (
            f"From the admins · {thread.org.name}"
            if thread.kind == MessageThread.KIND_NOTICE
            else f"With the admins · {thread.org.name}"
        )
        avatar_user, letter, face = None, "!", "admins"

    # The preview line. A message that is only a photograph or a voice note has
    # no words to show, and printing an empty line for it makes the row look
    # broken — so it is described instead, the way every messaging app does it.
    preview, who = "", ""
    if last_message is not None:
        who = "You" if last_message.author_id == user.id else last_message.author.display_name.split(" ")[0]
        body = (last_message.body or "").strip()
        if body:
            preview = body
        else:
            files = list(last_message.attachments.all())
            if any(f.is_voice for f in files):
                preview = "\U0001F3A4 Voice note"
            elif any(f.is_image for f in files):
                preview = "\U0001F4F7 Photo"
            elif any(f.is_video for f in files):
                preview = "\U0001F3AC Video"
            elif files:
                preview = f"\U0001F4CE {files[0].original_name}"

    return {
        "thread": thread,
        "id": thread.id,
        "org": thread.org,
        # Defaults, overwritten in conversations_for once the reader's own
        # preferences are in hand. Present here so a row is always the same
        # shape — a template that has to guard every one of these reads like a
        # list of apologies.
        "pinned": False, "favourite": False, "muted": False, "archived": False,
        "group": thread.group,
        "kind": thread.kind,
        "face": face,
        "title": title,
        "subtitle": subtitle,
        "letter": letter,
        "avatar_user": avatar_user,
        "other": other,
        "preview": preview,
        "preview_who": who,
        "when": thread.last_message_at,
        "unread": unread_counts.get(thread.id, 0),
    }


def room_members(thread, *, search="", limit=60, offset=0):
    """Who is in a room, as a page of rows the member panel can print.

    PAGED AND SEARCHABLE, because the client's own worst case is the reason
    this panel exists: "take a look at an organisation that has about 1000
    people". Rendering a thousand rows into every page load is the difference
    between a panel that opens instantly and one that does not open at all, so
    the list arrives sixty at a time and the search runs in the database.

    Returns (rows, total, has_more). Each row carries the badges the panel
    draws — Admin, Captain, You — resolved here rather than per row in the
    template, where each one would be a query.
    """
    from django.contrib.auth import get_user_model
    from django.db.models import Q

    from .models import GroupMember, MessageThread, OrgMember

    User = get_user_model()
    if thread.kind == MessageThread.KIND_GROUP:
        user_ids = GroupMember.objects.filter(
            group_id=thread.group_id,
        ).values_list("user_id", flat=True)
    elif thread.kind == MessageThread.KIND_DIRECT:
        user_ids = [thread.started_by_id] + list(
            thread.recipients.values_list("id", flat=True)
        )
    else:
        user_ids = OrgMember.objects.filter(
            org_id=thread.org_id,
        ).values_list("user_id", flat=True)

    people = User.objects.filter(id__in=list(user_ids))
    term = (search or "").strip()
    if term:
        people = people.filter(
            Q(display_name__icontains=term) | Q(email__icontains=term)
        )

    # ADMINS FIRST — "the details will include members, starting with admin,
    # name of the group and all, just like WhatsApp."
    #
    # Ordered in SQL rather than sorted after the slice, because the slice is
    # the paging: sorting sixty rows in Python would put the admins at the top
    # of page one and of page two, and leave an admin on page seventeen exactly
    # where they were.
    #
    # A group room ranks its own admins above the organisation's, because in a
    # group the person who runs the group is the one you are looking for.
    ranks = _member_rank_ids(thread)
    people = people.annotate(
        _rank=Case(
            *[When(id__in=ids, then=Value(n)) for n, ids in enumerate(ranks)],
            default=Value(len(ranks)),
            output_field=IntegerField(),
        )
    ).order_by("_rank", "display_name", "id")
    total = people.count()
    page = list(people[offset:offset + limit])

    # The organisation roles for exactly the people on this page — one query,
    # not one per row. `can_manage` is the tag the client asked for: "the guy
    # knows he is the admin, even in the list of members we will have the tag
    # admin, so no reason to have write-to-admin — I'll shoot them a DM."
    roles = {
        m.user_id: m for m in
        OrgMember.objects.filter(org_id=thread.org_id, user_id__in=[p.id for p in page])
    }
    group_admins = set()
    if thread.kind == MessageThread.KIND_GROUP:
        group_admins = set(
            GroupMember.objects.filter(
                group_id=thread.group_id, is_admin=True,
                user_id__in=[p.id for p in page],
            ).values_list("user_id", flat=True)
        )

    rows = []
    for person in page:
        member = roles.get(person.id)
        badges = []
        if member is not None and member.can_manage:
            badges.append("Admin")
        elif person.id in group_admins:
            badges.append("Group admin")
        if member is not None and member.is_captain:
            badges.append("Captain")
        rows.append({"user": person, "badges": badges, "member": member})
    return rows, total, (offset + len(page)) < total


def _member_rank_ids(thread) -> list:
    """The id sets that put a member list in WhatsApp's order, best first.

    Returned as a LIST OF SETS rather than a dict of ranks so the caller can
    turn it straight into one CASE expression — the position in the list is the
    rank, and anybody in none of them sorts last.

    An organisation room has one rank that matters: who runs the comp. A group
    room has two, and the group's own admins come first — in a group, the person
    you are looking for is the person who runs the group, not the person who
    runs the league it sits in.
    """
    from .models import GroupMember, MessageThread, OrgMember

    org_admins = set(
        OrgMember.objects.filter(org_id=thread.org_id)
        .filter(Q(role__in=[OrgMember.ROLE_MANAGER, OrgMember.ROLE_BOTH])
                | Q(is_league_owner=True))
        .values_list("user_id", flat=True)
    )
    if thread.kind != MessageThread.KIND_GROUP:
        return [org_admins]
    group_admins = set(
        GroupMember.objects.filter(group_id=thread.group_id, is_admin=True)
        .values_list("user_id", flat=True)
    )
    return [group_admins, org_admins - group_admins]


def contacts_for(user, *, search="", limit=40, offset=0):
    """Everybody this member can start a direct message with, as printable rows.

    ASKED FOR: "same with the people — I click and now a DM for one person is
    here." The People tab is a directory, not a filter of conversations you have
    already had: a member who has never messaged anybody has to be able to find
    somebody to message.

    ONE ROW PER PERSON, NOT PER MEMBERSHIP. Two people who share three
    organisations are one contact, and the row names the organisation the
    message would go through — the first one, alphabetically, because a direct
    message needs exactly one and any of them would do.

    PAGED AND SEARCHED IN THE DATABASE, for the same reason the room's member
    panel is: an organisation of a thousand people cannot be rendered into a
    sidebar, and a name typed into the box must not be matched by walking a
    thousand rows in the browser.

    Returns (rows, total, has_more).
    """
    from django.contrib.auth import get_user_model

    from .chatprefs import blocked_user_ids
    from .models import OrgMember

    User = get_user_model()
    my_org_ids = list(
        OrgMember.objects.filter(user=user).values_list("org_id", flat=True)
    )
    if not my_org_ids:
        return [], 0, False

    # Blocked people are not in the directory either. A block that hid the
    # conversation but left the person one press away from starting a new one
    # would be a setting, not a block.
    hidden = blocked_user_ids(user) | {user.id}
    people = (
        User.objects.filter(memberships__org_id__in=my_org_ids)
        .exclude(id__in=hidden)
        .distinct()
    )
    term = (search or "").strip()
    if term:
        people = people.filter(
            Q(display_name__icontains=term) | Q(email__icontains=term)
        )
    people = people.order_by("display_name", "id")
    total = people.count()
    page = list(people[offset:offset + limit])

    # Which organisation each row's message would go through, and whether they
    # run it — both in one query for the whole page rather than one per row.
    memberships = {}
    for m in (
        OrgMember.objects
        .filter(org_id__in=my_org_ids, user_id__in=[p.id for p in page])
        .select_related("org")
        .order_by("org__name")
    ):
        memberships.setdefault(m.user_id, m)

    rows = []
    for person in page:
        member = memberships.get(person.id)
        if member is None:      # left the organisation between the two queries
            continue
        rows.append({
            "user": person,
            "org": member.org,
            "badges": ["Admin"] if member.can_manage else [],
        })
    return rows, total, (offset + len(page)) < total


def shared_groups(user, other, org=None):
    """The groups two people are both in — "groups in common", as WhatsApp says.

    Scoped to an organisation when one is given, which is what the details panel
    for a direct message wants: the conversation belongs to one comp, and a
    group they share in a different one is not what "in common" means there.
    """
    from .models import Group

    qs = Group.objects.filter(
        approval_status=Group.APPROVAL_APPROVED,
        memberships__user=user,
    ).filter(memberships__user=other)
    if org is not None:
        qs = qs.filter(org=org)
    return list(qs.select_related("org").distinct().order_by("name"))


def attach_chat_state(user, *, orgs=(), groups=(), contacts=()):
    """Hang each row's existing conversation, and its menu, on the row itself.

    WHY THE DIRECTORY ROWS NEED THIS. "The right click and holding the
    organisation, or group, or people — one, someone — what should happen is the
    same like in WhatsApp: archive chat, mute notification, pin chat ... add to
    favourite ... delete chat and clear chat."

    The menu is about a CONVERSATION, and the tabs list things. So each row is
    matched to the thread it would open — if there already is one — and gets the
    same menu the recent-chats row would have carried, with its labels reading
    the state it is actually in.

    NOTHING IS CREATED HERE. A row with no conversation yet gets no menu, which
    is the honest answer: there is nothing to pin, mute, clear or archive until
    something has been said. Creating a thread per row per page load, so that a
    menu could be drawn over it, would fill the table with empty rooms nobody
    opened.

    Sets `.chat` on each organisation and group, and row["chat"] on each contact
    dict — attached to the object the template already loops rather than handed
    over as a dict, because Django templates cannot look a dict up by a variable
    key.
    """
    from django.db.models import Q

    from .chatprefs import prefs_by_thread
    from .models import MessageThread

    orgs, groups, contacts = list(orgs), list(groups), list(contacts)
    org_ids = [o.id for o in orgs]
    group_ids = [g.id for g in groups]
    people_ids = [r["user"].id for r in contacts]

    wanted = Q(pk__in=[])
    if org_ids:
        wanted |= Q(kind=MessageThread.KIND_ORG, org_id__in=org_ids)
    if group_ids:
        wanted |= Q(kind=MessageThread.KIND_GROUP, group_id__in=group_ids)
    if people_ids:
        # Both directions: who started it is an accident of who pressed first.
        wanted |= Q(
            Q(kind=MessageThread.KIND_DIRECT),
            Q(started_by=user, recipients__in=people_ids)
            | Q(started_by_id__in=people_ids, recipients=user),
        )
    threads = list(
        MessageThread.objects.filter(wanted).distinct()
        .prefetch_related("recipients")
    )
    prefs = prefs_by_thread(user, [t.id for t in threads])

    def state(thread):
        pref = prefs.get(thread.id)
        row = {
            "id": thread.id,
            "pinned": bool(pref and pref.is_pinned),
            "favourite": bool(pref and pref.is_favourite),
            "muted": bool(pref and pref.is_muted),
            "archived": bool(pref and pref.is_archived),
            "face": "person" if thread.kind == MessageThread.KIND_DIRECT else thread.kind,
        }
        row["menu"] = _chat_menu(row)
        return row

    by_org, by_group, by_person = {}, {}, {}
    for thread in threads:
        if thread.kind == MessageThread.KIND_ORG:
            by_org[thread.org_id] = state(thread)
        elif thread.kind == MessageThread.KIND_GROUP:
            by_group[thread.group_id] = state(thread)
        else:
            other = thread.other_party(user)
            if other is not None:
                # Two people in three shared comps have three conversations
                # (see direct_thread). The contact row is one person, so it
                # carries the first — pressing it opens that organisation's,
                # which is the one the row already names.
                by_person.setdefault(other.id, state(thread))

    for o in orgs:
        o.chat = by_org.get(o.id)
    for g in groups:
        g.chat = by_group.get(g.id)
    for row in contacts:
        row["chat"] = by_person.get(row["user"].id)


def charity_votes_for_room(user, org, group=None, *, ballot_context):
    """Every election this room has held, newest first, ready to draw.

    ASKED FOR: "let's say in an organisation we did an election, we should also
    have a card for that — when I click it I will see the votes, when the
    election was started, ended, a bar and pie chart of the results and all, and
    if it was a tie and the admin had to select, that also should be captured."

    So each row carries the whole history of one ballot, not just its winner:
    when it opened, when it closed, how many of the electorate voted, the tally
    per option, and — where the count did not decide it — who broke the tie and
    when. A charity chosen by one person rather than by the count is a different
    kind of decision, and a results screen that hides that is telling a story
    the numbers do not support.

    `ballot_context` is handed in rather than imported: it lives in orgs.views
    with the screens that render a single ballot, and importing a view into a
    service is the wrong direction. Passing it keeps the tally maths in ONE
    place — the blind-vote rule (counts while open, tallies only once closed) is
    a promise to members, and a promise implemented twice is a promise kept
    once.
    """
    from .models import CharityVote, GroupMember, OrgMember

    votes = (
        CharityVote.objects
        .filter(org=org, group=group)
        .select_related("winning_charity", "tie_broken_by", "group")
        .order_by("-opened_at")
    )
    if group is not None:
        eligible = GroupMember.objects.filter(group=group).count()
    else:
        eligible = OrgMember.objects.filter(org=org).count()

    rows = []
    for vote in votes:
        ctx = ballot_context(vote, user, eligible)
        # THE BAR AND ITS SLICE ARE THE SAME COLOUR. Two charts of one tally
        # that colour their series differently are two charts the reader has to
        # reconcile by name, which is the work the colour was supposed to save.
        for i, option in enumerate(ctx["results"] or []):
            option.colour = PIE_COLOURS[i % len(PIE_COLOURS)]
        rows.append({
            "vote": vote,
            "results": ctx["results"],
            "stats": ctx["stats"],
            "ballot_count": ctx["ballot_count"],
            "eligible_count": ctx["eligible_count"],
            "turnout_pct": ctx["turnout_pct"],
            "tied_options": ctx["tied_options"],
            # A PIE NEEDS ITS SLICES PRE-ADDED. conic-gradient takes absolute
            # stops rather than widths, so each slice has to know where the one
            # before it ended — which a Django template cannot accumulate.
            "pie": _pie_stops(ctx["results"]),
        })
    return rows


#: The wheel's colours, in order. Deliberately not the competition palette:
#: these are charities, and borrowing the codes' colours would suggest a
#: relationship between a cause and a football code that does not exist.
PIE_COLOURS = [
    "#2D7A3A", "#5AA9FF", "#C79BFF", "#FFB25C", "#FF8FB4",
    "#3FD0C9", "#9A5A05", "#6B34B8", "#B01F55", "#12558F",
]


def _pie_stops(results) -> str:
    """A conic-gradient value for one vote's tallies, or "" when there are none.

    Built here rather than in the template because each stop is the running
    total of everything before it, and a template cannot carry a sum across a
    loop. Returns the whole gradient so the markup is one style attribute.
    """
    if not results:
        return ""
    total = sum(o.n for o in results)
    if not total:
        return ""
    stops, at = [], 0.0
    for i, option in enumerate(results):
        share = option.n * 360.0 / total
        colour = PIE_COLOURS[i % len(PIE_COLOURS)]
        stops.append(f"{colour} {at:.2f}deg {at + share:.2f}deg")
        at += share
    return "conic-gradient(" + ", ".join(stops) + ")"


#: The photographs offered as a chat wallpaper, and what to call them.
#:
#: Our own rather than an upload — "we will not, like WhatsApp, be given the
#: opportunity to keep ours, but we will utilise our own." Which also means no
#: upload endpoint, no storage and no moderation question for what is only ever
#: decoration behind a conversation.
#:
#: Landscapes only: a wallpaper is a wide, mostly-empty field behind text, and
#: the portrait crops that suit the narrow side strips put a player's head in
#: the middle of the messages.
WALLPAPERS = [
    ("stadium-panorama", "Stadium panorama", "img/scenes/stadium-panorama.jpg"),
    ("mcg-stadium",      "The 'G",           "img/scenes/mcg-stadium.jpg"),
    ("stadium-night",    "Under lights",     "img/stadium-night.jpg"),
    ("afl-ground",       "Match day",        "img/scenes/afl-ground.jpg"),
    ("nrl-ground-dusk",  "Ground at dusk",   "img/scenes/nrl-ground-dusk.jpg"),
    ("aussie-crowd-flag", "The crowd",       "img/scenes/aussie-crowd-flag.jpg"),
]


def chat_wallpapers():
    """The wallpaper options, with their hashed URLs resolved once.

    `static()` is called here rather than in the template so the list is one
    thing with one shape — the template loops it for the layer AND for the
    picker, and resolving the URL twice in two loops is how the two end up
    disagreeing after somebody edits one of them.
    """
    from django.templatetags.static import static

    return [
        {"key": key, "label": label, "url": static(path)}
        for key, label, path in WALLPAPERS
    ]
