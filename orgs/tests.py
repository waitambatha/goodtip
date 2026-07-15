from django.contrib.auth import get_user_model
from django.test import TestCase

from catalog.models import Charity, Season, Sport

from .models import (
    CharityVote,
    CharityVoteOption,
    OrgCharitySelection,
    OrgMember,
    Organisation,
)
from .services import (
    cast_charity_ballot,
    close_charity_vote,
    open_charity_vote,
    record_charity_selection,
    set_org_charity,
)

User = get_user_model()


class CharityTimelineTests(TestCase):
    def setUp(self):
        self.season, _ = Season.objects.get_or_create(year=2099, defaults={"label": "Test"})
        self.sport, _ = Sport.objects.get_or_create(name="AFL", defaults={"slug": "afl"})
        self.lifeline, _ = Charity.objects.get_or_create(slug="lifeline", defaults={"name": "Lifeline", "is_approved": True})
        self.beyondblue, _ = Charity.objects.get_or_create(slug="beyond-blue", defaults={"name": "Beyond Blue", "is_approved": True})
        self.org = Organisation.objects.create(name="Acme", season=self.season, charity=self.lifeline)

    def test_record_initial_then_change_appends_history(self):
        record_charity_selection(self.org, self.lifeline, source=OrgCharitySelection.SOURCE_INITIAL)
        # Re-recording the same charity is a no-op (no duplicate row).
        record_charity_selection(self.org, self.lifeline, source=OrgCharitySelection.SOURCE_INITIAL)
        self.assertEqual(self.org.charity_selections.count(), 1)

        set_org_charity(self.org, self.beyondblue, source=OrgCharitySelection.SOURCE_MANUAL)
        self.org.refresh_from_db()
        self.assertEqual(self.org.charity, self.beyondblue)

        history = list(self.org.charity_selections.values_list("charity__name", flat=True))
        # Newest first per Meta.ordering.
        self.assertEqual(history, ["Beyond Blue", "Lifeline"])
        self.assertEqual(self.org.charity_selections.count(), 2)

    def test_closing_vote_records_a_selection(self):
        vote = open_charity_vote(self.org, [self.lifeline, self.beyondblue])
        voter = User.objects.create_user(email="v@example.com", password="x", display_name="V")
        OrgMember.objects.create(user=voter, org=self.org, role=OrgMember.ROLE_PARTICIPANT)
        option = vote.options.get(charity=self.beyondblue)
        cast_charity_ballot(user=voter, vote=vote, option=option)

        winner = close_charity_vote(vote)
        self.assertEqual(winner, self.beyondblue)
        self.org.refresh_from_db()
        self.assertEqual(self.org.charity, self.beyondblue)
        latest = self.org.charity_selections.first()
        self.assertEqual(latest.charity, self.beyondblue)
        self.assertEqual(latest.source, OrgCharitySelection.SOURCE_VOTE)
        self.assertEqual(latest.season, self.season)


class PaymentCharityFreezeTests(TestCase):
    def setUp(self):
        from billing import donations

        self.donations = donations
        self.season, _ = Season.objects.get_or_create(year=2099, defaults={"label": "Test"})
        self.lifeline, _ = Charity.objects.get_or_create(slug="lifeline", defaults={"name": "Lifeline", "is_approved": True})
        self.beyondblue, _ = Charity.objects.get_or_create(slug="beyond-blue", defaults={"name": "Beyond Blue", "is_approved": True})
        self.org = Organisation.objects.create(name="Acme", season=self.season, charity=self.lifeline)
        self.user = User.objects.create_user(email="p@example.com", password="x", display_name="P")

    def test_payment_keeps_charity_after_org_switches(self):
        from decimal import Decimal

        pledge = self.donations.set_pledge(self.org, pledged_amount=Decimal("100"))
        self.donations.record_topup(pledge, participant=self.user, amount=Decimal("20"))
        early_payment = pledge.payments.first()
        self.assertEqual(early_payment.charity, self.lifeline)

        # Org switches charity; re-pledge mirrors the new one onto the pledge.
        set_org_charity(self.org, self.beyondblue, source=OrgCharitySelection.SOURCE_MANUAL)
        self.donations.set_pledge(self.org, pledged_amount=Decimal("100"))

        early_payment.refresh_from_db()
        # The historical payment must still point at the original charity.
        self.assertEqual(early_payment.charity, self.lifeline)


class OrgCategoryFormTests(TestCase):
    """Per-type sign-up rules from the categories doc (7 Jul 2026)."""

    def setUp(self):
        from catalog.models import Competition, GroupType, SubCategory

        self.season, _ = Season.objects.get_or_create(year=2099, defaults={"label": "Test"})
        self.sport, _ = Sport.objects.get_or_create(name="AFL", defaults={"slug": "afl"})
        self.comp, _ = Competition.objects.get_or_create(
            sport=self.sport, season=self.season, slug="afl", defaults={"name": "AFL"},
        )
        self.charity, _ = Charity.objects.get_or_create(
            slug="lifeline", defaults={"name": "Lifeline", "is_approved": True},
        )
        self.types = {g.slug: g for g in GroupType.objects.all()}
        self.subcat = lambda t, s: SubCategory.objects.get(group_type__slug=t, slug=s)

    def form(self, **extra):
        from .forms import OrgCreateForm

        data = {
            "name": "Testers", "season": self.season.pk, "competitions": [self.comp.pk],
            "charity_method": "pick", "charity": self.charity.pk,
        }
        data.update(extra)
        return OrgCreateForm(data)

    def test_five_types_in_spec_order(self):
        from catalog.models import GroupType

        self.assertEqual(
            list(GroupType.objects.values_list("slug", flat=True)),
            ["community", "business", "education", "charities", "informal"],
        )

    def test_type_is_required(self):
        f = self.form()
        self.assertFalse(f.is_valid())
        self.assertIn("group_type", f.errors)

    def test_business_requires_exactly_one_sub_category(self):
        f = self.form(group_type=self.types["business"].pk)
        self.assertFalse(f.is_valid())
        self.assertIn("sub_categories", f.errors)
        f = self.form(group_type=self.types["business"].pk, sub_categories=[
            self.subcat("business", "finance").pk, self.subcat("business", "tech").pk,
        ])
        self.assertFalse(f.is_valid())
        f = self.form(group_type=self.types["business"].pk,
                      sub_categories=[self.subcat("business", "finance").pk])
        self.assertTrue(f.is_valid(), f.errors)

    def test_education_allows_primary_plus_secondary_only(self):
        pair = [self.subcat("education", "primary-school").pk,
                self.subcat("education", "secondary-school").pk]
        f = self.form(group_type=self.types["education"].pk, sub_categories=pair)
        self.assertTrue(f.is_valid(), f.errors)
        org = f.save()
        self.assertEqual(org.sub_categories.count(), 2)
        # Any other combination is rejected.
        f = self.form(group_type=self.types["education"].pk, sub_categories=[
            self.subcat("education", "university").pk, self.subcat("education", "tafe").pk,
        ])
        self.assertFalse(f.is_valid())
        self.assertIn("sub_categories", f.errors)

    def test_informal_requires_self_description(self):
        f = self.form(group_type=self.types["informal"].pk)
        self.assertFalse(f.is_valid())
        self.assertIn("informal_label", f.errors)
        f = self.form(group_type=self.types["informal"].pk, informal_label="Book Club")
        self.assertTrue(f.is_valid(), f.errors)
        org = f.save()
        self.assertEqual(org.category_label, "Book Club")

    def test_charities_type_needs_no_sub_category(self):
        f = self.form(group_type=self.types["charities"].pk)
        self.assertTrue(f.is_valid(), f.errors)

    def test_stale_sub_categories_from_other_type_are_dropped(self):
        f = self.form(group_type=self.types["charities"].pk,
                      sub_categories=[self.subcat("business", "finance").pk])
        self.assertTrue(f.is_valid(), f.errors)
        org = f.save()
        self.assertEqual(org.sub_categories.count(), 0)


class CharityPartnerWorkflowTests(TestCase):
    """Charity Partner Workflow (categories doc): lock-to-self is gated on the
    admin-set partner flag; non-partners stay on the vote path."""

    def setUp(self):
        from catalog.models import GroupType

        self.season, _ = Season.objects.get_or_create(year=2099, defaults={"label": "Test"})
        self.charities_type = GroupType.objects.get(slug="charities")
        self.community_type = GroupType.objects.get(slug="community")
        self.lifeline, _ = Charity.objects.get_or_create(
            slug="lifeline", defaults={"name": "Lifeline", "is_approved": True},
        )
        self.beyondblue, _ = Charity.objects.get_or_create(
            slug="beyond-blue", defaults={"name": "Beyond Blue", "is_approved": True},
        )
        self.user = User.objects.create_user(
            email="boss@charity.org", password="x", display_name="Boss",
        )

    def make_org(self, *, partner=False, group_type=None):
        org = Organisation.objects.create(
            name="Helping Hands", season=self.season,
            group_type=group_type or self.charities_type, is_charity_partner=partner,
        )
        OrgMember.objects.create(
            user=self.user, org=org, role=OrgMember.ROLE_BOTH, is_league_owner=True,
        )
        return org

    def test_non_partner_cannot_lock(self):
        from .services import lock_fundraising_to_self

        org = self.make_org(partner=False)
        with self.assertRaises(ValueError):
            lock_fundraising_to_self(org)

    def test_non_charity_type_cannot_lock_even_if_flagged(self):
        from .services import lock_fundraising_to_self

        org = self.make_org(partner=True, group_type=self.community_type)
        with self.assertRaises(ValueError):
            lock_fundraising_to_self(org)

    def test_partner_lock_sets_own_charity_and_closes_vote(self):
        from .services import lock_fundraising_to_self

        org = self.make_org(partner=True)
        vote = open_charity_vote(org, [self.lifeline, self.beyondblue])
        charity = lock_fundraising_to_self(org)
        org.refresh_from_db()
        vote.refresh_from_db()
        self.assertEqual(org.charity, charity)
        self.assertEqual(charity.name, org.name)
        self.assertFalse(charity.is_approved)  # not in other leagues' pickers
        self.assertEqual(vote.status, "closed")
        self.assertIsNone(vote.winning_charity)
        latest = org.charity_selections.first()
        self.assertEqual(latest.source, OrgCharitySelection.SOURCE_SELF)

    def test_lock_view_requires_partner_flag(self):
        org = self.make_org(partner=False)
        self.client.force_login(self.user)
        resp = self.client.post(f"/leagues/{org.id}/lock-fundraising/", follow=True)
        org.refresh_from_db()
        self.assertIsNone(org.charity)
        self.assertContains(resp, "Only confirmed GoodTip Partner Charities")

    def test_lock_view_happy_path(self):
        org = self.make_org(partner=True)
        self.client.force_login(self.user)
        resp = self.client.post(f"/leagues/{org.id}/lock-fundraising/", follow=True)
        self.assertEqual(resp.status_code, 200)
        org.refresh_from_db()
        self.assertIsNotNone(org.charity)
        self.assertEqual(org.charity.name, org.name)

    def test_vote_page_shows_partner_cta_to_non_partner_manager(self):
        org = self.make_org(partner=False)
        self.client.force_login(self.user)
        resp = self.client.get(f"/leagues/{org.id}/charity-vote/")
        self.assertContains(resp, "Want to become a GoodTip Partner Charity?")

    def test_vote_page_shows_lock_button_to_partner_manager(self):
        org = self.make_org(partner=True)
        self.client.force_login(self.user)
        resp = self.client.get(f"/leagues/{org.id}/charity-vote/")
        self.assertContains(resp, "Lock fundraising to us")
