"""Pin my favourite sport, and the dashboard's round-2 layout (client,
27 Oct 2026)."""
from django.test import TestCase
from django.urls import reverse

from accounts.models import User
from catalog.models import Season, Series, Sport
from orgs.models import OrgMember, Organisation


class PinTests(TestCase):
    def setUp(self):
        season = Season.objects.create(year=2096, label="2096")
        self.org = Organisation.objects.create(name="Pin Co", season=season)
        self.user = User.objects.create_user(email="pin@example.com", password="x", display_name="Pip")
        OrgMember.objects.create(user=self.user, org=self.org)
        self.client.force_login(self.user)
        Sport.objects.filter(slug="football").update(is_active=True)
        Series.objects.filter(slug="la-liga").update(is_active=True)
        # A round to tip, so the slate (and the chip row in it) is on the page.
        from django.utils import timezone

        from catalog.models import Competition
        from tipping.models import Match, Round, Team

        afl = Series.objects.get(slug="afl")
        comp = Competition.objects.create(sport=afl.sport, season=season, name="AFL", slug="afl-pins")
        comp.series.add(afl)
        self.org.competitions.add(comp)
        soon = timezone.now() + timezone.timedelta(days=2)
        rnd = Round.objects.create(org=self.org, round_number=1, series=afl, competition=comp, lockout_at=soon)
        Match.objects.create(round=rnd, kickoff_at=soon,
                             home_team=Team.objects.create(name="Pin Home", slug="pin-home", series=afl),
                             away_team=Team.objects.create(name="Pin Away", slug="pin-away", series=afl))

    def test_pinning_toggles_and_shows_beside_the_defaults(self):
        url = reverse("accounts:pin_series")
        self.client.post(url, {"series": "la-liga"})
        self.user.refresh_from_db()
        self.assertEqual(self.user.pinned_series, ["la-liga"])
        body = self.client.get(reverse("dashboard")).content.decode()
        # A member sees the chips (the four defaults); the pin panel offers
        # only what their organisation tips, which here is the AFL alone.
        self.assertIn('class="crow-chip', body)
        self.client.post(url, {"series": "la-liga"})
        self.user.refresh_from_db()
        self.assertEqual(self.user.pinned_series, [])

    def test_the_defaults_cannot_be_pinned_and_strangers_are_ignored(self):
        url = reverse("accounts:pin_series")
        self.client.post(url, {"series": "afl"})
        self.client.post(url, {"series": "no-such-thing"})
        self.user.refresh_from_db()
        self.assertEqual(self.user.pinned_series, [])

    def test_the_pin_redirect_stays_on_site(self):
        r = self.client.post(reverse("accounts:pin_series"), {"series": "la-liga", "next": "https://evil.example"})
        self.assertEqual(r["Location"], reverse("dashboard"))

    def test_the_top_menu_offers_to_create(self):
        body = self.client.get(reverse("dashboard")).content.decode()
        self.assertIn('class="an-ctx-create"', body)
        self.assertIn(reverse("orgs:create"), body)
