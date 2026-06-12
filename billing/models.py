from django.db import models

from .pricing import TIER_CHOICES, seat_limit_label


class PlanSubscription(models.Model):
    """A League Owner's platform-fee subscription for a season (deck slide 9).

    Phase 1: one single charge per league/season, fixed by tier.
    """

    STATUS_PENDING = "pending"
    STATUS_ACTIVE = "active"
    STATUS_EXPIRED = "expired"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending payment"),
        (STATUS_ACTIVE, "Active"),
        (STATUS_EXPIRED, "Expired"),
    ]

    org = models.ForeignKey("orgs.Organisation", on_delete=models.CASCADE, related_name="subscriptions")
    season = models.ForeignKey("catalog.Season", on_delete=models.PROTECT, related_name="subscriptions")
    tier = models.CharField(max_length=20, choices=TIER_CHOICES)
    # Platform fee, kept distinct from any donation amount (deck: store separately).
    price_aud = models.DecimalField(max_digits=8, decimal_places=2)
    seat_limit = models.PositiveIntegerField()
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_PENDING)

    stripe_checkout_session_id = models.CharField(max_length=255, blank=True)
    stripe_payment_intent_id = models.CharField(max_length=255, blank=True)

    paid_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.get_tier_display()} — {self.org.name} ({self.status})"

    @property
    def is_active(self) -> bool:
        return self.status == self.STATUS_ACTIVE

    @property
    def seat_limit_label(self) -> str:
        return seat_limit_label(self.seat_limit)

    @property
    def participant_count(self) -> int:
        return self.org.members.count()

    @property
    def seats_remaining(self) -> int:
        return max(self.seat_limit - self.participant_count, 0)
