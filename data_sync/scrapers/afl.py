"""AFL and AFLW, from afl.com.au's own CFS API.

Replaces the Squiggle dependency for two reasons:

  * Squiggle is AFL only. AFLW is one of the four codes GoodTip launches with
    and had no feed at all.
  * Squiggle is a free community API that rate-limits. It began returning 403
    the moment traffic-driven syncing was switched on, and dropped ladder and
    live syncs silently.

This is the same JSON API the afl.com.au front-end uses. Access is a token
handed out by an unauthenticated POST to WMCTok, exactly as the website's own
JavaScript does it, so there is no key to provision and nothing to pay for.

Ids are constructed, not searched:

    competition   CD_C014 (AFL) / CD_C264 (AFLW), the numeric tail is what
                  goes into every other id
    season        CD_S{year}{comp}          e.g. CD_S2026014
    round         CD_R{year}{comp}{round:02}  e.g. CD_R202601423

Venues arrive as ids (CD_V190) and are resolved through one cached lookup.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone as dt_timezone

import requests
from django.utils import timezone

logger = logging.getLogger(__name__)


# Series name -> the numeric tail of its CFS competition id.
COMPETITION_CODES = {
    "AFL": "014",    # Toyota AFL Premiership
    "AFLW": "264",   # NAB AFLW
}

# CFS match status -> our Match.status. UNCONFIRMED_TEAMS is a real fixture
# whose squads are not named yet, which is still a scheduled game to a tipper.
_STATUS = {
    "scheduled": "scheduled",
    "unconfirmed_teams": "scheduled",
    "playing": "live",
    "live": "live",
    "concluded": "complete",
    "final": "complete",
    "complete": "complete",
}


class AflScrapeError(Exception):
    pass


class AflApiScraper:
    BASE = "https://api.afl.com.au/cfs/afl/"
    TOKEN_URL = BASE + "WMCTok"
    HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Origin": "https://www.afl.com.au",
        "Referer": "https://www.afl.com.au/",
        "Accept": "application/json",
    }
    TIMEOUT = 25
    # Far gentler than nrl.com needs: this is a JSON API serving its own app,
    # not an HTML page behind an edge rule. Still paced, because one sync run
    # asks for a dozen rounds and there is no reason to burst them.
    DELAY = 1.0

    def __init__(self):
        self._session: requests.Session | None = None
        self._cache: dict = {}
        self._venues: dict[str, dict] | None = None
        self._last_request = 0.0

    # ---- transport ------------------------------------------------------

    def _auth(self) -> requests.Session:
        s = requests.Session()
        s.headers.update(self.HEADERS)
        try:
            r = s.post(self.TOKEN_URL, timeout=self.TIMEOUT)
            r.raise_for_status()
            token = r.json().get("token")
        except (requests.RequestException, ValueError) as e:
            raise AflScrapeError(f"could not get an afl.com.au API token: {e}") from e
        if not token:
            raise AflScrapeError("afl.com.au returned no API token.")
        s.headers["X-Media-Mis-Token"] = token
        return s

    def _throttle(self) -> None:
        wait = self.DELAY - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    def _get(self, path: str, *, retry: bool = True) -> dict:
        if path in self._cache:
            return self._cache[path]
        if self._session is None:
            self._session = self._auth()
        self._throttle()
        try:
            r = self._session.get(self.BASE + path, timeout=self.TIMEOUT)
        except requests.RequestException as e:
            raise AflScrapeError(f"afl.com.au request failed: {e}") from e

        # Tokens expire. One silent re-auth, then report it properly.
        if r.status_code in (401, 403) and retry:
            logger.info("afl.com.au token rejected; re-authenticating once")
            self._session = self._auth()
            return self._get(path, retry=False)
        if r.status_code == 404:
            # Cache the ABSENCE, not just the hit.
            #
            # A 404 here means "this round does not exist", which is a fact
            # about the season and cannot change during a run. Without caching
            # it, a full-season sweep re-asks for every non-existent round once
            # per organisation: the AFLW draw runs to about round 12, so rounds
            # 13-30 are eighteen guaranteed 404s, times thirty-three leagues,
            # each paying the one-second throttle. That is nine minutes of
            # deliberate waiting to re-learn the same nothing, per series, and
            # it is most of why the first sweep took hours rather than minutes.
            self._cache[path] = {}
            raise AflScrapeError(f"afl.com.au {path} returned HTTP 404")
        if r.status_code != 200:
            raise AflScrapeError(f"afl.com.au {path} returned HTTP {r.status_code}")
        try:
            data = r.json()
        except ValueError as e:
            raise AflScrapeError(f"afl.com.au {path} was not JSON: {e}") from e
        self._cache[path] = data
        return data

    # ---- ids ------------------------------------------------------------

    @staticmethod
    def _comp(series: str) -> str:
        code = COMPETITION_CODES.get(series.upper())
        if code is None:
            raise AflScrapeError(
                f"No afl.com.au competition for series {series!r}. "
                f"Known: {', '.join(sorted(COMPETITION_CODES))}."
            )
        return code

    def _round_id(self, series: str, season: int, round_number: int) -> str:
        return f"CD_R{season}{self._comp(series)}{round_number:02d}"

    # ---- venues ---------------------------------------------------------

    def _venue(self, venue_id: str, *, series: str, season: int) -> tuple[str, str]:
        """(name, city) for a CD_V id.

        Scoped by season id, because the bare /venues endpoint answers for the
        men's premiership whatever you pass it: 18 grounds, while AFLW plays at
        28. Four of the nine AFLW round 1 fixtures were at venues missing from
        that list and came back blank.

        Cached per competition-season, so one lookup serves the whole run.
        """
        if venue_id is None:
            return "", ""
        season_id = f"CD_S{season}{self._comp(series)}"
        if self._venues is None:
            self._venues = {}
        if season_id not in self._venues:
            try:
                rows = self._get(f"venues?seasonId={season_id}").get("venues") or []
            except AflScrapeError:
                rows = []
            self._venues[season_id] = {v.get("venueId") or v.get("id"): v for v in rows}
        v = self._venues[season_id].get(venue_id) or {}
        return v.get("name", "") or "", (v.get("location") or v.get("state") or "")

    # ---- parsing --------------------------------------------------------

    @staticmethod
    def _score(side: dict | None) -> int | None:
        if not side:
            return None
        total = (side.get("matchScore") or {}).get("totalScore")
        return total if isinstance(total, int) else None

    @staticmethod
    def _kickoff(match: dict) -> datetime | None:
        raw = match.get("utcStartTime") or match.get("date")
        if not raw:
            return None
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
        return dt if timezone.is_aware(dt) else dt.replace(tzinfo=dt_timezone.utc)

    # A finals fixture is published before qualifying decides who is in it, and
    # the API fills the slot with the ladder position ("1st", "4th") or the
    # outcome it waits on ("Winner QF1"). These are placeholders, not clubs:
    # they resolve to no team and would be skipped with a warning on every
    # sync from now until September. Recognised and dropped quietly instead.
    _PLACEHOLDER = re.compile(
        r"^(?:\d+(?:st|nd|rd|th)|winner\b.*|loser\b.*|tbc|tba|to be confirmed)$",
        re.I,
    )

    @classmethod
    def _is_placeholder(cls, name: str) -> bool:
        return bool(cls._PLACEHOLDER.match((name or "").strip()))

    def _normalise(self, item: dict, *, series: str, season: int) -> dict | None:
        m = item.get("match") or {}
        home, away = m.get("homeTeam") or {}, m.get("awayTeam") or {}
        if not home.get("name") or not away.get("name"):
            return None
        if self._is_placeholder(home["name"]) or self._is_placeholder(away["name"]):
            return None
        kickoff = self._kickoff(m)
        if kickoff is None:
            return None
        score = item.get("score") or {}
        venue_name, venue_city = self._venue(m.get("venue") or "", series=series, season=season)
        return {
            "external_id": (m.get("matchId") or "")[:100],
            "home_name": home["name"],
            "away_name": away["name"],
            "home_external_id": home.get("teamId") or "",
            "away_external_id": away.get("teamId") or "",
            "home_score": self._score(score.get("homeTeamScore")),
            "away_score": self._score(score.get("awayTeamScore")),
            "kickoff_at": kickoff,
            "venue": venue_name,
            "venue_city": venue_city,
            "status": _STATUS.get((m.get("status") or "").lower(), "scheduled"),
            "clock": "",
            "period": (m.get("status") or "")[:32],
        }

    # ---- public ---------------------------------------------------------

    def fixtures(self, *, series: str, season: int, round_number: int) -> list[dict]:
        rid = self._round_id(series, season, round_number)
        try:
            data = self._get(f"matchItems/round/{rid}")
        except AflScrapeError as e:
            logger.info("afl.com.au %s round %s: %s", series, round_number, e)
            return []
        out = []
        for item in data.get("items") or []:
            row = self._normalise(item, series=series, season=season)
            if row:
                out.append(row)
        return out

    def current_round(self, *, series: str, season: int) -> int | None:
        """The round the API considers current, in one request.

        matchItems with no round returns it, and the roundId carries the
        number in its last two characters.
        """
        try:
            data = self._get("matchItems")
        except AflScrapeError:
            return None
        m = re.match(r"CD_R(\d{4})(\d{3})(\d+)$", data.get("roundId") or "")
        if not m:
            return None
        # The default endpoint answers for the men's premiership. AFLW runs on
        # its own calendar, so its current round is found from its own draw
        # rather than borrowed from this one.
        if self._comp(series) != m.group(2):
            return self._current_round_by_scan(series=series, season=season)
        return int(m.group(3))

    def _current_round_by_scan(self, *, series: str, season: int) -> int | None:
        """First round with a game that has not finished, else the last round.

        Costs a few requests, so it is only used for a competition the default
        endpoint cannot answer for.
        """
        last_with_games = None
        for rn in range(1, 31):
            games = self.fixtures(series=series, season=season, round_number=rn)
            if not games:
                continue
            last_with_games = rn
            if any(g["status"] != "complete" for g in games):
                return rn
        return last_with_games

    def available_rounds(self, *, series: str, season: int) -> list[int]:
        """Rounds that actually have fixtures.

        The API has no round index, so this probes. Cheap in practice because
        every response is cached for the run and rounds_in_window narrows the
        range before anyone asks.
        """
        found = []
        for rn in range(1, 31):
            if self.fixtures(series=series, season=season, round_number=rn):
                found.append(rn)
        return found

    def rounds_in_window(self, *, series: str, season: int,
                         horizon_days: int = 21, back_days: int = 3) -> list[int]:
        """Rounds worth syncing now, derived from the current round.

        Same approach as the NRL scraper and for the same reason: probing all
        30 rounds to read their dates is how you get rate-limited. A round is
        about a week, so the day window converts directly.
        """
        current = self.current_round(series=series, season=season)
        if current is None:
            return []
        lo = max(1, current - max(1, round(back_days / 7)))
        hi = current + max(1, round(horizon_days / 7))
        return [rn for rn in range(lo, hi + 1)
                if self.fixtures(series=series, season=season, round_number=rn)]
