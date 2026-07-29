from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from billing.donations import donation_summary
from orgs.models import OrgMember, Organisation
from .models import Match, Round, Tip
from .services import (
    annotate_play_state, current_round, leaderboard_for_family,
    leaderboard_for_org, submit_tip, user_org_stats, user_rank_in_org,
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
        "org": org, "round": round_obj, "rows": rows, "locked": round_obj.is_locked,
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
        return render(request, "partials/tip_pick_cell.html", {
            "org": org, "match": match, "selection": selection,
            "editable": True, "saved": True,
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
    if round_obj.is_locked:
        messages.error(request, "This round is locked — tips are final.")
        return redirect(dest)
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
def my_tips_view(request, org_id: int):
    org = get_object_or_404(Organisation, pk=org_id)
    if not _require_member(request.user, org):
        return HttpResponseForbidden()
    # Annotated so current_round can pick the round in play without a query per
    # round. Kept newest-first: the prev/next sidebar below indexes into this
    # list and depends on that order.
    rounds = list(
        annotate_play_state(Round.objects.filter(org=org)).order_by("-round_number")
    )
    selected_round_id = request.GET.get("round")
    if selected_round_id:
        try:
            selected_round = next(r for r in rounds if str(r.id) == selected_round_id)
        except StopIteration:
            selected_round = current_round(rounds)
    else:
        selected_round = current_round(rounds)
    all_rows = []
    if selected_round:
        matches = selected_round.matches.select_related("home_team", "away_team").all()
        tips = {
            t.match_id: t
            for t in Tip.objects.filter(user=request.user, match__in=matches, org=org)
        }
        round_open = not selected_round.is_locked
        for m in matches:
            all_rows.append({
                "match": m,
                "tip": tips.get(m.id),
                # a pick can change until the round locks or the match kicks off
                "editable": round_open and not m.is_locked,
                # upcoming / live / complete — drives the state filter below
                "phase": m.phase,
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
