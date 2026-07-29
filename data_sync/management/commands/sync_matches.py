"""Keep fixtures, live scores and results in step with the upstream feeds.

Designed to be run on a schedule rather than from a request, because a feed
round-trip is far too slow to sit inside a page load. Three cadences, all from
this one command:

    # in-play scores and the clock — every 2 minutes
    */2 * * * *  cd /srv/goodtip && venv/bin/python manage.py sync_matches --live

    # final scores, which is what grades tips — every 15 minutes
    */15 * * * * cd /srv/goodtip && venv/bin/python manage.py sync_matches --results

    # the draw itself, which barely moves — nightly
    30 4 * * *   cd /srv/goodtip && venv/bin/python manage.py sync_matches --fixtures --all-rounds

With no flags it does --live --results, which is the useful default for a
single frequent job.

Every attempt is recorded as a data_sync.SyncRun, so "how fresh is this?" has
an answer, and a feed outage shows up as a failed run rather than silence.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from data_sync.models import SyncRun
from data_sync.services import SyncError, get_sync_service
from orgs.models import Organisation
from tipping.models import Match, Round

logger = logging.getLogger("data_sync")

# How far either side of "now" a round has to be for the live poller to care.
# Outside this window there is nothing in play, so there is no point paying for
# the request.
LIVE_WINDOW_HOURS_BEFORE = 2
LIVE_WINDOW_HOURS_AFTER = 6


class Command(BaseCommand):
    help = "Sync fixtures, live scores and results from the upstream feeds."

    def add_arguments(self, parser):
        parser.add_argument("--live", action="store_true", help="Refresh in-play score, period and clock.")
        parser.add_argument("--results", action="store_true", help="Pull final scores and grade tips.")
        parser.add_argument("--fixtures", action="store_true", help="Refresh the draw (kickoffs, venues).")
        parser.add_argument("--org", type=int, help="Limit to one organisation id.")
        parser.add_argument("--round", type=int, help="Limit to one round number.")
        parser.add_argument(
            "--all-rounds", action="store_true",
            help="Ignore the in-play time window and sweep every round.",
        )

    def handle(self, *args, **opts):
        kinds = [k for k in ("live", "results", "fixtures") if opts[k]]
        if not kinds:
            kinds = ["live", "results"]

        targets = self._targets(opts)
        if not targets:
            self.stdout.write("Nothing to sync — no rounds matched.")
            return

        total = 0
        failures = 0
        for kind in kinds:
            for org, round_number, competition in targets:
                try:
                    with SyncRun.record(
                        kind=kind, competition=competition, org=org, round_number=round_number,
                    ) as run:
                        svc = get_sync_service(competition)
                        fn = getattr(svc, f"sync_{kind}")
                        run.matches_touched = fn(
                            competition=competition, round_number=round_number, org=org,
                        )
                        total += run.matches_touched
                except SyncError as e:
                    # An unconfigured or flaky feed must not stop the other
                    # orgs and competitions from syncing.
                    failures += 1
                    logger.warning("sync %s %s r%s org=%s failed: %s",
                                   kind, competition, round_number, org.id, e)
                    self.stderr.write(f"  {kind} {competition} R{round_number} {org.name}: {e}")

        msg = f"Synced {total} match update(s) across {len(targets)} round/org pair(s)."
        if failures:
            self.stderr.write(self.style.WARNING(f"{msg} {failures} feed call(s) failed."))
        else:
            self.stdout.write(self.style.SUCCESS(msg))

    def _targets(self, opts) -> list[tuple[Organisation, int, str]]:
        """Work out which (org, round, competition) triples to hit.

        Competition comes from the round's series name, which is what
        get_sync_service dispatches on.
        """
        rounds = Round.objects.select_related("org", "series", "org__season")

        if opts["org"]:
            rounds = rounds.filter(org_id=opts["org"])
        if opts["round"]:
            rounds = rounds.filter(round_number=opts["round"])

        if not opts["all_rounds"] and not opts["round"]:
            now = timezone.now()
            # A round is interesting if any of its matches kicked off recently
            # or is about to. Cheaper and more accurate than guessing from the
            # round's lockout alone, which only marks the first game.
            window = Match.objects.filter(
                kickoff_at__gte=now - timedelta(hours=LIVE_WINDOW_HOURS_AFTER),
                kickoff_at__lte=now + timedelta(hours=LIVE_WINDOW_HOURS_BEFORE),
            ).values("round_id")
            rounds = rounds.filter(
                Q(id__in=window) | Q(status__in=("open", "locked")),
            )

        seen = set()
        targets = []
        for r in rounds.exclude(status="complete"):
            comp = r.series.name.upper()
            key = (r.org_id, r.round_number, comp)
            if key in seen:
                continue
            seen.add(key)
            targets.append((r.org, r.round_number, comp))
        return targets
