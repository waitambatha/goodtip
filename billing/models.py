from decimal import Decimal

from django.conf import settings
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


class DonationPledge(models.Model):
    """An org's charitable commitment for a season (deck slide 9: donation_pledge).

    Phase 1: the pledge is recorded in the DB and shown to participants from day
    one (the anchor). Payment timing is flexible; matching is calculated as
    top-ups arrive and disbursed at season close.
    """

    SCHEDULE_LUMP = "lump"
    SCHEDULE_MONTHLY = "monthly"
    SCHEDULE_SEASON_CLOSE = "season_close"
    SCHEDULE_CHOICES = [
        (SCHEDULE_LUMP, "Lump sum upfront"),
        (SCHEDULE_MONTHLY, "Monthly instalments"),
        (SCHEDULE_SEASON_CLOSE, "At season close"),
    ]

    org = models.ForeignKey("orgs.Organisation", on_delete=models.CASCADE, related_name="pledges")
    season = models.ForeignKey("catalog.Season", on_delete=models.PROTECT, related_name="pledges")
    # Mirrors the org's chosen charity at pledge time; may be unset during a vote.
    charity = models.ForeignKey(
        "catalog.Charity", on_delete=models.PROTECT, related_name="pledges", null=True, blank=True
    )
    pledged_amount_aud = models.DecimalField(max_digits=10, decimal_places=2)
    paid_amount_aud = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0"))
    payment_schedule = models.CharField(max_length=20, choices=SCHEDULE_CHOICES, default=SCHEDULE_SEASON_CLOSE)
    matching_enabled = models.BooleanField(default=False)
    matching_cap_aud = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0"))
    matching_used_aud = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0"))
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["org", "season"], name="one_pledge_per_org_season"),
        ]

    def __str__(self):
        return f"{self.org.name} pledge ${self.pledged_amount_aud} ({self.season})"

    @property
    def matching_remaining_aud(self) -> Decimal:
        if not self.matching_enabled:
            return Decimal("0")
        return max(self.matching_cap_aud - self.matching_used_aud, Decimal("0"))


class DonationPayment(models.Model):
    """A single movement of donation money (deck slide 9: donation_payment).

    Every payment stores its donation_amount distinct from any platform fee, as
    required for Foundation receipting, ATO compliance, and ESG reporting.
    """

    TYPE_BASE = "base"
    TYPE_INSTALMENT = "instalment"
    TYPE_TOP_UP = "top_up"
    TYPE_MATCHED = "matched"
    TYPE_CHOICES = [
        (TYPE_BASE, "Org base donation"),
        (TYPE_INSTALMENT, "Org instalment"),
        (TYPE_TOP_UP, "Participant top-up"),
        (TYPE_MATCHED, "Org matched amount"),
    ]

    PAID_BY_OWNER = "league_owner"
    PAID_BY_PARTICIPANT = "participant"
    PAID_BY_SYSTEM = "system"
    PAID_BY_CHOICES = [
        (PAID_BY_OWNER, "League Owner"),
        (PAID_BY_PARTICIPANT, "Participant"),
        (PAID_BY_SYSTEM, "System (matching)"),
    ]

    pledge = models.ForeignKey(DonationPledge, on_delete=models.CASCADE, related_name="payments")
    org = models.ForeignKey("orgs.Organisation", on_delete=models.CASCADE, related_name="donation_payments")
    # The charity this money was destined for, frozen at payment time. Stays put
    # even if the org later switches charity — required for an honest audit trail
    # (Foundation receipting, ATO, ESG). Nullable only to admit legacy rows.
    charity = models.ForeignKey(
        "catalog.Charity", on_delete=models.PROTECT, related_name="donation_payments",
        null=True, blank=True,
    )
    amount_aud = models.DecimalField(max_digits=10, decimal_places=2)
    type = models.CharField(max_length=20, choices=TYPE_CHOICES)
    paid_by = models.CharField(max_length=20, choices=PAID_BY_CHOICES)
    participant = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="donation_payments",
    )
    # Phase 1: funds held in the Pty Ltd account; no DGR receipt yet (deck).
    stripe_checkout_session_id = models.CharField(max_length=255, blank=True)
    stripe_payment_intent_id = models.CharField(max_length=255, blank=True)
    receipt_sent = models.BooleanField(default=False)
    receipt_url = models.URLField(blank=True)
    # When the money actually cleared. The Good List counts SETTLED money only
    # (spec §5.3): a pledge or an in-progress/bounced payment must never inflate
    # a public total. Null = not yet settled and excluded from the Good List.
    settled_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.get_type_display()} ${self.amount_aud} — {self.org.name}"

    @property
    def is_settled(self) -> bool:
        return self.settled_at is not None

    def mark_settled(self, when=None):
        """Flag this payment as cleared so it counts toward the Good List."""
        from django.utils import timezone

        if self.settled_at is None:
            self.settled_at = when or timezone.now()
            self.save(update_fields=["settled_at"])


class CharityDisbursement(models.Model):
    """Season-close settlement to a charity (deck slide 9: charity_disbursement).

    Phase 1: created when the season is closed; the actual transfer is manual
    until the Foundation has DGR status (Phase 2).
    """

    charity = models.ForeignKey("catalog.Charity", on_delete=models.PROTECT, related_name="disbursements")
    org = models.ForeignKey("orgs.Organisation", on_delete=models.CASCADE, related_name="disbursements")
    season = models.ForeignKey("catalog.Season", on_delete=models.PROTECT, related_name="disbursements")
    total_base_aud = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0"))
    total_matched_aud = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0"))
    total_topups_aud = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0"))
    total_disbursed_aud = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0"))
    stripe_transfer_id = models.CharField(max_length=255, blank=True)
    receipt_url = models.URLField(blank=True)
    disbursed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["org", "season"], name="one_disbursement_per_org_season"),
        ]

    def __str__(self):
        return f"Disbursement ${self.total_disbursed_aud} → {self.charity.name} ({self.season})"
