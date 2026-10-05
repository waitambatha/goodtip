"""The Leaderboard's wider boards (client, 4 Oct 2026): industry, national,
everyone — and the Ladder going back to being only about teams."""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import Match, Round, Team, Tip

User = get_user_model()


class RankingsTests(TestCase):
    def setUp(self):
        from catalog.models import Competition, OrganisationType, Season, Series, Sport
        from orgs.models import OrgMember, Organisation

        self.series = Series.objects.get(name="NRL")
        self.season = Season.objects.create(year=2097, label="2097")
        sport = Sport.objects.first() or Sport.objects.create(name="Rugby League", slug="rl-test")
        self.comp = Competition.objects.create(sport=sport, season=self.season, name="NRL 2097", slug="nrl-2097")
        self.it = OrganisationType.objects.create(slug="it-test", name="IT Test")
        self.sales = OrganisationType.objects.create(slug="sales-test", name="Sales Test")
        self.h = Team.objects.create(name="R Home", slug="r-h", series=self.series)
        self.a = Team.objects.create(name="R Away", slug="r-a", series=self.series)
        self.when = timezone.now() - timedelta(days=3)

        def org(name, kind, listed=False):
            return Organisation.objects.create(name=name, season=self.season, organisation_type=kind,
                                               is_public_listed=listed)
        self.mine = org("My Small Office", self.it)
        self.big = org("Big Tech Co", self.it, listed=True)
        self.hidden = org("Quiet Corp", self.it)
        self.shop = org("Corner Shop", self.sales)
        self.me = User.objects.create_user(email="me@x.com", password="x", display_name="Erick Waita")
        OrgMember.objects.create(user=self.me, org=self.mine)
        self._n = 0

        def tip(user, o, points):
            self._n += 1
            rnd, _ = Round.objects.get_or_create(org=o, round_number=1, series=self.series,
                                                 defaults={"lockout_at": self.when, "competition": self.comp})
            m = Match.objects.create(round=rnd, home_team=self.h, away_team=self.a, kickoff_at=self.when, result="home")
            Tip.objects.create(user=user, match=m, org=o, selection="home", is_correct=points > 0, points_awarded=points)
        self.tip = tip

        # My Small Office: one tipper, 10 points  -> average 10
        tip(self.me, self.mine, 10)
        # Big Tech Co: four tippers, 30 points    -> average 7.5 (more points, worse average)
        for i in range(4):
            u = User.objects.create_user(email=f"b{i}@x.com", password="x", display_name=f"Big Person{i}")
            OrgMember.objects.create(user=u, org=self.big)
            tip(u, self.big, [12, 8, 6, 4][i])
        # Quiet Corp: one tipper, 5 points        -> average 5
        q = User.objects.create_user(email="q@x.com", password="x", display_name="Quinn Quiet")
        OrgMember.objects.create(user=q, org=self.hidden)
        tip(q, self.hidden, 5)
        # Corner Shop (another industry): 20      -> average 20
        s = User.objects.create_user(email="s@x.com", password="x", display_name="Sam Shop")
        OrgMember.objects.create(user=s, org=self.shop)
        tip(s, self.shop, 20)

    def test_industry_ranks_on_average_per_tipper_not_size(self):
        from .rankings import org_board

        board = org_board(self.mine, "industry")
        self.assertEqual([r["org"].id for r in board["rows"]], [self.mine.id, self.big.id, self.hidden.id])
        self.assertEqual(board["mine"]["rank"], 1)
        self.assertEqual(board["label"], "IT Test")

    def test_national_includes_every_industry(self):
        from .rankings import org_board

        board = org_board(self.mine, "national")
        self.assertEqual(board["rows"][0]["org"].id, self.shop.id)
        self.assertEqual(board["mine"]["rank"], 2)
        self.assertEqual(board["of"], 4)

    def test_only_opted_in_organisations_are_named_and_yours_always_is(self):
        from .rankings import org_board

        names = {r["org"].id: r["name"] for r in org_board(self.mine, "national")["rows"]}
        self.assertEqual(names[self.mine.id], "My Small Office")
        self.assertEqual(names[self.big.id], "Big Tech Co")
        self.assertEqual(names[self.hidden.id], "An organisation in IT Test")

    def test_everyone_has_one_entry_each_and_shows_short_names(self):
        from orgs.models import OrgMember

        from .rankings import everyone_board

        OrgMember.objects.create(user=self.me, org=self.shop)
        self.tip(self.me, self.shop, 3)                  # a second, weaker org for me
        board = everyone_board(self.me, self.mine)
        mine = [r for r in board["rows"] if r["is_me"]]
        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0]["points"], 10)         # my best season, not 13
        self.assertEqual(mine[0]["name"], "Erick W.")
        self.assertEqual(board["mine"]["rank"], 3)       # behind Sam (20) and Big Person0 (12)

    def test_the_leaderboard_shows_where_you_stand_and_the_boards(self):
        self.client.force_login(self.me)
        url = reverse("tipping:leaderboard", args=[self.mine.id])
        resp = self.client.get(url)
        self.assertContains(resp, "You nationally")
        self.assertContains(resp, 'class="lb-filters"')
        for view, text in (("industry", "How My Small Office compares"), ("national", "Every organisation on GoodTip"),
                           ("everyone", "Everyone on GoodTip")):
            self.assertContains(self.client.get(url, {"view": view}), text)

    def test_the_ladder_is_one_link_not_a_dropdown(self):
        self.client.force_login(self.me)
        resp = self.client.get(reverse("tipping:leaderboard", args=[self.mine.id]))
        self.assertNotContains(resp, "Industry ladder")
        self.assertNotContains(resp, "National ladder")
        self.assertContains(resp, reverse("tipping:ladder", args=[self.mine.id]))
