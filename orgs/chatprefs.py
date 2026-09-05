"""What one person has decided about their own conversations.

Pin, favourite, mute, archive, clear, delete and block — the WhatsApp set the
client asked for. Kept together because they share one rule that is easy to
break one action at a time: every one of them is private to the person who set
it, and none of them may change what anybody else sees.

That rule is what makes "delete" honest. You cannot delete a room you share with
forty colleagues, so delete is archive plus clear: off your list, empty for you,
untouched for everyone else, and back the moment somebody writes in it again.
"""
from __future__ import annotations

from django.db.models import Q
from django.utils import timezone

#: The toggles, and the field each one writes. Named here rather than spelled
#: into the view's if/elif chain so the URL, the menu and the model cannot drift
#: apart — a menu item that posts an action nothing handles fails silently.
TOGGLES = {
    "pin": "pinned_at",
    "favourite": "favourite_at",
    "mute": "muted_at",
    "archive": "archived_at",
}


def pref_for(user, thread):
    from .models import ThreadPreference

    pref, _ = ThreadPreference.objects.get_or_create(user=user, thread=thread)
    return pref


def prefs_by_thread(user, thread_ids):
    """One query for a whole sidebar. Called per row, this would be the page."""
    from .models import ThreadPreference

    return {
        p.thread_id: p
        for p in ThreadPreference.objects.filter(user=user, thread_id__in=thread_ids)
    }


def toggle(user, thread, action: str) -> bool:
    """Flip one of the four. Returns whether it is now on.

    A toggle rather than separate set/unset endpoints: the menu shows one item
    whose label reads the current state ("Unpin" when it is pinned), and two
    endpoints would mean the label and the action could disagree.
    """
    field = TOGGLES[action]
    pref = pref_for(user, thread)
    now_on = getattr(pref, field) is None
    setattr(pref, field, timezone.now() if now_on else None)
    pref.save(update_fields=[field, "updated_at"])
    return now_on


def clear(user, thread):
    """Empty this conversation for one reader. Nothing is deleted.

    The line moves to now, so anything already said stops being shown to them
    and anything said next is. Reversible in the sense that matters: nothing was
    destroyed, though the interface deliberately offers no "unclear" — an undo
    for "I don't want to see that any more" invites the accidental re-reading it
    was meant to prevent.
    """
    pref = pref_for(user, thread)
    pref.cleared_at = timezone.now()
    pref.save(update_fields=["cleared_at", "updated_at"])
    return pref


def delete_for_me(user, thread):
    """Archive and clear together. The honest version of "delete chat"."""
    pref = pref_for(user, thread)
    pref.cleared_at = timezone.now()
    pref.archived_at = timezone.now()
    pref.save(update_fields=["cleared_at", "archived_at", "updated_at"])
    return pref


# ---------------------------------------------------------------------------
# Blocking
# ---------------------------------------------------------------------------

def block(user, other, org):
    from .models import MemberBlock

    MemberBlock.objects.get_or_create(org=org, user=user, blocked=other)


def unblock(user, other, org):
    from .models import MemberBlock

    MemberBlock.objects.filter(org=org, user=user, blocked=other).delete()


def blocked_between(user, other, org) -> bool:
    """True if either has blocked the other.

    BOTH DIRECTIONS. A block that only worked one way would let the blocked
    person keep writing into a conversation the other can no longer answer,
    which is a worse outcome than not having the feature.
    """
    from .models import MemberBlock

    return MemberBlock.objects.filter(
        Q(org=org, user=user, blocked=other) | Q(org=org, user=other, blocked=user)
    ).exists()


def blocked_user_ids(user) -> set[int]:
    """Everyone this person has blocked, or been blocked by, anywhere.

    Used to drop direct messages out of a sidebar in one pass. Not scoped to an
    organisation because the sidebar is not either — it lists every conversation
    from every organisation, and a per-row scope check would be a query per row.
    """
    from .models import MemberBlock

    rows = MemberBlock.objects.filter(Q(user=user) | Q(blocked=user)).values_list(
        "user_id", "blocked_id",
    )
    return {b if a == user.id else a for a, b in rows}
