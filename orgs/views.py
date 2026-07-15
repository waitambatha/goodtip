from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from accounts.views import JOIN_INVITER_SESSION_KEY, JOIN_SESSION_KEY
from .forms import OrgCreateForm
from .models import (
    CharityVote,
    CharityVoteOption,
    MembershipRequest,
    OrgCharitySelection,
    OrgMember,
    Organisation,
)
from .services import (
    add_member,
    approve_membership_request,
    can_lock_fundraising,
    cast_charity_ballot,
    close_charity_vote,
    decline_membership_request,
    lock_fundraising_to_self,
    nominate_manager_by_email,
    notify_charity_suggestion,
    open_charity_vote,
    record_charity_selection,
    request_to_join,
    set_member_role,
)
from .signing import make_join_token, parse_join_token


def _membership(user, org):
    return OrgMember.objects.filter(user=user, org=org).first()


def _can_manage(user, org) -> bool:
    m = _membership(user, org)
    return bool(m and m.can_manage)


def _is_member(user, org) -> bool:
    return OrgMember.objects.filter(user=user, org=org).exists()


def _invite_url(request, org) -> str:
    token = make_join_token(org.id, inviter_id=request.user.id)
    path = reverse("join_org", args=[org.id, token])
    return request.build_absolute_uri(path)


def _invitees(request, org):
    return (
        OrgMember.objects.filter(org=org, invited_by=request.user)
        .select_related("user")
        .order_by("joined_at")
    )


@login_required
def create_org_view(request):
    if request.method == "POST":
        form = OrgCreateForm(request.POST)
        if form.is_valid():
            org = form.save()
            # The creator runs and owns the league: Manager + Captain + Owner.
            OrgMember.objects.get_or_create(
                user=request.user, org=org,
                defaults={"role": OrgMember.ROLE_BOTH, "is_league_owner": True},
            )
            if form.is_vote:
                open_charity_vote(org, form.cleaned_data["vote_charities"])
                messages.success(request, f"{org.name} created — charity vote is open.")
            else:
                # Charity was picked at creation — start the timeline.
                record_charity_selection(org, org.charity, source=OrgCharitySelection.SOURCE_INITIAL)
                messages.success(request, f"{org.name} created.")
            suggested = getattr(form, "suggested_charity", None)
            if suggested is not None:
                notify_charity_suggestion(suggested, org, request.user)
                messages.info(
                    request,
                    f"{suggested.name} was sent to the GoodTip team for review.",
                )
            return redirect("orgs:created", org_id=org.id)
    else:
        form = OrgCreateForm()
    return render(request, "orgs/create.html", {"form": form})


@login_required
def org_created_view(request, org_id: int):
    org = get_object_or_404(Organisation, pk=org_id)
    if not _can_manage(request.user, org):
        return HttpResponseForbidden()
    return render(request, "orgs/created.html", {
        "org": org,
        "invite_url": _invite_url(request, org),
        "vote": org.active_charity_vote,
    })


@login_required
def org_invite_view(request, org_id: int):
    org = get_object_or_404(Organisation, pk=org_id)
    if not _can_manage(request.user, org):
        return HttpResponseForbidden()
    invitees = _invitees(request, org)
    return render(request, "orgs/invite.html", {
        "org": org,
        "invite_url": _invite_url(request, org),
        "invitees": invitees,
        "invitee_count": invitees.count(),
    })


def join_view(request, org_id: int, token: str):
    parsed = parse_join_token(token)
    if parsed is None or parsed["org_id"] != org_id:
        return render(request, "join_invalid.html", status=400)
    org = get_object_or_404(Organisation, pk=org_id)
    inviter_id = parsed["inviter_id"]
    if request.user.is_authenticated:
        already_member = _is_member(request.user, org)
        add_member(request.user, org, inviter_id=inviter_id)
        messages.success(request, f"Joined {org.name}.")
        from accounts.views import post_join_redirect

        # Only nudge the optional top-up the first time they join.
        return redirect("dashboard") if already_member else post_join_redirect(org)
    request.session[JOIN_SESSION_KEY] = org.id
    request.session[JOIN_INVITER_SESSION_KEY] = inviter_id
    signup_url = reverse("accounts:signup")
    return render(request, "join_prompt.html", {"org": org, "signup_url": signup_url})


@login_required
def charity_vote_view(request, org_id: int):
    org = get_object_or_404(Organisation, pk=org_id)
    if not _is_member(request.user, org):
        return HttpResponseForbidden()
    is_admin = _can_manage(request.user, org)
    # Charity Partner Workflow (categories doc): partners can lock fundraising
    # to themselves; non-partner charity orgs see the become-a-partner CTA.
    already_self = bool(org.charity_id and org.charity.name.lower() == org.name.lower())
    partner_ctx = {
        "can_lock_fundraising": is_admin and can_lock_fundraising(org) and not already_self,
        "show_partner_cta": (
            is_admin and org.group_type_id
            and org.group_type.is_charity_type and not org.is_charity_partner
        ),
    }
    vote = org.charity_votes.first()
    if vote is None:
        return render(request, "orgs/charity_vote.html", {"org": org, "vote": None, **partner_ctx})

    options = list(vote.options.select_related("charity"))
    my_ballot = vote.ballots.filter(user=request.user).first()
    results = None
    if not vote.is_open:
        # Tallies are revealed only once the vote has closed (blind vote).
        results = list(
            vote.options.select_related("charity")
            .annotate(n=Count("ballots"))
            .order_by("-n", "charity__name")
        )
    return render(request, "orgs/charity_vote.html", {
        "org": org,
        "vote": vote,
        "options": options,
        "my_option_id": my_ballot.option_id if my_ballot else None,
        "ballot_count": vote.ballots.count(),
        "results": results,
        "is_admin": is_admin,
        **partner_ctx,
    })


@login_required
@require_POST
def cast_charity_vote(request, org_id: int):
    org = get_object_or_404(Organisation, pk=org_id)
    if not _is_member(request.user, org):
        return HttpResponseForbidden()
    vote = org.charity_votes.first()
    if vote is None:
        return redirect("orgs:charity_vote", org_id=org.id)
    option = get_object_or_404(CharityVoteOption, pk=request.POST.get("option"), vote=vote)
    try:
        cast_charity_ballot(user=request.user, vote=vote, option=option)
        messages.success(request, "Your vote is in.")
    except ValueError as e:
        messages.error(request, str(e))
    return redirect("orgs:charity_vote", org_id=org.id)


@login_required
@require_POST
def close_charity_vote_view(request, org_id: int):
    org = get_object_or_404(Organisation, pk=org_id)
    if not _can_manage(request.user, org):
        return HttpResponseForbidden()
    vote = org.charity_votes.first()
    if vote is not None and vote.is_open:
        winner = close_charity_vote(vote)
        if winner:
            messages.success(request, f"Vote closed — {winner.name} won.")
        else:
            messages.error(request, "Vote closed, but no ballots were cast.")
    return redirect("orgs:charity_vote", org_id=org.id)


@login_required
@require_POST
def lock_fundraising_view(request, org_id: int):
    """Charity Partner Workflow: a confirmed partner charity locks fundraising
    to itself — manager-only, and only once GoodTip staff set the partner flag.
    """
    org = get_object_or_404(Organisation, pk=org_id)
    if not _can_manage(request.user, org):
        return HttpResponseForbidden()
    try:
        lock_fundraising_to_self(org)
        messages.success(request, f"Fundraising locked to {org.name} — no vote needed.")
    except ValueError as e:
        messages.error(request, str(e))
    return redirect("orgs:charity_vote", org_id=org.id)


@login_required
@require_POST
def request_join_view(request, org_id: int):
    """Ask to join an org found via search (org-structure §2, client
    amendment: joining goes through the org's admin, not straight in)."""
    org = get_object_or_404(Organisation, pk=org_id)
    try:
        request_to_join(request.user, org)
        messages.success(
            request,
            f"Request sent — {org.name}'s admin will review it.",
        )
    except ValueError as e:
        messages.info(request, str(e))
    next_url = request.POST.get("next", "")
    if not url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
        next_url = ""
    return redirect(next_url or "dashboard")


@login_required
def members_view(request, org_id: int):
    org = get_object_or_404(Organisation, pk=org_id)
    me = _membership(request.user, org)
    if me is None or not me.can_manage:
        return HttpResponseForbidden()

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "set_role":
            member = get_object_or_404(OrgMember, pk=request.POST.get("member_id"), org=org)
            try:
                set_member_role(member, request.POST.get("role"))
                messages.success(request, f"{member.user.display_name} is now {member.get_role_display()}.")
            except ValueError as e:
                messages.error(request, str(e))
        elif action == "nominate_manager":
            # Per the deck, nominating a Team Manager is a League Owner action.
            if not me.is_league_owner:
                return HttpResponseForbidden()
            email = request.POST.get("email", "")
            member = nominate_manager_by_email(org, email)
            if member:
                messages.success(request, f"{member.user.display_name} is now a Team Manager.")
            else:
                messages.error(request, "No member in this league has that email yet.")
        elif action in ("approve_request", "decline_request"):
            join_req = get_object_or_404(
                MembershipRequest, pk=request.POST.get("request_id"), org=org,
            )
            try:
                if action == "approve_request":
                    approve_membership_request(join_req, by_user=request.user)
                    messages.success(request, f"{join_req.user.display_name} is now a member.")
                else:
                    decline_membership_request(join_req, by_user=request.user)
                    messages.info(request, f"{join_req.user.display_name}'s request was declined.")
            except ValueError as e:
                messages.error(request, str(e))
        return redirect("orgs:members", org_id=org.id)

    members = (
        OrgMember.objects.filter(org=org)
        .select_related("user")
        .order_by("-is_league_owner", "role", "joined_at")
    )
    pending_requests = (
        org.membership_requests.filter(status=MembershipRequest.STATUS_PENDING)
        .select_related("user")
        .order_by("created_at")
    )
    return render(request, "orgs/members.html", {
        "org": org,
        "members": members,
        "pending_requests": pending_requests,
        "role_choices": OrgMember.ROLE_CHOICES,
        "is_owner": me.is_league_owner,
    })
