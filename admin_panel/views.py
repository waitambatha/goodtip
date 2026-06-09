from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from catalog.models import Competition, Season, Sport
from data_sync.services import get_sync_service, SyncError
from orgs.models import OrgMember, Organisation
from orgs.signing import make_join_token
from tipping.models import Match, Round, Team, Tip
from tipping.services import record_match_result


# Map the org-creation form's sport value to one or more catalog Sport names.
SPORT_FORM_MAP = {"AFL": ["AFL"], "NRL": ["NRL"], "BOTH": ["AFL", "NRL"]}


@staff_member_required
def overview(request):
    org_count = Organisation.objects.count()
    round_count = Round.objects.count()
    match_count = Match.objects.count()
    tip_count = Tip.objects.count()
    recent_orgs = Organisation.objects.order_by("-created_at")[:5]
    return render(request, "manage/overview.html", {
        "org_count": org_count, "round_count": round_count,
        "match_count": match_count, "tip_count": tip_count,
        "recent_orgs": recent_orgs,
    })


@staff_member_required
def orgs_list(request):
    if request.method == "POST":
        season, _ = Season.objects.get_or_create(
            year=int(request.POST["season"]),
            defaults={"label": request.POST["season"].strip()},
        )
        org = Organisation.objects.create(
            name=request.POST["name"].strip(),
            season=season,
            charity_name=request.POST["charity_name"].strip(),
            charity_url=request.POST.get("charity_url", "").strip(),
        )
        sport_names = SPORT_FORM_MAP.get(request.POST["sport"], [])
        org.sports.set(Sport.objects.filter(name__in=sport_names))
        messages.success(request, "Org created.")
        return redirect("manage:orgs_list")
    orgs = Organisation.objects.order_by("-created_at")
    return render(request, "manage/orgs_list.html", {"orgs": orgs})


@staff_member_required
def org_rounds(request, org_id: int):
    org = get_object_or_404(Organisation, pk=org_id)
    if request.method == "POST":
        action = request.POST.get("action", "create")
        if action == "create":
            Round.objects.create(
                org=org,
                round_number=int(request.POST["round_number"]),
                competition=get_object_or_404(Competition, pk=int(request.POST["competition"])),
                lockout_at=request.POST["lockout_at"],
                status=request.POST.get("status", "upcoming"),
            )
            messages.success(request, "Round created.")
        elif action == "status":
            r = get_object_or_404(Round, pk=int(request.POST["round_id"]), org=org)
            r.status = request.POST["status"]
            r.save(update_fields=["status"])
            messages.success(request, "Status updated.")
        return redirect("manage:org_rounds", org_id=org.id)
    rounds = Round.objects.filter(org=org).order_by("-round_number")
    return render(request, "manage/org_rounds.html", {"org": org, "rounds": rounds})


@staff_member_required
def round_matches(request, org_id: int, round_id: int):
    org = get_object_or_404(Organisation, pk=org_id)
    round_obj = get_object_or_404(Round, pk=round_id, org=org)
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "create":
            home = get_object_or_404(Team, pk=int(request.POST["home_team"]))
            away = get_object_or_404(Team, pk=int(request.POST["away_team"]))
            Match.objects.create(
                round=round_obj, home_team=home, away_team=away,
                kickoff_at=request.POST["kickoff_at"],
                venue=request.POST.get("venue", "").strip(),
            )
            messages.success(request, "Match created.")
        elif action == "result":
            match = get_object_or_404(Match, pk=int(request.POST["match_id"]), round=round_obj)
            try:
                hs = int(request.POST["home_score"])
                as_ = int(request.POST["away_score"])
            except (KeyError, ValueError):
                messages.error(request, "Invalid scores.")
                return redirect("manage:round_matches", org_id=org.id, round_id=round_obj.id)
            n = record_match_result(match, hs, as_)
            messages.success(request, f"Result saved. {n} tip(s) graded.")
        return redirect("manage:round_matches", org_id=org.id, round_id=round_obj.id)
    matches = round_obj.matches.select_related("home_team", "away_team").order_by("kickoff_at")
    # Teams from any competition under the same sport (e.g. an AFL round can draw on AFL + AFLW).
    teams = Team.objects.filter(competition__sport=round_obj.competition.sport).select_related("competition")
    return render(request, "manage/round_matches.html", {
        "org": org, "round": round_obj, "matches": matches, "teams": teams,
    })


@staff_member_required
def org_members(request, org_id: int):
    org = get_object_or_404(Organisation, pk=org_id)
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "remove":
            OrgMember.objects.filter(org=org, id=int(request.POST["member_id"])).delete()
            messages.success(request, "Member removed.")
        elif action == "promote":
            m = OrgMember.objects.filter(org=org, id=int(request.POST["member_id"])).first()
            if m:
                m.role = "admin" if m.role == "member" else "member"
                m.save(update_fields=["role"])
        return redirect("manage:org_members", org_id=org.id)
    members = OrgMember.objects.filter(org=org).select_related("user").order_by("joined_at")
    token = make_join_token(org.id)
    join_url = request.build_absolute_uri(reverse("join_org", args=[org.id, token]))
    return render(request, "manage/org_members.html", {
        "org": org, "members": members, "join_url": join_url,
    })


@staff_member_required
def sync_panel(request):
    msg = None
    if request.method == "POST":
        comp = request.POST["competition"]
        round_number = int(request.POST["round_number"])
        org = get_object_or_404(Organisation, pk=int(request.POST["org_id"]))
        kind = request.POST.get("kind", "fixtures")
        try:
            svc = get_sync_service(comp)
            if kind == "fixtures":
                n = svc.sync_fixtures(competition=comp, round_number=round_number, org=org)
                msg = f"Synced {n} fixtures."
            else:
                n = svc.sync_results(competition=comp, round_number=round_number, org=org)
                msg = f"Wrote {n} results."
            messages.success(request, msg)
        except SyncError as e:
            messages.error(request, str(e))
        return redirect("manage:sync")
    orgs = Organisation.objects.all()
    return render(request, "manage/sync.html", {"orgs": orgs})
