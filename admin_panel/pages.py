"""The pages an admin is allowed to rewrite the words on.

WHY A REGISTRY AND NOT "EVERY URL"
----------------------------------
The editor rewrites the HTML of a response on its way out. Left open, that
would take in the Stripe webhook, the HTMX partials, the admin itself and
every JSON endpoint — places where "edit the wording" is meaningless and where
parsing the body on every request buys nothing. So the editable pages are
named here, once, and every other response is passed through untouched.

The split is the one the client asked for: the pages anyone can reach, and the
pages you only see after signing in.

KEYED ON THE VIEW NAME, NOT THE PATH
------------------------------------
Half the signed-in pages carry an organisation id — /org/12/tips/,
/billing/12/plans/ — so there is no one path to match on, and an edit made
while standing in one organisation is meant for the page, not for that
organisation's copy of it. `request.resolver_match.view_name` is the same
string for all of them, so that is the key.
"""
from dataclasses import dataclass, field


GROUP_PUBLIC = "public"
GROUP_PRIVATE = "private"


@dataclass(frozen=True)
class Page:
    key: str
    label: str
    # The namespaced URL name, which is exactly what resolver_match.view_name
    # holds — "dashboard", "tipping:my_tips".
    view_name: str
    group: str
    blurb: str = ""
    # Which URL arguments this page needs before it can be opened. Only ever
    # ("org_id",) so far; named rather than assumed so the manage page can say
    # "pick a group first" instead of raising NoReverseMatch.
    needs: tuple = field(default=())

    @property
    def is_public(self) -> bool:
        return self.group == GROUP_PUBLIC


PAGES = [
    # ---- Public: no sign-in needed ----
    Page("home", "Home", "landing", GROUP_PUBLIC,
         "The landing page — the first thing anyone sees."),
    Page("how_it_works", "How it works", "how_it_works", GROUP_PUBLIC,
         "The explainer: what GoodTip is and how a group runs a season."),
    Page("pricing", "Pricing", "pricing", GROUP_PUBLIC,
         "Plans and what each one includes."),
    Page("wall", "The Wall", "wall", GROUP_PUBLIC,
         "The public feed of posts groups chose to share."),
    Page("good_list", "The Good List", "good_list", GROUP_PUBLIC,
         "The public leaderboard of groups and what they have raised."),
    Page("news_index", "News & blog list", "news_index", GROUP_PUBLIC,
         "The wrapper around the story list. The stories themselves are "
         "written in News & blog."),
    Page("tell_the_boss", "Tell the boss", "tell_the_boss", GROUP_PUBLIC,
         "The form for nudging your workplace into signing up."),
    Page("coming_soon", "Coming soon", "coming_soon", GROUP_PUBLIC,
         "The holding page for anything not open yet."),

    # ---- Private: signed-in members only ----
    Page("dashboard", "Dashboard", "dashboard", GROUP_PRIVATE,
         "Where a member lands after signing in."),
    Page("profile", "Profile", "profile", GROUP_PRIVATE,
         "Name, photo, password and the two-step switch."),
    Page("org_search", "Find a group", "orgs:search", GROUP_PRIVATE,
         "Search for a group and ask to join it."),
    Page("org_create", "Create a group", "orgs:create", GROUP_PRIVATE,
         "The wizard someone walks to set an organisation up."),
    Page("my_tips", "My tips", "tipping:my_tips", GROUP_PRIVATE,
         "A member's own tips, round by round.", needs=("org_id",)),
    Page("tip_round", "Tipping a round", "tipping:tip_round", GROUP_PRIVATE,
         "The page where tips are actually put in.", needs=("org_id", "round_id")),
    Page("leaderboard", "Group leaderboard", "tipping:leaderboard", GROUP_PRIVATE,
         "How the tippers in one group are ranked.", needs=("org_id",)),
    Page("members", "Group members", "orgs:members", GROUP_PRIVATE,
         "The member list and the join queue.", needs=("org_id",)),
    Page("org_wall", "Group wall", "orgs:wall", GROUP_PRIVATE,
         "The group's own feed.", needs=("org_id",)),
    Page("charity_vote", "Charity vote", "orgs:charity_vote", GROUP_PRIVATE,
         "Where a group votes on who it is raising for.", needs=("org_id",)),
    Page("billing_plans", "Plans & billing", "billing:plans", GROUP_PRIVATE,
         "What an organisation is on and what it costs.", needs=("org_id",)),
]

BY_KEY = {p.key: p for p in PAGES}
BY_VIEW_NAME = {p.view_name: p for p in PAGES}


def public_pages():
    return [p for p in PAGES if p.group == GROUP_PUBLIC]


def private_pages():
    return [p for p in PAGES if p.group == GROUP_PRIVATE]


def page_for_request(request):
    """The Page this response belongs to, or None.

    resolver_match is set by the time a response comes back through the
    middleware, but not on a 404 raised before routing — hence the guard.
    """
    match = getattr(request, "resolver_match", None)
    if match is None:
        return None
    return BY_VIEW_NAME.get(match.view_name)
