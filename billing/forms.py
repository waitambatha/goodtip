from decimal import Decimal

from django import forms

from .models import DonationPledge


class TopUpForm(forms.Form):
    amount = forms.DecimalField(
        min_value=Decimal("1"), max_digits=10, decimal_places=2,
        label="Your top-up (AUD)",
        widget=forms.NumberInput(attrs={"min": "1", "step": "1", "placeholder": "e.g. 20"}),
    )


class DonationPledgeForm(forms.Form):
    pledged_amount = forms.DecimalField(
        min_value=Decimal("1"), max_digits=10, decimal_places=2,
        label="Your league's donation pledge (AUD)",
        widget=forms.NumberInput(attrs={"min": "1", "step": "1", "placeholder": "e.g. 500"}),
    )
    payment_schedule = forms.ChoiceField(
        choices=DonationPledge.SCHEDULE_CHOICES,
        initial=DonationPledge.SCHEDULE_SEASON_CLOSE,
        widget=forms.RadioSelect,
        label="When will you pay?",
    )
    matching_enabled = forms.BooleanField(
        required=False,
        label="Match participant top-ups dollar-for-dollar",
    )
    matching_cap = forms.DecimalField(
        required=False, min_value=Decimal("0"), max_digits=10, decimal_places=2,
        label="Matching cap (AUD)",
        widget=forms.NumberInput(attrs={"min": "0", "step": "1", "placeholder": "e.g. 500"}),
    )

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("matching_enabled"):
            cap = cleaned.get("matching_cap")
            if not cap or cap <= 0:
                self.add_error(
                    "matching_cap",
                    "Set a matching cap so your budget stays predictable.",
                )
        else:
            cleaned["matching_cap"] = Decimal("0")
        return cleaned
