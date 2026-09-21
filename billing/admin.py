from django.contrib import admin

from .models import FoundingRate, PlanSubscription, SponsorshipApplication


@admin.register(PlanSubscription)
class PlanSubscriptionAdmin(admin.ModelAdmin):
    list_display = (
        "org", "tier", "price_aud", "is_founding_rate", "list_price_aud",
        "status", "seat_limit", "paid_at",
    )
    list_filter = ("status", "tier", "season", "is_founding_rate")
    search_fields = ("org__name", "stripe_payment_intent_id")
    readonly_fields = ("stripe_checkout_session_id", "stripe_payment_intent_id", "paid_at", "created_at")


@admin.register(FoundingRate)
class FoundingRateAdmin(admin.ModelAdmin):
    """Editable on purpose.

    Locks are granted automatically while the signup window is open, but the
    ones that matter most are the exceptions — a rate promised in a meeting, an
    extension given to a founding partner whose renewal slipped. Those arrive
    as a person telling support what was agreed, and there has to be somewhere
    to record it. ``note`` is what the row is for in those cases, hence it
    being on the list display rather than tucked away in the form.
    """

    list_display = ("org", "tier", "price_aud", "locked_until", "granted_at", "note")
    list_filter = ("tier", "locked_until")
    search_fields = ("org__name", "note")
    readonly_fields = ("granted_at",)
    autocomplete_fields = ("org",)


@admin.register(SponsorshipApplication)
class SponsorshipApplicationAdmin(admin.ModelAdmin):
    """The queue. Ordered newest first by the model's own Meta.

    ``status`` and ``decision_note`` are editable from the list because the
    common action is triage across a screenful of them, not opening one. The
    applicant's own words are deliberately NOT truncated into a column — they
    are the whole basis of the decision and belong on the detail page where
    they can be read properly.
    """

    list_display = (
        "organisation_name", "contact_name", "country", "people",
        "status", "created_at", "decided_by",
    )
    list_filter = ("status", "country", "created_at")
    list_editable = ("status",)
    search_fields = (
        "organisation_name", "contact_name", "email", "reason", "organisation_kind",
    )
    readonly_fields = ("created_at", "user", "org")
    date_hierarchy = "created_at"

    def save_model(self, request, obj, form, change):
        """Stamp who decided, the moment a decision is recorded.

        Approving one of these means GoodTip carries an organisation's fee out
        of a real budget, so the row has to say who made that call. Doing it
        here rather than asking the person to fill in two extra fields is the
        difference between an audit trail that exists and one that is usually
        blank.
        """
        from django.utils import timezone

        decided = obj.status in (obj.STATUS_APPROVED, obj.STATUS_DECLINED)
        if decided and obj.decided_at is None:
            obj.decided_at = timezone.now()
            obj.decided_by = request.user
        elif not decided:
            # Moved back to New or Being reviewed: the decision is withdrawn,
            # so the stamp for it goes too rather than sitting there naming
            # somebody for a decision that no longer stands.
            obj.decided_at = None
            obj.decided_by = None
        super().save_model(request, obj, form, change)
