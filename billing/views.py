import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from orgs.models import OrgMember, Organisation

from . import donations, services
from .forms import DonationPledgeForm, TopUpForm
from .models import DonationPledge, PlanSubscription
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
def pledge_view(request, org_id: int):
    org = get_object_or_404(Organisation, pk=org_id)
    if not _require_owner(request.user, org):
        return HttpResponseForbidden()

    pledge = donations.get_pledge(org)
    if request.method == "POST":
        form = DonationPledgeForm(request.POST)
        if form.is_valid():
            donations.set_pledge(
                org,
                pledged_amount=form.cleaned_data["pledged_amount"],
                payment_schedule=form.cleaned_data["payment_schedule"],
                matching_enabled=form.cleaned_data["matching_enabled"],
                matching_cap=form.cleaned_data["matching_cap"],
            )
            messages.success(request, "Donation pledge saved — participants can see it now.")
            return redirect("dashboard")
    else:
        initial = {}
        if pledge:
            initial = {
                "pledged_amount": pledge.pledged_amount_aud,
                "payment_schedule": pledge.payment_schedule,
                "matching_enabled": pledge.matching_enabled,
                "matching_cap": pledge.matching_cap_aud,
            }
        form = DonationPledgeForm(initial=initial)

    return render(request, "billing/pledge.html", {
        "org": org,
        "form": form,
        "pledge": pledge,
        "suggested_minimum": donations.suggested_minimum(org),
    })


@login_required
def topup_view(request, org_id: int):
    """The 'Add to the cause' flow — optional participant donation (deck slide 6)."""
    org = get_object_or_404(Organisation, pk=org_id)
    if not OrgMember.objects.filter(user=request.user, org=org).exists():
        return HttpResponseForbidden()

    summary = donations.donation_summary(org)
    if summary is None:
        messages.info(request, "This league hasn't set a donation pledge yet.")
        return redirect("dashboard")

    pledge = summary["pledge"]
    if request.method == "POST":
        form = TopUpForm(request.POST)
        if form.is_valid():
            amount = form.cleaned_data["amount"]
            if not services.is_configured():
                # Phase 1 without a gateway: record and hold (deck: held in Pty Ltd).
                result = donations.record_topup(pledge, participant=request.user, amount=amount)
                _flash_topup(request, result)
                return redirect("tipping:leaderboard", org_id=org.id)
            success_url = request.build_absolute_uri(
                reverse("tipping:leaderboard", args=[org.id])
            )
            cancel_url = request.build_absolute_uri(reverse("billing:topup", args=[org.id]))
            try:
                session = services.create_donation_checkout_session(
                    pledge, request.user, amount,
                    success_url=success_url, cancel_url=cancel_url,
                )
            except Exception:  # noqa: BLE001 — surface any Stripe error gracefully
                logger.exception("Stripe donation session creation failed")
                messages.error(request, "Couldn't start checkout. Please try again.")
                return redirect("billing:topup", org_id=org.id)
            return redirect(session.url)
    else:
        form = TopUpForm()

    return render(request, "billing/topup.html", {
        "org": org, "form": form, "summary": summary,
    })


def _flash_topup(request, result):
    if result["matched"] > 0:
        messages.success(
            request,
            f"Thank you! Your ${result['topup']:.0f} became "
            f"${result['topup'] + result['matched']:.0f} thanks to your organisation.",
        )
    else:
        messages.success(request, f"Thank you! Your ${result['topup']:.0f} donation is in.")


@login_required
def season_summary_view(request, org_id: int):
    """Season close: announce the winner and settle the donation pool (deck slide 4)."""
    org = get_object_or_404(Organisation, pk=org_id)
    member = OrgMember.objects.filter(user=request.user, org=org).first()
    if member is None or not member.can_manage:
        return HttpResponseForbidden()

    if request.method == "POST":
        try:
            donations.create_disbursement(org)
            messages.success(request, "Season closed — donation pool settled for disbursement.")
        except ValueError as e:
            messages.error(request, str(e))
        return redirect("billing:season_summary", org_id=org.id)

    from tipping.services import leaderboard_for_org

    from . import esg

    board = list(leaderboard_for_org(org)[:3])
    winner = board[0] if board else None
    disbursement = org.disbursements.filter(season=org.season).first()
    return render(request, "billing/season_summary.html", {
        "org": org,
        "summary": donations.donation_summary(org),
        "winner": winner,
        "podium": board,
        "disbursement": disbursement,
        "is_owner": member.is_league_owner,
        "subscription": services.active_subscription(org),
        "esg_allowed": esg.esg_export_allowed(org),
    })


@login_required
def esg_report_view(request, org_id: int):
    """Download the season's ESG report as a PDF (Pro+ owners only, deck slide 7)."""
    org = get_object_or_404(Organisation, pk=org_id)
    if not _require_owner(request.user, org):
        return HttpResponseForbidden()

    from . import esg

    if not esg.esg_export_allowed(org):
        messages.info(request, "ESG report export is available on the Pro plan and above.")
        return redirect("billing:plans", org_id=org.id)

    pdf = esg.build_esg_pdf(org)
    filename = f"goodtip-esg-{org.name.lower().replace(' ', '-')}-{org.season.year}.pdf"
    response = HttpResponse(pdf, content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


@login_required
def receipt_view(request, org_id: int):
    """Platform service-fee receipt (GoodTip Pty Ltd). Phase 1: fee receipt only."""
    org = get_object_or_404(Organisation, pk=org_id)
    if not _require_owner(request.user, org):
        return HttpResponseForbidden()
    sub = services.active_subscription(org)
    if sub is None:
        messages.info(request, "No paid plan yet — nothing to receipt.")
        return redirect("billing:plans", org_id=org.id)
    return render(request, "billing/receipt.html", {"org": org, "sub": sub})


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
        meta = session.get("metadata") or {}
        if meta.get("kind") == "donation":
            _record_donation_from_session(session, meta)
        else:
            sub_id = meta.get("subscription_id") or session.get("client_reference_id")
            if sub_id:
                sub = PlanSubscription.objects.filter(pk=sub_id).first()
                if sub:
                    services.mark_paid(sub, payment_intent_id=session.get("payment_intent") or "")
    return HttpResponse(status=200)


def _record_donation_from_session(session, meta):
    from decimal import Decimal

    from accounts.models import User

    pledge = DonationPledge.objects.filter(pk=meta.get("pledge_id")).first()
    participant = User.objects.filter(pk=meta.get("participant_id")).first()
    if not pledge or not participant:
        return
    # Idempotency: ignore if this session was already recorded.
    if pledge.payments.filter(stripe_checkout_session_id=session.get("id", "")).exists():
        return
    donations.record_topup(
        pledge, participant=participant, amount=Decimal(meta.get("amount", "0")),
        checkout_session_id=session.get("id", ""),
        payment_intent_id=session.get("payment_intent") or "",
    )
