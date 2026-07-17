from django.contrib import admin

from .models import LaunchSignup


@admin.register(LaunchSignup)
class LaunchSignupAdmin(admin.ModelAdmin):
    list_display = ("name", "email", "current_platform", "created_at")
    list_filter = ("current_platform",)
    search_fields = ("name", "email")
