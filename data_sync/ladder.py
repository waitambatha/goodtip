"""The competition ladder, computed from results we already hold.

Replaces the last two feed dependencies in one move. Before this, standings
were the only thing GoodTip could not produce for itself:

    AFL/AFLW   delegated to Squiggle, because afl.com.au's CFS ladder endpoint
               is behind the premium tier
    NRL/NRLW   raised outright — nrl.com serves standings from /ladder/, which
               was never collected

Both of those are network calls to somebody else's server for numbers that are
a pure function of match results sitting in our own database. A ladder is
arithmetic over completed games, so it is derived here instead.

WHAT THIS BUYS
--------------
No third-party call, no key, no rate limit, and NRL standings for the first
time. It also cannot silently disagree with the fixtures a tipper is looking
at, because it is computed from those exact rows.

WHERE IT CAN DRIFT FROM THE OFFICIAL LADDER
-------------------------------------------
Two places, both deliberate and both visible:

1. BYES. The NRL awards 2 competition points for a bye; the AFL awards none.
   A bye is an absence — no fixture is published for it, so there is no Match
   row to read. Byes are inferred: in a round where every published game has
   been played, a team with no game had a bye. That inference is exact for a
   normal season and wrong only if a fixture is withdrawn entirely rather than
   postponed, which is why ``byes`` is stored and shown rather than folded
   silently into the points total.

2. ADJUSTMENTS. Points deductions (salary-cap penalties, forfeits) are a
   governing-body decision and appear in no result. ``LadderAdjustment`` exists
   so an administrator can enter one; without it the derived ladder will read
   high for a penalised club. This is the one case that needs a human, and it
   is rare enough — a handful of times a decade — that a human is the right
   mechanism.

Finals are excluded: a ladder is a regular-season table, and it is what
DECIDES the finals. State of Origin has no ladder at all.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LadderRules:
    """How one code turns results into a table.

    The codes genuinely differ, and not just in the numbers: the AFL separates
    equal teams on PERCENTAGE (points for / points against) while the NRL uses
    the FOR-AND-AGAINST DIFFERENCE (points for - points against). Those rank
    differently. Two NRL teams on +40 and +40 from wildly different totals are
    level; the same two AFL teams are not.
    """

    win: int
    draw: int
    loss: int
    bye: int
    # "percentage" (AFL) or "differential" (NRL) — the separator for equal points.
    separator: str


RULES = {
    "AFL":  LadderRules(win=4, draw=2, loss=0, bye=0, separator="percentage"),
    "AFLW": LadderRules(win=4, draw=2, loss=0, bye=0, separator="percentage"),
    "NRL":  LadderRules(win=2, draw=1, loss=0, bye=2, separator="differential"),
    "NRLW": LadderRules(win=2, draw=1, loss=0, bye=2, separator="differential"),
}

# Series that have no ladder. Origin is a three-game series, not a season, and
# a table of it would be meaningless.
NO_LADDER = {"STATE OF ORIGIN"}


@dataclass
class _Row:
    """One team's running tally while the table is being built."""

    team_id: int
    played: int = 0
    wins: int = 0
    losses: int = 0
    draws: int = 0
    byes: int = 0
    points_for: int = 0
    points_against: int = 0
    adjustment: int = 0

    def points(self, rules: LadderRules) -> int:
        return (
            self.wins * rules.win
            + self.draws * rules.draw
            + self.losses * rules.loss
            + self.byes * rules.bye
            + self.adjustment
        )

    @property
    def percentage(self) -> float:
        """Points for / against as a percentage.

        Zero against is possible in a women's or representative fixture early
        in a season and would divide by zero, so it is treated as an unbeaten
        ratio rather than crashing the rebuild.
        """
        if self.points_against == 0:
            return float(self.points_for * 100) if self.points_for else 0.0
        return self.points_for / self.points_against * 100

    @property
    def differential(self) -> int:
        return self.points_for - self.points_against


def ladder_standings(*, series, season, up_to_round: int | None = None):
    """The table as it stood, computed and not stored.

    THE SAME ARITHMETIC AS rebuild_ladder, LIFTED OUT OF IT rather than written
    again. The ladder page can now be asked for a past round — "users might want
    to see, let's say the last month, round 10, who was on top" — and a second
    implementation of four-points-a-win and percentage would be two tables that
    disagree the first time a rule changed.

    Returns an ordered list of dicts shaped like LadderEntry's own fields, so a
    template cannot tell whether it was handed stored rows or computed ones.

    `up_to_round=None` is the whole season, which is what rebuild_ladder writes
    to LadderEntry. A number means "as at the end of that round", counting every
    completed regular-season game up to and including it.

    Returns [] where there is no ladder for the code (see NO_LADDER) or no rules
    for it, which is the same answer rebuild_ladder gives by writing nothing.
    """
    from matchreader.models import HistoricalMatch

    name = (series.name or "").strip().upper()
    if name in NO_LADDER:
        return []
    rules = RULES.get(name)
    if rules is None:
        return []

    matches = HistoricalMatch.objects.filter(
        series=series,
        season=season.year if hasattr(season, "year") else season,
        stage=HistoricalMatch.STAGE_REGULAR,
    ).only("round_number", "home_team_id", "away_team_id", "home_score", "away_score")
    if up_to_round is not None:
        matches = matches.filter(round_number__lte=up_to_round)

    rows: dict[int, _Row] = {}
    played_in_round: dict[int, set[int]] = {}

    def row_for(team_id: int) -> _Row:
        if team_id not in rows:
            rows[team_id] = _Row(team_id=team_id)
        return rows[team_id]

    for match in matches:
        home, away = row_for(match.home_team_id), row_for(match.away_team_id)
        played_in_round.setdefault(match.round_number, set()).update(
            (home.team_id, away.team_id)
        )
        home.played += 1
        away.played += 1
        home.points_for += match.home_score
        home.points_against += match.away_score
        away.points_for += match.away_score
        away.points_against += match.home_score
        if match.home_score > match.away_score:
            home.wins += 1
            away.losses += 1
        elif match.away_score > match.home_score:
            away.wins += 1
            home.losses += 1
        else:
            home.draws += 1
            away.draws += 1

    if not rows:
        return []

    if rules.bye:
        _apply_byes(rows, played_in_round)
    _apply_adjustments(rows, series=series, season=season)

    ordered = sorted(
        rows.values(),
        key=lambda r: (
            -r.points(rules),
            -(r.percentage if rules.separator == "percentage" else r.differential),
            -r.points_for,
        ),
    )
    return [
        {
            "rank": rank,
            "team_id": r.team_id,
            "played": r.played,
            "wins": r.wins,
            "losses": r.losses,
            "draws": r.draws,
            "byes": r.byes,
            "points": r.points(rules),
            "percentage": round(r.percentage, 2),
            "points_for": r.points_for,
            "points_against": r.points_against,
        }
        for rank, r in enumerate(ordered, start=1)
    ]


def season_rounds(*, series, season) -> list[int]:
    """Round numbers with a completed regular-season game, oldest first.

    What the ladder's round stepper can offer. Read from the same table the
    standings come from, so it can never list a round the ladder cannot draw.
    """
    from matchreader.models import HistoricalMatch

    return sorted(
        HistoricalMatch.objects.filter(
            series=series,
            season=season.year if hasattr(season, "year") else season,
            stage=HistoricalMatch.STAGE_REGULAR,
        )
        .values_list("round_number", flat=True)
        .distinct()
    )


def rebuild_ladder(*, series, season) -> int:
    """Recompute one series' ladder for one season. Returns rows written.

    Reads ``matchreader.HistoricalMatch``, NOT ``tipping.Match``. That choice
    is the difference between a working ladder and a broken one:

      * tipping.Match is org-scoped and partial. It holds only the rounds
        leagues are actively tipping — a live database had AFL 2026 rounds 1-4
        and 21-22 and nothing between, so a table derived from it showed
        Fremantle on 6 games played instead of 21. It also stores one row per
        league per fixture, so a naive tally multiplies every result by the
        number of leagues tipping it.
      * HistoricalMatch is one row per real game, whole seasons, no
        organisation anywhere in it. It exists to train MatchReader, and it is
        exactly the shape a ladder needs.

    Wholesale replacement rather than incremental update: the table is cheap to
    compute and an incremental path would need to know which result changed,
    which is exactly the kind of state that drifts. A corrected score therefore
    fixes the ladder on the next rebuild with no special handling.
    """
    from django.db import transaction
    from matchreader.models import HistoricalMatch
    from tipping.models import LadderEntry

    name = (series.name or "").strip().upper()
    if name in NO_LADDER:
        logger.debug("No ladder for %s; nothing to rebuild.", series.name)
        return 0
    rules = RULES.get(name)
    if rules is None:
        logger.warning("No ladder rules for series %r; skipping rebuild.", series.name)
        return 0

    # Regular season only. HistoricalMatch stores completed games exclusively,
    # so there is no status to filter on — a row's existence is the result.
    matches = HistoricalMatch.objects.filter(
        series=series,
        season=season.year,
        stage=HistoricalMatch.STAGE_REGULAR,
    ).only("round_number", "home_team_id", "away_team_id", "home_score", "away_score")

    rows: dict[int, _Row] = {}
    # round_number -> teams that had a fixture in it, for bye inference below.
    played_in_round: dict[int, set[int]] = {}

    def row_for(team_id: int) -> _Row:
        if team_id not in rows:
            rows[team_id] = _Row(team_id=team_id)
        return rows[team_id]

    for match in matches:
        home, away = row_for(match.home_team_id), row_for(match.away_team_id)
        played_in_round.setdefault(match.round_number, set()).update(
            (home.team_id, away.team_id)
        )

        home.played += 1
        away.played += 1
        home.points_for += match.home_score
        home.points_against += match.away_score
        away.points_for += match.away_score
        away.points_against += match.home_score

        if match.home_score > match.away_score:
            home.wins += 1
            away.losses += 1
        elif match.away_score > match.home_score:
            away.wins += 1
            home.losses += 1
        else:
            home.draws += 1
            away.draws += 1

    if not rows:
        # No results means no table — CLEAR it, do not leave it standing.
        #
        # Leaving it was the old behaviour, inherited from when ladders were
        # fetched and an empty read might be a transient feed failure worth
        # riding out. Nothing is transient now: this reads our own database,
        # so "no completed matches" is a fact about the season, not a blip.
        #
        # What it left behind was worse than nothing. AFL 2027 kept eighteen
        # all-zero rows from the decommissioned Squiggle sync, ranked
        # alphabetically — so a 2027 league opened the ladder to find Adelaide
        # first, Brisbane second, and the top eight marked as finals places,
        # for a season that has not started. The empty state on the ladder page
        # says "standings arrive with the results feed", which is true and is
        # what should be shown.
        deleted, _ = LadderEntry.objects.filter(series=series, season=season).delete()
        if deleted:
            logger.info(
                "No completed %s matches in %s; cleared %s stale ladder row(s).",
                series.name, season, deleted,
            )
        return 0

    if rules.bye:
        _apply_byes(rows, played_in_round)

    _apply_adjustments(rows, series=series, season=season)

    ordered = sorted(
        rows.values(),
        key=lambda r: (
            -r.points(rules),
            -(r.percentage if rules.separator == "percentage" else r.differential),
            -r.points_for,
        ),
    )

    with transaction.atomic():
        written = 0
        for rank, r in enumerate(ordered, start=1):
            LadderEntry.objects.update_or_create(
                series=series, season=season, team_id=r.team_id,
                defaults={
                    "rank": rank,
                    "played": r.played,
                    "wins": r.wins,
                    "losses": r.losses,
                    "draws": r.draws,
                    "byes": r.byes,
                    "points": r.points(rules),
                    "percentage": round(r.percentage, 2),
                    "points_for": r.points_for,
                    "points_against": r.points_against,
                },
            )
            written += 1
        # A team that dropped out of the competition (relocation, expansion
        # season) would otherwise keep a stale row and a stale rank forever.
        LadderEntry.objects.filter(series=series, season=season).exclude(
            team_id__in=list(rows)
        ).delete()

    return written


def _apply_byes(rows: dict[int, _Row], played_in_round: dict[int, set[int]]) -> None:
    """Infer byes from absence, for the codes that pay for them (NRL: 2 points).

    A bye is the one thing a results table cannot state directly — no fixture
    is published for a team that is not playing, so there is nothing to store.
    It has to be read from the gap: in a FINISHED round, a competing team with
    no game had a bye. There is no other way for that to happen.

    "Finished" is the delicate part, because HistoricalMatch holds only
    completed games and so cannot distinguish "this round is over" from "this
    round is half-played". The test used is that a LATER round has results:
    rounds are played in order, so a game in round 15 means round 14 is done.
    That leaves the newest round in the table never contributing byes, which is
    the right way to be wrong — it under-awards by at most one round for one
    week, and corrects itself the moment the next round is played. Awarding
    early would put a team above a rival on points it had not earned yet.

    Only rounds at or after a team's first appearance count, so an expansion
    club joining mid-season is not credited for rounds before it existed.
    """
    if not played_in_round:
        return

    # Every round except the most recent one, which may still be in progress.
    latest = max(played_in_round)
    settled = [rn for rn in played_in_round if rn < latest]

    first_seen = {
        team_id: min(rn for rn, teams in played_in_round.items() if team_id in teams)
        for team_id in rows
    }

    for rn in settled:
        playing = played_in_round.get(rn, set())
        for team_id, row in rows.items():
            if team_id not in playing and rn >= first_seen[team_id]:
                row.byes += 1


def _apply_adjustments(rows: dict[int, _Row], *, series, season) -> None:
    """Fold in any administrator-entered points adjustments.

    Kept out of the tally loop because an adjustment is not a result: it has no
    scores, no fixture and no date that matters, and it must survive a rebuild
    that recomputes everything else from scratch.
    """
    from tipping.models import LadderAdjustment

    for adj in LadderAdjustment.objects.filter(series=series, season=season):
        row = rows.get(adj.team_id)
        if row is None:
            logger.warning(
                "Ladder adjustment for team %s is not in the %s %s table; ignored.",
                adj.team_id, series.name, season,
            )
            continue
        row.adjustment += adj.points


def rebuild_for_competition(competition: str, *, season) -> int:
    """Rebuild every ladder a competition covers. Returns total rows written.

    The sync layer is keyed by COMPETITION ("AFL", "NRL") while ladders are per
    SERIES, and one competition bundles several — AFL covers AFLW, NRL covers
    NRLW. This is the seam between the two.
    """
    from catalog.models import Series

    comp = (competition or "").strip().upper()
    wanted = [name for name in RULES if name == comp or name.startswith(comp)]
    if not wanted:
        return 0

    total = 0
    for name in wanted:
        series = Series.objects.filter(name__iexact=name).first()
        if series is None:
            continue
        total += rebuild_ladder(series=series, season=season)
    return total
