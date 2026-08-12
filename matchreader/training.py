"""Fitting MatchReader, and scoring it honestly.

The discipline the deck insists on: a TIME-BASED split. The model is fitted
only on older seasons and scored on seasons it never saw during fitting. A
random split would leak — form is computed from neighbouring games, so a
shuffled holdout lets the model see the future of its own test rows and
reports an accuracy nobody will ever get in production.
"""

from __future__ import annotations

import logging

from catalog.models import Series

from .algorithm import FORM_WINDOW, recent_form
from .models import HistoricalMatch

logger = logging.getLogger(__name__)


def _form_before(history: list[HistoricalMatch], team_id: int, index: int) -> float:
    """A team's form going INTO the game at ``index``.

    Walks backwards from the game in question, so only results that had
    already happened are used. Getting this wrong is the classic leak: form
    computed over the whole season would include the very result being
    predicted.
    """
    seen: list[bool] = []
    for past in reversed(history[:index]):
        if past.home_team_id == team_id:
            seen.append(past.home_won)
        elif past.away_team_id == team_id:
            seen.append(not past.home_won)
        else:
            continue
        if len(seen) == FORM_WINDOW:
            break
    return recent_form(seen)


def build_dataset(series: Series):
    """(X, y, matches) for a series, ordered by kickoff.

    Games where neither side has any history yet are kept: round 1 exists
    every season and the model has to say something about it. They score 0.5
    each and contribute a dForm of 0, which is exactly "no evidence".
    """
    history = list(
        HistoricalMatch.objects.filter(series=series)
        .select_related("home_team", "away_team")
        .order_by("kickoff_at")
    )
    X, y, rows = [], [], []
    for i, m in enumerate(history):
        d_form = _form_before(history, m.home_team_id, i) - _form_before(history, m.away_team_id, i)
        X.append([d_form])
        y.append(1 if m.home_won else 0)
        rows.append(m)
    return X, y, rows


def fit_and_score(series_name: str, *, test_seasons: int = 1):
    """Fit on the older seasons, score on the most recent ones.

    Returns a dict of everything needed to justify shipping the model, or to
    refuse to: coefficients, sample sizes, out-of-sample accuracy, and the
    accuracy of simply always picking the home side. Home advantage is real
    and free, so a model that cannot beat it is not a model.
    """
    from sklearn.linear_model import LogisticRegression

    series = Series.objects.get(name=series_name)
    X, y, rows = build_dataset(series)
    if len(X) < 100:
        raise ValueError(
            f"Only {len(X)} games in history for {series_name}. "
            "Backfill more seasons before fitting — a two-coefficient model "
            "fitted on this little tells you nothing you can trust."
        )

    seasons = sorted({m.season for m in rows})
    if len(seasons) < 2:
        raise ValueError(
            f"History covers only season(s) {seasons}. A time-based split "
            "needs at least two so the model can be tested on one it never saw."
        )
    holdout = set(seasons[-test_seasons:])
    train_idx = [i for i, m in enumerate(rows) if m.season not in holdout]
    test_idx = [i for i, m in enumerate(rows) if m.season in holdout]

    Xtr = [X[i] for i in train_idx]; ytr = [y[i] for i in train_idx]
    Xte = [X[i] for i in test_idx];  yte = [y[i] for i in test_idx]

    model = LogisticRegression()
    model.fit(Xtr, ytr)

    correct = sum(1 for xi, yi in zip(Xte, yte) if int(model.predict([xi])[0]) == yi)
    accuracy = correct / len(yte) if yte else 0.0
    # The bar: home advantage costs nothing to exploit.
    baseline = sum(yte) / len(yte) if yte else 0.0

    return {
        "series": series,
        "intercept": float(model.intercept_[0]),
        "form_coef": float(model.coef_[0][0]),
        "exp_coef": None,          # no lineup feed yet — see algorithm.py
        "train_seasons": ",".join(str(s) for s in seasons if s not in holdout),
        "test_seasons": ",".join(str(s) for s in sorted(holdout)),
        "train_samples": len(ytr),
        "test_samples": len(yte),
        "accuracy": accuracy,
        "baseline_accuracy": baseline,
    }
