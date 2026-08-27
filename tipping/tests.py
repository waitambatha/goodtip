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
