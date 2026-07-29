"""Email results once they're settled: round scorecards and election outcomes.

Run after the results sync has had a chance to grade things:

    0 * * * * cd $APP && venv/bin/python manage.py send_result_emails

A round only qualifies once every fixture in it has a result, so nobody gets a
scorecard with half the games still blank. Both kinds are stamped when sent, so
running hourly does not mean mailing hourly.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db.models import Count, Q

from orgs.models import CharityVote
from orgs.notifications import send_election_result, send_round_results
from tipping.models import Round


class Command(BaseCommand):
    help = "Email round result scorecards and closed-election outcomes."

    def add_arguments(self, parser):
        parser.add_argument("--rounds-only", action="store_true")
        parser.add_argument("--elections-only", action="store_true")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **opts):
        dry = opts["dry_run"]
        do_rounds = not opts["elections_only"]
        do_elections = not opts["rounds_only"]
        total = 0

        if do_rounds:
            # Fully graded means: at least one fixture, and none still without
            # a result. Emailing a partial round would be worse than waiting.
            candidates = (
                Round.objects
                .filter(results_email_sent_at__isnull=True)
                .annotate(
                    n_matches=Count("matches", distinct=True),
                    n_ungraded=Count("matches", filter=Q(matches__result__isnull=True), distinct=True),
                )
                .filter(n_matches__gt=0, n_ungraded=0)
                .select_related("org")
            )
            for rnd in candidates:
                if dry:
                    self.stdout.write(f"[dry-run] round results: {rnd.org.name} R{rnd.round_number}")
                    continue
                sent = send_round_results(rnd)
                total += sent
                self.stdout.write(f"{rnd.org.name} R{rnd.round_number}: {sent} scorecard(s)")

        if do_elections:
            closed = (
                CharityVote.objects
                .filter(
                    status=CharityVote.STATUS_CLOSED,
                    result_email_sent_at__isnull=True,
                    winning_charity__isnull=False,
                )
                .select_related("org", "winning_charity")
            )
            for vote in closed:
                if dry:
                    self.stdout.write(f"[dry-run] election result: {vote.org.name} → {vote.winning_charity.name}")
                    continue
                sent = send_election_result(vote)
                total += sent
                self.stdout.write(f"{vote.org.name}: {sent} result email(s)")

        if not dry:
            self.stdout.write(self.style.SUCCESS(f"Sent {total} email(s)."))
