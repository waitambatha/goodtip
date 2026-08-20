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
