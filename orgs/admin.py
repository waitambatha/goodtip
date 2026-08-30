from django.contrib import admin
from django.contrib.auth.models import Group as AuthGroup
from django.db.models import Count
from django.urls import reverse
from django.utils.html import format_html

from .models import (
    CharityVote,
    CharityVoteBallot,
    CharityVoteOption,
    Group,
    GroupMember,
    MembershipRequest,
    OrgMember,
    Organisation,
    WallPost,
    WallReply,
)


# ---------------------------------------------------------------------------
# "Groups" used to open an empty page.
#
# Two different things were both called Groups. django.contrib.auth's Group is
# the permissions bucket, and this site has never put anybody in one — nothing
# grants is_staff except create_superuser — so its changelist was, correctly,
# always zero rows. GoodTip's own Group (Marketing, IT, Sales — a sub-unit
# inside one organisation) was never registered here at all, so the only place
# a group was visible was nested inside its organisation.
#
# Reading that page, the obvious conclusion is "groups live under
# organisations". They don't: Group is its own table with its own members and
# its own ladder. So the empty permissions Group is taken off the menu and the
# real one takes the name.
# ---------------------------------------------------------------------------
admin.site.unregister(AuthGroup)


class OrgMemberInline(admin.TabularInline):
    model = OrgMember
    extra = 0
    raw_id_fields = ("user", "invited_by")


class GroupInline(admin.TabularInline):
    """The organisation's groups, read-only, with a way through to each.

    Read-only on purpose: a group is worth opening on its own page, where its
    members are. This is the "one organisation, many groups" half of the
    relationship — the other half is the Organisation column on the Groups
    changelist.
    """

    model = Group
    extra = 0
    can_delete = False
    fields = ("open_link", "name", "kind", "approval_status", "member_count", "created_at")
    readonly_fields = ("open_link", "name", "kind", "approval_status", "member_count", "created_at")
    verbose_name = "group"
    verbose_name_plural = "Groups in this organisation"

    def has_add_permission(self, request, obj=None):
        return False

    @admin.display(description="")
    def open_link(self, obj):
        if not obj.pk:
            return "—"
        return format_html(
            '<a href="{}">Open &rarr;</a>',
            reverse("admin:orgs_group_change", args=[obj.pk]),
        )

    @admin.display(description="Members")
    def member_count(self, obj):
        return obj.memberships.count() if obj.pk else 0


@admin.register(Organisation)
class OrganisationAdmin(admin.ModelAdmin):
    list_display = (
        "name", "parent", "child_count", "group_count", "organisation_type", "category_label", "state",
        "is_charity_partner", "is_public_listed", "season", "charity", "created_at",
    )
    # is_charity_partner is set HERE and only here (categories doc: partner
    # status is never self-declared) — the org creation flow reads it to pick
    # the lock-to-self vs vote-plus-CTA workflow.
    list_filter = (
        "organisation_type", "sub_categories", "state",
        "is_charity_partner", "is_public_listed", "season", "competitions",
    )
    list_editable = ("is_charity_partner", "is_public_listed")
    filter_horizontal = ("sub_categories",)
    autocomplete_fields = ("parent",)
    readonly_fields = ("public_consent_at", "public_consent_by", "public_consent_reconfirmed")
    search_fields = ("name",)
    inlines = [OrgMemberInline, GroupInline]

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("parent").annotate(
            _child_count=Count("children", distinct=True),
            _group_count=Count("groups", distinct=True),
        )

    @admin.display(description="Children", ordering="_child_count")
    def child_count(self, obj):
        return obj._child_count

    @admin.display(description="Groups", ordering="_group_count")
    def group_count(self, obj):
        """Links into Groups filtered to this organisation, rather than just
        printing a number the reader then has to go and find."""
        if not obj._group_count:
            return "—"
        return format_html(
            '<a href="{}?org__id__exact={}">{}</a>',
            reverse("admin:orgs_group_changelist"), obj.pk, obj._group_count,
        )

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


class GroupMemberInline(admin.TabularInline):
    """Who is in this group.

    Group membership is its own table rather than a column on OrgMember,
    because someone can be in several groups or none without that touching
    their standing in the organisation — so this is the only place the
    "and has these members" half of the question gets answered.
    """

    model = GroupMember
    extra = 0
    raw_id_fields = ("user",)
    fields = ("user", "is_admin", "joined_at")
    readonly_fields = ("joined_at",)


@admin.register(Group)
class GroupAdmin(admin.ModelAdmin):
    """GoodTip's own groups — Marketing, IT, Sales — one per row.

    Every row names its parent organisation, because that is the constraint
    worth seeing on this page: a group belongs to exactly one organisation
    (`org` is a plain FK, not a M2M), while an organisation can hold any
    number of groups.
    """

    list_display = (
        "name", "org_link", "kind_label_col", "member_count",
        "approval_status", "created_by", "created_at",
    )
    list_filter = ("approval_status", "org", "kind")
    list_select_related = ("org", "kind", "created_by")
    search_fields = ("name", "label", "org__name")
    autocomplete_fields = ("org",)
    raw_id_fields = ("created_by", "approved_by")
    readonly_fields = ("created_at", "approved_at")
    inlines = [GroupMemberInline]
    fieldsets = (
        ("Which organisation", {
            "fields": ("org",),
            "description": (
                "A group sits inside exactly one organisation and inherits its "
                "season, competitions, rules and charity — there is nowhere on a "
                "group to set those. Groups only attach to a top-level "
                "organisation, never to a child of one."
            ),
        }),
        ("The group", {"fields": ("name", "kind", "label")}),
        ("Approval", {
            "fields": ("approval_status", "created_by", "created_at", "approved_by", "approved_at"),
            "description": (
                "A group raised by an ordinary member arrives pending. One "
                "created by an admin is approved on the spot."
            ),
        }),
    )
    actions = ("approve_groups",)

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(
            _member_count=Count("memberships", distinct=True)
        )

    @admin.display(description="Organisation", ordering="org__name")
    def org_link(self, obj):
        return format_html(
            '<a href="{}">{}</a>',
            reverse("admin:orgs_organisation_change", args=[obj.org_id]), obj.org.name,
        )

    @admin.display(description="Kind")
    def kind_label_col(self, obj):
        return obj.kind_label or "—"

    @admin.display(description="Members", ordering="_member_count")
    def member_count(self, obj):
        return obj._member_count

    @admin.action(description="Approve selected groups")
    def approve_groups(self, request, queryset):
        from django.utils import timezone

        n = queryset.filter(approval_status=Group.APPROVAL_PENDING).update(
            approval_status=Group.APPROVAL_APPROVED,
            approved_at=timezone.now(),
            approved_by=request.user,
        )
        self.message_user(request, f"{n} group{'s' if n != 1 else ''} approved.")


@admin.register(GroupMember)
class GroupMemberAdmin(admin.ModelAdmin):
    """Standalone because the question is asked from both ends: "who is in
    Marketing" is the inline above, "which groups is this person in" is a
    search here."""

    list_display = ("user", "group", "group_org", "is_admin", "joined_at")
    list_filter = ("is_admin", "group__org")
    list_select_related = ("group", "group__org", "user")
    search_fields = ("user__email", "user__display_name", "group__name", "group__org__name")
    raw_id_fields = ("user",)
    autocomplete_fields = ("group",)

    @admin.display(description="Organisation", ordering="group__org__name")
    def group_org(self, obj):
        return obj.group.org.name
