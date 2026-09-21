"""GoodTip platform tiers — the one place a plan's price, size and features live.

WHY THE KEYS AND THE LABELS DISAGREE
------------------------------------
The five keys below were written from the original Cost Structure deck, which
called the plans Starter / Growth / Pro / Enterprise / Enterprise+. The site has
since been repriced and renamed twice, and now sells Starter / Team / Workplace
/ Organisation / Enterprise+ at different prices and different seat ceilings.

The KEYS did not change with it, and deliberately so: they are stored in
``PlanSubscription.tier`` on every subscription ever taken out, they are posted
by the plans page as form values, and ``billing.esg`` and ``billing.donations``
both branch on them. Renaming a key means a data migration on live rows to
rename a string that no customer ever sees. So the key is an identifier and the
label is the product name, and this module is where the two are tied together.

Read ``label``, never the key, anywhere a human will see it.

THIS FILE AND THE MARKETING PAGES HAD DRIFTED. Until Sep 2026 the plans screen
offered "Growth — $199, up to 50" while /pricing/ and the home page sold "Team —
$299, up to 50". A member who read the site and then opened billing saw a
different product. The numbers here are now the published ones; if they change
again they change HERE and the two templates follow.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

STARTER = "starter"
GROWTH = "growth"
PRO = "pro"
ENTERPRISE = "enterprise"
ENTERPRISE_PLUS = "enterprise_plus"

# A very large seat ceiling stands in for the deck's "500+" tier.
UNLIMITED_SEATS = 100_000

TIERS = {
    STARTER: {
        "label": "Starter",
        "price": 99,
        "seat_limit": 20,
        "audience": "Small business, small sports club",
        "features": ["1 league", "All four codes", "Charity vote", "Invite link", "Live ladder"],
        # See GROUPS_MIN_SEATS below for why this is False on the two small
        # plans and what a locked group control has to do.
        "groups": False,
        "popular": False,
    },
    GROWTH: {
        "label": "Team",
        "price": 299,
        "seat_limit": 50,
        "audience": "Growing teams, community clubs",
        "features": [
            "Everything in Starter", "Your name and logo on it",
            "Round-by-round participation dashboard",
        ],
        "groups": False,
        "popular": False,
    },
    PRO: {
        "label": "Workplace",
        "price": 799,
        "seat_limit": 150,
        "audience": "Mid-size organisations",
        "features": [
            "Everything in Team", "Groups, each with its own ladder",
            "Priority support", "CSR Impact Report",
        ],
        "groups": True,
        # Client, 18 Sep 2026: "$799 package should be marked most popular"
        "popular": True,
    },
    ENTERPRISE: {
        "label": "Organisation",
        "price": 1299,
        "seat_limit": 500,
        "audience": "Large organisations",
        "features": [
            "Everything in Workplace", "Departments and sub-groups",
            "Dedicated account manager", "Bespoke onboarding",
        ],
        "groups": True,
        "popular": False,
    },
    ENTERPRISE_PLUS: {
        "label": "Enterprise+",
        "price": 1999,
        "seat_limit": 2499,
        "audience": "Large enterprise, multi-site",
        "features": [
            "Everything in Organisation", "Multi-league setup",
            "API access", "Flexible payment terms",
        ],
        "groups": True,
        "popular": False,
    },
}

TIER_CHOICES = [(key, cfg["label"]) for key, cfg in TIERS.items()]

# Plans in size order — used wherever "the next plan up" has to be worked out.
TIER_ORDER = [STARTER, GROWTH, PRO, ENTERPRISE, ENTERPRISE_PLUS]

# ---------------------------------------------------------------------------
# GROUPS — the gate, and where it sits
# ---------------------------------------------------------------------------
# Client, 16 Sep 2026: "if people want the groups/departments functionality,
# they only get it on the $799 plan — under 50 people can have the group button
# but if they click on it it triggers an upgrade."
#
# And the reasoning, in their words: at 20-50 people one shared ladder works
# fine and keeps the comp feeling lively. Once an org is big enough to have
# genuinely separate teams or departments, that is when groups earns its place
# — hence the gate sitting between Team and Workplace.
#
# LOCKED MEANS VISIBLE AND DISABLED, NOT ABSENT. Also theirs, and it is the
# part that is easy to get wrong: "don't just hide the option, they should see
# what they're missing and get a clear upgrade path". A hidden feature teaches
# a customer nothing; a greyed one with a reason next to it is the upgrade
# prompt. Every gate in the codebase therefore renders the control and refuses
# the action — see billing.entitlements.groups_gate.
GROUPS_MIN_TIER = PRO
GROUPS_MIN_SEATS = TIERS[PRO]["seat_limit"]

# ---------------------------------------------------------------------------
# FOUNDING MEMBER PRICE LOCK
# ---------------------------------------------------------------------------
# Sign up before the window closes and the rate you signed up at is the rate
# you are charged at renewal, for three further seasons, whatever the list
# price has done in the meantime.
#
# THE TWO NUMBERS ARE SEPARATE ON PURPOSE. The window is when you have to be in
# by; the lock is how long it then holds for. The client's own brief gave both
# a three-year lock and a "locked through end of 2027" label in the same
# paragraph, which cannot both be true — three seasons from a window closing on
# 31 Dec 2026 is 2027, 2028 and 2029. The three-year figure is implemented,
# because that is what the task asked to be built, and changing the answer is
# changing FOUNDING_LOCK_SEASONS on the line below and nothing else.
FOUNDING_WINDOW_CLOSES = date(2026, 12, 31)
FOUNDING_LOCK_SEASONS = 3


def founding_window_open(on: date | None = None) -> bool:
    """Whether an organisation signing up today gets the founding rate."""
    return (on or date.today()) <= FOUNDING_WINDOW_CLOSES


def founding_locked_until(granted_on: date | None = None) -> date:
    """The last day a founding rate holds.

    Anchored to the END of the window rather than to the day this particular
    organisation signed up, so every founding member's lock expires together
    and nobody is worse off for joining in January than in December. The three
    seasons are counted from the season after the window closes.
    """
    granted_on = granted_on or date.today()
    base = max(granted_on.year, FOUNDING_WINDOW_CLOSES.year)
    return date(base + FOUNDING_LOCK_SEASONS, 12, 31)


def tier_config(tier: str) -> dict:
    return TIERS[tier]


def tier_label(tier: str) -> str:
    """The product name for a stored key, safe for a key no longer offered."""
    cfg = TIERS.get(tier)
    return cfg["label"] if cfg else tier.replace("_", " ").title()


def tier_price(tier: str) -> Decimal:
    return Decimal(TIERS[tier]["price"])


def tier_has_groups(tier: str | None) -> bool:
    """Whether this plan includes groups. An org with no plan does not."""
    cfg = TIERS.get(tier or "")
    return bool(cfg and cfg["groups"])


def tier_for_team_size(team_size: int | None) -> str:
    """The smallest plan that fits this many people.

    Used to answer "which plan is this organisation on" for an org that has
    not paid yet — team size is what the wizard asked for and what the plans
    are sold by, so it is the honest stand-in. Anything over the largest
    published ceiling lands on Enterprise+, which is the plan whose answer is
    "talk to us" anyway.
    """
    if not team_size:
        return STARTER
    for key in TIER_ORDER:
        if team_size <= TIERS[key]["seat_limit"]:
            return key
    return ENTERPRISE_PLUS


def next_tier_with_groups(tier: str | None) -> str:
    """The cheapest plan above ``tier`` that unlocks groups — the upgrade to sell."""
    try:
        start = TIER_ORDER.index(tier) + 1
    except ValueError:
        start = 0
    for key in TIER_ORDER[start:]:
        if TIERS[key]["groups"]:
            return key
    return GROUPS_MIN_TIER


def seat_limit_label(seat_limit: int) -> str:
    return "500+" if seat_limit >= UNLIMITED_SEATS else f"Up to {seat_limit}"


def recommendation(team_size: int | None, *, wants_groups: bool = False) -> dict:
    """The plan to put in front of somebody setting an organisation up.

    CLIENT, 17 SEP 2026: "with the groups not being available to the small
    accounts — where in the process do they select their plan or get a
    recommended package? Is it when they get asked the size of the group, in
    the org sign-up process?"

    It was nowhere. The wizard asked how many people and then never mentioned a
    price again; the first time anybody saw one was the plans screen, after the
    organisation existed. Worse, the wizard asks whether to switch GROUPS on
    two screens before it asks how big the organisation is — so it was possible
    to say yes to a feature, never be told what it costs, and find out on a
    locked button afterwards.

    Two inputs, because size alone gives the wrong answer for the one case the
    client raised: a forty-person workplace that wants groups is not on the
    forty-person plan. Size sets the floor, groups raise it, and the caller is
    told WHICH of the two decided it so the screen can say why.

    Returns everything a template needs and nothing it has to compute:
    ``tier``, ``label``, ``price``, ``seats``, ``has_groups``, ``raised_by_
    groups`` and ``is_top``.
    """
    by_size = tier_for_team_size(team_size)
    tier = by_size
    if wants_groups and not tier_has_groups(tier):
        tier = next_tier_with_groups(tier)
    cfg = TIERS[tier]
    return {
        "tier": tier,
        "label": cfg["label"],
        "price": cfg["price"],
        "seats": seat_limit_label(cfg["seat_limit"]),
        "has_groups": cfg["groups"],
        # True when it is groups, not headcount, that landed them here — the
        # one thing a person seeing a bigger number than they expected will
        # want explained, and the one that is genuinely their choice to change.
        "raised_by_groups": tier != by_size,
        "is_top": tier == ENTERPRISE_PLUS,
    }
