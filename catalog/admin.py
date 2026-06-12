from django.contrib import admin

from .models import Charity, Competition, Season, Sport


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


@admin.register(Charity)
class CharityAdmin(admin.ModelAdmin):
    list_display = ("name", "is_approved", "website", "slug")
    list_filter = ("is_approved",)
    search_fields = ("name",)
    prepopulated_fields = {"slug": ("name",)}
    list_editable = ("is_approved",)
