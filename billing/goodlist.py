"""The Good List — read-only aggregation over settled donation money.

Implements the build spec ("THE GOOD LIST — Leaderboard build spec"). This
module writes nothing; it is a pure view over ``DonationPayment``. The rules it
enforces:

* Settled money only (§5.3) — pledged / in-progress money never counts.
* National total is always safe to show; it names no group.
* By-charity / by-state / by-sub-category aggregates are hidden until at least
  ``privacy_min_groups`` groups sit behind them (§5.2, §7.1).
* The By-Group board (real names + totals) shows only groups that have
  consented (§4), and the whole board stays hidden until at least
  ``credibility_min_groups`` such groups exist (§7.2).
* The board is filterable by organisation type, sub-category within that type,
  and state/territory (categories doc, 7 Jul 2026). An Education org holding
  both Primary and Secondary sub-categories surfaces under both filters.

Both thresholds are read from ``GoodListConfig`` so they're tunable in admin
without a redeploy.
"""
from __future__ import annotations

from decimal import Decimal

from django.db.models import Count, Sum

from catalog.models import GoodListConfig, GroupType
from orgs.models import Organisation

from .models import DonationPayment

COMMUNITY_SLUG = GroupType.SLUG_COMMUNITY


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


def by_sub_category() -> list[dict]:
    """Raised per sub-category (the old "By Industry", generalised). An org
    holding two sub-categories (Primary + Secondary school) counts under both.
    """
    return _aggregate_by("org__sub_categories__name")


def by_type() -> list[dict]:
    """Raised per organisation type (Community, Business, …)."""
    return _aggregate_by("org__group_type__name")


def _consenting_org_totals(
    group_type_slug: str | None = None,
    sub_category_slug: str | None = None,
    state_code: str | None = None,
):
    """Settled totals for publicly-consenting orgs, ranked. Excludes $0 groups.

    The three optional filters are the Good List's public controls (categories
    doc): organisation type, sub-category within that type, state/territory.
    """
    qs = Organisation.objects.filter(
        is_public_listed=True,
        donation_payments__settled_at__isnull=False,
    )
    if group_type_slug:
        qs = qs.filter(group_type__slug=group_type_slug)
    if sub_category_slug:
        # M2M: matches every org holding the sub-category, so a Primary +
        # Secondary school appears under both school filters (build note).
        qs = qs.filter(sub_categories__slug=sub_category_slug)
    if state_code:
        qs = qs.filter(state__code=state_code)
    return (
        qs.annotate(raised=Sum("donation_payments__amount_aud"))
        .filter(raised__gt=0)
        .select_related("charity", "group_type", "state")
        .prefetch_related("sub_categories")
        .order_by("-raised", "name")
        .distinct()
    )


def consenting_group_count() -> int:
    """How many named, consenting groups have a settled total (drives §7.2)."""
    return _consenting_org_totals().count()


def board_is_live() -> bool:
    """True once the public By-Group board clears the credibility threshold."""
    return consenting_group_count() >= GoodListConfig.get().credibility_min_groups


def by_group(
    group_type_slug: str | None = None,
    sub_category_slug: str | None = None,
    state_code: str | None = None,
) -> list[dict]:
    """Ranked public By-Group board — empty until the board goes live (§7.2).

    Filterable by organisation type, sub-category, and state/territory
    (categories doc). Pass ``COMMUNITY_SLUG`` for the Community surface so
    clubs rank among themselves rather than against corporate budgets (§8).
    """
    if not board_is_live():
        return []
    return [
        {
            "org": org,
            "name": org.name,
            "charity": org.charity,
            "type": org.group_type.name if org.group_type_id else "",
            # Informal groups show their self-description; others their
            # sub-categories ("Primary School + Secondary School").
            "category": org.category_label,
            "state": org.state.name if org.state_id else "",
            "raised": _q(org.raised),
        }
        for org in _consenting_org_totals(
            group_type_slug=group_type_slug,
            sub_category_slug=sub_category_slug,
            state_code=state_code,
        )
    ]


# What an unnamed group is called on the private board, per type.
_ANON_KINDS = {
    GroupType.SLUG_COMMUNITY: "A community group",
    GroupType.SLUG_BUSINESS: "A business",
    GroupType.SLUG_EDUCATION: "An education group",
    GroupType.SLUG_CHARITIES: "A charity",
    GroupType.SLUG_INFORMAL: "An informal group",
}


def _anonymised_label(org) -> str:
    kind = _ANON_KINDS.get(org.group_type.slug if org.group_type_id else "", "A group")
    where = org.state.name if org.state_id else "Australia"
    return f"{kind} in {where}"


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
