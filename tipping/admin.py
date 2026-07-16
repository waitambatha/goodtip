from django.contrib import admin

from .models import Match, Round, Team, Tip


@admin.register(Team)
class TeamAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "series", "has_logo")
    list_filter = ("series",)
    search_fields = ("name", "slug")

    @admin.display(boolean=True, description="Logo uploaded")
    def has_logo(self, obj):
        return bool(obj.logo)


@admin.register(Round)
class RoundAdmin(admin.ModelAdmin):
    list_display = ("org", "round_number", "series", "stage", "status", "lockout_at")
    list_filter = ("status", "stage", "series")


@admin.register(Match)
class MatchAdmin(admin.ModelAdmin):
    list_display = ("home_team", "away_team", "round", "kickoff_at", "result")
    list_filter = ("round__series",)


@admin.register(Tip)
class TipAdmin(admin.ModelAdmin):
    list_display = ("user", "match", "org", "selection", "is_correct", "points_awarded")
    list_filter = ("selection", "is_correct")
