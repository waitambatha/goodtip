from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.utils import timezone

from sysadmin.models import LoginEvent

from .models import LaunchSignup, User


@admin.register(LaunchSignup)
class LaunchSignupAdmin(admin.ModelAdmin):
    """The "tell me when it's ready" list.

    THE EXPORT IS THE POINT. This list exists to be mailed once, in a batch, on
    the day sign-ups open — so the question it has to answer is "give me the
    addresses", and until now the only way to get them was to read them off the
    screen. The client is running a function off this form; whatever it
    collects has to be able to leave.
    """

    list_display = ("name", "email", "org_type", "current_platform", "source_page",
                    "created_at", "notified_at")
    list_filter = ("org_type", "current_platform", "source_page", "notified_at")
    search_fields = ("name", "email")
    readonly_fields = ("created_at",)
    date_hierarchy = "created_at"
    actions = ("export_csv", "mark_notified")

    @admin.action(description="Export selected to CSV")
    def export_csv(self, request, queryset):
        import csv

        from django.http import HttpResponse

        response = HttpResponse(content_type="text/csv")
        stamp = timezone.now().strftime("%Y%m%d")
        response["Content-Disposition"] = f'attachment; filename="goodtip-launch-list-{stamp}.csv"'
        writer = csv.writer(response)
        writer.writerow(["Name", "Email", "Org type", "Current platform", "Source", "Signed up", "Notified"])
        for row in queryset:
            writer.writerow([
                row.name, row.email,
                # The LABEL, not the stored key. This file is read by a person
                # deciding who to write to, and "business" tells them less than
                # "Business or workplace".
                row.org_type_label, row.platform_label, row.source_page,
                row.created_at.strftime("%Y-%m-%d %H:%M"),
                row.notified_at.strftime("%Y-%m-%d") if row.notified_at else "",
            ])
        return response

    @admin.action(description="Mark selected as notified")
    def mark_notified(self, request, queryset):
        """So a second batch can skip them. Only ever set by hand, because only
        a person knows whether the email actually went."""
        n = queryset.update(notified_at=timezone.now())
        self.message_user(request, f"{n} marked as notified.")


class LoginEventInline(admin.TabularInline):
    model = LoginEvent
    extra = 0
    max_num = 0
    can_delete = False
    fields = ("created_at", "success", "ip_address", "user_agent")
    readonly_fields = fields
    ordering = ("-created_at",)
    verbose_name_plural = "Recent login history"

    def has_add_permission(self, request, obj=None):
        return False

    def get_queryset(self, request):
        return super().get_queryset(request)[:10]


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    """Not registered at all before this — a superuser had no way to browse
    or manage accounts through the default admin, which is the one control
    plane this app is meant to have."""

    list_display = ("email", "display_name", "is_staff", "is_superuser", "is_active", "last_login", "date_joined")
    list_filter = ("is_staff", "is_superuser", "is_active", "two_factor_enabled")
    search_fields = ("email", "display_name")
    ordering = ("email",)
    inlines = [LoginEventInline]
