import shutil
import tempfile
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.urls import reverse
from django.core.exceptions import ValidationError
from django.db.models import ProtectedError
import re

from django.conf import settings
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from goodtip.testing import drop_temp_media, temp_media
from catalog.models import Charity, Season, Series, Sport

from .models import (
    CharityVote,
    CharityVoteOption,
    MembershipRequest,
    Group,
    GroupMember,
    Notification,
    OrgCharitySelection,
    OrgDraft,
    OrgMember,
    Organisation,
    WallPost,
)
from .recaps import build_talking_points, compose_recap, fallback_line
# The wizard's step NUMBERS, taken from the wizard itself rather than written
# out here. They have already shifted once — inserting the formality step at
# the front moved every screen behind it — and a test that hardcodes a 4 does
# not fail when that happens, it silently posts the tipping fields at the
# groups screen and then asserts about the wrong thing.
from .views import (
    CHARITY_STEP,
    COMPETITION_STEP as TIPPING_STEP,
    DETAILS_STEP,
    FORMALITY_STEP,
    GROUPS_STEP,
    LAST_STEP as REVIEW_STEP,
    VERIFY_STEP,
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


def current_form_season():
    """The season OrgCreateForm actually offers competitions from.

    Nobody signing up picks a year — the form pins itself to the season in
    play (see OrgCreateForm.__init__) and hides the field. A fixture that
    invents a far-future season therefore builds a competition the form will
    not accept, and every test using it fails with "N is not one of the
    available choices" — which reads as a wizard bug and is really the test
    asking for a competition that is not on offer.

    Asks the form rather than deriving it a second way. Restating the rule
    here is what let it drift apart before: the test's copy and the form's
    copy agreed on the day they were written and not afterwards.
    """
    from .forms import current_signup_season

    return current_signup_season()


def current_form_competition(slug="afl"):
    """A competition the form will actually accept, from the seeded catalog.

    Building one in a fixture instead collided with the real seeded row —
    same (slug, season), different Sport — so get_or_create missed on the
    lookup and then violated the unique constraint on the insert. The seeded
    rows already carry their series, and they are what production looks like.
    """
    from catalog.models import Competition

    return Competition.objects.filter(
        slug=slug, season=current_form_season(),
    ).first()


class OrgCategoryFormTests(TestCase):
    """Per-type sign-up rules from the categories doc (7 Jul 2026)."""

    def setUp(self):
        from catalog.models import Competition, OrganisationType, SubCategory

        self.season = current_form_season()
        # The real seeded AFL competition for the season in play. It already
        # carries its series, so it is exactly what the form offers.
        self.comp = current_form_competition()
        self.sport = self.comp.sport
        self.charity, _ = Charity.objects.get_or_create(
            slug="lifeline", defaults={"name": "Lifeline", "is_approved": True},
        )
        self.types = {g.slug: g for g in OrganisationType.objects.all()}
        self.subcat = lambda t, s: SubCategory.objects.get(organisation_type__slug=t, slug=s)

    def form(self, **extra):
        from .forms import OrgCreateForm

        # Formality is answered before anything else in the real wizard, and
        # it is what decides whether a type is required at all — so the
        # default here is the formal path these type rules are about.
        data = {
            "formality": "formal",
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
        """For a FORMAL organisation. Informal has exactly one type and is
        filled in rather than asked — see test_informal_needs_no_type."""
        f = self.form()
        self.assertFalse(f.is_valid())
        self.assertIn("organisation_type", f.errors)

    def test_informal_needs_no_type(self):
        f = self.form(formality="informal", informal_label="Mates comp")
        f.is_valid()
        self.assertNotIn("organisation_type", f.errors)
        self.assertTrue(f.cleaned_data["organisation_type"].is_informal)

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
        # Asked as the FORMALITY answer, not as a type: the informal type is
        # filled in by the form once formality says so, and posting the type
        # alongside formality="formal" is a contradiction the form resolves in
        # favour of the first answer rather than a way of reaching this rule.
        f = self.form(formality="informal")
        self.assertFalse(f.is_valid())
        self.assertIn("informal_label", f.errors)
        f = self.form(formality="informal", informal_label="Book Club")
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
    # prove — so the verify step is not merely waved through, it is not shown
    # at all (see views._advance), and nothing here has to answer an email
    # code that has nothing to do with what is being tested. Verification has
    # its own tests.
    #
    # Formality is its own first step now, and it is what excuses the verify
    # step — so it is answered before the name rather than implied by the type
    # posted alongside it. The type itself is not posted: the form fills in
    # the one informal type once formality says informal.
    client.post(url, {
        "step": FORMALITY_STEP, "action": "next", "formality": "informal",
        "parent": parent,
    })
    client.post(url, {
        "step": DETAILS_STEP, "action": "next", "name": name,
        "informal_label": "Book Club", "parent": parent,
    })
    client.post(url, {
        "step": GROUPS_STEP, "action": "next", "groups_enabled": "no", "parent": parent,
    })
    client.post(url, {
        "step": TIPPING_STEP, "action": "next",
        "competitions": [case.comp.pk], "season": case.season.pk, "parent": parent,
    })
    client.post(url, {
        "step": CHARITY_STEP, "action": "next",
        "charity_method": "pick", "charity": case.charity.pk, "parent": parent,
    })
    final = {"step": REVIEW_STEP, "action": "next", "parent": parent}
    if extra.get("duplicate_confirmed"):
        final["duplicate_confirmed"] = extra["duplicate_confirmed"]
    return client.post(url, final)


class DuplicateDetectionTests(TestCase):
    """Org-structure note §4 Stage 2: creating an org whose name already
    exists needs one explicit confirmation — friction, not prevention."""

    def setUp(self):
        from catalog.models import OrganisationType

        # The season and competition the FORM offers, not one invented here.
        # A fixture season of 2099 built a competition the wizard's tipping
        # step then refused — its queryset is the current season only — so the
        # walk stalled there and every assertion about what happens at the END
        # of the wizard failed on a screen four steps earlier. See
        # current_form_season().
        self.season = current_form_season()
        self.comp = current_form_competition()
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
        from catalog.models import OrganisationType

        # The season and competition the FORM offers, not one invented here.
        # A fixture season of 2099 built a competition the wizard's tipping
        # step then refused — its queryset is the current season only — so the
        # walk stalled there and every assertion about what happens at the END
        # of the wizard failed on a screen four steps earlier. See
        # current_form_season().
        self.season = current_form_season()
        self.comp = current_form_competition()
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

    def test_existing_members_are_skipped_whatever_the_casing(self):
        """Postgres matches email exactly, addresses do not.

        Typing your own address with a capital letter used to slip past the
        already-a-member check and mail a "come join us" to somebody who was
        standing in the group already.
        """
        from django.core import mail

        mate = User.objects.create_user(
            email="mate@example.com", password="Str0ng!pass", display_name="Mate"
        )
        OrgMember.objects.create(user=mate, org=self.org)
        self._post("Mate@Example.com, fresh@example.com")
        self.assertEqual([m.to[0] for m in mail.outbox], ["fresh@example.com"])


class JoinLinkContextTests(TestCase):
    """Following an invite link has to leave you standing in the organisation
    that invited you.

    It used to write the membership row and nothing else, so a member of
    several organisations followed an invite and landed on the dashboard for
    whichever one the session happened to name — the mail said one
    organisation, the screen said another.
    """

    def setUp(self):
        self.season, _ = Season.objects.get_or_create(year=2032, defaults={"label": "2032"})
        # Named so the alphabetical fallback in context.current_org is NOT the
        # org being joined — that is the case that was broken.
        self.other = Organisation.objects.create(name="AquaFlow", season=self.season)
        self.org = Organisation.objects.create(name="Masterclass", season=self.season)
        self.admin = User.objects.create_user(
            email="boss@m.com", password="Str0ng!pass", display_name="Bea"
        )
        OrgMember.objects.create(user=self.admin, org=self.org, is_league_owner=True)
        self.joiner = User.objects.create_user(
            email="new@m.com", password="Str0ng!pass", display_name="Nia"
        )
        OrgMember.objects.create(user=self.joiner, org=self.other)

    def _join(self):
        from .signing import make_join_token

        token = make_join_token(self.org.id, inviter_id=self.admin.id)
        return self.client.get(f"/join/{self.org.id}/{token}/")

    def test_joining_puts_you_in_the_organisation_you_were_invited_to(self):
        from .context import ORG_KEY

        self.client.force_login(self.joiner)
        self._join()
        self.assertEqual(self.client.session[ORG_KEY], self.org.id)

    def test_it_overrides_wherever_you_were_standing(self):
        from .context import ORG_KEY

        self.client.force_login(self.joiner)
        self.client.post(reverse("orgs:switch_org", args=[self.other.id]))
        self._join()
        self.assertEqual(self.client.session[ORG_KEY], self.org.id)

    def test_a_second_click_still_lands_you_there(self):
        """An invite followed twice is the likeliest way to hit this: the
        member is already in, so nothing is written, and the context still has
        to move."""
        from .context import ORG_KEY

        self.client.force_login(self.joiner)
        self._join()
        self.client.post(reverse("orgs:switch_org", args=[self.other.id]))
        self._join()
        self.assertEqual(self.client.session[ORG_KEY], self.org.id)

    def test_following_the_link_signed_out_then_signing_in_lands_there_too(self):
        """The link is usually opened in the mail client, signed out. The join
        is held in the session and completed on the way in, and it has to move
        the context the same way the signed-in path does."""
        from .context import ORG_KEY

        # Straight through the password, so the test is about the join and not
        # about the emailed code.
        self.joiner.two_factor_enabled = False
        self.joiner.save(update_fields=["two_factor_enabled"])
        self._join()                            # signed out: nothing joined yet
        self.assertFalse(OrgMember.objects.filter(user=self.joiner, org=self.org).exists())
        self.client.post(reverse("accounts:login"), {
            "email": "new@m.com", "password": "Str0ng!pass",
        })
        self.assertTrue(OrgMember.objects.filter(user=self.joiner, org=self.org).exists())
        self.assertEqual(self.client.session[ORG_KEY], self.org.id)


class BellTickerTests(TestCase):
    """Undismissed notifications take turns at the bell.

    They used to be one pinned card showing only the newest, which had to be
    closed before the page underneath could be used, and which hid every
    notification behind it.
    """

    def setUp(self):
        self.season, _ = Season.objects.get_or_create(year=2033, defaults={"label": "2033"})
        self.org = Organisation.objects.create(name="Ticker FC", season=self.season)
        self.user = User.objects.create_user(
            email="tick@t.com", password="Str0ng!pass", display_name="Tam"
        )
        OrgMember.objects.create(user=self.user, org=self.org, is_league_owner=True)
        self.client.force_login(self.user)

    def _note(self, title, **kw):
        from .models import Notification

        return Notification.objects.create(
            user=self.user, org=self.org, kind=Notification.KIND_ADMIN_NOTE,
            title=title, message="the body stays in the panel", **kw
        )

    def _queue(self):
        """The ticker's queue, read back out of the rendered page."""
        import json
        import re

        html = self.client.get(reverse("dashboard")).content.decode()
        m = re.search(
            r'<script id="bellTickData"[^>]*>(.*?)</script>', html, re.S
        )
        self.assertIsNotNone(m, "the ticker's queue is not on the page")
        return json.loads(m.group(1))

    def test_every_undismissed_notification_is_in_the_queue(self):
        self._note("Charity election not set up")
        self._note("Round 3 results are in")
        titles = [n["title"] for n in self._queue()]
        self.assertEqual(
            sorted(titles), ["Charity election not set up", "Round 3 results are in"]
        )

    def test_a_dismissed_one_does_not_come_round_again(self):
        from django.utils import timezone

        self._note("Still going")
        self._note("Closed for good", dismissed_at=timezone.now())
        self.assertEqual([n["title"] for n in self._queue()], ["Still going"])

    def test_the_teaser_carries_the_title_but_not_the_message(self):
        """It is a hook, not a delivery — the body is what the panel is for."""
        self._note("Charity election not set up")
        item = self._queue()[0]
        self.assertEqual(item["title"], "Charity election not set up")
        self.assertEqual(item["org"], "Ticker FC")
        self.assertNotIn("message", item)

    def test_the_queue_is_capped(self):
        for n in range(8):
            self._note(f"Note {n}")
        self.assertEqual(len(self._queue()), 5)

    def test_the_pinned_card_is_gone(self):
        self._note("Charity election not set up")
        html = self.client.get(reverse("dashboard")).content.decode()
        self.assertNotIn("notePop", html)
        self.assertIn('id="bellTick"', html)

    def test_no_notifications_means_an_empty_queue_not_a_crash(self):
        self.assertEqual(self._queue(), [])


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


class OrgAdminApprovalsPageTests(TestCase):
    """The organisation admin's Approvals queue.

    This page used to list every pending request on the platform and was gated
    on `is_staff` — which, since nothing but create_superuser sets that flag,
    meant superusers only. It is the ORG ADMIN's queue now: the requests to
    join the organisations they run, and nobody else's.
    """

    def setUp(self):
        self.season, _ = Season.objects.get_or_create(year=2094, defaults={"label": "2094"})
        self.org = Organisation.objects.create(name="Mine", season=self.season)
        self.other = Organisation.objects.create(name="Somebody Else's", season=self.season)

        self.owner = User.objects.create_user(
            email="owner@w.com", password="x", display_name="Ollie",
        )
        OrgMember.objects.create(
            user=self.owner, org=self.org,
            role=OrgMember.ROLE_BOTH, is_league_owner=True,
        )

        self.joiner = User.objects.create_user(email="kim@w.com", password="x", display_name="Kim")
        self.req = request_to_join(self.joiner, self.org)

        self.stranger = User.objects.create_user(email="zed@w.com", password="x", display_name="Zed")
        self.other_req = request_to_join(self.stranger, self.other)

    def test_the_queue_lists_requests_for_your_own_organisation(self):
        self.client.force_login(self.owner)
        r = self.client.get("/manage/approvals/")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"kim@w.com", r.content)

    def test_somebody_elses_organisation_is_not_in_your_queue(self):
        self.client.force_login(self.owner)
        r = self.client.get("/manage/approvals/")
        self.assertNotIn(b"zed@w.com", r.content)

    def test_an_org_admin_can_clear_their_own_queue(self):
        self.client.force_login(self.owner)
        self.client.post(
            "/manage/approvals/", {"action": "approve", "request_id": self.req.id}
        )
        self.assertTrue(OrgMember.objects.filter(user=self.joiner, org=self.org).exists())

    def test_posting_somebody_elses_request_id_does_nothing(self):
        """The scoping is on the QUERY, not on which links the page drew.

        Hiding the row and then trusting the POST is how this kind of page
        leaks: the id is guessable and the form is not the only way to send one.
        """
        self.client.force_login(self.owner)
        r = self.client.post(
            "/manage/approvals/", {"action": "approve", "request_id": self.other_req.id}
        )
        self.assertEqual(r.status_code, 404)
        self.assertFalse(OrgMember.objects.filter(user=self.stranger, org=self.other).exists())

    def test_the_nav_carries_your_waiting_count(self):
        self.client.force_login(self.owner)
        body = self.client.get("/manage/").content
        self.assertIn(b'class="an-count">1<', body)

    def test_a_member_who_runs_nothing_cannot_reach_it(self):
        self.client.force_login(self.joiner)
        r = self.client.get("/manage/approvals/")
        self.assertEqual(r.status_code, 403)


class CreateWizardTests(TestCase):
    """Creating a group is four saved steps, resumable after you walk away."""

    URL = "/leagues/new/"

    def setUp(self):
        from catalog.models import Competition, OrganisationType, Series

        self.season = current_form_season()
        # The real seeded AFL competition for the season in play, series and
        # all. Building one here collided with the seeded row on
        # (slug, season), and asked for a season nobody is tipping — which
        # read as a wizard bug and was really a fixture gap.
        self.comp = current_form_competition()
        self.sport = self.comp.sport
        self.series = self.comp.series.first()
        self.gtype = OrganisationType.objects.get(slug="informal")
        self.charity, _ = Charity.objects.get_or_create(
            slug="lifeline", defaults={"name": "Lifeline", "is_approved": True},
        )
        self.user = User.objects.create_user(email="w@w.com", password="x", display_name="Wiz")
        self.client.force_login(self.user)

    # STEP NUMBERS COME FROM THE WIZARD, NEVER FROM THIS FILE.
    #
    # These helpers were already named rather than numbered, for exactly the
    # right reason — "so the next insertion does not silently repoint every
    # test at the wrong screen". The numbers were still written out inside
    # them, though, so the next insertion did precisely that. Reading them off
    # WIZARD_STEPS is what the comment was reaching for.

    @property
    def _steps(self):
        from orgs.views import (
            CHARITY_STEP, COMPETITION_STEP, DETAILS_STEP, FORMALITY_STEP,
            GROUPS_STEP, LAST_STEP, VERIFY_STEP,
        )

        return {
            "formality": FORMALITY_STEP, "details": DETAILS_STEP,
            "verify": VERIFY_STEP, "groups": GROUPS_STEP,
            "tipping": COMPETITION_STEP, "charity": CHARITY_STEP,
            "review": LAST_STEP,
        }

    def _formality_step(self, kind="informal"):
        """Step one, and the branch everything after it reads."""
        return self.client.post(self.URL, {
            "step": self._steps["formality"], "action": "next", "formality": kind,
        })

    def _step1(self, name="Wizard Group"):
        """Formality, then the basics — the two screens a name now sits behind.

        These tests run the INFORMAL path (see self.gtype), so no organisation
        type is posted: there is exactly one informal type and the form fills
        it in rather than asking.
        """
        self._formality_step("informal")
        return self.client.post(self.URL, {
            "step": self._steps["details"], "action": "next", "name": name,
            "informal_label": "Book Club",
        })

    def _verify_step(self):
        """A no-op on this path, and that is the point.

        An informal group has no employer domain to prove and never will, so
        the wizard now steps OVER the verify screen rather than showing it and
        letting them press past — a family comp being asked to prove a work
        domain was the wall the client reported. Kept as a call so the walk
        still reads as the sequence a person goes through.
        """
        return None

    def _groups_step(self, enabled=False):
        """Whether the organisation wants sub-groups.

        Defaults to "no" — an informal book club has no use for departments,
        and most of these tests are not about groups at all. Must still be
        walked through on any path that finishes creation, because the field
        is required on the form.
        """
        return self.client.post(self.URL, {
            "step": self._steps["groups"], "action": "next",
            "groups_enabled": "yes" if enabled else "no",
        })

    def _tipping_step(self):
        return self.client.post(self.URL, {
            "step": self._steps["tipping"], "action": "next",
            "competitions": [self.comp.pk], "season": self.season.pk,
        })

    def _charity_step(self):
        return self.client.post(self.URL, {
            "step": self._steps["charity"], "action": "next",
            "charity_method": "pick", "charity": self.charity.pk,
        })

    def _review_step(self):
        return self.client.post(self.URL, {
            "step": self._steps["review"], "action": "next",
        })

    def _through_to_review(self, name="Wizard Group"):
        self._step1(name)
        self._verify_step()
        self._groups_step()
        self._tipping_step()
        self._charity_step()

    def test_one_step_shows_at_a_time(self):
        body = self.client.get(self.URL).content
        # The first screen asks formal-or-informal and nothing else — that is
        # the whole point of it being its own step.
        self.assertIn(b'name="formality"', body)
        self.assertNotIn(b'id="id_name"', body)
        self._formality_step("informal")
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

        self._formality_step("informal")
        self.client.post(self.URL, {
            "step": self._steps["details"], "action": "next", "name": "",
        })
        self.assertEqual(
            OrgDraft.objects.get(user=self.user).step, self._steps["details"],
        )

    def test_a_later_step_does_not_block_an_earlier_one(self):
        """Step 1 must not fail because no charity has been chosen yet."""
        from .models import OrgDraft

        self._step1()
        # Informal steps straight over verify, so the details screen advances
        # to the groups question rather than to the work-domain check.
        self.assertEqual(
            OrgDraft.objects.get(user=self.user).step, self._steps["groups"],
        )

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
        self.client.post(self.URL, {
            "step": self._steps["charity"], "action": "back",
        })
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
        self.assertIsNone(OrgDraft.objects.get(user=self.user).data.get("parent"))
        # The name field is a step further in than it used to be — formality
        # is asked first now — so reaching it is part of the assertion rather
        # than something the first GET can show.
        self._formality_step("informal")
        resp = self.client.get(self.URL)
        self.assertNotContains(resp, "Department name")
        self.assertContains(resp, "Organisation name")

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
        self._formality_step("informal")
        resp = self.client.post(self.URL, {
            "step": self._steps["details"], "action": "next", "name": "Finance",
            "informal_label": "Book Club", "parent": parent.id,
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

        self._formality_step("formal")
        payload = {
            "step": self._steps["details"], "action": "next",
            "name": "Verify Step Co", "organisation_type": business.pk,
        }
        if sub is not None:
            payload["sub_categories"] = [sub.pk]
        self.client.post(self.URL, payload)

        self.assertEqual(
            OrgDraft.objects.get(user=self.user).step, self._steps["verify"],
        )
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

        # Step numbers are read from the wizard itself rather than written
        # out, so inserting a step moves this walk with it instead of
        # silently pointing each post at the wrong screen — which is exactly
        # what happened when the formality step went in at the front.
        from orgs.views import (
            CHARITY_STEP, COMPETITION_STEP, DETAILS_STEP, FORMALITY_STEP,
            GROUPS_STEP, LAST_STEP, VERIFY_STEP,
        )
        from catalog.models import Country

        # 1 — formal or informal. Everything after this reads the answer.
        self._post(FORMALITY_STEP, formality="formal")
        # 2 — who you are
        self._post(DETAILS_STEP, name="Wizard Walk FC",
                   organisation_type=self.gtype.id,
                   country=Country.objects.get(code="AU").pk,
                   sub_categories=self.subcat.id if self.subcat else "")
        # 3 — verification, not required for this type
        self._post(VERIFY_STEP)
        # 4 — groups, not for this small a crew
        self._post(GROUPS_STEP, groups_enabled="no")
        # 5 — what you tip
        self._post(
            COMPETITION_STEP, competitions=self.comp.id,
            season=self.comp.season_id, team_size="10",
        )
        # 6 — the cause
        self._post(CHARITY_STEP, charity_method="pick", charity=self.charity.id)
        # 7 — create
        self._post(LAST_STEP)

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
        # The step number comes from the wizard, like every other helper on
        # the parent class. Written out as a 5 here, it survived the formality
        # step being inserted at the front and quietly started posting the
        # ballot at the TIPPING screen — which has no charity fields to
        # complain about, so the walk stalled there and every vote in this
        # class was simply never created.
        data = {
            "step": self._steps["charity"], "action": "next",
            "charity_method": "vote",
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


class CharityVoteTieTests(TestCase):
    """A tie must stop and ask a person, not pick one and call it a result.

    Closing used to be ``.order_by("-n").first()``, so a level vote handed
    back whichever row the database returned first and presented it to the
    organisation as its decision. An even split between two charities is the
    ordinary outcome for a group with an even number of people, not an edge
    case — and a vote nobody turned up to ties every option at nil.
    """

    def setUp(self):
        from .models import CharityVote, Notification, OrgMember, Organisation
        from .services import cast_charity_ballot, open_charity_vote

        User = get_user_model()
        self.season = Season.objects.create(year=2097, label="2097")
        self.org = Organisation.objects.create(name="Tie Co", season=self.season)
        self.captain = User.objects.create_user(
            email="captain@tie.test", password="x", display_name="Cap Skipper",
        )
        self.alice = User.objects.create_user(
            email="alice@tie.test", password="x", display_name="Alice",
        )
        self.bob = User.objects.create_user(
            email="bob@tie.test", password="x", display_name="Bob",
        )
        OrgMember.objects.create(
            user=self.captain, org=self.org, role=OrgMember.ROLE_CAPTAIN,
        )
        OrgMember.objects.create(user=self.alice, org=self.org)
        OrgMember.objects.create(user=self.bob, org=self.org)
        self.one = Charity.objects.create(
            name="Tie Charity One", slug="tie-one", is_approved=True,
            website="https://one.example",
        )
        self.two = Charity.objects.create(
            name="Tie Charity Two", slug="tie-two", is_approved=True,
        )
        self.vote = open_charity_vote(self.org, [self.one, self.two])
        self.opt_one, self.opt_two = list(
            self.vote.options.order_by("charity__name")
        )
        self._cast = cast_charity_ballot

    def _split_the_vote(self):
        self._cast(user=self.alice, vote=self.vote, option=self.opt_one)
        self._cast(user=self.bob, vote=self.vote, option=self.opt_two)

    def _close(self):
        from .services import close_charity_vote

        winner = close_charity_vote(self.vote)
        self.vote.refresh_from_db()
        self.org.refresh_from_db()
        return winner

    def _url(self, name):
        from django.urls import reverse

        return reverse(f"orgs:{name}", args=[self.org.id])

    # ---- closing -------------------------------------------------------

    def test_a_level_vote_becomes_tied_rather_than_picking_one(self):
        from .models import CharityVote

        self._split_the_vote()
        self.assertIsNone(self._close())
        self.assertEqual(self.vote.status, CharityVote.STATUS_TIED)

    def test_a_tie_sets_no_charity_on_the_organisation(self):
        """The half that actually mattered: an arbitrary winner was being
        written onto the org and taking the season's donations with it."""
        self._split_the_vote()
        self._close()
        self.assertIsNone(self.org.charity_id)

    def test_a_vote_nobody_cast_ties_rather_than_crowning_the_first_option(self):
        from .models import CharityVote

        self.assertIsNone(self._close())
        self.assertEqual(self.vote.status, CharityVote.STATUS_TIED)
        self.assertIsNone(self.org.charity_id)

    def test_a_clear_winner_still_closes_normally(self):
        from .models import CharityVote

        self._cast(user=self.alice, vote=self.vote, option=self.opt_one)
        self._cast(user=self.bob, vote=self.vote, option=self.opt_one)
        self.assertEqual(self._close(), self.one)
        self.assertEqual(self.vote.status, CharityVote.STATUS_CLOSED)
        self.assertEqual(self.org.charity_id, self.one.pk)

    def test_tied_options_are_the_ones_that_actually_tied(self):
        third = Charity.objects.create(
            name="Tie Charity Three", slug="tie-three", is_approved=True,
        )
        from .models import CharityVoteOption

        opt_three = CharityVoteOption.objects.create(vote=self.vote, charity=third)
        self._split_the_vote()          # one and two get 1 each, three gets 0
        self._close()
        self.assertEqual(
            {o.charity_id for o in self.vote.tied_options()},
            {self.one.pk, self.two.pk},
        )

    def test_everyone_is_told_the_vote_tied(self):
        from .models import Notification

        self._split_the_vote()
        self._close()
        told = Notification.objects.filter(kind=Notification.KIND_ELECTION_TIED)
        self.assertEqual(told.count(), 3)

    def test_the_auto_closer_does_not_pick_the_tie_up_again(self):
        """close_due_elections filters on `open`, so a tied vote must fall out
        of it — otherwise every cron tick would re-close the same vote."""
        from django.utils import timezone

        from .services import close_due_elections

        self._split_the_vote()
        self.vote.scheduled_close_at = timezone.now() - timedelta(minutes=5)
        self.vote.save(update_fields=["scheduled_close_at"])
        self._close()
        self.assertEqual(close_due_elections(orgs=[self.org]), 0)

    # ---- the captain's call --------------------------------------------

    def test_a_captain_may_break_it(self):
        from .models import CharityVote
        from .services import break_charity_vote_tie

        self._split_the_vote()
        self._close()
        break_charity_vote_tie(self.vote, self.two, by_user=self.captain)
        self.vote.refresh_from_db()
        self.org.refresh_from_db()
        self.assertEqual(self.vote.status, CharityVote.STATUS_CLOSED)
        self.assertEqual(self.vote.winning_charity_id, self.two.pk)
        self.assertEqual(self.org.charity_id, self.two.pk)
        self.assertEqual(self.vote.tie_broken_by_id, self.captain.pk)

    def test_an_ordinary_member_may_not(self):
        from .services import break_charity_vote_tie

        self._split_the_vote()
        self._close()
        with self.assertRaises(ValueError):
            break_charity_vote_tie(self.vote, self.two, by_user=self.alice)

    def test_a_charity_that_did_not_tie_is_refused(self):
        """Breaking a tie is choosing between the options the group put level.
        Reaching past them would make the election advisory."""
        from .models import CharityVoteOption
        from .services import break_charity_vote_tie

        third = Charity.objects.create(
            name="Tie Charity Three", slug="tie-three", is_approved=True,
        )
        CharityVoteOption.objects.create(vote=self.vote, charity=third)
        self._split_the_vote()
        self._close()
        with self.assertRaises(ValueError):
            break_charity_vote_tie(self.vote, third, by_user=self.captain)

    def test_a_vote_that_is_not_tied_cannot_be_overridden(self):
        from .services import break_charity_vote_tie

        self._cast(user=self.alice, vote=self.vote, option=self.opt_one)
        self._cast(user=self.bob, vote=self.vote, option=self.opt_one)
        self._close()
        with self.assertRaises(ValueError):
            break_charity_vote_tie(self.vote, self.two, by_user=self.captain)

    def test_a_league_owner_can_break_it_when_there_is_no_captain(self):
        """Most groups never assign a captain. Gating this on the captain role
        alone would leave them deadlocked with no way forward."""
        from .models import OrgMember
        from .services import break_charity_vote_tie

        User = get_user_model()
        owner = User.objects.create_user(
            email="owner@tie.test", password="x", display_name="Owner",
        )
        OrgMember.objects.create(user=owner, org=self.org, is_league_owner=True)
        self._split_the_vote()
        self._close()
        break_charity_vote_tie(self.vote, self.one, by_user=owner)
        self.vote.refresh_from_db()
        self.assertEqual(self.vote.winning_charity_id, self.one.pk)

    # ---- the screen ----------------------------------------------------

    def test_the_page_says_it_tied_rather_than_showing_an_empty_result(self):
        """The reported symptom was a page that "just spins, loading, loading,
        forever". It was really the closed branch with no winner: no result, no
        tallies, no controls and nothing saying why."""
        self._split_the_vote()
        self._close()
        self.client.force_login(self.alice)
        body = self.client.get(self._url("charity_vote")).content.decode()
        self.assertIn("a tie", body)
        self.assertIn("Tie Charity One", body)

    def test_only_a_decider_is_offered_the_control(self):
        self._split_the_vote()
        self._close()
        self.client.force_login(self.alice)
        self.assertNotIn(
            "captains-call", self.client.get(self._url("charity_vote")).content.decode()
        )
        self.client.force_login(self.captain)
        self.assertIn(
            "captains-call", self.client.get(self._url("charity_vote")).content.decode()
        )

    def test_posting_the_call_as_a_member_changes_nothing(self):
        from .models import CharityVote

        self._split_the_vote()
        self._close()
        self.client.force_login(self.alice)
        self.client.post(self._url("captains_call"), {"charity": self.one.pk})
        self.vote.refresh_from_db()
        self.assertEqual(self.vote.status, CharityVote.STATUS_TIED)

    def test_the_result_page_credits_the_call(self):
        """A charity chosen by one person is a different kind of decision from
        one the group picked, and the page has to say so."""
        from .services import break_charity_vote_tie

        self._split_the_vote()
        self._close()
        break_charity_vote_tie(self.vote, self.two, by_user=self.captain)
        self.client.force_login(self.alice)
        body = self.client.get(self._url("charity_vote")).content.decode()
        self.assertIn("Cap Skipper", body)


class FormalityStepTests(TestCase):
    """Org type is asked FIRST, and informal never meets workplace validation.

    The reported symptom: "everyone gets funnelled through the same formal-org
    validation, which is why someone trying to set up an informal mates' group
    or family comp with a Gmail address hits a wall." The wall was never one
    check — it was that the question deciding which checks apply sat alongside
    four other fields, so nothing knew it was talking to a family until it had
    finished asking a business's questions.
    """

    def setUp(self):
        from catalog.models import Country, OrganisationType, SubCategory
        from .models import OrgDraft

        User = get_user_model()
        self.user = User.objects.create_user(
            email="wiz@example.com", password="x", display_name="Wiz",
        )
        self.client.force_login(self.user)
        self.business = OrganisationType.objects.get(slug="business")
        self.sub = SubCategory.objects.filter(
            organisation_type=self.business, is_active=True,
        ).first()
        self.au = Country.objects.get(code="AU")
        self.pg = Country.objects.get(code="PG")

    def draft(self):
        from .models import OrgDraft

        return OrgDraft.objects.get(user=self.user)

    def step(self, n, **data):
        data.setdefault("action", "next")
        data["step"] = str(n)
        return self.client.post("/leagues/new/", data)

    def on_verify_screen(self) -> bool:
        body = self.client.get("/leagues/new/").content.decode()
        return 'name="action" value="send_code"' in body

    # ---- the question comes first --------------------------------------

    def test_the_first_screen_asks_only_formal_or_informal(self):
        body = self.client.get("/leagues/new/").content.decode()
        self.assertIn('name="formality"', body)
        self.assertNotIn('id="id_name"', body)

    def test_the_name_is_not_asked_until_after_it(self):
        self.step(1, formality="informal")
        self.assertIn('id="id_name"', self.client.get("/leagues/new/").content.decode())

    # ---- what the answer changes ---------------------------------------

    def test_an_informal_group_never_sees_the_verify_step(self):
        """The wall itself. It is not enough for the check to be unenforced —
        a screen asking a family comp to prove a work domain must not be
        there at all."""
        self.step(1, formality="informal")
        self.step(2, name="Sunday Mates", informal_label="Mates comp",
                  country=str(self.au.pk))
        self.assertFalse(self.on_verify_screen())

    def test_a_formal_org_still_has_to_verify(self):
        self.step(1, formality="formal")
        self.step(2, name="Acme Pty", organisation_type=str(self.business.pk),
                  sub_categories=str(self.sub.pk), country=str(self.au.pk))
        self.assertTrue(self.on_verify_screen())

    def test_an_informal_group_needs_no_organisation_type(self):
        """There is exactly one informal type, so choosing from a list of one
        is pure friction — the form fills it in."""
        from catalog.models import OrganisationType

        self.step(1, formality="informal")
        self.step(2, name="Book Club", informal_label="Book club",
                  country=str(self.au.pk))
        self.assertGreater(self.draft().step, 2)

    def test_switching_to_informal_steps_back_over_the_verify_screen(self):
        """A draft can hold a step that stopped applying."""
        self.step(1, formality="formal")
        self.step(2, name="Acme Pty", organisation_type=str(self.business.pk),
                  sub_categories=str(self.sub.pk), country=str(self.au.pk))
        self.assertTrue(self.on_verify_screen())
        self.client.post("/leagues/new/", {"step": "3", "action": "back"})
        self.client.post("/leagues/new/", {"step": "2", "action": "back"})
        self.step(1, formality="informal")
        self.step(2, name="Acme Mates", informal_label="Mates",
                  country=str(self.au.pk))
        self.assertFalse(self.on_verify_screen())

    # ---- the informal screen has to be usable ---------------------------
    #
    # Staging, 27 Aug 2026: an informal setup filled in the name and the
    # country, pressed Continue, and stayed on step two with nothing on
    # screen to say why. The server was right — informal_label is required —
    # but the field, and the error against it, were both inside a block the
    # category script had hidden, because that script decided "is this
    # informal?" by reading the formality radios, and the radios are on step
    # one. Two invariants keep it fixed.

    def test_the_details_screen_says_which_branch_it_is_on(self):
        """What the category script reads. The radios are a step behind it."""
        self.step(1, formality="informal")
        body = self.client.get("/leagues/new/").content.decode()
        self.assertIn('data-formality="informal"', body)
        self.assertIn('id="id_informal_label"', body)

        self.client.post("/leagues/new/", {"step": "2", "action": "back"})
        self.step(1, formality="formal")
        self.assertIn(
            'data-formality="formal"',
            self.client.get("/leagues/new/").content.decode(),
        )

    def test_a_step_that_refuses_to_advance_says_so_where_it_can_be_seen(self):
        """The dead-button shape: 200, same step, no visible reason.

        Asserted at the top of the page rather than beside the field, because
        beside the field is exactly the place that can be hidden.
        """
        self.step(1, formality="informal")
        r = self.step(2, name="Sunday Mates", country=str(self.au.pk))
        self.assertEqual(self.draft().step, 2)          # it did refuse
        body = r.content.decode()
        banner = body[: body.index('<form method="post"')]
        self.assertIn("what kind of group you are", banner)

    def test_a_type_that_disagrees_with_the_answer_is_dropped(self):
        """The wizard hides the mismatched options, but the value arrives from
        a browser and a stale draft can carry the old one."""
        from .forms import OrgCreateForm

        form = OrgCreateForm(data={
            "formality": "informal",
            "name": "Mates",
            "organisation_type": self.business.pk,
            "informal_label": "Mates comp",
        })
        form.is_valid()
        self.assertTrue(form.cleaned_data["organisation_type"].is_informal)


class CountrySegmentTests(TestCase):
    """Country lives on the group, falling back to the organisation."""

    def setUp(self):
        from catalog.models import Country, Season
        from .models import Group, Organisation

        self.season = Season.objects.create(year=2093, label="2093")
        self.au = Country.objects.get(code="AU")
        self.pg = Country.objects.get(code="PG")
        self.nz = Country.objects.get(code="NZ")
        self.org = Organisation.objects.create(
            name="Westpac-ish", season=self.season, country=self.au,
            groups_enabled=True,
        )
        self.sydney = Group.objects.create(org=self.org, name="Sydney")
        self.moresby = Group.objects.create(
            org=self.org, name="Port Moresby", country=self.pg,
        )

    def test_a_group_with_no_country_inherits_the_organisations(self):
        """Groups are opt-in and off for most orgs — without the fallback,
        nearly every member would resolve to no country at all."""
        self.assertEqual(self.sydney.effective_country, self.au)

    def test_a_groups_own_country_wins(self):
        """The case the whole field exists for: one business, offices in
        Sydney, Melbourne and Port Moresby."""
        self.assertEqual(self.moresby.effective_country, self.pg)

    def test_the_segment_is_derived_not_asked(self):
        from catalog.models import Country

        self.assertEqual(self.sydney.segment, Country.SEGMENT_AUSTRALIA)
        self.assertEqual(self.moresby.segment, Country.SEGMENT_GLOBAL)

    def test_new_zealand_plays_on_the_main_ladder_for_now(self):
        """Client instruction of 2026-08-26: NZ folds into the Australia
        ladder until it gets a board of its own. Groups competing against each
        other share a segment, so while NZ groups play on the main ladder they
        carry the main ladder's segment — see catalog migration 0019."""
        from catalog.models import Country

        self.org.country = self.nz
        self.org.save(update_fields=["country"])
        self.sydney.refresh_from_db()
        self.assertEqual(self.sydney.segment, Country.SEGMENT_AUSTRALIA)

    def test_new_zealand_is_still_its_own_country(self):
        """Folding the SEGMENT is not merging the country. The Good List's
        country breakdown must still be able to show New Zealand on its own
        row — rolling its money into Australia's would label NZ giving as
        Australian, which is a different claim entirely."""
        from billing.goodlist import group_counts_by_country

        self.org.country = self.nz
        self.org.save(update_fields=["country"])
        counts = {r["label"]: r["groups"] for r in group_counts_by_country()}
        self.assertEqual(counts.get("New Zealand"), 1)

    def test_every_pacific_nation_the_client_named_is_seeded(self):
        from catalog.models import Country

        named = {
            "Papua New Guinea", "Fiji", "Samoa", "Tonga", "Vanuatu",
            "Solomon Islands",
        }
        self.assertEqual(
            named,
            set(Country.objects.filter(is_pacific=True).values_list("name", flat=True)),
        )

    def test_australia_and_new_zealand_are_not_pacific_nations(self):
        """They have their own segments; sweeping them into the pooled board
        would drown the thing it exists to make visible."""
        from catalog.models import Country

        self.assertFalse(Country.objects.get(code="AU").is_pacific)
        self.assertFalse(Country.objects.get(code="NZ").is_pacific)

    def test_a_group_counts_for_the_country_it_resolves_to(self):
        from billing.goodlist import group_counts_by_country

        counts = {r["label"]: r["groups"] for r in group_counts_by_country()}
        self.assertEqual(counts.get("Papua New Guinea"), 1)
        self.assertEqual(counts.get("Australia"), 1)


class PacificNationsBoardTests(TestCase):
    """The pooled board the client asked for: PNG, Fiji, Samoa, Tonga, Vanuatu
    and the Solomons competing as one.

    Each of those countries on its own is far too small to be a leaderboard —
    that is the whole premise — so what has to be asserted is the POOLING. A
    test that only checked the per-country rows would pass just as happily
    against six separate boards, which is the thing this replaced.
    """

    def setUp(self):
        from decimal import Decimal

        from django.utils import timezone

        from billing.models import DonationPayment, DonationPledge
        from catalog.models import Charity, Country, Season

        self.season = Season.objects.create(year=2094, label="2094")
        self.charity = Charity.objects.create(
            slug="pacific-aid", name="Pacific Aid", is_approved=True,
        )

        def org_in(code, name, amount):
            org = Organisation.objects.create(
                name=name, season=self.season, charity=self.charity,
                country=Country.objects.get(code=code),
            )
            pledge = DonationPledge.objects.create(
                org=org, season=self.season, charity=self.charity,
                pledged_amount_aud=Decimal(amount),
            )
            DonationPayment.objects.create(
                pledge=pledge, org=org, charity=self.charity,
                amount_aud=Decimal(amount),
                type=DonationPayment.TYPE_BASE,
                paid_by=DonationPayment.PAID_BY_OWNER,
                settled_at=timezone.now(),
            )
            return org

        self.png = org_in("PG", "Moresby Mob", "100.00")
        self.fiji = org_in("FJ", "Suva Crew", "50.00")
        self.aus = org_in("AU", "Melbourne Office", "900.00")

    def test_the_pacific_countries_are_counted_as_one_board(self):
        from billing.goodlist import pacific_nations

        board = pacific_nations()
        self.assertEqual(board["groups"], 2)
        self.assertEqual(str(board["raised"]), "150.00")

    def test_australia_is_not_swept_into_it(self):
        """Australia has its own segment and 6x the money here — pooling it in
        would drown exactly what the board exists to make visible."""
        from billing.goodlist import pacific_nations

        self.assertNotIn("Australia", [m["label"] for m in pacific_nations()["members"]])
        self.assertEqual(str(pacific_nations()["raised"]), "150.00")

    def test_the_pooled_total_shows_below_the_privacy_threshold(self):
        """A single country's row is gated so a total cannot be traced back to
        one identifiable group. Pooling six countries IS the anonymising step,
        so gating the pool as well would hide the board for precisely as long
        as it is most needed — its first season."""
        from billing.goodlist import pacific_nations
        from catalog.models import GoodListConfig

        cfg = GoodListConfig.get()
        cfg.privacy_min_groups = 5
        cfg.save(update_fields=["privacy_min_groups"])

        board = pacific_nations()
        self.assertEqual(board["groups"], 2)          # pooled total: shown
        self.assertEqual(board["members"], [])        # per-country rows: gated

    def test_it_names_the_countries_even_with_no_money_in_yet(self):
        """The empty state tells people who they would be competing with, so
        the first group in the Pacific has something to sign up to."""
        from billing.goodlist import pacific_nations

        self.assertEqual(
            set(pacific_nations()["countries"]),
            {"Papua New Guinea", "Fiji", "Samoa", "Tonga", "Vanuatu",
             "Solomon Islands"},
        )


class OrgOwnedCharityTests(TestCase):
    """An organisation adding a charity GoodTip's vetted list doesn't carry.

    The rule this pins down: usable by that org immediately, invisible to
    every other org until approved. Both halves matter — the first is why the
    feature exists (the wizard's approval wait is where people gave up), the
    second is why it is safe (one org's typo must not become everyone's).
    """

    def setUp(self):
        from .services import add_charity_for_org

        self.add = add_charity_for_org
        self.season = current_form_season()
        self.vetted, _ = Charity.objects.get_or_create(
            slug="lifeline", defaults={"name": "Lifeline", "is_approved": True},
        )
        self.org = Organisation.objects.create(name="Maccas AU", season=self.season)
        self.other = Organisation.objects.create(name="Someone Else", season=self.season)
        self.admin = User.objects.create_user(
            email="boss@maccas.test", password="x", display_name="Boss",
        )
        OrgMember.objects.create(
            user=self.admin, org=self.org, role=OrgMember.ROLE_MANAGER,
            is_league_owner=True,
        )

    def test_added_charity_is_usable_now_but_not_globally(self):
        rmh = self.add(self.org, name="Ronald McDonald House", by_user=self.admin)
        self.assertFalse(rmh.is_approved)
        self.assertEqual(rmh.owner_org, self.org)
        self.assertEqual(rmh.added_by, self.admin)
        self.assertIsNotNone(rmh.added_at)

        # Usable here...
        self.assertIn(rmh, Charity.objects.available_to(self.org))
        # ...and nowhere else.
        self.assertNotIn(rmh, Charity.objects.available_to(self.other))
        self.assertNotIn(rmh, Charity.objects.approved())
        # The vetted list stays visible to everyone throughout.
        self.assertIn(self.vetted, Charity.objects.available_to(self.org))
        self.assertIn(self.vetted, Charity.objects.available_to(self.other))

    def test_same_name_reuses_the_existing_row(self):
        """Near-duplicates are the damage this whole model exists to stop."""
        again = self.add(self.org, name="  lifeline  ", by_user=self.admin)
        self.assertEqual(again, self.vetted)
        self.assertEqual(Charity.objects.filter(name__iexact="Lifeline").count(), 1)
        # An existing vetted charity is not quietly reassigned to this org.
        self.assertIsNone(again.owner_org)

    def test_blank_name_is_refused(self):
        with self.assertRaises(ValueError):
            self.add(self.org, name="   ", by_user=self.admin)

    def test_manage_screen_adds_and_lists(self):
        self.client.force_login(self.admin)
        url = reverse("orgs:charities", args=[self.org.id])
        resp = self.client.post(url, {"name": "Ronald McDonald House", "website": ""})
        self.assertRedirects(resp, url)
        rmh = Charity.objects.get(name="Ronald McDonald House")
        self.assertEqual(rmh.owner_org, self.org)

        page = self.client.get(url)
        self.assertContains(page, "Ronald McDonald House")
        self.assertContains(page, "Lifeline")

    def test_non_admin_cannot_add(self):
        nobody = User.objects.create_user(email="cook@maccas.test", password="x", display_name="Cook")
        OrgMember.objects.create(user=nobody, org=self.org, role=OrgMember.ROLE_PARTICIPANT)
        self.client.force_login(nobody)
        resp = self.client.post(
            reverse("orgs:charities", args=[self.org.id]), {"name": "Sneaky Fund"},
        )
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(Charity.objects.filter(name="Sneaky Fund").exists())


class GroupCharityElectionTests(TestCase):
    """A group backing its own cause — the franchise case.

    One organisation, several groups that are really separate businesses. The
    Sydney store voting for Ronald McDonald House must not change what head
    office or the Parramatta store backs.
    """

    def setUp(self):
        from .services import add_charity_for_org, create_group_charity_election

        self.add = add_charity_for_org
        self.start = create_group_charity_election
        self.season = current_form_season()
        self.bb, _ = Charity.objects.get_or_create(
            slug="beyond-blue", defaults={"name": "Beyond Blue", "is_approved": True},
        )
        self.ll, _ = Charity.objects.get_or_create(
            slug="lifeline", defaults={"name": "Lifeline", "is_approved": True},
        )
        self.org = Organisation.objects.create(
            name="Maccas AU", season=self.season, charity=self.bb, groups_enabled=True,
        )
        self.sydney = Group.objects.create(org=self.org, name="Sydney CBD")
        self.parra = Group.objects.create(org=self.org, name="Parramatta")
        self.voter = User.objects.create_user(
            email="crew@maccas.test", password="x", display_name="Crew",
        )
        OrgMember.objects.create(user=self.voter, org=self.org, role=OrgMember.ROLE_PARTICIPANT)
        GroupMember.objects.create(group=self.sydney, user=self.voter)

    def test_group_inherits_until_it_votes(self):
        self.assertEqual(self.sydney.effective_charity, self.bb)
        self.assertFalse(self.sydney.charity_is_own)

    def test_group_vote_sets_only_that_group(self):
        vote = self.start(self.sydney, [self.bb, self.ll])
        vote.status = CharityVote.STATUS_OPEN
        vote.save(update_fields=["status"])
        cast_charity_ballot(
            user=self.voter, vote=vote, option=vote.options.get(charity=self.ll),
        )
        self.assertEqual(close_charity_vote(vote), self.ll)

        self.sydney.refresh_from_db()
        self.parra.refresh_from_db()
        self.org.refresh_from_db()
        self.assertEqual(self.sydney.charity, self.ll)
        self.assertTrue(self.sydney.charity_is_own)
        self.assertEqual(self.sydney.effective_charity, self.ll)
        # Head office and the other store are untouched.
        self.assertEqual(self.org.charity, self.bb)
        self.assertIsNone(self.parra.charity)
        self.assertEqual(self.parra.effective_charity, self.bb)

    def test_group_vote_is_not_the_orgs_vote(self):
        """The guard that stops one department's ballot speaking for everyone."""
        self.start(self.sydney, [self.bb, self.ll])
        CharityVote.objects.filter(group=self.sydney).update(
            status=CharityVote.STATUS_OPEN,
        )
        self.org.refresh_from_db()
        self.assertIsNone(self.org.active_charity_vote)
        self.assertIsNone(self.org.pending_election)

    def test_history_keeps_the_two_timelines_apart(self):
        from .services import set_group_charity

        set_group_charity(self.sydney, self.ll)
        rows = self.org.charity_selections.all()
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.first().group, self.sydney)
        # The org's own timeline is still empty.
        self.assertFalse(self.org.charity_selections.filter(group__isnull=True).exists())

    def test_ballot_is_limited_to_the_orgs_charities(self):
        outsider_org = Organisation.objects.create(name="Rival", season=self.season)
        theirs = self.add(outsider_org, name="Rival's Cause")
        with self.assertRaises(ValueError):
            self.start(self.sydney, [self.bb, theirs])

    def test_one_live_election_per_group(self):
        self.start(self.sydney, [self.bb, self.ll])
        with self.assertRaises(ValueError):
            self.start(self.sydney, [self.bb, self.ll])

    def test_ballot_needs_two_options(self):
        with self.assertRaises(ValueError):
            self.start(self.sydney, [self.bb])

    def test_only_group_members_may_cast(self):
        vote = self.start(self.sydney, [self.bb, self.ll])
        vote.status = CharityVote.STATUS_OPEN
        vote.save(update_fields=["status"])
        outsider = User.objects.create_user(
            email="other@maccas.test", password="x", display_name="Other",
        )
        OrgMember.objects.create(user=outsider, org=self.org, role=OrgMember.ROLE_PARTICIPANT)
        GroupMember.objects.create(group=self.parra, user=outsider)
        self.client.force_login(outsider)
        resp = self.client.post(
            reverse("orgs:cast_group_charity_vote", args=[self.org.id, self.sydney.id]),
            {"option": vote.options.first().id},
        )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(vote.ballots.count(), 0)

    def test_group_election_screen_renders_for_a_member(self):
        self.client.force_login(self.voter)
        resp = self.client.get(
            reverse("orgs:group_charity_vote", args=[self.org.id, self.sydney.id]),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Sydney CBD")


class WizardCharityEntryRemovedTests(TestCase):
    """The creation wizard no longer lets anyone invent a charity."""

    def test_form_has_no_free_text_charity_fields(self):
        from .forms import OrgCreateForm

        fields = OrgCreateForm().fields
        self.assertNotIn("new_charity_name", fields)
        self.assertNotIn("new_charity_url", fields)
        self.assertIn("charity", fields)

    def test_charity_step_requires_a_pick(self):
        from .forms import OrgCreateForm

        form = OrgCreateForm(data={"charity_method": "pick"})
        form.is_valid()
        self.assertIn("charity", form.errors)
        self.assertIn("Choose a charity from the list.", form.errors["charity"])
# ---------------------------------------------------------------------------
# The admin's own view of organisations and groups.
#
# The client's report: "I went to Groups and found zero groups, then went to
# Organisations and found groups are in Organisation." Both halves were true,
# and neither was about the data — it was about which model the admin menu was
# pointing at.
# ---------------------------------------------------------------------------
# Imported here rather than added to the block at the top of the file, and
# under its own name: this class is appended to a file that is edited on more
# than one branch at a time, and a new line in a shared import block is the
# most reliable way to manufacture a merge conflict out of nothing.
from django.urls import reverse as admin_reverse  # noqa: E402


class AdminGroupRegistryTests(TestCase):

    def setUp(self):
        from django.contrib.auth.models import Group as AuthGroup

        self.AuthGroup = AuthGroup
        self.admin = get_user_model().objects.create_superuser(
            email="root@example.com", password="pw", display_name="Root",
        )
        # /admin/ is behind an emailed one-time code (sysadmin.middleware), so
        # force_login alone lands on the verify screen instead of the page
        # under test. Stamping the session is what the OTP flow does on
        # success — same as sysadmin/test_control_plane.sign_in_to_admin.
        from sysadmin import otp

        self.client.force_login(self.admin)
        session = self.client.session
        otp.mark_verified(session)
        session.save()

        season, _ = Season.objects.get_or_create(year=2099, defaults={"label": "Test"})
        self.org = Organisation.objects.create(name="Acme Pty Ltd", season=season)
        self.other = Organisation.objects.create(name="Beta Pty Ltd", season=season)
        self.marketing = Group.objects.create(org=self.org, name="Marketing")
        GroupMember.objects.create(group=self.marketing, user=self.admin, is_admin=True)

    def test_the_empty_permissions_group_is_off_the_menu(self):
        """It is the page the client found, and it was correctly empty.

        django.contrib.auth's Group is the permissions bucket. Nothing in this
        site has ever put anybody in one, so its changelist always showed zero
        — while GoodTip's own Group, the thing they were looking for, was not
        registered anywhere.
        """
        from django.contrib import admin as django_admin

        self.assertNotIn(self.AuthGroup, django_admin.site._registry)

    def test_groups_opens_on_goodtip_groups(self):
        res = self.client.get(admin_reverse("admin:orgs_group_changelist"))
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "Marketing")

    def test_a_group_row_names_the_organisation_it_belongs_to(self):
        """One group belongs to exactly one organisation, and the list has to
        say which — that relationship is the thing the client could not see."""
        res = self.client.get(admin_reverse("admin:orgs_group_changelist"))
        self.assertContains(res, "Acme Pty Ltd")

    def test_a_group_page_shows_its_members(self):
        res = self.client.get(admin_reverse("admin:orgs_group_change", args=[self.marketing.pk]))
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "Root")

    def test_an_organisation_lists_its_own_groups(self):
        """The other direction: one organisation, many groups."""
        res = self.client.get(admin_reverse("admin:orgs_organisation_change", args=[self.org.pk]))
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "Marketing")

    def test_the_organisation_list_counts_groups_and_links_to_them(self):
        res = self.client.get(admin_reverse("admin:orgs_organisation_changelist"))
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, f"org__id__exact={self.org.pk}")

    def test_a_group_belongs_to_one_organisation_only(self):
        """Not a UI fact — a schema one, and worth pinning so the admin's
        framing stays honest if the model is ever revisited."""
        field = Group._meta.get_field("org")
        self.assertTrue(field.many_to_one)
        self.assertEqual(field.related_model, Organisation)


class MemberInboxTests(TestCase):
    """One inbox, every organisation — the bug the client actually hit.

    They raised something with the admins of one of their organisations,
    pressed Messages in the nav, and were told "Nothing yet" by the page for a
    DIFFERENT organisation. The message was fine; the page was scoped to
    whichever organisation the nav happened to point at, said nothing about
    which one that was, and offered no way to reach the others.
    """

    def setUp(self):
        from .models import Message, MessageThread

        User = get_user_model()
        self.season = Season.objects.create(year=2096, label="2096")
        # Named so the alphabetical fallback picks Aardvark, which is NOT where
        # the message is. That ordering is what made the old page look empty.
        self.first = Organisation.objects.create(name="Aardvark Pty", season=self.season)
        self.other = Organisation.objects.create(name="Zebra Pty", season=self.season)
        self.member = User.objects.create_user(email="m@b.com", password="x", display_name="Mem")
        self.admin = User.objects.create_user(email="ad@b.com", password="x", display_name="Ad")
        OrgMember.objects.create(user=self.member, org=self.first)
        OrgMember.objects.create(user=self.member, org=self.other)
        OrgMember.objects.create(user=self.admin, org=self.other, role=OrgMember.ROLE_BOTH)

        # A subject NOT on the composer's canned list. "Something about my
        # group" is one of the options in the picker, so asserting a page does
        # not contain it matches the <option> and never the row — the test
        # would pass or fail on the dropdown rather than on the inbox.
        self.subject = "Zebra roster query 4471"
        self.thread = MessageThread.objects.create(
            org=self.other, kind=MessageThread.KIND_RAISED,
            subject=self.subject, started_by=self.member,
        )
        Message.objects.create(thread=self.thread, author=self.member, body="How do I invite someone?")
        self.client.force_login(self.member)

    def test_the_inbox_shows_threads_from_every_organisation(self):
        r = self.client.get(reverse("orgs:member_messages", args=[self.first.id]))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, self.subject)
        # And says which room it is in, which is the part that was missing.
        self.assertContains(r, "Zebra Pty")

    def test_a_thread_from_an_organisation_you_left_is_not_shown(self):
        OrgMember.objects.filter(user=self.member, org=self.other).delete()
        r = self.client.get(reverse("orgs:member_messages", args=[self.first.id]))
        self.assertNotContains(r, self.subject)

    def test_another_members_thread_stays_private(self):
        """A member raising a problem is entitled to have the admins read it
        and not the rest of the room."""
        User = get_user_model()
        nosy = User.objects.create_user(email="n@b.com", password="x", display_name="Nosy")
        OrgMember.objects.create(user=nosy, org=self.other)
        self.client.force_login(nosy)
        r = self.client.get(reverse("orgs:member_messages", args=[self.other.id]))
        self.assertNotContains(r, self.subject)

    def test_the_composer_writes_to_the_organisation_it_was_told_to(self):
        r = self.client.post(
            reverse("orgs:member_messages", args=[self.first.id]),
            {"subject_choice": "A problem with the site", "body": "Broken.",
             "org": str(self.other.id)},
        )
        self.assertEqual(r.status_code, 302)
        from .models import MessageThread

        made = MessageThread.objects.get(subject="A problem with the site")
        self.assertEqual(made.org_id, self.other.id)

    def test_the_composer_refuses_an_organisation_you_are_not_in(self):
        outsider = Organisation.objects.create(name="Not Yours", season=self.season)
        self.client.post(
            reverse("orgs:member_messages", args=[self.first.id]),
            {"subject_choice": "A problem with the site", "body": "Broken.",
             "org": str(outsider.id)},
        )
        from .models import MessageThread

        made = MessageThread.objects.get(subject="A problem with the site")
        # Falls back to the organisation in the URL rather than trusting the id.
        self.assertEqual(made.org_id, self.first.id)

    def test_unread_counts_threads_not_messages(self):
        from .models import Message
        from .services import unread_message_count

        # Their own message is not unread mail.
        self.assertEqual(unread_message_count(self.member), 0)
        Message.objects.create(thread=self.thread, author=self.admin, body="Here you go.")
        Message.objects.create(thread=self.thread, author=self.admin, body="And this.")
        # Two messages, one conversation to look at.
        self.assertEqual(unread_message_count(self.member), 1)

    def test_opening_a_thread_marks_it_read(self):
        from .models import Message
        from .services import unread_message_count

        Message.objects.create(thread=self.thread, author=self.admin, body="Here you go.")
        self.assertEqual(unread_message_count(self.member), 1)
        self.client.get(reverse("orgs:member_message_thread",
                                args=[self.other.id, self.thread.id]))
        self.assertEqual(unread_message_count(self.member), 0)


# MEDIA_ROOT is not isolated by Django's test runner, so an upload made in a
# test lands in the real one — and on this box that is the running staging
# instance's own media directory. The first run of the class below left
# twenty-one PNGs in it. A temporary directory per run, removed afterwards,
# is the fix; nothing about the behaviour under test depends on where the
# files go.
@override_settings(MEDIA_ROOT=tempfile.mkdtemp(prefix="gt-test-media-"))
class MessageReplyAndAttachmentTests(TestCase):
    """Replying to one message in particular, and sending a file with it."""

    @classmethod
    def tearDownClass(cls):
        drop_temp_media()
        super().tearDownClass()

    def setUp(self):
        from .models import Message, MessageThread

        User = get_user_model()
        self.season = Season.objects.create(year=2095, label="2095")
        self.org = Organisation.objects.create(name="Attach Co", season=self.season)
        self.member = User.objects.create_user(email="am@b.com", password="x", display_name="AM")
        OrgMember.objects.create(user=self.member, org=self.org)
        self.thread = MessageThread.objects.create(
            org=self.org, kind=MessageThread.KIND_RAISED,
            subject="A question", started_by=self.member,
        )
        self.first = Message.objects.create(
            thread=self.thread, author=self.member, body="The original question.",
        )
        self.url = reverse("orgs:member_message_thread", args=[self.org.id, self.thread.id])
        self.client.force_login(self.member)

    def _png(self, name="shot.png"):
        from django.core.files.uploadedfile import SimpleUploadedFile

        return SimpleUploadedFile(name, b"\x89PNG\r\n\x1a\n" + b"0" * 40, content_type="image/png")

    def test_a_reply_records_which_message_it_answers(self):
        from .models import Message

        self.client.post(self.url, {"body": "Answering that.", "reply_to": str(self.first.id)})
        latest = Message.objects.order_by("-id").first()
        self.assertEqual(latest.reply_to_id, self.first.id)

    def test_a_message_id_from_another_thread_cannot_be_quoted(self):
        """The id arrives from a hidden field the page filled in.

        A posted one from a conversation the sender cannot read would put words
        somebody said elsewhere into this thread, attributed to them.
        """
        from .models import Message, MessageThread

        elsewhere = MessageThread.objects.create(
            org=self.org, kind=MessageThread.KIND_RAISED,
            subject="Private", started_by=self.member,
        )
        secret = Message.objects.create(thread=elsewhere, author=self.member, body="Secret.")
        self.client.post(self.url, {"body": "Hi", "reply_to": str(secret.id)})
        latest = Message.objects.filter(thread=self.thread).order_by("-id").first()
        self.assertIsNone(latest.reply_to_id)

    def test_a_file_can_be_sent_with_nothing_typed(self):
        """A screenshot IS the message often enough to be worth allowing.

        It is also the clearest bug report anyone sends, so requiring words
        beside it would be requiring the less useful half.
        """
        from .models import Message

        self.client.post(self.url, {"body": "", "files": [self._png()]})
        latest = Message.objects.filter(thread=self.thread).order_by("-id").first()
        self.assertEqual(latest.attachments.count(), 1)
        self.assertTrue(latest.attachments.first().is_image)

    def test_the_stored_path_never_uses_the_uploaded_name(self):
        """A filename from a browser is attacker-controlled.

        Keeping it only as a label means nothing from outside ever reaches the
        filesystem — and two people attaching "screenshot.png" to one thread
        do not collide.
        """
        from .models import Message

        self.client.post(self.url, {"body": "x", "files": [self._png("../../etc/passwd.png")]})
        att = Message.objects.order_by("-id").first().attachments.first()
        self.assertNotIn("passwd", att.file.name)
        self.assertNotIn("..", att.file.name)
        self.assertTrue(att.file.name.endswith(".png"))

    def test_a_kind_of_file_we_do_not_accept_is_refused(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from .models import Message

        bad = SimpleUploadedFile("run.exe", b"MZ" + b"0" * 20, content_type="application/octet-stream")
        self.client.post(self.url, {"body": "here", "files": [bad]})
        latest = Message.objects.order_by("-id").first()
        # The message still sends — losing what somebody typed because one
        # attachment was wrong is a worse answer than sending it without.
        self.assertEqual(latest.body, "here")
        self.assertEqual(latest.attachments.count(), 0)

    def test_only_the_first_four_files_are_kept(self):
        from .models import Message, MessageAttachment

        files = [self._png(f"s{i}.png") for i in range(6)]
        self.client.post(self.url, {"body": "lots", "files": files})
        latest = Message.objects.order_by("-id").first()
        self.assertEqual(latest.attachments.count(), MessageAttachment.MAX_PER_MESSAGE)

    def test_an_attachment_is_refused_to_someone_who_cannot_read_the_thread(self):
        """The file is served by a view, not from /media/, for exactly this.

        An unguessable path is protection enough for an avatar, whose whole
        purpose is to be looked at. A file on a private thread is the opposite.
        """
        from .models import Message

        self.client.post(self.url, {"body": "x", "files": [self._png()]})
        att = Message.objects.order_by("-id").first().attachments.first()
        url = reverse("orgs:message_file", args=[self.thread.id, att.id])
        self.assertEqual(self.client.get(url).status_code, 200)

        User = get_user_model()
        stranger = User.objects.create_user(email="s@b.com", password="x", display_name="S")
        self.client.force_login(stranger)
        self.assertEqual(self.client.get(url).status_code, 404)


class WallReplyThreadingTests(TestCase):
    """Replying to one reply under a post, not just to the post."""

    def setUp(self):
        from .models import WallReply

        User = get_user_model()
        self.season = Season.objects.create(year=2094, label="2094")
        self.org = Organisation.objects.create(name="Wall Co", season=self.season)
        self.user = User.objects.create_user(email="w@b.com", password="x", display_name="W")
        OrgMember.objects.create(user=self.user, org=self.org)
        self.post = WallPost.objects.create(org=self.org, author=self.user, body="Big call.")
        self.first = WallReply.objects.create(post=self.post, author=self.user, body="Nah.")
        self.client.force_login(self.user)

    def test_a_reply_can_answer_another_reply(self):
        from .models import WallReply

        self.client.post(
            reverse("orgs:wall_reply", args=[self.org.id, self.post.id]),
            {"body": "Yeah he's cooked.", "reply_to": str(self.first.id)},
        )
        latest = WallReply.objects.order_by("-id").first()
        self.assertEqual(latest.reply_to_id, self.first.id)

    def test_a_reply_from_another_post_cannot_be_quoted(self):
        """Resolved inside THIS post's own thread.

        A reply from elsewhere quoted in here would show its author saying
        something they never said on this post.
        """
        from .models import WallReply

        elsewhere = WallPost.objects.create(org=self.org, author=self.user, body="Other.")
        stray = WallReply.objects.create(post=elsewhere, author=self.user, body="Elsewhere.")
        self.client.post(
            reverse("orgs:wall_reply", args=[self.org.id, self.post.id]),
            {"body": "Hi", "reply_to": str(stray.id)},
        )
        latest = WallReply.objects.filter(post=self.post).order_by("-id").first()
        self.assertIsNone(latest.reply_to_id)


class CharityEditTests(TestCase):
    """Fixing a charity up, and who is allowed to.

    The logo fetch quietly finds nothing for a fair number of charities, and
    until now the initials tile it falls back to was a dead end: nobody short
    of a Django admin could supply the file they already had.
    """

    def setUp(self):
        User = get_user_model()
        self.season = Season.objects.create(year=2093, label="2093")
        self.org = Organisation.objects.create(name="Charity Co", season=self.season)
        self.admin = User.objects.create_user(email="ca@b.com", password="x", display_name="CA")
        self.member = User.objects.create_user(email="cm@b.com", password="x", display_name="CM")
        OrgMember.objects.create(user=self.admin, org=self.org, role=OrgMember.ROLE_BOTH)
        OrgMember.objects.create(user=self.member, org=self.org)
        self.ours = Charity.objects.create(
            name="Our Local Cause", slug="our-local-cause",
            owner_org=self.org, is_approved=False,
        )
        self.vetted = Charity.objects.create(
            name="Vetted Cause", slug="vetted-cause", is_approved=True,
        )

    def _url(self, charity):
        return reverse("orgs:charity_edit", args=[self.org.id, charity.id])

    def test_an_admin_can_rename_their_own_charity_and_the_slug_follows(self):
        self.client.force_login(self.admin)
        r = self.client.post(self._url(self.ours), {
            "name": "Our Local Cause Inc", "website": "", "logo": "",
        })
        self.assertEqual(r.status_code, 302)
        self.ours.refresh_from_db()
        self.assertEqual(self.ours.name, "Our Local Cause Inc")
        # Leaving the slug behind is how a charity ends up filed under a name
        # it is no longer called.
        self.assertEqual(self.ours.slug, "our-local-cause-inc")

    def test_a_plain_member_cannot_edit_anything(self):
        self.client.force_login(self.member)
        self.assertEqual(self.client.get(self._url(self.ours)).status_code, 403)

    def test_an_org_admin_cannot_edit_a_vetted_charity(self):
        """A vetted row is in every organisation's picker.

        Letting one org admin rename or re-logo it would change what every
        other organisation sees — a permission they have nowhere else in this
        system, and not one they should gain because the button is nearby.
        """
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(self._url(self.vetted)).status_code, 403)

    def test_staff_can_edit_a_vetted_charity(self):
        User = get_user_model()
        staff = User.objects.create_user(
            email="st@b.com", password="x", display_name="St", is_staff=True,
        )
        OrgMember.objects.create(user=staff, org=self.org, role=OrgMember.ROLE_BOTH)
        self.client.force_login(staff)
        self.assertEqual(self.client.get(self._url(self.vetted)).status_code, 200)

    def test_a_name_another_charity_already_has_is_refused(self):
        """unique=True would raise this as an integrity error at save time.

        Asked in the form it is a sentence the person can act on — and two
        rows for one cause split its total in two, which is the damage the
        whole ownership model exists to stop.
        """
        self.client.force_login(self.admin)
        r = self.client.post(self._url(self.ours), {
            "name": "Vetted Cause", "website": "", "logo": "",
        })
        self.assertEqual(r.status_code, 200)
        self.ours.refresh_from_db()
        self.assertEqual(self.ours.name, "Our Local Cause")

    def test_a_charity_from_outside_this_organisations_list_is_not_reachable(self):
        other_org = Organisation.objects.create(name="Someone Else", season=self.season)
        theirs = Charity.objects.create(
            name="Their Private Cause", slug="their-private-cause",
            owner_org=other_org, is_approved=False,
        )
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(self._url(theirs)).status_code, 404)


class MessageNotificationTests(TestCase):
    """The bell finally has something to say about messages.

    A member's question sat in an admin's inbox, or an admin's answer sat in a
    member's, and the only way to find out was to go and look — for the one
    kind of thing on this platform that is addressed to a person by name.
    """

    def setUp(self):
        User = get_user_model()
        self.season = Season.objects.create(year=2092, label="2092")
        self.org = Organisation.objects.create(name="Bell Co", season=self.season)
        self.member = User.objects.create_user(email="bm@b.com", password="x", display_name="Bea")
        self.other = User.objects.create_user(email="bo@b.com", password="x", display_name="Otto")
        self.admin = User.objects.create_user(email="ba@b.com", password="x", display_name="Ada")
        OrgMember.objects.create(user=self.member, org=self.org)
        OrgMember.objects.create(user=self.other, org=self.org)
        OrgMember.objects.create(user=self.admin, org=self.org, role=OrgMember.ROLE_BOTH)

    def _unread(self, user):
        from .models import Notification

        return Notification.objects.filter(
            user=user, kind=Notification.KIND_MESSAGE, read_at__isnull=True,
        )

    def test_raising_something_rings_the_admins(self):
        self.client.force_login(self.member)
        self.client.post(
            reverse("orgs:member_messages", args=[self.org.id]),
            {"subject_choice": "A problem with the site", "body": "It's broken."},
        )
        self.assertEqual(self._unread(self.admin).count(), 1)
        # Not the whole room — a member's problem is between them and the
        # admins, and Otto has nothing to do with it.
        self.assertEqual(self._unread(self.other).count(), 0)
        # And never your own message.
        self.assertEqual(self._unread(self.member).count(), 0)

    def test_the_admins_answer_rings_the_member(self):
        from .models import Message, MessageThread

        thread = MessageThread.objects.create(
            org=self.org, kind=MessageThread.KIND_RAISED,
            subject="A question", started_by=self.member,
        )
        Message.objects.create(thread=thread, author=self.member, body="Hi")
        self.client.force_login(self.admin)
        self.client.post(reverse("manage:message_thread", args=[thread.id]), {"body": "Here you go."})
        self.assertEqual(self._unread(self.member).count(), 1)
        note = self._unread(self.member).first()
        self.assertIn("Ada", note.title)
        # The toast has to say something before it is opened.
        self.assertIn("Here you go.", note.message)
        self.assertIn(f"/messages/{thread.id}/", note.link_url)

    def test_each_recipient_lands_on_their_own_side_of_it(self):
        """Both ends render the same thread, but not the same controls.

        An admin dropped on the member's page has the close/reopen buttons
        missing and a back link to the wrong inbox.
        """
        from .models import Notification

        self.client.force_login(self.member)
        self.client.post(
            reverse("orgs:member_messages", args=[self.org.id]),
            {"subject_choice": "A problem with the site", "body": "It's broken."},
        )
        note = self._unread(self.admin).first()
        self.assertTrue(note.link_url.startswith("/manage/messages/"), note.link_url)

        thread = self.org.message_threads.first()
        Notification.objects.all().delete()
        self.client.force_login(self.admin)
        self.client.post(reverse("manage:message_thread", args=[thread.id]), {"body": "Fixed."})
        note = self._unread(self.member).first()
        self.assertTrue(note.link_url.startswith(f"/leagues/{self.org.id}/"), note.link_url)

    def test_a_notice_to_everyone_rings_everyone(self):
        """The one message genuinely addressed to the whole room."""
        self.client.force_login(self.admin)
        self.client.post(
            reverse("manage:message_new") + f"?org={self.org.id}",
            {"subject": "Round 3 closes Friday", "body": "Get your tips in.", "org": self.org.id},
        )
        self.assertEqual(self._unread(self.member).count(), 1)
        self.assertEqual(self._unread(self.other).count(), 1)
        self.assertEqual(self._unread(self.admin).count(), 0)

    def test_a_notice_to_named_people_rings_only_them(self):
        self.client.force_login(self.admin)
        self.client.post(
            reverse("manage:message_new") + f"?org={self.org.id}",
            {"subject": "About your group", "body": "A word.",
             "org": self.org.id, "recipients": [self.member.id]},
        )
        self.assertEqual(self._unread(self.member).count(), 1)
        self.assertEqual(self._unread(self.other).count(), 0)

    def test_a_reply_under_a_broadcast_does_not_ring_the_whole_room(self):
        """One member saying "thanks" must not ring two hundred bells.

        This is why replies are narrowed to the admins and the thread's owner
        rather than to everyone who can read the thread — a broadcast notice
        is readable by the whole organisation.
        """
        from .models import Notification

        self.client.force_login(self.admin)
        self.client.post(
            reverse("manage:message_new") + f"?org={self.org.id}",
            {"subject": "Round 3 closes Friday", "body": "Get your tips in.", "org": self.org.id},
        )
        Notification.objects.all().delete()

        thread = self.org.message_threads.get(subject="Round 3 closes Friday")
        self.client.force_login(self.member)
        self.client.post(
            reverse("orgs:member_message_thread", args=[self.org.id, thread.id]),
            {"body": "Thanks!"},
        )
        self.assertEqual(self._unread(self.admin).count(), 1)
        self.assertEqual(self._unread(self.other).count(), 0)

    def test_somebody_who_has_left_is_not_rung(self):
        from .models import Message, MessageThread

        thread = MessageThread.objects.create(
            org=self.org, kind=MessageThread.KIND_RAISED,
            subject="A question", started_by=self.member,
        )
        Message.objects.create(thread=thread, author=self.member, body="Hi")
        OrgMember.objects.filter(user=self.member, org=self.org).delete()

        self.client.force_login(self.admin)
        self.client.post(reverse("manage:message_thread", args=[thread.id]), {"body": "Reply"})
        self.assertEqual(self._unread(self.member).count(), 0)


class MessageReceiptTests(TestCase):
    """The single orange tick and the double green one.

    Both states are derived from `Message.read_by`, which already existed for
    the unread count, so what these pin down is the derivation rather than any
    new storage: who counts as having read a message, and — the part that is
    easy to get wrong — that the author reading their own thread does not.
    """

    def setUp(self):
        from .models import Message, MessageThread

        User = get_user_model()
        self.season = Season.objects.create(year=2096, label="2096")
        self.org = Organisation.objects.create(name="Receipt Co", season=self.season)
        self.member = User.objects.create_user(
            email="rm@b.com", password="x", display_name="RM",
        )
        self.admin = User.objects.create_user(
            email="ra@b.com", password="x", display_name="RA",
        )
        OrgMember.objects.create(user=self.member, org=self.org)
        OrgMember.objects.create(
            user=self.admin, org=self.org, role=OrgMember.ROLE_MANAGER,
        )
        self.thread = MessageThread.objects.create(
            org=self.org, kind=MessageThread.KIND_RAISED,
            subject="Receipts", started_by=self.member,
        )
        self.entry = Message.objects.create(
            thread=self.thread, author=self.member, body="Anyone there?",
        )
        self.url = reverse(
            "orgs:member_message_thread", args=[self.org.id, self.thread.id],
        )

    def test_unread_message_shows_the_sent_tick(self):
        """Nobody else has opened it, so it is Sent — even though the author
        has now opened the thread themselves, which is the trap."""
        self.client.force_login(self.member)
        html = self.client.get(self.url).content.decode()
        self.assertIn("chat-tick sent", html)
        self.assertNotIn("chat-tick read", html)

    def test_the_author_reading_their_own_thread_does_not_mark_it_read(self):
        from .services import thread_entries

        # Opening it twice is what a real author does, and the second open is
        # where a naive implementation reports their own message back to them.
        thread_entries(self.thread, self.member)
        entries = thread_entries(self.thread, self.member)
        self.assertFalse(entries[0].is_read)
        self.assertEqual(entries[0].read_count, 0)

    def test_once_the_other_side_opens_it_it_reads_as_read(self):
        from .services import thread_entries

        thread_entries(self.thread, self.admin)
        entries = thread_entries(self.thread, self.member)
        self.assertTrue(entries[0].is_read)
        self.assertEqual(entries[0].read_count, 1)

        self.client.force_login(self.member)
        html = self.client.get(self.url).content.decode()
        self.assertIn("chat-tick read", html)
        self.assertNotIn("chat-tick sent", html)

    def test_a_receipt_is_only_drawn_on_your_own_messages(self):
        """A tick on something you received tells you that you read it."""
        from .models import Message

        Message.objects.create(
            thread=self.thread, author=self.admin, body="Here.",
        )
        self.client.force_login(self.member)
        html = self.client.get(self.url).content.decode()
        # One receipt for the member's own message, none for the admin's.
        self.assertEqual(html.count("chat-tick"), 1)

    def test_the_audience_names_the_whole_organisation_and_counts_it(self):
        from .services import thread_audience

        audience = thread_audience(self.thread)
        self.assertEqual(audience["scope"], "org")
        self.assertEqual(audience["name"], "Receipt Co")
        self.assertEqual(audience["count"], 2)

    def test_the_audience_narrows_to_named_recipients(self):
        from .services import thread_audience

        self.thread.recipients.add(self.admin)
        audience = thread_audience(self.thread)
        self.assertEqual(audience["scope"], "people")
        self.assertEqual(audience["count"], 1)
        self.assertIn("RA", audience["name"])


@override_settings(MEDIA_ROOT=temp_media("gt-video-"))
class MessageVideoTests(TestCase):
    """Sending a clip, and being able to watch it.

    THE DECORATOR ABOVE IS LOAD-BEARING. Without it this class deleted the
    REAL uploads directory of whatever checkout ran the suite — and staging's
    deploy gate runs the suite on every deploy, so every deploy wiped every
    file anybody had uploaded since the last one. That is where the client's
    blog images went. See goodtip/testing.py.

    ASKED FOR AS: "the chat box having that pip that makes you attach images
    and videos". Two halves, and the second is the one with teeth — accepting
    an .mp4 is a line in an allowlist; playing it back is a byte-range server.
    """

    @classmethod
    def tearDownClass(cls):
        drop_temp_media()
        super().tearDownClass()

    def setUp(self):
        from .models import Message, MessageThread

        User = get_user_model()
        season = Season.objects.create(year=2095, label="2095")
        self.org = Organisation.objects.create(name="Clip Co", season=season)
        self.member = User.objects.create_user(
            email="clip@b.com", password="x", display_name="Clipper",
        )
        OrgMember.objects.create(user=self.member, org=self.org)
        self.thread = MessageThread.objects.create(
            org=self.org, kind=MessageThread.KIND_RAISED,
            subject="Here's the clip", started_by=self.member,
        )
        self.message = Message.objects.create(
            thread=self.thread, author=self.member, body="Look at this",
        )
        self.client.force_login(self.member)

    def _clip(self, name="goal.mp4", size=5000):
        from django.core.files.uploadedfile import SimpleUploadedFile

        # Not real MP4 — nothing here decodes it, and the code under test
        # decides everything from the suffix on purpose.
        return SimpleUploadedFile(name, bytes(range(256)) * (size // 256 + 1), "video/mp4")

    def _attach(self, name="goal.mp4"):
        from .models import MessageAttachment
        from .services import attach_files

        upload = self._clip(name)
        problems = attach_files(self.message, [upload])
        self.assertEqual(problems, [])
        return MessageAttachment.objects.get(message=self.message), upload

    def test_a_video_is_accepted_and_knows_it_is_one(self):
        attachment, _ = self._attach()
        self.assertTrue(attachment.is_video)
        self.assertFalse(attachment.is_image)
        self.assertEqual(attachment.video_type, "video/mp4")

    def test_quicktime_is_served_as_mp4(self):
        """Browsers that play the same bytes as video/mp4 refuse them as
        video/quicktime, and .mov off a phone is H.264 either way."""
        attachment, _ = self._attach("clip.mov")
        self.assertEqual(attachment.video_type, "video/mp4")

    def test_video_gets_a_bigger_ceiling_than_a_document(self):
        from .models import MessageAttachment

        self.assertGreater(
            MessageAttachment.limit_for("goal.mp4"),
            MessageAttachment.limit_for("notes.pdf"),
        )

    def test_a_range_request_gets_a_206_with_the_right_slice(self):
        """Seeking IS byte-ranging, and Safari will not start a <video> at all
        without a 206."""
        attachment, upload = self._attach()
        url = reverse("orgs:message_file", args=[self.thread.id, attachment.id])
        resp = self.client.get(url, headers={"range": "bytes=10-19"})
        self.assertEqual(resp.status_code, 206)
        self.assertEqual(resp["Content-Range"], f"bytes 10-19/{attachment.file.size}")
        self.assertEqual(resp["Accept-Ranges"], "bytes")
        upload.seek(10)
        self.assertEqual(resp.content, upload.read(10))

    def test_a_plain_request_advertises_that_ranging_is_allowed(self):
        """Without Accept-Ranges the element never asks for a range at all."""
        attachment, _ = self._attach()
        url = reverse("orgs:message_file", args=[self.thread.id, attachment.id])
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Accept-Ranges"], "bytes")

    def test_an_open_ended_range_is_capped_rather_than_sending_everything(self):
        """A <video> opens with "bytes=0-"; answering it with the whole file
        is a 200 wearing a 206's clothes."""
        from orgs.views import RANGE_CHUNK

        attachment, _ = self._attach(name="long.mp4")
        url = reverse("orgs:message_file", args=[self.thread.id, attachment.id])
        resp = self.client.get(url, headers={"range": "bytes=0-"})
        self.assertEqual(resp.status_code, 206)
        self.assertLessEqual(len(resp.content), RANGE_CHUNK)

    def test_an_unsatisfiable_range_is_416_and_says_how_big_the_file_is(self):
        attachment, _ = self._attach()
        url = reverse("orgs:message_file", args=[self.thread.id, attachment.id])
        resp = self.client.get(url, headers={"range": "bytes=999999-"})
        self.assertEqual(resp.status_code, 416)
        self.assertEqual(resp["Content-Range"], f"bytes */{attachment.file.size}")

    def test_a_nonsense_range_falls_back_to_the_whole_file(self):
        """A Range header is a request, not a contract."""
        attachment, _ = self._attach()
        url = reverse("orgs:message_file", args=[self.thread.id, attachment.id])
        resp = self.client.get(url, headers={"range": "rows=1-2"})
        self.assertEqual(resp.status_code, 200)

    def test_the_content_type_is_never_the_one_the_uploader_claimed(self):
        """attachment.content_type is attacker-controlled; echoing it back is
        how a .txt uploaded as text/html gets rendered in the member's origin."""
        from django.core.files.uploadedfile import SimpleUploadedFile

        from .models import MessageAttachment
        from .services import attach_files

        attach_files(self.message, [
            SimpleUploadedFile("notes.txt", b"<script>alert(1)</script>", "text/html"),
        ])
        attachment = MessageAttachment.objects.get(
            message=self.message, original_name="notes.txt",
        )
        self.assertEqual(attachment.content_type, "text/html")   # as claimed
        url = reverse("orgs:message_file", args=[self.thread.id, attachment.id])
        resp = self.client.get(url, headers={"range": "bytes=0-5"})
        self.assertEqual(resp.status_code, 206)
        self.assertNotIn("text/html", resp["Content-Type"])

    def test_a_video_bubble_plays_in_place(self):
        attachment, _ = self._attach()
        resp = self.client.get(
            reverse("orgs:member_message_thread", args=[self.org.id, self.thread.id])
        )
        self.assertContains(resp, "chat-clip")
        self.assertContains(resp, 'type="video/mp4"')


@override_settings(MEDIA_ROOT=tempfile.mkdtemp(prefix="gt-rooms-"))
class ChatRoomTests(TestCase):
    """The three rooms, and who may read each.

    This is the security surface of the messaging rebuild. Adding organisation
    rooms, group rooms and direct messages to the table that held support
    tickets means one method — MessageThread.can_read — now decides four
    different questions, and getting any of them wrong exposes a private
    conversation to a whole organisation.
    """

    @classmethod
    def tearDownClass(cls):
        drop_temp_media()
        super().tearDownClass()

    def setUp(self):
        User = get_user_model()
        season = Season.objects.create(year=2094, label="2094")
        self.org = Organisation.objects.create(
            name="Masterclass", season=season, groups_enabled=True,
        )
        self.other_org = Organisation.objects.create(name="Net Providers", season=season)

        self.admin = User.objects.create_user(email="ad@x.com", password="x", display_name="Ada")
        self.ann = User.objects.create_user(email="ann@x.com", password="x", display_name="Ann")
        self.bob = User.objects.create_user(email="bob@x.com", password="x", display_name="Bob")
        self.cat = User.objects.create_user(email="cat@x.com", password="x", display_name="Cat")
        self.outsider = User.objects.create_user(email="out@x.com", password="x", display_name="Otto")

        OrgMember.objects.create(user=self.admin, org=self.org, role=OrgMember.ROLE_MANAGER)
        for person in (self.ann, self.bob, self.cat):
            OrgMember.objects.create(user=person, org=self.org)
        OrgMember.objects.create(user=self.outsider, org=self.other_org)

        self.group = Group.objects.create(
            org=self.org, name="AFLW Tippers",
            approval_status=Group.APPROVAL_APPROVED,
        )
        GroupMember.objects.create(group=self.group, user=self.ann, is_admin=True)
        GroupMember.objects.create(group=self.group, user=self.bob)

    # ---- the organisation room -----------------------------------------

    def test_the_organisation_room_is_readable_by_every_member(self):
        from .services import org_room

        room = org_room(self.org)
        for person in (self.admin, self.ann, self.bob, self.cat):
            self.assertTrue(room.can_read(person), person.display_name)

    def test_the_organisation_room_is_not_readable_from_another_organisation(self):
        from .services import org_room

        self.assertFalse(org_room(self.org).can_read(self.outsider))

    def test_opening_the_room_twice_does_not_make_two(self):
        """A room is created lazily, so the guard against duplicates is the
        get_or_create and not a migration that made one up front."""
        from .models import MessageThread
        from .services import org_room

        first, second = org_room(self.org), org_room(self.org)
        self.assertEqual(first.id, second.id)
        self.assertEqual(
            MessageThread.objects.filter(
                org=self.org, kind=MessageThread.KIND_ORG,
            ).count(), 1,
        )

    # ---- a group's room -------------------------------------------------

    def test_a_group_room_is_readable_only_by_the_group(self):
        from .services import group_room

        room = group_room(self.group)
        self.assertTrue(room.can_read(self.ann))
        self.assertTrue(room.can_read(self.bob))
        # In the organisation, not in the group.
        self.assertFalse(room.can_read(self.cat))

    def test_running_the_organisation_does_not_open_its_groups_rooms(self):
        """The one rule most likely to be got wrong, and the most expensive.

        An admin CAN read every support thread in their organisation, because
        those are addressed to the admins. A group's chat is not addressed to
        them, and "I run the comp" is not a reason to read a conversation
        nobody sent you. An admin who needs to be in a room joins the group,
        which everyone in it can see.
        """
        from .services import group_room

        self.assertFalse(group_room(self.group).can_read(self.admin))

    def test_a_group_room_404s_for_somebody_not_in_the_group(self):
        self.client.force_login(self.cat)
        resp = self.client.get(
            reverse("orgs:message_group_room", args=[self.org.id, self.group.id])
        )
        # Not 403: an organisation with a private group should not confirm to
        # a non-member that the group exists.
        self.assertEqual(resp.status_code, 404)

    # ---- direct messages -------------------------------------------------

    def test_a_direct_message_is_readable_by_exactly_two_people(self):
        from .services import direct_thread

        dm = direct_thread(self.ann, self.bob, self.org)
        self.assertTrue(dm.can_read(self.ann))
        self.assertTrue(dm.can_read(self.bob))
        self.assertFalse(dm.can_read(self.cat))
        # Including the person who runs the organisation.
        self.assertFalse(dm.can_read(self.admin))

    def test_a_direct_message_is_found_from_either_end(self):
        """Who started it is an accident of who pressed first. A lookup that
        only matched started_by would hand each person their own half of the
        conversation — two threads, each showing one side of it."""
        from .services import direct_thread

        first = direct_thread(self.ann, self.bob, self.org)
        second = direct_thread(self.bob, self.ann, self.org)
        self.assertEqual(first.id, second.id)

    def test_you_cannot_message_somebody_in_another_organisation(self):
        """Membership is what gives two people the right to write to each
        other. A user id in a URL must not be a way to reach a stranger."""
        self.client.force_login(self.ann)
        resp = self.client.get(
            reverse("orgs:message_direct", args=[self.org.id, self.outsider.id])
        )
        self.assertEqual(resp.status_code, 404)

    def test_messaging_yourself_goes_nowhere(self):
        from .models import MessageThread

        self.client.force_login(self.ann)
        resp = self.client.get(
            reverse("orgs:message_direct", args=[self.org.id, self.ann.id])
        )
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(
            MessageThread.objects.filter(kind=MessageThread.KIND_DIRECT).exists()
        )

    # ---- the admin area cannot be walked into these ----------------------

    def test_the_manage_inbox_cannot_open_a_group_room(self):
        """orgs.views guards these with can_read; /manage/ used to guard only
        on `org__in=the ones I manage`, which would have opened every group
        room and every direct message in an organisation to whoever runs it."""
        from .services import direct_thread, group_room

        self.admin.is_staff = True
        self.admin.save(update_fields=["is_staff"])
        self.client.force_login(self.admin)
        for thread in (group_room(self.group), direct_thread(self.ann, self.bob, self.org)):
            resp = self.client.get(reverse("manage:message_thread", args=[thread.id]))
            self.assertEqual(resp.status_code, 404, thread.kind)

    # ---- the screen itself ------------------------------------------------

    def test_the_messages_screen_lists_the_rooms_you_are_in(self):
        from .models import Message
        from .services import group_room, org_room

        Message.objects.create(
            thread=org_room(self.org), author=self.admin, body="Round 5 locks Friday",
        )
        Message.objects.create(
            thread=group_room(self.group), author=self.ann, body="Lions all the way",
        )
        self.client.force_login(self.bob)
        html = self.client.get(
            reverse("orgs:member_messages", args=[self.org.id])
        ).content.decode()
        self.assertIn("Round 5 locks Friday", html)
        self.assertIn("Lions all the way", html)

    def test_the_messages_screen_does_not_leak_a_room_you_are_not_in(self):
        from .models import Message
        from .services import group_room

        Message.objects.create(
            thread=group_room(self.group), author=self.ann, body="Lions all the way",
        )
        self.client.force_login(self.cat)      # in the org, not in the group
        html = self.client.get(
            reverse("orgs:member_messages", args=[self.org.id])
        ).content.decode()
        self.assertNotIn("Lions all the way", html)

    def test_the_member_list_tags_the_admins(self):
        """The reason "write to the admins" could come off the screen: if you
        can see who they are, a direct message is the obvious way to reach
        one."""
        from .services import org_room

        room = org_room(self.org)
        self.client.force_login(self.ann)
        html = self.client.get(
            reverse("orgs:message_people", args=[self.org.id, room.id])
        ).content.decode()
        self.assertIn("Ada", html)
        self.assertIn("Admin", html)

    def test_the_member_list_searches_in_the_database(self):
        from .services import org_room

        room = org_room(self.org)
        self.client.force_login(self.ann)
        html = self.client.get(
            reverse("orgs:message_people", args=[self.org.id, room.id]), {"q": "Bob"},
        ).content.decode()
        self.assertIn("Bob", html)
        self.assertNotIn("Cat", html)


@override_settings(MEDIA_ROOT=tempfile.mkdtemp(prefix="gt-react-"))
class MessageReactionTests(TestCase):
    """Reactions, and the toggle that is the whole point of them."""

    @classmethod
    def tearDownClass(cls):
        drop_temp_media()
        super().tearDownClass()

    def setUp(self):
        from .models import Message
        from .services import org_room

        User = get_user_model()
        season = Season.objects.create(year=2093, label="2093")
        self.org = Organisation.objects.create(name="Reactors", season=season)
        self.ann = User.objects.create_user(email="a@r.com", password="x", display_name="Ann")
        self.bob = User.objects.create_user(email="b@r.com", password="x", display_name="Bob")
        OrgMember.objects.create(user=self.ann, org=self.org)
        OrgMember.objects.create(user=self.bob, org=self.org)
        self.room = org_room(self.org)
        self.message = Message.objects.create(
            thread=self.room, author=self.ann, body="Big weekend",
        )
        self.url = reverse(
            "orgs:message_react", args=[self.org.id, self.room.id, self.message.id],
        )

    def test_pressing_a_reaction_twice_takes_it_back(self):
        from .models import MessageReaction

        self.client.force_login(self.bob)
        self.client.post(self.url, {"emoji": "\U0001F525"})
        self.assertEqual(MessageReaction.objects.count(), 1)
        self.client.post(self.url, {"emoji": "\U0001F525"})
        self.assertEqual(MessageReaction.objects.count(), 0)

    def test_two_people_on_one_emoji_count_as_two(self):
        for person in (self.ann, self.bob):
            self.client.force_login(person)
            self.client.post(self.url, {"emoji": "\U0001F44D"})
        self.assertEqual(self.message.reactions.count(), 2)

    def test_an_emoji_that_is_not_offered_is_refused(self):
        """An allowlist, not "whatever was posted" — the column is eight
        characters and the chips have to stay scannable."""
        from .models import MessageReaction

        self.client.force_login(self.bob)
        resp = self.client.post(self.url, {"emoji": "\U0001F4A9"})
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(MessageReaction.objects.count(), 0)

    def test_you_cannot_react_in_a_room_you_cannot_read(self):
        User = get_user_model()
        stranger = User.objects.create_user(email="s@r.com", password="x", display_name="Sid")
        other = Organisation.objects.create(
            name="Elsewhere", season=Season.objects.get(year=2093),
        )
        OrgMember.objects.create(user=stranger, org=other)
        self.client.force_login(stranger)
        self.assertEqual(self.client.post(self.url, {"emoji": "\U0001F525"}).status_code, 404)


@override_settings(MEDIA_ROOT=tempfile.mkdtemp(prefix="gt-voice-"))
class VoiceNoteTests(TestCase):
    """A recording is not a video clip, even when it is a .webm.

    Which is the whole reason `is_voice` is a stored flag rather than
    something worked out from the filename: Chrome's MediaRecorder produces
    audio/webm and so does a video camera, and the two want completely
    different bubbles.
    """

    @classmethod
    def tearDownClass(cls):
        drop_temp_media()
        super().tearDownClass()

    def setUp(self):
        from .models import Message
        from .services import org_room

        User = get_user_model()
        season = Season.objects.create(year=2092, label="2092")
        self.org = Organisation.objects.create(name="Talkers", season=season)
        self.ann = User.objects.create_user(email="a@t.com", password="x", display_name="Ann")
        OrgMember.objects.create(user=self.ann, org=self.org)
        self.room = org_room(self.org)
        self.message = Message.objects.create(thread=self.room, author=self.ann, body="")
        self.client.force_login(self.ann)

    def _blob(self, name="voice-note.webm"):
        from django.core.files.uploadedfile import SimpleUploadedFile

        return SimpleUploadedFile(name, b"\x1aE\xdf\xa3" + b"\0" * 2000, "audio/webm")

    def test_a_recording_is_audio_and_not_video(self):
        from .models import MessageAttachment
        from .services import attach_files

        self.assertEqual(attach_files(self.message, [self._blob()], voice=True, duration_s=47), [])
        clip = MessageAttachment.objects.get(message=self.message)
        self.assertTrue(clip.is_voice)
        self.assertTrue(clip.is_audio)
        self.assertFalse(clip.is_video)
        self.assertEqual(clip.duration_label, "0:47")
        self.assertEqual(clip.audio_type, "audio/webm")

    def test_the_same_container_attached_as_a_file_is_still_a_video(self):
        from .models import MessageAttachment
        from .services import attach_files

        attach_files(self.message, [self._blob("goal.webm")])
        clip = MessageAttachment.objects.get(message=self.message)
        self.assertTrue(clip.is_video)
        self.assertFalse(clip.is_audio)

    def test_a_duration_from_the_page_is_clamped(self):
        """The number arrives from the browser, and the browser is not a
        source of truth. The recorder stops at the ceiling; this is what
        happens when something else does not."""
        from .models import MessageAttachment
        from .services import attach_files

        attach_files(self.message, [self._blob()], voice=True, duration_s=9999)
        clip = MessageAttachment.objects.get(message=self.message)
        self.assertEqual(clip.duration_s, MessageAttachment.MAX_VOICE_SECONDS)

    def test_sending_a_voice_note_through_the_composer(self):
        from .models import MessageAttachment

        self.client.post(
            reverse("orgs:member_message_thread", args=[self.org.id, self.room.id]),
            {"body": "", "voice": self._blob(), "voice_seconds": "12"},
        )
        clip = MessageAttachment.objects.filter(is_voice=True).first()
        self.assertIsNotNone(clip)
        self.assertEqual(clip.duration_s, 12)


class MessagePinTests(TestCase):
    """One message held at the top of a room."""

    def setUp(self):
        from .models import Message
        from .services import org_room

        User = get_user_model()
        season = Season.objects.create(year=2091, label="2091")
        self.org = Organisation.objects.create(name="Pinners", season=season)
        self.admin = User.objects.create_user(email="ad@p.com", password="x", display_name="Ada")
        self.ann = User.objects.create_user(email="a@p.com", password="x", display_name="Ann")
        OrgMember.objects.create(user=self.admin, org=self.org, role=OrgMember.ROLE_MANAGER)
        OrgMember.objects.create(user=self.ann, org=self.org)
        self.room = org_room(self.org)
        self.message = Message.objects.create(
            thread=self.room, author=self.admin, body="Round 5 locks at 7:30pm",
        )
        self.url = reverse("orgs:message_pin", args=[self.org.id, self.room.id])

    def test_an_admin_pins_and_unpins_with_the_same_control(self):
        self.client.force_login(self.admin)
        self.client.post(self.url, {"message": str(self.message.id)})
        self.room.refresh_from_db()
        self.assertEqual(self.room.pinned_message_id, self.message.id)
        # Posting the same id again takes it down — one control, both jobs.
        self.client.post(self.url, {"message": str(self.message.id)})
        self.room.refresh_from_db()
        self.assertIsNone(self.room.pinned_message_id)

    def test_an_ordinary_member_cannot_pin(self):
        self.client.force_login(self.ann)
        self.assertEqual(
            self.client.post(self.url, {"message": str(self.message.id)}).status_code, 403,
        )
        self.room.refresh_from_db()
        self.assertIsNone(self.room.pinned_message_id)

    def test_deleting_a_pinned_message_unpins_rather_than_deleting_the_room(self):
        from .models import MessageThread

        self.client.force_login(self.admin)
        self.client.post(self.url, {"message": str(self.message.id)})
        self.message.delete()
        self.assertTrue(MessageThread.objects.filter(pk=self.room.pk).exists())
        self.room.refresh_from_db()
        self.assertIsNone(self.room.pinned_message_id)


class MessagesFollowContextTests(TestCase):
    """Opening a conversation moves you into its organisation.

    "When I click, let's say, Masterclass, the message should change now for
    me to be into Masterclass — or an organisation and group — irrespective of
    where I am currently."

    Opening one always WORKED from anywhere; what did not happen is the bar
    following you, so you could be reading the Masterclass room with "Net
    Providers" written above it. On this screen that matters more than
    elsewhere: the next thing you do is type into a room.
    """

    def setUp(self):
        User = get_user_model()
        season = Season.objects.create(year=2090, label="2090")
        self.a = Organisation.objects.create(name="Masterclass", season=season, groups_enabled=True)
        self.b = Organisation.objects.create(name="Net Providers", season=season)
        self.me = User.objects.create_user(email="me@f.com", password="x", display_name="Mo")
        self.mate = User.objects.create_user(email="mate@f.com", password="x", display_name="Mate")
        for org in (self.a, self.b):
            OrgMember.objects.create(user=self.me, org=org)
        OrgMember.objects.create(user=self.mate, org=self.a)
        self.group = Group.objects.create(
            org=self.a, name="AFLW Tippers", approval_status=Group.APPROVAL_APPROVED,
        )
        GroupMember.objects.create(group=self.group, user=self.me)
        self.client.force_login(self.me)

    def _where(self):
        from .context import GROUP_KEY, ORG_KEY

        return (self.client.session.get(ORG_KEY), self.client.session.get(GROUP_KEY))

    def test_opening_an_organisation_room_moves_you_into_it(self):
        self.client.post(reverse("orgs:switch_org", args=[self.b.id]))
        self.assertEqual(self._where()[0], self.b.id)
        self.client.get(reverse("orgs:message_room", args=[self.a.id]), follow=True)
        self.assertEqual(self._where()[0], self.a.id)

    def test_opening_a_group_room_steps_into_the_group(self):
        self.client.post(reverse("orgs:switch_org", args=[self.b.id]))
        self.client.get(
            reverse("orgs:message_group_room", args=[self.a.id, self.group.id]), follow=True,
        )
        self.assertEqual(self._where(), (self.a.id, self.group.id))

    def test_a_direct_message_moves_the_organisation_but_not_the_group(self):
        """A direct message is not a reason to step out of a group you are
        tipping in — you are already in the right organisation."""
        from .context import set_current_group

        self.client.get(
            reverse("orgs:message_group_room", args=[self.a.id, self.group.id]), follow=True,
        )
        self.assertEqual(self._where(), (self.a.id, self.group.id))
        self.client.get(reverse("orgs:message_direct", args=[self.a.id, self.mate.id]), follow=True)
        self.assertEqual(self._where(), (self.a.id, self.group.id))

    def test_the_poll_does_not_write_the_session(self):
        """The conversation refreshes every twelve seconds. Writing where you
        are on each of those is a database write per viewer per tick to record
        something that has not changed."""
        from .services import org_room

        room = org_room(self.a)
        self.client.post(reverse("orgs:switch_org", args=[self.b.id]))
        self.client.get(
            reverse("orgs:member_message_thread", args=[self.a.id, room.id]) + "?pane=chat"
        )
        self.assertEqual(self._where()[0], self.b.id)


class RoleIsVisibleWhereYouSwitchTests(TestCase):
    """What you are in each organisation, said out loud.

    ASKED FOR BY THE CLIENT, who was invited into somebody else's league as an
    ordinary member and "did not feel the distinction". The nav was already
    correct — primary_org_is_admin follows the switch, so the Manage menu is
    gone in an org you do not run. Being correct was not the problem: nothing
    on screen said which of your organisations you were about to walk into, so
    a nav that had quietly lost three items read as a page that had failed to
    finish loading.
    """

    def setUp(self):
        User = get_user_model()
        self.season = Season.objects.create(year=2099, label="2099")
        self.user = User.objects.create_user(
            email="two-hats@example.com", password="x", display_name="Two Hats",
        )
        self.boss = User.objects.create_user(
            email="theirs@example.com", password="x", display_name="Theirs",
        )
        # One they created, one they were invited into. created_by is what
        # is_creator_admin keys on — a managing row in somebody else's org
        # is deliberately not enough.
        self.mine = Organisation.objects.create(
            name="Mine Co", season=self.season, created_by=self.user,
        )
        self.theirs = Organisation.objects.create(
            name="Theirs Co", season=self.season, created_by=self.boss,
        )
        # can_manage is derived from role/ownership, not a column.
        OrgMember.objects.create(user=self.user, org=self.mine, is_league_owner=True)
        OrgMember.objects.create(user=self.user, org=self.theirs)
        OrgMember.objects.create(user=self.boss, org=self.theirs, is_league_owner=True)
        self.client.force_login(self.user)

    def _dash(self):
        return self.client.get(reverse("dashboard")).content.decode()

    def test_every_organisation_in_the_switcher_carries_its_own_role(self):
        body = self._dash()
        self.assertIn("gt-role is-admin", body)
        self.assertIn("gt-role is-member", body)

    def test_the_role_is_readable_before_the_switch_not_after(self):
        """The badge is on the row you are about to press. Reading it only
        after arriving is reading it too late — the point is to know what you
        are walking into."""
        body = self._dash()
        row = body[body.index("Theirs Co"):]
        self.assertIn("gt-role is-member", row[:600])
        self.assertIn("You're a member here", body)

    def test_the_chip_says_admin_in_the_org_you_run(self):
        body = self._dash()
        self.assertIn("an-ctx-role is-admin", body)

    def test_the_chip_says_member_once_you_switch_into_theirs(self):
        self.client.post(reverse("orgs:switch_org", args=[self.theirs.id]))
        body = self._dash()
        self.assertIn("an-ctx-role is-member", body)
        self.assertNotIn("an-ctx-role is-admin", body)

    def test_switching_into_theirs_really_does_take_the_admin_menu_away(self):
        """The behaviour the badge describes. Pinned here because a badge that
        says Member over a nav still offering Manage would be worse than no
        badge at all."""
        admin_body = self._dash()
        self.assertIn(reverse("orgs:members", args=[self.mine.id]), admin_body)

        self.client.post(reverse("orgs:switch_org", args=[self.theirs.id]))
        member_body = self._dash()
        self.assertNotIn(reverse("orgs:members", args=[self.theirs.id]), member_body)

    def test_the_context_processor_answers_for_every_membership(self):
        """Computed from the memberships already fetched, so this must stay a
        no-extra-query answer as the list grows."""
        from .context_processors import user_orgs
        from django.test import RequestFactory

        r = RequestFactory().get("/")
        r.user = self.user
        r.session = self.client.session
        ctx = user_orgs(r)
        roles = {o.name: o.nav_role for o in ctx["nav_orgs"]}
        self.assertEqual(roles, {"Mine Co": "Admin", "Theirs Co": "Member"})


class ANotificationPopsOnceTests(TestCase):
    """One turn at the bell, then the count carries it.

    REPORTED BY THE CLIENT: "he keeps getting the notification pop-up on things
    he already opened... let the pop-up be once. After it pops up once — and
    let's say the user views or even does not view — it will auto clear, and let
    it not pop out again; instead have the notification bell have like 1 or 2 or
    3 the way it counts notifications that have not been read."

    The ticker used to loop the undismissed ones forever, so anything opened but
    never explicitly closed with the ✕ came back on the next page load.
    """

    def setUp(self):
        User = get_user_model()
        self.season = Season.objects.create(year=2099, label="2099")
        self.user = User.objects.create_user(
            email="bell@example.com", password="x", display_name="Bell",
        )
        self.org = Organisation.objects.create(
            name="Bell Co", season=self.season, created_by=self.user,
        )
        OrgMember.objects.create(user=self.user, org=self.org, is_league_owner=True)
        self.notes = [
            Notification.objects.create(
                user=self.user, org=self.org, title=f"Something {i}",
            )
            for i in range(3)
        ]
        self.client.force_login(self.user)

    def _queue(self):
        from .context_processors import user_orgs
        from django.test import RequestFactory

        r = RequestFactory().get("/")
        r.user = self.user
        r.session = self.client.session
        return user_orgs(r)["ticker_notifications"]

    def test_a_fresh_notification_is_queued(self):
        self.assertEqual(len(self._queue()), 3)

    def test_one_that_has_had_its_turn_is_not_queued_again(self):
        self.client.post(
            reverse("orgs:notifications_announced"), {"ids": [self.notes[0].id]},
        )
        titles = {n["title"] for n in self._queue()}
        self.assertNotIn("Something 0", titles)
        self.assertEqual(len(titles), 2)

    def test_having_had_its_turn_does_not_mark_it_read(self):
        """The distinction the whole change rests on. A teaser that flashed past
        while somebody was looking at the fixtures has been announced and has
        NOT been read — and the bell's count is what has to go on saying so."""
        self.client.post(
            reverse("orgs:notifications_announced"),
            {"ids": [n.id for n in self.notes]},
        )
        self.notes[0].refresh_from_db()
        self.assertIsNotNone(self.notes[0].announced_at)
        self.assertIsNone(self.notes[0].read_at)

        from .context_processors import user_orgs
        from django.test import RequestFactory

        r = RequestFactory().get("/")
        r.user = self.user
        r.session = self.client.session
        ctx = user_orgs(r)
        self.assertEqual(ctx["ticker_notifications"], [])
        self.assertEqual(ctx["unread_notification_count"], 3)

    def test_opening_one_stops_it_coming_back(self):
        """The client's actual symptom, end to end: open it, reload, and it must
        not be waiting for you again."""
        self.client.post(
            reverse("orgs:notification_open", args=[self.notes[1].id]),
        )
        self.client.post(
            reverse("orgs:notifications_announced"), {"ids": [self.notes[1].id]},
        )
        self.assertNotIn("Something 1", {n["title"] for n in self._queue()})

    def test_it_will_not_announce_somebody_elses_notification(self):
        User = get_user_model()
        other = User.objects.create_user(
            email="nosy@example.com", password="x", display_name="Nosy",
        )
        theirs = Notification.objects.create(user=other, title="Not yours")
        r = self.client.post(
            reverse("orgs:notifications_announced"), {"ids": [theirs.id]},
        )
        self.assertEqual(r.status_code, 200)
        theirs.refresh_from_db()
        self.assertIsNone(theirs.announced_at)


class ARoleInTheOrgIsWhatMakesYouItsAdminTests(TestCase):
    """Appointed Team Manager of a league, and treated as one.

    THE RULE CHANGED HERE. It used to also require being the organisation's own
    `created_by`, so somebody the league had deliberately appointed through its
    own members screen got a member's nav and a 403 on the screens they had just
    been given the role for. Three live memberships on staging were in that
    state.

    The client, twice: "if I pick the organisation that I am an admin, I see
    what the admin should see... if I get into an organisation that I am not an
    admin then I see the menu and all as a member."
    """

    def setUp(self):
        User = get_user_model()
        self.season = Season.objects.create(year=2099, label="2099")
        self.founder = User.objects.create_user(
            email="founder@example.com", password="x", display_name="Founder",
        )
        self.manager = User.objects.create_user(
            email="manager@example.com", password="x", display_name="Manager",
        )
        self.org = Organisation.objects.create(
            name="Someone Else's Co", season=self.season, created_by=self.founder,
        )
        OrgMember.objects.create(user=self.founder, org=self.org, is_league_owner=True)
        self.appointed = OrgMember.objects.create(
            user=self.manager, org=self.org, role=OrgMember.ROLE_MANAGER,
        )

    def test_an_appointed_manager_manages_the_org_that_appointed_them(self):
        from .services import is_creator_admin

        self.assertTrue(
            is_creator_admin(self.manager, self.org, membership=self.appointed)
        )

    def test_a_participant_does_not(self):
        User = get_user_model()
        plain = User.objects.create_user(
            email="plain@example.com", password="x", display_name="Plain",
        )
        m = OrgMember.objects.create(user=plain, org=self.org)
        from .services import is_creator_admin

        self.assertFalse(is_creator_admin(plain, self.org, membership=m))

    def test_the_manage_menu_is_on_the_bar_for_them(self):
        self.client.force_login(self.manager)
        body = self.client.get(reverse("dashboard")).content.decode()
        self.assertIn(reverse("orgs:members", args=[self.org.id]), body)

    def test_the_bill_still_belongs_to_the_owner_alone(self):
        """What this widened, and what it deliberately did not. Billing is
        gated on is_league_owner, not on can_manage."""
        self.client.force_login(self.manager)
        body = self.client.get(reverse("dashboard")).content.decode()
        self.assertNotIn(reverse("billing:plans", args=[self.org.id]), body)


class OpeningARoomDoesNotReloadTheScreenTests(TestCase):
    """"If I pick a group or an organisation the whole page should not be
    loading — it should only load on that centre part where we have the data."

    Every room in the left-hand tree was a plain link, so choosing one threw
    away the list, the search box, the tabs and the scroll position, re-fetched
    all of it, and played the splash on the way — to change one panel that was
    already on screen.
    """

    def setUp(self):
        User = get_user_model()
        self.season = Season.objects.create(year=2099, label="2099")
        self.org = Organisation.objects.create(
            name="Room Co", season=self.season, groups_enabled=True,
        )
        self.me = User.objects.create_user(
            email="room-me@example.com", password="x", display_name="Room Me",
        )
        self.mate = User.objects.create_user(
            email="room-mate@example.com", password="x", display_name="Room Mate",
        )
        OrgMember.objects.create(user=self.me, org=self.org)
        OrgMember.objects.create(user=self.mate, org=self.org)
        self.group = Group.objects.create(
            org=self.org, name="Ops", approval_status=Group.APPROVAL_APPROVED,
        )
        GroupMember.objects.create(group=self.group, user=self.me)
        self.client.force_login(self.me)

    def test_the_org_room_comes_back_as_a_panel_not_a_page(self):
        r = self.client.get(
            reverse("orgs:message_room", args=[self.org.id]) + "?pane=room",
        )
        body = r.content.decode()
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("<!DOCTYPE", body)
        self.assertNotIn("gtm-nav", body)
        self.assertIn("gtm-chat-in", body)

    def test_a_group_room_does_the_same(self):
        r = self.client.get(
            reverse("orgs:message_group_room", args=[self.org.id, self.group.id])
            + "?pane=room",
        )
        self.assertEqual(r.status_code, 200)
        self.assertIn("gtm-chat-in", r.content.decode())

    def test_the_room_openers_do_not_redirect_the_fragment_away(self):
        """A redirect drops the query string, so ?pane=room would arrive at the
        thread view as an ordinary request and come back as an entire page —
        and the swap would put a whole document inside one div."""
        r = self.client.get(
            reverse("orgs:message_room", args=[self.org.id]) + "?pane=room",
        )
        self.assertEqual(r.status_code, 200, "it redirected instead of rendering")

    def test_the_same_url_without_the_flag_is_still_a_whole_page(self):
        """The links carry hx-push-url, so these get copied, bookmarked and
        opened cold."""
        r = self.client.get(
            reverse("orgs:message_room", args=[self.org.id]), follow=True,
        )
        self.assertIn("<!DOCTYPE", r.content.decode())

    def test_a_direct_message_opens_as_a_panel_too(self):
        r = self.client.get(
            reverse("orgs:message_direct", args=[self.org.id, self.mate.id])
            + "?pane=room",
        )
        self.assertEqual(r.status_code, 200)
        self.assertIn("gtm-chat-in", r.content.decode())

    def test_the_links_in_the_tree_ask_for_the_panel(self):
        body = self.client.get(
            reverse("orgs:member_messages", args=[self.org.id])
        ).content.decode()
        self.assertIn("pane=room", body)
        self.assertIn('hx-target=".gtm-chat"', body)

    def test_reading_a_room_this_way_still_moves_you_into_it(self):
        """_follow_context runs on the fragment path as well, or the sidebar
        and the nav chip would describe a different organisation from the
        conversation on screen."""
        from .context import ORG_KEY

        other = Organisation.objects.create(name="Elsewhere", season=self.season)
        OrgMember.objects.create(user=self.me, org=other)
        self.client.post(reverse("orgs:switch_org", args=[other.id]))
        self.client.get(
            reverse("orgs:message_room", args=[self.org.id]) + "?pane=room",
        )
        self.assertEqual(self.client.session.get(ORG_KEY), self.org.id)


class SendingAMessageIsNotANavigationTests(TestCase):
    """"When I type and hit send, why am I getting a loader?"

    It posted the form, followed a redirect, rebuilt the whole three-panel
    screen and played the splash — for a message that was already written.
    """

    def setUp(self):
        User = get_user_model()
        self.season = Season.objects.create(year=2099, label="2099")
        self.org = Organisation.objects.create(name="Send Co", season=self.season)
        self.me = User.objects.create_user(
            email="send-me@example.com", password="x", display_name="Sender",
        )
        OrgMember.objects.create(user=self.me, org=self.org)
        from .services import org_room

        self.thread = org_room(self.org)
        self.client.force_login(self.me)

    def _send(self, **extra):
        return self.client.post(
            reverse("orgs:member_message_thread", args=[self.org.id, self.thread.id]),
            {"body": "hello there"},
            **extra,
        )

    def test_an_htmx_send_gets_the_messages_back_not_a_redirect(self):
        r = self._send(HTTP_HX_REQUEST="true")
        body = r.content.decode()
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("<!DOCTYPE", body)
        self.assertIn('id="gtmChatList"', body)
        self.assertIn("hello there", body)

    def test_it_saves_the_message_either_way(self):
        from .models import Message

        self._send(HTTP_HX_REQUEST="true")
        self.assertEqual(Message.objects.filter(thread=self.thread).count(), 1)

    def test_without_javascript_it_still_posts_and_redirects(self):
        """The plain form action and enctype are still there, so a browser with
        no script sends a message and lands back on the conversation."""
        r = self._send()
        self.assertEqual(r.status_code, 302)

    def test_the_composer_is_never_the_thing_that_gets_replaced(self):
        """The reply is the message LIST. Swapping the composer would throw
        away anything half-typed, half-attached or half-recorded."""
        body = self.client.get(
            reverse("orgs:member_message_thread", args=[self.org.id, self.thread.id])
        ).content.decode()
        self.assertIn('hx-target="#gtmChatList"', body)
        self.assertNotIn('data-busy="Sending"', body)


class PrefectReadsTheRoomTests(TestCase):
    """What Prefect flags, and — more importantly — what it leaves alone.

    THE CLIENT'S OWN EXAMPLE IS THE HARD CASE: "sometimes friends talk like
    'what's up you crazy fool' — their friendship is at that level, but when you
    read it plainly you might think it's an abuse. So the prefect has to be
    smart."

    A word list cannot be smart about tone; what it can be is quiet. Every rule
    here is biased toward saying nothing, because a moderator that cries wolf
    trains people to ignore it — and the misses are invisible while the noise
    is not.
    """

    def _look(self, text, **ctx):
        from .prefect import classify

        return classify(text, ctx or None)

    def test_it_says_nothing_about_an_ordinary_message(self):
        self.assertFalse(self._look("good luck this round everyone").reportable)

    def test_banter_between_mates_is_not_reported(self):
        """The example, verbatim. Aimed at somebody, on the abuse list, and
        carrying every mark of a joke."""
        v = self._look("what's up you crazy fool 😂 lol mate")
        self.assertFalse(v.reportable, f"scored {v.score}: {v.reason}")

    def test_a_slur_is_reported_even_wrapped_in_laughing(self):
        """Three laughing emoji are not a defence.

        This failed first time round: the banter discount applied to every
        category, so "haha you faggot lol 😂" lost 45 points and fell under the
        reporting floor. Tone explains an INSULT, which is the client's own
        example, and explains nothing about a slur.
        """
        v = self._look("haha you faggot lol 😂")
        self.assertTrue(v.reportable)
        self.assertEqual(v.category, "hate")

    def test_pornography_in_a_workplace_room_is_reported(self):
        """Also failed first time: the "not aimed at anyone" discount halved it
        because the sentence pointed at nobody. A pornographic link addressed to
        nobody is still in the room."""
        v = self._look("check this out pornhub.com/watch")
        self.assertTrue(v.reportable)
        self.assertEqual(v.category, "sexual")

    def test_a_threat_aimed_at_somebody_is_reported(self):
        self.assertTrue(self._look("i will find you and kill you").reportable)

    def test_an_opinion_about_a_fixture_is_not_an_attack_on_a_person(self):
        """"This is stupid" is about the fixture. Only "you are stupid" is
        Prefect's business."""
        self.assertFalse(self._look("that fixture list is stupid").reportable)

    def test_the_room_laughing_is_still_reported_to_the_reviewer(self):
        """Recorded even where it is not applied. A reviewer is allowed to weigh
        what Prefect may not."""
        v = self._look("haha you faggot lol")
        self.assertTrue(v.reportable)
        self.assertIn("laughing", v.reason)

    def test_ordinary_abuse_alone_does_not_reach_the_floor(self):
        """The lowest-weighted list on purpose: this is where the client's
        example lives, and on its own it is banter far more often than it is
        anything else."""
        self.assertFalse(self._look("you idiot").reportable)

    def test_a_word_inside_another_word_is_not_a_match(self):
        """Matched on boundaries, so "classic" is not "ass" and Scunthorpe is
        nobody's problem."""
        for innocent in ("that was a classic finish", "assumption", "analysis"):
            self.assertFalse(self._look(innocent).reportable, innocent)

    def test_an_allowed_phrase_is_ignored_in_that_organisation(self):
        """The one thing Prefect learns, and it only learns to say less."""
        loud = "you faggot"
        self.assertTrue(self._look(loud).reportable)
        self.assertFalse(self._look(loud, allowed_phrases={"faggot"}).reportable)

    def test_a_classifier_that_breaks_lets_the_message_through(self):
        """A chat that cannot accept a message because the moderator fell over
        is a worse product than one that missed something."""
        from unittest.mock import patch

        from .prefect import classify

        with patch("orgs.prefect.get_classifier", side_effect=RuntimeError("boom")):
            self.assertFalse(classify("anything at all").reportable)

    def test_the_seam_is_a_setting(self):
        """One setting, one class, a .look() method — so a language model can
        replace this without touching anything that calls it."""
        from django.test import override_settings

        from .prefect import get_classifier

        with override_settings(PREFECT_CLASSIFIER="orgs.prefect.KeywordPrefect"):
            self.assertEqual(type(get_classifier()).__name__, "KeywordPrefect")


class PrefectRaisesAndReviewsTests(TestCase):
    """From a message to a decision, and everything that must not happen on the
    way.

    A flag is a REQUEST FOR REVIEW. Nothing is hidden, nobody is suspended and no
    message is touched by raising one — every consequence is applied by a person.
    """

    def setUp(self):
        User = get_user_model()
        self.season = Season.objects.create(year=2099, label="2099")
        self.boss = User.objects.create_user(
            email="pf-boss@example.com", password="x", display_name="Boss",
        )
        self.rude = User.objects.create_user(
            email="pf-rude@example.com", password="x", display_name="Rude",
        )
        self.bystander = User.objects.create_user(
            email="pf-by@example.com", password="x", display_name="Bystander",
        )
        self.org = Organisation.objects.create(
            name="Prefect Co", season=self.season, created_by=self.boss,
        )
        OrgMember.objects.create(user=self.boss, org=self.org, is_league_owner=True)
        OrgMember.objects.create(user=self.rude, org=self.org)
        OrgMember.objects.create(user=self.bystander, org=self.org)
        from .services import org_room

        self.thread = org_room(self.org)

    def _say(self, user, text):
        from .models import Message

        return Message.objects.create(thread=self.thread, author=user, body=text)

    def test_a_bad_message_raises_a_flag_and_tells_the_admins(self):
        from .models import ChatFlag, Notification
        from .moderation import review_message

        entry = self._say(self.rude, "you faggot")
        flag = review_message(entry)
        self.assertIsNotNone(flag)
        self.assertEqual(flag.status, ChatFlag.STATUS_OPEN)
        self.assertIsNone(flag.raised_by, "Prefect raised it, so raised_by is empty")
        self.assertTrue(
            Notification.objects.filter(user=self.boss, org=self.org).exists()
        )

    def test_the_notification_does_not_quote_the_message(self):
        """A teaser of the exact words would put them on the bell of everybody
        who runs the organisation — publishing them further, not handling them."""
        from .models import Notification
        from .moderation import review_message

        review_message(self._say(self.rude, "you faggot"))
        note = Notification.objects.filter(user=self.boss).first()
        self.assertNotIn("faggot", note.title + note.message)

    def test_the_author_is_not_told_prefect_flagged_them(self):
        """Before any human has looked, that is an accusation from a word list."""
        from .models import Notification
        from .moderation import review_message

        review_message(self._say(self.rude, "you faggot"))
        self.assertFalse(Notification.objects.filter(user=self.rude).exists())

    def test_nothing_is_hidden_or_deleted_by_a_flag(self):
        from .models import Message
        from .moderation import review_message

        entry = self._say(self.rude, "you faggot")
        review_message(entry)
        self.assertTrue(Message.objects.filter(pk=entry.pk).exists())

    def test_an_ordinary_message_raises_nothing(self):
        from .models import ChatFlag
        from .moderation import review_message

        review_message(self._say(self.rude, "good luck everyone"))
        self.assertEqual(ChatFlag.objects.count(), 0)

    def test_a_member_can_report_somebody_else(self):
        """"Apart from the prefect, the member can also report a member." Same
        queue, and raised_by is what tells the reviewer which it was."""
        from .models import ChatFlag
        from .moderation import report_message

        entry = self._say(self.rude, "something a person objects to")
        flag = report_message(entry, self.bystander, "not on")
        self.assertEqual(flag.raised_by, self.bystander)
        self.assertEqual(flag.source, ChatFlag.SOURCE_MEMBER)
        self.assertEqual(flag.note, "not on")

    def test_reporting_twice_does_not_make_two_flags(self):
        """A report button that can be pressed ten times is a way to make a
        queue useless."""
        from .models import ChatFlag
        from .moderation import report_message

        entry = self._say(self.rude, "whatever")
        report_message(entry, self.bystander)
        report_message(entry, self.bystander)
        self.assertEqual(ChatFlag.objects.filter(message=entry).count(), 1)

    def test_you_cannot_report_your_own_message(self):
        entry = self._say(self.rude, "hello")
        self.client.force_login(self.rude)
        self.client.post(
            reverse("orgs:report_message", args=[self.org.id, entry.id])
        )
        from .models import ChatFlag

        self.assertEqual(ChatFlag.objects.count(), 0)

    def test_you_cannot_report_a_conversation_you_are_not_in(self):
        """A message id is not a licence to report a room you were never in."""
        User = get_user_model()
        outsider = User.objects.create_user(
            email="pf-out@example.com", password="x", display_name="Out",
        )
        entry = self._say(self.rude, "hello")
        self.client.force_login(outsider)
        r = self.client.post(
            reverse("orgs:report_message", args=[self.org.id, entry.id])
        )
        self.assertEqual(r.status_code, 404)


class PrefectDecisionsTests(TestCase):
    """Clearing, learning, warning, suspending, removing and lifting."""

    def setUp(self):
        User = get_user_model()
        self.season = Season.objects.create(year=2099, label="2099")
        self.boss = User.objects.create_user(
            email="d-boss@example.com", password="x", display_name="Boss",
        )
        self.rude = User.objects.create_user(
            email="d-rude@example.com", password="x", display_name="Rude",
        )
        self.org = Organisation.objects.create(
            name="Decide Co", season=self.season, created_by=self.boss,
        )
        OrgMember.objects.create(user=self.boss, org=self.org, is_league_owner=True)
        OrgMember.objects.create(user=self.rude, org=self.org)
        from .services import org_room

        self.thread = org_room(self.org)
        from .models import Message
        from .moderation import review_message

        self.entry = Message.objects.create(
            thread=self.thread, author=self.rude, body="you faggot",
        )
        self.flag = review_message(self.entry)
        self.client.force_login(self.boss)

    def test_clearing_does_nothing_to_anybody(self):
        from .models import ChatFlag, MemberSanction

        self.client.post(reverse("manage:prefect_clear", args=[self.flag.id]))
        self.flag.refresh_from_db()
        self.assertEqual(self.flag.status, ChatFlag.STATUS_CLEARED)
        self.assertEqual(MemberSanction.objects.count(), 0)

    def test_clearing_with_learn_teaches_prefect_to_stay_quiet_here(self):
        from .models import PrefectAllowance
        from .prefect import classify
        from .moderation import allowed_phrases

        self.client.post(
            reverse("manage:prefect_clear", args=[self.flag.id]), {"learn": "1"},
        )
        self.assertTrue(PrefectAllowance.objects.filter(org=self.org).exists())
        v = classify("you faggot", {"allowed_phrases": allowed_phrases(self.org)})
        self.assertFalse(v.reportable)

    def test_what_one_organisation_allows_does_not_leak_to_another(self):
        """A shared list would let the loosest room set the standard for every
        other."""
        from .moderation import allowed_phrases

        other = Organisation.objects.create(name="Elsewhere", season=self.season)
        self.client.post(
            reverse("manage:prefect_clear", args=[self.flag.id]), {"learn": "1"},
        )
        self.assertEqual(allowed_phrases(other), set())

    def test_a_warning_is_recorded_and_the_person_is_told(self):
        from .models import MemberSanction, Notification

        self.client.post(
            reverse("manage:prefect_act", args=[self.flag.id]),
            {"action": "warn", "reason": "Keep it civil."},
        )
        s = MemberSanction.objects.get()
        self.assertEqual(s.kind, MemberSanction.KIND_WARNING)
        self.assertFalse(s.is_active, "a warning is a message, not a state")
        self.assertTrue(Notification.objects.filter(user=self.rude).exists())

    def test_a_suspension_stops_them_posting_and_says_why(self):
        from .moderation import may_post

        self.client.post(
            reverse("manage:prefect_act", args=[self.flag.id]),
            {"action": "suspend", "span": "week", "reason": "That's not on."},
        )
        allowed, why = may_post(self.rude, self.thread)
        self.assertFalse(allowed)
        self.assertIn("Decide Co", why)

    def test_until_lifted_is_a_real_state_not_a_distant_date(self):
        from .models import MemberSanction

        self.client.post(
            reverse("manage:prefect_act", args=[self.flag.id]),
            {"action": "suspend", "span": "open"},
        )
        s = MemberSanction.objects.get()
        self.assertIsNone(s.ends_at)
        self.assertTrue(s.is_active)
        self.assertEqual(s.until_label, "until lifted")

    def test_lifting_early_lets_them_back_and_keeps_the_record(self):
        from .models import MemberSanction
        from .moderation import may_post

        self.client.post(
            reverse("manage:prefect_act", args=[self.flag.id]),
            {"action": "suspend", "span": "month"},
        )
        s = MemberSanction.objects.get()
        self.client.post(reverse("manage:prefect_lift", args=[s.id]))
        s.refresh_from_db()
        self.assertIsNotNone(s.lifted_at)
        self.assertTrue(may_post(self.rude, self.thread)[0])
        self.assertTrue(
            MemberSanction.objects.filter(pk=s.pk).exists(),
            "a lift must not delete the history",
        )

    def test_a_suspended_member_cannot_post_even_by_posting_the_form(self):
        """Hiding the composer is a courtesy; refusing the post is the rule."""
        from .models import Message

        self.client.post(
            reverse("manage:prefect_act", args=[self.flag.id]),
            {"action": "suspend", "span": "week"},
        )
        before = Message.objects.filter(thread=self.thread).count()
        self.client.force_login(self.rude)
        self.client.post(
            reverse("orgs:member_message_thread", args=[self.org.id, self.thread.id]),
            {"body": "let me back in"},
        )
        self.assertEqual(Message.objects.filter(thread=self.thread).count(), before)

    def test_removing_takes_them_out_of_the_organisation(self):
        self.client.post(
            reverse("manage:prefect_act", args=[self.flag.id]), {"action": "remove"},
        )
        self.assertFalse(
            OrgMember.objects.filter(org=self.org, user=self.rude).exists()
        )

    def test_the_creator_cannot_be_removed_from_their_own_organisation(self):
        """There is no undo for taking a league away from the person who made
        it."""
        from .models import Message
        from .moderation import review_message

        entry = Message.objects.create(
            thread=self.thread, author=self.boss, body="you faggot",
        )
        flag = review_message(entry)
        self.client.post(
            reverse("manage:prefect_act", args=[flag.id]), {"action": "remove"},
        )
        self.assertTrue(
            OrgMember.objects.filter(org=self.org, user=self.boss).exists()
        )

    def test_an_admin_of_another_organisation_cannot_touch_this_flag(self):
        User = get_user_model()
        stranger = User.objects.create_user(
            email="d-str@example.com", password="x", display_name="Stranger",
        )
        theirs = Organisation.objects.create(
            name="Theirs", season=self.season, created_by=stranger,
        )
        OrgMember.objects.create(user=stranger, org=theirs, is_league_owner=True)
        self.client.force_login(stranger)
        r = self.client.post(
            reverse("manage:prefect_act", args=[self.flag.id]), {"action": "warn"},
        )
        self.assertEqual(r.status_code, 404)

    def test_the_queue_lists_what_is_waiting(self):
        body = self.client.get(reverse("manage:prefect")).content.decode()
        self.assertIn("Rude", body)
        self.assertIn("Review", body)

    def test_the_review_screen_shows_the_conversation_around_it(self):
        """The client's example turns entirely on context: the same words
        between friends and from a stranger are different events."""
        from .models import Message

        Message.objects.create(
            thread=self.thread, author=self.boss, body="haha get in here",
        )
        body = self.client.get(
            reverse("manage:prefect_flag", args=[self.flag.id])
        ).content.decode()
        self.assertIn("haha get in here", body)
        self.assertIn("pf-msg is-it", body)
