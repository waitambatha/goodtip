"""The turning news deck — the dashboard's two rows, and the news pages' four."""
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from .news_deck import NEWS_PAGE_ROWS, deal_news_deck


class NewsPageDeckTests(TestCase):
    """Client, Sep 2026: the cards should turn "in the dashboard as well as in
    the news page", "the way we have the news and blogs in the dashboard" —
    on the member page and the public one alike. Four rows, not three, since
    19 Oct 2026 (client: "I would want for both the staging and coming soon
    to have 4")."""

    def setUp(self):
        from accounts.models import User

        from .models import NewsPost

        # Fourteen stories: more than one twelve-card page (4 rows of 3), so
        # the deck turns.
        for n in range(14):
            NewsPost.objects.create(title=f"Story {n}", slug=f"story-{n}", is_published=True)
        self.user = User.objects.create_user(email="n@b.com", password="x", display_name="Nell")

    def _assert_four_row_deck(self, html):
        self.assertIn("data-news-deck", html)
        self.assertIn('data-turns="2"', html)
        self.assertEqual(html.count("data-news-row="), 4)
        self.assertIn("gt-news-cards.js", html)
        # A visible count alongside the dots (client, 19 Oct 2026: "so I can
        # know how many they are").
        self.assertIn("nd-pagelabel", html)
        # The long list below the deck is gone: twelve cards up, the rest
        # behind the dots.
        self.assertNotIn("nd-list", html)

    def test_a_member_gets_the_four_row_deck(self):
        self.client.force_login(self.user)
        self._assert_four_row_deck(self.client.get(reverse("news_index")).content.decode())

    def test_a_visitor_gets_the_same_deck_with_the_filter_centred(self):
        html = self.client.get(reverse("news_index")).content.decode()
        self._assert_four_row_deck(html)
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

    def test_the_news_page_deck_is_four_rows_of_three(self):
        deck = deal_news_deck(list(range(1, 15)), rows=NEWS_PAGE_ROWS)
        self.assertEqual(deck["news_turns"], 2)
        self.assertEqual([len(r) for r in deck["news_rows"]], [3, 3, 3, 3])

    def test_six_or_fewer_is_one_turn_and_does_not_rotate(self):
        deck = deal_news_deck([1, 2, 3, 4])
        self.assertEqual(deck["news_turns"], 1)
        self.assertEqual(len(deck["news_rows"]), 2)
        self.assertTrue(deck["news_any"])

    def test_no_stories_is_no_deck(self):
        deck = deal_news_deck([])
        self.assertFalse(deck["news_any"])
        self.assertEqual(deck["news_rows"], [[]])
