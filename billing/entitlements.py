"""What an organisation's plan lets it do, and what to say when it doesn't.

ONE ANSWER, ASKED FROM FOUR PLACES. Groups are gated by plan (see
``pricing.GROUPS_MIN_TIER``) and the gate has to hold in the org-creation
wizard, on the groups directory, in the manage panel and in the service that
actually writes a Group — four screens and one write path, which is four
chances to disagree about whether an organisation is allowed. They all call
``groups_gate``.

AND THE REFUSAL IS PART OF THE ANSWER. The client was explicit that a locked
feature must be visible: "don't just hide the option, they should see what
they're missing and get a clear upgrade path". So the gate does not return a
bool. It returns the bool plus the sentence to show, the plan they are on, the
plan that unlocks it, what it costs and where to go — everything a template
needs to draw a disabled control with a real reason beside it, without any
screen having to know the pricing table.
"""
from __future__ import annotations

from dataclasses import dataclass

from django.urls import reverse

from . import pricing


@dataclass(frozen=True)
class Gate:
    """One feature, one organisation, one answer."""

    allowed: bool
    #: Key of the plan the org is treated as being on right now.
    current_tier: str
    #: Key of the cheapest plan that unlocks the feature.
    required_tier: str
    #: Whether a paid subscription decided this, or the org's stated team size.
    from_subscription: bool

    @property
    def current_label(self) -> str:
        return pricing.tier_label(self.current_tier)

    @property
    def required_label(self) -> str:
        return pricing.tier_label(self.required_tier)

    @property
    def required_price(self) -> int:
        return pricing.TIERS[self.required_tier]["price"]

    @property
    def required_seats(self) -> int:
        return pricing.TIERS[self.required_tier]["seat_limit"]

    # Truthiness is the allowed flag, so `if gate:` reads the way a caller
    # expects and a template can write `{% if gate %}` without reaching for
    # `.allowed`. Every other attribute is only interesting when it is False.
    def __bool__(self) -> bool:
        return self.allowed


def current_tier(org) -> tuple[str, bool]:
    """The plan this organisation counts as being on, and whether it paid for it.

    A paid subscription is the answer whenever there is one. Where there is
    not — and for most of the platform's life so far there is not, because
    Stripe keys are not in the environment yet — the organisation's SIZE
    decides, since size is the only thing the plans are sold by.

    AND SIZE IS THE LARGER OF TWO NUMBERS: what the wizard was told, and how
    many people are actually in it. Neither alone is safe to trust:

      * team_size is nullable, and hundreds of organisations were created
        before the wizard asked for it. Reading it alone would put every one
        of them on Starter and lock a three-hundred-person comp out of the
        departments it is already running.
      * the member count alone would gate an organisation that has bought a
        Workplace plan for four hundred people but only invited twelve of them
        so far, which is the first week of every rollout.

    Taking the larger answers both, and errs toward letting an organisation in
    — which is the right direction to err when the alternative is telling a
    paying customer their feature has disappeared.
    """
    from .services import active_subscription

    sub = active_subscription(org)
    if sub is not None:
        return sub.tier, True
    size = max(org.team_size or 0, org.members.count())
    return pricing.tier_for_team_size(size), False


def groups_gate(org) -> Gate:
    """Whether ``org`` may create groups, and what to say if not."""
    tier, paid = current_tier(org)
    return Gate(
        allowed=pricing.tier_has_groups(tier),
        current_tier=tier,
        required_tier=pricing.next_tier_with_groups(tier),
        from_subscription=paid,
    )


def groups_upgrade_url(org) -> str:
    """Where the upgrade prompt sends somebody who wants groups.

    The organisation's own plans screen, not the public pricing page: they are
    already signed in and already have an org, so the useful destination is the
    one with a button on it, not the one with a table.
    """
    return reverse("billing:plans", args=[org.pk])


def groups_context(org) -> dict:
    """Template context for any screen that draws a group control.

    Every such screen wants the same three things, so they are assembled once:
    the gate, the destination, and the plain sentence. The sentence lives here
    rather than in four templates because it is the feature's explanation and
    it should not be possible for three of them to be updated and one missed.
    """
    gate = groups_gate(org)
    if gate.allowed:
        blurb = ""
    else:
        blurb = (
            f"Groups are part of {gate.required_label} "
            f"(${gate.required_price:,}/year, up to {gate.required_seats} people). "
            f"You're on {gate.current_label}. One shared ladder keeps a comp this "
            "size lively — groups earn their place once you have genuinely "
            "separate teams or departments."
        )
    return {
        "groups_gate": gate,
        "groups_upgrade_url": groups_upgrade_url(org),
        "groups_locked_blurb": blurb,
    }


class GroupsLocked(ValueError):
    """Raised by the write path when an org's plan does not include groups.

    Carries the gate so the view catching it can render the same upgrade
    prompt the disabled control would have shown, rather than a bare error.

    A ValueError ON PURPOSE. ``orgs.services.create_group`` already signals
    every other refusal that way and its callers all catch ValueError and flash
    ``str(e)``. Subclassing means a caller that has not been taught about plans
    still shows the member a correct sentence instead of raising a 500, and a
    caller that has been taught catches this class first and draws the full
    upgrade prompt. Neither behaviour depends on the other being updated.
    """

    def __init__(self, gate: Gate, org):
        self.gate = gate
        self.org = org
        super().__init__(
            f"Groups are part of {gate.required_label}. "
            f"{org.name} is on {gate.current_label}."
        )


def require_groups(org) -> Gate:
    """Gate the write path. Raises GroupsLocked rather than returning False.

    The templates disable the control; this is what stops a POST that skipped
    them. A gate that only exists in a template is a suggestion.
    """
    gate = groups_gate(org)
    if not gate.allowed:
        raise GroupsLocked(gate, org)
    return gate
