"""Carrying one set of picks into every room a member tips in.

THE PROBLEM
-----------
Someone in a work comp, a mates' group and a family comp tips the same eight
games three times over. The picks are identical — Collingwood is going to beat
Carlton whoever is asking — and only the ladder the tip lands on differs. Every
repeat is a chance to forget one and get auto-assigned instead.

WHY IT IS NOT A ONE-LINE COPY
-----------------------------
A Round belongs to an Organisation and a Match belongs to a Round, so every
org holds its OWN copy of every fixture: 11,314 match rows across the platform
resolve to 592 real games. "The same match" therefore does not exist as a row
that two orgs share — it exists as an `external_id` that appears once per org.
Everything here joins on that.

WHAT A "ROOM" IS
----------------
A tipping context: an organisation, plus optionally a group inside it. Both
are real destinations with separate ladders — Tip carries `org` and `group`,
and `group=None` means the organisation itself rather than a missing value. A
member of an org with two groups can therefore have three rooms in that org
alone.

THE ONE RULE WORTH KNOWING
--------------------------
A pick that disagrees with one already made elsewhere is NEVER overwritten
without being shown first. Somebody who deliberately tipped their own club in
the family comp and against it at work meant both, and a feature that quietly
reconciles them has destroyed information the member cannot get back. Those
are surfaced as `conflicts` and only written when explicitly confirmed.
"""
from dataclasses import dataclass, field

from orgs.models import Group, GroupMember, OrgMember

from .models import Match, Tip
from .services import submit_tip, tippable_round_ids


@dataclass
class Room:
    """One place a tip can land: an org, and optionally a group inside it."""

    org: object
    group: object = None

    @property
    def key(self) -> str:
        """Stable identifier for a form field. `0` means the org itself."""
        return f"{self.org.id}:{self.group.id if self.group else 0}"

    @property
    def label(self) -> str:
        return f"{self.org.name} · {self.group.name}" if self.group else self.org.name

    def __eq__(self, other):
        return isinstance(other, Room) and self.key == other.key

    def __hash__(self):
        return hash(self.key)


@dataclass
class RoomPlan:
    """What carrying WOULD do to one room, before anything is written."""

    room: Room
    # No tip there yet — these get written without asking.
    writes: list = field(default_factory=list)
    # A DIFFERENT pick is already recorded. Shown, never silently replaced.
    conflicts: list = field(default_factory=list)
    # Already tipped the same way. Nothing to do, and worth saying so.
    unchanged: list = field(default_factory=list)
    # The room cannot take this pick — game started, or its round is shut.
    blocked: list = field(default_factory=list)

    @property
    def has_work(self) -> bool:
        return bool(self.writes or self.conflicts)

    @property
    def change_count(self) -> int:
        return len(self.writes) + len(self.conflicts)


def rooms_for(user) -> list[Room]:
    """Every room this user can tip in, organisation first within each org.

    A group membership does NOT replace the organisation's own room: someone
    in Marketing can still tip for the company, and the room switcher exists
    precisely so they can move between the two.
    """
    rooms = []
    memberships = (
        OrgMember.objects.filter(user=user).select_related("org").order_by("org__name")
    )
    groups_by_org = {}
    for gm in (
        GroupMember.objects.filter(
            user=user, group__approval_status=Group.APPROVAL_APPROVED,
        )
        .select_related("group", "group__org")
        .order_by("group__name")
    ):
        groups_by_org.setdefault(gm.group.org_id, []).append(gm.group)

    for m in memberships:
        rooms.append(Room(org=m.org))
        # Groups only exist as destinations where the org has them switched on.
        if m.org.groups_enabled:
            for group in groups_by_org.get(m.org.id, []):
                rooms.append(Room(org=m.org, group=group))
    return rooms


def carry_rooms(user, source: Room) -> list[Room]:
    """The rooms a tip could carry INTO — everywhere but where it was made."""
    return [r for r in rooms_for(user) if r != source]


def build_plan(user, picks: dict, source: Room) -> list[RoomPlan]:
    """What carrying `picks` into every other room would do.

    `picks` is {match_id: "home"|"away"} as posted from the slate — matches in
    the SOURCE org. Each is resolved to its `external_id` and re-found in each
    target org, because the same fixture is a different row in every org.

    Writes nothing. Returns one RoomPlan per room that has something to do;
    rooms with nothing to carry are dropped rather than listed as empty.
    """
    targets = carry_rooms(user, source)
    if not targets or not picks:
        return []

    source_matches = {
        m.id: m
        for m in Match.objects.filter(
            pk__in=picks, round__org=source.org,
        ).select_related("round")
    }
    # external_id -> selection. A fixture with no external id cannot be
    # matched across orgs at all, so it simply does not carry.
    wanted = {}
    for mid, selection in picks.items():
        match = source_matches.get(int(mid))
        if match is not None and match.external_id:
            wanted[match.external_id] = selection
    if not wanted:
        return []

    target_orgs = {r.org.id: r.org for r in targets}
    siblings = {}
    for m in (
        Match.objects.filter(
            external_id__in=wanted, round__org_id__in=target_orgs,
        ).select_related("round", "home_team", "away_team")
    ):
        siblings.setdefault(m.round.org_id, {})[m.external_id] = m

    # The tipping window is per ORG, so it is resolved once per org rather
    # than once per room — an org with three groups would otherwise recompute
    # the same answer three times.
    open_rounds = {oid: tippable_round_ids(org) for oid, org in target_orgs.items()}

    existing = {}
    for tip in Tip.objects.filter(
        user=user, match__external_id__in=wanted, org_id__in=target_orgs,
    ).select_related("match"):
        existing[(tip.org_id, tip.group_id, tip.match.external_id)] = tip

    plans = []
    for room in targets:
        by_ext = siblings.get(room.org.id, {})
        plan = RoomPlan(room=room)
        for ext, selection in wanted.items():
            match = by_ext.get(ext)
            if match is None:
                continue                    # this org doesn't tip that comp
            row = {"match": match, "selection": selection}
            if match.is_locked or match.round_id not in open_rounds[room.org.id]:
                plan.blocked.append(row)
                continue
            tip = existing.get(
                (room.org.id, room.group.id if room.group else None, ext)
            )
            if tip is None:
                plan.writes.append(row)
            elif tip.selection == selection:
                plan.unchanged.append(row)
            else:
                plan.conflicts.append({**row, "existing": tip.selection})
        if plan.writes or plan.conflicts or plan.unchanged:
            plans.append(plan)
    return plans


def apply_plan(user, plans, *, rooms: set, overrides: set) -> dict:
    """Write the plan. Returns counts for the message back to the member.

    `rooms` is the set of room keys the member agreed to carry into.
    `overrides` is the set of "roomkey:matchid" they agreed to overwrite —
    anything not in it keeps whatever that room already had, which is the
    whole reason conflicts are surfaced rather than resolved.
    """
    carried = 0
    overwritten = 0
    kept = 0
    touched = []
    for plan in plans:
        if plan.room.key not in rooms:
            kept += len(plan.conflicts)
            continue
        wrote_here = 0
        for row in plan.writes:
            try:
                submit_tip(
                    user=user, match=row["match"], org=plan.room.org,
                    group=plan.room.group, selection=row["selection"],
                )
                carried += 1
                wrote_here += 1
            except ValueError:
                pass                        # locked between plan and submit
        for row in plan.conflicts:
            if f"{plan.room.key}:{row['match'].id}" not in overrides:
                kept += 1
                continue
            try:
                submit_tip(
                    user=user, match=row["match"], org=plan.room.org,
                    group=plan.room.group, selection=row["selection"],
                )
                overwritten += 1
                wrote_here += 1
            except ValueError:
                pass
        if wrote_here:
            touched.append(plan.room.label)
    return {
        "carried": carried, "overwritten": overwritten,
        "kept": kept, "rooms": touched,
    }


@dataclass
class OrgCarry:
    """One organisation's rooms, gathered under it.

    WHY THE FLAT LIST WAS NOT ENOUGH. build_plan returns one RoomPlan per
    destination, and a destination is an org-or-group. Rendered flat that is a
    list of "Acme", "Acme · Marketing", "Acme · IT", "Zenith", "Zenith · Sales"
    — five peers with the organisation's name repeated through them, and no
    way to say "all of Acme, but not IT". The client's note: "the organisations
    should have a dropdown that shows groups, so if I was to uncheck the group
    in an organisation … it cleans things up and makes me know this group is
    for this organisation."

    So the org is the unit you decide about and its rooms are the detail
    underneath. Nothing about what gets WRITTEN changes: every room still posts
    its own `room` checkbox with the same key, and the org-level control is a
    convenience over those. apply_plan never learns this type exists.
    """

    org: object
    #: The organisation's own room, when carrying there does anything.
    own: object = None
    #: Its group rooms, in the order rooms_for produced them (by group name).
    groups: list = field(default_factory=list)

    @property
    def plans(self) -> list:
        """Every room under this org, the organisation's own first."""
        return ([self.own] if self.own else []) + list(self.groups)

    @property
    def room_count(self) -> int:
        return len(self.plans)

    @property
    def has_rooms_to_choose_between(self) -> bool:
        """Whether the org needs a master control at all.

        One room is not a choice about an organisation, it IS the room — and
        wrapping it in a disclosure to reveal a single line is a click that
        buys nothing. Those render exactly as they did before.
        """
        return self.room_count > 1

    @property
    def change_count(self) -> int:
        return sum(p.change_count for p in self.plans)

    @property
    def conflict_count(self) -> int:
        return sum(len(p.conflicts) for p in self.plans)


def group_by_org(plans: list) -> list[OrgCarry]:
    """Gather RoomPlans under their organisation, keeping the incoming order.

    build_plan already walks rooms_for, which is ordered by org name and then
    by group name, so an ordered gather preserves both without re-sorting.
    """
    by_org: dict = {}
    order: list = []
    for plan in plans:
        org = plan.room.org
        entry = by_org.get(org.id)
        if entry is None:
            entry = by_org[org.id] = OrgCarry(org=org)
            order.append(entry)
        if plan.room.group is None:
            entry.own = plan
        else:
            entry.groups.append(plan)
    return order


# ---------------------------------------------------------------------------
# Carrying picks into a room you have only just joined
# ---------------------------------------------------------------------------
#
# WHAT WENT WRONG, in the client's words (16 Sep 2026):
#
#   "One of the team signed up and created a new account. Neither my tips nor
#    other tipper in the system showed up with their existing tips. It asked us
#    to tip again when we have already tipped."
#
# Everything above this line carries a pick FORWARD at the moment it is made:
# you tip in the work comp, and the same eight picks offer to land in the
# mates' comp too. That is the only direction it ever worked in. Joining a new
# room was the other direction and nothing covered it — a member of three
# comps who joined a fourth arrived with an empty slate for a round they had
# already tipped three times, and the only thing the app could think of to say
# was "you haven't tipped yet".
#
# It looks worse than an empty slate, too. Because rounds are per-org, the new
# comp holds its own copies of fixtures that have already been played, so the
# newcomer's ladder reads 0/0 next to people who have been tipping all season
# — which is what the screenshot attached to that email showed.
#
# THE RULE THAT MAKES THIS HONEST. A tip may only be copied onto a match that
# had not started when the original was made. That is the whole integrity
# question here: copying a pick onto a game whose result is already known is
# not carrying a decision across, it is scoring one after the fact. Every row
# written here is a decision this person demonstrably made in time, in another
# room, and the timestamp on the source tip is the evidence.
#
# WHAT IT LEAVES ALONE
#   * auto-assigned tips. The away-side default is what the system does when
#     you did not pick; carrying it would spread a non-decision across rooms
#     and dress it up as one. Those rooms get their own default from
#     backdate_missed_tips, which is the function for exactly that.
#   * disagreements. Somebody who tipped their own club at home and against it
#     at work meant both, and there is no answer to which of the two a third
#     room should get. Where the sources disagree, nothing is written and the
#     slate is left for them — the same rule the rest of this module follows.
#   * anything already in the target room. Re-running is a no-op, which is what
#     lets it be called from three different join paths without coordination.
#
# WHY IT WRITES ROWS RATHER THAN CALLING submit_tip. submit_tip refuses a
# locked match and refuses a round outside the organisation's tipping window,
# and both refusals are right for somebody making a pick — neither is right for
# replaying one they already made. Most of what carries is for rounds that have
# already been played, which submit_tip cannot write at all.
#
# The window is the one that is a judgement call rather than an obvious one. An
# org whose window opens three rounds ahead can end up holding a carried pick
# for round five, which its own screens would not yet have let the member make
# there. That is accepted: it is their pick, made in time, in another room, and
# the alternative is asking them to make it twice — which is the complaint this
# exists to answer. They can still change it when the round opens.


def carry_existing_tips(user, org, *, group=None) -> int:
    """Fill a newly joined room from the picks this member already made.

    Returns how many tips were written. Safe to call more than once, and safe
    to call on an org with no fixtures yet — it simply finds nothing.

    ``group=None`` is the organisation's own ladder, a real destination rather
    than a missing one, so joining an org and then a group inside it carries
    into both, separately, exactly as they are scored.
    """
    from orgs.models import OrgMember

    from .services import _recalculate_tips_for_match

    # Same role rule as the missed-tip default: a Team Manager who runs the
    # league and never makes a pick is not entered into a season behind their
    # back, even with picks that are genuinely theirs from somewhere else.
    if not OrgMember.objects.filter(user=user, org=org).exclude(
        role=OrgMember.ROLE_MANAGER,
    ).exists():
        return 0

    # The fixtures in the room being filled, keyed the only way the same game
    # can be recognised across organisations.
    targets = {
        m.external_id: m
        for m in Match.objects.filter(round__org=org)
        .exclude(external_id="")
        .select_related("round")
    }
    if not targets:
        return 0

    already = set(
        Tip.objects.filter(user=user, org=org, group=group)
        .values_list("match__external_id", flat=True)
    )
    wanted = set(targets) - already
    if not wanted:
        return 0

    # Every pick this person has made elsewhere for those same games. Excluding
    # this room by (org, group) rather than by org alone: a member carrying
    # into a GROUP should be able to draw on what they tipped for the
    # organisation itself, which is a different room in the same org.
    sources = (
        Tip.objects.filter(user=user, match__external_id__in=wanted, is_auto=False)
        .exclude(org=org, group=group)
        .select_related("match")
    )

    # external_id -> {"home"} or {"home", "away"}, plus the earliest timestamp
    # we can show for it. A set with two things in it is a disagreement and is
    # dropped below.
    picks: dict[str, set] = {}
    made_at: dict[str, object] = {}
    for tip in sources:
        ext = tip.match.external_id
        picks.setdefault(ext, set()).add(tip.selection)
        seen = made_at.get(ext)
        if seen is None or tip.submitted_at < seen:
            made_at[ext] = tip.submitted_at

    rows = []
    for ext, selections in picks.items():
        if len(selections) != 1:
            continue                        # they meant both; carry neither
        target = targets[ext]
        # THE INTEGRITY CHECK. The pick has to predate the game it is being
        # copied onto. `submitted_at` is auto_now, so it is when the tip was
        # last changed — which is the conservative reading, and the one that
        # cannot let a pick edited after kickoff through.
        if made_at[ext] >= target.kickoff_at:
            continue
        rows.append(Tip(
            user=user, match=target, org=org, group=group,
            selection=next(iter(selections)),
            # NOT is_auto. These are the member's own picks, made in time, and
            # they belong in the accuracy record the same as any other.
            is_auto=False,
        ))
    if not rows:
        return 0
    # ignore_conflicts so two joins racing cannot collide on the per-context
    # unique key — whichever row lands first is the same row.
    Tip.objects.bulk_create(rows, ignore_conflicts=True)

    # Score them. A carried pick on a round that finished in March will never
    # be revisited by anything else, and an ungraded tip leaves the member on
    # the zero this whole function exists to stop them sitting on.
    for match in {r.match for r in rows}:
        if match.result is not None:
            _recalculate_tips_for_match(match)
    return len(rows)
