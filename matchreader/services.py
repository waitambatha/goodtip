"""What the tipping screen asks MatchReader.

One entry point: ``read_match(match)`` returns a Prediction or None. None is
a real answer and the caller must handle it — no active model for that series,
or not enough history behind the two sides to say anything. Inventing an "Even
Contest" in those cases would dress up silence as analysis.
"""

from __future__ import annotations

import logging

from django.db.models import Q

from .algorithm import FORM_WINDOW, Prediction, predict, recent_form
from .models import HistoricalMatch, ModelVersion

logger = logging.getLogger(__name__)

# Below this, a form figure is noise. Two games is a coin toss dressed as a
# trend, and the tipper is better served by no tier than a confident one.
MIN_GAMES_FOR_FORM = 3


def _form(team_id: int, series_id: int, before) -> tuple[float, int]:
    """(form, games counted) for a team going into a fixture.

    Reads only games that had already kicked off, from the shared history
    rather than any org's rounds, so two leagues tipping the same fixture get
    the same answer.
    """
    rows = (
        HistoricalMatch.objects
        .filter(series_id=series_id, kickoff_at__lt=before)
        .filter(Q(home_team_id=team_id) | Q(away_team_id=team_id))
        .order_by("-kickoff_at")[:FORM_WINDOW]
    )
    results = [
        (m.home_score > m.away_score) if m.home_team_id == team_id
        else (m.away_score > m.home_score)
        for m in rows
    ]
    return recent_form(results), len(results)


def read_match(match) -> Prediction | None:
    """MatchReader's call on one fixture, or None if it should not make one."""
    series_id = match.round.series_id
    version = ModelVersion.objects.filter(series_id=series_id, is_active=True).first()
    if version is None:
        return None

    home_form, home_n = _form(match.home_team_id, series_id, match.kickoff_at)
    away_form, away_n = _form(match.away_team_id, series_id, match.kickoff_at)
    if min(home_n, away_n) < MIN_GAMES_FOR_FORM:
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
