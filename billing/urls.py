from django.urls import path

from . import views


app_name = "billing"

urlpatterns = [
    path("<int:org_id>/plans/", views.plans_view, name="plans"),
    path("<int:org_id>/checkout/", views.checkout_view, name="checkout"),
    path("<int:org_id>/success/", views.success_view, name="success"),
    path("<int:org_id>/pledge/", views.pledge_view, name="pledge"),
    path("<int:org_id>/top-up/", views.topup_view, name="topup"),
    path("<int:org_id>/season-summary/", views.season_summary_view, name="season_summary"),
    path("<int:org_id>/esg-report/", views.esg_report_view, name="esg_report"),
    path("<int:org_id>/receipt/", views.receipt_view, name="receipt"),
]
