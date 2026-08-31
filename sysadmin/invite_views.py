"""Accepting an administrator invitation.

Three things happen on one page, in order, and none of them can be skipped:
the code from the email is checked, a password is chosen and validated against
the project's own rules, and only then does the account become usable.

WHY THE ACCOUNT STARTS SWITCHED OFF. It is created with `is_active=False` and
an unusable password, so between "you have been made an admin" and "you have
set a password" there is nothing to sign in to. That closes the window where an
account with real powers exists that nobody has proved they control — and it
means an invitation sent to a mistyped address grants that address nothing.
"""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth import login, password_validation
from django.core.exceptions import ValidationError
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import ensure_csrf_cookie

from . import capabilities, otp
from .models import AdminAuditEvent, AdminInvite


SESSION_KEY = "admin_invite_verified"


def _find(token: str):
    return AdminInvite.objects.select_related("access__user").filter(token=token).first()


@never_cache
@ensure_csrf_cookie
def accept(request, token: str):
    invite = _find(token)
    if invite is None:
        return render(request, "hq/invite_dead.html", {
            "reason": "That invitation link is not one we recognise.",
        }, status=404)
    if not invite.is_usable:
        return render(request, "hq/invite_dead.html", {
            "reason": (
                "That invitation has already been used."
                if invite.consumed_at
                else "That invitation has expired, or the code was entered "
                     "wrongly too many times."
            ),
            "expired": True,
        }, status=410)

    user = invite.access.user
    grants = list(invite.access.grants.all())
    ctx = {
        "invite": invite,
        "user": user,
        "is_full_access": invite.access.is_full_access,
        "direct": [capabilities.label(g.capability) for g in grants if not g.requires_approval],
        "reviewed": [capabilities.label(g.capability) for g in grants if g.requires_approval],
        "code_length": AdminInvite.CODE_LENGTH,
        "verified": request.session.get(SESSION_KEY) == invite.pk,
    }

    if request.method != "POST":
        return render(request, "hq/invite_accept.html", ctx)

    step = request.POST.get("step")

    if step == "code":
        code = (request.POST.get("code") or "").strip()
        if invite.verify(code):
            request.session[SESSION_KEY] = invite.pk
            ctx["verified"] = True
        else:
            left = max(0, AdminInvite.MAX_ATTEMPTS - invite.attempts)
            ctx["error"] = (
                f"That code isn't right. {left} attempt{'s' if left != 1 else ''} left."
                if left else
                "Too many wrong attempts. Ask whoever invited you to send a new one."
            )
        return render(request, "hq/invite_accept.html", ctx)

    if step == "password":
        if request.session.get(SESSION_KEY) != invite.pk:
            ctx["error"] = "Enter the code from your email first."
            return render(request, "hq/invite_accept.html", ctx)

        pw1 = request.POST.get("password1") or ""
        pw2 = request.POST.get("password2") or ""
        ctx["verified"] = True

        if pw1 != pw2:
            ctx["error"] = "Those two passwords don't match."
            return render(request, "hq/invite_accept.html", ctx)
        try:
            password_validation.validate_password(pw1, user)
        except ValidationError as exc:
            ctx["error"] = " ".join(exc.messages)
            return render(request, "hq/invite_accept.html", ctx)

        user.set_password(pw1)
        user.is_active = True
        user.email_verified_at = timezone.now()
        user.save()
        invite.consume()
        request.session.pop(SESSION_KEY, None)

        AdminAuditEvent.record(
            actor=user,
            action=AdminAuditEvent.INVITE_ACCEPTED,
            subject=user,
            summary=f"{user} set their password and activated their account",
        )

        login(request, user, backend="accounts.backends.EmailBackend")
        # They proved they hold the address by reading the code out of it, on
        # this device, moments ago. Making them do the admin's own email
        # second factor immediately afterwards asks the same question twice.
        otp.mark_verified(request.session)
        messages.success(
            request,
            f"You're all set, {user.display_name}. Here's what you can do.",
        )
        return redirect("admin:hq_my_work")

    return render(request, "hq/invite_accept.html", ctx)
