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
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import ensure_csrf_cookie

from orgs.models import OrgMember, Organisation
from orgs.notifications import send_welcome
from tipping.models import Match, Round, Tip
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
            # If a colleague sent them a "tell the boss" note, this is the
            # moment it advances — matched on the address they just signed up
            # with, so it works whether or not they used the link in the email.
            from .boss import link_boss_signup

            link_boss_signup(user)
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


# How many upcoming fixtures the dashboard lists. ~4 AFL rounds: far enough to
# plan ahead, short enough that the panel is not a wall of games.
UPCOMING_LIMIT = 40


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
        # §7's local + national totals came off the dashboard — both read $0
        # for every group until money moves. family_totals is NOT called any
        # more: it walks the whole family tree per org card, and this loop
        # already runs once per league a member belongs to, so computing two
        # figures nothing renders was pure cost. billing.donations still has
        # it for whenever the totals find a home.
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
        # Land on a round that still has something to tip. lockout_at is the
        # round's FIRST kickoff, so selecting on it dropped a round the moment
        # its opening game began — even with six fixtures still days away — and
        # sent members to a read-only screen while their week was still live.
        live = [c for c in cards if c["round"] and c["round"].has_open_matches]
        needing = [c for c in live if c["tips_done"] < c["tips_total"]]
        pool = needing or live
        if pool:
            selected = min(pool, key=lambda c: c["round"].lockout_at)
        else:
            # Nothing left to tip anywhere: fall back to the round in play so
            # the member sees results rather than an empty screen.
            selected = cards[0]

    # ONE ROUND AT A TIME, with a navigator to move between them.
    #
    # This used to be a flat list of everything still to play, ordered by
    # kickoff and grouped by round in the template. The intent was "what can I
    # tip right now", and for a single-code league in mid-season it read fine.
    # In practice it did not: an org tipping AFL and NRL interleaves two round
    # numberings, so scrolling produced "Round 2, Round 24, Round 26, Round 25"
    # with fixtures running one after another and no way to get your bearings.
    # It also could not answer the question people actually ask on a Monday —
    # how did I go last week — because a past round was never in the list at
    # all. That meant leaving the dashboard for My Tips to see a result.
    #
    # So the panel is scoped to a round and given a way to move: arrows for the
    # neighbours, a dropdown for the jump. Past rounds come with what happened
    # (your pick, the result, the points) and future rounds beyond the tipping
    # window are shown but shut, which is the honest picture of a season rather
    # than a scroll of everything at once.
    games = []
    open_games = 0
    org_series, active_slugs = [], []
    round_nav = None
    if selected:
        # ---- competition filter. An org tipping two codes gets both slates
        # interleaved by kickoff, which is right for "what is on this week" and
        # wrong for "just show me the NRL".
        from tipping.services import competition_filter

        org_series, active_series = competition_filter(
            selected["org"], request.GET.getlist("series")
        )
        active_slugs = [s.slug for s in active_series if s in org_series]

        org_rounds = Round.objects.filter(
            org=selected["org"], competition__in=selected["org"].competitions.all()
        )
        if active_series:
            org_rounds = org_rounds.filter(series__in=active_series)

        # Navigation is by ROUND NUMBER, not by Round row. A round row is per
        # (org, series), so an org on AFL and NRL holds two rows called "round
        # 22" — stepping through rows would visit the same number twice and
        # make the arrows feel broken. The number is what a member means when
        # they say "go back to 16".
        numbers = sorted(set(org_rounds.values_list("round_number", flat=True)))
        if numbers:
            in_play = selected["round"].round_number if selected["round"] else numbers[-1]
            try:
                wanted = int(request.GET.get("round", ""))
            except (TypeError, ValueError):
                wanted = None
            # An out-of-range or hand-edited round falls back to the one being
            # played rather than 404ing — a stale bookmark should land you
            # somewhere useful.
            current_no = wanted if wanted in numbers else (
                in_play if in_play in numbers else numbers[-1]
            )
            i = numbers.index(current_no)
            round_nav = {
                "numbers": numbers,
                "current": current_no,
                "prev": numbers[i - 1] if i > 0 else None,
                "next": numbers[i + 1] if i < len(numbers) - 1 else None,
                "in_play": in_play,
                "is_in_play": current_no == in_play,
            }

        upcoming = Match.objects.filter(
            round__org=selected["org"],
            round__competition__in=selected["org"].competitions.all(),
        )
        if active_series:
            upcoming = upcoming.filter(round__series__in=active_series)
        if round_nav:
            upcoming = upcoming.filter(round__round_number=round_nav["current"])
        else:
            upcoming = upcoming.filter(kickoff_at__gt=now)
        upcoming = (
            upcoming
            .select_related("home_team", "away_team", "round", "round__series")
            .order_by("kickoff_at", "id")[:UPCOMING_LIMIT]
        )
        upcoming = list(upcoming)
        # The tip itself, plus how it went. Grading writes is_correct and
        # points_awarded onto the Tip, so a past round can report your result
        # without recomputing anything — which is what lets the dashboard
        # answer "how did I go last week" instead of sending you to My Tips.
        my_tips = {
            t.match_id: t
            for t in Tip.objects.filter(
                user=request.user, match__in=upcoming, org=selected["org"]
            )
        }
        my_picks = {mid: t.selection for mid, t in my_tips.items()}
        # MatchReader's read on each upcoming game. Attached here rather than
        # queried from the template so the work is visible and bounded: these
        # are the next few fixtures, not a whole season.
        #
        # Batched deliberately. Read one fixture at a time this cost three
        # queries each and put the dashboard well past two minutes on a remote
        # database; the batched call answers the same for the whole slate in a
        # handful.
        readers = {}
        try:
            from matchreader.services import read_matches_verbose

            readers = read_matches_verbose(upcoming)
        except Exception:                       # noqa: BLE001 — never break the dashboard
            logger.exception("MatchReader failed for the dashboard slate")

        # Only the rounds inside the tipping window may be picked. submit_tip
        # enforces this regardless; marking it here is so the screen says so
        # up front, and names the round it is waiting on — "locked" without a
        # reason is indistinguishable from "you missed it".
        from tipping.services import tip_window

        window = tip_window(selected["org"])
        round_points = 0
        round_correct = 0
        round_graded = 0
        for g in upcoming:
            tip = my_tips.get(g.id)
            g.my_tip = my_picks.get(g.id)
            g.tipped = g.my_tip is not None
            g.reader = readers.get(g.id)
            state = window.get(g.round_id, {"open": True, "waits_for": None})
            g.round_open = state["open"]
            g.can_tip = g.round_open and not g.is_locked
            g.lock_note = ""
            if not g.round_open and state["waits_for"] is not None:
                g.lock_note = (
                    f"Locked. You can tip this once round "
                    f"{state['waits_for'].round_number} is over."
                )
            # How this pick went, for a round already played. None means not
            # graded yet, which is different from wrong and has to look
            # different — a game still in play must not be shown as a loss.
            g.tip_correct = tip.is_correct if tip else None
            g.tip_points = tip.points_awarded if tip else 0
            if tip is not None and tip.is_correct is not None:
                round_graded += 1
                round_points += tip.points_awarded
                round_correct += 1 if tip.is_correct else 0
            # "Open" counts only what can still be acted on, which is what the
            # Confirm button and its empty state key off. A past round is
            # rendered read-only, so counting it here would offer a confirm for
            # games that finished a month ago.
            if g.can_tip:
                open_games += 1
            games.append(g)

        if round_nav is not None:
            round_nav["graded"] = round_graded
            round_nav["correct"] = round_correct
            round_nav["points"] = round_points
            # A round is "done" once every tip on it has been graded, which is
            # what switches the panel from tipping to reporting.
            round_nav["played"] = round_graded > 0

    # ---- htmx: just the slate ------------------------------------------
    #
    # The competition filter and the round navigator change this one region and
    # nothing else on the page, so they swap it in place rather than reloading
    # the document. Returning early here — from the SAME view, on the same
    # context — is what stops the swapped markup drifting from the first paint;
    # a second view rendering "the fixtures, roughly" is a copy that goes stale
    # the first time either one is edited.
    #
    # Everything below this point (the preview round for people with no group,
    # the news column, the prompt rotator) is not in the partial, so returning
    # before it is also the cheaper path.
    #
    # `c` is supplied because the full page binds it with {% with c=selected %}
    # around the include, and the partial cannot see that when rendered alone.
    if selected and request.headers.get("HX-Request") and request.GET.get("slate"):
        return render(request, "partials/dashboard_slate.html", {
            "c": selected,
            "selected": selected,
            "games": games,
            "open_games": open_games,
            "org_series": org_series,
            "active_slugs": active_slugs,
            "round_nav": round_nav,
        })

    # Someone who hasn't joined a group yet used to land on an empty page, which
    # reads as "nothing on" rather than "you're one step away". Rounds hang off
    # an org, so there is no such thing as a global fixture list — we borrow the
    # next round to lock anywhere on GoodTip and show it read-only. Touching a
    # team opens the join prompt instead of recording anything: no org means no
    # Tip row could be written even if the POST were attempted.
    preview_round = None
    if not cards:
        preview_round = (
            Round.objects.filter(lockout_at__gte=now)
            .select_related("series", "org")
            .order_by("lockout_at")
            .first()
            or Round.objects.select_related("series", "org").order_by("-lockout_at").first()
        )
        if preview_round:
            games = list(
                preview_round.matches
                .select_related("home_team", "away_team")
                .order_by("kickoff_at", "id")
            )

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
        # How many fixtures in the shown round have not kicked off. The panel
        # goes read-only on this rather than on the round's lockout, which is
        # only its FIRST kickoff and said "locked" while most of the round was
        # still days away.
        "open_games": open_games,
        # Competition filter: the series this org actually has rounds in, and
        # which are selected.
        "org_series": org_series,
        "active_slugs": active_slugs,
        # Round navigator: which round is shown, its neighbours for the arrows,
        # the full list for the dropdown, and — for a round already played —
        # how the member went.
        "round_nav": round_nav,
        "preview_round": preview_round,
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
        # the POST too so the relay can't be driven anonymously. Carry them to
        # signup and back rather than dead-ending: they filled the form in,
        # and losing that to a login wall is how people give up.
        request.session["boss_draft"] = {
            "your_name": (request.POST.get("your_name") or "").strip()[:80],
            "boss_name": (request.POST.get("boss_name") or "").strip()[:80],
            "boss_email": (request.POST.get("boss_email") or "").strip()[:254],
        }
        messages.info(request, "Create your free account and we'll send that note straight away.")
        return redirect(f"{reverse('accounts:signup')}?next={reverse('tell_the_boss')}")
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
                    # Recorded so the sender can watch it move, and so the
                    # boss's eventual signup has something to match against.
                    from .models import BossInvite

                    BossInvite.objects.create(
                        sender=request.user,
                        boss_name=boss_name,
                        boss_email=boss_email,
                        subject=f"{your_name} wants your tipping comp to do some good",
                        body_preview=render_to_string(
                            "emails/tell_the_boss.txt",
                            {"your_name": your_name, "boss_name": boss_name,
                             "how_url": site_url("/tell-the-boss/")},
                        ),
                    )
                    request.session.pop("boss_draft", None)
                    return redirect("boss_progress")
                else:
                    error = "The note didn't send — try again in a minute, or just copy it."
    draft = request.session.get("boss_draft") or {}
    # A signed-in member who has not asked to read the pitch gets the focused
    # send page: two image panes, one card, three fields. The full marketing
    # page is still there behind ?copy=1 for anyone who wants the letter to
    # paste themselves.
    if request.user.is_authenticated and request.GET.get("copy") != "1":
        return render(request, "public/boss_send.html", {
            "boss_error": error,
            "your_name": your_name or draft.get("your_name", "") or request.user.display_name,
            "boss_name": boss_name or draft.get("boss_name", ""),
            "boss_email_draft": draft.get("boss_email", ""),
        })
    return render(request, "public/tell_the_boss.html", {
        "active": "boss",
        "sent": sent,
        "boss_error": error,
        "your_name": your_name or draft.get("your_name", "") or (
            request.user.display_name if request.user.is_authenticated else ""
        ),
        "boss_name": boss_name or draft.get("boss_name", ""),
        "boss_email_draft": draft.get("boss_email", ""),
        "my_invites": (
            request.user.boss_invites.select_related("org", "boss_user")[:5]
            if request.user.is_authenticated else []
        ),
    })


@login_required
def boss_progress_view(request):
    """Where a sender watches their note travel: sent -> joined -> in the group.

    Its own page rather than a strip on the dashboard, because the interesting
    thing is the sequence, and a sequence needs room to be drawn.
    """
    invites = list(
        request.user.boss_invites.select_related("org", "boss_user")
    )
    return render(request, "public/boss_progress.html", {
        "invites": invites,
        "latest": invites[0] if invites else None,
    })


# ---------------------------------------------------------------------------
# Public contact form
# ---------------------------------------------------------------------------

ENQUIRY_LIMIT = 4            # per session…
ENQUIRY_WINDOW = 3600        # …per hour


def contact_submit_view(request):
    """Take a message from the public contact form.

    The form used to be a mockup: `onsubmit="return false"`, no action, no
    method, no field names and no CSRF token. It looked complete and did
    nothing — every enquiry typed into it was thrown away on submit, with the
    visitor shown no error because as far as the page was concerned nothing had
    gone wrong. So this is a new path rather than a fix to an existing one.

    Enquiries are stored first and emailed second, deliberately. If Postmark is
    down or misconfigured, the message is still recorded and still shows up in
    the manage inbox — losing a sales lead to a mail outage is the one failure
    worth engineering against here.
    """
    import time

    from django.core.exceptions import ValidationError
    from django.core.validators import validate_email

    from admin_panel.models import Enquiry
    from goodtip.mail import send_template, site_url

    back = request.POST.get("source") or "/"
    if not back.startswith("/"):
        back = "/"

    if request.method != "POST":
        return redirect(back)

    # Honeypot: a field no human sees, that bots fill in anyway. Answer as
    # though it worked — telling a bot it was caught only helps it try again.
    if request.POST.get("company"):
        return redirect(f"{back}?sent=1#contact-form")

    name = (request.POST.get("name") or "").strip()[:120]
    email = (request.POST.get("email") or "").strip()[:254]
    organisation = (request.POST.get("organisation") or "").strip()[:160]
    interest = (request.POST.get("interest") or "").strip()[:120]
    message = (request.POST.get("message") or "").strip()[:4000]

    now = time.time()
    recent = [t for t in request.session.get("enquiries", []) if now - t < ENQUIRY_WINDOW]

    error = ""
    if not name or not email or not message:
        error = "Your name, email and a message are all needed."
    elif len(recent) >= ENQUIRY_LIMIT:
        error = "That's a few messages already — give it an hour and try again."
    else:
        try:
            validate_email(email)
        except ValidationError:
            error = "That email address doesn't look right — check it and try again."

    if error:
        # Held in the session rather than re-rendered, because the form is an
        # include on five different pages: bouncing back to whichever one they
        # were on is the only way to return them to the page they were reading.
        request.session["enquiry_error"] = error
        request.session["enquiry_draft"] = {
            "name": name, "email": email, "organisation": organisation,
            "interest": interest, "message": message,
        }
        return redirect(f"{back}#contact-form")

    enquiry = Enquiry.objects.create(
        name=name, email=email, organisation=organisation,
        interest=interest, message=message, source_page=back,
    )

    recent.append(now)
    request.session["enquiries"] = recent
    request.session.pop("enquiry_error", None)
    request.session.pop("enquiry_draft", None)

    # Everyone who can act on it. Staff rather than superusers only: the point
    # is that somebody answers, and narrowing it to one account means one
    # person's holiday is an unanswered enquiry.
    staff_emails = list(
        User.objects.filter(is_staff=True, is_active=True)
        .exclude(email="")
        .values_list("email", flat=True)
    )
    if staff_emails:
        send_template(
            "enquiry_admin",
            subject=f"New enquiry — {name}" + (f" ({organisation})" if organisation else ""),
            to=staff_emails,
            context={
                "enquiry": enquiry,
                # Straight to the one enquiry. Not logged in? staff_member_required
                # sends them to the login page carrying this as ?next=, so they
                # land here the moment they are through it.
                "enquiry_url": site_url(
                    reverse("manage:enquiry_detail", args=[enquiry.pk])
                ),
            },
            # Reply-to the enquirer, so hitting reply in a mail client works
            # even when nobody feels like opening the admin.
            reply_to=[email],
        )

    return redirect(f"{back}?sent=1#contact-form")
