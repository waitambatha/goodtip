from __future__ import annotations

import logging
from decimal import Decimal

import stripe
from django.conf import settings
from django.utils import timezone

from .models import PlanSubscription
from .pricing import tier_config

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


def create_subscription(org, tier: str) -> PlanSubscription:
    """Create (or reuse) a pending subscription for an org's current season."""
    cfg = tier_config(tier)
    sub, _ = PlanSubscription.objects.get_or_create(
        org=org,
        season=org.season,
        status=PlanSubscription.STATUS_PENDING,
        defaults={
            "tier": tier,
            "price_aud": Decimal(cfg["price"]),
            "seat_limit": cfg["seat_limit"],
        },
    )
    # If they reselect a different tier before paying, update the pending row.
    if sub.tier != tier:
        sub.tier = tier
        sub.price_aud = Decimal(cfg["price"])
        sub.seat_limit = cfg["seat_limit"]
        sub.save(update_fields=["tier", "price_aud", "seat_limit"])
    return sub


def create_checkout_session(sub: PlanSubscription, *, success_url: str, cancel_url: str):
    """Create a Stripe Checkout session for the platform fee. Returns the session."""
    client = _client()
    cfg = tier_config(sub.tier)
    session = client.checkout.Session.create(
        mode="payment",
        line_items=[{
            "quantity": 1,
            "price_data": {
                "currency": "aud",
                "unit_amount": int(sub.price_aud * 100),
                "product_data": {
                    "name": f"GoodTip {cfg['label']} plan — {sub.org.name}",
                    "description": f"Platform service fee · {sub.season} season",
                },
            },
        }],
        success_url=success_url,
        cancel_url=cancel_url,
        client_reference_id=str(sub.id),
        metadata={"subscription_id": str(sub.id), "org_id": str(sub.org_id)},
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
