from datetime import timedelta
from unittest.mock import MagicMock

from django.test import TestCase
from django.utils import timezone

from catalog.models import Competition, Season, Series, Sport
from data_sync.models import SyncRun
from data_sync.services import (
    SquiggleSyncService,
    _normalise_team_name,
    _resolve_match,
    _split_period_clock,
)
from orgs.models import Organisation
from tipping.models import Match, Round, Team


class ClockParsingTests(TestCase):
    """Squiggle words the clock differently mid-quarter and at the breaks."""

    def test_period_and_clock_split(self):
        self.assertEqual(_split_period_clock("Q3 12:45"), ("Q3", "12:45"))
        self.assertEqual(_split_period_clock("2nd Half 8:12"), ("2nd Half", "8:12"))

    def test_boundary_phrases_are_period_only(self):
        self.assertEqual(_split_period_clock("Half Time"), ("Half Time", ""))
        self.assertEqual(_split_period_clock("Full Time"), ("Full Time", ""))
        self.assertEqual(_split_period_clock("Q2"), ("Q2", ""))

    def test_bare_clock_and_empty(self):
        self.assertEqual(_split_period_clock("66:00"), ("", "66:00"))
        self.assertEqual(_split_period_clock(""), ("", ""))
        self.assertEqual(_split_period_clock("   "), ("", ""))


class TeamAliasTests(TestCase):
    """Squiggle names some clubs by city; the Team table stores the full name."""

    def test_city_names_map_to_club_slugs(self):
        self.assertEqual(_normalise_team_name("Adelaide"), "adelaide-crows")
        self.assertEqual(_normalise_team_name("Sydney"), "sydney-swans")
        self.assertEqual(_normalise_team_name("Gold Coast"), "gold-coast-suns")
        self.assertEqual(_normalise_team_name("West Coast"), "west-coast-eagles")
        self.assertEqual(_normalise_team_name("Geelong"), "geelong-cats")

    def test_already_full_names_pass_through(self):
        self.assertEqual(_normalise_team_name("Carlton"), "carlton")
        self.assertEqual(_normalise_team_name("Brisbane Lions"), "brisbane-lions")


class _FixtureBase(TestCase):
    """The Squiggle service looks the AFL series up by name, so these tests reuse
    whatever the seed migrations already created rather than making a second one
    (Sport.name and Series.name are both unique)."""

    def setUp(self):
        self.sport, _ = Sport.objects.get_or_create(
            name="Australian Rules", defaults={"slug": "australian-rules"},
        )
        self.series, _ = Series.objects.get_or_create(
            name="AFL", defaults={"sport": self.sport, "slug": "afl"},
        )
        self.season = Season.objects.create(year=2099, label="2099")
        self.comp = Competition.objects.create(
            sport=self.series.sport, season=self.season, name="AFL", slug="afl",
        )
        self.comp.series.add(self.series)
        self.org = Organisation.objects.create(name="Sync League", season=self.season)
        self.home, _ = Team.objects.get_or_create(
            slug="carlton", series=self.series, defaults={"name": "Carlton"},
        )
        self.away, _ = Team.objects.get_or_create(
            slug="richmond", series=self.series, defaults={"name": "Richmond"},
        )
        self.round = Round.objects.create(
            org=self.org, round_number=1, series=self.series, competition=self.comp,
            lockout_at=timezone.now() + timedelta(days=1),
        )


class ResolveMatchTests(_FixtureBase):
    """Locally seeded fixtures carry no external_id — the feed must still find
    them, or every fixtures sync would duplicate the whole round."""

    def test_matches_seeded_fixture_by_teams_and_backfills_id(self):
        seeded = Match.objects.create(
            round=self.round, home_team=self.home, away_team=self.away,
            kickoff_at=timezone.now(), external_id="",
        )
        found = _resolve_match(self.round, "38499", self.home, self.away)
        self.assertEqual(found, seeded)
        seeded.refresh_from_db()
        self.assertEqual(seeded.external_id, "38499")

    def test_prefers_external_id_once_set(self):
        m = Match.objects.create(
            round=self.round, home_team=self.home, away_team=self.away,
            kickoff_at=timezone.now(), external_id="38499",
        )
        self.assertEqual(_resolve_match(self.round, "38499", self.home, self.away), m)

    def test_returns_none_when_nothing_matches(self):
        self.assertIsNone(_resolve_match(self.round, "1", self.home, self.away))


class LiveSyncTests(_FixtureBase):
    GAME = {
        "id": 38499, "hteam": "Carlton", "ateam": "Richmond",
        "hteamid": 3, "ateamid": 14,
        "date": "2099-03-12 19:30:00", "venue": "M.C.G.",
    }

    def _svc_with(self, **game_overrides):
        svc = SquiggleSyncService()
        svc._games = MagicMock(return_value=[{**self.GAME, **game_overrides}])
        return svc

    def _match(self, hours_ago):
        return Match.objects.create(
            round=self.round, home_team=self.home, away_team=self.away,
            kickoff_at=timezone.now() - timedelta(hours=hours_ago),
        )

    def test_in_play_sets_status_period_clock_and_score(self):
        self._match(1)
        svc = self._svc_with(complete=62, timestr="Q3 12:45", hscore=54, ascore=41)
        n = svc.sync_live(competition="AFL", round_number=1, org=self.org)

        self.assertEqual(n, 1)
        m = Match.objects.get()
        self.assertEqual(m.status, Match.STATUS_LIVE)
        self.assertEqual((m.period, m.clock), ("Q3", "12:45"))
        self.assertEqual(m.progress, 62)
        self.assertEqual(m.score_line, "54–41")
        self.assertEqual(m.live_label, "Q3 12:45")
        self.assertEqual(m.phase, "live")
        self.assertIsNotNone(m.live_updated_at)

    def test_live_sync_does_not_grade_tips(self):
        """Squiggle revises scores in play, so grading here would award then
        retract points and flag recaps for review on every poll."""
        self._match(1)
        svc = self._svc_with(complete=62, timestr="Q3 12:45", hscore=54, ascore=41)
        svc.sync_live(competition="AFL", round_number=1, org=self.org)
        self.assertIsNone(Match.objects.get().result)

    def test_complete_percentage_marks_match_complete(self):
        self._match(4)
        svc = self._svc_with(complete=100, timestr="Full Time", hscore=75, ascore=71)
        svc.sync_live(competition="AFL", round_number=1, org=self.org)
        m = Match.objects.get()
        self.assertEqual(m.status, Match.STATUS_COMPLETE)
        self.assertEqual(m.phase, "complete")

    def test_results_sync_ignores_unfinished_games(self):
        self._match(1)
        svc = self._svc_with(complete=62, timestr="Q3 12:45", hscore=54, ascore=41)
        self.assertEqual(svc.sync_results(competition="AFL", round_number=1, org=self.org), 0)
        self.assertIsNone(Match.objects.get().result)

    def test_results_sync_grades_finished_games(self):
        self._match(4)
        svc = self._svc_with(complete=100, timestr="Full Time", hscore=75, ascore=71)
        self.assertEqual(svc.sync_results(competition="AFL", round_number=1, org=self.org), 1)
        m = Match.objects.get()
        self.assertEqual(m.result, "home")
        self.assertEqual(m.status, Match.STATUS_COMPLETE)

    def test_fixtures_sync_is_idempotent(self):
        svc = self._svc_with(complete=0, timestr="")
        svc.sync_fixtures(competition="AFL", round_number=1, org=self.org)
        svc.sync_fixtures(competition="AFL", round_number=1, org=self.org)
        self.assertEqual(Match.objects.filter(round=self.round).count(), 1)


class MatchPhaseTests(_FixtureBase):
    """`phase` has to be right for fixtures the live poller never reached."""

    def _match(self, **kw):
        return Match.objects.create(
            round=self.round, home_team=self.home, away_team=self.away, **kw,
        )

    def test_future_kickoff_is_upcoming(self):
        m = self._match(kickoff_at=timezone.now() + timedelta(hours=3))
        self.assertEqual(m.phase, "upcoming")

    def test_just_started_is_live(self):
        m = self._match(kickoff_at=timezone.now() - timedelta(minutes=30))
        self.assertEqual(m.phase, "live")

    def test_long_past_kickoff_without_result_is_complete_not_live(self):
        m = self._match(kickoff_at=timezone.now() - timedelta(days=20))
        self.assertEqual(m.phase, "complete")

    def test_graded_match_is_complete_regardless_of_status(self):
        m = self._match(
            kickoff_at=timezone.now() - timedelta(minutes=10),
            result="home", home_score=10, away_score=4,
        )
        self.assertEqual(m.phase, "complete")

    def test_postponed_returns_to_upcoming(self):
        m = self._match(
            kickoff_at=timezone.now() - timedelta(hours=2),
            status=Match.STATUS_POSTPONED,
        )
        self.assertEqual(m.phase, "upcoming")

    def test_venue_label_combines_ground_and_city(self):
        m = self._match(kickoff_at=timezone.now(), venue="M.C.G.", venue_city="Melbourne")
        self.assertEqual(m.venue_label, "M.C.G., Melbourne")
        m2 = self._match(kickoff_at=timezone.now(), venue="S.C.G.")
        self.assertEqual(m2.venue_label, "S.C.G.")


class SyncRunTests(TestCase):
    def test_records_success_with_count(self):
        with SyncRun.record(kind=SyncRun.KIND_LIVE, competition="AFL") as run:
            run.matches_touched = 9
        run.refresh_from_db()
        self.assertTrue(run.ok)
        self.assertEqual(run.matches_touched, 9)
        self.assertIsNotNone(run.finished_at)

    def test_records_failure_and_reraises(self):
        with self.assertRaises(ValueError):
            with SyncRun.record(kind=SyncRun.KIND_LIVE, competition="AFL"):
                raise ValueError("feed down")
        run = SyncRun.objects.get()
        self.assertFalse(run.ok)
        self.assertIn("feed down", run.message)

    def test_last_success_ignores_failures(self):
        with self.assertRaises(ValueError):
            with SyncRun.record(kind=SyncRun.KIND_LIVE, competition="AFL"):
                raise ValueError("nope")
        self.assertIsNone(SyncRun.last_success(kind=SyncRun.KIND_LIVE))
        with SyncRun.record(kind=SyncRun.KIND_LIVE, competition="AFL"):
            pass
        self.assertIsNotNone(SyncRun.last_success(kind=SyncRun.KIND_LIVE))
