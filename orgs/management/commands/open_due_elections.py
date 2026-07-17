from django.core.management.base import BaseCommand

from orgs.services import open_due_elections


class Command(BaseCommand):
    help = "Open any scheduled charity elections whose start time has passed."

    def handle(self, *args, **options):
        n = open_due_elections()
        self.stdout.write(self.style.SUCCESS(f"Opened {n} election(s)."))
