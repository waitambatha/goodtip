def user_orgs(request):
    if not request.user.is_authenticated:
        return {"nav_orgs": []}
    orgs = [m.org for m in request.user.memberships.select_related("org").all()]
    primary = orgs[0] if orgs else None
    donation = None
    if primary is not None:
        # Local import avoids an app-load-order import cycle.
        from billing.donations import donation_summary

        donation = donation_summary(primary)
    return {"nav_orgs": orgs, "primary_org": primary, "primary_donation": donation}
