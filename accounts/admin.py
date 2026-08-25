from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from sysadmin.models import LoginEvent

from .models import LaunchSignup, User


@admin.register(LaunchSignup)
class LaunchSignupAdmin(admin.ModelAdmin):
    list_display = ("name", "email", "current_platform", "created_at")
    list_filter = ("current_platform",)
    search_fields = ("name", "email")


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
