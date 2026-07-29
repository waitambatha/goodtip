import logging

from django.contrib import messages
from django.contrib.auth import (
    authenticate, get_user_model, login, logout, update_session_auth_hash,
)
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import PasswordChangeForm
from django.db import transaction
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import ensure_csrf_cookie

from orgs.models import OrgMember, Organisation
from orgs.notifications import send_welcome
from tipping.models import Round, Tip
from tipping.services import (
    annotate_play_state, current_round, user_org_stats, user_rank_in_org,
)

from .forms import (
    AvatarForm, LoginForm, ProfileForm, SecurityForm, SignupForm, VerifyCodeForm,
)
from .models import LoginCode
from .notifications import send_login_code

logger = logging.getLogger(__name__)
User = get_user_model()


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
            # Welcome email is fire-and-forget: send_welcome swallows its own
            # failures, so a mail outage can never block a signup.
            transaction.on_commit(lambda u=user: send_welcome(u))
            next_url = request.GET.get("next") or request.POST.get("next") or ""
            if not next_url.startswith("/"):
                next_url = ""
            # The account exists but nobody is signed in yet: the address has
            # to prove itself first. The welcome-photo detour happens after the
            # code checks out, in verify_view.
            return _start_verification(
                request, user, LoginCode.PURPOSE_SIGNUP, next_url
            )
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


# Keys for the half-finished sign-in that lives in the session between the
# password step and the code step. Deliberately NOT a logged-in session: until
# the code checks out the person is not authenticated for anything.
PENDING_USER_KEY = "pending_2fa_user"
PENDING_PURPOSE_KEY = "pending_2fa_purpose"
PENDING_NEXT_KEY = "pending_2fa_next"


def _start_verification(request, user, purpose, next_url=""):
    """Park the sign-in and email a code. Returns the redirect to the code page."""
    _, code = LoginCode.issue(user, purpose=purpose)
    sent = send_login_code(user, code, purpose=purpose)
    request.session[PENDING_USER_KEY] = user.pk
    request.session[PENDING_PURPOSE_KEY] = purpose
    request.session[PENDING_NEXT_KEY] = next_url or ""
    if not sent:
        # Mail is down or unconfigured. Say so — parking someone on a code
        # screen waiting for an email that will never arrive is worse than
        # admitting the problem.
        messages.error(
            request,
            "We couldn't send your code just now. Try again in a moment, "
            "or contact support if it keeps happening.",
        )
    return redirect("accounts:verify")


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
                next_url = request.GET.get("next") or ""
                if user.two_factor_enabled:
                    # Password was right, but that is only step one.
                    return _start_verification(
                        request, user, LoginCode.PURPOSE_LOGIN, next_url
                    )
                login(request, user)
                joined = _consume_pending_join(request)
                if joined is not None:
                    return post_join_redirect(joined)
                return HttpResponseRedirect(next_url or reverse("dashboard"))
            messages.error(request, "Invalid email or password.")
    else:
        form = LoginForm()
    return render(request, "auth/login.html", {"form": form})


@never_cache
def verify_view(request):
    """Step two: the emailed code.

    Reads the pending user out of the session rather than a URL, so the
    half-finished sign-in can't be handed to someone else as a link.
    """
    user_id = request.session.get(PENDING_USER_KEY)
    if not user_id:
        return redirect("accounts:login")
    user = User.objects.filter(pk=user_id, is_active=True).first()
    if user is None:
        _clear_pending(request)
        return redirect("accounts:login")

    purpose = request.session.get(PENDING_PURPOSE_KEY, LoginCode.PURPOSE_LOGIN)

    if request.method == "POST":
        if "resend" in request.POST:
            return _start_verification(
                request, user, purpose, request.session.get(PENDING_NEXT_KEY, "")
            )
        form = VerifyCodeForm(request.POST)
        if form.is_valid():
            row = (
                LoginCode.objects.filter(
                    user=user, purpose=purpose, consumed_at__isnull=True
                )
                .order_by("-created_at")
                .first()
            )
            if row is None or not row.is_usable:
                messages.error(
                    request,
                    "That code has expired or been used too many times. "
                    "Send yourself a new one.",
                )
            elif row.verify(form.cleaned_data["code"]):
                next_url = request.session.get(PENDING_NEXT_KEY) or ""
                _clear_pending(request)
                if purpose == LoginCode.PURPOSE_SIGNUP and not user.email_verified_at:
                    user.email_verified_at = timezone.now()
                    user.save(update_fields=["email_verified_at"])
                # Backend is explicit because there's no authenticate() call
                # on this path to have set one.
                login(request, user, backend="accounts.backends.EmailBackend")
                joined = _consume_pending_join(request)
                if purpose == LoginCode.PURPOSE_SIGNUP:
                    request.session[POST_SIGNUP_NEXT_KEY] = (
                        next_url or post_join_redirect(joined).url
                    )
                    return redirect("accounts:welcome_photo")
                if joined is not None:
                    return post_join_redirect(joined)
                return HttpResponseRedirect(next_url or reverse("dashboard"))
            else:
                left = max(0, LoginCode.MAX_ATTEMPTS - row.attempts)
                messages.error(
                    request,
                    f"That code isn't right. {left} attempt{'s' if left != 1 else ''} left."
                    if left
                    else "Too many wrong attempts. Send yourself a new code.",
                )
    else:
        form = VerifyCodeForm()

    return render(request, "auth/verify.html", {
        "form": form,
        "email": user.email,
        "purpose": purpose,
        "minutes": int(LoginCode.TTL.total_seconds() // 60),
    })


def _clear_pending(request):
    for key in (PENDING_USER_KEY, PENDING_PURPOSE_KEY, PENDING_NEXT_KEY):
        request.session.pop(key, None)


def logout_view(request):
    logout(request)
    return redirect("landing")


def _first_name(user) -> str:
    """What to call someone in the banner. display_name is free text, so take
    the first word and fall back to the email local part rather than greeting
    anyone as "Demo Sparks FC Captain" or an empty string."""
    name = (user.display_name or "").strip()
    if name:
        return name.split()[0]
    return (user.email or "").split("@")[0] or "there"


def _dashboard_prompts(user, card, games):
    """The rotating headline — what this member should know or do right now,
    most urgent first.

    Each entry is {icon, text, cta, url}. Everything is derived from state we
    already loaded for the card, so this costs no extra queries beyond the
    Wall count.

    Texts are addressed to the member by name because they read as one voice
    talking to them, not as a status panel — the banner is the app's way of
    saying "here is your next move", and a name is what makes it land.
    """
    if not card:
        return []
    org = card["org"]
    rnd = card["round"]
    who = _first_name(user)
    prompts = []

    # 1. An open charity vote they haven't cast — the most time-boxed thing
    #    on the dashboard.
    if card["charity_vote"] and not card["has_voted"]:
        prompts.append({
            "icon": "ic-vote",
            "text": f"{who}, the charity vote for {org.name} is open — you haven't had your say yet.",
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
            "text": f"{who}, you've got {left} match{'es' if left != 1 else ''} still untipped in Round {rnd.round_number}.{when} Lock them in.",
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
        where = f" at {nxt.venue}" if nxt.venue else ""
        if nxt.tipped:
            backing = nxt.home_team.name if nxt.my_tip == "home" else nxt.away_team.name
            text = f"{who}, {nxt.home_team.name} v {nxt.away_team.name} plays {kick}{where} — you're backing {backing}."
        else:
            text = f"{who}, {nxt.home_team.name} v {nxt.away_team.name} plays {kick}{where}. Get your tip in and lock it."
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
        # The round being played, not the next one that can still be tipped.
        # Keying on lockout_at moved the card on to next week the moment the
        # first game bounced, so all weekend the dashboard named a round nobody
        # was watching while this round's scores were still moving. The card
        # already renders a locked round read-only, so it stays correct once
        # tipping closes.
        round_in_play = current_round(
            list(
                annotate_play_state(
                    Round.objects.filter(org=org, competition__in=org_comps)
                ).order_by("-round_number")
            )
        )
        stats = user_org_stats(request.user, org)
        rank = user_rank_in_org(request.user, org)
        tips_done = 0
        tips_total = 0
        if round_in_play:
            tips_total = round_in_play.matches.count()
            tips_done = Tip.objects.filter(user=request.user, match__round=round_in_play, org=org).count()
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
            "round": round_in_play,
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
    # Same round the card around this countdown names, or the two disagree all
    # weekend — the header saying "Round 21" while the timer counts down to
    # Round 22's lockout. countdown_cells renders a Locked chip once the time
    # has passed, which is the right thing to show for a round in play.
    round_in_play = current_round(
        list(
            annotate_play_state(
                Round.objects.filter(org=org, competition__in=org.competitions.all())
            ).order_by("-round_number")
        )
    )
    return render(request, "partials/countdown.html", {"round": round_in_play, "org": org})


@login_required
def profile_view(request):
    pwd_form = PasswordChangeForm(request.user)
    form = ProfileForm(instance=request.user)
    security_form = SecurityForm(instance=request.user)
    if request.method == "POST" and "two_factor" in request.POST:
        security_form = SecurityForm(request.POST, instance=request.user)
        if security_form.is_valid():
            security_form.save()
            messages.success(
                request,
                "Two-step verification is on — we'll email you a code each time you sign in."
                if request.user.two_factor_enabled
                else "Two-step verification is off. Your password alone will sign you in.",
            )
        return redirect("profile")
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
        "security_form": security_form,
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


# The boss letter is fixed copy — senders only ever fill in the two names, so
# the form can't be turned into an open relay for arbitrary text. The copy
# itself now lives in templates/emails/tell_the_boss.{html,txt}.
BOSS_SEND_LIMIT = 5          # per session…
BOSS_SEND_WINDOW = 3600      # …per hour


def tell_the_boss_view(request):
    """Public tell-the-boss page. Besides copy-paste, the letter can be sent
    by GoodTip itself: the visitor fills in their name and the boss's email
    and we deliver the (fixed) note for them.
    """
    import time

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
                from goodtip.mail import send_template, site_url

                # Reply-to is the sender, not GoodTip: the boss replying should
                # reach the member who asked for the note, and it tells the
                # recipient a real colleague is behind this.
                ok = send_template(
                    "tell_the_boss",
                    subject=f"{your_name} wants your tipping comp to do some good",
                    to=boss_email,
                    context={
                        "your_name": your_name,
                        "boss_name": boss_name,
                        "how_url": site_url("/tell-the-boss/"),
                    },
                    reply_to=[request.user.email] if request.user.email else None,
                )
                if ok:
                    sent = True
                    recent.append(now)
                    request.session["boss_sends"] = recent
                else:
                    error = "The note didn't send — try again in a minute, or just copy it."
    return render(request, "public/tell_the_boss.html", {
        "active": "boss",
        "sent": sent,
        "boss_error": error,
        "your_name": your_name,
        "boss_name": boss_name,
    })
