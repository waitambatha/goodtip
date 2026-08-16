"""Storage for MatchReader: the history it learns from, and what it learned.

Deliberately separate from tipping.Match. A tipping Match belongs to an
organisation's round — twenty leagues tipping the same fixture hold twenty
rows of it. The model does not care about organisations at all; it wants each
real-world game exactly once, across as many seasons as we can get. Training
off the org-scoped table would weight a fixture by how many leagues happened
to tip it.
"""

from django.db import models


class HistoricalMatch(models.Model):
    """One real-world game, stored once, used for fitting and validation.

    Also the source the competition ladder is derived from — it is the only
    table holding whole seasons independent of who tipped them.
    """

    STAGE_REGULAR = "regular"
    STAGE_FINALS = "finals"
    STAGE_CHOICES = [(STAGE_REGULAR, "Regular season"), (STAGE_FINALS, "Finals")]

    series = models.ForeignKey(
        "catalog.Series", on_delete=models.CASCADE, related_name="historical_matches"
    )
    season = models.PositiveIntegerField()
    round_number = models.PositiveIntegerField()
    # Finals must stay off the ladder — a ladder is the regular-season table,
    # and it is what decides who plays finals in the first place.
    stage = models.CharField(max_length=10, choices=STAGE_CHOICES, default=STAGE_REGULAR)
    external_id = models.CharField(max_length=100)
    home_team = models.ForeignKey(
        "tipping.Team", on_delete=models.CASCADE, related_name="historical_home"
    )
    away_team = models.ForeignKey(
        "tipping.Team", on_delete=models.CASCADE, related_name="historical_away"
    )
    kickoff_at = models.DateTimeField()
    home_score = models.IntegerField()
    away_score = models.IntegerField()

    class Meta:
        # TWO uniqueness rules, because one was not enough.
        #
        # (series, external_id) keeps a re-run of the same source idempotent.
        # It does NOT survive a change of source: Sportradar ids
        # ("sr:sport_event:45559680") and afl.com.au ids ("CD_M20240140101")
        # describe the same game under different schemes, so backfilling AFL
        # 2024 from both stored every fixture twice — 422 rows for a 207-game
        # season — and MatchReader was fitted on the doubled history.
        #
        # The natural key is what actually identifies a game: these two clubs,
        # that round, that season. It holds across any source.
        unique_together = [
            ("series", "external_id"),
            ("series", "season", "round_number", "home_team", "away_team"),
        ]
        indexes = [
            models.Index(fields=["series", "season", "round_number"]),
            models.Index(fields=["kickoff_at"]),
        ]
        ordering = ["kickoff_at"]

    def __str__(self):
        return f"{self.home_team} {self.home_score}-{self.away_score} {self.away_team}"

    @property
    def home_won(self) -> bool:
        return self.home_score > self.away_score


class ModelVersion(models.Model):
    """A fitted MatchReader, with the numbers needed to justify it.

    Kept rather than overwritten so a prediction shown last week can still be
    explained, and so a refit that makes accuracy worse can be rolled back by
    flipping is_active rather than retraining backwards.
    """

    series = models.ForeignKey(
        "catalog.Series", on_delete=models.CASCADE, related_name="matchreader_models"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    # z = intercept + form_coef*dForm + exp_coef*dExp
    intercept = models.FloatField()
    form_coef = models.FloatField()
    # Null until a lineup feed exists. The model is fitted on whatever signals
    # are actually available, and this records honestly which those were.
    exp_coef = models.FloatField(null=True, blank=True)

    train_seasons = models.CharField(max_length=100)
    test_seasons = models.CharField(max_length=100)
    train_samples = models.PositiveIntegerField()
    test_samples = models.PositiveIntegerField()
    # Out-of-sample: fitted on train_seasons, scored on seasons it never saw.
    accuracy = models.FloatField()
    baseline_accuracy = models.FloatField(
        help_text="Always-pick-home accuracy on the same test set. A model that "
                  "cannot beat this is not worth shipping."
    )
    is_active = models.BooleanField(default=False)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.series} model {self.created_at:%Y-%m-%d} ({self.accuracy:.1%})"
