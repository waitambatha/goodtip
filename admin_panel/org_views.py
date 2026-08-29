"""Organisation-admin pages that did not exist before the /manage/ split:
Messages, and the org's own Charity.

Both answer the same complaint from the client — that things an organisation
should decide for itself were being decided for it. A member had nowhere to
raise anything with their own admin (the public contact form goes to GoodTip,
not to the organisation), and a charity could only be added by whoever ran the
platform.
"""
from django.contrib import messages as flash
from django.db.models import Count, Max
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from orgs.models import Message, MessageThread, OrgMember, Organisation

from .perms import get_managed_org_or_404, managed_orgs, org_admin_required


def _pick_org(request, mine):
    """Which organisation the page is about.

    Most people run exactly one, so asking every time would be noise. `?org=`
    picks when there are several; otherwise the first. Always resolved against
    `mine`, so the parameter cannot reach somebody else's organisation.
    """
    requested = request.GET.get("org") or request.POST.get("org")
    if requested:
        org = mine.filter(pk=requested).first()
        if org is None:
            raise Http404("No organisation matches the given query.")
        return org
    return mine.first()


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------

@org_admin_required
def message_list(request):
    mine = managed_orgs(request.user)
    org = _pick_org(request, mine)
    if org is None:
        return render(request, "manage/messages.html", {"org": None, "orgs": mine})

    threads = (
        MessageThread.objects.filter(org=org)
        .select_related("started_by", "group")
        .annotate(reply_count=Count("messages"), last_at=Max("messages__created_at"))
        .order_by("-last_message_at")
    )
    tab = request.GET.get("tab") or "raised"
    if tab == "notices":
        threads = threads.filter(kind=MessageThread.KIND_NOTICE)
    elif tab == "closed":
        threads = threads.filter(status=MessageThread.STATUS_CLOSED)
    else:
        threads = threads.filter(kind=MessageThread.KIND_RAISED).exclude(
            status=MessageThread.STATUS_CLOSED,
        )

    return render(request, "manage/messages.html", {
        "org": org, "orgs": mine, "threads": threads, "tab": tab,
        "open_count": MessageThread.objects.filter(
            org=org, kind=MessageThread.KIND_RAISED, status=MessageThread.STATUS_OPEN,
        ).count(),
    })


@org_admin_required
def message_new(request):
    """An admin sending something out — to everyone, or to named people."""
    mine = managed_orgs(request.user)
    org = _pick_org(request, mine)
    if org is None:
        raise Http404("No organisation matches the given query.")

    if request.method == "POST":
        subject = (request.POST.get("subject") or "").strip()
        body = (request.POST.get("body") or "").strip()
        if not subject or not body:
            flash.error(request, "A notice needs both a subject and something to say.")
        else:
            thread = MessageThread.objects.create(
                org=org, kind=MessageThread.KIND_NOTICE, subject=subject,
                started_by=request.user, status=MessageThread.STATUS_OPEN,
            )
            # Recipient ids are filtered through the org's own membership, so a
            # posted id from outside it is dropped rather than trusted.
            picked = request.POST.getlist("recipients")
            if picked:
                member_users = OrgMember.objects.filter(
                    org=org, user_id__in=picked,
                ).values_list("user_id", flat=True)
                thread.recipients.set(list(member_users))
            Message.objects.create(thread=thread, author=request.user, body=body)
            who = "everyone" if not picked else f"{len(picked)} member(s)"
            flash.success(request, f"Sent to {who}.")
            return redirect("manage:message_thread", thread_id=thread.id)

    members = (
        OrgMember.objects.filter(org=org).select_related("user").order_by("user__display_name")
    )
    return render(request, "manage/message_new.html", {
        "org": org, "orgs": mine, "members": members,
    })


@org_admin_required
def message_thread(request, thread_id: int):
    mine = managed_orgs(request.user)
    thread = get_object_or_404(
        MessageThread.objects.select_related("org", "started_by"),
        pk=thread_id, org__in=mine,
    )

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "close":
            thread.status = MessageThread.STATUS_CLOSED
            thread.save(update_fields=["status"])
            flash.success(request, "Marked as closed.")
        elif action == "reopen":
            thread.status = MessageThread.STATUS_OPEN
            thread.save(update_fields=["status"])
        else:
            body = (request.POST.get("body") or "").strip()
            if body:
                Message.objects.create(thread=thread, author=request.user, body=body)
                if thread.status == MessageThread.STATUS_OPEN:
                    thread.status = MessageThread.STATUS_ANSWERED
                    thread.save(update_fields=["status"])
                flash.success(request, "Reply sent.")
        return redirect("manage:message_thread", thread_id=thread.id)

    entries = thread.messages.select_related("author").order_by("created_at")
    # Opening the thread is what marks it read for this admin.
    for entry in entries:
        entry.read_by.add(request.user)

    return render(request, "manage/message_thread.html", {
        "org": thread.org, "orgs": mine, "thread": thread, "entries": entries,
        "recipients": thread.recipients.all(),
    })


# ---------------------------------------------------------------------------
# Charity — a signpost, not a second implementation
# ---------------------------------------------------------------------------

@org_admin_required
def charity_redirect(request):
    """Send the rail's Charity link to the page that already does this.

    The organisation's own charity list — the vetted pool plus its own
    additions — is orgs.views.org_charities_view, and the creation wizard
    already stopped offering a free-text charity box. All that was missing was
    a way to reach it from the org admin's menu, which needs an org id the rail
    does not have.
    """
    mine = managed_orgs(request.user)
    org = _pick_org(request, mine)
    if org is None:
        return render(request, "manage/no_org.html", {"orgs": mine})
    return redirect("orgs:charities", org_id=org.id)
