"""GoodTip's fixture library (data_sync.library) and the org Sports page —
offline: the library is filled from canned ESPN rows, and nothing may touch
the network while an organisation is seeded from it."""
from datetime import timedelta
from unittest import mock

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from catalog.models import Competition, Season, Series
from data_sync.library import seed_org
from data_sync.prewarm import fixture_cache_put
from data_sync.scrapers import espn
from orgs.models import OrgMember, Organisation
from tipping.models import Match


def _row(i, kickoff, *, done=False):
    return {
        "league_season": kickoff.year, "external_id": f"espn:{i}",
        "home_name": f"Home {i}", "away_name": f"Away {i}",
        "home_external_id": f"espn:h{i}", "away_external_id": f"espn:a{i}",
        "home_logo": "", "away_logo": "",
        "home_score": 2 if done else None, "away_score": 1 if done else None,
        "kickoff_at": kickoff, "venue": "Ground", "venue_city": "City",
        "status": "complete" if done else "scheduled", "clock": "", "period": "",
    }


class LibrarySeedTests(TestCase):
    def setUp(self):
        self.season, _ = Season.objects.get_or_create(year=timezone.localdate().year)
        self.comp, _ = Competition.objects.get_or_create(
            slug="epl", season=self.season,
            defaults={"name": "English Premier League", "sport": Series.objects.get(slug="epl").sport})
        self.comp.series.add(Series.objects.get(slug="epl"))
        now = timezone.now()
        soon, past = now + timedelta(days=10), now - timedelta(days=10)   # two different weeks
        year = self.season.year
        fixture_cache_put("ESPN", "English Premier League", year, espn.round_of(soon, year), [_row(1, soon)])
        fixture_cache_put("ESPN", "English Premier League", year, espn.round_of(past, year), [_row(2, past, done=True)])

    def test_an_organisation_is_built_from_the_library_without_the_network(self):
        org = Organisation.objects.create(name="Tap Co", season=self.season)
        org.competitions.add(self.comp)
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("network used")):
            seed_org(org)
        games = Match.objects.filter(round__org=org)
        self.assertEqual(games.count(), 2)
        played = games.get(external_id="espn:2")
        self.assertEqual(played.result, "home")
        # It reached GoodTip after kick-off, so nobody is defaulted onto it.
        self.assertFalse(played.tips.exists())


class LadderRulesTests(TestCase):
    def test_every_espn_league_has_ladder_rules(self):
        from data_sync.ladder import RULES

        for name in espn.LEAGUES:
            self.assertIn(name.upper(), RULES, name)
        self.assertEqual(RULES["ENGLISH PREMIER LEAGUE"].win, 3)


class OrgSportsPageTests(TestCase):
    def setUp(self):
        self.season, _ = Season.objects.get_or_create(year=2095)
        self.org = Organisation.objects.create(name="Admin Co", season=self.season)
        self.user = User.objects.create_user(email="adm@example.com", password="x", display_name="Ad")
        OrgMember.objects.create(user=self.user, org=self.org, role=OrgMember.ROLE_MANAGER)
        self.org.created_by = self.user if hasattr(self.org, "created_by") else None
        if hasattr(self.org, "created_by"):
            self.org.save(update_fields=["created_by"])
        self.client.force_login(self.user)

    def test_only_its_admin_reaches_it(self):
        other = User.objects.create_user(email="m@example.com", password="x", display_name="M")
        OrgMember.objects.create(user=other, org=self.org)
        self.client.force_login(other)
        r = self.client.get(reverse("orgs:sports", args=[self.org.pk]))
        self.assertEqual(r.status_code, 403)
