"""Where the user currently is: which organisation, and which group inside it.

Until this existed there was no answer to that question. The nav simply took
the first membership the query happened to return —

    primary = orgs[0] if orgs else None

— which is not a choice anyone made, is not stable between requests once
someone joins a second organisation, and cannot express "I am looking at
Marketing" at all. Everything about groups depends on being able to say where
you are, so it lives here rather than being re-derived per view.

TWO RULES WORTH KNOWING

1. The session records an id, never an object, and the id is re-checked on
   every read. A session outlives permission: someone removed from an
   organisation an hour ago still has its id in their cookie, and it must not
   be enough to keep showing it to them. This is the same reasoning as
   _parent_for() in views.py, where trusting an id that arrived from the
   client turned out to let anyone plant a department in any organisation.

2. A group is only ever current *within* its organisation. Switching
   organisation drops the group, because "Marketing" is not a thing that can
   survive a move to a different company.
"""
from .models import Group, GroupMember, OrgMember, Organisation

ORG_KEY = "current_org_id"
GROUP_KEY = "current_group_id"


def memberships_for(user):
    """Every organisation this user belongs to, ordered so a fallback is stable.

    Ordering by name rather than by insertion means the default context does
    not silently change when an unrelated membership row is written.
    """
    if not user.is_authenticated:
        return OrgMember.objects.none()
    return (
        OrgMember.objects.filter(user=user)
        .select_related("org")
        .order_by("org__name")
    )


def orgs_for(user) -> list:
    return [m.org for m in memberships_for(user)]


def current_org(request):
    """The organisation being looked at, or None.

    Falls back to the first membership when the session names one the user can
    no longer see, and writes the fallback back so the next request is cheap
    and so the stale id stops travelling around in the cookie.
    """
    user = request.user
    if not user.is_authenticated:
        return None

    orgs = orgs_for(user)
    if not orgs:
        _forget(request)
        return None

    chosen_id = request.session.get(ORG_KEY)
    if chosen_id is not None:
        for org in orgs:
            if org.pk == chosen_id:
                return org
        # Named an organisation they are not in any more.
        _forget(request)

    fallback = orgs[0]
    # Only write when it actually changes. Django's SessionBase.__setitem__
    # marks the session modified on every assignment, even a no-op one, and
    # this runs from the context processor on every authenticated page view —
    # so assigning unconditionally would mean a session write per page load
    # for the whole site.
    if request.session.get(ORG_KEY) != fallback.pk:
        request.session[ORG_KEY] = fallback.pk
    return fallback


def set_current_org(request, org) -> None:
    """Move to an organisation. Any current group is dropped with it."""
    request.session[ORG_KEY] = org.pk
    request.session.pop(GROUP_KEY, None)


def groups_for(user, org):
    """Approved groups inside `org` that this user has joined.

    Pending groups are excluded: a group someone proposed is not somewhere you
    can go until an admin says it exists.
    """
    if org is None or not user.is_authenticated:
        return Group.objects.none()
    return (
        Group.objects.filter(
            org=org,
            approval_status=Group.APPROVAL_APPROVED,
            memberships__user=user,
        )
        .select_related("kind")
        .distinct()
        .order_by("name")
    )


def current_group(request, org=None):
    """The group being looked at, or None for "the organisation itself".

    None is a real answer, not a missing one — it is the context in which tips
    belong to the organisation rather than to a group.
    """
    user = request.user
    if not user.is_authenticated:
        return None

    org = org if org is not None else current_org(request)
    chosen_id = request.session.get(GROUP_KEY)
    if chosen_id is None:
        # The common case by far, and it must not touch the session: groups are
        # off for most organisations, and every page view runs this.
        return None
    if org is None or not org.groups_enabled:
        # Groups switched off underneath someone standing in one.
        request.session.pop(GROUP_KEY, None)
        return None

    group = (
        Group.objects.filter(
            pk=chosen_id,
            org=org,
            approval_status=Group.APPROVAL_APPROVED,
            memberships__user=user,
        )
        .select_related("kind", "org")
        .first()
    )
    if group is None:
        # Left the group, or it was declined, or it belongs to a different
        # organisation than the one now current.
        request.session.pop(GROUP_KEY, None)
    return group


def set_current_group(request, group) -> None:
    """Step into a group. Its organisation becomes current too, so the two can
    never disagree about where the user is."""
    request.session[ORG_KEY] = group.org_id
    request.session[GROUP_KEY] = group.pk


def leave_group_context(request) -> None:
    """Step back out to the organisation, staying in it."""
    request.session.pop(GROUP_KEY, None)


def is_group_member(user, group) -> bool:
    if not user.is_authenticated or group is None:
        return False
    return GroupMember.objects.filter(group=group, user=user).exists()


def tip_scope(request) -> dict:
    """The filter that says which tips belong to where the user is standing.

    Every tip query that should respect context goes through this, so "a tip
    belongs to the context it was made in" is stated once instead of being
    re-remembered — and forgetting `group` would not fail loudly, it would
    quietly pool a group's tips in with the organisation's.
    """
    org = current_org(request)
    return {"org": org, "group": current_group(request, org)}


def _forget(request) -> None:
    request.session.pop(ORG_KEY, None)
    request.session.pop(GROUP_KEY, None)
