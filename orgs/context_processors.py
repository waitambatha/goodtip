def user_orgs(request):
    if not request.user.is_authenticated:
        return {"nav_orgs": []}
    memberships = list(request.user.memberships.select_related("org").all())
    orgs = [m.org for m in memberships]

    # Scheduled charity elections open lazily on any app page view, so members
    # get their popup/email even before the cron command is wired up.
    from .services import open_due_elections

    if orgs:
        open_due_elections(orgs=orgs)

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

    primary = orgs[0] if orgs else None
    donation = None
    primary_sport = ""
    # Drives the admin-only nav items. Computed here rather than per template so
    # every page agrees on whether this member manages the org they are looking
    # at — a nav that appears on one screen and not the next reads as a bug.
    primary_org_is_admin = False
    if primary is not None:
        # Local import avoids an app-load-order import cycle.
        from billing.donations import donation_summary

        donation = donation_summary(primary)
        # Reuses the memberships already fetched above — the answer is in
        # hand, and this runs on every authenticated page view.
        mine = next((m for m in memberships if m.org_id == primary.id), None)
        primary_org_is_admin = bool(mine and mine.can_manage)
        comp = primary.competitions.select_related("sport").first()
        if comp:
            primary_sport = comp.sport.name
    return {
        "nav_orgs": orgs,
        "primary_org": primary,
        "primary_org_is_admin": primary_org_is_admin,
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
