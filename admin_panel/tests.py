"""Tests for the News & blog story editor.

These cover the round trip that has no other safety net: what the
contenteditable surfaces post, what gets stored, and what the published
article then shows. A broken template here is a runtime error, not an import
error, so `manage.py check` never sees it.
"""
import re
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from .models import NewsPost

User = get_user_model()


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
        root = Path(settings.BASE_DIR) / "templates"
        offenders = []
        for path in sorted(root.rglob("*.html")):
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
        self.client.force_login(self.admin)

    def _post(self, **overrides):
        data = {
            "title_html": "<b>Finals race tightens</b>",
            "excerpt_html": "<b>Two</b> games separate fourth from ninth.",
            "body": "<p>Three rounds to play.</p>",
            "tag": "AFL",
            "is_published": "on",
        }
        data.update(overrides)
        return data

    # ---- the teaser is rich text now, with a plain-text mirror --------------

    def test_teaser_keeps_its_formatting_and_mirrors_plain_text(self):
        self.client.post(reverse("manage:news_new"), self._post())
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
        self.client.post(reverse("manage:news_new"), self._post(
            title_html="Tips &amp; tricks",
            excerpt_html="Fourth &amp; ninth",
        ))
        post = NewsPost.objects.get()
        self.assertEqual(post.title, "Tips & tricks")
        self.assertEqual(post.excerpt, "Fourth & ninth")

    def test_article_shows_the_formatted_teaser(self):
        self.client.post(reverse("manage:news_new"), self._post())
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
        html = self.client.get(reverse("manage:news_edit", args=[post.id])).content.decode()
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
            reverse("manage:news_edit", args=[post.id]),
            self._post(excerpt_html="A teaser from before."),
        )
        post.refresh_from_db()
        self.assertEqual(post.excerpt_html, "A teaser from before.")
        self.assertEqual(post.excerpt, "A teaser from before.")

    # ---- the URL is generated, unique and stable ----------------------------

    def test_slug_is_generated_from_the_headline(self):
        self.client.post(reverse("manage:news_new"), self._post())
        self.assertEqual(NewsPost.objects.get().slug, "finals-race-tightens")

    def test_a_second_story_with_the_same_headline_gets_its_own_url(self):
        self.client.post(reverse("manage:news_new"), self._post())
        self.client.post(reverse("manage:news_new"), self._post())
        slugs = set(NewsPost.objects.values_list("slug", flat=True))
        self.assertEqual(slugs, {"finals-race-tightens", "finals-race-tightens-2"})

    def test_editing_the_headline_does_not_move_the_story(self):
        """Links already shared have to keep working."""
        self.client.post(reverse("manage:news_new"), self._post())
        post = NewsPost.objects.get()
        self.client.post(
            reverse("manage:news_edit", args=[post.id]),
            self._post(title_html="A completely different headline"),
        )
        post.refresh_from_db()
        self.assertEqual(post.title, "A completely different headline")
        self.assertEqual(post.slug, "finals-race-tightens")

    def test_the_post_list_offers_the_link_to_copy(self):
        self.client.post(reverse("manage:news_new"), self._post())
        html = self.client.get(reverse("manage:news")).content.decode()
        self.assertIn("/news/finals-race-tightens/", html)
        self.assertIn("data-copy-link", html)

    def test_the_edit_page_shows_the_live_url_with_copy_and_view(self):
        self.client.post(reverse("manage:news_new"), self._post())
        post = NewsPost.objects.get()
        html = self.client.get(reverse("manage:news_edit", args=[post.id])).content.decode()
        self.assertIn("Live at", html)
        self.assertIn("data-slug-fixed=\"finals-race-tightens\"", html)
        self.assertIn("http://testserver/news/finals-race-tightens/", html)

    def test_a_new_post_previews_the_url_it_will_get(self):
        html = self.client.get(reverse("manage:news_new")).content.decode()
        self.assertIn("Will publish at", html)
        self.assertNotIn("data-slug-fixed", html)

    def test_an_unpublished_post_offers_no_link_because_it_would_404(self):
        self.client.post(reverse("manage:news_new"), self._post(is_published=""))
        html = self.client.get(reverse("manage:news")).content.decode()
        self.assertNotIn("data-copy-link", html)

    # ---- "link to full story" is gone, sources stay -------------------------

    def test_the_editor_no_longer_offers_a_link_to_a_story_elsewhere(self):
        html = self.client.get(reverse("manage:news_new")).content.decode()
        self.assertNotIn('name="link_url"', html)
        self.assertIn('name="source_url"', html)

    def test_a_posted_link_url_is_ignored(self):
        self.client.post(reverse("manage:news_new"), self._post(link_url="https://elsewhere.test/"))
        self.assertEqual(NewsPost.objects.get().link_url, "")

    def test_the_article_sends_nobody_off_to_an_original_story(self):
        self.client.post(reverse("manage:news_new"), self._post())
        post = NewsPost.objects.get()
        html = self.client.get(post.get_absolute_url()).content.decode()
        self.assertNotIn("Read the original story", html)

    def test_sources_still_save(self):
        self.client.post(reverse("manage:news_new"), self._post(
            source_label="AFL.com.au", source_url="https://afl.com.au/news",
        ))
        self.assertEqual(
            NewsPost.objects.get().sources,
            [{"label": "AFL.com.au", "url": "https://afl.com.au/news"}],
        )

    # ---- toolbar controls ---------------------------------------------------

    def test_font_size_is_a_number_not_a_named_bucket(self):
        html = self.client.get(reverse("manage:news_new")).content.decode()
        self.assertIn('data-cmd="fontSizePx"', html)
        self.assertIn('data-size-step="-1"', html)
        self.assertNotIn("fontSizeCustom", html)

    def test_every_writing_surface_has_a_toolbar(self):
        """The teaser used to be the one plain textarea left on the page."""
        html = self.client.get(reverse("manage:news_new")).content.decode()
        for surface in ("headline", "teaser", "body"):
            self.assertIn(f'data-editor="{surface}"', html)
        self.assertNotIn("<textarea", html)

    # ---- featured image -----------------------------------------------------

    def test_no_comment_text_leaks_into_the_drop_zone(self):
        """The drop zone is a flex row, so leaked text is not just ugly.

        A stray text node between the flex children becomes an anonymous flex
        item, which stole the width from the real label and wrapped it one
        character per line.
        """
        html = self.client.get(reverse("manage:news_new")).content.decode()
        self.assertNotIn("{#", html)
        self.assertNotIn("bare file input cannot be styled", html)

    def test_no_comment_text_leaks_onto_the_published_article(self):
        self.client.post(reverse("manage:news_new"), self._post())
        post = NewsPost.objects.get()
        html = self.client.get(post.get_absolute_url()).content.decode()
        self.assertNotIn("{#", html)
        self.assertNotIn("Share bar + optional sources", html)
        self.assertNotIn("Open Graph / Twitter card tags", html)
        # …while the tags those comments describe are still emitted.
        self.assertIn('property="og:title"', html)
        self.assertIn("as-copy", html)


    def test_the_featured_image_can_be_taken_back_off_a_post(self):
        self.client.post(reverse("manage:news_new"), self._post())
        post = NewsPost.objects.get()
        post.image = "news/hero.jpg"
        post.save(update_fields=["image"])

        self.client.post(
            reverse("manage:news_edit", args=[post.id]),
            self._post(image_clear="1"),
        )
        post.refresh_from_db()
        self.assertFalse(post.image)

    def test_saving_without_touching_the_image_keeps_it(self):
        self.client.post(reverse("manage:news_new"), self._post())
        post = NewsPost.objects.get()
        post.image = "news/hero.jpg"
        post.save(update_fields=["image"])

        self.client.post(reverse("manage:news_edit", args=[post.id]), self._post())
        post.refresh_from_db()
        self.assertEqual(post.image.name, "news/hero.jpg")


# ---------------------------------------------------------------------------
# Pages — editing the words on the site from the page itself.
# ---------------------------------------------------------------------------
from .middleware import EDIT_PARAM  # noqa: E402
from .models import PageEdit  # noqa: E402
from .pagetext import block_key, find_blocks, rewrite  # noqa: E402


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

    def test_an_image_edit_swaps_the_src_and_keeps_the_rest(self):
        _, _, blocks = find_blocks(self.SAMPLE)
        key = next(k for _, _, k, kind in blocks if kind == "image")
        out, _ = rewrite(self.SAMPLE, {key: ("image", "/media/new.png")})
        self.assertIn('<img src="/media/new.png" alt="A">', out)

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
        self.client.force_login(self.admin)
        html = self.client.get(url, {EDIT_PARAM: "1"}).content.decode()
        keys = re.findall(r'data-gte="([^"]+)"', html)
        self.assertTrue(keys, "the page offered nothing to edit")
        return keys[0]

    def test_edit_mode_tags_the_blocks_and_loads_the_editor(self):
        self.client.force_login(self.admin)
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
        self.client.force_login(self.admin)
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
        self.client.force_login(self.member)
        html = self.client.get(reverse("how_it_works"), {EDIT_PARAM: "1"}).content.decode()
        self.assertNotIn("data-gte=", html)
        self.assertNotIn("gte-bar", html)

    def test_an_anonymous_visitor_cannot_turn_edit_mode_on(self):
        html = self.client.get(reverse("how_it_works"), {EDIT_PARAM: "1"}).content.decode()
        self.assertNotIn("gte-bar", html)

    def test_saving_changes_what_every_visitor_then_reads(self):
        key = self._a_block_on(reverse("how_it_works"))
        res = self.client.post(
            reverse("manage:page_save"),
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
            reverse("manage:page_save"),
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
            reverse("manage:page_save"),
            data={"page": "how_it_works", "blocks": [
                {"key": key, "html": '<b onclick="steal()">Hi</b><script>bad()</script>'},
            ]},
            content_type="application/json",
        )
        stored = PageEdit.objects.get(block_key=key).html
        self.assertNotIn("onclick", stored)
        self.assertNotIn("<script", stored)

    def test_a_member_cannot_save_an_edit(self):
        self.client.force_login(self.member)
        res = self.client.post(
            reverse("manage:page_save"),
            data={"page": "home", "blocks": [{"key": "p-abc", "html": "hi"}]},
            content_type="application/json",
        )
        self.assertNotEqual(res.status_code, 200)
        self.assertFalse(PageEdit.objects.exists())

    def test_saving_against_an_unregistered_page_is_refused(self):
        self.client.force_login(self.admin)
        res = self.client.post(
            reverse("manage:page_save"),
            data={"page": "not_a_page", "blocks": [{"key": "p-abc", "html": "hi"}]},
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 400)
        self.assertFalse(PageEdit.objects.exists())

    def test_reverting_a_block_removes_the_edit(self):
        key = self._a_block_on(reverse("how_it_works"))
        self.client.post(
            reverse("manage:page_save"),
            data={"page": "how_it_works", "blocks": [{"key": key, "html": "Hi"}]},
            content_type="application/json",
        )
        self.client.post(
            reverse("manage:page_save"),
            data={"page": "how_it_works", "blocks": [{"key": key, "revert": True}]},
            content_type="application/json",
        )
        self.assertFalse(PageEdit.objects.filter(block_key=key).exists())

    def test_reverting_a_whole_page_puts_every_word_back(self):
        key = self._a_block_on(reverse("how_it_works"))
        self.client.post(
            reverse("manage:page_save"),
            data={"page": "how_it_works", "blocks": [{"key": key, "html": "Changed"}]},
            content_type="application/json",
        )
        self.client.post(reverse("manage:page_revert", args=["how_it_works"]))
        self.assertFalse(PageEdit.objects.filter(page="how_it_works").exists())
        html = self.client.get(reverse("how_it_works")).content.decode()
        self.assertNotIn("Changed", html)

    def test_the_pages_index_lists_public_and_private_separately(self):
        self.client.force_login(self.admin)
        html = self.client.get(reverse("manage:pages")).content.decode()
        self.assertIn("Public pages", html)
        self.assertIn("Private pages", html)
        self.assertIn("How it works", html)
        self.assertIn("Dashboard", html)

    def test_one_page_shows_what_each_edit_replaced(self):
        key = self._a_block_on(reverse("how_it_works"))
        self.client.post(
            reverse("manage:page_save"),
            data={"page": "how_it_works", "blocks": [
                {"key": key, "html": "The new words", "original": "The old words"},
            ]},
            content_type="application/json",
        )
        html = self.client.get(reverse("manage:page_edits", args=["how_it_works"])).content.decode()
        self.assertIn("The old words", html)
        self.assertIn("The new words", html)

    def test_asking_for_a_page_that_does_not_exist_is_a_404(self):
        self.client.force_login(self.admin)
        self.assertEqual(
            self.client.get(reverse("manage:page_edits", args=["nope"])).status_code, 404
        )

    def test_only_a_superuser_reaches_the_pages_index(self):
        self.client.force_login(self.member)
        res = self.client.get(reverse("manage:pages"))
        self.assertNotEqual(res.status_code, 200)

    def test_an_unregistered_url_is_never_parsed(self):
        """The middleware has to be invisible everywhere it is not wanted."""
        self.client.force_login(self.admin)
        res = self.client.get(reverse("manage:pages"), {EDIT_PARAM: "1"})
        self.assertNotIn(b"data-gte=", res.content)


class AdminThemeToggleTests(TestCase):
    """The green/cream switch in /admin/.

    Django ships it as a bare 16px icon wedged into the header's row of text
    links, and the client's report was simply that they never saw it. The
    override keeps Django's button, class and behaviour — theme.js still binds
    to `.theme-toggle` and still cycles auto/light/dark — and adds the thing
    that was missing: words saying which theme you are on.
    """

    def setUp(self):
        self.admin = User.objects.create_superuser(
            email="root@example.com", password="pw", display_name="Root",
        )
        self.client.force_login(self.admin)

    def test_the_toggle_says_which_theme_it_is_on(self):
        html = self.client.get(reverse("admin:index")).content.decode()
        self.assertIn("Theme", html)
        for label in ("Auto", "Dark", "Light"):
            self.assertIn(f'class="theme-label-when-{label.lower()}">{label}<', html)

    def test_django_still_recognises_it_as_its_own_control(self):
        """theme.js finds the button by class name and nothing else. Lose the
        class and the control becomes decorative."""
        html = self.client.get(reverse("admin:index")).content.decode()
        self.assertIn('class="theme-toggle gt-theme-toggle"', html)
        for icon in ("icon-auto", "icon-moon", "icon-sun"):
            self.assertIn(icon, html)

    def test_the_labels_are_not_screen_reader_only_any_more(self):
        """The whole change: they used to carry .visually-hidden, which is why
        the control was three states of the same small icon."""
        html = self.client.get(reverse("admin:index")).content.decode()
        toggle = html[html.find("theme-toggle"):]
        toggle = toggle[:toggle.find("</button>")]
        self.assertNotIn("visually-hidden", toggle)

    def test_the_stylesheet_that_sizes_it_is_loaded(self):
        html = self.client.get(reverse("admin:index")).content.decode()
        self.assertIn("css/gt-admin.css", html)
