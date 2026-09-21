import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from catalog.models import GoodListConfig, OrganisationType, State
from orgs.models import OrgMember, Organisation
from orgs.services import is_creator_admin

from . import donations, goodlist, services
from .models import DonationPledge, PlanSubscription, SponsorshipApplication
from .pricing import (
    FOUNDING_WINDOW_CLOSES,
    GROUPS_MIN_TIER,
    STARTER,
    TIERS,
    founding_window_open,
    seat_limit_label,
    tier_label,
)

logger = logging.getLogger(__name__)


def good_list_view(request):
    """The public Good List (/leaderboard/) — live, privacy-gated data only.

    Never renders placeholder or example figures (categories doc reminder).
    The By Group board filters by organisation type, sub-category within that
    type, and state/territory via GET params.
    """
    type_slug = request.GET.get("type", "")
    cat_slug = request.GET.get("cat", "")
    state_code = request.GET.get("state", "")

    organisation_types = list(OrganisationType.objects.prefetch_related("sub_categories"))
    states = list(State.objects.all())
    # Drop unknown / mismatched filter values rather than 404ing a public page.
    if type_slug and not any(gt.slug == type_slug for gt in organisation_types):
        type_slug = ""
    if cat_slug and (not type_slug or not any(
        sc.slug == cat_slug
        for gt in organisation_types if gt.slug == type_slug
        for sc in gt.sub_categories.all()
    )):
        cat_slug = ""
    if state_code and not any(s.code == state_code for s in states):
        state_code = ""

    cfg = GoodListConfig.get()
    return render(request, "public/leaderboard.html", {
        "active": "leaderboard",
        "national_total": goodlist.national_total(),
        "board_live": goodlist.board_is_live(),
        "groups": goodlist.by_group(
            organisation_type_slug=type_slug or None,
            sub_category_slug=cat_slug or None,
            state_code=state_code or None,
        ),
        "by_charity": goodlist.by_charity(),
        "by_state": goodlist.by_state(),
        "by_country": goodlist.by_country(),
        # The Pacific grouping — see goodlist.pacific_nations for why it is
        # not privacy-gated the way a single country's row is.
        "pacific": goodlist.pacific_nations(),
        "by_sub_category": goodlist.by_sub_category(),
        "organisation_types": organisation_types,
        "states": states,
        "sel_type": type_slug,
        "sel_cat": cat_slug,
        "sel_state": state_code,
        "privacy_min": cfg.privacy_min_groups,
        "credibility_min": cfg.credibility_min_groups,
    })


def _require_owner(user, org):
    return OrgMember.objects.filter(
        user=user, org=org, is_league_owner=True
    ).exists()


def _tier_cards(org=None):
    """The five plans as the template wants them.

    Takes the org so each card can show what THIS organisation would actually
    be charged. A founding member looking at a price list showing everyone
    else's prices has been sold something they cannot see, which is the same
    problem the lock was built to fix.
    """
    cards = []
    for key, cfg in TIERS.items():
        card = {
            "key": key,
            "label": cfg["label"],
            "price": cfg["price"],
            "seat_label": seat_limit_label(cfg["seat_limit"]),
            "audience": cfg["audience"],
            "features": cfg["features"],
            "popular": cfg["popular"],
            "groups": cfg["groups"],
            "your_price": cfg["price"],
            "is_founding": False,
        }
        if org is not None:
            price, founding, _list = services.price_for(org, key)
            card["your_price"] = price
            card["is_founding"] = founding
        cards.append(card)
    return cards


@login_required
def plans_view(request, org_id: int):
    org = get_object_or_404(Organisation, pk=org_id)
    if not _require_owner(request.user, org):
        return HttpResponseForbidden()
    return render(request, "billing/plans.html", {
        "org": org,
        "tiers": _tier_cards(org),
        "current": services.active_subscription(org),
        "stripe_configured": services.is_configured(),
        # The organisation's own founding lock, and — for one that has not
        # taken a plan yet — whether the window is still open to them. Both,
        # because "you are locked until 2029" and "you have until 31 December
        # to be" are different sentences and the page has to pick the true one.
        "founding_rate": services.founding_rate_for(org),
        "founding_open": founding_window_open(),
        "founding_closes": FOUNDING_WINDOW_CLOSES,
        "groups_min_label": tier_label(GROUPS_MIN_TIER),
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


# pledge_view and topup_view were removed on 18 Aug 2026 with the wording
# change: GoodTip funds the donation from its own revenue, so an organisation
# has no pledge to set and a participant has nothing to top up. donations.py
# keeps the read-side helpers, which the dashboard and ESG report still use to
# report what has been given.


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
    if member is None or not is_creator_admin(request.user, org, membership=member):
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
    # THE ROOM NAVIGATOR, the one the leaderboard, the ladder and the charities
    # screen carry — "have the section that we have been using where we have all
    # the organisations and groups ... so I can be able to move from one
    # organisation and group summary without using the main menu."
    #
    # Only the organisations this person actually runs: a season summary is an
    # admin's screen (the guard above is is_creator_admin), so listing the ones
    # they merely play in would offer doors that answer 403.
    from orgs.services import is_creator_admin as _admin_of

    my_orgs = [
        m.org for m in
        OrgMember.objects.filter(user=request.user).select_related("org").order_by("org__name")
        if _admin_of(request.user, m.org, membership=m)
    ]
    return render(request, "billing/season_summary.html", {
        "org": org,
        "panel_orgs": my_orgs,
        "panel_org": org,
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


# ---------------------------------------------------------------------------
# Sponsored places — applying for one
# ---------------------------------------------------------------------------
#
# Saved first, emailed second, exactly as the public contact form does it and
# for the same reason: a mail outage must never lose the application. The
# difference from an enquiry is who is on the other end of it. Somebody filling
# this in has already read the price and concluded they cannot pay it, and they
# are telling us so — which takes a bit of doing, and a form that swallows that
# silently is a worse failure than losing a sales lead.

SPONSOR_LIMIT = 3            # applications per session…
SPONSOR_WINDOW = 24 * 3600   # …per day


def sponsorship_view(request):
    """Public page: apply for a sponsored place.

    Open to anyone, signed in or not. The audience for this is people who read
    /pricing/ and stopped — asking them to create an account and set up an
    organisation before they can ask whether they can afford one is the wrong
    order, and it is the order that loses them.
    """
    import time

    from django.core.exceptions import ValidationError
    from django.core.validators import validate_email

    from catalog.models import Country
    from goodtip.mail import send_template, site_url

    sent = False
    error = ""
    form = {}

    if request.method == "POST":
        form = {
            "contact_name": (request.POST.get("contact_name") or "").strip()[:120],
            "email": (request.POST.get("email") or "").strip()[:254],
            "organisation_name": (request.POST.get("organisation_name") or "").strip()[:160],
            "organisation_kind": (request.POST.get("organisation_kind") or "").strip()[:120],
            "people": (request.POST.get("people") or "").strip()[:6],
            "country": (request.POST.get("country") or "").strip()[:6],
            "reason": (request.POST.get("reason") or "").strip()[:4000],
        }
        now = time.time()
        recent = [
            t for t in request.session.get("sponsor_applications", [])
            if now - t < SPONSOR_WINDOW
        ]

        if request.POST.get("company"):
            # Honeypot. Answer as though it worked — telling a bot it was
            # caught only helps it try again.
            sent = True
        elif not (form["contact_name"] and form["email"]
                  and form["organisation_name"] and form["reason"]):
            error = "Your name, email, the group's name and the last question are all needed."
        elif len(recent) >= SPONSOR_LIMIT:
            error = "You've sent a few of these already — give us a day to read them."
        else:
            try:
                validate_email(form["email"])
            except ValidationError:
                error = "That email address doesn't look right — check it and try again."

        if not sent and not error:
            country = (
                Country.objects.filter(pk=form["country"], is_active=True).first()
                if form["country"].isdigit() else None
            )
            application = SponsorshipApplication.objects.create(
                contact_name=form["contact_name"],
                email=form["email"],
                organisation_name=form["organisation_name"],
                organisation_kind=form["organisation_kind"],
                country=country,
                people=int(form["people"]) if form["people"].isdigit() else None,
                reason=form["reason"],
                user=request.user if request.user.is_authenticated else None,
            )
            recent.append(now)
            request.session["sponsor_applications"] = recent
            sent = True

            # THE APPLICATION IS SAVED. NOTHING BELOW MAY UNDO THAT — same
            # reasoning as contact_submit_view, which has the long version.
            try:
                from accounts.models import User

                staff_emails = list(
                    User.objects.filter(is_staff=True, is_active=True)
                    .exclude(email="")
                    .values_list("email", flat=True)
                )
                if staff_emails:
                    send_template(
                        "sponsorship_applied",
                        subject=f"Sponsorship application — {application.organisation_name}",
                        to=staff_emails,
                        context={"application": application, "site_link": site_url("/")},
                        reply_to=[application.email],
                    )
                send_template(
                    "sponsorship_received",
                    subject="We've got your application — GoodTip",
                    to=[application.email],
                    context={"application": application, "site_link": site_url("/")},
                )
            except Exception:  # noqa: BLE001 — see the note above
                logger.exception(
                    "Sponsorship application %s saved, but its email(s) failed",
                    application.pk,
                )

    return render(request, "public/sponsorship.html", {
        "active": "pricing",
        "sent": sent,
        "error": error,
        "form": form,
        "countries": Country.objects.filter(is_active=True),
        "starter_price": TIERS[STARTER]["price"],
    })
