"""The home page's "Live this season" board.

It replaced a made-up ladder and an illustrative dollar total, so the tests
that matter are the ones that say every figure on it is read from results, and
that the invented ones are gone.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone as dt_timezone

from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from catalog.models import Season, Series
from data_sync.ladder import rebuild_ladder
from data_sync.public_board import board
from matchreader.models import HistoricalMatch
from tipping.models import LadderEntry, Team


class BoardTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.series = Series.objects.get(name="NRL")
        cls.season = Season.objects.get(year=2026)
        cls.teams = [
            Team.objects.create(name=n, slug=s, series=cls.series)
            for n, s in (("Alphas", "alphas"), ("Betas", "betas"), ("Gammas", "gammas"), ("Deltas", "deltas"))
        ]
        # Alphas beat everyone; Betas and Gammas split; Deltas lose all three.
        for rn, home, away, hs, as_ in (
            (1, 0, 3, 30, 6),
            (2, 1, 0, 12, 26),
            (3, 0, 2, 40, 10),
            (3, 1, 3, 20, 18),
        ):
            HistoricalMatch.objects.create(
                series=cls.series, season=2026, round_number=rn, stage="regular",
                external_id=f"b:{rn}:{home}:{away}",
                home_team=cls.teams[home], away_team=cls.teams[away],
                kickoff_at=datetime(2026, 3, 1, tzinfo=dt_timezone.utc) + timedelta(days=7 * rn, hours=home),
                home_score=hs, away_score=as_,
            )
        rebuild_ladder(series=cls.series, season=cls.season)

    def setUp(self):
        cache.clear()

    def nrl(self):
        return next(c for c in board()["codes"] if c["code"] == "NRL")

    def test_the_ladder_is_the_stored_ladder(self):
        nrl = self.nrl()
        stored = list(
            LadderEntry.objects.filter(series=self.series, season=self.season)
            .order_by("rank").values_list("team__name", "points")
        )
        self.assertEqual([(r["name"], r["points"]) for r in nrl["ladder"]], stored)
        self.assertEqual(nrl["ladder"][0]["name"], "Alphas")
        self.assertEqual(nrl["season"], 2026)
        self.assertEqual(nrl["round"], 3)

    def test_form_and_streak_come_from_results_in_kickoff_order(self):
        alphas = self.nrl()["ladder"][0]
        self.assertEqual(alphas["form"], "WWW")
        self.assertEqual(alphas["streak"], 3)
        deltas = next(r for r in self.nrl()["ladder"] if r["name"] == "Deltas")
        self.assertEqual(deltas["form"], "LL")
        self.assertEqual(deltas["streak"], -2)

    def test_competition_numbers_are_tallied_from_the_games(self):
        st = self.nrl()["stats"]
        self.assertEqual(st["games"], 4)
        self.assertEqual(st["points"], 30 + 6 + 12 + 26 + 40 + 10 + 20 + 18)
        self.assertEqual(st["avg_game"], 40.5)
        self.assertEqual(st["biggest_win"], {"winner": "Alphas", "loser": "Gammas", "margin": 30, "round": 3})
        self.assertEqual(st["highest_score"]["team"], "Alphas")
        self.assertEqual(st["highest_score"]["score"], 40)
        # Home sides won three of the four (Betas lost at home to Alphas in round 2).
        self.assertEqual(st["home_win_pct"], 75)

    def test_the_chart_series_are_tallied_from_the_games(self):
        nrl = self.nrl()
        # Round 3 had two games: 40+10 and 20+18 = 88 over two games.
        self.assertEqual(nrl["rounds"], [
            {"round": 1, "avg": 36.0}, {"round": 2, "avg": 38.0}, {"round": 3, "avg": 44.0},
        ])
        self.assertEqual(nrl["venue"], {"home": 3, "away": 1, "draws": 0})
        # Margins 24, 14, 30 and 2, in bars six points wide.
        self.assertEqual(nrl["margins"], {"width": 6, "bins": [1, 0, 1, 0, 1, 1, 0]})
        top = nrl["ladder"][0]
        self.assertEqual((top["avg_for"], top["avg_against"]), (32.0, 9.3))

    def test_nrl_shows_a_points_difference_and_afl_would_show_a_percentage(self):
        self.assertFalse(self.nrl()["by_percentage"])
        self.assertEqual(self.nrl()["ladder"][0]["diff"], "+68")

    def test_a_code_with_no_ladder_is_left_out(self):
        self.assertEqual([c["code"] for c in board()["codes"]], ["NRL"])

    def test_the_result_is_cached(self):
        board()
        with self.assertNumQueries(0):
            board()

    def test_the_json_endpoint_serves_the_same_payload(self):
        r = self.client.get(reverse("ladder_board"))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["codes"][0]["code"], "NRL")


class HomePageBoardTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_the_made_up_ladder_and_dollar_total_are_gone(self):
        html = self.client.get("/").content.decode()
        for invented in ("Sharon from Accounts", "Group ladder", "8,420", "Raised so far", "Goal $12,500"):
            self.assertNotIn(invented, html)

    def test_the_section_is_left_out_when_there_are_no_ladders_yet(self):
        html = self.client.get("/").content.decode()
        self.assertNotIn('id="board"', html)

    def test_the_section_draws_real_teams_with_the_data_the_script_needs(self):
        series = Series.objects.get(name="NRL")
        season = Season.objects.get(year=2026)
        a = Team.objects.create(name="Alphas", slug="alphas", series=series)
        b = Team.objects.create(name="Betas", slug="betas", series=series)
        HistoricalMatch.objects.create(
            series=series, season=2026, round_number=1, stage="regular", external_id="h:1",
            home_team=a, away_team=b, kickoff_at=datetime(2026, 3, 8, tzinfo=dt_timezone.utc),
            home_score=20, away_score=10,
        )
        rebuild_ladder(series=series, season=season)
        cache.clear()
        html = self.client.get("/").content.decode()
        self.assertIn('id="board"', html)
        self.assertIn("Alphas", html)
        self.assertIn('id="board-data"', html)
        self.assertIn("js/gt-board.js", html)
