"""The one-tap "Sign in" link in code emails (accounts/code_links.py)."""
from django.core import mail
from django.test import Client, TestCase
from django.urls import reverse

from accounts.code_links import make_url, read
from accounts.models import LoginCode, User


def _token_from(url: str) -> str:
    return url.rstrip("/").rsplit("/", 1)[-1]


class MemberCodeLinkTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="member@example.com", password="Str0ng!pass", display_name="Member"
        )

    def _password_step(self, client):
        return client.post(reverse("accounts:login"), {
            "email": "member@example.com", "password": "Str0ng!pass",
        })

    def _fresh_link(self):
        row, code = LoginCode.issue(self.user)
        return reverse("accounts:verify_link", args=[_token_from(make_url(_Req(), row, code, "accounts:verify_link"))]), row

    def test_the_code_email_carries_a_sign_in_link(self):
        self._password_step(self.client)
        body = mail.outbox[-1].body
        self.assertIn(reverse("accounts:verify_link", args=["x"]).rsplit("x/", 1)[0], body)

    def test_link_signs_in_the_browser_that_typed_the_password(self):
        self._password_step(self.client)
        url, _ = self._fresh_link()
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.user.pk)

    def test_link_in_another_browser_does_nothing_and_spends_nothing(self):
        """Email alone must not get anyone in, and a mail scanner following
        the link must not use the code up."""
        self._password_step(self.client)
        url, row = self._fresh_link()
        stranger = Client()
        stranger.get(url)
        self.assertNotIn("_auth_user_id", stranger.session)
        row.refresh_from_db()
        self.assertIsNone(row.consumed_at)
        self.assertEqual(row.attempts, 0)
        # And it still works where it belongs.
        self.client.get(url)
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.user.pk)

    def test_a_forged_link_fails(self):
        self._password_step(self.client)
        self.client.get(reverse("accounts:verify_link", args=["not-a-real-token"]))
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_a_link_works_once(self):
        self._password_step(self.client)
        url, _ = self._fresh_link()
        self.client.get(url)
        self.client.logout()
        self._password_step(self.client)
        self.client.get(url)          # the old link, after a fresh password step
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_tokens_round_trip_and_reject_tampering(self):
        row, code = LoginCode.issue(self.user)
        token = _token_from(make_url(_Req(), row, code, "accounts:verify_link"))
        self.assertEqual(read(token, 600), (row.pk, code))
        self.assertIsNone(read(token[:-2] + "xx", 600))


class AdminCodeLinkTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(email="root@goodtip.test", password="x", display_name="Root")
        self.admin.is_staff = True
        self.admin.is_superuser = True
        self.admin.save(update_fields=["is_staff", "is_superuser"])

    def _link(self, user):
        row, code = LoginCode.issue(user, purpose=LoginCode.PURPOSE_ADMIN)
        return reverse("sysadmin:admin_verify_link", args=[_token_from(make_url(_Req(), row, code, "sysadmin:admin_verify_link"))])

    def test_link_opens_the_admin_for_the_signed_in_admin(self):
        self.client.force_login(self.admin)
        self.client.get(reverse("sysadmin:admin_verify"))
        resp = self.client.get(self._link(self.admin))
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self.client.get("/admin/").status_code, 200)

    def test_link_is_nothing_without_the_signed_in_session(self):
        resp = Client().get(self._link(self.admin))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/admin/login/", resp["Location"])

    def test_one_admins_link_does_not_open_the_admin_for_another(self):
        other = User.objects.create_user(email="other@goodtip.test", password="x", display_name="Other")
        other.is_staff = True
        other.save(update_fields=["is_staff"])
        self.client.force_login(other)
        self.client.get(self._link(self.admin))
        self.assertNotEqual(self.client.get("/admin/").status_code, 200)


class _Req:
    """Enough of a request for make_url outside a view."""
    def build_absolute_uri(self, path):
        return "https://goodtip.test" + path


class CodesReachStagingMembersTests(TestCase):
    """On staging the allowlist is one address; a sign-in code must still
    reach whoever asked for it (23 Nov 2026: Ian could not sign in)."""

    def test_the_sign_in_code_gets_through_the_staging_allowlist(self):
        from django.test import override_settings

        user = User.objects.create_user(email="ian@bigcorp.example", password="Str0ng!pass", display_name="Ian")
        with override_settings(
            EMAIL_BACKEND="goodtip.email_backends.AllowlistEmailBackend",
            EMAIL_ALLOWLIST_DELEGATE="django.core.mail.backends.locmem.EmailBackend",
            EMAIL_ALLOWLIST="someone.else@gmail.com",
        ):
            self.client.post(reverse("accounts:login"), {"email": user.email, "password": "Str0ng!pass"})
        self.assertEqual([m.to for m in mail.outbox], [["ian@bigcorp.example"]])
