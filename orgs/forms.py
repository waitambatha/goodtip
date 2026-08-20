from django import forms
from django.utils import timezone

from catalog.models import Charity, Competition, OrganisationType, Season, State, SubCategory

from .models import Organisation
from .services import unique_charity_slug as _unique_charity_slug


def fed_competitions():
    """Competitions a feed can actually deliver fixtures for.

    Super League and Super Netball are seeded by a migration as 2027 roadmap
    entries. They have no scraper, no teams and no fixtures, and nothing is
    scheduled to give them any — so a league that picks one gets an empty
    dashboard for its whole season with nothing on screen suggesting why. Six
    leagues had already done exactly that, and one of them tips nothing else.

    Offering a competition is a promise to deliver its games; this keeps the
    signup form to the promises the system can actually keep. The moment either
    gains a scraper it reappears here on its own, because the test is what
    data_sync can resolve rather than a second hardcoded list that would have
    to be remembered.
    """
    from data_sync.services import competition_for_series

    ok = [
        c.pk for c in Competition.objects.prefetch_related("series")
        if any(competition_for_series(s.name) for s in c.series.all())
    ]
    return Competition.objects.filter(pk__in=ok).select_related("sport", "season")


# The only allowed sub-category pairing (categories doc build note): a school
# running both levels selects Primary + Secondary and surfaces under both
# Good List filters. Every other type picks exactly one sub-category.
EDUCATION_PAIR = {"primary-school", "secondary-school"}


class OrgCreateForm(forms.ModelForm):
    CHARITY_METHOD_CHOICES = [
        ("pick", "I'll choose the charity"),
        ("vote", "Let the group vote"),
    ]

    # Org-structure note §3: a child sits under one TOP-LEVEL parent, so the
    # queryset excludes children (two levels max). Hidden — the parent is
    # chosen on the search page (§2's create-a-child path), never typed here;
    # §1: a standalone creator must see no hierarchy question at all.
    parent = forms.ModelChoiceField(
        queryset=Organisation.objects.filter(parent__isnull=True),
        required=False,
        widget=forms.HiddenInput,
    )

    organisation_type = forms.ModelChoiceField(
        queryset=OrganisationType.objects.all(),  # ordered by sort_order per the spec
        label="Organisation type",
        empty_label="Choose your organisation type",
    )
    sub_categories = forms.ModelMultipleChoiceField(
        queryset=SubCategory.objects.filter(is_active=True).select_related("organisation_type"),
        required=False,
        widget=forms.CheckboxSelectMultiple,
        label="Sub-category",
    )
    informal_label = forms.CharField(
        required=False,
        max_length=60,
        label="What kind of group are you?",
        widget=forms.TextInput(attrs={"placeholder": "e.g. Book Club, Gaming Group, Cycling Crew"}),
    )
    state = forms.ModelChoiceField(
        queryset=State.objects.all(),
        required=False,
        label="State or territory (optional)",
        empty_label="We operate nationally",
    )
    # Only competitions a feed can actually deliver. See fed_competitions().
    competitions = forms.ModelMultipleChoiceField(
        queryset=Competition.objects.none(),
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
        empty_label="Choose an approved charity",
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
        fields = [
            "name", "parent", "organisation_type", "sub_categories", "informal_label", "state",
            "competitions", "season", "team_size", "finals_only",
        ]
        labels = {
            "name": "Organisation name",
            "team_size": "Expected group size (optional)",
            "finals_only": "Finals only (skip the regular season)",
        }
        widgets = {
            "name": forms.TextInput(attrs={"placeholder": "e.g. Acme Corp"}),
            "team_size": forms.NumberInput(attrs={"min": 1, "placeholder": "e.g. 12"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["competitions"].queryset = fed_competitions()
        self.fields["season"].queryset = Season.objects.all()
        self.fields["season"].empty_label = None
        # Season.Meta orders by -year, so the first choice was always the
        # furthest-future season on file. With empty_label off that became the
        # silent default, and every new league was created into a season nobody
        # is playing — the upstream feeds have no draw for it, so the league
        # syncs no fixtures and shows an empty dashboard with nothing obviously
        # wrong. Default to the season actually in play.
        if not self.is_bound and not self.initial.get("season"):
            current = (
                Season.objects.filter(year=timezone.now().year).first()
                or Season.objects.filter(year__lte=timezone.now().year).first()
                or Season.objects.first()
            )
            if current:
                self.initial["season"] = current.pk

    def _clean_categories(self, cleaned):
        """Per-type rules from the categories doc: Community/Business pick one
        sub-category, Education picks one or the Primary+Secondary pair,
        Charities has none, Informal self-describes instead.
        """
        gt = cleaned.get("organisation_type")
        if gt is None:
            return
        # Ignore stale checkboxes from a previously-selected type.
        subs = [s for s in cleaned.get("sub_categories") or [] if s.organisation_type_id == gt.id]
        cleaned["sub_categories"] = subs

        if gt.is_informal:
            if not (cleaned.get("informal_label") or "").strip():
                self.add_error("informal_label", "Tell us what kind of group you are — it shows next to your name on The Good List.")
            return
        cleaned["informal_label"] = ""

        if gt.is_charity_type:
            cleaned["sub_categories"] = []
            return

        if not subs:
            self.add_error("sub_categories", "Pick a sub-category.")
        elif gt.is_education:
            slugs = {s.slug for s in subs}
            if len(subs) > 1 and not slugs <= EDUCATION_PAIR:
                self.add_error("sub_categories", "Pick one — only Primary School and Secondary School can be combined.")
        elif len(subs) > 1:
            self.add_error("sub_categories", "Pick just one sub-category.")

    def clean(self):
        cleaned = super().clean()
        self._clean_categories(cleaned)
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


class InviteByEmailForm(forms.Form):
    """Send the join link to specific people.

    Accepts a list rather than one address because inviting a team is the
    normal case — asking someone to submit the form six times to add six
    colleagues is the kind of friction that stops a group forming at all.
    """

    MAX_PER_SEND = 25

    emails = forms.CharField(
        label="Email addresses",
        widget=forms.Textarea(attrs={
            "rows": 3,
            "placeholder": "sam@work.com.au, jo@work.com.au",
        }),
        help_text="Separate addresses with a comma, a space, or a new line.",
    )
    message = forms.CharField(
        label="Add a note (optional)",
        required=False,
        max_length=500,
        widget=forms.Textarea(attrs={
            "rows": 2,
            "placeholder": "Righto team — this is our tipping comp for the year.",
        }),
    )

    def clean_emails(self):
        raw = self.cleaned_data["emails"]
        # People paste from a spreadsheet, a mail client, or type them out —
        # accept all three rather than insisting on one separator.
        parts = [
            p.strip().strip("<>,;")
            for p in raw.replace(",", " ").replace(";", " ").split()
        ]
        seen, cleaned, bad = set(), [], []
        for part in parts:
            if not part:
                continue
            try:
                forms.EmailField().clean(part)
            except forms.ValidationError:
                bad.append(part)
                continue
            key = part.lower()
            if key not in seen:
                seen.add(key)
                cleaned.append(part)
        if bad:
            raise forms.ValidationError(
                "These don't look like email addresses: " + ", ".join(bad[:5])
                + ("…" if len(bad) > 5 else "")
            )
        if not cleaned:
            raise forms.ValidationError("Enter at least one email address.")
        if len(cleaned) > self.MAX_PER_SEND:
            raise forms.ValidationError(
                f"That's {len(cleaned)} addresses — {self.MAX_PER_SEND} at a time is the limit. "
                "Send the rest in a second batch."
            )
        return cleaned
