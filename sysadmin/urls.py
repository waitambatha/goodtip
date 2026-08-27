from django.urls import path

from . import views


app_name = "sysadmin"

urlpatterns = [
    # Inside /admin/ so the gate and the thing it guards share a prefix — the
    # middleware keys off that prefix, and a verify screen living outside it
    # would have to be special-cased in two places instead of one.
    path("verify/", views.admin_verify, name="admin_verify"),
    path("verify/resend/", views.admin_verify_resend, name="admin_verify_resend"),
    path("verify/cancel/", views.admin_verify_cancel, name="admin_verify_cancel"),
]
