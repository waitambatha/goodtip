"""The admin's second factor.

What these pin is the property that made it worth building: reaching /admin/
requires a fresh emailed code REGARDLESS of how the session became
authenticated. A password alone does not do it, and neither does having signed
in through the member app.
"""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import LoginCode

from . import otp


User = get_user_model()


class AdminOTPGateTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            email="root@goodtip.test", password="x", display_name="Root",
        )
        self.admin.is_staff = True
        self.admin.is_superuser = True
        self.admin.save(update_fields=["is_staff", "is_superuser"])

    def test_password_alone_does_not_open_the_admin(self):
        """The whole point. force_login is a fully authenticated session."""
        self.client.force_login(self.admin)
        resp = self.client.get("/admin/")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], reverse("sysadmin:admin_verify"))

    def test_the_gate_covers_the_whole_admin_not_just_the_index(self):
        """A per-view gate is a gate somebody forgets to apply."""
        self.client.force_login(self.admin)
        for path in ("/admin/", "/admin/auth/", "/admin/accounts/user/"):
            resp = self.client.get(path)
            self.assertEqual(resp.status_code, 302, path)
            self.assertEqual(resp["Location"], reverse("sysadmin:admin_verify"), path)

    def test_visiting_the_gate_issues_a_code(self):
        self.client.force_login(self.admin)
        self.client.get(reverse("sysadmin:admin_verify"))
        self.assertTrue(
            LoginCode.objects.filter(
                user=self.admin, purpose=LoginCode.PURPOSE_ADMIN,
            ).exists()
        )

    def test_a_correct_code_lets_them_through_to_where_they_were_going(self):
        self.client.force_login(self.admin)
        self.client.get("/admin/accounts/user/")     # stores the destination
        self.client.get(reverse("sysadmin:admin_verify"))
        _, code = LoginCode.issue(self.admin, purpose=LoginCode.PURPOSE_ADMIN)
        resp = self.client.post(reverse("sysadmin:admin_verify"), {"code": code})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], "/admin/accounts/user/")
        # And the admin now actually opens.
        self.assertEqual(self.client.get("/admin/").status_code, 200)

    def test_a_wrong_code_does_not(self):
        self.client.force_login(self.admin)
        self.client.get(reverse("sysadmin:admin_verify"))
        LoginCode.issue(self.admin, purpose=LoginCode.PURPOSE_ADMIN)
        resp = self.client.post(reverse("sysadmin:admin_verify"), {"code": "000000"})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "isn&#x27;t right", status_code=200)
        self.assertEqual(self.client.get("/admin/").status_code, 302)

    def test_a_members_sign_in_code_is_not_an_admin_code(self):
        """Different purpose, different code. A code emailed to get into the
        tipping app must not also open the control plane."""
        self.client.force_login(self.admin)
        self.client.get(reverse("sysadmin:admin_verify"))
        _, member_code = LoginCode.issue(self.admin, purpose=LoginCode.PURPOSE_LOGIN)
        resp = self.client.post(reverse("sysadmin:admin_verify"), {"code": member_code})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.client.get("/admin/").status_code, 302)

    def test_verification_expires(self):
        self.client.force_login(self.admin)
        self.client.get(reverse("sysadmin:admin_verify"))
        _, code = LoginCode.issue(self.admin, purpose=LoginCode.PURPOSE_ADMIN)
        self.client.post(reverse("sysadmin:admin_verify"), {"code": code})
        self.assertEqual(self.client.get("/admin/").status_code, 200)

        stale = timezone.now() - otp.SESSION_TTL - timedelta(minutes=1)
        session = self.client.session
        session[otp.SESSION_KEY] = stale.isoformat()
        session.save()
        self.assertEqual(self.client.get("/admin/").status_code, 302)

    def test_a_junk_stamp_is_treated_as_unverified(self):
        session = self.client.session
        session[otp.SESSION_KEY] = "not-a-date"
        session.save()
        self.assertFalse(otp.is_verified(self.client.session))

    def test_the_gate_does_not_intercept_the_login_page(self):
        """Anonymous requests must still reach the admin's own login form."""
        resp = self.client.get("/admin/login/")
        self.assertEqual(resp.status_code, 200)

    def test_a_half_verified_admin_can_still_sign_out(self):
        """Otherwise a locked-out admin has no way to try another account."""
        self.client.force_login(self.admin)
        resp = self.client.get(reverse("sysadmin:admin_verify_cancel"))
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], "/admin/login/")

    def test_a_non_staff_user_is_left_to_django(self):
        """The gate does not stand in front of people the admin refuses anyway."""
        member = User.objects.create_user(
            email="member@goodtip.test", password="x", display_name="Member",
        )
        self.client.force_login(member)
        resp = self.client.get("/admin/")
        self.assertEqual(resp.status_code, 302)
        self.assertNotEqual(resp["Location"], reverse("sysadmin:admin_verify"))

    def test_the_next_target_cannot_leave_the_admin(self):
        """An open redirect out of an auth step is a phishing hop."""
        self.client.force_login(self.admin)
        session = self.client.session
        session[otp.NEXT_KEY] = "https://evil.example.com/"
        session.save()
        self.client.get(reverse("sysadmin:admin_verify"))
        _, code = LoginCode.issue(self.admin, purpose=LoginCode.PURPOSE_ADMIN)
        resp = self.client.post(reverse("sysadmin:admin_verify"), {"code": code})
        self.assertEqual(resp["Location"], "/admin/")
