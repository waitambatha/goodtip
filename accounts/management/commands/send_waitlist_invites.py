"""Send the waiting-list launch invitation if its scheduled time has come.

Run by orgs' run_due_jobs on the jobs timer (every ten minutes), so a time the
super admin sets in the Waiting list screen is honoured to within one tick.
Nothing happens unless a campaign is scheduled and due; once a person has been
invited they are never picked again, so running this often is safe.
"""
from django.core.management.base import BaseCommand

from accounts import waitlist


class Command(BaseCommand):
    help = "Send the scheduled waiting-list invitation, if it is due."

    def handle(self, *args, **opts):
        campaign = waitlist.due_campaign()
        if campaign is None:
            self.stdout.write("No waiting-list invitation is due.")
            return
        sent = waitlist.send_campaign(campaign)
        self.stdout.write(self.style.SUCCESS(f"Waiting-list invitation: {sent} sent ({campaign.status})."))
