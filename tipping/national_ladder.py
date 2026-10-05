"""The Industry and National ladders — organisations ranked against each
other on tipping points, alongside what they've raised.

One table, not two: "Industry" is this same board filtered to one
organisation type, "National" is the same board with no filter. Both read
from `ladder()` below.

Mirrors billing/goodlist.py closely on purpose — same opt-in flag
(``Organisation.is_public_listed``), same ``GoodListConfig`` thresholds, same
organisation type / sub-category / state filters — so an organisation that
has already opted in to the donations Good List is opted in here too, with
nothing new to configure.

Points and dollars are queried SEPARATELY and merged in Python, not combined
into one ``annotate()``. Summing two multi-valued child relations (``tips``
and ``donation_payments``) in the same query would join both into the
aggregate and fan them out against each other — the exact cross-product bug
``tipping.services._leaderboard`` documents for ``OrgMember``, just with a
different pair of relations.
"""
from __future__ import annotations

from django.db.models import Count, F, Q, Sum, Value
from django.db.models.functions import Coalesce

from billing.goodlist import _q, _settled
from catalog.models import GoodListConfig
from orgs.models import Organisation

from .models import Tip


def _points_by_org(org_ids: list[int]) -> dict[int, dict]:
    """Season points and graded-tip counts, per org.

    Grouped by ``Tip.org`` directly — never through ``OrgMember``/``User`` —
    so no org's total can be multiplied by anything.

    ``group__isnull=True`` excludes tips made inside a group, for the same
    reason ``leaderboard_for_org`` excludes them from an organisation's own
    board: pooling group tips in with the organisation's would double-count
    anyone who tips in both.

    Scoped to each org's OWN current season via ``F("org__season")`` rather
    than a fixed value, since orgs are not all necessarily on the same one.
    """
    rows = (
        Tip.objects.filter(
            org_id__in=org_ids,
            group__isnull=True,
            match__round__competition__season=F("org__season"),
        )
        .values("org")
        .annotate(
            points=Coalesce(Sum("points_awarded"), Value(0)),
            tips_total=Count("id", filter=Q(is_correct__isnull=False)),
            tippers=Count("user", filter=Q(is_correct__isnull=False), distinct=True),
        )
    )
    return {r["org"]: r for r in rows}


def _consenting_orgs(
    organisation_type_slug: str | None = None,
    sub_category_slug: str | None = None,
    state_code: str | None = None,
):
    qs = Organisation.objects.filter(is_public_listed=True)
    if organisation_type_slug:
        qs = qs.filter(organisation_type__slug=organisation_type_slug)
    if sub_category_slug:
        # M2M: matches every org holding the sub-category, so a Primary +
        # Secondary school appears under both filters (goodlist does the same).
        qs = qs.filter(sub_categories__slug=sub_category_slug)
    if state_code:
        qs = qs.filter(state__code=state_code)
    return (
        qs.select_related("organisation_type", "state", "season")
        .prefetch_related("sub_categories")
        .distinct()
    )


def consenting_org_count() -> int:
    """How many opted-in organisations exist at all, filters aside.

    Drives the credibility gate below — deliberately unfiltered, so a narrow
    filter can't make the board look emptier than the real pool behind it.
    """
    return Organisation.objects.filter(is_public_listed=True).count()


def board_is_live() -> bool:
    """True once the board clears the same credibility threshold the
    donations Good List uses (§7.2) — it is the same opt-in pool."""
    return consenting_org_count() >= GoodListConfig.get().credibility_min_groups


def ladder(
    organisation_type_slug: str | None = None,
    sub_category_slug: str | None = None,
    state_code: str | None = None,
) -> list[dict]:
    """Ranked board of consenting organisations: tipping points, and dollars
    raised, side by side.

    Called with no filters for the National ladder, or with
    ``organisation_type_slug`` set to a sector for the Industry ladder — the
    same table, filtered differently.

    Empty until ``board_is_live()``, and empty for any org with neither a
    graded tip nor a settled donation, the same "nothing invented" rule
    ``billing.goodlist`` applies to its own board.
    """
    if not board_is_live():
        return []

    orgs = list(_consenting_orgs(organisation_type_slug, sub_category_slug, state_code))
    if not orgs:
        return []
    org_ids = [o.id for o in orgs]

    points_map = _points_by_org(org_ids)
    raised_map = {
        r["org"]: r["raised"]
        for r in _settled().filter(org_id__in=org_ids).values("org").annotate(raised=Sum("amount_aud"))
    }

    board = []
    for org in orgs:
        pts_row = points_map.get(org.id)
        points = pts_row["points"] if pts_row else 0
        tips_total = pts_row["tips_total"] if pts_row else 0
        tippers = pts_row["tippers"] if pts_row else 0
        raised = _q(raised_map.get(org.id))
        if points <= 0 and raised <= 0:
            continue
        board.append({
            "org": org,
            "name": org.name,
            "type": org.organisation_type.name if org.organisation_type_id else "",
            "category": org.category_label,
            "state": org.state.name if org.state_id else "",
            "points": points,
            "tips_total": tips_total,
            "tippers": tippers,
            # 4 Oct 2026: ranked like the member Leaderboard's boards
            # (tipping/rankings.py) — average per active tipper, so size
            # does not decide it — or the two would disagree on who leads.
            "avg": round(points / tippers, 1) if tippers else 0,
            "raised": raised,
        })
    board.sort(key=lambda r: (-r["avg"], -r["points"], -r["raised"], r["name"]))
    for i, row in enumerate(board, start=1):
        row["rank"] = i
    return board
