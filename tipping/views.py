import logging
from types import SimpleNamespace
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from billing.donations import donation_summary
from catalog.models import GoodListConfig, OrganisationType, State
from orgs.models import Group, OrgMember, Organisation
from . import national_ladder
from .models import LadderEntry, Match, Round, Team, Tip
from .services import (
    annotate_play_state, clear_tip, competition_filter, current_round,
    leaderboard_for_family, leaderboard_for_org, submit_tip, tip_window,
    user_org_stats, user_rank_in_org,
)

# What a control posts to mean "I have changed my mind about tipping this one
# at all", as opposed to "home" or "away". Named rather than spelled inline in
# four places: the value travels from a hidden radio on the dashboard, from an
# hx-vals on My Tips and from the round page's own fetch, and all three have to
# agree with what the views below test for.
NO_TIP = "none"


def _require_member(user, org):
    return OrgMember.objects.filter(user=user, org=org).exists()


def _group(request, org):
    """The group this request is tipping in, or None for the organisation.

    One helper rather than a call to orgs.context in each view, so a view that
    forgets it fails to import rather than quietly writing a group's tips into
    the organisation's ladder.
    """
    from orgs.context import current_group

    return current_group(request, org)


@login_required
def match_state_partial(request, match_id: int):
    """Just the score/clock block for one fixture, for the in-play poll.

    The live sync refreshes scores every two minutes and, before this, none of
    that reached a page already open — the number changed in the database while
    the reader watched a stale one. Re-rendering the whole fixture card would
    have worked too, but the card carries the tip controls, and swapping those
    out from under someone mid-tap is how you lose a selection they thought
    they had made.

    Membership is deliberately not checked. A fixture is not private — the same
    score is on the AFL's own website — and the tip, which IS private, is not
    in this fragment. Requiring a specific org membership would also mean
    resolving which org the reader is viewing as, for a fragment identical
    across all of them.
    """
    match = get_object_or_404(
        Match.objects.select_related("home_team", "away_team"), pk=match_id,
    )
    return render(request, "components/_match_state.html", {"match": match, "oob": True})


@login_required
def tip_round_view(request, org_id: int, round_id: int):
    org = get_object_or_404(Organisation, pk=org_id)
    if not _require_member(request.user, org):
        return HttpResponseForbidden()
    round_obj = get_object_or_404(Round, pk=round_id, org=org)
    matches = list(round_obj.matches.select_related("home_team", "away_team").order_by("kickoff_at"))
    existing_tips = {
        t.match_id: t.selection
        for t in Tip.objects.filter(user=request.user, match__in=matches, org=org, group=_group(request, org))
    }
    rows = []
    for m in matches:
        rows.append({
            "match": m,
            "tip": existing_tips.get(m.id),
            "save_url": reverse("tipping:tip_save", args=[org.id, round_obj.id, m.id]),
        })
    return render(request, "tip_round.html", {
        # "locked" now means every match has started, not that the round's
        # first kickoff has passed — the page is only read-only once there is
        # genuinely nothing left to tip.
        "org": org, "round": round_obj, "rows": rows,
        "locked": all(m["match"].is_locked for m in rows) if rows else round_obj.is_locked,
    })


@login_required
@require_POST
def tip_save_partial(request, org_id: int, round_id: int, match_id: int):
    org = get_object_or_404(Organisation, pk=org_id)
    if not _require_member(request.user, org):
        return HttpResponseForbidden()
    match = get_object_or_404(Match, pk=match_id, round_id=round_id, round__org=org)
    selection = request.POST.get("selection")
    # Pressing the team you already picked takes the tip back rather than
    # re-saving it. A radio cannot return to "none" on its own, so without this
    # the only way out of a pick was to pick the other side — see clear_tip.
    undo = selection in (NO_TIP, "", None)
    try:
        if undo:
            clear_tip(user=request.user, match=match, org=org,
                      group=_group(request, org))
            selection = ""
        else:
            submit_tip(user=request.user, match=match, org=org,
                       selection=selection, group=_group(request, org))
    except ValueError as e:
        return HttpResponse(f"<span class='text-red-400 text-xs'>{e}</span>", status=400)
    if request.POST.get("view") == "mytips":
        # The whole card comes back, not just the picker: the chosen club is
        # now shown by the club's own row, so a swap that replaced only the
        # buttons would leave the previous pick still looking selected.
        from matchreader.services import read_match_verbose

        return render(request, "partials/fixture_card.html", {
            "org": org, "match": match, "selection": selection,
            "editable": True, "saved": True, "mode": "htmx",
            "reader": read_match_verbose(match),
        })
    return render(request, "partials/tip_saved.html", {
        "match": match, "selection": selection, "undone": undo,
    })


@login_required
@require_POST
def tip_round_confirm(request, org_id: int, round_id: int):
    """Bulk-confirm a slate of picks made inline on the dashboard.

    Each fixture posts as match_<id>=home|away; anything unpicked is simply
    absent. Picks stay editable (here, on the round page or from My Tips)
    until the round locks.
    """
    org = get_object_or_404(Organisation, pk=org_id)
    if not _require_member(request.user, org):
        return HttpResponseForbidden()
    round_obj = get_object_or_404(Round, pk=round_id, org=org)
    dest = f"{reverse('dashboard')}?org={org.id}"
    # No round-level gate. A round's lockout is its FIRST kickoff, so refusing
    # the whole submission once that passed also refused games three days away
    # that had not started — someone who missed Thursday night lost Saturday
    # too. Locking is a property of a match, and submit_tip already enforces it
    # per match, so a late tipper keeps every game that has not begun.
    saved, skipped, cleared = 0, 0, 0
    picks = {}
    for match in round_obj.matches.all():
        selection = request.POST.get(f"match_{match.id}")
        # An explicit "no tip" is a decision, not an absence. A fixture the
        # member never touched simply isn't in the POST and is left alone; one
        # they un-picked posts NO_TIP, and any tip already saved for it goes.
        if selection == NO_TIP:
            try:
                if clear_tip(user=request.user, match=match, org=org,
                             group=_group(request, org)):
                    cleared += 1
            except ValueError:
                skipped += 1
            continue
        if selection not in ("home", "away", "draw"):
            continue
        try:
            submit_tip(user=request.user, match=match, org=org, selection=selection,
                       group=_group(request, org))
            saved += 1
            picks[match.id] = selection
        except ValueError:
            skipped += 1
    if saved:
        note = f"{saved} tip{'s' if saved != 1 else ''} confirmed — find them under My Tips. You can change any pick until the round locks."
        if cleared:
            note += f" {cleared} taken back."
        if skipped:
            note += f" ({skipped} match{'es' if skipped != 1 else ''} had already kicked off and couldn't be changed.)"
        messages.success(request, note)
    elif cleared:
        messages.success(
            request,
            f"{cleared} tip{'s' if cleared != 1 else ''} taken back. "
            "You can pick again any time before the round locks.",
        )
    elif skipped:
        messages.error(request, "Those matches have already kicked off — tips there are final.")
    else:
        messages.info(request, "Tap a team on each match to pick it, then press Confirm my tips.")
    if saved:
        onward = _carry(request, org, picks)
        if onward is not None:
            return onward
    return redirect(dest)


@login_required
@require_POST
def tip_confirm_upcoming(request, org_id: int):
    """Confirm picks across whatever fixtures were on screen, in any round.

    The dashboard used to show one round at a time and post to a round-scoped
    URL. That fell apart once "show me everything still to play" became the
    rule: a member looking at the back half of this round and the whole of the
    next has a slate that spans rounds, and a per-round endpoint cannot accept
    it.

    Matches are read from the posted keys rather than from a round, and each is
    re-fetched scoped to this org — a member cannot confirm a tip into someone
    else's league by editing the form, whatever ids they post. submit_tip
    enforces the per-match lock, so a game that kicked off while the page was
    open is skipped and reported rather than silently accepted.
    """
    org = get_object_or_404(Organisation, pk=org_id)
    if not _require_member(request.user, org):
        return HttpResponseForbidden()

    picks = _posted_picks(request, org)
    matches = {
        m.id: m
        for m in Match.objects.filter(pk__in=picks, round__org=org).select_related("round")
    }

    saved, skipped = 0, 0
    for mid, selection in picks.items():
        match = matches.get(mid)
        if match is None:
            continue  # not this org's match — ignore rather than error
        try:
            submit_tip(user=request.user, match=match, org=org, group=_group(request, org),
                       selection=selection)
            saved += 1
        except ValueError:
            skipped += 1

    # Fixtures the member took a tip back on. Same slate, same submit.
    cleared = 0
    for match in _posted_clears(request, org):
        try:
            if clear_tip(user=request.user, match=match, org=org,
                         group=_group(request, org)):
                cleared += 1
        except ValueError:
            skipped += 1

    if saved:
        note = (f"{saved} tip{'s' if saved != 1 else ''} confirmed — find them under "
                "My Tips. You can change any pick until that game kicks off.")
        if cleared:
            note += f" {cleared} taken back."
        if skipped:
            note += (f" ({skipped} match{'es' if skipped != 1 else ''} had already "
                     "started and couldn't be changed.)")
        messages.success(request, note)
    elif cleared:
        messages.success(
            request,
            f"{cleared} tip{'s' if cleared != 1 else ''} taken back. "
            "You can pick again any time before the game starts.",
        )
    elif skipped:
        messages.error(request, "Those matches have already kicked off — tips there are final.")
    else:
        messages.info(request, "Tap a team on each match to pick it, then press Confirm my tips.")

    if saved:
        onward = _carry(request, org, {
            mid: sel for mid, sel in picks.items() if mid in matches
        })
        if onward is not None:
            return onward
    return redirect(f"{reverse('dashboard')}?org={org.id}")


# ---------------------------------------------------------------------------
# Carrying picks into a member's other rooms
# ---------------------------------------------------------------------------
CARRY_SESSION_KEY = "tip_carry_pending"


def _carry(request, org, picks):
    """Deal with the member's other rooms after a confirm. See tipping.carry.

    Returns a redirect to the review screen when one is needed, or None to let
    the caller finish normally — which is what happens for the overwhelming
    majority: somebody in exactly one room has nothing to carry anywhere, and
    must not be shown a screen about it.
    """
    from accounts.models import User

    from .carry import Room, apply_plan, build_plan

    mode = getattr(request.user, "tip_carry_mode", User.CARRY_ASK)
    if mode == User.CARRY_NONE:
        return None
    source = Room(org=org, group=_group(request, org))
    plans = [p for p in build_plan(request.user, picks, source) if p.has_work]
    if not plans:
        return None

    # ALREADY ANSWERED. The confirm sheet now asks about carrying BEFORE the
    # save, so by the time we get here the member has usually made the call
    # and it is posted alongside the slate. Asking a second time — which is
    # what redirecting to the standalone screen would do — is the bug this
    # marker exists to prevent. The screen stays reachable for the no-JS path,
    # where the marker is simply absent.
    if request.POST.get("carry_answered"):
        _remember_carry_mode(request)
        result = apply_plan(
            request.user, plans,
            rooms=set(request.POST.getlist("room")),
            overrides=set(request.POST.getlist("override")),
        )
        if result["carried"] or result["overwritten"]:
            messages.success(request, _carry_message(result))
        return None

    if mode == User.CARRY_ALL:
        # "Yes, and never ask me again" carries into rooms with nothing in
        # them. It deliberately does NOT overwrite a pick that disagrees:
        # turning off the question is not consent to have a deliberate
        # different tip replaced, and overriding one is always an explicit
        # act. Those are reported instead.
        result = apply_plan(
            request.user, plans,
            rooms={p.room.key for p in plans}, overrides=set(),
        )
        messages.success(request, _carry_message(result))
        return None

    request.session[CARRY_SESSION_KEY] = {
        "org": org.id,
        "group": source.group.id if source.group else None,
        "picks": {str(k): v for k, v in picks.items()},
    }
    return redirect("tipping:tip_carry", org_id=org.id)


def _remember_carry_mode(request) -> None:
    """Persist "always"/"never" when the member ticked it. Same field either
    way, so the sheet and the standalone screen cannot drift apart."""
    from accounts.models import User

    remember = request.POST.get("remember")
    if remember in (User.CARRY_ALL, User.CARRY_NONE):
        request.user.tip_carry_mode = remember
        request.user.save(update_fields=["tip_carry_mode"])


def _carry_message(result) -> str:
    """Plain-language account of what carrying just did."""
    bits = []
    if result["carried"]:
        where = ", ".join(result["rooms"])
        bits.append(
            f"{result['carried']} tip{'s' if result['carried'] != 1 else ''} "
            f"carried across to {where}."
        )
    if result["overwritten"]:
        bits.append(
            f"{result['overwritten']} existing pick"
            f"{'s were' if result['overwritten'] != 1 else ' was'} replaced."
        )
    if result["kept"]:
        bits.append(
            f"{result['kept']} room{'s' if result['kept'] != 1 else ''} kept "
            "the different pick you'd already made there."
        )
    return " ".join(bits) or "Nothing to carry — your other groups were already up to date."


@login_required
@require_POST
def carry_preview(request, org_id: int):
    """The carry step, rendered as a fragment BEFORE anything is saved.

    WHY THIS EXISTS AS ITS OWN ENDPOINT. Carrying used to be a screen you
    landed on *after* confirming, which put the two decisions in the wrong
    order: you approved a slate, it was written, and only then were you asked
    the bigger question — should this go to your other four groups too. The
    client's note was blunter than that; the screen also looked like a form
    from a different product.

    So the question moves in front of the save. `build_plan` writes nothing
    and takes picks as a plain dict, so it can answer "what WOULD this do"
    from the radios still sitting on the page. Nothing here mutates anything;
    the single write happens once, at the end, when the member confirms.

    Returns 204 when there is nothing to ask about — the overwhelming
    majority, who tip in exactly one room — so the sheet can skip the step
    entirely rather than showing an empty pane.
    """
    from accounts.models import User

    from .carry import Room, build_plan, group_by_org

    org = get_object_or_404(Organisation, pk=org_id)
    if not _require_member(request.user, org):
        return HttpResponseForbidden()

    mode = getattr(request.user, "tip_carry_mode", User.CARRY_ASK)
    if mode == User.CARRY_NONE:
        return HttpResponse(status=204)

    picks = _posted_picks(request, org)
    if not picks:
        return HttpResponse(status=204)

    source = Room(org=org, group=_group(request, org))
    plans = [p for p in build_plan(request.user, picks, source) if p.has_work]
    if not plans:
        return HttpResponse(status=204)

    return render(request, "partials/carry_step.html", {
        "org": org,
        "source": source,
        # Rooms gathered under their organisation, so the sheet can offer one
        # decision per org with its groups underneath. `plans` stays as it was
        # for anything still counting rooms; nothing about the POST changes.
        "org_plans": group_by_org(plans),
        "plans": plans,
        "total_rooms": len(plans),
        "total_orgs": len({p.room.org.id for p in plans}),
        "total_changes": sum(p.change_count for p in plans),
        "conflict_count": sum(len(p.conflicts) for p in plans),
        # "Yes, always" is offered here rather than only on the profile,
        # because this is the moment the member has an opinion about it.
        "auto_mode": mode == User.CARRY_ALL,
    })


def _posted_picks(request, org) -> dict:
    """{match_id: selection} from a posted slate, scoped to this org.

    Shared by the confirm and the carry preview so the plan is built from
    exactly the fixtures the confirm will write — re-deriving it separately in
    each was how the two could disagree. Ids that are not this org's matches
    are dropped rather than raising: a posted id nobody can tip is not an
    error worth failing a whole slate over.
    """
    ids = []
    for key, value in request.POST.items():
        if not key.startswith("match_") or value not in ("home", "away", "draw"):
            continue
        raw = key[len("match_"):]
        if raw.isdigit():
            ids.append(int(raw))
    if not ids:
        return {}
    valid = set(
        Match.objects.filter(pk__in=ids, round__org=org).values_list("id", flat=True)
    )
    return {i: request.POST[f"match_{i}"] for i in ids if i in valid}


def _posted_clears(request, org) -> list:
    """Match ids the slate explicitly un-picked, scoped to this org.

    Kept apart from _posted_picks rather than folded into it as a third
    selection value, because that dict feeds the carry planner — and a
    "carry no tip into your other groups" plan is not a thing. What the
    planner wants is the picks; what the confirm additionally has to act on
    is the take-backs, and they are different questions.
    """
    ids = []
    for key, value in request.POST.items():
        if not key.startswith("match_") or value != NO_TIP:
            continue
        raw = key[len("match_"):]
        if raw.isdigit():
            ids.append(int(raw))
    if not ids:
        return []
    return list(
        Match.objects.filter(pk__in=ids, round__org=org).select_related("round")
    )


@login_required
def tip_carry_view(request, org_id: int):
    """Review what carrying would do, then do it.

    A GET renders the plan; a POST applies whichever rooms were ticked. The
    picks come from the session rather than from hidden fields — they were
    already saved in the source room by the confirm that redirected here, so
    re-posting them would be asking the browser to hold state the server
    already has.
    """
    from accounts.models import User

    from .carry import Room, apply_plan, build_plan, group_by_org

    org = get_object_or_404(Organisation, pk=org_id)
    if not _require_member(request.user, org):
        return HttpResponseForbidden()

    pending = request.session.get(CARRY_SESSION_KEY) or {}
    dest = f"{reverse('dashboard')}?org={org.id}"
    if pending.get("org") != org.id or not pending.get("picks"):
        return redirect(dest)

    group = None
    if pending.get("group"):
        group = Group.objects.filter(
            pk=pending["group"], org=org, memberships__user=request.user,
        ).first()
    source = Room(org=org, group=group)
    picks = {int(k): v for k, v in pending["picks"].items()}
    plans = [p for p in build_plan(request.user, picks, source) if p.has_work]
    if not plans:
        request.session.pop(CARRY_SESSION_KEY, None)
        return redirect(dest)

    if request.method == "POST":
        remember = request.POST.get("remember")
        _remember_carry_mode(request)
        if request.POST.get("action") == "skip":
            request.session.pop(CARRY_SESSION_KEY, None)
            if remember == User.CARRY_NONE:
                messages.info(
                    request,
                    "Got it — from now on your tips stay in the group you make "
                    "them in. You can change that in your profile.",
                )
            return redirect(dest)
        result = apply_plan(
            request.user, plans,
            rooms=set(request.POST.getlist("room")),
            overrides=set(request.POST.getlist("override")),
        )
        request.session.pop(CARRY_SESSION_KEY, None)
        messages.success(request, _carry_message(result))
        if remember == User.CARRY_ALL:
            messages.info(
                request,
                "From now on your tips carry across automatically. You can "
                "change that in your profile.",
            )
        return redirect(dest)

    return render(request, "tip_carry.html", {
        "org": org,
        "source": source,
        # Same shape as the sheet's, so the two render the same partial and
        # cannot drift apart again — this page and partials/carry_step.html
        # had already grown two separate copies of the same room list.
        "org_plans": group_by_org(plans),
        "plans": plans,
        "total_rooms": len(plans),
        "total_orgs": len({p.room.org.id for p in plans}),
        "total_changes": sum(p.change_count for p in plans),
        "conflict_count": sum(len(p.conflicts) for p in plans),
    })


logger = logging.getLogger(__name__)


def _readers_for(matches) -> dict:
    """MatchReader's take on a slate of fixtures, keyed by match id.

    Only offered BEFORE a game is decided. Once a result is in, a prediction
    is noise at best and an argument at worst, and the screen already shows
    who actually won — so decided games are dropped before the model is asked
    rather than after.

    Batched: read one at a time this cost three queries per fixture, which is
    a round's worth of latency for information that fits in one.
    """
    pending = [m for m in matches if m.phase != "complete"]
    if not pending:
        return {}
    try:
        from matchreader.services import read_matches_verbose

        return read_matches_verbose(pending)
    except Exception:  # noqa: BLE001 — an insight must never break the page
        logger.exception("MatchReader failed for a slate of %d", len(pending))
        return {}


TIPS_PER_PAGE = 30
TIP_TABS = (("all", "All"), ("won", "Correct"), ("lost", "Wrong"), ("missed", "Missed"), ("pending", "Pending"))


def _tip_history(user, org, group):
    """Every game this member has a story on, newest first: what they tipped
    and how it went, and what they let go by.

    A game counts as missed only if they could have tipped it: played since
    they joined, and on GoodTip before it kicked off (a league added to an
    organisation mid-season brings weeks of results nobody was ever offered).
    """
    now = timezone.now()
    tips = {t.match_id: t for t in Tip.objects.filter(user=user, org=org, group=group)}
    member = OrgMember.objects.filter(user=user, org=org).first()
    joined = member.joined_at if member else now
    matches = (
        Match.objects.filter(round__org=org)
        .filter(Q(id__in=list(tips)) | Q(kickoff_at__lte=now, kickoff_at__gte=joined))
        .select_related("round__series__sport", "home_team", "away_team")
        .order_by("-kickoff_at", "-id")
    )
    rows = []
    for m in matches:
        tip = tips.get(m.id)
        if tip is not None:
            outcome = {True: "won", False: "lost"}.get(tip.is_correct, "pending")
        elif m.phase == "upcoming" or m.arrived_after_kickoff:
            continue
        else:
            outcome = "missed"
        rows.append({"match": m, "tip": tip, "outcome": outcome,
                     "points": tip.points_awarded if tip else 0})
    return rows


def _tip_summary(rows) -> dict:
    """The numbers at the top of My Tips, over whichever rows are in view."""
    counts = {key: sum(1 for r in rows if r["outcome"] == key) for key, _label in TIP_TABS[1:]}
    graded = counts["won"] + counts["lost"]
    streak = 0
    for r in rows:
        if r["outcome"] in ("pending", "missed"):
            continue
        if r["outcome"] != "won":
            break
        streak += 1
    return {
        **counts, "all": len(rows), "points": sum(r["points"] for r in rows),
        "strike": round(counts["won"] / graded * 100) if graded else None, "streak": streak,
        # Oldest first, so it reads left to right into the latest result.
        "recent": [r["outcome"] for r in rows if r["outcome"] in ("won", "lost")][:10][::-1],
    }


@login_required
def my_tips_view(request, org_id: int):
    """MY TIPS IS A HISTORY (client, 1 Nov 2026: "just the way we have bet
    history, where you can see the bets that you did, the points earned, what
    did you get right, what did you get wrong"). Tipping is the dashboard's job;
    this page keeps the record: ?series= narrows it to a competition, ?show= to
    an outcome, and ?round= to one round, which is where notifications land.
    """
    org = get_object_or_404(Organisation, pk=org_id)
    if not _require_member(request.user, org):
        return HttpResponseForbidden()
    every = _tip_history(request.user, org, _group(request, org))

    by_series: dict[int, dict] = {}
    for r in every:
        series = r["match"].round.series
        entry = by_series.setdefault(series.id, {"series": series, "points": 0, "count": 0})
        entry["points"] += r["points"]
        entry["count"] += 1
    competitions = sorted(by_series.values(), key=lambda c: (-c["points"], c["series"].name))

    slug = request.GET.get("series", "")
    chosen = next((c["series"] for c in competitions if c["series"].slug == slug), None)
    rows = [r for r in every if chosen is None or r["match"].round.series_id == chosen.id]
    the_round = None
    raw_round = request.GET.get("round", "")
    if raw_round.isdigit():
        the_round = next((r["match"].round for r in rows if r["match"].round_id == int(raw_round)), None)
        if the_round is not None:
            rows = [r for r in rows if r["match"].round_id == the_round.id]

    summary = _tip_summary(rows)
    show = request.GET.get("show", "all")
    if show not in dict(TIP_TABS):
        show = "all"
    listed = rows if show == "all" else [r for r in rows if r["outcome"] == show]
    day = None
    for r in listed:
        this_day = timezone.localtime(r["match"].kickoff_at).date()
        r["new_day"] = this_day != day
        day = this_day

    raw_page = request.GET.get("page", "1")
    page = max(int(raw_page), 1) if raw_page.isdigit() else 1
    end = page * TIPS_PER_PAGE
    keep = {k: v for k, v in (("series", chosen.slug if chosen else ""), ("show", show if show != "all" else ""),
                              ("round", str(the_round.id) if the_round else "")) if v}
    def tab_query(key):
        return urlencode({a: b for a, b in {**keep, "show": key}.items() if key != "all" or a != "show"})

    strike = "–" if summary["strike"] is None else f'{summary["strike"]}%'
    return render(request, "my_tips.html", {
        "org": org, "rows": listed[end - TIPS_PER_PAGE:end], "summary": summary,
        # Each number opens the tips behind it.
        "stats": [
            {"value": summary["points"], "label": "Points", "tone": "points", "key": "all", "query": tab_query("all")},
            {"value": strike, "label": "Strike rate", "tone": "strike", "key": "", "query": tab_query("all")},
            {"value": summary["won"], "label": "Correct", "tone": "won", "key": "won", "query": tab_query("won")},
            {"value": summary["lost"], "label": "Wrong", "tone": "lost", "key": "lost", "query": tab_query("lost")},
            {"value": summary["missed"], "label": "Missed", "tone": "missed", "key": "missed", "query": tab_query("missed")},
            {"value": summary["streak"], "label": "In a row", "tone": "streak", "key": "", "query": tab_query("won")},
        ],
        "competitions": competitions, "chosen": chosen, "the_round": the_round, "show": show,
        "tabs": [{"key": k, "label": v, "count": summary[k],
                  "query": tab_query(k)}
                 for k, v in TIP_TABS],
        "show_query": urlencode({k: v for k, v in keep.items() if k == "show"}),
        "next_query": urlencode({**keep, "page": page + 1}) if end < len(listed) else "",
    })


#: How many rounds the strip shows at once. Five, as asked for: enough to hold
#: the run of a month, short enough that every one of them is a target rather
#: than a list to read.
STRIP_SIZE = 5


def round_strip(numbers, current, back=None, *, size=STRIP_SIZE, values=None):
    """A window of round buttons, and the way to move it.

    ASKED FOR: "the dropdown will show All by default; beside it let's have the
    last 5 rounds, with a button to keep on moving backward, and display what
    round it's showing — and the round you are on highlighted, so I can click
    25 and see the ranking of that round."

    `numbers`   every round that exists, ascending.
    `current`   the round being shown, or None for "all"/latest.
    `back`      how many windows back the reader has stepped, from ?back=.

    The window is anchored on the NEWEST round and walks backwards, because
    that is the direction people look: this week, then last week. Stepping back
    is a page of five at a time rather than one at a time — one at a time is
    the arrows, which is a different gesture for a different distance.

    The selected round is always IN the window even if it is far in the past,
    so a link to round 3 in August opens with round 3 on screen and highlighted
    rather than showing the last five and leaving the reader to hunt.
    """
    numbers = list(numbers)
    if not numbers:
        return None
    try:
        steps = max(0, int(back or 0))
    except (TypeError, ValueError):
        steps = 0

    # Windows are counted from the end: 0 is the newest `size`, 1 the `size`
    # before that, and so on.
    max_steps = max(0, (len(numbers) - 1) // size)
    if current in numbers:
        # A chosen round wins over ?back= — a URL naming a round must show it.
        pos_from_end = len(numbers) - 1 - numbers.index(current)
        steps = pos_from_end // size
    steps = min(steps, max_steps)

    end = len(numbers) - steps * size
    start = max(0, end - size)
    # A FULL PAGE AT THE FAR END. Walking back through eleven rounds otherwise
    # lands on a window of one — the arithmetic is right and the result is a
    # single lonely button where five were promised. Where there is room, the
    # oldest page is the first `size` rounds; the small overlap with the page
    # before it is worth more than a ragged end.
    if start == 0:
        end = min(len(numbers), size)
    window = numbers[start:end]
    # Each button carries what its LINK should say, resolved here rather than
    # in the template: Django templates cannot look a dict up by a variable
    # key, and the leaderboard's buttons post round IDs while the ladder's post
    # round numbers. One shape out of here, one loop in the partial.
    values = values or {}
    return {
        "window": [
            {"n": n, "value": values.get(n, n), "on": n == current} for n in window
        ],
        "numbers": window,
        "current": current,
        # "Older" walks toward round 1; "newer" comes back toward today.
        "older": steps + 1 if steps < max_steps else None,
        "newer": steps - 1 if steps > 0 else None,
        "back": steps,
        "showing": (
            f"Rounds {window[0]}\u2013{window[-1]}" if len(window) > 1
            else f"Round {window[0]}" if window else ""
        ),
        "latest": numbers[-1],
    }


def org_series(org):
    """The competitions this organisation tips, in its own order, no repeats.

    Lifted out of ladder_view because the leaderboard now asks the same
    question, and two copies of "which comps does this league run" is how the
    two pages come to offer different chips for the same league.
    """
    ordered, seen = [], set()
    for comp in org.competitions.select_related().prefetch_related("series"):
        for sr in comp.series.all():
            if sr.id not in seen:
                seen.add(sr.id)
                ordered.append(sr)
    return ordered


def pick_series(options, wanted, *, default_to_first=False):
    """Resolve ?series= / ?comp= against a list of Series.

    ACCEPTS A SLUG OR AN ID. The ladder's picker has always posted the id and
    there are links in the wild carrying one; the chips post the slug, because
    "?comp=nrlw" is a URL somebody can read, share and type. Both resolve here
    so neither has to know about the other.

    `default_to_first` is the ladder, which is a table OF one competition and
    has nothing to show without one. The leaderboard defaults to None instead —
    there, no competition means every competition, which is a real answer and
    the one most people want first.
    """
    if wanted:
        by_slug = next((s for s in options if s.slug == wanted), None)
        if by_slug is not None:
            return by_slug
        by_id = next((s for s in options if str(s.id) == str(wanted)), None)
        if by_id is not None:
            return by_id
    return options[0] if (default_to_first and options) else None


@login_required
def ladder_view(request, org_id: int):
    """The competition ladder — where the TEAMS sit, not the members.

    Distinct from the leaderboard, which ranks tippers. Both are "the table" in
    conversation, so the page says which one it is up front.

    Entries are per (series, season) and shared across every league, so this
    reads rows the sync wrote once rather than anything computed per org.
    """
    org = get_object_or_404(Organisation, pk=org_id)
    if not _require_member(request.user, org):
        return HttpResponseForbidden()

    ordered = org_series(org)
    selected = pick_series(ordered, request.GET.get("series"), default_to_first=True)

    # THE LADDER AS IT STOOD, for any round of the season.
    #
    # ASKED FOR: "users might want to see, let's say the last month, round 10 —
    # who was on top." A ladder is a running total, so "where were we in round
    # 10" is a question it can answer and a stored current-standings table
    # cannot.
    #
    # The current view still reads the stored LadderEntry rows: they carry
    # updated_at, they are what the sync maintains, and there is no reason to
    # recompute the answer that is already written down. A past round is
    # computed by data_sync.ladder.ladder_standings — the SAME arithmetic,
    # lifted out of rebuild_ladder rather than written a second time.
    from data_sync.ladder import ladder_standings, season_rounds

    entries, rounds_available, at_round = [], [], None
    if selected:
        rounds_available = season_rounds(series=selected, season=org.season)
        wanted = request.GET.get("round")
        if wanted and wanted.isdigit() and int(wanted) in rounds_available:
            at_round = int(wanted)

        if at_round is None:
            entries = list(
                LadderEntry.objects.filter(series=selected, season=org.season)
                .select_related("team")
                .order_by("rank")
            )
        else:
            rows = ladder_standings(
                series=selected, season=org.season, up_to_round=at_round,
            )
            teams = Team.objects.in_bulk([r["team_id"] for r in rows])
            # Shaped exactly like a LadderEntry so the template cannot tell
            # which of the two it was handed.
            entries = [
                SimpleNamespace(team=teams.get(r["team_id"]), updated_at=None, **r)
                for r in rows
                if teams.get(r["team_id"]) is not None
            ]

    # HOW MANY GAMES ARE STILL TO COME, per club.
    #
    # Honest about its source: this is what THIS SYSTEM knows is still to be
    # played — scheduled tipping.Match rows for the series, de-duplicated across
    # leagues by (round, home, away), because that table holds one row per
    # fixture PER ORGANISATION and a naive count multiplies every game by the
    # number of leagues tipping it. Where nothing is scheduled it is left out
    # rather than guessed at, and the column shows a dash.
    left_by_team = {}
    if selected:
        seen_fixtures = set()
        for m in (
            Match.objects.filter(
                round__series=selected, round__org__season=org.season,
            )
            .exclude(status=Match.STATUS_COMPLETE)
            .filter(result__isnull=True)
            .values_list("round__round_number", "home_team_id", "away_team_id")
        ):
            if m in seen_fixtures:
                continue
            seen_fixtures.add(m)
            for team_id in (m[1], m[2]):
                left_by_team[team_id] = left_by_team.get(team_id, 0) + 1
    for e in entries:
        e.games_left = left_by_team.get(e.team.id)

    # WHERE EACH TEAM WAS A ROUND AGO (client, 4 Oct 2026: "this team is
    # number two on this ladder; the previous round it was number six — it
    # went up"). The table as it stood after the round before the one shown,
    # by the same arithmetic as any past round, and the difference per club.
    prev_round = None
    if selected and rounds_available:
        ordered_rounds = sorted(rounds_available)
        ref = at_round if at_round is not None else ordered_rounds[-1]
        earlier = [r for r in ordered_rounds if r < ref]
        prev_round = earlier[-1] if earlier else None
    if prev_round is not None:
        before = {
            r["team_id"]: r["rank"]
            for r in ladder_standings(series=selected, season=org.season, up_to_round=prev_round)
        }
        for e in entries:
            was = before.get(e.team.id)
            e.prev_rank = was
            e.move = (was - e.rank) if was is not None else 0

    # Which teams this member has tipped most, so the ladder connects to their
    # own season rather than being a bare table.
    my_team_ids = set(
        Tip.objects.filter(user=request.user, org=org, group=_group(request, org), is_correct=True)
        .values_list("match__home_team_id", flat=True)
    )

    # THE TABLE ON ITS OWN, for the competition chips and the round strip.
    #
    # Client, 20 Sep 2026: "let it load on the area of context", and — of the
    # same press — that the full-page loader firing "does not make sense".
    # Both were one bug: the filters were ordinary links, so choosing NRL
    # navigated, and a navigation inside the member app raises the whole-page
    # splash over the top of the scoped one. Answering the fragment means the
    # press never leaves the page, so only the table's own loader runs, in that
    # competition's colour.
    #
    # Built from the SAME context as the full page and rendering the same
    # partial, so the fragment cannot drift from what a reload would produce.
    # The href on every control is unchanged, so a copied address, a new tab
    # and a browser with no JavaScript all still work.
    # THE ROUND'S RESULTS, under the table (client, 28 Oct 2026: "when I go
    # to the ladder, I can click this round — this team won by this point").
    # Read from the results store (matchreader.HistoricalMatch, one row per
    # real game, no organisation), for the round on screen — the latest when
    # none is picked.
    round_results, results_round = [], None
    if selected and rounds_available:
        from matchreader.models import HistoricalMatch

        results_round = at_round if at_round is not None else max(rounds_available)
        for hm in (HistoricalMatch.objects.filter(series=selected, season=org.season.year,
                                                  round_number=results_round)
                   .select_related("home_team", "away_team").order_by("kickoff_at")):
            margin = abs((hm.home_score or 0) - (hm.away_score or 0))
            round_results.append({
                "m": hm, "margin": margin,
                "winner": "home" if hm.home_score > hm.away_score else "away" if hm.away_score > hm.home_score else "draw",
            })

    ctx = {
        "org": org,
        "round_results": round_results,
        "results_round": results_round,
        "series_options": ordered,
        "selected_series": selected,
        "entries": entries,
        "my_team_ids": my_team_ids,
        "updated_at": entries[0].updated_at if entries else None,
        "round_strip": round_strip(
            rounds_available, at_round, request.GET.get("back"),
        ),
        "at_round": at_round,
        "prev_round": prev_round,
        # Stepping through rounds must not silently change which competition's
        # ladder is on screen.
        "ladder_keep": f"series={selected.slug}" if selected else "",
    }

    if request.GET.get("pane") == "table":
        return render(request, "partials/ladder_table.html", ctx)
    return render(request, "ladder.html", ctx)


def _code_chart(season, options) -> list[dict]:
    """A member's accuracy per competition, with each one's colour and logo."""
    by_slug = {s.slug: s for s in options}
    return [dict(c, series=by_slug.get(c["slug"])) for c in season["codes"] if by_slug.get(c["slug"])]


@login_required
def my_certificates_view(request, org_id: int):
    """Where a member stands against a certificate, competition by competition:
    what it would take while the season runs, and the download once it has
    ended (client, 2 Nov 2026). Top three only."""
    from . import certificates as certs

    org = get_object_or_404(Organisation, pk=org_id)
    if not _require_member(request.user, org):
        return HttpResponseForbidden()
    return render(request, "my_certificates.html", {
        "org": org,
        "progress": certs.certificate_progress(request.user, org),
        "can_certify": _can_certify(request.user, org),
    })


@login_required
def my_stats_view(request, org_id: int):
    """A member's own season, in more detail than a row of a table can hold.

    ASKED FOR: "I should have stats — my stats in the leaderboard, with some
    small visualisation, some charts and cards, that shows my performance
    generally: have I grown or have I dropped, what is my strongest competition,
    my weakness."

    Scoped exactly like the leaderboard it hangs off — same org, same room, same
    optional competition — so a figure here can never describe a different
    season from the rank it sits behind.
    """
    from .stats import bars, donut, member_season, spark

    org = get_object_or_404(Organisation, pk=org_id)
    if not _require_member(request.user, org):
        return HttpResponseForbidden()

    options = org_series(org)
    selected = pick_series(options, request.GET.get("comp"))
    group = _group(request, org)
    st = member_season(request.user, org, group=group, series=selected)
    # Rank AND the field it was read from the middle of, together — read from
    # the same board the competition filter scopes (client, 12 Oct 2026: the
    # rank card used to stay "all competitions" even with a filter chip on,
    # disagreeing with every other number on the page).
    board = leaderboard_for_org(org, group=group, series=selected)
    total_rankers = len(board)
    mine = next((u for u in board if u.id == request.user.id), None)
    rank = mine.rank if mine else None
    return render(request, "my_stats.html", {
        "org": org,
        "comp_options": options,
        "selected_comp": selected,
        "stats": st,
        "rank": rank,
        "total_rankers": total_rankers,
        # Five pictures, each answering a question the table cannot, no two
        # the same colour (client, 12 Oct 2026) — the house SVG/CSS chart
        # kit (tipping/stats.py), not a JS charting library.
        "chart_accuracy": spark(
            [r["accuracy"] for r in st["run"]], floor=0, ceiling=100,
        ),
        "chart_points": bars(st["run"], key="points"),
        "chart_codes": _code_chart(st, options),
        "chart_split": donut(st["right"], st["played"]),
        "chart_standing": donut(total_rankers - rank + 1, total_rankers) if rank else None,
    })


@login_required
def team_stats_view(request, org_id: int, team_id: int):
    """One club's season, reached by pressing its row on the ladder.

    ASKED FOR: "for the ladder it will be for the teams — be able to click a
    team, see its stats, how has it been performing, for people who are more
    detailed with stats."

    The club's season is a fact about the competition, not about the league
    reading it, so the numbers are the same whichever organisation you come
    from. The org in the URL is there for the permission check and for the way
    back to the right ladder.
    """
    from .stats import bars, donut, spark, team_season

    org = get_object_or_404(Organisation, pk=org_id)
    if not _require_member(request.user, org):
        return HttpResponseForbidden()
    team = get_object_or_404(Team, pk=team_id)

    entry = (
        LadderEntry.objects.filter(series=team.series, season=org.season, team=team)
        .select_related("team")
        .first()
    )
    st = team_season(team, team.series, org.season)
    return render(request, "team_stats.html", {
        "org": org,
        "team": team,
        "series": team.series,
        "entry": entry,
        "stats": st,
        "chart_margin": spark([r["margin"] for r in st["run"]]),
        "chart_scored": spark([r["for"] for r in st["run"]], floor=0),
        "chart_conceded": spark([r["against"] for r in st["run"]], floor=0),
        "chart_split": donut(st["won"], st["played"]),
        "chart_venue": bars(
            [
                {"name": "At home", **st["home"], "value": st["home"]["win_pct"] or 0},
                {"name": "Away", **st["away"], "value": st["away"]["win_pct"] or 0},
            ],
        ),
    })


@login_required
def comp_stats_view(request, org_id: int):
    """The whole competition at once — every club, not one of them.

    ASKED FOR: "let's add STATISTICS, where now we will see not individual but
    overall statistics of all the teams."

    The team page answers "how is this club going"; this answers the questions
    that need every club in front of you, because each of them is a comparison:
    who scores, who concedes, whether home advantage is real this season.
    """
    from .stats import bars, competition_season

    org = get_object_or_404(Organisation, pk=org_id)
    if not _require_member(request.user, org):
        return HttpResponseForbidden()

    options = org_series(org)
    selected = pick_series(options, request.GET.get("series"), default_to_first=True)
    st = competition_season(selected, org.season) if selected else None
    return render(request, "comp_stats.html", {
        "org": org,
        "series_options": options,
        "selected_series": selected,
        "stats": st,
        "chart_attack": bars(
            sorted(st["clubs"], key=lambda c: -(c["avg_for"] or 0)), key="avg_for",
        ) if st and st["clubs"] else [],
        "chart_defence": bars(
            sorted(st["clubs"], key=lambda c: (c["avg_against"] or 9999)), key="avg_against",
        ) if st and st["clubs"] else [],
    })


@login_required
def leaderboard_view(request, org_id: int):
    org = get_object_or_404(Organisation, pk=org_id)
    if not _require_member(request.user, org):
        return HttpResponseForbidden()
    # WHICH COMPETITION, and the rounds that belong to it.
    #
    # ASKED FOR: "let's say I tip in all competitions — I can filter, say NRL,
    # and see the points, see where I am and who is leading in the NRL." A
    # member tipping four codes has one total made of four seasons going
    # differently, and the combined number cannot say which of them they are
    # any good at.
    #
    # No comp means every comp, which is a real answer and the one most people
    # want on arrival — so unlike the ladder this does not fall back to the
    # first one.
    comp_options = org_series(org)
    selected_comp = pick_series(comp_options, request.GET.get("comp"))
    rounds = Round.objects.filter(org=org).order_by("-round_number")
    if selected_comp is not None:
        # Or the round list offers AFLW round 4 while the board is showing the
        # NRL, and choosing it silently empties the table.
        rounds = rounds.filter(series=selected_comp)
    selected_round_id = request.GET.get("round")
    round_filter = None
    if selected_round_id and selected_round_id != "all":
        try:
            round_filter = int(selected_round_id)
        except ValueError:
            round_filter = None
    # §8: one underlying competition, two views. "national" ranks every
    # member across the parent org and all its children; "local" (default)
    # filters to this org only. Standalone orgs only ever see local.
    is_family = org.is_child or org.children.exists()
    scope = request.GET.get("scope", "local")
    # The ladder you are standing on. Inside a group it is that group's, made
    # of that group's members and the tips they made there; outside one it is
    # the organisation's, and excludes every tip made inside a group.
    group = _group(request, org)
    if scope == "national" and is_family and group is None:
        board = leaderboard_for_family(org, round_id=round_filter, series=selected_comp)
    else:
        scope = "local"
        board = leaderboard_for_org(
            org, round_id=round_filter, group=group, series=selected_comp,
        )
    # Ranks come from the board, which resolved them under the addendum's
    # tiebreakers. Recomputing "equal points means equal rank" here would
    # undo that: two tippers separated on the paired comp would be reported
    # level again, and the leaderboard would disagree with the dashboard.
    # MORE THAN RANK, TIPPER, CORRECT, POINTS. "I think we can have more detail
    # than that."
    #
    # Accuracy is the column people actually argue about — 8 from 9 and 80 from
    # 90 are the same rate and a very different season, and neither of the two
    # numbers already here says so. Behind is the gap to the leader, which is
    # the second thing anybody looks for after their own position and was
    # arithmetic every reader was doing in their head.
    #
    # Both are derived here rather than in the template: a percentage computed
    # with `widthratio` is unreadable, and "points behind" needs the top score,
    # which a row does not have.
    top_points = board[0].points if board else 0
    ranked = [
        {"rank": u.rank, "user": u, "points": u.points,
         "tips_correct": u.correct_all, "tips_total": u.graded_all,
         "is_tied": u.is_tied,
         "accuracy": round(u.correct_all / u.graded_all * 100) if u.graded_all else None,
         "behind": top_points - u.points,
         # The badge belongs to whoever is on the spot right now, so it is read
         # off the live board and moves the moment the order does.
         "place": u.rank if u.rank <= 3 and u.points > 0 else None}
        for u in board
    ]
    # THE CERTIFICATE LINE (client, 2 Nov 2026): the top three finish with a
    # certificate, so everyone else can see how far off it they are.
    podium = [r for r in ranked if r["place"]]
    third = podium[-1]["points"] if len(podium) >= 3 else None
    for r in ranked:
        r["to_top3"] = third - r["points"] + 1 if third is not None and not r["place"] else None
    if request.headers.get("HX-Request") and not request.headers.get("HX-Boosted"):
        return render(request, "partials/leaderboard_table.html", {
            "ranked": ranked, "me": request.user, "selected_comp": selected_comp,
        })
    # What the competition filter must carry through, and what it must not.
    #
    # Scope: yes — "the national board, for the NRL" is a sentence, and losing
    # the scope every time somebody changed code would be maddening.
    #
    # Round: NO. A round id belongs to one competition, so carrying it into
    # another one names a round that comp does not have and empties the table.
    # Changing code resets to all rounds, which is the only honest default.
    comp_keep_pairs = [("scope", scope)] if (is_family and scope == "national") else []
    # The Industry / National / Everyone boards keep their view when the
    # competition changes, or a filter would silently drop you back on your own.
    _view = request.GET.get("view", "org")
    if _view in ("industry", "national", "everyone"):
        comp_keep_pairs = comp_keep_pairs + [("view", _view)]
    comp_keep = "&amp;".join(f"{k}={v}" for k, v in comp_keep_pairs)
    # The ROUND buttons carry the comp as well as the scope — they are inside
    # the competition, not beside it, so a round link that dropped ?comp would
    # widen the board back to every code while its chip still read as selected.
    round_keep_pairs = comp_keep_pairs + (
        [("comp", selected_comp.slug)] if selected_comp is not None else []
    )
    comp_keep_round = "&amp;".join(f"{k}={v}" for k, v in round_keep_pairs)

    # ARROWS EITHER SIDE OF THE ROUND, like the dashboard's navigator — "it
    # should not only be a dropdown, but a nice way I can press arrow left and
    # right the way we have it on the fixtures on the dashboard".
    #
    # Stepping is what people actually do with this control: last round, the one
    # before, back again. A dropdown makes each of those a click, a read and an
    # aim, and the list is eighty rounds long on a league that has been running.
    #
    # "All rounds" is the first stop rather than a separate control, so stepping
    # back from round 1 lands on the season and the sequence has one shape.
    steps = ["all"] + [str(r.id) for r in reversed(list(rounds))]
    here = selected_round_id if selected_round_id in steps else "all"
    at = steps.index(here)
    labels = {"all": "All rounds"}
    for r in rounds:
        labels[str(r.id)] = r.label
    # The strip of round buttons, on the same design as the ladder's — five at
    # a time, the current one highlighted, stepping backwards a page at a time.
    # DE-DUPLICATED. A Round row is per (org, round_number, series), so a league
    # tipping four codes has four round 1s — the strip showed "26 26 27 27" and
    # a window of five held three distinct rounds.
    numbers = sorted({r.round_number for r in rounds})
    here_number = next(
        (r.round_number for r in rounds if str(r.id) == selected_round_id), None,
    )
    # The buttons post round IDs, not numbers: a round number names one round
    # PER COMPETITION, so on an unfiltered board the number 4 is up to five
    # different rounds and only the id says which.
    id_for_number = {}
    for r in rounds:
        id_for_number.setdefault(r.round_number, r.id)
    strip = round_strip(
        numbers, here_number, request.GET.get("back"), values=id_for_number,
    )

    round_nav = {
        "prev": steps[at - 1] if at > 0 else "",
        "next": steps[at + 1] if at < len(steps) - 1 else "",
        "current": here,
        "label": labels.get(here, "All rounds"),
        "prev_label": labels.get(steps[at - 1]) if at > 0 else "",
        "next_label": labels.get(steps[at + 1]) if at < len(steps) - 1 else "",
    }
    # BEYOND YOUR OWN ORGANISATION (client, 4 Oct 2026). The Industry and
    # National boards were under the Ladder menu, which is for teams; they rank
    # people and organisations, so they are views of this page. See
    # tipping/rankings.py for how each is scored and what is named.
    from . import rankings

    board_view = request.GET.get("view", "org")
    if board_view not in ("org", "industry", "national", "everyone"):
        board_view = "org"
    wide_board = None
    if board_view in ("industry", "national"):
        wide_board = rankings.org_board(org, board_view, series=selected_comp)
    elif board_view == "everyone":
        wide_board = rankings.everyone_board(request.user, org, series=selected_comp)
    from . import certificates as certs

    progress = certs.certificate_progress(request.user, org)
    return render(request, "leaderboard.html", {
        # Gold in the middle, whatever the places either side of it are.
        "podium": ([podium[1], podium[0], *podium[2:3]] if len(podium) > 1 else podium),
        "cert_ready": sum(len(p["certificates"]) for p in progress),
        "board_view": board_view,
        "wide_board": wide_board,
        "org": org, "rounds": rounds, "selected_round_id": selected_round_id or "all",
        # Built here rather than in the template. The button now rides inside
        # the room switcher, which takes its href as an `action_url` argument —
        # and a Django template can neither concatenate a {% url %} with a
        # conditional query string on one line nor pass an {% if %} into an
        # {% include %}. One string out of the view, one argument in.
        "my_stats_url": (
            reverse("tipping:my_stats", args=[org.id])
            + (f"?comp={selected_comp.slug}" if selected_comp is not None else "")
        ),
        "comp_options": comp_options, "selected_comp": selected_comp,
        "comp_keep": comp_keep, "comp_keep_pairs": comp_keep_pairs,
        "comp_keep_round": comp_keep_round,
        "round_nav": round_nav, "round_strip": strip,
        "ranked": ranked, "me": request.user,
        "scope": scope, "is_family": is_family,
        "total_tippers": len(ranked),
    })


def tipping_ladder_view(request):
    """The public Industry / National ladder (/tipping-ladder/).

    Not org-scoped and not login-gated — same footing as billing.good_list_view,
    which this sits beside: an organisation opts in once
    (`Organisation.is_public_listed`) and shows up on both public boards.

    "Industry" and "National" are not two views. They are the same board,
    filtered by organisation type or not — arriving with `?type=` pre-fills
    the type filter (how the app nav's "Industry ladder" link gets you
    straight to your own sector); arriving with none shows every opted-in
    organisation.
    """
    type_slug = request.GET.get("type", "")
    cat_slug = request.GET.get("cat", "")
    state_code = request.GET.get("state", "")

    organisation_types = list(OrganisationType.objects.prefetch_related("sub_categories"))
    states = list(State.objects.all())
    # Drop unknown / mismatched filter values rather than 404ing a public page.
    if type_slug and not any(gt.slug == type_slug for gt in organisation_types):
        type_slug = ""
    if cat_slug and (not type_slug or not any(
        sc.slug == cat_slug
        for gt in organisation_types if gt.slug == type_slug
        for sc in gt.sub_categories.all()
    )):
        cat_slug = ""
    if state_code and not any(s.code == state_code for s in states):
        state_code = ""

    from data_sync.public_board import board as team_board

    cfg = GoodListConfig.get()
    return render(request, "public/tipping_ladder.html", {
        "active": "ladder",
        # The real-competitions read-only board, same payload/JS as home.html
        # — the "images and videos... tables changing with stats, read only"
        # showcase the client asked the How It Works ladder section to reuse.
        "board_snapshot": team_board(),
        "board_live": national_ladder.board_is_live(),
        "board": national_ladder.ladder(
            organisation_type_slug=type_slug or None,
            sub_category_slug=cat_slug or None,
            state_code=state_code or None,
        ),
        "organisation_types": organisation_types,
        "states": states,
        "sel_type": type_slug,
        "sel_cat": cat_slug,
        "sel_state": state_code,
        "credibility_min": cfg.credibility_min_groups,
    })



# ---------------------------------------------------------------------------
# Season Champion Certificates (spec addendum, 14 Aug 2026). See
# tipping/certificates.py for the rules; these are the org-side screens.
# ---------------------------------------------------------------------------

def _can_certify(user, org) -> bool:
    """§5: the admin, captain or team manager — the org side, not the winner."""
    from orgs.models import OrgMember

    m = OrgMember.objects.filter(user=user, org=org).first()
    return bool(m and (m.can_manage or m.is_captain))


@login_required
def certificates_view(request, org_id: int):
    """The competition picker (client, 12 Oct 2026: "square, 2 in each row,
    have the colours of the competitions"): one card per competition this
    organisation runs, coloured in that competition's own accent, each
    leading to that competition's own page — never four stacked at once."""
    from django.http import HttpResponseForbidden

    from orgs.models import Organisation

    from . import certificates as certs

    org = get_object_or_404(Organisation, pk=org_id)
    if not _can_certify(request.user, org):
        return HttpResponseForbidden()
    comps = [
        {
            "series": series,
            "accent": certs.ACCENTS.get(series.name, certs.FOREST),
            "state": certs.season_state(org, series),
        }
        for series in certs.competitions_for(org)
    ]
    return render(request, "certificates.html", {
        "org": org,
        "comps": comps,
        "year": org.season.year if org.season_id else "",
    })


@login_required
def certificate_competition_view(request, org_id: int, series: str):
    """One competition, on its own page (client, 12 Oct 2026: "when I click
    in each that is when I should see a preview of the certificate in that
    specific competition — not showing all of them at once"). The real
    certificate shown full size once the season is final; a SAMPLE preview on
    request while it's still running; otherwise a pointer at the member's own
    season-so-far — the same three states the old combined list carried, now
    one at a time instead of stacked four deep.
    """
    from django.http import Http404, HttpResponseForbidden

    from catalog.models import Series
    from orgs.models import Organisation

    from . import certificates as certs

    org = get_object_or_404(Organisation, pk=org_id)
    if not _can_certify(request.user, org):
        return HttpResponseForbidden()
    if series not in certs.CERT_SERIES:
        raise Http404
    series_obj = get_object_or_404(Series, name=series)
    if series_obj not in certs.competitions_for(org):
        raise Http404
    state = certs.season_state(org, series_obj)
    champions = certs.placegetters(org, series_obj) if state.final else []
    _attach_photos(org, series_obj, champions)
    preview = request.GET.get("preview") == "1"
    sample = None
    if not champions and preview and not state.final:
        sample = certs.sample_certificate(org, series_obj, request.user)
        _attach_photos(org, series_obj, [sample])
    return render(request, "certificate_competition.html", {
        "org": org,
        "series": series_obj,
        "state": state,
        "champions": champions,
        "sample": sample,
        "preview": preview,
        "accent": certs.ACCENTS.get(series_obj.name, certs.FOREST),
        "year": org.season.year if org.season_id else "",
        "individual_national": certs.national_individual_champion(org, series_obj) if state.final else None,
    })


def _champion_photo_field(org, series, user_id):
    """The field behind a champion's certificate photo (client, 12 Oct 2026:
    baked into the certificate itself, not just shown beside it): the one
    chosen for this certificate if there is one, else their profile photo,
    else ``None`` so the certificate falls back to initials."""
    from accounts.models import User

    from .models import ChampionPhoto

    override = ChampionPhoto.objects.filter(org=org, series=series, user_id=user_id).first()
    if override:
        return override.image
    avatar = getattr(User.objects.filter(id=user_id).first(), "avatar", None)
    return avatar or None


def _attach_photos(org, series, champs):
    """Each champion's photo for the page: the one chosen for this certificate
    if there is one, else their profile photo, else nothing."""
    from accounts.models import User

    from .models import ChampionPhoto

    chosen = {p.user_id: p for p in ChampionPhoto.objects.filter(org=org, series=series)}
    users = {u.id: u for u in User.objects.filter(id__in=[c.user_id for c in champs])}
    for c in champs:
        override = chosen.get(c.user_id)
        avatar = getattr(users.get(c.user_id), "avatar", None)
        c.photo_url = override.image.url if override else (avatar.url if avatar else "")
        c.photo_is_custom = bool(override)


@login_required
@require_POST
def certificate_photo(request, org_id: int, series: str, user_id: int):
    """Set (or, with ``reset``, clear) the photo beside a champion's
    certificate. Squared and sized on the way in, so a phone photo of any
    shape lands as a clean circle and nobody uploads twelve megabytes."""
    from io import BytesIO

    from django.core.files.base import ContentFile
    from django.http import Http404
    from PIL import Image, ImageOps

    from catalog.models import Series
    from orgs.models import Organisation

    from . import certificates as certs
    from .models import ChampionPhoto

    org = get_object_or_404(Organisation, pk=org_id)
    if not _can_certify(request.user, org):
        return HttpResponseForbidden()
    series_obj = get_object_or_404(Series, name=series)
    if not any(c.user_id == user_id for c in certs.champions(org, series_obj)):
        raise Http404
    back = reverse("tipping:certificates", args=[org.id])
    if request.POST.get("reset") == "1":
        for p in ChampionPhoto.objects.filter(org=org, series=series_obj, user_id=user_id):
            p.image.delete(save=False)
            p.delete()
        messages.success(request, "Back to the profile photo.")
        return redirect(back)
    upload = request.FILES.get("photo")
    if not upload:
        messages.error(request, "Choose a photo to upload.")
        return redirect(back)
    if upload.size > 12 * 1024 * 1024:
        messages.error(request, "That photo is over 12 MB. Try a smaller one.")
        return redirect(back)
    try:
        img = Image.open(upload)
        img = ImageOps.exif_transpose(img).convert("RGB")
    except Exception:  # noqa: BLE001 — not an image Pillow can read
        messages.error(request, "That file isn't a photo we can read. Try a JPG or PNG.")
        return redirect(back)
    img = ImageOps.fit(img, (800, 800), Image.LANCZOS, centering=(0.5, 0.4))
    out = BytesIO()
    img.save(out, "JPEG", quality=88, optimize=True)
    photo, _ = ChampionPhoto.objects.get_or_create(
        org=org, series=series_obj, user_id=user_id, defaults={"uploaded_by": request.user},
    )
    if photo.image:
        photo.image.delete(save=False)
    photo.image.save(f"{org.id}-{series.lower()}-{user_id}.jpg", ContentFile(out.getvalue()), save=False)
    photo.uploaded_by = request.user
    photo.save()
    messages.success(request, "Photo updated.")
    return redirect(back)


@login_required
def certificate_sample_file(request, org_id: int, series: str, fmt: str):
    """A competition's SAMPLE certificate, as ``pdf`` or ``png``
    — the preview's downloads. Marked SAMPLE on its face and in its name."""
    from django.http import Http404, HttpResponse, HttpResponseForbidden

    from catalog.models import Series
    from orgs.models import Organisation

    from . import certificates as certs

    org = get_object_or_404(Organisation, pk=org_id)
    if not _can_certify(request.user, org):
        return HttpResponseForbidden()
    if series not in certs.CERT_SERIES or fmt not in ("pdf", "png"):
        raise Http404
    series_obj = get_object_or_404(Series, name=series)
    cert = certs.sample_certificate(org, series_obj, request.user)
    photo = _champion_photo_field(org, series_obj, cert.user_id)
    if fmt == "pdf":
        body, ctype, name = certs.render_pdf(cert, photo), "application/pdf", cert.filename("pdf")
    else:
        body, ctype, name = certs.render_png(cert, photo), "image/png", cert.filename("png")
    resp = HttpResponse(body, content_type=ctype)
    disposition = "inline" if request.GET.get("view") == "1" else "attachment"
    resp["Content-Disposition"] = f'{disposition}; filename="{name}"'
    resp["Cache-Control"] = "private, max-age=60"
    return resp


@login_required
def certificate_file(request, org_id: int, series: str, user_id: int, fmt: str):
    """One certificate, as ``pdf`` (print) or ``png`` (to post).

    Re-derived from the final board on every request rather than stored: a
    certificate can only ever say what the locked ladder says, and a
    correction to a result before the season closed cannot leave a stale file
    behind. ``?view=1`` serves it inline for the on-page preview.
    """
    from django.http import Http404, HttpResponse, HttpResponseForbidden

    from catalog.models import Series
    from orgs.models import Organisation

    from . import certificates as certs

    org = get_object_or_404(Organisation, pk=org_id)
    # Your own certificate is yours to download; the org side can fetch anyone's.
    if not _can_certify(request.user, org) and request.user.id != user_id:
        return HttpResponseForbidden()
    if series not in certs.CERT_SERIES or fmt not in ("pdf", "png"):
        raise Http404
    series_obj = get_object_or_404(Series, name=series)
    group = None
    raw_group = request.GET.get("group", "")
    if raw_group.isdigit():
        group = get_object_or_404(Group, pk=int(raw_group), org=org)
    cert = next((c for c in certs.placegetters(org, series_obj, group=group) if c.user_id == user_id), None)
    if cert is None:
        raise Http404("No certificate — the season is not final, or this member did not finish in the top three.")
    photo = _champion_photo_field(org, series_obj, user_id)
    if fmt == "pdf":
        body, ctype, name = certs.render_pdf(cert, photo), "application/pdf", cert.filename("pdf")
    else:
        body, ctype, name = certs.render_png(cert, photo), "image/png", cert.filename("png")
    resp = HttpResponse(body, content_type=ctype)
    disposition = "inline" if request.GET.get("view") == "1" else "attachment"
    resp["Content-Disposition"] = f'{disposition}; filename="{name}"'
    resp["Cache-Control"] = "private, max-age=300"
    return resp


@login_required
def certificate_scope_file(request, org_id: int, series: str, scope: str, fmt: str):
    """A top-three place across all of GoodTip (scope "individual").
    Re-derived fresh, like the per-member one — the moment the wider field
    stops being final, this stops existing too. ``?user=`` picks the person;
    without it, the national champion."""
    from django.http import Http404, HttpResponse, HttpResponseForbidden

    from accounts.models import User
    from catalog.models import Series
    from orgs.models import Organisation

    from . import certificates as certs

    org = get_object_or_404(Organisation, pk=org_id)
    if not _require_member(request.user, org):
        return HttpResponseForbidden()
    if series not in certs.CERT_SERIES or fmt not in ("pdf", "png") or scope != "individual":
        raise Http404
    series_obj = get_object_or_404(Series, name=series)
    top = certs.national_top_three(org, series_obj)
    raw_user = request.GET.get("user", "")
    cert = next((c for c in top if str(c.user_id) == raw_user), None) if raw_user.isdigit() else (top[0] if top else None)
    if cert is None:
        raise Http404("No certificate — that wider field isn't final, or this person isn't in its top three.")
    if not _can_certify(request.user, org) and request.user.id != cert.user_id:
        return HttpResponseForbidden()
    photo = getattr(User.objects.filter(id=cert.user_id).first(), "avatar", None)
    if fmt == "pdf":
        body, ctype, name = certs.render_pdf(cert, photo), "application/pdf", cert.filename("pdf")
    else:
        body, ctype, name = certs.render_png(cert, photo), "image/png", cert.filename("png")
    resp = HttpResponse(body, content_type=ctype)
    disposition = "inline" if request.GET.get("view") == "1" else "attachment"
    resp["Content-Disposition"] = f'{disposition}; filename="{name}"'
    resp["Cache-Control"] = "private, max-age=300"
    return resp
