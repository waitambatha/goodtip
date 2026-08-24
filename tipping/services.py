from django.db import transaction
from django.db.models import Count, Q, Sum, Value
from django.db.models.functions import Coalesce

from .models import Match, Round, Tip


def annotate_play_state(rounds):
    """Add the per-round counts ``current_round`` reads.

    Kept next to ``current_round`` because the two are a pair: calling the
    latter on an unannotated queryset raises AttributeError, and doing the
    counting per round instead would be a query each.
    """
    return rounds.annotate(
        match_count=Count("matches", distinct=True),
        unplayed=Count(
            "matches",
            filter=~Q(matches__status=Match.STATUS_COMPLETE),
            distinct=True,
        ),
    )


def current_round(rounds):
    """The round a member actually cares about: the earliest one still in play.

    Expects an ``annotate_play_state`` queryset evaluated newest-first, so this
    walks backwards to the oldest round that hasn't finished.

    Deliberately NOT keyed on ``Round.is_locked``. A round locks at its first
    kickoff, so the locked round is usually the one being played right now —
    picking the first unlocked round would skip the whole weekend's games while
    they were still going and land the member on a round nobody can tip yet.
    "Every fixture graded" is the only thing that means a round is genuinely done.

    A round with no fixtures yet counts as unfinished, so one freshly created by
    a fixtures sync isn't stepped over before its games land.
    """
    for r in reversed(rounds):
        if r.unplayed > 0 or r.match_count == 0:
            return r
    # Every round has been played out — end of season. Show the most recent.
    return rounds[0] if rounds else None


def derive_result(home_score: int | None, away_score: int | None) -> str | None:
    if home_score is None or away_score is None:
        return None
    if home_score > away_score:
        return "home"
    if away_score > home_score:
        return "away"
    return "draw"


@transaction.atomic
def record_match_result(match: Match, home_score: int, away_score: int) -> int:
    # A match that was called off is not scored either way (addendum §1). It
    # has no result to grade, and any tips already on it are wound back to
    # ungraded rather than left holding points from a game nobody played.
    if match.status == Match.STATUS_POSTPONED:
        return match.tips.update(is_correct=None, points_awarded=0)

    had_result = match.result is not None
    match.home_score = home_score
    match.away_score = away_score
    match.result = derive_result(home_score, away_score)
    match.save(update_fields=["home_score", "away_score", "result"])
    if had_result:
        # A correction after grading: any recap already posted for this round
        # is flagged for admin review, never silently rewritten (recap spec §3).
        from orgs.models import RoundRecap

        RoundRecap.objects.filter(round=match.round).update(needs_review=True)
    _fill_missed_tips(match)
    return _recalculate_tips_for_match(match)


def _fill_missed_tips(match: Match) -> int:
    """Give every member without a tip on this match the away side.

    The addendum's missed-tip default (§2), applied per match rather than per
    round: one forgotten week should not zero a whole round, which is what
    happens when an absent tip is simply worth nothing.

    Done at grading time rather than on a lock-time timer. The result is the
    moment the answer stops being able to change, so filling here cannot race
    a late tip and cannot be forgotten by a scheduler that did not run — and
    it is idempotent, because a member who has a tip is skipped.

    The away side specifically, because it has to be a rule nobody can work an
    advantage from. Home teams win more often than not, so defaulting to home
    would hand a bonus to whoever skipped the round.

    WHO IT APPLIES TO is narrower than "everyone in the league", on two counts,
    and both matter:

      * Only members who were in the league BEFORE the match kicked off. A
        default is for a tip somebody could have made and did not. Someone who
        joined on Sunday never had the chance to tip Thursday's game, and
        handing them a pick — which might land and score — would credit them
        for a round they were not part of. It also makes a new member's total
        depend on when the sync happened to grade, which is not a thing anyone
        should be able to notice.

      * Only members who actually tip. The roles split manager, captain,
        participant and both; a Team Manager who runs the league and never
        makes a pick would otherwise be auto-entered into every match and
        appear up the leaderboard on tips the system made for them.

    ONE DEFAULT PER CONTEXT. A tip belongs to where it was made, so a member
    of Marketing who tipped there and not for the organisation has still missed
    the organisation's round and still gets the default there — and the other
    way round. Counting "has this person tipped this match at all" would have
    let one pick in a group silently cover both ladders.
    """
    from orgs.models import Group, GroupMember, OrgMember

    org_id = match.round.org_id
    eligible = set(
        OrgMember.objects.filter(org_id=org_id)
        .exclude(role=OrgMember.ROLE_MANAGER)
        .filter(joined_at__lt=match.kickoff_at)
        .values_list("user_id", flat=True)
    )
    if not eligible:
        return 0

    # Who already has a tip, per context. None is the organisation itself.
    have = {}
    for uid, gid in match.tips.values_list("user_id", "group_id"):
        have.setdefault(gid, set()).add(uid)

    contexts = [(None, eligible)]
    for group in Group.objects.filter(
        org_id=org_id, approval_status=Group.APPROVAL_APPROVED,
    ):
        members = set(
            GroupMember.objects.filter(group=group)
            .filter(joined_at__lt=match.kickoff_at)
            .values_list("user_id", flat=True)
        )
        contexts.append((group.pk, members & eligible))

    rows = []
    for group_id, users in contexts:
        for uid in users - have.get(group_id, set()):
            rows.append(Tip(
                user_id=uid, match=match, org_id=org_id, group_id=group_id,
                selection="away", is_auto=True,
            ))
    if not rows:
        return 0
    Tip.objects.bulk_create(
        rows,
        # Two graders racing the same match must not collide on the unique
        # (user, match, org, group) key and lose the whole batch.
        ignore_conflicts=True,
    )
    return len(rows)


def _recalculate_tips_for_match(match: Match) -> int:
    if match.result is None:
        return 0
    # The round's stage decides what a correct tip is worth (1 / 2 / 4) and
    # what a draw pays (0, except State of Origin's 2).
    points = match.round.points_per_correct
    updated = 0
    if match.result == "draw":
        # is_correct stays False: nobody picked the winner, because there was
        # not one. The points are a property of the fixture, not of the pick,
        # which is why they are awarded to every tip alike.
        updated += match.tips.update(
            is_correct=False, points_awarded=match.round.points_per_draw,
        )
    else:
        winning = match.result
        updated += match.tips.filter(selection=winning).update(is_correct=True, points_awarded=points)
        updated += match.tips.exclude(selection=winning).update(is_correct=False, points_awarded=0)
    return updated


@transaction.atomic
def regrade_round(round_obj: Round) -> int:
    """Re-award points for every graded match in a round. Returns tips changed.

    Needed because what a correct tip is WORTH is a property of the round —
    1 regular, 2 finals, 4 for State of Origin — while ``points_awarded`` is
    frozen onto the Tip at the moment it was graded. Correcting a round's stage
    after its games have been played therefore changes nothing on its own, and
    the leaderboard keeps paying the old rate for the rest of the season.

    That is not a hypothetical: every State of Origin round in the live
    database was created on the "regular" default, so the prestige series that
    is meant to be worth quadruple was paying single, and simply setting the
    stage correctly would not have moved a single member's score.
    """
    updated = 0
    for match in round_obj.matches.filter(result__isnull=False):
        updated += _recalculate_tips_for_match(match)
    return updated


def competition_filter(org, wanted_slugs):
    """(chips, series to filter by) for the competition filter.

    Returns the series worth offering as a button, and the series a selection
    actually resolves to. They are not the same list, because State of Origin
    is not a competition anyone tips week to week — it is three representative
    games that belong to the NRL competition. Offering it as its own button
    put a code on screen that is empty for eleven months of the year and
    invited the question "why is that there".

    So representative series are never chips, and a code's chip carries them:
    choosing NRL shows the NRL rounds AND the Origin rounds, because in a
    tipping league Origin week IS the NRL round. That keeps Origin reachable
    without giving it a button of its own, and means picking a code never
    silently hides games that count toward the same ladder.

    Unknown slugs are dropped rather than 404ing — a stale bookmark should
    show everything, not an error.
    """
    from catalog.models import Series

    have = Series.objects.filter(rounds__org=org).distinct().order_by("name")
    chips = [s for s in have if s.category != Series.CATEGORY_REPRESENTATIVE]
    reps = [s for s in have if s.category == Series.CATEGORY_REPRESENTATIVE]

    by_slug = {s.slug: s for s in chips}
    active = [by_slug[v] for v in (wanted_slugs or []) if v in by_slug]
    if not active:
        return chips, []

    # Carry each chosen code's representative series along with it.
    sports = {s.sport_id for s in active}
    return chips, active + [s for s in reps if s.sport_id in sports]


# How many rounds a member may have open at once. Two: the one being played
# and the one after it.
TIP_WINDOW_ROUNDS = 2


def tip_window(org) -> dict[int, dict]:
    """Which rounds are open to tip, and for the rest, what they are waiting on.

    The earliest ``TIP_WINDOW_ROUNDS`` rounds that still have a match to play,
    per series. Once the first of them is done, the window slides forward and
    the next one opens.

    Per series, not per org, because an org tipping AFL and NRL runs two
    ladders side by side: capping the org at two rounds total would close the
    NRL round purely because two AFL rounds happened to sit in front of it.

    Returns ``{round_id: {"open": bool, "waits_for": Round | None}}``.

    ``waits_for`` is the point of this over a bare set of ids. A closed round
    must be able to say WHICH round has to finish before it opens — "locked"
    on its own is indistinguishable from "you missed it", and the two need
    opposite reactions from whoever is reading. With a window of two, the
    round at position N is released by the round at position N-2 finishing,
    so that is the one to name.

    The rule is about what may be SAVED, so submit_tip enforces it; the
    screens only reflect it.
    """
    from django.utils import timezone

    now = timezone.now()
    live = (
        Round.objects.filter(org=org)
        # A round is still live while any of its matches has yet to be played.
        .filter(matches__kickoff_at__gt=now)
        .distinct()
        .order_by("series_id", "lockout_at", "round_number")
    )
    per_series: dict[int, list] = {}
    for rnd in live:
        per_series.setdefault(rnd.series_id, []).append(rnd)

    window: dict[int, dict] = {}
    for rounds in per_series.values():
        for i, rnd in enumerate(rounds):
            if i < TIP_WINDOW_ROUNDS:
                window[rnd.id] = {"open": True, "waits_for": None}
            else:
                window[rnd.id] = {"open": False, "waits_for": rounds[i - TIP_WINDOW_ROUNDS]}
    return window


def tippable_round_ids(org) -> set[int]:
    """Just the ids, for the enforcement check in submit_tip."""
    return {rid for rid, state in tip_window(org).items() if state["open"]}


@transaction.atomic
def submit_tip(*, user, match: Match, org, selection: str, group=None) -> Tip:
    """Record a tip in the context it was made in.

    `group=None` means the organisation itself, and it is a real context rather
    than a missing one: the same fixture can carry one tip from this person for
    Marketing and another for the organisation, scored on separate ladders.
    """
    if match.is_locked:
        raise ValueError("Match is locked")
    if selection not in ("home", "away"):
        raise ValueError("Invalid selection")
    if match.round_id not in tippable_round_ids(org):
        raise ValueError(
            f"That round isn't open yet. You can tip {TIP_WINDOW_ROUNDS} rounds "
            "at a time, so this one opens once the current round is done."
        )
    if group is not None and group.org_id != org.id:
        raise ValueError("That group belongs to a different organisation.")
    tip, _ = Tip.objects.update_or_create(
        user=user, match=match, org=org, group=group,
        defaults={"selection": selection},
    )
    return tip


def _leaderboard(org_ids, tip_filter):
    """Points, and the accuracy record — which count DIFFERENT things.

    POINTS include auto-assigned tips. The missed-tip default exists precisely
    so a skipped round still scores, and a leaderboard that left those out
    would disagree with the rule that produced them.

    ACCURACY does not. "8 of 9 correct" is a claim about judgement, and a tip
    the system wrote is not judgement — without this filter, somebody who never
    opened the app could finish a round on a perfect record, and the member who
    actually studied the form and got seven would rank below them on it. Points
    are what the default is for; a strike rate is not.
    """
    from accounts.models import User
    real = Q(tips__is_auto=False)
    qs = User.objects.filter(memberships__org_id__in=org_ids).distinct()
    return qs.annotate(
        # Weighted score: sum of points_awarded (finals and Origin count for more).
        points=Coalesce(Sum("tips__points_awarded", filter=tip_filter), Value(0)),
        tips_total=Count("tips", filter=tip_filter & real & Q(tips__is_correct__isnull=False)),
        tips_correct=Count("tips", filter=tip_filter & real & Q(tips__is_correct=True)),
    ).order_by("-points", "display_name")


def leaderboard_for_org(org, round_id: int | None = None, group=None):
    """The ladder for one context.

    `group=None` is the organisation's own ladder and must exclude every tip
    made inside a group — otherwise a group's tips would be pooled in with the
    organisation's and everyone who tips in both would be counted twice. The
    isnull filter is the whole reason `group` is a column rather than a
    separate table.
    """
    tip_filter = Q(tips__org=org)
    tip_filter &= Q(tips__group=group) if group is not None else Q(tips__group__isnull=True)
    if round_id is not None:
        tip_filter &= Q(tips__match__round_id=round_id)

    board = _leaderboard([org.id], tip_filter)
    if group is not None:
        # A group's ladder is its own members, not the whole organisation with
        # most of them on nothing.
        board = board.filter(group_memberships__group=group)
    return apply_tiebreakers(board, [org.id], round_id=round_id, group=group)


# ---------------------------------------------------------------------------
# Tiebreakers (Scoring & Tiebreaker Addendum rev 3, §3)
#
# Scope is deliberately narrow: within-org leaderboards only. The industry and
# public boards rank by charity given rather than by tipping score and are
# untouched by any of this.
#
# There is no prize pool at any tier, so a tie decides ladder position and
# bragging rights and nothing else. That is why the order below resolves on
# performance and then stops, rather than reaching for a number that would
# always separate two people.
# ---------------------------------------------------------------------------

#: Which series breaks a tie in which. Every org package bundles both codes of
#: its sport — AFL+AFLW, NRL+NRLW, with no code-only opt-out — so a tied tipper
#: always has a same-period score in the paired comp to be judged on.
PAIRED_SERIES = {
    "AFL": "AFLW", "AFLW": "AFL",
    "NRL": "NRLW", "NRLW": "NRL",
}


def _paired_scores(org_ids, series_name: str, user_ids, round_id=None, group=None) -> dict:
    """Each user's points in the comp paired with ``series_name``."""
    from accounts.models import User

    partner = PAIRED_SERIES.get((series_name or "").upper())
    if not partner or not user_ids:
        return {}

    tip_filter = Q(tips__org_id__in=org_ids, tips__match__round__series__name__iexact=partner)
    tip_filter &= Q(tips__group=group) if group is not None else Q(tips__group__isnull=True)
    if round_id is not None:
        rnd = Round.objects.filter(pk=round_id).only("round_number").first()
        if rnd is not None:
            # Same period, not the same Round row: the paired comp has its own
            # round of that number, which is the like-for-like comparison.
            tip_filter &= Q(tips__match__round__round_number=rnd.round_number)

    rows = (
        User.objects.filter(id__in=user_ids)
        .annotate(paired=Coalesce(Sum("tips__points_awarded", filter=tip_filter), Value(0)))
        .values_list("id", "paired")
    )
    return dict(rows)


def _reached_score_at(org_ids, user_ids, points_by_user, round_id=None, group=None) -> dict:
    """When each user first reached their final total — the countback.

    "Whoever reached the tied score first ranks higher." Walking each tipper's
    graded tips in time order and stopping at the moment their running total
    hits the tied figure is what that sentence means: the earlier that moment,
    the longer they have held the score.
    """
    from collections import defaultdict

    tips = (
        Tip.objects.filter(
            org_id__in=org_ids, user_id__in=user_ids, is_correct__isnull=False,
            **({"group": group} if group is not None else {"group__isnull": True}),
        )
        .values_list("user_id", "points_awarded", "match__kickoff_at")
        .order_by("match__kickoff_at", "id")
    )
    running = defaultdict(int)
    reached = {}
    for uid, pts, when in tips:
        if uid in reached:
            continue
        running[uid] += pts or 0
        if running[uid] >= points_by_user.get(uid, 0):
            reached[uid] = when
    return reached


def apply_tiebreakers(board, org_ids, round_id=None, group=None):
    """Order a leaderboard, resolving ties on performance rather than on a name.

    Returns a list, not a queryset — the second and third steps need data the
    database cannot sort on in one pass, and pretending otherwise would mean a
    queryset whose order changed depending on whether it had been evaluated.

    Order: points, then the paired women's/men's comp, then countback, then
    equal. Co-champions is a real outcome here and not a failure to decide:
    with no cheque to write there is no reason to force one name above another.

    Ranks are attached as ``rank`` and ``is_tied`` so a template can show
    joint positions without recomputing any of this.
    """
    rows = list(board)
    if not rows:
        return rows

    user_ids = [u.id for u in rows]
    points_by_user = {u.id: u.points for u in rows}

    # THE CROSS-CODE STEP ONLY APPLIES TO A BOARD SCOPED TO ONE COMP.
    #
    # The addendum phrases it as "a tie in the men's comp is broken by that
    # tipper's score in the paired women's comp", which presumes the ranking
    # being broken covers one code. An unfiltered org board does not: it sums
    # every comp the league tips, so the paired score is ALREADY inside the
    # total. Adding it again would rank on the women's comp twice — a tipper
    # level overall would be separated by a number both of them had already
    # been credited for, which is not a tiebreak, it is double-counting.
    #
    # A tie on an all-comps board is a tie on everything, so it goes straight
    # to the countback.
    series_name = ""
    if round_id is not None:
        rnd = Round.objects.filter(pk=round_id).select_related("series").first()
        if rnd:
            series_name = rnd.series.name

    # Both steps read tips, so both have to read the same room the board came
    # from. A group ladder broken on the organisation's countback would order
    # its members by tips that never counted towards the score being tied.
    paired = _paired_scores(org_ids, series_name, user_ids, round_id=round_id, group=group)
    reached = _reached_score_at(
        org_ids, user_ids, points_by_user, round_id=round_id, group=group,
    )

    from datetime import datetime, timezone as _tz
    far_future = datetime.max.replace(tzinfo=_tz.utc)

    for u in rows:
        u.paired_points = paired.get(u.id, 0)
        u.reached_at = reached.get(u.id, far_future)

    rows.sort(key=lambda u: (-u.points, -u.paired_points, u.reached_at, u.display_name or ""))

    # Joint ranks. Two people are genuinely tied only when every step above
    # failed to separate them, which is exactly when they share a position.
    rank = 0
    previous = None
    for i, u in enumerate(rows, start=1):
        key = (u.points, u.paired_points, u.reached_at)
        if key != previous:
            rank = i
            previous = key
        u.rank = rank
    counts = {}
    for u in rows:
        counts[u.rank] = counts.get(u.rank, 0) + 1
    for u in rows:
        u.is_tied = counts[u.rank] > 1
    return rows


def leaderboard_for_family(org, round_id: int | None = None):
    """The §8 national board: every member across the top-level parent and
    all its children, ranked together. Same underlying competition as the
    local board — each member's points come from their own org's tips.

    Rounds are per-org rows, so a round filter is aligned across the family
    by (round_number, series) rather than by the id of one org's round.
    """
    family_ids = org.family_ids()
    # Organisation-level tips only. A group's tips belong to that group's
    # ladder, and pooling them in here would count anyone who tips in both
    # twice on the national board — the same double count leaderboard_for_org
    # guards against, one level up.
    tip_filter = Q(tips__org_id__in=family_ids, tips__group__isnull=True)
    if round_id is not None:
        rnd = Round.objects.filter(pk=round_id).only("round_number", "series_id").first()
        if rnd is not None:
            tip_filter &= Q(
                tips__match__round__round_number=rnd.round_number,
                tips__match__round__series_id=rnd.series_id,
            )
    return apply_tiebreakers(
        _leaderboard(family_ids, tip_filter), family_ids, round_id=round_id,
    )


def user_org_stats(user, org, group=None):
    """Points over every tip; the record over the ones this member actually made.

    Same split as the leaderboard and for the same reason — see _leaderboard.
    tips_submitted likewise counts real picks, because it is the number shown
    as "tips made" and the system making one on your behalf is not you making
    a tip.

    Scoped to one room, like everything else that counts tips. These numbers
    sit beside a rank, and a total built from both rooms next to a rank built
    from one is two answers to the same question.
    """
    tips = Tip.objects.filter(user=user, org=org, group=group)
    mine = tips.filter(is_auto=False)
    points = tips.aggregate(p=Coalesce(Sum("points_awarded"), Value(0)))["p"]
    return {
        "points": points,
        "tips_correct": mine.filter(is_correct=True).count(),
        "tips_graded": mine.filter(is_correct__isnull=False).count(),
        "tips_submitted": mine.count(),
    }


def user_rank_in_org(user, org, group=None) -> int | None:
    """This member's position, with the addendum's tiebreakers applied.

    Reads the rank the board itself worked out rather than recounting by
    points here. The two used to disagree the moment a tie was broken by
    anything other than the score: the board would separate two people on the
    paired comp while this still called them equal, so a member saw one
    position on the leaderboard and a different one on their dashboard.
    """
    for row in leaderboard_for_org(org, group=group):
        if row.id == user.id:
            return row.rank
    return None

