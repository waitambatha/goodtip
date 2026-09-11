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


class TeamHubTests(TestCase):
    """One door in the rail, four rooms behind it.

    "Instead of dropdowns we just have a nice, well-detailed dashboard of it
    ... it's like grouped dashboards, so it gives me a nice user experience."
    """

    def setUp(self):
        from admin_panel.tests import sign_in_to_hq

        from .models import AdminAccess

        self.owner = User.objects.create_user(
            email="owner@goodtip.test", password="x", display_name="Olive",
        )
        self.owner.is_staff = self.owner.is_superuser = True
        self.owner.save(update_fields=["is_staff", "is_superuser"])
        AdminAccess.objects.update_or_create(
            user=self.owner, defaults={"is_full_access": True, "is_active": True},
        )
        self.helper = User.objects.create_user(
            email="helper@goodtip.test", password="x", display_name="Hal",
        )
        self.helper.is_staff = True
        self.helper.save(update_fields=["is_staff"])
        AdminAccess.objects.update_or_create(
            user=self.helper, defaults={"is_full_access": False, "is_active": True},
        )
        self._sign_in = sign_in_to_hq

    def test_the_hub_opens_for_somebody_who_runs_the_team(self):
        self._sign_in(self.client, self.owner)
        resp = self.client.get(reverse("admin:hq_team_home"))
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        # Every room is reachable from the one page.
        for url in ("hq_reviews", "hq_team", "hq_my_work", "hq_activity"):
            self.assertIn(reverse(f"admin:{url}"), html, url)

    def test_the_hub_shows_a_restricted_administrator_only_their_own_work(self):
        """It used to answer 403, which is the wrong answer to the right
        question. Somebody who does not run the team still has work of their
        own in this area; refusing them the door meant the rail had to carry a
        second entry just for them. The hub filters instead: every card asks
        the capability it leads to, so a restricted administrator gets the one
        card they can use rather than a refusal."""
        self._sign_in(self.client, self.helper)
        resp = self.client.get(reverse("admin:hq_team_home"))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual([c["label"] for c in resp.context["cards"]], ["Your work"])
        html = resp.content.decode()
        for shut in ("hq_team", "hq_reviews", "hq_activity"):
            self.assertNotIn(reverse(f"admin:{shut}"), html, shut)

    def test_the_old_team_hub_address_still_lands_somewhere(self):
        """Two hubs briefly meant two "Your team" pages. This one lost, but
        its address is in bookmarks and in the area's own links."""
        self._sign_in(self.client, self.owner)
        resp = self.client.get(reverse("admin:hq_team_hub"))
        self.assertRedirects(resp, reverse("admin:hq_team_home"))

    def _rail(self, url):
        html = self.client.get(url).content.decode()
        return html[html.index('class="gts-rail"'):html.index("</nav>")]

    def test_the_rail_carries_one_team_entry_and_not_four(self):
        """The point of the change. Four peers cost four lines of the rail on
        EVERY screen in HQ and told you nothing until you had opened them.

        Read from a screen outside the section, which is where that cost was
        being paid — inside it the four are listed on purpose, and that is the
        test below.
        """
        self._sign_in(self.client, self.owner)
        rail = self._rail(reverse("admin:hq_news"))
        self.assertIn(reverse("admin:hq_team_home"), rail)
        for gone in ("hq_reviews", "hq_activity"):
            self.assertNotIn(reverse(f"admin:{gone}"), rail, gone)

    def test_inside_the_section_the_rail_is_about_the_section(self):
        """The client: "when I click it, its menu should be about the other
        pages that were in its dashboard ... so I do not have the need to go to
        the main dashboard."

        So the four are absent from every other screen and present on these —
        the rail costs one line until you are standing in the section, and then
        moving between its screens is one click instead of a trip back through
        the hub.
        """
        self._sign_in(self.client, self.owner)
        rail = self._rail(reverse("admin:hq_my_work"))
        for there in ("hq_my_work", "hq_reviews", "hq_team", "hq_activity"):
            self.assertIn(reverse(f"admin:{there}"), rail, there)
        # And it says which one you are on.
        self.assertIn('class="gts-sub on"', rail)

    def test_the_section_menu_shows_only_what_its_viewer_may_open(self):
        """A restricted administrator gets a shorter list, never a link that
        refuses them — the three full-access screens are not theirs."""
        self._sign_in(self.client, self.helper)
        rail = self._rail(reverse("admin:hq_my_work"))
        for gone in ("hq_reviews", "hq_team", "hq_activity"):
            self.assertNotIn(reverse(f"admin:{gone}"), rail, gone)

    def test_an_administrator_without_the_team_keeps_your_work_in_the_rail(self):
        """It is a card on the hub for everyone else — but somebody who cannot
        open the hub must still be able to reach their own work."""
        self._sign_in(self.client, self.helper)
        html = self.client.get(reverse("admin:hq_my_work")).content.decode()
        rail = html[html.index('class="gts-rail"'):html.index("</nav>")]
        self.assertIn(reverse("admin:hq_my_work"), rail)

    def test_the_hub_counts_what_is_waiting(self):
        from .models import ChangeRequest

        ChangeRequest.objects.create(
            requested_by=self.helper, capability="news.write",
            summary="A new story", path="/admin/news/new/",
            status=ChangeRequest.PENDING,
        )
        self._sign_in(self.client, self.owner)
        html = self.client.get(reverse("admin:hq_team_home")).content.decode()
        # A hub whose cards are only labels is a menu with an extra click in
        # front of it — the number is what earns the page.
        self.assertIn("1 waiting", html)
