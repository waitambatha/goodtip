"""Telling a finals round from a regular one, without hardcoding a season shape.

Nothing in either feed says "this is a final" in a form we can rely on across
codes, so it is read from the shape of the season instead. That is deliberate:
the round count keeps moving. The AFL ran 22 rounds, then 23, then added an
Opening Round; the NRL has been between 24 and 27. Any constant here would be
wrong within a season or two.

What does not change is that finals are SMALL and they are LAST. A regular
round is a full slate — nine AFL games, eight NRL. A finals series narrows:
four, then two, then two, then one.
"""

from __future__ import annotations


def finals_rounds(sizes: dict[int, int]) -> set[int]:
    """Which round numbers in a season are finals, given games-per-round.

    Walks backwards from the last round, collecting rounds smaller than half
    the season's biggest. The walk stops at the first full-sized round, which
    is what makes it safe: a bye-thinned round in the middle of the season is
    never reached, however small it is.

    A LONE trailing short round is not treated as finals. That case is almost
    always the round currently being played — half its games are finished and
    stored, the rest have not kicked off — and flagging it would quietly drop
    the current round out of the ladder. Every real finals series is at least
    two weeks long, so requiring two costs nothing and removes the false
    positive. The price is that a season fetched no further than its grand
    final goes unflagged; it is corrected as soon as the rest is backfilled.
    """
    if not sizes:
        return set()

    threshold = max(sizes.values()) / 2
    trailing: list[int] = []
    for rn in sorted(sizes, reverse=True):
        if sizes[rn] < threshold:
            trailing.append(rn)
        else:
            break

    return set(trailing) if len(trailing) >= 2 else set()


def restage_season(series, season: int) -> set[int]:
    """Re-mark one season's finals in HistoricalMatch. Returns the finals rounds.

    Extracted from ``backfill_history`` so the scheduled results sync can call
    it too. That is the whole point: staging used to happen ONLY inside a
    manual backfill command, so a finals round pulled in by the nightly sync
    stayed marked "regular" and counted towards the ladder until somebody
    remembered to re-run the backfill by hand. A ladder that silently includes
    a preliminary final is worse than one that is a day stale.

    Both directions are written, so a round that stops looking like finals goes
    back to regular and re-running never leaves a stale flag behind.
    """
    from matchreader.models import HistoricalMatch

    base = HistoricalMatch.objects.filter(series=series, season=season)

    sizes: dict[int, int] = {}
    for rn in base.values_list("round_number", flat=True):
        sizes[rn] = sizes.get(rn, 0) + 1

    finals = finals_rounds(sizes)
    base.filter(round_number__in=finals).update(stage=HistoricalMatch.STAGE_FINALS)
    base.exclude(round_number__in=finals).update(stage=HistoricalMatch.STAGE_REGULAR)

    # Second pass: a source that numbers finals wrongly defeats the round-shape
    # rule entirely, so anything played inside the finals window is caught by
    # date regardless of what round it claims to be in.
    misfiled = finals_by_date(
        base.values_list("pk", "round_number", "kickoff_at"), finals
    )
    if misfiled:
        base.filter(pk__in=misfiled).update(stage=HistoricalMatch.STAGE_FINALS)

    return finals


def stage_for_round(series, season: int, round_number: int, game_count: int) -> str:
    """The ``tipping.Round.stage`` a synced round should carry: what it is WORTH.

    Distinct from the HistoricalMatch staging above, which only has to answer
    "does this count towards the ladder". This one drives scoring — a correct
    tip is worth 1 in a regular round, 2 in a final and 4 in State of Origin —
    and it has to be decided when the fixtures land, BEFORE the games are
    played and before any history exists for them.

    Origin needs no inference: it is a whole series, so the category settles it.
    Every State of Origin round in the live database was sitting on "regular"
    and paying 1 point instead of 4, purely because nothing ever set this.

    Finals are inferred from the same fact the ladder heuristic leans on —
    finals rounds are SMALL and they are LAST — but measured against this
    season's own history rather than a hardcoded round number, because the
    round count keeps moving. Two conditions, and both must hold:

      * the round is at most half the size of a normal round this season. AFL
        finals week 1 is 4 games against a 9-game round; NRL is 4 against 8.
      * it comes after the last full-sized round. This is what stops a
        bye-thinned round being mistaken for finals — those have full rounds
        after them, finals never do.

    With no history for the season yet there is nothing to measure against, so
    it stays regular. That is the right way to be wrong: under-paying a final
    is a visible, correctable number, while paying double for a regular round
    silently inflates a leaderboard nobody can then reconcile.
    """
    from catalog.models import Series
    from matchreader.models import HistoricalMatch
    from tipping.models import Round

    if getattr(series, "category", None) == Series.CATEGORY_REPRESENTATIVE:
        return Round.STAGE_ORIGIN

    if not game_count:
        return Round.STAGE_REGULAR

    sizes: dict[int, int] = {}
    for rn in HistoricalMatch.objects.filter(
        series=series, season=season
    ).values_list("round_number", flat=True):
        sizes[rn] = sizes.get(rn, 0) + 1
    if not sizes:
        return Round.STAGE_REGULAR

    typical = max(sizes.values())
    full = [rn for rn, n in sizes.items() if n * 2 > typical]
    last_full = max(full) if full else 0

    if game_count * 2 <= typical and round_number > last_full:
        return Round.STAGE_FINALS
    return Round.STAGE_REGULAR


def finals_by_date(rows, finals: set[int]):
    """Primary keys of games played after the regular season ended.

    The round-shape rule above reads ROUND NUMBERS, so it is defeated by a
    source that numbers finals wrongly. Sportradar did exactly that: it
    labelled a 6 September 2025 final as "round 1", where it sat among the
    March fixtures counting towards the regular-season ladder.

    Dates cannot be argued with. The regular season ends with the last game of
    the last regular round; anything played after that is a final, whatever
    round it claims to belong to.

    The anchor is the finals window itself. ``finals`` names the rounds the
    shape heuristic already identified, so the finals series begins at the
    earliest kickoff among them — and no regular-season game is played once
    that window opens. Any row still calling itself regular but kicking off
    inside it is misfiled.

    Anchoring this way rather than measuring a gap after the last regular round
    matters: the first attempt asked whether a game sat more than a fortnight
    past the end of the season, and AFL 2025's finals began THIRTEEN days
    after round 24, so it caught nothing. Any threshold is a guess; the finals
    window is a fact already in the data.

    ``rows`` is an iterable of (pk, round_number, kickoff_at). Returns the pks
    that should be marked finals but are not already covered by ``finals``.
    """
    rows = list(rows)
    if not rows or not finals:
        # With no finals rounds identified there is nothing to anchor against,
        # and guessing would be worse than leaving the season alone.
        return set()

    finals_start = min(k for _, rn, k in rows if rn in finals)
    return {pk for pk, rn, k in rows if rn not in finals and k >= finals_start}
