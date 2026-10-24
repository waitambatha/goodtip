import logging
from decimal import Decimal

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from catalog.models import GoodListConfig, OrganisationType, State
from orgs.models import OrgMember, Organisation
from orgs.services import is_creator_admin

from . import donations, goodlist, services
from .models import (
    CustomPackage, CustomPackageClaim, CustomPriceRequest, DonationPledge, PlanSubscription,
    SponsorshipApplication,
)
from .pricing import (
    FOUNDING_WINDOW_CLOSES,
    first_billing_at,
    CUSTOM,
    ENTERPRISE,
    ENTERPRISE_PLUS,
    GROUPS_MIN_TIER,
    GROWTH,
    PRO,
    STARTER,
    TIER_ACCENT,
    TIER_ORDER,
    TIERS,
    effective_tier_config,
    founding_stub_ends,
    founding_window_open,
    public_cards,
    seat_limit_label,
    tier_label,
)

from accounts.form_replies import json_error, json_ok, wants_json

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


def _period_progress(sub_info: dict) -> dict | None:
    """How far through the current billing period a subscription is — real
    dates from Stripe (client, 10 Oct 2026: "when the subscription is about
    to end, where it started"), not a guess at what a yearly plan's dates
    probably are. None until there is a real, started period to measure."""
    from datetime import datetime as _dt

    start = sub_info.get("current_period_start") if sub_info else ""
    end = sub_info.get("current_period_end") if sub_info else ""
    if not (start and end):
        return None
    start_dt, end_dt = _dt.fromisoformat(start), _dt.fromisoformat(end)
    now = timezone.now()
    span = (end_dt - start_dt).total_seconds()
    if span <= 0:
        return None
    elapsed = max(0, min(span, (now - start_dt).total_seconds()))
    pct = round(elapsed / span * 100)
    # The ring's SVG circumference (r=50 — see .bl-ring in goodtip.css),
    # computed here rather than in the template: Django's template language
    # has no multiplication, only the {% widthratio %} tag, and this needs
    # the same number twice (the dash-offset and the plain percentage).
    circumference = 314.159
    return {
        "pct": pct,
        "offset": round(circumference * (1 - pct / 100), 1),
        "start": start_dt,
        "end": end_dt,
        "days_left": max(0, (end_dt.date() - now.date()).days),
    }


# What each plan's showcase shows (client, 9 Oct 2026). Not app screenshots —
# "the images we have been uploading, forget about the screenshots" — but the
# photographs the public pages' hero splits are built from, so the billing
# screen looks like the rest of GoodTip rather than a product tour.
PLAN_MEDIA = {
    STARTER: [("img/scenes/afl-goal-posts.jpg", "One shared ladder"),
              ("img/scenes/afl-ball-grass.jpg", "All four competitions"),
              ("img/scenes/aussie-crowd-flag.jpg", "The charity your people pick")],
    GROWTH: [("img/scenes/nrl-players-fans.jpg", "Your name and logo on it"),
             ("img/scenes/afl-training.jpg", "Round by round, all season"),
             ("img/scenes/mcg-match.jpg", "Up to 50 tipping together")],
    PRO: [("img/scenes/mcg-stadium.jpg", "A group for each team"),
          ("img/scenes/nrl-posts-sunset.jpg", "Every group on its own ladder"),
          ("img/scenes/stadium-lights-grass.jpg", "Priority support")],
    ENTERPRISE: [("img/scenes/stadium-panorama.jpg", "Departments and sub-groups"),
                 ("img/scenes/afl-posts-mcg.jpg", "A dedicated account manager"),
                 ("img/scenes/nrl-ground-dusk.jpg", "Onboarding built around you")],
    ENTERPRISE_PLUS: [("img/stadium-night.jpg", "Multi-league setup"),
                      ("img/scenes/nrl-scoreboard.jpg", "Every result, as it lands"),
                      ("img/scenes/mcg-match.jpg", "Flexible payment terms")],
}


def _media_for(tier: str):
    return [{"img": path, "cap": cap} for path, cap in PLAN_MEDIA.get(tier, [])]


def _tier_cards(org=None):
    """The five plans as the template wants them.

    Takes the org so each card can show what THIS organisation would actually
    be charged. A founding member looking at a price list showing everyone
    else's prices has been sold something they cannot see, which is the same
    problem the lock was built to fix.
    """
    cards = []
    for key in TIER_ORDER:
        cfg = effective_tier_config(key)
        card = {
            "key": key,
            "label": cfg["label"],
            "price": cfg["price"],
            "seat_label": seat_limit_label(cfg["seat_limit"]),
            "seat_limit": cfg["seat_limit"],
            "media": _media_for(key),
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


def _subscription_status(org) -> dict:
    """The organisation's own subscription, read straight back from Stripe —
    not only for a test-mode daily charge (so a tester refreshing the page
    sees each day's charge land without a webhook, which a laptop or a
    firewalled server may not receive) but for anyone already subscribed:
    the "your subscription" panel needs the real billing dates and the real
    payment history, not a guess at what a yearly plan's dates probably are
    (client, 10 Oct 2026: "the level of... when the subscription is about to
    end, where it started... history of payments").

    Shared by plans_view (the small notice — see .plk-subnote) and
    subscription_view (the full page it links to — client, 21 Oct 2026: "the
    subscription will now have its own page"), so the two can never read
    Stripe back differently for the same organisation.
    """
    current = services.current_subscription(org)
    sub_info = None
    test_charges = None
    if current and services.is_configured():
        try:
            sub_info = services.sync_from_stripe(current)
            test_charges = sub_info.get("paid_count", 0)
            current.refresh_from_db()
        except Exception:  # noqa: BLE001 — the page must render whatever Stripe says
            logger.exception("Could not read the subscription back from Stripe")
    # Nothing current to show — but there may be something worth still
    # saying (client, 12 Oct 2026): a plan that ran out, or one cancelled
    # before it ever charged. Synced the same way as `current` above, so
    # "covered through" is Stripe's real date, not a guess.
    past = None
    past_info = None
    if not current:
        past = services.past_subscription(org)
        if past and past.stripe_subscription_id and services.is_configured():
            try:
                past_info = services.sync_from_stripe(past)
            except Exception:  # noqa: BLE001 — the page must render regardless
                logger.exception("Could not read the past subscription back from Stripe")
    return {
        "current": current, "sub_info": sub_info, "test_charges": test_charges,
        "past": past, "past_info": past_info,
    }


def _custom_request_state(org, *, on_custom_plan: bool) -> dict:
    """Where this organisation's negotiated custom price stands — shared by
    the Plans page's custom-pricing tile and the packages page's foot.

    The most recent APPROVED request is the offer; otherwise the most recent
    request of any status decides the quieter states, so an organisation
    always sees its *latest* word on this, not its first. org_declined_offer
    is the organisation's OWN "no" to a price Ian offered (client, 12 Oct
    2026) — distinct from custom_declined, which is Ian's "no" to their
    request; both end the same way on screen, but the reason belongs to a
    different person each time.
    """
    custom_offer = services.current_custom_offer(org)
    latest = CustomPriceRequest.objects.filter(org=org).order_by("-created_at").first()
    return {
        "custom_offer": None if on_custom_plan else custom_offer,
        "custom_pending": bool(latest and latest.is_pending),
        "custom_declined": latest if latest and latest.status == CustomPriceRequest.STATUS_DECLINED else None,
        "org_declined_offer": latest if latest and latest.is_declined_by_org else None,
    }


@login_required
def plans_view(request, org_id: int):
    org = get_object_or_404(Organisation, pk=org_id)
    if not _require_owner(request.user, org):
        return HttpResponseForbidden()
    status = _subscription_status(org)
    current, sub_info, past = status["current"], status["sub_info"], status["past"]

    tiers = _tier_cards(org)
    if current:
        here = TIER_ORDER.index(current.tier) if current.tier in TIER_ORDER else -1
        for card in tiers:
            there = TIER_ORDER.index(card["key"]) if card["key"] in TIER_ORDER else -1
            card["switch_direction"] = (
                "current" if card["key"] == current.tier
                else "upgrade" if there > here else "downgrade"
            )
    on_custom_plan = bool(current and current.tier == CUSTOM)
    # The standing custom packages (CustomPackage) — a different thing from
    # the negotiated offer: these are pre-made, browsable without asking
    # first, and "taking" one creates a CustomPackageClaim with its own
    # simulated review wait rather than a request Ian has to answer. Listed
    # on their own page (custom_packages_view); this screen only needs to
    # know whether there are any to link to.
    package_claim = services.current_package_claim(org)
    has_packages = not package_claim and CustomPackage.objects.filter(is_active=True).exists()

    return render(request, "billing/plans.html", {
        "org": org,
        "tiers": tiers,
        "current": current,
        "period": _period_progress(sub_info) if sub_info else None,
        "past_subscription": past,
        **_custom_request_state(org, on_custom_plan=on_custom_plan),
        "on_custom_plan": on_custom_plan,
        # Where the "how many people will tip?" slider starts.
        "member_count": OrgMember.objects.filter(org=org).count(),
        "stripe_configured": services.is_configured(),
        "stripe_test_mode": services.is_test_mode(),
        "first_billing_at": first_billing_at(),
        # The organisation's own founding lock, and — for one that has not
        # taken a plan yet — whether the window is still open to them. Both,
        # because "you are locked until 2029" and "you have until 31 December
        # to be" are different sentences and the page has to pick the true one.
        "founding_rate": services.founding_rate_for(org),
        "founding_open": founding_window_open(),
        "founding_closes": FOUNDING_WINDOW_CLOSES,
        # The end of the ragged first term. Named on this screen because it is
        # the number an organisation queries when the first invoice does not
        # cover twelve months (client, 22 Sep 2026).
        "founding_stub_ends": founding_stub_ends(),
        "groups_min_label": tier_label(GROUPS_MIN_TIER),
        "custom_packages": has_packages,
        "package_claim": package_claim,
    })


#: Each ready-made package's colour and mark, by position — fixed order, so a
#: package keeps its colour when another is hidden around it would not hold;
#: keyed on the row's own sort position instead (see _package_cards).
PACKAGE_LOOKS = [
    ("#2A7337", "ic-users"), ("#1F7A63", "ic-org"), ("#C9700A", "ic-spark"),
    ("#6B34B8", "ic-heart"), ("#2E6E7A", "ic-trophy"), ("#B23434", "ic-flame"),
]


def _package_cards(packages, people_today: int):
    """The packages as the page draws them: what each costs a person, what
    the published plans would charge for the same size, and how far it
    stretches next to the others (client, 24 Oct 2026: "when I click the
    starter, I need to know what starter is about")."""
    from . import custom_pricing

    packages = list(packages)
    biggest = max([p.seat_limit for p in packages] or [1])
    cards = []
    for i, pkg in enumerate(packages):
        accent, icon = PACKAGE_LOOKS[i % len(PACKAGE_LOOKS)]
        std = custom_pricing.standard_equivalent(pkg.seat_limit)
        per_person = pkg.price_aud / pkg.seat_limit if pkg.seat_limit else None
        cards.append({
            "pkg": pkg, "accent": accent, "icon": icon,
            "per_person": per_person,
            "std": std,
            "saving": (std["price"] - pkg.price_aud) if std and std["price"] > pkg.price_aud else None,
            "reach_pct": max(6, round(pkg.seat_limit / biggest * 100)),
            "you_pct": min(100, round(people_today / biggest * 100, 1)),
            "fits_today": people_today <= pkg.seat_limit,
            "sites": range(min(pkg.child_org_limit, 12)),
            "more_sites": max(0, pkg.child_org_limit - 12),
        })
    return cards


@login_required
def custom_packages_view(request, org_id: int):
    """Custom pricing, a page of its own (client, 21 Oct 2026; rebuilt 24 Oct
    2026: "when somebody clicks on the custom request, is able to know what
    is it about, things are well organized, I can be able to make my request,
    I can be able to see the other custom plans"). Four parts, top to bottom:
    how custom pricing works, where this organisation's own request stands
    (with the whole conversation and a way to write back), the ready-made
    packages, and the form to ask for a price of your own."""
    from . import custom_pricing

    org = get_object_or_404(Organisation, pk=org_id)
    if not _require_owner(request.user, org):
        return HttpResponseForbidden()
    current = services.current_subscription(org)
    on_custom_plan = bool(current and current.tier == CUSTOM)
    if on_custom_plan:
        # Already on a price made for them — nothing here to choose between.
        return redirect("billing:plans", org_id=org.id)

    people_today = OrgMember.objects.filter(org=org).count()
    requests = list(
        CustomPriceRequest.objects.filter(org=org)
        .select_related("requested_by", "decided_by").prefetch_related("thread__author")
        .order_by("-created_at")
    )
    active = requests[0] if requests else None
    return render(request, "billing/packages.html", {
        "org": org,
        "packages": _package_cards(CustomPackage.objects.filter(is_active=True), people_today),
        "package_claim": services.current_package_claim(org),
        "people_today": people_today,
        "active_request": active,
        "active_timeline": custom_pricing.timeline(active) if active else [],
        "steps": custom_pricing.steps_for(active),
        "earlier_requests": requests[1:],
        "org_kinds": custom_pricing.ORG_KINDS,
        "can_ask": not (active and active.is_pending),
        # The five, smallest first, for the form's "standard plan for this
        # size" line.
        "std_ladder": [
            {"seats": effective_tier_config(k)["seat_limit"], "label": effective_tier_config(k)["label"],
             "price": effective_tier_config(k)["price"]}
            for k in TIER_ORDER
        ],
        **_custom_request_state(org, on_custom_plan=False),
    })


def _member_growth(org, months: int = 6) -> list[dict]:
    """People in the organisation at the end of each of the last `months`
    months — cumulative, so the bars read as "how big we were", not "how
    many joined". The current month is "so far"."""
    from datetime import date

    today = timezone.localdate()
    joined = list(OrgMember.objects.filter(org=org).values_list("joined_at", flat=True))
    out = []
    for back in range(months - 1, -1, -1):
        y, m = today.year, today.month - back
        while m <= 0:
            m += 12
            y -= 1
        ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
        cut = date(ny, nm, 1)
        out.append({
            "label": date(y, m, 1).strftime("%b"),
            "value": sum(1 for j in joined if timezone.localtime(j).date() < cut),
            "now": back == 0,
        })
    return out


def _plan_ladder(team: int, highlight: str | None) -> list[dict]:
    """The five plans' yearly prices, each in its own colour, with the one
    this organisation is on (or would fit) marked."""
    rows = []
    for key in TIER_ORDER:
        cfg = effective_tier_config(key)
        rows.append({
            "key": key, "label": cfg["label"], "value": float(cfg["price"]),
            "seats": cfg["seat_limit"], "color": TIER_ACCENT.get(key),
            "per_person": round(float(cfg["price"]) / cfg["seat_limit"], 2) if cfg["seat_limit"] else None,
            "on": key == highlight, "fits": team <= cfg["seat_limit"],
        })
    return rows


@login_required
def subscription_view(request, org_id: int):
    """The whole subscription, on its own page (client, 21 Oct 2026; made
    "more detailed... seeing the history, seeing the charts... four
    visualizations... in 3D" on 24 Oct 2026). Status and the actions that
    act on it; four charts — seats used, where the season is, how the
    organisation has grown, and what each plan costs — then every plan this
    organisation has ever had, and its payments. An organisation with no
    plan sees the same four, measured against the plan that would fit it.
    Upgrade/Downgrade stay on Plans, since they act on the tab row there."""
    org = get_object_or_404(Organisation, pk=org_id)
    if not _require_owner(request.user, org):
        return HttpResponseForbidden()
    status = _subscription_status(org)
    current, sub_info, test_charges = status["current"], status["sub_info"], status["test_charges"]
    past, past_info = status["past"], status["past_info"]

    from .pricing import tier_for_team_size

    members = OrgMember.objects.filter(org=org).count()
    fit_key = tier_for_team_size(members)
    fit = effective_tier_config(fit_key)
    on_key = current.tier if current and current.tier in TIER_ORDER else None
    seat_limit = current.seat_limit if current else fit["seat_limit"]
    period = _period_progress(sub_info) if sub_info else None

    # The season clock: real billing dates when Stripe has them; otherwise
    # the first charge still ahead (scheduled), or — with no plan — the day
    # billing starts for anyone who signs up now.
    clock = None
    if period:
        clock = {"pct": period["pct"], "big": f"{period['days_left']}", "unit": "days left",
                 "line": f"Renews {period['end']:%-d %B %Y}"}
    elif current and current.starts_at:
        days = max(0, (timezone.localtime(current.starts_at).date() - timezone.localdate()).days)
        clock = {"pct": max(4, 100 - round(days / 365 * 100)), "big": f"{days}", "unit": "days to go",
                 "line": f"First charge {timezone.localtime(current.starts_at):%-d %B %Y}"}
    else:
        fb = first_billing_at()
        if fb:
            days = max(0, (timezone.localtime(fb).date() - timezone.localdate()).days)
            clock = {"pct": max(4, 100 - round(days / 365 * 100)), "big": f"{days}", "unit": "days",
                     "line": f"until billing starts, {fb:%-d %B %Y}"}

    plans_history = list(PlanSubscription.objects.filter(org=org).order_by("-created_at"))
    paid_total = sum((p.price_aud for p in plans_history if p.paid_at), Decimal("0"))
    history = (sub_info or {}).get("history") or []

    charts = {
        "growth": _member_growth(org),
        "ladder": _plan_ladder(members, on_key or fit_key),
    }
    for row in charts["ladder"]:
        row["tag"] = "yours" if on_key else "fits you"
        row["note"] = f"{row['seats']:,} people · ${row['per_person']} a person at full size"
    return render(request, "billing/subscription.html", {
        "org": org,
        "current": current,
        "current_accent": TIER_ACCENT.get(current.tier) if current else None,
        "period": period,
        "history": history,
        "test_charges": test_charges,
        "past_subscription": past,
        "past_accent": TIER_ACCENT.get(past.tier) if past else None,
        "past_period": _period_progress(past_info) if past_info else None,
        "members": members,
        "seat_limit": seat_limit,
        "seat_pct": min(100, round(members / seat_limit * 100)) if seat_limit else 0,
        "seats_left": max(0, seat_limit - members) if seat_limit else 0,
        "fit": {"key": fit_key, "label": fit["label"], "price": fit["price"], "seats": fit["seat_limit"],
                "accent": TIER_ACCENT.get(fit_key)},
        "clock": clock,
        "plans_history": plans_history,
        "paid_total": paid_total,
        "charts": charts,
        "growth_rows": charts["growth"],
        "ladder_rows": charts["ladder"],
        "growth_now": charts["growth"][-1]["value"] if charts["growth"] else members,
        "growth_from": charts["growth"][0]["value"] if charts["growth"] else members,
    })


@login_required
@require_POST
def take_custom_package_view(request, org_id: int, package_id: int):
    """"Take" a standing custom package (client, 19 Oct 2026) — creates the
    claim, plans_view then shows the simulated review state until
    CustomPackageClaim.is_ready. No real charge happens here: Stripe is not
    live (CLAUDE.md), so this ends in a handoff message, same as the
    negotiated custom-price flow does when there is nothing to check out to."""
    org = get_object_or_404(Organisation, pk=org_id)
    if not _require_owner(request.user, org):
        return HttpResponseForbidden()
    package = get_object_or_404(CustomPackage, pk=package_id, is_active=True)
    CustomPackageClaim.objects.create(package=package, org=org, requested_by=request.user)
    messages.success(request, f"Taking {package.name} — we're reviewing it now.")
    return redirect("billing:plans", org_id=org.id)


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

    # The demo (test keys only): the same subscription, run on a Stripe test
    # clock so the success page can jump it to the first billing date.
    demo = request.POST.get("demo") == "1" and services.is_test_mode()
    # Test mode's switch on the plans page (client, 6 Oct 2026): a daily
    # subscription charged from about a day from now. Test keys only — on live
    # keys the field is ignored, never honoured.
    test_daily = request.POST.get("test_daily") == "1" and services.is_test_mode()
    sub = services.create_subscription(org, tier)
    try:
        services.create_setup_intent(
            sub, email=request.user.email or "", demo=demo, test_daily=test_daily,
        )
    except Exception:  # noqa: BLE001 — surface any Stripe error gracefully
        logger.exception("Stripe setup intent creation failed")
        messages.error(request, "Couldn't start checkout. Please try again.")
        return redirect("billing:plans", org_id=org.id)
    # Paying happens on our own page now, not at checkout.stripe.com. The
    # POST only opens the intent; the card form lives behind a GET so it
    # survives a reload.
    return redirect("billing:pay", org_id=org.id)


@login_required
@require_POST
def request_custom_price_view(request, org_id: int):
    """"None of these quite fit" (client, 11 Oct 2026) — an organisation asks
    for a price outside the five published plans. Saved first, emailed
    second, same ordering as the sponsorship application and for the same
    reason: a mail outage must never lose the request.

    The form asks for the size of the thing as numbers too (client, 24 Oct
    2026: "things are well organized") — people, sites, a budget, what kind
    of organisation — so the team's queue can weigh it at a glance. They are
    written into the message as well, so the email reads whole on its own."""
    from . import custom_pricing

    org = get_object_or_404(Organisation, pk=org_id)
    if not _require_owner(request.user, org):
        return HttpResponseForbidden()
    back = redirect(reverse("billing:packages", args=[org.id]) + "#track")

    if CustomPriceRequest.objects.filter(org=org, status=CustomPriceRequest.STATUS_PENDING).exists():
        messages.error(request, "There's already a custom price request with the team — add to it below instead.")
        return back

    def _int(name):
        try:
            v = int((request.POST.get(name) or "").strip())
            return v if v > 0 else None
        except ValueError:
            return None

    people, sites = _int("people_count"), _int("sites_count")
    budget = _int("budget_aud")
    kind = (request.POST.get("org_kind") or "").strip()
    if kind not in dict(custom_pricing.ORG_KINDS):
        kind = ""
    note = (request.POST.get("message") or "").strip()[:1600]
    if not (note or people):
        messages.error(request, "Tell us at least how many people, or a line about what you need.")
        return redirect(reverse("billing:packages", args=[org.id]) + "#ask")

    facts = []
    if kind:
        facts.append(custom_pricing.org_kind_label(kind))
    if people:
        facts.append(f"{people:,} people")
    if sites:
        facts.append(f"{sites} site{'s' if sites != 1 else ''}")
    if budget:
        facts.append(f"budget about ${budget:,}/yr")
    message = (" · ".join(facts) + ("\n\n" if facts and note else "") + note)[:2000]

    cpr = CustomPriceRequest.objects.create(
        org=org, requested_by=request.user, message=message,
        people_count=people, sites_count=sites, budget_aud=budget, org_kind=kind,
    )
    custom_pricing.tell_team(cpr, template="custom_price_requested", subject=f"Custom price request — {org.name}")
    messages.success(request, "Sent. You'll get a notification here and an email as soon as the team replies.")
    return back


@login_required
@require_POST
def custom_price_reply_view(request, org_id: int, request_id: int):
    """Writing back to the team about a request, before or after it is
    decided (client, 24 Oct 2026: "me being able to reach back")."""
    from . import custom_pricing

    org = get_object_or_404(Organisation, pk=org_id)
    if not _require_owner(request.user, org):
        return HttpResponseForbidden()
    req = get_object_or_404(CustomPriceRequest, pk=request_id, org=org)
    body = (request.POST.get("body") or "").strip()
    if body:
        custom_pricing.post_message(req, author=request.user, body=body, from_team=False)
        messages.success(request, "Sent to the team. Their reply will land here and in your notifications.")
    return redirect(reverse("billing:packages", args=[org.id]) + "#track")


@login_required
@require_POST
def decline_custom_offer_view(request, org_id: int):
    """The other half of the negotiation (client, 12 Oct 2026: "you can
    either accept it or reject it... give a reason why"). Accepting an
    offer is already the existing "Subscribe" button on it; this is
    saying no, with a reason the team actually sees — not silence."""
    org = get_object_or_404(Organisation, pk=org_id)
    if not _require_owner(request.user, org):
        return HttpResponseForbidden()

    offer = services.current_custom_offer(org)
    if offer is None:
        messages.error(request, "There's no custom price offer here to decline.")
        return redirect("billing:plans", org_id=org.id)

    reason = (request.POST.get("reason") or "").strip()[:2000]
    services.decline_custom_offer(offer, reason=reason)

    from . import custom_pricing

    custom_pricing.tell_team(offer, template="custom_price_org_declined",
                             subject=f"{org.name} declined the custom price — {offer.label}")
    messages.success(request, "Sent — the team will know you'd like a different price.")
    return redirect(reverse("billing:packages", args=[org.id]) + "#track")


@login_required
@require_POST
def custom_checkout_view(request, org_id: int):
    """Pay for the custom price just approved for this organisation — same
    card-on-GoodTip's-own-page flow as the five published plans
    (billing.services.create_setup_intent doesn't care which tier it's
    saving a card for)."""
    org = get_object_or_404(Organisation, pk=org_id)
    if not _require_owner(request.user, org):
        return HttpResponseForbidden()

    offer = services.current_custom_offer(org)
    if offer is None:
        messages.error(request, "There's no custom price to subscribe to right now.")
        return redirect("billing:plans", org_id=org.id)
    from . import custom_pricing

    custom_pricing.mark_accepted(offer)

    if not services.is_configured():
        messages.error(
            request,
            "Payments aren't switched on yet — Stripe keys haven't been added. "
            "Your plan choice is saved; check back once billing is configured.",
        )
        services.create_custom_subscription(org, offer)
        return redirect("billing:plans", org_id=org.id)

    sub = services.create_custom_subscription(org, offer)
    try:
        services.create_setup_intent(sub, email=request.user.email or "")
    except Exception:  # noqa: BLE001 — surface any Stripe error gracefully
        logger.exception("Stripe setup intent creation failed (custom price)")
        messages.error(request, "Couldn't start checkout. Please try again.")
        return redirect("billing:plans", org_id=org.id)
    return redirect("billing:pay", org_id=org.id)


@login_required
def pay_view(request, org_id: int):
    """The card form, on GoodTip (client, 9 and 10 Oct 2026).

    Stripe Elements mounts into this page — the organisation never leaves the
    site to pay — and billing/checkout.html drives the loading, decline and
    success states itself around it. The card fields are still Stripe's,
    inside their iframe: this server never sees a card number.
    """
    org = get_object_or_404(Organisation, pk=org_id)
    if not _require_owner(request.user, org):
        return HttpResponseForbidden()
    sub = services.latest_subscription(org)
    intent = services.open_setup_intent(sub) if sub else None
    if intent is None:
        # Nothing to pay for — either it is already done or the intent has
        # lapsed. Either way the plans page is the honest place to land.
        return redirect("billing:plans", org_id=org.id)
    return render(request, "billing/checkout.html", {
        "org": org,
        "sub": sub,
        "client_secret": intent.client_secret,
        "publishable_key": settings.STRIPE_PUBLISHABLE_KEY,
        "confirm_url": reverse("billing:pay_confirm", args=[org.id]),
        "success_url": reverse("billing:success", args=[org.id]),
    })


@login_required
@require_POST
def pay_confirm_view(request, org_id: int):
    """Called from the pay page once Stripe's own confirmSetup() has resolved
    with a succeeded SetupIntent. Turns that saved card into the real
    subscription and reports back so the page can play its success animation
    knowing the subscription genuinely exists, not just that the browser
    thinks the card was accepted."""
    org = get_object_or_404(Organisation, pk=org_id)
    if not _require_owner(request.user, org):
        return HttpResponseForbidden()
    sub = services.latest_subscription(org)
    setup_intent_id = request.POST.get("setup_intent_id", "")
    if sub is None or not setup_intent_id:
        return JsonResponse({"ok": False, "error": "Nothing to confirm."}, status=400)
    try:
        services.finalize_subscription(sub, setup_intent_id)
    except services.BillingNotConfigured as e:
        return JsonResponse({"ok": False, "error": str(e)}, status=409)
    except Exception:  # noqa: BLE001
        logger.exception("Could not finalise subscription %s", sub.pk)
        return JsonResponse({"ok": False, "error": "Something went wrong finishing your subscription."}, status=502)
    return JsonResponse({"ok": True, "redirect": reverse("billing:success", args=[org.id])})


@login_required
@require_POST
def switch_tier_view(request, org_id: int):
    """Upgrade or downgrade an existing subscription (client, 10 Oct 2026:
    "the option of either upgrading or downgrading"). The card on file is
    reused — this never touches the pay page."""
    org = get_object_or_404(Organisation, pk=org_id)
    if not _require_owner(request.user, org):
        return HttpResponseForbidden()
    current = services.current_subscription(org)
    tier = request.POST.get("tier")
    if current is None or not current.stripe_subscription_id:
        messages.error(request, "There's no subscription here to change yet.")
        return redirect("billing:plans", org_id=org.id)
    if tier not in TIERS:
        messages.error(request, "Please choose a real plan.")
        return redirect("billing:plans", org_id=org.id)
    try:
        updated = services.switch_tier(current, tier)
    except Exception:  # noqa: BLE001 — surface it, don't 500 on a Stripe hiccup
        logger.exception("Plan switch failed for subscription %s", current.pk)
        messages.error(request, "Couldn't change plans just then. Please try again.")
        return redirect("billing:plans", org_id=org.id)
    messages.success(request, f"{org.name} is now on the {updated.get_tier_display()} plan.")
    return redirect("billing:plans", org_id=org.id)


@login_required
@require_POST
def cancel_subscription_view(request, org_id: int):
    """Opt out (client, 10 Oct 2026). The confirmation is in the template
    (data-confirm), not here — this runs the moment the request arrives."""
    org = get_object_or_404(Organisation, pk=org_id)
    if not _require_owner(request.user, org):
        return HttpResponseForbidden()
    current = services.current_subscription(org)
    if current is None or not current.stripe_subscription_id:
        messages.error(request, "There's no subscription here to cancel.")
        return redirect("billing:plans", org_id=org.id)
    try:
        updated = services.cancel_subscription(current)
    except Exception:  # noqa: BLE001
        logger.exception("Cancel failed for subscription %s", current.pk)
        messages.error(request, "Couldn't cancel just then. Please try again.")
        return redirect("billing:plans", org_id=org.id)
    if updated.is_active:
        messages.success(
            request,
            f"{org.name}'s {updated.get_tier_display()} plan won't renew — "
            f"you keep it through the season you've already paid for.",
        )
    else:
        messages.success(request, f"{org.name}'s subscription has been cancelled.")
    return redirect("billing:plans", org_id=org.id)


@login_required
@require_POST
def resume_subscription_view(request, org_id: int):
    """Undo a cancel-at-period-end before it takes effect."""
    org = get_object_or_404(Organisation, pk=org_id)
    if not _require_owner(request.user, org):
        return HttpResponseForbidden()
    current = services.current_subscription(org)
    if current is None:
        messages.error(request, "There's no subscription here to resume.")
        return redirect("billing:plans", org_id=org.id)
    try:
        services.resume_subscription(current)
    except Exception:  # noqa: BLE001
        logger.exception("Resume failed for subscription %s", current.pk)
        messages.error(request, "Couldn't undo the cancellation just then. Please try again.")
        return redirect("billing:plans", org_id=org.id)
    messages.success(request, f"{org.name}'s plan will keep renewing after all.")
    return redirect("billing:plans", org_id=org.id)


@login_required
@require_POST
def pay_now_view(request, org_id: int):
    """Charge the current subscription's price right now, ahead of schedule
    (client, 12 Oct 2026: "the capacity of paying the next subscription...
    even before the time that it's due to be auto-deducted")."""
    org = get_object_or_404(Organisation, pk=org_id)
    if not _require_owner(request.user, org):
        return HttpResponseForbidden()
    current = services.current_subscription(org)
    if current is None:
        messages.error(request, "There's no subscription here to pay.")
        return redirect("billing:plans", org_id=org.id)
    try:
        services.pay_now(current)
    except services.BillingNotConfigured as exc:
        messages.error(request, str(exc))
        return redirect("billing:plans", org_id=org.id)
    except Exception:  # noqa: BLE001
        logger.exception("Pay-now failed for subscription %s", current.pk)
        messages.error(request, "Couldn't take the payment just then. Please try again.")
        return redirect("billing:plans", org_id=org.id)
    messages.success(request, f"${current.price_aud:,.0f} taken — {org.name}'s plan renews a year from today.")
    return redirect("billing:plans", org_id=org.id)


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
    # Back from Stripe Checkout. The webhook is the source of truth in
    # production; this also reads the real state straight back from Stripe so
    # the page is right even where no webhook reaches (a laptop, locally).
    sub = services.latest_subscription(org)
    info = {}
    if sub is not None and services.is_configured():
        try:
            info = services.sync_from_stripe(sub)
        except Exception:  # noqa: BLE001 — show what we have; the webhook will catch up
            logger.exception("Could not read subscription %s back from Stripe", sub.pk)
    return render(request, "billing/success.html", {
        "org": org, "sub": sub, "info": info,
        "is_demo": bool(sub and sub.is_demo and services.is_test_mode()),
        "accent": TIER_ACCENT.get(sub.tier) if sub else None,
        "period": _period_progress(info) if info else None,
        "history": info.get("history") or [],
    })


@login_required
def subscription_status_view(request, org_id: int):
    """JSON for the success page's live status (and the demo's countdown)."""
    org = get_object_or_404(Organisation, pk=org_id)
    if not _require_owner(request.user, org):
        return HttpResponseForbidden()
    sub = services.latest_subscription(org)
    if sub is None or not services.is_configured():
        return JsonResponse({"status": "none"})
    try:
        return JsonResponse(services.sync_from_stripe(sub))
    except Exception:  # noqa: BLE001
        logger.exception("Subscription status read failed for %s", sub.pk)
        return JsonResponse({"status": sub.status, "error": "stripe"}, status=502)


@login_required
@require_POST
def demo_start_view(request, org_id: int):
    """Demo only (test keys): jump the demo subscription to its first billing
    date so the first yearly charge happens now, for real, in test mode."""
    org = get_object_or_404(Organisation, pk=org_id)
    if not _require_owner(request.user, org) or not services.is_test_mode():
        return HttpResponseForbidden()
    sub = services.latest_subscription(org)
    if sub is None or not sub.is_demo:
        return JsonResponse({"ok": False, "error": "No demo subscription to start."}, status=400)
    try:
        services.demo_jump_to_first_billing(sub)
    except Exception as e:  # noqa: BLE001
        logger.exception("Demo clock advance failed for %s", sub.pk)
        return JsonResponse({"ok": False, "error": str(e)[:200]}, status=502)
    return JsonResponse({"ok": True})


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
        # stripe==15.2.0's StripeObject stopped being a dict subclass (only
        # __getitem__/__contains__ remain) — .get() below is dict-only, so
        # this has to be converted once, here, rather than at every call site.
        # Found 28 Sep 2026: every real webhook delivery 500s without this,
        # for both a plan payment and a donation top-up, silently, because
        # Stripe keys had never been live in any environment before today.
        session = event["data"]["object"].to_dict()
        meta = session.get("metadata") or {}
        if meta.get("kind") == "donation":
            _record_donation_from_session(session, meta)
        else:
            sub_id = meta.get("subscription_id") or session.get("client_reference_id")
            if sub_id:
                sub = PlanSubscription.objects.filter(pk=sub_id).first()
                if sub and session.get("mode") == "subscription":
                    # Subscribed; the charge itself arrives as invoice.paid.
                    services.record_subscribed(
                        sub, subscription_id=session.get("subscription") or "",
                        customer_id=session.get("customer") or "",
                    )
                elif sub:
                    services.mark_paid(sub, payment_intent_id=session.get("payment_intent") or "")
    elif event["type"] == "invoice.paid":
        inv = event["data"]["object"].to_dict()
        if (inv.get("amount_paid") or 0) > 0:
            sid = services._invoice_subscription_id(inv)
            sub = PlanSubscription.objects.filter(stripe_subscription_id=sid).first() if sid else None
            if sub:
                services.record_invoice_paid(sub, inv)
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

    # gt-forms.js asked; give it the answer rather than the page.
    if request.method == "POST" and wants_json(request):
        return json_ok("sponsorship") if sent else json_error(
            error or "That did not go through — try again."
        )

    return render(request, "public/sponsorship.html", {
        "active": "pricing",
        "sent": sent,
        "error": error,
        "form": form,
        "countries": Country.objects.filter(is_active=True),
        "starter_price": effective_tier_config(STARTER)["price"],
    })


def pricing_view(request):
    """The public /pricing/ page.

    Used to be a bare TemplateView with its price cards computed once into
    extra_context at urls.py import time — fine while TIERS was a fixed
    dict, but now that Ian can edit a tier's price/seat/org limit from the
    admin (billing.models.PlanOverride) that would freeze whatever was true
    when gunicorn last started. Converted to a real view for the same
    reason accounts.views.home_view was: it has to read effective_tier_
    config fresh on every request, not once per process.
    """
    from goodtip.urls import PRICING_FACTS

    cards = public_cards()
    by_tier = {c["tier"]: c for c in cards}
    # The hand-built hero table on public/pricing.html keys its rows by the
    # product NAME (client-facing, see billing.pricing's own note on why the
    # dict key and the label disagree), not by pricing.TIERS's key — this is
    # the one place that table reads a real number rather than a hand-typed
    # one: its price, and the struck-through price above it when Ian has
    # set a discount (billing.models.PlanOverride).
    plan_numbers = {
        "starter": by_tier[STARTER],
        "team": by_tier[GROWTH],
        "workplace": by_tier[PRO],
        "organisation": by_tier[ENTERPRISE],
        "enterprise": by_tier[ENTERPRISE_PLUS],
    }

    return render(request, "public/pricing.html", {
        "active": "pricing",
        "price_cards": cards,
        "plan_numbers": plan_numbers,
        "custom_packages": CustomPackage.objects.filter(is_active=True),
        **PRICING_FACTS,
    })
