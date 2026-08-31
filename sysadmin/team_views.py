"""The screens for running a team of administrators.

Four jobs, in the order somebody meets them:

  team / team_new     — who the administrators are, and adding one
  review queue        — work waiting for a full-access administrator
  my work             — what a restricted administrator has in flight
  activity            — the record of who did what to whom

The wizard in `team_new` is three steps on one page rather than three pages,
because the thing being decided is a single shape — these powers, these ones
reviewed — and splitting it across requests means holding a half-made decision
in a session and reconciling it if they wander off.
"""
from __future__ import annotations

import json

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.db import transaction
from django.http import Http404, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import access, capabilities, review
from .models import (
    AdminAccess, AdminAuditEvent, AdminGrant, AdminInvite, AdminTask, ChangeRequest,
)
from .notify import notify_task_assigned, send_admin_invite

User = get_user_model()


def _require_full(request):
    """Creating admins and reviewing their work is never delegated."""
    if not access.is_full_access(request.user):
        return HttpResponseForbidden(
            "Only a full-access administrator can manage the team."
        )
    return None


# ---------------------------------------------------------------------------
# The team
# ---------------------------------------------------------------------------

def team(request):
    guard = _require_full(request)
    if guard:
        return guard

    access.ensure_access(request.user, full=True)
    rows = (
        AdminAccess.objects.select_related("user", "created_by")
        .prefetch_related("grants")
        .all()
    )
    return render(request, "hq/team.html", {
        "rows": rows,
        "pending_invites": AdminInvite.objects.filter(
            consumed_at__isnull=True, expires_at__gt=timezone.now()
        ).select_related("access__user"),
    })


def team_new(request):
    guard = _require_full(request)
    if guard:
        return guard

    if request.method == "POST":
        return _create_admin(request)

    return render(request, "hq/team_new.html", {
        "groups": capabilities.GROUPS,
        "reviewable": capabilities.REVIEWABLE,
    })


@transaction.atomic
def _create_admin(request):
    """Make the account, record what it may do, and send the invitation.

    The account is created INACTIVE with an unusable password. Nothing about it
    works until the invited person exchanges their code for a password of their
    own — so a typo in the address grants nobody anything, and the person doing
    the granting never holds the credential.
    """
    email = (request.POST.get("email") or "").strip().lower()
    name = (request.POST.get("display_name") or "").strip()
    note = (request.POST.get("note") or "").strip()
    full = request.POST.get("access_level") == "full"
    picked = capabilities.valid(request.POST.getlist("capability"))
    reviewed = set(capabilities.valid(request.POST.getlist("review"))) & set(picked)

    errors = []
    if not email:
        errors.append("Enter the email address to send the invitation to.")
    elif User.objects.filter(email__iexact=email).exists():
        errors.append(
            f"{email} already has a GoodTip account. "
            "Give that account access from the list instead of making a second one."
        )
    if not name:
        errors.append("Enter the name this person should be known by.")
    if not full and not picked:
        errors.append("Choose at least one thing this administrator can do.")

    if errors:
        for e in errors:
            messages.error(request, e)
        return render(request, "hq/team_new.html", {
            "groups": capabilities.GROUPS,
            "reviewable": capabilities.REVIEWABLE,
            "form": {
                "email": email, "display_name": name, "note": note,
                "access_level": "full" if full else "limited",
                "capability": picked, "review": list(reviewed),
            },
        })

    user = User.objects.create_user(
        email=email, password=None, display_name=name,
    )
    user.set_unusable_password()
    user.is_staff = True
    user.is_superuser = full
    # Nothing works until the invitation is accepted.
    user.is_active = False
    user.save()

    row = AdminAccess.objects.create(
        user=user, is_full_access=full, created_by=request.user, note=note,
    )
    if not full:
        AdminGrant.objects.bulk_create([
            AdminGrant(access=row, capability=key, requires_approval=key in reviewed)
            for key in picked
        ])

    AdminAuditEvent.record(
        actor=request.user,
        action=AdminAuditEvent.ADMIN_CREATED,
        subject=user,
        summary=f"Created {'a full-access' if full else 'a restricted'} administrator",
        full_access=full,
        granted=picked,
        reviewed=sorted(reviewed),
        note=note,
    )

    invite, code = AdminInvite.issue(row, by_user=request.user)
    sent = send_admin_invite(invite, code)
    AdminAuditEvent.record(
        actor=request.user,
        action=AdminAuditEvent.INVITE_SENT,
        subject=user,
        summary=f"Invitation emailed to {email}",
        delivered=bool(sent),
    )

    if sent:
        messages.success(
            request,
            f"{name} is now an administrator. We've emailed {email} a code to set "
            "their password — the account stays switched off until they use it.",
        )
    else:
        messages.error(
            request,
            f"{name} was created, but the invitation email could not be sent. "
            "Open their row and choose Resend invitation once mail is working.",
        )
    return redirect("admin:hq_team")


@require_POST
def team_resend(request, access_id: int):
    guard = _require_full(request)
    if guard:
        return guard
    row = get_object_or_404(AdminAccess, pk=access_id)
    invite, code = AdminInvite.issue(row, by_user=request.user)
    sent = send_admin_invite(invite, code)
    AdminAuditEvent.record(
        actor=request.user, action=AdminAuditEvent.INVITE_SENT, subject=row.user,
        summary=f"Invitation re-sent to {row.user.email}", delivered=bool(sent),
    )
    messages.success(request, f"A new invitation is on its way to {row.user.email}.")
    if not sent:
        messages.error(request, "The email could not be sent — check the mail settings.")
    return redirect("admin:hq_team")


def team_edit(request, access_id: int):
    """Change what an existing administrator may do."""
    guard = _require_full(request)
    if guard:
        return guard
    row = get_object_or_404(
        AdminAccess.objects.select_related("user").prefetch_related("grants"),
        pk=access_id,
    )

    if request.method == "POST":
        if row.user_id == request.user.pk:
            messages.error(
                request,
                "You cannot change your own access. Ask the other full-access "
                "administrator to do it.",
            )
            return redirect("admin:hq_team")

        before = {g.capability: g.requires_approval for g in row.grants.all()}
        was_full = row.is_full_access

        full = request.POST.get("access_level") == "full"
        picked = capabilities.valid(request.POST.getlist("capability"))
        reviewed = set(capabilities.valid(request.POST.getlist("review"))) & set(picked)

        row.is_full_access = full
        row.note = (request.POST.get("note") or "").strip()
        row.save(update_fields=["is_full_access", "note"])
        row.user.is_superuser = full
        row.user.save(update_fields=["is_superuser"])

        row.grants.all().delete()
        if not full:
            AdminGrant.objects.bulk_create([
                AdminGrant(access=row, capability=k, requires_approval=k in reviewed)
                for k in picked
            ])

        AdminAuditEvent.record(
            actor=request.user,
            action=AdminAuditEvent.ACCESS_CHANGED,
            subject=row.user,
            summary=f"Changed what {row.user} can do",
            was_full_access=was_full, now_full_access=full,
            before=before, granted=picked, reviewed=sorted(reviewed),
        )
        messages.success(request, f"Updated what {row.user} can do.")
        return redirect("admin:hq_team")

    held = {g.capability: g.requires_approval for g in row.grants.all()}
    return render(request, "hq/team_edit.html", {
        "row": row,
        "groups": capabilities.GROUPS,
        "reviewable": capabilities.REVIEWABLE,
        "held": held,
        "held_keys": list(held),
        "reviewed_keys": [k for k, v in held.items() if v],
    })


@require_POST
def team_toggle(request, access_id: int):
    """Suspend or restore an administrator.

    Suspending rather than deleting: their name still has to appear against the
    approvals and edits they made, and a deleted account takes that with it.
    """
    guard = _require_full(request)
    if guard:
        return guard
    row = get_object_or_404(AdminAccess, pk=access_id)
    if row.user_id == request.user.pk:
        messages.error(request, "You cannot suspend your own account.")
        return redirect("admin:hq_team")

    row.is_active = not row.is_active
    row.save(update_fields=["is_active"])
    row.user.is_active = row.is_active
    row.user.save(update_fields=["is_active"])

    AdminAuditEvent.record(
        actor=request.user,
        action=AdminAuditEvent.ADMIN_RESTORED if row.is_active else AdminAuditEvent.ADMIN_SUSPENDED,
        subject=row.user,
        summary=("Restored " if row.is_active else "Suspended ") + str(row.user),
    )
    messages.success(
        request,
        f"{row.user} has been {'restored' if row.is_active else 'suspended'}.",
    )
    return redirect("admin:hq_team")


# ---------------------------------------------------------------------------
# Reviewing
# ---------------------------------------------------------------------------

def reviews(request):
    guard = _require_full(request)
    if guard:
        return guard
    return render(request, "hq/reviews.html", {
        "waiting": ChangeRequest.objects.filter(status=ChangeRequest.PENDING)
                   .select_related("requested_by"),
        "decided": ChangeRequest.objects.exclude(status=ChangeRequest.PENDING)
                   .select_related("requested_by", "reviewed_by")[:25],
        "failed": ChangeRequest.objects.exclude(apply_error="")
                  .select_related("requested_by")[:10],
    })


# Fields that are markup rather than a value, shown in a big box and rendered
# as a preview instead of printed raw.
RICH_FIELDS = {"body", "title_html", "excerpt_html", "html", "reply_body"}


def review_detail(request, request_id: int):
    guard = _require_full(request)
    if guard:
        return guard
    cr = get_object_or_404(
        ChangeRequest.objects.select_related("requested_by", "reviewed_by"),
        pk=request_id,
    )

    if request.method == "POST":
        return _decide(request, cr)

    data = cr.data_to_apply()
    fields = [
        {
            "name": k,
            "value": v if isinstance(v, str) else json.dumps(v),
            "rich": k in RICH_FIELDS,
            "long": k in RICH_FIELDS or (isinstance(v, str) and len(v) > 90),
        }
        for k, v in sorted(data.items())
    ]
    return render(request, "hq/review_detail.html", {
        "cr": cr,
        "fields": fields,
        "files": cr.files.all(),
    })


@transaction.atomic
def _decide(request, cr):
    if not cr.is_pending:
        messages.info(request, "That has already been reviewed.")
        return redirect("admin:hq_reviews")
    if cr.requested_by_id == request.user.pk:
        messages.error(request, "You cannot review your own work.")
        return redirect("admin:hq_reviews")

    action = request.POST.get("decision")
    feedback = (request.POST.get("feedback") or "").strip()

    if action == "decline":
        if not feedback:
            messages.error(
                request,
                "Say why you are sending this back — that note is the only thing "
                "the author gets to work from.",
            )
            return redirect("admin:hq_review_detail", request_id=cr.pk)
        review.decide(cr, reviewer=request.user, outcome=ChangeRequest.DECLINED,
                      feedback=feedback)
        messages.success(request, f"Sent back to {cr.requested_by} with your note.")
        return redirect("admin:hq_reviews")

    # Approving, possibly after editing the fields. Anything the reviewer
    # actually changed is stored separately so the author can be shown it.
    amended = None
    if action == "amend":
        amended = dict(cr.post_data)
        for key in cr.post_data:
            field = f"field_{key}"
            if field in request.POST:
                amended[key] = request.POST[field]
        if amended == cr.post_data:
            amended = None

    outcome = ChangeRequest.AMENDED if amended is not None else ChangeRequest.APPROVED
    ok = review.decide(cr, reviewer=request.user, outcome=outcome,
                       feedback=feedback, amended=amended)
    if ok:
        messages.success(
            request,
            f"Approved{' with your changes' if amended is not None else ''} — it is live now.",
        )
    else:
        messages.error(
            request,
            "Approved, but it could not be carried out. The error is on the "
            "review page; nothing has changed on the site.",
        )
    return redirect("admin:hq_reviews")


# ---------------------------------------------------------------------------
# What a restricted administrator sees about their own work
# ---------------------------------------------------------------------------

def my_work(request):
    if not access.is_admin(request.user):
        return HttpResponseForbidden("Administrators only.")
    row = access.access_for(request.user)
    return render(request, "hq/my_work.html", {
        "row": row,
        "is_full": access.is_full_access(request.user),
        "grants": row.grants.all() if row else [],
        "capabilities": capabilities,
        "waiting": ChangeRequest.objects.filter(
            requested_by=request.user, status=ChangeRequest.PENDING),
        "decided": ChangeRequest.objects.filter(requested_by=request.user)
                   .exclude(status=ChangeRequest.PENDING)
                   .select_related("reviewed_by")[:20],
        "tasks": AdminTask.objects.filter(
            assigned_to=request.user, status=AdminTask.OPEN),
        "done_tasks": AdminTask.objects.filter(
            assigned_to=request.user, status=AdminTask.DONE)[:10],
    })


@require_POST
def task_new(request):
    guard = _require_full(request)
    if guard:
        return guard
    try:
        target = User.objects.get(pk=request.POST.get("assigned_to"))
    except (User.DoesNotExist, ValueError, TypeError):
        messages.error(request, "Pick who the task is for.")
        return redirect("admin:hq_team")

    title = (request.POST.get("title") or "").strip()
    if not title:
        messages.error(request, "Give the task a title.")
        return redirect("admin:hq_team")

    task = AdminTask.objects.create(
        assigned_to=target, assigned_by=request.user, title=title[:160],
        detail=(request.POST.get("detail") or "").strip(),
    )
    AdminAuditEvent.record(
        actor=request.user, action=AdminAuditEvent.TASK_ASSIGNED, subject=target,
        summary=title, task=task.pk,
    )
    notify_task_assigned(task)
    messages.success(request, f"Sent to {target}.")
    return redirect("admin:hq_team")


@require_POST
def task_done(request, task_id: int):
    task = get_object_or_404(AdminTask, pk=task_id)
    if task.assigned_to_id != request.user.pk and not access.is_full_access(request.user):
        return HttpResponseForbidden("That task is not yours.")
    task.status = AdminTask.DONE
    task.completed_at = timezone.now()
    task.save(update_fields=["status", "completed_at"])
    AdminAuditEvent.record(
        actor=request.user, action=AdminAuditEvent.TASK_COMPLETED,
        subject=task.assigned_to, summary=task.title, task=task.pk,
    )
    messages.success(request, "Marked done.")
    return redirect(request.POST.get("next") or "admin:hq_my_work")


# ---------------------------------------------------------------------------
# The record
# ---------------------------------------------------------------------------

def activity(request):
    guard = _require_full(request)
    if guard:
        return guard
    events = AdminAuditEvent.objects.select_related("actor", "subject")
    who = request.GET.get("who")
    what = request.GET.get("what")
    if who:
        events = events.filter(subject_id=who)
    if what:
        events = events.filter(action=what)
    return render(request, "hq/activity.html", {
        "events": events[:300],
        "actions": AdminAuditEvent.ACTION_CHOICES,
        "people": User.objects.filter(admin_audit_subject_of__isnull=False).distinct(),
        "who": who or "",
        "what": what or "",
    })
