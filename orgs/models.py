from django.conf import settings
from django.db import models


class Organisation(models.Model):
    SPORT_CHOICES = [("AFL", "AFL"), ("NRL", "NRL"), ("BOTH", "Both")]

    name = models.CharField(max_length=200)
    sport = models.CharField(max_length=10, choices=SPORT_CHOICES)
    season = models.IntegerField()
    charity_name = models.CharField(max_length=200)
    charity_url = models.URLField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.season})"


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
