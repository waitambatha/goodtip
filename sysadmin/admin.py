import json
import os
import shutil

from django.conf import settings
from django.contrib import admin
from django.contrib.admin.models import ADDITION, CHANGE, DELETION
from django.urls import path
from django.utils import timezone
from django.utils.html import format_html

from .models import AuditLog, LoginEvent, StressTestRun


@admin.register(LoginEvent)
class LoginEventAdmin(admin.ModelAdmin):
    list_display = ("created_at", "email", "user", "success", "ip_address")
    list_filter = ("success",)
    search_fields = ("email", "user__email", "ip_address")
    date_hierarchy = "created_at"
    # Written only by the login/failed-login signals — editing a row here
    # would just falsify the history.
    readonly_fields = ("user", "email", "success", "ip_address", "user_agent", "created_at")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    """The audit log — who changed what, in the admin, and when.

    Django writes LogEntry on every admin save and delete but registers no
    ModelAdmin for it, so the only view of it anyone had was the dashboard's
    "recent actions" panel: your own last handful, and no way to see anybody
    else's or anything older. That is a strange gap in a control plane where
    several people share superuser.

    Read-only in all three directions. A record of what happened that can be
    edited is not a record of what happened, and deleting rows here is how an
    audit log stops being one.
    """
    list_display = ("action_time", "user", "content_type", "object_repr", "_action")
    list_filter = ("action_flag", "content_type")
    search_fields = ("object_repr", "change_message", "user__email")
    date_hierarchy = "action_time"
    list_select_related = ("user", "content_type")

    @admin.display(description="Action", ordering="action_flag")
    def _action(self, obj):
        return {ADDITION: "Added", CHANGE: "Changed", DELETION: "Deleted"}.get(
            obj.action_flag, obj.action_flag,
        )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(StressTestRun)
class StressTestRunAdmin(admin.ModelAdmin):
    list_display = (
        "label", "started_at", "total_requests", "success_count",
        "failure_count", "avg_response_ms", "requests_per_sec", "created_by",
    )
    list_filter = ("label",)
    search_fields = ("label", "target", "notes")
    date_hierarchy = "started_at"
    readonly_fields = ("created_by", "created_at", "_raw_results_pretty")
    fields = (
        "label", "target", "started_at", "finished_at",
        "total_requests", "success_count", "failure_count",
        "avg_response_ms", "p95_response_ms", "max_response_ms", "requests_per_sec",
        "notes", "_raw_results_pretty", "created_by", "created_at",
    )

    @admin.display(description="Raw results")
    def _raw_results_pretty(self, obj):
        if not obj.raw_results:
            return "—"
        return format_html("<pre>{}</pre>", json.dumps(obj.raw_results, indent=2, default=str))

    def save_model(self, request, obj, form, change):
        if not change and obj.created_by_id is None:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)


# ---------------------------------------------------------------------------
# System report — a live-computed page inside the default /admin/, so the
# superuser has one control plane rather than a second custom dashboard.
# Bolted onto the existing admin.site via get_urls(), which is the documented
# way to add a page without a custom AdminSite subclass — admin.site.admin_view
# gives login/staff-required + CSRF + never-cache for free, the same wrapper
# Django uses for every built-in admin page.
# ---------------------------------------------------------------------------

def _day_buckets(dates, start, span):
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


def system_report_view(request):
    from django.contrib.admin.sites import site
    from django.db.models import Count
    from django.shortcuts import render
    from django.http import HttpResponseForbidden

    if not request.user.is_superuser:
        return HttpResponseForbidden()

    from orgs.models import MembershipRequest, OrgMember, Organisation, Group
    from admin_panel.models import Enquiry
    from accounts.models import User
    from tipping.models import Tip

    now = timezone.now()
    last_24h = now - timezone.timedelta(hours=24)
    last_7d = now - timezone.timedelta(days=7)
    last_30d = now - timezone.timedelta(days=30)

    # How far back the trend charts look. Clamped to a short list rather than
    # trusting the querystring: the value sizes an in-memory bucket list and
    # widens four unbounded queries.
    span = 30
    try:
        requested = int(request.GET.get("days", 30))
        if requested in (7, 30, 90):
            span = requested
    except (TypeError, ValueError):
        pass
    window_start = (now - timezone.timedelta(days=span - 1)).replace(
        hour=0, minute=0, second=0, microsecond=0,
    )

    counts = {
        "users": User.objects.count(),
        "orgs": Organisation.objects.count(),
        "groups": Group.objects.count(),
        "memberships": OrgMember.objects.count(),
        "orgs_last_7d": Organisation.objects.filter(created_at__gte=last_7d).count(),
        "orgs_last_30d": Organisation.objects.filter(created_at__gte=last_30d).count(),
        "users_last_7d": User.objects.filter(date_joined__gte=last_7d).count(),
        "pending_join_requests": MembershipRequest.objects.filter(
            status=MembershipRequest.STATUS_PENDING,
        ).count(),
        "open_enquiries": Enquiry.objects.filter(status=Enquiry.STATUS_NEW).count(),
        "tips": Tip.objects.count(),
        "tips_last_7d": Tip.objects.filter(submitted_at__gte=last_7d).count(),
    }

    login_counts = {
        "last_24h": LoginEvent.objects.filter(created_at__gte=last_24h, success=True).count(),
        "last_7d": LoginEvent.objects.filter(created_at__gte=last_7d, success=True).count(),
        "failed_last_24h": LoginEvent.objects.filter(created_at__gte=last_24h, success=False).count(),
        "failed_last_7d": LoginEvent.objects.filter(created_at__gte=last_7d, success=False).count(),
    }
    # Share of sign-in attempts that failed. A number worth watching on its own:
    # a normal week sits in single figures, and a spike is either an outage in
    # the login flow or somebody working through a password list.
    attempts_7d = login_counts["last_7d"] + login_counts["failed_last_7d"]
    login_counts["fail_rate_7d"] = round(
        login_counts["failed_last_7d"] / attempts_7d * 100, 1,
    ) if attempts_7d else 0.0

    labels = [
        timezone.localtime(window_start + timezone.timedelta(days=i)).strftime("%-d %b")
        for i in range(span)
    ]

    signups = _day_buckets(
        User.objects.filter(date_joined__gte=window_start).values_list("date_joined", flat=True),
        window_start, span,
    )
    new_orgs = _day_buckets(
        Organisation.objects.filter(created_at__gte=window_start).values_list("created_at", flat=True),
        window_start, span,
    )
    ok_logins = _day_buckets(
        LoginEvent.objects.filter(created_at__gte=window_start, success=True)
        .values_list("created_at", flat=True),
        window_start, span,
    )
    bad_logins = _day_buckets(
        LoginEvent.objects.filter(created_at__gte=window_start, success=False)
        .values_list("created_at", flat=True),
        window_start, span,
    )
    tips_daily = _day_buckets(
        Tip.objects.filter(submitted_at__gte=window_start).values_list("submitted_at", flat=True),
        window_start, span,
    )

    growth_chart = {
        "type": "area", "height": 260, "labels": labels,
        "summary": f"New members and new organisations per day over the last {span} days",
        "empty": f"Nobody signed up in the last {span} days",
        "series": [
            {"name": "New members", "color": "--gt-c1", "data": signups},
            {"name": "New organisations", "color": "--gt-c2", "data": new_orgs},
        ],
    }
    login_chart = {
        "type": "bar", "height": 260, "labels": labels, "stacked": True,
        "summary": f"Successful and failed sign-ins per day over the last {span} days",
        "empty": f"No sign-in attempts in the last {span} days",
        "series": [
            {"name": "Signed in", "color": "--gt-c4", "data": ok_logins},
            {"name": "Failed", "color": "--gt-c6", "data": bad_logins},
        ],
    }
    tips_chart = {
        "type": "area", "height": 220, "labels": labels,
        "summary": f"Tips submitted per day over the last {span} days",
        "empty": f"No tips submitted in the last {span} days",
        "legend": False,
        "series": [{"name": "Tips", "color": "--gt-c3", "data": tips_daily}],
    }

    # Organisation size mix. Bucketed rather than plotted per-org: with a few
    # hundred organisations a per-org chart is a wall, and the question this
    # answers is "are we selling to small rooms or to workplaces?".
    sized = (
        Organisation.objects.annotate(n=Count("members"))
        .values_list("n", flat=True)
    )
    size_buckets = [("1–10", 0), ("11–25", 0), ("26–50", 0), ("51–150", 0), ("151+", 0)]
    size_counts = [0, 0, 0, 0, 0]
    for n in sized:
        if n <= 10:
            size_counts[0] += 1
        elif n <= 25:
            size_counts[1] += 1
        elif n <= 50:
            size_counts[2] += 1
        elif n <= 150:
            size_counts[3] += 1
        else:
            size_counts[4] += 1
    size_chart = {
        "type": "donut", "height": 240,
        "centreLabel": "organisations",
        "unit": "Organisations",
        "empty": "No organisations yet",
        "summary": "Organisations grouped by how many members they have",
        "slices": [
            {"label": label, "value": size_counts[i], "color": f"--gt-c{i + 1}"}
            for i, (label, _) in enumerate(size_buckets)
        ],
    }

    # Biggest rooms on the platform.
    top_orgs = list(
        Organisation.objects.annotate(n=Count("members"))
        .filter(n__gt=0).order_by("-n")[:8]
        .values("id", "name", "n")
    )
    top_orgs_chart = {
        "type": "bar", "height": 240,
        "labels": [o["name"][:18] for o in top_orgs],
        "empty": "No organisations have members yet",
        "legend": False,
        "summary": "The eight organisations with the most members",
        "series": [{"name": "Members", "color": "--gt-c2", "data": [o["n"] for o in top_orgs]}],
    }

    enquiry_mix = list(Enquiry.objects.values("status").annotate(n=Count("id")))
    status_labels = dict(Enquiry.STATUS_CHOICES)
    status_colour = {
        Enquiry.STATUS_NEW: "--gt-c3",
        Enquiry.STATUS_REPLIED: "--gt-c1",
        Enquiry.STATUS_CLOSED: "--gt-c4",
    }
    enquiry_chart = {
        "type": "donut", "height": 240,
        "centreLabel": "enquiries",
        "unit": "Enquiries",
        "empty": "No enquiries yet",
        "summary": "Enquiries from the public contact form, by status",
        "slices": [
            {
                "label": status_labels.get(row["status"], row["status"]),
                "value": row["n"],
                "color": status_colour.get(row["status"], "--gt-c5"),
            }
            for row in enquiry_mix
        ],
    }

    recent_logins = list(LoginEvent.objects.select_related("user")[:10])
    recent_stress_tests = list(StressTestRun.objects.all()[:5])

    system = {}
    try:
        usage = shutil.disk_usage(str(settings.BASE_DIR))
        system["disk_total_gb"] = round(usage.total / (1024 ** 3), 1)
        system["disk_used_gb"] = round(usage.used / (1024 ** 3), 1)
        system["disk_free_gb"] = round(usage.free / (1024 ** 3), 1)
        system["disk_used_pct"] = round(usage.used / usage.total * 100, 1)
    except OSError:
        pass
    try:
        load1, load5, load15 = os.getloadavg()
        system["load_avg"] = (round(load1, 2), round(load5, 2), round(load15, 2))
        system["cpu_count"] = os.cpu_count() or 1
        # Load average is only readable against core count — 4.0 is idle on a
        # 16-core box and on fire on a single-core one.
        system["load_pct"] = min(100, round(load1 / system["cpu_count"] * 100, 1))
    except (OSError, AttributeError):
        pass

    return render(request, "admin/sysadmin/system_report.html", {
        **site.each_context(request),
        "title": "System report",
        "counts": counts,
        "login_counts": login_counts,
        "recent_logins": recent_logins,
        "recent_stress_tests": recent_stress_tests,
        "system": system,
        "generated_at": now,
        "span": span,
        "span_options": (7, 30, 90),
        "growth_chart": growth_chart,
        "login_chart": login_chart,
        "tips_chart": tips_chart,
        "size_chart": size_chart,
        "top_orgs": top_orgs,
        "top_orgs_chart": top_orgs_chart,
        "enquiry_chart": enquiry_chart,
    })


_original_get_urls = admin.site.get_urls


def _get_urls():
    # Imported here rather than at module scope: this module is executed by
    # admin autodiscovery, and importing another app's admin code at import
    # time is how circular-import problems get introduced. By the time
    # get_urls() is called every app is loaded.
    from admin_panel import site_content_admin

    # The organisation admin's area kept these three for as long as it was the
    # only admin there was. They are GoodTip's own work, not any
    # organisation's: an enquiry is addressed to the company, the news feed is
    # the company's, and the sync panel drives the platform's fixtures. They
    # answer to the super admin, so they live here now.
    from admin_panel import views as manage_views

    custom = [
        path("system-report/", admin.site.admin_view(system_report_view), name="system_report"),
        path("site-content/", admin.site.admin_view(site_content_admin.index), name="site_content"),
        path("site-content/<slug:slug>/", admin.site.admin_view(site_content_admin.page),
             name="site_content_page"),

        path("sync/", admin.site.admin_view(manage_views.sync_panel), name="hq_sync"),

        path("enquiries/", admin.site.admin_view(manage_views.enquiries), name="hq_enquiries"),
        path("enquiries/<int:enquiry_id>/", admin.site.admin_view(manage_views.enquiry_detail),
             name="hq_enquiry_detail"),

        path("news/", admin.site.admin_view(manage_views.news_list), name="hq_news"),
        path("news/new/", admin.site.admin_view(manage_views.news_new), name="hq_news_new"),
        path("news/upload-image/", admin.site.admin_view(manage_views.news_upload_image),
             name="hq_news_upload_image"),
        path("news/<int:post_id>/edit/", admin.site.admin_view(manage_views.news_edit),
             name="hq_news_edit"),
        path("news/<int:post_id>/toggle/", admin.site.admin_view(manage_views.news_toggle),
             name="hq_news_toggle"),
        path("news/<int:post_id>/announce/", admin.site.admin_view(manage_views.news_announce),
             name="hq_news_announce"),
        path("news/<int:post_id>/delete/", admin.site.admin_view(manage_views.news_delete),
             name="hq_news_delete"),

        # The public-page copy editor moved with the rest of the public site.
        path("pages/", admin.site.admin_view(manage_views.pages_list), name="hq_pages"),
        path("pages/<slug:slug>/", admin.site.admin_view(manage_views.page_edit), name="hq_page_edit"),
        path("pages/<slug:slug>/media/", admin.site.admin_view(manage_views.page_media_upload),
             name="hq_page_media_upload"),
        path("pages/<slug:slug>/media/<int:media_id>/delete/",
             admin.site.admin_view(manage_views.page_media_delete), name="hq_page_media_delete"),
        path("pages/<slug:slug>/media/<int:media_id>/toggle/",
             admin.site.admin_view(manage_views.page_media_toggle), name="hq_page_media_toggle"),
    ]
    return custom + _original_get_urls()


admin.site.get_urls = _get_urls
