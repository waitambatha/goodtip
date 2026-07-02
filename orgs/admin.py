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
        "name", "group_type", "state", "industry",
        "is_public_listed", "season", "charity", "created_at",
    )
    list_filter = ("group_type", "state", "industry", "is_public_listed", "season", "competitions")
    list_editable = ("is_public_listed",)
    readonly_fields = ("public_consent_at", "public_consent_by", "public_consent_reconfirmed")
    search_fields = ("name",)
    inlines = [OrgMemberInline]


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
