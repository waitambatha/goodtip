from django.db import models
from django.utils import timezone


class SyncRun(models.Model):
    """One recorded attempt at pulling data from a fixtures/results feed.

    Exists so "are the games up to date?" has an answer that isn't "read the
    logs". The live poller runs every few minutes on a schedule, and both the
    admin sync panel and the "as at" stamps on match cards read the most recent
    successful run from here.
    """

    KIND_FIXTURES = "fixtures"
    KIND_RESULTS = "results"
    KIND_LIVE = "live"
    KIND_LADDER = "ladder"
    KIND_BACKFILL = "backfill"
    KIND_CHOICES = [
        (KIND_FIXTURES, "Fixtures"),
        (KIND_RESULTS, "Results"),
        (KIND_LIVE, "Live scores"),
        (KIND_LADDER, "Ladder"),
        (KIND_BACKFILL, "Full-season backfill"),
    ]

    kind = models.CharField(max_length=10, choices=KIND_CHOICES)
    competition = models.CharField(max_length=20)
    org = models.ForeignKey(
        "orgs.Organisation", on_delete=models.CASCADE, related_name="sync_runs",
        null=True, blank=True,
    )
    round_number = models.IntegerField(null=True, blank=True)

    started_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True, blank=True)
    ok = models.BooleanField(default=False)
    # How many matches the run created or updated. 0 alongside ok=True is a
    # legitimate outcome — nothing had changed — which is why it's its own field
    # rather than being inferred from success.
    matches_touched = models.IntegerField(default=0)
    message = models.TextField(blank=True)

    class Meta:
        ordering = ["-started_at"]
        indexes = [
            models.Index(fields=["kind", "-started_at"]),
        ]

    def __str__(self):
        state = "ok" if self.ok else "failed"
        return f"{self.get_kind_display()} {self.competition} — {state} @ {self.started_at:%Y-%m-%d %H:%M}"

    @property
    def duration_ms(self) -> int | None:
        if not self.finished_at:
            return None
        return int((self.finished_at - self.started_at).total_seconds() * 1000)

    @property
    def state(self) -> str:
        """"running", "ok" or "failed" — THREE outcomes, not two.

        ``ok`` starts False and only becomes True when the run closes, so a run
        still in progress is indistinguishable from a failed one if you read
        that field alone. The sync panel did exactly that and reported healthy
        in-flight syncs as failures, which is the worst kind of monitoring: it
        cries wolf on the normal case and you stop believing it on the real one.

        ``finished_at`` is what separates them, because the recorder sets it in
        a finally-style exit whether or not the sync raised.
        """
        if self.finished_at is None:
            return "running"
        return "ok" if self.ok else "failed"

    @property
    def is_stuck(self) -> bool:
        """Open far longer than any sync legitimately takes.

        A process killed mid-run — a deploy, an OOM, a systemd timeout — leaves
        its row open forever, and "running" would then be a lie that never
        resolves. Anything past an hour is wreckage, not work: the slowest kind
        is the full-season backfill and it has its own two-hour ceiling.
        """
        if self.finished_at is not None:
            return False
        return (timezone.now() - self.started_at).total_seconds() > 3600

    @classmethod
    def last_success(cls, kind: str | None = None, competition: str | None = None):
        qs = cls.objects.filter(ok=True)
        if kind:
            qs = qs.filter(kind=kind)
        if competition:
            qs = qs.filter(competition=competition)
        return qs.first()

    @classmethod
    def record(cls, *, kind, competition, org=None, round_number=None):
        """Context manager that opens a run row and closes it either way.

            with SyncRun.record(kind="live", competition="AFL") as run:
                run.matches_touched = svc.sync_live(...)
        """
        return _RunRecorder(cls, kind=kind, competition=competition, org=org, round_number=round_number)


class _RunRecorder:
    def __init__(self, model, **kwargs):
        self.model = model
        self.kwargs = kwargs
        self.run = None

    def __enter__(self):
        self.run = self.model.objects.create(**self.kwargs)
        return self.run

    def __exit__(self, exc_type, exc, tb):
        self.run.finished_at = timezone.now()
        if exc is None:
            self.run.ok = True
        else:
            self.run.ok = False
            self.run.message = f"{exc_type.__name__}: {exc}"[:2000]
        self.run.save(update_fields=["finished_at", "ok", "matches_touched", "message"])
        # Propagate the exception — the caller decides whether a feed outage is
        # fatal. The failure is on record either way.
        return False


class SyncSchedule(models.Model):
    """One row per sync kind, holding when that kind is next due.

    The scheduling state has to live somewhere every web worker can see, and it
    has to be updatable atomically — otherwise two workers both read "due",
    both start a sync, and the feed gets hit twice for the same round. A single
    conditional UPDATE against this row is the lock (see data_sync.autosync).

    Cron and the in-app autosync both claim through here, so running both is
    safe: whichever arrives first moves next_run_at forward and the other finds
    nothing due.
    """

    kind = models.CharField(max_length=10, unique=True, choices=SyncRun.KIND_CHOICES)
    # Due immediately by default, so a fresh deploy syncs on its first request
    # rather than waiting out a full interval with an empty dashboard.
    next_run_at = models.DateTimeField(default=timezone.now)
    last_started_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["kind"]

    def __str__(self):
        return f"{self.kind} due {self.next_run_at:%Y-%m-%d %H:%M}"
