"""Ian's own edits to the five published plans, and the standing custom
packages the pricing page offers alongside them.

CLIENT, 19 OCT 2026: "he can now be able to manually change it himself via
the admin... we don't have to keep on going back and forth to the script."
And, on the discount: "either a flat amount or a percentage, and it
auto-calculates and saves the other."

Two kinds of row, one screen, because they answer the same question for
Ian — "what does the pricing page say right now" — even though they are
different models. billing.pricing.TIERS stays the seed and the fallback for
the five published plans (billing.models.PlanOverride is the editable layer
on top, see its own docstring and billing.pricing.effective_tier_config);
billing.models.CustomPackage rows have no code-side default at all, they are
created here from nothing.
"""
from decimal import Decimal, InvalidOperation

from django.contrib import admin, messages
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from billing.models import CustomPackage, PlanOverride
from billing.pricing import TIER_ORDER, TIERS, effective_tier_config, tier_label

from . import access


def _ctx(request, title, **extra):
    ctx = admin.site.each_context(request)
    ctx.update(title=title, **extra)
    return ctx


def _guard(request):
    if not access.can(request.user, "billing.plan_admin"):
        raise Http404("No plan admin here.")


#: The ready-made packages' colours, in order — the same set the
#: organisation's own Custom pricing page draws them in (billing.views.
#: PACKAGE_LOOKS), so a package looks the same on both sides.
def _package_looks():
    from billing.views import PACKAGE_LOOKS

    return PACKAGE_LOOKS


def _plan_rows():
    """The five plans with everything the hub and a plan's own page show."""
    import math

    from billing.models import PlanSubscription
    from billing.pricing import TIER_ACCENT

    overrides = {o.tier: o for o in PlanOverride.objects.all()}
    on_plan = {}
    for row in PlanSubscription.objects.filter(
        status__in=[PlanSubscription.STATUS_ACTIVE, PlanSubscription.STATUS_SCHEDULED],
    ).values_list("tier", flat=True):
        on_plan[row] = on_plan.get(row, 0) + 1
    biggest = max(effective_tier_config(t)["seat_limit"] for t in TIER_ORDER)
    tiers = []
    for i, tier in enumerate(TIER_ORDER, 1):
        eff = effective_tier_config(tier)
        tiers.append({
            "tier": tier, "step": i,
            "label": TIERS[tier]["label"],
            "audience": TIERS[tier].get("audience", ""),
            "features": TIERS[tier].get("features", []),
            "popular": TIERS[tier].get("popular", False),
            "groups": TIERS[tier].get("groups", False),
            "accent": TIER_ACCENT.get(tier),
            "base_price": Decimal(TIERS[tier]["price"]),
            "base_seat_limit": TIERS[tier]["seat_limit"],
            "base_child_org_limit": TIERS[tier]["child_org_limit"],
            "override": overrides.get(tier),
            "effective": eff,
            "changed": bool(overrides.get(tier)) and (
                eff["price"] != Decimal(TIERS[tier]["price"])
                or eff["seat_limit"] != TIERS[tier]["seat_limit"]
                or eff["child_org_limit"] != TIERS[tier]["child_org_limit"]
            ),
            "orgs_on": on_plan.get(tier, 0),
            # A log scale, so Starter's 20 is still a visible bar next to 2,499.
            "cap_pct": max(6, round(math.log(eff["seat_limit"] + 1) / math.log(biggest + 1) * 100)),
            "per_person": (eff["price"] / eff["seat_limit"]) if eff["seat_limit"] else None,
        })
    top_pp = max([t["per_person"] for t in tiers if t["per_person"]] or [1])
    top_price = max([t["effective"]["price"] for t in tiers] or [1])
    for t in tiers:
        t["pp_pct"] = round(t["per_person"] / top_pp * 100) if t["per_person"] else 0
        t["price_pct"] = max(4, round(t["effective"]["price"] / top_price * 100))
    return tiers


def _package_rows():
    from billing.models import CustomPackageClaim

    claims = {}
    for pk in CustomPackageClaim.objects.values_list("package_id", flat=True):
        claims[pk] = claims.get(pk, 0) + 1
    looks = _package_looks()
    out = []
    for i, pkg in enumerate(CustomPackage.objects.all()):
        accent, icon = looks[i % len(looks)]
        out.append({"pkg": pkg, "n": i + 1, "accent": accent, "icon": icon, "taken": claims.get(pkg.pk, 0),
                    "per_person": pkg.price_aud / pkg.seat_limit if pkg.seat_limit else None})
    return out


def plan_admin_hub(request):
    """The dashboard (rebuilt 24 Oct 2026: "see everything in whatever i
    click"). The five plans and the packages as tiles; each opens its own
    page (plan_detail, package_detail) with everything about it and its
    editor. Only the comparison across all five lives here, since it is the
    one thing no single plan's page can show."""
    _guard(request)
    tiers = _plan_rows()
    packages = _package_rows()
    return render(request, "admin/hub/plan_admin.html", _ctx(
        request, "Plans",
        tiers=tiers, packages=packages,
        orgs_paying=sum(t["orgs_on"] for t in tiers),
        overridden=sum(1 for t in tiers if t["override"]),
        active_packages=sum(1 for p in packages if p["pkg"].is_active),
        hub_title="Plans",
        hub_lead="The five published plans and the ready-made custom packages. Open one to see everything "
                 "about it and change it — /pricing/ follows straight away.",
        hub_icon="ic-sliders",
    ))


def plan_detail(request, tier: str):
    """One published plan, the whole page: who it is for, what it includes,
    published against live, where it sits among the five, the organisations
    on it now, and its editor."""
    from billing.models import PlanSubscription

    _guard(request)
    if tier not in TIERS:
        raise Http404("Not a real plan.")
    tiers = _plan_rows()
    i = TIER_ORDER.index(tier)
    row = tiers[i]
    subs = (PlanSubscription.objects.filter(tier=tier).select_related("org")
            .order_by("-created_at")[:30])
    return render(request, "admin/hub/plan_detail.html", _ctx(
        request, row["label"],
        row=row, tiers=tiers,
        live_subs=[s for s in subs if s.status in (PlanSubscription.STATUS_ACTIVE, PlanSubscription.STATUS_SCHEDULED)],
        past_subs=[s for s in subs if s.status not in (PlanSubscription.STATUS_ACTIVE, PlanSubscription.STATUS_SCHEDULED)][:10],
        prev=tiers[i - 1] if i > 0 else None,
        next=tiers[i + 1] if i + 1 < len(tiers) else None,
        back_url=reverse("admin:hq_plan_admin"), back_label="plans",
    ))


def package_detail(request, pk: int):
    """One ready-made package, the whole page: what it is, how it compares
    with the five plans, every organisation that has taken it, its editor."""
    from billing import custom_pricing
    from billing.models import CustomPackageClaim

    _guard(request)
    rows = _package_rows()
    found = [r for r in rows if r["pkg"].pk == pk]
    if not found:
        raise Http404("No such package.")
    row = found[0]
    i = rows.index(row)
    std = custom_pricing.standard_equivalent(row["pkg"].seat_limit)
    return render(request, "admin/hub/package_detail.html", _ctx(
        request, row["pkg"].name,
        row=row, pkg=row["pkg"], std=std,
        saving=(std["price"] - row["pkg"].price_aud) if std and std["price"] > row["pkg"].price_aud else None,
        claims=CustomPackageClaim.objects.filter(package=row["pkg"]).select_related("org", "requested_by"),
        prev=rows[i - 1] if i > 0 else None,
        next=rows[i + 1] if i + 1 < len(rows) else None,
        back_url=reverse("admin:hq_plan_admin") + "#packages", back_label="plans",
    ))


def package_new(request):
    _guard(request)
    return render(request, "admin/hub/package_detail.html", _ctx(
        request, "New package",
        row=None, pkg=None, is_new=True,
        back_url=reverse("admin:hq_plan_admin") + "#packages", back_label="plans",
    ))


def _decimal(raw: str):
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return Decimal(raw)
    except InvalidOperation:
        return None


def _int(raw: str):
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


@require_POST
def plan_override_save(request, tier: str):
    _guard(request)
    if tier not in TIERS:
        raise Http404("Not a real plan.")
    here = redirect("admin:hq_plan_detail", tier=tier)

    price_aud = _decimal(request.POST.get("price_aud"))
    seat_limit = _int(request.POST.get("seat_limit"))
    child_org_limit = _int(request.POST.get("child_org_limit"))

    # Whichever of the two the admin actually typed into — see the model's
    # own docstring on why both are stored but only one is authoritative per
    # save. static/js/gt-plan-admin.js sets this hidden field as the person
    # types; a form submitted with JS disabled still works, it just keeps
    # whatever discount was already saved unless this field says otherwise.
    discount_mode = request.POST.get("discount_mode") or ""
    discount_percent_raw = _decimal(request.POST.get("discount_percent"))
    discount_amount_raw = _decimal(request.POST.get("discount_amount_aud"))

    base_price = price_aud if price_aud is not None else Decimal(TIERS[tier]["price"])
    discount_percent = None
    discount_amount = None
    if discount_mode == "percent" and discount_percent_raw is not None:
        discount_percent = discount_percent_raw
        discount_amount = (base_price * discount_percent_raw / Decimal("100")).quantize(Decimal("0.01"))
    elif discount_mode == "amount" and discount_amount_raw is not None:
        discount_amount = discount_amount_raw
        discount_percent = (
            (discount_amount_raw / base_price * Decimal("100")).quantize(Decimal("0.01"))
            if base_price else None
        )
    # discount_mode == "clear", or nothing entered: no discount on this save.

    override, _created = PlanOverride.objects.update_or_create(
        tier=tier,
        defaults={
            "price_aud": price_aud,
            "seat_limit": seat_limit,
            "child_org_limit": child_org_limit,
            "discount_percent": discount_percent,
            "discount_amount_aud": discount_amount,
        },
    )
    messages.success(request, f"{tier_label(tier)} updated — live on /pricing/ now.")
    return here


@require_POST
def custom_package_save(request, pk: int = 0):
    _guard(request)
    here = redirect("admin:hq_custom_package_new") if not pk else redirect("admin:hq_custom_package_detail", pk=pk)

    name = (request.POST.get("name") or "").strip()[:80]
    description = (request.POST.get("description") or "").strip()[:240]
    price_aud = _decimal(request.POST.get("price_aud"))
    seat_limit = _int(request.POST.get("seat_limit"))
    child_org_limit = _int(request.POST.get("child_org_limit")) or 1
    sort_order = _int(request.POST.get("sort_order")) or 0
    is_active = bool(request.POST.get("is_active"))

    if not name or price_aud is None or price_aud <= 0 or not seat_limit or seat_limit <= 0:
        messages.error(request, "A package needs a name, a real price and a real number of seats.")
        return here

    defaults = {
        "name": name, "description": description, "price_aud": price_aud,
        "seat_limit": seat_limit, "child_org_limit": child_org_limit,
        "sort_order": sort_order, "is_active": is_active,
    }
    if pk:
        package = get_object_or_404(CustomPackage, pk=pk)
        for field, value in defaults.items():
            setattr(package, field, value)
        package.save(update_fields=list(defaults))
        messages.success(request, f"{package.name} updated.")
    else:
        package = CustomPackage.objects.create(**defaults)
        messages.success(request, f"{name} created — organisations can see it {'now' if is_active else 'once it is on offer'}.")
        return redirect("admin:hq_custom_package_detail", pk=package.pk)
    return here
