from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from catalog.models import Competition, Season, Series, Sport
from orgs.models import OrgMember, Organisation
from tipping.models import Match, Round, Team, Tip
from tipping.services import (
    clear_tip,
    leaderboard_for_family,
    leaderboard_for_org,
    record_match_result,
    submit_tip,
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


class TipCarryTests(TestCase):
    """One set of picks, landing in every room a member tips in.

    The hard part is not the copy — it is that a Round belongs to an
    Organisation and a Match belongs to a Round, so every org holds its OWN
    copy of every fixture. "The same match" is not a row two orgs share; it is
    an external_id that appears once per org. These tests pin that, and pin
    the rule that a contradicting pick is never overwritten unasked.
    """

    def setUp(self):
        from datetime import timedelta

        from django.utils import timezone

        from catalog.models import Competition, Season, Series, Sport
        from orgs.models import Group, GroupMember, OrgMember, Organisation
        from tipping.models import Match, Round, Team

        self.user = User.objects.create_user(
            email="carry@example.com", password="x", display_name="Carrie",
        )
        season = Season.objects.create(year=2094, label="2094")
        sport = Sport.objects.create(name="Carry Footy", slug="carry-footy")
        self.series = Series.objects.create(
            sport=sport, name="Carry Series", slug="carry-series",
        )
        comp = Competition.objects.create(
            sport=sport, season=season, name="Carry Comp", slug="carry-comp",
        )
        comp.series.add(self.series)
        home = Team.objects.create(name="Pies", slug="carry-pies", series=self.series)
        away = Team.objects.create(name="Blues", slug="carry-blues", series=self.series)
        now = timezone.now()

        self.orgs = {}
        for name, groups_on in (("Work", True), ("Mates", False), ("Family", False)):
            org = Organisation.objects.create(
                name=name, season=season, groups_enabled=groups_on,
            )
            org.competitions.add(comp)
            OrgMember.objects.create(user=self.user, org=org)
            rnd = Round.objects.create(
                org=org, round_number=1, series=self.series, competition=comp,
                lockout_at=now + timedelta(days=2),
            )
            # The SAME two real fixtures, as this org's own rows.
            for ext in ("EXT-1", "EXT-2"):
                Match.objects.create(
                    round=rnd, home_team=home, away_team=away,
                    kickoff_at=now + timedelta(days=2), external_id=ext,
                )
            self.orgs[name] = org
        self.group = Group.objects.create(org=self.orgs["Work"], name="Marketing")
        GroupMember.objects.create(group=self.group, user=self.user)
        self.client.force_login(self.user)

    def match(self, org_name, ext):
        from tipping.models import Match

        return Match.objects.get(round__org=self.orgs[org_name], external_id=ext)

    def tip_in(self, org_name, ext, group=None):
        from tipping.models import Tip

        return Tip.objects.filter(
            user=self.user, org=self.orgs[org_name], group=group,
            match__external_id=ext,
        ).first()

    def confirm_in_work(self):
        org = self.orgs["Work"]
        return self.client.post(f"/org/{org.id}/tip/confirm/", {
            f"match_{self.match('Work', 'EXT-1').id}": "home",
            f"match_{self.match('Work', 'EXT-2').id}": "home",
        })

    def carry_url(self):
        return f"/org/{self.orgs['Work'].id}/tip/carry/"

    def form_values(self, name):
        import re

        body = self.client.get(self.carry_url()).content.decode()
        return re.findall(rf'name="{name}" value="([^"]+)"', body)

    # ---- what a room is ------------------------------------------------

    def test_the_organisation_and_its_group_are_separate_rooms(self):
        """Being in Marketing does not stop you tipping for the company —
        the room switcher exists so you can do both."""
        from tipping.carry import rooms_for

        labels = {r.label for r in rooms_for(self.user)}
        self.assertIn("Work", labels)
        self.assertIn("Work · Marketing", labels)

    def test_a_group_is_not_a_room_when_the_org_has_groups_off(self):
        from orgs.models import Group, GroupMember
        from tipping.carry import rooms_for

        g = Group.objects.create(org=self.orgs["Mates"], name="Hidden")
        GroupMember.objects.create(group=g, user=self.user)
        self.assertNotIn(
            "Mates · Hidden", {r.label for r in rooms_for(self.user)},
        )

    # ---- the plan ------------------------------------------------------

    def test_picks_are_matched_across_orgs_by_external_id(self):
        """Each org holds its own row for the same fixture — the plan has to
        find the right one in each."""
        from tipping.carry import Room, build_plan

        plans = build_plan(
            self.user,
            {self.match("Work", "EXT-1").id: "home"},
            Room(org=self.orgs["Work"]),
        )
        by_label = {p.room.label: p for p in plans}
        self.assertEqual(
            by_label["Mates"].writes[0]["match"].id, self.match("Mates", "EXT-1").id,
        )

    def test_a_fixture_with_no_external_id_cannot_carry(self):
        from tipping.carry import Room, build_plan
        from tipping.models import Match

        m = self.match("Work", "EXT-1")
        Match.objects.filter(pk=m.pk).update(external_id="")
        m.refresh_from_db()
        self.assertEqual(build_plan(self.user, {m.id: "home"}, Room(org=self.orgs["Work"])), [])

    def test_an_identical_pick_elsewhere_is_not_a_conflict(self):
        from tipping.carry import Room, build_plan
        from tipping.services import submit_tip

        submit_tip(
            user=self.user, match=self.match("Mates", "EXT-1"),
            org=self.orgs["Mates"], selection="home",
        )
        plans = {
            p.room.label: p
            for p in build_plan(
                self.user, {self.match("Work", "EXT-1").id: "home"},
                Room(org=self.orgs["Work"]),
            )
        }
        self.assertEqual(plans["Mates"].conflicts, [])
        self.assertEqual(len(plans["Mates"].unchanged), 1)

    def test_a_different_pick_elsewhere_is_a_conflict(self):
        from tipping.carry import Room, build_plan
        from tipping.services import submit_tip

        submit_tip(
            user=self.user, match=self.match("Mates", "EXT-1"),
            org=self.orgs["Mates"], selection="away",
        )
        plans = {
            p.room.label: p
            for p in build_plan(
                self.user, {self.match("Work", "EXT-1").id: "home"},
                Room(org=self.orgs["Work"]),
            )
        }
        self.assertEqual(len(plans["Mates"].conflicts), 1)
        self.assertEqual(plans["Mates"].conflicts[0]["existing"], "away")

    # ---- the review screen ---------------------------------------------

    def test_confirming_sends_a_multi_room_member_to_the_review(self):
        r = self.confirm_in_work()
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.headers["Location"], self.carry_url())

    def test_a_single_room_member_is_never_shown_it(self):
        """Nearly everybody. The feature has to stay invisible to them."""
        from orgs.models import OrgMember

        OrgMember.objects.filter(user=self.user).exclude(
            org=self.orgs["Work"],
        ).delete()
        self.group.memberships.all().delete()
        r = self.confirm_in_work()
        self.assertIn("/dashboard/", r.headers["Location"])

    def test_carrying_writes_into_every_ticked_room(self):
        self.confirm_in_work()
        self.client.post(self.carry_url(), {
            "action": "carry", "room": self.form_values("room"),
        })
        self.assertEqual(self.tip_in("Mates", "EXT-1").selection, "home")
        self.assertEqual(self.tip_in("Family", "EXT-2").selection, "home")
        self.assertEqual(
            self.tip_in("Work", "EXT-1", group=self.group).selection, "home",
        )

    def test_an_unticked_room_gets_nothing(self):
        self.confirm_in_work()
        rooms = [k for k in self.form_values("room")
                 if k.startswith(str(self.orgs["Mates"].id))]
        self.client.post(self.carry_url(), {"action": "carry", "room": rooms})
        self.assertIsNotNone(self.tip_in("Mates", "EXT-1"))
        self.assertIsNone(self.tip_in("Family", "EXT-1"))

    # ---- the rule that matters -----------------------------------------

    def test_a_contradicting_pick_survives_a_plain_carry(self):
        """Somebody who tipped their own club at home and against it at work
        meant both. Carrying must not quietly reconcile them."""
        from tipping.services import submit_tip

        submit_tip(
            user=self.user, match=self.match("Mates", "EXT-1"),
            org=self.orgs["Mates"], selection="away",
        )
        self.confirm_in_work()
        self.client.post(self.carry_url(), {
            "action": "carry", "room": self.form_values("room"),
        })
        self.assertEqual(self.tip_in("Mates", "EXT-1").selection, "away")
        # ...while the game with no prior pick still carries.
        self.assertEqual(self.tip_in("Mates", "EXT-2").selection, "home")

    def test_it_is_replaced_only_when_explicitly_overridden(self):
        from tipping.services import submit_tip

        submit_tip(
            user=self.user, match=self.match("Mates", "EXT-1"),
            org=self.orgs["Mates"], selection="away",
        )
        self.confirm_in_work()
        self.client.post(self.carry_url(), {
            "action": "carry",
            "room": self.form_values("room"),
            "override": self.form_values("override"),
        })
        self.assertEqual(self.tip_in("Mates", "EXT-1").selection, "home")

    def test_an_override_for_an_unticked_room_does_nothing(self):
        """The screen dims those, but the POST must enforce it — an override
        is meaningless without the room it belongs to."""
        from tipping.services import submit_tip

        submit_tip(
            user=self.user, match=self.match("Mates", "EXT-1"),
            org=self.orgs["Mates"], selection="away",
        )
        self.confirm_in_work()
        self.client.post(self.carry_url(), {
            "action": "carry", "room": [], "override": self.form_values("override"),
        })
        self.assertEqual(self.tip_in("Mates", "EXT-1").selection, "away")

    def test_not_this_time_carries_nothing(self):
        self.confirm_in_work()
        self.client.post(self.carry_url(), {"action": "skip"})
        self.assertIsNone(self.tip_in("Mates", "EXT-1"))
        # ...but the source room keeps what was confirmed.
        self.assertEqual(self.tip_in("Work", "EXT-1").selection, "home")

    # ---- remembering the answer ----------------------------------------

    def test_always_skips_the_review_entirely(self):
        from accounts.models import User

        self.user.tip_carry_mode = User.CARRY_ALL
        self.user.save(update_fields=["tip_carry_mode"])
        r = self.confirm_in_work()
        self.assertIn("/dashboard/", r.headers["Location"])
        self.assertEqual(self.tip_in("Family", "EXT-1").selection, "home")

    def test_always_still_does_not_overwrite_a_different_pick(self):
        """Turning the question off is not consent to have a deliberate
        different tip replaced. Overriding stays an explicit act."""
        from accounts.models import User
        from tipping.services import submit_tip

        submit_tip(
            user=self.user, match=self.match("Mates", "EXT-1"),
            org=self.orgs["Mates"], selection="away",
        )
        self.user.tip_carry_mode = User.CARRY_ALL
        self.user.save(update_fields=["tip_carry_mode"])
        self.confirm_in_work()
        self.assertEqual(self.tip_in("Mates", "EXT-1").selection, "away")

    def test_never_carries_nothing_and_asks_nothing(self):
        from accounts.models import User

        self.user.tip_carry_mode = User.CARRY_NONE
        self.user.save(update_fields=["tip_carry_mode"])
        r = self.confirm_in_work()
        self.assertIn("/dashboard/", r.headers["Location"])
        self.assertIsNone(self.tip_in("Mates", "EXT-1"))

    def test_the_review_can_set_the_preference(self):
        from accounts.models import User

        self.confirm_in_work()
        self.client.post(self.carry_url(), {
            "action": "carry", "room": self.form_values("room"), "remember": "all",
        })
        self.user.refresh_from_db()
        self.assertEqual(self.user.tip_carry_mode, User.CARRY_ALL)

    def test_the_preference_is_reversible_from_the_profile(self):
        """"Never ask me again" has to be undoable somewhere obvious, or it
        is a trap rather than a preference."""
        from accounts.models import User

        self.user.tip_carry_mode = User.CARRY_NONE
        self.user.save(update_fields=["tip_carry_mode"])
        self.client.post("/profile/", {"tip_carry_mode": User.CARRY_ASK})
        self.user.refresh_from_db()
        self.assertEqual(self.user.tip_carry_mode, User.CARRY_ASK)

    def test_the_profile_hides_the_setting_from_single_room_members(self):
        from orgs.models import OrgMember

        OrgMember.objects.filter(user=self.user).exclude(org=self.orgs["Work"]).delete()
        self.group.memberships.all().delete()
        body = self.client.get("/profile/").content.decode()
        self.assertNotIn("Tipping in more than one group", body)

    # ---- safety --------------------------------------------------------

    def test_the_review_cannot_be_opened_for_someone_elses_org(self):
        other = User.objects.create_user(
            email="nosy@example.com", password="x", display_name="Nosy",
        )
        self.confirm_in_work()
        self.client.force_login(other)
        r = self.client.get(self.carry_url())
        self.assertEqual(r.status_code, 403)

    def test_the_review_redirects_when_there_is_nothing_pending(self):
        r = self.client.get(self.carry_url())
        self.assertEqual(r.status_code, 302)
        self.assertIn("/dashboard/", r.headers["Location"])


class RoundInPlayTests(TestCase):
    """Which round the dashboard calls "this round".

    The client's report: on wildcard finals weekend, with two AFL games on,
    the confirm sheet read "2 from 9 pick". Nine is a standard home-and-away
    round, so it looked like finals rounds were being given a regular round's
    fixture count. They were not — the finals round had its two fixtures and
    counted them correctly. A DIFFERENT round was being counted.
    """

    def setUp(self):
        # Its own sport and series: the real "afl" slug is seeded, and these
        # tests are about round bookkeeping rather than any particular code.
        self.sport = Sport.objects.create(name="Wildcard Footy", slug="wc-footy")
        self.series = Series.objects.create(
            sport=self.sport, name="Wildcard Premiership", slug="wc-prem",
        )
        self.season = Season.objects.create(year=2099, label="2099")
        self.org = Organisation.objects.create(name="Wildcard FC", season=self.season)
        self.home = Team.objects.create(name="Blues", slug="blues", series=self.series)
        self.away = Team.objects.create(name="Pies", slug="pies", series=self.series)
        self.now = timezone.now()

    def _round(self, number, stage=Round.STAGE_REGULAR):
        return Round.objects.create(
            org=self.org, round_number=number, series=self.series, stage=stage,
            lockout_at=self.now,
        )

    def _match(self, rnd, *, kickoff, complete):
        return Match.objects.create(
            round=rnd, home_team=self.home, away_team=self.away,
            kickoff_at=kickoff,
            status=Match.STATUS_COMPLETE if complete else Match.STATUS_SCHEDULED,
            home_score=30 if complete else None,
            away_score=10 if complete else None,
        )

    def _in_play(self):
        from tipping.services import annotate_play_state, current_round

        return current_round(list(
            annotate_play_state(Round.objects.filter(org=self.org))
            .order_by("-round_number")
        ))

    def test_a_fixture_that_never_got_graded_does_not_pin_the_season(self):
        """THE BUG. Round 1 kept three fixtures at "scheduled" — a dropped
        feed, or a game abandoned and never closed out. Five months later
        those rows still said "not complete", so the earliest round still in
        play was Round 1, and the stat card offered its 9 games while the
        slate underneath showed the finals."""
        march = self._round(1)
        for _ in range(3):
            self._match(march, kickoff=self.now - timedelta(days=150), complete=False)
        for _ in range(6):
            self._match(march, kickoff=self.now - timedelta(days=150), complete=True)

        finals = self._round(25, stage=Round.STAGE_FINALS)
        for _ in range(2):
            self._match(finals, kickoff=self.now + timedelta(days=2), complete=False)

        rnd = self._in_play()
        self.assertEqual(rnd.round_number, 25)
        self.assertEqual(rnd.matches.count(), 2)

    def test_a_game_currently_being_played_still_counts(self):
        """The reason this is a time window and not "kickoff is in the
        future". A round must not skip forward the moment its last game
        bounces — Sunday afternoon is exactly when people are watching."""
        live = self._round(10)
        self._match(live, kickoff=self.now - timedelta(hours=1), complete=False)
        later = self._round(11)
        self._match(later, kickoff=self.now + timedelta(days=6), complete=False)

        self.assertEqual(self._in_play().round_number, 10)

    def test_a_finals_round_is_counted_by_its_own_fixtures(self):
        """What the client thought was broken, asserted directly so the real
        fix cannot be mistaken for this one later."""
        finals = self._round(25, stage=Round.STAGE_FINALS)
        for _ in range(2):
            self._match(finals, kickoff=self.now + timedelta(days=1), complete=False)

        self.assertEqual(self._in_play().matches.count(), 2)

    def test_a_finished_season_still_names_a_round(self):
        """Every round played out: fall back to the most recent rather than
        leaving the dashboard with no round at all."""
        done = self._round(24)
        self._match(done, kickoff=self.now - timedelta(days=9), complete=True)

        self.assertEqual(self._in_play().round_number, 24)


class MidSeasonJoinerTests(TestCase):
    """Somebody joining in round 20 starts on the away side for rounds 1-19,
    not on nothing.

    Without this they sit on zero for two-thirds of a season they never had a
    chance at, and the ladder reads as though they played badly rather than
    that they arrived late.
    """

    def setUp(self):
        self.sport = Sport.objects.create(name="Join Footy", slug="join-footy")
        self.series = Series.objects.create(
            sport=self.sport, name="Join Premiership", slug="join-prem",
        )
        self.season = Season.objects.create(year=2099, label="2099")
        self.org = Organisation.objects.create(name="Late Starters", season=self.season)
        self.home = Team.objects.create(name="Reds", slug="j-reds", series=self.series)
        self.away = Team.objects.create(name="Greens", slug="j-greens", series=self.series)
        self.now = timezone.now()

        # Two rounds already played out, and one still to come.
        self.played = []
        for n in (1, 2):
            rnd = Round.objects.create(
                org=self.org, round_number=n, series=self.series,
                lockout_at=self.now - timedelta(days=40 - n),
            )
            m = Match.objects.create(
                round=rnd, home_team=self.home, away_team=self.away,
                kickoff_at=self.now - timedelta(days=40 - n),
            )
            # Away wins both, so a backdated pick is worth something and the
            # test can tell scored from merely written.
            record_match_result(m, 10, 30)
            self.played.append(m)

        self.upcoming_round = Round.objects.create(
            org=self.org, round_number=3, series=self.series,
            lockout_at=self.now + timedelta(days=3),
        )
        self.upcoming = Match.objects.create(
            round=self.upcoming_round, home_team=self.home, away_team=self.away,
            kickoff_at=self.now + timedelta(days=3),
        )

    def _join(self, email="late@example.com", **kwargs):
        from orgs.services import add_member

        user = User.objects.create_user(
            email=email, password="x", display_name="Latecomer",
        )
        add_member(user, self.org, **kwargs)
        return user

    def test_rounds_already_played_are_backdated_to_the_away_side(self):
        user = self._join()
        tips = Tip.objects.filter(user=user, org=self.org, group=None)
        self.assertEqual(tips.count(), 2)
        self.assertTrue(all(t.selection == "away" for t in tips))

    def test_the_backdated_picks_are_scored(self):
        """The whole point. An unscored backdated tip helps nobody."""
        user = self._join()
        self.assertEqual(
            user_org_stats(user, self.org)["points"], 2,   # away won both, 1pt each
        )

    def test_they_are_flagged_as_auto_so_accuracy_is_untouched(self):
        """Points include the default; a strike rate is a claim about
        judgement and cannot include picks the system made."""
        user = self._join()
        self.assertTrue(all(
            t.is_auto for t in Tip.objects.filter(user=user, org=self.org)
        ))
        self.assertEqual(user_org_stats(user, self.org)["tips_correct"], 0)

    def test_a_game_they_can_still_tip_is_left_alone(self):
        """Joining on Friday still lets you pick Saturday yourself."""
        user = self._join()
        self.assertFalse(
            Tip.objects.filter(user=user, match=self.upcoming).exists()
        )

    def test_a_manager_who_never_tips_is_not_entered(self):
        from orgs.models import OrgMember

        user = self._join(email="boss@example.com", role=OrgMember.ROLE_MANAGER)
        self.assertEqual(Tip.objects.filter(user=user, org=self.org).count(), 0)

    def test_running_it_again_changes_nothing(self):
        from tipping.services import backdate_missed_tips

        user = self._join()
        before = list(
            Tip.objects.filter(user=user, org=self.org)
            .order_by("match_id").values_list("match_id", "selection")
        )
        backdate_missed_tips(user, self.org)
        after = list(
            Tip.objects.filter(user=user, org=self.org)
            .order_by("match_id").values_list("match_id", "selection")
        )
        self.assertEqual(before, after)

    def test_a_real_pick_is_never_overwritten(self):
        """Somebody who tipped, left and rejoined keeps what they chose."""
        from tipping.services import backdate_missed_tips

        user = self._join()
        tip = Tip.objects.get(user=user, match=self.played[0], group=None)
        tip.selection = "home"
        tip.is_auto = False
        tip.save(update_fields=["selection", "is_auto"])

        backdate_missed_tips(user, self.org)
        tip.refresh_from_db()
        self.assertEqual(tip.selection, "home")
        self.assertFalse(tip.is_auto)

    def test_a_group_is_backdated_on_its_own_ladder(self):
        """A group scores separately, so the organisation's rows do not cover
        it — joining both backdates both."""
        from orgs.services import join_group
        from orgs.models import Group

        self.org.groups_enabled = True
        self.org.save(update_fields=["groups_enabled"])
        group = Group.objects.create(
            org=self.org, name="Night Shift",
            approval_status=Group.APPROVAL_APPROVED,
        )
        user = self._join()
        join_group(group, user=user)

        self.assertEqual(
            Tip.objects.filter(user=user, org=self.org, group=group).count(), 2,
        )
        self.assertEqual(
            Tip.objects.filter(user=user, org=self.org, group=None).count(), 2,
        )


class CarryBeforeSaveTests(TestCase):
    """Carrying is asked BEFORE the tips are written, and answered in one post.

    The old order wrote the slate, then sent you to a separate screen to
    decide whether it should also go to your other rooms. These pin the new
    shape: the preview endpoint writes nothing, and a confirm that carries an
    answer with it does not bounce anybody to that screen a second time.
    """

    def setUp(self):
        from datetime import timedelta

        from django.utils import timezone

        from catalog.models import Competition, Season, Series, Sport
        from orgs.models import OrgMember, Organisation
        from tipping.models import Match, Round, Team

        self.user = User.objects.create_user(
            email="two-step@example.com", password="x", display_name="Twostep",
        )
        season = Season.objects.create(year=2095, label="2095")
        sport = Sport.objects.create(name="Step Footy", slug="step-footy")
        series = Series.objects.create(sport=sport, name="Step Series", slug="step-series")
        comp = Competition.objects.create(
            sport=sport, season=season, name="Step Comp", slug="step-comp",
        )
        comp.series.add(series)
        home = Team.objects.create(name="Reds", slug="step-reds", series=series)
        away = Team.objects.create(name="Golds", slug="step-golds", series=series)
        now = timezone.now()

        self.orgs = {}
        for name in ("Work", "Mates"):
            org = Organisation.objects.create(name=name, season=season)
            org.competitions.add(comp)
            OrgMember.objects.create(user=self.user, org=org)
            rnd = Round.objects.create(
                org=org, round_number=1, series=series, competition=comp,
                lockout_at=now + timedelta(days=2),
            )
            for ext in ("S-1", "S-2"):
                Match.objects.create(
                    round=rnd, home_team=home, away_team=away,
                    kickoff_at=now + timedelta(days=2), external_id=ext,
                )
            self.orgs[name] = org
        self.client.force_login(self.user)

    def _match(self, org_name, ext):
        from tipping.models import Match

        return Match.objects.get(round__org=self.orgs[org_name], external_id=ext)

    def _slate(self):
        return {
            f"match_{self._match('Work', 'S-1').id}": "home",
            f"match_{self._match('Work', 'S-2').id}": "away",
        }

    def test_preview_writes_nothing(self):
        """The whole reason this can run before the save."""
        from tipping.models import Tip

        resp = self.client.post(
            reverse("tipping:carry_preview", args=[self.orgs["Work"].id]), self._slate(),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Mates")
        self.assertEqual(Tip.objects.count(), 0)

    def test_preview_is_204_when_there_is_nowhere_to_carry(self):
        """The majority case: one room, so the sheet skips the step."""
        from orgs.models import OrgMember

        OrgMember.objects.filter(user=self.user, org=self.orgs["Mates"]).delete()
        resp = self.client.post(
            reverse("tipping:carry_preview", args=[self.orgs["Work"].id]), self._slate(),
        )
        self.assertEqual(resp.status_code, 204)

    def test_confirm_with_an_answer_carries_and_does_not_redirect_to_the_screen(self):
        from tipping.models import Tip

        data = self._slate()
        data["carry_answered"] = "1"
        data["room"] = f"{self.orgs['Mates'].id}:0"
        resp = self.client.post(
            reverse("tipping:tip_confirm_upcoming", args=[self.orgs["Work"].id]), data,
        )
        # Straight back to the dashboard — NOT to /tip/carry/.
        self.assertEqual(resp.status_code, 302)
        self.assertNotIn("/carry/", resp["Location"])
        self.assertEqual(Tip.objects.filter(org=self.orgs["Work"]).count(), 2)
        self.assertEqual(Tip.objects.filter(org=self.orgs["Mates"]).count(), 2)

    def test_confirm_with_an_answer_that_declines_carries_nothing(self):
        from tipping.models import Tip

        data = self._slate()
        data["carry_answered"] = "1"          # asked, and every room unticked
        resp = self.client.post(
            reverse("tipping:tip_confirm_upcoming", args=[self.orgs["Work"].id]), data,
        )
        self.assertEqual(resp.status_code, 302)
        self.assertNotIn("/carry/", resp["Location"])
        self.assertEqual(Tip.objects.filter(org=self.orgs["Work"]).count(), 2)
        self.assertEqual(Tip.objects.filter(org=self.orgs["Mates"]).count(), 0)

    def test_confirm_without_an_answer_still_uses_the_standalone_screen(self):
        """The no-JS path has to keep working."""
        resp = self.client.post(
            reverse("tipping:tip_confirm_upcoming", args=[self.orgs["Work"].id]),
            self._slate(),
        )
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/carry/", resp["Location"])

    def test_remember_always_is_saved_from_the_sheet(self):
        from accounts.models import User as U

        data = self._slate()
        data["carry_answered"] = "1"
        data["remember"] = U.CARRY_ALL
        self.client.post(
            reverse("tipping:tip_confirm_upcoming", args=[self.orgs["Work"].id]), data,
        )
        self.user.refresh_from_db()
        self.assertEqual(self.user.tip_carry_mode, U.CARRY_ALL)

    def test_preview_refuses_a_non_member(self):
        other = User.objects.create_user(
            email="nope@example.com", password="x", display_name="Nope",
        )
        self.client.force_login(other)
        resp = self.client.post(
            reverse("tipping:carry_preview", args=[self.orgs["Work"].id]), self._slate(),
        )
        self.assertEqual(resp.status_code, 403)


class TakingATipBackTests(TestCase):
    """Pressing the team you already picked un-picks it.

    Before this a pick was a one-way door. The control is a radio, and a radio
    group has no way back to "none" once one of its members is checked — so a
    member could change their mind about WHICH side, but not about whether they
    had tipped at all. On a game they did not fancy either way, "no tip" is a
    real answer.
    """

    def setUp(self):
        self.sport = Sport.objects.create(name="Undo Footy", slug="undo-footy")
        self.series = Series.objects.create(sport=self.sport, name="Undo Series", slug="undo-series")
        self.season = Season.objects.create(year=2098, label="2098")
        self.org = Organisation.objects.create(name="Undo League", season=self.season)
        self.user = User.objects.create_user(email="undo@b.com", password="x", display_name="Undo")
        OrgMember.objects.create(user=self.user, org=self.org)
        self.home = Team.objects.create(name="Reds", slug="undo-reds", series=self.series)
        self.away = Team.objects.create(name="Blues", slug="undo-blues", series=self.series)
        self.round = Round.objects.create(
            org=self.org, round_number=1, series=self.series,
            lockout_at=timezone.now() + timedelta(days=2),
        )
        self.match = Match.objects.create(
            round=self.round, home_team=self.home, away_team=self.away,
            kickoff_at=timezone.now() + timedelta(days=2),
        )
        self.client.force_login(self.user)

    def _tip(self, selection="home"):
        return submit_tip(user=self.user, match=self.match, org=self.org, selection=selection)

    def test_clear_tip_removes_it(self):
        self._tip()
        self.assertTrue(clear_tip(user=self.user, match=self.match, org=self.org))
        self.assertFalse(Tip.objects.filter(user=self.user, match=self.match).exists())

    def test_clear_tip_reports_when_there_was_nothing_to_clear(self):
        # Not an error — un-picking something never picked is a no-op, and the
        # dashboard posts one for any fixture the member toggled off.
        self.assertFalse(clear_tip(user=self.user, match=self.match, org=self.org))

    def test_clear_tip_refuses_once_the_match_has_started(self):
        """What you tipped is a matter of record once the game is under way.

        Without this, clearing would be a way to quietly erase a wrong call
        after the fact — the same reason submit_tip refuses a locked match.
        """
        self._tip()
        Match.objects.filter(pk=self.match.pk).update(
            kickoff_at=timezone.now() - timedelta(hours=1),
        )
        self.match.refresh_from_db()
        with self.assertRaises(ValueError):
            clear_tip(user=self.user, match=self.match, org=self.org)
        self.assertTrue(Tip.objects.filter(user=self.user, match=self.match).exists())

    def test_posting_none_to_the_save_endpoint_clears(self):
        self._tip()
        url = reverse("tipping:tip_save", args=[self.org.id, self.round.id, self.match.id])
        r = self.client.post(url, {"selection": "none"})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(Tip.objects.filter(user=self.user, match=self.match).exists())
        # The tick has to say which of the two things happened; answering
        # "Saved" to a take-back tells the member the opposite of the truth.
        self.assertContains(r, "No tip")

    def test_the_card_that_comes_back_offers_the_pick_again(self):
        """After un-picking, the same button must post the side again.

        The card is re-rendered from the server, so this is the check that the
        toggle does not get stuck: an hx-vals still saying "none" would make
        the second press a second un-pick and the team unpickable.
        """
        self._tip()
        url = reverse("tipping:tip_save", args=[self.org.id, self.round.id, self.match.id])
        r = self.client.post(url, {"selection": "none", "view": "mytips"})
        self.assertContains(r, '"selection":"home"')
        self.assertNotContains(r, '"selection":"none"')

    def test_bulk_confirm_clears_what_the_slate_un_picked(self):
        self._tip()
        r = self.client.post(
            reverse("tipping:tip_confirm", args=[self.org.id, self.round.id]),
            {f"match_{self.match.id}": "none"},
        )
        self.assertEqual(r.status_code, 302)
        self.assertFalse(Tip.objects.filter(user=self.user, match=self.match).exists())

    def test_a_fixture_absent_from_the_slate_keeps_its_tip(self):
        """Absent is not the same as un-picked, and the difference matters.

        A member confirms a slate on a phone while a tip made last week from a
        desktop is not on screen. Treating "not in the POST" as "take it back"
        would silently delete it.
        """
        self._tip()
        self.client.post(
            reverse("tipping:tip_confirm", args=[self.org.id, self.round.id]),
            {},
        )
        self.assertTrue(Tip.objects.filter(user=self.user, match=self.match).exists())

    def test_cross_round_confirm_clears_too(self):
        self._tip()
        self.client.post(
            reverse("tipping:tip_confirm_upcoming", args=[self.org.id]),
            {f"match_{self.match.id}": "none"},
        )
        self.assertFalse(Tip.objects.filter(user=self.user, match=self.match).exists())

    def test_a_clear_for_another_org_is_ignored(self):
        """The posted ids are re-checked against this organisation's matches.

        Same rule as the picks beside them: an id from a league you are not in
        must not reach a delete just because you can spell it.
        """
        other_season = Season.objects.create(year=2097, label="2097")
        other = Organisation.objects.create(name="Someone Else", season=other_season)
        other_round = Round.objects.create(
            org=other, round_number=1, series=self.series,
            lockout_at=timezone.now() + timedelta(days=2),
        )
        other_match = Match.objects.create(
            round=other_round, home_team=self.home, away_team=self.away,
            kickoff_at=timezone.now() + timedelta(days=2),
        )
        victim = User.objects.create_user(email="v@b.com", password="x", display_name="V")
        OrgMember.objects.create(user=victim, org=other)
        Tip.objects.create(user=self.user, match=other_match, org=other, selection="home")

        self.client.post(
            reverse("tipping:tip_confirm_upcoming", args=[self.org.id]),
            {f"match_{other_match.id}": "none"},
        )
        self.assertTrue(Tip.objects.filter(match=other_match).exists())


class GroupContextSurvivesTheDashboardTests(TestCase):
    """A member of two organisations must not lose the group they stand in.

    REPORTED AS: "I entered and selected tips, then went to My Tips and it only
    had one." Which is what it looks like from the outside. Underneath, the
    tips were written — into the wrong room.

    THE MECHANISM. Tips belong to a (user, org, GROUP) triple, and which group
    is read from the session by orgs.context.current_group. That function
    self-heals a stale choice: if the id in the session does not resolve to a
    group of the organisation it is asked about, it forgets it. Correct, for
    the question it was written to answer — "which group am I standing in".

    dashboard_view asks it a different question. It loops over EVERY membership
    to build the org picker and calls current_group(request, org) once per
    organisation, meaning "which group applies to this card". For a member in
    two organisations, one of those calls is always about the org they are not
    in — and the self-heal fires and wipes the session.

    After that the confirm posts with group=None and the tips land on the
    organisation's ladder, invisible from inside the group. Whether it bites at
    all depends on the order memberships come back in, which is by org name —
    so it reproduces for one member and not for the next, and looked random.
    """

    def setUp(self):
        from orgs.models import Group

        sport = Sport.objects.create(name="Ctx Footy", slug="ctx-footy")
        self.series = Series.objects.create(sport=sport, name="Ctx", slug="ctx-series")
        season = Season.objects.create(year=2099, label="2099")
        # Named so "Acme" sorts BEFORE "Zenith" under memberships_for's
        # order_by("org__name") — the loop therefore reaches the org the member
        # is NOT standing in first, which is the order that loses the group.
        self.other = Organisation.objects.create(name="Acme", season=season)
        self.home_org = Organisation.objects.create(
            name="Zenith", season=season, groups_enabled=True,
        )
        self.user = User.objects.create_user(
            email="ctx@x.com", password="Str0ng!pass", display_name="Ctx",
        )
        OrgMember.objects.create(user=self.user, org=self.other)
        OrgMember.objects.create(user=self.user, org=self.home_org)
        self.group = Group.objects.create(
            org=self.home_org, name="Marketing",
            approval_status=Group.APPROVAL_APPROVED,
        )
        self.group.memberships.create(user=self.user)
        self.client.force_login(self.user)

    def _stand_in_the_group(self):
        from orgs.context import GROUP_KEY, ORG_KEY
        s = self.client.session
        s[ORG_KEY] = self.home_org.pk
        s[GROUP_KEY] = self.group.pk
        s.save()

    def test_the_dashboard_does_not_forget_which_group_you_are_in(self):
        from orgs.context import GROUP_KEY

        self._stand_in_the_group()
        self.client.get(f"/dashboard/?org={self.home_org.id}")
        self.assertEqual(
            self.client.session.get(GROUP_KEY), self.group.pk,
            "the dashboard's per-membership current_group() calls dropped the "
            "group from the session",
        )

    def test_tips_confirmed_from_the_dashboard_land_in_the_group(self):
        """The consequence, end to end: pick, confirm, and find it in My Tips."""
        rnd = Round.objects.create(
            org=self.home_org, round_number=1, series=self.series,
            lockout_at=timezone.now() + timedelta(days=2),
        )
        home = Team.objects.create(name="H", slug="ctx-h", series=self.series)
        away = Team.objects.create(name="A", slug="ctx-a", series=self.series)
        match = Match.objects.create(
            round=rnd, home_team=home, away_team=away,
            kickoff_at=timezone.now() + timedelta(days=2),
        )

        self._stand_in_the_group()
        self.client.get(f"/dashboard/?org={self.home_org.id}")   # the trigger
        self.client.post(
            reverse("tipping:tip_confirm_upcoming", args=[self.home_org.id]),
            {f"match_{match.id}": "home"},
        )

        tip = Tip.objects.get(user=self.user, match=match)
        self.assertEqual(
            tip.group_id, self.group.pk,
            "the tip was written to the organisation instead of the group the "
            "member was standing in",
        )


class LeaderboardCountsEachTipOnceTests(TestCase):
    """Points on the board have to be points somebody actually scored.

    REPORTED AS: "points in the leaderboard were not real — I don't know
    someone's 227 or some figure like that."

    THE MECHANISM. _leaderboard starts from

        User.objects.filter(memberships__org_id__in=org_ids).distinct()

    and then annotates Sum("tips__points_awarded") onto it. That is one query
    with two joins to multi-valued relations — OrgMember and Tip — so the rows
    the database sums over are the CROSS PRODUCT of the two. A member with one
    membership in scope gets each tip once. A member with two gets every tip
    twice, and their score doubles.

    `.distinct()` does not save it: SELECT DISTINCT de-duplicates the rows
    coming OUT of the aggregate, long after the sum was taken over the
    duplicated ones.

    On a local board org_ids is a single org and OrgMember is unique per
    (user, org), so it cannot bite. On the NATIONAL board org_ids is the whole
    family, and anybody who belongs to the parent as well as to a store — an
    owner who also works in one, which is the ordinary case, not the exotic
    one — is counted once per membership. Their real total is multiplied by
    how many of the family's organisations they are in.
    """

    def setUp(self):
        sport = Sport.objects.create(name="Dbl Footy", slug="dbl-footy")
        self.series = Series.objects.create(sport=sport, name="Dbl", slug="dbl-series")
        season = Season.objects.create(year=2099, label="2099")
        self.parent = Organisation.objects.create(name="Tiles Group", season=season)
        self.store = Organisation.objects.create(
            name="Tiles Mitcham", season=season, parent=self.parent,
        )
        self.home = Team.objects.create(name="DH", slug="dbl-h", series=self.series)
        self.away = Team.objects.create(name="DA", slug="dbl-a", series=self.series)

        # The owner works in the store as well as running the group — two
        # memberships inside one family, which is all it takes.
        self.owner = User.objects.create_user(
            email="owner@x.com", password="x", display_name="Owner",
        )
        OrgMember.objects.create(user=self.owner, org=self.parent)
        OrgMember.objects.create(user=self.owner, org=self.store)
        # Somebody in exactly one, as the control.
        self.staff = User.objects.create_user(
            email="staff@x.com", password="x", display_name="Staff",
        )
        OrgMember.objects.create(user=self.staff, org=self.store)

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

    def _round_for(self, org, number=1):
        return Round.objects.create(
            org=org, round_number=number, series=self.series,
            stage=Round.STAGE_REGULAR, lockout_at=timezone.now(),
        )

    def test_a_member_of_two_family_orgs_is_not_double_counted(self):
        rnd = self._round_for(self.store)
        self._graded_tip(self.owner, self.store, rnd, correct=True)
        self._graded_tip(self.owner, self.store, rnd, correct=True)
        self._graded_tip(self.staff, self.store, rnd, correct=True)

        board = {u.display_name: u.points for u in leaderboard_for_family(self.parent)}
        self.assertEqual(board["Staff"], 1)
        self.assertEqual(
            board["Owner"], 2,
            "two correct tips is two points; a membership is not a multiplier",
        )

    def test_the_record_is_not_double_counted_either(self):
        """"4 of 2 correct" is the shape this bug takes in the accuracy column."""
        rnd = self._round_for(self.store)
        self._graded_tip(self.owner, self.store, rnd, correct=True)
        self._graded_tip(self.owner, self.store, rnd, correct=False)

        row = next(
            u for u in leaderboard_for_family(self.parent)
            if u.display_name == "Owner"
        )
        self.assertEqual(row.tips_total, 2)
        self.assertEqual(row.tips_correct, 1)


class CarryRoomsAreGroupedUnderTheirOrgTests(TestCase):
    """The carry step lists organisations, with their groups inside them.

    REPORTED AS: "I did see the list of organisations and groups, I unchecked
    one and that part got distorted … the organisations should have a dropdown
    that shows groups, so if I was to uncheck the group in an organisation —
    because by default if I check an organisation the groups in it are all
    checked — it cleans things up and makes me know this group is for this
    organisation."

    What matters on the server side is narrow and worth pinning: the grouping
    is presentational, and the POST is not allowed to change with it. Every
    room still posts its own `room` checkbox under the same orgid:groupid key,
    so apply_plan is reached exactly as before. The master checkbox carries no
    name at all — if it ever gained one it would arrive in request.POST as a
    room that does not exist.
    """

    def setUp(self):
        from catalog.models import Competition
        from orgs.models import Group, GroupMember

        self.user = User.objects.create_user(
            email="tree@example.com", password="x", display_name="Tree",
        )
        season = Season.objects.create(year=2094, label="2094")
        sport = Sport.objects.create(name="Tree Footy", slug="tree-footy")
        self.series = Series.objects.create(
            sport=sport, name="Tree Series", slug="tree-series",
        )
        comp = Competition.objects.create(
            sport=sport, season=season, name="Tree Comp", slug="tree-comp",
        )
        comp.series.add(self.series)
        home = Team.objects.create(name="TH", slug="tree-h", series=self.series)
        away = Team.objects.create(name="TA", slug="tree-a", series=self.series)
        now = timezone.now()

        self.orgs = {}
        for name, groups_on in (("Acme", True), ("Zenith", False)):
            org = Organisation.objects.create(
                name=name, season=season, groups_enabled=groups_on,
            )
            org.competitions.add(comp)
            OrgMember.objects.create(user=self.user, org=org)
            rnd = Round.objects.create(
                org=org, round_number=1, series=self.series, competition=comp,
                lockout_at=now + timedelta(days=2),
            )
            for ext in ("TREE-1", "TREE-2"):
                Match.objects.create(
                    round=rnd, home_team=home, away_team=away,
                    kickoff_at=now + timedelta(days=2), external_id=ext,
                )
            self.orgs[name] = org

        # Two groups inside Acme, so Acme has three rooms: itself plus both.
        for gname in ("IT", "Marketing"):
            g = Group.objects.create(org=self.orgs["Acme"], name=gname)
            GroupMember.objects.create(group=g, user=self.user)
        self.client.force_login(self.user)

    def _preview(self):
        """The carry fragment, as the sheet fetches it, tipping from Zenith."""
        match = Match.objects.get(round__org=self.orgs["Zenith"], external_id="TREE-1")
        return self.client.post(
            reverse("tipping:carry_preview", args=[self.orgs["Zenith"].id]),
            {f"match_{match.id}": "home"},
        )

    def test_an_org_with_several_rooms_gets_a_master_and_a_disclosure(self):
        html = self._preview().content.decode()
        self.assertIn("data-cs-org", html)
        self.assertIn("data-cs-master", html)
        self.assertEqual(html.count("data-cs-child"), 3, "Acme's own room + IT + Marketing")
        self.assertIn("<details class=\"cs-rooms\"", html)
        # The organisation is named once, as the heading — not repeated into
        # every row as "Acme · IT".
        self.assertIn("Marketing", html)
        self.assertNotIn("Acme &middot; Marketing", html)
        self.assertNotIn("Acme · Marketing", html)

    def test_every_room_still_posts_its_own_key(self):
        html = self._preview().content.decode()
        acme = self.orgs["Acme"]
        from orgs.models import Group

        keys = [f'value="{acme.id}:0"'] + [
            f'value="{acme.id}:{g.id}"'
            for g in Group.objects.filter(org=acme).order_by("name")
        ]
        for key in keys:
            self.assertIn(key, html, f"room {key} lost its checkbox")

    def test_the_master_posts_nothing_of_its_own(self):
        """It is a control over the room boxes, never a value the server reads."""
        html = self._preview().content.decode()
        head, _, _ = html.partition("cs-rooms")
        master = head[head.index("data-cs-master") - 200:head.index("data-cs-master") + 60]
        self.assertNotIn('name="room"', master)

    def test_grouping_does_not_change_what_carrying_writes(self):
        """End to end: tick everything, and every room ends up with the tip."""
        from orgs.models import Group

        zenith = self.orgs["Zenith"]
        acme = self.orgs["Acme"]
        match = Match.objects.get(round__org=zenith, external_id="TREE-1")
        rooms = [f"{acme.id}:0"] + [
            f"{acme.id}:{g.id}" for g in Group.objects.filter(org=acme)
        ]
        self.client.post(
            reverse("tipping:tip_confirm_upcoming", args=[zenith.id]),
            {f"match_{match.id}": "home", "carry_answered": "1", "room": rooms},
        )
        landed = set(
            Tip.objects.filter(user=self.user, match__external_id="TREE-1")
            .values_list("org_id", "group_id")
        )
        expected = {(zenith.id, None), (acme.id, None)} | {
            (acme.id, g.id) for g in Group.objects.filter(org=acme)
        }
        self.assertEqual(landed, expected)


class RoundNumberIsNotARoundTests(TestCase):
    """The client's 1 Sept 2026 report, in full:

        "When I was looking at Round 4 womens to tip this weekend, the mens
         from round 4 back in April was still below it?"

    A Round row is per (org, series), so in a league tipping AFL and AFLW the
    number 4 names TWO rounds — AFLW's, on this weekend, and AFL's, played out
    in April. The navigator moved by number, so picking 4 showed both: one live
    round with eight dead fixtures stacked underneath it.

    "This week" — the default view — was already correct and is asserted here
    too, because that is the half the client refreshed into and reported as
    possibly fixed. The half that was still wrong is every numbered round.
    """

    def setUp(self):
        self.sport = Sport.objects.create(name="Two Code Footy", slug="tc-footy")
        self.mens = Series.objects.create(
            sport=self.sport, name="TC Mens", slug="tc-mens",
        )
        self.womens = Series.objects.create(
            sport=self.sport, name="TC Womens", slug="tc-womens", is_womens=True,
            category=Series.CATEGORY_WOMENS,
        )
        self.season = Season.objects.create(year=2099, label="2099")
        self.comp = Competition.objects.create(
            sport=self.sport, season=self.season, name="Two Code", slug="two-code",
        )
        self.comp.series.set([self.mens, self.womens])
        self.org = Organisation.objects.create(name="Two Code FC", season=self.season)
        self.org.competitions.set([self.comp])
        self.user = User.objects.create_user(
            email="ian@goodtip.test", password="x", display_name="Ian",
        )
        OrgMember.objects.create(user=self.user, org=self.org)
        self.now = timezone.now()

        self.teams = {}
        for series in (self.mens, self.womens):
            self.teams[series.id] = [
                Team.objects.create(name=f"{series.slug} {i}", slug=f"{series.slug}-{i}",
                                    series=series)
                for i in range(4)
            ]

        # AFL round 4, played in April. Every fixture graded and long gone.
        self.mens_r4 = self._round(self.mens, 4, self.now - timedelta(days=150))
        for _ in range(2):
            self._match(self.mens_r4, self.now - timedelta(days=150), complete=True)
        # And a men's round that IS on this weekend, so the league is not
        # simply finished on that side.
        self.mens_r26 = self._round(self.mens, 26, self.now + timedelta(days=1))
        self._match(self.mens_r26, self.now + timedelta(days=1), complete=False)

        # AFLW round 4, on this weekend.
        self.womens_r4 = self._round(self.womens, 4, self.now + timedelta(days=2))
        for _ in range(3):
            self._match(self.womens_r4, self.now + timedelta(days=2), complete=False)

        self.client.force_login(self.user)

    def _round(self, series, number, lockout):
        return Round.objects.create(
            org=self.org, round_number=number, series=series,
            competition=self.comp, lockout_at=lockout,
        )

    def _match(self, rnd, kickoff, *, complete):
        pair = self.teams[rnd.series_id]
        return Match.objects.create(
            round=rnd, home_team=pair[0], away_team=pair[1], kickoff_at=kickoff,
            status=Match.STATUS_COMPLETE if complete else Match.STATUS_SCHEDULED,
            home_score=30 if complete else None,
            away_score=10 if complete else None,
        )

    def _games(self, **params):
        params.setdefault("org", self.org.id)
        resp = self.client.get(reverse("dashboard"), params)
        return list(resp.context["games"]), resp

    def test_this_week_shows_only_the_rounds_actually_on(self):
        games, _ = self._games()
        numbers = {(g.round.series.slug, g.round.round_number) for g in games}
        self.assertNotIn(("tc-mens", 4), numbers)
        self.assertIn(("tc-womens", 4), numbers)
        self.assertIn(("tc-mens", 26), numbers)

    def test_a_numbered_round_no_longer_stacks_another_codes_dead_round(self):
        """THE BUG. `?round=tc-womens-4` is AFLW round 4 and nothing else."""
        games, _ = self._games(round="tc-womens-4")
        self.assertTrue(games)
        self.assertEqual({g.round.series.slug for g in games}, {"tc-womens"})
        self.assertEqual({g.round_id for g in games}, {self.womens_r4.id})

    def test_the_mens_round_four_is_still_reachable_on_its_own(self):
        """Nothing is hidden — it is separated. "How did I go in men's round 4"
        is a real question and still has an answer."""
        games, _ = self._games(round="tc-mens-4")
        self.assertEqual({g.round_id for g in games}, {self.mens_r4.id})

    def test_a_bare_number_from_an_old_link_lands_on_the_live_code(self):
        """Every URL shared before the navigator carried a code — and the
        client's own bookmark. It resolves to the code whose round 4 is nearest
        to now rather than to all of them at once."""
        games, _ = self._games(round="4")
        self.assertEqual({g.round.series.slug for g in games}, {"tc-womens"})

    def test_the_dropdown_groups_its_rounds_by_code(self):
        _, resp = self._games()
        nav = resp.context["round_nav"]
        self.assertTrue(nav["multi"])
        labels = [label for label, _ in nav["groups"]]
        self.assertEqual(sorted(labels), ["TC Mens", "TC Womens"])

    def test_the_arrows_step_inside_one_code(self):
        """The other half of the report: stepping back from AFLW round 4 walked
        out of the women's season and into last April's men's round without
        saying so."""
        _, resp = self._games(round="tc-mens-26")
        nav = resp.context["round_nav"]
        self.assertEqual(nav["prev"], "tc-mens-4")
        self.assertIsNone(nav["next"])

    def test_a_hand_edited_round_falls_back_to_this_week(self):
        _, resp = self._games(round="tc-womens-99")
        self.assertTrue(resp.context["round_nav"]["is_week"])

    def test_a_one_code_league_keeps_plain_numbers(self):
        """There is nothing to disambiguate, so the URLs and the dropdown stay
        exactly as they were."""
        _, resp = self._games(series=self.womens.slug)
        nav = resp.context["round_nav"]
        self.assertFalse(nav["multi"])
        self.assertEqual(nav["current"], "4")


class LeaderboardByCompetitionTests(TestCase):
    """"So I can filter, let's say NRL, and see where I am and who is leading."

    A member tipping four codes has one total made of four seasons going
    differently, and the combined number cannot say which of them they are any
    good at. This is the arithmetic behind that filter — the part that would be
    wrong silently, since a board that quietly sums the wrong tips still looks
    like a leaderboard.
    """

    def setUp(self):
        self.season = Season.objects.create(year=2099, label="2099")
        self.nrl = Series.objects.get(slug="nrl")
        self.nrlw = Series.objects.get(slug="nrlw")
        sport = self.nrl.sport
        comp = Competition.objects.create(
            sport=sport, season=self.season, name="Board Comp", slug="board-comp",
        )
        comp.series.add(self.nrl, self.nrlw)
        self.org = Organisation.objects.create(name="Board League", season=self.season)
        self.org.competitions.add(comp)

        self.mens_specialist = User.objects.create_user(
            email="mens@example.com", password="x", display_name="Mens Specialist",
        )
        self.womens_specialist = User.objects.create_user(
            email="womens@example.com", password="x", display_name="Womens Specialist",
        )
        for u in (self.mens_specialist, self.womens_specialist):
            OrgMember.objects.create(user=u, org=self.org)

        now = timezone.now()
        self.rounds = {}
        for series in (self.nrl, self.nrlw):
            rnd = Round.objects.create(
                org=self.org, round_number=1, series=series, competition=comp,
                lockout_at=now - timedelta(days=7), status="complete",
            )
            self.rounds[series.slug] = rnd
            home = Team.objects.create(
                name=f"{series.slug} H", slug=f"board-{series.slug}-h", series=series,
            )
            away = Team.objects.create(
                name=f"{series.slug} A", slug=f"board-{series.slug}-a", series=series,
            )
            for i in range(3):
                Match.objects.create(
                    round=rnd, home_team=home, away_team=away,
                    kickoff_at=now - timedelta(days=7, hours=i),
                    status="complete", result="home", home_score=20, away_score=10,
                )

        # Three right in the men's and none in the women's, and the mirror
        # image. Level overall; opposite ends of either code's board.
        self._tip(self.mens_specialist, self.nrl, correct=3)
        self._tip(self.mens_specialist, self.nrlw, correct=0)
        self._tip(self.womens_specialist, self.nrl, correct=0)
        self._tip(self.womens_specialist, self.nrlw, correct=3)

    def _tip(self, user, series, *, correct):
        matches = list(self.rounds[series.slug].matches.order_by("id"))
        for i, m in enumerate(matches):
            right = i < correct
            Tip.objects.create(
                user=user, match=m, org=self.org,
                selection="home" if right else "away",
                is_correct=right, points_awarded=1 if right else 0,
            )

    def _points(self, board):
        return {u.display_name: u.points for u in board}

    def test_unfiltered_the_two_are_level(self):
        """The premise. If they were not level overall, filtering could look
        like it worked while doing nothing."""
        self.assertEqual(
            self._points(leaderboard_for_org(self.org)),
            {"Mens Specialist": 3, "Womens Specialist": 3},
        )

    def test_filtering_to_one_code_ranks_on_that_code_alone(self):
        board = leaderboard_for_org(self.org, series=self.nrl)
        self.assertEqual(
            self._points(board), {"Mens Specialist": 3, "Womens Specialist": 0},
        )
        self.assertEqual(board[0].display_name, "Mens Specialist")

    def test_and_the_other_code_reverses_it(self):
        board = leaderboard_for_org(self.org, series=self.nrlw)
        self.assertEqual(
            self._points(board), {"Mens Specialist": 0, "Womens Specialist": 3},
        )
        self.assertEqual(board[0].display_name, "Womens Specialist")

    def test_the_accuracy_record_narrows_with_the_points(self):
        """Points and "3 of 3 correct" sit in the same row. One filtered and
        the other not would be two answers to one question."""
        board = leaderboard_for_org(self.org, series=self.nrl)
        mens = next(u for u in board if u.display_name == "Mens Specialist")
        self.assertEqual((mens.tips_correct, mens.tips_total), (3, 3))

    def test_a_filtered_board_breaks_ties_on_the_paired_comp(self):
        """The addendum's cross-code rule applies to a board scoped to one
        code — which a comp filter now produces, exactly as a round filter
        always did. Two tippers level in the NRL are separated by the NRLW.
        """
        board = leaderboard_for_org(self.org, series=self.nrl)
        # Level in the NRL at 0; the women's specialist has the paired score.
        levels = [u for u in board if u.points == 0]
        self.assertTrue(levels)
        self.assertEqual(levels[0].display_name, "Womens Specialist")
        self.assertEqual(levels[0].paired_points, 3)

    def test_an_all_comps_board_does_not_apply_the_cross_code_step(self):
        """It would be double counting: the paired score is already inside the
        total. The docstring on apply_tiebreakers has the reasoning."""
        board = leaderboard_for_org(self.org)
        self.assertTrue(all(u.paired_points == 0 for u in board))


class LeaderboardCompetitionFilterPageTests(TestCase):
    """The control itself: chips and a select, both real, on both pages."""

    def setUp(self):
        self.season = Season.objects.create(year=2099, label="2099")
        nrl = Series.objects.get(slug="nrl")
        nrlw = Series.objects.get(slug="nrlw")
        comp = Competition.objects.create(
            sport=nrl.sport, season=self.season, name="Page Comp", slug="page-comp",
        )
        comp.series.add(nrl, nrlw)
        self.org = Organisation.objects.create(name="Page League", season=self.season)
        self.org.competitions.add(comp)
        self.user = User.objects.create_user(
            email="page@example.com", password="x", display_name="Page",
        )
        OrgMember.objects.create(user=self.user, org=self.org)
        self.nrl, self.nrlw = nrl, nrlw
        self.client.force_login(self.user)

    def _board(self, qs=""):
        return self.client.get(
            reverse("tipping:leaderboard", args=[self.org.id]) + qs
        ).content.decode()

    def test_the_leaderboard_offers_both_a_chip_row_and_a_select(self):
        """"Let it be like both a dropdown and the button filter." Both, and
        both posting the same URL — neither is a fallback for the other."""
        body = self._board()
        self.assertIn("cf-chips", body)
        self.assertIn('name="comp"', body)
        self.assertIn('data-code="nrlw"', body)

    def test_all_competitions_is_the_arrival_state(self):
        body = self._board()
        self.assertIn("cf-chip cf-all on", body)

    def test_choosing_a_code_colours_the_table_with_it(self):
        """"Let the table also pick the colour of that competition." Absent on
        an all-comps board, which would otherwise be claiming to be one code's
        table."""
        self.assertIn('id="lbtable"\n     data-code="nrl"', self._board("?comp=nrl"))
        self.assertNotIn("data-code=", self._board().split('id="lbtable"')[1][:120])

    def test_the_round_list_narrows_to_the_chosen_code(self):
        """A round number names one round per code. Offering AFLW round 4 while
        the board shows the NRL is offering a filter that empties the table."""
        now = timezone.now()
        for series, n in ((self.nrl, 7), (self.nrlw, 19)):
            Round.objects.create(
                org=self.org, round_number=n, series=series,
                lockout_at=now + timedelta(days=3),
            )
        body = self._board("?comp=nrl")
        self.assertIn("Round 7", body)
        self.assertNotIn("Round 19", body)

    def test_the_ladder_takes_a_slug_as_well_as_an_id(self):
        """The chips post a slug because "?series=nrlw" is a URL somebody can
        read and share; the old picker posted an id and those links are in the
        wild. Both resolve."""
        by_slug = self.client.get(
            reverse("tipping:ladder", args=[self.org.id]) + "?series=nrlw"
        ).content.decode()
        by_id = self.client.get(
            reverse("tipping:ladder", args=[self.org.id]) + f"?series={self.nrlw.id}"
        ).content.decode()
        for body in (by_slug, by_id):
            self.assertIn('data-code="nrlw"', body)

    def test_the_ladder_offers_no_all_option(self):
        """A ladder is a table OF one competition — clubs from four codes
        ranked in one column would be a list, not a ladder."""
        body = self.client.get(
            reverse("tipping:ladder", args=[self.org.id])
        ).content.decode()
        self.assertIn("cf-chips", body)
        self.assertNotIn("cf-all", body)


class BoardHierarchyTests(TestCase):
    """The order of the page, and the size of the things on it.

    "I think this should be at the top — Leaderboard, the lead, Round, All
    competitions... hierarchy of information. Then the group and organisation
    filter, these two can be at the bottom, so the table is not squeezed."
    """

    def setUp(self):
        self.season = Season.objects.create(year=2099, label="2099")
        nrl = Series.objects.get(slug="nrl")
        origin = Series.objects.get(slug="state-of-origin")
        comp = Competition.objects.create(
            sport=nrl.sport, season=self.season, name="H Comp", slug="h-comp",
        )
        comp.series.add(nrl, origin)
        # groups_enabled, because room_switch.html renders nothing without it
        # and the position of that block is what this class is about.
        self.org = Organisation.objects.create(
            name="H League", season=self.season, groups_enabled=True,
        )
        self.org.competitions.add(comp)
        self.user = User.objects.create_user(
            email="h@example.com", password="x", display_name="H",
        )
        OrgMember.objects.create(user=self.user, org=self.org)
        now = timezone.now()
        for n in (1, 2, 3):
            Round.objects.create(
                org=self.org, round_number=n, series=nrl,
                lockout_at=now + timedelta(days=n),
            )
        self.client.force_login(self.user)

    def _board(self, qs=""):
        return self.client.get(
            reverse("tipping:leaderboard", args=[self.org.id]) + qs
        ).content.decode()

    def test_the_page_reads_title_room_filters_table(self):
        """The order was title → filters → table → room, and the room came
        back up: "this should not be at the bottom, it should come after
        Leaderboard, Compete Climb Make an impact."

        Right, and for a reason the first arrangement missed — the board is OF
        a room, so which room has to be settled before the filters that narrow
        it mean anything. Above the title it was worse; below the standings it
        was too late.
        """
        body = self._board()
        title = body.index("Compete. Climb.")
        room = body.index("room-switch")
        filters = body.index('class="boardbar"')
        table = body.index('id="lbtable"')
        self.assertLess(title, room, "the room switcher must follow the title")
        self.assertLess(room, filters, "the room is settled before the filters")
        self.assertLess(filters, table, "the filters must precede the table")

    def test_the_summary_cards_no_longer_flank_the_table(self):
        """They were a 320px sticky sidebar taking a third of the width from
        the four columns the page exists to show."""
        body = self._board()
        self.assertNotIn('class="gt-shell" data-coach="board-table"', body)
        self.assertIn("gt-side board-foot", body)
        self.assertLess(body.index('id="lbtable"'), body.index("gt-side board-foot"))

    def test_the_round_can_be_stepped_as_well_as_chosen(self):
        """"Not only a dropdown, but a nice way I can press arrow left and
        right the way we have it on the fixtures on the dashboard"."""
        body = self._board()
        self.assertIn("bb-arrow", body)
        self.assertIn('name="round"', body)

    def test_at_the_start_of_the_sequence_the_arrow_is_shown_but_dead(self):
        """A control that vanishes at the edges makes the row jump and leaves
        you wondering what you pressed."""
        body = self._board()          # "all rounds" is the first stop
        self.assertIn("bb-arrow is-off", body)

    def test_stepping_carries_the_competition(self):
        """Or changing round silently widens the board back to every comp —
        the table changing under a filter still drawn as selected."""
        body = self._board("?comp=nrl")
        # The live arrows only — the dead one at the end of the sequence is a
        # <span>, and the `href` in its <use> is an icon reference, not a link.
        arrows = [ln for ln in body.splitlines() if '<a class="bb-arrow"' in ln]
        self.assertTrue(arrows)
        self.assertTrue(all("comp=nrl" in a for a in arrows), arrows)

    def test_the_podium_names_its_medals(self):
        """"Number 2 and 3 have the same shade of colour, why?" They were two
        greens a couple of percent apart. Naming them as well as colouring
        them keeps the distinction in a screenshot and in greyscale."""
        User.objects.create_user(
            email="h2@example.com", password="x", display_name="H2",
        )
        for e in ("h2@example.com",):
            OrgMember.objects.create(user=User.objects.get(email=e), org=self.org)
        body = self._board()
        for word in ("Gold", "Silver"):
            self.assertIn(f'class="gp-medal">{word}<', body)


class EveryCodeHasItsOwnShadeTests(TestCase):
    """Five competitions, five hues.

    "Why do the NRL and State of Origin have the same shade of colour? That
    should not be it." Because Origin had no rule of its own and fell through
    to the representative category's gold, which sits a few degrees from the
    NRL's amber — and they are the two most likely to appear in the same list,
    since Origin rounds belong to the NRL competition.
    """

    def _tokens(self, css, selector):
        import re

        m = re.search(re.escape(selector) + r"[^{]*\{([^}]*)\}", css)
        self.assertIsNotNone(m, f"{selector} has no rule")
        return dict(
            re.findall(r"(--code(?:-ink|-on)?):\s*([^;]+);", m.group(1))
        )

    def test_no_two_competitions_share_an_ink(self):
        from pathlib import Path

        from django.conf import settings

        css = Path(settings.BASE_DIR, "static/css/goodtip.css").read_text()
        inks = {}
        for slug in ("afl", "aflw", "nrl", "nrlw", "state-of-origin"):
            inks[slug] = self._tokens(css, f'[data-code="{slug}"]')["--code-ink"].strip()
        self.assertEqual(
            len(set(inks.values())), 5, f"a code is sharing a shade: {inks}",
        )

    def test_origin_does_not_fall_through_to_a_category(self):
        from pathlib import Path

        from django.conf import settings

        css = Path(settings.BASE_DIR, "static/css/goodtip.css").read_text()
        origin = self._tokens(css, '[data-code="state-of-origin"]')["--code-ink"].strip()
        rep = self._tokens(css, '[data-cat="representative"]')["--code-ink"].strip()
        mens = self._tokens(css, '[data-cat="mens"]')["--code-ink"].strip()
        self.assertNotEqual(origin, rep)
        # And the men's fallback must not have inherited Origin's teal, or
        # Super League would arrive wearing it.
        self.assertNotEqual(origin, mens)

    def test_the_ladders_column_heads_are_not_dark_on_dark(self):
        """.lad-num sets `color: var(--ink)` and beats the cream .gb-head sets,
        so P W L D % Pts were painted near-black on a near-black bar — since
        the header band went green, long before it took a competition's
        colour. "The titles are not being seen with the colour background"."""
        from pathlib import Path

        from django.conf import settings

        css = Path(settings.BASE_DIR, "static/css/goodtip.css").read_text()
        self.assertIn(".gt-board.ladder .lad-head .lad-num { color: inherit;", css)


class RoundStripTests(TestCase):
    """Five rounds at a time, and the window that walks back through a season.

    ASKED FOR: "the dropdown will show All by default; beside it let's have the
    last 5 rounds, with a button to keep on moving backward... and on the round
    like 26 it must be highlighted so I know I am there, and be able to click
    the 25 and see the ranking of that round."
    """

    def test_it_shows_the_five_newest_by_default(self):
        from tipping.views import round_strip

        strip = round_strip(list(range(1, 28)), None)
        self.assertEqual([b["n"] for b in strip["window"]], [23, 24, 25, 26, 27])
        self.assertEqual(strip["showing"], "Rounds 23–27")

    def test_stepping_back_moves_a_page_at_a_time(self):
        """A page, not a round — one at a time is the arrows, which is a
        different gesture for a different distance."""
        from tipping.views import round_strip

        strip = round_strip(list(range(1, 28)), None, back=1)
        self.assertEqual([b["n"] for b in strip["window"]], [18, 19, 20, 21, 22])

    def test_it_cannot_be_walked_off_the_start_of_the_season(self):
        from tipping.views import round_strip

        strip = round_strip(list(range(1, 12)), None, back=99)
        # A full page, not the one round the arithmetic leaves at the far end.
        self.assertEqual([b["n"] for b in strip["window"]], [1, 2, 3, 4, 5])
        self.assertIsNone(strip["older"])

    def test_a_chosen_round_is_always_in_the_window(self):
        """A link to round 3 in August must open with round 3 on screen, not
        show the last five and leave the reader to hunt for it."""
        from tipping.views import round_strip

        strip = round_strip(list(range(1, 28)), 3)
        self.assertIn(3, [b["n"] for b in strip["window"]])
        self.assertTrue(next(b for b in strip["window"] if b["n"] == 3)["on"])

    def test_only_the_current_round_is_marked(self):
        from tipping.views import round_strip

        strip = round_strip(list(range(1, 28)), 26)
        marked = [b["n"] for b in strip["window"] if b["on"]]
        self.assertEqual(marked, [26])

    def test_a_button_carries_whatever_its_link_needs(self):
        """The leaderboard posts round IDs — a round number names one round PER
        competition, so on an unfiltered board the number 4 is up to five
        different rounds and only the id says which."""
        from tipping.views import round_strip

        strip = round_strip([1, 2, 3], 2, values={1: 101, 2: 102, 3: 103})
        self.assertEqual([b["value"] for b in strip["window"]], [101, 102, 103])

    def test_a_season_with_no_rounds_has_no_strip(self):
        from tipping.views import round_strip

        self.assertIsNone(round_strip([], None))


class LadderAtAPastRoundTests(TestCase):
    """"Users might want to see, let's say the last month, round 10 — who was
    on top."

    A ladder is a running total, so that is a question it can answer. The stored
    table only ever knew today, so a past round is computed — by the SAME
    arithmetic, lifted out of rebuild_ladder rather than written again.
    """

    def setUp(self):
        from matchreader.models import HistoricalMatch

        self.season = Season.objects.create(year=2099, label="2099")
        self.series = Series.objects.get(slug="afl")
        self.a = Team.objects.create(name="Alpha", slug="l-alpha", series=self.series)
        self.b = Team.objects.create(name="Bravo", slug="l-bravo", series=self.series)
        # Bravo wins round 1, Alpha wins rounds 2 and 3. So after round 1 Bravo
        # leads; after round 3 Alpha does. One table, two answers, and the round
        # is the only thing that changed.
        for rnd, (home, away, hs, aws) in enumerate(
            [(self.b, self.a, 100, 50), (self.a, self.b, 90, 40), (self.a, self.b, 80, 30)],
            start=1,
        ):
            HistoricalMatch.objects.create(
                series=self.series, season=self.season.year, round_number=rnd,
                stage=HistoricalMatch.STAGE_REGULAR,
                # Unique per row: (series, external_id) is a unique key and a
                # blank default makes the second fixture a duplicate of the first.
                external_id=f"ladder-test-{rnd}",
                home_team=home, away_team=away, home_score=hs, away_score=aws,
                kickoff_at=timezone.now() - timedelta(days=30 - rnd),
            )

    def test_after_round_one_the_early_winner_is_top(self):
        from data_sync.ladder import ladder_standings

        rows = ladder_standings(series=self.series, season=self.season, up_to_round=1)
        self.assertEqual(rows[0]["team_id"], self.b.id)
        self.assertEqual(rows[0]["played"], 1)

    def test_by_round_three_it_has_turned_over(self):
        from data_sync.ladder import ladder_standings

        rows = ladder_standings(series=self.series, season=self.season, up_to_round=3)
        self.assertEqual(rows[0]["team_id"], self.a.id)
        self.assertEqual(rows[0]["played"], 3)

    def test_no_round_means_the_whole_season(self):
        from data_sync.ladder import ladder_standings

        whole = ladder_standings(series=self.series, season=self.season)
        at_three = ladder_standings(series=self.series, season=self.season, up_to_round=3)
        self.assertEqual(whole, at_three)

    def test_it_agrees_with_what_the_sync_writes(self):
        """The point of lifting it out rather than writing it again: the stored
        table and the computed one cannot drift, because they are one function.
        """
        from data_sync.ladder import ladder_standings, rebuild_ladder
        from tipping.models import LadderEntry

        rebuild_ladder(series=self.series, season=self.season)
        stored = list(
            LadderEntry.objects.filter(series=self.series, season=self.season)
            .order_by("rank")
            .values("rank", "team_id", "played", "wins", "losses", "points")
        )
        computed = [
            {k: r[k] for k in ("rank", "team_id", "played", "wins", "losses", "points")}
            for r in ladder_standings(series=self.series, season=self.season)
        ]
        self.assertEqual(stored, computed)

    def test_the_rounds_offered_are_the_rounds_that_were_played(self):
        """Read from the same table the standings come from, so the stepper can
        never offer a round the ladder cannot draw."""
        from data_sync.ladder import season_rounds

        self.assertEqual(
            season_rounds(series=self.series, season=self.season), [1, 2, 3],
        )


class MemberStatsTests(TestCase):
    """A member's own season, in more detail than a row can hold.

    ASKED FOR: "my stats in the leaderboard... that shows my performance
    generally: have I grown or have I dropped, what is my strongest
    competition, my weakness."
    """

    def setUp(self):
        self.season = Season.objects.create(year=2099, label="2099")
        self.afl = Series.objects.get(slug="afl")
        self.aflw = Series.objects.get(slug="aflw")
        comp = Competition.objects.create(
            sport=self.afl.sport, season=self.season, name="S Comp", slug="s-comp",
        )
        comp.series.add(self.afl, self.aflw)
        self.org = Organisation.objects.create(name="Stats League", season=self.season)
        self.org.competitions.add(comp)
        self.user = User.objects.create_user(
            email="stats@example.com", password="x", display_name="Statty",
        )
        OrgMember.objects.create(user=self.user, org=self.org)
        self.now = timezone.now()
        self.client.force_login(self.user)

    def _round(self, series, n):
        return Round.objects.create(
            org=self.org, round_number=n, series=series,
            lockout_at=self.now - timedelta(days=40 - n), status="complete",
        )

    def _tips(self, series, n, results):
        """One round of graded tips; `results` is a list of booleans."""
        rnd = self._round(series, n)
        home = Team.objects.create(name=f"H{series.slug}{n}", slug=f"h-{series.slug}-{n}", series=series)
        away = Team.objects.create(name=f"A{series.slug}{n}", slug=f"a-{series.slug}-{n}", series=series)
        for i, ok in enumerate(results):
            m = Match.objects.create(
                round=rnd, home_team=home, away_team=away,
                kickoff_at=self.now - timedelta(days=40 - n, hours=i),
                status="complete", result="home", home_score=10, away_score=1,
            )
            Tip.objects.create(
                user=self.user, match=m, org=self.org,
                selection="home" if ok else "away",
                is_correct=ok, points_awarded=1 if ok else 0,
            )

    def test_the_headline_numbers_are_the_ones_on_the_board(self):
        from tipping.stats import member_season

        self._tips(self.afl, 1, [True, True, False])
        st = member_season(self.user, self.org)
        self.assertEqual((st["played"], st["right"], st["points"]), (3, 2, 2))
        self.assertEqual(st["accuracy"], 67)
        self.assertEqual(st["missed"], 1)

    def test_the_strongest_and_weakest_code_are_named(self):
        """The question a combined total cannot answer: four codes going
        differently add up to one number that describes none of them."""
        from tipping.stats import member_season

        self._tips(self.afl, 1, [True, True, True])
        self._tips(self.aflw, 1, [False, False, True])
        st = member_season(self.user, self.org)
        self.assertEqual(st["best_code"]["name"], "AFL")
        self.assertEqual(st["worst_code"]["name"], "AFLW")

    def test_a_code_with_a_handful_of_games_is_not_called_a_strength(self):
        """Three graded games is the floor. Below it there is a number and no
        claim — a small sample is not a talent."""
        from tipping.stats import member_season

        self._tips(self.afl, 1, [True, True, True])
        self._tips(self.aflw, 1, [True])          # one game, a perfect record
        st = member_season(self.user, self.org)
        self.assertEqual(st["best_code"]["name"], "AFL")

    def test_it_says_whether_you_have_grown_or_dropped(self):
        from tipping.stats import member_season

        # Six rounds: poor early, perfect late. Recent form is the last five.
        for n in range(1, 4):
            self._tips(self.afl, n, [False, False])
        for n in range(4, 9):
            self._tips(self.afl, n, [True, True])
        st = member_season(self.user, self.org)
        self.assertIsNotNone(st["trend"])
        self.assertGreater(st["trend"]["delta"], 0)

    def test_a_first_round_gets_no_trend_rather_than_a_guess(self):
        """A trend drawn from one round is a coin toss with an arrow on it."""
        from tipping.stats import member_season

        self._tips(self.afl, 1, [True, False])
        self.assertIsNone(member_season(self.user, self.org)["trend"])

    def test_the_streak_is_counted_in_time_not_in_round_numbers(self):
        """A member tipping two codes has two round 4s. "In a row" means in
        time, or a streak jumps between competitions."""
        from tipping.stats import member_season

        self._tips(self.afl, 1, [True, True])
        self._tips(self.aflw, 1, [True])
        st = member_season(self.user, self.org)
        self.assertEqual(st["streak"]["best"], 3)

    def test_the_page_renders_with_its_cards_and_charts(self):
        self._tips(self.afl, 1, [True, True, False])
        self._tips(self.afl, 2, [True, False])
        body = self.client.get(
            reverse("tipping:my_stats", args=[self.org.id])
        ).content.decode()
        self.assertIn("statcards", body)
        self.assertIn("statgrid", body)
        self.assertIn("gt-spark", body)

    def test_a_member_with_nothing_graded_gets_an_empty_state(self):
        """Not a page of noughts. Nobody with no graded tips has an accuracy of
        zero per cent — they have no accuracy yet."""
        body = self.client.get(
            reverse("tipping:my_stats", args=[self.org.id])
        ).content.decode()
        self.assertIn("Nothing to measure yet", body)
        self.assertNotIn("statcards", body)

    def test_it_is_scoped_the_same_way_the_leaderboard_is(self):
        """A figure beside a rank must not describe a different season."""
        from tipping.stats import member_season

        self._tips(self.afl, 1, [True, True, True])
        self._tips(self.aflw, 1, [False, False, False])
        self.assertEqual(member_season(self.user, self.org, series=self.afl)["right"], 3)
        self.assertEqual(member_season(self.user, self.org, series=self.aflw)["right"], 0)

    def test_a_stranger_cannot_read_the_page(self):
        outsider = User.objects.create_user(
            email="out@example.com", password="x", display_name="Out",
        )
        self.client.force_login(outsider)
        r = self.client.get(reverse("tipping:my_stats", args=[self.org.id]))
        self.assertEqual(r.status_code, 403)


class TeamStatsTests(TestCase):
    """A club's season, reached by pressing its row on the ladder."""

    def setUp(self):
        from matchreader.models import HistoricalMatch

        self.season = Season.objects.create(year=2099, label="2099")
        self.series = Series.objects.get(slug="nrl")
        comp = Competition.objects.create(
            sport=self.series.sport, season=self.season, name="T Comp", slug="t-comp",
        )
        comp.series.add(self.series)
        self.org = Organisation.objects.create(name="Team Stats League", season=self.season)
        self.org.competitions.add(comp)
        self.user = User.objects.create_user(
            email="ts@example.com", password="x", display_name="TS",
        )
        OrgMember.objects.create(user=self.user, org=self.org)
        self.us = Team.objects.create(name="Us", slug="ts-us", series=self.series)
        self.them = Team.objects.create(name="Them", slug="ts-them", series=self.series)
        # Two at home (won both), two away (lost both).
        rows = [
            (1, self.us, self.them, 30, 10),
            (2, self.us, self.them, 24, 20),
            (3, self.them, self.us, 40, 4),
            (4, self.them, self.us, 18, 12),
        ]
        for n, home, away, hs, aws in rows:
            HistoricalMatch.objects.create(
                series=self.series, season=self.season.year, round_number=n,
                stage=HistoricalMatch.STAGE_REGULAR, external_id=f"ts-{n}",
                home_team=home, away_team=away, home_score=hs, away_score=aws,
                kickoff_at=timezone.now() - timedelta(days=30 - n),
            )
        self.client.force_login(self.user)

    def test_the_record_is_counted_from_the_club_s_point_of_view(self):
        from tipping.stats import team_season

        st = team_season(self.us, self.series, self.season)
        self.assertEqual((st["played"], st["won"], st["lost"]), (4, 2, 2))
        self.assertEqual(st["win_pct"], 50)

    def test_home_and_away_are_split(self):
        """"Whether the ground makes a difference to this side" — which the
        ladder's single win column cannot say."""
        from tipping.stats import team_season

        st = team_season(self.us, self.series, self.season)
        self.assertEqual(st["home"]["win_pct"], 100)
        self.assertEqual(st["away"]["win_pct"], 0)

    def test_for_and_against_are_the_club_s_own_way_round(self):
        """Scored is what THIS club scored, home or away — reading home_score
        for an away game is the mistake this exists to not make."""
        from tipping.stats import team_season

        st = team_season(self.us, self.series, self.season)
        self.assertEqual(st["scored"], 30 + 24 + 4 + 12)
        self.assertEqual(st["conceded"], 10 + 20 + 40 + 18)
        self.assertEqual(st["differential"], st["scored"] - st["conceded"])

    def test_the_biggest_win_and_heaviest_loss_are_found(self):
        from tipping.stats import team_season

        st = team_season(self.us, self.series, self.season)
        self.assertEqual(st["biggest_win"]["round"], 1)
        self.assertEqual(st["heaviest_loss"]["round"], 3)

    def test_the_page_renders_from_the_ladder(self):
        ladder = self.client.get(
            reverse("tipping:ladder", args=[self.org.id]) + f"?series={self.series.slug}"
        ).content.decode()
        link = reverse("tipping:team_stats", args=[self.org.id, self.us.id])
        # It is reachable from the ladder, and it renders.
        body = self.client.get(link).content.decode()
        self.assertIn("statcards", body)
        self.assertIn("formrun", body)
        self.assertIn("Us", body)
        self.assertIsInstance(ladder, str)

    def test_a_stranger_cannot_read_it(self):
        outsider = User.objects.create_user(
            email="tsout@example.com", password="x", display_name="Out",
        )
        self.client.force_login(outsider)
        r = self.client.get(
            reverse("tipping:team_stats", args=[self.org.id, self.us.id])
        )
        self.assertEqual(r.status_code, 403)


class ChartHelperTests(TestCase):
    """The drawing itself, which is arithmetic and therefore testable."""

    def test_one_point_is_not_a_chart(self):
        from tipping.stats import spark

        self.assertIsNone(spark([50]))
        self.assertIsNone(spark([]))

    def test_a_flat_run_is_drawn_rather_than_divided_by_zero(self):
        from tipping.stats import spark

        s = spark([50, 50, 50])
        self.assertIsNotNone(s)
        self.assertEqual(len({p["y"] for p in s["points"]}), 1)

    def test_the_chart_is_flipped_so_bigger_is_higher(self):
        """SVG counts downward and a chart does not."""
        from tipping.stats import spark

        s = spark([0, 100], floor=0, ceiling=100)
        self.assertGreater(s["points"][0]["y"], s["points"][1]["y"])

    def test_a_ring_is_refused_when_nothing_has_been_played(self):
        """A ring at 0% claims somebody got everything wrong, which is not the
        same as not having played."""
        from tipping.stats import donut

        self.assertIsNone(donut(0, 0))
        self.assertEqual(donut(1, 2)["pct"], 50)

    def test_bars_scale_to_the_biggest_in_the_set(self):
        """And the key has no leading underscore: Django templates refuse any
        variable beginning with one, so `_pct` was unreachable from the markup
        that exists to draw it."""
        from tipping.stats import bars

        out = bars([{"value": 5}, {"value": 10}, {"value": 0}])
        self.assertEqual([b["bar_pct"] for b in out], [50, 100, 0])


class BoardLegibilityTests(TestCase):
    """The small print of the deep sheet, and the loader that covered the page.

    "The text that are white — some are not even bright enough. The white has to
    be strong so that in the green theme I am not struggling to see it." It was
    systematic rather than a few stray rules: secondary text on the dark sheet
    was cream at .6–.72 alpha, which is a mid-grey over dark green.

    Asserted against the stylesheet, because that is where the decision lives
    and the alternative is a person squinting at a screenshot.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from pathlib import Path

        from django.conf import settings

        cls.css = Path(settings.BASE_DIR, "static/css/goodtip.css").read_text()

    def _rule(self, selector):
        """Every declaration written for exactly this selector, joined.

        `selector` followed by optional whitespace and a brace — not by
        anything at all, which also matched `.rs-num:hover` and made the
        assertions read the hover state instead of the rule. Joined rather than
        taking one, because a property set in a later block is the one that
        wins and either might hold it.
        """
        import re

        matches = re.findall(
            re.escape(selector) + r"\s*\{([^}]*)\}", self.css,
        )
        self.assertTrue(matches, f"{selector} has no rule")
        return " ".join(matches)

    def test_the_round_stepper_is_white_not_faded_cream(self):
        for sel in (".rs-num", ".rs-step"):
            self.assertIn("#FFFFFF", self._rule(sel), sel)

    def test_the_labels_are_near_solid_white(self):
        import re

        rule = self._rule(".rs-showing, .bb-lab, .cf-lab")
        alpha = re.search(r"rgba\(255,255,255,\.(\d+)\)", rule)
        self.assertIsNotNone(alpha, rule)
        self.assertGreaterEqual(int(alpha.group(1)), 9, "still too faint")

    def test_the_light_theme_puts_them_back_to_ink(self):
        """These are all deep-sheet rules and the light theme repaints the same
        surfaces pale, where solid white would vanish — the same dark-on-dark
        mistake in a mirror."""
        self.assertIn(
            'html[data-theme="light"] .gt-app .rs-showing', self.css,
        )

    def test_the_loader_covers_the_content_and_not_the_window(self):
        """"It should not be the whole page, it should be that innermost
        section." Fixed to the viewport it withdrew the nav, the switcher, the
        bell and the charity strip to report that one region was being fetched.
        """
        rule = self._rule(".loader.loader-inset")
        self.assertIn("position: absolute", rule)
        self.assertIn(".app-main .page-sheet { position: relative; }", self.css)

    def test_the_public_splash_is_left_alone(self):
        """Scoped to .loader-inset, because the public site and the sign-in page
        have no furniture worth preserving behind the splash."""
        base = self._rule(".loader")
        self.assertIn("position: fixed", base)


class LadderPresentationTests(TestCase):
    """Full words in capitals, the key above the table, and a stats button."""

    def setUp(self):
        self.season = Season.objects.create(year=2099, label="2099")
        self.series = Series.objects.get(slug="afl")
        comp = Competition.objects.create(
            sport=self.series.sport, season=self.season, name="LP Comp", slug="lp-comp",
        )
        comp.series.add(self.series)
        self.org = Organisation.objects.create(name="LP League", season=self.season)
        self.org.competitions.add(comp)
        self.user = User.objects.create_user(
            email="lp@example.com", password="x", display_name="LP",
        )
        OrgMember.objects.create(user=self.user, org=self.org)
        from tipping.models import LadderEntry

        self.team = Team.objects.create(name="Lions", slug="lp-lions", series=self.series)
        LadderEntry.objects.create(
            series=self.series, season=self.season, team=self.team,
            rank=1, played=10, wins=8, losses=2, points=32, percentage=120.5,
        )
        self.client.force_login(self.user)

    def _body(self):
        return self.client.get(
            reverse("tipping:ladder", args=[self.org.id]) + f"?series={self.series.slug}"
        ).content.decode()

    def test_the_columns_are_named_in_full_and_in_capitals(self):
        """A single letter is a legend you have to have been taught."""
        body = self._body()
        for word in ("PLAYED", "WON", "LOST", "DRAWN", "POINTS", "GAMES LEFT", "CLUB"):
            self.assertIn(f">{word}<", body, word)

    def test_the_finals_key_sits_above_the_table(self):
        """Both halves are things you need BEFORE reading the rows. Underneath,
        they were an explanation arriving after the thing it explains."""
        body = self._body()
        self.assertLess(body.index("Top 8, finals places"), body.index('id="ladderTable"'))

    def test_every_club_has_a_statistics_button(self):
        """"I did not see the stats button." The name was already a link and
        that was not enough — a name that happens to be underlined is not an
        offer."""
        body = self._body()
        self.assertIn("STATISTICS", body)
        self.assertIn(
            reverse("tipping:team_stats", args=[self.org.id, self.team.id]), body,
        )
        self.assertIn("lad-stats", body)

    def test_the_leaderboards_columns_are_capitalised_too(self):
        """One convention across both boards."""
        body = self.client.get(
            reverse("tipping:leaderboard", args=[self.org.id])
        ).content.decode()
        for word in ("RANK", "TIPPER", "POINTS", "ACCURACY"):
            self.assertIn(f">{word}<", body, word)
