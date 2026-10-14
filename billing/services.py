from __future__ import annotations

import logging
from datetime import datetime, timedelta
from decimal import Decimal

import stripe
from django.conf import settings
from django.utils import timezone

from .models import CustomPriceRequest, FoundingRate, PlanSubscription
from .pricing import (
    CUSTOM,
    TIERS,
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


def current_custom_offer(org) -> CustomPriceRequest | None:
    """The custom price this organisation may currently subscribe to, if any
    — the most recent APPROVED request it has not itself turned down. An
    older, superseded approval, a decline (Ian's or, now, the
    organisation's own — client, 12 Oct 2026: "you can either accept it or
    reject it") is still in the table as history; only a live, un-rejected
    approval is offered."""
    return (
        CustomPriceRequest.objects.filter(
            org=org, status=CustomPriceRequest.STATUS_APPROVED, org_declined_at__isnull=True,
        )
        .order_by("-decided_at").first()
    )


def decline_custom_offer(offer: CustomPriceRequest, *, reason: str = "") -> CustomPriceRequest:
    """The organisation says no to a price Ian offered — not the same thing
    as Ian declining their request; the row stays APPROVED, since that is
    what happened, and simply stops being something billing.views.
    plans_view will hand back as current_custom_offer."""
    offer.org_declined_at = timezone.now()
    offer.org_decline_reason = reason[:2000]
    offer.save(update_fields=["org_declined_at", "org_decline_reason"])
    return offer


def create_custom_subscription(org, offer: CustomPriceRequest) -> PlanSubscription:
    """Create (or reuse) a pending subscription for a custom price Ian — or
    whoever else holds billing.custom_pricing — approved for this
    organisation.

    Mirrors create_subscription, but the price, seat limit and plan name come
    from the approval itself rather than pricing.TIERS: a custom price is
    data set per organisation, not a sixth row in the fixed dict every other
    org's plans page reads, so tier_config(CUSTOM) was never going to work —
    see pricing.CUSTOM.
    """
    sub, created = PlanSubscription.objects.get_or_create(
        org=org, season=org.season, status=PlanSubscription.STATUS_PENDING,
        defaults={
            "tier": CUSTOM,
            "price_aud": offer.price_aud,
            "seat_limit": offer.seat_limit,
            "is_founding_rate": False,
            "list_price_aud": offer.price_aud,
            "custom_label": offer.label,
        },
    )
    if not created and (
        sub.tier != CUSTOM or sub.price_aud != offer.price_aud
        or sub.seat_limit != offer.seat_limit or sub.custom_label != offer.label
    ):
        sub.tier = CUSTOM
        sub.price_aud = offer.price_aud
        sub.seat_limit = offer.seat_limit
        sub.is_founding_rate = False
        sub.list_price_aud = offer.price_aud
        sub.custom_label = offer.label
        sub.save(update_fields=[
            "tier", "price_aud", "seat_limit", "is_founding_rate", "list_price_aud", "custom_label",
        ])
    return sub


def create_setup_intent(sub: PlanSubscription, *, email: str = "", demo: bool = False,
                        test_daily: bool = False):
    """Start saving this organisation's card, on our own page.

    ELEMENTS, not Checkout (client, 10 Oct 2026: "a loader when I press
    subscribe ... a nice animation that says the card has declined ... a nice
    successful message"). Checkout's embedded iframe draws its own button and
    handles every outcome — loading, decline, success — inside itself, with
    no event the parent page can see; there is no way to put GoodTip's own
    animations on top of it. A SetupIntent confirmed with
    ``stripe.confirmSetup()`` hands the result straight back as a promise, so
    billing/checkout.html owns the whole sequence instead.

    This only SAVES the card — nothing is charged yet. Once confirmSetup
    succeeds, finalize_subscription below creates the real subscription with
    the saved payment method, at the usual billing_cycle_anchor. Reusing a
    customer across attempts (rather than one per POST) means a declined card
    and a retry are the same customer, not two.

    ``demo`` (test keys only) still creates a Stripe TEST CLOCK customer, kept
    for the existing demo-start flow (billing/success.html); nothing in the
    current UI reaches it, but the clock lets a test-mode customer be moved
    through time once it does.

    ``test_daily`` (test keys only, client 6 Oct 2026): the subscription
    finalize_subscription creates is DAILY, first charge about a day from
    now, so the people testing sign-ups see payment work end to end within a
    day rather than waiting for 31 January or a year.
    """
    if test_daily and not is_test_mode():
        raise BillingNotConfigured("Daily test subscriptions only run on Stripe test keys.")
    if demo and not is_test_mode():
        raise BillingNotConfigured("The demo only runs on Stripe test keys.")
    client = _client()
    customer_id = sub.stripe_customer_id
    clock_id = sub.stripe_test_clock_id
    # The saved customer id can go stale — test-mode data cleared, the
    # organisation switched Stripe accounts, anything — and reusing a
    # customer Stripe no longer has is exactly what "Couldn't start
    # checkout" looked like from one of these (found 11 Oct 2026, traced to
    # a customer deleted by hand while clearing other test data). Check
    # before trusting it, same as a fresh attempt would.
    if customer_id:
        try:
            client.Customer.retrieve(customer_id)
        except stripe.InvalidRequestError as exc:
            if getattr(exc, "code", "") != "resource_missing":
                raise
            logger.warning("Stripe customer %s for subscription %s is gone; making a new one",
                           customer_id, sub.pk)
            customer_id = None
    if not customer_id:
        extra = {}
        if demo:
            clock = client.test_helpers.TestClock.create(
                frozen_time=int(timezone.now().timestamp()),
                name=f"GoodTip demo · {sub.org.name}"[:100],
            )
            clock_id = clock.id
            extra["test_clock"] = clock.id
        customer = client.Customer.create(
            email=email or None, name=sub.org.name,
            metadata={"org_id": str(sub.org_id), "subscription_id": str(sub.id),
                     "demo": "1" if demo else ""},
            **extra,
        )
        customer_id = customer.id

    intent = client.SetupIntent.create(
        customer=customer_id,
        usage="off_session",
        payment_method_types=["card"],
        metadata={
            "subscription_id": str(sub.id), "org_id": str(sub.org_id),
            "demo": "1" if demo else "", "test_daily": "1" if test_daily else "",
        },
    )
    sub.stripe_customer_id = customer_id
    sub.stripe_setup_intent_id = intent.id
    sub.stripe_test_clock_id = clock_id
    sub.is_test_daily = test_daily
    sub.save(update_fields=[
        "stripe_customer_id", "stripe_setup_intent_id", "stripe_test_clock_id", "is_test_daily",
    ])
    return intent


def open_setup_intent(sub: PlanSubscription):
    """The organisation's setup intent, if it is still waiting for a card.

    The pay page is a GET, so it can be reloaded; rather than opening a new
    intent on every refresh it reads back the one the POST created. Returns
    None once a card has been accepted (or the subscription is already made),
    which is the pay page's cue to send them on instead of re-showing a form.
    """
    if sub.stripe_subscription_id:
        return None
    if not sub.stripe_setup_intent_id:
        return None
    try:
        intent = _client().SetupIntent.retrieve(sub.stripe_setup_intent_id)
    except Exception:  # noqa: BLE001 — a stale id is not an error worth a 500
        logger.exception("Could not read setup intent for subscription %s", sub.pk)
        return None
    # A Stripe object is not a dict: `.get` is intercepted by its attribute
    # lookup and raises, so read the field as an attribute.
    status = getattr(intent, "status", "")
    return intent if status in ("requires_payment_method", "requires_confirmation", "requires_action") else None


def _plan_description(sub: PlanSubscription, *, tier: str | None = None) -> str:
    """The line an invoice shows, for whichever tier is being priced — the
    subscription's own tier by default, or the one it is switching to."""
    tier = tier or sub.tier
    cadence = "billed daily" if sub.is_test_daily else "billed yearly"
    description = f"Platform service fee · {sub.season} season · {cadence}"
    if sub.is_test_daily:
        description = f"TEST MODE · {description}"
    if sub.is_founding_rate and tier == sub.tier:
        rate = founding_rate_for(sub.org)
        until = rate.locked_until.year if rate else ""
        description += f" · Founding Member rate, locked through {until}"
    return description


def _create_price(client, sub: PlanSubscription, *, tier: str, price_aud: Decimal):
    """A fresh Price (and the one-off Product behind it) for ``tier`` at
    ``price_aud``, on the subscription's own interval and currency.

    Subscription.create/modify's own items[].price_data has no room for an
    inline product (unlike Checkout Sessions) — only an existing product id.
    Price.create does accept product_data inline, so that is made first and
    referenced by id. A fresh one every time (first subscribe, every switch)
    rather than a shared one per tier, so each Price can carry the org's own
    name and this organisation's own price, founding-locked or not.

    ``tier`` is CUSTOM for a negotiated plan — there is no TIERS entry to read
    a label from, so the name Ian gave it on the subscription itself
    (sub.custom_label) is used instead.
    """
    label = sub.custom_label if tier == CUSTOM else tier_config(tier)["label"]
    return client.Price.create(
        currency="aud",
        unit_amount=int(price_aud * 100),
        recurring={"interval": "day" if sub.is_test_daily else "year"},
        product_data={"name": f"GoodTip {label} plan — {sub.org.name}"},
    )


def finalize_subscription(sub: PlanSubscription, setup_intent_id: str) -> PlanSubscription:
    """The card was accepted on the pay page; this is what turns it into the
    real subscription. Re-reads the intent from Stripe rather than trusting
    the browser's word for it — the browser can claim anything in a POST,
    Stripe's own record of its own intent cannot be forged from here.

    Idempotent: a double-click, or the success animation's own page load
    racing a slow network, must not create two subscriptions for one card.
    """
    if sub.stripe_subscription_id:
        return sub  # already finalised — nothing to redo
    client = _client()
    intent = client.SetupIntent.retrieve(setup_intent_id)
    if intent.status != "succeeded":
        raise BillingNotConfigured(f"That card has not been accepted yet (status={intent.status}).")
    if intent.customer != sub.stripe_customer_id or intent.id != sub.stripe_setup_intent_id:
        raise BillingNotConfigured("That card does not belong to this subscription.")
    payment_method = intent.payment_method

    client.Customer.modify(sub.stripe_customer_id,
                           invoice_settings={"default_payment_method": payment_method})

    metadata = {"subscription_id": str(sub.id), "org_id": str(sub.org_id)}
    if sub.is_founding_rate:
        metadata["founding_rate"] = "1"
        metadata["list_price_aud"] = str(sub.list_price_aud or "")
    if sub.is_test_daily:
        metadata["test_daily"] = "1"
        starts = timezone.now() + timedelta(days=1) - timedelta(minutes=5)
    else:
        if sub.stripe_test_clock_id:
            metadata["demo"] = "1"
        starts = first_billing_at()

    price = _create_price(client, sub, tier=sub.tier, price_aud=sub.price_aud)
    params = {
        "customer": sub.stripe_customer_id,
        "items": [{"price": price.id, "quantity": 1}],
        "default_payment_method": payment_method,
        "description": _plan_description(sub),
        "metadata": metadata,
    }
    if starts is not None:
        params["billing_cycle_anchor"] = int(starts.timestamp())
        params["proration_behavior"] = "none"
    subscription = client.Subscription.create(**params)

    sub.starts_at = starts
    sub.stripe_subscription_id = subscription.id
    if sub.status == PlanSubscription.STATUS_PENDING:
        sub.status = PlanSubscription.STATUS_SCHEDULED
    sub.save(update_fields=["starts_at", "stripe_subscription_id", "status"])
    return sub


def switch_tier(sub: PlanSubscription, new_tier: str) -> PlanSubscription:
    """Upgrade or downgrade an existing subscription to ``new_tier`` (client,
    10 Oct 2026: "the option of either ... downgrading or upgrading").

    The card already on file is reused — nobody re-enters it to change plans.
    Before the first charge (still SCHEDULED) the swap is free: nothing has
    been billed yet, so there is nothing to prorate. Once it is ACTIVE,
    Stripe's own proration adds a line to the NEXT invoice rather than
    charging the card the moment the button is pressed — switching plans
    should not be a surprise charge.

    A founding rate is a lock on the TIER originally chosen (billing.models.
    FoundingRate); price_for() already returns the list price once the tier
    no longer matches it, so moving plans costs the new plan's own price —
    never more than list, exactly as the founding copy on /pricing/ promises.
    """
    if not sub.stripe_subscription_id:
        raise BillingNotConfigured("This subscription has not been paid for yet.")
    if new_tier not in TIERS:
        raise BillingNotConfigured("That is not a real plan.")
    if new_tier == sub.tier:
        return sub
    client = _client()
    cfg = tier_config(new_tier)
    price_aud, founding, list_price = price_for(sub.org, new_tier)

    stripe_sub = client.Subscription.retrieve(sub.stripe_subscription_id)
    item_id = stripe_sub["items"]["data"][0]["id"]
    price = _create_price(client, sub, tier=new_tier, price_aud=price_aud)
    client.Subscription.modify(
        sub.stripe_subscription_id,
        items=[{"id": item_id, "price": price.id}],
        proration_behavior="none" if not sub.is_active else "create_prorations",
        description=_plan_description(sub, tier=new_tier),
        metadata={"subscription_id": str(sub.id), "org_id": str(sub.org_id)},
    )

    sub.tier = new_tier
    sub.price_aud = price_aud
    sub.seat_limit = cfg["seat_limit"]
    sub.is_founding_rate = founding
    sub.list_price_aud = list_price
    sub.save(update_fields=["tier", "price_aud", "seat_limit", "is_founding_rate", "list_price_aud"])
    return sub


def cancel_subscription(sub: PlanSubscription) -> PlanSubscription:
    """Opt out (client, 10 Oct 2026).

    Nothing charged yet (still SCHEDULED): cancelled outright, right now —
    there is nothing to let run out. Already ACTIVE, already paid for this
    season: cancelled AT THE END of what was already paid for, so the
    organisation keeps the season it bought and simply is not charged again.
    Either way the card is never touched here; Stripe does that at renewal,
    or not, depending on which of these happened.
    """
    if not sub.stripe_subscription_id:
        raise BillingNotConfigured("There is no subscription to cancel.")
    client = _client()
    if sub.is_active:
        client.Subscription.modify(sub.stripe_subscription_id, cancel_at_period_end=True)
        sub.cancel_at_period_end = True
        sub.canceled_at = timezone.now()
        sub.save(update_fields=["cancel_at_period_end", "canceled_at"])
    else:
        client.Subscription.cancel(sub.stripe_subscription_id)
        sub.status = PlanSubscription.STATUS_EXPIRED
        sub.canceled_at = timezone.now()
        sub.save(update_fields=["status", "canceled_at"])
    return sub


def resume_subscription(sub: PlanSubscription) -> PlanSubscription:
    """Undo an opt-out that hasn't taken effect yet — only possible while the
    subscription is still ACTIVE and merely not set to renew; once it has
    actually ended (cancelled outright, or the period really did run out)
    there is nothing to resume and subscribing again is the honest path."""
    if not sub.stripe_subscription_id or not sub.cancel_at_period_end:
        raise BillingNotConfigured("There is nothing to resume.")
    client = _client()
    client.Subscription.modify(sub.stripe_subscription_id, cancel_at_period_end=False)
    sub.cancel_at_period_end = False
    sub.canceled_at = None
    sub.save(update_fields=["cancel_at_period_end", "canceled_at"])
    return sub


def pay_now(sub: PlanSubscription) -> PlanSubscription:
    """Charge this subscription's full price right now, ahead of its normal
    date, and move the renewal clock to today (client, 12 Oct 2026: "I can
    be able to pay it even before the time that it's due to be auto-
    deducted"). Works whether nothing has been charged yet (still SCHEDULED)
    or the plan is already ACTIVE and this is an early renewal.

    TWO STRIPE CALLS, IN THIS ORDER, AND NEITHER IS THE FIRST THING TRIED.
    ``billing_cycle_anchor='now'`` on its own charges NOTHING —
    ``proration_behavior='none'`` means exactly that, no invoice at all —
    and would hand the organisation a free extra year.
    ``proration_behavior='always_invoice'`` looked like the fix but it
    PRORATES: it bills only the gap between the old and new anchor, not the
    plan's real price (verified live, 12 Oct 2026: a $99 plan ten days from
    its first charge invoiced for $96.29, not $99). So the full price is
    charged by hand first, as its own one-off invoice, and only once THAT is
    paid is the anchor reset, with ``proration_behavior='none'`` so Stripe
    does not also bill the now-zero gap a second time.

    THE INVOICE IS LINKED TO THE SUBSCRIPTION (``subscription=`` on both the
    item and the invoice) so an early renewal shows up in the organisation's
    own payment history (sync_from_stripe reads invoices by subscription
    id) rather than existing only as an orphaned customer-level charge.
    ``Invoice.create`` refuses ``subscription`` and
    ``pending_invoice_items_behavior`` together — found live, 12 Oct 2026 —
    but linking to the subscription turns out to pull in that subscription's
    own pending item on its own, so nothing else has to ask for it. Verified
    end to end against the real Stripe test API: one charge, for the full
    price, on the subscription's own invoice list, period shifted to today
    through one year from today.
    """
    if not sub.stripe_subscription_id:
        raise BillingNotConfigured("This subscription hasn't saved a card yet.")
    client = _client()
    client.InvoiceItem.create(
        customer=sub.stripe_customer_id, amount=int(sub.price_aud * 100), currency="aud",
        description=_plan_description(sub), subscription=sub.stripe_subscription_id,
    )
    invoice = client.Invoice.create(
        customer=sub.stripe_customer_id, auto_advance=True, collection_method="charge_automatically",
        subscription=sub.stripe_subscription_id,
    )
    invoice = client.Invoice.finalize_invoice(invoice.id)
    if getattr(invoice, "status", "") != "paid":
        invoice = client.Invoice.pay(invoice.id)
    client.Subscription.modify(sub.stripe_subscription_id, billing_cycle_anchor="now", proration_behavior="none")

    sub.status = PlanSubscription.STATUS_ACTIVE
    sub.paid_at = timezone.now()
    hosted_url = getattr(invoice, "hosted_invoice_url", "") or ""
    if hosted_url:
        sub.stripe_invoice_url = hosted_url
    sub.save(update_fields=["status", "paid_at", "stripe_invoice_url"])
    return sub


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

    The webhook does the same in production; this is what the success page,
    the demo poll, and the plans page's "your subscription" panel all call,
    so the screen is right even where no webhook can reach (a laptop running
    the site locally). Returns what the screen shows — including, since
    10 Oct 2026, the real billing-period dates and the real payment history,
    because "show me when the subscription started and when it's about to
    end" and "the history of payments" both have to be Stripe's own numbers,
    not a guess at what a yearly plan's dates probably are.
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
            "clock": "", "current_period_start": "", "current_period_end": "",
            "cancel_at_period_end": sub.cancel_at_period_end, "history": []}
    if not sub.stripe_subscription_id:
        return info
    s_obj = client.Subscription.retrieve(sub.stripe_subscription_id).to_dict()
    info["stripe_status"] = s_obj.get("status", "")
    # Moved from the subscription itself to its one line item on this
    # account's API version (found by reading the real object back rather
    # than trusting the docs — the same kind of rename as checkout's ui_mode
    # and invoice.subscription elsewhere in this file).
    items = (s_obj.get("items") or {}).get("data") or []
    period = items[0] if items else {}
    if period.get("current_period_start"):
        info["current_period_start"] = datetime.fromtimestamp(
            period["current_period_start"], tz=timezone.get_current_timezone()).isoformat()
    if period.get("current_period_end"):
        info["current_period_end"] = datetime.fromtimestamp(
            period["current_period_end"], tz=timezone.get_current_timezone()).isoformat()
    # Stripe's own word on whether this will renew — self-healing against the
    # local flag the same way .status already is below, in case a cancel was
    # made directly in the Stripe dashboard rather than through cancel_subscription.
    live_cancel = bool(s_obj.get("cancel_at_period_end"))
    if live_cancel != sub.cancel_at_period_end:
        sub.cancel_at_period_end = live_cancel
        sub.save(update_fields=["cancel_at_period_end"])
    info["cancel_at_period_end"] = live_cancel

    invoices = client.Invoice.list(subscription=sub.stripe_subscription_id, limit=24).to_dict()["data"]
    paid = [i for i in invoices if i.get("status") == "paid" and (i.get("amount_paid") or 0) > 0]
    info["paid_count"] = len(paid)
    if paid:
        record_invoice_paid(sub, paid[0])
    # Every invoice Stripe has actually raised on this subscription, newest
    # first (Invoice.list's own order) — paid, open, void, whatever it is;
    # the history should say what happened, not only the successes.
    info["history"] = [{
        "date": datetime.fromtimestamp(inv["created"], tz=timezone.get_current_timezone()).isoformat(),
        "amount": (inv.get("amount_paid") or inv.get("amount_due") or 0) / 100,
        "status": inv.get("status") or "",
        "number": inv.get("number") or "",
        "url": inv.get("hosted_invoice_url") or "",
    } for inv in invoices]
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


def past_subscription(org):
    """The most recently cancelled subscription this season, once there is no
    current one — so the Plans page can still say something true rather than
    reading as though nothing had ever happened here (client, 12 Oct 2026:
    "have that place where it tells me... you have not auto-subscribed to
    any subscription... when this ends"). Covers both shapes cancelling can
    leave behind: cancelled before the first charge (paid_at is never set),
    and cancelled after paying, where the organisation was genuinely covered
    through a real date that is worth still being able to see.
    """
    return org.subscriptions.filter(
        season=org.season, status=PlanSubscription.STATUS_EXPIRED,
    ).order_by("-canceled_at", "-created_at").first()


def latest_subscription(org):
    """The most recent subscription attempt this season, whatever its state."""
    return org.subscriptions.filter(season=org.season).exclude(
        status=PlanSubscription.STATUS_EXPIRED
    ).order_by("-created_at").first()
