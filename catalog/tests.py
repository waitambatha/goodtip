from django.test import SimpleTestCase, TestCase

from .logos import JUNK_NAME, candidate_icon_urls, derive_website
from .models import Charity


class CharityCardHelperTests(SimpleTestCase):
    """The bits the card draws itself from."""

    def initials(self, name):
        return Charity(name=name).initials

    def test_noise_words_are_not_the_initials(self):
        """"The Smith Family" is SF to everyone who knows it, not TS."""
        self.assertEqual(self.initials("The Smith Family"), "SF")
        self.assertEqual(self.initials("Royal Flying Doctor Service of Australia"), "RF")

    def test_a_one_word_name_gives_two_letters(self):
        """One capital alone looks like a mistake at tile size."""
        self.assertEqual(self.initials("Lifeline"), "LI")

    def test_a_name_that_is_all_noise_still_gets_initials(self):
        self.assertEqual(self.initials("The Australian"), "TA")

    def test_the_tile_colour_is_stable_for_a_name(self):
        """Recognition, not decoration — the same charity must be the same
        colour on the picker, the ballot and the result."""
        self.assertEqual(Charity(name="Lifeline").tile_hue, Charity(name="Lifeline").tile_hue)
        self.assertNotEqual(
            Charity(name="Lifeline").tile_hue, Charity(name="Beyond Blue").tile_hue,
        )

    def test_the_website_label_drops_the_dressing(self):
        label = Charity(name="x", website="https://www.beyondblue.org.au/").website_label
        self.assertEqual(label, "beyondblue.org.au")

    def test_no_website_is_an_empty_label_not_a_crash(self):
        self.assertEqual(Charity(name="x").website_label, "")


class CharityLogoSourceTests(SimpleTestCase):
    """Picking WHICH image off a charity's page. No network here."""

    def test_an_apple_touch_icon_beats_an_og_image(self):
        html = (
            '<link rel="icon" href="/fav.png">'
            '<meta property="og:image" content="/og.png">'
            '<link rel="apple-touch-icon" href="/touch.png">'
        )
        urls = candidate_icon_urls(html, "https://x.test/")
        self.assertEqual(urls[0], "https://x.test/touch.png")

    def test_favicon_is_always_a_last_resort(self):
        urls = candidate_icon_urls("", "https://x.test/")
        self.assertEqual(urls, ["https://x.test/favicon.ico"])

    def test_a_placeholder_is_not_a_logo(self):
        """headspace served "placeholder-image.jpg" as its og:image, which
        passed every size check and would have been published as their logo."""
        html = '<meta property="og:image" content="/assets/placeholder-image.jpg">'
        urls = candidate_icon_urls(html, "https://x.test/")
        self.assertNotIn("https://x.test/assets/placeholder-image.jpg", urls)

    def test_social_banners_are_skipped_too(self):
        for path in ("/og-image.png", "/social-share.png", "/default-cover.jpg"):
            self.assertTrue(JUNK_NAME.search(path), path)

    def test_data_uris_are_ignored(self):
        html = '<link rel="icon" href="data:image/png;base64,AAAA">'
        self.assertEqual(
            candidate_icon_urls(html, "https://x.test/"), ["https://x.test/favicon.ico"],
        )

    def test_a_website_is_guessed_from_the_name(self):
        self.assertEqual(derive_website("Beyond Blue"), "https://beyondblue.org.au")
        self.assertEqual(derive_website("The Smith Family"), "https://thesmithfamily.org.au")

    def test_a_nameless_charity_guesses_nothing(self):
        self.assertEqual(derive_website("!!!"), "")


class CharityLogoFieldTests(TestCase):
    def test_a_charity_without_a_logo_is_a_normal_state(self):
        """The initials tile is a designed state, not a gap — several large
        charities sit behind bot protection and publish nothing we can fetch."""
        c = Charity.objects.create(name="Unreachable Trust", slug="unreachable")
        self.assertFalse(c.logo)
        self.assertEqual(c.initials, "UT")
