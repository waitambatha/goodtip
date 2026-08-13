"""Sportradar: fixtures and results for all four launch codes.

Commissioned by the client (GoodTip Sports Data API Reference, 12 Aug 2026)
and verified by direct call, not documentation inference. This replaces the
nrl.com and afl.com.au scrapers as the primary source. Those stay in the tree
deliberately: this is a TRIAL key that stops working on 11 September 2026, and
the week that happens is not the week to start writing a fallback.

Two APIs, one key:

    Australian Rules   /australianrules/trial/v3/en/...
        AFL   sr:competition:656      AFLW  sr:competition:14866
    Rugby League       /rugby-league/trial/v3/en/...
        NRL   sr:competition:294      NRLW  sr:competition:37677

The doc's build pattern is followed exactly: seasons -> schedule for the
season -> each sport_event carries competitors, start_time, venue and (once
played) the score. Everything GoodTip needs is a fixture and a final result.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone as dt_timezone

import requests
from django.conf import settings

logger = logging.getLogger(__name__)


# Series name -> (url package, competition id)
COMPETITIONS = {
    "AFL":             ("australianrules", "sr:competition:656"),
    "AFLW":            ("australianrules", "sr:competition:14866"),
    "NRL":             ("rugby-league",    "sr:competition:294"),
    "NRLW":            ("rugby-league",    "sr:competition:37677"),
}

# Sportradar event status -> our Match.status.
_STATUS = {
    "not_started": "scheduled", "created": "scheduled", "postponed": "scheduled",
    "delayed": "scheduled", "live": "live", "inprogress": "live",
    "halftime": "live", "closed": "complete", "ended": "complete",
    "complete": "complete",
}


class SportradarError(Exception):
    pass


class SportradarClient:
    BASE = "https://api.sportradar.com"
    TIMEOUT = 30
    # Trial keys are rate limited (1 req/sec on the trial tier). Exceeding it
    # returns 429 and, sustained, gets the key throttled — so the pacing is
    # not politeness here, it is the documented limit.
    DELAY = 1.1

    def __init__(self, api_key: str | None = None):
        self.key = api_key or getattr(settings, "SPORTRADAR_API_KEY", "")
        if not self.key:
            raise SportradarError(
                "SPORTRADAR_API_KEY is not set in .env. The key is in the "
                "client's Sports Data API Reference; it is a trial token that "
                "expires 11 Sep 2026."
            )
        self._cache: dict = {}
        self._last = 0.0

    def _get(self, package: str, path: str) -> dict:
        key = (package, path)
        if key in self._cache:
            return self._cache[key]
        wait = self.DELAY - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        url = f"{self.BASE}/{package}/trial/v3/en/{path}"
        try:
            r = requests.get(url, params={"api_key": self.key}, timeout=self.TIMEOUT)
            self._last = time.monotonic()
            if r.status_code == 403:
                raise SportradarError(
                    "Sportradar returned 403. The trial key expired on "
                    "11 Sep 2026 or was revoked — production access is needed."
                )
            if r.status_code == 429:
                raise SportradarError("Sportradar rate limit hit (trial tier is 1 request/second).")
            r.raise_for_status()
            data = r.json()
        except requests.RequestException as e:
            raise SportradarError(f"Sportradar request failed: {e}") from e
        self._cache[key] = data
        return data

    # ---- seasons -------------------------------------------------------

    def season_id(self, series: str, year: int) -> str | None:
        """The season id for a competition and calendar year."""
        pkg, comp = self._competition(series)
        data = self._get(pkg, f"competitions/{comp}/seasons.json")
        for s in data.get("seasons", []):
            start = (s.get("start_date") or "")[:4]
            if start == str(year):
                return s.get("id")
        # Some competitions label a season by its end year instead.
        for s in data.get("seasons", []):
            if str(year) in (s.get("name") or ""):
                return s.get("id")
        return None

    @staticmethod
    def _competition(series: str) -> tuple[str, str]:
        try:
            return COMPETITIONS[series.upper()]
        except KeyError:
            raise SportradarError(
                f"No Sportradar competition for series {series!r}. "
                f"Known: {', '.join(sorted(COMPETITIONS))}."
            ) from None

    # ---- fixtures ------------------------------------------------------

    @staticmethod
    def _kickoff(ev: dict):
        raw = ev.get("start_time")
        if not raw:
            return None
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=dt_timezone.utc)

    def _normalise(self, ev: dict) -> dict | None:
        comps = ev.get("competitors") or []
        home = next((c for c in comps if c.get("qualifier") == "home"), None)
        away = next((c for c in comps if c.get("qualifier") == "away"), None)
        if not home or not away:
            return None
        kickoff = self._kickoff(ev)
        if kickoff is None:
            return None

        status_obj = ev.get("sport_event_status") or {}
        status = _STATUS.get((status_obj.get("status") or "").lower(), "scheduled")
        hs, as_ = status_obj.get("home_score"), status_obj.get("away_score")
        venue = (ev.get("venue") or {})
        rnd = ((ev.get("sport_event_context") or {}).get("round") or {})

        return {
            "external_id": (ev.get("id") or "")[:100],
            "round_number": rnd.get("number"),
            "home_name": home.get("name") or "",
            "away_name": away.get("name") or "",
            "home_external_id": home.get("id") or "",
            "away_external_id": away.get("id") or "",
            "home_score": hs if isinstance(hs, int) else None,
            "away_score": as_ if isinstance(as_, int) else None,
            "kickoff_at": kickoff,
            "venue": venue.get("name") or "",
            "venue_city": venue.get("city_name") or "",
            "status": status,
            "clock": "",
            "period": (status_obj.get("match_status") or "")[:32],
        }

    def season_schedule(self, series: str, year: int) -> list[dict]:
        """Every fixture in a season, normalised. One request, then cached."""
        pkg, _ = self._competition(series)
        sid = self.season_id(series, year)
        if not sid:
            logger.info("Sportradar: no %s season for %s", series, year)
            return []
        # summaries.json, not schedules.json — the latter 404s on every
        # competition. Each entry is {sport_event, sport_event_status},
        # so the status rides along with the fixture and one call gives
        # both the draw and the results.
        data = self._get(pkg, f"seasons/{sid}/summaries.json")
        out = []
        for entry in data.get("summaries", []):
            ev = entry.get("sport_event") or {}
            # The status is a sibling of sport_event, not a child of it.
            ev = dict(ev, sport_event_status=entry.get("sport_event_status") or {})
            row = self._normalise(ev)
            if row:
                out.append(row)
        return out

    def fixtures(self, *, series: str, season: int, round_number: int) -> list[dict]:
        """One round. Filtered from the cached season schedule, so asking for
        twelve rounds costs one request rather than twelve."""
        return [
            r for r in self.season_schedule(series, season)
            if r["round_number"] == round_number
        ]

    def available_rounds(self, *, series: str, season: int) -> list[int]:
        return sorted({
            r["round_number"] for r in self.season_schedule(series, season)
            if isinstance(r["round_number"], int)
        })

    def current_round(self, *, series: str, season: int) -> int | None:
        """The earliest round that still has an unfinished game in it."""
        rows = self.season_schedule(series, season)
        pending = [r["round_number"] for r in rows
                   if r["status"] != "complete" and isinstance(r["round_number"], int)]
        if pending:
            return min(pending)
        done = [r["round_number"] for r in rows if isinstance(r["round_number"], int)]
        return max(done) if done else None

    def rounds_in_window(self, *, series: str, season: int,
                         horizon_days: int = 21, back_days: int = 3) -> list[int]:
        from django.utils import timezone as djtz
        from datetime import timedelta
        now = djtz.now()
        lo, hi = now - timedelta(days=back_days), now + timedelta(days=horizon_days)
        return sorted({
            r["round_number"] for r in self.season_schedule(series, season)
            if isinstance(r["round_number"], int) and lo <= r["kickoff_at"] <= hi
        })
