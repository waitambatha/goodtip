import logging

from django.contrib import messages
from django.contrib.auth import authenticate, login, logout, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import PasswordChangeForm
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import ensure_csrf_cookie

from orgs.models import OrgMember, Organisation
from tipping.models import Round, Tip
from tipping.services import user_org_stats, user_rank_in_org

from .forms import AvatarForm, LoginForm, ProfileForm, SignupForm

logger = logging.getLogger(__name__)


JOIN_SESSION_KEY = "pending_join_org_id"
JOIN_INVITER_SESSION_KEY = "pending_join_inviter_id"


def _consume_pending_join(request):
    """Complete a pending invite join. Returns the joined Organisation, or None."""
    org_id = request.session.pop(JOIN_SESSION_KEY, None)
    inviter_id = request.session.pop(JOIN_INVITER_SESSION_KEY, None)
    if not org_id:
        return None
    try:
        org = Organisation.objects.get(pk=org_id)
    except Organisation.DoesNotExist:
        return None
    from orgs.services import add_member

    add_member(request.user, org, inviter_id=inviter_id)
    messages.success(request, f"Joined {org.name}.")
    return org


def post_join_redirect(org):
    """After joining, show the one-time optional top-up prompt if a pledge exists."""
    if org is not None and org.pledges.filter(season=org.season).exists():
        return redirect("billing:topup", org_id=org.id)
    return redirect("dashboard")


@never_cache
@ensure_csrf_cookie
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
                joined = _consume_pending_join(request)
                # Park the real destination and detour via the one-time
                # "add a profile photo?" step — skippable, never repeated.
                next_url = request.GET.get("next") or request.POST.get("next") or ""
                if not next_url.startswith("/"):
                    next_url = ""
                request.session[POST_SIGNUP_NEXT_KEY] = (
                    next_url or post_join_redirect(joined).url
                )
                return redirect("accounts:welcome_photo")
    else:
        form = SignupForm()
    return render(request, "auth/signup.html", {"form": form})


POST_SIGNUP_NEXT_KEY = "post_signup_next"


@never_cache
@login_required
def welcome_photo_view(request):
    """One-time step right after signup: add a profile photo, or skip.

    The face next to your name is what makes the Wall and the ladder feel
    like your group — but nobody's forced. Skip keeps the initials avatar.
    """
    next_url = request.session.get(POST_SIGNUP_NEXT_KEY) or reverse("dashboard")
    if request.method == "POST":
        if "skip" not in request.POST and request.FILES.get("avatar"):
            avatar_form = AvatarForm(request.POST, request.FILES, instance=request.user)
            if avatar_form.is_valid():
                avatar_form.save()
                messages.success(request, "Looking good — photo saved.")
            else:
                return render(request, "auth/welcome_photo.html", {
                    "error": avatar_form.errors["avatar"].as_text().lstrip("* "),
                })
        request.session.pop(POST_SIGNUP_NEXT_KEY, None)
        return HttpResponseRedirect(next_url)
    return render(request, "auth/welcome_photo.html", {})


@never_cache
@ensure_csrf_cookie
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
                joined = _consume_pending_join(request)
                if joined is not None:
                    return post_join_redirect(joined)
                next_url = request.GET.get("next") or reverse("dashboard")
                return HttpResponseRedirect(next_url)
            messages.error(request, "Invalid email or password.")
    else:
        form = LoginForm()
    return render(request, "auth/login.html", {"form": form})


def logout_view(request):
    logout(request)
    return redirect("landing")


def _dashboard_prompts(user, card, games):
    """The rotating line under "Welcome back" — what this member should know
    or do right now, most urgent first.

    Each entry is {icon, text, cta, url}. Everything is derived from state we
    already loaded for the card, so this costs no extra queries beyond the
    Wall count.
    """
    if not card:
        return []
    org = card["org"]
    rnd = card["round"]
    prompts = []

    # 1. An open charity vote they haven't cast — the most time-boxed thing
    #    on the dashboard.
    if card["charity_vote"] and not card["has_voted"]:
        prompts.append({
            "icon": "ic-vote",
            "text": f"A charity vote is open for {org.name} — you haven't had your say yet.",
            "cta": "Vote now",
            "url": reverse("orgs:charity_vote", args=[org.id]),
        })
    elif card["charity_vote"]:
        prompts.append({
            "icon": "ic-vote",
            "text": f"Your vote's in. {org.name} is still deciding where this season's money goes.",
            "cta": "See the runners",
            "url": reverse("orgs:charity_vote", args=[org.id]),
        })

    # 2. Tips outstanding, with the deadline attached.
    left = (card["tips_total"] or 0) - (card["tips_done"] or 0)
    if rnd and left > 0:
        when = ""
        if rnd.lockout_at and rnd.lockout_at >= timezone.now():
            when = f" Tips lock {timezone.localtime(rnd.lockout_at).strftime('%a %-d %b, %-I:%M%p').replace('AM', 'am').replace('PM', 'pm')}."
        prompts.append({
            "icon": "ic-clock",
            "text": f"{left} match{'es' if left != 1 else ''} still untipped in Round {rnd.round_number}.{when}",
            "cta": "Tip now",
            "url": reverse("tipping:my_tips", args=[org.id]),
        })
    elif rnd and card["tips_total"]:
        prompts.append({
            "icon": "ic-check",
            "text": f"All {card['tips_total']} tips are in for Round {rnd.round_number}. Now we wait.",
            "cta": "Review your picks",
            "url": reverse("tipping:my_tips", args=[org.id]),
        })

    # 3. The next game on the board — named, so it reads like a question.
    now = timezone.now()
    nxt = next((g for g in games if g.kickoff_at and g.kickoff_at >= now), None)
    if nxt:
        kick = timezone.localtime(nxt.kickoff_at).strftime("%a %-I:%M%p").replace("AM", "am").replace("PM", "pm")
        if nxt.tipped:
            backing = nxt.home_team.name if nxt.my_tip == "home" else nxt.away_team.name
            text = f"{nxt.home_team.name} v {nxt.away_team.name}, {kick} — you're backing {backing}."
        else:
            text = f"{nxt.home_team.name} v {nxt.away_team.name} kicks off {kick}. Who are you taking?"
        prompts.append({
            "icon": "ic-match",
            "text": text,
            "cta": "Make the call",
            "url": reverse("tipping:my_tips", args=[org.id]),
        })

    # 4. Where they sit.
    if card["rank"]:
        prompts.append({
            "icon": "ic-trophy",
            "text": f"You're {card['rank']}{_ordinal_suffix(card['rank'])} on the {org.name} ladder on {card['points']} points.",
            "cta": "See the ladder",
            "url": reverse("tipping:leaderboard", args=[org.id]),
        })

    # 5. What it's all for.
    if card["donation"] and card["donation"].get("raised"):
        prompts.append({
            "icon": "ic-heart",
            "text": f"{org.name} has raised ${card['donation']['raised']:,.0f} for {org.charity_display} so far.",
            "cta": "Add to it",
            "url": reverse("billing:topup", args=[org.id]),
        })

    # 6. The Wall, always last — the nudge that keeps the room warm.
    from orgs.models import WallPost

    wall_count = WallPost.objects.filter(org=org, is_hidden=False).count()
    prompts.append({
        "icon": "ic-msg",
        "text": (
            f"{wall_count} post{'s' if wall_count != 1 else ''} on the Wall. Talk up your picks."
            if wall_count else "The Wall's empty. First tip talk wins the room."
        ),
        "cta": "Open the Wall",
        "url": reverse("orgs:wall", args=[org.id]),
    })
    return prompts


def _ordinal_suffix(n: int) -> str:
    if 11 <= (n % 100) <= 13:
        return "th"
    return {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")


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
        org_comps = org.competitions.all()
        current_round = (
            Round.objects.filter(org=org, competition__in=org_comps, lockout_at__gte=timezone.now())
            .order_by("lockout_at").first()
        )
        if current_round is None:
            current_round = (
                Round.objects.filter(org=org, competition__in=org_comps)
                .order_by("-round_number").first()
            )
        stats = user_org_stats(request.user, org)
        rank = user_rank_in_org(request.user, org)
        tips_done = 0
        tips_total = 0
        if current_round:
            tips_total = current_round.matches.count()
            tips_done = Tip.objects.filter(user=request.user, match__round=current_round, org=org).count()
        charity_vote = org.active_charity_vote
        has_voted = bool(
            charity_vote
            and charity_vote.ballots.filter(user=request.user).exists()
        )
        subscription = None
        donation = None
        if m.is_league_owner:
            subscription = org.subscriptions.filter(
                season=org.season, status="active"
            ).first()
            from billing.donations import donation_summary

            donation = donation_summary(org)
        # §7: EVERY member of a family org sees local + national side by side,
        # never combined. None for standalone orgs (the majority) — no extra
        # queries and no second figure to show.
        from billing.donations import family_totals

        family = family_totals(org)
        cards.append({
            "org": org,
            "round": current_round,
            "tips_done": tips_done,
            "tips_total": tips_total,
            "points": stats["points"],
            "rank": rank,
            "is_admin": m.can_manage,
            "is_owner": m.is_league_owner,
            "role_labels": m.role_labels,
            "charity_vote": charity_vote,
            "has_voted": has_voted,
            "subscription": subscription,
            "donation": donation,
            "family": family,
        })
    # The dashboard is built around ONE comp at a time: a dropdown picks it,
    # its games come forth for tipping. Default to the next comp to lock that
    # still needs tips — that's the one the user should act on.
    now = timezone.now()
    selected = None
    org_param = request.GET.get("org")
    if org_param:
        selected = next((c for c in cards if str(c["org"].id) == org_param), None)
    if selected is None and cards:
        live = [c for c in cards if c["round"] and c["round"].lockout_at >= now]
        needing = [c for c in live if c["tips_done"] < c["tips_total"]]
        pool = needing or live
        selected = min(pool, key=lambda c: c["round"].lockout_at) if pool else cards[0]

    games = []
    if selected and selected["round"]:
        my_picks = dict(
            Tip.objects.filter(
                user=request.user, match__round=selected["round"], org=selected["org"]
            ).values_list("match_id", "selection")
        )
        for g in (
            selected["round"].matches
            .select_related("home_team", "away_team")
            .order_by("kickoff_at", "id")
        ):
            g.my_tip = my_picks.get(g.id)
            g.tipped = g.my_tip is not None
            games.append(g)

    locking_soon = sorted(
        (
            c for c in cards
            if c is not selected and c["round"] and c["round"].lockout_at >= now
        ),
        key=lambda c: c["round"].lockout_at,
    )

    # News & blog — posted by the super admin from /manage/news/.
    from admin_panel.models import NewsPost

    news_posts = list(NewsPost.objects.filter(is_published=True)[:9])

    return render(request, "dashboard.html", {
        "cards": cards,
        "selected": selected,
        "games": games,
        "locking_soon": locking_soon,
        "create_url": reverse("orgs:create"),
        "news_leads": news_posts[:3],
        "news_more": news_posts[3:],
        "prompts": _dashboard_prompts(request.user, selected, games),
    })


@login_required
def dashboard_countdown_partial(request, org_id: int):
    org = get_object_or_404(Organisation, pk=org_id)
    current_round = (
        Round.objects.filter(org=org, competition__in=org.competitions.all(), lockout_at__gte=timezone.now())
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
    elif request.method == "POST" and "avatar" in request.FILES:
        old_name = request.user.avatar.name if request.user.avatar else ""
        avatar_form = AvatarForm(request.POST, request.FILES, instance=request.user)
        if avatar_form.is_valid():
            avatar_form.save()
            if old_name and old_name != request.user.avatar.name:
                request.user.avatar.storage.delete(old_name)
            messages.success(request, "Profile photo updated.")
        else:
            messages.error(request, avatar_form.errors["avatar"].as_text().lstrip("* "))
        return redirect("profile")
    elif request.method == "POST" and "remove_avatar" in request.POST:
        if request.user.avatar:
            request.user.avatar.delete(save=True)
            messages.success(request, "Profile photo removed.")
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


def coming_soon_view(request):
    """Pre-launch 'lock in your spot' page (client's index template).

    Public by design; the staging gate still fronts it until launch. Saves a
    LaunchSignup lead and re-renders with the locked-in confirmation.
    """
    from .models import LaunchSignup

    locked_in = False
    error = ""
    if request.method == "POST":
        name = (request.POST.get("name") or "").strip()
        email = (request.POST.get("email") or "").strip().lower()
        platform = (request.POST.get("current_platform") or "").strip()
        valid_platforms = {c[0] for c in LaunchSignup.PLATFORM_CHOICES}
        if platform not in valid_platforms:
            platform = ""
        if not name or not email:
            error = "Both name and email are needed to lock in your spot."
        else:
            try:
                LaunchSignup.objects.update_or_create(
                    email=email,
                    defaults={"name": name, "current_platform": platform},
                )
                locked_in = True
            except Exception:
                error = "That didn't save — check the email address and try again."
    return render(request, "public/coming_soon.html", {
        "locked_in": locked_in,
        "error": error,
        "platforms": LaunchSignup.PLATFORM_CHOICES,
    })


# The boss letter is fixed copy — senders only fill in the two names, so the
# form can't be turned into an open relay for arbitrary text.
BOSS_LETTER = (
    "Dear {boss},\n"
    "\n"
    "What if our footy tips actually did some good?\n"
    "\n"
    "We're already picking teams and checking the ladder every Monday. "
    "GoodTip just sends our entry fees to a charity we choose. Together.\n"
    "\n"
    "Five minutes to set up. Nothing to lose but bragging rights.\n"
    "\n"
    "Can we do this?\n"
    "\n"
    "— {name}"
)
BOSS_SEND_LIMIT = 5          # per session…
BOSS_SEND_WINDOW = 3600      # …per hour


def tell_the_boss_view(request):
    """Public tell-the-boss page. Besides copy-paste, the letter can be sent
    by GoodTip itself: the visitor fills in their name and the boss's email
    and we deliver the (fixed) note for them.
    """
    import time

    from django.conf import settings
    from django.core.mail import send_mail
    from django.core.validators import validate_email
    from django.core.exceptions import ValidationError

    sent = False
    error = ""
    your_name = ""
    boss_name = ""
    if request.method == "POST" and not request.user.is_authenticated:
        # Sending is a member perk — the template hides the form, but guard
        # the POST too so the relay can't be driven anonymously.
        error = "Sending is a member thing — sign up free first, then we'll deliver it."
    elif request.method == "POST":
        your_name = (request.POST.get("your_name") or "").strip()[:80]
        boss_name = (request.POST.get("boss_name") or "").strip()[:80]
        boss_email = (request.POST.get("boss_email") or "").strip()
        now = time.time()
        recent = [
            t for t in request.session.get("boss_sends", [])
            if now - t < BOSS_SEND_WINDOW
        ]
        if request.POST.get("company"):  # honeypot — bots fill every field
            sent = True
        elif not your_name or not boss_email:
            error = "Your name and the boss's email are both needed."
        elif len(recent) >= BOSS_SEND_LIMIT:
            error = "That's a few notes already — give it an hour and try again."
        else:
            try:
                validate_email(boss_email)
            except ValidationError:
                error = "That email address doesn't look right — check it and try again."
            else:
                letter = BOSS_LETTER.format(boss=boss_name or "Boss", name=your_name)
                try:
                    send_mail(
                        subject=f"{your_name} wants your tipping comp to do some good",
                        message=(
                            f"{letter}\n\n"
                            "----\n"
                            f"{your_name} asked GoodTip to pass this note on. "
                            "See how it works: https://goodtip.com.au/tell-the-boss/\n"
                            "This is a one-off — you're not on any list."
                        ),
                        from_email=settings.DEFAULT_FROM_EMAIL,
                        recipient_list=[boss_email],
                        fail_silently=False,
                    )
                except Exception:
                    logger.exception("Failed to send tell-the-boss note")
                    error = "The note didn't send — try again in a minute, or just copy it."
                else:
                    sent = True
                    recent.append(now)
                    request.session["boss_sends"] = recent
    return render(request, "public/tell_the_boss.html", {
        "active": "boss",
        "sent": sent,
        "boss_error": error,
        "your_name": your_name,
        "boss_name": boss_name,
    })
