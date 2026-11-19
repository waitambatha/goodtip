"""FINAL WHISTLE — GoodTip's AI game stories (client, 11 Nov 2026): facts out
of a match summary, the game written up in our own words, ONE story per real
game for everybody, and every organisation that has the competition seeing it
on its Wall with its own conversation underneath."""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from catalog.models import Competition, Season, Series, Sport
from matchreader.models import HistoricalMatch
from orgs.models import OrgMember, Organisation, WallPost, WallReply
from tipping.match_reports import Game, analysis, generate_match_stories, parse_summary, story, write_story
from tipping.models import MatchStory, Team

User = get_user_model()

SUMMARY = {
    "keyEvents": [
        {"type": {"text": "Kickoff"}, "clock": {"displayValue": ""}},
        {"type": {"text": "Goal"}, "clock": {"displayValue": "15'"},
         "team": {"displayName": "Brighton & Hove Albion"}, "participants": [{"athlete": {"displayName": "Diego Gómez"}}]},
        {"type": {"text": "Red Card"}, "clock": {"displayValue": "23'"},
         "team": {"displayName": "Brentford"}, "participants": [{"athlete": {"displayName": "Igor Thiago"}}]},
        {"type": {"text": "Penalty - Scored"}, "clock": {"displayValue": "42'"},
         "team": {"displayName": "Brighton & Hove Albion"}, "participants": [{"athlete": {"displayName": "Pascal Groß"}}]},
    ],
    "boxscore": {"teams": [
        {"team": {"displayName": "Brighton & Hove Albion"}, "statistics": [
            {"name": "possessionPct", "displayValue": "75.3"}, {"name": "totalShots", "displayValue": "20"},
            {"name": "shotsOnTarget", "displayValue": "7"}, {"name": "saves", "displayValue": "0"}]},
        {"team": {"displayName": "Brentford"}, "statistics": [
            {"name": "possessionPct", "displayValue": "24.7"}, {"name": "totalShots", "displayValue": "5"},
            {"name": "shotsOnTarget", "displayValue": "1"}, {"name": "saves", "displayValue": "4"}]},
    ]},
    "header": {"competitions": [{"competitors": [
        {"team": {"displayName": "Brighton & Hove Albion"}, "linescores": [{"displayValue": "2"}, {"displayValue": "0"}]},
        {"team": {"displayName": "Brentford"}, "linescores": [{"displayValue": "0"}, {"displayValue": "0"}]},
    ]}]},
    "gameInfo": {"venue": {"fullName": "American Express Stadium"}, "attendance": 31629},
}


class FinalWhistleTests(TestCase):
    def setUp(self):
        sport = Sport.objects.create(name="FW Football", slug="fw-football")
        self.series = Series.objects.filter(name="English Premier League").first() or \
            Series.objects.create(sport=sport, name="English Premier League", slug="fw-epl")
        home = Team.objects.create(name="Brighton & Hove Albion", slug="fw-bri", series=self.series)
        away = Team.objects.create(name="Brentford", slug="fw-bre", series=self.series)
        self.hm = HistoricalMatch.objects.create(
            series=self.series, season=2026, round_number=11, external_id="espn:1",
            home_team=home, away_team=away, kickoff_at=timezone.now() - timedelta(hours=5),
            home_score=2, away_score=0,
        )

    def test_the_facts_come_out_of_the_summary(self):
        f = parse_summary(SUMMARY, Game(self.hm), "soccer")
        self.assertEqual([(e["minute"], e["kind"], e["player"], e["side"]) for e in f["events"]], [
            ("15'", "goal", "Diego Gómez", "home"), ("23'", "red", "Igor Thiago", "away"),
            ("42'", "pen", "Pascal Groß", "home")])
        self.assertEqual(f["periods"], {"home": [2, 0], "away": [0, 0]})
        self.assertEqual((f["venue"], f["attendance"]), ("American Express Stadium", 31629))

    def story_with_facts(self):
        s = MatchStory(game=self.hm, published_at=timezone.now() - timedelta(hours=2),
                       **parse_summary(SUMMARY, Game(self.hm), "soccer"))
        s.save()
        return write_story(self.hm, fetch=False)

    def test_the_game_is_written_up_in_sections(self):
        s = self.story_with_facts()
        self.assertTrue(s.headline.startswith("Brighton & Hove Albion beat ten-man Brentford 2–0."))
        sections = dict(s.sections)
        self.assertIn("2–0 at the break", sections["First half"])
        self.assertIn("20 shots (7 on target)", sections["Attack"])
        self.assertIn("Down to ten from 23'", sections["Why Brentford lost"])
        self.assertIn("red card", s.question)

    def test_one_story_per_game_however_often_the_job_runs(self):
        generate_match_stories(days=1, fetch=False)
        generate_match_stories(days=1, fetch=False)
        self.assertEqual(MatchStory.objects.filter(game=self.hm).count(), 1)

    def test_every_organisation_with_the_competition_sees_it_and_talks_on_its_own(self):
        self.story_with_facts()
        season = Season.objects.create(year=2091, label="2091")
        comp = Competition.objects.create(sport=self.series.sport, season=season, name="FW EPL", slug="fw-epl-c")
        comp.series.add(self.series)
        a = Organisation.objects.create(name="Org A", season=season)
        b = Organisation.objects.create(name="Org B", season=season)
        none = Organisation.objects.create(name="Org without it", season=season)
        for o in (a, b):
            o.competitions.add(comp)
        u = User.objects.create_user(email="fw@x.test", password="x", display_name="Fi")
        for o in (a, b, none):
            OrgMember.objects.create(user=u, org=o)
        self.client.force_login(u)
        story = MatchStory.objects.get(game=self.hm)
        for o in (a, b):
            html = self.client.get(reverse("orgs:wall", args=[o.id]) + f"?room={self.series.slug}").content.decode()
            self.assertIn(f'id="story-{story.id}"', html)
            self.assertIn("Match Recap", html)
            self.assertIn("by GoodTip AI", html)
        # A reply lands in Org A's own thread only.
        self.client.post(reverse("orgs:wall_story_reply", args=[a.id, story.id]), {"body": "What a red card"})
        self.assertEqual(WallReply.objects.filter(body="What a red card", post__org=a).count(), 1)
        html_b = self.client.get(reverse("orgs:wall", args=[b.id]) + f"?room={self.series.slug}").content.decode()
        self.assertNotIn("What a red card", html_b)
        # An organisation without the competition cannot reach the story.
        r = self.client.post(reverse("orgs:wall_story_reply", args=[none.id, story.id]), {"body": "x"})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(WallPost.objects.filter(story=story).count(), 1)

    def test_without_facts_it_is_just_the_result(self):
        self.assertEqual(story(Game(self.hm)), "Brighton & Hove Albion beat Brentford 2–0.")
