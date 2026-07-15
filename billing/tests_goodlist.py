from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from catalog.models import Charity, GoodListConfig, GroupType, Season, State, SubCategory
from orgs.models import Organisation

from . import goodlist
from .models import DonationPayment, DonationPledge

User = get_user_model()


class GoodListTests(TestCase):
    """The Good List build spec — settled-only money, privacy/credibility
    thresholds, per-group consent, and private-board anonymisation."""

    def setUp(self):
        self.season = Season.objects.create(year=2099, label="2099")
        self.lifeline = Charity.objects.create(name="GL Lifeline", slug="gl-lifeline", is_approved=True)
        self.headspace = Charity.objects.create(name="GL headspace", slug="gl-headspace", is_approved=True)
        self.nsw = State.objects.get(code="NSW")
        self.vic = State.objects.get(code="VIC")
        self.business = GroupType.objects.get(slug="business")
        self.community = GroupType.objects.get(slug="community")
        self.education = GroupType.objects.get(slug="education")
        # Small thresholds so tests stay readable.
        cfg = GoodListConfig.get()
        cfg.privacy_min_groups = 3
        cfg.credibility_min_groups = 2
        cfg.save()

    def make_group(self, name, *, amount, charity=None, state=None, sub_categories=(),
                   group_type=None, settled=True, public=False):
        org = Organisation.objects.create(
            name=name, season=self.season, charity=charity or self.lifeline,
            state=state, group_type=group_type or self.business,
            is_public_listed=public,
        )
        if sub_categories:
            org.sub_categories.set(sub_categories)
        pledge = DonationPledge.objects.create(
            org=org, season=self.season, charity=org.charity, pledged_amount_aud=Decimal("0"),
        )
        DonationPayment.objects.create(
            pledge=pledge, org=org, charity=org.charity, amount_aud=Decimal(amount),
            type=DonationPayment.TYPE_TOP_UP, paid_by=DonationPayment.PAID_BY_PARTICIPANT,
            settled_at=timezone.now() if settled else None,
        )
        return org

    def test_national_total_counts_settled_only(self):
        self.make_group("A", amount="100")
        self.make_group("B", amount="50")
        self.make_group("C-unsettled", amount="999", settled=False)
        self.assertEqual(goodlist.national_total(), Decimal("150.00"))

    def test_by_charity_hidden_below_privacy_threshold(self):
        # 2 groups behind Lifeline — below the threshold of 3, so hidden.
        self.make_group("A", amount="100", charity=self.lifeline)
        self.make_group("B", amount="100", charity=self.lifeline)
        self.assertEqual(goodlist.by_charity(), [])
        # A 3rd group tips it over the threshold — now it shows.
        self.make_group("C", amount="100", charity=self.lifeline)
        rows = goodlist.by_charity()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["label"], "GL Lifeline")
        self.assertEqual(rows[0]["groups"], 3)
        self.assertEqual(rows[0]["raised"], Decimal("300.00"))

    def test_unsettled_group_does_not_count_toward_threshold(self):
        self.make_group("A", amount="100", state=self.nsw)
        self.make_group("B", amount="100", state=self.nsw)
        self.make_group("C-unsettled", amount="100", state=self.nsw, settled=False)
        # Only 2 settled groups behind NSW — still below threshold.
        self.assertEqual(goodlist.by_state(), [])

    def test_by_group_hidden_until_credibility_threshold(self):
        # One consenting group — board not live yet (threshold is 2).
        self.make_group("Public One", amount="500", public=True)
        self.assertFalse(goodlist.board_is_live())
        self.assertEqual(goodlist.by_group(), [])
        # Second consenting group flips the board live.
        self.make_group("Public Two", amount="300", public=True)
        self.assertTrue(goodlist.board_is_live())
        names = [r["name"] for r in goodlist.by_group()]
        self.assertEqual(names, ["Public One", "Public Two"])

    def test_by_group_excludes_non_consenting(self):
        self.make_group("Public One", amount="500", public=True)
        self.make_group("Public Two", amount="300", public=True)
        self.make_group("Private", amount="9999", public=False)  # not consenting
        names = [r["name"] for r in goodlist.by_group()]
        self.assertNotIn("Private", names)

    def test_type_filter_separates_surfaces(self):
        self.make_group("Club A", amount="100", public=True, group_type=self.community)
        self.make_group("Club B", amount="100", public=True, group_type=self.community)
        self.make_group("Corp A", amount="500", public=True, group_type=self.business)
        self.make_group("Corp B", amount="500", public=True, group_type=self.business)
        community = [r["name"] for r in goodlist.by_group(group_type_slug="community")]
        self.assertEqual(sorted(community), ["Club A", "Club B"])

    def test_sub_category_filter_and_education_dual_surface(self):
        """Categories doc build note: a school holding Primary + Secondary must
        appear under BOTH sub-category filters."""
        primary = SubCategory.objects.get(group_type=self.education, slug="primary-school")
        secondary = SubCategory.objects.get(group_type=self.education, slug="secondary-school")
        finance = SubCategory.objects.get(group_type=self.business, slug="finance")
        self.make_group("Both Ways College", amount="400", public=True,
                        group_type=self.education, sub_categories=[primary, secondary])
        self.make_group("Finance Co", amount="200", public=True,
                        group_type=self.business, sub_categories=[finance])
        under_primary = [r["name"] for r in goodlist.by_group(sub_category_slug="primary-school")]
        under_secondary = [r["name"] for r in goodlist.by_group(sub_category_slug="secondary-school")]
        self.assertIn("Both Ways College", under_primary)
        self.assertIn("Both Ways College", under_secondary)
        self.assertNotIn("Finance Co", under_primary)
        # And its label shows both sub-categories.
        row = next(r for r in goodlist.by_group() if r["name"] == "Both Ways College")
        self.assertEqual(row["category"], "Primary School + Secondary School")

    def test_state_filter(self):
        self.make_group("NSW Co", amount="100", public=True, state=self.nsw)
        self.make_group("VIC Co", amount="100", public=True, state=self.vic)
        names = [r["name"] for r in goodlist.by_group(state_code="NSW")]
        self.assertEqual(names, ["NSW Co"])

    def test_informal_label_shows_as_category(self):
        informal = GroupType.objects.get(slug="informal")
        org = self.make_group("The Crew", amount="100", public=True, group_type=informal)
        self.make_group("Filler", amount="50", public=True)
        org.informal_label = "Cycling Crew"
        org.save(update_fields=["informal_label"])
        row = next(r for r in goodlist.by_group() if r["name"] == "The Crew")
        self.assertEqual(row["category"], "Cycling Crew")

    def test_private_board_anonymises_other_groups(self):
        mine = self.make_group("My Workplace", amount="100", state=self.nsw)
        self.make_group("Their Public Co", amount="500", state=self.vic, public=True)
        self.make_group("Their Private Co", amount="300", state=self.vic, public=False)
        board = goodlist.private_board(mine)
        names = [r["name"] for r in board]
        self.assertIn("My Workplace", names)          # own group named
        self.assertIn("Their Public Co", names)       # consenting group named
        self.assertIn("A business in Victoria", names)  # private group anonymised
        self.assertNotIn("Their Private Co", names)

    def test_set_and_revoke_consent(self):
        user = User.objects.create_user(email="gl@example.com", password="x", display_name="GL")
        org = self.make_group("Opt In Co", amount="100", public=False)
        org.set_public_consent(granted=True, by_user=user)
        org.refresh_from_db()
        self.assertTrue(org.is_public_listed)
        self.assertTrue(org.public_consent_reconfirmed)
        self.assertEqual(org.public_consent_by, user)
        self.assertIsNotNone(org.public_consent_at)
        # Revoking pulls it from the public board.
        org.set_public_consent(granted=False)
        org.refresh_from_db()
        self.assertFalse(org.is_public_listed)
