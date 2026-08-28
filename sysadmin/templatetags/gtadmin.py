"""Template helpers for the GoodTip admin skin.

Only two things need Python here: the counts that ride on the sub-nav (so a
waiting approval or an unanswered enquiry is visible from any admin page,
which is the whole point of putting them in the chrome), and the icon lookup
that gives each installed app a recognisable mark instead of a letter.
"""
from django import template
from django.urls import NoReverseMatch, reverse

register = template.Library()


# Maps an app's label to (icon id from public/partials/_icons.html, accent).
# Anything not listed falls back to a neutral document icon — a new app added
# later renders correctly without touching this table.
APP_ICONS = {
    "accounts":    ("ic-users", "var(--gt-c1)"),
    "orgs":        ("ic-org", "var(--gt-c2)"),
    "tipping":     ("ic-target", "var(--gt-c3)"),
    "catalog":     ("ic-sliders", "var(--gt-c4)"),
    "billing":     ("ic-coins", "var(--gt-c5)"),
    "admin_panel": ("ic-msg", "var(--gt-c2)"),
    "sysadmin":    ("ic-shield-check", "var(--gt-c6)"),
    "data_sync":   ("ic-cloud-sync", "var(--gt-c4)"),
    "auth":        ("ic-lock", "var(--gt-c5)"),
}

MODEL_ICONS = {
    "user": "ic-users",
    "organisation": "ic-org",
    "charity": "ic-heart",
    "match": "ic-match",
    "round": "ic-calendar",
    "tip": "ic-target",
    "newspost": "ic-doc",
    "enquiry": "ic-mail",
    "loginevent": "ic-shield",
    "stresstestrun": "ic-flask",
    "syncrun": "ic-sync",
}


@register.simple_tag
def app_icon(app_label):
    return APP_ICONS.get(app_label, ("ic-doc", "var(--gt-forest)"))[0]


@register.simple_tag
def app_accent(app_label):
    return APP_ICONS.get(app_label, ("ic-doc", "var(--gt-forest)"))[1]


@register.simple_tag
def model_icon(object_name):
    return MODEL_ICONS.get((object_name or "").lower(), "ic-doc")


@register.simple_tag(takes_context=True)
def gta_nav_counts(context):
    """Pending approvals and unanswered enquiries, for the sub-nav badges.

    Superuser-only and wrapped in a try: the sub-nav renders on every admin
    page including the login screen and error pages, and a chrome element must
    never be the thing that takes the admin down.
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


@register.simple_tag
def safe_url(name, *args):
    """reverse() that yields "" instead of raising.

    The sub-nav links into /manage/, which is a different app's URLconf; if it
    is ever renamed the admin should lose a link, not 500 on every page.
    """
    try:
        return reverse(name, args=args)
    except NoReverseMatch:
        return ""


@register.simple_tag
def gta_dashboard_stats():
    """Headline numbers + a 14-day activity series for the admin dashboard.

    Computed in a tag rather than by overriding AdminSite.index() for the same
    reason the system report is bolted on with get_urls(): the stock index view
    keeps working (permissions, app_list building, the model filtering staff
    users rely on) and the skin only adds to it.

    Every branch is guarded — the dashboard must still render if a model is
    mid-migration.
    """
    from django.utils import timezone

    now = timezone.now()
    d7 = now - timezone.timedelta(days=7)
    d24 = now - timezone.timedelta(hours=24)
    prev7 = now - timezone.timedelta(days=14)

    out = {"tiles": [], "days": [], "signups": [], "logins": []}

    try:
        from accounts.models import User
        from admin_panel.models import Enquiry
        from orgs.models import MembershipRequest, Organisation

        from sysadmin.models import LoginEvent
    except Exception:
        return out

    def delta(new, old):
        if not old:
            return None
        return round((new - old) / old * 100)

    users_total = User.objects.count()
    users_7d = User.objects.filter(date_joined__gte=d7).count()
    users_prev = User.objects.filter(date_joined__gte=prev7, date_joined__lt=d7).count()

    orgs_total = Organisation.objects.count()
    orgs_7d = Organisation.objects.filter(created_at__gte=d7).count()
    orgs_prev = Organisation.objects.filter(created_at__gte=prev7, created_at__lt=d7).count()

    pending = MembershipRequest.objects.filter(status=MembershipRequest.STATUS_PENDING).count()
    open_enq = Enquiry.objects.filter(status=Enquiry.STATUS_NEW).count()
    logins_24h = LoginEvent.objects.filter(created_at__gte=d24, success=True).count()

    out["tiles"] = [
        {
            "label": "Members", "value": users_total, "icon": "ic-users",
            "accent": "var(--gt-c1)", "sub": f"+{users_7d} in the last 7 days",
            "delta": delta(users_7d, users_prev), "url": "admin:accounts_user_changelist",
        },
        {
            "label": "Organisations", "value": orgs_total, "icon": "ic-org",
            "accent": "var(--gt-c2)", "sub": f"+{orgs_7d} in the last 7 days",
            "delta": delta(orgs_7d, orgs_prev), "url": "admin:orgs_organisation_changelist",
        },
        {
            "label": "Sign-ins today", "value": logins_24h, "icon": "ic-login",
            "accent": "var(--gt-c4)", "sub": "successful, last 24 hours",
            "delta": None, "url": "admin:sysadmin_loginevent_changelist",
        },
        {
            "label": "Waiting on you", "value": pending + open_enq, "icon": "ic-bell",
            "accent": "var(--gt-c3)" if (pending + open_enq) else "var(--gt-c1)",
            "sub": f"{pending} join request{'' if pending == 1 else 's'}, "
                   f"{open_enq} enquir{'y' if open_enq == 1 else 'ies'}",
            "delta": None, "url": None,
        },
    ]

    # 14 one-day buckets, oldest first. Counted in Python off two flat queries
    # rather than with TruncDate/annotate so the buckets land in the project's
    # display timezone and not the database's.
    start = (now - timezone.timedelta(days=13)).replace(hour=0, minute=0, second=0, microsecond=0)
    buckets = [start + timezone.timedelta(days=i) for i in range(14)]
    out["days"] = [timezone.localtime(b).strftime("%-d %b") for b in buckets]

    def bucket(dates):
        counts = [0] * 14
        for dt in dates:
            idx = (timezone.localtime(dt).date() - timezone.localtime(start).date()).days
            if 0 <= idx < 14:
                counts[idx] += 1
        return counts

    out["signups"] = bucket(User.objects.filter(date_joined__gte=start).values_list("date_joined", flat=True))
    out["logins"] = bucket(
        LoginEvent.objects.filter(created_at__gte=start, success=True).values_list("created_at", flat=True)
    )
    out["activity_chart"] = {
        "type": "area",
        "height": 220,
        "labels": out["days"],
        "summary": "New members and successful sign-ins per day over the last 14 days",
        "empty": "No sign-ups or sign-ins in the last 14 days",
        "series": [
            {"name": "New members", "color": "--gt-c1", "data": out["signups"]},
            {"name": "Sign-ins", "color": "--gt-c4", "data": out["logins"]},
        ],
    }
    return out
