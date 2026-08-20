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
import random
from dataclasses import dataclass
from datetime import datetime

from django.db.models import Q

from .algorithm import FORM_WINDOW, Prediction, predict, recent_form
from .models import HistoricalMatch, ModelVersion

logger = logging.getLogger(__name__)

# Below this, a form figure is noise. Two games is a coin toss dressed as a
# trend, and the tipper is better served by no tier than a confident one.
MIN_GAMES_FOR_FORM = 3

# Below this many completed regular-season games for a competition THIS
# season, its home-win rate is a real number built on too small a sample to
# show with the same visual confidence as AFL's, which rests on a full season
# and decades of history (MatchReader_Copy_Update_Erick.docx §4). Below the
# threshold a raw count is shown instead of a percentage — a count reads as
# obviously provisional in a way a percentage does not, which is the point.
MIN_GAMES_FOR_SEASON_RATE = 20

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


def _season_year(match) -> int | None:
    """The season this fixture's competition is playing, for scoping the
    small-sample check to THIS season rather than the competition's whole
    history. None when the round has no competition set, which the small-
    sample check treats the same as "no games yet" (§4)."""
    competition = match.round.competition
    return competition.season.year if competition and competition.season_id else None


def _season_records(series_season_pairs, before) -> dict:
    """(home wins, games played) so far this season, per (series_id,
    season_year) — fetched once for a whole batch rather than once per
    fixture, same reasoning as the batched form query below.

    Deliberately live off the shared history table rather than off the
    trained model's ``baseline_accuracy``: that figure is fixed at whatever
    it measured on the last retrain, while this is the actual count as of
    right now, which is what "early in the season" needs to be honest about.
    """
    pairs = {p for p in series_season_pairs if p[1] is not None}
    if not pairs:
        return {}
    q = Q()
    for series_id, season_year in pairs:
        q |= Q(series_id=series_id, season=season_year)
    rows = HistoricalMatch.objects.filter(
        q, stage=HistoricalMatch.STAGE_REGULAR, kickoff_at__lt=before,
    ).values_list("series_id", "season", "home_score", "away_score")
    out = {p: [0, 0] for p in pairs}
    for series_id, season_year, home_score, away_score in rows:
        rec = out[(series_id, season_year)]
        rec[1] += 1
        if home_score > away_score:
            rec[0] += 1
    return {k: tuple(v) for k, v in out.items()}


def _season_home_record(series_id, season_year, before) -> tuple[int, int]:
    return _season_records({(series_id, season_year)}, before).get(
        (series_id, season_year), (0, 0),
    )


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


def _assemble(match, version, home_games, away_games, home_form, away_form, season_record) -> dict | None:
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
        explanation = explain(prediction, home_row, away_row, version, match, season_record)
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
    season_record = _season_home_record(series_id, _season_year(match), match.kickoff_at)
    return _assemble(match, version, home_games, away_games, home_form, away_form, season_record)


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

    # A SEPARATE query from `rows` above: that one is scoped to the teams
    # playing in this batch, which is right for form but wrong for a
    # competition-wide home-win count — a small competition's fixture list
    # would then be judging itself on only the games its own batch happened
    # to touch. Fetched once per distinct (series, season) in the batch, not
    # once per fixture.
    season_records = _season_records(
        {(m.round.series_id, _season_year(m)) for m in matches}, latest,
    )

    out = {}
    for m in matches:
        sid = m.round.series_id
        home_form, home_games = form_before(sid, m.home_team_id, m.kickoff_at)
        away_form, away_games = form_before(sid, m.away_team_id, m.kickoff_at)
        season_record = season_records.get((sid, _season_year(m)), (0, 0))
        out[m.id] = _assemble(
            m, versions.get(sid), home_games, away_games, home_form, away_form, season_record,
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


# MatchReader_Copy_Update_Erick.docx §2: "form" and "prediction" sit too close
# to betting/tipster language once they sit next to "edge" and "pick"
# elsewhere on the card. Neither word gets a single substitute — swapping one
# word for another just moves the repetition problem — so headlines rotate
# across a small approved set instead. Three lines per tier, picked by a seed
# fixed on the fixture: the same card reads the same way on a second look,
# but three same-tier cards on one screen do not read as one sentence with
# the names swapped (§5).
HEADLINE_VARIANTS = {
    "Even Contest": [
        "Nothing between {home} and {away} on recent results.",
        "{home} and {away} are hard to split right now.",
        "The last five give no real edge either way.",
    ],
    "Slight Edge": [
        "{lead} are the pick, but it is close.",
        "{lead} get a slight nod from the last five.",
        "Lean {lead}. Not by much.",
    ],
    "Clear Edge": [
        "{lead} hold a clear edge here.",
        "Recent results clearly favour {lead}.",
        "{lead} are the clear pick from recent games.",
    ],
    "Strong Position": [
        "{lead} are strongly favoured on the last five.",
        "Recent record points firmly to {lead}.",
        "{lead} look well ahead here.",
    ],
    "Dominant Position": [
        "{lead} are the clear standout in recent games.",
        "Recent record heavily favours {lead}.",
        "{lead} dominate the recent picture.",
    ],
}

# §5: rotated only for "the favourite is at home", which is the one case the
# brief supplied variants for. The even-contest and favourite-plays-away
# branches below keep one line each until there is client-approved wording to
# rotate them too.
GROUND_HOME_VARIANTS = [
    "They are at home too, and home sides win about {rate}% in this competition.",
    "Home matters here. Home sides win about {rate}% of the time in this competition.",
    "Home sides win about {rate}% at home in this competition.",
]


def explain(prediction: Prediction, home: dict, away: dict, version, match, season_record) -> dict:
    """Why the model favours who it favours, for somebody who does not follow
    the game.

    Three things get said, in the order they carry weight:

      1. Who is favoured, as a sentence rather than a tier name. "Slight Edge:
         Fremantle" is jargon; "Fremantle are the pick, but it is close" is
         not.
      2. The form behind it, because that is literally the model's only input
         and the tipper can check it.
      3. Home ground. Above MIN_GAMES_FOR_SEASON_RATE completed games this
         season, that is the competition's own historical home-win rate — a
         fact about the past, not a forecast, which is why it can be shown
         where the model's probability cannot. Below the threshold, a raw
         count stands in for it instead (§4): the same fact, phrased so it
         cannot be mistaken for a stable, seasons-deep number.

    The favoured side's home status is what makes point 3 worth saying at all,
    so it is phrased as support or as a caveat depending on which way it cuts.
    """
    leader_is_home = home["is_leader"]
    lead, other = (home, away) if leader_is_home else (away, home)
    # Seeded on the fixture, not on the round or the screen: the same match
    # reads the same way if the page is reloaded, but the fixture next to it
    # in the list draws its own seed and so, almost always, its own words.
    rng = random.Random(f"matchreader:{match.id}:{prediction.tier}")

    if prediction.is_even:
        headline = rng.choice(HEADLINE_VARIANTS["Even Contest"]).format(
            home=home["name"], away=away["name"],
        )
    else:
        headline = rng.choice(HEADLINE_VARIANTS[prediction.tier]).format(lead=lead["name"])

    # §5: the joined "X... while Y..." sentence becomes two short ones. Both
    # keep the FULL record phrase (including a drawn game, or a clean sweep)
    # rather than dropping the qualifier on the second team — a shortened
    # second sentence must not read as a plainer record than the one that
    # actually happened.
    first, second = (home, away) if prediction.is_even else (lead, other)
    form = (
        f"{first['name']} have {_record_phrase(first)}. "
        f"{second['name']} have {_record_phrase(second)}."
    )

    home_wins, games_played = season_record
    ground = ""
    if games_played >= MIN_GAMES_FOR_SEASON_RATE:
        # baseline_accuracy IS the home-win rate: it is the accuracy of always
        # picking the home side, measured on real seasons of this competition.
        # Left exactly as built (client feedback §1) — only the framing around
        # a THIN sample changes below, never how this number is calculated.
        home_rate = round((version.baseline_accuracy or 0) * 100)
        if home_rate:
            if prediction.is_even:
                ground = (
                    f"{home['name']} are at home, and home sides have won about "
                    f"{home_rate}% of games in this competition."
                )
            elif leader_is_home:
                ground = rng.choice(GROUND_HOME_VARIANTS).format(rate=home_rate)
            else:
                ground = (
                    f"{other['name']} are at home though, and that is worth "
                    f"something: home sides win about {home_rate}% of these games."
                )
    elif games_played:
        if prediction.is_even:
            ground = (
                f"Early in the season, but home sides have won {home_wins} of "
                f"{games_played} games so far in this competition."
            )
        elif leader_is_home:
            ground = (
                f"Early in the season, but home sides have won {home_wins} of "
                f"{games_played} games so far."
            )
        else:
            ground = (
                f"Early in the season, but home sides have won {home_wins} of "
                f"{games_played} games so far — {other['name']} are at home though."
            )

    return {
        "headline": headline,
        "form": form,
        "ground": ground,
        "sentences": [s for s in (headline, form, ground) if s],
    }
