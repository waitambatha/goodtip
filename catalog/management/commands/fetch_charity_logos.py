"""Backfill charity logos from each charity's own site.

Run after seeding charities, and whenever someone suggests a new one. Kept as
a command rather than a signal because it makes network calls: a charity added
during signup must not make the person who added it wait on someone else's web
server.
"""
from django.core.management.base import BaseCommand

from catalog.logos import backfill_charity
from catalog.models import Charity


class Command(BaseCommand):
    help = "Fetch a logo (and a website, where missing) for each charity."

    def add_arguments(self, parser):
        parser.add_argument(
            "--force", action="store_true",
            help="Re-fetch even for charities that already have a logo.",
        )
        parser.add_argument(
            "--slug", action="append", default=[],
            help="Only this charity (repeatable).",
        )

    def handle(self, *args, **options):
        qs = Charity.objects.all()
        if options["slug"]:
            qs = qs.filter(slug__in=options["slug"])
        if not options["force"]:
            qs = qs.filter(logo="")

        done = 0
        for charity in qs:
            if backfill_charity(charity, force=options["force"]):
                done += 1
                got = "logo" if charity.logo else "website only"
                self.stdout.write(self.style.SUCCESS(f"  {charity.name} — {got}"))
            else:
                self.stdout.write(f"  {charity.name} — nothing found")
        self.stdout.write(self.style.SUCCESS(f"Updated {done} charities."))
