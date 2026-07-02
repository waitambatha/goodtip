from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password


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


class ProfileForm(forms.ModelForm):
    class Meta:
        model = User
        fields = ["display_name"]
