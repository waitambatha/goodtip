from django.contrib import messages
from django.contrib.auth import authenticate, login, logout, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import PasswordChangeForm
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from orgs.models import OrgMember, Organisation
from tipping.models import Round, Tip
from tipping.services import user_org_stats, user_rank_in_org

from .forms import LoginForm, ProfileForm, SignupForm


JOIN_SESSION_KEY = "pending_join_org_id"


def _consume_pending_join(request):
    org_id = request.session.pop(JOIN_SESSION_KEY, None)
    if not org_id:
        return
    try:
        org = Organisation.objects.get(pk=org_id)
    except Organisation.DoesNotExist:
        return
    OrgMember.objects.get_or_create(user=request.user, org=org, defaults={"role": "member"})
    messages.success(request, f"Joined {org.name}.")


def signup_view(request):
    if request.user.is_authenticated:
        return redirect("dashboard")
    if request.method == "POST":
        form = SignupForm(request.POST)
        if form.is_valid():
            user = form.save()
            user = authenticate(request, username=user.email, password=form.cleaned_data["password1"])
            if user is not None:
                login(request, user)
                _consume_pending_join(request)
                return redirect("dashboard")
    else:
        form = SignupForm()
    return render(request, "auth/signup.html", {"form": form})


def login_view(request):
    if request.user.is_authenticated:
        return redirect("dashboard")
    if request.method == "POST":
        form = LoginForm(request.POST)
        if form.is_valid():
            user = authenticate(
                request,
                username=form.cleaned_data["email"],
                password=form.cleaned_data["password"],
            )
            if user is not None:
                login(request, user)
                _consume_pending_join(request)
                next_url = request.GET.get("next") or reverse("dashboard")
                return HttpResponseRedirect(next_url)
            messages.error(request, "Invalid email or password.")
    else:
        form = LoginForm()
    return render(request, "auth/login.html", {"form": form})


def logout_view(request):
    logout(request)
    return redirect("landing")


@login_required
def dashboard_view(request):
    memberships = (
        OrgMember.objects.filter(user=request.user)
        .select_related("org")
        .order_by("org__name")
    )
    cards = []
    for m in memberships:
        org = m.org
        org_sports = org.sports.all()
        current_round = (
            Round.objects.filter(org=org, competition__sport__in=org_sports, lockout_at__gte=timezone.now())
            .order_by("lockout_at").first()
        )
        if current_round is None:
            current_round = (
                Round.objects.filter(org=org, competition__sport__in=org_sports)
                .order_by("-round_number").first()
            )
        stats = user_org_stats(request.user, org)
        rank = user_rank_in_org(request.user, org)
        tips_done = 0
        tips_total = 0
        if current_round:
            tips_total = current_round.matches.count()
            tips_done = Tip.objects.filter(user=request.user, match__round=current_round, org=org).count()
        cards.append({
            "org": org,
            "round": current_round,
            "tips_done": tips_done,
            "tips_total": tips_total,
            "points": stats["points"],
            "rank": rank,
        })
    return render(request, "dashboard.html", {"cards": cards})


@login_required
def dashboard_countdown_partial(request, org_id: int):
    org = get_object_or_404(Organisation, pk=org_id)
    current_round = (
        Round.objects.filter(org=org, competition__sport__in=org.sports.all(), lockout_at__gte=timezone.now())
        .order_by("lockout_at").first()
    )
    return render(request, "partials/countdown.html", {"round": current_round, "org": org})


@login_required
def profile_view(request):
    pwd_form = PasswordChangeForm(request.user)
    form = ProfileForm(instance=request.user)
    if request.method == "POST" and "display_name" in request.POST:
        form = ProfileForm(request.POST, instance=request.user)
        if form.is_valid():
            form.save()
            messages.success(request, "Profile updated.")
            return redirect("profile")
    elif request.method == "POST" and "old_password" in request.POST:
        pwd_form = PasswordChangeForm(request.user, request.POST)
        if pwd_form.is_valid():
            user = pwd_form.save()
            update_session_auth_hash(request, user)
            messages.success(request, "Password changed.")
            return redirect("profile")
    memberships = OrgMember.objects.filter(user=request.user).select_related("org")
    return render(request, "profile.html", {
        "form": form, "pwd_form": pwd_form, "memberships": memberships,
    })
