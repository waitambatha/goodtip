from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db.models import ProtectedError
from django.test import TestCase

from catalog.models import Charity, Season, Sport

from .models import (
    CharityVote,
    CharityVoteOption,
    MembershipRequest,
    OrgCharitySelection,
    OrgMember,
    Organisation,
)
from .services import (
    approve_membership_request,
    cast_charity_ballot,
    close_charity_vote,
    decline_membership_request,
    open_charity_vote,
    record_charity_selection,
    request_to_join,
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


class OrgHierarchyTests(TestCase):
    """Org-structure note §1/§3: standalone orgs are parents with zero
    children; a child sits under exactly one top-level parent; two levels only.
    """

    def setUp(self):
        self.season, _ = Season.objects.get_or_create(year=2099, defaults={"label": "Test"})
        self.charity, _ = Charity.objects.get_or_create(
            slug="lifeline", defaults={"name": "Lifeline", "is_approved": True}
        )
        self.parent = Organisation.objects.create(
            name="National Tiles", season=self.season, charity=self.charity
        )

    def make_org(self, name, **kwargs):
        return Organisation.objects.create(
            name=name, season=self.season, charity=self.charity, **kwargs
        )

    def test_standalone_org_is_its_own_root_and_family(self):
        self.assertFalse(self.parent.is_child)
        self.assertEqual(self.parent.root, self.parent)
        self.assertEqual(self.parent.family_ids(), [self.parent.id])

    def test_family_spans_root_and_all_children_from_any_member(self):
        mitcham = self.make_org("National Tiles Mitcham", parent=self.parent)
        preston = self.make_org("National Tiles Preston", parent=self.parent)
        expected = {self.parent.id, mitcham.id, preston.id}
        # §7: a child org's member sees the WHOLE family roll-up, siblings included.
        self.assertEqual(set(mitcham.family_ids()), expected)
        self.assertEqual(set(self.parent.family_ids()), expected)
        self.assertEqual(mitcham.root, self.parent)
        self.assertTrue(mitcham.is_child)

    def test_child_cannot_have_children(self):
        mitcham = self.make_org("National Tiles Mitcham", parent=self.parent)
        grandchild = Organisation(
            name="Mitcham Warehouse Crew", season=self.season,
            charity=self.charity, parent=mitcham,
        )
        with self.assertRaises(ValidationError):
            grandchild.full_clean()

    def test_org_cannot_be_its_own_parent(self):
        self.parent.parent = self.parent
        with self.assertRaises(ValidationError):
            self.parent.full_clean()

    def test_org_with_children_cannot_become_a_child(self):
        self.make_org("National Tiles Mitcham", parent=self.parent)
        other = self.make_org("Some Other Org")
        self.parent.parent = other
        with self.assertRaises(ValidationError):
            self.parent.full_clean()

    def test_deleting_parent_with_children_is_protected(self):
        self.make_org("National Tiles Mitcham", parent=self.parent)
        with self.assertRaises(ProtectedError):
            self.parent.delete()

    def test_child_keeps_its_own_charity_independent_of_parent(self):
        beyondblue, _ = Charity.objects.get_or_create(
            slug="beyond-blue", defaults={"name": "Beyond Blue", "is_approved": True}
        )
        mitcham = self.make_org("National Tiles Mitcham", parent=self.parent)
        set_org_charity(mitcham, beyondblue, source=OrgCharitySelection.SOURCE_MANUAL)
        mitcham.refresh_from_db()
        self.parent.refresh_from_db()
        # §5: no forced inheritance in either direction.
        self.assertEqual(mitcham.charity, beyondblue)
        self.assertEqual(self.parent.charity, self.charity)


class MembershipRequestTests(TestCase):
    """Org-structure note §2, client amendment: joining an org found via
    search goes through the org's admin — request, then approve/decline.
    """

    def setUp(self):
        self.season, _ = Season.objects.get_or_create(year=2099, defaults={"label": "Test"})
        self.charity, _ = Charity.objects.get_or_create(
            slug="lifeline", defaults={"name": "Lifeline", "is_approved": True}
        )
        self.org = Organisation.objects.create(
            name="National Tiles", season=self.season, charity=self.charity
        )
        self.admin = User.objects.create_user(
            email="admin@example.com", password="x", display_name="Admin",
        )
        OrgMember.objects.create(
            user=self.admin, org=self.org, role=OrgMember.ROLE_BOTH, is_league_owner=True,
        )
        self.joiner = User.objects.create_user(
            email="joiner@example.com", password="x", display_name="Joiner",
        )

    def test_request_does_not_create_membership(self):
        req = request_to_join(self.joiner, self.org)
        self.assertTrue(req.is_pending)
        self.assertFalse(OrgMember.objects.filter(user=self.joiner, org=self.org).exists())

    def test_repeat_request_returns_existing_pending(self):
        first = request_to_join(self.joiner, self.org)
        second = request_to_join(self.joiner, self.org)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(MembershipRequest.objects.count(), 1)

    def test_member_cannot_request(self):
        OrgMember.objects.create(user=self.joiner, org=self.org)
        with self.assertRaises(ValueError):
            request_to_join(self.joiner, self.org)

    def test_approve_creates_participant_and_records_decision(self):
        req = request_to_join(self.joiner, self.org)
        member = approve_membership_request(req, by_user=self.admin)
        req.refresh_from_db()
        self.assertEqual(req.status, MembershipRequest.STATUS_APPROVED)
        self.assertEqual(req.decided_by, self.admin)
        self.assertIsNotNone(req.decided_at)
        self.assertEqual(member.role, OrgMember.ROLE_PARTICIPANT)
        self.assertTrue(OrgMember.objects.filter(user=self.joiner, org=self.org).exists())

    def test_decline_leaves_no_membership_and_allows_reask(self):
        req = request_to_join(self.joiner, self.org)
        decline_membership_request(req, by_user=self.admin)
        req.refresh_from_db()
        self.assertEqual(req.status, MembershipRequest.STATUS_DECLINED)
        self.assertFalse(OrgMember.objects.filter(user=self.joiner, org=self.org).exists())
        # A declined user may ask again — a NEW pending request is created.
        again = request_to_join(self.joiner, self.org)
        self.assertNotEqual(again.pk, req.pk)
        self.assertTrue(again.is_pending)

    def test_decided_request_cannot_be_decided_twice(self):
        req = request_to_join(self.joiner, self.org)
        approve_membership_request(req, by_user=self.admin)
        with self.assertRaises(ValueError):
            decline_membership_request(req, by_user=self.admin)

    def test_request_join_endpoint_creates_pending_request(self):
        self.client.force_login(self.joiner)
        resp = self.client.post(f"/leagues/{self.org.id}/request-join/", follow=True)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(
            MembershipRequest.objects.filter(
                user=self.joiner, org=self.org, status=MembershipRequest.STATUS_PENDING,
            ).exists()
        )

    def test_members_page_lists_pending_and_admin_can_approve(self):
        req = request_to_join(self.joiner, self.org)
        self.client.force_login(self.admin)
        resp = self.client.get(f"/leagues/{self.org.id}/members/")
        self.assertContains(resp, "Join requests")
        self.assertContains(resp, "Joiner")
        resp = self.client.post(
            f"/leagues/{self.org.id}/members/",
            {"action": "approve_request", "request_id": req.id},
            follow=True,
        )
        self.assertContains(resp, "Joiner is now a member.")
        self.assertTrue(OrgMember.objects.filter(user=self.joiner, org=self.org).exists())

    def test_non_admin_cannot_approve(self):
        req = request_to_join(self.joiner, self.org)
        outsider = User.objects.create_user(
            email="outsider@example.com", password="x", display_name="Outsider",
        )
        self.client.force_login(outsider)
        resp = self.client.post(
            f"/leagues/{self.org.id}/members/",
            {"action": "approve_request", "request_id": req.id},
        )
        self.assertEqual(resp.status_code, 403)
        req.refresh_from_db()
        self.assertTrue(req.is_pending)

    def test_invite_link_still_joins_without_approval(self):
        # The signed invite token IS the admin's authorisation — no queue.
        from .signing import make_join_token

        token = make_join_token(self.org.id, inviter_id=self.admin.id)
        self.client.force_login(self.joiner)
        self.client.get(f"/join/{self.org.id}/{token}/")
        self.assertTrue(OrgMember.objects.filter(user=self.joiner, org=self.org).exists())


class OrgSearchTests(TestCase):
    """Org-structure note §2/§4: search surfaces close matches and, per match,
    both paths — ask to join, or create a child org under its parent."""

    def setUp(self):
        self.season, _ = Season.objects.get_or_create(year=2099, defaults={"label": "Test"})
        self.charity, _ = Charity.objects.get_or_create(
            slug="lifeline", defaults={"name": "Lifeline", "is_approved": True}
        )
        self.parent = Organisation.objects.create(
            name="National Tiles", season=self.season, charity=self.charity
        )
        self.child = Organisation.objects.create(
            name="National Tiles Mitcham", season=self.season,
            charity=self.charity, parent=self.parent,
        )
        self.user = User.objects.create_user(
            email="searcher@example.com", password="x", display_name="Searcher",
        )
        self.client.force_login(self.user)

    def test_json_returns_close_matches_with_root_for_child_creation(self):
        resp = self.client.get("/leagues/search.json", {"q": "mitcham"})
        rows = resp.json()["results"]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["name"], "National Tiles Mitcham")
        self.assertEqual(row["parent"], "National Tiles")
        # The create-a-child path points at the match's TOP-LEVEL parent.
        self.assertEqual(row["root_id"], self.parent.id)
        self.assertFalse(row["is_member"])
        self.assertFalse(row["pending"])

    def test_json_flags_membership_and_pending_request(self):
        OrgMember.objects.create(user=self.user, org=self.parent)
        request_to_join(self.user, self.child)
        rows = self.client.get("/leagues/search.json", {"q": "national"}).json()["results"]
        by_name = {r["name"]: r for r in rows}
        self.assertTrue(by_name["National Tiles"]["is_member"])
        self.assertTrue(by_name["National Tiles Mitcham"]["pending"])

    def test_json_requires_min_chars(self):
        rows = self.client.get("/leagues/search.json", {"q": "n"}).json()["results"]
        self.assertEqual(rows, [])

    def test_search_page_offers_both_paths(self):
        resp = self.client.get("/leagues/search/", {"q": "national tiles"})
        self.assertContains(resp, "Ask to join")
        self.assertContains(resp, f"?parent={self.parent.id}")

    def test_search_page_offers_create_when_no_match(self):
        resp = self.client.get("/leagues/search/", {"q": "zzz nothing"})
        self.assertContains(resp, "No groups match")

    def test_search_requires_login(self):
        self.client.logout()
        resp = self.client.get("/leagues/search/")
        self.assertEqual(resp.status_code, 302)


class DuplicateDetectionTests(TestCase):
    """Org-structure note §4 Stage 2: creating an org whose name already
    exists needs one explicit confirmation — friction, not prevention."""

    def setUp(self):
        from catalog.models import Competition, GroupType

        self.season, _ = Season.objects.get_or_create(year=2099, defaults={"label": "Test"})
        sport, _ = Sport.objects.get_or_create(name="AFL", defaults={"slug": "afl"})
        self.comp, _ = Competition.objects.get_or_create(
            sport=sport, season=self.season, slug="afl", defaults={"name": "AFL"},
        )
        self.charity, _ = Charity.objects.get_or_create(
            slug="lifeline", defaults={"name": "Lifeline", "is_approved": True},
        )
        self.charities_type = GroupType.objects.get(slug="charities")
        self.existing = Organisation.objects.create(
            name="National Tiles Mitcham", season=self.season, charity=self.charity,
        )
        self.user = User.objects.create_user(
            email="creator@example.com", password="x", display_name="Creator",
        )
        self.client.force_login(self.user)

    def create_post(self, name, **extra):
        data = {
            "name": name, "season": self.season.pk, "competitions": [self.comp.pk],
            "group_type": self.charities_type.pk,
            "charity_method": "pick", "charity": self.charity.pk,
        }
        data.update(extra)
        return self.client.post("/leagues/new/", data)

    def test_same_name_shows_confirmation_and_creates_nothing(self):
        resp = self.create_post("national tiles mitcham")  # case-insensitive
        self.assertContains(resp, "already exists")
        self.assertContains(resp, "duplicate_confirmed")
        self.assertEqual(Organisation.objects.filter(name__iexact="national tiles mitcham").count(), 1)

    def test_confirmed_resubmit_creates_independent_duplicate(self):
        resp = self.create_post("National Tiles Mitcham", duplicate_confirmed="1")
        self.assertEqual(resp.status_code, 302)
        dupes = Organisation.objects.filter(name="National Tiles Mitcham")
        self.assertEqual(dupes.count(), 2)
        # Fully independent: the new org has its own admin, not the old org's.
        new_org = dupes.exclude(pk=self.existing.pk).get()
        self.assertTrue(
            OrgMember.objects.filter(user=self.user, org=new_org, is_league_owner=True).exists()
        )
        self.assertFalse(OrgMember.objects.filter(user=self.user, org=self.existing).exists())

    def test_unique_name_creates_without_confirmation(self):
        resp = self.create_post("Totally Original Name")
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(Organisation.objects.filter(name="Totally Original Name").exists())
