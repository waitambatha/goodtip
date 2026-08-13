"""Keep fixtures, live scores and results in step with the upstream feeds.

Designed to be run on a schedule rather than from a request, because a feed
round-trip is far too slow to sit inside a page load. Three cadences, all from
this one command:

    # in-play scores and the clock — every 2 minutes
    */2 * * * *  cd /srv/goodtip && venv/bin/python manage.py sync_matches --live

    # final scores, which is what grades tips — every 15 minutes
    */15 * * * * cd /srv/goodtip && venv/bin/python manage.py sync_matches --results

    # the draw — hourly, so a game announced this morning is tippable by lunch
    7 * * * *    cd /srv/goodtip && venv/bin/python manage.py sync_matches --fixtures

With no flags it does --live --results, which is the useful default for a
single frequent job.

--fixtures runs in DISCOVERY mode: it asks each feed which rounds it is
publishing and creates whatever is missing, rather than refreshing the rounds
already in the database. The distinction matters more than it sounds. Without
it a new league holds no rounds, so the old target query matched nothing and it
stayed empty permanently — no cron frequency could have fixed that, because the
question being asked was "what changed in the rounds we have" and the answer for
an empty set is always "nothing".

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
from data_sync.services import SyncError, competition_for_series, get_sync_service
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
        parser.add_argument("--ladder", action="store_true", help="Refresh competition ladders/standings.")
        parser.add_argument("--org", type=int, help="Limit to one organisation id.")
        parser.add_argument("--round", type=int, help="Limit to one round number.")
        parser.add_argument(
            "--all-rounds", action="store_true",
            help="Ignore the in-play time window and sweep every round.",
        )
        parser.add_argument(
            "--discover", action="store_true",
            help="Ask the feeds which rounds exist instead of reading local rounds. "
                 "Required to pick up rounds and games not yet in the database.",
        )
        parser.add_argument(
            "--probe-nrl", action="store_true",
            help="Print one raw NRL game from API-SPORTS and stop. Use this to "
                 "confirm the field mapping once a key is configured.",
        )

    def handle(self, *args, **opts):
        if opts["probe_nrl"]:
            return self._probe_nrl(opts)

        kinds = [k for k in ("live", "results", "fixtures", "ladder") if opts[k]]
        if not kinds:
            kinds = ["live", "results"]

        # The ladder is a property of the competition and season, not of a round
        # or a league, so it runs once per competition rather than once per
        # target — otherwise ten leagues on AFL 2026 would write the same 18
        # rows ten times.
        #
        # It also has to run BEFORE the round targeting below. Standings do not
        # depend on a round existing, but the "no rounds matched" guard returns
        # early — so a lone --ladder run was exiting before it did anything.
        if "ladder" in kinds:
            kinds = [k for k in kinds if k != "ladder"]
            self._sync_ladders(opts)
            if not kinds:
                return

        # --fixtures implies discovery. Refreshing the draw is the one job whose
        # whole purpose is learning what changed upstream, and doing that from a
        # list of rounds we already hold can only ever confirm what we knew.
        if opts["discover"] or "fixtures" in kinds:
            targets = self._discovered_targets(opts)
            # Fixtures must run first, or live/results are asked about rounds
            # that this same invocation is about to create.
            kinds = ["fixtures"] + [k for k in kinds if k != "fixtures"]
        else:
            targets = self._targets(opts)
        if not targets:
            self.stdout.write("Nothing to sync — no rounds matched.")
            return

        total = 0
        failures = 0
        # One service instance per competition for the whole run. Building a
        # fresh one per target threw away its response cache every time, which
        # is what made a full sync issue the same feed request once per league.
        services: dict = {}
        for kind in kinds:
            for org, round_number, competition in targets:
                try:
                    with SyncRun.record(
                        kind=kind, competition=competition, org=org, round_number=round_number,
                    ) as run:
                        if competition not in services:
                            services[competition] = get_sync_service(competition)
                        svc = services[competition]
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



    def _sync_ladders(self, opts) -> None:
        seen = set()
        orgs = Organisation.objects.select_related("season").prefetch_related("competitions__series")
        if opts["org"]:
            orgs = orgs.filter(pk=opts["org"])
        services: dict = {}
        for org in orgs:
            if org.season_id is None:
                continue
            for comp in org.competitions.all():
                for series in comp.series.all():
                    # Same Series-to-Competition resolve as _targets: the
                    # ladder feeds answer to "AFL", never to "AFLW".
                    name = competition_for_series(series.name)
                    if name is None:
                        continue
                    key = (name, org.season_id)
                    if key in seen:
                        continue
                    seen.add(key)
                    if name not in services:
                        try:
                            services[name] = get_sync_service(name)
                        except SyncError:
                            services[name] = None
                    svc = services[name]
                    if svc is None or not hasattr(svc, "sync_ladder"):
                        continue
                    try:
                        with SyncRun.record(kind="ladder", competition=name, org=org) as run:
                            run.matches_touched = svc.sync_ladder(competition=name, org=org)
                        self.stdout.write(f"  ladder {name} {org.season.year}: {run.matches_touched} teams")
                    except SyncError as e:
                        logger.warning("ladder %s failed: %s", name, e)
                        self.stderr.write(f"  ladder {name}: {e}")

    def _probe_nrl(self, opts):
        """Dump one real game so the mapping can be checked against a payload.

        The rest of the NRL client was written without a key, so the envelope is
        verified but the per-game field names are from API-SPORTS' published
        rugby schema rather than from something observed. This is how that gets
        settled in one command rather than by a failing sync at 3am.
        """
        import json

        from data_sync.services import ApiSportsRugbySyncService

        org = None
        if opts["org"]:
            org = Organisation.objects.select_related("season").filter(pk=opts["org"]).first()
        if org is None:
            org = Organisation.objects.select_related("season").order_by("pk").first()
        if org is None:
            self.stderr.write("No organisation to probe with.")
            return

        svc = ApiSportsRugbySyncService()
        try:
            self.stdout.write(f"League id: {svc._nrl_league_id()}")
            games = svc._games(org)
        except SyncError as e:
            self.stderr.write(self.style.ERROR(str(e)))
            return

        self.stdout.write(f"Season {org.season.year}: {len(games)} game(s) returned.")
        if not games:
            return
        g = games[0]
        self.stdout.write("\n--- raw game[0] ---")
        self.stdout.write(json.dumps(g, indent=2, default=str)[:4000])
        self.stdout.write("\n--- as this client reads it ---")
        self.stdout.write(f"  round      -> {svc._round_number(g)}")
        self.stdout.write(f"  scores     -> {svc._scores(g)}")
        self.stdout.write(f"  status     -> {(g.get('status') or {})}")
        _, home, away = svc._teams(g)
        self.stdout.write(f"  home/away  -> {home} / {away}")
        if not home or not away:
            self.stdout.write(self.style.WARNING(
                "  Teams did not resolve — add the feed's spellings to "
                "TEAM_SLUG_ALIASES or seed the NRL teams."
            ))

    def _discovered_targets(self, opts) -> list[tuple[Organisation, int, str]]:
        """Ask each feed what it is publishing, and work from that.

        _targets below reads local Round rows, which makes it structurally
        incapable of noticing a round that does not exist yet: an org created
        today holds nothing, matches nothing, and syncs nothing forever. That
        is not a stale-data problem, it is a discovery problem, and no cron
        frequency fixes it.

        Here the feed is the source of truth. An org's Competitions name the
        Series it tips, each Series maps to a feed, and the feed is asked which
        rounds it has. Anything new comes back and gets created by
        sync_fixtures on the same pass.
        """
        orgs = Organisation.objects.select_related("season").prefetch_related(
            "competitions__series"
        )
        if opts["org"]:
            orgs = orgs.filter(pk=opts["org"])

        targets, seen = [], set()
        services: dict = {}
        for org in orgs:
            if org.season_id is None:
                continue
            # The set of feeds to ask falls out of what the org signed up for,
            # resolved from Series to Competition: an org on AFL + AFLW asks
            # the AFL feed once, not once per series under a name it does not
            # answer to.
            names = {
                key
                for comp in org.competitions.all()
                for s in comp.series.all()
                if (key := competition_for_series(s.name)) is not None
            }
            for comp_name in sorted(names):
                if comp_name not in services:
                    try:
                        services[comp_name] = get_sync_service(comp_name)
                    except SyncError:
                        services[comp_name] = None
                svc = services[comp_name]
                if svc is None:
                    continue  # no feed for this series yet — nothing to ask
                discover = getattr(svc, "discover_rounds", None)
                if discover is None:
                    continue
                try:
                    numbers = discover(competition=comp_name, org=org)
                except SyncError as e:
                    # A feed that is down or unimplemented must not stop the
                    # others; it is reported, not fatal.
                    logger.warning("discover %s org=%s failed: %s", comp_name, org.id, e)
                    self.stderr.write(f"  discover {comp_name} {org.name}: {e}")
                    continue
                if opts["round"]:
                    numbers = [n for n in numbers if n == opts["round"]]
                for n in numbers:
                    key = (org.id, n, comp_name)
                    if key not in seen:
                        seen.add(key)
                        targets.append((org, n, comp_name))
        return targets

    def _targets(self, opts) -> list[tuple[Organisation, int, str]]:
        """Work out which (org, round, competition) triples to hit.

        A Round knows its Series; the feeds are keyed by Competition. Those
        two names coincide for AFL and NRL and differ for everything else, so
        the series is resolved through competition_for_series rather than
        being upper-cased and hoped for.
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
            comp = competition_for_series(r.series.name)
            if comp is None:
                # No feed covers this series. Skipping is right: raising per
                # round would bury the codes that do sync under noise.
                continue
            # The AFL and AFLW rounds of one org collapse to a single AFL
            # target, because one call to the service syncs both series.
            key = (r.org_id, r.round_number, comp)
            if key in seen:
                continue
            seen.add(key)
            targets.append((r.org, r.round_number, comp))
        return targets
