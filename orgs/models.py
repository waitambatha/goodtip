from django.conf import settings
from django.db import models


class Organisation(models.Model):
    name = models.CharField(max_length=200)
    sports = models.ManyToManyField("catalog.Sport", related_name="organisations", blank=True)
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
    def sport_label(self) -> str:
        """Human-readable list of the org's sports, e.g. 'AFL' or 'AFL + NRL'."""
        return " + ".join(s.name for s in self.sports.all()) or "—"

    @property
    def available_competitions(self):
        """Competitions selectable for this org, based on its sports."""
        from catalog.models import Competition

        return Competition.objects.filter(sport__in=self.sports.all())


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
