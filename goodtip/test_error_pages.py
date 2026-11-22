"""GoodTip's own error pages (22 Nov 2026), instead of Django's bare ones."""
from django.template.loader import render_to_string
from django.test import Client, SimpleTestCase, TestCase, override_settings


class ErrorPageTemplateTests(SimpleTestCase):
    def test_each_renders_with_no_context_and_a_way_home(self):
        # 500.html is rendered with no context at all, so none may need one.
        for code in ("403", "403_csrf", "404", "500"):
            html = render_to_string(f"{code}.html")
            self.assertIn('href="/"', html, code)
            self.assertIn("GOOD", html, code)


@override_settings(DEBUG=False)
class NotFoundPageTests(TestCase):
    def test_a_wrong_address_gets_goodtips_page(self):
        resp = Client().get("/no-such-page-anywhere/")
        self.assertEqual(resp.status_code, 404)
        self.assertIn("went out on the full", resp.content.decode())
