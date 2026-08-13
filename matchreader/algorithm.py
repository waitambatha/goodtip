"""MatchReader: the signals, the fit, and the words shown to a tipper.

Implements the algorithm deck. Two signals feed a logistic regression, whose
output is never shown as a number — only as a tier, always with the leading
team's name beside it.

    Form  = wins in last 5 completed matches / 5
    dForm = Form(home) - Form(away)

    Exp   = (sum of career games, named 17) / 17
    dExp  = Exp(home) - Exp(away)

    z     = b0 + b1*dForm + b2*dExp
    P     = 1 / (1 + e^-z)

WHAT IS AND IS NOT BUILT
------------------------
Signal 1 (Recent Form) is complete: it needs only match results, which the
scrapers now supply.

Signal 2 (Squad Experience) is NOT. It needs the named starting 17 and each
player's career game count, and nothing in this project collects either yet.
The deck is explicit that it is worth 60.5% standalone and lifts the pair to
62.9%, so leaving it out costs real accuracy. It is wired through every
signature as an optional feature rather than stubbed with a zero, so that
adding the collector is a change to the data layer and not to the model:
``exp_coef`` stays NULL and the fit runs on one feature until it exists.

The deck's own numbers are the target to beat, not a claim about this build:
62.9% NRL out-of-sample for both signals, 62.2% for form alone.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

FORM_WINDOW = 5

# Deck slide 9. Symmetric, and applied to whichever side holds the higher
# probability, so the scale below is read on max(P, 1-P).
TIERS = [
    (0.85, "Dominant Position"),
    (0.75, "Strong Position"),
    (0.65, "Clear Edge"),
    (0.55, "Slight Edge"),
    (0.00, "Even Contest"),
]


@dataclass(frozen=True)
class Prediction:
    """What the tipping screen is allowed to know.

    ``probability`` is here for logging and evaluation. The deck forbids
    showing it, which is why ``label`` is what the template renders and why
    there is no formatter on this class that returns a percentage.
    """

    probability: float          # P(home win), 0..1
    leader: str                 # team name holding the higher probability
    tier: str                   # "Clear Edge"
    home_leads: bool

    @property
    def label(self) -> str:
        """The whole of what a tipper sees: "Clear Edge: Panthers"."""
        return f"{self.tier}: {self.leader}"

    @property
    def is_even(self) -> bool:
        return self.tier == "Even Contest"

    @property
    def strength(self) -> int:
        """The tier as 0–4, for drawing it rather than only naming it.

        Says nothing the tier name doesn't already: it is the position of the
        tier on the scale above, so a meter built from it is the same verdict
        in a second form. Not derived from ``probability``, deliberately — a
        bar whose length tracked P would be the forbidden number, drawn.
        """
        for i, (_, name) in enumerate(reversed(TIERS)):
            if name == self.tier:
                return i
        return 0


def tier_for(probability: float) -> str:
    """Tier for a probability, read on the leading side."""
    lead = max(probability, 1.0 - probability)
    for floor, name in TIERS:
        if lead >= floor:
            return name
    return "Even Contest"


def recent_form(results: list[bool]) -> float:
    """Wins over the last FORM_WINDOW completed matches, most recent first.

    ``results`` is already filtered to completed games — byes and washouts do
    not count toward the five, per the deck, which is the caller's job because
    only it knows what a bye looks like in its own data.

    A side with fewer than five games behind it (round 1, or an expansion
    club) is scored on what it has. Padding with losses would invent form; the
    honest read of "three from three" is 1.0, and the model learns how much to
    trust a small window from the fitted coefficient.
    """
    window = results[:FORM_WINDOW]
    if not window:
        return 0.5          # no history at all: no evidence either way
    return sum(1 for won in window if won) / len(window)


def squad_experience(career_games: list[int]) -> float | None:
    """Average career games across the named starting side.

    Returns None when the lineup is not published, which is the normal case
    today: no feed in this project supplies team lists yet. None propagates
    all the way to the fit, where it means "train on form alone" rather than
    "this side has no experience", a difference that would otherwise be
    invisible and badly wrong.
    """
    if not career_games:
        return None
    return sum(career_games) / len(career_games)


def logistic(z: float) -> float:
    # Guarded against overflow on extreme z, which a small training set can
    # produce before the coefficients settle.
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-min(z, 60.0)))
    e = math.exp(max(z, -60.0))
    return e / (1.0 + e)


def predict(
    *,
    intercept: float,
    form_coef: float,
    delta_form: float,
    home_name: str,
    away_name: str,
    exp_coef: float | None = None,
    delta_exp: float | None = None,
) -> Prediction:
    """One fixture, from fitted coefficients to the words on the screen."""
    z = intercept + form_coef * delta_form
    if exp_coef is not None and delta_exp is not None:
        z += exp_coef * delta_exp
    p_home = logistic(z)
    home_leads = p_home >= 0.5
    return Prediction(
        probability=p_home,
        leader=home_name if home_leads else away_name,
        tier=tier_for(p_home),
        home_leads=home_leads,
    )
