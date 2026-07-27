"""Generate due AI Group Recaps and post them to each group's Wall.

Run after results land (same cron neighbourhood as open_due_elections):

    */30 * * * *  cd /home/mbatha-goodtip/projects/goodtip && venv/bin/python manage.py generate_recaps

One recap per (org, round), only once every result in the round is final —
a round with a Monday fixture simply lands later in the week (spec §10).
"""

from django.core.management.base import BaseCommand

from orgs.models import Organisation
from orgs.recaps import generate_recaps


class Command(BaseCommand):
    help = "Generate AI Group Recaps for fully-resolved rounds and post them to the Wall."

    def add_arguments(self, parser):
        parser.add_argument("--org", type=int, help="Only this organisation id.")
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Generate and print the recap text without posting anything.",
        )

    def handle(self, *args, **options):
        org = None
        if options["org"]:
            org = Organisation.objects.get(pk=options["org"])
        results = generate_recaps(org=org, dry_run=options["dry_run"])
        if not results:
            self.stdout.write("No recaps due — nothing fully resolved and untipped rounds stay silent.")
            return
        for item, text in results:
            if options["dry_run"]:
                label = f"{item['group']['name']} R{item['round']['number']}"
            else:
                label = f"{item.org.name} R{item.round.round_number}" + (" [fallback]" if item.fallback_used else "")
            self.stdout.write(f"— {label}\n  {text}")
        verb = "Previewed" if options["dry_run"] else "Posted"
        self.stdout.write(self.style.SUCCESS(f"{verb} {len(results)} recap(s)."))
