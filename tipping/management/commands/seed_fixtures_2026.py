"""Seed the real August 2026 fixtures (from the Fixtures Reference doc) into a league.

Rounds belong to an organisation, so this loads the AFL/AFLW/NRL/NRLW August
fixtures into one target org. Idempotent: matches keyed by external_id, rounds by
(org, round_number, series). Lockout for each round is its earliest kickoff.

    python manage.py seed_fixtures_2026 --org <id>
    python manage.py seed_fixtures_2026 --org-name "Office Footy Crew"
"""
from datetime import datetime
from zoneinfo import ZoneInfo

from django.core.management.base import BaseCommand, CommandError
from django.utils.text import slugify

from catalog.models import Competition, Series
from orgs.models import Organisation
from tipping.models import Match, Round, Team

from ._fixtures_2026 import NRL_ALIASES, ROUNDS

SYDNEY = ZoneInfo("Australia/Sydney")


def _resolve_team(series: Series, display_name: str) -> Team | None:
    """Find a seeded Team in this series. NRL/NRLW use the alias map; AFL/AFLW
    resolve by slugify() of the display name."""
    slug = NRL_ALIASES.get(display_name) or slugify(display_name)
    return Team.objects.filter(series=series, slug=slug).first()


class Command(BaseCommand):
    help = "Seed real August 2026 AFL/AFLW/NRL/NRLW fixtures into a league."

    def add_arguments(self, parser):
        parser.add_argument("--org", type=int, help="Target Organisation id.")
        parser.add_argument("--org-name", type=str, help="Target Organisation name.")

    def _get_org(self, options) -> Organisation:
        if options.get("org"):
            try:
                return Organisation.objects.get(pk=options["org"])
            except Organisation.DoesNotExist:
                raise CommandError(f"No organisation with id {options['org']}.")
        if options.get("org_name"):
            org = Organisation.objects.filter(name__iexact=options["org_name"]).first()
            if org is None:
                raise CommandError(f"No organisation named {options['org_name']!r}.")
            return org
        raise CommandError("Pass --org <id> or --org-name <name>.")

    def handle(self, *args, **options):
        org = self._get_org(options)
        series_by_name = {s.name: s for s in Series.objects.all()}

        rounds_made = matches_made = skipped = 0
        for series_name, number, stage, fixtures in ROUNDS:
            series = series_by_name.get(series_name)
            if series is None:
                self.stderr.write(self.style.WARNING(f"Series {series_name!r} missing — run migrations/seed_teams."))
                continue

            kickoffs = [
                datetime.strptime(dt, "%Y-%m-%d %H:%M").replace(tzinfo=SYDNEY)
                for dt, *_ in fixtures
            ]
            round_obj, created = Round.objects.update_or_create(
                org=org, round_number=number, series=series,
                defaults={
                    "stage": stage,
                    "lockout_at": min(kickoffs),
                    "competition": Competition.for_series(series, org.season),
                },
            )
            rounds_made += int(created)

            for (dt, home_name, away_name, venue), kickoff in zip(fixtures, kickoffs):
                home = _resolve_team(series, home_name)
                away = _resolve_team(series, away_name)
                if home is None or away is None:
                    skipped += 1
                    self.stderr.write(self.style.WARNING(
                        f"  Skip {series_name} R{number}: {home_name} v {away_name} (unresolved team)"
                    ))
                    continue
                external_id = f"ref2026-{series.slug}-r{number}-{home.slug}-{away.slug}"
                _, m_created = Match.objects.update_or_create(
                    external_id=external_id,
                    defaults={
                        "round": round_obj, "home_team": home, "away_team": away,
                        "kickoff_at": kickoff, "venue": venue,
                    },
                )
                matches_made += int(m_created)

        self.stdout.write(self.style.SUCCESS(
            f"Fixtures seeded into {org.name!r}: +{rounds_made} rounds, "
            f"+{matches_made} matches, {skipped} skipped."
        ))
