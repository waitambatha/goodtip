from decimal import Decimal

from django.conf import settings
from django.db import models

from .pricing import TIER_CHOICES, seat_limit_label, tier_label


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

    # --- Founding Member rate ---
    # Whether THIS charge was taken at a locked founding rate, and what the
    # list price was at the time. Recorded on the subscription and not only on
    # the FoundingRate row, because a receipt has to be able to explain itself
    # years later: "$299, Founding Member rate, list was $399" is a line an
    # organisation can check, and a lock that is only stored as a rule is a
    # lock nobody can audit after the rule changes.
    is_founding_rate = models.BooleanField(default=False)
    list_price_aud = models.DecimalField(
        max_digits=8, decimal_places=2, null=True, blank=True,
    )

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

    @property
    def founding_saving_aud(self) -> Decimal:
        """What the founding rate is worth on this charge, in dollars."""
        if not self.is_founding_rate or self.list_price_aud is None:
            return Decimal("0")
        return max(self.list_price_aud - self.price_aud, Decimal("0"))


class FoundingRate(models.Model):
    """An organisation's locked-in price, and the date the lock runs out.

    WHAT THIS IS FOR. "Founding Partner pricing, locked" has been on the site
    since launch and was, until now, only a sentence on a marketing page —
    nothing in the system knew which organisations had been promised it, at
    what price, or for how long. The first renewal after a price rise would
    have quietly charged them the new list price, and the only record that a
    promise had been made would have been a paragraph on /pricing/.

    ONE PER ORGANISATION, NOT ONE PER SEASON. The lock is a property of the
    relationship — you were here early — so it survives the season rolling
    over, which is the entire point of it.

    IT LOCKS ONE PLAN'S PRICE, NOT EVERY PLAN'S. ``tier`` is the plan the rate
    was granted for and the lock applies to that plan alone: an organisation
    that grows from Team to Workplace pays Workplace's list price, because the
    founding promise was that their price would not rise under them, not that
    every larger product would be sold to them at the small one's rate.

    And it is a ceiling, never a floor. If a list price ever falls below a
    locked amount, the list price is charged — a loyalty rate that costs more
    than walking in off the street is the promise working against them.

    Both rules live in ``services.price_for``, which is the only thing that
    should ever read this row to decide an amount.

    EXPIRY IS A DATE, NOT A COUNTDOWN. ``locked_until`` is computed once, at
    grant time, by pricing.founding_locked_until, so changing the lock length
    later cannot silently re-date every organisation already holding one.
    """

    org = models.OneToOneField(
        "orgs.Organisation", on_delete=models.CASCADE, related_name="founding_rate",
    )
    # The plan in force when the rate was granted — context for support, not a
    # constraint. See the class docstring.
    tier = models.CharField(max_length=20, choices=TIER_CHOICES)
    # The locked amount, in AUD. This is the number that is enforced.
    price_aud = models.DecimalField(max_digits=8, decimal_places=2)
    # Last day the lock holds. Renewals on or before this date get price_aud.
    locked_until = models.DateField()
    granted_at = models.DateTimeField(auto_now_add=True)
    # Free text for a rate granted by hand — a negotiated deal, a goodwill
    # extension — so a row that did not come from the signup window says so.
    note = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["-granted_at"]

    def __str__(self):
        return f"{self.org.name} — ${self.price_aud} locked to {self.locked_until}"

    def is_live(self, on=None) -> bool:
        from datetime import date

        return (on or date.today()) <= self.locked_until

    @property
    def tier_label(self) -> str:
        return tier_label(self.tier)


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


class SponsorshipApplication(models.Model):
    """A request for a sponsored place from a group that cannot pay the fee.

    WHY THIS EXISTS. The platform fee is what funds the donation, so it is not
    something that can simply be waived by a checkbox — but "one flat fee" also
    quietly excludes exactly the groups the product is most worth something to.
    The client's first audience for this is Papua New Guinea, where a workplace
    comp is an entirely normal thing to run and AUD 99 a year is not an
    entirely normal thing to spend.

    So there is a door, and it is a door a human opens. Every field here exists
    to let whoever reads it make that decision without a reply-and-wait: who
    they are, how many of them, where, and why the fee is out of reach.

    IT IS NOT A DISCOUNT CODE, AND IT IS NOT AUTOMATIC. Approving one of these
    is GoodTip choosing to carry an organisation's fee, which is a real cost to
    a real budget — so approval is recorded against a person (``decided_by``)
    and the row it lands on is the audit trail. Nothing in the codebase grants
    a sponsored place without one of these rows being approved by hand.

    ANONYMOUS IS ALLOWED. ``user`` and ``org`` are both nullable, because the
    people this is for are the ones who have read the pricing page and stopped.
    Making them create an account and set an organisation up before they can
    ask whether they can afford to is the wrong order, and it is the order that
    loses them.
    """

    STATUS_NEW = "new"
    STATUS_REVIEWING = "reviewing"
    STATUS_APPROVED = "approved"
    STATUS_DECLINED = "declined"
    STATUS_CHOICES = [
        (STATUS_NEW, "New"),
        (STATUS_REVIEWING, "Being reviewed"),
        (STATUS_APPROVED, "Approved"),
        (STATUS_DECLINED, "Declined"),
    ]

    contact_name = models.CharField(max_length=120)
    email = models.EmailField()
    organisation_name = models.CharField(max_length=160)
    # Free text rather than a picker: a workplace in Lae describing itself is
    # more use to whoever reads this than the nearest of our five categories.
    organisation_kind = models.CharField(max_length=120, blank=True)
    country = models.ForeignKey(
        "catalog.Country", on_delete=models.SET_NULL,
        related_name="sponsorship_applications", null=True, blank=True,
    )
    people = models.PositiveIntegerField(
        null=True, blank=True,
        help_text="Roughly how many people would be tipping.",
    )
    reason = models.TextField(
        help_text="Why the platform fee is out of reach, in their own words.",
    )

    # Filled in only when the applicant already has an account or an org.
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        related_name="sponsorship_applications", null=True, blank=True,
    )
    org = models.ForeignKey(
        "orgs.Organisation", on_delete=models.SET_NULL,
        related_name="sponsorship_applications", null=True, blank=True,
    )

    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=STATUS_NEW)
    decision_note = models.TextField(
        blank=True,
        help_text="What was decided and why. Shown to nobody but staff.",
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        related_name="sponsorships_decided", null=True, blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.organisation_name} — {self.get_status_display()}"

    @property
    def is_open(self) -> bool:
        return self.status in (self.STATUS_NEW, self.STATUS_REVIEWING)
