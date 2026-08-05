from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone


class Organisation(models.Model):
    name = models.CharField(max_length=200)
    # The org's own logo. Search is where this earns its place: names collide
    # ("Acme Footy Tips" exists three times across three states), and a member
    # hunting for their workplace recognises the mark long before they can pick
    # the right row out of a list of near-identical names. Optional — a group
    # that has no logo falls back to its initials rather than being blocked.
    logo = models.ImageField(upload_to="org_logos/", blank=True)
    # Org-structure note (§1, §3): a standalone org IS a parent org with zero
    # children — no separate object type, no hierarchy question at creation.
    # A child sits under exactly one parent (FK enforces that); a parent can
    # have unlimited children. Two levels only for now — the permission model
    # (§6) defines just parent and child admins — enforced in clean().
    # PROTECT: a parent with live children can't be silently deleted; support
    # must move or remove the children first.
    parent = models.ForeignKey(
        "self", on_delete=models.PROTECT,
        related_name="children", null=True, blank=True,
    )
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

    def clean(self):
        super().clean()
        if self.parent_id:
            if self.parent_id == self.pk:
                raise ValidationError({"parent": "An organisation cannot be its own parent."})
            if self.parent.parent_id:
                raise ValidationError({
                    "parent": "Child organisations can only sit under a top-level organisation."
                })
            if self.pk and self.children.exists():
                raise ValidationError({
                    "parent": "This organisation has child organisations of its own, "
                              "so it cannot become a child."
                })

    # --- Hierarchy helpers (org-structure note §3, §7) ---

    @property
    def is_child(self) -> bool:
        return self.parent_id is not None

    @property
    def root(self) -> "Organisation":
        """The top of this org's family: its parent, or itself if top-level."""
        return self.parent if self.parent_id else self

    def family(self):
        """Every org in this org's family: the root plus all its children.

        Viewed from a child, the family is still the WHOLE tree (§7: a member
        of a child org sees the national roll-up across the parent and all
        its siblings, not just their own branch).
        """
        root = self.root
        return Organisation.objects.filter(models.Q(pk=root.pk) | models.Q(parent=root))

    def family_ids(self) -> list[int]:
        return list(self.family().values_list("id", flat=True))

    @property
    def active_charity_vote(self):
        """The open charity vote for this org, or None."""
        return self.charity_votes.filter(status="open").first()

    @property
    def pending_election(self):
        """A draft or scheduled (not yet open) charity election, or None."""
        return self.charity_votes.filter(status__in=("draft", "scheduled")).first()

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


class OrgDraft(models.Model):
    """A part-finished trip through the create-a-group wizard.

    Creating a group asks for a fair bit — who you are, which codes you tip,
    and which charity — and people put that down mid-way to go and ask someone.
    Keeping it in the session meant a logout or a different browser threw the
    lot away and they started at step one. This is a row, so it survives both.

    The shape of `data` is deliberately just the raw form field values, keyed by
    field name, exactly as OrgCreateForm expects them. Nothing here interprets
    them — the one form still owns every validation rule, and the wizard only
    decides which of its errors to show on which step.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="org_draft",
    )
    data = models.JSONField(default=dict, blank=True)
    step = models.PositiveSmallIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Draft group for {self.user} (step {self.step})"

    @property
    def is_started(self) -> bool:
        """Whether there's anything worth offering to resume."""
        return bool(self.data.get("name"))


class MembershipRequest(models.Model):
    """A user asking to join an org they found by searching (org-structure
    note §2, amended by the client: a search-and-join is not instant — the
    org's admin approves or declines each request. Invite-link joins bypass
    this queue because the signed token IS the admin's authorisation.)

    Declined users may ask again — only one *pending* request per user/org
    is enforced, so the history of past decisions is kept.
    """

    STATUS_PENDING = "pending"
    STATUS_APPROVED = "approved"
    STATUS_DECLINED = "declined"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_APPROVED, "Approved"),
        (STATUS_DECLINED, "Declined"),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="membership_requests",
    )
    org = models.ForeignKey(
        Organisation, on_delete=models.CASCADE, related_name="membership_requests",
    )
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_PENDING)
    created_at = models.DateTimeField(auto_now_add=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="membership_requests_decided",
    )

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "org"],
                condition=models.Q(status="pending"),
                name="one_pending_membership_request_per_user_org",
            ),
        ]

    def __str__(self):
        return f"{self.user} → {self.org.name} ({self.status})"

    @property
    def is_pending(self) -> bool:
        return self.status == self.STATUS_PENDING


class CharityVote(models.Model):
    """A charity election.

    Lifecycle: draft (created with the comp, waiting for the admin to set it
    up) → scheduled (admin picked a date, or "now") → open (members voting,
    notified by email + in-app) → closed (winner locked in).
    Legacy votes created before scheduling existed go straight to "open".
    """

    STATUS_DRAFT = "draft"
    STATUS_SCHEDULED = "scheduled"
    STATUS_OPEN = "open"
    STATUS_CLOSED = "closed"
    STATUS_CHOICES = [
        (STATUS_DRAFT, "Draft"),
        (STATUS_SCHEDULED, "Scheduled"),
        (STATUS_OPEN, "Open"),
        (STATUS_CLOSED, "Closed"),
    ]

    org = models.ForeignKey(Organisation, on_delete=models.CASCADE, related_name="charity_votes")
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="open")
    opened_at = models.DateTimeField(auto_now_add=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    # When the admin schedules the election rather than starting it now.
    scheduled_open_at = models.DateTimeField(null=True, blank=True)
    # Optional end time — the vote auto-closes when this passes. The admin
    # can always close manually before (or instead of) this.
    scheduled_close_at = models.DateTimeField(null=True, blank=True)
    # The admin's note to members — lands in the email and the in-app popup.
    admin_message = models.TextField(blank=True)
    winning_charity = models.ForeignKey(
        "catalog.Charity",
        on_delete=models.PROTECT,
        related_name="won_charity_votes",
        null=True,
        blank=True,
    )
    # Stamps for the outbound emails, so a cron that runs every ten minutes
    # can't send the same reminder six times. Set once, then checked.
    reminder_day_sent_at = models.DateTimeField(null=True, blank=True)
    reminder_hour_sent_at = models.DateTimeField(null=True, blank=True)
    result_email_sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-opened_at"]

    def __str__(self):
        return f"Charity vote for {self.org.name} ({self.status})"

    @property
    def is_open(self) -> bool:
        return self.status == self.STATUS_OPEN

    @property
    def is_pending_setup(self) -> bool:
        return self.status in (self.STATUS_DRAFT, self.STATUS_SCHEDULED)


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


class Notification(models.Model):
    """An in-app notification — the popup + bell-panel side of what email
    delivers. Kept after dismissal so the panel doubles as a small feed of
    what the admin has said.
    """

    KIND_ELECTION_OPEN = "election_open"
    KIND_ELECTION_RESULT = "election_result"
    KIND_ELECTION_SCHEDULED = "election_sched"
    KIND_ADMIN_NOTE = "admin_note"
    KIND_WALL_REPLY = "wall_reply"
    KIND_WALL_POST = "wall_post"
    # Joining is a conversation with an admin, not an instant action, so each
    # step of it reports back here — otherwise the wait after "Ask to join"
    # looks like nothing happened.
    KIND_JOIN_REQUESTED = "join_requested"
    KIND_JOIN_REVIEW = "join_review"
    KIND_JOIN_APPROVED = "join_approved"
    KIND_JOIN_DECLINED = "join_declined"
    KIND_CHOICES = [
        (KIND_ELECTION_OPEN, "Charity election open"),
        (KIND_ELECTION_RESULT, "Charity election result"),
        (KIND_ELECTION_SCHEDULED, "Charity election scheduled"),
        (KIND_ADMIN_NOTE, "Note from your admin"),
        (KIND_WALL_REPLY, "Reply to your post"),
        (KIND_WALL_POST, "New post on your Wall"),
        (KIND_JOIN_REQUESTED, "Join request sent"),
        (KIND_JOIN_REVIEW, "Join request to review"),
        (KIND_JOIN_APPROVED, "Join request approved"),
        (KIND_JOIN_DECLINED, "Join request declined"),
    ]

    # Bell-panel icon + accent per kind, so the panel scans at a glance.
    ICONS = {
        KIND_ELECTION_OPEN: "ic-vote",
        KIND_ELECTION_RESULT: "ic-vote",
        KIND_ELECTION_SCHEDULED: "ic-calendar",
        KIND_ADMIN_NOTE: "ic-msg",
        KIND_WALL_REPLY: "ic-msg",
        KIND_WALL_POST: "ic-flame",
        KIND_JOIN_REQUESTED: "ic-clock",
        KIND_JOIN_REVIEW: "ic-people",
        KIND_JOIN_APPROVED: "ic-check",
        KIND_JOIN_DECLINED: "ic-msg",
    }

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications")
    org = models.ForeignKey(Organisation, on_delete=models.CASCADE, related_name="notifications", null=True, blank=True)
    kind = models.CharField(max_length=20, choices=KIND_CHOICES, default=KIND_ADMIN_NOTE)
    title = models.CharField(max_length=200)
    message = models.TextField(blank=True)
    link_url = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    read_at = models.DateTimeField(null=True, blank=True)
    # Dismissed = the popup was cleared; the row stays visible in the panel.
    dismissed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.get_kind_display()} → {self.user}"

    @property
    def is_unread(self) -> bool:
        return self.read_at is None

    @property
    def icon(self) -> str:
        return self.ICONS.get(self.kind, "ic-msg")


class WallPost(models.Model):
    """A post on a group's Wall — the members-only in-app feed.

    Private to the org by default: every read and write goes through a
    membership check. The ONE exception is is_public, which the author sets
    themselves on the composer ("also show this on the public wall"). A post
    reaches the public /wall/ page only when the author opted that post in
    AND the group's manager opted the group into public listing — two
    separate consents, either of which revokes it (see public_feed()).

    kind="recap" is reserved for the AI Group Recap card
    (docs/ai-group-recap-spec.md §2: one per round, pinned at the top of the
    feed, author=None). Recap generation isn't built yet; the feed already
    renders the kind so it lands without a schema change.
    """

    KIND_MEMBER = "member"
    KIND_SYSTEM = "system"
    KIND_RECAP = "recap"
    KIND_CHOICES = [
        (KIND_MEMBER, "Member post"),
        (KIND_SYSTEM, "GoodTip update"),
        (KIND_RECAP, "AI Group Recap"),
    ]

    org = models.ForeignKey(Organisation, on_delete=models.CASCADE, related_name="wall_posts")
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        null=True, blank=True, related_name="wall_posts",
    )
    kind = models.CharField(max_length=10, choices=KIND_CHOICES, default=KIND_MEMBER)
    body = models.TextField(max_length=500)
    # Optional: the author's own pick this post talks up — renders as a
    # "★ Pies to win" chip so reacting to the games you tipped is one tap.
    tip = models.ForeignKey(
        "tipping.Tip", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="wall_posts",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    # Admin hide (recap spec §11): gone from the feed, data kept.
    is_hidden = models.BooleanField(default=False)
    # Author's own choice, per post, default off — never inherited from a
    # previous post and never set on their behalf.
    is_public = models.BooleanField(default=False)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["org", "-created_at"]),
            models.Index(fields=["is_public", "-created_at"]),
        ]

    def __str__(self):
        who = self.author or "GoodTip"
        return f"{who} on {self.org.name} wall ({self.kind})"

    @classmethod
    def public_feed(cls):
        """Every post cleared for the world: the author opted this post in and
        the group is publicly listed. Both consents are live — revoking either
        pulls the post off the public page on the next request.

        System and recap cards stay out of it: they carry a group's internal
        numbers (ladder moves, donation totals) that nobody consented to
        publish.
        """
        return (
            cls.objects.filter(
                is_public=True,
                is_hidden=False,
                kind=cls.KIND_MEMBER,
                org__is_public_listed=True,
                author__isnull=False,
            )
            .select_related(
                "author", "org", "org__group_type", "org__season",
                "tip__match__home_team", "tip__match__away_team",
            )
            # The card prints the group's category and competitions, so pull
            # them in one go rather than per post.
            .prefetch_related("org__sub_categories", "org__competitions")
            .annotate(reaction_total=models.Count("reactions", distinct=True))
        )


class RoundRecap(models.Model):
    """One AI Group Recap per (org, round) — docs/ai-group-recap-spec.md.

    The row is the idempotency lock: generation skips any pair that already
    has one, so a recap is never silently rewritten (§3 — if an admin
    corrects a result, flag needs_review instead of regenerating).
    """

    org = models.ForeignKey(Organisation, on_delete=models.CASCADE, related_name="round_recaps")
    round = models.ForeignKey("tipping.Round", on_delete=models.CASCADE, related_name="recaps")
    post = models.OneToOneField(
        WallPost, on_delete=models.SET_NULL, null=True, blank=True, related_name="recap",
    )
    # True when the LLM call failed and the plain factual line was posted
    # instead (§10) — worth re-running by hand once the API is reachable.
    fallback_used = models.BooleanField(default=False)
    model_used = models.CharField(max_length=60, blank=True)
    # Set when a result correction touches this round after the recap posted
    # (§3): surfaced to the admin rather than silently regenerated.
    needs_review = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("org", "round")

    def __str__(self):
        return f"Recap R{self.round.round_number} — {self.org.name}"


class WallReply(models.Model):
    """A reply under a wall post.

    Two ways in, with different trust:

    * A signed-in member of the post's org — posts straight into the thread,
      exactly like the post itself.
    * A visitor on the public /wall/ page — leaves a name and an email and the
      reply is held (is_approved=False) until a staffer clears it in the admin.
      Nothing an anonymous visitor types appears on a public marketing page
      before a human has read it.

    Guest email is contact detail for moderation, never rendered anywhere.
    """

    post = models.ForeignKey(WallPost, on_delete=models.CASCADE, related_name="replies")
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        null=True, blank=True, related_name="wall_replies",
    )
    guest_name = models.CharField(max_length=80, blank=True)
    guest_email = models.EmailField(blank=True)
    body = models.TextField(max_length=500)
    created_at = models.DateTimeField(auto_now_add=True)
    # Members are approved on arrival; guests wait for a staff review.
    is_approved = models.BooleanField(default=True)
    is_hidden = models.BooleanField(default=False)
    # Kept for rate-limiting and abuse follow-up on guest replies only.
    ip_address = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        ordering = ["created_at"]
        indexes = [models.Index(fields=["post", "created_at"])]
        verbose_name_plural = "wall replies"

    def __str__(self):
        return f"{self.display_name} on #{self.post_id}"

    @property
    def is_guest(self) -> bool:
        return self.author_id is None

    @property
    def display_name(self) -> str:
        if self.author_id:
            return self.author.display_name
        return self.guest_name or "Guest"

    @property
    def initials(self) -> str:
        return (self.display_name or "?")[:2].upper()


class WallReaction(models.Model):
    """One member's emoji reaction to one wall post. Toggled, not stacked —
    a member can add each emoji to a post once.
    """

    EMOJI_CHOICES = [
        ("fire", "🔥"),
        ("laugh", "😂"),
        ("heart", "❤️"),
        ("clap", "👏"),
        ("eyes", "👀"),
    ]

    post = models.ForeignKey(WallPost, on_delete=models.CASCADE, related_name="reactions")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="wall_reactions")
    emoji = models.CharField(max_length=8, choices=EMOJI_CHOICES)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("post", "user", "emoji")

    def __str__(self):
        return f"{self.user} {self.get_emoji_display()} on #{self.post_id}"
