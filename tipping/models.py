from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone


class Team(models.Model):
    name = models.CharField(max_length=100)
    slug = models.CharField(max_length=100)
    series = models.ForeignKey("catalog.Series", on_delete=models.PROTECT, related_name="teams")
    external_id = models.CharField(max_length=100, blank=True)
    # Real club logo. Uploaded files win; otherwise the crest template tag
    # falls back to a bundled static file (static/img/teams/<slug>.png) and
    # finally to the generated monogram badge.
    logo = models.ImageField(upload_to="team_logos/", blank=True, null=True)

    class Meta:
        unique_together = ("slug", "series")
        ordering = ["series", "name"]

    def __str__(self):
        return f"{self.name} [{self.series}]"


class Round(models.Model):
    STATUS_CHOICES = [
        ("upcoming", "Upcoming"),
        ("open", "Open"),
        ("locked", "Locked"),
        ("complete", "Complete"),
    ]

    # Match type drives the score weighting (Ambrose Hierarchy brief, slide 6):
    # regular rounds are the baseline; finals raise the stakes; State of Origin is
    # the prestige event worth the most.
    STAGE_REGULAR = "regular"
    STAGE_FINALS = "finals"
    STAGE_ORIGIN = "origin"
    STAGE_CHOICES = [
        (STAGE_REGULAR, "Regular round"),
        (STAGE_FINALS, "Finals"),
        (STAGE_ORIGIN, "State of Origin"),
    ]
    POINTS_PER_STAGE = {
        STAGE_REGULAR: 1,
        STAGE_FINALS: 2,
        STAGE_ORIGIN: 4,
    }
    # What a DRAWN match pays, per stage (Scoring & Tiebreaker Addendum rev 3,
    # §1). A draw is worth nothing in a regular round or a final; State of
    # Origin alone pays half, because the addendum scores it "4 pts / 2 pts
    # draw".
    #
    # Finals draws are listed in the addendum as unspecified — both codes
    # resolve finals with extra time or golden point rather than ending level,
    # so the case is expected never to arise. Nothing is paid until somebody
    # confirms otherwise, which is the correction-safe direction: awarding
    # points that a ruling later removes is far worse than awarding them late.
    DRAW_POINTS_PER_STAGE = {
        STAGE_REGULAR: 0,
        STAGE_FINALS: 0,
        STAGE_ORIGIN: 2,
    }

    org = models.ForeignKey("orgs.Organisation", on_delete=models.CASCADE, related_name="rounds")
    round_number = models.IntegerField()
    # The competition this round is tipped under (deck: fixtures keyed to a
    # competition_id). The series says whether it's the men's/women's/Origin slate.
    competition = models.ForeignKey(
        "catalog.Competition", on_delete=models.PROTECT, related_name="rounds", null=True, blank=True
    )
    series = models.ForeignKey("catalog.Series", on_delete=models.PROTECT, related_name="rounds")
    stage = models.CharField(max_length=10, choices=STAGE_CHOICES, default=STAGE_REGULAR)
    lockout_at = models.DateTimeField()
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="upcoming")
    # Set when the "how your tips went" emails go out for this round, so the
    # scheduled job can't mail everyone twice.
    results_email_sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-round_number"]
        unique_together = ("org", "round_number", "series")

    def __str__(self):
        return f"Round {self.round_number} [{self.series}] — {self.org.name}"

    @property
    def points_per_correct(self) -> int:
        """Points awarded for each correct tip in this round (1 / 2 / 4)."""
        return self.POINTS_PER_STAGE.get(self.stage, 1)

    @property
    def points_per_draw(self) -> int:
        """Points every tipper gets when a match in this round is drawn.

        Nobody picked correctly — there is no winner to have picked — so this
        is not a reward for being right. It is the addendum's rule that an
        Origin draw still pays half, on the reasoning that three games a series
        are too scarce to have one of them count for nothing.
        """
        return self.DRAW_POINTS_PER_STAGE.get(self.stage, 0)

    @property
    def is_locked(self) -> bool:
        return self.status in ("locked", "complete") or timezone.now() >= self.lockout_at

    @property
    def has_open_matches(self) -> bool:
        """Any fixture in this round that has not kicked off yet.

        The distinction that matters once tipping is per-match: `is_locked` asks
        whether the round's first game has started, which says nothing about
        whether there is still something to tip. A Thursday-night opener locks
        the round while Saturday and Sunday are wide open.
        """
        if self.status == "complete":
            return False
        return self.matches.filter(kickoff_at__gt=timezone.now()).exists()

    @property
    def effective_status(self) -> str:
        if self.status == "complete":
            return "complete"
        if self.is_locked:
            return "locked"
        if self.status == "upcoming" and timezone.now() < self.lockout_at:
            return "open"
        return self.status


class Match(models.Model):
    RESULT_CHOICES = [("home", "Home"), ("away", "Away"), ("draw", "Draw")]

    # Live state, refreshed by the data_sync live poller. Kept separate from
    # ``result``: result is the graded outcome that scores tips and must not
    # move once set, while these fields churn every few minutes during play.
    STATUS_SCHEDULED = "scheduled"
    STATUS_LIVE = "live"
    STATUS_COMPLETE = "complete"
    STATUS_POSTPONED = "postponed"
    STATUS_CHOICES = [
        (STATUS_SCHEDULED, "Scheduled"),
        (STATUS_LIVE, "In play"),
        (STATUS_COMPLETE, "Complete"),
        (STATUS_POSTPONED, "Postponed"),
    ]

    round = models.ForeignKey(Round, on_delete=models.CASCADE, related_name="matches")
    home_team = models.ForeignKey(Team, on_delete=models.PROTECT, related_name="home_matches")
    away_team = models.ForeignKey(Team, on_delete=models.PROTECT, related_name="away_matches")
    kickoff_at = models.DateTimeField()
    venue = models.CharField(max_length=200, blank=True)
    # Split out so "where is this being played" can be shown as a place, not
    # just a ground name that only locals recognise.
    venue_city = models.CharField(max_length=100, blank=True)
    result = models.CharField(max_length=10, choices=RESULT_CHOICES, null=True, blank=True)
    home_score = models.IntegerField(null=True, blank=True)
    away_score = models.IntegerField(null=True, blank=True)
    external_id = models.CharField(max_length=100, blank=True)

    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=STATUS_SCHEDULED)
    # Free text from the feed: "Q3", "Half Time", "2nd Half", "Full Time".
    period = models.CharField(max_length=32, blank=True)
    # Clock within the period as the feed words it: "12:45", "66'".
    clock = models.CharField(max_length=16, blank=True)
    # 0–100. Squiggle reports this directly; other feeds can derive it.
    progress = models.PositiveSmallIntegerField(default=0)
    live_updated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["kickoff_at"]
        indexes = [
            # The live poller and the My Tips filters both slice on these.
            models.Index(fields=["status", "kickoff_at"]),
        ]

    def __str__(self):
        return f"{self.home_team.name} v {self.away_team.name}"

    @property
    def is_locked(self) -> bool:
        return timezone.now() >= self.kickoff_at

    @property
    def is_open(self) -> bool:
        """Still tippable. The positive form of is_locked, for templates.

        Django templates have no `not`, so a card that wanted "editable while
        unlocked" was reaching for `is_locked|yesno:",True"` — correct, and
        unreadable. The negation belongs here where it can be named.
        """
        return not self.is_locked

    @property
    def has_result(self) -> bool:
        return self.result is not None

    # Longest a match is presumed to still be in progress once it has started,
    # for fixtures the live poller has never reached. Covers a long AFL game
    # plus a delay; past it, an ungraded match is over rather than in play.
    ASSUMED_MAX_DURATION = timedelta(hours=4)

    @property
    def phase(self) -> str:
        """Which bucket this match belongs in: upcoming / live / complete.

        Derived rather than read straight off ``status`` so the My Tips filters
        still sort correctly for matches the live poller has never touched.
        Two cases it has to get right without any feed data: a fixture that
        kicked off minutes ago is live, and one that kicked off last month is
        finished — not still running.
        """
        if self.status == self.STATUS_COMPLETE or self.result is not None:
            return "complete"
        if self.status == self.STATUS_LIVE:
            return "live"
        if self.status == self.STATUS_POSTPONED:
            return "upcoming"
        if not self.is_locked:
            return "upcoming"
        started_ago = timezone.now() - self.kickoff_at
        return "live" if started_ago <= self.ASSUMED_MAX_DURATION else "complete"

    @property
    def is_live(self) -> bool:
        return self.phase == "live"

    @property
    def live_label(self) -> str:
        """The betting-board style clock: "Q3 12:45", "66'", "Half Time"."""
        parts = [p for p in (self.period, self.clock) if p]
        if parts:
            return " ".join(parts)
        return "In play" if self.phase == "live" else ""

    @property
    def score_line(self) -> str:
        if self.home_score is None or self.away_score is None:
            return ""
        return f"{self.home_score}–{self.away_score}"

    @property
    def venue_label(self) -> str:
        if self.venue and self.venue_city:
            return f"{self.venue}, {self.venue_city}"
        return self.venue or self.venue_city or ""


class Tip(models.Model):
    SELECTION_CHOICES = [("home", "Home"), ("away", "Away")]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="tips")
    match = models.ForeignKey(Match, on_delete=models.CASCADE, related_name="tips")
    org = models.ForeignKey("orgs.Organisation", on_delete=models.CASCADE, related_name="tips")
    selection = models.CharField(max_length=10, choices=SELECTION_CHOICES)
    submitted_at = models.DateTimeField(auto_now=True)
    # Filled in by the system because the member did not tip before lock —
    # the away side, per the addendum's missed-tip default (§2). Flagged
    # rather than written silently: a score you did not choose has to be
    # distinguishable from one you did, both on screen and in any dispute
    # about how somebody's total was arrived at.
    is_auto = models.BooleanField(default=False)
    is_correct = models.BooleanField(null=True, blank=True)
    # Weighted score this tip earned once graded: round.points_per_correct if
    # correct, else 0. Stored so the leaderboard can Sum() instead of Count().
    points_awarded = models.PositiveIntegerField(default=0)

    class Meta:
        unique_together = ("user", "match", "org")

    def __str__(self):
        return f"{self.user} → {self.selection} for {self.match}"


class LadderEntry(models.Model):
    """One team's position on the competition ladder.

    Keyed to (series, season) rather than to an organisation. A ladder is a fact
    about the competition — Fremantle are top of the AFL whichever league you
    tip in — so unlike Round and Match it is stored once and read by everyone.
    That also means the sync fetches it once per competition per season instead
    of once per league.

    DERIVED, not fetched. Rank, points and percentage are computed from our own
    completed Match rows by ``data_sync.ladder``, under per-code rules (4 points
    a win and percentage in the AFL; 2 points a win, 2 for a bye and
    for-and-against in the NRL).

    It used to be fetched, and the comment here used to argue that recomputing
    locally would be a second implementation for no gain. That was wrong twice
    over: it made standings the only thing in the app that needed a third-party
    API, and the NRL had no ladder at all because nobody served one we could
    read. The arithmetic is a dozen lines and the results are already ours.

    Rebuilt wholesale, so a corrected score fixes the table on the next pass
    with no incremental-update state to drift.
    """

    series = models.ForeignKey("catalog.Series", on_delete=models.CASCADE, related_name="ladder_entries")
    season = models.ForeignKey("catalog.Season", on_delete=models.CASCADE, related_name="ladder_entries")
    team = models.ForeignKey("tipping.Team", on_delete=models.CASCADE, related_name="ladder_entries")

    rank = models.PositiveIntegerField()
    played = models.PositiveIntegerField(default=0)
    wins = models.PositiveIntegerField(default=0)
    losses = models.PositiveIntegerField(default=0)
    draws = models.PositiveIntegerField(default=0)
    # Byes are inferred from absence (a settled round with no fixture for this
    # team), and are worth 2 competition points in the NRL and none in the AFL.
    # Stored rather than folded into `points` so the inference is visible: if a
    # table looks wrong, this is the first column to check.
    byes = models.PositiveIntegerField(default=0)
    points = models.IntegerField(default=0)
    # AFL percentage is points-for/against x100 and runs well over 100, so this
    # is not a 0-100 value.
    percentage = models.FloatField(default=0)
    points_for = models.IntegerField(default=0)
    points_against = models.IntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["series", "rank"]
        unique_together = ("series", "season", "team")
        verbose_name_plural = "ladder entries"

    def __str__(self):
        return f"{self.rank}. {self.team.name} ({self.series} {self.season})"

    @property
    def played_check(self) -> int:
        """Wins+losses+draws. Should equal `played` on a derived table."""
        return self.wins + self.losses + self.draws


class LadderAdjustment(models.Model):
    """A points adjustment the results cannot account for.

    Salary-cap penalties, forfeits and administrative deductions are decisions
    by the governing body. They change the ladder without changing any score,
    so a table derived purely from results will read high for a penalised club
    until one of these is entered.

    Rare — a handful of times a decade — which is exactly why it is a manual
    row rather than something to scrape. Entering it takes a minute; building a
    collector for a tribunal ruling does not end.
    """

    series = models.ForeignKey("catalog.Series", on_delete=models.CASCADE, related_name="ladder_adjustments")
    season = models.ForeignKey("catalog.Season", on_delete=models.CASCADE, related_name="ladder_adjustments")
    team = models.ForeignKey("tipping.Team", on_delete=models.CASCADE, related_name="ladder_adjustments")
    # Negative for a deduction, which is what these almost always are.
    points = models.IntegerField(help_text="Negative to deduct, e.g. -12 for a salary-cap penalty.")
    reason = models.CharField(max_length=200)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["series", "team"]

    def __str__(self):
        return f"{self.team.name} {self.points:+d} ({self.reason})"
