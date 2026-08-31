"""Asking whether an administrator may do something.

Every screen in the control plane asks the same two questions — "are they
allowed?" and "does it need approving first?" — so both live here rather than
being re-derived at each call site with slightly different edge cases.

THE BACKWARDS-COMPATIBLE RULE. An account with `is_superuser` and no
AdminAccess row is treated as full access. That is the founding admin, made
with `createsuperuser` before any of this existed, and the alternative — a
migration that has to guess who should have what — is a worse way to end up
locked out of your own control plane.
"""
from .models import AdminAccess


def access_for(user):
    """The AdminAccess row for this user, or None if they are not an admin."""
    if not user or not user.is_authenticated:
        return None
    return getattr(user, "admin_access", None)


def is_admin(user) -> bool:
    if not user or not user.is_authenticated:
        return False
    row = access_for(user)
    if row is not None:
        return row.is_active
    return bool(user.is_superuser or user.is_staff)


def is_full_access(user) -> bool:
    """Full access holds the two powers that are never delegated: creating
    administrators, and approving other people's work."""
    if not user or not user.is_authenticated:
        return False
    row = access_for(user)
    if row is not None:
        return row.is_active and row.is_full_access
    # See the module docstring: a superuser predating this feature is the owner.
    return bool(user.is_superuser)


def can(user, capability: str) -> bool:
    if not user or not user.is_authenticated:
        return False
    row = access_for(user)
    if row is None:
        return bool(user.is_superuser)
    return row.can(capability)


def needs_approval(user, capability: str) -> bool:
    row = access_for(user)
    if row is None:
        return False
    return row.needs_approval_for(capability)


def full_access_admins():
    """Everyone who can review a change request.

    Deliberately all of them, not whoever created the person who raised it.
    The client runs GoodTip with their partner and either of them must be able
    to clear the queue — a review that can only be done by one named person is
    a review that waits for them to come back from holiday.
    """
    from django.contrib.auth import get_user_model

    User = get_user_model()
    explicit = set(
        AdminAccess.objects.filter(is_full_access=True, is_active=True)
        .values_list("user_id", flat=True)
    )
    # Founding superusers with no row yet, per the rule above.
    implicit = set(
        User.objects.filter(is_superuser=True, is_active=True, admin_access__isnull=True)
        .values_list("pk", flat=True)
    )
    return User.objects.filter(pk__in=explicit | implicit).order_by("display_name")


def ensure_access(user, *, full=False, created_by=None):
    """Give an account an AdminAccess row if it has not got one.

    Used when the founding superuser first touches a screen that needs a row to
    point at, so the audit trail can name them properly from then on.
    """
    row = access_for(user)
    if row is not None:
        return row
    return AdminAccess.objects.create(
        user=user,
        is_full_access=full or bool(user.is_superuser),
        created_by=created_by,
    )
