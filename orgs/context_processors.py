def user_orgs(request):
    if not request.user.is_authenticated:
        return {"nav_orgs": []}
    from .context import current_group, current_org, groups_for

    memberships = list(request.user.memberships.select_related("org").all())
    orgs = [m.org for m in memberships]

    # WHAT YOU ARE IN EACH ONE, on the org itself, so the switcher can say so
    # without a lookup per row.
    #
    # ASKED FOR AS: the client was invited into somebody else's organisation as
    # an ordinary member and "did not feel the distinction" — the nav already
    # drops the Manage menu when you switch into an org you do not run (see
    # primary_org_is_admin below), but nothing on the way IN says which of your
    # organisations that will be. A list of names cannot tell you that; a list
    # of names with roles on them can.
    #
    # The membership is already in hand, so is_creator_admin costs no query.
    from .services import is_creator_admin as _is_creator_admin

    for m in memberships:
        m.org.nav_is_admin = _is_creator_admin(request.user, m.org, membership=m)
        m.org.nav_role = "Admin" if m.org.nav_is_admin else "Member"

    # Scheduled charity elections open lazily on any app page view, so members
    # get their popup/email even before the cron command is wired up.
    from .services import open_due_elections

    if orgs:
        open_due_elections(orgs=orgs)

    # Every org's groups, in one query, so the nav's org switcher can preview
    # any org's groups on hover without a request per row. Attached directly
    # onto each Organisation instance (o.nav_groups) rather than a separate
    # dict, so the template can just loop `o.nav_groups` — Django templates
    # have no built-in "look this dict up by a variable key" syntax.
    from .models import Group, MessageReaction

    nav_any_groups_enabled = any(o.groups_enabled for o in orgs)
    for o in orgs:
        o.nav_groups = []
    if nav_any_groups_enabled:
        groups_by_org = {}
        enabled_ids = [o.id for o in orgs if o.groups_enabled]
        for g in (
            Group.objects.filter(
                org_id__in=enabled_ids,
                approval_status=Group.APPROVAL_APPROVED,
                memberships__user=request.user,
            )
            .select_related("kind")
            .distinct()
            .order_by("org_id", "name")
        ):
            groups_by_org.setdefault(g.org_id, []).append(g)
        for o in orgs:
            o.nav_groups = groups_by_org.get(o.id, [])

    notes = list(
        request.user.notifications.select_related("org").order_by("-created_at")[:12]
    )
    unread = sum(1 for n in notes if n.read_at is None)
    # The bell ticker.
    #
    # This used to be a single `popup` — the newest undismissed notification —
    # rendered as a card pinned bottom-right until someone closed it. One card
    # is one notification: the second and third waited behind it, unseen, and
    # the first sat over the page as a thing to clear before you could work.
    #
    # Now every undismissed notification takes a turn at the bell instead: the
    # bell buzzes, a one-line teaser appears next to it for a few seconds, and
    # the loop moves on. Nothing has to be closed, and being away from the
    # screen for one turn costs nothing because it comes round again.
    #
    # Only the title travels — the teaser is a hook, not the message. The
    # message, the timestamp and the full list stay in the bell panel, which is
    # where clicking a teaser goes.
    #
    # Capped at five: past that the loop is long enough that the first one is
    # forgotten before it returns, and the panel already holds the rest.
    ticker = [
        {
            "id": n.id,
            "org": n.org.name if n.org else "GoodTip",
            "title": n.title,
            "url": n.link_url or "",
            "icon": n.icon,
        }
        for n in notes if n.dismissed_at is None
    ][:5]
    # Nav badges for the organisation admin. Scoped to the organisations this
    # person actually runs — the counts used to be platform-wide and gated on
    # is_staff, which was correct while /manage/ was superuser-only and became
    # a leak the moment an org creator could open it.
    #
    # The enquiry badge is gone from here: an enquiry comes from the public
    # contact form and is addressed to GoodTip the company, not to anybody's
    # organisation. It belongs to the super admin and lives in /admin/ now.
    pending_approval_count = 0
    unread_thread_count = 0
    if request.user.is_authenticated:
        from admin_panel.perms import managed_orgs

        mine = list(managed_orgs(request.user).values_list("id", flat=True))
        if mine:
            from .models import MembershipRequest, MessageThread

            pending_approval_count = MembershipRequest.objects.filter(
                status=MembershipRequest.STATUS_PENDING, org_id__in=mine,
            ).count()
            unread_thread_count = MessageThread.objects.filter(
                org_id__in=mine,
                kind=MessageThread.KIND_RAISED,
                status=MessageThread.STATUS_OPEN,
            ).count()

    # The member's own count, which is a different question from the admin's
    # above. That one asks "how many members are waiting on me"; this asks "how
    # many conversations have something in them I haven't read" — and it is
    # what the Messages item in the member nav wears. Without it the member
    # side was the only place on the site where a reply arrived and nothing
    # anywhere said so, which is exactly what the client hit.
    unread_message_count = 0
    if request.user.is_authenticated:
        from .services import unread_message_count as _member_unread

        unread_message_count = _member_unread(request.user)

    # Where the user actually is, rather than whichever membership the query
    # happened to return first. `primary_org` is kept as the name the nav
    # templates already use; it now means "current" instead of "arbitrary".
    primary = current_org(request)
    group = current_group(request, primary)
    donation = None
    primary_sport = ""
    # Drives the admin-only nav items. Computed here rather than per template so
    # every page agrees on whether this member manages the org they are looking
    # at — a nav that appears on one screen and not the next reads as a bug.
    primary_org_is_admin = False
    # Billing is narrower than "can manage": a Team Manager can run Members
    # and Settings without ever seeing the bill, so the Plan link in the
    # Manage menu is gated on ownership specifically, not on can_manage.
    primary_org_is_owner = False
    if primary is not None:
        # Local import avoids an app-load-order import cycle.
        from billing.donations import donation_summary

        donation = donation_summary(primary)
        # Reuses the memberships already fetched above — the answer is in
        # hand, and this runs on every authenticated page view.
        mine = next((m for m in memberships if m.org_id == primary.id), None)
        from .services import is_creator_admin

        primary_org_is_admin = is_creator_admin(request.user, primary, membership=mine)
        primary_org_is_owner = bool(mine and mine.is_league_owner)
        comp = primary.competitions.select_related("sport").first()
        if comp:
            primary_sport = comp.sport.name
    return {
        "nav_orgs": orgs,
        "nav_any_groups_enabled": nav_any_groups_enabled,
        # The reaction picker's fixed set. Here rather than passed by each of
        # the three views that render a conversation, because it is a constant
        # and the alternative is three places to forget it in — the chat
        # partial is shared between the member's screen, the admin's, and the
        # fragment the reaction endpoint swaps back.
        "reaction_choices": MessageReaction.CHOICES,
        "primary_org": primary,
        "current_org": primary,
        "current_group": group,
        # The groups this member can step into, for the switcher. Empty when
        # the organisation has not switched groups on, which is the default.
        "current_org_groups": list(groups_for(request.user, primary)) if primary else [],
        "primary_org_is_admin": primary_org_is_admin,
        "primary_org_is_owner": primary_org_is_owner,
        "primary_donation": donation,
        # e.g. "Australian Rules" / "Rugby League" — drives the loader's
        # goal-post shape (client's Goal Posts Reference doc: AFL and NRL
        # must never share a silhouette).
        "primary_sport": primary_sport,
        "my_notifications": notes,
        "unread_notification_count": unread,
        "pending_approval_count": pending_approval_count,
        "unread_thread_count": unread_thread_count,
        "unread_message_count": unread_message_count,
        "ticker_notifications": ticker,
        # Watermark for the live poll: the page only toasts things that
        # arrive after it was rendered.
        "latest_notification_id": notes[0].id if notes else 0,
    }


def contact_form(request):
    """Carry a failed enquiry back to the page it was written on.

    The contact form is an include on five different pages, none of which own a
    view that could re-render it with errors. The submit endpoint therefore
    redirects back to wherever the visitor was and leaves the problem here.

    Read-and-clear: an error survives exactly one render, so returning to the
    page later does not show a stale complaint about a message already sent.
    """
    if not hasattr(request, "session"):
        return {}
    error = request.session.pop("enquiry_error", None)
    draft = request.session.pop("enquiry_draft", None)
    if not error and not draft:
        return {}
    request.session.modified = True
    return {"enquiry_error": error or "", "enquiry_draft": draft or {}}
