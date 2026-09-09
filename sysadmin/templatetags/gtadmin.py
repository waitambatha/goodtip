"""Template helpers for the GoodTip admin skin.

Three jobs, and each of them exists because the stock admin cannot answer a
question the control plane is asked every day:

* handing templates the category map in sysadmin/hub.py, which turns Django's
  flat `available_apps` into groups named for the work rather than for the app
  label. The app list alone put Season between Sport and Charity with nothing
  separating catalog from tipping, and that is what a 30-entry rail looks like
  when nobody has grouped it.
* the dashboard numbers, including the little sparklines on the tiles — a
  count on its own says nothing about whether it is going up.
* what is waiting on somebody, which is the first thing the screen should say
  and the last thing a list of model tables can tell you.
"""
from django import template
from django.urls import NoReverseMatch, reverse
from django.utils import timezone

register = template.Library()


# ---------------------------------------------------------------------------
# The section map
# ---------------------------------------------------------------------------
#
# It moved. What the tables are, what each one is for and which colour it
# carries now lives in sysadmin/hub.py, because the hub screens and the
# dashboard both render it and neither of them is a template helper. This
# module keeps only the tags that hand it to a template.

MODEL_ICONS = {
    "user": "ic-users",
    "launchsignup": "ic-send",
    "group": "ic-people",
    "groupmember": "ic-users",
    "organisation": "ic-org",
    "membershiprequest": "ic-org-add",
    "plansubscription": "ic-coins",
    "wallpost": "ic-msg",
    "wallreply": "ic-send",
    "charity": "ic-heart",
    "charityvote": "ic-vote",
    "charityvoteballot": "ic-vote",
    "tip": "ic-target",
    "match": "ic-match",
    "round": "ic-calendar",
    "team": "ic-flag",
    "competition": "ic-trophy",
    "series": "ic-trophy",
    "sport": "ic-flag",
    "season": "ic-calendar",
    "state": "ic-pin",
    "organisationtype": "ic-sliders",
    "subcategory": "ic-sliders",
    "goodlistconfig": "ic-sliders",
    "newspost": "ic-doc",
    "pageseo": "ic-globe",
    "redirect": "ic-link",
    "syncrun": "ic-sync",
    "loginevent": "ic-shield",
    "auditlog": "ic-clock",
    "stresstestrun": "ic-flask",
}


@register.simple_tag
def model_icon(object_name):
    return MODEL_ICONS.get((object_name or "").lower(), "ic-doc")


@register.simple_tag
def safe_url(name, *args):
    """reverse() that yields "" instead of raising.

    The menu links across app boundaries; if one of those URLs is ever renamed
    the admin should lose a link, not 500 on every page it appears on.
    """
    try:
        return reverse(name, args=args)
    except NoReverseMatch:
        return ""


def _apps(context):
    return context.get("available_apps") or context.get("app_list") or []


def _path(context):
    request = context.get("request")
    return getattr(request, "path", "") or ""


@register.simple_tag(takes_context=True)
def gta_categories(context):
    """Every data category this administrator can see.

    Used by the dashboard's "Everything in the database" card. The hub screens
    call sysadmin.hub directly — a view has no reason to go through a template
    library to reach a plain function.
    """
    from sysadmin import hub

    cats = hub.categories(_apps(context), _path(context))
    for cat in cats:
        cat["href"] = safe_url("admin:hq_tables_category", cat["key"])
    return cats


@register.simple_tag(takes_context=True)
def gta_table_accent(context):
    """The colour of whichever table the current page belongs to, or {}.

    Set on the shell by base_site.html so a changelist and a change form wear
    the same colour as the card that was clicked to reach them. Purely a
    thread of continuity: the alternative is thirty screens that are identical
    until you read the heading.

    Carries `ink` as well as `accent` because the accent is used as a solid
    fill — the Add button — and white is unreadable on two of the seven. See
    sysadmin.hub.INK.
    """
    from sysadmin import hub

    accent = hub.accent_for_path(_apps(context), _path(context))
    if not accent:
        return {}
    return {"accent": accent, "ink": hub.INK.get(accent, hub.LIGHT_INK)}


@register.simple_tag(takes_context=True)
def gta_on_table_screen(context):
    """Whether this page is a table's own changelist, form or history.

    Those screens are reached through Tables & models and have no other home
    in the menu, so the rail marks that item current while you are on one.
    Leaving the rail blank is how people lose track of which of six sections
    they are inside.
    """
    return bool(gta_table_accent(context))


# Where "up one level" goes, per screen. Keyed by what the path looks like
# rather than by resolver name because Django's admin URL names are per-model
# and this needs one rule for all thirty of them.
@register.simple_tag(takes_context=True)
def gta_up(context):
    """The parent of the current screen: a real URL, not history.back().

    `history.back()` is not "up" — it is "wherever you were", which after a
    save is the form you just left, and after arriving from a link somebody
    sent you is a different site entirely. A hierarchy has an actual parent,
    and on these screens it is always known, so the button goes there.
    """
    from sysadmin import hub

    path = _path(context)
    index = safe_url("admin:index")
    tables = safe_url("admin:hq_tables")
    if not path or path == index:
        return {}

    # A record's own screens sit under their changelist: /admin/app/model/…
    for ref, row in hub.flatten(_apps(context)).items():
        url = row.get("admin_url") or ""
        if not url:
            continue
        if path == url:
            # A changelist belongs to whichever category card leads to it.
            for cat in hub.categories(_apps(context), path):
                if any(t["ref"] == ref for t in cat["tables"]):
                    return {
                        "url": safe_url("admin:hq_tables_category", cat["key"]),
                        "label": cat["label"],
                    }
            if ref in {t.ref for t in hub.SECURITY_TABLES}:
                return {"url": safe_url("admin:hq_security"), "label": "Security"}
            return {"url": tables, "label": "Tables & models"}
        if path.startswith(url):
            return {"url": url, "label": row.get("name") or "the table"}

    if path.startswith(tables) and path != tables:
        return {"url": tables, "label": "Tables & models"}
    return {"url": index, "label": "the dashboard"}


@register.simple_tag(takes_context=True)
def gta_security_visible(context):
    """Whether this administrator can open any of the security logs.

    Asked of `available_apps`, which Django has already filtered by
    permission, rather than of `is_superuser` — a staff account granted
    view-only access to the sign-in log should find the menu item that leads
    to it.
    """
    from sysadmin import hub

    return bool(hub.security_tables(_apps(context), _path(context)))


# ---------------------------------------------------------------------------
# Dashboard numbers
# ---------------------------------------------------------------------------

def _buckets(dates, start, span):
    """Count datetimes into `span` one-day buckets starting at `start`.

    Counted in Python off one flat values_list rather than with TruncDate and
    an annotate, so the buckets land on calendar days in the project's display
    timezone rather than the database session's — an 11pm sign-up in Sydney
    belongs to that day, not the UTC one after it.
    """
    counts = [0] * span
    first = timezone.localtime(start).date()
    for value in dates:
        idx = (timezone.localtime(value).date() - first).days
        if 0 <= idx < span:
            counts[idx] += 1
    return counts


def _spark(data, color):
    """The tile sparklines: a shape, no axes, no numbers.

    A tile already states the number. What it cannot say is whether the number
    has been climbing all fortnight or spiked this morning, which is the whole
    job of the line behind it — so it is drawn without a scale on purpose.
    """
    return {
        "type": "spark", "height": 46, "legend": False,
        "empty": "",
        "series": [{"name": "", "color": color, "data": data}],
    }


@register.simple_tag(takes_context=True)
def gta_dashboard_stats(context):
    """Headline tiles, the activity chart, and what is waiting on somebody.

    Computed in a tag rather than by overriding AdminSite.index() for the same
    reason the system report is bolted on with get_urls(): the stock index view
    keeps working — permissions, the app_list build, the model filtering staff
    users rely on — and the skin only adds to it.

    Every branch is guarded. The dashboard must still render if a model is
    mid-migration; a chrome element is never the thing that takes the admin
    down.
    """
    request = context.get("request")

    # How far back the tiles and the chart look. Clamped to a short list rather
    # than trusting the querystring: the value sizes an in-memory bucket list
    # and widens five otherwise-unbounded queries.
    span = 14
    try:
        requested = int((request.GET.get("days") if request else "") or 14)
        if requested in (14, 30, 90):
            span = requested
    except (TypeError, ValueError, AttributeError):
        pass

    now = timezone.now()
    d24 = now - timezone.timedelta(hours=24)
    window = now - timezone.timedelta(days=span)
    previous = now - timezone.timedelta(days=span * 2)

    out = {"tiles": [], "alerts": [], "span": span, "span_options": (14, 30, 90)}

    try:
        from accounts.models import User
        from admin_panel.models import Enquiry
        from orgs.models import MembershipRequest, Organisation, WallReply

        from sysadmin.models import LoginEvent
    except Exception:
        return out

    def delta(new, old):
        if not old:
            return None
        return round((new - old) / old * 100, 1)

    start = (now - timezone.timedelta(days=span - 1)).replace(
        hour=0, minute=0, second=0, microsecond=0,
    )

    users_now = User.objects.filter(date_joined__gte=window).count()
    users_was = User.objects.filter(date_joined__gte=previous, date_joined__lt=window).count()
    orgs_now = Organisation.objects.filter(created_at__gte=window).count()
    orgs_was = Organisation.objects.filter(created_at__gte=previous, created_at__lt=window).count()
    logins_now = LoginEvent.objects.filter(created_at__gte=d24, success=True).count()
    logins_was = LoginEvent.objects.filter(
        created_at__gte=d24 - timezone.timedelta(hours=24), created_at__lt=d24, success=True,
    ).count()

    pending = MembershipRequest.objects.filter(
        status=MembershipRequest.STATUS_PENDING,
    ).count()
    open_enquiries = Enquiry.objects.filter(status=Enquiry.STATUS_NEW).count()
    held_replies = WallReply.objects.filter(is_approved=False, is_hidden=False).count()
    waiting = pending + open_enquiries + held_replies

    signups = _buckets(
        User.objects.filter(date_joined__gte=start).values_list("date_joined", flat=True),
        start, span,
    )
    new_orgs = _buckets(
        Organisation.objects.filter(created_at__gte=start).values_list("created_at", flat=True),
        start, span,
    )
    sign_ins = _buckets(
        LoginEvent.objects.filter(created_at__gte=start, success=True)
        .values_list("created_at", flat=True),
        start, span,
    )
    # The queue's own arrivals: join requests and enquiries landing per day.
    # Not the size of the queue over time — nothing records when an item was
    # cleared — so it is labelled as arrivals and not as a backlog.
    arrivals = _buckets(
        list(MembershipRequest.objects.filter(created_at__gte=start)
             .values_list("created_at", flat=True))
        + list(Enquiry.objects.filter(created_at__gte=start)
               .values_list("created_at", flat=True)),
        start, span,
    )

    window_label = f"vs previous {span} days"
    out["tiles"] = [
        {
            "label": "Members", "value": User.objects.count(),
            "icon": "ic-f-users", "accent": "var(--gt-c1)", "tone": "green",
            "delta": delta(users_now, users_was), "sub": window_label,
            "url": "admin:accounts_user_changelist",
            "spark": _spark(signups, "--gt-c1"),
        },
        {
            "label": "Organisations", "value": Organisation.objects.count(),
            "icon": "ic-f-org", "accent": "var(--gt-gold)", "tone": "gold",
            "delta": delta(orgs_now, orgs_was), "sub": window_label,
            "url": "admin:orgs_organisation_changelist",
            "spark": _spark(new_orgs, "--gt-gold"),
        },
        {
            "label": "Sign-ins today", "value": logins_now,
            "icon": "ic-f-login", "accent": "var(--gt-c1)", "tone": "green",
            "delta": delta(logins_now, logins_was), "sub": "vs yesterday",
            "url": "admin:sysadmin_loginevent_changelist",
            "spark": _spark(sign_ins, "--gt-c1"),
        },
        {
            "label": "Waiting on you", "value": waiting,
            "icon": "ic-f-clock", "accent": "var(--gt-gold)", "tone": "gold",
            "delta": None, "sub": "needs a decision",
            "url": None,
            "spark": _spark(arrivals, "--gt-gold"),
        },
    ]

    out["activity_chart"] = {
        "type": "area", "height": 300, "labels": [
            timezone.localtime(start + timezone.timedelta(days=i)).strftime("%-d %b")
            for i in range(span)
        ],
        "summary": f"New members and successful sign-ins per day over the last {span} days",
        "empty": f"No sign-ups or sign-ins in the last {span} days",
        "legend": False,
        "series": [
            {"name": "New members", "color": "--gt-c1", "data": signups},
            {"name": "Successful sign-ins", "color": "--gt-gold", "data": sign_ins},
        ],
    }

    # Each row is a real queue with a real destination. A number nobody can act
    # on belongs on the system report, not on the screen headed "waiting".
    out["alerts"] = [
        {
            "label": "Pending join requests", "count": pending,
            "url": safe_url("admin:orgs_membershiprequest_changelist"),
            "query": f"?status__exact={MembershipRequest.STATUS_PENDING}",
        },
        {
            "label": "Open enquiries", "count": open_enquiries,
            "url": safe_url("admin:hq_enquiries"), "query": "",
        },
        {
            "label": "Wall replies to approve", "count": held_replies,
            "url": safe_url("admin:orgs_wallreply_changelist"),
            "query": "?is_approved__exact=0",
        },
    ]
    out["waiting_total"] = waiting
    return out


@register.simple_tag(takes_context=True)
def gta_alerts(context):
    """Just the badge counts, for the menu.

    Superuser-only and wrapped in a try: the menu renders on every admin page
    including the login screen and error pages.
    """
    request = context.get("request")
    user = getattr(request, "user", None)
    if not (user and user.is_authenticated and user.is_superuser):
        return {}
    try:
        from admin_panel.models import Enquiry
        from orgs.models import MembershipRequest

        return {
            "approvals": MembershipRequest.objects.filter(
                status=MembershipRequest.STATUS_PENDING,
            ).count(),
            "enquiries": Enquiry.objects.filter(status=Enquiry.STATUS_NEW).count(),
        }
    except Exception:
        return {}


@register.filter
def compact_since(value):
    """"2m", "3h", "5d" — the age of something, in one glance.

    Django's `timesince` renders "2 minutes, 30 seconds", which is three times
    the width for the same fact in a column that is only ever scanned.
    """
    if not value:
        return ""
    seconds = (timezone.now() - value).total_seconds()
    if seconds < 60:
        return "just now"
    for cutoff, divisor, suffix in (
        (3600, 60, "m"),
        (86400, 3600, "h"),
        (2592000, 86400, "d"),
    ):
        if seconds < cutoff:
            return f"{int(seconds // divisor)}{suffix} ago"
    return f"{int(seconds // 2592000)}mo ago"


# ---------------------------------------------------------------------------
# Delegated administration: what this person is allowed to see
# ---------------------------------------------------------------------------

@register.simple_tag(takes_context=True)
def gta_can(context, capability):
    """Whether the signed-in administrator holds one capability.

    Used by the menu. A restricted administrator seeing a link they cannot open
    is worse than not seeing it: they click it, get refused, and learn that the
    product is broken rather than that they were not given that job.
    """
    from sysadmin import access

    request = context.get("request")
    return access.can(getattr(request, "user", None), capability)


@register.simple_tag(takes_context=True)
def gta_is_full_access(context):
    from sysadmin import access

    request = context.get("request")
    return access.is_full_access(getattr(request, "user", None))


@register.simple_tag(takes_context=True)
def gta_review_counts(context):
    """What is waiting on this person, for the badges in the menu.

    Two different numbers depending on who is asking: a full-access
    administrator wants the size of the review queue, and everybody else wants
    to know how much of their own work is still sitting in it.
    """
    from sysadmin import access
    from sysadmin.models import AdminTask, ChangeRequest

    request = context.get("request")
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        return {}

    out = {
        "tasks": AdminTask.objects.filter(
            assigned_to=user, status=AdminTask.OPEN).count(),
        "mine_waiting": ChangeRequest.objects.filter(
            requested_by=user, status=ChangeRequest.PENDING).count(),
    }
    if access.is_full_access(user):
        out["to_review"] = ChangeRequest.objects.filter(
            status=ChangeRequest.PENDING).exclude(requested_by=user).count()
    # One badge on one rail row now stands for the whole Your team hub, so the
    # sum belongs here rather than being added up with template filters — the
    # `add` filter yields "" for a missing key, which silently blanks a badge
    # that was meant to say 3.
    out["total"] = out.get("tasks", 0) + out.get("to_review", 0)
    return out


# ---------------------------------------------------------------------------
# Delegated administration: the picker's chrome
# ---------------------------------------------------------------------------

# One icon per capability group, for the picker's section headings. Purely
# presentational, which is why it lives here and not on the Group dataclass —
# capabilities.py states what an administrator may do, and it should not have
# to be edited to change a drawing.
CAP_GROUP_ICONS = {
    "news": "ic-doc",
    "pages": "ic-globe",
    "enquiries": "ic-mail",
    "orgs": "ic-org",
    "people": "ic-users",
    "charities": "ic-heart",
    "data": "ic-cloud-sync",
}


@register.simple_tag
def cap_group_icon(key):
    return CAP_GROUP_ICONS.get(key, "ic-sliders")
