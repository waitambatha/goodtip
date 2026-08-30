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
