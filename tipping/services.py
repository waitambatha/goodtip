from django.db import transaction
from django.db.models import Count, Q, Sum, Value
from django.db.models.functions import Coalesce

from .models import Match, Round, Tip


def annotate_play_state(rounds):
    """Add the per-round counts ``current_round`` reads.

    Kept next to ``current_round`` because the two are a pair: calling the
    latter on an unannotated queryset raises AttributeError, and doing the
    counting per round instead would be a query each.
    """
    return rounds.annotate(
        match_count=Count("matches", distinct=True),
        unplayed=Count(
            "matches",
            filter=~Q(matches__status=Match.STATUS_COMPLETE),
            distinct=True,
        ),
    )


def current_round(rounds):
    """The round a member actually cares about: the earliest one still in play.

    Expects an ``annotate_play_state`` queryset evaluated newest-first, so this
    walks backwards to the oldest round that hasn't finished.

    Deliberately NOT keyed on ``Round.is_locked``. A round locks at its first
    kickoff, so the locked round is usually the one being played right now —
    picking the first unlocked round would skip the whole weekend's games while
    they were still going and land the member on a round nobody can tip yet.
    "Every fixture graded" is the only thing that means a round is genuinely done.

    A round with no fixtures yet counts as unfinished, so one freshly created by
    a fixtures sync isn't stepped over before its games land.
    """
    for r in reversed(rounds):
        if r.unplayed > 0 or r.match_count == 0:
            return r
    # Every round has been played out — end of season. Show the most recent.
    return rounds[0] if rounds else None


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
    had_result = match.result is not None
    match.home_score = home_score
    match.away_score = away_score
    match.result = derive_result(home_score, away_score)
    match.save(update_fields=["home_score", "away_score", "result"])
    if had_result:
        # A correction after grading: any recap already posted for this round
        # is flagged for admin review, never silently rewritten (recap spec §3).
        from orgs.models import RoundRecap

        RoundRecap.objects.filter(round=match.round).update(needs_review=True)
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
def regrade_round(round_obj: Round) -> int:
    """Re-award points for every graded match in a round. Returns tips changed.

    Needed because what a correct tip is WORTH is a property of the round —
    1 regular, 2 finals, 4 for State of Origin — while ``points_awarded`` is
    frozen onto the Tip at the moment it was graded. Correcting a round's stage
    after its games have been played therefore changes nothing on its own, and
    the leaderboard keeps paying the old rate for the rest of the season.

    That is not a hypothetical: every State of Origin round in the live
    database was created on the "regular" default, so the prestige series that
    is meant to be worth quadruple was paying single, and simply setting the
    stage correctly would not have moved a single member's score.
    """
    updated = 0
    for match in round_obj.matches.filter(result__isnull=False):
        updated += _recalculate_tips_for_match(match)
    return updated


def competition_filter(org, wanted_slugs):
    """(chips, series to filter by) for the competition filter.

    Returns the series worth offering as a button, and the series a selection
    actually resolves to. They are not the same list, because State of Origin
    is not a competition anyone tips week to week — it is three representative
    games that belong to the NRL competition. Offering it as its own button
    put a code on screen that is empty for eleven months of the year and
    invited the question "why is that there".

    So representative series are never chips, and a code's chip carries them:
    choosing NRL shows the NRL rounds AND the Origin rounds, because in a
    tipping league Origin week IS the NRL round. That keeps Origin reachable
    without giving it a button of its own, and means picking a code never
    silently hides games that count toward the same ladder.

    Unknown slugs are dropped rather than 404ing — a stale bookmark should
    show everything, not an error.
    """
    from catalog.models import Series

    have = Series.objects.filter(rounds__org=org).distinct().order_by("name")
    chips = [s for s in have if s.category != Series.CATEGORY_REPRESENTATIVE]
    reps = [s for s in have if s.category == Series.CATEGORY_REPRESENTATIVE]

    by_slug = {s.slug: s for s in chips}
    active = [by_slug[v] for v in (wanted_slugs or []) if v in by_slug]
    if not active:
        return chips, []

    # Carry each chosen code's representative series along with it.
    sports = {s.sport_id for s in active}
    return chips, active + [s for s in reps if s.sport_id in sports]


# How many rounds a member may have open at once. Two: the one being played
# and the one after it.
TIP_WINDOW_ROUNDS = 2


def tip_window(org) -> dict[int, dict]:
    """Which rounds are open to tip, and for the rest, what they are waiting on.

    The earliest ``TIP_WINDOW_ROUNDS`` rounds that still have a match to play,
    per series. Once the first of them is done, the window slides forward and
    the next one opens.

    Per series, not per org, because an org tipping AFL and NRL runs two
    ladders side by side: capping the org at two rounds total would close the
    NRL round purely because two AFL rounds happened to sit in front of it.

    Returns ``{round_id: {"open": bool, "waits_for": Round | None}}``.

    ``waits_for`` is the point of this over a bare set of ids. A closed round
    must be able to say WHICH round has to finish before it opens — "locked"
    on its own is indistinguishable from "you missed it", and the two need
    opposite reactions from whoever is reading. With a window of two, the
    round at position N is released by the round at position N-2 finishing,
    so that is the one to name.

    The rule is about what may be SAVED, so submit_tip enforces it; the
    screens only reflect it.
    """
    from django.utils import timezone

    now = timezone.now()
    live = (
        Round.objects.filter(org=org)
        # A round is still live while any of its matches has yet to be played.
        .filter(matches__kickoff_at__gt=now)
        .distinct()
        .order_by("series_id", "lockout_at", "round_number")
    )
    per_series: dict[int, list] = {}
    for rnd in live:
        per_series.setdefault(rnd.series_id, []).append(rnd)

    window: dict[int, dict] = {}
    for rounds in per_series.values():
        for i, rnd in enumerate(rounds):
            if i < TIP_WINDOW_ROUNDS:
                window[rnd.id] = {"open": True, "waits_for": None}
            else:
                window[rnd.id] = {"open": False, "waits_for": rounds[i - TIP_WINDOW_ROUNDS]}
    return window


def tippable_round_ids(org) -> set[int]:
    """Just the ids, for the enforcement check in submit_tip."""
    return {rid for rid, state in tip_window(org).items() if state["open"]}


@transaction.atomic
def submit_tip(*, user, match: Match, org, selection: str) -> Tip:
    if match.is_locked:
        raise ValueError("Match is locked")
    if selection not in ("home", "away"):
        raise ValueError("Invalid selection")
    if match.round_id not in tippable_round_ids(org):
        raise ValueError(
            f"That round isn't open yet. You can tip {TIP_WINDOW_ROUNDS} rounds "
            "at a time, so this one opens once the current round is done."
        )
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
