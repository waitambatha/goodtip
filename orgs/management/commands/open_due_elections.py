from django.core.management.base import BaseCommand

from orgs.services import close_due_elections, open_due_elections


class Command(BaseCommand):
    help = "Open scheduled charity elections whose start time has passed, and close open ones whose end time has passed."

    def handle(self, *args, **options):
        opened = open_due_elections()
        closed = close_due_elections()
        self.stdout.write(self.style.SUCCESS(f"Opened {opened} election(s), closed {closed}."))
