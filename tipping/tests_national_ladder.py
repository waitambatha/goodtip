from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from billing.models import DonationPayment, DonationPledge
from catalog.models import Competition, GoodListConfig, OrganisationType, Season, Series, State, SubCategory
from orgs.models import Organisation

from . import national_ladder
from .models import Match, Round, Team, Tip


class NationalLadderTests(TestCase):
    """The Industry/National ladder — same opt-in and credibility rules as
    The Good List, points and dollars queried separately and merged by org id
    so neither figure can be inflated by the other (see the module
    docstring's cross-product warning)."""

    def setUp(self):
        self.season = Season.objects.create(year=2099, label="2099")
        self.nrl = Series.objects.get(slug="nrl")
        sport = self.nrl.sport
        self.comp = Competition.objects.create(
            sport=sport, season=self.season, name="Ladder Comp", slug="ladder-comp",
        )
        self.comp.series.add(self.nrl)
        self.nsw = State.objects.get(code="NSW")
        self.vic = State.objects.get(code="VIC")
        self.business = OrganisationType.objects.get(slug="business")
        self.community = OrganisationType.objects.get(slug="community")
        self.home = Team.objects.create(name="LH", slug="ladder-h", series=self.nrl)
        self.away = Team.objects.create(name="LA", slug="ladder-a", series=self.nrl)
        cfg = GoodListConfig.get()
        cfg.credibility_min_groups = 2
        cfg.save()
        self._user_seq = 0

    def make_org(self, name, *, public=True, season=None, state=None, organisation_type=None,
                 sub_categories=()):
        org = Organisation.objects.create(
            name=name, season=season or self.season, is_public_listed=public,
            state=state, organisation_type=organisation_type or self.business,
        )
        if sub_categories:
            org.sub_categories.set(sub_categories)
        return org

    def _user(self):
        self._user_seq += 1
        return User.objects.create_user(
            email=f"nl{self._user_seq}@example.com", password="x", display_name=f"U{self._user_seq}",
        )

    def make_round(self, org, *, competition=None, number=1):
        return Round.objects.create(
            org=org, round_number=number, series=self.nrl,
            competition=competition if competition is not None else self.comp,
            lockout_at=timezone.now() - timedelta(days=1), status="complete",
        )

    def make_tip(self, org, rnd, *, group=None, correct=True, points=1):
        match = Match.objects.create(
            round=rnd, home_team=self.home, away_team=self.away,
            kickoff_at=timezone.now() - timedelta(days=1),
            status="complete", result="home", home_score=20, away_score=10,
        )
        Tip.objects.create(
            user=self._user(), match=match, org=org, group=group,
            selection="home" if correct else "away",
            is_correct=correct, points_awarded=points if correct else 0,
        )

    _pledges = None

    def make_donation(self, org, amount, *, settled=True):
        if self._pledges is None:
            self._pledges = {}
        pledge = self._pledges.get(org.id)
        if pledge is None:
            pledge = DonationPledge.objects.create(
                org=org, season=org.season, pledged_amount_aud=Decimal("0"),
            )
            self._pledges[org.id] = pledge
        DonationPayment.objects.create(
            pledge=pledge, org=org, amount_aud=Decimal(amount),
            type=DonationPayment.TYPE_TOP_UP, paid_by=DonationPayment.PAID_BY_PARTICIPANT,
            settled_at=timezone.now() if settled else None,
        )

    def test_hidden_below_credibility_threshold(self):
        self.make_org("Solo Co", public=True)
        self.assertFalse(national_ladder.board_is_live())
        self.assertEqual(national_ladder.ladder(), [])
        self.make_org("Second Co", public=True)
        self.assertTrue(national_ladder.board_is_live())

    def test_excludes_non_consenting_orgs(self):
        a = self.make_org("Public A", public=True)
        self.make_org("Public B", public=True)
        private = self.make_org("Private Co", public=False)
        self.make_tip(a, self.make_round(a))
        self.make_donation(private, "9999")
        names = [r["name"] for r in national_ladder.ladder()]
        self.assertNotIn("Private Co", names)

    def test_points_and_donations_merge_without_cross_product(self):
        """Three correct tips (3 points) and two $100 donations ($200) must
        stay 3 and 200 — not 6 and 600, the shape a joined annotate() would
        produce by fanning the two relations against each other."""
        org = self.make_org("Merge Co", public=True)
        self.make_org("Other Co", public=True)
        rnd = self.make_round(org)
        for _ in range(3):
            self.make_tip(org, rnd, correct=True, points=1)
        self.make_donation(org, "100")
        self.make_donation(org, "100")
        row = next(r for r in national_ladder.ladder() if r["name"] == "Merge Co")
        self.assertEqual(row["points"], 3)
        self.assertEqual(row["raised"], Decimal("200.00"))

    def test_org_with_points_but_no_donations_still_appears(self):
        org = self.make_org("Points Only", public=True)
        self.make_org("Other Co", public=True)
        self.make_tip(org, self.make_round(org), correct=True)
        row = next(r for r in national_ladder.ladder() if r["name"] == "Points Only")
        self.assertEqual(row["points"], 1)
        self.assertEqual(row["raised"], Decimal("0"))

    def test_org_with_donations_but_no_points_still_appears(self):
        org = self.make_org("Donors Only", public=True)
        self.make_org("Other Co", public=True)
        self.make_donation(org, "50")
        row = next(r for r in national_ladder.ladder() if r["name"] == "Donors Only")
        self.assertEqual(row["points"], 0)
        self.assertEqual(row["raised"], Decimal("50.00"))

    def test_org_with_neither_is_excluded(self):
        org = self.make_org("Nothing Co", public=True)
        self.make_org("Other Co", public=True)
        names = [r["name"] for r in national_ladder.ladder()]
        self.assertNotIn("Nothing Co", names)

    def test_group_tips_excluded_from_org_points(self):
        """group__isnull=True — a tip made inside a Group must not inflate
        its parent organisation's own ladder total."""
        from orgs.models import Group
        org = self.make_org("Group Parent", public=True)
        self.make_org("Other Co", public=True)
        group = Group.objects.create(name="Sales", org=org)
        rnd = self.make_round(org)
        self.make_tip(org, rnd, group=group, correct=True)
        row = next((r for r in national_ladder.ladder() if r["name"] == "Group Parent"), None)
        self.assertIsNone(row)

    def test_points_scoped_to_each_orgs_own_season(self):
        """F("org__season") — an org's points come from tips graded in ITS
        season, so orgs on different seasons never bleed into each other."""
        old_season = Season.objects.create(year=2098, label="2098")
        org_new = self.make_org("New Season Co", public=True, season=self.season)
        org_old = self.make_org("Old Season Co", public=True, season=old_season)
        old_comp = Competition.objects.create(
            sport=self.nrl.sport, season=old_season, name="Old Comp", slug="old-comp",
        )
        old_comp.series.add(self.nrl)
        # A tip graded under the WRONG season's competition for org_new must
        # not count toward it.
        mismatched_round = self.make_round(org_new, competition=old_comp)
        self.make_tip(org_new, mismatched_round, correct=True)
        # A tip under org_old's own season correctly counts for org_old.
        own_round = self.make_round(org_old, competition=old_comp)
        self.make_tip(org_old, own_round, correct=True)

        rows = {r["name"]: r for r in national_ladder.ladder()}
        self.assertNotIn("New Season Co", rows)
        self.assertEqual(rows["Old Season Co"]["points"], 1)

    def test_type_filter_separates_industries(self):
        club = self.make_org("Club Co", public=True, organisation_type=self.community)
        self.make_org("Club Co 2", public=True, organisation_type=self.community)
        corp = self.make_org("Corp Co", public=True, organisation_type=self.business)
        self.make_org("Corp Co 2", public=True, organisation_type=self.business)
        self.make_donation(club, "10")
        self.make_donation(corp, "10")
        community_names = [r["name"] for r in national_ladder.ladder(organisation_type_slug="community")]
        self.assertIn("Club Co", community_names)
        self.assertNotIn("Corp Co", community_names)

    def test_sub_category_filter_dual_surface(self):
        education = OrganisationType.objects.get(slug="education")
        primary = SubCategory.objects.get(organisation_type=education, slug="primary-school")
        secondary = SubCategory.objects.get(organisation_type=education, slug="secondary-school")
        both = self.make_org(
            "Both Ways College", public=True, organisation_type=education,
            sub_categories=[primary, secondary],
        )
        self.make_org("Filler Co", public=True)
        self.make_donation(both, "10")
        under_primary = [r["name"] for r in national_ladder.ladder(sub_category_slug="primary-school")]
        under_secondary = [r["name"] for r in national_ladder.ladder(sub_category_slug="secondary-school")]
        self.assertIn("Both Ways College", under_primary)
        self.assertIn("Both Ways College", under_secondary)

    def test_state_filter(self):
        nsw_org = self.make_org("NSW Co", public=True, state=self.nsw)
        vic_org = self.make_org("VIC Co", public=True, state=self.vic)
        self.make_donation(nsw_org, "10")
        self.make_donation(vic_org, "10")
        names = [r["name"] for r in national_ladder.ladder(state_code="NSW")]
        self.assertEqual(names, ["NSW Co"])

    def test_ranking_order_points_then_raised_then_name(self):
        low = self.make_org("Zeta Co", public=True)
        high = self.make_org("Alpha Co", public=True)
        rnd_high = self.make_round(high)
        rnd_low = self.make_round(low)
        self.make_tip(high, rnd_high, correct=True, points=5)
        self.make_tip(low, rnd_low, correct=True, points=1)
        board = national_ladder.ladder()
        self.assertEqual([r["name"] for r in board], ["Alpha Co", "Zeta Co"])
        self.assertEqual(board[0]["rank"], 1)
        self.assertEqual(board[1]["rank"], 2)


class TippingLadderViewTests(TestCase):
    def setUp(self):
        self.season = Season.objects.create(year=2099, label="2099")
        self.business = OrganisationType.objects.get(slug="business")
        cfg = GoodListConfig.get()
        cfg.credibility_min_groups = 2
        cfg.save()

    def make_org(self, name, *, public=True, state=None):
        return Organisation.objects.create(
            name=name, season=self.season, is_public_listed=public,
            state=state, organisation_type=self.business,
        )

    def test_page_loads_before_board_is_live(self):
        resp = self.client.get(reverse("tipping_ladder"))
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.context["board_live"])
        self.assertEqual(resp.context["board"], [])

    def test_unknown_type_slug_is_dropped_not_404d(self):
        resp = self.client.get(reverse("tipping_ladder"), {"type": "not-a-real-slug"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["sel_type"], "")

    def test_valid_type_slug_is_kept(self):
        resp = self.client.get(reverse("tipping_ladder"), {"type": "business"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["sel_type"], "business")

    def test_unknown_state_code_is_dropped(self):
        resp = self.client.get(reverse("tipping_ladder"), {"state": "ZZ"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["sel_state"], "")

    def test_cat_without_matching_type_is_dropped(self):
        resp = self.client.get(reverse("tipping_ladder"), {"cat": "primary-school"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["sel_cat"], "")
