"""What the tipping screen asks MatchReader.

One entry point: ``read_match(match)`` returns a Prediction or None. None is
a real answer and the caller must handle it — no active model for that series,
or not enough history behind the two sides to say anything. Inventing an "Even
Contest" in those cases would dress up silence as analysis.

``read_match_verbose`` adds everything the card needs to SHOW its working: the
last five results as outcomes rather than booleans, the games behind them, and
a plain-English reading of why the model favours who it favours.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from django.db.models import Q

from .algorithm import FORM_WINDOW, Prediction, predict, recent_form
from .models import HistoricalMatch, ModelVersion

logger = logging.getLogger(__name__)

# Below this, a form figure is noise. Two games is a coin toss dressed as a
# trend, and the tipper is better served by no tier than a confident one.
MIN_GAMES_FOR_FORM = 3

WON, DREW, LOST = "W", "D", "L"


@dataclass(frozen=True)
class FormGame:
    """One past result, from one team's point of view.

    A draw is its own outcome here, which it is not in the model. The fitted
    coefficients were learned from ``home_score > away_score``, so a draw
    counts as a non-win in the FORM VALUE and must keep doing so or the live
    predictions stop matching the model that was validated. It is only the
    display that distinguishes the three, because a drawn game shown as a loss
    is a lie about a checkable fact.
    """

    outcome: str            # WON | DREW | LOST
    opponent: str
    scored: int
    conceded: int
    kickoff_at: datetime
    was_home: bool

    @property
    def won(self) -> bool:
        return self.outcome == WON

    @property
    def score_line(self) -> str:
        return f"{self.scored}-{self.conceded}"

    @property
    def result_word(self) -> str:
        return {WON: "Won", DREW: "Drew", LOST: "Lost"}[self.outcome]

    @property
    def venue_word(self) -> str:
        return "at home" if self.was_home else "away"


def _form(team_id: int, series_id: int, before) -> tuple[float, list[FormGame]]:
    """(form, games most-recent-first) for a team going into a fixture.

    Reads only games that had already kicked off, from the shared history
    rather than any org's rounds, so two leagues tipping the same fixture get
    the same answer.

    The games come back whole, not just their count, because the screen draws
    the run and offers the detail behind it: four wins then a loss is a
    different story to a loss then four wins, and both are "4 of 5".
    """
    rows = (
        HistoricalMatch.objects
        .filter(series_id=series_id, kickoff_at__lt=before)
        .filter(Q(home_team_id=team_id) | Q(away_team_id=team_id))
        .select_related("home_team", "away_team")
        .order_by("-kickoff_at")[:FORM_WINDOW]
    )
    games = []
    for m in rows:
        was_home = m.home_team_id == team_id
        scored, conceded = (
            (m.home_score, m.away_score) if was_home else (m.away_score, m.home_score)
        )
        if scored > conceded:
            outcome = WON
        elif scored == conceded:
            outcome = DREW
        else:
            outcome = LOST
        games.append(FormGame(
            outcome=outcome,
            opponent=(m.away_team if was_home else m.home_team).name,
            scored=scored, conceded=conceded,
            kickoff_at=m.kickoff_at, was_home=was_home,
        ))
    # The model's feature, unchanged: wins over the window, draws not counted.
    return recent_form([g.won for g in games]), games


def read_match(match) -> Prediction | None:
    """MatchReader's call on one fixture, or None if it should not make one."""
    series_id = match.round.series_id
    version = ModelVersion.objects.filter(series_id=series_id, is_active=True).first()
    if version is None:
        return None

    home_form, home_games = _form(match.home_team_id, series_id, match.kickoff_at)
    away_form, away_games = _form(match.away_team_id, series_id, match.kickoff_at)
    if min(len(home_games), len(away_games)) < MIN_GAMES_FOR_FORM:
        return None

    return predict(
        intercept=version.intercept,
        form_coef=version.form_coef,
        delta_form=home_form - away_form,
        home_name=match.home_team.name,
        away_name=match.away_team.name,
        # Stays None until a lineup feed exists; the fitted model has no
        # experience coefficient either, so the two agree.
        exp_coef=version.exp_coef,
        delta_exp=None,
    )


def _assemble(match, version, home_games, away_games, home_form, away_form) -> dict | None:
    """One fixture's card data: the form always, the prediction when earned.

    THE SPLIT. Recent results are FACTS — five outcomes and their scores, read
    straight off the history table. They need no model, and they are the part
    a tipper can check. A prediction is an OPINION, and it needs a fitted model
    that has been shown to beat picking the home side.

    Before this the two were welded together, so a series with no active model
    showed nothing at all: no dots, no scores, no last five. Half the fixtures
    on a screen came up bare next to fixtures that were fully dressed, which
    reads as broken rather than as careful. Now the facts show everywhere there
    is history, and only the opinion waits for a model that earned the right
    to give one.

    Returns None only when there is genuinely nothing to show.
    """
    if not home_games and not away_games:
        return None

    # The opinion needs a model AND enough form behind BOTH sides. Below three
    # games a form figure is a coin toss dressed as a trend.
    prediction = explanation = None
    if version is not None and min(len(home_games), len(away_games)) >= MIN_GAMES_FOR_FORM:
        prediction = predict(
            intercept=version.intercept,
            form_coef=version.form_coef,
            delta_form=home_form - away_form,
            home_name=match.home_team.name,
            away_name=match.away_team.name,
            # Stays None until a lineup feed exists; the fitted model has no
            # experience coefficient either, so the two agree.
            exp_coef=version.exp_coef,
            delta_exp=None,
        )

    home_row = _form_row(match.home_team, home_games, prediction, is_home=True)
    away_row = _form_row(match.away_team, away_games, prediction, is_home=False)
    if prediction is not None:
        explanation = explain(prediction, home_row, away_row, version)
    return {
        "prediction": prediction,
        "explanation": explanation,
        "home": home_row,
        "away": away_row,
        # Kept for anything still reading the flat shape.
        "home_wins": home_row["wins"], "home_of": home_row["of"],
        "away_wins": away_row["wins"], "away_of": away_row["of"],
        "form_rows": [home_row, away_row],
    }


def read_match_verbose(match) -> dict | None:
    """Recent form for both sides, and a prediction if one can be justified.

    The tier alone ("Clear Edge: Brisbane Lions") tells somebody who follows
    the game everything and somebody who does not almost nothing. The form
    records are what the model actually looked at, so showing them turns a
    verdict into a reason: 4 from 5 against 1 from 5 is a sentence anyone can
    check.

    Deliberately NOT the probability. The deck forbids showing a number, and
    a win record is a fact rather than a forecast.
    """
    series_id = match.round.series_id
    version = ModelVersion.objects.filter(series_id=series_id, is_active=True).first()
    home_form, home_games = _form(match.home_team_id, series_id, match.kickoff_at)
    away_form, away_games = _form(match.away_team_id, series_id, match.kickoff_at)
    return _assemble(match, version, home_games, away_games, home_form, away_form)


def read_matches_verbose(matches) -> dict:
    """``read_match_verbose`` for a whole screen, in a fixed number of queries.

    Called once per fixture, the single-match version costs three queries: the
    active model, then each side's history. A dashboard showing forty games
    therefore issued about a hundred and twenty, which on a remote database was
    the difference between a page and a two-minute wait.

    Nothing about the answers changes. The model lookup is done once per
    series, and every side's history is fetched in one query and then sliced in
    Python at each fixture's own kickoff — which is the part that has to stay
    per match, because a team's "last five" depends on when the game is.

    Returns {match_id: reader dict or None}.
    """
    matches = list(matches)
    if not matches:
        return {}

    series_ids = {m.round.series_id for m in matches}
    # Absent for a series with no fitted model. That is no longer a reason to
    # return nothing: the form still shows, only the prediction waits.
    versions = {
        v.series_id: v
        for v in ModelVersion.objects.filter(series_id__in=series_ids, is_active=True)
    }

    team_ids = {m.home_team_id for m in matches} | {m.away_team_id for m in matches}
    latest = max(m.kickoff_at for m in matches)

    rows = (
        HistoricalMatch.objects
        .filter(series_id__in=series_ids, kickoff_at__lt=latest)
        .filter(Q(home_team_id__in=team_ids) | Q(away_team_id__in=team_ids))
        .select_related("home_team", "away_team")
        .order_by("kickoff_at")
    )

    # (series, team) -> that team's games, oldest first, so the slice below is
    # a walk rather than a re-query.
    by_team: dict[tuple[int, int], list[FormGame]] = {}
    for m in rows:
        for team_id, was_home in ((m.home_team_id, True), (m.away_team_id, False)):
            if team_id not in team_ids:
                continue
            scored, conceded = (
                (m.home_score, m.away_score) if was_home else (m.away_score, m.home_score)
            )
            if scored > conceded:
                outcome = WON
            elif scored == conceded:
                outcome = DREW
            else:
                outcome = LOST
            by_team.setdefault((m.series_id, team_id), []).append(FormGame(
                outcome=outcome,
                opponent=(m.away_team if was_home else m.home_team).name,
                scored=scored, conceded=conceded,
                kickoff_at=m.kickoff_at, was_home=was_home,
            ))

    def form_before(series_id, team_id, when):
        games = by_team.get((series_id, team_id), ())
        # Oldest-first in, most-recent-first out, matching _form().
        window = [g for g in games if g.kickoff_at < when][-FORM_WINDOW:]
        window.reverse()
        return recent_form([g.won for g in window]), window

    out = {}
    for m in matches:
        sid = m.round.series_id
        home_form, home_games = form_before(sid, m.home_team_id, m.kickoff_at)
        away_form, away_games = form_before(sid, m.away_team_id, m.kickoff_at)
        out[m.id] = _assemble(
            m, versions.get(sid), home_games, away_games, home_form, away_form
        )
    return out


def _form_row(team, games: list[FormGame], prediction: Prediction, *, is_home: bool) -> dict:
    """One team's recent form, ready to render.

    ``games`` arrives most-recent-first because that is how it was queried;
    it goes out oldest-first because that is the direction a run is read in.
    """
    return {
        "team": team,
        "name": team.name,
        "games": list(reversed(games)),
        "wins": sum(1 for g in games if g.outcome == WON),
        "draws": sum(1 for g in games if g.outcome == DREW),
        "losses": sum(1 for g in games if g.outcome == LOST),
        "of": len(games),
        # No model, no favourite. Nothing is "leading" on facts alone.
        "is_leader": prediction is not None and team.name == prediction.leader,
        "is_home": is_home,
        # Kept for templates still drawing booleans.
        "results": [g.won for g in reversed(games)],
    }


# ---------------------------------------------------------------------------
# Saying it in words
# ---------------------------------------------------------------------------

def _record_phrase(row: dict) -> str:
    """"won 4 of their last 5" — the clause after a team name and "have".

    A clean sweep either way gets named rather than counted: "won 0 of their
    last 5" is the sort of phrasing that makes a reader stop and do the
    arithmetic, and "lost all 5" is the same fact said once.
    """
    n, w, d = row["of"], row["wins"], row["draws"]
    if not d and w == n:
        return f"won all {n}" if n > 1 else "won their last game"
    if not d and w == 0:
        return f"lost all {n}" if n > 1 else "lost their last game"
    base = f"won {w} of their last {n}"
    if d:
        base += f" with {d} drawn"
    return base


def explain(prediction: Prediction, home: dict, away: dict, version) -> dict:
    """Why the model favours who it favours, for somebody who does not follow
    the game.

    Three things get said, in the order they carry weight:

      1. Who is favoured, as a sentence rather than a tier name. "Slight Edge:
         Fremantle" is jargon; "Fremantle are the pick, but it is close" is
         not.
      2. The form behind it, because that is literally the model's only input
         and the tipper can check it.
      3. Home ground, with the competition's OWN historical home-win rate.
         That figure is a fact about the past, not a forecast, which is why it
         can be shown where the model's probability cannot. It also answers
         the question people actually ask: teams win at home, so how much is
         that worth here?

    The favoured side's home status is what makes point 3 worth saying at all,
    so it is phrased as support or as a caveat depending on which way it cuts.
    """
    leader_is_home = home["is_leader"]
    lead, other = (home, away) if leader_is_home else (away, home)

    if prediction.is_even:
        headline = f"Nothing between {home['name']} and {away['name']} on recent form."
    elif prediction.tier == "Slight Edge":
        headline = f"{lead['name']} are the pick, but there is not much in it."
    elif prediction.tier == "Clear Edge":
        headline = f"{lead['name']} are the pick here."
    else:
        headline = f"{lead['name']} are strongly favoured."

    first, second = (home, away) if prediction.is_even else (lead, other)
    form = (
        f"{first['name']} have {_record_phrase(first)}, "
        f"while {second['name']} have {_record_phrase(second)}."
    )

    # baseline_accuracy IS the home-win rate: it is the accuracy of always
    # picking the home side, measured on real seasons of this competition.
    home_rate = round((version.baseline_accuracy or 0) * 100)
    ground = ""
    if home_rate:
        if prediction.is_even:
            ground = (
                f"{home['name']} are at home, and home sides have won about "
                f"{home_rate}% of games in this competition."
            )
        elif leader_is_home:
            ground = (
                f"They are at home too, where sides in this competition win "
                f"about {home_rate}% of the time."
            )
        else:
            ground = (
                f"{other['name']} are at home though, and that is worth "
                f"something: home sides win about {home_rate}% of these games."
            )

    return {
        "headline": headline,
        "form": form,
        "ground": ground,
        "sentences": [s for s in (headline, form, ground) if s],
    }
