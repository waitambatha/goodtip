import re
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import LoginCode
from .validators import PasswordComplexityValidator

User = get_user_model()


class PasswordComplexityValidatorTests(TestCase):
    def setUp(self):
        self.validator = PasswordComplexityValidator()

    def test_accepts_complex_password(self):
        self.validator.validate("Str0ng!pass")

    def test_rejects_missing_uppercase(self):
        with self.assertRaisesMessage(ValidationError, "one uppercase letter"):
            self.validator.validate("str0ng!pass")

    def test_rejects_missing_lowercase(self):
        with self.assertRaisesMessage(ValidationError, "one lowercase letter"):
            self.validator.validate("STR0NG!PASS")

    def test_rejects_missing_number(self):
        with self.assertRaisesMessage(ValidationError, "one number"):
            self.validator.validate("Strong!pass")

    def test_rejects_missing_symbol(self):
        with self.assertRaisesMessage(ValidationError, "one symbol"):
            self.validator.validate("Str0ngpass")

    def test_reports_all_missing_classes_at_once(self):
        try:
            self.validator.validate("password")
        except ValidationError as e:
            msg = str(e)
            self.assertIn("one uppercase letter", msg)
            self.assertIn("one number", msg)
            self.assertIn("one symbol", msg)
        else:
            self.fail("Expected ValidationError")


class SignupPasswordEnforcementTests(TestCase):
    def signup(self, password):
        return self.client.post(reverse("accounts:signup"), {
            "display_name": "Test Tipper",
            "email": "tipper@example.com",
            "password1": password,
            "password2": password,
        })

    def test_weak_password_rejected(self):
        for weak in ["password1", "alllowercase!1", "SHOUTING!1", "NoSymbols123", "Sh0r!t"]:
            resp = self.signup(weak)
            self.assertEqual(resp.status_code, 200, weak)
            self.assertTrue(resp.context["form"].errors.get("password1"), weak)
            self.assertFalse(User.objects.filter(email="tipper@example.com").exists(), weak)

    def test_mismatched_passwords_rejected(self):
        resp = self.client.post(reverse("accounts:signup"), {
            "display_name": "Test Tipper",
            "email": "tipper@example.com",
            "password1": "Str0ng!pass",
            "password2": "Different!1",
        })
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.context["form"].errors.get("password2"))

    def test_complex_password_creates_account_pending_verification(self):
        """Signup creates the account but does NOT sign anyone in — the address
        has to prove itself with an emailed code first."""
        resp = self.signup("Str0ng!pass")
        self.assertRedirects(resp, reverse("accounts:verify"))
        user = User.objects.get(email="tipper@example.com")
        self.assertTrue(user.check_password("Str0ng!pass"))
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertTrue(
            LoginCode.objects.filter(
                user=user, purpose=LoginCode.PURPOSE_SIGNUP
            ).exists()
        )


class LoginCodeModelTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="coded@example.com", password="Str0ng!pass", display_name="Coded"
        )

    def test_issue_stores_a_hash_not_the_code(self):
        row, code = LoginCode.issue(self.user)
        self.assertEqual(len(code), 6)
        self.assertTrue(code.isdigit())
        self.assertNotIn(code, row.code_hash)

    def test_correct_code_verifies_once_only(self):
        row, code = LoginCode.issue(self.user)
        self.assertTrue(row.verify(code))
        self.assertIsNotNone(row.consumed_at)
        # Replay must fail — a consumed code is no longer usable.
        self.assertFalse(row.verify(code))

    def test_attempts_are_capped(self):
        row, code = LoginCode.issue(self.user)
        for _ in range(LoginCode.MAX_ATTEMPTS):
            row.verify("000000")
        self.assertFalse(row.is_usable)
        # Even the right code is refused once the cap is hit.
        self.assertFalse(row.verify(code))

    def test_expired_code_is_refused(self):
        row, code = LoginCode.issue(self.user)
        row.expires_at = timezone.now() - timedelta(seconds=1)
        row.save(update_fields=["expires_at"])
        self.assertFalse(row.is_usable)
        self.assertFalse(row.verify(code))

    def test_issuing_again_burns_the_previous_code(self):
        first, first_code = LoginCode.issue(self.user)
        LoginCode.issue(self.user)
        first.refresh_from_db()
        self.assertFalse(first.is_usable)
        self.assertFalse(first.verify(first_code))


class TwoFactorLoginTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="member@example.com", password="Str0ng!pass", display_name="Member"
        )

    def _password_step(self):
        return self.client.post(reverse("accounts:login"), {
            "email": "member@example.com", "password": "Str0ng!pass",
        })

    def test_password_alone_does_not_sign_in(self):
        resp = self._password_step()
        self.assertRedirects(resp, reverse("accounts:verify"))
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_correct_code_completes_sign_in(self):
        self._password_step()
        # Re-issue so we know the plaintext; the view reads the newest row.
        _, plain = LoginCode.issue(self.user)
        resp = self.client.post(reverse("accounts:verify"), {"code": plain})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.user.pk)

    def test_code_may_be_pasted_with_a_space(self):
        self._password_step()
        _, plain = LoginCode.issue(self.user)
        spaced = f"{plain[:3]} {plain[3:]}"
        self.client.post(reverse("accounts:verify"), {"code": spaced})
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.user.pk)

    def test_wrong_code_leaves_you_signed_out(self):
        self._password_step()
        LoginCode.issue(self.user)
        resp = self.client.post(reverse("accounts:verify"), {"code": "000000"})
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_verify_page_needs_a_pending_sign_in(self):
        """Straight to /verify/ with no password step behind it goes nowhere."""
        resp = self.client.get(reverse("accounts:verify"))
        self.assertRedirects(resp, reverse("accounts:login"))

    def test_disabled_two_factor_signs_in_directly(self):
        self.user.two_factor_enabled = False
        self.user.save(update_fields=["two_factor_enabled"])
        resp = self._password_step()
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.user.pk)
        self.assertFalse(LoginCode.objects.filter(user=self.user).exists())

    def test_settings_toggle_turns_it_off(self):
        self.client.force_login(self.user)
        self.client.post(reverse("profile"), {"two_factor": "1"})
        self.user.refresh_from_db()
        self.assertFalse(self.user.two_factor_enabled)

    def test_settings_toggle_turns_it_back_on(self):
        self.user.two_factor_enabled = False
        self.user.save(update_fields=["two_factor_enabled"])
        self.client.force_login(self.user)
        self.client.post(
            reverse("profile"), {"two_factor": "1", "two_factor_enabled": "on"}
        )
        self.user.refresh_from_db()
        self.assertTrue(self.user.two_factor_enabled)


class PasswordResetUnknownEmailTests(TestCase):
    def setUp(self):
        User.objects.create_user(
            email="known@example.com", password="Str0ng!pass", display_name="Known"
        )

    def test_unknown_email_is_told_so(self):
        resp = self.client.post(reverse("password_reset"), {"email": "nope@example.com"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("email", resp.context["form"].errors)
        self.assertEqual(len(mail.outbox), 0)

    def test_known_email_still_sends(self):
        resp = self.client.post(reverse("password_reset"), {"email": "known@example.com"})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(len(mail.outbox), 1)


class DashboardRoundNavTests(TestCase):
    """The round navigator and its htmx swap.

    Both halves matter and both are easy to break silently: the navigator is
    what replaced a flat cross-round list nobody could orient in, and the swap
    is what stops changing round reloading the whole page.
    """

    def setUp(self):
        from catalog.models import Competition, Season, Series, Sport
        from orgs.models import OrgMember, Organisation
        from tipping.models import Match, Round, Team

        User = get_user_model()
        self.user = User.objects.create_user(
            email="nav@example.com", password="x", display_name="Nav"
        )
        sport = Sport.objects.create(name="Nav Footy", slug="nav-footy")
        self.series = Series.objects.create(sport=sport, name="Nav Series", slug="nav-series")
        season = Season.objects.create(year=2099, label="2099")
        self.comp = Competition.objects.create(
            sport=sport, season=season, name="Nav Comp", slug="nav-comp",
        )
        self.comp.series.add(self.series)
        self.org = Organisation.objects.create(name="Nav League", season=season)
        self.org.competitions.add(self.comp)
        OrgMember.objects.create(user=self.user, org=self.org)

        home = Team.objects.create(name="Reds", slug="nav-reds", series=self.series)
        away = Team.objects.create(name="Blues", slug="nav-blues", series=self.series)
        now = timezone.now()
        # One played round, then THREE still to come. Three matters: the
        # tipping window is two rounds per series, so a fourth is needed for
        # anything to be legitimately shut — with only two ahead, everything is
        # open and the window test would pass for the wrong reason.
        for n, offset in ((1, -14), (2, 3), (3, 10), (4, 17)):
            rnd = Round.objects.create(
                org=self.org, round_number=n, series=self.series, competition=self.comp,
                lockout_at=now + timedelta(days=offset),
            )
            Match.objects.create(
                round=rnd, home_team=home, away_team=away,
                kickoff_at=now + timedelta(days=offset),
                status="complete" if offset < 0 else "scheduled",
            )
        self.client.force_login(self.user)

    def test_the_navigator_lists_every_round(self):
        body = self.client.get(f"/dashboard/?org={self.org.id}").content.decode()
        self.assertIn('id="rnavSel"', body)
        for n in (1, 2, 3, 4):
            self.assertIn(f'value="{n}"', body)

    def test_an_htmx_request_returns_only_the_slate(self):
        """The swap must not carry the nav, the news column or a <head>.

        Sending the whole document to replace one panel would work by accident
        and cost several times the bytes, so this pins the contract.
        """
        r = self.client.get(
            f"/dashboard/?slate=1&org={self.org.id}&round=1", HTTP_HX_REQUEST="true",
        )
        body = r.content.decode()
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("<!DOCTYPE", body)
        self.assertNotIn("<nav", body)
        self.assertTrue(body.lstrip().startswith('<div id="slipPanel">'))

    def test_the_same_url_without_htmx_returns_the_whole_page(self):
        """A shared or bookmarked round URL has to be a real page.

        hx-push-url puts these in the address bar, so they get copied and
        pasted — landing on a bare fragment would be a broken link.
        """
        body = self.client.get(
            f"/dashboard/?slate=1&org={self.org.id}&round=1"
        ).content.decode()
        self.assertIn("<!DOCTYPE", body)

    def test_the_slate_carries_the_requested_round(self):
        r = self.client.get(
            f"/dashboard/?slate=1&org={self.org.id}&round=3", HTTP_HX_REQUEST="true",
        )
        self.assertIn('value="3" selected', r.content.decode())

    def test_a_round_outside_the_window_is_shown_but_shut(self):
        """The two-round rule, visible rather than merely enforced on submit.

        Rounds 2 and 3 are the window, so round 4 is the first that must render
        shut — present, so you can see what is coming, but not offering
        controls that submit_tip would refuse.
        """
        r = self.client.get(
            f"/dashboard/?slate=1&org={self.org.id}&round=4", HTTP_HX_REQUEST="true",
        )
        body = r.content.decode()
        self.assertIn("fxc-state is-shut", body)
        self.assertNotIn("fxc-state is-open", body)

    def test_an_unknown_round_falls_back_rather_than_erroring(self):
        """A stale bookmark should land somewhere useful, not on a 404."""
        r = self.client.get(f"/dashboard/?org={self.org.id}&round=99")
        self.assertEqual(r.status_code, 200)
        self.assertIn('id="rnavSel"', r.content.decode())


class DashboardThisWeekTests(TestCase):
    """"This week" must not drag in another code's round of the same number.

    A Round row is per (org, series), so one round NUMBER can name several
    rounds at once. In a league tipping two codes at different points in their
    seasons, landing on a number showed the live round with the other code's
    long-finished round of that number stacked underneath it — greyed out,
    unclickable, months old. The reported symptom was "AFL for the next round
    showed correctly, but underneath it also displayed NRL Round 3 tips from
    March".
    """

    def setUp(self):
        from catalog.models import Competition, Season, Series, Sport
        from orgs.models import OrgMember, Organisation
        from tipping.models import Match, Round, Team

        User = get_user_model()
        self.user = User.objects.create_user(
            email="week@example.com", password="x", display_name="Week"
        )
        season = Season.objects.create(year=2098, label="2098")
        self.org = Organisation.objects.create(name="Two Code League", season=season)
        OrgMember.objects.create(user=self.user, org=self.org)

        now = timezone.now()
        self.series = {}
        # Two codes whose seasons are nowhere near each other. "Early" is at
        # round 3 with round 3 still to come; "Late" has already played its
        # round 3 and is up to round 20 — so the number 3 exists in both, and
        # only one of them is live.
        for slug, played, upcoming in (("early", [1, 2], [3, 4]),
                                       ("late", [1, 2, 3, 18, 19], [20, 21])):
            sport = Sport.objects.create(name=f"Sport {slug}", slug=f"sport-{slug}")
            series = Series.objects.create(sport=sport, name=slug.title(), slug=slug)
            comp = Competition.objects.create(
                sport=sport, season=season, name=slug.title(), slug=slug,
            )
            comp.series.add(series)
            self.org.competitions.add(comp)
            self.series[slug] = series
            home = Team.objects.create(name=f"{slug} H", slug=f"{slug}-h", series=series)
            away = Team.objects.create(name=f"{slug} A", slug=f"{slug}-a", series=series)
            for n in played:
                rnd = Round.objects.create(
                    org=self.org, round_number=n, series=series, competition=comp,
                    lockout_at=now - timedelta(days=60), status="complete",
                )
                Match.objects.create(
                    round=rnd, home_team=home, away_team=away,
                    kickoff_at=now - timedelta(days=60), status="complete",
                )
            for i, n in enumerate(upcoming):
                rnd = Round.objects.create(
                    org=self.org, round_number=n, series=series, competition=comp,
                    lockout_at=now + timedelta(days=3 + i * 7),
                )
                Match.objects.create(
                    round=rnd, home_team=home, away_team=away,
                    kickoff_at=now + timedelta(days=3 + i * 7), status="scheduled",
                )
        self.client.force_login(self.user)

    def _round_headings(self, body):
        import re

        return re.findall(
            r'<span class="fxr-n">Round (\d+)</span>\s*'
            r'<span class="fxr-series">([^<]+)</span>',
            body,
        )

    def test_this_week_shows_the_open_round_of_each_competition(self):
        body = self.client.get(f"/dashboard/?org={self.org.id}").content.decode()
        shown = {(n, s.strip()) for n, s in self._round_headings(body)}
        self.assertEqual(shown, {("3", "Early"), ("20", "Late")})

    def test_this_week_does_not_show_a_finished_round_of_the_same_number(self):
        """The bug itself: Late's round 3 was played and must stay away."""
        body = self.client.get(f"/dashboard/?org={self.org.id}").content.decode()
        self.assertNotIn(("3", "Late"), self._round_headings(body))

    def test_each_round_is_one_contiguous_block(self):
        """{% regroup %} only collects consecutive items, so a round split
        across the sort order renders its heading again and again."""
        body = self.client.get(f"/dashboard/?org={self.org.id}").content.decode()
        headings = self._round_headings(body)
        self.assertEqual(len(headings), len(set(headings)))

    def test_a_numbered_round_belongs_to_one_code(self):
        """THIS TEST USED TO ASSERT THE OPPOSITE, and the client overruled it.

        It read: "explicitly navigating to round 3 is a history question, and
        both codes genuinely have one — that is not the bug", and it showed
        Early's live round 3 with Late's long-played round 3 underneath. That
        reasoning is defensible right up until somebody uses the screen. On
        1 Sept 2026 Ian reported it from the other end: "when I was looking at
        Round 4 womens to tip this weekend, the mens from round 4 back in April
        was still below it?"

        Nobody asks for every code's round 3 at once. So a numbered round is
        scoped to one code, and the code travels in the URL with the number.
        Nothing is hidden — see the test below, which asks for the other one.
        """
        body = self.client.get(
            f"/dashboard/?org={self.org.id}&round=early-3"
        ).content.decode()
        shown = {s.strip() for _n, s in self._round_headings(body)}
        self.assertEqual(shown, {"Early"})

    def test_the_other_codes_round_of_that_number_is_still_reachable(self):
        """Separated, not removed: "how did Late's round 3 go" is a real
        question and still has an answer."""
        body = self.client.get(
            f"/dashboard/?org={self.org.id}&round=late-3"
        ).content.decode()
        shown = {(n, s.strip()) for n, s in self._round_headings(body)}
        self.assertEqual(shown, {("3", "Late")})

    def test_a_bare_number_lands_on_the_code_whose_round_is_live(self):
        """Every link shared before the code travelled in the URL. It resolves
        to one code rather than to all of them."""
        body = self.client.get(
            f"/dashboard/?org={self.org.id}&round=3"
        ).content.decode()
        shown = {s.strip() for _n, s in self._round_headings(body)}
        self.assertEqual(shown, {"Early"})

    def test_the_dropdown_offers_this_week_as_its_own_destination(self):
        body = self.client.get(f"/dashboard/?org={self.org.id}").content.decode()
        self.assertIn('<option value="" selected>This week</option>', body)

    def test_leaving_this_week_offers_a_way_back_without_a_round(self):
        """The return link must drop ?round= entirely — pointing it at a
        number would land back on the mixed-code screen it exists to escape."""
        body = self.client.get(
            f"/dashboard/?org={self.org.id}&round=1"
        ).content.decode()
        self.assertIn("rnav-now", body)
        self.assertIn(f'href="/dashboard/?org={self.org.id}"', body)


class OnboardingWalkthroughTests(TestCase):
    """The first-visit walkthrough: shown once, put away for good."""

    def setUp(self):
        from catalog.models import Season
        from orgs.models import OrgMember, Organisation

        self.season = Season.objects.create(year=2098, label="2098")
        self.org = Organisation.objects.create(name="Coach Co", season=self.season)
        self.user = User.objects.create_user(
            email="new@example.com", password="x", display_name="Newbie",
        )
        OrgMember.objects.create(user=self.user, org=self.org)
        self.client.force_login(self.user)

    def test_it_shows_on_a_first_visit(self):
        resp = self.client.get(reverse("dashboard"))
        self.assertContains(resp, 'id="coach"')

    def test_it_does_not_show_once_it_has_been_put_away(self):
        self.client.post(reverse("accounts:onboarding_seen"))
        resp = self.client.get(reverse("dashboard"))
        self.assertNotContains(resp, 'id="coach"')

    def test_putting_it_away_is_stamped_on_the_user(self):
        self.client.post(reverse("accounts:onboarding_seen"))
        self.user.refresh_from_db()
        self.assertIsNotNone(self.user.onboarding_seen_at)

    def test_the_stamp_is_not_moved_by_a_second_call(self):
        self.client.post(reverse("accounts:onboarding_seen"))
        self.user.refresh_from_db()
        first = self.user.onboarding_seen_at
        self.client.post(reverse("accounts:onboarding_seen"))
        self.user.refresh_from_db()
        self.assertEqual(self.user.onboarding_seen_at, first)

    def test_rendering_alone_does_not_count_as_seeing_it(self):
        """A page opened and abandoned, or one whose script never ran, must
        not burn the single chance to explain the app."""
        self.client.get(reverse("dashboard"))
        self.user.refresh_from_db()
        self.assertIsNone(self.user.onboarding_seen_at)

    def test_somebody_in_no_organisation_gets_the_starting_walkthrough(self):
        """The moment a new member is most lost is the empty dashboard telling
        them to create an organisation or find one. It used to be the one
        moment nothing explained itself."""
        from orgs.models import OrgMember

        OrgMember.objects.filter(user=self.user).delete()
        resp = self.client.get(reverse("dashboard"))
        self.assertContains(resp, 'data-key="dashboard-start"')

    def test_it_needs_a_post(self):
        self.assertEqual(
            self.client.get(reverse("accounts:onboarding_seen")).status_code, 405,
        )

    def test_the_dashboard_carries_the_dashboard_tour(self):
        resp = self.client.get(reverse("dashboard"))
        self.assertContains(resp, 'data-key="dashboard"')

    def test_finishing_one_page_does_not_put_away_another(self):
        """The whole point of per-page: each screen introduces itself once, and
        the others are still owed."""
        self.client.post(reverse("accounts:onboarding_seen"), {"key": "dashboard"})
        self.assertNotContains(self.client.get(reverse("dashboard")), 'id="coach"')
        self.assertContains(self.client.get(reverse("profile")), 'data-key="profile"')

    def test_a_page_stays_put_away(self):
        self.client.post(reverse("accounts:onboarding_seen"), {"key": "profile"})
        self.assertNotContains(self.client.get(reverse("profile")), 'id="coach"')

    def test_the_starting_walkthrough_does_not_burn_the_dashboard_one(self):
        """Somebody who is shown the empty dashboard, puts it away, and then
        joins an organisation has never seen the real dashboard tour."""
        from orgs.models import OrgMember

        OrgMember.objects.filter(user=self.user).delete()
        self.client.post(
            reverse("accounts:onboarding_seen"), {"key": "dashboard-start"},
        )
        OrgMember.objects.create(user=self.user, org=self.org)
        self.assertContains(self.client.get(reverse("dashboard")), 'data-key="dashboard"')

    def test_an_unknown_key_is_not_written_to_the_user(self):
        """The endpoint is public, so a key it does not recognise must not be
        able to grow the column without limit."""
        self.client.post(reverse("accounts:onboarding_seen"), {"key": "../../nonsense"})
        self.user.refresh_from_db()
        self.assertEqual(self.user.onboarding_pages_seen, [])

    def test_a_member_who_saw_the_old_single_walkthrough_is_not_shown_it_again(self):
        """Existing members on the day this ships have already had the
        dashboard's four bubbles."""
        self.user.onboarding_seen_at = timezone.now()
        self.user.save(update_fields=["onboarding_seen_at"])
        self.assertNotContains(self.client.get(reverse("dashboard")), 'id="coach"')

    def test_but_their_other_pages_are_still_new_to_them(self):
        self.user.onboarding_seen_at = timezone.now()
        self.user.save(update_fields=["onboarding_seen_at"])
        self.assertContains(self.client.get(reverse("profile")), 'data-key="profile"')

    def test_a_page_with_no_tour_registered_carries_none(self):
        self.assertNotContains(self.client.get(reverse("orgs:search")), 'data-key="dashboard"')

    def test_signed_out_pages_carry_no_walkthrough(self):
        self.client.logout()
        self.assertNotContains(self.client.get(reverse("landing")), 'id="coach"')
class VerifyPageTests(TestCase):
    """The code page. Two things the client asked about, and one bug beside them.

    The ask: entering the last digit should sign you in — no Enter, no
    Verify button. The bug found while doing it: the page told every member to
    expect a *sixteen*-digit code, because it printed the form field's
    max_length. That field is deliberately loose so a pasted "123 456" survives
    validation; it was never the length of anything.
    """

    def setUp(self):
        self.user = User.objects.create_user(
            email="member@example.com", password="Str0ng-Pass-9x", display_name="Mem",
        )
        self.user.two_factor_enabled = True
        self.user.save(update_fields=["two_factor_enabled"])
        self.client.post(reverse("accounts:login"), {
            "email": "member@example.com", "password": "Str0ng-Pass-9x",
        })

    def test_the_page_names_the_real_code_length(self):
        html = self.client.get(reverse("accounts:verify")).content.decode()
        self.assertIn(f"We've sent a {LoginCode.CODE_LENGTH}-digit code", html)
        self.assertNotIn("16-digit code", html)

    def test_the_field_tells_the_page_when_to_submit_itself(self):
        html = self.client.get(reverse("accounts:verify")).content.decode()
        self.assertIn(f'data-otp-length="{LoginCode.CODE_LENGTH}"', html)
        self.assertIn("gt-otp.js", html)

    def test_the_generated_code_is_that_many_digits(self):
        _, code = LoginCode.issue(self.user)
        self.assertEqual(len(code), LoginCode.CODE_LENGTH)
        self.assertTrue(code.isdigit())

    def test_a_pasted_code_with_spaces_is_still_accepted(self):
        """Why max_length stays loose — the auto-submit strips these client
        side, but the server cannot rely on that having happened."""
        from .forms import VerifyCodeForm

        form = VerifyCodeForm({"code": " 123 456 "})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["code"], "123456")

    def test_a_code_of_the_wrong_length_is_refused(self):
        from .forms import VerifyCodeForm

        form = VerifyCodeForm({"code": "12345"})
        self.assertFalse(form.is_valid())
        self.assertIn(f"{LoginCode.CODE_LENGTH}-digit", str(form.errors))


class CompetitionColourTests(TestCase):
    """Four competitions must not arrive as four identical grey rows.

    ASKED FOR BY THE CLIENT. On a league tipping two codes the slate
    interleaves them by kickoff, so AFL and AFLW come down one list
    alternating — one letter apart in the heading, and that letter decides
    which ladder the round counts toward.

    The colours themselves are CSS. What is testable, and what actually breaks,
    is whether the markup still carries the hooks they hang off: a template
    that stops emitting data-code renders every competition grey and nothing
    fails.
    """

    def setUp(self):
        from catalog.models import Competition, Season, Series, Sport
        from orgs.models import OrgMember, Organisation
        from tipping.models import Match, Round, Team

        User = get_user_model()
        self.user = User.objects.create_user(
            email="colour@example.com", password="x", display_name="Colour",
        )
        # THE REAL CODES, fetched rather than created. Series.name is unique
        # site-wide and the migrations seed AFL/AFLW/NRL/NRLW, so a test that
        # makes its own "AFL" collides — and a test that invents "Colour
        # Series" would prove the template emits SOME slug while saying nothing
        # about the four this feature is about.
        afl = Series.objects.get(slug="afl")
        aflw = Series.objects.get(slug="aflw")
        sport = afl.sport
        season = Season.objects.create(year=2099, label="2099")
        self.comp = Competition.objects.create(
            sport=sport, season=season, name="Colour Comp", slug="colour-comp",
        )
        self.org = Organisation.objects.create(name="Colour League", season=season)
        self.org.competitions.add(self.comp)
        OrgMember.objects.create(user=self.user, org=self.org)

        now = timezone.now()
        # The men's and the women's code of the same sport, which is the pair
        # the client actually confuses on screen.
        for series in (afl, aflw):
            slug, name = series.slug, series.name
            self.comp.series.add(series)
            rnd = Round.objects.create(
                org=self.org, round_number=2, series=series, competition=self.comp,
                lockout_at=now + timedelta(days=3),
            )
            Match.objects.create(
                round=rnd,
                home_team=Team.objects.create(name=f"{name} Reds", slug=f"{slug}-r", series=series),
                away_team=Team.objects.create(name=f"{name} Blues", slug=f"{slug}-b", series=series),
                kickoff_at=now + timedelta(days=3),
            )
        self.client.force_login(self.user)

    def _body(self):
        return self.client.get(f"/dashboard/?org={self.org.id}").content.decode()

    def test_the_filter_chips_name_their_code_and_category(self):
        body = self._body()
        self.assertIn('data-code="afl" data-cat="mens"', body)
        self.assertIn('data-code="aflw" data-cat="womens"', body)

    def test_the_round_heading_and_the_cards_carry_it_too(self):
        """Not just the filter. The colour has to reach the fixtures — the
        filter is where you choose a code, the slate is where you confuse
        two."""
        body = self._body()
        self.assertIn("fxr-series is-code", body)
        self.assertIn('class="fxc-code"', body)
        # One card per code, each labelled with its own competition.
        self.assertIn(">AFLW</span>", body)

    def test_the_stylesheet_gives_each_code_its_own_shade(self):
        """The women's competitions are not one colour between them, and
        neither are the men's — the client asked for a shade EACH.

        Read out of the stylesheet because that is where the decision lives.
        The alternative is four screenshots and a person to look at them.
        """
        from pathlib import Path

        from django.conf import settings

        css = Path(settings.BASE_DIR, "static/css/goodtip.css").read_text()
        shades = {}
        for code in ("afl", "aflw", "nrl", "nrlw"):
            match = re.search(
                r'\[data-code="%s"\][^{]*\{([^}]*)\}' % code, css,
            )
            self.assertIsNotNone(match, f"{code} has no colour rule")
            ink = re.search(r"--code-ink:\s*([^;]+);", match.group(1))
            self.assertIsNotNone(ink, f"{code} has no --code-ink")
            shades[code] = ink.group(1).strip()
        self.assertEqual(
            len(set(shades.values())), 4,
            f"each code needs its own shade, got {shades}",
        )


class PastRoundResultsTests(TestCase):
    """A round already played, read by somebody who did not tip it.

    ASKED FOR AS: "the previous games — I should be able to see the results
    even if I did not tip, the scores, and who won." The score line was already
    on the card; what it did not say is which club the numbers belong to, which
    on a card with two clubs either side of a versus is most of the question.
    """

    def setUp(self):
        from catalog.models import Competition, Season, Series, Sport
        from orgs.models import OrgMember, Organisation
        from tipping.models import Match, Round, Team

        User = get_user_model()
        self.user = User.objects.create_user(
            email="past@example.com", password="x", display_name="Past",
        )
        self.series = Series.objects.get(slug="nrl")
        sport = self.series.sport
        season = Season.objects.create(year=2099, label="2099")
        comp = Competition.objects.create(
            sport=sport, season=season, name="Past Comp", slug="past-comp",
        )
        comp.series.add(self.series)
        self.org = Organisation.objects.create(name="Past League", season=season)
        self.org.competitions.add(comp)
        OrgMember.objects.create(user=self.user, org=self.org)

        now = timezone.now()
        self.played = Round.objects.create(
            org=self.org, round_number=1, series=self.series, competition=comp,
            lockout_at=now - timedelta(days=14), status="complete",
        )
        # A round still to come, so the played one is genuinely in the past
        # rather than being the only thing the dashboard could show.
        Round.objects.create(
            org=self.org, round_number=2, series=self.series, competition=comp,
            lockout_at=now + timedelta(days=3),
        )
        self.home = Team.objects.create(name="Storm", slug="past-storm", series=self.series)
        self.away = Team.objects.create(name="Eels", slug="past-eels", series=self.series)
        self.match = Match.objects.create(
            round=self.played, home_team=self.home, away_team=self.away,
            kickoff_at=now - timedelta(days=14),
            status="complete", result="home", home_score=24, away_score=12,
        )
        self.client.force_login(self.user)

    def _past_round(self):
        return self.client.get(
            f"/dashboard/?slate=1&org={self.org.id}&round=1", HTTP_HX_REQUEST="true",
        ).content.decode()

    def test_the_scores_are_there_without_a_tip_on_the_game(self):
        body = self._past_round()
        self.assertIn("fxc-outcome", body)
        self.assertIn("24", body)
        self.assertIn("12", body)

    def test_the_winner_is_named_as_the_winner(self):
        """"Who won" is not the same question as "what were the scores" — a
        reader who does not follow the code cannot answer the first from the
        second."""
        body = self._past_round()
        self.assertIn("fxc-outcome is-won", body)
        self.assertIn("fxc-outcome is-lost", body)
        self.assertIn(">Won</i>", body)

    def test_a_game_you_left_alone_says_so_in_words(self):
        """"No tip" was read as a fact about the fixture. On a past round what
        it has to say is what YOU did, which is nothing."""
        self.assertIn("Not tipped", self._past_round())

    def test_a_drawn_game_is_neither_won_nor_lost(self):
        self.match.result = "draw"
        self.match.away_score = 24
        self.match.save(update_fields=["result", "away_score"])
        body = self._past_round()
        self.assertIn("fxc-outcome is-drew", body)
        self.assertNotIn("fxc-outcome is-won", body)


class DockedConfirmTests(TestCase):
    """Confirm has to be reachable from wherever you are in the slate.

    ASKED FOR AS: "after I scroll down and I have been making tips, I have to
    scroll up again to confirm."

    Whether it is on screen is a scroll position and belongs to the browser.
    What belongs here is that it is RENDERED, that it lives outside the panel
    htmx replaces, and that it presses the real button rather than being a
    second way to submit a slate.
    """

    def setUp(self):
        from catalog.models import Competition, Season, Series, Sport
        from orgs.models import OrgMember, Organisation
        from tipping.models import Match, Round, Team

        User = get_user_model()
        self.user = User.objects.create_user(
            email="dock@example.com", password="x", display_name="Dock",
        )
        series = Series.objects.get(slug="afl")
        sport = series.sport
        season = Season.objects.create(year=2099, label="2099")
        comp = Competition.objects.create(
            sport=sport, season=season, name="Dock Comp", slug="dock-comp",
        )
        comp.series.add(series)
        self.org = Organisation.objects.create(name="Dock League", season=season)
        self.org.competitions.add(comp)
        OrgMember.objects.create(user=self.user, org=self.org)

        now = timezone.now()
        rnd = Round.objects.create(
            org=self.org, round_number=1, series=series, competition=comp,
            lockout_at=now + timedelta(days=3),
        )
        for i in range(3):
            Match.objects.create(
                round=rnd,
                home_team=Team.objects.create(name=f"H{i}", slug=f"dock-h{i}", series=series),
                away_team=Team.objects.create(name=f"A{i}", slug=f"dock-a{i}", series=series),
                kickoff_at=now + timedelta(days=3),
            )
        self.client.force_login(self.user)

    def test_it_is_rendered_and_starts_hidden(self):
        body = self.client.get(f"/dashboard/?org={self.org.id}").content.decode()
        self.assertIn('id="slipDock"', body)
        # Hidden on arrival: at the top of the page the real button is right
        # there, and two confirms on one screen is a question, not a shortcut.
        self.assertRegex(body, r'id="slipDock"[^>]*hidden')

    def test_it_sits_outside_the_panel_htmx_replaces(self):
        """#slipPanel is swapped whole by the filter and the round navigator.
        Anything captured inside it is detached the moment either is used —
        which for a fixed-position dock means one that sticks on screen and
        stops working."""
        r = self.client.get(
            f"/dashboard/?slate=1&org={self.org.id}", HTTP_HX_REQUEST="true",
        )
        self.assertNotIn("slipDock", r.content.decode())

    def test_it_presses_the_real_button_rather_than_submitting(self):
        """One path to the review sheet. A dock that posted the form itself
        would be a second submit route to keep in step with the first."""
        body = self.client.get(f"/dashboard/?org={self.org.id}").content.decode()
        dock = body[body.index('id="slipDock"'):body.index('id="tipSheet"')]
        self.assertNotIn('type="submit"', dock)
        self.assertIn("real.click()", body)
