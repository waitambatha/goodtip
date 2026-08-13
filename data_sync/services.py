from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone as dt_timezone
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
    # Which rounds the feed has, independent of what exists locally. This is
    # what lets a newly published round be found at all.
    def discover_rounds(self, *, competition: str, org: Organisation) -> list[int]: ...
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
    # Squiggle publishes a fixture before both sides are known: finals slots and
    # the pre-season round arrive with hteam/ateam as null, and the draw fills
    # them in as qualifying games are played. That is normal feed data, not a
    # fault, so the name has to be allowed to be missing rather than crashing
    # the whole sync run on the first such row.
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


class SquiggleSyncService:
    BASE = "https://api.squiggle.com.au"
    HEADERS = {"User-Agent": "GoodTip/1.0 (goodtip.com.au)"}

    def __init__(self):
        # Responses memoised for the life of this instance. The draw is keyed on
        # (year, round) and nothing else — it does not vary by organisation — so
        # 25 leagues all tipping AFL 2026 were making 25 identical requests per
        # tick, and the same again for every round discovered. One sync of every
        # league was well over a hundred calls to a free community API for a
        # handful of distinct answers. The command holds one service per
        # competition per run, so the first league pays for the fetch and the
        # rest are served from here.
        self._cache: dict = {}

    def _get(self, params: dict) -> dict:
        key = tuple(sorted(params.items()))
        if key in self._cache:
            return self._cache[key]
        try:
            r = requests.get(self.BASE, params=params, headers=self.HEADERS, timeout=15)
            r.raise_for_status()
            data = r.json()
        except requests.RequestException as e:
            raise SyncError(f"Squiggle request failed: {e}") from e
        self._cache[key] = data
        return data

    def _games(self, round_number: int, year: int) -> list[dict]:
        data = self._get({"q": "games", "year": year, "round": round_number})
        return data.get("games", [])

    def discover_rounds(self, *, competition: str, org: Organisation,
                        horizon_days: int = 21, back_days: int = 3) -> list[int]:
        """Round numbers the FEED is publishing, regardless of what we hold.

        Everything else in this module refreshes rounds that already exist
        locally, which cannot answer "a new game was announced". Nothing local
        is consulted here: the season is fetched from Squiggle in one request
        and the rounds are read off the draw, so a round nobody has ever created
        is still found.

        Narrowed to a window around now — a whole season is ~25 rounds and
        syncing every one of them nightly is a lot of requests to re-learn that
        Round 3 finished in March. back_days keeps a just-finished round in
        scope so its final scores still land.
        """
        if competition != "AFL":
            raise SyncError("Squiggle service only handles AFL.")
        year = org.season.year
        data = self._get({"q": "games", "year": year})
        now = timezone.now()
        lo = now - timedelta(days=back_days)
        hi = now + timedelta(days=horizon_days)
        rounds = set()
        for g in data.get("games", []):
            if not g.get("date") or g.get("round") is None:
                continue
            try:
                when = _parse_dt(g["date"])
            except SyncError:
                continue
            if lo <= when <= hi:
                rounds.add(int(g["round"]))
        return sorted(rounds)

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
        _ensure_round_competition(round_obj, afl, org)
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


    def sync_ladder(self, *, competition: str, org: Organisation, **kw) -> int:
        """Refresh the competition ladder for this season.

        Takes an org only to read its season — the ladder itself is per
        (series, season) and shared, so syncing ten leagues on AFL 2026 writes
        the same 18 rows once, not ten times. The response is memoised on the
        instance, so the extra calls do not even reach Squiggle.
        """
        if competition != "AFL":
            raise SyncError("Squiggle service only handles AFL.")
        from tipping.models import LadderEntry

        afl = Series.objects.get(name="AFL")
        season = org.season
        rows = self._get({"q": "standings", "year": season.year}).get("standings", [])
        n = 0
        for row in rows:
            team = _resolve_team(afl, row.get("name", ""), str(row.get("id", "")))
            if not team:
                logger.warning("Ladder: unresolved team %r", row.get("name"))
                continue
            LadderEntry.objects.update_or_create(
                series=afl, season=season, team=team,
                defaults={
                    "rank": int(row.get("rank") or 0),
                    "played": int(row.get("played") or 0),
                    "wins": int(row.get("wins") or 0),
                    "losses": int(row.get("losses") or 0),
                    "draws": int(row.get("draws") or 0),
                    "points": int(row.get("pts") or 0),
                    "percentage": float(row.get("percentage") or 0),
                    "points_for": int(row.get("for") or 0),
                    "points_against": int(row.get("against") or 0),
                },
            )
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


class ApiSportsRugbySyncService:
    """NRL fixtures, live scores and results from API-SPORTS.

    Replaces a stub written against ``api.thesportsapi.com``, a host with no DNS
    record — so the previous client could never have worked, and every NRL sync
    failed with "integration pending" regardless of configuration.

    Verified directly against the live API: the base URL, the ``x-apisports-key``
    header, and the envelope every response arrives in::

        {"get":…, "parameters":…, "errors":{…}, "results":N, "paging":…, "response":[…]}

    Errors come back inside a 200 or 403 body rather than as an HTTP status
    alone — an unauthenticated call returns ``errors.token``, not a bare 401 —
    so ``_get`` inspects the body instead of trusting ``raise_for_status``.

    Field access on individual games is deliberately defensive (``.get`` with
    fallbacks, several accepted spellings for the round). The envelope is
    confirmed; the per-game keys are from API-SPORTS' documented rugby schema
    and could not be checked against a real payload because no key was
    available. ``manage.py sync_matches --probe-nrl`` dumps one raw game so the
    mapping can be confirmed the moment a key exists.
    """

    BASE = "https://v1.rugby.api-sports.io"
    # NRL round labels arrive as free text: "Regular Season - 22", "Round 22",
    # or bare "22" depending on competition stage.
    ROUND_RE = re.compile(r"(\d+)\s*$")

    def __init__(self):
        self.api_key = getattr(settings, "APISPORTS_KEY", "") or ""
        self._league_id = getattr(settings, "APISPORTS_NRL_LEAGUE_ID", "") or ""

    def _require_key(self):
        if not self.api_key:
            raise SyncError(
                "NRL sync unavailable — APISPORTS_KEY is not set in .env. "
                "Get one at https://dashboard.api-football.com (same account "
                "covers rugby)."
            )

    def _get(self, path: str, params: dict) -> list:
        self._require_key()
        try:
            r = requests.get(
                f"{self.BASE}/{path}",
                params=params,
                headers={"x-apisports-key": self.api_key,
                         "User-Agent": "GoodTip/1.0 (goodtip.com.au)"},
                timeout=15,
            )
            payload = r.json()
        except ValueError as e:
            raise SyncError(f"API-SPORTS returned non-JSON ({r.status_code}).") from e
        except requests.RequestException as e:
            raise SyncError(f"API-SPORTS request failed: {e}") from e

        # errors is {} when fine, and either a dict of reasons or a list when not.
        errs = payload.get("errors") or {}
        if errs:
            detail = "; ".join(f"{k}: {v}" for k, v in errs.items()) if isinstance(errs, dict) else str(errs)
            raise SyncError(f"API-SPORTS error: {detail}")
        return payload.get("response") or []

    def _nrl_league_id(self) -> str:
        """The NRL's id in the rugby catalogue, looked up once if not pinned."""
        if self._league_id:
            return self._league_id
        for row in self._get("leagues", {"search": "NRL"}):
            name = (row.get("name") or "").upper()
            country = ((row.get("country") or {}).get("name") or "").upper()
            if "NRL" in name or ("NATIONAL RUGBY LEAGUE" in name and "AUSTRALIA" in country):
                self._league_id = str(row.get("id"))
                logger.info("Resolved NRL league id=%s (%s). Pin it as "
                            "APISPORTS_NRL_LEAGUE_ID to save a request per sync.",
                            self._league_id, row.get("name"))
                return self._league_id
        raise SyncError("Could not find the NRL in API-SPORTS' league list.")

    def _round_number(self, game: dict):
        """Pull an int round out of whatever the feed labelled the stage."""
        for key in ("week", "round", "stage"):
            raw = game.get(key)
            if raw is None:
                continue
            if isinstance(raw, int):
                return raw
            m = self.ROUND_RE.search(str(raw))
            if m:
                return int(m.group(1))
        return None

    def _games(self, org: Organisation, round_number: int | None = None) -> list:
        params = {"league": self._nrl_league_id(), "season": org.season.year}
        games = self._get("games", params)
        if round_number is None:
            return games
        return [g for g in games if self._round_number(g) == round_number]

    def _teams(self, game: dict):
        nrl = Series.objects.get(name="NRL")
        t = game.get("teams") or {}
        h, a = t.get("home") or {}, t.get("away") or {}
        home = _resolve_team(nrl, h.get("name") or "", str(h.get("id") or ""))
        away = _resolve_team(nrl, a.get("name") or "", str(a.get("id") or ""))
        return nrl, home, away

    @staticmethod
    def _scores(game: dict):
        s = game.get("scores") or {}
        h, a = s.get("home"), s.get("away")
        # Some stages nest the total under a dict rather than giving an int.
        if isinstance(h, dict):
            h = h.get("total")
        if isinstance(a, dict):
            a = a.get("total")
        return h, a

    def discover_rounds(self, *, competition: str, org: Organisation,
                        horizon_days: int = 21, back_days: int = 3) -> list[int]:
        if competition != "NRL":
            raise SyncError("API-SPORTS rugby service handles NRL only.")
        now = timezone.now()
        lo, hi = now - timedelta(days=back_days), now + timedelta(days=horizon_days)
        rounds = set()
        for g in self._games(org):
            n = self._round_number(g)
            if n is None or not g.get("date"):
                continue
            try:
                when = _parse_dt(str(g["date"]).replace("Z", "+00:00"))
            except SyncError:
                continue
            if lo <= when <= hi:
                rounds.add(n)
        return sorted(rounds)

    def sync_ladder(self, *, competition: str, org: Organisation, **kw) -> int:
        self._require_key()
        raise SyncError("NRL ladder pending — API-SPORTS client not verified against a live key yet.")

    def sync_fixtures(self, *, competition: str, round_number: int, org: Organisation) -> int:
        if competition != "NRL":
            raise SyncError("API-SPORTS rugby service handles NRL only.")
        games = self._games(org, round_number)
        if not games:
            return 0
        nrl = Series.objects.get(name="NRL")
        kickoffs = []
        for g in games:
            try:
                kickoffs.append(_parse_dt(str(g["date"]).replace("Z", "+00:00")))
            except (KeyError, SyncError):
                continue
        if not kickoffs:
            return 0
        round_obj, _ = Round.objects.get_or_create(
            org=org, round_number=round_number, series=nrl,
            defaults={
                "lockout_at": min(kickoffs),
                "status": "upcoming",
                "competition": Competition.for_series(nrl, org.season),
            },
        )
        if round_obj.lockout_at != min(kickoffs):
            round_obj.lockout_at = min(kickoffs)
            round_obj.save(update_fields=["lockout_at"])
        _ensure_round_competition(round_obj, nrl, org)

        n = 0
        for g in games:
            _, home, away = self._teams(g)
            if not home or not away:
                logger.warning("Skip NRL game id=%s: unresolved teams", g.get("id"))
                continue
            try:
                kickoff = _parse_dt(str(g["date"]).replace("Z", "+00:00"))
            except (KeyError, SyncError):
                continue
            venue = ((g.get("venue") or {}).get("name")
                     if isinstance(g.get("venue"), dict) else g.get("venue")) or ""
            ext = str(g.get("id") or "")
            match = _resolve_match(round_obj, ext, home, away)
            if match is None:
                Match.objects.create(
                    round=round_obj, external_id=ext,
                    home_team=home, away_team=away,
                    kickoff_at=kickoff, venue=venue,
                )
            else:
                match.external_id = ext
                match.home_team, match.away_team = home, away
                match.kickoff_at, match.venue = kickoff, venue
                match.save(update_fields=[
                    "external_id", "home_team", "away_team", "kickoff_at", "venue",
                ])
            n += 1
        return n

    # API-SPORTS status short codes. NS is scheduled; FT/AET/AWD/WO are over;
    # anything else that is not postponed/cancelled means the ball is in play.
    FINISHED = {"FT", "AET", "AWD", "WO"}
    NOT_STARTED = {"NS", "TBD", "PST", "CANC", "ABD", "SUSP", "INTR"}

    def sync_live(self, *, competition: str, round_number: int, org: Organisation) -> int:
        """In-play score, period and clock. Does NOT grade — see sync_results."""
        if competition != "NRL":
            raise SyncError("API-SPORTS rugby service handles NRL only.")
        nrl = Series.objects.get(name="NRL")
        round_obj = Round.objects.filter(
            org=org, round_number=round_number, series=nrl,
        ).first()
        if round_obj is None:
            return 0
        now = timezone.now()
        n = 0
        for g in self._games(org, round_number):
            _, home, away = self._teams(g)
            if not home or not away:
                continue
            match = _resolve_match(round_obj, str(g.get("id") or ""), home, away)
            if not match:
                continue
            st = g.get("status") or {}
            short = (st.get("short") or "").upper()
            long_ = st.get("long") or ""

            if short in self.FINISHED:
                status, pct = Match.STATUS_COMPLETE, 100
            elif short in self.NOT_STARTED:
                # Mirrors the AFL path: before kickoff the feed says nothing
                # useful, so the clock is the only honest signal.
                status = Match.STATUS_LIVE if now >= match.kickoff_at else Match.STATUS_SCHEDULED
                pct = 0
            else:
                status, pct = Match.STATUS_LIVE, 50

            hs, as_ = self._scores(g)
            match.status = status
            match.period = (long_ or short)[:32]
            match.clock = str(g.get("timer") or "")[:16]
            match.progress = pct
            match.live_updated_at = now
            if hs is not None:
                match.home_score = int(hs)
            if as_ is not None:
                match.away_score = int(as_)
            match.save(update_fields=[
                "status", "period", "clock", "progress", "live_updated_at",
                "home_score", "away_score",
            ])
            n += 1
        return n

    def sync_results(self, *, competition: str, round_number: int, org: Organisation) -> int:
        if competition != "NRL":
            raise SyncError("API-SPORTS rugby service handles NRL only.")
        nrl = Series.objects.get(name="NRL")
        round_obj = Round.objects.filter(
            org=org, round_number=round_number, series=nrl,
        ).first()
        if round_obj is None:
            return 0
        n = 0
        for g in self._games(org, round_number):
            short = ((g.get("status") or {}).get("short") or "").upper()
            # Only grade a finished game — the same rule as AFL. Grading a live
            # scoreline awards and retracts points and re-flags recaps.
            if short not in self.FINISHED:
                continue
            hs, as_ = self._scores(g)
            if hs is None or as_ is None:
                continue
            _, home, away = self._teams(g)
            if not home or not away:
                continue
            match = _resolve_match(round_obj, str(g.get("id") or ""), home, away)
            if not match:
                continue
            record_match_result(match, int(hs), int(as_))
            if match.status != Match.STATUS_COMPLETE:
                match.status = Match.STATUS_COMPLETE
                match.progress = 100
                match.save(update_fields=["status", "progress"])
            n += 1
        return n


# The old name, kept so any import elsewhere still resolves.
TheSportsAPISyncService = ApiSportsRugbySyncService


def get_sync_service(competition: str) -> DataSyncService:
    """The provider for a competition.

    Sportradar first: it is the source the client commissioned, it covers all
    four launch codes from one key, and it is a commercial feed rather than a
    site we read politely. The scrapers stay as the fallback because the key
    is a TRIAL that stops on 11 Sep 2026 — the week that happens is not the
    week to start writing a replacement.
    """
    comp = competition.upper()
    if getattr(settings, "SPORTRADAR_API_KEY", "") and comp in SportradarSyncService.SERIES_FOR:
        try:
            return SportradarSyncService(comp)
        except Exception:                       # noqa: BLE001
            logger.exception("Sportradar unavailable for %s; falling back to scraping", comp)
    if comp == "AFL":
        # Scraped from afl.com.au, not Squiggle. Squiggle is AFL only, so AFLW
        # had no feed at all, and it rate-limits: it began returning 403 within
        # minutes of traffic-driven syncing being enabled. SquiggleSyncService
        # is still used for the ladder and can be swapped back in here.
        return AflScrapeSyncService()
    if comp == "NRL":
        # Scraped from nrl.com, not API-SPORTS. The API path needed a paid key
        # that was never issued, so every NRL sync in this project's history
        # failed with "APISPORTS_KEY is not set" and no NRL org ever had a
        # fixture. ApiSportsRugbySyncService is kept below for reference and
        # can be swapped back in by changing this one line if a key appears.
        return NrlScrapeSyncService()
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


class NrlScrapeSyncService:
    """NRL, NRLW and State of Origin, from nrl.com's own draw page.

    Replaces ApiSportsRugbySyncService, which could never run: the original
    host had no DNS record, and its replacement needs a paid key that was
    never issued. Every NRL sync in this project's history has failed with
    "APISPORTS_KEY is not set".

    One service instance per sync run holds one scraper, so its page cache and
    its request pacing are shared across every org tipping the same round.

    ``competition`` here is the catalog Competition name ("NRL"), while the
    scraper works in Series ("NRL", "NRLW", "State of Origin"). One page load
    per series per round covers all of them, which is why every method loops
    the series rather than taking one.
    """

    #: Catalog Series this service covers, in the order they are synced.
    SERIES = ["NRL", "NRLW", "State of Origin"]

    def __init__(self):
        from .scrapers.nrl import NrlDrawScraper
        self._scraper = NrlDrawScraper()

    def _check(self, competition: str) -> None:
        if competition != "NRL":
            raise SyncError("The nrl.com scraper handles the NRL competition only.")

    def _series_rows(self, series_name: str, round_number: int, year: int) -> tuple[Series | None, list[dict]]:
        from .scrapers.nrl import NrlScrapeError
        series = Series.objects.filter(name=series_name).first()
        if series is None:
            return None, []
        try:
            rows = self._scraper.fixtures(
                series=series_name.upper(), season=year, round_number=round_number,
            )
        except NrlScrapeError as e:
            # One series failing (Origin has no round 24) must not abort the
            # others, which are the ones anybody is tipping.
            logger.info("nrl.com %s round %s: %s", series_name, round_number, e)
            return series, []
        return series, rows

    def discover_rounds(self, *, competition: str, org: Organisation,
                        horizon_days: int = 21, back_days: int = 3) -> list[int]:
        self._check(competition)
        from .scrapers.nrl import NrlScrapeError
        found: set[int] = set()
        for name in self.SERIES:
            try:
                found.update(self._scraper.rounds_in_window(
                    series=name.upper(), season=org.season.year,
                    horizon_days=horizon_days, back_days=back_days,
                ))
            except NrlScrapeError as e:
                logger.info("nrl.com discover %s: %s", name, e)
        return sorted(found)

    def sync_fixtures(self, *, competition: str, round_number: int, org: Organisation) -> int:
        self._check(competition)
        year = org.season.year
        n = 0
        for name in self.SERIES:
            series, rows = self._series_rows(name, round_number, year)
            if not rows:
                continue
            round_obj, _ = Round.objects.get_or_create(
                org=org, round_number=round_number, series=series,
                defaults={
                    "lockout_at": min(r["kickoff_at"] for r in rows),
                    "status": "upcoming",
                    "competition": Competition.for_series(series, org.season),
                },
            )
            earliest = min(r["kickoff_at"] for r in rows)
            if round_obj.lockout_at != earliest:
                round_obj.lockout_at = earliest
                round_obj.save(update_fields=["lockout_at"])
            _ensure_round_competition(round_obj, series, org)

            for r in rows:
                home = _resolve_nrl_team(series, r["home_name"], r["home_external_id"])
                away = _resolve_nrl_team(series, r["away_name"], r["away_external_id"])
                if not home or not away:
                    logger.warning(
                        "Skip nrl.com game %s: unresolved teams %s/%s",
                        r["external_id"], r["home_name"], r["away_name"],
                    )
                    continue
                match = _resolve_match(round_obj, r["external_id"], home, away)
                if match is None:
                    Match.objects.create(
                        round=round_obj, external_id=r["external_id"],
                        home_team=home, away_team=away,
                        kickoff_at=r["kickoff_at"],
                        venue=r["venue"], venue_city=r["venue_city"],
                    )
                else:
                    match.external_id = r["external_id"]
                    match.home_team, match.away_team = home, away
                    match.kickoff_at = r["kickoff_at"]
                    match.venue, match.venue_city = r["venue"], r["venue_city"]
                    match.save(update_fields=[
                        "external_id", "home_team", "away_team",
                        "kickoff_at", "venue", "venue_city",
                    ])
                n += 1
        return n

    def sync_live(self, *, competition: str, round_number: int, org: Organisation) -> int:
        """In-play score and clock. Deliberately does NOT grade tips.

        Same discipline as the AFL service: a score mid-game is provisional,
        and grading on it would award points that a later correction takes
        away. Grading happens once, in sync_results, when the game is over.
        """
        self._check(competition)
        year = org.season.year
        n = 0
        for name in self.SERIES:
            series, rows = self._series_rows(name, round_number, year)
            if not rows:
                continue
            round_obj = Round.objects.filter(org=org, round_number=round_number, series=series).first()
            if round_obj is None:
                continue
            for r in rows:
                if r["status"] == "scheduled":
                    continue
                home = _resolve_nrl_team(series, r["home_name"], r["home_external_id"])
                away = _resolve_nrl_team(series, r["away_name"], r["away_external_id"])
                if not home or not away:
                    continue
                match = _resolve_match(round_obj, r["external_id"], home, away)
                if match is None:
                    continue
                match.home_score = r["home_score"]
                match.away_score = r["away_score"]
                match.status = r["status"]
                match.period = r["period"][:32]
                match.clock = (r["clock"] or "")[:16]
                match.live_updated_at = timezone.now()
                match.save(update_fields=[
                    "home_score", "away_score", "status", "period", "clock", "live_updated_at",
                ])
                n += 1
        return n

    def sync_results(self, *, competition: str, round_number: int, org: Organisation) -> int:
        """Final scores, and the grading that follows from them."""
        self._check(competition)
        year = org.season.year
        n = 0
        for name in self.SERIES:
            series, rows = self._series_rows(name, round_number, year)
            if not rows:
                continue
            round_obj = Round.objects.filter(org=org, round_number=round_number, series=series).first()
            if round_obj is None:
                continue
            for r in rows:
                # Only a finished game grades. Anything else is provisional.
                if r["status"] != "complete" or r["home_score"] is None:
                    continue
                home = _resolve_nrl_team(series, r["home_name"], r["home_external_id"])
                away = _resolve_nrl_team(series, r["away_name"], r["away_external_id"])
                if not home or not away:
                    continue
                match = _resolve_match(round_obj, r["external_id"], home, away)
                if match is None:
                    continue
                match.status = "complete"
                match.period = r["period"][:32]
                match.save(update_fields=["status", "period"])
                # Writes the scores, derives the result and regrades every tip
                # on this match. Also flags an already-published recap for
                # review if the score is a correction rather than a first
                # result, which is why the scores go through here and are not
                # written directly above.
                record_match_result(match, int(r["home_score"]), int(r["away_score"]))
                n += 1
        return n

    def sync_ladder(self, *, competition: str, org: Organisation, **kw) -> int:
        """Not scraped yet.

        The draw page carries no standings; they live on /ladder/ in a
        different payload. Raising rather than returning 0 keeps the failure
        visible in SyncRun instead of looking like a ladder with no movement.
        """
        self._check(competition)
        raise SyncError(
            "NRL ladder is not scraped yet — nrl.com serves standings from "
            "/ladder/, which needs its own collector."
        )


class AflScrapeSyncService:
    """AFL and AFLW from afl.com.au's own CFS API.

    Replaces SquiggleSyncService. Squiggle worked, but it is AFL only, so AFLW
    — one of the four codes GoodTip launches with — had no feed at all; and it
    rate-limits, returning 403 within minutes of traffic-driven syncing being
    enabled.

    Structured like NrlScrapeSyncService: ``competition`` is the catalog
    Competition name ("AFL"), while the scraper works in Series ("AFL",
    "AFLW"), and each method loops the series because one round of one series
    is one request.
    """

    SERIES = ["AFL", "AFLW"]

    def __init__(self):
        from .scrapers.afl import AflApiScraper
        self._scraper = AflApiScraper()

    def _check(self, competition: str) -> None:
        if competition != "AFL":
            raise SyncError("The afl.com.au scraper handles the AFL competition only.")

    def _series_rows(self, series_name: str, round_number: int, year: int):
        from .scrapers.afl import AflScrapeError
        series = Series.objects.filter(name=series_name).first()
        if series is None:
            return None, []
        try:
            rows = self._scraper.fixtures(
                series=series_name, season=year, round_number=round_number,
            )
        except AflScrapeError as e:
            logger.info("afl.com.au %s round %s: %s", series_name, round_number, e)
            return series, []
        return series, rows

    def discover_rounds(self, *, competition: str, org: Organisation,
                        horizon_days: int = 21, back_days: int = 3) -> list[int]:
        self._check(competition)
        from .scrapers.afl import AflScrapeError
        found: set[int] = set()
        for name in self.SERIES:
            try:
                found.update(self._scraper.rounds_in_window(
                    series=name, season=org.season.year,
                    horizon_days=horizon_days, back_days=back_days,
                ))
            except AflScrapeError as e:
                logger.info("afl.com.au discover %s: %s", name, e)
        return sorted(found)

    def sync_fixtures(self, *, competition: str, round_number: int, org: Organisation) -> int:
        self._check(competition)
        year = org.season.year
        n = 0
        for name in self.SERIES:
            series, rows = self._series_rows(name, round_number, year)
            if not rows:
                continue
            round_obj, _ = Round.objects.get_or_create(
                org=org, round_number=round_number, series=series,
                defaults={
                    "lockout_at": min(r["kickoff_at"] for r in rows),
                    "status": "upcoming",
                    "competition": Competition.for_series(series, org.season),
                },
            )
            earliest = min(r["kickoff_at"] for r in rows)
            if round_obj.lockout_at != earliest:
                round_obj.lockout_at = earliest
                round_obj.save(update_fields=["lockout_at"])
            _ensure_round_competition(round_obj, series, org)

            for r in rows:
                home = _resolve_team(series, r["home_name"], r["home_external_id"])
                away = _resolve_team(series, r["away_name"], r["away_external_id"])
                if not home or not away:
                    logger.warning(
                        "Skip afl.com.au game %s: unresolved teams %s/%s",
                        r["external_id"], r["home_name"], r["away_name"],
                    )
                    continue
                match = _resolve_match(round_obj, r["external_id"], home, away)
                if match is None:
                    Match.objects.create(
                        round=round_obj, external_id=r["external_id"],
                        home_team=home, away_team=away,
                        kickoff_at=r["kickoff_at"],
                        venue=r["venue"], venue_city=r["venue_city"],
                    )
                else:
                    match.external_id = r["external_id"]
                    match.home_team, match.away_team = home, away
                    match.kickoff_at = r["kickoff_at"]
                    match.venue, match.venue_city = r["venue"], r["venue_city"]
                    match.save(update_fields=[
                        "external_id", "home_team", "away_team",
                        "kickoff_at", "venue", "venue_city",
                    ])
                n += 1
        return n

    def sync_live(self, *, competition: str, round_number: int, org: Organisation) -> int:
        """In-play score. Does NOT grade — a mid-game score is provisional."""
        self._check(competition)
        year = org.season.year
        n = 0
        for name in self.SERIES:
            series, rows = self._series_rows(name, round_number, year)
            if not rows:
                continue
            round_obj = Round.objects.filter(org=org, round_number=round_number, series=series).first()
            if round_obj is None:
                continue
            for r in rows:
                if r["status"] == "scheduled":
                    continue
                home = _resolve_team(series, r["home_name"], r["home_external_id"])
                away = _resolve_team(series, r["away_name"], r["away_external_id"])
                if not home or not away:
                    continue
                match = _resolve_match(round_obj, r["external_id"], home, away)
                if match is None:
                    continue
                match.home_score = r["home_score"]
                match.away_score = r["away_score"]
                match.status = r["status"]
                match.period = r["period"][:32]
                match.live_updated_at = timezone.now()
                match.save(update_fields=[
                    "home_score", "away_score", "status", "period", "live_updated_at",
                ])
                n += 1
        return n

    def sync_results(self, *, competition: str, round_number: int, org: Organisation) -> int:
        self._check(competition)
        year = org.season.year
        n = 0
        for name in self.SERIES:
            series, rows = self._series_rows(name, round_number, year)
            if not rows:
                continue
            round_obj = Round.objects.filter(org=org, round_number=round_number, series=series).first()
            if round_obj is None:
                continue
            for r in rows:
                if r["status"] != "complete" or r["home_score"] is None:
                    continue
                home = _resolve_team(series, r["home_name"], r["home_external_id"])
                away = _resolve_team(series, r["away_name"], r["away_external_id"])
                if not home or not away:
                    continue
                match = _resolve_match(round_obj, r["external_id"], home, away)
                if match is None:
                    continue
                match.status = "complete"
                match.period = r["period"][:32]
                match.save(update_fields=["status", "period"])
                record_match_result(match, int(r["home_score"]), int(r["away_score"]))
                n += 1
        return n

    def sync_ladder(self, *, competition: str, org: Organisation, **kw) -> int:
        """Still Squiggle's job.

        The CFS ladder endpoint is behind the premium tier, and Squiggle's
        standings work and are not what was rate-limiting us — the draw calls
        were. Delegated rather than reimplemented.
        """
        self._check(competition)
        return SquiggleSyncService().sync_ladder(competition=competition, org=org, **kw)


def slug_candidates(clean_name: str) -> list[str]:
    """Slugs to try for a club name, most specific first.

    Pure and ordered, because the ORDER is the whole strategy and getting it
    wrong is silent: a name resolves to the wrong club, or to none, and the
    fixture is simply dropped from history with a log line nobody reads.

        Carlton Blues              -> carlton-blues, carlton, blues
        North Melbourne Kangaroos  -> north-melbourne-kangaroos,
                                      melbourne-kangaroos, north-melbourne,
                                      north, kangaroos

    Four passes, in this order:

      1. The full slug. A club stored under its whole broadcast name must
         match itself before any shortening is attempted, or "West Coast
         Eagles" would resolve on the prefix "west-coast".
      2. The trailing two words, which is where a nickname usually sits.
      3. LEADING words, longest prefix first. Sportradar sends "Carlton
         Blues" where the Team table holds "Carlton"; only the trailing words
         were tried before, so ten of eighteen AFLW clubs never resolved and
         their games never entered history. Longest first so "North Melbourne
         Kangaroos" reaches "north-melbourne" and never settles for "north".
      4. The last word alone, the loosest guess, tried only once everything
         more specific has failed.
    """
    words = slugify((clean_name or "").replace("&", "and")).split("-")
    words = [w for w in words if w]
    if not words:
        return []
    out = ["-".join(words)]
    if len(words) >= 2:
        out.append("-".join(words[-2:]))
    for cut in range(len(words) - 1, 0, -1):
        out.append("-".join(words[:cut]))
    out.append(words[-1])
    # Order-preserving dedupe: a two-word name generates the same slug more
    # than once, and re-querying it is pure waste.
    seen, unique = set(), []
    for slug in out:
        if slug not in seen:
            seen.add(slug)
            unique.append(slug)
    return unique


def _resolve_sr_team(series: Series, name: str, external_id: str = "") -> Team | None:
    """Team lookup for Sportradar names.

    Sportradar uses the broadcast name ("Manly Sea Eagles", "St George
    Illawarra Dragons") where the Team table stores the full registered name
    ("Manly Warringah Sea Eagles") under a nickname slug ("sea-eagles"). Neither
    an exact slug nor an exact name matches, so both sides were being skipped
    and whole fixtures silently dropped.

    Resolution walks from most to least specific and stops at the first hit:
    the stable Sportradar id, the full slug, then the trailing one or two words
    as a slug — which is where the nickname always lives in these names.
    """
    if not name:
        return None

    def stamp(t):
        if t and external_id and not t.external_id:
            t.external_id = external_id
            t.save(update_fields=["external_id"])
        return t

    if external_id:
        t = Team.objects.filter(series=series, external_id=external_id).first()
        if t:
            return t

    # Sportradar disambiguates clubs that share a name across codes by
    # appending the competition: "Dolphins (Nrl)". That suffix is not part of
    # the club's name and slugifies into it ("dolphins-nrl"), so it is stripped
    # before any lookup.
    clean = re.sub(r"\s*\([^)]*\)\s*", " ", name).strip()
    candidates = slug_candidates(clean)
    for slug in candidates:
        t = Team.objects.filter(series=series, slug=slug).first()
        if t:
            return stamp(t)

    t = Team.objects.filter(series=series, name__iexact=clean).first()
    if t:
        return stamp(t)
    # Last resort: the stored name ends with the nickname we were given.
    for slug in candidates[1:]:
        t = Team.objects.filter(series=series, name__iendswith=slug.replace("-", " ")).first()
        if t:
            return stamp(t)
    return None


class SportradarSyncService:
    """Fixtures and results from Sportradar, for whichever codes a competition
    bundles.

    One class serves both competitions because the only thing that differs is
    which Series it loops. The client caches a whole season per request, so a
    twelve-round sync costs one call, not twelve — which matters on a trial
    key rated at one request per second.
    """

    #: catalog Competition name -> the Series it covers
    SERIES_FOR = {
        "AFL": ["AFL", "AFLW"],
        "NRL": ["NRL", "NRLW"],
    }

    def __init__(self, competition: str):
        from .providers_sportradar import SportradarClient
        self.competition = competition.upper()
        if self.competition not in self.SERIES_FOR:
            raise SyncError(f"Sportradar does not cover {competition!r}.")
        self._client = SportradarClient()

    @property
    def series_names(self):
        return self.SERIES_FOR[self.competition]

    def _check(self, competition: str) -> None:
        if competition.upper() != self.competition:
            raise SyncError(f"This service handles {self.competition}, not {competition!r}.")

    def _rows(self, series_name: str, round_number: int, year: int):
        from .providers_sportradar import SportradarError
        series = Series.objects.filter(name=series_name).first()
        if series is None:
            return None, []
        try:
            return series, self._client.fixtures(
                series=series_name, season=year, round_number=round_number)
        except SportradarError as e:
            logger.info("Sportradar %s round %s: %s", series_name, round_number, e)
            return series, []

    def discover_rounds(self, *, competition: str, org: Organisation,
                        horizon_days: int = 21, back_days: int = 3) -> list[int]:
        self._check(competition)
        from .providers_sportradar import SportradarError
        found: set[int] = set()
        for name in self.series_names:
            try:
                found.update(self._client.rounds_in_window(
                    series=name, season=org.season.year,
                    horizon_days=horizon_days, back_days=back_days))
            except SportradarError as e:
                logger.info("Sportradar discover %s: %s", name, e)
        return sorted(found)

    def sync_fixtures(self, *, competition: str, round_number: int, org: Organisation) -> int:
        self._check(competition)
        n = 0
        for name in self.series_names:
            series, rows = self._rows(name, round_number, org.season.year)
            if not rows:
                continue
            round_obj, _ = Round.objects.get_or_create(
                org=org, round_number=round_number, series=series,
                defaults={"lockout_at": min(r["kickoff_at"] for r in rows),
                          "status": "upcoming",
                          "competition": Competition.for_series(series, org.season)},
            )
            earliest = min(r["kickoff_at"] for r in rows)
            if round_obj.lockout_at != earliest:
                round_obj.lockout_at = earliest
                round_obj.save(update_fields=["lockout_at"])
            _ensure_round_competition(round_obj, series, org)
            for r in rows:
                home = _resolve_sr_team(series, r["home_name"], r["home_external_id"])
                away = _resolve_sr_team(series, r["away_name"], r["away_external_id"])
                if not home or not away:
                    logger.warning("Skip sportradar %s: unresolved %s/%s",
                                   r["external_id"], r["home_name"], r["away_name"])
                    continue
                match = _resolve_match(round_obj, r["external_id"], home, away)
                if match is None:
                    Match.objects.create(
                        round=round_obj, external_id=r["external_id"],
                        home_team=home, away_team=away, kickoff_at=r["kickoff_at"],
                        venue=r["venue"], venue_city=r["venue_city"])
                else:
                    match.external_id = r["external_id"]
                    match.home_team, match.away_team = home, away
                    match.kickoff_at = r["kickoff_at"]
                    match.venue, match.venue_city = r["venue"], r["venue_city"]
                    match.save(update_fields=["external_id", "home_team", "away_team",
                                              "kickoff_at", "venue", "venue_city"])
                n += 1
        return n

    def sync_live(self, *, competition: str, round_number: int, org: Organisation) -> int:
        """In-play score only. Grading waits for sync_results, because a score
        mid-game is provisional and points taken back are worse than points late."""
        self._check(competition)
        n = 0
        for name in self.series_names:
            series, rows = self._rows(name, round_number, org.season.year)
            round_obj = Round.objects.filter(org=org, round_number=round_number, series=series).first() if series else None
            if not rows or round_obj is None:
                continue
            for r in rows:
                if r["status"] == "scheduled":
                    continue
                home = _resolve_sr_team(series, r["home_name"], r["home_external_id"])
                away = _resolve_sr_team(series, r["away_name"], r["away_external_id"])
                match = _resolve_match(round_obj, r["external_id"], home, away) if home and away else None
                if match is None:
                    continue
                match.home_score, match.away_score = r["home_score"], r["away_score"]
                match.status = r["status"]
                match.period = r["period"][:32]
                match.live_updated_at = timezone.now()
                match.save(update_fields=["home_score", "away_score", "status",
                                          "period", "live_updated_at"])
                n += 1
        return n

    def sync_results(self, *, competition: str, round_number: int, org: Organisation) -> int:
        self._check(competition)
        n = 0
        for name in self.series_names:
            series, rows = self._rows(name, round_number, org.season.year)
            round_obj = Round.objects.filter(org=org, round_number=round_number, series=series).first() if series else None
            if not rows or round_obj is None:
                continue
            for r in rows:
                if r["status"] != "complete" or r["home_score"] is None:
                    continue
                home = _resolve_sr_team(series, r["home_name"], r["home_external_id"])
                away = _resolve_sr_team(series, r["away_name"], r["away_external_id"])
                match = _resolve_match(round_obj, r["external_id"], home, away) if home and away else None
                if match is None:
                    continue
                match.status = "complete"
                match.period = r["period"][:32]
                match.save(update_fields=["status", "period"])
                record_match_result(match, int(r["home_score"]), int(r["away_score"]))
                n += 1
        return n

    def sync_ladder(self, *, competition: str, org: Organisation, **kw) -> int:
        """Standings are a separate Sportradar endpoint and are not in the
        client's launch scope ("fixtures and final results"). Falls back to
        the existing provider rather than pretending."""
        self._check(competition)
        if self.competition == "AFL":
            return SquiggleSyncService().sync_ladder(competition=competition, org=org, **kw)
        raise SyncError("NRL ladder is not in the Sportradar launch scope yet.")
