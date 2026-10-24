"""Setting up an organisation for someone else — client, 19 Oct 2026:
"he's already talked to a school... he creates the organisation, puts in
the future admin's name and email, the system creates the org, makes them
its admin, and sends them an invitation to log in." Ian is deliberately NOT
added as a member of what he creates here — he is standing the org up on
someone else's behalf, not joining it.

Reuses Django's own password-reset flow for a brand-new account's first
password rather than a bespoke code (see sysadmin.models.AdminInvite for
that pattern and why it is heavier than this needs): there is one way to
set a GoodTip password in this codebase, not two, and it is already wired
at accounts:password_reset_confirm. An email address that already has an
account is simply told to log in — nothing about its password is touched.
"""
from django.contrib import admin, messages
from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from django.views.decorators.http import require_POST

from catalog.models import OrganisationType, State
from orgs.forms import current_signup_season
from orgs.models import Organisation, OrgMember
from orgs.services import add_member

from . import access

User = get_user_model()


def _ctx(request, title, **extra):
    ctx = admin.site.each_context(request)
    ctx.update(title=title, **extra)
    return ctx


def _guard(request):
    if not access.can(request.user, "orgs.create_with_admin"):
        raise Http404("No organisation set-up here.")


def org_admin_hub(request):
    """The set-up screen, on the pitch (rebuilt 24 Oct 2026: "that form, that
    design horrible... work on it based on what we have been doing"). Two
    tabs: the form, in two numbered parts with a live preview of what will
    happen beside it, and everything set up this way so far — each with the
    admin who runs it and whether they have signed in yet."""
    _guard(request)
    orgs = list(
        Organisation.objects.filter(created_by=request.user)
        .exclude(members__user=request.user)
        .select_related("season", "organisation_type", "state")
        .order_by("-created_at")[:50]
    )
    owners = {}
    for m in OrgMember.objects.filter(org__in=orgs, is_league_owner=True).select_related("user"):
        owners.setdefault(m.org_id, m.user)
    counts = {}
    for org_id in OrgMember.objects.filter(org__in=orgs).values_list("org_id", flat=True):
        counts[org_id] = counts.get(org_id, 0) + 1
    recent = [{"org": o, "admin": owners.get(o.pk), "members": counts.get(o.pk, 0)} for o in orgs]
    return render(request, "admin/hub/org_admin.html", _ctx(
        request, "New organisation",
        recent=recent,
        signed_in=sum(1 for r in recent if r["admin"] and r["admin"].last_login),
        people=sum(r["members"] for r in recent),
        organisation_types=OrganisationType.objects.all(),
        states=State.objects.all(),
        season=current_signup_season(),
    ))


@require_POST
def org_admin_create(request):
    _guard(request)
    here = redirect("admin:hq_org_admin")

    name = (request.POST.get("name") or "").strip()[:200]
    admin_name = (request.POST.get("admin_name") or "").strip()[:150]
    admin_email = (request.POST.get("admin_email") or "").strip()[:254]
    team_size_raw = (request.POST.get("team_size") or "").strip()
    team_size = int(team_size_raw) if team_size_raw.isdigit() else None
    org_type_id = request.POST.get("organisation_type") or ""
    state_id = request.POST.get("state") or ""

    if not name or not admin_name or not admin_email:
        messages.error(request, "Give the organisation a name, and the admin's name and email.")
        return here
    try:
        validate_email(admin_email)
    except ValidationError:
        messages.error(request, "That email address doesn't look right.")
        return here

    season = current_signup_season()
    if season is None:
        messages.error(request, "No season is set up yet — nothing can be created.")
        return here

    org = Organisation.objects.create(
        name=name, season=season, team_size=team_size,
        organisation_type_id=org_type_id if org_type_id.isdigit() else None,
        state_id=state_id if state_id.isdigit() else None,
        created_by=request.user,
    )

    admin_user, user_created = User.objects.get_or_create(
        email=admin_email, defaults={"display_name": admin_name},
    )
    if user_created:
        admin_user.set_unusable_password()
        admin_user.save(update_fields=["password"])

    member = add_member(admin_user, org, role=OrgMember.ROLE_BOTH)
    if not member.is_league_owner:
        member.is_league_owner = True
        member.save(update_fields=["is_league_owner"])

    _send_invite(org, admin_user, admin_name, new_account=user_created)

    messages.success(
        request,
        f"{org.name} is set up. {admin_name} has been emailed at {admin_email}"
        f"{' to set a password and log in' if user_created else ' — they already have an account'}.",
    )
    return here


def _send_invite(org, user, admin_name, *, new_account: bool) -> None:
    from goodtip.mail import send_template, site_url

    link = None
    if new_account:
        uid = urlsafe_base64_encode(force_bytes(user.pk))
        token = default_token_generator.make_token(user)
        link = site_url(reverse("password_reset_confirm", args=[uid, token]))
    try:
        send_template(
            "org_admin_invite",
            subject=f"You're the admin of {org.name} on GoodTip",
            to=[user.email],
            context={
                "name": admin_name or user.display_name or user.email,
                "org": org,
                "link": link,
                "login_url": site_url(reverse("accounts:login")),
            },
        )
    except Exception:  # noqa: BLE001 — the org and admin are already set up either way
        import logging

        logging.getLogger(__name__).exception(
            "Organisation %s created, but the admin invite to %s failed", org.pk, user.email,
        )
