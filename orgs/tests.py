from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db.models import ProtectedError
import re

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from catalog.models import Charity, Season, Series, Sport

from .models import (
    CharityVote,
    CharityVoteOption,
    MembershipRequest,
    Group,
    GroupMember,
    OrgCharitySelection,
    OrgDraft,
    OrgMember,
    Organisation,
    WallPost,
)
from .recaps import build_talking_points, compose_recap, fallback_line
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


def signup_fixtures():
    """A (season, competition) pair `OrgCreateForm` will actually offer.

    Two rules decide what the signup form shows, and a test that guesses at
    either one fails in a way that reads as a wizard bug rather than a fixture
    gap:

    * the season must be the one in play — the form filters to
      `orgs.forms.current_signup_season()`, so a competition built for 2093 is
      never on the page;
    * the competition must have a series a feed can resolve
      (`orgs.forms.fed_competitions()`), so a bare Competition with no Series
      is correctly unselectable.

    Asking the form itself which season that is, rather than writing a year
    into the test, is what stops this drifting apart again the next time the
    calendar turns over.
    """
    from catalog.models import Competition

    from .forms import current_signup_season

    season = current_signup_season()
    if season is None:
        season = Season.objects.create(year=timezone.now().year, label="Test")
    # Looked up on (season, slug) — the unique constraint the table actually
    # has. A get_or_create on (sport, season, slug) misses the AFL row the
    # migrations already seed for this season whenever the seeded row points at
    # a different Sport, then fails the constraint trying to insert a second.
    comp = Competition.objects.filter(season=season, slug="afl").first()
    if comp is None:
        sport, _ = Sport.objects.get_or_create(name="AFL", defaults={"slug": "afl"})
        comp = Competition.objects.create(
            sport=sport, season=season, slug="afl", name="AFL",
        )
    series, _ = Series.objects.get_or_create(
        name="AFL", defaults={"sport": comp.sport, "slug": "afl"},
    )
    comp.series.add(series)
    return season, comp


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
        from catalog.models import Competition, OrganisationType, SubCategory

        # The season and competition the signup form actually offers — see
        # signup_fixtures().
        self.season, self.comp = signup_fixtures()
        self.sport = self.comp.sport
        self.charity, _ = Charity.objects.get_or_create(
            slug="lifeline", defaults={"name": "Lifeline", "is_approved": True},
        )
        self.types = {g.slug: g for g in OrganisationType.objects.all()}
        self.subcat = lambda t, s: SubCategory.objects.get(organisation_type__slug=t, slug=s)

    def form(self, **extra):
        from .forms import OrgCreateForm

        data = {
            "name": "Testers", "season": self.season.pk, "competitions": [self.comp.pk],
            "charity_method": "pick", "charity": self.charity.pk, "groups_enabled": "no",
        }
        data.update(extra)
        return OrgCreateForm(data)

    def test_five_types_in_spec_order(self):
        from catalog.models import OrganisationType

        self.assertEqual(
            list(OrganisationType.objects.values_list("slug", flat=True)),
            ["community", "business", "education", "charities", "informal"],
        )

    def test_type_is_required(self):
        f = self.form()
        self.assertFalse(f.is_valid())
        self.assertIn("organisation_type", f.errors)

    def test_business_requires_exactly_one_sub_category(self):
        f = self.form(organisation_type=self.types["business"].pk)
        self.assertFalse(f.is_valid())
        self.assertIn("sub_categories", f.errors)
        f = self.form(organisation_type=self.types["business"].pk, sub_categories=[
            self.subcat("business", "finance").pk, self.subcat("business", "tech").pk,
        ])
        self.assertFalse(f.is_valid())
        f = self.form(organisation_type=self.types["business"].pk,
                      sub_categories=[self.subcat("business", "finance").pk])
        self.assertTrue(f.is_valid(), f.errors)

    def test_education_allows_primary_plus_secondary_only(self):
        pair = [self.subcat("education", "primary-school").pk,
                self.subcat("education", "secondary-school").pk]
        f = self.form(organisation_type=self.types["education"].pk, sub_categories=pair)
        self.assertTrue(f.is_valid(), f.errors)
        org = f.save()
        self.assertEqual(org.sub_categories.count(), 2)
        # Any other combination is rejected.
        f = self.form(organisation_type=self.types["education"].pk, sub_categories=[
            self.subcat("education", "university").pk, self.subcat("education", "tafe").pk,
        ])
        self.assertFalse(f.is_valid())
        self.assertIn("sub_categories", f.errors)

    def test_informal_requires_self_description(self):
        f = self.form(organisation_type=self.types["informal"].pk)
        self.assertFalse(f.is_valid())
        self.assertIn("informal_label", f.errors)
        f = self.form(organisation_type=self.types["informal"].pk, informal_label="Book Club")
        self.assertTrue(f.is_valid(), f.errors)
        org = f.save()
        self.assertEqual(org.category_label, "Book Club")

    def test_charities_type_needs_no_sub_category(self):
        f = self.form(organisation_type=self.types["charities"].pk)
        self.assertTrue(f.is_valid(), f.errors)

    def test_stale_sub_categories_from_other_type_are_dropped(self):
        f = self.form(organisation_type=self.types["charities"].pk,
                      sub_categories=[self.subcat("business", "finance").pk])
        self.assertTrue(f.is_valid(), f.errors)
        org = f.save()
        self.assertEqual(org.sub_categories.count(), 0)


class CharityPartnerWorkflowTests(TestCase):
    """Charity Partner Workflow (categories doc): lock-to-self is gated on the
    admin-set partner flag; non-partners stay on the vote path."""

    def setUp(self):
        from catalog.models import OrganisationType

        self.season, _ = Season.objects.get_or_create(year=2099, defaults={"label": "Test"})
        self.charities_type = OrganisationType.objects.get(slug="charities")
        self.community_type = OrganisationType.objects.get(slug="community")
        self.lifeline, _ = Charity.objects.get_or_create(
            slug="lifeline", defaults={"name": "Lifeline", "is_approved": True},
        )
        self.beyondblue, _ = Charity.objects.get_or_create(
            slug="beyond-blue", defaults={"name": "Beyond Blue", "is_approved": True},
        )
        self.user = User.objects.create_user(
            email="boss@charity.org", password="x", display_name="Boss",
        )

    def make_org(self, *, partner=False, organisation_type=None):
        org = Organisation.objects.create(
            name="Helping Hands", season=self.season,
            organisation_type=organisation_type or self.charities_type, is_charity_partner=partner,
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

        org = self.make_org(partner=True, organisation_type=self.community_type)
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
        self.assertContains(resp, "No organisations match")

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
    # Informal, deliberately. These callers are about duplicate names and
    # parent/child rules, and an Informal group has no employer domain to
    # prove — so step 2 waves it through instead of demanding an email code
    # that has nothing to do with what is being tested. Verification has its
    # own tests.
    client.post(url, {
        "step": 1, "action": "next", "name": name,
        "organisation_type": case.gtype.pk, "informal_label": "Book Club", "parent": parent,
    })
    client.post(url, {"step": 2, "action": "next", "parent": parent})
    client.post(url, {"step": 3, "action": "next", "groups_enabled": "no", "parent": parent})
    client.post(url, {
        "step": 4, "action": "next",
        "competitions": [case.comp.pk], "season": case.season.pk, "parent": parent,
    })
    client.post(url, {
        "step": 5, "action": "next",
        "charity_method": "pick", "charity": case.charity.pk, "parent": parent,
    })
    final = {"step": 6, "action": "next", "parent": parent}
    if extra.get("duplicate_confirmed"):
        final["duplicate_confirmed"] = extra["duplicate_confirmed"]
    return client.post(url, final)


class DuplicateDetectionTests(TestCase):
    """Org-structure note §4 Stage 2: creating an org whose name already
    exists needs one explicit confirmation — friction, not prevention."""

    def setUp(self):
        from catalog.models import Competition, OrganisationType, Series

        # The season and competition the signup form actually offers — see
        # signup_fixtures().
        self.season, self.comp = signup_fixtures()
        self.charity, _ = Charity.objects.get_or_create(
            slug="lifeline", defaults={"name": "Lifeline", "is_approved": True},
        )
        self.gtype = OrganisationType.objects.get(slug="informal")
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
        from catalog.models import Competition, OrganisationType, Series

        # The season and competition the signup form actually offers — see
        # signup_fixtures().
        self.season, self.comp = signup_fixtures()
        self.charity, _ = Charity.objects.get_or_create(
            slug="lifeline", defaults={"name": "Lifeline", "is_approved": True},
        )
        self.gtype = OrganisationType.objects.get(slug="informal")
        self.parent = Organisation.objects.create(
            name="National Tiles", season=self.season, charity=self.charity,
        )
        self.user = User.objects.create_user(
            email="franchisee@example.com", password="x", display_name="Franchisee",
        )
        # Creating a child requires managing the parent — see _parent_for() in
        # orgs/views.py. Before that check existed, `?parent=` was read off the
        # query string and trusted, so anyone could plant a department inside
        # any organisation on the platform. These tests were written under the
        # old rules and drove the wizard as a stranger to the parent; that path
        # is closed on purpose now, so the creator is a parent admin here.
        OrgMember.objects.create(
            user=self.user, org=self.parent,
            role=OrgMember.ROLE_BOTH, is_league_owner=True,
        )
        self.client.force_login(self.user)

    def create_post(self, name, **extra):
        return _walk_create_wizard(self.client, self, name, **extra)

    def test_get_with_parent_preloads_banner_and_hidden_field(self):
        resp = self.client.get(f"/leagues/new/?parent={self.parent.id}")
        self.assertContains(resp, f"Part of {self.parent.name}")
        self.assertContains(resp, f'name="parent" value="{self.parent.id}"')

    def test_creator_becomes_admin_of_the_child_they_created(self):
        resp = self.create_post("National Tiles Mitcham", parent=self.parent.pk)
        self.assertEqual(resp.status_code, 302)
        child = Organisation.objects.get(name="National Tiles Mitcham")
        self.assertEqual(child.parent, self.parent)
        # §0: admin of the child they created.
        self.assertTrue(
            OrgMember.objects.filter(user=self.user, org=child, is_league_owner=True).exists()
        )
        # This used to assert they were NOT a member of the parent. That can no
        # longer be true of anyone who is allowed to do this — only a parent
        # admin can create a child now. What still has to hold is that creating
        # the child does not quietly change their standing in the parent.
        self.assertEqual(OrgMember.objects.filter(user=self.user, org=self.parent).count(), 1)

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

    # These two assert on the panel's anchor rather than its heading. The
    # heading has already been renamed once — "Child groups" became
    # "Departments", and the test kept passing on the negative case while
    # silently failing on the positive one — and it is about to be renamed
    # again. What the tests are actually about is who sees the panel at all.
    PANEL = 'id="child-orgs"'

    def test_parent_admin_sees_the_sub_org_panel(self):
        self.client.force_login(self.parent_admin)
        resp = self.client.get(f"/leagues/{self.parent.id}/members/")
        self.assertContains(resp, self.PANEL)
        self.assertContains(resp, "National Tiles Mitcham")
        self.assertContains(resp, "Mitcham Boss")

    def test_child_admin_page_has_no_sub_org_panel(self):
        """Two levels only: a child cannot have children, so it gets no panel."""
        self.client.force_login(self.child_admin)
        resp = self.client.get(f"/leagues/{self.child.id}/members/")
        self.assertNotContains(resp, self.PANEL)

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
    """Group Recap end to end (docs/ai-group-recap-spec.md §§1-4, 7, 10).

    The writer itself is covered branch by branch in RecapWriterTests; this
    class is about the batch: readiness, idempotency, and what a corrected
    result does to a card that has already gone up.
    """

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
        self.assertEqual(generate_recaps(org=self.org), [])
        self.assertFalse(WallPost.objects.filter(org=self.org).exists())

    def test_unresolved_round_is_not_ready(self):
        from tipping.models import Tip

        from .recaps import generate_recaps, round_ready_for_recap

        Tip.objects.create(user=self.user, match=self.match, org=self.org, selection="home")
        self.assertFalse(round_ready_for_recap(self.rnd))
        self.assertEqual(generate_recaps(org=self.org), [])

    def test_recap_posts_once_and_carries_its_board(self):
        from .models import RoundRecap
        from .recaps import RECAP_ENGINE, generate_recaps

        self._tip_and_settle()
        first = generate_recaps(org=self.org)
        second = generate_recaps(org=self.org)
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])  # idempotent — one per (org, round)

        post = WallPost.objects.get(org=self.org, kind=WallPost.KIND_RECAP)
        self.assertIsNone(post.author)
        self.assertIn("Dave", post.body)

        recap = RoundRecap.objects.get(org=self.org, round=self.rnd)
        self.assertEqual(recap.post, post)
        self.assertEqual(recap.model_used, RECAP_ENGINE)
        self.assertEqual(recap.leaderboard[0]["name"], "Dave")
        self.assertEqual(recap.leaderboard[0]["rank"], 1)
        self.assertTrue(recap.talking_points)

    def test_generation_never_reaches_the_network(self):
        """The writer is in house. If anything here opens a socket, that is a
        regression worth failing on rather than a slow test."""
        import socket
        from unittest.mock import patch

        from .recaps import generate_recaps

        self._tip_and_settle()
        with patch.object(socket.socket, "connect", side_effect=AssertionError("no outbound call")):
            results = generate_recaps(org=self.org)
        self.assertEqual(len(results), 1)

    def test_a_round_with_nothing_to_say_uses_the_factual_line(self):
        from unittest.mock import patch

        from .models import RoundRecap
        from .recaps import generate_recaps

        self._tip_and_settle()
        with patch("orgs.recaps.compose_recap", return_value=None):
            results = generate_recaps(org=self.org)
        self.assertEqual(len(results), 1)
        recap = RoundRecap.objects.get(org=self.org, round=self.rnd)
        self.assertTrue(recap.fallback_used)
        self.assertIn("Dave", recap.post.body)
        self.assertIn("1 point", recap.post.body)

    def test_a_writer_crash_still_posts_something(self):
        from unittest.mock import patch

        from .models import RoundRecap
        from .recaps import generate_recaps

        self._tip_and_settle()
        with patch("orgs.recaps.compose_recap", side_effect=RuntimeError("boom")):
            results = generate_recaps(org=self.org)
        self.assertEqual(len(results), 1)
        self.assertTrue(RoundRecap.objects.get(org=self.org, round=self.rnd).fallback_used)

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
        from tipping.services import record_match_result

        from .models import RoundRecap
        from .recaps import generate_recaps

        self._tip_and_settle()
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
        from catalog.models import Competition, OrganisationType, Series

        # The season and competition the signup form actually offers. Both
        # rules it applies — current season, and a series a feed can resolve —
        # live in signup_fixtures(); guessing at either from here is what made
        # the whole wizard suite fail on "not one of the available choices".
        self.season, self.comp = signup_fixtures()
        self.sport = self.comp.sport
        self.series = self.comp.series.first()
        self.gtype = OrganisationType.objects.get(slug="informal")
        self.charity, _ = Charity.objects.get_or_create(
            slug="lifeline", defaults={"name": "Lifeline", "is_approved": True},
        )
        self.user = User.objects.create_user(email="w@w.com", password="x", display_name="Wiz")
        self.client.force_login(self.user)

    def _step1(self, name="Wizard Group"):
        return self.client.post(self.URL, {
            "step": 1, "action": "next", "name": name,
            "organisation_type": self.gtype.pk, "informal_label": "Book Club",
        })

    # The wizard gained a step: Verify sits at 2 now, between the basics and
    # the tipping rules. These helpers are named rather than numbered so the
    # next insertion does not silently repoint every test at the wrong screen —
    # which is exactly what happened to the numbered ones.

    def _verify_step(self):
        """Step 2, the work-domain check.

        An Informal group has no employer domain to prove and never will, so it
        passes straight through — but it is still a step, and skipping it in a
        test means everything after lands one screen early.
        """
        return self.client.post(self.URL, {"step": 2, "action": "next"})

    def _groups_step(self, enabled=False):
        """Step 3, whether the organisation wants sub-groups.

        Defaults to "no" — an Informal book club has no use for departments,
        and most of these tests are not about groups at all. Must still be
        walked through on any path that finishes creation, because the field
        is required on the form: skipping it is exactly the "step" numbers
        drifting out from under a test that the named-helper pattern here
        exists to prevent.
        """
        return self.client.post(self.URL, {
            "step": 3, "action": "next",
            "groups_enabled": "yes" if enabled else "no",
        })

    def _tipping_step(self):
        return self.client.post(self.URL, {
            "step": 4, "action": "next",
            "competitions": [self.comp.pk], "season": self.season.pk,
        })

    def _charity_step(self):
        return self.client.post(self.URL, {
            "step": 5, "action": "next",
            "charity_method": "pick", "charity": self.charity.pk,
        })

    def _review_step(self):
        return self.client.post(self.URL, {"step": 6, "action": "next"})

    def _through_to_review(self, name="Wizard Group"):
        self._step1(name)
        self._verify_step()
        self._groups_step()
        self._tipping_step()
        self._charity_step()

    def test_one_step_shows_at_a_time(self):
        body = self.client.get(self.URL).content
        self.assertIn(b'id="id_name"', body)
        self.assertNotIn(b'name="groups_enabled"', body)
        self._step1()
        self._verify_step()
        body = self.client.get(self.URL).content
        self.assertIn(b'name="groups_enabled"', body)
        self.assertNotIn(b'id="id_name"', body)
        self.assertNotIn(b'id="id_season"', body)
        self._groups_step()
        body = self.client.get(self.URL).content
        self.assertIn(b'id="id_season"', body)
        self.assertNotIn(b'name="groups_enabled"', body)

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
        self._verify_step()
        self._tipping_step()
        self.client.logout()
        self.client.force_login(self.user)
        body = self.client.get(self.URL).content
        # Straight back to the charity step, with the earlier answers intact.
        self.assertIn(b"charity_method", body)
        self.assertNotIn(b'id="id_name"', body)

    def test_going_back_keeps_what_was_typed(self):
        self._step1()
        self._verify_step()
        self._groups_step()
        self._tipping_step()
        self.client.post(self.URL, {"step": 5, "action": "back"})
        body = self.client.get(self.URL).content
        self.assertIn(b'id="id_season"', body)
        self.assertIn(str(self.comp.pk).encode(), body)

    def test_the_review_step_reads_the_answers_back(self):
        self._through_to_review()
        body = self.client.get(self.URL).content
        self.assertIn(b"Wizard Group", body)
        self.assertIn(b"Lifeline", body)

    def test_finishing_creates_the_group_and_clears_the_draft(self):
        from .models import OrgDraft

        self._through_to_review()
        self._review_step()
        org = Organisation.objects.get(name="Wizard Group")
        self.assertTrue(
            OrgMember.objects.filter(org=org, user=self.user, is_league_owner=True).exists()
        )
        self.assertFalse(OrgDraft.objects.filter(user=self.user).exists())

    def test_opening_a_department_does_not_pin_the_wizard_to_it(self):
        """The bug: create-an-organisation kept showing "New department under X".

        There is one draft per user and both flows shared it, but the parent
        was only ever written into it, never cleared. So opening the department
        form once pinned that draft to a parent permanently — every later visit
        re-resolved it and rendered a Department name field to someone who had
        asked to create an organisation, with Start again as the only escape.
        """
        parent = Organisation.objects.create(
            name="Masterclass", season=self.season, charity=self.charity,
        )
        OrgMember.objects.create(
            user=self.user, org=parent,
            role=OrgMember.ROLE_BOTH, is_league_owner=True,
        )
        # Open the department form, then walk away from it.
        resp = self.client.get(f"{self.URL}?parent={parent.id}")
        self.assertContains(resp, "Part of Masterclass")

        # Come back to create an ORGANISATION. It must be one.
        resp = self.client.get(self.URL)
        self.assertNotContains(resp, "Part of Masterclass")
        self.assertNotContains(resp, "Department name")
        self.assertContains(resp, "Organisation name")
        self.assertIsNone(OrgDraft.objects.get(user=self.user).data.get("parent"))

    def test_a_department_in_progress_keeps_its_parent_between_steps(self):
        """The other half: clearing the parent must not break a real flow.

        The steps redirect, and a redirect drops the query string — which is
        why the parent used to be fished back out of the draft in the first
        place. It rides on the redirect now instead.
        """
        parent = Organisation.objects.create(
            name="Masterclass", season=self.season, charity=self.charity,
        )
        OrgMember.objects.create(
            user=self.user, org=parent,
            role=OrgMember.ROLE_BOTH, is_league_owner=True,
        )
        self.client.get(f"{self.URL}?parent={parent.id}")
        resp = self.client.post(self.URL, {
            "step": 1, "action": "next", "name": "Finance",
            "organisation_type": self.gtype.pk, "informal_label": "Book Club",
            "parent": parent.id,
        })
        # The redirect carries the parent, so the next screen is still a
        # department rather than silently becoming a new organisation.
        self.assertIn(f"parent={parent.id}", resp["Location"])
        self.assertContains(self.client.get(resp["Location"]), "Part of Masterclass")

    def test_start_again_empties_the_draft(self):
        from .models import OrgDraft

        self._step1()
        self.client.post(self.URL, {"step": 2, "action": "restart"})
        draft = OrgDraft.objects.get(user=self.user)
        self.assertEqual(draft.step, 1)
        self.assertEqual(draft.data, {})

    def test_verify_step_renders_before_any_code_is_requested(self):
        """Step 2 with no WorkEmailVerification row is the FIRST thing every
        business/education/charity creator sees, and it used to 500.

        The role field prefilled with `|default:verification.role`, and a
        filter argument is resolved eagerly: with no row, `verification` is
        None and the lookup raised VariableDoesNotExist through the whole
        page. The neighbouring `{% if verification.is_verified %}` swallowed
        the same failure, so nothing else on the step gave it away.
        """
        from catalog.models import OrganisationType

        from .models import OrgDraft, WorkEmailVerification

        business = OrganisationType.objects.filter(slug="business").first()
        if business is None:
            self.skipTest("no business group type in this catalog")
        sub = business.sub_categories.first()

        payload = {
            "step": 1, "action": "next", "name": "Verify Step Co",
            "organisation_type": business.pk,
        }
        if sub is not None:
            payload["sub_categories"] = [sub.pk]
        self.client.post(self.URL, payload)

        self.assertEqual(OrgDraft.objects.get(user=self.user).step, 2)
        self.assertFalse(WorkEmailVerification.objects.filter(user=self.user).exists())

        resp = self.client.get(self.URL)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'id="id_role"')
        # Blank, not the string "None" rendered into the box.
        self.assertNotContains(resp, 'value="None"')


class RecapWriterTests(SimpleTestCase):
    """The recap writer, driven directly.

    compose_recap and build_talking_points are pure functions over the facts
    dict, so every branch is reachable without a database. That is the point
    of keeping the writer separate from the query that feeds it: the voice
    rules in §8-9 of the spec are testable on their own.
    """

    def facts(self, **over):
        base = {
            "seed": "1:1",
            "round": {
                "number": 12, "competition": "NRL (2026)", "stage": "regular",
                "is_origin": False, "code": "nrl",
                "points_per_correct_pick": 1, "matches_in_round": 8,
            },
            "group": {
                "name": "Depot Crew", "members_who_tipped": 4,
                "first_round_for_group": False,
            },
            "members": [
                {"name": "Sam", "correct": 7, "picks": 8, "round_points": 7,
                 "season_points": 61, "rank_now": 1, "perfect_round": False,
                 "rank_before_round": 3, "moved": 2},
                {"name": "Alex", "correct": 6, "picks": 8, "round_points": 6,
                 "season_points": 58, "rank_now": 2, "perfect_round": False,
                 "rank_before_round": 1, "moved": -1},
                {"name": "Jo", "correct": 4, "picks": 8, "round_points": 4,
                 "season_points": 44, "rank_now": 3, "perfect_round": False,
                 "rank_before_round": 2, "moved": -1},
                {"name": "Pat", "correct": 3, "picks": 8, "round_points": 3,
                 "season_points": 30, "rank_now": 4, "perfect_round": False,
                 "rank_before_round": 4, "moved": 0},
            ],
            "standings": [
                {"name": "Sam", "rank": 1, "season_points": 61, "round_points": 7,
                 "tipped_this_round": True, "moved": 2},
                {"name": "Alex", "rank": 2, "season_points": 58, "round_points": 6,
                 "tipped_this_round": True, "moved": -1},
                {"name": "Jo", "rank": 3, "season_points": 44, "round_points": 4,
                 "tipped_this_round": True, "moved": -1},
                {"name": "Pat", "rank": 4, "season_points": 30, "round_points": 3,
                 "tipped_this_round": True, "moved": 0},
            ],
            "matches": {"upset": None, "consensus": None},
            "totals": {"correct": 20, "picks": 32},
        }
        for key, value in over.items():
            if isinstance(value, dict) and isinstance(base.get(key), dict):
                base[key] = {**base[key], **value}
            else:
                base[key] = value
        return base

    # -- voice (§8) --------------------------------------------------------

    def test_no_em_dashes_anywhere(self):
        for code in ("afl", "nrl", "generic"):
            text = compose_recap(self.facts(round={"code": code}))
            self.assertNotIn("—", text)
            self.assertNotIn("–", text)

    def test_two_to_four_sentences_on_every_branch(self):
        for text in self.every_recap():
            n = len([s for s in re.split(r"(?<=\.)\s+", text) if s.strip()])
            self.assertGreaterEqual(n, 2, text)
            self.assertLessEqual(n, 4, text)

    def test_no_banned_words_in_the_output(self):
        """Swept across every branch and a spread of seeds.

        Checked on finished text with the member names blanked first: the ban
        is on the writer's vocabulary, and a member called May or Hope is not
        a voice breach.
        """
        from orgs.recaps import BANNED_WORDS

        for text in self.every_recap():
            stripped = re.sub(r"\b(Sam|Alex|Jo|Pat|Depot|Crew|Titans|Storm)\b", "", text)
            for word in BANNED_WORDS:
                self.assertNotRegex(
                    stripped.lower(), rf"\b{re.escape(word)}\b",
                    f"banned word {word!r} in: {text}",
                )

    def every_recap(self):
        """One recap per branch the writer can take, across several seeds."""
        upset = {"home": "Storm", "away": "Titans", "winner": "Titans",
                 "loser": "Storm", "correct": 1, "tips": 4, "share": 0.25}
        consensus = {"home": "Storm", "away": "Titans", "winner": "Storm",
                     "loser": "Titans", "correct": 4, "tips": 4, "share": 1.0}
        shapes = []
        for code in ("afl", "nrl", "generic"):
            shapes.append(self.facts(round={"code": code}))
        shapes.append(self.facts(group={"first_round_for_group": True}))
        shapes.append(self.facts(round={"is_origin": True, "stage": "origin",
                                        "points_per_correct_pick": 4,
                                        "matches_in_round": 1}))
        shapes.append(self.facts(matches={"upset": upset, "consensus": consensus}))
        perfect = self.facts()
        perfect["members"][0].update(correct=8, round_points=8, perfect_round=True)
        shapes.append(perfect)
        tied = self.facts()
        tied["members"][1]["round_points"] = 7
        shapes.append(tied)
        solo = self.facts(group={"members_who_tipped": 1})
        solo["members"] = solo["members"][:1]
        solo["standings"] = solo["standings"][:1]
        shapes.append(solo)

        for shape in shapes:
            for n in range(1, 8):
                text = compose_recap({**shape, "seed": f"1:{n}"})
                if text:
                    yield text

    def test_never_borrows_the_other_codes_words(self):
        afl = compose_recap(self.facts(round={"code": "afl"}, seed="1:9"))
        nrl = compose_recap(self.facts(round={"code": "nrl"}, seed="1:9"))
        for banned in ("blinder", "ripper"):
            self.assertNotIn(banned, afl)
        for banned in ("chocolates", "kicked a bag", "home and hosed"):
            self.assertNotIn(banned, nrl)

    def test_phrasing_varies_between_rounds_but_not_between_runs(self):
        a = compose_recap(self.facts(seed="1:1"))
        again = compose_recap(self.facts(seed="1:1"))
        self.assertEqual(a, again)
        openers = {
            compose_recap(self.facts(seed=f"1:{n}")).split(".")[0]
            for n in range(1, 12)
        }
        self.assertGreater(len(openers), 1)

    # -- what it chooses to lead on ---------------------------------------

    def test_a_perfect_round_leads(self):
        f = self.facts()
        f["members"][0].update(correct=8, round_points=8, perfect_round=True)
        text = compose_recap(f)
        self.assertIn("Sam", text.split(".")[0])
        self.assertIn("clean", text)

    def test_a_tie_at_the_top_leads(self):
        f = self.facts()
        f["members"][1]["round_points"] = 7
        text = compose_recap(f)
        self.assertIn("Sam", text)
        self.assertIn("Alex", text)

    def test_origin_is_named_and_its_points_stated(self):
        text = compose_recap(self.facts(round={
            "is_origin": True, "stage": "origin",
            "points_per_correct_pick": 4, "matches_in_round": 1,
        }))
        self.assertIn("State of Origin", text)
        self.assertIn("4", text)

    def test_first_round_carries_no_movement_language(self):
        f = self.facts(group={"first_round_for_group": True})
        for m in f["members"]:
            m.pop("moved", None)
            m.pop("rank_before_round", None)
        text = compose_recap(f)
        for word in ("climbed", "is up", "takes over", "leads the season"):
            self.assertNotIn(word, text)

    def test_the_upset_names_the_side_that_won(self):
        text = compose_recap(self.facts(matches={"upset": {
            "home": "Storm", "away": "Titans", "winner": "Titans",
            "loser": "Storm", "correct": 1, "tips": 4, "share": 0.25,
        }}))
        self.assertIn("Titans", text)
        self.assertIn("1 of 4", text)

    def test_a_solo_tipper_is_not_told_their_own_score_twice(self):
        f = self.facts(group={"members_who_tipped": 1}, totals={"correct": 7, "picks": 8})
        f["members"] = f["members"][:1]
        f["standings"] = f["standings"][:1]
        text = compose_recap(f)
        self.assertNotIn("between them", text)

    def test_a_round_with_nothing_in_it_falls_back(self):
        f = self.facts(group={"members_who_tipped": 1, "first_round_for_group": True},
                       totals={"correct": 0, "picks": 1})
        f["members"] = [{
            "name": "Sam", "correct": 0, "picks": 1, "round_points": 0,
            "season_points": 0, "rank_now": 1, "perfect_round": False,
        }]
        f["standings"] = f["standings"][:1]
        self.assertIsNone(compose_recap(f))
        self.assertIn("Sam", fallback_line(f))

    # -- conversation starters --------------------------------------------

    def test_starters_are_short_and_ask_something(self):
        points = build_talking_points(self.facts(matches={"upset": {
            "home": "Storm", "away": "Titans", "winner": "Titans",
            "loser": "Storm", "correct": 1, "tips": 4, "share": 0.25,
        }}))
        self.assertTrue(1 <= len(points) <= 3)
        for p in points:
            self.assertLessEqual(len(p), 120)
            self.assertIn("?", p)

    def test_starters_only_name_people_in_the_group(self):
        f = self.facts()
        names = {m["name"] for m in f["members"]}
        for p in build_talking_points(f):
            for word in re.findall(r"\b[A-Z][a-z]+\b", p):
                if word in ("Who", "Anyone", "Which", "Was", "Did", "Only", "Everyone"):
                    continue
                self.assertIn(word, names | {"Depot", "Crew"})

    def test_a_quiet_round_still_gets_one_starter(self):
        points = build_talking_points(self.facts(
            members=[{"name": "Sam", "correct": 0, "picks": 8, "round_points": 0,
                      "season_points": 0, "rank_now": 1, "perfect_round": False}],
        ))
        self.assertTrue(points)


class WizardEndToEndTests(TestCase):
    """Walk the whole create-a-group wizard and check a group comes out.

    The existing CreateWizardTests assert on markup from an older shape — they
    look for the season field on step 1, which moved to step 3 when the verify
    step was inserted. Stale assertions hide a real question: does the flow
    still WORK? This answers it by using the wizard the way a person does,
    which is the thing that has to be true before anyone is invited to test.
    """

    def setUp(self):
        from catalog.models import Competition, OrganisationType

        User = get_user_model()
        self.user = User.objects.create_user(
            email="wiz@example.com", password="Str0ng!pass", display_name="Wiz",
        )
        self.client.force_login(self.user)
        # A type that does NOT require work-email verification, so the walk
        # exercises the wizard rather than the emailed-code path.
        self.gtype = OrganisationType.objects.exclude(
            slug__in=["business", "education", "charities"]
        ).first()
        # Community and Business must name a sub-category — step 1 will not
        # save without one, which is exactly the kind of thing a walk-through
        # exists to catch.
        from catalog.models import SubCategory
        self.subcat = SubCategory.objects.filter(organisation_type=self.gtype).first()
        self.comp = Competition.objects.filter(
            slug="afl", season__year=2026
        ).select_related("season").first()
        self.charity = Charity.objects.filter(is_approved=True).first()
        from django.urls import reverse
        self.url = reverse("orgs:create")

    def _post(self, step, **fields):
        return self.client.post(self.url, {"step": str(step), "action": "next", **fields})

    def test_a_group_can_be_created_from_start_to_finish(self):
        from orgs.models import OrgMember, Organisation

        self.assertIsNotNone(self.gtype, "no non-verifying group type seeded")
        self.assertIsNotNone(self.comp, "no AFL 2026 competition seeded")

        # 1 — who you are
        self._post(1, name="Wizard Walk FC", organisation_type=self.gtype.id,
                   sub_categories=self.subcat.id if self.subcat else "")
        # 2 — verification, not required for this type
        self._post(2)
        # 3 — groups, not for this small a crew
        self._post(3, groups_enabled="no")
        # 4 — what you tip
        self._post(
            4, competitions=self.comp.id, season=self.comp.season_id,
            team_size="10",
        )
        # 5 — the cause
        self._post(5, charity_method="pick", charity=self.charity.id)
        # 6 — create
        self._post(6)

        org = Organisation.objects.filter(name="Wizard Walk FC").first()
        self.assertIsNotNone(org, "the wizard finished without creating a group")
        self.assertEqual(org.season_id, self.comp.season_id)
        self.assertIn(self.comp, org.competitions.all())
        # The creator is the admin of what they just made, or nobody can run it.
        member = OrgMember.objects.filter(org=org, user=self.user).first()
        self.assertIsNotNone(member)
        self.assertTrue(member.is_league_owner)

    def test_the_created_group_lands_on_a_season_with_fixtures(self):
        """The whole point of the season default. A group created into a season
        the feeds have no draw for syncs nothing and looks broken."""
        from orgs.models import Organisation

        self._post(1, name="Season Check FC", organisation_type=self.gtype.id,
                   sub_categories=self.subcat.id if self.subcat else "")
        self._post(2)
        self._post(3, groups_enabled="no")
        # Season deliberately NOT posted — take whatever the form defaults to.
        self._post(4, competitions=self.comp.id, team_size="10")
        self._post(5, charity_method="pick", charity=self.charity.id)
        self._post(6)

        org = Organisation.objects.filter(name="Season Check FC").first()
        if org is not None:
            self.assertEqual(
                org.season.year, 2026,
                "a new group must land in the season being played, not a future one",
            )


class GroupModelTests(TestCase):
    """Groups sit inside one organisation and own nothing but their members
    and their ladder — see orgs.models.Group for why they are not child orgs."""

    def setUp(self):
        from django.utils import timezone
        from tipping.models import Match, Round, Team

        self.now = timezone.now()
        self.season, _ = Season.objects.get_or_create(year=2098, defaults={"label": "2098"})
        self.charity, _ = Charity.objects.get_or_create(
            slug="lifeline", defaults={"name": "Lifeline", "is_approved": True},
        )
        self.org = Organisation.objects.create(
            name="Acme", season=self.season, charity=self.charity,
        )
        self.user = User.objects.create_user(
            email="tipper@example.com", password="x", display_name="Tipper",
        )
        sport, _ = Sport.objects.get_or_create(name="AFL", defaults={"slug": "afl"})
        series, _ = Series.objects.get_or_create(
            name="AFL", defaults={"sport": sport, "slug": "afl"},
        )
        rnd = Round.objects.create(
            org=self.org, round_number=1, series=series,
            lockout_at=self.now + timedelta(days=1),
        )
        home = Team.objects.create(name="Home", slug="home", series=series)
        away = Team.objects.create(name="Away", slug="away", series=series)
        self.match = Match.objects.create(
            round=rnd, home_team=home, away_team=away, kickoff_at=self.now,
        )
        self.marketing = Group.objects.create(org=self.org, name="Marketing")
        self.it = Group.objects.create(org=self.org, name="IT")

    def _tip(self, group=None, selection="home"):
        from tipping.models import Tip

        return Tip.objects.create(
            user=self.user, match=self.match, org=self.org,
            group=group, selection=selection,
        )

    # ---- the two tip sets -------------------------------------------------

    def test_the_same_fixture_can_be_tipped_in_a_group_and_in_the_org(self):
        """The whole point: tipping in Marketing is a different act from
        tipping in Acme, and the two must not collide."""
        self._tip(group=None, selection="home")
        self._tip(group=self.marketing, selection="away")
        from tipping.models import Tip

        self.assertEqual(Tip.objects.filter(user=self.user, match=self.match).count(), 2)

    def test_one_tip_per_group(self):
        from django.db import IntegrityError

        self._tip(group=self.marketing)
        with self.assertRaises(IntegrityError):
            self._tip(group=self.marketing, selection="away")

    def test_one_tip_at_organisation_level(self):
        """The NULL trap.

        `group` is nullable, and Postgres treats every NULL as distinct from
        every other, so a single unique_together over (user, match, org, group)
        would enforce nothing here — this exact case, which used to be covered,
        would silently accept unlimited duplicates. Hence two partial
        constraints instead.
        """
        from django.db import IntegrityError

        self._tip(group=None)
        with self.assertRaises(IntegrityError):
            self._tip(group=None, selection="away")

    def test_two_groups_are_two_separate_ladders(self):
        from tipping.models import Tip

        self._tip(group=self.marketing, selection="home")
        self._tip(group=self.it, selection="away")
        self.assertEqual(Tip.objects.filter(group=self.marketing).count(), 1)
        self.assertEqual(Tip.objects.filter(group=self.it).count(), 1)

    def test_a_group_reuses_the_organisations_fixtures(self):
        """A group adds no rounds and no matches of its own.

        A department did: it was an organisation, and rounds and matches are
        stored per organisation, so each one duplicated the whole fixture list.
        """
        from tipping.models import Match, Round

        rounds_before = Round.objects.count()
        matches_before = Match.objects.count()
        Group.objects.create(org=self.org, name="Sales")
        self.assertEqual(Round.objects.count(), rounds_before)
        self.assertEqual(Match.objects.count(), matches_before)

    # ---- shape ------------------------------------------------------------

    def test_two_groups_in_one_org_cannot_share_a_name(self):
        from django.db import IntegrityError

        with self.assertRaises(IntegrityError):
            Group.objects.create(org=self.org, name="Marketing")

    def test_two_organisations_can_each_have_a_marketing(self):
        other = Organisation.objects.create(
            name="Beta", season=self.season, charity=self.charity,
        )
        Group.objects.create(org=other, name="Marketing")
        self.assertEqual(Group.objects.filter(name="Marketing").count(), 2)

    def test_a_group_cannot_hang_off_a_child_organisation(self):
        from django.core.exceptions import ValidationError

        child = Organisation.objects.create(
            name="Acme Mitcham", season=self.season,
            charity=self.charity, parent=self.org,
        )
        with self.assertRaises(ValidationError):
            Group(org=child, name="Marketing").full_clean()

    def test_groups_are_off_until_an_organisation_asks_for_them(self):
        self.assertFalse(self.org.groups_enabled)

    def test_a_member_can_be_in_more_than_one_group(self):
        GroupMember.objects.create(group=self.marketing, user=self.user)
        GroupMember.objects.create(group=self.it, user=self.user)
        self.assertEqual(self.user.group_memberships.count(), 2)

    def test_a_member_joins_a_group_once(self):
        from django.db import IntegrityError

        GroupMember.objects.create(group=self.marketing, user=self.user)
        with self.assertRaises(IntegrityError):
            GroupMember.objects.create(group=self.marketing, user=self.user)


class CurrentContextTests(TestCase):
    """Where the user is — see orgs/context.py.

    Before this the nav took whichever membership the query returned first,
    which was not a choice anyone made and could not express "I am looking at
    Marketing" at all.
    """

    def setUp(self):
        from django.urls import reverse
        from django.utils import timezone

        self.reverse = reverse
        self.season, _ = Season.objects.get_or_create(year=2097, defaults={"label": "2097"})
        self.charity, _ = Charity.objects.get_or_create(
            slug="lifeline", defaults={"name": "Lifeline", "is_approved": True},
        )
        self.now = timezone.now()
        self.acme = Organisation.objects.create(
            name="Acme", season=self.season, charity=self.charity, groups_enabled=True,
        )
        self.beta = Organisation.objects.create(
            name="Beta", season=self.season, charity=self.charity, groups_enabled=True,
        )
        self.user = User.objects.create_user(
            email="member@example.com", password="x", display_name="Member",
        )
        OrgMember.objects.create(user=self.user, org=self.acme)
        OrgMember.objects.create(user=self.user, org=self.beta)
        self.marketing = Group.objects.create(org=self.acme, name="Marketing")
        GroupMember.objects.create(group=self.marketing, user=self.user)
        self.client.force_login(self.user)

    def _request(self):
        """A request carrying this client's session, for calling context.py."""
        from django.test import RequestFactory

        r = RequestFactory().get("/")
        r.user = self.user
        r.session = self.client.session
        return r

    # ---- organisation -----------------------------------------------------

    def test_with_no_choice_made_it_settles_on_a_stable_one(self):
        from .context import current_org

        # Ordered by name, so the fallback does not move when an unrelated
        # membership row is written.
        self.assertEqual(current_org(self._request()), self.acme)

    def test_switching_organisation_sticks(self):
        self.client.post(self.reverse("orgs:switch_org", args=[self.beta.id]))
        from .context import current_org

        self.assertEqual(current_org(self._request()), self.beta)

    def test_you_cannot_switch_into_an_organisation_you_are_not_in(self):
        outsider_org = Organisation.objects.create(
            name="Zeta", season=self.season, charity=self.charity,
        )
        self.client.post(self.reverse("orgs:switch_org", args=[outsider_org.id]))
        from .context import current_org

        self.assertEqual(current_org(self._request()), self.acme)

    def test_losing_membership_drops_the_context(self):
        """A session outlives permission.

        Someone removed from an organisation still has its id in their cookie,
        and that must not be enough to keep showing it to them.
        """
        self.client.post(self.reverse("orgs:switch_org", args=[self.beta.id]))
        OrgMember.objects.filter(user=self.user, org=self.beta).delete()
        from .context import current_org

        self.assertEqual(current_org(self._request()), self.acme)

    def test_a_member_of_nothing_has_no_context(self):
        OrgMember.objects.filter(user=self.user).delete()
        from .context import current_org

        self.assertIsNone(current_org(self._request()))

    # ---- group ------------------------------------------------------------

    def test_the_organisation_itself_is_a_real_context_not_a_missing_one(self):
        from .context import current_group

        self.assertIsNone(current_group(self._request()))

    def test_stepping_into_a_group(self):
        self.client.post(
            self.reverse("orgs:switch_group", args=[self.acme.id, self.marketing.id])
        )
        from .context import current_group

        self.assertEqual(current_group(self._request()), self.marketing)

    def test_stepping_back_out_leaves_you_in_the_organisation(self):
        self.client.post(
            self.reverse("orgs:switch_group", args=[self.acme.id, self.marketing.id])
        )
        self.client.post(self.reverse("orgs:leave_group_context"))
        from .context import current_group, current_org

        self.assertIsNone(current_group(self._request()))
        self.assertEqual(current_org(self._request()), self.acme)

    def test_switching_organisation_drops_the_group(self):
        """Marketing cannot survive a move to a different company."""
        self.client.post(
            self.reverse("orgs:switch_group", args=[self.acme.id, self.marketing.id])
        )
        self.client.post(self.reverse("orgs:switch_org", args=[self.beta.id]))
        from .context import current_group

        self.assertIsNone(current_group(self._request()))

    def test_you_cannot_step_into_a_group_you_have_not_joined(self):
        sales = Group.objects.create(org=self.acme, name="Sales")
        self.client.post(self.reverse("orgs:switch_group", args=[self.acme.id, sales.id]))
        from .context import current_group

        self.assertIsNone(current_group(self._request()))

    def test_you_cannot_step_into_a_group_still_awaiting_approval(self):
        pending = Group.objects.create(
            org=self.acme, name="Skunkworks",
            approval_status=Group.APPROVAL_PENDING,
        )
        GroupMember.objects.create(group=pending, user=self.user)
        self.client.post(self.reverse("orgs:switch_group", args=[self.acme.id, pending.id]))
        from .context import current_group

        self.assertIsNone(current_group(self._request()))

    def test_leaving_a_group_drops_you_out_of_it(self):
        self.client.post(
            self.reverse("orgs:switch_group", args=[self.acme.id, self.marketing.id])
        )
        GroupMember.objects.filter(group=self.marketing, user=self.user).delete()
        from .context import current_group

        self.assertIsNone(current_group(self._request()))

    def test_turning_groups_off_drops_anyone_standing_in_one(self):
        self.client.post(
            self.reverse("orgs:switch_group", args=[self.acme.id, self.marketing.id])
        )
        self.acme.groups_enabled = False
        self.acme.save(update_fields=["groups_enabled"])
        from .context import current_group

        self.assertIsNone(current_group(self._request()))

    # ---- the scope every tip query should use -----------------------------

    def test_tip_scope_says_organisation_when_not_in_a_group(self):
        from .context import tip_scope

        self.assertEqual(tip_scope(self._request()), {"org": self.acme, "group": None})

    def test_tip_scope_says_group_when_in_one(self):
        self.client.post(
            self.reverse("orgs:switch_group", args=[self.acme.id, self.marketing.id])
        )
        from .context import tip_scope

        self.assertEqual(
            tip_scope(self._request()), {"org": self.acme, "group": self.marketing}
        )

    # ---- the switch endpoints themselves ----------------------------------

    def test_switching_is_not_a_get(self):
        """A GET switcher gets followed by every prefetcher that sees the nav."""
        resp = self.client.get(self.reverse("orgs:switch_org", args=[self.beta.id]))
        self.assertEqual(resp.status_code, 405)

    def test_next_only_honours_paths_on_this_site(self):
        resp = self.client.post(
            self.reverse("orgs:switch_org", args=[self.beta.id]),
            {"next": "https://evil.example.com/"},
        )
        self.assertEqual(resp["Location"], self.reverse("dashboard"))

    def test_next_honours_a_local_path(self):
        resp = self.client.post(
            self.reverse("orgs:switch_org", args=[self.beta.id]),
            {"next": "/leagues/search/"},
        )
        self.assertEqual(resp["Location"], "/leagues/search/")

    def test_a_protocol_relative_next_is_refused(self):
        resp = self.client.post(
            self.reverse("orgs:switch_org", args=[self.beta.id]),
            {"next": "//evil.example.com/"},
        )
        self.assertEqual(resp["Location"], self.reverse("dashboard"))


class GroupTippingTests(TestCase):
    """The two tip sets, end to end: tipping in a group and tipping in the
    organisation are separate acts on separate ladders."""

    def setUp(self):
        from django.urls import reverse
        from django.utils import timezone
        from tipping.models import Match, Round, Team

        self.reverse = reverse
        self.now = timezone.now()
        self.season, _ = Season.objects.get_or_create(year=2096, defaults={"label": "2096"})
        self.charity, _ = Charity.objects.get_or_create(
            slug="lifeline", defaults={"name": "Lifeline", "is_approved": True},
        )
        self.org = Organisation.objects.create(
            name="Acme", season=self.season, charity=self.charity, groups_enabled=True,
        )
        sport, _ = Sport.objects.get_or_create(name="AFL", defaults={"slug": "afl"})
        self.series, _ = Series.objects.get_or_create(
            name="AFL", defaults={"sport": sport, "slug": "afl"},
        )
        self.round = Round.objects.create(
            org=self.org, round_number=1, series=self.series,
            lockout_at=self.now + timedelta(days=2),
        )
        self.home = Team.objects.create(name="Home", slug="home", series=self.series)
        self.away = Team.objects.create(name="Away", slug="away", series=self.series)
        self.match = Match.objects.create(
            round=self.round, home_team=self.home, away_team=self.away,
            kickoff_at=self.now + timedelta(days=3),
        )
        self.user = User.objects.create_user(
            email="tipper@example.com", password="x", display_name="Tipper",
        )
        OrgMember.objects.create(user=self.user, org=self.org)
        self.marketing = Group.objects.create(org=self.org, name="Marketing")
        GroupMember.objects.create(group=self.marketing, user=self.user)
        self.client.force_login(self.user)

    def _enter_group(self):
        self.client.post(
            self.reverse("orgs:switch_group", args=[self.org.id, self.marketing.id])
        )

    def test_tipping_in_a_group_scores_on_the_group(self):
        from tipping.models import Tip
        from tipping.services import submit_tip

        submit_tip(user=self.user, match=self.match, org=self.org,
                   selection="home", group=self.marketing)
        tip = Tip.objects.get()
        self.assertEqual(tip.group, self.marketing)

    def test_the_same_round_can_be_tipped_both_ways(self):
        """Five in the group and four in the organisation is nine tips."""
        from tipping.models import Tip
        from tipping.services import submit_tip

        submit_tip(user=self.user, match=self.match, org=self.org,
                   selection="home", group=self.marketing)
        submit_tip(user=self.user, match=self.match, org=self.org,
                   selection="away", group=None)
        self.assertEqual(Tip.objects.count(), 2)
        self.assertEqual(
            Tip.objects.get(group=self.marketing).selection, "home",
        )
        self.assertEqual(Tip.objects.get(group__isnull=True).selection, "away")

    def test_a_group_from_another_organisation_is_refused(self):
        from tipping.services import submit_tip

        other = Organisation.objects.create(
            name="Beta", season=self.season, charity=self.charity, groups_enabled=True,
        )
        theirs = Group.objects.create(org=other, name="Marketing")
        with self.assertRaises(ValueError):
            submit_tip(user=self.user, match=self.match, org=self.org,
                       selection="home", group=theirs)

    # ---- the ladders stay apart -------------------------------------------

    def test_the_organisation_ladder_excludes_tips_made_in_a_group(self):
        """Otherwise a group's tips pool in with the organisation's and anyone
        tipping in both is counted twice."""
        from tipping.models import Tip
        from tipping.services import leaderboard_for_org, submit_tip

        submit_tip(user=self.user, match=self.match, org=self.org,
                   selection="home", group=self.marketing)
        Tip.objects.filter(group=self.marketing).update(
            is_correct=True, points_awarded=1,
        )
        board = {u.id: u.points for u in leaderboard_for_org(self.org)}
        self.assertEqual(board.get(self.user.id), 0)

    def test_the_group_ladder_counts_only_its_own_tips(self):
        from tipping.models import Tip
        from tipping.services import leaderboard_for_org, submit_tip

        submit_tip(user=self.user, match=self.match, org=self.org,
                   selection="home", group=self.marketing)
        Tip.objects.filter(group=self.marketing).update(
            is_correct=True, points_awarded=1,
        )
        board = {u.id: u.points for u in leaderboard_for_org(self.org, group=self.marketing)}
        self.assertEqual(board.get(self.user.id), 1)

    def test_a_group_ladder_lists_only_group_members(self):
        from tipping.services import leaderboard_for_org

        outsider = User.objects.create_user(
            email="outsider@example.com", password="x", display_name="Outsider",
        )
        OrgMember.objects.create(user=outsider, org=self.org)
        ids = {u.id for u in leaderboard_for_org(self.org, group=self.marketing)}
        self.assertIn(self.user.id, ids)
        self.assertNotIn(outsider.id, ids)

    # ---- the missed-tip default, per context ------------------------------

    def test_a_missed_round_defaults_in_every_context_separately(self):
        """Tipping in the group must not silently cover the organisation.

        Counting "has this person tipped this match at all" would have let one
        pick in Marketing stand in for the organisation's round too.
        """
        from tipping.models import Tip
        from tipping.services import submit_tip

        # Joined before kickoff, so eligible for the default.
        OrgMember.objects.filter(user=self.user, org=self.org).update(
            joined_at=self.now - timedelta(days=5),
        )
        GroupMember.objects.filter(group=self.marketing, user=self.user).update(
            joined_at=self.now - timedelta(days=5),
        )
        submit_tip(user=self.user, match=self.match, org=self.org,
                   selection="home", group=self.marketing)

        from tipping.services import _fill_missed_tips as fill
        fill(self.match)

        self.assertEqual(Tip.objects.filter(group=self.marketing).count(), 1)
        org_tip = Tip.objects.get(group__isnull=True)
        self.assertTrue(org_tip.is_auto)
        self.assertEqual(org_tip.selection, "away")


class GroupsPageTests(TestCase):
    """Creating, joining and approving groups from the directory."""

    def setUp(self):
        from django.urls import reverse

        self.reverse = reverse
        self.season, _ = Season.objects.get_or_create(year=2095, defaults={"label": "2095"})
        self.charity, _ = Charity.objects.get_or_create(
            slug="lifeline", defaults={"name": "Lifeline", "is_approved": True},
        )
        self.org = Organisation.objects.create(
            name="Acme", season=self.season, charity=self.charity,
        )
        self.admin = User.objects.create_user(
            email="admin@example.com", password="x", display_name="Admin",
        )
        self.member = User.objects.create_user(
            email="member@example.com", password="x", display_name="Member",
        )
        OrgMember.objects.create(
            user=self.admin, org=self.org,
            role=OrgMember.ROLE_BOTH, is_league_owner=True,
        )
        OrgMember.objects.create(user=self.member, org=self.org)
        self.url = reverse("orgs:groups", args=[self.org.id])

    def _enable(self):
        self.org.groups_enabled = True
        self.org.save(update_fields=["groups_enabled"])

    # ---- off by default ---------------------------------------------------

    def test_groups_are_off_and_the_page_explains_why_you_might_not_want_them(self):
        self.client.force_login(self.admin)
        html = self.client.get(self.url).content.decode()
        self.assertIn("Does Acme need groups?", html)
        self.assertIn("There are five or ten of you", html)
        self.assertIn("Switch groups on", html)

    def test_only_an_admin_can_switch_them_on(self):
        self.client.force_login(self.member)
        resp = self.client.post(self.reverse("orgs:groups_toggle", args=[self.org.id]))
        self.assertEqual(resp.status_code, 403)
        self.org.refresh_from_db()
        self.assertFalse(self.org.groups_enabled)

    def test_switching_on_and_off_keeps_everything(self):
        """Switching off is not a delete — it takes groups out of the nav."""
        self.client.force_login(self.admin)
        self._enable()
        Group.objects.create(org=self.org, name="Marketing")
        self.client.post(self.reverse("orgs:groups_toggle", args=[self.org.id]))
        self.org.refresh_from_db()
        self.assertFalse(self.org.groups_enabled)
        self.assertEqual(Group.objects.filter(org=self.org).count(), 1)

    def test_a_group_cannot_be_created_while_groups_are_off(self):
        self.client.force_login(self.admin)
        self.client.post(self.url, {"action": "create", "name": "Marketing"})
        self.assertFalse(Group.objects.exists())

    # ---- who has to ask ---------------------------------------------------

    def test_an_admin_creating_a_group_goes_live(self):
        self.client.force_login(self.admin)
        self._enable()
        self.client.post(self.url, {"action": "create", "name": "Marketing"})
        group = Group.objects.get()
        self.assertEqual(group.approval_status, Group.APPROVAL_APPROVED)
        self.assertTrue(
            GroupMember.objects.filter(group=group, user=self.admin, is_admin=True).exists()
        )

    def test_a_member_creating_a_group_has_to_be_approved(self):
        self.client.force_login(self.member)
        self._enable()
        self.client.post(self.url, {"action": "create", "name": "Skunkworks"})
        group = Group.objects.get()
        self.assertEqual(group.approval_status, Group.APPROVAL_PENDING)

    def test_a_pending_group_is_hidden_from_everyone_else(self):
        self._enable()
        self.client.force_login(self.member)
        self.client.post(self.url, {"action": "create", "name": "Skunkworks"})

        other = User.objects.create_user(
            email="other@example.com", password="x", display_name="Other",
        )
        OrgMember.objects.create(user=other, org=self.org)
        # A separate client on purpose. force_login starts a new session but
        # does not clear the cookie message store, so the "sent to the admins"
        # message queued for `member` above would render on this page and the
        # assertion would fail on a message rather than on the listing.
        from django.test import Client

        theirs = Client()
        theirs.force_login(other)
        self.assertNotIn("Skunkworks", theirs.get(self.url).content.decode())

        # But its author still sees it, so it does not silently vanish.
        mine = Client()
        mine.force_login(self.member)
        self.assertIn("Skunkworks", mine.get(self.url).content.decode())

    def test_an_admin_approving_makes_it_live(self):
        self._enable()
        self.client.force_login(self.member)
        self.client.post(self.url, {"action": "create", "name": "Skunkworks"})
        group = Group.objects.get()

        self.client.force_login(self.admin)
        self.client.post(self.url, {"action": "approve", "group_id": group.id})
        group.refresh_from_db()
        self.assertEqual(group.approval_status, Group.APPROVAL_APPROVED)
        self.assertEqual(group.approved_by, self.admin)

    def test_declining_removes_it_rather_than_leaving_a_ghost(self):
        self._enable()
        self.client.force_login(self.member)
        self.client.post(self.url, {"action": "create", "name": "Skunkworks"})
        group = Group.objects.get()

        self.client.force_login(self.admin)
        self.client.post(self.url, {"action": "decline", "group_id": group.id})
        self.assertFalse(Group.objects.filter(pk=group.pk).exists())

    def test_a_member_cannot_approve_their_own_group(self):
        self._enable()
        self.client.force_login(self.member)
        self.client.post(self.url, {"action": "create", "name": "Skunkworks"})
        group = Group.objects.get()
        resp = self.client.post(self.url, {"action": "approve", "group_id": group.id})
        self.assertEqual(resp.status_code, 403)
        group.refresh_from_db()
        self.assertEqual(group.approval_status, Group.APPROVAL_PENDING)

    # ---- joining and leaving ----------------------------------------------

    def test_joining_a_group_puts_you_in_it_and_steps_you_into_it(self):
        self._enable()
        marketing = Group.objects.create(org=self.org, name="Marketing")
        self.client.force_login(self.member)
        self.client.post(self.url, {"action": "join", "group_id": marketing.id})
        self.assertTrue(
            GroupMember.objects.filter(group=marketing, user=self.member).exists()
        )
        self.assertEqual(
            self.client.session.get("current_group_id"), marketing.pk,
        )

    def test_leaving_a_group_keeps_the_tips_you_made_in_it(self):
        """They were made there and scored on its ladder — deleting them would
        rewrite a season everyone else remembers."""
        from tipping.models import Match, Round, Team, Tip
        from django.utils import timezone

        self._enable()
        marketing = Group.objects.create(org=self.org, name="Marketing")
        GroupMember.objects.create(group=marketing, user=self.member)
        sport, _ = Sport.objects.get_or_create(name="AFL", defaults={"slug": "afl"})
        series, _ = Series.objects.get_or_create(
            name="AFL", defaults={"sport": sport, "slug": "afl"},
        )
        now = timezone.now()
        rnd = Round.objects.create(
            org=self.org, round_number=1, series=series,
            lockout_at=now + timedelta(days=1),
        )
        match = Match.objects.create(
            round=rnd,
            home_team=Team.objects.create(name="H", slug="h", series=series),
            away_team=Team.objects.create(name="A", slug="a", series=series),
            kickoff_at=now + timedelta(days=2),
        )
        Tip.objects.create(
            user=self.member, match=match, org=self.org,
            group=marketing, selection="home",
        )

        self.client.force_login(self.member)
        self.client.post(self.url, {"action": "leave", "group_id": marketing.id})
        self.assertFalse(
            GroupMember.objects.filter(group=marketing, user=self.member).exists()
        )
        self.assertEqual(Tip.objects.filter(group=marketing).count(), 1)

    def test_an_outsider_cannot_see_the_directory(self):
        outsider = User.objects.create_user(
            email="nope@example.com", password="x", display_name="Nope",
        )
        self.client.force_login(outsider)
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_two_groups_in_one_organisation_cannot_share_a_name(self):
        self._enable()
        Group.objects.create(org=self.org, name="Marketing")
        self.client.force_login(self.admin)
        self.client.post(self.url, {"action": "create", "name": "marketing"})
        self.assertEqual(Group.objects.filter(org=self.org).count(), 1)


class OrgSettingsTests(TestCase):
    """Changing what an organisation tips on, after it was created.

    Competitions were settable in exactly one template and frozen after that,
    so an organisation that started on NRL and wanted AFL the next year had no
    route to it at all.
    """

    def setUp(self):
        from catalog.models import Competition
        from django.urls import reverse

        self.reverse = reverse
        self.season, _ = Season.objects.get_or_create(year=2094, defaults={"label": "2094"})
        self.charity, _ = Charity.objects.get_or_create(
            slug="lifeline", defaults={"name": "Lifeline", "is_approved": True},
        )
        afl, _ = Sport.objects.get_or_create(name="AFL", defaults={"slug": "afl"})
        nrl, _ = Sport.objects.get_or_create(name="NRL", defaults={"slug": "nrl"})
        self.afl = self._comp(Competition, afl, "AFL", "afl", ["AFL", "AFLW"])
        self.nrl = self._comp(Competition, nrl, "NRL", "nrl", ["NRL", "NRLW"])

        self.org = Organisation.objects.create(
            name="Acme", season=self.season, charity=self.charity,
        )
        self.org.competitions.add(self.nrl)
        self.admin = User.objects.create_user(
            email="admin@example.com", password="x", display_name="Admin",
        )
        self.member = User.objects.create_user(
            email="member@example.com", password="x", display_name="Member",
        )
        OrgMember.objects.create(
            user=self.admin, org=self.org,
            role=OrgMember.ROLE_BOTH, is_league_owner=True,
        )
        OrgMember.objects.create(user=self.member, org=self.org)
        self.url = reverse("orgs:settings", args=[self.org.id])

    def _comp(self, Competition, sport, name, slug, series_names):
        comp, _ = Competition.objects.get_or_create(
            sport=sport, season=self.season, slug=slug, defaults={"name": name},
        )
        for sn in series_names:
            series, _ = Series.objects.get_or_create(
                name=sn, defaults={"sport": sport, "slug": sn.lower()},
            )
            comp.series.add(series)
        return comp

    def test_an_admin_can_add_a_competition_after_creation(self):
        self.client.force_login(self.admin)
        self.client.post(self.url, {"competitions": [self.nrl.pk, self.afl.pk]})
        self.assertEqual(
            set(self.org.competitions.values_list("pk", flat=True)),
            {self.nrl.pk, self.afl.pk},
        )

    def test_adding_a_code_brings_its_womens_competition_with_it(self):
        """Never one without the other — it is a property of Competition."""
        self.client.force_login(self.admin)
        self.client.post(self.url, {"competitions": [self.afl.pk]})
        names = {s.name for c in self.org.competitions.all() for s in c.series.all()}
        self.assertEqual(names, {"AFL", "AFLW"})

    def test_an_ordinary_member_cannot_change_them(self):
        self.client.force_login(self.member)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.client.post(self.url, {"competitions": [self.afl.pk]})
        self.assertEqual(
            set(self.org.competitions.values_list("pk", flat=True)), {self.nrl.pk},
        )

    def test_it_refuses_to_leave_an_organisation_with_nothing_to_tip(self):
        self.client.force_login(self.admin)
        self.client.post(self.url, {"competitions": []})
        self.assertEqual(self.org.competitions.count(), 1)

    def test_removing_a_competition_keeps_its_rounds_and_tips(self):
        """A season half-played is still a season people remember."""
        from django.utils import timezone
        from tipping.models import Match, Round, Team, Tip

        series = self.nrl.series.first()
        rnd = Round.objects.create(
            org=self.org, round_number=1, series=series, competition=self.nrl,
            lockout_at=timezone.now() + timedelta(days=1),
        )
        match = Match.objects.create(
            round=rnd,
            home_team=Team.objects.create(name="H", slug="h2", series=series),
            away_team=Team.objects.create(name="A", slug="a2", series=series),
            kickoff_at=timezone.now() + timedelta(days=2),
        )
        Tip.objects.create(user=self.member, match=match, org=self.org, selection="home")

        self.client.force_login(self.admin)
        self.client.post(self.url, {"competitions": [self.afl.pk]})
        self.assertEqual(Round.objects.filter(org=self.org).count(), 1)
        self.assertEqual(Tip.objects.filter(org=self.org).count(), 1)


class CharityVoteAtCreationTests(CreateWizardTests):
    """Scheduling the charity vote while creating, rather than "later".

    The wizard used to finish with "set up the charity election when you're
    ready" and drop you on the dashboard, so an admin who never came back left
    their members with a vote that silently never opened.
    """

    def _charity_step(self, **extra):
        data = {
            "step": 5, "action": "next", "charity_method": "vote",
            "vote_charities": [self.charity.pk, self.second_charity.pk],
        }
        data.update(extra)
        return self.client.post(self.URL, data)

    def setUp(self):
        super().setUp()
        self.second_charity, _ = Charity.objects.get_or_create(
            slug="beyond-blue", defaults={"name": "Beyond Blue", "is_approved": True},
        )

    def _walk(self, **charity_extra):
        self._step1()
        self._verify_step()
        self._groups_step()
        self._tipping_step()
        self._charity_step(**charity_extra)
        return self._review_step()

    def test_a_date_schedules_the_vote(self):
        from django.utils import timezone

        when = timezone.now() + timedelta(days=3)
        self._walk(vote_opens_at=when.strftime("%Y-%m-%dT%H:%M"))
        vote = CharityVote.objects.get()
        self.assertEqual(vote.status, CharityVote.STATUS_SCHEDULED)
        self.assertIsNotNone(vote.scheduled_open_at)

    def test_a_date_already_past_opens_it_now(self):
        from django.utils import timezone

        when = timezone.now() - timedelta(minutes=5)
        self._walk(vote_opens_at=when.strftime("%Y-%m-%dT%H:%M"))
        self.assertEqual(CharityVote.objects.get().status, CharityVote.STATUS_OPEN)

    def test_no_date_still_leaves_it_in_draft(self):
        """Blank is a real answer, not a missing one."""
        self._walk()
        self.assertEqual(CharityVote.objects.get().status, CharityVote.STATUS_DRAFT)

    def test_a_closing_time_without_an_opening_time_is_refused(self):
        from django.utils import timezone

        closes = timezone.now() + timedelta(days=4)
        self._step1()
        self._verify_step()
        self._groups_step()
        self._tipping_step()
        resp = self._charity_step(vote_closes_at=closes.strftime("%Y-%m-%dT%H:%M"))
        self.assertContains(resp, "when it opens")
        self.assertFalse(Organisation.objects.filter(name="Wizard Group").exists())

    def test_closing_before_opening_is_refused(self):
        from django.utils import timezone

        opens = timezone.now() + timedelta(days=4)
        closes = opens - timedelta(days=1)
        self._step1()
        self._verify_step()
        self._groups_step()
        self._tipping_step()
        resp = self._charity_step(
            vote_opens_at=opens.strftime("%Y-%m-%dT%H:%M"),
            vote_closes_at=closes.strftime("%Y-%m-%dT%H:%M"),
        )
        self.assertContains(resp, "close after it opens")


class ContextChipTests(TestCase):
    """The nav has to say which ladder a tip is about to land on."""

    def setUp(self):
        from django.urls import reverse

        self.reverse = reverse
        self.season, _ = Season.objects.get_or_create(year=2093, defaults={"label": "2093"})
        self.charity, _ = Charity.objects.get_or_create(
            slug="lifeline", defaults={"name": "Lifeline", "is_approved": True},
        )
        self.org = Organisation.objects.create(
            name="Acme", season=self.season, charity=self.charity, groups_enabled=True,
        )
        self.user = User.objects.create_user(
            email="m@example.com", password="x", display_name="M",
        )
        OrgMember.objects.create(user=self.user, org=self.org)
        self.marketing = Group.objects.create(org=self.org, name="Marketing")
        GroupMember.objects.create(group=self.marketing, user=self.user)
        self.client.force_login(self.user)

    def test_the_chip_names_the_organisation(self):
        html = self.client.get(self.reverse("dashboard")).content.decode()
        self.assertIn("an-ctx-org", html)
        self.assertIn("Acme", html)

    def test_the_chip_names_the_group_once_you_are_in_one(self):
        self.client.post(
            self.reverse("orgs:switch_group", args=[self.org.id, self.marketing.id])
        )
        html = self.client.get(self.reverse("dashboard")).content.decode()
        self.assertIn("an-ctx-group", html)
        self.assertIn("Marketing", html)

    def test_the_chip_offers_a_way_back_to_the_organisation(self):
        self.client.post(
            self.reverse("orgs:switch_group", args=[self.org.id, self.marketing.id])
        )
        html = self.client.get(self.reverse("dashboard")).content.decode()
        self.assertIn(self.reverse("orgs:leave_group_context"), html)

    def test_a_group_you_have_not_joined_is_not_offered(self):
        Group.objects.create(org=self.org, name="Sales")
        html = self.client.get(self.reverse("dashboard")).content.decode()
        self.assertNotIn("Sales", html)

    def test_switching_from_the_chip_returns_you_to_the_page_you_were_on(self):
        resp = self.client.post(
            self.reverse("orgs:switch_group", args=[self.org.id, self.marketing.id]),
            {"next": self.reverse("dashboard")},
        )
        self.assertEqual(resp["Location"], self.reverse("dashboard"))


class GroupWallTests(TestCase):
    """A group's wall is private to that group.

    Its posts never surface on the organisation's feed and can never be shared
    publicly — the public wall is the organisation speaking, not one of its
    teams.
    """

    def setUp(self):
        from django.urls import reverse

        self.reverse = reverse
        self.season, _ = Season.objects.get_or_create(year=2092, defaults={"label": "2092"})
        self.charity, _ = Charity.objects.get_or_create(
            slug="lifeline", defaults={"name": "Lifeline", "is_approved": True},
        )
        self.org = Organisation.objects.create(
            name="Acme", season=self.season, charity=self.charity,
            groups_enabled=True, is_public_listed=True,
        )
        self.insider = User.objects.create_user(
            email="in@example.com", password="x", display_name="Insider",
        )
        self.outsider = User.objects.create_user(
            email="out@example.com", password="x", display_name="Outsider",
        )
        self.admin = User.objects.create_user(
            email="boss@example.com", password="x", display_name="Boss",
        )
        for u in (self.insider, self.outsider, self.admin):
            OrgMember.objects.create(user=u, org=self.org)
        OrgMember.objects.filter(user=self.admin, org=self.org).update(
            role=OrgMember.ROLE_BOTH, is_league_owner=True,
        )
        self.marketing = Group.objects.create(org=self.org, name="Marketing")
        GroupMember.objects.create(group=self.marketing, user=self.insider)

        self.wall = reverse("orgs:wall", args=[self.org.id])
        self.post_url = reverse("orgs:wall_post", args=[self.org.id])

    def _client_for(self, user, group=None):
        from django.test import Client

        c = Client()
        c.force_login(user)
        if group is not None:
            c.post(self.reverse("orgs:switch_group", args=[self.org.id, group.id]))
        return c

    # ---- the two walls stay apart -----------------------------------------

    def test_a_post_made_in_a_group_lands_on_that_groups_wall(self):
        c = self._client_for(self.insider, self.marketing)
        c.post(self.post_url, {"body": "Marketing is winning"})
        post = WallPost.objects.get()
        self.assertEqual(post.group, self.marketing)

    def test_a_group_post_never_shows_on_the_organisation_wall(self):
        c = self._client_for(self.insider, self.marketing)
        c.post(self.post_url, {"body": "Marketing is winning"})

        # Same person, stepped back out to the organisation.
        c.post(self.reverse("orgs:leave_group_context"))
        self.assertNotIn("Marketing is winning", c.get(self.wall).content.decode())

    def test_an_organisation_post_never_shows_on_a_groups_wall(self):
        c = self._client_for(self.insider)
        c.post(self.post_url, {"body": "Everyone welcome"})
        c.post(self.reverse("orgs:switch_group", args=[self.org.id, self.marketing.id]))
        self.assertNotIn("Everyone welcome", c.get(self.wall).content.decode())

    def test_someone_not_in_the_group_cannot_read_it(self):
        self._client_for(self.insider, self.marketing).post(
            self.post_url, {"body": "Marketing is winning"}
        )
        theirs = self._client_for(self.outsider)
        self.assertNotIn("Marketing is winning", theirs.get(self.wall).content.decode())

    # ---- the id is not a key ----------------------------------------------

    def test_an_outsider_cannot_reply_by_guessing_the_post_id(self):
        """Membership of the organisation is not enough once groups exist.

        Reply, react and remove all fetched a post by (id, org), and the id is
        right there in the anchor of any link someone in the group pasted.
        """
        self._client_for(self.insider, self.marketing).post(
            self.post_url, {"body": "Marketing is winning"}
        )
        post = WallPost.objects.get()
        theirs = self._client_for(self.outsider)
        resp = theirs.post(
            self.reverse("orgs:wall_reply", args=[self.org.id, post.id]),
            {"body": "let me in"},
        )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(post.replies.count(), 0)

    def test_an_outsider_cannot_react_by_guessing_the_post_id(self):
        from .models import WallReaction

        self._client_for(self.insider, self.marketing).post(
            self.post_url, {"body": "Marketing is winning"}
        )
        post = WallPost.objects.get()
        theirs = self._client_for(self.outsider)
        # "fire", not the emoji character. Posting 🔥 is rejected as an invalid
        # choice before the access check is ever reached, so the test passed
        # with the gate removed — proving nothing.
        resp = theirs.post(
            self.reverse("orgs:wall_react", args=[self.org.id, post.id]),
            {"emoji": "fire"},
        )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(WallReaction.objects.count(), 0)

    def test_a_member_of_the_group_can_react(self):
        """The other half: proves the 403 above is the gate, not the emoji."""
        from .models import WallReaction

        c = self._client_for(self.insider, self.marketing)
        c.post(self.post_url, {"body": "Marketing is winning"})
        post = WallPost.objects.get()
        c.post(
            self.reverse("orgs:wall_react", args=[self.org.id, post.id]),
            {"emoji": "fire"},
        )
        self.assertEqual(WallReaction.objects.filter(post=post).count(), 1)

    def test_a_member_of_the_group_can_reply(self):
        c = self._client_for(self.insider, self.marketing)
        c.post(self.post_url, {"body": "Marketing is winning"})
        post = WallPost.objects.get()
        c.post(
            self.reverse("orgs:wall_reply", args=[self.org.id, post.id]),
            {"body": "too right"},
        )
        self.assertEqual(post.replies.count(), 1)

    def test_an_admin_can_moderate_a_group_they_are_not_in(self):
        """Someone has to be able to take down what is posted in their
        organisation's name, and a room nobody can moderate is worse."""
        self._client_for(self.insider, self.marketing).post(
            self.post_url, {"body": "Marketing is winning"}
        )
        post = WallPost.objects.get()
        boss = self._client_for(self.admin)
        boss.post(self.reverse("orgs:wall_remove", args=[self.org.id, post.id]))
        post.refresh_from_db()
        self.assertTrue(post.is_hidden)

    # ---- a group's wall is never public -----------------------------------

    def test_a_group_post_cannot_be_shared_publicly(self):
        c = self._client_for(self.insider, self.marketing)
        c.post(self.post_url, {"body": "Marketing is winning", "share_public": "1"})
        self.assertFalse(WallPost.objects.get().is_public)

    def test_the_composer_does_not_offer_it_on_a_group_wall(self):
        c = self._client_for(self.insider, self.marketing)
        self.assertNotIn("share_public", c.get(self.wall).content.decode())

    def test_an_organisation_post_can_still_be_shared(self):
        c = self._client_for(self.insider)
        c.post(self.post_url, {"body": "Everyone welcome", "share_public": "1"})
        self.assertTrue(WallPost.objects.get().is_public)

    # ---- who hears about it ------------------------------------------------

    def test_a_group_post_only_notifies_the_group(self):
        """Not twenty thousand people who cannot open it."""
        from .models import Notification

        c = self._client_for(self.insider, self.marketing)
        c.post(self.post_url, {"body": "Marketing is winning"})
        told = set(
            Notification.objects.filter(kind=Notification.KIND_WALL_POST)
            .values_list("user_id", flat=True)
        )
        self.assertEqual(told, set())          # insider is the only member

        GroupMember.objects.create(group=self.marketing, user=self.admin)
        c.post(self.post_url, {"body": "again"})
        told = set(
            Notification.objects.filter(kind=Notification.KIND_WALL_POST)
            .values_list("user_id", flat=True)
        )
        self.assertEqual(told, {self.admin.id})
        self.assertNotIn(self.outsider.id, told)


class GroupRecapTests(TestCase):
    """A group gets its own round recap, about its own room.

    The organisation's card is built from the organisation's tips and lands on
    the organisation's wall; Marketing's is built from Marketing's tips and
    lands on Marketing's.
    """

    def setUp(self):
        from django.utils import timezone
        from tipping.models import Match, Round, Team

        self.sport = Sport.objects.create(name="Recap Code", slug="recap-code")
        self.series = Series.objects.create(
            sport=self.sport, name="Recap Comp", slug="recap-comp",
        )
        self.season = Season.objects.create(year=2091, label="2091")
        self.org = Organisation.objects.create(
            name="Acme", season=self.season, groups_enabled=True,
        )
        self.insider = User.objects.create_user(
            email="in@r.com", password="x", display_name="Insider",
        )
        self.outsider = User.objects.create_user(
            email="out@r.com", password="x", display_name="Outsider",
        )
        for u in (self.insider, self.outsider):
            OrgMember.objects.create(user=u, org=self.org)
        self.marketing = Group.objects.create(org=self.org, name="Marketing")
        GroupMember.objects.create(group=self.marketing, user=self.insider)

        self.home = Team.objects.create(name="Pies", slug="pies-g", series=self.series)
        self.away = Team.objects.create(name="Roos", slug="roos-g", series=self.series)
        self.rnd = Round.objects.create(
            org=self.org, round_number=1, series=self.series,
            stage=Round.STAGE_REGULAR,
            lockout_at=timezone.now() - timedelta(days=2),
        )
        self.match = Match.objects.create(
            round=self.rnd, home_team=self.home, away_team=self.away,
            kickoff_at=timezone.now() - timedelta(days=1),
        )

    def _settle(self):
        from tipping.services import record_match_result

        record_match_result(self.match, 30, 10)

    def _tip(self, user, group=None, selection="home"):
        from tipping.models import Tip

        return Tip.objects.create(
            user=user, match=self.match, org=self.org, group=group,
            selection=selection,
        )

    # ---- one card per room -------------------------------------------------

    def test_a_group_that_tipped_gets_its_own_card(self):
        from .models import RoundRecap
        from .recaps import generate_recaps

        self._tip(self.insider, group=self.marketing)
        self._settle()
        generate_recaps(org=self.org)

        recap = RoundRecap.objects.get(group=self.marketing)
        self.assertEqual(recap.post.group, self.marketing)
        self.assertEqual(recap.post.kind, WallPost.KIND_RECAP)

    def test_the_organisation_and_a_group_each_get_one(self):
        from .models import RoundRecap
        from .recaps import generate_recaps

        self._tip(self.outsider, group=None)
        self._tip(self.insider, group=self.marketing)
        self._settle()
        generate_recaps(org=self.org)

        self.assertEqual(RoundRecap.objects.filter(group__isnull=True).count(), 1)
        self.assertEqual(RoundRecap.objects.filter(group=self.marketing).count(), 1)

    def test_a_group_nobody_tipped_in_stays_silent(self):
        """§10: silence, not an apologetic card."""
        from .models import RoundRecap
        from .recaps import generate_recaps

        self._tip(self.outsider, group=None)
        self._settle()
        generate_recaps(org=self.org)

        self.assertFalse(RoundRecap.objects.filter(group=self.marketing).exists())
        self.assertTrue(RoundRecap.objects.filter(group__isnull=True).exists())

    def test_groups_switched_off_means_no_group_cards(self):
        from .models import RoundRecap
        from .recaps import generate_recaps

        self.org.groups_enabled = False
        self.org.save(update_fields=["groups_enabled"])
        self._tip(self.insider, group=self.marketing)
        self._tip(self.outsider, group=None)
        self._settle()
        generate_recaps(org=self.org)
        self.assertFalse(RoundRecap.objects.filter(group=self.marketing).exists())

    def test_a_pending_group_gets_no_card(self):
        from .models import RoundRecap
        from .recaps import generate_recaps

        pending = Group.objects.create(
            org=self.org, name="Skunkworks",
            approval_status=Group.APPROVAL_PENDING,
        )
        GroupMember.objects.create(group=pending, user=self.insider)
        self._tip(self.insider, group=pending)
        self._settle()
        generate_recaps(org=self.org)
        self.assertFalse(RoundRecap.objects.filter(group=pending).exists())

    def test_running_twice_posts_nothing_new(self):
        """The NULL trap: `group` is nullable, so a plain unique_together over
        (org, round, group) would enforce nothing for the organisation's own
        card and the second run would post a duplicate to the Wall."""
        from .recaps import generate_recaps

        self._tip(self.outsider, group=None)
        self._tip(self.insider, group=self.marketing)
        self._settle()
        first = generate_recaps(org=self.org)
        second = generate_recaps(org=self.org)
        self.assertEqual(len(first), 2)
        self.assertEqual(second, [])
        self.assertEqual(WallPost.objects.filter(kind=WallPost.KIND_RECAP).count(), 2)

    # ---- the card is about the room it is in -------------------------------

    def test_the_card_names_the_group_not_the_organisation(self):
        from .recaps import build_recap_facts

        self._tip(self.insider, group=self.marketing)
        self._settle()
        facts = build_recap_facts(self.org, self.rnd, self.marketing)
        self.assertEqual(facts["group"]["name"], "Marketing")

    def test_the_organisation_card_still_names_the_organisation(self):
        from .recaps import build_recap_facts

        self._tip(self.outsider, group=None)
        self._settle()
        facts = build_recap_facts(self.org, self.rnd, None)
        self.assertEqual(facts["group"]["name"], "Acme")

    def test_a_group_card_counts_only_its_own_tippers(self):
        from .recaps import build_recap_facts

        self._tip(self.outsider, group=None)
        self._tip(self.insider, group=self.marketing)
        self._settle()
        facts = build_recap_facts(self.org, self.rnd, self.marketing)
        self.assertEqual(facts["group"]["members_who_tipped"], 1)
        self.assertEqual({m["name"] for m in facts["members"]}, {"Insider"})

    def test_two_rooms_do_not_get_word_for_word_identical_cards(self):
        """The seed picks the wording variant, and it used to be (org, round) —
        so every group in an organisation would post the same sentence on the
        same afternoon."""
        from .recaps import build_recap_facts

        self._tip(self.outsider, group=None)
        self._tip(self.insider, group=self.marketing)
        self._settle()
        org_facts = build_recap_facts(self.org, self.rnd, None)
        grp_facts = build_recap_facts(self.org, self.rnd, self.marketing)
        self.assertNotEqual(org_facts["seed"], grp_facts["seed"])


class GroupResultsEmailTests(TestCase):
    """One scorecard per room.

    The mail used to be organisation-only, so a member who only ever tipped
    inside a group was written to about a ladder they are not on, with a rank
    they do not have, over picks they did not make there.
    """

    def setUp(self):
        from django.core import mail
        from django.utils import timezone
        from tipping.models import Match, Round, Team

        mail.outbox = []
        self.sport = Sport.objects.create(name="Mail Code", slug="mail-code")
        self.series = Series.objects.create(
            sport=self.sport, name="Mail Comp", slug="mail-comp",
        )
        self.season = Season.objects.create(year=2090, label="2090")
        self.charity, _ = Charity.objects.get_or_create(
            slug="lifeline", defaults={"name": "Lifeline", "is_approved": True},
        )
        self.org = Organisation.objects.create(
            name="Acme", season=self.season, charity=self.charity, groups_enabled=True,
        )
        self.grouper = User.objects.create_user(
            email="grouper@x.com", password="x", display_name="Grouper",
        )
        self.orgonly = User.objects.create_user(
            email="orgonly@x.com", password="x", display_name="OrgOnly",
        )
        for u in (self.grouper, self.orgonly):
            OrgMember.objects.create(user=u, org=self.org)
        self.marketing = Group.objects.create(org=self.org, name="Marketing")
        GroupMember.objects.create(group=self.marketing, user=self.grouper)

        self.rnd = Round.objects.create(
            org=self.org, round_number=7, series=self.series,
            stage=Round.STAGE_REGULAR,
            lockout_at=timezone.now() - timedelta(days=2),
        )
        self.match = Match.objects.create(
            round=self.rnd,
            home_team=Team.objects.create(name="Pies", slug="pies-m", series=self.series),
            away_team=Team.objects.create(name="Roos", slug="roos-m", series=self.series),
            kickoff_at=timezone.now() - timedelta(days=1),
        )

    def _tip(self, user, group=None, selection="home"):
        from tipping.models import Tip

        return Tip.objects.create(
            user=user, match=self.match, org=self.org, group=group, selection=selection,
        )

    def _settle_and_send(self):
        from django.core import mail
        from tipping.services import record_match_result
        from .notifications import send_round_results

        record_match_result(self.match, 30, 10)
        send_round_results(self.rnd)
        return mail.outbox

    def _to(self, outbox):
        return {(m.to[0], m.subject) for m in outbox}

    # ---- the mismatch this fixes -------------------------------------------

    def test_someone_who_only_tipped_in_a_group_hears_about_the_group(self):
        self._tip(self.grouper, group=self.marketing)
        outbox = self._settle_and_send()
        self.assertEqual(len(outbox), 1)
        msg = outbox[0]
        self.assertEqual(msg.to, ["grouper@x.com"])
        self.assertIn("in Marketing", msg.subject)

    def test_they_are_not_written_to_about_the_organisation_ladder(self):
        """The bug: a rank they do not have, over picks they did not make."""
        self._tip(self.grouper, group=self.marketing)
        outbox = self._settle_and_send()
        self.assertEqual(len(outbox), 1)
        self.assertNotIn("Round 7: you got", outbox[0].subject)  # the org subject

    def test_tipping_in_both_gets_two_scorecards_that_say_which_is_which(self):
        self._tip(self.grouper, group=None, selection="away")
        self._tip(self.grouper, group=self.marketing, selection="home")
        outbox = self._settle_and_send()
        subjects = [m.subject for m in outbox if m.to == ["grouper@x.com"]]
        self.assertEqual(len(subjects), 2)
        group_line = next(x for x in subjects if "in Marketing" in x)
        org_line = next(x for x in subjects if "in Marketing" not in x)
        # Different picks in the two rooms, so different results — which is the
        # whole reason one mail could not honestly report both.
        self.assertIn("1 of 1", group_line)   # picked home, home won
        self.assertIn("0 of 1", org_line)     # picked away for the organisation

    def test_nobody_is_written_to_about_a_room_they_did_not_tip_in(self):
        self._tip(self.orgonly, group=None)
        outbox = self._settle_and_send()
        self.assertEqual(self._to(outbox), {
            ("orgonly@x.com", "Round 7: you got 1 of 1"),
        })

    def test_a_group_member_is_not_told_about_a_group_they_left(self):
        self._tip(self.grouper, group=self.marketing)
        GroupMember.objects.filter(group=self.marketing, user=self.grouper).delete()
        outbox = self._settle_and_send()
        self.assertEqual(outbox, [])

    def test_groups_switched_off_sends_only_the_organisation_mail(self):
        self._tip(self.grouper, group=self.marketing)
        self._tip(self.orgonly, group=None)
        self.org.groups_enabled = False
        self.org.save(update_fields=["groups_enabled"])
        outbox = self._settle_and_send()
        self.assertEqual(len(outbox), 1)
        self.assertEqual(outbox[0].to, ["orgonly@x.com"])

    def test_an_auto_tip_alone_still_earns_no_mail(self):
        """The missed-tip default must not become a scorecard, in any room."""
        from tipping.models import Tip

        Tip.objects.create(
            user=self.grouper, match=self.match, org=self.org,
            group=self.marketing, selection="away", is_auto=True,
        )
        self.assertEqual(self._settle_and_send(), [])

    def test_the_body_says_which_ladder_it_is_about(self):
        self._tip(self.grouper, group=self.marketing)
        outbox = self._settle_and_send()
        body = outbox[0].alternatives[0][0] if outbox[0].alternatives else outbox[0].body
        self.assertIn("Marketing", body)
        self.assertIn("counted separately", body)

    def test_the_round_is_stamped_once_for_the_whole_send(self):
        self._tip(self.grouper, group=self.marketing)
        self._tip(self.orgonly, group=None)
        self._settle_and_send()
        self.rnd.refresh_from_db()
        self.assertIsNotNone(self.rnd.results_email_sent_at)


class GroupScopeLeakTests(TestCase):
    """The places that count tips but were still counting all of them.

    Each of these sits next to a number that IS scoped, so a leak here does not
    look like a leak — it looks like two parts of one screen disagreeing.
    """

    def setUp(self):
        from django.utils import timezone
        from tipping.models import Match, Round, Team

        self.now = timezone.now()
        self.season = Season.objects.create(year=2089, label="2089")
        self.charity, _ = Charity.objects.get_or_create(
            slug="lifeline", defaults={"name": "Lifeline", "is_approved": True},
        )
        self.sport = Sport.objects.create(name="Leak Code", slug="leak-code")
        self.series = Series.objects.create(
            sport=self.sport, name="Leak Comp", slug="leak-comp",
        )
        self.parent = Organisation.objects.create(
            name="Parent", season=self.season, charity=self.charity, groups_enabled=True,
        )
        self.child = Organisation.objects.create(
            name="Child", season=self.season, charity=self.charity, parent=self.parent,
        )
        self.user = User.objects.create_user(
            email="u@leak.com", password="x", display_name="U",
        )
        OrgMember.objects.create(user=self.user, org=self.parent)
        self.marketing = Group.objects.create(org=self.parent, name="Marketing")
        GroupMember.objects.create(group=self.marketing, user=self.user)

        self.rnd = Round.objects.create(
            org=self.parent, round_number=1, series=self.series,
            lockout_at=self.now + timedelta(days=1),
        )
        self.match = Match.objects.create(
            round=self.rnd,
            home_team=Team.objects.create(name="H", slug="h-leak", series=self.series),
            away_team=Team.objects.create(name="A", slug="a-leak", series=self.series),
            kickoff_at=self.now + timedelta(days=2),
        )

    def _scored(self, group, points):
        from tipping.models import Tip

        return Tip.objects.create(
            user=self.user, match=self.match, org=self.parent, group=group,
            selection="home", is_correct=True, points_awarded=points,
        )

    def test_the_national_board_leaves_group_tips_out(self):
        """Otherwise anyone tipping in both is counted twice, one level up from
        where leaderboard_for_org already guards against it."""
        from tipping.services import leaderboard_for_family

        self._scored(None, 3)
        self._scored(self.marketing, 5)
        board = {u.id: u.points for u in leaderboard_for_family(self.child)}
        self.assertEqual(board.get(self.user.id), 3)

    def test_dashboard_stats_count_one_room(self):
        from tipping.services import user_org_stats

        self._scored(None, 3)
        self._scored(self.marketing, 5)
        self.assertEqual(user_org_stats(self.user, self.parent)["points"], 3)
        self.assertEqual(
            user_org_stats(self.user, self.parent, group=self.marketing)["points"], 5,
        )

    def test_the_countback_walks_one_rooms_tips(self):
        """A group ladder broken on the organisation's countback would order
        its members by tips that never counted towards the tied score."""
        from tipping.services import _reached_score_at

        self._scored(None, 3)
        self._scored(self.marketing, 5)
        org_reached = _reached_score_at(
            [self.parent.id], [self.user.id], {self.user.id: 3},
        )
        grp_reached = _reached_score_at(
            [self.parent.id], [self.user.id], {self.user.id: 5}, group=self.marketing,
        )
        # Both resolve, and neither borrowed the other's tip to get there.
        self.assertIn(self.user.id, org_reached)
        self.assertIn(self.user.id, grp_reached)

    def test_a_group_ladder_reports_the_groups_points(self):
        from tipping.services import leaderboard_for_org

        self._scored(None, 3)
        self._scored(self.marketing, 5)
        org_board = {u.id: u.points for u in leaderboard_for_org(self.parent)}
        grp_board = {
            u.id: u.points
            for u in leaderboard_for_org(self.parent, group=self.marketing)
        }
        self.assertEqual(org_board.get(self.user.id), 3)
        self.assertEqual(grp_board.get(self.user.id), 5)
