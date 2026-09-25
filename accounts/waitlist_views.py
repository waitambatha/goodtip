"""The waiting list's own pages: sign up, confirm, sign in, and "you're on the list".

Everything is under /coming-soon/ so the staging gate lets it through on the
public site without any change to the gate, and none of it touches the real
login — see accounts/waitlist.py for the boundary. The endpoints answer JSON to
the pop-ups in gt-waitlist.js and redirect for anything else.
"""
from django.core.cache import cache
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST

from . import waitlist
from .form_replies import json_error, wants_json
from .models import LaunchSignup, WaitlistCampaign


def _ip(request) -> str:
    fwd = request.META.get("HTTP_X_FORWARDED_FOR", "")
    return (fwd.split(",")[0].strip() or request.META.get("REMOTE_ADDR", "")) or "?"


def _throttled(request, bucket: str, limit: int, window: int) -> bool:
    """Count this request against the caller's address; True once it is over."""
    key = f"wl:{bucket}:{_ip(request)}"
    try:
        cache.add(key, 0, window)
        return cache.incr(key) > limit
    except Exception:  # noqa: BLE001 — a cache hiccup must not lock people out
        return False


def _back(request):
    return redirect("coming_soon")


def _slow_down():
    return json_error("Too many tries from this connection. Give it a few minutes and try again.")


@require_http_methods(["GET", "POST"])
def join(request):
    if request.method != "POST" or not wants_json(request):
        return _back(request)
    if _throttled(request, "join", 12, 3600):
        return _slow_down()
    res = waitlist.start_signup(request.POST, source_page=request.POST.get("source_page") or "coming-soon")
    if not res.ok:
        return json_error(res.error)
    return JsonResponse({"ok": True, "email": res.row.email})


@require_http_methods(["GET", "POST"])
def verify(request):
    if request.method != "POST" or not wants_json(request):
        return _back(request)
    if _throttled(request, "verify", 30, 3600):
        return _slow_down()
    res = waitlist.confirm_code(request.POST.get("email"), request.POST.get("code"))
    if not res.ok:
        return json_error(res.error)
    waitlist.log_in(request, res.row)
    return JsonResponse({"ok": True, "next": "/coming-soon/me/"})


@require_http_methods(["GET", "POST"])
def resend(request):
    if request.method != "POST" or not wants_json(request):
        return _back(request)
    if _throttled(request, "resend", 10, 3600):
        return _slow_down()
    res = waitlist.resend_code(request.POST.get("email"))
    return JsonResponse({"ok": True}) if res.ok else json_error(res.error)


@require_http_methods(["GET", "POST"])
def signin(request):
    if request.method != "POST" or not wants_json(request):
        return _back(request)
    if _throttled(request, "signin", 40, 900):
        return _slow_down()
    res = waitlist.sign_in(request.POST.get("email"), request.POST.get("password"))
    if not res.ok:
        return json_error(res.error)
    waitlist.log_in(request, res.row)
    return JsonResponse({"ok": True, "next": "/coming-soon/me/"})


@require_POST
def signout(request):
    waitlist.log_out(request)
    return redirect("coming_soon")


# The clips shown as "a look inside" on the member page, in order. Names are
# the stems in accounts.trailer.TRAILER_CHAPTERS, so a clip cannot be listed
# here without a recording behind it (a test walks this list).
MEMBER_REEL = ["picks", "matchreader", "ladder", "leaderboard", "wall", "charity-vote"]


def _progress(member, site_open: bool) -> list[dict]:
    """Joined -> confirmed -> invited -> live, each done / current / to come."""
    steps = [
        {"key": "joined", "label": "Joined the list", "when": member.created_at, "done": True},
        {"key": "confirmed", "label": "Email confirmed", "when": member.email_verified_at,
         "done": member.is_verified},
        {"key": "invited", "label": "Invitation sent", "when": member.notified_at, "done": member.is_invited},
        {"key": "live", "label": "GoodTip is on", "when": None, "done": site_open},
    ]
    current_set = False
    for st in steps:
        if st["done"]:
            st["state"] = "done"
        elif not current_set:
            st["state"], current_set = "current", True
        else:
            st["state"] = "todo"
    return steps


@require_http_methods(["GET", "POST"])
def home(request):
    """What a member of the waiting list sees: their place, their details, what is next."""
    from goodtip.staging_gate import is_holding_visitor

    from .trailer import TRAILER_CHAPTERS
    from .views import launch_signup_context

    member = waitlist.current_member(request)
    if member is None:
        return redirect("/coming-soon/#signin")

    saved = ""
    error = ""
    if request.method == "POST":
        name = (request.POST.get("name") or "").strip()[:120]
        if not name:
            error = "Your name can't be empty."
        else:
            member.name = name
            member.org_type = waitlist._clean_choice(request.POST.get("org_type"), LaunchSignup.ORG_TYPE_CHOICES)
            member.current_platform = waitlist._clean_choice(
                request.POST.get("current_platform"), LaunchSignup.PLATFORM_CHOICES,
            )
            member.save(update_fields=["name", "org_type", "current_platform"])
            saved = "details"
        if wants_json(request):
            return JsonResponse({"ok": True, "name": member.name}) if saved else json_error(error)

    site_open = not is_holding_visitor(request)
    launch_at = WaitlistCampaign.current().launch_at
    by_clip = {c["clip"]: c for c in TRAILER_CHAPTERS}
    return render(request, "public/waitlist_home.html", {
        "member": member,
        "position": member.position(),
        "total": LaunchSignup.objects.count(),
        "saved": saved,
        "error": error,
        "can_open_pages": site_open,
        "first_name": waitlist.first_name_of(member.name),
        "steps": _progress(member, site_open),
        # No date set, or it has passed: no countdown. Never a made-up one.
        "launch_at": launch_at if launch_at and launch_at > timezone.now() else None,
        "reel": [by_clip[k] for k in MEMBER_REEL if k in by_clip],
        **launch_signup_context(),
    })
