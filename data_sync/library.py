"""GOODTIP'S OWN FIXTURE LIBRARY (client, 28 Oct 2026: "when they turn on the
tap, the water is already there... it's us as GoodTip to already have the data,
running at the background, so when they request... it's already in our
databases... for this season and the previous season").

Two halves.

REFRESH — every competition with a feed (the AFL and NRL scrapers, and every
ESPN league in data_sync.scrapers.espn.LEAGUES), whether or not any
organisation tips it yet, and whether or not the admin has switched it on:

  * each round's fixtures and scores go into FixtureCache — the same store the
    syncs already read, so nothing downstream learns a new shape;
  * every finished game goes into matchreader.HistoricalMatch, which is what
    ladders and MatchReader are built from;
  * each competition's ladder is rebuilt from it.

The hourly pass reads the current window (two weeks back, five ahead); the
full pass (every six hours, with the backfill) reads every round of this
season and last. One SyncRun per competition, kind "library", so the Services
board and the super admin's Sports screen can show each one working.

SEED — when an organisation adds a competition, its rounds and games are built
from the library alone (``library_only``: no network), then the regular syncs
keep them current. Adding football takes seconds, not a crawl of a season.
"""
from __future__ import annotations

import logging

from django.utils import timezone

logger = logging.getLogger("data_sync")

WINDOW_BACK_DAYS = 14
WINDOW_AHEAD_DAYS = 35
SEED_WEEKS_BACK = 8


def competitions() -> list[str]:
    """Every ESPN league. The AFL and the NRL are not here: every organisation
    tips them, and their own six-hourly sweep (sync_matches --backfill) already
    keeps their seasons, results and ladders whole — and the nrl.com scraper
    is paced at six seconds a request, so sweeping it here too would hold the
    scheduler for half an hour. An ESPN full sweep takes a couple of minutes.
    (`--only AFL` still works by hand.)"""
    from .scrapers.espn import LEAGUES

    return list(LEAGUES)


def refresh(*, full: bool = False, only: list[str] | None = None, log=None) -> dict:
    """Bring the library current. Returns {competition: rows read}."""
    from catalog.models import Season, Series

    from .ladder import rebuild_ladder
    from .models import SyncRun
    from .services import SyncError, get_sync_service

    year = timezone.localdate().year
    seasons = [year, year - 1] if full else [year]
    out = {}
    for comp in (only or competitions()):
        try:
            svc = get_sync_service(comp)
        except SyncError:
            continue
        try:
            with SyncRun.record(kind=SyncRun.KIND_LIBRARY, competition=comp) as run:
                read = 0
                for name in svc.SERIES:
                    series = Series.objects.filter(name=name).first()
                    if series is None:
                        continue
                    for season in seasons:
                        read += _refresh_series(svc, series, season, full=full or season < year)
                        svc._restage(series, season)
                        season_obj = Season.objects.filter(year=season).first()
                        if season_obj is not None:
                            rebuild_ladder(series=series, season=season_obj)
                run.matches_touched = read
            out[comp] = read
            if log:
                log(f"  library {comp}: {read} rows")
        except Exception as exc:  # noqa: BLE001 — one feed down must not stop the rest
            logger.warning("library %s failed: %s", comp, exc)
            out[comp] = None
            if log:
                log(f"  library {comp}: FAILED {exc}")
    return out


def _refresh_series(svc, series, season: int, *, full: bool) -> int:
    """One series, one season: every round in reach, fetched fresh, cached,
    and its finished games written to the results store."""
    name = series.name
    if full:
        rounds = svc.published_rounds(name, season, full_season=True)
    else:
        rounds = svc._scraper.rounds_in_window(
            series=svc._scraper_key(name), season=season,
            horizon_days=WINDOW_AHEAD_DAYS, back_days=WINDOW_BACK_DAYS,
        )
    read = 0
    for rn in rounds:
        _series, rows = svc.feed_rows(name, rn, season, fresh=True)   # fetches and caches
        read += len(rows)
        for r in rows:
            if r.get("status") != "complete" or r.get("home_score") is None or r.get("away_score") is None:
                continue
            home = svc.resolve_team(series, r["home_name"], r["home_external_id"])
            away = svc.resolve_team(series, r["away_name"], r["away_external_id"])
            if home and away:
                svc._record_history(series, season, rn, home, away, r)
    svc._raise_problems()
    return read


def seed_org(org, competition_names: list[str] | None = None) -> int:
    """Build an organisation's rounds and games from the library — no network.

    From SEED_WEEKS_BACK before today to the end of the season, so the
    dashboard has recent results to show and every game still to come.
    Returns games written.
    """
    from .models import FixtureCache
    from .services import SyncError, competition_for_series, get_sync_service

    if org.season_id is None:
        return 0
    year = org.season.year
    names = competition_names or sorted({
        key for comp in org.competitions.all() for s in comp.series.all()
        if (key := competition_for_series(s.name)) is not None
    })
    written = 0
    for name in names:
        try:
            svc = get_sync_service(name)
        except SyncError:
            continue
        svc.library_only = True
        keys = [svc._scraper_key(s) for s in svc.SERIES]
        rounds = sorted(set(
            FixtureCache.objects.filter(source=svc.SOURCE, series_key__in=keys, season_year=year)
            .values_list("round_number", flat=True)
        ))
        rounds = [rn for rn in rounds if _round_recent_enough(svc, keys, year, rn)]
        for rn in rounds:
            try:
                written += svc.sync_fixtures(competition=name, round_number=rn, org=org)
                svc.sync_results(competition=name, round_number=rn, org=org)
            except SyncError as exc:
                logger.info("seed %s R%s for org %s: %s", name, rn, org.pk, exc)
    return written


def _round_recent_enough(svc, keys, year, rn) -> bool:
    """A round worth building for a new organisation: one with a game in the
    last SEED_WEEKS_BACK weeks or still to come."""
    from .prewarm import fixture_cache_get

    cutoff = timezone.now() - timezone.timedelta(weeks=SEED_WEEKS_BACK)
    for key in keys:
        rows = fixture_cache_get(svc.SOURCE, key, year, rn, any_age=True) or []
        if any(r.get("kickoff_at") and r["kickoff_at"] >= cutoff for r in rows):
            return True
    return False


def health() -> dict:
    """For the super admin's Sports screen: per competition, how the library
    stands — when it last refreshed and whether that worked, how many games it
    holds for this season, and the next game in it."""
    from django.db.models import Max

    from catalog.models import Series
    from matchreader.models import HistoricalMatch

    from .models import FixtureCache, SyncRun
    from .services import competition_for_series

    year = timezone.localdate().year
    now = timezone.now()
    last_runs = {}
    for run in SyncRun.objects.filter(kind=SyncRun.KIND_LIBRARY).order_by("competition", "-started_at"):
        last_runs.setdefault(run.competition, run)
    out = {}
    for series in Series.objects.all():
        comp = competition_for_series(series.name)
        if comp is None:
            continue
        from .services import get_sync_service

        svc = get_sync_service(comp)
        key = svc._scraper_key(series.name)
        cache = FixtureCache.objects.filter(source=svc.SOURCE, series_key=key, season_year=year)
        upcoming, nxt = 0, None
        for payload in cache.values_list("payload", flat=True):
            for r in payload or []:
                k = r.get("kickoff_at")
                if isinstance(k, str):
                    from django.utils.dateparse import parse_datetime
                    k = parse_datetime(k)
                if k and k >= now:
                    upcoming += 1
                    nxt = k if nxt is None or k < nxt else nxt
        run = last_runs.get(comp)
        out[series.id] = {
            "run": run,
            "ok": bool(run and run.ok),
            "refreshed": cache.aggregate(m=Max("fetched_at"))["m"],
            "rounds": cache.count(),
            "played": HistoricalMatch.objects.filter(series=series, season=year).count(),
            "upcoming": upcoming,
            "next": nxt,
        }
    return out
