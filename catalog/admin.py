from django.contrib import admin

from .models import (
    Charity,
    Competition,
    GoodListConfig,
    GroupType,
    Season,
    Series,
    State,
    Sport,
    SubCategory,
)


@admin.register(State)
class StateAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "sort_order")


@admin.register(GroupType)
class GroupTypeAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "sort_order")
    prepopulated_fields = {"slug": ("name",)}


@admin.register(SubCategory)
class SubCategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "group_type", "is_active", "sort_order", "slug")
    list_filter = ("group_type", "is_active")
    list_editable = ("is_active", "sort_order")
    prepopulated_fields = {"slug": ("name",)}


@admin.register(GoodListConfig)
class GoodListConfigAdmin(admin.ModelAdmin):
    list_display = ("privacy_min_groups", "credibility_min_groups", "updated_at")

    def has_add_permission(self, request):
        # Singleton — edit the one row, never add more.
        return not GoodListConfig.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Sport)
class SportAdmin(admin.ModelAdmin):
    list_display = ("name", "slug")


@admin.register(Series)
class SeriesAdmin(admin.ModelAdmin):
    list_display = ("name", "sport", "category", "representation_type", "slug")
    list_filter = ("sport", "category")


@admin.register(Competition)
class CompetitionAdmin(admin.ModelAdmin):
    list_display = ("name", "season", "sport", "slug")
    list_filter = ("season", "sport")
    filter_horizontal = ("series",)


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
