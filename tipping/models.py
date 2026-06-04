from django.conf import settings
from django.db import models
from django.utils import timezone


class Team(models.Model):
    COMPETITION_CHOICES = [
        ("AFL", "AFL"),
        ("AFLW", "AFLW"),
        ("NRL", "NRL"),
        ("NRLW", "NRLW"),
    ]

    name = models.CharField(max_length=100)
    slug = models.CharField(max_length=100)
    competition = models.CharField(max_length=10, choices=COMPETITION_CHOICES)
    external_id = models.CharField(max_length=100, blank=True)

    class Meta:
        unique_together = ("slug", "competition")
        ordering = ["competition", "name"]

    def __str__(self):
        return f"{self.name} [{self.competition}]"


class Round(models.Model):
    COMPETITION_CHOICES = [("AFL", "AFL"), ("NRL", "NRL")]
    STATUS_CHOICES = [
        ("upcoming", "Upcoming"),
        ("open", "Open"),
        ("locked", "Locked"),
        ("complete", "Complete"),
    ]

    org = models.ForeignKey("orgs.Organisation", on_delete=models.CASCADE, related_name="rounds")
    round_number = models.IntegerField()
    competition = models.CharField(max_length=10, choices=COMPETITION_CHOICES)
    lockout_at = models.DateTimeField()
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="upcoming")

    class Meta:
        ordering = ["-round_number"]
        unique_together = ("org", "round_number", "competition")

    def __str__(self):
        return f"Round {self.round_number} [{self.competition}] — {self.org.name}"

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

    class Meta:
        unique_together = ("user", "match", "org")

    def __str__(self):
        return f"{self.user} → {self.selection} for {self.match}"
