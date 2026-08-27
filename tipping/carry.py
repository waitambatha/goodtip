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
