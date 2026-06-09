from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from zoneinfo import ZoneInfo

import requests
from django.conf import settings
from django.utils.text import slugify
from django.utils import timezone

from catalog.models import Competition
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


def _normalise_team_name(name: str) -> str:
    return slugify(name.replace("&", "and"))


def _resolve_team(competition: Competition, name: str, external_id: str = "") -> Team | None:
    slug = _normalise_team_name(name)
    if external_id:
        t = Team.objects.filter(competition=competition, external_id=external_id).first()
        if t:
            return t
    t = Team.objects.filter(competition=competition, slug=slug).first()
    if t:
        if external_id and not t.external_id:
            t.external_id = external_id
            t.save(update_fields=["external_id"])
        return t
    t = Team.objects.filter(competition=competition, name__iexact=name).first()
    if t and external_id and not t.external_id:
        t.external_id = external_id
        t.save(update_fields=["external_id"])
    return t


def _parse_dt(value: str) -> datetime:
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            dt = datetime.strptime(value, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=SYDNEY)
            return dt.astimezone(timezone.utc)
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
        afl = Competition.objects.get(name="AFL")
        year = org.season.year
        games = self._games(round_number, year)
        if not games:
            return 0
        round_obj, _ = Round.objects.get_or_create(
            org=org, round_number=round_number, competition=afl,
            defaults={
                "lockout_at": _parse_dt(games[0]["date"]),
                "status": "upcoming",
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
            match, _ = Match.objects.update_or_create(
                round=round_obj, external_id=str(g["id"]),
                defaults={
                    "home_team": home, "away_team": away,
                    "kickoff_at": kickoff,
                    "venue": g.get("venue", "") or "",
                },
            )
            n += 1
        return n

    def sync_results(self, *, competition: str, round_number: int, org: Organisation) -> int:
        if competition != "AFL":
            raise SyncError("Squiggle service only handles AFL.")
        year = org.season.year
        games = self._games(round_number, year)
        n = 0
        for g in games:
            hs, as_ = g.get("hscore"), g.get("ascore")
            if hs is None or as_ is None:
                continue
            match = Match.objects.filter(external_id=str(g["id"]), round__org=org).first()
            if not match:
                continue
            record_match_result(match, int(hs), int(as_))
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


def get_sync_service(competition: str) -> DataSyncService:
    comp = competition.upper()
    if comp == "AFL":
        return SquiggleSyncService()
    if comp == "NRL":
        return TheSportsAPISyncService()
    raise SyncError(f"Unsupported competition: {competition}")
