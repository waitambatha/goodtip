from django.contrib import admin

from .models import (
    CharityVote,
    CharityVoteBallot,
    CharityVoteOption,
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
        "name", "group_type", "category_label", "state",
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
    readonly_fields = ("public_consent_at", "public_consent_by", "public_consent_reconfirmed")
    search_fields = ("name",)
    inlines = [OrgMemberInline]

    @admin.display(description="Sub-category")
    def category_label(self, obj):
        return obj.category_label


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
