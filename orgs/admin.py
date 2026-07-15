from django.contrib import admin
from django.db.models import Count

from .models import (
    CharityVote,
    CharityVoteBallot,
    CharityVoteOption,
    MembershipRequest,
    OrgMember,
    Organisation,
)


class OrgMemberInline(admin.TabularInline):
    model = OrgMember
    extra = 0
    raw_id_fields = ("user", "invited_by")


@admin.register(Organisation)
class OrganisationAdmin(admin.ModelAdmin):
    list_display = (
        "name", "parent", "child_count", "group_type", "category_label", "state",
        "is_charity_partner", "is_public_listed", "season", "charity", "created_at",
    )
    # is_charity_partner is set HERE and only here (categories doc: partner
    # status is never self-declared) — the org creation flow reads it to pick
    # the lock-to-self vs vote-plus-CTA workflow.
    list_filter = (
        "group_type", "sub_categories", "state",
        "is_charity_partner", "is_public_listed", "season", "competitions",
    )
    list_editable = ("is_charity_partner", "is_public_listed")
    filter_horizontal = ("sub_categories",)
    autocomplete_fields = ("parent",)
    readonly_fields = ("public_consent_at", "public_consent_by", "public_consent_reconfirmed")
    search_fields = ("name",)
    inlines = [OrgMemberInline]

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("parent").annotate(
            _child_count=Count("children", distinct=True)
        )

    @admin.display(description="Children", ordering="_child_count")
    def child_count(self, obj):
        return obj._child_count

    @admin.display(description="Sub-category")
    def category_label(self, obj):
        return obj.category_label


@admin.register(MembershipRequest)
class MembershipRequestAdmin(admin.ModelAdmin):
    list_display = ("user", "org", "status", "created_at", "decided_at", "decided_by")
    list_filter = ("status",)
    raw_id_fields = ("user", "org", "decided_by")
    search_fields = ("user__email", "user__display_name", "org__name")


class CharityVoteOptionInline(admin.TabularInline):
    model = CharityVoteOption
    extra = 0


@admin.register(CharityVote)
class CharityVoteAdmin(admin.ModelAdmin):
    list_display = ("org", "status", "winning_charity", "opened_at", "closed_at")
    list_filter = ("status",)
    inlines = [CharityVoteOptionInline]


@admin.register(CharityVoteBallot)
class CharityVoteBallotAdmin(admin.ModelAdmin):
    list_display = ("vote", "user", "option", "cast_at")
