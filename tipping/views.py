import logging
from types import SimpleNamespace

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from billing.donations import donation_summary
from orgs.models import Group, OrgMember, Organisation
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

    # ---- WHICH ROUNDS ARE ON SCREEN.
    #
    # REPORTED TWICE. First: "when I pick them I see the respective tips of,
    # let's say, NRL — but when I pick all competitions I only see AFLW." Then,
    # after a fix that matched on round NUMBER: "all are AFLW, why am I not
    # getting all, from AFL to NRL to NRLW?"
    #
    # The number was the wrong axis, and this organisation says why in one line:
    #
    #     AFL 1-27   NRL 1-27   AFLW 1-12   NRLW 1-11
    #
    # Four codes, four different lengths, all being played in the same week. AFL
    # is at round 26 while AFLW is at round 4 — so "round 4 in every code" means
    # this weekend's AFLW alongside an AFL round from four months ago, which
    # nobody tipped, which the "my tips means my tips" filter then dropped. One
    # code on screen again, for a new reason.
    #
    # THERE IS NO SHARED NUMBER. There is a shared position: the round each code
    # is UP TO. So the two modes are genuinely different questions and are
    # answered differently.
    #
    #   one competition   a round of that code. ?round=<id>, and the eight-wide
    #                     number stepper, because 26 and 25 mean something here.
    #   all competitions  the same position in every code — the current round of
    #                     each, then the one before that in each. ?step=<n>,
    #                     counted back from each code's own latest, because a
    #                     number stepper across four seasons of different
    #                     lengths is a control that cannot be right.
    by_series = {}
    for r in rounds:
        by_series.setdefault(r.series_id, []).append(r)   # already newest-first
    multi = len(by_series) > 1

    selected_round_id = request.GET.get("round")
    round_step = 0
    if multi:
        # WHERE STEP 0 IS, PER CODE. Not "the newest round that exists" — the
        # newest round YOU TIPPED, which is the same rule the single-code page
        # has always used to choose its opening round (_round_with_my_tips) and
        # for the same reason: this member's tips are at AFL 26 while the AFL
        # fixture list runs to 27, so anchoring on the last round in the table
        # opens on a round nobody has touched and reports no tips to somebody
        # who has plenty.
        #
        # Worked out once per code, then stepped within that code's own list.
        anchors = {}
        for sid, rs in by_series.items():
            pick = _round_with_my_tips(request.user, org, rs, _group(request, org))
            anchors[sid] = rs.index(pick) if pick in rs else 0
        # HOW MANY ROUNDS BACK, in every code at once. Clamped so no code runs
        # off the end of its own season: stepping past the start of AFLW would
        # drop it and leave the reader looking at three codes wondering where
        # the fourth went.
        raw = (request.GET.get("step") or "0").strip()
        round_step = int(raw) if raw.isdigit() else 0
        deepest = min(len(rs) - 1 - anchors[sid] for sid, rs in by_series.items())
        round_step = min(round_step, max(0, deepest))
        round_group = [rs[anchors[sid] + round_step] for sid, rs in by_series.items()]
        # The anchor is only for the things that need ONE round: the lock time
        # and the round-summary card. The one that locks last, because that is
        # the one still open to be tipped.
        selected_round = max(round_group, key=lambda r: r.lockout_at) if round_group else None
    else:
        if selected_round_id:
            try:
                selected_round = next(r for r in rounds if str(r.id) == selected_round_id)
            except StopIteration:
                selected_round = current_round(rounds)
        else:
            selected_round = _round_with_my_tips(request.user, org, rounds, _group(request, org))
        round_group = [selected_round] if selected_round is not None else []
    all_rows = []
    round_open, round_lock_note = True, ""
    if round_group:
        matches = list(
            Match.objects
            .filter(round__in=round_group)
            .select_related("home_team", "away_team", "round__series")
        )
        tips = {
            t.match_id: t
            for t in Tip.objects.filter(user=request.user, match__in=matches, org=org, group=_group(request, org))
        }
        # One batched read for the round rather than three queries per fixture.
        readers = _readers_for(matches)
        # THE WINDOW IS PER ROUND, and there can now be several on screen —
        # AFL's round 4 may be open while NRL's is still waiting on round 3.
        # So the verdict is looked up per fixture, and the banner above the
        # list reports the anchor's, which is the one the round picker names.
        windows = tip_window(org)
        states = {
            r.id: windows.get(r.id, {"open": True, "waits_for": None})
            for r in round_group
        }
        anchor_state = states.get(selected_round.id, {"open": True, "waits_for": None})
        # Open if ANY of them is: the composer is per fixture, and telling a
        # member the round is shut while three of its games still take a tip
        # would be the same bug in the other direction.
        round_open = any(st["open"] for st in states.values())
        if not anchor_state["open"] and anchor_state["waits_for"] is not None:
            round_lock_note = (
                f"Locked. You can tip round {selected_round.round_number} once "
                f"round {anchor_state['waits_for'].round_number} is over."
            )
        for m in matches:
            all_rows.append({
                "match": m,
                "tip": tips.get(m.id),
                # A pick can change until THIS match kicks off, and only while
                # its OWN round is inside the tipping window. The round's own
                # lockout is its first kickoff, so gating on it closed games
                # that had not started yet.
                "editable": not m.is_locked and states.get(m.round_id, {}).get("open", True),
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

    # ---- the round picker, in whichever of its two shapes applies.
    #
    # ONE CODE: an eight-wide strip of round NUMBERS — "have the numbers to be
    # up to like 8, the latest being the one that will be default, so I can see
    # the 25, 24, going downwards." Anchored on the newest round, and the chosen
    # one is always inside the window, so a link to round 3 in August opens with
    # round 3 on screen rather than leaving the reader to hunt for it.
    #
    # EVERY CODE: no numbers at all, because no number means the same thing in
    # all of them. Just how far back you are, in rounds, from what each code is
    # up to. Every fixture card names its own code and round, so nothing on
    # screen is ambiguous.
    #
    # `anchor_for` maps a number to the Round its link carries: the URL has
    # always been ?round=<id> and every notification ever sent uses it.
    tips_strip = None
    numbers_desc, anchor_for = [], {}
    prev_round = next_round = None
    prev_step = next_step = None
    if multi:
        prev_step = round_step + 1 if round_step < deepest else None
        next_step = round_step - 1 if round_step > 0 else None
    else:
        for r in rounds:
            if r.round_number not in anchor_for:
                anchor_for[r.round_number] = r
                numbers_desc.append(r.round_number)
        # round_strip walks its window backwards from the END of the list, so
        # it wants them ascending.
        numbers_asc = list(reversed(numbers_desc))
        selected_number = selected_round.round_number if selected_round else None
        tips_strip = round_strip(
            numbers_asc, selected_number, request.GET.get("back"),
            size=8, values={n: anchor_for[n].id for n in numbers_asc},
        )
        if selected_number is not None:
            at = numbers_desc.index(selected_number)
            prev_round = anchor_for[numbers_desc[at + 1]] if at + 1 < len(numbers_desc) else None
            next_round = anchor_for[numbers_desc[at - 1]] if at > 0 else None

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
        # Which shape the round picker is in, and what that shape needs.
        "multi_comp": multi,
        "round_step": round_step, "prev_step": prev_step, "next_step": next_step,
        "round_group": round_group,
        # What the round links must carry so stepping a round never silently
        # widens the page back to every competition.
        "tips_keep": "&".join(f"series={sl}" for sl in active_slugs),
        # Counted in numbers, not rows: with four codes on the page "of 80" was
        # four times the length of the season. Both are empty in
        # all-competitions mode, where there is no single number to count.
        "round_strip": tips_strip, "round_numbers": numbers_desc,
        "round_anchors": [anchor_for[n] for n in numbers_desc],
        "round_position": (
            len(numbers_desc) - numbers_desc.index(selected_round.round_number)
            if not multi and selected_round is not None else 0
        ),
        "round_total": len(numbers_desc),
        "total_matches": total_matches,
        "tips_this_round": tips_this_round,
        "tips_remaining": total_matches - tips_this_round,
        "round_pct": round(tips_this_round / total_matches * 100) if total_matches else 0,
        "rank": rank, "total_tippers": total_tippers, "percentile": percentile,
        "donation": donation_summary(org),
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
        "round_strip": round_strip(
            rounds_available, at_round, request.GET.get("back"),
        ),
        "at_round": at_round,
        # Stepping through rounds must not silently change which competition's
        # ladder is on screen.
        "ladder_keep": f"series={selected.slug}" if selected else "",
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
    return render(request, "my_stats.html", {
        "org": org,
        "comp_options": options,
        "selected_comp": selected,
        "stats": st,
        "rank": user_rank_in_org(request.user, org, group=group),
        # Four pictures, and each one answers a question the table cannot.
        "chart_accuracy": spark(
            [r["accuracy"] for r in st["run"]], floor=0, ceiling=100,
        ),
        "chart_points": bars(st["run"], key="points"),
        "chart_codes": bars(st["codes"], key="accuracy"),
        "chart_split": donut(st["right"], st["played"]),
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
         "tips_correct": u.tips_correct, "tips_total": u.tips_total,
         "is_tied": u.is_tied,
         "accuracy": round(u.tips_correct / u.tips_total * 100) if u.tips_total else None,
         "behind": top_points - u.points}
        for u in board
    ]
    if request.headers.get("HX-Request"):
        return render(request, "partials/leaderboard_table.html", {
            "ranked": ranked, "me": request.user, "selected_comp": selected_comp,
        })
    # ---- sidebar summary. "Perfect scores" counts tippers who got every
    # graded tip right in the view being shown, so it tracks the round filter.
    perfect = sum(
        1 for r in ranked if r["tips_total"] and r["tips_correct"] == r["tips_total"]
    )
    current_round = rounds.first()
    # What the competition filter must carry through, and what it must not.
    #
    # Scope: yes — "the national board, for the NRL" is a sentence, and losing
    # the scope every time somebody changed code would be maddening.
    #
    # Round: NO. A round id belongs to one competition, so carrying it into
    # another one names a round that comp does not have and empties the table.
    # Changing code resets to all rounds, which is the only honest default.
    comp_keep_pairs = [("scope", scope)] if (is_family and scope == "national") else []
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
        labels[str(r.id)] = f"Round {r.round_number}"
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
    return render(request, "leaderboard.html", {
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
        "perfect_scores": perfect,
        "total_rounds": rounds.count(),
        "current_round": current_round,
        "donation": donation_summary(org),
    })
