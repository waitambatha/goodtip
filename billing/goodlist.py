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

from catalog.models import GoodListConfig, OrganisationType
from orgs.models import Organisation

from .models import DonationPayment

COMMUNITY_SLUG = OrganisationType.SLUG_COMMUNITY


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
    return _aggregate_by("org__organisation_type__name")


def by_country() -> list[dict]:
    """Raised per country.

    ATTRIBUTED THROUGH THE ORGANISATION, not the group, because money is. A
    DonationPayment carries an org and no group, so an organisation running
    offices in two countries has all of its money counted under its own
    country here. That is a real limitation and worth stating plainly: the
    group-level country field drives who is in which market (see
    `group_counts_by_country`), and money follows it only as far as the
    schema allows. Splitting a multi-country org's money would need
    DonationPayment to carry the group it came from.
    """
    return _aggregate_by("org__country__name")


def pacific_nations() -> dict:
    """The Pacific Nations board: PNG, Fiji, Samoa, Tonga, Vanuatu and the
    Solomons counted as one.

    A grouping layer over the same country data, not a new source. Its whole
    reason for existing is scale — each of these countries on its own is far
    too small to be a leaderboard, and six empty boards is worse than none,
    but together they are a competition somebody can actually be in.

    Deliberately NOT privacy-gated the way `_aggregate_by` gates a single
    country. The threshold exists so a total cannot be traced back to one
    identifiable group; pooling six countries is itself the anonymising step,
    and applying the per-row gate on top would hide the board for exactly as
    long as it is most needed. The member countries' individual rows stay
    gated.
    """
    from catalog.models import Country

    codes = list(
        Country.objects.filter(is_pacific=True, is_active=True)
        .values_list("code", flat=True)
    )
    agg = (
        _settled()
        .filter(org__country__code__in=codes)
        .aggregate(groups=Count("org", distinct=True), raised=Sum("amount_aud"))
    )
    members = (
        _settled()
        .filter(org__country__code__in=codes)
        .values("org__country__name")
        .annotate(groups=Count("org", distinct=True), raised=Sum("amount_aud"))
        .order_by("-raised")
    )
    cfg = GoodListConfig.get()
    return {
        "label": "Pacific Nations",
        "groups": agg["groups"] or 0,
        "raised": _q(agg["raised"]),
        # Shown only where a single country clears the privacy threshold on
        # its own; the pooled total above always shows.
        "members": [
            {
                "label": r["org__country__name"],
                "groups": r["groups"],
                "raised": _q(r["raised"]),
            }
            for r in members
            if r["org__country__name"] and r["groups"] >= cfg.privacy_min_groups
        ],
        "countries": list(
            Country.objects.filter(is_pacific=True, is_active=True)
            .values_list("name", flat=True)
        ),
    }


def group_counts_by_country() -> list[dict]:
    """How many GROUPS sit in each country — no money involved.

    This is what the group-level country field buys that org-level could not:
    a business headquartered in Sydney with a team in Port Moresby shows up in
    both. Counting groups rather than dollars also means it is live from the
    first signup in a new market, well before any money has settled — which is
    the point when knowing where people are is most useful.
    """
    from django.db.models import Q

    from catalog.models import Country
    from orgs.models import Group

    out = []
    for country in Country.objects.filter(is_active=True):
        # A group counts for a country when it names it, OR when it names
        # nothing and its organisation names it — the same fallback
        # Group.effective_country applies, expressed as a query.
        n = Group.objects.filter(
            Q(country=country) | Q(country__isnull=True, org__country=country),
            approval_status=Group.APPROVAL_APPROVED,
        ).count()
        orgs = Organisation.objects.filter(country=country).count()
        if n or orgs:
            out.append({
                "code": country.code, "label": country.name,
                "segment": country.get_segment_display(),
                "is_pacific": country.is_pacific,
                "groups": n, "orgs": orgs,
            })
    return out


def _consenting_org_totals(
    organisation_type_slug: str | None = None,
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
    if organisation_type_slug:
        qs = qs.filter(organisation_type__slug=organisation_type_slug)
    if sub_category_slug:
        # M2M: matches every org holding the sub-category, so a Primary +
        # Secondary school appears under both school filters (build note).
        qs = qs.filter(sub_categories__slug=sub_category_slug)
    if state_code:
        qs = qs.filter(state__code=state_code)
    return (
        qs.annotate(raised=Sum("donation_payments__amount_aud"))
        .filter(raised__gt=0)
        .select_related("charity", "organisation_type", "state")
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
    organisation_type_slug: str | None = None,
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
            "type": org.organisation_type.name if org.organisation_type_id else "",
            # Informal groups show their self-description; others their
            # sub-categories ("Primary School + Secondary School").
            "category": org.category_label,
            "state": org.state.name if org.state_id else "",
            "raised": _q(org.raised),
        }
        for org in _consenting_org_totals(
            organisation_type_slug=organisation_type_slug,
            sub_category_slug=sub_category_slug,
            state_code=state_code,
        )
    ]


# What an unnamed group is called on the private board, per type.
_ANON_KINDS = {
    OrganisationType.SLUG_COMMUNITY: "A community group",
    OrganisationType.SLUG_BUSINESS: "A business",
    OrganisationType.SLUG_EDUCATION: "An education group",
    OrganisationType.SLUG_CHARITIES: "A charity",
    OrganisationType.SLUG_INFORMAL: "An informal group",
}


def _anonymised_label(org) -> str:
    kind = _ANON_KINDS.get(org.organisation_type.slug if org.organisation_type_id else "", "A group")
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
        .select_related("charity", "organisation_type", "state")
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
