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
# CLIENT, 22 SEP 2026, answering the contradiction this block used to describe:
#
#   "Locked for 3 years at the founding rate. First year is a stub period —
#    runs from now through the end of the 2027 season, not a standard 12
#    months — then two more full years on top. The founding rate is only
#    available to new sign-ups until end of January; after that the price goes
#    up for new joiners, but existing founding members keep their locked rate
#    regardless."
#
# So there are THREE numbers, not two, and the middle one is the one that was
# missing. The window is when you have to be in by. The stub is the ragged
# first term, which ends on a season boundary rather than twelve months after
# whatever day you happened to sign up. The extra seasons are the full years
# that follow it.
#
# THE LOCK ENDS ON THE SAME DAY FOR EVERYBODY, and that falls out of the stub
# rather than being imposed on top of it: sign up in October or in January and
# your first term still ends with the 2027 season, so the two full seasons
# after it are 2028 and 2029 either way. The old code tried to get the same
# fairness by anchoring to the window's close and adding three years, which
# now that the window crosses into 2027 would have run every lock to 2030.
FOUNDING_WINDOW_CLOSES = date(2027, 1, 31)

# The season the ragged first term runs through. Not a duration — a boundary.
FOUNDING_STUB_SEASON = 2027

# Full seasons on top of the stub. Stub + 2 is the "3 years" the client sells.
FOUNDING_EXTRA_SEASONS = 2

# What the marketing pages call it. One string, so "three" and the arithmetic
# above cannot come to disagree on a page nobody re-reads.
FOUNDING_LOCK_SEASONS = 1 + FOUNDING_EXTRA_SEASONS


def founding_window_open(on: date | None = None) -> bool:
    """Whether an organisation signing up today gets the founding rate."""
    return (on or date.today()) <= FOUNDING_WINDOW_CLOSES


def founding_locked_until(granted_on: date | None = None) -> date:
    """The last day a founding rate holds.

    The same day for every founding member, whenever in the window they came
    in — the stub term ends with the 2027 season for all of them, and the two
    full seasons after it are therefore the same two. ``granted_on`` is kept in
    the signature because callers pass it and because a lock granted after the
    window (which ``grant_founding_rate`` refuses) would otherwise be silently
    back-dated; anything that late gets its stub from its own year instead.
    """
    granted_on = granted_on or date.today()
    stub = max(FOUNDING_STUB_SEASON, granted_on.year)
    return date(stub + FOUNDING_EXTRA_SEASONS, 12, 31)


def founding_stub_ends() -> date:
    """The end of the ragged first term — what "your first year" actually means."""
    return date(FOUNDING_STUB_SEASON, 12, 31)


def founding_seasons() -> list[int]:
    """Every season a founding lock covers, for a page that wants to name them."""
    return list(range(FOUNDING_STUB_SEASON, FOUNDING_STUB_SEASON + FOUNDING_LOCK_SEASONS))


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
    return "500+" if seat_limit >= UNLIMITED_SEATS else f"Up to {seat_limit:,}"


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


# ---------------------------------------------------------------------------
# THE PUBLIC PRICE CARDS
# ---------------------------------------------------------------------------
# CLIENT, 22 SEP 2026, with a screenshot of the home page attached: "just have
# to match the changes to the pricing page."
#
# He was right, and the cause is worth naming because it will happen again
# otherwise. /pricing/ and the home page's pricing teaser were two hand-typed
# copies of the same five plans. Every correction since 16 September — most
# popular moving to Workplace, the groups tags, the founding date — was applied
# to the table on /pricing/ and to nothing else, so the first pricing anybody
# meets on the site still put MOST POPULAR on the $299 plan and offered no
# groups tag at all. Two sources of truth, and the one that got updated was the
# one fewer people read.
#
# So the cards are built from TIERS now. A price, a badge or a tag moves here
# and both pages follow; there is nothing left to forget to mirror.
#
# The home teaser shows the first four and /pricing/ shows all five — that is a
# slice of one list, not a second list.

# What the card says under the price, in the client's voice rather than the
# feature list's. The feature lists above are for the plans screen, which is
# read by somebody who has already decided; these are for somebody deciding.
CARD_BLURBS = {
    STARTER: (
        "The full platform. AFL, AFLW, NRL, NRLW. Charity Vote, live ladder, "
        "the Wall, email reminders. One ladder for everyone."
    ),
    GROWTH: (
        "Everything in Starter, plus your organisation&rsquo;s name and logo and a "
        "round-by-round participation dashboard. Still one shared ladder."
    ),
    PRO: (
        "Everything in Team, plus <b>groups</b> &mdash; a ladder of its own for each "
        "team or department &mdash; priority support and a full CSR Impact Report."
    ),
    ENTERPRISE: (
        "Groups and sub-groups, white-label platform, dedicated account manager, "
        "bespoke onboarding. Make it feel entirely your own."
    ),
    ENTERPRISE_PLUS: (
        "Everything in Organisation. Multi-league setup, API access, flexible "
        "payment terms and full bespoke configuration."
    ),
}

# The icon each card wears, from templates/public/partials/_icons.html. Held
# here rather than in the template for the same reason as everything else in
# this block: so the two pages cannot disagree about which plan is which.
CARD_ICONS = {
    STARTER: "ic-spark",
    GROWTH: "ic-users",
    PRO: "ic-org",
    ENTERPRISE: "ic-globe",
    ENTERPRISE_PLUS: "ic-shield-star",
}


def per_person(tier: str) -> str:
    """The price at the plan's ceiling, which is the best case and says so.

    Rounded to the dollar and prefixed "~", because the exact figure depends on
    how many people actually join and quoting cents on an estimate reads as a
    precision the number does not have. The largest plan says "from" instead:
    at 2,499 people it is under a dollar, and "~$1" would undersell it.
    """
    cfg = TIERS[tier]
    at_ceiling = cfg["price"] / cfg["seat_limit"]
    lead = "from ~" if tier in (ENTERPRISE, ENTERPRISE_PLUS) else "~"
    return f"{lead}${max(1, round(at_ceiling))} per person"


def public_cards(limit: int | None = None) -> list[dict]:
    """The price cards, in size order, ready for a template to print.

    ``limit`` takes the first N — the home page teaser shows four and links to
    /pricing/ for the rest. Everything a card needs is computed here so neither
    template has to know a price, a ceiling or which plan carries the badge.
    """
    cards = []
    for key in TIER_ORDER:
        cfg = TIERS[key]
        cards.append({
            "tier": key,
            "label": cfg["label"],
            "audience": cfg["audience"],
            "price": f"${cfg['price']:,}",
            "seats": seat_limit_label(cfg["seat_limit"]),
            "seat_limit": cfg["seat_limit"],
            "per_person": per_person(key),
            "blurb": CARD_BLURBS[key],
            "icon": CARD_ICONS[key],
            "groups": cfg["groups"],
            "popular": cfg["popular"],
        })
    return cards[:limit] if limit else cards
