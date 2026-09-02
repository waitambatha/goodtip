import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from billing.donations import donation_summary
from orgs.models import Group, OrgMember, Organisation
from .models import LadderEntry, Match, Round, Tip
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
    return render(request, "components/_match_state.html", {"match": match})


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
        if selection not in ("home", "away"):
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
        if not key.startswith("match_") or value not in ("home", "away"):
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


def _round_with_my_tips(user, org, rounds, group):
    """Which round My Tips should open on when the URL does not say.

    THE ROUND IN PLAY IS THE RIGHT ANSWER RIGHT UP UNTIL IT IS EMPTY.

    `current_round` picks the round the competition is in, which is what you
    want the moment you have tipped it. But an organisation running four codes
    has four sets of rounds interleaved, and the dashboard's slate deliberately
    spans every round that still has unplayed games — so a member tips rounds
    25 and 26 of the AFL and NRL on Tuesday, opens My Tips, and lands on round
    4 of the AFLW because that is the round "in play". The page then tells them
    they have not tipped, which is the one thing it should never say to
    somebody who has: reported as "I made tips on masterclass but on going to
    my tips I did not find them".

    So: the round in play if there is anything of theirs in it, otherwise the
    most recent round they actually tipped. Only ever a DEFAULT — `?round=`
    still wins, and the navigator still reaches every round either way.

    One query, and only when the first choice comes up empty.
    """
    played = current_round(rounds)
    if not rounds:
        return played

    def _mine_in(rnd):
        if rnd is None:
            return False
        return Tip.objects.filter(
            user=user, org=org, group=group, match__round=rnd,
        ).exists()

    if _mine_in(played):
        return played

    # `rounds` is already newest-first and already narrowed by the competition
    # filter, so the first one carrying a tip of theirs is the latest one.
    tipped_ids = set(
        Tip.objects.filter(
            user=user, org=org, group=group, match__round__in=rounds,
        ).values_list("match__round_id", flat=True)
    )
    return next((r for r in rounds if r.id in tipped_ids), played)



@login_required
def my_tips_view(request, org_id: int):
    org = get_object_or_404(Organisation, pk=org_id)
    if not _require_member(request.user, org):
        return HttpResponseForbidden()
    # Annotated so current_round can pick the round in play without a query per
    # round. Kept newest-first: the prev/next sidebar below indexes into this
    # list and depends on that order.
    # ---- competition filter.
    # A Round belongs to exactly one Series, so filtering by competition here
    # means narrowing the ROUND LIST: an org tipping AFL and NRL has two sets
    # of rounds interleaved by number, and "round 3" is ambiguous until you
    # say which code. The filter therefore also scopes prev/next, so paging
    # through NRL rounds never lands on an AFL one.
    org_series, active_series = competition_filter(org, request.GET.getlist("series"))
    active_slugs = [s.slug for s in active_series if s in org_series]

    round_qs = Round.objects.filter(org=org)
    if active_series:
        round_qs = round_qs.filter(series__in=active_series)
    rounds = list(annotate_play_state(round_qs).order_by("-round_number"))
    selected_round_id = request.GET.get("round")
    if selected_round_id:
        try:
            selected_round = next(r for r in rounds if str(r.id) == selected_round_id)
        except StopIteration:
            selected_round = current_round(rounds)
    else:
        selected_round = _round_with_my_tips(request.user, org, rounds, _group(request, org))
    all_rows = []
    round_open, round_lock_note = True, ""
    if selected_round:
        matches = list(
            selected_round.matches
            .select_related("home_team", "away_team", "round__series")
        )
        tips = {
            t.match_id: t
            for t in Tip.objects.filter(user=request.user, match__in=matches, org=org, group=_group(request, org))
        }
        # One batched read for the round rather than three queries per fixture.
        readers = _readers_for(matches)
        # This screen shows a single round, so the window verdict is the same
        # for every fixture on it — resolved once and reported once, above the
        # list, rather than repeated on every card.
        state = tip_window(org).get(selected_round.id, {"open": True, "waits_for": None})
        round_open = state["open"]
        if not round_open and state["waits_for"] is not None:
            round_lock_note = (
                f"Locked. You can tip round {selected_round.round_number} once "
                f"round {state['waits_for'].round_number} is over."
            )
        for m in matches:
            all_rows.append({
                "match": m,
                "tip": tips.get(m.id),
                # A pick can change until THIS match kicks off, and only while
                # its round is inside the tipping window. The round's own
                # lockout is its first kickoff, so gating on it closed games
                # that had not started yet.
                "editable": not m.is_locked and round_open,
                # upcoming / live / complete — drives the state filter below
                "phase": m.phase,
                # MatchReader's read. None whenever there is no fitted model
                # for this series or too little form behind either side, and
                # the template simply shows nothing rather than a hedge.
                "reader": readers.get(m.id),
            })

    # MY TIPS MEANS MY TIPS.
    #
    # This screen listed every fixture in the round and left the ones you had
    # not picked sitting there blank. On a nine-game round where somebody
    # tipped three, six of the nine rows were empty — so the page answering
    # "how am I going" was two-thirds filled with matches that had nothing to
    # do with them. The dashboard is where you tip; this is where you review
    # what you tipped.
    #
    # A row survives if there is a Tip on it, whoever made it: an auto-assigned
    # pick still scores, so hiding it would hide points from the very screen
    # that accounts for them. The card marks which ones were not yours.
    #
    # ONE EXCEPTION: a match being played right now stays, tipped or not.
    #
    # Everywhere else an untipped fixture is noise — nothing to do about an
    # upcoming game you have not picked except go to the dashboard, and nothing
    # at all about a finished one. In play is different: it is the only state
    # where an untipped match still tells you something you want to know, which
    # is that it is happening and you have no stake in it. Hiding those would
    # mean a member watching the round sees three of the six games on and no
    # sign the others exist.
    #
    # Captured BEFORE the filter: the sidebar reports progress as "3 of 9
    # tipped", which needs the round's real size. Measuring it after would make
    # it read "3 of 3" — permanently complete, however much was missed.
    round_match_count = len(all_rows)
    all_rows = [
        r for r in all_rows
        if r["tip"] is not None or r["phase"] == "live"
    ]
    for r in all_rows:
        # Drives the "you didn't tip this one" treatment on the card.
        r["missed_live"] = r["tip"] is None and r["phase"] == "live"

    # ---- state filter. Counts are always over the whole round, so the tab
    # labels don't change as you move between them.
    #
    # Ordered upcoming → live → complete: still to play is what you can still
    # act on, in progress is what you are watching, and finished is the record.
    # That is the order of decreasing urgency, and the order people read in.
    STATES = ("upcoming", "live", "complete")
    phase_counts = {s: sum(1 for r in all_rows if r["phase"] == s) for s in STATES}
    state = request.GET.get("state", "all")
    if state not in STATES:
        state = "all"
    tip_rows = all_rows if state == "all" else [r for r in all_rows if r["phase"] == state]
    if state == "all":
        order = {s: i for i, s in enumerate(STATES)}
        # Inside the in-play block, the ones you have NOT tipped come first.
        # They are the only rows on this page carrying anything to act on —
        # the round is running and you are not in these — so they lead, and
        # the games you did pick follow underneath.
        tip_rows = sorted(
            tip_rows,
            key=lambda r: (
                order.get(r["phase"], 99),
                0 if r.get("missed_live") else 1,
                r["match"].kickoff_at,
                r["match"].id,
            ),
        )

    stats = user_org_stats(request.user, org, group=_group(request, org))
    if request.headers.get("HX-Request"):
        return render(request, "partials/my_tips_round.html", {
            "org": org, "round": selected_round, "rows": tip_rows,
            "state": state, "phase_counts": phase_counts, "total_all": len(all_rows),
            "org_series": org_series, "active_slugs": active_slugs,
            "round_open": round_open, "round_lock_note": round_lock_note,
        })

    # ---- sidebar: where this member sits, and what's left to do this round.
    # `rounds` is newest-first, so "previous round" is the next item along.
    prev_round = next_round = None
    if selected_round:
        idx = rounds.index(selected_round)
        prev_round = rounds[idx + 1] if idx + 1 < len(rounds) else None
        next_round = rounds[idx - 1] if idx > 0 else None

    # Round-level stats describe the whole round, not the filtered view.
    total_matches = round_match_count
    tips_this_round = len(all_rows)
    total_tippers = len(leaderboard_for_org(org, group=_group(request, org)))
    rank = user_rank_in_org(request.user, org, group=_group(request, org))
    # Percentile = share of the field this member is ahead of or level with.
    percentile = None
    if rank and total_tippers:
        percentile = round((total_tippers - rank + 1) / total_tippers * 100)

    return render(request, "my_tips.html", {
        "org": org, "rounds": rounds, "selected_round": selected_round,
        "rows": tip_rows, "points": stats["points"],
        # Competition filter: the org's series, and which are selected.
        "org_series": org_series, "active_slugs": active_slugs,
        # Tipping window: whether this round is open, and why not.
        "round_open": round_open, "round_lock_note": round_lock_note,
        "state": state, "phase_counts": phase_counts, "total_all": len(all_rows),
        "prev_round": prev_round, "next_round": next_round,
        "round_position": len(rounds) - rounds.index(selected_round) if selected_round else 0,
        "round_total": len(rounds),
        "total_matches": total_matches,
        "tips_this_round": tips_this_round,
        "tips_remaining": total_matches - tips_this_round,
        "round_pct": round(tips_this_round / total_matches * 100) if total_matches else 0,
        "rank": rank, "total_tippers": total_tippers, "percentile": percentile,
        "donation": donation_summary(org),
    })


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

    series_list = [
        s
        for comp in org.competitions.select_related().prefetch_related("series")
        for s in comp.series.all()
    ]
    # De-duplicate while keeping the org's own ordering.
    seen, ordered = set(), []
    for s in series_list:
        if s.id not in seen:
            seen.add(s.id)
            ordered.append(s)

    wanted = request.GET.get("series")
    selected = next((s for s in ordered if str(s.id) == wanted), None) or (ordered[0] if ordered else None)

    entries = []
    if selected:
        entries = list(
            LadderEntry.objects.filter(series=selected, season=org.season)
            .select_related("team")
            .order_by("rank")
        )

    # Which teams this member has tipped most, so the ladder connects to their
    # own season rather than being a bare table.
    my_team_ids = set(
        Tip.objects.filter(user=request.user, org=org, group=_group(request, org), is_correct=True)
        .values_list("match__home_team_id", flat=True)
    )

    return render(request, "ladder.html", {
        "org": org,
        "series_options": ordered,
        "selected_series": selected,
        "entries": entries,
        "my_team_ids": my_team_ids,
        "updated_at": entries[0].updated_at if entries else None,
    })


@login_required
def leaderboard_view(request, org_id: int):
    org = get_object_or_404(Organisation, pk=org_id)
    if not _require_member(request.user, org):
        return HttpResponseForbidden()
    rounds = Round.objects.filter(org=org).order_by("-round_number")
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
        board = leaderboard_for_family(org, round_id=round_filter)
    else:
        scope = "local"
        board = leaderboard_for_org(org, round_id=round_filter, group=group)
    # Ranks come from the board, which resolved them under the addendum's
    # tiebreakers. Recomputing "equal points means equal rank" here would
    # undo that: two tippers separated on the paired comp would be reported
    # level again, and the leaderboard would disagree with the dashboard.
    ranked = [
        {"rank": u.rank, "user": u, "points": u.points,
         "tips_correct": u.tips_correct, "tips_total": u.tips_total,
         "is_tied": u.is_tied}
        for u in board
    ]
    if request.headers.get("HX-Request"):
        return render(request, "partials/leaderboard_table.html", {
            "ranked": ranked, "me": request.user,
        })
    # ---- sidebar summary. "Perfect scores" counts tippers who got every
    # graded tip right in the view being shown, so it tracks the round filter.
    perfect = sum(
        1 for r in ranked if r["tips_total"] and r["tips_correct"] == r["tips_total"]
    )
    current_round = rounds.first()
    return render(request, "leaderboard.html", {
        "org": org, "rounds": rounds, "selected_round_id": selected_round_id or "all",
        "ranked": ranked, "me": request.user,
        "scope": scope, "is_family": is_family,
        "total_tippers": len(ranked),
        "perfect_scores": perfect,
        "total_rounds": rounds.count(),
        "current_round": current_round,
        "donation": donation_summary(org),
    })
