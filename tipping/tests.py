from django.test import TestCase
from django.utils import timezone

from accounts.models import User
from catalog.models import Season, Series, Sport
from orgs.models import OrgMember, Organisation
from tipping.models import Match, Round, Team, Tip
from tipping.services import leaderboard_for_org, record_match_result, user_org_stats


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
