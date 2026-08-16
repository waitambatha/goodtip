from datetime import timedelta
from unittest.mock import MagicMock

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from catalog.models import Competition, Season, Series, Sport
from data_sync.models import SyncRun
from data_sync.services import (
    AflScrapeSyncService,
    NrlScrapeSyncService,
    SyncError,
    competition_for_series,
    get_sync_service,
    _normalise_team_name,
    _resolve_match,
)
from orgs.models import Organisation
from tipping.models import Match, Round, Team


class TeamAliasTests(TestCase):
    """afl.com.au names some clubs by city; the Team table stores the full name."""

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
    """The sync services look the AFL series up by name, so these tests reuse
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
    """The AFL sync, driven by a stubbed scraper.

    These used to run against SquiggleSyncService, which no longer exists —
    every third-party feed was removed on 14 Aug 2026. The scraper is stubbed
    at ``_scraper.fixtures``, the one seam where the network would be, so the
    sync logic is exercised without a request to afl.com.au.
    """

    # The shape AflApiScraper.fixtures() returns, per data_sync/scrapers/afl.py.
    GAME = {
        "external_id": "CD_M20990140101",
        "home_name": "Carlton", "away_name": "Richmond",
        "home_external_id": "CD_T100", "away_external_id": "CD_T140",
        "home_score": None, "away_score": None,
        "venue": "M.C.G.", "venue_city": "Melbourne",
        "clock": "", "period": "",
    }

    def _svc_with(self, **overrides):
        svc = AflScrapeSyncService()
        row = {**self.GAME, "kickoff_at": timezone.now(), **overrides}
        # Only the AFL series returns rows; AFLW returns none, so the service's
        # loop over both series does not double-count.
        svc._scraper.fixtures = MagicMock(
            side_effect=lambda *, series, season, round_number: (
                [row] if series == "AFL" else []
            )
        )
        return svc

    def _match(self, hours_ago):
        return Match.objects.create(
            round=self.round, home_team=self.home, away_team=self.away,
            kickoff_at=timezone.now() - timedelta(hours=hours_ago),
        )

    def test_in_play_sets_status_period_and_score(self):
        self._match(1)
        svc = self._svc_with(status="live", period="Q3", home_score=54, away_score=41)
        n = svc.sync_live(competition="AFL", round_number=1, org=self.org)

        self.assertEqual(n, 1)
        m = Match.objects.get()
        self.assertEqual(m.status, Match.STATUS_LIVE)
        self.assertEqual(m.period, "Q3")
        self.assertEqual(m.score_line, "54–41")
        self.assertEqual(m.phase, "live")
        self.assertIsNotNone(m.live_updated_at)

    def test_live_sync_does_not_grade_tips(self):
        """A mid-game score is provisional, so grading here would award then
        retract points and flag recaps for review on every poll."""
        self._match(1)
        svc = self._svc_with(status="live", period="Q3", home_score=54, away_score=41)
        svc.sync_live(competition="AFL", round_number=1, org=self.org)
        self.assertIsNone(Match.objects.get().result)

    def test_scheduled_games_are_skipped_by_the_live_poller(self):
        self._match(-1)
        svc = self._svc_with(status="scheduled")
        self.assertEqual(svc.sync_live(competition="AFL", round_number=1, org=self.org), 0)

    def test_complete_status_marks_match_complete(self):
        self._match(4)
        svc = self._svc_with(status="complete", period="Full Time", home_score=75, away_score=71)
        svc.sync_live(competition="AFL", round_number=1, org=self.org)
        m = Match.objects.get()
        self.assertEqual(m.status, Match.STATUS_COMPLETE)
        self.assertEqual(m.phase, "complete")

    def test_results_sync_ignores_unfinished_games(self):
        self._match(1)
        svc = self._svc_with(status="live", period="Q3", home_score=54, away_score=41)
        self.assertEqual(svc.sync_results(competition="AFL", round_number=1, org=self.org), 0)
        self.assertIsNone(Match.objects.get().result)

    def test_results_sync_grades_finished_games(self):
        self._match(4)
        svc = self._svc_with(status="complete", period="Full Time", home_score=75, away_score=71)
        self.assertEqual(svc.sync_results(competition="AFL", round_number=1, org=self.org), 1)
        m = Match.objects.get()
        self.assertEqual(m.result, "home")
        self.assertEqual(m.status, Match.STATUS_COMPLETE)

    def test_fixtures_sync_is_idempotent(self):
        svc = self._svc_with(status="scheduled")
        svc.sync_fixtures(competition="AFL", round_number=1, org=self.org)
        svc.sync_fixtures(competition="AFL", round_number=1, org=self.org)
        self.assertEqual(Match.objects.filter(round=self.round).count(), 1)

    def test_only_the_afl_competition_is_accepted(self):
        svc = self._svc_with(status="scheduled")
        with self.assertRaises(SyncError):
            svc.sync_live(competition="NRL", round_number=1, org=self.org)


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


class CompetitionForSeriesTests(SimpleTestCase):
    """Series names are not competition names, and the sync dispatches on the
    latter.

    Every sync service takes a COMPETITION and loops the SERIES that
    competition bundles. The two names happen to be identical for the men's
    series and for nothing else, so code that upper-cased a Round's series and
    handed it to get_sync_service worked on AFL and NRL while every AFLW,
    NRLW and Origin round failed with "Unsupported competition: AFLW".
    """

    def test_womens_and_representative_series_resolve_to_their_competition(self):
        self.assertEqual(competition_for_series("AFL"), "AFL")
        self.assertEqual(competition_for_series("AFLW"), "AFL")
        self.assertEqual(competition_for_series("NRL"), "NRL")
        self.assertEqual(competition_for_series("NRLW"), "NRL")
        self.assertEqual(competition_for_series("State of Origin"), "NRL")

    def test_case_and_padding_do_not_matter(self):
        self.assertEqual(competition_for_series("  aflw "), "AFL")
        self.assertEqual(competition_for_series("STATE OF ORIGIN"), "NRL")

    def test_a_series_with_no_feed_returns_none_rather_than_raising(self):
        """Skippable, not an error. Super Netball is in the catalog with no
        feed behind it, and one unfed series must not stop the codes that do
        have one."""
        self.assertIsNone(competition_for_series("Super Netball"))
        self.assertIsNone(competition_for_series("Super League"))
        self.assertIsNone(competition_for_series(""))
        self.assertIsNone(competition_for_series(None))

    def test_every_resolved_key_is_one_get_sync_service_accepts(self):
        for series in ("AFL", "AFLW", "NRL", "NRLW", "State of Origin"):
            key = competition_for_series(series)
            # Constructing the service is what used to raise. Any exception
            # here means the resolver is handing over a name no feed answers to.
            self.assertIsNotNone(get_sync_service(key))

    def test_the_resolver_tracks_the_services_own_series_lists(self):
        """Derived, not a second hardcoded table — so adding a series to a
        feed cannot leave this mapping behind."""
        for name in AflScrapeSyncService.SERIES:
            self.assertEqual(competition_for_series(name), "AFL")
        for name in NrlScrapeSyncService.SERIES:
            self.assertEqual(competition_for_series(name), "NRL")


class RoundStateTests(_FixtureBase):
    """Round.status, which nothing in this project ever maintained.

    On the live database 354 of 356 rounds sat on the "upcoming" default,
    including seasons that had finished months earlier. That was not cosmetic:
    the sync command targeted rounds with ``status__in=("open", "locked")``, so
    across thirty-three leagues exactly one stale round was ever polled.
    """

    def _match(self, hours_ago, status=Match.STATUS_SCHEDULED, **kw):
        return Match.objects.create(
            round=self.round, kickoff_at=timezone.now() - timedelta(hours=hours_ago),
            home_team=kw.pop("home", self.home), away_team=kw.pop("away", self.away),
            status=status, **kw,
        )

    def test_nothing_started_is_upcoming(self):
        from data_sync.services import refresh_round_state
        self._match(-5)
        refresh_round_state(self.round)
        self.round.refresh_from_db()
        self.assertEqual(self.round.status, "upcoming")

    def test_first_kickoff_locks_the_round(self):
        from data_sync.services import refresh_round_state
        self._match(1)
        refresh_round_state(self.round)
        self.round.refresh_from_db()
        self.assertEqual(self.round.status, "locked")

    def test_every_fixture_graded_completes_the_round(self):
        from data_sync.services import refresh_round_state
        self._match(4, status=Match.STATUS_COMPLETE)
        refresh_round_state(self.round)
        self.round.refresh_from_db()
        self.assertEqual(self.round.status, "complete")

    def test_one_game_left_is_not_complete(self):
        """A Saturday result must not close a round with a Sunday game in it."""
        from data_sync.services import refresh_round_state
        other = Team.objects.create(slug="essendon", series=self.series, name="Essendon")
        self._match(4, status=Match.STATUS_COMPLETE)
        self._match(-2, home=other, away=self.home)
        refresh_round_state(self.round)
        self.round.refresh_from_db()
        self.assertEqual(self.round.status, "locked")

    def test_a_round_with_no_fixtures_is_left_alone(self):
        from data_sync.services import refresh_round_state
        self.assertFalse(refresh_round_state(self.round))


class ResultsFeedHistoryTests(_FixtureBase):
    """Results must land in HistoricalMatch, which is what the ladder reads.

    This was THE broken link. ``rebuild_ladder`` reads HistoricalMatch and the
    only writer was a manual backfill command, so every ladder in the app was
    frozen at whenever somebody last remembered to run it — while results
    landed correctly in tipping.Match right beside it.
    """

    def _played(self):
        Match.objects.create(
            round=self.round, home_team=self.home, away_team=self.away,
            kickoff_at=timezone.now() - timedelta(hours=4),
        )
        return self._svc_with(
            status="complete", period="Full Time", home_score=75, away_score=71,
        )

    # The stubbed-scraper helpers, borrowed rather than copied so the fake feed
    # row stays defined in exactly one place.
    _svc_with = LiveSyncTests._svc_with
    GAME = LiveSyncTests.GAME

    def test_a_finished_game_is_written_to_history(self):
        from matchreader.models import HistoricalMatch
        self._played().sync_results(competition="AFL", round_number=1, org=self.org)

        game = HistoricalMatch.objects.get()
        self.assertEqual(game.series, self.series)
        self.assertEqual(game.season, self.season.year)
        self.assertEqual(game.round_number, 1)
        self.assertEqual((game.home_score, game.away_score), (75, 71))

    def test_history_is_written_once_however_many_leagues_tip_it(self):
        """HistoricalMatch is not org-scoped: one row per real game, whatever
        the number of leagues holding the fixture."""
        from matchreader.models import HistoricalMatch
        svc = self._played()
        other = Organisation.objects.create(name="Second League", season=self.season)
        Round.objects.create(
            org=other, round_number=1, series=self.series, competition=self.comp,
            lockout_at=timezone.now(),
        )
        svc.sync_results(competition="AFL", round_number=1, org=self.org)
        svc.sync_results(competition="AFL", round_number=1, org=other)
        self.assertEqual(HistoricalMatch.objects.count(), 1)

    def test_regrading_is_not_repeated_once_a_score_has_settled(self):
        """Results run every fifteen minutes for the rest of the season. A
        settled match must stop being rewritten, or every tip on it is updated
        forever."""
        svc = self._played()
        self.assertEqual(svc.sync_results(competition="AFL", round_number=1, org=self.org), 1)
        self.assertEqual(svc.sync_results(competition="AFL", round_number=1, org=self.org), 0)


class RoundStageTests(_FixtureBase):
    """What a correct tip is WORTH: 1 regular, 2 finals, 4 State of Origin."""

    def test_origin_is_worth_four_points(self):
        """Every Origin round in the live database sat on "regular" and paid 1.

        Origin needs no inference at all — it is a whole series — so this was
        purely nothing ever setting the field.
        """
        from matchreader.stages import stage_for_round
        origin = Series.objects.get(name="State of Origin")
        self.assertEqual(stage_for_round(origin, 2026, 1, 1), Round.STAGE_ORIGIN)
        rnd = Round.objects.create(
            org=self.org, round_number=1, series=origin,
            lockout_at=timezone.now(), stage=Round.STAGE_ORIGIN,
        )
        self.assertEqual(rnd.points_per_correct, 4)

    def test_a_short_trailing_round_is_finals(self):
        from matchreader.models import HistoricalMatch
        from matchreader.stages import stage_for_round
        for rn in range(1, 5):
            for i in range(9):
                t1 = Team.objects.create(slug=f"h{rn}-{i}", series=self.series, name=f"H{rn}{i}")
                t2 = Team.objects.create(slug=f"a{rn}-{i}", series=self.series, name=f"A{rn}{i}")
                HistoricalMatch.objects.create(
                    series=self.series, season=2099, round_number=rn,
                    external_id=f"x{rn}{i}", home_team=t1, away_team=t2,
                    kickoff_at=timezone.now(), home_score=10, away_score=5,
                )
        # Four games after a run of nine-game rounds: finals week one.
        self.assertEqual(stage_for_round(self.series, 2099, 5, 4), Round.STAGE_FINALS)

    def test_a_bye_thinned_round_is_not_finals(self):
        """Six games in a nine-game competition is a bye round, not September.

        The guard is that finals are LAST: a round with full rounds after it
        cannot be one, however small it is.
        """
        from matchreader.models import HistoricalMatch
        from matchreader.stages import stage_for_round
        for rn in range(1, 8):
            for i in range(9):
                t1 = Team.objects.create(slug=f"h{rn}-{i}", series=self.series, name=f"H{rn}{i}")
                t2 = Team.objects.create(slug=f"a{rn}-{i}", series=self.series, name=f"A{rn}{i}")
                HistoricalMatch.objects.create(
                    series=self.series, season=2099, round_number=rn,
                    external_id=f"x{rn}{i}", home_team=t1, away_team=t2,
                    kickoff_at=timezone.now(), home_score=10, away_score=5,
                )
        self.assertEqual(stage_for_round(self.series, 2099, 4, 6), Round.STAGE_REGULAR)

    def test_no_history_yet_stays_regular(self):
        """Under-paying a final is correctable; paying double for a regular
        round silently inflates a leaderboard nobody can reconcile."""
        from matchreader.stages import stage_for_round
        self.assertEqual(stage_for_round(self.series, 2099, 1, 4), Round.STAGE_REGULAR)


class RegradeTests(_FixtureBase):
    """Correcting a round's stage has to repay its tips, or it changes nothing."""

    def test_changing_the_stage_repays_already_graded_tips(self):
        from accounts.models import User
        from tipping.models import Tip
        from tipping.services import record_match_result, regrade_round

        match = Match.objects.create(
            round=self.round, home_team=self.home, away_team=self.away,
            kickoff_at=timezone.now() - timedelta(hours=4),
        )
        user = User.objects.create(username="t@example.com", email="t@example.com")
        Tip.objects.create(user=user, match=match, org=self.org, selection="home")
        record_match_result(match, 80, 60)
        self.assertEqual(Tip.objects.get().points_awarded, 1)

        # The round turns out to be Origin, worth four.
        self.round.stage = Round.STAGE_ORIGIN
        self.round.save(update_fields=["stage"])
        regrade_round(self.round)
        self.assertEqual(Tip.objects.get().points_awarded, 4)


class DiscoveryTargetingTests(_FixtureBase):
    """Discovery has to drive RESULTS too, not just fixtures.

    Targets are chosen before the sync loop runs. A backfill creates a season
    of rounds in its fixtures pass, so choosing the results targets from the
    database would evaluate before any of them existed — every fixture created
    and not one of their results filled in. The bug is invisible in a steady
    state and only bites on the run that matters most, the first one.
    """

    def test_a_discovery_run_syncs_results_for_the_rounds_it_discovers(self):
        from unittest.mock import patch
        from data_sync.management.commands.sync_matches import Command

        cmd = Command()
        opts = {
            "live": False, "results": True, "fixtures": True, "ladder": False,
            "backfill": False, "org": self.org.id, "round": None,
            "all_rounds": False, "discover": True, "full_season": False,
            "verbosity": 0,
        }
        discovered = [(self.org, 7, "AFL")]
        with patch.object(Command, "_discovered_targets", return_value=discovered), \
             patch.object(Command, "_targets", return_value=[]) as from_db, \
             patch("data_sync.management.commands.sync_matches.get_sync_service") as svc:
            svc.return_value.sync_fixtures.return_value = 0
            svc.return_value.sync_results.return_value = 0
            cmd.stdout = cmd.stderr = _Sink()
            cmd.handle(**opts)

        # Round 7 does not exist locally, so a database-driven lookup returns
        # nothing — results must still have been asked for it.
        svc.return_value.sync_results.assert_called_once()
        self.assertEqual(svc.return_value.sync_results.call_args.kwargs["round_number"], 7)
        from_db.assert_not_called()


class _Sink:
    """Swallows command output; the assertions are on the calls, not the text."""

    def write(self, *a, **k):
        pass

    def flush(self):
        pass

    @property
    def style(self):
        return self

    def __getattr__(self, _):
        return lambda *a, **k: ""


class NegativeCacheTests(SimpleTestCase):
    """Missing rounds must be remembered; transient failures must not.

    A full-season sweep asks each feed once per organisation. With 33 leagues
    and a season whose draw runs to round 12, rounds 13-30 are eighteen
    guaranteed 404s per series per org — each paying the scraper's deliberate
    throttle. Remembering them is the difference between a sweep of minutes and
    one of hours.

    The opposite error is worse and is why these are two separate tests: a
    cached RATE-LIMIT would make a real round look empty for the rest of the
    run, silently losing fixtures rather than merely wasting time.
    """

    def _afl(self):
        from unittest.mock import MagicMock
        from data_sync.scrapers.afl import AflApiScraper
        s = AflApiScraper()
        s._session = MagicMock()
        s._last_request = 0.0
        return s

    def _response(self, status):
        from unittest.mock import MagicMock
        r = MagicMock()
        r.status_code = status
        return r

    def test_afl_404_is_fetched_once_then_remembered(self):
        from data_sync.scrapers.afl import AflScrapeError
        s = self._afl()
        s._session.get.return_value = self._response(404)

        with self.assertRaises(AflScrapeError):
            s._get("matchItems/round/CD_R202626430")
        # Second ask is served from cache: no fixtures, and no second request.
        self.assertEqual(s.fixtures(series="AFLW", season=2026, round_number=30), [])
        self.assertEqual(s._session.get.call_count, 1)

    def test_afl_server_error_is_retried_not_remembered(self):
        """A 503 is the feed having a moment, not the round being absent."""
        from data_sync.scrapers.afl import AflScrapeError
        s = self._afl()
        s._session.get.return_value = self._response(503)

        for _ in range(2):
            with self.assertRaises(AflScrapeError):
                s._get("matchItems/round/CD_R202601423")
        self.assertEqual(s._session.get.call_count, 2)

    def test_nrl_error_carries_its_status(self):
        """The 404-vs-transient decision depends on this being populated."""
        from unittest.mock import MagicMock, patch
        import requests
        from data_sync.scrapers.nrl import NrlDrawScraper, NrlScrapeError

        s = NrlDrawScraper()
        s._session = MagicMock()
        s._last_request = 0.0
        response = MagicMock(status_code=404)
        s._session.get.side_effect = requests.HTTPError(response=response)

        with self.assertRaises(NrlScrapeError) as caught:
            s._fetch({"competition": 111, "season": 2027})
        self.assertEqual(caught.exception.status, 404)

    def test_nrl_404_is_remembered_but_a_rate_limit_is_not(self):
        from unittest.mock import MagicMock
        import requests
        from data_sync.scrapers.nrl import NrlDrawScraper, NrlScrapeError

        s = NrlDrawScraper()
        s._session = MagicMock()
        s._last_request = 0.0

        s._session.get.side_effect = requests.HTTPError(response=MagicMock(status_code=404))
        with self.assertRaises(NrlScrapeError):
            s._page(competition_id=111, season=2027, round_number=None)
        self.assertIn((111, 2027, None), s._cache)

        s._session.get.side_effect = requests.HTTPError(response=MagicMock(status_code=429))
        with self.assertRaises(NrlScrapeError):
            s._page(competition_id=111, season=2026, round_number=9)
        self.assertNotIn(
            (111, 2026, 9), s._cache,
            "a rate-limit must be retried, not remembered as an empty round",
        )
