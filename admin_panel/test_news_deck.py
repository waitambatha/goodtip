"""The turning news deck — the dashboard's two rows, and the news pages' three."""
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from .news_deck import NEWS_PAGE_ROWS, deal_news_deck


class NewsPageDeckTests(TestCase):
    """Client, Sep 2026: the cards should turn "in the dashboard as well as in
    the news page", three rows at most, "the way we have the news and blogs in
    the dashboard" — on the member page and the public one alike."""

    def setUp(self):
        from accounts.models import User

        from .models import NewsPost

        # Eleven stories: more than one nine-card page, so the deck turns.
        for n in range(11):
            NewsPost.objects.create(title=f"Story {n}", slug=f"story-{n}", is_published=True)
        self.user = User.objects.create_user(email="n@b.com", password="x", display_name="Nell")

    def _assert_three_row_deck(self, html):
        self.assertIn("data-news-deck", html)
        self.assertIn('data-turns="2"', html)
        self.assertEqual(html.count("data-news-row="), 3)
        self.assertIn("gt-news-cards.js", html)
        # The long list below the deck is gone: nine cards up, the rest behind
        # the dots.
        self.assertNotIn("nd-list", html)

    def test_a_member_gets_the_three_row_deck(self):
        self.client.force_login(self.user)
        self._assert_three_row_deck(self.client.get(reverse("news_index")).content.decode())

    def test_a_visitor_gets_the_same_deck_with_the_filter_centred(self):
        html = self.client.get(reverse("news_index")).content.decode()
        self._assert_three_row_deck(html)
        self.assertIn("newsfilter is-centred", html)
        self.assertNotIn("pnews-grid", html)


class DealNewsDeckTests(SimpleTestCase):
    def test_eight_stories_make_two_turns_and_wrap_rather_than_pad(self):
        deck = deal_news_deck(list(range(1, 9)))
        self.assertEqual(deck["news_turns"], 2)
        places = deck["news_rows"][0] + deck["news_rows"][1]
        self.assertEqual(len(places), 6)
        # Turn two shows 7, 8, 1, 2, 3, 4 — no holes.
        self.assertEqual([p[1] for p in places], [7, 8, 1, 2, 3, 4])

    def test_the_news_page_deck_is_three_rows_of_three(self):
        deck = deal_news_deck(list(range(1, 12)), rows=NEWS_PAGE_ROWS)
        self.assertEqual(deck["news_turns"], 2)
        self.assertEqual([len(r) for r in deck["news_rows"]], [3, 3, 3])

    def test_six_or_fewer_is_one_turn_and_does_not_rotate(self):
        deck = deal_news_deck([1, 2, 3, 4])
        self.assertEqual(deck["news_turns"], 1)
        self.assertEqual(len(deck["news_rows"]), 2)
        self.assertTrue(deck["news_any"])

    def test_no_stories_is_no_deck(self):
        deck = deal_news_deck([])
        self.assertFalse(deck["news_any"])
        self.assertEqual(deck["news_rows"], [[]])
