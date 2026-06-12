from django.urls import path

from . import views


app_name = "billing"

urlpatterns = [
    path("<int:org_id>/plans/", views.plans_view, name="plans"),
    path("<int:org_id>/checkout/", views.checkout_view, name="checkout"),
    path("<int:org_id>/success/", views.success_view, name="success"),
]
