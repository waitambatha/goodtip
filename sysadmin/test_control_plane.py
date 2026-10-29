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

from sysadmin import hub
from sysadmin.templatetags.gtadmin import (
    compact_since, gta_dashboard_stats,
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

    def test_every_registered_model_is_still_reachable_from_the_menu(self):
        """Register a model, forget sysadmin/hub.py, and it must still be
        linked.

        Walks admin.site's own registry rather than a hand-written list, so a
        model added next year is covered by this test the day it is registered.
        Since 26 Oct 2026 the menu names every table again, under Databases →
        Staging (client: "when I clicked on it, I should see the tables"), so
        that is where each one must be.
        """
        sign_in_to_admin(self.client, self.boss)
        html = self.client.get(reverse("admin:index")).content.decode()
        rail = html[html.index('id="nav-sidebar"'):html.index("gta-rail-foot")]

        for model in admin.site._registry:
            meta = model._meta
            url = reverse("admin:hq_database_table", args=["staging", meta.label_lower])
            self.assertIn(
                f'href="{url}"', rail,
                f"{meta.app_label}.{meta.model_name} is reachable from nowhere",
            )

    def test_an_unclaimed_model_lands_in_the_fallback_group(self):
        """The safety net, exercised directly — nothing in the project is
        currently unclaimed, so the only way to test it is to hand it one."""
        apps = [{
            "app_label": "somewhere",
            "name": "Somewhere",
            "models": [{
                "object_name": "Widget", "name": "Widgets",
                "admin_url": "/admin/somewhere/widget/", "add_url": None,
            }],
        }]
        cats = hub.categories(apps)
        self.assertEqual(len(cats), 1)
        self.assertEqual(cats[0]["key"], "other")
        self.assertEqual(cats[0]["tables"][0]["name"], "Widgets")

    def test_a_group_with_nothing_in_it_is_not_rendered(self):
        """An empty heading is worse than no heading."""
        self.assertEqual(hub.categories([]), [])

    def test_the_group_holding_the_current_page_is_marked(self):
        apps = [{
            "app_label": "accounts", "name": "Accounts",
            "models": [{
                "object_name": "User", "name": "Users",
                "admin_url": "/admin/accounts/user/", "add_url": None,
            }],
        }]
        cats = hub.categories(apps, "/admin/accounts/user/")
        self.assertTrue(cats[0]["is_current"])

    def test_category_keys_are_unique(self):
        keys = [c.key for c in hub.CATEGORIES] + [hub.FALLBACK.key]
        self.assertEqual(len(keys), len(set(keys)))

    def test_security_tables_are_not_also_data_categories(self):
        """A table reachable from two menu items is the duplication the flat
        rail exists to remove — the client named it about News & blog."""
        apps = [{
            "app_label": "sysadmin", "name": "Sysadmin",
            "models": [{
                "object_name": "LoginEvent", "name": "Login events",
                "admin_url": "/admin/sysadmin/loginevent/", "add_url": None,
            }],
        }]
        self.assertEqual(hub.categories(apps), [])
        self.assertEqual(len(hub.security_tables(apps)), 1)

    def test_a_table_screen_carries_its_own_colour(self):
        apps = [{
            "app_label": "tipping", "name": "Tipping",
            "models": [{
                "object_name": "Tip", "name": "Tips",
                "admin_url": "/admin/tipping/tip/", "add_url": None,
            }],
        }]
        accent = hub.accent_for_path(apps, "/admin/tipping/tip/3/change/")
        self.assertEqual(accent, hub.BY_REF["tipping.tip"].accent)
        self.assertEqual(hub.accent_for_path(apps, "/admin/"), "")

    def test_the_dashboard_card_links_to_both_databases_and_the_sync(self):
        sign_in_to_admin(self.client, self.boss)
        res = self.client.get(reverse("admin:index"))
        for url in (reverse("admin:hq_database", args=["live"]),
                    reverse("admin:hq_database", args=["staging"]),
                    reverse("admin:hq_backup_history")):
            self.assertContains(res, f'href="{url}"')


class MenuTests(TestCase):
    """What the menu lists, and for whom."""

    def setUp(self):
        self.boss = User.objects.create_superuser(
            email="boss@example.com", password="pw", display_name="Boss",
        )

    def test_the_dropdowns_open_where_you_are(self):
        """The menu is dropdowns since 26 Oct 2026 ("when you click
        databases, a dropdown... you choose between live and staging") — and
        one opens by itself when the page is inside it, closed everywhere
        else, so the rail is never taller than it needs to be."""
        sign_in_to_admin(self.client, self.boss)

        def rail(url):
            html = self.client.get(url).content.decode()
            return html[html.index('id="nav-sidebar"'):html.index("gta-rail-foot")]

        home = rail(reverse("admin:index"))
        self.assertIn('data-tree="databases">', home)
        inside = rail(reverse("admin:hq_database_table", args=["live", "tipping.tip"]))
        self.assertIn('data-tree="databases" open>', inside)
        self.assertIn('data-tree="db-live" open>', inside)
        self.assertIn('data-tree="db-staging">', inside)

    def test_the_four_hubs_are_in_the_menu(self):
        sign_in_to_admin(self.client, self.boss)
        html = self.client.get(reverse("admin:index")).content.decode()
        for name in ("admin:hq_tables", "admin:hq_home",
                     "admin:hq_team_home", "admin:hq_security"):
            self.assertIn(f'href="{reverse(name)}"', html, name)

    def test_the_hq_screens_are_still_one_click_from_the_menu(self):
        """They moved off the rail and onto the HQ hub. Before the menus were
        merged these four were reachable only by typing their URL, and losing
        them again to a tidier rail would be the same bug twice."""
        sign_in_to_admin(self.client, self.boss)
        html = self.client.get(reverse("admin:hq_home")).content.decode()
        for name in ("admin:hq_sync", "admin:hq_enquiries",
                     "admin:hq_news", "admin:hq_pages"):
            self.assertIn(f'href="{reverse(name)}"', html, name)

    def test_news_and_blog_appears_once_in_the_menu(self):
        """It used to be two rail entries with one name — the HQ editor and
        the `newspost` table — which is what the client actually reported.
        Since 26 Oct 2026 it is ONE entry, the story list ("on the menu... the
        admin can go fast and see the blogs"), and never the raw table."""
        sign_in_to_admin(self.client, self.boss)
        html = self.client.get(reverse("admin:index")).content.decode()
        rail = html[html.index('id="nav-sidebar"'):html.index("gta-rail-foot")]
        self.assertEqual(rail.count(f'href="{reverse("admin:hq_news")}"'), 1)
        self.assertNotIn(reverse("admin:admin_panel_newspost_changelist"), rail)

    def test_the_jump_box_can_still_reach_a_table(self):
        """The palette indexes the rail, and the rail stopped naming tables.
        Typing "/" then "tips" is the fastest route in the admin; a tidier
        menu does not get to cost it."""
        sign_in_to_admin(self.client, self.boss)
        html = self.client.get(reverse("admin:index")).content.decode()
        # Since 26 Oct 2026 the menu names every table again, under Databases
        # → Live / Staging, and the palette indexes those rows.
        rail = html[html.index('id="nav-sidebar"'):html.index("gta-rail-foot")]
        self.assertIn(reverse("admin:hq_database_table", args=["staging", "tipping.tip"]), rail)
        self.assertIn(reverse("admin:hq_database_table", args=["live", "tipping.tip"]), rail)

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
        self.assertNotIn(reverse("admin:hq_home"), html)

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


class HubTests(TestCase):
    """The screens the flat menu opens onto.

    One click to a page of cards, one click to the thing. What these check is
    that the second click always exists — a hub that lists a card leading
    nowhere is worse than the dropdown it replaced, because at least the
    dropdown's contents were links.
    """

    def setUp(self):
        self.boss = User.objects.create_superuser(
            email="boss@example.com", password="pw", display_name="Boss",
        )
        sign_in_to_admin(self.client, self.boss)

    def test_every_hub_renders(self):
        for name in ("admin:hq_tables", "admin:hq_home",
                     "admin:hq_team_home", "admin:hq_security"):
            with self.subTest(name):
                self.assertEqual(self.client.get(reverse(name)).status_code, 200)

    def test_each_database_card_opens_its_own_page(self):
        # Tables & models became Databases (client, 25 Oct 2026): the hub is
        # the two databases, each opening onto all of its tables.
        res = self.client.get(reverse("admin:hq_tables"))
        for db in ("live", "staging"):
            with self.subTest(db):
                href = reverse("admin:hq_database", args=[db])
                self.assertContains(res, href)
                self.assertEqual(self.client.get(href).status_code, 200)

    def test_a_category_that_does_not_exist_is_a_404_not_a_500(self):
        res = self.client.get(reverse("admin:hq_tables_category", args=["banana"]))
        self.assertEqual(res.status_code, 404)

    def test_a_table_card_states_its_size_and_both_ways_in(self):
        res = self.client.get(reverse("admin:hq_tables_category", args=["tipping"]))
        self.assertContains(res, reverse("admin:tipping_tip_changelist"))
        self.assertContains(res, reverse("admin:tipping_tip_add"))
        tip = next(t for t in res.context["category"]["tables"]
                   if t["ref"] == "tipping.tip")
        self.assertIsNotNone(tip["rows"])

    def test_a_hub_card_never_leads_nowhere(self):
        for name in ("admin:hq_home", "admin:hq_team_home"):
            res = self.client.get(reverse(name))
            self.assertTrue(res.context["cards"], name)
            for card in res.context["cards"]:
                with self.subTest(f"{name}: {card['label']}"):
                    self.assertTrue(card["href"])

    def test_security_shows_what_changed_and_who_signed_in(self):
        res = self.client.get(reverse("admin:hq_security"))
        self.assertContains(res, "What changed, and who changed it")
        self.assertContains(res, reverse("admin:sysadmin_auditlog_changelist"))
        # The sign-in this test's own setUp performed.
        self.assertContains(res, self.boss.email)

    def test_every_screen_offers_a_way_back_up(self):
        """Not history.back(): a real parent, so the button means the same
        thing after a save as it does after following somebody's link."""
        cases = [
            (reverse("admin:hq_tables_category", args=["tipping"]),
             reverse("admin:hq_tables")),
            (reverse("admin:tipping_tip_changelist"),
             reverse("admin:hq_tables_category", args=["tipping"])),
            (reverse("admin:sysadmin_loginevent_changelist"),
             reverse("admin:hq_security")),
        ]
        for path, parent in cases:
            with self.subTest(path):
                res = self.client.get(path)
                html = res.content.decode()
                # The hub screens draw the gx shell's back link since 26 Oct 2026.
                self.assertTrue('class="gta-back"' in html or 'class="gx-back"' in html)
                self.assertContains(res, f'href="{parent}"')

    def test_a_table_screen_is_tinted_and_the_dashboard_is_not(self):
        res = self.client.get(reverse("admin:tipping_tip_changelist"))
        self.assertContains(res, "--tbl-accent:")
        self.assertNotContains(self.client.get(reverse("admin:index")),
                               "--tbl-accent:")

    def test_restricted_admins_see_only_what_they_hold(self):
        """A card leading to a 403 teaches people the product is broken."""
        from sysadmin.models import AdminAccess, AdminGrant

        writer = User.objects.create_user(
            email="writer@example.com", password="pw", display_name="Writer",
        )
        writer.is_staff = True
        writer.save(update_fields=["is_staff"])
        access = AdminAccess.objects.create(user=writer, is_full_access=False)
        # News & blog left HQ for its own menu row (26 Oct 2026), so the
        # inbox is the one HQ screen this admin is given here.
        AdminGrant.objects.create(access=access, capability="enquiries.read")
        sign_in_to_admin(self.client, writer)

        labels = [c["label"] for c in
                  self.client.get(reverse("admin:hq_home")).context["cards"]]
        self.assertEqual(labels, ["Enquiries inbox"])

        labels = [c["label"] for c in
                  self.client.get(reverse("admin:hq_team_home")).context["cards"]]
        self.assertEqual(labels, ["Your work"])
