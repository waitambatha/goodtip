from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

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

    def test_complex_password_creates_account_and_logs_in(self):
        resp = self.signup("Str0ng!pass")
        self.assertEqual(resp.status_code, 302)
        user = User.objects.get(email="tipper@example.com")
        self.assertTrue(user.check_password("Str0ng!pass"))
