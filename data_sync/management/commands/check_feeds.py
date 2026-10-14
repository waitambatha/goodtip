"""Compare the feeds with every league's fixtures, fix what it can, alert on the rest.

Runs hourly from run_due_syncs (rounds around today) and over the whole season
after each backfill. Safe to run by hand at any time:

    manage.py check_feeds              # around today, fix, alert
    manage.py check_feeds --season     # every round the feeds publish
    manage.py check_feeds --dry-run    # report only: no fixing, no email
"""
from django.core.management.base import BaseCommand

from data_sync.feedcheck import run_check


class Command(BaseCommand):
    help = "Check fixtures and results against the feeds, fix gaps, alert on what is left."

    def add_arguments(self, parser):
        parser.add_argument("--season", action="store_true",
                            help="Check every round the feeds publish, not just around today.")
        parser.add_argument("--dry-run", action="store_true",
                            help="Report only — do not re-sync anything or send email.")

    def handle(self, *args, **opts):
        dry = opts["dry_run"]
        check = run_check(full_season=opts["season"], heal=not dry, alert=not dry)
        found = len(check.found)
        self.stdout.write(
            f"Checked {check.games_checked} feed game(s): {found} issue(s) found, "
            f"{check.healed} round(s) re-synced, {len(check.unresolved_errors)} error(s) "
            f"and {len(check.unresolved_warnings)} warning(s) left."
        )
        for issue in check.unresolved:
            where = " ".join(str(x) for x in (issue["series"], issue["year"],
                             f"R{issue['round']}" if issue["round"] else "") if x)
            self.stdout.write(f"  [{issue['severity']}] {issue['label']} {where} "
                              f"({len(issue['orgs'])} league(s)) {issue['detail']}")
        if check.error:
            self.stderr.write(self.style.ERROR(check.error))
        elif check.ok:
            self.stdout.write(self.style.SUCCESS("All in step with the feeds."))
