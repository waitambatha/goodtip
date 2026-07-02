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
        """The competition that includes this series in the given season (or None)."""
        return cls.objects.filter(series=series, season=season).first()


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


class GroupType(models.Model):
    """How a league is classified — workplace vs. community group.

    A lookup table (not a char choice) so the Good List keeps the two surfaces
    apart by id, and new classifications can be added without a schema change.
    """

    slug = models.SlugField(max_length=30, unique=True)  # workplace, community
    name = models.CharField(max_length=50, unique=True)
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "name"]

    def __str__(self):
        return self.name


class Industry(models.Model):
    """A workplace sector, used to group organisations for the Good List's
    "By Industry" aggregate (Good List spec §3, §8).

    Seeded with a starter taxonomy; the GoodTip team can edit the list in admin
    without a redeploy. New leagues pick one when they set up.
    """

    name = models.CharField(max_length=80, unique=True)
    slug = models.SlugField(max_length=80, unique=True)
    # Hidden industries drop out of the picker without deleting historical data.
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "industries"

    def __str__(self):
        return self.name


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


class Charity(models.Model):
    """A charity a league can raise funds for.

    Approved charities are vetted by GoodTip and appear in the public picker.
    Custom charities added by a league creator start unapproved.
    """

    name = models.CharField(max_length=200, unique=True)
    slug = models.SlugField(max_length=200, unique=True)
    website = models.URLField(blank=True)
    is_approved = models.BooleanField(default=False)

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "charities"

    def __str__(self):
        return self.name
