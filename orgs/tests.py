from datetime import timedelta

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
    WallPost,
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
        # The page is JS-driven: it carries the join button and the
        # create-under-parent link builder; search.json supplies the root_id
        # the builder plugs into ?parent=.
        resp = self.client.get("/leagues/search/", {"q": "national tiles"})
        self.assertContains(resp, "Ask to join")
        self.assertContains(resp, "?parent=")
        rows = self.client.get("/leagues/search.json", {"q": "national tiles"}).json()["results"]
        by_name = {r["name"]: r for r in rows}
        self.assertEqual(by_name["National Tiles"]["root_id"], self.parent.id)

    def test_search_page_offers_create_when_no_match(self):
        resp = self.client.get("/leagues/search/", {"q": "zzz nothing"})
        self.assertContains(resp, "No groups match")

    def test_search_requires_login(self):
        self.client.logout()
        resp = self.client.get("/leagues/search/")
        self.assertEqual(resp.status_code, 302)


def _walk_create_wizard(client, case, name, **extra):
    """Drive the create-a-group wizard end to end and return the final response.

    The wizard replaced a single all-fields POST. These callers care about what
    happens at the end — a duplicate warning, a child org, a redirect — not
    about the steps, so the stepping lives here rather than in every test.
    """
    url = "/leagues/new/"
    parent = extra.get("parent", "")
    client.post(url, {
        "step": 1, "action": "next", "name": name,
        "group_type": case.charities_type.pk, "parent": parent,
    })
    client.post(url, {
        "step": 2, "action": "next",
        "competitions": [case.comp.pk], "season": case.season.pk, "parent": parent,
    })
    client.post(url, {
        "step": 3, "action": "next",
        "charity_method": "pick", "charity": case.charity.pk, "parent": parent,
    })
    final = {"step": 4, "action": "next", "parent": parent}
    if extra.get("duplicate_confirmed"):
        final["duplicate_confirmed"] = extra["duplicate_confirmed"]
    return client.post(url, final)


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
        """Creating is a four-step wizard now — walk it and return the last
        response, so these tests still describe the outcome, not the mechanics."""
        return _walk_create_wizard(self.client, self, name, **extra)

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


class ChildOrgCreationTests(TestCase):
    """Org-structure note §0/§2/§3: creating a child org under an existing
    parent makes the creator that CHILD's admin — they do not join the parent.
    """

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
        self.parent = Organisation.objects.create(
            name="National Tiles", season=self.season, charity=self.charity,
        )
        self.user = User.objects.create_user(
            email="franchisee@example.com", password="x", display_name="Franchisee",
        )
        self.client.force_login(self.user)

    def create_post(self, name, **extra):
        return _walk_create_wizard(self.client, self, name, **extra)

    def test_get_with_parent_preloads_banner_and_hidden_field(self):
        resp = self.client.get(f"/leagues/new/?parent={self.parent.id}")
        self.assertContains(resp, f"Part of {self.parent.name}")
        self.assertContains(resp, f'name="parent" value="{self.parent.id}"')

    def test_creator_becomes_child_admin_not_parent_member(self):
        resp = self.create_post("National Tiles Mitcham", parent=self.parent.pk)
        self.assertEqual(resp.status_code, 302)
        child = Organisation.objects.get(name="National Tiles Mitcham")
        self.assertEqual(child.parent, self.parent)
        # §0: admin of the child they created…
        self.assertTrue(
            OrgMember.objects.filter(user=self.user, org=child, is_league_owner=True).exists()
        )
        # …and NOT a member (let alone admin) of the parent.
        self.assertFalse(OrgMember.objects.filter(user=self.user, org=self.parent).exists())

    def test_child_org_cannot_be_a_parent_option(self):
        child = Organisation.objects.create(
            name="National Tiles Mitcham", season=self.season,
            charity=self.charity, parent=self.parent,
        )
        resp = self.create_post("Mitcham Warehouse Crew", parent=child.pk)
        # Two levels only (§3): a child is not a valid parent choice.
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(Organisation.objects.filter(name="Mitcham Warehouse Crew").exists())

    def test_child_creation_still_passes_duplicate_check(self):
        Organisation.objects.create(
            name="National Tiles Mitcham", season=self.season,
            charity=self.charity, parent=self.parent,
        )
        resp = self.create_post("National Tiles Mitcham", parent=self.parent.pk)
        self.assertContains(resp, "already exists")
        # The parent banner survives the §4 confirmation re-render.
        self.assertContains(resp, f"Part of {self.parent.name}")
        self.assertEqual(Organisation.objects.filter(name="National Tiles Mitcham").count(), 1)

    def test_invalid_parent_param_falls_back_to_standalone(self):
        resp = self.client.get("/leagues/new/?parent=999999")
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, "Part of ")


class ChildAdminManagementTests(TestCase):
    """Org-structure note §6: parent org admins can remove or reassign a
    child org's admin — e.g. when a location closes down."""

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
        self.parent_admin = User.objects.create_user(
            email="hq@example.com", password="x", display_name="HQ",
        )
        OrgMember.objects.create(
            user=self.parent_admin, org=self.parent,
            role=OrgMember.ROLE_BOTH, is_league_owner=True,
        )
        self.child_admin = User.objects.create_user(
            email="mitcham@example.com", password="x", display_name="Mitcham Boss",
        )
        self.child_admin_member = OrgMember.objects.create(
            user=self.child_admin, org=self.child,
            role=OrgMember.ROLE_BOTH, is_league_owner=True,
        )

    def test_parent_admin_sees_child_groups_panel(self):
        self.client.force_login(self.parent_admin)
        resp = self.client.get(f"/leagues/{self.parent.id}/members/")
        self.assertContains(resp, "Child groups")
        self.assertContains(resp, "National Tiles Mitcham")
        self.assertContains(resp, "Mitcham Boss")

    def test_child_admin_page_has_no_child_groups_panel(self):
        self.client.force_login(self.child_admin)
        resp = self.client.get(f"/leagues/{self.child.id}/members/")
        self.assertNotContains(resp, "Child groups")

    def test_parent_admin_can_demote_child_admin(self):
        self.client.force_login(self.parent_admin)
        self.client.post(f"/leagues/{self.parent.id}/members/", {
            "action": "demote_child_admin", "member_id": self.child_admin_member.id,
        })
        self.child_admin_member.refresh_from_db()
        # Hats off, but they stay a member of the child org.
        self.assertEqual(self.child_admin_member.role, OrgMember.ROLE_PARTICIPANT)
        self.assertFalse(self.child_admin_member.is_league_owner)

    def test_parent_admin_can_reassign_child_admin_to_new_user(self):
        successor = User.objects.create_user(
            email="new-boss@example.com", password="x", display_name="New Boss",
        )
        self.client.force_login(self.parent_admin)
        self.client.post(f"/leagues/{self.parent.id}/members/", {
            "action": "assign_child_admin", "child_id": self.child.id,
            "email": "new-boss@example.com",
        })
        m = OrgMember.objects.get(user=successor, org=self.child)
        # §6 rationale: the target needn't be a member yet — they're added.
        self.assertTrue(m.is_league_owner)
        self.assertEqual(m.role, OrgMember.ROLE_BOTH)

    def test_demote_scoped_to_own_children(self):
        other_parent = Organisation.objects.create(
            name="Rival Corp", season=self.season, charity=self.charity
        )
        rival_admin = User.objects.create_user(
            email="rival@example.com", password="x", display_name="Rival",
        )
        OrgMember.objects.create(
            user=rival_admin, org=other_parent,
            role=OrgMember.ROLE_BOTH, is_league_owner=True,
        )
        self.client.force_login(rival_admin)
        # Rival Corp's admin cannot touch National Tiles Mitcham's admin.
        resp = self.client.post(f"/leagues/{other_parent.id}/members/", {
            "action": "demote_child_admin", "member_id": self.child_admin_member.id,
        })
        self.assertEqual(resp.status_code, 404)
        self.child_admin_member.refresh_from_db()
        self.assertTrue(self.child_admin_member.is_league_owner)

    def test_child_admin_cannot_use_parent_page(self):
        self.client.force_login(self.child_admin)
        resp = self.client.post(f"/leagues/{self.parent.id}/members/", {
            "action": "demote_child_admin", "member_id": self.child_admin_member.id,
        })
        # Not a member of the parent org at all → forbidden.
        self.assertEqual(resp.status_code, 403)


class RoundRecapTests(TestCase):
    """AI Group Recap launch-minimum (docs/ai-group-recap-spec.md §§1–4, 7, 10)."""

    def setUp(self):
        from datetime import timedelta

        from django.utils import timezone

        from catalog.models import Season, Series, Sport
        from tipping.models import Match, Round, Team

        self.sport = Sport.objects.create(name="Recap Footy", slug="recap-footy")
        self.series = Series.objects.create(sport=self.sport, name="Recap Series", slug="recap-series")
        self.season = Season.objects.create(year=2098, label="2098")
        self.org = Organisation.objects.create(name="Recap League", season=self.season)
        self.user = User.objects.create_user(email="r@b.com", password="x", display_name="Dave")
        OrgMember.objects.create(user=self.user, org=self.org)
        self.home = Team.objects.create(name="Pies", slug="pies-r", series=self.series)
        self.away = Team.objects.create(name="Roos", slug="roos-r", series=self.series)
        self.rnd = Round.objects.create(
            org=self.org, round_number=1, series=self.series,
            stage=Round.STAGE_REGULAR,
            lockout_at=timezone.now() - timedelta(days=2),
        )
        self.match = Match.objects.create(
            round=self.rnd, home_team=self.home, away_team=self.away,
            kickoff_at=timezone.now() - timedelta(days=1),
        )

    def _tip_and_settle(self):
        from tipping.models import Tip
        from tipping.services import record_match_result

        Tip.objects.create(user=self.user, match=self.match, org=self.org, selection="home")
        record_match_result(self.match, 30, 10)

    def test_nobody_tipped_means_silence(self):
        from tipping.services import record_match_result

        from .recaps import generate_recaps

        record_match_result(self.match, 30, 10)
        with self.settings(ANTHROPIC_API_KEY="test-key"):
            results = generate_recaps(org=self.org)
        self.assertEqual(results, [])
        self.assertFalse(WallPost.objects.filter(org=self.org).exists())

    def test_unresolved_round_is_not_ready(self):
        from tipping.models import Tip

        from .recaps import generate_recaps, round_ready_for_recap

        Tip.objects.create(user=self.user, match=self.match, org=self.org, selection="home")
        self.assertFalse(round_ready_for_recap(self.rnd))
        with self.settings(ANTHROPIC_API_KEY="test-key"):
            self.assertEqual(generate_recaps(org=self.org), [])

    def test_recap_posts_once_and_pins(self):
        from unittest.mock import patch

        from .models import RoundRecap
        from .recaps import generate_recaps

        self._tip_and_settle()
        fake = "Dave backed the Pies and got the points. He tops the ladder on 1."
        with self.settings(ANTHROPIC_API_KEY="test-key"), \
             patch("orgs.recaps.generate_recap_text", return_value=fake):
            first = generate_recaps(org=self.org)
            second = generate_recaps(org=self.org)
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])  # idempotent — one per (org, round)
        post = WallPost.objects.get(org=self.org, kind=WallPost.KIND_RECAP)
        self.assertEqual(post.body, fake)
        self.assertIsNone(post.author)
        recap = RoundRecap.objects.get(org=self.org, round=self.rnd)
        self.assertEqual(recap.post, post)
        self.assertFalse(recap.fallback_used)

    def test_model_failure_uses_factual_fallback(self):
        from unittest.mock import patch

        from .models import RoundRecap
        from .recaps import generate_recaps

        self._tip_and_settle()
        with self.settings(ANTHROPIC_API_KEY="test-key"), \
             patch("orgs.recaps.generate_recap_text", side_effect=RuntimeError("api down")):
            results = generate_recaps(org=self.org)
        self.assertEqual(len(results), 1)
        recap = RoundRecap.objects.get(org=self.org, round=self.rnd)
        self.assertTrue(recap.fallback_used)
        self.assertIn("Dave", recap.post.body)
        self.assertIn("1 point", recap.post.body)

    def test_no_api_key_skips_without_burning_the_slot(self):
        from unittest.mock import patch

        from .models import RoundRecap
        from .recaps import generate_recaps

        self._tip_and_settle()
        with self.settings(ANTHROPIC_API_KEY=""), \
             patch("orgs.recaps.generate_recap_text") as gen:
            results = generate_recaps(org=self.org)
        gen.assert_not_called()
        self.assertEqual(results, [])
        self.assertFalse(RoundRecap.objects.filter(org=self.org).exists())

    def test_facts_are_real_results_only(self):
        from .recaps import build_recap_facts

        self._tip_and_settle()
        facts = build_recap_facts(self.org, self.rnd)
        self.assertEqual(facts["group"]["name"], "Recap League")
        self.assertTrue(facts["group"]["first_round_for_group"])
        member = facts["members"][0]
        self.assertEqual(member["name"], "Dave")
        self.assertEqual(member["correct"], 1)
        self.assertTrue(member["perfect_round"])
        self.assertFalse(facts["round"]["is_origin"])

    def test_result_correction_flags_recap_for_review(self):
        from unittest.mock import patch

        from tipping.services import record_match_result

        from .models import RoundRecap
        from .recaps import generate_recaps

        self._tip_and_settle()
        with self.settings(ANTHROPIC_API_KEY="test-key"), \
             patch("orgs.recaps.generate_recap_text", return_value="Dave got the points."):
            generate_recaps(org=self.org)
        recap = RoundRecap.objects.get(org=self.org, round=self.rnd)
        self.assertFalse(recap.needs_review)
        record_match_result(self.match, 10, 30)  # admin corrects the score
        recap.refresh_from_db()
        self.assertTrue(recap.needs_review)
        # Not regenerated, not rewritten — same single post (spec §3).
        self.assertEqual(WallPost.objects.filter(org=self.org, kind="recap").count(), 1)


class PublicWallTests(TestCase):
    """The two-consent rule for public posts, and how outsiders reply."""

    def setUp(self):
        self.season, _ = Season.objects.get_or_create(year=2097, defaults={"label": "2097"})
        self.org = Organisation.objects.create(name="Open Room", season=self.season, is_public_listed=True)
        self.shut = Organisation.objects.create(name="Closed Room", season=self.season, is_public_listed=False)
        self.member = User.objects.create_user(email="m@w.com", password="x", display_name="Dave")
        self.outsider = User.objects.create_user(email="o@w.com", password="x", display_name="Nige")
        OrgMember.objects.create(user=self.member, org=self.org)
        OrgMember.objects.create(user=self.member, org=self.shut)

    def _post(self, org, *, is_public):
        return WallPost.objects.create(
            org=org, author=self.member, body="Backing the Pies", is_public=is_public,
        )

    def test_public_feed_needs_both_consents(self):
        shared = self._post(self.org, is_public=True)
        self._post(self.org, is_public=False)          # author kept it in
        self._post(self.shut, is_public=True)          # group isn't listed
        self.assertEqual(list(WallPost.public_feed()), [shared])

    def test_revoking_group_listing_pulls_posts_immediately(self):
        self._post(self.org, is_public=True)
        self.assertEqual(WallPost.public_feed().count(), 1)
        self.org.is_public_listed = False
        self.org.save(update_fields=["is_public_listed"])
        self.assertEqual(WallPost.public_feed().count(), 0)

    def test_share_toggle_is_ignored_for_an_unlisted_group(self):
        self.client.force_login(self.member)
        self.client.post(
            f"/leagues/{self.shut.id}/wall/post/",
            {"body": "Sneaking this out", "share_public": "1"},
        )
        post = WallPost.objects.get(org=self.shut)
        self.assertFalse(post.is_public)

    def test_composer_defaults_to_group_only(self):
        self.client.force_login(self.member)
        self.client.post(f"/leagues/{self.org.id}/wall/post/", {"body": "Just for us"})
        self.assertFalse(WallPost.objects.get(org=self.org).is_public)

    def test_guest_reply_is_held_for_approval(self):
        from .models import WallReply

        post = self._post(self.org, is_public=True)
        self.client.post(f"/wall/{post.id}/reply/", {
            "body": "Go the Pies", "guest_name": "Sam", "guest_email": "sam@example.com",
        })
        reply = WallReply.objects.get()
        self.assertFalse(reply.is_approved)
        self.assertEqual(reply.guest_email, "sam@example.com")
        # ...and it stays off the public page until a staffer clears it.
        self.assertNotContains(self.client.get("/wall/"), "Go the Pies")
        reply.is_approved = True
        reply.save(update_fields=["is_approved"])
        self.assertContains(self.client.get("/wall/"), "Go the Pies")

    def test_guest_reply_without_an_email_is_refused(self):
        from .models import WallReply

        post = self._post(self.org, is_public=True)
        self.client.post(f"/wall/{post.id}/reply/", {"body": "Anonymous sledge"})
        self.assertEqual(WallReply.objects.count(), 0)

    def test_honeypot_silently_swallows_the_bot(self):
        from .models import WallReply

        post = self._post(self.org, is_public=True)
        resp = self.client.post(f"/wall/{post.id}/reply/", {
            "body": "cheap watches", "guest_email": "bot@spam.com", "website": "http://spam",
        })
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(WallReply.objects.count(), 0)

    def test_group_member_replying_from_the_public_page_goes_straight_up(self):
        from .models import WallReply

        post = self._post(self.org, is_public=True)
        self.client.force_login(self.member)
        self.client.post(f"/wall/{post.id}/reply/", {"body": "My own thread"})
        self.assertTrue(WallReply.objects.get().is_approved)

    def test_signed_in_outsider_still_waits_for_approval(self):
        from .models import WallReply

        post = self._post(self.org, is_public=True)
        self.client.force_login(self.outsider)
        self.client.post(f"/wall/{post.id}/reply/", {"body": "Butting in"})
        reply = WallReply.objects.get()
        self.assertFalse(reply.is_approved)
        self.assertEqual(reply.guest_email, self.outsider.email)

    def test_private_post_cannot_be_replied_to_from_the_public_page(self):
        from .models import WallReply

        private = self._post(self.org, is_public=False)
        resp = self.client.post(f"/wall/{private.id}/reply/", {
            "body": "Prying", "guest_email": "sam@example.com",
        })
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(WallReply.objects.count(), 0)

    def test_private_posts_never_reach_the_public_page(self):
        WallPost.objects.create(org=self.org, author=self.member, body="Secret group business")
        self.assertNotContains(self.client.get("/wall/"), "Secret group business")


class WallReplyNotificationTests(TestCase):
    """Replies are what put something in the bell."""

    def setUp(self):
        self.season, _ = Season.objects.get_or_create(year=2096, defaults={"label": "2096"})
        self.org = Organisation.objects.create(name="Chatty Room", season=self.season)
        self.author = User.objects.create_user(email="a@w.com", password="x", display_name="Ann")
        self.other = User.objects.create_user(email="b@w.com", password="x", display_name="Bob")
        self.third = User.objects.create_user(email="c@w.com", password="x", display_name="Cal")
        for u in (self.author, self.other, self.third):
            OrgMember.objects.create(user=u, org=self.org)
        self.post = WallPost.objects.create(org=self.org, author=self.author, body="Pies by 30")

    def test_reply_notifies_the_post_author(self):
        from .models import Notification

        self.client.force_login(self.other)
        self.client.post(f"/leagues/{self.org.id}/wall/{self.post.id}/reply/", {"body": "Dreaming"})
        note = self.author.notifications.get(kind=Notification.KIND_WALL_REPLY)
        self.assertIn("Bob", note.title)
        self.assertIn(f"#post-{self.post.id}", note.link_url)

    def test_replying_to_yourself_notifies_nobody(self):
        from .models import Notification

        self.client.force_login(self.author)
        self.client.post(f"/leagues/{self.org.id}/wall/{self.post.id}/reply/", {"body": "Adding to this"})
        self.assertEqual(Notification.objects.filter(kind=Notification.KIND_WALL_REPLY).count(), 0)

    def test_later_replies_reach_everyone_in_the_thread(self):
        from .models import Notification

        self.client.force_login(self.other)
        self.client.post(f"/leagues/{self.org.id}/wall/{self.post.id}/reply/", {"body": "Dreaming"})
        self.client.force_login(self.third)
        self.client.post(f"/leagues/{self.org.id}/wall/{self.post.id}/reply/", {"body": "Agreed"})
        self.assertTrue(self.author.notifications.filter(kind=Notification.KIND_WALL_REPLY).exists())
        self.assertTrue(self.other.notifications.filter(kind=Notification.KIND_WALL_REPLY).exists())
        self.assertFalse(self.third.notifications.filter(kind=Notification.KIND_WALL_REPLY).exists())

    def test_new_post_tells_the_rest_of_the_group(self):
        from .models import Notification

        self.client.force_login(self.other)
        self.client.post(f"/leagues/{self.org.id}/wall/post/", {"body": "Anyone watching this?"})
        self.assertTrue(self.author.notifications.filter(kind=Notification.KIND_WALL_POST).exists())
        self.assertTrue(self.third.notifications.filter(kind=Notification.KIND_WALL_POST).exists())
        self.assertFalse(self.other.notifications.filter(kind=Notification.KIND_WALL_POST).exists())

    def test_outsider_cannot_reply_to_a_private_group_post(self):
        from .models import WallReply

        stranger = User.objects.create_user(email="x@w.com", password="x", display_name="Stranger")
        self.client.force_login(stranger)
        resp = self.client.post(f"/leagues/{self.org.id}/wall/{self.post.id}/reply/", {"body": "Nope"})
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(WallReply.objects.count(), 0)

    def test_feed_endpoint_only_returns_what_is_new(self):
        self.client.force_login(self.other)
        self.client.post(f"/leagues/{self.org.id}/wall/{self.post.id}/reply/", {"body": "Dreaming"})
        self.client.force_login(self.author)
        data = self.client.get("/leagues/notifications/feed.json").json()
        self.assertEqual(len(data["items"]), 1)
        self.assertEqual(data["unread"], 1)
        latest = data["latest_id"]
        # Nothing new since — the page shouldn't toast the same thing twice.
        again = self.client.get(f"/leagues/notifications/feed.json?since={latest}").json()
        self.assertEqual(again["items"], [])


class InviteByEmailTests(TestCase):
    """The emailed invite carries the same signed link as the Copy button."""

    def setUp(self):
        from django.urls import reverse

        self.season = Season.objects.create(year=2031)
        self.org = Organisation.objects.create(name="Invite FC", season=self.season)
        self.admin = User.objects.create_user(
            email="boss@example.com", password="Str0ng!pass", display_name="Erick Boss"
        )
        OrgMember.objects.create(
            user=self.admin, org=self.org,
            role=OrgMember.ROLE_MANAGER, is_league_owner=True,
        )
        self.url = reverse("orgs:invite", args=[self.org.id])
        self.client.force_login(self.admin)

    def _post(self, emails, message=""):
        return self.client.post(self.url, {"emails": emails, "message": message})

    def test_sends_one_email_per_address(self):
        from django.core import mail

        self._post("a@example.com, b@example.com")
        self.assertEqual(len(mail.outbox), 2)
        self.assertEqual(
            sorted(m.to[0] for m in mail.outbox), ["a@example.com", "b@example.com"]
        )

    def test_subject_names_the_inviter_and_group(self):
        from django.core import mail

        self._post("a@example.com")
        self.assertEqual(
            mail.outbox[0].subject,
            "Erick Boss invited you to join Invite FC on GoodTip",
        )

    def test_body_carries_a_working_join_link(self):
        from django.core import mail

        from .signing import parse_join_token

        self._post("a@example.com")
        body = mail.outbox[0].body
        token = body.split(f"/join/{self.org.id}/")[1].split("/")[0]
        parsed = parse_join_token(token)
        self.assertEqual(parsed["org_id"], self.org.id)
        self.assertEqual(parsed["inviter_id"], self.admin.pk)

    def test_reply_goes_to_the_inviter(self):
        from django.core import mail

        self._post("a@example.com")
        self.assertEqual(mail.outbox[0].reply_to, ["boss@example.com"])

    def test_optional_note_is_included(self):
        from django.core import mail

        self._post("a@example.com", message="Righto team.")
        self.assertIn("Righto team.", mail.outbox[0].body)

    def test_existing_members_are_skipped(self):
        from django.core import mail

        mate = User.objects.create_user(
            email="mate@example.com", password="Str0ng!pass", display_name="Mate"
        )
        OrgMember.objects.create(user=mate, org=self.org)
        self._post("mate@example.com, fresh@example.com")
        self.assertEqual([m.to[0] for m in mail.outbox], ["fresh@example.com"])

    def test_separators_and_duplicates_are_tolerated(self):
        from django.core import mail

        # Comma, semicolon, newline and space all work; the repeat is dropped.
        self._post("a@example.com; b@example.com\nc@example.com a@example.com")
        self.assertEqual(len(mail.outbox), 3)

    def test_invalid_address_is_reported_and_nothing_sent(self):
        from django.core import mail

        resp = self._post("a@example.com, not-an-email")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("emails", resp.context["email_form"].errors)
        self.assertEqual(len(mail.outbox), 0)

    def test_batch_size_is_capped(self):
        from django.core import mail

        from .forms import InviteByEmailForm

        too_many = ", ".join(
            f"p{n}@example.com" for n in range(InviteByEmailForm.MAX_PER_SEND + 1)
        )
        resp = self._post(too_many)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("emails", resp.context["email_form"].errors)
        self.assertEqual(len(mail.outbox), 0)

    def test_non_managers_cannot_invite(self):
        from django.core import mail

        rando = User.objects.create_user(
            email="rando@example.com", password="Str0ng!pass", display_name="Rando"
        )
        self.client.force_login(rando)
        resp = self._post("a@example.com")
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(len(mail.outbox), 0)


class ProcessNotificationTests(TestCase):
    """Joining and elections are multi-step and slow, so each step reports back
    to the bell panel — not just to email."""

    def setUp(self):
        self.season, _ = Season.objects.get_or_create(year=2097, defaults={"label": "2097"})
        self.org = Organisation.objects.create(name="Waiting Room FC", season=self.season)
        self.admin = User.objects.create_user(email="boss@w.com", password="x", display_name="Bea")
        OrgMember.objects.create(user=self.admin, org=self.org, is_league_owner=True)
        self.joiner = User.objects.create_user(email="new@w.com", password="x", display_name="Nia")

    def test_asking_to_join_tells_both_sides(self):
        from .models import Notification
        from .services import request_to_join

        req = request_to_join(self.joiner, self.org)
        mine = self.joiner.notifications.get(kind=Notification.KIND_JOIN_REQUESTED)
        self.assertIn(self.org.name, mine.title)
        theirs = self.admin.notifications.get(kind=Notification.KIND_JOIN_REVIEW)
        self.assertIn("Nia", theirs.title)
        # Straight to the request itself, where the buttons are.
        self.assertEqual(theirs.link_url, f"/leagues/{self.org.id}/requests/{req.id}/")

    def test_approval_and_decline_both_reach_the_requester(self):
        from .models import Notification
        from .services import (
            approve_membership_request, decline_membership_request, request_to_join,
        )

        req = request_to_join(self.joiner, self.org)
        decline_membership_request(req, by_user=self.admin)
        self.assertTrue(
            self.joiner.notifications.filter(kind=Notification.KIND_JOIN_DECLINED).exists()
        )
        # A declined user may ask again — the second round must notify too.
        again = request_to_join(self.joiner, self.org)
        approve_membership_request(again, by_user=self.admin)
        self.assertTrue(
            self.joiner.notifications.filter(kind=Notification.KIND_JOIN_APPROVED).exists()
        )

    def test_scheduling_an_election_announces_the_date(self):
        from django.utils import timezone

        from .models import CharityVote, Notification
        from .services import schedule_charity_election

        vote = CharityVote.objects.create(org=self.org, status=CharityVote.STATUS_DRAFT)
        schedule_charity_election(vote, when=timezone.now() + timedelta(days=5))
        note = self.admin.notifications.get(kind=Notification.KIND_ELECTION_SCHEDULED)
        self.assertIn("vote", note.title.lower())
        # The date is the whole point of the message.
        self.assertIn("2", note.message)

    def test_an_election_opening_now_does_not_also_announce_a_date(self):
        """Otherwise "it's coming" and "it's open" land in the same second."""
        from django.utils import timezone

        from .models import CharityVote, Notification
        from .services import schedule_charity_election

        vote = CharityVote.objects.create(org=self.org, status=CharityVote.STATUS_DRAFT)
        schedule_charity_election(vote, when=timezone.now() - timedelta(minutes=1))
        self.assertFalse(
            Notification.objects.filter(kind=Notification.KIND_ELECTION_SCHEDULED).exists()
        )
        self.assertTrue(
            Notification.objects.filter(kind=Notification.KIND_ELECTION_OPEN).exists()
        )

    def test_closing_an_election_announces_the_winner(self):
        from django.utils import timezone

        from .models import CharityVote, Notification
        from .services import close_charity_vote, open_charity_election

        charity = Charity.objects.create(name="Beyond Reach")
        vote = CharityVote.objects.create(org=self.org, status=CharityVote.STATUS_DRAFT)
        vote.options.create(charity=charity)
        open_charity_election(vote)
        close_charity_vote(vote)
        note = self.admin.notifications.get(kind=Notification.KIND_ELECTION_RESULT)
        self.assertIn("Beyond Reach", note.title)


class ReviewRequestPageTests(TestCase):
    """The notification links straight to one request, buttons already there."""

    def setUp(self):
        self.season, _ = Season.objects.get_or_create(year=2098, defaults={"label": "2098"})
        self.org = Organisation.objects.create(name="Gatekeepers", season=self.season)
        self.admin = User.objects.create_user(email="ad@w.com", password="x", display_name="Ada")
        OrgMember.objects.create(user=self.admin, org=self.org, is_league_owner=True)
        self.joiner = User.objects.create_user(email="jo@w.com", password="x", display_name="Jo")

    def _request(self):
        from .services import request_to_join

        return request_to_join(self.joiner, self.org)

    def test_the_notification_links_to_the_review_page(self):
        from .models import Notification

        req = self._request()
        note = self.admin.notifications.get(kind=Notification.KIND_JOIN_REVIEW)
        self.assertEqual(note.link_url, f"/leagues/{self.org.id}/requests/{req.id}/")

    def test_admin_can_approve_from_that_page(self):
        req = self._request()
        self.client.force_login(self.admin)
        self.client.post(
            f"/leagues/{self.org.id}/requests/{req.id}/", {"action": "approve"}
        )
        self.assertTrue(OrgMember.objects.filter(user=self.joiner, org=self.org).exists())

    def test_a_non_admin_is_redirected_not_shown_a_blank_403(self):
        req = self._request()
        self.client.force_login(self.joiner)
        r = self.client.get(f"/leagues/{self.org.id}/requests/{req.id}/")
        self.assertEqual(r.status_code, 302)
        self.assertFalse(OrgMember.objects.filter(user=self.joiner, org=self.org).exists())

    def test_an_already_decided_request_still_opens(self):
        """Clicking a stale notification must explain, not crash."""
        from .services import approve_membership_request

        req = self._request()
        approve_membership_request(req, by_user=self.admin)
        self.client.force_login(self.admin)
        r = self.client.get(f"/leagues/{self.org.id}/requests/{req.id}/")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Already", r.content)


class StaffApprovalsPageTests(TestCase):
    """The admin menu's Approvals queue — every pending request in one place."""

    def setUp(self):
        self.season, _ = Season.objects.get_or_create(year=2094, defaults={"label": "2094"})
        self.org = Organisation.objects.create(name="Orphan Group", season=self.season)
        self.staff = User.objects.create_user(
            email="staff@w.com", password="x", display_name="Sam", is_staff=True,
        )
        self.joiner = User.objects.create_user(email="kim@w.com", password="x", display_name="Kim")
        self.req = request_to_join(self.joiner, self.org)

    def test_the_queue_lists_pending_requests(self):
        self.client.force_login(self.staff)
        r = self.client.get("/manage/approvals/")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"kim@w.com", r.content)

    def test_staff_can_approve_a_group_with_no_admin_of_its_own(self):
        """The reason this page exists: nobody else can clear this request."""
        self.client.force_login(self.staff)
        self.client.post(
            "/manage/approvals/", {"action": "approve", "request_id": self.req.id}
        )
        self.assertTrue(OrgMember.objects.filter(user=self.joiner, org=self.org).exists())

    def test_the_nav_carries_the_waiting_count(self):
        self.client.force_login(self.staff)
        self.assertIn(b'class="an-count">1<', self.client.get("/manage/").content)

    def test_a_non_staff_member_cannot_reach_it(self):
        self.client.force_login(self.joiner)
        r = self.client.get("/manage/approvals/")
        self.assertEqual(r.status_code, 302)
        # Bounced to the admin login rather than served the queue.
        self.assertTrue(r["Location"].startswith("/admin/login/"))


class CreateWizardTests(TestCase):
    """Creating a group is four saved steps, resumable after you walk away."""

    URL = "/leagues/new/"

    def setUp(self):
        from catalog.models import Competition, GroupType

        self.season, _ = Season.objects.get_or_create(year=2093, defaults={"label": "2093"})
        self.sport, _ = Sport.objects.get_or_create(name="AFL", defaults={"slug": "afl"})
        self.comp, _ = Competition.objects.get_or_create(
            sport=self.sport, season=self.season, slug="afl", defaults={"name": "AFL"},
        )
        self.gtype = GroupType.objects.get(slug="informal")
        self.charity, _ = Charity.objects.get_or_create(
            slug="lifeline", defaults={"name": "Lifeline", "is_approved": True},
        )
        self.user = User.objects.create_user(email="w@w.com", password="x", display_name="Wiz")
        self.client.force_login(self.user)

    def _step1(self, name="Wizard Group"):
        return self.client.post(self.URL, {
            "step": 1, "action": "next", "name": name,
            "group_type": self.gtype.pk, "informal_label": "Book Club",
        })

    def _step2(self):
        return self.client.post(self.URL, {
            "step": 2, "action": "next",
            "competitions": [self.comp.pk], "season": self.season.pk,
        })

    def _step3(self):
        return self.client.post(self.URL, {
            "step": 3, "action": "next",
            "charity_method": "pick", "charity": self.charity.pk,
        })

    def test_one_step_shows_at_a_time(self):
        body = self.client.get(self.URL).content
        self.assertIn(b'id="id_name"', body)
        self.assertNotIn(b'id="id_season"', body)
        self._step1()
        body = self.client.get(self.URL).content
        self.assertIn(b'id="id_season"', body)
        self.assertNotIn(b'id="id_name"', body)

    def test_a_missing_answer_holds_you_on_that_step(self):
        from .models import OrgDraft

        self.client.post(self.URL, {"step": 1, "action": "next", "name": ""})
        self.assertEqual(OrgDraft.objects.get(user=self.user).step, 1)

    def test_a_later_step_does_not_block_an_earlier_one(self):
        """Step 1 must not fail because no charity has been chosen yet."""
        from .models import OrgDraft

        self._step1()
        self.assertEqual(OrgDraft.objects.get(user=self.user).step, 2)

    def test_progress_survives_a_brand_new_session(self):
        self._step1()
        self._step2()
        self.client.logout()
        self.client.force_login(self.user)
        body = self.client.get(self.URL).content
        # Straight back to step 3, with the earlier answers intact.
        self.assertIn(b"charity_method", body)
        self.assertNotIn(b'id="id_name"', body)

    def test_going_back_keeps_what_was_typed(self):
        self._step1()
        self._step2()
        self.client.post(self.URL, {"step": 3, "action": "back"})
        body = self.client.get(self.URL).content
        self.assertIn(b'id="id_season"', body)
        self.assertIn(str(self.comp.pk).encode(), body)

    def test_the_review_step_reads_the_answers_back(self):
        self._step1()
        self._step2()
        self._step3()
        body = self.client.get(self.URL).content
        self.assertIn(b"Wizard Group", body)
        self.assertIn(b"Lifeline", body)

    def test_finishing_creates_the_group_and_clears_the_draft(self):
        from .models import OrgDraft

        self._step1()
        self._step2()
        self._step3()
        self.client.post(self.URL, {"step": 4, "action": "next"})
        org = Organisation.objects.get(name="Wizard Group")
        self.assertTrue(
            OrgMember.objects.filter(org=org, user=self.user, is_league_owner=True).exists()
        )
        self.assertFalse(OrgDraft.objects.filter(user=self.user).exists())

    def test_start_again_empties_the_draft(self):
        from .models import OrgDraft

        self._step1()
        self.client.post(self.URL, {"step": 2, "action": "restart"})
        draft = OrgDraft.objects.get(user=self.user)
        self.assertEqual(draft.step, 1)
        self.assertEqual(draft.data, {})
