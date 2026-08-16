"""Pull past seasons of real results into HistoricalMatch, for training.

The model cannot be fitted on what the app already holds: those rows are
org-scoped, cover a handful of recent rounds, and count a fixture once per
league that tipped it. This walks the scrapers back through whole seasons and
stores each real game once.

Idempotent on the NATURAL key — these two clubs, that round, that season — so
re-running to top up the current season updates rather than duplicates, and so
does a re-run against a different source. Keying on the external id alone was
not enough: Sportradar and afl.com.au id the same fixture differently, so
backfilling AFL 2024 from both stored all 207 games twice and MatchReader was
fitted on the doubled history.

Rounds are stored with a stage, so the ladder can exclude finals.
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

        # One source per code, and no key anywhere. Sportradar used to be tried
        # first here because it returned a whole season per request where the
        # scrapers cost one page per round — but mixing the two is what split
        # AFL 2024 into 422 rows for a 207-game season, since the two id
        # schemes never collided and so never matched.
        if name in ("AFL", "AFLW"):
            from data_sync.scrapers.afl import AflApiScraper
            scraper, resolve, key = AflApiScraper(), _resolve_team, name
            self.stdout.write("  source: afl.com.au")
        elif name in ("NRL", "NRLW"):
            from data_sync.scrapers.nrl import NrlDrawScraper
            scraper, resolve, key = NrlDrawScraper(), _resolve_nrl_team, name.upper()
            self.stdout.write("  source: nrl.com")
        else:
            self.stderr.write(self.style.ERROR(f"No source for {name!r}."))
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
                    # Keyed on the NATURAL key, not the external id. Keying on
                    # the id is what let AFL 2024 be stored twice — once under
                    # Sportradar ids, once under afl.com.au ids — because the
                    # two schemes never collide and so never matched. These two
                    # clubs in this round of this season are one game whatever
                    # the source calls it.
                    with transaction.atomic():
                        _, was_new = HistoricalMatch.objects.update_or_create(
                            series=series, season=season, round_number=rn,
                            home_team=home, away_team=away,
                            defaults={
                                "external_id": g["external_id"],
                                "kickoff_at": g["kickoff_at"],
                                "home_score": g["home_score"],
                                "away_score": g["away_score"],
                            },
                        )
                    created += was_new
                    updated += not was_new
                    season_rows += 1
            self._stage_season(series, season)
            self.stdout.write(f"  {name} {season}: {season_rows} completed games")

        total = HistoricalMatch.objects.filter(series=series).count()

        self.stdout.write(self.style.SUCCESS(
            f"{name}: {created} new, {updated} updated, {skipped} unresolved. "
            f"History now holds {total} games."
        ))

    def _stage_season(self, series, season):
        """Mark this season's finals rounds, so the ladder can exclude them.

        Done per season as it is fetched rather than in one pass at the end,
        because a run that dies partway should still leave the seasons it did
        finish correctly staged.

        The rule itself lives in ``matchreader.stages`` so the scheduled
        results sync applies exactly the same one — staging that only happened
        inside this manual command is how finals ended up counting towards the
        ladder between backfills.
        """
        from matchreader.stages import restage_season

        restage_season(series, season)
