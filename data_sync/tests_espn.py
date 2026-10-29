"""The ESPN reader (data_sync.scrapers.espn), offline — a canned scoreboard
in place of the network."""
from datetime import date, datetime, timezone as dt_tz
from unittest import mock

from django.test import TestCase

from data_sync.scrapers import espn
from data_sync.services import EspnSyncService, competition_for_series, get_sync_service

EVENT = {
    "id": "740001", "date": "2026-10-31T12:30Z",
    "competitions": [{
        "venue": {"fullName": "Stamford Bridge", "address": {"city": "London"}},
        "status": {"type": {"state": "post", "name": "STATUS_FULL_TIME", "shortDetail": "FT"}},
        "competitors": [
            {"homeAway": "home", "score": "1", "team": {"id": "363", "displayName": "Chelsea", "logo": "https://a.espncdn.com/chelsea.png"}},
            {"homeAway": "away", "score": "1", "team": {"id": "360", "displayName": "Manchester United", "logo": "https://a.espncdn.com/manu.png"}},
        ],
    }],
}


class EspnReaderTests(TestCase):
    def test_an_event_becomes_a_row(self):
        row = espn.EspnScraper()._row(EVENT)
        self.assertEqual(row["external_id"], "espn:740001")
        self.assertEqual((row["home_name"], row["away_name"]), ("Chelsea", "Manchester United"))
        self.assertEqual((row["home_score"], row["away_score"]), (1, 1))
        self.assertEqual(row["status"], "complete")
        self.assertEqual(row["venue_city"], "London")
        self.assertTrue(row["home_logo"].endswith("chelsea.png"))

    def test_rounds_are_calendar_weeks(self):
        self.assertEqual(espn.week_start(2026), date(2025, 12, 29))
        kick = datetime(2026, 1, 1, 3, 0, tzinfo=dt_tz.utc)
        self.assertEqual(espn.round_of(kick, 2026), 1)
        first, last = espn.week_range(2026, 2)
        self.assertEqual((first, last), (date(2026, 1, 5), date(2026, 1, 11)))

    def test_the_new_leagues_route_to_espn(self):
        self.assertEqual(competition_for_series("English Premier League"), "English Premier League")
        self.assertIsInstance(get_sync_service("NBA"), EspnSyncService)
        self.assertEqual(competition_for_series("AFLW"), "AFL")          # unchanged
        self.assertIsNone(competition_for_series("PWHL"))                 # no free feed

    def test_fixtures_for_a_week_read_that_month(self):
        s = espn.EspnScraper()
        with mock.patch.object(s, "_get", return_value={"events": [EVENT]}) as got:
            rows = s.fixtures(series="English Premier League", season=2026,
                              round_number=espn.round_of(datetime(2026, 10, 31, 12, 30, tzinfo=dt_tz.utc), 2026))
        self.assertEqual(len(rows), 1)                 # the week spans two months; one game
        urls = [c[0][0] for c in got.call_args_list]
        self.assertTrue(any("dates=202610" in u for u in urls))
