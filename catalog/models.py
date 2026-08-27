from django.conf import settings
from django.db import models


class Sport(models.Model):
    """The code itself — the top of the hierarchy (Ambrose brief, slide 7).

    Examples: Rugby League, Australian Rules, Netball. A Sport is season- and
    brand-independent; the competitions people actually join (NRL, AFL) sit two
    levels below it via ``Series`` → ``Competition``.
    """

    name = models.CharField(max_length=50, unique=True)
    slug = models.SlugField(max_length=50, unique=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Series(models.Model):
    """A specific competition running under a Sport, e.g. AFL, AFLW, NRL, NRLW,
    State of Origin (Ambrose brief, slide 7: the "Series" level).

    Every Series is fully integrated into its Sport's single leaderboard —
    ``representation_type`` is FULL, meaning no opt-out (Fixtures Reference,
    §4: "NRLW is structural in GoodTip — representation_type = FULL, no opt-out").
    """

    CATEGORY_MENS = "mens"
    CATEGORY_WOMENS = "womens"
    CATEGORY_REPRESENTATIVE = "representative"
    CATEGORY_CHOICES = [
        (CATEGORY_MENS, "Men's"),
        (CATEGORY_WOMENS, "Women's"),
        (CATEGORY_REPRESENTATIVE, "Representative"),
    ]

    REPRESENTATION_FULL = "full"
    REPRESENTATION_CHOICES = [
        (REPRESENTATION_FULL, "Fully integrated — no opt-out"),
    ]

    sport = models.ForeignKey(Sport, on_delete=models.CASCADE, related_name="series")
    name = models.CharField(max_length=50, unique=True)
    slug = models.SlugField(max_length=50, unique=True)
    is_womens = models.BooleanField(default=False)
    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES, default=CATEGORY_MENS)
    representation_type = models.CharField(
        max_length=10, choices=REPRESENTATION_CHOICES, default=REPRESENTATION_FULL
    )

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "series"

    def __str__(self):
        return self.name


class Season(models.Model):
    """A playing season, identified by its year."""

    year = models.IntegerField(unique=True)
    label = models.CharField(max_length=50, blank=True)

    class Meta:
        ordering = ["-year"]

    def __str__(self):
        return self.label or str(self.year)


class Competition(models.Model):
    """What orgs join & tip on (Ambrose brief, slide 7): a Sport's Series bundled
    for one season, e.g. "NRL (2026)" = NRL + NRLW + State of Origin.

    This is the annual commitment a league signs up for. Fixtures are keyed to a
    Competition so a season's tipping is a simple ``WHERE competition_id = X``
    (brief slide 9), and a new series can be added to a competition without
    touching fixtures logic.
    """

    sport = models.ForeignKey(Sport, on_delete=models.CASCADE, related_name="competitions")
    season = models.ForeignKey(Season, on_delete=models.PROTECT, related_name="competitions")
    name = models.CharField(max_length=50)
    slug = models.SlugField(max_length=50)
    # Which series make up this competition this season (men's + women's + reps).
    series = models.ManyToManyField(Series, related_name="competitions", blank=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["slug", "season"], name="uniq_competition_slug_per_season"),
        ]

    def __str__(self):
        return f"{self.name} ({self.season})"

    @classmethod
    def for_series(cls, series, season):
        """The competition that includes this series in the given season (or None).

        Ordered by id so a stray duplicate competition can never make rounds
        land under two different comps (the "AFL + AFL 2026" bug).
        """
        return cls.objects.filter(series=series, season=season).order_by("id").first()


class State(models.Model):
    """An Australian state/territory — a lookup table so the Good List's
    "By State" aggregate groups by a stable id, not a free-text string.
    """

    code = models.CharField(max_length=3, unique=True)  # NSW, VIC, QLD…
    name = models.CharField(max_length=50, unique=True)
    # Sort order for display (roughly by population); ties fall back to name.
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "name"]

    def __str__(self):
        return self.name


class Country(models.Model):
    """Where a group is actually based.

    ASKED AT GROUP LEVEL, NOT ORG LEVEL. One business can run offices in
    Sydney, Melbourne and Port Moresby under a single organisation, so an
    org-level answer cannot describe it — the org would have to claim to be in
    one country while most of its people were not. Groups carry the answer and
    the organisation carries the default, which is what an org with no groups
    (most of them) uses. See Organisation.country / Group.country.

    THE SEGMENT IS DERIVED, NOT ASKED. Nobody should have to answer "which
    market are you in" on top of "where are you" — one field, no double entry.

    NEW ZEALAND CURRENTLY SITS IN THE AUSTRALIA SEGMENT. The choice is still
    here, and NZ is still its own COUNTRY — the Good List's country breakdown
    lists it separately, and always did. What folding means is narrower: a
    segment is what a future ladder or charity shortlist would be split on,
    and the client's instruction is that New Zealand does not get its own
    yet ("they'll get their own charity shortlist and leaderboard down the
    track, but that's a later item"). Until then NZ groups compete on the
    main ladder, so they carry the same segment as the groups they are
    competing against.

    Splitting it back out is a data migration and nothing else, which is
    precisely why the segment stayed a field on the country rather than
    being inferred from the code wherever it was needed.

    NOT A CURRENCY. Payment stays in AUD whatever is chosen here; this exists
    to know where a group is based so it can be segmented cleanly, and changes
    nothing about pricing.
    """

    SEGMENT_AUSTRALIA = "australia"
    # Defined, and deliberately unused today — see the class docstring. Kept
    # so that splitting New Zealand out later is a migration rather than a
    # schema change plus a hunt for everywhere the string was written out.
    SEGMENT_NEW_ZEALAND = "new_zealand"
    SEGMENT_GLOBAL = "global"
    SEGMENT_CHOICES = [
        (SEGMENT_AUSTRALIA, "Australia"),
        (SEGMENT_NEW_ZEALAND, "New Zealand"),
        (SEGMENT_GLOBAL, "Global"),
    ]

    # ISO 3166-1 alpha-2, so this lines up with anything external later.
    code = models.CharField(max_length=2, unique=True)
    name = models.CharField(max_length=60, unique=True)
    segment = models.CharField(
        max_length=12, choices=SEGMENT_CHOICES, default=SEGMENT_GLOBAL,
    )
    # Kept separate from `segment` deliberately. Today every non-AU/NZ country
    # here is a Pacific nation, so the two look interchangeable — but the
    # moment a country outside the Pacific is added it would be swept into the
    # Pacific Nations board purely for not being Australian.
    is_pacific = models.BooleanField(default=False)
    sort_order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["sort_order", "name"]
        verbose_name_plural = "countries"

    def __str__(self):
        return self.name


class OrganisationType(models.Model):
    """The organisation type an org self-selects at sign-up (categories doc,
    7 Jul 2026): Community, Business, Education, Charities, Informal — in that
    order, which drives both the sign-up dropdown and the Good List filters.

    A lookup table (not a char choice) so the Good List filters by a stable id,
    and new classifications can be added without a schema change.
    """

    # Slugs with behaviour attached (categories doc): Charities gets the partner
    # workflow, Informal self-describes instead of picking a sub-category.
    SLUG_COMMUNITY = "community"
    SLUG_BUSINESS = "business"
    SLUG_EDUCATION = "education"
    SLUG_CHARITIES = "charities"
    SLUG_INFORMAL = "informal"

    slug = models.SlugField(max_length=30, unique=True)
    name = models.CharField(max_length=50, unique=True)
    # FORMAL OR INFORMAL — the first question the signup wizard asks, and the
    # one that decides what validation the rest of it applies.
    #
    # It used to be implied by the type, several screens in, which is why
    # someone setting up a family comp or a mates' group with a Gmail address
    # ran into workplace validation before anything had established that they
    # were not a workplace. Asking it first means an informal group never
    # meets a rule written for an employer.
    #
    # A field rather than a slug test so the GoodTip team can add a type in
    # admin and say which side it falls on, without a code change.
    is_formal = models.BooleanField(
        default=True,
        help_text="Formal: workplace, school, club, registered entity. "
                  "Informal: mates, family, community group.",
    )
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "name"]

    def __str__(self):
        return self.name

    @property
    def has_sub_categories(self) -> bool:
        return self.sub_categories.filter(is_active=True).exists()

    @property
    def is_education(self) -> bool:
        return self.slug == self.SLUG_EDUCATION

    @property
    def is_charity_type(self) -> bool:
        return self.slug == self.SLUG_CHARITIES

    @property
    def is_informal(self) -> bool:
        return self.slug == self.SLUG_INFORMAL


class SubCategory(models.Model):
    """A sub-category within an organisation type (categories doc, 7 Jul 2026),
    e.g. Business → Finance, Community → Sports Club, Education → University.

    Replaces the old flat ``Industry`` table: the same idea, but scoped to its
    parent ``OrganisationType`` so the sign-up dropdown and the Good List filters show
    only the sub-categories that belong to the selected type. Charities and
    Informal deliberately have none — Informal orgs self-describe instead.

    Seeded from the spec; the GoodTip team can edit the list in admin without a
    redeploy.
    """

    organisation_type = models.ForeignKey(OrganisationType, on_delete=models.CASCADE, related_name="sub_categories")
    name = models.CharField(max_length=80)
    slug = models.SlugField(max_length=80)
    sort_order = models.PositiveIntegerField(default=0)
    # Hidden sub-categories drop out of the picker without deleting history.
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["organisation_type__sort_order", "sort_order", "name"]
        verbose_name_plural = "sub-categories"
        constraints = [
            models.UniqueConstraint(fields=["organisation_type", "slug"], name="uniq_subcategory_slug_per_type"),
            models.UniqueConstraint(fields=["organisation_type", "name"], name="uniq_subcategory_name_per_type"),
        ]

    def __str__(self):
        return f"{self.name} ({self.organisation_type.name})"


class GoodListConfig(models.Model):
    """Singleton holding the Good List's two tunable thresholds (spec §7).

    Kept in the DB (not settings) so Hop can tune them in admin without a
    redeploy, as the spec explicitly requires.
    """

    # §7.1 — an aggregate (charity/state/industry) only shows publicly once at
    # least this many groups sit behind it, so small-n figures can't be
    # reverse-engineered to a named group.
    privacy_min_groups = models.PositiveIntegerField(default=5)
    # §7.2 — the public By Group board stays hidden until at least this many
    # named, consenting groups with settled totals exist.
    credibility_min_groups = models.PositiveIntegerField(default=10)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Good List config"
        verbose_name_plural = "Good List config"

    def __str__(self):
        return f"Good List config (privacy≥{self.privacy_min_groups}, credibility≥{self.credibility_min_groups})"

    @classmethod
    def get(cls) -> "GoodListConfig":
        """Return the single config row, creating it with defaults if absent."""
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class CharityQuerySet(models.QuerySet):
    """Charity visibility, in one place so no picker has to reinvent it."""

    def approved(self):
        return self.filter(is_approved=True)

    def available_to(self, org):
        """Every charity `org` may pick or put on a ballot.

        The vetted global list, PLUS the charities this organisation added for
        itself. An org-added charity is usable the moment it is created — the
        admin who typed it is mid-task and should not be told to come back
        later — but it stays `is_approved=False` so it does not appear in
        anyone else's picker until GoodTip has looked at it.

        A group's charities are its organisation's: the org is the one that
        adds them, and its groups choose from what it has made available.
        """
        if org is None:
            return self.approved()
        return self.filter(models.Q(is_approved=True) | models.Q(owner_org=org))


class Charity(models.Model):
    """A charity a league can raise funds for.

    Approved charities are vetted by GoodTip and appear in the public picker.
    Custom charities added by a league creator start unapproved.

    WHO CAN ADD ONE. Only an organisation admin, from Manage → Charities. The
    creation wizard deliberately does NOT offer it any more: someone standing
    up their first league is the person least able to judge whether "Cancer
    Council" and "The Cancer Council" are the same row, and every typo made
    there became a permanent near-duplicate in the picker everyone else reads.
    Pick from the list at creation; add your own later, once you are in.
    """

    name = models.CharField(max_length=200, unique=True)
    slug = models.SlugField(max_length=200, unique=True)
    website = models.URLField(blank=True)
    # The organisation that added this one, when it wasn't GoodTip. Scopes
    # visibility (see CharityQuerySet.available_to) and tells the superadmin
    # review queue who to ask about a name it doesn't recognise. NULL means
    # curated centrally, which is what every vetted charity is.
    owner_org = models.ForeignKey(
        "orgs.Organisation",
        on_delete=models.SET_NULL,
        related_name="owned_charities",
        null=True,
        blank=True,
    )
    # Who typed it, for the same reason.
    added_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="charities_added",
        null=True,
        blank=True,
    )
    added_at = models.DateTimeField(null=True, blank=True)
    # Fetched once from the charity's own site — see catalog.logos. Blank is
    # an ordinary state, not a failure: `initials` covers it, so a card is
    # never rendered half-built waiting on a network call that may never
    # succeed.
    logo = models.ImageField(upload_to="charity_logos/", blank=True)
    # Stamped whether the fetch found anything or not, so a charity whose site
    # has no usable icon is not re-fetched on every pass.
    logo_fetched_at = models.DateTimeField(null=True, blank=True)
    is_approved = models.BooleanField(default=False)

    objects = CharityQuerySet.as_manager()

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "charities"

    def __str__(self):
        return self.name

    @property
    def initials(self) -> str:
        """One or two letters for the fallback tile.

        Real charity names carry noise words a reader does not use to identify
        them — "The Smith Family" is SF, not TS — so those are dropped before
        taking initials. A single-word name gives its first two letters, which
        reads better at tile size than one lonely capital.
        """
        skip = {"the", "of", "and", "for", "a", "an", "australia", "australian"}
        words = [w for w in self.name.split() if w.strip(".,'\"")]
        kept = [w for w in words if w.lower().strip(".,") not in skip] or words
        if not kept:
            return "?"
        if len(kept) == 1:
            return kept[0][:2].upper()
        return (kept[0][:1] + kept[1][:1]).upper()

    @property
    def website_label(self) -> str:
        """The website without the scheme or a trailing slash.

        "beyondblue.org.au" is what people recognise;
        "https://www.beyondblue.org.au/" is the same thing wearing a hat.
        """
        url = (self.website or "").strip()
        for prefix in ("https://", "http://"):
            if url.startswith(prefix):
                url = url[len(prefix):]
                break
        if url.startswith("www."):
            url = url[4:]
        return url.rstrip("/")

    @property
    def tile_hue(self) -> int:
        """A stable colour for the fallback tile, derived from the name.

        Stable is the whole point: the same charity is the same colour on the
        picker, the ballot and the result, so the tile works as recognition
        rather than as decoration that reshuffles per page.
        """
        return sum(ord(c) for c in self.name) % 360


class GroupType(models.Model):
    """A ready-made department name, offered when someone creates a department
    inside an organisation (e.g. Business → IT, Finance, People & Culture).

    Scoped to a ``OrganisationType`` the same way ``SubCategory`` is, so a bank is
    offered banking-shaped departments and a school is offered Year Levels
    rather than a Warehouse. ``organisation_type`` NULL means "offer this to
    everyone" — Finance and IT exist in almost every organisation, and
    duplicating them under all five types would be a list to maintain in five
    places.

    A picker, never a constraint. The create form keeps a free-text field
    beside this list, because the point of departments is that a big
    organisation carves itself up the way IT ACTUALLY is, not the way a
    dropdown assumed. The typed name is stored on the org as
    ``department_label`` and no row is created here, so one team calling
    itself "The Cave" does not pollute the list everybody else sees.
    """

    organisation_type = models.ForeignKey(
        OrganisationType, on_delete=models.CASCADE, related_name="group_types",
        null=True, blank=True,
    )
    name = models.CharField(max_length=80)
    slug = models.SlugField(max_length=80)
    sort_order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["sort_order", "name"]
        # Same name may exist under different types (Education "Admin" is not
        # Business "Admin"), so uniqueness is per type rather than global.
        unique_together = [("organisation_type", "slug")]

    def __str__(self):
        return f"{self.organisation_type.name} → {self.name}" if self.organisation_type_id else self.name
