import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from orgs.models import OrgMember, Organisation

from . import services
from .models import PlanSubscription
from .pricing import TIERS, seat_limit_label

logger = logging.getLogger(__name__)


def _require_owner(user, org):
    return OrgMember.objects.filter(
        user=user, org=org, is_league_owner=True
    ).exists()


def _tier_cards():
    cards = []
    for key, cfg in TIERS.items():
        cards.append({
            "key": key,
            "label": cfg["label"],
            "price": cfg["price"],
            "seat_label": seat_limit_label(cfg["seat_limit"]),
            "audience": cfg["audience"],
            "features": cfg["features"],
            "popular": cfg["popular"],
        })
    return cards


@login_required
def plans_view(request, org_id: int):
    org = get_object_or_404(Organisation, pk=org_id)
    if not _require_owner(request.user, org):
        return HttpResponseForbidden()
    return render(request, "billing/plans.html", {
        "org": org,
        "tiers": _tier_cards(),
        "current": services.active_subscription(org),
        "stripe_configured": services.is_configured(),
    })


@login_required
@require_POST
def checkout_view(request, org_id: int):
    org = get_object_or_404(Organisation, pk=org_id)
    if not _require_owner(request.user, org):
        return HttpResponseForbidden()
    tier = request.POST.get("tier")
    if tier not in TIERS:
        messages.error(request, "Please choose a plan.")
        return redirect("billing:plans", org_id=org.id)

    if not services.is_configured():
        messages.error(
            request,
            "Payments aren't switched on yet — Stripe keys haven't been added. "
            "Your plan choice is saved; check back once billing is configured.",
        )
        services.create_subscription(org, tier)
        return redirect("billing:plans", org_id=org.id)

    sub = services.create_subscription(org, tier)
    success_url = request.build_absolute_uri(
        reverse("billing:success", args=[org.id])
    ) + "?session_id={CHECKOUT_SESSION_ID}"
    cancel_url = request.build_absolute_uri(reverse("billing:plans", args=[org.id]))
    try:
        session = services.create_checkout_session(
            sub, success_url=success_url, cancel_url=cancel_url
        )
    except Exception:  # noqa: BLE001 — surface any Stripe error gracefully
        logger.exception("Stripe checkout session creation failed")
        messages.error(request, "Couldn't start checkout. Please try again.")
        return redirect("billing:plans", org_id=org.id)
    return redirect(session.url)


@login_required
def success_view(request, org_id: int):
    org = get_object_or_404(Organisation, pk=org_id)
    if not _require_owner(request.user, org):
        return HttpResponseForbidden()
    sub = services.active_subscription(org)
    # The webhook is the source of truth; this page may render a moment before
    # it lands, so we just reassure the owner either way.
    return render(request, "billing/success.html", {"org": org, "sub": sub})


@csrf_exempt
@require_POST
def stripe_webhook(request):
    sig_header = request.META.get("HTTP_STRIPE_SIGNATURE", "")
    try:
        event = services.construct_webhook_event(request.body, sig_header)
    except services.BillingNotConfigured:
        return HttpResponse(status=503)
    except Exception:  # noqa: BLE001 — invalid signature/payload
        logger.warning("Invalid Stripe webhook payload/signature")
        return HttpResponse(status=400)

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        sub_id = (session.get("metadata") or {}).get("subscription_id") or session.get("client_reference_id")
        if sub_id:
            sub = PlanSubscription.objects.filter(pk=sub_id).first()
            if sub:
                services.mark_paid(sub, payment_intent_id=session.get("payment_intent") or "")
    return HttpResponse(status=200)
