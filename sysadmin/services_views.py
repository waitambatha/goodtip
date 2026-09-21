"""Services — what the system does on its own, and how it has been getting on.

CLIENT, 20 SEP 2026: "in the super admin we also need to add Services. I do
not know why we did not think of that. In Services we have things like email
services where we monitor the email services and such, monitoring things like
that, your traffic and such — kind of services that our system does. And the
AI, like the Prefect; monitor things like the MatchReader, Group Recap, see how
they perform. In MatchReader, based on rounds and how it predicted, how is it,
strengths and all — things like that, just monitoring services."

WHAT COUNTS AS A SERVICE HERE
-----------------------------
Something the system does WITHOUT anybody pressing a button. That is the line,
and it is why the Wall is not on this page and the recap writer is: one is a
screen people use, the other runs on a timer and either worked or did not.
Five of them, and each answers the same three questions in the same order —
is it on, how has it been doing, and when did it last do anything.

NOTHING HERE IS INVENTED
------------------------
Every number on this screen is read from a table that something actually
wrote. Where there is no such table the screen says so in as many words rather
than showing a plausible zero: a monitoring page whose figures might be
decorative is worse than no monitoring page, because it gets believed.

That is why traffic is the short panel. There is no page-view tracking
installed on this site — analytics is an open item on the client's own task
sheet, assigned elsewhere — so what is here is sign-ins, which IS measured
(sysadmin.LoginEvent), and a sentence saying plainly what is not.

MATCHREADER IS TWO DIFFERENT QUESTIONS
--------------------------------------
"How good is the model" is answered by the fitted version: out-of-sample
accuracy against the always-pick-home baseline it has to beat. "How has it
been going lately" would need every prediction it has shown to have been
recorded, and they are not — they are computed when a card is drawn. Rather
than quietly answer the first and label it the second, both are stated, and
the gap between them is named as the gap it is.
"""
from datetime import timedelta

from django.contrib import admin
from django.db.models import Avg, Count, Q
from django.http import Http404
from django.shortcuts import render
from django.utils import timezone

from . import access


def _ctx(request, title, **extra):
    ctx = admin.site.each_context(request)
    ctx.update(title=title, **extra)
    return ctx


def _guard(request):
    if not access.can(request.user, "data.report"):
        raise Http404("No services screen here.")


def _recent(rows, when_field, why_field=None):
    """Recent rows flattened to {what, when}.

    The template used to be handed model instances and reached for whichever
    timestamp field it thought they had. That works right up until two
    services keep their time under different names — SyncRun has started_at
    and MailLog has created_at — at which point the page raises on the panel
    that is trying to tell you something went wrong, which is the worst
    possible moment for it. The view knows which field it is; the template
    should not have to guess.
    """
    out = []
    for row in rows:
        out.append({
            "what": str(row),
            "when": getattr(row, when_field, None),
            # WHY it failed, where the service records one. A list of failures
            # that does not say what went wrong is a list that sends everybody
            # who reads it to the server log, which is the thing this screen
            # exists to save them from.
            "why": (getattr(row, why_field, "") or "") if why_field else "",
        })
    return out


def _pct(part, whole):
    if not whole:
        return None
    return round(100 * part / whole)


def _state(ok_ratio, *, warn=90, bad=70):
    """Three words, so every panel reports health the same way.

    Ratios rather than raw counts because "eleven failures" means nothing
    without the denominator — eleven out of twelve thousand is a healthy
    service and eleven out of twelve is a broken one.
    """
    if ok_ratio is None:
        return "idle"
    if ok_ratio >= warn:
        return "good"
    if ok_ratio >= bad:
        return "watch"
    return "bad"


# ---------------------------------------------------------------------------
# 1. Email
# ---------------------------------------------------------------------------

def _email_panel(since):
    from django.conf import settings

    from .models import MailLog

    rows = MailLog.objects.filter(created_at__gte=since)
    total = rows.count()
    sent = rows.filter(ok=True).count()
    failed = total - sent
    messages = sum(rows.values_list("recipients", flat=True)) if total else 0
    ratio = _pct(sent, total)

    backend = getattr(settings, "EMAIL_BACKEND", "")
    allowlist = getattr(settings, "EMAIL_ALLOWLIST", "") or ""
    token = bool(getattr(settings, "POSTMARK_SERVER_TOKEN", ""))

    return {
        "key": "email",
        "label": "Email",
        "icon": "ic-mail",
        "blurb": "Sign-in codes, invitations, reminders and the round wrap-ups. "
                 "Delivered through Postmark.",
        "state": "bad" if not token else _state(ratio),
        "headline": f"{messages} message{'' if messages == 1 else 's'}",
        "headline_sub": f"in {total} batch{'es' if total != 1 else ''}",
        "stats": [
            ("Batches accepted", sent),
            ("Batches that failed or were held", failed),
            ("Success rate", f"{ratio}%" if ratio is not None else "—"),
        ],
        "facts": [
            ("Backend", backend.rsplit(".", 1)[-1] or "—"),
            ("Postmark token", "set" if token else "NOT SET — nothing will send"),
            ("Allowlist", allowlist or "off (everyone)"),
        ],
        "recent": _recent(rows.filter(ok=False)[:6], "created_at", "detail"),
        "recent_label": "Recent failures and holds",
        "empty": "Nothing has been sent in this window.",
    }


# ---------------------------------------------------------------------------
# 2. Fixture syncs
# ---------------------------------------------------------------------------

def _sync_panel(since):
    from data_sync.models import SyncRun, SyncSchedule

    rows = SyncRun.objects.filter(started_at__gte=since)
    total = rows.count()
    ok = rows.filter(ok=True).count()
    ratio = _pct(ok, total)
    touched = sum(rows.values_list("matches_touched", flat=True)) if total else 0

    by_kind = list(
        rows.values("kind")
        .annotate(n=Count("id"), good=Count("id", filter=Q(ok=True)))
        .order_by("kind")
    )
    for row in by_kind:
        row["rate"] = _pct(row["good"], row["n"])

    schedule = list(SyncSchedule.objects.all().order_by("kind"))

    return {
        "key": "sync",
        "label": "Fixture and results sync",
        "icon": "ic-spark",
        "blurb": "Draws, live scores, final results and the ladders, pulled "
                 "from the feeds on a timer. Nothing here is typed in by hand.",
        "state": _state(ratio),
        "headline": f"{touched} fixture{'' if touched == 1 else 's'} touched",
        "headline_sub": f"across {total} run{'' if total == 1 else 's'}",
        "stats": [
            ("Runs", total),
            ("Failed", total - ok),
            ("Success rate", f"{ratio}%" if ratio is not None else "—"),
        ],
        "facts": [(r["kind"].title(), f"{r['n']} runs · {r['rate']}% ok") for r in by_kind],
        "recent": _recent(rows.filter(ok=False)[:6], "started_at", "message"),
        "recent_label": "Recent failures",
        "schedule": schedule,
        "empty": "No sync has run in this window.",
    }


# ---------------------------------------------------------------------------
# 3. Prefect — the chat moderator
# ---------------------------------------------------------------------------

def _prefect_panel(since):
    from orgs.models import ChatFlag

    rows = ChatFlag.objects.filter(created_at__gte=since)
    total = rows.count()
    by_prefect = rows.filter(raised_by__isnull=True).count()
    by_member = total - by_prefect
    reviewed = rows.exclude(status=ChatFlag.STATUS_OPEN).count()
    actioned = rows.filter(status=ChatFlag.STATUS_ACTIONED).count()
    open_now = ChatFlag.objects.filter(status=ChatFlag.STATUS_OPEN).count()

    # PRECISION, and it is named as a proxy rather than as a measurement.
    # "A reviewer did something about it" is the closest thing this product
    # holds to "the flag was right", and it is not the same thing: a reviewer
    # may clear a flag that was correct but not worth acting on.
    precision = _pct(actioned, reviewed)

    return {
        "key": "prefect",
        "label": "Prefect",
        "icon": "ic-shield-check",
        "blurb": "Reads the rooms and raises anything that needs a person's "
                 "eye. It never hides a message or acts on its own — every "
                 "consequence in the feature is applied by a human.",
        "state": "watch" if open_now > 20 else "good",
        "headline": f"{total} flag{'' if total == 1 else 's'} raised",
        "headline_sub": f"{by_prefect} by Prefect, {by_member} by members",
        "stats": [
            ("Waiting for review", open_now),
            ("Reviewed in this window", reviewed),
            ("Acted on / reviewed", f"{precision}%" if precision is not None else "—"),
        ],
        "facts": [
            ("Raised by Prefect", by_prefect),
            ("Reported by a member", by_member),
        ],
        "recent": [],
        "note": "“Acted on” is the nearest thing held to “the flag was right”, "
                "and it is not the same thing — a reviewer can agree with a "
                "flag and still decide it needs nothing done.",
        "empty": "Nothing has been flagged in this window.",
    }


# ---------------------------------------------------------------------------
# 4. MatchReader
# ---------------------------------------------------------------------------

def _matchreader_panel(since):
    from matchreader.models import ModelVersion

    active = list(
        ModelVersion.objects.filter(is_active=True).select_related("series")
    )
    latest = list(
        ModelVersion.objects.select_related("series").order_by("-created_at")[:6]
    )

    edges = []
    for v in active:
        edge = round((v.accuracy - (v.baseline_accuracy or 0)) * 100, 1)
        edges.append({
            "series": v.series.name,
            "accuracy": round(v.accuracy * 100, 1),
            "baseline": round((v.baseline_accuracy or 0) * 100, 1),
            "edge": edge,
            "samples": v.test_samples,
            "fitted": v.created_at,
            "beats_baseline": edge > 0,
        })

    best = max((e["edge"] for e in edges), default=None)
    state = "idle" if not edges else ("good" if best and best > 0 else "bad")

    return {
        "key": "matchreader",
        "label": "MatchReader",
        "icon": "ic-f-spark",
        "blurb": "Reads the last five for both sides and says who it leans "
                 "towards. Offered as a read, never as a tip — nothing it says "
                 "is entered on anybody's behalf.",
        "state": state,
        "headline": (f"+{best} pts over baseline" if best and best > 0
                     else ("no edge" if edges else "not fitted")),
        "headline_sub": f"{len(active)} active model{'' if len(active) == 1 else 's'}",
        "stats": [
            ("Competitions with a model", len(active)),
            ("Best edge over always-pick-home", f"{best} pts" if best is not None else "—"),
        ],
        "edges": edges,
        "versions": latest,
        "note": "Accuracy here is OUT OF SAMPLE: each model was fitted on some "
                "seasons and scored on seasons it never saw, against the "
                "always-pick-home rate it has to beat to be worth shipping. "
                "How it has done round by round SINCE going live is a different "
                "question, and this product does not answer it — a prediction is "
                "computed when a card is drawn and is not stored, so there is "
                "nothing to score. Recording them is what that would take.",
        "recent": [],
        "empty": "No model has been fitted yet.",
    }


# ---------------------------------------------------------------------------
# 5. Group recaps
# ---------------------------------------------------------------------------

def _recap_panel(since):
    from orgs.models import RoundRecap

    rows = RoundRecap.objects.filter(created_at__gte=since)
    total = rows.count()
    fallback = rows.filter(fallback_used=True).count()
    review = RoundRecap.objects.filter(needs_review=True).count()
    written = _pct(total - fallback, total)
    last = RoundRecap.objects.order_by("-created_at").first()

    return {
        "key": "recaps",
        "label": "Group recaps",
        "icon": "ic-doc",
        "blurb": "Writes the round up for every comp once it is graded, and "
                 "posts it to the Wall with the ladder as it stood.",
        "state": "watch" if review else _state(written, warn=70, bad=40),
        "headline": f"{total} recap{'' if total == 1 else 's'} written",
        "headline_sub": f"{fallback} fell back to the plain line" if fallback else "none fell back",
        "stats": [
            ("Written in full", total - fallback),
            ("Fell back", fallback),
            ("Flagged for review", review),
        ],
        "facts": [
            ("Last written", last.created_at if last else "never"),
        ],
        "recent": [],
        "note": "A recap falls back to a plain factual line when the round was "
                "too thin to find two sentences in. That is the feature "
                "working, not failing — a high rate means quiet rounds.",
        "empty": "No recap has been written in this window.",
    }


# ---------------------------------------------------------------------------
# 6. Traffic
# ---------------------------------------------------------------------------

def _traffic_panel(since):
    from django.contrib.auth import get_user_model

    from .models import LoginEvent

    rows = LoginEvent.objects.filter(created_at__gte=since)
    total = rows.count()
    ok = rows.filter(success=True).count()
    people = rows.filter(success=True).values("user_id").distinct().count()
    User = get_user_model()
    active = User.objects.filter(last_login__gte=since).count()

    return {
        "key": "traffic",
        "label": "Traffic",
        "icon": "ic-chart",
        "blurb": "Who has actually been in. Sign-ins are measured; page views "
                 "are not.",
        "state": "good" if ok else "idle",
        "headline": f"{people} account{'' if people == 1 else 's'}",
        "headline_sub": f"signed in, {total} attempt{'' if total == 1 else 's'}",
        "stats": [
            ("Successful sign-ins", ok),
            ("Failed attempts", total - ok),
            ("Accounts active", active),
        ],
        "note": "THERE IS NO PAGE-VIEW TRACKING ON THIS SITE. No analytics tag "
                "is installed and nothing records a visit, so this panel counts "
                "sign-ins and stops there rather than showing a number it would "
                "have had to invent. Analytics and conversion tracking are an "
                "open item on the task sheet and are assigned outside the build.",
        "recent": [],
        "empty": "Nobody has signed in during this window.",
    }


# ---------------------------------------------------------------------------

WINDOWS = [("24h", "Last 24 hours", 1), ("7d", "Last 7 days", 7), ("30d", "Last 30 days", 30)]


def services_hub(request):
    """One screen, six panels, one window across all of them.

    THE WINDOW IS SHARED on purpose. Six panels each with their own period is
    six numbers that cannot be put beside one another — "were the emails
    failing while the syncs were?" is the question somebody opens this page
    with, and it can only be answered if both are describing the same hours.
    """
    _guard(request)

    key = request.GET.get("window") or "7d"
    days = next((d for k, _l, d in WINDOWS if k == key), 7)
    since = timezone.now() - timedelta(days=days)

    panels = []
    for build in (_email_panel, _sync_panel, _prefect_panel,
                  _matchreader_panel, _recap_panel, _traffic_panel):
        try:
            panels.append(build(since))
        except Exception as exc:  # noqa: BLE001
            # ONE PANEL FAILING MUST NOT TAKE THE PAGE. This is the screen
            # somebody opens BECAUSE something is wrong, and a 500 here would
            # hide the five services that are fine behind the one that is not.
            panels.append({
                "key": "unknown", "label": "A service could not be read",
                "icon": "ic-shield-check", "state": "bad",
                "blurb": str(exc)[:200], "stats": [], "recent": [],
                "headline": "unavailable", "headline_sub": "",
            })

    return render(request, "admin/hub/services.html", _ctx(
        request, "Services",
        panels=panels,
        windows=WINDOWS,
        window=key,
        window_label=next((l for k, l, _d in WINDOWS if k == key), "Last 7 days"),
        hub_title="Services",
        hub_lead="Everything the system does on its own — the email, the feeds, "
                 "the moderator and the two writers. How each has been getting "
                 "on, and when it last did anything.",
        hub_icon="ic-chart",
    ))
