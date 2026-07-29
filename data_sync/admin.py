from django.contrib import admin

from .models import SyncRun


@admin.register(SyncRun)
class SyncRunAdmin(admin.ModelAdmin):
    list_display = ("started_at", "kind", "competition", "org", "round_number",
                    "ok", "matches_touched", "duration_ms")
    list_filter = ("kind", "ok", "competition")
    search_fields = ("competition", "org__name", "message")
    date_hierarchy = "started_at"
    # Rows are written by the sync command and the admin sync panel, never by
    # hand — editing one would only falsify the history.
    readonly_fields = ("kind", "competition", "org", "round_number", "started_at",
                       "finished_at", "ok", "matches_touched", "message")

    def has_add_permission(self, request):
        return False
