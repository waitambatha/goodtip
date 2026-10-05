"""Where you stand, beyond your own organisation (client, 4 Oct 2026).

"The ladder is purely for teams." What had been put under the Ladder menu —
Industry and National tables — ranks PEOPLE and ORGANISATIONS, so it belongs
to the Leaderboard. Three questions, answered here:

  * INDUSTRY — where does our organisation stand among organisations of the
    same type (IT, Sales, a school, a club…), on how its tippers have done?
  * NATIONAL — where does it stand among every organisation on GoodTip,
    whatever its type?
  * EVERYONE — where do I stand against every tipper on GoodTip, people I have
    never met included? ("number 2 in my organisation, number 20 nationally")

HOW AN ORGANISATION IS SCORED. Average points per active tipper — anyone with
at least one graded tip — not the total. A total ranks headcount: a 300-person
workplace would sit above a 12-person office that tipped far better, and the
question asked was how the participants did, "all as a group". The total is
kept beside it, and breaks a tie.

HOW A PERSON IS SCORED NATIONALLY. Their best season total across the
organisations they tip in — one entry per person. Adding their organisations
together would count the same match once per organisation they belong to.

PRIVACY. Every organisation counts towards the positions, so a rank means the
same thing whoever is looking, but only an organisation that has opted in to
public naming (``Organisation.is_public_listed``, the Good List's consent) is
NAMED to people outside it; the rest read as "An organisation in <type>". Your
own organisation is always named to you. People on the national board are shown
by first name and last initial, never with an email.

All of it is the organisation-level comp (group tips are a group's own ladder),
in each organisation's own current season, and narrowed to one competition
when the leaderboard is filtered to one.
"""
from __future__ import annotations

from django.db.models import Count, F, Q, Sum, Value
from django.db.models.functions import Coalesce

from orgs.models import Organisation

from .models import Tip


def _season_tips(series=None):
    qs = Tip.objects.filter(group__isnull=True, match__round__competition__season=F("org__season"))
    if series is not None:
        qs = qs.filter(match__round__series=series)
    return qs


def _org_rows(series=None, organisation_type_id=None):
    """Every organisation's tipping, ranked by average points per tipper."""
    tips = _season_tips(series)
    if organisation_type_id is not None:
        tips = tips.filter(org__organisation_type_id=organisation_type_id)
    agg = (
        tips.values("org")
        .annotate(
            points=Coalesce(Sum("points_awarded"), Value(0)),
            tippers=Count("user", filter=Q(is_correct__isnull=False), distinct=True),
        )
        .filter(tippers__gt=0)
    )
    rows = [
        {"org_id": r["org"], "points": r["points"], "tippers": r["tippers"],
         "avg": r["points"] / r["tippers"]}
        for r in agg
    ]
    orgs = Organisation.objects.in_bulk([r["org_id"] for r in rows])
    out = []
    for r in rows:
        org = orgs.get(r["org_id"])
        if org is None:
            continue
        r["org"] = org
        out.append(r)
    out.sort(key=lambda r: (-r["avg"], -r["points"], r["org"].name))
    rank, prev = 0, None
    for i, r in enumerate(out, start=1):
        key = (round(r["avg"], 6), r["points"])
        if key != prev:
            rank, prev = i, key
        r["rank"] = rank
    return out


def _display_org(org, viewer_org):
    if org.id == getattr(viewer_org, "id", None) or org.is_public_listed:
        return org.name
    kind = org.organisation_type.name if org.organisation_type_id else "Australia"
    return f"An organisation in {kind}"


def org_board(viewer_org, scope: str, series=None) -> dict:
    """The Industry (``scope='industry'``) or National board, as the viewer's
    organisation sees it: every row ranked, named only where allowed."""
    type_id = viewer_org.organisation_type_id if scope == "industry" else None
    if scope == "industry" and type_id is None:
        return {"rows": [], "mine": None, "of": 0, "label": ""}
    rows = _org_rows(series, organisation_type_id=type_id)
    for r in rows:
        r["name"] = _display_org(r["org"], viewer_org)
        r["is_mine"] = r["org"].id == viewer_org.id
        r["avg"] = round(r["avg"], 1)
    mine = next((r for r in rows if r["is_mine"]), None)
    label = viewer_org.organisation_type.name if scope == "industry" and type_id else "Australia"
    return {"rows": rows, "mine": mine, "of": len(rows), "label": label}


def _short_name(user):
    name = (user.display_name or "").strip() or user.email.split("@")[0]
    parts = name.split()
    return f"{parts[0]} {parts[-1][0]}." if len(parts) > 1 else parts[0]


def everyone_board(viewer, viewer_org, series=None) -> dict:
    """Every tipper on GoodTip, one entry each, by their best season."""
    from accounts.models import User

    agg = (
        _season_tips(series)
        .values("user", "org")
        .annotate(
            points=Coalesce(Sum("points_awarded"), Value(0)),
            graded=Count("id", filter=Q(is_correct__isnull=False)),
            correct=Count("id", filter=Q(is_correct=True, is_auto=False)),
        )
        .filter(graded__gt=0)
    )
    best = {}
    for r in agg:
        cur = best.get(r["user"])
        if cur is None or r["points"] > cur["points"]:
            best[r["user"]] = r
    users = User.objects.in_bulk(list(best))
    orgs = Organisation.objects.in_bulk({r["org"] for r in best.values()})
    rows = []
    for uid, r in best.items():
        u, o = users.get(uid), orgs.get(r["org"])
        if u is None or o is None:
            continue
        rows.append({
            "user": u, "name": _short_name(u), "org_name": _display_org(o, viewer_org),
            "points": r["points"], "correct": r["correct"], "graded": r["graded"],
            "is_me": uid == viewer.id,
        })
    rows.sort(key=lambda r: (-r["points"], -r["correct"], r["name"]))
    rank, prev = 0, None
    for i, r in enumerate(rows, start=1):
        if r["points"] != prev:
            rank, prev = i, r["points"]
        r["rank"] = rank
    mine = next((r for r in rows if r["is_me"]), None)
    return {"rows": rows, "mine": mine, "of": len(rows)}


def standing(viewer, org, series=None, group=None) -> dict:
    """The "where you stand" strip: you in your organisation (and group), your
    organisation in its industry and nationally, and you nationally."""
    from .services import leaderboard_for_org

    def my_rank(board):
        row = next((u for u in board if u.id == viewer.id), None)
        active = [u for u in board if (u.tips_total or 0) > 0 or (u.points or 0) > 0]
        return (row.rank if row is not None and ((row.tips_total or 0) > 0 or (row.points or 0) > 0) else None,
                len(active))

    me_org = my_rank(leaderboard_for_org(org, series=series))
    me_group = my_rank(leaderboard_for_org(org, series=series, group=group)) if group is not None else None
    ind = org_board(org, "industry", series)
    nat = org_board(org, "national", series)
    every = everyone_board(viewer, org, series)
    return {
        "me_org": {"rank": me_org[0], "of": me_org[1]},
        "me_group": {"rank": me_group[0], "of": me_group[1], "name": group.name} if me_group else None,
        "org_industry": {"rank": ind["mine"]["rank"] if ind["mine"] else None, "of": ind["of"], "label": ind["label"]},
        "org_national": {"rank": nat["mine"]["rank"] if nat["mine"] else None, "of": nat["of"]},
        "me_national": {"rank": every["mine"]["rank"] if every["mine"] else None, "of": every["of"]},
    }
