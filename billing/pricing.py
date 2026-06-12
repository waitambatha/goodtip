"""GoodTip platform tiers (Cost Structure deck, slide 3).

Phase 1: a single platform service fee per season, fixed by tier. Prices in AUD.
"""

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
        "features": ["1 league", "NRL or AFL", "Charity vote", "Invite link", "Basic dashboard"],
        "popular": False,
    },
    GROWTH: {
        "label": "Growth",
        "price": 199,
        "seat_limit": 50,
        "audience": "Mid-market, community clubs",
        "features": ["Everything in Starter", "Up to 3 leagues", "Captain tools", "Participation tracking", "Donation progress bar"],
        "popular": True,
    },
    PRO: {
        "label": "Pro",
        "price": 499,
        "seat_limit": 200,
        "audience": "Corporate, larger organisations",
        "features": ["Everything in Growth", "Unlimited leagues", "Org dashboard", "ESG report export", "Matching tools", "Multiple sports"],
        "popular": False,
    },
    ENTERPRISE: {
        "label": "Enterprise",
        "price": 999,
        "seat_limit": 500,
        "audience": "Large corporate, ASX-listed",
        "features": ["Everything in Pro", "Multiple divisions", "Priority support", "Custom charity nomination", "Advanced analytics"],
        "popular": False,
    },
    ENTERPRISE_PLUS: {
        "label": "Enterprise+",
        "price": 1999,
        "seat_limit": UNLIMITED_SEATS,
        "audience": "Telstra-scale organisations",
        "features": ["Everything in Enterprise", "API access", "Custom branding", "Multi-sport simultaneous", "Dedicated onboarding"],
        "popular": False,
    },
}

TIER_CHOICES = [(key, cfg["label"]) for key, cfg in TIERS.items()]


def tier_config(tier: str) -> dict:
    return TIERS[tier]


def seat_limit_label(seat_limit: int) -> str:
    return "500+" if seat_limit >= UNLIMITED_SEATS else f"Up to {seat_limit}"
