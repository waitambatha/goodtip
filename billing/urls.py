from django.urls import path

from . import views


app_name = "billing"

urlpatterns = [
    path("<int:org_id>/plans/", views.plans_view, name="plans"),
    path("<int:org_id>/checkout/", views.checkout_view, name="checkout"),
    path("<int:org_id>/request-custom-price/", views.request_custom_price_view, name="request_custom_price"),
    path("<int:org_id>/decline-custom-offer/", views.decline_custom_offer_view, name="decline_custom_offer"),
    path("<int:org_id>/custom-checkout/", views.custom_checkout_view, name="custom_checkout"),
    # Paying happens here, on GoodTip, with Stripe's embedded card form.
    path("<int:org_id>/pay/", views.pay_view, name="pay"),
    path("<int:org_id>/pay/confirm/", views.pay_confirm_view, name="pay_confirm"),
    # Changing an existing subscription — the card on file is reused for both.
    path("<int:org_id>/switch-tier/", views.switch_tier_view, name="switch_tier"),
    path("<int:org_id>/cancel/", views.cancel_subscription_view, name="cancel_subscription"),
    path("<int:org_id>/resume/", views.resume_subscription_view, name="resume_subscription"),
    path("<int:org_id>/pay-now/", views.pay_now_view, name="pay_now"),
    path("<int:org_id>/success/", views.success_view, name="success"),
    path("<int:org_id>/subscription-status/", views.subscription_status_view, name="subscription_status"),
    path("<int:org_id>/demo-start/", views.demo_start_view, name="demo_start"),
    # The donation pledge page and the participant top-up flow are gone
    # (client wording spec, 18 Aug 2026). GoodTip funds the donation from its
    # own revenue now, so there is no pledge for an organisation to set and
    # nothing for a participant to add. The views and templates are removed
    # with them rather than left unreachable — a dead screen is a screen
    # somebody eventually links to again.
    path("<int:org_id>/season-summary/", views.season_summary_view, name="season_summary"),
    path("<int:org_id>/esg-report/", views.esg_report_view, name="esg_report"),
    path("<int:org_id>/receipt/", views.receipt_view, name="receipt"),
]
