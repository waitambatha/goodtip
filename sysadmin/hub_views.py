"""The hub screens the flat menu opens onto.

THE SHAPE
---------
Every menu item is now one link to one page of cards, and every card is one
link to the thing itself. Two levels, never a disclosure triangle:

    Tables & models  ->  ten category cards  ->  that category's tables  ->  a table
    Security         ->  three cards         ->  the log
    HQ               ->  six cards           ->  the editor
    Your team        ->  four cards          ->  the queue

A dropdown in a rail hides its contents behind a click that leaves you exactly
where you started, and ten of them side by side means the menu is taller than
the window before you have opened any. A card can carry an icon, a sentence
saying what the thing is for, and the number waiting inside it — none of which
fits on a rail row, and all of which is the reason somebody clicks.

WHY THESE ARE PLAIN VIEWS
-------------------------
They are wired in through `admin.site.get_urls()` alongside the HQ screens and
wrapped in `admin_view`, so the gate, the login redirect and the permission
check are Django's own. `each_context()` gives them `available_apps` — already
filtered by permission — which is the only thing the card map is matched
against, so no screen here can show a table its viewer could not open.
"""
from django.contrib import admin
from django.http import Http404
from django.shortcuts import render
from django.urls import NoReverseMatch, reverse

from . import access, hub
from .models import AdminTask, AuditLog, ChangeRequest, LoginEvent


def _ctx(request, title, **extra):
    ctx = admin.site.each_context(request)
    ctx.update(title=title, **extra)
    return ctx


def _url(name, *args):
    try:
        return reverse(name, args=args)
    except NoReverseMatch:
        return ""


def _row_count(ref):
    """How many rows a table holds, or None if it cannot be counted right now.

    Guarded rather than trusted: a hub screen must still render while a model
    is mid-migration. A missing number costs a line on a card; an exception
    costs the whole page.
    """
    from django.apps import apps

    try:
        app_label, model_name = ref.split(".", 1)
        return apps.get_model(app_label, model_name).objects.count()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Tables & models
# ---------------------------------------------------------------------------

def tables_hub(request):
    ctx = admin.site.each_context(request)
    cats = hub.categories(ctx.get("available_apps"), request.path)
    for cat in cats:
        cat["href"] = _url("admin:hq_tables_category", cat["key"])
    ctx.update(
        title="Tables & models", categories=cats,
        total_tables=sum(c["count"] for c in cats),
    )
    return render(request, "admin/hub/tables.html", ctx)


def tables_category(request, key):
    ctx = admin.site.each_context(request)
    cat = hub.category(ctx.get("available_apps"), key, request.path)
    if cat is None:
        raise Http404("No such category")
    for table in cat["tables"]:
        table["rows"] = _row_count(table["ref"])
    ctx.update(
        title=cat["label"], category=cat,
        back_url=_url("admin:hq_tables"), back_label="Tables & models",
    )
    return render(request, "admin/hub/tables_category.html", ctx)


# ---------------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------------

def security_hub(request):
    """Who got in, and who changed what.

    The counts are the point of the cards. "Audit log" as a menu row tells you
    a log exists; "Audit log — 214 changes, 9 today" tells you whether opening
    it is worth your afternoon.
    """
    from django.utils import timezone

    ctx = admin.site.each_context(request)
    cards = hub.security_tables(ctx.get("available_apps"), request.path)

    since = timezone.now() - timezone.timedelta(hours=24)
    counts = {}
    try:
        counts["sysadmin.loginevent"] = (
            LoginEvent.objects.filter(created_at__gte=since).count(),
            LoginEvent.objects.filter(created_at__gte=since, success=False).count(),
        )
    except Exception:
        pass
    try:
        counts["sysadmin.auditlog"] = (
            AuditLog.objects.filter(action_time__gte=since).count(), 0,
        )
    except Exception:
        pass

    for card in cards:
        recent, bad = counts.get(card["ref"], (None, 0))
        card["recent"] = recent
        card["bad"] = bad

    # The changes themselves, newest first. Django writes a LogEntry on every
    # admin save and delete already, so "this table was edited, then, by them"
    # needs surfacing here rather than recording somewhere new — two records of
    # one event is two records that can disagree.
    try:
        recent_changes = list(
            AuditLog.objects.select_related("user", "content_type")
            .order_by("-action_time")[:12]
        )
    except Exception:
        recent_changes = []

    try:
        recent_logins = list(
            LoginEvent.objects.select_related("user").order_by("-created_at")[:8]
        )
    except Exception:
        recent_logins = []

    ctx.update(
        title="Security", cards=cards, recent_changes=recent_changes,
        recent_logins=recent_logins,
        audit_url=_url("admin:sysadmin_auditlog_changelist"),
        logins_url=_url("admin:sysadmin_loginevent_changelist"),
    )
    return render(request, "admin/hub/security.html", ctx)


# ---------------------------------------------------------------------------
# HQ
# ---------------------------------------------------------------------------

def hq_hub(request):
    """GoodTip's own work: the site's words, its inbox, its fixtures.

    Each card asks the capability it leads to rather than `is_superuser`. A
    restricted administrator is not a superuser, and hiding these from them
    would hide the very screens their account was created to use.
    """
    from admin_panel.models import Enquiry, NewsPost

    def cap(key):
        return access.can(request.user, key)

    try:
        open_enquiries = Enquiry.objects.filter(status=Enquiry.STATUS_NEW).count()
    except Exception:
        open_enquiries = None
    try:
        drafts = NewsPost.objects.filter(is_published=False).count()
    except Exception:
        drafts = None

    cards = [
        {
            "label": "News & blog", "icon": "ic-f-doc", "accent": hub.TEAL,
            "blurb": "Write, publish and email stories. Everything here reaches "
                     "every member's dashboard.",
            "href": _url("admin:hq_news"), "show": cap("news.write"),
            "note": (f"{drafts} draft{'' if drafts == 1 else 's'}"
                     if drafts else None),
        },
        {
            "label": "Enquiries inbox", "icon": "ic-f-mail", "accent": hub.GOLD,
            "blurb": "Messages sent through the public contact form, and what "
                     "was replied.",
            "href": _url("admin:hq_enquiries"), "show": cap("enquiries.read"),
            "note": (f"{open_enquiries} open" if open_enquiries else None),
            "hot": bool(open_enquiries),
        },
        {
            "label": "Pages", "icon": "ic-f-pages", "accent": hub.GREEN,
            "blurb": "The words and pictures on every page, public and "
                     "members-only. Nothing to mark up first.",
            "href": _url("admin:hq_pages"), "show": cap("pages.edit"),
        },
        {
            "label": "SEO", "icon": "ic-globe", "accent": hub.OCEAN,
            "blurb": "Titles, descriptions and share images — what Google and "
                     "Facebook show when a page is linked.",
            "href": _url("admin:hq_seo"), "show": cap("seo.edit"),
        },
        {
            "label": "Redirects", "icon": "ic-link", "accent": hub.PLUM,
            "blurb": "Point an old address at a new one so no link anybody has "
                     "shared ever dies.",
            "href": _url("admin:hq_redirects"), "show": cap("seo.redirects"),
        },
        {
            "label": "Sync panel", "icon": "ic-cloud-sync", "accent": hub.AMBER,
            "blurb": "Pull fixtures and results in, and see whether the last "
                     "run worked.",
            "href": _url("admin:hq_sync"), "show": cap("data.sync"),
        },
    ]
    cards = [c for c in cards if c["show"] and c["href"]]
    return render(request, "admin/hub/cards.html", _ctx(
        request, "HQ",
        cards=cards,
        hub_title="HQ",
        hub_lead="GoodTip's own work — the company's words, its inbox and its "
                 "fixtures. None of it belongs to any one organisation.",
        hub_icon="ic-f-org",
    ))


# ---------------------------------------------------------------------------
# Your team
# ---------------------------------------------------------------------------

def team_hub(request):
    full = access.is_full_access(request.user)
    user = request.user

    try:
        my_tasks = AdminTask.objects.filter(
            assigned_to=user, status=AdminTask.OPEN).count()
    except Exception:
        my_tasks = None
    try:
        to_review = (
            ChangeRequest.objects.filter(status=ChangeRequest.PENDING)
            .exclude(requested_by=user).count() if full else 0
        )
    except Exception:
        to_review = None

    cards = [
        {
            "label": "Your work", "icon": "ic-f-home", "accent": hub.GREEN,
            "blurb": "What you have been asked to do, what you are allowed to "
                     "do, and what came back from review.",
            "href": _url("admin:hq_my_work"), "show": True,
            "note": (f"{my_tasks} open" if my_tasks else None),
            "hot": bool(my_tasks),
        },
        {
            "label": "Waiting for review", "icon": "ic-f-clock", "accent": hub.GOLD,
            "blurb": "Changes raised by administrators who need your approval "
                     "before they go live.",
            "href": _url("admin:hq_reviews"), "show": full,
            "note": (f"{to_review} waiting" if to_review else None),
            "hot": bool(to_review),
        },
        {
            "label": "Administrators", "icon": "ic-f-shield-star", "accent": hub.RUST,
            "blurb": "Who can get in, exactly what each of them may do, and "
                     "which of it you see first.",
            "href": _url("admin:hq_team"), "show": full,
        },
        {
            "label": "Activity", "icon": "ic-clock", "accent": hub.PLUM,
            "blurb": "The record of what every administrator has done, in order.",
            "href": _url("admin:hq_activity"), "show": full,
        },
    ]
    cards = [c for c in cards if c["show"] and c["href"]]
    return render(request, "admin/hub/cards.html", _ctx(
        request, "Your team",
        cards=cards,
        hub_title="Your team",
        hub_lead="Who administers GoodTip, what each of them is trusted with, "
                 "and everything waiting on somebody.",
        hub_icon="ic-f-shield-star",
    ))
