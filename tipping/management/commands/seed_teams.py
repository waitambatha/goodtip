from django.core.management.base import BaseCommand
from django.utils.text import slugify

from catalog.models import Series
from tipping.models import Team


AFL_TEAMS = [
    ("Adelaide Crows", "adelaide-crows"),
    ("Brisbane Lions", "brisbane-lions"),
    ("Carlton", "carlton"),
    ("Collingwood", "collingwood"),
    ("Essendon", "essendon"),
    ("Fremantle", "fremantle"),
    ("Geelong Cats", "geelong-cats"),
    ("Gold Coast SUNS", "gold-coast-suns"),
    ("GWS GIANTS", "gws-giants"),
    ("Hawthorn", "hawthorn"),
    ("Melbourne", "melbourne"),
    ("North Melbourne", "north-melbourne"),
    ("Port Adelaide", "port-adelaide"),
    ("Richmond", "richmond"),
    ("St Kilda", "st-kilda"),
    ("Sydney Swans", "sydney-swans"),
    ("West Coast Eagles", "west-coast-eagles"),
    ("Western Bulldogs", "western-bulldogs"),
]

NRL_TEAMS = [
    ("Brisbane Broncos", "brisbane-broncos"),
    ("Canberra Raiders", "canberra-raiders"),
    ("Canterbury-Bankstown Bulldogs", "bulldogs"),
    ("Cronulla-Sutherland Sharks", "sharks"),
    ("Dolphins", "dolphins"),
    ("Gold Coast Titans", "titans"),
    ("Manly Warringah Sea Eagles", "sea-eagles"),
    ("Melbourne Storm", "storm"),
    ("Newcastle Knights", "knights"),
    ("New Zealand Warriors", "warriors"),
    ("North Queensland Cowboys", "cowboys"),
    ("Parramatta Eels", "eels"),
    ("Penrith Panthers", "panthers"),
    ("South Sydney Rabbitohs", "rabbitohs"),
    ("St. George Illawarra Dragons", "dragons"),
    ("Sydney Roosters", "roosters"),
    ("Wests Tigers", "wests-tigers"),
]

NRLW_EXCLUDED_SLUGS = {"dolphins", "sea-eagles", "storm", "panthers", "rabbitohs"}


class Command(BaseCommand):
    help = "Seed AFL/AFLW/NRL/NRLW teams. Idempotent."

    def handle(self, *args, **options):
        comp = {c.name: c for c in Series.objects.filter(name__in=("AFL", "AFLW", "NRL", "NRLW"))}
        missing = {"AFL", "AFLW", "NRL", "NRLW"} - set(comp)
        if missing:
            self.stderr.write(self.style.ERROR(
                f"Missing series {sorted(missing)}. Run migrations first."
            ))
            return

        created = 0
        for name, slug in AFL_TEAMS:
            _, was_created = Team.objects.update_or_create(
                series=comp["AFL"], slug=slug, defaults={"name": name},
            )
            created += int(was_created)
            _, was_created = Team.objects.update_or_create(
                series=comp["AFLW"], slug=slug, defaults={"name": name},
            )
            created += int(was_created)
        for name, slug in NRL_TEAMS:
            _, was_created = Team.objects.update_or_create(
                series=comp["NRL"], slug=slug, defaults={"name": name},
            )
            created += int(was_created)
            if slug not in NRLW_EXCLUDED_SLUGS:
                _, was_created = Team.objects.update_or_create(
                    series=comp["NRLW"], slug=slug, defaults={"name": name},
                )
                created += int(was_created)
        totals = {c: Team.objects.filter(series=comp[c]).count() for c in ("AFL", "AFLW", "NRL", "NRLW")}
        self.stdout.write(self.style.SUCCESS(
            f"Seed complete. New: {created}. Totals → AFL={totals['AFL']} AFLW={totals['AFLW']} NRL={totals['NRL']} NRLW={totals['NRLW']}"
        ))
