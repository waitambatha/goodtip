from pathlib import Path
from uuid import uuid4

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import models
from django.db.models import Count, Q
from django.http import HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST
from django.urls import reverse
from django.core.files.storage import default_storage
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.timesince import timesince
from django.views.decorators.http import require_POST

from accounts.views import JOIN_INVITER_SESSION_KEY, JOIN_SESSION_KEY
from catalog.models import GroupType
from .forms import InviteByEmailForm, OrgCreateForm, fed_competitions
from .notifications import send_org_invites
from .models import (
    CharityVote,
    CharityVoteOption,
    Group,
    GroupMember,
    MembershipRequest,
    Notification,
    OrgCharitySelection,
    OrgDraft,
    OrgMember,
    Organisation,
)
from .services import (
    active_work_verification,
    add_member,
    apply_verification_to_org,
    approve_membership_request,
    resend_work_email_code,
    start_work_email_verification,
    can_lock_fundraising,
    cast_charity_ballot,
    close_charity_vote,
    approve_group,
    close_due_elections,
    create_charity_election,
    create_group,
    decline_group,
    decline_membership_request,
    groups_for,
    join_group,
    leave_group,
    demote_child_org_admin,
    lock_fundraising_to_self,
    open_due_elections,
    reassign_child_org_admin,
    nominate_manager_by_email,
    notify_charity_suggestion,
    open_charity_vote,
    record_charity_selection,
    request_to_join,
    schedule_charity_election,
    set_election_close_time,
    set_member_role,
)
from . import context as ctx
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


def _parent_for(user, pid):
    """Resolve a department's parent org, or None.

    Three things have to hold, and only the first two used to be checked:
    the id has to be real, the org has to be top-level (§3 — two levels, so a
    department cannot have departments), and THE USER HAS TO RUN IT.

    That last one was missing. `?parent=` was read straight off the query
    string and trusted, so any signed-in user could append the id of any
    organisation on the platform and create a department inside it — becoming
    its admin, appearing in that org's Manage screen, and rolling their totals
    into that org's national figure. Nothing in the wizard asked whether they
    had ever heard of the place. Departments are the feature that makes that
    reachable from the UI, so the check lands with it.
    """
    if not pid or not str(pid).isdigit():
        return None
    org = Organisation.objects.filter(parent__isnull=True, pk=pid).first()
    if org is None or not _can_manage(user, org):
        return None
    return org


def _requested_parent(request):
    """The top-level org a department is being created under (§2's second
    path), from ?parent= on GET or the posted form value on re-renders. None
    for the standalone default case (§1), for an invalid or child id, or when
    the user does not manage the org they named."""
    return _parent_for(
        request.user, request.POST.get("parent") or request.GET.get("parent")
    )


# The create form, cut into steps. Each entry is (number, label, field names).
# The field lists are the ONLY thing that decides which errors surface on which
# step — validation itself stays entirely in OrgCreateForm, so there is one
# source of truth for the rules and the wizard just decides what to show when.
# (number, label, sub-label, fields). The sub-label is what the rail shows under
# the step name — "Your organisation" alone does not tell you whether the step
# wants a name or a whole charity policy, and the rail is the only place someone
# can see what is still ahead of them before committing to start.
WIZARD_STEPS = [
    (1, "Your organisation", "Basic details",
     ["name", "organisation_type", "sub_categories", "informal_label", "state"]),
    # Step 2 owns no form fields: it is the work-email check, and it is held in
    # its own table rather than in the draft's JSON because a hashed code with
    # an expiry, an attempt count and a send count is not a form value. It sits
    # here, straight after the name, so nobody fills in three more screens
    # before finding out they cannot prove the organisation is theirs.
    (2, "Verify", "Prove it's yours", []),
    # Asked straight after verification, and before anything about the season
    # or the charity: whether this organisation needs sub-groups is a shape
    # question, like step 1's type, not a setting to bury on a settings page
    # someone has to go looking for after the fact.
    (3, "Groups", "One ladder, or several",
     ["groups_enabled"]),
    (4, "The tipping", "Scoring & rules",
     ["competitions", "season", "team_size", "finals_only"]),
    (5, "The charity", "Choose a cause",
     ["charity_method", "charity", "new_charity_name",
      "new_charity_url", "vote_charities", "vote_opens_at", "vote_closes_at"]),
    (6, "Review", "Check & create", []),
]
VERIFY_STEP = 2

# Types that have a work domain to prove. A book club or a cycling crew has no
# organisational domain and never will, so requiring one would block exactly the
# groups the Informal type exists for. They skip the step; everyone whose whole
# claim is "I represent this employer" does not.
VERIFY_REQUIRED_SLUGS = {"business", "education", "charities"}
LAST_STEP = WIZARD_STEPS[-1][0]
# Fields the browser posts as a list rather than a single value.
WIZARD_MULTI_FIELDS = {"sub_categories", "competitions", "vote_charities"}
# Unchecked checkboxes post nothing at all, so "absent" has to mean False for
# these rather than "leave whatever was there before".
WIZARD_BOOLEAN_FIELDS = {"finals_only"}


def _wizard_fields(step: int) -> list:
    return next((f for n, _, _sub, f in WIZARD_STEPS if n == step), [])


def _verification_required(draft) -> bool:
    """Does this draft have to prove a work domain before it can go on?

    Read off the type chosen at step one. Unknown or unset counts as NOT
    required: a draft that has not reached the question yet must not be
    blocked by it, and the gate is re-evaluated on every submit, so choosing
    Business later still brings the requirement with it.
    """
    from catalog.models import OrganisationType

    gt_id = draft.data.get("organisation_type")
    if not gt_id or not str(gt_id).isdigit():
        return False
    slug = OrganisationType.objects.filter(pk=gt_id).values_list("slug", flat=True).first()
    return slug in VERIFY_REQUIRED_SLUGS


LOGO_MAX_BYTES = 5 * 1024 * 1024
LOGO_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg"}


def _absorb_step(draft, post, step: int, files=None) -> None:
    """Fold one step's posted values into the draft."""
    for name in _wizard_fields(step):
        if name in WIZARD_MULTI_FIELDS:
            draft.data[name] = post.getlist(name)
        elif name in WIZARD_BOOLEAN_FIELDS:
            draft.data[name] = bool(post.get(name))
        else:
            draft.data[name] = post.get(name, "")

    # The draft is a JSON column, so an uploaded file cannot be stored in it.
    # Write the upload to storage straight away and keep only its path: the
    # wizard can span days and machines, so neither holding the bytes in the
    # session nor asking for the file again at the final step would survive the
    # way this form is actually used.
    upload = (files or {}).get("logo")
    if upload:
        suffix = Path(upload.name).suffix.lower()
        if suffix in LOGO_SUFFIXES and upload.size <= LOGO_MAX_BYTES:
            draft.data["logo"] = default_storage.save(
                f"org_logos/drafts/{uuid4().hex}{suffix}", upload
            )
        else:
            draft.data["logo_error"] = (
                "That file is over 5 MB." if upload.size > LOGO_MAX_BYTES
                else "Use a JPG, PNG, WEBP, GIF or SVG."
            )
            return
    draft.data.pop("logo_error", None)


def _step_errors(form, step: int) -> dict:
    """Only this step's errors. The form validates everything every time, so
    without this filter step one would refuse to advance over a missing charity."""
    fields = set(_wizard_fields(step))
    return {name: errs for name, errs in form.errors.items() if name in fields}


@login_required
def create_org_view(request):
    """Create a group, one step at a time, resumable.

    A single long form was a wall: people abandoned it part-way to go and ask
    which charity the office wanted, and lost everything. Progress now lands in
    an OrgDraft row after every step, so leaving and coming back — even on
    another machine — picks up where they left off.
    """
    draft, _ = OrgDraft.objects.get_or_create(user=request.user)
    # Keep the raw value, valid or not: the form's queryset is what rejects a
    # child-as-parent (§3, two levels only), and dropping a bad id here would
    # quietly create a top-level group instead of refusing.
    raw_parent = request.POST.get("parent") or request.GET.get("parent")
    if raw_parent:
        draft.data["parent"] = raw_parent
    elif request.method == "GET":
        # Arriving here with no parent named means "create an organisation",
        # and it has to actually mean that.
        #
        # There is one draft per user and it was shared by both flows, but the
        # parent was only ever written and never cleared. So opening the
        # department form even once pinned that draft to a parent for good:
        # every later visit to /orgs/create/ re-resolved it and rendered a page
        # headed "New department under <that org>", with a Department name
        # field, while the person had asked to create an organisation. The only
        # escape was Start again, which is not something anyone would guess.
        #
        # An in-flight department keeps its parent regardless: every POST
        # carries the hidden field, and the redirect between steps now carries
        # ?parent= (see _create_redirect).
        draft.data.pop("parent", None)
    parent_org = _requested_parent(request) or _parent_from_draft(draft)

    step = draft.step or 1
    errors, duplicates = {}, None

    if request.method == "POST":
        action = request.POST.get("action", "next")
        step = int(request.POST.get("step") or step)

        if action == "restart":
            draft.data, draft.step = {}, 1
            draft.save()
            messages.info(request, "Started again — nothing was created.")
            return _create_redirect(parent_org)

        # Absorb first, so going back never loses what was just typed.
        if step != LAST_STEP:
            _absorb_step(draft, request.POST, step, request.FILES)

        if action == "back":
            # The verify step has sub-states of its own — nothing started, a
            # code in flight, verified — that "back" needs to unwind one at a
            # time. Falling straight through to "step - 1" from the code-entry
            # sub-state skipped past the role/domain/email form entirely and
            # landed a step early, on step 1: the same one-press-too-far a
            # "change_email" click already knew to avoid.
            if step == VERIFY_STEP:
                row = active_work_verification(request.user)
                if row and not row.is_verified:
                    row.delete()
                    return _create_redirect(parent_org)
            draft.step = max(1, step - 1)
            draft.save()
            return _create_redirect(parent_org)

        if action == "logo_clear":
            _absorb_step(draft, request.POST, step, None)
            draft.data.pop("logo", None)
            draft.data.pop("logo_error", None)
            draft.save()
            return _create_redirect(parent_org)

        if action == "save":
            # Save without advancing. Until this existed, "save" and "next" were
            # the same button: the only way to keep what you had typed was to
            # complete the step and move on, so anyone who wanted to stop
            # half-way had to guess whether their work was safe.
            form = _draft_form(draft, parent_org, bound=True)
            errors = _step_errors(form, step)
            if not errors:
                draft.save()
                messages.success(
                    request, "Draft saved — pick it up here whenever you like."
                )
                return _create_redirect(parent_org)
            # Fall through to the render below so the offending fields are named
            # and marked, rather than reporting a bare "could not save".
            messages.error(request, "Fill these in first, then we can save your draft.")
            return _render_wizard(
                request, draft, form, step, parent_org, errors=errors,
            )

        # ---- the verify step ----------------------------------------------
        # Its own actions, and its own gate. Handled before the form path
        # because it owns no form fields at all: the state lives in
        # WorkEmailVerification, which is the only place a hashed code with an
        # expiry and an attempt count can honestly live.
        if step == VERIFY_STEP:
            row = active_work_verification(request.user)

            if action == "send_code":
                try:
                    row = start_work_email_verification(
                        user=request.user,
                        role=request.POST.get("role", ""),
                        domain=request.POST.get("domain", ""),
                        email=request.POST.get("work_email", ""),
                    )
                    messages.success(request, f"Code sent to {row.email}. It expires in 15 minutes.")
                    return _create_redirect(parent_org)
                except ValueError as e:
                    # Render rather than redirect, so the message can sit under
                    # the field that caused it AND the other two keep what was
                    # typed. A redirect would empty the form and leave one
                    # anonymous banner above three blank boxes.
                    field = getattr(e, "field", None)
                    return _render_wizard(
                        request, draft, _draft_form(draft, parent_org, bound=False),
                        step, parent_org,
                        verify_error={"field": field, "message": str(e)},
                        verify_values={
                            "role": request.POST.get("role", ""),
                            "domain": request.POST.get("domain", ""),
                            "work_email": request.POST.get("work_email", ""),
                        },
                    )

            if action == "resend_code":
                try:
                    resend_work_email_code(row) if row else None
                    messages.success(request, "Another code is on its way.")
                except ValueError as e:
                    messages.error(request, str(e))
                return _create_redirect(parent_org)

            if action == "change_email":
                # Abandon the current check so the form comes back blank. The
                # row is unverified by definition here, so nothing is lost.
                if row and not row.is_verified:
                    row.delete()
                return _create_redirect(parent_org)

            if action == "verify_code":
                if row is None:
                    messages.error(request, "Send yourself a code first.")
                elif row.is_verified:
                    pass                                  # already through
                elif row.verify(request.POST.get("code", "")):
                    # Stays on step 2 rather than jumping straight to step 3.
                    # It used to advance immediately, which meant a correct
                    # code was never actually SEEN succeeding — the code-entry
                    # veil just vanished into the next step's card with no
                    # beat in between. Landing back here renders the
                    # "Verified" tick (create.html's verify-done block, which
                    # a script advances on its own after a moment — see
                    # app_js), so the moment of getting it right is something
                    # that happens on screen before the wizard moves on.
                    messages.success(request, f"{row.domain} verified. Nice one.")
                    return _create_redirect(parent_org)
                elif row.attempts >= row.MAX_ATTEMPTS:
                    messages.error(
                        request,
                        "That's too many wrong codes. Send yourself a fresh one to try again.",
                    )
                else:
                    left = row.MAX_ATTEMPTS - row.attempts
                    messages.error(
                        request,
                        f"That code isn't right. {left} attempt{'s' if left != 1 else ''} left, "
                        "or send yourself a new one.",
                    )
                return _create_redirect(parent_org)

            if action == "next":
                if _verification_required(draft) and not (row and row.is_verified):
                    messages.error(
                        request,
                        "Send yourself a code and enter it before you go on — that "
                        "is what proves the domain is yours.",
                    )
                    return _create_redirect(parent_org)
                draft.step = step + 1
                draft.save()
                return _create_redirect(parent_org)

            # No recognised action on the verify step means the submitter's
            # name never arrived. Falling through from here would reach the
            # generic form path below, and step 2 owns no form fields — so it
            # would find nothing to complain about and ADVANCE, carrying an
            # unverified draft past the one gate that exists to stop it.
            #
            # That is not hypothetical: a submit handler that disabled the
            # pressed button stripped `action` from every POST this wizard
            # made. Fixed at source in gt-busy.js, but the step must not be
            # one dropped field away from skipping its own check.
            return _create_redirect(parent_org)

        form = _draft_form(draft, parent_org, bound=True)
        errors = _step_errors(form, step)
        if not errors and step < LAST_STEP:
            draft.step = step + 1
            draft.save()
            return _create_redirect(parent_org)

        # The review step owns no fields, so nothing would surface a problem
        # carried over from an earlier one. Show the lot rather than a button
        # that silently does nothing.
        if step == LAST_STEP and not form.is_valid():
            errors = dict(form.errors)

        if not errors and step == LAST_STEP:
            # §4 Stage 2: same-named org(s) exist → one explicit confirmation
            # before creating anyway. Friction, not prevention: the resubmit
            # carries duplicate_confirmed and sails through.
            duplicates = Organisation.objects.filter(
                name__iexact=form.cleaned_data["name"].strip(),
            ).select_related("parent", "state")
            if duplicates.exists() and request.POST.get("duplicate_confirmed") != "1":
                return _render_wizard(
                    request, draft, form, step, parent_org, duplicates=duplicates,
                )
            org = form.save()
            org.created_by = request.user
            org.save(update_fields=["created_by"])
            # Carry the work-email check onto the org it was done for. Belt and
            # braces on the gate above: an org whose type does not require
            # verification still records one if its creator did it anyway.
            apply_verification_to_org(org, active_work_verification(request.user))
            # Anyone who talked this person into starting a comp gets added to
            # it automatically — see accounts.boss for the three-step flow.
            from accounts.boss import complete_boss_invites

            brought_in = complete_boss_invites(org, request.user)
            if brought_in:
                messages.info(
                    request,
                    f"{brought_in} colleague{'s' if brought_in != 1 else ''} who asked you "
                    "to start this comp have been added automatically.",
                )
            # The logo was written to storage during step one; attach the path
            # now that there is a row to attach it to.
            if draft.data.get("logo"):
                org.logo.name = draft.data["logo"]
                org.save(update_fields=["logo"])
            # The creator runs and owns the league: Manager + Captain + Owner.
            OrgMember.objects.get_or_create(
                user=request.user, org=org,
                defaults={"role": OrgMember.ROLE_BOTH, "is_league_owner": True},
            )
            if form.is_vote:
                # The election is created in draft — the admin schedules it
                # (or starts it now) from the dashboard, and members are
                # notified by email + in-app when it actually opens.
                vote = create_charity_election(org, form.cleaned_data["vote_charities"])
                opens = form.cleaned_data.get("vote_opens_at")
                if opens:
                    # Scheduling here rather than leaving it in draft: the
                    # wizard used to end with "set up the election when you're
                    # ready", and an admin who never came back left their
                    # members with a vote that silently never opened.
                    schedule_charity_election(
                        vote, when=opens, close_at=form.cleaned_data.get("vote_closes_at"),
                    )
                    if vote.is_open:
                        messages.success(request, f"{org.name} created, and the charity vote is open.")
                    else:
                        stamp = timezone.localtime(opens)
                        messages.success(
                            request,
                            f"{org.name} created. The charity vote opens {stamp:%-d %b at %-I:%M %p}.",
                        )
                else:
                    messages.success(
                        request,
                        f"{org.name} created — set up the charity election when you're ready.",
                    )
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
            # The draft has served its purpose.
            draft.delete()
            return redirect("orgs:created", org_id=org.id)
    else:
        draft.save()
        form = _draft_form(draft, parent_org, bound=False)

    return _render_wizard(
        request, draft, form, step, parent_org, errors=errors, duplicates=duplicates,
    )


def _create_redirect(parent_org):
    """Back to the wizard, keeping a department flow on its parent.

    A plain redirect drops the query string, which is why the parent used to be
    fished back out of the draft — and why it could never be cleared.
    """
    url = reverse("orgs:create")
    return redirect(f"{url}?parent={parent_org.id}" if parent_org else url)


def _parent_from_draft(draft):
    """Same three checks as _requested_parent, re-run every time.

    A draft survives for as long as the user leaves it, so the permission it
    was started under is not evidence of the permission they have now — an
    admin who is removed from the parent between opening the wizard and
    finishing it must not still be creating departments inside it.
    """
    return _parent_for(draft.user, draft.data.get("parent"))


def _draft_form(draft, parent_org, *, bound: bool) -> OrgCreateForm:
    """The one create form, fed from the draft.

    Bound when we're checking a step (so errors exist to filter), unbound with
    the same values as `initial` when we're only drawing the page — an unbound
    form shows the values back without painting every not-yet-visited field red.
    """
    data = dict(draft.data)
    return OrgCreateForm(data) if bound else OrgCreateForm(initial=data)


def _render_wizard(request, draft, form, step, parent_org, *, errors=None, duplicates=None,
                   verify_error=None, verify_values=None):
    steps = [
        {"n": n, "label": label, "sub": sub,
         "done": n < draft.step, "current": n == step,
         # The connector is drawn by the step to its left, so the last one has
         # none — otherwise the rail ends in a line pointing at nothing.
         "connector": n < len(WIZARD_STEPS)}
        for n, label, sub, _ in WIZARD_STEPS
    ]
    return render(request, "orgs/create.html", {
        "form": form,
        "parent_org": parent_org,
        "draft": draft,
        "step": step,
        "steps": steps,
        "last_step": LAST_STEP,
        "step_errors": errors or {},
        "duplicates": duplicates,
        "summary": _draft_summary(form, draft) if step == LAST_STEP else None,
        "draft_saved_label": _saved_label(draft),
        # Verify step state. Cheap enough to always provide: the template only
        # reads it on step 2, and computing it conditionally would mean the
        # "verified" tick could not show on the review step.
        "verification": active_work_verification(request.user),
        "verify_required": _verification_required(draft),
        "verify_step": VERIFY_STEP,
        # {"field": "domain"|"work_email"|"role"|None, "message": str}
        "verify_error": verify_error,
        "verify_values": verify_values or {},
        # Built here rather than as {{ MEDIA_URL }}{{ path }} in the template:
        # that only works if the media context processor happens to be enabled,
        # and storage backends are free to serve from somewhere else entirely.
        "draft_logo_url": (
            default_storage.url(draft.data["logo"]) if draft.data.get("logo") else ""
        ),
        # The competitions queryset (see OrgCreateForm.__init__) is already
        # filtered down to one season, so any row in it names the season the
        # step-4 eyebrow should show — no separate lookup, no separate
        # "which season is current" logic living in two places.
        "current_season": (
            getattr(form.fields["competitions"].queryset.first(), "season", None)
            if step == 4 else None
        ),
    })


def _saved_label(draft):
    """Human wording for the draft chip.

    timesince() renders anything under a minute as "0 minutes", and "saved 0
    minutes ago" beside a form someone just typed into reads as a bug. Under a
    minute is the common case here, so it gets its own words.
    """
    if not draft.updated_at:
        return "Just now"
    seconds = (timezone.now() - draft.updated_at).total_seconds()
    if seconds < 60:
        return "Just now"
    return f"{timesince(draft.updated_at)} ago"


def _draft_summary(form, draft) -> list:
    """Plain-language read-back of the answers, for the review step."""
    def label_for(field, pk):
        if not pk:
            return ""
        obj = form.fields[field].queryset.filter(pk=pk).first()
        return str(obj) if obj else ""

    def labels_for(field, pks):
        qs = form.fields[field].queryset.filter(pk__in=pks or [])
        return ", ".join(str(o) for o in qs)

    d = draft.data
    charity = (
        "The group votes on it"
        if d.get("charity_method") == "vote"
        else (label_for("charity", d.get("charity")) or d.get("new_charity_name") or "")
    )
    rows = [
        ("Group name", d.get("name", "")),
        ("Type", label_for("organisation_type", d.get("organisation_type"))),
        ("Sub-category", labels_for("sub_categories", d.get("sub_categories"))),
        ("Described as", d.get("informal_label", "")),
        ("State", label_for("state", d.get("state")) or "National"),
        ("Competitions", labels_for("competitions", d.get("competitions"))),
        ("Season", label_for("season", d.get("season"))),
        ("Expected size", d.get("team_size", "")),
        ("Finals only", "Yes" if d.get("finals_only") else ""),
        (
            "Groups",
            "On — teams can start their own group" if d.get("groups_enabled") == "yes"
            else "Off for now",
        ),
        ("Charity", charity),
        ("On the ballot", labels_for("vote_charities", d.get("vote_charities"))),
    ]
    return [{"label": k, "value": v} for k, v in rows if v]


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
    invite_url = _invite_url(request, org)
    email_form = InviteByEmailForm()

    if request.method == "POST":
        email_form = InviteByEmailForm(request.POST)
        if email_form.is_valid():
            emails = email_form.cleaned_data["emails"]
            # Anyone already in the group gets nothing — a "come join us" mail
            # to someone who is already a member reads as a system that isn't
            # paying attention.
            existing = set(
                OrgMember.objects.filter(org=org, user__email__in=emails)
                .values_list("user__email", flat=True)
            )
            existing_lower = {e.lower() for e in existing}
            to_send = [e for e in emails if e.lower() not in existing_lower]

            sent = 0
            if to_send:
                sent = send_org_invites(
                    org, request.user, to_send, invite_url,
                    message=email_form.cleaned_data.get("message", ""),
                )
            if sent:
                messages.success(
                    request,
                    f"Invitation sent to {sent} "
                    f"{'person' if sent == 1 else 'people'}.",
                )
            elif to_send:
                messages.error(
                    request,
                    "We couldn't send those invitations just now. "
                    "Copy the link and share it directly, or try again shortly.",
                )
            skipped = len(emails) - len(to_send)
            if skipped:
                messages.info(
                    request,
                    f"{skipped} of those {'is' if skipped == 1 else 'are'} "
                    f"already in {org.name}, so we skipped them.",
                )
            return redirect("orgs:invite", org_id=org.id)

    invitees = _invitees(request, org)
    return render(request, "orgs/invite.html", {
        "org": org,
        "invite_url": invite_url,
        "invitees": invitees,
        "invitee_count": invitees.count(),
        "email_form": email_form,
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
            is_admin and org.organisation_type_id
            and org.organisation_type.is_charity_type and not org.is_charity_partner
        ),
    }
    # A scheduled election whose time has come opens on first visit — and an
    # open one whose end time has passed closes and reveals its results.
    open_due_elections(orgs=[org])
    close_due_elections(orgs=[org])
    vote = org.charity_votes.first()
    if vote is None:
        return render(request, "orgs/charity_vote.html", {"org": org, "vote": None, **partner_ctx})
    vote.refresh_from_db()

    options = list(vote.options.select_related("charity"))
    my_ballot = vote.ballots.filter(user=request.user).first()
    # Everyone in the org may vote — that's the eligible pool. While the vote
    # is open only the *counts* are shown: never who has voted, never tallies.
    eligible_count = org.members.count()
    ballot_count = vote.ballots.count()
    turnout_pct = round(ballot_count * 100 / eligible_count) if eligible_count else 0
    results = None
    stats = None
    if vote.status == CharityVote.STATUS_CLOSED:
        # Tallies are revealed only once the vote has closed (blind vote).
        results = list(
            vote.options.select_related("charity")
            .annotate(n=Count("ballots"))
            .order_by("-n", "charity__name")
        )
        total = sum(o.n for o in results)
        top_n = results[0].n if results else 0
        for o in results:
            o.pct = round(o.n * 100 / total) if total else 0
            o.bar = round(o.n * 100 / top_n) if top_n else 0
        winner_n = next((o.n for o in results if o.charity_id == vote.winning_charity_id), 0)
        from django.db.models.functions import TruncDate

        by_day = (
            vote.ballots.annotate(d=TruncDate("cast_at"))
            .values("d").annotate(n=Count("id")).order_by("d")
        )
        max_day = max((r["n"] for r in by_day), default=0)
        stats = {
            "eligible": eligible_count,
            "ballots": total,
            "abstained": max(eligible_count - total, 0),
            "turnout_pct": round(total * 100 / eligible_count) if eligible_count else 0,
            "winner_share": round(winner_n * 100 / total) if total else 0,
            "timeline": [
                {"day": r["d"], "n": r["n"], "h": round(r["n"] * 100 / max_day) if max_day else 0}
                for r in by_day
            ],
        }
        stats["turnout_deg"] = round(stats["turnout_pct"] * 3.6, 1)
        stats["winner_deg"] = round(stats["winner_share"] * 3.6, 1)
    return render(request, "orgs/charity_vote.html", {
        "org": org,
        "vote": vote,
        "options": options,
        "my_option_id": my_ballot.option_id if my_ballot else None,
        "ballot_count": ballot_count,
        "eligible_count": eligible_count,
        "turnout_pct": turnout_pct,
        "results": results,
        "stats": stats,
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
def election_close_time_view(request, org_id: int):
    """Manager sets, moves, or clears the automatic end time of an open vote."""
    org = get_object_or_404(Organisation, pk=org_id)
    if not _can_manage(request.user, org):
        return HttpResponseForbidden()
    vote = org.charity_votes.first()
    if vote is None or not vote.is_open:
        return redirect("orgs:charity_vote", org_id=org.id)
    from django.utils import timezone as tz
    from django.utils.dateparse import parse_datetime

    raw = (request.POST.get("scheduled_close_at") or "").strip()
    close_at = parse_datetime(raw) if raw else None
    if raw and close_at is None:
        messages.error(request, "That end time didn't make sense — try again.")
        return redirect("orgs:charity_vote", org_id=org.id)
    if close_at is not None and tz.is_naive(close_at):
        close_at = tz.make_aware(close_at, tz.get_current_timezone())
    try:
        set_election_close_time(vote, close_at)
        if close_at:
            messages.success(request, f"Voting now ends {close_at.strftime('%A %-d %B, %-I:%M %p')} — it'll close and reveal results automatically.")
        else:
            messages.success(request, "Automatic end time removed — the vote stays open until you close it.")
    except ValueError as e:
        messages.error(request, str(e))
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


# Minimum characters before search matches anything — also the typeahead
# trigger threshold in the search/create templates.
SEARCH_MIN_CHARS = 2


def _search_orgs(q: str, *, limit: int = 10):
    """Close-match org lookup for search and duplicate detection (§2, §4)."""
    return (
        Organisation.objects.filter(name__icontains=q)
        .select_related("parent", "organisation_type", "state")
        .prefetch_related("sub_categories")
        .annotate(_member_count=Count("members", distinct=True))
        .order_by("name")[:limit]
    )


def _search_payload(user, q: str) -> list[dict]:
    """JSON-safe rows for the typeahead: who the org is, how big, and where
    THIS user stands with it (member / request pending / free to ask)."""
    member_ids = set(user.memberships.values_list("org_id", flat=True))
    pending_ids = set(
        user.membership_requests.filter(
            status=MembershipRequest.STATUS_PENDING,
        ).values_list("org_id", flat=True)
    )
    return [
        {
            "id": org.id,
            "name": org.name,
            "parent": org.parent.name if org.parent_id else None,
            "category": org.category_label,
            "state": org.state.code if org.state_id else None,
            "members": org._member_count,
            "is_member": org.id in member_ids,
            "pending": org.id in pending_ids,
            # §2's second path: create a new child org under this match's
            # top-level parent (the match itself when it IS top-level). Only
            # offered when the viewer actually runs that root — _parent_for()
            # drops an unmanaged parent silently, so the button must not
            # promise something it will not deliver.
            "root_id": org.root.id,
            "root_name": org.root.name,
            "can_manage_root": _can_manage(user, org.root),
        }
        for org in _search_orgs(q)
    ]


# A tile icon per group, so the browse list reads as a set of things rather
# than a wall of text. Keyed on the org id rather than picked at random, so a
# group keeps the same tile on every visit — a list that reshuffled its own
# icons each load would read as a rendering bug.
BROWSE_ICONS = [
    "ic-trophy", "ic-people", "ic-leaf", "ic-chart", "ic-cloud-sync",
    "ic-calendar", "ic-target", "ic-spark", "ic-globe", "ic-flag",
    "ic-heart", "ic-cap", "ic-flame", "ic-ribbon", "ic-users",
]

# Order of the browse list. "Most members" leads because the biggest groups are
# the ones a newcomer is most likely to be looking for.
BROWSE_SORTS = [
    ("members", "Most members"),
    ("name", "A–Z"),
    ("new", "Newest"),
]


@login_required
def org_search_view(request):
    """Find-your-group page (org-structure §2): each match offers BOTH paths —
    request to join it, or create a new child org under its top-level parent.

    Besides the typeahead, the page lists every group — parents with their
    child groups beneath them — so people can browse instead of guessing
    the exact name.
    """
    q = (request.GET.get("q") or "").strip()
    results = _search_payload(request.user, q) if len(q) >= SEARCH_MIN_CHARS else []

    member_ids = set(request.user.memberships.values_list("org_id", flat=True))
    pending_ids = set(
        request.user.membership_requests.filter(
            status=MembershipRequest.STATUS_PENDING,
        ).values_list("org_id", flat=True)
    )
    all_orgs = list(
        Organisation.objects.select_related("parent", "organisation_type", "state")
        .annotate(member_count=Count("members", distinct=True))
        .order_by("name")
    )

    def _row(org):
        return {
            "org": org,
            "is_member": org.id in member_ids,
            "pending": org.id in pending_ids,
            "icon": BROWSE_ICONS[org.id % len(BROWSE_ICONS)],
            # Every row here is top-level (children are attached separately,
            # below), so managing `org` IS managing the root — the same check
            # _parent_for() runs before it lets a child actually get created.
            "can_manage": _can_manage(request.user, org),
        }

    children = {}
    for org in all_orgs:
        if org.parent_id:
            # keyed by the top-level root so deeper descendants still surface
            children.setdefault(org.root.id, []).append(_row(org))
    browse = [
        {**_row(org), "children": children.get(org.id, [])}
        for org in all_orgs
        if org.parent_id is None
    ]

    # Sorting applies to the top-level rows only; children stay in name order
    # beneath whichever parent they belong to, so a family group doesn't get
    # torn apart by the sort. Done in Python because `browse` is already built
    # and re-querying to reorder 24 rows would cost more than it saves.
    sort = request.GET.get("sort", "members")
    if sort not in dict(BROWSE_SORTS):
        sort = "members"
    if sort == "members":
        browse.sort(key=lambda r: (-r["org"].member_count, r["org"].name.lower()))
    elif sort == "new":
        browse.sort(key=lambda r: r["org"].created_at, reverse=True)
    else:
        browse.sort(key=lambda r: r["org"].name.lower())

    return render(request, "orgs/search.html", {
        "q": q,
        "results": results,
        "min_chars": SEARCH_MIN_CHARS,
        "browse": browse,
        "browse_total": len(all_orgs),
        "sort": sort,
        "sorts": BROWSE_SORTS,
    })


@login_required
def org_search_json(request):
    """Typeahead endpoint — also Stage 1 of duplicate detection (§4): the
    create form queries it as the user types an org name."""
    q = (request.GET.get("q") or "").strip()
    results = _search_payload(request.user, q) if len(q) >= SEARCH_MIN_CHARS else []
    return JsonResponse({"results": results})


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
def review_request_view(request, org_id: int, req_id: int):
    """One join request, with Approve and Decline right there.

    The bell-panel notification links straight here. Sending admins to the
    Members page instead meant hunting for the right row among a hundred
    members — and if they weren't an admin of that org they just got a blank
    403, with nothing explaining why.
    """
    org = get_object_or_404(Organisation, pk=org_id)
    join_req = get_object_or_404(
        MembershipRequest.objects.select_related("user", "org", "decided_by"),
        pk=req_id, org=org,
    )
    me = _membership(request.user, org)
    if me is None or not me.can_manage:
        messages.error(
            request,
            f"Only an admin of {org.name} can approve join requests.",
        )
        return redirect("dashboard")

    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "approve":
                approve_membership_request(join_req, by_user=request.user)
                messages.success(
                    request, f"{join_req.user.display_name} is now a member of {org.name}."
                )
            elif action == "decline":
                decline_membership_request(join_req, by_user=request.user)
                messages.info(request, f"{join_req.user.display_name}'s request was declined.")
        except ValueError as e:
            # Two admins acting on the same request — say so rather than 500.
            messages.info(request, str(e))
        return redirect("orgs:members", org_id=org.id)

    return render(request, "orgs/review_request.html", {
        "org": org,
        "join_req": join_req,
        "other_pending": org.membership_requests.filter(
            status=MembershipRequest.STATUS_PENDING,
        ).exclude(pk=join_req.pk).count(),
    })


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
        elif action == "demote_child_admin":
            # §6: parent org admins may strip a CHILD org admin's hats. The
            # org__parent=org filter scopes it to this org's own children.
            member = get_object_or_404(
                OrgMember, pk=request.POST.get("member_id"), org__parent=org,
            )
            demote_child_org_admin(member)
            messages.success(
                request,
                f"{member.user.display_name} is no longer an admin of {member.org.name}.",
            )
        elif action == "assign_child_admin":
            child = get_object_or_404(
                Organisation, pk=request.POST.get("child_id"), parent=org,
            )
            member = reassign_child_org_admin(child, request.POST.get("email", ""), by_user=request.user)
            if member:
                messages.success(request, f"{member.user.display_name} now runs {child.name}.")
            else:
                messages.error(request, "No GoodTip account has that email.")
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
    # §6: parent org admins see each child group with its admins, so they can
    # step in (remove/reassign) if a location closes. Day-to-day member
    # management stays with the child's own admin — only admin hats show here.
    child_groups = None
    if not org.is_child:
        children = list(org.children.order_by("name"))
        if children:
            admins_by_org = {}
            admin_members = (
                OrgMember.objects.filter(org__in=children)
                .filter(
                    models.Q(role__in=[OrgMember.ROLE_MANAGER, OrgMember.ROLE_BOTH])
                    | models.Q(is_league_owner=True)
                )
                .select_related("user", "org")
            )
            for m in admin_members:
                admins_by_org.setdefault(m.org_id, []).append(m)
            child_groups = [
                {"org": c, "admins": admins_by_org.get(c.id, [])} for c in children
            ]
    return render(request, "orgs/members.html", {
        "org": org,
        "members": members,
        "pending_requests": pending_requests,
        "child_groups": child_groups,
        "role_choices": OrgMember.ROLE_CHOICES,
        "is_owner": me.is_league_owner,
    })


@login_required
def election_setup_view(request, org_id: int):
    """Admin schedules the charity election — pick a time or start it now."""
    org = get_object_or_404(Organisation, pk=org_id)
    if not _can_manage(request.user, org):
        return HttpResponseForbidden()
    vote = org.charity_votes.first()
    if vote is None:
        messages.info(request, "This league has no charity election — its charity was picked directly.")
        return redirect("dashboard")
    if not vote.is_pending_setup:
        return redirect("orgs:charity_vote", org_id=org.id)

    if request.method == "POST":
        from django.utils import timezone as tz
        from django.utils.dateparse import parse_datetime

        def _aware(raw):
            when = parse_datetime(raw or "")
            if when is not None and tz.is_naive(when):
                when = tz.make_aware(when, tz.get_current_timezone())
            return when

        message = (request.POST.get("admin_message") or "").strip()
        close_at = _aware(request.POST.get("scheduled_close_at"))
        if request.POST.get("action") == "now":
            try:
                schedule_charity_election(vote, when=tz.now(), close_at=close_at, message=message)
            except ValueError as e:
                messages.error(request, str(e))
                return redirect("orgs:election_setup", org_id=org.id)
            messages.success(request, "Election open — members have been notified by email and in the app.")
            return redirect("orgs:charity_vote", org_id=org.id)
        when = _aware(request.POST.get("scheduled_open_at"))
        if when is None:
            messages.error(request, "Pick a date and time for the election to open.")
        else:
            try:
                schedule_charity_election(vote, when=when, close_at=close_at, message=message)
            except ValueError as e:
                messages.error(request, str(e))
                return redirect("orgs:election_setup", org_id=org.id)
            if vote.status == CharityVote.STATUS_OPEN:
                messages.success(request, "That time has already passed, so the election opened straight away — members have been notified.")
                return redirect("orgs:charity_vote", org_id=org.id)
            messages.success(request, f"Election scheduled — members will be notified when it opens.")
            return redirect("orgs:election_setup", org_id=org.id)

    return render(request, "orgs/election_setup.html", {
        "org": org,
        "vote": vote,
        "options": vote.options.select_related("charity"),
    })


@login_required
@require_POST
def dismiss_notification(request, note_id: int):
    """Clear the popup. The row stays in the bell panel as history."""
    from django.utils import timezone as tz

    note = get_object_or_404(Notification, pk=note_id, user=request.user)
    if note.dismissed_at is None:
        note.dismissed_at = tz.now()
        if note.read_at is None:
            note.read_at = tz.now()
        note.save(update_fields=["dismissed_at", "read_at"])
    from django.http import HttpResponse

    return HttpResponse("")


@login_required
@require_POST
def notifications_read_all(request):
    """Opening the bell panel marks everything read (badge clears)."""
    from django.utils import timezone as tz

    request.user.notifications.filter(read_at__isnull=True).update(read_at=tz.now())
    from django.http import HttpResponse

    return HttpResponse("")


# ---------------------------------------------------------------------------
# The Wall — the group's members-only feed. Private by construction: every
# view checks membership, and posts never render anywhere public.
# ---------------------------------------------------------------------------

WALL_PAGE_SIZE = 50
# How many opted-in posts feed the global strip / public rotator. Small on
# purpose: it's a shop window, not a feed to page through.
GLOBAL_WALL_SIZE = 12
PUBLIC_FEED_SIZE = 20


def _attach_threads(posts, *, viewer=None):
    """Hang the visible reply thread off each post.

    Only approved, unhidden replies — a guest reply awaiting moderation is
    invisible to everyone but staff in the Django admin.
    """
    from .models import WallReply

    if not posts:
        return posts
    replies = (
        WallReply.objects.filter(post__in=posts, is_approved=True, is_hidden=False)
        .select_related("author")
        .order_by("created_at")
    )
    by_post = {}
    for r in replies:
        r.can_remove = bool(
            viewer and viewer.is_authenticated
            and (r.author_id == viewer.id or viewer.is_staff)
        )
        by_post.setdefault(r.post_id, []).append(r)
    for p in posts:
        p.thread = by_post.get(p.id, [])
        p.reply_count = len(p.thread)
    return posts


def _global_wall_posts(limit=GLOBAL_WALL_SIZE):
    """Posts members chose to share beyond their own group — the cross-group
    feed behind the public rotator and the in-app 'across GoodTip' strip."""
    from .models import WallPost

    return list(WallPost.public_feed()[:limit])


def _wall_posts_context(request, org, group=None):
    """The feed, with per-post reaction state for the current user.

    `group=None` is the organisation's own wall and excludes every group post —
    otherwise a sub-team's chatter would surface in front of the whole
    organisation, which is the opposite of what a group is for.
    """
    from .models import WallPost, WallReaction

    posts = list(
        WallPost.objects.filter(org=org, group=group, is_hidden=False)
        # "recap" is the reverse OneToOne carrying the round's leaderboard and
        # conversation starters — one join beats a query per card.
        .select_related(
            "author", "recap", "recap__round",
            "tip__match__home_team", "tip__match__away_team",
        )
        [:WALL_PAGE_SIZE]
    )
    # The latest recap card is pinned to the top (recap spec §2).
    recap = next((p for p in posts if p.kind == WallPost.KIND_RECAP), None)
    if recap is not None:
        posts.remove(recap)
        posts.insert(0, recap)

    counts = (
        WallReaction.objects.filter(post__in=posts)
        .values("post_id", "emoji")
        .annotate(n=Count("id"))
    )
    mine = set(
        WallReaction.objects.filter(post__in=posts, user=request.user)
        .values_list("post_id", "emoji")
    )
    by_post = {}
    for row in counts:
        by_post.setdefault(row["post_id"], {})[row["emoji"]] = row["n"]
    for p in posts:
        p.reaction_bar = [
            {
                "emoji": key,
                "glyph": glyph,
                "count": by_post.get(p.id, {}).get(key, 0),
                "mine": (p.id, key) in mine,
            }
            for key, glyph in WallReaction.EMOJI_CHOICES
        ]
        p.can_remove = (
            p.author_id == request.user.id or _can_manage(request.user, org)
        )
    return _attach_threads(posts, viewer=request.user)


def _can_see_wall_post(user, post) -> bool:
    """Is this post on a wall this person can stand on?

    Membership of the organisation is not enough once groups exist. Replying,
    reacting and removing all fetched a post by (id, org), so any member of a
    twenty-thousand-person organisation could act on a post in a group they
    were never in by knowing its id — the id being right there in the anchor
    of any link someone in that group pasted.
    """
    from .models import GroupMember

    if post.group_id is None:
        return True
    return GroupMember.objects.filter(group_id=post.group_id, user=user).exists()


def _current_round_tips(user, org, group=None):
    """The user's picks for the round currently on the board — offered in the
    composer so a post can carry the pick it's talking up.

    Scoped to the wall you are standing on: on Marketing's wall the composer
    offers the picks you made in Marketing, because those are the ones that
    scored on the ladder everyone reading it is on.
    """
    from django.utils import timezone as tz

    from tipping.models import Round, Tip

    rnd = (
        Round.objects.filter(org=org, competition__in=org.competitions.all(), lockout_at__gte=tz.now())
        .order_by("lockout_at").first()
    ) or (
        Round.objects.filter(org=org, competition__in=org.competitions.all())
        .order_by("-round_number").first()
    )
    if rnd is None:
        return []
    return list(
        Tip.objects.filter(user=user, org=org, group=group, match__round=rnd)
        .select_related("match__home_team", "match__away_team")
        .order_by("match__kickoff_at")
    )


@login_required
def wall_view(request, org_id: int):
    org = get_object_or_404(Organisation, pk=org_id)
    if not _is_member(request.user, org):
        return HttpResponseForbidden()
    group = ctx.current_group(request, org)
    # No donation figures on the Wall — participants are out of the money
    # flow entirely (donation-model reference, 23 Jul 2026).
    return render(request, "orgs/wall.html", {
        "org": org,
        "group": group,
        "posts": _wall_posts_context(request, org, group),
        # The cross-organisation feed is about organisations, so it stays off
        # a group's wall — a group is a room inside one building, not a window
        # onto the street.
        "global_posts": _global_wall_posts() if group is None else [],
        "my_tips": _current_round_tips(request.user, org, group),
        "is_admin": _can_manage(request.user, org),
        # Only offer the public toggle where it can actually do something — in
        # an organisation that isn't publicly listed, sharing wide is not on
        # offer, and a group's wall is never public at all.
        "can_share_public": org.is_public_listed and group is None,
    })


@login_required
@require_POST
def wall_post_create(request, org_id: int):
    org = get_object_or_404(Organisation, pk=org_id)
    if not _is_member(request.user, org):
        return HttpResponseForbidden()
    from .models import WallPost

    group = ctx.current_group(request, org)
    body = (request.POST.get("body") or "").strip()[:500]
    tip = None
    tip_id = request.POST.get("tip")
    if tip_id:
        from tipping.models import Tip

        # Only the author's own pick, made in this same context — a pick from
        # the organisation's ladder has no business being talked up on a
        # group's wall as though it scored there.
        tip = Tip.objects.filter(
            pk=tip_id, user=request.user, org=org, group=group,
        ).first()
    if body or tip:
        post = WallPost.objects.create(
            org=org, group=group, author=request.user, body=body, tip=tip,
            kind=WallPost.KIND_MEMBER,
            # Opt-in, only honoured while the organisation is publicly listed,
            # and never for a group post: the public wall is the organisation
            # speaking, not one of its teams.
            is_public=(
                bool(request.POST.get("share_public"))
                and org.is_public_listed
                and group is None
            ),
        )
        _notify_wall_post(post)
    else:
        messages.error(request, "Say something — or at least attach a pick.")
    return redirect("orgs:wall", org_id=org.id)


def _notify_wall_post(post):
    """Tell the rest of the room there's something new on the Wall.

    The room, not the organisation: a post on Marketing's wall must not put a
    notification in front of twenty thousand people who cannot open it.
    """
    from .models import GroupMember

    if post.group_id:
        others = (
            GroupMember.objects.filter(group_id=post.group_id)
            .select_related("user")
            .exclude(user_id=post.author_id)
        )
    else:
        others = (
            post.org.members.select_related("user")
            .exclude(user_id=post.author_id)
        )
    link = reverse("orgs:wall", args=[post.org_id]) + f"#post-{post.id}"
    Notification.objects.bulk_create([
        Notification(
            user=m.user, org=post.org,
            kind=Notification.KIND_WALL_POST,
            title=f"{post.author.display_name} posted on the Wall",
            message=post.body[:160],
            link_url=link,
            # Wall chatter belongs in the bell, not in a modal over the page —
            # only elections and admin notes earn the popup.
            dismissed_at=timezone.now(),
        )
        for m in others
    ])


@login_required
@require_POST
def wall_reply_create(request, org_id: int, post_id: int):
    """A member replies in the thread under a post."""
    org = get_object_or_404(Organisation, pk=org_id)
    if not _is_member(request.user, org):
        return HttpResponseForbidden()
    from .models import WallPost, WallReply

    post = get_object_or_404(WallPost, pk=post_id, org=org, is_hidden=False)
    if not _can_see_wall_post(request.user, post):
        return HttpResponseForbidden()
    body = (request.POST.get("body") or "").strip()[:500]
    if not body:
        messages.error(request, "Write something first.")
        return redirect(_wall_anchor(org, post))
    reply = WallReply.objects.create(post=post, author=request.user, body=body)
    _notify_wall_reply(reply)
    return redirect(_wall_anchor(org, post))


def _wall_anchor(org, post):
    return reverse("orgs:wall", args=[org.id]) + f"#post-{post.id}"


def _notify_wall_reply(reply):
    """Ping the post's author and everyone else already in the thread."""
    post = reply.post
    recipients = set()
    if post.author_id and post.author_id != reply.author_id:
        recipients.add(post.author_id)
    recipients.update(
        post.replies.filter(author__isnull=False, is_approved=True)
        .exclude(author_id=reply.author_id)
        .values_list("author_id", flat=True)
    )
    if not recipients:
        return
    # Never notify someone who has since left the group.
    member_ids = set(post.org.members.values_list("user_id", flat=True))
    recipients &= member_ids
    who = reply.display_name
    link = _wall_anchor(post.org, post)
    Notification.objects.bulk_create([
        Notification(
            user_id=uid, org=post.org,
            kind=Notification.KIND_WALL_REPLY,
            title=(
                f"{who} replied to your post" if uid == post.author_id
                else f"{who} replied in a thread you're in"
            ),
            message=reply.body[:160],
            link_url=link,
            dismissed_at=timezone.now(),
        )
        for uid in recipients
    ])


@login_required
@require_POST
def wall_reply_remove(request, org_id: int, reply_id: int):
    """Author deletes their own reply; a group admin hides anyone's."""
    org = get_object_or_404(Organisation, pk=org_id)
    if not _is_member(request.user, org):
        return HttpResponseForbidden()
    from .models import WallReply

    reply = get_object_or_404(WallReply, pk=reply_id, post__org=org)
    if reply.author_id == request.user.id:
        reply.delete()
    elif _can_manage(request.user, org):
        reply.is_hidden = True
        reply.save(update_fields=["is_hidden"])
    else:
        return HttpResponseForbidden()
    return redirect("orgs:wall", org_id=org.id)


@login_required
@require_POST
def wall_react(request, org_id: int, post_id: int):
    """Toggle one emoji on one post. Returns the refreshed reaction bar for
    htmx swaps; falls back to a redirect for plain form posts."""
    org = get_object_or_404(Organisation, pk=org_id)
    if not _is_member(request.user, org):
        return HttpResponseForbidden()
    from .models import WallPost, WallReaction

    post = get_object_or_404(WallPost, pk=post_id, org=org, is_hidden=False)
    if not _can_see_wall_post(request.user, post):
        return HttpResponseForbidden()
    emoji = request.POST.get("emoji")
    if emoji not in dict(WallReaction.EMOJI_CHOICES):
        return HttpResponseForbidden()
    existing = WallReaction.objects.filter(post=post, user=request.user, emoji=emoji)
    if existing.exists():
        existing.delete()
    else:
        WallReaction.objects.create(post=post, user=request.user, emoji=emoji)
    if request.headers.get("HX-Request"):
        counts = dict(
            WallReaction.objects.filter(post=post)
            .values_list("emoji").annotate(n=Count("id"))
            .values_list("emoji", "n")
        )
        mine = set(
            WallReaction.objects.filter(post=post, user=request.user)
            .values_list("emoji", flat=True)
        )
        bar = [
            {"emoji": k, "glyph": g, "count": counts.get(k, 0), "mine": k in mine}
            for k, g in WallReaction.EMOJI_CHOICES
        ]
        return render(request, "partials/wall_reactions.html", {
            "org": org, "post": post, "bar": bar,
        })
    return redirect("orgs:wall", org_id=org.id)


@login_required
@require_POST
def wall_post_remove(request, org_id: int, post_id: int):
    """Author deletes their own post outright; an admin hides anyone's
    (kept in the DB, per the recap spec's hide-don't-delete admin control)."""
    org = get_object_or_404(Organisation, pk=org_id)
    if not _is_member(request.user, org):
        return HttpResponseForbidden()
    from .models import WallPost

    post = get_object_or_404(WallPost, pk=post_id, org=org)
    if post.author_id == request.user.id and _can_see_wall_post(request.user, post):
        post.delete()
    elif _can_manage(request.user, org):
        # Admins can moderate a group they are not in. Someone has to be able
        # to take down what is posted in their organisation's name, and a room
        # nobody can moderate is worse than one that is read by an admin.
        post.is_hidden = True
        post.save(update_fields=["is_hidden"])
    else:
        return HttpResponseForbidden()
    return redirect("orgs:wall", org_id=org.id)


# ---------------------------------------------------------------------------
# The public Wall — /wall/. Shows only what members explicitly shared beyond
# their group (WallPost.public_feed()), plus curated sample posts so the page
# still reads as a feed on a quiet day.
# ---------------------------------------------------------------------------

# Guest replies are rate-limited per IP: enough to hold a conversation, not
# enough to flood the moderation queue.
GUEST_REPLY_WINDOW_SECONDS = 60 * 10
GUEST_REPLY_MAX_PER_WINDOW = 3


def _client_ip(request):
    fwd = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


def public_wall_view(request):
    """The marketing Wall page: a live, cross-group feed of shared posts."""
    from .models import WallPost

    posts = _attach_threads(
        list(WallPost.public_feed()[:PUBLIC_FEED_SIZE]),
        viewer=request.user,
    )
    # Members of the posting group reply straight into the thread; everyone
    # else goes through the guest form and the moderation queue.
    my_org_ids = set()
    if request.user.is_authenticated:
        my_org_ids = set(request.user.memberships.values_list("org_id", flat=True))
    for p in posts:
        p.can_reply_direct = p.org_id in my_org_ids
    return render(request, "public/wall.html", {
        "active": "wall",
        "posts": posts,
        # The hero rotator runs off the same posts; the template falls back to
        # its scripted sample cards when the real feed is still thin.
        "rotator_posts": posts[:GLOBAL_WALL_SIZE],
        "has_live_posts": bool(posts),
        "live_group_count": (
            Organisation.objects.filter(is_public_listed=True, wall_posts__is_public=True)
            .distinct().count()
        ),
    })


@require_POST
def public_wall_reply(request, post_id: int):
    """A visitor replies from the public page.

    Members posting from here go straight in. Everyone else leaves a name and
    an email and the reply is held for staff approval — nothing anonymous
    lands on a public page unread.
    """
    from django.core.cache import cache
    from django.core.exceptions import ValidationError
    from django.core.validators import validate_email

    from .models import WallPost, WallReply

    post = get_object_or_404(WallPost.public_feed(), pk=post_id)
    back = reverse("wall") + f"#post-{post.id}"
    body = (request.POST.get("body") or "").strip()[:500]

    # Honeypot: a real person never fills a field they cannot see. Answer as
    # if it worked so the bot has nothing to learn.
    if request.POST.get("website"):
        return redirect(back)
    if not body:
        messages.error(request, "Write something first.")
        return redirect(back)

    if request.user.is_authenticated and _is_member(request.user, post.org):
        reply = WallReply.objects.create(post=post, author=request.user, body=body)
        _notify_wall_reply(reply)
        messages.success(request, "Posted to the Wall.")
        return redirect(back)

    if request.user.is_authenticated:
        # Signed in, but not in that group — we already know who they are, so
        # don't ask for an email; the reply still waits for approval.
        name, email = request.user.display_name, request.user.email
    else:
        name = (request.POST.get("guest_name") or "").strip()[:80]
        email = (request.POST.get("guest_email") or "").strip()[:254]
        if not email:
            messages.error(request, "Add your email so we know who's talking.")
            return redirect(back)
        try:
            validate_email(email)
        except ValidationError:
            messages.error(request, "That email doesn't look right — check it and try again.")
            return redirect(back)

    ip = _client_ip(request)
    key = f"wall-guest-reply:{ip}"
    used = cache.get(key, 0)
    if used >= GUEST_REPLY_MAX_PER_WINDOW:
        messages.error(request, "That's a few replies in a row — give it a minute and try again.")
        return redirect(back)
    cache.set(key, used + 1, GUEST_REPLY_WINDOW_SECONDS)

    WallReply.objects.create(
        post=post, body=body,
        guest_name=name or email.split("@")[0],
        guest_email=email,
        is_approved=False,
        ip_address=ip,
    )
    messages.success(
        request,
        "Thanks — your reply is with us. It goes up on the Wall once a human's had a look.",
    )
    return redirect(back)


# ---------------------------------------------------------------------------
# Live notifications — the nav polls this so a new election, reply or post
# lands as a toast without a page refresh.
# ---------------------------------------------------------------------------

@login_required
def notifications_feed(request):
    """Unread notifications newer than the id the client already has."""
    since = request.GET.get("since")
    qs = request.user.notifications.select_related("org").filter(read_at__isnull=True)
    if since and since.isdigit():
        qs = qs.filter(id__gt=int(since))
    notes = list(qs.order_by("-created_at")[:5])
    return JsonResponse({
        "unread": request.user.notifications.filter(read_at__isnull=True).count(),
        "latest_id": max((n.id for n in notes), default=int(since) if since and since.isdigit() else 0),
        "items": [
            {
                "id": n.id,
                "kind": n.kind,
                "icon": n.icon,
                "title": n.title,
                "message": (n.message or "")[:160],
                "url": n.link_url or "",
                "org": n.org.name if n.org_id else "GoodTip",
                "when": "just now",
            }
            for n in notes
        ],
    })


@login_required
@require_POST
def notification_open(request, note_id: int):
    """Mark one notification read (the bell items call this on click)."""
    note = get_object_or_404(Notification, pk=note_id, user=request.user)
    if note.read_at is None:
        note.read_at = timezone.now()
        note.save(update_fields=["read_at"])
    from django.http import HttpResponse

    return HttpResponse("")


# ---------------------------------------------------------------------------
# Where the user is: switching organisation, and stepping in and out of a group
# ---------------------------------------------------------------------------


def _safe_next(request, fallback="dashboard"):
    """Where to land after switching.

    `next` arrives from the client, so it is only honoured when it is a path on
    this site. An open redirect here would be handed out on every switch link
    in the nav.
    """
    target = request.POST.get("next") or request.GET.get("next") or ""
    if target.startswith("/") and not target.startswith("//"):
        return target
    return reverse(fallback)


@login_required
@require_POST
def switch_org(request, org_id: int):
    """Move to another organisation.

    POST because it changes state that outlives the request. Membership is
    checked here as well as in context.current_org — this is the write, and a
    write that trusts the id it was given is how someone ends up looking at an
    organisation they were never in.
    """
    org = get_object_or_404(Organisation, pk=org_id)
    if not _is_member(request.user, org):
        messages.error(request, "You're not a member of that organisation.")
        return redirect(_safe_next(request))

    ctx.set_current_org(request, org)
    messages.success(request, f"You're now in {org.name}.")
    return redirect(_safe_next(request))


@login_required
@require_POST
def switch_group(request, org_id: int, group_id: int):
    """Step into a group. Everything from here on belongs to the group."""
    group = get_object_or_404(
        Group, pk=group_id, org_id=org_id,
        approval_status=Group.APPROVAL_APPROVED,
    )
    if not ctx.is_group_member(request.user, group):
        messages.error(request, "You're not in that group.")
        return redirect(_safe_next(request))
    if not group.org.groups_enabled:
        messages.error(request, "Groups aren't switched on for this organisation.")
        return redirect(_safe_next(request))

    ctx.set_current_group(request, group)
    messages.success(request, f"You're now tipping in {group.name}.")
    return redirect(_safe_next(request))


@login_required
@require_POST
def leave_group_view(request):
    """Step back out to the organisation itself, without leaving the group."""
    ctx.leave_group_context(request)
    org = ctx.current_org(request)
    messages.success(
        request,
        f"Back to {org.name}." if org else "Back to your organisation.",
    )
    return redirect(_safe_next(request))


# ---------------------------------------------------------------------------
# Organisation settings — the things that were only settable at creation
# ---------------------------------------------------------------------------


@login_required
def org_settings_view(request, org_id: int):
    """Change what an organisation picked when it was created.

    Competitions were chosen at step 3 of the wizard and then frozen: the field
    appeared in exactly one template and there was no edit path anywhere, so an
    organisation that started on NRL and wanted AFL the following year had no
    way to say so short of asking support. That is the whole reason this page
    exists.

    Adding a competition brings its whole series with it — pick NRL and the
    members get NRL and NRLW, never one without the other. That is a property
    of Competition rather than a rule enforced here: a Competition bundles its
    men's, women's and representative series, and the form offers Competitions.
    """
    org = get_object_or_404(Organisation, pk=org_id, parent__isnull=True)
    if not _can_manage(request.user, org):
        return HttpResponseForbidden()

    if request.method == "POST":
        picked = set(request.POST.getlist("competitions"))
        allowed = {str(c.pk): c for c in fed_competitions()}
        chosen = [allowed[p] for p in picked if p in allowed]
        if not chosen:
            messages.error(request, "Keep at least one competition — members need something to tip.")
            return redirect("orgs:settings", org_id=org.pk)

        before = set(org.competitions.values_list("pk", flat=True))
        org.competitions.set(chosen)
        after = {c.pk for c in chosen}

        added = [c.name for c in chosen if c.pk not in before]
        # Removing is not a delete. The rounds, the tips and the ladder for a
        # competition that has been dropped all stay exactly where they are —
        # a season half-played is still a season people remember. It stops
        # appearing on the tipping screens, and putting it back restores it.
        removed_count = len(before - after)
        if added:
            messages.success(
                request,
                f"Added {', '.join(added)}. Members get the men's and women's "
                "competitions together — there's no picking one without the other.",
            )
        if removed_count:
            messages.info(
                request,
                f"{removed_count} competition{'s' if removed_count != 1 else ''} removed. "
                "Nothing was deleted — the rounds and tips are still there if you add it back.",
            )
        if not added and not removed_count:
            messages.info(request, "Nothing changed.")
        return redirect("orgs:settings", org_id=org.pk)

    return render(request, "orgs/settings.html", {
        "org": org,
        "competitions": fed_competitions(),
        "chosen_ids": set(org.competitions.values_list("pk", flat=True)),
    })


# ---------------------------------------------------------------------------
# Groups — the directory, and the switch that turns the feature on
# ---------------------------------------------------------------------------


def _group_icon(group) -> tuple[str, str]:
    """A glyph for a group, from its kind or its name.

    Matched on the words people actually use, so a group called "IT Support"
    and one called "Technology" land on the same mark.
    """
    hay = " ".join([
        group.name or "",
        (group.kind.name if group.kind_id else ""),
        group.label or "",
    ]).lower()
    for words, icon, tone in GROUP_GLYPHS:
        if any(w in hay for w in words):
            return icon, tone
    return "ic-people", "slate"


GROUP_GLYPHS = [
    (("it", "tech", "engineer", "developer", "software"), "ic-bolt", "violet"),
    (("finance", "account", "payroll"), "ic-coin", "gold"),
    (("marketing", "brand", "comms", "creative"), "ic-star", "pink"),
    (("sales", "revenue", "business development"), "ic-trophy", "amber"),
    (("people", "hr", "culture", "talent"), "ic-people", "teal"),
    (("ops", "operation", "logistics", "warehouse"), "ic-shield", "slate"),
    (("legal", "risk", "compliance"), "ic-doc", "navy"),
    (("support", "service", "customer"), "ic-chat", "sky"),
]


@login_required
@require_POST
def groups_toggle(request, org_id: int):
    """Switch groups on or off for an organisation.

    Off is the default and stays right for most: five people in an office do
    not need departments. Switching off leaves every group and every tip
    exactly where it is — it only takes the feature out of the nav, so an
    organisation that tries it and changes its mind loses nothing.
    """
    org = get_object_or_404(Organisation, pk=org_id, parent__isnull=True)
    if not _can_manage(request.user, org):
        return HttpResponseForbidden()

    org.groups_enabled = not org.groups_enabled
    org.save(update_fields=["groups_enabled"])
    if org.groups_enabled:
        messages.success(request, "Groups are on. Start one, or let your members ask.")
    else:
        ctx.leave_group_context(request)
        messages.info(
            request,
            "Groups are off. Nothing was deleted — switch them back on and "
            "they're all still there.",
        )
    return redirect("orgs:groups", org_id=org.pk)


@login_required
def groups_view(request, org_id: int):
    """The group directory: find yours, join it, or start one."""
    org = get_object_or_404(Organisation, pk=org_id)
    root = org.root
    if not _is_member(request.user, root):
        return HttpResponseForbidden()
    is_admin = _can_manage(request.user, root)

    if request.method == "POST":
        action = request.POST.get("action", "")

        if action == "create":
            raw_kind = request.POST.get("kind") or ""
            kind = (
                GroupType.objects.filter(pk=raw_kind, is_active=True).first()
                if raw_kind.isdigit() else None
            )
            try:
                group = create_group(
                    root,
                    name=request.POST.get("name", ""),
                    by_user=request.user,
                    kind=kind,
                    label=request.POST.get("label", ""),
                )
                if group.is_pending_approval:
                    messages.success(
                        request,
                        f"{group.name} has been sent to {root.name}'s admins to "
                        "approve. You'll get a notification when they decide.",
                    )
                else:
                    messages.success(request, f"{group.name} is live. Bring your team in.")
            except ValueError as e:
                messages.error(request, str(e))
            return redirect("orgs:groups", org_id=root.pk)

        group = get_object_or_404(Group, pk=request.POST.get("group_id"), org=root)

        if action == "join":
            try:
                join_group(group, user=request.user)
                ctx.set_current_group(request, group)
                messages.success(request, f"You're in {group.name}.")
            except ValueError as e:
                messages.error(request, str(e))
            return redirect("orgs:groups", org_id=root.pk)

        if action == "leave":
            leave_group(group, user=request.user)
            messages.info(
                request,
                f"You've left {group.name}. The tips you made there stay on its ladder.",
            )
            return redirect("orgs:groups", org_id=root.pk)

        # Everything below is an admin decision on a pending group.
        if not is_admin:
            return HttpResponseForbidden()
        if action == "approve":
            approve_group(group, by_user=request.user)
            messages.success(request, f"{group.name} is live.")
        elif action == "decline":
            name = group.name
            try:
                decline_group(group, by_user=request.user)
                messages.info(request, f"{name} was declined.")
            except ValueError as e:
                messages.error(request, str(e))
        return redirect("orgs:groups", org_id=root.pk)

    groups = groups_for(root, include_pending_for=request.user)
    q = (request.GET.get("q") or "").strip()
    if q:
        groups = groups.filter(
            Q(name__icontains=q) | Q(label__icontains=q) | Q(kind__name__icontains=q)
        )

    mine = set(
        GroupMember.objects.filter(user=request.user, group__org=root)
        .values_list("group_id", flat=True)
    )
    member_counts = {
        row["group"]: row["n"]
        for row in GroupMember.objects.filter(group__org=root)
        .values("group").annotate(n=Count("id"))
    }

    rows = []
    for g in groups:
        icon, tone = _group_icon(g)
        rows.append({
            "group": g,
            "icon": icon,
            "tone": tone,
            "members": member_counts.get(g.pk, 0),
            "is_mine": g.pk in mine,
            "awaiting_approval": g.is_pending_approval,
        })

    kind_choices = GroupType.objects.filter(is_active=True).filter(
        Q(organisation_type__isnull=True) | Q(organisation_type=root.organisation_type_id)
    ).order_by("organisation_type__id", "sort_order", "name")

    return render(request, "orgs/groups.html", {
        "org": root,
        "rows": rows,
        "q": q,
        "kind_choices": kind_choices,
        "is_admin": is_admin,
        "current_group": ctx.current_group(request, root),
        "pending_count": sum(1 for r in rows if r["awaiting_approval"]),
    })
