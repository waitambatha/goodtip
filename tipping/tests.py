from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from catalog.models import Season, Series, Sport
from orgs.models import OrgMember, Organisation
from tipping.models import Match, Round, Team, Tip
from tipping.services import (
    leaderboard_for_family,
    leaderboard_for_org,
    record_match_result,
    user_org_stats,
)


class WeightedScoringTests(TestCase):
    """Ambrose Hierarchy brief, slide 6: regular=1, finals=2, Origin=4."""

    def setUp(self):
        self.sport = Sport.objects.create(name="Test Footy", slug="test-footy")
        self.series = Series.objects.create(sport=self.sport, name="Test Series", slug="test-series")
        self.season = Season.objects.create(year=2099, label="2099")
        self.org = Organisation.objects.create(name="Test League", season=self.season)
        self.user = User.objects.create_user(email="a@b.com", password="x", display_name="Ada")
        OrgMember.objects.create(user=self.user, org=self.org)
        self.home = Team.objects.create(name="Broncos", slug="broncos", series=self.series)
        self.away = Team.objects.create(name="Storm", slug="storm", series=self.series)

    def _round(self, number, stage):
        return Round.objects.create(
            org=self.org, round_number=number, series=self.series,
            stage=stage, lockout_at=timezone.now(),
        )

    def _correct_tip(self, rnd):
        match = Match.objects.create(
            round=rnd, home_team=self.home, away_team=self.away,
            kickoff_at=timezone.now(),
        )
        Tip.objects.create(user=self.user, match=match, org=self.org, selection="home")
        record_match_result(match, 30, 10)  # home wins → the tip is correct
        return match

    def test_points_per_correct_by_stage(self):
        self.assertEqual(self._round(1, Round.STAGE_REGULAR).points_per_correct, 1)
        self.assertEqual(self._round(2, Round.STAGE_FINALS).points_per_correct, 2)
        self.assertEqual(self._round(3, Round.STAGE_ORIGIN).points_per_correct, 4)

    def test_correct_tip_awards_weighted_points(self):
        self._correct_tip(self._round(1, Round.STAGE_REGULAR))
        self._correct_tip(self._round(2, Round.STAGE_FINALS))
        self._correct_tip(self._round(3, Round.STAGE_ORIGIN))
        # 1 + 2 + 4 = 7
        self.assertEqual(user_org_stats(self.user, self.org)["points"], 7)
        self.assertEqual(user_org_stats(self.user, self.org)["tips_correct"], 3)

    def test_wrong_tip_awards_zero(self):
        rnd = self._round(1, Round.STAGE_ORIGIN)
        match = Match.objects.create(
            round=rnd, home_team=self.home, away_team=self.away, kickoff_at=timezone.now(),
        )
        Tip.objects.create(user=self.user, match=match, org=self.org, selection="away")
        record_match_result(match, 30, 10)  # home wins → away tip is wrong
        tip = Tip.objects.get(user=self.user, match=match)
        self.assertFalse(tip.is_correct)
        self.assertEqual(tip.points_awarded, 0)

    def test_leaderboard_sums_weighted_points(self):
        self._correct_tip(self._round(2, Round.STAGE_FINALS))
        row = leaderboard_for_org(self.org).get(id=self.user.id)
        self.assertEqual(row.points, 2)
        self.assertEqual(row.tips_correct, 1)


class FamilyLeaderboardTests(TestCase):
    """Org-structure note §8: one national competition; the national board
    ranks every member across the parent and all children, the local board
    filters to one org."""

    def setUp(self):
        self.sport = Sport.objects.create(name="Test Footy", slug="test-footy")
        self.series = Series.objects.create(sport=self.sport, name="Test Series", slug="test-series")
        self.season = Season.objects.create(year=2099, label="2099")
        self.parent = Organisation.objects.create(name="National Tiles", season=self.season)
        self.mitcham = Organisation.objects.create(
            name="National Tiles Mitcham", season=self.season, parent=self.parent,
        )
        self.preston = Organisation.objects.create(
            name="National Tiles Preston", season=self.season, parent=self.parent,
        )
        self.home = Team.objects.create(name="Broncos", slug="broncos", series=self.series)
        self.away = Team.objects.create(name="Storm", slug="storm", series=self.series)
        self.ada = self._member("ada@x.com", "Ada", self.mitcham)
        self.bob = self._member("bob@x.com", "Bob", self.preston)
        self.cec = self._member("cec@x.com", "Cec", self.parent)

    def _member(self, email, name, org):
        user = User.objects.create_user(email=email, password="x", display_name=name)
        OrgMember.objects.create(user=user, org=org)
        return user

    def _round_for(self, org, number=1):
        return Round.objects.create(
            org=org, round_number=number, series=self.series,
            stage=Round.STAGE_REGULAR, lockout_at=timezone.now(),
        )

    def _graded_tip(self, user, org, rnd, *, correct):
        match = Match.objects.create(
            round=rnd, home_team=self.home, away_team=self.away,
            kickoff_at=timezone.now(),
        )
        Tip.objects.create(
            user=user, match=match, org=org,
            selection="home" if correct else "away",
        )
        record_match_result(match, 30, 10)  # home wins
        return match

    def test_national_board_ranks_whole_family_from_any_org(self):
        self._graded_tip(self.ada, self.mitcham, self._round_for(self.mitcham), correct=True)
        self._graded_tip(self.bob, self.preston, self._round_for(self.preston), correct=True)
        self._graded_tip(self.cec, self.parent, self._round_for(self.parent), correct=False)
        board = list(leaderboard_for_family(self.mitcham))
        # All three tippers, across parent and both siblings (§7-style scope).
        self.assertEqual({u.display_name for u in board}, {"Ada", "Bob", "Cec"})
        points = {u.display_name: u.points for u in board}
        self.assertEqual(points, {"Ada": 1, "Bob": 1, "Cec": 0})

    def test_local_board_stays_filtered_to_own_org(self):
        self._graded_tip(self.ada, self.mitcham, self._round_for(self.mitcham), correct=True)
        self._graded_tip(self.bob, self.preston, self._round_for(self.preston), correct=True)
        board = list(leaderboard_for_org(self.mitcham))
        self.assertEqual([u.display_name for u in board], ["Ada"])

    def test_family_round_filter_aligns_by_round_number(self):
        mitcham_r1 = self._round_for(self.mitcham, number=1)
        preston_r1 = self._round_for(self.preston, number=1)
        preston_r2 = self._round_for(self.preston, number=2)
        self._graded_tip(self.ada, self.mitcham, mitcham_r1, correct=True)
        self._graded_tip(self.bob, self.preston, preston_r1, correct=True)
        self._graded_tip(self.bob, self.preston, preston_r2, correct=True)
        # Filtering by Mitcham's round 1 id must count Preston's round 1 too.
        board = {u.display_name: u.points for u in leaderboard_for_family(self.mitcham, round_id=mitcham_r1.id)}
        self.assertEqual(board["Ada"], 1)
        self.assertEqual(board["Bob"], 1)  # r2 tip excluded

    def test_leaderboard_page_scopes(self):
        self._graded_tip(self.ada, self.mitcham, self._round_for(self.mitcham), correct=True)
        self._graded_tip(self.bob, self.preston, self._round_for(self.preston), correct=True)
        self.client.force_login(self.ada)
        local = self.client.get(f"/org/{self.mitcham.id}/leaderboard/")
        self.assertContains(local, "Ada")
        self.assertNotContains(local, "Bob")
        national = self.client.get(f"/org/{self.mitcham.id}/leaderboard/?scope=national")
        self.assertContains(national, "Ada")
        self.assertContains(national, "Bob")
        self.assertContains(national, "ranked together")

    def test_standalone_org_has_no_scope_tabs(self):
        loner = Organisation.objects.create(name="Loner", season=self.season)
        solo = self._member("solo@x.com", "Solo", loner)
        self.client.force_login(solo)
        resp = self.client.get(f"/org/{loner.id}/leaderboard/")
        self.assertNotContains(resp, "scope=national")


class MatchStatePollTests(TestCase):
    """The in-play refresh endpoint the live badge polls.

    Before this existed, scores changed in the database every two minutes and
    never reached a page somebody already had open — the "live" badge pulsed
    against a number that only moved on reload.
    """

    def setUp(self):
        self.sport = Sport.objects.create(name="Poll Footy", slug="poll-footy")
        self.series = Series.objects.create(sport=self.sport, name="Poll Series", slug="poll-series")
        self.season = Season.objects.create(year=2099, label="2099")
        self.org = Organisation.objects.create(name="Poll League", season=self.season)
        self.user = User.objects.create_user(email="p@b.com", password="x", display_name="Pat")
        OrgMember.objects.create(user=self.user, org=self.org)
        self.round = Round.objects.create(
            org=self.org, round_number=1, series=self.series,
            lockout_at=timezone.now() - timedelta(hours=1),
        )
        self.home = Team.objects.create(name="Reds", slug="reds", series=self.series)
        self.away = Team.objects.create(name="Blues", slug="blues", series=self.series)

    def _match(self, **kw):
        return Match.objects.create(
            round=self.round, home_team=self.home, away_team=self.away,
            kickoff_at=timezone.now() - timedelta(hours=1), **kw,
        )

    def test_a_live_match_returns_its_score_and_keeps_polling(self):
        match = self._match(
            status=Match.STATUS_LIVE, period="Q3", clock="12:45",
            home_score=54, away_score=41,
        )
        self.client.force_login(self.user)
        r = self.client.get(reverse("tipping:match_state", args=[match.id]))
        self.assertEqual(r.status_code, 200)
        body = r.content.decode()
        self.assertIn("54–41", body)
        self.assertIn("Q3 12:45", body)
        # Still in play, so the fragment must carry the trigger that fetches
        # it again — otherwise the first poll is also the last.
        self.assertIn("every 30s", body)

    def test_a_finished_match_stops_polling(self):
        """The trigger has to disappear, or a completed game keeps costing a
        request every thirty seconds for the rest of the day."""
        match = self._match(
            status=Match.STATUS_COMPLETE, period="Full Time",
            home_score=80, away_score=60, result="home",
        )
        self.client.force_login(self.user)
        body = self.client.get(reverse("tipping:match_state", args=[match.id])).content.decode()
        self.assertNotIn("every 30s", body)
        self.assertIn("80–60", body)

    def test_it_requires_a_login(self):
        match = self._match(status=Match.STATUS_LIVE)
        r = self.client.get(reverse("tipping:match_state", args=[match.id]))
        self.assertEqual(r.status_code, 302)
