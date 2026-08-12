import logging

from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone
from django.utils.text import slugify

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
        link_url=f"/leagues/{req.org_id}/requests/{req.id}/",
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
        link_url="/leagues/search/",
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

    from .models import Notification

    if winner is not None:
        title = f"{winner.name} won your charity vote"
        body = (
            f"{vote.org.name} has chosen {winner.name}. Everything your organisation "
            "raises from here goes to them — thanks for having your say."
        )
    else:
        title = f"Charity vote closed — {vote.org.name}"
        body = (
            "The vote has closed without a result, so no charity was chosen this "
            "time. Your admin can start another one whenever the group's ready."
        )
    notify(
        [m.user for m in vote.org.members.select_related("user")], org=vote.org,
        kind=Notification.KIND_ELECTION_RESULT,
        title=title, message=body,
        link_url=f"/leagues/{vote.org_id}/charity-vote/",
    )
    return winner


def can_lock_fundraising(org) -> bool:
    """Charity Partner Workflow (categories doc): only a Charities-type org
    whose partner flag GoodTip staff have set in admin may lock fundraising
    to itself. The flag is never self-declared.
    """
    return bool(org.group_type_id and org.group_type.is_charity_type and org.is_charity_partner)


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
    notify(
        [m.user for m in vote.org.members.select_related("user")], org=vote.org,
        kind=Notification.KIND_ELECTION_SCHEDULED,
        title=f"Charity vote coming up — {vote.org.name}",
        message=(
            f"Your organisation is about to choose the charity it raises for this season. "
            f"Voting opens {opens}"
            + (f" and closes {closes}" if closes else "")
            # em dash, not a full stop: the localised time already ends in one
            # ("5:14 p.m."), and a period after it reads as a typo.
            + " — every member gets one vote, so keep an eye out. We'll let you "
            "know here the moment it opens."
            + (f"\n\nFrom your admin: {vote.admin_message}" if vote.admin_message else "")
        ),
        link_url=f"/leagues/{vote.org_id}/charity-vote/",
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

    members = list(vote.org.members.select_related("user"))
    title = f"Charity election open — {vote.org.name}"
    body = vote.admin_message or (
        "Your organisation is choosing where this season's money goes. Cast your vote!"
    )
    link = f"/leagues/{vote.org_id}/charity-vote/"
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
    vote_url = _vote_url(vote.org_id)
    messages = [
        build(
            "election_open",
            subject=f"Vote now — where should {vote.org.name}'s money go?",
            to=m.user.email,
            context={
                "user": m.user, "org": vote.org, "vote": vote,
                "options": options, "vote_url": vote_url,
            },
        )
        for m in vote.org.members.select_related("user")
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
    for vote in qs.select_related("org"):
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
    for vote in qs.select_related("org"):
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
# Everything but the name and the type is inherited from the parent. A
# department does not re-answer which codes it tips, which season it is in, or
# which charity it raises for, because those are the organisation's answers and
# a department that could contradict them would fragment the roll-up the parent
# exists to produce. That is also why creating one is a two-field form and not
# the four-step wizard a top-level org goes through.


def create_department(parent, *, name, by_user, department_type=None, department_label=""):
    """Create a department under ``parent``.

    Who is asking decides whether it is live or a request. An admin of the
    parent creates it outright; anyone else raises it for approval, because
    the alternative is any of 20,000 staff being able to mint official-looking
    sub-groups of the organisation unchecked.

    Either way the creator becomes the department's admin. They are the person
    who wanted it to exist and who knows who belongs in it, and a department
    nobody can administer is worse than no department.
    """
    from .models import Organisation, OrgMember

    name = (name or "").strip()
    if not name:
        raise ValueError("Give the department a name.")
    if parent.parent_id:
        # Two levels only. Guarded here as well as in Organisation.clean()
        # because this path builds the org in code and never runs full_clean.
        raise ValueError("Departments sit under a top-level organisation, not under another department.")

    is_admin = OrgMember.objects.filter(
        user=by_user, org=parent, role__in=[OrgMember.ROLE_MANAGER, OrgMember.ROLE_BOTH],
    ).exists()

    if Organisation.objects.filter(parent=parent, name__iexact=name).exists():
        raise ValueError(f"{parent.name} already has a department called {name}.")

    with transaction.atomic():
        dept = Organisation.objects.create(
            parent=parent,
            name=name,
            department_type=department_type,
            department_label=(department_label or "").strip(),
            created_by=by_user,
            approval_status=(
                Organisation.APPROVAL_APPROVED if is_admin else Organisation.APPROVAL_PENDING
            ),
            approved_at=timezone.now() if is_admin else None,
            approved_by=by_user if is_admin else None,
            # Inherited, never re-asked.
            group_type=parent.group_type,
            state=parent.state,
            season=parent.season,
            charity=parent.charity,
            finals_only=parent.finals_only,
        )
        dept.competitions.set(parent.competitions.all())
        add_member(by_user, dept, role=OrgMember.ROLE_BOTH)

    if dept.is_pending_approval:
        _notify_department_request(dept)
    return dept


def _notify_department_request(dept) -> None:
    """Tell the parent's admins there is a department waiting on them."""
    admins = org_admin_users(dept.parent)
    if not admins:
        return
    who = getattr(dept.created_by, "display_name", "A member")
    notify(
        admins,
        kind="department_request",
        title=f"New department: {dept.name}",
        message=f"{who} wants to start {dept.name} inside {dept.parent.name}. Approve it to make it visible to everyone.",
        link_url=f"/leagues/{dept.parent_id}/departments/",
        org=dept.parent,
    )


def approve_department(dept, *, by_user):
    """Approve a pending department and tell the person who asked for it."""
    if not dept.approve(by_user=by_user):
        return dept          # already approved; someone got there first
    if dept.created_by_id:
        notify(
            [dept.created_by],
            kind="department_approved",
            title=f"{dept.name} is live",
            message=f"Your department inside {dept.parent.name} was approved. Invite your team and start tipping.",
            link_url=f"/leagues/{dept.pk}/wall/",
            org=dept,
        )
    return dept


def decline_department(dept, *, by_user):
    """Decline a pending department.

    The row is deleted rather than kept in a declined state. A department that
    was never approved has no members but its creator, no tips and no history,
    so there is nothing to preserve, and leaving rejected rows behind would put
    ghost departments in the very directory this feature exists to keep clean.
    Declining is only ever offered while the department is still pending.
    """
    from .models import Organisation, OrgMember

    if dept.approval_status != Organisation.APPROVAL_PENDING:
        raise ValueError("That department has already been approved.")

    parent_name, dept_name, creator = dept.parent.name, dept.name, dept.created_by
    with transaction.atomic():
        OrgMember.objects.filter(org=dept).delete()
        dept.competitions.clear()
        dept.sub_categories.clear()
        dept.delete()

    if creator:
        notify(
            [creator],
            kind="department_declined",
            title=f"{dept_name} wasn't approved",
            message=f"An admin of {parent_name} declined the request. Have a word with them if you think it should exist.",
            org=None,
        )
    return True


def departments_for(org, *, include_pending_for=None):
    """The departments of an org, for showing in the directory.

    Pending ones are hidden from the membership at large. Two people see them:
    an admin of the parent, who has to decide on them, and the member who
    raised the request, so their own department does not silently vanish while
    it waits.
    """
    from .models import Organisation, OrgMember

    root = org.root
    qs = Organisation.objects.filter(parent=root).select_related("department_type", "created_by")

    if include_pending_for is None:
        return qs.filter(approval_status=Organisation.APPROVAL_APPROVED)

    is_admin = OrgMember.objects.filter(
        user=include_pending_for, org=root,
        role__in=[OrgMember.ROLE_MANAGER, OrgMember.ROLE_BOTH],
    ).exists()
    if is_admin:
        return qs
    return qs.filter(
        Q(approval_status=Organisation.APPROVAL_APPROVED)
        | Q(created_by=include_pending_for)
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


def _send_work_email_code(row, code: str) -> None:
    from django.template.loader import render_to_string

    ctx = {"code": code, "domain": row.domain, "email": row.email,
           "minutes": int(row.TTL.total_seconds() // 60)}
    try:
        send_mail(
            subject=f"{code} is your GoodTip verification code",
            message=render_to_string("emails/work_email_code.txt", ctx),
            html_message=render_to_string("emails/work_email_code.html", ctx),
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[row.email],
            fail_silently=False,
        )
    except Exception:
        # The row is already saved. Logging rather than raising means a
        # provider hiccup does not destroy a code the user may yet receive,
        # and Resend is one click away on the screen they are already on.
        logger.exception("work email code send failed for %s", row.email)


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
