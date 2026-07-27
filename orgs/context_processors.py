def user_orgs(request):
    if not request.user.is_authenticated:
        return {"nav_orgs": []}
    orgs = [m.org for m in request.user.memberships.select_related("org").all()]

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
    primary = orgs[0] if orgs else None
    donation = None
    primary_sport = ""
    if primary is not None:
        # Local import avoids an app-load-order import cycle.
        from billing.donations import donation_summary

        donation = donation_summary(primary)
        comp = primary.competitions.select_related("sport").first()
        if comp:
            primary_sport = comp.sport.name
    return {
        "nav_orgs": orgs,
        "primary_org": primary,
        "primary_donation": donation,
        # e.g. "Australian Rules" / "Rugby League" — drives the loader's
        # goal-post shape (client's Goal Posts Reference doc: AFL and NRL
        # must never share a silhouette).
        "primary_sport": primary_sport,
        "my_notifications": notes,
        "unread_notification_count": unread,
        "popup_notification": popup,
        # Watermark for the live poll: the page only toasts things that
        # arrive after it was rendered.
        "latest_notification_id": notes[0].id if notes else 0,
    }
