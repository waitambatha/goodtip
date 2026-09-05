"""Prefect's review desk, for the people who run an organisation.

Everything Prefect and the members raise arrives here, and every consequence in
the feature is applied from here by a person. That separation is the whole
design: the reader is a word list today and a language model tomorrow, and
neither of them gets to warn, suspend or remove anybody.

WHO MAY OPEN IT. org_admin_required, the same gate as the rest of /manage/ —
and a captain who is not an admin is deliberately NOT let in. The client asked
for captains to SEE the content, which they do: the notification goes to them
and it carries the message. What they cannot do is suspend somebody, because
suspending a colleague is an organisation-level act and the role that carries it
is the one the organisation hands out for exactly that.
"""
from datetime import timedelta

from django.contrib import messages as flash
from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from orgs.models import (
    ChatFlag, Group, MemberSanction, Notification, PrefectAllowance,
)

from .org_views import _pick_org
from .perms import managed_orgs, org_admin_required


#: The lengths offered as buttons. "Until I lift it" is the third and it is a
#: NULL end date rather than a distant one — see MemberSanction.
SPANS = {"week": timedelta(days=7), "month": timedelta(days=30)}


@org_admin_required
def prefect_queue(request):
    """What is waiting, worst first, with what has already been decided beside it."""
    mine = managed_orgs(request.user)
    org = _pick_org(request, mine)
    if org is None:
        return render(request, "manage/prefect.html", {"org": None, "orgs": mine})

    show = request.GET.get("show", "open")
    flags = (
        ChatFlag.objects.filter(org=org)
        .select_related("message", "message__thread", "message__thread__group",
                        "author", "raised_by", "reviewed_by")
    )
    if show == "open":
        flags = flags.filter(status=ChatFlag.STATUS_OPEN)
    elif show in {ChatFlag.STATUS_CLEARED, ChatFlag.STATUS_ACTIONED}:
        flags = flags.filter(status=show)
    # Worst first, then newest. A queue in date order buries the thing that
    # matters under six reports of somebody saying "clown".
    flags = flags.order_by("-score", "-created_at")[:200]

    return render(request, "manage/prefect.html", {
        "org": org, "orgs": mine, "show": show, "flags": flags,
        "open_count": ChatFlag.objects.filter(org=org, status=ChatFlag.STATUS_OPEN).count(),
        "sanctions": (
            MemberSanction.objects.filter(org=org)
            .select_related("user", "group", "issued_by")[:50]
        ),
        "allowances": PrefectAllowance.objects.filter(org=org).select_related("added_by"),
    })


@org_admin_required
def prefect_flag(request, flag_id: int):
    """One flag, the message in the room it was said in, and what can be done.

    THE MESSAGE IS SHOWN WITH WHAT CAME BEFORE IT. The client's own example is
    the reason: "what's up you crazy fool" between friends and the same words
    from a stranger are the same string and different events, and the only thing
    that tells them apart is the conversation around them. A review screen that
    shows one line in isolation is asking somebody to make exactly the mistake
    Prefect is not trusted to make.
    """
    mine = managed_orgs(request.user)
    flag = get_object_or_404(
        ChatFlag.objects.select_related(
            "org", "message", "message__thread", "message__thread__group",
            "author", "raised_by", "reviewed_by",
        ),
        pk=flag_id,
    )
    if not mine.filter(pk=flag.org_id).exists():
        raise Http404("No flag matches the given query.")

    thread = flag.message.thread
    around = list(
        thread.messages.select_related("author")
        .filter(created_at__lte=flag.message.created_at)
        .order_by("-created_at")[:6]
    )[::-1]
    after = list(
        thread.messages.select_related("author")
        .filter(created_at__gt=flag.message.created_at)
        .order_by("created_at")[:3]
    )

    return render(request, "manage/prefect_flag.html", {
        "org": flag.org, "orgs": mine, "flag": flag, "thread": thread,
        "before": around, "after": after,
        "groups": Group.objects.filter(org=flag.org, approval_status=Group.APPROVAL_APPROVED),
        "history": (
            MemberSanction.objects.filter(org=flag.org, user=flag.author)
            .select_related("group", "issued_by")[:10]
        ),
        "prior_flags": ChatFlag.objects.filter(
            org=flag.org, author=flag.author, status=ChatFlag.STATUS_ACTIONED,
        ).exclude(pk=flag.pk).count(),
    })


def _close(flag, request, status):
    flag.status = status
    flag.reviewed_by = request.user
    flag.reviewed_at = timezone.now()
    flag.save(update_fields=["status", "reviewed_by", "reviewed_at"])


@org_admin_required
@require_POST
def prefect_clear(request, flag_id: int):
    """"Not an issue" — and, if asked, teach Prefect not to raise it here again.

    The learning is opt-in on this screen rather than automatic, because the two
    cases look identical from the outside and are not: one is "these two are
    friends", the other is "that word is ordinary in this workplace". Only the
    second should change what Prefect says next time, and only a person can tell
    which one they are looking at.
    """
    mine = managed_orgs(request.user)
    flag = get_object_or_404(ChatFlag, pk=flag_id)
    if not mine.filter(pk=flag.org_id).exists():
        raise Http404("No flag matches the given query.")

    learned = []
    if request.POST.get("learn") and flag.terms:
        for phrase in flag.terms:
            _, created = PrefectAllowance.objects.get_or_create(
                org=flag.org, phrase=phrase.lower(),
                defaults={"added_by": request.user},
            )
            if created:
                learned.append(phrase)
    flag.note = (request.POST.get("note") or flag.note).strip()[:2000]
    flag.save(update_fields=["note"])
    _close(flag, request, ChatFlag.STATUS_CLEARED)

    if learned:
        flash.success(
            request,
            "Cleared. Prefect won't raise " +
            ", ".join(f"“{w}”" for w in learned) + " in this organisation again.",
        )
    else:
        flash.success(request, "Cleared. Nothing has been done to anybody.")
    return redirect("manage:prefect")


@org_admin_required
@require_POST
def prefect_act(request, flag_id: int):
    """Warn, suspend, or remove — the three things a reviewer can do.

    All of them write a MemberSanction row and tell the person themselves. Being
    warned or suspended without being told is the version of this that turns a
    tipping comp into a place people are quietly managed out of.
    """
    mine = managed_orgs(request.user)
    flag = get_object_or_404(
        ChatFlag.objects.select_related("author", "org", "message__thread__group"),
        pk=flag_id,
    )
    if not mine.filter(pk=flag.org_id).exists():
        raise Http404("No flag matches the given query.")

    action = request.POST.get("action", "")
    reason = (request.POST.get("reason") or "").strip()[:2000]
    scope = request.POST.get("scope", "org")
    group = None
    if scope == "group":
        group = (
            flag.message.thread.group
            or Group.objects.filter(org=flag.org, pk=request.POST.get("group")).first()
        )

    if action == "warn":
        MemberSanction.objects.create(
            org=flag.org, user=flag.author, group=group,
            kind=MemberSanction.KIND_WARNING, reason=reason,
            flag=flag, issued_by=request.user,
        )
        _tell(flag.author, flag.org, "A warning from your organisation",
              reason or "Please keep it civil in the chat.")
        flash.success(request, f"{flag.author.display_name} has been warned.")

    elif action == "suspend":
        span = request.POST.get("span", "week")
        ends = timezone.now() + SPANS[span] if span in SPANS else None
        MemberSanction.objects.create(
            org=flag.org, user=flag.author, group=group,
            kind=MemberSanction.KIND_SUSPENSION, reason=reason,
            ends_at=ends, flag=flag, issued_by=request.user,
        )
        where = group.name if group else flag.org.name
        when = "until an admin lifts it" if ends is None else f"until {ends:%-d %B}"
        _tell(flag.author, flag.org, f"You can't post in {where} for now",
              (reason + " " if reason else "") + f"This lasts {when}.")
        flash.success(request, f"{flag.author.display_name} is suspended {when}.")

    elif action == "remove":
        removed = _remove_member(flag.author, flag.org, group)
        if removed:
            _tell(flag.author, flag.org,
                  f"You've been removed from {group.name if group else flag.org.name}",
                  reason or "An admin has removed you.")
            flash.success(request, f"{flag.author.display_name} has been removed.")
        else:
            flash.error(request, "That member could not be removed.")
    else:
        flash.error(request, "Pick what to do.")
        return redirect("manage:prefect_flag", flag_id=flag.id)

    flag.note = reason or flag.note
    flag.save(update_fields=["note"])
    _close(flag, request, ChatFlag.STATUS_ACTIONED)
    return redirect("manage:prefect")


def _remove_member(user, org, group=None):
    """Out of the group, or out of the organisation.

    The organisation's creator cannot be removed from it — an admin removing the
    owner would take the league away from the person who made it, and there is
    no undo for that anywhere in this product.
    """
    from orgs.models import GroupMember, OrgMember

    if group is not None:
        return bool(GroupMember.objects.filter(group=group, user=user).delete()[0])
    if org.created_by_id == user.id:
        return False
    return bool(OrgMember.objects.filter(org=org, user=user).delete()[0])


@org_admin_required
@require_POST
def prefect_lift(request, sanction_id: int):
    """Let somebody back in early. "Be able to uplift the suspension even if the
    time set is not met."

    Stamps lifted_at rather than deleting the row: an organisation asking "has
    this happened before" needs an answer that survives somebody being let back
    in.
    """
    mine = managed_orgs(request.user)
    sanction = get_object_or_404(
        MemberSanction.objects.select_related("user", "org", "group"), pk=sanction_id,
    )
    if not mine.filter(pk=sanction.org_id).exists():
        raise Http404("No suspension matches the given query.")
    if sanction.lifted_at is None:
        sanction.lifted_at = timezone.now()
        sanction.lifted_by = request.user
        sanction.save(update_fields=["lifted_at", "lifted_by"])
        _tell(sanction.user, sanction.org, "You can post again",
              "An admin has lifted your suspension.")
    flash.success(request, f"{sanction.user.display_name} can post again.")
    return redirect("manage:prefect")


@org_admin_required
@require_POST
def prefect_unlearn(request, allowance_id: int):
    """Take a phrase back off the allowance list.

    A decision that can only be made in one direction is a trap: an organisation
    that allowed a word in a lighter moment must be able to change its mind.
    """
    mine = managed_orgs(request.user)
    allowance = get_object_or_404(PrefectAllowance, pk=allowance_id)
    if not mine.filter(pk=allowance.org_id).exists():
        raise Http404("No allowance matches the given query.")
    phrase = allowance.phrase
    allowance.delete()
    flash.success(request, f"Prefect will raise “{phrase}” again.")
    return redirect("manage:prefect")


def _tell(user, org, title, message):
    """The person on the receiving end hears it from the product, in their bell.

    Not email: a suspension is not news that improves for arriving in an inbox
    at 3am, and the notification is where everything else about their
    organisation already reaches them.
    """
    Notification.objects.create(
        user=user, org=org, kind=Notification.KIND_ADMIN_NOTE,
        title=title, message=message,
    )
