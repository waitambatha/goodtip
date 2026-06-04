from django.db import transaction
from django.db.models import Count, Q

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
    updated = 0
    if match.result == "draw":
        updated += match.tips.update(is_correct=False)
    else:
        winning = match.result
        updated += match.tips.filter(selection=winning).update(is_correct=True)
        updated += match.tips.exclude(selection=winning).update(is_correct=False)
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


def leaderboard_for_org(org, round_id: int | None = None):
    from accounts.models import User
    qs = User.objects.filter(memberships__org=org).distinct()
    tip_filter = Q(tips__org=org)
    if round_id is not None:
        tip_filter &= Q(tips__match__round_id=round_id)
    qs = qs.annotate(
        points=Count("tips", filter=tip_filter & Q(tips__is_correct=True)),
        tips_total=Count("tips", filter=tip_filter & Q(tips__is_correct__isnull=False)),
        tips_correct=Count("tips", filter=tip_filter & Q(tips__is_correct=True)),
    ).order_by("-points", "display_name")
    return qs


def user_org_stats(user, org):
    tips = Tip.objects.filter(user=user, org=org)
    correct = tips.filter(is_correct=True).count()
    graded = tips.filter(is_correct__isnull=False).count()
    return {"points": correct, "tips_graded": graded, "tips_submitted": tips.count()}


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
