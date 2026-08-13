import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from billing.donations import donation_summary
from orgs.models import OrgMember, Organisation
from .models import LadderEntry, Match, Round, Tip
from .services import (
    annotate_play_state, competition_filter, current_round,
    leaderboard_for_family, leaderboard_for_org, submit_tip, tip_window,
    user_org_stats, user_rank_in_org,
)


def _require_member(user, org):
    return OrgMember.objects.filter(user=user, org=org).exists()


@login_required
def tip_round_view(request, org_id: int, round_id: int):
    org = get_object_or_404(Organisation, pk=org_id)
    if not _require_member(request.user, org):
        return HttpResponseForbidden()
    round_obj = get_object_or_404(Round, pk=round_id, org=org)
    matches = list(round_obj.matches.select_related("home_team", "away_team").order_by("kickoff_at"))
    existing_tips = {
        t.match_id: t.selection
        for t in Tip.objects.filter(user=request.user, match__in=matches, org=org)
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
    try:
        submit_tip(user=request.user, match=match, org=org, selection=selection)
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
    return render(request, "partials/tip_saved.html", {"match": match, "selection": selection})


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
    saved, skipped = 0, 0
    for match in round_obj.matches.all():
        selection = request.POST.get(f"match_{match.id}")
        if selection not in ("home", "away"):
            continue
        try:
            submit_tip(user=request.user, match=match, org=org, selection=selection)
            saved += 1
        except ValueError:
            skipped += 1
    if saved:
        note = f"{saved} tip{'s' if saved != 1 else ''} confirmed — find them under My Tips. You can change any pick until the round locks."
        if skipped:
            note += f" ({skipped} match{'es' if skipped != 1 else ''} had already kicked off and couldn't be changed.)"
        messages.success(request, note)
    elif skipped:
        messages.error(request, "Those matches have already kicked off — tips there are final.")
    else:
        messages.info(request, "Tap a team on each match to pick it, then press Confirm my tips.")
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

    ids = []
    for key, value in request.POST.items():
        if not key.startswith("match_") or value not in ("home", "away"):
            continue
        raw = key[len("match_"):]
        if raw.isdigit():
            ids.append(int(raw))
    matches = {
        m.id: m
        for m in Match.objects.filter(pk__in=ids, round__org=org).select_related("round")
    }

    saved, skipped = 0, 0
    for mid in ids:
        match = matches.get(mid)
        if match is None:
            continue  # not this org's match — ignore rather than error
        try:
            submit_tip(user=request.user, match=match, org=org,
                       selection=request.POST[f"match_{mid}"])
            saved += 1
        except ValueError:
            skipped += 1

    if saved:
        note = (f"{saved} tip{'s' if saved != 1 else ''} confirmed — find them under "
                "My Tips. You can change any pick until that game kicks off.")
        if skipped:
            note += (f" ({skipped} match{'es' if skipped != 1 else ''} had already "
                     "started and couldn't be changed.)")
        messages.success(request, note)
    elif skipped:
        messages.error(request, "Those matches have already kicked off — tips there are final.")
    else:
        messages.info(request, "Tap a team on each match to pick it, then press Confirm my tips.")
    return redirect(f"{reverse('dashboard')}?org={org.id}")


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
        selected_round = current_round(rounds)
    all_rows = []
    round_open, round_lock_note = True, ""
    if selected_round:
        matches = list(
            selected_round.matches
            .select_related("home_team", "away_team", "round__series")
        )
        tips = {
            t.match_id: t
            for t in Tip.objects.filter(user=request.user, match__in=matches, org=org)
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

    # ---- state filter. Counts are always over the whole round, so the tab
    # labels don't change as you move between them.
    STATES = ("upcoming", "live", "complete")
    phase_counts = {s: sum(1 for r in all_rows if r["phase"] == s) for s in STATES}
    state = request.GET.get("state", "all")
    if state not in STATES:
        state = "all"
    tip_rows = all_rows if state == "all" else [r for r in all_rows if r["phase"] == state]

    stats = user_org_stats(request.user, org)
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
    total_matches = len(all_rows)
    tips_this_round = sum(1 for r in all_rows if r["tip"])
    board = list(leaderboard_for_org(org).values("id", "points"))
    total_tippers = len(board)
    rank = user_rank_in_org(request.user, org)
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
        Tip.objects.filter(user=request.user, org=org, is_correct=True)
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
    if scope == "national" and is_family:
        board = leaderboard_for_family(org, round_id=round_filter)
    else:
        scope = "local"
        board = leaderboard_for_org(org, round_id=round_filter)
    ranked = []
    last_points = None
    rank = 0
    real_rank = 0
    for u in board:
        real_rank += 1
        if u.points != last_points:
            rank = real_rank
            last_points = u.points
        ranked.append({"rank": rank, "user": u, "points": u.points,
                       "tips_correct": u.tips_correct, "tips_total": u.tips_total})
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
