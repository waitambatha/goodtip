"""Bring GoodTip's fixture library current — see data_sync.library.

    manage.py refresh_library            the window around today, every competition
    manage.py refresh_library --full     every round of this season and last
    manage.py refresh_library --auto     the window, or a full sweep when the last
                                         one is more than six hours old (the
                                         scheduler's choice: run_due_syncs)
    manage.py refresh_library --only "NBA" --only "La Liga"
"""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone


class Command(BaseCommand):
    help = "Refresh GoodTip's own fixture library for every competition with a feed."

    def add_arguments(self, parser):
        parser.add_argument("--full", action="store_true")
        parser.add_argument("--auto", action="store_true")
        parser.add_argument("--only", action="append")

    def handle(self, *args, **opts):
        from data_sync.library import refresh
        from data_sync.models import SyncRun

        full = opts["full"]
        if opts["auto"]:
            last_full = (SyncRun.objects.filter(kind=SyncRun.KIND_LIBRARY, ok=True, message="full sweep")
                         .order_by("-started_at").first())
            full = last_full is None or timezone.now() - last_full.started_at > timedelta(hours=6)
        self.stdout.write(f"Library: {'full sweep' if full else 'window'}")
        result = refresh(full=full, only=opts["only"], log=self.stdout.write)
        if full:
            # Marks the sweep so --auto knows when the next is due.
            SyncRun.objects.filter(kind=SyncRun.KIND_LIBRARY, ok=True,
                                   started_at__gte=timezone.now() - timedelta(hours=3),
                                   competition__in=[k for k, v in result.items() if v is not None]
                                   ).update(message="full sweep")
        bad = [k for k, v in result.items() if v is None]
        msg = f"Library refreshed: {len(result) - len(bad)} competitions"
        if bad:
            self.stderr.write(self.style.WARNING(f"{msg}; failed: {', '.join(bad)}"))
        else:
            self.stdout.write(self.style.SUCCESS(msg))
