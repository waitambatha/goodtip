from django.conf import settings
from django.db import models


class Organisation(models.Model):
    name = models.CharField(max_length=200)
    sports = models.ManyToManyField("catalog.Sport", related_name="organisations", blank=True)
    season = models.ForeignKey("catalog.Season", on_delete=models.PROTECT, related_name="organisations")
    charity_name = models.CharField(max_length=200)
    charity_url = models.URLField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.season})"

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
    ROLE_CHOICES = [("admin", "Admin"), ("member", "Member")]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="memberships")
    org = models.ForeignKey(Organisation, on_delete=models.CASCADE, related_name="members")
    role = models.CharField(max_length=10, choices=ROLE_CHOICES, default="member")
    joined_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("user", "org")
        ordering = ["joined_at"]

    def __str__(self):
        return f"{self.user} @ {self.org} ({self.role})"
