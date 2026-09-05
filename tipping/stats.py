"""Stats for the two ranking pages: a member's season, and a club's.

WHY THIS IS A MODULE AND NOT TWO VIEWS
--------------------------------------
Asked for on both boards at once — "I should have stats, so we can have my stats
in the leaderboard... then for the ladder it will be for the teams, be able to
click a team and see its stats, how has it been performing" — and the two
answer the same shape of question: a run of results over a season, a trend, a
best and a worst. Keeping the arithmetic here means the tipper's page and the
club's page cannot come to disagree about what "form" or "recent" means.

WHAT IS DELIBERATELY NOT HERE
-----------------------------
Nothing is predicted. MatchReader is the part of the product that forecasts, and
it is a fitted model with an accuracy figure attached; a page of season summary
statistics quietly extrapolating a trend line would be making claims of the same
kind with none of the same backing.

Every figure below is a count of things that happened.
"""
from __future__ import annotations

from django.db.models import Count, Q, Sum

#: How many rounds "recent form" looks back over. Five, matching the fixture
#: card's form strip and the round stepper — three numbers that mean "lately"
#: on the same screen should mean the same lately.
FORM_ROUNDS = 5


def _pct(part, whole):
    return round(part / whole * 100) if whole else None


def _trend(recent, earlier):
    """A direction and a size, or None when there is not enough season yet.

    "Have I grown, or have I dropped." Answered as the difference between the
    most recent stretch and everything before it, which is the comparison a
    reader makes for themselves — and refused rather than guessed when either
    side is empty, because a trend drawn from one round is a coin toss with an
    arrow on it.
    """
    if recent is None or earlier is None:
        return None
    return {"delta": recent - earlier, "recent": recent, "earlier": earlier}


def member_season(user, org, group=None, series=None):
    """One member's season: the cards, the per-round run, and the codes.

    Returns a dict a template can render without arithmetic. Every number is
    scoped the same way the leaderboard is — same org, same room, same optional
    competition — so a stat beside a rank cannot describe a different season
    from the rank.
    """
    from .models import Tip

    tips = Tip.objects.filter(user=user, org=org)
    tips = tips.filter(group=group) if group is not None else tips.filter(group__isnull=True)
    if series is not None:
        tips = tips.filter(match__round__series=series)

    # Graded, and made by the person. The missed-tip default writes a real tip
    # that scores real points, so it belongs in the points total and not in a
    # claim about judgement — the same split the leaderboard makes.
    graded = tips.filter(is_correct__isnull=False)
    mine = graded.filter(is_auto=False)

    rows = list(
        mine.values(
            "match__round__round_number",
            "match__round__series__name",
            "match__round__series__slug",
            "match__round__series__category",
        )
        .annotate(
            played=Count("id"),
            right=Count("id", filter=Q(is_correct=True)),
            points=Sum("points_awarded"),
        )
        .order_by("match__round__round_number")
    )

    # Per round, pooled across competitions: the shape of the season as the
    # member lived it, week by week.
    by_round: dict[int, dict] = {}
    for r in rows:
        n = r["match__round__round_number"]
        slot = by_round.setdefault(n, {"round": n, "played": 0, "right": 0, "points": 0})
        slot["played"] += r["played"]
        slot["right"] += r["right"]
        slot["points"] += r["points"] or 0
    run = [dict(v, accuracy=_pct(v["right"], v["played"])) for v in sorted(by_round.values(), key=lambda x: x["round"])]

    # Per competition: strongest and weakest, which is the question that
    # cannot be asked of a single combined total.
    # Slug and category travel with the name so each bar can wear its own
    # competition's colour — the name lowercased is not the slug ("State of
    # Origin" against "state-of-origin"), and guessing one from the other is
    # how a code ends up grey.
    by_code: dict[str, dict] = {}
    for r in rows:
        name = r["match__round__series__name"]
        slot = by_code.setdefault(name, {
            "name": name,
            "slug": r["match__round__series__slug"],
            "category": r["match__round__series__category"],
            "played": 0, "right": 0, "points": 0,
        })
        slot["played"] += r["played"]
        slot["right"] += r["right"]
        slot["points"] += r["points"] or 0
    codes = sorted(
        (dict(v, accuracy=_pct(v["right"], v["played"])) for v in by_code.values()),
        key=lambda c: (-(c["accuracy"] or 0), -c["played"]),
    )
    # A code with a handful of games is not a strength or a weakness, it is a
    # small sample — three is the floor at which the label is worth printing.
    rated = [c for c in codes if c["played"] >= 3]

    played = sum(v["played"] for v in by_round.values())
    right = sum(v["right"] for v in by_round.values())
    points = tips.aggregate(p=Sum("points_awarded"))["p"] or 0

    recent = run[-FORM_ROUNDS:]
    earlier = run[:-FORM_ROUNDS]
    recent_acc = _pct(sum(r["right"] for r in recent), sum(r["played"] for r in recent))
    earlier_acc = _pct(sum(r["right"] for r in earlier), sum(r["played"] for r in earlier))

    return {
        "points": points,
        "played": played,
        "right": right,
        # Subtraction a template cannot do: `add` is addition, and chaining it
        # through a negative sign does not work the way it looks like it does.
        "missed": played - right,
        "accuracy": _pct(right, played),
        "run": run,
        "recent": recent,
        "codes": codes,
        "best_code": rated[0] if rated else None,
        "worst_code": rated[-1] if len(rated) > 1 else None,
        "trend": _trend(recent_acc, earlier_acc),
        "best_round": max(run, key=lambda r: (r["points"], r["right"]), default=None),
        "streak": _streak(mine),
        "form_rounds": FORM_ROUNDS,
    }


def _streak(graded_tips):
    """The current run of correct tips, newest first, and the best of the season.

    Walked in kickoff order rather than by round number: a member tipping two
    codes has two round 4s, and "in a row" means in time, not in numbering.
    """
    marks = list(
        graded_tips.order_by("match__kickoff_at", "id").values_list("is_correct", flat=True)
    )
    best = run = 0
    for ok in marks:
        run = run + 1 if ok else 0
        best = max(best, run)
    current = 0
    for ok in reversed(marks):
        if not ok:
            break
        current += 1
    return {"current": current, "best": best}


def team_season(team, series, season, up_to_round=None):
    """One club's season: the cards, the results run, and the splits.

    Reads matchreader.HistoricalMatch, for the same reason the ladder does —
    tipping.Match is org-scoped and partial, so a tally taken from it multiplies
    every result by the number of leagues tipping the game and misses every
    round nobody is tipping.
    """
    from matchreader.models import HistoricalMatch

    year = season.year if hasattr(season, "year") else season
    games = HistoricalMatch.objects.filter(
        series=series, season=year, stage=HistoricalMatch.STAGE_REGULAR,
    ).filter(Q(home_team=team) | Q(away_team=team)).select_related("home_team", "away_team")
    if up_to_round is not None:
        games = games.filter(round_number__lte=up_to_round)
    games = list(games.order_by("round_number", "kickoff_at"))

    run, home, away = [], {"played": 0, "won": 0}, {"played": 0, "won": 0}
    scored = conceded = 0
    for g in games:
        at_home = g.home_team_id == team.id
        us, them = (g.home_score, g.away_score) if at_home else (g.away_score, g.home_score)
        outcome = "W" if us > them else "L" if them > us else "D"
        scored += us
        conceded += them
        side = home if at_home else away
        side["played"] += 1
        side["won"] += 1 if outcome == "W" else 0
        run.append({
            "round": g.round_number,
            "outcome": outcome,
            "for": us,
            "against": them,
            "margin": us - them,
            "opponent": (g.away_team if at_home else g.home_team),
            "at_home": at_home,
        })

    played = len(run)
    won = sum(1 for r in run if r["outcome"] == "W")
    drawn = sum(1 for r in run if r["outcome"] == "D")
    recent = run[-FORM_ROUNDS:]
    earlier = run[:-FORM_ROUNDS]
    recent_pct = _pct(sum(1 for r in recent if r["outcome"] == "W"), len(recent))
    earlier_pct = _pct(sum(1 for r in earlier if r["outcome"] == "W"), len(earlier))

    return {
        "played": played,
        "won": won,
        "lost": played - won - drawn,
        "drawn": drawn,
        "win_pct": _pct(won, played),
        "scored": scored,
        "conceded": conceded,
        "differential": scored - conceded,
        "avg_for": round(scored / played, 1) if played else None,
        "avg_against": round(conceded / played, 1) if played else None,
        "run": run,
        "recent": recent,
        "home": dict(home, win_pct=_pct(home["won"], home["played"])),
        "away": dict(away, win_pct=_pct(away["won"], away["played"])),
        "trend": _trend(recent_pct, earlier_pct),
        "biggest_win": max(
            (r for r in run if r["outcome"] == "W"), key=lambda r: r["margin"], default=None,
        ),
        "heaviest_loss": min(
            (r for r in run if r["outcome"] == "L"), key=lambda r: r["margin"], default=None,
        ),
        "form_rounds": FORM_ROUNDS,
    }


# ---------------------------------------------------------------------------
# Drawing helpers.
#
# The shapes are built here, in Python, and handed to the template as finished
# path strings. Two reasons: a Django template cannot do the arithmetic without
# a filter per operation, and a charting library is 60-90KB, has to be told the
# palette twice for the two themes, and paints after the page instead of with
# it. Inline SVG is in the first paint, inherits the competition's colour from
# CSS like everything else on these pages, prints, and needs no JavaScript.
# ---------------------------------------------------------------------------

SPARK_W, SPARK_H = 440, 120
#: Breathing room top and bottom so a maximum does not sit on the frame.
SPARK_PAD = 12


def spark(values, *, width=SPARK_W, height=SPARK_H, floor=None, ceiling=None):
    """A run of numbers as an SVG line, plus the area under it.

    Returns None for fewer than two points — one point is not a trend, and a
    single dot on an empty box reads as a chart that failed to load.
    """
    vals = [v for v in values if v is not None]
    if len(vals) < 2:
        return None
    lo = floor if floor is not None else min(vals)
    hi = ceiling if ceiling is not None else max(vals)
    if hi == lo:
        # A flat run is a real answer; draw it down the middle rather than
        # dividing by zero or spiking it to the top of the box.
        lo, hi = lo - 1, hi + 1
    span = hi - lo
    inner = height - SPARK_PAD * 2
    step = width / (len(vals) - 1)

    pts = []
    for i, v in enumerate(vals):
        x = round(i * step, 1)
        # Flipped: SVG counts downward and a chart does not.
        y = round(SPARK_PAD + inner - (v - lo) / span * inner, 1)
        pts.append({"x": x, "y": y, "value": v})

    path = "M" + " L".join(f"{p['x']},{p['y']}" for p in pts)
    fill = f"{path} L{pts[-1]['x']},{height} L{pts[0]['x']},{height} Z"
    return {"path": path, "fill_path": fill, "last": pts[-1], "points": pts,
            "low": lo, "high": hi}


def bars(rows, *, key="value", label="label"):
    """Rows scaled to a 0-100 width, for a bar drawn in CSS rather than SVG.

    A bar chart is a list of divs with widths — it does not need a canvas, and
    as markup it stays readable to a screen reader and to anyone printing it.
    """
    # `bar_pct` and not `_pct`: Django templates refuse any variable or
    # attribute beginning with an underscore, so a leading-underscore key is
    # unreachable from the markup that exists to draw it.
    values = [r.get(key) or 0 for r in rows]
    top = max(values) if values else 0
    out = []
    for r in rows:
        v = r.get(key) or 0
        out.append(dict(r, bar_pct=round(v / top * 100) if top else 0))
    return out


def donut(part, whole, *, size=120, thickness=14):
    """One proportion as a ring. Returns the dash geometry, or None.

    Refused when there is nothing graded yet: a ring drawn at 0% is a claim
    that somebody got everything wrong, which is a different thing from not
    having played.
    """
    if not whole:
        return None
    r = (size - thickness) / 2
    circumference = 2 * 3.141592653589793 * r
    filled = circumference * (part / whole)
    return {
        "size": size, "r": round(r, 2), "cx": size / 2, "cy": size / 2,
        "thickness": thickness,
        "dash": f"{round(filled, 2)} {round(circumference - filled, 2)}",
        "pct": _pct(part, whole),
    }


def competition_season(series, season, up_to_round=None):
    """The whole competition at once: every club, and what the season looks like.

    ASKED FOR: "let's add STATISTICS, where now we will see not individual but
    overall statistics of all the teams."

    The team page answers "how is this club going". This answers the questions
    that need every club in front of you at once — who scores, who concedes,
    whether home advantage is real in this competition this year — none of which
    a single club's page can say, because each of them is a comparison.

    Built from one pass over the season's completed games rather than a call to
    team_season per club: eighteen clubs would otherwise be eighteen queries
    returning the same rows, since every game belongs to two of them.
    """
    from matchreader.models import HistoricalMatch

    year = season.year if hasattr(season, "year") else season
    games = HistoricalMatch.objects.filter(
        series=series, season=year, stage=HistoricalMatch.STAGE_REGULAR,
    ).select_related("home_team", "away_team")
    if up_to_round is not None:
        games = games.filter(round_number__lte=up_to_round)
    games = list(games.order_by("round_number", "kickoff_at"))

    clubs: dict[int, dict] = {}

    def club(team):
        if team.id not in clubs:
            clubs[team.id] = {
                "team": team, "played": 0, "won": 0, "drawn": 0,
                "scored": 0, "conceded": 0, "form": [],
                "home_played": 0, "home_won": 0,
            }
        return clubs[team.id]

    home_wins = away_wins = draws = 0
    biggest = None
    for g in games:
        h, a = club(g.home_team), club(g.away_team)
        h["played"] += 1
        a["played"] += 1
        h["home_played"] += 1
        h["scored"] += g.home_score
        h["conceded"] += g.away_score
        a["scored"] += g.away_score
        a["conceded"] += g.home_score

        if g.home_score > g.away_score:
            h["won"] += 1
            h["home_won"] += 1
            home_wins += 1
            h["form"].append("W")
            a["form"].append("L")
            margin, winner, loser = g.home_score - g.away_score, g.home_team, g.away_team
        elif g.away_score > g.home_score:
            a["won"] += 1
            away_wins += 1
            h["form"].append("L")
            a["form"].append("W")
            margin, winner, loser = g.away_score - g.home_score, g.away_team, g.home_team
        else:
            h["drawn"] += 1
            a["drawn"] += 1
            draws += 1
            h["form"].append("D")
            a["form"].append("D")
            margin = 0
            winner = loser = None
        if winner is not None and (biggest is None or margin > biggest["margin"]):
            biggest = {
                "margin": margin, "winner": winner, "loser": loser,
                "round": g.round_number, "score": f"{max(g.home_score, g.away_score)}–{min(g.home_score, g.away_score)}",
            }

    rows = []
    for c in clubs.values():
        rows.append(dict(
            c,
            form=c["form"][-FORM_ROUNDS:],
            differential=c["scored"] - c["conceded"],
            win_pct=_pct(c["won"], c["played"]),
            avg_for=round(c["scored"] / c["played"], 1) if c["played"] else None,
            avg_against=round(c["conceded"] / c["played"], 1) if c["played"] else None,
            home_win_pct=_pct(c["home_won"], c["home_played"]),
        ))
    rows.sort(key=lambda r: (-(r["win_pct"] or 0), -r["differential"]))

    total = len(games)
    return {
        "clubs": rows,
        "games": total,
        "teams": len(rows),
        "total_points": sum(g.home_score + g.away_score for g in games),
        "avg_points": round(
            sum(g.home_score + g.away_score for g in games) / total, 1
        ) if total else None,
        # Home advantage as a fact about THIS season, not folklore. Draws are
        # left out of the percentage rather than counted as half a win: a draw
        # is a result neither side's ground produced.
        "home_win_pct": _pct(home_wins, home_wins + away_wins),
        "home_wins": home_wins,
        "away_wins": away_wins,
        "draws": draws,
        "biggest_win": biggest,
        "best_attack": max(rows, key=lambda r: r["avg_for"] or 0, default=None),
        "best_defence": min(rows, key=lambda r: r["avg_against"] or 9999, default=None),
        "form_rounds": FORM_ROUNDS,
    }
