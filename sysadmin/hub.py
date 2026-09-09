"""What every table in the control plane is FOR, and who should open it.

WHY THIS FILE EXISTS
--------------------
The menu used to carry ten disclosure groups, each opening onto a list of
model names. That is a filing cabinet: it answers "where does Season live?"
and nothing else, and it answers it by making you open ten drawers. Worse, a
name could appear twice — "News & blog" was both the HQ editor and the
`newspost` table, side by side in the same rail, with nothing to say which one
published a story and which one showed you rows.

So the rail is flat now, and depth moved into hub screens: one menu item, one
page of cards, click a card to go a level down. This module is the map those
screens are built from.

A CATEGORY IS A JOB, NOT AN APP
-------------------------------
`available_apps` groups by Django app label, which is an implementation
detail — it puts Charity under `catalog` and CharityVote under `orgs`, though
anyone looking for either is doing the same job. The groups here are named for
the work: "People & accounts", "Charities & giving", "Reference lists".

Nothing here grants access. Rows are matched against `available_apps`, which
Django has already filtered by permission, so a category renders only the
tables this particular administrator was going to be shown anyway — and a
category left with no tables is dropped rather than shown empty.

ANYTHING NOT LISTED STILL APPEARS. Unclaimed tables collect in a final
"Everything else" category, so registering a model and forgetting this file
costs a tidy heading, not access to the data.
"""
from dataclasses import dataclass, field


# The categorical ramp from gtadmin.css. Named rather than spelled out so a
# palette change lands in one stylesheet instead of thirty-one string literals.
GREEN = "var(--gt-c1)"
TEAL = "var(--gt-c2)"
AMBER = "var(--gt-c3)"
OCEAN = "var(--gt-c4)"
PLUM = "var(--gt-c5)"
RUST = "var(--gt-c6)"
GOLD = "var(--gt-gold)"

# What to write ON an accent when it is used as a solid fill.
#
# Not a detail. White on --gt-gold (#E9A81C) is 1.9:1 and on --gt-c3 (#C77C1E)
# is 3.1:1 — both well under the 4.5:1 that 14px bold text needs, so the
# "Open table" button on the two warm categories would have been a label you
# squint at. The other five are dark enough for white. CSS cannot branch on a
# colour value, so the pairing is declared here and travels with the card.
DARK_INK = "var(--gt-deep)"
LIGHT_INK = "#FFFFFF"
INK = {GOLD: DARK_INK, AMBER: DARK_INK}


@dataclass(frozen=True)
class Table:
    """One registered model, described for somebody who has not met it.

    `blurb` is the sentence on the card. It says what the rows ARE, in the
    words a person would use out loud — "every account that can sign in", not
    "the accounts.User model" — because the name on its own is exactly the
    thing that was already failing to explain anything.

    `accent` is per-table on purpose. A wall of thirty identically-coloured
    cards is a wall; giving each table its own colour means the one you open
    every day is findable by shape before you have read a word of it, and the
    same colour then follows you onto the table screen itself.
    """
    ref: str
    blurb: str
    accent: str = GREEN
    icon: str = "ic-doc"


@dataclass(frozen=True)
class Category:
    key: str
    label: str
    blurb: str
    icon: str
    accent: str
    tables: list = field(default_factory=list)


CATEGORIES = [
    Category(
        "people", "People & accounts",
        "Everyone who can sign in, and everyone who asked to.",
        "ic-f-users", GREEN, [
            Table("accounts.user",
                  "Every account on GoodTip — members, staff and administrators alike.",
                  GREEN, "ic-users"),
            Table("accounts.launchsignup",
                  "Addresses left on the pre-launch page, before accounts existed.",
                  TEAL, "ic-send"),
            Table("auth.group",
                  "Django's own permission bundles. Rarely the thing you want — "
                  "administrator powers live under Your team.",
                  PLUM, "ic-people"),
        ],
    ),
    Category(
        "orgs", "Organisations & groups",
        "The clubs, workplaces and schools that tip together.",
        "ic-f-org", GOLD, [
            Table("orgs.organisation",
                  "Every organisation, its type, its state and who runs it.",
                  GOLD, "ic-org"),
            Table("orgs.membershiprequest",
                  "People waiting to be let into an organisation.",
                  AMBER, "ic-org-add"),
            Table("orgs.group",
                  "The smaller rooms inside an organisation — a department, a team, a year level.",
                  TEAL, "ic-people"),
            Table("orgs.groupmember",
                  "Who sits in which of those rooms.",
                  OCEAN, "ic-users"),
            Table("billing.plansubscription",
                  "What each organisation is paying for. Stripe is switched off on staging.",
                  PLUM, "ic-coins"),
        ],
    ),
    Category(
        "tipping", "Tipping",
        "The picks themselves, and the fixtures they are made against.",
        "ic-f-target", GREEN, [
            Table("tipping.tip",
                  "Every pick every member has made. The biggest table here.",
                  GREEN, "ic-target"),
            Table("tipping.match",
                  "Individual fixtures — who played whom, when, and the result.",
                  OCEAN, "ic-match"),
            Table("tipping.round",
                  "The rounds matches are grouped into, and when each one locks.",
                  TEAL, "ic-calendar"),
            Table("tipping.team",
                  "The clubs that play. Names, colours and logos.",
                  RUST, "ic-flag"),
        ],
    ),
    Category(
        "comps", "Competitions & fixtures",
        "The sports and seasons everything above hangs off.",
        "ic-f-trophy", GOLD, [
            Table("catalog.competition",
                  "NRL, AFL, AFLW and the rest — one row per competition.",
                  GOLD, "ic-trophy"),
            Table("catalog.sport",
                  "The sport a competition belongs to, and the colours it carries.",
                  RUST, "ic-flag"),
            Table("catalog.season",
                  "Each competition's years, and which one is current.",
                  TEAL, "ic-calendar"),
            Table("catalog.series",
                  "Multi-match series that sit outside the normal season.",
                  PLUM, "ic-trophy"),
        ],
    ),
    Category(
        "wall", "The Wall",
        "What members post to each other, and what is waiting to be approved.",
        "ic-f-msg", TEAL, [
            Table("orgs.wallpost",
                  "Posts on an organisation's wall.",
                  TEAL, "ic-msg"),
            Table("orgs.wallreply",
                  "Replies to those posts. Held ones need approving before anyone sees them.",
                  OCEAN, "ic-send"),
        ],
    ),
    Category(
        "charities", "Charities & giving",
        "Who the money goes to, and how members chose them.",
        "ic-f-heart", RUST, [
            Table("catalog.charity",
                  "Every charity on the Good List.",
                  RUST, "ic-heart"),
            Table("orgs.charityvote",
                  "An organisation's election for which charity it backs.",
                  AMBER, "ic-vote"),
            Table("orgs.charityvoteballot",
                  "The individual ballots cast in those elections.",
                  PLUM, "ic-vote"),
        ],
    ),
    Category(
        "content", "Stories & pages",
        "The rows behind the site's words. Writing happens in HQ, not here.",
        "ic-f-doc", TEAL, [
            Table("admin_panel.newspost",
                  "The raw rows behind every story. To write or publish one, "
                  "use News & blog in HQ.",
                  TEAL, "ic-doc"),
            Table("admin_panel.pageseo",
                  "Titles, descriptions and share images, one row per page.",
                  OCEAN, "ic-globe"),
            Table("admin_panel.redirect",
                  "Old addresses pointed at new ones, so no link ever dies.",
                  PLUM, "ic-link"),
        ],
    ),
    Category(
        "reference", "Reference lists",
        "The short fixed lists the sign-up forms and filters are built from.",
        "ic-f-sliders", OCEAN, [
            Table("catalog.state",
                  "Australian states and New Zealand regions.",
                  OCEAN, "ic-pin"),
            Table("catalog.organisationtype",
                  "Club, workplace, school — what kinds of organisation can sign up.",
                  TEAL, "ic-sliders"),
            Table("catalog.subcategory",
                  "The finer split inside each of those types.",
                  PLUM, "ic-sliders"),
            Table("catalog.goodlistconfig",
                  "Settings for how the Good List is put together.",
                  AMBER, "ic-sliders"),
        ],
    ),
    Category(
        "data", "Data & sync",
        "Where fixtures and results come from, and whether the last run worked.",
        "ic-cloud-sync", OCEAN, [
            Table("data_sync.syncrun",
                  "One row per sync. Open the Sync panel in HQ to start one.",
                  OCEAN, "ic-sync"),
        ],
    ),
]

# Security is deliberately NOT a category above. It has its own menu item and
# its own hub, because "who signed in, and who changed what" is a question
# asked on its own — not while browsing tables — and burying it eleventh in a
# grid of data categories is how it never gets looked at.
SECURITY_TABLES = [
    Table("sysadmin.loginevent",
          "Every sign-in attempt, successful or not, with the address it came from.",
          GREEN, "ic-shield"),
    Table("sysadmin.auditlog",
          "Every record added, changed or deleted through the admin — what, when, and by whom.",
          GOLD, "ic-clock"),
    Table("sysadmin.stresstestrun",
          "Load tests that have been run against the platform.",
          PLUM, "ic-flask"),
]

FALLBACK = Category(
    "other", "Everything else",
    "Registered tables that have not been filed anywhere yet.",
    "ic-sliders", PLUM, [],
)

# Ref -> Table, for the accent lookup that colours a table's own screen.
BY_REF = {t.ref: t for c in CATEGORIES for t in c.tables}
BY_REF.update({t.ref: t for t in SECURITY_TABLES})


# ---------------------------------------------------------------------------
# Turning the map above into something a template can render
# ---------------------------------------------------------------------------

def flatten(available_apps):
    """`available_apps` as {"app_label.modelname": row}.

    Django spells the model's own name `object_name` on some versions and
    `model` on others; the lowercased object name is stable across both.
    """
    flat = {}
    for app in available_apps or []:
        label = app.get("app_label", "")
        for model in app.get("models", []):
            name = (model.get("object_name") or "").lower()
            if not name:
                continue
            row = dict(model)
            row["ref"] = f"{label}.{name}"
            row["app_label"] = label
            row["app_name"] = app.get("name", "")
            flat[row["ref"]] = row
    return flat


def _card(row, table, path=""):
    """One table, as the card template wants it."""
    url = row.get("admin_url") or ""
    accent = table.accent if table else PLUM
    return {
        "ref": row["ref"],
        "name": row.get("name") or row["ref"],
        "blurb": table.blurb if table else "",
        "accent": accent,
        "ink": INK.get(accent, LIGHT_INK),
        "icon": table.icon if table else "ic-doc",
        "url": url,
        "add_url": row.get("add_url") or "",
        "app_name": row.get("app_name", ""),
        "is_current": bool(url and path.startswith(url)),
    }


def categories(available_apps, path=""):
    """Every data category this administrator can see, security excluded.

    Security's tables are filtered out even though they are registered: they
    belong to the Security hub, and a table reachable from two menu items is
    the duplication this whole restructure exists to remove.
    """
    flat = flatten(available_apps)
    security_refs = {t.ref for t in SECURITY_TABLES}
    claimed = set(security_refs)
    out = []

    for cat in CATEGORIES:
        cards = []
        for table in cat.tables:
            row = flat.get(table.ref)
            if row is None:
                continue        # not registered, or not this user's to see
            claimed.add(table.ref)
            cards.append(_card(row, table, path))
        if not cards:
            continue            # an empty heading is worse than no heading
        out.append({
            "key": cat.key, "label": cat.label, "blurb": cat.blurb,
            "icon": cat.icon, "accent": cat.accent,
            "tables": cards, "count": len(cards),
            "is_current": any(c["is_current"] for c in cards),
        })

    leftovers = sorted(
        (flat[ref] for ref in flat if ref not in claimed),
        key=lambda r: (r["app_label"], r.get("name", "")),
    )
    if leftovers:
        cards = [_card(row, None, path) for row in leftovers]
        out.append({
            "key": FALLBACK.key, "label": FALLBACK.label, "blurb": FALLBACK.blurb,
            "icon": FALLBACK.icon, "accent": FALLBACK.accent,
            "tables": cards, "count": len(cards),
            "is_current": any(c["is_current"] for c in cards),
        })
    return out


def category(available_apps, key, path=""):
    """One category by key, or None if it is empty or this user cannot see it."""
    for cat in categories(available_apps, path):
        if cat["key"] == key:
            return cat
    return None


def security_tables(available_apps, path=""):
    flat = flatten(available_apps)
    out = []
    for table in SECURITY_TABLES:
        row = flat.get(table.ref)
        if row is not None:
            out.append(_card(row, table, path))
    return out


def accent_for_path(available_apps, path):
    """The accent of whichever table `path` belongs to, or "".

    Lets a changelist and a change form wear the same colour as the card that
    was clicked to reach them — the thread that makes a two-level menu feel
    like one place rather than two.
    """
    if not path:
        return ""
    flat = flatten(available_apps)
    best = ""
    longest = 0
    for ref, row in flat.items():
        url = row.get("admin_url") or ""
        if url and path.startswith(url) and len(url) > longest:
            table = BY_REF.get(ref)
            best = table.accent if table else PLUM
            longest = len(url)
    return best
