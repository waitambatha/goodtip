from django.db import transaction
from django.db.models import Count, Q, Sum, Value
from django.db.models.functions import Coalesce

from .models import Match, Round, Tip


def derive_result(home_score: int | None, away_score: int | None) -> str | None:
    if home_score is None or away_score is None:
        return None
    if home_score > away_score:
        return "home"
    if away_score > home_score:
        return "away"
    return "draw"


@transaction.atomic
def record_match_result(match: Match, home_score: int, away_score: int) -> int:
    match.home_score = home_score
    match.away_score = away_score
    match.result = derive_result(home_score, away_score)
    match.save(update_fields=["home_score", "away_score", "result"])
    return _recalculate_tips_for_match(match)


def _recalculate_tips_for_match(match: Match) -> int:
    if match.result is None:
        return 0
    # The round's stage decides what a correct tip is worth (1 / 2 / 4).
    points = match.round.points_per_correct
    updated = 0
    if match.result == "draw":
        updated += match.tips.update(is_correct=False, points_awarded=0)
    else:
        winning = match.result
        updated += match.tips.filter(selection=winning).update(is_correct=True, points_awarded=points)
        updated += match.tips.exclude(selection=winning).update(is_correct=False, points_awarded=0)
    return updated


@transaction.atomic
def submit_tip(*, user, match: Match, org, selection: str) -> Tip:
    if match.is_locked:
        raise ValueError("Match is locked")
    if selection not in ("home", "away"):
        raise ValueError("Invalid selection")
    tip, _ = Tip.objects.update_or_create(
        user=user, match=match, org=org,
        defaults={"selection": selection},
    )
    return tip


def _leaderboard(org_ids, tip_filter):
    from accounts.models import User
    qs = User.objects.filter(memberships__org_id__in=org_ids).distinct()
    return qs.annotate(
        # Weighted score: sum of points_awarded (finals and Origin count for more).
        points=Coalesce(Sum("tips__points_awarded", filter=tip_filter), Value(0)),
        tips_total=Count("tips", filter=tip_filter & Q(tips__is_correct__isnull=False)),
        tips_correct=Count("tips", filter=tip_filter & Q(tips__is_correct=True)),
    ).order_by("-points", "display_name")


def leaderboard_for_org(org, round_id: int | None = None):
    tip_filter = Q(tips__org=org)
    if round_id is not None:
        tip_filter &= Q(tips__match__round_id=round_id)
    return _leaderboard([org.id], tip_filter)


def leaderboard_for_family(org, round_id: int | None = None):
    """The §8 national board: every member across the top-level parent and
    all its children, ranked together. Same underlying competition as the
    local board — each member's points come from their own org's tips.

    Rounds are per-org rows, so a round filter is aligned across the family
    by (round_number, series) rather than by the id of one org's round.
    """
    family_ids = org.family_ids()
    tip_filter = Q(tips__org_id__in=family_ids)
    if round_id is not None:
        rnd = Round.objects.filter(pk=round_id).only("round_number", "series_id").first()
        if rnd is not None:
            tip_filter &= Q(
                tips__match__round__round_number=rnd.round_number,
                tips__match__round__series_id=rnd.series_id,
            )
    return _leaderboard(family_ids, tip_filter)


def user_org_stats(user, org):
    tips = Tip.objects.filter(user=user, org=org)
    points = tips.aggregate(p=Coalesce(Sum("points_awarded"), Value(0)))["p"]
    correct = tips.filter(is_correct=True).count()
    graded = tips.filter(is_correct__isnull=False).count()
    return {"points": points, "tips_correct": correct, "tips_graded": graded, "tips_submitted": tips.count()}


def user_rank_in_org(user, org) -> int | None:
    board = list(leaderboard_for_org(org).values("id", "points"))
    if not board:
        return None
    last_points = None
    rank = 0
    real_rank = 0
    for row in board:
        real_rank += 1
        if row["points"] != last_points:
            rank = real_rank
            last_points = row["points"]
        if row["id"] == user.id:
            return rank
    return None
