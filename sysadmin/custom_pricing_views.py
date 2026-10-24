"""Custom pricing requests — organisations asking for a price outside the
five published plans, and the super admin's answer to them.

CLIENT, 11 OCT 2026: "the super admin will be able to create a custom price
for that specific organisation... or deny it and say we can work with the
five subscriptions we currently have."

One screen, like waitlist_views.py's queue rather than ChangeRequest's
hold-and-replay-a-POST machinery: there is nothing to replay here, only a
price to set once and an email to send. Approving writes straight onto the
CustomPriceRequest row, which billing.services.current_custom_offer then
reads back on the organisation's own Plans page.
"""
from decimal import Decimal, InvalidOperation

from django.contrib import admin, messages
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from billing.models import CustomPriceRequest

from . import access


def _ctx(request, title, **extra):
    ctx = admin.site.each_context(request)
    ctx.update(title=title, **extra)
    return ctx


def _guard(request):
    if not access.can(request.user, "billing.custom_pricing"):
        raise Http404("No custom pricing queue here.")


def _age(dt):
    """How long something has waited, as a word the queue can colour."""
    hours = (timezone.now() - dt).total_seconds() / 3600
    return "fresh" if hours < 24 else "aging" if hours < 72 else "late"


#: The queue's filters — one per place a request can be in, then all of them.
FILTERS = [
    ("waiting", "Waiting for you"),
    ("offer", "Offers out"),
    ("accepted", "Accepted"),
    ("closed", "Ended in a no"),
    ("all", "Everything"),
    ("packages", "Packages taken"),
]


def _filtered(show: str) -> list:
    rows = list(
        CustomPriceRequest.objects.select_related("org", "requested_by", "decided_by")
        .prefetch_related("thread").order_by("-created_at")
    )
    if show == "waiting":
        rows = sorted((r for r in rows if r.stage == "waiting"), key=lambda r: r.created_at)  # oldest first
    elif show == "offer":
        rows = [r for r in rows if r.stage == "offer"]
    elif show == "accepted":
        rows = [r for r in rows if r.stage == "accepted"]
    elif show == "closed":
        rows = [r for r in rows if r.stage in ("declined", "refused")]
    return rows


def _stats(rows) -> dict:
    answered = [r for r in rows if r.decided_at]
    hours = [(r.decided_at - r.created_at).total_seconds() / 3600 for r in answered]
    avg = sum(hours) / len(hours) if hours else None
    return {
        "waiting": sum(1 for r in rows if r.stage == "waiting"),
        "offer": sum(1 for r in rows if r.stage == "offer"),
        "accepted": sum(1 for r in rows if r.stage == "accepted"),
        "closed": sum(1 for r in rows if r.stage in ("declined", "refused")),
        "all": len(rows),
        "avg_reply": "—" if avg is None else "<1h" if avg < 1 else f"{avg:.0f}h" if avg < 72 else f"{avg / 24:.0f}d",
    }


def _waited(dt) -> str:
    """"8d", "5h", "12m" — short enough to sit on the scoreboard."""
    secs = (timezone.now() - dt).total_seconds()
    if secs >= 86400:
        return f"{int(secs // 86400)}d"
    if secs >= 3600:
        return f"{int(secs // 3600)}h"
    return f"{max(1, int(secs // 60))}m"


def custom_pricing_hub(request):
    """The dashboard (rebuilt again 24 Oct 2026: "let it be where i see one at
    a time... its own dashboard where i click... if its a request click it
    and now that whole page is everything and anything to do with it").
    Tiles only — who, how big, where it stands, the last word said — each
    opening its own page (custom_pricing_detail). Waiting requests show
    first, oldest first, since those are the ones that need someone."""
    from billing.models import CustomPackageClaim

    _guard(request)
    everything = _filtered("all")
    stats = _stats(everything)
    show = request.GET.get("show") or ("waiting" if stats["waiting"] else "all")
    if show not in dict(FILTERS):
        show = "all"
    q = (request.GET.get("q") or "").strip()
    rows = [] if show == "packages" else _filtered(show)
    if q:
        ql = q.lower()
        rows = [r for r in rows if ql in r.org.name.lower() or ql in (r.requested_by.email or "").lower()
                or ql in (r.requested_by.display_name or "").lower()]
    for r in rows:
        r.age = _age(r.created_at)
        last = list(r.thread.all())
        r.last_word = last[-1].body if last else (r.decision_note or r.org_decline_reason or r.message)
        r.last_side = ("team" if last[-1].from_team else "org") if last else ("team" if r.decided_at else "org")
    return render(request, "admin/hub/custom_pricing.html", _ctx(
        request, "Custom pricing",
        rows=rows, stats=stats, show=show, filters=FILTERS, q=q,
        claims=CustomPackageClaim.objects.select_related("package", "org", "requested_by")[:50],
        hub_title="Custom pricing",
        hub_lead="Organisations asking for a price outside the five plans. Open one to see everything about it "
                 "and answer it.",
        hub_icon="ic-f-org",
    ))


def custom_pricing_detail(request, request_id: int):
    """One request, the whole page: the ask, the organisation behind it, the
    conversation, and the answer — and the way to the next one."""
    from billing import custom_pricing
    from billing.models import CustomPackageClaim, PlanSubscription
    from orgs.models import OrgMember
    from orgs.services import org_admin_users

    _guard(request)
    req = get_object_or_404(
        CustomPriceRequest.objects.select_related("org", "requested_by", "decided_by").prefetch_related("thread__author"),
        pk=request_id,
    )
    members = OrgMember.objects.filter(org=req.org).count()
    size = req.people_count or members
    # Next and previous within the same queue the person came from.
    show = request.GET.get("show") or "all"
    if show not in dict(FILTERS) or show == "packages":
        show = "all"
    ids = [r.pk for r in _filtered(show)]
    if req.pk not in ids:
        show, ids = "all", [r.pk for r in _filtered("all")]
    i = ids.index(req.pk)
    return render(request, "admin/hub/custom_pricing_detail.html", _ctx(
        request, req.org.name,
        req=req,
        events=custom_pricing.timeline(req),
        steps=custom_pricing.steps_for(req),
        std=custom_pricing.standard_equivalent(size),
        size_for_std=size,
        members=members,
        runners=org_admin_users(req.org),
        past_plans=PlanSubscription.objects.filter(org=req.org).order_by("-created_at")[:6],
        other_requests=CustomPriceRequest.objects.filter(org=req.org).exclude(pk=req.pk).order_by("-created_at"),
        claims=CustomPackageClaim.objects.filter(org=req.org).select_related("package"),
        age=_age(req.created_at),
        waited=_waited(req.created_at),
        show=show, position=i + 1, count=len(ids),
        prev_id=ids[i - 1] if i > 0 else None,
        next_id=ids[i + 1] if i + 1 < len(ids) else None,
        back_url=reverse("admin:hq_custom_pricing") + f"?show={show}",
        back_label="custom pricing",
    ))


@require_POST
def custom_pricing_reply(request, request_id: int):
    """The team writing back on a request without deciding it — a question,
    or a word about an offer already made (client, 24 Oct 2026)."""
    from billing import custom_pricing

    _guard(request)
    req = get_object_or_404(CustomPriceRequest, pk=request_id)
    body = (request.POST.get("body") or "").strip()
    if body:
        custom_pricing.post_message(req, author=request.user, body=body, from_team=True)
        messages.success(request, f"Sent to {req.org.name} — they'll see it in their notifications and by email.")
    return redirect("admin:hq_custom_pricing_detail", request_id=req.pk)


@require_POST
def custom_pricing_decide(request, request_id: int):
    _guard(request)
    req = get_object_or_404(CustomPriceRequest, pk=request_id)
    here = redirect("admin:hq_custom_pricing_detail", request_id=req.pk)
    if not req.is_pending:
        messages.error(request, "That request has already been decided.")
        return here

    action = request.POST.get("action")
    note = (request.POST.get("decision_note") or "").strip()[:2000]

    if action == "decline":
        req.status = CustomPriceRequest.STATUS_DECLINED
        req.decision_note = note
        req.decided_at = timezone.now()
        req.decided_by = request.user
        req.save(update_fields=["status", "decision_note", "decided_at", "decided_by"])
        _notify_org(req, template="custom_price_declined")
        messages.success(request, f"Declined. {req.org.name} has been told.")
        return here

    if action == "approve":
        price_raw = (request.POST.get("price_aud") or "").strip()
        seats_raw = (request.POST.get("seat_limit") or "").strip()
        label = (request.POST.get("label") or "").strip()[:80] or f"{req.org.name} custom plan"
        try:
            price = Decimal(price_raw)
            seats = int(seats_raw)
            if price <= 0 or seats <= 0:
                raise ValueError
        except (InvalidOperation, ValueError):
            messages.error(request, "Give a real price and a real number of people before approving.")
            return here

        req.status = CustomPriceRequest.STATUS_APPROVED
        req.price_aud = price
        req.seat_limit = seats
        req.label = label
        req.decision_note = note
        req.decided_at = timezone.now()
        req.decided_by = request.user
        req.save(update_fields=[
            "status", "price_aud", "seat_limit", "label", "decision_note", "decided_at", "decided_by",
        ])
        _notify_org(req, template="custom_price_approved")
        messages.success(request, f"Approved — ${price:,.0f}/yr for up to {seats} people. {req.org.name} has been told.")
        return here

    messages.error(request, "That wasn't something this screen can do.")
    return here


def _notify_org(req: CustomPriceRequest, *, template: str) -> None:
    """The decision, in the organisation's bell panel and by email (billing.
    custom_pricing.tell_org) — before 24 Oct 2026 this was email only, so an
    answer could sit unread with nothing in the app to say it had come."""
    from billing import custom_pricing

    if template == "custom_price_approved":
        title = f"Your custom price is ready — {req.label}"
        message = f"${req.price_aud:,.0f} a year for up to {req.seat_limit} people. Accept it, decline it, or reply."
        subject = f"A custom price for {req.org.name}"
    else:
        title = "The GoodTip team answered your custom price request"
        message = req.decision_note or "For now one of the five plans is the best fit."
        subject = f"About {req.org.name}'s custom price request"
    custom_pricing.tell_org(req, title=title, message=message, email_template=template, subject=subject)
