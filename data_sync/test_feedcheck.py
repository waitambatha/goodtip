"""The feed check (client, 13 Oct 2026, Ian Hopkinson: "AFLW still going but
not in the site?... we should... have ways to know if it has done so, and if
not how to correct that"). See data_sync/feedcheck.py.
"""
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from catalog.models import Competition, Season, Series, Sport
from orgs.models import Organisation
from tipping.models import Match, Round, Team

from . import feedcheck


def _row(home, away, *, kickoff, external_id="ext-1", status="complete",
        home_score=20, away_score=10):
    return {
        "external_id": external_id,
        "home_name": home, "away_name": away,
        "home_external_id": "", "away_external_id": "",
        "home_score": home_score, "away_score": away_score,
        "kickoff_at": kickoff, "venue": "", "venue_city": "",
        "status": status, "clock": "", "period": "",
    }


class _FakeSvc:
    """Just enough of a sync service for ``_compare_round`` — no network,
    no scraper, the exact seam feedcheck actually calls through."""

    def __init__(self, teams):
        self.teams = teams  # name -> Team

    def resolve_team(self, series, name, external_id):
        return self.teams.get(name)

    def match_index(self, round_obj):
        matches = list(round_obj.matches.all())
        by_ext = {m.external_id: m for m in matches if m.external_id}
        by_pair = {(m.home_team_id, m.away_team_id): m for m in matches}
        return by_ext, by_pair

    def lookup(self, index, external_id, home, away):
        by_ext, by_pair = index
        if external_id and external_id in by_ext:
            return by_ext[external_id]
        return by_pair.get((home.id, away.id))

    def sync_ladder(self, **kwargs):
        return 0


class _Base(TestCase):
    def setUp(self):
        self.sport, _ = Sport.objects.get_or_create(
            name="Australian Rules", defaults={"slug": "australian-rules"},
        )
        self.series, _ = Series.objects.get_or_create(
            name="AFL", defaults={"sport": self.sport, "slug": "afl"},
        )
        self.season = Season.objects.create(year=2098, label="2098")
        self.comp = Competition.objects.create(
            sport=self.sport, season=self.season, name="AFL", slug="afl-fc",
        )
        self.comp.series.add(self.series)
        self.org = Organisation.objects.create(name="Check League", season=self.season)
        self.org.competitions.add(self.comp)
        self.home, _ = Team.objects.get_or_create(
            slug="carlton", series=self.series, defaults={"name": "Carlton"},
        )
        self.away, _ = Team.objects.get_or_create(
            slug="richmond", series=self.series, defaults={"name": "Richmond"},
        )
        self.svc = _FakeSvc({"Carlton": self.home, "Richmond": self.away})
        self.kickoff = timezone.now() - timedelta(hours=6)


class CompareRoundTests(_Base):
    """Each kind of mismatch _compare_round is meant to catch."""

    def test_a_round_we_do_not_hold_is_missing(self):
        issues, n = feedcheck._compare_round(
            self.svc, "AFL", self.series, 2098, 1,
            [_row("Carlton", "Richmond", kickoff=self.kickoff)],
            [self.org.id], timezone.now(),
        )
        self.assertEqual(n, 1)
        self.assertEqual([i["kind"] for i in issues], ["missing_round"])
        self.assertEqual(issues[0]["orgs"], [self.org.id])

    def test_a_club_name_the_feed_uses_that_we_cannot_place(self):
        issues, n = feedcheck._compare_round(
            self.svc, "AFL", self.series, 2098, 1,
            [_row("Carlton", "Fitzroy", kickoff=self.kickoff)],
            [self.org.id], timezone.now(),
        )
        self.assertEqual(n, 0)
        self.assertEqual(issues[0]["kind"], "unknown_club")
        self.assertIn("Fitzroy", issues[0]["detail"])

    def test_a_game_the_round_is_missing(self):
        Round.objects.create(org=self.org, round_number=1, series=self.series,
                             competition=self.comp, lockout_at=self.kickoff)
        issues, _n = feedcheck._compare_round(
            self.svc, "AFL", self.series, 2098, 1,
            [_row("Carlton", "Richmond", kickoff=self.kickoff)],
            [self.org.id], timezone.now(),
        )
        self.assertEqual([i["kind"] for i in issues], ["missing_match"])

    def test_a_kickoff_the_feed_has_moved(self):
        r = Round.objects.create(org=self.org, round_number=1, series=self.series,
                                 competition=self.comp, lockout_at=self.kickoff)
        Match.objects.create(round=r, home_team=self.home, away_team=self.away,
                             kickoff_at=self.kickoff, external_id="ext-1",
                             status=Match.STATUS_SCHEDULED)
        moved = self.kickoff + timedelta(hours=2)
        issues, _n = feedcheck._compare_round(
            self.svc, "AFL", self.series, 2098, 1,
            [_row("Carlton", "Richmond", kickoff=moved, status="scheduled")],
            [self.org.id], timezone.now(),
        )
        self.assertEqual([i["kind"] for i in issues], ["kickoff_drift"])

    def test_a_finished_game_with_no_result_here(self):
        r = Round.objects.create(org=self.org, round_number=1, series=self.series,
                                 competition=self.comp, lockout_at=self.kickoff)
        Match.objects.create(round=r, home_team=self.home, away_team=self.away,
                             kickoff_at=self.kickoff, external_id="ext-1",
                             status=Match.STATUS_SCHEDULED)
        issues, _n = feedcheck._compare_round(
            self.svc, "AFL", self.series, 2098, 1,
            [_row("Carlton", "Richmond", kickoff=self.kickoff)],
            [self.org.id], timezone.now(),
        )
        self.assertEqual([i["kind"] for i in issues], ["result_missing"])

    def test_a_score_that_disagrees_with_the_feed(self):
        r = Round.objects.create(org=self.org, round_number=1, series=self.series,
                                 competition=self.comp, lockout_at=self.kickoff)
        Match.objects.create(round=r, home_team=self.home, away_team=self.away,
                             kickoff_at=self.kickoff, external_id="ext-1",
                             status=Match.STATUS_COMPLETE, result="home",
                             home_score=99, away_score=10)
        issues, _n = feedcheck._compare_round(
            self.svc, "AFL", self.series, 2098, 1,
            [_row("Carlton", "Richmond", kickoff=self.kickoff, home_score=20, away_score=10)],
            [self.org.id], timezone.now(),
        )
        self.assertEqual([i["kind"] for i in issues], ["score_mismatch"])

    def test_a_finished_game_within_the_grading_grace_period_is_not_flagged(self):
        """Just finished — give the next results poll a chance before alarming."""
        r = Round.objects.create(org=self.org, round_number=1, series=self.series,
                                 competition=self.comp, lockout_at=self.kickoff)
        Match.objects.create(round=r, home_team=self.home, away_team=self.away,
                             kickoff_at=timezone.now() - timedelta(minutes=90),
                             external_id="ext-1", status=Match.STATUS_SCHEDULED)
        issues, _n = feedcheck._compare_round(
            self.svc, "AFL", self.series, 2098, 1,
            [_row("Carlton", "Richmond", kickoff=timezone.now() - timedelta(minutes=90))],
            [self.org.id], timezone.now(),
        )
        self.assertEqual(issues, [])

    def test_a_matching_graded_game_raises_nothing(self):
        r = Round.objects.create(org=self.org, round_number=1, series=self.series,
                                 competition=self.comp, lockout_at=self.kickoff)
        Match.objects.create(round=r, home_team=self.home, away_team=self.away,
                             kickoff_at=self.kickoff, external_id="ext-1",
                             status=Match.STATUS_COMPLETE, result="home",
                             home_score=20, away_score=10)
        issues, n = feedcheck._compare_round(
            self.svc, "AFL", self.series, 2098, 1,
            [_row("Carlton", "Richmond", kickoff=self.kickoff)],
            [self.org.id], timezone.now(),
        )
        self.assertEqual(issues, [])
        self.assertEqual(n, 1)

    def test_a_game_we_hold_that_the_feed_no_longer_lists_is_a_warning_not_an_error(self):
        r = Round.objects.create(org=self.org, round_number=1, series=self.series,
                                 competition=self.comp, lockout_at=self.kickoff)
        Match.objects.create(round=r, home_team=self.home, away_team=self.away,
                             kickoff_at=self.kickoff, external_id="stale",
                             status=Match.STATUS_SCHEDULED)
        issues, _n = feedcheck._compare_round(
            self.svc, "AFL", self.series, 2098, 1, [], [self.org.id], timezone.now(),
        )
        self.assertEqual([i["kind"] for i in issues], ["orphan_match"])
        self.assertEqual(issues[0]["severity"], feedcheck.WARNING)


class InUseTests(_Base):
    def test_groups_by_competition_series_and_year(self):
        use = feedcheck._in_use()
        self.assertIn("AFL", use)
        self.assertIn((self.series.name, self.season.year), use["AFL"])
        self.assertIn(self.org.id, use["AFL"][(self.series.name, self.season.year)])


class SignatureTests(TestCase):
    def test_same_errors_in_a_different_order_hash_the_same(self):
        a = [feedcheck._issue("missing_round", series="AFL", year=2098, round_number=1),
             feedcheck._issue("result_missing", series="NRL", year=2098, round_number=2)]
        b = list(reversed(a))
        self.assertEqual(feedcheck._signature(a), feedcheck._signature(b))

    def test_no_errors_is_an_empty_signature(self):
        self.assertEqual(feedcheck._signature([]), "")

    def test_a_different_error_set_hashes_differently(self):
        a = [feedcheck._issue("missing_round", series="AFL", year=2098, round_number=1)]
        b = [feedcheck._issue("missing_round", series="AFL", year=2098, round_number=2)]
        self.assertNotEqual(feedcheck._signature(a), feedcheck._signature(b))


class SchedulerStalledTests(TestCase):
    def test_a_kind_that_has_never_run_is_stalled(self):
        from .models import SyncSchedule

        SyncSchedule.objects.filter(kind__in=feedcheck.SCHEDULER_LIMITS).delete()
        issues = feedcheck._scheduler_issues()
        kinds = {i["kind"] for i in issues}
        self.assertEqual(kinds, {"scheduler_stalled"} if feedcheck.SCHEDULER_LIMITS else set())

    def test_a_kind_started_recently_is_not_stalled(self):
        from .models import SyncSchedule

        for kind in feedcheck.SCHEDULER_LIMITS:
            SyncSchedule.objects.update_or_create(
                kind=kind, defaults={"last_started_at": timezone.now()},
            )
        self.assertEqual(feedcheck._scheduler_issues(), [])

    def test_a_kind_stale_past_its_own_limit_is_stalled(self):
        from .models import SyncSchedule

        for kind, limit in feedcheck.SCHEDULER_LIMITS.items():
            SyncSchedule.objects.update_or_create(
                kind=kind, defaults={"last_started_at": timezone.now() - limit - timedelta(minutes=1)},
            )
        issues = feedcheck._scheduler_issues()
        self.assertEqual(len(issues), len(feedcheck.SCHEDULER_LIMITS))


class RunCheckTests(_Base):
    """The orchestration: compare, heal, re-check, record — with the feed
    itself replaced by a fake service, which is the seam the checker actually
    calls through (AflScrapeSyncService implements the same four methods)."""

    def setUp(self):
        super().setUp()
        self._in_use_patcher = patch.object(
            feedcheck, "_in_use",
            return_value={"AFL": {(self.series.name, self.season.year): [self.org.id]}},
        )
        self._in_use_patcher.start()
        self.addCleanup(self._in_use_patcher.stop)
        self._scheduler_patcher = patch.object(feedcheck, "_scheduler_issues", return_value=[])
        self._scheduler_patcher.start()
        self.addCleanup(self._scheduler_patcher.stop)

    def test_a_round_missing_everywhere_cannot_be_healed_by_a_resync(self):
        """sync_fixtures is mocked to a no-op here, so healing cannot actually
        create the round — proving an unresolved issue really does stay
        unresolved rather than vanishing by accident."""
        svc = self.svc
        svc.published_rounds = lambda *a, **k: [1]
        svc.feed_rows = lambda *a, **k: (self.series, [_row("Carlton", "Richmond", kickoff=self.kickoff)])
        svc.sync_fixtures = lambda **k: 0
        svc.sync_results = lambda **k: 0

        with patch("data_sync.services.get_sync_service", return_value=svc):
            check = feedcheck.run_check(alert=False)

        self.assertFalse(check.ok)
        self.assertEqual(check.healed, 1)  # it tried
        self.assertEqual([i["kind"] for i in check.unresolved], ["missing_round"])

    def test_healing_that_actually_creates_the_round_resolves_it(self):
        svc = self.svc
        svc.published_rounds = lambda *a, **k: [1]
        svc.feed_rows = lambda *a, **k: (self.series, [_row("Carlton", "Richmond", kickoff=self.kickoff)])

        def fake_sync_fixtures(**k):
            Round.objects.get_or_create(
                org=self.org, round_number=1, series=self.series,
                defaults={"competition": self.comp, "lockout_at": self.kickoff},
            )
            r = Round.objects.get(org=self.org, round_number=1, series=self.series)
            Match.objects.get_or_create(
                round=r, home_team=self.home, away_team=self.away,
                defaults={"kickoff_at": self.kickoff, "external_id": "ext-1"},
            )
            return 1
        svc.sync_fixtures = fake_sync_fixtures
        svc.sync_results = lambda **k: 0  # the game is "complete" in the row; results would grade it

        with patch("data_sync.services.get_sync_service", return_value=svc):
            check = feedcheck.run_check(alert=False)

        self.assertEqual(check.healed, 1)
        # The round now exists and the match now matches by team pairing, so
        # the only thing left wrong is the still-ungraded result — proving the
        # re-check genuinely re-read the database rather than trusting the fix.
        self.assertEqual([i["kind"] for i in check.unresolved], ["result_missing"])

    def test_dry_run_neither_heals_nor_alerts(self):
        svc = self.svc
        svc.published_rounds = lambda *a, **k: [1]
        svc.feed_rows = lambda *a, **k: (self.series, [_row("Carlton", "Richmond", kickoff=self.kickoff)])
        with patch("data_sync.services.get_sync_service", return_value=svc), \
             patch("data_sync.feedcheck._alert") as alert:
            check = feedcheck.run_check(heal=False, alert=False)
        self.assertEqual(check.healed, 0)
        alert.assert_not_called()

    def test_a_crash_is_recorded_rather_than_raised(self):
        with patch("data_sync.services.get_sync_service", side_effect=RuntimeError("feed is on fire")):
            check = feedcheck.run_check(alert=False)
        self.assertFalse(check.ok)
        self.assertIn("feed is on fire", check.error)

    def test_a_clean_run_needs_no_healing(self):
        r = Round.objects.create(org=self.org, round_number=1, series=self.series,
                                 competition=self.comp, lockout_at=self.kickoff)
        Match.objects.create(round=r, home_team=self.home, away_team=self.away,
                             kickoff_at=self.kickoff, external_id="ext-1",
                             status=Match.STATUS_COMPLETE, result="home",
                             home_score=20, away_score=10)
        svc = self.svc
        svc.published_rounds = lambda *a, **k: [1]
        svc.feed_rows = lambda *a, **k: (self.series, [_row("Carlton", "Richmond", kickoff=self.kickoff)])
        with patch("data_sync.services.get_sync_service", return_value=svc):
            check = feedcheck.run_check(alert=False)
        self.assertTrue(check.ok)
        self.assertEqual(check.healed, 0)
        self.assertEqual(check.unresolved, [])


class AlertTests(_Base):
    def test_errors_with_no_staff_holder_send_nothing(self):
        from .models import FeedCheck

        check = FeedCheck.objects.create(
            finished_at=timezone.now(),
            unresolved=[feedcheck._issue("missing_round", series="AFL", year=2098, round_number=1)],
        )
        check.signature = feedcheck._signature(check.unresolved_errors)
        self.assertFalse(feedcheck._alert(check))

    def test_an_all_clear_with_no_prior_alert_sends_nothing(self):
        from .models import FeedCheck

        check = FeedCheck.objects.create(finished_at=timezone.now(), unresolved=[])
        self.assertFalse(feedcheck._alert(check))

    def test_an_unchanged_problem_is_not_realerted_immediately(self):
        from accounts.models import User
        from data_sync.models import FeedCheck

        # A superuser with no AdminAccess row is full access by default (see
        # sysadmin.access) — the simplest staff holder of data.sync to set up.
        User.objects.create_user(
            email="sync-staff@example.com", password="x",
            is_staff=True, is_superuser=True, is_active=True,
        )

        issue = [feedcheck._issue("missing_round", series="AFL", year=2098, round_number=1)]
        sig = feedcheck._signature(issue)
        first = FeedCheck.objects.create(
            finished_at=timezone.now() - timedelta(hours=1),
            unresolved=issue, signature=sig, alerted=True,
        )
        second = FeedCheck.objects.create(finished_at=timezone.now(), unresolved=issue, signature=sig)

        with patch("goodtip.mail.send_template") as send:
            sent = feedcheck._alert(second)
        self.assertFalse(sent)
        send.assert_not_called()
