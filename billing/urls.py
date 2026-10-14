from django.urls import path

from . import views


app_name = "billing"

urlpatterns = [
    path("<int:org_id>/plans/", views.plans_view, name="plans"),
    path("<int:org_id>/checkout/", views.checkout_view, name="checkout"),
    path("<int:org_id>/success/", views.success_view, name="success"),
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
