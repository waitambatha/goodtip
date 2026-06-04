def user_orgs(request):
    if not request.user.is_authenticated:
        return {"nav_orgs": []}
    orgs = [m.org for m in request.user.memberships.select_related("org").all()]
    return {"nav_orgs": orgs, "primary_org": orgs[0] if orgs else None}
