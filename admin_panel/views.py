from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from catalog.models import Charity, Competition, Season, Series
from data_sync.services import get_sync_service, SyncError
from orgs.forms import _unique_charity_slug
from orgs.models import OrgMember, Organisation
from orgs.signing import make_join_token
from tipping.models import Match, Round, Team, Tip
from tipping.services import record_match_result


# Map the org-creation form's value to one or more Competition slugs.
COMP_FORM_MAP = {"AFL": ["afl"], "NRL": ["nrl"], "BOTH": ["afl", "nrl"]}


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
        charity_name = request.POST["charity_name"].strip()
        charity = Charity.objects.filter(name__iexact=charity_name).first()
        if charity is None:
            charity = Charity.objects.create(
                name=charity_name,
                slug=_unique_charity_slug(charity_name),
                website=request.POST.get("charity_url", "").strip(),
                is_approved=True,
            )
        org = Organisation.objects.create(
            name=request.POST["name"].strip(),
            season=season,
            charity=charity,
        )
        comp_slugs = COMP_FORM_MAP.get(request.POST["sport"], [])
        org.competitions.set(Competition.objects.filter(slug__in=comp_slugs, season=season))
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
            series = get_object_or_404(Series, pk=int(request.POST["series"]))
            Round.objects.create(
                org=org,
                round_number=int(request.POST["round_number"]),
                series=series,
                competition=Competition.for_series(series, org.season),
                stage=request.POST.get("stage", Round.STAGE_REGULAR),
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
    # Teams from any series under the same sport (e.g. an AFL round can draw on AFL + AFLW).
    teams = Team.objects.filter(series__sport=round_obj.series.sport).select_related("series")
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
                # Toggle between Participant and Manager+Captain.
                m.role = OrgMember.ROLE_PARTICIPANT if m.is_manager else OrgMember.ROLE_BOTH
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


# ---------------------------------------------------------------------------
# News & blog — super admin only (the platform owner, not group admins and
# not ordinary staff). Posts feed the member dashboard.
# ---------------------------------------------------------------------------
from django.contrib.auth.decorators import user_passes_test  # noqa: E402

from .models import NewsPost  # noqa: E402

superuser_required = user_passes_test(
    lambda u: u.is_active and u.is_superuser, login_url="/admin/login/"
)


def _apply_news_form(request, post: NewsPost) -> NewsPost:
    post.title = request.POST["title"].strip()
    post.tag = request.POST.get("tag", "NEWS")
    post.excerpt = request.POST.get("excerpt", "").strip()
    post.body = request.POST.get("body", "").strip()
    post.link_url = request.POST.get("link_url", "").strip()
    post.is_published = bool(request.POST.get("is_published"))
    if request.FILES.get("image"):
        post.image = request.FILES["image"]
    post.save()
    return post


@superuser_required
def news_list(request):
    if request.method == "POST":
        post = NewsPost(created_by=request.user, published_at=timezone.now())
        _apply_news_form(request, post)
        messages.success(request, "Post published." if post.is_published else "Post saved as a draft.")
        return redirect("manage:news")
    posts = NewsPost.objects.all()
    return render(request, "manage/news.html", {
        "posts": posts, "tag_choices": NewsPost.TAG_CHOICES,
    })


@superuser_required
def news_edit(request, post_id: int):
    post = get_object_or_404(NewsPost, pk=post_id)
    if request.method == "POST":
        _apply_news_form(request, post)
        messages.success(request, "Post updated.")
        return redirect("manage:news")
    return render(request, "manage/news_edit.html", {
        "post": post, "tag_choices": NewsPost.TAG_CHOICES,
    })


@superuser_required
def news_toggle(request, post_id: int):
    post = get_object_or_404(NewsPost, pk=post_id)
    if request.method == "POST":
        post.is_published = not post.is_published
        post.save(update_fields=["is_published"])
        messages.success(request, "Post published." if post.is_published else "Post unpublished.")
    return redirect("manage:news")


@superuser_required
def news_delete(request, post_id: int):
    post = get_object_or_404(NewsPost, pk=post_id)
    if request.method == "POST":
        post.delete()
        messages.success(request, "Post deleted.")
    return redirect("manage:news")


# ---- member-facing news pages (any logged-in user) ------------------------
from django.contrib.auth.decorators import login_required  # noqa: E402


@login_required
def news_index(request):
    """All published posts — the "more news" destination."""
    posts = NewsPost.objects.filter(is_published=True)
    return render(request, "news_index.html", {"posts": posts})


@login_required
def news_detail(request, post_id: int):
    """Full story page a dashboard card clicks through to."""
    post = get_object_or_404(NewsPost, pk=post_id, is_published=True)
    more = NewsPost.objects.filter(is_published=True).exclude(pk=post.pk)[:5]
    return render(request, "news_detail.html", {"post": post, "more": more})
