from __future__ import annotations

import logging
from decimal import Decimal

import stripe
from django.conf import settings
from django.utils import timezone

from .models import FoundingRate, PlanSubscription
from .pricing import (
    founding_locked_until,
    founding_window_open,
    tier_config,
)

logger = logging.getLogger(__name__)


class BillingNotConfigured(Exception):
    """Raised when a Stripe action is attempted but no keys are configured."""


def is_configured() -> bool:
    """True once Stripe secret keys have been added to the environment."""
    return bool(settings.STRIPE_SECRET_KEY)


def _client():
    if not is_configured():
        raise BillingNotConfigured(
            "Stripe is not configured — add STRIPE_SECRET_KEY to .env."
        )
    stripe.api_key = settings.STRIPE_SECRET_KEY
    return stripe


# ---------------------------------------------------------------------------
# Founding Member rate
# ---------------------------------------------------------------------------


def founding_rate_for(org) -> FoundingRate | None:
    """This organisation's live founding lock, or None."""
    rate = FoundingRate.objects.filter(org=org).first()
    return rate if rate is not None and rate.is_live() else None


def grant_founding_rate(org, tier: str, *, note: str = "") -> FoundingRate | None:
    """Lock this organisation's rate, if the founding window is still open.

    CALLED WHEN A PLAN IS FIRST CHOSEN, not when it is first paid. The promise
    on the site is made to anyone who "signs up before 31 December", and
    choosing a plan is the act of signing up — an organisation that picks
    Workplace on 30 December and whose payment settles on 2 January has not
    missed the window by any reading a customer would accept. The subscription
    row that comes out of the same call is still pending until Stripe says
    otherwise, so nothing here grants access to anything.

    IDEMPOTENT, AND IT NEVER RE-PRICES AN EXISTING LOCK. Coming back to change
    plan before paying must not move the locked amount up, and must not restart
    the clock; the first lock an organisation is granted is the one it keeps.
    A rate granted by hand (``note`` set) is likewise never overwritten here.
    """
    if not founding_window_open():
        return None
    existing = FoundingRate.objects.filter(org=org).first()
    if existing is not None:
        return existing
    cfg = tier_config(tier)
    return FoundingRate.objects.create(
        org=org,
        tier=tier,
        price_aud=Decimal(cfg["price"]),
        locked_until=founding_locked_until(),
        note=note,
    )


def price_for(org, tier: str) -> tuple[Decimal, bool, Decimal]:
    """What to charge this org for this plan: (price, is_founding, list_price).

    TWO RULES, AND BOTH OF THEM NARROW THE LOCK.

    It only applies to the plan it was granted for. A $99 Starter lock does not
    follow an organisation up to the $799 Workplace plan — the promise was that
    their price would not rise under them, not that every larger product would
    be sold to them at the small one's rate, and reading it the other way hands
    a founding member a $1,299 plan for $99. Moving up, or down, is buying a
    different thing, and a different thing costs what it costs.

    And it is a CEILING, never a floor. If the list price ever falls below
    somebody's locked amount they pay the list price, because a loyalty rate
    that costs more than walking in off the street is not a loyalty rate.
    """
    list_price = Decimal(tier_config(tier)["price"])
    rate = founding_rate_for(org)
    if rate is None or rate.tier != tier or rate.price_aud >= list_price:
        return list_price, False, list_price
    return rate.price_aud, True, list_price


def create_subscription(org, tier: str) -> PlanSubscription:
    """Create (or reuse) a pending subscription for an org's current season.

    Also the moment the founding rate is granted — see grant_founding_rate for
    why choosing the plan, rather than paying for it, is the right trigger.
    """
    cfg = tier_config(tier)
    grant_founding_rate(org, tier)
    price, founding, list_price = price_for(org, tier)
    sub, created = PlanSubscription.objects.get_or_create(
        org=org,
        season=org.season,
        status=PlanSubscription.STATUS_PENDING,
        defaults={
            "tier": tier,
            "price_aud": price,
            "seat_limit": cfg["seat_limit"],
            "is_founding_rate": founding,
            "list_price_aud": list_price,
        },
    )
    # If they reselect a different tier before paying, update the pending row —
    # including its price, which a founding lock may have moved.
    if not created and (
        sub.tier != tier
        or sub.price_aud != price
        or sub.is_founding_rate != founding
    ):
        sub.tier = tier
        sub.price_aud = price
        sub.seat_limit = cfg["seat_limit"]
        sub.is_founding_rate = founding
        sub.list_price_aud = list_price
        sub.save(update_fields=[
            "tier", "price_aud", "seat_limit", "is_founding_rate", "list_price_aud",
        ])
    return sub


def create_checkout_session(sub: PlanSubscription, *, success_url: str, cancel_url: str):
    """Create a Stripe Checkout session for the platform fee. Returns the session."""
    client = _client()
    cfg = tier_config(sub.tier)
    # The founding rate is named on the charge itself, not just in our database.
    # Somebody approving this line on a corporate card months later should be
    # able to see why it is not the price on the website, and a Stripe receipt
    # is the document that reaches them.
    description = f"Platform service fee · {sub.season} season"
    if sub.is_founding_rate:
        rate = founding_rate_for(sub.org)
        until = rate.locked_until.year if rate else ""
        description += f" · Founding Member rate, locked through {until}"
    metadata = {"subscription_id": str(sub.id), "org_id": str(sub.org_id)}
    if sub.is_founding_rate:
        metadata["founding_rate"] = "1"
        metadata["list_price_aud"] = str(sub.list_price_aud or "")
    session = client.checkout.Session.create(
        mode="payment",
        line_items=[{
            "quantity": 1,
            "price_data": {
                "currency": "aud",
                "unit_amount": int(sub.price_aud * 100),
                "product_data": {
                    "name": f"GoodTip {cfg['label']} plan — {sub.org.name}",
                    "description": description,
                },
            },
        }],
        success_url=success_url,
        cancel_url=cancel_url,
        client_reference_id=str(sub.id),
        metadata=metadata,
    )
    sub.stripe_checkout_session_id = session.id
    sub.save(update_fields=["stripe_checkout_session_id"])
    return session


def create_donation_checkout_session(pledge, participant, amount, *, success_url: str, cancel_url: str):
    """Create a Stripe Checkout session for a participant top-up.

    The top-up is only recorded once the webhook confirms payment, so the amount
    and identifiers travel in the session metadata.
    """
    client = _client()
    charity_name = pledge.charity.name if pledge.charity_id else "the charity"
    session = client.checkout.Session.create(
        mode="payment",
        line_items=[{
            "quantity": 1,
            "price_data": {
                "currency": "aud",
                "unit_amount": int(amount * 100),
                "product_data": {
                    "name": f"Donation to {charity_name}",
                    "description": f"Your top-up via {pledge.org.name}",
                },
            },
        }],
        success_url=success_url,
        cancel_url=cancel_url,
        metadata={
            "kind": "donation",
            "pledge_id": str(pledge.id),
            "participant_id": str(participant.id),
            "amount": str(amount),
        },
    )
    return session


def mark_paid(sub: PlanSubscription, *, payment_intent_id: str = "") -> PlanSubscription:
    """Activate a subscription once payment is confirmed (idempotent)."""
    if sub.status == PlanSubscription.STATUS_ACTIVE:
        return sub
    sub.status = PlanSubscription.STATUS_ACTIVE
    sub.paid_at = timezone.now()
    if payment_intent_id:
        sub.stripe_payment_intent_id = payment_intent_id
    sub.save(update_fields=["status", "paid_at", "stripe_payment_intent_id"])
    logger.info("Subscription %s marked active for org %s", sub.id, sub.org_id)
    return sub


def construct_webhook_event(payload: bytes, sig_header: str):
    """Verify and parse a Stripe webhook event. Raises on bad signature."""
    if not settings.STRIPE_WEBHOOK_SECRET:
        raise BillingNotConfigured("STRIPE_WEBHOOK_SECRET is not set.")
    return stripe.Webhook.construct_event(
        payload, sig_header, settings.STRIPE_WEBHOOK_SECRET
    )


def active_subscription(org):
    """The org's active subscription for its current season, if any."""
    return org.subscriptions.filter(
        season=org.season, status=PlanSubscription.STATUS_ACTIVE
    ).first()
