from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from catalog.models import Charity, Season, Sport
from orgs.models import OrgMember, Organisation

from . import donations
from .models import CharityDisbursement, PlanSubscription
from .pricing import PRO, STARTER, tier_config

User = get_user_model()


class FamilyTotalsTests(TestCase):
    """Org-structure note §7: local and national totals, always separate,
    with child org totals rolling up into the parent automatically."""

    def setUp(self):
        self.season, _ = Season.objects.get_or_create(year=2099, defaults={"label": "Test Season"})
        self.lifeline, _ = Charity.objects.get_or_create(
            slug="lifeline", defaults={"name": "Lifeline", "is_approved": True}
        )
        self.beyondblue, _ = Charity.objects.get_or_create(
            slug="beyond-blue", defaults={"name": "Beyond Blue", "is_approved": True}
        )
        self.parent = Organisation.objects.create(
            name="National Tiles", season=self.season, charity=self.lifeline
        )
        self.mitcham = Organisation.objects.create(
            name="National Tiles Mitcham", season=self.season,
            charity=self.beyondblue, parent=self.parent,
        )
        self.preston = Organisation.objects.create(
            name="National Tiles Preston", season=self.season,
            charity=self.lifeline, parent=self.parent,
        )

    def test_standalone_org_has_no_national_figure(self):
        loner = Organisation.objects.create(
            name="Loner", season=self.season, charity=self.lifeline
        )
        donations.set_pledge(loner, pledged_amount=Decimal("100"))
        self.assertIsNone(donations.family_totals(loner))

    def test_rollup_spans_parent_and_all_children_across_charities(self):
        donations.set_pledge(self.parent, pledged_amount=Decimal("1000"))
        donations.set_pledge(self.mitcham, pledged_amount=Decimal("200"))
        donations.set_pledge(self.preston, pledged_amount=Decimal("300"))
        totals = donations.family_totals(self.mitcham)
        # Local stays the child's own figure; national is the whole family —
        # a dollar total regardless of each org's charity choice (§5).
        self.assertEqual(totals["local"], Decimal("200.00"))
        self.assertEqual(totals["national"], Decimal("1500.00"))
        self.assertEqual(totals["root"], self.parent)
        self.assertEqual(totals["org_count"], 3)

    def test_parent_sees_same_national_with_its_own_local(self):
        donations.set_pledge(self.parent, pledged_amount=Decimal("1000"))
        donations.set_pledge(self.mitcham, pledged_amount=Decimal("200"))
        totals = donations.family_totals(self.parent)
        self.assertEqual(totals["local"], Decimal("1000.00"))
        self.assertEqual(totals["national"], Decimal("1200.00"))

    def test_orgs_without_pledges_count_as_zero(self):
        donations.set_pledge(self.mitcham, pledged_amount=Decimal("200"))
        totals = donations.family_totals(self.preston)
        self.assertEqual(totals["local"], Decimal("0.00"))
        self.assertEqual(totals["national"], Decimal("200.00"))

    def test_dashboard_shows_both_figures_to_child_member(self):
        donations.set_pledge(self.parent, pledged_amount=Decimal("1000"))
        donations.set_pledge(self.mitcham, pledged_amount=Decimal("200"))
        member = User.objects.create_user(
            email="m@example.com", password="x", display_name="M",
        )
        OrgMember.objects.create(user=member, org=self.mitcham)
        self.client.force_login(member)
        resp = self.client.get(reverse("dashboard"))
        self.assertContains(resp, "National Tiles national total")
        self.assertContains(resp, "$1,200")
        self.assertContains(resp, "$200")


def _activate_plan(org, tier):
    cfg = tier_config(tier)
    return PlanSubscription.objects.create(
        org=org, season=org.season, tier=tier,
        price_aud=cfg["price"], seat_limit=cfg["seat_limit"],
        status=PlanSubscription.STATUS_ACTIVE,
    )


class DonationEngineTests(TestCase):
    def setUp(self):
        self.season, _ = Season.objects.get_or_create(year=2099, defaults={"label": "Test Season"})
        self.sport, _ = Sport.objects.get_or_create(name="AFL", defaults={"slug": "afl"})
        self.charity, _ = Charity.objects.get_or_create(
            slug="test-charity", defaults={"name": "Test Charity", "is_approved": True}
        )
        self.org = Organisation.objects.create(name="Acme", season=self.season, charity=self.charity)
        self.user = User.objects.create_user(email="p@example.com", password="x", display_name="P")

    def test_pledge_anchors_the_total(self):
        donations.set_pledge(self.org, pledged_amount=Decimal("500"))
        s = donations.donation_summary(self.org)
        self.assertEqual(s["base"], Decimal("500.00"))
        self.assertEqual(s["raised"], Decimal("500.00"))
        self.assertEqual(s["goal"], Decimal("500.00"))
        self.assertEqual(s["pct"], 100)

    def test_topup_without_matching(self):
        pledge = donations.set_pledge(self.org, pledged_amount=Decimal("200"))
        result = donations.record_topup(pledge, participant=self.user, amount=Decimal("20"))
        self.assertEqual(result["matched"], Decimal("0"))
        s = donations.donation_summary(self.org)
        self.assertEqual(s["topups"], Decimal("20.00"))
        self.assertEqual(s["matched"], Decimal("0.00"))
        self.assertEqual(s["raised"], Decimal("220.00"))

    def test_matching_doubles_topup_until_cap(self):
        pledge = donations.set_pledge(
            self.org, pledged_amount=Decimal("200"),
            matching_enabled=True, matching_cap=Decimal("50"),
        )
        # $20 top-up → $20 matched (cap not yet reached)
        r1 = donations.record_topup(pledge, participant=self.user, amount=Decimal("20"))
        self.assertEqual(r1["matched"], Decimal("20.00"))
        self.assertFalse(r1["cap_reached"])
        # $40 more → only $30 left under the $50 cap gets matched
        r2 = donations.record_topup(pledge, participant=self.user, amount=Decimal("40"))
        self.assertEqual(r2["matched"], Decimal("30.00"))
        self.assertTrue(r2["cap_reached"])
        # Further top-ups still go to charity but are no longer doubled
        r3 = donations.record_topup(pledge, participant=self.user, amount=Decimal("10"))
        self.assertEqual(r3["matched"], Decimal("0.00"))

        s = donations.donation_summary(self.org)
        self.assertEqual(s["topups"], Decimal("70.00"))   # 20 + 40 + 10
        self.assertEqual(s["matched"], Decimal("50.00"))  # capped
        self.assertEqual(s["matching_remaining"], Decimal("0"))
        self.assertEqual(s["raised"], Decimal("320.00"))  # 200 base + 70 + 50
        self.assertEqual(s["goal"], Decimal("250.00"))    # 200 base + 50 cap
        self.assertEqual(s["pct"], 100)

    def test_disbursement_aggregates_the_season(self):
        pledge = donations.set_pledge(
            self.org, pledged_amount=Decimal("200"),
            matching_enabled=True, matching_cap=Decimal("200"),
        )
        donations.record_topup(pledge, participant=self.user, amount=Decimal("15"))
        d = donations.create_disbursement(self.org)
        self.assertEqual(d.total_base_aud, Decimal("200.00"))
        self.assertEqual(d.total_topups_aud, Decimal("15.00"))
        self.assertEqual(d.total_matched_aud, Decimal("15.00"))
        self.assertEqual(d.total_disbursed_aud, Decimal("230.00"))
        self.assertEqual(d.charity, self.charity)

    def test_negative_topup_rejected(self):
        pledge = donations.set_pledge(self.org, pledged_amount=Decimal("100"))
        with self.assertRaises(ValueError):
            donations.record_topup(pledge, participant=self.user, amount=Decimal("0"))


class DonationViewTests(TestCase):
    def setUp(self):
        self.season, _ = Season.objects.get_or_create(year=2099, defaults={"label": "Test Season"})
        self.sport, _ = Sport.objects.get_or_create(name="AFL", defaults={"slug": "afl"})
        self.charity, _ = Charity.objects.get_or_create(
            slug="test-charity", defaults={"name": "Test Charity", "is_approved": True}
        )
        self.org = Organisation.objects.create(name="Acme", season=self.season, charity=self.charity)
        self.owner = User.objects.create_user(email="o@example.com", password="x", display_name="Owner")
        OrgMember.objects.create(user=self.owner, org=self.org, role=OrgMember.ROLE_BOTH, is_league_owner=True)
        self.player = User.objects.create_user(email="p@example.com", password="x", display_name="Player")
        OrgMember.objects.create(user=self.player, org=self.org, role=OrgMember.ROLE_PARTICIPANT)

    def test_owner_sets_pledge_via_view(self):
        self.client.force_login(self.owner)
        url = reverse("billing:pledge", args=[self.org.id])
        self.assertEqual(self.client.get(url).status_code, 200)
        resp = self.client.post(url, {
            "pledged_amount": "500",
            "payment_schedule": "season_close",
            "matching_enabled": "on",
            "matching_cap": "200",
        })
        self.assertEqual(resp.status_code, 302)
        pledge = donations.get_pledge(self.org)
        self.assertEqual(pledge.pledged_amount_aud, Decimal("500.00"))
        self.assertTrue(pledge.matching_enabled)
        self.assertEqual(pledge.matching_cap_aud, Decimal("200.00"))

    def test_participant_cannot_set_pledge(self):
        self.client.force_login(self.player)
        url = reverse("billing:pledge", args=[self.org.id])
        self.assertEqual(self.client.get(url).status_code, 403)

    def test_participant_topup_recorded_without_gateway(self):
        donations.set_pledge(self.org, pledged_amount=Decimal("200"))
        self.client.force_login(self.player)
        url = reverse("billing:topup", args=[self.org.id])
        self.assertEqual(self.client.get(url).status_code, 200)
        resp = self.client.post(url, {"amount": "25"})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(donations.donation_summary(self.org)["topups"], Decimal("25.00"))

    def test_manager_closes_season(self):
        donations.set_pledge(self.org, pledged_amount=Decimal("300"))
        self.client.force_login(self.owner)
        url = reverse("billing:season_summary", args=[self.org.id])
        self.assertEqual(self.client.get(url).status_code, 200)
        self.client.post(url)
        self.assertTrue(CharityDisbursement.objects.filter(org=self.org, season=self.season).exists())

    def test_esg_export_blocked_below_pro(self):
        _activate_plan(self.org, STARTER)
        donations.set_pledge(self.org, pledged_amount=Decimal("100"))
        self.client.force_login(self.owner)
        resp = self.client.get(reverse("billing:esg_report", args=[self.org.id]))
        self.assertEqual(resp.status_code, 302)

    def test_esg_pdf_for_pro_owner(self):
        _activate_plan(self.org, PRO)
        donations.set_pledge(self.org, pledged_amount=Decimal("500"))
        donations.record_topup(donations.get_pledge(self.org), participant=self.player, amount=Decimal("20"))
        self.client.force_login(self.owner)
        resp = self.client.get(reverse("billing:esg_report", args=[self.org.id]))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "application/pdf")
        self.assertTrue(resp.content.startswith(b"%PDF"))
