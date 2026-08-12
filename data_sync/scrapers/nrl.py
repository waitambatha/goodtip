"""NRL, NRLW and State of Origin, scraped from nrl.com's own draw page.

Why scraping beats the API here: nrl.com renders its draw from a JSON payload
embedded in the page as a ``q-data`` attribute on ``#vue-draw``. We read the
same structure the site's front-end reads, so this is not selector-matching
against markup that changes with every restyle. It carries everything the sync
needs and more than API-SPORTS offered:

    homeTeam/awayTeam  teamId, nickName, score
    matchState         Pre / FirstHalf / HalfTime / SecondHalf / FullTime
    matchMode          Pre / Live / Post
    clock              kickOffTimeLong (ISO UTC), gameTime ("80:00")
    venue, venueCity, roundTitle, matchCentreUrl

The page also embeds filterCompetitions, filterSeasons and filterRounds, so
the set of rounds is discovered from the site rather than guessed.

ONE PAGE FETCH SERVES ONE ROUND OF ONE COMPETITION, and every org tipping that
competition shares it through the instance cache. Twenty leagues on NRL 2026
round 22 is one request, not twenty.
"""

from __future__ import annotations

import html
import json
import logging
import re
import time
from datetime import datetime, timezone as dt_timezone

import requests
from django.utils import timezone

logger = logging.getLogger(__name__)


# nrl.com competition ids -> the Series name in our catalog. Everything the
# NRL competition bundles is here, which is the point: NRLW and Origin are not
# a separate integration, they are the same page with a different id.
COMPETITION_IDS = {
    "NRL": 111,             # Telstra Premiership
    "NRLW": 161,            # Telstra Women's Premiership
    "STATE OF ORIGIN": 116,  # Ampol State of Origin
}

# matchMode/matchState -> our Match.status. matchState is the finer of the two
# and is preferred; matchMode is the fallback because pre-season and exhibition
# fixtures sometimes carry a mode with no state.
_STATE_TO_STATUS = {
    "pre": "scheduled",
    "firsthalf": "live",
    "halftime": "live",
    "secondhalf": "live",
    "extratime": "live",
    "goldenpoint": "live",
    "fulltime": "complete",
    "postgame": "complete",
    "final": "complete",
}
_MODE_TO_STATUS = {"pre": "scheduled", "live": "live", "post": "complete"}


class NrlScrapeError(Exception):
    pass


class NrlDrawScraper:
    """Fetches and parses one nrl.com draw page, with caching.

    Instantiate once per sync run. The command already holds one service per
    competition per run, so every org tipping the same round is served from
    ``self._cache`` after the first fetch.
    """

    BASE = "https://www.nrl.com/draw/"
    # A real browser UA. nrl.com serves the embedded payload to browsers; a
    # bare python-requests UA is the kind of thing edge rules drop.
    HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-AU,en;q=0.9",
    }
    TIMEOUT = 25

    # The payload lives in an HTML attribute, so it arrives entity-escaped and
    # has to be unescaped before it is JSON. Non-greedy up to the closing quote
    # followed by whitespace: the JSON itself contains no raw double quotes,
    # they are all &quot; at this point.
    _Q_DATA = re.compile(r'q-data="(.*?)"\s', re.S)

    # Seconds between requests. nrl.com starts bouncing unauthenticated
    # traffic to account.nrl.com/authorize when hit faster than this, which is
    # a rate limit rather than a crawl policy: their robots.txt sets no
    # Disallow at all. Pacing is how we stay a well-behaved client of a site
    # that has told us we may read it.
    DELAY = 6.0

    def __init__(self):
        self._cache: dict = {}
        self._session: requests.Session | None = None
        self._last_request = 0.0

    # ---- fetching -------------------------------------------------------

    def _warm(self) -> requests.Session:
        """A browser-shaped session.

        Hitting /draw/ cold, with no cookies, is what gets redirected to the
        login. The homepage sets four cookies and the draw page then serves the
        payload normally, so this does once what any browser does implicitly.
        """
        s = requests.Session()
        s.headers.update(self.HEADERS)
        try:
            s.get("https://www.nrl.com/", timeout=self.TIMEOUT)
        except requests.RequestException as e:
            raise NrlScrapeError(f"could not reach nrl.com: {e}") from e
        return s

    def _throttle(self) -> None:
        wait = self.DELAY - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    def _fetch(self, params: dict, *, retry: bool = True) -> str:
        if self._session is None:
            self._session = self._warm()
        self._throttle()
        try:
            r = self._session.get(self.BASE, params=params, headers=self.HEADERS, timeout=self.TIMEOUT)
            r.raise_for_status()
        except requests.RequestException as e:
            raise NrlScrapeError(f"nrl.com request failed: {e}") from e

        # Bounced to the login: the session went stale or we were paced out.
        # One re-warm and retry, then give up rather than hammering.
        if "account.nrl.com" in r.url or "q-data" not in r.text:
            if retry:
                logger.info("nrl.com redirected to auth; re-warming session and retrying once")
                self._session = self._warm()
                return self._fetch(params, retry=False)
        return r.text

    def _page(self, *, competition_id: int, season: int, round_number: int | None) -> dict:
        key = (competition_id, season, round_number)
        if key in self._cache:
            return self._cache[key]

        params = {"competition": competition_id, "season": season}
        if round_number is not None:
            params["round"] = round_number
        text = self._fetch(params)

        m = self._Q_DATA.search(text)
        if not m:
            # The shape changed, or we were served an interstitial. Either way
            # this is worth saying loudly rather than returning zero fixtures
            # and looking like a quiet round.
            raise NrlScrapeError(
                "nrl.com draw payload not found on the page. The embedded "
                "q-data attribute is missing, which means the page shape "
                "changed or the request was blocked."
            )
        try:
            data = json.loads(html.unescape(m.group(1)))
        except json.JSONDecodeError as e:
            raise NrlScrapeError(f"nrl.com draw payload was not valid JSON: {e}") from e

        self._cache[key] = data
        return data

    # ---- parsing --------------------------------------------------------

    @staticmethod
    def _kickoff(fixture: dict) -> datetime | None:
        raw = (fixture.get("clock") or {}).get("kickOffTimeLong") or fixture.get("kickOffTimeLong")
        if not raw:
            return None
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
        return dt if timezone.is_aware(dt) else dt.replace(tzinfo=dt_timezone.utc)

    @staticmethod
    def _round_of(fixture: dict) -> int | None:
        """Which round this fixture actually belongs to.

        Needed because the round parameter is not honoured by every
        competition. State of Origin returns all three games whatever you ask
        for, so without this every Origin "round" was created holding the whole
        series: game 1, 2 and 3 duplicated into rounds 1, 2 and 3 alike.

        roundTitle is the source ("Round 22", "Game 2"); the match-centre URL
        (.../game-2/...) is the fallback for fixtures that carry no title.
        """
        m = re.search(r"(\d+)", fixture.get("roundTitle") or "")
        if m:
            return int(m.group(1))
        m = re.search(r"/(?:round|game)-(\d+)/", fixture.get("matchCentreUrl") or "")
        return int(m.group(1)) if m else None

    @staticmethod
    def _status(fixture: dict) -> str:
        state = (fixture.get("matchState") or "").replace(" ", "").lower()
        if state in _STATE_TO_STATUS:
            return _STATE_TO_STATUS[state]
        mode = (fixture.get("matchMode") or "").lower()
        return _MODE_TO_STATUS.get(mode, "scheduled")

    @staticmethod
    def _external_id(fixture: dict) -> str:
        """A stable id for the fixture.

        matchCentreUrl is the only genuinely stable identifier on the payload
        (e.g. /draw/nrl-premiership/2026/round-22/cowboys-v-roosters/), so the
        path is the id. Falling back to the team-and-round triple keeps a row
        matchable even if the URL is absent, which happens on some byes and
        exhibition fixtures.
        """
        url = (fixture.get("matchCentreUrl") or "").strip("/")
        if url:
            return url[-100:]
        h = (fixture.get("homeTeam") or {}).get("teamId")
        a = (fixture.get("awayTeam") or {}).get("teamId")
        return f"nrl:{fixture.get('roundTitle','')}:{h}:{a}"[:100]

    def fixtures(self, *, series: str, season: int, round_number: int) -> list[dict]:
        """Normalised fixtures for one round, ready for the sync layer."""
        comp_id = COMPETITION_IDS.get(series.upper())
        if comp_id is None:
            raise NrlScrapeError(
                f"No nrl.com competition id for series {series!r}. "
                f"Known: {', '.join(sorted(COMPETITION_IDS))}."
            )
        data = self._page(competition_id=comp_id, season=season, round_number=round_number)
        out = []
        for f in data.get("fixtures") or []:
            # Trust the fixture's own round over the one we asked for. Where
            # the parameter IS honoured this changes nothing; where it is not,
            # it is the only thing stopping a round from absorbing the whole
            # competition.
            actual = self._round_of(f)
            if actual is not None and actual != round_number:
                continue
            home, away = f.get("homeTeam") or {}, f.get("awayTeam") or {}
            # A fixture with a side still to be decided (finals slots) is
            # published early with no nickname. It cannot be created or
            # matched, so it is skipped rather than crashing the run.
            if not home.get("nickName") or not away.get("nickName"):
                continue
            kickoff = self._kickoff(f)
            if kickoff is None:
                continue
            out.append({
                "external_id": self._external_id(f),
                "home_name": home["nickName"],
                "away_name": away["nickName"],
                "home_external_id": str(home.get("teamId") or ""),
                "away_external_id": str(away.get("teamId") or ""),
                "home_score": home.get("score"),
                "away_score": away.get("score"),
                "kickoff_at": kickoff,
                "venue": f.get("venue") or "",
                "venue_city": f.get("venueCity") or "",
                "status": self._status(f),
                "clock": (f.get("clock") or {}).get("gameTime") or "",
                "period": f.get("matchState") or "",
            })
        return out

    def available_rounds(self, *, series: str, season: int) -> list[int]:
        """Round numbers the site publishes for this competition and season."""
        comp_id = COMPETITION_IDS.get(series.upper())
        if comp_id is None:
            return []
        data = self._page(competition_id=comp_id, season=season, round_number=None)
        rounds = []
        for r in data.get("filterRounds") or []:
            value = r.get("value")
            if isinstance(value, int):
                rounds.append(value)
        if rounds:
            return sorted(rounds)
        # No round filter published at all — State of Origin is a three-game
        # series, not a season, so nrl.com gives it no round picker. Read the
        # rounds off the fixtures themselves instead of reporting none, which
        # is what kept Origin out of discovery entirely.
        derived = {
            self._round_of(f)
            for f in (data.get("fixtures") or [])
        }
        return sorted(r for r in derived if r is not None)

    def current_round(self, *, series: str, season: int) -> int | None:
        """The round nrl.com considers current, in ONE request.

        The draw page with no round parameter returns the current round, and
        every fixture on it carries isCurrentRound. That is the cheap way to
        locate ourselves in the season.
        """
        comp_id = COMPETITION_IDS.get(series.upper())
        if comp_id is None:
            return None
        data = self._page(competition_id=comp_id, season=season, round_number=None)
        for f in data.get("fixtures") or []:
            if f.get("isCurrentRound"):
                title = f.get("roundTitle") or ""
                m = re.search(r"(\d+)", title)
                if m:
                    return int(m.group(1))
        sel = data.get("selectedRoundId")
        return sel if isinstance(sel, int) else None

    def rounds_in_window(self, *, series: str, season: int,
                         horizon_days: int = 21, back_days: int = 3) -> list[int]:
        """Rounds worth syncing right now.

        Derived from the CURRENT round rather than by fetching every round and
        reading its dates. The first version did the latter and it is what got
        us rate-limited: 27 rounds x 3 series is 81 page loads to re-learn that
        round 3 finished in March.

        A round is roughly a week, so the day window converts to a round window
        directly. One page fetch decides the lot.
        """
        current = self.current_round(series=series, season=season)
        if current is None:
            return []
        available = set(self.available_rounds(series=series, season=season))
        lo = current - max(1, round(back_days / 7))
        hi = current + max(1, round(horizon_days / 7))
        return sorted(rn for rn in available if lo <= rn <= hi)
