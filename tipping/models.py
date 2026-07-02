from django.conf import settings
from django.db import models
from django.utils import timezone


class Team(models.Model):
    name = models.CharField(max_length=100)
    slug = models.CharField(max_length=100)
    series = models.ForeignKey("catalog.Series", on_delete=models.PROTECT, related_name="teams")
    external_id = models.CharField(max_length=100, blank=True)

    class Meta:
        unique_together = ("slug", "series")
        ordering = ["series", "name"]

    def __str__(self):
        return f"{self.name} [{self.series}]"


class Round(models.Model):
    STATUS_CHOICES = [
        ("upcoming", "Upcoming"),
        ("open", "Open"),
        ("locked", "Locked"),
        ("complete", "Complete"),
    ]

    # Match type drives the score weighting (Ambrose Hierarchy brief, slide 6):
    # regular rounds are the baseline; finals raise the stakes; State of Origin is
    # the prestige event worth the most.
    STAGE_REGULAR = "regular"
    STAGE_FINALS = "finals"
    STAGE_ORIGIN = "origin"
    STAGE_CHOICES = [
        (STAGE_REGULAR, "Regular round"),
        (STAGE_FINALS, "Finals"),
        (STAGE_ORIGIN, "State of Origin"),
    ]
    POINTS_PER_STAGE = {
        STAGE_REGULAR: 1,
        STAGE_FINALS: 2,
        STAGE_ORIGIN: 4,
    }

    org = models.ForeignKey("orgs.Organisation", on_delete=models.CASCADE, related_name="rounds")
    round_number = models.IntegerField()
    # The competition this round is tipped under (deck: fixtures keyed to a
    # competition_id). The series says whether it's the men's/women's/Origin slate.
    competition = models.ForeignKey(
        "catalog.Competition", on_delete=models.PROTECT, related_name="rounds", null=True, blank=True
    )
    series = models.ForeignKey("catalog.Series", on_delete=models.PROTECT, related_name="rounds")
    stage = models.CharField(max_length=10, choices=STAGE_CHOICES, default=STAGE_REGULAR)
    lockout_at = models.DateTimeField()
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="upcoming")

    class Meta:
        ordering = ["-round_number"]
        unique_together = ("org", "round_number", "series")

    def __str__(self):
        return f"Round {self.round_number} [{self.series}] — {self.org.name}"

    @property
    def points_per_correct(self) -> int:
        """Points awarded for each correct tip in this round (1 / 2 / 4)."""
        return self.POINTS_PER_STAGE.get(self.stage, 1)

    @property
    def is_locked(self) -> bool:
        return self.status in ("locked", "complete") or timezone.now() >= self.lockout_at

    @property
    def effective_status(self) -> str:
        if self.status == "complete":
            return "complete"
        if self.is_locked:
            return "locked"
        if self.status == "upcoming" and timezone.now() < self.lockout_at:
            return "open"
        return self.status


class Match(models.Model):
    RESULT_CHOICES = [("home", "Home"), ("away", "Away"), ("draw", "Draw")]

    round = models.ForeignKey(Round, on_delete=models.CASCADE, related_name="matches")
    home_team = models.ForeignKey(Team, on_delete=models.PROTECT, related_name="home_matches")
    away_team = models.ForeignKey(Team, on_delete=models.PROTECT, related_name="away_matches")
    kickoff_at = models.DateTimeField()
    venue = models.CharField(max_length=200, blank=True)
    result = models.CharField(max_length=10, choices=RESULT_CHOICES, null=True, blank=True)
    home_score = models.IntegerField(null=True, blank=True)
    away_score = models.IntegerField(null=True, blank=True)
    external_id = models.CharField(max_length=100, blank=True)

    class Meta:
        ordering = ["kickoff_at"]

    def __str__(self):
        return f"{self.home_team.name} v {self.away_team.name}"

    @property
    def is_locked(self) -> bool:
        return timezone.now() >= self.kickoff_at

    @property
    def has_result(self) -> bool:
        return self.result is not None


class Tip(models.Model):
    SELECTION_CHOICES = [("home", "Home"), ("away", "Away")]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="tips")
    match = models.ForeignKey(Match, on_delete=models.CASCADE, related_name="tips")
    org = models.ForeignKey("orgs.Organisation", on_delete=models.CASCADE, related_name="tips")
    selection = models.CharField(max_length=10, choices=SELECTION_CHOICES)
    submitted_at = models.DateTimeField(auto_now=True)
    is_correct = models.BooleanField(null=True, blank=True)
    # Weighted score this tip earned once graded: round.points_per_correct if
    # correct, else 0. Stored so the leaderboard can Sum() instead of Count().
    points_awarded = models.PositiveIntegerField(default=0)

    class Meta:
        unique_together = ("user", "match", "org")

    def __str__(self):
        return f"{self.user} → {self.selection} for {self.match}"
