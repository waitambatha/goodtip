"""Enforce the admin's second factor on every admin request.

Middleware rather than an AdminSite subclass or a decorator, for one reason:
coverage. The admin is not a single view, it is a URL tree that grows every
time a model is registered — plus autocomplete endpoints, the doc views, and
whatever a third-party app mounts inside it. A gate applied per-view is a gate
somebody forgets to apply, and the thing being forgotten here is access to
every record in the product. One check across the prefix cannot be skipped.
"""
from __future__ import annotations

from django.shortcuts import redirect
from django.urls import reverse

from . import otp


class AdminOTPMiddleware:
    """Redirect authenticated staff to the code screen until they verify.

    Deliberately does NOT gate anonymous requests: those already land on the
    admin's own login page, and intercepting them first would mean asking for
    a code before knowing whose code to ask for.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if self._needs_gate(request):
            # Remember where they were headed so verifying lands them there
            # rather than dumping everyone on the admin index.
            request.session[otp.NEXT_KEY] = request.get_full_path()
            return redirect("sysadmin:admin_verify")
        return self.get_response(request)

    def _needs_gate(self, request) -> bool:
        path = request.path
        if not path.startswith("/admin/"):
            return False
        # The gate's own screens, and the doors out. Logging out while
        # half-verified has to keep working, or a locked-out admin has no way
        # to try a different account.
        for exempt in (
            reverse("sysadmin:admin_verify"),
            reverse("sysadmin:admin_verify_resend"),
            # Cancel signs them out. Gating it is what turns "I typed the wrong
            # address" into a genuine lockout: bounced back to the code screen
            # for an account whose mail they cannot read, with no way to reach
            # a different one. Caught by the test of the same name.
            reverse("sysadmin:admin_verify_cancel"),
            "/admin/login/",
            "/admin/logout/",
        ):
            if path == exempt:
                return False
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return False        # the admin's own login page handles these
        if not (user.is_staff or user.is_superuser):
            return False        # Django will refuse them anyway
        return not otp.is_verified(request.session)
