from __future__ import annotations

import logging
from decimal import Decimal

import stripe
from django.conf import settings
from django.utils import timezone

from .models import FoundingRate, PlanSubscription
from .pricing import (
    first_billing_at,
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


def is_test_mode() -> bool:
    """True when the configured key is a Stripe TEST key — the only time the
    demo controls exist. A live key never shows them."""
    return (settings.STRIPE_SECRET_KEY or "").startswith("sk_test_")


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


def create_checkout_session(sub: PlanSubscription, *, success_url: str, cancel_url: str,
                            email: str = "", demo: bool = False):
    """A Stripe Checkout session that SUBSCRIBES the organisation to its plan.

    3 OCT 2026 (client): "they can subscribe now, but the subscription starts
    on 31 January". So this is a yearly Stripe subscription, not a one-off
    payment: the card is saved today, nothing is charged today, and the first
    charge lands on FIRST_BILLING_DATE (billing_cycle_anchor, no proration),
    renewing yearly from then. Once that date has passed it is charged at once.

    ``demo`` (test keys only) runs the same subscription on a Stripe TEST
    CLOCK, so the demo can jump that one customer forward to 31 January and
    show the first charge really happening, instead of waiting for it. A
    test-clock customer is the only kind that can be moved through time; every
    other customer, and every live-mode one, keeps real time.
    """
    client = _client()
    cfg = tier_config(sub.tier)
    description = f"Platform service fee · {sub.season} season · billed yearly"
    if sub.is_founding_rate:
        rate = founding_rate_for(sub.org)
        until = rate.locked_until.year if rate else ""
        description += f" · Founding Member rate, locked through {until}"
    metadata = {"subscription_id": str(sub.id), "org_id": str(sub.org_id)}
    if sub.is_founding_rate:
        metadata["founding_rate"] = "1"
        metadata["list_price_aud"] = str(sub.list_price_aud or "")

    starts = first_billing_at()
    subscription_data = {"metadata": metadata}
    if starts is not None:
        subscription_data["billing_cycle_anchor"] = int(starts.timestamp())
        subscription_data["proration_behavior"] = "none"

    extra = {}
    clock_id = ""
    if demo:
        if not is_test_mode():
            raise BillingNotConfigured("The demo only runs on Stripe test keys.")
        metadata["demo"] = "1"
        clock = client.test_helpers.TestClock.create(
            frozen_time=int(timezone.now().timestamp()),
            name=f"GoodTip demo · {sub.org.name}"[:100],
        )
        clock_id = clock.id
        customer = client.Customer.create(
            email=email or None, name=sub.org.name, test_clock=clock.id,
            metadata={"org_id": str(sub.org_id), "demo": "1"},
        )
        extra["customer"] = customer.id
        sub.stripe_customer_id = customer.id
    elif email:
        extra["customer_email"] = email

    session = client.checkout.Session.create(
        mode="subscription",
        line_items=[{
            "quantity": 1,
            "price_data": {
                "currency": "aud",
                "unit_amount": int(sub.price_aud * 100),
                "recurring": {"interval": "year"},
                "product_data": {
                    "name": f"GoodTip {cfg['label']} plan — {sub.org.name}",
                    "description": description,
                },
            },
        }],
        subscription_data=subscription_data,
        success_url=success_url,
        cancel_url=cancel_url,
        client_reference_id=str(sub.id),
        metadata=metadata,
        # Charged and shown in Australian dollars, whoever is looking. Stripe's
        # adaptive pricing otherwise converts the page to the visitor's own
        # currency — it showed Kenyan shillings to the demo.
        adaptive_pricing={"enabled": False},
        **extra,
    )
    sub.stripe_checkout_session_id = session.id
    sub.starts_at = starts
    sub.stripe_test_clock_id = clock_id
    sub.save(update_fields=[
        "stripe_checkout_session_id", "starts_at", "stripe_test_clock_id", "stripe_customer_id",
    ])
    return session


def _invoice_subscription_id(inv: dict) -> str:
    """The subscription an invoice belongs to, across Stripe API versions —
    newer ones moved it under parent.subscription_details."""
    sid = inv.get("subscription")
    if isinstance(sid, dict):
        sid = sid.get("id")
    if not sid:
        sid = (((inv.get("parent") or {}).get("subscription_details") or {}).get("subscription"))
    return sid or ""


def record_subscribed(sub: PlanSubscription, *, subscription_id: str, customer_id: str = "") -> PlanSubscription:
    """Checkout finished: the organisation is subscribed. Paid comes later."""
    fields = []
    if subscription_id and sub.stripe_subscription_id != subscription_id:
        sub.stripe_subscription_id = subscription_id; fields.append("stripe_subscription_id")
    if customer_id and sub.stripe_customer_id != customer_id:
        sub.stripe_customer_id = customer_id; fields.append("stripe_customer_id")
    if sub.status == PlanSubscription.STATUS_PENDING:
        sub.status = PlanSubscription.STATUS_SCHEDULED; fields.append("status")
    if fields:
        sub.save(update_fields=fields)
    return sub


def record_invoice_paid(sub: PlanSubscription, inv: dict) -> PlanSubscription:
    """A real charge went through on the subscription: the plan is active."""
    if inv.get("hosted_invoice_url") and sub.stripe_invoice_url != inv["hosted_invoice_url"]:
        sub.stripe_invoice_url = inv["hosted_invoice_url"]
        sub.save(update_fields=["stripe_invoice_url"])
    pi = inv.get("payment_intent") or ""
    return mark_paid(sub, payment_intent_id=pi if isinstance(pi, str) else (pi or {}).get("id", ""))


def sync_from_stripe(sub: PlanSubscription) -> dict:
    """Read the subscription's real state back from Stripe and record it.

    The webhook does the same in production; this is what the success page
    and the demo poll call, so the screen is right even where no webhook can
    reach (a laptop running the site locally). Returns what the screen shows.
    """
    client = _client()
    if not sub.stripe_subscription_id and sub.stripe_checkout_session_id:
        session = client.checkout.Session.retrieve(sub.stripe_checkout_session_id).to_dict()
        if session.get("status") == "complete" and session.get("subscription"):
            record_subscribed(sub, subscription_id=session["subscription"],
                              customer_id=session.get("customer") or "")
    info = {"status": sub.status, "stripe_status": "", "paid": sub.is_active,
            "amount": str(sub.price_aud), "invoice_url": sub.stripe_invoice_url,
            "starts_at": sub.starts_at.isoformat() if sub.starts_at else "",
            "clock": ""}
    if not sub.stripe_subscription_id:
        return info
    s_obj = client.Subscription.retrieve(sub.stripe_subscription_id).to_dict()
    info["stripe_status"] = s_obj.get("status", "")
    invoices = client.Invoice.list(subscription=sub.stripe_subscription_id, limit=10).to_dict()["data"]
    paid = [i for i in invoices if i.get("status") == "paid" and (i.get("amount_paid") or 0) > 0]
    if paid:
        record_invoice_paid(sub, paid[0])
    if sub.stripe_test_clock_id:
        info["clock"] = client.test_helpers.TestClock.retrieve(sub.stripe_test_clock_id).to_dict().get("status", "")
    info.update({"status": sub.status, "paid": sub.is_active, "invoice_url": sub.stripe_invoice_url})
    return info


def demo_jump_to_first_billing(sub: PlanSubscription) -> None:
    """Demo only: move this subscription's test clock to two hours past the
    first billing date. Stripe then does exactly what it will do on the day —
    raises the year's invoice, finalises it and charges the saved card."""
    if not (sub.stripe_test_clock_id and is_test_mode()):
        raise BillingNotConfigured("Only a demo subscription on test keys can be moved through time.")
    client = _client()
    target = int(sub.starts_at.timestamp()) + 2 * 3600 if sub.starts_at else int(timezone.now().timestamp()) + 2 * 3600
    clock = client.test_helpers.TestClock.retrieve(sub.stripe_test_clock_id).to_dict()
    if clock.get("status") == "ready" and clock.get("frozen_time", 0) < target:
        client.test_helpers.TestClock.advance(sub.stripe_test_clock_id, frozen_time=target)


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
    """The org's PAID subscription for its current season, if any."""
    return org.subscriptions.filter(
        season=org.season, status=PlanSubscription.STATUS_ACTIVE
    ).first()


def current_subscription(org):
    """The plan the organisation is on: paid, or subscribed and waiting for its
    first charge (FIRST_BILLING_DATE). Subscribing is the commitment, so the
    plan's features follow it from that day, not from the day the card is
    charged — an organisation that signs up in October should not spend four
    months on a plan smaller than the one it chose."""
    return org.subscriptions.filter(
        season=org.season,
        status__in=[PlanSubscription.STATUS_ACTIVE, PlanSubscription.STATUS_SCHEDULED],
    ).order_by("-created_at").first()


def latest_subscription(org):
    """The most recent subscription attempt this season, whatever its state."""
    return org.subscriptions.filter(season=org.season).exclude(
        status=PlanSubscription.STATUS_EXPIRED
    ).order_by("-created_at").first()
