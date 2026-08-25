import json
import os
import shutil

from django.conf import settings
from django.contrib import admin
from django.urls import path
from django.utils import timezone
from django.utils.html import format_html

from .models import LoginEvent, StressTestRun


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

def system_report_view(request):
    from django.contrib.admin.sites import site
    from django.shortcuts import render
    from django.http import HttpResponseForbidden

    if not request.user.is_superuser:
        return HttpResponseForbidden()

    from orgs.models import MembershipRequest, OrgMember, Organisation, Group
    from admin_panel.models import Enquiry
    from accounts.models import User

    now = timezone.now()
    last_24h = now - timezone.timedelta(hours=24)
    last_7d = now - timezone.timedelta(days=7)
    last_30d = now - timezone.timedelta(days=30)

    counts = {
        "users": User.objects.count(),
        "orgs": Organisation.objects.count(),
        "groups": Group.objects.count(),
        "memberships": OrgMember.objects.count(),
        "orgs_last_7d": Organisation.objects.filter(created_at__gte=last_7d).count(),
        "orgs_last_30d": Organisation.objects.filter(created_at__gte=last_30d).count(),
        "pending_join_requests": MembershipRequest.objects.filter(
            status=MembershipRequest.STATUS_PENDING,
        ).count(),
        "open_enquiries": Enquiry.objects.filter(status=Enquiry.STATUS_NEW).count(),
    }

    login_counts = {
        "last_24h": LoginEvent.objects.filter(created_at__gte=last_24h, success=True).count(),
        "last_7d": LoginEvent.objects.filter(created_at__gte=last_7d, success=True).count(),
        "failed_last_24h": LoginEvent.objects.filter(created_at__gte=last_24h, success=False).count(),
        "failed_last_7d": LoginEvent.objects.filter(created_at__gte=last_7d, success=False).count(),
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
    })


_original_get_urls = admin.site.get_urls


def _get_urls():
    custom = [
        path("system-report/", admin.site.admin_view(system_report_view), name="system_report"),
    ]
    return custom + _original_get_urls()


admin.site.get_urls = _get_urls
