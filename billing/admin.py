from django.contrib import admin

from .models import PlanSubscription


@admin.register(PlanSubscription)
class PlanSubscriptionAdmin(admin.ModelAdmin):
    list_display = ("org", "tier", "price_aud", "status", "seat_limit", "paid_at")
    list_filter = ("status", "tier", "season")
    search_fields = ("org__name", "stripe_payment_intent_id")
    readonly_fields = ("stripe_checkout_session_id", "stripe_payment_intent_id", "paid_at", "created_at")
