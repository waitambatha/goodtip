from django.conf import settings
from django.db import models
from django.utils import timezone


class Organisation(models.Model):
    name = models.CharField(max_length=200)
    # Self-selected at sign-up (categories doc, 7 Jul 2026): one of the five
    # organisation types — Community, Business, Education, Charities, Informal.
    # Drives the Good List's type filter. Lookup FKs keep the DB normalised.
    group_type = models.ForeignKey(
        "catalog.GroupType", on_delete=models.PROTECT,
        related_name="organisations", null=True, blank=True,
    )
    # Sub-categories within the type (Business → Finance, Community → Sports
    # Club…). M2M because an Education org running both a primary and a
    # secondary school selects both and must surface under BOTH Good List
    # filters (categories doc build note). Charities/Informal orgs have none.
    sub_categories = models.ManyToManyField(
        "catalog.SubCategory", related_name="organisations", blank=True,
    )
    # Informal groups have no sub-category list — they self-describe ("Book
    # Club", "Cycling Crew") and that label shows next to their name on the
    # Good List.
    informal_label = models.CharField(max_length=60, blank=True)
    # Confirmed GoodTip partner charity (categories doc: Charity Partner
    # Workflow). NEVER self-declared — set only by GoodTip staff in admin.
    # True lets a Charities-type org lock fundraising to itself (no vote);
    # false routes it through the standard vote plus a partner CTA.
    is_charity_partner = models.BooleanField(default=False)
    # Location is optional — for orgs that operate nationally — and powers the
    # Good List's state/territory filter.
    state = models.ForeignKey(
        "catalog.State", on_delete=models.SET_NULL,
        related_name="organisations", null=True, blank=True,
    )
    # --- Public Good List consent (spec §4) ---
    # The manager opts in to public display of THIS group's name + total.
    # Default OFF; opt-in only; revocable. Never gates the private in-app board.
    is_public_listed = models.BooleanField(default=False)
    public_consent_at = models.DateTimeField(null=True, blank=True)
    public_consent_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        related_name="public_consents_given", null=True, blank=True,
    )
    # §5.5 — consent given at signup is consent at zero. Once the group has a
    # real total, we re-ask; this records that the re-confirm has been handled.
    public_consent_reconfirmed = models.BooleanField(default=False)
    # A league joins one or more Competitions (the deck's "what orgs join & tip
    # on" level), e.g. "NRL (2026)". Each Competition bundles its men's, women's
    # and representative Series into one leaderboard.
    competitions = models.ManyToManyField("catalog.Competition", related_name="organisations", blank=True)
    season = models.ForeignKey("catalog.Season", on_delete=models.PROTECT, related_name="organisations")
    charity = models.ForeignKey(
        "catalog.Charity",
        on_delete=models.PROTECT,
        related_name="organisations",
        null=True,
    )
    team_size = models.PositiveIntegerField(null=True, blank=True)
    finals_only = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.season})"

    @property
    def active_charity_vote(self):
        """The open charity vote for this org, or None."""
        return self.charity_votes.filter(status="open").first()

    @property
    def charity_display(self) -> str:
        """Charity name, or a placeholder while a vote decides it."""
        if self.charity_id:
            return self.charity.name
        if self.active_charity_vote:
            return "Charity vote in progress"
        return "—"

    @property
    def charity_name(self) -> str:
        """Backwards-compatible accessor for templates/code expecting a name."""
        return self.charity.name if self.charity_id else ""

    @property
    def charity_url(self) -> str:
        """Backwards-compatible accessor for templates/code expecting a URL."""
        return self.charity.website if self.charity_id else ""

    @property
    def category_label(self) -> str:
        """What shows next to the org's name on the Good List: the informal
        self-description for Informal groups, otherwise its sub-categories
        ("Primary School + Secondary School"), otherwise the bare type name.
        """
        if self.group_type_id and self.group_type.is_informal:
            return self.informal_label
        subs = " + ".join(s.name for s in self.sub_categories.all())
        if subs:
            return subs
        return self.group_type.name if self.group_type_id else ""

    @property
    def competition_label(self) -> str:
        """Human-readable list of the org's competitions, e.g. 'AFL' or 'AFL + NRL'."""
        return " + ".join(c.name for c in self.competitions.all()) or "—"

    # Backwards-compatible alias — older templates/exports call sport_label.
    @property
    def sport_label(self) -> str:
        return self.competition_label

    @property
    def available_series(self):
        """Series tipped in this org, drawn from its competitions (e.g. joining
        NRL exposes NRL, NRLW and State of Origin)."""
        from catalog.models import Series

        return Series.objects.filter(competitions__organisations=self).distinct()

    def set_public_consent(self, *, granted: bool, by_user=None):
        """Opt this group in/out of public naming on the Good List (spec §4).

        Records who consented and when. Revoking pulls the group from the public
        board immediately but never touches its private in-app standing.
        """
        self.is_public_listed = granted
        if granted:
            self.public_consent_at = timezone.now()
            self.public_consent_by = by_user
            # An explicit choice with a total in hand also settles the §5.5 re-ask.
            self.public_consent_reconfirmed = True
        self.save(update_fields=[
            "is_public_listed", "public_consent_at",
            "public_consent_by", "public_consent_reconfirmed",
        ])


class OrgMember(models.Model):
    # Per the customer-journey deck: a member's base role can be manager,
    # captain, both, or participant. League Owner is a separate permission flag.
    ROLE_MANAGER = "manager"
    ROLE_CAPTAIN = "captain"
    ROLE_BOTH = "both"
    ROLE_PARTICIPANT = "participant"
    ROLE_CHOICES = [
        (ROLE_MANAGER, "Team Manager"),
        (ROLE_CAPTAIN, "Captain"),
        (ROLE_BOTH, "Manager & Captain"),
        (ROLE_PARTICIPANT, "Participant"),
    ]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="memberships")
    org = models.ForeignKey(Organisation, on_delete=models.CASCADE, related_name="members")
    role = models.CharField(max_length=12, choices=ROLE_CHOICES, default=ROLE_PARTICIPANT)
    is_league_owner = models.BooleanField(default=False)
    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="invitees_referred",
    )
    joined_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("user", "org")
        ordering = ["joined_at"]

    def __str__(self):
        return f"{self.user} @ {self.org} ({self.role})"

    @property
    def is_manager(self) -> bool:
        return self.role in (self.ROLE_MANAGER, self.ROLE_BOTH)

    @property
    def is_captain(self) -> bool:
        return self.role in (self.ROLE_CAPTAIN, self.ROLE_BOTH)

    @property
    def can_manage(self) -> bool:
        """Who can configure the league, invite, and run the charity vote."""
        return self.is_manager or self.is_league_owner

    @property
    def role_labels(self) -> list[str]:
        """Short badges for display, e.g. ['Owner', 'Manager', 'Captain']."""
        labels = []
        if self.is_league_owner:
            labels.append("Owner")
        if self.is_manager:
            labels.append("Manager")
        if self.is_captain:
            labels.append("Captain")
        if not labels:
            labels.append("Participant")
        return labels


class CharityVote(models.Model):
    STATUS_CHOICES = [("open", "Open"), ("closed", "Closed")]

    org = models.ForeignKey(Organisation, on_delete=models.CASCADE, related_name="charity_votes")
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="open")
    opened_at = models.DateTimeField(auto_now_add=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    winning_charity = models.ForeignKey(
        "catalog.Charity",
        on_delete=models.PROTECT,
        related_name="won_charity_votes",
        null=True,
        blank=True,
    )

    class Meta:
        ordering = ["-opened_at"]

    def __str__(self):
        return f"Charity vote for {self.org.name} ({self.status})"

    @property
    def is_open(self) -> bool:
        return self.status == "open"


class CharityVoteOption(models.Model):
    vote = models.ForeignKey(CharityVote, on_delete=models.CASCADE, related_name="options")
    charity = models.ForeignKey("catalog.Charity", on_delete=models.PROTECT, related_name="vote_options")

    class Meta:
        unique_together = ("vote", "charity")
        ordering = ["charity__name"]

    def __str__(self):
        return f"{self.charity.name} (vote #{self.vote_id})"


class CharityVoteBallot(models.Model):
    vote = models.ForeignKey(CharityVote, on_delete=models.CASCADE, related_name="ballots")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="charity_ballots")
    option = models.ForeignKey(CharityVoteOption, on_delete=models.CASCADE, related_name="ballots")
    cast_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("vote", "user")

    def __str__(self):
        return f"{self.user} → {self.option.charity.name}"


class OrgCharitySelection(models.Model):
    """Append-only history of which charity an org backed, and from when.

    ``Organisation.charity`` holds the *current* choice as a convenience cache;
    this table is the historical record. Because a league can change its charity
    over time — a different cause next season, or a re-run vote — donations and
    reports can always be traced to the charity that was active at the time
    rather than whatever the org points at now.
    """

    SOURCE_INITIAL = "initial"
    SOURCE_VOTE = "vote"
    SOURCE_MANUAL = "manual"
    SOURCE_SELF = "self"
    SOURCE_CHOICES = [
        (SOURCE_INITIAL, "Set at league creation"),
        (SOURCE_VOTE, "Won the charity vote"),
        (SOURCE_MANUAL, "Changed manually"),
        # Charity Partner Workflow (categories doc): a confirmed partner
        # charity locked fundraising to itself — no participant vote.
        (SOURCE_SELF, "Locked to own charity"),
    ]

    org = models.ForeignKey(Organisation, on_delete=models.CASCADE, related_name="charity_selections")
    # Denormalised from org.season so the timeline is queryable by season directly.
    season = models.ForeignKey("catalog.Season", on_delete=models.PROTECT, related_name="charity_selections")
    charity = models.ForeignKey("catalog.Charity", on_delete=models.PROTECT, related_name="selections")
    source = models.CharField(max_length=10, choices=SOURCE_CHOICES, default=SOURCE_MANUAL)
    effective_from = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-effective_from", "-id"]

    def __str__(self):
        return f"{self.org.name} → {self.charity.name} ({self.season}, {self.source})"
