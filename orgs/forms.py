from django import forms
from django.utils.text import slugify

from catalog.models import Charity, Competition, Season

from .models import Organisation


def _unique_charity_slug(name: str) -> str:
    base = slugify(name)[:200] or "charity"
    slug = base
    i = 2
    while Charity.objects.filter(slug=slug).exists():
        slug = f"{base}-{i}"
        i += 1
    return slug


class OrgCreateForm(forms.ModelForm):
    CHARITY_METHOD_CHOICES = [
        ("pick", "I'll choose the charity"),
        ("vote", "Let the group vote"),
    ]

    competitions = forms.ModelMultipleChoiceField(
        queryset=Competition.objects.select_related("sport", "season"),
        widget=forms.CheckboxSelectMultiple,
        label="Competition(s)",
    )
    charity_method = forms.ChoiceField(
        choices=CHARITY_METHOD_CHOICES,
        widget=forms.RadioSelect,
        initial="pick",
        label="How is the charity decided?",
    )
    charity = forms.ModelChoiceField(
        queryset=Charity.objects.filter(is_approved=True),
        required=False,
        label="Charity",
        empty_label="— Choose an approved charity —",
    )
    new_charity_name = forms.CharField(
        required=False,
        label="…or add a different charity",
        widget=forms.TextInput(attrs={"placeholder": "Charity name"}),
    )
    new_charity_url = forms.URLField(
        required=False,
        label="New charity website (optional)",
        widget=forms.URLInput(attrs={"placeholder": "https://"}),
    )
    vote_charities = forms.ModelMultipleChoiceField(
        queryset=Charity.objects.filter(is_approved=True),
        required=False,
        widget=forms.CheckboxSelectMultiple,
        label="Charities to put to the vote (pick at least 2)",
    )

    class Meta:
        model = Organisation
        fields = ["name", "competitions", "season", "team_size", "finals_only"]
        labels = {
            "name": "League name",
            "team_size": "Expected group size (optional)",
            "finals_only": "Finals only (skip the regular season)",
        }
        widgets = {
            "name": forms.TextInput(attrs={"placeholder": "e.g. Acme Corp Footy Tips"}),
            "team_size": forms.NumberInput(attrs={"min": 1, "placeholder": "e.g. 12"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["season"].queryset = Season.objects.all()
        self.fields["season"].empty_label = None

    def clean(self):
        cleaned = super().clean()
        method = cleaned.get("charity_method")
        if method == "vote":
            candidates = cleaned.get("vote_charities")
            if not candidates or candidates.count() < 2:
                self.add_error(
                    "vote_charities",
                    "Pick at least 2 charities for the group to vote on.",
                )
        else:  # pick
            charity = cleaned.get("charity")
            new_name = (cleaned.get("new_charity_name") or "").strip()
            if not charity and not new_name:
                self.add_error(
                    "charity",
                    "Choose an approved charity or add a different one.",
                )
            if charity and new_name:
                self.add_error(
                    "new_charity_name",
                    "Pick from the list or add a new one — not both.",
                )
        return cleaned

    @property
    def is_vote(self) -> bool:
        return self.cleaned_data.get("charity_method") == "vote"

    def _resolve_picked_charity(self):
        charity = self.cleaned_data.get("charity")
        new_name = (self.cleaned_data.get("new_charity_name") or "").strip()
        if new_name:
            charity = Charity.objects.filter(name__iexact=new_name).first()
            if charity is None:
                charity = Charity.objects.create(
                    name=new_name,
                    slug=_unique_charity_slug(new_name),
                    website=self.cleaned_data.get("new_charity_url") or "",
                    is_approved=False,
                )
                # Flag for the view to notify the GoodTip team for manual review.
                self.suggested_charity = charity
        return charity

    def save(self, commit=True):
        org = super().save(commit=False)
        # In vote mode the charity stays unset until the vote resolves.
        org.charity = None if self.is_vote else self._resolve_picked_charity()
        if commit:
            org.save()
            self.save_m2m()
        return org
