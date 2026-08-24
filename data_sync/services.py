from __future__ import annotations

import logging
from typing import Protocol

from django.db import IntegrityError
from django.utils.text import slugify
from django.utils import timezone

from catalog.models import Competition, Series
from orgs.models import Organisation
from tipping.models import Match, Round, Team
from tipping.services import record_match_result
from .prewarm import fixture_cache_get, fixture_cache_put


logger = logging.getLogger(__name__)


class SyncError(Exception):
    pass


class DataSyncService(Protocol):
    # Which rounds the feed has, independent of what exists locally. This is
    # what lets a newly published round be found at all.
    def discover_rounds(self, *, competition: str, org: Organisation) -> list[int]: ...
    def sync_fixtures(self, *, competition: str, round_number: int, org: Organisation) -> int: ...
    def sync_results(self, *, competition: str, round_number: int, org: Organisation) -> int: ...
    def sync_live(self, *, competition: str, round_number: int, org: Organisation) -> int: ...


# afl.com.au names several clubs by their city alone, while the Team table
# stores the full club name. Without this map, games fail to resolve and get
# skipped, so scores and results never land for them.
#
# "gws-giants" earns its line the hard way: Sportradar's resolver did not
# consult this map, could not match "GWS GIANTS" to Greater Western Sydney, and
# created a SECOND club record. GWS's history was split across the two until
# tipping/0010 merged them.
TEAM_SLUG_ALIASES = {
    "adelaide": "adelaide-crows",
    "brisbane": "brisbane-lions",
    "geelong": "geelong-cats",
    "gold-coast": "gold-coast-suns",
    "gws": "greater-western-sydney",
    "gws-giants": "greater-western-sydney",
    "kangaroos": "north-melbourne",
    "sydney": "sydney-swans",
    "west-coast": "west-coast-eagles",
    "footscray": "western-bulldogs",
    "bulldogs": "western-bulldogs",
}


def _normalise_team_name(name: str) -> str:
    # A fixture can be published before both sides are known: finals slots
    # arrive with no team named, and the draw fills them in as qualifying games
    # are played. That is normal feed data, not a fault, so the name has to be
    # allowed to be missing rather than crashing the sync run on the first such
    # row.
    slug = slugify((name or "").replace("&", "and"))
    return TEAM_SLUG_ALIASES.get(slug, slug)


def _resolve_team(series: Series, name: str, external_id: str = "") -> Team | None:
    if not name:
        return None
    slug = _normalise_team_name(name)
    if external_id:
        t = Team.objects.filter(series=series, external_id=external_id).first()
        if t:
            return t
    t = Team.objects.filter(series=series, slug=slug).first()
    if t:
        if external_id and not t.external_id:
            t.external_id = external_id
            t.save(update_fields=["external_id"])
        return t
    t = Team.objects.filter(series=series, name__iexact=name).first()
    if t and external_id and not t.external_id:
        t.external_id = external_id
        t.save(update_fields=["external_id"])
    return t


def _resolve_match(round_obj: Round, external_id: str, home: Team, away: Team) -> Match | None:
    """Find the row a feed game corresponds to, by id or by the team pairing.

    Fixtures seeded locally (seed_fixtures_2026) carry no external_id, so an
    id-only lookup misses them — which would make sync_fixtures create a second
    copy of every game rather than update the one already there, and leave
    sync_live with nothing to update. Falling back to the two teams within the
    round identifies the same game, and the id is backfilled so subsequent
    syncs take the fast path.
    """
    if external_id:
        m = Match.objects.filter(round=round_obj, external_id=external_id).first()
        if m:
            return m
    m = Match.objects.filter(round=round_obj, home_team=home, away_team=away).first()
    if m and external_id and m.external_id != external_id:
        m.external_id = external_id
        m.save(update_fields=["external_id"])
    return m


def _ensure_round_competition(round_obj: Round, series: Series, org: Organisation) -> None:
    """Make sure a round points at the org's Competition for this season.

    get_or_create only applies ``defaults`` when it CREATES. A round made by any
    other path — the old seeding command, an import, a season change — keeps
    whatever competition it had, which for the seed was none at all.

    That is invisible until it is not: the dashboard selects rounds with
    ``competition__in=org.competitions``, so a round with competition=NULL is
    filtered out and the member sees no games, with correct fixtures sitting in
    the database the whole time. Repairing it on every fixtures sync means the
    condition heals itself rather than needing a migration each time it appears.
    """
    want = Competition.for_series(series, org.season)
    if want and round_obj.competition_id != want.id:
        round_obj.competition = want
        round_obj.save(update_fields=["competition"])


def get_sync_service(competition: str) -> DataSyncService:
    """The provider for a competition.

    Two branches, both scrapers, no keys and no fallbacks. Every commercial and
    community feed this project tried has been removed:

        Sportradar   commissioned, worked, and was a TRIAL key expiring
                     11 Sep 2026. Priced out.
        Squiggle     free, AFL only, and began returning 403 within minutes of
                     traffic-driven syncing being switched on.
        API-SPORTS   needed a paid key that was never issued, so every NRL sync
                     in this project's history failed with "APISPORTS_KEY is
                     not set" and no NRL org ever had a fixture.

    What is left reads the same data the two sports' own websites serve their
    own front-ends, which is both free and, in coverage, strictly better than
    what was paid for: nrl.com carries State of Origin, which Sportradar's
    launch scope did not.

    There is deliberately nothing to fall back to. A scraper break is now a
    visible outage recorded against SyncRun rather than a silent switch to a
    source nobody is watching.
    """
    comp = competition.upper()
    if comp == "AFL":
        return AflScrapeSyncService()       # afl.com.au, covers AFL and AFLW
    if comp == "NRL":
        return NrlScrapeSyncService()       # nrl.com, covers NRL, NRLW, Origin
    raise SyncError(f"Unsupported competition: {competition}")


def competition_for_series(series_name: str) -> str | None:
    """The competition key that covers a Series, or None if no feed does.

    Feeds are keyed by COMPETITION ("AFL", "NRL"), and each sync service loops
    the Series that competition bundles internally: AFL covers AFLW, NRL
    covers NRLW and State of Origin. A caller that starts from a Round holds a
    SERIES, so it has to come back through here first.

    Handing a Series name straight to get_sync_service is what made every
    AFLW, NRLW and Origin round fail with "Unsupported competition: AFLW"
    while the AFL round sitting beside it synced fine — the two names happen
    to match for the men's series and nowhere else.

    Derived from the services' own SERIES lists rather than a second hardcoded
    table, so adding a series to a feed cannot leave this behind.
    """
    wanted = (series_name or "").strip().upper()
    for competition, service in (
        ("AFL", AflScrapeSyncService),
        ("NRL", NrlScrapeSyncService),
    ):
        if wanted in {s.upper() for s in service.SERIES}:
            return competition
    return None


# NRL nickNames that do not slugify to our Team slug. Only two of seventeen,
# because nrl.com's nickName ("Sharks", "Cowboys") already matches how the
# Team table slugs most clubs; these two are stored under their full name.
NRL_SLUG_ALIASES = {
    "broncos": "brisbane-broncos",
    "raiders": "canberra-raiders",
    # Canterbury-Bankstown are stored under the bare slug "bulldogs". This
    # entry exists to STOP the AFL map being consulted for them: it maps
    # "bulldogs" to Western Bulldogs, an AFL club, so every Canterbury game was
    # being skipped as an unresolved team. Codes share nicknames, and a single
    # global alias table cannot serve both.
    "bulldogs": "bulldogs",
}


def _resolve_nrl_team(series: Series, nickname: str, external_id: str = "") -> Team | None:
    """Team lookup for the scraped NRL feed.

    Resolution is deliberately self-contained rather than delegating to
    _resolve_team: that helper runs names through _normalise_team_name, which
    applies the AFL alias map, and rugby league shares nicknames with Aussie
    rules. Backfilling external_id from nrl.com's stable teamId is the useful
    side effect — after one sync every club matches by id and the name mapping
    stops mattering.
    """
    if not nickname:
        return None

    def _stamp(team):
        if team and external_id and not team.external_id:
            team.external_id = external_id
            team.save(update_fields=["external_id"])
        return team

    if external_id:
        t = Team.objects.filter(series=series, external_id=external_id).first()
        if t:
            return t
    slug = slugify(nickname.replace("&", "and"))
    slug = NRL_SLUG_ALIASES.get(slug, slug)
    t = Team.objects.filter(series=series, slug=slug).first()
    if t:
        return _stamp(t)
    # Full name, then a nickname that is the tail of the stored club name
    # ("Dragons" inside "St. George Illawarra Dragons").
    t = Team.objects.filter(series=series, name__iexact=nickname).first()
    if t:
        return _stamp(t)
    return _stamp(Team.objects.filter(series=series, name__iendswith=nickname).first())


def refresh_round_state(round_obj: Round) -> bool:
    """Recompute a round's status from its fixtures. True if it changed.

    Nothing in this project ever set ``Round.status``. On the live database 354
    of 356 rounds were still sitting on the "upcoming" default, including
    rounds whose grand final was played in March — and the two exceptions were
    stale rows somebody had edited by hand in the admin.

    That was not cosmetic. The sync command's round targeting matched on
    ``status__in=("open", "locked")``, so across thirty-three leagues the only
    round the live poller ever visited was one org's AFL Round 1, refetched
    every two minutes since March. The ESG report counts completed rounds off
    this field too, and read 1.

    Derived, never authored: the fixtures say what the round is doing.

        complete  every fixture graded
        locked    the first fixture has kicked off, some are still to finish
        upcoming  nothing has started

    "open" is deliberately not produced here. Whether a member may TIP a round
    is decided by ``tipping.services.tip_window`` — a rolling two rounds per
    series — which is a different question from what the fixtures are doing,
    and having two mechanisms answer it is how they drift apart.
    """
    now = timezone.now()
    rows = list(round_obj.matches.values_list("status", "kickoff_at"))
    if not rows:
        return False

    if all(status == Match.STATUS_COMPLETE for status, _ in rows):
        state = "complete"
    elif any(kickoff <= now for _, kickoff in rows):
        state = "locked"
    else:
        state = "upcoming"

    if round_obj.status == state:
        return False
    round_obj.status = state
    round_obj.save(update_fields=["status"])
    return True


class _ScrapeSyncService:
    """What the two scraped feeds have in common, which is nearly everything.

    AFL and NRL differ in three places: which scraper they hold, how a team
    name is resolved to a club, and which Series the competition bundles.
    Everything else — round creation, staging, change detection, grading,
    history, the ladder — was duplicated line for line in the two services,
    and a fix applied to one of them silently did not apply to the other.

    ``competition`` throughout is the catalog Competition name ("AFL", "NRL"),
    while the scrapers work in Series ("AFLW", "State of Origin"). One request
    serves one round of one series, so every method loops ``SERIES``.

    ONE INSTANCE PER SYNC RUN. The scraper's page cache and its request pacing
    hang off it, so thirty leagues tipping the same round cost one fetch.
    """

    #: Catalog Series this service covers, in the order they are synced.
    SERIES: list[str] = []
    #: The catalog Competition name this service answers to.
    COMPETITION = ""
    #: Named in the error a caller sees, so "which feed?" needs no lookup.
    SOURCE = ""

    def __init__(self):
        self._scraper = self._build_scraper()
        # Games already written to HistoricalMatch this run. That table is not
        # org-scoped, so without this the same fixture is upserted once per
        # league — thirty-three identical round trips to a remote database for
        # one game.
        self._history_seen: set[tuple[int, int, int, int]] = set()
        # Seasons whose finals staging has already been recomputed this run.
        self._restaged: set[tuple[int, int]] = set()

    # ---- subclass hooks -------------------------------------------------

    def _build_scraper(self):
        raise NotImplementedError

    @property
    def _error(self) -> type[Exception]:
        """The scraper's own exception type, for the per-series catch."""
        raise NotImplementedError

    def _scraper_key(self, series_name: str) -> str:
        """How this feed's scraper wants the series named."""
        return series_name

    def _resolve(self, series: Series, name: str, external_id: str) -> Team | None:
        raise NotImplementedError

    # ---- shared ---------------------------------------------------------

    def _check(self, competition: str) -> None:
        if competition != self.COMPETITION:
            raise SyncError(
                f"The {self.SOURCE} scraper handles the {self.COMPETITION} "
                "competition only."
            )

    def _series_rows(self, series_name: str, round_number: int, year: int) -> tuple[Series | None, list[dict]]:
        series = Series.objects.filter(name=series_name).first()
        if series is None:
            return None, []
        key = self._scraper_key(series_name)

        # This call is the org-independent half of a fixtures sync, and the
        # only slow one — everything after it is local writes. A page already
        # fetched for one organisation is the same page every other one wants,
        # so it is served from FixtureCache when it is still fresh. That is
        # what lets a brand-new org be given its draw with no network in the
        # request: the wizard warms this as soon as the competitions are
        # picked (see prewarm_fixtures).
        cached = fixture_cache_get(self.SOURCE, key, year, round_number)
        if cached is not None:
            return series, cached

        try:
            rows = self._scraper.fixtures(
                series=key, season=year, round_number=round_number,
            )
        except self._error as e:
            # One series failing — Origin has no round 24 — must not abort the
            # others, which are the ones anybody is tipping.
            logger.info("%s %s round %s: %s", self.SOURCE, series_name, round_number, e)
            return series, []
        fixture_cache_put(self.SOURCE, key, year, round_number, rows)
        return series, rows

    def _round_for(self, org: Organisation, series: Series, round_number: int) -> Round | None:
        return Round.objects.filter(
            org=org, round_number=round_number, series=series
        ).first()

    def _match_index(self, round_obj: Round) -> tuple[dict, dict]:
        """Every fixture in the round, indexed for in-memory resolution.

        One query instead of one or two per game. Against a database ~370ms
        away that is the difference between a full-season sweep taking minutes
        and taking most of an hour.
        """
        matches = list(Match.objects.filter(round=round_obj))
        by_ext = {m.external_id: m for m in matches if m.external_id}
        by_pair = {(m.home_team_id, m.away_team_id): m for m in matches}
        return by_ext, by_pair

    @staticmethod
    def _lookup(by_ext: dict, by_pair: dict, external_id: str, home: Team, away: Team) -> Match | None:
        """The in-memory twin of ``_resolve_match``.

        Fixtures seeded locally carry no external_id, so an id-only lookup
        misses them and the sync would create a second copy of every game.
        The team pairing within the round identifies the same fixture.
        """
        if external_id and external_id in by_ext:
            return by_ext[external_id]
        return by_pair.get((home.id, away.id))

    # ---- warming, ahead of any organisation -----------------------------

    def discover_rounds_for_season(self, competition: str, year: int) -> list[int]:
        """Every round this feed publishes for a season, with no org involved.

        The org-taking ``discover_rounds`` below reads nothing from the
        organisation except ``org.season.year``, so this is the same question
        asked with the year passed in directly — which is what makes it usable
        before the organisation exists (see data_sync.prewarm).
        """
        self._check(competition)
        found: set[int] = set()
        for name in self.SERIES:
            try:
                found.update(self._scraper.available_rounds(
                    series=self._scraper_key(name), season=year,
                ))
            except self._error as e:
                logger.info("%s available_rounds %s %s: %s", self.SOURCE, name, year, e)
        return sorted(found)

    def warm_round(self, competition: str, round_number: int, year: int) -> int:
        """Pull one round's fixtures into the cache without writing any rounds.

        Goes through ``_series_rows``, so it populates FixtureCache by the same
        path a real sync reads it back from — there is no second fetching code
        path that could drift from the one that matters.
        """
        self._check(competition)
        seen = 0
        for name in self.SERIES:
            _series, rows = self._series_rows(name, round_number, year)
            seen += len(rows)
        return seen

    # ---- discovery ------------------------------------------------------

    def discover_rounds(self, *, competition: str, org: Organisation,
                        horizon_days: int = 21, back_days: int = 3,
                        full_season: bool = False) -> list[int]:
        """Which rounds this feed is publishing that are worth syncing now.

        ``full_season`` is the difference between staying current and being
        complete. The rolling window asks the feed for one round back and three
        forward, which keeps this week right and costs one page — but a round
        outside that window when the sync happened to run is never fetched, and
        never will be. That is exactly how the live database ended up holding
        AFL 2026 rounds 1-5 and 21-25 with nothing in between: a permanent hole
        in the middle of the season that no cron frequency could close, because
        nothing was ever going to ask about round 12 again.

        The full sweep asks for every round the feed publishes. It is the
        expensive one, so it runs on its own slow cadence rather than every
        tick — see the "backfill" kind in data_sync.autosync.
        """
        self._check(competition)
        found: set[int] = set()
        for name in self.SERIES:
            try:
                if full_season:
                    found.update(self._scraper.available_rounds(
                        series=self._scraper_key(name), season=org.season.year,
                    ))
                else:
                    found.update(self._scraper.rounds_in_window(
                        series=self._scraper_key(name), season=org.season.year,
                        horizon_days=horizon_days, back_days=back_days,
                    ))
            except self._error as e:
                logger.info("%s discover %s: %s", self.SOURCE, name, e)
        return sorted(found)

    # ---- fixtures -------------------------------------------------------

    def _ensure_round(self, org: Organisation, series: Series,
                      round_number: int, rows: list[dict]) -> Round:
        """The Round these fixtures belong to, with its lockout and stage right."""
        from matchreader.stages import stage_for_round

        earliest = min(r["kickoff_at"] for r in rows)
        stage = stage_for_round(series, org.season.year, round_number, len(rows))

        round_obj, created = Round.objects.get_or_create(
            org=org, round_number=round_number, series=series,
            defaults={
                "lockout_at": earliest,
                "status": "upcoming",
                "stage": stage,
                "competition": Competition.for_series(series, org.season),
            },
        )
        if created:
            _ensure_round_competition(round_obj, series, org)
            return round_obj

        changed = []
        if round_obj.lockout_at != earliest:
            round_obj.lockout_at = earliest
            changed.append("lockout_at")
        if round_obj.stage != stage:
            # The stage decides what a correct tip is WORTH (1 / 2 / 4), and
            # points_awarded is stored on the Tip at grading time. Changing the
            # stage after a round has been graded therefore has to regrade it,
            # or the round keeps paying the old rate forever. This is not
            # hypothetical: every State of Origin round in the live database
            # was created on the "regular" default and paid 1 point instead of
            # 4, and simply correcting the stage would not have moved a single
            # leaderboard.
            round_obj.stage = stage
            changed.append("stage")
        if changed:
            round_obj.save(update_fields=changed)
            if "stage" in changed:
                from tipping.services import regrade_round
                regrade_round(round_obj)

        _ensure_round_competition(round_obj, series, org)
        return round_obj

    def sync_fixtures(self, *, competition: str, round_number: int, org: Organisation) -> int:
        """Create and refresh the draw. Returns fixtures actually written.

        The count is WRITES, not rows seen. A steady state genuinely is zero,
        which is what makes the number worth watching: the old code counted
        every fixture it looked at, so a healthy run and a run that changed
        nothing both reported 18 and neither told you anything.
        """
        self._check(competition)
        year = org.season.year
        written = 0
        for name in self.SERIES:
            series, rows = self._series_rows(name, round_number, year)
            if not rows:
                continue
            round_obj = self._ensure_round(org, series, round_number, rows)
            by_ext, by_pair = self._match_index(round_obj)

            for r in rows:
                home = self._resolve(series, r["home_name"], r["home_external_id"])
                away = self._resolve(series, r["away_name"], r["away_external_id"])
                if not home or not away:
                    logger.warning(
                        "Skip %s game %s: unresolved teams %s/%s",
                        self.SOURCE, r["external_id"], r["home_name"], r["away_name"],
                    )
                    continue
                match = self._lookup(by_ext, by_pair, r["external_id"], home, away)
                if match is None:
                    match = Match.objects.create(
                        round=round_obj, external_id=r["external_id"],
                        home_team=home, away_team=away,
                        kickoff_at=r["kickoff_at"],
                        venue=r["venue"], venue_city=r["venue_city"],
                    )
                    by_ext[match.external_id] = match
                    by_pair[(home.id, away.id)] = match
                    written += 1
                    continue

                wanted = {
                    "external_id": r["external_id"],
                    "home_team_id": home.id,
                    "away_team_id": away.id,
                    "kickoff_at": r["kickoff_at"],
                    "venue": r["venue"],
                    "venue_city": r["venue_city"],
                }
                changed = [f for f, v in wanted.items() if getattr(match, f) != v]
                if not changed:
                    continue
                for field in changed:
                    setattr(match, field, wanted[field])
                match.save(update_fields=changed)
                written += 1

            refresh_round_state(round_obj)
        return written

    # ---- live -----------------------------------------------------------

    def sync_live(self, *, competition: str, round_number: int, org: Organisation) -> int:
        """In-play score and clock. Deliberately does NOT grade tips.

        A score mid-game is provisional, and grading on it would award points
        that a later correction takes away. Grading happens once, in
        sync_results, when the game is over.
        """
        self._check(competition)
        year = org.season.year
        written = 0
        for name in self.SERIES:
            series, rows = self._series_rows(name, round_number, year)
            if not rows:
                continue
            round_obj = self._round_for(org, series, round_number)
            if round_obj is None:
                continue
            by_ext, by_pair = self._match_index(round_obj)

            for r in rows:
                if r["status"] == "scheduled":
                    continue
                home = self._resolve(series, r["home_name"], r["home_external_id"])
                away = self._resolve(series, r["away_name"], r["away_external_id"])
                if not home or not away:
                    continue
                match = self._lookup(by_ext, by_pair, r["external_id"], home, away)
                if match is None:
                    continue

                wanted = {
                    "home_score": r["home_score"],
                    "away_score": r["away_score"],
                    "status": r["status"],
                    "period": (r["period"] or "")[:32],
                    "clock": (r["clock"] or "")[:16],
                }
                if all(getattr(match, f) == v for f, v in wanted.items()):
                    # Nothing moved. Stamping live_updated_at anyway would make
                    # every quiet poll look like a score change to anything
                    # watching freshness.
                    continue
                for field, value in wanted.items():
                    setattr(match, field, value)
                match.live_updated_at = timezone.now()
                match.save(update_fields=[*wanted, "live_updated_at"])
                written += 1

            refresh_round_state(round_obj)
        return written

    # ---- results --------------------------------------------------------

    def _record_history(self, series: Series, season: int, round_number: int,
                        home: Team, away: Team, row: dict) -> None:
        """Store the finished game in HistoricalMatch, which the ladder reads.

        THE missing link. ``rebuild_ladder`` reads HistoricalMatch, and the
        only thing that ever wrote it was ``backfill_history`` — a command
        somebody had to remember to run. So every ladder in the app was frozen
        at whatever the last manual backfill captured, while results landed
        correctly in tipping.Match beside it. Writing here closes the loop and
        the standings keep themselves current.

        Keyed on the natural key — these two clubs, that round, that season —
        for the same reason the backfill is: it is what identifies a real game
        across any source, where an external id identifies it only within one.
        """
        from matchreader.models import HistoricalMatch

        key = (series.id, season, round_number, home.id)
        if key in self._history_seen:
            return
        self._history_seen.add(key)

        try:
            HistoricalMatch.objects.update_or_create(
                series=series, season=season, round_number=round_number,
                home_team=home, away_team=away,
                defaults={
                    "external_id": row["external_id"],
                    "kickoff_at": row["kickoff_at"],
                    "home_score": int(row["home_score"]),
                    "away_score": int(row["away_score"]),
                },
            )
        except IntegrityError:
            # The other uniqueness rule, (series, external_id), belongs to a
            # different row — the same fixture already stored under a differing
            # natural key, e.g. after a feed swaps the home side. Worth a line
            # in the log; not worth failing a whole sync run over.
            logger.warning(
                "History clash for %s %s R%s %s v %s (%s)",
                series.name, season, round_number, home, away, row["external_id"],
            )

    def sync_results(self, *, competition: str, round_number: int, org: Organisation) -> int:
        """Final scores, the grading that follows, and the ladder's input."""
        self._check(competition)
        year = org.season.year
        written = 0
        for name in self.SERIES:
            series, rows = self._series_rows(name, round_number, year)
            if not rows:
                continue
            round_obj = self._round_for(org, series, round_number)
            if round_obj is None:
                continue
            by_ext, by_pair = self._match_index(round_obj)
            recorded = False

            for r in rows:
                # Only a finished game grades. Anything else is provisional.
                if r["status"] != "complete" or r["home_score"] is None:
                    continue
                home = self._resolve(series, r["home_name"], r["home_external_id"])
                away = self._resolve(series, r["away_name"], r["away_external_id"])
                if not home or not away:
                    continue

                home_score, away_score = int(r["home_score"]), int(r["away_score"])
                # History is per real game, so it is recorded whether or not
                # this league happens to hold the fixture. That is what lets a
                # ladder be complete while the org-scoped draw is partial.
                self._record_history(series, year, round_number, home, away, r)
                recorded = True

                match = self._lookup(by_ext, by_pair, r["external_id"], home, away)
                if match is None:
                    continue

                settled = (
                    match.status == Match.STATUS_COMPLETE
                    and match.home_score == home_score
                    and match.away_score == away_score
                    and match.result is not None
                )
                if settled:
                    # Already graded on these exact scores. Re-running
                    # record_match_result would rewrite every tip on the match
                    # every fifteen minutes for the rest of the season.
                    continue

                period = (r["period"] or "")[:32]
                if match.status != Match.STATUS_COMPLETE or match.period != period:
                    match.status = Match.STATUS_COMPLETE
                    match.period = period
                    match.save(update_fields=["status", "period"])
                # Writes the scores, derives the result and regrades every tip
                # on this match. Also flags an already-published recap for
                # review if the score is a correction rather than a first
                # result, which is why the scores go through here and are not
                # written directly above.
                record_match_result(match, home_score, away_score)
                written += 1

            if recorded:
                self._restage(series, year)
            refresh_round_state(round_obj)
        return written

    def _restage(self, series: Series, season: int) -> None:
        """Re-mark the season's finals, once per series per run.

        Has to happen after history is written rather than before: whether a
        round is finals is read from the shape of the season, so the answer can
        change the moment a new round of results lands.
        """
        from matchreader.stages import restage_season

        key = (series.id, season)
        if key in self._restaged:
            return
        self._restaged.add(key)
        restage_season(series, season)

    # ---- ladder ---------------------------------------------------------

    def sync_ladder(self, *, competition: str, org: Organisation, **kw) -> int:
        """Derived from our own results — no third-party request at all.

        AFL standings used to come from Squiggle (403s under load) and the NRL
        had none at all, because nrl.com serves them from a page nobody
        collected. A ladder is arithmetic over completed games, and the games
        are already ours.
        """
        self._check(competition)
        from .ladder import rebuild_for_competition

        return rebuild_for_competition(competition, season=org.season)


class NrlScrapeSyncService(_ScrapeSyncService):
    """NRL, NRLW and State of Origin, from nrl.com's own draw page.

    Replaces ApiSportsRugbySyncService, which could never run: the original
    host had no DNS record, and its replacement needs a paid key that was never
    issued. Every NRL sync in this project's history failed with "APISPORTS_KEY
    is not set", and no NRL league ever held a fixture.
    """

    SERIES = ["NRL", "NRLW", "State of Origin"]
    COMPETITION = "NRL"
    SOURCE = "nrl.com"

    def _build_scraper(self):
        from .scrapers.nrl import NrlDrawScraper
        return NrlDrawScraper()

    @property
    def _error(self):
        from .scrapers.nrl import NrlScrapeError
        return NrlScrapeError

    def _scraper_key(self, series_name: str) -> str:
        # nrl.com's competition map is keyed upper-case, including the
        # three-word "STATE OF ORIGIN".
        return series_name.upper()

    def _resolve(self, series, name, external_id):
        return _resolve_nrl_team(series, name, external_id)


class AflScrapeSyncService(_ScrapeSyncService):
    """AFL and AFLW from afl.com.au's own CFS API.

    Replaces SquiggleSyncService. Squiggle worked, but it is AFL only — so
    AFLW, one of the codes GoodTip launches with, had no feed at all — and it
    rate-limits, returning 403 within minutes of syncing being switched on.
    """

    SERIES = ["AFL", "AFLW"]
    COMPETITION = "AFL"
    SOURCE = "afl.com.au"

    def _build_scraper(self):
        from .scrapers.afl import AflApiScraper
        return AflApiScraper()

    @property
    def _error(self):
        from .scrapers.afl import AflScrapeError
        return AflScrapeError

    def _resolve(self, series, name, external_id):
        return _resolve_team(series, name, external_id)
