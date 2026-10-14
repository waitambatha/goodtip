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


def custom_pricing_hub(request):
    _guard(request)
    pending = CustomPriceRequest.objects.filter(
        status=CustomPriceRequest.STATUS_PENDING,
    ).select_related("org", "requested_by")
    decided = CustomPriceRequest.objects.exclude(
        status=CustomPriceRequest.STATUS_PENDING,
    ).select_related("org", "requested_by", "decided_by")[:40]
    return render(request, "admin/hub/custom_pricing.html", _ctx(
        request, "Custom pricing",
        pending=pending, decided=decided,
        hub_title="Custom pricing",
        hub_lead="Organisations asking for a price outside the five published plans — "
                 "set one for them, or point them back at the five.",
        hub_icon="ic-f-org",
    ))


@require_POST
def custom_pricing_decide(request, request_id: int):
    _guard(request)
    req = get_object_or_404(CustomPriceRequest, pk=request_id)
    here = redirect("admin:hq_custom_pricing")
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
    from goodtip.mail import send_template, site_url

    if not req.requested_by.email:
        return
    subject = (
        f"A custom price for {req.org.name}" if template == "custom_price_approved"
        else f"About {req.org.name}'s custom price request"
    )
    try:
        send_template(
            template, subject=subject, to=[req.requested_by.email],
            context={"req": req, "org": req.org,
                     "plans_link": site_url(reverse("billing:plans", args=[req.org_id]))},
        )
    except Exception:  # noqa: BLE001 — the decision is saved either way
        import logging

        logging.getLogger(__name__).exception(
            "Custom price request %s decided, but the organisation's email failed", req.pk,
        )
