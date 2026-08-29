"""Fetch the logos that are still missing.

catalog.logos already knows how to find one — resolve the charity's site, try
its apple-touch-icon, then its og:image, then its favicon, store one PNG and
stamp `logo_fetched_at` so it is never fetched twice. What was missing is
anything that runs it over charities that ALREADY EXIST.

The only caller was the signup wizard, firing once per newly suggested
charity. A charity whose site was down that afternoon, or that was seeded
before the fetcher was written, keeps its initials tile forever with nothing
to try again.

    python manage.py backfill_charity_logos
    python manage.py backfill_charity_logos --retry      # failures too
    python manage.py backfill_charity_logos --force      # everything, again
    python manage.py backfill_charity_logos --dry-run

Deliberately serial with a pause between charities: this reaches out to other
people's web servers, and a burst of parallel requests from one host is how a
well-meaning backfill gets an IP blocked.
"""
from __future__ import annotations

import time

from django.core.management.base import BaseCommand

from catalog.logos import backfill_charity
from catalog.models import Charity


class Command(BaseCommand):
    help = "Fetch logos for charities that do not have one yet."

    def add_arguments(self, parser):
        parser.add_argument(
            "--retry", action="store_true",
            help="Include charities a previous run already tried and failed.",
        )
        parser.add_argument(
            "--force", action="store_true",
            help="Refetch every charity, including ones that have a logo.",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="List what would be fetched and change nothing.",
        )
        parser.add_argument(
            "--pause", type=float, default=1.5,
            help="Seconds between charities (default 1.5).",
        )

    def handle(self, *args, **opts):
        charities = Charity.objects.order_by("name")
        if opts["force"]:
            targets = list(charities)
        else:
            targets = [c for c in charities if not c.logo]
            if not opts["retry"]:
                # An untried charity is the common case and the polite one to
                # start with; a previous failure is usually a site that will
                # fail again, so it takes an explicit --retry.
                targets = [c for c in targets if c.logo_fetched_at is None]

        if not targets:
            self.stdout.write(self.style.SUCCESS("Nothing to fetch — every charity has a logo."))
            return

        self.stdout.write(f"{len(targets)} charit{'y' if len(targets) == 1 else 'ies'} to try.\n")

        got = failed = 0
        for i, charity in enumerate(targets):
            label = f"{charity.name} ({charity.website or 'no website on file'})"
            if opts["dry_run"]:
                self.stdout.write(f"  would try  {label}")
                continue

            try:
                backfill_charity(charity, force=opts["force"])
            except Exception as e:                      # noqa: BLE001
                # One unreachable site must not end the run — the next charity
                # is unrelated to it.
                failed += 1
                self.stdout.write(self.style.WARNING(f"  error      {label}: {e}"))
            else:
                charity.refresh_from_db(fields=["logo"])
                if charity.logo:
                    got += 1
                    self.stdout.write(self.style.SUCCESS(f"  got        {charity.name}"))
                else:
                    failed += 1
                    self.stdout.write(f"  no logo    {label}")

            if opts["pause"] and i < len(targets) - 1:
                time.sleep(opts["pause"])

        if opts["dry_run"]:
            return
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(f"{got} fetched") + f", {failed} still without one.")
