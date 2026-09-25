"""The waiting list, and the one email that tells it GoodTip is open.

CLIENT, 25 SEP 2026: "in our super admin we will need to create the waitlist
that will monitor the people that sign up in the waiting list and also give the
super admin a functionality of sending them an invitation email all at once
that will tell them that the system is now fully completed and they can sign in
now ... also give him the chance to schedule it, so it can auto send, and also
you will draft the message that will be sent but if he also wants to update the
wordings and edit that message so the email can go as it is."

Two screens. The list answers "who is waiting and how many have I told"; the
invitation is a composer with the drafted wording in it, a live preview of the
real email, a test send to yourself, and the two ways of sending it.

TWO CAPABILITIES, because looking and mailing are different jobs. `waitlist.view`
opens the list; `waitlist.invite` edits and sends, and is marked sensitive — the
email goes to real inboxes and cannot be recalled.

SENDING IS SAFE TO REPEAT. See accounts.waitlist.send_campaign: a person is
stamped invited before they are mailed, so a double click, an overlapping timer
run or a retry after a crash cannot mail anybody twice.
"""
import csv
from datetime import datetime

from django.conf import settings
from django.contrib import admin, messages
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from accounts import waitlist
from accounts.models import LaunchSignup, WaitlistCampaign

from . import access

PER_PAGE = 25
#: How long a click on "Send now" works before handing the rest to the timer.
#: A web request has to finish inside the gunicorn timeout; the jobs command has
#: no such limit and picks up wherever this stopped.
SEND_NOW_BUDGET = 20

FILTERS = [
    ("all", "Everyone"),
    ("waiting", "Not invited yet"),
    ("invited", "Invited"),
    ("verified", "Confirmed email"),
    ("unconfirmed", "Unconfirmed"),
]


def _ctx(request, title, **extra):
    ctx = admin.site.each_context(request)
    ctx.update(title=title, **extra)
    return ctx


def _guard_view(request):
    if not access.can(request.user, "waitlist.view"):
        raise Http404("No waiting list here.")


def _guard_invite(request):
    if not access.can(request.user, "waitlist.invite"):
        raise Http404("No invitation here.")


def _filtered(request):
    show = request.GET.get("show") or "all"
    if show not in dict(FILTERS):
        show = "all"
    q = (request.GET.get("q") or "").strip()
    qs = LaunchSignup.objects.all()
    if show == "waiting":
        qs = qs.filter(notified_at__isnull=True)
    elif show == "invited":
        qs = qs.filter(notified_at__isnull=False)
    elif show == "verified":
        qs = qs.filter(email_verified_at__isnull=False)
    elif show == "unconfirmed":
        qs = qs.filter(email_verified_at__isnull=True)
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(email__icontains=q))
    return qs.order_by("created_at", "pk"), show, q


def _places() -> dict:
    """pk -> place in the whole queue (1 = first to sign up)."""
    ordered = LaunchSignup.objects.order_by("created_at", "pk").values_list("pk", flat=True)
    return {pk: n for n, pk in enumerate(ordered, 1)}


def waitlist_hub(request):
    _guard_view(request)
    qs, show, q = _filtered(request)

    if request.GET.get("export") == "csv":
        resp = HttpResponse(content_type="text/csv")
        resp["Content-Disposition"] = 'attachment; filename="goodtip-waiting-list.csv"'
        w = csv.writer(resp)
        w.writerow(["Place", "Name", "Email", "Organisation type", "Tipping with", "Email confirmed",
                    "Signed up", "Invited", "Source"])
        places = _places()
        for r in qs:
            w.writerow([
                places[r.pk], r.name, r.email, r.org_type_label, r.platform_label,
                "yes" if r.is_verified else "no",
                timezone.localtime(r.created_at).strftime("%Y-%m-%d %H:%M"),
                timezone.localtime(r.notified_at).strftime("%Y-%m-%d %H:%M") if r.notified_at else "",
                r.source_page,
            ])
        return resp

    total = LaunchSignup.objects.count()
    page = Paginator(qs, PER_PAGE).get_page(request.GET.get("page"))
    # Positions are the person's place in the whole queue, not in this filter.
    places = _places() if page.object_list else {}
    rows = [(places.get(r.pk), r) for r in page.object_list]

    campaign = WaitlistCampaign.current()
    return render(request, "admin/hub/waitlist.html", _ctx(
        request, "Waiting list",
        rows=rows, page=page, show=show, q=q, filters=FILTERS,
        total=total,
        confirmed=LaunchSignup.objects.filter(email_verified_at__isnull=False).count(),
        invited=LaunchSignup.objects.filter(notified_at__isnull=False).count(),
        waiting=waitlist.audience().count(),
        campaign=campaign,
        can_invite=access.can(request.user, "waitlist.invite"),
        hub_title="Waiting list",
        hub_lead="Everyone who asked to be told when GoodTip opens — and the "
                 "invitation that tells them it has.",
        hub_icon="ic-f-people",
    ))


def _read_fields(post):
    return {
        "subject": (post.get("subject") or "").strip()[:200],
        "heading": (post.get("heading") or "").strip()[:120],
        "body": (post.get("body") or "").replace("\r\n", "\n").strip(),
        "button_label": (post.get("button_label") or "").strip()[:60],
    }


def _parse_when(raw):
    """The datetime-local box, read in the site's own timezone (Sydney)."""
    try:
        naive = datetime.strptime((raw or "").strip(), "%Y-%m-%dT%H:%M")
    except ValueError:
        return None
    return timezone.make_aware(naive, timezone.get_current_timezone())


def invitation(request):
    _guard_view(request)
    campaign = WaitlistCampaign.current()
    can_invite = access.can(request.user, "waitlist.invite")

    if request.method == "POST":
        _guard_invite(request)
        return _invitation_action(request, campaign)

    return render(request, "admin/hub/waitlist_invitation.html", _ctx(
        request, "Launch invitation",
        campaign=campaign, can_invite=can_invite,
        waiting=waitlist.audience().count(),
        default_body=waitlist.DEFAULT_BODY,
        scheduled_local=(timezone.localtime(campaign.scheduled_for).strftime("%Y-%m-%dT%H:%M")
                         if campaign.scheduled_for else ""),
        launch_local=(timezone.localtime(campaign.launch_at).strftime("%Y-%m-%dT%H:%M")
                      if campaign.launch_at else ""),
        tz_name=str(timezone.get_current_timezone()),
        holding_on=getattr(settings, "HOLDING_PAGE", False),
        is_staging=getattr(settings, "IS_STAGING", False),
        signup_link=waitlist.invitation_link(),
        back_url=reverse("admin:hq_waitlist"),
        back_label="the waiting list",
        hub_title="Launch invitation",
        hub_lead="The email that tells the list GoodTip is open. Edit it, "
                 "check it, then send it now or set a time.",
        hub_icon="ic-f-mail",
    ))


def _invitation_action(request, campaign):
    action = request.POST.get("action") or "save"
    fields = _read_fields(request.POST)
    here = redirect("admin:hq_waitlist_invitation")

    if action == "reset":
        for k, v in waitlist.default_campaign_fields().items():
            setattr(campaign, k, v)
        campaign.updated_by = request.user
        campaign.save()
        messages.success(request, "The wording is back to the original draft.")
        return here

    if action == "unschedule":
        if campaign.status in (WaitlistCampaign.STATUS_SCHEDULED, WaitlistCampaign.STATUS_SENDING):
            campaign.status = WaitlistCampaign.STATUS_DRAFT
            campaign.scheduled_for = None
            campaign.save()
            messages.success(request, "Nothing more will be sent. It is a draft again.")
        return here

    if action == "launch_date":
        raw = (request.POST.get("launch_at") or "").strip()
        if not raw:
            campaign.launch_at = None
            campaign.save()
            messages.success(request, "The launch date is cleared. The countdown is hidden.")
            return here
        when = _parse_when(raw)
        if when is None:
            messages.error(request, "That launch date wasn't understood. Choose a date and time.")
            return here
        campaign.launch_at = when
        campaign.save()
        messages.success(
            request,
            f"Launch date set: {timezone.localtime(when):%A %d %B, %-I:%M %p}. "
            f"The waiting list now sees a countdown to it on their page.",
        )
        return here

    # Everything else may carry edits, so validate and store them first.
    if not all(fields.values()):
        messages.error(request, "The subject, heading, message and button label can't be empty.")
        return here
    for k, v in fields.items():
        setattr(campaign, k, v)
    campaign.updated_by = request.user

    if action == "save":
        campaign.save()
        messages.success(request, "Saved. Nothing has been sent.")
        return here

    if action == "test":
        campaign.save()
        sample = LaunchSignup(name=request.user.display_name or "Test", email=request.user.email)
        msg = waitlist.build_invitation(campaign, sample, to=request.user.email)
        from goodtip.mail import send_bulk

        if msg is not None and send_bulk([msg]):
            messages.success(request, f"A test copy went to {request.user.email}. Nobody on the list was emailed.")
        else:
            messages.error(request, "The test email did not go — check the mail settings.")
        return here

    waiting = waitlist.audience().count()

    if action == "schedule":
        when = _parse_when(request.POST.get("scheduled_for"))
        if when is None:
            messages.error(request, "Choose a date and time to send.")
            return here
        if when <= timezone.now():
            messages.error(request, "That time has already passed. Choose one in the future.")
            return here
        if not waiting:
            messages.error(request, "Everyone on the list has already been invited.")
            return here
        if request.POST.get("confirm") != "yes":
            messages.error(request, "Tick the box to confirm you want this to go out.")
            return here
        campaign.status = WaitlistCampaign.STATUS_SCHEDULED
        campaign.scheduled_for = when
        campaign.finished_at = None
        campaign.save()
        messages.success(
            request,
            f"Scheduled for {timezone.localtime(when):%A %d %B, %-I:%M %p}. The invitation goes to "
            f"everyone not yet invited then (about {waiting} today) — within ten minutes of that time.",
        )
        return here

    if action == "send_now":
        if not waiting:
            messages.error(request, "Everyone on the list has already been invited.")
            return here
        if request.POST.get("confirm") != "yes":
            messages.error(request, "Tick the box to confirm you want this to go out.")
            return here
        campaign.scheduled_for = None
        campaign.save()
        sent = waitlist.send_campaign(campaign, budget_seconds=SEND_NOW_BUDGET)
        campaign.refresh_from_db()
        left = waitlist.audience().count()
        if campaign.status == WaitlistCampaign.STATUS_SENDING and left:
            messages.warning(
                request,
                f"{sent} sent so far and {left} still to go. The rest is sent automatically "
                f"within the next ten minutes; you don't need to do anything.",
            )
        else:
            messages.success(request, f"Sent to {sent} {'person' if sent == 1 else 'people'}.")
        return here

    messages.error(request, "That wasn't something this page can do.")
    return here


@require_POST
def invitation_preview(request):
    """The real email, rendered from what is in the boxes right now. Saves nothing."""
    _guard_view(request)
    fields = _read_fields(request.POST)
    campaign = WaitlistCampaign(**{k: v or "…" for k, v in fields.items()})
    sample = LaunchSignup(name="Alex Sample", email="alex@example.com")
    msg = waitlist.build_invitation(campaign, sample)
    html = ""
    if msg is not None:
        html = next((c for c, m in msg.alternatives if m == "text/html"), "")
    return HttpResponse(html or "<p>The preview could not be drawn.</p>")
