"""The Good List — read-only aggregation over settled donation money.

Implements the build spec ("THE GOOD LIST — Leaderboard build spec"). This
module writes nothing; it is a pure view over ``DonationPayment``. The rules it
enforces:

* Settled money only (§5.3) — pledged / in-progress money never counts.
* National total is always safe to show; it names no group.
* By-charity / by-state / by-industry aggregates are hidden until at least
  ``privacy_min_groups`` groups sit behind them (§5.2, §7.1).
* The By-Group board (real names + totals) shows only groups that have
  consented (§4), and the whole board stays hidden until at least
  ``credibility_min_groups`` such groups exist (§7.2).

Both thresholds are read from ``GoodListConfig`` so they're tunable in admin
without a redeploy.
"""
from __future__ import annotations

from decimal import Decimal

from django.db.models import Count, Sum

from catalog.models import GoodListConfig
from orgs.models import Organisation

from .models import DonationPayment

COMMUNITY_SLUG = "community"


def _q(value) -> Decimal:
    return Decimal(value or 0).quantize(Decimal("0.01"))


def _settled():
    """Base queryset: only donation money that has actually cleared."""
    return DonationPayment.objects.filter(settled_at__isnull=False)


def national_total() -> Decimal:
    """Total settled money raised across every group. Always safe to show."""
    return _q(_settled().aggregate(s=Sum("amount_aud"))["s"])


def _aggregate_by(group_field: str, *, label_map=None):
    """Sum settled money grouped by an org attribute, gated by group count.

    Returns rows ``{key, label, groups, raised}`` sorted by raised desc, keeping
    only groups whose distinct-org count meets the privacy threshold (§7.1).
    """
    cfg = GoodListConfig.get()
    rows = (
        _settled()
        .values(group_field)
        .annotate(groups=Count("org", distinct=True), raised=Sum("amount_aud"))
        .filter(groups__gte=cfg.privacy_min_groups)
        .order_by("-raised")
    )
    out = []
    for r in rows:
        key = r[group_field]
        if key in (None, ""):
            continue
        label = label_map.get(key, key) if label_map else key
        out.append({
            "key": key, "label": label,
            "groups": r["groups"], "raised": _q(r["raised"]),
        })
    return out


def by_charity() -> list[dict]:
    return _aggregate_by("charity__name")


def by_state() -> list[dict]:
    return _aggregate_by("org__state__name")


def by_industry() -> list[dict]:
    return _aggregate_by("org__industry__name")


def _consenting_org_totals(group_type_slug: str | None = None):
    """Settled totals for publicly-consenting orgs, ranked. Excludes $0 groups."""
    qs = Organisation.objects.filter(
        is_public_listed=True,
        donation_payments__settled_at__isnull=False,
    )
    if group_type_slug is not None:
        qs = qs.filter(group_type__slug=group_type_slug)
    return (
        qs.annotate(raised=Sum("donation_payments__amount_aud"))
        .filter(raised__gt=0)
        .select_related("charity", "industry", "state")
        .order_by("-raised", "name")
        .distinct()
    )


def consenting_group_count() -> int:
    """How many named, consenting groups have a settled total (drives §7.2)."""
    return _consenting_org_totals().count()


def board_is_live() -> bool:
    """True once the public By-Group board clears the credibility threshold."""
    return consenting_group_count() >= GoodListConfig.get().credibility_min_groups


def by_group(group_type_slug: str | None = None) -> list[dict]:
    """Ranked public By-Group board — empty until the board goes live (§7.2).

    Pass ``COMMUNITY_SLUG`` for the Community surface so clubs rank among
    themselves rather than against corporate budgets (§8).
    """
    if not board_is_live():
        return []
    return [
        {
            "org": org,
            "name": org.name,
            "charity": org.charity,
            "state": org.state.name if org.state_id else "",
            "industry": org.industry.name if org.industry_id else "",
            "raised": _q(org.raised),
        }
        for org in _consenting_org_totals(group_type_slug=group_type_slug)
    ]


def _anonymised_label(org) -> str:
    kind = "community group" if (org.group_type_id and org.group_type.slug == COMMUNITY_SLUG) else "workplace"
    where = org.state.name if org.state_id else "Australia"
    return f"A {kind} in {where}"


def private_board(viewer_org) -> list[dict]:
    """The in-app board for a signed-in manager (spec §3, Private Good List).

    Every group with a settled total is ranked. The viewer's own group is named;
    all others are anonymised unless they've opted into public naming — a
    signed-in manager never sees every group's named standing.
    """
    orgs = (
        Organisation.objects.filter(donation_payments__settled_at__isnull=False)
        .annotate(raised=Sum("donation_payments__amount_aud"))
        .filter(raised__gt=0)
        .select_related("charity", "group_type", "state")
        .order_by("-raised", "name")
        .distinct()
    )
    board = []
    for rank, org in enumerate(orgs, start=1):
        is_self = org.id == viewer_org.id
        named = is_self or org.is_public_listed
        board.append({
            "rank": rank,
            "is_self": is_self,
            "name": org.name if named else _anonymised_label(org),
            "raised": _q(org.raised),
        })
    return board
