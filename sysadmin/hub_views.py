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
    """Level one: what the database is FOR, in ten cards.

    THE CARDS CARRY HOW MUCH IS IN THEM. "Tipping — 4 tables" says a group
    exists; "Tipping — 4 tables, 25,663 rows" says whether it is the one with
    the data in it, which is the question somebody standing here is actually
    asking. It is also the cheapest way to spot a sync that has not run.

    That is one COUNT per table, about thirty on this screen. They are cheap
    counts on indexed tables and this page is opened a handful of times a day
    by a handful of people; _row_count swallows a failure per table, so a
    model mid-migration costs a number rather than the page.
    """
    ctx = admin.site.each_context(request)
    cats = hub.categories(ctx.get("available_apps"), request.path)
    for cat in cats:
        cat["href"] = _url("admin:hq_tables_category", cat["key"])
        counted = [_row_count(t["ref"]) for t in cat["tables"]]
        known = [n for n in counted if n is not None]
        # None, not 0, when nothing could be counted: "0 rows" is a claim
        # about the data, and this would be a claim about the database being
        # unavailable.
        cat["rows"] = sum(known) if known else None
    ctx.update(
        title="Tables & models", categories=cats,
        total_tables=sum(c["count"] for c in cats),
        total_rows=sum(c["rows"] or 0 for c in cats),
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

    WHAT is in the section comes from hub.HQ_SCREENS — the same list the rail
    reads, so the menu and this page cannot disagree about what HQ contains.
    What is WAITING is counted here, because a count is a fact about this
    minute and the rail draws itself on every screen in the admin.

    Each screen asks the capability it leads to rather than `is_superuser`. A
    restricted administrator is not a superuser, and hiding these from them
    would hide the very screens their account was created to use.
    """
    from admin_panel.models import Enquiry, NewsPost

    cards = hub.screens(hub.HQ_SCREENS, lambda key: access.can(request.user, key))

    try:
        open_enquiries = Enquiry.objects.filter(status=Enquiry.STATUS_NEW).count()
    except Exception:
        open_enquiries = None
    try:
        drafts = NewsPost.objects.filter(is_published=False).count()
    except Exception:
        drafts = None

    notes = {
        "news": (f"{drafts} draft{'' if drafts == 1 else 's'}", False) if drafts else None,
        "enquiries": (f"{open_enquiries} open", True) if open_enquiries else None,
    }
    for card in cards:
        note = notes.get(card["key"])
        if note:
            card["note"], card["hot"] = note

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

    cards = hub.screens(
        hub.TEAM_SCREENS, lambda key: access.can(user, key), full=full)

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

    notes = {
        "my_work": (f"{my_tasks} open", True) if my_tasks else None,
        "reviews": (f"{to_review} waiting", True) if to_review else None,
    }
    for card in cards:
        note = notes.get(card["key"])
        if note:
            card["note"], card["hot"] = note

    return render(request, "admin/hub/cards.html", _ctx(
        request, "Your team",
        cards=cards,
        hub_title="Your team",
        hub_lead="Who administers GoodTip, what each of them is trusted with, "
                 "and everything waiting on somebody.",
        hub_icon="ic-f-shield-star",
    ))
