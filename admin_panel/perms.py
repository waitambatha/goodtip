"""Who may see what in /manage/.

/manage/ is the ORGANISATION admin's area: the person who created an
organisation, running the one (or few) they created. It is not the system
control plane — that is /admin/, and it is reached by being a superuser.

Before this split every view here was `@staff_member_required`, and since
nothing in the codebase grants `is_staff` except `create_superuser`, the
practical effect was that /manage/ was superuser-only and an org creator could
never open it. Opening it up is the change most likely to leak data, because
every query in here was written under the assumption "you can see everything".

So the rule is enforced by scoping the QUERY, never by hiding the link:
`managed_orgs()` is the only door, and a view that needs one organisation asks
`get_managed_org_or_404()` rather than `get_object_or_404` — a stranger's org
id is indistinguishable from one that does not exist, which is what you want.
"""
from functools import wraps

from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.http import Http404
from django.shortcuts import redirect

from orgs.models import OrgMember, Organisation


def managed_orgs(user):
    """Organisations `user` administers, as a queryset.

    "Administers" mirrors OrgMember.can_manage — a league owner (the creator)
    or a team manager. Superusers are NOT handed everything here: /manage/
    answers "how is my organisation doing", and a superuser who runs no
    organisation genuinely has nothing to see on it. Their view of every org
    lives in /admin/, which is a different question asked from a different
    place.

    The whole thing is ONE filter() call, and that is load-bearing. Written as
    two chained filters —

        .filter(members__user=user).filter(members__is_league_owner=True)

    — Django is free to satisfy each against a DIFFERENT OrgMember row, so a
    plain participant in an organisation that has any owner at all would match
    it. One filter() forces both conditions onto the same joined row.
    """
    if not (user and user.is_authenticated):
        return Organisation.objects.none()
    return Organisation.objects.filter(
        Q(members__user=user)
        & (
            Q(members__role__in=(OrgMember.ROLE_MANAGER, OrgMember.ROLE_BOTH))
            | Q(members__is_league_owner=True)
        )
    ).distinct()


def manages_any(user) -> bool:
    return managed_orgs(user).exists()


def get_managed_org_or_404(user, org_id):
    """One organisation, but only if this user administers it.

    404 rather than 403 on purpose: telling a stranger "that exists but is not
    yours" confirms the organisation exists, which is not theirs to learn.
    """
    org = managed_orgs(user).filter(pk=org_id).first()
    if org is None:
        raise Http404("No organisation matches the given query.")
    return org


def org_admin_required(view):
    """Reachable by anyone who administers at least one organisation.

    Superusers pass too — not because /manage/ is theirs, but because being
    locked out of a page you are debugging is worse than seeing an empty one,
    and every query behind this decorator is scoped to `managed_orgs` anyway,
    so a superuser who runs no organisation simply sees nothing.
    """
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        user = request.user
        if not (user and user.is_authenticated):
            from django.urls import reverse
            return redirect(f"{reverse('accounts:login')}?next={request.path}")
        if user.is_superuser or manages_any(user):
            return view(request, *args, **kwargs)
        raise PermissionDenied("You do not administer any organisation.")
    return wrapper
