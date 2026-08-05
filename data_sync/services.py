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
    comp = competition.upper()
    if comp == "AFL":
        return SquiggleSyncService()
    if comp == "NRL":
        return ApiSportsRugbySyncService()
    raise SyncError(f"Unsupported competition: {competition}")
