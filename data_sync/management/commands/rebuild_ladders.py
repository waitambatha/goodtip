"""Rebuild competition ladders from stored results.

The ladder is derived, not fetched, so this needs no network and no key — it is
arithmetic over ``matchreader.HistoricalMatch``. Safe to run any time; it
replaces each table wholesale, so a corrected score is picked up on the next
pass with nothing to reconcile.

``sync_matches --ladder`` reaches the same code through each organisation. This
exists because a ladder belongs to a (series, season) and not to an org, so
requiring one just to rebuild a table is a detour — and a season with no
leagues on it yet could not be rebuilt at all.

    manage.py rebuild_ladders                    # every series, every season
    manage.py rebuild_ladders --season 2026
    manage.py rebuild_ladders --series NRL --season 2026
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from catalog.models import Season, Series
from data_sync.ladder import NO_LADDER, RULES, rebuild_ladder


class Command(BaseCommand):
    help = "Recompute competition ladders from stored match results."

    def add_arguments(self, parser):
        parser.add_argument("--series", help="AFL, AFLW, NRL or NRLW. Default: all.")
        parser.add_argument("--season", type=int, help="Season year. Default: all.")

    def handle(self, *args, **o):
        series_qs = Series.objects.all()
        if o["series"]:
            series_qs = series_qs.filter(name__iexact=o["series"])
            if not series_qs.exists():
                self.stderr.write(self.style.ERROR(f"No series named {o['series']!r}."))
                return

        season_qs = Season.objects.all()
        if o["season"]:
            season_qs = season_qs.filter(year=o["season"])
            if not season_qs.exists():
                self.stderr.write(self.style.ERROR(f"No season for {o['season']}."))
                return

        total = 0
        for series in series_qs:
            name = (series.name or "").upper()
            if name in NO_LADDER or name not in RULES:
                # Origin has no table, and a series with no rules is one we do
                # not know how to score. Both are skipped quietly; saying so on
                # every run would bury the lines that matter.
                continue
            for season in season_qs:
                written = rebuild_ladder(series=series, season=season)
                if written:
                    self.stdout.write(f"  {series.name} {season.year}: {written} teams")
                    total += written

        self.stdout.write(self.style.SUCCESS(f"{total} ladder rows rebuilt."))
