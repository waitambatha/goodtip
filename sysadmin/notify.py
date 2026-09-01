"""The messages delegated administration sends.

Every one of these is fire-and-forget: `send_template` swallows its own
failures, so a mail outage can never stop an invitation being created or a
change being approved. What it must never do is go quiet about a decision —
an author who submits work and hears nothing back concludes the system ate it.
"""
import logging

from django.urls import reverse

from goodtip import mail

from . import access, capabilities
from .models import ChangeRequest


logger = logging.getLogger(__name__)


def _url(name, *args):
    return mail.site_url(reverse(name, args=args))


def send_admin_invite(invite, code: str) -> bool:
    """The one-time code that lets a new administrator set their password."""
    user = invite.access.user
    grants = list(invite.access.grants.all())
    return mail.send_template(
        "admin_invite",
        subject="You've been given access to GoodTip HQ",
        to=user.email,
        context={
            "name": user.display_name or user.email,
            "code": code,
            "link": mail.site_url(reverse("admin_invite_accept", args=[invite.token])),
            "inviter": str(invite.created_by) if invite.created_by else "A GoodTip administrator",
            "is_full_access": invite.access.is_full_access,
            "direct": [capabilities.label(g.capability) for g in grants if not g.requires_approval],
            "reviewed": [capabilities.label(g.capability) for g in grants if g.requires_approval],
            "hours": int(invite.TTL.total_seconds() // 3600),
        },
    )


def send_access_granted(access_row, by_user=None) -> bool:
    """For somebody who was already a member: the account you have now opens HQ.

    Deliberately not the invitation. An invitation's whole job is to prove the
    recipient holds the address by making them exchange a code for a password,
    and this person proved that when they signed up — sending them a set-up
    code for an account they already sign into is asking a question that has
    been answered, and it reads like their password has been reset.
    """
    user = access_row.user
    grants = list(access_row.grants.all())
    return mail.send_template(
        "admin_access_granted",
        subject="You now have access to GoodTip HQ",
        to=user.email,
        context={
            "name": user.display_name or user.email,
            "inviter": str(by_user) if by_user else "A GoodTip administrator",
            "is_full_access": access_row.is_full_access,
            "direct": [capabilities.label(g.capability) for g in grants if not g.requires_approval],
            "reviewed": [capabilities.label(g.capability) for g in grants if g.requires_approval],
            "link": _url("admin:hq_my_work"),
        },
    )


def notify_reviewers_of_new_request(change_request) -> int:
    """Tell everyone who can review that something is waiting.

    All full-access administrators, not just whoever created the author — see
    access.full_access_admins for why.
    """
    cr = change_request
    reviewers = [u for u in access.full_access_admins() if u.pk != cr.requested_by_id]
    if not reviewers:
        logger.warning("Change request %s has no reviewer to notify", cr.pk)
        return 0

    messages = []
    for user in reviewers:
        msg = mail.build(
            "admin_review_needed",
            subject=f"Waiting for you: {cr.summary}",
            to=user.email,
            context={
                "name": user.display_name or user.email,
                "author": str(cr.requested_by),
                "summary": cr.summary,
                "area": cr.capability_label,
                "link": _url("admin:hq_review_detail", cr.pk),
                "queue_link": _url("admin:hq_reviews"),
            },
        )
        if msg:
            messages.append(msg)
    return mail.send_bulk(messages)


def notify_author_of_decision(change_request) -> bool:
    """Tell the author what happened — approved, changed, or turned down.

    The same message for all three outcomes on purpose. What differs is what it
    says, not whether it arrives: "declined" without a reason and "approved
    after I rewrote your second paragraph" without the diff are both ways of
    leaving somebody unable to do better next time.
    """
    cr = change_request
    outcome = {
        ChangeRequest.APPROVED: "approved",
        ChangeRequest.AMENDED: "amended",
        ChangeRequest.DECLINED: "declined",
    }.get(cr.status)
    if outcome is None:
        return False

    subject = {
        "approved": f"Approved: {cr.summary}",
        "amended": f"Approved with changes: {cr.summary}",
        "declined": f"Not published yet: {cr.summary}",
    }[outcome]

    return mail.send_template(
        "admin_review_decision",
        subject=subject,
        to=cr.requested_by.email,
        context={
            "name": cr.requested_by.display_name or cr.requested_by.email,
            "outcome": outcome,
            "summary": cr.summary,
            "area": cr.capability_label,
            "reviewer": str(cr.reviewed_by) if cr.reviewed_by else "A GoodTip administrator",
            "feedback": cr.feedback,
            "changed": cr.changed_fields(),
            "failed": bool(cr.apply_error),
            "link": _url("admin:hq_my_work"),
        },
    )


def notify_task_assigned(task) -> bool:
    return mail.send_template(
        "admin_task",
        subject=f"For you: {task.title}",
        to=task.assigned_to.email,
        context={
            "name": task.assigned_to.display_name or task.assigned_to.email,
            "title": task.title,
            "detail": task.detail,
            "assigner": str(task.assigned_by) if task.assigned_by else "A GoodTip administrator",
            "link": _url("admin:hq_my_work"),
        },
    )
