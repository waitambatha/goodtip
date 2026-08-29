"""The organisation admin is walled off from every other organisation.

/manage/ was superuser-only until the split — nothing but create_superuser sets
is_staff — so every query behind it was written assuming "you can see
everything". Opening it to org creators is the change most likely to leak, and
these tests are the guard: each one asserts on the RESPONSE, not on which links
a template happened to draw.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase

from catalog.models import Season
from orgs.models import Message, MessageThread, OrgMember, Organisation

from .perms import managed_orgs

User = get_user_model()


class Fixture(TestCase):
    def setUp(self):
        self.season, _ = Season.objects.get_or_create(year=2094, defaults={"label": "2094"})
        self.mine = Organisation.objects.create(name="Mine", season=self.season)
        self.theirs = Organisation.objects.create(name="Theirs", season=self.season)

        self.owner = User.objects.create_user(email="o@w.com", password="x", display_name="Ollie")
        OrgMember.objects.create(
            user=self.owner, org=self.mine, role=OrgMember.ROLE_BOTH, is_league_owner=True,
        )
        self.member = User.objects.create_user(email="m@w.com", password="x", display_name="Mo")
        OrgMember.objects.create(user=self.member, org=self.mine, role=OrgMember.ROLE_PARTICIPANT)

        self.rival = User.objects.create_user(email="r@w.com", password="x", display_name="Rae")
        OrgMember.objects.create(
            user=self.rival, org=self.theirs, role=OrgMember.ROLE_BOTH, is_league_owner=True,
        )


class ManagedOrgsTests(Fixture):
    def test_an_owner_gets_their_own_organisation(self):
        self.assertEqual(list(managed_orgs(self.owner)), [self.mine])

    def test_a_participant_manages_nothing(self):
        self.assertEqual(list(managed_orgs(self.member)), [])

    def test_a_participant_does_not_inherit_somebody_elses_ownership(self):
        """The bug a chained .filter() would have shipped.

        Written as .filter(members__user=u).filter(members__is_league_owner=True)
        the two conditions can be satisfied by DIFFERENT OrgMember rows, so any
        participant in an organisation that has an owner at all would match.
        """
        self.assertNotIn(self.mine, managed_orgs(self.member))

    def test_a_superuser_who_runs_nothing_still_manages_nothing(self):
        boss = User.objects.create_superuser(
            email="b@w.com", password="x", display_name="Boss",
        )
        self.assertEqual(list(managed_orgs(boss)), [])


class IsolationTests(Fixture):
    def test_the_org_list_shows_only_your_own(self):
        self.client.force_login(self.owner)
        body = self.client.get("/manage/orgs/").content
        self.assertIn(b"Mine", body)
        self.assertNotIn(b"Theirs", body)

    def test_somebody_elses_members_page_is_a_404(self):
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(f"/manage/org/{self.theirs.id}/members/").status_code, 404)

    def test_somebody_elses_rounds_page_is_a_404(self):
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(f"/manage/org/{self.theirs.id}/rounds/").status_code, 404)

    def test_your_own_members_page_opens(self):
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(f"/manage/org/{self.mine.id}/members/").status_code, 200)

    def test_a_plain_member_cannot_reach_the_area_at_all(self):
        self.client.force_login(self.member)
        self.assertEqual(self.client.get("/manage/").status_code, 403)

    def test_the_overview_counts_only_your_organisations(self):
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get("/manage/").context["org_count"], 1)


class SystemPagesMovedTests(Fixture):
    """Sync, enquiries, news and the page editor answer to the super admin now."""

    def test_they_are_gone_from_the_organisation_admin(self):
        self.client.force_login(self.owner)
        for path in ("/manage/sync/", "/manage/enquiries/", "/manage/news/", "/manage/pages/"):
            self.assertEqual(self.client.get(path).status_code, 404, path)

    def test_an_org_admin_cannot_reach_them_at_their_new_home(self):
        self.client.force_login(self.owner)
        for path in ("/admin/sync/", "/admin/enquiries/", "/admin/news/", "/admin/pages/"):
            resp = self.client.get(path)
            self.assertIn(resp.status_code, (302, 403), f"{path} -> {resp.status_code}")


class MessagingTests(Fixture):
    def test_an_admin_can_send_a_notice_to_everyone(self):
        self.client.force_login(self.owner)
        self.client.post("/manage/messages/new/", {
            "org": self.mine.id, "subject": "Tips close Friday", "body": "Get them in.",
        })
        thread = MessageThread.objects.get()
        self.assertEqual(thread.org, self.mine)
        self.assertEqual(thread.kind, MessageThread.KIND_NOTICE)
        self.assertTrue(thread.is_broadcast)
        self.assertEqual(thread.messages.count(), 1)

    def test_naming_recipients_makes_it_private_to_them(self):
        self.client.force_login(self.owner)
        self.client.post("/manage/messages/new/", {
            "org": self.mine.id, "subject": "A word", "body": "Just you.",
            "recipients": [self.member.id],
        })
        thread = MessageThread.objects.get()
        self.assertFalse(thread.is_broadcast)
        self.assertTrue(thread.can_read(self.member))
        self.assertFalse(thread.can_read(self.rival))

    def test_a_posted_recipient_from_another_organisation_is_dropped(self):
        """Recipient ids come from a form, so they are filtered through the
        organisation's own membership rather than trusted."""
        self.client.force_login(self.owner)
        self.client.post("/manage/messages/new/", {
            "org": self.mine.id, "subject": "Oi", "body": "Hello",
            "recipients": [self.member.id, self.rival.id],
        })
        thread = MessageThread.objects.get()
        self.assertEqual(list(thread.recipients.all()), [self.member])

    def test_a_rival_admin_cannot_open_your_thread(self):
        thread = MessageThread.objects.create(
            org=self.mine, kind=MessageThread.KIND_RAISED,
            subject="Private", started_by=self.member,
        )
        self.client.force_login(self.rival)
        self.assertEqual(self.client.get(f"/manage/messages/{thread.id}/").status_code, 404)

    def test_replying_marks_the_thread_answered(self):
        thread = MessageThread.objects.create(
            org=self.mine, kind=MessageThread.KIND_RAISED,
            subject="Question", started_by=self.member,
        )
        Message.objects.create(thread=thread, author=self.member, body="How do I tip?")
        self.client.force_login(self.owner)
        self.client.post(f"/manage/messages/{thread.id}/", {"body": "Like this."})
        thread.refresh_from_db()
        self.assertEqual(thread.status, MessageThread.STATUS_ANSWERED)
        self.assertEqual(thread.messages.count(), 2)

    def test_a_raised_thread_is_not_readable_by_other_members(self):
        """A member raising a problem is entitled to have it read by the admins
        and not by the rest of the room."""
        thread = MessageThread.objects.create(
            org=self.mine, kind=MessageThread.KIND_RAISED,
            subject="Private", started_by=self.member,
        )
        other = User.objects.create_user(email="x@w.com", password="x", display_name="Ex")
        OrgMember.objects.create(user=other, org=self.mine, role=OrgMember.ROLE_PARTICIPANT)
        self.assertTrue(thread.can_read(self.member))
        self.assertTrue(thread.can_read(self.owner))
        self.assertFalse(thread.can_read(other))

    def test_a_broadcast_is_readable_by_somebody_who_joins_later(self):
        thread = MessageThread.objects.create(
            org=self.mine, kind=MessageThread.KIND_NOTICE,
            subject="Welcome", started_by=self.owner,
        )
        latecomer = User.objects.create_user(email="l@w.com", password="x", display_name="Lee")
        OrgMember.objects.create(user=latecomer, org=self.mine, role=OrgMember.ROLE_PARTICIPANT)
        self.assertTrue(thread.can_read(latecomer))


class MemberSideTests(Fixture):
    """The other end of the same threads."""

    def test_a_member_can_raise_something_with_their_admins(self):
        self.client.force_login(self.member)
        self.client.post(f"/leagues/{self.mine.id}/messages/", {
            "subject": "Tips didn't save", "body": "Round 3 went missing.",
        })
        thread = MessageThread.objects.get()
        self.assertEqual(thread.kind, MessageThread.KIND_RAISED)
        self.assertEqual(thread.started_by, self.member)
        self.assertEqual(thread.org, self.mine)

    def test_it_shows_up_in_the_admins_queue(self):
        self.client.force_login(self.member)
        self.client.post(f"/leagues/{self.mine.id}/messages/", {
            "subject": "Tips didn't save", "body": "Round 3 went missing.",
        })
        self.client.force_login(self.owner)
        body = self.client.get("/manage/messages/").content
        self.assertIn(b"Tips didn&#x27;t save", body)

    def test_a_non_member_cannot_open_the_page(self):
        self.client.force_login(self.rival)
        self.assertEqual(
            self.client.get(f"/leagues/{self.mine.id}/messages/").status_code, 404,
        )

    def test_a_member_cannot_open_a_thread_they_are_not_party_to(self):
        thread = MessageThread.objects.create(
            org=self.mine, kind=MessageThread.KIND_RAISED,
            subject="Somebody else's", started_by=self.owner,
        )
        other = User.objects.create_user(email="q@w.com", password="x", display_name="Qi")
        OrgMember.objects.create(user=other, org=self.mine, role=OrgMember.ROLE_PARTICIPANT)
        self.client.force_login(other)
        self.assertEqual(
            self.client.get(f"/leagues/{self.mine.id}/messages/{thread.id}/").status_code, 404,
        )
