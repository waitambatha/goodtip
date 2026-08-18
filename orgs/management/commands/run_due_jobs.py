"""Run the scheduled application jobs — the ones that send email.

Companion to data_sync's run_due_syncs. That one keeps scores current; this one
keeps people informed, and until now nothing ran it. `open_due_elections` had a
lazy in-app fallback so it limped along on page views, but the three below had
neither a timer nor a fallback: they only ever ran if somebody typed the command
by hand. In practice that meant election reminders, round scorecards and recaps
were never sent at all, with no error anywhere to show for it — the code was
correct and simply never called.

Every job here is idempotent by its own stamp (results_email_sent_at,
reminder_day_sent_at, and so on), so running this often does not mean sending
anything twice. That is what makes a short interval safe, and a short interval
is what makes a "one hour before voting closes" reminder land within the hour.
"""
from __future__ import annotations

from django.core.management import call_command
from django.core.management.base import BaseCommand

# (command, human label). Order matters: elections open before anyone can be
# reminded about them, and results are graded before scorecards go out.
JOBS = [
    ("open_due_elections", "open/close elections whose time has come"),
    ("send_election_reminders", "vote reminders (a day out, an hour out)"),
    ("send_result_emails", "round scorecards and election outcomes"),
    ("generate_recaps", "AI round recaps"),
    # Housekeeping. Both are self-throttling no-ops on almost every tick, which
    # is why they can sit in a ten-minute loop rather than needing timers of
    # their own — a new unit is a thing that has to be installed with sudo, and
    # the one job that DID need its own timer is still not installed weeks
    # later. Adding work to a loop that already runs is the cheaper promise.
    ("retrain_matchreader", "refit MatchReader where the model is missing or stale"),
    ("prune_sync_runs", "trim the sync history"),
]


class Command(BaseCommand):
    help = "Run the scheduled email/notification jobs that are due."

    def add_arguments(self, parser):
        parser.add_argument(
            "--only", help="Run a single job by command name, for debugging.",
        )

    def handle(self, *args, **opts):
        ran, failed = [], []
        for name, label in JOBS:
            if opts["only"] and opts["only"] != name:
                continue
            try:
                call_command(name, verbosity=opts["verbosity"])
                ran.append(name)
            except Exception as e:  # noqa: BLE001
                # One broken job must not stop the rest. A missing API key
                # should not cost members their round results.
                failed.append(name)
                self.stderr.write(self.style.WARNING(f"  {name} ({label}) failed: {e}"))

        if ran:
            self.stdout.write(self.style.SUCCESS(f"Ran: {', '.join(ran)}."))
        if failed:
            self.stderr.write(self.style.WARNING(f"Failed: {', '.join(failed)}."))
        if not ran and not failed:
            self.stdout.write("Nothing to run.")
