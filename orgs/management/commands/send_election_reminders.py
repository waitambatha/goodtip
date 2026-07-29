"""Nudge members who haven't voted, a day out and an hour out.

Run on a schedule (every 10 minutes is plenty):

    */10 * * * * cd $APP && venv/bin/python manage.py send_election_reminders

Both reminders are stamped on the vote once sent, so running this often does
not mean sending it often. Only members with no ballot get mailed.
"""
from __future__ import annotations

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from orgs.models import CharityVote
from orgs.notifications import send_election_reminders

# How close to the deadline each reminder fires. The windows are one-sided —
# "we are now inside a day of closing" — and the sent-stamp stops repeats.
DAY_OUT = timedelta(days=1)
HOUR_OUT = timedelta(hours=1)


class Command(BaseCommand):
    help = "Email vote reminders to members who haven't voted yet (1 day out, 1 hour out)."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true",
                            help="Report what would be sent without sending it.")

    def handle(self, *args, **opts):
        now = timezone.now()
        dry = opts["dry_run"]
        total = 0

        open_votes = (
            CharityVote.objects
            .filter(status=CharityVote.STATUS_OPEN, scheduled_close_at__isnull=False)
            .select_related("org")
        )

        for vote in open_votes:
            closes = vote.scheduled_close_at
            if closes <= now:
                continue  # closing is close_due_elections' job, not ours

            remaining = closes - now

            # Hour reminder takes precedence: if a vote is opened only 30
            # minutes before closing, the useful message is "last call", not
            # "closes tomorrow".
            if remaining <= HOUR_OUT and vote.reminder_hour_sent_at is None:
                urgency = "hour"
            elif remaining <= DAY_OUT and vote.reminder_day_sent_at is None:
                urgency = "day"
            else:
                continue

            if dry:
                self.stdout.write(
                    f"[dry-run] {urgency} reminder for {vote.org.name} "
                    f"(closes in {remaining})"
                )
                continue

            sent = send_election_reminders(vote, urgency=urgency)
            total += sent
            self.stdout.write(f"{vote.org.name}: {sent} {urgency} reminder(s)")

        if not dry:
            self.stdout.write(self.style.SUCCESS(f"Sent {total} reminder(s)."))
