"""The decorator that puts a capability in front of a screen.

Replaces `@superuser_required` on everything a restricted administrator might
be given. It does three things in one place so that no screen has to remember
all three:

  * refuses outright if they do not hold the capability;
  * captures the submission if they hold it "needs approval";
  * otherwise gets out of the way.

The refusal is a page rather than a bare 403, because the person hitting it is
a colleague who was told they are an administrator, and "Forbidden" is not an
explanation of why the button they were given does nothing.
"""
from functools import wraps

from django.contrib import messages
from django.shortcuts import redirect, render

from . import access, capabilities
from .review import HeldForReview, NotAllowed, guard


def requires(capability: str, summary=None, *, to_submit=None):
    """Guard a control-plane screen with one capability.

    `summary` builds the one-line description shown in the review queue. Pass a
    callable taking the request; anything else is used as a literal.

    `to_submit` names a SECOND capability needed only to post. Some screens
    read and write in one place — the enquiry page shows the message and
    carries the reply box — and gating the whole thing on the write capability
    would mean somebody given "read enquiries" could not open one. Reading is
    checked on the way in, replying on the way out, and only the reply is ever
    held for review.
    """

    def make_summary(request):
        if callable(summary):
            try:
                return summary(request)
            except Exception:  # noqa: BLE001 — a label must never break a save
                pass
        elif summary:
            return str(summary)
        return capabilities.label(capability)

    def decorator(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            if not access.is_admin(request.user):
                return redirect("/admin/login/")
            needed = capability
            if to_submit and request.method == "POST":
                needed = to_submit
            try:
                guard(request, needed, make_summary)
            except NotAllowed:
                return render(request, "hq/not_allowed.html", {
                    "capability": capabilities.get(needed),
                }, status=403)
            except HeldForReview as held:
                messages.success(
                    request,
                    "Sent for approval. A full-access administrator will look at "
                    "it and you'll get an email either way — nothing has changed "
                    "on the site yet.",
                )
                return redirect("admin:hq_my_work")
            return view(request, *args, **kwargs)

        # Read by the menu so it can hide what somebody cannot reach.
        wrapped.gt_capability = capability
        return wrapped

    return decorator
