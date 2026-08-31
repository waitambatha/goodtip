"""Template helpers for the GoodTip admin skin.

Three jobs, and each of them exists because the stock admin cannot answer a
question the control plane is asked every day:

* SECTIONS turns Django's flat `available_apps` into the ten groups the menu
  and the dashboard both show. The app list alone put Season between Sport and
  Charity with nothing separating catalog from tipping, and that is what a
  25-entry rail looks like when nobody has grouped it.
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
# Ten groups over the ~25 registered models, in the order the menu shows them.
# Members are named "app_label.model_name" and listed in the order they should
# appear inside their group, which is rarely alphabetical — under Tipping, Tip
# is the one people open and Team is the one they almost never do.
#
# ANYTHING NOT LISTED STILL SHOWS UP. Unclaimed models are collected into a
# final "Everything else" group rather than dropped, so registering a model and
# forgetting this table costs you a tidy heading, not access to your data.
#
# Nothing here grants access. The rows are matched against `available_apps`,
# which Django has already filtered by permission, so a section renders only
# the models this particular user was going to be shown anyway.
SECTIONS = [
    ("users", "Users", "ic-users", "var(--gt-c1)", [
        "accounts.user", "accounts.launchsignup", "auth.group",
    ]),
    ("organisations", "Organisations", "ic-org", "var(--gt-gold)", [
        "orgs.organisation", "orgs.membershiprequest", "billing.plansubscription",
    ]),
    ("wall", "The Wall", "ic-msg", "var(--gt-c2)", [
        "orgs.wallpost", "orgs.wallreply",
    ]),
    ("charities", "Charities", "ic-heart", "var(--gt-c6)", [
        "catalog.charity", "orgs.charityvote", "orgs.charityvoteballot",
    ]),
    ("tipping", "Tipping", "ic-target", "var(--gt-c1)", [
        "tipping.tip", "tipping.match", "tipping.round", "tipping.team",
    ]),
    ("competitions", "Competitions", "ic-trophy", "var(--gt-gold)", [
        "catalog.competition", "catalog.series", "catalog.sport", "catalog.season",
    ]),
    ("catalog", "Catalogue", "ic-sliders", "var(--gt-c4)", [
        "catalog.state", "catalog.organisationtype", "catalog.subcategory",
        "catalog.goodlistconfig",
    ]),
    ("news", "News & blog", "ic-doc", "var(--gt-c2)", [
        "admin_panel.newspost",
    ]),
    ("sync", "Data sync", "ic-cloud-sync", "var(--gt-c4)", [
        "data_sync.syncrun",
    ]),
    ("security", "Security", "ic-shield-check", "var(--gt-c6)", [
        "sysadmin.loginevent", "sysadmin.auditlog", "sysadmin.stresstestrun",
    ]),
]

FALLBACK_SECTION = ("other", "Everything else", "ic-sliders", "var(--gt-c5)")

MODEL_ICONS = {
    "user": "ic-users",
    "launchsignup": "ic-send",
    "group": "ic-people",
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


@register.simple_tag(takes_context=True)
def gta_sections(context):
    """`available_apps`, regrouped by SECTIONS.

    Returns a list of dicts the menu and the dashboard grid both render:

        {key, label, icon, accent, models: [...], count, is_open}

    `is_open` marks the group holding the page you are on, so arriving at a
    changelist from anywhere finds its group already expanded instead of
    leaving you to guess which of ten it lives under.
    """
    apps = context.get("available_apps") or context.get("app_list") or []
    request = context.get("request")
    path = getattr(request, "path", "") or ""

    # Flatten to "app_label.model_name" -> the dict Django built, keeping the
    # app label on it so the fallback group can still say where a model came
    # from. Django spells the key `object_name` on some versions and `model`
    # on others; the lowercase object name is stable across both.
    flat = {}
    for app in apps:
        label = app.get("app_label", "")
        for model in app.get("models", []):
            name = (model.get("object_name") or "").lower()
            if not name:
                continue
            row = dict(model)
            row["app_label"] = label
            row["app_name"] = app.get("name", "")
            flat[f"{label}.{name}"] = row

    claimed = set()
    sections = []
    for key, label, icon, accent, members in SECTIONS:
        rows = []
        for ref in members:
            row = flat.get(ref)
            if row is None:
                continue        # not registered, or not this user's to see
            claimed.add(ref)
            rows.append(row)
        if not rows:
            continue            # an empty heading is worse than no heading
        sections.append({
            "key": key,
            "label": label,
            "icon": icon,
            "accent": accent,
            "models": rows,
            "count": len(rows),
            "is_open": any(
                r.get("admin_url") and path.startswith(r["admin_url"]) for r in rows
            ),
        })

    leftovers = [flat[ref] for ref in flat if ref not in claimed]
    if leftovers:
        key, label, icon, accent = FALLBACK_SECTION
        leftovers.sort(key=lambda r: (r["app_label"], r.get("name", "")))
        sections.append({
            "key": key,
            "label": label,
            "icon": icon,
            "accent": accent,
            "models": leftovers,
            "count": len(leftovers),
            "is_open": any(
                r.get("admin_url") and path.startswith(r["admin_url"]) for r in leftovers
            ),
        })
    return sections


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
    return out
