from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from orgs.models import OrgMember, Organisation
from .models import Match, Round, Tip
from .services import leaderboard_for_family, leaderboard_for_org, submit_tip, user_org_stats


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
    return render(request, "partials/tip_saved.html", {"match": match, "selection": selection})


@login_required
def my_tips_view(request, org_id: int):
    org = get_object_or_404(Organisation, pk=org_id)
    if not _require_member(request.user, org):
        return HttpResponseForbidden()
    rounds = list(
        Round.objects.filter(org=org).order_by("-round_number")
    )
    selected_round_id = request.GET.get("round")
    if selected_round_id:
        try:
            selected_round = next(r for r in rounds if str(r.id) == selected_round_id)
        except StopIteration:
            selected_round = rounds[0] if rounds else None
    else:
        selected_round = rounds[0] if rounds else None
    tip_rows = []
    if selected_round:
        matches = selected_round.matches.select_related("home_team", "away_team").all()
        tips = {
            t.match_id: t
            for t in Tip.objects.filter(user=request.user, match__in=matches, org=org)
        }
        for m in matches:
            tip_rows.append({"match": m, "tip": tips.get(m.id)})
    stats = user_org_stats(request.user, org)
    if request.headers.get("HX-Request"):
        return render(request, "partials/my_tips_round.html", {
            "org": org, "round": selected_round, "rows": tip_rows,
        })
    return render(request, "my_tips.html", {
        "org": org, "rounds": rounds, "selected_round": selected_round,
        "rows": tip_rows, "points": stats["points"],
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
    return render(request, "leaderboard.html", {
        "org": org, "rounds": rounds, "selected_round_id": selected_round_id or "all",
        "ranked": ranked, "me": request.user,
        "scope": scope, "is_family": is_family,
    })
