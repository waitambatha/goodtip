"""Tests for the News & blog story editor.

These cover the round trip that has no other safety net: what the
contenteditable surfaces post, what gets stored, and what the published
article then shows. A broken template here is a runtime error, not an import
error, so `manage.py check` never sees it.
"""
import re
from io import BytesIO, StringIO
import tempfile
import shutil
from datetime import timedelta

from django.core.files.storage import default_storage
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from sysadmin.models import AdminAccess

from goodtip.testing import drop_temp_media, temp_media

from .models import NewsPost

User = get_user_model()


def sign_in_to_hq(client, user):
    """Log in and clear the admin's second factor.

    News, Enquiries and the public-page editor moved under /admin/ when the
    organisation admin and the super admin were split apart, and /admin/ is
    behind an emailed six-digit code (sysadmin.middleware). Stamping the
    session is what the OTP flow does on success.
    """
    from sysadmin import otp

    client.force_login(user)
    session = client.session
    otp.mark_verified(session)
    session.save()



class TemplateCommentTests(TestCase):
    """No `{# ... #}` comment may span a newline, anywhere in the project.

    Django's tag regex is not compiled with DOTALL, so `{#` and `#}` only pair
    up on one line. Spread a comment over two and it is never tokenised as a
    comment at all — it is emitted as literal page text.

    That is a nasty failure because it reads as correct in the editor. It has
    shipped three times now: once pushing the nav off the top of the page, once
    as visible prose mid-layout, and once inside a flex row where the leaked
    text became an anonymous flex item and crushed the real label to one
    character per line. A regex is cheaper than finding it in a screenshot.
    """

    # A {# with a newline before its matching #}.
    MULTILINE_COMMENT = re.compile(r"\{#(?:(?!#\}).)*\n(?:(?!#\}).)*#\}", re.S)

    def test_no_template_comment_spans_a_newline(self):
        # Every template the project can load, not just templates/ — app
        # directories are on the loader path too (APP_DIRS is on), and the
        # first template written under one of them leaked a comment onto an
        # admin page precisely because this test was only looking in one place.
        root = Path(settings.BASE_DIR)
        roots = [root / "templates"] + sorted(
            d for d in root.glob("*/templates") if "venv" not in d.parts
        )
        offenders = []
        for base in roots:
            for path in sorted(base.rglob("*.html")):
                body = path.read_text()
                for match in self.MULTILINE_COMMENT.finditer(body):
                    line = body[: match.start()].count("\n") + 1
                    offenders.append(f"{path.relative_to(root)}:{line}")
        self.assertEqual(
            offenders, [],
            "Multi-line {# #} renders as page text — use {% comment %} instead:\n  "
            + "\n  ".join(offenders),
        )


class NewsEditorTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(
            email="editor@goodtip.test", password="Str0ng!pass", display_name="Editor"
        )
        sign_in_to_hq(self.client, self.admin)

    def _post(self, **overrides):
        data = {
            "title_html": "<b>Finals race tightens</b>",
            "excerpt_html": "<b>Two</b> games separate fourth from ninth.",
            "body": "<p>Three rounds to play.</p>",
            # A list, because tags are multi-select now — the test client sends
            # one `tags` value per entry, which is what the browser does.
            "tags": ["AFL"],
            "is_published": "on",
        }
        data.update(overrides)
        return data

    # ---- the teaser is rich text now, with a plain-text mirror --------------

    def test_teaser_keeps_its_formatting_and_mirrors_plain_text(self):
        self.client.post(reverse("admin:hq_news_new"), self._post())
        post = NewsPost.objects.get()
        self.assertEqual(post.excerpt_html, "<b>Two</b> games separate fourth from ninth.")
        # The plain mirror feeds the OG description, the announcement email and
        # the truncated cards, none of which can carry markup.
        self.assertEqual(post.excerpt, "Two games separate fourth from ninth.")

    def test_entities_are_unescaped_into_the_plain_text_mirror(self):
        """A headline typed as "Tips & tricks" must not be stored as "Tips &amp; tricks".

        The stored string is printed through autoescape in the page <title>,
        the email subject and the OG tags, so a literal entity there comes out
        as "Tips &amp;amp; tricks" on the page.
        """
        self.client.post(reverse("admin:hq_news_new"), self._post(
            title_html="Tips &amp; tricks",
            excerpt_html="Fourth &amp; ninth",
        ))
        post = NewsPost.objects.get()
        self.assertEqual(post.title, "Tips & tricks")
        self.assertEqual(post.excerpt, "Fourth & ninth")

    def test_article_shows_the_formatted_teaser(self):
        self.client.post(reverse("admin:hq_news_new"), self._post())
        post = NewsPost.objects.get()
        html = self.client.get(post.get_absolute_url()).content.decode()
        self.assertIn("<b>Two</b> games separate fourth from ninth.", html)

    # ---- posts written before the teaser had formatting ---------------------

    def test_a_teaser_written_before_this_change_still_shows(self):
        """No backfill migration needed — both templates fall back to `excerpt`.

        Old posts have `excerpt` set and `excerpt_html` empty. There is no
        formatting to recover for them, so a data migration would only be
        rewriting plain text as plain text.
        """
        post = NewsPost.objects.create(
            title="Old story", excerpt="A teaser from before the editor existed.",
            is_published=True,
        )
        self.assertEqual(post.excerpt_html, "")
        html = self.client.get(post.get_absolute_url()).content.decode()
        self.assertIn("A teaser from before the editor existed.", html)

    def test_the_editor_loads_an_old_teaser_into_the_surface(self):
        post = NewsPost.objects.create(
            title="Old story", excerpt="A teaser from before the editor existed.",
            is_published=True,
        )
        html = self.client.get(reverse("admin:hq_news_edit", args=[post.id])).content.decode()
        surface = html.split('data-hidden-input="np_excerpt_html"')[1]
        self.assertIn("A teaser from before the editor existed.", surface.split("</div>")[0])

    def test_an_old_teaser_is_escaped_not_trusted_as_markup(self):
        """`excerpt` is plain text, so it must go through autoescape."""
        post = NewsPost.objects.create(
            title="Old story", excerpt="Fourth <b>&</b> ninth", is_published=True,
        )
        html = self.client.get(post.get_absolute_url()).content.decode()
        self.assertIn("Fourth &lt;b&gt;&amp;&lt;/b&gt; ninth", html)

    def test_saving_an_old_post_moves_its_teaser_forward(self):
        post = NewsPost.objects.create(
            title="Old story", excerpt="A teaser from before.", is_published=True,
        )
        self.client.post(
            reverse("admin:hq_news_edit", args=[post.id]),
            self._post(excerpt_html="A teaser from before."),
        )
        post.refresh_from_db()
        self.assertEqual(post.excerpt_html, "A teaser from before.")
        self.assertEqual(post.excerpt, "A teaser from before.")

    # ---- the URL is generated, unique and stable ----------------------------

    def test_slug_is_generated_from_the_headline(self):
        self.client.post(reverse("admin:hq_news_new"), self._post())
        self.assertEqual(NewsPost.objects.get().slug, "finals-race-tightens")

    def test_a_second_story_with_the_same_headline_gets_its_own_url(self):
        self.client.post(reverse("admin:hq_news_new"), self._post())
        self.client.post(reverse("admin:hq_news_new"), self._post())
        slugs = set(NewsPost.objects.values_list("slug", flat=True))
        self.assertEqual(slugs, {"finals-race-tightens", "finals-race-tightens-2"})

    def test_editing_the_headline_does_not_move_the_story(self):
        """Links already shared have to keep working."""
        self.client.post(reverse("admin:hq_news_new"), self._post())
        post = NewsPost.objects.get()
        self.client.post(
            reverse("admin:hq_news_edit", args=[post.id]),
            self._post(title_html="A completely different headline"),
        )
        post.refresh_from_db()
        self.assertEqual(post.title, "A completely different headline")
        self.assertEqual(post.slug, "finals-race-tightens")

    def test_the_post_list_offers_the_link_to_copy(self):
        self.client.post(reverse("admin:hq_news_new"), self._post())
        html = self.client.get(reverse("admin:hq_news")).content.decode()
        self.assertIn("/news/finals-race-tightens/", html)
        self.assertIn("data-copy-link", html)

    def test_the_edit_page_shows_the_url_with_copy_and_view(self):
        """The address is editable now, so it is an input rather than a label.

        It used to be frozen and rendered as text with `data-slug-fixed`. The
        client asked to be able to change it; the old address is kept working
        by a redirect instead of by refusing the edit — see
        RenamingAStoryTests.
        """
        self.client.post(reverse("admin:hq_news_new"), self._post())
        post = NewsPost.objects.get()
        html = self.client.get(reverse("admin:hq_news_edit", args=[post.id])).content.decode()
        self.assertIn('name="slug"', html)
        self.assertIn('value="finals-race-tightens"', html)
        self.assertIn("http://testserver/news/finals-race-tightens/", html)

    def test_a_new_post_previews_the_url_it_will_get(self):
        html = self.client.get(reverse("admin:hq_news_new")).content.decode()
        self.assertIn("Will publish at", html)
        self.assertNotIn("data-slug-fixed", html)

    def test_an_unpublished_post_offers_no_link_because_it_would_404(self):
        self.client.post(reverse("admin:hq_news_new"), self._post(is_published=""))
        html = self.client.get(reverse("admin:hq_news")).content.decode()
        self.assertNotIn("data-copy-link", html)

    # ---- "link to full story" is gone, sources stay -------------------------

    def test_the_editor_no_longer_offers_a_link_to_a_story_elsewhere(self):
        html = self.client.get(reverse("admin:hq_news_new")).content.decode()
        self.assertNotIn('name="link_url"', html)
        self.assertIn('name="source_url"', html)

    def test_a_posted_link_url_is_ignored(self):
        self.client.post(reverse("admin:hq_news_new"), self._post(link_url="https://elsewhere.test/"))
        self.assertEqual(NewsPost.objects.get().link_url, "")

    def test_the_article_sends_nobody_off_to_an_original_story(self):
        self.client.post(reverse("admin:hq_news_new"), self._post())
        post = NewsPost.objects.get()
        html = self.client.get(post.get_absolute_url()).content.decode()
        self.assertNotIn("Read the original story", html)

    def test_sources_still_save(self):
        self.client.post(reverse("admin:hq_news_new"), self._post(
            source_label="AFL.com.au", source_url="https://afl.com.au/news",
        ))
        self.assertEqual(
            NewsPost.objects.get().sources,
            [{"label": "AFL.com.au", "url": "https://afl.com.au/news"}],
        )

    # ---- toolbar controls ---------------------------------------------------

    def test_font_size_is_a_number_not_a_named_bucket(self):
        html = self.client.get(reverse("admin:hq_news_new")).content.decode()
        self.assertIn('data-cmd="fontSizePx"', html)
        self.assertIn('data-size-step="-1"', html)
        self.assertNotIn("fontSizeCustom", html)

    def test_every_writing_surface_has_a_toolbar(self):
        """The teaser used to be the one plain textarea left on the page.

        The check is that none of the three WRITING surfaces is a textarea, not
        that the page contains no textarea at all: the SEO block below them has
        two, for the meta and share-card descriptions, and those are plain text
        by definition — a meta description with bold in it is a meta
        description with `<b>` in the search result.
        """
        html = self.client.get(reverse("admin:hq_news_new")).content.decode()
        for surface in ("headline", "teaser", "body"):
            self.assertIn(f'data-editor="{surface}"', html)
        writing_surfaces, _, seo_block = html.partition("seo-block")
        self.assertTrue(seo_block, "the SEO block should be on the editor")
        self.assertNotIn("<textarea", writing_surfaces)

    # ---- featured image -----------------------------------------------------

    def test_no_comment_text_leaks_into_the_drop_zone(self):
        """The drop zone is a flex row, so leaked text is not just ugly.

        A stray text node between the flex children becomes an anonymous flex
        item, which stole the width from the real label and wrapped it one
        character per line.
        """
        html = self.client.get(reverse("admin:hq_news_new")).content.decode()
        self.assertNotIn("{#", html)
        self.assertNotIn("bare file input cannot be styled", html)

    def test_no_comment_text_leaks_onto_the_published_article(self):
        self.client.post(reverse("admin:hq_news_new"), self._post())
        post = NewsPost.objects.get()
        html = self.client.get(post.get_absolute_url()).content.decode()
        self.assertNotIn("{#", html)
        self.assertNotIn("Share bar + optional sources", html)
        self.assertNotIn("Open Graph / Twitter card tags", html)
        # …while the tags those comments describe are still emitted.
        self.assertIn('property="og:title"', html)
        # The share panel's copy button, by its behaviour hook rather than a
        # class name — the panel was redesigned in Sep 2026 and renamed them.
        self.assertIn("data-copy-link", html)


    def test_the_featured_image_can_be_taken_back_off_a_post(self):
        self.client.post(reverse("admin:hq_news_new"), self._post())
        post = NewsPost.objects.get()
        post.image = "news/hero.jpg"
        post.save(update_fields=["image"])

        self.client.post(
            reverse("admin:hq_news_edit", args=[post.id]),
            self._post(image_clear="1"),
        )
        post.refresh_from_db()
        self.assertFalse(post.image)

    def test_saving_without_touching_the_image_keeps_it(self):
        self.client.post(reverse("admin:hq_news_new"), self._post())
        post = NewsPost.objects.get()
        post.image = "news/hero.jpg"
        post.save(update_fields=["image"])

        self.client.post(reverse("admin:hq_news_edit", args=[post.id]), self._post())
        post.refresh_from_db()
        self.assertEqual(post.image.name, "news/hero.jpg")


# ---------------------------------------------------------------------------
# Pages — editing the words on the site from the page itself.
# ---------------------------------------------------------------------------
from .middleware import EDIT_PARAM  # noqa: E402
from .models import PageEdit  # noqa: E402
from .pagetext import block_key, find_blocks, rewrite  # noqa: E402


def sign_in_to_admin(client, user):
    """Log in and clear the admin's second factor.

    /admin/ sits behind an emailed one-time code (sysadmin.middleware), so
    force_login on its own lands on the verify screen rather than the page
    under test. Stamping the session is what the OTP flow does on success.
    Same helper as sysadmin/test_control_plane.py — the Pages screens moved
    into the control plane and inherited its front door.
    """
    from sysadmin import otp

    client.force_login(user)
    session = client.session
    otp.mark_verified(session)
    session.save()


class PageTextTests(TestCase):
    """The rewriter, on its own. Everything below depends on these holding."""

    SAMPLE = (
        '<div class="wrap"><h1>Give &amp; win</h1>'
        "<p>Some <b>bold</b> copy.</p>"
        "<ul><li>One</li><li>Two</li></ul>"
        '<img src="/static/a.png" alt="A">'
        '<script>var s = "<p>not a block</p>";</script>'
        "<span>Chip</span></div>"
    )

    def test_a_page_with_no_edits_is_returned_byte_for_byte(self):
        """The property that makes this safe to put in front of every page.

        Any difference at all — a re-encoded entity, a dropped attribute
        quote, a normalised tag — would be a rendering change on the landing
        page caused by a feature nobody is even using yet.
        """
        out, applied = rewrite(self.SAMPLE, {}, edit_mode=False)
        self.assertEqual(out, self.SAMPLE)
        self.assertEqual(applied, set())

    def test_blocks_are_the_smallest_thing_holding_words(self):
        _, _, blocks = find_blocks(self.SAMPLE)
        tags = [key.split("-")[0] for _, _, key, _ in blocks]
        # The <ul> and the outer <div> are not blocks; the two <li> are.
        self.assertEqual(tags, ["h1", "p", "li", "li", "img", "span"])

    def test_script_bodies_are_never_editable(self):
        _, _, blocks = find_blocks(self.SAMPLE)
        keys = [k for _, _, k, _ in blocks]
        self.assertNotIn(block_key("p", "not a block"), keys)

    def test_an_edit_replaces_only_that_block(self):
        _, _, blocks = find_blocks(self.SAMPLE)
        key = next(k for _, _, k, kind in blocks if k.startswith("h1") and kind == "text")
        out, applied = rewrite(self.SAMPLE, {key: ("text", "Give &amp; thrive")})
        self.assertIn("<h1>Give &amp; thrive</h1>", out)
        self.assertIn("<p>Some <b>bold</b> copy.</p>", out)
        self.assertEqual(applied, {key})

    def test_an_image_edit_swaps_the_src_and_the_alt_with_it(self):
        """Alt is the one attribute that must NOT survive a swap.

        Everything else on the tag describes the slot — how big it is, when to
        load it — and is still true after the picture changes. `alt` describes
        the picture, so keeping it meant every replaced photograph on the site
        was announced to a screen reader as the one it replaced: a description
        that is wrong and sounds right.
        """
        _, _, blocks = find_blocks(self.SAMPLE)
        key = next(k for _, _, k, kind in blocks if kind == "image")
        out, _ = rewrite(self.SAMPLE, {key: ("image", "/media/new.png", "A crowd at dusk")})
        self.assertIn('src="/media/new.png"', out)
        self.assertIn('alt="A crowd at dusk"', out)
        self.assertNotIn('alt="A"', out)

    def test_an_image_edit_with_no_alt_given_empties_the_old_one(self):
        """Empty alt is correct markup for a decorative picture, and honest.

        The alternative — leaving the previous picture's description in place
        because nobody typed a new one — is the exact failure above.
        """
        _, _, blocks = find_blocks(self.SAMPLE)
        key = next(k for _, _, k, kind in blocks if kind == "image")
        out, _ = rewrite(self.SAMPLE, {key: ("image", "/media/new.png", "")})
        self.assertIn('alt=""', out)
        self.assertNotIn('alt="A"', out)

    def test_the_key_follows_the_wording_not_the_position(self):
        """An edit has to survive a paragraph being added above it.

        Keying on position is the obvious implementation and the wrong one:
        insert something earlier in the template and every later edit silently
        lands on a different sentence.
        """
        before = "<div><p>First.</p><p>Second.</p></div>"
        after = "<div><p>New one.</p><p>First.</p><p>Second.</p></div>"
        key = next(k for _, _, k, _ in find_blocks(before)[2] if "Second" not in k)
        keys_before = [k for _, _, k, _ in find_blocks(before)[2]]
        keys_after = [k for _, _, k, _ in find_blocks(after)[2]]
        for k in keys_before:
            self.assertIn(k, keys_after)
        self.assertTrue(key)

    def test_reindenting_a_template_does_not_orphan_an_edit(self):
        self.assertEqual(
            block_key("p", "Hello   there"),
            block_key("p", "Hello\n    there"),
        )

    def test_two_blocks_with_the_same_words_get_different_keys(self):
        _, _, blocks = find_blocks("<div><p>Same</p><p>Same</p></div>")
        keys = [k for _, _, k, _ in blocks]
        self.assertEqual(len(set(keys)), 2)

    def test_unclosed_tags_do_not_take_the_page_down(self):
        out, _ = rewrite("<div><p>Open forever<div>More</div>", {}, edit_mode=True)
        self.assertIn("More", out)


class PageEditFlowTests(TestCase):
    """The whole loop: open a page in edit mode, save a change, read it back."""

    def setUp(self):
        self.admin = User.objects.create_superuser(
            email="boss@example.com", password="pw", display_name="Boss",
        )
        self.member = User.objects.create_user(
            email="member@example.com", password="pw", display_name="Mem",
        )

    def _a_block_on(self, url):
        """Open a page in edit mode and pick a block off it."""
        sign_in_to_admin(self.client, self.admin)
        html = self.client.get(url, {EDIT_PARAM: "1"}).content.decode()
        keys = re.findall(r'data-gte="([^"]+)"', html)
        self.assertTrue(keys, "the page offered nothing to edit")
        return keys[0]

    def test_edit_mode_tags_the_blocks_and_loads_the_editor(self):
        sign_in_to_admin(self.client, self.admin)
        res = self.client.get(reverse("how_it_works"), {EDIT_PARAM: "1"})
        html = res.content.decode()
        self.assertIn("data-gte=", html)
        self.assertIn("gte-bar", html)
        self.assertIn("gt-page-editor.js", html)

    def test_the_nav_and_the_loader_are_not_editable(self):
        """Site furniture is skipped, and this is worth pinning down.

        Before it was, the first thirteen editable blocks on every page were
        nav fragments — the brand mark is three spans, one of which is a full
        stop — and the loading screen's copy of the same. You reached the
        headline fourteenth.
        """
        sign_in_to_admin(self.client, self.admin)
        html = self.client.get(reverse("how_it_works"), {EDIT_PARAM: "1"}).content.decode()
        # The first tagged block should be page content, not chrome.
        first = re.search(r'data-gte="[^"]+"[^>]*>([^<]{3,})', html)
        self.assertIsNotNone(first)
        self.assertNotIn("GOOD", first.group(1))
        # And the nav's own links never became blocks.
        nav = html[html.find("<nav"):html.find("</nav>")]
        self.assertNotIn("data-gte=", nav)

    def test_a_plain_visit_is_left_completely_alone(self):
        """No tags, no editor, and no marker of any kind for a reader."""
        html = self.client.get(reverse("how_it_works")).content.decode()
        self.assertNotIn("data-gte", html)
        self.assertNotIn("gte-bar", html)

    def test_a_member_cannot_turn_edit_mode_on(self):
        sign_in_to_admin(self.client, self.member)
        html = self.client.get(reverse("how_it_works"), {EDIT_PARAM: "1"}).content.decode()
        self.assertNotIn("data-gte=", html)
        self.assertNotIn("gte-bar", html)

    def test_an_anonymous_visitor_cannot_turn_edit_mode_on(self):
        html = self.client.get(reverse("how_it_works"), {EDIT_PARAM: "1"}).content.decode()
        self.assertNotIn("gte-bar", html)

    def test_saving_changes_what_every_visitor_then_reads(self):
        key = self._a_block_on(reverse("how_it_works"))
        res = self.client.post(
            reverse("admin:hq_page_save"),
            data={"page": "how_it_works", "blocks": [
                {"key": key, "html": "Tipping, but for good.", "original": "whatever"},
            ]},
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["saved"], 1)

        self.client.logout()
        html = self.client.get(reverse("how_it_works")).content.decode()
        self.assertIn("Tipping, but for good.", html)

    def test_a_saved_edit_is_marked_live_once_it_has_been_shown(self):
        key = self._a_block_on(reverse("how_it_works"))
        self.client.post(
            reverse("admin:hq_page_save"),
            data={"page": "how_it_works", "blocks": [{"key": key, "html": "Hi"}]},
            content_type="application/json",
        )
        row = PageEdit.objects.get(block_key=key)
        # Not yet — nothing has rendered since it was saved.
        self.assertFalse(row.is_live)

        self.client.get(reverse("how_it_works"))
        row.refresh_from_db()
        self.assertTrue(row.is_live)

    def test_an_edit_for_wording_that_no_longer_exists_is_never_applied(self):
        """The safety property. A stale edit shows the original, not somebody
        else's paragraph."""
        PageEdit.objects.create(
            page="how_it_works",
            block_key=block_key("p", "wording that was never on this page"),
            html="<script>alert(1)</script>SHOULD NOT APPEAR",
            last_applied_at=None,
        )
        html = self.client.get(reverse("how_it_works")).content.decode()
        self.assertNotIn("SHOULD NOT APPEAR", html)

    def test_saved_html_is_sanitised(self):
        key = self._a_block_on(reverse("how_it_works"))
        self.client.post(
            reverse("admin:hq_page_save"),
            data={"page": "how_it_works", "blocks": [
                {"key": key, "html": '<b onclick="steal()">Hi</b><script>bad()</script>'},
            ]},
            content_type="application/json",
        )
        stored = PageEdit.objects.get(block_key=key).html
        self.assertNotIn("onclick", stored)
        self.assertNotIn("<script", stored)

    def test_a_member_cannot_save_an_edit(self):
        sign_in_to_admin(self.client, self.member)
        res = self.client.post(
            reverse("admin:hq_page_save"),
            data={"page": "home", "blocks": [{"key": "p-abc", "html": "hi"}]},
            content_type="application/json",
        )
        self.assertNotEqual(res.status_code, 200)
        self.assertFalse(PageEdit.objects.exists())

    def test_saving_against_an_unregistered_page_is_refused(self):
        sign_in_to_admin(self.client, self.admin)
        res = self.client.post(
            reverse("admin:hq_page_save"),
            data={"page": "not_a_page", "blocks": [{"key": "p-abc", "html": "hi"}]},
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 400)
        self.assertFalse(PageEdit.objects.exists())

    def test_reverting_a_block_removes_the_edit(self):
        key = self._a_block_on(reverse("how_it_works"))
        self.client.post(
            reverse("admin:hq_page_save"),
            data={"page": "how_it_works", "blocks": [{"key": key, "html": "Hi"}]},
            content_type="application/json",
        )
        self.client.post(
            reverse("admin:hq_page_save"),
            data={"page": "how_it_works", "blocks": [{"key": key, "revert": True}]},
            content_type="application/json",
        )
        self.assertFalse(PageEdit.objects.filter(block_key=key).exists())

    def test_reverting_a_whole_page_puts_every_word_back(self):
        key = self._a_block_on(reverse("how_it_works"))
        self.client.post(
            reverse("admin:hq_page_save"),
            data={"page": "how_it_works", "blocks": [{"key": key, "html": "Changed"}]},
            content_type="application/json",
        )
        self.client.post(reverse("admin:hq_page_revert", args=["how_it_works"]))
        self.assertFalse(PageEdit.objects.filter(page="how_it_works").exists())
        html = self.client.get(reverse("how_it_works")).content.decode()
        self.assertNotIn("Changed", html)

    def test_the_pages_index_lists_public_and_private_separately(self):
        sign_in_to_admin(self.client, self.admin)
        html = self.client.get(reverse("admin:hq_pages")).content.decode()
        self.assertIn("Public pages", html)
        self.assertIn("Private pages", html)
        self.assertIn("How it works", html)
        self.assertIn("Dashboard", html)

    def test_one_page_shows_what_each_edit_replaced(self):
        key = self._a_block_on(reverse("how_it_works"))
        self.client.post(
            reverse("admin:hq_page_save"),
            data={"page": "how_it_works", "blocks": [
                {"key": key, "html": "The new words", "original": "The old words"},
            ]},
            content_type="application/json",
        )
        html = self.client.get(reverse("admin:hq_page_edits", args=["how_it_works"])).content.decode()
        self.assertIn("The old words", html)
        self.assertIn("The new words", html)

    def test_asking_for_a_page_that_does_not_exist_is_a_404(self):
        sign_in_to_admin(self.client, self.admin)
        self.assertEqual(
            self.client.get(reverse("admin:hq_page_edits", args=["nope"])).status_code, 404
        )

    def test_only_a_superuser_reaches_the_pages_index(self):
        sign_in_to_admin(self.client, self.member)
        res = self.client.get(reverse("admin:hq_pages"))
        self.assertNotEqual(res.status_code, 200)

    def test_an_unregistered_url_is_never_parsed(self):
        """The middleware has to be invisible everywhere it is not wanted."""
        sign_in_to_admin(self.client, self.admin)
        res = self.client.get(reverse("admin:hq_pages"), {EDIT_PARAM: "1"})
        self.assertNotIn(b"data-gte=", res.content)


class AdminThemeToggleTests(TestCase):
    """The green/cream switch in /admin/.

    Django ships this as one small icon that CYCLES auto -> light -> dark on
    click. The client could not find it, which is what the first rewrite fixed
    by labelling it — but cycling has the deeper problem that the control means
    something different on every press, so it cannot be aimed: you want dark,
    you press once, you get light.

    Three segments answer both. Every option is visible, the one in use is
    filled, and any of them is one click away.
    """

    def setUp(self):
        self.admin = User.objects.create_superuser(
            email="root@example.com", password="pw", display_name="Root",
        )
        sign_in_to_admin(self.client, self.admin)

    def test_all_three_modes_are_on_screen_at_once(self):
        html = self.client.get(reverse("admin:index")).content.decode()
        for mode in ("auto", "light", "dark"):
            self.assertIn(f'data-set-theme="{mode}"', html)
        for label in ("Auto", "Light", "Dark"):
            self.assertIn(f"<span>{label}</span>", html)

    def test_nothing_cycles_each_button_sets_one_mode(self):
        """The bug in the shipped control: pressing it is a guess."""
        html = self.client.get(reverse("admin:index")).content.decode()
        self.assertNotIn("cycleTheme", html)
        # The attribute with a value is a button; bare occurrences are the
        # script's own selectors.
        self.assertEqual(html.count('data-set-theme="'), 3)

    def test_it_writes_the_same_place_django_reads_from(self):
        """Django's early-boot script restores the theme before first paint by
        reading localStorage["theme"] and stamping data-theme. Write anywhere
        else and the choice is forgotten on the next page."""
        html = self.client.get(reverse("admin:index")).content.decode()
        self.assertIn("localStorage.setItem('theme'", html)
        self.assertIn("document.documentElement.dataset.theme", html)

    def test_the_stylesheet_that_shapes_it_is_loaded(self):
        html = self.client.get(reverse("admin:index")).content.decode()
        self.assertIn("css/gt-admin.css", html)


class EnquiryAcknowledgementTests(TestCase):
    """Somebody who writes in gets told straight away that it arrived.

    Before this an enquiry produced a "sent" flag on the page and then nothing
    at all until a human got round to it — days, on a slow week. Somebody who
    has just written to a company they are considering paying should not have
    to wonder whether the form worked.
    """

    def setUp(self):
        self.staff = User.objects.create_user(
            email="boss@example.com", password="pw", display_name="Boss", is_staff=True,
        )
        self.payload = {
            "name": "Jane Tester", "email": "jane@example.com",
            "organisation": "Acme Pty", "interest": "Setting up a comp",
            "message": "How much for 40 people?", "source": "/",
        }

    def _send(self, **overrides):
        data = {**self.payload, **overrides}
        return self.client.post(reverse("contact_submit"), data)

    def test_the_sender_is_emailed_as_well_as_the_team(self):
        from django.core import mail

        with self.settings(EMAIL_HOST="localhost", EMAIL_ALLOWLIST="*"):
            self._send()
        to = [m.to for m in mail.outbox]
        self.assertIn(["jane@example.com"], to)
        self.assertIn([self.staff.email], to)

    def test_the_acknowledgement_quotes_what_they_wrote(self):
        """An acknowledgement with nothing in it reads like an autoresponder.

        Quoting the message back is what makes it recognisable as a reply to
        the thing they actually sent, and lets them see it arrived intact.
        """
        from django.core import mail

        with self.settings(EMAIL_HOST="localhost", EMAIL_ALLOWLIST="*"):
            self._send()
        ack = next(m for m in mail.outbox if m.to == ["jane@example.com"])
        self.assertIn("How much for 40 people?", ack.body)
        self.assertIn("Jane Tester", ack.body)

    def test_a_reply_to_the_acknowledgement_reaches_a_person(self):
        """It says "just reply to this", so replying has to work.

        The from address is no-reply@; an acknowledgement that invites a reply
        and then bounces is worse than not sending one.
        """
        from django.core import mail

        from accounts.views import CONTACT_REPLY_TO

        with self.settings(EMAIL_HOST="localhost", EMAIL_ALLOWLIST="*"):
            self._send()
        ack = next(m for m in mail.outbox if m.to == ["jane@example.com"])
        self.assertEqual(ack.reply_to, [CONTACT_REPLY_TO])
        self.assertNotIn("no-reply", CONTACT_REPLY_TO)

    def test_a_rejected_enquiry_sends_nothing(self):
        from django.core import mail

        with self.settings(EMAIL_HOST="localhost", EMAIL_ALLOWLIST="*"):
            self._send(message="")
        self.assertEqual(mail.outbox, [])

    def test_the_enquiry_is_still_recorded_when_the_mail_fails(self):
        """Stored first, emailed second, and the order is the point.

        Losing a sales lead to a mail outage is the one failure this path is
        engineered against — so a send that blows up must not take the record
        with it, or cost the sender their "it arrived" page.
        """
        from unittest.mock import patch

        from .models import Enquiry

        with patch("goodtip.mail.send_template", side_effect=RuntimeError("mail is down")):
            response = self._send()
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Enquiry.objects.filter(email="jane@example.com").exists())


class PagesScopeTests(TestCase):
    """Public or private, one list at a time.

    Both tables used to sit on the screen together inside `.gt-shell-wide`, a
    1.55fr / 1fr split meant for a list beside a form. The private table — the
    longer of the two, with the longer addresses — was living in the 1fr
    column against a `min-width: 640px` table, so it collapsed and scrolled
    sideways inside its own panel.
    """

    def setUp(self):
        self.boss = User.objects.create_superuser(
            email="pages-boss@example.com", password="pw", display_name="Boss",
        )
        sign_in_to_admin(self.client, self.boss)

    def _get(self, query=""):
        return self.client.get(reverse("admin:hq_pages") + query)

    def test_public_is_what_you_get_without_asking(self):
        r = self._get()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context["scope"], "public")
        self.assertTrue(all(row["page"].is_public for row in r.context["rows"]))

    def test_asking_for_private_gets_only_private(self):
        r = self._get("?scope=private")
        self.assertEqual(r.context["scope"], "private")
        self.assertTrue(r.context["rows"])
        self.assertFalse(any(row["page"].is_public for row in r.context["rows"]))

    def test_a_scope_nobody_offers_falls_back_to_public(self):
        """The value picks a list; it is not passed to anything else.

        Still clamped rather than trusted, because a querystring that reaches
        a lookup unchecked is the shape of a bug even when this one is benign.
        """
        self.assertEqual(self._get("?scope=../secret").context["scope"], "public")
        self.assertEqual(self._get("?scope=").context["scope"], "public")

    def test_both_counts_are_offered_whichever_side_is_showing(self):
        """The tab that is not open still has to say how much is behind it."""
        r = self._get("?scope=private")
        self.assertGreater(r.context["public_count"], 0)
        self.assertGreater(r.context["private_count"], 0)

    def test_the_page_no_longer_uses_the_two_column_shell(self):
        """The split was the whole cause of the squeeze, so it is asserted.

        A future edit putting `gt-shell-wide` back would bring the sideways
        scroll back with it, silently and only at certain widths.
        """
        html = self._get().content.decode()
        self.assertIn("gt-shell-single", html)
        self.assertNotIn("gt-shell-wide", html)


class SystemReportTilesTests(TestCase):
    """The top row answers for the screens an admin actually opens.

    It carried four platform figures and stopped. News, Pages, Enquiries and
    the team each have a screen somebody visits weekly and had no number
    anywhere, so "is there anything in News?" meant opening News to find out.
    """

    def setUp(self):
        self.boss = User.objects.create_superuser(
            email="report-boss@example.com", password="pw", display_name="Boss",
        )
        sign_in_to_admin(self.client, self.boss)

    def test_the_four_new_figures_are_counted(self):
        NewsPost.objects.create(title="One", slug="one", is_published=True)
        NewsPost.objects.create(title="Two", slug="two", is_published=False)
        counts = self.client.get(reverse("admin:system_report")).context["counts"]
        self.assertEqual(counts["news"], 2)
        self.assertEqual(counts["news_published"], 1)
        self.assertGreater(counts["pages"], 0)
        self.assertEqual(counts["team"], AdminAccess.objects.count())
        self.assertIn("enquiries", counts)

    def test_every_tile_that_claims_a_destination_has_one(self):
        """Four of the eight are new links, and a NoReverseMatch in a template
        takes the whole report down rather than one tile with it."""
        r = self.client.get(reverse("admin:system_report"))
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        for url in ("admin:hq_news", "admin:hq_pages", "admin:hq_enquiries", "admin:hq_team"):
            self.assertIn(reverse(url), html)

    def test_the_report_survives_a_missing_model(self):
        """The added counts are one guarded block on a diagnostic screen.

        The report is where somebody looks when something is already wrong, so
        a count that cannot be computed must cost its own tile and nothing
        else.
        """
        from unittest.mock import patch

        with patch("admin_panel.models.NewsPost.objects") as broken:
            broken.count.side_effect = RuntimeError("mid-migration")
            r = self.client.get(reverse("admin:system_report"))
        self.assertEqual(r.status_code, 200)


# ---------------------------------------------------------------------------
# The client's blog asks: more than one tag, and a date you choose
# ---------------------------------------------------------------------------

class TaggingAStoryWithSeveralCodesTests(TestCase):
    """"You can't put more than one tag. Ideally if something is about AFL and
    NRL or AFLW and NRLW we should be able to tag it with both?" — the client,
    1 Sept 2026."""

    def setUp(self):
        self.admin = User.objects.create_superuser(
            email="tagger@goodtip.test", password="Str0ng!pass", display_name="Tagger",
        )
        sign_in_to_hq(self.client, self.admin)

    def _post(self, **overrides):
        data = {
            "title_html": "Womens weekend",
            "excerpt_html": "Both codes on at once.",
            "body": "<p>Story.</p>",
            "tags": ["AFLW", "NRLW"],
            "is_published": "on",
        }
        data.update(overrides)
        return data

    def test_a_story_can_carry_two_codes(self):
        self.client.post(reverse("admin:hq_news_new"), self._post())
        post = NewsPost.objects.get()
        self.assertEqual(post.tag_list, ["AFLW", "NRLW"])
        self.assertEqual(post.tag_labels, ["AFLW", "NRLW"])

    def test_the_retired_single_tag_column_tracks_the_first_one(self):
        """`tag` is kept in step rather than dropped.

        It is a NOT NULL column with data in it and several templates still
        read `get_tag_display`. Keeping it pointed at the primary tag means
        nothing had to be found and changed in the same breath as this feature
        — and means a row written by anything that still only knows about `tag`
        is still readable.
        """
        self.client.post(reverse("admin:hq_news_new"), self._post())
        post = NewsPost.objects.get()
        self.assertEqual(post.tag, "AFLW")
        self.assertEqual(post.get_tag_display(), "AFLW")

    def test_the_order_is_the_choice_list_not_the_browser(self):
        """Which tag is primary must not depend on checkbox submission order."""
        self.client.post(reverse("admin:hq_news_new"), self._post(tags=["NRLW", "AFLW"]))
        post = NewsPost.objects.get()
        self.assertEqual(post.tag_list, ["AFLW", "NRLW"])

    def test_a_story_with_nothing_ticked_is_filed_under_news(self):
        """Untagged means missing from every filter, which is worse than
        mislabelled — there would be no way to find it on the list at all."""
        self.client.post(reverse("admin:hq_news_new"), self._post(tags=[]))
        post = NewsPost.objects.get()
        self.assertEqual(post.tag_list, ["NEWS"])

    def test_the_news_list_finds_a_story_under_either_of_its_codes(self):
        self.client.post(reverse("admin:hq_news_new"), self._post())
        for code in ("AFLW", "NRLW"):
            with self.subTest(code=code):
                html = self.client.get(reverse("news_index"), {"code": code}).content.decode()
                self.assertIn("Womens weekend", html)

    def test_a_story_does_not_show_under_a_code_it_is_not_tagged_with(self):
        self.client.post(reverse("admin:hq_news_new"), self._post())
        html = self.client.get(reverse("news_index"), {"code": "NRL"}).content.decode()
        self.assertNotIn("Womens weekend", html)

    def test_a_pre_migration_story_still_appears_under_its_old_tag(self):
        """`tags` empty, `tag` set — the shape every row had before today.

        The migration backfills these, but the fallback matters anyway: nothing
        should depend on a data migration having run for a story to be findable.
        """
        NewsPost.objects.create(title="Old one", tag="NRL", tags=[])
        html = self.client.get(reverse("news_index"), {"code": "NRL"}).content.decode()
        self.assertIn("Old one", html)


class SchedulingAndBackdatingTests(TestCase):
    """"Ideally it would be good if you can back date or schedule a blog in the
    future as opposed to just being able to publish today." — the client."""

    def setUp(self):
        self.admin = User.objects.create_superuser(
            email="sched@goodtip.test", password="Str0ng!pass", display_name="Sched",
        )
        sign_in_to_hq(self.client, self.admin)

    def _post(self, when, **overrides):
        data = {
            "title_html": "Queued story",
            "excerpt_html": "Teaser.",
            "body": "<p>Body.</p>",
            "tags": ["NEWS"],
            "is_published": "on",
            "published_at": when,
        }
        data.update(overrides)
        return data

    def _local(self, delta):
        return timezone.localtime(timezone.now() + delta).strftime("%Y-%m-%dT%H:%M")

    def test_a_future_date_holds_the_story_back(self):
        self.client.post(reverse("admin:hq_news_new"), self._post(self._local(timedelta(days=3))))
        post = NewsPost.objects.get()
        self.assertTrue(post.is_published)
        self.assertTrue(post.is_scheduled)
        self.assertFalse(post.is_live)

    def test_a_scheduled_story_is_not_on_the_public_list(self):
        self.client.post(reverse("admin:hq_news_new"), self._post(self._local(timedelta(days=3))))
        html = self.client.get(reverse("news_index")).content.decode()
        self.assertNotIn("Queued story", html)

    def test_a_scheduled_story_404s_at_its_own_address(self):
        """The half that would be missed by filtering the list alone.

        A story's address is its headline slugified, so a queued announcement
        is guessable — "hidden from the list" is not the same as unpublished.
        """
        self.client.post(reverse("admin:hq_news_new"), self._post(self._local(timedelta(days=3))))
        post = NewsPost.objects.get()
        self.client.logout()
        self.assertEqual(self.client.get(post.get_absolute_url()).status_code, 404)

    def test_it_appears_on_its_own_once_the_time_passes(self):
        """No cron: every reader-facing query asks for published_at <= now, so
        the moment simply arrives."""
        self.client.post(reverse("admin:hq_news_new"), self._post(self._local(timedelta(hours=2))))
        post = NewsPost.objects.get()
        self.assertNotIn("Queued story", self.client.get(reverse("news_index")).content.decode())

        post.published_at = timezone.now() - timedelta(minutes=1)
        post.save(update_fields=["published_at"])
        self.assertIn("Queued story", self.client.get(reverse("news_index")).content.decode())

    def test_a_past_date_backdates_the_story(self):
        self.client.post(reverse("admin:hq_news_new"), self._post(self._local(timedelta(days=-30))))
        post = NewsPost.objects.get()
        self.assertTrue(post.is_live)
        self.assertLess(post.published_at, timezone.now() - timedelta(days=29))

    def test_the_date_is_read_in_the_sites_timezone_not_utc(self):
        """`datetime-local` sends wall-clock time with no zone. Reading it as
        UTC would publish a story scheduled for 9am Melbourne at 7pm."""
        wanted = timezone.localtime(timezone.now()).replace(
            hour=9, minute=0, second=0, microsecond=0,
        ) + timedelta(days=1)
        self.client.post(
            reverse("admin:hq_news_new"),
            self._post(wanted.strftime("%Y-%m-%dT%H:%M")),
        )
        post = NewsPost.objects.get()
        self.assertEqual(timezone.localtime(post.published_at).hour, 9)

    def test_a_scheduled_story_cannot_be_emailed_out_yet(self):
        """Emailing cannot be recalled, so it must not run ahead of the thing
        it announces — every member would get a link that 404s."""
        self.client.post(reverse("admin:hq_news_new"), self._post(self._local(timedelta(days=2))))
        post = NewsPost.objects.get()
        self.client.post(reverse("admin:hq_news_announce", args=[post.id]))
        post.refresh_from_db()
        self.assertIsNone(post.announced_at)


class RenamingAStoryTests(TestCase):
    """The address is editable, and the old one keeps working."""

    def setUp(self):
        self.admin = User.objects.create_superuser(
            email="renamer@goodtip.test", password="Str0ng!pass", display_name="Renamer",
        )
        sign_in_to_hq(self.client, self.admin)
        self.client.post(reverse("admin:hq_news_new"), {
            "title_html": "Finals race tightens",
            "excerpt_html": "Teaser.", "body": "<p>Body.</p>",
            "tags": ["AFL"], "is_published": "on",
        })
        self.post = NewsPost.objects.get()

    def _edit(self, **overrides):
        data = {
            "title_html": "Finals race tightens",
            "excerpt_html": "Teaser.", "body": "<p>Body.</p>",
            "tags": ["AFL"], "is_published": "on",
            "slug": self.post.slug,
        }
        data.update(overrides)
        return self.client.post(reverse("admin:hq_news_edit", args=[self.post.id]), data)

    def test_the_slug_can_be_changed(self):
        self._edit(slug="finals-race")
        self.post.refresh_from_db()
        self.assertEqual(self.post.slug, "finals-race")

    def test_the_old_address_redirects_to_the_new_one(self):
        old_url = self.post.get_absolute_url()
        self._edit(slug="finals-race")
        self.client.logout()
        resp = self.client.get(old_url)
        self.assertEqual(resp.status_code, 301)
        self.assertEqual(resp["Location"], "/news/finals-race/")

    def test_renaming_twice_does_not_leave_a_chain(self):
        """Browsers cap redirect chains and search engines discount them, and
        the second rename is exactly when nobody is thinking about the first."""
        first = self.post.get_absolute_url()
        self._edit(slug="second-name")
        self.post.refresh_from_db()
        self._edit(slug="third-name")
        self.client.logout()
        resp = self.client.get(first)
        self.assertEqual(resp["Location"], "/news/third-name/")

    def test_a_slug_already_taken_is_refused_and_the_old_one_kept(self):
        NewsPost.objects.create(title="Other", slug="taken")
        self._edit(slug="taken")
        self.post.refresh_from_db()
        self.assertEqual(self.post.slug, "finals-race-tightens")

    def test_editing_a_headline_does_not_move_the_address(self):
        """The reason it was frozen in the first place still holds: a slug that
        followed every tweak of a headline would break every link already
        shared, silently, the moment somebody fixed a typo."""
        self._edit(title_html="Finals race tightens further")
        self.post.refresh_from_db()
        self.assertEqual(self.post.slug, "finals-race-tightens")


class StoryReaderTests(TestCase):
    """One story page, for everyone, with the opening third free.

    Two client notes in one screen:

      "I clicked it while I was at the public page news, and I was taken to
      the blog itself in my page ... it should be a page in itself."

      "Let them see like 1/3 of the blog, and if they want to read our blogs
      in full they will have to sign in, and if not a member they will sign
      up."
    """

    def setUp(self):
        self.member = User.objects.create_user(
            email="reader@goodtip.test", password="Str0ng!pass", display_name="Reader",
        )
        # Long enough to be worth gating: _story_preview leaves a short story
        # whole, because a three-line piece cut to one line is a tease with no
        # article behind it.
        self.body = "".join(
            f"<p>Paragraph {n} about the finals race and what it means.</p>"
            for n in range(1, 21)
        )
        self.post = NewsPost.objects.create(
            title="The finals race", slug="the-finals-race",
            excerpt="Where it stands.", body=self.body,
            is_published=True, published_at=timezone.now(),
        )
        self.url = reverse("news_detail", args=[self.post.slug])

    def test_a_member_does_not_get_the_app_nav_on_a_story(self):
        """The bug as reported: a signed-in member pressing a story on the
        public news page landed inside their own dashboard."""
        self.client.force_login(self.member)
        html = self.client.get(self.url).content.decode()
        self.assertTemplateUsed(self.client.get(self.url), "news_reader.html")
        # The member nav's own markers, none of which belong on a story page.
        self.assertNotIn("app-nav", html)
        self.assertNotIn("charity-strip", html)

    def test_a_member_reads_the_whole_story(self):
        self.client.force_login(self.member)
        html = self.client.get(self.url).content.decode()
        self.assertIn("Paragraph 20", html)
        self.assertNotIn("rd-gate", html)

    def test_a_visitor_gets_the_opening_and_an_invitation(self):
        html = self.client.get(self.url).content.decode()
        self.assertIn("Paragraph 1 about", html)
        self.assertIn("Create a free account", html)

    def test_the_rest_of_the_story_is_not_in_the_response(self):
        """THE POINT OF THE WHOLE THING. A gate that ships the article and
        covers two-thirds of it with a gradient is not a gate — it is in View
        Source, in reader mode, and in what a crawler indexes."""
        html = self.client.get(self.url).content.decode()
        self.assertNotIn("Paragraph 20", html)

    def test_the_log_in_link_comes_back_to_the_story(self):
        """Somebody who already has an account should land back on what they
        were reading, not on a dashboard having forgotten what it was."""
        html = self.client.get(self.url).content.decode()
        self.assertIn("next=", html)
        self.assertIn(self.post.slug, html)

    def test_a_short_story_is_not_gated(self):
        short = NewsPost.objects.create(
            title="Quick one", slug="quick-one", body="<p>Two lines, that's it.</p>",
            is_published=True, published_at=timezone.now(),
        )
        html = self.client.get(reverse("news_detail", args=[short.slug])).content.decode()
        self.assertIn("Two lines", html)
        self.assertNotIn("Create a free account", html)

    def test_the_preview_is_still_valid_html(self):
        """Truncator walks the tree rather than slicing the string, so what
        comes back closes every tag it opened. Cutting markup at an arbitrary
        character leaves a half-written tag and the browser closes it wherever
        it likes, taking the rest of the page with it."""
        html = self.client.get(self.url).content.decode()
        opened = html.count("<p>Paragraph")
        closed = len(re.findall(r"Paragraph \d+ about[^<]*</p>", html))
        self.assertEqual(opened, closed)
        self.assertGreater(opened, 0)

    def test_a_scheduled_story_still_404s(self):
        self.post.published_at = timezone.now() + timedelta(days=3)
        self.post.save(update_fields=["published_at"])
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_a_visitor_sees_a_blurred_taste_of_what_follows(self):
        """"Make the part that is the 2/3 visible but translucent, so someone
        can know we have data there — something that they see but cannot see."
        """
        html = self.client.get(self.url).content.decode()
        self.assertIn("rd-tease", html)
        # Real text, not placeholder bars — that is the difference between
        # "there is more here" and "this is still loading".
        self.assertRegex(html, r'rd-tease"[^>]*>\s*<p>Paragraph \d+ about')

    def test_the_blurred_run_is_bounded_not_the_whole_article(self):
        """A deliberate trim of the instruction. Anything sent to a browser is
        readable by anyone who looks — blur is a CSS filter over real text, not
        encryption — so serving the rest of the story under one would publish
        it to everybody and to every crawler while merely inconveniencing a
        reader."""
        html = self.client.get(self.url).content.decode()
        served = len(re.findall(r"<p>Paragraph \d+ about", html))
        self.assertGreater(served, 6)     # a third, plus a taste of the next
        self.assertLess(served, 20)       # never the lot

    def test_the_blurred_run_is_hidden_from_screen_readers(self):
        """Two paragraphs that stop mid-sentence are not something to read
        out. It is decoration, and it is marked as such."""
        html = self.client.get(self.url).content.decode()
        self.assertIn('class="rd-tease" aria-hidden="true"', html)

    def test_a_member_gets_no_blurred_run(self):
        self.client.force_login(self.member)
        self.assertNotIn("rd-tease", self.client.get(self.url).content.decode())

    def test_the_story_page_carries_every_social_account(self):
        """"Make sure all the socials we have are there" — the same five, and
        the same addresses, as the footer."""
        html = self.client.get(self.url).content.decode()
        for needle in (
            "mailto:hello@goodtip.com.au",
            "linkedin.com/company/good-tip-australia",
            "facebook.com/profile.php?id=61593478288209",
            "instagram.com/goodtip_mate",
            "youtube.com/@GoodTip_Australia",
        ):
            self.assertIn(needle, html, needle)
        self.assertIn("rd-social-row", html)


class ConfirmAndBusyContractTests(TestCase):
    """The guarded-submit contract between gt-confirm.js and gt-busy.js.

    A static check, and it earns its place. The two scripts broke every
    confirm-guarded form in the product and there is no server-side symptom at
    all: gt-confirm cancelled the submit to ask its question, gt-busy — whose
    listener still ran, because preventDefault does not stop propagation —
    marked the form busy for a request nobody had made, and gt-busy's own
    "already submitting" guard then blocked the real submit when the dialog
    fired it. The button sat reading "Sending" forever and nothing was ever
    posted.

    That is what "Email members" on a story did, which the client read as the
    feature never having been built. It was built; it could not be reached.

    Nothing in the Django test client executes JavaScript, so the only way to
    stop this coming back is to pin the two lines that fix it.
    """

    def _js(self, name):
        return (Path(settings.BASE_DIR) / "static" / "js" / name).read_text(encoding="utf-8")

    def test_the_confirm_dialog_stops_the_submit_it_cancelled(self):
        js = self._js("gt-confirm.js")
        head = js[js.index("addEventListener('submit'"):]
        head = head[:head.index("}, true);")]
        self.assertIn("preventDefault", head)
        self.assertIn("stopPropagation", head)

    def test_the_busy_indicator_ignores_a_form_still_carrying_its_guard(self):
        """The second lock on the same door: whichever script changes, a form
        that has not been confirmed yet cannot be marked as submitting."""
        js = self._js("gt-busy.js")
        self.assertIn("hasAttribute('data-confirm')", js)

    def test_email_members_is_a_guarded_form_and_so_is_delete(self):
        """If either loses its guard this contract stops mattering — and if
        either keeps it, the two rules above are what make it work."""
        html = (Path(settings.BASE_DIR) / "templates" / "manage" / "news.html").read_text(
            encoding="utf-8",
        )
        self.assertIn("hq_news_announce", html)
        self.assertIn("data-confirm", html)


class SeoHealthTests(TestCase):
    """The SEO screen answers "what should I do", not "how many are there".

    "For the SEO we should have a whole dashboard for that to help with
    everything." It had a strip of counts, which is not the same thing.
    """

    def setUp(self):
        self.admin = User.objects.create_superuser(
            email="seo@goodtip.test", password="Str0ng!pass", display_name="Sea",
        )
        sign_in_to_hq(self.client, self.admin)

    def _health(self):
        from admin_panel.views import _seo_health, page_registry

        pages = [{"page": p, "seo": None, "noindex": False, "customised": False}
                 for p in page_registry.PAGES]
        return {r["key"]: r for r in _seo_health(pages, list(NewsPost.objects.all()))}

    def test_a_live_story_with_no_picture_is_flagged(self):
        """Pasted into Slack or LinkedIn it comes through as a grey box with a
        URL, which reads as spam rather than as a story."""
        NewsPost.objects.create(
            title="No picture here", slug="no-picture-here",
            is_published=True, published_at=timezone.now(),
        )
        row = self._health()["share"]
        self.assertEqual(row["count"], 1)
        self.assertIn("No picture here", row["items"])

    def test_a_scheduled_story_is_not_flagged_yet(self):
        """It is not in a search result and not shareable, so counting it is
        noise — and noise is how a health check gets ignored."""
        NewsPost.objects.create(
            title="Next week", slug="next-week", is_published=True,
            published_at=timezone.now() + timedelta(days=3),
        )
        self.assertEqual(self._health()["share"]["count"], 0)

    def test_a_passing_check_still_reports(self):
        """A dashboard that hides its passing checks makes you wonder whether
        it ran them."""
        rows = self._health()
        self.assertEqual(rows["share"]["count"], 0)
        self.assertIn("share", rows)

    def test_the_failing_checks_sort_above_the_clean_ones(self):
        from admin_panel.views import _seo_health, page_registry

        NewsPost.objects.create(
            title="Needs a picture", slug="needs-a-picture",
            is_published=True, published_at=timezone.now(),
        )
        pages = [{"page": p, "seo": None, "noindex": False, "customised": False}
                 for p in page_registry.PAGES]
        rows = _seo_health(pages, list(NewsPost.objects.all()))
        self.assertGreater(rows[0]["count"], 0, "the row that needs doing is not first")
        self.assertEqual(rows[-1]["count"], 0)

    def test_the_page_renders_the_checks(self):
        html = self.client.get(reverse("admin:hq_seo")).content.decode()
        self.assertIn("What needs doing", html)
        self.assertIn("sh-card", html)


class SchedulerLookTests(TestCase):
    """The story scheduler, and the two states that used to look identical.

    "A new look and design on the blog timers where we schedule uploads."
    The redesign is not decoration: `published` here means `is_published AND
    published_at <= now`, so a future date is a schedule and looked exactly
    like a mistake — and on the list a scheduled story wore the DRAFT chip,
    which is the opposite state.
    """

    def setUp(self):
        self.admin = User.objects.create_superuser(
            email="sched@goodtip.test", password="Str0ng!pass", display_name="Skedd",
        )
        sign_in_to_hq(self.client, self.admin)

    def _post(self, when):
        return NewsPost.objects.create(
            title="Finals preview", slug="finals-preview",
            is_published=True, published_at=when,
        )

    def test_the_editor_says_which_state_a_scheduled_story_is_in(self):
        """Sep 2026: the scheduler is a state line and a button now ("let it be
        a button, then a nice time and calendar UI comes in"), not an open
        date box — but it still has to say, before anything is pressed, that
        this story is waiting on a clock."""
        when = timezone.now() + timedelta(days=2)
        post = self._post(when)
        html = self.client.get(reverse("admin:hq_news_edit", args=[post.id])).content.decode()
        self.assertIn('data-saved="scheduled"', html)
        self.assertIn(">\n              Scheduled\n", html.replace("\r", ""))
        # The countdown is drawn from the stored moment, in the browser: "in 2
        # days" baked into the HTML is wrong the moment the page has been open
        # an hour. The moment rides on the hidden field the view reads.
        stamp = timezone.localtime(when).strftime("%Y-%m-%dT%H:%M")
        self.assertIn(f'value="{stamp}"', html)

    def test_a_live_story_is_not_dressed_as_scheduled(self):
        post = self._post(timezone.now() - timedelta(days=1))
        html = self.client.get(reverse("admin:hq_news_edit", args=[post.id])).content.decode()
        self.assertIn('data-saved="live"', html)
        self.assertNotIn('data-saved="scheduled"', html)

    def test_the_list_gives_scheduled_its_own_chip(self):
        """It had the draft one, so "nobody published this" and "this is
        published and waiting on a clock" were the same grey pill."""
        self._post(timezone.now() + timedelta(days=2))
        html = self.client.get(reverse("admin:hq_news")).content.decode()
        self.assertIn('gt-chip sched', html)
        self.assertNotIn('gt-chip draft">Scheduled', html)

    def test_the_editor_offers_a_calendar_behind_a_button(self):
        """The times people actually pick — in an hour, tonight, tomorrow
        morning, next Monday — are still one press away, but they now live
        inside the calendar the button opens, which gt-news-editor.js builds
        ("WHEN IT GOES OUT"). A server-rendered test cannot see inside it; what
        it can hold is that the button, the empty picker and the field it
        writes to are all on the page, and that the old always-open date box is
        not. The presets themselves are checked in the browser."""
        post = self._post(timezone.now())
        html = self.client.get(reverse("admin:hq_news_edit", args=[post.id])).content.decode()
        self.assertIn("data-pub-open", html)
        self.assertIn("data-pub-pop", html)
        self.assertIn('name="published_at"', html)
        self.assertNotIn('type="datetime-local"', html)
        self.assertNotIn("data-sched-set", html)


@override_settings(MEDIA_ROOT=tempfile.mkdtemp(prefix="gt-prune-"))
class PruneMissingMediaTests(TestCase):
    """A reference to a file that is not there is not information.

    Found 4 Sep 2026: MEDIA_ROOT had gone from the production checkout
    entirely, the database still named 15 files, and the client's console was
    a wall of 404s. Every template already handles "no image" properly — the
    cards and the reader both draw one of the site's own match-day shots (see
    StoryFallbackImageTests), an avatar falls back to an initial — and none of
    it ran, because `{% if p.image %}` is true for a dangling reference.
    """

    @classmethod
    def tearDownClass(cls):
        drop_temp_media()
        super().tearDownClass()

    def setUp(self):
        self.gone = NewsPost.objects.create(
            title="No file behind it", slug="no-file-behind-it",
            image="news/vanished.png",
            is_published=True, published_at=timezone.now(),
        )
        self.kept = NewsPost.objects.create(
            title="This one is real", slug="this-one-is-real",
            is_published=True, published_at=timezone.now(),
        )
        self.kept.image.save("real.png", ContentFile(b"not really a png"), save=True)

    def _run(self, **opts):
        out = StringIO()
        call_command("prune_missing_media", stdout=out, **opts)
        return out.getvalue()

    def test_it_reports_and_changes_nothing_by_default(self):
        """It edits whatever database the environment points at, and on
        production that is member avatars."""
        out = self._run()
        self.assertIn("Dry run", out)
        self.gone.refresh_from_db()
        self.assertEqual(self.gone.image.name, "news/vanished.png")

    def test_apply_clears_only_the_dangling_one(self):
        self._run(apply=True)
        self.gone.refresh_from_db()
        self.kept.refresh_from_db()
        self.assertEqual(self.gone.image.name, "")
        self.assertTrue(self.kept.image.name, "a file that exists must be left alone")

    def test_a_cleared_row_renders_the_designed_empty_state(self):
        """The whole point: not "no broken image", but the fallback that was
        already written for this case actually running.

        The empty state itself changed after this test was written — the
        branded panel became one of the site's own photographs — so this
        asserts on the card carrying SOME fallback scene rather than on the
        markup of any particular one. Which photograph, and why that one, is
        StoryFallbackImageTests' job.
        """
        self._run(apply=True)
        self.gone.refresh_from_db()
        html = self.client.get(reverse("news_index")).content.decode()
        self.assertIn(self.gone.fallback_scene.rsplit(".", 1)[0], html)
        self.assertNotIn("news/vanished.png", html)

    def test_it_can_be_pointed_at_one_model(self):
        out = self._run(model="admin_panel.NewsPost")
        self.assertIn("admin_panel.NewsPost.image", out)
        self.assertNotIn("accounts.User.avatar", out)


# Writes an upload (test_a_story_with_its_own_picture_keeps_it), so it needs a
# MEDIA_ROOT of its own like every other class here — without one it leaves a
# real.png in the running instance's uploads directory on every run, which is
# where staging's came from.
@override_settings(MEDIA_ROOT=temp_media())
class StoryFallbackImageTests(TestCase):
    """A story with no picture of its own still looks like a story.

    The reader page has always done this — a pictureless story got the site's
    own match-day photographs rather than a flat green panel. The CARDS did
    not, so a list of pictureless stories read as a page that had failed to
    load. That is what the client was looking at after their uploads were
    destroyed.
    """

    @classmethod
    def tearDownClass(cls):
        drop_temp_media()
        super().tearDownClass()

    def _post(self, pk, tags):
        return NewsPost.objects.create(
            pk=pk, title=f"Story {pk}", slug=f"story-{pk}", tags=tags,
            is_published=True, published_at=timezone.now(),
        )

    def test_the_photograph_matches_the_code(self):
        """An NRL piece must not be illustrated with the MCG.

        Asserted against the POOLS rather than the filenames: mcg-match.jpg is
        an AFL ground and does not say "afl", so a substring check on the name
        both fails on a correct answer and would pass on a wrong one.
        """
        afl = set(NewsPost._SCENES["AFL"])
        nrl = set(NewsPost._SCENES["NRL"])

        def scene_of(pk, tags):
            return self._post(pk, tags).fallback_scene.rsplit("/", 1)[-1]

        self.assertIn(scene_of(101, ["NRL"]), nrl)
        self.assertIn(scene_of(102, ["NRLW"]), nrl)
        self.assertIn(scene_of(103, ["AFL"]), afl)
        self.assertIn(scene_of(104, ["AFLW"]), afl)

    def test_a_story_filed_under_news_gets_a_neutral_stadium(self):
        scene = self._post(105, ["NEWS"]).fallback_scene.rsplit("/", 1)[-1]
        self.assertIn(scene, set(NewsPost._SCENES[None]))

    def test_it_is_the_same_photograph_every_time(self):
        """A card that changed picture on every load reads as broken in a
        different way — and the same story appears on the list, the dashboard
        and the foot of another story."""
        post = self._post(106, ["AFL"])
        self.assertEqual(post.fallback_scene, NewsPost.objects.get(pk=106).fallback_scene)

    def test_the_news_list_shows_a_photograph_not_an_empty_panel(self):
        self._post(107, ["NRL"])
        html = self.client.get(reverse("news_index")).content.decode()
        self.assertIn("img/scenes/nrl-", html)
        self.assertNotIn("pn-thumb noimg", html)

    def test_a_story_with_its_own_picture_keeps_it(self):
        post = self._post(108, ["AFL"])
        post.image.save("real.png", ContentFile(b"x"), save=True)
        html = self.client.get(reverse("news_index")).content.decode()
        self.assertIn("real", html)


@override_settings(MEDIA_ROOT=temp_media())
class AttachingAPictureInTheEditorTests(TestCase):
    """A picture attached in the story editor is saved, and shows on the story.

    NOTHING COVERED THIS. Every other image test in this file assigns
    `post.image = "news/hero.jpg"` directly — a name in a column, with no file
    behind it and no upload ever performed. So the suite could stay green
    through a broken file field, a missing enctype, or a view that never looks
    at `request.FILES`, and did stay green through four weeks in which the
    uploads themselves were being deleted.

    This posts a real PNG at the real editor and follows it all the way to the
    published page.
    """

    @classmethod
    def tearDownClass(cls):
        drop_temp_media()
        super().tearDownClass()

    def setUp(self):
        self.admin = User.objects.create_superuser(
            email="shooter@goodtip.test", password="Str0ng!pass", display_name="Shooter",
        )
        sign_in_to_hq(self.client, self.admin)

    def _png(self, name="hero.png"):
        """A real PNG, because ImageField opens it and a stub is rejected."""
        from PIL import Image

        buf = BytesIO()
        Image.new("RGB", (8, 8), (10, 90, 60)).save(buf, format="PNG")
        return SimpleUploadedFile(name, buf.getvalue(), content_type="image/png")

    def _post(self, **overrides):
        data = {
            "title_html": "<b>A picture on it</b>",
            "excerpt_html": "With a photograph.",
            "body": "<p>Words.</p>",
            "tags": ["AFL"],
            "is_published": "on",
        }
        data.update(overrides)
        return data

    def test_the_file_is_written_and_the_story_shows_it(self):
        self.client.post(
            reverse("admin:hq_news_new"), self._post(image=self._png()),
        )
        post = NewsPost.objects.get()

        # The row names a file...
        self.assertTrue(post.image.name.startswith("news/"), post.image.name)
        # ...and the file is behind it. This is the assertion the incident on
        # 4 Sep 2026 turned on: the column was right and the file was gone.
        self.assertTrue(
            Path(settings.MEDIA_ROOT, post.image.name).exists(),
            "the upload was accepted but no file was written",
        )

        html = self.client.get(post.get_absolute_url()).content.decode()
        self.assertIn(post.image.url, html)

    def test_a_story_that_has_one_does_not_fall_back_to_a_stock_photograph(self):
        """The fallback is for stories with no picture. A story WITH one must
        show the picture that was chosen for it.

        Read out of the CARD rather than looked for in the page. The news list
        already draws stock photographs of its own — `page_scenes.html` rotates
        them behind the whole page — so "img/scenes/ appears in the HTML" is
        true whatever the cards are doing, and a bare assertNotIn passes and
        fails for the wrong reasons.

        The card turns through the story's pictures now (NewsPost.card_slides),
        so the assertion is that the run of frames is exactly the one picture
        the story has — a story with one picture must not be padded out with
        stock ones to make its card move.
        """
        self.client.post(
            reverse("admin:hq_news_new"), self._post(image=self._png()),
        )
        post = NewsPost.objects.get()

        html = self.client.get(reverse("news_index")).content.decode()
        frames = re.findall(
            r"""class="ndc-shot[^"]*"[^>]*background-image:url\('([^']+)'\)""", html,
        )
        self.assertEqual(frames, [post.image.url])


# ===========================================================================
# Sep 2026, the client's round on the blog: tags in their codes' colours, the
# code filter on the news lists, the share button, and slideshows in a story.
# ===========================================================================


def _mp4(seconds, *, timescale=1000, version=0):
    """The least of an MP4 that admin_panel.media_probe has to read.

    An ftyp, some media data, and then the moov holding the mvhd — moov LAST,
    because that is how a phone writes a video and the case a reader that only
    looked at the start of the file would get wrong.
    """
    import struct

    ticks = int(seconds * timescale)
    if version == 1:
        head = bytes([1, 0, 0, 0]) + struct.pack(">QQIQ", 0, 0, timescale, ticks)
    else:
        head = bytes([0, 0, 0, 0]) + struct.pack(">IIII", 0, 0, timescale, ticks)
    payload = head + b"\0" * 80
    mvhd = struct.pack(">I4s", 8 + len(payload), b"mvhd") + payload
    moov = struct.pack(">I4s", 8 + len(mvhd), b"moov") + mvhd
    ftyp = struct.pack(">I4s", 16, b"ftyp") + b"isom" + b"\0" * 4
    mdat = struct.pack(">I4s", 8 + 256, b"mdat") + b"\0" * 256
    return ftyp + mdat + moov


class StoryTagColourTests(TestCase):
    """"If it's a blog that is NRL it should carry its colour.\""""

    def test_every_tag_is_paired_with_the_code_its_colour_is_keyed_on(self):
        post = NewsPost(title="x", tags=["NRL", "NRLW"])
        self.assertEqual(post.tag_pairs, [("nrl", "NRL"), ("nrlw", "NRLW")])

    def test_every_tag_a_story_can_carry_has_a_colour(self):
        """A code with no [data-code] rule renders as a chip with no colour at
        all — nothing fails, it simply looks unfinished. So the list of tags
        and the stylesheet are held against each other here."""
        css = Path(settings.BASE_DIR, "static", "css", "goodtip.css").read_text()
        missing = [
            tag for tag, _ in NewsPost.TAG_CHOICES
            if f'[data-code="{tag.lower()}"]' not in css
        ]
        self.assertEqual(missing, [])

    def test_a_card_shows_every_code_on_the_story_each_in_its_own_colour(self):
        NewsPost.objects.create(title="NRL v NRLW", tags=["NRL", "NRLW"])
        html = self.client.get(reverse("news_index")).content.decode()
        self.assertIn('<span class="nt" data-code="nrl">NRL</span>', html)
        self.assertIn('<span class="nt" data-code="nrlw">NRLW</span>', html)


class NewsListFilterTests(TestCase):
    """Chips and a dropdown on the news & blog list, like the ladder's."""

    def setUp(self):
        now = timezone.now()
        NewsPost.objects.create(title="Origin preview", tags=["NRL"], published_at=now)
        NewsPost.objects.create(title="NRL against NRLW", tags=["NRL", "NRLW"], published_at=now)
        NewsPost.objects.create(title="Finals race", tags=["AFL"], published_at=now)

    def _html(self, **query):
        return self.client.get(reverse("news_index"), query).content.decode()

    def test_the_public_list_offers_chips_and_a_dropdown_in_the_codes_colours(self):
        html = self._html()
        for tag, _ in NewsPost.TAG_CHOICES:
            self.assertIn(f'class="cf-chip" data-code="{tag.lower()}"', html)
            self.assertIn(f'<option value="{tag}" data-code="{tag.lower()}"', html)
        self.assertIn('<select name="code"', html)

    def test_a_code_shows_every_story_that_carries_it_and_no_other(self):
        html = self._html(code="NRLW")
        self.assertIn("NRL against NRLW", html)
        self.assertNotIn("Origin preview", html)
        self.assertNotIn("Finals race", html)

    def test_the_chosen_code_is_marked_on_its_chip_and_colours_the_dropdown(self):
        html = self._html(code="NRL")
        self.assertIn('class="cf-chip on" data-code="nrl"', html)
        self.assertRegex(html, r'<select name="code"[^>]*data-code="nrl"')

    def test_the_member_list_has_the_same_filter(self):
        member = User.objects.create_user(
            email="reader@goodtip.test", password="x", display_name="Reader",
        )
        self.client.force_login(member)
        html = self._html(code="AFL")
        self.assertIn("newsfilter", html)
        self.assertIn('class="cf-chip on" data-code="afl"', html)
        self.assertIn("Finals race", html)
        self.assertNotIn("Origin preview", html)


class StoryShareTests(TestCase):
    """One labelled button, and every platform behind it."""

    def setUp(self):
        self.post = NewsPost.objects.create(
            title="Share me", tags=["AFL"], body="<p>Short.</p>",
        )
        self.html = self.client.get(self.post.get_absolute_url()).content.decode()

    def test_there_is_one_share_button_that_says_what_it_does(self):
        self.assertIn("data-share-toggle", self.html)
        self.assertIn("Share this story", self.html)

    def test_each_platform_is_its_own_share_address_for_this_story(self):
        # Django's urlencode leaves "/" alone.
        url = "http%3A//testserver/news/share-me/"
        for address in (
            f"https://www.facebook.com/sharer/sharer.php?u={url}",
            "https://api.whatsapp.com/send?text=Share%20me%20" + url,
            f"https://twitter.com/intent/tweet?url={url}",
            f"https://www.linkedin.com/sharing/share-offsite/?url={url}",
            f"https://t.me/share/url?url={url}",
            "mailto:?subject=Share%20me",
        ):
            self.assertIn(address, self.html)

    def test_every_icon_the_panel_uses_is_on_the_page(self):
        """An <svg><use href="#s-x"> with no #s-x anywhere draws nothing, and
        nothing reports it."""
        for icon in ("ic-share", "s-facebook", "s-whatsapp", "s-x", "s-linkedin",
                     "s-telegram", "s-mail", "ic-link"):
            self.assertIn(f'<symbol id="{icon}"', self.html)


@override_settings(MEDIA_ROOT=temp_media())
class StorySlideshowUploadTests(TestCase):
    """Pictures and short videos for a slideshow, through the editor's upload."""

    @classmethod
    def tearDownClass(cls):
        drop_temp_media()
        super().tearDownClass()

    def setUp(self):
        self.admin = User.objects.create_superuser(
            email="slides@goodtip.test", password="Str0ng!pass", display_name="Slides",
        )
        sign_in_to_hq(self.client, self.admin)
        self.url = reverse("admin:hq_news_upload_image")

    def _up(self, name, data, ctype):
        return self.client.post(self.url, {"file": SimpleUploadedFile(name, data, content_type=ctype)})

    def test_a_picture_is_stored_and_said_to_be_one(self):
        resp = self._up("a.gif", b"GIF89a\x01\x00\x01\x00\x00\x00\x00;", "image/gif")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["kind"], "image")

    def test_a_short_video_is_stored_and_said_to_be_one(self):
        resp = self._up("clip.mp4", _mp4(30), "video/mp4")
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        self.assertEqual(body["kind"], "video")
        self.assertEqual(body["seconds"], 30.0)
        self.assertIn("/news/slides/", body["url"])

    def test_a_video_over_45_seconds_is_refused_whatever_the_browser_said(self):
        resp = self._up("long.mp4", _mp4(52), "video/mp4")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("52 seconds", resp.json()["error"])
        # Refused before it was stored, not stored and then complained about.
        self.assertEqual(list(Path(settings.MEDIA_ROOT).rglob("long*")), [])

    def test_a_mov_is_stored_as_mp4_so_browsers_will_play_it(self):
        resp = self._up("IMG_0001.MOV", _mp4(10), "video/quicktime")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertTrue(resp.json()["url"].endswith(".mp4"))

    def test_something_that_is_neither_is_refused(self):
        resp = self._up("notes.txt", b"hello", "text/plain")
        self.assertEqual(resp.status_code, 400)


@override_settings(MEDIA_ROOT=temp_media())
class VideoLengthProbeTests(TestCase):
    """admin_panel.media_probe reads a video's length from the file itself."""

    @classmethod
    def tearDownClass(cls):
        drop_temp_media()
        super().tearDownClass()

    def test_it_finds_the_length_after_the_media_data(self):
        from .media_probe import video_seconds

        f = SimpleUploadedFile("c.mp4", _mp4(32.5), content_type="video/mp4")
        self.assertAlmostEqual(video_seconds(f), 32.5)
        self.assertEqual(f.tell(), 0, "the upload must be rewound for saving")

    def test_it_reads_the_64_bit_header_too(self):
        from .media_probe import video_seconds

        f = SimpleUploadedFile("c.mp4", _mp4(12, timescale=600, version=1), content_type="video/mp4")
        self.assertAlmostEqual(video_seconds(f), 12)

    def test_it_says_it_does_not_know_rather_than_guessing(self):
        from .media_probe import video_seconds

        self.assertIsNone(video_seconds(SimpleUploadedFile("c.mp4", b"junk" * 10)))
        self.assertIsNone(video_seconds(SimpleUploadedFile("c.webm", b"\x1aE\xdf\xa3")))


class StorySlideshowRenderTests(TestCase):
    """A slideshow is markup in the body: it has to survive the save and reach
    the reader with the script that runs it."""

    def setUp(self):
        self.admin = User.objects.create_superuser(
            email="show@goodtip.test", password="Str0ng!pass", display_name="Show",
        )
        sign_in_to_hq(self.client, self.admin)

    SHOW = (
        '<p>Para one.</p><figure class="gt-slides" data-slides contenteditable="false">'
        '<div class="gs-track"><div class="gs-slide" data-kind="image"><img src="/media/a.jpg" alt="A"></div>'
        '<div class="gs-slide" data-kind="video"><video src="/media/b.mp4" muted playsinline preload="metadata">'
        '</video></div></div></figure><p>Para two.</p>'
    )

    def test_it_survives_the_save_and_reaches_the_reader(self):
        self.client.post(reverse("admin:hq_news_new"), {
            "title_html": "Slides", "body": self.SHOW, "tags": ["NRL"], "is_published": "on",
        })
        post = NewsPost.objects.get()
        self.assertIn('<video src="/media/b.mp4" muted playsinline', post.body)
        html = self.client.get(post.get_absolute_url()).content.decode()
        self.assertIn('class="gt-slides" data-slides', html)
        self.assertIn("js/gt-slides", html)

    def test_the_editor_offers_the_slideshow_button_and_its_manager(self):
        html = self.client.get(reverse("admin:hq_news_new")).content.decode()
        self.assertIn('data-action="slides"', html)
        self.assertIn("data-slides-dialog", html)
        self.assertIn("0 of 10", html)


# ===========================================================================
# Sep 2026, the client's third round on the blog cards: the codes above the
# headline, the picture turning through the story's own media, "More", the
# whole card as one link, and a colour fusion for a story about two
# competitions. See partials/_news_deck_card.html.
# ===========================================================================

class StoryCardMediaTests(TestCase):
    """What a card turns through — NewsPost.card_slides.

    "In that place we show the images, it must be changing in like 3 seconds."
    The frames are the story's OWN pictures, which is the whole of the
    behaviour: a card that turns through photographs the story does not
    contain is telling the reader something untrue about it.
    """

    def _post(self, **kw):
        kw.setdefault("title", "A story")
        kw.setdefault("tags", ["NRL"])
        return NewsPost.objects.create(**kw)

    def test_it_finds_the_pictures_in_the_body(self):
        post = self._post(body=(
            '<p>One.</p><figure class="gt-slides"><div class="gs-track">'
            '<div class="gs-slide"><img src="/media/a.jpg" alt="A goal"></div>'
            '<div class="gs-slide"><video src="/media/b.mp4"></video></div>'
            "</div></figure>"
        ))
        self.assertEqual(
            [(s["kind"], s["url"]) for s in post.card_slides],
            [("image", "/media/a.jpg"), ("video", "/media/b.mp4")],
        )
        self.assertEqual(post.card_slides[0]["alt"], "A goal")
        self.assertTrue(post.card_turns)

    def test_the_featured_picture_comes_first(self):
        post = self._post(body='<p><img src="/media/in-body.jpg"></p>')
        post.image = "news/hero.png"
        post.image_alt = "The hero"
        post.save()
        self.assertEqual(
            [s["url"] for s in post.card_slides],
            [post.image.url, "/media/in-body.jpg"],
        )

    def test_the_same_picture_twice_is_one_frame(self):
        """A featured image reused as the first picture of the body is one
        photograph, and a card that shows it, then shows it again, reads as
        broken rather than as a slideshow."""
        post = self._post(body='<p><img src="/media/news/hero.png"></p>')
        post.image = "news/hero.png"
        post.save()
        self.assertEqual([s["url"] for s in post.card_slides], [post.image.url])
        self.assertFalse(post.card_turns)

    def test_one_picture_is_not_padded_out_with_stock_ones(self):
        """The card sits still rather than claiming pictures the story has
        not got. Movement is not worth a lie about the contents."""
        post = self._post()
        post.image = "news/hero.png"
        post.save()
        self.assertEqual(len(post.card_slides), 1)
        self.assertFalse(post.card_turns)

    def test_no_picture_at_all_turns_through_the_code_s_own_scenes(self):
        """What the reader page has always done with a missing hero. They are
        marked "scene" so a template knows not to describe them as the
        story's, and they are the RIGHT sport's."""
        post = self._post(tags=["NRL"])
        kinds = {s["kind"] for s in post.card_slides}
        self.assertEqual(kinds, {"scene"})
        self.assertTrue(post.card_turns)
        self.assertTrue(all("nrl" in s["url"] for s in post.card_slides),
                        post.card_slides)
        self.assertTrue(all(s["alt"] == "" for s in post.card_slides))

    def test_the_first_frame_is_the_photograph_the_story_wears_elsewhere(self):
        """The same story has to look the same on the list, on the dashboard
        and in the "more from GoodTip" row at the foot of another story."""
        post = self._post()
        self.assertEqual(post.card_slides[0]["url"], post.fallback_scene)

    def test_a_card_never_turns_through_more_than_six(self):
        body = "".join(f'<img src="/media/{n}.jpg">' for n in range(20))
        post = self._post(body=body)
        self.assertEqual(len(post.card_slides), NewsPost.CARD_SLIDE_MAX)


class StoryCardRenderTests(TestCase):
    """The card itself: order, the fusion band, one link, and "More"."""

    def setUp(self):
        self.user = User.objects.create_user(
            email="card@example.com", password="x", display_name="Card",
        )
        self.client.force_login(self.user)

    def _card(self, **kw):
        kw.setdefault("title", "Two codes, one story")
        NewsPost.objects.create(**kw)
        html = self.client.get(reverse("news_index")).content.decode()
        start = html.index('<article class="nd-card')
        return html[start:html.index("</article>", start)]

    def test_the_codes_sit_above_the_headline_and_the_headline_above_the_picture(self):
        """The client's order, top to bottom: "the competitions ... should be
        on top of that blog but still look like part of that blog, then let's
        have the Title of that blog come under it, then now the image"."""
        card = self._card(tags=["NRL"], excerpt="A teaser.")
        self.assertLess(card.index("ndc-tags"), card.index("ndc-h"))
        self.assertLess(card.index("ndc-h"), card.index("ndc-shots"))
        self.assertLess(card.index("ndc-shots"), card.index("ndc-p"))
        self.assertLess(card.index("ndc-p"), card.index("ndc-more"))

    def test_a_story_about_two_codes_gets_one_band_segment_each(self):
        """"If it's a mixture blog of different competitions, let it have a
        slick colour fusion that does not mix." One hard-edged segment per
        code, each reading its own [data-code] token, so the colours meet
        without either being diluted into the other."""
        card = self._card(tags=["NRL", "AFL"])
        bar = card[card.index('class="ndc-bar"'):card.index("</span>", card.index('class="ndc-bar"'))]
        self.assertIn('<i data-code="nrl"></i>', bar)
        self.assertIn('<i data-code="afl"></i>', bar)
        # Nothing spells a colour out: the segments read the tokens the ladder
        # and the fixtures read, so a competition's colour is defined once.
        self.assertNotIn("#", bar)

    def test_one_code_is_one_segment_and_still_its_own_colour(self):
        card = self._card(tags=["AFLW"])
        self.assertEqual(card.count("<i data-code="), 2)   # the bar and its halo
        self.assertIn('data-code="aflw"', card)

    def test_the_whole_card_is_one_link(self):
        """A press anywhere opens the story — .ndc-a is stretched over the
        card — and there is still only ONE link in it, so a screen reader is
        handed the headline rather than six controls called "More"."""
        card = self._card(tags=["NEWS"], excerpt="A teaser.")
        self.assertEqual(card.count("<a "), 1)
        self.assertIn('class="ndc-a"', card)
        self.assertIn("news/", card)
        # "More" looks like the button that was asked for and is not a second
        # copy of the same address.
        self.assertIn('class="ndc-more"', card)
        self.assertNotIn('<a class="ndc-more"', card)

    def test_a_turning_card_says_so_and_a_still_one_does_not(self):
        card = self._card(tags=["NEWS"])           # no picture: scenes, so it turns
        self.assertIn("data-card-shots", card)
        self.assertIn('data-every="3000"', card)
        self.assertIn("ndc-pips", card)

    def test_the_public_list_and_the_dashboard_render_the_same_card(self):
        """One story card in the product. It was three, kept looking alike by
        hand, and only two of them ever got a change."""
        from catalog.models import Season
        from orgs.models import OrgMember, Organisation

        # The deck lives on the dashboard a member with an organisation gets;
        # somebody who has not joined one yet sees the "find your organisation"
        # page instead, which has never carried it.
        season = Season.objects.create(year=2097, label="2097")
        org = Organisation.objects.create(name="Card Co", season=season)
        OrgMember.objects.create(user=self.user, org=org)

        NewsPost.objects.create(title="Shared", tags=["NRL"])
        for url in (reverse("news_index"), reverse("dashboard")):
            html = self.client.get(url).content.decode()
            self.assertIn('<article class="nd-card', html, url)
            self.assertIn("ndc-more", html, url)


class FeaturedMediaTests(TestCase):
    """The featured set: three pictures and three videos, posted as JSON.

    "The place where we have featured image, can it take 3 images and 3 videos
    as well, that will also be changing." The files upload first (through
    news_upload_image) and the editor posts back their storage PATHS — which
    makes the path attacker-supplied text, and most of these tests are about
    what the view refuses to believe.
    """

    def setUp(self):
        from django.core.files.base import ContentFile

        self.admin = User.objects.create_superuser(
            email="fm@example.com", password="x", display_name="FM",
        )
        self.client.force_login(self.admin)
        s = self.client.session
        from sysadmin import otp
        otp.mark_verified(s)
        s.save()
        self.saved = []
        for n in range(5):
            self.saved.append(default_storage.save(f"news/featured/p{n}.png", ContentFile(b"png")))
        for n in range(4):
            self.saved.append(default_storage.save(f"news/featured/v{n}.mp4", ContentFile(b"mp4")))

    def tearDown(self):
        for path in self.saved:
            default_storage.delete(path)

    def _save(self, items, **extra):
        import json
        data = {"title_html": "Featured", "body": "<p>x</p>", "tags": ["NRL"],
                "is_published": "on", "featured_media": json.dumps(items)}
        data.update(extra)
        self.client.post(reverse("admin:hq_news_new"), data)
        return NewsPost.objects.get()

    def test_pictures_and_videos_are_saved_in_order(self):
        imgs = [p for p in self.saved if p.endswith(".png")]
        vids = [p for p in self.saved if p.endswith(".mp4")]
        post = self._save([
            {"kind": "video", "path": vids[0], "alt": "The try"},
            {"kind": "image", "path": imgs[0], "alt": "The crowd"},
        ])
        self.assertEqual(
            [(m.kind, m.file.name) for m in post.media.all()],
            [("video", vids[0]), ("image", imgs[0])],
        )

    def test_the_share_image_is_the_first_picture_not_the_first_item(self):
        """A clip cannot be a share card. post.image follows the first IMAGE."""
        imgs = [p for p in self.saved if p.endswith(".png")]
        vids = [p for p in self.saved if p.endswith(".mp4")]
        post = self._save([
            {"kind": "video", "path": vids[0]},
            {"kind": "image", "path": imgs[1], "alt": "Second in line, first picture"},
        ])
        self.assertEqual(post.image.name, imgs[1])
        self.assertEqual(post.image_alt, "Second in line, first picture")

    def test_three_of_each_and_no_more(self):
        imgs = [p for p in self.saved if p.endswith(".png")]
        vids = [p for p in self.saved if p.endswith(".mp4")]
        post = self._save(
            [{"kind": "image", "path": p} for p in imgs] +
            [{"kind": "video", "path": p} for p in vids]
        )
        kinds = [m.kind for m in post.media.all()]
        self.assertEqual(kinds.count("image"), 3)
        self.assertEqual(kinds.count("video"), 3)

    def test_a_path_outside_news_is_refused(self):
        """Without this a crafted form could put any file in media/ — an avatar,
        a message attachment — on a public story."""
        from django.core.files.base import ContentFile
        other = default_storage.save("avatars/someone.png", ContentFile(b"png"))
        self.saved.append(other)
        post = self._save([
            {"kind": "image", "path": other},
            {"kind": "image", "path": "news/../avatars/someone.png"},
            {"kind": "image", "path": "/etc/passwd"},
        ])
        self.assertEqual(post.media.count(), 0)
        self.assertFalse(post.image)

    def test_a_path_that_does_not_exist_is_refused(self):
        post = self._save([{"kind": "image", "path": "news/featured/never-uploaded.png"}])
        self.assertEqual(post.media.count(), 0)

    def test_saving_without_the_set_leaves_the_picture_alone(self):
        """An older client, or a script, that does not send featured_media must
        not be read as "take every picture off this story"."""
        imgs = [p for p in self.saved if p.endswith(".png")]
        post = self._save([{"kind": "image", "path": imgs[0], "alt": "Keep me"}])
        self.client.post(reverse("admin:hq_news_edit", args=[post.pk]), {
            "title_html": "Featured, edited", "body": "<p>x</p>", "tags": ["NRL"],
            "is_published": "on",
        })
        post.refresh_from_db()
        self.assertEqual(post.image.name, imgs[0])
        self.assertEqual(post.image_alt, "Keep me")
        self.assertEqual(post.media.count(), 1)

    def test_a_story_from_before_the_set_opens_with_its_picture(self):
        """Every story on the site today has `image` and no NewsMedia rows. It
        must arrive in the manager as its first picture, or opening it and
        pressing Save would drop the picture."""
        from admin_panel.views import _featured_items
        imgs = [p for p in self.saved if p.endswith(".png")]
        post = NewsPost.objects.create(title="Old", tags=["NRL"], image=imgs[2], image_alt="Old pic")
        self.assertEqual(
            _featured_items(post),
            [{"kind": "image", "url": post.image.url, "path": imgs[2], "alt": "Old pic"}],
        )
        self.assertEqual(post.featured_media[0]["url"], post.image.url)


class WhenItGoesOutTests(TestCase):
    """The scheduler is a button now; blank means "when you save"."""

    def setUp(self):
        self.admin = User.objects.create_superuser(
            email="pub@example.com", password="x", display_name="Pub",
        )
        self.client.force_login(self.admin)
        s = self.client.session
        from sysadmin import otp
        otp.mark_verified(s)
        s.save()

    def test_a_new_story_has_no_date_until_one_is_picked(self):
        """The old box was always open and always filled in with "now", so
        every story looked scheduled and a stray keystroke could hold one back
        by a month."""
        html = self.client.get(reverse("admin:hq_news_new")).content.decode()
        self.assertIn('data-pub-input', html)
        self.assertIn('value=""', html[html.index('data-pub-input') - 120:html.index('data-pub-input') + 40])
        self.assertIn("Schedule for later", html)
        self.assertNotIn('type="datetime-local"', html)

    def test_blank_publishes_at_the_moment_of_saving(self):
        before = timezone.now()
        self.client.post(reverse("admin:hq_news_new"), {
            "title_html": "Now", "body": "<p>x</p>", "tags": ["NRL"],
            "is_published": "on", "published_at": "",
        })
        post = NewsPost.objects.get()
        self.assertGreaterEqual(post.published_at, before)
        self.assertTrue(post.is_live)

    def test_a_picked_future_time_holds_the_story(self):
        when = timezone.localtime(timezone.now() + timedelta(days=3)).strftime("%Y-%m-%dT%H:%M")
        self.client.post(reverse("admin:hq_news_new"), {
            "title_html": "Later", "body": "<p>x</p>", "tags": ["NRL"],
            "is_published": "on", "published_at": when,
        })
        post = NewsPost.objects.get()
        self.assertTrue(post.is_scheduled)
        self.assertFalse(post.is_live)

    def test_editing_a_story_keeps_its_date(self):
        """So fixing a typo does not quietly re-date a story as today's news."""
        old = timezone.now() - timedelta(days=10)
        post = NewsPost.objects.create(title="Old news", tags=["NRL"], published_at=old)
        html = self.client.get(reverse("admin:hq_news_edit", args=[post.pk])).content.decode()
        stamp = timezone.localtime(old).strftime("%Y-%m-%dT%H:%M")
        self.assertIn(f'value="{stamp}"', html)
