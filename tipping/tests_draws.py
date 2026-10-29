"""Tipping a draw (client, 27 Oct 2026: "in things like football the user
should be able to tip home or away or draw... if they tip draw points is
given also as a win"), and the sports switches that decide what is offered."""
from django.test import TestCase
from django.utils import timezone

from accounts.models import User
from catalog.models import Competition, Season, Series, Sport
from orgs.models import OrgMember, Organisation
from tipping.models import Match, Round, Team, Tip
from tipping.services import draw_tippable, record_match_result, submit_tip


class DrawTipTests(TestCase):
    def setUp(self):
        self.season = Season.objects.create(year=2099, label="2099")
        self.org = Organisation.objects.create(name="Draw League", season=self.season)
        self.user = User.objects.create_user(email="d@b.com", password="x", display_name="Dee")
        self.other = User.objects.create_user(email="e@b.com", password="x", display_name="Eve")
        for u in (self.user, self.other):
            OrgMember.objects.create(user=u, org=self.org)

    def _match(self, draw_tips):
        sport = Sport.objects.create(name=f"Code {draw_tips}", slug=f"code-{draw_tips}", draw_tips=draw_tips)
        series = Series.objects.create(sport=sport, name=f"Series {draw_tips}", slug=f"series-{draw_tips}")
        home = Team.objects.create(name=f"Home {draw_tips}", slug=f"home-{draw_tips}", series=series)
        away = Team.objects.create(name=f"Away {draw_tips}", slug=f"away-{draw_tips}", series=series)
        rnd = Round.objects.create(org=self.org, round_number=1, series=series,
                                   lockout_at=timezone.now() + timezone.timedelta(days=2))
        return Match.objects.create(round=rnd, home_team=home, away_team=away,
                                    kickoff_at=timezone.now() + timezone.timedelta(days=2))

    def test_a_draw_pick_pays_like_a_winner_where_the_sport_allows_it(self):
        match = self._match(True)
        self.assertTrue(draw_tippable(match))
        Tip.objects.create(user=self.user, match=match, org=self.org, selection="draw")
        Tip.objects.create(user=self.other, match=match, org=self.org, selection="home")
        record_match_result(match, 1, 1)
        mine = Tip.objects.get(user=self.user, match=match)
        theirs = Tip.objects.get(user=self.other, match=match)
        self.assertTrue(mine.is_correct)
        self.assertEqual(mine.points_awarded, match.round.points_per_correct)
        self.assertFalse(theirs.is_correct)
        self.assertEqual(theirs.points_awarded, 0)
        self.assertEqual(mine.pick_text, f"a draw, {match.home_team.name} v {match.away_team.name}")

    def test_a_draw_pick_is_wrong_when_someone_wins(self):
        match = self._match(True)
        Tip.objects.create(user=self.user, match=match, org=self.org, selection="draw")
        record_match_result(match, 2, 1)
        self.assertFalse(Tip.objects.get(user=self.user, match=match).is_correct)

    def test_a_draw_cannot_be_tipped_where_the_sport_has_no_draws(self):
        match = self._match(False)
        with self.assertRaises(ValueError):
            submit_tip(user=self.user, match=match, org=self.org, selection="draw")

    def test_afl_style_draws_keep_their_fixture_rule(self):
        """Where a draw cannot be tipped, a drawn match still pays every tip
        the round's draw points (0 in a regular round) — unchanged."""
        match = self._match(False)
        Tip.objects.create(user=self.user, match=match, org=self.org, selection="home")
        record_match_result(match, 50, 50)
        tip = Tip.objects.get(user=self.user, match=match)
        self.assertFalse(tip.is_correct)
        self.assertEqual(tip.points_awarded, match.round.points_per_draw)


class SportsSwitchTests(TestCase):
    def test_the_defaults_are_afl_and_the_nrl(self):
        on = set(Series.objects.filter(is_active=True, sport__is_active=True).values_list("name", flat=True))
        self.assertTrue({"AFL", "AFLW", "NRL", "NRLW"} <= on)
        self.assertFalse(Series.objects.filter(name="English Premier League", is_active=True).exists())
        self.assertTrue(Sport.objects.get(slug="football").draw_tips)
        self.assertEqual(Sport.objects.filter(is_active=False).count() >= 10, True)

    def test_switching_a_competition_off_withdraws_it_from_signup(self):
        from orgs.forms import fed_competitions

        season = Season.objects.order_by("-year").first() or Season.objects.create(year=2026)
        comp = Competition.objects.filter(series__name="AFL").first()
        if comp is None:
            sport = Sport.objects.get(slug="australian-rules")
            comp = Competition.objects.create(sport=sport, season=season, name="AFL", slug="afl-test")
            comp.series.add(Series.objects.get(name="AFL"), Series.objects.get(name="AFLW"))
        self.assertIn(comp, fed_competitions())
        Series.objects.filter(competitions=comp).update(is_active=False)
        self.assertNotIn(comp, fed_competitions())
        Series.objects.filter(competitions=comp).update(is_active=True)
        Sport.objects.filter(pk=comp.sport_id).update(is_active=False)
        self.assertNotIn(comp, fed_competitions())


class LateArrivalTests(TestCase):
    """A competition added part-way through a season brings games already
    played. Nobody could tip those, so nobody is defaulted onto them — an
    automatic "away" pick there would be free points (27 Oct 2026)."""

    def test_no_default_tip_on_a_game_that_arrived_after_kickoff(self):
        season = Season.objects.create(year=2098, label="2098")
        org = Organisation.objects.create(name="Late League", season=season)
        user = User.objects.create_user(email="l@b.com", password="x", display_name="Lee")
        member = OrgMember.objects.create(user=user, org=org)
        OrgMember.objects.filter(pk=member.pk).update(joined_at=timezone.now() - timezone.timedelta(days=30))
        sport = Sport.objects.create(name="Late code", slug="late-code")
        series = Series.objects.create(sport=sport, name="Late series", slug="late-series")
        home = Team.objects.create(name="H", slug="h", series=series)
        away = Team.objects.create(name="A", slug="a", series=series)
        rnd = Round.objects.create(org=org, round_number=1, series=series,
                                   lockout_at=timezone.now() - timezone.timedelta(days=3))
        match = Match.objects.create(round=rnd, home_team=home, away_team=away,
                                     kickoff_at=timezone.now() - timezone.timedelta(days=3))
        self.assertTrue(match.arrived_after_kickoff)
        record_match_result(match, 1, 2)
        self.assertFalse(Tip.objects.filter(match=match).exists())

    def test_a_game_known_before_kickoff_still_gets_the_default(self):
        season = Season.objects.create(year=2097, label="2097")
        org = Organisation.objects.create(name="Early League", season=season)
        user = User.objects.create_user(email="m@b.com", password="x", display_name="Mo")
        member = OrgMember.objects.create(user=user, org=org)
        OrgMember.objects.filter(pk=member.pk).update(joined_at=timezone.now() - timezone.timedelta(days=30))
        sport = Sport.objects.create(name="Early code", slug="early-code")
        series = Series.objects.create(sport=sport, name="Early series", slug="early-series")
        home = Team.objects.create(name="H", slug="h", series=series)
        away = Team.objects.create(name="A", slug="a", series=series)
        rnd = Round.objects.create(org=org, round_number=1, series=series,
                                   lockout_at=timezone.now() - timezone.timedelta(days=3))
        match = Match.objects.create(round=rnd, home_team=home, away_team=away,
                                     kickoff_at=timezone.now() - timezone.timedelta(days=3))
        Match.objects.filter(pk=match.pk).update(created_at=timezone.now() - timezone.timedelta(days=10))
        match.refresh_from_db()
        record_match_result(match, 1, 2)
        tip = Tip.objects.get(match=match, user=user)
        self.assertTrue(tip.is_auto)
        self.assertEqual(tip.selection, "away")
