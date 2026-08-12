"""Pull past seasons of real results into HistoricalMatch, for training.

The model cannot be fitted on what the app already holds: those rows are
org-scoped, cover a handful of recent rounds, and count a fixture once per
league that tipped it. This walks the scrapers back through whole seasons and
stores each real game once.

Idempotent on (series, external_id), so re-running to top up the current
season updates rather than duplicates.
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from catalog.models import Series
from matchreader.models import HistoricalMatch


class Command(BaseCommand):
    help = "Backfill historical match results used to train MatchReader."

    def add_arguments(self, parser):
        parser.add_argument("--series", default="AFL", help="AFL, AFLW, NRL or NRLW.")
        parser.add_argument("--from-season", type=int, required=True)
        parser.add_argument("--to-season", type=int, required=True)
        parser.add_argument("--max-round", type=int, default=27)

    def handle(self, *args, **o):
        from data_sync.services import _resolve_team, _resolve_nrl_team

        name = o["series"]
        series = Series.objects.filter(name=name).first()
        if series is None:
            self.stderr.write(self.style.ERROR(f"No series named {name!r}."))
            return

        if name in ("AFL", "AFLW"):
            from data_sync.scrapers.afl import AflApiScraper
            scraper, resolve, key = AflApiScraper(), _resolve_team, name
        elif name in ("NRL", "NRLW"):
            from data_sync.scrapers.nrl import NrlDrawScraper
            scraper, resolve, key = NrlDrawScraper(), _resolve_nrl_team, name.upper()
        else:
            self.stderr.write(self.style.ERROR(f"No scraper for {name!r}."))
            return

        created = updated = skipped = 0
        for season in range(o["from_season"], o["to_season"] + 1):
            season_rows = 0
            for rn in range(1, o["max_round"] + 1):
                try:
                    games = scraper.fixtures(series=key, season=season, round_number=rn)
                except Exception as e:                       # noqa: BLE001
                    self.stderr.write(f"  {season} R{rn}: {e}")
                    continue
                for g in games:
                    # Only finished games teach anything.
                    if g["status"] != "complete" or g["home_score"] is None:
                        continue
                    home = resolve(series, g["home_name"], g["home_external_id"])
                    away = resolve(series, g["away_name"], g["away_external_id"])
                    if not home or not away:
                        skipped += 1
                        continue
                    with transaction.atomic():
                        _, was_new = HistoricalMatch.objects.update_or_create(
                            series=series, external_id=g["external_id"],
                            defaults={
                                "season": season, "round_number": rn,
                                "home_team": home, "away_team": away,
                                "kickoff_at": g["kickoff_at"],
                                "home_score": g["home_score"],
                                "away_score": g["away_score"],
                            },
                        )
                    created += was_new
                    updated += not was_new
                    season_rows += 1
            self.stdout.write(f"  {name} {season}: {season_rows} completed games")

        total = HistoricalMatch.objects.filter(series=series).count()
        self.stdout.write(self.style.SUCCESS(
            f"{name}: {created} new, {updated} updated, {skipped} unresolved. "
            f"History now holds {total} games."
        ))
