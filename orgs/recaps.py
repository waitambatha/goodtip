"""Group Recap — docs/ai-group-recap-spec.md, built in house.

One short summary of a group's tipping round, posted to the Wall as the
pinned recap card with the leaderboard and a few conversation starters
attached. No group activity, no recap.

There is no model API behind this and no outbound call. The recap is composed
here, from the group's own numbers, by finding the one moment in the round
worth remarking on and writing it up. That is a deliberate choice, not a
stopgap:

  * A round recap is a small, closed problem. The facts are a leaderboard, a
    set of graded picks, and a comparison against where everyone stood before
    the round. Everything worth saying is already in those numbers, and a
    writer that can only speak from them cannot invent a streak that did not
    happen or a name that is not in the group.
  * The voice rules in §8 and §9 of the spec are absolute: no em dashes, no
    weasel words, British English, code-specific vernacular, two to four
    sentences. Meeting those by choosing the words is exact. Asking for them
    and checking afterwards is not.
  * It costs nothing per group per round, runs with the database and nothing
    else, and gives the same text for the same round every time. That last
    part matters when the recap is the pinned card a group argues about for
    the rest of the week.

Variety comes from a seed fixed on (org, round): a group gets different
phrasing week to week, and re-running a round produces what it produced
before.

Run via `manage.py generate_recaps` once results land.
"""

import logging
import random
import re

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

logger = logging.getLogger(__name__)

# Stamped onto RoundRecap.model_used so a card can be traced to the writer
# that produced it. Bump the number when the phrasing changes materially.
RECAP_ENGINE = "goodtip-recap-1"

# How many rows of the season table ride along on the card.
LEADERBOARD_ROWS = 5


# ---------------------------------------------------------------------------
# Vernacular (§9)
#
# Each code gets its own words for the same event, and never borrows the
# other's. The shared bank is the register both codes use, so it is safe
# anywhere. Everything here describes a tipster's round, not play on the
# field: a writer that reaches for "speccie" to describe someone picking six
# from eight is writing about the wrong thing.
# ---------------------------------------------------------------------------

VERNACULAR = {
    "afl": {
        "led": ["took the chocolates", "was home and hosed", "led from the front"],
        "big": "kicked a bag",
    },
    "nrl": {
        "led": ["took the points", "had a ripper", "led from the front"],
        "big": "put in a blinder",
    },
    "generic": {
        "led": ["topped the group", "led the way", "led from the front"],
        "big": "put in a big one",
    },
}

# Words the spec bans outright (§8). RecapWriterTests sweeps every branch of
# the writer and checks the finished text against this list, with the member
# names blanked out first: the ban is on the writer's own vocabulary, and a
# member called May or Hope is not a voice breach.
BANNED_WORDS = (
    "may", "maybe", "hope", "wish", "try", "could", "perhaps", "strive",
    "epic", "incredible", "legendary", "dominant", "seamless", "elevated",
    "leverage", "journey", "moving forward",
)


def _code_for(rnd) -> str:
    """Which code's words to use, read off the round's own series."""
    sport = getattr(getattr(rnd, "series", None), "sport", None)
    text = f"{getattr(sport, 'slug', '')} {getattr(sport, 'name', '')}".lower()
    if "rules" in text or "afl" in text:
        return "afl"
    if "league" in text or "nrl" in text:
        return "nrl"
    return "generic"


# ---------------------------------------------------------------------------
# Readiness + facts (§3, §4)
# ---------------------------------------------------------------------------

def round_ready_for_recap(rnd) -> bool:
    """§3: the round's last match has locked and every result is final."""
    matches = list(rnd.matches.all())
    if not matches:
        return False
    if any(m.result is None for m in matches):
        return False
    return timezone.now() >= max(m.kickoff_at for m in matches)


def _season_ranks(org, cutoff):
    """Standings from all of the org's tips in rounds locked out at or before
    ``cutoff`` — same Sum(points_awarded) the leaderboard uses. Competition
    ranking with ties sharing a rank, matching user_rank_in_org."""
    from tipping.models import Tip

    rows = list(
        Tip.objects.filter(org=org, match__round__lockout_at__lte=cutoff)
        .values("user_id")
        .annotate(points=Sum("points_awarded"))
        .order_by("-points")
    )
    ranks, points = {}, {}
    last, rank = None, 0
    for i, row in enumerate(rows, start=1):
        if row["points"] != last:
            rank, last = i, row["points"]
        ranks[row["user_id"]] = rank
        points[row["user_id"]] = row["points"]
    return ranks, points


def _match_stories(rnd, round_tips):
    """How the group went game by game.

    Two of these are worth more than the whole ledger put together: the game
    nearly everybody got wrong, and the game nobody doubted. The first is
    what people actually talk about on a Monday, so it feeds both the recap
    line and the conversation starters.
    """
    counts = {}
    for t in round_tips:
        c = counts.setdefault(t.match_id, {"correct": 0, "tips": 0})
        c["tips"] += 1
        if t.is_correct:
            c["correct"] += 1

    rows = []
    for m in rnd.matches.select_related("home_team", "away_team"):
        c = counts.get(m.id)
        if not c or c["tips"] < 2 or m.result is None:
            continue
        if m.result == "draw":
            winner = loser = None
        elif m.result == "home":
            winner, loser = m.home_team.name, m.away_team.name
        else:
            winner, loser = m.away_team.name, m.home_team.name
        rows.append({
            "home": m.home_team.name,
            "away": m.away_team.name,
            "winner": winner,
            "loser": loser,
            "correct": c["correct"],
            "tips": c["tips"],
            "share": c["correct"] / c["tips"],
        })
    if not rows:
        return {"upset": None, "consensus": None}

    upset = min(rows, key=lambda r: (r["share"], r["correct"]))
    consensus = max(rows, key=lambda r: (r["share"], r["tips"]))
    return {
        # Only an upset if most of the group missed it.
        "upset": upset if upset["share"] <= 0.5 and upset["winner"] else None,
        # Only worth a mention if it was unanimous.
        "consensus": consensus if consensus["share"] == 1.0 and consensus["winner"] else None,
    }


def build_recap_facts(org, rnd):
    """Everything the writer is allowed to know (§4). Real results only.

    Returns None when nobody in the group tipped this round — silence, not an
    apologetic card (§10).
    """
    from tipping.models import Tip

    round_tips = list(
        Tip.objects.filter(org=org, match__round=rnd)
        .select_related("user")
    )
    if not round_tips:
        return None

    per_member = {}
    for t in round_tips:
        m = per_member.setdefault(t.user_id, {
            "name": t.user.display_name or t.user.email,
            "correct": 0, "picks": 0, "round_points": 0,
        })
        m["picks"] += 1
        if t.is_correct:
            m["correct"] += 1
            m["round_points"] += t.points_awarded

    ranks_now, season_points = _season_ranks(org, rnd.lockout_at)
    ranks_prev, season_prev = _season_ranks(org, rnd.lockout_at - timezone.timedelta(seconds=1))
    prev_rounds_exist = bool(ranks_prev)

    match_count = rnd.matches.count()
    members = []
    for user_id, m in per_member.items():
        entry = {
            "name": m["name"],
            "correct": m["correct"],
            "picks": m["picks"],
            "round_points": m["round_points"],
            "season_points": season_points.get(user_id, m["round_points"]),
            "rank_now": ranks_now.get(user_id),
            "perfect_round": m["correct"] == match_count and match_count > 0,
        }
        if user_id in ranks_prev:
            entry["rank_before_round"] = ranks_prev[user_id]
            if entry["rank_now"] is not None:
                entry["moved"] = ranks_prev[user_id] - entry["rank_now"]  # +ve = climbed
        members.append(entry)
    members.sort(key=lambda e: (-e["round_points"], e["name"]))

    # The season table, which is the group's own leaderboard rather than just
    # the people who tipped this round: somebody who sat the round out is
    # still on the ladder and still being caught.
    standings = []
    for user_id, pts in sorted(season_points.items(), key=lambda kv: -kv[1]):
        name = per_member.get(user_id, {}).get("name")
        if name is None:
            name = _name_for(org, user_id)
        row = {
            "name": name,
            "rank": ranks_now.get(user_id),
            "season_points": pts,
            "round_points": per_member.get(user_id, {}).get("round_points", 0),
            "tipped_this_round": user_id in per_member,
        }
        if user_id in ranks_prev:
            row["moved"] = ranks_prev[user_id] - row["rank"]
        standings.append(row)

    return {
        # Fixes the phrasing to this group and this round, so a re-run reads
        # the same and next week reads differently.
        "seed": f"{org.id}:{rnd.id}",
        "round": {
            "number": rnd.round_number,
            "competition": rnd.competition.name if rnd.competition else rnd.series.name,
            "stage": rnd.stage,
            "is_origin": rnd.stage == rnd.STAGE_ORIGIN,
            "code": _code_for(rnd),
            "points_per_correct_pick": rnd.points_per_correct,
            "matches_in_round": match_count,
        },
        "group": {
            "name": org.name,
            "members_who_tipped": len(members),
            "first_round_for_group": not prev_rounds_exist,
        },
        "members": members,
        "standings": standings,
        "matches": _match_stories(rnd, round_tips),
        "totals": {
            "correct": sum(m["correct"] for m in members),
            "picks": sum(m["picks"] for m in members),
        },
    }


def _name_for(org, user_id):
    """Display name for someone on the ladder who skipped this round."""
    from accounts.models import User

    u = User.objects.filter(pk=user_id).first()
    if u is None:
        return "A member"
    return u.display_name or u.email


# ---------------------------------------------------------------------------
# The writer (§7-9)
# ---------------------------------------------------------------------------

def _round_label(rnd) -> str:
    if rnd["is_origin"]:
        return f"State of Origin {rnd['number']}".strip()
    return f"Round {rnd['number']}"


def _pts(n: int) -> str:
    return f"{n} point" if n == 1 else f"{n} points"


def _picks(n: int) -> str:
    return f"{n} pick" if n == 1 else f"{n} picks"


def _names(items) -> str:
    """Join names the way somebody reads them out: A, B and C."""
    names = [i["name"] for i in items]
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + f" and {names[-1]}"


def _opening(facts, rng, words) -> str:
    """The moment. One sentence on whatever the round is actually about,
    chosen by what happened rather than by a fixed running order."""
    rnd, group, members = facts["round"], facts["group"], facts["members"]
    label = _round_label(rnd)
    top = members[0]
    joint = [m for m in members if m["round_points"] == top["round_points"]]
    perfect = [m for m in members if m["perfect_round"]]
    n = rnd["matches_in_round"]

    if len(perfect) == 1:
        return rng.choice([
            f"{perfect[0]['name']} went through {label} clean, all {n} correct.",
            f"A clean sweep for {perfect[0]['name']} in {label}, {n} from {n}.",
        ])
    if len(perfect) > 1:
        if len(perfect) == group["members_who_tipped"]:
            return f"Everyone who tipped {label} got the lot, {n} from {n}."
        return f"{_names(perfect)} all went through {label} clean, {n} from {n}."

    if group["first_round_for_group"]:
        return rng.choice([
            f"{label} opens the account for {group['name']}.",
            f"First round on the board for {group['name']}, and {label} goes to {top['name']}.",
        ])

    if len(joint) > 1 and top["round_points"] > 0:
        return rng.choice([
            f"Nothing in it at the top of {label}. {_names(joint)} finished level on {_pts(top['round_points'])}.",
            f"{_names(joint)} finished {label} inseparable, "
            f"{'both' if len(joint) == 2 else 'all of them'} on {_pts(top['round_points'])}.",
        ])

    leader = next((m for m in members if m.get("rank_now") == 1), None)
    if leader and leader.get("rank_before_round", 1) != 1:
        return rng.choice([
            f"{leader['name']} takes over top spot after {label}.",
            f"New name at the top after {label}. {leader['name']} moves in front.",
        ])

    climber = max(members, key=lambda m: m.get("moved", 0))
    if climber.get("moved", 0) >= 3:
        return f"{climber['name']} climbed {climber['moved']} places in {label}."

    upset = facts["matches"]["upset"]
    if upset and upset["correct"] == 0:
        return f"Nobody in {group['name']} had {upset['winner']} in {label}."

    return rng.choice([
        f"{label} goes to {top['name']}.",
        f"{top['name']} {rng.choice(words['led'])} in {label}.",
    ])


def _ledger(facts, rng, words, opening) -> str | None:
    """The score.

    Dropped when the opening already quoted it. Shortened when the opening
    already named the winner, because "X goes to Sam. Sam took the
    chocolates" is the same sentence twice.
    """
    members = facts["members"]
    top = members[0]
    if top["round_points"] == 0:
        return None
    if "point" in opening:
        return None

    # A perfect round has already been counted out loud in the opening, and
    # at one point a pick the count and the score are the same number.
    counted = top["perfect_round"] and str(top["correct"]) in opening

    if counted:
        line = ""
    elif top["name"] in opening:
        line = f"That was {_pts(top['round_points'])} from {_picks(top['picks'])}."
    else:
        big = top["correct"] == top["picks"] and top["picks"] >= 5
        verb = words["big"] if big else rng.choice(words["led"])
        line = f"{top['name']} {verb} with {_pts(top['round_points'])} from {_picks(top['picks'])}."

    chasers = [m for m in members[1:] if m["round_points"] < top["round_points"]]
    if chasers:
        gap = top["round_points"] - chasers[0]["round_points"]
        if gap <= 2:
            line += f" {chasers[0]['name']} kept it tight, {_pts(gap)} back."
    return line.strip() or None


def _movement(facts, rng) -> str | None:
    """Who moved, and where that leaves the season."""
    if facts["group"]["first_round_for_group"]:
        return None
    standings = facts["standings"]
    if len(standings) < 2:
        return None

    movers = [m for m in facts["members"] if m.get("moved", 0) >= 2]
    if movers:
        best = max(movers, key=lambda m: m["moved"])
        return (
            f"{best['name']} is up {best['moved']} places to "
            f"{_ordinal(best['rank_now'])} on {_pts(best['season_points'])}."
        )

    leader, second = standings[0], standings[1]
    gap = leader["season_points"] - second["season_points"]
    if gap == 0:
        return f"{leader['name']} and {second['name']} share the lead on {_pts(leader['season_points'])}."
    return rng.choice([
        f"{leader['name']} leads the season on {_pts(leader['season_points'])}, {gap} clear of {second['name']}.",
        f"That keeps {leader['name']} in front on {_pts(leader['season_points'])}, {_pts(gap)} ahead of {second['name']}.",
    ])


def _tail(facts) -> str | None:
    """One closing fact. The game the group got wrong beats the group average
    every time, so it goes first."""
    upset = facts["matches"]["upset"]
    if upset and upset["correct"] < upset["tips"]:
        if upset["correct"] == 0:
            return f"Not one pick went {upset['winner']}'s way against {upset['loser']}."
        # The group came unstuck, not the winning side. Worth being careful
        # about: the other way round reads as though the team that won lost.
        return (
            f"{upset['winner']} caught the group out, "
            f"{upset['correct']} of {upset['tips']} backed them over {upset['loser']}."
        )

    # Only says something a group of one has not already read twice.
    totals = facts["totals"]
    if totals["picks"] and facts["group"]["members_who_tipped"] > 1:
        return f"The group landed {totals['correct']} of {totals['picks']} between them."
    return None


_ORDINALS = {1: "first", 2: "second", 3: "third", 4: "fourth", 5: "fifth"}


def _ordinal(n) -> str:
    if n is None:
        return "the pack"
    if n in _ORDINALS:
        return _ORDINALS[n]
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def compose_recap(facts) -> str | None:
    """Two to four sentences on the group's round.

    Returns None when the round is too thin to say anything about, which
    leaves the caller to post the plain factual line instead.
    """
    rng = random.Random(facts.get("seed", facts["group"]["name"]))
    words = VERNACULAR.get(facts["round"]["code"], VERNACULAR["generic"])

    opening = _opening(facts, rng, words)
    has_upset = bool(facts["matches"]["upset"])

    # (narrative position, priority, text). Four sentences is a tight budget
    # and some parts spend two of them, so what gets cut is decided by what
    # is worth reading rather than by what happens to be last. The game the
    # group got wrong outranks the runner-up's margin: it is the line people
    # reply to.
    parts = [
        (0, 0, opening),
        (1, 3, _ledger(facts, rng, words, opening)),
        (2, 4, _movement(facts, rng)),
        (3, 1 if has_upset else 5, _tail(facts)),
    ]
    if facts["round"]["is_origin"]:
        parts.append(
            (0.5, 2, f"Origin picks were worth {facts['round']['points_per_correct_pick']} apiece."),
        )

    def split(text):
        return [s for s in re.split(r"(?<=\.)\s+", text.strip()) if s]

    chosen, spent = [], 0
    for position, _, text in sorted(
        (p for p in parts if p[2]), key=lambda p: p[1]
    ):
        sentences = split(text)
        if spent + len(sentences) > 4:
            continue
        chosen.append((position, sentences))
        spent += len(sentences)

    if spent < 2:
        return None
    chosen.sort(key=lambda c: c[0])
    return _clean_output(" ".join(s for _, block in chosen for s in block))


def _clean_output(text: str) -> str:
    """The mechanical part of §8: no em dashes, no doubled spacing, no
    stray whitespace before punctuation."""
    text = " ".join(text.strip().split())
    text = text.replace(" — ", ", ").replace("—", ", ").replace(" – ", ", ")
    text = re.sub(r"\s+([.,])", r"\1", text)
    return text


# ---------------------------------------------------------------------------
# Conversation starters
#
# The card is not the conversation, it is the thing the conversation hangs
# off. Each starter is a line a member could send as-is, built from a number
# that is actually on the card, and it lands in their reply box rather than
# being posted by anyone. Nothing here speaks in a member's voice or on their
# behalf.
# ---------------------------------------------------------------------------

def build_talking_points(facts) -> list:
    """Two or three openers for the thread under the recap."""
    rng = random.Random(facts.get("seed", "") + "|talk")
    members, group = facts["members"], facts["group"]
    top = members[0]
    upset = facts["matches"]["upset"]
    consensus = facts["matches"]["consensus"]
    points = []

    if upset:
        if upset["correct"] == 0:
            points.append(f"Did anyone see {upset['winner']} coming?")
        else:
            points.append(
                f"Only {upset['correct']} of us had {upset['winner']}. Who called that one?"
            )

    perfect = [m for m in members if m["perfect_round"]]
    if perfect:
        who = perfect[0]["name"]
        points.append(f"{who} went clean. Which one was closest?")
    elif top["round_points"] > 0:
        points.append(f"{top['name']} topped the round. Anyone getting close next week?")

    joint = [m for m in members if m["round_points"] == top["round_points"]]
    if len(joint) > 1:
        points.append(f"{_names(joint)} are level. Who takes the next one?")

    movers = [m for m in members if m.get("moved", 0) >= 2]
    if movers:
        best = max(movers, key=lambda m: m["moved"])
        points.append(f"{best['name']} is climbing. Anyone chasing them down?")

    if consensus and len(points) < 3:
        points.append(f"Everyone had {consensus['winner']}. Was that ever in doubt?")

    if not points:
        points.append(f"Who is everyone backing for {group['name']} next round?")

    rng.shuffle(points)
    return points[:3]


def leaderboard_snapshot(facts) -> list:
    """The top of the season table as it stood when the recap was written, so
    the card keeps showing the round it describes rather than today."""
    return facts["standings"][:LEADERBOARD_ROWS]


def fallback_line(facts) -> str:
    """§10: one factual line for a round with nothing to say about it."""
    top = facts["members"][0]
    rnd = facts["round"]
    label = _round_label(rnd).lower()
    return (
        f"{top['name']} topped {label} for {facts['group']['name']} with "
        f"{_pts(top['round_points'])} from {_picks(top['picks'])}."
    )


# ---------------------------------------------------------------------------
# Batch
# ---------------------------------------------------------------------------

def generate_recaps(org=None, dry_run=False):
    """Generate every recap that's due: rounds fully resolved, tips present,
    no recap yet. One per (org, round), posted to the Wall (§2, §3).

    Returns a list of (RoundRecap|facts, text) — with dry_run the text is
    printed by the caller and nothing is written.
    """
    from tipping.models import Round

    from .models import Organisation, RoundRecap, WallPost

    orgs = [org] if org is not None else list(Organisation.objects.all())
    results = []
    for o in orgs:
        done = set(RoundRecap.objects.filter(org=o).values_list("round_id", flat=True))
        candidate_rounds = (
            Round.objects.filter(org=o, lockout_at__lte=timezone.now())
            .exclude(id__in=done)
            .prefetch_related("matches")
            .select_related("competition", "series", "series__sport")
        )
        for rnd in candidate_rounds:
            if not round_ready_for_recap(rnd):
                continue
            facts = build_recap_facts(o, rnd)
            if facts is None:
                continue  # nobody tipped — silence (§10)

            fallback = False
            try:
                text = compose_recap(facts)
            except Exception:
                logger.exception("Recap composition failed for %s R%s", o, rnd.round_number)
                text = None
            if not text:
                text = fallback_line(facts)
                fallback = True

            if dry_run:
                results.append((facts, text))
                continue

            with transaction.atomic():
                post = WallPost.objects.create(
                    org=o, kind=WallPost.KIND_RECAP, body=text,
                )
                recap = RoundRecap.objects.create(
                    org=o, round=rnd, post=post,
                    fallback_used=fallback,
                    model_used=RECAP_ENGINE,
                    talking_points=build_talking_points(facts),
                    leaderboard=leaderboard_snapshot(facts),
                )
            results.append((recap, text))
    return results
