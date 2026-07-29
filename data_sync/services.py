from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone as dt_timezone
from typing import Protocol
from zoneinfo import ZoneInfo

import requests
from django.conf import settings
from django.utils.text import slugify
from django.utils import timezone

from catalog.models import Competition, Series
from orgs.models import Organisation
from tipping.models import Match, Round, Team
from tipping.services import derive_result, record_match_result


logger = logging.getLogger(__name__)
SYDNEY = ZoneInfo("Australia/Sydney")


class SyncError(Exception):
    pass


class DataSyncService(Protocol):
    def sync_fixtures(self, *, competition: str, round_number: int, org: Organisation) -> int: ...
    def sync_results(self, *, competition: str, round_number: int, org: Organisation) -> int: ...
    def sync_live(self, *, competition: str, round_number: int, org: Organisation) -> int: ...


def _split_period_clock(timestr: str) -> tuple[str, str]:
    """Split a feed's clock string into (period, clock).

    Squiggle words it as "Q3 12:45" mid-quarter, and as a bare phrase at the
    boundaries: "Half Time", "Full Time", "Q2". Anything with a colon in the
    last token is treated as the clock; the rest is the period.
    """
    s = (timestr or "").strip()
    if not s:
        return "", ""
    parts = s.split()
    if len(parts) > 1 and ":" in parts[-1]:
        return " ".join(parts[:-1]), parts[-1]
    if ":" in s:
        return "", s
    return s, ""


# Squiggle names several clubs by their city alone, while the Team table stores
# the full club name. Without this map five of the nine games in an AFL round
# fail to resolve and get skipped, so scores and results never land for them.
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
    slug = slugify(name.replace("&", "and"))
    return TEAM_SLUG_ALIASES.get(slug, slug)


def _resolve_team(series: Series, name: str, external_id: str = "") -> Team | None:
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


def _parse_dt(value: str) -> datetime:
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            dt = datetime.strptime(value, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=SYDNEY)
            # datetime's UTC, not django.utils.timezone's — the latter was
            # removed in Django 5.0 and this runs 5.2, so the old reference
            # raised AttributeError and took every fixtures sync down with it.
            return dt.astimezone(dt_timezone.utc)
        except ValueError:
            continue
    raise SyncError(f"Unparseable datetime: {value}")


class SquiggleSyncService:
    BASE = "https://api.squiggle.com.au"
    HEADERS = {"User-Agent": "GoodTip/1.0 (goodtip.com.au)"}

    def _get(self, params: dict) -> dict:
        try:
            r = requests.get(self.BASE, params=params, headers=self.HEADERS, timeout=15)
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            raise SyncError(f"Squiggle request failed: {e}") from e

    def _games(self, round_number: int, year: int) -> list[dict]:
        data = self._get({"q": "games", "year": year, "round": round_number})
        return data.get("games", [])

    def sync_fixtures(self, *, competition: str, round_number: int, org: Organisation) -> int:
        if competition != "AFL":
            raise SyncError("Squiggle service only handles AFL.")
        afl = Series.objects.get(name="AFL")
        year = org.season.year
        games = self._games(round_number, year)
        if not games:
            return 0
        round_obj, _ = Round.objects.get_or_create(
            org=org, round_number=round_number, series=afl,
            defaults={
                "lockout_at": _parse_dt(games[0]["date"]),
                "status": "upcoming",
                "competition": Competition.for_series(afl, org.season),
            },
        )
        kickoffs = [_parse_dt(g["date"]) for g in games]
        earliest = min(kickoffs)
        if round_obj.lockout_at != earliest:
            round_obj.lockout_at = earliest
            round_obj.save(update_fields=["lockout_at"])
        n = 0
        for g in games:
            home = _resolve_team(afl, g["hteam"], str(g.get("hteamid", "")))
            away = _resolve_team(afl, g["ateam"], str(g.get("ateamid", "")))
            if not home or not away:
                logger.warning("Skip game id=%s: unresolved teams %s/%s", g.get("id"), g["hteam"], g["ateam"])
                continue
            kickoff = _parse_dt(g["date"])
            venue = g.get("venue", "") or ""
            ext = str(g["id"])
            match = _resolve_match(round_obj, ext, home, away)
            if match is None:
                Match.objects.create(
                    round=round_obj, external_id=ext,
                    home_team=home, away_team=away,
                    kickoff_at=kickoff, venue=venue,
                )
            else:
                match.external_id = ext
                match.home_team = home
                match.away_team = away
                match.kickoff_at = kickoff
                match.venue = venue
                match.save(update_fields=[
                    "external_id", "home_team", "away_team", "kickoff_at", "venue",
                ])
            n += 1
        return n

    def sync_live(self, *, competition: str, round_number: int, org: Organisation) -> int:
        """Refresh in-play state: score, quarter, clock, and completion.

        Deliberately does NOT grade tips. Squiggle revises scores while a game
        is in progress, and ``record_match_result`` fires the recap-review flag
        on any change to an already-graded match — so grading mid-game would
        churn recaps and briefly award points on a scoreline that hasn't
        settled. Grading stays with sync_results, once ``complete`` hits 100.
        """
        if competition != "AFL":
            raise SyncError("Squiggle service only handles AFL.")
        afl = Series.objects.get(name="AFL")
        games = self._games(round_number, org.season.year)
        round_obj = Round.objects.filter(
            org=org, round_number=round_number, series=afl,
        ).first()
        if round_obj is None:
            return 0
        now = timezone.now()
        n = 0
        for g in games:
            home = _resolve_team(afl, g["hteam"], str(g.get("hteamid", "")))
            away = _resolve_team(afl, g["ateam"], str(g.get("ateamid", "")))
            if not home or not away:
                continue
            match = _resolve_match(round_obj, str(g["id"]), home, away)
            if not match:
                continue

            pct = int(g.get("complete") or 0)
            period, clock = _split_period_clock(g.get("timestr", ""))
            hs, as_ = g.get("hscore"), g.get("ascore")

            if pct >= 100:
                status = Match.STATUS_COMPLETE
            elif pct > 0:
                status = Match.STATUS_LIVE
            else:
                # Squiggle leaves complete at 0 until the ball is bounced, so
                # kickoff time is the only signal a game has actually started.
                status = Match.STATUS_LIVE if now >= match.kickoff_at else Match.STATUS_SCHEDULED

            match.status = status
            match.period = period[:32]
            match.clock = clock[:16]
            match.progress = max(0, min(100, pct))
            match.live_updated_at = now
            if hs is not None:
                match.home_score = int(hs)
            if as_ is not None:
                match.away_score = int(as_)
            if g.get("venue"):
                match.venue = g["venue"]
            match.save(update_fields=[
                "status", "period", "clock", "progress", "live_updated_at",
                "home_score", "away_score", "venue",
            ])
            n += 1
        return n

    def sync_results(self, *, competition: str, round_number: int, org: Organisation) -> int:
        if competition != "AFL":
            raise SyncError("Squiggle service only handles AFL.")
        afl = Series.objects.get(name="AFL")
        games = self._games(round_number, org.season.year)
        round_obj = Round.objects.filter(
            org=org, round_number=round_number, series=afl,
        ).first()
        if round_obj is None:
            return 0
        n = 0
        for g in games:
            hs, as_ = g.get("hscore"), g.get("ascore")
            if hs is None or as_ is None:
                continue
            # Only grade once the game is actually over. Squiggle revises scores
            # while play is in progress, and grading a moving scoreline would
            # award and retract points — and flag every recap for review each
            # time. In-play numbers are sync_live's job.
            if int(g.get("complete") or 0) < 100:
                continue
            home = _resolve_team(afl, g["hteam"], str(g.get("hteamid", "")))
            away = _resolve_team(afl, g["ateam"], str(g.get("ateamid", "")))
            if not home or not away:
                continue
            match = _resolve_match(round_obj, str(g["id"]), home, away)
            if not match:
                continue
            record_match_result(match, int(hs), int(as_))
            # Grading is the point at which the match is definitively over.
            if match.status != Match.STATUS_COMPLETE:
                match.status = Match.STATUS_COMPLETE
                match.progress = 100
                match.save(update_fields=["status", "progress"])
            n += 1
        return n


class TheSportsAPISyncService:
    BASE = "https://api.thesportsapi.com/v1"

    def __init__(self):
        self.api_key = settings.THESPORTS_API_KEY

    def _require_key(self):
        if not self.api_key:
            raise SyncError("NRL sync unavailable — THESPORTS_API_KEY is not set in .env.")

    def sync_fixtures(self, *, competition: str, round_number: int, org: Organisation) -> int:
        self._require_key()
        # TODO: real TheSports API integration — endpoint paths depend on their docs.
        # When key is wired, fetch fixtures, map to Team via _resolve_team("NRL", ...).
        raise SyncError("TheSports API integration pending — key provided but client not implemented yet.")

    def sync_results(self, *, competition: str, round_number: int, org: Organisation) -> int:
        self._require_key()
        raise SyncError("TheSports API integration pending — key provided but client not implemented yet.")

    def sync_live(self, *, competition: str, round_number: int, org: Organisation) -> int:
        self._require_key()
        raise SyncError("TheSports API integration pending — key provided but client not implemented yet.")


def get_sync_service(competition: str) -> DataSyncService:
    comp = competition.upper()
    if comp == "AFL":
        return SquiggleSyncService()
    if comp == "NRL":
        return TheSportsAPISyncService()
    raise SyncError(f"Unsupported competition: {competition}")
