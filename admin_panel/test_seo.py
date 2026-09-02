"""The SEO fields the client asked to have exposed in the admin.

Covering the round trip, because it is the part with no other safety net: what
an admin types in the HQ, what lands on the response's <head>, and what the
sitemap and the redirect table then do with it. Every one of these is a runtime
concern that `manage.py check` cannot see.
"""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import NewsPost, PageSeo, Redirect
from .tests import sign_in_to_hq

User = get_user_model()


def _seo_post(**overrides):
    """A POST from the shared SEO block, with the marker it always carries."""
    data = {
        "seo_fields": "1",
        "meta_title": "",
        "meta_description": "",
        "og_title": "",
        "og_description": "",
        "canonical_url": "",
        "robots_index": "1",
        "robots_follow": "1",
        "path_override": "",
    }
    data.update(overrides)
    return data


class PageSeoEditingTests(TestCase):
    """"Meta title, meta description, editable URL slug, Open Graph, canonical
    URL override, robots meta control" — the client's checklist, per page."""

    def setUp(self):
        self.admin = User.objects.create_superuser(
            email="seo@goodtip.test", password="Str0ng!pass", display_name="SEO",
        )
        sign_in_to_hq(self.client, self.admin)

    def test_the_index_lists_every_registered_page(self):
        html = self.client.get(reverse("admin:hq_seo")).content.decode()
        self.assertIn("Pricing", html)
        self.assertIn("Tell the boss", html)

    def test_a_meta_title_replaces_the_pages_own_title(self):
        self.client.post(
            reverse("admin:hq_seo_edit", args=["pricing"]),
            _seo_post(meta_title="GoodTip pricing, plans and platform fees"),
        )
        self.client.logout()
        html = self.client.get(reverse("pricing")).content.decode()
        self.assertIn("<title>GoodTip pricing, plans and platform fees</title>", html)

    def test_a_meta_description_replaces_the_pages_own(self):
        self.client.post(
            reverse("admin:hq_seo_edit", args=["pricing"]),
            _seo_post(meta_description="One platform fee. A share goes to your charity."),
        )
        self.client.logout()
        html = self.client.get(reverse("pricing")).content.decode()
        self.assertIn(
            '<meta name="description" content="One platform fee. A share goes to your charity.">',
            html,
        )

    def test_an_empty_box_leaves_the_page_saying_what_it_said(self):
        """Every field is an override, not a value.

        This is what makes it safe to save a row and fill in one box: an SEO
        team should not have to complete a form of seven empties before a page
        has a title at all.
        """
        before = self.client.get(reverse("pricing")).content.decode()
        self.client.post(reverse("admin:hq_seo_edit", args=["pricing"]), _seo_post())
        after = self.client.get(reverse("pricing")).content.decode()
        original_title = before.split("<title>")[1].split("</title>")[0]
        self.assertIn(f"<title>{original_title}</title>", after)

    def test_noindex_is_emitted_for_a_page_that_should_not_be_indexed(self):
        """The client named this one: "needed for pages like tell the boss"."""
        self.client.post(
            reverse("admin:hq_seo_edit", args=["tell_the_boss"]),
            _seo_post(robots_index=""),
        )
        self.client.logout()
        html = self.client.get(reverse("tell_the_boss")).content.decode()
        self.assertIn('<meta name="robots" content="noindex,follow">', html)

    def test_robots_is_stated_even_when_it_is_the_default(self):
        """Unlike the others this is not an override on something the page
        already says — and "index,follow" being visible in view-source beats
        having to infer it from an absent tag."""
        self.client.post(reverse("admin:hq_seo_edit", args=["pricing"]), _seo_post())
        html = self.client.get(reverse("pricing")).content.decode()
        self.assertIn('<meta name="robots" content="index,follow">', html)

    def test_a_canonical_override_is_emitted(self):
        self.client.post(
            reverse("admin:hq_seo_edit", args=["pricing"]),
            _seo_post(canonical_url="https://goodtip.com.au/plans/"),
        )
        html = self.client.get(reverse("pricing")).content.decode()
        self.assertIn('<link rel="canonical" href="https://goodtip.com.au/plans/">', html)

    def test_open_graph_title_and_description_are_emitted(self):
        self.client.post(
            reverse("admin:hq_seo_edit", args=["pricing"]),
            _seo_post(og_title="What GoodTip costs", og_description="One fee, shared."),
        )
        html = self.client.get(reverse("pricing")).content.decode()
        self.assertIn('<meta property="og:title" content="What GoodTip costs">', html)
        self.assertIn('<meta property="og:description" content="One fee, shared.">', html)

    def test_the_share_card_falls_back_to_the_meta_fields(self):
        """So an SEO team fills in two boxes rather than four to get both
        right, and the pair can never silently disagree."""
        self.client.post(
            reverse("admin:hq_seo_edit", args=["pricing"]),
            _seo_post(meta_title="GoodTip pricing", meta_description="One fee."),
        )
        html = self.client.get(reverse("pricing")).content.decode()
        self.assertIn('<meta property="og:title" content="GoodTip pricing">', html)
        self.assertIn('<meta property="og:description" content="One fee.">', html)

    def test_admin_typed_text_is_escaped_into_the_tag(self):
        """A meta description is an HTML attribute, and this one is typed by a
        person who is not thinking about quotes."""
        self.client.post(
            reverse("admin:hq_seo_edit", args=["pricing"]),
            _seo_post(meta_description='He said "no" & left'),
        )
        html = self.client.get(reverse("pricing")).content.decode()
        self.assertIn("&quot;no&quot;", html)
        self.assertNotIn('content="He said "no"', html)

    def test_a_page_with_no_row_is_not_touched_at_all(self):
        """The cost of this feature on an untouched page has to be nothing."""
        self.assertEqual(PageSeo.objects.count(), 0)
        resp = self.client.get(reverse("pricing"))
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(b'name="robots"', resp.content)


class MovingAPageTests(TestCase):
    """"Editable URL slug (not permanently locked to the page title)."""

    def setUp(self):
        self.admin = User.objects.create_superuser(
            email="mover@goodtip.test", password="Str0ng!pass", display_name="Mover",
        )
        sign_in_to_hq(self.client, self.admin)

    def _move(self, key, to):
        return self.client.post(
            reverse("admin:hq_seo_edit", args=[key]), _seo_post(path_override=to),
        )

    def test_the_page_is_served_at_its_new_address(self):
        self._move("pricing", "/what-it-costs/")
        self.client.logout()
        resp = self.client.get("/what-it-costs/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"GoodTip", resp.content)

    def test_the_built_in_address_redirects_to_the_new_one(self):
        """A renamed page must have ONE address, or the two compete for the
        same ranking — which is the thing a rename is meant to fix."""
        self._move("pricing", "/what-it-costs/")
        self.client.logout()
        resp = self.client.get(reverse("pricing"))
        self.assertEqual(resp.status_code, 301)
        self.assertEqual(resp["Location"], "/what-it-costs/")

    def test_the_wording_editor_still_works_at_the_new_address(self):
        """The middleware keys off resolver_match.view_name, and a request to
        the override resolves to the override view — so without re-pointing it,
        a renamed page silently loses every edit made to it."""
        self._move("pricing", "/what-it-costs/")
        self.client.post(
            reverse("admin:hq_seo_edit", args=["pricing"]),
            _seo_post(path_override="/what-it-costs/", meta_title="Costs"),
        )
        html = self.client.get("/what-it-costs/").content.decode()
        self.assertIn("<title>Costs</title>", html)

    def test_clearing_the_box_puts_the_page_back(self):
        self._move("pricing", "/what-it-costs/")
        self._move("pricing", "")
        self.client.logout()
        self.assertEqual(self.client.get(reverse("pricing")).status_code, 200)

    def test_typing_the_built_in_address_back_in_is_not_a_move(self):
        """Storing it as an override would leave the middleware redirecting the
        page to itself for ever."""
        self._move("pricing", reverse("pricing"))
        row = PageSeo.objects.get(page="pricing")
        self.assertEqual(row.path_override, "")
        self.assertEqual(self.client.get(reverse("pricing")).status_code, 200)

    def test_an_address_with_no_override_behind_it_still_404s(self):
        self.client.logout()
        self.assertEqual(self.client.get("/no-such-page/").status_code, 404)

    def test_a_missing_trailing_slash_still_redirects_to_the_real_page(self):
        """The catch-all's own trailing slash is what keeps this working.

        APPEND_SLASH only redirects an address that does not resolve. A bare
        `<path:...>` resolves everything, so a catch-all without the slash in
        its pattern silently turns every link on the web that omits the
        trailing slash into a 404 — which is the largest thing that could go
        wrong here and the least visible.
        """
        self.client.logout()
        resp = self.client.get("/pricing")
        self.assertEqual(resp.status_code, 301)
        self.assertEqual(resp["Location"], "/pricing/")

    def test_an_override_cannot_shadow_a_real_page(self):
        """The catch-all is mounted last, so a route that exists always wins.

        Pointed at an address that IS a page, the override is simply never
        reached — which is the safe direction, and the reason a typo in this
        box cannot take a live page off the site.
        """
        self.client.post(
            reverse("admin:hq_seo_edit", args=["about"]),
            _seo_post(path_override=reverse("pricing"), meta_title="ABOUT MARKER"),
        )
        self.client.logout()
        html = self.client.get(reverse("pricing")).content.decode()
        self.assertNotIn("ABOUT MARKER", html)


class RedirectManagerTests(TestCase):
    """"Redirect manager, so old URLs can be pointed to new ones when slugs
    change, rather than resulting in a broken link"."""

    def setUp(self):
        self.admin = User.objects.create_superuser(
            email="rd@goodtip.test", password="Str0ng!pass", display_name="RD",
        )
        sign_in_to_hq(self.client, self.admin)

    def _add(self, old, new, **extra):
        data = {"old_path": old, "new_path": new, "is_permanent": "1"}
        data.update(extra)
        return self.client.post(reverse("admin:hq_redirect_save"), data)

    def test_a_dead_address_is_redirected(self):
        self._add("/old-pricing/", "/pricing/")
        self.client.logout()
        resp = self.client.get("/old-pricing/")
        self.assertEqual(resp.status_code, 301)
        self.assertEqual(resp["Location"], "/pricing/")

    def test_a_temporary_redirect_is_a_302(self):
        self._add("/trial/", "/pricing/", is_permanent="")
        self.client.logout()
        self.assertEqual(self.client.get("/trial/").status_code, 302)

    def test_a_live_page_always_wins_over_a_redirect(self):
        """Consulted only on a 404, so a row can never shadow a real page —
        which is what makes the table safe to type into."""
        self._add(reverse("pricing"), "/about/")
        self.client.logout()
        self.assertEqual(self.client.get(reverse("pricing")).status_code, 200)

    def test_both_spellings_of_the_trailing_slash_are_matched(self):
        """A link in the wild is as likely to be missing the slash as to have
        it, and nobody typing one form should have to know which."""
        self._add("/old-pricing/", "/pricing/")
        self.client.logout()
        self.assertEqual(self.client.get("/old-pricing").status_code, 301)

    def test_a_pasted_full_url_is_stored_as_a_path(self):
        self._add("https://goodtip.com.au/old-thing/", "/pricing/")
        self.assertTrue(Redirect.objects.filter(old_path="/old-thing/").exists())

    def test_a_redirect_to_itself_is_refused(self):
        """The one mistake here a browser will not let an admin undo: a 301
        loop is cached hard at both ends."""
        self._add("/loop/", "/loop/")
        self.assertFalse(Redirect.objects.filter(old_path="/loop/").exists())

    def test_following_one_counts_a_hit(self):
        """So a redirect can be retired on evidence rather than on a guess."""
        self._add("/old-pricing/", "/pricing/")
        self.client.logout()
        self.client.get("/old-pricing/")
        row = Redirect.objects.get(old_path="/old-pricing/")
        self.assertEqual(row.hits, 1)
        self.assertIsNotNone(row.last_hit_at)

    def test_one_can_be_deleted(self):
        self._add("/gone/", "/pricing/")
        row = Redirect.objects.get(old_path="/gone/")
        self.client.post(reverse("admin:hq_redirect_delete", args=[row.id]))
        self.assertFalse(Redirect.objects.filter(pk=row.pk).exists())


class SitemapTests(TestCase):
    """"XML sitemap, auto generated and kept current as pages are added"."""

    def setUp(self):
        self.admin = User.objects.create_superuser(
            email="sm@goodtip.test", password="Str0ng!pass", display_name="SM",
        )

    def test_it_lists_the_public_pages(self):
        xml = self.client.get("/sitemap.xml").content.decode()
        self.assertIn("/pricing/", xml)
        self.assertIn("/how-it-works/", xml)

    def test_it_does_not_advertise_the_pages_behind_a_login(self):
        """A sitemap is an invitation to crawl; listing the dashboard would be
        advertising a wall."""
        xml = self.client.get("/sitemap.xml").content.decode()
        self.assertNotIn("<loc>http://testserver/dashboard/</loc>", xml)

    def test_a_published_story_is_in_it(self):
        NewsPost.objects.create(title="Finals race", slug="finals-race", is_published=True)
        xml = self.client.get("/sitemap.xml").content.decode()
        self.assertIn("/news/finals-race/", xml)

    def test_a_scheduled_story_is_not(self):
        NewsPost.objects.create(
            title="Queued", slug="queued", is_published=True,
            published_at=timezone.now() + timedelta(days=2),
        )
        xml = self.client.get("/sitemap.xml").content.decode()
        self.assertNotIn("/news/queued/", xml)

    def test_a_noindex_page_is_left_out(self):
        """Asking Google to crawl a page while telling it not to index that
        page is the same request twice, in opposite directions."""
        PageSeo.objects.create(page="tell_the_boss", robots_index=False)
        xml = self.client.get("/sitemap.xml").content.decode()
        self.assertNotIn("/tell-the-boss/", xml)

    def test_a_page_with_a_canonical_elsewhere_is_left_out(self):
        PageSeo.objects.create(page="pricing", canonical_url="https://goodtip.com.au/plans/")
        xml = self.client.get("/sitemap.xml").content.decode()
        self.assertNotIn("<loc>http://testserver/pricing/</loc>", xml)

    def test_a_moved_page_is_listed_at_its_new_address(self):
        PageSeo.objects.create(page="pricing", path_override="/what-it-costs/")
        xml = self.client.get("/sitemap.xml").content.decode()
        self.assertIn("/what-it-costs/", xml)
        self.assertNotIn("<loc>http://testserver/pricing/</loc>", xml)

    @override_settings(ALLOWED_HOSTS=["staging.goodtip.com.au", "testserver"])
    def test_it_uses_the_host_it_was_asked_on(self):
        """No SITE_ID — the one setting that could silently make staging write
        production's URLs into a file crawlers read.

        `django.contrib.sites` is deliberately not installed, so sitemaps falls
        back to RequestSite and takes the domain off the request. Staging
        therefore writes staging URLs and production writes production ones,
        with nothing in either .env to get wrong.
        """
        xml = self.client.get(
            "/sitemap.xml", HTTP_HOST="staging.goodtip.com.au",
        ).content.decode()
        self.assertIn("staging.goodtip.com.au", xml)
        self.assertNotIn("testserver", xml)


class StorySeoTests(TestCase):
    """A story's own SEO fields, edited on the story rather than on a list of
    its own — its meta title falls back to the headline and its description to
    the teaser, so editing them anywhere else means guessing what you override.
    """

    def setUp(self):
        self.admin = User.objects.create_superuser(
            email="ss@goodtip.test", password="Str0ng!pass", display_name="SS",
        )
        sign_in_to_hq(self.client, self.admin)

    def _write(self, **overrides):
        data = _seo_post(**{
            "title_html": "Finals race tightens",
            "excerpt_html": "Two games separate fourth from ninth.",
            "body": "<p>Body.</p>",
            "tags": ["AFL"],
            "is_published": "on",
        })
        data.update(overrides)
        self.client.post(reverse("admin:hq_news_new"), data)
        return NewsPost.objects.get()

    def test_the_editor_offers_the_meta_fields(self):
        """"Blog admin already looks great, just needs the meta title and
        description fields added." — the client."""
        html = self.client.get(reverse("admin:hq_news_new")).content.decode()
        self.assertIn('name="meta_title"', html)
        self.assertIn('name="meta_description"', html)

    def test_a_meta_title_becomes_the_page_title(self):
        post = self._write(meta_title="AFL finals race: who is still alive")
        self.client.logout()
        html = self.client.get(post.get_absolute_url()).content.decode()
        self.assertIn("AFL finals race: who is still alive", html.split("</title>")[0])

    def test_the_headline_is_used_when_no_meta_title_is_given(self):
        post = self._write()
        self.client.logout()
        html = self.client.get(post.get_absolute_url()).content.decode()
        self.assertIn("Finals race tightens", html.split("</title>")[0])

    def test_a_story_can_be_kept_out_of_google(self):
        post = self._write(robots_index="")
        self.client.logout()
        html = self.client.get(post.get_absolute_url()).content.decode()
        self.assertIn('<meta name="robots" content="noindex,follow">', html)

    def test_a_story_carries_a_canonical(self):
        post = self._write()
        self.client.logout()
        html = self.client.get(post.get_absolute_url()).content.decode()
        self.assertIn('rel="canonical"', html)

    def test_the_share_card_falls_back_through_og_then_meta_then_headline(self):
        post = self._write(meta_title="Meta version")
        self.client.logout()
        html = self.client.get(post.get_absolute_url()).content.decode()
        self.assertIn('<meta property="og:title" content="Meta version">', html)

    def test_image_alt_text_reaches_the_share_card(self):
        """"Image alt text, per image, editable without a developer"."""
        post = self._write(image_alt="Crowd at the MCG holding club flags")
        post.image = "news/hero.jpg"
        post.save(update_fields=["image"])
        self.client.logout()
        html = self.client.get(post.get_absolute_url()).content.decode()
        self.assertIn('og:image:alt" content="Crowd at the MCG holding club flags"', html)

    def test_the_share_buttons_are_on_the_story(self):
        """"Confirm whether blog posts already have a social sharing
        mechanism." They do, and the Open Graph fields above have something to
        attach to."""
        post = self._write()
        self.client.logout()
        html = self.client.get(post.get_absolute_url()).content.decode()
        self.assertIn("facebook.com/sharer", html)
        self.assertIn("linkedin.com/sharing", html)
        self.assertIn("data-copy-link", html)

    def test_a_save_without_the_seo_block_changes_none_of_it(self):
        """An unticked checkbox is absent from a POST in exactly the way a
        whole missing block is — so without the marker, any other caller that
        saved a story would quietly take it out of Google."""
        post = self._write(meta_title="Kept")
        self.client.post(reverse("admin:hq_news_edit", args=[post.id]), {
            "title_html": "Finals race tightens",
            "excerpt_html": "Teaser.", "body": "<p>Body.</p>",
            "tags": ["AFL"], "is_published": "on",
        })
        post.refresh_from_db()
        self.assertTrue(post.robots_index)
        self.assertEqual(post.meta_title, "Kept")


class PageImageAltTests(TestCase):
    """"Image alt text, per image, editable without a developer."

    Two halves, because there are two situations. Swapping a photograph asks
    for a description as part of the swap (covered by the page-editor JS and
    `page_upload_image`). This covers the other one: the picture on the page is
    the right picture and its description — written into the template by
    whoever put it there — is wrong or missing.
    """

    def setUp(self):
        self.admin = User.objects.create_superuser(
            email="alt@goodtip.test", password="Str0ng!pass", display_name="Alt",
        )
        sign_in_to_hq(self.client, self.admin)

    def _save_alt(self, key, alt):
        return self.client.post(reverse("admin:hq_page_image_alt"), {
            "page": "pricing", "key": key, "alt": alt, "original": "",
        })

    def _a_picture_key(self):
        """A real image block key off the live pricing page."""
        from .pagetext import find_blocks

        html = self.client.get(reverse("pricing")).content.decode()
        _, _, blocks = find_blocks(html)
        return next((k for _, _, k, kind in blocks if kind == "image"), None)

    def test_alt_can_be_set_without_replacing_the_picture(self):
        key = self._a_picture_key()
        if key is None:
            self.skipTest("the pricing page has no <img> to describe")
        self._save_alt(key, "Two colleagues comparing tips at a desk")
        html = self.client.get(reverse("pricing")).content.decode()
        self.assertIn('alt="Two colleagues comparing tips at a desk"', html)

    def test_clearing_it_puts_the_templates_own_description_back(self):
        """An alt-only row with an empty description is not an override, it is
        the absence of one — leaving it in place would pin the picture to
        alt="" for ever."""
        from .models import PageEdit

        key = self._a_picture_key()
        if key is None:
            self.skipTest("the pricing page has no <img> to describe")
        self._save_alt(key, "Something")
        self._save_alt(key, "")
        self.assertFalse(
            PageEdit.objects.filter(page="pricing", block_key=key).exists()
        )
