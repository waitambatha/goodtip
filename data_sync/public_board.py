"""What the home page's "Live this season" board shows.

A public, read-only summary of the four real competitions: the top of the
ladder, and the numbers behind four charts (points a game by round, points a game by
club, winning margins, share of wins). It replaced a made-up
four-person office ladder and an illustrative dollar total, so the one rule
here is that NOTHING on it is invented — every figure is read from results we
already hold.

WHERE THE NUMBERS COME FROM
---------------------------
* The table itself is ``tipping.LadderEntry``, the same rows the members'
  ladder page reads. Rank and points are therefore never a second opinion.
* Everything else is worked out from ``matchreader.HistoricalMatch`` — one row
  per real game, regular season only, so a stat can never disagree with the
  ladder it sits beside.

"LIVE" MEANS FRESH, NOT STREAMING
---------------------------------
Results reach us on the sports-sync timers and the ladder is rebuilt after
them, so this can only ever be as current as the last sync. The page polls
``ladder_board_view`` once a minute and the payload is cached for 30 seconds,
which keeps a busy home page from re-tallying two hundred games per visitor.
The payload carries ``updated`` (the newest ladder row) so the page can say
when it last changed instead of implying a heartbeat it does not have.
"""

from __future__ import annotations

from collections import defaultdict

from django.core.cache import cache
from django.http import JsonResponse

# The order the board walks through them. NRL and AFL first: they are what most visitors
# came for, and the women's competitions have far fewer rounds to show.
CODES = ("NRL", "AFL", "NRLW", "AFLW")
# Eight is the finals line in both the AFL and the NRL, so the table is the race
# for the finals rather than an arbitrary slice.
LADDER_ROWS = 8
# The histogram: seven bars of winning margin, the last one open-ended. A bar is
# one converted try wide in the NRL (6) and two goals wide in the AFL (12), so
# each competition's shape reads at its own scale.
MARGIN_BINS = 7
MARGIN_WIDTH = {"NRL": 6, "NRLW": 6, "AFL": 12, "AFLW": 12}
CACHE_KEY = "public-board-v3"
CACHE_SECONDS = 30


def _crest(team) -> dict:
    """Everything the page needs to draw a badge, logo or monogram."""
    from tipping.team_colors import get_team_colors
    from tipping.templatetags.team_extras import _code, _logo_url

    colours = get_team_colors((team.slug or "").lower())
    return {
        "logo": _logo_url(team),
        "code": _code(team),
        "bg": colours["primary"],
        "fg": colours["text_on_primary"],
    }


def _signed(n: int) -> str:
    return f"+{n}" if n > 0 else str(n)


def _form(results: list[dict]) -> str:
    """Last five results, oldest first, as 'WWLWL'."""
    return "".join(r["res"] for r in results[-5:])


def _streak(results: list[dict]) -> int:
    """Current run: +4 is four wins in a row, -2 is two straight losses, 0 after a draw."""
    if not results:
        return 0
    last = results[-1]["res"]
    if last == "D":
        return 0
    n = 0
    for r in reversed(results):
        if r["res"] != last:
            break
        n += 1
    return n if last == "W" else -n


def _code_payload(code: str) -> dict | None:
    from catalog.models import Series
    from data_sync.ladder import RULES
    from matchreader.models import HistoricalMatch
    from tipping.models import LadderEntry

    series = Series.objects.filter(name__iexact=code).first()
    if series is None:
        return None
    latest = (
        LadderEntry.objects.filter(series=series)
        .order_by("-season__year")
        .values_list("season_id", "season__year")
        .first()
    )
    if latest is None:
        return None
    season_id, year = latest

    entries = list(
        LadderEntry.objects.filter(series=series, season_id=season_id)
        .select_related("team")
        .order_by("rank")
    )
    games = list(
        HistoricalMatch.objects.filter(
            series=series, season=year, stage=HistoricalMatch.STAGE_REGULAR
        )
        .select_related("home_team", "away_team")
        .order_by("kickoff_at", "id")
    )
    if not entries or not games:
        return None

    by_percentage = RULES[code].separator == "percentage"

    # Per-club results in the order they were played, plus the season-wide
    # extremes, in a single pass over the games.
    results: dict[int, list[dict]] = defaultdict(list)
    total_points = 0
    home_wins = 0
    away_wins = 0
    draws = 0
    decided = 0
    by_round: dict[int, list[int]] = defaultdict(lambda: [0, 0])  # round -> [points, games]
    biggest = None
    highest = None
    width = MARGIN_WIDTH.get(code, 6)
    margins = [0] * MARGIN_BINS
    for g in games:
        total_points += g.home_score + g.away_score
        tally = by_round[g.round_number]
        tally[0] += g.home_score + g.away_score
        tally[1] += 1
        margin = abs(g.home_score - g.away_score)
        margins[min(margin // width, MARGIN_BINS - 1)] += 1
        if g.home_score != g.away_score:
            decided += 1
            if g.home_score > g.away_score:
                home_wins += 1
            else:
                away_wins += 1
        else:
            draws += 1
        for team, pf, pa in (
            (g.home_team, g.home_score, g.away_score),
            (g.away_team, g.away_score, g.home_score),
        ):
            res = "W" if pf > pa else "L" if pf < pa else "D"
            results[team.id].append({"res": res, "pf": pf, "pa": pa})
            if highest is None or pf > highest["score"]:
                highest = {"team": team.name, "score": pf, "round": g.round_number}
        if margin and (biggest is None or margin > biggest["margin"]):
            winner, loser = (
                (g.home_team, g.away_team)
                if g.home_score > g.away_score
                else (g.away_team, g.home_team)
            )
            biggest = {
                "winner": winner.name,
                "loser": loser.name,
                "margin": margin,
                "round": g.round_number,
            }

    ladder = []
    for e in entries[:LADDER_ROWS]:
        mine = results.get(e.team_id, [])
        played = len(mine) or e.played
        card = {
            "name": e.team.name,
            "rank": e.rank,
            "played": e.played,
            "wins": e.wins,
            "losses": e.losses,
            "draws": e.draws,
            "points": e.points,
            "diff": f"{e.percentage:.1f}%" if by_percentage else _signed(e.points_for - e.points_against),
            "form": _form(mine),
            "streak": _streak(mine),
            "avg_for": round(e.points_for / played, 1) if played else 0,
            "avg_against": round(e.points_against / played, 1) if played else 0,
            **_crest(e.team),
        }
        ladder.append(card)

    return {
        "code": code,
        "season": year,
        "round": max(g.round_number for g in games),
        "games": len(games),
        "ladder": ladder,
        # Average points in a game, round by round. The first chart's line.
        "rounds": [
            {"round": rn, "avg": round(pts / n, 1)}
            for rn, (pts, n) in sorted(by_round.items())
        ],
        "venue": {"home": home_wins, "away": away_wins, "draws": draws},
        # Games by winning margin. A draw lands in the first bar.
        "margins": {"width": width, "bins": margins},
        "stats": {
            "games": len(games),
            "points": total_points,
            "avg_game": round(total_points / len(games), 1),
            "home_win_pct": round(home_wins * 100 / decided) if decided else None,
            "biggest_win": biggest,
            "highest_score": highest,
        },
        "by_percentage": by_percentage,
        "updated": max(e.updated_at for e in entries).isoformat(),
    }


def build_board() -> dict:
    codes = [p for p in (_code_payload(c) for c in CODES) if p]
    return {"codes": codes}


def board() -> dict:
    data = cache.get(CACHE_KEY)
    if data is None:
        data = build_board()
        cache.set(CACHE_KEY, data, CACHE_SECONDS)
    return data


def ladder_board_view(request):
    """The same payload the home page ships inline, for its once-a-minute refresh."""
    return JsonResponse(board())
