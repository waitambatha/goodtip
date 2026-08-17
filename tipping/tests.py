from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from catalog.models import Season, Series, Sport
from orgs.models import OrgMember, Organisation
from tipping.models import Match, Round, Team, Tip
from tipping.services import (
    leaderboard_for_family,
    leaderboard_for_org,
    record_match_result,
    user_org_stats,
)


class WeightedScoringTests(TestCase):
    """Ambrose Hierarchy brief, slide 6: regular=1, finals=2, Origin=4."""

    def setUp(self):
        self.sport = Sport.objects.create(name="Test Footy", slug="test-footy")
        self.series = Series.objects.create(sport=self.sport, name="Test Series", slug="test-series")
        self.season = Season.objects.create(year=2099, label="2099")
        self.org = Organisation.objects.create(name="Test League", season=self.season)
        self.user = User.objects.create_user(email="a@b.com", password="x", display_name="Ada")
        OrgMember.objects.create(user=self.user, org=self.org)
        self.home = Team.objects.create(name="Broncos", slug="broncos", series=self.series)
        self.away = Team.objects.create(name="Storm", slug="storm", series=self.series)

    def _round(self, number, stage):
        return Round.objects.create(
            org=self.org, round_number=number, series=self.series,
            stage=stage, lockout_at=timezone.now(),
        )

    def _correct_tip(self, rnd):
        match = Match.objects.create(
            round=rnd, home_team=self.home, away_team=self.away,
            kickoff_at=timezone.now(),
        )
        Tip.objects.create(user=self.user, match=match, org=self.org, selection="home")
        record_match_result(match, 30, 10)  # home wins → the tip is correct
        return match

    def test_points_per_correct_by_stage(self):
        self.assertEqual(self._round(1, Round.STAGE_REGULAR).points_per_correct, 1)
        self.assertEqual(self._round(2, Round.STAGE_FINALS).points_per_correct, 2)
        self.assertEqual(self._round(3, Round.STAGE_ORIGIN).points_per_correct, 4)

    def test_correct_tip_awards_weighted_points(self):
        self._correct_tip(self._round(1, Round.STAGE_REGULAR))
        self._correct_tip(self._round(2, Round.STAGE_FINALS))
        self._correct_tip(self._round(3, Round.STAGE_ORIGIN))
        # 1 + 2 + 4 = 7
        self.assertEqual(user_org_stats(self.user, self.org)["points"], 7)
        self.assertEqual(user_org_stats(self.user, self.org)["tips_correct"], 3)

    def test_wrong_tip_awards_zero(self):
        rnd = self._round(1, Round.STAGE_ORIGIN)
        match = Match.objects.create(
            round=rnd, home_team=self.home, away_team=self.away, kickoff_at=timezone.now(),
        )
        Tip.objects.create(user=self.user, match=match, org=self.org, selection="away")
        record_match_result(match, 30, 10)  # home wins → away tip is wrong
        tip = Tip.objects.get(user=self.user, match=match)
        self.assertFalse(tip.is_correct)
        self.assertEqual(tip.points_awarded, 0)

    def test_leaderboard_sums_weighted_points(self):
        self._correct_tip(self._round(2, Round.STAGE_FINALS))
        row = next(r for r in leaderboard_for_org(self.org) if r.id == self.user.id)
        self.assertEqual(row.points, 2)
        self.assertEqual(row.tips_correct, 1)


class FamilyLeaderboardTests(TestCase):
    """Org-structure note §8: one national competition; the national board
    ranks every member across the parent and all children, the local board
    filters to one org."""

    def setUp(self):
        self.sport = Sport.objects.create(name="Test Footy", slug="test-footy")
        self.series = Series.objects.create(sport=self.sport, name="Test Series", slug="test-series")
        self.season = Season.objects.create(year=2099, label="2099")
        self.parent = Organisation.objects.create(name="National Tiles", season=self.season)
        self.mitcham = Organisation.objects.create(
            name="National Tiles Mitcham", season=self.season, parent=self.parent,
        )
        self.preston = Organisation.objects.create(
            name="National Tiles Preston", season=self.season, parent=self.parent,
        )
        self.home = Team.objects.create(name="Broncos", slug="broncos", series=self.series)
        self.away = Team.objects.create(name="Storm", slug="storm", series=self.series)
        self.ada = self._member("ada@x.com", "Ada", self.mitcham)
        self.bob = self._member("bob@x.com", "Bob", self.preston)
        self.cec = self._member("cec@x.com", "Cec", self.parent)

    def _member(self, email, name, org):
        user = User.objects.create_user(email=email, password="x", display_name=name)
        OrgMember.objects.create(user=user, org=org)
        return user

    def _round_for(self, org, number=1):
        return Round.objects.create(
            org=org, round_number=number, series=self.series,
            stage=Round.STAGE_REGULAR, lockout_at=timezone.now(),
        )

    def _graded_tip(self, user, org, rnd, *, correct):
        match = Match.objects.create(
            round=rnd, home_team=self.home, away_team=self.away,
            kickoff_at=timezone.now(),
        )
        Tip.objects.create(
            user=user, match=match, org=org,
            selection="home" if correct else "away",
        )
        record_match_result(match, 30, 10)  # home wins
        return match

    def test_national_board_ranks_whole_family_from_any_org(self):
        self._graded_tip(self.ada, self.mitcham, self._round_for(self.mitcham), correct=True)
        self._graded_tip(self.bob, self.preston, self._round_for(self.preston), correct=True)
        self._graded_tip(self.cec, self.parent, self._round_for(self.parent), correct=False)
        board = list(leaderboard_for_family(self.mitcham))
        # All three tippers, across parent and both siblings (§7-style scope).
        self.assertEqual({u.display_name for u in board}, {"Ada", "Bob", "Cec"})
        points = {u.display_name: u.points for u in board}
        self.assertEqual(points, {"Ada": 1, "Bob": 1, "Cec": 0})

    def test_local_board_stays_filtered_to_own_org(self):
        self._graded_tip(self.ada, self.mitcham, self._round_for(self.mitcham), correct=True)
        self._graded_tip(self.bob, self.preston, self._round_for(self.preston), correct=True)
        board = list(leaderboard_for_org(self.mitcham))
        self.assertEqual([u.display_name for u in board], ["Ada"])

    def test_family_round_filter_aligns_by_round_number(self):
        mitcham_r1 = self._round_for(self.mitcham, number=1)
        preston_r1 = self._round_for(self.preston, number=1)
        preston_r2 = self._round_for(self.preston, number=2)
        self._graded_tip(self.ada, self.mitcham, mitcham_r1, correct=True)
        self._graded_tip(self.bob, self.preston, preston_r1, correct=True)
        self._graded_tip(self.bob, self.preston, preston_r2, correct=True)
        # Filtering by Mitcham's round 1 id must count Preston's round 1 too.
        board = {u.display_name: u.points for u in leaderboard_for_family(self.mitcham, round_id=mitcham_r1.id)}
        self.assertEqual(board["Ada"], 1)
        self.assertEqual(board["Bob"], 1)  # r2 tip excluded

    def test_leaderboard_page_scopes(self):
        self._graded_tip(self.ada, self.mitcham, self._round_for(self.mitcham), correct=True)
        self._graded_tip(self.bob, self.preston, self._round_for(self.preston), correct=True)
        self.client.force_login(self.ada)
        local = self.client.get(f"/org/{self.mitcham.id}/leaderboard/")
        self.assertContains(local, "Ada")
        self.assertNotContains(local, "Bob")
        national = self.client.get(f"/org/{self.mitcham.id}/leaderboard/?scope=national")
        self.assertContains(national, "Ada")
        self.assertContains(national, "Bob")
        self.assertContains(national, "ranked together")

    def test_standalone_org_has_no_scope_tabs(self):
        loner = Organisation.objects.create(name="Loner", season=self.season)
        solo = self._member("solo@x.com", "Solo", loner)
        self.client.force_login(solo)
        resp = self.client.get(f"/org/{loner.id}/leaderboard/")
        self.assertNotContains(resp, "scope=national")


class MatchStatePollTests(TestCase):
    """The in-play refresh endpoint the live badge polls.

    Before this existed, scores changed in the database every two minutes and
    never reached a page somebody already had open — the "live" badge pulsed
    against a number that only moved on reload.
    """

    def setUp(self):
        self.sport = Sport.objects.create(name="Poll Footy", slug="poll-footy")
        self.series = Series.objects.create(sport=self.sport, name="Poll Series", slug="poll-series")
        self.season = Season.objects.create(year=2099, label="2099")
        self.org = Organisation.objects.create(name="Poll League", season=self.season)
        self.user = User.objects.create_user(email="p@b.com", password="x", display_name="Pat")
        OrgMember.objects.create(user=self.user, org=self.org)
        self.round = Round.objects.create(
            org=self.org, round_number=1, series=self.series,
            lockout_at=timezone.now() - timedelta(hours=1),
        )
        self.home = Team.objects.create(name="Reds", slug="reds", series=self.series)
        self.away = Team.objects.create(name="Blues", slug="blues", series=self.series)

    def _match(self, **kw):
        return Match.objects.create(
            round=self.round, home_team=self.home, away_team=self.away,
            kickoff_at=timezone.now() - timedelta(hours=1), **kw,
        )

    def test_a_live_match_returns_its_score_and_keeps_polling(self):
        match = self._match(
            status=Match.STATUS_LIVE, period="Q3", clock="12:45",
            home_score=54, away_score=41,
        )
        self.client.force_login(self.user)
        r = self.client.get(reverse("tipping:match_state", args=[match.id]))
        self.assertEqual(r.status_code, 200)
        body = r.content.decode()
        self.assertIn("54–41", body)
        self.assertIn("Q3 12:45", body)
        # Still in play, so the fragment must carry the trigger that fetches
        # it again — otherwise the first poll is also the last.
        self.assertIn("every 30s", body)

    def test_a_finished_match_stops_polling(self):
        """The trigger has to disappear, or a completed game keeps costing a
        request every thirty seconds for the rest of the day."""
        match = self._match(
            status=Match.STATUS_COMPLETE, period="Full Time",
            home_score=80, away_score=60, result="home",
        )
        self.client.force_login(self.user)
        body = self.client.get(reverse("tipping:match_state", args=[match.id])).content.decode()
        self.assertNotIn("every 30s", body)
        self.assertIn("80–60", body)

    def test_it_requires_a_login(self):
        match = self._match(status=Match.STATUS_LIVE)
        r = self.client.get(reverse("tipping:match_state", args=[match.id]))
        self.assertEqual(r.status_code, 302)


class AddendumScoringTests(TestCase):
    """Scoring & Tiebreaker Addendum rev 3, §1 and §2."""

    def setUp(self):
        from catalog.models import Season, Series, Sport
        from orgs.models import OrgMember, Organisation
        sport = Sport.objects.create(name="Add Footy", slug="add-footy")
        self.series = Series.objects.create(sport=sport, name="Add Series", slug="add-series")
        season = Season.objects.create(year=2099, label="2099")
        self.org = Organisation.objects.create(name="Add League", season=season)
        self.user = User.objects.create_user(email="a1@x.com", password="x", display_name="A1")
        self.other = User.objects.create_user(email="a2@x.com", password="x", display_name="A2")
        OrgMember.objects.create(user=self.user, org=self.org)
        OrgMember.objects.create(user=self.other, org=self.org)
        self.home = Team.objects.create(name="H", slug="add-h", series=self.series)
        self.away = Team.objects.create(name="A", slug="add-a", series=self.series)

    def _match(self, stage):
        rnd = Round.objects.create(
            org=self.org, round_number=1, series=self.series,
            stage=stage, lockout_at=timezone.now(),
        )
        return Match.objects.create(
            round=rnd, home_team=self.home, away_team=self.away, kickoff_at=timezone.now(),
        )

    # ---- §1 draws ------------------------------------------------------

    def test_a_drawn_regular_round_pays_nothing(self):
        m = self._match(Round.STAGE_REGULAR)
        Tip.objects.create(user=self.user, match=m, org=self.org, selection="home")
        record_match_result(m, 20, 20)
        t = Tip.objects.get(user=self.user, match=m)
        self.assertEqual(t.points_awarded, 0)
        self.assertFalse(t.is_correct)

    def test_a_drawn_origin_game_pays_two(self):
        """Origin scores "4 pts / 2 pts draw" — three games a series is too few
        for one of them to count for nothing."""
        m = self._match(Round.STAGE_ORIGIN)
        Tip.objects.create(user=self.user, match=m, org=self.org, selection="home")
        Tip.objects.create(user=self.other, match=m, org=self.org, selection="away")
        record_match_result(m, 12, 12)
        # Both sides, because nobody picked a winner that existed.
        for u in (self.user, self.other):
            self.assertEqual(Tip.objects.get(user=u, match=m).points_awarded, 2)

    def test_a_correct_origin_tip_still_pays_four(self):
        m = self._match(Round.STAGE_ORIGIN)
        Tip.objects.create(user=self.user, match=m, org=self.org, selection="home")
        record_match_result(m, 20, 10)
        self.assertEqual(Tip.objects.get(user=self.user, match=m).points_awarded, 4)

    def test_a_postponed_match_is_not_scored_either_way(self):
        m = self._match(Round.STAGE_REGULAR)
        Tip.objects.create(user=self.user, match=m, org=self.org, selection="home")
        m.status = Match.STATUS_POSTPONED
        m.save(update_fields=["status"])
        record_match_result(m, 30, 10)
        t = Tip.objects.get(user=self.user, match=m)
        self.assertIsNone(t.is_correct)
        self.assertEqual(t.points_awarded, 0)

    # ---- §2 missed-tip default -----------------------------------------

    def test_a_missed_tip_defaults_to_the_away_side(self):
        m = self._match(Round.STAGE_REGULAR)
        record_match_result(m, 10, 30)          # away won
        t = Tip.objects.get(user=self.user, match=m)
        self.assertEqual(t.selection, "away")
        self.assertTrue(t.is_auto)
        self.assertTrue(t.is_correct)
        self.assertEqual(t.points_awarded, 1)

    def test_an_auto_tip_can_be_wrong_like_any_other(self):
        m = self._match(Round.STAGE_REGULAR)
        record_match_result(m, 30, 10)          # home won
        t = Tip.objects.get(user=self.user, match=m)
        self.assertEqual(t.points_awarded, 0)
        self.assertFalse(t.is_correct)

    def test_a_real_tip_is_never_overwritten(self):
        m = self._match(Round.STAGE_REGULAR)
        Tip.objects.create(user=self.user, match=m, org=self.org, selection="home")
        record_match_result(m, 30, 10)
        t = Tip.objects.get(user=self.user, match=m)
        self.assertEqual(t.selection, "home")
        self.assertFalse(t.is_auto)

    def test_filling_twice_does_not_duplicate(self):
        """Results run every fifteen minutes; a re-grade must not stack tips."""
        m = self._match(Round.STAGE_REGULAR)
        record_match_result(m, 10, 30)
        record_match_result(m, 10, 30)
        self.assertEqual(Tip.objects.filter(match=m).count(), 2)   # one per member


class TiebreakerTests(TestCase):
    """Addendum §3: cross-code, then countback, then co-champions."""

    def setUp(self):
        from catalog.models import Season, Series, Sport
        from orgs.models import OrgMember, Organisation
        # The seeded NRL/NRLW, not new rows: Series.name is unique, and the
        # pairing map is keyed on those exact names, so inventing a couple
        # would either collide or never pair.
        self.mens = Series.objects.get(name="NRL")
        self.womens = Series.objects.get(name="NRLW")
        season = Season.objects.create(year=2099, label="2099")
        self.org = Organisation.objects.create(name="Tie League", season=season)
        self.a = User.objects.create_user(email="t1@x.com", password="x", display_name="Ann")
        self.b = User.objects.create_user(email="t2@x.com", password="x", display_name="Bob")
        for u in (self.a, self.b):
            OrgMember.objects.create(user=u, org=self.org)
        self.h = Team.objects.create(name="Tie Home", slug="tie-h", series=self.mens)
        self.aw = Team.objects.create(name="Tie Away", slug="tie-a", series=self.mens)

    def _award(self, user, series, points, when):
        rnd, _ = Round.objects.get_or_create(
            org=self.org, round_number=1, series=series,
            defaults={"lockout_at": when},
        )
        m = Match.objects.create(
            round=rnd, home_team=self.h, away_team=self.aw, kickoff_at=when,
        )
        Tip.objects.create(
            user=user, match=m, org=self.org, selection="home",
            is_correct=True, points_awarded=points,
        )

    def test_a_tie_in_the_mens_comp_breaks_on_the_womens(self):
        """Scoped to the NRL round, so the tie is a tie IN THAT COMP.

        The step only applies to a single-comp board. On an all-comps board the
        women's points are already inside the total, and using them again would
        rank on the same number twice.
        """
        now = timezone.now()
        self._award(self.a, self.mens, 5, now)
        self._award(self.b, self.mens, 5, now)
        self._award(self.b, self.womens, 3, now)   # Bob is better in NRLW
        nrl_round = Round.objects.get(org=self.org, series=self.mens)
        board = leaderboard_for_org(self.org, round_id=nrl_round.id)
        self.assertEqual([u.display_name for u in board], ["Bob", "Ann"])
        self.assertEqual([u.rank for u in board], [1, 2])
        self.assertFalse(board[0].is_tied)

    def test_level_on_both_falls_to_countback(self):
        """Same score in both comps — whoever got there first ranks higher."""
        early = timezone.now() - timedelta(days=7)
        late = timezone.now() - timedelta(days=1)
        self._award(self.a, self.mens, 4, early)
        self._award(self.b, self.mens, 4, late)
        board = leaderboard_for_org(self.org)
        self.assertEqual([u.display_name for u in board], ["Ann", "Bob"])

    def test_still_level_means_co_champions(self):
        same = timezone.now() - timedelta(days=3)
        self._award(self.a, self.mens, 4, same)
        self._award(self.b, self.mens, 4, same)
        board = leaderboard_for_org(self.org)
        self.assertEqual([u.rank for u in board], [1, 1])
        self.assertTrue(all(u.is_tied for u in board))

    def test_points_still_outrank_everything(self):
        """Within the comp being ranked, a lower score cannot be rescued."""
        now = timezone.now()
        self._award(self.a, self.mens, 9, now)
        self._award(self.b, self.mens, 4, now)
        self._award(self.b, self.womens, 99, now)
        nrl_round = Round.objects.get(org=self.org, series=self.mens)
        board = leaderboard_for_org(self.org, round_id=nrl_round.id)
        self.assertEqual([u.display_name for u in board], ["Ann", "Bob"])

    def test_an_all_comps_board_does_not_reuse_the_paired_score(self):
        """Everything is already in the total there, so it must not count twice."""
        now = timezone.now()
        self._award(self.a, self.mens, 9, now)
        self._award(self.b, self.mens, 4, now)
        self._award(self.b, self.womens, 99, now)
        board = leaderboard_for_org(self.org)          # unscoped
        # Bob genuinely leads on 103 to 9. The point is that he is not ranked
        # by his NRLW score a second time on top of it.
        self.assertEqual([u.display_name for u in board], ["Bob", "Ann"])
        self.assertEqual([u.points for u in board], [103, 9])


class MyTipsScopeTests(TestCase):
    """What My Tips shows, and the one untipped case it keeps."""

    def setUp(self):
        from catalog.models import Season, Series, Sport
        from orgs.models import OrgMember, Organisation
        sport = Sport.objects.create(name="Scope Footy", slug="scope-footy")
        self.series = Series.objects.create(sport=sport, name="Scope Series", slug="scope-series")
        season = Season.objects.create(year=2099, label="2099")
        self.org = Organisation.objects.create(name="Scope League", season=season)
        self.user = User.objects.create_user(
            email="scope@x.com", password="Str0ng!pass", display_name="Scoper",
        )
        OrgMember.objects.create(user=self.user, org=self.org)
        self.rnd = Round.objects.create(
            org=self.org, round_number=1, series=self.series,
            lockout_at=timezone.now() - timedelta(hours=3),
        )
        self.client.force_login(self.user)

    def _match(self, slug, *, hours, status=Match.STATUS_SCHEDULED, result=None):
        h = Team.objects.create(name=f"H{slug}", slug=f"sh-{slug}", series=self.series)
        a = Team.objects.create(name=f"A{slug}", slug=f"sa-{slug}", series=self.series)
        return Match.objects.create(
            round=self.rnd, home_team=h, away_team=a,
            kickoff_at=timezone.now() + timedelta(hours=hours),
            status=status, result=result,
        )

    def _rows(self):
        r = self.client.get(f"/org/{self.org.id}/tips/?round={self.rnd.id}")
        return r.context["rows"]

    def test_an_untipped_upcoming_match_is_hidden(self):
        self._match("up", hours=48)
        self.assertEqual(self._rows(), [])

    def test_an_untipped_finished_match_is_hidden(self):
        """Nothing to do and nothing to report — it was never yours."""
        m = self._match("done", hours=-30, status=Match.STATUS_COMPLETE, result="home")
        m.home_score, m.away_score = 30, 10
        m.save()
        self.assertEqual(self._rows(), [])

    def test_a_tipped_match_is_shown(self):
        m = self._match("mine", hours=48)
        Tip.objects.create(user=self.user, match=m, org=self.org, selection="home")
        rows = self._rows()
        self.assertEqual([r["match"].id for r in rows], [m.id])
        self.assertFalse(rows[0]["missed_live"])

    def test_an_untipped_LIVE_match_is_kept_and_flagged(self):
        """The one exception: a game on right now that you are not in.

        Hiding it would mean somebody watching the round sees only the games
        they picked, with no sign the others are being played.
        """
        m = self._match("live", hours=-1, status=Match.STATUS_LIVE)
        rows = self._rows()
        self.assertEqual([r["match"].id for r in rows], [m.id])
        self.assertTrue(rows[0]["missed_live"])
        self.assertIsNone(rows[0]["tip"])

    def test_untipped_live_games_lead_the_in_play_block(self):
        """They are the only rows carrying anything to notice, so they go first."""
        mine = self._match("livemine", hours=-2, status=Match.STATUS_LIVE)
        Tip.objects.create(user=self.user, match=mine, org=self.org, selection="home")
        theirs = self._match("livemissed", hours=-1, status=Match.STATUS_LIVE)
        rows = self._rows()
        self.assertEqual([r["match"].id for r in rows], [theirs.id, mine.id])

    def test_the_sidebar_still_counts_the_whole_round(self):
        """"1 of 4 tipped", not "1 of 1" — the round's real size is kept."""
        m = self._match("a", hours=48)
        Tip.objects.create(user=self.user, match=m, org=self.org, selection="home")
        for s in ("b", "c", "d"):
            self._match(s, hours=48)
        r = self.client.get(f"/org/{self.org.id}/tips/?round={self.rnd.id}")
        self.assertEqual(r.context["total_matches"], 4)
        self.assertEqual(r.context["tips_this_round"], 1)
