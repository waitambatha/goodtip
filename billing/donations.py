"""Donation, top-up, and matching logic (Cost Structure deck slides 2, 4, 5).

The charity total is the hero number across the app. Platform fees never mix in
here — this module only ever deals with money destined for the charity.
"""
from __future__ import annotations

from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from .models import CharityDisbursement, DonationPayment, DonationPledge
from .pricing import (
    ENTERPRISE,
    ENTERPRISE_PLUS,
    GROWTH,
    PRO,
    STARTER,
)

# Suggested minimum org pledge by tier (deck slide 4). Suggestions only — orgs
# can always pledge more.
SUGGESTED_MINIMUMS = {
    STARTER: Decimal("100"),
    GROWTH: Decimal("250"),
    PRO: Decimal("500"),
    ENTERPRISE: Decimal("1000"),
    ENTERPRISE_PLUS: Decimal("2500"),
}
DEFAULT_SUGGESTED_MINIMUM = Decimal("100")


def _q(value) -> Decimal:
    return Decimal(value or 0).quantize(Decimal("0.01"))


def suggested_minimum(org) -> Decimal:
    """A suggested pledge floor, based on the org's active plan tier."""
    from .services import active_subscription

    sub = active_subscription(org)
    if sub:
        return SUGGESTED_MINIMUMS.get(sub.tier, DEFAULT_SUGGESTED_MINIMUM)
    return DEFAULT_SUGGESTED_MINIMUM


def get_pledge(org) -> DonationPledge | None:
    return org.pledges.filter(season=org.season).first()


@transaction.atomic
def set_pledge(
    org,
    *,
    pledged_amount: Decimal,
    payment_schedule: str = DonationPledge.SCHEDULE_SEASON_CLOSE,
    matching_enabled: bool = False,
    matching_cap: Decimal = Decimal("0"),
) -> DonationPledge:
    """Create or update the org's pledge for its current season."""
    pledge, _ = DonationPledge.objects.get_or_create(
        org=org,
        season=org.season,
        defaults={"pledged_amount_aud": _q(pledged_amount)},
    )
    pledge.pledged_amount_aud = _q(pledged_amount)
    pledge.payment_schedule = payment_schedule
    pledge.matching_enabled = matching_enabled
    pledge.matching_cap_aud = _q(matching_cap) if matching_enabled else Decimal("0")
    pledge.charity = org.charity
    pledge.save()
    return pledge


@transaction.atomic
def record_topup(pledge: DonationPledge, *, participant, amount: Decimal, **stripe_ids) -> dict:
    """Record a participant top-up and apply dollar-for-dollar matching.

    Returns a dict describing what happened: the top-up, the matched amount (0 if
    matching is off or the cap is exhausted), and whether the cap was just hit.
    """
    amount = _q(amount)
    if amount <= 0:
        raise ValueError("Top-up must be a positive amount.")

    DonationPayment.objects.create(
        pledge=pledge, org=pledge.org, charity=pledge.charity, amount_aud=amount,
        type=DonationPayment.TYPE_TOP_UP,
        paid_by=DonationPayment.PAID_BY_PARTICIPANT,
        participant=participant,
        stripe_checkout_session_id=stripe_ids.get("checkout_session_id", ""),
        stripe_payment_intent_id=stripe_ids.get("payment_intent_id", ""),
    )

    matched = Decimal("0")
    cap_reached = False
    if pledge.matching_enabled:
        remaining = pledge.matching_remaining_aud
        matched = min(amount, remaining)
        if matched > 0:
            DonationPayment.objects.create(
                pledge=pledge, org=pledge.org, charity=pledge.charity, amount_aud=matched,
                type=DonationPayment.TYPE_MATCHED,
                paid_by=DonationPayment.PAID_BY_SYSTEM,
                participant=participant,
            )
            pledge.matching_used_aud = _q(pledge.matching_used_aud + matched)
            pledge.save(update_fields=["matching_used_aud", "updated_at"])
        cap_reached = pledge.matching_remaining_aud <= 0

    return {"topup": amount, "matched": matched, "cap_reached": cap_reached}


@transaction.atomic
def record_org_payment(pledge: DonationPledge, *, amount: Decimal, kind: str = DonationPayment.TYPE_BASE, **stripe_ids) -> DonationPayment:
    """Record an org base/instalment payment toward its pledge."""
    amount = _q(amount)
    if amount <= 0:
        raise ValueError("Payment must be a positive amount.")
    payment = DonationPayment.objects.create(
        pledge=pledge, org=pledge.org, charity=pledge.charity, amount_aud=amount,
        type=kind, paid_by=DonationPayment.PAID_BY_OWNER,
        stripe_checkout_session_id=stripe_ids.get("checkout_session_id", ""),
        stripe_payment_intent_id=stripe_ids.get("payment_intent_id", ""),
    )
    pledge.paid_amount_aud = _q(pledge.paid_amount_aud + amount)
    pledge.save(update_fields=["paid_amount_aud", "updated_at"])
    return payment


def _sum(qs, **flt) -> Decimal:
    total = qs.filter(**flt).aggregate(s=Sum("amount_aud"))["s"]
    return _q(total)


def donation_summary(org) -> dict | None:
    """The numbers behind the progress bar. None if no pledge exists yet.

    raised  = org base pledge (committed, shown from day one) + participant
              top-ups + org matched amounts.
    goal    = base pledge + matching cap (the full potential pool).
    """
    pledge = get_pledge(org)
    if pledge is None:
        return None

    payments = pledge.payments
    topups = _sum(payments, type=DonationPayment.TYPE_TOP_UP)
    matched = _sum(payments, type=DonationPayment.TYPE_MATCHED)

    base = _q(pledge.pledged_amount_aud)
    raised = _q(base + topups + matched)
    goal = _q(base + (pledge.matching_cap_aud if pledge.matching_enabled else Decimal("0")))
    pct = int(min((raised / goal * 100) if goal > 0 else 0, 100))

    return {
        "pledge": pledge,
        "charity": pledge.charity or org.charity,
        "base": base,
        "topups": topups,
        "matched": matched,
        "raised": raised,
        "goal": goal,
        "pct": pct,
        "matching_enabled": pledge.matching_enabled,
        "matching_cap": _q(pledge.matching_cap_aud),
        "matching_used": _q(pledge.matching_used_aud),
        "matching_remaining": pledge.matching_remaining_aud,
        "paid": _q(pledge.paid_amount_aud),
        "outstanding": max(_q(base - pledge.paid_amount_aud), Decimal("0")),
    }


def org_raised(org) -> Decimal:
    """An org's own standalone charity total — §7's 'local' figure.
    Zero when no pledge exists yet."""
    summary = donation_summary(org)
    return summary["raised"] if summary else Decimal("0.00")


def family_totals(org) -> dict | None:
    """The two §7 figures members always see, kept distinct and never blended:
    'local' (this org's own total) and 'national' (automatic roll-up across
    the top-level parent and ALL its children — a dollar total regardless of
    which charity each org picked, §5). None for a standalone org, where no
    separate national figure exists.
    """
    family = list(org.family())
    if len(family) <= 1:
        return None
    local = org_raised(org)
    national = sum((org_raised(o) for o in family), Decimal("0.00"))
    return {
        "local": _q(local),
        "national": _q(national),
        "root": org.root,
        "org_count": len(family),
    }


@transaction.atomic
def create_disbursement(org) -> CharityDisbursement:
    """Aggregate the season's donations into a single disbursement record."""
    summary = donation_summary(org)
    if summary is None:
        raise ValueError("This league has no donation pledge to disburse.")
    charity = summary["charity"]
    if charity is None:
        raise ValueError("No charity is set for this league yet.")

    disbursement, _ = CharityDisbursement.objects.update_or_create(
        org=org, season=org.season,
        defaults={
            "charity": charity,
            "total_base_aud": summary["base"],
            "total_matched_aud": summary["matched"],
            "total_topups_aud": summary["topups"],
            "total_disbursed_aud": summary["raised"],
            "disbursed_at": timezone.now(),
        },
    )
    return disbursement
