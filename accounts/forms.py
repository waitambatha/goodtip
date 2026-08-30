from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import PasswordResetForm as DjangoPasswordResetForm
from django.contrib.auth.password_validation import validate_password

from .models import LoginCode


User = get_user_model()


class SignupForm(forms.Form):
    display_name = forms.CharField(
        max_length=100,
        label="Your name",
        widget=forms.TextInput(attrs={
            "placeholder": "Jordan Smith",
            "autocomplete": "name",
            "autofocus": True,
        }),
    )
    email = forms.EmailField(
        label="Work email",
        widget=forms.EmailInput(attrs={
            "placeholder": "you@company.com.au",
            "autocomplete": "email",
        }),
    )
    password1 = forms.CharField(
        label="Password",
        widget=forms.PasswordInput(attrs={
            "placeholder": "Create a password",
            "autocomplete": "new-password",
            "data-pw-strength": "",
        }),
    )
    password2 = forms.CharField(
        label="Confirm password",
        widget=forms.PasswordInput(attrs={
            "placeholder": "Re-enter your password",
            "autocomplete": "new-password",
            "data-pw-match": "id_password1",
        }),
    )

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("An account with that email already exists.")
        return email

    def clean(self):
        cleaned = super().clean()
        p1 = cleaned.get("password1")
        p2 = cleaned.get("password2")
        if p1 and p2 and p1 != p2:
            self.add_error("password2", "Passwords do not match.")
        if p1:
            try:
                validate_password(p1)
            except forms.ValidationError as e:
                self.add_error("password1", e)
        return cleaned

    def save(self):
        return User.objects.create_user(
            email=self.cleaned_data["email"],
            password=self.cleaned_data["password1"],
            display_name=self.cleaned_data["display_name"],
        )


class LoginForm(forms.Form):
    email = forms.EmailField(
        label="Email",
        widget=forms.EmailInput(attrs={
            "placeholder": "you@company.com.au",
            "autocomplete": "email",
            "autofocus": True,
        }),
    )
    password = forms.CharField(
        label="Password",
        widget=forms.PasswordInput(attrs={
            "placeholder": "Your password",
            "autocomplete": "current-password",
        }),
    )


class VerifyCodeForm(forms.Form):
    # Deliberately looser than six characters. People paste "123 456" straight
    # out of the email and phone keyboards add a trailing space; a max_length
    # of 6 would reject those before clean_code ever got to tidy them up. The
    # real "exactly six digits" rule lives in clean_code, after stripping.
    code = forms.CharField(
        label="Verification code",
        max_length=16,
        widget=forms.TextInput(attrs={
            "placeholder": "000000",
            "autocomplete": "one-time-code",
            "inputmode": "numeric",
            "autofocus": True,
            "class": "otp-input",
            "maxlength": "16",
        }),
    )

    def clean_code(self):
        digits = "".join(c for c in self.cleaned_data["code"] if c.isdigit())
        if len(digits) != LoginCode.CODE_LENGTH:
            raise forms.ValidationError(
                f"Enter the {LoginCode.CODE_LENGTH}-digit code from your email."
            )
        return digits


class SecurityForm(forms.ModelForm):
    """The two-step verification switch on the settings page."""

    two_factor_enabled = forms.BooleanField(
        required=False,
        label="Require a code emailed to me when I sign in",
    )

    class Meta:
        model = User
        fields = ["two_factor_enabled"]


class RegisteredEmailPasswordResetForm(DjangoPasswordResetForm):
    """Password reset that says so when the address isn't registered.

    Django's default accepts any address silently, on purpose: replying
    "no such account" tells an attacker which addresses exist. That
    protection is being traded away here deliberately, because a member who
    mistypes their address otherwise waits forever for an email that was
    never going to arrive and has no way to find out why.

    The trade is narrowed rather than taken wholesale — the view rate-limits
    per session, so the page can't be used to enumerate a list at speed.
    """

    def clean_email(self):
        email = self.cleaned_data["email"]
        if not User.objects.filter(email__iexact=email, is_active=True).exists():
            raise forms.ValidationError(
                "We don't have an account for that email. "
                "Check the address for typos, or sign up instead."
            )
        return email


class ProfileForm(forms.ModelForm):
    class Meta:
        model = User
        fields = ["display_name"]


class AvatarForm(forms.ModelForm):
    MAX_BYTES = 5 * 1024 * 1024  # 5 MB

    class Meta:
        model = User
        fields = ["avatar"]

    def clean_avatar(self):
        avatar = self.cleaned_data.get("avatar")
        if avatar and avatar.size > self.MAX_BYTES:
            raise forms.ValidationError("Please choose an image under 5 MB.")
        return avatar
