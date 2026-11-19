from django.core.management.base import BaseCommand

from tipping.certificate_mail import auto_send_due


class Command(BaseCommand):
    help = "Email season certificates to their winners once a season is final (PDF and PNG)."

    def handle(self, *args, **opts):
        n = auto_send_due(log=self.stdout.write if opts["verbosity"] > 1 else None)
        self.stdout.write(f"{n} certificate email{'s' if n != 1 else ''} sent.")
