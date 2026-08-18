"""Delete SyncRun rows older than the window anybody looks at.

Every sync attempt writes a row, and nothing ever removed one. Two weeks after
the table was introduced it held 146,879 rows and was growing by roughly ten
thousand a day — a few million a year, on a database ~370ms away that the admin
panel queries on every page load.

The rows earn their keep for a few days: "are the games up to date", "what
failed last night", "did the sweep run". Nobody has ever needed the sync
history of a Tuesday in March, and keeping it makes the questions people DO ask
slower to answer.

WHAT IS KEPT REGARDLESS
The most recent successful run of each kind, whatever its age. Those are what
the sync panel's freshness stamps read, and a quiet feed — State of Origin
between series, say — can legitimately have no successful run inside the
window. Pruning it would make a working feed report "never", which is exactly
the signal that panel exists to give honestly.
"""

from __future__ import annotations

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from data_sync.models import SyncRun


class Command(BaseCommand):
    help = "Delete old SyncRun rows, keeping a recent window and the last success per kind."

    def add_arguments(self, parser):
        parser.add_argument(
            "--days", type=int, default=14,
            help="Keep runs newer than this many days. Default 14.",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report what would go without deleting it.",
        )

    def handle(self, *args, **opts):
        cutoff = timezone.now() - timedelta(days=opts["days"])

        keep_ids = [
            row.id for row in (
                SyncRun.objects.filter(ok=True)
                .order_by("kind", "-started_at")
                .distinct("kind")
            )
        ]

        stale = SyncRun.objects.filter(started_at__lt=cutoff).exclude(id__in=keep_ids)
        count = stale.count()

        if opts["dry_run"]:
            self.stdout.write(
                f"Would delete {count} run(s) older than {opts['days']} days, "
                f"keeping {len(keep_ids)} last-success row(s)."
            )
            return

        # Chunked. A single DELETE of a few hundred thousand rows against a
        # remote database holds a long transaction and can time out partway,
        # which leaves the job looking hung and achieving nothing.
        deleted = 0
        while True:
            batch = list(stale.values_list("id", flat=True)[:5000])
            if not batch:
                break
            deleted += SyncRun.objects.filter(id__in=batch).delete()[0]

        self.stdout.write(self.style.SUCCESS(
            f"Deleted {deleted} run(s) older than {opts['days']} days. "
            f"{SyncRun.objects.count()} remain."
        ))
