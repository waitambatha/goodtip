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


def _charts(total: int, confirmed: int, invited: int) -> dict:
    """The waiting list as pictures (client, 24 Oct 2026: the screen was
    "not quite giving... not self-explanatory"): the funnel from signing up
    to being invited, signups per day over the last 30 days, and who they
    are — what kind of organisation, and what they tip on now."""
    from datetime import timedelta

    from django.db.models import Count
    from django.db.models.functions import TruncDate

    def pct(n):
        return round(n / total * 100) if total else 0

    funnel = [
        {"label": "Signed up", "n": total, "pct": 100 if total else 0, "k": "var(--gt-c4)"},
        {"label": "Confirmed their email", "n": confirmed, "pct": pct(confirmed), "k": "var(--gt-c2)"},
        {"label": "Invited", "n": invited, "pct": pct(invited), "k": "var(--gt-c1)"},
    ]
    today = timezone.localdate()
    since = today - timedelta(days=29)
    per_day = dict(
        LaunchSignup.objects.filter(created_at__date__gte=since)
        .annotate(d=TruncDate("created_at")).values_list("d").annotate(n=Count("pk")).values_list("d", "n")
    )
    days = [{"d": since + timedelta(days=i), "n": per_day.get(since + timedelta(days=i), 0)} for i in range(30)]
    top = max([d["n"] for d in days] or [0]) or 1
    for d in days:
        d["h"] = round(d["n"] / top * 100)

    def split(field, choices):
        labels = dict(choices)
        rows = (LaunchSignup.objects.exclude(**{field: ""}).values_list(field)
                .annotate(n=Count("pk")).order_by("-n").values_list(field, "n"))
        rows = [{"label": labels.get(k, k), "n": n} for k, n in rows][:6]
        most = max([r["n"] for r in rows] or [1])
        for r in rows:
            r["pct"] = round(r["n"] / most * 100)
        return rows

    return {
        "funnel": funnel,
        "days": days,
        "last30": sum(d["n"] for d in days),
        "by_type": split("org_type", LaunchSignup.ORG_TYPE_CHOICES),
        "by_platform": split("current_platform", LaunchSignup.PLATFORM_CHOICES),
    }


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
    confirmed = LaunchSignup.objects.filter(email_verified_at__isnull=False).count()
    invited = LaunchSignup.objects.filter(notified_at__isnull=False).count()
    return render(request, "admin/hub/waitlist.html", _ctx(
        request, "Waiting list",
        rows=rows, page=page, show=show, q=q, filters=FILTERS,
        total=total,
        confirmed=confirmed,
        invited=invited,
        waiting=waitlist.audience().count(),
        **_charts(total, confirmed, invited),
        campaign=campaign,
        can_invite=access.can(request.user, "waitlist.invite"),
        hub_title="Waiting list",
        hub_lead="Everyone who asked to be told when GoodTip opens — and the "
                 "invitation that tells them it has.",
        hub_icon="ic-f-people",
    ))


def waitlist_person(request, pk: int):
    """One person on the list, the whole page (client, 24 Oct 2026: "click
    it and now that whole page is everything and anything to do with it"):
    their place, what they told us, each step from joining to being invited,
    their waiting-list account, and who joined either side of them."""
    from django.shortcuts import get_object_or_404

    _guard_view(request)
    person = get_object_or_404(LaunchSignup, pk=pk)
    ordered = list(LaunchSignup.objects.order_by("created_at", "pk").values_list("pk", flat=True))
    i = ordered.index(person.pk)
    near_ids = ordered[max(0, i - 3):i] + ordered[i + 1:i + 4]
    near = {r.pk: r for r in LaunchSignup.objects.filter(pk__in=near_ids)}
    places = {pk_: n for n, pk_ in enumerate(ordered, 1)}
    # Each stage carries what the hover card says about it (client, 24 Oct
    # 2026: "hover a stage and get more nice looking info").
    first = (person.name or "").split(" ")[0] or "They"
    steps = [
        {"label": "Joined the list", "icon": "ic-users", "at": person.created_at, "done": True,
         "big": f"Number {i + 1} in the queue",
         "detail": (f"{first} signed up from {person.source_page}." if person.source_page
                    else f"{first} signed up from the coming-soon page.")
                   + (f" They run a {person.org_type_label.lower()}." if person.org_type_label else "")},
        {"label": "Confirmed their email", "icon": "ic-mail", "at": person.email_verified_at,
         "done": bool(person.email_verified_at),
         "big": "Address confirmed" if person.email_verified_at else "Waiting on the code",
         "detail": ("Their address is real, so the launch invitation can reach them."
                    if person.email_verified_at else
                    "They haven't entered the code we emailed. Only confirmed addresses are invited — an "
                    "unconfirmed one may be a typo.")},
        {"label": "Signed in to their place", "icon": "ic-login", "at": person.last_signin_at,
         "done": bool(person.last_signin_at),
         "big": "Came back to check" if person.last_signin_at else "Not signed in yet",
         "detail": ("They set a password and have looked at their place in the queue since joining."
                    if person.last_signin_at else
                    "They can sign in from the coming-soon page to see their place and the countdown. Optional.")},
        {"label": "Invited to GoodTip", "icon": "ic-send", "at": person.notified_at, "done": bool(person.notified_at),
         "big": "Told we're open" if person.notified_at else "Next: the invitation",
         "detail": ("The launch invitation went to them. From here they sign up for real."
                    if person.notified_at else
                    "They'll get the launch invitation when it goes out — once, and only if confirmed.")},
    ]
    done = sum(1 for st in steps if st["done"])
    for n, st in enumerate(steps):
        st["next"] = not st["done"] and all(x["done"] for x in steps[:n])
    progress = max(0, max([n for n, st in enumerate(steps) if st["done"]] or [0])) / (len(steps) - 1)
    return render(request, "admin/hub/waitlist_person.html", _ctx(
        request, person.name,
        person=person, place=i + 1, total=len(ordered), steps=steps, done=done, progress=progress,
        neighbours=[(places[pk_], near[pk_]) for pk_ in near_ids if pk_ in near],
        prev_id=ordered[i - 1] if i > 0 else None,
        next_id=ordered[i + 1] if i + 1 < len(ordered) else None,
        back_url=reverse("admin:hq_waitlist"), back_label="the waiting list",
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
        backgrounds=waitlist.invitation_backgrounds(),
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
    # Back onto the tab the button was on, not always the first one.
    tab = {"send_now": "send", "schedule": "send", "unschedule": "send", "launch_date": "launch"}.get(action, "write")
    here = redirect(reverse("admin:hq_waitlist_invitation") + f"#{tab}")

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
    campaign.background = waitlist.valid_background(request.POST.get("background") or "")
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
    campaign.background = waitlist.valid_background(request.POST.get("background") or "")
    sample = LaunchSignup(name="Alex Sample", email="alex@example.com")
    msg = waitlist.build_invitation(campaign, sample)
    html = ""
    if msg is not None:
        html = next((c for c, m in msg.alternatives if m == "text/html"), "")
    return HttpResponse(html or "<p>The preview could not be drawn.</p>")
