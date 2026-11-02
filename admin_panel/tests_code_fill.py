"""A story's card wears its competition, or runs through all of them."""
from django.test import SimpleTestCase

from admin_panel.models import NewsPost
from catalog.models import Series


class CodeFillTests(SimpleTestCase):
    def test_one_competition_is_its_colour(self):
        self.assertEqual(NewsPost(tags=["NRL"]).code_fill, Series.COLOURS["nrl"])

    def test_several_run_into_one_another(self):
        fill = NewsPost(tags=["NRL", "AFL"]).code_fill
        self.assertIn(Series.COLOURS["nrl"], fill)
        self.assertIn(Series.COLOURS["afl"], fill)
        self.assertTrue(fill.startswith("linear-gradient"))

    def test_news_and_blog_are_not_competitions(self):
        self.assertEqual(NewsPost(tags=["NRL", "BLOG"]).code_fill, Series.COLOURS["nrl"])
