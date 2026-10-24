"""Setting up an organisation for someone else, and inviting its admin
(client, 19 Oct 2026). See sysadmin/org_admin_views.py."""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from orgs.models import Organisation, OrgMember
from sysadmin.test_delegation import make_admin, sign_in

User = get_user_model()


@override_settings(STAGING_GATE=False)
class OrgAdminInviteTests(TestCase):
    def setUp(self):
        self.boss = User.objects.create_superuser(email="boss@example.com", password="pw")

    def test_someone_without_the_capability_gets_a_404_not_a_403(self):
        plain = make_admin("plain@example.com", by=self.boss)
        sign_in(self.client, plain)
        self.assertEqual(self.client.get(reverse("admin:hq_org_admin")).status_code, 404)

    def test_creating_an_org_makes_the_named_person_its_admin_not_ian(self):
        sign_in(self.client, self.boss)
        with patch("goodtip.mail.send_template") as mocked:
            resp = self.client.post(reverse("admin:hq_org_admin_create"), {
                "name": "Riverside Primary", "admin_name": "Jo Principal",
                "admin_email": "jo@riverside.example",
            })
        self.assertRedirects(resp, reverse("admin:hq_org_admin"))
        org = Organisation.objects.get(name="Riverside Primary")
        self.assertEqual(org.created_by, self.boss)
        jo = User.objects.get(email="jo@riverside.example")
        member = OrgMember.objects.get(org=org, user=jo)
        self.assertTrue(member.is_league_owner)
        self.assertEqual(member.role, OrgMember.ROLE_BOTH)
        self.assertFalse(OrgMember.objects.filter(org=org, user=self.boss).exists())
        self.assertFalse(jo.has_usable_password())
        mocked.assert_called_once()
        self.assertEqual(mocked.call_args.kwargs["to"], ["jo@riverside.example"])
        self.assertIsNotNone(mocked.call_args.kwargs["context"]["link"])

    def test_an_email_that_already_has_an_account_is_just_told_to_log_in(self):
        existing = User.objects.create_user(
            email="already@example.com", password="RealPass123!", display_name="Already Here",
        )
        sign_in(self.client, self.boss)
        with patch("goodtip.mail.send_template") as mocked:
            self.client.post(reverse("admin:hq_org_admin_create"), {
                "name": "Second Org", "admin_name": "Already Here",
                "admin_email": "already@example.com",
            })
        existing.refresh_from_db()
        self.assertTrue(existing.has_usable_password())
        self.assertIsNone(mocked.call_args.kwargs["context"]["link"])

    def test_a_missing_field_is_refused_without_creating_anything(self):
        sign_in(self.client, self.boss)
        self.client.post(reverse("admin:hq_org_admin_create"), {
            "name": "No Admin Org", "admin_name": "", "admin_email": "",
        })
        self.assertFalse(Organisation.objects.filter(name="No Admin Org").exists())

    def test_a_bad_email_is_refused(self):
        sign_in(self.client, self.boss)
        self.client.post(reverse("admin:hq_org_admin_create"), {
            "name": "Bad Email Org", "admin_name": "Someone",
            "admin_email": "not-an-email",
        })
        self.assertFalse(Organisation.objects.filter(name="Bad Email Org").exists())

    def test_creating_without_the_capability_is_a_404(self):
        plain = make_admin("plain2@example.com", by=self.boss)
        sign_in(self.client, plain)
        resp = self.client.post(reverse("admin:hq_org_admin_create"), {
            "name": "Blocked Org", "admin_name": "Someone", "admin_email": "x@example.com",
        })
        self.assertEqual(resp.status_code, 404)
        self.assertFalse(Organisation.objects.filter(name="Blocked Org").exists())

    def test_the_hub_lists_orgs_ian_set_up_but_is_not_in(self):
        sign_in(self.client, self.boss)
        with patch("goodtip.mail.send_template"):
            self.client.post(reverse("admin:hq_org_admin_create"), {
                "name": "Listed Org", "admin_name": "Someone",
                "admin_email": "someone@example.com",
            })
        resp = self.client.get(reverse("admin:hq_org_admin"))
        self.assertContains(resp, "Listed Org")
