from django.contrib import admin, messages
from django.utils.html import format_html

from .models import (
    Charity,
    Competition,
    GoodListConfig,
    OrganisationType,
    Season,
    Series,
    State,
    Sport,
    SubCategory,
)


@admin.register(State)
class StateAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "sort_order")


@admin.register(OrganisationType)
class OrganisationTypeAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "sort_order")
    prepopulated_fields = {"slug": ("name",)}


@admin.register(SubCategory)
class SubCategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "organisation_type", "is_active", "sort_order", "slug")
    list_filter = ("organisation_type", "is_active")
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
    list_display = ("name", "_logo", "is_approved", "website", "slug")
    actions = ("refetch_logos",)

    @admin.display(description="Logo")
    def _logo(self, obj):
        if not obj.logo:
            return "—" if obj.logo_fetched_at else "not tried"
        return format_html(
            '<img src="{}" style="height:26px;width:26px;object-fit:contain;'
            'background:#fff;border-radius:5px;padding:2px">', obj.logo.url,
        )

    @admin.action(description="Fetch the logo again from the charity's own site")
    def refetch_logos(self, request, queryset):
        """Retry the fetch for the selected charities.

        Synchronous on purpose, unlike the wizard's fire-and-forget thread:
        somebody who ticked five rows and chose this is waiting for an answer
        about those five rows, and "it might have worked" is not one.
        """
        from catalog.logos import backfill_charity

        got = failed = 0
        for charity in queryset:
            try:
                backfill_charity(charity, force=True)
            except Exception:                       # noqa: BLE001
                failed += 1
                continue
            charity.refresh_from_db(fields=["logo"])
            if charity.logo:
                got += 1
            else:
                failed += 1
        if got:
            self.message_user(request, f"Fetched {got} logo{'' if got == 1 else 's'}.")
        if failed:
            self.message_user(
                request,
                f"{failed} still without one — their site published nothing usable.",
                level=messages.WARNING,
            )
    list_filter = ("is_approved",)
    search_fields = ("name",)
    prepopulated_fields = {"slug": ("name",)}
    list_editable = ("is_approved",)
