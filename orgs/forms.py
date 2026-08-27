from django import forms
from django.utils import timezone

from catalog.models import (
    Charity, Competition, Country, OrganisationType, Season, State, SubCategory,
)

from .models import Organisation


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

    # ASKED BEFORE ANYTHING ELSE. See WIZARD_STEPS in views.py — this is step
    # one on its own, and every rule below keys off it. Someone setting up a
    # family comp used to meet workplace validation before anything had
    # established they were not a workplace.
    FORMALITY_FORMAL = "formal"
    FORMALITY_INFORMAL = "informal"
    FORMALITY_CHOICES = [
        (FORMALITY_FORMAL, "Formal"),
        (FORMALITY_INFORMAL, "Informal"),
    ]
    formality = forms.ChoiceField(
        choices=FORMALITY_CHOICES,
        widget=forms.RadioSelect,
        label="What kind of setup is this?",
    )
    organisation_type = forms.ModelChoiceField(
        queryset=OrganisationType.objects.all(),  # ordered by sort_order per the spec
        required=False,
        label="Organisation type",
        empty_label="Choose your organisation type",
    )
    country = forms.ModelChoiceField(
        queryset=Country.objects.filter(is_active=True),
        required=False,
        label="Country",
        empty_label="Choose a country",
        help_text="Where the group is based. Payment stays in AUD either way.",
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
    # PICK FROM THE LIST, FULL STOP. This step used to offer "…or add a
    # different charity" as a free-text name and URL, and it was the wrong
    # place to ask: someone standing up their first league has no way to know
    # whether "Cancer Council" is already there under a slightly different
    # name, and every near-miss they typed became a permanent duplicate in the
    # picker every other organisation reads. Adding a charity now happens in
    # Manage → Charities, after creation, by an admin who is looking at the
    # existing list while they do it — see services.add_charity_for_org.
    charity = forms.ModelChoiceField(
        queryset=Charity.objects.filter(is_approved=True),
        required=False,
        label="Charity",
        empty_label="Choose a charity",
    )
    # When the vote runs. Both optional: leaving them blank creates the
    # election in draft, which is what happened to every vote before these
    # fields existed — the wizard finished with "set up the charity election
    # when you're ready" and dropped you, so an admin who did not come back
    # left their members with a vote that never opened.
    vote_opens_at = forms.DateTimeField(
        required=False,
        label="Open the vote",
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
    )
    vote_closes_at = forms.DateTimeField(
        required=False,
        label="Close it",
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
    )
    vote_charities = forms.ModelMultipleChoiceField(
        queryset=Charity.objects.filter(is_approved=True),
        required=False,
        widget=forms.CheckboxSelectMultiple,
        label="Charities to put to the vote (pick at least 2)",
    )
    # Asked once, at creation, instead of being left to a settings toggle
    # nobody stumbles across. "no" is the initial so a small team that never
    # touches this step still gets the right default — most organisations
    # never need groups — but it is a real radio a person looks at and picks,
    # not a silent default they never saw.
    groups_enabled = forms.TypedChoiceField(
        choices=[("no", "Not for now"), ("yes", "Yes, switch them on")],
        coerce=lambda v: v == "yes",
        widget=forms.RadioSelect,
        initial="no",
        label="Split into groups?",
    )

    class Meta:
        model = Organisation
        fields = [
            "name", "parent", "organisation_type", "sub_categories", "informal_label",
            "country", "state",
            "competitions", "season", "team_size", "finals_only", "groups_enabled",
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
        # The season actually in play right now — the same fallback a betting
        # site's "current round" resolves to without ever asking a punter to
        # pick a year. Nobody signing up should have to know that a 2027
        # competition row exists on file before its season has even started;
        # they tip the season that is live, full stop.
        current = (
            Season.objects.filter(year=timezone.now().year).first()
            or Season.objects.filter(year__lte=timezone.now().year).first()
            or Season.objects.first()
        )
        self.fields["competitions"].queryset = (
            fed_competitions().filter(season=current) if current else fed_competitions()
        )
        # No longer a question anyone answers: hidden, and always the current
        # season, so "AFL (2027)" cannot even be selected before 2027 starts —
        # it simply is not offered, rather than being offered and then having
        # to be explained away with a second "which season" field beside it.
        self.fields["season"].queryset = Season.objects.all()
        self.fields["season"].widget = forms.HiddenInput()
        self.fields["season"].required = False
        if not self.is_bound and not self.initial.get("season") and current:
            self.initial["season"] = current.pk

    def _clean_formality(self, cleaned):
        """Resolve the Formal / Informal answer into a concrete type.

        This runs BEFORE the per-type rules, because it is what decides which
        of them apply. Two things happen here:

        * Informal needs no type question at all — there is exactly one
          informal type, so asking someone to choose from a list of one is
          pure friction. It is filled in for them.
        * A type that disagrees with the formality answer is dropped rather
          than trusted. The wizard hides the mismatched options, but the value
          arrives from a browser and a stale draft can easily still carry the
          type someone picked before going back and changing their mind.
        """
        formality = cleaned.get("formality")
        if not formality:
            return
        informal = formality == self.FORMALITY_INFORMAL
        gt = cleaned.get("organisation_type")
        if gt is not None and gt.is_formal == informal:
            gt = None                       # answers disagree — the first wins
        if informal:
            gt = OrganisationType.objects.filter(
                slug=OrganisationType.SLUG_INFORMAL
            ).first()
        elif gt is None:
            self.add_error("organisation_type", "Choose your organisation type.")
        cleaned["organisation_type"] = gt

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
        self._clean_formality(cleaned)
        self._clean_categories(cleaned)
        method = cleaned.get("charity_method")
        if method == "vote":
            opens = cleaned.get("vote_opens_at")
            closes = cleaned.get("vote_closes_at")
            if closes and not opens:
                self.add_error("vote_opens_at", "Say when it opens as well as when it closes.")
            if opens and closes and closes <= opens:
                self.add_error("vote_closes_at", "The vote has to close after it opens.")
            candidates = cleaned.get("vote_charities")
            if not candidates or candidates.count() < 2:
                self.add_error(
                    "vote_charities",
                    "Pick at least 2 charities for the group to vote on.",
                )
        else:  # pick
            if not cleaned.get("charity"):
                self.add_error("charity", "Choose a charity from the list.")
        return cleaned

    @property
    def is_vote(self) -> bool:
        return self.cleaned_data.get("charity_method") == "vote"

    def save(self, commit=True):
        org = super().save(commit=False)
        # In vote mode the charity stays unset until the vote resolves.
        org.charity = None if self.is_vote else self.cleaned_data.get("charity")
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


class OrgCharityForm(forms.Form):
    """Add a charity this organisation backs but GoodTip's list doesn't have.

    Deliberately two fields. The wizard's version asked the same thing of
    someone who had been on the platform for ninety seconds; this one is in
    Manage, next to the list of what already exists, so the person filling it
    in can see whether their cause is already there before typing it again.
    """

    name = forms.CharField(
        max_length=200,
        label="Charity name",
        widget=forms.TextInput(attrs={
            "placeholder": "e.g. Ronald McDonald House",
            "autocomplete": "off",
        }),
    )
    website = forms.URLField(
        required=False,
        label="Website (optional)",
        widget=forms.URLInput(attrs={
            "placeholder": "https://",
            # Guessed from the name as it is typed — see the wizard's old
            # field for the same trick. Still hand-editable.
            "data-url-from": "id_name",
        }),
    )

    def clean_name(self):
        name = (self.cleaned_data.get("name") or "").strip()
        if len(name) < 3:
            raise forms.ValidationError("That's too short to be a charity name.")
        return name


class GroupCharityBallotForm(forms.Form):
    """The charities a group is putting to its members.

    The queryset is the ORGANISATION's available list — vetted plus anything
    it added — because a group votes on what its organisation offers. Built
    per-instance, so one org's private charities can never appear on another's
    ballot.
    """

    charities = forms.ModelMultipleChoiceField(
        queryset=Charity.objects.none(),
        widget=forms.CheckboxSelectMultiple,
        label="Charities on the ballot",
    )
    opens_at = forms.DateTimeField(
        required=False,
        label="Open the vote",
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
    )
    closes_at = forms.DateTimeField(
        required=False,
        label="Close it",
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
    )
    message = forms.CharField(
        required=False,
        label="A note to your group (optional)",
        widget=forms.Textarea(attrs={
            "rows": 3,
            "placeholder": "Why we're voting, when it closes…",
        }),
    )

    def __init__(self, *args, org=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["charities"].queryset = Charity.objects.available_to(org).order_by("name")

    def clean(self):
        cleaned = super().clean()
        picked = cleaned.get("charities")
        if picked is not None and picked.count() < 2:
            self.add_error("charities", "Pick at least 2 charities to vote between.")
        opens, closes = cleaned.get("opens_at"), cleaned.get("closes_at")
        if closes and not opens:
            self.add_error("opens_at", "Say when it opens as well as when it closes.")
        if opens and closes and closes <= opens:
            self.add_error("closes_at", "The vote has to close after it opens.")
        return cleaned
