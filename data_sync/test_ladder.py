"""Tests for the derived ladder.

The ladder used to be somebody else's arithmetic, so getting it wrong was
their problem. It is ours now, and a wrong ladder is the kind of thing a tipper
notices immediately and trusts nothing else after.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone as dt_timezone

from django.test import SimpleTestCase, TestCase

from catalog.models import Season, Series
from data_sync.ladder import rebuild_ladder
from matchreader.models import HistoricalMatch
from matchreader.stages import finals_rounds
from tipping.models import LadderAdjustment, LadderEntry, Team


class FinalsDetectionTests(SimpleTestCase):
    """The round-shape heuristic, away from the database."""

    def test_trailing_short_rounds_are_finals(self):
        sizes = {rn: 9 for rn in range(1, 25)} | {25: 4, 26: 2, 27: 2, 28: 1}
        self.assertEqual(finals_rounds(sizes), {25, 26, 27, 28})

    def test_a_lone_short_last_round_is_the_round_in_progress(self):
        """NRL 2026 was flagged as finals off a single part-played round."""
        sizes = {rn: 8 for rn in range(1, 22)} | {22: 1}
        self.assertEqual(finals_rounds(sizes), set())

    def test_a_thin_round_mid_season_is_not_finals(self):
        """Byes shrink a round without ending the season. The backwards walk
        stops at the first full round, so this is never even reached."""
        sizes = {rn: 8 for rn in range(1, 28)} | {12: 4, 15: 4}
        self.assertEqual(finals_rounds(sizes), set())

    def test_no_games_no_finals(self):
        self.assertEqual(finals_rounds({}), set())


class _LadderBase(TestCase):
    """A season of results to build a table from.

    Uses the catalog the migrations seed rather than creating its own. Sport,
    Series and Season names are all unique, so building a second "Australian
    Rules" / "AFL" / 2026 raised IntegrityError before a single ladder
    assertion ran — thirteen tests that never executed. Reading the real rows
    is also the more honest fixture: these are the ones production computes
    ladders for.
    """

    SERIES = "AFL"

    @classmethod
    def setUpTestData(cls):
        cls.series = Series.objects.get(name=cls.SERIES)
        cls.season = Season.objects.get(year=2026)
        cls.teams = [
            Team.objects.create(name=f"Team {i}", slug=f"team-{i}", series=cls.series)
            for i in range(4)
        ]

    def game(self, rn, home, away, hs, as_, *, stage="regular"):
        return HistoricalMatch.objects.create(
            series=self.series,
            season=self.season.year,
            round_number=rn,
            stage=stage,
            external_id=f"t:{rn}:{home}:{away}",
            home_team=self.teams[home],
            away_team=self.teams[away],
            kickoff_at=datetime(2026, 3, 1, tzinfo=dt_timezone.utc) + timedelta(days=7 * rn),
            home_score=hs,
            away_score=as_,
        )

    def table(self):
        return list(
            LadderEntry.objects.filter(series=self.series, season=self.season)
            .select_related("team").order_by("rank")
        )


class AflLadderTests(_LadderBase):
    SERIES = "AFL"

    def test_four_points_a_win_two_a_draw(self):
        self.game(1, 0, 1, 100, 50)     # team 0 wins
        self.game(1, 2, 3, 60, 60)      # draw
        rebuild_ladder(series=self.series, season=self.season)

        by_team = {e.team.name: e for e in self.table()}
        self.assertEqual(by_team["Team 0"].points, 4)
        self.assertEqual(by_team["Team 1"].points, 0)
        self.assertEqual(by_team["Team 2"].points, 2)
        self.assertEqual(by_team["Team 3"].points, 2)

    def test_percentage_separates_teams_level_on_points(self):
        """Both win once; the bigger winning margin ranks above."""
        self.game(1, 0, 1, 100, 50)     # team 0: 200%
        self.game(1, 2, 3, 60, 50)      # team 2: 120%
        rebuild_ladder(series=self.series, season=self.season)

        table = self.table()
        self.assertEqual(table[0].team.name, "Team 0")
        self.assertEqual(table[1].team.name, "Team 2")
        self.assertAlmostEqual(table[0].percentage, 200.0, places=1)

    def test_finals_do_not_count(self):
        self.game(1, 0, 1, 100, 50)
        self.game(2, 1, 0, 100, 50, stage="finals")
        rebuild_ladder(series=self.series, season=self.season)

        by_team = {e.team.name: e for e in self.table()}
        self.assertEqual(by_team["Team 0"].played, 1)
        self.assertEqual(by_team["Team 0"].points, 4)
        self.assertEqual(by_team["Team 1"].points, 0)

    def test_afl_pays_nothing_for_a_bye(self):
        self.game(1, 0, 1, 100, 50)
        self.game(2, 0, 1, 100, 50)
        self.game(3, 2, 3, 60, 50)      # 0 and 1 have a bye in a settled round
        rebuild_ladder(series=self.series, season=self.season)

        by_team = {e.team.name: e for e in self.table()}
        self.assertEqual(by_team["Team 0"].points, 8)   # two wins, nothing else

    def test_rebuild_is_idempotent_and_reflects_corrections(self):
        g = self.game(1, 0, 1, 100, 50)
        rebuild_ladder(series=self.series, season=self.season)
        self.assertEqual(self.table()[0].team.name, "Team 0")

        # The feed revises the score the other way.
        g.home_score, g.away_score = 50, 100
        g.save()
        rebuild_ladder(series=self.series, season=self.season)

        table = self.table()
        self.assertEqual(len(table), 2)                  # not doubled
        self.assertEqual(table[0].team.name, "Team 1")   # and corrected

    def test_adjustment_is_applied(self):
        self.game(1, 0, 1, 100, 50)
        self.game(2, 0, 1, 100, 50)
        LadderAdjustment.objects.create(
            series=self.series, season=self.season, team=self.teams[0],
            points=-8, reason="Salary cap breach",
        )
        rebuild_ladder(series=self.series, season=self.season)

        by_team = {e.team.name: e for e in self.table()}
        self.assertEqual(by_team["Team 0"].points, 0)    # 8 earned, 8 deducted


class NrlLadderTests(_LadderBase):
    SERIES = "NRL"

    def test_two_points_a_win_one_a_draw(self):
        self.game(1, 0, 1, 20, 10)
        self.game(1, 2, 3, 12, 12)
        rebuild_ladder(series=self.series, season=self.season)

        by_team = {e.team.name: e for e in self.table()}
        self.assertEqual(by_team["Team 0"].points, 2)
        self.assertEqual(by_team["Team 2"].points, 1)

    def test_a_bye_is_worth_two_points(self):
        """Teams 0 and 1 sit out round 2, which is settled because round 3 has
        been played."""
        self.game(1, 0, 1, 20, 10)
        self.game(2, 2, 3, 20, 10)
        self.game(3, 0, 1, 20, 10)
        rebuild_ladder(series=self.series, season=self.season)

        by_team = {e.team.name: e for e in self.table()}
        self.assertEqual(by_team["Team 0"].byes, 1)
        self.assertEqual(by_team["Team 0"].points, 6)    # two wins + one bye
        self.assertEqual(by_team["Team 1"].byes, 1)
        self.assertEqual(by_team["Team 1"].points, 2)    # two losses + one bye

    def test_the_newest_round_awards_no_byes(self):
        """Round 2 is still being played — one game in, the rest to come. Teams
        0 and 1 have not had a bye, they are waiting for Sunday."""
        self.game(1, 0, 1, 20, 10)
        self.game(2, 2, 3, 20, 10)
        rebuild_ladder(series=self.series, season=self.season)

        by_team = {e.team.name: e for e in self.table()}
        self.assertEqual(by_team["Team 0"].byes, 0)
        self.assertEqual(by_team["Team 1"].byes, 0)

    def test_ranked_on_differential_not_percentage(self):
        """Level on points. Team 0's +10 beats team 2's +2, though team 2 has
        the better ratio (12/10 = 120% against 20/10 = 200%)... which is the
        point: the AFL would rank these the other way round."""
        self.game(1, 0, 1, 20, 10)      # +10, 200%
        self.game(1, 2, 3, 12, 10)      # +2,  120%
        rebuild_ladder(series=self.series, season=self.season)

        self.assertEqual(self.table()[0].team.name, "Team 0")


class LadderSourceTests(_LadderBase):
    """Where the numbers come from, which is the part that was wrong first."""

    SERIES = "AFL"

    def test_org_scoped_matches_are_not_counted(self):
        """The ladder reads HistoricalMatch, never tipping.Match.

        tipping.Match holds one row per league per fixture, so twenty leagues
        tipping one game would report twenty games played. It is also partial —
        only the rounds leagues are actively tipping — which is what showed
        Fremantle on 6 games instead of 21.
        """
        from orgs.models import Organisation
        from tipping.models import Match, Round

        self.game(1, 0, 1, 100, 50)
        rebuild_ladder(series=self.series, season=self.season)
        before = {e.team.name: e.played for e in self.table()}

        # Whatever exists in the org-scoped table must not move the ladder.
        self.assertEqual(before["Team 0"], 1)
        self.assertFalse(
            Match.objects.exists(),
            "test asserts the ladder ignores tipping.Match; none was created",
        )
        self.assertEqual(Organisation.objects.count(), 0)
        self.assertEqual(Round.objects.count(), 0)

    def test_state_of_origin_has_no_ladder(self):
        origin = Series.objects.get(name="State of Origin")
        self.assertEqual(rebuild_ladder(series=origin, season=self.season), 0)
        self.assertFalse(LadderEntry.objects.filter(series=origin).exists())

    def test_a_series_with_no_rules_is_skipped_not_crashed(self):
        """Super Netball is in the catalog with no feed and no ladder rules.

        Six leagues are signed up to it, so this path is reached in production
        rather than being hypothetical: it has to be a quiet skip, not a crash
        that takes the AFL and NRL ladders down with it.
        """
        netball = Series.objects.get(name="Super Netball")
        self.assertEqual(rebuild_ladder(series=netball, season=self.season), 0)


class EmptySeasonTests(_LadderBase):
    """A season with no results must show no table, not an empty one."""

    def test_a_season_with_no_results_clears_stale_rows(self):
        """AFL 2027 held eighteen all-zero rows from the removed Squiggle sync.

        Ranked alphabetically, so a 2027 league opened the ladder to Adelaide
        first and the top eight flagged as finals places, for a season that had
        not started. An empty table is not a neutral thing to leave lying
        around — it reads as a standing.
        """
        LadderEntry.objects.create(
            series=self.series, season=self.season, team=self.teams[0],
            rank=1, played=0, wins=0, losses=0, draws=0, points=0, percentage=0,
        )
        self.assertEqual(rebuild_ladder(series=self.series, season=self.season), 0)
        self.assertFalse(LadderEntry.objects.filter(series=self.series, season=self.season).exists())

    def test_a_real_table_is_not_cleared(self):
        self.game(1, 0, 1, 100, 50)
        rebuild_ladder(series=self.series, season=self.season)
        self.assertEqual(len(self.table()), 2)
        # Second pass over the same results leaves it intact.
        rebuild_ladder(series=self.series, season=self.season)
        self.assertEqual(len(self.table()), 2)
