"""The control plane's menu and dashboard.

The one invariant worth defending here is that the curated grouping never
hides anything. SECTIONS names models explicitly, which is what makes the menu
readable and also what makes it possible to register a model and forget it —
so the first test below walks every registered model and insists the dashboard
still links to it, and the second proves the fallback group is what catches
one nobody claimed.

The rest pin what the split left broken: /admin/ listed neither the sync panel
nor the enquiry inbox nor the news editor, so the only way to those screens was
to already know their URL.
"""
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from sysadmin.templatetags.gtadmin import (
    SECTIONS, compact_since, gta_dashboard_stats, gta_sections,
)


User = get_user_model()


def sign_in_to_admin(client, user):
    """Log in and clear the admin's second factor.

    /admin/ is behind an emailed one-time code (sysadmin.middleware), so
    force_login alone lands on the verify screen rather than the page under
    test. Stamping the session is what the OTP flow itself does on success.
    """
    from sysadmin import otp

    client.force_login(user)
    session = client.session
    otp.mark_verified(session)
    session.save()


class SectionMapTests(TestCase):
    """The grouping regroups; it must never subtract."""

    def setUp(self):
        self.boss = User.objects.create_superuser(
            email="boss@example.com", password="pw", display_name="Boss",
        )

    def test_every_registered_model_is_still_reachable_from_the_dashboard(self):
        """Register a model, forget SECTIONS, and it must still be linked.

        Walks admin.site's own registry rather than a hand-written list, so a
        model added next year is covered by this test the day it is registered.
        """
        sign_in_to_admin(self.client, self.boss)
        html = self.client.get(reverse("admin:index")).content.decode()

        for model in admin.site._registry:
            meta = model._meta
            url = reverse(
                f"admin:{meta.app_label}_{meta.model_name}_changelist"
            )
            self.assertIn(
                f'href="{url}"', html,
                f"{meta.app_label}.{meta.model_name} vanished from the dashboard",
            )

    def test_an_unclaimed_model_lands_in_the_fallback_group(self):
        """The safety net, exercised directly — nothing in the project is
        currently unclaimed, so the only way to test it is to hand it one."""
        context = {"available_apps": [{
            "app_label": "somewhere",
            "name": "Somewhere",
            "models": [{
                "object_name": "Widget", "name": "Widgets",
                "admin_url": "/admin/somewhere/widget/", "add_url": None,
            }],
        }]}
        sections = gta_sections(context)
        self.assertEqual(len(sections), 1)
        self.assertEqual(sections[0]["key"], "other")
        self.assertEqual(sections[0]["models"][0]["name"], "Widgets")

    def test_a_group_with_nothing_in_it_is_not_rendered(self):
        """An empty heading is worse than no heading."""
        self.assertEqual(gta_sections({"available_apps": []}), [])

    def test_the_group_holding_the_current_page_arrives_open(self):
        context = {
            "available_apps": [{
                "app_label": "accounts", "name": "Accounts",
                "models": [{
                    "object_name": "User", "name": "Users",
                    "admin_url": "/admin/accounts/user/", "add_url": None,
                }],
            }],
            "request": type("R", (), {"path": "/admin/accounts/user/"})(),
        }
        self.assertTrue(gta_sections(context)[0]["is_open"])

    def test_section_keys_are_unique(self):
        keys = [s[0] for s in SECTIONS]
        self.assertEqual(len(keys), len(set(keys)))


class MenuTests(TestCase):
    """What the menu lists, and for whom."""

    def setUp(self):
        self.boss = User.objects.create_superuser(
            email="boss@example.com", password="pw", display_name="Boss",
        )

    def test_the_hq_screens_are_in_the_menu(self):
        """Before the menus were merged these four were reachable only by
        typing their URL: neither of the two rails listed the other's pages."""
        sign_in_to_admin(self.client, self.boss)
        html = self.client.get(reverse("admin:index")).content.decode()
        for name in ("admin:hq_sync", "admin:hq_enquiries",
                     "admin:hq_news", "admin:hq_pages"):
            self.assertIn(f'href="{reverse(name)}"', html, name)

    def test_staff_who_are_not_superusers_get_no_hq_band(self):
        """HQ is the platform owner's, and site content is superuser-only in
        the view as well — the menu must not advertise a 403."""
        staffer = User.objects.create_user(
            email="staff@example.com", password="pw", display_name="Staff",
        )
        staffer.is_staff = True
        staffer.save(update_fields=["is_staff"])
        sign_in_to_admin(self.client, staffer)

        html = self.client.get(reverse("admin:index")).content.decode()
        self.assertNotIn(reverse("admin:hq_sync"), html)
        self.assertNotIn(reverse("admin:hq_pages"), html)

    def test_the_shell_class_marks_pages_that_have_a_rail(self):
        """The chrome is scoped to it; without it the login screen would be
        indented past a menu that is not there."""
        sign_in_to_admin(self.client, self.boss)
        self.assertContains(self.client.get(reverse("admin:index")), "gta-shell")
        self.client.logout()
        self.assertNotContains(self.client.get("/admin/login/"), "gta-shell")


class DashboardTests(TestCase):
    def setUp(self):
        self.boss = User.objects.create_superuser(
            email="boss@example.com", password="pw", display_name="Boss",
        )

    def test_the_tiles_and_both_charts_render(self):
        sign_in_to_admin(self.client, self.boss)
        res = self.client.get(reverse("admin:index"))
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "c-dash-data")            # the activity chart
        self.assertContains(res, 'id="spark-1-data"')      # a tile's sparkline
        self.assertContains(res, 'id="spark-4-data"')      # and the last one's
        self.assertContains(res, "Waiting on you")

    def test_the_window_switch_widens_the_charts(self):
        sign_in_to_admin(self.client, self.boss)
        res = self.client.get(reverse("admin:index"), {"days": "90"})
        self.assertContains(res, "last 90 days")

    def test_a_junk_window_falls_back_rather_than_500ing(self):
        sign_in_to_admin(self.client, self.boss)
        for junk in ("banana", "99999", "-1", ""):
            res = self.client.get(reverse("admin:index"), {"days": junk})
            self.assertEqual(res.status_code, 200, junk)
            self.assertContains(res, "last 14 days")

    def test_every_queue_row_links_somewhere(self):
        """A count nobody can act on belongs on the system report, not on the
        screen headed "waiting"."""
        sign_in_to_admin(self.client, self.boss)
        res = self.client.get(reverse("admin:index"))
        stats = gta_dashboard_stats({"request": res.wsgi_request})

        self.assertEqual(len(stats["alerts"]), 3)
        for alert in stats["alerts"]:
            self.assertTrue(alert["url"], f"{alert['label']} leads nowhere")
            self.assertContains(res, alert["label"])


class CompactSinceTests(TestCase):
    def test_it_reads_at_a_glance(self):
        now = timezone.now()
        cases = [
            (timezone.timedelta(seconds=5), "just now"),
            (timezone.timedelta(minutes=2), "2m ago"),
            (timezone.timedelta(hours=3), "3h ago"),
            (timezone.timedelta(days=5), "5d ago"),
            (timezone.timedelta(days=70), "2mo ago"),
        ]
        for delta, expected in cases:
            self.assertEqual(compact_since(now - delta), expected, expected)

    def test_nothing_in_gives_nothing_out(self):
        self.assertEqual(compact_since(None), "")
