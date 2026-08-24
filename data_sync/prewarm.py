"""Fetching fixtures ahead of the organisation that will need them.

A ``Round`` carries an ``org`` FK, so each organisation holds its own copy of
the draw and a newly created one has nothing until a sync writes it. Doing that
sync at creation meant the person landed on a dashboard with no games and had
to wait out a scrape they never asked for.

The scrape does not actually need the organisation. The feeds are asked for a
``(series, season, round)``; ``org`` only says which year to ask about and
which rows to write under. So the slow half can happen BEFORE the org exists —
which is what this module does. The wizard calls ``prewarm_fixtures`` the
moment the competitions are chosen at step four, and by the time the person has
filled in the charity step and read the review step, the pages are sitting in
FixtureCache. Creation then writes the org's rounds straight from them.

Nothing here invents or copies a fixture: what lands is what the feed said,
fetched once instead of once per organisation.
"""
from __future__ import annotations

import logging

from django.core.serializers.json import DjangoJSONEncoder
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .models import FixtureCache

logger = logging.getLogger(__name__)

# Row keys holding a datetime, which JSON has no native type for.
_DATETIME_KEYS = ("kickoff_at",)


def _encode(rows: list[dict]) -> list[dict]:
    return [
        {k: (v.isoformat() if k in _DATETIME_KEYS and hasattr(v, "isoformat") else v)
         for k, v in row.items()}
        for row in rows
    ]


def _decode(rows: list[dict]) -> list[dict]:
    out = []
    for row in rows:
        row = dict(row)
        for k in _DATETIME_KEYS:
            if isinstance(row.get(k), str):
                row[k] = parse_datetime(row[k])
        out.append(row)
    return out


def fixture_cache_get(source, series_key, season_year, round_number):
    """A still-fresh copy of this feed page, or None to go and fetch it."""
    try:
        row = FixtureCache.objects.filter(
            source=source, series_key=series_key,
            season_year=season_year, round_number=round_number,
        ).first()
    except Exception:  # noqa: BLE001 — a cache miss is always survivable
        logger.exception("fixture cache read failed")
        return None
    if row is None or not row.is_fresh:
        return None
    return _decode(row.payload or [])


def fixture_cache_put(source, series_key, season_year, round_number, rows) -> None:
    """Keep this feed page for the next organisation that wants it."""
    try:
        FixtureCache.objects.update_or_create(
            source=source, series_key=series_key,
            season_year=season_year, round_number=round_number,
            defaults={"payload": _encode(rows or []), "fetched_at": timezone.now()},
        )
    except Exception:  # noqa: BLE001 — caching must never break a real sync
        logger.exception("fixture cache write failed")


def prewarm_fixtures(competitions, season_year: int) -> int:
    """Fetch the draw for these competitions so it is ready before it is asked for.

    Returns the number of feed pages warmed. Best-effort throughout: this runs
    off the request thread purely to make a later creation fast, so a feed
    being down costs a slower creation rather than an error anyone sees.
    """
    from .services import competition_for_series, get_sync_service, SyncError

    names = {
        key
        for comp in competitions
        for s in comp.series.all()
        if (key := competition_for_series(s.name)) is not None
    }

    warmed = 0
    for comp_name in sorted(names):
        try:
            svc = get_sync_service(comp_name)
        except SyncError:
            continue
        discover = getattr(svc, "discover_rounds", None)
        if discover is None:
            continue
        try:
            # full_season so the whole draw is warmed, not just the rolling
            # window — a comp created today still wants the rounds already
            # played, which is what its ladder and history are built from.
            numbers = svc.discover_rounds_for_season(comp_name, season_year)
        except Exception:  # noqa: BLE001
            logger.exception("prewarm discover failed for %s", comp_name)
            continue
        for n in numbers:
            try:
                svc.warm_round(comp_name, n, season_year)
                warmed += 1
            except Exception:  # noqa: BLE001
                logger.exception("prewarm %s round %s failed", comp_name, n)
    return warmed
