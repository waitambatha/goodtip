from django.contrib import admin

from .models import Competition, Season, Sport


@admin.register(Sport)
class SportAdmin(admin.ModelAdmin):
    list_display = ("name", "slug")


@admin.register(Competition)
class CompetitionAdmin(admin.ModelAdmin):
    list_display = ("name", "sport", "is_womens", "slug")
    list_filter = ("sport", "is_womens")


@admin.register(Season)
class SeasonAdmin(admin.ModelAdmin):
    list_display = ("year", "label")
