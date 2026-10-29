"""ESPN's public scoreboard feeds — the fixtures and results for every sport
beyond the AFL and the NRL (client, 27 Oct 2026: "lets add more sports...
we will need a stable free database source").

WHERE IT COMES FROM. site.api.espn.com serves the JSON behind espn.com's own
scoreboards: free, no key, and one shape for every sport —
``/apis/site/v2/sports/<sport>/<league>/scoreboard?dates=YYYYMM`` returns that
month's games, with both sides, the kick-off, the venue, the score and the
state (pre / in / post). It is UNOFFICIAL: ESPN could change it without
notice. That is why every run is recorded against SyncRun and shows on the
Services board — a change shows up there as Failing on the day it happens.

ROUNDS ARE WEEKS. Most of these leagues have no rounds — the NBA plays every
night — and the ones that do (matchdays) are not in this feed. So a round is
a calendar week, Monday to Sunday in the project's time zone, numbered from
the first week of the year: round 1 is the week holding 1 January. Matches
still lock one by one at their own kick-off (tipping.Match.is_locked), so a
week-long round never shuts a Sunday game on Monday.

The reader mirrors the AFL / NRL scrapers' interface — ``fixtures``,
``available_rounds``, ``rounds_in_window`` — so the sync machinery in
data_sync.services runs it unchanged.
"""
from __future__ import annotations

import json
import logging
import urllib.request
from datetime import date, datetime, timedelta

from django.utils import timezone
from django.utils.dateparse import parse_datetime

logger = logging.getLogger(__name__)

BASE = "https://site.api.espn.com/apis/site/v2/sports"

# Catalog Series name -> ESPN (sport, league). A competition missing from here
# has no free source; the Sports screen says so.
LEAGUES = {
    "English Premier League": ("soccer", "eng.1"),
    "La Liga": ("soccer", "esp.1"),
    "Serie A": ("soccer", "ita.1"),
    "Bundesliga": ("soccer", "ger.1"),
    "UEFA Champions League": ("soccer", "uefa.champions"),
    "A-League Men": ("soccer", "aus.1"),
    "A-League Women": ("soccer", "aus.w.1"),
    "Women's Super League": ("soccer", "eng.w.1"),
    "NBA": ("basketball", "nba"),
    "WNBA": ("basketball", "wnba"),
    "NBL": ("basketball", "nbl"),
    "NFL": ("football", "nfl"),
    "NHL": ("hockey", "nhl"),
    "MLB": ("baseball", "mlb"),
    "Super Rugby Pacific": ("rugby", "242041"),
    "Six Nations": ("rugby", "180659"),
    "Big Bash League": ("cricket", "8044"),
}

_STATE = {"pre": "scheduled", "in": "live", "post": "complete"}


class EspnError(Exception):
    loud = False


def week_start(season: int) -> date:
    """Monday of round 1: the week holding 1 January."""
    jan1 = date(season, 1, 1)
    return jan1 - timedelta(days=jan1.weekday())


def round_of(kickoff: datetime, season: int) -> int:
    local = timezone.localtime(kickoff).date()
    return (local - week_start(season)).days // 7 + 1


def week_range(season: int, round_number: int) -> tuple[date, date]:
    start = week_start(season) + timedelta(days=7 * (round_number - 1))
    return start, start + timedelta(days=6)


class EspnScraper:
    """One per sync run: months already fetched are kept for the run."""

    timeout = 15

    def __init__(self):
        self._months: dict[tuple[str, str], list[dict]] = {}

    # ---- fetching -------------------------------------------------------

    def _get(self, url: str) -> dict:
        req = urllib.request.Request(url, headers={
            # A URL in the agent string is refused (403); the bare name is not.
            "User-Agent": "GoodTip/1.0",
            "Accept": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310 — fixed https host
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    import gzip
                    raw = gzip.decompress(raw)
        except Exception as e:  # noqa: BLE001 — network faults become one error type
            err = EspnError(f"ESPN did not answer: {e}")
            err.loud = True
            raise err from e
        try:
            return json.loads(raw)
        except ValueError as e:
            err = EspnError("ESPN answered with something that is not JSON")
            err.loud = True
            raise err from e

    def _month(self, series: str, year: int, month: int) -> list[dict]:
        if series not in LEAGUES:
            raise EspnError(f"No ESPN feed for {series}")
        key = (series, f"{year}{month:02d}")
        if key not in self._months:
            sport, league = LEAGUES[series]
            data = self._get(f"{BASE}/{sport}/{league}/scoreboard?dates={key[1]}&limit=1000")
            self._months[key] = [r for r in (self._row(e) for e in data.get("events") or []) if r]
        return self._months[key]

    def _months_between(self, first: date, last: date):
        y, m = first.year, first.month
        while (y, m) <= (last.year, last.month):
            yield y, m
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)

    def _between(self, series: str, first: date, last: date) -> list[dict]:
        rows, seen = [], set()
        for y, m in self._months_between(first, last):
            for r in self._month(series, y, m):
                # A week can span two months; a game is still one game.
                if r["external_id"] not in seen:
                    seen.add(r["external_id"])
                    rows.append(r)
        return [r for r in rows if first <= timezone.localtime(r["kickoff_at"]).date() <= last]

    # ---- one event -------------------------------------------------------

    @staticmethod
    def _score(raw):
        try:
            return int(float(raw))
        except (TypeError, ValueError):
            return None

    def _row(self, event: dict) -> dict | None:
        comp = (event.get("competitions") or [{}])[0]
        sides = {c.get("homeAway"): c for c in comp.get("competitors") or []}
        home, away = sides.get("home"), sides.get("away")
        kickoff = parse_datetime((event.get("date") or "").replace("Z", "+00:00"))
        if not home or not away or kickoff is None:
            return None
        status = (comp.get("status") or event.get("status") or {}).get("type") or {}
        state = _STATE.get(status.get("state"), "scheduled")
        if "POSTPONED" in (status.get("name") or "") or "CANCELED" in (status.get("name") or ""):
            state = "postponed"
        venue = comp.get("venue") or {}

        def team(c):
            t = c.get("team") or {}
            return {
                "name": t.get("displayName") or t.get("name") or "",
                "id": str(t.get("id") or ""),
                "logo": t.get("logo") or ((t.get("logos") or [{}])[0].get("href") or ""),
                "short": t.get("abbreviation") or "",
            }

        h, a = team(home), team(away)
        played = state in ("live", "complete")
        return {
            # The LEAGUE's own season — 2026 is 2026-27 for the Premier League,
            # which runs August to May across two calendar years. Results are
            # filed under it so a ladder is one real season, not two halves.
            "league_season": (event.get("season") or {}).get("year"),
            "external_id": f"espn:{event.get('id')}",
            "home_name": h["name"], "away_name": a["name"],
            "home_external_id": f"espn:{h['id']}" if h["id"] else "",
            "away_external_id": f"espn:{a['id']}" if a["id"] else "",
            "home_logo": h["logo"], "away_logo": a["logo"],
            "home_score": self._score(home.get("score")) if played else None,
            "away_score": self._score(away.get("score")) if played else None,
            "kickoff_at": kickoff,
            "venue": (venue.get("fullName") or "")[:200],
            "venue_city": ((venue.get("address") or {}).get("city") or "")[:100],
            "status": state,
            "clock": (status.get("detail") and (comp.get("status") or {}).get("displayClock")) or "",
            "period": (status.get("shortDetail") or "")[:32],
        }

    # ---- the scraper interface ----------------------------------------

    def fixtures(self, *, series: str, season: int, round_number: int) -> list[dict]:
        first, last = week_range(season, round_number)
        return self._between(series, first, last)

    def available_rounds(self, *, series: str, season: int) -> list[int]:
        rows = self._between(series, date(season, 1, 1), date(season, 12, 31))
        return sorted({round_of(r["kickoff_at"], season) for r in rows})

    def rounds_in_window(self, *, series: str, season: int, horizon_days: int = 21,
                         back_days: int = 3) -> list[int]:
        today = timezone.localdate()
        first = max(today - timedelta(days=back_days), date(season, 1, 1))
        last = min(today + timedelta(days=horizon_days), date(season, 12, 31))
        if first > last:
            return []
        rows = self._between(series, first, last)
        return sorted({round_of(r["kickoff_at"], season) for r in rows})
