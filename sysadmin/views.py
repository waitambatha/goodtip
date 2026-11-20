"""The admin's second-factor screens. See sysadmin.otp for why they exist."""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth import logout
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from . import otp


def _target(request) -> str:
    """Where to land after verifying: where they were going, or the index."""
    nxt = request.session.get(otp.NEXT_KEY) or "/admin/"
    # Only ever inside the admin. A stored `next` is attacker-influencable in
    # principle, and an open redirect out of an authentication step is the
    # classic way to turn one into a phishing hop.
    return nxt if nxt.startswith("/admin/") else "/admin/"


def admin_verify(request):
    """Ask for the emailed code, and let them in when it checks out."""
    user = request.user
    if not user.is_authenticated or not (user.is_staff or user.is_superuser):
        return redirect("/admin/login/")
    if otp.is_verified(request.session):
        return redirect(_target(request))

    # Issue on first arrival, not on every render — a reload should not
    # invalidate the code the person is currently typing in.
    if not request.session.get("admin_otp_sent"):
        otp.issue_and_send(user, request)
        request.session["admin_otp_sent"] = True

    error = ""
    if request.method == "POST":
        if otp.check(user, request.POST.get("code", "")):
            otp.mark_verified(request.session)
            request.session.pop("admin_otp_sent", None)
            dest = _target(request)
            request.session.pop(otp.NEXT_KEY, None)
            # Rotate the session key on a successful second factor, so a
            # session id captured before verification is not one that is now
            # trusted with the control plane.
            request.session.cycle_key()
            otp.mark_verified(request.session)
            return redirect(dest)
        error = "That code isn't right, or it's expired. Try again, or send a new one."

    return render(request, "admin/verify.html", {
        "email": user.email,
        "error": error,
        "title": "Admin verification",
    })


def admin_verify_link(request, token):
    """The "Open the admin" button in the admin code email.

    Works only for the staff member already signed in in THIS browser — the
    same session the code screen is waiting in — so the link is never a way
    into the control plane by itself. Checked before the code is tried, so a
    link opened elsewhere (or by a mail scanner) never uses the code up.
    """
    from accounts.code_links import read
    from accounts.models import LoginCode

    user = request.user
    if not user.is_authenticated or not (user.is_staff or user.is_superuser):
        return redirect("/admin/login/")
    if otp.is_verified(request.session):
        return redirect(_target(request))
    parsed = read(token, int(LoginCode.TTL.total_seconds()))
    row = None
    if parsed:
        row = LoginCode.objects.filter(pk=parsed[0], purpose=LoginCode.PURPOSE_ADMIN, user=user).first()
    if row is None or not row.verify(parsed[1]):
        messages.error(request, "That link has expired or was already used. Enter the code, or send a new one.")
        return redirect("sysadmin:admin_verify")
    dest = _target(request)
    request.session.pop("admin_otp_sent", None)
    request.session.pop(otp.NEXT_KEY, None)
    request.session.cycle_key()
    otp.mark_verified(request.session)
    return redirect(dest)


@require_POST
def admin_verify_resend(request):
    user = request.user
    if not user.is_authenticated or not (user.is_staff or user.is_superuser):
        return redirect("/admin/login/")
    otp.issue_and_send(user, request)
    messages.success(request, "A new code is on its way.")
    return redirect("sysadmin:admin_verify")


def admin_verify_cancel(request):
    """Give up and sign out — the way back to a different account."""
    otp.clear(request.session)
    logout(request)
    return redirect("/admin/login/")
