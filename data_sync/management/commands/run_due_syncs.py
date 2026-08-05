"""Run whichever syncs are due. One entry point for the server's scheduler.

The three sync cadences (live 2min, results 15min, fixtures hourly) used to mean
three crontab lines, each of which had to be installed by hand and any of which
could be missed. This is a single job the scheduler calls on a fixed short tick;
it reads SyncSchedule to decide what is actually due and runs only that.

Why one dispatcher rather than three timers:

* One unit to install and one to check. "Is the sync running?" has a single
  answer instead of three.
* The cadences live in the database, so changing how often results are pulled
  does not mean editing the server's scheduler config.
* Claiming is the same conditional UPDATE the rest of the system uses, so two
  overlapping ticks — a slow run still going when the next fires — cannot
  double-hit a feed.

Run it every 2 minutes; it costs one cheap query when nothing is due.
"""
from __future__ import annotations

from django.core.management import call_command
from django.core.management.base import BaseCommand

from data_sync.autosync import INTERVALS, claim


class Command(BaseCommand):
    help = "Run the feed syncs that are currently due (scheduler entry point)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--force", action="store_true",
            help="Run every kind now, ignoring what is due. For manual checks.",
        )

    def handle(self, *args, **opts):
        # Fixtures first: live and results ask about rounds that a fixtures run
        # may be about to create, so the other order wastes a whole tick.
        # Ladder last: it reflects results, so grade first then read standings.
        order = ["fixtures", "results", "live", "ladder"]
        ran = []
        for kind in order:
            if not opts["force"] and not claim(kind):
                continue
            call_command("sync_matches", **{kind: True}, verbosity=opts["verbosity"])
            ran.append(kind)

        if ran:
            self.stdout.write(self.style.SUCCESS(f"Ran: {', '.join(ran)}."))
        else:
            due = ", ".join(f"{k}/{v}s" for k, v in INTERVALS.items())
            self.stdout.write(f"Nothing due ({due}).")
