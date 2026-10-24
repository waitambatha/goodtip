from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils import timezone

from .pricing import CUSTOM, TIER_CHOICES, seat_limit_label, tier_label


class PlanSubscription(models.Model):
    """A League Owner's platform-fee subscription for a season (deck slide 9).

    Phase 1: one single charge per league/season, fixed by tier.
    """

    STATUS_PENDING = "pending"
    # Subscribed and card saved, first charge still to come (FIRST_BILLING_DATE).
    STATUS_SCHEDULED = "scheduled"
    STATUS_ACTIVE = "active"
    STATUS_EXPIRED = "expired"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending payment"),
        (STATUS_SCHEDULED, "Subscribed, billing starts later"),
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
    # The card is saved through a SetupIntent, confirmed on our own page with
    # Stripe Elements (client, 10 Oct 2026: a loader, a decline animation and
    # a success animation GoodTip draws itself — not reachable with Checkout's
    # own iframe UI, which handles all of that inside itself with no event the
    # parent page can see). The subscription itself is created once this
    # succeeds — see billing.services.finalize_subscription.
    stripe_setup_intent_id = models.CharField(max_length=255, blank=True)

    # --- Yearly subscription (3 Oct 2026) ---
    # The plan is a Stripe subscription now, not a one-off charge: subscribed
    # today, first charged on ``starts_at``, renewing yearly from then.
    stripe_customer_id = models.CharField(max_length=255, blank=True)
    stripe_subscription_id = models.CharField(max_length=255, blank=True, db_index=True)
    starts_at = models.DateTimeField(null=True, blank=True)
    stripe_invoice_url = models.URLField(max_length=500, blank=True)
    # Test mode only: the Stripe test clock the demo subscription runs on, so
    # the demo can jump it to the first billing date instead of waiting.
    stripe_test_clock_id = models.CharField(max_length=255, blank=True)
    # Test mode (client, 6 Oct 2026): on Stripe TEST keys only, a plan can be
    # taken as a DAILY subscription whose first charge lands about a day after
    # checkout, so testers can watch real (test-money) charges arrive without
    # waiting for 31 January or a year. Never possible on live keys.
    is_test_daily = models.BooleanField(default=False)

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

    # --- Switching and opting out (client, 10 Oct 2026: "upgrade or
    # downgrade", "opting out") ---
    # Set when an ACTIVE subscription is cancelled: it keeps the season
    # already paid for and simply will not renew, rather than cutting off
    # something that has been paid for. A SCHEDULED one — nothing charged
    # yet — is cancelled outright instead (billing.services.cancel_subscription)
    # and never sets this; it goes straight to STATUS_EXPIRED.
    cancel_at_period_end = models.BooleanField(default=False)
    canceled_at = models.DateTimeField(null=True, blank=True)

    # Only set on a CUSTOM-tier subscription (CustomPriceRequest, below) — the
    # name Ian gave the offer when approving it, e.g. "AquaFlow partner plan".
    # Held here rather than looked up from the request every time a page or an
    # invoice needs a label, so the name on an old receipt still reads right
    # even if the request row it came from is edited or gone.
    custom_label = models.CharField(max_length=80, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.get_tier_display()} — {self.org.name} ({self.status})"

    def get_tier_display(self):
        """The five published plans read their name from TIER_CHOICES as
        usual; a custom one reads the name Ian gave it, because "Custom" on
        every screen an organisation sees is not the name of their plan."""
        if self.tier == CUSTOM and self.custom_label:
            return self.custom_label
        return dict(TIER_CHOICES).get(self.tier, self.tier)

    @property
    def is_active(self) -> bool:
        return self.status == self.STATUS_ACTIVE

    @property
    def is_scheduled(self) -> bool:
        return self.status == self.STATUS_SCHEDULED

    @property
    def is_demo(self) -> bool:
        return bool(self.stripe_test_clock_id)

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


class CustomPriceRequest(models.Model):
    """An organisation asking for a price outside the five published plans,
    and the super admin's answer to it.

    CLIENT, 11 OCT 2026: "the user will be able to request for a custom
    price... an email will be sent to the super admin that this specific
    organisation is requesting for a custom price... the super admin will be
    able to create a custom price for that specific organisation... or deny
    it and say we can work with the five subscriptions we currently have."

    Shaped like SponsorshipApplication (a request, a decision, a person who
    made it) rather than sysadmin.ChangeRequest's hold-and-replay-the-POST
    machinery — there is no form submission to replay here, only a price for
    a reviewer to set once, which then drives billing.services.
    create_custom_subscription directly.

    ONE OFFER AT A TIME IS NOT ENFORCED HERE. An organisation can ask again
    after a decline, and a later approval simply becomes the current one —
    billing.views reads the most recent APPROVED row, so an old declined or
    superseded request is history, not a row that has to be deleted to get
    out of the way.
    """

    STATUS_PENDING = "pending"
    STATUS_APPROVED = "approved"
    STATUS_DECLINED = "declined"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Waiting for review"),
        (STATUS_APPROVED, "Approved"),
        (STATUS_DECLINED, "Declined"),
    ]

    org = models.ForeignKey(
        "orgs.Organisation", on_delete=models.CASCADE, related_name="custom_price_requests",
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="custom_price_requests",
    )
    message = models.TextField(
        blank=True, help_text="What the organisation is asking for, in their own words.",
    )
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_PENDING)
    # What the request form asks for, kept as numbers as well as in the
    # message (client, 24 Oct 2026: "being able to custom request a price...
    # things are well organized") — so the team's queue can show the size of
    # the ask at a glance and suggest a price for it, instead of reading it
    # out of a paragraph. All optional: an older request has only `message`.
    people_count = models.PositiveIntegerField(null=True, blank=True)
    sites_count = models.PositiveIntegerField(null=True, blank=True)
    budget_aud = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    org_kind = models.CharField(max_length=40, blank=True, help_text="School, club, franchise... as the form offers.")
    created_at = models.DateTimeField(auto_now_add=True)

    # Set only on approval — what the organisation is being offered.
    price_aud = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    seat_limit = models.PositiveIntegerField(null=True, blank=True)
    label = models.CharField(
        max_length=80, blank=True, help_text="Shown to the organisation as the plan's name.",
    )

    decision_note = models.TextField(
        blank=True, help_text="Sent to the organisation by email — why declined, or terms of the offer.",
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="custom_prices_decided",
    )

    # THE OTHER HALF OF THE NEGOTIATION (client, 12 Oct 2026): approving a
    # request offers a price, it does not commit the organisation to it —
    # "you can either accept it or reject it... give a reason why... a form
    # of negotiation." Kept on the same row as the approval rather than a
    # new status, because an org that declines has not un-approved Ian's
    # price, it has said no to it — the offer stays exactly what it was for
    # the record, and billing.services.current_custom_offer simply stops
    # treating a declined-by-the-org row as live.
    org_declined_at = models.DateTimeField(null=True, blank=True)
    org_decline_reason = models.TextField(
        blank=True, help_text="Sent back to the team by email — why the organisation said no to the offer.",
    )
    # When the organisation said yes — pressed Accept on the offer, which
    # makes its subscription (billing.views.custom_checkout_view).
    accepted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.org.name} — {self.get_status_display()}"

    @property
    def is_pending(self) -> bool:
        return self.status == self.STATUS_PENDING

    @property
    def is_declined_by_org(self) -> bool:
        return self.status == self.STATUS_APPROVED and self.org_declined_at is not None

    @property
    def org_kind_label(self) -> str:
        from .custom_pricing import org_kind_label

        return org_kind_label(self.org_kind)

    @property
    def stage(self) -> str:
        """Where the conversation is, as one word both screens draw from:
        waiting, offer, declined (by the team), refused (by the org),
        accepted (a subscription was made from it)."""
        if self.status == self.STATUS_PENDING:
            return "waiting"
        if self.status == self.STATUS_DECLINED:
            return "declined"
        if self.org_declined_at:
            return "refused"
        if self.accepted_at:
            return "accepted"
        return "offer"


class CustomPackage(models.Model):
    """A pre-made custom offer, browsable without asking first.

    CLIENT, 19 OCT 2026: "he wanted us to... share a various custom prices
    that we can hard code" — but rather than literally hard-coding them, Ian
    gets a place to create and edit these himself (the super admin), same
    reasoning as everywhere else prices live in an editable row rather than
    in a deploy. Distinct from CustomPriceRequest on purpose: a request is
    one organisation's negotiation, decided once; a package is a standing
    offer — "for schools", say — anyone can see and take without writing in
    first. The two can still meet: a visitor who takes none of these can
    fall through to "request a custom price" (the existing flow).
    """

    name = models.CharField(max_length=80, help_text='Shown as the plan\'s name, e.g. "Schools Plan".')
    description = models.CharField(
        max_length=240, blank=True, help_text="One line under the name, said to the person deciding.",
    )
    price_aud = models.DecimalField(max_digits=8, decimal_places=2)
    seat_limit = models.PositiveIntegerField(help_text="How many people an organisation on this package may have.")
    child_org_limit = models.PositiveIntegerField(
        default=1,
        help_text="How many organisations (locations/departments) one subscription to this package covers.",
    )
    is_active = models.BooleanField(
        default=True, help_text="Unchecked hides it from the pricing page without deleting it.",
    )
    sort_order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["sort_order", "price_aud"]

    def __str__(self):
        return f"{self.name} (${self.price_aud:,.0f})"

    @property
    def seat_limit_label(self) -> str:
        return seat_limit_label(self.seat_limit)


class CustomPackageClaim(models.Model):
    """One organisation taking a CustomPackage, with a simulated review wait.

    CLIENT, 19 OCT 2026: "we can just have a simulation of the approvals
    just happening automatic, but giving the impression that it's taking
    time... maybe it can take 5 minutes, and then it will say your custom
    [package] has been approved." Nobody actually reviews this — `is_ready`
    is a plain elapsed-time check, not a job or a queue, so there is nothing
    to run and nothing that can get stuck. Ends in a handoff to the team
    rather than a charge: Stripe is not live yet (CLAUDE.md), so this is
    honest about what happens next instead of pretending to collect payment
    it cannot actually take.
    """

    REVIEW_WAIT = timedelta(minutes=5)

    package = models.ForeignKey(CustomPackage, on_delete=models.PROTECT, related_name="claims")
    org = models.ForeignKey("orgs.Organisation", on_delete=models.CASCADE, related_name="custom_package_claims")
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="custom_package_claims",
    )
    requested_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-requested_at"]

    def __str__(self):
        return f"{self.org.name} — {self.package.name}"

    @property
    def is_ready(self) -> bool:
        return timezone.now() >= self.requested_at + self.REVIEW_WAIT

    @property
    def ready_at(self):
        return self.requested_at + self.REVIEW_WAIT


class PlanOverride(models.Model):
    """Ian's own edits on top of the five published plans — billing.pricing's
    TIERS dict stays the seed and the fallback, this is the editable layer.

    CLIENT, 19 OCT 2026: "he can now be able to manually change it himself
    via the admin... we don't have to keep on going back and forth to the
    script." TIERS itself stays code (billing.pricing explains why: the key
    is an identifier used across the codebase, and nothing here renames
    one) — only the numbers next to each key move into a row Ian can edit
    without a deploy. A blank field here means "use the published default";
    billing.pricing.effective_tier_config is what merges the two.
    """

    tier = models.CharField(max_length=20, choices=TIER_CHOICES[:-1], unique=True)  # excludes CUSTOM
    price_aud = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    seat_limit = models.PositiveIntegerField(null=True, blank=True)
    child_org_limit = models.PositiveIntegerField(null=True, blank=True)
    # Either one drives the other — see static/js/gt-plan-admin.js. Stored
    # separately rather than derived at read time because the relationship
    # is only invertible one way at a time: whichever the admin typed last
    # is the one that is exact, and rounding the other on every page load
    # would make a saved 10% discount drift by a cent against a changing
    # list price.
    discount_percent = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    discount_amount_aud = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{tier_label(self.tier)} override"


class CustomPriceMessage(models.Model):
    """One message in a custom-price conversation, from either side.

    CLIENT, 24 OCT 2026: "being able to see some notifications, being able
    to see some follow-up if they have accepted... me being able to reach
    back". A request used to be one message and one decision; now either
    side can write again before or after it is decided — a question about
    the ask, a counter to an offer — and the other side is told (the team
    by email, the organisation in its bell panel and by email).
    """

    request = models.ForeignKey(CustomPriceRequest, on_delete=models.CASCADE, related_name="thread")
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="custom_price_messages",
    )
    from_team = models.BooleanField(default=False)
    body = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"{'Team' if self.from_team else self.request.org.name}: {self.body[:40]}"
