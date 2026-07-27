from django.contrib import admin
from django.db.models import Count

from .models import (
    CharityVote,
    CharityVoteBallot,
    CharityVoteOption,
    MembershipRequest,
    OrgMember,
    Organisation,
    WallPost,
    WallReply,
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


@admin.register(WallPost)
class WallPostAdmin(admin.ModelAdmin):
    list_display = ("__str__", "org", "author", "kind", "is_public", "is_hidden", "created_at")
    list_filter = ("kind", "is_public", "is_hidden", "org")
    list_editable = ("is_public", "is_hidden")
    raw_id_fields = ("org", "author", "tip")
    search_fields = ("body", "author__display_name", "org__name")


@admin.register(WallReply)
class WallReplyAdmin(admin.ModelAdmin):
    """The moderation desk for guest replies left on the public /wall/ page.

    Filter to is_approved=No to see the queue; approving publishes it.
    """

    list_display = ("display_name", "post", "excerpt", "is_guest", "is_approved", "is_hidden", "created_at")
    list_filter = ("is_approved", "is_hidden", "created_at")
    list_editable = ("is_approved", "is_hidden")
    raw_id_fields = ("post", "author")
    readonly_fields = ("created_at", "ip_address")
    search_fields = ("body", "guest_name", "guest_email", "author__display_name")
    actions = ("approve_replies", "reject_replies")

    @admin.display(description="Reply")
    def excerpt(self, obj):
        return obj.body[:70] + ("…" if len(obj.body) > 70 else "")

    @admin.display(description="Guest", boolean=True)
    def is_guest(self, obj):
        return obj.author_id is None

    @admin.action(description="Approve selected replies (publishes them)")
    def approve_replies(self, request, queryset):
        n = queryset.update(is_approved=True, is_hidden=False)
        self.message_user(request, f"{n} repl{'y' if n == 1 else 'ies'} published.")

    @admin.action(description="Reject selected replies (hides them)")
    def reject_replies(self, request, queryset):
        n = queryset.update(is_approved=False, is_hidden=True)
        self.message_user(request, f"{n} repl{'y' if n == 1 else 'ies'} rejected.")
