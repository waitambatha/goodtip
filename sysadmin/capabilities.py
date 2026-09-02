"""Everything an administrator can be given permission to do.

WHY THIS FILE EXISTS
--------------------
Until now the control plane had exactly two states: `is_superuser`, which is
every power in the product, or nothing. That is fine while the only admin is
the person who owns the company. It stops being fine the moment they want
somebody to write the blog — because the only way to let them write the blog
was to also let them delete every member, edit every payment record and drop
an organisation.

So the powers are named here, one per capability, and an administrator holds
the ones they have been given. A capability is a thing a person would say out
loud ("reply to enquiries"), not a Django model permission — nobody sensible
grants `admin_panel.change_enquiry` and `admin_panel.view_enquiry` separately,
and a screen listing forty rows like that is not a screen anyone can consent
to. The unit here is the job, not the table.

TWO WAYS TO HOLD ONE
--------------------
Each granted capability is either DIRECT — they do it and it happens — or
REVIEWED, where doing it raises a change request that a full-access
administrator has to approve first. That distinction is the point of the
feature: "write the blog, but I see it before members do" is the actual thing
being asked for, and it is not expressible as a permission on its own.

WHAT IS NOT HERE
----------------
Managing administrators. Creating admins, granting capabilities and approving
change requests belong to full access and cannot be delegated — an admin who
could grant themselves capabilities has full access by another name, and one
who could approve their own change requests has no reviewer.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Capability:
    key: str
    label: str
    # Written for the person ticking the box, in the second person, saying what
    # the holder will be able to do — not what the code checks.
    detail: str
    group: str
    # Whether "needs my approval first" is a coherent option for this one.
    # Reading something cannot be reviewed after the fact, and a data sync has
    # no draft state to hold, so those are always direct.
    reviewable: bool = True
    # Shown with a warning in the picker. Not a different check — a different
    # amount of thought before ticking it.
    sensitive: bool = False


@dataclass(frozen=True)
class Group:
    key: str
    label: str
    blurb: str
    capabilities: list


NEWS = Group("news", "News & blog", "Writing and publishing stories.", [
    Capability(
        "news.write", "Write and edit stories",
        "Create new posts and edit existing ones, including pictures and formatting.",
        "news",
    ),
    Capability(
        "news.publish", "Publish and unpublish",
        "Decide whether a story is visible to members, and take one back down.",
        "news",
    ),
    Capability(
        "news.email", "Email a story to every member",
        "Send a published story to every member's inbox. This cannot be undone "
        "once it has gone out.",
        "news", sensitive=True,
    ),
    Capability(
        "news.delete", "Delete stories",
        "Remove a post permanently, including one somebody else wrote.",
        "news", sensitive=True,
    ),
])

PAGES = Group("pages", "Pages", "The words and pictures on the site itself.", [
    Capability(
        "pages.edit", "Change the wording on any page",
        "Edit the text on the public pages and the members-only pages.",
        "pages",
    ),
    Capability(
        "pages.images", "Replace pictures on a page",
        "Swap the photographs used on any page.",
        "pages",
    ),
    Capability(
        "pages.revert", "Put a page back to its original wording",
        "Undo every change made to a page at once.",
        "pages", sensitive=True,
    ),
])

SEO = Group("seo", "SEO", "How the site appears in Google and on social.", [
    Capability(
        "seo.edit", "Edit SEO settings",
        "Set the title, description, share preview, canonical address and "
        "whether a page or story is indexed by Google.",
        "seo",
    ),
    Capability(
        "seo.redirects", "Manage redirects",
        "Point an address that no longer exists at the page that replaced it.",
        "seo",
    ),
])

ENQUIRIES = Group("enquiries", "Enquiries", "Messages from the public contact form.", [
    Capability(
        "enquiries.read", "Read enquiries",
        "Open the inbox and read what people have sent.",
        "enquiries", reviewable=False,
    ),
    Capability(
        "enquiries.reply", "Reply to enquiries",
        "Answer an enquiry. The reply is emailed to the person who wrote in.",
        "enquiries",
    ),
])

ORGS = Group("orgs", "Organisations", "The groups running a season.", [
    Capability(
        "orgs.view", "View organisations",
        "See every organisation, its members and its settings.",
        "orgs", reviewable=False,
    ),
    Capability(
        "orgs.edit", "Edit organisations",
        "Change an organisation's name, season, competitions or charity.",
        "orgs",
    ),
    Capability(
        "orgs.approve", "Approve new organisations and join requests",
        "Let a waiting organisation or member through.",
        "orgs",
    ),
])

PEOPLE = Group("people", "People", "The accounts of everyone using GoodTip.", [
    Capability(
        "people.view", "View members",
        "See member accounts, their email addresses and when they last signed in.",
        "people", reviewable=False,
    ),
    Capability(
        "people.edit", "Edit member accounts",
        "Change somebody's name, email address or account settings.",
        "people", sensitive=True,
    ),
    Capability(
        "people.delete", "Delete member accounts",
        "Remove an account and everything attached to it. This cannot be undone.",
        "people", sensitive=True,
    ),
])

CHARITIES = Group("charities", "Charities", "The causes organisations raise for.", [
    Capability(
        "charities.edit", "Add and edit charities",
        "Put a new charity on the list, or correct one already on it.",
        "charities",
    ),
    Capability(
        "charities.approve", "Approve charities",
        "Let a charity an organisation has added become selectable by everyone.",
        "charities",
    ),
])

DATA = Group("data", "Fixtures & reports", "Where the sport data comes from.", [
    Capability(
        "data.sync", "Run fixture syncs",
        "Pull the latest draws, teams and results from the sports feeds.",
        "data", reviewable=False,
    ),
    Capability(
        "data.report", "See the system report",
        "The platform-wide numbers: sign-ups, organisations, tips, mail.",
        "data", reviewable=False,
    ),
])

GROUPS = [NEWS, PAGES, SEO, ENQUIRIES, ORGS, PEOPLE, CHARITIES, DATA]

ALL = {c.key: c for g in GROUPS for c in g.capabilities}
KEYS = list(ALL)

# The ones where "and I want to see it before it happens" is offerable.
REVIEWABLE = [k for k, c in ALL.items() if c.reviewable]


def get(key):
    return ALL.get(key)


def label(key) -> str:
    cap = ALL.get(key)
    return cap.label if cap else key


def group_of(key):
    cap = ALL.get(key)
    if not cap:
        return None
    return next((g for g in GROUPS if g.key == cap.group), None)


def valid(keys) -> list:
    """Only the keys this build actually knows about.

    Capabilities are code, and code changes: a grant saved against a capability
    that has since been renamed must not silently become a grant for something
    else, and must not crash a page that lists it. It is dropped.
    """
    return [k for k in keys if k in ALL]
