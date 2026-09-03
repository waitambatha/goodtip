import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import (
    authenticate, get_user_model, login, logout, update_session_auth_hash,
)
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import PasswordChangeForm
from django.db import transaction
from django.http import HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_POST

from orgs.models import Group, OrgMember, Organisation
from orgs.notifications import send_welcome
from tipping.models import Match, Round, Tip
from tipping.services import (
    annotate_play_state, current_round, user_org_stats, user_rank_in_org,
)

from .forms import (
    AvatarForm, LoginForm, ProfileForm, SecurityForm, SignupForm, TipCarryForm,
    VerifyCodeForm,
)
from .models import LoginCode
from .onboarding import TOURS
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
    from orgs.context import set_current_org
    from orgs.services import add_member

    add_member(request.user, org, inviter_id=inviter_id)
    # Same reason as orgs.views.join_view: the organisation they were invited
    # to has to become the one they are standing in, or the dashboard they
    # land on names a different one.
    set_current_org(request, org)
    messages.success(request, f"Joined {org.name}.")
    return org


def post_join_redirect(org):
    """Where a new member lands after joining a group.

    This used to divert to a one-time top-up prompt when the org had a pledge.
    Both are gone: GoodTip funds the donation from its own revenue, so there is
    nothing for a participant to contribute and asking would contradict the
    promise the site now makes. Straight to the dashboard, which is where
    somebody who just joined wants to be anyway.
    """
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
        # The page used to print the form field's max_length here, which is 16
        # — deliberately loose so a pasted "123 456" survives — and so told
        # every member to expect a 16-digit code. This is the real length, and
        # the same number the page auto-submits on.
        "code_length": LoginCode.CODE_LENGTH,
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
        # Reports the total; no longer invites a contribution. Participants are
        # never asked for money, so "Add to it" pointed at a flow that has been
        # removed and a proposition the site no longer makes.
        prompts.append({
            "icon": "ic-heart",
            "text": (
                f"{org.name} has raised ${card['donation']['raised']:,.0f} "
                f"for {org.charity_display} so far."
            ),
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


def _parse_round_param(raw, numbers_by_code, numbers):
    """Read `?round=` into (code slug or None, round number or None).

    Two spellings, because both have to keep working:

      "4"       a plain number — every link shared before the navigator became
                code-aware, and still the normal form in a one-code league.
      "aflw-4"  a code and a number, which is what a multi-code league emits.

    Anything unparseable, or naming a round that does not exist, comes back as
    (None, None) and the caller falls back to this week. A hand-edited or stale
    URL should land somewhere useful, never on an error.

    Split on the LAST hyphen: a slug can contain one (state-of-origin), a round
    number cannot.
    """
    raw = (raw or "").strip()
    if not raw:
        return None, None
    if raw.isdigit():
        n = int(raw)
        return (None, n) if n in numbers else (None, None)
    code, _, tail = raw.rpartition("-")
    if code in numbers_by_code and tail.isdigit() and int(tail) in numbers_by_code[code]:
        return code, int(tail)
    return None, None


def _nearest_code_for(org_rounds, number, numbers_by_code, week_round_ids):
    """Which code's round `number` a bare `?round=<number>` means.

    Only reached in a league tipping several codes, where the number alone is
    ambiguous — AFL round 4 was played in April and AFLW round 4 is this
    weekend, and one of those is what somebody following the link wants.

    A round that is open to tip right now wins. Failing that, the one whose
    lockout is closest to now in either direction: for a number every code has
    long finished, that is the most recent, and for one still ahead it is the
    next one up.
    """
    candidates = list(
        org_rounds.filter(round_number=number)
        .select_related("series")
        .only("id", "lockout_at", "series__slug")
    )
    if not candidates:
        return None
    open_ids = set(week_round_ids)
    live = [r for r in candidates if r.id in open_ids]
    if live:
        return live[0].series.slug
    now = timezone.now()
    nearest = min(candidates, key=lambda r: abs(r.lockout_at - now))
    return nearest.series.slug


@login_required
def dashboard_view(request):
    from orgs.context import current_group, current_org, set_current_org

    memberships = (
        OrgMember.objects.filter(user=request.user)
        .select_related("org")
        .order_by("org__name")
    )
    # Two passes: an account can belong to several organisations (several
    # businesses run through GoodTip, each with its own staff), and only ONE
    # card — the selected one — is ever rendered in full. A single pass used
    # to run a stats query, a rank query, a charity-vote lookup and (for every
    # org this member owns) a subscription + donation-summary call for EVERY
    # membership, on every dashboard load, only to throw away all but one set
    # of numbers. This first pass builds just enough per org to pick which one
    # is selected and to drive the org <select> and "locking soon" list — the
    # full stats are computed once, below, for the selected org only.
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
        # The card is about the room this member is standing in, so its total
        # and its rank have to come from the same one.
        card_group = current_group(request, org)
        tips_done = 0
        tips_total = 0
        if round_in_play:
            tips_total = round_in_play.matches.count()
            tips_done = Tip.objects.filter(
                user=request.user, match__round=round_in_play, org=org,
                group=card_group,
            ).count()
        cards.append({
            "org": org,
            # Which room this card is reporting on. It was already worked out
            # above to scope the points and the rank, but never reached the
            # template — so a member tipping inside a group saw a card headed
            # only by the organisation, with group figures under it and nothing
            # saying so. The card can now name the room it is talking about.
            "group": card_group,
            "groups": (
                list(org.groups.filter(approval_status=Group.APPROVAL_APPROVED))
                if org.groups_enabled else []
            ),
            "round": round_in_play,
            "tips_done": tips_done,
            "tips_total": tips_total,
            "is_admin": m.can_manage,
            "is_owner": m.is_league_owner,
            "role_labels": m.role_labels,
            # Kept only to compute the full stats below for whichever card is
            # selected — never read by the template.
            "_membership": m,
        })
    # The dashboard is built around ONE comp at a time: a dropdown picks it,
    # its games come forth for tipping. Default to the next comp to lock that
    # still needs tips — that's the one the user should act on.
    now = timezone.now()
    selected = None
    org_param = request.GET.get("org")
    if org_param:
        selected = next((c for c in cards if str(c["org"].id) == org_param), None)
        # The "Your organisation" picker changes which card is shown here, but
        # used to leave the session's current-org untouched — so everything
        # ELSE that reads it (the room switcher on this very card, the nav,
        # My Tips, the Leaderboard) kept pointing at whichever org the nav
        # dropdown last set, disagreeing with the card on screen in front of
        # you. Picking an org here is the same action as picking one from the
        # nav, so it carries the same effect.
        if selected is not None and selected["org"].id != getattr(current_org(request), "id", None):
            set_current_org(request, selected["org"])
    if selected is None and cards:
        # No explicit ?org= — default to wherever the nav already says you
        # are. Without this, a plain visit to /dashboard/ ignored the session
        # entirely and jumped to whichever org's tipping deadline was
        # soonest, which could — and did — disagree with the room switcher
        # and every other page, all of which read the session directly.
        session_org = current_org(request)
        if session_org is not None:
            selected = next((c for c in cards if c["org"].id == session_org.id), None)
    if selected is None and cards:
        # First visit ever, or the session named an org this account no
        # longer has a card for. Land on a round that still has something to
        # tip. lockout_at is the round's FIRST kickoff, so selecting on it
        # dropped a round the moment its opening game began — even with six
        # fixtures still days away — and sent members to a read-only screen
        # while their week was still live.
        live = [c for c in cards if c["round"] and c["round"].has_open_matches]
        needing = [c for c in live if c["tips_done"] < c["tips_total"]]
        pool = needing or live
        if pool:
            selected = min(pool, key=lambda c: c["round"].lockout_at)
        else:
            # Nothing left to tip anywhere: fall back to the round in play so
            # the member sees results rather than an empty screen.
            selected = cards[0]

    # Full stats — points, rank, charity vote, subscription, donation — computed
    # once, for the selected card only. See the comment above the first pass.
    if selected is not None:
        org = selected["org"]
        m = selected["_membership"]
        card_group = selected["group"]
        stats = user_org_stats(request.user, org, group=card_group)
        rank = user_rank_in_org(request.user, org, group=card_group)
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
        # more: it walks the whole family tree per org card, and this only
        # ever runs once now, so computing two figures nothing renders would
        # still be pure cost. billing.donations still has it for whenever the
        # totals find a home.
        selected.update({
            "points": stats["points"],
            "rank": rank,
            "charity_vote": charity_vote,
            "has_voted": has_voted,
            "subscription": subscription,
            "donation": donation,
        })
    for c in cards:
        c.pop("_membership", None)

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
    slate_done = 0
    slate_total = 0
    slate_round_ids = set()
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
        # THIS WEEK IS NOT A ROUND NUMBER.
        #
        # The navigator moves by round NUMBER, because that is what a member
        # means by "go back to 16". But a number does not identify a round:
        # a round row is per (org, series), so "round 3" in a league tipping
        # four codes is FIVE rows — AFLW round 3, which is on this week, plus
        # NRL and AFL round 3 from March, NRLW round 3 from July and Origin
        # game 3. Landing on the number showed all five: one live round and
        # four months-dead ones stacked underneath it, greyed out and
        # unclickable. That is the screen the client reported, and it was not
        # an NRL filtering bug — it was every code at once.
        #
        # So the default landing is its own mode rather than a number. "This
        # week" means the current open round FOR EACH COMPETITION — AFLW 3 and
        # NRL 26 side by side, each one genuinely tippable. Picking a number
        # from the dropdown or stepping with the arrows switches to number
        # mode.
        #
        # AND A NUMBER ON ITS OWN IS NOT A ROUND EITHER.
        #
        # Number mode used to show every round carrying that number, on the
        # reasoning that it was the honest answer when four codes have one. The
        # client hit the other end of that: opening AFLW round 4 to tip this
        # weekend and finding AFL round 4 — played in April, eight dead
        # fixtures — stacked underneath it. Honest is not the same as useful,
        # and nobody has ever meant "every code's round 4 at once".
        #
        # So in a league tipping more than one code the navigator moves inside
        # ONE code: the dropdown groups its rounds under AFL, AFLW, NRL, NRLW,
        # and the arrows step through that code's numbers. `?round=` carries
        # the code with it — `aflw-4` — and a bare `?round=4` from an old link
        # is resolved to whichever code's round 4 is nearest to now rather than
        # to all of them. A single-code league is untouched: there is nothing
        # to disambiguate, so the value stays a plain number.
        week_round_ids = []
        if numbers:
            # The tipping window names the rounds open to tip right now, per
            # series. The earliest per series is this week's round for that
            # code; the rest of the window sits one step along the navigator.
            from tipping.services import tip_window as _tw

            open_ids = [rid for rid, st in _tw(selected["org"]).items() if st["open"]]
            earliest_per_series = {}
            for rnd in (
                org_rounds.filter(id__in=open_ids)
                .order_by("series_id", "round_number")
                .only("id", "series_id", "round_number")
            ):
                prev = earliest_per_series.get(rnd.series_id)
                if prev is None or rnd.round_number < prev.round_number:
                    earliest_per_series[rnd.series_id] = rnd
            week_rounds = list(earliest_per_series.values())
            week_round_ids = [r.id for r in week_rounds]
            open_numbers = sorted(r.round_number for r in week_rounds)

            # The number the arrows and the "this week" link step from. With
            # several codes open at different numbers there is no single right
            # answer, so the earliest is used — it is the one a member is most
            # likely to still be filling in.
            if open_numbers:
                in_play = open_numbers[0]
            elif selected["round"]:
                in_play = selected["round"].round_number
            else:
                in_play = numbers[-1]

            # ---- what the navigator can move between ----------------------
            #
            # Every (code, number) in view, and the codes in a stable order, so
            # the dropdown's groups and the arrows' step both come from one
            # list rather than from two queries that could disagree.
            rows = list(
                org_rounds.select_related("series")
                .order_by("series__name", "round_number")
                .values_list("series__slug", "series__name", "round_number")
            )
            series_order, numbers_by_code, code_names = [], {}, {}
            for slug, name, number in rows:
                if slug not in numbers_by_code:
                    series_order.append(slug)
                    numbers_by_code[slug] = []
                    code_names[slug] = name
                numbers_by_code[slug].append(number)
            for slug in numbers_by_code:
                numbers_by_code[slug] = sorted(set(numbers_by_code[slug]))
            # One code in view — either the league only tips one, or the chips
            # have narrowed it to one — means a number is unambiguous, so the
            # navigator stays exactly as it was and the URLs stay plain.
            multi = len(series_order) > 1

            wanted_code, wanted_no = _parse_round_param(
                request.GET.get("round", ""), numbers_by_code, numbers,
            )
            if multi and wanted_no is not None and wanted_code is None:
                # A bare `?round=4` in a multi-code league — an old bookmark,
                # or a link shared before this existed. Resolve it to the code
                # whose round 4 is nearest to now, which is the one somebody
                # following that link almost certainly meant, instead of
                # showing every code's round 4 at once.
                wanted_code = _nearest_code_for(
                    org_rounds, wanted_no, numbers_by_code, week_round_ids,
                )

            # Week mode is the default: no ?round= at all, or one that is out
            # of range or hand-edited. A stale bookmark should land somewhere
            # useful rather than 404, and "somewhere useful" is this week.
            is_week = wanted_no is None
            # With nothing open anywhere — end of season — there is no week to
            # show, so fall back to a real number rather than an empty panel.
            if is_week and not week_round_ids:
                is_week = False

            current_code = wanted_code
            current_no = wanted_no
            if current_no is None:
                current_no = in_play if in_play in numbers else numbers[-1]
            if multi and current_code is None:
                current_code = _nearest_code_for(
                    org_rounds, current_no, numbers_by_code, week_round_ids,
                )
            # The list the arrows step along: this code's rounds when there is
            # a code, every number otherwise. Stepping across codes was the
            # other half of the client's report — the arrows walked out of AFLW
            # and into last April's AFL without saying so.
            steps = numbers_by_code.get(current_code) or numbers
            if current_no not in steps:
                current_no = steps[-1]
            i = steps.index(current_no)

            def _value(code, number):
                """What goes in ?round=. Plain in a one-code league."""
                return f"{code}-{number}" if (multi and code) else str(number)

            round_nav = {
                # Flat numbers, still, for a single-code league — the template
                # renders these as bare options.
                "numbers": numbers,
                "multi": multi,
                # [(code label, [(value, number), ...]), ...] for the grouped
                # dropdown a multi-code league gets.
                "groups": [
                    (code_names[slug], [(_value(slug, n), n) for n in numbers_by_code[slug]])
                    for slug in series_order
                ] if multi else [],
                "current": _value(current_code, current_no),
                "current_number": current_no,
                "current_code": current_code,
                "current_code_label": code_names.get(current_code, ""),
                "prev": _value(current_code, steps[i - 1]) if i > 0 else None,
                "next": _value(current_code, steps[i + 1]) if i < len(steps) - 1 else None,
                "in_play": in_play,
                # Week mode, as opposed to a numbered round. Drives both the
                # dropdown's selected option and the "back to this week" link.
                "is_week": is_week,
            }

        upcoming = Match.objects.filter(
            round__org=selected["org"],
            round__competition__in=selected["org"].competitions.all(),
        )
        if active_series:
            upcoming = upcoming.filter(round__series__in=active_series)
        if round_nav and round_nav["is_week"]:
            upcoming = upcoming.filter(round_id__in=week_round_ids)
        elif round_nav:
            upcoming = upcoming.filter(round__round_number=round_nav["current_number"])
            # THE LINE THAT FIXES THE CLIENT'S SCREEN. Without it, "round 4" in
            # a league tipping four codes is four rounds, three of them months
            # dead, all rendered one under the other.
            if round_nav["current_code"]:
                upcoming = upcoming.filter(round__series__slug=round_nav["current_code"])
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
                user=request.user, match__in=upcoming, org=selected["org"],
                group=current_group(request, selected["org"]),
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
        slate_done = 0
        slate_total = 0
        # The rounds the TIPPABLE part of the slate spans. In week mode that
        # is one round per competition, so naming "Round 3" over a sheet
        # holding AFLW 3 and NRL 26 would be picking one of them at random.
        slate_round_ids = set()
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
                # The slate's own progress meter. c.tips_done / c.tips_total
                # describe the round in play, which is what the stat card is
                # about; the meter above the fixtures is about the fixtures
                # actually on screen. Those were the same thing back when the
                # panel always showed one round, and stopped being the same
                # thing when "this week" became the current open round of every
                # competition at once — a slate of 32 fixtures under a meter
                # reading "3 of 8".
                slate_total += 1
                slate_round_ids.add(g.round_id)
                if g.tipped:
                    slate_done += 1
            games.append(g)

        # PLAYABLE SERIES FIRST.
        #
        # One round number is not one round. Codes run their own seasons and
        # are nowhere near each other: for a league on AFL+NRL, "round 2" is
        # NRL round 2 (played in March), AFL round 2 (March), Origin game 2
        # (June), NRLW round 2 (July) — and AFLW round 2, which is the only one
        # still to come. Ordered by kickoff, the four dead groups sat on top and
        # the one live group was below the fold, so the first team anybody
        # clicked belonged to a match finished five months ago and nothing
        # happened. It was not broken; it was buried.
        #
        # Sorting by (round finished, kickoff) floats whatever can still be
        # tipped to the top and leaves the rest below in the order they were
        # played. Chronology is preserved inside each group, which is what
        # matters once you are reading one.
        played_round = {}
        round_start = {}
        for g in games:
            if g.round_id not in played_round:
                played_round[g.round_id] = True
            if not g.is_locked:
                played_round[g.round_id] = False
            first = round_start.get(g.round_id)
            if first is None or g.kickoff_at < first:
                round_start[g.round_id] = g.kickoff_at
        # A ROUND IS A BLOCK, not a scatter of fixtures that happen to share a
        # number. The template groups with {% regroup %}, which only collects
        # CONSECUTIVE items — so sorting by kickoff alone broke each round into
        # as many fragments as it had gaps in the interleaving, and the panel
        # repeated "Round 3 · AFLW … Round 26 · NRL … Round 3 · AFLW" down the
        # page. That was survivable while the panel showed one round; showing
        # the current round of four codes at once turned it into nonsense.
        #
        # Rounds are ordered by when they START (still-tippable ones first),
        # and chronology is preserved inside each — which is what the previous
        # sort was reaching for and did not quite express.
        games.sort(key=lambda g: (
            played_round.get(g.round_id, False),
            round_start.get(g.round_id, g.kickoff_at),
            g.round_id,
            g.kickoff_at,
            g.id,
        ))

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
    # One round on the sheet, or several? Only a single-round slate can
    # honestly be labelled with a round number.
    slate_round_no = None
    if len(slate_round_ids) == 1:
        only = next(iter(slate_round_ids))
        slate_round_no = next(
            (g.round.round_number for g in games if g.round_id == only), None,
        )

    if selected and request.headers.get("HX-Request") and request.GET.get("slate"):
        return render(request, "partials/dashboard_slate.html", {
            "c": selected,
            "selected": selected,
            "games": games,
            "open_games": open_games,
            "slate_done": slate_done,
            "slate_total": slate_total,
            "slate_round_no": slate_round_no,
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

    # News & blog — posted by the super admin from /manage/news/.
    from admin_panel.models import NewsPost

    # `live`, not `is_published=True` — a story queued for next Friday is
    # published and is not due. See admin_panel.models.LivePostManager.
    #
    # SIX AT A TIME, IN SLIDES (Sep 2026, client). The dashboard used to show
    # three cards and then repeat the rest as a flat list of headlines under
    # them — the same stories twice, in two shapes, taking a screenful for six
    # links. The list is gone; the cards now carry six and the deck turns
    # itself over every few seconds, so the space holds a rotating window onto
    # everything published rather than a fixed three.
    #
    # 18 rather than 9: three full turns of the deck. Enough that the rotation
    # is worth having, bounded so a busy news week does not put eighty cards
    # into every dashboard's HTML.
    news_posts = list(NewsPost.live.all()[:18])
    # Chunked here rather than in the template, because Django's template
    # language cannot slice by a computed index and the alternative is a
    # custom filter doing exactly this. A deck of one slide is the ordinary
    # case for a new organisation and the template turns the rotation off for
    # it — see the news block in dashboard.html.
    news_slides = [news_posts[i:i + 6] for i in range(0, len(news_posts), 6)]

    return render(request, "dashboard.html", {
        "cards": cards,
        "selected": selected,
        "games": games,
        # How many fixtures in the shown round have not kicked off. The panel
        # goes read-only on this rather than on the round's lockout, which is
        # only its FIRST kickoff and said "locked" while most of the round was
        # still days away.
        "open_games": open_games,
        # How much of the slate on screen is picked, as opposed to how much of
        # the round in play is — see the comment where these are counted.
        "slate_done": slate_done,
        "slate_total": slate_total,
        # The round number to put on the confirm sheet — only where the slate
        # is ONE round. See slate_round_ids.
        "slate_round_no": slate_round_no,
        # Competition filter: the series this org actually has rounds in, and
        # which are selected.
        "org_series": org_series,
        "active_slugs": active_slugs,
        # Round navigator: which round is shown, its neighbours for the arrows,
        # the full list for the dropdown, and — for a round already played —
        # how the member went.
        "round_nav": round_nav,
        "preview_round": preview_round,
        "create_url": reverse("orgs:create"),
        "news_slides": news_slides,
        # Kept so the first slide can be named in the markup without indexing
        # into the deck twice.
        "news_any": bool(news_posts),
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
@require_POST
def onboarding_seen(request):
    """The member skipped a page's walkthrough or reached its end.

    Idempotent, and deliberately not fussy about which of the two it was —
    both mean "do not show me this again", and a bell that has been rung does
    not need to know who rang it.

    `key` names the page. It is checked against the registry rather than
    trusted, because this is a public endpoint and an unchecked key would let
    anyone write arbitrary strings into the column forever. An unknown key is
    not an error to the caller — there is nothing for the browser to do about
    it, and the tour is already gone from the screen either way.
    """
    key = (request.POST.get("key") or "").strip()
    if key in TOURS:
        request.user.mark_tour_seen(key)

    # The original single flag, still stamped for anyone who finishes the
    # dashboard's tour. It is what tells this feature not to re-run that one
    # walkthrough at every member who had already seen it before the per-page
    # version shipped — see tour_for_request.
    if key in ("", "dashboard") and request.user.onboarding_seen_at is None:
        request.user.onboarding_seen_at = timezone.now()
        request.user.save(update_fields=["onboarding_seen_at"])
    return HttpResponse(status=204)


def _carry_rooms(user):
    """Imported lazily: tipping.carry pulls in orgs and tipping models, and
    accounts is imported by both."""
    from tipping.carry import rooms_for

    return rooms_for(user)


@login_required
def profile_view(request):
    pwd_form = PasswordChangeForm(request.user)
    form = ProfileForm(instance=request.user)
    security_form = SecurityForm(instance=request.user)
    carry_form = TipCarryForm(instance=request.user)
    if request.method == "POST" and "tip_carry_mode" in request.POST:
        carry_form = TipCarryForm(request.POST, instance=request.user)
        if carry_form.is_valid():
            carry_form.save()
            messages.success(request, dict(User.CARRY_CHOICES).get(
                request.user.tip_carry_mode, "Saved.",
            ) + " — saved.")
        return redirect("profile")
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
        "carry_form": carry_form,
        # The carry setting is meaningless for someone who tips in exactly one
        # room, so it is only offered to people it can actually do something
        # for — which today is 5 accounts out of 55.
        "show_carry": len(_carry_rooms(request.user)) > 1,
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

# Where a reply to the acknowledgement email lands.
#
# NOT the DEFAULT_FROM_EMAIL, which is no-reply@ — an acknowledgement that
# says "just reply to this" and then bounces is worse than no acknowledgement
# at all. This is the address the public site already publishes in its footer,
# so it is one a person actually reads. Overridable, because the address on
# the footer is the client's to change.
CONTACT_REPLY_TO = getattr(settings, "CONTACT_REPLY_TO", "hello@goodtip.com.au")


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

    # THE ENQUIRY IS SAVED. NOTHING BELOW MAY UNDO THAT.
    #
    # Both sends sit inside one try. send_template already swallows and logs a
    # failed *send*, but that is not the whole surface: rendering a template,
    # resolving a URL and opening a connection all happen before it gets that
    # far, and any of them raising would 500 a request whose Enquiry row is
    # already written — telling somebody their message did not send while it
    # sits in the inbox, and costing the lead this path exists to protect.
    #
    # Broad except on purpose. There is no failure here worth showing a
    # visitor, because there is nothing they could do about it and nothing has
    # actually been lost.
    try:
        # Everyone who can act on it. Staff rather than superusers only: the
        # point is that somebody answers, and narrowing it to one account
        # means one person's holiday is an unanswered enquiry.
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
                    # Straight to the one enquiry. Not logged in?
                    # staff_member_required sends them to the login page
                    # carrying this as ?next=, so they land here the moment
                    # they are through it.
                    "enquiry_url": site_url(
                        reverse("admin:hq_enquiry_detail", args=[enquiry.pk])
                    ),
                },
                # Reply-to the enquirer, so hitting reply in a mail client
                # works even when nobody feels like opening the admin.
                reply_to=[email],
            )

        # And an acknowledgement to the person who wrote it. Before this, an
        # enquiry produced a "sent" flag on the page and then silence until a
        # human got round to it — days, on a slow week.
        send_template(
            "enquiry_received",
            subject="We've got your message — GoodTip",
            to=[email],
            context={"enquiry": enquiry, "site_link": site_url("/")},
            # Replying to the acknowledgement reaches a person rather than a
            # no-reply address, which is the difference between an
            # acknowledgement and an autoresponder.
            reply_to=[CONTACT_REPLY_TO],
        )
    except Exception:  # noqa: BLE001 — see the note above
        logger.exception("Enquiry %s saved, but its email(s) failed", enquiry.pk)

    return redirect(f"{back}?sent=1#contact-form")
