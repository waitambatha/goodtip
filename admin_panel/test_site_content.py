"""Tests for the Site content editor and the slots it drives.

The load-bearing property here is the fallback: a database with no overrides
must render the public pages exactly as the hard-coded templates did. If that
ever stops being true, converting a section to editable slots silently changes
what visitors read, and nobody finds out from a stack trace.
"""
import re
import subprocess
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.core.cache import cache
from django.template.loader import get_template
from django.test import RequestFactory, TestCase
from django.urls import reverse

from .models import SiteContent
from .site_blocks import BLOCKS, IMAGE, PAGES, RICH, TEXT, VIDEO

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


def public_request():
    """A request the public templates can render against.

    RequestFactory does not run middleware, so `request.user` is missing and
    the project's own context processors raise on it before any template code
    runs.
    """
    request = RequestFactory().get("/")
    request.user = AnonymousUser()
    return request


class RegistryTests(TestCase):
    def test_every_key_is_unique_and_namespaced_by_page(self):
        seen = set()
        for page in PAGES:
            for group in page.groups:
                for block in group.blocks:
                    self.assertNotIn(block.key, seen, f"duplicate key {block.key}")
                    seen.add(block.key)
                    self.assertTrue(
                        block.key.startswith(f"{page.slug}."),
                        f"{block.key} is declared on the {page.slug} page but not "
                        f"prefixed with it",
                    )

    def test_every_block_has_a_kind_we_can_render(self):
        for key, block in BLOCKS.items():
            self.assertIn(block.kind, (TEXT, RICH, IMAGE, VIDEO), key)

    def test_media_blocks_point_at_a_file_that_exists(self):
        """A media default is a path under static/ — a typo there is a broken
        image on the live front page, and {% static %} will not catch it."""
        for key, block in BLOCKS.items():
            if block.kind not in (IMAGE, VIDEO):
                continue
            self.assertTrue(block.default, f"{key} has no default asset")
            found = any(
                (Path(d) / block.default).exists() for d in settings.STATICFILES_DIRS
            )
            self.assertTrue(found, f"{key}: static/{block.default} does not exist")

    def test_text_blocks_have_a_default(self):
        for key, block in BLOCKS.items():
            if block.kind in (TEXT, RICH):
                self.assertTrue(block.default.strip(), f"{key} has no default copy")


class FallbackRenderTests(TestCase):
    """The home page with no overrides must be byte-identical to the version
    before any of it was made editable."""

    def test_defaults_reproduce_the_pre_cms_page(self):
        before = subprocess.run(
            ["git", "show", "HEAD:templates/public/home.html"],
            cwd=settings.BASE_DIR, capture_output=True, text=True,
        )
        if before.returncode != 0:
            self.skipTest("no git history available in this checkout")
        if "{% site_" in before.stdout:
            self.skipTest("HEAD already contains the editable version")

        scratch = Path(settings.BASE_DIR) / "templates" / "public" / "_home_pre_cms.html"
        scratch.write_text(before.stdout)
        try:
            request = public_request()
            ctx = {"active": "home"}
            was = get_template("public/_home_pre_cms.html").render(ctx, request)
            now = get_template("public/home.html").render(ctx, request)
            # The contact form's CSRF token is regenerated on every render, so
            # it is the one thing that legitimately differs between the two.
            csrf = re.compile(r'name="csrfmiddlewaretoken" value="[^"]+"')
            self.assertEqual(csrf.sub("CSRF", was), csrf.sub("CSRF", now))
        finally:
            scratch.unlink(missing_ok=True)


class OverrideTests(TestCase):
    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    def test_an_override_reaches_the_public_page(self):
        SiteContent.objects.create(key="home.hero.sub", text="Tip for something better.")
        html = get_template("public/home.html").render({}, public_request())
        self.assertIn("Tip for something better.", html)
        self.assertNotIn(BLOCKS["home.hero.sub"].default, html)

    def test_a_blank_override_falls_back_to_the_default(self):
        SiteContent.objects.create(key="home.hero.sub", text="   ")
        html = get_template("public/home.html").render({}, public_request())
        self.assertIn(BLOCKS["home.hero.sub"].default, html)

    def test_saving_busts_the_cache(self):
        get_template("public/home.html").render({}, public_request())
        SiteContent.objects.create(key="home.hero.pill", text="Round 1 is live")
        html = get_template("public/home.html").render({}, public_request())
        self.assertIn("Round 1 is live", html)


class EditorAccessTests(TestCase):
    def setUp(self):
        cache.clear()
        self.boss = User.objects.create_superuser(
            email="boss@example.com", password="pw", display_name="Boss",
        )
        self.staffer = User.objects.create_user(
            email="staff@example.com", password="pw", display_name="Staffer",
        )
        self.staffer.is_staff = True
        self.staffer.save()

    def test_superuser_sees_the_editor(self):
        sign_in_to_admin(self.client, self.boss)
        res = self.client.get(reverse("admin:site_content_page", args=["home"]))
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "home.hero.title")

    def test_staff_without_superuser_is_refused(self):
        sign_in_to_admin(self.client, self.staffer)
        res = self.client.get(reverse("admin:site_content_page", args=["home"]))
        self.assertEqual(res.status_code, 403)

    def test_anonymous_is_sent_to_the_login(self):
        res = self.client.get(reverse("admin:site_content"))
        self.assertEqual(res.status_code, 302)

    def test_unknown_page_redirects_rather_than_500s(self):
        sign_in_to_admin(self.client, self.boss)
        res = self.client.get(reverse("admin:site_content_page", args=["nope"]))
        self.assertRedirects(res, reverse("admin:site_content"))


class EditorSaveTests(TestCase):
    def setUp(self):
        cache.clear()
        self.boss = User.objects.create_superuser(
            email="boss@example.com", password="pw", display_name="Boss",
        )
        sign_in_to_admin(self.client, self.boss)
        self.url = reverse("admin:site_content_page", args=["home"])

    def _post(self, **overrides):
        """Post the whole form the way a browser would, with defaults everywhere
        except the fields under test — a partial POST would look like every
        other field was cleared."""
        data = {}
        for key, block in BLOCKS.items():
            if block.kind in (TEXT, RICH):
                data[f"f_{key}"] = block.default
        data.update(overrides)
        return self.client.post(self.url, data)

    def test_editing_one_field_stores_only_that_one(self):
        res = self._post(**{"f_home.hero.pill": "Round 1 is live"})
        self.assertRedirects(res, self.url)
        self.assertEqual(SiteContent.objects.count(), 1)
        self.assertEqual(SiteContent.objects.get().key, "home.hero.pill")

    def test_a_field_left_at_its_default_is_not_stored(self):
        self._post()
        self.assertEqual(SiteContent.objects.count(), 0)

    def test_clearing_a_field_removes_the_override(self):
        SiteContent.objects.create(key="home.hero.pill", text="Round 1 is live")
        self._post(**{"f_home.hero.pill": ""})
        self.assertFalse(SiteContent.objects.filter(key="home.hero.pill").exists())

    def test_rich_html_is_sanitised_on_the_way_in(self):
        self._post(**{"f_home.hero.title": '<em>Hi</em><script>alert(1)</script>'})
        stored = SiteContent.objects.get(key="home.hero.title")
        self.assertIn("<em>Hi</em>", stored.html)
        self.assertNotIn("<script", stored.html)

    def test_inline_event_handlers_are_stripped(self):
        self._post(**{"f_home.faq.a1": '<a href="/x" onclick="steal()">go</a>'})
        stored = SiteContent.objects.get(key="home.faq.a1")
        self.assertNotIn("onclick", stored.html)


class SystemReportTests(TestCase):
    def setUp(self):
        self.boss = User.objects.create_superuser(
            email="boss@example.com", password="pw", display_name="Boss",
        )

    def test_report_renders_with_chart_payloads(self):
        sign_in_to_admin(self.client, self.boss)
        res = self.client.get(reverse("admin:system_report"))
        self.assertEqual(res.status_code, 200)
        for chart_id in ("c-growth-data", "c-logins-data", "c-sizes-data", "c-toporgs-data"):
            self.assertContains(res, chart_id)

    def test_range_switch_is_clamped_to_the_offered_options(self):
        sign_in_to_admin(self.client, self.boss)
        res = self.client.get(reverse("admin:system_report"), {"days": "99999"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.context["span"], 30)

    def test_a_junk_range_does_not_500(self):
        sign_in_to_admin(self.client, self.boss)
        res = self.client.get(reverse("admin:system_report"), {"days": "banana"})
        self.assertEqual(res.status_code, 200)

    def test_dashboard_renders(self):
        sign_in_to_admin(self.client, self.boss)
        res = self.client.get(reverse("admin:index"))
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "c-dash-data")


class MediaSlotTests(TestCase):
    """Media slots, which have three ways to change and one way to undo."""

    def setUp(self):
        cache.clear()
        self.boss = User.objects.create_superuser(
            email="boss@example.com", password="pw", display_name="Boss",
        )
        sign_in_to_admin(self.client, self.boss)
        self.url = reverse("admin:site_content_page", args=["home"])

    def _post(self, **overrides):
        data = {}
        for key, block in BLOCKS.items():
            if block.kind in (TEXT, RICH):
                data[f"f_{key}"] = block.default
        data.update(overrides)
        return self.client.post(self.url, data)

    def test_an_unedited_image_serves_the_static_default(self):
        html = get_template("public/home.html").render({}, public_request())
        self.assertIn(BLOCKS["home.hero.shot1"].default, html)

    def test_alt_text_alone_is_stored(self):
        self._post(**{"alt_home.hero.shot1": "The MCG under lights"})
        row = SiteContent.objects.get(key="home.hero.shot1")
        self.assertEqual(row.alt_text, "The MCG under lights")

    def test_resubmitting_the_same_alt_text_changes_nothing(self):
        SiteContent.objects.create(key="home.hero.shot1", alt_text="The MCG under lights")
        before = SiteContent.objects.get(key="home.hero.shot1").updated_at
        self._post(**{"alt_home.hero.shot1": "The MCG under lights"})
        self.assertEqual(SiteContent.objects.get(key="home.hero.shot1").updated_at, before)

    def test_reset_removes_the_row(self):
        SiteContent.objects.create(key="home.hero.shot1", alt_text="Whatever")
        self._post(**{"reset_home.hero.shot1": "1"})
        self.assertFalse(SiteContent.objects.filter(key="home.hero.shot1").exists())
