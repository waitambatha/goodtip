def user_orgs(request):
    if not request.user.is_authenticated:
        return {"nav_orgs": []}
    from .context import current_group, current_org, groups_for

    memberships = list(request.user.memberships.select_related("org").all())
    orgs = [m.org for m in memberships]

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
    from .models import Group

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
    # The popup: newest not-yet-dismissed notification.
    popup = next((n for n in notes if n.dismissed_at is None), None)
    # Staff nav badge: anything waiting in the Approvals queue, shown on every
    # admin page so a request can't sit there unseen.
    pending_approval_count = 0
    open_enquiry_count = 0
    if request.user.is_staff:
        from .models import MembershipRequest

        pending_approval_count = MembershipRequest.objects.filter(
            status=MembershipRequest.STATUS_PENDING,
        ).count()

        # Same idea for the enquiry inbox: a customer asking a question is at
        # least as time-sensitive as a join request, and the badge is what stops
        # it sitting unread because nobody thought to go looking for it.
        from admin_panel.models import Enquiry

        open_enquiry_count = Enquiry.objects.filter(status=Enquiry.STATUS_NEW).count()

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
        "open_enquiry_count": open_enquiry_count,
        "popup_notification": popup,
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
