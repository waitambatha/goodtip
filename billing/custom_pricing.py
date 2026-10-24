"""The custom-price conversation, shared by both sides of it.

CLIENT, 24 OCT 2026: "being able to custom request a price to the admin,
being able to see some notifications, being able to see some follow-up if
they have accepted or things like that, me being able to reach back".

billing.views (the organisation's Custom pricing page) and
sysadmin.custom_pricing_views (the team's queue) both write through here, so
"who gets told, and how" is decided once:

  organisation -> team   email to everyone holding billing.custom_pricing
  team -> organisation   a bell-panel notification for the people who run
                         the organisation, and an email to whoever asked

Telling anyone is best-effort. The message or decision is saved first; a
mail or notification failure is logged, never raised.
"""
import logging
from decimal import Decimal

from django.urls import reverse
from django.utils import timezone

from .models import CustomPriceMessage, CustomPriceRequest
from .pricing import TIER_ACCENT, TIER_ORDER, effective_tier_config

logger = logging.getLogger(__name__)

#: What the request form offers as "what kind of organisation" — the same
#: groups the ready-made packages are written for, plus a way out.
ORG_KINDS = [
    ("school", "School or campus"),
    ("club", "Sports or social club"),
    ("charity", "Charity or not-for-profit"),
    ("franchise", "Franchise or branch network"),
    ("workplace", "Large workplace"),
    ("other", "Something else"),
]


def org_kind_label(key: str) -> str:
    return dict(ORG_KINDS).get(key, "")


def team_emails() -> list[str]:
    from accounts.models import User
    from sysadmin import access as admin_access

    return [
        u.email for u in User.objects.filter(is_staff=True, is_active=True).exclude(email="")
        if admin_access.can(u, "billing.custom_pricing")
    ]


def page_link(req: CustomPriceRequest) -> str:
    """Where the organisation reads this conversation."""
    return reverse("billing:packages", args=[req.org_id]) + "#track"


def tell_team(req: CustomPriceRequest, *, template: str, subject: str, extra: dict | None = None) -> None:
    from goodtip.mail import send_template, site_url

    try:
        emails = team_emails()
        if emails:
            send_template(
                template, subject=subject, to=emails,
                context={"req": req, "org": req.org,
                         "queue_link": site_url(reverse("admin:hq_custom_pricing")), **(extra or {})},
                reply_to=[req.requested_by.email] if req.requested_by.email else None,
            )
    except Exception:  # noqa: BLE001 — saved already; telling is a courtesy
        logger.exception("Custom price request %s: the team's email failed", req.pk)


def tell_org(req: CustomPriceRequest, *, title: str, message: str, email_template: str = "",
             subject: str = "", extra: dict | None = None) -> None:
    """The bell panel for the people who run the organisation, and — when a
    template is given — an email to whoever asked."""
    from orgs.models import Notification
    from orgs.services import notify, org_admin_users

    people = {u.pk: u for u in org_admin_users(req.org)}
    people.setdefault(req.requested_by_id, req.requested_by)
    notify(list(people.values()), kind=Notification.KIND_MESSAGE, title=title, message=message[:280],
           link_url=page_link(req), org=req.org)

    if not (email_template and req.requested_by.email):
        return
    from goodtip.mail import send_template, site_url

    try:
        send_template(
            email_template, subject=subject or title, to=[req.requested_by.email],
            context={"req": req, "org": req.org, "plans_link": site_url(page_link(req)), **(extra or {})},
        )
    except Exception:  # noqa: BLE001
        logger.exception("Custom price request %s: the organisation's email failed", req.pk)


def post_message(req: CustomPriceRequest, *, author, body: str, from_team: bool) -> CustomPriceMessage:
    msg = CustomPriceMessage.objects.create(request=req, author=author, body=body[:2000], from_team=from_team)
    if from_team:
        tell_org(req, title="The GoodTip team replied about your custom price",
                 message=body, email_template="custom_price_message",
                 subject=f"A reply about {req.org.name}'s custom price", extra={"msg": msg})
    else:
        tell_team(req, template="custom_price_message",
                  subject=f"{req.org.name} wrote about their custom price", extra={"msg": msg})
    return msg


def timeline(req: CustomPriceRequest) -> list[dict]:
    """Everything that happened to one request, oldest first — the asking,
    every message, the team's decision and the organisation's answer — as
    one list both screens draw the same way."""
    who = req.requested_by.display_name if hasattr(req.requested_by, "display_name") else ""
    events = [{
        "kind": "sent", "at": req.created_at, "side": "org",
        "title": "Request sent", "who": who or req.requested_by.email, "body": req.message,
    }]
    for m in req.thread.all():
        name = ""
        if m.author:
            name = getattr(m.author, "display_name", "") or m.author.email
        events.append({
            "kind": "message", "at": m.created_at, "side": "team" if m.from_team else "org",
            "title": "GoodTip team" if m.from_team else "You wrote", "who": name, "body": m.body,
        })
    if req.decided_at:
        if req.status == CustomPriceRequest.STATUS_APPROVED:
            events.append({
                "kind": "offer", "at": req.decided_at, "side": "team",
                "title": f"Offer: {req.label}", "body": req.decision_note,
                "offer": {"price": req.price_aud, "seats": req.seat_limit},
            })
        else:
            events.append({
                "kind": "declined", "at": req.decided_at, "side": "team",
                "title": "The team kept to the five plans", "body": req.decision_note,
            })
    if req.org_declined_at:
        events.append({
            "kind": "refused", "at": req.org_declined_at, "side": "org",
            "title": "Offer turned down", "body": req.org_decline_reason,
        })
    if req.accepted_at:
        events.append({"kind": "accepted", "at": req.accepted_at, "side": "org",
                       "title": "Offer accepted", "body": ""})
    events.sort(key=lambda e: e["at"])
    return events


#: The four steps drawn on both screens, and which stage lights which.
STEPS = [("sent", "Request sent"), ("review", "Team reviewing"), ("offer", "Offer or answer"), ("done", "Your decision")]
_STAGE_STEP = {"waiting": 1, "offer": 2, "declined": 3, "refused": 3, "accepted": 3}


def steps_for(req: CustomPriceRequest | None) -> list[dict]:
    reached = _STAGE_STEP.get(req.stage, -1) if req else -1
    return [{"key": k, "label": label, "done": i < reached, "now": i == reached}
            for i, (k, label) in enumerate(STEPS)]


def standard_equivalent(people: int | None) -> dict | None:
    """The smallest published plan that would hold this many people, as a
    yardstick for a custom price — on the team's queue ("the five would
    charge this") and on each package ("instead of this")."""
    if not people:
        return None
    for key in TIER_ORDER:
        cfg = effective_tier_config(key)
        if people <= cfg["seat_limit"]:
            return {"key": key, "label": cfg["label"], "price": Decimal(cfg["price"]),
                    "seats": cfg["seat_limit"], "accent": TIER_ACCENT.get(key)}
    return None


def mark_accepted(req: CustomPriceRequest) -> None:
    if req.accepted_at:
        return
    req.accepted_at = timezone.now()
    req.save(update_fields=["accepted_at"])
    tell_team(req, template="custom_price_message",
              subject=f"{req.org.name} accepted {req.label}",
              extra={"msg": {"body": f"{req.org.name} accepted the offer — {req.label}, "
                                     f"${req.price_aud:,.0f}/yr for up to {req.seat_limit} people."}})
