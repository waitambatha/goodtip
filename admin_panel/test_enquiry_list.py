"""The enquiries inbox, as the Sep 2026 mockup: pill tabs with counts, a
search, a sort, rows as cards and pages of ten."""
from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import User

from .models import Enquiry
from .tests import sign_in_to_admin


class EnquiryListTests(TestCase):
    def setUp(self):
        self.boss = User.objects.create_superuser(
            email="enq-boss@example.com", password="pw", display_name="Boss",
        )
        sign_in_to_admin(self.client, self.boss)
        self.url = reverse("admin:hq_enquiries")
        now = timezone.now()

        def enquiry(name, status=Enquiry.STATUS_NEW, days=0, **kw):
            return Enquiry.objects.create(
                name=name, email=f"{name.lower()}@example.com", message=kw.pop("message", "Hello"),
                status=status, created_at=now - timedelta(days=days), **kw,
            )

        self.ann = enquiry("Ann", days=3, interest="Pricing")
        self.bob = enquiry("Bob", days=1, message="Tell me about CHARITY comps")
        self.cat = enquiry("Cat", status=Enquiry.STATUS_REPLIED, days=2, organisation="Cat Club")

    def names(self, query=""):
        return [e.name for e in self.client.get(self.url + query).context["enquiries"]]

    def test_open_is_the_default_tab_newest_first(self):
        self.assertEqual(self.names(), ["Bob", "Ann"])

    def test_the_tabs_carry_their_counts(self):
        html = self.client.get(self.url).content.decode()
        self.assertIn('Open <span class="hq-n">2</span>', html)
        self.assertIn('Replied <span class="hq-n">1</span>', html)
        self.assertIn('All <span class="hq-n">3</span>', html)

    def test_search_reads_the_message_and_the_organisation(self):
        self.assertEqual(self.names("?show=all&q=charity"), ["Bob"])
        self.assertEqual(self.names("?show=all&q=cat+club"), ["Cat"])
        html = self.client.get(self.url + "?q=nobody").content.decode()
        self.assertIn("Nothing matches", html)

    def test_sort(self):
        self.assertEqual(self.names("?show=all&sort=old"), ["Ann", "Cat", "Bob"])
        self.assertEqual(self.names("?show=all&sort=name"), ["Ann", "Bob", "Cat"])
        # An unknown sort or tab falls back rather than failing.
        self.assertEqual(self.names("?show=bogus&sort=bogus"), ["Bob", "Ann"])

    def test_pages_of_ten_keep_the_search(self):
        for i in range(12):
            Enquiry.objects.create(name=f"Page{i:02}", email=f"p{i}@example.com", message="paged")
        r = self.client.get(self.url + "?show=all&q=paged")
        self.assertEqual(len(r.context["enquiries"]), 10)
        html = r.content.decode()
        self.assertIn("Showing 1 to 10 of 12 enquiries", html)
        self.assertIn("show=all&amp;q=paged&amp;page=2", html)
        r2 = self.client.get(self.url + "?show=all&q=paged&page=2")
        self.assertIn("Showing 11 to 12 of 12 enquiries", r2.content.decode())

    def test_every_row_has_reply_and_its_menu(self):
        html = self.client.get(self.url).content.decode()
        self.assertIn('class="hq-reply"', html)
        self.assertIn("Close without a reply", html)
        self.assertIn(reverse("admin:hq_enquiry_detail", args=[self.ann.id]), html)


class HqAccountMenuTests(TestCase):
    """The drop-down in the HQ bar: who you are, your profile, the site, and
    the way out — which the plain avatar link it replaced did not have."""

    def test_the_menu_is_on_every_hq_page(self):
        boss = User.objects.create_superuser(email="menu@example.com", password="pw", display_name="Menu Boss")
        sign_in_to_admin(self.client, boss)
        for name in ("admin:hq_enquiries", "admin:hq_news", "admin:hq_team"):
            html = self.client.get(reverse(name)).content.decode()
            self.assertIn('class="gts-acct"', html)
            self.assertIn(reverse("admin:logout"), html)
            self.assertIn(reverse("profile"), html)
            self.assertIn("View site", html)
            self.assertIn("menu@example.com", html)
            self.assertIn('class="gts-hero"', html)
